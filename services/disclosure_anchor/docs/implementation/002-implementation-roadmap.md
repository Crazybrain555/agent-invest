---
id: disclosure_anchor_implementation_roadmap
project: disclosure_anchor
title: disclosure_anchor 分阶段实施路线图
status: final-for-implementation
created_at: 2026-06-26
---

# disclosure_anchor 分阶段实施路线图

## 0. 阶段总览

本路线图按“方便核验、方便中断、方便 agent 接力”的原则拆分。每个 milestone 都必须能独立验收。

| 阶段 | 名称 | 目标产物 | 是否接外部网络 |
|---|---|---|---|
| 00 | 本地环境与样本 parser 验证 | 外置盘、PG、MinerU 跑通；样本生成 IR / units | 否 |
| 01 | 代码骨架与配置 | repo、settings、PathBuilder、doctor、基础 domain | 否 |
| 02 | PostgreSQL 与 migrations | database、schema、roles、核心表、public views、repositories | 否 |
| 03 | 文件存储与 raw archive | raw store、atomic write、hash、register local PDF | 否 |
| 04 | MinerU adapter 与 NormalizedIR | parser_artifacts、MinerU mapper、normalized_ir | 否 |
| 04R | Phase000–004 返工 | 0007/0008 迁移、主体解析服务化、注册核心、IR v2、事件惯例、parse 加固、doctor | 否 |
| 05 | document_unit builder 与 active run | text/table/qa/mixed units、载体规范化、publish 原子事务、diff 事件 | 否 |
| 06 | Filing API 与 public contracts | API、OpenAPI、source_ref、changes、contract tests | 否 |
| 07 | CNINFO 增量同步 | source adapter、index sync、download、checkpoint、重试 | 是 |
| 08 | worker loop 与本地运行 | worker-once/loop、locks、reports、运行闭环 | 可选 |

## 1. 实施原则

1. 先本地样本闭环，再接 CNINFO。当前进度：00–04 已完成；下一步按 04R → 05 → 06 → 07 → 08 执行，
   04R 是 05 的硬前置（IR v2、change_kind 列、事件惯例、注册核心都在 04R 交付）。
2. 先 raw archive，再 parser，再 unit，再 API。
3. 每个阶段都要有测试或可复核产物。
4. 不为了后续 L2-L6 预先实现它们的数据结构。
5. 每个阶段结束后，必须更新 `checks/acceptance-matrix.md` 对应项。

## 2. 推荐执行顺序

```text
00 → 01 → 02 → 03 → 04 → 05 → 06 → 07 → 08
```

不建议跳过 00。Phase 00 的目的不是写代码，而是避免在 parser 不可用、外置盘路径不稳定、模型缓存乱放的情况下开始建库。

## 3. 每阶段统一验收格式

每个 milestone 完成后，执行 agent 必须输出：

```text
完成了哪些文件
新增了哪些命令
新增了哪些测试
运行了哪些检查
检查输出摘要
未完成或故意不做的事项
下一阶段输入是否齐备
```

## 4. 阶段间交付物

```text
00 输出：样本 PDF 的 normalized_ir / document_units fixture，外置盘与 PG 初始可用。
01 输出：可启动的空 app、settings、doctor、PathBuilder、Makefile。
02 输出：可迁移的 DB schema、repository integration tests。
03 输出：本地 PDF 可登记为 document + raw file + hash。
04 输出：document 可解析成 parser_artifacts + normalized_ir。
05 输出：document_unit 可生成，active run 可发布，change_event 可产生。
06 输出：L2 可通过 API/public views/source_ref/changes 消费。
07 输出：CNINFO 可对精选股票池增量发现和下载公告。
08 输出：worker 可把 pending document 跑到 active run，失败可定位。
```

## 5. 失败处理原则

- 环境失败：停在当前阶段，不向下游扩散。
- parser 质量失败：记录 fixture 与质量问题，不改数据架构。
- DB migration 失败：不得手工改库绕过 migration。
- raw hash 不一致：进入 quarantine，不生成 document_unit。
- active run 发布失败：旧 active run 保持不变。
- API contract test 失败：不得进入下一阶段。
