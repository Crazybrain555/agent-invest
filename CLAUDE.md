# Claude Code adapter

@AGENTS.md

## Claude-specific continuity

- Use `/context` to verify instruction loading. Descendant `CLAUDE.md` adapters load on demand; after compact or
  resume, re-read the applicable leaf before editing there.
- Durable product/task state belongs in tracked docs or the applicable HANDOFF, not Auto Memory. Never store
  credentials, acceptance criteria, runtime claims, or volatile service state in memory.
- After compact or resume, reconcile the current request with HANDOFF, Git/worktree truth, and any narrow
  read-only external observation required by the next action. Preserve `Completed / do not repeat` separately
  from pending work.

## Subagents

- The main agent owns semantics, authorization, architecture, safety, and final validation.
- Use `.claude/agents/opus-executor.md` only for a bounded implementation with explicit acceptance checks.
  A mutating executor runs in the foreground in the current checkout, or in a project-workflow worktree;
  never let the main agent and a subagent write the same checkout concurrently.
- Subagent results are unverified claims until the main agent inspects the diff and reruns the relevant gates.
