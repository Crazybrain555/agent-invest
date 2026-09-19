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

Each accepted pending task obtains its durable result-byte reservation before
entering the parse semaphore. `reserve_result_for_parse` atomically checks the
actual retained bytes plus existing reservations and the requested budget.
The same key and same budget are idempotent; a different budget conflicts.
A capacity-full task remains accepted pending, waits outside parse/finalizer
slots, and rechecks after the existing same-loop capacity event. It is not a
new 429 rejection and has not performed expensive parsing. The manager reads
and validates one positive per-task budget against the positive global limit
at construction; the finalizer receives that same budget explicitly.

Reservations stay charged through pending, processing, finalizing, failure,
and owned task-tree cleanup. Completion atomically replaces the reservation
with the actual result bytes, which cannot exceed its held budget. Successful
completion and completed ACK/cleanup notify waiting processors; notification
is only a reason to retry the atomic reservation, not a grant of capacity.
Writing cleanup intent, warning-only partial-ZIP deletion, observing a missing
file, or a failed registry commit cannot return credit. Only successful owned
namespace cleanup followed by durable `consumed` releases the responsibility.

Cold replay retains the original reservation. Its physical-cleanup barrier is
installed before attempting the pending transition, so later reconciliation
of an uncertain registry write cannot skip physical cleanup. Even an empty
retry syncs the held task directory before the barrier is removed. Legacy
zero-reservation active/failed tree responsibility and a loaded total above
the configured limit block new parsing until explicit recovery or owned
cleanup. Existing readers, leases, result downloads and ACK remain available.
Recovery-required responses retain their distinct cause and do not invent
persistence phase/outcome fields.

Normal shutdown closes new admission and wakes result-capacity waiters. Already
accepted work, including owned ingress and cold backlog, still drains when it
holds or can obtain its result reservation. If capacity is full during shutdown,
the waiter retains its pending key and shutdown reports incomplete responsibility
instead of waiting indefinitely for an external ACK. Worker failure also wakes
waiters and prevents work that has not entered parsing from starting; the same
pending key and any held reservation remain recoverable. Already running native
work drains by its existing ownership boundary. Ordinary capacity wait while
the manager is running does not create a worker-failure condition.

A capacity waiter stopped by normal shutdown is excluded from further scheduling
in that manager run. This preserves its pending responsibility without converting
the normal stop into a worker error that would abort already reserved peers.
Shutdown waits for those peers to drain before reporting any remaining pending
responsibility; starting the manager again clears only this process-local exclusion.

During persistence uncertainty no new mutation can allocate or return credit.
The separate retained/reserved accessors are conservative component bounds;
their sum is not necessarily the exact charge in one durable snapshot. These
result-ZIP reservations do not account for all uploaded files, parser
intermediates, RAM, VRAM or whole-disk capacity.

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
the registry's recovery action. A processor failure additionally marks the worker
unavailable and wakes all waiters; the original cause remains task-specific.

Each synchronous wait retains both created `Event.wait` helper tasks from the
moment they are started. Its `finally` path cancels both helpers and awaits both
before normal return or propagation of the original outer cancellation or
exception. Cancellation while `asyncio.wait` itself is suspended therefore
cannot bypass helper cleanup merely because its result assignment did not
complete. A normal event or shutdown wake uses the same cleanup path, and any
completed helper result keeps the prior fail-visible inspection behavior.

Ordinary errors and cancellation after entering parse retain their terminal task
behavior. Cancellation while still pending for result capacity or a parse slot
retains that pending responsibility. Shutdown wakes waiters through the manager signal.
Startup clears only this process-local map before durable cleanup/recovery
reconstructs routes.

HTTP conflict boundaries return 503 for persistence failures. Accepted input
survives a routing, submission, or persistence-response failure. Incomplete
ingress uses the owned cleanup sequence below; persistence ambiguity preserves
the input and its responsibility for reconciliation.

Health remains closed: a degraded or uncertain persistence status raises the
structured persistence error rather than reporting a healthy runtime. This is
an operational signal for P1 recovery, not proof of complete M6 service
qualification.

