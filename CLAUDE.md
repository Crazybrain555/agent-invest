# CLAUDE.md — Claude Code adapter (monorepo root)

<!-- Maintainer note (stripped before injection): root AGENTS.md is the shared tool-neutral operating
     contract and is imported below (user decision 2026-07-16, superseding the earlier no-import decision;
     official guidance: code.claude.com/docs/en/memory "AGENTS.md" section). Component/nested CLAUDE.md
     files remain symlinks to their sibling AGENTS.md. Keep this file a thin Claude-only adapter; never
     duplicate shared semantics or hard boundaries here. -->

@AGENTS.md

## Claude Code-specific rules

- Deeper `AGENTS.md` files auto-load through their sibling CLAUDE.md symlinks as you work in those
  directories. When instruction discovery is uncertain, inspect the loaded instruction set before mutating
  rather than assuming a nested rule was loaded.
- Auto Memory holds only durable user preferences and collaboration habits — never product semantics,
  credentials, task state, acceptance criteria, or volatile runtime facts.
- Use Claude Code settings/hooks for deterministic lifecycle or permission enforcement; repository prose
  states policy but does not replace executable controls.

## 子代理配发规则（执行层路由；2026-07-17 自用户级 CLAUDE.md 迁入）

默认自动配发，无需口头触发；用户开口仅用于覆盖某一次的路由。

- **主循环亲自做**：方案设计、语义敏感修复（并发/可见性/契约/迁移/鉴权类）、公共 API 面、
  独立复审、硬骨头实现、第二实现/诊断视角、深度 root-cause。
- **默认执行子代理 `opus-executor`**（定义在 `.claude/agents/`，固定 Opus）：主循环把规格一次
  写全后交给它做边界清晰、验收可机械化的执行（代码修改、写测试、批量改写、模板文件、跑命令）。
  需要并行广度时，用标准 Agent 工具同时派多个 opus-executor，不用 Workflow 编排。子代理模型
  已由用户级 settings env `CLAUDE_CODE_SUBAGENT_MODEL=opus` 机械兜底为 Opus。
- **Workflow（JS 多代理编排）仅在用户显式要求时使用**（ultracode 或明说）；其扇出同受
  `CLAUDE_CODE_SUBAGENT_MODEL=opus` 兜底，执行类 stage 仍显式 `agentType: 'opus-executor'`
  以带上执行契约（不 commit、如实报告等）。
- **Codex 不作默认路由**：openai-codex 插件仅在用户点名（`/codex:rescue` 或「让 Codex 看看」）
  时作跨厂商第二意见。
- 任何执行代理报「完成」都不可信转述：主循环必须实物核查（git diff、真实重跑验收命令）后
  才认账——执行代理曾虚构过整批交付，这条纪律始终挂着。
