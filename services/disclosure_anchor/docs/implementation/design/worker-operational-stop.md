# Worker operational stop (F5)

This is the current contract for stopping the resident worker on a public fault and resuming it only
by an explicit, hash-bound operator release. It is operational control, not a business ledger: business
outcomes stay in the existing checkpoints, failure receipts, processing runs and caches. Operator
procedures are in `../runbooks/production-operations.md` §1.1f.

## Goal and non-goals

- A public or common fault stops the whole worker persistently instead of being re-driven by launchd
  over the same uncached semantic group (a failed group is never cached, so the cache cannot bound it).
- A pure operator stop (TERM/INT with no fault, owned work closed) stays an ordinary clean exit.
- After the cause is fixed and the stop explicitly released, the existing recovery barrier resumes the
  same attempt, run, materialized output and successful group cache. Only still-uncached groups can call
  a model again. There is no whole-document at-most-once claim and no new parse key.
- Not added: per-document quarantine, cross-boot retry counters, half-open probes, a second business
  ledger, new item-local mappings, a stop service/park loop, or a WAL/IO framework. Existing typed
  item-local failures (for example the locked-candidate overflow), the six availability reasons and
  `RetryStage`/`StageWaiting` budgets keep their bounded behavior. Only a provider-witnessed
  `StageProviderWaiting` ends its own attempt's retry episode (worker-dynamic-scheduling.md), so a
  `retry_stage_stuck` stop means genuinely consecutive failures, never two blips in a long healthy wait.

## First cause

`application/ports/worker_stop_control.py` defines the closed, content-free `PublicStopCause`:
`kind`, `reason_code`, `origin`, `exception_class`, `exception_fingerprint` (SHA-256 of class and
message, never the text) and the dispatch-time projection `attempt_id`, `lane`, `state_at_dispatch`,
`lifecycle_version_at_dispatch`, `claim_generation`, `claim_owner_identity`. Semantic faults add up to 16
`provider_attempts` limited to provider identity, outcome, reason code and cache key. Values that do
not validate are `None`, never guessed. No prompt, model output, Unit text, environment or credential
can enter it.

| kind | raised by | reason_code examples |
|---|---|---|
| `semantic_failed_closed` | stage call | adapter/router reason (`forbidden_tool_call`, `invalid_runtime_protocol`, `invalid_decision`, ...) |
| `stage_fault` | stage call | `<lane>_unexpected_failure`, `stage_lease_lost` |
| `deadline_exhausted` | stage call / coordinator | `bounded_stage_deadline_exceeded` |
| `ownership_lost` | coordinator / startup recovery | `claim_renewal_failed`, `in_flight_claim_lost`, `in_flight_claim_reconcile_failed`, `process_guard_failed`, `singleton_lost` |
| `coordinator_circuit` | coordinator / resident | `retry_stage_stuck`, `durable_transition_contract_violation`, `unsupported_durable_state`, `admission_interrupted`, `admission_*_contract_violation`, `resource_credit_grant_unavailable`, `stream_pressure_closed`, `native_storage_hold`, `transfer_integrity_hold`, `stage_grant_unsatisfiable`, `capacity_holds_exhausted`, `unclassified_circuit` |
| `coordinator_fault` | coordinator / resident | `controller_unexpected_failure`, `coordinator_run_failed` |
| `maintenance_fatal` | maintenance | `maintenance_loop_failed` |
| `startup_fatal` | startup recovery / staged startup | `startup_recovery_failed`, `staged_startup_verification_failed`, `execution_upgrade_scope_failed` (local-execution-upgrade.md) |

`WorkerStopControlPort` is `trip(cause) -> bool`, `is_tripped()` and `first_cause()`. The first call
wins; later causes never overwrite it. `InProcessWorkerStopLatch` (application service) latches under a
short lock, makes the halt visible, runs wake callbacks and then an optional persistence hook, all in the
first caller only; later callers never wait behind persistence. A failing callback, a failing `__str__` or a
broken stderr while reporting it never skips the remaining callbacks or the persistence hook, which runs
exactly once even if a callback escapes with a `BaseException`. A coordinator composed without a control
gets this latch: it still halts and records the cause, it only persists nothing. Production composition
(`build_staged_worker_v4_runtime` and `run_resident_worker`) always uses the durable adapter; the builder
composes it from settings when the caller passes none.

