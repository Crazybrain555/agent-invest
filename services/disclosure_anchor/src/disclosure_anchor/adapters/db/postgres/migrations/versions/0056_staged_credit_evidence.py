"""Persist closed v3 staged credit and materialization evidence.

Revision ID: 0056_staged_credit_evidence
Revises: 0055_staged_recovery_claims
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import OPS_SCHEMA


revision: str = "0056_staged_credit_evidence"
down_revision: Union[str, None] = "0055_staged_recovery_claims"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CREDITS = (
    "documents", "remote_waits", "retained_results", "retained_bytes",
    "local_items", "compressed_bytes", "decoded_bytes", "temp_disk_bytes",
    "db_stage_items", "db_staged_bytes", "ack_items", "unpublished_pages",
)
_IDENTITY_COLUMNS = (
    "process_profile_sha256", "credit_policy_sha256",
    "reservation_input_sha256", "reservation_input_bytes",
    "reservation_input_byte_count", "reservation_source_byte_count",
    "reservation_source_page_count", "reservation_bucket",
)
_MATERIALIZATION_COLUMNS = (
    "materialization_receipt_sha256", "materialization_receipt_bytes",
    "materialization_receipt_byte_count", "materialization_source_page_count",
    "materialization_spool_relpath", "materialization_spool_sha256",
    "materialization_spool_byte_count", "materialization_compressed_byte_count",
    "materialization_uncompressed_byte_count",
    "materialization_temp_disk_byte_count", "materialization_decoded_byte_count",
    "materialization_member_count", "materialization_token_sha256",
)
_V1_TERMINAL = "(state IN ('remote_terminal','materializing','local_materialized','finish_committed','acked','local_failed') AND remote_task_identity IS NOT NULL AND terminal_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND terminal_receipt_bytes IS NOT NULL AND terminal_receipt_byte_count=octet_length(terminal_receipt_bytes) AND terminal_receipt_byte_count BETWEEN 1 AND 65536 AND result_owner_identity IS NOT NULL AND result_artifact_sha256 ~ '^sha256:[0-9a-f]{64}$' AND result_artifact_bytes>0) OR (state IN ('prepared','submitted','remote_failed') AND terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL) OR (state='superseded' AND ((terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL) OR (remote_task_identity IS NOT NULL AND terminal_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND terminal_receipt_bytes IS NOT NULL AND terminal_receipt_byte_count=octet_length(terminal_receipt_bytes) AND terminal_receipt_byte_count BETWEEN 1 AND 65536 AND result_owner_identity IS NOT NULL AND result_artifact_sha256 ~ '^sha256:[0-9a-f]{64}$' AND result_artifact_bytes>0)))"
_V2_TERMINAL = "(state IN ('remote_terminal','materializing','local_materialized','finish_committed','local_failure_committed','acked','local_failed') AND remote_task_identity IS NOT NULL AND terminal_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND terminal_receipt_bytes IS NOT NULL AND terminal_receipt_byte_count=octet_length(terminal_receipt_bytes) AND terminal_receipt_byte_count BETWEEN 1 AND 65536 AND result_owner_identity IS NOT NULL AND result_artifact_sha256 ~ '^sha256:[0-9a-f]{64}$' AND result_artifact_bytes>0) OR (state IN ('prepared','reconciling','submitted','remote_failure_committed','remote_failed','pre_submission_failed') AND terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL AND ((state IN ('remote_failure_committed','remote_failed') AND remote_task_identity IS NOT NULL) OR (state NOT IN ('remote_failure_committed','remote_failed') AND (remote_task_identity IS NULL OR state='submitted')))) OR (state='superseded' AND remote_task_identity IS NULL AND terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL)"
_V3_TERMINAL = _V2_TERMINAL.replace(
    "terminal_receipt_sha256 ~", "terminal_receipt_sha256 IS NOT NULL AND terminal_receipt_sha256 ~"
).replace(
    "terminal_receipt_byte_count=", "terminal_receipt_byte_count IS NOT NULL AND terminal_receipt_byte_count="
).replace(
    "result_artifact_sha256 ~", "result_artifact_sha256 IS NOT NULL AND result_artifact_sha256 ~"
).replace("result_artifact_bytes>0", "result_artifact_bytes IS NOT NULL AND result_artifact_bytes>0")
_TERMINAL_0055 = f"(checkpoint_contract_version=1 AND ({_V1_TERMINAL})) OR (checkpoint_contract_version=2 AND ({_V2_TERMINAL}))"


def _all_null(columns: Sequence[str]) -> str:
    return " AND ".join(f"{name} IS NULL" for name in columns)


def _all_present(columns: Sequence[str]) -> str:
    return " AND ".join(f"{name} IS NOT NULL" for name in columns)


def _zero(prefix: str) -> str:
    return " AND ".join(f"{prefix}_{name}=0" for name in _CREDITS)


def _shape(**values: str) -> str:
    return " AND ".join(
        f"current_{name}={values.get(name, '0')}" for name in _CREDITS
    )


_NON_V3_COLUMNS = (
    *_IDENTITY_COLUMNS,
    *(f"reservation_{name}" for name in _CREDITS),
    *(f"current_{name}" for name in _CREDITS),
    *_MATERIALIZATION_COLUMNS,
    "local_db_staged_byte_count",
)
_MATERIALIZATION_PRESENT = (
    "materialization_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND "
    "materialization_receipt_bytes IS NOT NULL AND "
    "materialization_receipt_byte_count=octet_length(materialization_receipt_bytes) AND "
    "materialization_receipt_byte_count BETWEEN 1 AND 65536 AND "
    + _all_present(_MATERIALIZATION_COLUMNS[3:])
    + " AND materialization_source_page_count>0 AND materialization_spool_byte_count>0 "
    "AND materialization_compressed_byte_count=materialization_spool_byte_count "
    "AND materialization_uncompressed_byte_count>0 AND materialization_decoded_byte_count>0 "
    "AND materialization_temp_disk_byte_count="
    "materialization_spool_byte_count+materialization_uncompressed_byte_count "
    "AND materialization_member_count>0 AND materialization_spool_relpath !~ '(^/|(^|/)\\.\\.?(/|$)|\\\\)' "
    "AND materialization_spool_sha256 ~ '^sha256:[0-9a-f]{64}$' "
    "AND materialization_token_sha256 ~ '^sha256:[0-9a-f]{64}$'"
)


def upgrade() -> None:
    op.create_table(
        "remote_parse_v3_resume_secret",
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("secret_kind", sa.String(32), nullable=False),
        sa.Column("token_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("token_sha256", sa.String(71), nullable=False),
        sa.Column("token_byte_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["attempt_id"], [f"{OPS_SCHEMA}.remote_parse_attempt.attempt_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id", "secret_kind"),
        sa.CheckConstraint(
            "secret_kind IN ('prepared_reconcile','accepted_submission','terminal','materialization')",
            name="ck_remote_parse_v3_resume_secret_kind",
        ),
        sa.CheckConstraint(
            "token_sha256 ~ '^sha256:[0-9a-f]{64}$' AND token_byte_count=octet_length(token_bytes) AND token_byte_count BETWEEN 1 AND 65536",
            name="ck_remote_parse_v3_resume_secret_identity",
        ),
        schema=OPS_SCHEMA,
    )
    op.execute(f"REVOKE ALL ON TABLE {OPS_SCHEMA}.remote_parse_v3_resume_secret FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {OPS_SCHEMA}.remote_parse_v3_resume_secret TO disclosure_app")

    with op.batch_alter_table("remote_parse_attempt", schema=OPS_SCHEMA) as batch:
        for name in (
            "ck_remote_parse_attempt_contract_version",
            "ck_remote_parse_attempt_initial_shape",
            "ck_remote_parse_attempt_claim_shape",
            "ck_remote_parse_attempt_local_receipt",
            "ck_remote_parse_attempt_failure_receipt",
            "ck_remote_parse_attempt_submitted_shape",
            "ck_remote_parse_attempt_terminal_shape",
        ):
            batch.drop_constraint(name, type_="check")
        for name in ("process_profile_sha256", "credit_policy_sha256", "reservation_input_sha256"):
            batch.add_column(sa.Column(name, sa.String(71)))
        batch.add_column(sa.Column("reservation_input_bytes", sa.LargeBinary()))
        batch.add_column(sa.Column("reservation_input_byte_count", sa.Integer()))
        batch.add_column(sa.Column("reservation_source_byte_count", sa.BigInteger()))
        batch.add_column(sa.Column("reservation_source_page_count", sa.BigInteger()))
        batch.add_column(sa.Column("reservation_bucket", sa.String(16)))
        for prefix in ("reservation", "current"):
            for name in _CREDITS:
                batch.add_column(sa.Column(f"{prefix}_{name}", sa.BigInteger()))
        for name in ("materialization_receipt_sha256", "materialization_spool_sha256", "materialization_token_sha256"):
            batch.add_column(sa.Column(name, sa.String(71)))
        batch.add_column(sa.Column("materialization_receipt_bytes", sa.LargeBinary()))
        for name in (
            "materialization_receipt_byte_count", "materialization_source_page_count",
            "materialization_spool_byte_count", "materialization_compressed_byte_count",
            "materialization_uncompressed_byte_count", "materialization_temp_disk_byte_count",
            "materialization_decoded_byte_count", "materialization_member_count",
            "local_db_staged_byte_count",
        ):
            batch.add_column(sa.Column(name, sa.BigInteger()))
        batch.add_column(sa.Column("materialization_spool_relpath", sa.String(1024)))

        batch.create_check_constraint(
            "ck_remote_parse_attempt_contract_version",
            "checkpoint_contract_version IN (1,2,3) AND "
            f"((checkpoint_contract_version<3 AND {_all_null(_NON_V3_COLUMNS)}) OR "
            "(checkpoint_contract_version=3 AND "
            f"{_all_present(_IDENTITY_COLUMNS)} AND "
            "process_profile_sha256 ~ '^sha256:[0-9a-f]{64}$' AND "
            "credit_policy_sha256 ~ '^sha256:[0-9a-f]{64}$' AND "
            "reservation_input_sha256 ~ '^sha256:[0-9a-f]{64}$' AND "
            "reservation_input_byte_count=octet_length(reservation_input_bytes) AND "
            "reservation_input_byte_count BETWEEN 1 AND 65536 AND "
            "reservation_source_byte_count>0 AND reservation_source_page_count>0 AND "
            "reservation_bucket IN ('regular','heavy','huge') AND "
            f"{_all_present(tuple(f'reservation_{n}' for n in _CREDITS) + tuple(f'current_{n}' for n in _CREDITS))}))",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_credit_bounds",
            "checkpoint_contract_version<3 OR ("
            + " AND ".join(
                f"reservation_{n}>=0 AND current_{n}>=0 AND current_{n}<=reservation_{n}"
                for n in _CREDITS
            )
            + ")",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_final_zero",
            "checkpoint_contract_version<3 OR state NOT IN "
            "('acked','remote_failed','local_failed','pre_submission_failed','superseded') OR "
            f"({_zero('current')})",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_materialization",
            "checkpoint_contract_version<3 OR "
            f"(state IN ('prepared','reconciling','submitted','remote_terminal','remote_failure_committed','remote_failed','pre_submission_failed','superseded') AND {_all_null(_MATERIALIZATION_COLUMNS)}) OR "
            f"(state IN ('materializing','local_materialized','finish_committed','acked') AND {_MATERIALIZATION_PRESENT}) OR "
            f"(state IN ('local_failure_committed','local_failed') AND (({_all_null(_MATERIALIZATION_COLUMNS)}) OR ({_MATERIALIZATION_PRESENT})))",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_local_projection",
            "checkpoint_contract_version<3 OR ((local_receipt_bytes IS NULL AND local_db_staged_byte_count IS NULL) "
            "OR (local_receipt_bytes IS NOT NULL AND local_db_staged_byte_count>0))",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_state_credit",
            "checkpoint_contract_version<3 OR "
            f"(state='prepared' AND {_shape(documents='1')}) OR "
            f"(state IN ('reconciling','submitted') AND {_shape(documents='1', remote_waits='1')}) OR "
            f"(state='remote_terminal' AND {_shape(documents='1', retained_results='1', retained_bytes='result_artifact_bytes')}) OR "
            f"(state='materializing' AND {_shape(documents='1', retained_results='1', retained_bytes='result_artifact_bytes', local_items='1', compressed_bytes='materialization_compressed_byte_count', decoded_bytes='materialization_decoded_byte_count', temp_disk_bytes='materialization_temp_disk_byte_count')}) OR "
            f"(state='local_materialized' AND {_shape(documents='1', retained_results='1', retained_bytes='result_artifact_bytes', db_stage_items='1', db_staged_bytes='local_db_staged_byte_count', unpublished_pages='reservation_source_page_count')}) OR "
            f"(state='finish_committed' AND {_shape(documents='1', retained_results='1', retained_bytes='result_artifact_bytes', ack_items='1')}) OR "
            f"(state='remote_failure_committed' AND {_shape(documents='1', retained_results='1', ack_items='1')}) OR "
            f"(state='local_failure_committed' AND materialization_receipt_bytes IS NULL AND {_shape(documents='1', retained_results='1', retained_bytes='result_artifact_bytes', ack_items='1')}) OR "
            f"(state='local_failure_committed' AND materialization_receipt_bytes IS NOT NULL AND local_receipt_bytes IS NULL AND {_shape(documents='1', retained_results='1', retained_bytes='result_artifact_bytes', local_items='1', compressed_bytes='materialization_compressed_byte_count', decoded_bytes='materialization_decoded_byte_count', temp_disk_bytes='materialization_temp_disk_byte_count', ack_items='1')}) OR "
            f"(state='local_failure_committed' AND local_receipt_bytes IS NOT NULL AND {_shape(documents='1', retained_results='1', retained_bytes='result_artifact_bytes', local_items='1', compressed_bytes='materialization_compressed_byte_count', decoded_bytes='materialization_decoded_byte_count', temp_disk_bytes='materialization_temp_disk_byte_count', db_stage_items='1', db_staged_bytes='local_db_staged_byte_count', ack_items='1', unpublished_pages='reservation_source_page_count')}) OR "
            f"(state IN ('acked','remote_failed','local_failed','pre_submission_failed','superseded') AND {_zero('current')})",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_initial_shape",
            "(checkpoint_contract_version=1 AND ((state='prepared' AND row_version=0 AND remote_task_identity IS NULL) OR (state<>'prepared' AND row_version>=1))) OR "
            "(checkpoint_contract_version IN (2,3) AND ((state IN ('prepared','reconciling') AND remote_task_identity IS NULL) OR (state NOT IN ('prepared','reconciling') AND row_version>=1)))",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_claim_shape",
            "(checkpoint_contract_version=1 AND claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR "
            "(checkpoint_contract_version IN (2,3) AND state='prepared' AND ((claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (claim_generation>=1 AND claim_owner_identity IS NOT NULL AND claim_lease_until IS NOT NULL))) OR "
            "(checkpoint_contract_version IN (2,3) AND state IN ('reconciling','submitted','remote_terminal','materializing','local_materialized','finish_committed','remote_failure_committed','local_failure_committed') AND claim_generation>=1 AND claim_owner_identity IS NOT NULL AND claim_lease_until IS NOT NULL) OR "
            "(checkpoint_contract_version IN (2,3) AND state IN ('acked','remote_failed','local_failed','pre_submission_failed','superseded') AND claim_generation>=1 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL)",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_local_receipt",
            "checkpoint_contract_version=1 OR "
            "(checkpoint_contract_version=2 AND ((state IN ('local_materialized','finish_committed','acked') AND local_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND local_receipt_bytes IS NOT NULL AND local_receipt_byte_count=octet_length(local_receipt_bytes) AND local_receipt_byte_count BETWEEN 1 AND 65536) OR (state NOT IN ('local_materialized','finish_committed','acked') AND local_receipt_sha256 IS NULL AND local_receipt_bytes IS NULL AND local_receipt_byte_count IS NULL))) OR "
            "(checkpoint_contract_version=3 AND ((state IN ('local_materialized','finish_committed','acked') AND local_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND local_receipt_bytes IS NOT NULL AND local_receipt_byte_count=octet_length(local_receipt_bytes) AND local_receipt_byte_count BETWEEN 1 AND 65536 AND local_db_staged_byte_count>0) OR (state IN ('local_failure_committed','local_failed') AND ((local_receipt_sha256 IS NULL AND local_receipt_bytes IS NULL AND local_receipt_byte_count IS NULL AND local_db_staged_byte_count IS NULL) OR (local_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND local_receipt_bytes IS NOT NULL AND local_receipt_byte_count=octet_length(local_receipt_bytes) AND local_receipt_byte_count BETWEEN 1 AND 65536 AND local_db_staged_byte_count>0))) OR (state NOT IN ('local_materialized','finish_committed','acked','local_failure_committed','local_failed') AND local_receipt_sha256 IS NULL AND local_receipt_bytes IS NULL AND local_receipt_byte_count IS NULL AND local_db_staged_byte_count IS NULL)))",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_submitted_shape",
            "(checkpoint_contract_version=1 AND (state <> 'submitted' OR remote_task_identity IS NOT NULL)) OR (checkpoint_contract_version=2 AND ((state IN ('prepared','reconciling','pre_submission_failed','superseded') AND submitted_receipt_sha256 IS NULL AND submitted_receipt_bytes IS NULL AND submitted_receipt_byte_count IS NULL) OR (state NOT IN ('prepared','reconciling','pre_submission_failed','superseded') AND remote_task_identity IS NOT NULL AND submitted_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND submitted_receipt_bytes IS NOT NULL AND submitted_receipt_byte_count=octet_length(submitted_receipt_bytes) AND submitted_receipt_byte_count BETWEEN 1 AND 65536))) OR (checkpoint_contract_version=3 AND ((state IN ('prepared','reconciling','pre_submission_failed','superseded') AND submitted_receipt_sha256 IS NULL AND submitted_receipt_bytes IS NULL AND submitted_receipt_byte_count IS NULL) OR (state NOT IN ('prepared','reconciling','pre_submission_failed','superseded') AND remote_task_identity IS NOT NULL AND submitted_receipt_sha256 IS NOT NULL AND submitted_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND submitted_receipt_bytes IS NOT NULL AND submitted_receipt_byte_count IS NOT NULL AND submitted_receipt_byte_count=octet_length(submitted_receipt_bytes) AND submitted_receipt_byte_count BETWEEN 1 AND 65536)))",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_failure_receipt",
            "checkpoint_contract_version=1 OR (checkpoint_contract_version=2 AND ((state IN ('remote_failure_committed','remote_failed','pre_submission_failed') AND failure_stage='remote' AND failure_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND failure_receipt_bytes IS NOT NULL AND failure_receipt_byte_count=octet_length(failure_receipt_bytes) AND failure_receipt_byte_count BETWEEN 1 AND 65536) OR (state IN ('local_failure_committed','local_failed') AND failure_stage='local' AND failure_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND failure_receipt_bytes IS NOT NULL AND failure_receipt_byte_count=octet_length(failure_receipt_bytes) AND failure_receipt_byte_count BETWEEN 1 AND 65536) OR (state NOT IN ('remote_failure_committed','remote_failed','pre_submission_failed','local_failure_committed','local_failed') AND failure_receipt_sha256 IS NULL AND failure_receipt_bytes IS NULL AND failure_receipt_byte_count IS NULL AND failure_stage IS NULL))) OR (checkpoint_contract_version=3 AND ((state IN ('remote_failure_committed','remote_failed','pre_submission_failed') AND failure_stage='remote' AND failure_receipt_sha256 IS NOT NULL AND failure_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND failure_receipt_bytes IS NOT NULL AND failure_receipt_byte_count IS NOT NULL AND failure_receipt_byte_count=octet_length(failure_receipt_bytes) AND failure_receipt_byte_count BETWEEN 1 AND 65536) OR (state IN ('local_failure_committed','local_failed') AND failure_stage='local' AND failure_receipt_sha256 IS NOT NULL AND failure_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND failure_receipt_bytes IS NOT NULL AND failure_receipt_byte_count IS NOT NULL AND failure_receipt_byte_count=octet_length(failure_receipt_bytes) AND failure_receipt_byte_count BETWEEN 1 AND 65536) OR (state NOT IN ('remote_failure_committed','remote_failed','pre_submission_failed','local_failure_committed','local_failed') AND failure_receipt_sha256 IS NULL AND failure_receipt_bytes IS NULL AND failure_receipt_byte_count IS NULL AND failure_stage IS NULL)))",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_terminal_shape",
            f"(checkpoint_contract_version=1 AND ({_V1_TERMINAL})) OR (checkpoint_contract_version=2 AND ({_V2_TERMINAL})) OR (checkpoint_contract_version=3 AND ({_V3_TERMINAL}))",
        )

    op.execute(f"""
        CREATE OR REPLACE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v3_secrets()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE a {OPS_SCHEMA}.remote_parse_attempt%ROWTYPE;
        DECLARE target_attempt text;
        BEGIN
          target_attempt := CASE WHEN TG_OP='DELETE' THEN OLD.attempt_id ELSE NEW.attempt_id END;
          SELECT * INTO a FROM {OPS_SCHEMA}.remote_parse_attempt
           WHERE attempt_id=target_attempt;
          IF NOT FOUND THEN IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF; END IF;
          IF a.checkpoint_contract_version=3 THEN
            IF NOT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v3_resume_secret s WHERE s.attempt_id=a.attempt_id AND s.secret_kind='prepared_reconcile') THEN RAISE EXCEPTION 'v3 attempt lacks prepared secret'; END IF;
            IF (a.state NOT IN ('prepared','reconciling','pre_submission_failed','superseded')) <> EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v3_resume_secret s WHERE s.attempt_id=a.attempt_id AND s.secret_kind='accepted_submission') THEN RAISE EXCEPTION 'v3 accepted secret drift'; END IF;
            IF (a.terminal_receipt_bytes IS NOT NULL) <> EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v3_resume_secret s WHERE s.attempt_id=a.attempt_id AND s.secret_kind='terminal') THEN RAISE EXCEPTION 'v3 terminal secret drift'; END IF;
            IF (a.materialization_receipt_bytes IS NOT NULL) <> EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v3_resume_secret s WHERE s.attempt_id=a.attempt_id AND s.secret_kind='materialization') THEN RAISE EXCEPTION 'v3 materialization secret drift'; END IF;
            IF a.materialization_receipt_bytes IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v3_resume_secret s WHERE s.attempt_id=a.attempt_id AND s.secret_kind='materialization' AND s.token_sha256=a.materialization_token_sha256) THEN RAISE EXCEPTION 'v3 materialization token drift'; END IF;
          END IF;
          IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
        END $$
    """)
    immutable_columns = ",".join((*_IDENTITY_COLUMNS, *(f"reservation_{n}" for n in _CREDITS)))
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {OPS_SCHEMA}.reject_remote_parse_v3_identity_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.checkpoint_contract_version=3 AND
             ROW({','.join(f'OLD.{n}' for n in (*_IDENTITY_COLUMNS, *(f'reservation_{c}' for c in _CREDITS)))})
             IS DISTINCT FROM
             ROW({','.join(f'NEW.{n}' for n in (*_IDENTITY_COLUMNS, *(f'reservation_{c}' for c in _CREDITS)))})
          THEN RAISE EXCEPTION 'v3 reservation identity is immutable'; END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(f"CREATE TRIGGER ck_remote_parse_v3_identity_immutable BEFORE UPDATE OF {immutable_columns} ON {OPS_SCHEMA}.remote_parse_attempt FOR EACH ROW EXECUTE FUNCTION {OPS_SCHEMA}.reject_remote_parse_v3_identity_update()")
    op.execute(f"CREATE CONSTRAINT TRIGGER ck_remote_parse_v3_attempt_secrets AFTER INSERT OR UPDATE ON {OPS_SCHEMA}.remote_parse_attempt DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v3_secrets()")
    op.execute(f"CREATE CONSTRAINT TRIGGER ck_remote_parse_v3_secret_attempt AFTER INSERT OR UPDATE OR DELETE ON {OPS_SCHEMA}.remote_parse_v3_resume_secret DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v3_secrets()")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_attempt WHERE checkpoint_contract_version=3 OR NOT ({_all_null(_NON_V3_COLUMNS)}))")).scalar():
        raise RuntimeError("0056 downgrade would destroy v3 staged evidence")
    if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v3_resume_secret)")).scalar():
        raise RuntimeError("0056 downgrade would destroy v3 staged secrets")
    op.execute(f"DROP TRIGGER IF EXISTS ck_remote_parse_v3_secret_attempt ON {OPS_SCHEMA}.remote_parse_v3_resume_secret")
    op.execute(f"DROP TRIGGER IF EXISTS ck_remote_parse_v3_attempt_secrets ON {OPS_SCHEMA}.remote_parse_attempt")
    op.execute(f"DROP TRIGGER IF EXISTS ck_remote_parse_v3_identity_immutable ON {OPS_SCHEMA}.remote_parse_attempt")
    op.execute(f"DROP FUNCTION IF EXISTS {OPS_SCHEMA}.enforce_remote_parse_v3_secrets()")
    op.execute(f"DROP FUNCTION IF EXISTS {OPS_SCHEMA}.reject_remote_parse_v3_identity_update()")
    with op.batch_alter_table("remote_parse_attempt", schema=OPS_SCHEMA) as batch:
        for name in (
            "ck_remote_parse_attempt_contract_version", "ck_remote_parse_attempt_v3_credit_bounds",
            "ck_remote_parse_attempt_v3_final_zero", "ck_remote_parse_attempt_v3_materialization",
            "ck_remote_parse_attempt_v3_local_projection", "ck_remote_parse_attempt_v3_state_credit",
            "ck_remote_parse_attempt_initial_shape", "ck_remote_parse_attempt_claim_shape",
            "ck_remote_parse_attempt_local_receipt",
            "ck_remote_parse_attempt_failure_receipt",
            "ck_remote_parse_attempt_submitted_shape", "ck_remote_parse_attempt_terminal_shape",
        ):
            batch.drop_constraint(name, type_="check")
        for name in reversed(_NON_V3_COLUMNS):
            batch.drop_column(name)
    with op.batch_alter_table("remote_parse_attempt", schema=OPS_SCHEMA) as batch:
        batch.create_check_constraint("ck_remote_parse_attempt_initial_shape", "(checkpoint_contract_version=1 AND ((state='prepared' AND row_version=0 AND remote_task_identity IS NULL) OR (state<>'prepared' AND row_version>=1))) OR (checkpoint_contract_version=2 AND ((state IN ('prepared','reconciling') AND remote_task_identity IS NULL) OR (state NOT IN ('prepared','reconciling') AND row_version>=1)))")
        batch.create_check_constraint("ck_remote_parse_attempt_contract_version", "checkpoint_contract_version IN (1,2) AND (checkpoint_contract_version=2 OR (claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL AND submitted_receipt_sha256 IS NULL AND submitted_receipt_bytes IS NULL AND submitted_receipt_byte_count IS NULL AND local_receipt_sha256 IS NULL AND local_receipt_bytes IS NULL AND local_receipt_byte_count IS NULL AND failure_receipt_sha256 IS NULL AND failure_receipt_bytes IS NULL AND failure_receipt_byte_count IS NULL AND failure_stage IS NULL AND state NOT IN ('remote_failure_committed','local_failure_committed','pre_submission_failed')))")
        batch.create_check_constraint("ck_remote_parse_attempt_submitted_shape", "(checkpoint_contract_version=1 AND (state <> 'submitted' OR remote_task_identity IS NOT NULL)) OR (checkpoint_contract_version=2 AND ((state IN ('prepared','reconciling','pre_submission_failed','superseded') AND submitted_receipt_sha256 IS NULL AND submitted_receipt_bytes IS NULL AND submitted_receipt_byte_count IS NULL) OR (state NOT IN ('prepared','reconciling','pre_submission_failed','superseded') AND remote_task_identity IS NOT NULL AND submitted_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND submitted_receipt_bytes IS NOT NULL AND submitted_receipt_byte_count=octet_length(submitted_receipt_bytes) AND submitted_receipt_byte_count BETWEEN 1 AND 65536)))")
        batch.create_check_constraint("ck_remote_parse_attempt_claim_shape", "(checkpoint_contract_version=1 AND claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (checkpoint_contract_version=2 AND state='prepared' AND ((claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (claim_generation>=1 AND claim_owner_identity IS NOT NULL AND claim_lease_until IS NOT NULL))) OR (checkpoint_contract_version=2 AND state IN ('reconciling','submitted','remote_terminal','materializing','local_materialized','finish_committed','remote_failure_committed','local_failure_committed') AND claim_generation>=1 AND claim_owner_identity IS NOT NULL AND claim_lease_until IS NOT NULL) OR (checkpoint_contract_version=2 AND state IN ('acked','remote_failed','local_failed','pre_submission_failed','superseded') AND claim_generation>=1 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL)")
        batch.create_check_constraint("ck_remote_parse_attempt_local_receipt", "checkpoint_contract_version=1 OR ((state IN ('local_materialized','finish_committed','acked') AND local_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND local_receipt_bytes IS NOT NULL AND local_receipt_byte_count=octet_length(local_receipt_bytes) AND local_receipt_byte_count BETWEEN 1 AND 65536) OR (state NOT IN ('local_materialized','finish_committed','acked') AND local_receipt_sha256 IS NULL AND local_receipt_bytes IS NULL AND local_receipt_byte_count IS NULL))")
        batch.create_check_constraint("ck_remote_parse_attempt_failure_receipt", "checkpoint_contract_version=1 OR ((state IN ('remote_failure_committed','remote_failed','pre_submission_failed') AND failure_stage='remote' AND failure_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND failure_receipt_bytes IS NOT NULL AND failure_receipt_byte_count=octet_length(failure_receipt_bytes) AND failure_receipt_byte_count BETWEEN 1 AND 65536) OR (state IN ('local_failure_committed','local_failed') AND failure_stage='local' AND failure_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND failure_receipt_bytes IS NOT NULL AND failure_receipt_byte_count=octet_length(failure_receipt_bytes) AND failure_receipt_byte_count BETWEEN 1 AND 65536) OR (state NOT IN ('remote_failure_committed','remote_failed','pre_submission_failed','local_failure_committed','local_failed') AND failure_receipt_sha256 IS NULL AND failure_receipt_bytes IS NULL AND failure_receipt_byte_count IS NULL AND failure_stage IS NULL))")
        batch.create_check_constraint("ck_remote_parse_attempt_terminal_shape", _TERMINAL_0055)
    op.drop_table("remote_parse_v3_resume_secret", schema=OPS_SCHEMA)
