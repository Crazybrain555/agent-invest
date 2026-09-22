# V4 execution-spec and local-resource lifetime

This private/default-off design does not qualify live GPU throughput or change the public Unit/Filing contract.

## Execution specs

`remote_parse_v4_execution_spec` owns at most 512 KiB of canonical execution-control bytes per resourceful
attempt. Source PDFs and provider artifacts remain immutable files. Spec insertion, preparation evidence and
H0 share one UoW. Rollback leaves no spec file or detached row; uncertain commits reconcile exact bytes.
Deferred FKs and H0 closure bind attempt/fence/preparation; updates/deletes are forbidden. Strict application
reload verifies the canonical hash, size, profiles, request, source and reservation—not merely FK presence.
All historical references, including final and noncurrent attempts, retain their spec. Resource-free H0 has none.

Worker runtime reads only PostgreSQL. The legacy catalog is read-only and injected only into explicit offline
backfill; it has no writer and is not a recovery fallback. Stage-local hydration remains once per stage, with
fresh mutable document/run and side-effect ownership checks.

## In-place ownership and cleanup

Ambiguous staging remains at its deterministic attempt-owned path. It does not move to a new detached
quarantine directory or authorize another extraction. `V4ResourceOwnershipError` is not an item/parser failure:
the coordinator opens its circuit and keeps durable held credits. Exact journal-owned failure cleanup and
validated deterministic cleanup suffixes remain recoverable; unknown, mutated, linked or oversized contents
are not deletion authority.

A locked semantic-route overflow (`SemanticRouteLockedCandidateOverflowError`) is raised by the publication
request builder before transaction P, so nothing is persisted. The COMMIT lane records it as an attempt-local
`local_failure` with error code `semantic_route_locked_candidate_overflow`, retry budget class
`semantic_route_contract` and `retryable=false`; the attempt closes through its cleanup plan and ACK without a
publication. The base `SemanticRouteContractError` and every integrity error still open the run circuit.

A contract retry-budget class is never retried automatically: scheduling cannot know that the cause was fixed.
The releasable classes are the closed set the coordinator persists — `semantic_route_contract`,
`provider_protocol`, `provider_artifact_contract`, `provider_runaway`, `provider_terminal`. Two admission gates
hold such a document out of `pending_parse`: the view's `last_failed_retryable` latch, because the failure is
stored with `retryable=false`, and the contract-class exclusion over every failed provider parse run. One
append-only row in `disclosure_ops.parse_requeue_decision` releases both, and only for the run it names — the
exclusion takes a per-run exception, and the latch only while that run is still the latest failure. A newer
undecided failure re-engages both gates. The failed run is never rewritten; the decision carries what was fixed,
why and who decided it, and only `disclosure_app` may SELECT and INSERT it, never UPDATE or DELETE.

`python -m disclosure_anchor.cli.parse_requeue` writes the decision with fail-closed guardrails and a
`--dry-run` that writes nothing. A missing, malformed or unknown retry class, or a non-boolean `error.retryable`,
is refused rather than released: the queue excluding a row is not evidence that an operator may re-admit it. The
receipt keeps three facts apart — whether the decision was recorded, whether the document is eligible right now
(asked of `pending_parse` itself, after the write), and the remaining blockers. Recording a decision admits
nothing by itself; the document is parsed only when a normal worker scan or an authorized campaign picks it up.
Retry budgets are unchanged: a released contract failure still charges neither budget. Every succeeded provider
generation must be provably older than the released failure; one with an unknown start time refuses the release.
Doctor lists contract-class failures with no decision over the documents the queue would consider, and reports
each decision as the outcome of the latest provider parse run after it (released_pending, released_refailed or
resolved); any unresolved decision older than a day warns, and a failed diagnostic is a check failure, never a
quiet zero.

