---
id: disclosure_anchor_design_document_outline_toc
project: disclosure_anchor
title: 文件脉络与标题层级边界
status: implemented
revised_at: 2026-08-12
depends_on: provider_document.v1、provider_unit.v23、provider_unit_locator.v9、06R 检索投影
---

# 文件脉络与标题层级边界

## 1. 目标

L1 给 L2 一棵可追溯、保守的章节树和对应 coarse Units。脉络是 provider occurrence 的
确定性投影，不是第二套 PDF parser，也不修正文、表格或字符。

## 2. 输入与裁决

MinerU Medium 提供 reading order、title candidate、page/bbox 与原始 payload。层级阶段只处理
已有 source occurrence，绝不从普通正文创造标题。通用负证据先行：page furniture、表内 carrier、
普通 caption/footnote 与跨页续句不能开节。唯一窄例外是 Provider 将缺失标题合入 table block，
且恰有一个非空 `table_caption` 以强根编号开头；它以 source index + payload ordinal 成为 candidate。
“表4”、括号子组、checkbox-only 或无编号 selector、正文偶现编号及多 caption 均不满足；
强编号 table-caption 即使同时携带适用性选择，仍按上一条 source-bound 窄例外裁决。

层级信号顺序是本项目决策：

1. 唯一绑定到 candidate 的 PDF bookmark / authored ToC hint；
2. 明确且成体系的编号家族；
3. candidate-local、全篇校准的字号/样式 hint；
4. 弱 provider level。

每个 hint 必须绑定 source PDF hash、raw block hash 与 source index；歧义或 stale hint 直接拒绝。
弱 provider level 只作 leaf tier，不得压过可靠编号父级。

## 3. parent/headpath

accepted candidate 按 reading order 用单调栈生成 parent 与完整 headpath。每一级保留
`source_index`、`payload_ordinal` 和 `placement_source`，从 ProviderDocument 可回放 page、bbox、text 与 raw hash。
不确定 candidate 降为 body 或挂到最近可靠父级；不得猜一个具体 sibling parent。弱 provider-only、
无编号且全文精确等于“适用”或“不适用”的 selector statement 必须降为 body；“适用范围”等
有实质语义的标题及带可靠编号的标题不受此规则影响。

编号重启只在 source-bound 结构证据下纠正。表格和 payload kind 不得作为缺失章节的代理边界，
也不得使可靠 plain-numbered parent 出栈。无编号弱标题仅在紧邻 provider source block 明确从同族
ordinal one 重启时退出已完成 subgroup；较远或只是变小的 ordinal 保持原父级。

每个 accepted heading 开一个 coarse Unit；文首内容默认进入无标题 preamble Unit。只有同页、位于首个
heading 之前、且全部是闭合集合内的证券/债券代码与简称、公告编号或无文字 hash-bound 图像时，
这些机械封面块并入首个有标题 Unit；源 block、搜索文本、图像 digest 与顺序全部保留，未知标签、
句式正文、跨页前言仍保持独立 preamble。registered
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
- 除上述强编号 table-caption source occurrence 外，caption、业务 taxonomy、搜索召回与 metadata title
  不反向改变结构。

## 6. 验收

真实样本必须逐项清点 blocks/headings/units，验证 title 不重复进入 body、Q/A 或表内文本不被
误升、编号父子连续、preamble 保留、source locator 可回放。任何增强都同时提供邻接负例，禁止
文档 ID、公司名或固定章节词典补丁。
