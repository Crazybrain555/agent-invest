# CLAUDE.md - Claude Code Agent Guide (Phase 1 Skills Repo)

**Last Updated:** 2026-02-07
**Scope:** This repository is a **skills-only** Phase 1 company research/valuation pipeline (not the full AIQuantLab codebase).

This file is optimized for Claude Code (and other coding agents). It records agent-critical truth:
repo reality, where outputs go, and how to run the Phase 1 skill chain safely.

If anything here conflicts with the filesystem or with `Phase 1 核心估值链 Codex Skills 实施指南.md`,
the **real repo structure / real commands win**.

---

## 0) Continuity Ledger (compaction-safe)

Maintain a single Continuity Ledger in `CONTINUITY.md` (repo root).
It is the canonical session briefing designed to survive context compaction; do not rely on earlier chat text
unless it's reflected in the ledger.

If MCP `fs` tool access is restricted to `/home/help/mcp/work` and cannot reach the repo root, maintain a
temporary mirror at `/home/help/mcp/work/CONTINUITY.md` and sync it back to repo-root `CONTINUITY.md` when
shell access is available (repo root remains canonical).

**How it works**
- At the start of every assistant turn: read `CONTINUITY.md`, update it to reflect the latest goal/constraints/decisions/state, then proceed.
- Update `CONTINUITY.md` again whenever any of these change: goal, constraints/assumptions, key decisions, progress state (Done/Now/Next), or important tool outcomes.
- Keep it short and stable: facts only, no transcripts. Prefer bullets. Mark uncertainty as `UNCONFIRMED` (never guess).

---

## 1) Operating Contract (Claude Code)

These are behavioral constraints for Claude Code sessions in this repo:

1. **Default brevity**: Unless the user asks otherwise, keep responses to **1 paragraph, <= 5 sentences**.
2. **Plan before edits**: Before editing any file, first provide a **3-7 step plan** and a **file list** of what will change.
3. **Self-evolving docs (on correction)**: Whenever the user corrects a mistake, append this line verbatim and follow it with concrete text to add:
   `Propose an update to CLAUDE.md to prevent repeating this mistake.`
   Do **not** auto-edit `CLAUDE.md` unless the user explicitly approves the change.

---

## 2) Repo Reality (actual structure)

This repo is intentionally trimmed and centered around Phase 1 skills + a shared runtime.
Do not assume `src/`, schedulers, data pipelines, or other AIQuantLab production code exists here.

Primary working areas:

- `.codex/skills/`:
  - `company_research/*/SKILL.md` defines each skill contract and how to run it.
  - `company_research/*/scripts/run.py` are the canonical runners.
- `company_research_runtime/`: shared runtime utilities used by skills (do not delete).
- `_sec_downloads/`: sample SEC download/materialization utilities and specimens (if present).
- `.mcp.json`: Claude Code 项目级 MCP 声明（路径为当前开发机布局，迁移时需调整）。
- Root docs (source of truth for Phase 1 workflow):
  - `Phase 1 核心估值链 Codex Skills 实施指南.md`
  - `stock_skills_buildplan_v2.md`
  - `SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md`
  - `AGENTS.md` (cross-agent invariants)

---

## 3) Runtime Output Directory (运行产物目录)

All skill outputs are written under:

- `/home/help/mcp/work/company_research/company/{TICKER}/`

Key invariants (do not break):
- `raw/` is **immutable evidence** (append-only, traceable, reproducible).
- `current/` is the **query layer** (the latest promoted state).
- `runs/{run_id}/` is the **per-run record** (inputs/meta/result/needs + outputs snapshot).

Minimal shape (not exhaustive):
```text
/home/help/mcp/work/company_research/company/{TICKER}/
  company.yaml
  latest.json
  current/
    artifacts_state.yaml
    evidence.jsonl
    market_snapshot.yaml
    filings_index.yaml
    xbrl_atlas/
    economic/
    valuation/
  raw/
    sec/{accession}/...
  runs/{run_id}/
    meta.yaml
    result.yaml
    needs.yaml
    outputs/...
```

