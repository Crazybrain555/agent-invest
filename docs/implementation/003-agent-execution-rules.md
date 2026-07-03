---
id: disclosure_anchor_agent_execution_rules
project: disclosure_anchor
title: 后续 AI Agent 实施规则
status: final-for-implementation
created_at: 2026-06-26
---

# 后续 AI Agent 实施规则

## 1. 必须先读的文件

每次开始实现前，agent 必须读取：

```text
docs/architecture/service-purpose.md
docs/implementation/001-disclosure-anchor-framework.md
docs/implementation/002-implementation-roadmap.md
docs/implementation/milestones/<current>.md
docs/implementation/checks/acceptance-matrix.md
```

`service-purpose.md` 是 canonical 契约；实施文档与它冲突时以它为准。

如果实现涉及 API、DB、doctor、contract，还必须读取对应 checklist。

## 2. 不得改变的决策

除非用户明确要求，agent 不得改以下决策：

```text
native PostgreSQL + 外置 PGDATA
native macOS MinerU batch worker
单 PG cluster、多 database
模块化单体 ports/adapters
不引入 Redis/Celery/Kafka/Airflow
不实现 L2-L6
数据和运行态放 /Volumes/AgentSSD
代码放内置盘 Git 仓库
```

## 3. 实现范围控制

每次只实现当前 milestone 范围。

禁止为了“顺手”加入：

- claim 表；
- evidence ledger；
- forecast snapshot；
- vector database；
- table_cell 表；
- page/bbox 核心索引；
- Docker PG；
- Celery worker；
- 独立 scheduler；
- 备份恢复系统。

## 4. 路径规则

代码中不得硬编码 `/Volumes/AgentSSD`。

只允许：

```text
settings → DATA_ROOT / SHARED_ROOT / RUNTIME_ROOT
FileStorePathBuilder → 相对路径
DB → relpath + hash
API → artifact locator / source_ref
```

启动时如果外置盘未挂载，必须 fail closed。

## 5. 数据边界规则

本服务只能写：

```text
disclosure_core
disclosure_ops
/Volumes/AgentSSD/agent_system/services/disclosure_anchor
```

兄弟服务只能读：

```text
disclosure_public.*_v1
Filing API
change feed
source_ref
```

不得让 L2 直接读 `disclosure_core`。

## 6. 测试规则

每个 milestone 至少要有一种可执行检查：

```text
unit test
integration test
contract test
fixture replay
make doctor
manual inspection report
```

能自动化就自动化；不能自动化时，输出固定格式报告。

## 7. 文档更新规则

每个 milestone 完成后，agent 必须更新：

```text
docs/implementation/checks/acceptance-matrix.md
当前 milestone 文件中的状态或备注
必要时更新 docs/operations/ 或 README
```

不得只改代码不改实施状态。

## 8. 失败输出格式

如果无法完成当前阶段，agent 必须输出：

```text
失败位置
已完成内容
失败原因
可复现命令
相关日志路径
建议下一步
哪些数据未被破坏
```

禁止模糊描述“可能环境问题”。
