#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可持续查询的货品迁移账本（纯标准库 + SQLite）。

设计要点
--------
1. 初始化时流式读取完整货品 JSONL，批量写入 SQLite；重复 item_id、坏引用或
   损坏 JSON 会使整个初始化事务回滚，临时库不会变成正式账本。
2. 旧/新树只在初始化时由 migrate.TreeStore 校验并抽取叶子；日常修订直接查库，
   不再重读树文件、完整货品文件或整张对照关系。
3. 关系按旧叶建索引，货品按 old_leaf + 顺序建索引。旧叶声明替换只处理该旧叶的
   关系和货品；人工分流只处理一个 item_id。
4. 每个修订号只对应一份规范化请求：同号同内容返回首次结果（replay=true，不再
   产生变化行），同号不同内容拒绝。已提交修订在一个 SQLite 事务内完成，重开
   只会看到完整旧版或完整新版。
5. 当前结果始终由“当前关系 + 当前人工分流 + 货品原件”重新解释；旧分流不会因
   为曾经有效而永久有效。

CLI 示例：
  python3 -I ledger.py init --ledger DIR --items items.jsonl \
      --old-tree old.json --new-tree new.json --mapping edges.jsonl
  python3 -I ledger.py revise --ledger DIR --request rev.json --diff-out d.jsonl
  python3 -I ledger.py export --ledger DIR --outdir OUT
  python3 -I ledger.py diff --ledger DIR --revision-id REV-1 --out rev1.jsonl
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# 保证以 `python3 -I /path/ledger.py` 运行时能导入同目录的 migrate.py。
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)

from migrate import TreeStore, iter_jsonl  # noqa: E402

SCHEMA_VERSION = 1
INIT_REVISION = "LEDGER-INIT"

REASON_CN = {
    "old_destinations_incomplete": "旧叶去向未列全",
    "no_destination": "旧叶没有任何去向",
    "unapproved_relation": "旧叶去向声明含未核准关系",
    "manual_routing_required": "多个去向，缺少指向当前核准去向的有效逐件分流",
    "manual_routing_revoked": "逐件人工分流已撤销",
    "manual_route_target_absent": "人工分流目标已不在当前完整去向声明中",
    "manual_route_target_unapproved": "人工分流目标关系当前未核准",
    "invalid_manual_routing": "人工分流当前失效",
}

# 主原因按此优先级选择；数组顺序也是稳定输出顺序。
REASON_PRIORITY = [
    "old_destinations_incomplete",
    "no_destination",
    "unapproved_relation",
    "manual_route_target_unapproved",
    "manual_route_target_absent",
    "manual_routing_revoked",
    "manual_routing_required",
    "invalid_manual_routing",
]


class LedgerError(Exception):
    """可直接展示为“输入错误并以退出码 2 拒绝”的错误。"""


# ----------------------------- 通用辅助 -----------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def canonical_json(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def request_fingerprint(req):
    raw = canonical_json(req)
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


def parse_json_scalar(text):
    """命令行 ID：能按 JSON 解析就按 JSON，否则视为字符串。"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def scalar_id_key(value, what):
    """
    字符串/整数 ID 都支持，并在键中带类型前缀，避免 "1" 和 1 被误合并。
    布尔值是 int 子类，但不作为合法 ID。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise LedgerError("%s 必须是非空字符串或整数 ID: %r" % (what, value))
    if isinstance(value, str):
        if not value:
            raise LedgerError("%s 不能为空字符串" % what)
        return "s:" + value
    return "i:" + str(value)


def item_key(value):
    return scalar_id_key(value, "item_id")


def revision_key(value):
    return scalar_id_key(value, "revision_id")


def json_id_text(value):
    return json.dumps(value, ensure_ascii=False)


@contextlib.contextmanager
def atomic_text_writer(path):
    """同目录临时文件 + fsync + rename，保证单个输出文件是完整旧版或新版。"""
    path = os.path.abspath(path)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp", dir=parent)
    try:
        os.fchmod(fd, 0o644)
    except OSError:
        pass
    fh = os.fdopen(fd, "w", encoding="utf-8")
    ok = False
    try:
        yield fh
        fh.flush()
        os.fsync(fh.fileno())
        fh.close()
        os.replace(tmp, path)
        ok = True
    finally:
        if not ok:
            try:
                fh.close()
            except Exception:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_json_atomic(path, obj):
    with atomic_text_writer(path) as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# ----------------------------- 数据库结构 -----------------------------

