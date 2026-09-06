"""PostgreSQL projection of the ordinary parse backlog for staged V4."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from disclosure_anchor.application.ports.staged_new_work_v4 import (
    V4OrdinaryParseCandidate,
    V4OrdinaryParseCandidatePage,
)
from disclosure_anchor.application.worker.queries import pending_parse


class PostgresV4OrdinaryParseCandidateSource:
    def __init__(
        self,
        *,
        engine: Engine,
        max_retries: int,
        scope_classes: tuple[str, ...] | None,
    ) -> None:
        if not isinstance(engine, Engine):
            raise ValueError("V4 candidate source requires a SQLAlchemy engine")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 1:
            raise ValueError("V4 candidate source retry limit is invalid")
        self._engine = engine
        self._max_retries = max_retries
        self._scope_classes = scope_classes

    def list_candidates(
        self,
        *,
        after_document_id: str | None,
        limit: int,
    ) -> V4OrdinaryParseCandidatePage:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("V4 candidate page limit must be positive")
        with self._engine.connect() as connection:
            rows = pending_parse(
                connection,
                max_retries=self._max_retries,
                limit=limit + 1,
                scope_classes=self._scope_classes,
                require_active_company_scope=True,
                after_document_id=after_document_id,
            )
            visible = rows[:limit]
            if not visible:
                return V4OrdinaryParseCandidatePage(
                    candidates=(),
                    has_more=False,
                )
            document_ids = [str(row["document_id"]) for row in visible]
            facts = connection.execute(
                text(
                    "SELECT d.document_id,d.provider,d.provider_document_id,"
                    "d.security_id,d.raw_file_relpath,d.raw_file_hash,"
                    "s.security_code FROM disclosure_core.document AS d "
                    "JOIN disclosure_core.security AS s "
                    "ON s.security_id=d.security_id "
                    "WHERE d.document_id=ANY(:document_ids)"
                ),
                {"document_ids": document_ids},
            ).mappings()
            by_id = {str(row["document_id"]): row for row in facts}
        candidates: list[V4OrdinaryParseCandidate] = []
        for row in visible:
            document_id = str(row["document_id"])
            exact = by_id.get(document_id)
            if exact is None:
                raise ValueError(
                    f"V4 candidate {document_id} lost document/security authority"
                )
            candidates.append(
                V4OrdinaryParseCandidate(
                    document_id=document_id,
                    provider=exact["provider"],
                    provider_document_id=exact["provider_document_id"],
                    security_id=exact["security_id"],
                    security_code=exact["security_code"],
                    raw_file_relpath=exact["raw_file_relpath"],
                    raw_file_hash=exact["raw_file_hash"],
                    archived_raw_byte_count=row.get("raw_byte_count"),
                )
            )
        return V4OrdinaryParseCandidatePage(
            candidates=tuple(candidates),
            has_more=len(rows) > limit,
        )


__all__ = ["PostgresV4OrdinaryParseCandidateSource"]
