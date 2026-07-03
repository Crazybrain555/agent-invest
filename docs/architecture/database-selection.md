# disclosure_anchor 数据库选型调研

调研时间：2026-06-16（2026-06-20 按 v1.0 canonical 设计对齐）

> **对齐说明**：本文结论（PostgreSQL 主库 + 文件系统 + 可选 DuckDB）不变，但第一版的对象模型已按
> `service-purpose.md` 和 `财报与披露数据接入及切分方案.md` 收缩为 `document_unit` 体系。原稿中的
> `filing_text_block` / `filing_text_chunk` / `filing_table` / `filing_table_cell` / `page_idx` / `bbox` /
> 独立 embedding 字段**不再是第一版核心对象**，相关段落已重写。术语和硬决策以 `service-purpose.md`（canonical 契约）为准，实施细则见《财报与披露数据接入及切分方案》（implementation_plan）。

输入文档：

- `docs/architecture/service-purpose.md`（canonical 契约）
- `docs/architecture/财报与披露数据接入及切分方案.md`（实施方案，status: implementation_plan）
- `docs/architecture/pdf-parsing-investigation.md`
- `docs/architecture/cninfo-webapi-usage-reference.md`

本文回答三个问题：

1. `disclosure_anchor` 第一版数据库应该选什么？
2. “Postgres 对 AI 好”这个说法是否成立、成立到什么程度？
3. 财报解析成 Markdown 后，正文、表格应该切到什么粒度？

## 结论

推荐把 **PostgreSQL 作为主数据库**，把原始 PDF 和全部 parser artifact 继续放在本地文件系统，必要时再用 DuckDB 做分析副引擎。

第一版不要先上独立向量数据库，也不要把 DuckDB/SQLite 当主账本；数据库只保存需要被查询、约束、版本化和追踪的核心对象。

推荐形态：

```text
local filesystem
  raw_documents/              # 原始 PDF / 附件 / H5 保存件（不可变、只追加）
  parser_artifacts/           # MinerU/Docling 的 md / json / html / 图片等解析产物（可重生成）

PostgreSQL
  company                     # 公司主体
  security                    # 证券标识（公司 1 : N 证券）
  source_access               # 一次远端访问 / 文件获取记录（标准数据 provider/CNINFO/MCP/Web 通用，含查空）
  document                    # 一个披露文件版本：来源、日期、类型、报告期、raw 路径 + 哈希、状态
  processing_run              # 下载/解析/清洗/切分/重跑的版本、状态、错误、是否 active
  document_unit               # 当前 run 切好的 text / table / qa 单元（payload 存内容快照）
  optional: source_checkpoint # 增量同步游标，仅在数据源需要断点续跑时建立

DuckDB, optional
  read-side analytics over Parquet/CSV/JSON exports
```

把握等级：

- 高把握：主账本用 PostgreSQL 比 SQLite/DuckDB/独立向量库更适合当前服务目的。
- 高把握：Markdown 适合正文展示和检索派生，不适合做表格事实来源。
- 高把握：第一版不建 `chunk` / `table_cell` / `page/bbox` 表，把这些 parser 细节留在文件系统。
- 中高把握：第一版不上独立向量库；以后真有语义检索瓶颈，pgvector 可作为 `document_unit` payload 之上的派生索引。
- 中等把握：中文全文检索的最终效果要实测；PostgreSQL 内置全文检索很好，但中文财报需要额外分词/三元组/外部搜索方案验证。

## 需求映射

`service-purpose.md` 里的数据库不是临时索引，而是一个可查询的 filings database + Filing API。它至少要稳定回答：

- 某家公司有哪些公告和财报已发现、已下载、已解析；
- 某份 `document` 的元数据、原始文件路径、哈希、来源 URL、公告日期、报告期；
- 正文按 `heading_path` 取得的 `text` 单元；
- 完整结构的 `table` 单元（表名、单位、表头、行数据、脚注、原始字符串）；
- 完整问答的 `qa` 单元；
- 哪次 `processing_run` 成功或失败、用哪个 parser 版本、哪个是 active run；
- L2 能按 公司 / 报告期 / 公告类型 / `payload_kind` / `heading_path` / `semantic_key` / 标题 / `asset_id` 直接查询。

> 标准财务数据（标准数据 provider 的三大表、财务指标、审计意见等）不入本库，走 `Dataset API`；本库只承载自维护披露文件的 `document` / `document_unit`。两条路径在 L2 汇合（见 canonical §1.1）。

