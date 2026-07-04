---
id: disclosure_anchor
title: disclosure_anchor 服务目的
contract_version: v1.2
status: canonical
layer: L1
layer_name: 披露文件接入与结构化准备层
reference: docs/reference/投研预测引擎顶层框架协议_v0.7修订最终版本.md
delivers_to: L2
scope: self_maintained_exchange_disclosures
output_kind: l2_ready_document_units
output_form: queryable_database_plus_filing_api
payload_kinds: [text, table, qa]
query_keys: [company_ref, security_ref, report_period, announcement_date, filing_type, document_id, asset_id, payload_kind, heading_path, semantic_key, quality_status, content_hash, source_ref, producer_action_ref]
core_objects: [company, security, source_access, document, processing_run, document_unit]
optional_objects: [source_checkpoint]
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
- 标准数据 provider 已稳定覆盖的标准财务数据，通过 `Dataset API` 使用，不复制成 `document_unit`。
- PDF 中 provider 未覆盖、但对预测有价值的表格和文本，由本服务处理成 `document_unit`。
- 非标准 API、MCP、Web 查询、搜索、新闻等一次性来源，由对应 L1 adapter 登记为 `data_asset` 或
  `source_access` 后进入 `L2`；第一版不要求把它们转成 `document_unit`，也不由本服务承担（详见
  《财报与披露数据接入及切分方案》§1.2）。
- 上述路径都在 `L2` 汇合，不在底层强行统一原始形态。

## 1.2 与 L2 的分界

本服务做的是**文档结构切分与载体规范化**；`L2` 做的是**高价值证据化与语义归一**。

```text
L1 disclosure_anchor
PDF → 章节 / 子标题 / 完整表格 / 完整问答

L2
一个 document_unit → 0 到多条 evidence_record（observed_claim / relation_claim / numeric_observation 等）
```

因此：

> `document_unit` 是最小的 L1 可寻址文档单元，不是最小事实，也不是 claim。

一个完整问答、一个经营分析小节或一张应收账款账龄表，都可能在 `L2` 中产生多条
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

正文按标题层级、显式编号、完整问答和完整事项切分。

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

## 2.4 表格先保留完整结构，不急于全市场标准化

PDF 表格默认保存为完整 `table` 单元：

- 表名；
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

三大报表、标准财务指标、业绩预告、业绩快报、审计意见等，若标准数据 provider 已稳定覆盖，本服务不从 PDF 重建第二套标准表。

PDF 原文件和 parser artifact 仍可保留，但不为这些内容默认生成可查询的标准 `table` 单元。

## 2.6 L1 不判断真伪和重要性

本服务可以做：

- 确定性去噪；
- 标题树识别；
- 表格抽取；
- Q&A 识别；
- 固定规则的保留 / 跳过；
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

## 2.7 删除派生单元，不删除原文件

“去掉废话”在本服务中的含义是：

> 不为该内容生成 `document_unit`，但原始 PDF 和完整 parser artifact 仍然保留。

这样可以显著减轻 L2 负担，同时允许未来规则变化后重新处理。

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

`document.status` 生命周期枚举（04R-D4，随 0007 加 CHECK）：

```text
registered → parsed | parse_failed →（发布后）published
```

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

第一版只有三种：

```text
text
表述性正文、章节、子标题或完整事项

table
保留结构的完整表格

qa
一个完整 Question + Answer
```

没有 `event_unit`。短公告中的事件字段由 `L2` 从 `text/table` 单元中抽取，形成事件类
`evidence_record` 或后续派生对象。

