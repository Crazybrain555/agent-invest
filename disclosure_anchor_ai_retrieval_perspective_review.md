# disclosure_anchor：按“AI 可检索性”重新审查后的补充建议

> 目的：把这轮新需求——“不是传统 RAG，不按 token chunk，而是让 AI 能按语义、结构、标题、摘要、关键词快速找到正确 document_unit”——明确写给 Claude / coding agent，并重新审查已经采纳或拒绝的修改。
>
> 结论：当前 04R/05/06/07/08 的主线仍然正确，可以开工 04R；但建议补一个轻量的 **retrieval projection / search projection** 概念。它不改变核心对象、不新增 persistent chunk、不引入向量库，却能把 `title / heading_path / semantic_key / summary / keywords / questions_answered / table headers` 这些检索信号系统化。

---

## 1. 给 Claude 的说明：隐藏的新需求是什么

### 1.1 一句话

`disclosure_anchor` 不只是“PDF → 结构化内容”的 L1 服务，它隐含的目标是：

```text
让 AI / L2 能在不重新打开 PDF、不理解 parser schema、不做传统 RAG chunking 的情况下，
通过结构化语义入口快速找到正确的披露原文单元 document_unit。
```

所以，`document_unit` 应该被理解为：

```text
durable semantic retrieval anchor
= 可追溯、不可变、按业务结构切好的语义检索锚点
```

不是：

```text
固定 token chunk
向量库文档片段
RAG 临时切片
claim / fact / event
```

### 1.2 这个需求和“不做传统 RAG”不矛盾

“不做传统 RAG”的意思不是“不做检索”，而是：

```text
不把 token chunk / overlap chunk / embedding 表变成 L1 核心对象；
不让向量切片成为证据身份；
不让检索摘要替代原始 payload。
```

真正需要的是两层结构：

```text
核心层：document_unit
- text / table / qa
- payload 是完整内容快照
- asset_id 可追溯
- content_hash 稳定
- 不按 token 拆

检索投影层：retrieval/search projection
- title / subtitle / heading_path_text
- semantic_key / controlled keywords
- optional summary / questions_answered
- table caption / headers / row labels
- 用于搜索、排序、召回、上下文包装
- 不作为事实，不进入 content_hash
```

### 1.3 title、subtitle、summary、keywords 应该怎么放

建议分类：

| 字段 | 是否应有 | 放哪 | 是否进 content_hash | 是否影响证据 |
|---|---:|---|---:|---:|
| `title` | 应有 | `document_unit.title` | 否 | 否 |
| `subtitle` / `display_subtitle` | 建议有 | search projection，可由 `heading_path[-2]` 派生 | 否 | 否 |
| `heading_path` | 已有 | core/public view | 否 | 否 |
| `semantic_key` | 已有 | core/public view，规则版本化 | 否 | 否 |
| deterministic keywords | 建议有 | search projection | 否 | 否 |
| LLM summary | 可后置 | search projection / L2 retrieval view | 否 | 否 |
| questions_answered | 可后置 | search projection | 否 | 否 |
| embedding | 后置 | retrieval index，不是 core | 否 | 否 |

关键约束：

```text
summary / keywords 可以帮助 AI 找到 unit，
但不能成为披露事实本身，不能替代 payload，不能进入 content_hash。
```

---

## 2. 外部项目给出的做法

### 2.1 Unstructured：先 partition 成元素，再 chunk；chunking 不是原始切分

Unstructured 的官方文档把 chunking 放在 partition 之后：先得到 document elements，再基于元素 metadata 后处理成 chunk；除非单个元素过长，否则 chunk 保持完整语义元素。这个方向支持当前 `document_unit` 不是 token chunk，而是结构单元。

可借鉴点：

```text
partition / normalized IR 是结构识别层；
chunk / retrieval projection 是消费层；
不要把消费层 chunk 反写成核心对象。
```

### 2.2 Docling：统一文档表示 + hierarchy + furniture/provenance

DoclingDocument 明确包含 Text、Tables、Pictures、document hierarchy、headers/footers furniture、layout 和 provenance。Docling chunker 还提供 `contextualize(chunk)`，用于生成 metadata-enriched serialization。

