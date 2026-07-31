---
id: disclosure_anchor_design_retrieval_scale_hardening
project: disclosure_anchor
title: 检索/API 规模化加固（写时物化分类 + 索引 + 分批重建）
status: implementing (0028 scratch-validated; production reset/migration pending)
created_at: 2026-07-21
depends_on: 0022 分类视图、06R 检索投影、api routers
---

# 检索/API 规模化加固

## 1. 问题与实测证据（2026-07-21，8,500 文档 / 83 万单元基线）

审查（三读审查员发现 + Fable 逐条实物核查）定位 7 条规模化缺陷，实测坐实：

| 热路径 | 基线延迟 | 根因 |
|---|---|---|
| latest_filings 首页 20 条 | **26,968ms** | 内层 DISTINCT ON 全语料重算，且每行过 LATERAL 分类 |
| filing_type 过滤文档列表 | 1,110ms | 过滤的是 LATERAL 现算列，绕开一切索引 |
| 投影 delta 选择（每轮跑）| 3,132ms | retrieval_rules_version 无索引 + 全量物化进 Python |
| title ILIKE 子串 | 31ms（线性涨）| document.title 无 trgm 索引 |

其余：投影全量重建单一大事务（钉 vacuum、WAL 膨胀、失败全回滚）；
supersedes_document_id 无索引（视图每行 LATERAL 子扫）。

## 2. 外部对标（设计前调研）

- **GitLab**：keyset 分页强制主键平局键；latest-per-group 用 DISTINCT ON /
  LATERAL top-1 **前提是分组键为真实索引列**；大表变更一律有界批+逐批事务+游标。
- **Discourse**：search_data 带版本列，定时作业按「版本 ≠ 当前」捞过期行、
  每批 2 万条有界重建——版本列即过期队列。
- 结论：问题不在 DISTINCT ON 语法，在**分组/过滤键是每读现算的派生列**。
  根治 = 写时物化 + 版本化刷新 + 补索引；重建作业 = keyset 游标 + 逐批提交。

## 3. 对 0017「不物化分类」决策的显式修订

0017 选择视图 LATERAL 现算，契约是「改 JSON → load-rules → 立即生效，
零陈旧、免重分类工具」。该契约在 8.5k 文档时读代价已不可接受（上表），
10 万级直接不可用。修订如下，**运维契约保持不变**：

- document 表新增物化列：`class_filing_type / class_disclosure_topics /
  class_publisher_categories / class_market / class_content_categories /
  class_rules_version`，语义与 0022 LATERAL 逐字段等价（含 NULL 行为）。
- 刷新语句单一权威在 `adapters/db/postgres/classification_refresh.py`；
  迁移内为语义快照副本（迁移冻结原则，不 import 运行时代码）。
- 三个写入点：①注册时逐文档刷新（同事务）；②`make load-rules` 装载规则后
  按 `class_rules_version` 不匹配整表刷新（**保住「load-rules 即生效」契约**，
  10 万行 UPDATE 秒级）；③迁移回填存量。
- 公共视图列契约不变（列名/类型/NULL 语义同 0022），仅派生方式改为读实列。

## 4. 索引组

- `(class_filing_type, announcement_date DESC NULLS LAST, document_id DESC)` —— 分类列表页
- `(company_id, class_filing_type, report_period, announcement_date DESC
  NULLS LAST, document_id DESC)` —— latest_filings 的 DISTINCT ON 排序
- GIN `class_disclosure_topics`、GIN `class_content_categories`（路由器
  content_category 过滤改写为 `@>` 包含式以吃 GIN）
- trgm GIN `document.title`（ILIKE 子串直达）
- 部分索引 `supersedes_document_id WHERE NOT NULL`
- `unit_search_projection.retrieval_rules_version` btree（delta 队列）

## 5. 投影重建边界（0028 当前契约）

BuildSearchProjection 以 `processing_run_id` 作 keyset 和事务边界。只要 run 内任一 active
unit 缺失或版本陈旧，就完整准备该 run 的 parent rows 与 lossless body windows，再在一个
事务中替换；child 写失败回滚整 run。没有 row limit，stop 只在 run 间生效；孤儿删除仍按
asset_id 有界提交。早期 asset-level batch 方案及其事故演进保留在 §8.1–§8.3 作为历史证据，
但不再描述现行实现。

## 6. 明确不做（本轮）

- 不改 API 分页契约（现有 cursor 形态保留；查询形状经索引化后自然达标）；
- 不物化 superseded_by（LATERAL top-1 经 supersedes 索引后为点查）；
- 不动 worker 侧 classification_rule 谓词（scope 判定路径无读放大证据）。

## 7. 验收

