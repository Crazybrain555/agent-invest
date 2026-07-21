"""SQLAlchemy repository implementations.

Each repository adds domain entities into the active session (mapping them to ORM
models) and loads them back as entities. They never commit; the UnitOfWork owns
the transaction boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import json
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres import mappers, models
from disclosure_anchor.adapters.db.postgres.classification_refresh import (
    refresh_document_classification,
)
from disclosure_anchor.application.worker.locks import OUTBOX_NS
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.value_objects import canonical_security_identity
from disclosure_anchor.domain.errors import (
    DocumentIdentityConflictError,
    SubjectIdentityRaceError,
)


class CompanyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, company: e.Company) -> e.Company:
        row = mappers.company_to_model(company)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            detail = str(getattr(exc, "orig", exc))
            if "unified_social_credit_code" in detail:
                raise SubjectIdentityRaceError(
                    "company unified_social_credit_code already exists"
                ) from exc
            raise
        return mappers.company_to_entity(row)

    def get(self, company_id: str) -> Optional[e.Company]:
        row = self._session.get(models.Company, company_id)
        return mappers.company_to_entity(row) if row is not None else None

    def get_by_legal_name(self, legal_name: str) -> Optional[e.Company]:
        row = (
            self._session.query(models.Company)
            .filter(models.Company.legal_name == legal_name)
            .order_by(models.Company.created_at.desc(), models.Company.company_id.desc())
            .first()
        )
        return mappers.company_to_entity(row) if row is not None else None

    def get_by_credit_code(self, uscc: str) -> Optional[e.Company]:
        row = (
            self._session.query(models.Company)
            .filter(models.Company.unified_social_credit_code == uscc)
            .one_or_none()
        )
        return mappers.company_to_entity(row) if row is not None else None

    def update(self, company: e.Company) -> e.Company:
        row = self._session.get(models.Company, company.company_id)
        if row is None:
            raise KeyError(f"company not found: {company.company_id}")
        updated = mappers.company_to_model(company)
        for column in ("legal_name", "unified_social_credit_code"):
            setattr(row, column, getattr(updated, column))
        self._session.flush()
        return mappers.company_to_entity(row)


class CompanyIdentifierRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, identifier: e.CompanyIdentifier) -> e.CompanyIdentifier:
        row = mappers.company_identifier_to_model(identifier)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            detail = str(getattr(exc, "orig", exc))
            if "uq_company_identifier_strong_key" in detail:
                raise SubjectIdentityRaceError(
                    "company identifier strong key already exists"
                ) from exc
            raise
        return mappers.company_identifier_to_entity(row)

    def get(self, identifier_id: str) -> Optional[e.CompanyIdentifier]:
        row = self._session.get(models.CompanyIdentifier, identifier_id)
        return mappers.company_identifier_to_entity(row) if row is not None else None

    def get_by_scheme_value(
        self, scheme: str, normalized_value: str
    ) -> Optional[e.CompanyIdentifier]:
        row = (
            self._session.query(models.CompanyIdentifier)
            .filter(
                models.CompanyIdentifier.scheme == scheme,
                models.CompanyIdentifier.normalized_value == normalized_value,
                models.CompanyIdentifier.status == "active",
            )
            .order_by(
                models.CompanyIdentifier.created_at.desc(),
                models.CompanyIdentifier.identifier_id.desc(),
            )
            .first()
        )
        return mappers.company_identifier_to_entity(row) if row is not None else None

    def update(self, identifier: e.CompanyIdentifier) -> e.CompanyIdentifier:
        row = self._session.get(models.CompanyIdentifier, identifier.identifier_id)
        if row is None:
            raise KeyError(f"company identifier not found: {identifier.identifier_id}")
        updated = mappers.company_identifier_to_model(identifier)
        for column in (
            "company_id",
            "scheme",
            "raw_value",
            "normalized_value",
            "jurisdiction",
            "source_access_id",
            "status",
            "valid_from",
            "valid_to",
            "observed_at",
        ):
            setattr(row, column, getattr(updated, column))
        self._session.flush()
        return mappers.company_identifier_to_entity(row)


class SecurityRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, security: e.Security) -> e.Security:
        row = mappers.security_to_model(security)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            detail = str(getattr(exc, "orig", exc))
            if "uq_security_code_exchange" in detail:
                raise SubjectIdentityRaceError(
                    "security code/exchange already exists"
                ) from exc
            raise
        return mappers.security_to_entity(row)

    def get(self, security_id: str) -> Optional[e.Security]:
        row = self._session.get(models.Security, security_id)
        return mappers.security_to_entity(row) if row is not None else None

    def get_by_code_exchange(self, security_code: str, exchange: str) -> Optional[e.Security]:
        security_code, exchange = canonical_security_identity(security_code, exchange)
        row = (
            self._session.query(models.Security)
            .filter(
                models.Security.security_code == security_code,
                models.Security.exchange == exchange,
            )
            .one_or_none()
        )
        return mappers.security_to_entity(row) if row is not None else None


class TrackedCompanyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, tracked_company: e.TrackedCompany) -> e.TrackedCompany:
        row = mappers.tracked_company_to_model(tracked_company)
        self._session.add(row)
        self._session.flush()
        return mappers.tracked_company_to_entity(row)

    def get(self, tracked_company_id: str) -> Optional[e.TrackedCompany]:
        row = self._session.get(models.TrackedCompany, tracked_company_id)
        return mappers.tracked_company_to_entity(row) if row is not None else None

    def get_by_company_id(self, company_id: str) -> Optional[e.TrackedCompany]:
        row = (
            self._session.query(models.TrackedCompany)
            .filter(models.TrackedCompany.company_id == company_id)
            .one_or_none()
        )
        return mappers.tracked_company_to_entity(row) if row is not None else None

    def list_all(self) -> list[e.TrackedCompany]:
        rows = (
            self._session.query(models.TrackedCompany)
            .order_by(models.TrackedCompany.tracked_company_id)
            .all()
        )
        return [mappers.tracked_company_to_entity(row) for row in rows]

    def update(self, tracked_company: e.TrackedCompany) -> e.TrackedCompany:
        row = self._session.get(models.TrackedCompany, tracked_company.tracked_company_id)
        if row is None:
            raise KeyError(f"tracked company not found: {tracked_company.tracked_company_id}")
        updated = mappers.tracked_company_to_model(tracked_company)
        for column in (
            "company_id",
            "security_id",
            "status",
            "lookback",
            "process_classes",
            "sync_frequency",
        ):
            setattr(row, column, getattr(updated, column))
        self._session.flush()
        return mappers.tracked_company_to_entity(row)

    def delete(self, tracked_company_id: str) -> None:
        row = self._session.get(models.TrackedCompany, tracked_company_id)
        if row is None:
            raise KeyError(f"tracked company not found: {tracked_company_id}")
        self._session.delete(row)
        self._session.flush()


class SourceAccessRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, source_access: e.SourceAccess) -> e.SourceAccess:
        row = mappers.source_access_to_model(source_access)
        self._session.add(row)
        self._session.flush()
        return mappers.source_access_to_entity(row)

    def get(self, source_access_id: str) -> Optional[e.SourceAccess]:
        row = self._session.get(models.SourceAccess, source_access_id)
        return mappers.source_access_to_entity(row) if row is not None else None

    def list_candidate_snapshots(
        self, *, provider: str, provider_interface: str, company_id: str
    ) -> list[dict[str, object]]:
        rows = (
            self._session.query(models.SourceAccess)
            .filter(
                models.SourceAccess.provider == provider,
                models.SourceAccess.provider_interface == provider_interface,
                models.SourceAccess.company_id == company_id,
                models.SourceAccess.status == "ok",
            )
            .order_by(models.SourceAccess.accessed_at.asc())
            .all()
        )
        snapshots: list[dict[str, object]] = []
        for row in rows:
            snapshot = row.result_snapshot
            if isinstance(snapshot, dict) and isinstance(snapshot.get("candidates"), list):
                snapshots.append(snapshot)
        return snapshots

    def list_pending_download_candidates(
        self,
        *,
        provider: str,
        index_interfaces: Sequence[str],
        download_interface: str,
        max_retries: int,
        overlap_start: object,
    ) -> list[dict[str, object]]:
        overlap_date = _coerce_date(overlap_start)
        candidates = _candidate_rows(
            self._session.query(models.SourceAccess)
            .filter(
                models.SourceAccess.provider == provider,
                models.SourceAccess.provider_interface.in_(list(index_interfaces)),
                models.SourceAccess.status == "ok",
            )
            .order_by(models.SourceAccess.accessed_at.asc())
            .all()
        )
        pending: list[dict[str, object]] = []
        seen: set[str] = set()
        for candidate in candidates:
            provider_document_id = candidate.get("provider_document_id")
            if not isinstance(provider_document_id, str) or provider_document_id in seen:
                continue
            seen.add(provider_document_id)
            if self._terminal_download_failure(
                provider=provider,
                download_interface=download_interface,
                provider_document_id=provider_document_id,
                max_retries=max_retries,
            ):
                continue
            document = self._latest_document(
                provider=provider, provider_document_id=provider_document_id
            )
            if _should_download_candidate(
                candidate=candidate,
                document=document,
                overlap_start=overlap_date,
            ):
                pending.append(candidate)
        return pending

    def _terminal_download_failure(
        self,
        *,
        provider: str,
        download_interface: str,
        provider_document_id: str,
        max_retries: int,
    ) -> bool:
        rows = (
            self._session.query(models.SourceAccess)
            .filter(
                models.SourceAccess.provider == provider,
                models.SourceAccess.provider_interface == download_interface,
                models.SourceAccess.status == "failed",
                models.SourceAccess.query_params.op("->>")("provider_document_id")
                == provider_document_id,
            )
            .all()
        )
        if len(rows) >= max_retries:
            return True
        return any(_error_retryable(row.error) is False for row in rows)

    def _latest_document(
        self, *, provider: str, provider_document_id: str
    ) -> Optional[e.Document]:
        row = (
            self._session.query(models.Document)
            .filter(
                models.Document.provider == provider,
                models.Document.provider_document_id == provider_document_id,
            )
            .order_by(models.Document.created_at.desc(), models.Document.document_id.desc())
            .first()
        )
        return mappers.document_to_entity(row) if row is not None else None


def _candidate_rows(rows: list[models.SourceAccess]) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for row in rows:
        snapshot = row.result_snapshot
        if not isinstance(snapshot, dict):
            continue
        raw_candidates = snapshot.get("candidates")
        if not isinstance(raw_candidates, list):
            continue
        candidates.extend(item for item in raw_candidates if isinstance(item, dict))
    return candidates


def _coerce_date(value: object) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"overlap_start must be date or ISO string, got {type(value).__name__}")


def _error_retryable(error: str | None) -> bool | None:
    if not error:
        return None
    try:
        payload = json.loads(error)
    except json.JSONDecodeError:
        return None
    retryable = payload.get("retryable")
    return retryable if isinstance(retryable, bool) else None


def _should_download_candidate(
    *,
    candidate: dict[str, object],
    document: e.Document | None,
    overlap_start: date,
) -> bool:
    if document is None:
        return True
    if _has_correction_signal(candidate.get("title")):
        return True
    signature_state = _signature_state(
        candidate.get("file_signature_hint"),
        document.provider_metadata.get("file_signature"),
    )
    if signature_state == "different":
        return True
    # Inside the overlap verification window a matching signature is not
    # trusted (F005N is KB-granular); re-download and let raw_file_hash decide.
    announcement_date = _candidate_announcement_date(candidate)
    if announcement_date is not None and announcement_date >= overlap_start:
        return True
    return False


def _has_correction_signal(title: object) -> bool:
    if not isinstance(title, str):
        return False
    return any(signal in title for signal in ("更正", "修订", "更新后", "补充", "取消"))


def _candidate_announcement_date(candidate: Mapping[str, object]) -> date | None:
    value = candidate.get("announcement_date")
    if not isinstance(value, str):
        return None
    return date.fromisoformat(value)


def _signature_state(left: object, right: object) -> str:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return "unreliable"
    comparable = False
    for field in ("file_size", "etag", "last_modified"):
        left_value = left.get(field)
        right_value = right.get(field)
        if left_value is None or right_value is None:
            continue
        comparable = True
        if left_value != right_value:
            return "different"
    return "same" if comparable else "unreliable"


class SourceCheckpointRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, checkpoint: e.SourceCheckpoint) -> e.SourceCheckpoint:
        row = mappers.source_checkpoint_to_model(checkpoint)
        self._session.add(row)
        self._session.flush()
        return mappers.source_checkpoint_to_entity(row)

    def get(self, source_checkpoint_id: str) -> Optional[e.SourceCheckpoint]:
        row = self._session.get(models.SourceCheckpoint, source_checkpoint_id)
        return mappers.source_checkpoint_to_entity(row) if row is not None else None

    def get_by_scope(self, provider: str, scope_key: str) -> Optional[e.SourceCheckpoint]:
        row = (
            self._session.query(models.SourceCheckpoint)
            .filter(
                models.SourceCheckpoint.provider == provider,
                models.SourceCheckpoint.scope_key == scope_key,
            )
            .one_or_none()
        )
        return mappers.source_checkpoint_to_entity(row) if row is not None else None

    def update(self, checkpoint: e.SourceCheckpoint) -> e.SourceCheckpoint:
        row = self._session.get(models.SourceCheckpoint, checkpoint.source_checkpoint_id)
        if row is None:
            raise KeyError(f"source checkpoint not found: {checkpoint.source_checkpoint_id}")
        updated = mappers.source_checkpoint_to_model(checkpoint)
        row.provider = updated.provider
        row.scope_key = updated.scope_key
        row.cursor = updated.cursor
        row.updated_at = updated.updated_at
        self._session.flush()
        return mappers.source_checkpoint_to_entity(row)


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, document: e.Document) -> e.Document:
        row = mappers.document_to_model(document)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            detail = str(getattr(exc, "orig", exc))
            if "uq_document_provider_doc_hash" in detail:
                raise DocumentIdentityConflictError(
                    "document provider/document/hash already exists"
                ) from exc
            raise
        # Materialized classification stamps at insert, in the same
        # transaction; rules reloads refresh the rest by stamp mismatch
        # (design: retrieval-scale-hardening.md §3).
        refresh_document_classification(
            self._session.connection(), document_id=row.document_id
        )
        return mappers.document_to_entity(row)

    def get(self, document_id: str) -> Optional[e.Document]:
        row = self._session.get(models.Document, document_id)
        return mappers.document_to_entity(row) if row is not None else None

    def get_for_update(self, document_id: str) -> Optional[e.Document]:
        row = (
            self._session.query(models.Document)
            .filter(models.Document.document_id == document_id)
            .with_for_update()
            .one_or_none()
        )
        return mappers.document_to_entity(row) if row is not None else None

    def update(self, document: e.Document) -> e.Document:
        row = self._session.get(models.Document, document.document_id)
        if row is None:
            raise KeyError(f"document not found: {document.document_id}")
        updated = mappers.document_to_model(document)
        classification_inputs_changed = (
            row.title != updated.title
            or row.provider != updated.provider
            or row.provider_metadata != updated.provider_metadata
        )
        for column in (
            "company_id",
            "security_id",
            "source_access_id",
            "provider",
            "provider_document_id",
            "title",
            "announcement_date",
            "report_period",
            "raw_file_relpath",
            "raw_file_hash",
            "status",
            "provider_metadata",
            "current_processing_run_id",
            "supersedes_document_id",
            "correction_of_document_id",
        ):
            setattr(row, column, getattr(updated, column))
        self._session.flush()
        if classification_inputs_changed:
            # Materialized classification derives from title/provider/
            # metadata; a mutation without a re-stamp would survive under a
            # still-matching stamp forever (the loader only refreshes on
            # stamp mismatch).
            refresh_document_classification(
                self._session.connection(), document_id=row.document_id
            )
        return mappers.document_to_entity(row)

    def get_by_provider_document_and_hash(
        self, *, provider: str, provider_document_id: str, raw_file_hash: str
    ) -> Optional[e.Document]:
        row = (
            self._session.query(models.Document)
            .filter(
                models.Document.provider == provider,
                models.Document.provider_document_id == provider_document_id,
                models.Document.raw_file_hash == raw_file_hash,
            )
            .order_by(models.Document.created_at.desc(), models.Document.document_id.desc())
            .first()
        )
        return mappers.document_to_entity(row) if row is not None else None

    def latest_by_provider_document(
        self, *, provider: str, provider_document_id: str
    ) -> Optional[e.Document]:
        row = (
            self._session.query(models.Document)
            .filter(
                models.Document.provider == provider,
                models.Document.provider_document_id == provider_document_id,
            )
            .order_by(models.Document.created_at.desc(), models.Document.document_id.desc())
            .first()
        )
        return mappers.document_to_entity(row) if row is not None else None


class ProcessingRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, run: e.ProcessingRun) -> e.ProcessingRun:
        row = mappers.processing_run_to_model(run)
        self._session.add(row)
        self._session.flush()
        return mappers.processing_run_to_entity(row)

    def get(self, processing_run_id: str) -> Optional[e.ProcessingRun]:
        row = self._session.get(models.ProcessingRun, processing_run_id)
        return mappers.processing_run_to_entity(row) if row is not None else None

    def latest_succeeded_parse_for_document(
        self, document_id: str
    ) -> Optional[e.ProcessingRun]:
        row = (
            self._session.query(models.ProcessingRun)
            .filter(
                models.ProcessingRun.document_id == document_id,
                # rebuild_units runs copy the parse artifacts references, so
                # they are equally valid rebuild sources (prune-history may
                # have removed the original parse run; provenance chains).
                models.ProcessingRun.run_kind.in_(("parse", "rebuild_units")),
                models.ProcessingRun.status == "succeeded",
                models.ProcessingRun.normalized_ir_relpath.isnot(None),
            )
            .order_by(
                models.ProcessingRun.started_at.desc().nullslast(),
                models.ProcessingRun.processing_run_id.desc(),
            )
            .first()
        )
        return mappers.processing_run_to_entity(row) if row is not None else None

    def update(self, run: e.ProcessingRun) -> e.ProcessingRun:
        row = self._session.get(models.ProcessingRun, run.processing_run_id)
        if row is None:
            raise KeyError(f"processing_run not found: {run.processing_run_id}")
        updated = mappers.processing_run_to_model(run)
        for column in (
            "document_id",
            "run_kind",
            "status",
            "parser_name",
            "parser_version",
            "parser_backend",
            "parser_method",
            "parser_language",
            "input_raw_file_hash",
            "parser_artifact_relpath",
            "artifact_hash",
            "normalized_ir_relpath",
            "document_units_relpath",
            "content_hash_aggregate",
            "structure_hash",
            "builder_rules_version",
            "is_active",
            "unit_build_status",
            "unit_build_error",
            "unit_build_attempt_count",
            "unit_built_at",
            "started_at",
            "finished_at",
            "error",
        ):
            setattr(row, column, getattr(updated, column))
        self._session.flush()
        return mappers.processing_run_to_entity(row)


class DocumentUnitRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, unit: e.DocumentUnit) -> e.DocumentUnit:
        row = mappers.document_unit_to_model(unit)
        self._session.add(row)
        self._session.flush()
        return mappers.document_unit_to_entity(row)

    def add_many(self, units: list[e.DocumentUnit]) -> list[e.DocumentUnit]:
        rows = [mappers.document_unit_to_model(unit) for unit in units]
        self._session.add_all(rows)
        self._session.flush()
        return [mappers.document_unit_to_entity(row) for row in rows]

    def get(self, asset_id: str) -> Optional[e.DocumentUnit]:
        row = self._session.get(models.DocumentUnit, asset_id)
        return mappers.document_unit_to_entity(row) if row is not None else None

    def list_by_processing_run(self, processing_run_id: str) -> list[e.DocumentUnit]:
        rows = (
            self._session.query(models.DocumentUnit)
            .filter(models.DocumentUnit.processing_run_id == processing_run_id)
            .order_by(models.DocumentUnit.order_index, models.DocumentUnit.asset_id)
            .all()
        )
        return [mappers.document_unit_to_entity(row) for row in rows]

    def list_by_document_active(self, document_id: str) -> list[e.DocumentUnit]:
        rows = (
            self._session.query(models.DocumentUnit)
            .join(
                models.ProcessingRun,
                models.DocumentUnit.processing_run_id
                == models.ProcessingRun.processing_run_id,
            )
            .filter(
                models.DocumentUnit.document_id == document_id,
                models.ProcessingRun.is_active.is_(True),
            )
            .order_by(models.DocumentUnit.order_index, models.DocumentUnit.asset_id)
            .all()
        )
        return [mappers.document_unit_to_entity(row) for row in rows]


class OutboxRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, event: e.OutboxEvent) -> e.OutboxEvent:
        # BIGSERIAL assigns seq at INSERT, not at commit: two concurrent
        # publishers can commit out of seq order, and a `seq > cursor`
        # consumer that saw the later seq first would skip the earlier one
        # forever. This xact-scoped advisory lock serializes [first outbox
        # insert .. commit], making committed seq order == commit order
        # (holes come only from rolled-back transactions). Outbox-writing
        # transactions are short DB-only writes, so the serialization cost
        # is negligible at this deployment's write rate (round23).
        self._session.execute(
            sa.text("SELECT pg_advisory_xact_lock(:ns, 0)"), {"ns": OUTBOX_NS}
        )
        row = mappers.outbox_event_to_model(event)
        self._session.add(row)
        self._session.flush()
        return mappers.outbox_event_to_entity(row)

    def get(self, event_id: str) -> Optional[e.OutboxEvent]:
        row = (
            self._session.query(models.OutboxEvent)
            .filter(models.OutboxEvent.event_id == event_id)
            .one_or_none()
        )
        return mappers.outbox_event_to_entity(row) if row is not None else None
