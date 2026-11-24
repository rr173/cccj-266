#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对照片发布审计（migrate.py audit）的独立校验：

  1) 演示数据三种场景（闭合关系 / 单侧缺确认未闭合块 / 未核准边）；
  2) 独立第二套实现（字符串 id 朴素并查集）重算，与产物逐块对照；
  3) JSONL 行序无关：同一批记录打乱重跑，块标识/成员/原因/计数完全一致；
  4) 边界：全确认可发布、孤立未确认叶自成未闭合单叶块；
  5) 非法输入拒绝（退出码 2，错误语义与迁交流程一致）；
  6) 规模冒烟：大块（千边单块）+ 深链，验证流式与无递归。
"""
import json
import os
import subprocess
import sys
import tempfile

TOOL = "/workspace/migrate_tool/migrate.py"
DEMO = "/workspace/audit_demo_data"
DEMO_OUT = "/workspace/audit_demo_out"
TMP = tempfile.mkdtemp(prefix="audit_test_")


def run_audit(old, new, edges, out):
    return subprocess.run([sys.executable, "-I", TOOL, "audit",
                           "--old-tree", old, "--new-tree", new,
                           "--edges", edges, "--outdir", out],
                          capture_output=True, text=True)


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


def load_tree_leaves(path):
    if path.endswith(".jsonl"):
        return {r["id"] for r in load_jsonl(path) if r["leaf"]}
    doc = load_json(path)
    return {k for k, v in doc["nodes"].items() if v["leaf"]}


# ---------------- 独立第二套实现 ----------------

def independent_model(old_path, new_path, edges_path):
    """朴素重算：字符串元组并查集。返回块成员集、未闭合块缺口、核准/待处理边。"""
    old_leaves = load_tree_leaves(old_path)
    new_leaves = load_tree_leaves(new_path)
    parent = {}

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for leaf in old_leaves:
        parent[("old", leaf)] = ("old", leaf)
    for leaf in new_leaves:
        parent[("new", leaf)] = ("new", leaf)

    comp, approved, pending = {}, set(), set()
    for rec in load_jsonl(edges_path):
        if rec.get("type", "edge") == "completeness":
            comp[(rec["side"], rec["leaf"])] = rec["complete"]
        else:
            pair = (rec["old"], rec["new"])
            if rec["approved"]:
                approved.add(pair)
                union(("old", rec["old"]), ("new", rec["new"]))
            else:
                pending.add(pair)

    blocks = {}
    for leaf in old_leaves:
        blocks.setdefault(find(("old", leaf)), set()).add(("old", leaf))
    for leaf in new_leaves:
        blocks.setdefault(find(("new", leaf)), set()).add(("new", leaf))

    unclosed = {}
    for members in blocks.values():
        gaps = {(side, leaf) for (side, leaf) in members
                if comp.get((side, leaf)) is not True}
        if gaps:
            unclosed[frozenset(members)] = gaps
    return {"blocks": [frozenset(m) for m in blocks.values()],
            "unclosed": unclosed, "approved": approved, "pending": pending,
            "comp": comp}


def check_against_model(old_path, new_path, edges_path, outdir, tag):
    model = independent_model(old_path, new_path, edges_path)
    summary = load_json(os.path.join(outdir, "publish_audit_summary.json"))
    issues = load_jsonl(os.path.join(outdir, "publish_block_issues.jsonl"))
    pending = load_jsonl(os.path.join(outdir, "publish_pending_edges.jsonl"))

    assert summary["block_count"] == len(model["blocks"]), tag
    assert summary["unclosed_block_count"] == len(model["unclosed"]), tag
    assert summary["closed_block_count"] == len(model["blocks"]) - len(model["unclosed"]), tag
    assert summary["pending_edge_count"] == len(model["pending"]), tag
    assert summary["approved_edge_count"] == len(model["approved"]), tag
    assert summary["gap_count"] == sum(len(g) for g in model["unclosed"].values()), tag
    assert summary["publishable"] == (not model["unclosed"] and not model["pending"]), tag

    # 逐未闭合块对照：成员、缺口（含一侧原因分类）、核准边
    assert len(issues) == len(model["unclosed"]), tag
    seen_blocks = set()
    for rec in issues:
        members = (frozenset(("old", d["node_id"]) for d in rec["old_leaves"]) |
                   frozenset(("new", d["node_id"]) for d in rec["new_leaves"]))
        assert members in model["unclosed"], (tag, rec["block_id"])
        seen_blocks.add(members)
        expect_gaps = model["unclosed"][members]
        got_gaps = {(g["side"], g["node_id"]) for g in rec["gaps"]}
        assert got_gaps == expect_gaps, (tag, rec["block_id"])
        for g in rec["gaps"]:
            key = (g["side"], g["node_id"])
            if key not in model["comp"]:
                assert g["problem"] == "missing_confirmation", (tag, g)
            else:
                assert model["comp"][key] is False
                assert g["problem"] == "explicitly_incomplete", (tag, g)
        got_edges = {(e["old"], e["new"]) for e in rec["approved_edges"]}
        expect_edges = {(o, n) for (o, n) in model["approved"]
                        if ("old", o) in members}
        assert got_edges == expect_edges, (tag, rec["block_id"])
        assert rec["gap_count"] == len(expect_gaps), tag
        assert rec["block_id"].startswith("BLK-"), tag
    assert seen_blocks == set(model["unclosed"]), tag

    assert {(p["old"], p["new"]) for p in pending} == model["pending"], tag
    assert all(p["problem"] == "unapproved_edge" for p in pending), tag
    return summary, issues, pending


# ---------------- 1) 演示数据 ----------------

def test_demo():
    proc = run_audit(os.path.join(DEMO, "old_tree.json"),
                     os.path.join(DEMO, "new_tree.json"),
                     os.path.join(DEMO, "edges.jsonl"), DEMO_OUT)
    assert proc.returncode == 0, proc.stderr
    summary, issues, pending = check_against_model(
        os.path.join(DEMO, "old_tree.json"), os.path.join(DEMO, "new_tree.json"),
        os.path.join(DEMO, "edges.jsonl"), DEMO_OUT, "demo")
    assert summary["publishable"] is False
    assert (summary["block_count"], summary["closed_block_count"],
            summary["unclosed_block_count"]) == (7, 5, 2)
    assert summary["pending_edge_count"] == 1
    # 三种必备场景逐项点名
    by_gap = {g["node_id"]: rec for rec in issues for g in rec["gaps"]}
    assert by_gap["NB"]["gaps"][0]["problem"] == "missing_confirmation"   # 单侧未确认
    assert by_gap["OC"]["gaps"][0]["problem"] == "explicitly_incomplete"  # 明确未列全
    assert pending[0]["old"] == "OE" and pending[0]["new"] == "NE"        # 未核准边
    closed_members = set()
    # 闭合块不出现在问题清单；BLK-0001..BLK-0007 标识稳定
    assert {rec["block_id"] for rec in issues} <= {"BLK-%04d" % i for i in range(1, 8)}
    print("  OK: 演示数据（闭合关系/单侧缺确认未闭合块/未核准边）")


# ---------------- 2) 既有 demo_data 交叉对照 ----------------

def test_existing_demo_data():
    out = os.path.join(TMP, "existing_out")
    proc = run_audit("/workspace/demo_data/old_tree.json",
                     "/workspace/demo_data/new_tree.json",
                     "/workspace/demo_data/edges.jsonl", out)
    assert proc.returncode == 0, proc.stderr
    summary, issues, pending = check_against_model(
        "/workspace/demo_data/old_tree.json", "/workspace/demo_data/new_tree.json",
        "/workspace/demo_data/edges.jsonl", out, "existing")
    gap_nodes = {g["node_id"] for rec in issues for g in rec["gaps"]}
    assert gap_nodes == {"N12", "O13"}, gap_nodes          # 两片 complete=false 叶
    assert {(p["old"], p["new"]) for p in pending} == {("O14", "N8b")}
    assert summary["publishable"] is False
    print("  OK: 既有 demo_data 交叉对照（N12/O13 未闭合，O14->N8b 待处理）")


# ---------------- 3) JSONL 行序无关 ----------------

def test_row_order_independence():
    src = os.path.join(DEMO, "edges.jsonl")
    with open(src, encoding="utf-8") as fh:
        rows = [ln for ln in fh.read().splitlines() if ln.strip()]
    variants = {
        "rev": rows[::-1],
        "interleave": rows[::2] + rows[1::2][::-1],
        "rot": rows[5:] + rows[:5],
    }
    base_issues = open(os.path.join(DEMO_OUT, "publish_block_issues.jsonl"), "rb").read()
    base_pending = open(os.path.join(DEMO_OUT, "publish_pending_edges.jsonl"), "rb").read()
    base_summary = load_json(os.path.join(DEMO_OUT, "publish_audit_summary.json"))
    base_summary.pop("generated_at_utc")
    for tag, perm in variants.items():
        ep = os.path.join(TMP, "perm_%s.jsonl" % tag)
        with open(ep, "w", encoding="utf-8") as fh:
            fh.write("\n".join(perm) + "\n")
        out = os.path.join(TMP, "perm_%s_out" % tag)
        proc = run_audit(os.path.join(DEMO, "old_tree.json"),
                         os.path.join(DEMO, "new_tree.json"), ep, out)
        assert proc.returncode == 0, proc.stderr
        assert open(os.path.join(out, "publish_block_issues.jsonl"), "rb").read() == base_issues, tag
        assert open(os.path.join(out, "publish_pending_edges.jsonl"), "rb").read() == base_pending, tag
        summ = load_json(os.path.join(out, "publish_audit_summary.json"))
        summ.pop("generated_at_utc")
        assert summ == base_summary, tag
    print("  OK: JSONL 行序无关（逆序/交错/轮转三种重排产物逐字节一致）")


# ---------------- 4) 边界：可发布 & 孤立未确认叶 ----------------

def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def tiny_trees(extra_old=(), extra_new=()):
    old = {"root": "R", "nodes": {
        "R": {"parent": None, "leaf": False, "name": "根"},
        "A": {"parent": "R", "leaf": True, "name": "叶A"}}}
    new = {"root": "S", "nodes": {
        "S": {"parent": None, "leaf": False, "name": "根"},
        "B": {"parent": "S", "leaf": True, "name": "叶B"}}}
    for nid in extra_old:
        old["nodes"][nid] = {"parent": "R", "leaf": True, "name": nid}
    for nid in extra_new:
        new["nodes"][nid] = {"parent": "S", "leaf": True, "name": nid}
    op, np_ = os.path.join(TMP, "t_old.json"), os.path.join(TMP, "t_new.json")
    write_json(op, old)
    write_json(np_, new)
    return op, np_


def write_edges(name, records):
    p = os.path.join(TMP, name)
    with open(p, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return p


def test_publishable_and_isolated():
    op, np_ = tiny_trees(extra_old=("X",), extra_new=("Y",))
    # A<->B 闭合；X 无核准边但明确列全（确认无对应）；Y 同样
    ep = write_edges("ok.jsonl", [
        {"type": "edge", "old": "A", "new": "B", "approved": True},
        {"type": "completeness", "side": "old", "leaf": "A", "complete": True},
        {"type": "completeness", "side": "new", "leaf": "B", "complete": True},
        {"type": "completeness", "side": "old", "leaf": "X", "complete": True},
        {"type": "completeness", "side": "new", "leaf": "Y", "complete": True},
    ])
    out = os.path.join(TMP, "ok_out")
    proc = run_audit(op, np_, ep, out)
    assert proc.returncode == 0, proc.stderr
    summary = load_json(os.path.join(out, "publish_audit_summary.json"))
    assert summary["publishable"] is True and summary["conclusion"] == "publishable"
    assert summary["block_count"] == 3 and summary["closed_block_count"] == 3
    assert load_jsonl(os.path.join(out, "publish_block_issues.jsonl")) == []
    assert load_jsonl(os.path.join(out, "publish_pending_edges.jsonl")) == []

    # 孤立但未确认的旧叶 -> 未闭合单叶块（缺失确认），整体不可发布
    ep2 = write_edges("iso.jsonl", [
        {"type": "edge", "old": "A", "new": "B", "approved": True},
        {"type": "completeness", "side": "old", "leaf": "A", "complete": True},
        {"type": "completeness", "side": "new", "leaf": "B", "complete": True},
        {"type": "completeness", "side": "new", "leaf": "Y", "complete": True},
        # X 无任何记录
    ])
    out2 = os.path.join(TMP, "iso_out")
    proc = run_audit(op, np_, ep2, out2)
    assert proc.returncode == 0, proc.stderr
    summary2 = load_json(os.path.join(out2, "publish_audit_summary.json"))
    assert summary2["publishable"] is False
    assert summary2["unclosed_block_count"] == 1
    issues = load_jsonl(os.path.join(out2, "publish_block_issues.jsonl"))
    assert len(issues) == 1 and issues[0]["approved_edges"] == []
    assert issues[0]["gaps"][0]["problem"] == "missing_confirmation"
    assert issues[0]["gaps"][0]["node_id"] == "X"
    print("  OK: 可发布结论 + 孤立未确认叶自成未闭合单叶块")


# ---------------- 5) 非法输入拒绝 ----------------

def test_error_rejections():
    op, np_ = tiny_trees()

    def case(name, edge_lines, expect_kw):
        ep = write_edges(name + ".jsonl", edge_lines)
        proc = run_audit(op, np_, ep, os.path.join(TMP, name + "_out"))
        assert proc.returncode == 2, "%s: 应被拒绝却成功" % name
        assert expect_kw in proc.stderr, "%s: 报错未含 %r，实际 %s" % (name, expect_kw, proc.stderr)

    case("e_ghost", [{"type": "edge", "old": "GHOST", "new": "B", "approved": True}],
         "不是old树叶子")
    case("e_nonleaf", [{"type": "edge", "old": "R", "new": "B", "approved": True}],
         "不是old树叶子")
    case("e_comp_ghost", [{"type": "completeness", "side": "new", "leaf": "X", "complete": True}],
         "不存在或不是")
    case("e_comp_nonleaf", [{"type": "completeness", "side": "new", "leaf": "S", "complete": True}],
         "不存在或不是")
    case("e_dup", [{"type": "edge", "old": "A", "new": "B", "approved": True},
                   {"type": "edge", "old": "A", "new": "B", "approved": True}],
         "重复边")
    case("e_dup_mixed", [{"type": "edge", "old": "A", "new": "B", "approved": True},
                         {"type": "edge", "old": "A", "new": "B", "approved": False}],
         "重复边")
    case("e_bad_comp", [{"type": "completeness", "side": "new", "leaf": "B", "complete": "yes"}],
         "必须为布尔值")
    case("e_bad_side", [{"type": "completeness", "side": "middle", "leaf": "B", "complete": True}],
         "side 非法")
    case("e_unknown", [{"type": "mystery", "old": "A", "new": "B"}], "未知边表记录类型")
    case("e_bad_edge", [{"type": "edge", "old": "A", "new": "B"}], "字段不全")
    p = os.path.join(TMP, "broken.jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write('{"type": "edge", "old": "A",\n')
    proc = run_audit(op, np_, p, os.path.join(TMP, "broken_out"))
    assert proc.returncode == 2 and "不是合法 JSON" in proc.stderr
    print("  OK: 非法输入拒绝（10 种坏数据均以退出码 2 拒绝）")


# ---------------- 6) 规模冒烟：千边单块 + 深链 ----------------

def test_scale():
    n_pair, n_big = 5000, 1000
    oldp = os.path.join(TMP, "s_old.jsonl")
    newp = os.path.join(TMP, "s_new.jsonl")
    with open(oldp, "w", encoding="utf-8") as fh:
        fh.write('{"id":"OR","parent":null,"leaf":false,"name":"r"}\n')
        for i in range(n_pair + n_big):
            fh.write(json.dumps({"id": "O%d" % i, "parent": "OR",
                                 "leaf": True, "name": "o"}) + "\n")
    with open(newp, "w", encoding="utf-8") as fh:
        fh.write('{"id":"NR","parent":null,"leaf":false,"name":"r"}\n')
        for i in range(n_pair):
            fh.write(json.dumps({"id": "N%d" % i, "parent": "NR",
                                 "leaf": True, "name": "n"}) + "\n")
        fh.write('{"id":"NBIG","parent":"NR","leaf":true,"name":"big"}\n')
    ep = os.path.join(TMP, "s_edges.jsonl")
    with open(ep, "w", encoding="utf-8") as fh:
        for i in range(n_pair):
            fh.write(json.dumps({"type": "edge", "old": "O%d" % i,
                                 "new": "N%d" % i, "approved": True}) + "\n")
        for j in range(n_big):   # 千边汇入同一新叶 -> 单一大关系块
            fh.write(json.dumps({"type": "edge", "old": "O%d" % (n_pair + j),
                                 "new": "NBIG", "approved": True}) + "\n")
        for i in range(10):      # 少量待处理边
            fh.write(json.dumps({"type": "edge", "old": "O%d" % i,
                                 "new": "NBIG", "approved": False}) + "\n")
        for i in range(n_pair + n_big):
            if i != n_pair:      # 大块里留一片未确认旧叶 -> 大块不闭合
                fh.write(json.dumps({"type": "completeness", "side": "old",
                                     "leaf": "O%d" % i, "complete": True}) + "\n")
        for i in range(n_pair):
            fh.write(json.dumps({"type": "completeness", "side": "new",
                                 "leaf": "N%d" % i, "complete": True}) + "\n")
        fh.write(json.dumps({"type": "completeness", "side": "new",
                             "leaf": "NBIG", "complete": True}) + "\n")
    out = os.path.join(TMP, "s_out")
    proc = run_audit(oldp, newp, ep, out)
    assert proc.returncode == 0, proc.stderr
    summary, issues, pending = check_against_model(oldp, newp, ep, out, "scale")
    assert summary["block_count"] == n_pair + 1
    assert summary["unclosed_block_count"] == 1
    assert len(issues) == 1 and len(issues[0]["approved_edges"]) == n_big
    assert issues[0]["gaps"][0]["node_id"] == "O%d" % n_pair
    assert summary["pending_edge_count"] == 10
    assert summary["publishable"] is False

    # 深链 3000 层：无递归，且全确认 -> 可发布
    depth = 3000
    oldp2 = os.path.join(TMP, "d_old.jsonl")
    newp2 = os.path.join(TMP, "d_new.jsonl")
    with open(oldp2, "w", encoding="utf-8") as fh:
        fh.write('{"id":"D0","parent":null,"leaf":false,"name":"r"}\n')
        for i in range(1, depth):
            fh.write(json.dumps({"id": "D%d" % i, "parent": "D%d" % (i - 1),
                                 "leaf": False, "name": "d"}) + "\n")
        fh.write(json.dumps({"id": "DL", "parent": "D%d" % (depth - 1),
                             "leaf": True, "name": "l"}) + "\n")
    with open(newp2, "w", encoding="utf-8") as fh:
        fh.write('{"id":"E0","parent":null,"leaf":false,"name":"r"}\n')
        for i in range(1, depth):
            fh.write(json.dumps({"id": "E%d" % i, "parent": "E%d" % (i - 1),
                                 "leaf": False, "name": "d"}) + "\n")
        fh.write(json.dumps({"id": "EL", "parent": "E%d" % (depth - 1),
                             "leaf": True, "name": "l"}) + "\n")
    ep2 = os.path.join(TMP, "d_edges.jsonl")
    with open(ep2, "w", encoding="utf-8") as fh:
        fh.write('{"type":"edge","old":"DL","new":"EL","approved":true}\n')
        fh.write('{"type":"completeness","side":"old","leaf":"DL","complete":true}\n')
        fh.write('{"type":"completeness","side":"new","leaf":"EL","complete":true}\n')
    out2 = os.path.join(TMP, "d_out")
    proc = run_audit(oldp2, newp2, ep2, out2)
    assert proc.returncode == 0, proc.stderr
    s2 = load_json(os.path.join(out2, "publish_audit_summary.json"))
    assert s2["publishable"] is True and s2["block_count"] == 1
    print("  OK: 规模冒烟（%d 对叶 + %d 边单块 + %d 层深链）" % (n_pair, n_big, depth))


def main():
    test_demo()
    test_existing_demo_data()
    test_row_order_independence()
    test_publishable_and_isolated()
    test_error_rejections()
    test_scale()
    print("对照片发布审计全部测试通过。")


if __name__ == "__main__":
    main()
