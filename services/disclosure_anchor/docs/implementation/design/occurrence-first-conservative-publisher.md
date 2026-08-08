# Occurrence-First Conservative Publisher (OFCP) v1 — superseded research draft

> **Do not implement this document.** The user changed the release target on
> 2026-08-07. Exact PDF occurrence/interval closure, FrozenOutline, PDFium and
> cross-page table proof are no longer production gates. The active design is
> [Conservative Content Publisher v1](conservative-content-publisher.md).

状态：已被上面的简化方案替代，仅保留为研究过程记录。本设计曾从
`effe4b7` 建立新的实现分支；该提交只是可识别的干净仓库锚点，**不是** v27
源码快照。v27 只作为 run/hash-bound 行为验收基线。

## 1. 要解决的根因

现有流水线把 provider block、原生 PDF occurrence、标题边界、unit owner 和搜索投影
过早折叠。这样会产生三类系统性错误：

- provider 生成或误识别的文本取得 reader-visible 发布资格；
- 跨页表格的页级 carrier 被误当成 durable unit 边界，或同一 source slice 以
  HTML/row/cell/flat text/residual 多次进入搜索；
- 标题、内容边界和最终 builder 输出互相反推，导致无法区分上游证据与下游结果。

OFCP 把这些身份分开，并采用“细结构不能证明就上收到可靠父容器”的产品原则。
错误细标题比诚实的粗容器更不可接受。

## 2. 不可混用的四层身份

1. **Observation**：MinerU/VLM/native extractor 等给出的观察。Observation 本身没有
   reader-visible 资格。
2. **Immutable occurrence**：原 PDF 中可见或有明确 source identity 的内容发生。
   reader-visible occurrence 必须有唯一 terminal state。
3. **PlacementOwner**：物理归属，可以是 `document_root`、`heading_section`、
   `page_table_fragment`、`page_region` 或 `visible_asset_annotation`。
4. **SearchOwner / UnitDraft**：durable L1 检索对象。Phase 3 中只属于 frozen
   `heading_section` 或 `document_root`，不属于页级 table fragment。

`page_table_fragment` 只是一张页面内的物理 PlacementOwner。跨页表格的每页 fragment
在同一个可靠 section/root mixed target 中保持独立 leaf 和 provenance；页边界不能自动
创建 unit。Phase 5 接受的 `same_logical_table` LINK 只增加可逆逻辑关系和检索聚合，
不改变物理 owner，也不推出 `same_cell`、`same_occurrence` 或 L1 unit 边界。

### 2.1 Source identity 与 provider identity 必须分离

`source_item_sha256` 是整个 MinerU item 的 hash；HTML、provider type 或描述改变都会使其
改变，所以它和 selector 只能进入 `ProviderObservation` / `LegacyImportMap`，不得决定
durable source occurrence、ContentLeaf、owner 或 search identity。Phase 2 核心是单一用途的
`AtomicSourceIdentityLedger`：

- 一个可由 source PDF + 固定 Poppler epoch 重放的 Poppler `<word>` **几何节点**，对应一个
  atomic source identity；它是否成功解码为 Unicode 不决定 occurrence 是否存在。decoded
  value 是后续 observation，不回填 identity；
- durable ID 只由 source PDF、extractor epoch、page-box fingerprint、完整
  `flow/block/line/word` XML node path 和整数化 bbox 构成；native/decoded text、text hash、
  char span、provider/NIR 字段和任何 ordinal 全部排除；
- 当前 v2 epoch 绑定 `pdftotext`、`pdfinfo` actual executable bytes 的 SHA-256、各自版本与
  executable-snapshot hash；抽取前后重新解析并散列 actual executable，任何变化直接失败。
  这只能标记为 `runtime_attestation_strength=executable_snapshot_only`、
  `source_replay_authority=shadow_only`，不能作为 Phase 3A production authority；
