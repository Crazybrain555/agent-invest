---
id: disclosure_anchor
title: disclosure_anchor 服务目的
contract_version: v1.2
status: canonical
layer: L1
layer_name: 披露文件接入与结构化准备层
reference: ../../docs/reference/投研预测引擎顶层框架协议_v0.8.md
delivers_to: L2
scope: self_maintained_exchange_disclosures
output_kind: l2_ready_document_units
output_form: queryable_database_plus_filing_api
payload_kinds: [text, table, qa, mixed]
query_keys: [company_ref, security_ref, report_period, announcement_date, filing_type, document_id, asset_id, payload_kind, heading_path, semantic_key, semantic_keys, section_keys, applicability, page_no, quality_status, content_hash, source_ref, producer_action_ref]
core_objects: [company, security, source_access, document, processing_run, document_unit]
optional_objects: [source_checkpoint, provider_category]
primary_store: postgresql
raw_store: filesystem
parser_artifact_store: filesystem
run_model: scheduled_incremental_polling
operator_input: [tracked_companies]
external_provider_relation:
  same_logical_layer: true
  owned_by_this_service: false
  integration_boundary: dataset_api_and_l2
not_core_objects: [page_idx, bbox, chunk, table_cell, embedding]
not_produces: [standard_financial_dataset, event_fact, metric_observation, claim, metric_normalization, reconciliation, adjudication, prediction]
---

# disclosure_anchor 服务目的

> 本文件是 `disclosure_anchor` 的 canonical 契约。其他架构、数据库、解析、接口和实施文档应以本文件的术语、边界和硬决策为准。

## 0. 一句话定义

`disclosure_anchor` 是投研预测引擎 `L1` 中负责**交易所披露文件**的接入与结构化准备服务。

它把公告和财报从原始 PDF 处理成：

```text
可查询的 document
+ 按文档结构切好的 document_unit
+ 可复现的处理记录
```

然后交给 `L2` 做高价值证据化：抽取 observed claims、登记 numeric observations、识别事件候选、
判断重要性并进入观测证据账本。

它不是 PDF 文件夹，不是 RAG 知识库，不是财务数据仓库，也不是事实层。

---

# 1. 服务在总系统中的位置

## 1.1 与标准数据 provider、API、MCP 的关系

标准数据 provider（Wind / Tushare / 同花顺 iFinD / Choice 等）、其他数据 API、MCP、Web 和公告 PDF 在逻辑上都属于 `L1` 来源接入层，但不需要使用同一种物理存储方式。

| L1 标准数据侧 | L1 披露文件侧（本服务） | L1 其他来源（非标，旁路） |
| --- | --- | --- |
| Wind / Tushare / 同花顺 iFinD / Choice / API | CNINFO / 交易所 PDF | 非标 API / MCP / Web / 搜索 |
| `Dataset API` | `disclosure_anchor` → `document` / `document_unit` → `Filing API` | 其他 L1 adapter → `data_asset` / `source_access` |

三条路径最终都在 `L2` 汇合 →（证据化 / 口径归一 / 冲突 / 采信）→ `L3` 派生证据。

本服务只负责其中的披露文件侧（自维护公告 / 财报 PDF）。

以下原则必须保持：

- **同一逻辑层，不等于同一数据库表。**
- **标准数据侧是 provider 无关的。** Wind 只是首版示例，可整体或按 dataset 替换 / 并存为 Tushare、同花顺 iFinD、Choice 等；抽象由 `dataset_registry + provider_adapter` 承担，`dataset_key` 不绑定具体 provider。
- 标准数据 provider 已稳定覆盖的标准财务数据，通过 `Dataset API` 使用；本服务不把 PDF
  内容再建设成第二套**标准化 dataset**，但 PDF 中实际出现的表格和正文仍作为可检索
  `document_unit` 证据发布。
- PDF 中所有有实质内容的表格、正文和视觉载体都进入结构化证据链；是否已被 provider 覆盖、
  是否“对预测有价值”只影响 L2 路由、排序和采用，不影响 L1 证据是否存在。
- 非标准 API、MCP、Web 查询、搜索、新闻等一次性来源，由对应 L1 adapter 登记为 `data_asset` 或
  `source_access` 后进入 `L2`；第一版不要求把它们转成 `document_unit`，也不由本服务承担（详见
  《财报与披露数据接入及切分方案》§1.2）。
- 上述路径都在 `L2` 汇合，不在底层强行统一原始形态。

## 1.2 与 L2 的分界

本服务做的是**文档结构切分与载体规范化**；`L2` 做的是**高价值证据化与语义归一**。

```text
L1 disclosure_anchor
PDF → 标题树 / 完整结构 evidence block（text / table / mixed）

L2
一个 document_unit → 可选 Q&A / event / fact 抽取 → 0 到多条 evidence_record
（observed_claim / relation_claim / numeric_observation 等）
```

因此：

> `document_unit` 是最小的 L1 可寻址文档单元，不是最小事实，也不是 claim。

一个包含完整问答上下文的结构块、一个经营分析小节或一张应收账款账龄表，都可能在 `L2` 中产生多条
`evidence_record`。

---

# 2. 核心设计原则

## 2.1 预测用途优先

任何解析、切分和存储对象都必须能说明它如何帮助：

- L2 更快找到原始依据；
- L3 更可靠地形成证据；
- L5 更准确地维护预测。

不能改善上述目标的对象，不进入第一版核心模型。

## 2.2 原文件不可变，派生结果可重跑

服务内部只有两类资产：

```text
不可变资产
- 原始 PDF
- 文件哈希
- 来源和获取记录

可重生成资产
- Markdown / JSON / HTML 等 parser artifact
- document_unit
- semantic_key
- 查询索引
```

原始 PDF 只追加，不覆盖。解析器升级时生成新的 `processing_run`，不回改旧运行结果。

## 2.3 按业务结构切，不按版面和 token 切

正文按 PDF 中可回放的标题层级与标题 occurrence 切分。

**单元边界是业务语义块，不是 parser 元素**（2026-07-06 phase008 审查定案）：
一个 unit 必须表达一个完整的章节证据块——大到足以让 L2 命中其中任一内容后取得该真实章节下
连续出现的文字、表格和视觉证据。payload kind（text/table/mixed）、caption 文本、监管 taxonomy、
页码、字符数和 token 数都不决定边界。边界只来自可回放的源结构：

- 有 typed heading 时，以完整标题树中的**具体 occurrence**为边界；相同标题文字在不同位置仍是
  不同 occurrence；
- 同一 occurrence 下连续出现的 text/table/image 合成一个 `mixed`，parts 保持原顺序、原 payload、
  适用性和各自 locator；遇到下一个真实 heading 才闭合；
- 没有可靠 heading 的内容进入 `title=null, heading_path=[]` 的 coarse root unit；登记文档标题只留
  document scope，不能复制成 Unit 标题，也不能把 table/image caption、单位、脚注或业务短语
  提升为虚构章节；
