"""envelope, change feed, queue and identifier hardening

Revision ID: 0007_envelope_and_feed_hardening
Revises: 0006_v07_terminology_convergence
Create Date: 2026-07-04
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    FUTURE_L2_READER_ROLE,
    OPS_SCHEMA,
    PUBLIC_SCHEMA,
    READER_ROLE,
)
from disclosure_anchor.domain import ids

# revision identifiers, used by Alembic.
revision: str = "0007_envelope_and_feed_hardening"
down_revision: Union[str, None] = "0006_v07_terminology_convergence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PUBLIC_REBUILT_VIEWS = (
    "document_units_v1",
    "change_events_v1",
    "documents_v1",
    "processing_runs_v1",
)
OPS_QUEUE_VIEWS = (
    "pending_parse_v1",
    "pending_build_v1",
    "pending_publish_v1",
    "retryable_failed_run_v1",
    "stale_running_run_v1",
)


def upgrade() -> None:
    _drop_0007_views()
    _harden_outbox_event()
    _harden_processing_run()
    _harden_document()
    _harden_document_unit()
    _create_company_identifier()
    _create_hot_path_indexes()
    _normalize_legacy_provider()
    _create_0007_views()
    _grant_0007_views()


def downgrade() -> None:
    _drop_0007_views()
    _drop_hot_path_indexes()
    op.drop_table("company_identifier", schema=CORE_SCHEMA)

    with op.batch_alter_table("document_unit", schema=CORE_SCHEMA) as batch:
        batch.drop_constraint("ck_document_unit_quality_status", type_="check")
        batch.drop_column("query_projection_hash")

    with op.batch_alter_table("document", schema=CORE_SCHEMA) as batch:
        batch.drop_constraint("ck_document_status", type_="check")
        batch.drop_column("provider_metadata")

    op.execute(
        f"""
        ALTER TABLE {CORE_SCHEMA}.processing_run
        ALTER COLUMN error TYPE text
        USING CASE WHEN error IS NULL THEN NULL ELSE error::text END
        """
    )
    with op.batch_alter_table("processing_run", schema=CORE_SCHEMA) as batch:
        batch.drop_constraint("ck_processing_run_unit_build_status", type_="check")
        batch.drop_column("unit_built_at")
        batch.drop_column("unit_build_attempt_count")
        batch.drop_column("unit_build_error")
        batch.drop_column("unit_build_status")
        batch.drop_column("parser_language")
        batch.drop_column("parser_method")

    with op.batch_alter_table("outbox_event", schema=OPS_SCHEMA) as batch:
        batch.drop_constraint("ck_outbox_event_subject_kind", type_="check")
        batch.drop_constraint("ck_outbox_event_change_kind", type_="check")
        batch.drop_column("subject_ref")
        batch.drop_column("subject_kind")
        batch.drop_column("change_kind")

    op.execute(_documents_view_sql_0006())
    op.execute(_document_units_view_sql_0006())
    op.execute(_processing_runs_view_sql_0006())
    op.execute(_change_events_view_sql_0006())
    _grant_public_views()


def _drop_0007_views() -> None:
    for view in OPS_QUEUE_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {OPS_SCHEMA}.{view}")
    for view in PUBLIC_REBUILT_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.{view}")


def _harden_outbox_event() -> None:
    with op.batch_alter_table("outbox_event", schema=OPS_SCHEMA) as batch:
        batch.add_column(sa.Column("change_kind", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("subject_kind", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("subject_ref", sa.String(length=64), nullable=True))

    op.execute(
        f"""
        UPDATE {OPS_SCHEMA}.outbox_event
        SET change_kind = CASE
            WHEN payload ->> 'change_kind' IN ('observed', 'materialized')
                THEN payload ->> 'change_kind'
            ELSE 'materialized'
        END
        """
    )
    op.execute(
        f"""
        UPDATE {OPS_SCHEMA}.outbox_event
        SET
            subject_kind = CASE
                WHEN asset_id IS NOT NULL THEN 'document_unit'
                WHEN processing_run_id IS NOT NULL THEN 'processing_run'
                ELSE 'document'
            END,
            subject_ref = CASE
                WHEN asset_id IS NOT NULL THEN asset_id
                WHEN processing_run_id IS NOT NULL THEN processing_run_id
                ELSE COALESCE(document_id, event_id)
            END
        """
    )
    with op.batch_alter_table("outbox_event", schema=OPS_SCHEMA) as batch:
        batch.alter_column("change_kind", nullable=False)
        batch.alter_column("subject_kind", nullable=False)
        batch.alter_column("subject_ref", nullable=False)
        batch.create_check_constraint(
            "ck_outbox_event_change_kind",
            "change_kind IN ('observed','materialized')",
        )
        batch.create_check_constraint(
            "ck_outbox_event_subject_kind",
            "subject_kind IN ('document','processing_run','document_unit','source_access')",
        )


def _harden_processing_run() -> None:
    with op.batch_alter_table("processing_run", schema=CORE_SCHEMA) as batch:
        batch.add_column(sa.Column("parser_method", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("parser_language", sa.String(length=16), nullable=True))
        batch.add_column(
            sa.Column(
                "unit_build_status",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'not_started'"),
            )
        )
        batch.add_column(sa.Column("unit_build_error", postgresql.JSONB(), nullable=True))
        batch.add_column(
            sa.Column(
                "unit_build_attempt_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(sa.Column("unit_built_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint(
            "ck_processing_run_unit_build_status",
            "unit_build_status IN ('not_started','running','succeeded','failed')",
        )

    op.execute(
        f"""
        ALTER TABLE {CORE_SCHEMA}.processing_run
        ALTER COLUMN error TYPE jsonb
        USING CASE
            WHEN error IS NULL OR error = '' THEN NULL
            ELSE error::jsonb
        END
        """
    )


def _harden_document() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM {CORE_SCHEMA}.document
                WHERE status NOT IN ('registered','parsed','parse_failed','published')
            ) THEN
                RAISE EXCEPTION 'document.status contains values outside 0007 enum';
            END IF;
        END
        $$;
        """
    )
    with op.batch_alter_table("document", schema=CORE_SCHEMA) as batch:
        batch.create_check_constraint(
            "ck_document_status",
            "status IN ('registered','parsed','parse_failed','published')",
        )
        batch.add_column(
            sa.Column(
                "provider_metadata",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            )
        )


