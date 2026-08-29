"""SQLAlchemy ORM models for the disclosure_anchor core/ops schemas.

The ORM metadata is the single source of truth for table structure; the initial
Alembic migration creates these tables from this metadata, then adds public
views and grants that ORM cannot express.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA, OPS_SCHEMA


class Base(DeclarativeBase):
    pass


class RemoteParseAttempt(Base):
    __tablename__ = "remote_parse_attempt"
    __table_args__ = (
        CheckConstraint("attempt_generation >= 1 AND row_version >= 0", name="ck_remote_parse_attempt_versions"),
        CheckConstraint("source_pdf_sha256 ~ '^sha256:[0-9a-f]{64}$' AND parser_target_sha256 ~ '^sha256:[0-9a-f]{64}$' AND request_sha256 ~ '^sha256:[0-9a-f]{64}$' AND runtime_epoch_sha256 ~ '^sha256:[0-9a-f]{64}$'", name="ck_remote_parse_attempt_hashes"),
        CheckConstraint("state IN ('prepared','submitted','remote_terminal','materializing','local_materialized','finish_committed','acked','remote_failed','local_failed','superseded')", name="ck_remote_parse_attempt_state"),
        CheckConstraint("(state IN ('remote_terminal','materializing','local_materialized','finish_committed','acked') AND remote_task_identity IS NOT NULL AND terminal_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND terminal_receipt_bytes IS NOT NULL AND terminal_receipt_byte_count = octet_length(terminal_receipt_bytes) AND terminal_receipt_byte_count > 0 AND result_owner_identity IS NOT NULL AND result_artifact_sha256 ~ '^sha256:[0-9a-f]{64}$' AND result_artifact_bytes > 0) OR (state NOT IN ('remote_terminal','materializing','local_materialized','finish_committed','acked') AND terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL)", name="ck_remote_parse_attempt_terminal_shape"),
        ForeignKeyConstraint(
            ["processing_run_id", "document_id", "source_pdf_sha256"],
            [
                f"{CORE_SCHEMA}.processing_run.processing_run_id",
                f"{CORE_SCHEMA}.processing_run.document_id",
                f"{CORE_SCHEMA}.processing_run.input_raw_file_hash",
            ],
            name="fk_remote_parse_attempt_run_owner",
            ondelete="CASCADE",
        ),
        Index("uq_remote_parse_attempt_current_document", "document_id", unique=True, postgresql_where=text("is_current")),
        Index("ix_remote_parse_attempt_recovery", "state", "updated_at", "attempt_id"),
        {"schema": OPS_SCHEMA},
    )
    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    processing_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    document_id: Mapped[str] = mapped_column(ForeignKey(f"{CORE_SCHEMA}.document.document_id", ondelete="CASCADE"), nullable=False)
    attempt_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    fence_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    source_pdf_sha256: Mapped[str] = mapped_column(String(71), nullable=False)
    parser_target_sha256: Mapped[str] = mapped_column(String(71), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(71), nullable=False)
    runtime_epoch_sha256: Mapped[str] = mapped_column(String(71), nullable=False)
    client_submit_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    remote_task_identity: Mapped[Optional[str]] = mapped_column(String(1024))
    terminal_receipt_sha256: Mapped[Optional[str]] = mapped_column(String(71))
    terminal_receipt_bytes: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    terminal_receipt_byte_count: Mapped[Optional[int]] = mapped_column(Integer)
    result_owner_identity: Mapped[Optional[str]] = mapped_column(String(1024))
    result_artifact_sha256: Mapped[Optional[str]] = mapped_column(String(71))
    result_artifact_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class RemoteParseResumeSecret(Base):
    __tablename__ = "remote_parse_resume_secret"
    __table_args__ = (
        CheckConstraint("secret_kind IN ('submission','terminal','ack')", name="ck_remote_parse_resume_secret_kind"),
        CheckConstraint("token_sha256 ~ '^sha256:[0-9a-f]{64}$' AND token_byte_count = octet_length(token_bytes) AND token_byte_count BETWEEN 1 AND 65536", name="ck_remote_parse_resume_secret_identity"),
        {"schema": OPS_SCHEMA},
    )
    attempt_id: Mapped[str] = mapped_column(ForeignKey(f"{OPS_SCHEMA}.remote_parse_attempt.attempt_id", ondelete="CASCADE"), primary_key=True)
    secret_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    token_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    token_sha256: Mapped[str] = mapped_column(String(71), nullable=False)
    token_byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Company(Base):
    __tablename__ = "company"
    __table_args__ = {"schema": CORE_SCHEMA}

    company_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    legal_name: Mapped[str] = mapped_column(Text, nullable=False)
    unified_social_credit_code: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CompanyIdentifier(Base):
    __tablename__ = "company_identifier"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','retired','contested')",
            name="ck_company_identifier_status",
        ),
        Index(
            "uq_company_identifier_strong_key",
            "scheme",
            "normalized_value",
            unique=True,
            postgresql_where=text(
                "scheme IN ('uscc','lei','sec_cik','hk_cr') AND status='active'"
            ),
        ),
        Index("ix_company_identifier_company", "company_id"),
        {"schema": CORE_SCHEMA},
    )

    identifier_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.company.company_id"), nullable=False
    )
    scheme: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(128), nullable=False)
    jurisdiction: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    source_access_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.source_access.source_access_id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'active'")
    )
    valid_from: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


_PYTHON_STRIP_CHARS_SQL = (
    r"U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F"
    r"\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005"
    r"\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000'"
)


class Security(Base):
    __tablename__ = "security"
    __table_args__ = (
        UniqueConstraint("security_code", "exchange", name="uq_security_code_exchange"),
        CheckConstraint(
            f"security_code = btrim(security_code, {_PYTHON_STRIP_CHARS_SQL})",
            name="ck_security_code_canonical",
        ),
        CheckConstraint(
            f"exchange = upper(btrim(exchange, {_PYTHON_STRIP_CHARS_SQL}))",
            name="ck_security_exchange_canonical",
        ),
        CheckConstraint(
            "exchange NOT IN ('SSE', 'SZSE', 'BSE') OR ("
            "security_code ~ '^[0-9]{6}$' AND CASE "
            "WHEN security_code LIKE '92%' OR security_code LIKE '4%' "
            "  OR security_code LIKE '8%' THEN exchange = 'BSE' "
            "WHEN security_code LIKE '6%' OR security_code LIKE '9%' "
            "  THEN exchange = 'SSE' "
            "WHEN security_code LIKE '0%' OR security_code LIKE '2%' "
            "  OR security_code LIKE '3%' THEN exchange = 'SZSE' "
            "ELSE false END)",
            name="ck_security_mainland_exchange_code",
        ),
        {"schema": CORE_SCHEMA},
    )

    security_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.company.company_id"), nullable=False, index=True
    )
    security_code: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    board: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TrackedCompany(Base):
    __tablename__ = "tracked_company"
    __table_args__ = (
        UniqueConstraint("company_id", name="uq_tracked_company_company"),
        Index("ix_tracked_company_security", "security_id"),
        CheckConstraint(
            "status IN ('active','paused')",
            name="ck_tracked_company_status",
        ),
        {"schema": CORE_SCHEMA},
    )

    tracked_company_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.company.company_id"), nullable=False
    )
    security_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.security.security_id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'active'")
    )
    lookback: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    process_classes: Mapped[Optional[list[str]]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    sync_frequency: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SourceAccess(Base):
    __tablename__ = "source_access"
    __table_args__ = (
        Index("ix_source_access_provider", "provider"),
        Index("ix_source_access_company", "company_id"),
        Index("ix_source_access_security", "security_id"),
        {"schema": CORE_SCHEMA},
    )

    source_access_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_interface: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    dataset_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    query_params: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    company_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.company.company_id"), nullable=True
    )
    security_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.security.security_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SourceCheckpoint(Base):
    __tablename__ = "source_checkpoint"
    __table_args__ = (
        UniqueConstraint("provider", "scope_key", name="uq_source_checkpoint_scope"),
        {"schema": CORE_SCHEMA},
    )

    source_checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(256), nullable=False)
    cursor: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProviderCategory(Base):
    """Provider-native announcement classification dictionary (round3 P1#6).

    Seeded from the p_info3005 snapshot by migration 0012; F006V segments on
    document.provider_metadata join against this dimension via the
    document_categories_v1 public view.
    """

    __tablename__ = "provider_category"
    __table_args__ = {"schema": CORE_SCHEMA}

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    category_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    parent_category_code: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    category_name: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    valid_to: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Document(Base):
    __tablename__ = "document"
    __table_args__ = (
        CheckConstraint(
            "status IN ('registered','parsed','parse_failed','published')",
            name="ck_document_status",
        ),
        Index("ix_document_company", "company_id"),
        Index("ix_document_security", "security_id"),
        Index("ix_document_source_access", "source_access_id"),
        Index("ix_document_provider_ref", "provider", "provider_document_id"),
        Index("ix_document_raw_hash", "raw_file_hash"),
        Index("ix_document_company_period", "company_id", "report_period"),
        Index("ix_document_announcement_date", "announcement_date"),
        Index(
            "uq_document_provider_doc_hash",
            "provider",
            "provider_document_id",
            "raw_file_hash",
            unique=True,
            postgresql_where=text(
                "provider IS NOT NULL "
                "AND provider_document_id IS NOT NULL "
                "AND raw_file_hash IS NOT NULL"
            ),
        ),
        {"schema": CORE_SCHEMA},
    )

    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.company.company_id"), nullable=True
    )
    security_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.security.security_id"), nullable=True
    )
    source_access_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.source_access.source_access_id"), nullable=True
    )
    provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provider_document_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    announcement_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    report_period: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    raw_file_relpath: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_file_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Pointer to the current default run; intentionally not a hard FK to avoid a
    # cycle with processing_run.document_id.
    current_processing_run_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    supersedes_document_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    correction_of_document_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    # Materialized classification (0027): stamped at insert and refreshed by
    # the rules loader on stamp mismatch; the public views read these instead
    # of recomputing the classification per row per read.
    class_filing_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    class_market: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    class_rules_version: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
    class_disclosure_topics: Mapped[Optional[list[Any]]] = mapped_column(
        JSONB, nullable=True
    )
    class_publisher_categories: Mapped[Optional[list[Any]]] = mapped_column(
        JSONB, nullable=True
    )
    class_content_categories: Mapped[Optional[list[Any]]] = mapped_column(
        JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProcessingRun(Base):
    __tablename__ = "processing_run"
    __table_args__ = (
        UniqueConstraint(
            "processing_run_id",
            "document_id",
            "input_raw_file_hash",
            name="uq_processing_run_remote_attempt_owner",
        ),
        CheckConstraint(
            "unit_build_status IN ('not_started','running','succeeded','failed')",
            name="ck_processing_run_unit_build_status",
        ),
        CheckConstraint(
            "parser_target_identity IS NULL "
            "OR jsonb_typeof(parser_target_identity) = 'object'",
            name="ck_processing_run_parser_target_identity",
        ),
        CheckConstraint(
            "search_projection_error IS NULL OR ("
            "jsonb_typeof(search_projection_error) = 'object' "
            "AND COALESCE(search_projection_error->>'stage' = "
            "'search_projection', false) "
            "AND COALESCE(search_projection_error->'retryable' = "
            "'false'::jsonb, false) "
            "AND NULLIF(btrim(search_projection_error->>'error_code'), '') "
            "IS NOT NULL "
            "AND NULLIF(btrim("
            "search_projection_error->>'retrieval_rules_version'), '') "
            "IS NOT NULL)",
            name="ck_processing_run_search_projection_error",
        ),
        CheckConstraint(
            "run_kind <> 'parse' OR "
            "artifact_owner_processing_run_id = processing_run_id",
            name="ck_processing_run_parse_artifact_owner",
        ),
        CheckConstraint(
            "run_kind <> 'rebuild_units' OR "
            "artifact_owner_processing_run_id <> processing_run_id",
            name="ck_processing_run_rebuild_artifact_owner",
        ),
        CheckConstraint(
            "(run_kind NOT IN ('parse', 'rebuild_units') OR "
            "num_nonnulls(normalized_ir_relpath, provider_document_relpath) = 1) "
            "AND (provider_document_relpath IS NULL OR "
            "run_kind IN ('parse', 'rebuild_units'))",
            name="ck_processing_run_primary_output_exactly_one",
        ),
        CheckConstraint(
            "semantic_route_receipts_hash IS NULL OR "
            "(semantic_route_receipts_hash ~ '^sha256:[0-9a-f]{64}$' AND "
            "document_units_relpath IS NOT NULL)",
            name="ck_processing_run_semantic_receipt_hash",
        ),
        CheckConstraint(
            "(semantic_route_receipts_relpath IS NULL AND "
            "semantic_route_receipts_contract_version IS NULL) OR ("
            "semantic_route_receipts_relpath IS NOT NULL AND "
            "semantic_route_receipts_contract_version = "
            "'semantic_route_receipt.v2' AND "
            "semantic_route_receipts_hash IS NOT NULL)",
            name="ck_processing_run_semantic_receipt_locator",
        ),
        CheckConstraint(
            "semantic_adjudication_status IS NULL OR "
            "semantic_adjudication_status IN ('not_required','complete_primary',"
            "'complete_backup','degraded_unavailable','failed_closed')",
            name="ck_processing_run_semantic_adjudication_status",
        ),
        CheckConstraint(
            "semantic_degraded_unit_count IS NULL OR "
            "semantic_degraded_unit_count >= 0",
            name="ck_processing_run_semantic_degraded_count",
        ),
        CheckConstraint(
            "semantic_failover_group_count IS NULL OR "
            "semantic_failover_group_count >= 0",
            name="ck_processing_run_semantic_failover_count",
        ),
        CheckConstraint(
            "semantic_adjudication_summary IS NULL OR "
            "jsonb_typeof(semantic_adjudication_summary) = 'object'",
            name="ck_processing_run_semantic_summary",
        ),
        CheckConstraint(
            "error IS NULL OR (jsonb_typeof(error) = 'object' AND "
            "error ?& ARRAY['stage','error_code','retryable'] AND "
            "jsonb_typeof(error->'retryable') = 'boolean')",
            name="ck_processing_run_error_object",
        ),
        CheckConstraint(
            "unit_build_error IS NULL OR ("
            "jsonb_typeof(unit_build_error) = 'object' AND "
            "unit_build_error ?& ARRAY['stage','error_code','retryable'] AND "
            "jsonb_typeof(unit_build_error->'retryable') = 'boolean')",
            name="ck_processing_run_unit_build_error_object",
        ),
        Index("ix_processing_run_document", "document_id"),
        Index(
            "ix_processing_run_artifact_owner",
            "artifact_owner_processing_run_id",
        ),
        Index(
            "uq_processing_run_one_active_per_document",
            "document_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        {"schema": CORE_SCHEMA},
    )

    processing_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.document.document_id"), nullable=False
    )
    artifact_owner_processing_run_id: Mapped[str] = mapped_column(
        ForeignKey(
            f"{CORE_SCHEMA}.processing_run.processing_run_id",
            name="fk_processing_run_artifact_owner",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=False,
    )
    run_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    parser_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    parser_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    parser_backend: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    parser_method: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    parser_language: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    parser_target_identity: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    search_projection_error: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    input_raw_file_hash: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    parser_artifact_relpath: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    artifact_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    normalized_ir_relpath: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provider_document_relpath: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    document_units_relpath: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    semantic_route_receipts_hash: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    semantic_route_receipts_relpath: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    semantic_route_receipts_contract_version: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    semantic_adjudication_status: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    semantic_degraded_unit_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    semantic_failover_group_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    semantic_adjudication_summary: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    content_hash_aggregate: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    structure_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    builder_rules_version: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    unit_build_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'not_started'")
    )
    unit_build_error: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    unit_build_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    unit_built_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DocumentUnit(Base):
    __tablename__ = "document_unit"
    __table_args__ = (
        CheckConstraint(
            "payload_kind in ('text','table','qa','mixed')",
            name="ck_document_unit_payload_kind",
        ),
        CheckConstraint(
            "quality_status IN ('ok','needs_review','unusable')",
            name="ck_document_unit_quality_status",
        ),
        CheckConstraint(
            "applicability IN ('applicable','not_applicable')",
            name="ck_document_unit_applicability",
        ),
        CheckConstraint(
            "semantic_keys IS NULL OR ("
            "jsonb_typeof(semantic_keys) = 'array' "
            "AND jsonb_array_length(semantic_keys) BETWEEN 1 AND 8)",
            name="ck_document_unit_semantic_keys",
        ),
        UniqueConstraint(
            "processing_run_id", "order_index", name="uq_document_unit_run_order"
        ),
        Index("ix_document_unit_document", "document_id"),
        Index("ix_document_unit_run", "processing_run_id"),
        Index(
            "ix_document_unit_semantic_keys",
            "semantic_keys",
            postgresql_using="gin",
            postgresql_where=text("semantic_keys IS NOT NULL"),
        ),
        CheckConstraint(
            "section_keys IS NULL OR ("
            "jsonb_typeof(section_keys) = 'array' "
            "AND jsonb_array_length(section_keys) > 0)",
            name="ck_document_unit_section_keys",
        ),
        Index(
            "ix_document_unit_section_keys",
            "section_keys",
            postgresql_using="gin",
            postgresql_where=text("section_keys IS NOT NULL"),
        ),
        Index(
            "ix_document_unit_run_order",
            "document_id",
            "processing_run_id",
            "order_index",
            "asset_id",
        ),
        Index("ix_document_unit_content_hash", "content_hash"),
        Index(
            "ix_document_unit_heading_path",
            "heading_path",
            postgresql_using="gin",
            postgresql_ops={"heading_path": "jsonb_path_ops"},
        ),
        {"schema": CORE_SCHEMA},
    )

    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.document.document_id"), nullable=False
    )
    processing_run_id: Mapped[str] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.processing_run.processing_run_id"), nullable=False
    )
    provider_document_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    payload_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    heading_path: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Python None is the deliberate "no semantic claim" state and must bind
    # as SQL NULL.  JSON ``null`` would fail the scalar/array pairing CHECK
    # while looking deceptively null through JSON-oriented clients.
    semantic_keys: Mapped[Optional[list[str]]] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    section_keys: Mapped[Optional[list[str]]] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    structure_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    quality_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'ok'")
    )
    applicability: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    page_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    query_projection_hash: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    artifact_locator: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_event"
    __table_args__ = (
        CheckConstraint(
            "change_kind IN ('observed','materialized')",
            name="ck_outbox_event_change_kind",
        ),
        CheckConstraint(
            "subject_kind IN ('document','processing_run','document_unit','source_access')",
            name="ck_outbox_event_subject_kind",
        ),
        Index("ix_outbox_event_document", "document_id"),
        {"schema": OPS_SCHEMA},
    )

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    change_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    document_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    processing_run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    asset_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# Weighted tsvector persisted mirror of migration 0028.  Safe rows retain the
# original A/B/C/D vector.  Bodies that PostgreSQL cannot represent losslessly
# move to ``unit_body_search_window`` and the parent stores A/B/D only.
_SEARCH_TSV_EXPRESSION = (
    "CASE WHEN body_search_windowed THEN "
    "setweight(to_tsvector('simple', title_tokens), 'A') || "
    "setweight(to_tsvector('simple', path_tokens), 'B') || "
    "setweight(to_tsvector('simple', key_tokens), 'D') "
    "ELSE "
    "setweight(to_tsvector('simple', title_tokens), 'A') || "
    "setweight(to_tsvector('simple', path_tokens), 'B') || "
    "setweight(to_tsvector('simple', body_tokens), 'C') || "
    "setweight(to_tsvector('simple', key_tokens), 'D') "
    "END"
)
_BODY_SEARCH_TSV_EXPRESSION = "setweight(to_tsvector('simple', body_tokens), 'C')"
_ROW_SEARCH_TSV_EXPRESSION = "setweight(to_tsvector('simple', row_tokens), 'C')"


class UnitSearchProjection(Base):
    """06R derived retrieval projection (U7): 1:1 with ``document_unit``.

    Every column regenerates deterministically from the persisted unit via the
    pinned application-side jieba tokenizer; nothing here enters content /
    query-projection hashes and rebuilds emit no outbox events. Created by
    migration 0025 with a matching ``pg_trgm`` GIN pair on the raw
    title/breadcrumb strings; this ORM mirror exists so the build use case can
    read units and upsert the projection through the same metadata.
    """

    __tablename__ = "unit_search_projection"
    __table_args__ = (
        CheckConstraint(
            "row_atom_count >= 0 AND "
            "row_atom_manifest_hash ~ '^sha256:[0-9a-f]{64}$' AND "
            "((row_atom_manifest_ready = false AND row_atom_count = 0 AND "
            "row_atom_manifest_hash = "
            "'sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5"
            "ed12ab4d8e11ba873c2f11161202b945') OR "
            "(row_atom_manifest_ready = true AND "
            "((row_atom_count = 0 AND row_atom_manifest_hash = "
            "'sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5"
            "ed12ab4d8e11ba873c2f11161202b945') OR "
            "(row_atom_count > 0 AND row_atom_manifest_hash <> "
            "'sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5"
            "ed12ab4d8e11ba873c2f11161202b945')))",
            name="ck_unit_search_projection_row_atom_manifest",
        ),
        CheckConstraint(
            f"{CORE_SCHEMA}.search_tsvector_is_safe("
            "title_tokens, path_tokens, "
            "CASE WHEN body_search_windowed THEN '' ELSE body_tokens END, "
            "key_tokens)",
            name="ck_unit_search_projection_tsv_safe",
        ),
        Index(
            "ix_unit_search_projection_tsv",
            "search_tsv",
            postgresql_using="gin",
        ),
        Index(
            "ix_unit_search_projection_title_trgm",
            "title_text",
            postgresql_using="gin",
            postgresql_ops={"title_text": "gin_trgm_ops"},
        ),
        Index(
            "ix_unit_search_projection_path_trgm",
            "heading_path_text",
            postgresql_using="gin",
            postgresql_ops={"heading_path_text": "gin_trgm_ops"},
        ),
        {"schema": CORE_SCHEMA},
    )

    asset_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.document_unit.asset_id", ondelete="CASCADE"),
        primary_key=True,
    )
    retrieval_rules_version: Mapped[str] = mapped_column(String(64), nullable=False)
    title_text: Mapped[str] = mapped_column(Text, nullable=False)
    heading_path_text: Mapped[str] = mapped_column(Text, nullable=False)
    title_tokens: Mapped[str] = mapped_column(Text, nullable=False)
    path_tokens: Mapped[str] = mapped_column(Text, nullable=False)
    body_tokens: Mapped[str] = mapped_column(Text, nullable=False)
    key_tokens: Mapped[str] = mapped_column(Text, nullable=False)
    header_row_candidate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    body_search_windowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    row_atom_manifest_ready: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    row_atom_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    row_atom_manifest_hash: Mapped[str] = mapped_column(
        String(71),
        nullable=False,
        server_default=text(
            "'sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5"
            "ed12ab4d8e11ba873c2f11161202b945'"
        ),
    )
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR(),
        Computed(_SEARCH_TSV_EXPRESSION, persisted=True),
        nullable=False,
    )


class UnitBodySearchWindow(Base):
    """Lossless C-weight body fragment for a windowed unit projection."""

    __tablename__ = "unit_body_search_window"
    __table_args__ = (
        CheckConstraint(
            "window_index >= 0",
            name="ck_unit_body_search_window_index",
        ),
        CheckConstraint(
            "body_token_start >= 0 AND body_token_end > body_token_start",
            name="ck_unit_body_search_window_range",
        ),
        CheckConstraint(
            "btrim(body_tokens) <> ''",
            name="ck_unit_body_search_window_body",
        ),
        CheckConstraint(
            f"{CORE_SCHEMA}.search_tsvector_is_safe('', '', body_tokens, '')",
            name="ck_unit_body_search_window_tsv_safe",
        ),
        Index(
            "ix_unit_body_search_window_tsv",
            "search_tsv",
            postgresql_using="gin",
        ),
        {"schema": CORE_SCHEMA},
    )

    asset_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{CORE_SCHEMA}.unit_search_projection.asset_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    window_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    body_token_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    body_token_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    body_tokens: Mapped[str] = mapped_column(Text, nullable=False)
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR(),
        Computed(_BODY_SEARCH_TSV_EXPRESSION, persisted=True),
        nullable=False,
    )


class UnitSearchAtom(Base):
    """One normalized leaf from an explicit source-bound search target."""

    __tablename__ = "unit_search_atom"
    __table_args__ = (
        CheckConstraint(
            "atom_index >= 0",
            name="ck_unit_search_atom_index",
        ),
        CheckConstraint(
            "btrim(atom_text) <> ''",
            name="ck_unit_search_atom_text",
        ),
        Index(
            "ix_unit_search_atom_text_trgm",
            "atom_text",
            postgresql_using="gin",
            postgresql_ops={"atom_text": "gin_trgm_ops"},
        ),
        {"schema": CORE_SCHEMA},
    )

    asset_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{CORE_SCHEMA}.unit_search_projection.asset_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    atom_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    atom_text: Mapped[str] = mapped_column(Text, nullable=False)


class UnitSearchRowAtom(Base):
    """One source-bound Q&A table row, derived without changing Unit identity."""

    __tablename__ = "unit_search_row_atom"
    __table_args__ = (
        CheckConstraint(
            "row_atom_index >= 0 AND source_row_index >= 0",
            name="ck_unit_search_row_atom_indices",
        ),
        CheckConstraint(
            "btrim(table_target_id) <> '' AND btrim(row_text) <> '' "
            "AND btrim(row_tokens) <> ''",
            name="ck_unit_search_row_atom_text",
        ),
        CheckConstraint(
            f"{CORE_SCHEMA}.search_tsvector_is_safe('', '', row_tokens, '')",
            name="ck_unit_search_row_atom_tsv_safe",
        ),
        CheckConstraint(
            "row_atom_manifest_hash ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_unit_search_row_atom_manifest_hash",
        ),
        Index(
            "ix_unit_search_row_atom_tsv",
            "search_tsv",
            postgresql_using="gin",
        ),
        {"schema": CORE_SCHEMA},
    )

    asset_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{CORE_SCHEMA}.unit_search_projection.asset_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    row_atom_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_target_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    row_text: Mapped[str] = mapped_column(Text, nullable=False)
    row_tokens: Mapped[str] = mapped_column(Text, nullable=False)
    row_atom_manifest_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR(),
        Computed(_ROW_SEARCH_TSV_EXPRESSION, persisted=True),
        nullable=False,
    )
