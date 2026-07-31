---
id: disclosure_anchor_design_document_outline_toc
project: disclosure_anchor
title: 文件脉络视图与目录证据边界
status: implemented
created_at: 2026-07-17
revised_at: 2026-07-27
depends_on: NormalizedIR v4、source-bound structure proof、06R 检索投影
---

# 文件脉络视图与目录证据边界

## 1. L2 消费目标

L2 先查看单份文件的章节、表格和附属证据，再进入具体 evidence block。L1 因此同时提供：

1. 可验证的原子 document units；
2. 由已证明 `heading_path` 派生的文件脉络；
3. 命中任一成员后可展开的 structural evidence block / table family /
   source-page fallback。

脉络是 evidence 的导航投影，不是另一套 PDF 解析器。

## 2. `document_outline_v1`

`disclosure_public.document_outline_v1` 是从 active published units 确定性派生的只读视图。
每行代表一个已发布 heading occurrence/path，包含：

- `document_id`
- `path` / `depth`
- `unit_count` 以及 table/image 等类型计数
- 去重的 semantic keys
- page span
- first order index

该视图不进入 content hash、不发送 outbox，也不反向修改 document units。L2 可按
`document_id, first_order_index` 读取整个文件的已证明骨架。

## 3. 目录不是结构白名单

旧实现曾解析文内“目录”文本、编号和页码，再把目录词面匹配回正文标题；该实现已删除。
原因不是某几条正则写得不够好，而是目录本身仍可能：

- 被双栏排版横向拼接；
- 标题与页码分列到不同 carrier；
- 只列到部分层级；
- 混入图表目录、附件目录或印刷页码；
- 与正文 occurrence 重名但不唯一。

因此：

- 目录词面、固定章节名、编号 grammar、caption 都不能开节或定 parent；
- PDF StructTree 中的 `TOC/TOCI` 是明确的非正文 role，用于防止目录项冒充 heading；
- PDF bookmark 只有绑定到唯一正文 occurrence 时才可提供 heading/parent 证据；
- PDF `H1`–`H6` 与 MinerU v2 typed title 仍按
  `heading-level-arbitration.md` 的 source-bound 规则处理。

目录可以帮助人工发现“PDF 似乎有章节但当前 proof 没有”的质量问题，但不能作为自动修复
白名单。

## 4. 缺口处理

发现目录、页图或人工阅读显示章节存在，而当前 outline 没有时：

1. 回查 raw PDF hash、StructTree/MCID/bookmark、MinerU v2/content-list/model/middle、
   page/bbox 与页面图；
2. 判断是 PDF 无 typed structure、原生结构未被读取、结构 occurrence 无法唯一对齐，
   还是 MinerU 漏 carrier；
3. 修复通用 source alignment / provider contract / visual evidence 机制，并用同族正例和
   邻接负例验证；
4. 在无法证明结构时保留原 carrier 或整页 fallback，记录 conflict/fallback family；
   不把最近标题、目录文本或监管 taxonomy 写回 `heading_path`。

## 5. 删除与重解析门禁

全量 source replay 必须同时报告：

- proven heading / provider/native/bookmark candidate 数；
- 每一种 `structure_proof.conflicts`；
- fallback pages/reasons、visual pages、native geometry issues；
- final units 的 source projection、顺序与证据文件闭包；
- `document_outline_v1` 与 active units 的确定性一致性。

“unit audit 0 errors”只表示已检查的不变量通过；删除许可还要求所有 material conflict/
fallback family 都有真实样本、根因和明确处置，不能保留未知分类。

## 6. 明确不做

- 不恢复 `toc_outline.py` 或 `audit_toc_alignment.py`；
- 不用目录、业务栏目名、caption、编号/标点正则推断标题；
- 不为 outline 新建可变业务事实表；
- 不让 taxonomy 或搜索召回结果反向改变证据内容、边界或 ancestry。
