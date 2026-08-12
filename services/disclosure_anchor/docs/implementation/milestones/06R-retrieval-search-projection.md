---
id: disclosure_anchor_milestone_06R
project: disclosure_anchor
title: 06R — 检索投影层（写入时分词 + 加权 tsvector + trgm 兜底）
status: implemented; 0028 lossless windows + 0030 source-bound atoms validated in scratch (production migration pending reset)
created_at: 2026-07-17
depends_on: milestone 05/06、design/retrieval-and-semantic-keys.md §5/§6.3
decided_by: 用户 2026-07-17 三项确认（应用侧 jieba 预分词；pg_trgm 兜底本期做；CLI 重建 + worker 增量）
---

# 06R 检索投影层（实施级规格）

> 2026-08-13：0034 恢复 Unit 的完整 route set。当前 `key_tokens` 按顺序取
> `semantic_keys` 全集；Provider writer 尚无可信分类器时两列均为 NULL、channel 为空。
> 0033 的 duplicate-only 观测仍是历史事实，但不再被当作删除检索容量的依据。

**U7 边界（不变）**：投影是派生层——全部字段可由已持久化数据确定性再生；不进 content_hash /
query_projection_hash；重建不产生 outbox 事件；不替代 payload、不作证据；L1 API 不新增
search 端点（红线不变），L2 经公开视图直接消费。

## 1. 环境约束与选型（2026-07-17 探针 + 外部对标）

本地 Homebrew PG18 集群仅 `pg_trgm` 可用（zhparser/pg_jieba/pg_bigm 均不可得，编译 C 扩展
与零依赖面原则相悖）。选型：**应用侧 jieba 预分词**（托管 PG 场景的业界标准做法）+ DB 内置
`simple` 配置 + `setweight` 四权拼接 + 单 GIN；`pg_trgm` 对 title/面包屑建 GIN 作短查询/新词/
子串兜底双通道。竞争方案（in-DB pg_jieba）因部署脆弱性被否。

## 2. 分词器契约（adapters/retrieval/tokenizer.py）

- 依赖锁定：`jieba==0.42.1`（纯 Python）；精确模式 `lcut(text, HMM=True)`。
- 不加载领域短语词典或样本同义词；analyzer 漏召回由 0030 source-bound atom channel 补足，
  不靠持续追加词面。
- 版本钉死：`RETRIEVAL_RULES_VERSION = "rp-2026.07-5"`；jieba/Unicode 归一化、source-target
  或 atom 投影契约变化必须升版并全量重建。
- 归一化：NFKC → casefold → 去空 token；数字/英文原样保留为独立 token。
- 输出：空格连接的 token 文本（供 `to_tsvector('simple', …)`）。

## 3. Schema（0025 parent + 0028 lossless body windows）

`disclosure_core.unit_search_projection`：

| 列 | 类型 | 说明 |
|---|---|---|
| asset_id | varchar PK, FK document_unit ON DELETE CASCADE | 1:1 投影 |
| retrieval_rules_version | varchar NOT NULL | 重建幂等键 |
| title_text | text NOT NULL | title 原文副本（trgm 通道） |
| heading_path_text | text NOT NULL | heading_path 以 " > " 连接（trgm 通道；替代 0022 视图内派生） |
| title_tokens / path_tokens / body_tokens / key_tokens | text NOT NULL | 预分词，空串合法 |
| header_row_candidate | boolean NOT NULL | §5 派生标注 |
| body_search_windowed | boolean NOT NULL | private state；true 时 parent 不把 body 写进 tsvector |
| built_at | timestamptz NOT NULL | 诊断用，不参与幂等 |
| search_tsv | tsvector GENERATED STORED | safe row=A/B/C/D；windowed row=A/B/D |

