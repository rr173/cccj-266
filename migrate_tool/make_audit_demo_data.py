#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成对照片发布审计的演示数据（合成数据，纯本地）。覆盖：

  1) 可发布的闭合关系块：OA <-> NA，两侧都明确列全；
  2) 因单侧确认缺失而未闭合的关系块：OB <-> NA 侧确认齐全，
     但新叶 NB 没有任何完整性确认记录（缺失确认）；
  3) 明确未列全的未闭合关系块：OC 自报 complete=false（仍可能有未列去向）；
  4) 一条未核准边：OE -> NE，不进关系块，只作待处理边保留；
  5) 确认无对应的孤立叶：OD（旧侧）、ND（新侧）无核准边且明确列全，不是问题。

对照片行序故意打乱，用于验证审计结果与 JSONL 行序无关。
"""
import json
import os

OUT = "/workspace/audit_demo_data"
os.makedirs(OUT, exist_ok=True)

# ---------------- 旧树 ----------------
old_rows = [
    ("ORA", None,  False, "审计旧树根"),
    ("OC1", "ORA", False, "承接确认区"),
    ("OA",  "OC1", True,  "闭合关系旧叶"),
    ("OB",  "OC1", True,  "对端缺确认旧叶"),
    ("OC",  "OC1", True,  "明确未列全旧叶"),
    ("OC2", "ORA", False, "无对应确认区"),
    ("OD",  "OC2", True,  "确认无对应旧叶"),
    ("OE",  "OC2", True,  "未核准边旧叶"),
]
old_nodes = {nid: {"parent": p, "leaf": leaf, "name": nm}
             for (nid, p, leaf, nm) in old_rows}
with open(os.path.join(OUT, "old_tree.json"), "w", encoding="utf-8") as fh:
    json.dump({"root": "ORA", "nodes": old_nodes}, fh, ensure_ascii=False, indent=2)

# ---------------- 新树 ----------------
new_rows = [
    ("NRA", None,  False, "审计新树根"),
    ("NC1", "NRA", False, "承接确认区"),
    ("NA",  "NC1", True,  "闭合关系新叶"),
    ("NB",  "NC1", True,  "确认缺失新叶"),
    ("NC",  "NC1", True,  "承接未列全旧叶的新叶"),
    ("NC2", "NRA", False, "无对应确认区"),
    ("ND",  "NC2", True,  "确认无对应新叶"),
    ("NE",  "NC2", True,  "未核准边新叶"),
]
new_nodes = {nid: {"parent": p, "leaf": leaf, "name": nm}
             for (nid, p, leaf, nm) in new_rows}
with open(os.path.join(OUT, "new_tree.json"), "w", encoding="utf-8") as fh:
    json.dump({"root": "NRA", "nodes": new_nodes}, fh, ensure_ascii=False, indent=2)

# ---------------- 对照片（行序故意打乱） ----------------
records = [
    {"type": "edge", "old": "OB", "new": "NB", "approved": True},
    {"type": "completeness", "side": "new", "leaf": "NA", "complete": True},
    {"type": "edge", "old": "OE", "new": "NE", "approved": False},   # 未核准边
    {"type": "completeness", "side": "old", "leaf": "OA", "complete": True},
    {"type": "edge", "old": "OA", "new": "NA", "approved": True},
    {"type": "completeness", "side": "old", "leaf": "OC", "complete": False},  # 明确未列全
    {"type": "completeness", "side": "new", "leaf": "NC", "complete": True},
    {"type": "edge", "old": "OC", "new": "NC", "approved": True},
    {"type": "completeness", "side": "old", "leaf": "OB", "complete": True},
    {"type": "completeness", "side": "new", "leaf": "ND", "complete": True},
    {"type": "completeness", "side": "old", "leaf": "OD", "complete": True},
    {"type": "completeness", "side": "new", "leaf": "NE", "complete": True},
    {"type": "completeness", "side": "old", "leaf": "OE", "complete": True},
    # 注意：新叶 NB  deliberately 没有任何 completeness 记录 -> 缺失确认
]
with open(os.path.join(OUT, "edges.jsonl"), "w", encoding="utf-8") as fh:
    for rec in records:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

print("审计演示数据已生成于 %s" % OUT)
