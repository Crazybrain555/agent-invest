# M6 read-only whole-document qualification (V3-aware)

`adapters/runtime/m6_qualification_verifier.py` is the first implementation of
the `M6QualificationSource` port. It qualifies a publication that has already
been committed; it does not admit, route, publish, emit owner events or credit
anything. `cli/m6_qualify_readonly.py` is the thin operator entry.

## Inputs and identities

- Private facts (`read_private_qualification_facts`): one app/worker identity
  `READ ONLY REPEATABLE READ` snapshot, the same discipline as the private
  publication verifier. It reads the V4 checkpoint/winner/materialization intent,
  verifies the stored publication closure, and loads the document, processing
  run, parse owner and security. The public reader principal is never used for
  private tables and no privilege is added. A supplied admission must equal the
  persisted checkpoint identity field by field; otherwise the admission is
  derived from that checkpoint and recorded as such.
- Sealed publication: the winner's immutable readiness reference is verified
  through the existing read-only readiness verifier (parser tree inventory plus
  the three derived resources). The sealed request must close the admitted
  attempt/document/run/source and the run's provider-record and receipt paths.
- Source: full `ProviderDocumentAdmission.admit` (record hash, owner facts, raw
  PDF hash/pages, frozen bundle rebuild equal to the envelope, native text
  reconciliation). `admit_materialized` is not accepted here because it skips
  the bundle rebuild. A second raw observation supplies the byte count.
- Semantic receipts: the V3 sidecar is read by sealed hash/byte count, parsed
  with the typed V3 decoder, validated for run/order/uniqueness, re-encoded
  canonically, compared with the sealed request rows, and bound row by row to
  the sealed Units' locator and routed-draft hashes. The nested V2 receipts go to
  `SemanticRouter.replay` with a refusing executor; `route` is never called.
- Public digest: only an independent public reader receipt
  (`m6.public-consumer-audit.v1`) supplied with its expected hash. Without it the
  qualification is unavailable; no digest is invented because the 39 public
  columns include database timestamps that cannot be rebuilt from sealed Units.

## Read budget

One `_ReadBudget` (the public consumer's mechanism) spans a whole `qualify`
call. The readiness verifier charges the parser tree inventory and the three
derived resources through the shared read-only store; the second V3 sidecar
read and the public receipt bytes are charged to the same budget. Before the
expensive admission the adapter binds the source and bundle paths to the sealed
publication, `lstat`s the raw PDF (regular, non-symlink, exactly the admitted
byte size) and reserves the provider record once, the source at six streamed
passes plus the five-byte signature probe, and the sealed parser tree at three
passes. The counts come from the adapters' own read paths: the source
observation hashes the file twice and loads it once for the page count, the
native-text observation hashes it before and after one PDFium load; the pinned
bundle reader streams every file during the tree scan and reads role files
again for content and UTF-8 validation while images are read only by the scan.
The two PDFium loads are metered as one whole-file pass each; their internal
on-demand reads are not metered, so the reservation bounds the adapter's
explicit streams, not every operating-system read. The report records the scope
and the final reserved totals. After admission the source's stat identity must
be unchanged. Exhausting the budget at any point makes the qualification
unavailable (`read_budget`), never a semantic failure.

The public receipt is accepted only with the consumer verifier's exact
top-level field set, the reader principal on `invest_engine` (the same database
as the private facts), and a `publication_committed` payload that equals the
winner's own identity except for the ledger sequence, which is recorded.

## Checks

The thirteen `M6CheckId` values are produced with these sources:

| check | source of truth |
|---|---|
| source_identity | admitted source hash, envelope input hash, observed bytes/hash, winner base identity, sealed upstream evidence, provider record hash vs run/preparation |
| independent_rebuild_match | `admit` succeeded, i.e. the rebuilt bundle projection equals the sealed envelope |
| artifact_closure | readiness resources verified and the pinned bundle tree hashed during rebuild |
| page_closure | provider pages equal admitted pages and are contiguous; sealed page numbers in range; unit pages equal |
| block/table_segment/logical_table conservation, retrieval_target_binding, repair_binding, finding_binding, heading_occurrence_closure | `compare_build_conservation(sealed drafts, fresh drafts)` with equality on both sides; retrieval additionally replays every sealed binding through one local `ProviderUnitReplayContext`; finding additionally requires the quality assessment to succeed |
| reading_order_contiguity | V3 outer validation, replay success, sealed indices 1..N, and the existing pure `_unit` projection of replayed drafts equal to the sealed pre-ID Units |
| public_units_hash_match | receipt hash equals the expected hash, canonical contract, reader principal, admission/publication/document/run identity, every row has the 39 public fields and its 18 row fields equal the sealed projection, recomputed digest equals the declared digest |

`logical_table_conservation` compares fresh unassigned table parts with the
sealed side. The sealed side is empty because the publication request builder
and `publish_run` reject unassigned parts; a fresh unassigned part is therefore
a conservation failure, not a new quality gate.

Review reasons are derived from the existing quality assessment of the fresh
build (`assess_source_build_quality`): the sorted set of occurrence
`reason_id` values. Needs-review and unusable counts come from the sealed Unit
statuses. The verifier never adds reasons, reviews or acceptances; unknown or
review-required reasons resolve to `review_pending` in `qualify_document`.

Every `pass`/`fail` result carries the canonical hash of its detail object; the
details are embedded in the report. A failed contract check after admission is a
named `fail`; readiness, admission and public-receipt problems make the
qualification unavailable and only the report is written.

## Outputs

`receipt_sink(name, bytes)` receives `qualification-report`
(`m6.readonly-qualification-report.v1`) and then `qualification-evidence`
(`m6.qualification-evidence.v1`). The CLI writes them under a new output
directory with `O_EXCL`, adds `qualification.json` when a plan is given, and
ends with `run-summary.json`. Exit code 0 means every attempt produced evidence;
2 means at least one was unavailable; other errors return 1. Verdicts are data.

## Limits

This adapter reads the current document classification for the semantic
context; if it drifted since publication, replay fails visibly instead of
reconstructing history. It does not read the public views itself, does not
authenticate the producer of a receipt beyond hash and principal fields, and
does not stamp owner ticks. Assembling `document_qualified` events, the public
consumer and the reducer into a live run remains a separate boundary.