- 原生文字 occurrence 的几何不可用时，保留同页其余可定位文字，并以类型化 issue + hash-bound
  无损整页图闭合；视觉护栏补足证据模态但不产生标题、父子关系或业务边界；
- 粒度过粗时由 L2 基于真实标题路径和逻辑表格行做临时 context packaging；不得反过来用
  L2 taxonomy、内容词表、页码或固定阈值改写 L1 证据边界。

第一版不把以下对象作为长期数据模型：

```text
page_idx
bbox
固定 token chunk
overlap chunk
parser block
单元格级 table_cell
```

agent 运行时为了控制上下文长度，可以临时合并、截取或拆分内容；这是 context packaging，不是持久化证据对象。

检索策略同理分两层：`document_unit` 是证据锚点（durable semantic retrieval anchor），
其上可叠加 retrieval/search projection 派生发现层（heading_path_text、keywords、search_text、
可选 summary），帮助 AI/L2 按语义找到 unit。投影不进 content_hash、不替代 payload、不作为
证据；不引入 persistent chunk / RAG node / 独立向量库（边界与实现见 milestone 05-U7 / 06R；
06R 为规划中的检索投影里程碑，规格文档尚未编写）。

L2 的上下文包与 L1 的持久化边界不得混为一谈：L1 按源标题 occurrence 保留可寻址
证据；L2 按最终模型 tokenizer 对渲染后的 prompt +
sources 计数，并预留输出/工具开销。ub-2026.07-48 的 1,371 份历史离线重放按展开表格字符
估算时呈双峰：1,292 份正文
不超过 40,000 字符，79 份年报/半年报从 103,354 到 388,948 字符，两组之间无样本。
这只是 corpus 分层信号，不是 token 阈值：短文档在实际预算内可整篇，长文档用 12–20k
token 的 section packs；超长表格另建 1.5–3k token 的连续行窗口，重复 caption/表头/路径且
不拆逻辑行。多文档联合抽取共享总 token budget，并保留每个 asset_id/source_ref。
不得把“少于 50k tokens”实现成一个 L1 巨型 unit。完整四档决策见
`docs/implementation/design/retrieval-and-semantic-keys.md` §6.2。

## 2.4 表格先保留完整结构，不急于全市场标准化

PDF 表格默认保存为完整 `table` part（无同节相邻内容时也可单独成为 unit）：

- 源 `table_caption`（通常只是关联文本，不假定为表名；唯一的强编号标题子 occurrence
  例外见 §8.3）；
- 标题路径；
- 单位；
- 表头；
- 行数据；
- 脚注或邻近解释；
- 原始字符串；
- 质量状态。

不拆成数据库单元格，不强行建立全市场统一产品分类。

只有一种表经过真实使用证明可反复复用后，才在更上层晋级成标准 dataset。

## 2.5 不重复建设标准数据

三大报表、标准财务指标、业绩预告、业绩快报、审计意见等，若标准数据 provider 已稳定覆盖，
本服务不从 PDF 重建第二套统一口径、可替代 Dataset API 的标准表；但它们在 PDF 中的原始
标题、表格、脚注和说明仍按源结构发布为可查询证据。`provider-covered` 是检索/采用 facet，
不是 suppress 规则。

## 2.6 L1 不判断真伪和重要性

本服务可以做：

- 确定性去噪；
- 标题树识别；
- 表格抽取；
- 对 parser 已给出的结构关系做可回放校验；
- 仅对有封闭 source type、位置和重复证明的外部版面元数据做抑制；
- 粗粒度 `semantic_key` 标注；
- 解析质量标记。

本服务不做：

- 哪条信息值得改变预测；
- 管理层解释是否可信；
- 一个产品应归入哪个预测节点；
- 数字应采用什么最终会计口径；
- 冲突裁决；
- 事实采信；
- 事件 canonical 化；
- claim 抽取和入账。

## 2.7 轻量化不能以证据不可见为代价

原 PDF 和 parser artifact 不是 L2 自动检索的替代品。所谓“去掉废话”只允许发生在派生的
检索排序、上下文组包或 L2 采用阶段：

> L1 仍发布每个有实质内容、可定位的 source carrier；检索投影可降低模板性内容的排序权重，
> section pack 可按请求裁剪，但 canonical evidence、title/path 和 locator 不得因重要性判断消失。

---

# 3. 服务范围

## 3.1 范围内

- 年度报告；
- 半年度报告；
- 季度报告；
- 业绩预告和业绩快报原文；
- 投资者关系活动记录；
- 业绩说明会记录；
- 问询函、监管函及回复；
- 分红、回购、定增、股权激励、重大合同、投资扩产、并购重组等公告；
- 其他交易所或上市公司正式披露文件。

## 3.2 范围外

- Wind、Tushare、同花顺 iFinD、Choice 等标准数据集的全量镜像；
- Web、MCP、新闻和研报的通用接入；
- 人工纪要、微信群、录音转写等非披露资料；
- claim、证据账本、假设账本和预测快照；
- 估值、选股和交易判断。

这些对象可以和本服务共享公司主数据、来源登记或调用接口，但不属于本服务自身职责。

---

# 4. 输入与运行方式

## 4.1 常规输入

人工长期维护的输入只有：

```text
tracked_companies
```

即需要持续跟踪的公司或证券代码清单。第一版从一开始就是 **≥500 只人工精选**（研究员精选名单录入），不是先跑几家的试点；它定义第一版的覆盖范围，但仍小于全市场全 A（~5000+），后者是非目标。

清单真源是 DB 的 `tracked_company`（round22 裁决）；维护路径三条等价：
`PUT /v1/admin/tracked-companies`（服务/AI 程序化）、`make track CODES=...`（快捷入池）、
watchlist.csv + `make track`（批量导入/恢复，`make track-export` 回写 git 快照）。
读侧：`GET /v1/tracked-companies` / `tracked_companies_v1` 视图。
删除三层语义（round22）：`status=paused` 可逆停；`DELETE /v1/admin/tracked-companies/{code}`
（= `make untrack`）删订阅行、公司与已获取文档留档（下载队列只放行有 active 行的公司）；
`make purge-company`（测试期专用 CLI）单公司级联清除——登记账本按 GLEIF 模式从不经
运营路径删除。

其他参数均为有默认值的配置：

- 市场；
- 公告类别；
- 历史回看范围；
- 同步频率；
- 优先级；
- 下载策略；
- 解析策略；
- 单元保留策略。

## 4.2 运行模式

```text
维护公司清单
→ 增量同步公告索引
→ 识别新公告或新文件版本
→ 下载原始 PDF
→ 保存文件与哈希
→ 执行 parser
→ 机械清洗和结构切分
→ 生成 document_unit
→ 发布当前 active processing run
→ 供 Filing API 查询
```

支持一次性人工 seed，但仍走同一管道：

- 指定公告 ID；
- 指定 URL；
- 指定本地 PDF；
- 指定公司和报告期重跑。

---

# 5. 输出契约

本服务对外只交付两类结果。

## 5.1 文档结果

