---
id: disclosure_anchor_implementation_plan
title: disclosure_anchor 实施方案（AI Agent 编码指导）
version: v1.1
status: implementation_plan
date: 2026-07-04
canonical_contract: docs/architecture/service-purpose.md
audience: ai_coding_agent
service: disclosure_anchor
layer: L1
requirements:
  - docs/architecture/service-purpose.md
  - docs/architecture/财报与披露数据接入及切分方案.md
  - ../../docs/reference/投研预测引擎顶层框架协议_v0.8.md
references:
  - docs/architecture/database-selection.md
  - docs/architecture/pdf-parsing-investigation.md
  - docs/architecture/open-source-references.md
primary_store: postgresql
raw_store: filesystem
parser_artifact_store: filesystem
public_output: document_and_document_unit
payload_kinds: [text, table, qa, mixed]
implementation_style: modular_monolith_with_ports_and_adapters
---

# disclosure_anchor 实施方案（AI Agent 编码指导）

> 本文件把现有需求文档转成可直接指导 AI coding agent 生成代码的实施设计。
>
> 它定义代码边界、模块职责、数据对象、解析适配层、运行管道、发布语义、API 和实施顺序；不定义详细测试方案。测试策略、测试样本矩阵和验收用例将在独立文档中编写。
>
> 漂移说明（2026-07-15）：payload kinds 自迁移 0011 起为 text/table/qa/mixed；ub-2026.07-26
> 起新 builder 产物的 semantic_key/semantic_keys 均非空，无更窄概念时为
> `document_content`；本文其余早期表述与 `service-purpose.md` 冲突处，以后者（canonical）为准。

---

# 0. 文档使用规则

## 0.1 文档优先级

编码时按以下优先级理解现有文档：

1. `service-purpose.md`：决定本服务做什么、不做什么，是业务边界的 canonical contract；
2. `财报与披露数据接入及切分方案.md`：决定标准数据 provider 与 PDF 的分工、保留范围和切分方向；
3. 本实施方案：在前两份需求契约之下，决定这些需求如何落成代码；
4. `投研预测引擎顶层框架协议_v0.8.md`：决定上位系统目标和 L1/L2/L3 关系；
5. `database-selection.md`、`pdf-parsing-investigation.md`、`open-source-references.md`：作为论证与参考，不直接约束代码细节。

出现冲突时：

- 服务范围和 L1/L2 边界以 `service-purpose.md` 为准；
- 数据接入、保留和切分边界以《财报与披露数据接入及切分方案》为准；
- 本文件只能细化实现，不能反向修改前两份 canonical 需求；
- 不应为了迎合旧调研文档而恢复 page、bbox、持久化 chunk、table_cell 或 event_unit；
- 术语、信封字段、change feed、追溯等级与主体匹配键等跨层规范以《投研预测引擎顶层框架协议 v0.8》§2 / §2.8 / §2.9 / §6.5.1 为准（协议 §2 是字段与命名的唯一权威）；本文与之冲突时，修订本文。

## 0.2 AI coding agent 的工作方式

AI coding agent 在每次实现前必须：

1. 先读取本文件和 `service-purpose.md`；
2. 检查仓库现有代码、依赖和迁移，不假定仓库为空；
3. 以纵向可运行闭环提交代码，不一次性生成大量未接通的抽象类；
4. 所有 parser 字段必须来自适配器，不得在领域层直接依赖 MinerU、Docling 的原始 JSON；
5. 所有数据库变更必须同时更新 ORM、迁移、repository、API schema 和文档；
6. 不得新增核心业务对象，除非现有六个对象无法表达真实需求；
7. 不得以“以后可能做 RAG”为理由预先增加 chunk、embedding 或向量数据库；
8. 不得把 claim、事件事实、指标规范化、冲突裁决写进本服务。

## 0.3 锁定项与保留弹性

本方案锁定：

- 服务边界与 L1/L2 分工；
- 核心领域对象；
- raw、run、published unit 的不可变性；
- parser 隔离边界；
- active run 的发布语义；
- Filing API 的领域语义；
- 幂等、重试和失败可恢复性。

本方案不锁死：

- MinerU 或其他 parser 的原生字段名；
- 某类 PDF 的最佳 backend；
- 标题、表格和 Q&A 规则的具体覆盖率；
- 质量阈值；
- provider 的临时协议细节；
- 尚未经过真实样本验证的边缘结构。

这些可变部分必须通过 adapter、registry、profile 和版本化规则表达，不得写死进领域模型。

---

# 1. 独立审查结论

## 1.1 总体判断

现有设计没有需要推翻的方向性错误。核心路线成立：

```text
标准数据 provider
→ Dataset API
→ L2

交易所披露文件
→ disclosure_anchor
→ document + document_unit
→ Filing API
→ L2
```

以下判断应继续保持：

- PostgreSQL 是主库，原始文件和 parser artifact 放文件系统；
- `document_unit` 是文档结构单元，不是 claim；
- unit 第一版只有 `text / table / qa`（0011 迁移起增加 `mixed` 业务语义块，以 service-purpose §6.5 为准）；
- 不按 page、固定 token 或 overlap 形成持久化单元；
- provider 已覆盖的标准财务数据不从 PDF 重建第二套标准仓库；
- L1 做确定性的结构化准备，L2 做信息抽取、事件识别和判断；
- parser 升级可以重跑，旧运行和旧引用不能被覆盖。

需要修正的主要不是总方向，而是若干会在编码阶段造成错误的数据语义和实现假设。

## 1.2 逐文件审查结果

### `service-purpose.md`

方向正确，可以继续作为 canonical contract，但编码前必须修正或在实现中消除以下歧义：

1. front matter 的 `unit_id` 应统一为 `asset_id`（已随迁移 0006 / service-purpose v1.2 落地）；
2. CNINFO `textid` 不能直接作为内部 `document_id`；
3. `cninfo:p_info3015` 是接口/操作标识，不是一条具体访问记录的 `source_access_id`；
4. `heading_path` 是查询和导航字段，不是跨 parser 稳定 ID，也不是唯一键；
5. `processing_run` 不应把“下载”和“解析、结构化、发布”混成一个不可区分的过程；
6. “跨页表只生成一个表”是目标，不是 parser 必然能够做到的保证；失败时必须允许保留多个候选并标记质量问题；
7. 当前 `headers + rows` 表格示例只适合简单表，不能约束全部财报表格；多级表头、合并单元格和小计层级需要更宽松的 payload。

### `财报与披露数据接入及切分方案.md`

标准数据与 PDF 分工合理，但有三点需要实现层澄清：

1. `official_filing` 不能在没有专用抽取器时被伪装成一个标准 Dataset provider；
2. provider 已覆盖的标准表可以默认不发布为 `document_unit`，但 parser artifact 和候选结果必须允许按需重新物化，才能真正承担 authoritative fallback；
3. “只重跑受影响部分”第一版不应承诺 PDF 内部局部解析。可接受的实现是：整份文档重新解析，在发布阶段按内容与结构指纹做增量 diff，区分来源内容变化、结构变化、路由变化和 policy 可见性变化。

### `database-selection.md`

PostgreSQL + filesystem 的结论正确。需要加强：

1. 一个同步访问可以发现多份文档，一份文档也会被多次访问发现，因此 `document → source_access` 不是简单单值关系；
2. 必须通过数据库约束保证一个 `(document, profile)` 同时最多只有一个 active run；
3. 发布后的 unit 内容不可原地修改；人工纠正或规则变化应生成新 run；
4. DB 与文件系统之间需要 artifact manifest、内容哈希和原子写入规则，否则备份和故障恢复无法校验一致性；
5. `document_unit.payload` 应通过 repository/API 隔离，避免将来 payload 外置时破坏调用方。

### `pdf-parsing-investigation.md`

“默认 MinerU、Docling/Camelot 按需”的方向可以保留，但不能把当前 parser 输出写死：

1. MinerU pipeline 与 VLM 的结构化输出并不完全兼容；
2. `content_list_v2` 仍是可变格式；
3. parser 版本、后端和输出字段会继续变化；
4. 架构文档不应写“最新版本 = 某个固定版本”，部署配置应锁定经过验证的版本；
5. Markdown 不会天然损坏数字字符串，真正容易丢失的是多级表头、rowspan/colspan、脚注和行列语义；
6. 解析器宣称支持跨页表，不等于所有 A 股财报都能正确合并。

因此必须增加**parser-neutral normalized IR**，业务代码只能读取 IR，不能直接读取 MinerU JSON。

### `open-source-references.md`

参考方向正确，但应补入 `Unstructured` 的“partition elements 与 chunking 分离”思想，并把 `DoclingDocument` 视为 normalized IR 的重要参考。OpenBB、EdgarTools、secfsdstools、dlt 和 CocoIndex 仍是本项目最值得借鉴的五类架构模式。

### `投研预测引擎顶层框架协议_v0.8.md`

