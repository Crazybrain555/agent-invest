# Conservative Content Publisher v1

状态：2026-08-07 经用户调整产品目标并由 Pro、独立只读 reviewer 复核后，替代旧的
OFCP exact-occurrence / FrozenOutline / exact-interval 生产方案。目标是稳定发布可供 L2
使用的可靠内容容器，而不是恢复 PDF 的最细结构。

## 1. 产品取舍

按以下顺序验收，前一项不能为了后一项让步：

1. 内容不丢、不重复，payload 可回放；
2. 每个内容 leaf 只有一个可靠粗 owner，页序和可用阅读顺序正确；
3. 大标题和文档框架稳定；
4. 细标题尽力恢复，但没有把握就并入最近可靠的已发布容器或 `document_root`。

95% 以上的稳定结构质量是当前目标。细标题漏召回、跨页表未自动合成和最细引用粒度下降
是明确接受的代价；错误标题、错误归属和内容损坏不是。

## 2. 唯一生产路径

```text
hash-bound PDF + parser artifacts
        ↓
NormalizedIR + source evidence
        ↓
page-local carriers (text / table fragment / media)
        ↓
conservative heading admission
        ↓
single-pass owner assignment (root / heading section)
        ↓
mixed unit materializer
        ↓
owner-level search projection + PublicationGateV1
```

生产继续使用现有 `canonical_occurrence.py`、`source_evidence_occurrence.py` 和
`document_unit_audit.py`。新 gate 的诚实能力声明是
`source-evidence-bounded content conservation`，不是“枚举了原 PDF 的所有 paint
occurrence”。Raw PDF、parser artifact、run/parser identity 和 source selectors 仍提供
可追溯性。

不再引入第二套 AtomicSourceIdentity、FrozenOutline、OccurrenceLedger、PlacementLedger、
SearchOwner graph 或 runtime epoch authority。旧实验链没有接到当前 placement 链，不能进入
生产，也不再作为 release gate。

## 3. 标题准入与上收

候选状态只有三种：

- `accepted`：取得发布资格并成为新的 section owner；
- `suppressed`：明确不是标题，原内容仍作为普通 leaf 发布；
- `flattened`：可能是标题但证据不足，不发布 heading，内容归最近可靠父节/root。

标题准入只使用与文本主题无关的结构证据。普通编号标题采用一个刻意较小的判据：MinerU typed
title 主张、source selector 对原生文字的精确闭合、完整且连续的单/多行组件、文档自适应左边界、
通用编号文法，以及 page-local Table/TD/TH/TOC 否决。不得再叠加全局字号倍率、页内间距异常、
公司/短语字典或 sibling recovery。bookmark 与 StructTree 若来自同一 authoring artifact，只算
相关 authored observations，不能机械当成两个独立视觉见证。

多行文档标题不使用一个跨文档固定字号倍率。已获准入的页首 title anchor 可以吸收相邻 paragraph
carrier，但后者的 provider 类型不提供标题票；全部 carrier 必须由原生 glyph 精确重放，并共同形成
页 1 上居中、display-size、连续且高度相容的组件。这样覆盖 provider 只标首行的小字号分行标题，
又不会把日期、编号或正文仅因相邻而并入。无法闭合则不合并。

section 只由可信 heading start 建立；结束位置由下一个可信 start 自然产生。不再消费
`accepted_content_interval`，也不要求 1,504 个历史 interval 的 exact endpoint。

当前 simple95 cutover 发布可信标题节点、物理顺序和 section start，但不宣称所有祖先边均已恢复。
`heading_path` 原样保留 producer 已发布的路径（包括有 source proof 的多段路径），不得为了能力声明
截短；document/run 级 `hierarchy_status=flattened_unresolved` 明确表示这些路径不能被理解为全文件
exact tree。层级边缺失是 P2 召回/上下文能力缺口，不得用未经证明的编号栈、provider level 或旧
final units 补造。它不改变 payload 的唯一 owner、source order 或内容守恒。只有未来独立的完整
层级证明门通过后，新 run 才可声明 `exact_proven`。

