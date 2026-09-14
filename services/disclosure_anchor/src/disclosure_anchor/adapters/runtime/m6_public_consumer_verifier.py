"""Independent public-v1 consumption of one exact committed M6 publication.

The assembly supplies authenticated private audit bytes, not a writer engine.
Only this reader's actual public rows and immutable file reads issue its receipt.
Process isolation, owner stamps and whole-document quality remain assembly gates.

Evidence references resolve through the existing API integrity checks against
one provider envelope read for the confirmation's exact owner; each Unit still
binds its own locator and each artifact's bytes are read and verified. Read cost
is therefore linear in referenced artifact bytes, not references x envelope.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, cast

from pydantic import TypeAdapter
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from disclosure_anchor.adapters.db.postgres.connection import require_runtime_reader_connection
from disclosure_anchor.adapters.storage.atomic_publication_artifact_readiness_v4 import (
    FilesystemAtomicPublicationArtifactReadinessV4,
)
from disclosure_anchor.adapters.storage.immutable_artifact_store import (
    ImmutableArtifactStore, _DirectoryChain, _file_flags, _full_identity,
)
from disclosure_anchor.adapters.storage.published_parser_output_verifier_v4 import PublishedParserOutputVerifierV4
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.api.unit_evidence import (
    ProviderEvidenceOwner, read_provider_envelope, read_unit_evidence, unit_evidence_refs,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactsReadyV4,
)
from disclosure_anchor.application.contracts.m6_common import M6Id
from disclosure_anchor.application.contracts.m6_run import M6SourceHistoryFact
from disclosure_anchor.application.contracts.m6_run_events import (
    M6AttemptAdmitted, M6PublicConfirmation, M6PublicationCommitted,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationWinnerV4, decode_atomic_publication_winner_v4,
)
from disclosure_anchor.application.ports.file_store import ArtifactWriteResult, FileStorePathPort


_MAX_PRIVATE_BYTES = 16 * 1024**2
_ROW_FIELDS = (
    "applicability", "artifact_locator", "asset_id", "content_hash", "document_id",
    "heading_path", "order_index", "page_no", "payload", "payload_kind", "processing_run_id",
    "provider_document_id", "quality_status", "query_projection_hash", "section_keys",
    "semantic_keys", "structure_hash", "title",
)
_PUBLIC_FIELDS = frozenset(_ROW_FIELDS) | {
    "is_active_run", "heading_path_text", "created_at", "contract_version", "company_ref", "security_ref",
    "security_code", "exchange", "filing_type", "disclosure_topics", "report_period", "announcement_date",
    "producer_action_ref", "source_ref", "parent_ref", "asset_kind", "observed_at", "source_tier",
    "trace_level", "raw_file_hash", "body_status",
}


@dataclass(frozen=True, slots=True)
class M6PublicAuditInput:
    publication: M6PublicationCommitted
    publication_audit_sha256: str
    publication_audit: bytes
    history: M6SourceHistoryFact
    history_audit: bytes


def _canonical(value: object) -> bytes:
    def temporal(item: object) -> str:
        if isinstance(item, datetime):
            if item.tzinfo is None or item.utcoffset() is None:
                raise ValueError("M6 public audit timestamp has no timezone")
            return item.astimezone(UTC).isoformat()
        if isinstance(item, date):
            return item.isoformat()
        raise TypeError("unsupported public audit value: " + type(item).__name__)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=temporal).encode()


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _audit(raw: bytes, digest: str, version: str) -> dict[str, Any]:
    if type(raw) is not bytes or not 0 < len(raw) <= _MAX_PRIVATE_BYTES or _digest(raw) != digest:
        raise ValueError("M6 private audit bytes do not match their bounded receipt")
    result = strict_json_loads(raw)
    if not isinstance(result, dict) or _canonical(result) != raw or result.get("contract_version") != version:
        raise ValueError("M6 private audit is not its canonical contract")
    expected_fields = ({"contract_version", "snapshot", "admission", "checkpoint_sha256", "publication",
                        "winner_canonical_json", "published_event_id"}
                       if version == "m6.private-publication-audit.v1" else
                       {"contract_version", "snapshot", "sources", "base_rows", "publication_witnesses",
                        "unattributed_publication_count", "projections", "query_sha256"})
    if set(result) != expected_fields:
        raise ValueError("M6 private audit fields are not closed")
    snapshot = result.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("read_only") != "on" or snapshot.get("isolation") != "repeatable read":
        raise ValueError("M6 private audit lacks its read-only snapshot")
    identity = snapshot.get("identity")
    if (not isinstance(identity, dict) or identity.get("session_role") != "disclosure_app"
            or identity.get("current_role") != "disclosure_app"
            or identity.get("session_superuser") is not False or identity.get("current_superuser") is not False):
        raise ValueError("M6 private audit principal differs from the private verifier")
    return result


def _bind_inputs(admission: M6AttemptAdmitted, value: M6PublicAuditInput) -> tuple[AtomicPublicationWinnerV4, str]:
    if type(value) is not M6PublicAuditInput or type(value.publication) is not M6PublicationCommitted or type(value.history) is not M6SourceHistoryFact:
        raise ValueError("M6 public verifier requires exact private audit inputs")
    audit = _audit(value.publication_audit, value.publication_audit_sha256, "m6.private-publication-audit.v1")
    history = _audit(value.history_audit, value.history.audit_receipt_sha256, "m6.private-source-history-audit.v1")
    if (M6AttemptAdmitted.model_validate(audit.get("admission")) != admission
            or M6PublicationCommitted.model_validate(audit.get("publication")) != value.publication
            or value.history.source_pdf_sha256 != admission.source_pdf_sha256):
        raise ValueError("M6 private audit belongs to another admission/publication/source")
    projections = history.get("projections")
    if not isinstance(projections, list):
        raise ValueError("M6 history audit lacks source projections")
    rows = [p for p in projections if isinstance(p, dict) and p.get("source_pdf_sha256") == admission.source_pdf_sha256]
    expected = value.history.model_dump(mode="json", exclude={"audit_receipt_sha256"})
    if (len(rows) != 1 or {k: v for k, v in rows[0].items()
                         if k not in {"coverage_gaps", "base_identity_conflicts"}} != expected):
        raise ValueError("M6 history projection differs from the exact audited fact")
    M6SourceHistoryFact.model_validate({**{k: rows[0][k] for k in expected},
                                       "audit_receipt_sha256": value.history.audit_receipt_sha256})
    encoded = audit.get("winner_canonical_json")
    if type(encoded) is not str:
        raise ValueError("M6 publication audit lacks canonical winner bytes")
    winner = decode_atomic_publication_winner_v4(encoded.encode())
    publication = value.publication
    if (winner.sha256 != publication.winner_sha256
            or winner.attempt_id != admission.attempt_id or winner.fence_identity != admission.fence_identity
            or winner.document_id != admission.document_id or winner.processing_run_id != admission.processing_run_id
            or publication.attempt_id != admission.attempt_id or publication.document_id != admission.document_id
            or publication.processing_run_id != admission.processing_run_id
            or publication.source_pdf_sha256 != admission.source_pdf_sha256
            or publication.source_page_count != admission.source_page_count
            or winner.durable_base_commit.source_identity_sha256 != admission.source_pdf_sha256
            or winner.durable_base_commit.source_page_count != admission.source_page_count
            or winner.durable_base_commit.durable_base_sha256 != publication.durable_base_sha256):
        raise ValueError("M6 publication winner does not close admitted identity")
    database = audit["snapshot"]["identity"].get("database_name")
    if type(database) is not str or history["snapshot"]["identity"].get("database_name") != database:
        raise ValueError("M6 private audit database identity differs")
    return winner, database


@dataclass
class _ReadBudget:
    maximum_bytes: int
    maximum_files: int
    bytes_reserved: int = 0
    files_reserved: int = 0

    def reserve(self, byte_count: int, file_count: int = 1) -> None:
        if (type(byte_count) is not int or byte_count < 0 or type(file_count) is not int or file_count < 1
                or self.bytes_reserved + byte_count > self.maximum_bytes
                or self.files_reserved + file_count > self.maximum_files):
            raise ValueError("M6 public artifact read budget exceeded")
        self.bytes_reserved += byte_count
        self.files_reserved += file_count


class _ReadOnlyStore(ImmutableArtifactStore):
    def __init__(self, paths: FileStorePathPort, budget: _ReadBudget) -> None:
        super().__init__(paths)
        self._budget = budget

    def create_or_verify(self, *, relpath: Path, payload: bytes) -> ArtifactWriteResult:
        raise RuntimeError("M6 public verifier cannot write publication artifacts")

    def read_exact(self, *, relpath: Path, expected_sha256: str,
                   expected_byte_count: int, max_byte_count: int) -> bytes:
        self._budget.reserve(expected_byte_count)
        return super().read_exact(relpath=relpath, expected_sha256=expected_sha256,
                                  expected_byte_count=expected_byte_count, max_byte_count=max_byte_count)


class _ReadOnlyOutput(PublishedParserOutputVerifierV4):
    def __init__(self, paths: FileStorePathPort, budget: _ReadBudget) -> None:
        super().__init__(paths)
        self._budget = budget

    def promote_or_replay(self, **kwargs: Any) -> None:
        raise RuntimeError("M6 public verifier cannot promote parser output")

    def verify_published(self, *, published_relpath: str, expected_inventory_sha256: str,
                         expected_file_count: int, expected_byte_count: int) -> None:
        self._budget.reserve(expected_byte_count, expected_file_count)
        super().verify_published(published_relpath=published_relpath, expected_inventory_sha256=expected_inventory_sha256,
                                 expected_file_count=expected_file_count, expected_byte_count=expected_byte_count)


def _source_bytes(paths: FileStorePathPort, relpath: str, admission: M6AttemptAdmitted) -> dict[str, Any]:
    relative = Path(relpath)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("M6 source reference path is invalid")
    with _DirectoryChain(root_path=paths.data_path(Path()), components=relative.parts[:-1],
                         create=False, trip=lambda _event: None) as chain:
        descriptor = os.open(relative.name, _file_flags(), dir_fd=chain.leaf_fd)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_dev != chain.root_device
                    or before.st_uid != os.getuid() or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) & 0o022 or before.st_size != admission.source_byte_count):
                raise ValueError("M6 original source file identity/bytes differ")
            digest, count = hashlib.sha256(), 0
            while chunk := os.read(descriptor, min(1024**2, admission.source_byte_count - count + 1)):
                count += len(chunk)
                if count > admission.source_byte_count:
                    raise ValueError("M6 source grew during public verification")
                digest.update(chunk)
            chain.verify()
            if (count != admission.source_byte_count or "sha256:" + digest.hexdigest() != admission.source_pdf_sha256
                    or _full_identity(before) != _full_identity(os.fstat(descriptor))
                    or _full_identity(before) != _full_identity(os.stat(relative.name, dir_fd=chain.leaf_fd, follow_symlinks=False))):
                raise ValueError("M6 source bytes or path changed during public verification")
        finally:
            os.close(descriptor)
    return {"relpath": relpath, "sha256": admission.source_pdf_sha256, "byte_count": count}


def _expected_rows(ready: AtomicPublicationArtifactsReadyV4) -> tuple[dict[str, Any], ...]:
    rows = []
    for unit, binding in zip(ready.request.units, ready.preparation.unit_bindings, strict=True):
        row = {name: getattr(unit, name) for name in _ROW_FIELDS if name not in {
            "asset_id", "artifact_locator", "payload", "order_index", "heading_path", "semantic_keys", "section_keys",
        }}
        row.update(asset_id=binding.asset_id, order_index=unit.unit_index,
                   heading_path=list(unit.heading_path), payload=strict_json_loads(unit.canonical_payload_json.encode()),
                   artifact_locator=strict_json_loads(unit.canonical_artifact_locator_json.encode()),
                   semantic_keys=None if unit.semantic_keys is None else list(unit.semantic_keys),
                   section_keys=None if unit.section_keys is None else list(unit.section_keys))
        if _digest(_canonical(row)) != binding.final_unit_row_sha256:
            raise ValueError("M6 expected Unit row does not close readiness binding")
        # Migration 0040 defines precisely these two NULL -> [] projections.
        # The sealed request supplies NULL; never guess it back from public [].
        for name in ("semantic_keys", "section_keys"):
            if row[name] is None:
                row[name] = []
        rows.append(row)
    return tuple(rows)


class M6PublicConsumerVerifier:
    def __init__(self, *, engine: Engine, paths: FileStorePathPort,
                 private_audit_for: Callable[[M6AttemptAdmitted], M6PublicAuditInput],
                 receipt_sink: Callable[[bytes], None], verifier_identity: str,
                 page_size: int = 100, maximum_receipt_bytes: int = 32 * 1024**2,
                 maximum_artifact_bytes: int = 2 * 1024**3, maximum_artifact_files: int = 200_000) -> None:
        self._identity = TypeAdapter(M6Id).validate_python(verifier_identity)
        if type(page_size) is not int or not 1 <= page_size <= 500:
            raise ValueError("M6 public page size must be in 1..500")
        if type(maximum_receipt_bytes) is not int or not 1 <= maximum_receipt_bytes <= 64 * 1024**2:
            raise ValueError("M6 public receipt byte limit is invalid")
        if (type(maximum_artifact_bytes) is not int or not 1 <= maximum_artifact_bytes <= 2**63 - 1
                or type(maximum_artifact_files) is not int or not 1 <= maximum_artifact_files <= 1_000_000):
            raise ValueError("M6 public artifact read limits are invalid")
        self._engine, self._paths, self._audits, self._sink = engine, paths, private_audit_for, receipt_sink
        self._page_size, self._max_bytes = page_size, maximum_receipt_bytes
        self._artifact_bytes, self._artifact_files = maximum_artifact_bytes, maximum_artifact_files

    def confirm(self, admission: M6AttemptAdmitted) -> tuple[M6PublicConfirmation, M6SourceHistoryFact]:
        if type(admission) is not M6AttemptAdmitted or admission.document_id is None or admission.processing_run_id is None:
            raise ValueError("M6 public confirmation requires an exact E2E admission")
        inputs = self._audits(admission)
        winner, database = _bind_inputs(admission, inputs)
        if winner.artifact_readiness is None:
            raise ValueError("M6 public confirmation requires a winner bound to immutable artifact readiness")
        budget = _ReadBudget(self._artifact_bytes, self._artifact_files)
        store = _ReadOnlyStore(self._paths, budget)
        verifier = FilesystemAtomicPublicationArtifactReadinessV4(
            paths=self._paths, immutable_store=store,
            output_promotion=_ReadOnlyOutput(self._paths, budget),
        )
        ready = verifier.verify_ready(reference=winner.artifact_readiness, expected_winner=winner)
        expected = _expected_rows(ready)
        envelope_plan = ready.preparation.provider_document_plan
        source_parts = Path(ready.request.upstream_evidence.source_pdf_relpath).parts
        if len(source_parts) != 6:
            raise ValueError("M6 publication source path topology differs from the provider contract")
        # One exact owner per confirmation: the admitted document/run, the
        # sealed provider/provider-document identity, the sealed envelope hash
        # and the admitted source hash. The API-path envelope read happens once
        # for this owner; every public Unit must later bind this same owner.
        owner = ProviderEvidenceOwner(
            document_id=admission.document_id, artifact_owner_processing_run_id=admission.processing_run_id,
            provider=ready.request.upstream_evidence.provider, security_code=source_parts[2],
            provider_document_id=ready.request.identity.provider_document_id,
            provider_document_sha256=envelope_plan.sha256, source_pdf_sha256=admission.source_pdf_sha256,
        )
        budget.reserve(envelope_plan.byte_count)
        scoped = read_provider_envelope(owner=owner, paths=cast(FileStorePathBuilder, self._paths),
                                        expected_byte_count=envelope_plan.byte_count)
        envelope = scoped.envelope
        if (scoped.record_relpath != Path(envelope_plan.relpath) or scoped.byte_count != envelope_plan.byte_count
                or envelope.document_id != admission.document_id
                or envelope.artifact_owner_processing_run_id != admission.processing_run_id
                or envelope.input_raw_file_hash != admission.source_pdf_sha256
                or envelope.source_pdf_page_count != admission.source_page_count
                or envelope.source_pdf_relpath != ready.request.upstream_evidence.source_pdf_relpath
                or envelope.provider != ready.request.upstream_evidence.provider
                or envelope.provider_document_id != ready.request.identity.provider_document_id
                or envelope.parser_artifact_root_relpath != ready.preparation.parser_output_plan.published_relpath):
            raise ValueError("M6 provider envelope/source/owner differs from admission")
        budget.reserve(admission.source_byte_count)
        source = _source_bytes(self._paths, envelope.source_pdf_relpath, admission)
        params = {"document": admission.document_id, "run": admission.processing_run_id,
                  "limit": self._page_size, "after": 0, "after_asset": "", "first_page": True}
        with self._engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            connection.exec_driver_sql("SET LOCAL statement_timeout='15s'")
            identity = require_runtime_reader_connection(connection)
            if identity.database_name != database:
                raise ValueError("M6 public/private database identities differ")
            snapshot = dict(connection.execute(sa.text(
                "SELECT current_setting('transaction_read_only') AS read_only, "
                "current_setting('transaction_isolation') AS isolation, "
                "pg_current_snapshot()::text AS snapshot, transaction_timestamp() AS observed_at"
            )).mappings().one())
            if snapshot["read_only"] != "on" or snapshot["isolation"] != "repeatable read":
                raise ValueError("M6 public snapshot is not read-only repeatable read")
            snapshot["identity"] = asdict(identity)
            try:
                document = dict(connection.execute(sa.text(
                    "SELECT * FROM disclosure_public.documents_v1 WHERE document_id=:document"
                ), params).mappings().one())
                run = dict(connection.execute(sa.text(
                    "SELECT * FROM disclosure_public.processing_runs_v1 WHERE document_id=:document AND processing_run_id=:run"
                ), params).mappings().one())
                if (document["status"] != "published" or document["current_processing_run_id"] != admission.processing_run_id
                        or document["superseded_by_document_id"] is not None or document["raw_file_hash"] != admission.source_pdf_sha256
                        or document["provider"] != ready.request.upstream_evidence.provider
                        or document["provider_document_id"] != ready.request.identity.provider_document_id
                        or document["security_code"] != Path(envelope.source_pdf_relpath).parts[2]
                        or run["is_active"] is not True or run["status"] != "succeeded"
                        or run["run_kind"] != "parse" or run["unit_build_status"] != "succeeded"
                        or run["artifact_owner_processing_run_id"] != admission.processing_run_id
                        or run["input_raw_file_hash"] != admission.source_pdf_sha256
                        or run["artifact_hash"] != ready.request.upstream_evidence.provider_envelope_sha256):
                    raise ValueError("M6 public document/run/source projection differs from publication")
                units: list[dict[str, Any]] = []
                refs: list[dict[str, Any]] = []
                evidence: list[dict[str, Any]] = []
                page_receipts: list[dict[str, Any]] = []
                pages = 0
                byte_count = 0
                while True:
                    page = [dict(row) for row in connection.execute(sa.text(
                        "SELECT * FROM disclosure_public.document_units_v1 "
                        "WHERE document_id=:document AND processing_run_id=:run "
                        "AND (:first_page OR (order_index,asset_id)>(:after,:after_asset)) "
                        "ORDER BY order_index,asset_id LIMIT :limit"
                    ), params).mappings()]
                    pages += 1
                    page_receipts.append({"first_page": params["first_page"], "after": params["after"], "after_asset": params["after_asset"],
                                          "row_count": len(page), "page_sha256": _digest(_canonical(page))})
                    if not page:
                        break
                    for row in page:
                        index = len(units)
                        if (set(row) != _PUBLIC_FIELDS or index >= len(expected)
                                or _canonical({name: row[name] for name in _ROW_FIELDS}) != _canonical(expected[index])
                                or row["is_active_run"] is not True or row["raw_file_hash"] != admission.source_pdf_sha256
                                or row["contract_version"] != "document_unit.v1"):
                            raise ValueError("M6 public Unit inventory/order/payload differs from sealed publication")
                        units.append(row)
                        byte_count += len(_canonical(row))
                    reference_page = [dict(row) for row in connection.execute(sa.text(
                        "SELECT * FROM disclosure_public.source_refs_v1 "
                        "WHERE document_id=:document AND processing_run_id=:run AND asset_id=ANY(:assets) ORDER BY asset_id"
                    ), {**params, "assets": [row["asset_id"] for row in page]}).mappings()]
                    by_asset = {row["asset_id"]: row for row in reference_page}
                    if len(by_asset) != len(reference_page) or set(by_asset) != {row["asset_id"] for row in page}:
                        raise ValueError("M6 public source reference inventory is incomplete")
                    for row in page:
                        ref = by_asset[row["asset_id"]]
                        expected_ref = {name: row[name] for name in (
                            "asset_id", "document_id", "processing_run_id", "is_active_run", "payload_kind",
                            "heading_path", "title", "quality_status", "applicability", "page_no", "artifact_locator",
                        )}
                        expected_ref.update(service="disclosure_anchor", contract_version="source_ref.v1",
                                            source_access_id=document["source_ref"], provider=document["provider"],
                                            provider_document_id=document["provider_document_id"],
                                            raw_file_hash=admission.source_pdf_sha256, unit_content_hash=row["content_hash"])
                        if _canonical(ref) != _canonical(expected_ref):
                            raise ValueError("M6 public source reference differs from its Unit/source")
                        refs.append(ref)
                        byte_count += len(_canonical(ref))
                        metadata = {**row, "provider": document["provider"],
                                    "security_code": document["security_code"],
                                    "artifact_owner_processing_run_id": run["artifact_owner_processing_run_id"],
                                    "resolved_artifact_owner_processing_run_id": run["processing_run_id"],
                                    "artifact_owner_document_id": run["document_id"], "artifact_owner_run_kind": run["run_kind"],
                                    "artifact_hash": run["artifact_hash"], "producer_artifact_hash": run["artifact_hash"],
                                    "producer_input_raw_file_hash": run["input_raw_file_hash"],
                                    "artifact_owner_input_raw_file_hash": run["input_raw_file_hash"]}
                        for reference in unit_evidence_refs(asset_id=row["asset_id"], payload_kind=row["payload_kind"],
                                                            payload=row["payload"], artifact_locator=row["artifact_locator"]):
                            # The existing API resolution binds this Unit's
                            # published owner/locator to the one verified
                            # envelope, then reads and verifies the exact
                            # artifact bytes. Only those bytes are charged.
                            budget.reserve(reference.size_bytes)
                            verified = read_unit_evidence(row=metadata, digest=reference.sha256.removeprefix("sha256:"),
                                                          paths=cast(FileStorePathBuilder, self._paths), envelope=scoped)
                            if (verified is None or verified.sha256 != reference.sha256
                                    or len(verified.content) != reference.size_bytes or verified.media_type != reference.media_type):
                                raise ValueError("M6 public evidence reference does not resolve exact bytes")
                            item = {"asset_id": row["asset_id"], **reference.model_dump(mode="json")}
                            evidence.append(item)
                            byte_count += len(_canonical(item))
                    if byte_count > self._max_bytes:
                        raise ValueError("M6 public evidence exceeds receipt byte bound")
                    params["after"] = page[-1]["order_index"]
                    params["after_asset"] = page[-1]["asset_id"]
                    params["first_page"] = False
                if len(units) != len(expected):
                    raise ValueError("M6 public result omits committed Units")
                # Fixed verification window: the envelope authority read before
                # the snapshot must still be the identical immutable bytes after
                # the last reference resolved, or this confirmation fails.
                store.read_exact(relpath=Path(envelope_plan.relpath), expected_sha256=envelope_plan.sha256,
                                 expected_byte_count=envelope_plan.byte_count, max_byte_count=envelope_plan.byte_count)
                artifact = {"source": source, "readiness_reference": asdict(ready.reference),
                            "resources_sha256": ready.manifest.resources_sha256,
                            "public_evidence": evidence, "provider_document_sha256": envelope_plan.sha256,
                            "provider_document_relpath": envelope_plan.relpath,
                            "provider_document_byte_count": envelope_plan.byte_count}
                artifact_digest = _digest(_canonical(artifact))
                unit_digest = _digest(_canonical(units))
                receipt = _canonical({
                    "contract_version": "m6.public-consumer-audit.v1", "verifier_identity": self._identity,
                    "snapshot": snapshot, "admission": admission.model_dump(mode="json"),
                    "publication": inputs.publication.model_dump(mode="json"), "document": document, "run": run,
                    "units": units, "source_refs": refs, "public_units_sha256": unit_digest,
                    "page_size": self._page_size, "queries_including_empty_tail": pages, "page_receipts": page_receipts,
                    "artifact_closure": artifact, "artifact_closure_sha256": artifact_digest,
                    "artifact_read_budget": asdict(budget),
                    "private_publication_audit_sha256": inputs.publication_audit_sha256,
                    "history_audit_receipt_sha256": inputs.history.audit_receipt_sha256,
                })
                if len(receipt) > self._max_bytes:
                    raise ValueError("M6 public audit exceeds receipt byte bound")
                self._sink(receipt)
                return M6PublicConfirmation(
                    **inputs.publication.model_dump(exclude={"kind"}), public_units_sha256=unit_digest,
                    artifact_closure_sha256=artifact_digest, consumer_check_receipt_sha256=_digest(receipt),
                    history_audit_receipt_sha256=inputs.history.audit_receipt_sha256, verifier_identity=self._identity,
                ), inputs.history
            finally:
                connection.rollback()
