"""tracked_companies_v1 public view — the tracking pool becomes readable contract

Revision ID: 0019_tracked_companies_view
Revises: 0018_process_classes
Create Date: 2026-07-08

User ruling (round22): the DB is now the single source of truth for the
tracking pool; config/watchlist.csv demotes to import/seed + git snapshot.
Other services (L2-L6) and operators read the pool through this view instead
of touching disclosure_core tables. The view exposes RAW override columns
only (NULL = inherit the global default); cascade-resolved effective values
are an API-layer derivation because the global policy lives in
config/processing_policy.json, which the database cannot see.
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    FUTURE_L2_READER_ROLE,
    PUBLIC_SCHEMA,
    READER_ROLE,
)

# revision identifiers, used by Alembic.
revision: str = "0019_tracked_companies_view"
down_revision: Union[str, None] = "0018_process_classes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        f"""
        CREATE VIEW {PUBLIC_SCHEMA}.tracked_companies_v1 AS
        SELECT
            tc.tracked_company_id,
            tc.company_id AS company_ref,
            tc.security_id AS security_ref,
            s.security_code,
            s.exchange,
            c.legal_name,
            tc.status,
            (tc.lookback->>'days')::int AS lookback_days,
            tc.sync_frequency,
            tc.process_classes,
            tc.created_at,
            tc.updated_at,
            'tracked_company.v1'::text AS contract_version
        FROM {CORE_SCHEMA}.tracked_company tc
        JOIN {CORE_SCHEMA}.company c ON c.company_id = tc.company_id
        LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = tc.security_id
        """
    )
    op.execute(
        f"GRANT SELECT ON {PUBLIC_SCHEMA}.tracked_companies_v1 TO "
        f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
    )


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.tracked_companies_v1")
