# 离线分类筛选迁交工具

把一套“老分类树 + 老筛选 + 新旧叶双向对照片”离线迁成交接新系统的
**可执行新筛选**与**完整迁交凭证**。纯 Python 标准库，建议一律用 `python3 -I` 运行
（隔离当前目录/`PYTHONPATH`，防止数据目录里被植入的同名模块被 import）。

## 1. 它解决的问题

- 老报表的取数范围是“勾选父类、再挖掉部分子类”，叶子按**根→叶路径上最深的标记**决定取舍。
- 新树里有的老末级被拆开（一对多），有的新末级混合了老范围内外的货物（多对一）。
- 对照片带双向“列全/未列全”确认；允许没有边（= 在另一版确实无对应，不等于没做完）。
- 新系统**只接受“包含这些整棵子树”，不支持排除语句**。
- 要求：清楚的范围直接迁；混合类不许因名字相近就假装完全迁移；所有保留项与未迁部分都要可追查。

## 2. 判定规则（与交接约定逐条对应）

老叶先求“生效选择”：沿根→叶路径取最深的 `include/exclude`，无标记默认不选。

对每片**新叶**，只统计已核准（`approved=true`）的来源边：

| 条件 | 判定 | 能否进自动筛选 |
|---|---|---|
| 来源未列全（`complete=false`） | 待复核 REVIEW | 否 |
| 来源列全，且已核准来源全部被选（至少 1 条） | 确定包含 INCLUDE | 是 |
| 来源列全，且无任何被选来源 | 确定排除 EXCLUDE | 否（无边时另标“无历史来源”） |
| 来源列全，但被选/不选来源两边都有 | 待复核 REVIEW | 否 |

未核准边不参与判定，只写入见证。待复核一律不进自动筛选。

对每片**被选旧叶**，单独给出未完整迁交的原因与见证：

- `no_listed_destination`：对照片里没有任何去向边；
- `no_approved_destination`：有边但全部未核准；
- `destinations_incomplete`：旧叶的新去向未列全；
- `points_to_review_leaf`：有已核准去向落在待复核新叶上。

部分迁交的旧叶 `fully_migrated=false`，不会被报成等价迁移。

## 3. 可执行新筛选（最大子树）

新系统只支持整棵子树包含。工具自底向上给每个节点标
`ALL（子树全是 INCLUDE 叶）/ NONE（无 INCLUDE 叶）/ MIXED（混合）`，
再自顶向下截取：遇到 `ALL` 就作为最大子树根并停止下钻，`MIXED` 继续下钻，`NONE` 剪枝。

因此结果保证：

- 各子树根**互不包含**；
- 子树展开后的叶集合**恰好等于全部 INCLUDE 新叶**（程序内做了不变量断言）；
- 只要某祖先下有一片非确定包含叶，该祖先就不会被整选。

## 4. 输入格式

树（整份 JSON 或每行一条的 JSONL；JSONL 更省内存，两趟流式读取）：

```json
{"root": "R",
 "nodes": {"R": {"parent": null, "leaf": false, "name": "根"},
           "A": {"parent": "R",  "leaf": true,  "name": "叶A"}}}
```

老筛选：

```json
{"marks": {"O2": "include", "O4": "exclude"}}
```

对照片（JSONL，逐行流式，边与完整性确认两类记录）：

```json
{"type": "edge", "old": "O9", "new": "N9a", "approved": true}
{"type": "completeness", "side": "new", "leaf": "N9a", "complete": true}
{"type": "completeness", "side": "old", "leaf": "O9",  "complete": true}
```

所有边两端、所有完整性确认对象都必须存在且是叶子，否则以退出码 2 拒绝。
同名不同 ID 的节点只提示、**绝不归并**。

## 5. 运行

```bash
python3 -I migrate.py \
  --old-tree  /path/old_tree.jsonl \
  --new-tree  /path/new_tree.jsonl \
  --old-filter /path/old_filter.json \
  --edges     /path/edges.jsonl \
  --outdir    /path/out
```

## 6. 输出产物（全部在 outdir）