def _harden_document_unit() -> None:
    with op.batch_alter_table("document_unit", schema=CORE_SCHEMA) as batch:
        batch.create_check_constraint(
            "ck_document_unit_quality_status",
            "quality_status IN ('ok','needs_review','unusable')",
        )
        batch.add_column(sa.Column("query_projection_hash", sa.String(length=128), nullable=True))


def _create_company_identifier() -> None:
    op.create_table(
        "company_identifier",
        sa.Column("identifier_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "company_id",
            sa.String(length=64),
            sa.ForeignKey(f"{CORE_SCHEMA}.company.company_id"),
            nullable=False,
        ),
        sa.Column("scheme", sa.String(length=32), nullable=False),
        sa.Column("raw_value", sa.Text(), nullable=False),
        sa.Column("normalized_value", sa.String(length=128), nullable=False),
        sa.Column("jurisdiction", sa.String(length=8), nullable=True),
        sa.Column(
            "source_access_id",
            sa.String(length=64),
            sa.ForeignKey(f"{CORE_SCHEMA}.source_access.source_access_id"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('active','retired','contested')",
            name="ck_company_identifier_status",
        ),
        schema=CORE_SCHEMA,
    )
    op.create_index(
        "uq_company_identifier_strong_key",
        "company_identifier",
        ["scheme", "normalized_value"],
        unique=True,
        schema=CORE_SCHEMA,
        postgresql_where=sa.text(
            "scheme IN ('uscc','lei','sec_cik','hk_cr') AND status='active'"
        ),
    )
    op.create_index(
        "ix_company_identifier_company",
        "company_identifier",
        ["company_id"],
        schema=CORE_SCHEMA,
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            f"""
            SELECT company_id, unified_social_credit_code, created_at
            FROM {CORE_SCHEMA}.company
            WHERE unified_social_credit_code IS NOT NULL
            """
        )
    ).mappings()
    for row in rows:
        bind.execute(
            sa.text(
                f"""
                INSERT INTO {CORE_SCHEMA}.company_identifier
                    (identifier_id, company_id, scheme, raw_value, normalized_value,
                     jurisdiction, status, observed_at)
                VALUES
                    (:identifier_id, :company_id, 'uscc', :raw_value, :normalized_value,
                     'CN', 'active', :observed_at)
                """
            ),
            {
                "identifier_id": ids.new_company_identifier_id(),
                "company_id": row["company_id"],
                "raw_value": row["unified_social_credit_code"],
                "normalized_value": row["unified_social_credit_code"].strip().upper(),
                "observed_at": row["created_at"],
            },
        )


def _create_hot_path_indexes() -> None:
    op.create_index(
        "ix_document_company_period_type",
        "document",
        ["company_id", "report_period", "filing_type"],
        schema=CORE_SCHEMA,
    )
    op.create_index(
        "ix_document_announcement_date",
        "document",
        ["announcement_date"],
        schema=CORE_SCHEMA,
    )
    op.create_index(
        "ix_document_unit_run_order",
        "document_unit",
        ["document_id", "processing_run_id", "order_index", "asset_id"],
        schema=CORE_SCHEMA,
    )
    op.create_index(
        "ix_document_unit_content_hash",
        "document_unit",
        ["content_hash"],
        schema=CORE_SCHEMA,
    )
    op.create_index(
        "ix_document_unit_heading_path",
        "document_unit",
        ["heading_path"],
        schema=CORE_SCHEMA,
        postgresql_using="gin",
        postgresql_ops={"heading_path": "jsonb_path_ops"},
    )


