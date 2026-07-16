# asset_intake - Agent Operating Contract

L1 轻资产登记服务：dataset_snapshot（结构化数据查询结果）+ tool_result（工具返回登记），统一出口
`data_asset` 信封。语义权威：根 `../../docs/reference/投研预测引擎顶层框架协议_v0.8.md`
（§3.2 信封、§3.7 payload 契约、§3.9 查空、§2.8 change feed、§3.11 对外契约）。跨服务规矩见根
`AGENTS.md`；蓝图（只读参考）是 `../disclosure_anchor/`。

Keep this file thin: norms + pointers. Follow `../../docs/agent-workflow.md` when a root durable trigger applies;
service work uses gitignored `docs/agent/HANDOFF.md`, cross-repo work uses `../../docs/agent/HANDOFF.md`, and never
both as gate holders. Legacy task files are read-only history.

## Hard rules

1. **DB 边界**：只创建/触碰 database `invest_engine` 内的 `intake_core` / `intake_public` /
   `intake_ops` schema 与 `intake_owner` / `intake_app` / `intake_reader` 角色；`disclosure_*`
   对象绝对只读且不进任何 migration。跨服务只经 `*_v1` public views 消费。
2. **信封复用**：信封字段模型、kind 矩阵、`asset://` URI、契约校验一律来自共享包
   `envelope-kernel`（`make venv` 以相对路径可编辑安装），不得在本服务重复定义。
3. **provider 端口**：信封登记、幂等去重、outbox、public views 只依赖 provider 端口
   （port/interface），不依赖具体 provider；端口须容纳 HTTP API 型与 SQL 直查型实现而不改表、
   不改契约，且不写死只剩这两种。
4. 凭证只走环境变量（`.env.template` 占位符）；运行时数据、原始返回留存都在
   `/Volumes/AgentSSD/agent_system/services/asset_intake/`，never in the repo。
5. unittest only（no pytest）；真实 provider 调用只在显式 opt-in 的 smoke 测试里，缺凭证时 skip。
6. 需要提交时只暂存明确路径，避免把工作树中的无关改动带入提交；commit/push 授权服从根规则。

## Validation

`make agent-check`（ruff + mypy + no-DB unittest + `git diff --check`）；live-DB 测试另跑
`make test`（需 `ASSET_INTAKE_DATABASE_URL` / `ASSET_INTAKE_MIGRATION_DATABASE_URL`，socket DSN 形如
`postgresql+psycopg://<role>@/invest_engine?host=/Volumes/AgentSSD/agent_system/postgres/sockets&port=55432`）。
Live-DB 验证必须附带 `disclosure_*` 对象零变化断言。

修改 provider、registry、登记/public-view、envelope 或 provenance 行为前，先检查真实 payload/schema；
库可用时再用只读 DBHub/SQL 校准 `intake_public.*_v1` 的 schema、active 数据及跨 provider/kind 分布。
已发布旧产物和历史测试都不是单独的语义权威，差异须回到顶层协议、source payload 和信封身份。
根级“前置设计研究门”适用于每一次 provider、registry、登记、public-view、envelope 或 provenance
行为修改，即使问题看似简单、熟悉或已有本地测试；先检查真实样本并做有界外部校验，再设计修改，
难度只改变调研深度：
供应商/库契约查官方文档或 Context7，成熟实现查 DeepWiki/GitHub/web（1–2 个），并公开研究问题及
采纳/否决的不变量。只有格式、拼写、纯机械重命名等设计中性的机械修改可说明理由后跳过。结论须与
`envelope-kernel` 和代表性 dataset_snapshot/tool_result 交叉验证并固化为正反测试，
不得把单一 provider 样本或外部内部结构写进核心契约，也不得为只读诊断改写共享运行时。

## Layout

```text
src/asset_intake/settings.py            环境配置（唯一读 env 的模块）
src/asset_intake/cli/                   doctor / db create / export_contracts
src/asset_intake/db/                    schema 常量、bootstrap、models、migrations（intake_* only）
src/asset_intake/providers/             端口(port.py)、双层配置加载校验(registry.py)、SQL 白名单(sql_template.py)
src/asset_intake/application/           registrar（唯一写 DB 的地方）
src/asset_intake/domain/errors.py       §3.11 错误码
src/asset_intake/contracts.py           公共契约定义与导出（make export-contracts,禁止手写 contracts/）
registry/datasets/*.yaml                语义数据集契约（v1.2 F4）
registry/providers/*.catalog.yaml       provider 物理 catalog（候选表 + activation,F12 fail-fast）
contracts/                              导出物,契约测试守护 byte-for-byte
tests/unit|contract|integration/        no-DB / 契约 / live-DB(缺 DSN skip)
```