| 文件 | 内容 |
|---|---|
| `new_filter.json` | **可执行新筛选**：最大子树根列表 + 展开后的确定包含叶（机器可读） |
| `new_leaf_verdicts.jsonl` | 每片新叶的判定、原因、来源边与各来源选中/核准状态 |
| `old_selected_witnesses.jsonl` | 每个被选旧叶的去向、是否完整迁交、未迁原因与见证 |
| `evidence.jsonl` | 双向凭证：`edge_witness`（边）、`by_new_leaf`（按新叶追查老选择/边）、`by_old_leaf`（按旧叶追查未迁部分） |
| `unapproved_edges.jsonl` | 未核准边，仅留见证 |
| `summary.json` | 计数汇总、同名节点提示 |

追查路径：

- 按**新叶**：`evidence.jsonl` 里 `kind=by_new_leaf` → 看到影响它的每条老来源、
  老来源是否被选、边是否核准、完整性确认与最终判定原因。
- 按**被选旧叶**：`kind=by_old_leaf` → 看到全部去向、各去向新叶判定、未迁原因。

## 7. 规模与内存设计

- 全程**显式栈迭代，无递归**，树深无业务上限（压力测试 10 万层深链通过）。
- DFS 栈帧只携带一个“当前生效标记”，**不逐叶复制祖先路径**。
- 节点转紧凑整数索引：父/首子/右兄弟用 `array('i')`（邻接信息固定 12 字节/节点），
  叶标记用 `bytearray`，名称驻留（重名只存一份）。
- 边表**只过一遍**：内存用 CSR 定长数组（偏移 + 对端索引 + 核准位）聚合，
  边见证即时落盘；不为每条边回扫整棵树。树的遍历趟数为常数，与边数无关。
- 节点字符串 ID → 整数索引的字典是纯 Python 下的主要残余内存；
  所有访问都集中在 `TreeStore` 接口后，超内存场景可把该接口换成外存/KV 支撑，上层算法不变。
- 运行结束打印本进程峰值 RSS，便于容量评估。

## 8. 演示与自检

```bash
python3 -I make_demo_data.py   # 生成覆盖全部场景的合成树/筛选/对照片
python3 -I migrate.py --old-tree /workspace/demo_data/old_tree.json \
        --new-tree /workspace/demo_data/new_tree.json \
        --old-filter /workspace/demo_data/old_filter.json \
        --edges /workspace/demo_data/edges.jsonl --outdir /workspace/demo_out
python3 -I verify_demo.py      # 独立第二套实现重算并断言全部规则（42 项）
python3 -I test_errors.py      # 非法输入必须以退出码 2 拒绝
python3 -I stress_test.py      # 10 万层深链 / 5 万叶宽树，验证无递归与线性内存
```

合成演示覆盖：内部挖除（最深标记胜）、一对多拆分、多对一混合、来源未列全、
确认无历史来源、未核准边、同名不归并、深链整选、兄弟取舍不同导致的最大子树拦阻。

## 9. 对照片发布审计（audit 子命令，不依赖老筛选）

交接前，数据治理员需要先确认**双向对照片本身**能否作为可发布的来源。
迁交流程只在给定老筛选时判断哪些新叶可交付，发现不了关系表中影响所有报表的
闭合缺口；审计入口只读旧树、新树与 JSONL 对照片，不读老筛选：

```bash
python3 -I migrate.py audit \
  --old-tree /path/old_tree.jsonl \
  --new-tree /path/new_tree.jsonl \
  --edges    /path/edges.jsonl \
  --outdir   /path/audit_out
```

### 判定规则

- 每条**已核准**边把两端叶并入同一个**关系块**（传递闭包，并查集）。
- 块内任一旧叶/新叶的完整性确认**缺失**（无记录）或**明确未列全**
  （`complete=false`，仍可能有未列来源/去向）→ 该关系块**未闭合**；
  问题清单列出块内全部叶、核准边与每个缺口的具体一侧原因。
- 两端都明确列全的叶才能让关系块闭合；**无核准边且明确列全的孤立叶**
  表示“确认无对应”，是闭合单叶块，不是问题。
- **未核准边不进关系块**，作为待处理边单独保留；
  只要存在待处理边或未闭合关系块，整体结论即为**不可发布**。

### 审计产物（全部在 outdir）