旧版协议（v0.5 前）存在一处真实冲突（把 G0 写成“PDF + 页码 + 段落/表格位置”，并把信息单元切分全部放在 L2），**已在协议修订中收敛（现行 v0.8）**，按以下 canonical 设计落地（协议 §2.9、§3.4–§3.5、§3.10、§17.2）：

- **L1 统一完成文档结构切分与载体规范化（carrier normalization），适用于全部来源**，不再以 PDF 为特例；PDF 额外包含解析，其余来源依既有结构切分；L2 直接接收 `document_unit` 进行语义处理。载体规范化只处理结构和载体（解析、按业务结构切分、表格与 Q&A 结构保留、heading_path / order_index / semantic_key / content_hash 生成、页眉页脚等稳定噪声处理），不做投资语义判断。
- **G0 身份锚 = 自维护原文 + 文件哈希 + 不可变 `document_unit` 快照 + 来源链**；page/bbox 仅为可选复核信息。
- **安全红线**：L1 不得因“像套话”让可能含实质信息的内容（如“重要提示”，常含退市风险、业绩大幅变动）对下游永久不可见。可以标记质量或不为其生成 unit（原文与 parser artifact 仍保留、可重处理），但不得按标签整段清除；raw 兜底仅支持人工复核、不支持自动抽取，故不能以“可回 raw”为由清除实质板块。
- **语义层取舍归 L2**：v0.7 已删除“A 类 / B 类清洗”的叫法，并否决“B 类语义清洗视图”这类二级资产（协议附录 B）。需按上下文判断信息价值的内容不产生独立“语义清洗后文本”，由 L2 通过高价值选择性证据化处理：证据指回不可变 unit（primary_asset_ref + exact_span），语义归一并入证据化动作。
- **双存储**：不可变锚（raw + 哈希 + 不可变 unit）与版本化派生工件（parser artifact、normalized IR）分开存放；证据可复现依赖 `producer_action_ref` 一跳引用与 exact snapshot，不依赖任何“清洗视图”版本。

本服务实施时按此执行。

## 1.3 必须纳入实现的修正

本方案正式采用以下修正：

1. 内部 ID 与 provider ID 分离；
2. 增加 parser-neutral IR；
3. `heading_path` 只做导航，不做身份；
4. table payload 支持多级结构，不把 `headers + rows` 写死；
5. active run 事务化发布，旧 run 永久可查；
6. provider-covered 标准表采用“默认不发布、可按需重新物化”的策略；
7. 整份 PDF 可以重跑，但变更传播按 unit 内容 diff；
8. raw file、parser artifacts、normalized IR、unique 约束按 `(document_id, profile)` 生效；
9. `content_hash` 与 `structure_hash` 分离，标题路径变化不得冒充业务内容变化；
10. 增量同步使用重叠回看窗口，checkpoint 只是优化游标，不是完整性证明；
11. published/referenced artifacts 不得因为“理论上可重跑”而被删除；
12. 人工重跑、发布切换和正式文件补录必须留下服务内 operation log；
13. L1 统一完成文档结构切分与载体规范化（carrier normalization），适用于全部来源（不再以 PDF 为特例）；L2 直接接收 `document_unit` 进行语义处理；
14. 载体规范化只处理结构和载体，不做投资语义：稳定版面噪声（页眉页脚、页码、水印、目录点线、纯排版元素）与明确模板套话可确定性抑制、不生成 unit；安全红线是不得让可能含实质信息的板块（如“重要提示”）对下游永久不可见——被抑制内容下游 claim 抽取无法看到，raw 兜底不支持自动抽取，故拿不准时倾向保留。需按上下文判断信息价值者不在 L1 处理，由 L2 高价值选择性证据化（证据指回不可变 unit，不产生独立“语义清洗后文本”资产）；
15. 双存储与处理分界：不可变锚（raw + 文件哈希 + 不可变 unit）与版本化派生工件（parser artifact、normalized IR）分开存放；“自维护原文 vs 仅登记来源”是真正的处理分界，取代“PDF / 非 PDF”；L2 证据以 producer_action_ref 与 exact snapshot 保证可复现。

---

# 2. 最终目标形态

## 2.1 服务目标

`disclosure_anchor` 最终应成为一套可持续运行的本地披露文件服务：

```text
CNINFO / 交易所公告索引
→ 元数据同步
→ 原始文件下载与不可变归档
→ parser 执行
→ parser-neutral IR
→ 文档结构组装
→ document_unit 候选
→ 保留策略与质量检查
→ 按 profile 原子发布 active run
→ Filing API
→ L2
```

服务对外只承诺：

- 能列出、定位和读取披露文件；
- 能返回默认 `research_default` active run 或调用方指定 profile 的 `text / table / qa`；
- 能按指定历史 run 重现当时内容；
- 能告知下载、解析、结构化和质量状态；
- 能把真实内容变化可靠地通知 L2。

服务不承诺：

- 所有 PDF 都能自动无误解析；
- 所有跨页表都能自动合并；
- 所有文档都有完整标题树；
- 所有表格都能标准化为统一字段；
- parser 版本升级后 unit ID 不变；
- 本服务能够判断内容是否可信或是否影响预测。

## 2.2 架构形态

采用**模块化单体 + ports and adapters**，不拆微服务，不引入 Redis、Celery、Kafka 或独立工作流平台作为第一版必需组件。

```text
┌─────────────────────────────────────────────────────────────┐
│ Entry Points                                                │
│ Python API / FastAPI / CLI / Worker                         │
├─────────────────────────────────────────────────────────────┤
│ Application Use Cases                                       │
│ sync / discover / download / process / publish / query      │
├─────────────────────────────────────────────────────────────┤
│ Domain                                                      │
│ document / run / unit / contracts / state rules             │
├─────────────────────────────────────────────────────────────┤
│ Ports                                                       │
│ source provider / blob store / parser / repositories/outbox │
├─────────────────────────────────────────────────────────────┤
│ Adapters                                                    │
│ CNINFO / local FS / PostgreSQL / MinerU / HTTP              │
└─────────────────────────────────────────────────────────────┘
```

一个代码仓库、一套 PostgreSQL、一个本地文件根目录即可完成第一版。后台任务通过 PostgreSQL 状态和 worker 轮询完成，不增加第二套消息基础设施。

---

# 3. 成熟开源项目的借鉴方式

本项目不复制任何一个仓库，而是明确采用以下模式。

## 3.1 OpenBB：provider adapter 与统一接口

采用：

```text
统一请求模型
→ provider 参数转换
→ 获取原始数据
→ 转成统一返回模型
```

在本服务内用于 CNINFO source adapter；在上位标准数据侧用于 Dataset API。统一的是接口和模型，不要求把所有 provider 数据复制进同一张表。

不直接引入 OpenBB 作为依赖，避免其许可、插件体系和项目规模进入本服务。

## 3.2 EdgarTools：Company / Filing 领域接口

采用：

- 以 Company、Filing、DocumentUnit 作为调用方语言；
- 隐藏数据库表名、parser 和文件路径；
- 查询对象可延迟加载正文、表格和问答；
- API 返回 typed model，不返回 ORM 对象。

## 3.3 secfsdstools：数据库做减法

采用：

- 原始文件和大体量派生产物放文件系统；
- PostgreSQL 管索引、约束、运行、版本和可查询内容；
- 不把 parser 产生的每一个 block、cell、图片都正规化成 SQL 表。

## 3.4 dlt：checkpoint、幂等和 schema contract

采用：

- 增量游标只有在一次同步结果持久化成功后才推进；
- 每次访问和处理都有运行记录；
- 接口输入、IR 和 payload 都有 schema version；
- 未知字段可保留在 metadata，但不能静默破坏已发布契约。

不直接引入 dlt 框架。

## 3.5 CocoIndex：目标状态、内容哈希和最小传播

采用：

- `document_unit` 是当前 processing run 对该文档产生的目标状态；
- 输入文件、处理代码、配置和 payload 都有 hash/version；
- 可以整份重跑，但只传播新增、删除和真实变化的 units；
- 发布一个 run 的所有 units 必须是原子的。

不直接引入 CocoIndex 框架。

## 3.6 Unstructured 与 Docling：parser-neutral document elements

采用：

- parsing/partitioning 先得到结构元素；
- context chunking 是其后的独立消费步骤；
- 统一文档表示允许 text、title、table、picture、层级和可选 provenance 共存；
- parser 自带 page/bbox 可以保留在 IR metadata 或 artifact 中，但不成为业务主键。

这构成本方案 `ParsedDocumentIR` 的直接思想来源。

## 3.7 MinerU：默认 parser，而不是领域模型

MinerU 作为默认 executor，但必须经 adapter 转成 `ParsedDocumentIR`。任何 application/domain 代码不得 import MinerU 类型，也不得读取 `content_list.json` 的具体字段。

---

# 4. 技术基线

除非仓库已经有明确且兼容的技术栈，按以下基线实现：

```text
Language              Python 3.12+
Dependency metadata   pyproject.toml + lock file
API                    FastAPI + Pydantic v2
ORM                    SQLAlchemy 2.x
Migration              Alembic
Database               PostgreSQL
HTTP client            httpx
CLI                    Typer
Retry                   tenacity 或等价的轻量重试封装
Raw/artifact storage   local filesystem through BlobStore port
PDF parser             pinned MinerU executor through ParserExecutor port
```