`document` 表示一份具体的披露文件版本，至少能回答：

- 哪家公司；
- 什么公告；
- 公告日期；
- 报告期或事件时间；
- 来自哪个来源；
- 原始文件在哪里；
- 文件哈希是什么；
- 当前是否下载、解析和发布成功。

`document.status` 是 **public availability state**（04R-D4/B1，随 0007 加 CHECK）——只回答
"public 契约下这份文档现在可消费吗"，不代表最近一次 run 的结果：

```text
registered → parsed | parse_failed →（发布后）published
published 后重解析失败不降级：status 保持 published，旧 active run 继续可读，
失败只体现在 processing_run 层（unit_build_status / failed run + observed 事件）
```

`report_period` 可空（B8）：定期报告（annual/semiannual/quarterly_report）必填；
业绩预告/快报/说明会建议填；临时公告（investor_relations/inquiry_reply/other 等）可为 null，
public view 保留该列且允许 null，不得伪造 period。半年报按顶层协议 §2.5 的 period label
词表登记为 `YYYYQ2`（例如 2025 年半年报为 `2025Q2`），不引入 `YYYYH1` 形态。

`filing_type` 初始词表（04R-D7；新增值走契约升版，禁止自由字符串）：

```text
annual_report / semiannual_report / quarterly_report / performance_forecast /
performance_flash / investor_relations / performance_briefing / inquiry_reply / other
```

`source_tier` 派生映射（04R-D2，协议 §2.9；视图 CASE 派生，本表是唯一权威）：

```text
filing_type ∈ {investor_relations, performance_briefing} → tier_0b（软披露锚）
其余                                                      → tier_0a（硬披露锚）
```

## 5.2 文档单元结果

`document_unit` 表示这份文档中供 L2 直接使用的结构单元。

第一版有四种（`mixed` 自迁移 0011 起加入，见 §6.5）：

```text
text
表述性正文、章节、子标题或完整事项

table
保留结构的完整表格

qa
一个完整 Question + Answer

mixed
一个业务语义块内 text/table 交替的有序组合（议案、短公告整体、业务小节），定义见 §6.5
```

没有 `event_unit`。短公告中的事件字段由 `L2` 从 `text/table` 单元中抽取，形成事件类
`evidence_record` 或后续派生对象。

术语对齐顶层协议（收敛完成于 v0.7 时期，迁移 `0006_v07_terminology_convergence`；现行 v0.8 同口径）：

- 本服务代码、数据库列与 public view 统一使用 `payload_kind`；`unit_kind` 为曾用名，不再出现在任何契约面；
- 当 `asset_kind = document_unit` 时，`payload_kind` 即顶层协议的同名字段，无需映射；
- `document_unit` 的稳定 ID 统一为 `asset_id`（曾用名 `document_unit_id` / fixture `unit_id`），ID 取值不变。

---

# 6. document_unit 的定义

## 6.1 通用字段

一个 `document_unit` 至少具有以下语义：

```text
asset_id
所属 document
所属 processing_run
payload_kind
heading_path
title
order_index
semantic_key（可选；只有存在受控的真实 Unit 级路由键时才填写）
semantic_keys（可选；完整有序路由集，首项等于 semantic_key）
section_keys（可选；从可靠 heading_path 精确归一的完整结构位置路由）
payload
content_hash
quality_status
artifact_locator（可选）
```

这里描述的是逻辑契约，不是 SQL DDL。

## 6.2 text 单元

适用于：

- 主营业务；
- 行业情况；
- 产品进展；
- 价格、销量、订单、产能和客户解释；
- 毛利率、费用、现金流和资产负债变化原因；
- 风险因素；
- 未来展望；
- 重大事项说明；
- 问询函中的单个问题或回复章节。

示例：

```json
{
  "payload_kind": "text",
  "heading_path": [
    "第三节 管理层讨论与分析",
    "一、报告期内公司从事的主要业务"
  ],
  "title": "报告期内公司从事的主要业务",
  "payload": {
    "text": "报告期内，铝电解电容器……"
  }
}
```

## 6.3 table 单元

适用于：

- 分行业、分产品、分地区收入和毛利率；
- 产量、销量、库存量；
- 成本构成；
- 客户和供应商集中度；
- 应收账款账龄和坏账准备；
- 存货分类和跌价准备；
- 固定资产、在建工程、债务结构；
- 分部、子公司和研发项目等。

示例：

```json
{
  "payload_kind": "table",
  "heading_path": [
    "第八节 财务报告",
    "财务报表附注",
    "应收账款",
    "按账龄披露"
  ],
  "title": "按账龄披露",
  "payload": {
    "table_body": "<table><tr><th>账龄</th><th>期末账面余额</th><th>期初账面余额</th></tr><tr><td>1 年以内（含1 年）</td><td>1,765,831,017.43</td><td>1,653,778,854.38</td></tr></table>",
    "table_caption": ["按账龄披露"],
    "table_footnote": []
  }
}
```

新 writer 保留 MinerU 3.4.4 Hybrid-medium merge-on 的 provider-native 表达：非空
content-list owner 携带唯一可见/可检索的原始聚合 HTML；后续空 stub 不另发正文，而在 locator
中连接逐页 physical segment、crop、page/bbox 和 raw hash。L1 不把 HTML 解析成 canonical grid，
不修 cell，不用 middle HTML 覆盖 owner，也不按相似度自行跨页合表。关系不闭合时保留原始
ProviderDocument occurrence 并 fail closed 或 `needs_review`，不得造逻辑 owner。

## 6.4 qa 单元

> 历史值：旧产物存在、2026-07-16 起不再产出（QA 判别已移除，转写以 raw text 落地）。以下 schema 仅供历史行解读，新 run 不再生成 qa 单元。

旧值曾用于投关记录、业绩说明会和公开交流问答；现行 L1 不再生成该类型。

```json
{
  "payload_kind": "qa",
  "heading_path": ["投资者关系活动主要内容介绍"],
  "title": "美国加征关税对公司有什么影响？",
  "semantic_key": "tariff_exposure",
  "payload": {
    "question": "美国加征关税对公司有什么影响？",
    "answer": "美的集团是一家覆盖智能家居、新能源及工业技术、智能建筑科技、机器人与自动化、健康医疗、智慧物流等业务的全球领先的科技集团，已建立ToC与ToB并重发展的业务矩阵，既可为消费者提供各类智能家居的产品与服务，也可为企业客户提供多元化的商业及工业解决方案。目前，公司业务遍及200多个国家和地区，其中美国收入占比很低。在海外设有22个研发中心和23个主要制造基地，遍布南美洲、北美洲、欧洲、亚洲、非洲等区域的十多个国家。未来，公司还将持续拓展海外制造布局，推动海外新工厂的建设与投产。美的持续加强自有品牌产品研发投入，并通过本地化用户洞察与创新，不断完善全球各区域产品布局和产品竞争力，2024年美的系自有品牌在多个国家和多个家电品类均取得市场突破，如美的系冰箱产品在马来西亚、沙特、智利等国家取得市场份额第一，在越南、泰国等国家提升至市场份额第二；美的系洗衣机产品在马来西亚和沙特的市场份额分别达到第一和第二；家用空调产品在巴西、埃及的市场份额连续多年位居第一；此外，美的系微波炉、洗碗机、风扇、电压力锅等品类产品在部分新兴市场国家的市场份额亦位居前列。"
  }
}
```

