# Staged execution observation (measurement only)

Scalar timing notes attached to the existing bounded stage guard, so one
commissioning run can attribute `local_materialized → publish_committed` to
source build, semantic input preparation, model groups, semantic slot waits,
provider subprocesses and transaction P. Default off: every new parameter is
`None`, call sites reach the guard through `note_stage`, and without an observer
nothing is called. Business results, receipts, credits and exits are unchanged.

## Contract

`application/ports/staged_execution.py` defines `StageNote` (attempt, lane,
kind, `time.monotonic_ns()`, sorted scalar pairs of int/str/None, text ≤ 256),
`StageObserverPort` (`note`, `record_failure`), `note_stage(guard, kind, ...)`
and the thread-local `semantic_group_scope`/`current_semantic_group` used by the
executor to hand the group hash to the provider adapters on the same thread.
`StageLeaseGuard.note` builds the note from its own attempt/lane; an observer
exception is passed to `record_failure` and never reaches the stage. The
coordinator fills attempt/lane when it constructs each stage guard.

## Points

| kind | where |
|---|---|
| `source_admitted`, `units_built`, `route_started`, `route_finished` | publication request builder |
| `semantic_inputs_prepared`, `semantic_groups_planned` | `SemanticRouter.route` (replay unchanged) |
| `group_started/ended`, `provider_call_started/ended`, `cache_hit` | ordered adjudication executor: exactly one group pair per `adjudicate` call on every exit path; one provider-call pair per real adapter call, closed also for lease loss or unexpected errors (`outcome` = succeeded / succeeded_cache_write_failed / cache_hit / degraded_unavailable / availability_failed / cancelled / failed_closed / lease_lost / error:<Type>) |
| `slot_requested/acquired/released` | Codex and Claude adapters around the concurrency semaphore |
| `process_started/ended` | the shared subprocess runner; `reason` is timeout/cancelled/error:<Type> |
| `producer_lock_acquired`, `request_ready`, `readiness_prepared`, `transaction_p_started/ended`, `winner_resolved`, `readiness_verified` | prepare-and-publish use case: one P pair around each real commit call (`outcome` = committed / response_lost / error:<Type>); the read-only winner lookup after a lost response is its own `winner_resolved(found)` note, never a second end of the same P |

Derived intervals: source build = `source_admitted→units_built`; semantic
preparation = `route_started→semantic_inputs_prepared`; slot wait =
`slot_requested→slot_acquired`; provider process = `process_started→process_ended`;
transaction P = `transaction_p_started→transaction_p_ended`; producer lock hold =
`producer_lock_acquired→readiness_verified`. Group and route totals are never
presented as subprocess time. Notes carry attempt/run/lane/group/provider
identifiers and small integers only; no prompt, document text, path or credential.

## Sink and bounds

`adapters/runtime/stage_observation.py` provides `JsonlStageObserver` (bounded
queue, one non-daemon writer thread, `stage-events.jsonl` created exclusively,
periodic fsync) and `ProgressRecorder` (`progress.jsonl` on snapshot change or
heartbeat, plus the pruning signal recorded as a signal, not a fact). Neither
sink raises into the coordinator or publication callbacks: losses are sticky
counters (`dropped` for a full queue, `late_notes` after close, `note_errors`,
`guard_failures`, `writer_errors`, `truncated` for the byte bound,
`join_timeout`, `summary_write_error`; progress has its own `write_errors`).

Closure: `close()` first refuses further notes, enqueues the closing record
with a bounded wait, raises a separate stop flag that never depends on queue
space, and joins the real writer. The writer drains every accepted record
after the flag and owns the events descriptor, closing it on its own exit; a
join timeout is recorded and the descriptor stays with the thread, so a writer
blocked on I/O still exits by itself once the I/O returns. The summary
`measurement_status` is complete only without any counted loss; any loss is
partial; a dead or unjoined writer or a failed summary write is invalid.
`writer_failed` lets the commissioning entry stop admitting new work while
accepted work drains; nothing else couples measurement to scheduling. The
commissioning entry closes progress, observer and signal handlers in
independent steps; measurement closure errors are listed in the printed
summary and never replace the commissioning receipt, exception or exit code.

Bounds are explicit CLI inputs and are recorded in the summary:
`--observation-max-events` (default 65536) is the queue capacity, i.e. the
burst bound between producer threads and the writer, not a total record count;
`--observation-max-bytes` (default 64 MiB) bounds the written total. For the
six-source trial (1555 pages, roughly 6.2k Units, batch size 16, at most ~390
model groups, 11 notes per group plus 17 per attempt) both defaults are far
above the expected volume; a burst beyond the queue or a total beyond the byte
bound surfaces as `dropped`/`truncated`, never as a silently complete
measurement.

## Not claimed

No owner ticks, credit, hour or throughput acceptance. Monotonic instants are
comparable only inside one process; they are not aligned to the Windows clock.