这几个要求决定了主数据库需要优先满足：

1. **关系型约束**：公司、证券、文档、处理运行、文档单元、来源访问之间有明确关系。
2. **事务和去重**：下载、哈希、解析状态、重跑版本、active run 标记不能乱。
3. **并发写入**：后续可能同时跑 CNINFO 同步、PDF 下载、parser、质量检查、API 查询。
4. **半结构化承载**：CNINFO 返回字段、`source_access.query_params`、`document_unit.payload`（headers/rows/text/qa）都会变化，需要 `jsonb` 或等价能力。
5. **检索扩展**：先支持元数据过滤和关键词搜索，再按需扩展向量/混合检索。
6. **本地 API 化**：未来 Filing API / REST / MCP 读写同一个稳定数据库，而不是每次扫文件夹。

PostgreSQL 正好覆盖这些需求；SQLite 覆盖本地简单账本；DuckDB 覆盖分析；向量库覆盖语义近邻检索，但它们单独都不适合作为这个服务的事实主库。

## 候选方案对比

| 候选 | 适合角色 | 优点 | 主要问题 | 本项目结论 |
|---|---|---|---|---|
| PostgreSQL | 主数据库 / API 后端 | ACID、约束、并发、JSONB、索引类型、全文检索、扩展生态、pgvector 可选 | 要运行服务；中文全文检索需要额外方案；pgvector 不是无限规模向量系统 | **推荐作为主库** |
| SQLite | 本地原型 / 测试 fixture / 单机缓存 | 零运维、单文件、可靠、足够快 | 单库同一时刻只有一个 writer；多进程/多机器/高并发写不合适；后续迁移成本 | 不做长期主账本 |
| DuckDB | 分析副引擎 / Parquet 查询 / 批量校验 | 嵌入式 OLAP、直接读 Parquet/JSON/CSV、分析很强 | 官方并发模型偏单进程；不是 OLTP 主库 | 作为 read-side 工具，不做主账本 |
| 独立向量库 | 大规模语义检索 sidecar | 专门做向量 ANN、过滤、扩展、部署形态成熟 | 需要和主库同步；事实来源会分裂；第一版复杂度过高 | 等向量规模和延迟需求证明后再引入 |
| Elasticsearch/OpenSearch | 关键词/中文检索 sidecar | 文本检索强，中文 analyzer 生态成熟 | 又是一个同步系统；不是事实主库 | 可作为后续检索增强，不是第一版主库 |

## 为什么主库选 PostgreSQL

### 1. 它最像这个服务需要的“本地 filings database”

这个服务要保存的是有强关系的事实记录，不只是文档 blob：

```text
company ──< security
company ──< document ──< processing_run ──< document_unit (text/table/qa)
document ──> source_access            # 来源访问登记
(optional) source_checkpoint          # 增量同步游标
```

这些对象需要唯一约束、外键、状态流转、版本记录（active run）、可重跑和可审计。PostgreSQL 是成熟的事务型关系数据库；相比之下，向量库和搜索引擎更像派生索引，不适合承载事实账本。

> 注意第一版**不**建 `document → block → chunk` 和 `table → cell` 这类回指结构。`document_unit` 直接保存切好的内容快照（在 `payload` 里），`artifact_locator` 只是可选的回溯指针，用于回看 parser artifact，不是主查询键，也不要求 page/bbox（见 canonical §11、§7.2）。

### 2. JSONB 适合保存“有结构但会变”的解析/接口字段

CNINFO 字段、MinerU `content_list.json`、DoclingDocument、表格解析结果都会随版本变化。PostgreSQL 的 `jsonb` 能验证 JSON、支持二进制分解存储、函数/操作符和 GIN 索引。官方文档也提醒，关系模型和 JSON 可以共存，并建议 JSON 文档保持可预测结构。

本项目建议：

- **稳定查询字段升成列**：`document_id`、`company_id`、`announcement_date`、`report_period`、`filing_type`、`payload_kind`、`heading_path`、`semantic_key`、`order_index`、`content_hash`、`quality_status`、`processor`、`processor_version`、`is_active_run`。
- **原始/变动字段进 `jsonb`**：CNINFO 原始 payload、`source_access.query_params`、`document_unit.payload`（text 的正文 / table 的 headers+rows+notes / qa 的 question+answer）、parser metadata。
- **大文件不要塞 DB**：PDF、完整 parser artifact、表格 HTML 等放文件系统，DB 存路径、哈希和摘要字段。

