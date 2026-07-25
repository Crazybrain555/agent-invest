# tests — 分层、门控与运行方式

框架是 **unittest**（`make test` 用 unittest discover；.venv 无 pytest，禁止引入 pytest）。

```text
unit/          无 DB 无外部依赖；_fakes.py 提供内存仓储 + FakeUnitOfWork
contract/      schema/fixture 契约（phase00 legacy-v2 golden fixtures + v2/v3 schemas）
sample_corpus/ 本地样本存在性/形状检查
integration/   DB-gated：`make test-integration` 从本机集群创建/迁移/销毁一个 suite-level
               `invest_engine_itest_*` 数据库，加载 tracked 分类规则，并覆盖子进程全部
               DB/file-root/cache 环境；默认把 MinerU binary 指向不存在的 scratch 路径，
               并移除远端 backend/server，不占生产 GPU。
               _support.engine_or_skip() 只认 runner 注入且带 marker 的
               DISCLOSURE_TEST_DATABASE_URL；绝不回退到生产 DATABASE_URL。
integration/smoke_real_mineru.py  真 MinerU 全链冒烟，**不进默认发现**（无 test_ 前缀），
               显式跑：make test-mineru-smoke（同样使用 disposable DB/产物根；
               `--real-mineru` 有意识复用本机 MinerU/server/model cache）
```

运行：`make test`（无 DB 门禁）/ `make test-unit|test-contract|test-data|test-integration`；
**`make agent-check`** = lint(ruff) + typecheck(mypy 全仓严格) + no-DB test + git diff --check
——AI 改完代码的默认提交门禁（06 起写入 milestone 协议）。
绿判据：no-DB 模式末行 `OK (skipped=N)`；live-DB 模式 `OK`——若未设 DISCLOSURE_MINERU_BIN
则为 `OK (skipped=1)`（06 的 admin 全链测试三重门控 skip，属合法绿；配上 MINERU_BIN 才是零 skip）。
（socket DSN 见 04R §6.3，免密码）。政策与 fixture 规范：
`docs/implementation/checks/fixture-and-test-policy.md`；再生成协议：04R §6.4。
集成测试不得写 `invest_engine`；需要 DB 的测试只能经 scratch runner 或显式、带 marker 的
`DISCLOSURE_TEST_DATABASE_URL`。测试内 tearDown 仍负责相互隔离，但不是生产安全边界。
Parser/builder 回归不能只固定一个 provider_document_id。行为变化至少需要：命中该 failure family 的
代表性正例、相邻但不应命中的负例，以及按 filing type/issuer 分层的确定性 corpus replay；断言来源
slice 守恒、边界和输出语义，不以某一 golden 的 unit 数恰好相等代替泛化验证。
补回归前先审计相邻测试组合，优先删除、合并或重写过期/重复断言，再增加最小正负例；历史 AI
fixture、旧实现预期或测试数量本身都不是保留理由，确认无冗余时才原样保留。
本原则由**棘轮账本机械执行**（quality-ratchet CI 模式，用户 2026-07-16 确认）：`agent-check`
跑 `scripts/audit_test_composition.py`，每文件测试数或 src 私有符号导入超过
`tests/composition_ledger.json` 即门禁红；完成组合审计后用 `--update` 有意识刷新账本，
缩减自动通过。
**会提交事务、消费全局队列或重建 projection 的测试必须留在 suite scratch database**；
不得通过停 worker、同库 marker、外层 rollback 或 advisory lock 代替数据库隔离。runner
普通退出/异常会精确 DROP；父 runner 在 maintenance DB 持有 session advisory lease：
SIGKILL 会自动释放，下一轮只回收严格名称/精确 marker 且租约已释放的库；无 marker 的
CREATE 窗口残留另加 TTL。中断升级始终信号整个 unittest 进程组。
