---
id: disclosure_anchor_agent_execution_rules
project: disclosure_anchor
title: AI Agent 产品实施规则
status: final-for-implementation
created_at: 2026-06-26
updated_at: 2026-07-12
---

# AI Agent 产品实施规则

本文件只补充 disclosure_anchor 的产品实施边界。代理路由、授权、durable state、验证与独立审查
以适用的 `AGENTS.md` 为准；不要在这里复制一套第二工作流。

## 1. 按任务读取权威

任何产品实现先读根与服务 `AGENTS.md`，再按改动范围读取：

```text
../../docs/reference/投研预测引擎顶层框架协议_v0.8.md
docs/architecture/service-purpose.md
docs/implementation/milestones/<relevant>.md
docs/implementation/checks/<relevant>.md
```

`service-purpose.md` 是服务语义契约；顶层协议优先于服务文档。只读与任务相关的 milestone/checklist，
不要求每个小修遍历整套实施文档。

## 2. 默认不改变的架构决定

除非用户明确授权并更新相应协议/计划：

```text
一个 PostgreSQL 集群 + database invest_engine
服务以 schema + role 隔离，跨服务只读 versioned public views
模块化单体 ports/adapters
解析器和 provider 通过端口/配置注入，不把具体传输形态写入领域层
现有 launchd + worker 调度边界；不引入 Redis/Celery/Kafka/Airflow
本服务不实现 L2-L6
运行态与原始数据放 /Volumes/AgentSSD，代码留在 Git 工作树
```

## 3. 实现范围

每次只实现用户请求和当前计划授权的最小切片。不要顺手加入 claim/evidence/forecast 表、vector
database、table_cell 核心索引、Docker PG、新队列平台、独立编排平台或备份系统。

公共视图、API/CLI、错误模型、迁移、导出契约或数据位置发生变化时，同步更新对应测试、schema、
checklist 和用户命令。已应用迁移只新增后继，不回写历史。

## 4. 路径与数据边界

- 运行时代码不得硬编码 AgentSSD 绝对路径；通过 settings 和路径构造器注入。
- 外置盘未挂载或必需根不可写时 fail closed，不伪造成功或静默降级。
- 本服务只写 `disclosure_core`、`disclosure_ops` 与自己的 AgentSSD 服务根。
- 兄弟服务只读 `disclosure_public.*_v1`、Filing API、change feed 和 source references。
- DB 保存相对 locator + hash；API 返回契约化 locator/source reference，不泄露本机绝对路径。

## 5. 验证

行为变更必须有能观察到结果的验证：unit/contract/integration、代表性 fixture replay、doctor 或明确
记录的环境 blocker。涉及 PDF、parser、文件归档、DB publication、API 或 worker 状态时，优先使用
已有本地代表性样本；纯 synthetic 验证不足时按
`docs/implementation/checks/fixture-and-test-policy.md` 记录例外。

默认门是 `make agent-check`；DB/migration、真实 provider 或 MinerU 行为再按任务运行显式 opt-in gate。

## 6. 失败交接

无法完成时给出：失败位置、已完成内容、可复现命令、根因或精确未知点、相关日志/状态位置、数据
是否受影响，以及最小下一步。不要用“可能是环境问题”代替证据。