迁移往返（upgrade/downgrade）绿；存量回填后**新列与旧 LATERAL 全库逐行
等价断言**；四条热路径复测延迟（目标：latest_filings < 100ms）；全量源身份
重放审计零发现；`make agent-check` + §4 独立复审。

## 8.1 §5 修订（2026-07-23，投影饱和事故）

§5 的「delta 由版本列索引直达」隐含假设 worker 每轮的 delta 上限足够覆盖新增；
实际 `_project_stage` 借用了 `WORKER_BATCH_PUBLISH`（=10，文档尺度）作单元尺度上限，
每轮只重建 10 个单元，与解析产出差约 170×。覆盖率自上次全量重建后单调衰减至 48%
（活跃 1,755,455 vs 投影 846,495；缺口 908,960 全为完全缺失），doctor 告警按设计触发，
但 delta 自身无自愈能力。

修订采纳的不变量（对标 Discourse write-through+版本 rebake / GitLab ES 事件队列 /
Zulip tsvector 触发器的共同形态）：**索引维护量与新增/变更内容成比例，禁止与任何
固定常数或语料规模挂钩**。落地：

- worker 每轮 delta **全排空**（当时实现为 `limit=None`；0028 已删除 limit 参数），追平后
  每轮工作量自动等于该轮新发布单元数；
- 孤儿清理从仅 full 扩展到**每轮**：`unit_search_projection_v1` 是裸投影读
  （无 is_active join），滞留行会持续服务被替代 run 的单元；
- 否决方案：调大常数（结构性复发）；
- 缓做方案（带触发指标）：发布时 write-through（事件供给、零扫描）。当前 delta 空探测
  ~2.9s@85 万行，随语料线性增长；**超过 ~30s/轮时启动该设计**（约 10M+ 单元）。

## 8.2 §8.1 追加修订（2026-07-24，delta 追赶卡死事故）

§8.1 的「每轮全排空」上线后第一次追赶（缺口 90.9 万）暴露 delta 选择器自身的
计划病理：`OUTER JOIN + OR(缺失, 版本陈旧)` 令规划器无法用 anti-join 策略，
每批退化为全表 Parallel Hash Join + Sort（EXPLAIN cost 656,020/批），454 批
即天级——worker 轮被投影阶段钉住 15 小时，GPU 空转、解析要等轮末才重试而
轮末永不到达。叠加诱因：前日 AgentSSD 卷抖动造成 parser_invocation_failed
批量失败与一次探测失败 halt。

修复（EXPLAIN 实测过的两段式）：

- **缺失 pass**：`NOT EXISTS` 反连接 + keyset —— 规划器走双 PK Merge Anti
  Join 按 asset_id 序推进（cost 12,138/批，startup 1.4），找满即停，游标续走
  不重扫；追赶总代价 O(语料) 一次，而非 O(语料×批数)。
- **陈旧版本 pass**：投影表按版本≠当前自消耗队列，**亦带 keyset 游标**：单元
  行已被 prune-history 删除的陈旧投影行永远无法重打戳，无游标会活锁；游标跳
  过的残留恰为非活跃集，交由随后的孤儿清理删除。稳态一次空探测（~1-2s）。
- full 模式路径不变（干净 keyset，本就是快路径）。

运维配套：`run_worker_once.sh` 改为 `exec` python 本体（去掉 make/sh 夹层），
launchd 信号可达；此前三次 kickstart 均把 python 留成持锁孤儿，KeepAlive
新实例只能 [skip] 空转。

## 8.3 §8.2 复审轮落地（2026-07-24 下午）

对 §8.2 修复的独立复审（3 视角+对抗核查）实测出两个稳态回归，均已根治：

1. **孤儿清理 NOT IN 病理**（复审判 high）：`NOT IN (子查询)` 被规划成逐行重扫
   1.64M 物化子计划（EXPLAIN cost 1.05e10，零孤儿时 EXPLAIN ANALYZE 400s 未完）。
   改写为 NOT EXISTS 反连接（实测 15.7s@零孤儿）后仍属全库扫描类，故再加**事件门**：
   run 失活是本服务内孤儿的唯一来源，PublishRunResult 新增 `superseded_run_id`，
   worker 汇总为轮级 `runs_deactivated` 信号——本轮零替换即跳过扫描；投影数 >
   活跃数时无条件强制清理（孤儿必然存在）。残余：publish 与投影之间崩溃产生的
   孤儿可跨轮滞留；0028 的 exact missing drain 会先补齐抵消计数的 missing，下一轮
   `projection > active` 门即可证明并清除 orphan。当前没有 write-through trigger。