- production v3 必须在 build time 闭合并散列两个工具的全部非系统传递 dylib、selected arch、
  macOS build、固定 loader/locale/fontconfig 环境、poppler-data 与允许字体资源；batch 前后复验
  同一静态 manifest。绝对安装 prefix 只作 locator/audit，不进入 durable root；
- `pdftotext -bbox-layout` 的完整 geometry-word 直接遍历 ordinal 只进入独立
  `PopplerLayoutEnumerationReceipt`。它不是 content-stream/paint/physical/reading order，
  也不能作为 path+bbox 碰撞时的补丁；碰撞必须 fail closed 并提升 locator epoch；
- provider 把多个 atoms 合成的 block/cell/title 只产生后续 `SourceSpanBinding`，不能通过
  隐藏在 membership 中的 provider segmentation 再铸 occurrence；
- 当前 MinerU/NIR bbox 只能成为 `RegionObservation` 或 `LegacyRegionImportLocator`，不能
  因 role-neutral 命名而升级为 `SourceCarrier`。durable carrier 以后只能来自 native
  topology 或独立证明的 exact atomic membership set；
- provider field、HTML、region、媒体描述、table fragment 和 LegacyImportMap 都是独立
  hash-bound artifacts，不得 import 回 atomic core。

v27 all15 在当前 Poppler epoch 下从原 PDF 双重重放得到 44,194 个 geometry-word
occurrences，locator
碰撞为零。原先另计的 7 个 image/chart crop 由 MinerU item bbox 触发，没有 PDF
object/paint event、MCID/StructTree 或独立 segmentation polygon，因此只属于
`ProviderBoundVisualCropObservation`，不进入 source occurrence 分母、ContentLeaf、owner
或搜索。坐标按 ties-to-even 量化成固定 page-box u1e6 整数；不能直接 hash 二进制浮点。
以后取得 raw-PDF locator 时必须开启新 identity epoch 和显式迁移，不能静默重定义旧 ID。

Transport validator 只证明 closed schema、record ID、唯一性、page/document roots 和本地
hash 自洽；它不叫 source proof。v2 的不可注入
`replay_atomic_source_identity_by_pdf` 会重新打开 hash-bound PDF 并完整枚举，但只返回
`ShadowReplayedAtomicSourceIdentity`。持久 `SourceIdentityReplayAuditClaim` 必须明确
`runtime_attestation_strength=executable_snapshot_only`、`source_replay_authority=shadow_only`、
`production_admission=false`、`authority_restorable=false` 与 `capability_serialized=false`；
即使字段和自哈希完全自洽，也不能恢复 production admission。

Corpus 层严格区分：

- 纯 `AtomicIdentitySet` 只含 PDF hash、epoch hash、document atomic root 和 occurrence
  count；不含 provider ID、路径、v27、enumeration 或 replay claim，重复 PDF/root 拒绝；
- `V27AtomicIdentityEvidenceBundle` 才携带 provider/path/v27 trust、完整 ledger、enumeration
  和 audit claim。其 transport hash 只证明运输完整性；逐 PDF 现场 v2 replay 只返回
  `ShadowReplayedAtomicIdentitySet`，可供 observation/binding/materializer shadow 使用，不能供
  production publication。v3 L2 runtime manifest replay 才允许定义真正的 admitted capability。

必须保留“篡改 locator 后重算全部本地 hash，transport 可过但 source replay 必须拒绝”、
“跨页交换 enumeration occurrence ID 并重算 hash 仍因 page 外键拒绝”、以及“伪造 audit
claim 永远不能取得 capability”的变异门。

## 3. v27 行为基线，不是源码基线

`V27BehaviorBaselineManifest` 必须把可用于结构锚定的事实与只能用于行为比较的结果
物理分栏：

### 3.1 Source identity（可作为 importer 输入）

- source PDF SHA-256、页数和文件大小；
- processing run id；
- parser、remote runtime/model/request identity 及其 canonical hash；
- NormalizedIR 相对路径、文件 hash、carrier-set hash；
- parser artifact 的 role、相对路径、hash 和大小；
- acceptance ledger、batch receipt、docs manifest 的文件 hash。

