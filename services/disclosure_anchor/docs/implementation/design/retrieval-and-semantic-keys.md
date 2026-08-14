# Provider-native 检索投影与语义键

状态：current（2026-08-13）。本文件只描述当前 writer；旧 NormalizedIR/unit-source-projection
实现从工作树删除，需要考古时查 Git。

## 1. 边界

L1 保存 source-bound Unit，并生成可完全重建的检索投影。检索投影不是来源证据，不改变
`payload`、`content_hash`、`query_projection_hash`、`structure_hash`，也不产生 outbox 事件。
自然语言 claim/evidence/forecast 仍属于 L2。

当前唯一新 writer 是 MinerU 3.4.4 Hybrid-medium：

- `provider_document.v1` 保存官方 provider artifact 的闭合投影与 hash-bound inventory；
- `provider_unit_locator.v2` 保存 Unit 对 source block、逻辑表物理段、evidence digest、检索目标与
  可选的 source-PDF 数字校正 provenance；历史 v1 继续只读；
- 历史 `normalized_ir.v4` 只允许通过窄 resolver 读取已发布 evidence，不再 Build、Rebuild、
  Publish 或重建检索投影。

## 2. Unit 可检索字段

每个 Unit 的投影只有四个输入面：

1. `title`：已接受的 source heading 叶标题；metadata document title 绝不复制到 Unit。
2. `heading_path`：已接受 heading occurrence 的完整根到叶路径。
3. body：只回放 `provider_unit_locator.v2.search_targets` 明确列出的 provider payload destination。
4. `semantic_key` / `semantic_keys`：可选的真实受控 Unit **直接主题**；scalar 是稳定 lead，数组是
   完整有序集合，所有项都进入 key channel，避免 mixed/长 Unit 的 secondary route 漏召回。
   Provider writer 使用版本化闭集词表和 source-bound candidate gate：Document 的 filing type 和
   authoritative disclosure topics 只负责开放对应 scope，不能独立成为 Unit route 证据；provider
   content categories 只作 facet/模型上下文，绝不授权 scope。
   Unit 自身标题标准化后唯一精确命中可确定性落键；严格两列表单字段与有结构证据的表头也可
   形成 Unit-local direct route；标题包含式/字符相似命中只生成候选；其余候选才可能由低成本模型
   逐候选返回闭合布尔裁决。模型不能造 key、决定 Unit 边界，或只凭文档标题、
   父标题、类别传播 route。无充分证据时两列仍写 NULL；不以 `document_content` 占位。
5. `section_keys`：确定性的结构位置。只从已接受 `heading_path` 根到叶精确匹配 taxonomy
   中显式结构容器；定期报告使用 `context_container`，事件公告仅开放少量命中 filing_type/
   authoritative disclosure_topics scope 的
   `section_container`。无 contains/similarity、无模型、无 Document facet 直接传播，heading-only/
   空 Unit 不继承。它与 direct topic 分列、分过滤器；只有 direct topic 进入全文 key token
   channel，section route 由显式数组过滤参与 L2/L3 查询联合。

不存在通用的 `other_information` / “其他信息” direct route。源标题确实是“其他信息”“其他事项”时，
原文标题仍由 `title` / `heading_path` 保真；该 Unit 内若有回购账户、风险提示等具体事实，只落对应的
具体 route。没有可确认具体主题时 direct route 保持 NULL，绝不为了填满字段而发明“其他”占位键。