> 示例取自美的集团（000333）2025 年 4 月 11 日投资者关系活动记录表（2024 年度业绩说明会，编号 2025-2）的第 1 问。

历史 qa 行中的回答即使很长也不按 token 拆碎。新 run 则按真实 heading occurrence
保留问句、回答和表格上下文，由 L2 抽取多条 `evidence_record`。

## 6.5 mixed 单元

适用于同一个已证明结构区间内 text 与 table（或 image）交替的场景：短公告整体、
股东会/董事会的一个 source-proved section、年报里的一个业务小节（研发投入、附注某科目）。
payload 是有序 parts；每个 part 只保存 source-bound 浅内容字段，不重复类型标签。
精确 MinerU type 留在 hash-bound ProviderDocument，owner/evidence 的粗粒度
`text|table|visual` kind 与 source block 索引只存在 Unit locator；顶层 text/table 的类型由
`payload_kind` 唯一表达。
视觉 part 额外保存内容型 artifact 的 `{sha256,size_bytes,media_type}`，使图像变化进入
`content_hash`；路径、crop、bbox、search binding 与 supporting evidence 只在 Unit 顶层
`provider_unit_locator.v3`，不复制到每个 part，也不形成第二套证据图。

```json
{
  "payload_kind": "mixed",
  "heading_path": ["二、议案审议情况"],
  "title": "二、议案审议情况",
  "payload": {
    "parts": [
      {"text": "审议结果：通过\n表决情况："},
      {"table_body": "<table><tr><td>A股</td><td>99.98%</td></tr></table>", "table_caption": [], "table_footnote": []},
      {"text": "会议决定，聘请天健会计师事务所……"}
    ]
  }
}
```

`document|section` 不作为 payload 字段重复保存：是否属于结构 section 已由 `title`、
`heading_path` 和 locator 中的 source-bound heading chain 唯一表达；无标题的根区间自然是
document preamble。监管 taxonomy 可在组装完成后帮助 L2 路由，但不得反向决定 section 边界。
单元级 `applicability` 只从该 CoarseUnit 自己拥有的 ProviderBlock 中投影：仅受控的
`checked + 适用 / unchecked + 不适用` 成对勾选可确定 `applicable`，反向勾选可确定
`not_applicable`；多个有效声明必须一致。普通“适用/不适用”文字、双选、双空、冲突，
以及只出现在祖先标题、相邻 Unit 或上一页的声明一律保持 NULL，原始细节仍由 payload
与 source-bound locator 承载。

---

# 7. heading_path 与 artifact_locator

## 7.1 heading_path

`heading_path` 是**逻辑文档地址**，表示一个单元位于哪条标题层级下。

以江海股份（002484）2025 年年度报告为例，浅层的管理层讨论小节：

```text
第三节 管理层讨论与分析
  └─ 一、报告期内公司从事的主要业务
```

```json
[
  "第三节 管理层讨论与分析",
  "一、报告期内公司从事的主要业务"
]
```

财务附注里的表格则是更深的层级。Provider writer 只允许严格 typed field/header 或其他
Unit-local 受控 witness 产生粗主题 `semantic_key(s)`；普通 table text/data cell 不造 route，
具体数值口径、事实性与业务解释仍留给 L2：

```text
第八节 财务报告
  └─ 财务报表附注
       └─ 应收账款
            └─ 按账龄披露
```

```json
[
  "第八节 财务报告",
  "财务报表附注",
  "应收账款",
  "按账龄披露"
]
```

它用于：

- agent 查询；
- L2 路由；
- 结构导航；
- 去重和候选匹配；
- 人类理解上下文。

`section_path` 是已废弃的曾用名，统一使用 `heading_path`，避免被误解为文件系统路径。

## 7.2 artifact_locator

`artifact_locator` 是可选的**技术位置**。新产物使用闭合的
`provider_unit_locator.v3`，绑定 `provider_document.v1` hash、source block index + payload ordinal、标题链、
Unit parts、物理表格段、evidence digest 与显式 search target。`title`、heading_path 或 caption
发生争议时，必须沿 locator 回到 ProviderDocument、MinerU 原始 artifact 和不可变 PDF 查证；
缺 locator 或源字段不是“保守猜一个值”的理由，而是 parser 质量故障。

它可以包含：

```text
contract_version
provider_document_sha256
heading_chain
parts / physical_table_segment_indices
evidence_artifacts
search_targets
source_text_reconciliations（仅 native-PDF 数字校正时）
```

示意：

```json
{
  "contract_version": "provider_unit_locator.v3",
  "provider_document_sha256": "sha256:...",
  "heading_chain": [{"source_index": 42, "placement_source": "numbering"}],
  "parts": [{"part_index": 0, "block_source_indices": [43, 44],
              "physical_table_segment_indices": [7, 8]}]
}
```

locator v2 允许一种窄的 source-PDF 校正：若 MinerU 的一个 `text` block 在 exact bbox 内只漏数字，
admission 可读取同一不可变 PDF 的 native text。只有 PDF hash/page count、block/raw hash、bbox 与
payload identity 全部闭合，且 MinerU 文本可由 native text 仅删除完整数字核心（可连同或保留
`%/‰`）得到；被删除数字 token 位置的相邻 ASCII 横向空格/Tab 可随 token 缺失或作为 MinerU 占位保留，其余字符
（包括宽窄字符、标点、空白）和
已有数字保持原序逐字相等时，Unit payload 才使用
source PDF 文字。唯一 reader 规范化是 PDFium bounded-text 生成的、非首尾孤立 `CRLF`（ASCII
单词边界保留一个空格）；CRLF 周围水平空格仍须逐字匹配，且仅在同一观察含该软换行时移除一个、
不能是多个的矩形末尾空格。NUL、裸 `CR/LF`、空白行及其他空白均拒绝。ProviderDocument 始终保留 MinerU 原文。locator 的
`source_text_reconciliations` 只保存两侧 text hash 与 source identity，不存第二份路径或结构。
每个 Unit locator 覆盖本 Unit source blocks 及其完整 heading chain 所依赖的校正；Publish 必须从
PDF 重新生成同一结果。表格、数字替换/重排、非数字差异、无 text layer、旋转/页面形状不闭合、
高度重叠 bbox 或任何歧义均不修。native reader 固定使用 `pypdfium2==5.13.0`。
历史 `provider_unit_locator.v1/v2` 继续只读；v1 不能声明该校正，v1/v2 的 heading
默认绑定 source block 的首个 payload。只有 v3 可把标题精确绑定到非首个 payload occurrence。

