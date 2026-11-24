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
