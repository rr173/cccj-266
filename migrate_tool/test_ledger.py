#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
迁移账本自检：原子拒绝、幂等修订、核准撤回、分流失效、净变化与索引。

用法：
    python3 -I test_ledger.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(TOOL_DIR, "ledger.py")
MIGRATE = os.path.join(TOOL_DIR, "migrate.py")
TMP = tempfile.mkdtemp(prefix="ledger_test_")


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_cli(*args, expect=0):
    proc = subprocess.run([sys.executable, "-I", LEDGER, *args],
                          capture_output=True, text=True)
    if expect is not None:
        assert proc.returncode == expect, (
            "命令应退出 %s，实际 %s\nargs=%s\nstdout=%s\nstderr=%s"
            % (expect, proc.returncode, args, proc.stdout, proc.stderr))
    return proc


def ledger_dir(name):
    return os.path.join(TMP, name + "_ledger")


def make_dataset(name="good", duplicate=False, corrupt=False, bad_old_ref=False):
    d = os.path.join(TMP, name + "_data")
    os.makedirs(d, exist_ok=True)
    old_tree = {"root": "R", "nodes": {
        "R": {"parent": None, "leaf": False, "name": "旧根"},
        "O_A": {"parent": "R", "leaf": True, "name": "唯一核准"},
        "O_M": {"parent": "R", "leaf": True, "name": "多去向"},
        "O_W": {"parent": "R", "leaf": True, "name": "撤回核准"},
        "O_X": {"parent": "R", "leaf": True, "name": "未列全"},
    }}
    new_tree = {"root": "N", "nodes": {
        "N": {"parent": None, "leaf": False, "name": "新根"},
        "N_A": {"parent": "N", "leaf": True, "name": "A"},
        "N_1": {"parent": "N", "leaf": True, "name": "1"},
        "N_2": {"parent": "N", "leaf": True, "name": "2"},
        "N_3": {"parent": "N", "leaf": True, "name": "3"},
        "N_W": {"parent": "N", "leaf": True, "name": "W"},
        "N_X": {"parent": "N", "leaf": True, "name": "X"},
    }}
    write_json(os.path.join(d, "old.json"), old_tree)
    write_json(os.path.join(d, "new.json"), new_tree)
    mapping = [
        {"type": "edge", "old": "O_A", "new": "N_A", "approved": True},
        {"type": "edge", "old": "O_M", "new": "N_1", "approved": True},
        {"type": "edge", "old": "O_M", "new": "N_2", "approved": True},
        {"type": "edge", "old": "O_W", "new": "N_W", "approved": True},
        {"type": "edge", "old": "O_X", "new": "N_X", "approved": True},
        {"type": "completeness", "side": "old", "leaf": "O_A", "complete": True},
        {"type": "completeness", "side": "old", "leaf": "O_M", "complete": True},
        {"type": "completeness", "side": "old", "leaf": "O_W", "complete": True},
        {"type": "completeness", "side": "old", "leaf": "O_X", "complete": False},
    ]
    write_jsonl(os.path.join(d, "map.jsonl"), mapping)
    items = [
        {"item_id": "A1", "old_leaf": "O_A", "keep": "字段一", "n": 1},
        {"item_id": "A2", "old_leaf": "O_A", "keep": "字段二", "n": 2},
        {"item_id": "M1", "old_leaf": "O_M", "color": "red"},
        {"item_id": "M2", "old_leaf": "O_M", "color": "blue"},
        {"item_id": "W1", "old_leaf": "O_W"},
        {"item_id": "X1", "old_leaf": "O_X"},
    ]
    if duplicate:
        items.append(dict(items[0]))
    if bad_old_ref:
        items.append({"item_id": "BAD", "old_leaf": "O_GHOST"})
    items_path = os.path.join(d, "items.jsonl")
    write_jsonl(items_path, items)
    if corrupt:
        with open(items_path, "a", encoding="utf-8") as fh:
            fh.write('{"item_id":"BROKEN", "old_leaf":\n')
    return d, items_path


def init_good(name="good"):
    d, items = make_dataset(name)
    ld = ledger_dir(name)
    proc = run_cli("init", "--ledger", ld, "--items", items,
                   "--old-tree", os.path.join(d, "old.json"),
                   "--new-tree", os.path.join(d, "new.json"),
                   "--mapping", os.path.join(d, "map.jsonl"))
    summary = json.loads(proc.stdout)
    assert summary["item_count"] == 6
    assert summary["migrated_count"] == 3
    assert summary["pending_count"] == 3
    return d, ld