可借鉴点：

```text
document_unit 仍是核心 payload；
额外提供 contextualized_search_text，给检索/embedding/BM25 使用；
search_text 可以包含 metadata，但 unit payload 不被改写。
```

### 2.3 LlamaIndex：metadata extractors 是检索增强，不是事实层

LlamaIndex 的 metadata extraction 直接包括 `TitleExtractor`、`SummaryExtractor`、`QuestionsAnsweredExtractor`、`KeywordExtractor`。官方说明长文档 chunk 往往缺上下文，因此要提取额外上下文帮助检索。

可借鉴点：

```text
summary / keyword / questions_answered 是很自然的检索辅助字段；
但要标明 extractor_version / model / prompt_hash / confidence；
不要和原始披露 payload 混同。
```

### 2.4 Anthropic Contextual Retrieval：给片段补上下文，解决“孤立片段”问题

Anthropic 的 Contextual Retrieval 核心思想是：传统检索片段失去文档上下文，导致召回失败；给每个片段加上下文能显著减少 retrieval failure。

对本项目的翻译：

```text
不要只把 payload.text 拿去搜；
应该生成 contextualized_search_text：
公司 + 公告类型 + 报告期 + 标题路径 + unit title + semantic_key + payload 摘要/表头。
```

### 2.5 Haystack Auto-Merging：用层级结构做检索回填

Haystack 的 auto-merging 思路是：先检索较小子节点，如果同一父节点下命中足够多，则返回父节点。这和你的 “完整小节 / 完整表格 / 完整问答” 很一致。

可借鉴点：

```text
先返回精确 unit；
如果同一 heading_path 下多个 unit 命中，则 API 可以提供 parent_context；
parent_context 是运行时包装，不是持久化 chunk。
```

### 2.6 PostgreSQL FTS / pg_trgm / pgvector 的取舍

第一版不需要独立向量库。PostgreSQL 自带 full-text search 能做 `tsvector / tsquery` 和排序；`pg_trgm` 可做标题、关键词、中文短语的相似匹配；`pgvector` 可以后置作为同库扩展，而不是先引入独立向量库。

建议路径：

```text
Phase 06R：PostgreSQL FTS + trigram + structured filters
Phase later：pgvector / reranker 可选
不要把向量检索变成 04R/05 的前置依赖
```

---

## 3. 当前设计在“AI 可检索性”视角下的判断

### 3.1 做得好的地方

当前架构已经抓住了几个正确方向：

1. **不做 persistent chunk**：`document_unit` 按标题、完整表格、完整问答切分，运行时 context packaging 可临时截取。
2. **检索键已经有雏形**：`company_ref / security_ref / filing_type / report_period / announcement_date / payload_kind / heading_path / semantic_key / quality_status / title / asset_id` 基本覆盖了 L2/AI 检索的第一层过滤。
3. **三哈希分层是正确的**：`content_hash` 不含 title/heading_path/semantic_key；`query_projection_hash` 捕捉 title/heading_path/semantic_key/quality_status 的变化。
4. **旧 asset_id 永远可解析**：这保证了检索结果、L2 引用和证据链不会因为 active run 切换而断。
5. **不把 vector / embedding 放进核心**：现在不做全文/向量检索，不是方向错，而是还没到 search projection 阶段。

### 3.2 还欠缺的地方

主要欠缺不是“要不要 chunk”，而是没有把检索投影建模清楚：

```text
title 有了，但 subtitle/display path 没有；
semantic_key 有了，但 keywords/tags 没有分层；
payload 有了，但 search_text 没有；
API 有 list/filter，但没有 search endpoint；
query_projection_hash 有了，但 summary/keywords/search_text 的变更归属没定义；
acceptance matrix 里没有“检索质量”验收项。
```

这会导致后续 AI agent 只能靠：

```text
heading_path exact/prefix + semantic_key + title contains
```

这对规则化场景足够，但对自然语言问题不够。例如：

```text
“公司有没有说美国关税影响？”
“哪个表能看到应收账款账龄？”
“这份公告里有没有退市风险？”
“AI 服务器相关产品进展在哪里？”
```

