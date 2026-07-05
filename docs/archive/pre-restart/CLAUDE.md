# CLAUDE.md — Claude Code Agent Guide

**Scope:** Claude Code-specific guidance for this reset checkout. Repo-wide policy lives in `AGENTS.md`.

## 0. Durable State And Resume

Start multi-step work by reading:

- `AGENTS.md`
- `docs/agent/Status.md`
- `docs/agent/Plan.md`
- `docs/agent/Implement.md`

Do not rely on deleted historical files such as `docs/skills/*`, `.agents/skills/*`, or `company_research_runtime/*`.

## 1. Claude-Specific Preferences

1. Keep responses concise unless the user asks for detail.
2. Before multi-file or risky edits, give a short plan and intended file list.
3. If the user corrects a workflow rule, propose a concrete `CLAUDE.md` or `AGENTS.md` update before treating it as permanent.

## 2. Current Repo Reality

This repo has been reset to keep the Codex / Claude harness and MCP setup only.

Retained areas:

- `AGENTS.md`
- `CLAUDE.md`
- `docs/agent/`
- `.codex/`
- `.claude/settings.local.json`
- `.mcp.json`
- `.env.template`
- `docs/MCP_SETUP_GUIDE.md`

Removed or intentionally absent:

- old company-research skills,
- stale Claude worktrees,
- old skill specs and master plan,
- old company-research runtime helpers,
- old README / requirements / helper scripts tied to the removed skill workspace.

Do not assume a production codebase, scheduler, database, notebook pipeline, or company-research skill chain exists here unless new files are created.

## 3. MCP Notes

MCP config is machine-specific and may stay in this checkout while the user decides what to keep enabled. Real credentials must live in environment variables or private user-level config, not in repo files.

Use `docs/MCP_SETUP_GUIDE.md` for local MCP setup notes. Treat it as a setup checklist, not as a product architecture plan.

## 4. Reference Files

- `AGENTS.md` — cross-agent operating contract
- `docs/agent/Status.md` — short current state
- `docs/agent/Plan.md` — active durable plan/checklist
- `docs/agent/Implement.md` — execution runbook
- `docs/agent/code_review.md` — independent review rubric
- `.codex/agents/quanti_reviewer.toml` — read-only reviewer subagent
