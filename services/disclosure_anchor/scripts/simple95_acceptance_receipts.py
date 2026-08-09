"""Build and diff deterministic, run-bound Simple95 acceptance receipts.

This module is an offline observer.  It imports the production hashing,
search-materialization and publication-gate contracts, while no production
module imports it.  Run it from the service root with ``python -m`` so the
``scripts`` namespace remains importable::

    PYTHONPATH=src python -m scripts.simple95_acceptance_receipts ...
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, cast

from jsonschema import Draft202012Validator

from disclosure_anchor.adapters.retrieval.tokenizer import (
    RETRIEVAL_RULES_VERSION,
    normalize_search_text,
)
from disclosure_anchor.application.contracts.document_structure import (
    CURRENT_PUBLIC_HIERARCHY_STATUS,
)
from disclosure_anchor.application.contracts.parse_receipt import (
    PARSE_RECEIPT_ARTIFACT_ROLE,
    validate_parse_receipt,
)
from disclosure_anchor.application.contracts.parser_target import (
    CURRENT_PARSER_TARGET_CONTRACT_VERSION,
    ParserTargetIdentity,
)
from disclosure_anchor.application.contracts.publication_safety import (
    PUBLICATION_GATE_VERSION,
    evaluate_publication_gate_v1,
)
from disclosure_anchor.application.contracts.unit_source_projection import (
    UNIT_SEARCH_PLAN_VERSION,
    materialize_search_projection,
)
from disclosure_anchor.application.contracts.visual_semantics import (
    parser_target_sha256,
)
from disclosure_anchor.application.services.unit_builder.builder import UnitDraft
from disclosure_anchor.domain.services.unit_hashing import (
    QUERY_PROJECTION_V2_VERSION,
    canonical_json,
    compute_unit_hashes,
    content_hash,
    content_hash_aggregate,
    query_projection,
    query_projection_hash,
    sha256_prefixed,
    structure_hash,
)
from scripts.audit_unit_corpus import (
    ReceiptAuditObservation,
    audit_document_for_receipt,
    load_manifest,
)


RUN_RECEIPT_VERSION = "simple95-run-receipt.v1"
DIFF_RECEIPT_VERSION = "simple95-diff-receipt.v1"
RUN_SCHEMA_FILENAME = f"{RUN_RECEIPT_VERSION}.json"
DIFF_SCHEMA_FILENAME = f"{DIFF_RECEIPT_VERSION}.json"
REPO_ROOT = Path(__file__).resolve().parents[3]

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_CORE_PROVIDER_ROLES = frozenset(
    {
        "content_list",
        "content_list_v2",
        "middle",
        "model",
        "pdf_structure",
        "source_evidence",
        "visual_semantics",
        PARSE_RECEIPT_ARTIFACT_ROLE,
    }
)
_BINDING_FIELDS = frozenset(
    {
        "code_commit_sha",
        "corpus_manifest_sha256",
        "document_id",
        "provider_document_id",
        "source_pdf_sha256",
        "processing_run_id",
        "parser_target_sha256",
        "parse_receipt_sha256",
        "normalized_ir_sha256",
        "source_evidence_sha256",
        "provider_artifact_hashes",
    }
)
_UNIT_INPUT_FIELDS = frozenset(
    {
        "asset_id",
        "payload_kind",
        "payload",
        "title",
        "heading_path",
        "section_path",
        "detached_from_section",
        "source_order",
        "source_order_phase",
        "native_order_anchor",
        "semantic_key",
        "semantic_keys",
        "quality_status",
        "applicability",
        "artifact_locator",
        "stored_hashes",
    }
)
_STORED_HASH_FIELDS = frozenset(
    {"content_hash", "query_projection_hash", "structure_hash"}
)


class ReceiptContractError(ValueError):
    """A run/diff receipt is incomplete, stale or internally inconsistent."""

    reason_code = "simple95_receipt_invalid"


def canonical_receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    """Return the only accepted on-disk encoding: canonical JSON + one LF."""

    return (canonical_json(dict(receipt)) + "\n").encode("utf-8")


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    return _bytes_sha256(canonical_receipt_bytes(receipt))


def unit_inputs_from_drafts(
    drafts: Sequence[UnitDraft],
    *,
    asset_ids: Sequence[str] | None = None,
    stored_hash_overrides: Sequence[Mapping[str, str] | None] | None = None,
) -> list[dict[str, Any]]:
    """Observe candidate materialization and retain hashes for strict replay.

    ``asset_id`` is accepted only to prove that storage identity never enters
    a receipt.  Every semantic hash is recomputed once more by
    :func:`build_run_receipt`; overrides model a corrupt/stale stored row.
    """

    if asset_ids is not None and len(asset_ids) != len(drafts):
        raise ReceiptContractError("asset_ids cardinality differs from drafts")
    if stored_hash_overrides is not None and len(stored_hash_overrides) != len(
        drafts
    ):
        raise ReceiptContractError(
            "stored_hash_overrides cardinality differs from drafts"
        )
    values: list[dict[str, Any]] = []
    for index, draft in enumerate(drafts, start=1):
        materialized = materialize_search_projection(
            payload_kind=draft.payload_kind,
            payload=draft.payload,
            artifact_locator=draft.artifact_locator,
        )
        hashes = compute_unit_hashes(
            payload_kind=draft.payload_kind,
            payload=draft.payload,
            title=draft.title,
            heading_path=draft.heading_path,
            semantic_key=draft.semantic_key,
            semantic_keys=draft.semantic_keys,
            quality_status=draft.quality_status,
            applicability=draft.applicability,
            order_index=index,
            search_plan=materialized.plan,
        )
        stored = {
            "content_hash": hashes.content_hash,
            "query_projection_hash": hashes.query_projection_hash,
            "structure_hash": hashes.structure_hash,
        }
        if stored_hash_overrides is not None:
            override = stored_hash_overrides[index - 1]
            if override is not None:
                stored.update(dict(override))
        value: dict[str, Any] = {
            "payload_kind": draft.payload_kind,
            "payload": draft.payload,
            "title": draft.title,
            "heading_path": list(draft.heading_path),
            "section_path": list(draft.section_path),
            "detached_from_section": draft.detached_from_section,
            "source_order": draft.source_order,
            "source_order_phase": draft.source_order_phase,
            "native_order_anchor": (
                list(draft.native_order_anchor)
                if draft.native_order_anchor is not None
                else None
            ),
            "semantic_key": draft.semantic_key,
            "semantic_keys": (
                list(draft.semantic_keys) if draft.semantic_keys is not None else None
            ),
            "quality_status": draft.quality_status,
            "applicability": draft.applicability,
            "artifact_locator": draft.artifact_locator,
            "stored_hashes": stored,
        }
        if asset_ids is not None:
            value["asset_id"] = asset_ids[index - 1]
        values.append(value)
    return values


def build_run_receipt(
    *,
    binding: Mapping[str, Any],
    unit_inputs: Sequence[Mapping[str, Any]],
    publication_gate: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    hierarchy_status: str,
    retrieval_rules_version: str = RETRIEVAL_RULES_VERSION,
) -> dict[str, Any]:
    """Build one self-explaining receipt and replay every stored unit hash."""

    normalized_binding = _validated_binding(binding)
    canonical_units = [
        _canonical_unit_leaf(value, order_index=index)
        for index, value in enumerate(unit_inputs, start=1)
    ]
    normalized_findings = [dict(item) for item in findings]
    content_hashes = [
        cast(str, cast(Mapping[str, Any], unit["content"])["content_hash"])
        for unit in canonical_units
    ]
    receipt: dict[str, Any] = {
        "receipt_contract_version": RUN_RECEIPT_VERSION,
        **normalized_binding,
        "publication_gate": dict(publication_gate),
        "unit_count": len(canonical_units),
        "content_multiset_root": content_hash_aggregate(content_hashes),
        "structure_ordered_root": _projection_root(
            "simple95.structure-ordered.v1",
            [unit["structure"] for unit in canonical_units],
        ),
        "query_projection_ordered_root": _projection_root(
            "simple95.query-projection-ordered.v1",
            [unit["query"] for unit in canonical_units],
        ),
        "search_atoms_root": _projection_root(
            "simple95.search-atoms-ordered.v1",
            [
                {
                    "order_index": cast(Mapping[str, Any], unit["structure"])[
                        "order_index"
                    ],
                    "values": unit["search_atoms"],
                }
                for unit in canonical_units
            ],
        ),
        "retrieval_rules_version": retrieval_rules_version,
        "findings_root": _projection_root(
            "simple95.audit-findings-ordered.v1",
            normalized_findings,
        ),
        "hierarchy_status": hierarchy_status,
        "canonical_units": canonical_units,
        "findings": normalized_findings,
    }
    validate_run_receipt(receipt)
    return receipt


def _canonical_unit_leaf(
    value: Mapping[str, Any], *, order_index: int
) -> dict[str, Any]:
    unknown = set(value) - _UNIT_INPUT_FIELDS
    missing = _UNIT_INPUT_FIELDS - {"asset_id"} - set(value)
    if unknown or missing:
        raise ReceiptContractError(
            f"unit input fields are not closed; missing={sorted(missing)!r} "
            f"unknown={sorted(unknown)!r}"
        )
    payload_kind = _required_text(value.get("payload_kind"), "payload_kind")
    payload = _required_dict(value.get("payload"), "payload")
    heading_path = _text_list(value.get("heading_path"), "heading_path")
    section_path = _int_list(value.get("section_path"), "section_path")
    detached = _required_bool(
        value.get("detached_from_section"), "detached_from_section"
    )
    source_order = _required_int(value.get("source_order"), "source_order")
    source_order_phase = _required_int(
        value.get("source_order_phase"), "source_order_phase"
    )
    native_order_anchor = _native_anchor(value.get("native_order_anchor"))
    title = _optional_text(value.get("title"), "title")
    semantic_key = _optional_text(value.get("semantic_key"), "semantic_key")
    semantic_keys = _optional_text_list(
        value.get("semantic_keys"), "semantic_keys"
    )
    quality_status = _required_text(value.get("quality_status"), "quality_status")
    applicability = _optional_text(value.get("applicability"), "applicability")
    artifact_locator = value.get("artifact_locator")
    if artifact_locator is not None and not isinstance(artifact_locator, Mapping):
        raise ReceiptContractError("artifact_locator must be an object or null")
    materialized = materialize_search_projection(
        payload_kind=payload_kind,
        payload=payload,
        artifact_locator=cast(Mapping[str, Any] | None, artifact_locator),
    )
    computed = compute_unit_hashes(
        payload_kind=payload_kind,
        payload=payload,
        title=title,
        heading_path=heading_path,
        semantic_key=semantic_key,
        semantic_keys=semantic_keys,
        quality_status=quality_status,
        applicability=applicability,
        order_index=order_index,
        search_plan=materialized.plan,
    )
    stored = value.get("stored_hashes")
    if not isinstance(stored, Mapping) or set(stored) != _STORED_HASH_FIELDS:
        raise ReceiptContractError("stored unit hash fields are not closed")
    expected_hashes = {
        "content_hash": computed.content_hash,
        "query_projection_hash": computed.query_projection_hash,
        "structure_hash": computed.structure_hash,
    }
    if dict(stored) != expected_hashes:
        differing = sorted(
            key for key, expected in expected_hashes.items() if stored.get(key) != expected
        )
        raise ReceiptContractError(
            f"stored unit hashes do not replay at order {order_index}: {differing!r}"
        )
    projection = query_projection(
        payload_kind=payload_kind,
        payload=payload,
        title=title,
        heading_path=heading_path,
        semantic_key=semantic_key,
        semantic_keys=semantic_keys,
        quality_status=quality_status,
        applicability=applicability,
        search_plan=materialized.plan,
    )
    owner = (
        {"kind": "document_root", "section_path": []}
        if detached or not section_path
        else {"kind": "heading_section", "section_path": section_path}
    )
    return {
        "content": {
            "payload_kind": payload_kind,
            "payload": payload,
            "content_hash": computed.content_hash,
        },
        "structure": {
            "order_index": order_index,
            "content_hash": computed.content_hash,
            "payload_kind": payload_kind,
            "heading_path": heading_path,
            "owner": owner,
            "source_order": source_order,
            "source_order_phase": source_order_phase,
            "native_order_anchor": native_order_anchor,
            "structure_hash": computed.structure_hash,
        },
        "query": {
            "projection": projection,
            "query_projection_hash": computed.query_projection_hash,
        },
        "search_atoms": [
            normalize_search_text(item) for item in materialized.values
        ],
    }


def validate_run_receipt(receipt: Mapping[str, Any]) -> None:
    """Validate closed shape and recompute every semantic root/hash."""

    _validate_schema(receipt, RUN_RECEIPT_SCHEMA, label="run receipt")
    if receipt.get("receipt_contract_version") != RUN_RECEIPT_VERSION:
        raise ReceiptContractError("run receipt version is unsupported")
    _validated_binding({field: receipt[field] for field in _BINDING_FIELDS})
    canonical_units = cast(list[dict[str, Any]], receipt["canonical_units"])
    for index, unit in enumerate(canonical_units, start=1):
        _validate_canonical_unit_leaf(unit, order_index=index)
    if receipt["unit_count"] != len(canonical_units):
        raise ReceiptContractError("unit_count differs from canonical units")
    content_hashes = [
        cast(str, cast(Mapping[str, Any], unit["content"])["content_hash"])
        for unit in canonical_units
    ]
    expected_roots = {
        "content_multiset_root": content_hash_aggregate(content_hashes),
        "structure_ordered_root": _projection_root(
            "simple95.structure-ordered.v1",
            [unit["structure"] for unit in canonical_units],
        ),
        "query_projection_ordered_root": _projection_root(
            "simple95.query-projection-ordered.v1",
            [unit["query"] for unit in canonical_units],
        ),
        "search_atoms_root": _projection_root(
            "simple95.search-atoms-ordered.v1",
            [
                {
                    "order_index": cast(Mapping[str, Any], unit["structure"])[
                        "order_index"
                    ],
                    "values": unit["search_atoms"],
                }
                for unit in canonical_units
            ],
        ),
        "findings_root": _projection_root(
            "simple95.audit-findings-ordered.v1",
            cast(list[dict[str, Any]], receipt["findings"]),
        ),
    }
    differing_roots = sorted(
        field
        for field, expected in expected_roots.items()
        if receipt.get(field) != expected
    )
    if differing_roots:
        raise ReceiptContractError(
            f"run receipt roots do not replay: {differing_roots!r}"
        )
    _validate_publication_gate(
        cast(Mapping[str, Any], receipt["publication_gate"]),
        findings=cast(list[dict[str, Any]], receipt["findings"]),
    )
    if receipt["hierarchy_status"] != str(CURRENT_PUBLIC_HIERARCHY_STATUS):
        raise ReceiptContractError("run receipt hierarchy status is not current")


def _validate_canonical_unit_leaf(
    unit: Mapping[str, Any], *, order_index: int
) -> None:
    content = cast(Mapping[str, Any], unit["content"])
    structure = cast(Mapping[str, Any], unit["structure"])
    query = cast(Mapping[str, Any], unit["query"])
    projection = cast(Mapping[str, Any], query["projection"])
    payload_kind = cast(str, content["payload_kind"])
    payload = cast(dict[str, Any], content["payload"])
    expected_content = content_hash(payload_kind=payload_kind, payload=payload)
    if content["content_hash"] != expected_content:
        raise ReceiptContractError(
            f"content hash does not replay at order {order_index}"
        )
    if structure["order_index"] != order_index:
        raise ReceiptContractError("canonical unit order is not contiguous")
    if structure["payload_kind"] != payload_kind:
        raise ReceiptContractError("structure payload kind differs from content")
    if structure["content_hash"] != content["content_hash"]:
        raise ReceiptContractError(
            "structure content occurrence differs from content leaf"
        )
    expected_structure = structure_hash(
        payload_kind=payload_kind,
        heading_path=cast(list[str], structure["heading_path"]),
        order_index=order_index,
    )
    if structure["structure_hash"] != expected_structure:
        raise ReceiptContractError(
            f"structure hash does not replay at order {order_index}"
        )
    if projection["payload_kind"] != payload_kind:
        raise ReceiptContractError("query payload kind differs from content")
    if projection["heading_path"] != structure["heading_path"]:
        raise ReceiptContractError("query heading path differs from structure")
    expected_projection = query_projection(
        payload_kind=payload_kind,
        payload=payload,
        title=cast(str | None, projection["title"]),
        heading_path=cast(list[str], projection["heading_path"]),
        semantic_key=cast(str | None, projection["semantic_key"]),
        semantic_keys=cast(list[str] | None, projection["semantic_keys"]),
        quality_status=cast(str, projection["quality_status"]),
        applicability=cast(str | None, projection["applicability"]),
        search_plan=cast(Mapping[str, Any], projection["search_plan"]),
    )
    if dict(projection) != expected_projection:
        raise ReceiptContractError(
            f"query projection does not replay at order {order_index}"
        )
    expected_query = query_projection_hash(
        payload_kind=payload_kind,
        payload=payload,
        title=cast(str | None, projection["title"]),
        heading_path=cast(list[str], projection["heading_path"]),
        semantic_key=cast(str | None, projection["semantic_key"]),
        semantic_keys=cast(list[str] | None, projection["semantic_keys"]),
        quality_status=cast(str, projection["quality_status"]),
        applicability=cast(str | None, projection["applicability"]),
        search_plan=cast(Mapping[str, Any], projection["search_plan"]),
    )
    if query["query_projection_hash"] != expected_query:
        raise ReceiptContractError(
            f"query projection hash does not replay at order {order_index}"
        )
    owner = cast(Mapping[str, Any], structure["owner"])
    if owner["kind"] == "document_root" and owner["section_path"]:
        raise ReceiptContractError("document-root owner cannot carry a section path")
    if owner["kind"] == "heading_section" and not owner["section_path"]:
        raise ReceiptContractError("heading-section owner requires a section path")


def diff_run_receipts(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    """Classify one document's receipt delta into the four acceptance families."""

    validate_run_receipt(before)
    validate_run_receipt(after)
    for field in ("document_id", "provider_document_id"):
        if before[field] != after[field]:
            raise ReceiptContractError(
                f"cannot diff receipts for different {field} values"
            )

    before_units = cast(list[dict[str, Any]], before["canonical_units"])
    after_units = cast(list[dict[str, Any]], after["canonical_units"])
    before_content = _projection_counter(
        cast(Mapping[str, Any], unit["content"])["content_hash"]
        for unit in before_units
    )
    after_content = _projection_counter(
        cast(Mapping[str, Any], unit["content"])["content_hash"]
        for unit in after_units
    )
    before_queries = _projection_counter(
        cast(Mapping[str, Any], unit["query"])["projection"]
        for unit in before_units
    )
    after_queries = _projection_counter(
        cast(Mapping[str, Any], unit["query"])["projection"]
        for unit in after_units
    )
    before_plans = _projection_counter(
        cast(Mapping[str, Any], cast(Mapping[str, Any], unit["query"])["projection"])[
            "search_plan"
        ]
        for unit in before_units
    )
    after_plans = _projection_counter(
        cast(Mapping[str, Any], cast(Mapping[str, Any], unit["query"])["projection"])[
            "search_plan"
        ]
        for unit in after_units
    )
    before_atoms = _projection_counter(unit["search_atoms"] for unit in before_units)
    after_atoms = _projection_counter(unit["search_atoms"] for unit in after_units)
    before_findings = _projection_counter(cast(list[Any], before["findings"]))
    after_findings = _projection_counter(cast(list[Any], after["findings"]))

    query_multiset_changes = _counter_distance(before_queries, after_queries)
    content_delta = before["content_multiset_root"] != after[
        "content_multiset_root"
    ]
    before_structure = [
        cast(Mapping[str, Any], unit["structure"]) for unit in before_units
    ]
    after_structure = [
        cast(Mapping[str, Any], unit["structure"]) for unit in after_units
    ]
    before_structure_metadata = [
        _structure_metadata(item) for item in before_structure
    ]
    after_structure_metadata = [
        _structure_metadata(item) for item in after_structure
    ]
    pairing_changes = _retained_content_pairing_changes(
        before_structure, after_structure
    )
    structure_delta = bool(
        before_structure_metadata != after_structure_metadata
        or pairing_changes
        or before["hierarchy_status"] != after["hierarchy_status"]
    )
    query_delta = bool(
        query_multiset_changes
        or before["retrieval_rules_version"] != after["retrieval_rules_version"]
    )
    publication_delta = bool(
        before["publication_gate"] != after["publication_gate"]
        or before["findings_root"] != after["findings_root"]
    )

    structure_counts = {
        field: _positional_field_changes(before_structure, after_structure, field)
        for field in (
            "content_hash",
            "payload_kind",
            "heading_path",
            "owner",
            "order_index",
            "source_order",
            "source_order_phase",
            "native_order_anchor",
            "structure_hash",
        )
    }
    before_gate = cast(Mapping[str, Any], before["publication_gate"])
    after_gate = cast(Mapping[str, Any], after["publication_gate"])
    changed_fields = {
        "content": {
            "unit_count": _changed(before["unit_count"], after["unit_count"]),
            "content_multiset_root": _changed(
                before["content_multiset_root"], after["content_multiset_root"]
            ),
            "content_hash_multiset": _counter_distance(
                before_content, after_content
            ),
        },
        "structure_order_owner": {
            "structure_ordered_root": _changed(
                before["structure_ordered_root"], after["structure_ordered_root"]
            ),
            "hierarchy_status": _changed(
                before["hierarchy_status"], after["hierarchy_status"]
            ),
            "content_occurrence_owner_order_pairing": pairing_changes,
            **structure_counts,
        },
        "query_search_plan": {
            "query_projection_ordered_root": _changed(
                before["query_projection_ordered_root"],
                after["query_projection_ordered_root"],
            ),
            "search_atoms_root": _changed(
                before["search_atoms_root"], after["search_atoms_root"]
            ),
            "retrieval_rules_version": _changed(
                before["retrieval_rules_version"],
                after["retrieval_rules_version"],
            ),
            "query_projection": query_multiset_changes,
            "search_plan": _counter_distance(before_plans, after_plans),
            "search_atoms": _counter_distance(before_atoms, after_atoms),
        },
        "publication_outcome": {
            "publication_gate": _changed(before_gate, after_gate),
            "decision": _changed(before_gate["decision"], after_gate["decision"]),
            "checks": _mapping_field_changes(
                cast(Mapping[str, Any], before_gate["checks"]),
                cast(Mapping[str, Any], after_gate["checks"]),
            ),
            "diagnostics": _mapping_field_changes(
                cast(Mapping[str, Any], before_gate["diagnostics"]),
                cast(Mapping[str, Any], after_gate["diagnostics"]),
            ),
            "findings_root": _changed(
                before["findings_root"], after["findings_root"]
            ),
            "findings": _counter_distance(before_findings, after_findings),
        },
        "run_binding": {
            field: _changed(before[field], after[field])
            for field in sorted(_BINDING_FIELDS - {"document_id", "provider_document_id"})
        },
    }
    root_explanations: dict[str, list[str]] = {}
    unexplained: list[str] = []

    def explain(field: str, *reasons: tuple[bool, str]) -> None:
        if before[field] == after[field]:
            return
        labels = [label for condition, label in reasons if condition]
        if not labels:
            unexplained.append(field)
        root_explanations[field] = labels

    explain("content_multiset_root", (content_delta, "content_delta"))
    explain(
        "structure_ordered_root",
        (content_delta, "content_delta"),
        (structure_delta, "structure_order_owner_delta"),
    )
    explain(
        "query_projection_ordered_root",
        (query_delta, "query_search_plan_delta"),
        (structure_delta, "structure_order_owner_delta"),
    )
    explain(
        "search_atoms_root",
        (content_delta, "content_delta"),
        (structure_delta, "structure_order_owner_delta"),
        (query_delta, "query_search_plan_delta"),
    )
    explain("findings_root", (publication_delta, "publication_outcome_delta"))
    if unexplained:
        raise ReceiptContractError(
            f"receipt delta has no semantic explanation: {unexplained!r}"
        )
    diff: dict[str, Any] = {
        "receipt_contract_version": DIFF_RECEIPT_VERSION,
        "document_id": before["document_id"],
        "provider_document_id": before["provider_document_id"],
        "before_processing_run_id": before["processing_run_id"],
        "after_processing_run_id": after["processing_run_id"],
        "before_receipt_sha256": receipt_sha256(before),
        "after_receipt_sha256": receipt_sha256(after),
        "content_delta": content_delta,
        "structure_order_owner_delta": structure_delta,
        "query_search_plan_delta": query_delta,
        "publication_outcome_delta": publication_delta,
        "changed_fields": changed_fields,
        "root_explanations": root_explanations,
        "unexplained_deltas": unexplained,
    }
    _validate_schema(diff, DIFF_RECEIPT_SCHEMA, label="diff receipt")
    return diff


