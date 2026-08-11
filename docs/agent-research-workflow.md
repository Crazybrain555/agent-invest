# Pre-design research workflow

This is the shared, tool-neutral research protocol referenced by root `AGENTS.md` §3. It defines when the
research gate triggers, what evidence passes it, when it stops, and how conflicts resolve. Task-state and
runtime protocol live in `docs/agent-workflow.md`.

## 1. Trigger

The gate applies to every decision or change that may alter normative semantics or observable application
behavior. It always requires the governing local contract, current implementation, and a representative real
case when one is available and applicable. External research is required when the decision depends on an
external mechanism, has material or unresolved design alternatives, or a nearer component rule or the user
requires it. Reuse research already closed in the active task when the question, source version, and deployed
identity have not changed.

Observable application behavior includes: accepted/rejected inputs; produced outputs; persisted state;
emitted events (outbox/change feed); public API/CLI semantics; state transitions; scheduling, retry,
timeout, and idempotency behavior; error classification and failure visibility; authorization; migration
semantics; and configuration that changes runtime behavior. A test that encodes an expectation no existing
contract confirms is defining behavior and passes the gate too.

Evidence-only inspection, reproduction, and a failing regression test that encodes an already-authoritative
contract may precede external research when they establish no new semantics.

The gate is keyed to decisions, not just code edits: removals, simplifications, tool choices, and operational
designs count when they change behavior. User authorization sets scope but does not replace relevant mechanism
research. When external evidence conflicts with the intended product semantics, surface the conflict before
executing; do not browse merely to satisfy a source count after the decision is already closed. An explicit
no-network instruction controls the research method: use available local/attached evidence and report the
resulting limitation instead of browsing.

## 2. Permitted skip of external research

Every gated change still produces a before-edit record (§5); skipping affects only the external step. It may be
skipped when external evidence cannot materially change the decision: a design-neutral edit; a local correction
fully determined by an authoritative repository contract with no external mechanism involved; or the same
question already closed in the active task against unchanged source and runtime identities. Record the reason
and supporting contract or equivalence evidence. Agent confidence, time pressure, or a convenient fixture is
not evidence.

## 3. Evidence by question

- Product intent: the user's current request/acceptance criteria and the repository's normative contracts.
- External mechanism contract: version-matched official specification, documentation, release notes, and
  source for the dependency actually deployed.
- Current behavior: code, schemas, runtime commands, raw artifacts, and representative real data.
- Design alternatives: maintainer design records, issues/PRs, and mature implementations.
- Tools do not create a new evidence class; classify the underlying source. DeepWiki and search snippets supply
  code maps or hypotheses. Context7 locates candidate or version-relevant documentation; the official page and
  version remain the evidence. A GitHub exact file proves only that repository/ref/path; issues/PRs and
  default-branch files do not establish released or deployed behavior. A read-only DBHub result is direct
  current-behavior evidence only for its recorded environment, query, and observation time. Two tools exposing
  the same upstream artifact are one lineage, not independent corroboration.
- For behavior claims from upstream code, record the repository, exact source path and immutable commit SHA,
  plus release/tag association and issue/PR state when relevant; verify its mapping to the locally deployed
  version. Distinguish representational capacity from implemented behavior: a data model's ability to express an
  outcome does not prove that a detection, linking, continuation, or reconstruction algorithm exists.

## 4. Depth and stop condition

- Routine, contract-determined fix: inspect the local authority and, when an external mechanism is involved,
  the relevant official external contract.
- Non-novel design choice: add 1–2 mature analogues.
- Material architecture, cross-service contract, dependency, provider framework, security, migration, or
  ops change: compare 2–4 independent relevant sources or implementations, including at least one competing
  approach.
- Domain-specific work follows the nearest component `AGENTS.md` research-source requirements.

Source count is not the goal. Stop when the local authority, an available applicable representative case, any
governing external contract, the adopted invariant, the main rejected alternative, and the validation plan are
settled.

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
- Discovery summary vs primary artifact: the primary artifact at the relevant ref wins; correct the claim
  before design.
- Upstream main, issue, or PR vs deployed runtime: match merged, released, and deployed identities; otherwise
  the upstream material remains comparative evidence.
- Within conflict resolution, escalate to the user only when product semantics, acceptance criteria, or
  authorization boundaries must change; broader ask-before duties in root `AGENTS.md` §3 are unaffected.

## 7. Validation

Validate the adopted invariant with a representative positive case, an adjacent negative case, and the
applicable real boundary. Never encode document-ID patches, isolated phrase fixes, copied project internals,
or synthetic defaults without provenance.
