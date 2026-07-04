# adapters/db/postgres — 存储与迁移

```text
schema.py          三 schema（disclosure_core/public/ops）+ 四角色常量（owner/app/reader/l2_reader）
migrations/        alembic；文件名即 revision（0001…0007）；0001–0007 已冻结不改，
                   新改动一律开新迁移（0008=unit builder provenance 归 05，0009=队列视图归 08）
models.py          SQLAlchemy 模型 + 与迁移同名的 CheckConstraint（两边必须同名同义）
repositories.py    仓储实现；unique 冲突翻译成领域错误（DocumentIdentityConflictError 等）
mappers.py         entity ↔ model 全字段映射（新加列两边都要动）
unit_of_work.py    SqlAlchemyUnitOfWork：默认回滚、显式 commit
bootstrap.py       角色/库/schema 幂等创建（调用方必须用 AUTOCOMMIT 引擎——见 cli/db.py）
connection.py      create_db_engine / URL 解析（socket DSN 带 port=55432）
```

读写边界：写侧只进 core/ops 表；**读契约只有 `disclosure_public.*_v1` 视图**
（32 列全集见 04R-R7）；ops.pending_*_v1 队列视图只授 app 角色、只暴露事实列，
阈值由调用方从 settings 施加。视图列变更 = 契约变更，必须同步
contract 测试 + `docs/implementation/checks/contract-checklist.md`。
迁移往返核验命令见 04R §6.2（六个根路径 env 缺一会报误导性的 "No migration database URL"）。