2. **缺失 pass 稳态空探测 19s**（复审判 medium，实测 ~6GB 读/轮）：反连接必须吐出
   83 万非活跃单元再被过滤，LIMIT 永远填不满。修复为**计数门 + ULID 时间高水位**：
   清理后投影 ⊆ 活跃集，计数相等即证明无缺失（跳过扫描）；不等时从
   `max(投影 asset_id) - 2h` 的 ULID 下界起扫（新单元必然带新时间前缀 ULID）；
   精确性不依赖高水位假设——扫后复算计数，仍不等则无界全扫兜底。等量对冲盲点
   （孤儿与缺失同轮等量出现）由下一不等轮自愈 + doctor 覆盖率告警兜底。
3. 陈旧版本 pass 加 **btree 双向 range 门**（`<v` 与 `>v` 各一次索引探测，实测 3ms）；
   陈旧 pass 因清理先行不再重打非活跃行（复审 low 发现同步关闭）。

稳态轮末投影成本实测构成：计数 1.7s + 陈旧门 3ms + 高水位 2ms ≈ **~2s/轮**
（有替换的轮 + 清理 15.7s，事件比例摊销）。复审另一 low 发现（陈旧 pass 游标
防的活锁在 FK CASCADE 下不可达）被对抗核查反驳，游标作为无害防御保留。

## 8.4 §8.3 修订（2026-07-28，lossless tsvector + run 原子性）

PostgreSQL 18.4 的 tsvector 在 1 MB 之前也会静默丢 occurrence：同 lexeme 第 256 个
position、总 position 第 16,384 个以及超过 2,047 bytes 的 lexeme。固定 chunk size 无法
覆盖这些不同物理边界。0028 因此采用数据库 source-occurrence probe：

- safe parent 保留 A/B/C/D；unsafe body 由 probe 驱动 token 半开区间二分，parent 只存 A/B/D，
  child 存 C；无词数、词面或文档类型阈值；
- 每个 accepted window 由 DB CHECK 再证无丢失，所有窗口连续、无重叠且精确重建 body；
- delta 始终执行一次 exact candidate-run anti-join，首个空 batch 即 caught-up 证明；不再使用
  会掩盖 orphan+missing 等量对冲的 count-equality skip，也不重复预扫描；
- parent/window 按完整 processing_run 单事务替换；这替代 asset-level 逐批提交，避免同一证据
  run 的检索面处于半窗状态；
- 跨窗 AND 保留 tokenizer 的 AND-of-OR groups，按 `(asset_id, group_id)` 聚合；禁止把同义词
  alternatives 展平成全 AND、跨 asset 合并或要求全部 group 落在单一窗口。

PG18 managed scratch 已覆盖 255/256、16383/16384、长 lexeme、1 MB、跨窗 AND、跨资产负例、
GIN plan、downgrade fail-closed 与 child insert 整 run rollback。

## 8.5 CJK analyzer 漏召回修订（2026-07-28，0030）

研究问题：body 只有 jieba→tsvector 时，如何补足 analyzer 未产生 query lexeme 的精确子串召回，
同时不让相邻 table cell/search target/mixed part 拼成不存在的证据。PostgreSQL 18 `pg_trgm`
官方契约确认 GIN 支持非左锚定 `LIKE`，但 query 中可提取 trigram 越少效率越差、无 trigram
会退化为 full-index scan；Elastic 官方 n-gram 指南同样建议固定 `min_gram=max_gram=3` 作为
起点，gram 越短候选越泛。

采纳不变量：每个 v2 explicit search-target 字符串叶子单独存为 NFKC→casefold atom；长度 ≥3
的完整 normalized query 用 GIN `LIKE` 取候选，再以同 atom `strpos` 精确复核。word channel
只有满足全部 query group 才命中，atom channel 只有一个 atom 含完整 query 才命中，最后才 OR。
query gram 只用于找候选，绝不进入 payload/source_ref 或成为证据。

否决方案：给 jieba 追加样本词表（无穷补丁且改变 analyzer 语义）；对 joined body 建 trgm
（会跨证据边界造命中）；把 1–2 字 whole-atom equality 宣称成任意子串（语义不完整）。本轮
1–2 字只走既有完整 word channel；若未来真实召回集证明需要短子串，另行评估 scoped scan 或
source-bound 1/2-gram 的空间成本。

验证：三个 analyzer 漏召回 canary、跨 atom 负例、`%/_/\` 转义、全半角/NFKC、GIN plan，以及 atom 删除后
child 写失败的整 run rollback，均在 PG18 managed scratch 执行。

外部证据：

- PostgreSQL 18 `pg_trgm`：
  https://www.postgresql.org/docs/18/pgtrgm.html
- Elastic n-gram tokenizer（固定 gram 与 trigram 起点）：
  https://www.elastic.co/docs/reference/text-analysis/analysis-ngram-tokenizer
