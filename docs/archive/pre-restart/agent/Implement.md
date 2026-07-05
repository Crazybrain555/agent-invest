# Implement.md — Durable Execution Runbook

This file defines how agents should execute durable work in this repository. `AGENTS.md` handles routing and policy; this file handles the execution loop.

## Core principle

Durable work should be recoverable from files, not from chat history.

A fresh session should be able to read:

1. `AGENTS.md`
2. `docs/agent/Status.md`
3. `docs/agent/Plan.md`
4. this file

and continue safely.

## Mandatory trigger protocol

Before editing, check whether any mandatory trigger in `AGENTS.md` applies.

If a trigger applies:

1. Update the active milestone or active working checklist in `Plan.md` if the current plan does not already cover the change.
2. Identify the contract boundary: agent policy, MCP setup, validation command, future runtime behavior, or future artifact contract.
3. Define the smallest observable validation that can prove the change.
4. Plan for the independent review gate before declaring a durable milestone complete when required.
5. Keep `Status.md` and `Documentation.md` aligned with the active task policy.

## Request relationship protocol

Before editing, decide how the current user request relates to durable state:

- New durable task: replace the current task sections of `Prompt.md`, `Plan.md`, and `Status.md`.
- Revision of current durable task: update only the durable files affected by the user's correction.
- Execution of current durable task: follow the active milestone and checklist in `Plan.md`.
- Harness/policy maintenance: keep changes focused on agent behavior, MCP setup, and durable workflow files unless the user explicitly asks for product/runtime code.

## Durable execution loop

For the active milestone:

1. Confirm the current milestone and active checklist item from `Plan.md`.
2. Identify the minimal files needed.
3. Inspect with targeted search before broad file reads.
4. Make the smallest coherent implementation or doc change.
5. Update the active checklist as steps complete, split, or become obsolete.
6. Update `Surprises & Discoveries` when observed behavior differs from the plan or assumptions.
7. Update `Decision log` when choosing between material alternatives.
8. Run the milestone validation commands.
9. If validation fails, repair the milestone or mark it blocked before moving on.
10. Run the independent review gate when required.
11. Resolve material review items or record explicit follow-ups/blockers according to the active task policy.
12. Update `Status.md`.
13. Update `Documentation.md` if the active task requires long-form notes.

## Active working checklist discipline

Use `Plan.md` for small dynamic steps such as:

- inspect a file/path,
- verify command behavior,
- decide one contract question,
- update one doc section,
- run one validation command,
- run independent review,
- resolve a material review item.

Checklist rules:

- Keep checklist items short and directly actionable.
- Mark completed steps as `[x]`.
- Add new items when new evidence changes the path.
- Mark obsolete items as obsolete rather than pretending they were completed.
- Keep `Status.md` as a short pointer, not a full log.

## Living-plan sections

Keep these sections current in `Plan.md`:

- `Progress`: dated or timestamped execution state.
- `Active working checklist`: granular todos for the current milestone.
- `Surprises & Discoveries`: unexpected facts with evidence.
- `Decision log`: material choices with rationale.
- `Outcomes & Retrospective`: what was achieved, what remains, and lessons after a milestone closes.

## Independent review protocol

Run this gate before marking a durable milestone complete when the milestone changed runtime code, setup docs, user-facing commands, validation commands, agent policy, durable workflow files, or future artifact contracts.

Important distinction:

- `/review` is a user/interface slash workflow. Codex should not claim it can run `/review` automatically on the user's behalf.
- If the user did not run `/review`, explicitly spawn the read-only `quanti_reviewer` subagent and ask it to review the current milestone diff using `docs/agent/code_review.md`.

Reviewer constraints:

- Reviewer must not edit files.
- Reviewer must not update durable state.
- Reviewer should inspect only bounded inputs listed in `docs/agent/code_review.md`.
- Reviewer output is a candidate report, not accepted truth.

Main-agent handling:

1. Accept only material, evidence-backed review items.
2. Resolve high/medium material items before marking the milestone complete unless the user explicitly defers them.
3. Resolve low material items only when cheap and in-scope.
4. Record review handling according to the active task policy.

## Completion protocol

Before saying a durable milestone is complete:

- Check active milestone acceptance criteria in `Plan.md`.
- Ensure the active checklist is current.
- Run required validation commands or record exact blockers.
- Run the independent review gate when required or record why it was skipped.
- Resolve material high/medium review items or record explicit user deferral.
- Update `Status.md`.
- Update `Documentation.md` if the active task requires long-form notes.
- Summarize what changed, what was verified, review outcome, and remaining risks.