MinerU merge-on 输出中的非空 content-list table owner 是唯一逻辑/检索 payload；后续空 table
stub 不另发正文，只通过 locator 连接其逐页 physical segment、crop、page/bbox 和 raw hash。
ProviderDocument 永久保留 owner/stub 与所有 page-local segment；L1 不解析 grid、不修 cell、
不按相似度跨页合表。关系证据缺失或歧义时保留 provider occurrence 并 fail closed 或标记
`needs_review`，绝不猜 owner。locator 不是 agent 的主查询键。

locator 中登记的视觉 evidence artifact 通过
`GET /v1/units/{asset_id}/evidence/{sha256}` 读取。请求只携带 unit 身份与内容 digest，
不得携带或推导文件路径；服务先确认 digest 被该 unit（含 mixed part）引用，再校验
processing run 登记的 primary artifact hash、provider manifest（新产物）或冻结 v4 manifest
（历史产物）及返回 bytes 的 size/hash/media type。分派只看 locator contract，不靠文件存在性
或 parser 名猜版本。未被 unit 引用的 digest 返回 `NOT_FOUND`；已发布 descriptor、manifest
或文件发生缺失/漂移时返回明确的 `EVIDENCE_INTEGRITY_ERROR`，不得伪装成 404。

## 7.3 第一版追溯锚

进入 L2/L3 的披露证据使用通用来源锚，不依赖 L1 业务词典。例如：

```text
source_access_id    = sa_...
document_id         = doc_...
provider_document_id= ...
raw_file_hash       = sha256:...
processing_run_id   = run_...
asset_id            = du_...
semantic_key        = source-bound 受控路由或 NULL（证据不足时不伪造）
exact payload       = {"table_body":"<table>...</table>"}
```

这已经能说明来源文件、处理版本和当时实际使用的内容。

---

# 8. 切分规则

## 8.1 通用优先级

先从 parser typed heading、编号语法、TOC 对账和全篇重复的单栏缩进/布局层级恢复保守标题树；
单次位置或双栏间距不构成层级证据。编号层级按整份文档是否存在更高的编/篇/部/章定标，
长标题只有在页首且被同族 N-1/N+1 真标题夹住、期间无同级或更高边界时，才可从 paragraph
补为候选。正式章节前的无编号标题只有同时满足非封面页、页首、居中，才可成为 front-matter
根。再以具体 heading
occurrence 划分连续 source carriers。表格、文字、图片、适用性声明和关联 caption 都是该结构下
的证据 parts，不是新的边界。没有可靠标题结构时生成 `title=null, heading_path=[]` 的 coarse
unit；登记文档标题只留 document scope，不复制进 Unit 或 A-weight body。

编号单调栈还必须抵抗 provider style 的弱层级漂移：仍在栈内的同族、同 rank 且 ordinal 递增
标题按 source order 回到共同父级；若前一编号已被弱 style root 挤出栈，只允许连续 ordinal、且
两者之间全为无编号 `provider_style/pdf_style` 标题时恢复其父链。长文中新的“(一)/(1)”序列不能
只凭页距重置；只有当前 plain-numbered parent 自身带 continuation marker、距新序列至少八页，且
区间内同一无编号 provider title 在至少两个不同页面以近同 bbox 重复，才允许弹出 stale parent。
表格和 payload kind 永远不作为缺失章节的代理边界。若一个无编号弱标题的紧邻 source block
明确从同族 ordinal one 重启，该弱标题先退出已完成的括号编号 subgroup；较远的编号、仅仅
变小的 ordinal 或中间出现表格都不得移动它。
序号重启、新强边界、单页重复、不同版位、短间隔或正文短语均不得触发重置。

不使用业务短语白名单/黑名单、监管 taxonomy、payload kind、页码、固定字符数、固定 token 数
或 overlap 作为持久化边界。编号语法只说明源文档的 outline 形态，不说明其金融主题。

## 8.2 长文本处理

如果一个逻辑小节本身很长，但没有更细的真实结构，第一版仍保存为一个 unit；其中不同
source kinds 以有序 `mixed.parts` 保留。

运行时 agent 可以按需摘取上下文，但数据库不为此生成长期 chunk。

## 8.3 表格和邻近解释

`title` 只取 PDF 标题树的叶节点。通常 `table_caption`、单位、表头、行数据和脚注分别保留在
table payload 中，不能互相冒充；唯一窄例外是 Provider 已把缺失标题并入 table block，且该 block
恰有一个非空 `table_caption`、caption 以强根编号（如“第四节”或“四、”）开头时，该 caption
以 `(source_index,payload_ordinal)` 成为 source-bound heading occurrence。它从 table payload
移出并只出现在 Unit title/search 一次，table_body 与 footnote 原样保留。普通“表4”、括号子组、
checkbox-only 或无编号 selector、正文中偶现编号均不能开节；这不排除上行已绑定的强编号
table-caption 中同时包含适用性选择。表格与同一 heading occurrence 下的相邻解释进入一个有序 `mixed`。
下一真实 heading occurrence 下的内容另建 unit；不按“是否像管理层分析”等词面语义猜边界。

## 8.4 短公告

短公告默认按显式章节切成少量 `text/table` 单元。

若全文只表达一个事项，也可以生成一个主 `text` 单元。事件字段在 L2 抽取，不在 L1 建 canonical event。

---

# 9. 保留与跳过策略

## 9.1 证据守恒

ProviderDocument 中每个非空 source carrier 默认都必须进入 unit payload、mixed part、标题投影、
明确的 continuation/evidence-only 位置或
明确可审计的结构去重记录。L1 不根据“投资价值”、监管主题、标题短语或模板词表删除内容。
目录、释义、责任声明、风险提示、签章、联系方式、标准财务报表等只要 parser 识别为非空正文，
都仍是可检索证据；是否进入某个 L2 任务由检索路由和 context packaging 决定。

## 9.2 仅结构可证明时不重复发布

允许不生成独立业务 unit 的范围仅限：

- parser 明确标注的页码，以及同一 frame role、同一规范化文字在至少两个不同页面逐字重复的
  running header/footer；仅有 provider header/footer 类型或页边位置而未跨页重复的非空文字仍是
  source content，例如唯一出现的证券代码、公告编号和末页公告语句；
- provider 明确声明为非内容且可由 source contract 验证的空载体；MinerU merge-on 的空 table
  continuation stub 仅在它与 page-local deleted segment、前序逻辑 owner 唯一绑定时属于该类，
  其逐页 crop/HTML/page/bbox 仍保留为 evidence；
- 与登记元数据逐字相等、且所有来源位置均保存在 locator/统计中的重复封面或证券元数据；
- 没有内容寻址视觉资产、没有 caption、没有可用文本的纯空视觉载体。

这些判断都必须依赖 source type、位置、重复关系、哈希或登记元数据相等性，不得依赖业务词面。
一旦所谓“噪声”含有无法由其他已发布字段逐字重放的信息，就必须保留并追查 parser/结构证据，
不能以降级、黑名单或白名单掩盖。

## 9.3 规则边界

