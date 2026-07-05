"""initial core/ops tables, public views and grants

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-29
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
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

from disclosure_anchor.adapters.db.postgres.schema import (
    ALEMBIC_VERSION_TABLE,
    APP_ROLE,
    CORE_SCHEMA,
    FUTURE_L2_READER_ROLE,
    OPS_SCHEMA,
    OWNER_ROLE,
    PUBLIC_SCHEMA,
    PUBLIC_VIEWS,
    READER_ROLE,
)

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Frozen table/index snapshot for this revision. Do not import live ORM metadata
# here: future model edits must not change what 0001 creates on a fresh DB.
FROZEN_METADATA = MetaData()

company = Table(
    "company",
    FROZEN_METADATA,
    Column("company_id", String(64), primary_key=True),
    Column("legal_name", Text, nullable=False),
    Column("unified_social_credit_code", String(32), nullable=True, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    schema=CORE_SCHEMA,
)

security = Table(
    "security",
    FROZEN_METADATA,
    Column("security_id", String(64), primary_key=True),
    Column(
        "company_id",
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.company.company_id"),
        nullable=False,
    ),
    Column("security_code", String(32), nullable=False),
    Column("exchange", String(32), nullable=False),
    Column("board", String(32), nullable=True),
    Column("status", String(32), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    UniqueConstraint("security_code", "exchange", name="uq_security_code_exchange"),
    schema=CORE_SCHEMA,
)
Index("ix_security_company", security.c.company_id)

tracked_company = Table(
    "tracked_company",
    FROZEN_METADATA,
    Column("tracked_company_id", String(64), primary_key=True),
    Column(
        "company_id",
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.company.company_id"),
        nullable=False,
    ),
    Column(
        "security_id",
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.security.security_id"),
        nullable=True,
    ),
    Column("status", String(32), nullable=False, server_default=sa_text("'active'")),
    Column("lookback", JSONB, nullable=True),
    Column("filing_categories", JSONB, nullable=True),
    Column("sync_frequency", String(32), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    UniqueConstraint("company_id", name="uq_tracked_company_company"),
    schema=CORE_SCHEMA,
)
Index("ix_tracked_company_security", tracked_company.c.security_id)

source_access = Table(
    "source_access",
    FROZEN_METADATA,
    Column("source_access_id", String(64), primary_key=True),
    Column("provider", String(64), nullable=False),
    Column("provider_interface", String(128), nullable=True),
    Column("dataset_key", String(128), nullable=True),
    Column("query_params", JSONB, nullable=True),
    Column("accessed_at", DateTime(timezone=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("result_hash", String(128), nullable=True),
    Column("error", Text, nullable=True),
    Column("result_snapshot", JSONB, nullable=True),
    Column(
        "company_id",
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.company.company_id"),
        nullable=True,
    ),
    Column(
        "security_id",
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.security.security_id"),
        nullable=True,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    schema=CORE_SCHEMA,
)
Index("ix_source_access_provider", source_access.c.provider)
Index("ix_source_access_company", source_access.c.company_id)
Index("ix_source_access_security", source_access.c.security_id)

source_checkpoint = Table(
    "source_checkpoint",
    FROZEN_METADATA,
    Column("source_checkpoint_id", String(64), primary_key=True),
    Column("provider", String(64), nullable=False),
    Column("scope_key", String(256), nullable=False),
    Column("cursor", JSONB, nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    UniqueConstraint("provider", "scope_key", name="uq_source_checkpoint_scope"),
    schema=CORE_SCHEMA,
)

document = Table(
    "document",
    FROZEN_METADATA,
    Column("document_id", String(64), primary_key=True),
    Column(
        "company_id",
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.company.company_id"),
        nullable=True,
    ),
    Column(
        "security_id",
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.security.security_id"),
        nullable=True,
    ),
    Column(
        "source_access_id",
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.source_access.source_access_id"),
        nullable=True,
    ),
    Column("provider", String(64), nullable=True),
    Column("provider_document_id", String(128), nullable=True),
    Column("title", Text, nullable=True),
    Column("filing_type", String(64), nullable=True),
    Column("announcement_date", Date, nullable=True),
    Column("report_period", String(32), nullable=True),
    Column("raw_file_relpath", Text, nullable=True),
    Column("raw_file_hash", String(128), nullable=True),
    Column("status", String(32), nullable=False),
    Column("current_processing_run_id", String(64), nullable=True),
    Column("supersedes_document_id", String(64), nullable=True),
    Column("correction_of_document_id", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    schema=CORE_SCHEMA,
)
Index("ix_document_company", document.c.company_id)
Index("ix_document_security", document.c.security_id)
Index("ix_document_source_access", document.c.source_access_id)
Index("ix_document_provider_ref", document.c.provider, document.c.provider_document_id)
Index("ix_document_raw_hash", document.c.raw_file_hash)

processing_run = Table(
    "processing_run",
    FROZEN_METADATA,
    Column("processing_run_id", String(64), primary_key=True),
    Column(
        "document_id",
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.document.document_id"),
        nullable=False,
    ),
    Column("run_kind", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("parser_name", String(64), nullable=True),
    Column("parser_version", String(64), nullable=True),
    Column("artifact_hash", String(128), nullable=True),
    Column("normalized_ir_relpath", Text, nullable=True),
    Column("document_units_relpath", Text, nullable=True),
    Column("content_hash_aggregate", String(128), nullable=True),
    Column("structure_hash", String(128), nullable=True),
    Column("is_active", Boolean, nullable=False, server_default=sa_text("false")),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("error", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    schema=CORE_SCHEMA,
)
Index("ix_processing_run_document", processing_run.c.document_id)
Index(
    "uq_processing_run_one_active_per_document",
    processing_run.c.document_id,
    unique=True,
    postgresql_where=sa_text("is_active"),
)

document_unit = Table(
    "document_unit",
    FROZEN_METADATA,
    Column("document_unit_id", String(64), primary_key=True),
    Column(
        "document_id",
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.document.document_id"),
        nullable=False,
    ),
    Column(
        "processing_run_id",
        String(64),
        ForeignKey(f"{CORE_SCHEMA}.processing_run.processing_run_id"),
        nullable=False,
    ),
    Column("provider_document_id", String(128), nullable=True),
    Column("unit_kind", String(16), nullable=False),
    Column("heading_path", JSONB, nullable=False, server_default=sa_text("'[]'::jsonb")),
    Column("title", Text, nullable=True),
    Column("order_index", Integer, nullable=False),
    Column("semantic_key", String(128), nullable=True),
    Column("payload", JSONB, nullable=False),
    Column("content_hash", String(128), nullable=False),
    Column("structure_hash", String(128), nullable=True),
    Column("quality_status", String(32), nullable=False, server_default=sa_text("'ok'")),
    Column("artifact_locator", JSONB, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    CheckConstraint("unit_kind in ('text','table','qa')", name="ck_document_unit_kind"),
    UniqueConstraint("processing_run_id", "order_index", name="uq_document_unit_run_order"),
    schema=CORE_SCHEMA,
)
Index("ix_document_unit_document", document_unit.c.document_id)
Index("ix_document_unit_run", document_unit.c.processing_run_id)
Index("ix_document_unit_semantic_key", document_unit.c.semantic_key)

outbox_event = Table(
    "outbox_event",
    FROZEN_METADATA,
    Column("seq", BigInteger, primary_key=True, autoincrement=True),
    Column("event_id", String(64), nullable=False, unique=True),
    Column("event_type", String(64), nullable=False),
    Column("document_id", String(64), nullable=True),
    Column("processing_run_id", String(64), nullable=True),
    Column("document_unit_id", String(64), nullable=True),
    Column("payload", JSONB, nullable=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=sa_text("now()")),
    schema=OPS_SCHEMA,
)
Index("ix_outbox_event_document", outbox_event.c.document_id)


# Public read views. They run with the owner's privileges, so read-only roles
# need only SELECT on the view plus USAGE on the public schema. None of these
# expose absolute paths, secrets, MinerU raw JSON, internal error text or
# private schema structure.
VIEW_SQL: list[str] = [
    f"""
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.documents_v1 AS
    SELECT
        d.document_id,
        d.provider,
        d.provider_document_id,
        s.security_code,
        s.exchange,
        d.filing_type,
        d.title,
        d.announcement_date,
        d.report_period,
        d.raw_file_hash,
        d.status,
        d.current_processing_run_id,
        d.created_at,
        d.updated_at
    FROM {CORE_SCHEMA}.document d
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    """,
    f"""
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.document_units_v1 AS
    SELECT
        u.document_unit_id,
        u.document_id,
        u.processing_run_id,
        u.provider_document_id,
        u.unit_kind,
        u.heading_path,
        u.title,
        u.order_index,
        u.semantic_key,
        u.payload,
        u.content_hash,
        u.structure_hash,
        u.quality_status,
        u.artifact_locator,
        u.created_at
    FROM {CORE_SCHEMA}.document_unit u
    """,
    f"""
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.processing_runs_v1 AS
    SELECT
        r.processing_run_id,
        r.document_id,
        r.run_kind,
        r.status,
        r.parser_name,
        r.parser_version,
        r.artifact_hash,
        r.content_hash_aggregate,
        r.structure_hash,
        r.is_active,
        r.started_at,
        r.finished_at,
        r.created_at
    FROM {CORE_SCHEMA}.processing_run r
    """,
    f"""
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.source_refs_v1 AS
    SELECT
        u.document_unit_id,
        d.source_access_id,
        u.document_id,
        d.provider,
        d.provider_document_id,
        d.raw_file_hash,
        u.processing_run_id,
        u.unit_kind,
        u.heading_path,
        u.title,
        u.content_hash AS unit_content_hash,
        u.quality_status,
        u.artifact_locator
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    """,
    f"""
    CREATE OR REPLACE VIEW {PUBLIC_SCHEMA}.change_events_v1 AS
    SELECT
        e.seq,
        e.event_id,
        e.event_type,
        e.document_id,
        e.processing_run_id,
        e.document_unit_id,
        e.payload,
        e.occurred_at
    FROM {OPS_SCHEMA}.outbox_event e
    """,
]


GRANT_SQL: list[str] = [
    # app: read/write private core + ops
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {CORE_SCHEMA} TO {APP_ROLE}",
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON {OPS_SCHEMA}.outbox_event TO {APP_ROLE}",
    f"REVOKE ALL PRIVILEGES ON {OPS_SCHEMA}.{ALEMBIC_VERSION_TABLE} FROM {APP_ROLE}",
    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {CORE_SCHEMA} TO {APP_ROLE}",
    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {OPS_SCHEMA} TO {APP_ROLE}",
    # app + readers: read public views only
    f"GRANT SELECT ON ALL TABLES IN SCHEMA {PUBLIC_SCHEMA} TO {APP_ROLE}",
    f"GRANT SELECT ON ALL TABLES IN SCHEMA {PUBLIC_SCHEMA} TO {READER_ROLE}, {FUTURE_L2_READER_ROLE}",
    # default privileges so future owner-created objects stay consistent
    f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} IN SCHEMA {CORE_SCHEMA} "
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}",
    f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} IN SCHEMA {OPS_SCHEMA} "
    f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}",
    f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} IN SCHEMA {CORE_SCHEMA} "
    f"GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}",
    f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} IN SCHEMA {OPS_SCHEMA} "
    f"GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}",
    f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} IN SCHEMA {PUBLIC_SCHEMA} "
    f"GRANT SELECT ON TABLES TO {APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}",
]


def upgrade() -> None:
    bind = op.get_bind()
    # Schemas are pre-created by db-create bootstrap.
    FROZEN_METADATA.create_all(bind=bind, checkfirst=True)

    for statement in VIEW_SQL:
        op.execute(statement)
    for statement in GRANT_SQL:
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    for view in PUBLIC_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.{view}")
    FROZEN_METADATA.drop_all(bind=bind, checkfirst=True)
