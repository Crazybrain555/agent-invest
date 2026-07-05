# asset_intake

投研预测引擎 L1 轻资产登记服务：接收 dataset_snapshot（Tushare 等结构化数据查询结果，优先）与
tool_result（web/MCP/工具返回登记），统一出口为协议 §3.2 的 `data_asset` 信封（经共享包
`envelope-kernel`）。复用 disclosure_anchor 蓝图：稳定键列化 + jsonb payload、`intake_public.*_v1`
只读视图、outbox change feed、processing_run/source_access、角色最小权限。

数据库：共享集群（AgentSSD pg18-main）内 database `invest_engine` 的 `intake_core` /
`intake_public` / `intake_ops` schema；运行时数据在 `/Volumes/AgentSSD/agent_system/services/asset_intake/`。

```bash
make venv          # 建 .venv 并安装（含 ../../packages/envelope_kernel 路径依赖）
make doctor        # 环境体检
make agent-check   # ruff + mypy + no-DB 测试 + git diff --check
make test          # 设好 ASSET_INTAKE_*_DATABASE_URL 时含 live-DB 测试
```

规范见本目录 `AGENTS.md`；引擎协议在根 `docs/reference/`。
