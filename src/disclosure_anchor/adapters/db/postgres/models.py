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
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA, OPS_SCHEMA


class Base(DeclarativeBase):
    pass


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


class Security(Base):
    __tablename__ = "security"
    __table_args__ = (
        UniqueConstraint("security_code", "exchange", name="uq_security_code_exchange"),
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
        {"schema": CORE_SCHEMA},
    )

    tracked_company_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.company.company_id"), nullable=False
    )
    security_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.security.security_id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'active'"))
    lookback: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    filing_categories: Mapped[Optional[list[str]]] = mapped_column(JSONB, nullable=True)
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
    provider_interface: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    dataset_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    query_params: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
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


class Document(Base):
    __tablename__ = "document"
    __table_args__ = (
        Index("ix_document_company", "company_id"),
        Index("ix_document_security", "security_id"),
        Index("ix_document_source_access", "source_access_id"),
        Index("ix_document_provider_ref", "provider", "provider_document_id"),
        Index("ix_document_raw_hash", "raw_file_hash"),
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
    provider_document_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    filing_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    announcement_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    report_period: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    raw_file_relpath: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_file_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # Pointer to the current default run; intentionally not a hard FK to avoid a
    # cycle with processing_run.document_id.
    current_processing_run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    supersedes_document_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    correction_of_document_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProcessingRun(Base):
    __tablename__ = "processing_run"
    __table_args__ = (
        Index("ix_processing_run_document", "document_id"),
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
    run_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    parser_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    parser_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    parser_backend: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    input_raw_file_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    parser_artifact_relpath: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    artifact_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    normalized_ir_relpath: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    document_units_relpath: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_hash_aggregate: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    structure_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DocumentUnit(Base):
    __tablename__ = "document_unit"
    __table_args__ = (
        CheckConstraint(
            "payload_kind in ('text','table','qa')", name="ck_document_unit_payload_kind"
        ),
        UniqueConstraint(
            "processing_run_id", "order_index", name="uq_document_unit_run_order"
        ),
        Index("ix_document_unit_document", "document_id"),
        Index("ix_document_unit_run", "processing_run_id"),
        Index("ix_document_unit_semantic_key", "semantic_key"),
        {"schema": CORE_SCHEMA},
    )

    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.document.document_id"), nullable=False
    )
    processing_run_id: Mapped[str] = mapped_column(
        ForeignKey(f"{CORE_SCHEMA}.processing_run.processing_run_id"), nullable=False
    )
    provider_document_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    payload_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    heading_path: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    semantic_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    structure_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    quality_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'ok'")
    )
    artifact_locator: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_event"
    __table_args__ = (
        Index("ix_outbox_event_document", "document_id"),
        {"schema": OPS_SCHEMA},
    )

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False)
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