### 3. 它能先做关键词检索，再按需做向量/混合检索

PostgreSQL 内置全文检索支持 `tsvector`、`tsquery`、ranking、GIN 索引；同时 `pg_trgm` 是官方附带扩展，可以做 trigram 相似度和非左锚定 `LIKE`/正则加速。检索可以直接建在 `document_unit.payload` 的正文上。

但要注意中文：

- 英文型 `to_tsvector('english', ...)` 不等于中文财报搜索。
- A 股财报需要实测中文分词。可选路线包括 `simple`/`pg_trgm` 粗检索、`pg_jieba`/`zhparser` 这类中文分词扩展，或后续接 Elasticsearch/OpenSearch。
- 所以第一版可以先把 `document_unit` 结构存对；中文检索质量作为可替换索引层验证，不要让 schema 依赖某个分词器。

> canonical §12 明确：全文关键词检索可以后加，但**不是证据对象，也不要求向量化**。检索只用于候选发现，证据引用永远依赖 `document_unit` 的 payload 快照。

### 4. pgvector 让“PG 对 AI 好”有现实含义——但第一版不急着用

“Postgres 对 AI 好”不是因为 Postgres 自己会理解财报，而是因为：

- pgvector 可以把 embedding 存在同一个事务数据库里；
- 它支持 exact nearest neighbor，也支持 HNSW/IVFFlat 近似索引；
- 它可以和普通 SQL 条件一起用，例如先限制公司、日期、公告类型、`heading_path`、parser 版本，再做语义相似度；
- 它可以和 PostgreSQL 全文检索做 hybrid search。

这对财报检索尤其重要，因为财报检索经常带硬过滤：

```text
公司 = 300066
报告期 = 2026Q1
公告类型 = 季报
heading_path 命中 管理层讨论 / 财务报表附注
quality_status != unusable
```

把 metadata 和（未来的）embedding 放在一个主库里，可以减少“主库记录更新了，向量库没同步”的问题。

但**第一版的决策是不建 embedding 列、不建 chunk 表**（见 canonical §11、财报方案 §11.9）：

- 向量索引一定是派生物，且当前还没有证据证明本地 filings 文本规模需要它；
- 真要做时，它是 `document_unit` payload 之上的派生索引，按 财报方案 §16 Phase 6“只对明确瓶颈增加全文或语义检索”引入；
- pgvector 的 HNSW 查询性能好但建索引慢、占内存；IVFFlat 建索引更轻但召回/速度取舍不同；
- pgvector 不替代 chunking、embedding model、reranker、评测集；这些 AI 质量问题不由数据库自动解决。

结论：**PG 对 AI 好，在本项目里成立；但成立点是“统一事实库 + 元数据过滤 + 未来可选向量索引”，不是“现在就建 chunk + embedding”，更不是“数据库替代 AI 检索工程”。**

## 为什么不把 SQLite 当主库

SQLite 很适合低运维本地应用。官方文档明确说它强调经济、效率、可靠、独立、简单，适合本地文件型应用；如果数据小、写并发低、低于 TB 级，SQLite 往往很香。

但当前目标不是一个临时脚本，而是本地 filings database + Filing API。后续很自然会出现：

- 下载器写 `document` / `source_access`；
- parser 写 `processing_run` / `document_unit`；
- 质量检查器更新 `quality_status` / `needs_review`；
- Filing API / 查询进程同时读。

SQLite 的官方选择指南也说，多个并发 writer 或数据跨网络/多进程时，client/server 数据库通常更合适。第一版如果为了省安装用 SQLite，之后大概率要迁移 schema 和查询语义。

因此：

- SQLite 可用于单元测试、fixture、临时 cache；
- 不建议把 SQLite 作为这个服务的目标主库。

## 为什么不把 DuckDB 当主库

DuckDB 很适合本项目的另一个侧面：分析。

它可以直接读 Parquet/JSON/CSV，读 Parquet 支持 projection/filter pushdown，非常适合后续做：

- 导出 `document_unit(table)` 的行数据到 Parquet 后批量分析；
- 对表格数字做 cross-check；
- notebook/研究脚本临时聚合；
- 从 parser artifact 或中间文件快速抽样。

