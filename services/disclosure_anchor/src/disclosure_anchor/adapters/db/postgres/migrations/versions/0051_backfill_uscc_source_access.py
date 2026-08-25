"""Bind provider-sourced USCC identifiers to their exact source access.

Revision ID: 0051_uscc_observation
Revises: 0050_verify_unit_routes
Create Date: 2026-08-24

Historical CNINFO profile syncs persisted complete p_stock2100 snapshots after
SubjectResolver had already created the matching USCC ledger rows without a
source_access_id.  Bind the first later independent exact observation; this is
explicit remediation evidence, not a claim about the unknown creation source.
Manual identifiers without such proof remain nullable.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA


revision: str = "0051_uscc_observation"
down_revision: Union[str, None] = "0050_verify_unit_routes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_EXACT_PROFILE_MATCHES = f"""
    WITH exact_matches AS (
        SELECT ci.identifier_id,
               sa.source_access_id,
               sa.accessed_at,
               row_number() OVER (
                   PARTITION BY ci.identifier_id
                   ORDER BY sa.accessed_at ASC, sa.source_access_id ASC
               ) AS match_rank
          FROM {CORE_SCHEMA}.company_identifier AS ci
          JOIN {CORE_SCHEMA}.security AS sec
            ON sec.company_id = ci.company_id
          JOIN {CORE_SCHEMA}.source_access AS sa
            ON sa.provider = 'cninfo'
           AND sa.provider_interface = 'cninfo:p_stock2100'
           AND sa.query_params ->> 'scode' = sec.security_code
           AND sa.status = 'ok'
           AND upper(btrim(sa.result_snapshot #>> '{{profile,uscc}}'))
               = ci.normalized_value
           AND sa.accessed_at >= ci.created_at
         WHERE ci.scheme = 'uscc'
           AND ci.source_access_id IS NULL
    )
"""


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            _EXACT_PROFILE_MATCHES
            + f"""
            UPDATE {CORE_SCHEMA}.company_identifier AS ci
               SET source_access_id = matched.source_access_id,
                   observed_at = matched.accessed_at
              FROM exact_matches AS matched
             WHERE matched.match_rank = 1
               AND ci.identifier_id = matched.identifier_id
               AND ci.source_access_id IS NULL
            """
        )
    )
    unresolved_exact_matches = connection.execute(
        sa.text(
            _EXACT_PROFILE_MATCHES
            + """
            SELECT count(*)
              FROM exact_matches
             WHERE match_rank = 1
            """
        )
    ).scalar_one()
    if unresolved_exact_matches:
        raise RuntimeError(
            "0051 failed to bind exact CNINFO USCC source-access evidence"
        )


def downgrade() -> None:
    # The backfill records true, hash-bound provenance. Removing it would lose
    # evidence and could also erase later observations, so downgrade is a no-op.
    pass
