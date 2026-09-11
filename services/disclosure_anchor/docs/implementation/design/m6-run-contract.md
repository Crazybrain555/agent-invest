# M6 run evidence v1

This private operational contract implements the R28 M6 plan's WP-B. Product
authority remains the repository protocol, L1 plan and service-purpose contract.
It does not change public v1, publication transactions, the existing source-first
ledger, the legacy commissioning limit, or the UTC-hour telemetry/KPI contracts.

## Scope and completion

`M6CorpusManifest` freezes full original PDF hash, bytes, pages, stratum, origin
and (for E2E) explicit document ID. Membership has one row per original source.
`M6CampaignScope.from_manifest()` projects the complete E2E membership and binds
the manifest hash. Its 10,000-entry wire/memory ceiling is an engineering bound,
not a prescribed campaign size. Ordinary and prepared SQL must apply this entire
membership before LIMIT/keyset pagination; slicing independent batches and
concatenating limited results is not equivalent. Existing 1..8 commissioning and
global recovery obligations keep their separate authority.

`M6RunSpec` freezes source/runtime/profile/deployment/owner/GPU identities, corpus,
quality plan, mode, original interval and finite evidence budgets. Hashes use exact
canonical model bytes, excluding no fields and storing no recursive self-hash.
Unknown/missing wire fields, duplicate JSON keys, noncanonical JSON and coercions
are rejected. All nested collections are tuples and closed immutable models.

`complete` on a run receipt means the **measurement and resource closure** are
complete. A complete zero is possible; it does not establish product acceptance,
representative quality, continuous supply, a qualified platform, or stability.
Those gates require independent adapter, corpus and repeated live-run evidence.
`short_batch` and `recovery_experiment` do not claim hour/stability acceptance.
Formal baseline/repeat specs require at least 3600 planned seconds and a measured
close no earlier than the original deadline. Every phase includes whole-run cost.

## Time and owner journal

Physical Windows QPC frequency and boot bind the clock domain using the existing
resident telemetry canonical input. Host identity, boot/clock, and owner process
incarnation are separate. T0 precedes run-specific input freeze/copy, auxiliary
process preparation, startup and warmup. Tclose follows validation, independent
confirmation, ACK/cleanup, verifier drain and child/resource closure.

Only the qualified Windows owner stamps receipt ticks and sequence. A producer
envelope carries its role, process incarnation and sequence; the stamp hashes the
entire envelope. The sink port requires caller authentication and durable append
before acknowledgement. Hash consistency alone does not authenticate a caller,
prove an artifact was read, or attest physical host identity.

Journal records are bounded, canonical UTF-8 JSON plus LF in physical append
order. Reordering them to repair the log is forbidden. Identical journal records
or identical producer retries deduplicate; changed bytes under the same identity
conflict. Producer identity includes incarnation, so sequence reset cannot alias
an earlier process. Missing producer/owner sequences remain incomplete. Business
commit, public confirmation and final qualification may arrive in different
orders after the owner has acknowledged admission; the credit time is the maximum
of their receipt ticks. Each final fact is immutable. Intermediate review progress
must not be sent as `document_qualified`; emit the frozen final qualification once.

Same-boot owner resume names its predecessor and retains exact T0/deadline/clock.
Recovery delay remains in whole-run elapsed time. A changed boot invalidates a
combined QPC denominator; raw evidence remains available and trusted metrics and
elapsed ticks are absent. Tail truncation, byte/record/attempt bounds, measurement
incidents and missing closure expose incomplete receipts. Malformed records,
identity conflicts and clock regression expose invalid receipts. IO/programming
exceptions propagate. `journal_prefix_sha256` and `journal_bytes_consumed` identify
the exact bytes consumed, even when a bound prevents reading the entire log.

## Credit authority

The E2E result is `first_qualified_publication`. Full source pages count once only
after manifest/admission, immutable winner/base/ledger, global source-first audit,
whole-document qualification, and independent public v1 confirmation agree.
Public reads target `disclosure_public.document_units_v1` or the v1 API, including
complete pagination and evidence artifacts. The confirmation binds both the public
units digest and exact global-history audit receipt. History whose coverage is
unknown never establishes novelty from absence. A historical first publication
is not reset by a different run, document alias, attempt, profile or old quality.

Window credit is `[T0, deadline)`. Final qualification, commit observation or public
confirmation received at/after the deadline can count only in whole-run totals.
Late quality review cannot backfill an earlier public-confirmation tick. Carry-in
is explicit and separate from fresh credit; same-run resume uses original T0 and
is not reclassified as a new run. E2E replay is excluded from first-source totals.

