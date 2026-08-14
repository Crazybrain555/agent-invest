---
id: disclosure_anchor_design_document_outline_toc
project: disclosure_anchor
title: 文件脉络与标题层级边界
status: implemented
revised_at: 2026-08-12
depends_on: provider_document.v1、provider_unit.v6、06R 检索投影
---

# 文件脉络与标题层级边界

## 1. 目标

L1 给 L2 一棵可追溯、保守的章节树和对应 coarse Units。脉络是 provider occurrence 的
确定性投影，不是第二套 PDF parser，也不修正文、表格或字符。

## 2. 输入与裁决

MinerU Medium 提供 reading order、title candidate、page/bbox 与原始 payload。层级阶段只处理
已有 candidate，绝不从普通正文创造标题。通用负证据先行：page furniture、表内 carrier、
caption/footnote 与跨页续句不能开节。

层级信号顺序是本项目决策：

1. 唯一绑定到 candidate 的 PDF bookmark / authored ToC hint；
2. 明确且成体系的编号家族；
3. candidate-local、全篇校准的字号/样式 hint；
4. 弱 provider level。

每个 hint 必须绑定 source PDF hash、raw block hash 与 source index；歧义或 stale hint 直接拒绝。
弱 provider level 只作 leaf tier，不得压过可靠编号父级。

## 3. parent/headpath

accepted candidate 按 reading order 用单调栈生成 parent 与完整 headpath。每一级保留
`source_index` 和 `placement_source`，从 ProviderDocument 可回放 page、bbox、text 与 raw hash。
不确定 candidate 降为 body 或挂到最近可靠父级；不得猜一个具体 sibling parent。

编号重启只在 source-bound 结构证据下纠正。表格和 payload kind 不得作为缺失章节的代理边界，
也不得使可靠 plain-numbered parent 出栈。无编号弱标题仅在紧邻 provider source block 明确从同族
ordinal one 重启时退出已完成 subgroup；较远或只是变小的 ordinal 保持原父级。

每个 accepted heading 开一个 coarse Unit；文首内容进入无标题 preamble Unit。registered
document title 不进入 Unit title/headpath。demoted candidate 仍按普通 body 保存。

## 4. document_outline_v1

`disclosure_public.document_outline_v1` 从 active published Units 确定性派生，不进入 content
hash、不发 outbox，也不反向修改 Unit。它按 source occurrence/path 展示 depth、unit count、
page span 和 first order index；同名标题的内部 identity 仍由 locator/source index 区分。

## 5. ToC、LLM 与表格的边界

- printed ToC 只在精确绑定已有 candidate 时给 hint，不能凭词面造标题；
- 低成本 LLM 若以后接入，只能 keep/demote/reparent/abstain candidate ID，整体输出不合法则
  回退 deterministic outline；
- logical table owner、continuation stub、physical segment 与 search primary 完全由独立阶段处理，
  不进入 heading arbitration；
- caption、业务 taxonomy、搜索召回与 metadata title 不反向改变结构。

## 6. 验收

真实样本必须逐项清点 blocks/headings/units，验证 title 不重复进入 body、Q/A 或表内文本不被
误升、编号父子连续、preamble 保留、source locator 可回放。任何增强都同时提供邻接负例，禁止
文档 ID、公司名或固定章节词典补丁。
