"""Classify mixed visual-only Units as non-answer carriers.

Revision ID: 0043_visual_only_body_status
Revises: 0042_outline_full_route_keys
Create Date: 2026-08-20

Visual evidence remains hash-bound in the Unit payload and locator.  A mixed
payload whose shallow parts contain no non-empty provider text, table, list,
caption, footnote, code, or equation field is nevertheless not searchable
answer content.  The public v1 view therefore reports it as ``heading_only``
when it has a title and ``empty`` otherwise; no source row is deleted.
"""

from importlib import import_module
from typing import Sequence, Union

from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    FUTURE_L2_READER_ROLE,
    PUBLIC_SCHEMA,
    READER_ROLE,
)


revision: str = "0043_visual_only_body_status"
down_revision: Union[str, None] = "0042_outline_full_route_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VIEW = "document_units_v1"
_LEGACY_BODY_STATUS = """        CASE
            WHEN u.payload_kind = 'text'
                 AND u.payload = '{\"text\": \"\"}'::jsonb
                 AND u.title IS NOT NULL
                THEN 'heading_only'
            WHEN u.payload_kind = 'text'
                 AND u.payload = '{\"text\": \"\"}'::jsonb
                THEN 'empty'
            ELSE 'content'
        END AS body_status"""
_VISUAL_AWARE_BODY_STATUS = """        CASE
            WHEN u.payload_kind = 'text'
                 AND u.payload = '{\"text\": \"\"}'::jsonb
                 AND u.title IS NOT NULL
                THEN 'heading_only'
            WHEN u.payload_kind = 'text'
                 AND u.payload = '{\"text\": \"\"}'::jsonb
                THEN 'empty'
            WHEN u.payload_kind = 'mixed'
                 AND jsonb_typeof(u.payload -> 'parts') = 'array'
                 AND NOT EXISTS (
                     SELECT 1
                       FROM jsonb_array_elements(u.payload -> 'parts') AS part(item)
                       CROSS JOIN LATERAL jsonb_each(part.item) AS field(key, value)
                      WHERE field.key <> 'content_artifacts'
                        AND (
                            (jsonb_typeof(field.value) = 'string'
                             AND btrim(field.value #>> '{}') <> '')
                            OR
                            (jsonb_typeof(field.value) = 'array'
                             AND EXISTS (
                                 SELECT 1
                                   FROM jsonb_array_elements_text(field.value) AS item(value)
                                  WHERE btrim(item.value) <> ''
                             ))
                        )
                 )
                 AND u.title IS NOT NULL
                THEN 'heading_only'
            WHEN u.payload_kind = 'mixed'
                 AND jsonb_typeof(u.payload -> 'parts') = 'array'
                 AND NOT EXISTS (
                     SELECT 1
                       FROM jsonb_array_elements(u.payload -> 'parts') AS part(item)
                       CROSS JOIN LATERAL jsonb_each(part.item) AS field(key, value)
                      WHERE field.key <> 'content_artifacts'
                        AND (
                            (jsonb_typeof(field.value) = 'string'
                             AND btrim(field.value #>> '{}') <> '')
                            OR
                            (jsonb_typeof(field.value) = 'array'
                             AND EXISTS (
                                 SELECT 1
                                   FROM jsonb_array_elements_text(field.value) AS item(value)
                                  WHERE btrim(item.value) <> ''
                             ))
                        )
                 )
                THEN 'empty'
            ELSE 'content'
        END AS body_status"""


def upgrade() -> None:
    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_VIEW}")
    op.execute(_document_units_view_sql(visual_only_aware=True))
    _grant_view()


def downgrade() -> None:
    op.execute(f"DROP VIEW {PUBLIC_SCHEMA}.{_VIEW}")
    op.execute(_document_units_view_sql(visual_only_aware=False))
    _grant_view()


def _grant_view() -> None:
    op.execute(
        f"GRANT SELECT ON {PUBLIC_SCHEMA}.{_VIEW} TO "
        f"{APP_ROLE}, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
    )


def _document_units_view_sql(*, visual_only_aware: bool) -> str:
    prior = import_module(
        "disclosure_anchor.adapters.db.postgres.migrations.versions."
        "0041_drop_public_unit_semantic_key"
    )
    sql = prior._document_units_view_sql(include_scalar_semantic_key=False)
    if _LEGACY_BODY_STATUS not in sql:
        raise RuntimeError("prior Unit body-status SQL drifted")
    if not visual_only_aware:
        return sql
    return sql.replace(
        _LEGACY_BODY_STATUS,
        _VISUAL_AWARE_BODY_STATUS,
        1,
    )
