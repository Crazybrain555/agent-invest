"""Permit an owned snapshot receipt on the prepared-to-reconciling edge.

Revision ID: 0059_v4_delayed_snapshot
Revises: 0058_v4_supersession_stage
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import OPS_SCHEMA


revision: str = "0059_v4_delayed_snapshot"
down_revision: Union[str, None] = "0058_v4_supersession_stage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_HELD_CREDITS = (
    "documents",
    "snapshot_items",
    "snapshot_bytes",
    "remote_waits",
    "provider_tasks",
    "provider_result_bytes",
    "materialization_items",
    "compressed_bytes",
    "decoded_bytes",
    "temp_disk_bytes",
    "output_items",
    "output_bytes",
    "output_pages",
    "ack_items",
)
_EVIDENCE_KINDS = (
    "preparation_intent",
    "snapshot_receipt",
    "submission_intent",
    "accepted_submission",
    "terminal_receipt",
    "materialization_intent",
    "local_materialization_receipt",
    "failure_receipt",
    "supersession_receipt",
    "cleanup_plan",
    "cleanup_receipt",
    "ack_receipt",
)
_EVIDENCE_COLUMNS = tuple(f"{kind}_sha256" for kind in _EVIDENCE_KINDS)
_V4_STATE_TRANSITIONS = {
    "prepared": ("reconciling", "cleanup_pending"),
    "reconciling": ("submitted", "cleanup_pending"),
    "submitted": ("remote_terminal", "cleanup_pending"),
    "remote_terminal": ("materializing", "cleanup_pending"),
    "materializing": ("local_materialized", "cleanup_pending"),
    "local_materialized": ("publish_committed", "cleanup_pending"),
    "publish_committed": ("cleanup_pending",),
    "cleanup_pending": ("ack_pending", "pre_submission_failed", "superseded"),
    "ack_pending": ("acked", "remote_failed", "local_failed", "superseded"),
}


def _sql_values(values: Sequence[str]) -> str:
    return ",".join(f"'{value}'" for value in values)


def _allowed_new_evidence_by_transition(
    *, allow_delayed_snapshot: bool
) -> dict[tuple[str, str], tuple[str, ...]]:
    prepared_evidence = ["submission_intent_sha256"]
    if allow_delayed_snapshot:
        prepared_evidence.insert(0, "snapshot_receipt_sha256")
    return {
        ("prepared", "reconciling"): tuple(prepared_evidence),
        ("reconciling", "submitted"): ("accepted_submission_sha256",),
        ("submitted", "remote_terminal"): ("terminal_receipt_sha256",),
        ("remote_terminal", "materializing"): (
            "materialization_intent_sha256",
        ),
        ("materializing", "local_materialized"): (
            "local_materialization_receipt_sha256",
        ),
        ("local_materialized", "publish_committed"): (
            "publication_winner_sha256",
        ),
        **{
            (state, "cleanup_pending"): (
                "failure_receipt_sha256",
                "supersession_receipt_sha256",
                "cleanup_plan_sha256",
            )
            for state in (
                "prepared",
                "reconciling",
                "submitted",
                "remote_terminal",
                "materializing",
                "local_materialized",
                "publish_committed",
            )
        },
        ("cleanup_pending", "ack_pending"): ("cleanup_receipt_sha256",),
        ("cleanup_pending", "pre_submission_failed"): (
            "cleanup_receipt_sha256",
        ),
        ("cleanup_pending", "superseded"): ("cleanup_receipt_sha256",),
        ("ack_pending", "acked"): ("ack_receipt_sha256",),
        ("ack_pending", "remote_failed"): ("ack_receipt_sha256",),
        ("ack_pending", "local_failed"): ("ack_receipt_sha256",),
        ("ack_pending", "superseded"): ("ack_receipt_sha256",),
    }


def _checkpoint_chain_function_sql(*, allow_delayed_snapshot: bool) -> str:
    allowed_transition = " OR ".join(
        f"(predecessor.state='{old}' AND NEW.state IN ({_sql_values(new)}))"
        for old, new in _V4_STATE_TRANSITIONS.items()
    )
    evidence_drift = " OR ".join(
        f"(predecessor.{name} IS NOT NULL AND "
        f"predecessor.{name} IS DISTINCT FROM NEW.{name})"
        for name in (*_EVIDENCE_COLUMNS, "publication_winner_sha256")
    )
    cleanup_credit_drift = " OR ".join(
        f"predecessor.held_{name} IS DISTINCT FROM NEW.held_{name}"
        for name in _HELD_CREDITS
    )
    source_count_drift = (
        "predecessor.source_byte_count IS DISTINCT FROM NEW.source_byte_count "
        "OR predecessor.source_page_count IS DISTINCT FROM NEW.source_page_count"
    )
    allowed_evidence = _allowed_new_evidence_by_transition(
        allow_delayed_snapshot=allow_delayed_snapshot
    )
    unexpected_evidence_addition = " OR ".join(
        "(predecessor.state='{}' AND NEW.state='{}' AND ({}))".format(
            old,
            new,
            " OR ".join(
                f"(predecessor.{name} IS NULL AND NEW.{name} IS NOT NULL)"
                for name in (*_EVIDENCE_COLUMNS, "publication_winner_sha256")
                if name not in allowed
            ),
        )
        for (old, new), allowed in allowed_evidence.items()
    )
    return f"""
        CREATE OR REPLACE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v4_checkpoint_chain()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE predecessor {OPS_SCHEMA}.remote_parse_v4_checkpoint%ROWTYPE;
        BEGIN
          IF NEW.lifecycle_version > 0 THEN
            SELECT * INTO predecessor
              FROM {OPS_SCHEMA}.remote_parse_v4_checkpoint
             WHERE attempt_id=NEW.attempt_id
               AND lifecycle_version=NEW.lifecycle_version-1;
            IF NOT FOUND
               OR predecessor.checkpoint_sha256 IS DISTINCT FROM NEW.previous_checkpoint_sha256
            THEN
              RAISE EXCEPTION 'remote parse v4 checkpoint predecessor is not exact';
            END IF;
            IF NOT ({allowed_transition}) THEN
              RAISE EXCEPTION 'remote parse v4 checkpoint state transition is invalid';
            END IF;
            IF {evidence_drift} THEN
              RAISE EXCEPTION 'remote parse v4 checkpoint discarded immutable evidence';
            END IF;
            IF {source_count_drift} THEN
              RAISE EXCEPTION 'remote parse v4 checkpoint source counts drifted';
            END IF;
            IF {unexpected_evidence_addition} THEN
              RAISE EXCEPTION 'remote parse v4 checkpoint introduced unexpected evidence';
            END IF;
            IF NEW.state='cleanup_pending' AND ({cleanup_credit_drift}) THEN
              RAISE EXCEPTION 'remote parse v4 cleanup checkpoint changed held credit';
            END IF;
          END IF;
          RETURN NEW;
        END $$
    """


def upgrade() -> None:
    op.execute(_checkpoint_chain_function_sql(allow_delayed_snapshot=True))


def _guard_delayed_snapshot_history() -> None:
    exists = op.get_bind().execute(
        sa.text(
            f"""
            SELECT EXISTS (
              SELECT 1
                FROM {OPS_SCHEMA}.remote_parse_v4_checkpoint AS successor
                JOIN {OPS_SCHEMA}.remote_parse_v4_checkpoint AS predecessor
                  ON predecessor.attempt_id = successor.attempt_id
                 AND predecessor.lifecycle_version = successor.lifecycle_version - 1
               WHERE predecessor.state = 'prepared'
                 AND successor.state = 'reconciling'
                 AND predecessor.snapshot_receipt_sha256 IS NULL
                 AND successor.snapshot_receipt_sha256 IS NOT NULL
            )
            """
        )
    ).scalar_one()
    if exists:
        raise RuntimeError(
            "0059 downgrade would strand delayed snapshot authority"
        )


def downgrade() -> None:
    _guard_delayed_snapshot_history()
    op.execute(_checkpoint_chain_function_sql(allow_delayed_snapshot=False))
