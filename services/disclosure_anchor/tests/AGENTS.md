# tests — 分层、门控与运行方式

框架是 **unittest**（`make test` 用 unittest discover；.venv 无 pytest，禁止引入 pytest）。

```text
unit/          无 DB 无外部依赖；_fakes.py 提供内存仓储 + FakeUnitOfWork
contract/      schema/fixture 契约（phase00 legacy-v2 golden fixtures + v2/v3 schemas）
sample_corpus/ 本地样本存在性/形状检查
integration/   DB-gated：_support.engine_or_skip() 只认
               DISCLOSURE_MIGRATION_DATABASE_URL / DATABASE_URL（缺则 skip，套件保持绿）；
               同时对共享真库加进程级 advisory lock(815003)——并发套件自动排队，
               不会互相打架（VSCode 面板与终端同时跑也安全）
integration/smoke_real_mineru.py  真 MinerU 全链冒烟，**不进默认发现**（无 test_ 前缀），
               显式跑：make test-mineru-smoke（三重门控：DB + DISCLOSURE_MINERU_BIN + 样本 PDF）
```

运行：`make test`（全量）/ `make test-unit|test-contract|test-data|test-integration`；
**`make agent-check`** = lint(ruff) + typecheck(mypy 全仓严格) + no-DB test + git diff --check
——AI 改完代码的默认提交门禁（06 起写入 milestone 协议）。
绿判据：no-DB 模式末行 `OK (skipped=N)`；live-DB 模式 `OK`——若未设 DISCLOSURE_MINERU_BIN
则为 `OK (skipped=1)`（06 的 admin 全链测试三重门控 skip，属合法绿；配上 MINERU_BIN 才是零 skip）。
（socket DSN 见 04R §6.3，免密码）。政策与 fixture 规范：
`docs/implementation/checks/fixture-and-test-policy.md`；再生成协议：04R §6.4。
集成测试写真库必须在 tearDown 清理自己的行；固定标识要选可清理、不与生产样本冲突的值
（run-unique 后缀，参考 test_public_views_content 的 T{ulid} 模式）。
Parser/builder 回归不能只固定一个 provider_document_id。行为变化至少需要：命中该 failure family 的
代表性正例、相邻但不应命中的负例，以及按 filing type/issuer 分层的确定性 corpus replay；断言来源
slice 守恒、边界和输出语义，不以某一 golden 的 unit 数恰好相等代替泛化验证。
补回归前先审计相邻测试组合，优先删除、合并或重写过期/重复断言，再增加最小正负例；历史 AI
fixture、旧实现预期或测试数量本身都不是保留理由，确认无冗余时才原样保留。
**会消费全局队列的测试（worker run_once 类）必须建独立 scratch database**
（模式见 test_worker_integration.setUpClass：CREATE DATABASE + ensure_schemas + alembic 子进程），
共享真库上跑 run_once 会把真实 pending 文档灌进 fake——已实测踩过。
