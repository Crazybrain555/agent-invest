from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from disclosure_anchor.application.contracts.provider_document_admission import (
    AdmittedProviderDocument,
)
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderUnitBuildResult,
    ProviderUnitDraft,
    ProviderUnitLocator,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_ROUTE_RECEIPT_V3,
    SEMANTIC_ROUTE_RECEIPT_VERSION,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4Authority,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
)
from disclosure_anchor.application.services.atomic_publication_request_builder_v4 import (
    AtomicPublicationRequestBuilderV4Error,
    ProductionAtomicPublicationRequestBuilderV4,
)
from disclosure_anchor.application.services.semantic_router import (
    SemanticRouteBatchResult,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.services.unit_hashing import compute_unit_hashes
from tests.integration._remote_parse_v4_factory import build_v4_authority_fixture
from tests.unit._semantic_routes import _fallback_receipt


class AtomicPublicationRequestBuilderV4Tests(unittest.TestCase):
    def test_builds_from_exact_materialization_and_untouched_ingress(self) -> None:
        harness = _Harness()
        guard = _Guard()

        with patch(
            "disclosure_anchor.application.services.atomic_publication_request_builder_v4.build_provider_units",
            return_value=ProviderUnitBuildResult(
                provider_document_sha256=(
                    harness.materialized.receipt.provider_envelope_sha256
                ),
                units=(_draft(harness),),
                unassigned_table_parts=(),
            ),
        ):
            request = harness.builder.build(
                checkpoint=harness.fixture.local_materialized,
                materialized=harness.materialized,
                stage_guard=guard,  # type: ignore[arg-type]
            )

        self.assertEqual(request.identity.attempt_id, harness.fixture.attempt_id)
        self.assertEqual(request.identity.expected_previous_processing_run_id, None)
        self.assertEqual(
            request.upstream_evidence.provider_envelope_sha256,
            (harness.materialized.receipt.provider_envelope_sha256),
        )
        self.assertEqual(len(request.units), 1)
        self.assertEqual(request.units[0].page_numbers, (1,))
        self.assertEqual(
            request.semantic_route_receipts_contract_version, SEMANTIC_ROUTE_RECEIPT_V3
        )
        projection = json.loads(request.processing_run_projection_json)
        self.assertEqual(projection["status"], "succeeded")
        self.assertEqual(projection["unit_count"], 1)
        self.assertEqual(
            projection["parser_artifact_relpath"],
            (
                harness.fixture.materialization_intent.provider_envelope_context.parser_artifact_root_relpath
            ),
        )
        self.assertGreaterEqual(guard.calls, 5)
        self.assertEqual(harness.admission.calls[0]["document"], harness.document)
        self.assertEqual(harness.admission.calls[0]["expected_source_byte_count"], 100)

    def test_binds_the_complete_previous_inventory(self) -> None:
        previous = _previous_unit()
        harness = _Harness(previous_unit=previous)
        with patch(
            "disclosure_anchor.application.services.atomic_publication_request_builder_v4.build_provider_units",
            return_value=ProviderUnitBuildResult(
                provider_document_sha256=harness.materialized.receipt.provider_envelope_sha256,
                units=(_draft(harness),),
                unassigned_table_parts=(),
            ),
        ):
            request = harness.builder.build(
                checkpoint=harness.fixture.local_materialized,
                materialized=harness.materialized,
                stage_guard=_Guard(),  # type: ignore[arg-type]
            )

        self.assertEqual(
            request.identity.expected_previous_processing_run_id, "run-previous"
        )
        self.assertEqual(len(request.previous_active_units), 1)
        self.assertEqual(request.previous_active_units[0].asset_id, previous.asset_id)

    def test_fails_before_admission_when_current_inventory_drifted(self) -> None:
        previous = replace(
            _previous_unit(),
            query_projection_hash="sha256:" + "0" * 64,
        )
        harness = _Harness(previous_unit=previous)

        with self.assertRaisesRegex(ValueError, "projection drifted"):
            harness.builder.build(
                checkpoint=harness.fixture.local_materialized,
                materialized=harness.materialized,
                stage_guard=_Guard(),  # type: ignore[arg-type]
            )
        self.assertEqual(harness.admission.calls, [])

    def test_rejects_candidate_that_is_not_untouched_ingress(self) -> None:
        harness = _Harness(candidate_status="succeeded")

        with self.assertRaisesRegex(
            AtomicPublicationRequestBuilderV4Error,
            "untouched V4 ingress",
        ):
            harness.builder.build(
                checkpoint=harness.fixture.local_materialized,
                materialized=harness.materialized,
                stage_guard=_Guard(),  # type: ignore[arg-type]
            )
        self.assertEqual(harness.admission.calls, [])


class _Harness:
    def __init__(
        self,
        *,
        previous_unit: e.DocumentUnit | None = None,
        candidate_status: str = "running",
    ) -> None:
        self.fixture = build_v4_authority_fixture()
        if previous_unit is not None:
            previous_unit = replace(
                previous_unit,
                document_id=self.fixture.document_id,
            )
        self.materialized = MaterializedProviderDocumentV4(
            receipt=self.fixture.local_materialization_receipt,
            intent=self.fixture.materialization_intent,
            provider_envelope=self.fixture.provider_envelope,
            manifest=self.fixture.materialization_manifest,
        )
        context = self.fixture.materialization_intent.provider_envelope_context
        self.document = e.Document(
            document_id=self.fixture.document_id,
            status="downloaded",
            security_id="sec-1",
            provider=context.provider,
            provider_document_id=context.provider_document_id,
            raw_file_relpath=context.source_pdf_relpath,
            raw_file_hash=self.fixture.source_pdf_sha256,
            current_processing_run_id=(
                None if previous_unit is None else "run-previous"
            ),
        )
        target = context.parser_target_identity
        candidate = e.ProcessingRun(
            processing_run_id=self.fixture.processing_run_id,
            document_id=self.fixture.document_id,
            artifact_owner_processing_run_id=self.fixture.processing_run_id,
            run_kind="parse",
            status=candidate_status,
            parser_name=target.name,
            parser_version=target.package_version,
            parser_backend=target.backend,
            parser_method=target.method,
            parser_language=target.language,
            parser_target_identity=target.to_payload(),
            input_raw_file_hash=self.fixture.source_pdf_sha256,
            parser_artifact_relpath=context.parser_artifact_root_relpath,
            provider_document_relpath=_Paths.provider_document_relpath(
                provider=context.provider,
                security_code="000001",
                provider_document_id=context.provider_document_id,
                artifact_owner_processing_run_id=self.fixture.processing_run_id,
            ).as_posix(),
        )
        runs = {candidate.processing_run_id: candidate}
        if previous_unit is not None:
            runs["run-previous"] = e.ProcessingRun(
                processing_run_id="run-previous",
                document_id=self.fixture.document_id,
                artifact_owner_processing_run_id="run-previous",
                run_kind="parse",
                status="succeeded",
                is_active=True,
            )
        authority = RemoteParseV4Authority(
            attempt_id=self.fixture.attempt_id,
            processing_run_id=self.fixture.processing_run_id,
            document_id=self.fixture.document_id,
            attempt_generation=self.fixture.local_materialized.attempt_generation,
            fence_identity=self.fixture.fence_identity,
            source_pdf_sha256=self.fixture.source_pdf_sha256,
            parser_target_sha256=self.fixture.parser_target_sha256,
            request_sha256=self.fixture.request_sha256,
            runtime_epoch_sha256=self.fixture.runtime_epoch_sha256,
            client_submit_key=self.fixture.client_submit_key,
            state="local_materialized",
            is_current=True,
            lifecycle_version=self.fixture.local_materialized.lifecycle_version,
            checkpoint_sha256=self.fixture.local_materialized.sha256,
            claim_generation=0,
            claim_owner_identity=None,
            claim_lease_until=None,
            checkpoint_history=self.fixture.local_materialized_checkpoints,
            reservation=self.fixture.reservation,
            evidence=tuple(
                encode_remote_parse_evidence_v4(item)
                for item in self.fixture.local_materialized_evidence
            ),
            publication_winner=None,
            secret_history=(),
            source_supersession_link=None,
            staged_by_link=None,
            database_lease=None,
        )
        self.admission = _Admission(self.materialized)
        self.builder = ProductionAtomicPublicationRequestBuilderV4(
            path_builder=_Paths(),  # type: ignore[arg-type]
            uow_factory=lambda: _Uow(
                authority=authority,
                document=self.document,
                runs=runs,
                previous_units=(() if previous_unit is None else (previous_unit,)),
            ),  # type: ignore[arg-type]
            admission=self.admission,  # type: ignore[arg-type]
            semantic_router=_Router(),  # type: ignore[arg-type]
        )


class _Admission:
    def __init__(self, materialized: MaterializedProviderDocumentV4) -> None:
        self.materialized = materialized
        self.calls: list[dict[str, object]] = []

    def admit_materialized(self, **kwargs: object) -> AdmittedProviderDocument:
        self.calls.append(kwargs)
        return AdmittedProviderDocument(
            provider_document_relpath=Path("derived/provider_documents/record.json"),
            provider_document_sha256=self.materialized.receipt.provider_envelope_sha256,
            envelope=self.materialized.provider_envelope,
        )


class _Router:
    def route(
        self, *, drafts: tuple[ProviderUnitDraft, ...], **_kwargs: object
    ) -> SemanticRouteBatchResult:
        receipt = replace(
            _fallback_receipt(0),
            contract_version=SEMANTIC_ROUTE_RECEIPT_VERSION,
            semantic_keys=(),
            evidence=(),
        )
        return SemanticRouteBatchResult(units=drafts, receipts=(receipt,))


class _Guard:
    def __init__(self) -> None:
        self.calls = 0

    def checkpoint(self) -> None:
        self.calls += 1


class _Repo:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def get(self, identity: str) -> object | None:
        return self.values.get(identity)


class _RemoteRepo:
    def __init__(self, authority: RemoteParseV4Authority) -> None:
        self.authority = authority

    def load(self, attempt_id: str) -> RemoteParseV4Authority:
        if attempt_id != self.authority.attempt_id:
            raise KeyError(attempt_id)
        return self.authority


class _Units:
    def __init__(self, values: tuple[e.DocumentUnit, ...]) -> None:
        self.values = values

    def list_by_document_active(self, document_id: str) -> list[e.DocumentUnit]:
        return list(self.values)


class _Uow:
    def __init__(
        self,
        *,
        authority: RemoteParseV4Authority,
        document: e.Document,
        runs: dict[str, e.ProcessingRun],
        previous_units: tuple[e.DocumentUnit, ...],
    ) -> None:
        self.remote_parse_v4 = _RemoteRepo(authority)
        self.documents = _Repo({document.document_id: document})
        self.processing_runs = _Repo(runs)
        self.securities = _Repo(
            {
                "sec-1": e.Security(
                    security_id="sec-1",
                    company_id="company-1",
                    security_code="000001",
                    exchange="SZSE",
                )
            }
        )
        self.document_units = _Units(previous_units)

    def __enter__(self) -> _Uow:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Paths:
    @staticmethod
    def provider_document_relpath(**kwargs: str) -> Path:
        return _unit_root(kwargs) / "provider_document.v1.json"

    @staticmethod
    def document_units_snapshot_relpath(**kwargs: str) -> Path:
        return _snapshot_root(kwargs) / "document_units.v1.jsonl"

    @staticmethod
    def semantic_route_receipts_v3_relpath(**kwargs: str) -> Path:
        return _snapshot_root(kwargs) / "semantic_route_receipts.v3.jsonl"


def _unit_root(values: dict[str, str]) -> Path:
    return (
        Path("derived/provider_documents")
        / values["provider"]
        / values["security_code"]
        / values["provider_document_id"]
        / values["artifact_owner_processing_run_id"]
    )


def _snapshot_root(values: dict[str, str]) -> Path:
    return (
        Path("derived/document_unit_snapshots")
        / values["provider"]
        / values["security_code"]
        / values["provider_document_id"]
        / values["processing_run_id"]
    )


def _draft(harness: _Harness) -> ProviderUnitDraft:
    payload = {"text": "publication unit"}
    hashes = compute_unit_hashes(
        payload_kind="text",
        payload=payload,
        title=None,
        heading_path=[],
        semantic_keys=None,
        section_keys=["section"],
        quality_status="ok",
        applicability="applicable",
        order_index=1,
    )
    locator = ProviderUnitLocator(
        provider_document_sha256=harness.materialized.receipt.provider_envelope_sha256,
        unit_index=0,
        heading_chain=(),
        parts=(),
        evidence_only_block_source_indices=(),
        unbound_table_parts=(),
        evidence_artifacts=(),
        search_targets=(),
    )
    return ProviderUnitDraft(
        unit_index=0,
        payload_kind="text",
        payload=payload,
        title=None,
        heading_path=(),
        section_keys=("section",),
        semantic_keys=None,
        applicability="applicable",
        quality_status="ok",
        page_no=1,
        locator=locator,
        content_hash=hashes.content_hash,
        query_projection_hash=hashes.query_projection_hash,
        structure_hash=hashes.structure_hash,
    )


def _previous_unit() -> e.DocumentUnit:
    payload = {"text": "previous"}
    hashes = compute_unit_hashes(
        payload_kind="text",
        payload=payload,
        title=None,
        heading_path=[],
        semantic_keys=None,
        section_keys=["section"],
        quality_status="ok",
        applicability="applicable",
        order_index=1,
    )
    return e.DocumentUnit(
        asset_id="du_01K00000000000000000000000",
        document_id="placeholder",
        processing_run_id="run-previous",
        provider_document_id="1225087169",
        payload_kind="text",
        order_index=1,
        payload=payload,
        content_hash=hashes.content_hash,
        structure_hash=hashes.structure_hash,
        quality_status="ok",
        applicability="applicable",
        page_no=1,
        query_projection_hash=hashes.query_projection_hash,
        section_keys=["section"],
    )


if __name__ == "__main__":
    unittest.main()