这些问题需要一个搜索投影，而不是传统 RAG chunk。

---

## 4. 建议新增：retrieval projection，不新增核心 document_unit 类型

### 4.1 名称

建议叫：

```text
search_projection
或 retrieval_projection
```

不要叫：

```text
chunk
embedding_document
rag_node
knowledge_block
```

### 4.2 最小字段

可以先作为 view / API serialization 字段，后续证明需要索引再落表：

```text
asset_id
processing_run_id
document_id
is_active_run
company_ref
security_ref
filing_type
report_period
announcement_date
payload_kind
heading_path
heading_path_text
title
display_subtitle
semantic_key
semantic_key_version
quality_status
content_hash
query_projection_hash
retrieval_projection_hash
search_text
search_keywords
summary
questions_answered
matched_fields / debug fields（API 返回用）
retrieval_rules_version
retrieval_generated_by
retrieval_generated_at
```

其中：

```text
heading_path_text = join(heading_path, " / ")
display_subtitle = heading_path[-2] if len(heading_path) >= 2 else document.title
```

### 4.3 search_text 怎么生成

按 payload_kind 区分：

```text
text unit:
  document title
  company/security
  filing_type/report_period/announcement_date
  heading_path_text
  title
  semantic_key label
  first N chars or deterministic extractive summary

table unit:
  document title
  heading_path_text
  table title/caption
  unit
  headers
  row header labels
  notes
  semantic_key label
  不默认塞全部 cell 值，避免 search_text 巨大；完整 payload 仍在 unit

qa unit:
  document title
  heading_path_text
  question 高权重
  answer deterministic summary or first N chars
  semantic_key label
```

### 4.4 keywords 的分层

不要把 title 简单塞进 keywords。建议三类：

```text
controlled_keywords
- 来自 semantic_key registry / filing_type / heading rules
- 稳定、版本化、可解释

extractive_keywords
- 从 title / heading / caption / question / table header 中抽取
- 规则生成，版本化

llm_keywords
- 后置，可选
- 必须保存 model/prompt/rules version
- 不作为事实，不进 content_hash
```

第一版可以只做前两类。

### 4.5 summary 的边界

summary 很有价值，但边界要清楚：

```text
第一版：可以先做 deterministic/extractive summary
- text: 前 N 字 + 关键句规则
- table: caption + headers + unit + notes
- qa: question + answer 前 N 字

后置：LLM summary
- 作为 retrieval_projection，不进入 document_unit.payload
- 生成时记录 model_id / prompt_hash / input_hash / summary_hash
- 变化只触发 search projection rebuild，不触发 L3 fact invalidation
```

### 4.6 新增 API

建议放在 06R 或 06.5，不阻塞 04R/05：

```text
GET /v1/search/units?q=&company_ref=&security_code=&filing_type=&report_period=&payload_kind=&semantic_key=&heading_prefix=&limit=
```

返回：

```json
{
  "asset_id": "du_...",
  "asset_uri": "asset://...",
  "document_id": "doc_...",
  "payload_kind": "table",
  "title": "应收账款按账龄披露",
  "heading_path": ["第八节 财务报告", "应收账款", "按账龄披露"],
  "semantic_key": "receivable_aging",
  "quality_status": "ok",
  "rank_score": 0.83,
  "matched_fields": ["title", "headers", "semantic_key"],
  "snippet": "应收账款 / 按账龄披露 / 单位：元 / 1年以内...",
  "is_active_run": true
}
```

硬规则：

```text
search endpoint 只负责发现；
证据读取仍必须 GET /v1/units/{asset_id}；
search snippet 不能作为证据 snapshot。
```

---

## 5. 对之前 5 个拒绝/改造项的再判断

### 5.1 E12：`GET /v1/units/{asset_id}` 不默认只返回 active ——拒绝正确，且在检索视角下更正确

AI 检索结果和 L2 证据都会持有 `asset_id`。如果 active run 切换后旧 asset_id 不可解引用，检索缓存、人工复核和历史证据都会断。

最终裁决保持：

