"""Stage linked V4 superseding H0 authority before currentness transfer.

Revision ID: 0058_v4_supersession_stage
Revises: 0057_remote_parse_v4_authority
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    FUTURE_L2_READER_ROLE,
    OPS_SCHEMA,
    READER_ROLE,
)

revision: str = "0058_v4_supersession_stage"
down_revision: str | None = "0057_remote_parse_v4_authority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAX_INT = (1 << 63) - 1
_SHA_CHECK = "~ '^sha256:[0-9a-f]{64}$'"
_V4_CURRENT_STATES = (
    "prepared",
    "reconciling",
    "submitted",
    "remote_terminal",
    "materializing",
    "local_materialized",
    "publish_committed",
    "cleanup_pending",
    "ack_pending",
)
_V4_FINAL_STATES = (
    "acked",
    "remote_failed",
    "local_failed",
    "pre_submission_failed",
    "preparation_failed",
    "superseded",
)


def _sql_values(values: Sequence[str]) -> str:
    return ",".join(f"'{value}'" for value in values)


def _legacy_claim_shape() -> str:
    return "(checkpoint_contract_version=1 AND claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (checkpoint_contract_version IN (2,3) AND state='prepared' AND ((claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (claim_generation>=1 AND claim_owner_identity IS NOT NULL AND claim_lease_until IS NOT NULL))) OR (checkpoint_contract_version IN (2,3) AND state IN ('reconciling','submitted','remote_terminal','materializing','local_materialized','finish_committed','remote_failure_committed','local_failure_committed') AND claim_generation>=1 AND claim_owner_identity IS NOT NULL AND claim_lease_until IS NOT NULL) OR (checkpoint_contract_version IN (2,3) AND state IN ('acked','remote_failed','local_failed','pre_submission_failed','superseded') AND claim_generation>=1 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL)"


def _remote_parse_lifecycle_shape(*, allow_staged: bool) -> str:
    legacy = "(checkpoint_contract_version IN (1,2,3) AND ((state IN ('prepared','reconciling','submitted','remote_terminal','materializing','local_materialized','finish_committed','remote_failure_committed','local_failure_committed') AND is_current) OR (state IN ('acked','remote_failed','local_failed','pre_submission_failed','superseded') AND NOT is_current)))"
    v4_arms = [
        f"(state IN ({_sql_values(_V4_CURRENT_STATES)}) AND is_current)",
        f"(state IN ({_sql_values(_V4_FINAL_STATES)}) AND NOT is_current)",
    ]
    if allow_staged:
        v4_arms.append("(state='prepared' AND NOT is_current)")
    return f"{legacy} OR (checkpoint_contract_version=4 AND ({' OR '.join(v4_arms)}))"


def _remote_parse_claim_shape(*, allow_staged: bool) -> str:
    legacy = f"({_legacy_claim_shape()})"
    if not allow_staged:
        return (
            legacy
            + f" OR (checkpoint_contract_version=4 AND state IN ({_sql_values(_V4_CURRENT_STATES)}) AND ((state='prepared' AND claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (claim_generation BETWEEN 1 AND {_MAX_INT} AND claim_owner_identity IS NOT NULL AND btrim(claim_owner_identity)<>'' AND claim_lease_until IS NOT NULL)))"
            + f" OR (checkpoint_contract_version=4 AND state IN ({_sql_values(_V4_FINAL_STATES)}) AND (((row_version=0 AND state IN ('preparation_failed','superseded')) AND claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (row_version>0 AND claim_generation BETWEEN 1 AND {_MAX_INT} AND claim_owner_identity IS NULL AND claim_lease_until IS NULL)))"
        )
    return (
        legacy
        + f" OR (checkpoint_contract_version=4 AND is_current AND state IN ({_sql_values(_V4_CURRENT_STATES)}) AND ((state='prepared' AND claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (claim_generation BETWEEN 1 AND {_MAX_INT} AND claim_owner_identity IS NOT NULL AND btrim(claim_owner_identity)<>'' AND claim_lease_until IS NOT NULL)))"
        + " OR (checkpoint_contract_version=4 AND NOT is_current AND state='prepared' AND row_version=0 AND claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL)"
        + f" OR (checkpoint_contract_version=4 AND NOT is_current AND state IN ({_sql_values(_V4_FINAL_STATES)}) AND (((row_version=0 AND state IN ('preparation_failed','superseded')) AND claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (row_version>0 AND claim_generation BETWEEN 1 AND {_MAX_INT} AND claim_owner_identity IS NULL AND claim_lease_until IS NULL)))"
    )


def _replace_attempt_constraints(*, allow_staged: bool) -> None:
    with op.batch_alter_table("remote_parse_attempt", schema=OPS_SCHEMA) as batch:
        batch.drop_constraint(
            "ck_remote_parse_attempt_lifecycle_shape",
            type_="check",
        )
        batch.drop_constraint(
            "ck_remote_parse_attempt_claim_shape",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_lifecycle_shape",
            _remote_parse_lifecycle_shape(allow_staged=allow_staged),
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_claim_shape",
            _remote_parse_claim_shape(allow_staged=allow_staged),
        )


def _replace_secret_lifecycle_function(*, allow_staged: bool) -> None:
    if allow_staged:
        nonfinal_current_guard = """
          ELSIF head_current IS DISTINCT FROM true THEN
            IF head_state<>'prepared'
               OR accepted_hash IS NOT NULL
               OR history_count<>0
            THEN
              RAISE EXCEPTION 'remote parse v4 nonfinal head is not current';
            END IF;
        """
    else:
        nonfinal_current_guard = """
          ELSIF head_current IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'remote parse v4 nonfinal head is not current';
        """
    op.execute(f"""
        CREATE OR REPLACE FUNCTION
          {OPS_SCHEMA}.enforce_remote_parse_v4_secret_lifecycle()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE target_attempt text;
        DECLARE head_state text;
        DECLARE head_current boolean;
        DECLARE head_fence text;
        DECLARE accepted_hash text;
        DECLARE history_count bigint;
        DECLARE history_min bigint;
        DECLARE history_max bigint;
        DECLARE history_drift bigint;
        BEGIN
          IF TG_TABLE_NAME='remote_parse_v4_secret' AND TG_OP='DELETE' THEN
            target_attempt := OLD.attempt_id;
          ELSE
            target_attempt := NEW.attempt_id;
          END IF;
          SELECT a.state, a.is_current, a.fence_identity,
                 c.accepted_submission_sha256
            INTO head_state, head_current, head_fence, accepted_hash
            FROM {OPS_SCHEMA}.remote_parse_attempt a
            JOIN {OPS_SCHEMA}.remote_parse_v4_checkpoint c
              ON c.attempt_id=a.attempt_id
             AND c.lifecycle_version=a.row_version
             AND c.checkpoint_sha256=a.current_checkpoint_sha256
             AND c.state=a.state
             AND c.fence_identity=a.fence_identity
           WHERE a.attempt_id=target_attempt
             AND a.checkpoint_contract_version=4;
          IF NOT FOUND THEN
            IF TG_TABLE_NAME='remote_parse_v4_secret' THEN
              RAISE EXCEPTION 'remote parse v4 secret lifecycle lacks exact head';
            END IF;
            IF NEW.checkpoint_contract_version=4 THEN
              RAISE EXCEPTION 'remote parse v4 secret lifecycle lacks exact head';
            END IF;
            RETURN NEW;
          END IF;
          SELECT count(*), min(encryption_revision), max(encryption_revision),
                 count(*) FILTER (
                   WHERE fence_identity IS DISTINCT FROM head_fence
                      OR accepted_submission_sha256 IS DISTINCT FROM accepted_hash
                 )
            INTO history_count, history_min, history_max, history_drift
            FROM {OPS_SCHEMA}.remote_parse_v4_secret
           WHERE attempt_id=target_attempt;
          IF head_state IN ({_sql_values(_V4_FINAL_STATES)}) THEN
            IF head_current IS DISTINCT FROM false OR history_count<>0 THEN
              RAISE EXCEPTION 'remote parse v4 final head retains or revives secret';
            END IF;
          {nonfinal_current_guard}
          ELSIF accepted_hash IS NULL THEN
            IF history_count<>0 THEN
              RAISE EXCEPTION 'remote parse v4 secret exists before accepted submission';
            END IF;
          ELSIF history_count<1
             OR history_min<>1
             OR history_max<>history_count
             OR history_drift<>0
          THEN
            RAISE EXCEPTION 'remote parse v4 accepted head lacks exact secret history';
          END IF;
          IF TG_OP='DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END $$
    """)


def _replace_evidence_kind_hash_constraint(*, enable: bool) -> None:
    with op.batch_alter_table(
        "remote_parse_v4_evidence",
        schema=OPS_SCHEMA,
    ) as batch:
        if enable:
            batch.create_unique_constraint(
                "uq_remote_parse_v4_evidence_kind_hash",
                ["attempt_id", "evidence_kind", "evidence_sha256"],
            )
        else:
            batch.drop_constraint(
                "uq_remote_parse_v4_evidence_kind_hash",
                type_="unique",
            )


def _create_link_table() -> None:
    op.create_index(
        "uq_remote_parse_v4_staged_document",
        "remote_parse_attempt",
        ["document_id"],
        unique=True,
        schema=OPS_SCHEMA,
        postgresql_where=sa.text(
            "checkpoint_contract_version=4 AND state='prepared' AND NOT is_current"
        ),
    )
    op.create_table(
        "remote_parse_v4_supersession_link",
        sa.Column("source_attempt_id", sa.String(64), nullable=False),
        sa.Column("source_fence_identity", sa.String(128), nullable=False),
        sa.Column(
            "source_evidence_kind",
            sa.String(64),
            nullable=False,
        ),
        sa.Column(
            "source_supersession_receipt_sha256",
            sa.String(71),
            nullable=False,
        ),
        sa.Column("superseding_attempt_id", sa.String(64), nullable=False),
        sa.Column("superseding_fence_identity", sa.String(128), nullable=False),
        sa.Column(
            "superseding_lifecycle_version",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "superseding_checkpoint_sha256",
            sa.String(71),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("source_attempt_id"),
        sa.UniqueConstraint(
            "superseding_attempt_id",
            name="uq_remote_parse_v4_supersession_link_target",
        ),
        sa.ForeignKeyConstraint(
            ["source_attempt_id", "source_fence_identity"],
            [
                f"{OPS_SCHEMA}.remote_parse_attempt.attempt_id",
                f"{OPS_SCHEMA}.remote_parse_attempt.fence_identity",
            ],
            name="fk_remote_parse_v4_supersession_link_source",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            [
                "source_attempt_id",
                "source_evidence_kind",
                "source_supersession_receipt_sha256",
            ],
            [
                f"{OPS_SCHEMA}.remote_parse_v4_evidence.attempt_id",
                f"{OPS_SCHEMA}.remote_parse_v4_evidence.evidence_kind",
                f"{OPS_SCHEMA}.remote_parse_v4_evidence.evidence_sha256",
            ],
            name="fk_remote_parse_v4_supersession_link_receipt",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["superseding_attempt_id", "superseding_fence_identity"],
            [
                f"{OPS_SCHEMA}.remote_parse_attempt.attempt_id",
                f"{OPS_SCHEMA}.remote_parse_attempt.fence_identity",
            ],
            name="fk_remote_parse_v4_supersession_link_target",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            [
                "superseding_attempt_id",
                "superseding_lifecycle_version",
                "superseding_checkpoint_sha256",
            ],
            [
                f"{OPS_SCHEMA}.remote_parse_v4_checkpoint.attempt_id",
                f"{OPS_SCHEMA}.remote_parse_v4_checkpoint.lifecycle_version",
                f"{OPS_SCHEMA}.remote_parse_v4_checkpoint.checkpoint_sha256",
            ],
            name="fk_remote_parse_v4_supersession_link_checkpoint",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "source_attempt_id<>superseding_attempt_id",
            name="ck_remote_parse_v4_supersession_link_distinct",
        ),
        sa.CheckConstraint(
            "source_evidence_kind='supersession_receipt' AND "
            "superseding_lifecycle_version=0 AND "
            f"source_supersession_receipt_sha256 {_SHA_CHECK} AND "
            f"superseding_checkpoint_sha256 {_SHA_CHECK}",
            name="ck_remote_parse_v4_supersession_link_identity",
        ),
        schema=OPS_SCHEMA,
    )


def _create_link_triggers() -> None:
    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.assert_remote_parse_v4_supersession_link(
          source_id text,
          superseding_id text
        ) RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
          relation_row {OPS_SCHEMA}.remote_parse_v4_supersession_link%ROWTYPE;
          source_head {OPS_SCHEMA}.remote_parse_attempt%ROWTYPE;
          superseding_head {OPS_SCHEMA}.remote_parse_attempt%ROWTYPE;
          source_checkpoint {OPS_SCHEMA}.remote_parse_v4_checkpoint%ROWTYPE;
          superseding_checkpoint {OPS_SCHEMA}.remote_parse_v4_checkpoint%ROWTYPE;
        BEGIN
          SELECT * INTO STRICT relation_row
            FROM {OPS_SCHEMA}.remote_parse_v4_supersession_link
           WHERE source_attempt_id=source_id
             AND superseding_attempt_id=superseding_id;

          SELECT * INTO STRICT source_head
            FROM {OPS_SCHEMA}.remote_parse_attempt
           WHERE attempt_id=relation_row.source_attempt_id;
          SELECT * INTO STRICT superseding_head
            FROM {OPS_SCHEMA}.remote_parse_attempt
           WHERE attempt_id=relation_row.superseding_attempt_id;
          SELECT * INTO STRICT source_checkpoint
            FROM {OPS_SCHEMA}.remote_parse_v4_checkpoint
           WHERE attempt_id=source_head.attempt_id
             AND lifecycle_version=source_head.row_version
             AND checkpoint_sha256=source_head.current_checkpoint_sha256;
          SELECT * INTO STRICT superseding_checkpoint
            FROM {OPS_SCHEMA}.remote_parse_v4_checkpoint
           WHERE attempt_id=relation_row.superseding_attempt_id
             AND lifecycle_version=0
             AND checkpoint_sha256=relation_row.superseding_checkpoint_sha256;

          IF source_head.checkpoint_contract_version<>4
             OR superseding_head.checkpoint_contract_version<>4
             OR source_head.fence_identity IS DISTINCT FROM relation_row.source_fence_identity
             OR superseding_head.fence_identity IS DISTINCT FROM relation_row.superseding_fence_identity
             OR source_checkpoint.fence_identity IS DISTINCT FROM relation_row.source_fence_identity
             OR superseding_checkpoint.fence_identity IS DISTINCT FROM relation_row.superseding_fence_identity
          THEN
            RAISE EXCEPTION 'remote parse v4 supersession link parent identity drifted';
          END IF;

          IF source_head.document_id IS DISTINCT FROM superseding_head.document_id
             OR superseding_head.attempt_generation<=source_head.attempt_generation
          THEN
            RAISE EXCEPTION 'remote parse v4 supersession link generation or document drifted';
          END IF;

          IF source_checkpoint.supersession_receipt_sha256
               IS DISTINCT FROM relation_row.source_supersession_receipt_sha256
             OR relation_row.source_evidence_kind<>'supersession_receipt'
             OR NOT EXISTS (
               SELECT 1
                 FROM {OPS_SCHEMA}.remote_parse_v4_evidence AS evidence
                WHERE evidence.attempt_id=relation_row.source_attempt_id
                  AND evidence.fence_identity=relation_row.source_fence_identity
                  AND evidence.evidence_kind='supersession_receipt'
                  AND evidence.evidence_sha256=
                        relation_row.source_supersession_receipt_sha256
             )
          THEN
            RAISE EXCEPTION 'remote parse v4 supersession link receipt is not exact';
          END IF;

          IF relation_row.superseding_lifecycle_version<>0
             OR superseding_checkpoint.lifecycle_version<>0
             OR superseding_checkpoint.state<>'prepared'
          THEN
            RAISE EXCEPTION 'remote parse v4 supersession link target is not exact h0';
          END IF;

          IF source_head.is_current THEN
            IF source_head.state NOT IN ('cleanup_pending','ack_pending')
               OR superseding_head.is_current
               OR superseding_head.state<>'prepared'
               OR superseding_head.row_version<>0
               OR superseding_head.current_checkpoint_sha256
                    IS DISTINCT FROM relation_row.superseding_checkpoint_sha256
               OR superseding_head.claim_generation<>0
               OR superseding_head.claim_owner_identity IS NOT NULL
               OR superseding_head.claim_lease_until IS NOT NULL
            THEN
              RAISE EXCEPTION 'remote parse v4 supersession staging shape drifted';
            END IF;
          ELSIF source_head.state='superseded' THEN
            IF NOT superseding_head.is_current
               AND superseding_head.state='prepared'
               AND superseding_head.row_version=0
               AND superseding_head.current_checkpoint_sha256
                    IS NOT DISTINCT FROM relation_row.superseding_checkpoint_sha256
               AND superseding_head.claim_generation=0
               AND superseding_head.claim_owner_identity IS NULL
               AND superseding_head.claim_lease_until IS NULL
            THEN
              RAISE EXCEPTION 'remote parse v4 supersession activation is incomplete';
            END IF;
          ELSE
            RAISE EXCEPTION 'remote parse v4 supersession source state drifted';
          END IF;
        EXCEPTION
          WHEN NO_DATA_FOUND OR TOO_MANY_ROWS THEN
            RAISE EXCEPTION 'remote parse v4 supersession link closure is incomplete';
        END $$
    """)
    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v4_supersession_link()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        BEGIN
          PERFORM {OPS_SCHEMA}.assert_remote_parse_v4_supersession_link(
            NEW.source_attempt_id,
            NEW.superseding_attempt_id
          );
          RETURN NEW;
        END $$
    """)
    op.execute(f"""
        CREATE CONSTRAINT TRIGGER ck_remote_parse_v4_supersession_link_relations
        AFTER INSERT ON {OPS_SCHEMA}.remote_parse_v4_supersession_link
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION
          {OPS_SCHEMA}.enforce_remote_parse_v4_supersession_link()
    """)
    op.execute(
        f"CREATE TRIGGER ck_remote_parse_v4_supersession_link_immutable "
        f"BEFORE UPDATE OR DELETE ON {OPS_SCHEMA}.remote_parse_v4_supersession_link "
        f"FOR EACH ROW EXECUTE FUNCTION "
        f"{OPS_SCHEMA}.reject_remote_parse_v4_immutable_change()"
    )

    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v4_supersession_head()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
          relation_count bigint;
          relation_row {OPS_SCHEMA}.remote_parse_v4_supersession_link%ROWTYPE;
          current_supersession_receipt text;
        BEGIN
          IF NEW.checkpoint_contract_version<>4 THEN
            RETURN NEW;
          END IF;

          IF NEW.state='prepared' AND NOT NEW.is_current THEN
            SELECT count(*) INTO relation_count
              FROM {OPS_SCHEMA}.remote_parse_v4_supersession_link
             WHERE superseding_attempt_id=NEW.attempt_id;
            IF relation_count<>1 THEN
              RAISE EXCEPTION 'remote parse v4 staged h0 lacks one supersession link';
            END IF;
          END IF;

          SELECT checkpoint.supersession_receipt_sha256
            INTO current_supersession_receipt
            FROM {OPS_SCHEMA}.remote_parse_v4_checkpoint AS checkpoint
           WHERE checkpoint.attempt_id=NEW.attempt_id
             AND checkpoint.lifecycle_version=NEW.row_version
             AND checkpoint.checkpoint_sha256=NEW.current_checkpoint_sha256;
          IF current_supersession_receipt IS NOT NULL
             AND (
               (NEW.is_current AND NEW.state IN ('cleanup_pending','ack_pending'))
               OR (NOT NEW.is_current AND NEW.state='superseded')
             )
          THEN
            SELECT count(*) INTO relation_count
              FROM {OPS_SCHEMA}.remote_parse_v4_supersession_link
             WHERE source_attempt_id=NEW.attempt_id
               AND source_supersession_receipt_sha256=current_supersession_receipt;
            IF relation_count<>1 THEN
              RAISE EXCEPTION 'remote parse v4 current supersession lacks one staged authority';
            END IF;
          END IF;

          FOR relation_row IN
            SELECT *
              FROM {OPS_SCHEMA}.remote_parse_v4_supersession_link
             WHERE source_attempt_id=NEW.attempt_id
                OR superseding_attempt_id=NEW.attempt_id
          LOOP
            PERFORM {OPS_SCHEMA}.assert_remote_parse_v4_supersession_link(
              relation_row.source_attempt_id,
              relation_row.superseding_attempt_id
            );
          END LOOP;
          RETURN NEW;
        END $$
    """)
    op.execute(f"""
        CREATE CONSTRAINT TRIGGER ck_remote_parse_v4_supersession_head_relations
        AFTER INSERT OR UPDATE ON {OPS_SCHEMA}.remote_parse_attempt
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION
          {OPS_SCHEMA}.enforce_remote_parse_v4_supersession_head()
    """)


