---
id: disclosure_anchor_design_heading_level_arbitration
project: disclosure_anchor
title: 标题与章节结构：source-bound typed proof
status: approved
created_at: 2026-07-17
revised_at: 2026-07-27
depends_on: NormalizedIR v4、document-structure.v1、source-evidence-conservation.v8
---

# 标题与章节结构：source-bound typed proof

## 1. 目标与边界

L1 的任务不是“猜一个看起来合理的大纲”，而是把 PDF 中可验证的结构 occurrence 绑定到
可检索证据。一个标题只有同时具备来源身份、页面位置和明确结构角色，才可进入
`heading_path`；无法证明的文本仍须保留和可检索，但不能伪造标题、层级或父子关系。

以下信息彼此正交：

- heading/title/path：只来自本文件规定的结构 proof；
- caption、unit、footnote、applicability：是 payload annotation；
- 监管 taxonomy / semantic keys：只用于 L2/L3 检索路由；
- source fallback / visual guard：用于补足原生文本或几何证据。

后三类都不得反向改变 PDF 边界、标题或 ancestry。

## 2. 已否决的旧实现

以下机制已从当前热路径删除：

- `text_level >= 1` 直接把 MinerU legacy text carrier 升成 heading；
- 用固定栏目名、财报业务短语、caption 或“单位：…”决定标题；
- 用编号/标点正则、最近标题栈或文档级词面白名单推断父子关系；
- 从目录文本匹配回写正文结构；
- 证据冲突时强制改成 level 1；
- 把碎片保留下来后交给 L2 猜测拼接。

旧语料研究仍可从 Git 历史追溯，但不再是现行设计或 agent 指令。

## 3. 可接受的结构证据

`pdf_native_structure.py` 从原始 PDF 读取：

1. Tagged PDF 的 `StructTreeRoot`、标准 role（`H1`–`H6`、`TOC/TOCI`、
   `Table/TH/TD` 等）与 ancestry；
2. page-local marked content / MCID、文本与 bbox；
3. PDF bookmark 的 title、level、destination page/y。

