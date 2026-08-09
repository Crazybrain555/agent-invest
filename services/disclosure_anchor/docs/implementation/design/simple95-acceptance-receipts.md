# Simple95 run and diff acceptance receipts

Status: Round 1 final-candidate observer contract. This design does not change
parser, heading, owner, builder, publication, API, database, worker or retrieval
semantics.

## 1. Purpose and authority boundary

`simple95-run-receipt.v1` binds one audited document run to immutable code,
corpus and parser artifacts. `simple95-diff-receipt.v1` compares two valid run
receipts and classifies their changes. Receipts are release evidence only:
they are not database records and are never builder or PublicationGate input.

The corpus tool exposes the exact `UnitDraft` list and `DocumentAuditReport`
already produced by `prepare_and_audit_units()`. The receipt observer consumes
that result after audit; it does not rebuild owner placement through a second
authority. Production modules do not import the receipt script.

External analogues informed only the evidence shape: in-toto Statement v1
binds immutable subjects by digest, SLSA provenance separates build inputs from
run outcome, and Reproducible Builds requires identical outputs for identical
inputs. The service retains its existing sorted UTF-8 canonical-JSON profile;
it does not claim RFC 8785/JCS conformance.

## 2. Run binding

Every receipt carries and validates:

- full 40-character `code_commit_sha` and the exact corpus-manifest SHA-256;
- document/provider/run identity and the verified source-PDF SHA-256;
- parser-target, parse-receipt, frozen NormalizedIR and source-evidence hashes;
- a sorted role-to-`{sha256,size_bytes}` map for every present provider
  artifact, after reading and verifying the actual bytes;
- the full PublicationGate version, decision, checks and diagnostics;
- findings, public `hierarchy_status`, retrieval-rules version and unit count.

Receipt generation requires `normalized_ir.v4`, the current
`parser-target.v2`, a present parse receipt whose endpoint/model/PDF/target
semantics replay, and present core provider artifacts. Frozen v1 generations
remain auditable but cannot receive a current run receipt. No null, placeholder
or reconstructed parse-receipt identity is legal.

## 3. Four semantic projections

### Content

`content_multiset_root` directly reuses `content_hash_aggregate()` over
recomputed canonical unit content hashes. Sorting removes asset/order identity;
retaining every hash preserves duplicate multiplicity. Each canonical leaf
contains the payload needed to replay its content hash. `asset_id` is never
serialized.

### Structure, owner and order

The production `structure_hash` is recomputed and retained, but by itself it
does not identify equal-text heading occurrences. The receipt-only ordered leaf
therefore also binds:

- the canonical `content_hash` carried by this physical occurrence;
- published `order_index`, `payload_kind` and `heading_path`;
- coarse owner `document_root|heading_section` and `UnitDraft.section_path`;
- `source_order`, `source_order_phase` and `native_order_anchor`.

Detached or empty-section drafts are document-root owned. This is an
observation of existing placement, never a new owner decision. Diffing treats
a payload replacement at one unchanged position as content-only. If a retained
canonical content occurrence moves to a different owner/order identity—even
when its normalized search text is equal—the change is structural. Duplicate
occurrences remain interchangeable and are never paired by `asset_id`.

### Query and search plan

Every draft first passes `materialize_search_projection()`. Its current v2
query projection/hash, effective ordered target/grouping plan and normalized
search atoms are recorded. The query delta compares projection/search-plan
multisets rather than asset IDs or unit order, so a pure physical reorder is a
structure delta. `search_atoms_root` remains separately visible: a payload
edit may change atom text while still being classified as content, not as a
search-plan change.

### Publication outcome

The receipt records the unmodified result of
`evaluate_publication_gate_v1(report)` and the ordered audit findings. A gate
check/diagnostic/decision or findings change is a publication-outcome delta.

## 4. Diff explanation

The diff validates both inputs, recomputes all embedded unit hashes and roots,
and then reports:

- `content_delta`;
- `structure_order_owner_delta`;
- `query_search_plan_delta`;
- `publication_outcome_delta`;
- per-family `changed_fields` counts and run-binding counts;
- `root_explanations` plus an always-empty `unexplained_deltas` array.

An ordered query/search-atom root may change as a downstream consequence of
content or physical order. The explanation names every contributing family.
A changed semantic root with no valid explanation is rejected rather than
emitted.

## 5. Determinism and fail-closed rules

The only accepted bytes are one canonical JSON object followed by exactly one
LF. There are no timestamps, absolute paths, random identifiers, worker counts
or process IDs. A loaded receipt must already have that byte encoding.
The `build-run` command also requires the supplied full SHA to equal checkout
`HEAD` and refuses a tracked or untracked dirty checkout.

Generation or validation stops on malformed fields, non-current parser target,
missing/mismatched artifacts, unsafe paths, stale stored unit hashes,
non-contiguous unit order, cross-layer hash/root disagreement, contradictory
PublicationGate diagnostics/checks/decision, disagreement between gate error
counts and audit findings, unsupported hierarchy status, cross-document diff
or unexplained delta. A receipt cannot repair any input.

## 6. Tool surface

Run from `services/disclosure_anchor` with `PYTHONPATH=src`:

```text
python -m scripts.simple95_acceptance_receipts build-run \
  --manifest MANIFEST.jsonl --data-root SERVICE_ROOT \
  --code-commit-sha FULL_SHA --source-replay --out run-receipts.jsonl

python -m scripts.simple95_acceptance_receipts diff \
  --before before.jsonl --after after.jsonl \
  --document-id DOCUMENT_ID --out diff.json
```

Outputs are created with exclusive-create semantics; the tool never silently
overwrites prior acceptance evidence.

References: [in-toto Statement v1](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md),
[SLSA build provenance](https://slsa.dev/spec/v1.2/build-provenance),
[Reproducible Builds definition](https://reproducible-builds.org/docs/definition/),
[RFC 8785](https://datatracker.ietf.org/doc/html/rfc8785).
