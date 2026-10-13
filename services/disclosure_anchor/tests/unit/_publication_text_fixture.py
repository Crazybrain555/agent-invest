"""Shared fixtures for the publication text backstop and cross-version sealed requests.

* ``single_unit_request`` mirrors ``tests.unit.test_atomic_document_publication_v4._request`` (an unheaded
  text Unit over the same materialized evidence) with the payload, keys, locator and projection as
  parameters, so a test can place exact decoded values in the fields transaction P persists;
* ``gate_replaced`` swaps every loaded binding of the publication text check, however it was imported;
* ``as_v23`` reproduces the E1 (``provider_unit.v23``) publication path inside the current code by exactly
  the release's three differences: no publication text check, provider text published as-is (no U+0000
  substitution record, so the builder keeps the NUL), and the ``provider_unit.v23`` request stamp.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
import dataclasses
import hashlib
import json
import sys
from types import ModuleType
from typing import Any
from unittest import mock

from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
    PublicationAttemptIdentityV4,
    previous_active_units_sha256_v4,
    seal_atomic_publication_request_v4,
    seal_pre_id_unit_publication_v4,
    seal_upstream_publication_evidence_v4,
)
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderUnitLocator,
    provider_unit_locator_to_payload,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_ROUTE_RECEIPT_V3,
    SEMANTIC_ROUTE_RECEIPT_VERSION,
    SEMANTIC_ROUTE_RECEIPTS_V3_FILENAME,
    SemanticRouteReceiptRowV3,
    semantic_adjudication_terminal_v1,
    semantic_route_receipts_file_bytes_v3,
)
from disclosure_anchor.domain.services.unit_hashing import (
    compute_unit_hashes,
    content_hash_aggregate,
    structure_hash_aggregate,
)
from tests.unit._semantic_routes import _fallback_receipt
from disclosure_anchor.application.contracts.provider_source_semantics import effective_provider_document
from disclosure_anchor.application.services import atomic_publication_request_builder_v4 as request_builder_module
from disclosure_anchor.application.services import provider_unit_builder as unit_builder_module
from disclosure_anchor.application.services import publication_text_representability_v4 as gate_module
from tests.unit.test_atomic_document_publication_v4 import _publication_materialized_evidence


def canonical_json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def single_unit_request(
    payload: dict[str, Any],
    *,
    section_keys: tuple[str, ...] = ("section",),
    semantic_keys: tuple[str, ...] | None = None,
    locator_edit: Any = None,
    projection_edit: Any = None,
) -> AtomicPublicationRequestV4:
    """A sealed one-Unit request (unheaded text Unit) whose payload is exactly ``payload``."""

    reservation, checkpoint, intent, receipt, manifest, provider_envelope = _publication_materialized_evidence()
    upstream = seal_upstream_publication_evidence_v4(
        reservation=reservation, checkpoint=checkpoint, intent=intent, receipt=receipt, manifest=manifest,
        provider_envelope=provider_envelope,
    )
    target = intent.provider_envelope_context.parser_target_identity
    hashes = compute_unit_hashes(
        payload_kind="text", payload=payload, title=None, heading_path=[],
        semantic_keys=None if semantic_keys is None else list(semantic_keys),
        section_keys=list(section_keys), quality_status="ok", applicability="applicable", order_index=1,
    )
    locator = provider_unit_locator_to_payload(ProviderUnitLocator(
        provider_document_sha256=upstream.provider_document_sha256, unit_index=0, heading_chain=(), parts=(),
        evidence_only_block_source_indices=(), unbound_table_parts=(), evidence_artifacts=(), search_targets=(),
    ))
    if locator_edit is not None:
        locator_edit(locator)
    locator_json = canonical_json_text(locator)
    unit = seal_pre_id_unit_publication_v4(
        document_id="doc-1", processing_run_id="run-1", provider_document_id=upstream.provider_document_id,
        unit_index=1, payload_kind="text", heading_path=(), title=None, semantic_keys=semantic_keys,
        section_keys=section_keys, canonical_payload_json=canonical_json_text(payload),
        content_hash=hashes.content_hash, structure_hash=hashes.structure_hash, quality_status="ok",
        applicability="applicable", page_no=1, page_numbers=(1,),
        query_projection_hash=hashes.query_projection_hash, canonical_artifact_locator_json=locator_json,
        provider_locator_sha256="sha256:" + hashlib.sha256(locator_json.encode()).hexdigest(),
    )
    route = SemanticRouteReceiptRowV3(
        processing_run_id="run-1", unit_order_index=1, provider_locator_sha256=unit.provider_locator_sha256,
        routed_draft_sha256=unit.routed_draft_sha256,
        receipt=dataclasses.replace(_fallback_receipt(1), contract_version=SEMANTIC_ROUTE_RECEIPT_VERSION,
                                    semantic_keys=(), evidence=()),
    )
    semantic_file = semantic_route_receipts_file_bytes_v3((route,))
    terminal = semantic_adjudication_terminal_v1((route.receipt,))
    run_root = f"derived/document_unit_snapshots/cninfo/000001/{upstream.provider_document_id}/run-1"
    projection: dict[str, Any] = {
        "artifact_owner_processing_run_id": "run-1",
        "builder_rules_version": "provider-unit-builder.v1",
        "content_hash_aggregate": content_hash_aggregate([unit.content_hash]),
        "contract_version": "processing-run-publication.v4",
        "document_id": "doc-1",
        "document_units_relpath": f"{run_root}/document_units.v1.jsonl",
        "is_active": True,
        "normalized_ir_relpath": None,
        "parser_artifact_relpath": upstream.parser_artifact_root_relpath,
        "parser_backend": target.backend,
        "parser_language": target.language,
        "parser_method": target.method,
        "parser_name": target.name,
        "parser_target_identity": target.to_payload(),
        "parser_version": target.package_version,
        "processing_run_id": "run-1",
        "provider_document_id": upstream.provider_document_id,
        "provider_document_relpath": (
            f"derived/provider_documents/cninfo/000001/{upstream.provider_document_id}/run-1/provider_document.v1.json"
        ),
        "provider_document_sha256": upstream.provider_document_sha256,
        "run_kind": "parse",
        "semantic_adjudication_status": terminal.status,
        "semantic_adjudication_summary": terminal.summary,
        "semantic_degraded_unit_count": terminal.degraded_unit_count,
        "semantic_failover_group_count": terminal.failover_group_count,
        "semantic_route_receipts_contract_version": SEMANTIC_ROUTE_RECEIPT_V3,
        "semantic_route_receipts_relpath": f"{run_root}/{SEMANTIC_ROUTE_RECEIPTS_V3_FILENAME}",
        "semantic_route_receipts_sha256": "sha256:" + hashlib.sha256(semantic_file).hexdigest(),
        "source_pdf_relpath": upstream.source_pdf_relpath,
        "source_pdf_sha256": upstream.source_pdf_sha256,
        "status": "succeeded",
        "structure_hash_aggregate": structure_hash_aggregate([unit.structure_hash]),
        "unit_build_attempt_count": 1,
        "unit_build_status": "succeeded",
        "unit_count": 1,
    }
    if projection_edit is not None:
        projection_edit(projection)
    projection_json = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    return seal_atomic_publication_request_v4(
        identity=PublicationAttemptIdentityV4(
            attempt_id="attempt-1", document_id="doc-1", processing_run_id="run-1",
            provider_document_id=upstream.provider_document_id, attempt_generation=1, fence_identity="fence-1",
            expected_attempt_state="local_materialized", expected_lifecycle_version=checkpoint.lifecycle_version,
            expected_checkpoint_sha256=checkpoint.sha256,
            expected_local_materialization_receipt_sha256=receipt.sha256,
            expected_previous_processing_run_id=None,
        ),
        upstream_evidence=upstream,
        source_page_count=2,
        processing_run_projection_json=projection_json,
        processing_run_projection_sha256="sha256:" + hashlib.sha256(projection_json.encode()).hexdigest(),
        semantic_route_receipts_contract_version=SEMANTIC_ROUTE_RECEIPT_V3,
        semantic_route_receipts=(route,),
        expected_unit_build_status_before="not_started",
        expected_unit_build_attempt_count_before=0,
        previous_active_units=(),
        previous_active_units_sha256=previous_active_units_sha256_v4(()),
        units=(unit,),
    )


V23_BUILDER_VERSION = "provider_unit.v23"


def gate_bindings() -> list[tuple[ModuleType, str]]:
    """Every loaded product binding of the publication text check, however it was imported."""

    original = gate_module.validate_publication_text_representability
    found = []
    for name, module in list(sys.modules.items()):
        if (name == "disclosure_anchor" or name.startswith("disclosure_anchor.")) and module is not None:
            for attribute, value in list(vars(module).items()):
                if value is original:
                    found.append((module, attribute))
    return found


@contextmanager
def gate_replaced(replacement: Any) -> Iterator[None]:
    bindings = gate_bindings()
    if not bindings:
        raise AssertionError("the publication text check has no loaded binding to replace")
    with ExitStack() as stack:
        for module, attribute in bindings:
            stack.enter_context(mock.patch.object(module, attribute, replacement))
        yield


@contextmanager
def without_gate() -> Iterator[None]:
    with gate_replaced(lambda *_args, **_kwargs: None):
        yield


class V23Simulation:
    """Counts how often the v23 build view was actually used (a simulation that never ran proves nothing)."""

    def __init__(self) -> None:
        self.builds = 0


@contextmanager
def as_v23() -> Iterator[V23Simulation]:
    """The E1 worker's publication path, inside the current process.

    v23 built Units from the provider text with native repairs only (a U+0000 stayed in the Unit), had no
    text check before readiness and stamped ``provider_unit.v23``. Everything else (outline, tables,
    retrieval, locators v9, quality, request contract, readiness, P) is byte-identical by the accepted
    decision, so only these three bindings change.
    """

    real_input = unit_builder_module._admitted_build_input
    simulation = V23Simulation()

    def v23_build_input(admitted: Any) -> Any:
        simulation.builds += 1
        value = real_input(admitted)
        changes: dict[str, Any] = {
            "document": effective_provider_document(
                admitted.provider_document, admitted.source_text_reconciliations,
            ),
        }
        if hasattr(value, "text_substitutions"):
            changes["text_substitutions"] = ()
        return dataclasses.replace(value, **changes)

    with ExitStack() as stack:
        stack.enter_context(without_gate())
        stack.enter_context(mock.patch.object(unit_builder_module, "_admitted_build_input", v23_build_input))
        stack.enter_context(mock.patch.object(
            request_builder_module, "PROVIDER_UNIT_BUILDER_VERSION", V23_BUILDER_VERSION))
        yield simulation


__all__ = [
    "V23Simulation",
    "V23_BUILDER_VERSION",
    "as_v23",
    "canonical_json_text",
    "gate_bindings",
    "gate_replaced",
    "single_unit_request",
    "without_gate",
]