术语对齐顶层 v0.7（已完成收敛，迁移 `0006_v07_terminology_convergence`）：

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
semantic_key（可选）
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
  "semantic_key": "business_overview",
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
  "title": "应收账款按账龄披露",
  "semantic_key": "receivable_aging",
  "payload": {
    "unit": "元",
    "headers": ["账龄", "期末账面余额", "期初账面余额"],
    "rows": [
      ["1 年以内（含1 年）", "1,765,831,017.43", "1,653,778,854.38"],
      ["1 至2 年", "23,872,757.96", "35,360,192.57"],
      ["2 至3 年", "14,382,374.82", "7,076,511.89"],
      ["3 年以上", "73,573,362.32", "78,454,116.92"],
      ["3 至4 年", "1,443,796.67", "11,272,000.94"],
      ["4 至5 年", "5,272,677.17", "450,708.56"],
      ["5 年以上", "66,856,888.48", "66,731,407.42"],
      ["合计", "1,877,659,512.53", "1,774,669,675.76"]
    ],
    "notes": []
  }
}
```

> 示例取自江海股份（002484）2025 年年度报告"第八节 财务报告 / 应收账款 / 按账龄披露"。注意原表中"3 年以上"为小计行，其下再拆"3 至4 年 / 4 至5 年 / 5 年以上"三个子区间，下游做"合计=各账龄之和"校验时应避免重复计数。

表格跨页时仍然只生成一个逻辑 `table` 单元。是否跨页不是调用方需要关心的业务属性。

## 6.4 qa 单元

适用于投关记录、业绩说明会和公开交流中的完整问答。

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

一个回答即使很长，也不按 token 拆碎。L2 可以从中抽取多条 `evidence_record`。

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

财务附注里的表格则是更深的层级（对应 6.3 的 receivable_aging 单元）：

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

`artifact_locator` 是可选的**技术位置**，用于回到 Markdown、JSON、HTML 或 parser artifact。

它可以包含：

```text
artifact_path
artifact_unit_ref
order_index
```

以江海股份 receivable_aging 单元为例（document 来自 CNINFO textid `1225087169`）：

```json
{
  "artifact_path": "artifacts/002484/1225087169/parsed.md",
  "artifact_unit_ref": "table-receivable_aging",
  "order_index": 312
}
```

它指回 parser 产物中该表所在位置，便于人工复核或重解析；但它不是 agent 的主查询键，也不要求使用 page 或 bbox。

## 7.3 第一版追溯锚

进入 L2/L3 的披露证据可以使用（以江海股份 receivable_aging 单元为例）：

```text
source_access_id    = cninfo:p_info3015
document_id         = 1225087169        # CNINFO textid
raw_file_hash       = sha256:7c73103aa3c93778d2d1d18bcf55a2f76413887a8aa3f6b50f0749038edc19b3
processing_run_id   = run_20260618_v3
asset_id            = du_receivable_aging_002484_2025
exact table snapshot= {"账龄":"1 年以内（含1 年）","期末账面余额":"1,765,831,017.43", ...}
```

这已经能说明来源文件、处理版本和当时实际使用的内容。

---

# 8. 切分规则

## 8.1 通用优先级

按以下顺序识别逻辑边界：

1. 文档级标题；
2. 章节标题；
3. 子标题；
4. 显式编号条目；
5. 完整 Q&A；
6. 完整表格；
7. 短公告中的完整事项说明。

不使用固定字符数、固定 token 数和 overlap 作为持久化边界。

## 8.2 长文本处理

如果一个逻辑小节本身很长，但没有更细的真实结构，第一版仍保存为一个 `text` 单元。

运行时 agent 可以按需摘取上下文，但数据库不为此生成长期 chunk。

## 8.3 表格和邻近解释

一张表的标题、单位、表头、行数据、脚注和紧邻解释应尽量放在同一 `table` 单元中。

与表格无关的后续管理层分析应另建 `text` 单元。

## 8.4 短公告

短公告默认按显式章节切成少量 `text/table` 单元。

若全文只表达一个事项，也可以生成一个主 `text` 单元。事件字段在 L2 抽取，不在 L1 建 canonical event。

---

# 9. 保留与跳过策略

## 9.1 默认保留

优先生成单元的内容包括：

**业务与经营**

- 主营业务、业务结构和经营模式；
- 行业格局、竞争地位和市场环境分析；
- 产品、客户、供应商、区域和渠道变化；
- 价格、销量、订单、产能、产销量和库存；
- 核心竞争力、品牌、专利和特许经营权。

**财务表现与财务附注**

- 收入、成本、毛利率、费用、利润和现金流的变化及原因；
- 分行业、分产品、分地区的收入和毛利率拆分；
- 关键财务附注表（应收账款账龄及坏账、存货分类及跌价、商誉及减值、固定资产及在建工程、长期股权投资、债务及融资结构、收入确认、税项、关联交易等）；
- 分部报告、合并范围变化和重要会计政策、会计估计变更；
- 重要资本开支、并购重组、资产剥离和资产减值。

**治理、资本与股东**

- 公司治理结构、董监高履职和薪酬；
- 股本结构、股份变动、股东和实际控制人情况；
- 募集资金使用、利润分配和权益分派方案；
- 股权激励、员工持股和回购方案；
- 债券、可转债及其他融资工具的相关情况。

**重大事项与公告**

- 重大合同、对外担保、对外投资和委托理财；
- 关联交易、同业竞争和承诺事项及履行情况；
- 诉讼仲裁、行政处罚和其他或有事项；
- 业绩预告、业绩快报和重大事项进展；
- 监管问询函、关注函的单个问题及回复。

**风险、展望与交流**

- 风险因素和应对措施；
- 未来发展战略、经营计划和未来展望；
- 投资者关系活动、业绩说明会和公开交流中的完整问答；
- 环境、社会和可持续发展（ESG）中的实质披露；

等。

## 9.2 默认不生成单元

以下内容仍保留在原始 PDF 和 parser artifact，但默认不生成 `document_unit`：

- 封面、扉页、目录、页眉、页脚和页码；
- 释义和固定责任声明（董监高保证真实准确完整等套话）；「重要提示」板块**不得按标签整段跳过**（协议 §3.5 安全红线：常含退市风险、业绩大幅变动等实质内容，按 9.1 保留），仅其中纯模板句可跳过；
- 签章、签字页、盖章、联系方式和备查文件目录；
- 空表，以及只有表头、单位而无实质数据的表；
- 只有“适用 / 不适用”“是 / 否”勾选而无实质内容的模板项；
- 重复性极高的法定格式文字和标准化风险提示套话；
- 财务报表附注中照抄会计准则的套话段落，包括重要会计政策的一般性表述、各会计科目的确认计量原则、金融工具 / 收入 / 租赁 / 合并报表编制等的标准定义，以及"遵循企业会计准则的声明""重要性判断标准"等模板（注意：会计政策 / 会计估计的**实际变更及其影响**仍按 9.1 保留）；
- 独立董事、监事会、保荐机构、会计师等出具的标准格式意见中无个性化结论的模板段落；
- 重复出现的免责声明、版权声明和前瞻性陈述提示；
- 纯排版元素（分隔线、装饰图、二维码、占位空白）；
- 标准数据 provider 已覆盖、且本服务不需要重建的标准财务报表；
- 当前预测阶段明确不使用的形式化披露；

等。

> 9.1 和 9.2 都是**示意性清单，不是穷尽的硬编码规则**，目的是说明"按实质内容判断"的取舍倾向，而非要求实现逐条照搬。两个原则优先于具体条目：一是**有实质信息就保留、纯格式和重复套话才跳过**；二是**拿不准时倾向保留**（漏掉实质披露的代价远高于多生成一个单元）。同一类内容在不同文档里可能落在不同侧——例如"风险提示"，个性化、可量化的风险应保留，纯模板套话才跳过——应结合 9.3 的规则边界按文档类型和语义判断，不要机械匹配字面标题。

## 9.3 规则边界

保留 / 跳过应由：

- 文档类型规则；
- 标题规则；
- 表格语义规则；
- 明确的 allowlist / denylist；
- 可版本化的机械分类器；

共同决定。

不得让 LLM 在 L1 自由判断“这段有没有投资价值”。

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

保存当前 run 生成的 `text/table/qa` 单元。

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
filing.units(semantic_key="receivable_aging")
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
- `quality_status`；
- `content_hash`；
- `source_ref`；
- `producer_action_ref`；
- 标题；
- `asset_id`。

全文关键词检索可以后加，但不是证据对象，也不要求向量化。

## 12.1 Public view 读契约

跨服务读侧只经 `disclosure_public.*_v1` 或等价 Filing API 暴露，不要求下游理解
`disclosure_core` / `disclosure_ops` 私有表。

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
（按 §5.1 映射 CASE 派生）、`trace_level`（常量 G0）、`raw_file_hash`（join document）。

`asset://` URI（顶层协议 §2.3）只在序列化边界派生，不落存储：