```text
GET /v1/units/{asset_id} 永远按 ID 解引用；
返回 is_active_run；
列表接口默认 active；
单 ID 解引用不默认 active。
```

### 5.2 B4：taxonomy 加 list/chart/code/footnote ——当前裁决要微调

你们拒绝扩 `document_unit.payload_kind` 是对的：payload_kind 继续只允许 `text/table/qa`。

但“parser 不产出 list/chart/code/footnote，所以不建枚举”这个理由不宜写死。更稳的说法是：

```text
不扩 payload_kind；
NormalizedIR.kind 维持小集合；
raw_kind 保留 parser 原始类型；
未知或未显式枚举 raw_kind 不得静默丢弃；
有可读内容的 list/chart/code/footnote 类输入应 fallback 成 text/table/needs_review。
```

另外，你当前文档有一个小不一致：04R-D9 写了 `equation`，05 前置也写了 `equation`，但 04R-R5 的 kind 集合漏了 `equation`。需要统一。

建议把 04R-R5 改为：

```text
kind ∈ {text, heading, table, image, equation, page_furniture, unknown}
```

并补一句：

```text
raw_kind 可为 list/chart/code/footnote 等 parser 原始类型；当前不升为中立 kind，但必须保留 raw_kind 并按 fallback 规则处理。
```

在检索视角下，`footnote / list / chart caption` 经常是高价值检索入口，不能因为不是 payload_kind 就消失。

### 5.3 E3：不加 tier_unknown ——拒绝正确

`tier_unknown` 不应污染顶层协议枚举。写侧 provider whitelist / quarantine 更干净。

但要确认：

```text
manual local pdf 必须声明 official provider 或 official source_url；
不能 provider=local + filing_type=other 自动 tier_0a。
```

检索视角下，source_tier 会成为筛选/排序因子，错误 tier 比空 tier 更危险。

### 5.4 archived + latest_processing_run_id ——推迟正确

当前不需要 archived，也不需要 document.latest_processing_run_id 存储列。

但 search projection 后续可能需要：

```text
document_processing_state_v1
```

用于展示：

```text
active_run_id
latest_run_status
latest_failed_run_id
has_search_projection
search_projection_version
```

这可以先做 view，不落 core document。

### 5.5 S2：保留 heading_path GIN ——可以，但必须明确查询语义

在检索视角下，heading_path 是非常重要的。保留索引可以接受。

但需要把 API 语义写死：

```text
heading_path containment/jsonpath?
heading_path prefix?
heading_path_text title contains?
```

建议：

```text
v1 API 支持 heading_path prefix；
实现上用 heading_path_text 或 text[] 前缀更清楚；
jsonb_path_ops 可保留，但不要把它当成 prefix 查询的唯一长期方案。
```

---

## 6. 对当前 milestone 的具体补丁建议

### 6.1 04R 补丁

#### 04R-D9 / R5

改掉“parser 不产出 list/chart/code/footnote”的绝对表述。建议改为：

```text
NormalizedIR.kind 维持小集合，不等同 parser raw type；
parser raw type 一律进入 raw_kind；
raw_kind 出现 list/chart/code/footnote 等未知或暂不支持类型时，
mapper 不得静默丢弃；builder 按 text/table/needs_review fallback。
```

同时统一 equation：

```text
D9 / R5 / 05 前置依赖 / schema 四处 kind 集合必须一致。
```

#### 04R-R1

不建议现在就把 summary/keywords 加进 core.document_unit。可以预留：

```text
query_projection_hash 仍只覆盖 title/heading_path/semantic_key/quality_status；
search_projection_hash 后续 06R 再加。
```

原因：summary/keywords 规则还没定，过早落 core 可能制造迁移债。

### 6.2 05 补丁

在 05 增加一个 S9，名字可以叫：

```text
S9 search projection artifact（不落 DB 或仅落 artifact）
```

第一版只生成 artifact：

```text
unit_search_projection.jsonl
```

每行：