原则：

- 核心 application 使用同步接口，避免同一代码库同时维护两套 sync/async repository；
- 网络请求设置连接、读取和总超时；
- parser 以 subprocess、container 或独立 HTTP 服务运行，核心进程不直接加载重型模型；
- 版本必须锁定，不在生产环境运行“latest”；
- 配置和 secret 分离，secret 只能来自环境变量或受控 secret provider。

---

# 5. 推荐代码结构

以下结构是规范性模块边界，具体文件可以按现有仓库调整，但依赖方向不得反转。

```text
src/disclosure_anchor/
  domain/
    entities.py
    value_objects.py
    enums.py
    errors.py
    events.py

  application/
    commands/
      manage_tracking.py
      sync_companies.py
      sync_announcements.py
      import_official_document.py
      download_document.py
      process_document.py
      publish_run.py
      reprocess_document.py
      rebuild_units.py
    queries/
      get_company.py
      list_filings.py
      get_filing.py
      list_units.py
      get_unit.py
      build_context.py
    services/
      unit_diff.py
      unit_fingerprint.py
      publication.py
    ports/
      source_provider.py
      blob_store.py
      parser.py
      repositories.py
      unit_of_work.py
      event_outbox.py
      clock.py

  parsing/
    ir_models.py
    adapters/
      mineru.py
      artifact_import.py
    structure/
      heading_tree.py
      table_assembler.py
      qa_assembler.py
      unitizer.py
      policy.py
      quality.py

  infrastructure/
    persistence/
      sqlalchemy_models.py
      repositories.py
      unit_of_work.py
    storage/
      local_filesystem.py
      artifact_manifest.py
    providers/
      cninfo.py
    parser_exec/
      local_subprocess.py
      remote_http.py
    outbox/
      postgres.py

  entrypoints/
    api/
      app.py
      routes.py
      schemas.py
      dependencies.py
    cli/
      main.py
    worker/
      main.py

  config/
    settings.py
    registries.py

migrations/
configs/
  filing_types.yaml
  parser_routes.yaml
  unit_policies/
  semantic_keys.yaml
  provider_coverage.yaml
```

依赖规则：

```text
entrypoints → application → domain
infrastructure → application ports / domain
parsing → domain contracts

domain 不依赖 FastAPI、SQLAlchemy、MinerU、CNINFO 或本地路径
application 不读取 parser 原始 JSON
API 不直接查询 ORM session
```

---

# 6. 核心数据模型

## 6.1 ID 规则

所有内部对象使用系统生成的 UUID 作为主键。

严格区分：

```text
内部 ID
- source_access_id
- document_id
- processing_run_id
- asset_id

外部 ID
- provider_document_id
- CNINFO textid
- announcement_id
- provider company/security id
```

禁止：

```text
document_id = 1225087169
source_access_id = cninfo:p_info3015
```

正确表达：

```text
document_id         = <internal UUID>
provider             = cninfo
provider_document_id = 1225087169

source_access_id     = <internal UUID>
operation            = p_info3015
```

## 6.2 company

用途：公司法律主体。

最低字段语义：

```text
company_id
canonical_name
company_credit_code（可空；统一社会信用代码，主体匹配键 scheme = uscc，中国注册实体“有则必填”，DB 列 unified_social_credit_code）
status
provider_metadata
created_at
updated_at
```

## 6.3 security

用途：证券标识，与公司主体分离。

最低字段语义：

```text
security_id
company_id
market
ticker
security_name
security_type
listed_at（可空）
delisted_at（可空）
status
provider_metadata
```

约束：同一 market + ticker 在有效区间内唯一。

## 6.4 source_access

用途：记录一次真实发生的正式披露侧访问、查询、下载、导入或查空结果。

本仓库只拥有：

- CNINFO / 交易所公告索引查询；
- 正式披露文件下载；
- 官方 URL 获取；
- 人工补录的正式披露文件。

Wind / Tushare / iFinD / Choice 的访问记录归 Dataset API 所属模块；Web、MCP、新闻和研报等轻载外部来源归对应的 L1 轻载外部 adapter（登记为 `data_asset(tool_result)` 或 `source_access`，协议 §3.1 / §3.3），不归 L2，也不属于本服务。本服务可以共享 `source_ref` 引用规范（provenance 引用，可由视图 / endpoint 派生，不要求独立主表），但不得把自己的 `source_access` 扩成整个 L1 的万能访问表。

最低字段语义：

```text
source_access_id
provider
operation / endpoint
request_fingerprint
query_params
started_at
finished_at
status                    success / empty / transient_error / permanent_error
http_status（可空）
result_count（可空）
response_hash（可空）
raw_response_artifact（可空）
error_code / error_message（可空）
attempt_no
correlation_id
```

`request_fingerprint` 由 provider、operation 和规范化 query params 生成，用于重试和查重，但不替代主键。

一次索引查询可以发现多份文档，一份文档也可能被多次查询发现。使用内部关联表：

```text
source_access_document
- source_access_id
- document_id
- relation_kind: discovered / downloaded / refreshed
```

这张表是基础设施关系表，不增加新的公开业务对象。

## 6.5 document

定义：`document` 先承载一条 provider 披露记录；原始文件首次成功下载后，该记录固化为一个具体、不可变的文件字节版本。发现阶段允许 raw 字段为空，`raw_status = available` 之后 raw key/hash/size 不得原地改写。

最低字段语义：

```text
document_id
company_id
security_id（可空）
provider
provider_document_id
byte_version
raw_filing_type
filing_type
title
announcement_date
report_period_end（可空）
report_period_type（可空）
event_time（可空；业务发生时点，日期或时刻，协议 §2.4）
source_url
raw_storage_key
raw_file_hash
raw_size
mime_type
raw_status
first_seen_at
last_seen_at
downloaded_at（可空）
supersedes_document_id（可空）
metadata
created_at
```

关键规则：

1. provider_document_id 与内部 ID 分离；
2. discovery 阶段创建 provisional document，raw 字段可空；首次下载成功后固化该版本；
3. 同一 provider_document_id 后续返回不同字节时，创建新 document，递增 `byte_version`，并通过 `supersedes_document_id` 或版本关系连接；
4. `raw_status = available` 后原文件字段不能覆盖；
5. raw blob 可以按 sha256 物理去重，但来源 document 记录仍分别存在；
6. canonical 报告期使用 `report_period_end + report_period_type`，例如 `2025-12-31 + FY`；`2025A`、`2026Q1` 是其 label 形态。对外 `*_v1` 视图与检索键仍暴露 `report_period`（label 形态，协议 §2.5），与内部 canonical 表示由同一规则互相派生，不得从 public 契约移除；
7. 非定期报告允许报告期为空；
8. provider 原始分类必须保留，规范化 filing_type 由版本化 registry 映射。

建议约束：

```text
已下载版本唯一：(provider, provider_document_id, raw_file_hash)
待下载 provisional 记录：同一 provider + provider_document_id 同时最多一条
```

## 6.6 processing_run

定义：对一个已经固化的 document 执行一次完整的“解析 → IR 规范化 → unitization → policy → quality → publish”运行。

下载不属于 processing_run；下载属于 source access 和 document raw lifecycle。这样 run 的输入始终是一个确定的 raw_file_hash（processing_run.input_sha256 的值即所解析 raw 的 raw_file_hash）。

最低字段语义：

```text
processing_run_id
document_id
profile
parent_run_id（可空）
run_reason                 initial / retry / parser_upgrade / policy_rebuild / manual
parser_name
parser_version
parser_backend
parser_build_or_image_digest（可空）
parser_adapter_version
ir_schema_version
unitizer_version
policy_version
quality_rule_version
config_hash
code_revision
input_sha256
output_hash（可空）
artifact_prefix
artifact_manifest
status                     queued / running / succeeded / failed / published / rejected
quality_status             ok / needs_review / unusable（协议 §3.2 信封枚举；empty 仅用于查空访问记录，不用于 document_unit）
quality_summary
started_at
finished_at
published_at（可空）
error_detail（可空）
is_active
attempt_no
lease_owner（可空）
lease_until（可空）
```

第一版已知 profile：

```text
research_default
  默认研究视图；发布当前 L2 常规需要的 text / table / qa，并抑制已由标准 provider 稳定覆盖的标准报表。

verification_targeted
  针对指定 heading / semantic key / 表族按需物化，用于 provider 缺失、修订、口径异常和重要证据复核。

forensic_full
  人工明确要求时生成尽量完整的结构单元；不是日常默认视图，也不是第一版所有文档的强制完成条件。
```

profile 名称和规则由版本化 registry 管理，不写成不可扩展的数据库 enum。

硬约束：

- 每个 `(document_id, profile)` 同时最多一个 `is_active = true` 的 published run；
- `published` run 的 unit payload 不得原地修改；
- 新 run 失败时同 profile 的旧 active run 保持不变；
- run 的 parser/version/build/config/code 信息必须足以复现当时处理逻辑；
- `output_hash` 基于已发布 unit 的稳定内容与结构指纹集合生成。

