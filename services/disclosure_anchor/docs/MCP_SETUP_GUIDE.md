# MCP Setup Guide — disclosure-anchor

Setup checklist for the dev-facing MCP servers shared between **Codex** and **Claude Code**
on this machine. These MCPs help *develop* the disclosure-anchor service code; the data
service itself (cninfo/HKEX scraping, PDF download, parsing, ledger) is the project's own
Python code and does **not** use data MCPs.

> Scope: this is a machine-local setup note, not product architecture. `.mcp.json`,
> `.codex/config.toml`, and `dbhub.toml` are gitignored (machine-specific). Real secrets
> live in the shell env, never in repo files.

## 0. Runtime (one-time)

This machine has **no system Node**; only `uv`/`uvx` and Codex.app's bundled Node existed.
A standalone Node LTS was installed to a shared location both harnesses use:

```text
~/.local/node/                      # extracted Node v24.16.0 (darwin-arm64)
~/.local/bin/{node,npm,npx,corepack} -> ~/.local/node/bin/*   # on PATH, next to uv
```

Reinstall / upgrade Node (no Homebrew needed):

```bash
VER=v24.16.0    # or latest LTS from https://nodejs.org/dist/index.json
curl -fsSL -o /tmp/node.tgz "https://nodejs.org/dist/${VER}/node-${VER}-darwin-arm64.tar.gz"
rm -rf ~/.local/node && mkdir -p ~/.local/node ~/.local/bin
tar -xzf /tmp/node.tgz -C ~/.local/node --strip-components=1
for b in node npm npx corepack; do ln -sf ~/.local/node/bin/$b ~/.local/bin/$b; done
node --version && npx --version
```

## 1. Install approach: shared npm cache ("download once, both share")

We use **Plan A**: harness configs invoke `npx -y <pkg>@<pinned-version>` directly. The
"public place" is the per-user **shared npm cache** (`~/.npm/_cacache` + `~/.npm/_npx`),
which Codex and Claude both hit — so a package is downloaded once and reused by both.

- The npm cache is content-addressed with **no time-based expiry** — it does not re-download
  "a day later". Re-download happens only if: the spec is unpinned/`@latest` (version drift),
  the cache is cleared, or the cache dir is missing.
- **Pin every version** in the spec (done below) → no drift, offline-capable after first pull.
- Future hardening (optional, "Plan B"): `npm install pkg@x.y.z --prefix ~/.mcp-servers/node/<srv>`
  and point `command` at the absolute `node_modules/.bin/<bin>` for 100% offline determinism.

Warm the cache after a `npm cache clean` or on a new machine:

```bash
npm install --prefix /tmp/warm --no-fund --no-audit \
  @upstash/context7-mcp@3.2.1 @playwright/mcp@0.0.76 @bytebase/dbhub@0.22.3
rm -rf /tmp/warm           # throwaway; packages now live in shared ~/.npm cache
npx -y playwright@1 install chromium   # browser for Playwright MCP (shared ms-playwright cache)
```

## 2. Active servers (minimal set)

| Server | Package (pinned) | Role in developing this service | Auth |
|---|---|---|---|
| context7 | `@upstash/context7-mcp@3.2.1` | Version-accurate docs for MinerU/Docling/PyMuPDF/SQLModel/FastAPI/etc. | `CONTEXT7_API_KEY` (optional; runs keyless) |
| playwright | `@playwright/mcp@0.0.76` | Recon cninfo/HKEXnews pages: real XHR, `hisAnnouncement/query` params, PDF URL patterns | none |
| dbhub | `@bytebase/dbhub@0.22.3` | Read-only introspection of the SQLite evidence ledger the service writes | none (local file) |

GitHub is **not** added as an MCP: Codex already has the `github` plugin and Claude Code has
the `gh` CLI. Optional snippet in §6 if you want the dedicated server later.

## 3. Claude Code config — `.mcp.json` (project-scoped, this repo)

Already written. Claude expands `${VAR:-}` from its environment; an unset `CONTEXT7_API_KEY`
falls back to keyless. Each project opts in by listing only the servers it wants.

