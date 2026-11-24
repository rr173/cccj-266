#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成演示用两版分类树、老筛选与双向对照片（合成数据，纯本地）。"""
import json
import os

OUT = "/workspace/demo_data"
os.makedirs(OUT, exist_ok=True)

# ---------------- 旧树 ----------------
# (id, parent, leaf, name)
old_rows = [
    ("O1",  None, False, "旧采购根"),
    ("O2",  "O1", False, "整类勾选区（内部有挖除）"),
    ("O3",  "O2", True,  "直接旧叶"),
    ("O4",  "O2", False, "被挖掉的子类"),
    ("O5",  "O4", True,  "挖除叶A"),
    ("O6",  "O4", True,  "挖除叶B"),
    ("O7",  "O1", False, "干净整选类"),
    ("O8",  "O7", True,  "干净旧叶"),
    ("O9",  "O1", True,  "一对多拆分类旧叶"),
    ("O10", "O1", True,  "多对一来源A（选）"),
    ("O11", "O1", True,  "多对一来源B（不选）"),
    ("O12", "O1", True,  "老范围外旧叶（不选）"),
    ("O13", "O1", True,  "去向未列全旧叶（选）"),
    ("O14", "O1", True,  "确认无去向旧叶（选）"),
    ("O20", "O1", True,  "指向待复核新叶的旧叶（选）"),
    ("O22", "O1", False, "深层整选链顶"),
    ("O15", "O22", True, "深链旧叶A"),
    ("O16", "O22", True, "深链旧叶B"),
    ("O23", "O1", False, "兄弟取舍不同的内部类"),
    ("O17", "O23", True, "兄弟中被选旧叶"),
    ("O18", "O23", True, "兄弟中不选旧叶"),
]
old_nodes = {nid: {"parent": p, "leaf": leaf, "name": nm}
             for (nid, p, leaf, nm) in old_rows}
with open(os.path.join(OUT, "old_tree.json"), "w", encoding="utf-8") as fh:
    json.dump({"root": "O1", "nodes": old_nodes}, fh, ensure_ascii=False, indent=2)

# 老筛选：默认不选；O2 整类 include，但 O4 显式 exclude（最深标记胜）
old_filter = {"marks": {
    "O2": "include",   # O3 被选；O5/O6 因 O4=exclude 被挖掉
    "O4": "exclude",
    "O7": "include",   # O8 选
    "O9": "include",
    "O10": "include",  # O11/O12 无标记 -> 不选
    "O13": "include",
    "O14": "include",
    "O20": "include",
    "O22": "include",  # O15/O16 选
    "O17": "include",  # O18 不选，演示最大子树不能上提
}}
with open(os.path.join(OUT, "old_filter.json"), "w", encoding="utf-8") as fh:
    json.dump(old_filter, fh, ensure_ascii=False, indent=2)

# ---------------- 新树 ----------------
new_rows = [
    ("N1",  None, False, "新采购根"),
    ("N2",  "N1", False, "干净承接类"),
    ("N8",  "N2", True,  "干净承接叶"),
    ("N3",  "N1", False, "拆分承接类"),
    ("N3c", "N3", True,  "直接承接叶"),
    ("N9a", "N3", True,  "拆分承接A"),
    ("N9b", "N3", True,  "拆分承接B"),
    ("N4",  "N1", False, "混合多对一区"),
    ("N10", "N4", True,  "混合多对一叶"),
    ("N5",  "N1", False, "来源不全区"),
    ("N12", "N5", True,  "范围外但来源未列全叶"),
    ("N6",  "N1", False, "确认排除区"),
    ("N13", "N6", True,  "确认无历史来源叶"),
    ("N5b", "N6", True,  "仅不选来源的排除叶"),
    ("N99", "N6", True,  "干净承接叶"),          # 与 N8 同名不同 ID，禁止归并
    ("N7",  "N1", False, "部分迁交区"),
    ("N7a", "N7", True,  "仅承接部分旧货物叶"),
    ("N8b", "N1", True,  "待复核混合新叶"),
    ("N21", "N1", False, "深层链顶"),
    ("N15", "N21", True, "深链新叶A"),
    ("N16", "N21", True, "深链新叶B"),
    ("N23", "N1", False, "新混合区"),
    ("N17", "N23", True, "新选叶"),
    ("N18", "N23", True, "新不选叶"),
]
new_nodes = {nid: {"parent": p, "leaf": leaf, "name": nm}
             for (nid, p, leaf, nm) in new_rows}
with open(os.path.join(OUT, "new_tree.json"), "w", encoding="utf-8") as fh:
    json.dump({"root": "N1", "nodes": new_nodes}, fh, ensure_ascii=False, indent=2)

# ---------------- 对照片（边 + 完整性确认，JSONL 流式格式） ----------------
edges = [
    # 干净承接
    ("O8", "N8", True),
    # 最深标记：O3 选、O5/O6 被挖除
    ("O3", "N3c", True),
    ("O5", "N5b", True),       # 不选来源 -> N5b 确定排除
    # 一对多拆分（旧叶 O9 -> 两片新叶）
    ("O9", "N9a", True),
    ("O9", "N9b", True),
    # 多对一混合（选中+不选旧叶汇入同一新叶）
    ("O10", "N10", True),
    ("O11", "N10", True),
    # 来源未列全的新叶：当前只挂了不选来源，也不能判排除
    ("O12", "N12", True),
    # 部分迁交：旧叶去向未列全，但新叶来源列全且全选中
    ("O13", "N7a", True),
    # 待复核混合新叶（选中 O20 + 不选 O18）
    ("O20", "N8b", True),
    ("O18", "N8b", True),
    # 未核准边：只留见证，不参与判定（O14 因此仍属“无去向”）
    ("O14", "N8b", False),
    # 深链
    ("O15", "N15", True),
    ("O16", "N16", True),
    # 兄弟取舍不同
    ("O17", "N17", True),
    ("O18", "N18", True),
    # N13、N99  deliberately 无边：N13 演示“无历史来源”，N99 演示同名不归并
]
new_complete_true = [nid for (nid, p, leaf, nm) in new_rows if leaf and nid != "N12"]
old_complete_true = [nid for (nid, p, leaf, nm) in old_rows if leaf and nid != "O13"]

with open(os.path.join(OUT, "edges.jsonl"), "w", encoding="utf-8") as fh:
    for oid, nid, approved in edges:
        fh.write(json.dumps({"type": "edge", "old": oid, "new": nid,
                             "approved": approved}, ensure_ascii=False) + "\n")
    for nid in new_complete_true:
        fh.write(json.dumps({"type": "completeness", "side": "new",
                             "leaf": nid, "complete": True}, ensure_ascii=False) + "\n")
    fh.write(json.dumps({"type": "completeness", "side": "new",
                         "leaf": "N12", "complete": False}, ensure_ascii=False) + "\n")
    for oid in old_complete_true:
        fh.write(json.dumps({"type": "completeness", "side": "old",
                             "leaf": oid, "complete": True}, ensure_ascii=False) + "\n")
    fh.write(json.dumps({"type": "completeness", "side": "old",
                         "leaf": "O13", "complete": False}, ensure_ascii=False) + "\n")

print("演示数据已生成于 %s" % OUT)
