"""Read-only, V3-aware whole-document qualification of one committed publication.

This adapter is the first implementation of ``M6QualificationSource``. It
qualifies a publication that already exists: private facts come from one
app-identity ``READ ONLY REPEATABLE READ`` snapshot, the source PDF and the
frozen MinerU bundle are re-admitted through the sole production admission
path, the sealed V3 receipt sidecar is validated and its nested V2 receipts are
replayed (never routed), the sealed pre-ID Units are rebuilt through the
existing pure projection, and the independent public reader's receipt bytes
are bound by hash and identity.

Authority boundaries: no database write, lock, ACK or owner event; no parser,
model or public-view call; no evidence sink overwrite. A missing or untrusted
public receipt makes the qualification unavailable instead of inventing a
digest. Contract mismatches after admission become named ``fail`` checks; the
reducer's ``qualify_document`` still owns the verdict.

One read budget spans the whole ``qualify`` call. The readiness verifier and
the V3 sidecar read charge it through the existing read-only store; the source
PDF, provider record and bundle rebuild are charged before the expensive
admission starts, as conservative reservations derived from the sealed plans
and the source's checked size. Reservations are upper bounds on streamed bytes,
not exact operating-system byte counts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import stat
from typing import Any, cast

import sqlalchemy as sa
from pydantic import TypeAdapter
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres.atomic_document_publisher_v4 import (
    PostgresAtomicWholeDocumentPublisherV4,
)
from disclosure_anchor.adapters.db.postgres.connection import require_runtime_app_connection
from disclosure_anchor.adapters.db.postgres.remote_parse_v4_repository import RemoteParseV4Repository
from disclosure_anchor.adapters.db.postgres.repositories import (
    DocumentRepository, ProcessingRunRepository, SecurityRepository,
)
from disclosure_anchor.adapters.db.postgres.schema import DATABASE_NAME, READER_ROLE
from disclosure_anchor.adapters.runtime.m6_public_consumer_verifier import (
    _PUBLIC_FIELDS, _ROW_FIELDS, _ReadBudget, _ReadOnlyOutput, _ReadOnlyStore, _canonical,
    _digest, _expected_rows,
)
from disclosure_anchor.adapters.storage.atomic_publication_artifact_readiness_v4 import (
    FilesystemAtomicPublicationArtifactReadinessV4,
)
from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4, PreIdUnitPublicationV4, WholeDocumentPublicationV4Error,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactReadinessError, AtomicPublicationArtifactsReadyV4,
)
from disclosure_anchor.application.contracts.m6_common import M6Id
from disclosure_anchor.application.contracts.m6_document_qualification import (
    M6_SERVICE_CHECKS, M6CheckId, M6CheckResult, M6QualificationEvidence, M6QualificationObservation,
)
from disclosure_anchor.application.contracts.m6_run_events import M6AttemptAdmitted, M6PublicationCommitted
from disclosure_anchor.application.contracts.provider_document_admission import (
    AdmittedProviderDocument, ProviderDocumentAdmissionError,
)
from disclosure_anchor.application.contracts.provider_quality import quality_occurrence_to_payload
from disclosure_anchor.application.contracts.provider_source_semantics import ProviderSourceSemantics
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderUnitApplicability, ProviderUnitDraft, ProviderUnitPayloadKind,
    ProviderUnitSearchContractError, provider_unit_locator_from_payload,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import ProviderEnvelopeContextV4
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_ROUTE_RECEIPT_V3, SemanticRouteContractError, SemanticRouteReceiptRowV3,
    SemanticRouteTaxonomy, semantic_route_receipt_row_v3_from_payload,
    semantic_route_receipts_file_bytes_v3, validate_semantic_route_receipt_rows_v3,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import AtomicPublicationWinnerV4
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.application.ports.provider_document_source import ProviderDocumentSourcePort
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticAdjudicationBatch, SemanticAdjudicationOutcome, SemanticExecutionGuard,
)
from disclosure_anchor.application.services.atomic_publication_request_builder_v4 import (
    ProductionAtomicPublicationRequestBuilderV4,
)
from disclosure_anchor.application.services.provider_document_admission import ProviderDocumentAdmission
from disclosure_anchor.application.services.provider_quality import assess_source_build_quality
from disclosure_anchor.application.services.provider_unit_builder import (
    ProviderUnitReplayContext, build_provider_units,
)
from disclosure_anchor.application.services.semantic_router import SemanticRouter, semantic_document_context
from disclosure_anchor.application.services.source_semantic_comparison import compare_build_conservation
from disclosure_anchor.domain import entities as e


REPORT_CONTRACT_VERSION = "m6.readonly-qualification-report.v1"
PUBLIC_RECEIPT_CONTRACT_VERSION = "m6.public-consumer-audit.v1"
# The exact top-level fields the public consumer verifier writes
# (``m6_public_consumer_verifier.py``, receipt assembly). Nothing else is a receipt.
_PUBLIC_RECEIPT_FIELDS = frozenset({
    "contract_version", "verifier_identity", "snapshot", "admission", "publication", "document", "run",
    "units", "source_refs", "public_units_sha256", "page_size", "queries_including_empty_tail",
    "page_receipts", "artifact_closure", "artifact_closure_sha256", "artifact_read_budget",
    "private_publication_audit_sha256", "history_audit_receipt_sha256",
})
_CHECK_IDS: tuple[M6CheckId, ...] = (*M6_SERVICE_CHECKS, "public_units_hash_match")
_CONSERVATION_CHECKS: tuple[M6CheckId, ...] = (
    "page_closure", "block_conservation", "table_segment_conservation",
    "logical_table_conservation", "retrieval_target_binding", "repair_binding",
    "finding_binding", "reading_order_contiguity", "heading_occurrence_closure",
)
# Streamed passes over the source PDF inside one full admission: the
# ``observe_source_pdf`` pair of hashes plus the page count read, then the
# native-text observation's hash, PDFium read and closing hash. Counted from
# ``adapters/storage/provider_document_source.py``; the two PDFium loads are
# metered as one whole-file pass each, their internal seeks are not.
_SOURCE_READ_PASSES = 6
# ``observe_source_pdf`` also reads the five-byte ``%PDF-`` signature.
_SOURCE_PROBE_BYTES = 5
# The pinned bundle reader streams every file once while scanning the tree,
# then reads role files again through ``read_bytes``/``read_json`` and once
# more in ``validate_utf8`` (``adapters/parsers/mineru_medium/artifacts.py``);
# images are streamed only by the scan. Three passes over the sealed parser
# tree therefore bound every explicit read of the rebuild.
_BUNDLE_READ_PASSES = 3
_SOURCE_IDENTITY_FIELDS = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
# Contract mismatches that become named failed checks. IO, configuration and
# cancellation errors are not in this tuple and propagate unchanged.
_CONTRACT_ERRORS = (
    ProviderDocumentAdmissionError, AtomicPublicationArtifactReadinessError,
    SemanticRouteContractError, WholeDocumentPublicationV4Error,
    ProviderUnitSearchContractError, ValueError, TypeError,
)


class M6QualificationUnavailable(ValueError):
    """The verifier cannot produce trustworthy evidence for this admission."""

    def __init__(self, phase: str, message: str, *, reason_code: str | None = None) -> None:
        super().__init__(f"{phase}: {message}")
        self.phase = phase
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class M6PrivateQualificationFacts:
    """Private, read-only facts for one committed attempt; never a claim."""

    admission: M6AttemptAdmitted
    checkpoint_sha256: str
    winner: AtomicPublicationWinnerV4
    envelope_context: ProviderEnvelopeContextV4
    document: e.Document
    run: e.ProcessingRun
    artifact_owner: e.ProcessingRun
    security_code: str
    snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class M6PublicReceiptInput:
    """Independent public reader receipt bytes with a caller-supplied expected hash."""

    receipt: bytes
    receipt_sha256: str
    expected_verifier_identity: str | None = None


@contextmanager
def _private_snapshot(engine: Engine) -> Iterator[tuple[Session, dict[str, Any]]]:
    with engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        connection.exec_driver_sql("SET LOCAL statement_timeout='15s'")
        identity = require_runtime_app_connection(connection)
        info = dict(connection.execute(sa.text(
            "SELECT current_setting('transaction_read_only') AS read_only, "
            "current_setting('transaction_isolation') AS isolation, "
            "pg_current_snapshot()::text AS snapshot, transaction_timestamp() AS observed_at"
        )).mappings().one())
        if info["read_only"] != "on" or info["isolation"] != "repeatable read":
            raise ValueError("M6 qualification snapshot is not read-only repeatable read")
        info["identity"] = asdict(identity)
        try:
            with Session(bind=connection, autoflush=False) as session:
                yield session, info
        finally:
            connection.rollback()


def read_private_qualification_facts(
    engine: Engine, *, attempt_id: str, admission: M6AttemptAdmitted | None = None,
) -> M6PrivateQualificationFacts:
    """Read one attempt's committed publication facts with the app identity.

    The same read-only repeatable-read discipline as the private publication
    verifier applies; the public reader principal is never used for private
    tables and no privilege is added. The admission, when supplied, must equal
    the persisted checkpoint identity field by field.
    """

    TypeAdapter(M6Id).validate_python(attempt_id)
    with _private_snapshot(engine) as (session, snapshot):
        stored = RemoteParseV4Repository(session).read_publication_snapshot(attempt_id)
        checkpoint = stored.checkpoint
        persisted = M6AttemptAdmitted(
            attempt_id=checkpoint.attempt_id, fence_identity=checkpoint.fence_identity,
            document_id=checkpoint.document_id, processing_run_id=checkpoint.processing_run_id,
            source_pdf_sha256=checkpoint.source_pdf_sha256,
            source_byte_count=checkpoint.source_byte_count,
            source_page_count=checkpoint.source_page_count,
            process_profile_sha256=checkpoint.process_profile_sha256,
        )
        if admission is not None and admission != persisted:
            raise ValueError("M6 admission differs from the stored publication checkpoint")
        if stored.winner is None or stored.materialization_intent is None:
            raise ValueError("M6 qualification requires a committed publication winner and intent")
        winner, intent = stored.winner, stored.materialization_intent
        if winner.artifact_readiness is None:
            raise ValueError("M6 qualification requires a winner bound to immutable artifact readiness")
        PostgresAtomicWholeDocumentPublisherV4.verify_publication_snapshot(
            session, winner=winner, context=intent.provider_envelope_context,
        )
        runs = ProcessingRunRepository(session)
        run = runs.get(checkpoint.processing_run_id)
        if run is None or run.document_id != checkpoint.document_id:
            raise ValueError("M6 qualification processing run is absent or belongs to another document")
        artifact_owner = runs.get(run.artifact_owner_processing_run_id)
        if artifact_owner is None:
            raise ValueError("M6 qualification parse owner is absent")
        document = DocumentRepository(session).get(checkpoint.document_id)
        if document is None or document.security_id is None:
            raise ValueError("M6 qualification document or its security identity is absent")
        security = SecurityRepository(session).get(document.security_id)
        if security is None or not security.security_code:
            raise ValueError("M6 qualification security is absent")
        return M6PrivateQualificationFacts(
            admission=persisted, checkpoint_sha256=checkpoint.sha256, winner=winner,
            envelope_context=intent.provider_envelope_context, document=document, run=run,
            artifact_owner=artifact_owner, security_code=security.security_code,
            snapshot={**snapshot, "document_current_processing_run_id": document.current_processing_run_id,
                      "run_is_active": run.is_active},
        )


class _RefusingExecutor:
    """A router executor that makes any model call an explicit failure."""

    @property
    def provider_identities(self) -> tuple[Any, ...]:
        raise RuntimeError("read-only qualification cannot discover semantic providers")

    def adjudicate(
        self, batch: SemanticAdjudicationBatch, *, group_hash: str,
        stage_guard: SemanticExecutionGuard | None = None,
    ) -> SemanticAdjudicationOutcome:
        raise RuntimeError("read-only qualification cannot call a semantic model")


class _Checks:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {
            name: {"outcome": "unverified", "reason": "not_evaluated", "detail": {}} for name in _CHECK_IDS
        }

    def passed(self, check: M6CheckId, **detail: Any) -> None:
        if self.items[check]["outcome"] == "fail":
            return
        self.items[check] = {"outcome": "pass", "reason": "verified", "detail": detail}

    def failed(self, check: M6CheckId, reason: str, **detail: Any) -> None:
        current = self.items[check]
        reasons = list(current.get("failures", []))
        reasons.append({"reason": reason, **detail})
        self.items[check] = {"outcome": "fail", "reason": reasons[0]["reason"],
                             "detail": current.get("detail", {}), "failures": reasons}

    def unverified(self, check: M6CheckId, reason: str) -> None:
        if self.items[check]["outcome"] == "unverified":
            self.items[check]["reason"] = reason

    def results(self) -> tuple[M6CheckResult, ...]:
        results = []
        for name in sorted(self.items):
            item = self.items[name]
            digest = None if item["outcome"] == "unverified" else _digest(_canonical({"check_id": name, **item}))
            results.append(M6CheckResult(check_id=cast(M6CheckId, name), outcome=item["outcome"], evidence_sha256=digest))
        return tuple(results)

    def payload(self) -> dict[str, Any]:
        return {name: self.items[name] for name in sorted(self.items)}


def _sealed_draft(unit: PreIdUnitPublicationV4) -> ProviderUnitDraft:
    if unit.page_no is None:
        raise ValueError("sealed Unit lacks its primary page")
    locator = provider_unit_locator_from_payload(
        strict_json_loads(unit.canonical_artifact_locator_json.encode("utf-8"))
    )
    payload = strict_json_loads(unit.canonical_payload_json.encode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("sealed Unit payload is not an object")
    return ProviderUnitDraft(
        unit_index=unit.unit_index - 1, payload_kind=cast(ProviderUnitPayloadKind, unit.payload_kind),
        payload=cast(dict[str, object], payload), title=unit.title, heading_path=unit.heading_path,
        section_keys=unit.section_keys, semantic_keys=unit.semantic_keys,
        applicability=cast(ProviderUnitApplicability | None, unit.applicability),
        quality_status=unit.quality_status, page_no=unit.page_no, locator=locator,
        content_hash=unit.content_hash, query_projection_hash=unit.query_projection_hash,
        structure_hash=unit.structure_hash,
    )


def _error(exc: BaseException) -> dict[str, str]:
    return {"error_type": type(exc).__name__, "error": str(exc)}


def _reserve(budget: _ReadBudget, byte_count: int, file_count: int, label: str) -> None:
    try:
        budget.reserve(byte_count, file_count)
    except ValueError as exc:
        raise M6QualificationUnavailable(
            "read_budget", f"{label} exceeds the qualification read budget "
            f"({byte_count} bytes, {file_count} files; reserved {budget.bytes_reserved}/{budget.maximum_bytes} bytes, "
            f"{budget.files_reserved}/{budget.maximum_files} files)",
        ) from exc


def _source_identity(info: os.stat_result) -> dict[str, int]:
    return {name: int(getattr(info, name)) for name in _SOURCE_IDENTITY_FIELDS}


class M6ReadonlyQualificationVerifier:
    """Independent, read-only whole-document qualification for e2e publications."""

    def __init__(
        self, *,
        private_facts_for: Callable[[M6AttemptAdmitted], M6PrivateQualificationFacts],
        paths: FileStorePathPort,
        source: ProviderDocumentSourcePort,
        taxonomy: SemanticRouteTaxonomy,
        batch_size: int,
        receipt_sink: Callable[[str, bytes], None],
        verifier_identity: str,
        public_receipt_for: Callable[[M6AttemptAdmitted], M6PublicReceiptInput | None] | None = None,
        maximum_artifact_bytes: int = 2 * 1024**3,
        maximum_artifact_files: int = 200_000,
        maximum_receipt_bytes: int = 64 * 1024**2,
    ) -> None:
        self._identity = TypeAdapter(M6Id).validate_python(verifier_identity)
        if type(batch_size) is not int or not 1 <= batch_size <= 32:
            raise ValueError("M6 qualification semantic batch size must be in 1..32")
        for value, label in ((maximum_artifact_bytes, "artifact byte"), (maximum_artifact_files, "artifact file"),
                             (maximum_receipt_bytes, "receipt byte")):
            if type(value) is not int or value < 1:
                raise ValueError(f"M6 qualification {label} limit is invalid")
        if type(taxonomy) is not SemanticRouteTaxonomy:
            raise ValueError("M6 qualification requires the exact semantic taxonomy")
        self._facts_for, self._paths, self._source = private_facts_for, paths, source
        self._taxonomy, self._batch_size = taxonomy, batch_size
        self._sink, self._public_for = receipt_sink, public_receipt_for
        self._artifact_bytes, self._artifact_files = maximum_artifact_bytes, maximum_artifact_files
        self._receipt_bytes = maximum_receipt_bytes

    # -- M6QualificationSource -------------------------------------------------

    def qualify(self, admission: M6AttemptAdmitted) -> M6QualificationEvidence:
        if (type(admission) is not M6AttemptAdmitted or admission.document_id is None
                or admission.processing_run_id is None):
            raise ValueError("M6 read-only qualification requires an exact E2E admission")
        report: dict[str, Any] = {
            "contract_version": REPORT_CONTRACT_VERSION, "verifier_identity": self._identity,
            "generated_utc": datetime.now(UTC).isoformat(), "admission": admission.model_dump(mode="json"),
            "phase_errors": [], "status": "unavailable",
        }
        checks = _Checks()
        try:
            evidence = self._qualify(admission, report, checks)
        except M6QualificationUnavailable as exc:
            report.update(unavailable_phase=exc.phase, unavailable_reason=str(exc),
                          unavailable_reason_code=exc.reason_code, checks=checks.payload())
            self._sink("qualification-report", _canonical(report))
            raise
        report["status"] = "evidence"
        report["checks"] = checks.payload()
        report["observation"] = evidence.observation.model_dump(mode="json")
        report["evidence_sha256"] = evidence.canonical_sha256()
        self._sink("qualification-report", _canonical(report))
        self._sink("qualification-evidence", evidence.canonical_bytes())
        return evidence

    # -- phases -----------------------------------------------------------------

    def _qualify(self, admission: M6AttemptAdmitted, report: dict[str, Any], checks: _Checks) -> M6QualificationEvidence:
        facts = self._facts_for(admission)
        if type(facts) is not M6PrivateQualificationFacts or facts.admission != admission:
            raise M6QualificationUnavailable("private_facts", "private facts belong to another admission")
        report.update(private_snapshot=facts.snapshot, checkpoint_sha256=facts.checkpoint_sha256,
                      winner_sha256=facts.winner.sha256)
        budget = _ReadBudget(self._artifact_bytes, self._artifact_files)
        store = _ReadOnlyStore(self._paths, budget)
        report["read_budget"] = {
            "scope": "one budget for the whole qualification: readiness resources, provider record, "
                     "source PDF passes, bundle rebuild passes, v3 sidecar and public receipt; "
                     "reservations are conservative upper bounds, not measured bytes",
            "source_read_passes": _SOURCE_READ_PASSES, "bundle_read_passes": _BUNDLE_READ_PASSES,
        }
        try:
            ready = self._readiness(facts, report, budget, store)
            source_before = self._bind_source_inputs(facts, ready, report, budget)
            admitted = self._admit(facts, ready, source_before, report, checks)
            rows = self._v3_rows(facts, ready, store, report, checks)
            base_build = build_provider_units(admitted)
            routed_units = self._replay(facts, admitted, base_build.units, rows, report, checks)
            self._projection(facts, ready, admitted, base_build, routed_units, report, checks)
            quality = self._quality(admitted, base_build, ready.request, report, checks)
            public_digest = self._public(admission, facts, ready, budget, report, checks)
        finally:
            report["read_budget"].update(asdict(budget))
        observation = M6QualificationObservation(
            mode="e2e_publication", source_pdf_sha256=admission.source_pdf_sha256,
            source_byte_count=admission.source_byte_count, source_page_count=admission.source_page_count,
            provider_page_count=len(admitted.provider_document.pages),
            processing_run_id=admission.processing_run_id,
            provider_bundle_sha256=admitted.provider_document.bundle_sha256,
            provider_document_sha256=admitted.provider_document_sha256,
            public_units_sha256=public_digest, unit_count=len(ready.request.units),
            unusable_unit_count=quality["unusable_unit_count"],
            needs_review_unit_count=quality["needs_review_unit_count"],
            review_reasons=tuple(quality["review_reasons"]), checks=checks.results(),
        )
        return M6QualificationEvidence(observation=observation, reviews=())

    def _readiness(self, facts: M6PrivateQualificationFacts, report: dict[str, Any],
                   budget: _ReadBudget, store: _ReadOnlyStore) -> AtomicPublicationArtifactsReadyV4:
        winner, admission = facts.winner, facts.admission
        reference = winner.artifact_readiness
        if reference is None:
            raise M6QualificationUnavailable("readiness", "winner is not bound to immutable artifact readiness")
        verifier = FilesystemAtomicPublicationArtifactReadinessV4(
            paths=self._paths, immutable_store=store, output_promotion=_ReadOnlyOutput(self._paths, budget),
        )
        try:
            ready = verifier.verify_ready(reference=reference, expected_winner=winner)
        except M6QualificationUnavailable:
            raise
        except _CONTRACT_ERRORS as exc:
            report["readiness"] = {"reference": asdict(reference), **_error(exc)}
            phase = "read_budget" if "budget" in str(exc) else "readiness"
            raise M6QualificationUnavailable(phase, str(exc)) from exc
        request = ready.request
        if (request.identity.attempt_id != admission.attempt_id
                or request.identity.document_id != admission.document_id
                or request.identity.processing_run_id != admission.processing_run_id
                or request.upstream_evidence.source_pdf_sha256 != admission.source_pdf_sha256
                or request.source_page_count != admission.source_page_count
                or ready.preparation.provider_document_plan.relpath != facts.run.provider_document_relpath
                or ready.preparation.semantic_route_receipts_plan.relpath != facts.run.semantic_route_receipts_relpath
                or ready.preparation.semantic_route_receipts_plan.sha256 != facts.run.semantic_route_receipts_hash):
            raise M6QualificationUnavailable("readiness", "sealed request does not close the admitted run/source")
        report["readiness"] = {
            "reference": asdict(reference), "resources_sha256": ready.manifest.resources_sha256,
            "request_sha256": request.request_sha256, "sealed_unit_count": len(request.units),
            "parser_output": asdict(ready.preparation.parser_output_plan),
            "provider_document_plan": asdict(ready.preparation.provider_document_plan),
            "semantic_route_receipts_plan": asdict(ready.preparation.semantic_route_receipts_plan),
            "read_budget_after_readiness": asdict(budget),
        }
        return ready

    def _bind_source_inputs(self, facts: M6PrivateQualificationFacts, ready: AtomicPublicationArtifactsReadyV4,
                            report: dict[str, Any], budget: _ReadBudget) -> dict[str, int]:
        """Bind the source/bundle paths and reserve their reads before admission.

        The raw PDF is only stat-checked here: it must be the sealed source
        path, a regular non-symlink file, and exactly the admitted byte size.
        Only then are the record, the source passes and the bundle passes
        reserved against the shared budget.
        """

        admission, preparation = facts.admission, ready.preparation
        relpath = facts.document.raw_file_relpath
        parser_root = facts.run.parser_artifact_relpath
        if (not relpath or relpath != facts.envelope_context.source_pdf_relpath
                or relpath != ready.request.upstream_evidence.source_pdf_relpath):
            raise M6QualificationUnavailable("source_binding", "document source path differs from the sealed publication")
        if (not parser_root or parser_root != preparation.parser_output_plan.published_relpath
                or parser_root != facts.envelope_context.parser_artifact_root_relpath):
            raise M6QualificationUnavailable("source_binding", "parse owner bundle path differs from the sealed parser output")
        relative = Path(relpath)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise M6QualificationUnavailable("source_binding", "document source path is not a safe relative path")
        try:
            info = os.lstat(self._paths.data_path(relative))
        except OSError as exc:
            raise M6QualificationUnavailable("source_binding", f"cannot stat the source PDF: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise M6QualificationUnavailable("source_binding", "source PDF is not a regular file")
        if info.st_size != admission.source_byte_count:
            raise M6QualificationUnavailable(
                "source_binding", f"source PDF size {info.st_size} differs from admitted {admission.source_byte_count}",
            )
        _reserve(budget, preparation.provider_document_plan.byte_count, 1, "provider record read")
        _reserve(budget, admission.source_byte_count * _SOURCE_READ_PASSES + _SOURCE_PROBE_BYTES, 1,
                 "source PDF admission passes")
        _reserve(budget, preparation.parser_output_plan.byte_count * _BUNDLE_READ_PASSES,
                 preparation.parser_output_plan.file_count * _BUNDLE_READ_PASSES, "bundle rebuild passes")
        identity = _source_identity(info)
        report["source_binding"] = {"source_pdf_relpath": relpath, "parser_artifact_root_relpath": parser_root,
                                    "source_stat_before": identity, "read_budget_after_reservation": asdict(budget)}
        return identity

    def _admit(self, facts: M6PrivateQualificationFacts, ready: AtomicPublicationArtifactsReadyV4,
               source_before: dict[str, int], report: dict[str, Any], checks: _Checks) -> AdmittedProviderDocument:
        admission = facts.admission
        try:
            admitted = ProviderDocumentAdmission(path_builder=self._paths, source=self._source).admit(
                document=facts.document, run=facts.run, artifact_owner=facts.artifact_owner,
                security_code=facts.security_code,
            )
            source_after = _source_identity(os.lstat(self._paths.data_path(Path(cast(str, facts.document.raw_file_relpath)))))
        except M6QualificationUnavailable:
            raise
        except ProviderDocumentAdmissionError as exc:
            report["source_admission"] = {"reason_code": exc.reason_code, "retryable": exc.retryable, **_error(exc)}
            raise M6QualificationUnavailable("source_admission", str(exc), reason_code=exc.reason_code) from exc
        except (*_CONTRACT_ERRORS, OSError) as exc:
            report["source_admission"] = _error(exc)
            raise M6QualificationUnavailable("source_admission", str(exc)) from exc
        document = admitted.provider_document
        envelope = admitted.envelope
        detail = {
            "provider_document_sha256": admitted.provider_document_sha256,
            "provider_document_relpath": admitted.provider_document_relpath.as_posix(),
            "bundle_sha256": document.bundle_sha256,
            "parser_artifact_root_relpath": envelope.parser_artifact_root_relpath,
            "source_pdf_sha256": document.source_pdf_sha256,
            "source_stat_before": source_before, "source_stat_after": source_after,
            "observed_source_byte_count": source_after["st_size"],
            "envelope_source_page_count": envelope.source_pdf_page_count,
            "provider_page_count": len(document.pages), "block_count": len(document.blocks),
            "artifact_count": len(document.artifacts),
            "reconciliation_count": len(admitted.source_text_reconciliations),
            "finding_count": len(admitted.source_quality_findings),
        }
        report["source_admission"] = detail
        identity_failures: list[dict[str, Any]] = []
        for label, actual in (
            ("provider_document.source_pdf_sha256", document.source_pdf_sha256),
            ("envelope.input_raw_file_hash", envelope.input_raw_file_hash),
            ("winner.durable_base.source_identity_sha256", facts.winner.durable_base_commit.source_identity_sha256),
            ("request.upstream_evidence.source_pdf_sha256", ready.request.upstream_evidence.source_pdf_sha256),
        ):
            if actual != admission.source_pdf_sha256:
                identity_failures.append({"field": label, "actual": actual})
        if source_after != source_before or source_after["st_size"] != admission.source_byte_count:
            identity_failures.append({"field": "source_stat_after", "actual": source_after,
                                      "reason": "source_changed_during_admission"})
        record_hashes: tuple[tuple[str, str | None], ...] = (
            ("preparation.provider_document_plan.sha256", ready.preparation.provider_document_plan.sha256),
            ("run.artifact_hash", facts.run.artifact_hash),
        )
        for label, record_hash in record_hashes:
            if record_hash != admitted.provider_document_sha256:
                identity_failures.append({"field": label, "actual": record_hash})
        if identity_failures:
            for failure in identity_failures:
                checks.failed("source_identity", "identity_mismatch", **failure)
        else:
            checks.passed("source_identity", **detail)
        checks.passed("independent_rebuild_match", bundle_sha256=document.bundle_sha256,
                      parser_artifact_root_relpath=envelope.parser_artifact_root_relpath,
                      provider_document_sha256=admitted.provider_document_sha256,
                      rebuilt_equals_envelope=True)
        checks.passed("artifact_closure", readiness_reference=asdict(ready.reference),
                      resources_sha256=ready.manifest.resources_sha256,
                      parser_output=asdict(ready.preparation.parser_output_plan),
                      bundle_sha256=document.bundle_sha256, artifact_count=len(document.artifacts))
        return admitted

    def _v3_rows(self, facts: M6PrivateQualificationFacts, ready: AtomicPublicationArtifactsReadyV4,
                 store: _ReadOnlyStore, report: dict[str, Any], checks: _Checks,
                 ) -> tuple[SemanticRouteReceiptRowV3, ...] | None:
        plan = ready.preparation.semantic_route_receipts_plan
        run = facts.run
        try:
            if run.semantic_route_receipts_contract_version != SEMANTIC_ROUTE_RECEIPT_V3:
                raise SemanticRouteContractError("processing run semantic receipts are not v3")
            # The shared read-only store charges this second sidecar read to
            # the qualification budget; a budget failure is not a contract fail.
            try:
                raw = store.read_exact(relpath=Path(plan.relpath), expected_sha256=plan.sha256,
                                       expected_byte_count=plan.byte_count, max_byte_count=plan.byte_count)
            except ValueError as exc:
                if "budget" in str(exc):
                    raise M6QualificationUnavailable("read_budget", str(exc)) from exc
                raise
            if not raw or any(not line.strip() for line in raw.splitlines()):
                raise SemanticRouteContractError("semantic receipt v3 JSONL is not canonical")
            rows = tuple(semantic_route_receipt_row_v3_from_payload(strict_json_loads(line)) for line in raw.splitlines())
            validate_semantic_route_receipt_rows_v3(rows, processing_run_id=run.processing_run_id)
            if semantic_route_receipts_file_bytes_v3(rows) != raw:
                raise SemanticRouteContractError("semantic receipt v3 bytes are not canonical")
            if ready.request.semantic_route_receipts != rows:
                raise SemanticRouteContractError("sealed request receipts differ from the v3 sidecar")
            units = ready.request.units
            if len(units) != len(rows):
                raise SemanticRouteContractError("sealed Unit count differs from v3 rows")
            for unit, row in zip(units, rows, strict=True):
                if (unit.unit_index, unit.provider_locator_sha256, unit.routed_draft_sha256) != (
                        row.unit_order_index, row.provider_locator_sha256, row.routed_draft_sha256):
                    raise SemanticRouteContractError(f"v3 row {row.unit_order_index} differs from its sealed Unit")
        except M6QualificationUnavailable:
            # A read-budget failure is unavailability, never a semantic fail.
            raise
        except _CONTRACT_ERRORS as exc:
            report["v3_sidecar"] = {"relpath": plan.relpath, "sha256": plan.sha256, **_error(exc)}
            checks.failed("reading_order_contiguity", "v3_outer:" + type(exc).__name__, message=str(exc))
            return None
        decisions: dict[str, int] = {}
        for row in rows:
            decisions[row.receipt.decision_source] = decisions.get(row.receipt.decision_source, 0) + 1
        report["v3_sidecar"] = {"relpath": plan.relpath, "sha256": plan.sha256, "rows": len(rows),
                                "decision_sources": dict(sorted(decisions.items()))}
        return rows

    def _replay(self, facts: M6PrivateQualificationFacts, admitted: AdmittedProviderDocument,
                drafts: tuple[ProviderUnitDraft, ...], rows: tuple[SemanticRouteReceiptRowV3, ...] | None,
                report: dict[str, Any], checks: _Checks) -> tuple[ProviderUnitDraft, ...] | None:
        if rows is None:
            report["replay"] = {"performed": False, "reason": "v3_sidecar_invalid"}
            return None
        router = SemanticRouter(taxonomy=self._taxonomy, executor=_RefusingExecutor(), batch_size=self._batch_size)
        try:
            if len(drafts) != len(rows):
                raise SemanticRouteContractError("fresh draft count differs from frozen receipts")
            routed = router.replay(
                admitted=admitted, document=semantic_document_context(facts.document), drafts=drafts,
                receipts=tuple(row.receipt for row in rows),
            )
        except _CONTRACT_ERRORS as exc:
            report["replay"] = {"performed": True, "succeeded": False, **_error(exc)}
            checks.failed("reading_order_contiguity", "semantic_replay:" + type(exc).__name__, message=str(exc))
            return None
        report["replay"] = {"performed": True, "succeeded": True, "units": len(routed.units),
                            "taxonomy_version": self._taxonomy.version, "new_model_calls": 0}
        return routed.units

    def _projection(self, facts: M6PrivateQualificationFacts, ready: AtomicPublicationArtifactsReadyV4,
                    admitted: AdmittedProviderDocument, base_build: Any,
                    routed_units: tuple[ProviderUnitDraft, ...] | None,
                    report: dict[str, Any], checks: _Checks) -> None:
        request: AtomicPublicationRequestV4 = ready.request
        page_count = facts.admission.source_page_count
        pages = admitted.provider_document.pages
        detail: dict[str, Any] = {"sealed_units": len(request.units), "fresh_units": len(base_build.units),
                                  "fresh_unassigned_table_parts": len(base_build.unassigned_table_parts)}
        if (len(pages) != page_count or admitted.envelope.source_pdf_page_count != page_count
                or tuple(page.page_index for page in pages) != tuple(range(page_count))):
            checks.failed("page_closure", "provider_pages_differ_from_source", provider_page_count=len(pages),
                          envelope_page_count=admitted.envelope.source_pdf_page_count, source_page_count=page_count)
        sealed: list[ProviderUnitDraft] = []
        for unit in request.units:
            if unit.page_no is None or not 1 <= unit.page_no <= page_count or any(
                    not 1 <= number <= page_count for number in unit.page_numbers):
                checks.failed("page_closure", "sealed_unit_page_out_of_range", unit_index=unit.unit_index)
            try:
                sealed.append(_sealed_draft(unit))
            except _CONTRACT_ERRORS as exc:
                checks.failed("reading_order_contiguity", "sealed_unit_decode_failed", unit_index=unit.unit_index,
                              **_error(exc))
        if tuple(unit.unit_index for unit in request.units) != tuple(range(1, len(request.units) + 1)):
            checks.failed("reading_order_contiguity", "sealed_unit_index_not_contiguous")
        fresh = routed_units if routed_units is not None else base_build.units
        if len(sealed) == len(request.units):
            differences = compare_build_conservation(
                tuple(sealed), fresh, candidate_unassigned=(), reference_unassigned=base_build.unassigned_table_parts,
            )
            detail["differences"] = differences
            for check in _CONSERVATION_CHECKS:
                for mismatch in differences.get(check, []):
                    checks.failed(check, "conservation_difference", **mismatch)
            context = ProviderUnitReplayContext(admitted)
            replay_failures = []
            for draft in sealed:
                for index, binding in enumerate(draft.locator.search_targets):
                    for label, replay in (("values", context.replay_search_binding),
                                          ("scalar", context.replay_search_binding_source_text)):
                        try:
                            replay(draft, binding)
                        except _CONTRACT_ERRORS as exc:
                            replay_failures.append({"unit_index": draft.unit_index, "binding_index": index,
                                                    "replay": label, **_error(exc)})
            detail["binding_replay_failures"] = replay_failures
            for failure in replay_failures:
                checks.failed("retrieval_target_binding", "binding_replay_failed", **failure)
            if routed_units is not None:
                try:
                    projected = tuple(ProductionAtomicPublicationRequestBuilderV4._unit(
                        draft=draft, document=facts.document, processing_run_id=cast(str, facts.admission.processing_run_id),
                        admitted=admitted,
                    ) for draft in routed_units)
                except _CONTRACT_ERRORS as exc:
                    checks.failed("reading_order_contiguity", "pre_id_projection_failed", **_error(exc))
                else:
                    mismatched: list[dict[str, Any]] = []
                    if len(projected) != len(request.units):
                        mismatched.append({"count": [len(projected), len(request.units)]})
                    else:
                        for index, (actual, original) in enumerate(zip(projected, request.units, strict=True), 1):
                            if actual != original:
                                mismatched.append({"unit_order_index": index, "fields": [
                                    name for name in PreIdUnitPublicationV4.__dataclass_fields__
                                    if getattr(actual, name) != getattr(original, name)]})
                    detail["pre_id_projection_mismatches"] = mismatched
                    for mismatch in mismatched:
                        checks.failed("reading_order_contiguity", "pre_id_projection_differs", **mismatch)
            else:
                checks.failed("reading_order_contiguity", "routed_units_unavailable")
        else:
            for check in _CONSERVATION_CHECKS:
                checks.failed(check, "sealed_units_not_decoded")
        report["projection"] = detail
        for check in _CONSERVATION_CHECKS:
            checks.passed(check, **{key: value for key, value in detail.items() if key != "differences"})

    def _quality(self, admitted: AdmittedProviderDocument, base_build: Any,
                 request: AtomicPublicationRequestV4, report: dict[str, Any], checks: _Checks) -> dict[str, Any]:
        try:
            semantics = ProviderSourceSemantics(
                admitted.provider_document, admitted.source_text_reconciliations, admitted.source_quality_findings,
            )
            occurrences = assess_source_build_quality(semantics, base_build)
        except _CONTRACT_ERRORS as exc:
            checks.failed("finding_binding", "quality_assessment_failed", **_error(exc))
            occurrences = ()
        reasons = sorted({item.reason_id for item in occurrences})
        statuses = [unit.quality_status for unit in request.units]
        quality = {
            "review_reasons": reasons, "occurrence_count": len(occurrences),
            "occurrences": [quality_occurrence_to_payload(item) for item in occurrences],
            "needs_review_unit_count": statuses.count("needs_review"),
            "unusable_unit_count": statuses.count("unusable"),
            "fresh_needs_review_unit_count": sum(1 for unit in base_build.units if unit.quality_status == "needs_review"),
        }
        report["quality"] = quality
        return quality

    def _public(self, admission: M6AttemptAdmitted, facts: M6PrivateQualificationFacts,
                ready: AtomicPublicationArtifactsReadyV4, budget: _ReadBudget,
                report: dict[str, Any], checks: _Checks) -> str:
        provided = None if self._public_for is None else self._public_for(admission)
        if provided is None:
            report["public_receipt"] = {"provided": False}
            raise M6QualificationUnavailable("public_receipt_missing",
                                             "no independent public receipt was supplied for this admission")
        if type(provided) is not M6PublicReceiptInput:
            raise M6QualificationUnavailable("public_receipt_invalid", "public receipt input is not exact")
        raw = provided.receipt
        if type(raw) is not bytes or not 0 < len(raw) <= self._receipt_bytes:
            report["public_receipt"] = {"provided": True, "expected_sha256": provided.receipt_sha256,
                                        "byte_count": None if type(raw) is not bytes else len(raw)}
            raise M6QualificationUnavailable("public_receipt_invalid", "public receipt bytes are empty or exceed the receipt bound")
        _reserve(budget, len(raw), 1, "public receipt bytes")
        if _digest(raw) != provided.receipt_sha256:
            report["public_receipt"] = {"provided": True, "expected_sha256": provided.receipt_sha256, "byte_count": len(raw)}
            raise M6QualificationUnavailable("public_receipt_invalid", "public receipt bytes do not match their expected hash")
        parsed = strict_json_loads(raw)
        if (not isinstance(parsed, dict) or _canonical(parsed) != raw
                or parsed.get("contract_version") != PUBLIC_RECEIPT_CONTRACT_VERSION):
            report["public_receipt"] = {"provided": True, "sha256": provided.receipt_sha256}
            raise M6QualificationUnavailable("public_receipt_invalid", "public receipt is not its canonical contract")
        if set(parsed) != _PUBLIC_RECEIPT_FIELDS:
            report["public_receipt"] = {"provided": True, "sha256": provided.receipt_sha256,
                                        "unexpected_fields": sorted(set(parsed) - _PUBLIC_RECEIPT_FIELDS),
                                        "missing_fields": sorted(_PUBLIC_RECEIPT_FIELDS - set(parsed))}
            raise M6QualificationUnavailable("public_receipt_invalid", "public receipt fields are not closed")
        snapshot = parsed.get("snapshot")
        identity = snapshot.get("identity") if isinstance(snapshot, dict) else None
        private_identity = facts.snapshot.get("identity") if isinstance(facts.snapshot, dict) else None
        private_database = private_identity.get("database_name") if isinstance(private_identity, dict) else DATABASE_NAME
        if (not isinstance(snapshot, dict) or not isinstance(identity, dict)
                or snapshot.get("read_only") != "on" or snapshot.get("isolation") != "repeatable read"
                or identity.get("session_role") != READER_ROLE or identity.get("current_role") != READER_ROLE
                or identity.get("session_superuser") is not False or identity.get("current_superuser") is not False):
            report["public_receipt"] = {"provided": True, "sha256": provided.receipt_sha256}
            raise M6QualificationUnavailable("public_receipt_invalid", "public receipt principal is not the independent reader")
        if identity.get("database_name") != DATABASE_NAME or private_database != DATABASE_NAME:
            report["public_receipt"] = {"provided": True, "sha256": provided.receipt_sha256,
                                        "database_name": identity.get("database_name")}
            raise M6QualificationUnavailable("public_receipt_invalid", "public receipt database differs from the private facts")
        declared = parsed.get("public_units_sha256")
        units = parsed.get("units")
        if type(declared) is not str or not isinstance(units, list):
            report["public_receipt"] = {"provided": True, "sha256": provided.receipt_sha256}
            raise M6QualificationUnavailable("public_receipt_invalid", "public receipt lacks units or their digest")
        recomputed = _digest(_canonical(units))
        detail: dict[str, Any] = {
            "provided": True, "sha256": provided.receipt_sha256, "byte_count": len(raw),
            "verifier_identity": parsed.get("verifier_identity"), "declared_public_units_sha256": declared,
            "recomputed_public_units_sha256": recomputed, "unit_rows": len(units),
            "sealed_units": len(ready.request.units),
        }
        report["public_receipt"] = detail
        failures: list[dict[str, Any]] = []
        if recomputed != declared:
            failures.append({"reason": "declared_digest_differs_from_rows"})
        if (provided.expected_verifier_identity is not None
                and parsed.get("verifier_identity") != provided.expected_verifier_identity):
            failures.append({"reason": "verifier_identity_differs", "actual": parsed.get("verifier_identity")})
        try:
            if M6AttemptAdmitted.model_validate(parsed.get("admission")) != admission:
                failures.append({"reason": "admission_differs"})
        except Exception as exc:  # noqa: BLE001 - pydantic errors are contract failures here
            failures.append({"reason": "admission_invalid", **_error(exc)})
        winner = facts.winner
        try:
            publication = M6PublicationCommitted.model_validate(parsed.get("publication"))
        except Exception as exc:  # noqa: BLE001 - pydantic errors are contract failures here
            failures.append({"reason": "publication_invalid", **_error(exc)})
        else:
            # The committed publication is the winner's own identity; only the
            # ledger sequence is a durable-base fact this consumer cannot rebuild,
            # so it is bound to the receipt's declared value and recorded.
            expected_publication = M6PublicationCommitted(
                attempt_id=admission.attempt_id, processing_run_id=cast(str, admission.processing_run_id),
                document_id=cast(str, admission.document_id), source_pdf_sha256=admission.source_pdf_sha256,
                source_page_count=admission.source_page_count, ledger_seq=publication.ledger_seq,
                winner_sha256=winner.sha256, durable_base_sha256=winner.durable_base_commit.durable_base_sha256,
            )
            detail["publication_ledger_seq"] = publication.ledger_seq
            if publication != expected_publication:
                failures.append({"reason": "publication_identity_differs", "fields": sorted(
                    name for name in M6PublicationCommitted.model_fields
                    if getattr(publication, name) != getattr(expected_publication, name)
                )})
        document, run = parsed.get("document"), parsed.get("run")
        if (not isinstance(document, dict) or not isinstance(run, dict)
                or document.get("document_id") != admission.document_id
                or document.get("raw_file_hash") != admission.source_pdf_sha256
                or run.get("processing_run_id") != admission.processing_run_id
                or run.get("artifact_hash") != ready.request.upstream_evidence.provider_envelope_sha256):
            failures.append({"reason": "document_run_identity_differs"})
        expected = _expected_rows(ready)
        if len(units) != len(expected):
            failures.append({"reason": "unit_row_count_differs", "actual": len(units), "expected": len(expected)})
        else:
            for index, (row, expected_row) in enumerate(zip(units, expected, strict=True)):
                if (not isinstance(row, dict) or set(row) != _PUBLIC_FIELDS
                        or _canonical({name: row[name] for name in _ROW_FIELDS}) != _canonical(expected_row)
                        or row.get("is_active_run") is not True or row.get("raw_file_hash") != admission.source_pdf_sha256):
                    failures.append({"reason": "unit_row_differs_from_sealed_projection", "row_index": index})
                    break
        if failures:
            for failure in failures:
                checks.failed("public_units_hash_match", **failure)
        else:
            checks.passed("public_units_hash_match", **detail)
        return declared


__all__ = [
    "M6PrivateQualificationFacts",
    "M6PublicReceiptInput",
    "M6QualificationUnavailable",
    "M6ReadonlyQualificationVerifier",
    "PUBLIC_RECEIPT_CONTRACT_VERSION",
    "REPORT_CONTRACT_VERSION",
    "read_private_qualification_facts",
]