但 DuckDB 官方并发文档说明，它的 read-write 模式主要是单进程内并发；多进程写 DuckDB 原生库需要额外协议，2026-06 时相关远程协议仍处于 beta/演进阶段。它的长处是 OLAP，不是承载多任务 ingest/parser/API 的主账本。

因此：

- DuckDB 应作为 **read-side analytics engine**；
- 可以从 PostgreSQL 导出 Parquet，或直接读文件系统里的 parser artifact；
- 不建议让 DuckDB 管 `processing_run`、下载状态、去重、Filing API 主查询。

## 为什么第一版不单独上向量库

Qdrant/Milvus/Chroma/LanceDB 这类系统对大规模向量搜索很有价值。以 Qdrant 为例，它围绕 collection/point/vector/payload 组织数据，支持 payload 过滤、dense/sparse/hybrid 检索、HNSW 和扩展部署。

但第一版不建议把它作为必需组件：

1. 本服务的事实主数据是 `document` 元数据、原始文件、`document_unit` 内容快照和处理状态，不是向量。
2. 向量索引一定是派生物：embedding model 变了就要重算。
3. 单独上向量库会引入同步、删除、重跑、权限、备份、恢复的一整套额外问题。
4. 目前还没有证明本地 filings 文本规模会超过 pgvector 的舒适区。

推荐路线：

- 第一版：PostgreSQL 主库，落 `company` / `security` / `source_access` / `document` / `processing_run` / `document_unit`；**不建 chunk 表，不留 embedding 列**。
- 后续阶段：若出现明确检索瓶颈，先在主库里用 pgvector 对 `document_unit` payload 做小规模语义检索和 hybrid search。
- 只有当出现明确证据时再拆 sidecar：例如向量条数达到千万级、高 QPS、复杂 payload filtering 性能不足、多模型多向量召回需要专门引擎。

## Markdown 财报应该切到什么程度

结论：**Markdown 和 parser artifact 是正文的可读/检索派生层，留在文件系统；入库的事实对象是 `document_unit`（text/table/qa）的内容快照，不是 block/chunk/cell。**

按 canonical §6–§8 和 财报方案 §5–§6 的切分规则：

```text
PDF
 └─ MinerU/Docling 解析产物（parser_artifacts/，文件系统，可重生成）
     ├─ document.md / content_list.json / table html-json
     └─ 按业务结构切分，落 document_unit：
         ├─ text  : 章节 / 子标题 / 完整事项（payload.text）
         ├─ table : 完整表格（payload: unit, headers, rows, notes, nearby_explanation）
         └─ qa    : 完整 Question + Answer（payload: question, answer）
```

正文切分建议：

- **按业务结构切，不按版面和 token 切**。识别顺序：文档标题 → 章节 → 子章节 → 显式编号条目 → 完整 Q&A → 完整表格 → 完整事项。不使用固定字符数、固定 token、overlap、page 作为持久化边界。
- 单元的逻辑地址用 `heading_path`（标题面包屑），例如 `["第三节 管理层讨论与分析", "一、报告期内公司从事的主要业务"]`，而不是 Markdown 文件路径或页码。
- 长小节若没有更细的真实结构，即使较长也保存为一个 `text` 单元；运行时 agent 可临时截取上下文，但**数据库不为此生成长期 chunk**。
- 回到 parser artifact 用可选 `artifact_locator`（`artifact_path` + `artifact_unit_ref` + `order_index`），不要求 page/bbox/字符偏移。

表格切分建议：

- 表格保存为**完整 `table` 单元**：表名、`heading_path`、单位、表头、行数据、脚注、紧邻解释、原始字符串、`quality_status`。
- **不拆数据库单元格（no `table_cell`），不强行建立全市场统一产品分类**。负号、括号、千分位、单位都按原始字符串保留；数字规范化是下游（L2）的派生工作，原值必须留。
- 跨页表只是版面问题，解析后合并为一张逻辑表，生成一个 `table` 单元；调用方不需要知道它来自几页。
- HTML / cell 级 JSON 可以作为 parser artifact 留在文件系统供人工复核，但**不进核心 schema**。
- 只有某种表经过真实使用证明可反复复用后，才在更上层晋级成标准 dataset（财报方案 §4.4），原 `table` 单元仍保留，晋级数据是派生视图。

标准数据 provider 已覆盖的标准财务数据（三大表、标准指标、审计意见等）不从 PDF 重建为第二套本地标准表（canonical §2.5），走 `Dataset API`。

## 第一版实施建议

