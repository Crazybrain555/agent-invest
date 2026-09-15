"""Coherent synthetic files and typed publication, with no live DB/PDF/model.

Only fixture construction intercepts old fixture constructors to replace their
placeholder artifact hashes and empty pages. Qualification core is never mocked.
"""
from dataclasses import fields, replace
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from disclosure_anchor.application.contracts.provider_document import ProviderDocument, ProviderArtifact, ProviderPage
from disclosure_anchor.application.contracts.provider_document_admission import SourcePdfObservation
from disclosure_anchor.application.contracts.provider_document_envelope import provider_document_envelope_to_bytes
from disclosure_anchor.application.contracts.semantic_routes import (
    SemanticRouteDefinition, SemanticRouteTaxonomy, SemanticRouteReceiptRowV3,
    semantic_route_receipts_file_bytes_v3, semantic_adjudication_terminal_v1,
)
from disclosure_anchor.application.contracts.atomic_document_publication_v4 import seal_atomic_publication_request_v4
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import document_unit_snapshot_file_bytes_v1
from disclosure_anchor.application.contracts.m6_run_events import M6AttemptAdmitted, M6PublicationCommitted
from disclosure_anchor.application.services.provider_document_admission import ProviderDocumentAdmission
from disclosure_anchor.application.services.provider_unit_builder import build_provider_units
from disclosure_anchor.application.services.semantic_router import SemanticRouter, semantic_document_context
from disclosure_anchor.application.services.atomic_publication_request_builder_v4 import ProductionAtomicPublicationRequestBuilderV4
from disclosure_anchor.adapters.storage.atomic_publication_artifact_readiness_v4 import FilesystemAtomicPublicationArtifactReadinessV4
from disclosure_anchor.adapters.storage.immutable_artifact_store import ImmutableArtifactStore
from disclosure_anchor.adapters.storage.published_parser_output_verifier_v4 import PublishedParserOutputVerifierV4
from disclosure_anchor.adapters.runtime.m6_public_consumer_verifier import _expected_rows, _PUBLIC_FIELDS
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.services.unit_hashing import content_hash_aggregate, structure_hash_aggregate
from tests.unit import test_atomic_document_publication_v4 as atomic
from tests.unit.test_atomic_publication_artifact_readiness_adapter_v4 import _Paths
from tests._provider_source_semantics_fixture import block, observation


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class Paths(_Paths):
    def provider_document_relpath(self, **values):
        return Path("derived/provider_documents") / values["provider"] / values["security_code"] / values["provider_document_id"] / values["artifact_owner_processing_run_id"] / "provider_document.v1.json"


class Source:
    """External source observation double; real admit verifies its bindings."""
    def __init__(self, fixture):
        self.fixture = fixture
        self.rebuilt = fixture.envelope.provider_document
        self.calls = []

    def read_provider_document_record(self, relpath):
        self.calls.append("record")
        return self.fixture.paths.data_path(relpath).read_bytes()

    def observe_source_pdf(self, relpath):
        self.calls.append("raw")
        assert str(relpath) == self.fixture.envelope.source_pdf_relpath
        return SourcePdfObservation(self.fixture.envelope.input_raw_file_hash, 100, 2)

    def rebuild_provider_document(self, relpath, *, source_pdf_sha256):
        self.calls.append("rebuild")
        assert str(relpath) == self.fixture.envelope.parser_artifact_root_relpath
        assert source_pdf_sha256 == self.rebuilt.source_pdf_sha256
        return self.rebuilt

    def observe_source_pdf_text(self, relpath, *, document, expected_sha256):
        self.calls.append("native")
        assert document == self.fixture.envelope.provider_document
        assert expected_sha256 == document.source_pdf_sha256
        return self.fixture.observations


class RefusingExecutor:
    def adjudicate(self, *args, **kwargs):
        raise AssertionError("fixture has no model candidates")


