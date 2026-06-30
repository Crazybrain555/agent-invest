# CLAUDE.md - Claude Code Agent Guide

**Scope:** Claude Code-specific guidance for this reset checkout. Repo-wide policy lives in `AGENTS.md`.

Always reply to the user in Chinese unless the user asks otherwise or the content is code/API text. Do not turn
normal answers into long operation manuals.

## 0. Durable State And Resume

Start multi-step work by reading:

- `AGENTS.md`
- `docs/agent/Status.md`
- `docs/agent/Plan.md`
- `docs/agent/Implement.md`

Do not rely on deleted historical files such as `docs/skills/*`, `.agents/skills/*`, or `company_research_runtime/*`.

## 1. Claude-Specific Preferences

1. Keep responses concise unless the user asks for detail.
2. Before multi-file or risky edits, give a short plan with the key assumption, intended file list, and
   validation target.
3. Prefer the smallest direct change. Do not add speculative abstraction, broad configuration, unrelated
   cleanup, or "just in case" defensive branches.
4. Let unexpected failures surface. Catch specific expected errors only when the code can recover, quarantine,
   persist failure status, clean up, or re-raise with useful context.
5. After code changes, update or add the relevant tests. If behavior, schema, command, or public contract
   changes, update the matching spec/docs.
6. When the user asks to continue an active durable task, advance the next safe checklist item before replying
   unless a concrete blocker or user decision is required.
7. If the user corrects a workflow rule, propose a concrete `CLAUDE.md` or `AGENTS.md` update before treating
   it as permanent.

## 2. Current Repo Reality

This repo now contains the disclosure_anchor service implementation plus the Codex / Claude harness and MCP
setup files.

Retained areas:

- `AGENTS.md`
- `CLAUDE.md`
- `docs/agent/`
- `.codex/`
- `.claude/settings.local.json`
- `.mcp.json`
- `.env.template`
- `.env.example`
- `docs/MCP_SETUP_GUIDE.md`
- `README.md`
- `pyproject.toml`
- `Makefile`
- `src/disclosure_anchor/`
- `contracts/`
- `tests/`

Removed or intentionally absent:

- old company-research skills,
- stale Claude worktrees,
- old skill specs and master plan,
- old company-research runtime helpers,
- old README / requirements / helper scripts tied to the removed skill workspace.

Do not assume a scheduler, notebook pipeline, or company-research skill chain exists here unless new files are
created. The current service code is a local disclosure ingestion/parser codebase.

## 3. MCP Notes

MCP config is machine-specific and may stay in this checkout while the user decides what to keep enabled. Real credentials must live in environment variables or private user-level config, not in repo files.

Use `docs/MCP_SETUP_GUIDE.md` for local MCP setup notes. Treat it as a setup checklist, not as a product architecture plan.

## 4. Reference Files

- `AGENTS.md` — cross-agent operating contract
- `docs/agent/Status.md` — short current state
- `docs/agent/Plan.md` — active durable plan/checklist
- `docs/agent/Implement.md` — execution runbook
- `docs/agent/code_review.md` — independent review rubric
- `AGENTS.md` independent-review trigger — use the read-only reviewer gate when required
