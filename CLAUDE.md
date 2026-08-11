# CLAUDE.md — Claude Code adapter (monorepo root)

<!-- Shared project rules live in AGENTS.md; keep this file a thin Claude-only adapter. -->

@AGENTS.md

## Claude Code-specific rules

- Deeper `AGENTS.md` files load on demand through sibling `CLAUDE.md` symlinks. If discovery is uncertain, use
  `/memory` or a configured `InstructionsLoaded` hook to verify the active files before mutating.
- Project policy, intentionally stricter than Claude Code's general Auto Memory guidance: Auto Memory holds only
  durable user preferences and collaboration habits — never product semantics, credentials, task state,
  acceptance criteria, or volatile runtime facts.
- Use Claude Code settings/hooks for deterministic lifecycle or permission enforcement; repository prose
  states policy but does not replace executable controls.

## Compact instructions

- Preserve the exact current task and acceptance criteria, authorization boundary, current phase, changed paths,
  latest commit/test/review results, blockers, and exact next action.
- Preserve `Completed / do not repeat` separately from pending work, including stable evidence and invalidation
  conditions. Never turn a completed external or destructive action back into a pending action.
- After compact or resume, follow `docs/agent-workflow.md` §2 and reconcile the summary with HANDOFF and actual
  repository state before continuing.

## Claude subagents

- 主代理负责架构、语义决策、风险边界和最终验收。
- 规格与机械验收已明确时，才用 `.claude/agents/opus-executor.md` 做边界清晰的实现；大量临时调查可
  放入隔离子代理，并行写入只能使用独立 worktree。
- 仅在用户明确要求时使用自定义 Workflow 编排。
- 子代理结果只是待验证主张；主代理必须检查实际 diff，并重跑相关门禁后才能认定完成。