## 4. Owner 与内容守恒

Durable owner 只有：

- `document_root`；
- `heading_section`。

`page_table_fragment`、cell、image、caption 和 native residual 都只是 owner 内的 leaf，不是
durable unit 边界。每个 payload-bearing source selector 必须恰好进入一个 active owner；
不确定标题不能阻塞其内容，只能使内容上收。

同一 owner 下连续的 text/table/media 按稳定 source order 组成一个 `mixed` unit；当前不把该
序列冒充为精确 canonical reading order。所有新 mixed payload 显式声明
`order_status=unresolved_physical_fallback`，各 part 保留自己的 page/bbox/source order，L2 不得
跨 part 强拼句子。可证明的 exact reading-order contract 留待后续独立能力升级。
根内容合法使用 `heading_path=[]`、`heading_path_text=""`，不得伪造标题。

source-native residual 的规则：

- 有确定 containment/相邻 owner 时，作为该 owner 内的 text/image leaf 保留；
- 位于表格 carrier 内时与该 page-local table fragment 同属一个 mixed target；
- residual 不得另建独立 document unit；
- 同一 source slice 的 table HTML/rows/cells/flat text/native residual 至多一个 active primary
  search projection。仅能证明同一 coarse owner 的严格表面重复时，residual 仍保留为带
  page/bbox/source ref 的 `unresolved_source_alternative`，但 `search_policy=none`；它不是
  occurrence alias，也不被删除。非严格相同 residual 继续作为同 owner 的 primary leaf。

## 5. 表格、glyph 与附件

表格按物理页保留 fragment、HTML/grid/payload、caption/footnote、page/bbox 和 artifact
provenance，但页边界不创建 unit。当前不做跨页 logical-table LINK、cell continuation 或
same-cell 推断；L2 命中 owner 后取得这些有序 fragment。

未解码 glyph 的 raw code/CID/GID/font/bbox/raster provenance 可以保留；PUA 字符和
`⟦未解码字形…⟧` 只能进入 display/diagnostic，不能写入 canonical semantic text 或 search。
安全周边文本仍可检索，关键值无法安全表达时只阻塞相应 leaf。

PDF embedded file、file attachment、associated file 和 page media 属于不同 asset domain。
只有页面真实可见的附件 annotation/reference 进入正文 leaf；附件 blob 自身不伪装成正文 unit。

## 6. PublicationGateV1

文档级硬门只检查：

1. PDF、NIR、source-evidence、processing run、parser/artifact hash 一致；
2. source item/selector 唯一且顺序闭合；
3. substantive source coverage gap 为零；
4. 每个 payload-bearing selector 恰好一个 durable owner；
5. duplicate active primary payload/search projection 为零；
6. payload 字段和 source projection 可重放；
7. canonical page order 无 hard cycle/violation；
8. unresolved glyph/provider hallucination 不进入 canonical text/search；
9. 每个 visual occurrence 有 display leaf 或明确 non-reader/advisory/rejected terminal。

以下情况只 suppress/flatten heading，不阻塞文档：provider-only title、visual-only fine heading、
父链/层级不确定、细 interval endpoint 不确定、跨页 table continuation 不确定。当前未发布的祖先
层级边也不得被 review UI 画成已证明的树。

只有内容缺失或重复、competing coarse owner、payload 无法重放、hard order 矛盾或唯一视觉内容
无 source artifact 时，才阻塞文档或相应 leaf。

## 7. 已知样本的统一结果

- 财通：`票?` 和 Q1–Q6 不满足标题准入，作为四页表单内容留在根/主表 mixed owner；
- 能辉、明阳：第一页两行 root title 由页首 display-title 样式簇合并；
- 江海：页首页题保持完整；少量表题/附注细标题允许在 accepted 与 flattened 之间保守变化，内容
  始终留在最近可靠的已发布容器；