## Error versus cancellation

- `OrderedSemanticAdjudicationExecutor` classifies the closed reason before any guard checkpoint. A
  failed-closed reason raises the typed error with a `failed_closed` attempt immediately, so a concurrent
  revocation cannot replace it with lease loss. `cancelled` and availability reasons still check the live
  guard first, so there is never a fallback or accepted late answer after revocation. Adapters already
  turn a subprocess killed by shutdown into `cancelled` before parsing its (possibly truncated) output.
- The semantic child boundary (`codex_cli._run_process`, shared by the Codex and Claude adapters) never
  lets its cleanup replace the call's original cancellation or fault. A cleanup error could otherwise
  be read as `executable_unavailable` and become a fallback or a degraded publication. On Darwin,
  `killpg` answers EPERM, not ESRCH, for a group whose leader has exited but is not yet reaped. This was
  observed on this host; `kill(2)` documents EPERM only as a permission failure. So EPERM proves nothing
  on its own. A group counts as ended only when its own leader has been reaped and a signal-0 probe then
  answers ESRCH.

  If a member truly cannot be signalled, cleanup behaves as follows:
  - it drains only for the grace period;
  - the original exception is still the one raised, with a note;
  - one `[semantic-process]` stderr line reports the failure;
  - the stage note records `closure=unproven`;
  - a leader that is still running stays registered for the shutdown sweep.

  The sweep itself never raises, and it reports only children it could not signal that are still
  running afterwards.
- `StageLeaseGuard.revoke(provenance)` records why permission ended: `operator_cancel`, `public_stop`,
  `ownership_lost`, `deadline_exhausted`, or `unspecified` for a legacy caller. The guard records
  `deadline_exhausted` itself when its clock proves the budget is gone. The first provenance is immutable,
  so an already-triggered deadline stays a fault when TERM arrives later. `StageLeaseLost.provenance`
  carries it; only `operator_cancel`/`public_stop` count as positive cancellation evidence.
- A stop request (a final boolean) never exempts a fault: faults latch where they are observed.

## Coordinator boundary

- Every backend call of all seven lanes runs through `_run_stage_call` in the stage thread. A fault is
  latched there before the Future is done, so the reconciliation path that reads and discards a raced
  Future's exception cannot hide it. `RetryStage`, `StageWaiting` (including `StageAdmissionDeferred`) and
  `RecoveryDeferred` keep their existing semantics, except that `StageProviderWaiting` also restarts its
  attempt's retry count and window; `ExpectedV4AttemptFailure` never leaves the backend.
- Every circuit the coordinator opens latches its cause at the point of decision. Ownership loss keeps the
  existing renew/reload/reconcile path and latches only when the loss is final. Process-guard failures and
  any exception escaping the synchronous controller latch before `pool.shutdown(wait=True)`.
- When any plane trips, the loop closes admission, opens the circuit, revokes every running stage guard
  with `public_stop`, and drains. The durable claim, fence, credit and publication state is never rolled
  back, and `_fail_attempt`/cleanup/ACK are not invoked for a stopped attempt.
- A typed `cancelled` error or a drain-revoked stage opens the circuit only to end the drain promptly; it
  is retry-neutral and latches nothing. `KeyboardInterrupt`/`SystemExit` are operator interrupts.
