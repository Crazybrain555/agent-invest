"""Add private durable staged-parse attempt and resume-secret checkpoints.

Revision ID: 0053_remote_parse_checkpoint
Revises: 0052_publish_kpi_indexes
Create Date: 2026-08-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    FUTURE_L2_READER_ROLE,
    OPS_SCHEMA,
    READER_ROLE,
)

revision: str = "0053_remote_parse_checkpoint"
down_revision: Union[str, None] = "0052_publish_kpi_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATES = (
    "prepared", "submitted", "remote_terminal", "materializing",
    "local_materialized", "finish_committed", "acked", "remote_failed",
    "local_failed", "superseded",
)
def upgrade() -> None:
    op.create_unique_constraint(
        "uq_processing_run_remote_attempt_owner",
        "processing_run",
        ["processing_run_id", "document_id", "input_raw_file_hash"],
        schema=CORE_SCHEMA,
    )
    op.create_table(
        "remote_parse_attempt",
        sa.Column("attempt_id", sa.String(64), primary_key=True),
        sa.Column("processing_run_id", sa.String(64), nullable=False),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("attempt_generation", sa.Integer(), nullable=False),
        sa.Column("fence_identity", sa.String(128), nullable=False),
        sa.Column("source_pdf_sha256", sa.String(71), nullable=False),
        sa.Column("parser_target_sha256", sa.String(71), nullable=False),
        sa.Column("request_sha256", sa.String(71), nullable=False),
        sa.Column("runtime_epoch_sha256", sa.String(71), nullable=False),
        sa.Column("client_submit_key", sa.String(128), nullable=False, unique=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("row_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("remote_task_identity", sa.String(1024), nullable=True),
        sa.Column("terminal_receipt_sha256", sa.String(71), nullable=True),
        sa.Column("terminal_receipt_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("terminal_receipt_byte_count", sa.Integer(), nullable=True),
        sa.Column("result_owner_identity", sa.String(1024), nullable=True),
        sa.Column("result_artifact_sha256", sa.String(71), nullable=True),
        sa.Column("result_artifact_bytes", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["processing_run_id", "document_id", "source_pdf_sha256"],
            [
                f"{CORE_SCHEMA}.processing_run.processing_run_id",
                f"{CORE_SCHEMA}.processing_run.document_id",
                f"{CORE_SCHEMA}.processing_run.input_raw_file_hash",
            ],
            name="fk_remote_parse_attempt_run_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["document_id"], [f"{CORE_SCHEMA}.document.document_id"], ondelete="CASCADE"),
        sa.CheckConstraint("attempt_generation >= 1 AND row_version >= 0", name="ck_remote_parse_attempt_versions"),
        sa.CheckConstraint("source_pdf_sha256 ~ '^sha256:[0-9a-f]{64}$' AND parser_target_sha256 ~ '^sha256:[0-9a-f]{64}$' AND request_sha256 ~ '^sha256:[0-9a-f]{64}$' AND runtime_epoch_sha256 ~ '^sha256:[0-9a-f]{64}$'", name="ck_remote_parse_attempt_hashes"),
        sa.CheckConstraint("state IN (" + ",".join(f"'{value}'" for value in _STATES) + ")", name="ck_remote_parse_attempt_state"),
        sa.CheckConstraint("(state IN ('prepared','submitted','remote_terminal','materializing','local_materialized','finish_committed') AND is_current) OR (state IN ('acked','remote_failed','local_failed','superseded') AND NOT is_current)", name="ck_remote_parse_attempt_lifecycle_shape"),
        sa.CheckConstraint("(state = 'prepared' AND row_version = 0 AND remote_task_identity IS NULL) OR (state <> 'prepared' AND row_version >= 1)", name="ck_remote_parse_attempt_initial_shape"),
        sa.CheckConstraint("state <> 'submitted' OR remote_task_identity IS NOT NULL", name="ck_remote_parse_attempt_submitted_shape"),
        sa.CheckConstraint("(state IN ('remote_terminal','materializing','local_materialized','finish_committed','acked','local_failed') AND remote_task_identity IS NOT NULL AND terminal_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND terminal_receipt_bytes IS NOT NULL AND terminal_receipt_byte_count = octet_length(terminal_receipt_bytes) AND terminal_receipt_byte_count BETWEEN 1 AND 65536 AND result_owner_identity IS NOT NULL AND result_artifact_sha256 ~ '^sha256:[0-9a-f]{64}$' AND result_artifact_bytes > 0) OR (state IN ('prepared','submitted','remote_failed') AND terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL) OR (state = 'superseded' AND ((terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL) OR (remote_task_identity IS NOT NULL AND terminal_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND terminal_receipt_bytes IS NOT NULL AND terminal_receipt_byte_count = octet_length(terminal_receipt_bytes) AND terminal_receipt_byte_count BETWEEN 1 AND 65536 AND result_owner_identity IS NOT NULL AND result_artifact_sha256 ~ '^sha256:[0-9a-f]{64}$' AND result_artifact_bytes > 0)))", name="ck_remote_parse_attempt_terminal_shape"),
        sa.UniqueConstraint("document_id", "attempt_generation", name="uq_remote_parse_attempt_document_generation"),
        schema=OPS_SCHEMA,
    )
    op.create_index("uq_remote_parse_attempt_current_document", "remote_parse_attempt", ["document_id"], unique=True, schema=OPS_SCHEMA, postgresql_where=sa.text("is_current"))
    op.create_index("ix_remote_parse_attempt_recovery", "remote_parse_attempt", ["state", "updated_at", "attempt_id"], schema=OPS_SCHEMA)
    op.create_table(
        "remote_parse_resume_secret",
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("secret_kind", sa.String(16), nullable=False),
        sa.Column("token_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("token_sha256", sa.String(71), nullable=False),
        sa.Column("token_byte_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("attempt_id", "secret_kind"),
        sa.ForeignKeyConstraint(["attempt_id"], [f"{OPS_SCHEMA}.remote_parse_attempt.attempt_id"], ondelete="CASCADE"),
        sa.CheckConstraint("secret_kind IN ('submission','terminal','ack')", name="ck_remote_parse_resume_secret_kind"),
        sa.CheckConstraint("token_sha256 ~ '^sha256:[0-9a-f]{64}$' AND token_byte_count = octet_length(token_bytes) AND token_byte_count BETWEEN 1 AND 65536", name="ck_remote_parse_resume_secret_identity"),
        schema=OPS_SCHEMA,
    )
    for table in ("remote_parse_attempt", "remote_parse_resume_secret"):
        op.execute(f"REVOKE ALL ON {OPS_SCHEMA}.{table} FROM PUBLIC, {READER_ROLE}, {FUTURE_L2_READER_ROLE}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {OPS_SCHEMA}.{table} TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("remote_parse_resume_secret", schema=OPS_SCHEMA)
    op.drop_index("ix_remote_parse_attempt_recovery", table_name="remote_parse_attempt", schema=OPS_SCHEMA)
    op.drop_index("uq_remote_parse_attempt_current_document", table_name="remote_parse_attempt", schema=OPS_SCHEMA)
    op.drop_table("remote_parse_attempt", schema=OPS_SCHEMA)
    op.drop_constraint(
        "uq_processing_run_remote_attempt_owner",
        "processing_run",
        schema=CORE_SCHEMA,
        type_="unique",
    )
