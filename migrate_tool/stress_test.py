#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
规模压力测试（合成数据，JSONL 流式）：
  A. 10 万层深链 + 每层一片侧叶：验证无递归、栈帧不复制祖先路径、最深标记生效。
  B. 单父 5 万叶宽树：验证最大子树整选与展开不变量。
边表以 JSONL 逐行写出，工具端只过一遍。
"""
import json
import os
import resource
import subprocess
import sys
import time

BASE = "/workspace/stress_data"
OUT = "/workspace/stress_out"
os.makedirs(BASE, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

DEPTH = 100_000
WIDE = 50_000
EXCLUDE_AT = 60_000


def write_tree(path, rows_iter):
    with open(path, "w", encoding="utf-8") as fh:
        for rec in rows_iter:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def build_deep():
    def old_rows():
        yield {"id": "D0", "parent": None, "leaf": False, "name": "root"}
        for i in range(1, DEPTH):
            yield {"id": "D%d" % i, "parent": "D%d" % (i - 1), "leaf": False, "name": "d"}
        for i in range(DEPTH):
            yield {"id": "L%d" % i, "parent": "D%d" % i, "leaf": True, "name": "side"}
        yield {"id": "T", "parent": "D%d" % (DEPTH - 1), "leaf": True, "name": "tail"}

    def new_rows():
        yield {"id": "E0", "parent": None, "leaf": False, "name": "root"}
        for i in range(1, DEPTH):
            yield {"id": "E%d" % i, "parent": "E%d" % (i - 1), "leaf": False, "name": "d"}
        for i in range(DEPTH):
            yield {"id": "M%d" % i, "parent": "E%d" % i, "leaf": True, "name": "side"}
        yield {"id": "U", "parent": "E%d" % (DEPTH - 1), "leaf": True, "name": "tail"}

    write_tree(os.path.join(BASE, "deep_old.jsonl"), old_rows())
    write_tree(os.path.join(BASE, "deep_new.jsonl"), new_rows())

    with open(os.path.join(BASE, "deep_filter.json"), "w", encoding="utf-8") as fh:
        json.dump({"marks": {"D0": "include", "D%d" % EXCLUDE_AT: "exclude"}}, fh)

    with open(os.path.join(BASE, "deep_edges.jsonl"), "w", encoding="utf-8") as fh:
        for i in range(DEPTH):
            fh.write(json.dumps({"type": "edge", "old": "L%d" % i,
                                 "new": "M%d" % i, "approved": True}) + "\n")
        fh.write(json.dumps({"type": "edge", "old": "T", "new": "U",
                             "approved": True}) + "\n")
        for i in range(DEPTH):
            fh.write(json.dumps({"type": "completeness", "side": "old",
                                 "leaf": "L%d" % i, "complete": True}) + "\n")
            fh.write(json.dumps({"type": "completeness", "side": "new",
                                 "leaf": "M%d" % i, "complete": True}) + "\n")
        for lid in ("T", "U"):
            side = "old" if lid == "T" else "new"
            fh.write(json.dumps({"type": "completeness", "side": side,
                                 "leaf": lid, "complete": True}) + "\n")


def build_wide():
    def old_rows():
        yield {"id": "W", "parent": None, "leaf": False, "name": "wide"}
        for i in range(WIDE):
            yield {"id": "W%d" % i, "parent": "W", "leaf": True, "name": "w"}

    def new_rows():
        yield {"id": "V", "parent": None, "leaf": False, "name": "wide"}
        for i in range(WIDE):
            yield {"id": "V%d" % i, "parent": "V", "leaf": True, "name": "w"}

    write_tree(os.path.join(BASE, "wide_old.jsonl"), old_rows())
    write_tree(os.path.join(BASE, "wide_new.jsonl"), new_rows())
    with open(os.path.join(BASE, "wide_filter.json"), "w", encoding="utf-8") as fh:
        json.dump({"marks": {"W": "include"}}, fh)
    with open(os.path.join(BASE, "wide_edges.jsonl"), "w", encoding="utf-8") as fh:
        for i in range(WIDE):
            fh.write(json.dumps({"type": "edge", "old": "W%d" % i,
                                 "new": "V%d" % i, "approved": True}) + "\n")
            fh.write(json.dumps({"type": "completeness", "side": "old",
                                 "leaf": "W%d" % i, "complete": True}) + "\n")
            fh.write(json.dumps({"type": "completeness", "side": "new",
                                 "leaf": "V%d" % i, "complete": True}) + "\n")


def run(tag, old_t, new_t, filt, edges):
    outdir = os.path.join(OUT, tag)
    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    proc = subprocess.run([sys.executable, "-I", "/workspace/migrate_tool/migrate.py",
                           "--old-tree", old_t, "--new-tree", new_t,
                           "--old-filter", filt, "--edges", edges,
                           "--outdir", outdir],
                          capture_output=True, text=True)
    dt = time.time() - t0
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        raise SystemExit("用例 %s 失败" % tag)
    with open(os.path.join(outdir, "new_filter.json"), encoding="utf-8") as fh:
        result = json.load(fh)
    with open(os.path.join(outdir, "summary.json"), encoding="utf-8") as fh:
        summ = json.load(fh)
    rss_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    print("[%s] 用时 %.1fs，子进程峰值 RSS 约 %.0f MB" % (tag, dt, rss_mb))
    return result, summ


def main():
    t0 = time.time()
    build_deep()
    build_wide()
    print("数据生成用时 %.1fs" % (time.time() - t0))

    deep_res, deep_sum = run(
        "deep",
        os.path.join(BASE, "deep_old.jsonl"),
        os.path.join(BASE, "deep_new.jsonl"),
        os.path.join(BASE, "deep_filter.json"),
        os.path.join(BASE, "deep_edges.jsonl"))
    # 侧叶 L0..L(EXCLUDE_AT-1) 选中；挖除点及其后、尾叶不选
    expect_include = {"M%d" % i for i in range(EXCLUDE_AT)}
    assert set(deep_res["expanded_include_leaves"]) == expect_include
    assert deep_res["include_leaf_count"] == EXCLUDE_AT
    assert deep_sum["new_exclude_leaf_count"] == DEPTH - EXCLUDE_AT + 1
    # 链上每节点子树均为混合 -> 每片包含叶自成最大子树根，且互不包含
    assert len(deep_res["subtree_roots"]) == EXCLUDE_AT
    assert all(r["include_leaf_count"] == 1 for r in deep_res["subtree_roots"])

    wide_res, wide_sum = run(
        "wide",
        os.path.join(BASE, "wide_old.jsonl"),
        os.path.join(BASE, "wide_new.jsonl"),
        os.path.join(BASE, "wide_filter.json"),
        os.path.join(BASE, "wide_edges.jsonl"))
    assert [r["node_id"] for r in wide_res["subtree_roots"]] == ["V"]
    assert wide_res["include_leaf_count"] == WIDE
    assert wide_res["subtree_roots"][0]["include_leaf_count"] == WIDE

    print("压力测试通过：深链 %d 层（最深标记挖除生效）、宽树 %d 叶整选，均无递归错误。"
          % (DEPTH, WIDE))


if __name__ == "__main__":
    main()
