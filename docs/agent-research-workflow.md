# Pre-design research workflow

This is the shared, tool-neutral research protocol referenced by root `AGENTS.md` §3. It defines when the
research gate triggers, what evidence passes it, when it stops, and how conflicts resolve. Task-state and
runtime protocol live in `docs/agent-workflow.md`.

## 1. Trigger

The gate applies to every change that may alter normative semantics or observable application behavior. It
always triggers; only the external-research step may be skipped (§2).

Observable application behavior includes: accepted/rejected inputs; produced outputs; persisted state;
emitted events (outbox/change feed); public API/CLI semantics; state transitions; scheduling, retry,
timeout, and idempotency behavior; error classification and failure visibility; authorization; migration
semantics; and configuration that changes runtime behavior. A test that encodes an expectation no existing
contract confirms is defining behavior and passes the gate too.

Evidence-only inspection, reproduction, and a failing regression test that encodes an already-authoritative
contract may precede external research when they establish no new semantics.

## 2. Permitted skip of external research

Every gated change still produces a before-edit record (§5); skipping affects only the external-research
step, never the gate. External research may be skipped only when the change is provably design-neutral, so
external evidence cannot affect the design: formatting, spelling, comments, a documentation correction of an
already-established fact, a pure mechanical rename with all references and tests updated, or a refactor whose
behavior equivalence is demonstrated by existing characterization tests. Record the skip reason and the
equivalence evidence — the mere existence of tests, an obvious-looking bug, agent confidence, time pressure,
or an existing fixture is not sufficient.

## 3. Evidence by question

- Product intent: the user's current request/acceptance criteria and the repository's normative contracts.
- External mechanism contract: version-matched official specification, documentation, release notes, and
  source for the dependency actually deployed.
- Current behavior: code, schemas, runtime commands, raw artifacts, and representative real data.
- Design alternatives: maintainer design records, issues/PRs, and mature implementations.
- Discovery tools (Context7, DeepWiki, GitHub/web search, DBHub) locate and summarize evidence; their
  summaries are never authorities by themselves.

## 4. Depth and stop condition

- Routine, contract-determined fix: inspect the local authority and the relevant official external contract.
- Non-novel design choice: add 1–2 mature analogues.
- Material architecture, cross-service contract, dependency, provider framework, security, migration, or
  ops change: compare 2–4 independent relevant sources or implementations, including at least one competing
  approach.
- Domain-specific work follows the nearest component `AGENTS.md` research-source requirements.

Source count is not the goal. Stop when the local authority, a representative case, the governing external
contract, the adopted invariant, the main rejected alternative, and the validation plan are settled.

## 5. Before-edit record

Record in a progress update for short tasks, or in the HANDOFF `research/decision evidence` field for
durable ones. Long-lived conclusions belong in tracked architecture/contract docs, not task records.

Always:

    Research question:
    Governing authority:
    Invariant adopted / alternative rejected:
    Validation plan:
    Skip reason:            # only when external research was skipped

Material changes additionally:

    Behavior boundary:      # which observable behaviors the change may touch
    Failure family:         # e.g. cross-page table continuation, provider retry classification, stale projection
    Fail-closed boundary:   # what must not be guessed or synthesized when evidence is insufficient
    Analogues compared:

## 6. Conflict resolution

- Implementation vs repository contract: implementation drift—fix toward the contract.
- Mature analogue vs repository contract: repository product semantics prevail; record why the external
  design was not adopted.
- Official external contract vs implementation: the implementation conforms to the actually deployed version.
- Official external contract vs repository product contract: never silently pick one; identify the required
  contract revision or adapter, and obtain user authorization when product semantics would change.
- Conflicting official versions: anchor to the deployed version; verify with a minimal reproduction if needed.
- Within conflict resolution, escalate to the user only when product semantics, acceptance criteria, or
  authorization boundaries must change; broader ask-before duties in root `AGENTS.md` §3 are unaffected.

## 7. Validation

Validate the adopted invariant with a representative positive case, an adjacent negative case, and the
applicable real boundary. Never encode document-ID patches, isolated phrase fixes, copied project internals,
or synthetic defaults without provenance.
