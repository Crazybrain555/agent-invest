# agent-invest

投研预测引擎（L1–L6）monorepo。架构由《投研预测引擎顶层框架协议 v0.7》锁定：
`docs/reference/投研预测引擎顶层框架协议_v0.7修订最终版本.md`。

## 布局

| 路径 | 说明 |
|---|---|
| `services/disclosure_anchor/` | L1 披露公告/财报 PDF 路径服务（live），也是后续服务的复用蓝图 |
| `docs/archive/pre-restart/` | Quant_agent 时代冻结存档（协议 v0.4/v0.5、旧 harness 文档），仅作历史证据 |
| `packages/`（规划中） | 跨服务共享代码：信封模型、契约测试工具 |
| `services/asset_intake/`（规划中） | L1 标准数据 + 工具结果轻登记服务 |
| `services/upload_service/`（规划中） | L1 人工上传服务（独立服务，最后立项） |

## 约定

- 跨服务规范见根 `AGENTS.md`；服务内规范以各服务自己的 `AGENTS.md` 为准（就近优先）。
- 一个 PostgreSQL 集群（AgentSSD `pg18-main`），服务按 schema + 角色隔离，跨服务只读 `*_v1` public 视图。
- 运行时数据、原始文件、模型缓存都在 `/Volumes/AgentSSD/agent_system/`，不进仓库。

## 校验

```bash
make agent-check   # 逐服务跑 ruff + mypy + no-DB 测试 + git diff --check
make test          # 逐服务跑测试（live-DB 部分见各服务文档）
```

本仓库由旧仓库 Quant_agent 改名延续而来；disclosure_anchor 的独立仓库历史已经由
`git subtree` 完整并入 `services/disclosure_anchor/`。
