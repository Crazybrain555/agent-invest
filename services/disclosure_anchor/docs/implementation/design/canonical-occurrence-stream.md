# Canonical Occurrence Stream — 分组前统一证据流设计

状态：已定稿待实施（2026-07-28）。取代三套 post-hoc placement 语法
（`source_native_fallback` 编排、`source_evidence_projection` planner、
`document_unit_audit` placement 重放）。

## 1. 问题与根因

Native gap（MinerU 未认领的原生文本/视觉证据）目前在 unit 分组**之后**决定去向：
builder 产出 finalized drafts 后，从 payload 反刮 `source_item_index` 集合重建"物理足迹"，
再用 slot 插入把 gap 物化为 document-scope mixed unit。由于分组后只剩 unit 级偏序，
placement 只能得到 `proved / inside_regular_unit / incomparable_same_page /
regular_order_conflict` 四态，后三态以 `needs_review` + `review_reason` 降级发布——
违反「证据不足必须 fail loud，不能降级猜位置」。同一套判定在 planner 与 audit 各写一遍
（制度化于 AST 隔离测试），且已产生真实语义分歧：mapped 页不一致时 planner 抛错而
audit 降级；`order_state=conflict` 仅在紧邻 gap 时生效，无 gap 文档的顺序冲突静默通过。

根因不是判定规则不够聪明，而是**判定时机太晚**：ledger 在解析边界已经证明了每页的
原子全序（`(source_item_index, part_order, start_char)` 三级 position、页内 `order` 严格
递增、bbox/char_span/layout_path 齐全），但 `source_evidence_validator.
source_evidence_proof_from_validated_ledger()` 把 selector 压缩成仅 `source_item_index`，
下游只能在信息不足的空间里补偿。

## 2. 业界对照结论（设计前调研，2026-07-28）

对照 Docling（layout_postprocessor / DoclingDocument）、Unstructured（sorting /
pdfminer_processing）、MinerU（pipeline_magic_model / span_pre_proc）源码级核对：

采纳的不变量：

- **I1 gap 判定发生在唯一一次排序/分组之前**；判定后下游无法区分"原生补入"与
  "模型产出"（Docling orphan cluster 在 ReadingOrderModel 之前生成）。
- **I2 原生抽取序作主键、几何只作 tiebreak**（Docling `_sort_cells` 只按 cell.index）。
  反例即 MinerU：模型 index 独裁主序，代价是未认领 span 无输出路径静默蒸发
  （issue #3849）。
- **I3 containment 是排序前算好的单归属阈值关系，分母是子元素自身面积**（三家同构：
  intersection_over_self，阈值各自标定）。
- **I4 无法归属的原生文本升格为一等 occurrence（自身 bbox 为几何），而不是
  "插到某个已有单元附近"**——不再决定"插到哪"，只决定"它是什么"，位置交给
  同一条排序规则。
- **I5 降级必须带类型、可见、可序列化**（Docling furniture layer / MinerU
  discarded_blocks）。
- **I8 几何 containment 排序前解决；语义 association（caption↔table）排序后解决，
  以既定顺序为距离度量，只改树形不改线性序。**
- **I9 顺序信号不可信时按页整体确定性降级，不逐元素兜底。**
- **I10 序号一次性致密赋值，下游不再重编号。**

拒绝项：跨页重排（R2）、layout 模型序独裁（R1）、StructTree 当主序（R6，只作
约束/见证）、逐元素临时兜底放置（R8，即被本设计取代的三套语法）、无记录的
几何/统计静默过滤（R4）、照抄他家阈值（R9）。

自建不变量（业界无先例，须明示）：**位置无法证明 → build 失败**。三家的处理都是
静默（空 prov / 沉底 / 丢弃）；本服务契约要求可验证溯源，故 fail-closed 是自己
承担的更强选择。可达成性由证明粒度分层保证（§3）。

## 3. 核心模型

### 3.1 位置证明分层（"所有位置都可证明"的达成机制）

每个证据原子的位置证明至少存在于**页级**：Poppler 原子天然携带
`(page_idx, 页内词序)`，这是物理事实。更细粒度只在有证据时细化：

| 证明级 | 含义 | 依据 |
|---|---|---|
| `native_proven` | 页内线性位置由原生词序证明 | 该 occurrence 的原生词序 span 与同页其他 occurrence 的 span 不重叠 |
| `containment_proven` | 归属某 carrier 内部 | 词序有界：gap 的前后锚是同一 carrier 的 mapped 事件（`bounded_by_same_source`），精确无需标定；bbox intersection_over_self 仅作未来可选细化（须语料标定，I3/R9） |
| `provider_attested` | 页内相对序由 provider block 序确定 | 同页 native span 重叠（order_state=conflict 的新语义），页内整体切换到 MinerU block 序，typed 记录 + 计数 |
| （无） | 页身份都矛盾 | **fail loud**（SourceEvidenceClosureError） |