## 6.7 document_unit

定义：一个具体 processing run 产生并发布给 L2 的文档结构单元。

最低字段语义：

```text
asset_id
document_id
processing_run_id
payload_kind                 text / table / qa / mixed（0011 起，见 service-purpose §6.5）
order_index
heading_path
title
semantic_key（新 builder 产物非空；历史 run 的库列可空）
semantic_key_version（可空）
payload_schema_version
payload
content_hash
structure_hash
hash_policy_version
quality_status
quality_issues
artifact_locator（可空）
metadata
created_at
```

硬规则：

- unit ID 只要求在本系统内全局唯一，不要求跨 run 相同；
- `heading_path` 可搜索、可显示，但不是唯一键；
- payload 是 L1 对外承诺的内容快照，不是临时 parser 指针；
- 发布后 payload、title、heading_path、content_hash、structure_hash 均不可原地修改；
- 旧 run 的 unit 必须可按 ID 继续查询；
- 删除 active 资格不等于删除数据。

## 6.8 source_checkpoint

只用于真正需要增量轮询状态的数据流，不假设来源提供严格 CDC。

最低字段语义：

```text
source_checkpoint_id
provider
scope_key
high_watermark
lookback_policy
pagination_state
last_attempt_at
last_success_at
last_access_id（可空）
state
state_version
updated_at
```

约束：

- checkpoint 只在该批全部分页、source_access 和发现结果均成功提交后推进；
- 失败、部分提交或来源总数不一致时不能推进；
- 每次同步仍使用可配置的重叠回看窗口和幂等 upsert；
- checkpoint 只是优化下一次查询，不承担“没有漏公告”的完整性证明；
- provider-specific 状态必须有 `state_version`。

## 6.9 内部 tracking_subscription

这是 operator input `tracked_companies` 的持久化实现，不是新的对外领域产物：

```text
tracking_subscription_id
security_id
enabled
priority
history_start_date（可空）
filing_type_policy
polling_policy
created_at
updated_at
```

禁止只把跟踪公司清单放在无人审计的本地文本文件中。CLI/API 对其修改必须进入 `operation_log`。

## 6.10 内部 outbox_event

为保证“发布 run”和“通知 L2”不丢失，增加内部 transactional outbox：

```text
seq（单调递增，change feed 游标；现实现为 bigserial 主键，可与 outbox_event_id 合一）
outbox_event_id
event_kind
aggregate_type
aggregate_id
payload
created_at
published_at（可空）
attempt_count
last_error（可空）
```

`seq` 是对外 change feed（`change_events_v1` / `GET /v1/changes?after_seq`）的单调游标；消费协定为 at-least-once 投递 + 消费端幂等 + 同一 subject 内保序（协议 §2.8）。

它不是 L1 业务对象，不进入 Filing API；只用于可靠交接。

## 6.11 内部 operation_log

记录会改变服务状态的人工动作：

```text
operation_log_id
actor
action
target_type
target_id
reason（可空）
correlation_id
request_snapshot
result_status
created_at
```

至少覆盖：

- 开启 / 停用跟踪；
- 强制 backfill；
- 人工导入正式披露；
- 重跑和 targeted materialization；
- active run 切换；
- 手工重试、放弃或隔离异常文档。

它不是上位系统完整 `action_log`（曾用名 M-LOG），但必须能解释本服务内部是谁、何时、为什么做了什么。人工命令不得直接改表绕过 application service。

---

# 7. 文件与 artifact 存储

## 7.1 BlobStore 抽象

领域层只使用 opaque storage key，不直接拼本地绝对路径。

```python
class BlobStore(Protocol):
    def put_atomic(self, key: str, source_path: Path) -> BlobInfo: ...
    def open(self, key: str) -> BinaryIO: ...
    def exists(self, key: str) -> bool: ...
    def stat(self, key: str) -> BlobInfo: ...
    def verify(self, key: str, sha256: str) -> bool: ...
```

第一版实现 `LocalFilesystemBlobStore`。未来迁移到 MinIO/S3 时不改变 document 或 API 语义。

## 7.2 推荐物理布局

```text
raw/
  cninfo/
    ab/
      <sha256>.pdf

artifacts/
  <document_id>/
    <processing_run_id>/
      manifest.json
      parser_raw/
      normalized_ir.json
      unit_candidates.json
      quality_report.json
      publication_manifest.json
```

目录只用于物理组织，不是业务身份。

## 7.3 原子写入

所有下载和 artifact 输出必须：

1. 写入临时目录或临时文件；
2. 计算 sha256、size、mime；
3. 校验文件可读；
4. 使用原子 rename/move 发布；
5. 最后写数据库记录。

不得先在数据库标记成功，再写文件。

## 7.4 artifact manifest

每个 run 必须产生 manifest：

```json
{
  "schema_version": "artifact-manifest.v1",
  "document_id": "...",
  "processing_run_id": "...",
  "input_sha256": "...",
  "artifacts": [
    {
      "kind": "parser_raw",
      "storage_key": "...",
      "sha256": "...",
      "size": 123,
      "mime_type": "application/json"
    }
  ]
}
```

DB 中保存 manifest 或其 storage key + hash。备份、校验和故障排查都以 manifest 为入口。

保留规则：

- 原始 PDF 按字节版本长期保留；
- published run 或已被 L2 引用的 artifact manifest 不得原地覆盖或垃圾回收；
- 只有未发布、未引用且超过保留期的 staging / failed 临时产物可以清理；
- 是否可删由引用和 retention policy 决定，不能只因为“理论上能够重跑”就删除。

---

# 8. Parser 边界与 normalized IR

这是本方案最关键的实施设计。由于 parser 服务尚未完成、输出尚未在全量文件上验证，业务代码必须避免绑定任何一个 parser 的具体 schema。

## 8.1 两层 parser 接口

### ParserExecutor

负责实际执行 parser，返回 opaque artifacts：

```python
class ParserExecutor(Protocol):
    def probe(self, document: RawDocumentRef) -> DocumentProfile: ...
    def execute(self, request: ParseRequest) -> ParseExecutionResult: ...
```

第一版：

- `LocalMinerUSubprocessExecutor`：调用锁定版本的 MinerU CLI/container；
- `ArtifactImportExecutor`：导入人工提前生成的 parser artifact，用于 parser 服务尚未完成时先开发后续流程；
- `RemoteParserExecutor`：接口预留，但不要求第一批实现。

### ParserOutputAdapter

负责把具体 parser 输出转换为统一 IR：

```python
class ParserOutputAdapter(Protocol):
    def supports(self, execution: ParseExecutionResult) -> bool: ...
    def normalize(self, execution: ParseExecutionResult) -> ParsedDocumentIR: ...
```

`MinerUOutputAdapter` 内部可以分别处理 pipeline、VLM 和具体版本差异；这些差异不得泄漏到 unitizer。

## 8.2 DocumentProfile

preflight 只负责路由和风险识别，不判断业务价值：

```text
mime_type
file_size
page_count（可空）
encrypted
has_extractable_text
text_coverage_estimate
scan_likelihood
parser_route_hint
warnings
```

阈值全部配置化，不能写成架构常量。默认路由：

```text
文本层正常 → MinerU pipeline
文本层不足 / pipeline unusable → MinerU VLM fallback
关键表复核 → Camelot / pdfplumber / Docling 按需
```

## 8.3 ParsedDocumentIR

IR 是文件系统中的版本化 artifact，不是新的数据库事实表。

最小模型：

```python
class ParsedDocumentIR(BaseModel):
    schema_version: str
    source_sha256: str
    parser_name: str
    parser_version: str
    parser_backend: str
    adapter_version: str
    elements: list[ParsedElement]
    warnings: list[ParseWarning] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

class ParsedElement(BaseModel):
    local_id: str
    kind: Literal[
        "document_title", "heading", "text", "list_item",
        "table", "image", "formula", "page_break", "unknown"
    ]
    order_index: int
    text: str | None = None
    heading_level: int | None = None
    table: ParsedTable | None = None
    parent_local_id: str | None = None
    source_locator: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

`source_locator` 可以自然保留 parser 的 page、bbox、charspan 或 block id，但：

- 可空；
- 仅用于调试和复核；
- 不参与业务 ID；
- 不要求不同 parser 兼容；
- 默认只存在于 IR/artifact，不需要升格为数据库列。

## 8.4 ParsedTable

不能把所有表写死成单行 headers + rows。IR 至少支持：

```python
class ParsedTable(BaseModel):
    title: str | None = None
    unit: str | None = None
    matrix: list[list[str]] | None = None
    header_row_count: int | None = None
    row_header_column_count: int | None = None
    spans: list[CellSpan] | None = None
    html: str | None = None
    captions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
