# Independent Review Gate

This file defines the review rubric for future durable milestones. Reference it from `AGENTS.md` and use it for `/review` or the read-only `quanti_reviewer` subagent.

The purpose of review is to catch material issues that could make a milestone unsafe, misleading, unverifiable, or inconsistent with the durable workflow.

## When review is required

Run this gate before marking a durable milestone complete when the milestone changed any of the following:

- runtime code or runner behavior,
- setup instructions, user-facing commands, or MCP setup docs,
- validation commands or test expectations,
- agent policy, `.codex/*`, `.claude/settings.local.json`, `.mcp.json`, `docs/agent/*`, or future hooks/skills,
- implementation-status claims about what exists today versus what has been removed.

Review may be skipped only for trivial quick tasks that do not change files, or tiny typo fixes that cannot affect commands, setup, contracts, status, or user behavior. If skipped for a durable milestone, record the reason according to the active task policy.

## Reviewer independence

The reviewer must be as context-independent as Codex allows:

- Use `/review` manually when the user triggers it.
- Otherwise explicitly spawn the read-only `quanti_reviewer` subagent.
- The reviewer must not edit files.
- The reviewer must not update durable state.
- The reviewer should not read the main agent's full conversational history or self-justification.
- The reviewer should base findings only on the bounded inputs below.

## Bounded reviewer inputs

The reviewer should inspect only:

- `AGENTS.md`,
- `CLAUDE.md`,
- `docs/agent/Prompt.md`,
- the active milestone, `Progress`, `Surprises & Discoveries`, `Decision log`, and working checklist in `docs/agent/Plan.md`,
- `docs/agent/Status.md`,
- `docs/agent/Implement.md`,
- this file,
- current `git diff` / changed files,
- relevant validation output,
- relevant source/config files touched by the diff.

Avoid unrelated archives, raw artifacts, old transcripts, and broad repository scanning unless a finding cannot be validated without them.

## Materiality gate

Report a finding only if all of these are true:

1. It is introduced or worsened by the current milestone/diff.
2. It is actionable with a clear minimal fix.
3. It is supported by file, line, command, diff, or validation evidence.
4. The original implementer would likely want to fix it before calling the milestone complete.
5. It can materially affect runtime correctness, validation reliability, durable workflow reliability, setup accuracy, security, data loss risk, or claims about what exists in the checkout.

If uncertain, do not report the finding.

## Acceptance-oriented review

Before reporting a finding, ask:

- Does this prevent the active milestone acceptance criteria from being true?
- Would a user following the documented command or setup step fail or be materially misled?
- Does it make the durable workflow harder to resume or objectively verify?
- Does it expose credentials or encourage hard-coding secrets?
- Does it contradict the current task boundary?

If the answer is "no" to all of these, do not report it.

## Do not report

Do not report:

- style-only comments,
- minor prose polish,
- speculative architecture preferences,
- broad cleanup unrelated to the active milestone,
- pre-existing issues not worsened by this diff,
- issues already clearly recorded as current blockers, unless the diff made them worse,
- missing tests for docs-only or harness-only changes unless the change affects validation commands, setup, credentials, or future agent behavior,
- duplicate comments already covered by deterministic validation.

## Severity

Use these severities:

- `high`: likely broken runtime behavior, data/evidence corruption, destructive risk, secret leakage, security risk, or false completion of a milestone.
- `medium`: setup/command guidance is materially misleading, validation is unreliable, durable workflow can drift, or implementation-status claims are wrong.
- `low`: real and actionable but low impact; fix if cheap, otherwise record as follow-up.

Do not output nit-level comments.

## Output format

Return at most five findings, ordered by severity.

For each finding include:

```text
Severity: high | medium | low
File/lines: path:start-end
Evidence: <short concrete evidence>
Why it matters: <impact>
Minimal fix: <smallest reasonable fix>
```

Then include:

```text
Overall verdict: pass | pass_with_items | fail
Confidence: high | medium | low
```

If no material findings exist, say:

```text
No material findings.
Overall verdict: pass
Confidence: <high|medium|low>
```

## How the main agent should use review output

Reviewer output is a candidate report, not accepted truth.

The main agent must:

1. Accept only material, evidence-backed findings.
2. Resolve high/medium material findings before marking the milestone complete unless the user explicitly defers them.
3. Resolve low material findings only when cheap and in-scope, otherwise record them as follow-up if useful.
4. Ignore rejected nitpicks.
5. Record review handling according to the active task policy.