### 3.2 Behavior oracle（只允许比较）

- canonical outline version/hash/count；
- ordered heading-path hash；
- unit snapshot、content、structure 和 query/search projection hashes；
- canonical content-stream/ownership receipt；
- 主 Agent 实际查看的 PDF 页与标题树验收记录。

final units、builder/coalescer 输出、heading path 和 search hash **不得**传入
FrozenOutline 边界生成 API。代码接口和 schema 要让这种依赖不可表达。

## 4. FrozenOutline 的唯一合法来源

Phase 3 的标题约束来自 `frozen-outline.v1`，其 source side 只接受同时满足以下条件的
v27 NIR：

- NIR 文件 hash 与 baseline 完全相等；
- source PDF hash、processing run id、parser identity 与 baseline 完全相等；
- structure proof 固定为 `document_structure.v1` /
  `document-structure-evidence.v27`；
- carrier-set hash 能由 ordered `(source_item_index, source_item_sha256)` 重算；
- published outline 能由 proof source refs 重算并与 baseline outline hash 完全相等；
- 每个 heading selector、text span 和 section-span 端点都精确落到 hash-bound NIR
  carrier；禁止文本相似度、近似 bbox 或 fuzzy index。

每个 frozen node 保存：proof node identity、父节点、depth、精确 title parts、
`exact_concat_then_outer_strip` display transform，以及有序 composite heading anchors。
每个 line anchor 必须是 `native_exact` 或 hash-bound `visual_bound` typed union，不能因
缺 native atom 而伪造一个。v27 `section_span` 只转换成 immutable provider-item 序列上的
半开区间 `[before(start_item), before(end_item + 1))`（末项为 `document_end`）；它只证明
item membership，不声称 field/atom 级 reading order 或全部 reader-visible occurrence
已经闭包。postposed table caption 允许 heading anchor 位于区间 start 之前，必须保留
`boundary_position=after_source_payload` 的 typed 事实。

Phase 2 的 atomic identity ledger 与独立 enumeration receipt 建成后，还必须产生 exact
binding receipt，并对
item 区间内外的 native-only occurrences 单独完成 coarse placement。任何 source anchor 无
唯一 occurrence、item-sequence 邻接/interval 端点不一致或 ledger hash 不匹配时，文档
保持 blocked；不得调用 legacy fallback。FrozenOutline 不能提前冒充 content-closure
receipt。每个 run 的 outline authority 只能是 frozen 或未来的 new outline engine 之一。

Frozen importer 的允许链只能是 hash-bound v27 selector → exact provider observation →
exact source locator/span → source representation/occurrence。legacy occurrence ID 只进入
`LegacyImportMap`。任何参与发布的 heading/boundary/interval 无法 exact-one 映射时，整份
文档的 Phase 3A admission blocked；禁止文本相等、最近 bbox、IoU、source item index 单独
匹配、final unit 或 root 自动兜底。

### 4.1 仅限五个 visual/media 例外的 PDFium source lane

all15 的 1,517 个 heading parts 中，1,515 个 native parts 可映射为 2,230 个 Poppler atomic
occurrences；当前全部 1,504 个 content intervals 仍明确为
`occurrence_closure_proved=false`，不能把 provider item membership 冒充 exact interval。
剩余例外只涉及江海 p141 的两个 printed headings，以及三孚 p133/p134 的三个 IMAGE
interval ends。Phase 3A 对它们采用 page-scoped lane，不建设第二套全文件语义抽取器：

1. `PdfiumPageObjectInventory` 从 hash-bound PDF 与 pinned PDFium epoch 完整递归枚举相关页。
   epoch 必须绑定实际 PDFium shared library、bindings 与 extractor source digest。完整
   `pdfium_object_node_path + object_type` 构成 epoch-bound locator；flat ordinal、文本、bbox、
   matrix、role、owner 均不进入 object ID。
