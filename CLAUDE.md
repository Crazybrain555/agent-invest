# Claude Code adapter

@AGENTS.md

## Claude-specific continuity

- Use `/context` when instruction loading is uncertain. Descendant `CLAUDE.md` adapters load on demand;
  read the applicable leaf before its first edit if it has not already been loaded.
- Durable product/task state belongs in tracked docs or the applicable HANDOFF, not Auto Memory. Never store
  credentials, acceptance criteria, runtime claims, or volatile service state in memory.
- Recover after compact or resume only when history is incomplete, unclear or conflicts with current state,
  following `docs/agent-workflow.md`. With intact context, continue without repeating completed reads or checks.
  Preserve `Completed / do not repeat` separately from pending work.
- When compacting, keep verbatim: the user's authorizations and their limits, open HANDOFF obligations, the
  exact commands run with their results (credentials and raw datasets redacted), touched files, open blockers,
  and the `Completed / do not repeat` list.

## Subagents

- The main agent owns semantics, authorization, architecture, safety, and final validation. A Fable main agent
  additionally keeps planning, review, semantics-sensitive repair, and the hard cases: cross-service or
  public-contract changes, migration semantics, product-semantics disputes, incident diagnosis, and any task
  whose acceptance criteria cannot yet be written down. It plans before dispatch and reads the returned diff
  line by line; at that tier the main agent is never a pass-through dispatcher.
- A non-Fable session treats those cases as reserved for a Fable session: it completes the authorized work
  outside them and reports the remainder as a hand-back, never delegating it downward. A model name never
  transfers the checkout, the HANDOFF, or writer identity.
- The built-in `Explore` and `Plan` subagents skip `CLAUDE.md`, so they never load this contract or `AGENTS.md`;
  use them for lookup, never to settle a plan or to stand in for review.
- A mutating executor takes bounded execution only: a spec carrying explicit acceptance commands, no open
  judgment call, and a deletion list — which code and tests to drop — already settled against the tree. A
  judgment the spec does not cover, such as product semantics, a public contract, authorization, or scope,
  stops execution and returns to the main agent with the conflicting evidence (file:line); routine
  implementation choices inside the spec are resolved from the contracts and reported with the result.
- Dispatch by agent name, not filename: `opus-executor` from `.claude/agents/opus-executor.md`, or the nearest
  component's executor — for disclosure_anchor that is `disclosure-anchor-opus-executor`. A mutating executor
  runs in the foreground in the current checkout, or in a project-workflow worktree; never let the main agent
  and a subagent write the same checkout concurrently, and dispatch several at once only when each is read-only
  or holds its own worktree.
- Name the model when dispatching an agent whose definition does not fix one; an omitted model there falls
  through `CLAUDE_CODE_SUBAGENT_MODEL` and need not land on the session's tier. Executors fix their model in
  their definitions; do not override it per call. Leave `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` unset — it erases
  per-call and per-definition model choice.
- Ordinary parallelism dispatches executors through the standard Agent tool. Workflow is not the default
  fan-out; use it only when the user opts into ultracode or asks for a workflow. Codex is an on-demand
  cross-vendor second opinion, never auto-assigned.
- An implementation author never owns its own acceptance verdict: it runs the acceptance commands it was given
  and reports the exact output, which is evidence, not a verdict. Where AGENTS.md requires an independent
  read-only review, it goes to a read-only agent that wrote neither the change nor its spec — never an
  executor; the main agent's own diff read is verification, not that review. Findings are claims to verify
  and the verdict stays with the main agent. Subagent results are unverified claims until the main agent
  inspects the diff and reruns the relevant gates — confirm the touched files exist, read `git diff`, rerun
  the gate.
