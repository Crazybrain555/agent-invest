# Prompt.md — Quanti Durable Task Specification

This file freezes the standing target for long-horizon Codex work in this repository. For a new major task, update the task-specific sections before implementation begins, then regenerate or update `Plan.md`.

## Standing repository purpose

Quanti is a skills-only workspace for evidence-driven company research and valuation. The target system decomposes company analysis into reusable, independently rerunnable skills that produce traceable artifacts for the formula:

```text
valuation = sustainable economic profit x quality coefficient
```

The system should answer:

1. What is the company's future sustainable economic profit?
2. How certain are we, based on traceable evidence rather than subjective scoring?
3. What valuation range and margin of safety follow from that evidence?

## Current repository scope

This repository currently contains:

- shared runtime helpers in `company_research_runtime/`,
- one implemented in-repo runner: `.agents/skills/company_research/collect-company-facts/scripts/run.py`,
- a `collect-company-facts` skill definition,
- target architecture and per-skill specifications under `docs/skills/`,
- MCP configuration and setup documentation,
- Codex durable workflow files under `docs/agent/`,
- and a read-only reviewer subagent config under `.codex/agents/quanti_reviewer.toml`.

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

Current task: Stabilize the Codex-native harness and the first in-repo runner contract for Quanti.

Purpose: keep Codex easy to operate from natural-language prompts, preserve durable project memory under `docs/agent/`, align docs with repo reality, and harden the current `collect-company-facts` runner contract before adding more in-repo skills.

Goals:

- Keep `AGENTS.md`, `docs/agent/*`, and reviewer policy files consistent.
- Use a three-mode router in `AGENTS.md`:
  1. Quick standalone task.
  2. Durable workflow.
  3. Harness or agent-policy maintenance.
- Route durable work by task relationship inside Mode 2:
  - new durable task,
  - revise current durable task,
  - execute current durable task.
- Treat continuation/recovery as reading `Status.md`, `Plan.md`, and `Implement.md`, not as a separate top-level mode.
- Keep `AGENTS.md` focused on routing, hard rules, and stable repository facts.
- Keep detailed execution steps in `docs/agent/Implement.md` rather than repeating them in `AGENTS.md`.
- Keep `Plan.md` as the source of truth for milestones, progress, active working checklist, validation, and risks.
- Keep `Status.md` short enough for session start and continuation.
- Keep `Documentation.md` as the longer audit and decision log.
- Keep human-facing docs aligned with the actual trimmed repo state.
- Make the current `collect-company-facts` runner behavior explicit around blocked dependencies, validation, demo semantics, and artifact outputs.
- Add an explicit independent review gate through `docs/agent/code_review.md` and the read-only `quanti_reviewer` subagent.

Non-goals:

- Do not add hooks, write-capable subagents, or new skill runners in this phase.
- Do not broaden into full multi-skill implementation before the current runner contract is stable.
- Do not overfit `AGENTS.md` to one prompt template.
- Do not claim that Codex can silently run `/review` on the user's behalf.

## Deliverables

- `.codex/config.toml`: project-scoped Codex behavior and MCP config.
- `AGENTS.md`: concise operating contract and three-mode router.
- `docs/agent/Prompt.md`: standing goals plus current durable task brief.
- `docs/agent/Plan.md`: current milestones, progress, active checklist, and validation.
- `docs/agent/Status.md`: short continuation state.
- `docs/agent/Implement.md`: execution runbook.
- `docs/agent/Documentation.md`: audit log and operator notes.
- `docs/agent/code_review.md`: independent review rubric and materiality gate.
- `.codex/agents/quanti_reviewer.toml`: read-only reviewer subagent definition.

## Done when

This stabilization phase is done when:

- Codex can start from the repo root and understand the repo is skills-only.
- Codex can distinguish implemented skill assets from target specs.
- `AGENTS.md` routes common requests into the three active top-level modes above.
- Durable work uses task-relationship handling and file-based continuation instead of a separate resume mode.
- `AGENTS.md` stays concise enough to fit the project-doc budget and defers detailed run loops to `Implement.md`.
- `code_review.md` and `.codex/agents/quanti_reviewer.toml` define a usable independent review gate.
- Human docs no longer imply missing repo paths or obsolete continuity files are active.
- The current `collect-company-facts` runner contract is explicit about dependencies, blocked behavior, validation, demo semantics, and output locations.
- Long tasks have a file-based startup and continuation protocol.
- Validation commands and blocked-environment handling are explicit.
- `Status.md` is short enough to read at every session start.

## Assumptions

- User usually launches Codex from the repository root.
- Project-level `.codex/config.toml` is trusted and loaded by Codex in the user's environment.
- MCP server paths are machine-specific and may need local adjustment.
- `COMPANY_RESEARCH_ROOT` may point to `/home/help/mcp/work/company_research` or another local artifact root.

## Open questions

- Should `company-foundation` be implemented next, or should `collect-company-facts` be renamed or migrated toward the target Skill 2 contract first?
- Should the runner dependency set be formalized in `requirements.txt` or a future `pyproject.toml`?
- Should `--demo` remain dependent on `company.yaml.cik`, or should M2 add a true dependency-light demonstration path?
