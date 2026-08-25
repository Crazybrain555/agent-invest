---
id: disclosure_anchor_milestone_02_postgres-and-migrations
project: disclosure_anchor
title: PostgreSQL 与 migrations
status: complete
created_at: 2026-06-26
implemented_at: 2026-06-29
verified_at: 2026-06-29
---

# Milestone 02: PostgreSQL 与 migrations

> 实施状态（2026-06-29；2026-08-23 边界校正）：已实现并完成独立复测。`make db-create` / `make migrate` 幂等；
> `disclosure_core` / `disclosure_public` / `disclosure_ops` 三个 schema、八张 core 表、`outbox_event`、
> `alembic_version`、五个 public view、四个角色及最小权限、单 active run 偏唯一索引均已验证；
> repository / UnitOfWork 与 16 个集成测试通过（无 DB 环境时干净跳过）。A08-A10 保持 pass。
> 初次独立 review 发现 migration/权限问题，已用 `0002_harden_ops_permissions` 修复；post-fix
> 独立复查无 material findings，verdict pass，confidence high。
> 当前仓库统一使用预先存在的共享数据库 `invest_engine`，其明确的仓库级 owner 是非登录角色
> `invest_engine_owner`；服务 bootstrap 只验证该 owner，然后创建自有 roles/schemas。它不创建
> database、`invest_engine_owner`，也不取得整库 ownership。

## 1. 目标

在共享 `invest_engine` database 内建立服务自有 schema、roles、核心表、public views、outbox seq 和 repository adapter。

## 2. 范围

范围内：

- native PostgreSQL cluster 脚本管理。
- database / schema / role 初始化。
- Alembic migration。
- 核心表。
- public views。
- repository integration tests。


## 3. 实施细则

1. Makefile 增加：

```bash
make pg-init
make pg-start
make pg-stop
make pg-status
make db-create
make migrate
```

2. 验证仓库级预先创建的共享 database `invest_engine` 由 `invest_engine_owner` 所有；服务不得创建
   database/该 owner role，或拥有整库。
3. 创建 schema：

```text
disclosure_core
disclosure_public
disclosure_ops
```

4. 创建 roles：

```text
disclosure_owner
disclosure_app
disclosure_reader
future_l2_reader
```

5. Alembic 创建核心表：

```text
company
security
tracked_company
source_access
source_checkpoint
document
processing_run
document_unit
outbox_event
```

6. 创建 public views：

```text
documents_v1
document_units_v1
processing_runs_v1
source_refs_v1
change_events_v1
```

7. 实现 repositories 与 UnitOfWork。
8. integration tests 使用测试 database 或临时 schema。


## 4. 检查点

- `make migrate` 幂等。
- `disclosure_app` 可写 core/ops。
- `disclosure_reader` 只能读 public views。
- public view 不暴露绝对路径、密钥、内部错误堆栈、MinerU raw JSON。
- repository tests 可创建 company/security/document/run/unit/outbox。
- UnitOfWork rollback 测试通过。


## 5. Definition of Done

- DB schema 可迁移。
- 权限边界成立。
- public views 可查询。
- repository integration tests 通过。


## 6. 明确不做

- 不写 CNINFO。
- 不做 API query。
- 不写 raw file store。
- 不写 claim/evidence 表。


## 7. 交付给下一阶段

- database/schema/roles。
- Alembic migration。
- repository adapter。
- public views。


## 8. 常见失败与处理

- 权限过宽：先修 role grant，不继续。
- migration 不可重复：修 Alembic，不手工改库。
- public view 泄露 relpath 以外绝对路径：立即移除。


## 9. 独立 testing 验证

验证环境：

```text
PostgreSQL 18 / AgentSSD pg18-main
socket: /Volumes/AgentSSD/agent_system/postgres/sockets
port: 55432
database: invest_engine
```

命令验证：

```text
make migrate                         pass，重复运行 pass
.venv/bin/python -m alembic current  0002_harden_ops_permissions (当时 head；现已推进至 0006)
make test-integration                16 tests, OK
make test                            DB env: 54 tests, OK
make test                            no DB env: 54 tests, OK (skipped=16)
.venv/bin/python -m compileall -q src tests  pass
```

SQL 点检：

```text
tables=10
views=5
alembic=0002_harden_ops_permissions
leak_cols=0
active_idx=1
app_alembic_priv_count=0
missing_fk_indexes=none
```

Fresh DB 验证：

- 创建随机临时 database。
- 建立 `disclosure_core` / `disclosure_public` / `disclosure_ops` schema 和基础 schema grants。
- 从零执行 `alembic upgrade head`。
- 验证 head、public views、`alembic_version` 权限和 FK helper indexes。
- 删除临时 database。

测试结论：

- Phase 02 的 DB schema、migration、权限、public views、repository、UnitOfWork 验收项通过。
- `make doctor` 当前不作为 Phase 02 DB 证明；PG doctor 输出项仍是后续补齐项。
- Post-fix 独立 review：No material findings，Overall verdict pass，Confidence high。