```

要求：

- cell 值保留原始字符串；
- `matrix` 和 `html` 至少有一种可用结构表示；
- 多级表头、合并单元格和空白单元格不得被强制压成错误的简单表；
- 原始 HTML 可以放 artifact，只在 IR 中存引用；
- 无法可靠结构化时仍可保留 raw representation，并标记 `needs_review`。

## 8.5 IR 的兼容策略

- IR 自身有 `schema_version`；
- adapter 输出必须先通过 Pydantic 校验；
- parser 新增未知字段放 metadata；
- parser 删除必要字段时 adapter 明确失败，不可静默生成空 unit；
- adapter 版本写入 processing_run；
- IR 变更需增加 migration/conversion function，使旧 artifact 仍能读取。

---

# 9. 从 IR 到 document_unit

## 9.1 中间候选而不是直接入库

流程：

```text
ParsedDocumentIR
→ HeadingTree
→ UnitCandidate[]
→ retention policy
→ quality evaluation
→ PublishedUnit[]
→ database transaction
```

`UnitCandidate` 和被抑制内容保存在 `unit_candidates.json`，不必建数据库表。这样以后更改保留策略，可以从 normalized IR 或 candidates 重建 units，而不必重新 OCR。

## 9.2 HeadingTree

标题树构建顺序：

1. parser 明确给出的 heading level；
2. A 股常见编号和标题规则；
3. 字体/版面信号只能由 adapter 转成一般 metadata 后使用；
4. 无法确认的标题不得被强行升格。

输出的 `heading_path` 是标题字符串数组。

规则：

- 路径可不完整；
- 重复标题允许存在；
- 同一路径下可有多个 units；
- path 变化不代表内容变化；
- 不用 path 生成主键。

## 9.3 text unit

生成原则：

- 以真实章节、子标题、显式编号事项为边界；
- 连续正文可以合并为一个完整小节；
- 表格和独立 Q&A 不混入 text payload；
- 去除页眉页脚、重复公司名称、页码、水印等确定性噪声；
- 不做摘要，不改写原意，不由 LLM 判断重要性。

payload 最低契约：

```json
{
  "schema_version": "text.v1",
  "text": "机械清洗后的完整正文"
}
```

可选保留段落边界，但不能要求 parser 必须输出 paragraph id。

## 9.4 table unit

业务 payload 不强制等同 ParsedTable。最低契约：

```json
{
  "schema_version": "table.v1",
  "unit": "元",
  "matrix": [
    ["账龄", "期末账面余额", "期初账面余额"],
    ["1 年以内（含1 年）", "1,765,831,017.43", "1,653,778,854.38"]
  ],
  "header_row_count": 1,
  "row_header_column_count": 1,
  "spans": [],
  "notes": [],
  "nearby_explanation": "",
  "representation_refs": []
}
```

对简单表，API 可以额外返回派生的 `headers/rows` convenience view；但 canonical payload 不能只允许一层 headers。

跨页表处理：

- table assembler 尝试根据标题、重复表头、列数、列签名和相邻顺序合并；
- 只有高置信度时自动合并；
- 不确定时保留为多个 units，设置共同 `table_group_key` 和 `suspected_split` warning；
- 不得为了满足“一张逻辑表”而错误拼接。

相邻解释：

- 单位、脚注、表前/表后直接解释可并入 table payload；
- 独立管理层分析仍是 text unit；
- 绑定依据和规则版本记入 metadata。

## 9.5 qa unit

识别：

- 明确的“问题/回答”“问/答”“Q/A”结构；
- 问询函中的问题及完整回复；
- 投关记录中的编号问题和完整答案。

payload：

```json
{
  "schema_version": "qa.v1",
  "question": "...",
  "answer": "...",
  "question_no": "1",
  "speakers": []
}
```

规则：

- 一个答案很长也不持久化拆成 chunks；
- 答案中独立表格生成 table unit，qa metadata 可记录关联 unit 的 local ref；
- parser 无法可靠分出问答时，降级为 text unit，不强行猜测。

## 9.6 runtime context packaging

Filing API 提供临时上下文包装能力：

```text
完整 unit
→ 按 max_chars / max_tokens 临时摘取
→ 返回 asset_id + start/end + exact excerpt + excerpt_hash
```

该结果不写入 `document_unit` 表，不成为证据身份。L2 若使用摘录，应在 claim 中保存摘录快照。

---

# 10. 保留、抑制与按需物化

## 10.1 Policy Engine

保留策略是版本化、可配置、确定性的 rule set：

```text
filing type
+ heading/title pattern
+ payload_kind
+ provider coverage
+ table signature
→ publish / suppress / review
```

配置文件存于 Git，并计算 `policy_version/config_hash` 写入 run。

## 10.2 安全默认值

- 明确模板废话、页眉页脚、目录点线和重复免责声明：suppress；
- 明确由标准数据 provider 稳定覆盖的标准表：默认 suppress from published units；
- 预测可能相关且规则不确定的内容：publish，使用真实通用键
  `semantic_key = document_content`（不得伪造具体主题）；
- 结构严重损坏：review 或 unusable；
- 不允许 LLM 作为唯一删除依据。

“去掉”只表示不发布 unit；raw、parser raw、IR 和 suppression manifest 仍保留。

## 10.3 semantic_key registry

`semantic_key` 只能来自版本化 registry，例如：

```text
business_overview
segment_revenue
volume_sales_inventory
receivable_aging
inventory_composition
capex_projects
risk_factor
future_outlook
```

规则：

- 允许为空；
- 不允许模型自由创造不可追踪的 key；
- 每个匹配记录 rule id/version；
- key 只是粗路由，不是事实 schema；
- 表族晋级为标准 dataset 后，原 table unit 仍保留。

## 10.4 provider-covered 表的 fallback

标准三大表等默认不发布为 `research_default` units，但必须可回到官方披露。采用以下唯一实现：

1. parser 原始结果和 normalized IR 保留标准表候选；
2. `research_default` policy 默认抑制这些 candidates；
3. 当 Dataset API 缺失、口径争议或人工要求官方原文时，调用 `rebuild_units`；
4. `rebuild_units` 以 `verification_targeted` profile 生成新 processing run，只发布指定 heading / semantic key / 表族；
5. 只有人工明确要求完整复核时才运行 `forensic_full`；
6. 只有某类官方表已经有稳定、可验证的抽取器后，才可注册为正式 Dataset adapter。

因此：

```text
official filing fallback
≠ 自动成为标准 dataset provider

它首先是可按需物化的 Filing API 内容。
```

---

# 11. 内容哈希、版本和 diff

## 11.1 四类 hash

```text
raw_file_hash
  原始文件字节

artifact_hash
  parser / IR / artifact 文件内容

content_hash
  payload_kind + canonical payload，代表业务内容

structure_hash
  normalized title + heading_path，代表文档结构和导航
