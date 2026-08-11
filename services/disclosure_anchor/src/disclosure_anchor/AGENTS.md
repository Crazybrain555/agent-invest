# src/disclosure_anchor — 代码层地图

分层与依赖方向（只能向内依赖，adapter 不得被 domain/application 引用）：

```text
domain/        实体、ids（ULID+前缀）、typed errors、value objects（无任何 IO/框架依赖）
application/   ports（抽象接口）+ services（SubjectResolver/register_document 核心；
               ProviderDocument admission + thin outline/table/Unit projection）
               + use_cases（RegisterLocalPdf/ParseDocument/BuildUnits/PublishRun/Sync…，经 UoW 编排）
               + worker（run_once 各阶段编排 + 队列查询单一定义）
adapters/      db/postgres（迁移/模型/仓储/UoW）、parsers/mineru_medium（官方 3.4.4
               Hybrid-medium 产物读取与 sole-writer adapter）、retrieval（jieba 钉版分词）、
               sources/cninfo（API/web 双通道）、storage、runtime(doctor)、publisher（预留空包）
cli/           db.py(bootstrap) / doctor.py / pipeline.py / worker.py（once|loop，单例锁在专用 NullPool 连接上，拿不到锁打印 [skip] 并退出 0）
               / export_contracts.py（只导出 OpenAPI + public model）
api/           Filing API（milestone 06 已实现：documents/units/filings/changes/health 读侧 + admin 写侧）
settings.py    fail-closed pydantic settings（缺 env 即抛错；env_file=None 不自动读 .env）
main.py        create_app：快检 preflight + 进程级单 engine
```

找东西的入口：

- 业务契约与硬边界：`docs/architecture/service-purpose.md`（canonical）
- 当前实施地图：`docs/implementation/README.md`；结构与检索设计见
  `docs/implementation/design/document-outline-and-toc.md`、
  `retrieval-and-semantic-keys.md` 和 `mineru-medium-greenfield.md`
- 每个子目录有自己的 AGENTS.md（就近优先），细节看那里
- 验收/测试政策：`docs/implementation/checks/`

硬规则速记：原始 PDF 不可变只追加；已应用迁移一律冻结（随时以 alembic head 为准，新改动开新迁移，
冻结策略与增量速记见 `adapters/db/postgres/AGENTS.md`）；
public 视图 `disclosure_public.*_v1` 是唯一读契约；凭据只从环境变量进 settings。
