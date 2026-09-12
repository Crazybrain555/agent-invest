"""Compare complete source/build claims without granting runtime authority.

Reference builds are freshly derived, never accepted as caller-supplied oracles.
Candidate discrepancies remain observable in their named checks. Actual file
reads and independently owned processes can only be verified by the IO owner.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Literal

from disclosure_anchor.application.contracts.diagnostic_json import bounded_json_bytes
from disclosure_anchor.application.contracts.provider_document import ProviderDocument
from disclosure_anchor.application.contracts.provider_unit import ProviderUnitBuildResult
from disclosure_anchor.application.services.provider_quality import assess_source_build_quality
from disclosure_anchor.application.services.provider_source_semantics import derive_source_semantics
from disclosure_anchor.application.services.provider_unit_builder import (
    build_source_provider_units,
    replay_source_provider_unit_search_binding,
    replay_source_provider_unit_search_binding_source_text,
)
from disclosure_anchor.application.services.source_semantic_build import (
    SourceSemanticBuildCandidate, decode_source_semantic_build,
    encode_source_semantic_build,
)
from disclosure_anchor.application.services.source_semantic_record import (
    DecodedSourceSemanticRecord, UntrustedSourceSemanticCandidate,
    decode_source_semantic_record, parse_source_semantic_candidate,
)


_DATA_CHECKS = (
    "block_conservation", "finding_binding", "heading_occurrence_closure",
    "logical_table_conservation", "page_closure", "reading_order_contiguity",
    "repair_binding", "retrieval_target_binding", "source_identity",
    "table_segment_conservation",
)
_OWNED_CHECKS = ("artifact_closure", "independent_rebuild_match")


@dataclass(frozen=True, slots=True)
class SourceSemanticCheck:
    check_id: str
    outcome: Literal["pass", "fail", "unverified"]


@dataclass(frozen=True, slots=True)
class SourceSemanticComparison:
    checks: tuple[SourceSemanticCheck, ...]
    evidence: bytes
    evidence_sha256: str


class _Differences:
    def __init__(self) -> None:
        self.items: dict[str, list[dict[str, object]]] = {
            name: [] for name in _DATA_CHECKS
        }

    def compare(self, check: str, field: str, candidate: object, reference: object) -> None:
        if candidate == reference:
            return
        context: dict[str, object] = {"field": field}
        if type(candidate) is tuple and type(reference) is tuple:
            context.update(candidate_items=len(candidate), reference_items=len(reference))
            context["first_different_index"] = next(
                (index for index, pair in enumerate(zip(candidate, reference)) if pair[0] != pair[1]),
                min(len(candidate), len(reference)),
            )
        self.items[check].append(context)

    def invalid(self, check: str, field: str, position: tuple[int, int] | None = None) -> None:
        context: dict[str, object] = {"field": field}
        if position is not None:
            context.update(unit_index=position[0], binding_index=position[1])
        self.items[check].append(context)


def compare_source_semantic_candidate(
    candidate_source: bytes, candidate_build: bytes,
    reference_source: bytes, reference_build: bytes,
    *, maximum_record_bytes: int, maximum_evidence_bytes: int,
) -> SourceSemanticComparison:
    """Compare bounded canonical bytes; all malformed records fail visibly."""
    # Validate the evidence ceiling before expensive reconstruction. The complete
    # report must still fit exactly; this never substitutes a truncated report.
    bounded_json_bytes({}, maximum_bytes=maximum_evidence_bytes)
    candidate = parse_source_semantic_candidate(candidate_source, maximum_bytes=maximum_record_bytes)
    proposed = decode_source_semantic_build(candidate_build, maximum_bytes=maximum_record_bytes)
    reference = decode_source_semantic_record(reference_source, maximum_bytes=maximum_record_bytes)
    expected = decode_source_semantic_build(reference_build, maximum_bytes=maximum_record_bytes)
    fresh_build = build_source_provider_units(
        reference.semantics, semantic_record_sha256=reference.record_sha256,
        target_identity=reference.target_identity, level_hints=(), negative_hints=(),
    )
    fresh_bytes = encode_source_semantic_build(
        semantic_record_sha256=reference.record_sha256, build=fresh_build,
        quality_occurrences=assess_source_build_quality(reference.semantics, fresh_build),
        maximum_bytes=maximum_record_bytes,
    )
    if fresh_bytes != reference_build:
        raise ValueError("reference source build differs from fresh complete reconstruction")

    differences = _Differences()
    _compare_source(differences, candidate, reference, proposed, expected)
    _compare_build(differences, proposed, expected)
    _compare_derivations(differences, candidate, reference)
    # Replay both wrappers on every target, even when another named check failed.
    # Reference failures are visible faults in the chosen oracle, not candidate
    # mismatches. Only expected ValueError discrepancies are classified below.
    reference_failure = _replay_failure(reference, expected)
    if reference_failure is not None:
        raise ValueError("fresh reference retrieval replay failed")
    candidate_failure = _replay_failure(reference, proposed)
    if candidate_failure is not None:
        differences.invalid("retrieval_target_binding", "source_or_destination_replay", candidate_failure)

    checks = tuple(
        SourceSemanticCheck(
            name, "unverified" if name in _OWNED_CHECKS
            else "fail" if differences.items[name] else "pass",
        )
        for name in sorted((*_DATA_CHECKS, *_OWNED_CHECKS))
    )
    evidence = bounded_json_bytes({
        "contract_version": "m6.source-semantic-comparison.v1",
        "mode": "service_diagnostic",
        "inputs": {
            name: {"sha256": _sha(raw), "byte_count": len(raw)}
            for name, raw in (
                ("candidate_source", candidate_source), ("candidate_build", candidate_build),
                ("reference_source", reference_source), ("reference_build", reference_build),
            )
        },
        "checks": [{
            "check_id": check.check_id, "outcome": check.outcome,
            "reason": "requires_owned_runtime" if check.outcome == "unverified"
            else "different_claims" if check.outcome == "fail" else "equal_rederived_data",
            "mismatches": differences.items.get(check.check_id, []),
        } for check in checks],
    }, maximum_bytes=maximum_evidence_bytes)
    return SourceSemanticComparison(checks, evidence, _sha(evidence))


def _compare_source(
    differences: _Differences, candidate: UntrustedSourceSemanticCandidate,
    reference: DecodedSourceSemanticRecord, proposed: SourceSemanticBuildCandidate,
    expected: SourceSemanticBuildCandidate,
) -> None:
    doc, ref = candidate.provider_document, reference.provider_document
    differences.compare("source_identity", "source_observation", candidate.source_observation, reference.source_observation)
    differences.compare("source_identity", "target_identity", candidate.target_identity, reference.target_identity)
    differences.compare("source_identity", "provider_identity", _provider_identity(doc), _provider_identity(ref))
    differences.compare("source_identity", "source_record", candidate.record_sha256, reference.record_sha256)
    for name, value in (("candidate", proposed), ("reference", expected)):
        digest = candidate.record_sha256 if name == "candidate" else reference.record_sha256
        differences.compare("source_identity", name + ".semantic_record_sha256", value.semantic_record_sha256, digest)
        differences.compare("source_identity", name + ".build_reference", value.build.provider_document_sha256, digest)
        differences.compare("source_identity", name + ".locator_references", tuple(
            unit.locator.provider_document_sha256 for unit in value.build.units
        ), (digest,) * len(value.build.units))
    differences.compare("page_closure", "physical_page_count", candidate.source_observation.page_count, reference.source_observation.page_count)
    differences.compare("page_closure", "provider_page_count", candidate.source_observation.page_count, len(doc.pages))
    differences.compare("page_closure", "provider_pages", tuple((p.page_index, p.page_size) for p in doc.pages), tuple((p.page_index, p.page_size) for p in ref.pages))
    differences.compare("block_conservation", "original_blocks", doc.blocks, ref.blocks)
    differences.compare("table_segment_conservation", "original_segments", doc.physical_table_segments, ref.physical_table_segments)


def _compare_build(
    differences: _Differences, proposed: SourceSemanticBuildCandidate,
    expected: SourceSemanticBuildCandidate,
) -> None:
    build, reference = proposed.build, expected.build
    differences.compare("page_closure", "unit_pages", tuple((u.unit_index, u.page_no) for u in build.units), tuple((u.unit_index, u.page_no) for u in reference.units))
    differences.compare("block_conservation", "block_ownership", _block_ownership(build), _block_ownership(reference))
    differences.compare("table_segment_conservation", "segment_ownership", _segment_ownership(build), _segment_ownership(reference))
    differences.compare("logical_table_conservation", "ordered_partitions", _table_partitions(build), _table_partitions(reference))
    differences.compare("logical_table_conservation", "unassigned_parts", build.unassigned_table_parts, reference.unassigned_table_parts)
    differences.compare("retrieval_target_binding", "ordered_bindings", tuple(u.locator.search_targets for u in build.units), tuple(u.locator.search_targets for u in reference.units))
    differences.compare("repair_binding", "locator_repairs", tuple(u.locator.source_text_reconciliations for u in build.units), tuple(u.locator.source_text_reconciliations for u in reference.units))
    differences.compare("finding_binding", "locator_findings", tuple(u.locator.source_quality_findings for u in build.units), tuple(u.locator.source_quality_findings for u in reference.units))
    differences.compare("finding_binding", "quality_occurrences", proposed.quality_occurrences, expected.quality_occurrences)
    differences.compare("finding_binding", "unit_quality", tuple(u.quality_status for u in build.units), tuple(u.quality_status for u in reference.units))
    # Complete Unit equality also covers non-retrieval payload, routes, hashes,
    # artifact references and order; contiguous indices alone cannot prove this.
    differences.compare("reading_order_contiguity", "complete_ordered_units", build.units, reference.units)
    differences.compare("reading_order_contiguity", "ordered_unassigned_parts", build.unassigned_table_parts, reference.unassigned_table_parts)
    differences.compare("heading_occurrence_closure", "occurrences_and_titles", tuple((u.title, u.heading_path, u.locator.heading_chain) for u in build.units), tuple((u.title, u.heading_path, u.locator.heading_chain) for u in reference.units))


def _compare_derivations(
    differences: _Differences, candidate: UntrustedSourceSemanticCandidate,
    reference: DecodedSourceSemanticRecord,
) -> None:
    for check, field, actual, expected in (
        ("repair_binding", "native_observations", candidate.native_observations, reference.native_observations),
        ("finding_binding", "native_observations", candidate.native_observations, reference.native_observations),
        ("repair_binding", "source_repairs", candidate.source_text_reconciliations, reference.source_text_reconciliations),
        ("finding_binding", "source_findings", candidate.source_quality_findings, reference.source_quality_findings),
    ):
        differences.compare(check, field, actual, expected)
    try:
        derived = derive_source_semantics(document=candidate.provider_document, observations=candidate.native_observations)
    except ValueError:
        differences.invalid("repair_binding", "native_derivation")
        differences.invalid("finding_binding", "native_derivation")
    else:
        differences.compare("repair_binding", "fresh_candidate_repairs", candidate.source_text_reconciliations, derived.source_text_reconciliations)
        differences.compare("finding_binding", "fresh_candidate_findings", candidate.source_quality_findings, derived.source_quality_findings)


def _block_ownership(build: ProviderUnitBuildResult) -> tuple[object, ...]:
    owners: list[object] = []
    for unit in build.units:
        locator = unit.locator
        owned = {
            source for part in locator.parts for source in part.block_source_indices
        } | set(locator.evidence_only_block_source_indices)
        if locator.heading_chain:
            heading = locator.heading_chain[-1]
            for source in (heading.source_index, *(f.source_index for f in heading.continuation_fragments)):
                if source not in owned:
                    owners.append((unit.unit_index, "leaf_heading", source))
        for part in locator.parts:
            owners.extend((unit.unit_index, "part", part.part_index, source) for source in part.block_source_indices)
        owners.extend((unit.unit_index, "evidence", source) for source in locator.evidence_only_block_source_indices)
    return tuple(owners)


def _segment_ownership(build: ProviderUnitBuildResult) -> tuple[object, ...]:
    owners: list[object] = [
        (unit.unit_index, part.part_index, segment)
        for unit in build.units for part in unit.locator.parts
        for segment in part.physical_table_segment_indices
    ]
    owners.extend(("unassigned", part.part.physical_segment_index) for part in build.unassigned_table_parts if part.part.physical_segment_index is not None)
    return tuple(owners)


def _table_partitions(build: ProviderUnitBuildResult) -> tuple[object, ...]:
    return tuple((
        unit.unit_index,
        tuple(part for part in unit.locator.parts if part.kind == "table"),
        unit.locator.unbound_table_parts,
    ) for unit in build.units)


def _replay_failure(
    reference: DecodedSourceSemanticRecord, candidate: SourceSemanticBuildCandidate,
) -> tuple[int, int] | None:
    first_failure: tuple[int, int] | None = None
    semantics = reference.semantics
    for unit in candidate.build.units:
        for index, binding in enumerate(unit.locator.search_targets):
            for replay in (
                replay_source_provider_unit_search_binding_source_text,
                replay_source_provider_unit_search_binding,
            ):
                try:
                    replay(semantics, unit, binding, semantic_record_sha256=candidate.semantic_record_sha256, target_identity=reference.target_identity)
                except ValueError:
                    if first_failure is None:
                        first_failure = (unit.unit_index, index)
    return first_failure


def _provider_identity(document: ProviderDocument) -> tuple[object, ...]:
    return (
        document.source_pdf_sha256, document.parser_version, document.backend,
        document.effort, document.ocr_enabled, document.bundle_sha256, document.artifacts,
    )


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()