```json
{
  "mcpServers": {
    "context7":  { "command": "/Users/zhang/.local/bin/npx",
                   "args": ["-y", "@upstash/context7-mcp@3.2.1"],
                   "env": { "CONTEXT7_API_KEY": "${CONTEXT7_API_KEY:-}" } },
    "playwright":{ "command": "/Users/zhang/.local/bin/npx",
                   "args": ["-y", "@playwright/mcp@0.0.76"] },
    "dbhub":     { "command": "/Users/zhang/.local/bin/npx",
                   "args": ["-y", "@bytebase/dbhub@0.22.3", "--transport", "stdio",
                            "--config", "/Users/zhang/dev/agent-invest/services/disclosure_anchor/dbhub.toml"] }
  }
}
```

Verify: `claude mcp list` (run from this repo).

## 4. Codex config — global + project-scoped

Codex resolves config in precedence order (highest first): CLI `--config` → project
`.codex/config.toml` (walked project-root→cwd, closest wins, **trusted projects only**) →
`~/.codex/config.toml` → `/etc/codex/config.toml`. Layers **deep-merge per key**, so a project
file only needs to add its extra servers (it does not replace the global `mcp_servers`).

- **`~/.codex/config.toml` (global):** `context7` + `playwright` — project-agnostic, identical
  npx commands → same shared cache as Claude.
- **`<repo>/.codex/config.toml` (project-scoped, gitignored):** `dbhub`. Its DSN is
  project-specific, so it lives here instead of the global config — mirroring Claude's
  project-scoped `.mcp.json`. Codex loads it because this project is trusted
  (`trust_level = "trusted"` for this path in `~/.codex/config.toml`); untrusted projects skip
  all `.codex/` layers.

```toml
# <repo>/.codex/config.toml
[mcp_servers.dbhub]
enabled = true
command = "/Users/zhang/.local/bin/npx"
args = ["-y", "@bytebase/dbhub@0.22.3", "--transport", "stdio",
        "--config", "/Users/zhang/dev/agent-invest/services/disclosure_anchor/dbhub.toml"]
startup_timeout_sec = 120
tool_timeout_sec = 60
enabled_tools = ["execute_sql"]
```

Verify: from this repo, `codex mcp list` shows `dbhub` plus the global servers; run it from
elsewhere (e.g. `~`) and `dbhub` is absent — confirming it is project-scoped, not global.

## 5. DBHub read-only ledger — `dbhub.toml` (this repo, gitignored)

DBHub 0.22.x dropped the `--readonly` flag in favor of a config file:

```toml
[[sources]]
id = "ledger"
dsn = "sqlite:///Volumes/AgentSSD/disclosure_anchor/ledger.db"   # sqlite:/// + absolute path with its leading slash dropped (3rd slash = root) → opens /Volumes/...

[[tools]]
name = "execute_sql"
source = "ledger"
readonly = true
```

The ledger lives on AgentSSD (`/Volumes/AgentSSD/disclosure_anchor/ledger.db`). A valid empty
SQLite db was seeded so the server starts before the service builds the real schema. Point the
DSN at wherever the service finally writes the ledger.

## 6. Secrets & optional servers

- Export secrets in `~/.zshrc` (never in repo): `export CONTEXT7_API_KEY=...`. GUI-launched
  apps may not inherit shell env — Context7 still works keyless if absent.
- Optional GitHub MCP for Claude (remote): add to `.mcp.json`
  `{"github": {"type":"http","url":"https://api.githubcopilot.com/mcp/","headers":{"Authorization":"Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"}}}`.
- Optional Tier-2 (per-project, as needed): DuckDB (`mcp-server-motherduck` via uvx),
  filesystem (scoped to AgentSSD), sequential-thinking, web (Firecrawl/Tavily/Exa — need keys).

## 7. Smoke tests

```bash
export PATH="$HOME/.local/bin:$PATH"
npx -y @upstash/context7-mcp@3.2.1 --help            # prints usage
node "$(npm root -g 2>/dev/null)"; :                 # (no-op placeholder)
# DBHub connects read-only and exits on EOF:
npx -y @bytebase/dbhub@0.22.3 --transport stdio \
  --config "$PWD/dbhub.toml" < /dev/null             # expect EXIT 0, "🔒 execute_sql"
claude mcp list                                       # context7/playwright/dbhub healthy
codex mcp list                                        # from repo: +dbhub; from ~: dbhub absent
```
