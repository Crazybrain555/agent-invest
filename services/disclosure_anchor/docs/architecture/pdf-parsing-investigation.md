# PDF 解析方案调研（开源框架 + 输出格式）

本文是对 `open-source-references.md` 中 PDF→Markdown/JSON、表格抽取部分的专项深入调研，回答两个问题：

1. 有没有解析做得好的开源框架，财报解析是不是“规则一样、没那么难”？
2. PDF 解析成 **Markdown** 是不是最合适的目标格式？

> 调研时间：2026-06（2026-06-20 按 v1.0 canonical 设计对齐）。
>
> **对齐说明**：框架选型结论（默认 MinerU，Docling/Camelot/pdfplumber 按需）不变；但解析的**入库目标**已按
> `service-purpose.md` 和 `财报与披露数据接入及切分方案.md` 改为 `document_unit`
> （text/table/mixed；qa 仅为历史产物），不再以复制
> parser 的 `page/block/cell` 为目标。表格“双解析对账”从“第一版每张财报页必做”改为**按需触发**（默认单 parser）。
> 服务边界以 `service-purpose.md`（canonical）为准，实施顺序与取舍见《财报与披露数据接入及切分方案》（implementation_plan）。
>
> **2026-07-26 更新**：后文“标题正则足够”“MinerU 原生跨页合并完成”和“L1 生成 QA”的
> 早期判断已被真实语料否定。当前把 MinerU typed blocks、page/model provenance、TOC/几何和页图
> 视为结构证据；Markdown 只是展示格式。文档级结构恢复可借鉴
> [MinerU-Popo](https://github.com/opendatalab/MinerU-Popo) 的页面图像 + OCR blocks +
> document-tree 任务拆分，但其标点/短语候选启发式必须先经过本地跨公司正反语料校准，不能直接
> 成为删除或边界规则。
>
> **2026-08-12 cutover**：当前唯一 writer 是 MinerU 3.4.4 Hybrid-medium →
> `provider_document.v1` → `provider_unit_locator.v2`（历史 v1 只读）。后文 NormalizedIR、双 parser、规则修复和
>旧版本号只属于调研历史；不得据此恢复生产路径。
>
> **2026-08-14 数字保真边界**：不恢复默认双 parser。MinerU 仍唯一拥有 block/order/bbox/table；
> admission 只可在同一 hash-bound PDF、同一 MinerU text bbox 内，用 native PDF text 修正“native
> 相对 MinerU 仅新增完整数字核心（可连同或保留 `%/‰`），其余字符与已有数字原序逐字相等”的
> 漏数，并在 locator v2 记录双侧 hash。无 text layer、表格、数字替换/重排、非数字差异、旋转、
> 高度重叠 bbox 或坐标/身份不闭合时仅保持 MinerU 原文；当前不额外声称已产生独立质量告警。
> native reader 固定 `pypdfium2==5.13.0`。
>
> 服务目的见 `service-purpose.md`：我们要的不是“把 PDF 下载到文件夹”，也不是镜像 parser 的每一页每一格，而是把
> 按真实文档结构形成包含正文、表格、caption/footnote 与视觉证据的完整 evidence block，
> 让 L2 能从任一命中点取得可回放上下文。问答等业务语义由 L2 从这些证据中抽取。
>
> 本文中的 license / 平台支持等关键事实以各项目**官方仓库**为准（部分第三方对比博客信息已过期，见下方“事实更正”）。

---

## 一、结论（TL;DR）

1. **默认解析后端用 MinerU**。中文版面/表格是它的强项（CJK SOTA），已支持 macOS 14+ / Apple Silicon，license 已从 AGPL 改为基于 Apache 2.0 的自有协议，商用更友好。它同时输出 Markdown + 结构化 JSON（含页码、bbox、块类型、表格 HTML），便于从中**派生** `document_unit`。
2. **Docling 作为第二后端 / 交叉校验**，按需启用。MIT 协议、Linux Foundation 项目、`DoclingDocument` 结构化模型，适合复杂件对照。
3. **Camelot / pdfplumber 作为 text-based PDF 的表格 fallback 与“数字对账”工具**，输出 DataFrame，便于在需要时和 MinerU 的表格做一致性校验。
4. **格式不是“二选一”，而是双轨**：
   - 正文/章节/item → **Markdown**（可读、好切块）；
   - 表格 → **保留结构化形式（headers/rows/notes/原始字符串），不要拍平成 Markdown 管道符**。财报表格拍平成 `| a | b |` 会丢精度。
   - 两者都来自 parser artifact。MinerU 的 `middle.json` 保存
     page→block→line→span 与 bbox 的结构化中间结果，`content_list.json`
     是从中派生的扁平阅读序投影；两者都留在 `parser_artifacts/`，真正入库的是
     可回放 source edge 所派生的 `document_unit` 内容快照。
5. **你的假设“财报规则一样、没那么难”——只在模板层成立**：章节外观高度规则，
   但 heading 识别、标题层级、caption 归属、跨页 logical row、阅读顺序与数字保真都会失真。
   结构恢复和表格保真必须同等对待；每次修复都要有 source identity、结构证据和跨公司正负回放。

---

## 二、直接回答你的两个假设

### 假设 A：「财报解析应该都是一样的规则，没那么难」

**部分成立。**

- ✅ **成立的部分**：A 股定期报告有稳定的章节和编号语法，因此 parser typed heading、
  文档自身 TOC、通用 outline grammar、bbox/阅读顺序可以互相校验。编号或标点只能作为
  结构证据组合的一部分，不能靠标题词表/正则单独决定边界。
- ❌ **不成立的部分**：**表格才是难点，而不是版面**。财报里的财务报表：
  - 大量**合并单元格**、多级表头（“本期/上期”、“合并/母公司”）；
  - **跨页表格**不能靠 parser 猜成一个逻辑 `table`；canonical evidence 先按物理页闭合，
    跨页关系另行证明，且必须能回到 model/page bbox、crop 和源 HTML；
  - 数字格式坑：括号表负数 `(1,234)`、负号、千分位逗号、单位（元/万元/亿元）、`-` 表示空值；
  - 数字一旦 OCR 错位（小数点、负号、漏位），下游所有计算都错——这正是 **FinCriticalED**（2026 金融 fact-level OCR 基准）证明的：通用解析器在金融表格上“文本看着对、数字未必对”。

**结论**：版面解析借现成框架就够；**表格数字要保留原始字符串 + 轻量质量检查 + 按需复核**，这是第一版要设计进去的，但不等于每张表都强制双 parser 对账。

### 假设 B：「PDF 解析成 Markdown 是不是最合适？」

**Markdown 是正文的最佳格式，但不是表格的最佳格式，也不能是唯一格式。**

业界 2026 的共识（多篇 RAG 解析评测一致）：
- **Markdown 适合正文**：保留标题层级、列表、阅读顺序，切块干净。
- **JSON/结构化适合精确字段和表格**：需要“表头、行、单元格原值”时，Markdown 表达力不够。
- **生产最佳实践 = 两者都留**（hybrid）。

对**我们这个项目**尤其如此：`service-purpose.md` 要求表格以完整结构（表名、单位、表头、行数据、脚注、原始字符串）保存为 `table` 单元。这只能靠结构化产物，Markdown 管道符表格做不到。

**结论**：
- 把 **Markdown 当正文（`document_unit(text)`）的展示/检索副本**，留在 `parser_artifacts/`；
- 把解析器的**结构化中间产物（带 bbox/page/type/表格 HTML）作为切分依据**，也留在文件系统；
- 入库的事实是从中派生的 `document_unit`：`text` 存正文，`table` 存
  `headers/rows/notes/unit`，`mixed` 按源顺序组合同一结构 occurrence 下的异构证据。
  page/bbox/source item/hash chain 保存在 locator/provenance，不能因组块而丢失。

---

## 三、候选框架对比（2026-06 现状）

| 框架 | 类型 | License | macOS/Apple Silicon | 中文/CJK | 表格 | 速度 | 输出 | 适合角色 |
|---|---|---|---|---|---|---|---|---|
| **MinerU** (现役 3.4.0) | 版面模型 + 可选 VLM | MinerU 协议（基于 Apache 2.0） | ✅ 14+，MPS / `vlm-mlx` 加速 | ✅✅✅ SOTA | ✅✅✅ HTML + page/model 结构；生产关闭跨页表合并并做页内闭合 | Pipeline 快(CPU可跑)，VLM 需 GPU | **Markdown + JSON（content_list/model，带 bbox/page）** | **默认后端** |
| **Docling** | 版面模型 | MIT（Linux Foundation） | 部分（CPU 慢） | ✅ 好 | ✅✅ | 中等，CPU 偏慢 | DoclingDocument → md/json/html | **第二后端 / 交叉校验（按需）** |
| **Marker** | 版面模型 | 商用有营收门槛（采用前复核） | ✅ MPS | ✅ 好 | ✅✅ | GPU 很快 | Markdown/JSON | 候补，授权需确认 |
| **PaddleOCR-VL** | VLM | Apache 2.0 | 需确认 | ✅✅ 强 | ✅✅ | 需 GPU | Markdown/JSON | 中文 OCR 强，可作 VLM 候选 |
| **PyMuPDF4LLM** | 原生文本抽取 | **AGPL-3.0 / 商用付费**（注意） | ✅ CPU | 基础 | ✅ 一般 | **最快**，无 ML | Markdown | 纯数字版 PDF 快速兜底 |
| **Camelot** | 表格专用 | MIT | ✅ CPU | n/a | text-PDF 强 | 快 | DataFrame/CSV/JSON | **表格 fallback / 数字对账（按需）** |
| **pdfplumber** | 文本/表格 | MIT | ✅ CPU | n/a | text-PDF 中 | 快 | dict/表格 | 轻量表格 + 字符级定位 |

### 事实更正（重要）

第三方对比博客（如 themenonlab、Spheron）仍在写 “MinerU 是 AGPL、不支持 macOS”——**已过期**。以官方仓库为准：

- **MinerU v3.3.1（2026-06-11 发布）**：协议为 “MinerU Open Source License, based on Apache 2.0”；**支持 Windows/Linux/macOS 14+**；Apple Silicon 走 MPS，另有 `vlm-mlx-engine` 后端（相对 transformers 后端 100%–200% 提速）。这对你（darwin 本机）很关键。

---

## 四、为什么默认选 MinerU

针对“中文财报 + 本地数据库”这个具体场景，MinerU 的契合点：

1. **中文是第一公民**：OmniDocBench 上 CJK 版面/识别 SOTA，支持 109 种语言；A 股 PDF 大多是数字版（非扫描），命中它最强的场景。
2. **页内表格闭合**：生产 MinerU 关闭跨页表合并；content/model 的每张表必须在同一
   物理页以唯一 bbox 和完全相同的 logical cells 一一闭合，并保留非空 HTML、grid 与 crop。
   任一侧缺失、为空、歧义或不等价即解析失败。跨页逻辑关系若需要，作为独立派生关系建立，
   不拼接或改写 canonical page-local evidence。
3. **输出天然分层**，正好用于派生 `document_unit`：
   - `*.md` → 正文 Markdown 副本（artifact）；
   - `middle.json` → page/block/line/span、bbox 与复杂版面结构，供缺陷回放和二次开发；
   - `content_list.json` → 扁平阅读序载体；legacy 标题是
     `type=text + text_level>=1`，不是 `type=title`；
   - 表格块以 **HTML** 给出（保结构），公式给 LaTeX。
   - 这套结构按 heading occurrence 重组成完整 text/table/mixed evidence blocks；
     问答、事件或财务事实属于 L2/L3 抽取，不在 L1 用词面重写。
4. **两档后端，按 PDF 质量分流**：
   - **Pipeline 后端**：CPU 可跑（16GB+ RAM），适合干净的数字版 PDF（A 股年报多数属于此类），成本低；
   - **VLM/Hybrid 后端**：复杂版面/扫描件，精度更高（OmniDocBench ~95），需要 GPU（8GB+）或在 Apple Silicon 上用 `vlm-mlx`。
5. **license 风险下降**：从 AGPL 改为 Apache-2.0 基础协议，作为本地服务后端更可用（仍建议采用前读一遍附加条款）。

**MinerU 的注意点**：
- VLM 后端要 GPU 才划算；纯 CPU 上优先用 pipeline 后端。
- 它仍是“概率模型”，**表格数字不能盲信**（见第五节质量策略）。
- 数据库要保留 `processor`、`processor_version`、`backend`、`processing_run`，允许同一 `document` 多次重跑、多版本并存（一个 active run）。

---

## 五、财报特有的硬骨头 + 质量策略

FinCriticalED / Agentar-Fin-OCR 这类金融专用工作给出的明确信号：**通用解析器在金融数字与结构上都有错误率**。
`needs_review` 只能是故障信号，不能作为问题解决；命中后必须继续获取并比较 raw PDF、
MinerU model/middle/content-list、页图或独立 parser 证据，直到证明修复或明确定位上游能力缺口：

1. **数字版优先**：A 股 PDF 多为文本层 PDF。先判定“有无文本层”，有就走 pipeline / pdfplumber 直取，OCR 只作为扫描件的退路——能取文本层就别 OCR，精度最高。
2. **表格按需双解析对账**：默认**单 parser**。只有出现以下情况才用 Camelot/pdfplumber 二次解析并比对关键数字（总资产、营收、净利润等可加总校验的行）：
   - 默认 parser 失败或关键表结构明显错误；
   - 某张表即将进入正式预测 / 重要 claim，需要增强复核；
   - 轻量质量检查（如显式合计加总）不通过。
   不一致 → 记录精确表/行/页/bbox 与 reason code，进入定向复核；不能静默选一份或只打状态。
   是否扩大到全量 cell 对账由实际故障分布决定。
3. **结构化保真**：表格存为 `document_unit(table)` 的 `headers/rows/notes` + **原始字符串**（行列结构保留，不拆 cell）。负号、括号、千分位、单位都按原值保留，规范化（转 float）放到下游 L2，并保留原值。表格 HTML/cell JSON 可作为 parser artifact 留在文件系统。
4. **可重跑、可追溯**：每次解析写 `processing_run`，记录后端/版本/耗时/错误，永不覆盖旧结果——换 parser 或升级模型可以回溯对比；旧 claim 继续引用当时的 `processing_run + document_unit + exact snapshot`。
5. **轻量勾稽校验**（质量检查的一部分）：有显式合计时做简单加总检查，例如账龄表“合计 = 各账龄之和”、利润表分项加总。失败必须留下可定位证据并触发进一步调查；自动修复只接受可回放、可逆且经正负语料证明的结构族。

---

## 六、和数据对象的映射

把解析产物落到 `service-purpose.md` 的 `document_unit` 体系：

```text
PDF
 ├─ PDF native structure/text → StructTree/MCID/bookmark + 独立文本 occurrence
 │   └─ 原生文本缺失或几何异常页 → hash-bound lossless full-page PNG
 └─ MinerU(pipeline 或 vlm)    → parser_artifacts/（文件系统，可重生成，不是 DB 事实）
     ├─ content_list.json      → typed carrier + 扁平阅读序
     ├─ content_list_v2.json   → 可选 typed title block；按 MinerU 3.4 官方 serializer
     │                           投影后与 legacy 同页/等价 bbox/唯一 carrier 闭合
     ├─ model/middle.json      → page/block/line/span + bbox 结构回放依据
     └─ source-bound proof     → 按 PDF hash/page/MCID/bbox/具体 field 唯一对齐
         ├─ proved heading     → document_unit heading_path/title
         ├─ type=table         → logical grid 闭合后 payload{unit, headers, rows, notes}
         ├─ 同一 proved heading occurrence 下的异构证据 → mixed，保持 source order
         └─ native/visual fallback → 无标题 document-level 粗证据，供 L2 直接召回
 └─ Camelot/pdfplumber(按需，仅 text-PDF 表格)
     └─ 对账结果              → 定位差异；needs_review 只是调查状态，不是终态
processing_run: processor, processor_version, backend, status, error, duration, is_active_run
artifact_locator(可选): artifact_path + artifact_unit_ref + order_index   # 回看用，不要求 page/bbox
```

要点：**结构来自 source-bound typed proof，不来自业务词面或 legacy `text_level`。**
v1/v2 的 Markdown escape 与 inline-equation delimiter 是同一 provider block 的格式差异，
只能精确投影，不能用 bbox overlap、`text_level` 或模糊反转义补齐。
parser artifact 本体不入核心库，但其 hash/role/locator 与派生的 `document_unit` 快照共同进入
证据闭环；caption、unit、footnote 和 taxonomy 都不能反向决定标题或边界。

---

## 七、第一版建议路线

1. **闭环与现役实现**：MinerU 产物已映射为 NormalizedIR 并生成/发布 document units；
   结构变更必须用登记 source hash 绑定的真实 artifact 离线重放，不能只验证合成 fixture。
2. **分流策略**：有文本层 → pipeline/pdfplumber；扫描/复杂 → VLM 后端（macOS 上 `vlm-mlx`，有 GPU 用 GPU）。
3. **表格复核按需**：默认单 parser；仅在第五节列出的触发条件下用 Camelot/pdfplumber 二次解析与关键数字比对。
4. **入库**：按第六节映射写 `document_unit` / `processing_run`，parser artifact 留文件系统，必要时记 `artifact_locator`。
5. **Docling 作对照（按需）**：对默认 parser 表现差的文档类型，用 Docling 跑一遍对比，决定是否按文档类型路由到不同后端。

**第一版先不做**：自训模型、全市场全 A 全量（第一版只覆盖 ≥500 只人工精选池）、向量库、claim 抽取、`page/block/cell` 镜像表、全量双 parser 逐格对账、所有 PDF 与标准数据 provider 的逐项核验（与 `service-purpose.md` / `财报与披露数据接入及切分方案.md` 的取舍一致）。

---

## 八、风险与开放问题

- **license 复核**：MinerU 自有协议附加条款、Marker 商用门槛、PyMuPDF(4LLM) 的 AGPL，采用前都要再读一遍。本文以 2026-06 官方信息为准，license 会变（MinerU 就刚从 AGPL 改过）。
- **算力**：VLM 后端要 GPU 才划算；纯 CPU/Mac 以 pipeline 后端为主，复杂件再考虑 `vlm-mlx` 或外置 GPU。
- **数字与结构保真上限**：再好的 parser 都会错。原值、页图、page/model/bbox、hash 与可重跑能力
  是继续找证据的基础；`needs_review` 必须携带具体缺口并进入调查，不能替代修复或复核。
- **是否需要金融专用模型**：Agentar-Fin-OCR / PaddleOCR-VL 等值得跟踪；但第一版用 MinerU + 轻量质量检查已足够，等闭环稳定后再评估是否引入。

---

## 参考来源

- MinerU 官方仓库（license / macOS / 后端 / 输出，v3.3.1）：https://github.com/opendatalab/MinerU
- MinerU 官方结构化输出契约（title/table body/caption/footnote、page/bbox）：https://opendatalab.github.io/MinerU/reference/output_files/
- MinerU-Popo 文档级后处理仓库与论文：https://github.com/opendatalab/MinerU-Popo ；https://arxiv.org/abs/2605.24973
- MinerU Changelog（MLX 后端、协议变更）：https://opendatalab.github.io/MinerU/reference/changelog/
- Docling 论文（MIT、DoclingDocument）：https://arxiv.org/pdf/2501.17887
- Docling document hierarchy/provenance 与 hierarchical/hybrid chunking：https://docling-project.github.io/docling/concepts/docling_document/ ；https://docling-project.github.io/docling/concepts/chunking/
- Unstructured `by_title` 与 `orig_elements` 来源恢复：https://docs.unstructured.io/open-source/core-functionality/chunking
- OmniDocBench（CVPR 2025 文档解析基准，2026 更新 MinerU2.5 / PaddleOCR-VL）：https://github.com/opendatalab/OmniDocBench
- FinCriticalED（金融 fact-level OCR 基准，数字保真难点）：https://arxiv.org/pdf/2511.14998
- 开源 PDF→Markdown 工具对比（2026，Marker/Docling/MinerU/pdf-craft/PyMuPDF4LLM）：https://themenonlab.blog/blog/best-open-source-pdf-to-markdown-tools-2026
- 自托管文档智能（Docling/Marker/MinerU 生产部署对比）：https://www.spheron.network/blog/self-host-document-intelligence-docling-marker-mineru-rag-guide/
- RAG 解析格式 Markdown vs JSON vs flat text：https://ocrqueen.com/blog/document-extraction-rag-markdown-vs-json-vs-flat-text
- 2026 RAG PDF 解析器评测（Firecrawl）：https://www.firecrawl.dev/blog/best-pdf-parsers
