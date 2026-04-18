# Quanti — 公司研究与估值 Skills 仓库

> 本仓库是一个 **skills-only** 的公司研究 / 估值流水线工作区，不是完整的 AIQuantLab 生产代码。
> 历史上这个仓库承载过更广义的量化项目，现在已经裁剪聚焦到 **"以证据驱动的公司估值"** 这一条主线。

---

## 1. 目标（在做什么）

一句话目标：

> **估值 = 利润 × 质量系数**
>
> — 未来可持续的经济利润（Owner Earnings / NOPAT / FCF / ROIC），乘以一个把证据映射成估值参数的确定性系数（折现率、优势期、情景权重）。

系统要回答的三件事：

1. **未来利润是什么**（口径是经济利润，不是会计利润）
2. **我对它有多确定**（证据 → 参数，而不是主观打分）
3. **因此值多少钱**（估值区间 + 安全边际 + 敏感性）

所有结论必须**指向可追溯的证据**（evidence ledger），失败时显式写 `needs.yaml` 说明缺什么。

详细设计见 [docs/skills/MASTER_PLAN.md](docs/skills/MASTER_PLAN.md)。

---

## 2. 计划的手段（怎么做）

### 2.1 9-Skill 目标链

把整个公司研究拆成 9 个**可独立重跑的 skill**，skill 之间不互相调用，只通过"规定文件"耦合：

```text
company-foundation
  → sec-ingest-and-materialize-events
    → xbrl-parse-financial-report-events
      → recast-economic-statements
        → profit-quality-and-risk
          → growth-driver-explorer
            → moat-inferencer
              → valuation-and-margin-of-safety
                → cross-examination-audit
```

每个 skill 的职责和规格见 [docs/skills/specs/](docs/skills/specs/)。
索引与规格成熟度见 [docs/skills/README.md](docs/skills/README.md)。

### 2.2 三层数据架构（raw / events / current）

所有产物写入：

```text
/home/help/mcp/work/company_research/company/{TICKER}/
  company.yaml                # 公司身份静态信息
  latest.json                 # 指向最新 run

  raw/        # 证据镜像，不可变、可追溯、可复现（SEC 原件 + 我们生成的 meta/manifest）
  events/     # 事件层（event_id 为主键，下游 skill 直接消费，不再读 raw 复杂结构）
  current/    # 查询层：latest promoted 状态（filings_index / atlas / economic / valuation ...）
  runs/{run_id}/
    meta.yaml / result.yaml / needs.yaml / outputs/...
```

不变量：
- `raw/` **只写不删**，不在里面做研究拆解。
- `events/` 是未来数据库化的核心。财报事件是第一优先级，要能处理"正文在 EX-99.\*"的现实。
- `current/` 是下游和 UI 消费的唯一查询点。

### 2.3 Skills 不互相调用

- skill = 参数 + 规定输入文件 → 规定输出文件 + `meta/result`
- 缺依赖时 → 写 `runs/{run_id}/needs.yaml`，由编排器决定下一个跑谁
- 避免隐式耦合，保证任何一步都能单独重跑

---

## 3. 当前进度

### 3.1 真实落地的 skill（可跑的 `scripts/run.py`）

| Skill | 路径 | 状态 |
|---|---|---|
| `company-foundation` | `.agents/skills/company_research/company-foundation/` | 预留路径，当前仓库未跟踪 runner |
| `collect-company-facts` | [.agents/skills/company_research/collect-company-facts/](.agents/skills/company_research/collect-company-facts/) | 有 `SKILL.md` + `scripts/`，生成 `filings_index.yaml` 和 `raw/sec` 快照 |

其余 **7 个 skill 目前只有规格文档**（见 [docs/skills/specs/](docs/skills/specs/)），仓库里还没有对应的 `scripts/run.py`。不要把"规格已定义"误读为"代码已实现"。

### 3.2 支持设施

- [company_research_runtime/](company_research_runtime/) — 共享运行时工具（各 skill 复用，不要删）
- [CONTINUITY.md](CONTINUITY.md) — 跨会话的连续性账本（抗 compaction 的会话简报）
- [CLAUDE.md](CLAUDE.md) / [AGENTS.md](AGENTS.md) — agent 协作契约与仓库不变量
- [docs/MCP_SETUP_GUIDE.md](docs/MCP_SETUP_GUIDE.md) — MCP 工具配置指南

### 3.3 下一步优先级

1. 把 `company-foundation` 和 `collect-company-facts` 的契约固化，`filings_index` / `company.yaml` 产物稳定。
2. 启动 `xbrl-parse-financial-report-events`（Statement Atlas）—— 利润事实底座。
3. 然后是 `recast-economic-statements`（Owner Earnings / NOPAT / ROIC），打通到估值前的最后一步。
4. 估值链 `valuation-and-margin-of-safety` 先跑 demo / 占位，等上游数据到位后换真数据。

