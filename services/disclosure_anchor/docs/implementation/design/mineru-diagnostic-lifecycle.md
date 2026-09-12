# MinerU diagnostic lifecycle v2

`adapters/runtime/mineru_diagnostic_lifecycle.py::run_diagnostic_attempt_v2`
executes one whole-source protocol-v2 diagnostic through terminal observation,
artifact validation, local disposal, provider ACK and verified remote absence.
It returns a private `mineru-diagnostic-disposal.v2` receipt. It does not publish,
write a database, construct a production completion witness, or qualify M6 pages.
The existing v1 `run_diagnostic_pdf` and opt-in smoke CLI retain their interfaces
and behavior. A multi-input M6 controller and its CLI are separate work.

The caller supplies the original attempt/fence/epoch, PDF SHA-256/byte count/page
count, API and upstream URLs, complete parser/runtime options, clock identity,
absolute continuous-clock deadline and continuous-clock function. A declared
quality verifier is paired with its SHA-256. All these bindings are checked
before a resumed attempt can issue another remote operation. The parser's
`timeout_seconds` never resets the v2 deadline. Native Windows QPC values are not
interpreted as local nanoseconds.

Before submission, a bounded child independently rereads the sealed snapshot
with the existing `pdf_source_observation_process` and verifies its actual PDF
format, physical page count, bytes and hash. Its exact output binds the snapshot
record. The parent enforces the original deadline and 8192-byte combined output
limit and waits for that exact child and closes its pipes before proceeding.
An intent is sealed before spawning. If child closure is not proved, that intent
blocks another probe/submission on resume and retains the source resources.
Termination has a separate bounded one-second reap allowance; this never grants
new observation time. Failure retains the exact child object in the propagated
unresolved error for caller reconciliation, rather than claiming that it exited.
Supplying a plausible page count alone cannot authorize a POST.

## Evidence and recovery

The journal has one exclusive local writer. Its header and immutable hash-linked
records bind the original private root and budget. The closed phase replay in
`mineru_diagnostic_phases.py` checks prerequisites, identities and bounded wire
evidence. Exact bounded HTTP bytes and their status/hash are retained before
protocol interpretation; invalid evidence stops later automatic continuation.
Pending polls are not individually journaled. Terminal, lease, lookup and ACK
observations use the existing closed protocol parsers and canonical routes.

| Durable state | Permitted continuation |
|---|---|
| New source snapshot sealed and physically observed | One new-attempt POST with the prepared key and exact snapshot bytes. |
| Resumed submit intent or missing submit response | GET by the original key only. A 404 remains unresolved; no new key, resubmission or invented `not_submitted` receipt. |
| Accepted task | Poll that task under the original deadline. Completed and failed terminals remain distinct. |
| Sealed ZIP or output | Reverify original identity and bytes, then reuse. Corruption never causes redownload or overwrite. |
| Object created without a durable identity, or unsealed interrupted transfer/extraction | Retain it for explicit reconciliation; do not adopt a pathname. |
| Validation and cleanup intent | Remove only the sealed original inventory. Continue a partial authorized cleanup, including an empty root already moved for reclamation. |
| Local closure and ACK intent | Reconcile original-task absence or, if the same terminal task remains, retry its ACK within the finite exchange budget. |
| Remote absence and final receipt | Replay the final evidence without another remote effect. |

The forward hash chain does not prove that a valid suffix was never removed.
Missing records therefore grant no new POST, overwrite, redownload, or cleanup
authority. A damaged/pending append is retained rather than repaired by guessing.
Transport exceptions remain visible; a later explicit resume performs permitted
reconciliation. A failed provider terminal may clean and ACK its own obligation,
with `outcome: failed`, no result artifact and no invented successful quality.

## Local resources and closure

`mineru_diagnostic_resources.py` registers the actual FD-derived identity before
payload writes. Original inodes, type, owner, private modes, link count and sealed
content are rechecked before reuse. Parent directories are opened through pinned
FDs and compared with their original identities before writes. Payload opens
reject special files without first blocking on a substituted FIFO.

The disposable tree contains only the private source snapshot, retained ZIP and
materialized output. The caller's original PDF is never deleted. A completed
output inventory records original directories/files and content hashes. Readers
and verifier callbacks finish and their resources close before cleanup begins.
Source/output seals are rechecked before and after validation and verifier use.

A cleanup intent persists a nonce and binds the validation record. Each original
child is first moved to a deterministic reserved name derived from that nonce,
then rechecked before removal. Replacement bytes moved during a raw-name race
remain quarantined. An emptied original root is moved to `resources-reclaim`
before removal, allowing recovery after that move. Unknown/reappearing entries
are not adopted. This is an exclusive private diagnostic namespace, not an
isolation boundary against a hostile process with the same OS identity rewriting
arbitrary reserved names or descriptors.

## Quality and ACK authority

The optional verifier receives the sealed snapshot path, output path and parsed
`ProviderDocument`. Its result has exactly `status`, nonempty `reason` and an
object `report`; status is `pass`, `fail`, `needs_review` or `unverified`. The
wrapper binds the declared verifier SHA and persists this evidence outside the
disposable tree. Without a verifier, quality is explicitly `unverified`. No Unit
counts, public hashes or scorable qualification are synthesized. Callback errors,
invalid evidence or modified source/output preserve the resources.

The final receipt distinguishes an actual raw HTTP 200 consumed ACK plus
canonical task absence from absence reconciled under an already sealed local
closure and ACK intent. It retains original raw proof hashes and never fills in
a synthetic successful ACK body. Bare absence before that authority is not
disposal.

The durable final seal stores the expanded proof's hash and original record
references. Exact wire bodies and quality reports remain in their original
bounded records; they are expanded when returning the proof, rather than copied
again into a final record that could overflow when multiple valid responses are
near their individual limits.

## Bounds and validation boundary

The source is at most 512 MiB; protocol JSON is at most 1 MiB; retained results
use the existing 256 MiB reservation. Existing ZIP member, uncompressed and
decoded limits apply. Journal bounds remain 96 records, 2 MiB + 8192 bytes per
record, 16 MiB total and a maximum initial lifetime of 7200 seconds. Original-key
lookup and resumed ACK reconciliation each have at most four durable exchange
intents. Each is persisted before its GET, even when the response is lost; an ACK
exchange can retry only the same terminal task after the actual lookup. The
initial new ACK has its own intent. Space and deadline are checked before another request. Exhaustion is unresolved,
not permission to drop evidence. Incomplete file creation remains visible.

Deterministic validation uses independent stateful HTTP fixtures, actual ZIP
materialization/artifact reads, phase-cut recovery and ownership/ACK failures.
These tests establish the diagnostic mechanism, not real PDF semantic quality,
GPU throughput, public PostgreSQL publication or M6 G2–G6 acceptance. Cooperative
guards also do not prove hard termination of arbitrary blocked OS/decoder calls;
supervised source/quality workers and whole-run drainage remain controller work.