def _grant_access() -> None:
    op.execute(
        f"REVOKE ALL ON TABLE {OPS_SCHEMA}.remote_parse_v4_supersession_link "
        f"FROM PUBLIC, {READER_ROLE}, {FUTURE_L2_READER_ROLE}, {APP_ROLE}"
    )
    op.execute(
        f"GRANT SELECT, INSERT ON TABLE "
        f"{OPS_SCHEMA}.remote_parse_v4_supersession_link TO {APP_ROLE}"
    )
    for signature in (
        "assert_remote_parse_v4_supersession_link(text,text)",
        "enforce_remote_parse_v4_supersession_link()",
        "enforce_remote_parse_v4_supersession_head()",
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION {OPS_SCHEMA}.{signature} "
            f"FROM PUBLIC, {READER_ROLE}, {FUTURE_L2_READER_ROLE}, {APP_ROLE}"
        )


def _guard_preexisting_unlinked_supersession_authority() -> None:
    bind = op.get_bind()
    has_unlinked_authority = bool(
        bind.execute(
            sa.text(
                f"""
                SELECT EXISTS (
                  SELECT 1
                    FROM {OPS_SCHEMA}.remote_parse_attempt AS head
                    JOIN {OPS_SCHEMA}.remote_parse_v4_checkpoint AS checkpoint
                      ON checkpoint.attempt_id=head.attempt_id
                     AND checkpoint.lifecycle_version=head.row_version
                     AND checkpoint.checkpoint_sha256=
                           head.current_checkpoint_sha256
                   WHERE head.checkpoint_contract_version=4
                     AND checkpoint.supersession_receipt_sha256 IS NOT NULL
                )
                """
            )
        ).scalar_one()
    )
    if has_unlinked_authority:
        raise RuntimeError(
            "0058 refuses preexisting unlinked v4 supersession authority"
        )