- Capacity holds (`StageCapacityBlocked`) are classified once, in the coordinator's handler, from their
  closed reason vocabularies. A hold no retained attempt can clear by itself is a site stop latched as
  `coordinator_circuit`: a native storage hold (`native_storage_hold`: hard envelope, codec bound, seal or
  tree integrity), an unprovable local transfer prefix (`transfer_integrity_hold`) and a grant no ledger
  can ever hold (`stage_grant_unsatisfiable`). A document-local hold (decode envelope, spent transfer
  budget, publication envelope) stays a claimed, visible per-attempt hold while anything else can progress.
  The coordinator stops once when nothing runs and nothing can wake by itself while holds or queued work
  remain, holds with an empty queue included. Wake paths are a retry or wait timer, a foreign lease, an
  admission observation, a stream-deferred candidate, and admission that can still bring runnable work (an
  unread scan position, a re-probed readiness deferral, a safe stream pause); a scan held in place for
  credit and open legacy obligations are not. An operator drain that leaves only holds ends as
  `operator_drain` without a new public stop. `capacity_holds_exhausted` names the hold that owns part of a
  short dimension (or the only holds left) with that hold's class and fingerprint; otherwise
  `resource_credit_grant_unavailable` names the first blocked lane head ([V4 resource
  lifetime](v4-resource-lifetime.md)). The attempt keeps its durable state, evidence, spool, retained result and
  credits; a release re-dispatches it once and a persisting hold re-trips with the same cause. Transient
  space waits, transfer/unpack continuations and admitted grants waiting for capacity stay healthy waits.
- `CoordinatorResult` gains `stop_cause` and `termination_kind` (`quiescent`, `public_stop`,
  `operator_drain`, `circuit`). The snapshot shows `blocked_reason=public_stop:<reason>`.

## Worker planes and exits

- `worker loop|once`: a read-only start gate (see "Start gate and supervision") right after settings
  load, before any DB engine, MinerU checker, recovery or dependency composition. `run_resident_worker`
  re-checks under the `WORKER_NS` singleton. Busy singleton exits 75 with no record (`once` keeps its
  historical 0).
- `_run_loop` shares one control across the stage plane, maintenance, startup recovery and the outer
  boundary; `should_stop` includes the halt, and a trip wakes an idle resident. A fatal startup-recovery or
  maintenance error latches before cleanup and before external process termination. Handled temporary
  source/report outages keep their old backoff. The halt stops the next sync/download/build/publish/
  projection step; an in-flight transaction may finish.
- `_run_staged_v4_resident`: QUIESCENT stays the idle loop. A non-QUIESCENT result returns normally only
  for a pure operator drain (operator flag set, no latched cause, `termination_kind=operator_drain`);
  anything else is a public stop, latched as `unclassified_circuit` if nothing latched it. The cause is
  latched before anything is logged. The exit then logs only content-free lines: the coordinator's
  retry-exhaustion lines and its typed diagnostics, each `[staged-v4] diagnostic <sorted ASCII JSON>` of
  an exact `CapacityHoldEvent` or `NoProgressSummary` (at most 64; any other or unencodable record is
  dropped and only counted). Other `errors` entries can carry raw text and are never logged, and no
  malformed result or failing log stream can keep the stop from latching.
- `_StopFlag` keeps operator provenance separately and can never clear a cause. The liveness watchdog
  keeps exit 70 and its bounded owned-child termination and never waits on persistence. It marks its
  exit (`_WEDGED_EXIT`) before that termination, and the exit itself is unconditional even if a sweep
  raises. From then on the resident's fallback latches (`_trip_worker_fault` and the
  `unclassified_circuit` fallback in `_end_staged_resident`) record no new cause. The semantic cancellation
  that its own termination causes therefore ends the run as an unlatched circuit, logged as
  `[staged-v4] circuit opened during watchdog exit; no public stop latched`, never as a public stop or a
  native disable. A cause latched earlier stays the first cause. Faults the coordinator classifies and
  latches itself are unchanged.