- 三孚：责任声明不再误升；表题、报表名和附注细标题可上收，媒体各保留一次 display leaf；
- 中科飞测：provider 将根标题装进 `table_caption` 时不破例升 heading；文档 metadata title 与根内容
  仍完整发布，caption 不反向取得 section authority；
- 所有文档：表格 fragment、正文、媒体和 residual 不能因标题降级而丢失或另起噪声 unit。

这些规则不得按公司、文档 ID、`票`、问句或公告短语分支。

## 8. Shadow QA 与后续工作

Terra/Codex 低成本多模态只作为 shadow QA/proposer：输入页图、native blocks、provider candidates
和 deterministic accepted headings；输出 typed `HeadingProposal`、`NotHeadingProposal`、
`BoundaryRisk` 或 `FlattenSuggestion`。模型没有 publication authority；与 deterministic engine
不一致时只允许 flatten/upward 或告警，不能新增标题。固定模型/request identity，并以重复 run
一致性评估成本和收益。

PDFium visual heading、exact Frozen interval、cross-extractor alias、跨页 logical table 和 cell
continuation 全部延期，不再阻塞当前 cutover。

## 9. 最小验收

- unit/mutation：标题 accept/suppress/flatten、父级上收、root 合并、residual 同 owner、search
  primary 唯一、glyph leak guard；
- 标题：财通、能辉、明阳与 all15 title sequence；江海/三孚允许预先声明的保守差异；review
  必须把 sequence 与尚未证明的 hierarchy 分开显示；
- 内容：all15 source replay 的 coverage/duplicate/owner/order/publication gate；
- 表格：逐页 payload、caption/footnote、顺序和 provenance，不验 cell continuation；
- fresh：Windows GPU 同构解析，run-bound identity，主 Agent 重新肉眼查看；
- review：未参与实现的 reviewer 检查 diff、真实样本与相邻负例。

生产 DB、AgentSSD、worker、清库、重切、commit 或 push 仍需各自授权和 gate。

## 10. 95% 计分与停止规则

内容、粗 owner、顺序、provenance、payload replay 和 active search 去重是 100% 硬门，不参与标题
平均分抵消。标题只作为独立软门：相对已肉眼接受的 run-bound corpus，node-occurrence precision
至少 97%、F1 至少 95%，并且财通式跨页残句、表内问答、元数据升 heading 等严重 false positive
为零；文档根与主要章节框架另做肉眼检查。该分数不包含 hierarchy edge；未发布的祖先边必须
单独登记，不能用高 node 分数掩盖。

满足硬门和上述标题门后，孤立的细标题漏召回或上收登记为 P2，不再通过阈值、字典或单文档 patch
继续优化。2026-08-07 fresh Windows r3 + current-code r6 共发布 1,474 个标题节点。与用户已查看的
v27 序列机械比较为 precision 96.68%、recall 94.75%、F1 95.70%；该比较只能发现变化，不能把旧
run 当真值。主 Agent 随后逐页查看 fresh layout/PDF 并逐条读完江海 517、三孚 869 个当前标题，
将 49 个新节点全部确认成真实印刷标题或表单小节，没有严重 false positive。把 v27 已接受节点与
这些 fresh 新节点组成保守视觉参考集后，本 run 为 TP=1,474、FP=0、FN=79，node precision 100%、
recall 94.91%、F1 97.39%。FN 主要是江海、三孚和衢州的报表名/附注细标题以及中科飞测未取得
heading authority 的根标题；内容与 metadata label 仍保留。完整祖先层级仍为
`flattened_unresolved`，不纳入上述 node 分数，也不宣称已解决。

Terra + Low 只在上述 stop rule 之后处理 suspicion pages：它可以建议 flatten 或人工复核，不能新增
heading、改变 owner 或覆盖 deterministic hard gate。只有跨 authoring family 重复出现并影响 L2 的
边缘族，才提交 Pro 重新做顶层设计。
