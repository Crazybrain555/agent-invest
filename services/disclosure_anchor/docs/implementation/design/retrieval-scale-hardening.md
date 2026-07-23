---
id: disclosure_anchor_design_retrieval_scale_hardening
project: disclosure_anchor
title: 检索/API 规模化加固（写时物化分类 + 索引 + 分批重建）
status: implementing (0027)
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

## 5. 投影重建分批化（Discourse 形）

BuildSearchProjection 改 keyset 游标循环：`asset_id > :cursor ORDER BY
asset_id LIMIT :batch`，**逐批 upsert + commit**（默认 2,000/批）；full 与
delta 同构，delta 由版本列索引直达。孤儿删除同样分批。用例接口不变。

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

- worker 每轮 delta **全排空**（`limit=None`；keyset 分批+逐批提交机制不变），追平后
  每轮工作量自动等于该轮新发布单元数；
- 孤儿清理从仅 full 扩展到**每轮**：`unit_search_projection_v1` 是裸投影读
  （无 is_active join），滞留行会持续服务被替代 run 的单元；
- 否决方案：调大常数（结构性复发）；
- 缓做方案（带触发指标）：发布时 write-through（事件供给、零扫描）。当前 delta 空探测
  ~2.9s@85 万行，随语料线性增长；**超过 ~30s/轮时启动该设计**（约 10M+ 单元）。
