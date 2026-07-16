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