---

## 4) Phase 1 Skill Chain (canonical order + commands)

Canonical execution order:
1. `company-foundation`
2. `collect-company-facts`
3. `extract-xbrl-timeseries`
4. `recast-economic-statements`
5. `valuation-and-margin-of-safety`

Canonical commands (copy/paste):
```bash
# 1) Identity + market snapshot denominators
python .codex/skills/company_research/company-foundation/scripts/run.py AAPL

# 2) SEC evidence pool (filings_index + raw/sec snapshots)
python .codex/skills/company_research/collect-company-facts/scripts/run.py AAPL

# 3) XBRL timeseries -> Statement Atlas
python .codex/skills/company_research/extract-xbrl-timeseries/scripts/run.py AAPL

# 4) GAAP -> economic statements (NOPAT/ROIC/FCF/etc)
python .codex/skills/company_research/recast-economic-statements/scripts/run.py AAPL

# 5) Valuation + margin of safety
python .codex/skills/company_research/valuation-and-margin-of-safety/scripts/run.py AAPL --model-type hybrid
```

Shared flags:
- `--demo`: offline demo mode (use built-in demo payloads).
- `--force-refresh`: recompute even if `current/` already looks complete.
- `--as-of YYYY-MM-DD`: pin snapshot date (default is "today").

If a skill is blocked, it should write `runs/{run_id}/needs.yaml` and point to the upstream skill dependency.

---

## 5) Claude Code Power Features (how we use them here)

### 5.1 `@` context discipline
Prefer explicitly attaching the most relevant sources instead of describing them:
- `@CONTINUITY.md`
- `@AGENTS.md`
- `@Phase 1 核心估值链 Codex Skills 实施指南.md`
- `@SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md`
- `@.codex/skills/company_research/<skill>/SKILL.md`
- `@company_research_runtime/`

### 5.2 `/clear` reset rule
Use `/clear` immediately when any of these happen:
- The agent repeats the same wrong repo structure assumptions.
- File paths are repeatedly guessed incorrectly.
- The conversation accumulates contradictory constraints.

After `/clear`, re-attach: `@CONTINUITY.md`, `@AGENTS.md`, and the relevant `@.../SKILL.md`.

### 5.3 `/init` (docs alignment)
Run `/init` when you make significant changes to:
- repo structure
- skill outputs layout (`raw/`, `current/`, `runs/`)
- operating constraints (safety rules, invariants)

Use `/init` output to propose an update to this `CLAUDE.md` (do not auto-edit unless approved).

### 5.4 Custom project slash commands
Project-level custom commands should live in:
- `.claude/commands/*.md`

Keep them short and task-focused (e.g., run a chain, generate diffs, lint a subset).
Note: we are **not** versioning `.claude/commands/` in this repo yet; consider adding it after the 5 skills stabilize.

### 5.5 Hooks (non-destructive only)
Hooks should be:
- non-destructive
- fast
- reliable in WSL/Linux shells

If you later enable hooks, start with a non-destructive check like `python -m compileall` (Claude Code hooks are typically configured via `.claude/settings.json`).

### 5.6 MCP/tooling
Rule of thumb:
- First, use existing MCP tools already available in the environment.
- If a new tool is needed: write a minimal interface contract (inputs/outputs/error modes) before implementation.

### 5.7 `claude -p` for batch work
Use `claude -p` for repeatable, non-interactive tasks such as:
- generate a plan/checklist for a change
- generate a proposed diff for docs
- produce summaries from a fixed set of files

### 5.8 Parallelism (`git worktree`)
For large tasks, prefer parallel sessions in separate worktrees to avoid cross-task context pollution.

---

## 6) Reference Documents

- `AGENTS.md` (cross-agent invariants and safe editing rules)
- `CONTINUITY.md` (canonical session ledger)
- `Phase 1 核心估值链 Codex Skills 实施指南.md` (Phase 1 end-to-end workflow)
- `stock_skills_buildplan_v2.md` (planning notes)
- `SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md` (SEC/XBRL artifact handling specifics)
