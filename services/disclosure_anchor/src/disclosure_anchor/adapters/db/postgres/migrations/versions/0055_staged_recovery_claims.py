"""Add durable staged recovery claims and local/failure receipts.

Revision ID: 0055_staged_recovery_claims
Revises: 0054_publish_evidence_ledger
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from disclosure_anchor.adapters.db.postgres.schema import OPS_SCHEMA

revision: str = "0055_staged_recovery_claims"
down_revision: Union[str, None] = "0054_publish_evidence_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_V1_REMOTE_TERMINAL = "(state IN ('remote_terminal','materializing','local_materialized','finish_committed','acked','local_failed') AND remote_task_identity IS NOT NULL AND terminal_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND terminal_receipt_bytes IS NOT NULL AND terminal_receipt_byte_count=octet_length(terminal_receipt_bytes) AND terminal_receipt_byte_count BETWEEN 1 AND 65536 AND result_owner_identity IS NOT NULL AND result_artifact_sha256 ~ '^sha256:[0-9a-f]{64}$' AND result_artifact_bytes>0) OR (state IN ('prepared','submitted','remote_failed') AND terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL) OR (state='superseded' AND ((terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL) OR (remote_task_identity IS NOT NULL AND terminal_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND terminal_receipt_bytes IS NOT NULL AND terminal_receipt_byte_count=octet_length(terminal_receipt_bytes) AND terminal_receipt_byte_count BETWEEN 1 AND 65536 AND result_owner_identity IS NOT NULL AND result_artifact_sha256 ~ '^sha256:[0-9a-f]{64}$' AND result_artifact_bytes>0)))"
_V2_REMOTE_TERMINAL = "(state IN ('remote_terminal','materializing','local_materialized','finish_committed','local_failure_committed','acked','local_failed') AND remote_task_identity IS NOT NULL AND terminal_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND terminal_receipt_bytes IS NOT NULL AND terminal_receipt_byte_count=octet_length(terminal_receipt_bytes) AND terminal_receipt_byte_count BETWEEN 1 AND 65536 AND result_owner_identity IS NOT NULL AND result_artifact_sha256 ~ '^sha256:[0-9a-f]{64}$' AND result_artifact_bytes>0) OR (state IN ('prepared','reconciling','submitted','remote_failure_committed','remote_failed','pre_submission_failed') AND terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL AND ((state IN ('remote_failure_committed','remote_failed') AND remote_task_identity IS NOT NULL) OR (state NOT IN ('remote_failure_committed','remote_failed') AND (remote_task_identity IS NULL OR state='submitted')))) OR (state='superseded' AND remote_task_identity IS NULL AND terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL)"
_REMOTE_PARSE_TERMINAL_SHAPE = (
    f"(checkpoint_contract_version=1 AND ({_V1_REMOTE_TERMINAL})) OR "
    f"(checkpoint_contract_version=2 AND ({_V2_REMOTE_TERMINAL}))"
)

def upgrade() -> None:
    with op.batch_alter_table("remote_parse_resume_secret", schema=OPS_SCHEMA) as batch:
        batch.drop_constraint("ck_remote_parse_resume_secret_kind", type_="check")
        batch.alter_column("secret_kind", type_=sa.String(32))
        batch.add_column(
            sa.Column(
                "secret_contract_version", sa.Integer(), nullable=False,
                server_default="1",
            )
        )
        batch.alter_column("secret_contract_version", server_default="2")
        batch.create_check_constraint(
            "ck_remote_parse_resume_secret_kind",
            "(secret_contract_version=1 AND secret_kind IN ('submission','terminal','ack')) OR (secret_contract_version=2 AND secret_kind IN ('prepared_reconcile','accepted_submission','terminal'))",
        )
    with op.batch_alter_table("remote_parse_attempt", schema=OPS_SCHEMA) as batch:
        for name in (
            "ck_remote_parse_attempt_state", "ck_remote_parse_attempt_lifecycle_shape",
            "ck_remote_parse_attempt_initial_shape",
            "ck_remote_parse_attempt_submitted_shape",
            "ck_remote_parse_attempt_terminal_shape",
        ):
            batch.drop_constraint(name, type_="check")
        batch.add_column(sa.Column("claim_generation", sa.BigInteger(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("checkpoint_contract_version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("claim_owner_identity", sa.String(128)))
        batch.add_column(sa.Column("claim_lease_until", sa.DateTime(timezone=True)))
        for prefix in ("local_receipt", "failure_receipt"):
            batch.add_column(sa.Column(f"{prefix}_sha256", sa.String(71)))
            batch.add_column(sa.Column(f"{prefix}_bytes", sa.LargeBinary()))
            batch.add_column(sa.Column(f"{prefix}_byte_count", sa.Integer()))
        batch.add_column(sa.Column("failure_stage", sa.String(16)))
        for suffix, column_type in (
            ("sha256", sa.String(71)),
            ("bytes", sa.LargeBinary()),
            ("byte_count", sa.Integer()),
        ):
            batch.add_column(sa.Column(f"submitted_receipt_{suffix}", column_type))
        batch.alter_column("checkpoint_contract_version", server_default="2")
        states = "'prepared','reconciling','submitted','remote_terminal','materializing','local_materialized','finish_committed','remote_failure_committed','local_failure_committed','acked','remote_failed','local_failed','pre_submission_failed','superseded'"
        batch.create_check_constraint("ck_remote_parse_attempt_state", f"state IN ({states})")
        batch.create_check_constraint("ck_remote_parse_attempt_lifecycle_shape", "(state IN ('prepared','reconciling','submitted','remote_terminal','materializing','local_materialized','finish_committed','remote_failure_committed','local_failure_committed') AND is_current) OR (state IN ('acked','remote_failed','local_failed','pre_submission_failed','superseded') AND NOT is_current)")
        batch.create_check_constraint("ck_remote_parse_attempt_initial_shape", "(checkpoint_contract_version=1 AND ((state='prepared' AND row_version=0 AND remote_task_identity IS NULL) OR (state<>'prepared' AND row_version>=1))) OR (checkpoint_contract_version=2 AND ((state IN ('prepared','reconciling') AND remote_task_identity IS NULL) OR (state NOT IN ('prepared','reconciling') AND row_version>=1)))")
        batch.create_check_constraint("ck_remote_parse_attempt_contract_version", "checkpoint_contract_version IN (1,2) AND (checkpoint_contract_version=2 OR (claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL AND submitted_receipt_sha256 IS NULL AND submitted_receipt_bytes IS NULL AND submitted_receipt_byte_count IS NULL AND local_receipt_sha256 IS NULL AND local_receipt_bytes IS NULL AND local_receipt_byte_count IS NULL AND failure_receipt_sha256 IS NULL AND failure_receipt_bytes IS NULL AND failure_receipt_byte_count IS NULL AND failure_stage IS NULL AND state NOT IN ('remote_failure_committed','local_failure_committed','pre_submission_failed')))")
        batch.create_check_constraint("ck_remote_parse_attempt_submitted_shape", "(checkpoint_contract_version=1 AND (state <> 'submitted' OR remote_task_identity IS NOT NULL)) OR (checkpoint_contract_version=2 AND ((state IN ('prepared','reconciling','pre_submission_failed','superseded') AND submitted_receipt_sha256 IS NULL AND submitted_receipt_bytes IS NULL AND submitted_receipt_byte_count IS NULL) OR (state NOT IN ('prepared','reconciling','pre_submission_failed','superseded') AND remote_task_identity IS NOT NULL AND submitted_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND submitted_receipt_bytes IS NOT NULL AND submitted_receipt_byte_count=octet_length(submitted_receipt_bytes) AND submitted_receipt_byte_count BETWEEN 1 AND 65536)))")
        batch.create_check_constraint("ck_remote_parse_attempt_claim_shape", "(checkpoint_contract_version=1 AND claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (checkpoint_contract_version=2 AND state='prepared' AND ((claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (claim_generation>=1 AND claim_owner_identity IS NOT NULL AND claim_lease_until IS NOT NULL))) OR (checkpoint_contract_version=2 AND state IN ('reconciling','submitted','remote_terminal','materializing','local_materialized','finish_committed','remote_failure_committed','local_failure_committed') AND claim_generation>=1 AND claim_owner_identity IS NOT NULL AND claim_lease_until IS NOT NULL) OR (checkpoint_contract_version=2 AND state IN ('acked','remote_failed','local_failed','pre_submission_failed','superseded') AND claim_generation>=1 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL)")
        batch.create_check_constraint("ck_remote_parse_attempt_local_receipt", "checkpoint_contract_version=1 OR ((state IN ('local_materialized','finish_committed','acked') AND local_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND local_receipt_bytes IS NOT NULL AND local_receipt_byte_count=octet_length(local_receipt_bytes) AND local_receipt_byte_count BETWEEN 1 AND 65536) OR (state NOT IN ('local_materialized','finish_committed','acked') AND local_receipt_sha256 IS NULL AND local_receipt_bytes IS NULL AND local_receipt_byte_count IS NULL))")
        batch.create_check_constraint("ck_remote_parse_attempt_failure_receipt", "checkpoint_contract_version=1 OR ((state IN ('remote_failure_committed','remote_failed','pre_submission_failed') AND failure_stage='remote' AND failure_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND failure_receipt_bytes IS NOT NULL AND failure_receipt_byte_count=octet_length(failure_receipt_bytes) AND failure_receipt_byte_count BETWEEN 1 AND 65536) OR (state IN ('local_failure_committed','local_failed') AND failure_stage='local' AND failure_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND failure_receipt_bytes IS NOT NULL AND failure_receipt_byte_count=octet_length(failure_receipt_bytes) AND failure_receipt_byte_count BETWEEN 1 AND 65536) OR (state NOT IN ('remote_failure_committed','remote_failed','pre_submission_failed','local_failure_committed','local_failed') AND failure_receipt_sha256 IS NULL AND failure_receipt_bytes IS NULL AND failure_receipt_byte_count IS NULL AND failure_stage IS NULL))")
        batch.create_check_constraint("ck_remote_parse_attempt_terminal_shape", _REMOTE_PARSE_TERMINAL_SHAPE)
    op.create_index("ix_remote_parse_attempt_claim", "remote_parse_attempt", ["is_current", "claim_lease_until", "attempt_id"], schema=OPS_SCHEMA)

def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_attempt "
            "WHERE checkpoint_contract_version=2)"
        )
    ).scalar_one():
        raise RuntimeError("0055 downgrade would destroy v2 staged recovery evidence")
    if bind.execute(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_resume_secret "
            "WHERE secret_contract_version=2)"
        )
    ).scalar_one():
        raise RuntimeError("0055 downgrade would destroy v2 staged resume evidence")
    op.drop_index(
        "ix_remote_parse_attempt_claim",
        table_name="remote_parse_attempt",
        schema=OPS_SCHEMA,
    )
    with op.batch_alter_table("remote_parse_attempt", schema=OPS_SCHEMA) as batch:
        for name in (
            "ck_remote_parse_attempt_state",
            "ck_remote_parse_attempt_lifecycle_shape",
            "ck_remote_parse_attempt_initial_shape",
            "ck_remote_parse_attempt_submitted_shape",
            "ck_remote_parse_attempt_contract_version",
            "ck_remote_parse_attempt_claim_shape",
            "ck_remote_parse_attempt_local_receipt",
            "ck_remote_parse_attempt_failure_receipt",
            "ck_remote_parse_attempt_terminal_shape",
        ):
            batch.drop_constraint(name, type_="check")
        for name in (
            "failure_stage",
            "submitted_receipt_byte_count",
            "submitted_receipt_bytes",
            "submitted_receipt_sha256",
            "failure_receipt_byte_count",
            "failure_receipt_bytes",
            "failure_receipt_sha256",
            "local_receipt_byte_count",
            "local_receipt_bytes",
            "local_receipt_sha256",
            "claim_lease_until",
            "claim_owner_identity",
            "checkpoint_contract_version",
            "claim_generation",
        ):
            batch.drop_column(name)
        batch.create_check_constraint(
            "ck_remote_parse_attempt_state",
            "state IN ('prepared','submitted','remote_terminal','materializing','local_materialized','finish_committed','acked','remote_failed','local_failed','superseded')",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_lifecycle_shape",
            "(state IN ('prepared','submitted','remote_terminal','materializing','local_materialized','finish_committed') AND is_current) OR (state IN ('acked','remote_failed','local_failed','superseded') AND NOT is_current)",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_initial_shape",
            "(state = 'prepared' AND row_version = 0 AND remote_task_identity IS NULL) OR (state <> 'prepared' AND row_version >= 1)",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_submitted_shape",
            "state <> 'submitted' OR remote_task_identity IS NOT NULL",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_terminal_shape",
            "(state IN ('remote_terminal','materializing','local_materialized','finish_committed','acked','local_failed') AND remote_task_identity IS NOT NULL AND terminal_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND terminal_receipt_bytes IS NOT NULL AND terminal_receipt_byte_count = octet_length(terminal_receipt_bytes) AND terminal_receipt_byte_count BETWEEN 1 AND 65536 AND result_owner_identity IS NOT NULL AND result_artifact_sha256 ~ '^sha256:[0-9a-f]{64}$' AND result_artifact_bytes > 0) OR (state IN ('prepared','submitted','remote_failed') AND terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL) OR (state = 'superseded' AND ((terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL) OR (remote_task_identity IS NOT NULL AND terminal_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND terminal_receipt_bytes IS NOT NULL AND terminal_receipt_byte_count = octet_length(terminal_receipt_bytes) AND terminal_receipt_byte_count BETWEEN 1 AND 65536 AND result_owner_identity IS NOT NULL AND result_artifact_sha256 ~ '^sha256:[0-9a-f]{64}$' AND result_artifact_bytes > 0)))",
        )
    with op.batch_alter_table("remote_parse_resume_secret", schema=OPS_SCHEMA) as batch:
        batch.drop_constraint("ck_remote_parse_resume_secret_kind", type_="check")
        batch.drop_column("secret_contract_version")
        batch.alter_column("secret_kind", type_=sa.String(16))
        batch.create_check_constraint(
            "ck_remote_parse_resume_secret_kind",
            "secret_kind IN ('submission','terminal','ack')",
        )
