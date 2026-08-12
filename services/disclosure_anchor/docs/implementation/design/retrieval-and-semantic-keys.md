# Provider-native 检索投影与语义键

状态：current（2026-08-12）。本文件只描述当前 writer；旧 NormalizedIR/unit-source-projection
实现从工作树删除，需要考古时查 Git。

## 1. 边界

L1 保存 source-bound Unit，并生成可完全重建的检索投影。检索投影不是来源证据，不改变
`payload`、`content_hash`、`query_projection_hash`、`structure_hash`，也不产生 outbox 事件。
自然语言 claim/evidence/forecast 仍属于 L2。

当前唯一新 writer 是 MinerU 3.4.4 Hybrid-medium：

- `provider_document.v1` 保存官方 provider artifact 的闭合投影与 hash-bound inventory；
- `provider_unit_locator.v1` 保存 Unit 对 source block、逻辑表物理段、evidence digest 与检索目标的
  显式引用；
- 历史 `normalized_ir.v4` 只允许通过窄 resolver 读取已发布 evidence，不再 Build、Rebuild、
  Publish 或重建检索投影。

## 2. Unit 可检索字段

每个 Unit 的投影只有四个输入面：

1. `title`：已接受的 source heading 叶标题；metadata document title 绝不复制到 Unit。
2. `heading_path`：已接受 heading occurrence 的完整根到叶路径。
3. body：只回放 `provider_unit_locator.v1.search_targets` 明确列出的 provider payload destination。
4. `semantic_key`：可选的真实受控 scalar；当前 Provider writer 不在 L1 推断业务 taxonomy，
   因而写 NULL，检索 key channel 为空。

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

表格 body 保存 MinerU owner 的原始 `table_body` HTML。检索时仅投影其可见文本片段；raw HTML
始终留在 Unit payload。L1 不解析 grid、不恢复 cell continuation、不猜 header row。

MinerU merge-on 的跨页表只发布一个逻辑 owner body；continuation stub 没有搜索目标。每个物理页
segment/crop 仍以 page/bbox/hash-bound evidence 留在 locator/ProviderDocument。相邻相似表不会因
文本或 bbox 相似度合并。

无文字的 image/chart 仍通过 `content_artifacts` digest 参与 content hash，但没有 body atom。
supporting table crop 只作 evidence，不成为第二份可检索正文。

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

## 6. 版本与验证

任何 tokenizer、Unicode normalization、binding/HTML transform 或 atom/window 行为变化都必须升
`RETRIEVAL_RULES_VERSION`。验证至少覆盖：

- accepted heading 不重复进 body，demoted title 仍按正文回放；
- equal-but-distinct source occurrence 保持两个 atom；
- table HTML visible text、caption/footnote 与跨页 stub owner 守恒；
- visual-only Unit 为零 body atom但 content hash 随 artifact digest 变化；
- malformed/unknown/cross-part locator fail closed；
- active historical v4 run 不进入新投影候选；
- parent/window/atom 的 run-atomic replacement、orphan prune 与 PostgreSQL safety probe。

公开 `document_unit`、search projection view 与 source/evidence 引用仍保持现有 v1 列面；新旧 Unit
通过 locator contract 区分，绝不按文件存在性、parser 版本或字符串形状猜代际。
