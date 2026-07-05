# CLAUDE.md - Claude Code Agent Guide (monorepo root)

**Scope:** root-level guidance only. Repo-wide policy lives in `AGENTS.md` (this directory); each service
carries its own `AGENTS.md`/`CLAUDE.md` which win inside their subtree (nearest file wins).

Always reply to the user in Chinese unless the user asks otherwise or the content is code/API text. Do not
turn normal answers into long operation manuals.

## Working in this repo

1. Cross-service or repo-structure work: follow root `AGENTS.md`.
2. Work on a specific service: `cd` mentally into it — read that service's `AGENTS.md`/`CLAUDE.md` and its
   `docs/agent/` durable state first. The live service today is `services/disclosure_anchor/`.
3. Durable working memory is per-service (`services/<svc>/docs/agent/`, gitignored). The root keeps no
   `docs/agent/`; if cross-service durable state becomes necessary, propose it to the user first.
4. `docs/archive/pre-restart/` is frozen Quant_agent-era history — read on demand only, never as policy.
5. The engine protocol (v0.7) lives at
   `docs/reference/投研预测引擎顶层框架协议_v0.7修订最终版本.md`.

## Preferences (inherited from disclosure_anchor practice)

1. Keep responses concise; smallest direct change; let unexpected failures surface.
2. Before multi-file or risky edits, give a short plan (key assumption, file list, validation target).
3. After code changes, update or add the relevant tests; contract changes update the matching spec/docs.
4. If the user corrects a workflow rule, propose a concrete `CLAUDE.md`/`AGENTS.md` update before treating
   it as permanent.
