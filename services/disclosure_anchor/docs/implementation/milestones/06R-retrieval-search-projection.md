---
id: disclosure_anchor_milestone_06R
project: disclosure_anchor
title: 06R — 检索投影层（写入时分词 + 加权 tsvector + trgm 兜底）
status: done (2026-07-17 上线：0025 已应用，86,713/86,713 投影，ts_rank/trgm 验收通过)
created_at: 2026-07-17
depends_on: milestone 05/06、design/retrieval-and-semantic-keys.md §5/§6.3
decided_by: 用户 2026-07-17 三项确认（应用侧 jieba 预分词；pg_trgm 兜底本期做；CLI 重建 + worker 增量）
---

# 06R 检索投影层（实施级规格）

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
- 领域词典：`src/disclosure_anchor/adapters/retrieval/domain_dict.txt`（tracked，包内放置沿
  note_key_map 先例，运行时无 cwd 依赖）。初版内容 = note_key_map 的 389 个法定科目标签——
  全部来自既有受控词表，非样本短语。
- 版本钉死：`RETRIEVAL_RULES_VERSION = "rp-2026.07-1"`；模块内校验词典 sha256 与条目数，
  漂移即抛错（同 note_key_map 模式）。jieba 版本升级/词典变更必须升版并全量重建。
- 归一化：NFKC → casefold → 去空 token；数字/英文原样保留为独立 token。
- 输出：空格连接的 token 文本（供 `to_tsvector('simple', …)`）。

## 3. Schema（migration 0025_retrieval_search_projection）

`disclosure_core.unit_search_projection`：

| 列 | 类型 | 说明 |
|---|---|---|
| asset_id | varchar PK, FK document_unit ON DELETE CASCADE | 1:1 投影 |
| retrieval_rules_version | varchar NOT NULL | 重建幂等键 |
| title_text | text NOT NULL | title 原文副本（trgm 通道） |
| heading_path_text | text NOT NULL | heading_path 以 " > " 连接（trgm 通道；替代 0022 视图内派生） |
| title_tokens / path_tokens / body_tokens / key_tokens | text NOT NULL | 预分词，空串合法 |
| header_row_candidate | boolean NOT NULL | §5 派生标注 |
| built_at | timestamptz NOT NULL | 诊断用，不参与幂等 |
| search_tsv | tsvector GENERATED STORED | setweight(A=title,B=path,C=body,D=keys) 拼接 |

索引：GIN(search_tsv)；`CREATE EXTENSION IF NOT EXISTS pg_trgm`；GIN(title_text gin_trgm_ops)、
GIN(heading_path_text gin_trgm_ops)。公开视图 `disclosure_public.unit_search_projection_v1`
暴露全部列并授予 reader 角色（沿 0024 授权模式）。

## 4. 投影内容（确定性线性化）

- title_text = unit.title or ''；heading_path_text = " > ".join(heading_path)。
- body 线性化按 payload_kind：text→payload.text；table→caption+unit+headers+rows+notes
  （**排除 raw_html**，标签噪声不进 token）；mixed→按 parts 顺序递归；历史 qa 行不存在于新代。
- key_tokens = " ".join(semantic_keys)（本就是受控 ASCII token，不过分词器）。
- **header_row_candidate**（用户 2026-07-16 方向的落地，判错不污染证据）：
  payload_kind='table' 且 headers 为空 且 len(rows)≥2，且 rows[0] 每个 cell 非空且不匹配
  数值形态（`^[-+]?[\d,.]+%?$` 或含货币量级词），且其后至少一行含 ≥1 个数值形态 cell。

## 5. 重建与增量

- CLI：`pipeline rebuild-search-projection [--all]`（make rebuild-search-projection）。
  全量 = 对全部 active-run units 计算并 upsert（ON CONFLICT asset_id DO UPDATE），随后删除
  孤儿行（asset 不再 active）；增量 = 仅缺行或 retrieval_rules_version 过期的 units。
  单事务批量提交（每 1000 行 flush），无 outbox 事件。
- worker：每轮 publish 阶段后追加 `project` 增量阶段（复用同一 use case，限批
  WORKER_BATCH_PUBLISH 同阶）；失败按既有 failure 隔离规范记录，不阻塞轮次。

## 6. 验收

- 单元：分词器确定性（同输入同输出、词典 sha 校验拒漂移）、线性化各 payload_kind 正反例、
  header_row_candidate 正例（td-only 数值表）+ 负例（KV 表/纯文本表）。
- 迁移往返：0025 upgrade→downgrade→upgrade。
- 集成：真库全量重建后 `SELECT count(*)=active units`；`ts_rank` 加权可检索样例
  （"应收账款账龄"命中 receivable_aging 表投影；title 权重 > body）；trgm 子串命中简称。
- 契约：视图列集冻结导出；tests ledger --update。