- Exit codes: 0 pure operator stop or idle shutdown; 70 watchdog; 75 busy; 77 wrapper TCC failure;
  78 public stop (latched now or found at start; also the wrapper's missing-env refusal); other nonzero
  for unexpected, cleanup or configuration failures. A cleanup error after a trip is printed as secondary
  and the exit stays 78. The singleton is released only after persistence and all planes have joined.
- Manual semantic business entrypoints honor the same read gate at their semantic composition boundary,
  with no generic override: `pipeline build-units|publish|process|rebuild-units` (78), admin API
  build/publish (503 `SERVICE_UNAVAILABLE`, no paths) and `scripts/generate_current_source_replay.py` (78).
  The staged builder gate also covers `staged_commission`, `staged_campaign` and `staged_recover`; their
  coordinators latch through the same durable control. Status, doctor, parse, track, sync, backfill,
  parse-requeue and other repair/read-only commands stay usable.

## Durable record and supervisor

Control files live only under the canonical `DISCLOSURE_RUNTIME_ROOT/control/` (path builder methods):

| file | meaning |
|---|---|
| `worker-circuit-stop.json` | the one active stop; presence means stopped |
| `worker-circuit-stop.<sha256hex>.json` | immutable archive of a released record's exact bytes |
| `worker-circuit-release.<sha256hex>.json` | immutable release decision bound to that hash |
| `worker-circuit.lock` | short control lock, always taken after the worker singleton |

The directory is 0700 and files 0600, owned by the worker uid. The runtime root is never created as a
fallback and must itself be trusted: it is `lstat`ed (a symlinked root is refused, so a substituted stand-in
cannot hide a stop), must be a directory owned by the worker uid and not group/world-writable, and the
supervised root must also share its device with the existing agent_system mount sentinel
(`Settings.sentinel_path`, the same guard doctor/health use). A missing root (unmounted volume), EACCES, an
untrusted root, a symlinked/non-regular/oversized/corrupt record, a foreign owner or a group/other-accessible
mode is a stop (`CONTROL_UNAVAILABLE` or `INVALID_STOP`), never "no stop". Archive/release reads are bounded
even if a file grows while it is read, and a planted symlink is a refusal. The record is canonical JSON
`worker-circuit-stop.v1`, scope `worker-operational-control`,
`record_origin` `automatic_fault` or `operator_reconstructed`, the cause, the worker pid and ownership
start time, available profile/runtime fingerprints, and `native_disable` (`verified_disabled`, `failed`
or `unknown`, a detail token and the service target). Its identity is the SHA-256 of its raw bytes; it
never hashes itself.

Persistence order, by the first tripping thread only, after latch/halt/wake: (1) bounded
`launchctl disable gui/<uid>/<label>` with a `print-disabled` readback; (2) the record, written complete
and fsynced under a same-directory temporary name, installed by hard link (never overwriting; an existing
record is the preserved first cause), then the directory fsynced. A failed channel never prevents the
other and never erases the cause. Native disable uses the one supervision binding described under "Start
gate and supervision": only over the supervised runtime root on macOS, for its bound label, whether launchd
or an operator started the process. Every other root records `native_disable` `failed:<scope>` without
running launchctl, so tests and scratch runners on temporary roots never disable the production job. The
process's own launchd identity is not consulted: launchd-spawned children of the wrapper carry
`XPC_SERVICE_NAME="0"`, not the job label (18 of 18 native spawns, independent acceptance run
2026-09-24), so an equality test there would never select the native channel. One channel succeeding is a durable stop; native-only
prints `STOP_PERSISTENCE_FAILED` plus the would-be record for reconstruction; with both failed the loaded
job's exit policy is the only stop and does not survive a manual start or reboot. A hanging file syscall
is not promised away.

The worker plist is `RunAtLoad=true`, `KeepAlive={SuccessfulExit=true}`, `ThrottleInterval=30`,
`ExitTimeOut=90`: launchd restarts only a clean exit 0. No PathState/WatchPaths/QueueDirectories/
StartInterval triggers (KeepAlive conditions are ORed). The wrapper keeps TCC probing, zsh as the job
process, TERM/INT forwarding and the reap loop, and passes the child's true status through (an
interrupted `wait` that raced the child's exit re-reaps once instead of reporting 128+signal).
`scripts/install_launchd.sh` refuses a recorded/invalid/unavailable stop with 78 before any mutation and
never re-enables a disabled label unless run with `--confirm-operator-disabled` (its preflight passes only
`OPERATOR_DISABLED` through to that explicit confirmation). `make worker-restart` refuses unless the control
state is `RUNNABLE`.

## Start gate and supervision

`require_worker_start_permitted` is the one read gate for every entry above (worker, staged builder,
pipeline/admin/replay composition, installer preflight, plain status). It checks, in order: this process's
latch; the active record under the trusted root; then, only when the record is absent and a native
supervisor applies, a bounded `launchctl print-disabled` + `print` readback of the supervising label.

A native supervisor applies only on macOS and only to the supervised runtime root:
`DISCLOSURE_RUNTIME_ROOT` equal to `DISCLOSURE_WORKER_SUPERVISED_RUNTIME_ROOT` (default
`/Volumes/AgentSSD/agent_system/services/disclosure_anchor/runtime`, the production root). Its label is
`DISCLOSURE_WORKER_LAUNCHD_LABEL` when set, else the production label, so a manual run from a production
shell is gated by the production job. Every other root (temp, test, scratch, offline) and every non-macOS
host has no native supervisor, never runs launchctl and keeps the record-only gate; status reports this
as `supervision` `unsupervised_runtime_root` or `not_macos`. The same binding is used by status, doctor,
release, reconstruction and the public-stop disable (`worker_supervision(settings)`).

The production label and the production runtime root are bound to each other only. A temporary root
declared supervised must name its own label, and the production root must not name another one; either
mismatch is `supervision` `label_root_mismatch`: launchctl is never asked, the native state is unknown
(`CONTROL_UNAVAILABLE`, starts refused, release closure unknown) and a public stop records
`native_disable` `failed:label_root_mismatch` next to its record.

The readback is tri-state, because a public stop whose record failed survives only as the native disable:

| readback | state | start |
|---|---|---|
| known disabled | `OPERATOR_DISABLED` (cause not proven by any record; never reported as a fault) | refused (78 / 503) until an explicit operator enable, or a reconstructed-then-released stop |
| unknown: launchctl missing, timeout or nonzero; no `disabled services` table or an unrecognized value; a `print` failure other than the exact not-found result | `CONTROL_UNAVAILABLE` | refused |
| known enabled, job not loaded or not last-exited 78 | `RUNNABLE` | allowed |
| known enabled, loaded, not running (or an unrecognized state), last exit 78 | `SUPERVISOR_ONLY_STOP` | refused |

"Not loaded" is proven only by the exact `print` result observed on this platform: exit 113 with
`Could not find service "<label>"` on stderr. Only `state = running` / `state = not running` are
interpreted; launchd's `last exit code = 78: EX_CONFIG` form is parsed by its leading integer.

## Status and release

`worker status --control-only [--format json]` (`worker-operational-control.v1`) reads only the control
files and, where a native supervisor applies, the launchd readback: no DB, MinerU or model. It derives
exactly the gate's state: `RUNNABLE`, `OPERATOR_DISABLED`, `PUBLIC_STOP`, `INVALID_STOP`,
`CONTROL_UNAVAILABLE` (files or launchd state unavailable/unknown/untrusted), `SUPERVISOR_ONLY_STOP`. JSON
adds `supervision` (`supervised`, `not_macos`, `unsupervised_runtime_root`, `label_root_mismatch`);
`native_supervisor` is `null` without a supervisor (`label_root_mismatch`: an unknown readback with
`service_target` `null`), else `service_target`, `available`, `disabled` + `disabled_detail`, `loaded`,
`running`, `pid`, `last_exit_code`, `print_detail` and `closure` (`closed`, `running`, `unknown`); `null`
fields are unknown, never absent. Exit 0 only for `RUNNABLE`, else 3. Plain `worker status` keeps its
stdout shape, reports a stop on stderr before the business query and exits 3 while stopped. Doctor adds
`worker operational control`: FAIL for a record, invalid, unavailable or unknown control state (DB not
needed), WARN for `OPERATOR_DISABLED`/`SUPERVISOR_ONLY_STOP` (the gate still refuses them).

`worker release-circuit --expect-sha256 --decided-by --reason --fixed-by [--dry-run]`: takes the
`WORKER_NS` singleton, requires proven closure, then under the control lock rereads the active raw bytes
and hash, archives
them create-only (an existing archive must hold the same bytes), writes the release decision create-only,
fsyncs, rechecks the active identity and only then unlinks it and fsyncs the directory. Any failure leaves
the stop. A replay after a crash is idempotent only for the same stop and decision; a stale or different
hash, a conflicting decision or a live owner refuses. Closure means: the singleton is held (every worker
takes it before business), the supervising job's readback is `closure=closed` (not loaded, or loaded and
`not running`) when a native supervisor applies, and a readable process table shows no known
wrapper/worker Python or owned MinerU/semantic child. A running job or live child, an unknown launchd
result, or an unreadable process table refuses with 75 and keeps the stop. The process scan matches known
command shapes only, so it can add blockers but never proves an unknown alias absent. `--dry-run` takes no
lock file, singleton, archive, record or enable; it reports the singleton owner as unknown and
`native_closure` (`not_applicable`, `closed`, `running`, `unknown`), `process_closure` (`no_known_owner`,
`owners_alive`, `unknown`) and the blockers. Invalid records are released by their exact hash;
symlinked/non-regular/unhashed records first need trusted-storage repair. A release record alone never
permits a start. Release never enables or starts the job and never touches business DB state.