当前身份是 taxonomy `semantic-taxonomy-2026-08-r37`、router `semantic_router.v54`、prompt
`semantic_route_adjudication.v31`，当前候选 adjudicator 为 `codex_cli.v4.low` / `gpt-5.6-luna`；
候选与 direct route 都最多 8 个。model/effort 与 cache/receipt identity 绑定。定期报告正文/表格也
可以生成 Unit-local 直接主题候选；章节上下文另走 section_keys，不参与 shortlist。截断时，Unit
自身标题/正文/表格直接证据先于
纯字符相似或文档上下文召回，避免弱相似候选挤掉表单字段。候选若只出现在解释另一个主题的
原因、背景、影响或条件从句中，不成为独立 route；必须另行披露其自身余额、金额、比率、结果或
安排。唯一精确的定期报告标题仍直接成为唯一 route。事件公告
允许独立表单字段或正文直接事实产生 secondary；仅靠低阈值标题相似度、没有其他直接证据的
候选在任何 Unit 中都不得成为 secondary；真实 overview Unit 也必须有标题包含、正文或表格的
直接证据才能保留 secondary。taxonomy 只标识一跳的 overview container，不构建父子图、不传播父键：具体子标题
Unit 无论模型只选 overview，还是同时选 overview 与 direct route，程序都会在 receipt 冻结前去掉
overview；“重要内容提示”、
“主要内容”、概要/概况/报告书等真实 overview Unit 仍允许 container 与独立字段 route 同列。
Build 将候选定义、source IDs、裁决来源及模型/cache 身份冻结到
私有 receipt sidecar；Publish 重新 admission/Build 并只重放 receipt，绝不再次调用模型。词表、候选
定义、source 或上下文变化都会改变输入哈希，旧缓存/receipt 必须 fail closed。sidecar 原始字节的
SHA-256 由 private ProcessingRun 持有；每日 GC 仅在 snapshot owner 与该 hash 同时存在时保护固定
receipt sibling，失败/WIP 的无主 sidecar 仍可回收。

事件文类与 route scope 分层：年度/半年度/季度报告更正仍保持原报告文类；其他标题明确含
“更正公告/补充更正”的事件件才归 `correction_supplement`。`更正如下`/`更正为`、业绩预告中的
`净利润为…` 等格式信号只增加同 scope candidate 及 source witness，绝不绕过 Luna 直接落键。
回购“完成/结果”和未来“注销安排”是两个不同 route，避免用计划性文字污染完成结果检索。

Query hash 绑定真正独立的 secondary routes 与 section routes；singleton semantic 数组不重复
primary scalar 的哈希身份。检索投影索引 semantic/section 两个数组去重后的并集，规则版本为
`rp-2026.08-provider-unit-v3`。

不得递归扫描 payload、按字段名猜正文、按相同字符串去重、把 metadata title 注入每个 Unit，
或把 caption/页眉/粗体小计自动升格成标题。

## 3. 显式 search binding

每个 binding 同时保存：

- provider `source_index`、payload ordinal、field、item index 与 raw block hash；
- Unit destination：`unit_title`、top-level payload，或一个明确 mixed part；
- transform：`identity.v1` 或 `html_visible_text_segments.v1`。

读侧使用闭合 decoder，拒绝额外字段、未知版本、越界 part、source/destination field 漂移、重复
target 与错误 owner。`unit_title` 只进入 title 权重，不再次进入 body。相同文字的两个独立 source
occurrence 仍是两个 atom；跨 target/part 不拼成一个 substring atom。

## 4. 表格与视觉内容

表格 body 保存 MinerU owner 的原始 `table_body` HTML。检索时投影其可见文本片段；raw HTML
始终留在 Unit payload。L1 不重建 grid、不恢复 cell continuation，也不按词面猜表头。为避免
MinerU 3.4.4 的 `td`-only HTML 丢失监管表单语义，只接受两类机械闭合 role：严格两列多行
`label/value` 表的左列字段，以及矩形首行表头（或一个跨全列标题后的矩形第二行表头）。嵌套、
畸形、span 不闭合或可见文本不守恒时全部退化为普通 `table_text`；单行 `td` 表不做隐式
表头推断，只有 provider 已显式给出的 `th` / `thead` 语义原样保留；普通数据格永不升级。

MinerU merge-on 的跨页表只发布一个逻辑 owner body；continuation stub 没有搜索目标。每个物理页
segment/crop 仍以 page/bbox/hash-bound evidence 留在 locator/ProviderDocument。相邻相似表不会因
文本或 bbox 相似度合并。

无文字的 image/chart 仍通过 `content_artifacts` digest 参与 content hash，但没有 body atom。
supporting table crop 只作 evidence，不成为第二份可检索正文。

