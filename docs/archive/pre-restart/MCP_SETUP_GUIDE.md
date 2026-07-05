# MCP Setup Guide

This file is a lightweight MCP setup checklist for the reset checkout. It preserves the useful Codex / Claude MCP notes without treating any old skill chain as active product architecture.

## 1. Files

| File | Purpose | Notes |
| --- | --- | --- |
| `.codex/config.toml` | Project-local Codex MCP config | Machine-specific; keep secrets in env vars. |
| `.mcp.json` | Project-local Claude MCP config | Machine-specific; ignored by git in this checkout. |
| `.env.template` | Environment-variable reference | Use placeholders only; never store real keys. |
| `.claude/settings.local.json` | Claude local permissions/settings | Local harness support. |

User-level config may still live outside the repo:

- Codex: `~/.codex/config.toml`
- Claude Code MCP runtime state: `~/.claude.json`
- Claude Code settings: `~/.claude/settings.json`

## 2. Credential Rules

Do not put real credentials in repo files.

Use environment variables such as:

```bash
export CONTEXT7_API_KEY="replace_me"
export ALPACA_API_KEY="replace_me"
export ALPACA_SECRET_KEY="replace_me"
export ALPACA_PAPER_TRADE="True"
export GITHUB_PERSONAL_ACCESS_TOKEN="replace_me"
export GITHUB_HOST="github.com"
```

If a real key was ever committed or saved in a shared config file, rotate it at the provider.

## 3. Current MCP Posture

The user may disable some MCP servers later. Until then, retained MCP entries should be treated as optional local tooling, not as a required product dependency.

Common server categories currently represented in local config:

- market data / trading data,
- SEC EDGAR,
- search / fetch / RSS,
- academic paper lookup,
- GitHub,
- browser automation,
- documentation lookup.

Keep local absolute paths machine-specific. Do not rewrite them into product requirements unless the user asks.

## 4. Validation

Parse project-local config:

```bash
python - <<'PY'
import json
import pathlib
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

for p in ['.codex/config.toml', '.codex/agents/quanti_reviewer.toml']:
    path = pathlib.Path(p)
    if path.exists():
        tomllib.loads(path.read_text(encoding='utf-8'))
        print(f'{p} parses')

for p in ['.mcp.json', '.claude/settings.local.json']:
    path = pathlib.Path(p)
    if path.exists():
        json.loads(path.read_text(encoding='utf-8'))
        print(f'{p} parses')
PY
```

Check MCP server visibility from each tool after launching in this repo:

```bash
codex mcp list
claude mcp list
```

These commands can fail if the relevant CLI is not installed, not authenticated, or not launched from a trusted project.
