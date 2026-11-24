#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
迁移账本规模/增量冒烟：

生成 N 件货品、L 个旧叶/新叶（默认 100000 件、200 叶），初始化后只修订 O0。
修订命令不接收货品文件；SQLite 查询计划必须走 (old_leaf, seq) 索引。
本脚本不声称在默认配置下复现“远大于内存”，而是用可调大的 N 验证流式摄入和
按旧叶增量重算；例如：
    python3 -I stress_ledger.py --items 5000000 --old-leaves 1000
"""
import argparse
import json
import os
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.py")


def write_dataset(workdir, item_count, leaf_count):
    data = os.path.join(workdir, "data")
    ledger = os.path.join(workdir, "ledger")
    os.makedirs(data, exist_ok=True)

    old_nodes = {"R": {"parent": None, "leaf": False, "name": "old-root"}}
    new_nodes = {"N": {"parent": None, "leaf": False, "name": "new-root"}}
    for i in range(leaf_count):
        old_nodes["O%d" % i] = {"parent": "R", "leaf": True, "name": "O%d" % i}
        new_nodes["N%d" % i] = {"parent": "N", "leaf": True, "name": "N%d" % i}
    with open(os.path.join(data, "old.json"), "w", encoding="utf-8") as fh:
        json.dump({"root": "R", "nodes": old_nodes}, fh)
    with open(os.path.join(data, "new.json"), "w", encoding="utf-8") as fh:
        json.dump({"root": "N", "nodes": new_nodes}, fh)

    with open(os.path.join(data, "map.jsonl"), "w", encoding="utf-8") as fh:
        for i in range(leaf_count):
            fh.write(json.dumps({
                "type": "edge", "old": "O%d" % i,
                "new": "N%d" % i, "approved": True}) + "\n")
            fh.write(json.dumps({
                "type": "completeness", "side": "old",
                "leaf": "O%d" % i, "complete": True}) + "\n")

    items = os.path.join(data, "items.jsonl")
    with open(items, "w", encoding="utf-8") as fh:
        for i in range(item_count):
            leaf = i % leaf_count
            fh.write(json.dumps({
                "item_id": "ITEM-%012d" % i,
                "old_leaf": "O%d" % leaf,
                "payload": "x" * 128,
                "n": i,
            }, ensure_ascii=False) + "\n")

    rev = os.path.join(data, "revoke_o0.json")
    with open(rev, "w", encoding="utf-8") as fh:
        json.dump({
            "revision_id": "REV-SCALE-1",
            "kind": "old_leaf_destinations",
            "old_leaf": "O0",
            "complete": True,
            "destinations": [{"new_leaf": "N0", "approved": False}],
        }, fh)
    return data, ledger, items, rev


def run(cmd):
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0
    if proc.returncode != 0:
        raise AssertionError("命令失败(%s):\n%s\n%s"
                             % (proc.returncode, proc.stdout, proc.stderr))
    return proc.stdout, elapsed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--items", type=int, default=100000)
    ap.add_argument("--old-leaves", type=int, default=200)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    workdir = args.workdir or tempfile.mkdtemp(prefix="ledger_stress_")
    os.makedirs(workdir, exist_ok=True)
    data, ledger, items, rev = write_dataset(
        workdir, args.items, args.old_leaves)

    out, init_sec = run([
        sys.executable, "-I", TOOL, "init", "--ledger", ledger,
        "--items", items,
        "--old-tree", os.path.join(data, "old.json"),
        "--new-tree", os.path.join(data, "new.json"),
        "--mapping", os.path.join(data, "map.jsonl")])
    init_summary = json.loads(out)

    out, rev_sec = run([
        sys.executable, "-I", TOOL, "revise", "--ledger", ledger,
        "--request", rev])
    receipt = json.loads(out)

    expected_affected = sum(
        1 for i in range(args.items) if i % args.old_leaves == 0)
    assert init_summary["item_count"] == args.items
    assert receipt["affected_item_count"] == expected_affected
    assert receipt["changed_item_count"] == expected_affected

    conn = sqlite3.connect(os.path.join(ledger, "ledger.sqlite3"))
    plan = [r[-1] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM item WHERE old_leaf='O0' ORDER BY seq")]
    assert any("item_old_leaf_seq_idx" in p for p in plan), plan
    conn.close()

    rss_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    print(json.dumps({
        "workdir": workdir,
        "items": args.items,
        "old_leaves": args.old_leaves,
        "init_seconds": round(init_sec, 3),
        "revision_o0_seconds": round(rev_sec, 3),
        "expected_affected_o0": expected_affected,
        "query_plan_uses_index": True,
        "children_peak_rss_mb_reported_by_os": round(rss_kb / 1024, 1),
        "keep_workdir": args.keep,
    }, ensure_ascii=False, indent=2))
    if not args.keep and args.workdir is None:
        # 数据库可能数百 MB；默认清理临时目录。
        subprocess.run(["rm", "-rf", workdir], check=False)


if __name__ == "__main__":
    main()
