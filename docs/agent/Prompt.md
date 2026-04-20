# Prompt.md — Quanti Durable Task Specification

This file freezes the standing target for long-horizon Codex work in this repository. For a new major task, update the task-specific sections before implementation begins, then regenerate or update `Plan.md`.

## Standing repository purpose

Quanti is a skills-only workspace for evidence-driven company research and valuation. The target system decomposes company analysis into reusable, independently rerunnable skills that produce traceable artifacts for the formula:

```text
valuation = sustainable economic profit × quality coefficient
```

The system should answer:

1. What is the company’s future sustainable economic profit?
2. How certain are we, based on traceable evidence rather than subjective scoring?
3. What valuation range and margin of safety follow from that evidence?

## Current repository scope

This repository currently contains:

- shared runtime helpers in `company_research_runtime/`,
- one implemented in-repo runner: `.agents/skills/company_research/collect-company-facts/scripts/run.py`,
- a `collect-company-facts` skill definition,
- target architecture and per-skill specifications under `docs/skills/`,
- MCP configuration and setup documentation,
- Codex durable workflow files under `docs/agent/`.

This repository does **not** currently contain a complete production scheduler, database, app, notebook pipeline, or all nine skill runners.

## Standing goals

- Keep the repo skills-only unless the user explicitly changes the architecture.
- Preserve evidence traceability for every company research artifact.
- Keep production artifacts outside the repo under `COMPANY_RESEARCH_ROOT`.
- Make each skill independently rerunnable, auditable, and explicit about hard dependencies.
- Prefer blocked/partial outputs with structured `needs.yaml` over fabricated downstream data.
- Keep Codex long-horizon work recoverable through `docs/agent/Status.md`, `Plan.md`, `Implement.md`, and `Documentation.md`.

## Standing non-goals

- Do not turn this repository into the full AIQuantLab production platform.
- Do not invent commands for skill runners that do not exist.
- Do not silently orchestrate the entire 9-skill chain from inside one skill.
- Do not store SEC, market, model, or valuation output artifacts inside the repo.
- Do not treat old prose docs as more authoritative than current files and runnable commands.

## Current durable task brief

Current task: Codex-native harness foundation and policy simplification for Quanti.

Purpose: make Codex easier to operate from natural-language prompts by keeping the repo policy concise, reducing task routing to five modes, and preserving durable project memory under `docs/agent/`.

Goals:

- Keep `.codex/config.toml`, `AGENTS.md`, and `docs/agent/*` consistent.
- Use a 5-mode task router in `AGENTS.md` rather than many fine-grained modes.
- Keep `AGENTS.md` focused on routing, hard rules, and stable repository facts.
- Keep detailed execution steps in `docs/agent/Implement.md` rather than repeating them in `AGENTS.md`.
- Keep `Status.md` short enough for session start/resume.
- Keep `Plan.md` as the source of truth for milestones and validation.
- Keep `Implement.md` as the runbook rather than duplicating detailed execution logic in `AGENTS.md`.

Non-goals:

- Do not add hooks, subagents, or new skills in this phase.
- Do not change runtime/business code as part of harness maintenance unless explicitly requested.
- Do not overfit `AGENTS.md` to one prompt template.

## Deliverables

- `.codex/config.toml`: project-scoped Codex behavior and MCP config.
- `AGENTS.md`: concise operating contract and 5-mode task router.
- `docs/agent/Prompt.md`: standing goals plus current durable task brief.
- `docs/agent/Plan.md`: current milestones and validation.
- `docs/agent/Status.md`: short resume state.
- `docs/agent/Implement.md`: execution runbook.
- `docs/agent/Documentation.md`: audit log and operator notes.

## Done when

The harness foundation is done when:

- Codex can start from the repo root and understand the repo is skills-only.
- Codex can distinguish implemented skill assets from target specs.
- `AGENTS.md` routes common requests into five clear modes.
- `AGENTS.md` stays concise enough to fit the project-doc budget and defers detailed run loops to `Implement.md`.
- Long tasks have a file-based startup/resume protocol.
- Validation commands and blocked-environment handling are explicit.
- `Status.md` is short enough to read at every session start.

## Assumptions

- User usually launches Codex from the repository root.
- Project-level `.codex/config.toml` is trusted and loaded by Codex in the user’s environment.
- MCP server paths are machine-specific and may need local adjustment.
- `COMPANY_RESEARCH_ROOT` may point to `/home/help/mcp/work/company_research` or another local artifact root.

## Open questions

- Should the old `CONTINUITY.md` be retired, or kept only for Claude-specific continuity?
- Should `company-foundation` be implemented next, or should `collect-company-facts` be renamed/migrated toward the target Skill 2 contract first?
- Should the runner dependency set be formalized in `requirements.txt` or a future `pyproject.toml`?