```json
{
  "asset_id": "du_...",
  "heading_path_text": "第八节 财务报告 / 应收账款 / 按账龄披露",
  "display_subtitle": "应收账款",
  "search_text": "...",
  "controlled_keywords": ["receivable_aging", "应收账款", "账龄", "坏账准备"],
  "extractive_keywords": ["1年以内", "3年以上", "合计"],
  "summary": null,
  "retrieval_rules_version": "retr-2026.07-1"
}
```

这不需要先做搜索 API，但会逼 builder 在生成 unit 时顺手生成检索投影，后面 06R 可直接索引。

### 6.3 06 补丁

06 现在“明确不做全文检索/向量检索”可以保留，但建议改成：

```text
Phase 06 不做全文/向量检索；
但 06 的 DTO/API 必须不阻断 06R retrieval projection；
保留 GET /v1/search/units 作为后置 milestone。
```

同时在 06 或 06R 追加：

```text
GET /v1/units/{asset_id}/context
```

应返回 metadata-enriched context：

```text
document metadata + heading_path + title + unit payload excerpt/full payload
```

这已经在实施方案里有，但 06 文件当前 endpoint 清单没有 `/context`。建议统一。

### 6.4 acceptance-matrix 补丁

新增 3 行：

```text
A38 retrieval projection artifact：每个 published unit 有 search projection，含 heading_path_text/title/semantic_key/search_text/keywords/version
A39 search retrieval smoke：按自然语言关键词能找到已知样本 unit（receivable aging / tariff exposure / risk notice）
A40 search projection 不污染 evidence：summary/keywords 不进 content_hash，unit payload 不变时 search projection rebuild 不触发 L3 materialized invalidation
```

---

## 7. Claude 执行建议

给 Claude 的执行口径可以直接写成：

```text
请不要把“AI 可检索性”理解为传统 RAG 或向量库。
当前系统的核心仍是 document_unit：完整 text/table/qa，按业务结构切分，不按 token chunk。
但为了让 AI/L2 能准确找到 unit，需要新增 retrieval/search projection 概念。

检索投影只服务于发现：title、subtitle、heading_path_text、semantic_key、controlled_keywords、
extractive_keywords、summary/questions_answered/search_text。它不替代 payload，不作为 claim，
不进入 content_hash，不改变 asset_id 的解引用语义。

04R/05 的实现不能新增 chunk/table_cell/embedding 核心对象。
05 可以先生成 unit_search_projection artifact；06R 再实现 PostgreSQL FTS/trigram 的 search endpoint。
如果未来加入 LLM summary/keywords，也必须以 retrieval_projection 形式版本化保存，记录 model/prompt/hash，
并且不得触发 L3 fact invalidation。
```

---

## 8. 最终结论

当前架构在你的新需求下不是错的，反而方向更清楚了：

```text
不要传统 RAG chunk；
要 durable semantic document_unit；
再叠一层 retrieval projection。
```

现在不建议推翻 04R/05/06。建议只做三类小修：

```text
1. 修 04R-D9/R5 的 raw_kind / equation / unknown fallback 表述；
2. 在 05 增加 search projection artifact，不进 core content_hash；
3. 在 06/06R 规划 search endpoint + FTS/trigram，后置向量与 LLM summary。
```

这样既保住 L1 的确定性和追溯性，也让后续 AI agent 能真正“按语义找原文”。

---

## 参考资料

- Unstructured Chunking: https://docs.unstructured.io/open-source/core-functionality/chunking
- Docling Document: https://docling-project.github.io/docling/concepts/docling_document/
- Docling Chunking: https://docling-project.github.io/docling/concepts/chunking/
- LlamaIndex Metadata Extraction: https://developers.llamaindex.ai/python/framework/module_guides/indexing/metadata_extraction/
- Anthropic Contextual Retrieval: https://www.anthropic.com/engineering/contextual-retrieval
- Haystack Auto-Merging: https://haystack.deepset.ai/blog/improve-retrieval-with-auto-merging
- PostgreSQL Full Text Search: https://www.postgresql.org/docs/current/textsearch-controls.html
- PostgreSQL pg_trgm: https://www.postgresql.org/docs/current/pgtrgm.html
- pgvector: https://github.com/pgvector/pgvector