2. matrix/quad/bbox 是可重放 fact，进入 record/root；object path 不是 paint、reading 或 source
   order。TEXT/IMAGE frontier 分类型闭包；相关 FORM 子树、path、matrix 或 geometry 失败时仅该
   capability blocked，不能用 nearest/IoU/text similarity 兜底。
3. `HumanAcceptedVisualBoundaryReceipt` 必须绑定整页 inventory root、source raster 与 renderer
   epoch、确定性的完整 candidate set、人工 polygon、selected object subset，以及前后
   `preceding/following_source_anchor_id` boundary bracket。provider bbox 只能是
   `advisory_only` proposal，不能定义候选域或自证唯一。
4. 通过 receipt 后才从 page-object representations 生成 `PaintOccurrenceAdmission`。江海
   两个标题分别由 objects `75/76/77`、`84/85/86` 组成 printed-heading occurrence；PDFium
   raw text 只是 value alternative，frozen artifact 继续授权 display title。
5. 三孚 IMAGE 是读者可见 `page_media` occurrence，形成同一 frozen section/root owner 下
   `display_only / search=none / semantic_text=null` 的 ContentLeaf；它不是 page-region owner，
   也不能只当 boundary witness 后丢弃。多个 boundary 可引用同一媒体 occurrence，但只发布
   一个 leaf。MinerU Mermaid 仍终止为 `rejected_observation`。
6. Poppler 与 PDFium 可以有两套 representation identity，但只能有一套 reader-visible
   occurrence obligation。`same_visible_occurrence` 与 `semantic_value_alias` 必须分开；仅 bbox
   containment、文本近似或标点相差不足以合并 coverage。未闭合时相关文档 blocked。

这条 lane 的权限固定为 `frozen-outline-v27-import-only`，不得外推成通用标题恢复器。进入
Phase 3A 还需依次通过 atomic replay、PDFium epoch、page frontier、human receipt、paint
occurrence、cross-extractor coverage、boundary bracket 与 media-leaf projection 八道门。

## 5. 内容闭包与 provider terminal

零丢失/零重复只约束原 PDF 的 reader-visible/source occurrences，不约束 provider
凭空生成的内容。provider observation 必须终止在以下一种状态：

- `reader_visible_source`：已精确绑定原 PDF occurrence；
- `alias_or_support`：同一 source slice 的已证明别名/支撑表示；
- `rejected_observation`；
- `non_reader_visible`；
- `advisory_only`。

后三者保存 artifact、runtime 和 provenance，但没有 reader-visible occurrence、
PlacementOwner、payload leaf 或 search atom。它们不能为了“provider 内容守恒”被塞进
document root。

Provider projection terminal 必须绑定产生该投影的完整 artifact 集合。v27 的 canonical
field projection同时依赖 `content_list` 与 `content_list_v2`，不得用一个含糊的单一
`artifact_sha256` 冒充完整 provenance。

Table `embedded_media` 只有 provider JPG、row/col/span 和描述，而没有逐图 PDF object、
page polygon/crop exact-one 证明时，只能作为绑定整表 carrier 的
`ProviderMediaObservation(advisory_only)`：逐项闭合、零 ContentLeaf、零搜索、零 semantic
authority，且不能计入 reader-visible occurrence closure。原 PDF 完整视觉内容由整表/page
source refs 保全。只有取得逐图 paint event/object 或独立 page-bound region receipt 后，
才可升级为 display-only media occurrence；provider 描述仍不能自动取得 canonical 权威。

同一个 source slice 只能有一个 active primary 搜索表示。HTML、rows、cells、flattened
text 和 native residual 不得多视图重复索引。一个 section/root SearchOwner 可以有多个
text/table/image/conflict leaves；它不是一条拼接长字符串。

## 6. Phase 1 public/asset/glyph 合同

