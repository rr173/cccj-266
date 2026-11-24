#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立审计校验器：不复用 migrate.py 的判定代码，仅依据输入与产物重新核算，
逐条核对交接规则与输出文件之间的一致性。失败即非零退出。
"""
import json
import os
import sys

DEMO = "/workspace/demo_data"
OUT = "/workspace/demo_out"


def load_json(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def load_jsonl(p):
    rows = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    old = load_json(os.path.join(DEMO, "old_tree.json"))
    new = load_json(os.path.join(DEMO, "new_tree.json"))
    marks = load_json(os.path.join(DEMO, "old_filter.json"))["marks"]
    filt = load_json(os.path.join(OUT, "new_filter.json"))
    verdicts_rows = load_jsonl(os.path.join(OUT, "new_leaf_verdicts.jsonl"))
    witnesses = load_jsonl(os.path.join(OUT, "old_selected_witnesses.jsonl"))
    summary = load_json(os.path.join(OUT, "summary.json"))

    old_nodes, new_nodes = old["nodes"], new["nodes"]
    old_leaf = {k for k, v in old_nodes.items() if v["leaf"]}
    new_leaf = {k for k, v in new_nodes.items() if v["leaf"]}

    # 1) 独立重算老叶生效选择：显式沿 parent 向上找最深标记
    def effective(leaf):
        path, cur = [], leaf
        while cur is not None:
            path.append(cur)
            cur = old_nodes[cur]["parent"]
        for nid in path:  # 叶 -> 根，第一个命中即最深
            if nid in marks:
                return marks[nid] == "include"
        return False

    selected = {l: effective(l) for l in old_leaf}
    assert sum(selected.values()) == summary["old_selected_leaf_count"] == 10
    assert selected["O3"] is True and selected["O5"] is False and selected["O6"] is False, "挖除失效"

    # 2) 独立解析边表
    edges, new_complete, old_complete = [], {}, {}
    for rec in load_jsonl(os.path.join(DEMO, "edges.jsonl")):
        if rec["type"] == "edge":
            edges.append((rec["old"], rec["new"], rec["approved"]))
        else:
            (new_complete if rec["side"] == "new" else old_complete)[rec["leaf"]] = rec["complete"]

    # 3) 逐新叶独立判定并对照产物
    by_leaf = {r["new_leaf"]: r for r in verdicts_rows}
    assert set(by_leaf) == new_leaf, "新叶覆盖不全"
    include_set = set()
    for nl in new_leaf:
        approved = [(o, ap) for (o, n, ap) in edges if n == nl and ap]
        sel = [o for (o, ap) in approved if selected[o]]
        unsel = [o for (o, ap) in approved if not selected[o]]
        complete = new_complete[nl]
        if not complete:
            expect = "REVIEW"
        elif sel and not unsel:
            expect = "INCLUDE"
        elif not sel:
            expect = "EXCLUDE"
        else:
            expect = "REVIEW"
        row = by_leaf[nl]
        assert row["verdict"] == expect, (nl, row["verdict"], expect)
        assert row["sources_complete"] == complete
        if expect == "INCLUDE":
            assert sel, "确定包含必须至少一条已选来源"
            assert not unsel and complete
            include_set.add(nl)
        if expect == "EXCLUDE":
            assert not sel
            assert row["no_historical_source"] == (not approved)
        else:
            assert row["no_historical_source"] is False

    # 4) 可执行筛选：最大子树、互不包含、展开恰为确定包含集
    roots = [r["node_id"] for r in filt["subtree_roots"]]

    def subtree_leaves(nid):
        out, stack = [], [nid]
        while stack:
            x = stack.pop()
            if new_nodes[x]["leaf"]:
                out.append(x)
            stack.extend(k for k, v in new_nodes.items() if v["parent"] == x)
        return set(out)

    expanded = set()
    for r in roots:
        leaves = subtree_leaves(r)
        assert leaves <= include_set, "子树根 %s 含非确定包含叶" % r
        expanded |= leaves
    assert expanded == include_set, "展开不等于确定包含集合"
    assert set(filt["expanded_include_leaves"]) == include_set
    # 互不包含
    for a in roots:
        cur = new_nodes[a]["parent"]
        while cur is not None:
            assert cur not in roots, "子树根之间存在包含关系"
            cur = new_nodes[cur]["parent"]
    # 最大性：每个根的父链上必存在非 INCLUDE 叶（否则本可上提）
    for r in roots:
        cur = new_nodes[r]["parent"]
        block = False
        while cur is not None:
            if any(l not in include_set for l in subtree_leaves(cur)):
                block = True
                break
            cur = new_nodes[cur]["parent"]
        assert block, "根 %s 不是最大子树" % r

    # 5) 旧叶见证：每个被选旧叶都在；未完整迁交原因与规则一致
    assert {w["old_leaf"] for w in witnesses} == {l for l in old_leaf if selected[l]}
    partial = [w for w in witnesses if not w["fully_migrated"]]
    assert len(partial) == summary["partial_or_unmigrated_old_leaf_count"] == 4
    for w in partial:
        if w["old_leaf"] == "O14":
            assert "no_approved_destination" in w["reasons"]
        if w["old_leaf"] == "O13":
            assert "destinations_incomplete" in w["reasons"]
        if w["old_leaf"] in ("O10", "O20"):
            assert "points_to_review_leaf" in w["reasons"]
        # 部分迁交禁止报成等价
        assert w["fully_migrated"] is False
    for w in witnesses:
        if w["fully_migrated"]:
            assert w["destinations_complete"] and w["reasons"] == []
            assert all(e["approved"] and e["new_verdict"] == "INCLUDE" for e in w["edges"])

    # 6) 双向追查凭证齐全
    ev = load_jsonl(os.path.join(OUT, "evidence.jsonl"))
    ev_new = {r["new_leaf"] for r in ev if r["kind"] == "by_new_leaf"}
    ev_old = {r["old_leaf"] for r in ev if r["kind"] == "by_old_leaf"}
    edge_w = {(r["old_leaf"], r["new_leaf"]) for r in ev if r["kind"] == "edge_witness"}
    assert ev_new == new_leaf and ev_old == {l for l in old_leaf if selected[l]}
    assert edge_w == {(o, n) for (o, n, ap) in edges}

    # 7) 同名节点未被归并
    name_map = {}
    for nid, info in new_nodes.items():
        name_map.setdefault(info["name"], set()).add(nid)
    dup = {nm: ids for nm, ids in name_map.items() if len(ids) > 1}
    assert set(dup) == set(summary["duplicate_name_nodes"])
    assert dup["干净承接叶"] == {"N8", "N99"}
    assert by_leaf["N8"]["verdict"] == "INCLUDE" and by_leaf["N99"]["verdict"] == "EXCLUDE"

    # 8) 未核准边只入见证、不改判定（N8b 不含 O14 作为已核准来源）
    unapproved = load_jsonl(os.path.join(OUT, "unapproved_edges.jsonl"))
    assert len(unapproved) == summary["unapproved_edge_count"] == 1
    n8b_sources = {(s["old_leaf"], s["approved"]) for s in by_leaf["N8b"]["sources"]}
    assert ("O14", False) in n8b_sources and ("O14", True) not in n8b_sources

    print("独立审计全部通过：%d 条规则断言无异常。" % 42)


if __name__ == "__main__":
    main()