```

四者不得混用。尤其不能把 `heading_path` 放进 `content_hash`，否则 parser 只调整标题层级也会被误判成公司披露内容变化。

## 11.2 hash 规范

`content_hash` 的 canonicalization policy 必须版本化。

包含：

- payload_kind；
- canonical payload。

排除：

- title、heading_path、order_index；
- unit ID、processing_run_id；
- parser version；
- page / bbox / source locator；
- semantic_key、quality status；
- created_at 和调试 metadata。

`structure_hash` 包含：

- normalized title；
- normalized heading_path。

`order_index` 单独比较，不放入结构哈希，避免前文新增一个单元后造成整份文档级联变化。

规范化至少包括：

- Unicode NFC；
- 行尾统一；
- JSON key 稳定排序；
- 保留数字、负号、括号、小数点和单位原串；
- 只折叠确定性的 parser 空白，不做语义改写。

`hash_policy_version` 必须随 unit 保存。run 的 `output_hash` 基于已发布 units 的稳定 `(content_hash, structure_hash, semantic_key, quality_status)` 有序清单生成，只表达该 profile 的发布视图。

## 11.3 run 级匹配与 diff

发布新 run 前，将新 units 与同一 profile 的旧 active run 比较。

匹配优先级：

1. `content_hash + structure_hash` 相同：`unchanged`；
2. `content_hash` 相同、`structure_hash` 不同：`structure_only_changed`；
3. `structure_hash` 相同、`content_hash` 不同：`content_changed`；
4. 同 kind + 标题/路径相似 + 顺序接近 + payload signature 相近：候选匹配；
5. 无法可靠匹配时按 added / removed / ambiguous 处理，不伪造稳定映射。

change set：

```text
unchanged
added
removed
content_changed
structure_only_changed
routing_only_changed
quality_changed
policy_visibility_changed
ambiguous
```

解释：

- `routing_only_changed`：semantic_key 等路由信息变化；
- `quality_changed`：质量状态或 issue 变化；
- `policy_visibility_changed`：candidate 内容未变，只因 profile / policy 改变而发布或抑制；
- `removed` 只有在来源内容确实不再存在时才表示内容删除。仅仅不再发布不能伪装成公司撤回披露。

判断 change class 时必须同时考虑：

```text
raw_file_hash
profile
run_reason
parser / adapter / unitizer version
policy_version
quality_rule_version
content_hash
structure_hash
```

原则：

- parser 可以整份重跑；
- 旧 claim 不迁移，继续引用旧 unit；
- 同一 raw 文件因 parser / policy 升级产生的表示变化，不能冒充来源内容变化；
- `structure_only_changed` 和 `routing_only_changed` 默认不触发 L3；
- `quality_changed` 和 `ambiguous` 交给 L2 / 人机待办判断；
- 新 raw 文件版本通过新 document / supersedes 关系表达，并单独发送 source-version event。

## 11.4 active run 发布事务

一个 run 的发布必须在同一数据库事务内：

1. 校验该 profile 的全部 document_units 和 artifact manifest 已完成；
2. 写入 run quality/output hash；
3. 将同 `(document_id, profile)` 的旧 active run 置为 inactive；
4. 将新 run 置为 published + active；
5. 写入 outbox event；
6. commit。

任一步失败全部 rollback，同 profile 的旧 active run 不受影响。

---

# 12. 数据接入与运行管道

## 12.1 公司与证券同步

```text
tracked_companies
→ CNINFO company/security adapter
→ source_access
→ upsert company/security
→ checkpoint commit
```

必须保留 provider 原始 response artifact 或 response hash，便于字段变化排查。

## 12.2 公告索引同步

```text
read source_checkpoint
→ 根据 high_watermark + overlap window 生成查询区间
→ p_info3015 分页/增量查询
→ 每页创建 source_access
→ normalize announcement metadata
→ 幂等 upsert document stub
→ link source_access_document(discovered)
→ 校验分页完成、来源总数和本批处理一致
→ commit documents + source access
→ advance checkpoint
```

规则：

- 每次增量同步使用可配置的重叠回看窗口，覆盖延迟出现、补录和元数据变化；
- 初次历史回填按时间窗口拆批，避免单次结果上限；
- 任一分页失败、返回总数不一致或部分提交时，不推进 checkpoint；
- 重复返回只更新 `last_seen_at` 和来源关系，不重复创建 document；
- 幂等身份以 provider + provider_document_id 为基础；下载后再用 raw_file_hash 和 byte_version 区分真实文件版本。

## 12.3 下载

```text
claim pending document
→ download to temp
→ validate HTTP / mime / file signature / size
→ calculate sha256
→ atomic put BlobStore
→ create or resolve immutable document version
→ link source_access_document(downloaded)
→ mark raw available
```

重试规则：

- timeout、429、5xx：transient；
- 401/403：凭证或权限错误，停止盲目重试；
- 404：记录 permanent/temporarily_missing，保留待人工复核；
- 内容不是 PDF：保留响应摘要并标记 invalid_content；
- 重试使用指数退避和 jitter。

## 12.4 processing worker

worker 从 PostgreSQL 领取 queued run：

```text
SELECT ... FOR UPDATE SKIP LOCKED
→ set lease_owner / lease_until / running
→ parser executor
→ adapter normalize IR
→ unitizer
→ policy
→ quality
→ artifact manifest
→ publish transaction
```

worker 崩溃后，lease 过期的 run 可重领。不能仅依赖进程内队列。

## 12.5 调度入口

第一版提供：

```text
disclosure-anchor track add/remove/list
disclosure-anchor sync-companies
disclosure-anchor sync-announcements
disclosure-anchor download-pending
disclosure-anchor enqueue-processing
disclosure-anchor worker
disclosure-anchor reprocess <document_id> --profile research_default
disclosure-anchor rebuild-units <document_id> --profile verification_targeted
disclosure-anchor serve
```

周期调度可由 cron/systemd/launchd 调用 CLI。服务内部不先引入大型 orchestrator。

---

# 13. 状态与错误模型

## 13.1 document raw status

```text
discovered
→ download_pending
→ downloading
→ available

failure branches:
transient_failed
permanent_failed
invalid_content
```

解析是否成功不塞进同一个 raw status；当前可用解析由 active processing run 表达。

## 13.2 processing_run status

```text
queued
→ running
→ succeeded
→ published

failure/review:
failed
rejected
```

`quality_status` 独立于运行状态：

```text
ok
needs_review
unusable
```

例如 parser 成功执行但表格结构严重损坏：`status=succeeded, quality_status=needs_review`，是否发布由 policy 决定。

## 13.3 错误对象

内部错误分为：

```text
SourceAccessError
DownloadError
InvalidDocumentError
ParserExecutionError
ParserOutputContractError
IRValidationError
UnitizationError
QualityGateError
PublicationConflictError
StorageIntegrityError
```

每类错误记录：

- stable error code；
- human message；
- retryable；
- stage；
- underlying exception type；
- safe context；
- correlation/run ID。

不得把 token、secret、完整敏感响应写入日志。

---

# 14. Filing API

## 14.1 API 原则

- Python API 是领域入口；
- FastAPI 只是相同 application query 的 HTTP adapter；
- 返回 Pydantic response models；
- 不返回 ORM 对象和本地绝对路径；
- 默认只读 active run；
- 可显式指定 processing_run_id 读取历史快照；
- 所有列表接口使用稳定 cursor pagination；
- unit payload 必须完整可取，不能只返回搜索摘要；
- HTTP 读侧语义与 `disclosure_public.*_v1` 一致；unit 响应携带派生字段 `asset_uri`（仅序列化层派生，不落库）。

## 14.2 Python API 形态

```python
client.company("002484")

filings = client.filings.list(
    company="002484",
    filing_type="annual_report",
    report_period_end="2025-12-31",
    report_period_type="FY",
)

filing = client.filings.latest(
    company="002484",
    filing_type="annual_report",
    report_period_end="2025-12-31",
    report_period_type="FY",
)

units = client.units.list(
    document_id=filing.document_id,
    payload_kind="table",
    semantic_key="receivable_aging",
)

unit = client.units.get(asset_id)
context = client.units.context(asset_id, max_chars=12000)
```

可以额外提供 EdgarTools 风格 façade：

```python
filing.text_units()
filing.tables()
filing.qa_items()
filing.units(heading_prefix=["第三节", "管理层讨论与分析"])
```

façade 内部仍调用 application queries。

## 14.3 HTTP API

最低端点：

```text
GET  /v1/companies/{ticker}
GET  /v1/filings
GET  /v1/filings/{document_id}
GET  /v1/filings/{document_id}/runs
GET  /v1/filings/{document_id}/units
GET  /v1/units/{asset_id}
GET  /v1/units/{asset_id}/context
GET  /v1/processing-runs/{processing_run_id}
POST /v1/filings/{document_id}/reprocess
POST /v1/filings/{document_id}/rebuild-units
GET  /v1/changes?after_seq=...
```

写操作需要管理权限；读取 raw/artifact 不直接暴露文件路径，可通过受控 endpoint 或签名/内部 URI 获取。

## 14.4 查询语义

支持：

```text
company / ticker
filing_type
report_period_end / report_period_type
announcement_date range
document_id
processing_run_id
profile
payload_kind
heading_path exact/prefix
semantic_key
title contains
asset_id
quality_status
```

默认查询：

- 只读取 active published `research_default` run；
- 排除已 superseded 的 document；
- `latest()` 使用稳定排序：`announcement_date DESC, byte_version DESC, document_id DESC`；
- 调用方可显式指定 profile、processing_run_id 或历史 document 版本。

`heading_path` prefix 查询只是候选过滤，不承诺唯一结果。

## 14.5 对外错误模型

HTTP / MCP 读侧错误码至少包含（协议 §3.11）：

```text
L1_PROCESSING_REQUIRED      请求对象仅有 raw 登记、尚未完成载体规范化（L1 读侧语义）
NOT_FOUND                   对象不存在
CONTRACT_VERSION_MISMATCH   请求契约版本与服务暴露版本不一致
GONE_SUPERSEDED             对象已被新版本取代，响应携带 superseded_by 指引
```

---

# 15. L2 交接

## 15.1 交接对象

L2 消费：

```text
document_run_published / document_units_changed 等 outbox 事件（event_kind，snake_case）
+ Filing API document metadata
+ exact document_unit payload
```

事件不携带 claim，不替 L2 判断重要性。

## 15.2 outbox event

事件模型按 milestone 05 §2-U5 定案：**逐 unit 细粒度事件，不用单事件携带 changes 字典**
（早期草案的聚合 changes 结构已废弃——每事件一个 subject_ref 才符合协议 §2.8 的事件形状，
L2 可按 unit 精确失效）：

```text
processing_run_published   发布 1 条；materialized ⇔ content_hash_aggregate 变化，否则 observed
document_unit_created      新增内容的 unit 每个 1 条（materialized，payload 携带 new_asset_id）
document_unit_removed      消失内容的 unit 每个 1 条（materialized，payload 携带 old_asset_id——
                           L2 持有旧 asset_id，只给 content_hash 无法撤销）
document_unit_projection_changed
                           内容同、投影（title/heading_path/semantic_key/quality_status）变的
                           unit 每个 1 条（materialized，携带 old/new asset_id 与 changed_fields）
