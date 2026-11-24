#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成迁移账本合成演示数据。

场景刻意覆盖：
  * 唯一且全部核准：自动迁入；
  * 多核准去向：有逐件分流才迁入，否则待处理；
  * 核准撤回：已自动迁入货品重新变待处理；
  * 关系替换导致旧分流目标消失：分流失效；
  * 幂等重试 / 同号不同内容冲突 / 坏引用整次拒绝；
  * 未列全、无去向、含未核准边。
"""
import json
import os
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else "/workspace/ledger_demo_data"
REQ = os.path.join(OUT, "revisions")
os.makedirs(REQ, exist_ok=True)


def tree(root, prefix, labels):
    nodes = {root: {"parent": None, "leaf": False, "name": root + " 根"}}
    for leaf, label in labels.items():
        nodes[leaf] = {"parent": root, "leaf": True, "name": label}
    return {"root": root, "nodes": nodes}


old_labels = {
    "O_AUTO": "唯一核准去向",
    "O_MULTI": "多个核准去向",
    "O_WITHDRAW": "将被撤回核准",
    "O_ROUTE": "分流目标将因关系替换消失",
    "O_INCOMPLETE": "去向未列全",
    "O_NONE": "列全但确认无去向",
    "O_UNAPPROVED": "含未核准边",
}
new_labels = {
    "N_AUTO": "自动迁入叶",
    "N_M1": "多去向一",
    "N_M2": "多去向二",
    "N_WD": "核准将撤回叶",
    "N_R1": "旧分流目标（将被移除）",
    "N_R2": "保留去向",
    "N_R3": "新替代去向",
    "N_INC": "未列全关系叶",
    "N_UA": "未核准叶",
}

with open(os.path.join(OUT, "old_tree.json"), "w", encoding="utf-8") as fh:
    json.dump(tree("OROOT", "O", old_labels), fh, ensure_ascii=False, indent=2)
with open(os.path.join(OUT, "new_tree.json"), "w", encoding="utf-8") as fh:
    json.dump(tree("NROOT", "N", new_labels), fh, ensure_ascii=False, indent=2)

edges = [
    ("O_AUTO", "N_AUTO", True),
    ("O_MULTI", "N_M1", True),
    ("O_MULTI", "N_M2", True),
    ("O_WITHDRAW", "N_WD", True),
    ("O_ROUTE", "N_R1", True),
    ("O_ROUTE", "N_R2", True),
    ("O_INCOMPLETE", "N_INC", True),
    ("O_UNAPPROVED", "N_UA", False),
]
old_complete = {
    "O_AUTO": True,
    "O_MULTI": True,
    "O_WITHDRAW": True,
    "O_ROUTE": True,
    "O_INCOMPLETE": False,
    "O_NONE": True,
    "O_UNAPPROVED": True,
}
with open(os.path.join(OUT, "mapping.jsonl"), "w", encoding="utf-8") as fh:
    for o, n, a in edges:
        fh.write(json.dumps({"type": "edge", "old": o, "new": n,
                             "approved": a}, ensure_ascii=False) + "\n")
    for leaf, complete in old_complete.items():
        fh.write(json.dumps({"type": "completeness", "side": "old",
                             "leaf": leaf, "complete": complete},
                            ensure_ascii=False) + "\n")
    for leaf in new_labels:
        fh.write(json.dumps({"type": "completeness", "side": "new",
                             "leaf": leaf, "complete": True},
                            ensure_ascii=False) + "\n")

items = [
    {"item_id": "ITEM-AUTO-1", "old_leaf": "O_AUTO", "title": "自动货一", "price": 19.9, "tags": ["a", "b"]},
    {"item_id": "ITEM-AUTO-2", "old_leaf": "O_AUTO", "title": "自动货二", "price": 20.0},
    {"item_id": "ITEM-MULTI-1", "old_leaf": "O_MULTI", "title": "多去向逐件指定 M1", "sku": "M-1"},
    {"item_id": "ITEM-MULTI-2", "old_leaf": "O_MULTI", "title": "多去向但无分流", "sku": "M-2"},
    {"item_id": "ITEM-WD-1", "old_leaf": "O_WITHDRAW", "title": "核准撤回货一"},
    {"item_id": "ITEM-WD-2", "old_leaf": "O_WITHDRAW", "title": "核准撤回货二"},
    {"item_id": "ITEM-ROUTE-1", "old_leaf": "O_ROUTE", "title": "分流随后失效"},
    {"item_id": "ITEM-ROUTE-2", "old_leaf": "O_ROUTE", "title": "另一个多去向无分流"},
    {"item_id": "ITEM-INC-1", "old_leaf": "O_INCOMPLETE", "title": "未列全"},
    {"item_id": "ITEM-NONE-1", "old_leaf": "O_NONE", "title": "无去向"},
    {"item_id": "ITEM-UA-1", "old_leaf": "O_UNAPPROVED", "title": "未核准边"},
]
with open(os.path.join(OUT, "items.jsonl"), "w", encoding="utf-8") as fh:
    for item in items:
        fh.write(json.dumps(item, ensure_ascii=False) + "\n")

# 坏输入：重复 item_id；初始化必须整次拒绝，不得留下正式 ledger.sqlite3。
bad_items = items + [dict(items[0])]
with open(os.path.join(OUT, "items_duplicate.jsonl"), "w", encoding="utf-8") as fh:
    for item in bad_items:
        fh.write(json.dumps(item, ensure_ascii=False) + "\n")

requests = {
    # 成功的人工分流；之后同文件重送应 no-op。
    "rev_001_route_multi1.json": {
        "revision_id": "REV-001",
        "kind": "manual_routing",
        "item_id": "ITEM-MULTI-1",
        "action": "replace",
        "new_leaf": "N_M1",
        "note": "合成：逐件指定已核准去向",
    },
    "rev_002_route_route1.json": {
        "revision_id": "REV-002",
        "kind": "manual_routing",
        "item_id": "ITEM-ROUTE-1",
        "action": "replace",
        "new_leaf": "N_R1",
    },
    # 核准撤回：完整替换 O_WITHDRAW 声明，唯一去向变成未核准。
    "rev_003_withdraw_approval.json": {
        "revision_id": "REV-003",
        "kind": "old_leaf_destinations",
        "old_leaf": "O_WITHDRAW",
        "complete": True,
        "destinations": [
            {"new_leaf": "N_WD", "approved": False},
        ],
        "note": "合成：治理员撤回核准",
    },
    # 关系替换：移除 N_R1，加入 N_R3；旧分流 ITEM-ROUTE-1 指向消失目标。
    "rev_004_replace_route_relation.json": {
        "revision_id": "REV-004",
        "kind": "old_leaf_destinations",
        "old_leaf": "O_ROUTE",
        "complete": True,
        "destinations": [
            {"new_leaf": "N_R2", "approved": True},
            {"new_leaf": "N_R3", "approved": True},
        ],
    },
    # 修复失效分流，指向新核准去向 N_R3。
    "rev_005_repair_route.json": {
        "revision_id": "REV-005",
        "kind": "manual_routing",
        "item_id": "ITEM-ROUTE-1",
        "action": "replace",
        "new_leaf": "N_R3",
    },
    # 撤销 ITEM-MULTI-1：多去向下应回到待处理。
    "rev_006_revoke_multi1.json": {
        "revision_id": "REV-006",
        "kind": "manual_routing",
        "item_id": "ITEM-MULTI-1",
        "action": "revoke",
    },
    # 坏引用：item 不存在；必须拒绝且不占用任何已提交修订状态。
    "rev_bad_unknown_item.json": {
        "revision_id": "REV-BAD-REF",
        "kind": "manual_routing",
        "item_id": "ITEM-DOES-NOT-EXIST",
        "action": "replace",
        "new_leaf": "N_M1",
    },
    # 与 REV-001 同号不同内容：必须拒绝。
    "rev_001_conflicting_reuse.json": {
        "revision_id": "REV-001",
        "kind": "manual_routing",
        "item_id": "ITEM-MULTI-1",
        "action": "replace",
        "new_leaf": "N_M2",
    },
}
for name, doc in requests.items():
    with open(os.path.join(REQ, name), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

print("已生成合成账本演示数据：%s" % OUT)
print("修订请求目录：%s" % REQ)