def validate_diff_receipt(
    receipt: Mapping[str, Any],
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    _validate_schema(receipt, DIFF_RECEIPT_SCHEMA, label="diff receipt")
    expected = diff_run_receipts(before, after)
    if dict(receipt) != expected:
        raise ReceiptContractError("diff receipt does not replay from run receipts")


def build_run_receipt_from_observation(
    observation: ReceiptAuditObservation,
    *,
    code_commit_sha: str,
    corpus_manifest_sha256: str,
) -> dict[str, Any]:
    """Bind an exact corpus audit composition to immutable parser artifacts."""

    binding = _verified_run_binding(
        observation,
        code_commit_sha=code_commit_sha,
        corpus_manifest_sha256=corpus_manifest_sha256,
    )
    hierarchy = observation.report.metrics.get("hierarchy_capability")
    if not isinstance(hierarchy, Mapping):
        raise ReceiptContractError("audit lacks hierarchy capability metrics")
    status = hierarchy.get("status")
    if status != str(CURRENT_PUBLIC_HIERARCHY_STATUS):
        raise ReceiptContractError("audit hierarchy capability is not current")
    return build_run_receipt(
        binding=binding,
        unit_inputs=unit_inputs_from_drafts(observation.drafts),
        publication_gate=evaluate_publication_gate_v1(
            observation.report
        ).as_dict(),
        findings=[item.as_dict() for item in observation.report.findings],
        hierarchy_status=cast(str, status),
    )


def _verified_run_binding(
    observation: ReceiptAuditObservation,
    *,
    code_commit_sha: str,
    corpus_manifest_sha256: str,
) -> dict[str, Any]:
    entry = observation.entry
    frozen = observation.frozen_normalized_ir
    actual_nir_sha = _bytes_sha256(observation.frozen_normalized_ir_bytes)
    if actual_nir_sha != entry.normalized_ir_sha256:
        raise ReceiptContractError("frozen NormalizedIR differs from manifest")
    if frozen.get("contract_version") != "normalized_ir.v4":
        raise ReceiptContractError("run receipt requires normalized_ir.v4")
    if frozen.get("document_id") != entry.document_id:
        raise ReceiptContractError("NormalizedIR names a different document")
    parser_payload = frozen.get("parser")
    if not isinstance(parser_payload, Mapping):
        raise ReceiptContractError("NormalizedIR lacks a parser target")
    target = ParserTargetIdentity.from_payload(parser_payload)
    if target.target_contract_version != CURRENT_PARSER_TARGET_CONTRACT_VERSION:
        raise ReceiptContractError(
            "run receipt requires current parser-target.v2 and parse receipt"
        )
    source_pdf_sha = frozen.get("source_pdf_sha256")
    source_pdf_relpath = frozen.get("source_pdf")
    if not isinstance(source_pdf_sha, str) or _SHA256_RE.fullmatch(source_pdf_sha) is None:
        raise ReceiptContractError("NormalizedIR source PDF hash is invalid")
    if not isinstance(source_pdf_relpath, str) or not source_pdf_relpath:
        raise ReceiptContractError("NormalizedIR source PDF path is invalid")
    source_pdf_raw = _read_data_file(
        observation.data_root, source_pdf_relpath, label="source PDF"
    )
    if _bytes_sha256(source_pdf_raw) != source_pdf_sha:
        raise ReceiptContractError("source PDF bytes do not match NormalizedIR")
    artifacts = frozen.get("parser_artifacts")
    if not isinstance(artifacts, Mapping):
        raise ReceiptContractError("NormalizedIR lacks parser artifacts")
    files = artifacts.get("files")
    if not isinstance(files, Mapping):
        raise ReceiptContractError("parser artifact manifest lacks files")
    missing = sorted(_CORE_PROVIDER_ROLES - set(files))
    if missing:
        raise ReceiptContractError(
            f"parser artifact manifest lacks required roles: {missing!r}"
        )
    verified: dict[str, dict[str, Any]] = {}
    payloads: dict[str, bytes] = {}
    for role in sorted(files):
        if not isinstance(role, str) or _ROLE_RE.fullmatch(role) is None:
            raise ReceiptContractError("parser artifact role is invalid")
        descriptor = files[role]
        if not isinstance(descriptor, Mapping):
            raise ReceiptContractError(f"parser artifact {role} descriptor is invalid")
        availability = descriptor.get("availability")
        if availability == "not_emitted":
            if set(descriptor) != {"availability"}:
                raise ReceiptContractError(
                    f"not-emitted parser artifact {role} is not closed"
                )
            continue
        if set(descriptor) != {"availability", "relpath", "sha256", "size_bytes"}:
            raise ReceiptContractError(f"parser artifact {role} fields are not closed")
        relpath = descriptor.get("relpath")
        digest = descriptor.get("sha256")
        size = descriptor.get("size_bytes")
        if (
            availability != "present"
            or not isinstance(relpath, str)
            or not relpath
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise ReceiptContractError(f"parser artifact {role} descriptor is invalid")
        raw = _read_data_file(
            observation.data_root, relpath, label=f"parser artifact {role}"
        )
        if len(raw) != size or _bytes_sha256(raw) != digest:
            raise ReceiptContractError(f"parser artifact {role} bytes do not replay")
        payloads[role] = raw
        verified[role] = {"sha256": digest, "size_bytes": size}
    if not _CORE_PROVIDER_ROLES.issubset(payloads):
        missing_present = sorted(_CORE_PROVIDER_ROLES - set(payloads))
        raise ReceiptContractError(
            f"required parser artifacts are not present: {missing_present!r}"
        )
    try:
        parse_payload: object = json.loads(payloads[PARSE_RECEIPT_ARTIFACT_ROLE])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptContractError("parse receipt artifact is not JSON") from exc
    validate_parse_receipt(
        parse_payload,
        source_pdf_sha256=source_pdf_sha,
        parser_target_payload=parser_payload,
    )
    return _validated_binding(
        {
            "code_commit_sha": code_commit_sha,
            "corpus_manifest_sha256": corpus_manifest_sha256,
            "document_id": entry.document_id,
            "provider_document_id": entry.provider_document_id,
            "source_pdf_sha256": source_pdf_sha,
            "processing_run_id": entry.processing_run_id,
            "parser_target_sha256": parser_target_sha256(parser_payload),
            "parse_receipt_sha256": verified[PARSE_RECEIPT_ARTIFACT_ROLE][
                "sha256"
            ],
            "normalized_ir_sha256": actual_nir_sha,
            "source_evidence_sha256": verified["source_evidence"]["sha256"],
            "provider_artifact_hashes": verified,
        }
    )


def _validated_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != _BINDING_FIELDS:
        raise ReceiptContractError("run binding fields are not closed")
    if not isinstance(value["code_commit_sha"], str) or _COMMIT_RE.fullmatch(
        cast(str, value["code_commit_sha"])
    ) is None:
        raise ReceiptContractError("code_commit_sha must be a full Git SHA")
    for field in (
        "corpus_manifest_sha256",
        "source_pdf_sha256",
        "parser_target_sha256",
        "parse_receipt_sha256",
        "normalized_ir_sha256",
        "source_evidence_sha256",
    ):
        item = value[field]
        if not isinstance(item, str) or _SHA256_RE.fullmatch(item) is None:
            raise ReceiptContractError(f"{field} is not a SHA-256 digest")
    for field in ("document_id", "provider_document_id", "processing_run_id"):
        _required_text(value[field], field)
    artifacts = value["provider_artifact_hashes"]
    if not isinstance(artifacts, Mapping) or not artifacts:
        raise ReceiptContractError("provider_artifact_hashes must be non-empty")
    normalized_artifacts: dict[str, dict[str, Any]] = {}
    for role in sorted(artifacts):
        descriptor = artifacts[role]
        if (
            not isinstance(role, str)
            or _ROLE_RE.fullmatch(role) is None
            or not isinstance(descriptor, Mapping)
            or set(descriptor) != {"sha256", "size_bytes"}
        ):
            raise ReceiptContractError("provider artifact hash map is not closed")
        digest = descriptor.get("sha256")
        size = descriptor.get("size_bytes")
        if (
            not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise ReceiptContractError("provider artifact hash entry is invalid")
        normalized_artifacts[role] = {"sha256": digest, "size_bytes": size}
    missing_roles = sorted(_CORE_PROVIDER_ROLES - set(normalized_artifacts))
    if missing_roles:
        raise ReceiptContractError(
            f"provider artifact hash map lacks core roles: {missing_roles!r}"
        )
    normalized = dict(value)
    normalized["provider_artifact_hashes"] = normalized_artifacts
    if normalized_artifacts[PARSE_RECEIPT_ARTIFACT_ROLE]["sha256"] != value[
        "parse_receipt_sha256"
    ]:
        raise ReceiptContractError(
            "parse_receipt_sha256 differs from provider artifact hashes"
        )
    if normalized_artifacts["source_evidence"]["sha256"] != value[
        "source_evidence_sha256"
    ]:
        raise ReceiptContractError(
            "source_evidence_sha256 differs from provider artifact hashes"
        )
    return normalized


def _validate_publication_gate(
    gate: Mapping[str, Any], *, findings: Sequence[Mapping[str, Any]]
) -> None:
    if set(gate) != {
        "contract_version",
        "capability",
        "decision",
        "checks",
        "diagnostics",
    }:
        raise ReceiptContractError("publication gate fields are not closed")
    if gate["contract_version"] != PUBLICATION_GATE_VERSION:
        raise ReceiptContractError("publication gate version is unsupported")
    checks = gate["checks"]
    diagnostics = gate["diagnostics"]
    if not isinstance(checks, Mapping) or set(checks) != {
        "metric_shape_closed",
        "audit_ok",
        "error_count_zero",
        "coverage_closed",
        "primary_search_closed",
    }:
        raise ReceiptContractError("publication gate checks are not closed")
    if any(not isinstance(item, bool) for item in checks.values()):
        raise ReceiptContractError("publication gate checks must be boolean")
    if not isinstance(diagnostics, Mapping) or set(diagnostics) != {
        "error_count",
        "coverage_uncovered",
        "primary_search_missing",
    }:
        raise ReceiptContractError("publication gate diagnostics are not closed")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < -1
        for item in diagnostics.values()
    ):
        raise ReceiptContractError("publication gate diagnostics are invalid")
    error_finding_count = sum(
        item.get("severity") == "error" for item in findings
    )
    if diagnostics["error_count"] != error_finding_count:
        raise ReceiptContractError(
            "publication gate error count differs from audit findings"
        )
    expected_checks = {
        "metric_shape_closed": all(item >= 0 for item in diagnostics.values()),
        "audit_ok": error_finding_count == 0,
        "error_count_zero": diagnostics["error_count"] == 0,
        "coverage_closed": diagnostics["coverage_uncovered"] == 0,
        "primary_search_closed": diagnostics["primary_search_missing"] == 0,
    }
    if dict(checks) != expected_checks:
        raise ReceiptContractError(
            "publication gate checks contradict diagnostics/findings"
        )
    expected_decision = "publish" if all(expected_checks.values()) else "block"
    if gate["decision"] != expected_decision:
        raise ReceiptContractError("publication gate decision contradicts checks")


def load_run_receipts(path: Path) -> list[dict[str, Any]]:
    """Load canonical JSONL receipts and reject alternate byte encodings."""

    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise ReceiptContractError("receipt file must end in exactly one LF per row")
    receipts: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(keepends=True), start=1):
        if not line or line == b"\n":
            raise ReceiptContractError("receipt file cannot contain blank rows")
        try:
            decoded: object = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReceiptContractError(
                f"receipt row {line_number} is not JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise ReceiptContractError(
                f"receipt row {line_number} must be an object"
            )
        if canonical_receipt_bytes(decoded) != line:
            raise ReceiptContractError(
                f"receipt row {line_number} is not canonically encoded"
            )
        validate_run_receipt(decoded)
        receipts.append(decoded)
    identities = [
        (item["provider_document_id"], item["document_id"]) for item in receipts
    ]
    if len(identities) != len(set(identities)):
        raise ReceiptContractError("receipt file contains duplicate documents")
    return receipts


def export_receipt_schemas(output_root: Path) -> list[Path]:
    """Export tracked receipt schemas from their executable contract source."""

    output_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, schema in (
        (RUN_SCHEMA_FILENAME, RUN_RECEIPT_SCHEMA),
        (DIFF_SCHEMA_FILENAME, DIFF_RECEIPT_SCHEMA),
    ):
        Draft202012Validator.check_schema(schema)
        path = output_root / filename
        path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written


def _projection_root(domain: str, values: Any) -> str:
    return sha256_prefixed(canonical_json({"domain": domain, "values": values}))


def _projection_counter(values: Iterable[Any]) -> Counter[str]:
    return Counter(canonical_json({"value": value}) for value in values)


def _counter_distance(before: Counter[str], after: Counter[str]) -> int:
    # A replacement is one changed occurrence, not a remove+add pair.
    return max(
        sum((before - after).values()),
        sum((after - before).values()),
    )


def _changed(before: Any, after: Any) -> int:
    return int(before != after)


def _positional_field_changes(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    field: str,
) -> int:
    overlap = min(len(before), len(after))
    return sum(
        before[index].get(field) != after[index].get(field)
        for index in range(overlap)
    ) + abs(len(before) - len(after))


def _structure_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the physical owner/order identity without its content link."""

    return {key: item for key, item in value.items() if key != "content_hash"}


def _retained_content_pairing_changes(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
) -> int:
    """Count retained canonical occurrences that changed physical identity.

    New or removed content belongs to the content family.  For each hash that
    exists on both sides, this measures whether the retained multiplicity can
    stay attached to the same owner/order metadata.  Duplicate occurrences
    remain interchangeable, so no asset identifier or arbitrary pairing is
    introduced.
    """

    def locations_by_content(
        values: Sequence[Mapping[str, Any]],
    ) -> dict[str, Counter[str]]:
        result: dict[str, Counter[str]] = {}
        for value in values:
            content_identity = cast(str, value["content_hash"])
            location_identity = canonical_json(
                {"structure": _structure_metadata(value)}
            )
            result.setdefault(content_identity, Counter())[location_identity] += 1
        return result

    before_locations = locations_by_content(before)
    after_locations = locations_by_content(after)
    changes = 0
    for content_identity in before_locations.keys() & after_locations.keys():
        old = before_locations[content_identity]
        new = after_locations[content_identity]
        retained = min(sum(old.values()), sum(new.values()))
        unchanged = sum((old & new).values())
        changes += retained - unchanged
    return changes


def _mapping_field_changes(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> int:
    return sum(before.get(key) != after.get(key) for key in set(before) | set(after))


def _bytes_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_data_file(data_root: Path, relpath: str, *, label: str) -> bytes:
    relative = Path(relpath)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ReceiptContractError(f"{label} has an unsafe relative path")
    base = (data_root / "data").resolve()
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ReceiptContractError(f"{label} escapes the data root") from exc
    try:
        return candidate.read_bytes()
    except OSError as exc:
        raise ReceiptContractError(f"{label} cannot be read: {relpath}") from exc


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReceiptContractError(f"{field} must be non-empty text")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReceiptContractError(f"{field} must be text or null")
    return value


def _required_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReceiptContractError(f"{field} must be an object")
    return value


def _required_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ReceiptContractError(f"{field} must be boolean")
    return value


def _required_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReceiptContractError(f"{field} must be a non-negative integer")
    return value


def _text_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ReceiptContractError(f"{field} must be an array of text")
    return list(value)


def _optional_text_list(value: Any, field: str) -> list[str] | None:
    if value is None:
        return None
    return _text_list(value, field)


def _int_list(value: Any, field: str) -> list[int]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0
        for item in value
    ):
        raise ReceiptContractError(
            f"{field} must be an array of non-negative integers"
        )
    return list(value)


def _native_anchor(value: Any) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 3:
        raise ReceiptContractError("native_order_anchor must have three integers")
    anchor_order, page_idx, span_start = value
    if (
        isinstance(anchor_order, bool)
        or not isinstance(anchor_order, int)
        or anchor_order < -1
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in (page_idx, span_start)
        )
    ):
        raise ReceiptContractError("native_order_anchor values are invalid")
    return [anchor_order, page_idx, span_start]


def _validate_schema(
    value: Mapping[str, Any], schema: Mapping[str, Any], *, label: str
) -> None:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda item: list(item.absolute_path),
    )
    if not errors:
        return
    first = errors[0]
    location = "/".join(str(item) for item in first.absolute_path) or "$"
    raise ReceiptContractError(f"{label} schema failed at {location}: {first.message}")


def _select_receipt(
    receipts: Sequence[dict[str, Any]], document_id: str | None
) -> dict[str, Any]:
    if document_id is None:
        if len(receipts) != 1:
            raise ReceiptContractError(
                "--document-id is required for a multi-document receipt file"
            )
        return receipts[0]
    selected = [item for item in receipts if item["document_id"] == document_id]
    if len(selected) != 1:
        raise ReceiptContractError(
            f"receipt file does not contain exactly one document {document_id!r}"
        )
    return selected[0]


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
    except FileExistsError as exc:
        raise ReceiptContractError(f"refusing to overwrite existing output: {path}") from exc


def _require_current_clean_commit(code_commit_sha: str) -> None:
    if not isinstance(code_commit_sha, str) or _COMMIT_RE.fullmatch(
        code_commit_sha
    ) is None:
        raise ReceiptContractError("code commit must be a full 40-character SHA")
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReceiptContractError("cannot verify the receipt code checkout") from exc
    if head != code_commit_sha:
        raise ReceiptContractError(
            f"code commit differs from checkout HEAD: {code_commit_sha} != {head}"
        )
    if status:
        raise ReceiptContractError(
            "receipt generation requires a clean tracked/untracked checkout"
        )


def _build_run_command(args: argparse.Namespace) -> int:
    _require_current_clean_commit(args.code_commit_sha)
    entries, manifest_sha256 = load_manifest(args.manifest)
    if args.document_id is not None:
        entries = [entry for entry in entries if entry.document_id == args.document_id]
        if len(entries) != 1:
            raise ReceiptContractError("manifest document selection is not unique")
    receipts: list[dict[str, Any]] = []
    for entry in entries:
        observation = audit_document_for_receipt(
            (entry, str(args.data_root.resolve()), args.source_replay)
        )
        receipts.append(
            build_run_receipt_from_observation(
                observation,
                code_commit_sha=args.code_commit_sha,
                corpus_manifest_sha256=manifest_sha256,
            )
        )
    receipts.sort(
        key=lambda item: (
            cast(str, item["provider_document_id"]),
            cast(str, item["document_id"]),
        )
    )
    _write_new(args.out, b"".join(canonical_receipt_bytes(item) for item in receipts))
    print(f"wrote {len(receipts)} run receipt(s) to {args.out}")
    return 0


def _diff_command(args: argparse.Namespace) -> int:
    before = _select_receipt(load_run_receipts(args.before), args.document_id)
    after = _select_receipt(load_run_receipts(args.after), args.document_id)
    diff = diff_run_receipts(before, after)
    _write_new(args.out, canonical_receipt_bytes(diff))
    print(f"wrote diff receipt to {args.out}")
    return 0


def _export_command(args: argparse.Namespace) -> int:
    written = export_receipt_schemas(args.output_root)
    for path in written:
        print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-run", help="audit and write run receipts")
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--data-root", type=Path, required=True)
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--code-commit-sha", required=True)
    build.add_argument("--document-id")
    build.add_argument("--source-replay", action="store_true")
    build.set_defaults(handler=_build_run_command)
    diff = commands.add_parser("diff", help="write one classified diff receipt")
    diff.add_argument("--before", type=Path, required=True)
    diff.add_argument("--after", type=Path, required=True)
    diff.add_argument("--out", type=Path, required=True)
    diff.add_argument("--document-id")
    diff.set_defaults(handler=_diff_command)
    export = commands.add_parser("export-schemas", help="export JSON Schemas")
    export.add_argument("--output-root", type=Path, required=True)
    export.set_defaults(handler=_export_command)
    args = parser.parse_args(argv)
    try:
        return cast(Any, args.handler)(args)
    except ValueError as exc:
        parser.error(str(exc))
    raise AssertionError("unreachable")


def _fixed_count_object(fields: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(fields),
        "properties": {
            field: {"type": "integer", "minimum": 0} for field in fields
        },
    }


_DIGEST_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": "^sha256:[0-9a-f]{64}$",
}
_NULLABLE_TEXT_SCHEMA: dict[str, Any] = {"type": ["string", "null"]}
_TEXT_ARRAY_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
}
_SEARCH_PLAN_ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["carrier", "target_fields", "transform"],
    "properties": {
        "carrier": {"type": "string", "minLength": 1},
        "target_fields": _TEXT_ARRAY_SCHEMA,
        "transform": {"type": "string", "minLength": 1},
    },
}
_SEARCH_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "atomic_targets", "grouped_atoms"],
    "properties": {
        "version": {"const": UNIT_SEARCH_PLAN_VERSION},
        "atomic_targets": {
            "type": "array",
            "items": _SEARCH_PLAN_ENTRY_SCHEMA,
        },
        "grouped_atoms": {
            "type": "array",
            "items": _SEARCH_PLAN_ENTRY_SCHEMA,
        },
    },
}
_QUERY_PROJECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "payload_kind",
        "title",
        "heading_path",
        "semantic_key",
        "semantic_keys",
        "quality_status",
        "applicability",
        "version",
        "search_plan",
    ],
    "properties": {
        "payload_kind": {"type": "string", "minLength": 1},
        "title": _NULLABLE_TEXT_SCHEMA,
        "heading_path": _TEXT_ARRAY_SCHEMA,
        "semantic_key": _NULLABLE_TEXT_SCHEMA,
        "semantic_keys": {
            "oneOf": [_TEXT_ARRAY_SCHEMA, {"type": "null"}],
        },
        "quality_status": {"type": "string", "minLength": 1},
        "applicability": _NULLABLE_TEXT_SCHEMA,
        "version": {"const": QUERY_PROJECTION_V2_VERSION},
        "search_plan": _SEARCH_PLAN_SCHEMA,
        "mixed_part_annotations": {
            "type": "object",
            "additionalProperties": False,
            "required": ["semantic_type", "parts"],
            "properties": {
                "semantic_type": {},
                "parts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "heading_path": _TEXT_ARRAY_SCHEMA,
                            "local_heading": _NULLABLE_TEXT_SCHEMA,
                            "applicability": _NULLABLE_TEXT_SCHEMA,
                            "quality_status": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}
_FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["code", "severity", "message", "source_ref", "unit_order"],
    "properties": {
        "code": {"type": "string", "minLength": 1},
        "severity": {"type": "string", "minLength": 1},
        "message": {"type": "string"},
        "source_ref": _NULLABLE_TEXT_SCHEMA,
        "unit_order": {
            "type": ["integer", "null"],
            "minimum": 0,
        },
    },
}
_PUBLICATION_GATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "contract_version",
        "capability",
        "decision",
        "checks",
        "diagnostics",
    ],
    "properties": {
        "contract_version": {"const": PUBLICATION_GATE_VERSION},
        "capability": {
            "const": "source-evidence-bounded-content-conservation"
        },
        "decision": {"enum": ["publish", "block"]},
        "checks": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "metric_shape_closed",
                "audit_ok",
                "error_count_zero",
                "coverage_closed",
                "primary_search_closed",
            ],
            "properties": {
                field: {"type": "boolean"}
                for field in (
                    "metric_shape_closed",
                    "audit_ok",
                    "error_count_zero",
                    "coverage_closed",
                    "primary_search_closed",
                )
            },
        },
        "diagnostics": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "error_count",
                "coverage_uncovered",
                "primary_search_missing",
            ],
            "properties": {
                field: {"type": "integer", "minimum": -1}
                for field in (
                    "error_count",
                    "coverage_uncovered",
                    "primary_search_missing",
                )
            },
        },
    },
}
_CANONICAL_UNIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["content", "structure", "query", "search_atoms"],
    "properties": {
        "content": {
            "type": "object",
            "additionalProperties": False,
            "required": ["payload_kind", "payload", "content_hash"],
            "properties": {
                "payload_kind": {"type": "string", "minLength": 1},
                "payload": {"type": "object"},
                "content_hash": _DIGEST_SCHEMA,
            },
        },
        "structure": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "order_index",
                "content_hash",
                "payload_kind",
                "heading_path",
                "owner",
                "source_order",
                "source_order_phase",
                "native_order_anchor",
                "structure_hash",
            ],
            "properties": {
                "order_index": {"type": "integer", "minimum": 1},
                "content_hash": _DIGEST_SCHEMA,
                "payload_kind": {"type": "string", "minLength": 1},
                "heading_path": _TEXT_ARRAY_SCHEMA,
                "owner": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "section_path"],
                    "properties": {
                        "kind": {"enum": ["document_root", "heading_section"]},
                        "section_path": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0},
                        },
                    },
                },
                "source_order": {"type": "integer", "minimum": 0},
                "source_order_phase": {"type": "integer", "minimum": 0},
                "native_order_anchor": {
                    "oneOf": [
                        {
                            "type": "array",
                            "minItems": 3,
                            "maxItems": 3,
                            "prefixItems": [
                                {"type": "integer", "minimum": -1},
                                {"type": "integer", "minimum": 0},
                                {"type": "integer", "minimum": 0},
                            ],
                            "items": False,
                        },
                        {"type": "null"},
                    ]
                },
                "structure_hash": _DIGEST_SCHEMA,
            },
        },
        "query": {
            "type": "object",
            "additionalProperties": False,
            "required": ["projection", "query_projection_hash"],
            "properties": {
                "projection": _QUERY_PROJECTION_SCHEMA,
                "query_projection_hash": _DIGEST_SCHEMA,
            },
        },
        "search_atoms": _TEXT_ARRAY_SCHEMA,
    },
}

_RUN_REQUIRED = [
    "receipt_contract_version",
    *_BINDING_FIELDS,
    "publication_gate",
    "unit_count",
    "content_multiset_root",
    "structure_ordered_root",
    "query_projection_ordered_root",
    "search_atoms_root",
    "retrieval_rules_version",
    "findings_root",
    "hierarchy_status",
    "canonical_units",
    "findings",
]
RUN_RECEIPT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://agent-invest.local/contracts/simple95-run-receipt.v1.json",
    "title": RUN_RECEIPT_VERSION,
    "type": "object",
    "additionalProperties": False,
    "required": sorted(_RUN_REQUIRED),
    "properties": {
        "receipt_contract_version": {"const": RUN_RECEIPT_VERSION},
        "code_commit_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "corpus_manifest_sha256": _DIGEST_SCHEMA,
        "document_id": {"type": "string", "minLength": 1},
        "provider_document_id": {"type": "string", "minLength": 1},
        "source_pdf_sha256": _DIGEST_SCHEMA,
        "processing_run_id": {"type": "string", "minLength": 1},
        "parser_target_sha256": _DIGEST_SCHEMA,
        "parse_receipt_sha256": _DIGEST_SCHEMA,
        "normalized_ir_sha256": _DIGEST_SCHEMA,
        "source_evidence_sha256": _DIGEST_SCHEMA,
        "provider_artifact_hashes": {
            "type": "object",
            "minProperties": 1,
            "required": sorted(_CORE_PROVIDER_ROLES),
            "propertyNames": {"pattern": "^[a-z][a-z0-9_]*$"},
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "required": ["sha256", "size_bytes"],
                "properties": {
                    "sha256": _DIGEST_SCHEMA,
                    "size_bytes": {"type": "integer", "minimum": 0},
                },
            },
        },
        "publication_gate": _PUBLICATION_GATE_SCHEMA,
        "unit_count": {"type": "integer", "minimum": 0},
        "content_multiset_root": _DIGEST_SCHEMA,
        "structure_ordered_root": _DIGEST_SCHEMA,
        "query_projection_ordered_root": _DIGEST_SCHEMA,
        "search_atoms_root": _DIGEST_SCHEMA,
        "retrieval_rules_version": {"type": "string", "minLength": 1},
        "findings_root": _DIGEST_SCHEMA,
        "hierarchy_status": {"const": str(CURRENT_PUBLIC_HIERARCHY_STATUS)},
        "canonical_units": {
            "type": "array",
            "items": _CANONICAL_UNIT_SCHEMA,
        },
        "findings": {"type": "array", "items": _FINDING_SCHEMA},
    },
}

_CONTENT_COUNT_FIELDS = (
    "unit_count",
    "content_multiset_root",
    "content_hash_multiset",
)
_STRUCTURE_COUNT_FIELDS = (
    "structure_ordered_root",
    "hierarchy_status",
    "content_occurrence_owner_order_pairing",
    "content_hash",
    "payload_kind",
    "heading_path",
    "owner",
    "order_index",
    "source_order",
    "source_order_phase",
    "native_order_anchor",
    "structure_hash",
)
_QUERY_COUNT_FIELDS = (
    "query_projection_ordered_root",
    "search_atoms_root",
    "retrieval_rules_version",
    "query_projection",
    "search_plan",
    "search_atoms",
)
_PUBLICATION_COUNT_FIELDS = (
    "publication_gate",
    "decision",
    "checks",
    "diagnostics",
    "findings_root",
    "findings",
)
_RUN_BINDING_COUNT_FIELDS = tuple(
    sorted(_BINDING_FIELDS - {"document_id", "provider_document_id"})
)
_EXPLANATION_LABELS = [
    "content_delta",
    "structure_order_owner_delta",
    "query_search_plan_delta",
    "publication_outcome_delta",
]
DIFF_RECEIPT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://agent-invest.local/contracts/simple95-diff-receipt.v1.json",
    "title": DIFF_RECEIPT_VERSION,
    "type": "object",
    "additionalProperties": False,
    "required": [
        "receipt_contract_version",
        "document_id",
        "provider_document_id",
        "before_processing_run_id",
        "after_processing_run_id",
        "before_receipt_sha256",
        "after_receipt_sha256",
        "content_delta",
        "structure_order_owner_delta",
        "query_search_plan_delta",
        "publication_outcome_delta",
        "changed_fields",
        "root_explanations",
        "unexplained_deltas",
    ],
    "properties": {
        "receipt_contract_version": {"const": DIFF_RECEIPT_VERSION},
        "document_id": {"type": "string", "minLength": 1},
        "provider_document_id": {"type": "string", "minLength": 1},
        "before_processing_run_id": {"type": "string", "minLength": 1},
        "after_processing_run_id": {"type": "string", "minLength": 1},
        "before_receipt_sha256": _DIGEST_SCHEMA,
        "after_receipt_sha256": _DIGEST_SCHEMA,
        "content_delta": {"type": "boolean"},
        "structure_order_owner_delta": {"type": "boolean"},
        "query_search_plan_delta": {"type": "boolean"},
        "publication_outcome_delta": {"type": "boolean"},
        "changed_fields": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "content",
                "structure_order_owner",
                "query_search_plan",
                "publication_outcome",
                "run_binding",
            ],
            "properties": {
                "content": _fixed_count_object(_CONTENT_COUNT_FIELDS),
                "structure_order_owner": _fixed_count_object(
                    _STRUCTURE_COUNT_FIELDS
                ),
                "query_search_plan": _fixed_count_object(_QUERY_COUNT_FIELDS),
                "publication_outcome": _fixed_count_object(
                    _PUBLICATION_COUNT_FIELDS
                ),
                "run_binding": _fixed_count_object(_RUN_BINDING_COUNT_FIELDS),
            },
        },
        "root_explanations": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                field: {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"enum": _EXPLANATION_LABELS},
                }
                for field in (
                    "content_multiset_root",
                    "structure_ordered_root",
                    "query_projection_ordered_root",
                    "search_atoms_root",
                    "findings_root",
                )
            },
        },
        "unexplained_deltas": {
            "type": "array",
            "maxItems": 0,
            "items": {"type": "string"},
        },
    },
}


if __name__ == "__main__":
    raise SystemExit(main())