- 文档根内容的 `heading_path=[]` 是合法结构；public `heading_path_text` 统一投影为
  空字符串而非伪造标题。
- PDF `embedded_file`、`file_attachment`、`associated_file`、`page_media` 是不同
  asset domain。只有页面真实可见的 attachment annotation/reference 才在正文中产生
  `asset_ref` leaf；嵌入文件本身属于 disclosure_anchor 内部的 PDF source-object/blob
  ledger，可另起子文档 run。这个 `DocumentAsset` **不是** v0.8 `data_asset` 的第五种
  `asset_kind`；子文档被实际解析后才按内容进入既有四种合法 L1 信封之一。
- 未解码 glyph 的权威值是 typed `GlyphToken`：raw code/CID/GID/font object/bbox/
  raster provenance 保留，`semantic_unicode=null`。占位符只属于 display projection，
  不写入 canonical semantic text，也不进入搜索；无法安全保留周边语义时阻塞对应 leaf。
- unresolved native/provider 值位于同一可靠粗 owner 时，以 structured alternatives
  发布；不选 winner、不拼接。只有 competing owner、value replay 失败、hard order
  矛盾或必须破坏性选择时才 blocked。

## 7. 阶段与切换门

1. Phase 0：行为 baseline、FrozenOutline source contract 和 hash 重放。
2. Phase 1：public root-path、asset domain、glyph/provider leak guard。
3. Phase 2：atomic source identity + PDF/Poppler replay；value、order、provider、region、
   table 和 legacy migration 分别形成单向依赖的独立 capability，不回填核心。
4. Phase 3A：frozen outline exact binding、page-local placement、section/root SearchOwner、
   pure materializer；先 shadow，之后仅 allowlist 原子切换。
5. Phase 3B：fresh blind owner shadow validation，不改变 outline authority。
6. Phase 4：新标题引擎；一般生产 cutover 需至少 30 issuers 和 8 份 Windows blind
   fresh，并按 authoring family 分层验收。
7. Phase 5：可逆跨页 table LINK 与 search aggregation；不做 cell merge。
8. Phase 6：可选 cell continuation 或额外视觉 proposer。

Phase 3 release gate 至少要求：reader-visible coverage gap=0、competing coarse owner=0、
duplicate active search target=0、hard order cycle/violation=0。允许合法 coarse container
中的 `unresolved_physical_fallback`，但 receipt 必须分别报告 exact/coarse/blocked，且
不得把 fallback 宣称为 canonical reading order。

物理序列化与 reading order 是两种合同。Phase 3A 可使用统一 PDF page 坐标的
`serialization_order_key` 稳定保留 leaves；NIR/provider order 不得作为主序。没有 region/
column/reading-before proof 时，target 内和 target 间状态均为
`unresolved_physical_fallback`，所有目标保守发布为 coarse，且不得由该序推导句子、row、
cell continuation 或 canonical document order。

## 8. 明确拒绝

- 从 `effe4b7` 或 dirty diff 声称重现 v27 源码；
- 从 final units、unit 数、heading paths 或 search winner 反推标题边界；
- provider/native/多次 VLM 输出盲目多数投票；
- page-table fragment 自动变成 durable unit/SearchOwner；
- 按文本关键词、公司、文档 ID 或全局常数修单例；
- 未知 glyph 的 PUA/raw code 或 display placeholder 进入 semantic search；
- 同一 run 内 frozen/new/legacy authority fallback；
- 证据不足时继续发布细标题。此时必须上收到可靠父节/root，或 blocked。

## 9. 验收

Phase 0/1 先以 synthetic mutation 加 all15 hash-bound replay验证：文件/hash/run/parser
篡改、source ref/span/carrier/parent/interval 篡改、final-unit 诱导、provider hallucination、
glyph placeholder 泄漏和 asset-domain 混淆均须 fail closed。之后才进入 immutable ledger
与 materializer。任何 production DB/AgentSSD/worker 操作、提交或推送都不属于当前阶段。