SCHEMA_SQL = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE old_leaf (
    leaf_id TEXT PRIMARY KEY,
    ord     INTEGER NOT NULL,
    name    TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE new_leaf (
    leaf_id TEXT PRIMARY KEY,
    ord     INTEGER NOT NULL,
    name    TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE old_completeness (
    leaf_id     TEXT PRIMARY KEY REFERENCES old_leaf(leaf_id),
    complete    INTEGER NOT NULL,
    revision_id TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE new_completeness (
    leaf_id     TEXT PRIMARY KEY REFERENCES new_leaf(leaf_id),
    complete    INTEGER NOT NULL,
    revision_id TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE relation (
    old_leaf    TEXT NOT NULL REFERENCES old_leaf(leaf_id),
    new_leaf    TEXT NOT NULL REFERENCES new_leaf(leaf_id),
    approved    INTEGER NOT NULL,
    revision_id TEXT NOT NULL,
    PRIMARY KEY (old_leaf, new_leaf)
) WITHOUT ROWID;

CREATE INDEX relation_new_leaf_idx ON relation(new_leaf);

CREATE TABLE item (
    id_key                 TEXT PRIMARY KEY,
    item_id_json           TEXT NOT NULL,
    seq                    INTEGER NOT NULL,
    old_leaf               TEXT NOT NULL REFERENCES old_leaf(leaf_id),
    raw_json               TEXT NOT NULL,
    content_sha256         TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN ('migrated','pending')),
    new_leaf               TEXT REFERENCES new_leaf(leaf_id),
    primary_reason         TEXT,
    decision_json          TEXT NOT NULL,
    decision_revision_id   TEXT NOT NULL,
    decided_at             TEXT NOT NULL
);

CREATE INDEX item_old_leaf_seq_idx ON item(old_leaf, seq);
CREATE INDEX item_status_idx ON item(status);
CREATE INDEX item_new_leaf_idx ON item(new_leaf);
CREATE INDEX item_primary_reason_idx ON item(primary_reason);

CREATE TABLE item_reason (
    item_id_key TEXT NOT NULL REFERENCES item(id_key),
    reason      TEXT NOT NULL,
    PRIMARY KEY (item_id_key, reason)
) WITHOUT ROWID;

CREATE INDEX item_reason_reason_idx ON item_reason(reason);

CREATE TABLE manual_route_current (
    item_id_key TEXT PRIMARY KEY REFERENCES item(id_key),
    active      INTEGER NOT NULL,
    new_leaf    TEXT REFERENCES new_leaf(leaf_id),
    revision_id TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE manual_route_history (
    revision_id TEXT NOT NULL,
    item_id_key TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('replace','revoke')),
    new_leaf    TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (revision_id, item_id_key)
);

CREATE INDEX manual_route_history_item_idx
    ON manual_route_history(item_id_key);

CREATE TABLE revision (
    revision_id           TEXT PRIMARY KEY,
    revision_key          TEXT NOT NULL UNIQUE,
    canonical_request     TEXT NOT NULL,
    request_sha256        TEXT NOT NULL,
    kind                  TEXT NOT NULL,
    target_type           TEXT NOT NULL,
    target_id             TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    idempotent_replay     INTEGER NOT NULL DEFAULT 0,
    effect_summary_json   TEXT NOT NULL
);

CREATE TABLE revision_change (
    revision_id   TEXT NOT NULL REFERENCES revision(revision_id),
    item_id_key   TEXT NOT NULL,
    item_id_json  TEXT NOT NULL,
    item_seq      INTEGER NOT NULL,
    old_leaf      TEXT NOT NULL,
    change_kind   TEXT NOT NULL,
    before_json   TEXT NOT NULL,
    after_json    TEXT NOT NULL,
    PRIMARY KEY (revision_id, item_id_key)
);

CREATE INDEX revision_change_item_idx ON revision_change(item_id_key);
CREATE INDEX revision_change_rev_seq_idx
    ON revision_change(revision_id, item_seq);
"""


def db_path_for(ledger_dir):
    return os.path.join(os.path.abspath(ledger_dir), "ledger.sqlite3")


def connect_ledger(ledger_dir, readonly=False):
    path = db_path_for(ledger_dir)
    if not os.path.exists(path):
        raise LedgerError("账本不存在或尚未初始化: %s" % path)
    if readonly:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def require_schema(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None or int(row["value"]) != SCHEMA_VERSION:
        raise LedgerError("账本 schema 版本无法识别；请使用与账本匹配的 ledger.py")


@contextlib.contextmanager
def read_snapshot(conn):
    """让一次导出/摘要中的多条查询看到同一个已提交版本。"""
    conn.execute("BEGIN")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


# ----------------------------- 判定核心 -----------------------------

def get_declaration(conn, old_leaf):
    comp = conn.execute(
        "SELECT complete FROM old_completeness WHERE leaf_id=?", (old_leaf,)
    ).fetchone()
    rows = conn.execute(
        """
        SELECT r.new_leaf AS new_leaf, r.approved AS approved,
               nc.complete AS new_side_complete
          FROM relation r
          LEFT JOIN new_completeness nc ON nc.leaf_id = r.new_leaf
         WHERE r.old_leaf=?
         ORDER BY r.new_leaf
        """, (old_leaf,)
    ).fetchall()
    destinations = [{
        "new_leaf": r["new_leaf"],
        "approved": bool(r["approved"]),
        "new_side_complete": (
            None if r["new_side_complete"] is None
            else bool(r["new_side_complete"])),
    } for r in rows]
    return {
        "old_leaf": old_leaf,
        "complete": None if comp is None else bool(comp["complete"]),
        "destinations": destinations,
        "by_new_leaf": {d["new_leaf"]: d["approved"] for d in destinations},
    }


def classify(declaration, route, revision_id, at):
    """
    返回紧凑判定。route 为 None 或 sqlite Row/dict：
      active, new_leaf, revision_id
    """
    reason_set = set()

    def add_reason(code):
        reason_set.add(code)

    complete = declaration["complete"]
    destinations = declaration["destinations"]
    by_new = declaration["by_new_leaf"]

    if complete is not True:
        add_reason("old_destinations_incomplete")
    if not destinations:
        add_reason("no_destination")
    if any(not d["approved"] for d in destinations):
        add_reason("unapproved_relation")

    route_info = None
    active_valid_route = False
    if route is not None:
        active = bool(route["active"])
        route_info = {
            "active": active,
            "new_leaf": route["new_leaf"],
            "revision_id": route["revision_id"],
            "valid": False,
        }
        if active:
            target = route["new_leaf"]
            if target not in by_new:
                add_reason("manual_route_target_absent")
                add_reason("invalid_manual_routing")
            elif not by_new[target]:
                add_reason("manual_route_target_unapproved")
                add_reason("invalid_manual_routing")
            else:
                route_info["valid"] = True
                active_valid_route = True

    relation_blocked = (
        complete is not True
        or not destinations
        or any(not d["approved"] for d in destinations)
    )

    status = "pending"
    target = None

    if not relation_blocked:
        if len(destinations) == 1:
            sole = destinations[0]["new_leaf"]
            # 唯一去向本可自动迁入；但若存在一条当前失效的“有效状态”人工分流，
            # 按“失效分流保留原件”处理，等待明确替换/撤销，绝不猜。
            invalid_active_route = (
                route is not None and bool(route["active"])
                and not active_valid_route
            )
            if invalid_active_route:
                status = "pending"
            else:
                status = "migrated"
                target = sole
        else:
            if active_valid_route:
                status = "migrated"
                target = route["new_leaf"]
            else:
                if route is not None and not bool(route["active"]):
                    add_reason("manual_routing_revoked")
                elif route is None:
                    add_reason("manual_routing_required")
                # active 但无效时，具体原因已在上面加入。
                if "manual_routing_revoked" in reason_set:
                    add_reason("invalid_manual_routing")
                elif route is None:
                    pass
                else:
                    add_reason("invalid_manual_routing")

    reasons = [c for c in REASON_PRIORITY if c in reason_set]
    # 兜底：未来新增原因码也不能丢失。
    reasons.extend(sorted(c for c in reason_set if c not in REASON_PRIORITY))
    primary = reasons[0] if reasons else None

    decision = {
        "schema": 1,
        "status": status,
        "new_leaf": target,
        "reasons": reasons,
        "primary_reason": primary,
        "revision": revision_id,
        "at": at,
    }
    return decision, route_info


def decision_signature(decision):
    return canonical_json({
        "status": decision["status"],
        "new_leaf": decision.get("new_leaf"),
        "reasons": decision.get("reasons", []),
    }).decode("utf-8")


def change_kind(old, new):
    if old.get("status") != new["status"]:
        if old.get("status") == "pending" and new["status"] == "migrated":
            return "pending_to_migrated"
        if old.get("status") == "migrated" and new["status"] == "pending":
            return "migrated_to_pending"
        return "status_changed"
    if old.get("new_leaf") != new.get("new_leaf"):
        return "destination_changed"
    if old.get("reasons") != new.get("reasons"):
        return "pending_reasons_changed"
    return None


def replace_item_reasons(conn, item_key_value, reasons):
    conn.execute("DELETE FROM item_reason WHERE item_id_key=?", (item_key_value,))
    conn.executemany(
        "INSERT INTO item_reason(item_id_key, reason) VALUES(?,?)",
        [(item_key_value, r) for r in reasons],
    )


def persist_evaluation(conn, row, declaration, revision_id, at):
    """
    重新判定一行。只有交付签名变化才更新货品当前结果并写 revision_change；
    未变化货品不产生变化行，也不重写结果。
    """
    key = row["id_key"]
    route = None
    if "m_active" in row.keys() and row["m_active"] is not None:
        route = {
            "active": row["m_active"],
            "new_leaf": row["m_new_leaf"],
            "revision_id": row["m_rev"],
        }

    new_decision, _route_info = classify(declaration, route, revision_id, at)
    old_decision = json.loads(row["decision_json"])
    kind = change_kind(old_decision, new_decision)

    if kind is None:
        return {"changed": False, "change_kind": None, "decision": old_decision}

    new_json = json.dumps(new_decision, ensure_ascii=False, separators=(",", ":"))
    conn.execute(
        """
        UPDATE item
           SET status=?, new_leaf=?, primary_reason=?, decision_json=?,
               decision_revision_id=?, decided_at=?
         WHERE id_key=?
        """,
        (new_decision["status"], new_decision["new_leaf"],
         new_decision["primary_reason"], new_json, revision_id, at, key),
    )
    replace_item_reasons(conn, key, new_decision["reasons"])
    conn.execute(
        """
        INSERT INTO revision_change(
            revision_id, item_id_key, item_id_json, item_seq, old_leaf,
            change_kind, before_json, after_json)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (revision_id, key, row["item_id_json"], row["seq"], row["old_leaf"],
         kind, row["decision_json"], new_json),
    )
    return {"changed": True, "change_kind": kind, "decision": new_decision}


SELECT_ITEM_WITH_ROUTE = """
    SELECT i.*, m.active AS m_active, m.new_leaf AS m_new_leaf,
           m.revision_id AS m_rev
      FROM item i
      LEFT JOIN manual_route_current m ON m.item_id_key = i.id_key
"""


def reevaluate_old_leaf(conn, old_leaf, revision_id, at):
    declaration = get_declaration(conn, old_leaf)
    rows = conn.execute(
        SELECT_ITEM_WITH_ROUTE + " WHERE i.old_leaf=? ORDER BY i.seq",
        (old_leaf,),
    )
    affected = changed = 0
    kinds = {}
    after_status = {"migrated": 0, "pending": 0}
    for row in rows:
        affected += 1
        result = persist_evaluation(conn, row, declaration, revision_id, at)
        if result["changed"]:
            changed += 1
            kinds[result["change_kind"]] = kinds.get(result["change_kind"], 0) + 1
        # 摘要统计重新从库中取当前状态成本高；这里按新签名统计本次扫描后状态。
        after_status[result["decision"]["status"]] += 1
    return {
        "target_type": "old_leaf",
        "target_id": old_leaf,
        "affected_item_count": affected,
        "changed_item_count": changed,
        "unchanged_item_count": affected - changed,
        "change_kind_counts": kinds,
        "after_status_among_affected": after_status,
    }


def reevaluate_one_item(conn, item_key_value, revision_id, at):
    row = conn.execute(
        SELECT_ITEM_WITH_ROUTE + " WHERE i.id_key=?", (item_key_value,)
    ).fetchone()
    if row is None:
        raise LedgerError("内部错误：刚校验存在的 item 消失: %s" % item_key_value)
    declaration = get_declaration(conn, row["old_leaf"])
    result = persist_evaluation(conn, row, declaration, revision_id, at)
    return {
        "target_type": "item_id",
        "target_id": json.loads(row["item_id_json"]),
        "old_leaf": row["old_leaf"],
        "affected_item_count": 1,
        "changed_item_count": 1 if result["changed"] else 0,
        "unchanged_item_count": 0 if result["changed"] else 1,
        "change_kind_counts": {result["change_kind"]: 1} if result["changed"] else {},
        "after_status_among_affected": {
            result["decision"]["status"]: 1,
            "pending" if result["decision"]["status"] == "migrated" else "migrated": 0,
        },
    }


# ----------------------------- 初始化 -----------------------------

def insert_tree_leaves(conn, old_tree, new_tree, init_rev):
    for side, tree, table in (("old", old_tree, "old_leaf"),
                              ("new", new_tree, "new_leaf")):
        for leaf_ord in tree.leaf_indices_preorder():
            node_idx = tree.node_idx_for_leaf(leaf_ord)
            conn.execute(
                "INSERT INTO %s(leaf_id, ord, name) VALUES(?,?,?)" % table,
                (tree.ids[node_idx], leaf_ord, tree.names[node_idx]),
            )


def load_initial_mapping(conn, mapping_path, old_tree, new_tree):
    edge_count = 0
    old_comp = new_comp = 0
    for rec in iter_jsonl(mapping_path):
        rtype = rec.get("type", "edge")
        if rtype == "completeness":
            side = rec.get("side")
            if side not in ("old", "new"):
                raise LedgerError("completeness.side 必须是 old/new: %r" % rec)
            if not isinstance(rec.get("leaf"), str) or not isinstance(
                    rec.get("complete"), bool):
                raise LedgerError("completeness 记录需要字符串 leaf 和布尔 complete: %r" % rec)
            if side == "old":
                leaf = old_tree.require_leaf(rec["leaf"], "old")
                leaf_id = old_tree.ids[old_tree.node_idx_for_leaf(leaf)]
                table = "old_completeness"
                old_comp += 1
            else:
                leaf = new_tree.require_leaf(rec["leaf"], "new")
                leaf_id = new_tree.ids[new_tree.node_idx_for_leaf(leaf)]
                table = "new_completeness"
                new_comp += 1
            conn.execute(
                f"INSERT INTO {table}(leaf_id, complete, revision_id) "
                "VALUES(?,?,?) ON CONFLICT(leaf_id) DO UPDATE SET "
                "complete=excluded.complete, revision_id=excluded.revision_id",
                (leaf_id, 1 if rec["complete"] else 0, INIT_REVISION),
            )
        elif rtype == "edge":
            old_id = rec.get("old") or rec.get("old_leaf")
            new_id = rec.get("new") or rec.get("new_leaf")
            approved = rec.get("approved")
            if not isinstance(old_id, str) or not isinstance(new_id, str) \
                    or not isinstance(approved, bool):
                raise LedgerError(
                    "edge 记录需要字符串 old/new 和布尔 approved: %r" % rec)
            oi = old_tree.require_leaf(old_id, "old")
            ni = new_tree.require_leaf(new_id, "new")
            old_leaf_id = old_tree.ids[old_tree.node_idx_for_leaf(oi)]
            new_leaf_id = new_tree.ids[new_tree.node_idx_for_leaf(ni)]
            try:
                conn.execute(
                    "INSERT INTO relation(old_leaf,new_leaf,approved,revision_id)"
                    " VALUES(?,?,?,?)",
                    (old_leaf_id, new_leaf_id, 1 if approved else 0, INIT_REVISION),
                )
            except sqlite3.IntegrityError:
                raise LedgerError(
                    "对照片存在重复边: %s -> %s" % (old_id, new_id))
            edge_count += 1
        else:
            raise LedgerError("未知对照片记录类型 %r: %r" % (rtype, rec))
    return {"edge_count": edge_count,
            "old_completeness_records": old_comp,
            "new_completeness_records": new_comp}


def cmd_init(args):
    ledger_dir = os.path.abspath(args.ledger)
    final_db = db_path_for(ledger_dir)
    if os.path.exists(final_db):
        raise LedgerError("账本已存在，拒绝覆盖: %s" % final_db)
    if os.path.exists(ledger_dir) and not os.path.isdir(ledger_dir):
        raise LedgerError("--ledger 必须是目录: %s" % ledger_dir)
    os.makedirs(ledger_dir, exist_ok=True)
    tmp_db = os.path.join(ledger_dir, ".ledger.init.%s.sqlite3" % os.getpid())
    if os.path.exists(tmp_db):
        os.unlink(tmp_db)

    # 树校验失败时 migrate.fail 会抛 SystemExit；下方 BaseException 清理临时库。
    old_tree = TreeStore(args.old_tree)
    new_tree = TreeStore(args.new_tree)

    conn = None
    try:
        conn = sqlite3.connect(tmp_db, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        # 注意：executescript 会先提交当前事务，因此事务必须在 schema
        # 建完后再显式开启；否则逐货插入会退化成每条自动提交/fsync。
        conn.executescript(SCHEMA_SQL)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)",
                     (str(SCHEMA_VERSION),))
        conn.execute("INSERT INTO meta(key,value) VALUES('latest_revision_id',?)",
                     (INIT_REVISION,))
        insert_tree_leaves(conn, old_tree, new_tree, INIT_REVISION)
        mapping_stats = load_initial_mapping(
            conn, args.mapping, old_tree, new_tree)

        item_field = args.item_id_field
        old_field = args.old_leaf_field
        item_count = 0
        migrated = pending = 0
        at = now_iso()
        declaration_cache = {}

        with open(args.items, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise LedgerError(
                        "%s 第 %d 行不是合法 JSON: %s"
                        % (args.items, lineno, exc))
                if not isinstance(obj, dict):
                    raise LedgerError(
                        "%s 第 %d 行必须是 JSON 对象" % (args.items, lineno))
                if item_field not in obj:
                    raise LedgerError(
                        "%s 第 %d 行缺少 item ID 字段 %r"
                        % (args.items, lineno, item_field))
                id_value = obj[item_field]
                key = item_key(id_value)

                if old_field not in obj or not isinstance(obj[old_field], str) \
                        or not obj[old_field]:
                    raise LedgerError(
                        "%s 第 %d 行缺少非空字符串旧叶字段 %r"
                        % (args.items, lineno, old_field))
                old_leaf = obj[old_field]
                exists = conn.execute(
                    "SELECT 1 FROM old_leaf WHERE leaf_id=?", (old_leaf,)
                ).fetchone()
                if not exists:
                    raise LedgerError(
                        "%s 第 %d 行引用了不存在的旧树叶子 %r"
                        % (args.items, lineno, old_leaf))

                digest = hashlib.sha256(canonical_json(obj)).hexdigest()
                declaration = declaration_cache.get(old_leaf)
                if declaration is None:
                    declaration = get_declaration(conn, old_leaf)
                    declaration_cache[old_leaf] = declaration
                decision, _ = classify(declaration, None, INIT_REVISION, at)
                decision_json = json.dumps(
                    decision, ensure_ascii=False, separators=(",", ":"))
                try:
                    conn.execute(
                        """
                        INSERT INTO item(
                            id_key,item_id_json,seq,old_leaf,raw_json,
                            content_sha256,status,new_leaf,primary_reason,
                            decision_json,decision_revision_id,decided_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (key, json_id_text(id_value), item_count, old_leaf,
                         stripped, digest, decision["status"],
                         decision["new_leaf"], decision["primary_reason"],
                         decision_json, INIT_REVISION, at),
                    )
                except sqlite3.IntegrityError:
                    raise LedgerError(
                        "货品 item_id 重复，整次初始化拒绝: %r（%s 第 %d 行）"
                        % (id_value, args.items, lineno))
                replace_item_reasons(conn, key, decision["reasons"])
                item_count += 1
                if decision["status"] == "migrated":
                    migrated += 1
                else:
                    pending += 1

        init_summary = {
            "initialized": True,
            "ledger": ledger_dir,
            "item_count": item_count,
            "migrated_count": migrated,
            "pending_count": pending,
            "old_leaf_count": old_tree.leaf_n,
            "new_leaf_count": new_tree.leaf_n,
            **mapping_stats,
        }
        conn.execute(
            "INSERT INTO revision(revision_id,revision_key,canonical_request,"
            "request_sha256,kind,target_type,target_id,created_at,"
            "idempotent_replay,effect_summary_json) "
            "VALUES(?,?,?,?,?,?,?,?,0,?)",
            (INIT_REVISION, "s:" + INIT_REVISION,
             json.dumps({"revision_id": INIT_REVISION, "kind": "init"},
                        ensure_ascii=False, sort_keys=True),
             hashlib.sha256(b"init").hexdigest(), "init", "ledger", ledger_dir,
             at, json.dumps(init_summary, ensure_ascii=False, sort_keys=True)),
        )
        conn.execute(
            "UPDATE meta SET value=? WHERE key='latest_revision_id'",
            (INIT_REVISION,))
        conn.commit()
        conn.close()
        conn = None
        os.replace(tmp_db, final_db)
        print(json.dumps(init_summary, ensure_ascii=False, indent=2))
        return 0
    except BaseException:
        if conn is not None:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
        for suffix in ("", "-journal", "-wal", "-shm"):
            p = tmp_db + suffix
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass
        raise


# ----------------------------- 修订请求 -----------------------------

def load_request(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise LedgerError("无法读取修订请求 %s: %s" % (path, exc))
    try:
        req = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LedgerError("修订请求 %s 不是合法 JSON: %s" % (path, exc))
    if not isinstance(req, dict):
        raise LedgerError("修订请求必须是 JSON 对象: %s" % path)
    return req


def request_revision_id(req):
    rid = req.get("revision_id", req.get("id", req.get("revision")))
    if isinstance(rid, bool) or not isinstance(rid, (str, int)):
        raise LedgerError("修订请求必须包含非空字符串或整数 revision_id")
    if isinstance(rid, str):
        if not rid:
            raise LedgerError("修订请求 revision_id 不能为空字符串")
        normalized = rid
    else:
        normalized = str(rid)
    if normalized == INIT_REVISION:
        raise LedgerError("修订号 %s 为账本保留号" % INIT_REVISION)
    return normalized


def normalize_kind(req):
    raw = req.get("kind", req.get("op", req.get("type")))
    relation_aliases = {
        "old_leaf_destinations", "destination_declaration",
        "replace_destinations", "replace_old_leaf_destinations",
        "old_leaf_declaration", "relation_revision",
    }
    manual_aliases = {
        "manual_routing", "item_routing", "route_item",
        "replace_item_routing", "manual_route",
    }
    revoke_aliases = {"revoke_item_routing", "revoke_manual_routing"}
    if raw in relation_aliases or (raw is None and "destinations" in req):
        return "old_leaf_destinations"
    if raw in revoke_aliases:
        return "manual_routing:revoke"
    if raw in manual_aliases or (raw is None and "item_id" in req):
        return "manual_routing"
    raise LedgerError(
        "无法识别修订类型 %r；应为 old_leaf_destinations 或 manual_routing" % (raw,))


def validate_relation_request(conn, req):
    old_leaf = req.get("old_leaf", req.get("old_leaf_id"))
    if not isinstance(old_leaf, str) or not old_leaf:
        raise LedgerError("关系修订需要非空字符串 old_leaf")
    if not conn.execute("SELECT 1 FROM old_leaf WHERE leaf_id=?",
                        (old_leaf,)).fetchone():
        raise LedgerError("关系修订引用了不存在的旧树叶子: %s" % old_leaf)

    complete = req.get("complete", req.get("destinations_complete"))
    if not isinstance(complete, bool):
        raise LedgerError("关系修订需要布尔 complete（旧叶去向是否列全）")

    destinations = req.get("destinations")
    if not isinstance(destinations, list):
        raise LedgerError("关系修订需要 destinations 列表（完整替换该旧叶原声明）")

    normalized = []
    seen = set()
    for idx, dest in enumerate(destinations):
        if not isinstance(dest, dict):
            raise LedgerError("destinations[%d] 必须是对象" % idx)
        new_leaf = dest.get("new_leaf", dest.get("new"))
        approved = dest.get("approved")
        if not isinstance(new_leaf, str) or not new_leaf:
            raise LedgerError("destinations[%d] 缺少非空字符串 new_leaf" % idx)
        if not isinstance(approved, bool):
            raise LedgerError(
                "destinations[%d] 需要布尔 approved；不允许猜测核准状态" % idx)
        if new_leaf in seen:
            raise LedgerError("修订声明中新叶 %s 重复" % new_leaf)
        seen.add(new_leaf)
        if not conn.execute("SELECT 1 FROM new_leaf WHERE leaf_id=?",
                            (new_leaf,)).fetchone():
            raise LedgerError("关系修订引用了不存在的新树叶子: %s" % new_leaf)
        normalized.append((new_leaf, 1 if approved else 0))
    return old_leaf, complete, normalized


def validate_manual_request(conn, req, kind):
    if "item_id" not in req:
        raise LedgerError("人工分流修订需要 item_id")
    id_value = req["item_id"]
    key = item_key(id_value)
    row = conn.execute("SELECT old_leaf FROM item WHERE id_key=?", (key,)).fetchone()
    if row is None:
        raise LedgerError("人工分流引用了不存在的 item_id: %r" % (id_value,))

    action = req.get("action")
    if kind == "manual_routing:revoke":
        action = "revoke"
    if action is None:
        if "new_leaf" in req and req["new_leaf"] is not None:
            action = "replace"
        elif req.get("revoke") is True or req.get("new_leaf", None) is None \
                and "new_leaf" in req:
            action = "revoke"
        else:
            raise LedgerError(
                "人工分流修订需指定 action=replace/revoke，或 replace 时给 new_leaf")
    if action not in ("replace", "revoke"):
        raise LedgerError("人工分流 action 只能是 replace/revoke: %r" % action)

    new_leaf = req.get("new_leaf")
    if action == "revoke":
        if new_leaf is not None:
            raise LedgerError("撤销分流时 new_leaf 必须省略或为 null")
        return key, id_value, "revoke", None

    if not isinstance(new_leaf, str) or not new_leaf:
        raise LedgerError("替换分流需要非空字符串 new_leaf")
    if not conn.execute("SELECT 1 FROM new_leaf WHERE leaf_id=?",
                        (new_leaf,)).fetchone():
        raise LedgerError("人工分流引用了不存在的新树叶子: %s" % new_leaf)
    return key, id_value, "replace", new_leaf


def apply_revision(conn, req):
    rid = request_revision_id(req)
    canonical, digest = request_fingerprint(req)
    existing = conn.execute(
        "SELECT canonical_request, effect_summary_json FROM revision "
        "WHERE revision_id=?", (rid,)
    ).fetchone()
    if existing is not None:
        if existing["canonical_request"] == canonical:
            summary = json.loads(existing["effect_summary_json"])
            return {
                "revision_id": rid,
                "idempotent_replay": True,
                "operation": "no_op_same_id_and_content",
                "effect_summary": summary,
            }
        raise LedgerError(
            "修订号 %s 已用于不同内容；同号不同内容必须拒绝，原修订保持不变" % rid)

    kind = normalize_kind(req)
    # 引用校验在事务前进行；只读查询不会改变账本。
    if kind == "old_leaf_destinations":
        old_leaf, complete, destinations = validate_relation_request(conn, req)
        target_type, target_id = "old_leaf", old_leaf
    else:
        item_k, id_value, action, new_leaf = validate_manual_request(
            conn, req, kind)
        kind = "manual_routing"
        # 列字段用于人读列表；完整 JSON 类型保存在 item_id_json/效果摘要中。
        target_type, target_id = "item_id", str(id_value)

    at = now_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 并发情况下在事务内复查幂等键。
        existing = conn.execute(
            "SELECT canonical_request FROM revision WHERE revision_id=?", (rid,)
        ).fetchone()
        if existing is not None:
            if existing["canonical_request"] != canonical:
                raise LedgerError("修订号 %s 已用于不同内容" % rid)
            conn.rollback()
            row = conn.execute(
                "SELECT effect_summary_json FROM revision WHERE revision_id=?",
                (rid,)).fetchone()
            summary = json.loads(row["effect_summary_json"])
            return {"revision_id": rid, "idempotent_replay": True,
                    "operation": "no_op_same_id_and_content",
                    "effect_summary": summary}

        # 先占住修订号主记录：revision_change 对它有外键；若后续失败，
        # 整个事务回滚，不会留下“已占用但无效果”的已提交修订号。
        conn.execute(
            "INSERT INTO revision(revision_id,revision_key,canonical_request,"
            "request_sha256,kind,target_type,target_id,created_at,"
            "idempotent_replay,effect_summary_json) "
            "VALUES(?,?,?,?,?,?,?,?,0,'{}')",
            (rid, revision_key(rid), canonical, digest, kind, target_type,
             target_id, at),
        )

        if target_type == "old_leaf":
            conn.execute("DELETE FROM relation WHERE old_leaf=?", (old_leaf,))
            conn.executemany(
                "INSERT INTO relation(old_leaf,new_leaf,approved,revision_id)"
                " VALUES(?,?,?,?)",
                [(old_leaf, nid, approved, rid)
                 for nid, approved in destinations],
            )
            conn.execute(
                "INSERT INTO old_completeness(leaf_id,complete,revision_id) "
                "VALUES(?,?,?) ON CONFLICT(leaf_id) DO UPDATE SET "
                "complete=excluded.complete, revision_id=excluded.revision_id",
                (old_leaf, 1 if complete else 0, rid),
            )
            effect = reevaluate_old_leaf(conn, old_leaf, rid, at)
        else:
            active = 1 if action == "replace" else 0
            conn.execute(
                """
                INSERT INTO manual_route_current(
                    item_id_key,active,new_leaf,revision_id,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(item_id_key) DO UPDATE SET
                    active=excluded.active,
                    new_leaf=excluded.new_leaf,
                    revision_id=excluded.revision_id,
                    updated_at=excluded.updated_at
                """,
                (item_k, active, new_leaf, rid, at),
            )
            conn.execute(
                "INSERT INTO manual_route_history("
                "revision_id,item_id_key,action,new_leaf,created_at) "
                "VALUES(?,?,?,?,?)",
                (rid, item_k, action, new_leaf, at),
            )
            effect = reevaluate_one_item(conn, item_k, rid, at)

        receipt = {
            "revision_id": rid,
            "idempotent_replay": False,
            "applied": True,
            "kind": kind,
            "target_type": target_type,
            "target_id": target_id,
            "applied_at": at,
            **effect,
        }
        conn.execute(
            "UPDATE revision SET effect_summary_json=? WHERE revision_id=?",
            (json.dumps(receipt, ensure_ascii=False, sort_keys=True), rid),
        )
        conn.execute(
            "UPDATE meta SET value=? WHERE key='latest_revision_id'", (rid,))
        conn.commit()
        return receipt
    except LedgerError:
        conn.rollback()
        raise
    except BaseException:
        conn.rollback()
        raise


# ----------------------------- 查询/导出 -----------------------------

def reason_details(reasons):
    return [{"code": code, "message_cn": REASON_CN.get(code, code)}
            for code in reasons]


def merged_item(row):
    obj = json.loads(row["raw_json"])
    decision = json.loads(row["decision_json"])
    meta = {
        "status": decision["status"],
        "new_leaf": decision.get("new_leaf"),
        "reasons": decision.get("reasons", []),
        "reason_details": reason_details(decision.get("reasons", [])),
        "primary_reason": decision.get("primary_reason"),
        "decision_revision": decision.get("revision"),
        "decided_at": decision.get("at"),
    }
    key = "_migration_ledger"
    if key in obj:
        n = 2
        while "%s_%d" % (key, n) in obj:
            n += 1
        key = "%s_%d" % (key, n)
    obj[key] = meta
    return obj


def query_rows(conn, status="all", reason=None, old_leaf=None,
               item_id=None, limit=None):
    where = []
    params = []
    if status in ("migrated", "pending"):
        where.append("i.status=?")
        params.append(status)
    if old_leaf is not None:
        where.append("i.old_leaf=?")
        params.append(old_leaf)
    if item_id is not None:
        where.append("i.id_key=?")
        params.append(item_key(item_id))
    if reason is not None:
        where.append(
            "EXISTS (SELECT 1 FROM item_reason r "
            "WHERE r.item_id_key=i.id_key AND r.reason=?)")
        params.append(reason)
    sql = "SELECT i.* FROM item i"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.seq"
    if limit is not None:
        if not isinstance(limit, int) or limit < 0:
            raise LedgerError("--limit 必须是非负整数")
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params)


def cmd_query(args):
    conn = connect_ledger(args.ledger, readonly=True)
    require_schema(conn)
    item_id = parse_json_scalar(args.item_id) if args.item_id is not None else None
    cur = query_rows(conn, args.status, args.reason, args.old_leaf,
                     item_id, args.limit)
    count = 0
    if args.out:
        with atomic_text_writer(args.out) as fh:
            for row in cur:
                fh.write(json.dumps(merged_item(row), ensure_ascii=False) + "\n")
                count += 1
        print(json.dumps({"written": args.out, "row_count": count},
                         ensure_ascii=False))
    else:
        for row in cur:
            sys.stdout.write(json.dumps(merged_item(row), ensure_ascii=False) + "\n")
            count += 1
    conn.close()
    return 0


def build_summary(conn):
    require_schema(conn)
    total = conn.execute("SELECT COUNT(*) c FROM item").fetchone()["c"]
    migrated = conn.execute(
        "SELECT COUNT(*) c FROM item WHERE status='migrated'").fetchone()["c"]
    pending = total - migrated
    latest = conn.execute(
        "SELECT value FROM meta WHERE key='latest_revision_id'").fetchone()
    rev_count = conn.execute(
        "SELECT COUNT(*) c FROM revision WHERE revision_id != ?",
        (INIT_REVISION,)).fetchone()["c"]
    by_new = [dict(r) for r in conn.execute(
        "SELECT new_leaf, COUNT(*) AS item_count FROM item "
        "WHERE status='migrated' GROUP BY new_leaf ORDER BY new_leaf")]
    by_primary = [dict(r) for r in conn.execute(
        "SELECT primary_reason AS reason, COUNT(*) AS item_count FROM item "
        "WHERE status='pending' GROUP BY primary_reason "
        "ORDER BY primary_reason")]
    by_reason = [dict(r) for r in conn.execute(
        "SELECT reason, COUNT(*) AS item_count FROM item_reason "
        "GROUP BY reason ORDER BY reason")]
    return {
        "schema_version": SCHEMA_VERSION,
        "item_count": total,
        "migrated_count": migrated,
        "pending_count": pending,
        "migrated_ratio": (migrated / total) if total else 0.0,
        "latest_revision_id": latest["value"] if latest else None,
        "user_revision_count": rev_count,
        "old_leaf_count": conn.execute("SELECT COUNT(*) c FROM old_leaf").fetchone()["c"],
        "new_leaf_count": conn.execute("SELECT COUNT(*) c FROM new_leaf").fetchone()["c"],
        "relation_count": conn.execute("SELECT COUNT(*) c FROM relation").fetchone()["c"],
        "migrated_by_new_leaf": by_new,
        "pending_by_primary_reason": by_primary,
        "pending_by_reason_tag_counts": by_reason,
    }


def cmd_summary(args):
    conn = connect_ledger(args.ledger, readonly=True)
    with read_snapshot(conn):
        summary = build_summary(conn)
    conn.close()
    if args.out:
        write_json_atomic(args.out, summary)
        print(json.dumps({"written": args.out}, ensure_ascii=False))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_export(args):
    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    conn = connect_ledger(args.ledger, readonly=True)
    with read_snapshot(conn):
        summary = build_summary(conn)
        migrated_path = os.path.join(outdir, "migrated_items.jsonl")
        pending_path = os.path.join(outdir, "pending_items.jsonl")
        with atomic_text_writer(migrated_path) as fh:
            for row in query_rows(conn, status="migrated"):
                fh.write(json.dumps(merged_item(row), ensure_ascii=False) + "\n")
        with atomic_text_writer(pending_path) as fh:
            for row in query_rows(conn, status="pending"):
                fh.write(json.dumps(merged_item(row), ensure_ascii=False) + "\n")
        summary["migrated_items_file"] = "migrated_items.jsonl"
        summary["pending_items_file"] = "pending_items.jsonl"
        write_json_atomic(os.path.join(outdir, "summary.json"), summary)
    conn.close()
    print(json.dumps({
        "outdir": outdir,
        "migrated": summary["migrated_count"],
        "pending": summary["pending_count"],
        "migrated_items_file": migrated_path,
        "pending_items_file": pending_path,
        "summary_file": os.path.join(outdir, "summary.json"),
    }, ensure_ascii=False, indent=2))
    return 0


def change_output(conn, row):
    item_row = conn.execute(
        "SELECT raw_json FROM item WHERE id_key=?", (row["item_id_key"],)
    ).fetchone()
    original = json.loads(item_row["raw_json"]) if item_row else None
    before = json.loads(row["before_json"])
    after = json.loads(row["after_json"])
    return {
        "revision_id": row["revision_id"],
        "item_id": json.loads(row["item_id_json"]),
        "old_leaf": row["old_leaf"],
        "change_kind": row["change_kind"],
        "before": {
            "status": before.get("status"),
            "new_leaf": before.get("new_leaf"),
            "reasons": before.get("reasons", []),
            "decision_revision": before.get("revision"),
        },
        "after": {
            "status": after.get("status"),
            "new_leaf": after.get("new_leaf"),
            "reasons": after.get("reasons", []),
            "reason_details": reason_details(after.get("reasons", [])),
            "decision_revision": after.get("revision"),
        },
        "item": original,
    }


def cmd_diff(args):
    conn = connect_ledger(args.ledger, readonly=True)
    require_schema(conn)
    rev = conn.execute(
        "SELECT * FROM revision WHERE revision_id=?", (args.revision_id,)
    ).fetchone()
    if rev is None:
        raise LedgerError("找不到修订号: %s" % args.revision_id)
    rows = conn.execute(
        "SELECT * FROM revision_change WHERE revision_id=? ORDER BY item_seq",
        (args.revision_id,))
    count = 0
    if args.out:
        with atomic_text_writer(args.out) as fh:
            for row in rows:
                fh.write(json.dumps(change_output(conn, row), ensure_ascii=False) + "\n")
                count += 1
        receipt = {
            "revision_id": args.revision_id,
            "written": os.path.abspath(args.out),
            "changed_item_count_in_file": count,
            "stored_effect_summary": json.loads(rev["effect_summary_json"]),
        }
        if args.receipt_out:
            write_json_atomic(args.receipt_out, receipt)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            sys.stdout.write(json.dumps(change_output(conn, row), ensure_ascii=False) + "\n")
            count += 1
    conn.close()
    return 0


def cmd_revise(args):
    req = load_request(args.request)
    conn = connect_ledger(args.ledger)
    require_schema(conn)
    try:
        receipt = apply_revision(conn, req)
    finally:
        conn.close()

    if args.receipt_out:
        write_json_atomic(args.receipt_out, receipt)

    if args.diff_out:
        # 从已提交库只读导出；重放同一请求不会新增变化行，因此导出的是同一净变化。
        rconn = connect_ledger(args.ledger, readonly=True)
        try:
            rows = rconn.execute(
                "SELECT * FROM revision_change WHERE revision_id=? "
                "ORDER BY item_seq", (receipt["revision_id"],))
            with atomic_text_writer(args.diff_out) as fh:
                n = 0
                for row in rows:
                    fh.write(json.dumps(change_output(rconn, row),
                                        ensure_ascii=False) + "\n")
                    n += 1
            receipt["diff_file"] = os.path.abspath(args.diff_out)
            receipt["diff_file_change_count"] = n
        finally:
            rconn.close()
        if args.receipt_out:
            write_json_atomic(args.receipt_out, receipt)

    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


def cmd_revisions(args):
    conn = connect_ledger(args.ledger, readonly=True)
    require_schema(conn)
    if args.revision_id:
        row = conn.execute(
            "SELECT * FROM revision WHERE revision_id=?",
            (args.revision_id,)).fetchone()
        if row is None:
            raise LedgerError("找不到修订号: %s" % args.revision_id)
        out = {k: row[k] for k in row.keys()}
        out["effect_summary"] = json.loads(out.pop("effect_summary_json"))
        out["canonical_request"] = json.loads(out["canonical_request"])
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    rows = conn.execute(
        "SELECT revision_id, kind, target_type, target_id, created_at, "
        "effect_summary_json FROM revision ORDER BY rowid").fetchall()
    for row in rows:
        summary = json.loads(row["effect_summary_json"])
        line = {
            "revision_id": row["revision_id"],
            "kind": row["kind"],
            "target": [row["target_type"], row["target_id"]],
            "created_at": row["created_at"],
            "affected": summary.get("affected_item_count"),
            "changed": summary.get("changed_item_count"),
        }
        sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
    conn.close()
    return 0


def cmd_show(args):
    item_id = parse_json_scalar(args.item_id)
    key = item_key(item_id)
    conn = connect_ledger(args.ledger, readonly=True)
    require_schema(conn)
    row = conn.execute(SELECT_ITEM_WITH_ROUTE + " WHERE i.id_key=?",
                       (key,)).fetchone()
    if row is None:
        raise LedgerError("找不到 item_id: %r" % item_id)
    declaration = get_declaration(conn, row["old_leaf"])
    history = [dict(r) for r in conn.execute(
        "SELECT revision_id, action, new_leaf, created_at "
        "FROM manual_route_history WHERE item_id_key=? ORDER BY rowid", (key,))]
    changes = [dict(r) for r in conn.execute(
        "SELECT revision_id, change_kind, before_json, after_json "
        "FROM revision_change WHERE item_id_key=? ORDER BY rowid", (key,))]
    for c in changes:
        c["before"] = json.loads(c.pop("before_json"))
        c["after"] = json.loads(c.pop("after_json"))
    original = json.loads(row["raw_json"])
    result = {
        "item_id": item_id,
        "current_output": merged_item(row),
        "original_item": original,
        "current_declaration": declaration,
        "manual_routing_current": None if row["m_active"] is None else {
            "active": bool(row["m_active"]),
            "new_leaf": row["m_new_leaf"],
            "revision_id": row["m_rev"],
        },
        "manual_routing_history": history,
        "classification_changes": changes,
    }
    conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# ----------------------------- CLI -----------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="可持续查询的货品迁移账本（migrate.py ledger 也可调用）")
    sub = p.add_subparsers(dest="command", required=True)

    q = sub.add_parser("init", help="从完整货品 JSONL、树和双向对照初始化账本")
    q.add_argument("--ledger", required=True, help="账本目录（内含 ledger.sqlite3）")
    q.add_argument("--items", required=True, help="完整货品 JSONL")
    q.add_argument("--old-tree", required=True)
    q.add_argument("--new-tree", required=True)
    q.add_argument("--mapping", "--edges", dest="mapping", required=True,
                   help="既有双向叶子对照 JSONL（与 migrate.py --edges 同格式）")
    q.add_argument("--item-id-field", default="item_id")
    q.add_argument("--old-leaf-field", default="old_leaf")
    q.set_defaults(func=cmd_init)

    r = sub.add_parser("revise", aliases=["apply", "revision"],
                       help="提交一个旧叶声明替换或 item 人工分流修订")
    r.add_argument("--ledger", required=True)
    r.add_argument("--request", required=True, help="单个修订 JSON 对象")
    r.add_argument("--diff-out", help="把该修订的净变化 JSONL 原子导出到此")
    r.add_argument("--receipt-out", help="把修订回执 JSON 原子导出到此")
    r.set_defaults(func=cmd_revise)

    e = sub.add_parser("export", help="导出当前 migrated/pending JSONL 和摘要")
    e.add_argument("--ledger", required=True)
    e.add_argument("--outdir", required=True)
    e.set_defaults(func=cmd_export)

    s = sub.add_parser("summary", help="当前迁移摘要")
    s.add_argument("--ledger", required=True)
    s.add_argument("--out", help="可选：原子写入摘要 JSON")
    s.set_defaults(func=cmd_summary)

    l = sub.add_parser("query", aliases=["list"], help="按状态/原因/旧叶/item 查询当前结果")
    l.add_argument("--ledger", required=True)
    l.add_argument("--status", choices=["all", "migrated", "pending"], default="all")
    l.add_argument("--reason")
    l.add_argument("--old-leaf")
    l.add_argument("--item-id")
    l.add_argument("--limit", type=int)
    l.add_argument("--out", help="可选：原子写入 JSONL；缺省输出到 stdout")
    l.set_defaults(func=cmd_query)

    d = sub.add_parser("diff", aliases=["delta", "net-change"],
                       help="查询某次已提交修订的前后净变化")
    d.add_argument("--ledger", required=True)
    d.add_argument("--revision-id", required=True)
    d.add_argument("--out")
    d.add_argument("--receipt-out")
    d.set_defaults(func=cmd_diff)

    v = sub.add_parser("revisions", help="列出修订号，或查看一个修订的原始请求/回执")
    v.add_argument("--ledger", required=True)
    v.add_argument("--revision-id")
    v.set_defaults(func=cmd_revisions)

    sh = sub.add_parser("show", help="查看单个货品的当前判定、关系、分流和变化史")
    sh.add_argument("--ledger", required=True)
    sh.add_argument("--item-id", required=True)
    sh.set_defaults(func=cmd_show)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except LedgerError as exc:
        print("账本输入错误: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("账本文件/IO 错误，本次操作未提交: %s" % exc, file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print("输入 JSON 损坏，本次操作未提交: %s" % exc, file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print("账本数据库错误（本次事务已回滚，旧版本仍可打开）: %s" % exc,
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
