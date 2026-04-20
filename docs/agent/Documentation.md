# Documentation.md — Quanti Agent Audit Log and Operator Notes

This file is durable audit memory for long-horizon Codex work. It may grow over time. Keep `Status.md` short; put long history, decisions, run instructions, and known issues here.

## Current feature / task overview

The Codex-native harness foundation has now been applied in this repository, simplified to a 5-mode task router, and synchronized with the exported overlay archive.

The repository is a company research / valuation skills workspace. Its target architecture is a 9-skill chain that produces evidence-backed valuation outputs, but the current checkout only has one implemented in-repo runner: `collect-company-facts`.

The applied harness deliberately uses native Codex mechanisms first:

- `.codex/config.toml` for session-level behavior and machine-specific MCP configuration.
- `AGENTS.md` for concise repo operating policy and task routing.
- `docs/agent/Prompt.md` for goals and constraints.
- `docs/agent/Plan.md` for milestones and validation.
- `docs/agent/Implement.md` for the execution loop.
- `docs/agent/Status.md` for short resume state.
- this file for audit history and operator notes.

No hooks, subagents, or new skills are part of the first phase.

The active next step is `M2 — Harden collect-company-facts`.

## How to prompt Codex for this repo

The user does not need to write rigid workflow prompts. Natural prompts are expected; `AGENTS.md` routes them.

### Quick task

```text
Check whether README mentions any runner command that does not exist. Do not edit yet; just report findings.
```

### New durable task / plan first

```text
I want to implement company-foundation next. First inspect the relevant specs and current repo status, then update Prompt.md, Plan.md, and Status.md. Do not write runtime code yet.
```

### Execute approved milestone

```text
Continue the durable workflow. Execute the active milestone only, validate it, and update Status.md and Documentation.md before reporting back.
```

### Resume

```text
Resume from docs/agent/Status.md. Tell me the current milestone, next action, blockers, and latest validation; then continue with the next safe action.
```

### Harness maintenance

```text
Tighten AGENTS.md routing without changing product code. Validate config/docs consistency and update Status.md/Documentation.md.
```

## How to validate the harness files

After changing the harness foundation, parse the Codex config:

```text
python - <<'PY'
import pathlib
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
p = pathlib.Path('.codex/config.toml')
tomllib.loads(p.read_text(encoding='utf-8'))
print('config.toml parses')
PY
```

Check routing and durable docs:

```text
rg -n 'Mode 1|Mode 2|Mode 3|Mode 4|Mode 5' AGENTS.md
find docs/agent -maxdepth 1 -type f | sort
```

After changing Python runtime or runner code:

```text
python -m compileall company_research_runtime .agents/skills/company_research/collect-company-facts/scripts/run.py
python .agents/skills/company_research/collect-company-facts/scripts/run.py --help
```

Optional functional check when a valid company artifact exists:

```text
python .agents/skills/company_research/collect-company-facts/scripts/run.py AAPL --demo
```

## Important repository facts

- Current actual skill runner: `.agents/skills/company_research/collect-company-facts/scripts/run.py`.
- Current actual skill definition: `.agents/skills/company_research/collect-company-facts/SKILL.md`.
- Shared runtime: `company_research_runtime/`.
- Target architecture docs: `docs/skills/MASTER_PLAN.md` and `docs/skills/specs/`.
- Implementation status index: `docs/skills/README.md`.
- Production artifact root: `${COMPANY_RESEARCH_ROOT:-/home/help/mcp/work/company_research}`.
- `raw/` evidence is append-only.

## Decisions

### 2026-04-20 — Keep `AGENTS.md` concise and push detailed execution to `Implement.md`

Decision: Keep `AGENTS.md` focused on stable repository facts, routing, validation entrypoints, and hard rules; keep the detailed per-mode execution loop in `docs/agent/Implement.md`.

