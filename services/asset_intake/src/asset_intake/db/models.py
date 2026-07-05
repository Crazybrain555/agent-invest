"""Live SQLAlchemy Core table definitions for the intake_* schemas.

These mirror what migrations create. Migration files keep their own frozen
snapshots (never import this module) so future edits here cannot change what an
old revision creates on a fresh database.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB

from asset_intake.db.schema import CORE_SCHEMA, OPS_SCHEMA

metadata = MetaData()

ASSET_KINDS = ("document_unit", "dataset_snapshot", "tool_result", "artifact_unit")
SOURCE_TIERS = ("tier_0a", "tier_0b", "tier_1", "tier_2", "tier_3", "tier_f")
TRACE_LEVELS = ("G0", "G1", "G2", "G3", "G4")
QUALITY_STATUSES = ("ok", "needs_review", "unusable", "empty")
RUN_STATUSES = ("running", "succeeded", "failed")
ACCESS_RESULT_STATUSES = ("ok", "empty", "error")
EVENT_KINDS = ("materialized", "observed")


def _enum_check(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


processing_run = Table(
    "processing_run",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("run_kind", String(64), nullable=False),
    Column("provider", String(64), nullable=False),
    Column("adapter", String(128), nullable=False),
    Column("adapter_version", String(64), nullable=False),
    Column("params", JSONB, nullable=True),
    Column("status", String(32), nullable=False, server_default=sa_text("'running'")),
    Column("error", JSONB, nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    _enum_check("status", RUN_STATUSES, "ck_processing_run_status"),
    schema=CORE_SCHEMA,
)

source_access = Table(
    "source_access",
    metadata,
    Column("access_id", String(64), primary_key=True),
    Column("provider", String(64), nullable=False),
    Column("adapter", String(128), nullable=False),
    Column("adapter_version", String(64), nullable=False),
    Column("dataset_key", String(255), nullable=True),
    Column("tool", String(128), nullable=True),
    Column("query_params", JSONB, nullable=False),
    Column("query_params_hash", String(64), nullable=False),
    Column("provider_as_of", Text, nullable=True),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("result_status", String(32), nullable=False),
    Column("result_count", Integer, nullable=True),
    Column("error", JSONB, nullable=True),
    Column(
        "processing_run_id",
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.processing_run.run_id"),
        nullable=False,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    _enum_check("result_status", ACCESS_RESULT_STATUSES, "ck_source_access_result_status"),
    Index("ix_source_access_provider_dataset", "provider", "dataset_key", "observed_at"),
    schema=CORE_SCHEMA,
)

data_asset = Table(
    "data_asset",
    metadata,
    # A. identity
    Column("asset_id", String(64), primary_key=True),
    Column("asset_kind", String(32), nullable=False),
    Column("payload_kind", String(32), nullable=True),
    Column("contract_version", String(32), nullable=False, server_default=sa_text("'data_asset.v1'")),
    Column("content_hash", String(128), nullable=False),
    # B. scope
    Column("subject_candidates", JSONB, nullable=True),
    Column("title", Text, nullable=True),
    Column("heading_path", JSONB, nullable=True),
    Column("semantic_key", String(255), nullable=True),
    Column("parent_ref", String(255), nullable=True),
    Column("order_index", Integer, nullable=True),
    Column("material_type", String(64), nullable=True),
    # C. time
    Column("event_time", DateTime(timezone=True), nullable=True),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("report_period", String(32), nullable=True),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    # D. provenance (query_params live on source_access; one-hop via source_access_id)
    Column(
        "source_access_id",
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.source_access.access_id"),
        nullable=False,
    ),
    Column("provider", String(64), nullable=False),
    Column("adapter", String(128), nullable=False),
    Column("tool", String(128), nullable=True),
    Column("source_tier", String(16), nullable=False),
    Column("trace_level", String(4), nullable=False),
    Column("locator", Text, nullable=True),
    Column("raw_asset_ref", Text, nullable=True),
    Column(
        "processing_run_id",
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.processing_run.run_id"),
        nullable=False,
    ),
    Column("sensitivity", String(32), nullable=True),
    # E. payload
    Column("payload", JSONB, nullable=True),
    # F. state
    Column("quality_status", String(32), nullable=False, server_default=sa_text("'ok'")),
    Column("is_active", Boolean, nullable=False, server_default=sa_text("true")),
    Column("change_seq", BigInteger, nullable=True),
    Column("superseded_by", String(64), nullable=True),
    Column("dedup_key", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    _enum_check("asset_kind", ASSET_KINDS, "ck_data_asset_asset_kind"),
    _enum_check("source_tier", SOURCE_TIERS, "ck_data_asset_source_tier"),
    _enum_check("trace_level", TRACE_LEVELS, "ck_data_asset_trace_level"),
    _enum_check("quality_status", QUALITY_STATUSES, "ck_data_asset_quality_status"),
    CheckConstraint(
        "payload IS NOT NULL OR raw_asset_ref IS NOT NULL",
        name="ck_data_asset_payload_or_raw",
    ),
    UniqueConstraint("provider", "dedup_key", name="uq_data_asset_provider_dedup"),
    Index("ix_data_asset_provider_observed", "provider", "observed_at"),
    Index("ix_data_asset_kind_period", "asset_kind", "report_period"),
    schema=CORE_SCHEMA,
)

outbox_event = Table(
    "outbox_event",
    metadata,
    Column("seq", BigInteger, primary_key=True, autoincrement=True),
    Column("event_id", String(64), nullable=False, unique=True),
    Column("event_kind", String(32), nullable=False),
    Column("subject_ref", Text, nullable=False),
    Column("asset_id", String(64), nullable=True),
    Column("processing_run_id", String(64), nullable=True),
    Column("payload", JSONB, nullable=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    _enum_check("event_kind", EVENT_KINDS, "ck_outbox_event_kind"),
    Index("ix_outbox_event_asset", "asset_id"),
    schema=OPS_SCHEMA,
)
