#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""非法输入拒绝测试：每种坏数据必须以退出码 2 失败并给出中文原因。"""
import json
import os
import subprocess
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="migrate_bad_")
TOOL = "/workspace/migrate_tool/migrate.py"

GOOD_TREE = {"root": "R", "nodes": {
    "R": {"parent": None, "leaf": False, "name": "根"},
    "A": {"parent": "R", "leaf": True, "name": "叶A"},
}}
GOOD_FILTER = {"marks": {"R": "include"}}


def write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def edges_path(lines):
    p = os.path.join(TMP, "edges.jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln, ensure_ascii=False) + "\n")
    return p


def case(name, tree=GOOD_TREE, filt=GOOD_FILTER, edge_lines=None,
         new_tree=None, expect_kw=None):
    op = os.path.join(TMP, name + "_old.json")
    np_ = os.path.join(TMP, name + "_new.json")
    fp = os.path.join(TMP, name + "_filter.json")
    write(op, tree)
    write(np_, new_tree or GOOD_TREE)
    write(fp, filt)
    if edge_lines is None:
        edge_lines = [
            {"type": "completeness", "side": "new", "leaf": "A", "complete": True},
            {"type": "completeness", "side": "old", "leaf": "A", "complete": True},
        ]
    ep = edges_path(edge_lines)
    out = os.path.join(TMP, name + "_out")
    proc = subprocess.run([sys.executable, "-I", TOOL, "--old-tree", op,
                           "--new-tree", np_, "--old-filter", fp,
                           "--edges", ep, "--outdir", out],
                          capture_output=True, text=True)
    assert proc.returncode == 2, "%s: 应被拒绝却成功了" % name
    if expect_kw:
        assert expect_kw in proc.stderr, "%s: 报错未含 %r，实际: %s" % (name, expect_kw, proc.stderr)
    print("  拒绝 OK: %-28s %s" % (name, expect_kw or ""))


def main():
    # 1. 边引用不存在的旧叶
    case("edge_bad_old",
         edge_lines=[{"type": "edge", "old": "GHOST", "new": "A", "approved": True}],
         expect_kw="不是old树叶子")
    # 2. 边引用非叶节点
    case("edge_nonleaf",
         edge_lines=[{"type": "edge", "old": "R", "new": "A", "approved": True}],
         expect_kw="不是old树叶子")
    # 3. 完整性确认了不存在的叶
    case("completeness_ghost",
         edge_lines=[{"type": "completeness", "side": "new", "leaf": "X", "complete": True}],
         expect_kw="不存在或不是")
    # 4. 筛选标记了不存在的节点
    case("filter_ghost", filt={"marks": {"NOPE": "include"}}, expect_kw="不存在")
    # 5. 重复边
    case("dup_edge",
         edge_lines=[{"type": "edge", "old": "A", "new": "A", "approved": True},
                     {"type": "edge", "old": "A", "new": "A", "approved": False}],
         expect_kw="重复边")
    # 6. 树成环（内部节点 C/B 互相指；挂一个叶使非叶校验先通过）
    cyclic = {"root": "R", "nodes": {
        "R": {"parent": None, "leaf": False, "name": "根"},
        "C": {"parent": "B", "leaf": False, "name": "环上"},
        "B": {"parent": "C", "leaf": False, "name": "环下"},
        "Z": {"parent": "C", "leaf": True, "name": "环内叶"}}}
    case("cycle", tree=cyclic, expect_kw="树存在环")
    # 7. 非根内部节点声明却无子
    bad_leaf = {"root": "R", "nodes": {
        "R": {"parent": None, "leaf": False, "name": "根"},
        "S": {"parent": "R", "leaf": False, "name": "空内部"}}}
    case("empty_internal", tree=bad_leaf, edge_lines=[], expect_kw="非叶节点")
    # 8. 叶却挂了子节点
    bad_parent = {"root": "R", "nodes": {
        "R": {"parent": None, "leaf": True, "name": "根"},
        "Q": {"parent": "R", "leaf": True, "name": "子"}}}
    case("root_marked_leaf", tree=bad_parent, edge_lines=[],
         expect_kw="声明为叶却有子节点")
    # 9. 非法标记值
    case("bad_mark", filt={"marks": {"A": "maybe"}}, expect_kw="非法")
    # 10. 非法 JSONL 行
    write(os.path.join(TMP, "broken_old.json"), GOOD_TREE)
    write(os.path.join(TMP, "broken_new.json"), GOOD_TREE)
    write(os.path.join(TMP, "broken_filter.json"), GOOD_FILTER)
    p = os.path.join(TMP, "broken.jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write('{"type": "edge", "old": "A",\n')
    out = os.path.join(TMP, "broken_out")
    proc = subprocess.run([sys.executable, "-I", TOOL,
                           "--old-tree", os.path.join(TMP, "broken_old.json"),
                           "--new-tree", os.path.join(TMP, "broken_new.json"),
                           "--old-filter", os.path.join(TMP, "broken_filter.json"),
                           "--edges", p, "--outdir", out],
                          capture_output=True, text=True)
    assert proc.returncode == 2 and "不是合法 JSON" in proc.stderr
    print("  拒绝 OK: broken_jsonl                 不是合法 JSON")
    print("全部非法输入用例均被正确拒绝。")


if __name__ == "__main__":
    main()