Valid promoted output replays exactly. Invalid promoted output, including a markerless response-loss tree,
is contained by no-replace output→staging rename under the existing resource lock and claim guard, pinned
root identity and parent fsync, then stops with ownership unresolved. Simultaneous paths, root substitution
or marker drift fail closed without overwriting either tree. A crash around containment replays the same one
charged namespace. Operator resolution is separate; no forensic-retention SLA or automatic guessed deletion
is introduced.

Cleanup cannot report `absent` until the planned source paths are actually absent. It also checks uncommitted
output omitted by older failure plans and legacy detached siblings. ACK repeats absence checks immediately
before HTTP POST under the resource locks; a canonical old cleanup receipt is not current filesystem proof.
Published targets live under the distinct canonical data root and are not mistaken for scratch residuals.

Once per resident start, before coordinator effects/admission, a read-only all-materialization-intent keyset
scan validates historical local ownership, including `ack_pending`, final and superseded histories. Each page
is at most 100; memory is page-bounded, but cold-start time depends on historical size. It is not repeated at
every quiescent cycle. Any legacy detached entry or false historical absence stops startup without adopting
or deleting it. The worker singleton is not evidence that an old disconnected filesystem writer has exited.

## Offline migration and retirement

These are protected runtime operations, not actions authorized by a code merge. Obtain the actual runtime
claim and an approved maintenance window. Disable automatic restart and observe actual old-worker/child
exit before migration, backfill or retirement; a lease expiry, advisory-lock acquisition or the CLI flag alone
does not prove that drain. Preserve source identities and do not delete ambiguous staging as part of this work.

1. Apply `0060_v4_execution_spec`, which adds the table and initially unvalidated historical FK. New H0 writes
   must already include specs. Do not start the new resident until historical cutover is complete.
2. Run finite batches from the service directory with the approved app-role environment loaded:

   ```bash
   PYTHONPATH=src .venv/bin/python -m disclosure_anchor.cli.v4_spec_maintenance --limit 100 --old-writers-drained
   ```

   Continue with `--after-attempt-id <returned-cursor>` until `exhausted=true`. Every resourceful H0 is visited,
   not just current work. Missing legacy bytes or identity drift stops the batch. Existing PG copies are strictly
   verified without filesystem fallback. After an uncertain commit, repeat the same input cursor.
3. Apply `0061_validate_v4_spec` (or the subsequent graph head). FK validation is a necessary closure check,
   not a substitute for the canonical all-history backfill. Missing specs prevent activation.
4. Only after successful cutover, explicitly selected old spec files may be retired, at most 100 per invocation:

   ```bash
   PYTHONPATH=src .venv/bin/python -m disclosure_anchor.cli.v4_spec_maintenance --old-writers-drained --retire-spec 'sha256:<exact-digest>=<exact-byte-count>'
   ```

   Repeat `--retire-spec` for each selected reference. There is no directory sweep, age cutoff or auto-GC.
   Retirement pins the exact private single-link file and parent chain; checks bytes and canonical identity;
   requires the validated FK plus an exact PG copy or absence of that attempt; rechecks ownership and file
   identity before unlink/fsync. Unknown/conflicting files remain untouched. Retrying a removed file reports
   `already_absent`; an unavailable data root fails rather than pretending retirement succeeded. Copied PG
   history remains intact. Deleted legacy files are not moved to trash; choose this action only after the stated
   copy/orphan proof and approved drain.

## Verification scope

`make agent-check` and managed `make test-integration` cover atomic spec closure, all-history backfill,
rollback/response loss, exact retirement, in-place ambiguity families, containment races, historical startup
and pre-ACK checks. The composed scratch test runs the real coordinator/PG/filesystem across three boot
identities with markerless damaged output: no publication/failure/cleanup receipt or ACK, and held credits
remain unchanged. Fake HTTP and synthetic fixtures are mechanism evidence, not live PDF-quality/throughput
qualification. Real held-out PDFs, long-running turnover and GPU host-hour measurement remain runtime gates.
