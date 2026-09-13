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

- The main agent owns semantics, authorization, architecture, safety, and final validation.
- Use `.claude/agents/opus-executor.md` only for a bounded implementation with explicit acceptance checks.
  A mutating executor runs in the foreground in the current checkout, or in a project-workflow worktree;
  never let the main agent and a subagent write the same checkout concurrently.
- Subagent results are unverified claims until the main agent inspects the diff and reruns the relevant gates.