def write_rev(d, name, doc):
    p = os.path.join(d, name)
    write_json(p, doc)
    return p


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_atomic_rejections():
    d, items = make_dataset("bad_dup", duplicate=True)
    ld = ledger_dir("bad_dup")
    proc = run_cli("init", "--ledger", ld, "--items", items,
                   "--old-tree", os.path.join(d, "old.json"),
                   "--new-tree", os.path.join(d, "new.json"),
                   "--mapping", os.path.join(d, "map.jsonl"), expect=2)
    assert "重复" in proc.stderr
    assert not os.path.exists(os.path.join(ld, "ledger.sqlite3"))

    d, items = make_dataset("bad_json", corrupt=True)
    ld = ledger_dir("bad_json")
    proc = run_cli("init", "--ledger", ld, "--items", items,
                   "--old-tree", os.path.join(d, "old.json"),
                   "--new-tree", os.path.join(d, "new.json"),
                   "--mapping", os.path.join(d, "map.jsonl"), expect=2)
    assert "不是合法 JSON" in proc.stderr
    assert not os.path.exists(os.path.join(ld, "ledger.sqlite3"))

    d, items = make_dataset("bad_ref", bad_old_ref=True)
    ld = ledger_dir("bad_ref")
    proc = run_cli("init", "--ledger", ld, "--items", items,
                   "--old-tree", os.path.join(d, "old.json"),
                   "--new-tree", os.path.join(d, "new.json"),
                   "--mapping", os.path.join(d, "map.jsonl"), expect=2)
    assert "不存在的旧树叶子" in proc.stderr
    assert not os.path.exists(os.path.join(ld, "ledger.sqlite3"))
    print("  OK 重复 ID / 损坏 JSON / 坏引用均整次拒绝且无正式库")


def test_decisions_revisions_and_delta():
    d, ld = init_good("main")

    # 原字段保留 + 当前结果导出。
    out = os.path.join(d, "migrated.jsonl")
    run_cli("query", "--ledger", ld, "--status", "migrated", "--out", out)
    rows = read_jsonl(out)
    assert {r["item_id"] for r in rows} == {"A1", "A2", "W1"}
    a1 = next(r for r in rows if r["item_id"] == "A1")
    assert a1["keep"] == "字段一" and a1["n"] == 1
    assert a1["_migration_ledger"]["new_leaf"] == "N_A"

    # M1 分流到 N_1：pending -> migrated。
    rev1 = write_rev(d, "r1.json", {
        "revision_id": "R1", "kind": "manual_routing", "item_id": "M1",
        "action": "replace", "new_leaf": "N_1"})
    diff1 = os.path.join(d, "r1.diff.jsonl")
    proc = run_cli("revise", "--ledger", ld, "--request", rev1,
                   "--diff-out", diff1)
    receipt = json.loads(proc.stdout)
    assert receipt["changed_item_count"] == 1
    assert receipt["change_kind_counts"] == {"pending_to_migrated": 1}
    assert len(read_jsonl(diff1)) == 1

    # 同号同内容重送：replay，且不新增变化行。
    proc = run_cli("revise", "--ledger", ld, "--request", rev1)
    replay = json.loads(proc.stdout)
    assert replay["idempotent_replay"] is True
    assert replay["operation"] == "no_op_same_id_and_content"
    proc = run_cli("diff", "--ledger", ld, "--revision-id", "R1")
    assert len([ln for ln in proc.stdout.splitlines() if ln.strip()]) == 1

    # 同号不同内容：拒绝。
    rev1_conflict = write_rev(d, "r1_conflict.json", {
        "revision_id": "R1", "kind": "manual_routing", "item_id": "M1",
        "action": "replace", "new_leaf": "N_2"})
    proc = run_cli("revise", "--ledger", ld, "--request", rev1_conflict, expect=2)
    assert "不同内容" in proc.stderr

    # 坏 item 引用：拒绝。
    bad = write_rev(d, "bad.json", {
        "revision_id": "R_BAD", "kind": "manual_routing",
        "item_id": "GHOST", "action": "replace", "new_leaf": "N_1"})
    proc = run_cli("revise", "--ledger", ld, "--request", bad, expect=2)
    assert "不存在的 item_id" in proc.stderr

    # 核准撤回：W1 自动迁入 -> pending。
    rev_w = write_rev(d, "rw.json", {
        "revision_id": "RW", "kind": "old_leaf_destinations",
        "old_leaf": "O_W", "complete": True,
        "destinations": [{"new_leaf": "N_W", "approved": False}]})
    diff_w = os.path.join(d, "rw.diff.jsonl")
    receipt = json.loads(run_cli(
        "revise", "--ledger", ld, "--request", rev_w,
        "--diff-out", diff_w).stdout)
    assert receipt["affected_item_count"] == 1
    assert receipt["changed_item_count"] == 1
    row = read_jsonl(diff_w)[0]
    assert row["change_kind"] == "migrated_to_pending"
    assert row["after"]["reasons"] == ["unapproved_relation"]

    # 关系替换移除 M1 的旧目标 N_1；M2 仍只是缺分流，签名不变，不进净变化。
    rev_m = write_rev(d, "rm.json", {
        "revision_id": "RM", "kind": "old_leaf_destinations",
        "old_leaf": "O_M", "complete": True,
        "destinations": [
            {"new_leaf": "N_2", "approved": True},
            {"new_leaf": "N_3", "approved": True}]})
    diff_m = os.path.join(d, "rm.diff.jsonl")
    receipt = json.loads(run_cli(
        "revise", "--ledger", ld, "--request", rev_m,
        "--diff-out", diff_m).stdout)
    assert receipt["affected_item_count"] == 2
    assert receipt["changed_item_count"] == 1
    assert receipt["unchanged_item_count"] == 1
    changes = read_jsonl(diff_m)
    assert [c["item_id"] for c in changes] == ["M1"]
    assert changes[0]["after"]["reasons"][0] == "manual_route_target_absent"
    assert "invalid_manual_routing" in changes[0]["after"]["reasons"]

    # 修复分流到新目标 N_3。
    rev_fix = write_rev(d, "fix.json", {
        "revision_id": "RF", "kind": "manual_routing", "item_id": "M1",
        "action": "replace", "new_leaf": "N_3"})
    receipt = json.loads(run_cli("revise", "--ledger", ld, "--request", rev_fix).stdout)
    assert receipt["change_kind_counts"] == {"pending_to_migrated": 1}

    # 撤销分流：多去向下重新 pending。
    rev_del = write_rev(d, "del.json", {
        "revision_id": "RD", "kind": "manual_routing", "item_id": "M1",
        "action": "revoke"})
    receipt = json.loads(run_cli("revise", "--ledger", ld, "--request", rev_del).stdout)
    assert receipt["change_kind_counts"] == {"migrated_to_pending": 1}
    show = json.loads(run_cli("show", "--ledger", ld, "--item-id", "M1").stdout)
    assert show["current_output"]["_migration_ledger"]["status"] == "pending"
    assert show["manual_routing_current"]["active"] is False
    actions = [h["action"] for h in show["manual_routing_history"]]
    assert actions == ["replace", "replace", "revoke"]

    summary = json.loads(run_cli("summary", "--ledger", ld).stdout)
    # A1/A2 migrated；M1 revoked pending, M2 pending, W1 approval withdrawn,
    # X1 incomplete => migrated 2 / pending 4.
    assert summary["migrated_count"] == 2
    assert summary["pending_count"] == 4
    print("  OK 判定、幂等/冲突、撤回、失效、撤销与净变化均正确")