结构去重只能由可版本化、可回放、与内容主题无关的机械证明决定。监管 taxonomy、
`semantic_key`、filing type 和 L2 查询同义词只用于路由/排序，绝不参与 heading_path、title、
unit 边界、内容归属或删除。不得让 LLM 在 L1 自由判断“这段有没有投资价值”；若将来引入
模型辅助结构判定，也必须输出可核验位置和独立证据，不能把模型猜测写成源事实。

---

# 10. 最小数据对象

## 10.1 company / security

维护公司主体与证券标识。公司和证券分开，允许一家公司对应多个证券。

主体匹配键采集义务（顶层协议 §6.5.1 主体匹配键规范在本服务的落地）：

- `company_id` 是本服务铸造的本地不透明 ID；L4 Subject Registry 上线前不承诺全局主体身份，
  届时按强键 crosswalk 回填 `subject_ref`（视图加列，兼容变更），`company_ref` 语义不变；
- 强键"有则必填"：中国注册实体的统一社会信用代码经 `company_credit_code` 采集（scheme = uscc）；
  `(exchange, security_code)` 已必填（证券级中键）；美股 / 港股主体接入时同规范采集
  sec_cik / hk_cr / lei；
- 规范化名称 + 辖区只作弱键，仅产生合并候选，不得自动合并主体。

## 10.2 source_access

记录一次远端访问或文件获取：

- provider；
- 接口 / URL；
- 查询参数；
- 访问时间；
- 结果状态；
- 返回摘要或结果哈希；
- 错误和重试信息。

它同时支持“查空”记录。

## 10.3 document

一条 `document` 对应一个具体披露文件版本。

核心内容包括：

- 公司和证券；
- provider 文档 ID；
- 公告类型和标题；
- 公告日期与报告期；
- 来源 URL；
- 原文件路径和哈希；
- 下载、解析和发布状态；
- 被更正 / 替代关系。

更正公告或不同文件哈希形成新 `document`，不覆盖旧记录。

## 10.4 processing_run

记录一次下载、解析、清洗、切分或重跑：

- processor 名称和版本；
- 输入哈希；
- 输出哈希；
- 开始和结束时间；
- 状态；
- 错误；
- 是否为当前 active run。

## 10.5 document_unit

保存当前 run 生成的 `text/table/qa/mixed` 单元。

`asset_id` 在对应 run 内不可变，但不承诺跨 parser 版本保持同一 ID。

## 10.6 source_checkpoint（可选）

用于增量同步游标和最近成功时间。只有当 CNINFO 或其他数据源需要断点续跑时才建立。

---

# 11. 存储形态

```text
filesystem
  raw_documents/          原始 PDF
  parser_artifacts/       Markdown / JSON / HTML / 图片等解析产物

postgresql
  company
  security
  source_access
  document
  processing_run
  document_unit
  optional source_checkpoint
```

> `document_unit` 存的是**切好的内容快照本身**（在 `payload` 里），不是只存地址。`artifact_locator` 只是可选的回溯指针，用于回看原文核对；查询和证据引用都依赖 `payload` 快照，不依赖它。这也是为什么内容固化后无需 parser block 镜像表 / table_cell / persisted chunk 等回指结构。

第一版不建设：

- 独立向量数据库；
- 图数据库；
- page / bbox 索引；
- parser block 镜像表；
- table_cell 表；
- persisted chunk 表；
- 全量标准数据 provider 财务仓库。

---

# 12. 查询接口

本服务应提供 agent 友好的 Filing API，而不是要求调用方直接理解底层 SQL。

概念调用形态：

```python
company("002484").filings(
    filing_type="annual_report",
    period="2025A",
).latest()
```

取得 filing 后：

```python
filing.text_units()
filing.tables()
filing.qa_items()
filing.units(semantic_key="receivable_aging")  # 仅当真实受控键存在
filing.units(heading_path="第三节/管理层讨论与分析")
```

查询入口至少支持：

- 公司 / 证券；
- 公告日期；
- 报告期；
- 公告类型；
- `payload_kind`；
- `heading_path`；
- `semantic_key`；
- `semantic_keys`（mixed Unit 的完整路由集合）；
- `section_keys`（已接受标题树中的规范化章节位置集合）；
- `applicability`（0010 起，节适用性一等筛选列）；
- `page_no`（0010 起，定位与审查的一等筛选参数）；
- `quality_status`；
- `content_hash`；
- `source_ref`；
- `producer_action_ref`；
- 标题；
- `asset_id`。

查询面说明：上述键在 `disclosure_public.*_v1` 视图上全部可作谓词（DB 直读满足全集）；
Filing API 首版只暴露其中一部分为查询参数（documents：company_ref / security_code /
filing_type / report_period / announcement_date_from,to / status；units：payload_kind /
semantic_key（兼容召回 primary 或 secondary route）/ semantic_keys_any / semantic_keys_all /
section_keys_any / section_keys_all / quality_status /
heading_prefix），其余键经 DB 视图直读或后续 API 升版满足。

全文关键词检索可以后加，但不是证据对象，也不要求向量化。

## 12.1 Public view 读契约

跨服务读侧只经 `disclosure_public.*_v1` 或等价 Filing API 暴露，不要求下游理解
`disclosure_core` / `disclosure_ops` 私有表。

当前 public view 全集（与 `contract-checklist.md` §3 一致）：

```text
documents_v1 / document_units_v1 / document_categories_v1 /
processing_runs_v1 / source_refs_v1 / change_events_v1 / tracked_companies_v1 /
unit_search_projection_v1 / unit_body_search_windows_v1 / unit_search_atoms_v1
```

`unit_search_projection_v1`（0025 迁移，06R 检索投影层）是**派生、可再生、无事件语义**的读面：
每列可由已持久化 unit 经钉死 jieba 分词确定性再生，不进 content/query_projection 哈希、重建
不发 outbox 事件，L2 直接消费加权 tsvector + pg_trgm 子串通道；证据引用始终回到 document_unit
的 asset_id。

`unit_search_atoms_v1`（0030）把 body 的每个 explicit search-target 字符串叶子保留为独立
NFKC+casefold atom，供长度 ≥3 的精确子串候选；不得连接相邻 target/mixed part。GIN `LIKE`
只是候选，L2 必须转义 pattern 并在同 atom 以 `strpos` 复核；1–2 字仍只走完整 word channel。

`tracked_companies_v1`（0019+0020 迁移，contract_version `tracked_company.v1`，round22）
暴露股票池配置与生命周期：真源是 `tracked_company` 表（watchlist.csv 降级为导入/快照格式，
改判记录见 `docs/implementation/design/watchlist-operations.md` §7）。视图暴露 raw 覆盖列
（NULL=继承全局默认）+ 生命周期事实（legal_name_status、last_synced_at、synced_through）；
级联生效值（effective_*）与采集状态 sync_state（never_synced/due/fresh）由
`GET /v1/tracked-companies` 在 API 层派生——全局处理策略在
`config/processing_policy.json`、间隔默认在 env，SQL 视图看不见。写侧走
`PUT /v1/admin/tracked-companies`（admin gate 内，复用 TrackCompanies 用例；整行 upsert，
空可选字段=清除覆盖）；入池即解名：track 提交后有凭据时当场拉档案升级占位公司名
（best-effort fail-open，首次同步兜底自愈）。

