---
id: disclosure_anchor_milestone_01_code-skeleton-and-config
project: disclosure_anchor
title: 代码骨架与配置
status: complete
created_at: 2026-06-26
---

# Milestone 01: 代码骨架与配置

## 1. 目标

建立可维护的模块化单体代码骨架，让后续实现都落在正确层级；完成配置、路径注入、doctor 基础检查和最小 app 启动。

## 2. 范围

范围内：

- 创建 repo 目录结构。
- 创建 `pyproject.toml`、`Makefile`、`.env.example`。
- 实现 `settings.py`。
- 实现 `FileStorePathBuilder`。
- 实现 `doctor` 基础检查。
- 实现 domain ids、value objects、基础错误类型。
- 建立空 FastAPI app 和 CLI 入口。


## 3. 实施细则

1. 按 `001` 创建目录结构。
2. `settings.py` 使用 Pydantic Settings，支持：

```text
DISCLOSURE_DATA_ROOT
DISCLOSURE_SHARED_ROOT
DISCLOSURE_RUNTIME_ROOT
DATABASE_URL
MINERU_MODEL_CACHE
HF_HOME
MODELSCOPE_CACHE
CNINFO_ACCESS_KEY
CNINFO_ACCESS_SECRET
CNINFO_ACCESS_TOKEN
```

CNINFO 配置在 Phase 01 只定义 settings 字段和示例占位，不实现 client，也不要求变量非空。
真实凭据不得放在 repo-local `.env`、`.env.example`、docs、测试 fixture 或日志中；推荐放在
`~/.config/disclosure_anchor/cninfo.env`、shell 环境、Keychain 或外置盘私有 config 中，由运行命令加载为环境变量。
`DATABASE_URL` 同样只从环境变量注入；本地开发连接姿态为 Homebrew PostgreSQL 18 / AgentSSD
`pg18-main`，`127.0.0.1:55432` localhost-only TCP（也保留 AgentSSD Unix socket）。`.env.example` 只能写
占位符，不得写入本机开发密码。

3. `FileStorePathBuilder` 提供：

```text
raw_document_relpath(...)
parser_artifacts_root_relpath(...)
normalized_ir_relpath(...)
document_units_snapshot_relpath(...)
runtime_tmp_path(...)
```

4. `doctor` 检查：

```text
外置盘是否挂载
sentinel 是否存在
DATA_ROOT 是否可写
runtime 目录是否可写
模型缓存是否指向外置盘
```

5. FastAPI app 只实现 `/v1/health`。
6. CLI 实现 `python -m disclosure_anchor.cli.doctor`。
7. Makefile 实现：

```bash
make doctor
make test-unit
make api
```


## 4. 检查点

- `make doctor` 可运行。
- 外置盘未挂载时 doctor 失败。
- `DATABASE_URL` 由环境变量提供，示例配置不包含真实密码。
- `PathBuilder` 只返回相对路径或受控 runtime path。
- repo 中没有硬编码 `/Volumes/AgentSSD` 的业务逻辑；只允许 `.env.example`、docs 出现。
- repo 中没有真实 CNINFO 凭据；`.env.example` 只出现变量名和占位符。
- `/v1/health` 可返回 ok。
- unit tests 覆盖 settings、PathBuilder、ids。


## 5. Definition of Done

- 代码骨架完整。
- `make doctor` 和 `make test-unit` 通过。
- 空 app 可启动。
- 后续 DB / storage / parser adapter 有明确落位。


## 6. 明确不做

- 不建数据库表。
- 不实现 CNINFO。
- 不调用 MinerU。
- 不做业务查询 API。


## 7. 交付给下一阶段

- repo 骨架。
- settings / path builder / doctor。
- 空 FastAPI app。
- Makefile 基础命令。


## 8. 常见失败与处理

- 配置读取混乱：优先修 settings，不让 use case 读取环境变量。
- 路径手写扩散：立即收敛到 `FileStorePathBuilder`。
- doctor 误创建内置盘目录：视为严重错误，修正 fail closed。


## 9. Phase 01 实施记录

2026-06-29 已实现 Phase 01 骨架：

- 新增 `pyproject.toml`、`Makefile`、`.env.example`、`config/`、`contracts/`、`src/disclosure_anchor/`
  和 `tests/unit/`。
- `settings.py` 只从环境变量读取 `DISCLOSURE_*`、`DATABASE_URL`、模型缓存和 CNINFO 凭据字段。
- `FileStorePathBuilder` 集中生成 raw/parser/normalized_ir/document_units 相对路径和受控 runtime tmp 路径。
- `doctor` 检查 AgentSSD agent_system root、sentinel、data/shared/runtime 可写性和模型缓存归属；不自动创建或修复目录。
- `make api` 使用 uvicorn app factory，启动时运行 settings + doctor preflight，缺失外置盘/环境时 fail closed。
- `/v1/health` 返回 `{"status":"ok","service":"disclosure_anchor","version":"0.1.0"}`。
- 单元测试覆盖 settings、PathBuilder、ids、doctor、app startup 和 health payload。

已验证：

- `make test-unit` 通过（14 tests）。
- `make doctor` 在真实 AgentSSD 环境变量下通过；沙盒内因外置盘写权限边界会失败，已用 real filesystem check 复核。
- `make api` 无运行环境变量时拒绝启动；带真实 AgentSSD 环境变量时可启动，`GET /v1/health` 返回 ok。
- `src/`、`tests/unit/`、`Makefile`、`pyproject.toml`、`config/`、`contracts/`、`README.md` 无
  `/Volumes/AgentSSD` 硬编码；只有 `.env.example` 保留示例路径。
- Phase 01 文件未匹配真实 `DATABASE_URL` 密码或 CNINFO 凭据模式。
- 独立 reviewer gate 通过：no material findings，overall verdict pass，confidence high。
