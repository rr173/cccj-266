#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线分类筛选迁交工具（纯标准库，python3 -I 安全；紧凑存储版）

输入：
  --old-tree / --new-tree  分类树。支持两种格式：
        1) 整份 JSON：{"root": "id", "nodes": {"id": {"name":..., "leaf": bool, "parent": "id|null"}}}
        2) JSONL：每行一条节点记录 {"id":..., "name":..., "leaf":bool, "parent":...}，根的 parent 为 null
  --old-filter             老筛选：{"marks": {"节点id": "include"|"exclude"}}（也接受 true/false）
  --edges                  双向对照片 JSONL，逐行流式读取，每行两类记录：
        {"type":"edge", "old":"旧叶id", "new":"新叶id", "approved": true|false}
        {"type":"completeness", "side":"new"|"old", "leaf":"叶id", "complete": true|false}
  --outdir                 报告输出目录

判定规则（与交接约定一一对应）：
  * 老叶取舍 = 根→叶路径上最深的 include/exclude 标记；无标记默认不选。
  * 新叶只认“已核准(approved=true)”来源边；未核准边只进见证，不参与判定。
  * 新叶来源未列全(complete=false)        -> 待复核 REVIEW（即使当前无已选来源也不能确认排除）
  * 来源列全 + 仅有选中来源(>=1)          -> 确定包含 INCLUDE
  * 来源列全 + 无任何选中来源             -> 确定排除 EXCLUDE（无已核准边时另标“无历史来源”）
  * 来源列全 + 选中/不选来源两边都有      -> 待复核 REVIEW
  * 可执行新筛选 = 全部 INCLUDE 新叶的最大可整选子树（新系统只支持包含整棵子树）。

规模与流式约定：
  * 节点在摄入期即转为紧凑整数索引，邻接表/父指针/叶标记/状态均为定长数组（每节点约百字节
    量级），字符串 id 与名称各只保留一份；超内存场景可把这层 TreeStore 换成外存/KV 实现，
    上层算法不变。
  * 全程迭代 DFS、显式栈，不使用递归，树深无业务上限；栈帧只携带一个“当前生效标记”，
    不逐叶复制祖先路径。
  * 边表 JSONL 单行扫描、只过一遍：边证据即时落盘 evidence.jsonl，内存只保留每叶聚合信息
    与对端整数索引，绝不逐条边回扫整棵树。树的遍历趟数为常数（建校验/生效标记/
    子树状态/输出），与边数无关。

对照片发布审计（audit 子命令，不依赖老筛选）：
  python3 -I migrate.py audit --old-tree T1 --new-tree T2 --edges E.jsonl --outdir OUT
  只读旧树、新树与 JSONL 对照片，判断对照片本身是否闭合到可发布：
  已核准边把两端叶并入同一关系块；块内任一叶完整性确认缺失或明确未列全
  -> 块未闭合；未核准边不进关系块、作为待处理边单独保留；存在未闭合块
  或待处理边 -> 整体不可发布。详见 README 第 9 节。