`document_categories_v1`（0012 迁移，contract_version `document_category.v1`）暴露 provider
原生分类维表：CNINFO F006V 段 × `provider_category` 字典（p_info3005 快照 seed）；facet
语义只给 ordinal、不造 is_primary；`filing_type` 仍是 §5.1 的 9 值粗桶契约词表，原始分类
语义经该视图完整可查。

`document_units_v1` 必须保留足够的 unit 级 scope keys：

```text
company_ref
security_ref
filing_type
report_period
announcement_date
payload_kind
heading_path
semantic_key
quality_status
content_hash
contract_version
producer_action_ref
source_ref
parent_ref
order_index
```

映射关系：

- `company_ref` = `document.company_id`（本地不透明 ID，见 §10.1 主体匹配键采集义务）；
- `security_ref` = `document.security_id`；
- `payload_kind` = `document_unit.payload_kind`（0006 起列名已收敛，不再映射）；
- `producer_action_ref` = `processing_run_id`，即顶层 `action_log` 的 L1 特化；
- `parent_ref` = 所属 `document_id`；
- `source_ref` = `source_access_id`，完整 source reference 可由 `source_refs_v1` 或
  `GET /v1/units/{id}/source-ref` 派生。

0007 迁移起，`document_units_v1` 以派生投影补齐协议 §3.2 信封最小核（04R-D1，不加存储列）：
`asset_kind`（常量 document_unit）、`observed_at`（= created_at 别名）、`source_tier`
（按 §5.1 映射 CASE 派生）、`trace_level`（常量 G0）、`raw_file_hash`（join document）；
另投影 `query_projection_hash`（document_unit 存储列，05-U2 查询投影哈希）——0007 新增共
6 列，至 04R-R7 为 32 列（仅历史基线）。

0008 迁移起，`processing_runs_v1` 投影 `builder_rules_version`，用于确定性 Unit builder 归因；
历史 run 可为 NULL 或旧版本，新 Provider writer 成功落库的 run 当前必须等于 `provider_unit.v8`。

0031 迁移起，`processing_runs_v1` 只额外暴露不透明的
`artifact_owner_processing_run_id`：parse run 指向自身，`rebuild_units` 指向实际拥有
parser artifact 与 primary parse artifact 字节的根 parse run。0032 为 core 增加私有
`provider_document_relpath`，parse/rebuild 必须在历史 `normalized_ir_relpath` 与新路径之间精确
选择一个；public view 仍不暴露任何相对路径。evidence resolver 经 owner run 的 hash、document、
run_kind 与 locator contract 校验后使用统一 PathBuilder 定位，禁止把 unit producer run 当成
artifact owner，也禁止从文件存在性或路径词面猜版本。

0038 后 `document_units_v2` 为当前 **40 列** Unit 读面：保留 Unit 自有 scope/provenance、
`semantic_key`/`semantic_keys` 检索路由，以及 `filing_type`/`disclosure_topics` 路由字段；
`section_keys` 单独提供确定性的规范化章节召回；
`content_categories`、`publisher_categories`、`market` 的当前事实面均只在 `documents_v1` 与
`document_categories_v1`。L2 如需 provider 粗分类，先筛 Document 再按 document_id 取 v2 Units，
不能把 Document facet 冒充 Unit 内容标签。`document_units_v1` 为兼容既有消费者恢复弃用的
末列 `content_categories`（40 列，仅 join、不存 Unit）；v2 用 Unit 自有
`body_status=content|heading_only|empty` 取代该 Document facet，新消费者不得依赖 v1 弃用列。完整列集以
contract-checklist §2 为准：

- 0010：`applicability`（'applicable'|'not_applicable'|NULL，节适用性一等筛选列，部分索引）
  与 `page_no`（artifact_locator 首页码提升列）；
- 0011：`is_active_run` 成为 `document_units_v1` / `source_refs_v1` 的真实视图列
  （DB 直读方可直接过滤 active run）；同迁移将 `payload_kind` CHECK 扩为含 `mixed`（§6.5）；
- 0033：开发期误把当时 duplicate-only 的观测推广为 schema 结论，删除了 plural route；
- 0034：恢复 `semantic_keys` 存储/GIN/API any/all recall，并恢复 Unit 视图继承的
  `content_categories`。当前 Provider writer 使用版本化受控词表、filing/topic scope、Unit-local
  精确标题与 recall-only 包含/字符相似候选，再由闭集 Luna 裁决真实 route；模型只能选择候选或弃权。
  一跳 overview 标记只负责在具体子标题 Unit 中去掉上位容器，真实概要/汇总 Unit 仍可保留
  container 与独立字段 routes，且绝不自动补父键。Build 冻结
  receipt、Publish 只重放。证据不足时 scalar/array 均为 NULL，不授权恢复旧的自由词面规则堆。
- 0036：新增 `section_keys` 存储/GIN/API any/all recall。当前 writer 只从已接受 heading_path 的
  显式结构容器精确匹配生成：定期报告使用 context-container，事件公告仅使用命中 filing_type/
  authoritative disclosure_topics scope 的窄 section-container。它不调用模型、不与直接主题竞争，
  也不复制 Document facet。
- 0037：从 `document_units_v1` / `DocumentUnitV1` 删除继承的 `content_categories`；CNInfo
  原始分类、Document materialized facet、documents API/filter 与 semantic router 的 Document
  context 均保留。
- 0038：不改写 0037，恢复 40 列 `document_units_v1` 兼容面，并新增无该字段、带
  `body_status` 的 40 列 `document_units_v2`；两者分别投影 `document_unit.v1` / `document_unit.v2`。当前 Filing API
  仍仅实现 v1，完整 HTTP v2 未落地前拒绝 `X-Contract-Version:v2`。该迁移 downgrade 只移除 v2，
  继续保留已恢复的 40 列 v1，避免以回滚名义重新暴露 0037 的已知破坏形状；更深历史回退仍由各自
  migration 处理。

`asset://` URI（顶层协议 §2.3）只在序列化边界派生，不落存储：

- 形式：`asset://disclosure_anchor/v1/document_unit/{asset_id}`；
- Filing API 响应以派生字段 `asset_uri` 返回（Phase006 落地）；MCP 包装时以该 URI 作为
  resource key（`resources/read` 按 URI 取回，协议 §3.11）；
- 数据库表与 `*_v1` 视图不存储 URI：它是 (service, kind, asset_id) 的纯投影，存储即冗余，且会随
  scheme / 契约版本演进漂移（业界先例：Kubernetes 因同样原因弃用并移除了 `metadata.selfLink`）。

## 12.2 Change feed 读契约

`disclosure_ops.outbox_event` 是本服务的写侧 outbox，字段已收敛为 `event_kind`（曾用名 `event_type`）。