`worker record-circuit-stop --from-disabled --evidence --evidence-sha256 --decided-by --reason
[--dry-run]` reconstructs an active record for a natively disabled job whose automatic record failed. It
requires the supervised root on macOS, a live disabled readback, no active record, and hash-bound
evidence (normally the logged
`STOP_RECORD` line); it writes `record_origin=operator_reconstructed` and never claims to be the original
automatic record. Release it the same way. An ordinary maintenance disable needs no record: confirm it at
install time instead.

After release, the operator explicitly `launchctl enable`s the label and `kickstart`s it (no `-k`) if
loaded and inactive, or bootstraps the accepted plist if unloaded.

## Residual limits

- A kill between latch and persistence, or a file syscall that hangs, can leave no durable record; the
  loaded job still does not restart a nonzero exit.
- `SUPERVISOR_ONLY_STOP` cannot distinguish a both-channels-failed public stop from the wrapper's
  missing-env 78; the worker log decides.
- The supervised binding is an exact configured path. A production runtime root spelled differently from
  `DISCLOSURE_WORKER_SUPERVISED_RUNTIME_ROOT` gets no native gate, no sentinel check and no native stop
  channel; status shows `supervision=unsupervised_runtime_root`, which deployment must verify is
  `supervised`.
- The binding is configuration, not a proof of launchd parentage: any process over the supervised root
  (launchd-spawned or manual) disables the bound label on a public stop, which is the intended common
  stop. `launchctl disable` from a context without access to the `gui/<uid>` domain (for example an SSH
  session) can fail; the record then carries `native_disable` `failed`/`unknown` and remains the gate.
- While the production label is disabled for any reason, every business start over the production root
  refuses, including attended manual pipeline/admin/replay entries; an explicit enable is the recovery.
- With `SuccessfulExit=true`, crashes, watchdog exits and TCC failures also stay down until an operator
  acts (no automatic recovery promise); only a reboot/login reloads the job.
- A watchdog exit writes no stop record of its own; the log line and last exit 70 are its evidence. A
  genuine fault that first reaches a fallback latch after the watchdog decided to exit is not recorded
  either: it cannot be told apart from one the exit induced, exit 70 keeps the job down, and the fault
  recurs after the operator restart.
- The legacy-sync resident parse plane does not classify faults; its failures stay item-local or exit
  nonzero.
- A long stop does not by itself force a re-parse: the task protocol's expiry
  (`scripts/windows/mineru_heap_trim_compat/agent_task_protocol_v2.py`,
  `_records_without_expired_tombstones`) evicts only consumed records past retention, not completed
  results that were never acknowledged.
