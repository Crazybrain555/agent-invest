"""tracked_company.filing_categories -> process_classes (cascade override)

Revision ID: 0018_process_classes
Revises: 0017_filing_type_derived
Create Date: 2026-07-08

Round21 user ruling: the per-company column must be a cascade override of the
SAME parameter as the global default (config/processing_policy.json process
list) — like lookback/sync_frequency. Old semantics (sync-side registration
filter) die: registration is always full so a class added to the policy later
backfills from already-registered metadata. Values are class keys (jsonb
array), replacement semantics, NULL inherits the global policy.
"""

from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA

# revision identifiers, used by Alembic.
revision: str = "0018_process_classes"
down_revision: Union[str, None] = "0017_filing_type_derived"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.tracked_company "
        "RENAME COLUMN filing_categories TO process_classes"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE {CORE_SCHEMA}.tracked_company "
        "RENAME COLUMN process_classes TO filing_categories"
    )
