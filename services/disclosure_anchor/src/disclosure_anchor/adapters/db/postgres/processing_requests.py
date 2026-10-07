"""PostgreSQL store and status facts for explicit processing requests (0066).

Writes run inside the caller's UnitOfWork session. Status facts are read in one
read-only transaction through the same queue predicates the worker uses, so a
reported ``*_queued`` state is the scanner's own answer, not a view row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.adapters.db.postgres.schema import (
    CORE_SCHEMA,
    OPS_SCHEMA,
    PUBLIC_SCHEMA,
)
from disclosure_anchor.application.contracts.processing_request import (
    AttemptRecord,
    DocumentFacts,
    DownloadFacts,
    FilingFacts,
    ProcessingFacts,
    ProcessingIntent,
    RequestRecord,
    TargetRecord,
    filing_from_candidate,
)
from disclosure_anchor.application.ports.processing_requests import (
    LocalDocument,
    OriginalLocator,
    PersistedCandidate,
    ProcessingAttemptConflict,
)
from disclosure_anchor.application.worker import queries
from disclosure_anchor.application.worker.locks import (
    PROCESSING_REQUEST_ADMISSION_NS,
    PROCESSING_REQUEST_KEY_NS,
    stable_document_hash,
)


INDEX_INTERFACES = ("cninfo:p_info3015", "cninfo:hisAnnouncement")
DOWNLOAD_INTERFACE = "cninfo:download_pdf"


class PostgresProcessingRequestRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # -- locks ---------------------------------------------------------------
    def lock_idempotency_key(self, idempotency_key: str) -> None:
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :key)"),
            {"ns": PROCESSING_REQUEST_KEY_NS, "key": stable_document_hash(idempotency_key)},
        )

    def lock_request_admission(self) -> None:
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, 0)"),
            {"ns": PROCESSING_REQUEST_ADMISSION_NS},
        )

    # -- requests ------------------------------------------------------------
    def get(self, request_id: str) -> RequestRecord | None:
        row = self._session.get(models.ProcessingRequest, request_id)
        return None if row is None else _request_record(row)

    def get_by_key(self, idempotency_key: str) -> RequestRecord | None:
        row = (
            self._session.query(models.ProcessingRequest)
            .filter(models.ProcessingRequest.idempotency_key == idempotency_key)
            .one_or_none()
        )
        return None if row is None else _request_record(row)

    def count_created_since(self, since: datetime) -> int:
        return int(
            self._session.execute(
                text(
                    f"SELECT count(*) FROM {OPS_SCHEMA}.processing_request "
                    "WHERE created_at >= :since"
                ),
                {"since": since},
            ).scalar_one()
        )

    def add_request(
        self, *, request_id: str, intent: ProcessingIntent, requested_by: str
    ) -> RequestRecord:
        row = models.ProcessingRequest(
            request_id=request_id,
            idempotency_key=intent.idempotency_key,
            intent_sha256=intent.intent_sha256,
            contract_version=str(intent.canonical()["contract_version"]),
            target_kind=intent.target.kind,
            target=intent.target.to_json(),
            enqueue_parse=intent.enqueue_parse,
            requested_by=requested_by,
        )
        self._session.add(row)
        self._session.flush()
        self._session.refresh(row)
        return _request_record(row)

    # -- attempts ------------------------------------------------------------
    def attempts(self, request_id: str) -> list[AttemptRecord]:
        rows = (
            self._session.query(models.ProcessingRequestAttempt)
            .filter(models.ProcessingRequestAttempt.request_id == request_id)
            .order_by(models.ProcessingRequestAttempt.attempt_no)
            .all()
        )
        return [_attempt_record(row) for row in rows]

    def due_discovery_requests(self, *, now: datetime, limit: int) -> list[RequestRecord]:
        """Unresolved provider/title requests whose next attempt is due now.

        Due = never attempted, the latest attempt's lease expired unfinished,
        or its scheduled retry time has come. Oldest due first.
        """

        rows = self._session.execute(
            text(
                f"""
                SELECT r.request_id
                  FROM {OPS_SCHEMA}.processing_request r
                  LEFT JOIN LATERAL (
                        SELECT a.finished_at, a.lease_expires_at, a.next_attempt_at
                          FROM {OPS_SCHEMA}.processing_request_attempt a
                         WHERE a.request_id = r.request_id
                         ORDER BY a.attempt_no DESC
                         LIMIT 1
                       ) latest ON true
                 WHERE r.target_kind IN ('provider_document', 'title_match')
                   AND NOT EXISTS (
                        SELECT 1 FROM {OPS_SCHEMA}.processing_request_target t
                         WHERE t.request_id = r.request_id)
                   AND (latest.lease_expires_at IS NULL
                        OR (latest.finished_at IS NULL
                            AND latest.lease_expires_at <= :now)
                        OR (latest.finished_at IS NOT NULL
                            AND latest.next_attempt_at <= :now))
                 ORDER BY COALESCE(latest.next_attempt_at, latest.lease_expires_at,
                                   r.created_at), r.request_id
                 LIMIT :limit
                """
            ),
            {"now": now, "limit": limit},
        ).scalars()
        records: list[RequestRecord] = []
        for request_id in rows:
            record = self.get(str(request_id))
            if record is not None:
                records.append(record)
        return records

    def start_attempt(
        self,
        *,
        attempt_id: str,
        request_id: str,
        attempt_no: int,
        started_at: datetime,
        lease_expires_at: datetime,
    ) -> AttemptRecord:
        row = models.ProcessingRequestAttempt(
            attempt_id=attempt_id,
            request_id=request_id,
            attempt_no=attempt_no,
            started_at=started_at,
            lease_expires_at=lease_expires_at,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError as exc:
            raise ProcessingAttemptConflict(
                f"processing request {request_id} already has an open attempt"
            ) from exc
        self._session.refresh(row)
        return _attempt_record(row)

    def finish_attempt(
        self,
        *,
        attempt_id: str,
        outcome: str,
        finished_at: datetime,
        next_attempt_at: datetime | None = None,
        profile_source_access_id: str | None = None,
        index_source_access_id: str | None = None,
        provider_row_count: int | None = None,
        choices: Sequence[Mapping[str, object]] | None = None,
        choices_truncated: bool = False,
        error: Mapping[str, object] | None = None,
    ) -> bool:
        if outcome == "abandoned":
            raise ValueError("an abandoned attempt is closed only by abandon_attempt")
        attempt = models.ProcessingRequestAttempt
        updated = (
            self._session.query(attempt)
            .filter(
                attempt.attempt_id == attempt_id,
                attempt.finished_at.is_(None),
                attempt.lease_expires_at > finished_at,
            )
            .update(
                {
                    attempt.finished_at: finished_at,
                    attempt.outcome: outcome,
                    attempt.next_attempt_at: next_attempt_at,
                    attempt.profile_source_access_id: profile_source_access_id,
                    attempt.index_source_access_id: index_source_access_id,
                    attempt.provider_row_count: provider_row_count,
                    attempt.choices: (
                        None if choices is None else [dict(choice) for choice in choices]
                    ),
                    attempt.choices_truncated: choices_truncated,
                    attempt.error: None if error is None else dict(error),
                },
                synchronize_session=False,
            )
        )
        return int(updated) == 1

    def abandon_attempt(
        self,
        *,
        attempt_id: str,
        finished_at: datetime,
        next_attempt_at: datetime | None,
        error: Mapping[str, object],
    ) -> bool:
        attempt = models.ProcessingRequestAttempt
        updated = (
            self._session.query(attempt)
            .filter(
                attempt.attempt_id == attempt_id,
                attempt.finished_at.is_(None),
                attempt.lease_expires_at <= finished_at,
            )
            .update(
                {
                    attempt.finished_at: finished_at,
                    attempt.outcome: "abandoned",
                    attempt.next_attempt_at: next_attempt_at,
                    attempt.error: dict(error),
                },
                synchronize_session=False,
            )
        )
        return int(updated) == 1

    # -- target --------------------------------------------------------------
    def target(self, request_id: str) -> TargetRecord | None:
        row = (
            self._session.query(models.ProcessingRequestTarget)
            .filter(models.ProcessingRequestTarget.request_id == request_id)
            .one_or_none()
        )
        return None if row is None else _target_record(row)

    def add_target(self, *, request_id: str, target: TargetRecord) -> TargetRecord:
        row = models.ProcessingRequestTarget(
            target_id=target.target_id,
            request_id=request_id,
            resolution_method=target.resolution_method,
            attempt_id=target.attempt_id,
            provider=target.provider,
            provider_document_id=target.provider_document_id,
            document_id=target.document_id,
            raw_file_hash=target.raw_file_hash,
            index_source_access_id=target.index_source_access_id,
            admit_download=target.admit_download,
            admit_parse=target.admit_parse,
        )
        self._session.add(row)
        self._session.flush()
        return _target_record(row)

    # -- local identity reads -------------------------------------------------
    def document(self, document_id: str) -> LocalDocument | None:
        return _local_document(
            self._session.connection(),
            "d.document_id = :document_id",
            {"document_id": document_id},
        )

    def latest_provider_document(
        self, *, provider: str, provider_document_id: str
    ) -> LocalDocument | None:
        return _local_document(
            self._session.connection(),
            "d.provider = :provider AND d.provider_document_id = :provider_document_id",
            {"provider": provider, "provider_document_id": provider_document_id},
        )

    def persisted_candidate(self, provider_document_id: str) -> PersistedCandidate | None:
        return _persisted_candidate(self._session.connection(), provider_document_id)


class PostgresProcessingStatusReader:
    """One read-only snapshot of every fact a request status is derived from."""

    def __init__(
        self,
        engine: Engine,
        *,
        max_download_retries: int,
        max_parse_retries: int,
        max_build_retries: int,
        scope_classes: tuple[str, ...] | None,
    ) -> None:
        self._engine = engine
        self._max_download_retries = max_download_retries
        self._max_parse_retries = max_parse_retries
        self._max_build_retries = max_build_retries
        self._scope_classes = scope_classes

    def read(self, request_id: str, *, observed_at: datetime) -> ProcessingFacts | None:
        with self._engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            try:
                return self._read(conn, request_id, observed_at=observed_at)
            finally:
                conn.rollback()

    def _read(
        self, conn: Connection, request_id: str, *, observed_at: datetime
    ) -> ProcessingFacts | None:
        request_row = (
            conn.execute(
                text(
                    "SELECT request_id, idempotency_key, intent_sha256, target, "
                    "enqueue_parse, created_at "
                    f"FROM {OPS_SCHEMA}.processing_request WHERE request_id = :request_id"
                ),
                {"request_id": request_id},
            )
            .mappings()
            .one_or_none()
        )
        if request_row is None:
            return None
        request = RequestRecord(
            request_id=request_row["request_id"],
            idempotency_key=request_row["idempotency_key"],
            intent_sha256=request_row["intent_sha256"],
            target=dict(request_row["target"]),
            enqueue_parse=bool(request_row["enqueue_parse"]),
            created_at=request_row["created_at"],
        )
        attempts = [
            _attempt_from_mapping(row)
            for row in conn.execute(
                text(
                    "SELECT attempt_id, attempt_no, started_at, lease_expires_at, "
                    "finished_at, next_attempt_at, outcome, "
                    "profile_source_access_id, index_source_access_id, "
                    "provider_row_count, choices, choices_truncated, error "
                    f"FROM {OPS_SCHEMA}.processing_request_attempt "
                    "WHERE request_id = :request_id ORDER BY attempt_no"
                ),
                {"request_id": request_id},
            ).mappings()
        ]
        target_row = (
            conn.execute(
                text(
                    "SELECT target_id, resolution_method, attempt_id, provider, "
                    "provider_document_id, document_id, raw_file_hash, "
                    "index_source_access_id, admit_download, admit_parse "
                    f"FROM {OPS_SCHEMA}.processing_request_target "
                    "WHERE request_id = :request_id"
                ),
                {"request_id": request_id},
            )
            .mappings()
            .one_or_none()
        )
        target = None if target_row is None else _target_from_mapping(target_row)
        facts = ProcessingFacts(
            request=request,
            attempts=tuple(attempts),
            target=target,
            observed_at=observed_at,
            max_download_retries=self._max_download_retries,
        )
        if target is None:
            return facts
        document = self._document_facts(conn, target)
        if document is not None:
            return ProcessingFacts(
                request=request,
                attempts=tuple(attempts),
                target=target,
                observed_at=observed_at,
                max_download_retries=self._max_download_retries,
                document=document,
            )
        candidate_filing = self._candidate_filing(conn, target)
        download = self._download_facts(conn, target)
        return ProcessingFacts(
            request=request,
            attempts=tuple(attempts),
            target=target,
            observed_at=observed_at,
            max_download_retries=self._max_download_retries,
            candidate_filing=candidate_filing,
            download=download,
        )

    def _document_facts(
        self, conn: Connection, target: TargetRecord
    ) -> DocumentFacts | None:
        if target.document_id is not None:
            where, params = "d.document_id = :document_id", {"document_id": target.document_id}
        elif target.provider is not None and target.provider_document_id is not None:
            # The requested candidate has been downloaded since the request.
            where = "d.provider = :provider AND d.provider_document_id = :provider_document_id"
            params = {
                "provider": target.provider,
                "provider_document_id": target.provider_document_id,
            }
        else:
            return None
        row = (
            conn.execute(
                text(
                    "SELECT d.document_id, d.status, d.raw_file_hash, d.provider, "
                    "d.provider_document_id, d.title, d.announcement_date, "
                    "d.current_processing_run_id, s.security_code, s.exchange, "
                    "(SELECT r.processing_run_id FROM "
                    f"{CORE_SCHEMA}.processing_run r WHERE r.document_id = d.document_id "
                    "AND r.status = 'running' ORDER BY r.started_at DESC, "
                    "r.processing_run_id DESC LIMIT 1) AS running_processing_run_id "
                    f"FROM {CORE_SCHEMA}.document d "
                    f"LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id "
                    f"WHERE {where} "
                    "ORDER BY d.created_at DESC, d.document_id DESC LIMIT 1"
                ),
                params,
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        document_id = str(row["document_id"])
        selected = bool(
            queries.pending_parse(
                conn,
                max_retries=self._max_parse_retries,
                limit=1,
                scope_classes=self._scope_classes,
                require_active_company_scope=True,
                document_ids=(document_id,),
            )
        )
        diagnosis = queries.parse_admission_diagnosis(
            conn,
            document_id=document_id,
            max_retries=self._max_parse_retries,
            scope_classes=self._scope_classes,
            require_active_company_scope=True,
        )
        status = str(row["status"])
        post_parse_selected = (
            queries.post_parse_selected(
                conn, document_id=document_id, max_retries=self._max_build_retries
            )
            if status == "parsed"
            else False
        )
        return DocumentFacts(
            document_id=document_id,
            status=status,
            raw_file_hash=row["raw_file_hash"],
            filing=FilingFacts(
                provider=row["provider"],
                provider_document_id=row["provider_document_id"],
                security_code=row["security_code"],
                exchange=row["exchange"],
                title=row["title"],
                announcement_date=row["announcement_date"],
            ),
            current_processing_run_id=row["current_processing_run_id"],
            running_processing_run_id=row["running_processing_run_id"],
            parse_selected=selected,
            retry=dict(diagnosis["remaining_blockers"]),
            post_parse_selected=post_parse_selected,
        )

    def _candidate_filing(
        self, conn: Connection, target: TargetRecord
    ) -> FilingFacts | None:
        if target.index_source_access_id is None or target.provider_document_id is None:
            return None
        row = (
            conn.execute(
                text(
                    "SELECT c AS candidate "
                    f"FROM {CORE_SCHEMA}.source_access sa, "
                    "jsonb_array_elements(sa.result_snapshot->'candidates') AS c "
                    "WHERE sa.source_access_id = :source_access_id "
                    "AND c->>'provider_document_id' = :provider_document_id LIMIT 1"
                ),
                {
                    "source_access_id": target.index_source_access_id,
                    "provider_document_id": target.provider_document_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None or not isinstance(row["candidate"], Mapping):
            return None
        return filing_from_candidate(row["candidate"])

    def _download_facts(self, conn: Connection, target: TargetRecord) -> DownloadFacts:
        provider_document_id = target.provider_document_id
        if provider_document_id is None:
            return DownloadFacts(selected=False)
        selected = bool(
            queries.pending_downloads(
                conn,
                max_retries=self._max_download_retries,
                limit=1,
                scope_classes=self._scope_classes,
                provider_document_ids=(provider_document_id,),
            )
        )
        failures = (
            conn.execute(
                text(
                    "SELECT count(*) AS failed_count, "
                    "(SELECT f.error FROM "
                    f"{CORE_SCHEMA}.source_access f WHERE f.provider = 'cninfo' "
                    "AND f.provider_interface = :download_interface "
                    "AND f.status = 'failed' "
                    "AND f.query_params->>'provider_document_id' = :provider_document_id "
                    "ORDER BY f.accessed_at DESC, f.source_access_id DESC LIMIT 1) "
                    "AS last_error, "
                    "(SELECT max(f.accessed_at) FROM "
                    f"{CORE_SCHEMA}.source_access f WHERE f.provider = 'cninfo' "
                    "AND f.provider_interface = :download_interface "
                    "AND f.status = 'failed' "
                    "AND f.query_params->>'provider_document_id' = :provider_document_id) "
                    "AS last_failed_at, "
                    "EXISTS (SELECT 1 FROM "
                    f"{OPS_SCHEMA}.download_failure_resolution_v1 nf "
                    "WHERE nf.provider_document_id = :provider_document_id "
                    "AND nf.nonretryable AND nf.resolved_by_source_access_id IS NULL) "
                    "AS nonretryable_unresolved "
                    f"FROM {CORE_SCHEMA}.source_access sa "
                    "WHERE sa.provider = 'cninfo' "
                    "AND sa.provider_interface = :download_interface "
                    "AND sa.status = 'failed' "
                    "AND sa.query_params->>'provider_document_id' = :provider_document_id"
                ),
                {
                    "download_interface": DOWNLOAD_INTERFACE,
                    "provider_document_id": provider_document_id,
                },
            )
            .mappings()
            .one()
        )
        return DownloadFacts(
            selected=selected,
            failed_download_count=int(failures["failed_count"]),
            nonretryable_unresolved=bool(failures["nonretryable_unresolved"]),
            last_failure=_last_failure(failures["last_error"], failures["last_failed_at"]),
        )


class PostgresOriginalLocatorReader:
    """Private raw locator of one registered document plus its public refs.

    The relpath is read from the owning core row with the app engine and never
    leaves the service; citation refs come from the public view.
    """

    def __init__(self, app_engine: Engine, reader_engine: Engine) -> None:
        self._app_engine = app_engine
        self._reader_engine = reader_engine

    def locate(self, document_id: str) -> OriginalLocator | None:
        with self._app_engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            try:
                row = (
                    conn.execute(
                        text(
                            "SELECT d.document_id, d.status, d.raw_file_hash, "
                            "d.raw_file_relpath, d.provider, d.provider_document_id, "
                            "d.title, d.announcement_date, s.security_code, s.exchange "
                            f"FROM {CORE_SCHEMA}.document d "
                            f"LEFT JOIN {CORE_SCHEMA}.security s "
                            "ON s.security_id = d.security_id "
                            "WHERE d.document_id = :document_id"
                        ),
                        {"document_id": document_id},
                    )
                    .mappings()
                    .one_or_none()
                )
            finally:
                conn.rollback()
        if row is None:
            return None
        with self._reader_engine.connect() as conn:
            refs = (
                conn.execute(
                    text(
                        "SELECT source_ref, company_ref, security_ref "
                        f"FROM {PUBLIC_SCHEMA}.documents_v1 WHERE document_id = :document_id"
                    ),
                    {"document_id": document_id},
                )
                .mappings()
                .one_or_none()
            )
            conn.rollback()
        return OriginalLocator(
            document_id=row["document_id"],
            status=row["status"],
            raw_file_hash=row["raw_file_hash"],
            raw_file_relpath=row["raw_file_relpath"],
            provider=row["provider"],
            provider_document_id=row["provider_document_id"],
            security_code=row["security_code"],
            exchange=row["exchange"],
            title=row["title"],
            announcement_date=row["announcement_date"],
            source_ref=None if refs is None else refs["source_ref"],
            company_ref=None if refs is None else refs["company_ref"],
            security_ref=None if refs is None else refs["security_ref"],
        )


def _local_document(
    conn: Connection, where: str, params: Mapping[str, object]
) -> LocalDocument | None:
    row = (
        conn.execute(
            text(
                "SELECT d.document_id, d.status, d.raw_file_hash, d.provider, "
                "d.provider_document_id, d.announcement_date, s.security_code, s.exchange "
                f"FROM {CORE_SCHEMA}.document d "
                f"LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id "
                f"WHERE {where} ORDER BY d.created_at DESC, d.document_id DESC LIMIT 1"
            ),
            dict(params),
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return LocalDocument(
        document_id=row["document_id"],
        status=row["status"],
        raw_file_hash=row["raw_file_hash"],
        provider=row["provider"],
        provider_document_id=row["provider_document_id"],
        security_code=row["security_code"],
        exchange=row["exchange"],
        announcement_date=row["announcement_date"],
    )


def _persisted_candidate(
    conn: Connection, provider_document_id: str
) -> PersistedCandidate | None:
    # Same snapshot preference as ops.pending_download_v1: coded candidate
    # first, then the newest index access.
    row = (
        conn.execute(
            text(
                "SELECT sa.source_access_id, c AS candidate "
                f"FROM {CORE_SCHEMA}.source_access sa, "
                "jsonb_array_elements(sa.result_snapshot->'candidates') AS c "
                "WHERE sa.provider = 'cninfo' "
                "AND sa.provider_interface = ANY(:index_interfaces) "
                "AND sa.status = 'ok' "
                "AND c->>'provider_document_id' = :provider_document_id "
                "ORDER BY (CASE WHEN COALESCE(c->>'raw_category', '') <> '' "
                "THEN 0 ELSE 1 END), sa.accessed_at DESC LIMIT 1"
            ),
            {
                "index_interfaces": list(INDEX_INTERFACES),
                "provider_document_id": provider_document_id,
            },
        )
        .mappings()
        .one_or_none()
    )
    if row is None or not isinstance(row["candidate"], Mapping):
        return None
    return PersistedCandidate(
        index_source_access_id=row["source_access_id"], candidate=dict(row["candidate"])
    )


def _last_failure(error: object, accessed_at: object) -> dict[str, object] | None:
    if error is None:
        return None
    payload: Mapping[str, Any] = {}
    if isinstance(error, Mapping):
        payload = error
    elif isinstance(error, str):
        try:
            decoded = json.loads(error)
        except ValueError:
            decoded = {}
        if isinstance(decoded, Mapping):
            payload = decoded
    retryable = payload.get("retryable")
    code = payload.get("error_code")
    return {
        "error_code": code if isinstance(code, str) else None,
        "retryable": retryable if isinstance(retryable, bool) else None,
        "accessed_at": accessed_at.isoformat() if isinstance(accessed_at, datetime) else None,
    }


def _request_record(row: models.ProcessingRequest) -> RequestRecord:
    return RequestRecord(
        request_id=row.request_id,
        idempotency_key=row.idempotency_key,
        intent_sha256=row.intent_sha256,
        target=dict(row.target),
        enqueue_parse=bool(row.enqueue_parse),
        created_at=row.created_at,
    )


def _attempt_record(row: models.ProcessingRequestAttempt) -> AttemptRecord:
    return AttemptRecord(
        attempt_id=row.attempt_id,
        attempt_no=row.attempt_no,
        started_at=row.started_at,
        lease_expires_at=row.lease_expires_at,
        finished_at=row.finished_at,
        next_attempt_at=row.next_attempt_at,
        outcome=row.outcome,
        profile_source_access_id=row.profile_source_access_id,
        index_source_access_id=row.index_source_access_id,
        provider_row_count=row.provider_row_count,
        choices=None if row.choices is None else tuple(dict(item) for item in row.choices),
        choices_truncated=bool(row.choices_truncated),
        error=None if row.error is None else dict(row.error),
    )


def _attempt_from_mapping(row: Mapping[Any, Any]) -> AttemptRecord:
    choices = row["choices"]
    return AttemptRecord(
        attempt_id=row["attempt_id"],
        attempt_no=int(row["attempt_no"]),
        started_at=row["started_at"],
        lease_expires_at=row["lease_expires_at"],
        finished_at=row["finished_at"],
        next_attempt_at=row["next_attempt_at"],
        outcome=row["outcome"],
        profile_source_access_id=row["profile_source_access_id"],
        index_source_access_id=row["index_source_access_id"],
        provider_row_count=row["provider_row_count"],
        choices=None if choices is None else tuple(dict(item) for item in choices),
        choices_truncated=bool(row["choices_truncated"]),
        error=None if row["error"] is None else dict(row["error"]),
    )


def _target_record(row: models.ProcessingRequestTarget) -> TargetRecord:
    return TargetRecord(
        target_id=row.target_id,
        resolution_method=row.resolution_method,
        admit_download=bool(row.admit_download),
        admit_parse=bool(row.admit_parse),
        attempt_id=row.attempt_id,
        provider=row.provider,
        provider_document_id=row.provider_document_id,
        document_id=row.document_id,
        raw_file_hash=row.raw_file_hash,
        index_source_access_id=row.index_source_access_id,
    )


def _target_from_mapping(row: Mapping[Any, Any]) -> TargetRecord:
    return TargetRecord(
        target_id=row["target_id"],
        resolution_method=row["resolution_method"],
        admit_download=bool(row["admit_download"]),
        admit_parse=bool(row["admit_parse"]),
        attempt_id=row["attempt_id"],
        provider=row["provider"],
        provider_document_id=row["provider_document_id"],
        document_id=row["document_id"],
        raw_file_hash=row["raw_file_hash"],
        index_source_access_id=row["index_source_access_id"],
    )


__all__ = [
    "DOWNLOAD_INTERFACE",
    "INDEX_INTERFACES",
    "PostgresOriginalLocatorReader",
    "PostgresProcessingRequestRepository",
    "PostgresProcessingStatusReader",
    "ProcessingAttemptConflict",
]
