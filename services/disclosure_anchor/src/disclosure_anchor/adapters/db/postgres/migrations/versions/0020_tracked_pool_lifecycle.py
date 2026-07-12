"""tracked_companies_v1 lifecycle columns — pool state visible at a glance

Revision ID: 0020_tracked_pool_lifecycle
Revises: 0019_tracked_companies_view
Create Date: 2026-07-08

Round22c (user: 状态怎么说 — which companies are initialized vs synced?).
Industry analog: Miniflux feeds expose checked_at, changedetection.io watches
expose last_checked. Additive columns, all derived (no new storage):
  legal_name_status  'pending' while the offline-intake placeholder name
                     stands, 'resolved' once a profile/document supplied the
                     real legal name;
  last_synced_at     cninfo index-sync checkpoint timestamp (NULL = the
                     worker has never synced this company);
  synced_through     checkpoint cursor window_end (coverage date).
The due/fresh judgement stays API-side (GET /v1/tracked-companies
sync_state) because the effective interval cascade includes env defaults
the database cannot see.
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
revision: str = "0020_tracked_pool_lifecycle"
down_revision: Union[str, None] = "0019_tracked_companies_view"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VIEW_WITH_LIFECYCLE = f"""
    CREATE VIEW {PUBLIC_SCHEMA}.tracked_companies_v1 AS
    SELECT
        tc.tracked_company_id,
        tc.company_id AS company_ref,
        tc.security_id AS security_ref,
        s.security_code,
        s.exchange,
        c.legal_name,
        CASE WHEN c.legal_name LIKE 'PENDING_LEGAL_NAME %'
             THEN 'pending' ELSE 'resolved' END AS legal_name_status,
        tc.status,
        (tc.lookback->>'days')::int AS lookback_days,
        tc.sync_frequency,
        tc.process_classes,
        sc.updated_at AS last_synced_at,
        (sc.cursor->>'window_end')::date AS synced_through,
        tc.created_at,
        tc.updated_at,
        'tracked_company.v1'::text AS contract_version
    FROM {CORE_SCHEMA}.tracked_company tc
    JOIN {CORE_SCHEMA}.company c ON c.company_id = tc.company_id
    LEFT JOIN {CORE_SCHEMA}.security s ON s.security_id = tc.security_id
    LEFT JOIN {CORE_SCHEMA}.source_checkpoint sc
      ON sc.provider = 'cninfo'
     AND sc.scope_key = tc.company_id || '\\:p_info3015'
"""

_VIEW_0019 = f"""
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


def _grant() -> None:
    op.execute(
        f"GRANT SELECT ON {PUBLIC_SCHEMA}.tracked_companies_v1 TO "
        f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
    )


def upgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.tracked_companies_v1")
    op.execute(_VIEW_WITH_LIFECYCLE)
    _grant()


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {PUBLIC_SCHEMA}.tracked_companies_v1")
    op.execute(_VIEW_0019)
    _grant()