---

## 4. 仓库结构（真实情况）

```text
quanti/
  CLAUDE.md                         # Claude Code 专用操作契约
  AGENTS.md                         # 跨 agent 不变量与安全规则
  CONTINUITY.md                     # 会话连续性账本
  README.md                         # 本文件

  .agents/
    skills/company_research/
      company-foundation/           # 预留路径（当前未跟踪 runner）
      collect-company-facts/        # 实际存在（SKILL.md + scripts/）

  .codex/
    config.toml                     # 项目级 Codex / MCP 配置

  ~/.codex/skills/                  # 官方/本机安装 skill（不在仓库内版本管理）

  company_research_runtime/         # 各 skill 共享的运行时工具

  docs/
    MCP_SETUP_GUIDE.md
    skills/
      MASTER_PLAN.md                # 核心公式 / 设计原则 / 产物 schema
      README.md                     # 9-skill 索引 + 当前实现状态
      specs/skill1..skill9*.md      # 每个 skill 的详细规格
      references/                   # SEC/XBRL 技术参考
      archive/                      # Phase 1 历史实现笔记（仅参考）

  tools/
  requirements.txt
```

生产输出**不**写到仓库，统一落到 `/home/help/mcp/work/company_research/`（见 §2.2）。

---

## 5. 如何运行

### 5.1 环境

建议 Python 3.10+：

```bash
pip install -r requirements.txt
```

### 5.2 当前仓库实际可跑的命令

```bash
# SEC 证据池（filings_index + raw/sec 快照）
python .agents/skills/company_research/collect-company-facts/scripts/run.py AAPL
```

常用 flag（skill 规格中约定的通用开关）：
- `--demo`：离线 demo 模式（内置 demo 负载）
- `--force-refresh`：即使 `current/` 看起来已完整也重算
- `--as-of YYYY-MM-DD`：锁定快照日期（默认今天）

skill 被阻塞时会在 `runs/{run_id}/needs.yaml` 里说明缺的上游依赖，不会抛脏数据到 `current/`。

---

## 6. MCP 依赖

本仓库的 skill 链依赖一组 MCP 服务（SEC EDGAR、yfinance、alpaca、trading_mcp 等）。完整清单和安装/环境变量说明在 [docs/MCP_SETUP_GUIDE.md](docs/MCP_SETUP_GUIDE.md)。

Phase 1 技能链 → MCP 映射：

- `company-foundation` → `sec_edgar_mcp, alpaca, trading_mcp, yfinance`
- `collect-company-facts` → `sec_edgar_mcp, fs`
- `extract-xbrl-timeseries` → `sec_edgar_mcp, fs`
- `recast-economic-statements` → `fs`
- `valuation-and-margin-of-safety` → `fs, yfinance, alpaca`

---

## 7. WSL 下挂载 Space/NAS 共享（可选）

如果在 WSL 下工作并且需要访问 Windows 共享，一次性准备：

```bash
sudo apt-get update && sudo apt-get install -y cifs-utils
sudo mkdir -p /mnt/space/forbid
sudo tee /etc/cifs-space-cred >/dev/null <<'EOF'
domain=space
username=bsshare
password=YOUR_PASSWORD_HERE
EOF
sudo chmod 600 /etc/cifs-space-cred
```

每次 WSL/Windows 重启后挂载：

```bash
sudo umount /mnt/space/forbid 2>/dev/null || true
sudo mount -t cifs //space/forbid /mnt/space/forbid \
  -o credentials=/etc/cifs-space-cred,vers=3.0,sec=ntlmssp,iocharset=utf8,uid=$(id -u),gid=$(id -g)

export NAS_BASE_PATH=/mnt/space/forbid
```

如果 `mount` 报协议/认证错误，可把 `vers=3.0` 改为 `vers=2.1`，或查看 `dmesg | tail` 的 CIFS 日志。

---

## 8. 文档导航

- [docs/skills/MASTER_PLAN.md](docs/skills/MASTER_PLAN.md) — 架构、核心公式、产物 schema（权威）
- [docs/skills/README.md](docs/skills/README.md) — 9-skill 索引与实现状态
- [docs/skills/specs/](docs/skills/specs/) — 每个 skill 的详细规格
- [docs/skills/references/SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md](docs/skills/references/SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md) — SEC/XBRL 技术参考
- [CLAUDE.md](CLAUDE.md) / [AGENTS.md](AGENTS.md) — agent 协作与安全规则
- [CONTINUITY.md](CONTINUITY.md) — 当前会话状态

> 如果本 README 与文件系统、`docs/skills/MASTER_PLAN.md` 冲突，以 **真实仓库状态 / MASTER_PLAN** 为准。
