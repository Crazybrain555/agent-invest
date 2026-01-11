# AIQuantLab Agent Guide (AGENTS.md)

This document is optimized for coding agents (Codex/Cursor/Devin/etc.) and also readable for humans.
README/Docs are for humans; this file captures the extra, agent-critical context: build/run commands,
repo invariants, operational gotchas, and "how we do changes safely".

---

## 0) Continuity Ledger (compaction-safe)

Maintain a single Continuity Ledger for this workspace in `CONTINUITY.md` (repo root).
The ledger is the canonical session briefing designed to survive context compaction; do not rely on
earlier chat text unless it's reflected in the ledger.
If MCP `fs` tool access is restricted to `/home/help/mcp/work` and cannot reach the repo root,
maintain a temporary mirror at `/home/help/mcp/work/CONTINUITY.md` for tool access and sync it back
to the repo-root `CONTINUITY.md` whenever shell access is available (repo root remains canonical).

### How it works
- At the start of every assistant turn: read `CONTINUITY.md`, update it to reflect the latest
  goal/constraints/decisions/state, then proceed with the work.
- Update `CONTINUITY.md` again whenever any of these change: goal, constraints/assumptions,
  key decisions, progress state (Done/Now/Next), or important tool outcomes.
- Keep it short and stable: facts only, no transcripts. Prefer bullets. Mark uncertainty as
  `UNCONFIRMED` (never guess).
- If you notice missing recall or a compaction/summary event: refresh/rebuild the ledger from visible
  context, mark gaps `UNCONFIRMED`, ask up to 1-3 targeted questions, then continue.

### update_plan vs the Ledger
- `update_plan` (or similar TODO tool) is for short-term execution scaffolding (3-7 steps).
- `CONTINUITY.md` is for long-running continuity across compaction (the "what/why/current state"),
  not a step-by-step task list.
- Keep them consistent: when the plan/state changes, update the ledger at the intent/progress level.

### Suggested CONTINUITY.md format (keep headings)
- Goal (incl. success criteria):
- Constraints/Assumptions:
- Key decisions:
- State:
- Done:
- Now:
- Next:
- Open questions (UNCONFIRMED if needed):
- Working set (files/ids/commands):

---

## 1) Agent Operating Mode (Codex-style, repo-specific)

### Bias to action
- Default expectation: deliver working code + verification steps, not just a plan.
- If details are missing, make reasonable assumptions and implement a safe default.
- Do not produce mid-rollout status chatter; keep intermediate messages minimal.

### Explore efficiently
- Search before writing: reuse existing helpers/patterns.
- Prefer fast search (`rg`) and targeted reads (avoid reading huge files end-to-end).
- Batch file reads when possible; avoid one-file-at-a-time thrashing.

### Tool output truncation (for harness implementers + agents)
If a tool response may be huge (logs, big diffs, data dumps), keep outputs small and "in-distribution":
- Target <= ~10k tokens/tool response (approx `num_bytes/4`).
- If truncating: use half budget for the beginning + half for the end, and truncate the middle with:
  `...3 tokens truncated...`
- Prefer producing smaller outputs upstream:
  - narrow `rg` patterns, add `--max-count`, print specific ranges, or summarize.

### Safe editing rules
- Never run destructive commands (`git reset --hard`, mass deletes, DB drops) unless explicitly asked.
- Do not "fix" by broad try/except or silent fallbacks; surface failures with actionable logs.
- Preserve existing behavior by default; gate behavior changes behind config/flags when possible.
- Keep UTF-8 explicit for file/log I/O (`encoding="utf-8"`).

### Presenting results
- Be concise. Reference paths as inline code (e.g. `.codex/skills/company_research/collect-company-facts/SKILL.md`).
- When you changed code: explain what + why + how to verify.

---

## 2) AGENTS.md Discovery (FYI)

Codex builds an instruction chain every run:
- Global: `~/.codex/AGENTS.override.md` wins; else `~/.codex/AGENTS.md` (only the first non-empty file).
- Project: from repo root down to current working directory. In each directory:
  `AGENTS.override.md`, then `AGENTS.md`, then any configured fallback names
  (`project_doc_fallback_filenames`).
- Merge order: root -> leaf; deeper files override earlier guidance.
- Combined size cap: `project_doc_max_bytes` (default 32 KiB). Split into nested AGENTS files or raise
  the limit if needed.

Useful verification commands:
- `codex --ask-for-approval never "Summarize the current instructions."`
- `codex --cd <subdir> --ask-for-approval never "List the instruction sources you loaded."`

---

## 3) Contributor Quickstart Guide

### 3.1 Encoding defaults (required)
Always run commands in UTF-8 terminals.
- WSL/Linux:
  - `export PYTHONIOENCODING=UTF-8`
  - `export PYTHONUTF8=1`
- PowerShell:
  - `chcp 65001 | Out-Null; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.UTF8Encoding]::new()`
- CMD:
  - `chcp 65001 >NUL`

When reading/writing text files: always set `encoding="utf-8"` (or `utf-8-sig` for Excel-friendly CSVs).

### 3.2 VS Code / Cursor tasks
If present, common tasks live in `.vscode/tasks.json`. Run via Terminal -> Run Task...

### 3.3 Proxy note (WSL)
If WSL prints localhost proxy warnings in NAT mode:
- disable proxy envs in `~/.bashrc`, or
- switch WSL to mirrored networking on Windows: `wsl --set-default-networking-mode mirrored`,
  then restart: `wsl --shutdown`

---

## 4) LLM Repo Layout (current)

Primary working areas:
- `.codex/skills/`: skill definitions and scripts; start from the relevant `SKILL.md`.
- `.codex/skills/public/`: reusable public skills (e.g., stock pool).
- `.codex/skills/company_research/`: company research skills and scripts.
- Root docs: `README.md`, `AGENTS.md`, `CLAUDE.md`, and planning notes (`stock_skills_*.md`, `Phase 1 核心估值链 Codex Skills 实施指南.md`, `CORR_TOOL_CORE_API_PLAN.md`).

Notes:
- This LLM branch is intentionally trimmed; traditional pipelines/tools are removed.
- Keep edits focused on skill workflows and documentation.

---

## 5) LLM Skill Workflow

- Use the skill list in the system prompt to select a skill; open the matching `SKILL.md` first.
- Prefer running/patching scripts under the skill folder when available.
- Keep context small: read only the needed references; avoid bulk loading.

---

## 6) Use the Codex Task Queue as a Lightweight Backlog

When tangential fixes appear mid-task:
- capture them as queued tasks instead of expanding scope immediately
- keep each queued task independently shippable
- return to the main goal unless explicitly asked to broaden scope