def upgrade() -> None:
    _guard_preexisting_unlinked_supersession_authority()
    _replace_attempt_constraints(allow_staged=True)
    _replace_secret_lifecycle_function(allow_staged=True)
    _replace_evidence_kind_hash_constraint(enable=True)
    _create_link_table()
    _create_link_triggers()
    _grant_access()


def downgrade() -> None:
    bind = op.get_bind()
    has_authority = bool(
        bind.execute(
            sa.text(
                f"SELECT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v4_supersession_link) "
                f"OR EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_attempt "
                "WHERE checkpoint_contract_version=4 AND state='prepared' AND NOT is_current)"
            )
        ).scalar_one()
    )
    if has_authority:
        raise RuntimeError(
            "0058 downgrade would destroy staged v4 supersession authority"
        )

    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_supersession_head_relations ON "
        f"{OPS_SCHEMA}.remote_parse_attempt"
    )
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_supersession_link_immutable ON "
        f"{OPS_SCHEMA}.remote_parse_v4_supersession_link"
    )
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_supersession_link_relations ON "
        f"{OPS_SCHEMA}.remote_parse_v4_supersession_link"
    )
    op.drop_table("remote_parse_v4_supersession_link", schema=OPS_SCHEMA)
    op.execute(
        f"DROP FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v4_supersession_head()"
    )
    op.execute(
        f"DROP FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v4_supersession_link()"
    )
    op.execute(
        f"DROP FUNCTION {OPS_SCHEMA}.assert_remote_parse_v4_supersession_link(text,text)"
    )
    op.drop_index(
        "uq_remote_parse_v4_staged_document",
        table_name="remote_parse_attempt",
        schema=OPS_SCHEMA,
    )
    _replace_secret_lifecycle_function(allow_staged=False)
    _replace_attempt_constraints(allow_staged=False)
    _replace_evidence_kind_hash_constraint(enable=False)
