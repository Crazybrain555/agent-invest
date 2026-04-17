# MCP 安装与配置指南

本文档面向在新机器上 clone 本项目后，需要配置 MCP server 的开发者。

官方参考（最新版）：
- OpenAI Codex MCP: https://developers.openai.com/codex/mcp/
- OpenAI Codex Config: https://developers.openai.com/codex/config
- Anthropic Claude Code MCP: https://docs.anthropic.com/en/docs/claude-code/mcp

---

## 1. 概览

本项目的 AI 编码 agent（Claude Code / OpenAI Codex）依赖一组 MCP server 来访问金融数据、SEC 文件、学术文献、搜索引擎等外部能力。

**配置文件位置：**

| 文件 | 格式 | 作用 | 谁读 |
|------|------|------|------|
| `~/.claude.json` | JSON | **用户级 MCP 声明（Claude Code 实际读取的文件）** | Claude Code |
| `~/.claude/settings.json` | JSON | Claude Code 偏好设置（model、plugins 等），**不是** MCP 配置的主文件 | Claude Code |
| `.mcp.json` | JSON | 项目级 MCP 声明（可选，会覆盖同名用户级配置） | Claude Code |
| `.codex/config.toml` | TOML | 项目级 MCP 声明 | OpenAI Codex |
| `~/.codex/config.toml` | TOML | 用户级 MCP 声明 | OpenAI Codex |
| `.env.template` | shell | 环境变量参考模板 | 人 |

> **重要区分**：Claude Code 有两个容易混淆的配置文件：
> - `~/.claude.json` — 运行时状态 + **MCP server 定义**（`mcpServers` 字段在这里）
> - `~/.claude/settings.json` — 偏好设置（model、plugins、alwaysThinkingEnabled 等）
>
> 手动编辑 `~/.claude/settings.json` 中的 `mcpServers` **不会生效**。必须通过 `claude mcp add-json` 命令操作 `~/.claude.json`，或者直接编辑 `~/.claude.json`。

路径是当前开发机的绝对路径，新机器需要调整。

---

## 2. MCP 全量清单