Rationale: Recent OpenAI/Codex guidance and public OpenAI repository examples consistently treat `AGENTS.md` as a concise project guide, while detailed execution, verification, or living-plan behavior is delegated to dedicated runbooks or plan files. This reduces instruction load and makes routing easier for the model.

Consequences: Future harness edits should resist turning `AGENTS.md` into a second `Implement.md`. If routing or policy changes, update both files together and preserve the separation of responsibilities.

### 2026-04-20 — Retire `CONTINUITY.md` from the active workflow

Decision: Treat `docs/agent/Status.md` as the canonical resume file for current long-horizon work and stop documenting `CONTINUITY.md` as an active repo requirement.

Rationale: The file is already absent from the current worktree, while the new durable workflow is present and in active use. Restoring `CONTINUITY.md` implicitly would create a second, conflicting state source.

Consequences: `README.md` and `CLAUDE.md` now point agents to `docs/agent/Status.md`, `Plan.md`, and `Implement.md`. If a Claude-specific continuity ledger is ever needed again, it should be reintroduced explicitly rather than inferred from stale docs.

### 2026-04-20 — Simplify task routing to five modes

Decision: Replace the earlier fine-grained router with five primary modes: quick task, plan/replan durable work, execute approved milestone, resume durable work, and harness/policy maintenance.

Rationale: Too many modes make routing itself a cognitive burden for the model. Five modes preserve the important control points while keeping `AGENTS.md` easier to follow.

Consequences: Simple read-only inspection and simple bounded edits are merged into Mode 1. Plan creation and plan revision are merged into Mode 2. Review/audit-only work should usually use Mode 1 unless it belongs to an active durable milestone.

### 2026-04-20 — Apply the harness foundation in the live repo

Decision: Land the overlay into the real repository and immediately promote the active milestone to `M1 — Repo reality reconciliation`.

Rationale: The durable workflow files only become useful once they are the in-repo source of truth. Leaving them in a pre-application state would make the status docs stale on day one.

Consequences: Future Codex runs should treat M0 as complete and start from M1 unless the user explicitly reopens the foundation milestone.

### 2026-04-20 — Raise reasoning effort, but keep verbosity within valid bounds

Decision: Set `model_reasoning_effort = "xhigh"` and `plan_mode_reasoning_effort = "xhigh"`, while setting `model_verbosity = "high"`.

Rationale: The user wants maximum reasoning effort. OpenAI's Codex config reference supports `xhigh` for reasoning effort on supported models, but `model_verbosity` only supports `low | medium | high`.

Consequences: Codex should think harder by default, while response detail is increased to the highest supported verbosity rather than an invalid `xhigh` value.

### 2026-04-20 — Keep Context7 environment-backed in Codex config

Decision: Keep `env_http_headers = { "CONTEXT7_API_KEY" = "CONTEXT7_API_KEY" }` in `.codex/config.toml`.

Rationale: Repo operating rules prefer environment-backed secrets over hard-coded API keys, even in machine-local config files.

Consequences: If Context7 is needed in a session, the launching shell or app environment must provide `CONTEXT7_API_KEY`.

### 2026-04-19 — Use `docs/agent/` as Codex durable memory

Decision: Add a dedicated `docs/agent/` directory for Codex long-horizon state.

Rationale: The existing `CONTINUITY.md` appears to be a session-specific continuity ledger and currently describes a completed git cleanup task. A dedicated durable workflow directory gives Codex a cleaner state source for future work.

Consequences: Future Codex sessions should read `docs/agent/Status.md` first. A later cleanup milestone should decide whether to retire, rewrite, or keep `CONTINUITY.md` for Claude-only workflows.

### 2026-04-19 — Keep the first phase native and minimal

Decision: Start with `.codex/config.toml`, `AGENTS.md`, and durable markdown state only.

Rationale: Hooks, subagents, and new skills add complexity. The first goal is to observe how well native Codex follows a concise policy plus file-based state.

