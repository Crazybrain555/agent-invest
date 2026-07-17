# src/disclosure_anchor — 代码层地图

分层与依赖方向（只能向内依赖，adapter 不得被 domain/application 引用）：

```text
domain/        实体、ids（ULID+前缀）、typed errors、value objects（无任何 IO/框架依赖）
application/   ports（抽象接口）+ services（SubjectResolver/register_document 核心）
               + use_cases（RegisterLocalPdf/ParseDocument/BuildUnits/PublishRun/Sync…，经 UoW 编排）
               + worker（run_once 各阶段编排 + 队列查询单一定义）
adapters/      db/postgres（迁移/模型/仓储/UoW）、parsers/mineru、unit_builder（S1–S8 切分规则 +
               toc_outline 目录解析）、retrieval（jieba 钉扳分词 + 查询同义扩展）、
               sources/cninfo（API/web 双通道）、storage、runtime(doctor)、publisher（预留空包）
cli/           db.py(bootstrap) / doctor.py / pipeline.py / worker.py（once|loop，单例锁在专用 NullPool 连接上，拿不到锁打印 [skip] 并退出 0）
               / export_contracts.py（OpenAPI + public model schema 冻结导出）
api/           Filing API（milestone 06 已实现：documents/units/filings/changes/health 读侧 + admin 写侧）
settings.py    fail-closed pydantic settings（缺 env 即抛错；env_file=None 不自动读 .env）
main.py        create_app：快检 preflight + 进程级单 engine
```

找东西的入口：

- 业务契约与硬边界：`docs/architecture/service-purpose.md`（canonical）
- 实施规格：`docs/implementation/milestones/`（00–08 与 06R 检索投影均已完成）；标题仲裁/脉络与目录/检索词表的设计权威在 `docs/implementation/design/`
- 每个子目录有自己的 AGENTS.md（就近优先），细节看那里
- 验收/测试政策：`docs/implementation/checks/`

硬规则速记：原始 PDF 不可变只追加；已应用迁移一律冻结（随时以 alembic head 为准，新改动开新迁移，
冻结策略与增量速记见 `adapters/db/postgres/AGENTS.md`）；
public 视图 `disclosure_public.*_v1` 是唯一读契约；凭据只从环境变量进 settings。