def _drop_hot_path_indexes() -> None:
    for name, table in (
        ("ix_document_unit_heading_path", "document_unit"),
        ("ix_document_unit_content_hash", "document_unit"),
        ("ix_document_unit_run_order", "document_unit"),
        ("ix_document_announcement_date", "document"),
        ("ix_document_company_period_type", "document"),
    ):
        op.drop_index(name, table_name=table, schema=CORE_SCHEMA)


def _normalize_legacy_provider() -> None:
    op.execute(f"UPDATE {CORE_SCHEMA}.document SET provider='cninfo' WHERE provider='local'")
    op.execute(
        f"UPDATE {CORE_SCHEMA}.source_access SET provider='cninfo' WHERE provider='local'"
    )


def _create_0007_views() -> None:
    op.execute(_document_units_view_sql_0007())
    op.execute(_change_events_view_sql_0007())
    op.execute(_documents_view_sql_0007())
    op.execute(_processing_runs_view_sql_0007())
    op.execute(_pending_parse_view_sql())
    op.execute(_pending_build_view_sql())
    op.execute(_pending_publish_view_sql())
    op.execute(_retryable_failed_run_view_sql())
    op.execute(_stale_running_run_view_sql())


def _grant_0007_views() -> None:
    _grant_public_views()
    for view in OPS_QUEUE_VIEWS:
        op.execute(f"GRANT SELECT ON {OPS_SCHEMA}.{view} TO {APP_ROLE}")


def _grant_public_views() -> None:
    views = ", ".join(f"{PUBLIC_SCHEMA}.{view}" for view in PUBLIC_REBUILT_VIEWS)
    op.execute(
        f"GRANT SELECT ON {views} TO {APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
    )


def _document_units_view_sql_0007() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.document_units_v1 AS
    SELECT
        u.asset_id,
        u.document_id,
        u.processing_run_id,
        u.provider_document_id,
        u.payload_kind,
        u.heading_path,
        u.title,
        u.order_index,
        u.semantic_key,
        u.payload,
        u.content_hash,
        u.structure_hash,
        u.quality_status,
        u.artifact_locator,
        u.created_at,
        'document_unit.v1'::text AS contract_version,
        d.company_id AS company_ref,
        d.security_id AS security_ref,
        s.security_code,
        s.exchange,
        d.filing_type,
        d.report_period,
        d.announcement_date,
        u.processing_run_id AS producer_action_ref,
        d.source_access_id AS source_ref,
        u.document_id AS parent_ref,
        'document_unit'::text AS asset_kind,
        u.created_at AS observed_at,
        CASE
            WHEN d.filing_type IN ('investor_relations','performance_briefing')
                THEN 'tier_0b'
            ELSE 'tier_0a'
        END AS source_tier,
        'G0'::text AS trace_level,
        d.raw_file_hash,
        u.query_projection_hash
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    """


def _change_events_view_sql_0007() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.change_events_v1 AS
    SELECT
        e.seq,
        e.event_id,
        e.event_kind,
        e.document_id,
        e.processing_run_id,
        e.asset_id,
        e.payload,
        e.occurred_at,
        e.change_kind,
        e.subject_kind,
        e.subject_ref,
        'disclosure_anchor'::text AS source,
        'change_event.v1'::text AS contract_version,
        e.created_at
    FROM {OPS_SCHEMA}.outbox_event e
    """


def _documents_view_sql_0007() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.documents_v1 AS
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
        d.updated_at,
        'document.v1'::text AS contract_version,
        d.company_id AS company_ref,
        d.security_id AS security_ref,
        d.source_access_id AS source_ref,
        d.supersedes_document_id,
        d.correction_of_document_id,
        sb.document_id AS superseded_by_document_id,
        d.provider_metadata
    FROM {CORE_SCHEMA}.document d
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    LEFT JOIN LATERAL (
        SELECT x.document_id
        FROM {CORE_SCHEMA}.document x
        WHERE x.supersedes_document_id = d.document_id
        ORDER BY x.created_at DESC, x.document_id DESC
        LIMIT 1
    ) sb ON true
    """


def _processing_runs_view_sql_0007() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.processing_runs_v1 AS
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
        r.created_at,
        r.parser_backend,
        r.input_raw_file_hash,
        r.parser_method,
        r.parser_language,
        r.unit_build_status,
        r.unit_build_attempt_count,
        r.unit_built_at
    FROM {CORE_SCHEMA}.processing_run r
    """


