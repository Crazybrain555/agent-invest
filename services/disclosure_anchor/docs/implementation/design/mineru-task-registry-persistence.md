# MinerU task-registry persistence boundary (M6 P1)

## Scope

This contract covers only the durable MinerU task registry used by the staged
Windows compatibility service. It does not qualify GPU throughput, real PDF
processing, PostgreSQL publication, live service operation, deployment, or M6
as a whole.

The registry is the durable owner of the exact tuple formed by the
idempotency key, task ID, attempt identity, fence identity, bound task payload,
result hash/size/owner, reservation, lease, acknowledgement state, and cleanup
intent. Callers must not infer a task outcome from an exception class alone;
`TaskRegistryPersistenceError.committed` and `outcome` distinguish the durable
result.

## Atomic mutation boundary

Every state-changing registry method runs under one re-entrant registry lock
and an outer transactional snapshot. Returned records and payloads are deep
copies. A mutation that raises before a durable commit restores the last
known-durable records and submission watermark, including nested payload,
upload, and ownership data.

The persistence sequence is:

1. create a same-directory private temporary file;
2. write the complete canonical JSON payload;
3. flush the language stream;
4. `fsync` the temporary file;
5. close the temporary stream;
6. replace the registry pathname atomically;
7. `fsync` the registry parent directory; and
8. remove any residual temporary pathname.

A visible reread is never treated as durability proof. When the replace call
raises, or the first parent-directory `fsync` raises, the registry compares the
visible bytes only to its exact previous and candidate payloads, successfully
retries the parent-directory `fsync`, and performs a stable second reread
before selecting an outcome.

## Outcome classes

### Not committed

Write, flush, file-`fsync`, file-close, or a replacement known to have left the
previous bytes durable is `not_committed`. The in-memory mutation is rolled
back and the original storage exception is retained as the chained cause. A
caller may retry the same idempotent operation.

### Durability uncertain

If the target bytes are neither the exact previous nor candidate payload, if
the parent-directory `fsync` cannot be established, or if the bytes change
during reconciliation, the result is `durability_uncertain`. Ordinary reads
that could report a false outcome and all new mutations fail closed. Capacity
properties remain conservative by taking the maximum charge represented by
the durable and candidate snapshots.

For non-reader mutations, `recover_persistence_uncertainty()` is the explicit
in-process recovery path. It uses the saved exact snapshots, successfully
`fsync`s the parent directory, and requires a stable reread before choosing
the previous or candidate state. It does not cold-decode the registry, so an
unrelated live result reader remains live across recovery.

An uncertain `acquire_result` or `release_result` instead reports
`restart_registry_process`. A failed acquire may not have returned its handle,
and a failed release may already have relinquished one; inventing either fact
in-process would be unsafe. Cold startup validates the durable registry and
resets process-local reader counts to zero.

### Committed with cleanup error

Once replacement and parent-directory `fsync` have completed, a temporary-file
or directory-descriptor cleanup error cannot roll the task state back or turn
the durable business mutation into a failed call. The registry returns the
committed outcome, records `outcome="committed_cleanup_failed"`, and marks
persistence health degraded with recovery action
`do_not_retry_committed_operation`. This is essential for non-idempotent reader
count mutations: a committed acquire still returns its path and a committed
release still returns normally, so the caller does not leak or double-release a
reader merely because post-commit cleanup failed.

`assert_persistence_healthy()` raises `TaskRegistryPersistenceError` with
`committed=True` and chains the retained cleanup exception. Thus health and
diagnostics preserve the original exception context while normal task/result
callers receive the exact already-durable outcome. A later clean mutation clears
the degraded event.

A replace-call exception followed by an exact candidate reread and successful
parent-directory `fsync` is a committed recovery, not an ambiguous success.
The status remains degraded until a later clean mutation, and records the
original exception type without embedding task content.

## Readers, ACK, and capacity

