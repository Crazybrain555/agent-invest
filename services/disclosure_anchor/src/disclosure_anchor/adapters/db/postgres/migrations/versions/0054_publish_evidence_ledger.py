"""Add private durable publish evidence ledger and progress relay head.

Revision ID: 0054_publish_evidence_ledger
Revises: 0053_remote_parse_checkpoint
Create Date: 2026-08-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE, CORE_SCHEMA, FUTURE_L2_READER_ROLE, OPS_SCHEMA, READER_ROLE,
)

revision: str = "0054_publish_evidence_ledger"
down_revision: Union[str, None] = "0053_remote_parse_checkpoint"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_HASH = r"^sha256:[0-9a-f]{64}$"


def upgrade() -> None:
    ledger_sequence = sa.Sequence("durable_publish_ledger_seq", schema=OPS_SCHEMA)
    op.execute(sa.schema.CreateSequence(ledger_sequence))
    op.create_unique_constraint(
        "uq_processing_run_publish_evidence_owner", "processing_run",
        ["processing_run_id", "document_id"], schema=CORE_SCHEMA,
    )
    op.create_table(
        "durable_publish_base",
        sa.Column(
            "ledger_seq", sa.BigInteger(), nullable=False, unique=True,
            server_default=ledger_sequence.next_value(),
        ),
        sa.Column("processing_run_id", sa.String(64), primary_key=True),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("source_identity_sha256", sa.String(71), nullable=False),
        sa.Column("source_page_count", sa.Integer(), nullable=False),
        sa.Column("publish_precommit_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["processing_run_id", "document_id"],
            [f"{CORE_SCHEMA}.processing_run.processing_run_id", f"{CORE_SCHEMA}.processing_run.document_id"],
            name="fk_durable_publish_base_run_owner", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(f"source_identity_sha256 ~ '{_HASH}'", name="ck_durable_publish_base_source"),
        sa.CheckConstraint("source_page_count > 0", name="ck_durable_publish_base_pages"),
        schema=OPS_SCHEMA,
    )
    op.create_index(
        "ix_durable_publish_base_source_order", "durable_publish_base",
        ["source_identity_sha256", "ledger_seq"], schema=OPS_SCHEMA,
    )
    op.create_index(
        "ix_durable_publish_base_commit", "durable_publish_base",
        ["publish_precommit_at", "ledger_seq"], schema=OPS_SCHEMA,
    )
    op.create_table(
        "durable_publish_supplement",
        sa.Column("supplement_id", sa.String(64), primary_key=True),
        sa.Column("processing_run_id", sa.String(64), nullable=False),
        sa.Column("source_identity_sha256", sa.String(71), nullable=False),
        sa.Column("source_page_count", sa.Integer(), nullable=False),
        sa.Column("publish_precommit_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("host_assignment_identity_sha256", sa.String(71), nullable=False),
        sa.Column("boot_identity_sha256", sa.String(71), nullable=False),
        sa.Column("runtime_bundle_identity_sha256", sa.String(71), nullable=False),
        sa.Column("process_profile_sha256", sa.String(71), nullable=False),
        sa.Column("observer_run_id", sa.String(64), nullable=False),
        sa.Column("observer_receipt_sha256", sa.String(71), nullable=False),
        sa.Column("observer_seal_sha256", sa.String(71), nullable=False),
        sa.Column("observer_contract_version", sa.String(64), nullable=False),
        sa.Column("publish_durable_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["processing_run_id"], [f"{OPS_SCHEMA}.durable_publish_base.processing_run_id"],
            name="fk_durable_publish_supplement_base", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            " AND ".join(f"{column} ~ '{_HASH}'" for column in (
                "source_identity_sha256", "host_assignment_identity_sha256",
                "boot_identity_sha256", "runtime_bundle_identity_sha256", "process_profile_sha256",
                "observer_receipt_sha256", "observer_seal_sha256",
            )), name="ck_durable_publish_supplement_hashes",
        ),
        sa.CheckConstraint(
            "observer_run_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'",
            name="ck_durable_publish_supplement_observer_run",
        ),
        sa.CheckConstraint(
            "supplement_id ~ '^pes_[0-9A-HJKMNP-TV-Z]{26}$'",
            name="ck_durable_publish_supplement_id",
        ),
        sa.CheckConstraint("observer_contract_version = 'mineru.synchronized-telemetry-receipt.v2'", name="ck_durable_publish_supplement_contract"),
        sa.CheckConstraint("source_page_count > 0", name="ck_durable_publish_supplement_pages"),
        sa.CheckConstraint("publish_durable_observed_at >= publish_precommit_at", name="ck_durable_publish_supplement_time"),
        schema=OPS_SCHEMA,
    )
    op.create_index(
        "ix_durable_publish_supplement_run", "durable_publish_supplement",
        ["processing_run_id", "created_at", "supplement_id"], schema=OPS_SCHEMA,
    )
    op.create_table(
        "progress_relay_head",
        sa.Column("relay_id", sa.String(128), nullable=False),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.Column("previous_checkpoint_sha256", sa.String(71), nullable=True),
        sa.Column("checkpoint_sha256", sa.String(71), nullable=False),
        sa.Column("checkpoint_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("checkpoint_byte_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("relay_id", "row_version"),
        sa.CheckConstraint("row_version >= 0 AND ((row_version = 0 AND previous_checkpoint_sha256 IS NULL) OR (row_version > 0 AND previous_checkpoint_sha256 ~ '^sha256:[0-9a-f]{64}$'))", name="ck_progress_relay_head_version"),
        sa.CheckConstraint(
            f"checkpoint_sha256 ~ '{_HASH}' AND checkpoint_byte_count = octet_length(checkpoint_bytes) "
            "AND checkpoint_byte_count BETWEEN 1 AND 1048576",
            name="ck_progress_relay_head_identity",
        ),
        sa.CheckConstraint(
            "relay_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}:sha256:[0-9a-f]{64}$'",
            name="ck_progress_relay_head_relay_id",
        ),
        schema=OPS_SCHEMA,
    )
    for table in ("durable_publish_base", "durable_publish_supplement", "progress_relay_head"):
        op.execute(f"REVOKE ALL ON {OPS_SCHEMA}.{table} FROM PUBLIC, {READER_ROLE}, {FUTURE_L2_READER_ROLE}")
    for table in ("durable_publish_base", "durable_publish_supplement"):
        op.execute(f"GRANT SELECT, INSERT ON {OPS_SCHEMA}.{table} TO {APP_ROLE}")
    op.execute(
        f"REVOKE ALL ON SEQUENCE {OPS_SCHEMA}.durable_publish_ledger_seq "
        f"FROM PUBLIC, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
    )
    op.execute(
        f"GRANT USAGE, SELECT ON SEQUENCE {OPS_SCHEMA}.durable_publish_ledger_seq "
        f"TO {APP_ROLE}"
    )
    op.execute(f"GRANT SELECT, INSERT ON {OPS_SCHEMA}.progress_relay_head TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("progress_relay_head", schema=OPS_SCHEMA)
    op.drop_index("ix_durable_publish_supplement_run", table_name="durable_publish_supplement", schema=OPS_SCHEMA)
    op.drop_table("durable_publish_supplement", schema=OPS_SCHEMA)
    op.drop_index("ix_durable_publish_base_commit", table_name="durable_publish_base", schema=OPS_SCHEMA)
    op.drop_index("ix_durable_publish_base_source_order", table_name="durable_publish_base", schema=OPS_SCHEMA)
    op.drop_table("durable_publish_base", schema=OPS_SCHEMA)
    op.execute(
        sa.schema.DropSequence(
            sa.Sequence("durable_publish_ledger_seq", schema=OPS_SCHEMA)
        )
    )
    op.drop_constraint(
        "uq_processing_run_publish_evidence_owner", "processing_run",
        schema=CORE_SCHEMA, type_="unique",
    )