MinerU content-list 若只漏掉一个 text block 中的数字，Build admission 可读取**同一不可变 PDF、
同一 MinerU bbox**内的 native text。只有 block type=`text`、raw block/hash/page 精确绑定，且
MinerU 文本可由 native text 仅删除完整数字核心（可连同或保留 `%/‰`）得到，其余字符与已有数字
保持原序逐字相等，且只有数字 token 位置的相邻 ASCII 横向空格/Tab 可随 token 缺失或保留 MinerU 占位，才用
source-PDF 文字形成 Unit payload。PDFium bounded-text 明确生成的、非首尾孤立 `CRLF` 软换行会按文字边界移除（ASCII
单词间保留一个边界），CRLF 前后的水平空格仍须逐字匹配，且仅在同一观察中出现软换行时移除已校准的单个矩形末尾空格；宽窄字符、
标点、NUL、裸 `CR/LF`、空白行、其他空白或多个末尾空格差异均拒绝。ProviderDocument
仍原样保留 MinerU 输出。locator v2 记录 raw block、provider/source text hash 与固定
`source_pdf_native_numeric.v1` provenance，覆盖 Unit 自身 blocks 和 heading-chain 依赖，Publish 每次
从 PDF 重放。数字替换/重排、非数字差异、表格、高度重叠 bbox、旋转/页面形状不闭合、无 native
text 或 hash/page 漂移一律不修；native reader 固定 `pypdfium2==5.13.0`，不得据此引入第二套
阅读顺序、标题树或表格结构。

## 5. PostgreSQL 派生层

`BuildSearchProjection` 只选择 active provider-document runs；历史 v4 active row 不被重新解释。
投影按 processing run 原子替换：

- title / path / body / key 分别预分词并写入加权 `tsvector`；
- 每个非空 body target 另存一个 `unit_search_atom`，供同一 atom 内的 trigram substring 检索；
- PostgreSQL `tsvector` 不安全时按连续 token 半开区间建立 body windows，顺序拼接必须精确恢复
  parent body tokens；
- 未知 locator、错误 binding、不可安全切分的单 token 均 fail closed，并记录 terminal projection
  error，不发布静默缺词结果。

`header_row_candidate` 对 provider-native Unit 固定为 false。未来如需表头 role，必须有明确
source/provider 结构证据，不能恢复数值/词面启发式。

`content_categories` 是 Document 的 provider/classification 粗分类，与
`publisher_categories`、`market` 一样保持 Document-only。SQL/L2 如需 facet 粗筛，先查询
Document，再以 document_id 获取 Units；它不进入 Unit public view，也不加入全文 key tokens。

`GET /v1/semantic-routes` 直接从同一 taxonomy 公开 key、中文 labels、scopes、版本与
`usable_as_section_key`；后者同时覆盖定期报告 context-container 与事件 section-container，避免
L2 复制 L1 私有 JSON；它不创建第二套 registry。

## 6. 版本与验证

任何 tokenizer、Unicode normalization、binding/HTML transform 或 atom/window 行为变化都必须升
`RETRIEVAL_RULES_VERSION`。验证至少覆盖：

- accepted heading 不重复进 body，demoted title 仍按正文回放；
- equal-but-distinct source occurrence 保持两个 atom；
- table HTML visible text、caption/footnote 与跨页 stub owner 守恒；
- visual-only Unit 为零 body atom但 content hash 随 artifact digest 变化；
- malformed/unknown/cross-part locator fail closed；
- 标准定期/临时公告标题确定性命中；已知 scope 拒绝外族 key；低重叠词面不制造候选；
- 模型只能选择当前 Unit 的 candidate IDs 或 abstain，receipt/source/taxonomy 漂移 fail closed；
- active historical v4 run 不进入新投影候选；
- parent/window/atom 的 run-atomic replacement、orphan prune 与 PostgreSQL safety probe。

805-Unit 的完整 source-identity 数据质量结果见
[`semantic-route-pilot-20260813.md`](../checks/semantic-route-pilot-20260813.md)。覆盖率只作诊断；
unsupported narrow key 是 stop，而有证据缺口的 NULL 是允许的保守结果。

公开 `document_unit`、search projection view 与 source/evidence 引用仍保持现有 v1 列面；新旧 Unit
通过 locator contract 区分，绝不按文件存在性、parser 版本或字符串形状猜代际。