An admission observation may arrive at/after the deadline while stop propagation
is still within the declared budget and admission has not been acknowledged shut.
It is retained as `admitted_after_deadline`, contributes **zero** window/whole-run
pages, and retains all processing and closure cost/obligations. Beyond the stop
budget the run is incomplete; new admission after acknowledged stop/drain is
invalid. The receipt preserves admission ticks, requested/effective stop ticks and
close reason. An early stopped run that waits until the deadline is distinguishable
from continuous supply and cannot establish G4/G5 merely by having a complete
measurement receipt. Local conservative leases supplement this owner evidence.

Service diagnostics return `service_validated_source_pages`, never publication
credit. A service source needs full validation, qualification and diagnostic
disposal/ACK closure. Replay throughput is allowed and explicitly labeled, deduped
once per original source within the run. It cannot be substituted for the E2E KPI.

Successful attempts require a bound remote acceptance and matching remote
task/closure receipts. Every admitted attempt, including failures and carry-in,
must close. Failed attempts contribute time and zero pages. A final ACK/cleanup
fact is not a substitute for publication or quality. Missing ACK/cleanup, residual
children/resources or absent verifier drain prevents any trusted run numerator.
New evidence after claimed drain/close contradicts that closure.

The adapter must report each attempt final before verifier drain, retain owner
sequence across same-boot recovery, and never reopen admission during resume.
It must not freshly resubmit a manifest source marked carry-in. An exact already
recorded owner record can be replayed by the reader; a *new* owner stamp after
close, even for a producer retry, contradicts Tclose and makes the receipt invalid.
`stop_admission_effective` and `resources_closed` are each one terminal fact per
run. Producer retries must retain their original incarnation/sequence and bytes.
Report residual resources as incidents until final closure; a terminal residual
report leaves the measurement incomplete and cannot be replaced with a second
`resources_closed` event claiming success.

## Whole-document quality

`M6QualityPlan` cannot omit source identity, full-page closure, block/table/logical
table conservation, retrieval/repair/finding bindings, reading order, heading
occurrence, immutable artifact closure and independent rebuild checks. E2E also
requires public units to match. Every verified check references evidence bytes.
An adapter must actually execute the checks; the model is not that authority.

Mismatched source/provider page counts, missing/failed/unverified required checks,
or any unusable units exclude the whole document. Good pages cannot dilute failed
ones. A genuinely blank page, heading-only unit or legitimately empty document is
not itself a failure: full source/structure conservation still must be proved.
Reason dispositions are frozen before measurement. Unknown reasons and unexplained
needs-review units remain pending. Required independent reviews bind exact whole
observation bytes and evidence; rejected review excludes the source. This does not
change the business publication policy or add L2 semantic repair.

## Validation and implementation boundaries

Deterministic tests cover closed membership, canonical bytes, source/page/winner
conflicts, global history, late qualification, carry-in/replay, owner/producer
sequences, original-deadline resume, boot changes and failed closure. Operational
schemas are generated by the existing `disclosure_anchor.cli.export_contracts`
registry. No current public schema bytes change.

WP-D owner durability/clock/caller qualification, WP-E service execution, WP-F
campaign SQL/resident integration, WP-G public verifier and WP-H real source
quality checks remain separate implementation/live gates. Synthetic evidence tests
validate accounting invariants; they do not certify PDFs, PostgreSQL persistence,
Windows physical ownership, continuous one-hour supply or stable throughput.

## R28/Fable detail-plan adjudication

Adopted: explicit campaign projection; physical receipt clock; existing global-first
ledger; bounded immutable evidence; service/E2E separation; all-attempt closure;
whole-document qualification; existing schema export routing.

Corrected: no cross-boot subtraction; no arbitrary 4096 campaign target; no sorting
a damaged owner journal; no role-only dedup key; no late-quality backfill; no
mandatory hour length for short batches; service replay remains labeled diagnostic
throughput; no synthetic nonempty-unit/one-unit-per-page quality rule; unknown
quality reasons remain unresolved instead of silently changing publication policy.

The first implementation review's deadline propagation finding is addressed by
the explicit excluded late-admission outcome. Malformed nesting and zero-duration
runs now produce fail-closed receipts, and carry-in lookup/state bookkeeping remain
linear in bounded evidence. The hash remains a hash of the exact consumed prefix,
including a partial tail; an over-bound record is not included. Discarding a partial
tail from that identity would hide the bytes responsible for incompleteness.
`events_total` counts consumed records (including malformed records and a partial
tail), consistent with `journal_bytes_consumed`; the first rejected over-bound
record belongs to neither count nor prefix hash.