## Ingress and acceptance (M6 admission responsibility R1)

The registry writer uses `mineru-task-registry.v3`; the decoder explicitly
accepts the original closed v2 shape and the new closed v3 shape. The new
private `ingress_owner` field is absent from v2. An older executable cannot
read v3; restoring its image alone is not a registry rollback. Deployment
still requires an independently inspected quiescent output root and retained
backups. Never delete retained responsibilities to make a rollback pass.
Before automatic rollback can restart an existing older API, the installer
requires the complete predeployment physical witness to remain identical:
root identity, registry bytes or absence, counts and submission watermark.
Changed evidence blocks rollback; missing, unreadable or malformed evidence
blocks it as unverified. This check precedes restoring any old deployment
files or tag. Operator writer exclusion must span the entire operation,
including already-issued requests still in multipart/Form processing.

After FastAPI has parsed the Form, but before creating an API-owned directory
or awaiting upload copies, new keyed ingress reserves durable nonterminal
capacity atomically. Existing keys reconcile their original task/attempt/fence
before capacity is charged. A fresh over-capacity request gets 429 without an
owned directory or executable payload. This boundary does not claim that the
framework avoided reading or spooling the multipart body.

The nonterminal count is `ingress + ingress_cleanup + pending + processing +
finalizing`, with each durable record counted once. A new-only task directory
and uploads directory are pinned by actual device/inode/uid/mode receipts.
Complete upload files and their containing namespaces are synced before one
durable transition binds the payload and changes ingress to pending. That
transition is executable acceptance; queue insertion is a derived operation
and cannot subsequently reject the accepted task as fresh 429 work.

An upload failure first persists `ingress_cleanup`. Only identity-checked
owned deletion and namespace sync, followed by registry persistence, release
the preparation credit. A crash between directory creation and owner-receipt
persistence is not an atomic filesystem transaction: an existing directory
without its receipt remains charged and requires recovery. Neither cold
recovery nor legacy `abandon_unbound` may delete such a record by guessing
ownership from its task ID.

Same-key retries during live ingress report the original task ID with
`accepted=false`; abandoned ingress reports recovery required. Accepted
pending tasks missing an online route hydrate the existing payload without
changing generation or invoking cold replay. Processing/finalizing tasks
without a scheduled owner report recovery required. Any accepted task with a
known persistence failure, including a pending task whose processing transition
failed, reports 503 with the original phase/outcome/committed diagnostics and
exception cause. Wire status remains pending/processing/completed/failed;
finalizing projects as processing and cleanup intent projects its original
completed/failed terminal, while the protocol state keeps the actual phase.

Stopping closes fresh admission and waits for already-owned uploads to accept
or clean up. Completion callbacks drive bounded pending refill and shutdown;
an already-completed queue join is not a reason to spin on the event loop.
Cold accepted backlog can exceed the current admission limit, remains durable,
and refills only available scheduling capacity. It is reported as recovering,
not as a qualified idle runtime.

New health uses `mineru-task-runtime.v2` with registry schema v3 and scope
`post_form_owned_upload`, plus closed `mineru-task-admission.v1` evidence.
The normalized 13-field observation remains: queued counts ingress plus
accepted pending responsibility, processing includes finalizing. Physical queue
depth, live processors, scheduled IDs, route-less accepted tasks and unowned
ingress are separate gauges. Thus upload responsibility cannot disappear from
idle checks or sampled nonterminal load. Observers retain an explicit closed
legacy-v1 branch for old service inspection; the new runtime collector and new
installation qualification require v2 admission evidence. Overcommitted
recovery remains diagnostically visible and fails normal qualification.

The R1 admission changes by themselves do not qualify native cancellation,
result capacity, increased concurrency or real GPU throughput. The result
reservation boundary above and the native lifetime boundary below are separate
changes; neither establishes full M6 acceptance.


## Owned asynchronous native lifetimes

