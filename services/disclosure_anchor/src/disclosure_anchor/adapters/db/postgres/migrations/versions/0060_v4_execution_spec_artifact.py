"""Atomically bind bounded execution bytes to H0; historical backfill is explicit.

Revision ID: 0060_v4_execution_spec
Revises: 0059_v4_delayed_snapshot

Stop/drain old staged writers before upgrade. Existing rows await the bounded
offline backfill before 0061 validation. No existing canonical bytes are changed.
"""

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE, FUTURE_L2_READER_ROLE, OPS_SCHEMA, READER_ROLE,
)

revision = "0060_v4_execution_spec"
down_revision = "0059_v4_delayed_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "remote_parse_v4_execution_spec",
        sa.Column("attempt_id", sa.String(64), primary_key=True),
        sa.Column("fence_identity", sa.String(128), nullable=False),
        sa.Column("preparation_intent_sha256", sa.String(71), nullable=False),
        sa.Column("execution_spec_sha256", sa.String(71), nullable=False),
        sa.Column("execution_spec_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("execution_spec_byte_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("attempt_id", "fence_identity", "preparation_intent_sha256",
                            name="uq_v4_execution_spec_preparation"),
        sa.CheckConstraint(
            "execution_spec_sha256 ~ '^sha256:[0-9a-f]{64}$' AND "
            "preparation_intent_sha256 ~ '^sha256:[0-9a-f]{64}$' AND "
            "execution_spec_byte_count=octet_length(execution_spec_bytes) AND "
            "execution_spec_byte_count BETWEEN 1 AND 524288",
            name="ck_v4_execution_spec_identity",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "fence_identity"],
            [f"{OPS_SCHEMA}.remote_parse_attempt.attempt_id",
             f"{OPS_SCHEMA}.remote_parse_attempt.fence_identity"],
            name="fk_v4_execution_spec_parent", onupdate="RESTRICT", ondelete="RESTRICT",
            deferrable=True, initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "preparation_intent_sha256"],
            [f"{OPS_SCHEMA}.remote_parse_v4_evidence.attempt_id",
             f"{OPS_SCHEMA}.remote_parse_v4_evidence.evidence_sha256"],
            name="fk_v4_execution_spec_preparation", onupdate="RESTRICT", ondelete="RESTRICT",
            deferrable=True, initially="DEFERRED",
        ),
        schema=OPS_SCHEMA,
    )
    op.execute(f"""
        ALTER TABLE {OPS_SCHEMA}.remote_parse_v4_checkpoint
        ADD CONSTRAINT fk_v4_checkpoint_execution_spec
        FOREIGN KEY (attempt_id,fence_identity,preparation_intent_sha256)
        REFERENCES {OPS_SCHEMA}.remote_parse_v4_execution_spec
            (attempt_id,fence_identity,preparation_intent_sha256)
        ON UPDATE RESTRICT ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED NOT VALID
    """)
    op.execute(f"""
        CREATE TRIGGER ck_v4_execution_spec_immutable BEFORE UPDATE OR DELETE
        ON {OPS_SCHEMA}.remote_parse_v4_execution_spec FOR EACH ROW
        EXECUTE FUNCTION {OPS_SCHEMA}.reject_remote_parse_v4_immutable_change()
    """)
    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.require_v4_execution_spec_closure()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v4_evidence e
            JOIN {OPS_SCHEMA}.remote_parse_v4_checkpoint h
              ON h.attempt_id=e.attempt_id AND h.fence_identity=e.fence_identity
             AND h.preparation_intent_sha256=e.evidence_sha256
            WHERE e.attempt_id=NEW.attempt_id AND e.fence_identity=NEW.fence_identity
              AND e.evidence_sha256=NEW.preparation_intent_sha256
              AND e.evidence_kind='preparation_intent'
              AND h.lifecycle_version=0 AND h.state='prepared'
          ) THEN
            RAISE EXCEPTION 'v4 execution spec lacks exact resourceful H0 closure';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(f"""
        CREATE CONSTRAINT TRIGGER ck_v4_execution_spec_closure AFTER INSERT
        ON {OPS_SCHEMA}.remote_parse_v4_execution_spec
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION {OPS_SCHEMA}.require_v4_execution_spec_closure()
    """)
    op.execute(f"REVOKE ALL ON {OPS_SCHEMA}.remote_parse_v4_execution_spec FROM PUBLIC, {READER_ROLE}, {FUTURE_L2_READER_ROLE}, {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON {OPS_SCHEMA}.remote_parse_v4_execution_spec TO {APP_ROLE}")


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
        f"SELECT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v4_execution_spec)"
    )).scalar_one():
        raise RuntimeError("0060 downgrade would discard immutable execution specs")
    op.drop_constraint("fk_v4_checkpoint_execution_spec", "remote_parse_v4_checkpoint", schema=OPS_SCHEMA)
    op.drop_table("remote_parse_v4_execution_spec", schema=OPS_SCHEMA)
    op.execute(f"DROP FUNCTION {OPS_SCHEMA}.require_v4_execution_spec_closure()")
