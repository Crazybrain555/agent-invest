# adapters/db/postgres — 存储与迁移

```text
schema.py          三 schema（disclosure_core/public/ops）+ 四角色常量（owner/app/reader/l2_reader）
migrations/        alembic；冻结策略（唯一权威表述）：所有已应用迁移一律冻结，新改动只开新迁移；
                   当前 head 必须由 Alembic revision graph 推导，不在测试/doctor 手写 revision 字符串。
                   增量速记：0008=unit builder provenance；0009=sync/download 队列视图（op.execute 内字面冒号要 \: 转义，
                   source_access.error 是 Text 需 ::jsonb cast）；0010=applicability/page_no；0011=payload_kind 'mixed'
                   + is_active_run 视图列；0012=provider 分类维表 + document_categories_v1；0013=semantic_keys + GIN 部分索引；
                   0021=documents/units 视图分类改并集推导；0022=证券规范形态 CHECK + coded/无码
                   标题路由恢复与 title_topic 对齐（public 列集不变）
models.py          SQLAlchemy 模型 + 与迁移同名的 CheckConstraint（两边必须同名同义）
repositories.py    仓储实现；unique 冲突翻译成领域错误（DocumentIdentityConflictError 等）
mappers.py         entity ↔ model 全字段映射（新加列两边都要动）
unit_of_work.py    SqlAlchemyUnitOfWork：默认回滚、显式 commit
bootstrap.py       角色/库/schema 幂等创建（调用方必须用 AUTOCOMMIT 引擎——见 cli/db.py）
connection.py      create_db_engine / URL 解析（socket DSN 带 port=55432）
```

读写边界：写侧只进 core/ops 表；**读契约只有 `disclosure_public.*_v1` 视图**
（document_units_v1 当前 42 列；32/36/41 仅是历史迁移口径，列集以
`docs/implementation/checks/contract-checklist.md` §2 为准）；
ops.pending_*_v1 队列视图只授 app 角色、只暴露事实列，
阈值由调用方从 settings 施加。视图列变更 = 契约变更，必须同步
contract 测试 + `docs/implementation/checks/contract-checklist.md`。
迁移往返核验命令见 04R §6.2（六个根路径 env 缺一会报误导性的 "No migration database URL"）。
