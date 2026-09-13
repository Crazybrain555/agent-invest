# Functional M6 service batch

`adapters/runtime/m6_service_batch.py::run_service_batch` runs complete original
PDFs through the existing v2 diagnostic lifecycle. It uses
`application/services/m6_service_controller.py` to refill available slots with the
oldest work that fits every declared resource dimension. At most `max_in_flight`
futures exist; the executor never receives the whole pending corpus in advance.
The current API supports up to 64 concurrent futures, but the actual deployment
and credit envelope determine permitted concurrency, not that engineering limit.

## Ownership and recovery

Before any task dispatch, the batch persists its exact inputs, source identities,
attempt/fence/epoch, options, API/upstream, original sibling journal paths and
resource envelope. The existing private journal owns the single batch writer.
A dispatch intent is durable before executor submission. The worker creates and
owns its original E1 journal; source observation, final-POST guard, protocol
requests, artifacts, cleanup and consumed ACK remain E1's actual operations.
Batch callbacks append only small dispatch/disposal references on the controller
thread. Neither V4 claims nor PostgreSQL processing runs are fabricated.

The batch derives its maximum 47 inputs from the existing 96-record journal:
one binding plus two records per input. This is a functional evidence ceiling,
not a tuning arm, corpus-quality criterion or replacement for the existing
10,000-member campaign/PG integration. All journals retain the existing 7200s
initial lifetime bound and the original absolute deadline. Changing the complete
request, clock or deadline is not resume.

Resume reconciles only previously dispatched attempts. It first verifies every
already-disposed attempt using E1's read-only `require_disposed` guard and exact
final-proof hash. Those journals remain charged as retained evidence. All other
dispatched attempts are conservatively charged their full reservations before
recovery begins. They reuse original keys; no fresh POST or undispatched input is
admitted. Missing, partial, foreign or damaged evidence remains unresolved.

`ServiceBatchResult.retained_or_unresolved_credits` includes the batch journal,
all proved retained attempt journals, and full reservations for every unresolved
dispatch, including one not yet scheduled by this recovery invocation.
`unreconciled` and `not_dispatched` retain that distinction. A controller result
alone describes one invocation's futures and is not the complete recovery ledger.
Exceptions preserve original causes and partial results; they never silently
drop pending resources. A failed provider that actually disposes and ACKs is a
closed failed result, while an unknown exception closes new supply and preserves
its obligation. Running companions drain; running futures are not cancellable.

## Actual resource limits

Reservations use the actual E1 limits rather than caller estimates: complete
source bytes up to 512MiB, provider ZIP reservation 256MiB, unpacked output up to
16GiB, decoded bytes up to 4GiB, and retained journal up to 16MiB per attempt.
Temporary disk also includes source, ZIP and output; the batch journal's own
16MiB is charged separately. Other vector dimensions include source pages, task,
materialization, output and ACK counts. These conservative reservations bound
admitted work; they are not process RSS assertions or measured resource usage.
The current implementation does not lower the underlying extraction limits.

## Explicit CLI

From the service root with the existing environment:

```sh
PYTHONPATH=src .venv/bin/python -m disclosure_anchor.cli.m6_service_batch --request /absolute/runtime/batch-request.json
PYTHONPATH=src .venv/bin/python -m disclosure_anchor.cli.m6_service_batch --request /absolute/runtime/batch-request.json --resume
```

The owned regular request is at most 1MiB, canonical UTF-8 JSON without a trailing
newline. It has exactly `contract_version=m6.service-batch-request.v1`, `batch_id`,
`inputs`, `api_url`, `server_url`, `options`, `journal_root`,
`clock_identity_sha256`, `deadline_ns`, `max_in_flight` and `credits_limit`.
Inputs contain the `ServiceBatchInput` fields. Option and credit mappings use
the current `ParserOptions` and `ResourceCreditVector` constructors, including
their defaults for omitted optional fields. The durable runtime binding stores
all resulting values; changed defaults therefore cannot silently alter resume.
The caller
freezes the real Mac `diagnostic_continuous_clock()` identity and absolute
deadline before running; this CLI never creates a new deadline or guesses a
clock on recovery. Runtime paths remain outside Git under the service data root.

SIGINT/SIGTERM close new admission; the final POST boundary checks that signal
and original deadline again. Previous signal handlers are restored when the
command returns. A finished complete batch exits 0; stopped/unreconciled work
exits nonzero. Runtime errors remain visible along with available partial JSON.
The controller's cooperative checks do not prove hard termination of arbitrary
blocked decoder/OS calls, physical Windows ownership or bounded native stop.

## Acceptance boundary

Current results explicitly say
`functional_lifecycle_only_quality_unverified`. Real source/provider artifact
validation and disposal are necessary functional facts, but do not imply full
Unit semantic qualification, public publication, formal G2, or hourly throughput.
The new source/provider qualification family in [m6-run-contract.md](m6-run-contract.md)
is separately versioned; its actual verifier/accounting connection remains a
distinct implementation step. Existing parser acceptance is reused where its
identity applies, without inventing passed checks.

Before live load, verify the actual original WSL GPU and inference execution,
not merely API metadata health. Reconcile current driver/WSL boot/container
image and start epochs with the runtime identity; an epoch change cannot reuse
old live-run measurements. Keep driver/device failures visible and repair the
original platform path before commencing real complete-PDF batches.
