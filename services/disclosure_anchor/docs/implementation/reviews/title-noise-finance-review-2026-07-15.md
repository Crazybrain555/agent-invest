# Title-noise 金融事实复核（2026-07-15）

## 1. 结论

`title_noise` 只能回答“这个标题确定没有新增金融事实吗”，不能用“是否例行”代替。
本轮把绝对门从 77 条 JSON 规则 / 79 个 SQL pattern 收缩到 12 / 12：

| 处置 | pattern | 公告 | 已有 document | candidate-only | r12 行为 |
|---|---:|---:|---:|---:|---|
| 事实公告 | 41 | 311 | 264 | 47 | 移出 absolute noise |
| 条件去重 | 26 | 157 | 127 | 30 | 移出 absolute noise，暂不猜主附件 |
| hard noise | 12 | 127 | 39 | 88 | 继续绝对排除 |
| 合计 | 79 | 595 | 430 | 165 | 468 行恢复正常分层 |

语料为 2026-07-15 生成的
`/private/tmp/disclosure_noise_audit_20260715/title_noise_announcements.csv`。
文件为 596 行（含表头），SHA-256
`f09e0183bb9bda3162f01049fb0093ae30e476c31e20fa3a404c50c380585162`。
逐行字段包含标题、命中 pattern、分类码、document/raw PDF、active units 与历史发布状态。
保留的 12 个 hard pattern 命中 127 行，其中 10 个 active units；本轮不操作 live 数据。

## 2. 金融判断

41 个事实 pattern 覆盖：

- 股本/稀释：回购注销完成或实施、解除限售、归属上市、行权结果、期权注销/作废、
  授予登记、行权/授予价格调整；
- 资本结构：季度转股及股份变动、优先股股息实施；
- 债务：实际发债完成、票据发行与担保；
- 现金/募投：募集资金存放与使用、闲置/归还/补流/专户注销、摊薄即期回报。

这些公告即使格式例行，也分别确认了已发行股数、潜在股数、流通日期、现金流、债务余额、
募投进度或 EPS 稀释假设，不能作为 hard noise。为覆盖仅有 `0115` 等股本变动码、没有
equity_incentive 共码的公告，`equity_share_change` 从 register_only 移入 process。
`convertible_bond` 已在 process；financing/dividend 仍按 r3 保持 register_only，可由公司
`process_classes` 显式拉回。

26 个条件项包括英文/H 股副本、名单和中介附件、理财授信、转股日历、异动及重复风险提示。
这些只有在存在同发行人、同事件、同期间的可靠主件承接时才可去重。当前没有稳定的
主附件 family/linkage 键；仅凭同公司、同日、相似标题会再次误杀。因此 r12 先移出绝对门，
交给现有 class/process/carrier：中介载体仍受 carrier guard，其他可能暂时多处理。

继续 hard 的 12 个 pattern：

1. `股票期权%限制行权期间`
2. `激励计划自查表`
3. `提前赎回%的第%次提示性公告`
4. `即将停止%的重要提示性公告`
5. `回售的第%次提示性公告`
6. `授权董事会%中期分红方案的公告`
7. `中期票据计划%上市`
8. `上市%中期票据计划`
9. `中期票据计划%挂牌`
10. `独立董事候选人声明`
11. `发售通函`
12. `赎回选择权%提示性公告`

这些是纯窗口日历、明确第 N 次重复提示、无金额授权、上市/挂牌/刊发行政载体或标准声明；
首发决定、实际发行条款和实际结果的标题不在这些窄 pattern 中。它们的池外/留出集证据沿用
[vocab-generalization-2026-07-13.md](vocab-generalization-2026-07-13.md) 的逐 pattern
记录；r12 没有新增或放宽任何 hard pattern，只减少绝对排除面。

## 3. 生命周期与上线边界

本轮不改变 runs/units 生命周期。用户明确裁决：策略认定应排除时，可以物理删除已生成
`processing_run` / `document_unit`，不强制保留为 inactive 历史。本次提交只改规则和
处理范围，不自动执行清理；live 清理仍应在明确范围、dry-run 对账和 runtime claim 下单独执行。

上线顺序：

1. `make config-check`
2. `make load-rules`
3. 重启 resident worker（处理策略不是规则表的一部分，不会由 load-rules 热刷新）
4. `make doctor`
5. 如决定清理剩余 hard-noise 成果，再单独预览并物理删除对应 runs/units

## 4. documents_v1 边界

2026-07-15 live 审计：`documents_v1` 1,535 行，`raw_file_hash` 空值 0；对应归档文件
存在性和哈希 1,535/1,535 通过。另有 3,565 个未下载候选在 `pending_download_v1`，
均不在 `documents_v1`。正式写入流程是下载并归档原件后创建 document，因此当前
`registered` 表示“已有原件、尚未解析/发布”。

数据库模型仍允许 raw path/hash 为 NULL，视图也不额外过滤，因此“documents_v1 只含已有
原件”是正式应用写入不变量，不是数据库 CHECK 硬约束；直接 SQL 或未来错误写入理论上可破坏。