| 文件 | 内容 |
|---|---|
| `publish_audit_summary.json` | 机器可读摘要：可发布结论、闭合/未闭合块计数、待处理边计数、缺口数 |
| `publish_block_issues.jsonl` | 逐未闭合关系块问题清单：稳定块号 `BLK-*`、块内全部叶、核准边、每个缺口的一侧原因（`missing_confirmation` 缺失确认 / `explicitly_incomplete` 明确未列全） |
| `publish_pending_edges.jsonl` | 待处理未核准边（`unapproved_edge`），单独保留 |

无问题的闭合块只在摘要中计数，不进入问题清单。

### 确定性与规模

- 块号按“树前序叶序”最小成员升序赋号，成员/缺口按叶序，边按
  （块， 旧叶， 新叶） 排序：重复运行同一输入时，块标识、成员顺序、原因和计数
  **不受 JSONL 行顺序影响**。
- 内存只保留 O（叶数） 结构（并查集、确认状态、块号、成员 CSR）；边表两趟流式、
  落盘为 12 字节定长记录，三遍稳定计数排序后顺序读取——不把整张边表或
  关系块副本作为内存前提（10 万边实测峰值 RSS 约 97 MB）。
- 重复边在 （旧叶， 新叶） 有序后相邻扫描发现；引用不存在/非叶节点、重复关系、
  非法完整性记录等仍以退出码 2 拒绝，错误语义与迁交流程一致。

### 审计演示与自检

```bash
python3 -I make_audit_demo_data.py   # 合成：闭合关系 / 单侧缺确认未闭合块 / 未核准边
python3 -I migrate.py audit --old-tree /workspace/audit_demo_data/old_tree.json \
        --new-tree /workspace/audit_demo_data/new_tree.json \
        --edges /workspace/audit_demo_data/edges.jsonl --outdir /workspace/audit_demo_out
python3 -I test_audit.py             # 独立第二套实现重算对照 + 行序无关 + 错误拒绝 + 规模冒烟
```

## 10. 可持续查询的货品迁移账本（ledger 子命令）

`ledger.py`（也可通过 `migrate.py ledger ...` 调用）在离线对照工具旁增加一个
可反复重开的 SQLite 账本。它接收一次**完整货品 JSONL**、旧/新树和第 2 节同格式
的双向叶子对照，之后把关系声明和人工分流修订持久化，并只重算受影响货品。

### 10.1 初始化

```bash
python3 -I /workspace/migrate_tool/ledger.py init \
  --ledger /path/to/ledger_dir \
  --items  /path/to/items.jsonl \
  --old-tree /path/to/old_tree.json \
  --new-tree /path/to/new_tree.json \
  --mapping  /path/to/edges.jsonl
```

默认货品字段为 `item_id` 与 `old_leaf`，可用 `--item-id-field` /
`--old-leaf-field` 改名。货品的全部原始 JSON 字段原样保存在 `raw_json`，导出时
合并到结果行的 `_migration_ledger` 元数据对象中；若原货品恰好有同名字段，账本会
改用不覆盖原字段的 `_migration_ledger_2` 等键名。

初始化在一个 SQLite 事务中流式写货品。损坏 JSON、非对象行、重复 `item_id`、
不存在的旧叶/新叶引用或重复对照边都会使**整次初始化回滚**，正式的
`ledger.sqlite3` 不会出现；已存在的账本也不会被覆盖。

### 10.2 当前判定规则

账本只按稳定叶 ID 判断，不按名称猜：

| 旧叶声明/分流状态 | 当前结果 | 原因码 |
|---|---|---|
| 旧叶去向列全，恰有 1 条去向且已核准 | 自动迁入该新叶 | — |
| 去向列全且全部核准，但有多个去向；逐件分流当前指向其中一个核准去向 | 迁入分流目标 | — |
| 多个核准去向，缺少有效逐件分流 | 待处理 | `manual_routing_required` |
| 撤销了多去向下的分流 | 待处理 | `manual_routing_revoked` |
| 旧叶完整性缺失或为 `false` | 待处理，保留原件 | `old_destinations_incomplete` |
| 没有任何去向边 | 待处理 | `no_destination` |
| 当前声明含 `approved=false` 的边 | 待处理 | `unapproved_relation` |
| 有效状态的人工分流目标已被关系替换移除 | 待处理 | `manual_route_target_absent` / `invalid_manual_routing` |
| 分流目标边当前未核准 | 待处理 | `manual_route_target_unapproved` / `invalid_manual_routing` |