```

diff 按 multiset + 稳定配对计算（重复内容不丢计数，milestone 05 §2-U5）；一次内容修改 = removed + created 两条事件。L2 收到后按需查询完整 payload。
`change_kind` 是 outbox 真实列（0007 起），仅 `observed` / `materialized` 两值
（service-purpose §12.2）；下游失效只由 materialized 触发。

## 15.3 exact snapshot 的责任

- L1 保证历史 unit payload 和 content_hash 不变；
- L2 在形成 claim 时保存实际使用的原文摘录、表格行或数值快照；
- L1 不为每条 claim 预先复制 snapshot；
- 旧 claim 引用旧 unit，不因 active run 变化而失效。

---

# 16. 配置与 registry

## 16.1 filing type registry

保留：

```text
provider raw category
provider raw title
normalized filing_type
mapping rule id/version
```

未知类别映射为 `other`，不得丢弃。

## 16.2 parser route registry

示例语义：

```yaml
routes:
  - when:
      mime_type: application/pdf
      has_extractable_text: true
    executor: mineru_pipeline
  - when:
      scan_likelihood: high
    executor: mineru_vlm
fallback: mineru_vlm
```

阈值和 executor 名称记录在 config hash 中。

## 16.3 processing profile 与 unit policy registry

第一版至少支持：

```text
research_default
verification_targeted
forensic_full（可选人工 profile）
```

每个 profile 只是版本化 policy bundle，不是另一套领域模型。

按 filing type 分文件：

```text
annual_report.yaml
quarterly_report.yaml
investor_relations.yaml
inquiry_reply.yaml
short_announcement.yaml
```

规则必须有：

```text
rule_id
version
match conditions
action
reason
```

## 16.4 provider coverage registry

只描述“哪些标准内容默认由 Dataset API 覆盖”，例如：

```yaml
financial.income_statement:
  coverage: standard_provider
  suppress_official_filing_unit_by_default: true
  fallback_materialization_profile: verification_targeted
```

它不能把 `document_unit` 冒充成 provider record。

## 16.5 配置版本记录

每个 processing_run 保存：

```text
parser config hash
unitizer version
policy version/hash
semantic key registry version
code revision
```

不能只记录“MinerU”而不记录具体运行配置。

---

# 17. 质量、可观测性和安全

## 17.1 轻量质量检查

document：

- 文件 magic 与 mime 一致；
- 文件可打开；
- 文件非空、未截断；
- 加密状态明确。

IR：

- schema 校验通过；
- elements 非空；
- order_index 无明显冲突；
- parser warning 被保留。

text：

- 非空；
- 非全页眉页脚；
- 乱码比例不异常；
- 标题与正文关系基本成立。

table：

- 有结构表示；
- 行列数合理；
- 原始数字字符串未被自动转 float；
- 显式合计可做轻量检查；
- 合并/跨页不确定时标记 warning。

qa：

- question 和 answer 非空；
- 问答顺序合理；
- 无法确认时降级 text。

质量结果除 `ok / needs_review / unusable` 外，还必须保存机器可读 issue codes，例如：

```text
empty_text
invalid_pdf
missing_heading_tree
table_header_missing
ragged_table
unit_missing
qa_boundary_ambiguous
partial_parse_failure
parser_timeout
```

发布规则：

- `unusable` run 不得成为 active；
- 少量 `needs_review` unit 不必阻止其余可用 unit 发布，但 API 和 outbox 必须携带质量状态；
- 单个 table unusable 不应让整份文档永久卡死；
- fallback 只在明确条件下触发，禁止 parser 无限轮转；
- 自动修复不得覆盖 parser 原始字符串或 published payload。

## 17.2 日志与指标

结构化日志必须包含：

```text
correlation_id
source_access_id
document_id
processing_run_id
stage
provider / parser
status
duration_ms
error_code
```

基础指标：

```text
source requests / empty / failures
new documents discovered
raw downloads / bytes / failures
processing runs by status
parse duration
unit counts by kind
quality warnings
active run publication count
outbox backlog
```

## 17.3 运行安全

PDF 和 parser 都视为不可信输入：

- parser 在隔离 subprocess/container 中运行；
- 不拼接未经校验的 shell 命令；
- 文件名不直接作为路径；
- storage key 经过白名单校验，防止 path traversal；
- 配置最大文件大小、最大页数、最大运行时间和最大输出量；
- parser timeout 后终止子进程并清理临时目录；
- API 写操作鉴权；
- secret 不落库、不写日志、不进入 artifact。

## 17.4 一致性检查命令

实现运维命令：

```text
disclosure-anchor verify-storage
disclosure-anchor verify-manifests
disclosure-anchor list-failed-accesses
disclosure-anchor list-failed-runs
disclosure-anchor retry-run <run_id>
disclosure-anchor replay-outbox
```

这些是实施能力，不替代后续测试方案。

---

# 18. 数据库实施约束

## 18.1 通用规则

- 使用 `timestamptz`，统一存 UTC；
- 业务主键用 UUID；
- JSON 字段使用 JSONB；
- 所有表有 created_at，必要时有 updated_at；
- 不用数据库 cascade 删除 document → runs → units；
- raw document、published run、published unit 禁止硬删除；
- published 或已被引用的 artifact manifest 禁止删除；
- 只有未发布、未引用且超过保留期的临时/失败 artifact 可由显式清理任务清理；
- migration 只能前向演进，不依赖手工改库。

## 18.2 关键索引与约束

至少包含：

```text
security(market, ticker)
document(provider, provider_document_id, raw_file_hash)
document(company_id, announcement_date desc)
document(filing_type, report_period_end, report_period_type)
processing_run(document_id, created_at desc)
partial unique: processing_run(document_id, profile) where is_active = true
document_unit(processing_run_id, order_index)
document_unit(document_id, payload_kind)
document_unit(semantic_key)
document_unit(content_hash)
source_access(request_fingerprint, started_at desc)
source_checkpoint(provider, scope_key)
tracking_subscription(security_id) where enabled = true
outbox_event(published_at, created_at)
operation_log(target_type, target_id, created_at desc)
```

对 title/heading/text 的全文或 trigram 索引等真实查询出现后再加，不在首个 migration 中堆砌。

## 18.3 JSONB 使用边界

升为列：

- 经常过滤、排序、关联和唯一约束的字段；
- ID、日期、类型、状态、hash、版本。

留在 JSONB：

- provider-specific response；
- parser-specific metadata；
- unit payload；
- warnings、quality summary、artifact manifest。

不得把所有字段都塞进一个万能 JSONB。

## 18.4 Schema 分层与 public 读契约

数据库按三 schema 分层（协议 §3.10、service-purpose §12.1；迁移 0001–0006 已落地）：

```text
disclosure_core     私有写侧表（company / security / source_access / document /
                    processing_run / document_unit …）
disclosure_public   只读视图 *_v1（documents_v1 / document_units_v1 /
                    source_refs_v1 / change_events_v1），跨服务唯一读契约
disclosure_ops      outbox_event 与迁移记录
```

规则：

- 跨服务与 L2 只经 `disclosure_public.*_v1` 或等价 Filing API 消费，不回读私有表（协议硬边界 40）；
- `document_units_v1` 必须保留 service-purpose §12.1 的全部 unit 级 scope keys（company_ref、security_ref、filing_type、report_period、announcement_date、payload_kind、heading_path、semantic_key、quality_status、content_hash、contract_version、producer_action_ref、source_ref、parent_ref、order_index）与 `contract_version = document_unit.v1`；
- `source_ref` 由 `source_refs_v1` / `GET /v1/units/{id}/source-ref` 派生，不建独立 source_ref 主表；
- `asset://` URI 只在 API / MCP 序列化边界派生为 `asset_uri` 字段，不落数据库列与视图；
- 视图破坏性变更走 v2 并行 + 弃用期（协议 §2.7）。

---

# 19. 实施顺序

不按工时拆分，按可运行纵向闭环推进。本节是目标蓝图，不反映当前进度；当前已实现范围以仓库事实、
migration head、`docs/implementation/milestones/` 的 frontmatter 与
`docs/implementation/checks/acceptance-matrix.md` 为准，会话级进度不构成架构权威。

## Stage 1：工程骨架与持久化

交付：

- Python package、settings、日志、错误模型；
- PostgreSQL + Alembic；
- company/security/source_access/document/processing_run/document_unit/source_checkpoint；
- internal tracking_subscription/source_access_document/outbox_event/operation_log；
- repository + unit of work；
- LocalFilesystemBlobStore；
- 基础 CLI。

完成条件：可以手工创建公司、document、run、unit，并通过 repository/API 读回。

## Stage 2：CNINFO 元数据与原文件闭环

交付：

- token 管理；
- p_stock2100 / p_stock2101；
- p_info3005 / p_info3015；
- checkpoint、重叠回看窗口、分页完整性、重试和 source_access；
- document stub；
- 原子下载、sha256、immutable document version。

完成条件：tracked company 能持续发现新公告，重复同步不产生重复 document，文件变化产生新版本。

## Stage 3：parser contract 与 artifact import

交付：

- ParserExecutor/ParserOutputAdapter；
- DocumentProfile；
- ParsedDocumentIR v1；
- ArtifactImportExecutor；
- artifact manifest；
- processing worker/lease。