| 名称 | 用途 | 运行时 | 安装方式 | Phase 1 必需 |
|------|------|--------|---------|-------------|
| `sec_edgar_mcp` | SEC EDGAR 文件检索 | Python (conda) | `pip install sec-edgar-mcp`（在 aiquantlab 环境中） | **必需** |
| `alpaca` | Alpaca 市场数据/纸盘交易 | Python (venv) | `pip install alpaca-mcp-server` 或 clone 后 `pip install -e .` | **必需** |
| `trading_mcp` | Finviz 股票筛选/基本面指标 | Node.js | clone [trading-mcp](https://github.com/netanelavr/trading-mcp) → `npm install && npm run build` | **必需** |
| `yfinance` | Yahoo Finance 数据 | Python (uv) | clone [yahoo-finance-mcp](https://github.com/Alex2Yang97/yahoo-finance-mcp) → `uv sync` | **必需** |
| `fs` | MCP 文件系统访问 | Node.js (npx) | 无需安装，`npx -y @modelcontextprotocol/server-filesystem <路径>` | **必需** |
| `context7` | 库文档实时查询 | HTTP | 无需安装，直接连 `https://mcp.context7.com/mcp` | 可选 |
| `fetch` | 通用网页抓取 | Python (venv) | `pip install mcp-server-fetch` | 可选 |
| `rss` | RSS 新闻订阅 | Node.js | clone [rss-mcp](https://github.com/veithly/rss-mcp) → `npm install && npm run build` | 可选 |
| `gdelt` | GDELT 全球新闻/事件 | Node.js | clone [GDELT-mcp](https://github.com/anysiteio/GDELT-mcp) → `npm install && npm run build` | 可选 |
| `search` | DuckDuckGo 搜索 | Python (venv) | `pip install mcp-search-server` | 可选 |
| `openalex` | OpenAlex 学术论文 | Node.js | clone [openalex-research-mcp](https://github.com/oksure/openalex-research-mcp) → `npm install && npm run build` | 可选 |
| `crossref` | Crossref 文献 DOI | Node.js | 本地固定安装 `@botanicastudios/crossref-mcp`（不要用 `npx -y`） | 可选 |
| `pubmed` | PubMed 医学文献 | Python (venv) | clone [PubMed-MCP-Server](https://github.com/JackKuo666/PubMed-MCP-Server) → `pip install -r requirements.txt` | 可选 |
| `arxiv` | arXiv 论文检索 | Python (uv) | `uv tool install arxiv-mcp-server` | 可选 |
| `github` | GitHub API | Node.js | 本地固定安装 `@modelcontextprotocol/server-github`（不要用 `npx -y`） | 可选 |
| `git` | 本地 Git 操作 | Python (venv) | `pip install mcp-server-git` | 可选 |
| `playwright` | 浏览器自动化 | Node.js (npx) | `npx @playwright/mcp@latest`（若无系统 Chrome，请改用 `--executable-path ~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome`） | 可选 |

---

## 3. 环境变量

### 3.1 需要 export 到 shell 的变量

把以下内容加到 `~/.bashrc`（参考 `.env.template` 中的实际值）：

```bash
# Alpaca（纸盘交易）
# 获取: https://app.alpaca.markets/ → Paper Trading → API Keys
export ALPACA_API_KEY="你的key"
export ALPACA_SECRET_KEY="你的secret"
export ALPACA_PAPER_TRADE="True"

# GitHub（可选，github MCP 需要）
# 获取: GitHub Settings → Developer settings → Personal access tokens → Fine-grained
export GITHUB_PERSONAL_ACCESS_TOKEN="你的token"

# 代理（按网络环境配置，无代理可不设）
export HTTP_PROXY="http://127.0.0.1:端口"
export HTTPS_PROXY="http://127.0.0.1:端口"
export NO_PROXY="127.0.0.1,localhost,..."
# 部分工具读小写版本，建议同时设
export http_proxy="$HTTP_PROXY"
export https_proxy="$HTTPS_PROXY"
export no_proxy="$NO_PROXY"
```

### 3.2 写在配置文件内的变量（不需要 export）

以下变量直接写在 `.codex/config.toml` 的 `[mcp_servers.xxx.env]` 或 `.mcp.json` 的 `"env"` 中，迁移时改对应值即可：

| 变量 | 所属 MCP | 说明 | 示例值 |
|------|---------|------|--------|
| `SEC_EDGAR_USER_AGENT` | sec_edgar_mcp | SEC 要求的身份标识 | `"你的名字 (你的邮箱)"` |
| `SEC_EDGAR_RATE_LIMIT` | sec_edgar_mcp | 请求速率（次/秒） | `8` |
| `SEC_EDGAR_TIMEOUT` | sec_edgar_mcp | 超时秒数 | `60` |
| `OPENALEX_EMAIL` | openalex | OpenAlex 联系邮箱 | `you@example.com` |
| `GDELT_USER_AGENT` | gdelt | GDELT 身份标识 | `"你的名字 (邮箱) gdelt-mcp"` |
| `GDELT_API_TIMEOUT` | gdelt | API 超时（毫秒） | `60000` |
| `PYMUPDF_SUGGEST_LAYOUT_ANALYZER` | arxiv | 抑制 PyMuPDF 提示 | `0` |
| `SSL_CERT_FILE` | fetch | 代理 TLS 证书链（证书校验失败时） | `/etc/ssl/certs/ca-certificates.crt` |
| `REQUESTS_CA_BUNDLE` | fetch | requests/httpx 证书链路径 | `/etc/ssl/certs/ca-certificates.crt` |
| `CURL_CA_BUNDLE` | fetch | curl 证书链路径 | `/etc/ssl/certs/ca-certificates.crt` |

---

## 4. 安装步骤（新机器）

### 4.1 前置条件

```bash
# 1. Python 环境（推荐 conda）
conda create -n aiquantlab python=3.12 -y
conda activate aiquantlab
pip install pyyaml pandas pyarrow  # 基础依赖

# 2. Node.js（推荐 nvm）
nvm install 20
nvm use 20

# 3. uv（Python 包运行器，yfinance/arxiv 需要）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 4. npx 确认可用
npx --version
```

### 4.2 安装 Phase 1 必需的 MCP（最小集）

```bash
# sec_edgar_mcp
conda activate aiquantlab
pip install sec-edgar-mcp

# alpaca
cd ~/mcp/servers
git clone https://github.com/alpacahq/alpaca-mcp-server.git
cd alpaca-mcp-server && pip install -e .

# trading_mcp
cd ~/mcp/servers
git clone https://github.com/netanelavr/trading-mcp.git
cd trading-mcp && npm install && npm run build

# yfinance
cd ~/mcp/servers
git clone https://github.com/Alex2Yang97/yahoo-finance-mcp.git
cd yahoo-finance-mcp && uv sync

# fs — 无需安装，npx 自动拉取
```

### 4.3 修改配置文件中的路径

配置文件中所有路径都是**绝对路径**，需要改成你本地的实际路径。需要修改的关键路径：

| 占位路径（当前开发机） | 你需要改成 |
|----------------------|-----------|
| `/home/help/miniconda3/envs/aiquantlab/bin/python` | 你的 conda python 路径 |
| `/home/help/mcp/tools/node/bin/node` | 你的 node 路径（`which node`） |
| `/home/help/mcp/tools/bin/npx` | 你的 npx 路径（`which npx`） |
| `/home/help/mcp/tools/uv/uv` | 你的 uv 路径（`which uv`） |
| `/home/help/mcp/servers/<name>` | 你 clone MCP server 的目录 |
| `/home/help/.venvs/<name>/bin/...` | 你的 venv 路径 |
| `/home/help/mcp/work` | 技能链输出目录（fs MCP 的 allowed dir） |
| `/home/help/mcp/data/arxiv_papers` | arXiv 论文缓存目录 |
| `/home/help/.local/bin/arxiv-mcp-server` | arxiv 可执行文件（`uv tool install` 安装位置） |
| `/home/help/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome` | Playwright Chromium 路径（版本号会变，用 `ls ~/.cache/ms-playwright/` 查找） |
| `/mnt/d/python_project/my-quant-project` | git MCP 的 `--repository` 参数（改为你的项目路径） |

**修改哪个文件取决于你用哪个 agent：**
- Claude Code → 用 `claude mcp add-json <name> '<json>' -s user` 写入 `~/.claude.json`（推荐），或手动编辑 `~/.claude.json` 的 `mcpServers` 字段
- Codex → 改 `.codex/config.toml` + 全局 `~/.codex/config.toml`

> **不要**手动编辑 `~/.claude/settings.json` 来配置 MCP — Claude Code 不从那里读 MCP 定义（详见第 1 节说明）。

---

## 5. 注意事项

### 5.1 fs MCP 不能放在项目级配置中（Claude Code）

Claude Code 会将项目级 `.mcp.json` 中的 filesystem MCP **沙箱化到项目目录**。如果你需要访问项目外的路径（如技能链输出目录 `/home/help/mcp/work`），`fs` 必须配置在全局 `~/.claude.json` 的 `mcpServers` 中，**不能**放在 `.mcp.json`。

当前 `.mcp.json` 已移除 `fs`，全局配置中保留。

### 5.2 代理变量传递

- **Codex**: 使用 `env_vars = ["HTTP_PROXY", ...]` 自动从 shell 透传，无需在 config 中写值
- **Claude Code**: 需要在 `.mcp.json` 的 `"env"` 中显式写 `"HTTP_PROXY": "${HTTP_PROXY}"`（支持 `${VAR}` 展开）
- 如果某个 MCP 的网络请求失败（如 GDELT fetch failed），优先检查该 MCP 的 env 中是否传了代理变量
- **Windows Codex App + `wsl.exe` bridge**: 这是第三种情况。`command = "wsl.exe"` + `bash -lic` 的 Node MCP，不能简单照搬 `env_vars`/`npx -y` 方案。对于走 HTTPS 的 Node `fetch`（如 `github`/`crossref`），实测会出现 `UND_ERR_CONNECT_TIMEOUT`；优先用本地固定安装 + 代理 launcher，不要用浮动 `npx -y`

### 5.3 项目级配置覆盖全局配置

**同名 MCP server，项目级会覆盖全局**（不是合并）。这意味着：
- 如果 `.mcp.json` 和 `~/.claude.json` 的 `mcpServers` 都定义了 `gdelt`，项目级的生效
- 如果项目级的配置缺少某些字段（如 env），全局的那些字段不会补上

建议：项目级配置要写完整，不要依赖全局补齐。

### 5.4 Codex trust 机制

Codex 需要在全局 `~/.codex/config.toml` 中标记项目为 trusted 才会加载项目级配置：

```toml
[projects."/你的项目绝对路径"]
trust_level = "trusted"
```

### 5.5 验证 MCP 可用性

```bash
# Claude Code — 进入项目目录后
claude mcp list

# Codex — 进入项目目录后
codex mcp list
codex mcp get <server_name>

# 通用：检查关键二进制路径是否存在
which python node npx uv
ls -l /path/to/your/mcp/servers/*/dist/index.js
```

### 5.6 GDELT 目录名是 GDELT-mcp（不是 mcp-gdelt）

历史遗留问题：部分旧配置中 GDELT 目录名写成了 `mcp-gdelt`，实际目录名是 `GDELT-mcp`。已在项目配置和全局配置中统一修正。

### 5.7 GDELT 连接排障（已验证的排查路径）

`gdelt` server 本地启动成功不代表上游 API 一定可达。若工具调用报 `fetch failed` / `UND_ERR_CONNECT_TIMEOUT`，按以下顺序排查：

**Step 1: 确认 DNS 解析**
```bash
getent ahosts api.gdeltproject.org
# 预期输出只有 IPv4（如 104.197.47.124），GDELT 无 AAAA (IPv6) 记录
```

**Step 2: 分别测试 HTTPS 和 HTTP**
```bash
# HTTPS（GDELT 的 TLS 端口历史上有间歇性故障）
curl -4 -m 15 -v "https://api.gdeltproject.org/api/v2/doc/doc?query=apple&mode=ArtList&maxrecords=1&format=json" 2>&1 | head -30

# HTTP（GDELT 官方支持 HTTP，参考 https://blog.gdeltproject.org/https-now-available-for-selected-gdelt-apis-and-services/）
curl -4 -m 15 -v "http://api.gdeltproject.org/api/v2/doc/doc?query=apple&mode=ArtList&maxrecords=1&format=json" 2>&1 | head -30
```

**Step 3: 隔离代理影响**
```bash
# 绕过代理直连
curl -4 -m 15 --noproxy '*' "http://api.gdeltproject.org/api/v2/doc/doc?query=apple&mode=ArtList&maxrecords=1&format=json"

# 走代理
curl -4 -m 15 -x http://127.0.0.1:10808 "http://api.gdeltproject.org/api/v2/doc/doc?query=apple&mode=ArtList&maxrecords=1&format=json"
```

**已知问题与解决方案（2026-03 实测）：**

| 现象 | 根因 | 修复 |
|------|------|------|
| HTTPS 直连超时，但 `curl -x proxy https://...` 返回 200 | 本地网络到 `api.gdeltproject.org:443` 直连不稳定；代理路径可用 | 保持 `GDELT-mcp` 使用 upstream `https://` 端点，不要再强制改 `http://`；同时像 `github/crossref` 一样通过 `launch_with_proxy.mjs` 启动，让 Node `fetch()` 真正走代理 |
| `curl` 走代理 HTTPS 能通，但 Node `fetch()` 仍报 `UND_ERR_CONNECT_TIMEOUT` | Node 原生 `fetch()` 没有正确套用环境代理 | 不要只设 `HTTP_PROXY` / `NODE_USE_ENV_PROXY`；要用 `undici.setGlobalDispatcher(new ProxyAgent(...))` 的 launcher 包一层 |
| HTTP 返回 `429 Too Many Requests` | GDELT DOC HTTP 端点当前更容易触发限流 | 优先尝试 HTTPS + proxy launcher；如果仍遇到 429，调用间隔至少 5 秒，必要时更换代理节点 |

> **提示**: GDELT DNS 仅解析到 IPv4，不存在 IPv6 路由问题。不需要加 `NODE_OPTIONS=--dns-result-order=ipv4first`。
>
> **2026-03-23 当前机器复核：**
> - `curl --noproxy '*' https://api.gdeltproject.org/...` -> 超时
> - `curl -x http://127.0.0.1:10808 https://api.gdeltproject.org/...` -> `200 OK`
> - Node 原生 `fetch('https://...')` -> `UND_ERR_CONNECT_TIMEOUT`
> - Node + `launch_with_proxy.mjs` / `ProxyAgent` -> 可命中上游，返回 `200` 或 `429`
>
> 结论：当前突破口不是“重装 GitHub 仓库”，而是“让 GDELT 和 `github` / `crossref` 一样通过代理 launcher 跑 HTTPS”。

### 5.8 GitHub MCP "Bad credentials" 排障

GitHub MCP 报 `Bad credentials` 时，按以下顺序排查：

**Step 1: 确认 token 有效性**
```bash
curl -s -H "Authorization: Bearer $GITHUB_PERSONAL_ACCESS_TOKEN" https://api.github.com/rate_limit | head -5
# 预期返回 JSON（含 rate limit 信息），不是 "Bad credentials"
```

**Step 2: 确认环境变量在当前进程中存在**
```bash
echo "Length: ${#GITHUB_PERSONAL_ACCESS_TOKEN}"
# 如果返回 0，说明当前 shell/进程没有这个变量
```

**Step 3: WSL 环境中的常见陷阱**

VSCode 从 Windows 启动时，继承的是 **Windows 环境变量**，不是 WSL `~/.bashrc`。即使 bashrc 里配好了 token，VSCode 启动的 MCP server 也可能拿不到。

**解决方案（二选一）：**

a) **PowerShell 设置 Windows 环境变量（推荐）：**
```powershell
[System.Environment]::SetEnvironmentVariable("GITHUB_PERSONAL_ACCESS_TOKEN", "你的token", "User")
[System.Environment]::SetEnvironmentVariable("GITHUB_HOST", "github.com", "User")
# 然后完全关闭再重开 VSCode（不是 reload window）
```

b) **WSL bashrc（仅对 WSL 终端生效）：**
```bash
echo 'export GITHUB_PERSONAL_ACCESS_TOKEN="你的token"' >> ~/.bashrc
echo 'export GITHUB_HOST=github.com' >> ~/.bashrc
source ~/.bashrc
# 需要完全重启 VSCode 才能让 MCP server 继承
```

**关键点：** MCP server 的 `${ENV_VAR}` 展开发生在 **server 启动时**。修改环境变量后必须重启 VSCode，让 MCP server 进程重新启动并读取新值。仅 `source ~/.bashrc` 不够——它只影响当前 shell，不影响已运行的 MCP server 进程。

### 5.9 GitHub / Crossref 的 `fetch failed`（Windows bridge / Node HTTPS）

如果满足下面 3 个条件：

- `curl https://api.github.com/rate_limit` 或 `curl https://api.crossref.org/...` 能通
- token 有效（GitHub `rate_limit` 返回 200）
- MCP 仍然报 `fetch failed` / `UND_ERR_CONNECT_TIMEOUT`

那么根因通常不是 token，而是 **Node 进程直连 443 超时**。实测现象是：

- `curl` 走代理正常
- Node 原生 `fetch()` 直连 `api.github.com:443` / `api.crossref.org:443` 超时
- `gdelt` 如果继续用裸 Node `fetch()` 打 `https://api.gdeltproject.org/...`，也会遇到同类直连超时问题

稳定做法：

1. 不要再用 `npx -y @modelcontextprotocol/server-github`
2. 不要再用 `npx -y @botanicastudios/crossref-mcp`
3. 在 WSL 本地固定安装包
4. 用一个代理 launcher 先执行 `undici.setGlobalDispatcher(new ProxyAgent(...))`，再 import 真正的 MCP 入口
5. `gdelt` 在需要走 HTTPS 时，也使用同一个 launcher；不要只依赖 `NODE_USE_ENV_PROXY=1`

当前本机落地路径：

- launcher: `/home/help/mcp/servers/_mcp_wrappers/launch_with_proxy.mjs`
- github: `/home/help/mcp/servers/github-mcp-local/node_modules/@modelcontextprotocol/server-github/dist/index.js`
- crossref: `/home/help/mcp/servers/crossref-mcp-local/node_modules/@botanicastudios/crossref-mcp/mcp-server.js`
- gdelt: `/home/help/mcp/servers/GDELT-mcp/dist/index.js`

这条规则同样适用于 Windows Codex App 的 `wsl.exe -> bash -lic` bridge 配置。

### 5.10 Claude Code MCP 配置文件陷阱（`~/.claude.json` vs `~/.claude/settings.json`）

Claude Code 有**两个**看起来都像全局配置的 JSON 文件，但它们的作用完全不同：

| 文件 | 实际作用 | MCP 定义是否在这里 |
|------|---------|-------------------|
| `~/.claude.json` | 运行时状态 + MCP server 注册 | **是** ✅ |
| `~/.claude/settings.json` | 偏好设置（model、plugins、proxy env 等） | **否** ❌ |

**常见踩坑场景：**

1. 你手动编辑了 `~/.claude/settings.json` 里的 `mcpServers`，重启后发现 MCP 没变
2. 你用 `claude mcp add` 注册了新 server，但去 `~/.claude/settings.json` 里找不到——因为它写入的是 `~/.claude.json`
3. 两个文件里都有 `mcpServers` 字段（历史遗留），但 Claude Code **只读 `~/.claude.json`** 中的

**正确的操作方式：**

```bash
# 查看当前 MCP 配置
claude mcp list

# 添加/替换 MCP server（写入 ~/.claude.json）
claude mcp add-json <name> '<json>' -s user

# 删除 MCP server
claude mcp remove <name> -s user

# 查看单个 server 配置
claude mcp get <name>
```

> **Mac / Linux / WSL 都适用**：这不是 WSL 特有问题。所有平台的 Claude Code 都从 `~/.claude.json` 读 MCP 配置。如果你之前在 `~/.claude/settings.json` 里配了 MCP，需要迁移到 `~/.claude.json`（推荐用 `claude mcp add-json` 命令）。

---

## 6. Phase 1 技能链 → MCP 依赖映射

只需安装对应行的 MCP 即可运行该技能：

```
company-foundation           → sec_edgar_mcp, alpaca, trading_mcp, yfinance
collect-company-facts        → sec_edgar_mcp, fs
extract-xbrl-timeseries      → sec_edgar_mcp, fs
recast-economic-statements   → fs
valuation-and-margin-of-safety → fs, yfinance, alpaca
```

**最小安装集**（跑完整 Phase 1 链路）：`sec_edgar_mcp` + `alpaca` + `trading_mcp` + `yfinance` + `fs`

---

## 7. 排障顺序

遇到 MCP 工具不可用时，按以下顺序排查：

1. **二进制路径** — `command` 指向的可执行文件是否存在？（`ls -l /path/to/command`）
2. **环境变量** — 需要 export 的变量是否在当前 shell 中？（`echo $ALPACA_API_KEY`）
   - **WSL 特别注意**：VSCode 继承 Windows 环境变量，不读 WSL `~/.bashrc`。设好变量后必须**完全重启 VSCode**（详见 5.8）
3. **服务能否单独启动** — 手动运行 command + args，看有无报错
4. **代理** — 网络请求失败优先检查 `HTTP_PROXY` 是否传入 MCP env
5. **HTTPS vs HTTP** — 若 HTTPS 超时但 HTTP 正常，可能是上游 TLS 故障（如 GDELT，详见 5.7）
6. **trust（Codex）** — 项目是否标记为 trusted？
7. **同名覆盖** — 项目级是否覆盖了全局配置？项目级配置是否完整？
8. **Node HTTPS 直连超时** — 若 `curl` 正常但 Node MCP 仍报 `fetch failed` / `UND_ERR_CONNECT_TIMEOUT`，优先排查是否仍在用 `npx -y` 或未经过代理 launcher（详见 5.9）

---

## 8. 全量 MCP 冒烟验收模板（17 项）

以下模板用于快速确认“服务已启动”之外的“工具可调用”状态。建议每个 server 至少跑 1 个轻量调用。

> 2026-03-22 本机实测：17/17 通过。`github`/`crossref` 走 proxy launcher 恢复；`gdelt` 配置正常（`timeline`/`tone_chart` 可用，`search_articles` 受上游 429 限流）。

| MCP | 最小调用示例 | 通过标准 |
|-----|--------------|----------|
| `context7` | `resolve-library-id("pandas", "...")` | 返回至少 1 个 library id |
| `sec_edgar_mcp` | `get_cik_by_ticker("AAPL")` | 返回 `success=true` 与 CIK |
| `fs` | `list_directory("/home/help/mcp/work")` | 返回目录列表 |
| `fetch` | `fetch("https://httpbin.org/get")` | 返回页面/JSON 内容 |
| `alpaca` | `get_clock()` | 返回 market clock |
| `rss` | `get_feed("https://hnrss.org/frontpage", count=1)` | 返回至少 1 条 item |
| `gdelt` | `gdelt_timeline("nvidia earnings", "1d", "volume")` | 返回 timeline 数据（**默认验收口径**）。不要用 `search_articles`/`monitor` 做 smoke（DOC API 易触发 `429`），不要用 `geo_search`（返回 `404`） |
| `trading_mcp` | `get_fundamental_stock_metrics("AAPL")` | 返回 fundamentals 字段 |
| `search` | `search_duckduckgo("OpenAI Codex MCP", 1)` | 返回搜索结果列表 |
| `openalex` | `search_works("deep learning finance", per_page=1)` | 返回 `meta/results` |
| `crossref` | `searchByTitle("deep learning", rows=1)` | 返回 `status=success` |
| `pubmed` | `search_pubmed_key_words("cancer immunotherapy", 1)` | 返回文章列表 |
| `arxiv` | `search_papers("transformer", 1)` | 返回 papers 列表 |
| `yfinance` | `get_stock_info("AAPL")` | 返回行情/公司信息 JSON |
| `github` | `search_repositories("owner:你的用户名")` | 返回 repo 列表（含 private 需 token） |
| `git` | `git_status("<repo_path>")` | 返回仓库状态 |
| `playwright` | `browser_navigate("https://example.com")` + `browser_snapshot()` | 返回页面 URL/标题/快照 |

如果某个 server 在并行测试中显示 `<tool_use_error>Sibling tool call errored</tool_use_error>`，请改为单独重试该 server，以排除并行链路中的级联中断影响。