`needs_review` 在 placement 维度整体消失：要么在某个证明级上 proved，要么 build 失败。
旧四态映射：`proved→native_proven`；`inside_regular_unit→containment_proven`；
`incomparable_same_page` 与 `regular_order_conflict→provider_attested` 页级确定性
lane（I9）或真矛盾 fail。③a 实况：containment_proven 的 gap 仍是独立 document-scope
单元（紧随 owner 之后发布）；「拼进宿主 unit 的 parts」需要 search-atom 目标字段
重编号与 audit 双形态支持，记为 ③b 后续。physical_context 同时携带 gap 级
`order_basis` 与页级 `page_order_basis`（containment 覆盖不吞掉宿主页 lane 信息）。

### 3.2 CanonicalOccurrence 流

在 parser/validator 边界（ledger 校验之后、builder 之前）构造一次：

```
CanonicalOccurrence:
  kind: mineru_carrier | native_text_run | native_visual
  page_idx: int
  native_span: (start_word_order, end_word_order) | None   # carrier 由 mapped 原子推得
  provider_order: int | None                               # MinerU block index
  order_basis: native_proven | provider_attested (页级标注)
  containment: occurrence_ref | None                       # I3 单归属
  selectors: 完整精确 selector（field/index/char_span/value_sha256/projection）
  source_refs / visual_artifacts / needs_review(内容质量维度，非位置维度)
```

排序规则（唯一一次，R2 严格 page-major；实施裁决 2026-07-29 修订）：
1. 主键 `page_idx`；
2. 页内：**carrier 一律保持 provider 序**（= content-list/IR 序，公开 part.order
   仍锚定 IR 事实）；原生词序只决定 gap run 相对 carrier 的插位——每个 gap 以其
   前导 mapped 锚（或 page_prefix/page_only 的确定性槽位）织入；
3. builder 排序键：carrier=(element_order,0,0,0)，native=(前导 carrier
   element_order,1,page_idx,span_start)。
   按原生词序重排 carrier 本身（如双栏页 Poppler 序 vs MinerU 序分歧）是更大
   行为半径的独立决策，留待全量回放的语料证据支持后单独立项；page_basis
   仍逐页记录（native_proven/provider_attested）作为诊断与 audit 合法性判据。

上游前置：`source_evidence_validator.py` 的 proof 转换补回 ledger 已证明的完整
selector 与 carrier order / atom bbox（丢失点 :336-347）；`MappedSourceEvent`
扩展为携带 field/index/char_span/value_sha256。

### 3.3 Builder 集成

③a 实况：native run 物化为完整 UnitDraft（text 带 `text_identity_exact`
selector；visual 带 hash-bound artifact），以 `native_order_anchor` 排序键在
s6 分组前注入草稿序列，统一走 s7 finalize（但 taxonomy 恒为 document_content，
filing 级键不得触碰 native 证据单元）。gap 单元当前保持 document-scope、
`detached_from_section=True`：section 组装中遇到时悬挂、容器 flush 后紧随输出
（不切章节、不嵌套 mixed）。「native run 作为一等 PreparedElement 过 s2 获得
章节归属」与「containment run 拼进宿主 unit parts」是 ③b 后续（需 audit 双形态
与 search-atom 重编号支持）。`unit_preparation` 单次调用 builder；
`bind_visual_page_evidence`（非 placement 语法）保留为独立 pass。

### 3.4 Audit 收敛

删除 placement 重放（`_native_slot_constraints`、`_native_position_relation`、
`_build_native_audit_plan` 的 gap 分区重算等），保留并强化**结果性断言**（直接对
ledger 事实校验，不 import 流构造器，独立性不变）：

- 每个 ledger 原子在发布单元中恰好出现一次且带精确 selector；
- 极大 gap run 不得拆分/合并；
- 页的 order_basis 合法（native_proven 页无 span 重叠；provider_attested 页有
  typed 记录与计数）；`order_state=conflict` 必须体现为该页 provider_attested——
  **与是否存在 gap 无关**（修复静默通过漏洞）；
- mapped 页 ≠ element 页 → 一律 fail（裁决 planner/audit 分歧，从严）；
- native 单元形状不得发明结构（title/heading/taxonomy/applicability 保持既有断言）。

## 4. 删除面与新增面（诚实核算）

毛删（生产）：planner ~228 行 + G1 编排 ~93-115 行 + audit 重放 ~253 行 ≈ 575-600 行；
测试面另删/重写 ~400-600 行（6 个 placement 语义测试、AST 隔离测试改写、
audit placement fixtures）。新增：流构造 + builder 集成 ~150-250 行。
净删（生产）约 330-450 行；含测试面接近 HANDOFF 预估的 700-1,000。

## 5. 实施阶段与验收

1. **上游 selector 补全**：validator/contract/tests；`MappedSourceEvent` 扩展。
2. **流构造器**：新模块 + 独立正反例测试（含 provider_attested lane、containment
   阈值标定用真实语料统计、fail 集）。
3. **Builder 集成**：s1 消费流；删 G1/G2 placement；`unit_preparation` 简化。
4. **Audit 收敛**：删重放，改结果断言；AST 隔离测试更新。
5. **测试迁移 + 账本**；RULES_VERSION 递增。
6. **回放验收**：4 份真实样本 → 28 份历史失败集 → 代表语料 source replay；
   门槛：mapped_order_conflict 页全部落 provider_attested lane 且计数可见、
   non_proved_occurrence_placement=0、occurrence_coverage_gap=0、
   碎片化指标不劣化。

每阶段过 `make agent-check`；阶段 3/4 完成后跑独立只读复审。
