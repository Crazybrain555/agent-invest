Goal (incl. success criteria):
- Verify Claude-side fixes are correctly landed:
  - `.codex/config.toml` gdelt proxy passthrough + playwright absolute npx
  - `docs/MCP_SETUP_GUIDE.md` real clone URLs + missing path rows
- Re-run full MCP smoke validation and report deterministic results.

Constraints/Assumptions:
- Keep scope focused on MCP startup/config only.
- Global config edits are allowed (user explicitly requested earlier).
- Prefer official OpenAI/Codex documentation for format updates.

Key decisions:
- Use project file `.codex/config.toml` as source-of-truth for this validation run.
- Treat "success" as practical callability (not just `enabled` listing).
- Keep `fs` rooted at `/home/help/mcp/work` (runtime output invariant), not repo root.

State:
- Completed.

Done:
- Added user-provided GitHub PAT to local shell startup files (non-repo):
  - `/home/help/.bashrc`
  - `/home/help/.profile`
  - Also set `GITHUB_HOST=github.com`.
- Verified PAT validity directly via GitHub API:
  - Authenticated `GET /rate_limit` returns `200`.
  - Private repo `Crazybrain555/my-quant-project`: unauthenticated `404`, authenticated `200`.
- Verified GitHub MCP works in a fresh Claude process:
  - `claude -p` could read private repo `README.md` via github MCP and returned `PASS`.
- Re-tested both target MCP tools in current Codex session:
  - `github/search_repositories` for `owner:Crazybrain555` returns repositories successfully.
  - `gdelt/gdelt_search_articles` returns news results successfully.
- Ran full 17-server smoke validation in current Codex session:
  - PASS: `context7`, `sec_edgar_mcp`, `fs`, `fetch`, `alpaca`, `rss`, `gdelt`, `trading_mcp`, `search`, `openalex`, `crossref`, `pubmed`, `arxiv`, `yfinance`, `github`, `git`, `playwright`.
  - GitHub private repo read (`get_file_contents` on `Crazybrain555/my-quant-project`) also passed.
- Cleaned accidental config pollution caused by an interrupted bulk edit:
  - Removed stray root-level `HTTP_PROXY/HTTPS_PROXY/NO_PROXY/...` keys from:
    - `.codex/config.toml`
    - `/home/help/.codex/config.toml`
  - Re-validated TOML parsing for both files.
- Confirmed `docs/MCP_SETUP_GUIDE.md` already contains troubleshooting sections for:
  - GDELT connectivity diagnosis and HTTP/HTTPS fallback.
  - GitHub `Bad credentials` + WSL/PowerShell environment inheritance notes.
- Updated `docs/MCP_SETUP_GUIDE.md` with a new full validation section:
  - Added `## 8. 全量 MCP 冒烟验收模板（17 项）`
  - Included per-server minimal call + pass criteria + note on sibling tool-call cascading errors.
- Re-verified user-mentioned fix points:
  - `.codex/config.toml` gdelt has `env_vars` proxy passthrough (HTTP/HTTPS/NO_PROXY + lowercase).
  - `.codex/config.toml` playwright uses absolute `/home/help/mcp/tools/bin/npx` (not bare `npx`).
  - `docs/MCP_SETUP_GUIDE.md` MCP table now uses real GitHub URLs for trading/yfinance/rss/gdelt/openalex/pubmed.
  - `docs/MCP_SETUP_GUIDE.md` path table includes arxiv binary path, playwright chromium path, and git `--repository` path.
- Re-ran full MCP smoke checks in current session: all 17 passed.
  - Confirmed GitHub private repo read via `github/get_file_contents` on `Crazybrain555/my-quant-project`.

Now:
- Report final verification results to user.

Next:
- None.

Open questions (UNCONFIRMED if needed):
- UNCONFIRMED: long-term stability of GDELT endpoint behavior (network/egress conditions can vary by time and proxy exit).

Working set (files/ids/commands):
- `.mcp.json`
- `.codex/config.toml`
- `/home/help/.claude/settings.json`
- `/home/help/.bashrc`
- `/home/help/.profile`
- `CONTINUITY.md`