第一版目标不是把所有高级检索一次做完，而是选一个不需要推倒重来的主库和一组小而稳定的对象。

建议顺序：

1. **PostgreSQL 主库**：先落 `company` / `security` / `source_access` / `document` / `processing_run` / `document_unit`。
2. **文件系统存大对象**：PDF 放 `raw_documents/`；完整 Markdown / parser JSON / 表格 HTML 放 `parser_artifacts/`；DB 存路径、哈希、parser run。
3. **结构化优先**：稳定字段列化，变动字段进 `jsonb`，`document_unit.payload` 存切好的内容快照。
4. **关键词检索先轻量**：先有 公司/日期/类型/报告期/`heading_path`/`semantic_key`/标题 查询；再加 `pg_trgm` 或 `tsvector`。
5. **中文检索单独验证**：拿 20-50 个真实中文查询测试 `simple`、`pg_trgm`、`pg_jieba/zhparser` 或外部搜索方案。
6. **向量检索后置但不封死**：第一版不建 chunk 表、不留 embedding 列；真有瓶颈时在 `document_unit` payload 上加 pgvector 派生索引。
7. **DuckDB 作为分析工具**：当 `document_unit(table)` 或 parser artifact 变多时，导出 Parquet 给 DuckDB 做批量校验和 notebook 分析。

第一版不建议：

- 把原始 PDF 作为 bytea 大字段塞进 Postgres；
- 建 `filing_text_block` / `filing_text_chunk` / `filing_table` / `filing_table_cell` / `page_idx` / `bbox` 这类回指表（留在 parser artifact）；
- 先上 Qdrant/Milvus/Chroma 再回头补关系型主库；
- 用 DuckDB 管下载/解析状态；
- 为了省事先用 SQLite 做主库，除非明确承认它只是 throwaway prototype；
- 为每一种财务附注表新建一张专门 SQL 表（先统一作为 `table` 单元，按使用频率再晋级）。

## 仍需实测的问题

这些问题不阻止选 PostgreSQL，但会影响后续 schema 和索引细节：

1. **中文全文检索质量**：PostgreSQL 内置能力够不够，还是要 `pg_jieba`/`zhparser`/外部搜索？
2. **embedding 模型和切分长度**：若未来引入语义检索，中文财报的最佳单元/摘要长度要靠真实问答/召回样例定，不应现在拍死。
3. **`document_unit` 规模与冷热分层**：全市场多年财报的 `text/table` 单元如果增长很快，主库是否全部在线，还是把历史 `payload` 冷存到 Parquet、DB 留索引？
4. **部署方式**：本地 `brew services`、Docker、还是项目脚本启动 Postgres，需要等实现计划确定。
5. **备份与迁移**：主库、`raw_documents/`、`parser_artifacts/` 三者要保持一致备份，后续要定义 manifest/checksum。

## 参考来源

- PostgreSQL 18 文档：Full Text Search，`tsvector`/GIN/indexed search
  https://www.postgresql.org/docs/current/textsearch.html
  https://www.postgresql.org/docs/current/textsearch-tables.html
- PostgreSQL 18 文档：JSON/JSONB 设计与索引
  https://www.postgresql.org/docs/current/datatype-json.html
- PostgreSQL 18 文档：索引类型
  https://www.postgresql.org/docs/current/indexes-types.html
- PostgreSQL 18 文档：`pg_trgm`
  https://www.postgresql.org/docs/current/pgtrgm.html
- pgvector 官方仓库：向量类型、HNSW/IVFFlat、hybrid search
  https://github.com/pgvector/pgvector
- SQLite 官方选择指南与 FTS5
  https://www.sqlite.org/whentouse.html
  https://www.sqlite.org/fts5.html
- DuckDB 官方文档：数据导入、Parquet、JSON、FTS、并发模型
  https://duckdb.org/docs/current/data/overview
  https://duckdb.org/docs/current/data/parquet/overview
  https://duckdb.org/docs/current/data/json/overview
  https://duckdb.org/docs/current/core_extensions/full_text_search
  https://duckdb.org/docs/current/connect/concurrency
- Qdrant 官方文档：向量检索、payload filtering、scaling
  https://qdrant.tech/documentation/overview/
  https://qdrant.tech/documentation/search/filtering/
- PostgreSQL 中文分词扩展参考（需后续实测，不作为第一版强依赖）
  https://github.com/jaiminpan/pg_jieba
  https://github.com/amutu/zhparser