关系或分流每次都按**当前状态**重新判定。某个分流过去曾核准/曾成功迁入，并不给它
永久豁免；核准撤回或目标被替换后，对应货品会进入下一次修订的净变化。

### 10.3 修订请求与幂等编号

按旧叶**完整替换**去向声明（`destinations` 是该旧叶此刻的完整列表；
`complete=false` 时列表可以是未列全的已知部分）：

```json
{
  "revision_id": "REV-0003",
  "kind": "old_leaf_destinations",
  "old_leaf": "O9",
  "complete": true,
  "destinations": [
    {"new_leaf": "N9a", "approved": true},
    {"new_leaf": "N9b", "approved": false}
  ]
}
```

按 `item_id` 替换人工分流：

```json
{
  "revision_id": "REV-0004",
  "kind": "manual_routing",
  "item_id": "ITEM-1001",
  "action": "replace",
  "new_leaf": "N9a"
}
```

撤销人工分流：

```json
{
  "revision_id": "REV-0005",
  "kind": "manual_routing",
  "item_id": "ITEM-1001",
  "action": "revoke"
}
```

提交：

```bash
python3 -I /workspace/migrate_tool/ledger.py revise \
  --ledger /path/to/ledger_dir \
  --request /path/to/rev.json \
  --diff-out /path/to/rev_delta.jsonl \
  --receipt-out /path/to/rev_receipt.json
```

- `revision_id` 是修订号；账本保存规范化请求 JSON 与 SHA-256。
- **同号 + 同内容**重送：返回 `"idempotent_replay": true`，不再写新变化行。
- **同号 + 不同内容**：退出码 2 拒绝，原修订不变。
- 坏 `item_id` / 旧叶 / 新叶引用、损坏请求在事务前或事务中拒绝并回滚。
- 每次修订是一个 SQLite 事务；进程中断后重开只能看到完整旧版或完整新版。

### 10.4 查询、摘要与一次修订的净变化

```bash
# 当前全量结果：migrated_items.jsonl、pending_items.jsonl、summary.json
python3 -I /workspace/migrate_tool/ledger.py export \
  --ledger /path/to/ledger_dir --outdir /path/to/current_out

# 摘要到 stdout 或原子写入 JSON
python3 -I /workspace/migrate_tool/ledger.py summary \
  --ledger /path/to/ledger_dir --out summary.json

# 按状态/原因/旧叶/item 查询
python3 -I /workspace/migrate_tool/ledger.py query \
  --ledger /path/to/ledger_dir --status pending \
  --reason unapproved_relation --out pending_unapproved.jsonl

# 以后再次查询某次修订的前后净变化
python3 -I /workspace/migrate_tool/ledger.py diff \
  --ledger /path/to/ledger_dir --revision-id REV-0003 --out rev3_delta.jsonl

# 列修订 / 看单个货品的当前判定、分流史和变化史
python3 -I /workspace/migrate_tool/ledger.py revisions --ledger /path/to/ledger_dir
python3 -I /workspace/migrate_tool/ledger.py show \
  --ledger /path/to/ledger_dir --item-id ITEM-1001
```

净变化文件每行包含 `item_id`、`change_kind`、`before`、`after` 和完整原始
`item`。只有交付签名（`status/new_leaf/reasons`）变化的货品才写入
`revision_change`；状态、目标和原因码均未变的货品计入
`unchanged_item_count`，但**不进入净变化文件**。

### 10.5 大规模与增量路径

- 初始化只顺序读一次完整货品 JSONL，批量写 SQLite；不需要把所有货品放进内存。
- `item(old_leaf, seq)` 与 `relation(old_leaf,new_leaf)` 是索引/主键。
- 旧叶声明修订只读写该旧叶的关系、完整性行和该旧叶下的货品；人工分流只处理一个
  `item_id`。日常修订命令不接收也不重读完整货品文件或整份对照文件。
- SQLite B-tree 承担货品量和修订变化史；只读导出使用单个快照事务，输出文件经
  同目录临时文件 + `fsync` + `rename` 原子替换。