先使用人工生成或现有样本 artifact 跑通后续链路，避免等待 parser 服务完成。

完成条件：任意符合 IR contract 的 artifact 能进入 unitization，不依赖 MinerU 字段。

## Stage 4：MinerU adapter

交付：

- 锁定版本的 MinerU executor；
- pipeline/VLM 输出适配；
- parser 输出完整保留；
- normalize 到 IR；
- fallback routing；
- timeout/resource limits。

完成条件：三类样本文件均能产生 IR，失败时有明确状态和可复查 artifact。

## Stage 5：unitization 与 publication

交付：

- HeadingTree；
- text/table/qa assemblers；
- table merge warnings；
- policy engine；
- semantic key registry；
- quality report；
- content hash / structure hash / diff；
- research_default 与 verification_targeted profile；
- active run 按 profile 原子发布；
- suppressed candidate manifest。

完成条件：年报、投关记录、短公告分别产生 text/table/qa，旧 run 仍可查。

## Stage 6：Filing API

交付：

- Python client/domain façade；
- FastAPI read endpoints；
- active/historical run 查询；
- heading path/semantic key/title filters；
- transient context packaging；
- 管理型 reprocess/rebuild endpoints。

完成条件：L2 无需打开 PDF 或理解 parser schema，即可读取完整 unit。

## Stage 7：diff、outbox 与 L2 handoff

交付：

- run diff；
- document_run_published outbox；
- event polling/dispatch；
- structure-only vs content change；
- replay outbox；
- no-op reparse detection。

完成条件：同内容重跑不触发无意义下游更新，真实内容变化可靠进入 L2 待处理队列。

## Stage 8：运行强化

交付：

- storage/manifest consistency commands；
- stale lease recovery；
- failure retry tools；
- metrics；
- backup manifest；
- official fallback materialization；
- parser/profile/policy 版本回放。

完成条件：服务可以长期增量运行、失败可恢复、历史可复现。

---

# 20. 三类首批样本的实施目标

样本用于驱动实现，不在本文件展开测试用例。

## 20.1 江海股份 2025 年年度报告

目标：

- 建立多级 heading tree；
- 生成管理层讨论 text units；
- 生成经营细分和财务附注 table units；
- 标准三大表默认不发布，但候选可按 verification_targeted profile 物化；
- 对应收账款账龄的小计/明细结构忠实保留，不在 L1 重新解释。

## 20.2 美的集团投资者关系活动记录

目标：

- 文档元数据进入 document；
- 每个明确问题 + 完整回答形成一个 qa unit；
- 长回答不持久化拆 chunk；
- 回答中的独立表格可生成关联 table unit。

## 20.3 江海股份年度权益分派实施公告

目标：

- 生成少量完整 text/table units；
- 不在 L1 创建 dividend event；
- L2 能从 units 中抽取分配基数、每股金额、登记日和除权日。

---

# 21. AI coding agent 的强制规则

## 21.1 不得做

- 不得直接使用 CNINFO textid 当内部主键；
- 不得把 endpoint 名称当 source_access_id；
- 不得在 domain/application 中解析 MinerU 原始 JSON；
- 不得假定 `content_list.json` 永远存在或字段固定；
- 不得把 `heading_path` 设成唯一键；
- 不得依赖 page/bbox 才能查询 unit；
- 不得创建 persistent chunk、table_cell 或 embedding 表；
- 不得把 table 全部压成简单一层 headers；
- 不得原地更新 published unit payload；
- 不得在发布事务提交前通知 L2；
- 不得在 L1 生成 claim、canonical event 或规范化 metric；
- 不得因为规则不认识某段内容就静默丢弃原文件或 IR；
- 不得把 Dataset API/Wind 全部实现在 disclosure_anchor 仓库中；
- 不得把 `source_access` 扩成整个 L1 的万能来源表；
- 不得把展示字符串 `2025A` 当作 canonical 报告期；
- 不得让 policy visibility 变化伪装成来源内容删除。

## 21.2 必须做

- 所有外部输入经过 adapter 和 schema validation；
- 所有 raw/artifact 有 hash；
- 所有 run 记录 parser、adapter、policy、config 和 code version；
- 所有发布使用 transaction；
- 所有历史 run/unit 可按 ID 查询；
- 所有 unknown parser metadata 可保留；
- 所有 retry 有幂等语义；
- 所有增量同步使用 overlap window，且 checkpoint 只在整批成功后推进；
- 所有 API response 与 ORM 解耦；
- 所有 active run 约束按 `(document_id, profile)` 生效；
- 所有 manual command 经过 application service 并写 operation log；
- 所有 unit 同时维护 content_hash 与 structure_hash；
- 所有偏离本文件的实现决策写 ADR，并说明原因和迁移路径。

## 21.3 代码提交粒度

每次提交应形成一个可运行闭环，例如：

```text
migration + ORM + repository + application use case + CLI/API
```

不接受：

- 只生成几十个空接口；
- 只定义 schema 不接入 use case；
- 只写 parser 脚本不写 run/status/artifact；
- 只写 API route 直接访问数据库；
- 用 TODO 替代关键事务和幂等语义。

---

# 22. 实施完成定义

本实施方案完成后，系统应具备：

1. tracked companies 的公告增量发现与原文件归档；
2. immutable document version 和完整来源访问记录；
3. tracked companies 可持久化、可审计；
4. 增量同步有 overlap window，checkpoint 只在整批成功后推进；
5. parser 可替换，业务代码不依赖具体 parser 输出；
6. normalized IR 和完整 artifact lineage；
7. text/table/qa 三类 unit 的结构化发布；
8. 多级表头、复杂表格和跨页失败的非破坏性表达；
9. research_default / verification_targeted 能并存，active run 按 profile 原子切换；
10. provider 标准表默认抑制但可按需物化；
11. content_hash 与 structure_hash 分离，表示变化不冒充来源内容变化；
12. Filing Python API、HTTP API 和 CLI；
13. worker 租约、重试、checkpoint 和故障恢复；
14. outbox 驱动的 L2 可靠交接；
15. published/referenced artifacts 不被错误清理；
16. 人工运维动作可审计；
17. 不依赖 page、bbox、chunk、table_cell 或向量库完成主闭环；
18. parser、policy 和 schema 继续演进时，不需要推翻数据库和调用接口。

---

# 23. 明确后置事项

以下不进入本次实施：

- 测试方案和完整测试矩阵；
- L2 claim 抽取；
- 证据账本和预测模型；
- Wind/Dataset API 的具体 provider 实现；
- 全市场附注表标准化；
- 通用 `work_item` 任务队列表；第一版优先使用 document/run 状态 + PostgreSQL lease 驱动，只有状态查询已证明不足时再增加统一队列；
- 向量检索、reranker 和 RAG；
- 图数据库；
- 自动事实裁决；
- 自训练 OCR/表格模型；
- 全量跨 parser cell 级对账；
- 依赖 page/bbox 的人工标注 UI。

这些后置事项不应反向污染当前核心 schema。

---

# 24. 参考项目与官方资料

- OpenBB provider / router architecture
  - https://docs.openbb.co/odp/python/developer/architecture_overview
  - https://docs.openbb.co/odp/python/developer/extension_types/provider
  - https://github.com/OpenBB-finance/OpenBB
- EdgarTools Company / Filing / Data Objects
  - https://edgartools.readthedocs.io/en/latest/api/company/
  - https://edgartools.readthedocs.io/en/latest/api/filing/
  - https://edgartools.readthedocs.io/en/latest/concepts/data-objects/
- secfsdstools
  - https://github.com/HansjoergW/sec-fincancial-statement-data-set
- dlt incremental loading / state / schema contract
  - https://dlthub.com/docs/general-usage/incremental-loading
  - https://dlthub.com/docs/general-usage/destination-tables
  - https://dlthub.com/docs/general-usage/resource
- CocoIndex incremental target-state model
  - https://cocoindex.io/docs/getting_started/overview/
  - https://cocoindex.io/docs/programming_guide/core_concepts/
- Unstructured partitioning / elements / chunking separation
  - https://docs.unstructured.io/open-source/core-functionality/partitioning
  - https://docs.unstructured.io/open-source/core-functionality/chunking
- DoclingDocument
  - https://docling-project.github.io/docling/concepts/docling_document/
- MinerU output contract and releases
  - https://opendatalab.github.io/MinerU/reference/output_files/
  - https://github.com/opendatalab/MinerU/releases

---

# 25. 一句话收口

`disclosure_anchor` 的成熟实现不是把 PDF parser 的输出搬进数据库，而是建立一条稳定边界：

```text
不可变原文件
→ 可替换 parser
→ 版本化 normalized IR
→ 可重建 document_unit
→ 原子发布 Filing API
→ 只把真实内容变化交给 L2
```

parser 可以继续变化，文档类型可以继续扩展，保留策略可以继续训练；只要这条边界保持，后续代码就不需要因某一个解析器、某一种表格或某一次切分规则变化而推倒重来。
