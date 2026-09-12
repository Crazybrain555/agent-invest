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

The source-only digest is a caller-supplied reference in this pure interface, not
proof that record bytes or a PDF were read. A later diagnostic evidence adapter
must define and verify its versioned semantic-record domain and independent file
reads. Source-only drafts cannot be supplied as an admitted capability and must
not be published through production persistence. Arbitrary in-process Python is
outside this API type boundary's threat model.

This extraction does not implement owned producer/verifier processes, quality
qualification evidence, public consumer confirmation or a real GPU batch. Those
remain separate M6 runtime and acceptance steps. Compatibility checks compare the
complete production envelope/build/replay outputs with the frozen prior source;
independently authored semantic and rejection cases cover the new entrypoints.