Result-reader acquisition is persisted before a path is returned. Failed
acquisition restores the exposed count. Release checks for underflow before
changing the count, and a failed release restores the durable count. ACK is
rejected while a reader is active. The `open_result()` context keeps a primary
download exception as the raised exception if reader release also fails; the
release exception is chained and noted instead of masking the primary error.

Reservations remain charged through finalization. Completed and
`cleanup_pending` result bytes remain charged until durable cleanup reaches
`consumed`; uncertainty never releases the larger possible charge early.

## Recovery hydration and cleanup intent

Binding stores a private `_agent_protocol` ownership receipt containing the
generation, task-root identity, upload-root identity, and exact upload
identities and hashes. Startup recovery reads that receipt (not a public
`protocol` field), increments and persists the generation transition before
removing stale output, and hydrates pending, completed, failed, and
`cleanup_pending` routes without exposing the private receipt to callers.

Filesystem deletion is an external side effect and cannot be undone by
restoring Python fields. ACK therefore persists `cleanup_pending` before
unlinking. Cleanup validates the durable ownership receipt, recursively removes
owned entries while `fsync`ing each modified containing directory, `fsync`s the
empty task directory, removes the task entry from the pinned output root, and
then `fsync`s that output root. Only after these task/output namespace barriers
succeed may the registry clear payload/ownership, release reserved and unacked
capacity, and durably transition to `consumed`.

A retry after an earlier attempt removed the task entry but failed before the
output-root barrier performs the bounded renamed-inode scan and `fsync`s the
observed absence before consuming the intent. Partial deletion, unlink,
namespace-`fsync`, or descriptor-close failure leaves the durable
`cleanup_pending` receipt, result identity, and conservative capacity intact.
A secondary close failure is attached as a note and does not replace the
original deletion or `fsync` exception. A registry-persistence failure after the
namespace barrier likewise retains intent and capacity for an exact retry. The
retry tolerates an already-removed owned result or uploads tree, but rejects a
renamed inode, symlink, owner/mode drift, unexpected replacement, or an
unbounded directory scan. This ordering closes a proven commit-barrier gap; it
is not represented as a physical power-loss experiment.

## Generated FastAPI integration

The compatibility patcher imports `TaskRegistryPersistenceError` in the actual
grouped protocol import, applies the persistence behavior only to generated
`fast_api.py`, and compiles all seven pinned upstream preimages. Background
processing stores the exact persistence exception in a task-specific,
process-local wait-failure map, signals that task's event, logs, and re-raises
without converting the durable nonterminal task to `failed` or `completed`.
The synchronous wait path checks that map both before sleeping and after an
event wake. A caller already waiting and a caller arriving after the processor
failure therefore receive a prompt `TaskWaitAbortedError` chained from the
original `TaskRegistryPersistenceError`, including its persistence outcome and
the registry's recovery action. Unrelated task events are not signalled.

Each synchronous wait retains both created `Event.wait` helper tasks from the
moment they are started. Its `finally` path cancels both helpers and awaits both
before normal return or propagation of the original outer cancellation or
exception. Cancellation while `asyncio.wait` itself is suspended therefore
cannot bypass helper cleanup merely because its result assignment did not
complete. A normal event or shutdown wake uses the same cleanup path, and any
completed helper result keeps the prior fail-visible inspection behavior.

Ordinary parser errors and cancellation retain their established terminal task
behavior, while shutdown continues to wake waiters through the manager signal.
Startup clears only this process-local map before durable cleanup/recovery
reconstructs routes.

HTTP conflict boundaries return 503 for persistence failures. Failed request
setup deletes the task tree only after durable `abandon_unbound`; persistence
ambiguity preserves the input for exact retry.

Health remains closed: a degraded or uncertain persistence status raises the
structured persistence error rather than reporting a healthy runtime. This is
an operational signal for P1 recovery, not proof of complete M6 service
qualification.