def test_query_plan_and_reopen():
    _d, ld = init_good("plan")
    uri = "file:%s?mode=ro" % os.path.abspath(os.path.join(ld, "ledger.sqlite3"))
    conn = sqlite3.connect(uri, uri=True)
    plans = [r[-1] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM item WHERE old_leaf='O_M' ORDER BY seq")]
    assert any("item_old_leaf_seq_idx" in p for p in plans), plans
    conn.close()

    # 新进程重新打开只读摘要，等同于“重开”。
    proc = run_cli("summary", "--ledger", ld)
    assert json.loads(proc.stdout)["item_count"] == 6

    # 通过现有 migrate.py ledger 子命令也必须可用。
    proc = subprocess.run(
        [sys.executable, "-I", MIGRATE, "ledger", "summary", "--ledger", ld],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert '"item_count": 6' in proc.stdout
    print("  OK 查询走旧叶索引；重开及 migrate.py ledger 入口正常")


def test_integer_item_id():
    d = os.path.join(TMP, "intid_data")
    os.makedirs(d, exist_ok=True)
    tree = {"root": "R", "nodes": {
        "R": {"parent": None, "leaf": False},
        "O": {"parent": "R", "leaf": True},
        "N": {"parent": "R", "leaf": True},
    }}
    write_json(os.path.join(d, "old.json"), tree)
    write_json(os.path.join(d, "new.json"), tree)
    write_jsonl(os.path.join(d, "map.jsonl"), [
        {"type": "edge", "old": "O", "new": "N", "approved": True},
        {"type": "completeness", "side": "old", "leaf": "O", "complete": True}])
    items = os.path.join(d, "items.jsonl")
    write_jsonl(items, [{"item_id": 123, "old_leaf": "O"}])
    ld = os.path.join(TMP, "intid_ledger")
    run_cli("init", "--ledger", ld, "--items", items,
            "--old-tree", os.path.join(d, "old.json"),
            "--new-tree", os.path.join(d, "new.json"),
            "--mapping", os.path.join(d, "map.jsonl"))
    proc = run_cli("show", "--ledger", ld, "--item-id", "123")
    assert json.loads(proc.stdout)["item_id"] == 123
    print("  OK 整数 item_id 可查询且不与字符串 ID 混淆")


def main():
    print("使用临时目录: %s" % TMP)
    test_atomic_rejections()
    test_decisions_revisions_and_delta()
    test_query_plan_and_reopen()
    test_integer_item_id()
    shutil.rmtree(TMP, ignore_errors=True)
    print("test_ledger.py 全部通过。")


if __name__ == "__main__":
    main()