- 增量冒烟可运行 `python3 -I stress_ledger.py --items 100000 --old-leaves 200`。
  参考环境中初始化 10 万件约 9 秒，单旧叶修订约 0.2 秒（只处理该叶 500 件），
  子进程峰值 RSS 约 23 MB；调大 `--items` 可验证流式摄入。

### 10.6 合成演示：核准撤回、分流失效、失败重试

一键生成数据并跑完所有场景：

```bash
python3 -I /workspace/migrate_tool/make_ledger_demo_data.py \
  /workspace/ledger_demo_data

rm -rf /workspace/ledger_demo /workspace/ledger_demo_out
python3 -I /workspace/migrate_tool/ledger.py init \
  --ledger /workspace/ledger_demo \
  --items /workspace/ledger_demo_data/items.jsonl \
  --old-tree /workspace/ledger_demo_data/old_tree.json \
  --new-tree /workspace/ledger_demo_data/new_tree.json \
  --mapping /workspace/ledger_demo_data/mapping.jsonl

# REV-001/002：先给多去向货品逐件分流
for f in /workspace/ledger_demo_data/revisions/rev_00{1,2}_*.json; do
  python3 -I /workspace/migrate_tool/ledger.py revise \
    --ledger /workspace/ledger_demo --request "$f"
done

# REV-003：撤回 O_WITHDRAW -> N_WD 的核准；2 件已自动迁入货品变待处理
python3 -I /workspace/migrate_tool/ledger.py revise \
  --ledger /workspace/ledger_demo \
  --request /workspace/ledger_demo_data/revisions/rev_003_withdraw_approval.json \
  --diff-out /workspace/ledger_demo_out/rev_003_diff.jsonl

# REV-004：O_ROUTE 移除 N_R1；旧分流目标消失，1 件变待处理，
# 另 1 件前后都是 manual_routing_required，不进入净变化。
python3 -I /workspace/migrate_tool/ledger.py revise \
  --ledger /workspace/ledger_demo \
  --request /workspace/ledger_demo_data/revisions/rev_004_replace_route_relation.json \
  --diff-out /workspace/ledger_demo_out/rev_004_diff.jsonl

# REV-005/006：把失效分流改到 N_R3；随后撤销 ITEM-MULTI-1 的分流。
for f in /workspace/ledger_demo_data/revisions/rev_005_*.json \
         /workspace/ledger_demo_data/revisions/rev_006_*.json; do
  python3 -I /workspace/migrate_tool/ledger.py revise \
    --ledger /workspace/ledger_demo --request "$f"
done

# 失败重试/幂等：同号同内容 no-op；同号改目标退出码 2；坏 item 引用退出码 2。
python3 -I /workspace/migrate_tool/ledger.py revise \
  --ledger /workspace/ledger_demo \
  --request /workspace/ledger_demo_data/revisions/rev_001_route_multi1.json
python3 -I /workspace/migrate_tool/ledger.py revise \
  --ledger /workspace/ledger_demo \
  --request /workspace/ledger_demo_data/revisions/rev_001_conflicting_reuse.json
python3 -I /workspace/migrate_tool/ledger.py revise \
  --ledger /workspace/ledger_demo \
  --request /workspace/ledger_demo_data/revisions/rev_bad_unknown_item.json

python3 -I /workspace/migrate_tool/ledger.py export \
  --ledger /workspace/ledger_demo --outdir /workspace/ledger_demo_out/current
python3 -I /workspace/migrate_tool/test_ledger.py
```

演示中的关键净变化：

- `REV-003`：`affected_item_count=2, changed_item_count=2`，两件均
  `migrated_to_pending`，原因 `unapproved_relation`。
- `REV-004`：`affected_item_count=2, changed_item_count=1,
  unchanged_item_count=1`；变化货品原因是
  `manual_route_target_absent + invalid_manual_routing`。
- 重发 `REV-001` 返回 `idempotent_replay=true`；发送
  `rev_001_conflicting_reuse.json` 返回退出码 2 和“已用于不同内容”。
- `items_duplicate.jsonl` 可验证重复货品 ID 整次初始化拒绝，且不生成正式
  `ledger.sqlite3`。
