# Shared provider source semantics

Production admission and source-only diagnostics share the provider-content codec,
native-source reconciliation/finding rules, and Unit build/replay kernel. Sharing
these mechanisms does not give a diagnostic result production admission authority.

`application/contracts/_provider_content.py` owns the existing document payload
codec and profile, media and canonical raw-preimage checks. The production envelope
retains its document/run ownership, source/path/page checks and canonical outer
encoding. Its error is re-exported as the same class object. Pretty envelope bytes
and compact raw JSON fragments retain their distinct canonical representations.

`application/contracts/provider_source_semantics.py` owns the existing source
observation, reconciliation and finding value classes, plus shared binding
validation and effective-document projection. The old admission module re-exports
the same value classes. `application/services/provider_source_semantics.py` applies
the existing ordered repair/finding rules; it performs no IO and does not alter
their thresholds, precedence or kind/version labels. Original raw blocks, hashes
and artifact identities remain unchanged when the effective text is repaired.

`provider_unit_builder.py` has one private input and one build/replay implementation.
Production entrypoints still require `AdmittedProviderDocument`; they now explicitly
reject other runtime types before accessing properties. The separate source-only
entrypoints require `ProviderSourceSemantics`, the pinned `ParserTargetIdentity`,
and a canonical `semantic_record_sha256`. They validate the original provider
content before using the effective view. Both replay routes retain source/digest,
membership, ownership, destination and transform checks. Production Unit hashes,
locator versions, conservation rules and quality predicates are unchanged.

The source-only builder digest is a caller-supplied reference in that interface,
not proof that record bytes or a PDF were read. The bounded records and comparison
below bind the actual serialized bytes. Independent file reads still require an
owned runtime adapter. Source-only drafts cannot be supplied as an admitted
capability and must not be published through production persistence. Arbitrary
in-process Python is outside this API type boundary's threat model.

This extraction does not implement owned producer/verifier processes, quality
qualification evidence, public consumer confirmation or a real GPU batch. Those
remain separate M6 runtime and acceptance steps. Compatibility checks compare the
complete production envelope/build/replay outputs with the frozen prior source;
independently authored semantic and rejection cases cover the new entrypoints.

## Bounded semantic and build records

`source_semantic_record.py` defines `m6.source-semantic-record.v1`, a complete
canonical compact UTF-8 record of the source observation, parser target, original
provider document, native observations, derived repairs and quality findings.
`decode_source_semantic_record` validates source/page binding and freshly rederives
the exact claims. The separately typed `parse_source_semantic_candidate` preserves
structurally valid cross-source and derivation differences for diagnosis. It does
not return the strict decoded type or expose admission authority. Both parsers
reject malformed original content, unknown fields and loose scalar types.

`source_semantic_build.py` defines `m6.source-semantic-build.v1`, containing the
source-record hash, current builder version, explicitly empty hint arrays, complete
build and ordered quality occurrences. Every Unit retains all fourteen current
fields and the complete v9 locator. Decoding recomputes the three Unit hashes;
it does not run private build validation or replace a divergent candidate with a
fresh build. Correctly rehashed semantic mistakes therefore reach comparison.

All encoders and decoders require explicit positive exact-integer byte ceilings.
The shared `diagnostic_json.py` rejects noncanonical JSON, duplicate keys, nonfinite
numbers and nesting beyond 64 containers. Encoding charges UTF-8/escape chunks;
record encoders reject unavailable projection capacity before expanding DTOs.
Overflow is a visible failure, not a truncated successful record. These are
per-record bounds; total retained evidence, child output and deadlines belong to
the future runtime owner.

## Shared quality and complete comparison

The builder and diagnostics use `services/provider_quality.py`. Existing encoded
text and truncated-title predicates and their scope are unchanged. Occurrences
preserve source-finding preimages, unbound table parts/reasons, encoded block
identity, or heading occurrence/fragments. Identical evidence is deduplicated;
different source occurrences remain distinct. Segment-only unassigned evidence
has no fabricated Unit. Actual Unit counts come from the complete build, never
the number of reasons or a failed operation converted into zero.

`compare_source_semantic_candidate` takes four bounded canonical byte records:
candidate source/build and reference source/build. It strictly decodes and freshly
rebuilds the reference with shared source semantics, empty hints and shared quality
assessment. The full encoded reference must match; two equal but wrong inputs
cannot act as their own oracle. It preserves candidate discrepancies and compares:

| Check | Evidence compared |
| --- | --- |
| source identity | Source/target/provider identities and every record/build/locator reference. |
| page closure | Physical count, every provider page including blank pages, and Unit pages. |
| block conservation | Complete original blocks and ownership occurrences with multiplicity. |
| table segment conservation | Complete original physical segments and bound/unassigned occurrences. |
| logical table conservation | Ordered owner/continuation partitions and exact unbound reasons. |
| retrieval target binding | Ordered bindings and actual source-scalar/destination-transform replays. |
| repair binding | Fresh native derivation, source claims and dependent locator occurrences. |
| finding binding | Fresh native derivation, source/locator findings, quality occurrences and Unit statuses. |
| reading order | Complete ordered Units, including payload, hashes and locators, plus unassigned parts. |
| heading occurrence closure | Occurrence chains/fragments, titles and heading paths. |

The canonical comparison report binds the hashes and byte counts of all four
original inputs, each outcome and bounded mismatch locations. Malformed reference
or candidate data remains a visible error. `artifact_closure` and
`independent_rebuild_match` are always `unverified` here. No pure result grants
scorable pages, publication, cleanup or ACK; producer/verifier process ownership
and actual file-read closure remain separate runtime work.

## Held source and owned-quality wire primitives

`pdf_text_observation.py` exposes a separate
`observe_pdf_text_rectangles_from_open_file` entrypoint. It rewinds the caller's
held binary stream under the existing PDFium lock and shares the path entrypoint's
exact extraction and handle-closing loop. PDFium leaves the caller stream open.
The caller remains responsible for the original file identity, content seal and
exclusive stream use; the helper does not reopen a pathname or certify those facts.

`application/contracts/mineru_diagnostic_quality.py` defines immutable private
budget, frame, stream, bounded-error and retained-file values. A thirteen-byte
`M6Q1` header carries a fixed frame kind and an unsigned 64-bit payload declaration.
Declarations are distinct from actual observed bytes. Stream totals remain unknown
until EOF; retained and discarded bytes must exactly sum to observed bytes. A
partial or discarded frame cannot claim a complete retained payload hash. Error
records explicitly distinguish retained UTF-8 text from truncation.

Six budget fields map to sixteen fixed retained slots. Requests and raw control
streams have separate per-file control limits and both consume the total budget;
source, build, evidence and stderr have their own limits. The sum of slot ceilings
need not equal the aggregate allowance. Primitive codecs apply projection checks
before materialization and use the shared bounded canonical JSON implementation.
These values carry no process or filesystem authority. Actual byte charging,
create-only file ownership, frame sequencing, child closure, qualification and
versioned lifecycle integration must be enforced by the runtime owner.