`structure_proof.py` 另可读取 MinerU v2 明确标成 `type=title` 且携带 level/bbox 的 block。
它只能对齐正文 text carrier；table/image caption、note、footnote、unit 或 HTML 字段都不是
heading carrier。MinerU v2 与 legacy content list 是同一个 provider block 的两种序列化：
v2 text span 保留原字符，legacy text span 按 MinerU 3.4 官方规则转义 `* _ ` ~ $`，
inline equation 由当前冻结 profile 包成 `$...$`。对齐必须先把 v2 精确投影成 legacy 表示，
再要求同物理页、等价 bbox 和唯一 legacy text carrier；仅 bbox 相交、任意同 bbox
`text_level` 或模糊反转义均不是证明。MinerU legacy `content_list.text_level` 只保留为
provider annotation 和冲突诊断，不能单独产生 heading。唯一闭合的 MinerU v2 typed title
可以按其显式 level/顺序建立 provider section；与 native 非 heading role、重复 occurrence
或另一张结构图冲突时 fail closed，原 carrier 仍保留。

所有候选必须按同一 source PDF hash、物理 page、bbox/MCID 与具体 MinerU carrier field
唯一对齐。全局文本相等、相邻位置、业务语义或多个同源 JSON 的“一致投票”都不构成证明。

## 4. 构造规则

1. StructTree、bookmark、MinerU v2 分别在自己的图内建立 exact heading occurrence 和
   parent edge；不同来源的 raw `H3/L2/L1` 数字不直接比较。
2. 同一 source refs/text span 的正面 role evidence 合并为一个 anchor；native
   `P/TOCI/TD/Table` 是 role/containment evidence，不是删除 anchor 的全局否决票。
3. root-reachable 且 parent 一致的 native H、有效 destination bookmark 可提供父边；
   source-bound MinerU v2 title 也可在自身 typed title 序列中按显式 level 提供父边。
   父边一致或只有一张无冲突的有效结构图时采用；父节点真正冲突、逆序或跨过独立
   reading-order branch 时切断。
4. 最终 `heading_level` 是已接受 parent DAG 的 canonical depth，而不是复制任一来源的
   local level。来源 level 差异保留为诊断，不能抹掉 anchor。
5. 父图或 TOC/table containment 无法证明向后传播时，anchor 的 `section_span` 收缩到自身；
   节点显式写 `propagates=false`。它仍可检索，但不控制后续 unit 的 title/path，也不能截断
   其他已证明 section 的 span。其他 section span 才按 DAG 与 source order 计算。
6. builder 只能消费该 proof，不能再建立第二棵结构树。表格 title 取最深 resolved section；
   caption 永不反向补成结构标题。

结果允许更粗，但不允许错误。无法唯一绑定的候选仍以普通 source carrier 保留；其 conflict
relation、source item、native node/bookmark order 必须进入审计分布。

## 5. 页框与页码

页眉、页脚、页码只有在来源 role 与物理版面共同证明时才可外部化：

- PDF native `Artifact/Header/Footer`；
- MinerU typed `header/footer` 在 top/bottom band 的连续重复 occurrence；
- 仅有 MinerU v2 title 的 occurrence，在至少三个连续物理页以相同文本、相同 top/bottom
  band 和既有 bbox 等价容差重复时；同一 occurrence 具有有效 StructTree H 或 bookmark
  证据时永不按此规则外部化；
- MinerU typed `page_number` 在 top/bottom band 的连续递增打印序列。

页码正则只解析 provider 已标成 `page_number` 的短数字格式，并且必须通过物理 band 与
跨页单调序列；它不识别标题，也不影响业务 section。上述 title 复现判定只使用来源角色、
连续页和版面位置，不使用公司名、栏目名或其他词表。页框 occurrence 仍保留为
document-level evidence，并阻断跨页文本聚合，避免正文被静默吞掉。

## 6. 证据缺口

“无法对齐”是需要观测和追根的状态，不是完成状态：

- MinerU 漏文本：由独立 Poppler native occurrence ledger 记录并生成可搜索的整页
  fallback unit；
- 原生文本缺失：生成 hash-bound lossless full-page PNG；
- 原生 word geometry 异常：保留有效 native text，同时绑定整页 visual guard；
- PDF 有 typed heading 但 carrier 无法唯一对齐：记录结构 conflict，保留原 carrier，
  不伪造 path；
- exact text/bbox 已闭合但两种 extractor 阅读顺序不同：保留双侧 order 与 conflict 诊断，
  不伪装成内容缺失，也不生成重复整页 fallback；
- source evidence、fallback unit、visual descriptor 或实际 PNG bytes 任一不闭合：
  BuildUnits fail loud，不发布该 run。

`audit_unit_corpus.py --source-replay` 必须从 raw PDF、原 MinerU artifact 重建 v4 IR，
把 fallback units 在最终 audit 前合流，并汇总所有 structure conflicts、fallback reasons、
visual pages 和 native geometry issues。删除/重解析许可要求“零未知、零未分类”；诊断不必
机械归零，但每个 material family 必须有真实样本、根因和明确处置。

## 7. 与检索的关系

builder 发布两个层次：

- 原子 evidence：保留 source order/page/bbox/hash/provenance；
- structural evidence block：仅按同一已证明 heading occurrence 聚合连续证据。

命中原子成员时，L2/L3 可展开同一 structural block、table family 或 source-page fallback。
taxonomy 可以增加召回入口，但不得改变成员、边界、标题或 source locator。表格 title 取
最深已证明 heading；caption 始终留在 payload，绝不回填 title。

## 8. 变更与验证

任何结构行为变化必须：

1. 升 `RULES_VERSION`；
2. 用正例与邻接负例覆盖 failure family；
3. 对代表性真实 PDF 做 raw source → v4 IR → final units 重放；
4. 汇总而不是隐藏 structure conflicts/fallback/visual 分布；
5. 通过独立只读审查后，才可冻结全量 manifest 并进入 reset/reparse。

外部机制与可借鉴不变量来自
[PDFium structure API](https://pdfium.googlesource.com/pdfium/+/main/public/fpdf_structtree.h)、
[Docling](https://github.com/docling-project/docling) 和
[Unstructured](https://github.com/Unstructured-IO/unstructured)；这些项目只提供机制对照，
不覆盖本服务的产品契约，也不授权复制其项目特定启发式。