"""

import argparse
import array
import json
import os
import resource
import struct
import sys
from datetime import datetime, timezone

# ----------------------------- 基础工具 -----------------------------

def fail(msg):
    sys.stderr.write("输入错误: %s\n" % msg)
    sys.exit(2)


def iter_jsonl(path):
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                fail("%s 第 %d 行不是合法 JSON: %s" % (path, lineno, exc))
            if not isinstance(rec, dict):
                fail("%s 第 %d 行必须是对象记录" % (path, lineno))
            yield rec


class TreeStore:
    """
    紧凑树存储：整数索引 + 定长数组。
      parent[]       array('i')  父节点索引（根为 -1）
      first_child[]  array('i')  首子索引（叶为 -1）
      next_sib[]     array('i')  右兄弟索引（末子为 -1）
    每节点邻接信息固定 12 字节，无逐节点 Python 列表对象。
    叶子紧凑索引区间 [0, leaf_n)。
    """

    __slots__ = ("path", "ids", "names", "parent", "first_child", "next_sib",
                 "is_leaf", "leaf_n", "root", "preorder", "leaf_index",
                 "dup_names", "_id_map_cache", "_leaf_to_node")

    def __init__(self, path):
        self.path = path
        # 名称驻留：深树里重名极多，驻留后 names 只保留一个字符串对象。
        intern = sys.intern

        if path.endswith(".jsonl"):
            # 两趟流式读取：第一趟只建 id 索引/名称/叶标记，不保留整份记录元组；
            # 第二趟按 id 解析父指针。任意时刻内存里只有一行 JSON。
            ids, names, id_to_idx, is_leaf_list = [], [], {}, []
            for rec in iter_jsonl(path):
                nid = rec.get("id")
                if nid is None or "leaf" not in rec:
                    fail("%s 节点记录缺少 id/leaf 字段: %r" % (path, rec))
                if not isinstance(nid, str) or not nid:
                    fail("节点 id 必须是非空字符串: %r" % (nid,))
                if nid in id_to_idx:
                    fail("节点 id 重复: %s" % nid)
                id_to_idx[nid] = len(ids)
                ids.append(nid)
                names.append(intern(str(rec.get("name", nid))))
                is_leaf_list.append(bool(rec["leaf"]))

            n = len(ids)
            if n == 0:
                fail("%s 是空树" % path)
            parent_str = [None] * n
            idx = 0
            for rec in iter_jsonl(path):
                parent_str[idx] = rec.get("parent")
                idx += 1
        else:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict) or "root" not in doc or "nodes" not in doc:
                fail("%s 需为 {root, nodes} 结构" % path)
            nodes = doc["nodes"]
            if not isinstance(nodes, dict):
                fail("%s nodes 必须为对象" % path)
            ids, names, parent_str, is_leaf_list = [], [], [], []
            id_to_idx = {}
            for nid, info in nodes.items():
                if not isinstance(nid, str) or not nid:
                    fail("节点 id 必须是非空字符串: %r" % (nid,))
                if nid in id_to_idx:
                    fail("节点 id 重复: %s" % nid)
                id_to_idx[nid] = len(ids)
                ids.append(nid)
                names.append(intern(str(info.get("name", nid))))
                parent_str.append(info.get("parent"))
                is_leaf_list.append(bool(info["leaf"]))
            n = len(ids)
            if n == 0:
                fail("%s 是空树" % path)

        self.ids = ids
        self.names = names
        self.is_leaf = bytearray(is_leaf_list)
        del is_leaf_list
        self.parent = array.array("i", [-1]) * n
        self.first_child = array.array("i", [-1]) * n
        self.next_sib = array.array("i", [-1]) * n

        root_idx = -1
        for idx, p in enumerate(parent_str):
            if p is None:
                if root_idx != -1:
                    fail("存在多个根节点: %s 与 %s" % (ids[root_idx], ids[idx]))
                root_idx = idx
            else:
                if p not in id_to_idx:
                    fail("节点 %s 的父节点 %s 不存在" % (ids[idx], p))
                pidx = id_to_idx[p]
                self.parent[idx] = pidx
                # 头插法挂子：新孩子成为首子，原首子成为其右兄弟
                if self.first_child[pidx] == -1:
                    self.first_child[pidx] = idx
                else:
                    self.next_sib[idx] = self.first_child[pidx]
                    self.first_child[pidx] = idx
        if root_idx == -1:
            fail("树中没有根节点（parent=null 的节点）")
        self.root = root_idx

        # 叶/非叶一致性
        for idx in range(n):
            has_child = self.first_child[idx] != -1
            if self.is_leaf[idx] and has_child:
                fail("节点 %s 声明为叶却有子节点" % ids[idx])
            if (not self.is_leaf[idx]) and not has_child and idx != root_idx:
                fail("非叶节点 %s 没有子节点" % ids[idx])

        # 连通性 + 环检测，O(n)：
        # 从每个节点沿 parent 向上，遇已处理节点即停；经过未处理节点先记账。
        # 若在同一次上行中再次撞到在途节点（灰撞灰），说明进入环。
        color = bytearray(n)          # 0 未处理(白) 1 在途(灰) 2 已到根(黑)
        for start in range(n):
            if color[start] != 0:
                continue
            path = []
            x = start
            while x != -1 and color[x] == 0:
                color[x] = 1
                path.append(x)
                x = self.parent[x]
            if x != -1 and color[x] == 1:
                fail("树存在环，节点 %s 位于环上" % ids[path[-1]])
            for y in path:
                color[y] = 2

        # 迭代 DFS 生成 preorder（显式栈，无递归；右兄弟链逆序入栈保持稳定顺序）
        preorder = array.array("i")
        stack = [root_idx]
        while stack:
            x = stack.pop()
            preorder.append(x)
            kids = []
            c = self.first_child[x]
            while c != -1:
                kids.append(c)
                c = self.next_sib[c]
            for c in reversed(kids):
                stack.append(c)
        if len(preorder) != n:
            fail("存在与根不连通的节点")
        self.preorder = preorder

        # 叶子紧凑索引（先按 preorder 排序，输出稳定）。
        # leaf_index_by_node[节点索引] = 叶索引，非叶填 -1；定长数组而非 dict。
        leaf_index_by_node = array.array("i", [-1]) * n
        leaf_to_node = array.array("i", [-1]) * sum(self.is_leaf)
        leaf_counter = 0
        for x in preorder:
            if self.is_leaf[x]:
                leaf_index_by_node[x] = leaf_counter
                leaf_to_node[leaf_counter] = x
                leaf_counter += 1
        self.leaf_index = leaf_index_by_node
        self.leaf_n = leaf_counter
        self._id_map_cache = id_to_idx   # 复用构造期字典，不再重建
        self._leaf_to_node = leaf_to_node

        # 同名提示（仅提示，绝不归并——一切以稳定 id 为准）
        name_buckets = {}
        for idx, nm in enumerate(names):
            name_buckets.setdefault(nm, []).append(ids[idx])
        self.dup_names = {nm: idl for nm, idl in name_buckets.items() if len(idl) > 1}

    def iter_children(self, nidx):
        c = self.first_child[nidx]
        while c != -1:
            yield c
            c = self.next_sib[c]


    def require_leaf(self, nid, side):
        node_idx = self._id_map_cache.get(nid, -1)
        if node_idx == -1:
            fail("节点 %s 不存在或不是%s树叶子" % (nid, side))
        li = self.leaf_index[node_idx]
        if li == -1:
            fail("节点 %s 不存在或不是%s树叶子" % (nid, side))
        return li

    def require_node(self, nid, side):
        if nid not in self._id_map_cache:
            fail("筛选标记的节点 %s 在%s树中不存在" % (nid, side))
        return self._id_map_cache[nid]

    def _idx_quiet(self, nid):
        return self._id_map_cache.get(nid, -1)

    def leaf_indices_preorder(self):
        for x in self.preorder:
            if self.is_leaf[x]:
                yield self.leaf_index[x]

    def node_idx_for_leaf(self, leaf_idx):
        return self._leaf_to_node[leaf_idx]


def load_filter(path, tree, side_label):
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    marks_raw = doc.get("marks")
    if not isinstance(marks_raw, dict):
        fail("%s 需为 {marks: {节点id: include|exclude}} 结构" % path)
    marks = {}
    for nid, val in marks_raw.items():
        idx = tree.require_node(nid, side_label)
        if val in ("include", True):
            marks[idx] = True
        elif val in ("exclude", False):
            marks[idx] = False
        else:
            fail("%s 节点 %s 的标记值非法: %r（仅支持 include/exclude）" % (path, nid, val))
    return marks


def resolve_effective_selection(tree, marks):
    """
    根→叶路径上最深标记决定叶的取舍。
    迭代 DFS：每个栈帧只携带一个当前生效布尔值，不复制祖先路径。
    返回长度=叶数的 bytearray（1=选中）。
    """
    selected = bytearray(tree.leaf_n)
    stack = [(tree.root, -1)]  # (节点索引, 路径上最深标记：-1 无 / 0 exclude / 1 include)
    while stack:
        nidx, cur = stack.pop()
        if nidx in marks:
            cur = 1 if marks[nidx] else 0
        if tree.is_leaf[nidx]:
            if cur == 1:
                selected[tree.leaf_index[nidx]] = 1
        # 右兄弟链本身即“倒序”，直接依次入栈即可保持稳定的先左后右弹出顺序
        c = tree.first_child[nidx]
        while c != -1:
            stack.append((c, cur))
            c = tree.next_sib[c]
    return selected


# ----------------------------- 边表流式摄入 -----------------------------

def ingest_edges(path, old_tree, new_tree, evidence_fh, scratch_fh):
    """
    边表只过一遍（流式 JSONL）。内存只保留紧凑定长结构：
      new_deg[k]/old_deg[k]   每叶边数（array('i')）
      new_off/old_off         CSR 偏移（边读完即由度数前缀和得到）
      new_pair/old_pair       边对端叶索引（array('i')，按下标位写入）
      new_appr/old_appr       与 pair 平行的核准位（bytearray）
      *_complete              完整性确认（bytearray，默认 0 = 保守的未列全）
      unapproved              未核准边的整数对（仅计数通常很小）
    边的完整见证在扫描当口写 evidence_fh；同时写一份暂存到 scratch_fh，
    供输出阶段按 id 物化，避免在内存堆积字符串边明细。
    去重用每叶“已见对端”位图，不建全局 pair 集合。
    """
    on_, nn = old_tree.leaf_n, new_tree.leaf_n
    new_deg = array.array("i", [0]) * nn
    old_deg = array.array("i", [0]) * on_
    new_complete = bytearray(nn)
    old_complete = bytearray(on_)
    seen_pairs = set()           # 整数编码对，仅在出现重复边时才需要增长
    unapproved = []
    edge_seq = 0

    # 第一阶段：扫描 + 计数 + 见证落盘（不分配 pair 数组以外的 Python 容器）
    raw_pairs = []  # (oi, ni, approved) 临时紧凑三元组，随后转 CSR
    for rec in iter_jsonl(path):
        rtype = rec.get("type", "edge")
        if rtype == "completeness":
            side = rec.get("side")
            if side not in ("new", "old"):
                fail("completeness 记录 side 非法: %r" % rec)
            if not isinstance(rec.get("complete"), bool):
                fail("completeness 记录 complete 必须为布尔值: %r" % rec)
            if side == "new":
                li = new_tree.require_leaf(rec.get("leaf"), "new")
                new_complete[li] = 1 if rec["complete"] else 0
            else:
                li = old_tree.require_leaf(rec.get("leaf"), "old")
                old_complete[li] = 1 if rec["complete"] else 0
            continue

        if rtype != "edge":
            fail("未知边表记录类型: %r" % rec)
        oid, nid = rec.get("old"), rec.get("new")
        approved = rec.get("approved")
        if oid is None or nid is None or not isinstance(approved, bool):
            fail("edge 记录字段不全/类型错: %r" % rec)
        oi = old_tree.require_leaf(oid, "old")
        ni = new_tree.require_leaf(nid, "new")
        pair_key = (oi, ni)
        if pair_key in seen_pairs:
            fail("旧叶 %s 与新叶 %s 之间存在重复边记录" % (oid, nid))
        seen_pairs.add(pair_key)

        new_deg[ni] += 1
        old_deg[oi] += 1
        raw_pairs.append((oi, ni, 1 if approved else 0))
        if not approved:
            unapproved.append((oi, ni))

        scratch_fh.write(json.dumps({"oi": oi, "ni": ni, "ap": approved},
                                    ensure_ascii=False) + "\n")
        evidence_fh.write(json.dumps({
            "kind": "edge_witness",
            "old_leaf": oid,
            "old_name": old_tree.names[old_tree.node_idx_for_leaf(oi)],
            "new_leaf": nid,
            "new_name": new_tree.names[new_tree.node_idx_for_leaf(ni)],
            "approved": approved,
        }, ensure_ascii=False) + "\n")
        edge_seq += 1

    # 度数前缀和 -> CSR 偏移
    new_off = array.array("i", [0]) * (nn + 1)
    old_off = array.array("i", [0]) * (on_ + 1)
    acc = 0
    for k in range(nn):
        new_off[k] = acc
        acc += new_deg[k]
    new_off[nn] = acc
    acc = 0
    for k in range(on_):
        old_off[k] = acc
        acc += old_deg[k]
    old_off[on_] = acc

    m = len(raw_pairs)
    new_pair = array.array("i", [-1]) * m
    new_appr = bytearray(m)
    old_pair = array.array("i", [-1]) * m
    old_appr = bytearray(m)
    new_cursor = array.array("i", new_off[:nn])
    old_cursor = array.array("i", old_off[:on_])
    for (oi, ni, ap) in raw_pairs:
        p = new_cursor[ni]
        new_pair[p] = oi
        new_appr[p] = ap
        new_cursor[ni] = p + 1
        p = old_cursor[oi]
        old_pair[p] = ni
        old_appr[p] = ap
        old_cursor[oi] = p + 1
    del raw_pairs, new_cursor, old_cursor, new_deg, old_deg

    return {
        "edge_count": edge_seq,
        "new_off": new_off, "new_pair": new_pair, "new_appr": new_appr,
        "new_complete": new_complete,
        "old_off": old_off, "old_pair": old_pair, "old_appr": old_appr,
        "old_complete": old_complete,
        "unapproved": unapproved,
    }


def iter_csr(off, pair, appr, leaf_idx):
    """遍历某叶在 CSR 中的边：yield (对端叶索引, 核准位)。"""
    start, end = off[leaf_idx], off[leaf_idx + 1]
    for p in range(start, end):
        yield pair[p], appr[p]


# ----------------------------- 新叶判定 -----------------------------

REVIEW, INCLUDE, EXCLUDE = "REVIEW", "INCLUDE", "EXCLUDE"
VERDICT_CN = {INCLUDE: "确定包含", REVIEW: "待复核", EXCLUDE: "确定排除"}


def classify_new_leaves(new_tree, store, old_selected):
    """对新树每片叶子给判定；无边叶子同样必须覆盖。返回 verdict bytearray 编码 + 明细。"""
    code = {INCLUDE: 1, REVIEW: 2, EXCLUDE: 3}
    verdicts = bytearray(new_tree.leaf_n)
    details = [None] * new_tree.leaf_n
    new_off, new_pair, new_appr = store["new_off"], store["new_pair"], store["new_appr"]
    new_complete = store["new_complete"]

    for li in new_tree.leaf_indices_preorder():
        node_idx = new_tree.node_idx_for_leaf(li)
        sel_count, unsel_count, approved_count = 0, 0, 0
        for oi, ap in iter_csr(new_off, new_pair, new_appr, li):
            if ap:
                approved_count += 1
                if old_selected[oi]:
                    sel_count += 1
                else:
                    unsel_count += 1
        complete = bool(new_complete[li])

        reasons = []
        no_history = False
        if not complete:
            v = REVIEW
            reasons.append("sources_incomplete")
            if sel_count == 0:
                reasons.append("incomplete_but_no_listed_selected_source")
        elif sel_count > 0 and unsel_count == 0:
            v = INCLUDE
            reasons.append("sources_complete_all_selected")
        elif sel_count == 0:
            v = EXCLUDE
            reasons.append("sources_complete_none_selected")
            if approved_count == 0:
                no_history = True
                reasons.append("no_historical_source")
        else:
            v = REVIEW
            reasons.append("mixed_selected_and_unselected")
        verdicts[li] = code[v]

        details[li] = {
            "new_leaf": new_tree.ids[node_idx],
            "new_name": new_tree.names[node_idx],
            "verdict": v,
            "verdict_cn": VERDICT_CN[v],
            "sources_complete": complete,
            "reasons": reasons,
            "no_historical_source": no_history,
            "_csr_leaf": li,   # 输出物化时凭它遍历 CSR，不在此复制边列表
        }
    return verdicts, details


# ----------------------------- 旧叶迁交见证 -----------------------------

def build_old_witnesses(old_tree, store, old_selected, verdict_code):
    """只针对被选旧叶；列出任何未能完整迁交的原因与见证（边走 CSR，不复制列表）。"""
    records = []
    old_off, old_pair, old_appr = store["old_off"], store["old_pair"], store["old_appr"]
    old_complete = store["old_complete"]
    for li in old_tree.leaf_indices_preorder():
        if not old_selected[li]:
            continue
        node_idx = old_tree.node_idx_for_leaf(li)
        edge_n = old_off[li + 1] - old_off[li]
        approved_targets = [ni for (ni, ap) in iter_csr(old_off, old_pair, old_appr, li) if ap]
        reasons = []
        if edge_n == 0:
            reasons.append("no_listed_destination")
        elif not approved_targets:
            reasons.append("no_approved_destination")
        if not old_complete[li]:
            reasons.append("destinations_incomplete")
        if any(verdict_code[ni] == 2 for ni in approved_targets):  # REVIEW
            reasons.append("points_to_review_leaf")

        fully_migrated = (
            bool(old_complete[li])
            and bool(approved_targets)
            and all(verdict_code[ni] == 1 for ni in approved_targets)  # INCLUDE
        )
        records.append({
            "old_leaf_idx": li,
            "old_leaf": old_tree.ids[node_idx],
            "old_name": old_tree.names[node_idx],
            "selected": True,
            "destinations_complete": bool(old_complete[li]),
            "fully_migrated": fully_migrated,
            "reasons": reasons,
            "_csr_leaf": li,
        })
    return records


# ----------------------------- 最大可整选子树 -----------------------------

def maximal_include_subtrees(new_tree, verdict_code):
    """
    自底向上（reverse preorder 保证子孙先算）求每节点子树状态：
      1 ALL   子树内全部叶子都是 INCLUDE
      0 NONE  子树内没有 INCLUDE 叶子
      2 MIXED 两者都有 -> 祖先不能整选
    再自顶向下收集：ALL 即截取为最大子树根，MIXED 继续下钻，NONE 剪枝。
    """
    ALL, NONE, MIXED = 1, 0, 2
    state = bytearray(len(new_tree.ids))
    include_cnt = array.array("q", [0]) * len(new_tree.ids)
    for nidx in reversed(new_tree.preorder):
        if new_tree.is_leaf[nidx]:
            li = new_tree.leaf_index[nidx]
            if verdict_code[li] == 1:
                state[nidx], include_cnt[nidx] = ALL, 1
            else:
                state[nidx], include_cnt[nidx] = NONE, 0
            continue
        total_cnt, all_all, all_none = 0, True, True
        c = new_tree.first_child[nidx]
        while c != -1:
            total_cnt += include_cnt[c]
            if state[c] != ALL:
                all_all = False
            if state[c] != NONE:
                all_none = False
            c = new_tree.next_sib[c]
        include_cnt[nidx] = total_cnt
        if all_all:
            state[nidx] = ALL
        elif all_none:
            state[nidx] = NONE
        else:
            state[nidx] = MIXED

    roots = []
    stack = [new_tree.root]
    while stack:
        nidx = stack.pop()
        if state[nidx] == ALL:
            roots.append(nidx)          # 截取：不再下钻，保证结果互不包含
        elif state[nidx] == MIXED:
            c = new_tree.first_child[nidx]
            while c != -1:
                stack.append(c)
                c = new_tree.next_sib[c]
        # NONE 剪枝
    return roots, include_cnt, state


def expand_subtree_roots(tree, roots):
    """展开子树根为确切叶索引集合（仅用于校验与报告）。"""
    root_set = set(roots)
    leaves = set()
    stack = [tree.root]
    while stack:
        nidx = stack.pop()
        if nidx in root_set:
            sub = [nidx]
            while sub:
                x = sub.pop()
                if tree.is_leaf[x]:
                    leaves.add(tree.leaf_index[x])
                c = tree.first_child[x]
                while c != -1:
                    sub.append(c)
                    c = tree.next_sib[c]
            continue
        c = tree.first_child[nidx]
        while c != -1:
            stack.append(c)
            c = tree.next_sib[c]
    return leaves


# ----------------------------- 对照片发布审计（audit 子命令） -----------------------------
#
# 与迁交流程的区别：不读老筛选，只审“旧树 + 新树 + 对照片”本身是否闭合到可发布。
#   * 每条已核准边把两端叶并入同一关系块（并查集，迭代 find，无递归）。
#   * 块内任一旧叶/新叶的完整性确认缺失（无记录）或明确 complete=false
#     （仍可能有未列来源/去向）-> 块未闭合；问题清单列出块内全部叶、核准边
#     与每个缺口的具体一侧原因。
#   * 无核准边且明确列全的孤立叶 = 确认无对应（闭合单叶块，不是问题）。
#   * 未核准边不进关系块，作为待处理边单独保留；
#     只要存在待处理边或未闭合块 -> 整体不可发布。
#
# 确定性：块号 = 按“树前序叶序”最小成员升序赋号；成员/缺口按叶序；
# 边按 (块, 旧叶, 新叶) 计数排序 —— 均只取决于树与边集合，与 JSONL 行序无关。
#
# 内存前提：仅 O(叶数) 结构（并查集、确认状态、块号、成员 CSR）。
# 边表两趟流式、落盘为 12 字节定长记录，三遍稳定计数排序后顺序读取；
# 重复边在 (旧叶, 新叶) 有序后相邻扫描发现，不在内存保存整张边表或关系块副本。

AUDIT_COMP_MISSING, AUDIT_COMP_FALSE, AUDIT_COMP_TRUE = 0, 1, 2
_EDGE_REC = struct.Struct("<iii")  # (排序键: 块号+1 / 未核准为 0, 旧叶序号, 新叶序号)


def _counting_sort_records(src, dst, nrec, keyoff, radix):
    """稳定计数排序定长记录文件：按 keyoff 处 '<i' 键（键域 [0, radix)）src -> dst。
    顺序读 src、按计算位写 dst；内存 O(radix)，与记录数无关。"""
    recsize = _EDGE_REC.size
    counts = array.array("q", [0]) * radix
    with open(src, "rb") as f:
        left = nrec
        while left > 0:
            buf = f.read(recsize * min(left, 65536))
            if not buf:
                fail("审计暂存文件损坏（记录数不足）")
            for off in range(0, len(buf), recsize):
                counts[struct.unpack_from("<i", buf, off + keyoff)[0]] += 1
            left -= len(buf) // recsize
    offs = array.array("q", [0]) * radix
    acc = 0
    for k in range(radix):
        offs[k] = acc
        acc += counts[k]
    cursor = array.array("q", offs)
    with open(src, "rb") as f, open(dst, "wb", buffering=1 << 20) as g:
        for _ in range(nrec):
            rec = f.read(recsize)
            if len(rec) != recsize:
                fail("审计暂存文件损坏（记录截断）")
            key = struct.unpack_from("<i", rec, keyoff)[0]
            pos = cursor[key]
            cursor[key] = pos + 1
            g.seek(pos * recsize)
            g.write(rec)


def audit_main(argv):
    ap = argparse.ArgumentParser(
        prog="migrate.py audit",
        description="对照片发布审计：不依赖老筛选，检查双向对照片本身是否闭合到可发布。")
    ap.add_argument("--old-tree", required=True)
    ap.add_argument("--new-tree", required=True)
    ap.add_argument("--edges", required=True)
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args(argv)

    os.makedirs(args.outdir, exist_ok=True)
    old_tree = TreeStore(args.old_tree)
    new_tree = TreeStore(args.new_tree)
    on_, nn = old_tree.leaf_n, new_tree.leaf_n
    total = on_ + nn

    # 每叶完整性确认状态：0 缺失 / 1 明确未列全 / 2 明确列全
    old_state = bytearray(on_)
    new_state = bytearray(nn)

    # 并查集：旧叶占 [0, on_)，新叶占 [on_, on_+nn)；迭代 find，树深/块规模无递归风险
    uf_parent = array.array("i", range(total))
    uf_size = array.array("i", [1]) * total

    def uf_find(x):
        while uf_parent[x] != x:
            uf_parent[x] = uf_parent[uf_parent[x]]
            x = uf_parent[x]
        return x

    approved_count = 0
    pending_count = 0

    # ---- 第一趟：校验 + 完整性状态 + 已核准边并块（边表只过一遍，不在内存留边）
    for rec in iter_jsonl(args.edges):
        rtype = rec.get("type", "edge")
        if rtype == "completeness":
            side = rec.get("side")
            if side not in ("new", "old"):
                fail("completeness 记录 side 非法: %r" % rec)
            if not isinstance(rec.get("complete"), bool):
                fail("completeness 记录 complete 必须为布尔值: %r" % rec)
            if side == "new":
                li = new_tree.require_leaf(rec.get("leaf"), "new")
                new_state[li] = AUDIT_COMP_TRUE if rec["complete"] else AUDIT_COMP_FALSE
            else:
                li = old_tree.require_leaf(rec.get("leaf"), "old")
                old_state[li] = AUDIT_COMP_TRUE if rec["complete"] else AUDIT_COMP_FALSE
            continue
        if rtype != "edge":
            fail("未知边表记录类型: %r" % rec)
        oid, nid = rec.get("old"), rec.get("new")
        approved = rec.get("approved")
        if oid is None or nid is None or not isinstance(approved, bool):
            fail("edge 记录字段不全/类型错: %r" % rec)
        oi = old_tree.require_leaf(oid, "old")
        ni = new_tree.require_leaf(nid, "new")
        if approved:
            ra, rb = uf_find(oi), uf_find(on_ + ni)
            if ra != rb:
                if uf_size[ra] < uf_size[rb]:
                    ra, rb = rb, ra
                uf_parent[rb] = ra
                uf_size[ra] += uf_size[rb]
            approved_count += 1
        else:
            pending_count += 1
    edge_rows = approved_count + pending_count

    # ---- 关系块编号：规范代表元 = 块内最小叶序号，按叶序升序赋号（与 JSONL 行序无关）
    minrep = array.array("i", [-1]) * total
    for i in range(total):
        r = uf_find(i)
        if minrep[r] < 0 or i < minrep[r]:
            minrep[r] = i
    block_of_leaf = array.array("i", [-1]) * total
    block_of_rep = array.array("i", [-1]) * total
    nb = 0
    for i in range(total):
        c = minrep[uf_find(i)]
        b = block_of_rep[c]
        if b < 0:
            b = nb
            nb += 1
            block_of_rep[c] = b
        block_of_leaf[i] = b
    del minrep, block_of_rep, uf_parent, uf_size

    # ---- 闭合性：块内任一叶未明确列全 -> 未闭合；统计每块缺口数
    closed = bytearray([1]) * nb
    gap_cnt = array.array("i", [0]) * nb
    for i in range(total):
        st = old_state[i] if i < on_ else new_state[i - on_]
        if st != AUDIT_COMP_TRUE:
            b = block_of_leaf[i]
            closed[b] = 0
            gap_cnt[b] += 1

    # ---- 块成员 CSR（每叶一条，全局仅一份；输出未闭合块时按块切片引用，不复制）
    member_off = array.array("q", [0]) * (nb + 1)
    for i in range(total):
        member_off[block_of_leaf[i] + 1] += 1
    for b in range(nb):
        member_off[b + 1] += member_off[b]
    members = array.array("i", [0]) * total
    cur = array.array("q", member_off[:-1])
    for i in range(total):
        b = block_of_leaf[i]
        members[cur[b]] = i
        cur[b] += 1
    del cur

    # ---- 第二趟：边表落盘为定长记录（排序键 = 块号+1，未核准边为 0）
    scratch_a = os.path.join(args.outdir, ".audit_edges_a.bin")
    scratch_b = os.path.join(args.outdir, ".audit_edges_b.bin")
    key_cnt = array.array("q", [0]) * (nb + 1)
    edge_total = 0
    seen_approved = 0
    with open(scratch_a, "wb") as g:
        for rec in iter_jsonl(args.edges):
            if rec.get("type", "edge") != "edge":
                continue
            oi = old_tree.require_leaf(rec.get("old"), "old")
            ni = new_tree.require_leaf(rec.get("new"), "new")
            if rec.get("approved"):
                seen_approved += 1
                key = block_of_leaf[oi] + 1
            else:
                key = 0
            key_cnt[key] += 1
            g.write(_EDGE_REC.pack(key, oi, ni))
            edge_total += 1
    if seen_approved != approved_count or edge_total != edge_rows:
        fail("对照片在审计过程中被修改，请保持输入文件不变")

    # ---- 三遍稳定计数排序：先 (新叶, 旧叶) 使重复边相邻以便检测，再按块键分组。
    #      最终顺序：待处理边(键0, 按旧/新叶序) + 各块(键=块号+1, 块内按旧/新叶序)。
    _counting_sort_records(scratch_a, scratch_b, edge_total, 8, nn)   # 按新叶序号
    _counting_sort_records(scratch_b, scratch_a, edge_total, 4, on_)  # 按旧叶序号
    prev = None
    with open(scratch_a, "rb") as f:
        for _ in range(edge_total):
            rec = _EDGE_REC.unpack(f.read(_EDGE_REC.size))
            oi, ni = rec[1], rec[2]
            if prev is not None and oi == prev[0] and ni == prev[1]:
                fail("旧叶 %s 与新叶 %s 之间存在重复边记录" % (
                    old_tree.ids[old_tree.node_idx_for_leaf(oi)],
                    new_tree.ids[new_tree.node_idx_for_leaf(ni)]))
            prev = (oi, ni)
    _counting_sort_records(scratch_a, scratch_b, edge_total, 0, nb + 1)  # 按块键

    # ---- 输出：待处理边 + 逐未闭合块问题清单（顺序流式读最终文件，边不进内存）
    width = max(4, len(str(nb)))

    def block_label(b):
        return "BLK-%0*d" % (width, b + 1)

    issues_path = os.path.join(args.outdir, "publish_block_issues.jsonl")
    pending_path = os.path.join(args.outdir, "publish_pending_edges.jsonl")
    console_blocks, console_pending = [], []
    unclosed_count = 0
    with open(scratch_b, "rb") as ef, \
         open(pending_path, "w", encoding="utf-8") as pend, \
         open(issues_path, "w", encoding="utf-8") as iss:
        for _ in range(key_cnt[0]):
            rec = _EDGE_REC.unpack(ef.read(_EDGE_REC.size))
            onode = old_tree.node_idx_for_leaf(rec[1])
            nnode = new_tree.node_idx_for_leaf(rec[2])
            oid, nid = old_tree.ids[onode], new_tree.ids[nnode]
            pend.write(json.dumps({
                "kind": "pending_edge",
                "problem": "unapproved_edge",
                "problem_cn": "未核准边",
                "old": oid, "old_name": old_tree.names[onode],
                "new": nid, "new_name": new_tree.names[nnode],
            }, ensure_ascii=False) + "\n")
            if len(console_pending) < 50:
                console_pending.append("%s -> %s" % (oid, nid))
        for b in range(nb):
            ecnt = key_cnt[b + 1]
            if closed[b]:
                ef.seek(ecnt * _EDGE_REC.size, os.SEEK_CUR)   # 闭合块只计数，跳过
                continue
            unclosed_count += 1
            lo, hi = member_off[b], member_off[b + 1]
            old_leaves, new_leaves, gaps = [], [], []
            for p in range(lo, hi):
                leaf = members[p]
                if leaf < on_:
                    node = old_tree.node_idx_for_leaf(leaf)
                    old_leaves.append({"node_id": old_tree.ids[node],
                                       "name": old_tree.names[node]})
                    st, side, ids, names = old_state[leaf], "old", old_tree.ids, old_tree.names
                else:
                    node = new_tree.node_idx_for_leaf(leaf - on_)
                    new_leaves.append({"node_id": new_tree.ids[node],
                                       "name": new_tree.names[node]})
                    st, side, ids, names = new_state[leaf - on_], "new", new_tree.ids, new_tree.names
                if st != AUDIT_COMP_TRUE:
                    gaps.append({
                        "side": side,
                        "node_id": ids[node],
                        "name": names[node],
                        "problem": ("missing_confirmation" if st == AUDIT_COMP_MISSING
                                    else "explicitly_incomplete"),
                        "problem_cn": ("完整性确认缺失" if st == AUDIT_COMP_MISSING
                                       else "明确未列全（仍可能有未列来源/去向）"),
                    })
            iss.write('{"kind":"unclosed_block","block_id":%s,"gap_count":%d,'
                      '"old_leaves":%s,"new_leaves":%s,"approved_edges":['
                      % (json.dumps(block_label(b)), len(gaps),
                         json.dumps(old_leaves, ensure_ascii=False),
                         json.dumps(new_leaves, ensure_ascii=False)))
            for k in range(ecnt):
                rec = _EDGE_REC.unpack(ef.read(_EDGE_REC.size))
                onode = old_tree.node_idx_for_leaf(rec[1])
                nnode = new_tree.node_idx_for_leaf(rec[2])
                iss.write(("" if k == 0 else ",") + json.dumps(
                    {"old": old_tree.ids[onode], "new": new_tree.ids[nnode]},
                    ensure_ascii=False))
            iss.write('],"gaps":%s}\n' % json.dumps(gaps, ensure_ascii=False))
            if len(console_blocks) < 50:
                console_blocks.append("%s 旧叶%s 新叶%s 缺口[%s]" % (
                    block_label(b),
                    [d["node_id"] for d in old_leaves],
                    [d["node_id"] for d in new_leaves],
                    "; ".join("%s:%s %s" % (g["side"], g["node_id"], g["problem_cn"])
                              for g in gaps)))
        if ef.read(1):
            fail("内部不变量被破坏：边记录未消费完")

    closed_count = nb - unclosed_count
    publishable = (unclosed_count == 0 and pending_count == 0)
    summary = {
        "schema": 1,
        "audit": "mapping_sheet_publication",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "publishable": publishable,
        "conclusion": "publishable" if publishable else "not_publishable",
        "conclusion_cn": "可发布" if publishable else "不可发布",
        "block_count": nb,
        "closed_block_count": closed_count,
        "unclosed_block_count": unclosed_count,
        "pending_edge_count": pending_count,
        "approved_edge_count": approved_count,
        "gap_count": sum(gap_cnt),
        "old_leaf_count": on_,
        "new_leaf_count": nn,
        "issues_file": "publish_block_issues.jsonl",
        "pending_edges_file": "publish_pending_edges.jsonl",
    }
    with open(os.path.join(args.outdir, "publish_audit_summary.json"), "w",
              encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    for p in (scratch_a, scratch_b):
        try:
            os.remove(p)
        except OSError:
            pass

    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print("=" * 68)
    print("对照片发布审计完成。结论：%s。" % summary["conclusion_cn"])
    print("关系块共 %d 个：闭合 %d，未闭合 %d；待处理未核准边 %d 条；缺口 %d 处。"
          % (nb, closed_count, unclosed_count, pending_count, summary["gap_count"]))
    if console_blocks:
        print("-" * 68)
        print("未闭合关系块（详见 %s）：" % issues_path)
        for line in console_blocks:
            print("    * %s" % line)
        if unclosed_count > len(console_blocks):
            print("    … 其余 %d 个见问题清单文件" % (unclosed_count - len(console_blocks)))
    if console_pending:
        print("-" * 68)
        print("待处理未核准边（详见 %s）：" % pending_path)
        for line in console_pending:
            print("    * %s" % line)
        if pending_count > len(console_pending):
            print("    … 其余 %d 条见待处理边文件" % (pending_count - len(console_pending)))
    print("-" * 68)
    print("本进程峰值 RSS 约 %.1f MB（O(叶数) 结构；边表落盘排序，不进内存）。"
          % (peak_rss_kb / 1024))
    print("审计产物位于: %s" % args.outdir)


# ----------------------------- 主流程 -----------------------------

def migrate_main(argv):
    ap = argparse.ArgumentParser(
        description="离线分类筛选迁交工具（对照片发布审计请用 audit 子命令）")
    ap.add_argument("--old-tree", required=True)
    ap.add_argument("--new-tree", required=True)
    ap.add_argument("--old-filter", required=True)
    ap.add_argument("--edges", required=True)
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args(argv)

    os.makedirs(args.outdir, exist_ok=True)
    evidence_path = os.path.join(args.outdir, "evidence.jsonl")
    scratch_path = os.path.join(args.outdir, ".edge_scratch.jsonl")

    old_tree = TreeStore(args.old_tree)
    new_tree = TreeStore(args.new_tree)
    marks = load_filter(args.old_filter, old_tree, "旧")
    old_selected = resolve_effective_selection(old_tree, marks)

    with open(evidence_path, "w", encoding="utf-8") as ev_fh, \
         open(scratch_path, "w", encoding="utf-8") as scratch_fh:
        store = ingest_edges(args.edges, old_tree, new_tree, ev_fh, scratch_fh)
        verdict_code, new_details = classify_new_leaves(
            new_tree, store, old_selected)
        old_records = build_old_witnesses(
            old_tree, store, old_selected, verdict_code)

        new_off, new_pair, new_appr = store["new_off"], store["new_pair"], store["new_appr"]
        old_off, old_pair, old_appr = store["old_off"], store["old_pair"], store["old_appr"]

        # 输出物化：把紧凑索引翻译回稳定 id / 名称，凭证双向各一份
        verdict_name = {1: INCLUDE, 2: REVIEW, 3: EXCLUDE}
        for rec in old_records:
            li = rec.pop("_csr_leaf")
            rec.pop("old_leaf_idx", None)
            edges_out = []
            for ni, ap in iter_csr(old_off, old_pair, old_appr, li):
                nnode = new_tree.node_idx_for_leaf(ni)
                v = verdict_name[verdict_code[ni]]
                edges_out.append({
                    "new_leaf": new_tree.ids[nnode],
                    "new_name": new_tree.names[nnode],
                    "approved": bool(ap),
                    "new_verdict": v,
                    "new_verdict_cn": VERDICT_CN[v],
                })
            rec["edges"] = edges_out
            ev_fh.write(json.dumps(
                {"kind": "by_old_leaf", **rec}, ensure_ascii=False) + "\n")

        for li in new_tree.leaf_indices_preorder():
            d = new_details[li]
            d.pop("_csr_leaf", None)
            sources_out = []
            for oi, ap in iter_csr(new_off, new_pair, new_appr, li):
                onode = old_tree.node_idx_for_leaf(oi)
                sources_out.append({
                    "old_leaf": old_tree.ids[onode],
                    "old_selected": bool(old_selected[oi]),
                    "approved": bool(ap),
                })
            d["sources"] = sources_out
            ev_fh.write(json.dumps(
                {"kind": "by_new_leaf", **d}, ensure_ascii=False) + "\n")

    unapproved = store["unapproved"]

    # 最大可整选子树 + 不变量校验
    roots_idx, include_cnt, _ = maximal_include_subtrees(new_tree, verdict_code)
    include_leaves = {li for li in range(new_tree.leaf_n) if verdict_code[li] == 1}
    expanded = expand_subtree_roots(new_tree, roots_idx)
    if expanded != include_leaves:
        fail("内部不变量被破坏：子树展开集合 != 确定包含叶集合")
    if sum(include_cnt[r] for r in roots_idx) != len(include_leaves):
        fail("内部不变量被破坏：子树叶计数之和 != 确定包含叶数")

    # ---- 可执行筛选 ----
    filter_doc = {
        "schema": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "semantics": "include_whole_subtrees_only",
        "subtree_roots": [
            {"node_id": new_tree.ids[r], "name": new_tree.names[r],
             "include_leaf_count": include_cnt[r]}
            for r in roots_idx
        ],
        "expanded_include_leaves": sorted(
            new_tree.ids[new_tree.node_idx_for_leaf(li)] for li in include_leaves),
        "include_leaf_count": len(include_leaves),
    }
    with open(os.path.join(args.outdir, "new_filter.json"), "w", encoding="utf-8") as fh:
        json.dump(filter_doc, fh, ensure_ascii=False, indent=2)

    # ---- 新叶判定明细 ----
    with open(os.path.join(args.outdir, "new_leaf_verdicts.jsonl"), "w", encoding="utf-8") as fh:
        for li in new_tree.leaf_indices_preorder():
            fh.write(json.dumps(new_details[li], ensure_ascii=False) + "\n")

    partial = [r for r in old_records if not r["fully_migrated"]]
    with open(os.path.join(args.outdir, "old_selected_witnesses.jsonl"), "w", encoding="utf-8") as fh:
        for rec in old_records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    with open(os.path.join(args.outdir, "unapproved_edges.jsonl"), "w", encoding="utf-8") as fh:
        for (oi, ni) in unapproved:
            fh.write(json.dumps({
                "old_leaf": old_tree.ids[old_tree.node_idx_for_leaf(oi)],
                "new_leaf": new_tree.ids[new_tree.node_idx_for_leaf(ni)],
            }, ensure_ascii=False) + "\n")

    cnt = lambda code: sum(1 for x in verdict_code if x == code)
    summary = {
        "old_leaf_count": old_tree.leaf_n,
        "old_selected_leaf_count": sum(old_selected),
        "new_leaf_count": new_tree.leaf_n,
        "new_include_leaf_count": cnt(1),
        "new_review_leaf_count": cnt(2),
        "new_exclude_leaf_count": cnt(3),
        "new_no_historical_source_count": sum(
            1 for d in new_details if d["no_historical_source"]),
        "executable_subtree_root_count": len(roots_idx),
        "partial_or_unmigrated_old_leaf_count": len(partial),
        "unapproved_edge_count": len(unapproved),
        "duplicate_name_nodes": new_tree.dup_names,
    }
    with open(os.path.join(args.outdir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    # 暂存边文件仅用于物化阶段，产物齐全后删除，不留在交接目录
    try:
        os.remove(scratch_path)
    except OSError:
        pass

    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print("-" * 68)
    print("本进程峰值 RSS 约 %.1f MB（紧凑数组存储；旧/新树节点共 %d，边 %d）。"
          % (peak_rss_kb / 1024,
             len(old_tree.ids) + len(new_tree.ids), store["edge_count"]))
    # ---- 控制台人读摘要 ----
    print("=" * 68)
    print("迁交完成。可执行新筛选（仅包含整棵子树）共 %d 个最大子树根：" % len(roots_idx))
    for r in roots_idx:
        print("  - 子树根 %s(%s)，内含确定包含叶 %d 片"
              % (new_tree.ids[r], new_tree.names[r], include_cnt[r]))
    print("展开后恰好覆盖确定包含新叶 %d 片（不变量已校验）。" % len(include_leaves))
    print("-" * 68)
    print("新叶判定：确定包含 %d，待复核 %d，确定排除 %d（其中无历史来源 %d）。"
          % (cnt(1), cnt(2), cnt(3), summary["new_no_historical_source_count"]))
    review_rows = [d for d in new_details if d["verdict"] == REVIEW]
    if review_rows:
        print("  待复核新叶（不得进入自动筛选）：")
        for d in review_rows:
            print("    * %s(%s) 原因=%s" % (d["new_leaf"], d["new_name"], ",".join(d["reasons"])))
    print("-" * 68)
    print("被选旧叶中未完整迁交 %d 片（部分迁交不得报成等价）：" % len(partial))
    for rec in partial:
        print("    * %s(%s) 原因=%s 去向列全=%s"
              % (rec["old_leaf"], rec["old_name"],
                 ",".join(rec["reasons"]) or "(无)", rec["destinations_complete"]))
    if unapproved:
        print("-" * 68)
        print("未核准边 %d 条（不参与自动判定，仅留见证）：" % len(unapproved))
        for (oi, ni) in unapproved:
            print("    * %s -> %s"
                  % (old_tree.ids[old_tree.node_idx_for_leaf(oi)],
                     new_tree.ids[new_tree.node_idx_for_leaf(ni)]))
    if new_tree.dup_names:
        print("-" * 68)
        print("提示：新树存在同名节点，系统按稳定 ID 区分、未做任何归并：")
        for nm, idl in new_tree.dup_names.items():
            print("    名称 %r -> 节点 %s" % (nm, ", ".join(idl)))
    print("=" * 68)
    print("全部产物位于: %s" % args.outdir)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "audit":
        audit_main(argv[1:])
        return
    migrate_main(argv)


if __name__ == "__main__":
    main()