索引：GIN(search_tsv)；`CREATE EXTENSION IF NOT EXISTS pg_trgm`；GIN(title_text gin_trgm_ops)、
GIN(heading_path_text gin_trgm_ops)。公开视图 `disclosure_public.unit_search_projection_v1`
保持 0025 的原 11 列与顺序，不暴露 private `body_search_windowed`。

PostgreSQL 18.4 实测存在四种静默或硬上限：同一 lexeme 第 256 个 occurrence 丢 position、
总 position 第 16,384 个饱和、超过 2,047 bytes 的 lexeme 被丢弃、超过 1 MB 抛 SQLSTATE
54000。0028 的 `disclosure_core.search_tsvector_is_safe()` 不靠固定窗口阈值，而是把
`ts_debug(simple)` 的 source occurrence 数与候选 tsvector 实际 positions 逐 lexeme 对账，
并捕获 datum limit；parent/child 各有 DB CHECK 再验证。

仅在完整 A/B/C/D 不安全且 A/B/D 安全时建立
`disclosure_core.unit_body_search_window`：

| 列 | 说明 |
|---|---|
| asset_id + window_index | PK；FK parent ON DELETE CASCADE |
| body_token_start / body_token_end | canonical token 序列的连续半开区间 |
| body_tokens | 区间原文；按序拼接必须精确重建 parent.body_tokens |
| search_tsv | C 权重 GENERATED STORED + GIN |

公开只读视图为 `disclosure_public.unit_body_search_windows_v1`。一个 token 自身仍不安全、
metadata 自身不安全、窗口 gap/overlap 或 DB probe 不完整均 fail loud，禁止丢词。

0030 增加 `disclosure_core.unit_search_atom(asset_id, atom_index, atom_text)`：每个非空行只对应
`provider_unit_locator.v1.search_targets` 选中的一个字符串叶子，绝不递归发现字段，也绝不连接
相邻 target/part；`atom_text` 固定为 NFKC→casefold 后的原文。主键为 `(asset_id, atom_index)`，
父投影删除时级联；`atom_text` 建 `gin_trgm_ops` GIN，公开只读面为
`disclosure_public.unit_search_atoms_v1`。atom 是可再生候选投影，不是证据；命中后引用仍回到
document_unit/source_ref。

## 4. 投影内容（确定性线性化）

- title_text = unit.title or ''；heading_path_text = " > ".join(heading_path)。
- body 的唯一输入是 `provider_unit_locator.v1.search_targets`；每个 target 必须绑定 provider
  source block、字段/item 与 Unit payload destination。word channel 仅为兼容现有 tsvector 而连接这些
  原子；0030 substring channel 保留逐叶 atom，禁止跨 target/part 拼接。`raw_html`、context、
  文件路径、taxonomy 标签和未声明 payload 字段一律不能被递归发现。
- key_tokens = " ".join(semantic_keys)（本就是受控 ASCII token，不过分词器）。
- `header_row_candidate` 在 provider-native writer 中固定为 false；L1 不从 HTML `<td>` 或数值
  形态猜表头。未来若需要 header role，只能来自可核验的 provider/source 结构证据。

## 5. 重建与增量

- CLI：`pipeline rebuild-search-projection [--all]`（make rebuild-search-projection）。
  全量按 active `processing_run` keyset 遍历；增量选择任一 unit 缺 current projection 的 run。
  每个 run 先完成全部 parent/window 准备，再在一个事务内删除旧窗、upsert 全部 parent、
  插入全部新窗并 commit。任何 child 失败会回滚整 run；没有 row limit，stop 只在 run 边界
  生效。孤儿 parent 分批删且 child FK cascade；无 outbox 事件。
- worker：每轮 publish 后执行同一无上限 delta。首个空 candidate-run batch 就是 exact caught-up
  证明；不能以 parent count 等于 active count 跳过，因为一个 orphan 与一个 missing 可抵消。

### 5.1 跨窗查询契约