Hybrid and VLM CPU/model/finalization thread awaits use the existing
`drain_owned_awaitable` through a thin `to_thread_owned` adapter. Cancellation
waits for the submitted function to settle before caller-owned images/PDFs or
result-source file descriptors are closed. Repeated cancellation preserves the
first cancellation and retains a later native failure as its cause. The two
render callers also return images produced after cancellation through their
existing image-close callback. No new global model lock or parser algorithm
is introduced by these adapters.

The retained-result builder drains acquisition, writing, verification/closure
and hashing. Cancelled acquisition closes the descriptors it actually returns;
verification transfers the source list only when its callee starts. Each
acquired descriptor receives one close attempt; a consumed and recycled integer
is not retried. Cleanup attempts the remaining descriptors and retains the
primary IO error. Acquisition and builder cleanup record secondary close errors
as notes; verification retains its first verification or close failure.
Hashing must finish before the caller unlinks the retained ZIP.

This boundary covers the selected hybrid/VLM callers and retained-result
builder. It does not certify forcible process-pool termination, every inference
client cancellation, the separate pipeline backend or legacy response builders.
Cooperative drain does not create a new production timeout or prove a native
thread can be forcibly cancelled. Real service/GPU and publication acceptance
remain separate from these deterministic lifetime checks.


After successful ACK, both task-ID GET and idempotency-key GET return the
original 404 `Task not found` absence response. The retained consumed tombstone
still prevents a repeated POST from treating that key as fresh work. The GET
lookup must not reuse the repeated-submission 410 response for a consumed key;
otherwise the original diagnostic disposal cannot confirm absence.

## Durable view observation

Live evidence (R24 G4 r2, 2026-09-19): under seven concurrent parses the serving loop's `/health` waited 78–310 ms
for the registry lock because every state-changing mutator holds the re-entrant lock across the durable commit
(temp write, file `fsync`, replace, parent `fsync`) on the Windows bind mount, and the host telemetry lane's 900 ms
cycle is single-failure fatal. The atomic mutation boundary above is kept unchanged: a mutation is durable or rolled
back before the lock is released, and no reader of the registry ever sees a non-durable mutation.

The registry additionally publishes an immutable **durable view** (`DurableRegistryView`): deep-copied records,
the submission watermark bucket, the persistence generation, the last persistence event and the uncertainty flag,
with the publication instant on the monotonic clock. It is published at initial load (before any other thread can
reach the registry) and under the registry lock at every `_mark_durable_commit`, on restoring the last durable
state, at every recorded persistence event (the only path by which a degraded or uncertain state reaches the view)
and at recovery hydration. `durable_view()` returns
the current reference without acquiring the registry lock and never blocks on a commit in flight; a view may lag
that in-flight commit by at most one mutation and therefore never contains a mutation that did not commit. A
degradation or uncertainty recorded under the lock reaches the view at the next publication within the same lock
hold; a reader may observe the previous view for the few statements in between, and every record it carries is
durable. The container is frozen and its records and event are deep-copied once per publication, not per read;
readers share those objects and must not mutate them.

`/health` is served from the view. Manager health (`is_healthy()`, worker error, shutdown) stays live and
loop-owned; the admission counts, the runtime facts and the persistence verdict come from the view, joined with the
loop-owned route and ingress sets. A degraded or uncertain persistence state is carried by the view exactly as
`assert_persistence_healthy()` would raise it, so the route fails closed with the same structured
`registry_persistence_unavailable` 503 and never reports a healthy runtime it cannot prove. The route no longer
takes the registry lock, an executor lane or the bounded `observe()` poll, so it never answers
`registry_observation_busy`: the R24 choice for this route (503 busy rather than a cached healthy) is superseded,
because the view is the last durable state, not a cached response. The admission counts may lag one in-flight
commit by construction. Today no route or ingress removal site can flip `blocked_reason` or `admission_open`
between a commit and the loop's own bookkeeping; a reordering of those sites must keep that property.

The view does not participate in admission decisions, in the task executor's record checks, or in any mutation;
those and the task routes keep the locked `admission_status()` and `observe()` paths with their busy semantics.