对外 `change_events_v1` / `GET /v1/changes?after_seq=...` 使用顶层协议口径：

- `event_kind` 是对外事件名，与 outbox 列同名，无需映射；
- `change_kind` 只有 `observed` / `materialized` 两类；
- 未显式声明 `change_kind` 的历史事件默认是 `materialized`；
- `observed` 表示巡检或来源观察到了对象但没有产生可消费内容变化；
- `materialized` 表示 public read model 可见内容发生变化，才允许触发下游失效 / 重算；
- `GET /v1/changes` 是 Phase006 Filing API 契约，按 `seq > after_seq` 或 cursor 增量读取。

## 12.3 对外错误模型

HTTP / MCP 读侧错误码枚举至少包含（顶层协议 §3.11）：

```text
L1_PROCESSING_REQUIRED      请求对象仅有 raw 登记、尚未完成载体规范化（L1 读侧语义）
NOT_FOUND                   对象不存在
CONTRACT_VERSION_MISMATCH   请求契约版本与服务暴露版本不一致
GONE_SUPERSEDED             对象已被新版本取代，响应携带 superseded_by 指引
VALIDATION_ERROR            参数、过滤值或游标校验失败
EVIDENCE_INTEGRITY_ERROR    已发布 evidence 的 IR/manifest/bytes 完整性校验失败
```

错误响应不含内部堆栈与绝对路径。Phase006 Filing API 已落地该 envelope。

---

# 13. 版本与变更传播

## 13.1 不触发 L3 的变化

以下变化本身不触发 L3：

- parser 版本变化；
- Markdown 格式变化；
- 单元顺序变化；
- 单元边界变化；
- page 或 bbox 变化；
- 临时 context packaging 变化。

前提是被 L2/L3 实际使用的内容快照没有变化。

## 13.2 应触发重新处理的变化

- 新公告或更正公告；
- 原始文件哈希变化；
- `document_unit` 的实际文本变化；
- 表头、行数据、单位或数字变化；
- 新规则识别出此前未进入 L2 的有效单元；
- 解析质量从失败变为可用；
- provider 标准值发生修订。

## 13.3 不做跨 parser 锚迁移

第一版不建设旧 unit 到新 unit 的几何对齐系统。

旧证据记录继续引用当时的 `processing_run + document_unit + exact snapshot`。新 run 产生新单元，
必要时由 L2 重新评估。

---

# 14. 质量控制

本服务只做轻量、明确的解析质量控制：

- 文件可打开；
- 文本不是空白；
- 标题树基本成立；
- 表格有标题、表头和数据行；
- 数字解析率在合理范围；
- 单位能识别或标记缺失；
- 显式合计存在时可做简单加总检查；
- 失败对象标记 `needs_review` 或 `unusable`。

第一版不要求：

- 每份文件双 parser；
- 每张表 cell 级对账；
- 所有 PDF 表与标准数据 provider 双源核验；
- 所有异常自动裁决。

重要表格在 L2 真正用于证据入账时，可以触发更严格复核。

---

# 15. 与 L2 的交接契约

本服务交给 L2 的对象应满足五个条件：

1. **好找**：可按公司、期间、公告类型和语义查询；
2. **好读**：正文、表格和问答保持完整业务边界；
3. **够轻**：通过检索排序、section pack 和按需上下文窗口控制 L2 输入量，不通过隐藏
   canonical evidence 控制体量；
4. **可追**：能回到原文件、处理运行和 exact snapshot；
5. **不越界**：不提前形成事实、采信和预测判断。

L2 收到一个 unit 后负责：

```text
主体 / 时间 / 指标 / 事件识别
→ 高价值 evidence_record 抽取 / 登记
→ 口径和单位处理
→ 去重、对账、冲突检测
→ 置信度与重要性判断
→ 进入 L3 / 冷存 / 待办 / 丢弃
```

---

# 16. 验收标准

服务完成第一版后，应稳定回答：

- 某公司有哪些公告和财报已发现、已下载、已解析？
- 某份文档的原始 PDF、文件哈希和处理状态是什么？
- 某年报的管理层讨论、风险、未来展望能否按标题直接取得？
- 某投关记录能否按完整 Q&A 取得？
- 某年报中的产品收入、产销量、应收账款账龄等完整表格能否直接取得？
- 标准数据 provider 已覆盖的三大表是否没有被重复建设为第二套本地标准表？
- L2 是否可以在不重新打开 PDF、不依赖 page、不理解 parser 内部结构的情况下工作？
- parser 升级但内容不变时，是否不会误触发 L3？
- 任一进入预测的披露证据是否能引用原文件哈希、unit 和 exact snapshot？
- 失败的下载和解析是否可定位、可重试？

---

# 17. 明确废弃的旧设计

以下设计不再作为第一版要求：

| 旧设计 | 新决策 |
| --- | --- |
| `filing_text_block` | 改为业务结构级 `document_unit(kind=text)` |
| `filing_text_chunk` | 删除；只做运行时 context packaging |
| `filing_table` + `filing_table_cell` | 合并为完整 `document_unit(kind=table)`，不拆 cell |
| `page_idx + bbox` | 不进入核心契约；parser artifact 可自然保留 |
| `event_unit` | 移到 L2，由 `text/table` 抽取事件 |
| `content_item` 统一 provider 和 PDF | 删除；标准数据 provider 走 Dataset API，PDF 走 Filing API |
| `section_path` | 新文档统一使用 `heading_path` |
| 全量三大表 PDF 重建 | 删除；标准数据 provider 为默认来源（首版 Wind） |
| 多 parser 常态交叉验证 | 删除；只在明确失败或高价值复核时启用 |
| 独立 topic/tag 关系表 | 第一版只保留可选 `semantic_key` |

---

# 18. 对上位协议的兼容说明

本文件已对齐 `投研预测引擎顶层框架协议_v0.8.md`，尤其是 §3.10
对 `disclosure_anchor` 的三点补强：

1. `document_units_v1` 保留 unit 级 scope keys，方便 L2 / MCP / API 检索；
2. 术语已收敛：`document_unit_id → asset_id`、`unit_kind → payload_kind`、outbox
   `event_type → event_kind`；`processing_run` 保留为 `action_log` 的 L1 特化；
3. change feed 以 `change_events_v1` / `GET /v1/changes` 暴露，区分 observed / materialized。

披露侧 `G0` 采用：

```text
自维护原文件
+ 文件哈希
+ 可精确引用的 document_unit / exact snapshot
```

page 或视觉区域可以作为附加复核信息，但不是 G0 的必要条件。

---

# 19. 最终判断

`disclosure_anchor` 第一版应该是一套小而稳定的披露文件服务：

```text
原始文件可靠保存
+ 文档结构切得清楚
+ 表格整体可读
+ 问答完整
+ 数据库对象少
+ L2 能直接查询
```

它的价值不在于重建 PDF 的每一页、每一个 block 和每一个 cell，而在于让 L2 不再处理文件格式，把工程资源留给证据、冲突、口径和预测。