- 形式：`asset://disclosure_anchor/v1/document_unit/{asset_id}`；
- Filing API 响应以派生字段 `asset_uri` 返回（随 Phase006 落地）；MCP 包装时以该 URI 作为
  resource key（`resources/read` 按 URI 取回，协议 §3.11）；
- 数据库表与 `*_v1` 视图不存储 URI：它是 (service, kind, asset_id) 的纯投影，存储即冗余，且会随
  scheme / 契约版本演进漂移（业界先例：Kubernetes 因同样原因弃用并移除了 `metadata.selfLink`）。

## 12.2 Change feed 读契约

`disclosure_ops.outbox_event` 是本服务的写侧 outbox，字段已收敛为 `event_kind`（曾用名 `event_type`）。

对外 `change_events_v1` / 未来 `GET /v1/changes?after_seq=...` 使用顶层协议口径：

- `event_kind` 是对外事件名，与 outbox 列同名，无需映射；
- `change_kind` 只有 `observed` / `materialized` 两类；
- 未显式声明 `change_kind` 的历史事件默认是 `materialized`；
- `observed` 表示巡检或来源观察到了对象但没有产生可消费内容变化；
- `materialized` 表示 public read model 可见内容发生变化，才允许触发下游失效 / 重算；
- `GET /v1/changes` 是 Phase005+ 的 Filing API 目标契约；当前阶段先保证 outbox 与 public view 语义。

## 12.3 对外错误模型

HTTP / MCP 读侧错误码枚举至少包含（顶层协议 §3.11）：

```text
L1_PROCESSING_REQUIRED      请求对象仅有 raw 登记、尚未完成载体规范化（L1 读侧语义）
NOT_FOUND                   对象不存在
CONTRACT_VERSION_MISMATCH   请求契约版本与服务暴露版本不一致
GONE_SUPERSEDED             对象已被新版本取代，响应携带 superseded_by 指引
```

错误响应不含内部堆栈与绝对路径。随 Phase006 Filing API 落地。

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
3. **够轻**：不把模板废话和重复标准表全部推给 L2；
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

本文件已对齐 `投研预测引擎顶层框架协议_v0.7修订最终版本.md`，尤其是 §3.10
对 `disclosure_anchor` 的三点补强：

1. `document_units_v1` 保留 unit 级 scope keys，方便 L2 / MCP / API 检索；
2. 术语已收敛：`document_unit_id → asset_id`、`unit_kind → payload_kind`、outbox
   `event_type → event_kind`；`processing_run` 保留为 `action_log` 的 L1 特化；
3. change feed 以 `change_events_v1` / 未来 `GET /v1/changes` 暴露，区分 observed / materialized。

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