Consequences: If Codex repeatedly forgets to update `Status.md` or run validation, add hooks in a later phase.

## Validation history

| Date | Milestone | Command | Result | Notes |
| --- | --- | --- | --- | --- |
| 2026-04-19 | M0 | Static archive inspection | pass | Repository shape, docs, config, and runner inspected. |
| 2026-04-19 | M0 | Generated overlay files | pass | No product code changed. |
| 2026-04-20 | M0 | `python - <<'PY' ... tomllib ... PY` | pass | The applied `.codex/config.toml` parses successfully. |
| 2026-04-20 | M0 | `find docs/agent -maxdepth 1 -type f \| sort` | pass | Confirmed all 5 durable workflow docs exist. |
| 2026-04-20 | M0 | `rg -n 'Mode 1\|Mode 2\|Mode 3\|Mode 4\|Mode 5\|Keep this file concise' AGENTS.md` | pass | Confirmed simplified 5-mode router exists and that detailed execution is delegated to `Implement.md`. |
| 2026-04-20 | M0 | `zipinfo -1 quanti_codex_harness_final_overlay.zip \| sort` | pass | Confirmed the rebuilt archive contains only the intended overlay files. |
| 2026-04-20 | M0 | overlay content sync check | pass | Confirmed the archive matches the live repo byte-for-byte for the exported harness files. |
| 2026-04-20 | M0 | `codex --version` | pass | Local CLI available as `codex-cli 0.121.0`. |
| 2026-04-20 | M0 | `codex exec --ephemeral --sandbox read-only ...` | inconclusive | CLI session stalled after external `403 Forbidden` warnings during plugin/analytics traffic and produced no final answer. |
| 2026-04-20 | M1 | `find . -maxdepth 4 -type f \( -name '*.md' -o -name '*.py' \) \| sort` | pass | Confirmed the trimmed repo shape used for doc reconciliation. |
| 2026-04-20 | M1 | `rg -n "docs/MASTER_PLAN\|extract-xbrl-timeseries\|company-foundation/scripts/run.py\|valuation-and-margin-of-safety/scripts/run.py\|CONTINUITY" README.md AGENTS.md CLAUDE.md docs \|\| true` | pass | Active entry docs were cleaned; remaining matches are deliberate retirement notes, historical aliases in references, or archive/history files. |
| 2026-04-20 | M1 | `python .agents/skills/company_research/collect-company-facts/scripts/run.py --help` | fail | Local environment lacks `PyYAML`, so even CLI help is currently blocked on dependency setup. |

## Known issues

| Issue | Impact | Workaround / next step |
| --- | --- | --- |
| Non-interactive `codex exec` smoke test was inconclusive in this shell | Could not fully verify the first-prompt behavior from CLI automation | Retry from the normal Codex app/CLI environment after confirming auth/session health; M0 file-level validation is still complete. |
| Existing `.mcp.json` in uploaded archive contained a hard-coded Context7 key | Secret hygiene risk if shared | Prefer env vars; consider rotating the key and updating `.mcp.json` separately. |
| `collect-company-facts --demo` still appears to require `company.yaml.cik` | A demo run may block unexpectedly | Document current behavior or modify intentionally in M2. |
| `requirements.txt` is mostly commented and may not install all imports used by runners | Fresh setup may fail on missing packages such as YAML support | Decide in a later milestone whether to formalize dependencies. |

## Follow-ups

- [x] Apply overlay files to the real repo.
- [x] Simplify task routing to five modes.
- [ ] Export `CONTEXT7_API_KEY` locally if Context7 MCP is needed.
- [x] Run TOML parse validation.
- [x] Ask Codex to perform M1 repo reality reconciliation.
- [x] Retire `CONTINUITY.md` from the active workflow in favor of `docs/agent/Status.md`.
- [ ] Start M2 on `collect-company-facts` blocked/demo/runtime contract hardening.
