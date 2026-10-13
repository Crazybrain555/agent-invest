"""Let the explicit parse requeue name a managed expired-prepared closure.

Revision ID: 0065_requeue_key_lifetime
Revises: 0064_retained_registration

The managed closure of an expired, never-submitted prepared V4 obligation fails
its run with retry class ``original_key_lifetime`` (error ``original_key_expired``).
The parse queue already keeps every non-automatic class out until a decision
names the failed run. This revision only widens the decision table's closed
class set (0063) by that one class, so the existing explicit, append-only
parse-requeue can record such a decision. No row, run, grant or view changes;
nothing is released automatically, and an unknown class is still refused.
Downgrade refuses while a decision records the added class rather than
deleting or reinterpreting it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import OPS_SCHEMA

revision: str = "0065_requeue_key_lifetime"
down_revision: Union[str, None] = "0064_retained_registration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "parse_requeue_decision"
_CONSTRAINT = "ck_parse_requeue_decision_class"
# 0063's closed set, unchanged; the added class is the only widening.
CONTRACT_CLASSES_0063 = (
    "provider_artifact_contract",
    "provider_protocol",
    "provider_runaway",
    "provider_terminal",
    "semantic_route_contract",
)
ADDED_CLASS = "original_key_lifetime"


def class_check_sql(classes: Sequence[str]) -> str:
    """The CHECK body in 0063's exact form: sorted, quoted, no spaces."""

    return "failure_retry_budget_class IN (" + ",".join(f"'{name}'" for name in sorted(classes)) + ")"


def _replace_class_check_sql(classes: Sequence[str]) -> str:
    # One statement: the table is never without its closed class CHECK.
    return (
        f"ALTER TABLE {OPS_SCHEMA}.{_TABLE} DROP CONSTRAINT {_CONSTRAINT}, "
        f"ADD CONSTRAINT {_CONSTRAINT} CHECK ({class_check_sql(classes)})"
    )


def _guard_added_class_history() -> None:
    exists = op.get_bind().execute(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.{_TABLE} "
            "WHERE failure_retry_budget_class = :added)"
        ),
        {"added": ADDED_CLASS},
    ).scalar_one()
    if exists:
        raise RuntimeError(
            "0065 downgrade would strand original_key_lifetime requeue decisions"
        )


def upgrade() -> None:
    op.execute(_replace_class_check_sql((*CONTRACT_CLASSES_0063, ADDED_CLASS)))


def downgrade() -> None:
    _guard_added_class_history()
    op.execute(_replace_class_check_sql(CONTRACT_CLASSES_0063))