query tokenizer 输出有序的 AND-of-OR groups（一个原词及其同义词是一组）。parent 与 child
分别对每个完整 OR-group 执行 `@@ to_tsquery('simple', group_query)`，结果 `UNION` 后按
`(asset_id, group_id)` 去重；仅 `HAVING count(DISTINCT group_id)=group_count` 的 asset 命中。
不得把 synonym alternatives 展平成全 AND，也不得跨 asset 或要求所有 group 落在同一个窗口。

### 5.2 atom 子串候选契约（L2 直读）

atom query 使用与写侧完全相同的 NFKC→casefold；归一化后少于 3 个 Unicode 字符时，atom
channel 必须关闭，只走 §5.1 的完整 word-group channel。1–2 字不承诺任意 body 子串召回；
未来若需要，必须另做有 scope 的扫描或独立 source-bound gram 索引，不能把 whole-atom equality
冒充子串完整性。

长度 ≥3 时，`LIKE` 只负责触发 `gin_trgm_ops` 候选，且 `%`、`_`、`\` 必须逐一转义；随后在
同一 `atom_text` 上以 `strpos(atom_text, normalized_query) > 0` 做精确 heap recheck。
word candidate 仍须满足全部 query group，atom candidate 仍须由一个 atom 包含完整 normalized
query；两组完整候选最后才 `UNION`，不得把任一 channel 的局部 token/gram 当命中或证据。

```sql
WITH input AS (
  SELECT CAST(:normalized_query AS text) AS atom_query,
         CAST(:query_groups AS text[]) AS word_groups
),
groups AS (
  SELECT ordinality AS group_id, query_text
    FROM input,
         unnest(word_groups) WITH ORDINALITY AS g(query_text, ordinality)
),
word_group_hits AS (
  SELECT p.asset_id, g.group_id
    FROM disclosure_public.unit_search_projection_v1 p CROSS JOIN groups g
   WHERE p.search_tsv @@ to_tsquery('simple', g.query_text)
  UNION
  SELECT w.asset_id, g.group_id
    FROM disclosure_public.unit_body_search_windows_v1 w CROSS JOIN groups g
   WHERE w.search_tsv @@ to_tsquery('simple', g.query_text)
),
word_hits AS (
  SELECT asset_id FROM word_group_hits
   GROUP BY asset_id
  HAVING count(DISTINCT group_id) = (SELECT count(*) FROM groups)
     AND (SELECT count(*) FROM groups) > 0
),
atom_hits AS (
  SELECT DISTINCT a.asset_id
    FROM disclosure_public.unit_search_atoms_v1 a CROSS JOIN input i
   WHERE char_length(i.atom_query) >= 3
     AND a.atom_text LIKE (
           '%' || replace(replace(replace(i.atom_query, '\', '\\'),
                                  '%', '\%'), '_', '\_') || '%'
         ) ESCAPE '\'
     AND strpos(a.atom_text, i.atom_query) > 0
)
SELECT asset_id FROM word_hits
UNION
SELECT asset_id FROM atom_hits;
```

## 6. 验收

- 单元：分词器确定性（同输入同输出、词典 sha 校验拒漂移）、线性化各 payload_kind 正反例、
  header_row_candidate 正例（td-only 数值表）+ 负例（KV 表/纯文本表）。
- 迁移往返：0028 upgrade→downgrade→upgrade；存在 windowed row 时 downgrade fail loud。
- 集成：真库全量重建后 `SELECT count(*)=active units`；`ts_rank` 加权可检索样例
  （"应收账款账龄"命中 receivable_aging 表投影；title 权重 > body）；trgm 子串命中简称；
  255/256、16383/16384、长 lexeme、1 MB、连续覆盖、跨窗 AND、跨资产负例、GIN plan 与
  child insert failure 的 run rollback；body atom 三个 analyzer canary、跨 atom 负例、LIKE 符号转义、
  NFKC/全半角、atom GIN plan 与删除后失败的事务回滚。
- 契约：视图列集冻结导出；tests ledger --update。
