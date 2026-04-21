# Implement.md — Quanti Durable Execution Runbook

This file defines how Codex should execute durable work in this repository. `AGENTS.md` handles routing and policy; this file handles detailed execution behavior.

## Core principle

Durable work should be recoverable from files, not from chat history.

A fresh session should be able to read:

1. `AGENTS.md`
2. `docs/agent/Status.md`
3. `docs/agent/Plan.md`
4. this file

and continue safely without relying on the previous conversation.

## Request relationship protocol

Before editing, decide how the current user request relates to durable state.

### Current durable task continuation

Use when the user says “continue”, “next”, “resume”, “current milestone”, “keep going”, or otherwise points to existing state.

Protocol:

1. Read `Status.md` first.
2. Read the active milestone, `Progress`, and active working checklist in `Plan.md`.
3. State the current milestone, next action, blockers, latest validation, and review state briefly.
4. Continue only if the next action is safe and unambiguous.
5. If state is incomplete or contradictory, update `Status.md` and/or `Plan.md` before proceeding.

### Current durable task revision

Use when the user interrupts, adds a new idea, rejects the current direction, changes priority, or points out that something is wrong.

Protocol:

1. Treat the user’s latest instruction as the current steering signal.
2. Decide the smallest durable state update required:
   - Update `Prompt.md` if the task goal, non-goal, hard constraint, deliverable, or done criteria changed.
   - Update `Plan.md` if milestones, active checklist/todos, acceptance criteria, validation commands, risk, or execution order changed.
   - Update `Status.md` if current milestone, next action, blockers, validation, or review state changed.
   - Update `Documentation.md` if the change is a decision future sessions need.
3. Do not keep implementing against a plan the user just invalidated.
4. If the user’s revision creates ambiguity that blocks implementation, record the blocker and ask only the minimum needed question.

### New durable task

Use when the user introduces a materially new goal rather than continuing or revising the active task.

Protocol:

1. Preserve any useful handoff from the previous task in `Documentation.md` before replacing current-task state.
2. Reinitialize current-task sections of `Prompt.md`, `Plan.md`, and `Status.md`.
3. Draft a milestone-based plan with acceptance criteria and validation commands.
4. Do not edit runtime/product code until the new plan is clear and accepted, unless the user explicitly asks to plan and implement in the same turn.

## Durable execution loop

For the active milestone:

1. Confirm the current milestone and active todo/checklist item from `Plan.md`.
2. Identify the minimal files needed.
3. Inspect code with targeted search before opening broad files.
4. Make the smallest coherent implementation or doc change.
5. Update the active working checklist in `Plan.md` as steps complete, split, or become obsolete.
6. Run the milestone validation commands.
7. If validation fails, repair the current milestone or mark it blocked before moving on.
8. Run the independent review gate when required.
9. Fix accepted material findings or record them as explicit follow-ups/blockers.
10. Update `Status.md`.
11. Update `Documentation.md` if decisions, commands, behavior, validation history, review history, or known issues changed.
12. Do not move to the next milestone unless the user asked for multi-milestone execution.

## Active working checklist discipline

`Plan.md` contains both large milestones and a smaller active working checklist.

Use the checklist for small dynamic steps such as:

- inspect specific file/path,
- verify command behavior,
- decide one contract question,
- update one doc section,
- run one validation command,
- run independent review gate,
- fix accepted finding.

Checklist rules:

- Keep checklist items short and directly actionable.
- Mark completed steps as `[x]`.
- Add new items when new evidence changes the path.
- Mark obsolete items as obsolete rather than pretending they were completed.
- Do not copy the entire checklist into `Status.md`; `Status.md` should only keep the current pointer and next action.

## Planning protocol

When creating or revising a durable plan:

1. Read `Prompt.md` if present.
2. Read `Status.md` to avoid losing current context.
3. Inspect only enough code/docs to make the plan accurate.
4. Create/update `Plan.md` with:
   - task identity,
   - current milestone,
   - progress,
   - active working checklist,
   - milestone overview,
   - milestone details,
   - acceptance criteria,
   - validation commands,
   - risks,
   - decisions.
5. Update `Status.md` with the new current milestone and next action.
6. Stop for user review if implementation direction is material or risky.

## Bug-fix protocol

When fixing a bug:

1. Reproduce or identify the failing behavior when feasible.
2. Add or update a focused test/check when feasible.
3. Make the smallest fix.
4. Run focused validation.
5. Run broader validation if the change touches shared runtime code or artifact contracts.
6. Run independent review gate when the fix is part of a durable milestone.
7. Record validation and review outcome in `Status.md`.

## Skill runner protocol

When creating or changing a company-research skill runner:

1. Read the relevant `docs/skills/specs/skill*.md` and existing `SKILL.md`.
2. Identify hard input dependencies and blocked conditions before implementation.
3. Use `company_research_runtime` helpers for paths, atomic writes, evidence, hashing, status, and artifact state when applicable.
4. Do not fabricate output artifacts when hard dependencies are missing. Write `needs.yaml` and return a blocked status.
5. Write run outputs under `COMPANY_RESEARCH_ROOT`, not the repo.
6. Keep `--help` useful and accurate.
7. Keep demo behavior explicit. If `--demo` requires real artifacts such as `company.yaml.cik`, document that clearly; if it should be dependency-light, implement that deliberately.
8. Validate with `compileall` and `--help`; run focused functional validation only when required artifacts exist.
9. Run independent review gate before marking the milestone complete.

## Independent review protocol

Run this gate before marking a durable milestone complete when the milestone changed runtime code, setup docs, user-facing commands, validation commands, artifact contracts, agent policy, or durable workflow files.

Important distinction:

- `/review` is a user/interface slash workflow. Codex should not claim it can run `/review` automatically on the user's behalf.
- If the user manually ran `/review`, use that output as independent review evidence and filter it through `docs/agent/code_review.md`.
- If the user did not run `/review`, explicitly spawn the project-scoped read-only `quanti_reviewer` subagent and ask it to review the current milestone diff using `docs/agent/code_review.md`.

Reviewer constraints:

- Reviewer must not edit files.
- Reviewer must not update `Plan.md`, `Status.md`, `Documentation.md`, or any durable state.
- Reviewer should inspect only bounded inputs listed in `docs/agent/code_review.md`.
- Reviewer findings are candidate issues, not accepted truth.

Main-agent handling:

1. Accept only material, evidence-backed findings.
2. Fix accepted `high` and `medium` findings before marking the milestone complete unless the user explicitly defers them.
3. Fix `low` findings only when cheap and in-scope; otherwise record them as follow-up if useful.
4. Do not copy rejected nitpicks into durable state.
5. Record review method, verdict, accepted findings, and fixes in `Status.md` and `Documentation.md`.

## Completion protocol

Before saying a durable milestone is complete:

- Check active milestone acceptance criteria in `Plan.md`.
- Ensure active working checklist in `Plan.md` is current.
- Run required validation commands or record exact blockers.
- Run independent review gate when required or record why it was skipped.
- Fix accepted high/medium review findings or record explicit user deferral.
- Update `Status.md`.
- Update `Documentation.md`.
- Summarize:
  - what changed,
  - what was verified,
  - review method and outcome,
  - what remains risky or follow-up.
