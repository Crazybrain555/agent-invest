"""Private, read-only M6 publication facts; no owner stamps or public grants.

The caller supplies an evidence sink which persists the exact canonical bytes
before returning. These facts are one input to the separate public consumer,
not a claim that public pagination, artifact verification or M6 ran.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.adapters.db.postgres.atomic_document_publisher_v4 import (
    PostgresAtomicWholeDocumentPublisherV4,
)
from disclosure_anchor.adapters.db.postgres.connection import require_runtime_app_connection
from disclosure_anchor.adapters.db.postgres.remote_parse_v4_repository import RemoteParseV4Repository
from disclosure_anchor.application.contracts.m6_run import M6SourceHistoryFact
from disclosure_anchor.application.contracts.m6_run_events import M6AttemptAdmitted, M6PublicationCommitted

_MAX_ROWS = 100_000
_MAX_RECEIPT_BYTES = 16 * 1024**2
_SOURCE_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


def _source_valid(value: object) -> bool:
    return type(value) is str and _SOURCE_PATTERN.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class PublishedAttemptFact:
    publication: M6PublicationCommitted
    audit_receipt_sha256: str


def _sources(sources: tuple[str, ...]) -> None:
    if (type(sources) is not tuple or not 1 <= len(sources) <= 10_000
            or any(not _source_valid(s) for s in sources)
            or sources != tuple(sorted(set(sources)))):
        raise ValueError("M6 history requires bounded sorted unique source SHA identities")


def _json_default(value: object) -> str:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).isoformat()
    raise TypeError("unsupported private evidence value: " + type(value).__name__)


class PostgresM6PublishVerifier:
    def __init__(self, *, engine: Engine, receipt_sink: Callable[[bytes], None]) -> None:
        self._engine, self._receipt_sink = engine, receipt_sink

    @contextmanager
    def _snapshot(self) -> Iterator[tuple[Session, dict[str, Any]]]:
        with self._engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            connection.exec_driver_sql("SET LOCAL statement_timeout='15s'")
            identity = require_runtime_app_connection(connection)
            info = dict(connection.execute(sa.text(
                "SELECT current_setting('transaction_read_only') AS read_only, "
                "current_setting('transaction_isolation') AS isolation, "
                "pg_current_snapshot()::text AS snapshot, transaction_timestamp() AS observed_at"
            )).mappings().one())
            if info["read_only"] != "on" or info["isolation"] != "repeatable read":
                raise ValueError("M6 private audit snapshot is not read-only repeatable read")
            info["identity"] = asdict(identity)
            try:
                with Session(bind=connection, autoflush=False) as session:
                    yield session, info
            finally:
                connection.rollback()

    def _record(self, value: dict[str, Any]) -> str:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False, default=_json_default).encode()
        if len(raw) > _MAX_RECEIPT_BYTES:
            raise ValueError("M6 private audit receipt exceeds its byte bound")
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        self._receipt_sink(raw)  # Failure propagates: never return an orphan hash.
        return digest

    @staticmethod
    def _rows(session: Session, sql: str, sources: tuple[str, ...]) -> list[dict[str, Any]]:
        result = session.execute(sa.text(sql), {"sources": list(sources), "limit": _MAX_ROWS + 1})
        rows = [dict(row) for row in result.mappings()]
        if len(rows) > _MAX_ROWS:
            raise ValueError("M6 source-global history exceeds bounded audit capacity")
        return rows

    def first_ledger_for(self, sources: tuple[str, ...]) -> tuple[M6SourceHistoryFact, ...]:
        _sources(sources)
        with self._snapshot() as (session, snapshot):
            bases = self._rows(session, _BASE_HISTORY, sources)
            witnesses = self._rows(session, _PUBLICATION_WITNESSES, sources)
            unknown = int(session.execute(sa.text(_UNATTRIBUTED_PUBLICATIONS)).scalar_one())
            by_run = {row["processing_run_id"]: row for row in bases}
            bases_by_source: dict[str, list[dict[str, Any]]] = {source: [] for source in sources}
            witnesses_by_source: dict[str, list[dict[str, Any]]] = {source: [] for source in sources}
            for row in bases:
                bases_by_source[row["source_identity_sha256"]].append(row)
            for row in witnesses:
                attributed = {row["run_source"], row["payload_source"]}
                if not _source_valid(row["run_source"]) or row["witness_kind"] == "published_document":
                    attributed.add(row["document_source"])
                for source in attributed.intersection(witnesses_by_source):
                    witnesses_by_source[source].append(row)
            projections: list[dict[str, Any]] = []
            for source in sources:
                selected = bases_by_source[source]
                relevant = witnesses_by_source[source]
                gaps = []
                for row in relevant:
                    base = by_run.get(row["processing_run_id"])
                    if (base is None or base["source_identity_sha256"] != source
                            or base["document_id"] != row["document_id"]
                            or row["run_source"] != source
                            or (row["witness_kind"] == "published_document"
                                and row["document_source"] != source)
                            or row["payload_source"] not in {None, source}):
                        gaps.append(row)
                malformed = [row for row in selected if row["run_source"] != source
                             or row["run_document_id"] != row["document_id"]]
                first = selected[0] if selected else None  # SQL orders the complete source history.
                projections.append({
                    "source_pdf_sha256": source,
                    "scan_complete": not (gaps or malformed or unknown),
                    "first_processing_run_id": None if first is None else first["processing_run_id"],
                    "first_ledger_seq": None if first is None else first["ledger_seq"],
                    "first_source_page_count": None if first is None else first["source_page_count"],
                    "source_page_variants": len({row["source_page_count"] for row in selected}),
                    "coverage_gaps": gaps, "base_identity_conflicts": malformed,
                })
            digest = self._record({
                "contract_version": "m6.private-source-history-audit.v1", "snapshot": snapshot,
                "sources": sources, "base_rows": bases, "publication_witnesses": witnesses,
                "unattributed_publication_count": unknown, "projections": projections,
                "query_sha256": "sha256:" + hashlib.sha256(
                    (_BASE_HISTORY + _PUBLICATION_WITNESSES + _UNATTRIBUTED_PUBLICATIONS).encode(),
                ).hexdigest(),
            })
            return tuple(M6SourceHistoryFact(
                **{k: v for k, v in row.items() if k not in {"coverage_gaps", "base_identity_conflicts"}},
                audit_receipt_sha256=digest,
            ) for row in projections)

    def read_publication(self, admission: M6AttemptAdmitted) -> PublishedAttemptFact | None:
        if (type(admission) is not M6AttemptAdmitted or admission.document_id is None
                or admission.processing_run_id is None):
            raise ValueError("M6 publication audit requires an exact E2E admission")
        with self._snapshot() as (session, snapshot):
            stored = RemoteParseV4Repository(session).read_publication_snapshot(admission.attempt_id)
            checkpoint = stored.checkpoint
            for name in ("attempt_id", "fence_identity", "document_id", "processing_run_id",
                         "source_pdf_sha256", "source_byte_count", "source_page_count", "process_profile_sha256"):
                if getattr(checkpoint, name) != getattr(admission, name):
                    raise ValueError("M6 admission differs from stored publication " + name)
            if stored.winner is None:
                contradicted = session.execute(sa.text(_PUBLICATION_WITHOUT_WINNER), {
                    "run_id": admission.processing_run_id,
                }).scalar_one()
                if contradicted:
                    raise ValueError("M6 publication witnesses exist without their V4 winner")
                self._record({"contract_version": "m6.private-publication-audit.v1", "snapshot": snapshot,
                              "admission": admission.model_dump(mode="json"),
                              "checkpoint_sha256": checkpoint.sha256, "publication": None})
                return None
            winner, intent = stored.winner, stored.materialization_intent
            if intent is None:
                raise ValueError("M6 publication materialization is absent")
            PostgresAtomicWholeDocumentPublisherV4.verify_publication_snapshot(
                session, winner=winner, context=intent.provider_envelope_context,
            )
            base = session.get(models.DurablePublishBase, winner.processing_run_id)
            if base is None or base.source_identity_sha256 != admission.source_pdf_sha256 or base.source_page_count != admission.source_page_count:
                raise ValueError("M6 publication base differs from admitted source")
            publication = M6PublicationCommitted(
                attempt_id=admission.attempt_id, processing_run_id=winner.processing_run_id,
                document_id=winner.document_id, source_pdf_sha256=base.source_identity_sha256,
                source_page_count=base.source_page_count, ledger_seq=base.ledger_seq,
                winner_sha256=winner.sha256, durable_base_sha256=winner.durable_base_commit.durable_base_sha256,
            )
            digest = self._record({
                "contract_version": "m6.private-publication-audit.v1", "snapshot": snapshot,
                "admission": admission.model_dump(mode="json"), "checkpoint_sha256": checkpoint.sha256,
                "publication": publication.model_dump(mode="json"),
                "winner_canonical_json": winner.canonical_bytes.decode(),
                "published_event_id": winner.outbox_commit.processing_run_published_event_id,
            })
            return PublishedAttemptFact(publication, digest)


_BASE_HISTORY = """
SELECT b.*, r.input_raw_file_hash AS run_source, r.document_id AS run_document_id
FROM disclosure_ops.durable_publish_base b
LEFT JOIN disclosure_core.processing_run r USING(processing_run_id)
WHERE b.source_identity_sha256 = ANY(:sources)
ORDER BY b.ledger_seq LIMIT :limit
"""

# Every query is source-global. A current document hash is only a conservative
# attribution for missing run provenance, never a replacement for that provenance.
_PUBLICATION_WITNESSES = """
WITH witnesses AS (
 SELECT r.processing_run_id, r.document_id, r.input_raw_file_hash AS run_source,
        NULL::text AS payload_source, d.raw_file_hash AS document_source, 'run' AS witness_kind
 FROM disclosure_core.processing_run r JOIN disclosure_core.document d USING(document_id)
 WHERE (r.input_raw_file_hash = ANY(:sources)
        OR ((r.input_raw_file_hash IS NULL OR r.input_raw_file_hash !~ '^sha256:[0-9a-f]{64}$')
            AND d.raw_file_hash = ANY(:sources)))
 AND (d.current_processing_run_id=r.processing_run_id OR r.is_active
      OR EXISTS(SELECT 1 FROM disclosure_core.document_unit u WHERE u.processing_run_id=r.processing_run_id)
      OR EXISTS(SELECT 1 FROM disclosure_ops.atomic_publication_winner_v4 w WHERE w.processing_run_id=r.processing_run_id))
 UNION ALL
 SELECT e.processing_run_id,e.document_id,r.input_raw_file_hash,
        e.payload->>'source_identity',d.raw_file_hash,'outbox'
 FROM disclosure_ops.outbox_event e
 LEFT JOIN disclosure_core.processing_run r ON r.processing_run_id=e.processing_run_id
 LEFT JOIN disclosure_core.document d ON d.document_id=e.document_id
 WHERE e.event_kind='processing_run_published'
 AND (e.payload->>'source_identity'=ANY(:sources) OR r.input_raw_file_hash=ANY(:sources)
      OR ((r.input_raw_file_hash IS NULL OR r.input_raw_file_hash !~ '^sha256:[0-9a-f]{64}$')
          AND d.raw_file_hash=ANY(:sources)))
 UNION ALL
 SELECT d.current_processing_run_id,d.document_id,r.input_raw_file_hash,
        NULL::text,d.raw_file_hash,'published_document'
 FROM disclosure_core.document d
 LEFT JOIN disclosure_core.processing_run r ON r.processing_run_id=d.current_processing_run_id
 WHERE d.status='published'
 AND (d.raw_file_hash=ANY(:sources) OR r.input_raw_file_hash=ANY(:sources))
)
SELECT * FROM witnesses ORDER BY document_id,processing_run_id,witness_kind LIMIT :limit
"""

_UNATTRIBUTED_PUBLICATIONS = """
SELECT count(*) FROM (
 SELECT e.processing_run_id FROM disclosure_ops.outbox_event e
 LEFT JOIN disclosure_core.processing_run r ON r.processing_run_id=e.processing_run_id
 LEFT JOIN disclosure_core.document d ON d.document_id=e.document_id
 WHERE e.event_kind='processing_run_published'
 AND (r.input_raw_file_hash IS NULL OR r.input_raw_file_hash !~ '^sha256:[0-9a-f]{64}$'
      OR r.document_id IS DISTINCT FROM e.document_id)
 AND (e.payload->>'source_identity' IS NULL
      OR e.payload->>'source_identity' !~ '^sha256:[0-9a-f]{64}$'
      OR r.processing_run_id IS NULL
      OR r.document_id IS DISTINCT FROM e.document_id)
 AND NOT EXISTS(SELECT 1 FROM disclosure_ops.durable_publish_base b
                WHERE b.processing_run_id=e.processing_run_id AND b.document_id=e.document_id)
 UNION ALL
 SELECT d.current_processing_run_id FROM disclosure_core.document d
 LEFT JOIN disclosure_core.processing_run r ON r.processing_run_id=d.current_processing_run_id
 WHERE d.status='published'
 AND (r.input_raw_file_hash IS NULL OR r.input_raw_file_hash !~ '^sha256:[0-9a-f]{64}$'
      OR r.document_id IS DISTINCT FROM d.document_id)
 AND NOT EXISTS(SELECT 1 FROM disclosure_ops.durable_publish_base b
                WHERE b.processing_run_id=d.current_processing_run_id AND b.document_id=d.document_id)
 AND NOT EXISTS(SELECT 1 FROM disclosure_ops.outbox_event e
                WHERE e.processing_run_id=d.current_processing_run_id AND e.document_id=d.document_id
                AND r.document_id=d.document_id AND e.event_kind='processing_run_published'
                AND e.payload->>'source_identity' ~ '^sha256:[0-9a-f]{64}$')
 UNION ALL
 SELECT r.processing_run_id FROM disclosure_core.processing_run r
 LEFT JOIN disclosure_core.document d USING(document_id)
 WHERE (r.input_raw_file_hash IS NULL OR r.input_raw_file_hash !~ '^sha256:[0-9a-f]{64}$')
 AND NOT EXISTS(SELECT 1 FROM disclosure_ops.durable_publish_base b
                WHERE b.processing_run_id=r.processing_run_id AND b.document_id=r.document_id)
 AND NOT EXISTS(SELECT 1 FROM disclosure_ops.outbox_event e
                WHERE e.processing_run_id=r.processing_run_id AND e.document_id=r.document_id
                AND e.event_kind='processing_run_published'
                AND e.payload->>'source_identity' ~ '^sha256:[0-9a-f]{64}$')
 AND (r.is_active OR d.current_processing_run_id=r.processing_run_id
      OR EXISTS(SELECT 1 FROM disclosure_core.document_unit u WHERE u.processing_run_id=r.processing_run_id)
      OR EXISTS(SELECT 1 FROM disclosure_ops.atomic_publication_winner_v4 w WHERE w.processing_run_id=r.processing_run_id))
) unknown_witnesses
"""

_PUBLICATION_WITHOUT_WINNER = """
SELECT EXISTS (
 SELECT 1 FROM disclosure_ops.durable_publish_base WHERE processing_run_id=:run_id
 UNION ALL
 SELECT 1 FROM disclosure_ops.outbox_event
 WHERE processing_run_id=:run_id AND event_kind='processing_run_published'
 UNION ALL
 SELECT 1 FROM disclosure_core.document WHERE current_processing_run_id=:run_id
 UNION ALL
 SELECT 1 FROM disclosure_core.processing_run WHERE processing_run_id=:run_id AND is_active
 UNION ALL
 SELECT 1 FROM disclosure_core.document_unit WHERE processing_run_id=:run_id
)
"""