class Fixture:
    def __init__(self, root, *, quality=False):
        self.paths = Paths(root)
        item = block(0, 0, 0, "请参阅公告2026〕7号。" if quality else "完整正文。")
        self.observations = (observation(item, "请参阅公告〔2026〕7号。"),) if quality else ()
        self.artifact_bytes = b"{}        "

        def artifact(**kwargs):
            return ProviderArtifact(**(kwargs | {"sha256": sha(self.artifact_bytes)}))

        def document(**kwargs):
            return ProviderDocument(**(kwargs | {"pages": (
                ProviderPage(0, (595.0, 842.0), (item,)), ProviderPage(1, (595.0, 842.0), ()),
            )}))

        with patch.object(atomic, "ProviderArtifact", artifact), patch.object(atomic, "ProviderDocument", document):
            materialized = atomic._publication_materialized_evidence()
        self.reservation, self.checkpoint, self.intent, self.receipt, self.local_manifest, self.envelope = materialized
        with patch.object(atomic, "_publication_materialized_evidence", return_value=materialized):
            original = atomic._request()
        context = self.intent.provider_envelope_context
        projection = json.loads(original.processing_run_projection_json)
        self.document = e.Document(document_id=context.document_id, status="parsed", security_id="sec-1",
            provider=context.provider, provider_document_id=context.provider_document_id,
            raw_file_relpath=context.source_pdf_relpath, raw_file_hash=self.envelope.input_raw_file_hash,
            class_filing_type="annual_report")
        self.run = e.ProcessingRun(processing_run_id=context.processing_run_id, document_id=context.document_id,
            artifact_owner_processing_run_id=context.processing_run_id, run_kind="parse", status="succeeded",
            parser_name=context.parser_target_identity.name, parser_version=context.parser_target_identity.package_version,
            parser_backend=context.parser_target_identity.backend, parser_method=context.parser_target_identity.method,
            parser_language=context.parser_target_identity.language, parser_target_identity=context.parser_target_identity.to_payload(),
            input_raw_file_hash=self.envelope.input_raw_file_hash, parser_artifact_relpath=context.parser_artifact_root_relpath,
            provider_document_relpath=projection["provider_document_relpath"], artifact_hash=sha(provider_document_envelope_to_bytes(self.envelope)))
        self.write(self.run.provider_document_relpath, provider_document_envelope_to_bytes(self.envelope))
        self.write(context.source_pdf_relpath, b"Synthetic source observation fixture, not a PDF".ljust(100, b" "))
        self.source = Source(self)
        self.admitted = ProviderDocumentAdmission(path_builder=self.paths, source=self.source).admit(
            document=self.document, run=self.run, artifact_owner=self.run, security_code="000001")
        self.base = build_provider_units(self.admitted)
        self.taxonomy = SemanticRouteTaxonomy(version="independent-r16.v1", definitions=(SemanticRouteDefinition(
            key="independent_topic", description="unrelated", labels=("ZZZ未出现主题ZZZ",), scopes=("annual_report",)),))
        self.routed = SemanticRouter(taxonomy=self.taxonomy, executor=RefusingExecutor()).route(
            admitted=self.admitted, document=semantic_document_context(self.document), drafts=self.base.units)
        units = tuple(ProductionAtomicPublicationRequestBuilderV4._unit(draft=draft, document=self.document,
                      processing_run_id=self.run.processing_run_id, admitted=self.admitted) for draft in self.routed.units)
        rows = tuple(SemanticRouteReceiptRowV3(processing_run_id=self.run.processing_run_id, unit_order_index=unit.unit_index,
                     provider_locator_sha256=unit.provider_locator_sha256, routed_draft_sha256=unit.routed_draft_sha256,
                     receipt=receipt) for unit, receipt in zip(units, self.routed.receipts, strict=True))
        terminal = semantic_adjudication_terminal_v1(self.routed.receipts)
        projection.update(unit_count=len(units), content_hash_aggregate=content_hash_aggregate(u.content_hash for u in units),
            structure_hash_aggregate=structure_hash_aggregate(u.structure_hash for u in units),
            semantic_route_receipts_sha256=sha(semantic_route_receipts_file_bytes_v3(rows)),
            semantic_adjudication_status=terminal.status, semantic_adjudication_summary=terminal.summary,
            semantic_degraded_unit_count=terminal.degraded_unit_count, semantic_failover_group_count=terminal.failover_group_count)
        values = {f.name: getattr(original, f.name) for f in fields(original) if f.name != "request_sha256"}
        values.update(units=units, semantic_route_receipts=rows, processing_run_projection_json=canonical(projection).decode(),
                      processing_run_projection_sha256=sha(canonical(projection)))
        self.request = seal_atomic_publication_request_v4(**values)
        self.preparation = atomic._artifact_preparation(self.request)
        self.manifest, self.reference = atomic._artifact_readiness(self.preparation)
        self.winner = replace(atomic._winner(self.request), artifact_readiness=self.reference, winner_row_version=2)
        for field, key in (("document_units_relpath", "document_units_relpath"),
                           ("semantic_route_receipts_relpath", "semantic_route_receipts_relpath"),
                           ("semantic_route_receipts_hash", "semantic_route_receipts_sha256"),
                           ("semantic_route_receipts_contract_version", "semantic_route_receipts_contract_version")):
            setattr(self.run, field, projection[key])
        self.write(self.manifest.preparation_relpath, self.preparation.canonical_bytes)
        self.write(self.reference.manifest_relpath, self.manifest.canonical_bytes)
        self.write(self.run.document_units_relpath, document_unit_snapshot_file_bytes_v1(request=self.request, bindings=self.preparation.unit_bindings))
        self.write(self.run.semantic_route_receipts_relpath, semantic_route_receipts_file_bytes_v3(rows))
        parser_root = Path(self.envelope.parser_artifact_root_relpath)
        for artifact in self.envelope.provider_document.artifacts:
            self.write(parser_root / artifact.relative_path, self.artifact_bytes)
        self.write(parser_root / "provider_document.v1.json", provider_document_envelope_to_bytes(self.envelope))
        self.write(parser_root / self.receipt.output_manifest_relpath, self.local_manifest.canonical_bytes)
        self.ready = FilesystemAtomicPublicationArtifactReadinessV4(paths=self.paths,
            immutable_store=ImmutableArtifactStore(self.paths), output_promotion=PublishedParserOutputVerifierV4(self.paths)
        ).verify_ready(reference=self.reference, expected_winner=self.winner)
        self.admission = M6AttemptAdmitted(**{name: getattr(self.checkpoint, name) for name in (
            "attempt_id", "fence_identity", "document_id", "processing_run_id", "source_pdf_sha256",
            "source_byte_count", "source_page_count", "process_profile_sha256")})
        self.snapshot = {"read_only": "on", "isolation": "repeatable read", "snapshot": "10:10:",
            "observed_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(), "identity": {
            "database_name": "invest_engine", "session_role": "disclosure_app", "current_role": "disclosure_app",
            "session_superuser": False, "current_superuser": False}}
        self.publication = M6PublicationCommitted(attempt_id=self.winner.attempt_id, processing_run_id=self.winner.processing_run_id,
            document_id=self.winner.document_id, source_pdf_sha256=self.admission.source_pdf_sha256, source_page_count=2,
            ledger_seq=7, winner_sha256=self.winner.sha256, durable_base_sha256=self.winner.durable_base_commit.durable_base_sha256)
        self.public = self.public_payload()
        self.source.calls.clear()

    def write(self, relpath, raw):
        path = self.paths.data_path(Path(relpath))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o600)

    def public_payload(self):
        units = []
        for expected in _expected_rows(self.ready):
            row = {name: None for name in _PUBLIC_FIELDS}
            row.update(expected, raw_file_hash=self.admission.source_pdf_sha256, contract_version="document_unit.v1",
                       is_active_run=True, created_at="2026-01-01T00:00:00+00:00", observed_at="2026-01-01T00:00:00+00:00")
            units.append(row)
        snapshot = json.loads(json.dumps(self.snapshot))
        snapshot["identity"].update(session_role="disclosure_reader", current_role="disclosure_reader")
        return dict(contract_version="m6.public-consumer-audit.v1", verifier_identity="independent-public-reader",
            snapshot=snapshot, admission=self.admission.model_dump(mode="json"), publication=self.publication.model_dump(mode="json"),
            document={"document_id": self.document.document_id, "raw_file_hash": self.document.raw_file_hash},
            run={"processing_run_id": self.run.processing_run_id, "artifact_hash": self.run.artifact_hash}, units=units, source_refs=[], public_units_sha256=sha(canonical(units)),
            page_size=100, queries_including_empty_tail=2, page_receipts=[], artifact_closure={}, artifact_closure_sha256=sha(canonical({})),
            artifact_read_budget={}, private_publication_audit_sha256=sha(b"independent private audit"),
            history_audit_receipt_sha256=sha(b"independent history"))

    def facts(self, cls):
        return cls(admission=self.admission, checkpoint_sha256=self.checkpoint.sha256, winner=self.winner,
            envelope_context=self.intent.provider_envelope_context, document=self.document, run=self.run,
            artifact_owner=self.run, security_code="000001", snapshot=self.snapshot)

    def check_fixture(self):
        return SimpleNamespace(units=len(self.request.units), quality=self.request.units[0].quality_status)