def _pending_parse_view_sql() -> str:
    return f"""
    CREATE VIEW {OPS_SCHEMA}.pending_parse_v1 AS
    SELECT d.document_id, d.status,
        (SELECT count(*) FROM {CORE_SCHEMA}.processing_run r
          WHERE r.document_id=d.document_id AND r.status='failed'
            AND r.run_kind='parse') AS failed_parse_count,
        (SELECT (r.error->>'retryable')::boolean FROM {CORE_SCHEMA}.processing_run r
          WHERE r.document_id=d.document_id AND r.status='failed'
          ORDER BY r.started_at DESC, r.processing_run_id DESC LIMIT 1)
          AS last_failed_retryable
      FROM {CORE_SCHEMA}.document d
      WHERE d.status IN ('registered','parse_failed')
        AND NOT EXISTS (SELECT 1 FROM {CORE_SCHEMA}.processing_run r
          WHERE r.document_id=d.document_id AND r.status='running')
    """


def _pending_build_view_sql() -> str:
    return f"""
    CREATE VIEW {OPS_SCHEMA}.pending_build_v1 AS
    SELECT r.processing_run_id, r.document_id, r.unit_build_status,
           r.unit_build_attempt_count
      FROM {CORE_SCHEMA}.processing_run r
      WHERE r.status='succeeded' AND r.unit_build_status IN ('not_started','failed')
    """


def _pending_publish_view_sql() -> str:
    return f"""
    CREATE VIEW {OPS_SCHEMA}.pending_publish_v1 AS
    SELECT r.processing_run_id, r.document_id
      FROM {CORE_SCHEMA}.processing_run r
      WHERE r.status='succeeded' AND r.unit_build_status='succeeded'
        AND r.is_active=false
        AND r.started_at > COALESCE((SELECT a.started_at FROM {CORE_SCHEMA}.processing_run a
          WHERE a.document_id=r.document_id AND a.is_active), '-infinity'::timestamptz)
    """


def _retryable_failed_run_view_sql() -> str:
    return f"""
    CREATE VIEW {OPS_SCHEMA}.retryable_failed_run_v1 AS
    SELECT r.processing_run_id, r.document_id, r.run_kind,
           r.error->>'error_code' AS error_code, r.finished_at
      FROM {CORE_SCHEMA}.processing_run r
      WHERE r.status='failed' AND (r.error->>'retryable')::boolean
    """


def _stale_running_run_view_sql() -> str:
    return f"""
    CREATE VIEW {OPS_SCHEMA}.stale_running_run_v1 AS
    SELECT r.processing_run_id, r.document_id, r.started_at
      FROM {CORE_SCHEMA}.processing_run r WHERE r.status='running'
    """


def _documents_view_sql_0006() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.documents_v1 AS
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
    """


def _document_units_view_sql_0006() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.document_units_v1 AS
    SELECT
        u.asset_id,
        u.document_id,
        u.processing_run_id,
        u.provider_document_id,
        u.payload_kind,
        u.heading_path,
        u.title,
        u.order_index,
        u.semantic_key,
        u.payload,
        u.content_hash,
        u.structure_hash,
        u.quality_status,
        u.artifact_locator,
        u.created_at,
        'document_unit.v1'::text AS contract_version,
        d.company_id AS company_ref,
        d.security_id AS security_ref,
        s.security_code,
        s.exchange,
        d.filing_type,
        d.report_period,
        d.announcement_date,
        u.processing_run_id AS producer_action_ref,
        d.source_access_id AS source_ref,
        u.document_id AS parent_ref
    FROM {CORE_SCHEMA}.document_unit u
    JOIN {CORE_SCHEMA}.document d ON d.document_id = u.document_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = d.security_id
    """


def _processing_runs_view_sql_0006() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.processing_runs_v1 AS
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
        r.created_at,
        r.parser_backend,
        r.input_raw_file_hash
    FROM {CORE_SCHEMA}.processing_run r
    """


def _change_events_view_sql_0006() -> str:
    return f"""
    CREATE VIEW {PUBLIC_SCHEMA}.change_events_v1 AS
    SELECT
        e.seq,
        e.event_id,
        e.event_kind,
        e.document_id,
        e.processing_run_id,
        e.asset_id,
        e.payload,
        e.occurred_at,
        CASE
            WHEN e.payload ->> 'change_kind' IN ('observed', 'materialized')
                THEN e.payload ->> 'change_kind'
            WHEN e.event_kind LIKE '%observed%' THEN 'observed'
            ELSE 'materialized'
        END AS change_kind,
        e.created_at
    FROM {OPS_SCHEMA}.outbox_event e
    """
