# src/disclosure_anchor — 代码层地图

分层与依赖方向（只能向内依赖，adapter 不得被 domain/application 引用）：

```text
domain/        实体、ids（ULID+前缀）、typed errors、value objects（无任何 IO/框架依赖）
application/   ports（抽象接口）+ services（SubjectResolver/register_document 核心）
               + use_cases（RegisterLocalPdf/ParseDocument…，经 UoW 编排）
adapters/      db/postgres（迁移/模型/仓储/UoW）、parsers/mineru、storage、runtime(doctor)
cli/           db.py(bootstrap) / doctor.py；pipeline.py 到 milestone 05 才创建
api/           FastAPI 骨架（Filing API 到 milestone 06 才实现）
settings.py    fail-closed pydantic settings（缺 env 即抛错；env_file=None 不自动读 .env）
main.py        create_app：快检 preflight + 进程级单 engine
```

找东西的入口：

- 业务契约与硬边界：`docs/architecture/service-purpose.md`（canonical）
- 当前实施规格：`docs/implementation/milestones/`（04R 已完成；05–08 待做）
- 每个子目录有自己的 AGENTS.md（就近优先），细节看那里
- 验收/测试政策：`docs/implementation/checks/`

硬规则速记：原始 PDF 不可变只追加；迁移 0001–0007 冻结（新改动开新迁移）；
public 视图 `disclosure_public.*_v1` 是唯一读契约；凭据只从环境变量进 settings。
