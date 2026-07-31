# adapters/parsers/mineru — PDF/MinerU 证据适配与 NormalizedIR v4

管道：`mineru_process.py`（subprocess 调 CLI，剥离代理 env，HTTP 内并发显式限额；
timeout/worker stop 先 SIGINT 走 MinerU 官方临时 API cleanup，超时才强杀）
→ `artifact_reader.py`（定位/读 content_list 及同 stem 可选 model，形状不符
→ParserOutputContractError）→ PDF 原生适配器（`pdf_native_structure.py` 读取 StructTree/MCID/
bookmark，`pdf_native_text.py` 读取独立文本 occurrence，`pdf_visual_evidence.py` 对原生文本
缺失/几何异常页生成 hash-bound 全页 PNG，并只为未获完整原生文本覆盖的 typed carrier
生成合并后的同页 bbox PNG；另外为每个 image/chart source occurrence 生成按原索引独立、
绝不合并的 PDF 原位 crop）→ `structure_proof.py`（只把可唯一绑定到
MinerU carrier 的 typed heading/显式父子关系写入闭合 proof）→
`table_reconciler.py`（在关闭 MinerU 跨页表合并的前提下，对 content/model 两套输出执行
严格的页内一一闭合：同页 bbox 唯一匹配且 logical cell 的文本、`th/td`、rowspan、colspan
完全相同；每张表还必须有非空 HTML 和已登记的安全 crop）→ `mapper_to_ir.py`
（content_list + structure/source evidence → normalized_ir.v4）。上述 raw PDF + 既有 MinerU
artifacts 到 current IR 的唯一编排在 `existing_artifact_pipeline.py`；production parser 与 corpus
source replay 均调用它，`parser.py` 只负责运行 MinerU、登记/持久化产物和组装 ParserResult。
MinerU 是一个 typed provider，不是 PDF 结构真值；parser/mapper 不做
投关问答、业务内容替换或词面标题推断。

IR v4 当前契约（schema：`contracts/normalized_ir/normalized_ir.v4.json`；v2/v3 仅供既有 artifact
识别，禁止新写且不得直接进入当前 BuildUnits）：

- kind ∈ {text, heading, table, image, equation, page_furniture, unknown}；
  新写入的 legacy `content_list` 只接受 MinerU 官方 discriminated types；`type=list` 的纯字符串
  `list_items` 按原顺序以换行连接成 `kind=text, raw_kind=list`（空/全空白数组是可证明空 carrier），
  `type=code` 的 body/caption/footnote 精确保真并以 source-item hash 锚定。已知类型坏 shape、
  未支持 type 或未消费的 content-looking 字段均 `ParserOutputContractError`，不能再生成空
  unknown 后由 builder 静默丢弃；`kind=unknown` 仅保留为旧 artifact 的兼容读形状。
- heading 判定只接受 `structure_proof` 中 source-bound 的 PDF `H1`–`H6`、唯一书签
  occurrence 或 MinerU v2 typed title。legacy/v2 必须先按 MinerU 3.4 同一有序 `para_blocks`
  序列逐页逐 block 完成 typed text projection；v2 title 只按该页内 ordinal 映射绑定到 canonical
  legacy text carrier，禁止再用转义后文本/bbox 二次猜配，也禁止 bbox overlap 或 `text_level` 兜底。
  legacy `text_level` 只作 provider annotation/冲突诊断，不能单独开节、定级或建立父子关系；
  caption、unit、footnote、taxonomy 永不成为标题。
- 同一 carrier 的 heading level/native ancestry 冲突时不发布该结构候选；父边冲突或不连续时
  不发布该边。原 carrier 仍按 source occurrence 保留，缺失的原生文本/几何页由 source-evidence
  fallback/visual guard 闭环，禁止改成任意一级标题或最近标题归属。
- source-evidence v8 对每个非空 typed text field 发布唯一 `native_exact` 或 `visual_bound`
  disposition；后者只绑定原 carrier 的 source-PDF bbox/hash，不复制或改写文本。MinerU
  `type=image` 的 `text/content` 是 `generated_annotation`，必须绑定同 source item 的 image
  artifact，且不得进入 source-text support、heading 或 section proof。每个 `image/chart`
  必须在 `visual_occurrences` 中与 content-list index、完整 provider-item hash、raw kind、
  page/bbox 和独立 source-PDF crop 一一闭合；即使 crop 字节相同也不得合并 occurrence。
  `type=chart` 的非空 `text/content` 标为 `visual_recognition`，强制以本 occurrence crop
  `visual_bound`，绝不能因词面恰好出现在 PDF native text 中而升级为 `native_exact`。
  caption/footnote 仍是普通 source carrier。每个 image/chart、图像型 equation、table
  embedded media 和 visual-only page 都必须由精确 crop/media bytes 形成
  `semantic_text|unresolved` disposition；`unresolved` 独立阻断 Build 与 Publish。相邻单元格、
  caption、alt/title、结构路径和相邻证据都不能替代该 occurrence 的语义，也不能靠词面猜配。
- 表结构化：rowspan/colspan 感知的 HTML→grid；**headers 只在 `<th>` 证据时非空**
  （MinerU 输出纯 td，headers 通常为空、全网格在 rows；用户 2026-07-16 裁决
  **不做表头提升**——无 th 证据时 headers 保持空、忠实落盘，表头解释归 L2/视图层；
  不要在任何层猜表头，实测会错标续表/KV 表）
- 表 HTML 必须解析出至少一个 logical cell；空 HTML、零 cell、缺 crop、坏路径均在 parser
  边界失败，不写 `table_parse_failed` 占位状态。
- model 对账不得依赖标题、科目词或监管 taxonomy：仅支持已验证的 pipeline/VLM schema，
  content 与 model 的每张表必须在同一物理页上以唯一 bbox（归一化坐标最大差 3/1000）
  和完全相同的 logical cells 一一配对。content/model 任一侧缺表、多表、空表、歧义或不等价
  都 fail closed；不跨页串接、不修补 HTML、不抑制续页首行、不生成 aggregate locator，
  且 reconciler 返回的 content carrier 与输入逐项相同。
- `parser_diagnostics.table_reconciliation` 只记录
  `mineru-page-local-table-closure.v6`、精确 model hash、三方相等的表计数与闭合布尔值；
  `parser_artifacts.model` 必须存在并与该 hash 绑定。任何旧算法或旧 locator shape 都不能进入
  当前写入或 BuildUnits。
- mapper 不做业务判断（单位识别/保留跳过在 05 builder；表头提升已整体废除）

golden fixtures 再生成：`scripts/regen_phase00_fixtures.py` 不带参数时从本地 source artifact
重跑 reader→reconciler→mapper→builder；仅 builder 变化才用 `--units-only` 从 committed IR
重建 units（旧 fixture 只作历史兼容输入；当前发布门必须重 parse 为 v4；document_id 必须保持
phase00_<key>）。source artifact 不在
clean checkout 时，parser/reconciler 由真实样本测试或 `make test-mineru-smoke` 覆盖，不得把
builder-only fixture 更新当成 parser 回归。
