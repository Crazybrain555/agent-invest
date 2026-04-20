# CLAUDE.md - Claude Code Agent Guide (Skills Repo)

**Last Updated:** 2026-04-20
**Scope:** Claude Code-specific guidance for this repository. If anything here conflicts with the filesystem, `AGENTS.md`, `docs/agent/*`, or `docs/skills/MASTER_PLAN.md`, the real repo structure and runnable commands win.

---

## 0) Durable state and resume

This repository now uses the `docs/agent/` durable workflow as the primary long-horizon state:

- `docs/agent/Status.md` — short resume pointer
- `docs/agent/Plan.md` — active milestone and validation
- `docs/agent/Implement.md` — execution runbook
- `docs/agent/Documentation.md` — longer audit log

`CONTINUITY.md` is **not** part of the active workflow in the current repo state. Do not recreate or depend on it unless the user explicitly asks for a Claude-specific continuity ledger.

---

## 1) Claude-specific operating preferences

These preferences are Claude-specific; repo-wide safety and source-of-truth rules live in `AGENTS.md`.

1. Default brevity: unless the user asks otherwise, keep responses concise.
2. Before multi-file or risky edits, give a short plan and the intended file list.
3. If the user corrects a mistake, propose a concrete `CLAUDE.md` update before assuming the repo wants that rule permanently.

---

## 2) Repo reality

This repo is intentionally trimmed and centered around skills plus a shared runtime. Do not assume `src/`, schedulers, data pipelines, or the full AIQuantLab production codebase exists here.

Current in-repo implementation reality:

- The only implemented in-repo runner today is `.agents/skills/company_research/collect-company-facts/scripts/run.py`.
- The corresponding skill contract is `.agents/skills/company_research/collect-company-facts/SKILL.md`.
- `docs/skills/README.md` is the implementation-status index for what actually exists.
- `docs/skills/MASTER_PLAN.md` and `docs/skills/specs/` describe the target 9-skill architecture, not current implementation completeness.
- `company_research_runtime/` contains shared runtime helpers and should not be removed casually.

Primary output root:

- `${COMPANY_RESEARCH_ROOT:-/home/help/mcp/work/company_research}/company/{TICKER}/`

Key invariants:

- `raw/` is append-only evidence.
- `current/` is the query layer.
- `runs/{run_id}/` is the per-run audit record.

---

## 3) Useful Claude context discipline

When you need to re-anchor context, prefer attaching the smallest relevant files:

- `@AGENTS.md`
- `@docs/agent/Status.md`
- `@docs/agent/Plan.md`
- `@docs/agent/Implement.md`
- `@docs/skills/README.md`
- `@docs/skills/MASTER_PLAN.md`
- `@docs/skills/specs/<skill-doc>.md`
- `@.agents/skills/company_research/<skill>/SKILL.md`
- `@company_research_runtime/`

After `/clear`, re-attach `@AGENTS.md`, `@docs/agent/Status.md`, and the most relevant skill/spec files instead of relying on old chat context.

---

## 4) Reference documents

- `AGENTS.md` — cross-agent invariants and safe editing rules
- `docs/agent/Status.md` — current durable resume state
- `docs/agent/Plan.md` — milestone plan and validation
- `docs/agent/Implement.md` — durable execution runbook
- `docs/skills/README.md` — skills overview and implementation status
- `docs/skills/MASTER_PLAN.md` — architecture, execution protocol, and shared schemas
- `docs/skills/specs/skill*.md` — per-skill specifications
- `docs/skills/references/SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md` — SEC/XBRL artifact handling specifics
