"""Add durable explicit processing requests and their exact-filing admission.

Revision ID: 0066_processing_request
Revises: 0065_requeue_key_lifetime

A processing request is one caller intent keyed by its idempotency key. Its
resolution attempts record bounded provider discovery; its target binds the one
exact filing it resolved to, and the target's admit flags are what the existing
download/parse predicates read to admit that filing past the pool and taxonomy
gates. Retry budgets, latches and running-run guards are not touched, and no
queue row or company tracking state is created here.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE, CORE_SCHEMA, FUTURE_L2_READER_ROLE, OPS_SCHEMA, READER_ROLE,
)

revision: str = "0066_processing_request"
down_revision: Union[str, None] = "0065_requeue_key_lifetime"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REQUEST = "processing_request"
_ATTEMPT = "processing_request_attempt"
_TARGET = "processing_request_target"
_ULID = "[0-9A-HJKMNP-TV-Z]{26}"
_SHA256 = "^sha256:[0-9a-f]{64}$"


def upgrade() -> None:
    op.create_table(
        _REQUEST,
        sa.Column("request_id", sa.String(64), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("intent_sha256", sa.String(71), nullable=False),
        sa.Column("contract_version", sa.String(64), nullable=False),
        sa.Column("target_kind", sa.String(32), nullable=False),
        sa.Column("target", postgresql.JSONB(), nullable=False),
        sa.Column("enqueue_parse", sa.Boolean(), nullable=False),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_processing_request_idempotency_key"
        ),
        sa.CheckConstraint(
            f"request_id ~ '^dreq_{_ULID}$'", name="ck_processing_request_id"
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[A-Za-z0-9._:-]{16,128}$'",
            name="ck_processing_request_idempotency_key",
        ),
        sa.CheckConstraint(
            f"intent_sha256 ~ '{_SHA256}'", name="ck_processing_request_intent"
        ),
        sa.CheckConstraint(
            "contract_version = 'disclosure-processing-request.v1'",
            name="ck_processing_request_contract",
        ),
        sa.CheckConstraint(
            "target_kind IN ('document','provider_document','title_match') "
            "AND jsonb_typeof(target) = 'object' "
            "AND (target->>'kind') IS NOT DISTINCT FROM target_kind",
            name="ck_processing_request_target",
        ),
        sa.CheckConstraint(
            "btrim(requested_by) <> ''", name="ck_processing_request_requested_by"
        ),
        schema=OPS_SCHEMA,
    )
    op.create_index(
        "ix_processing_request_created", _REQUEST, ["created_at"],
        schema=OPS_SCHEMA,
    )

    op.create_table(
        _ATTEMPT,
        sa.Column("attempt_id", sa.String(64), primary_key=True),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("profile_source_access_id", sa.String(64), nullable=True),
        sa.Column("index_source_access_id", sa.String(64), nullable=True),
        sa.Column("provider_row_count", sa.Integer(), nullable=True),
        sa.Column("choices", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column(
            "choices_truncated", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("error", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["request_id"], [f"{OPS_SCHEMA}.{_REQUEST}.request_id"],
            name="fk_processing_request_attempt_request", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["profile_source_access_id"],
            [f"{CORE_SCHEMA}.source_access.source_access_id"],
            name="fk_processing_request_attempt_profile", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["index_source_access_id"],
            [f"{CORE_SCHEMA}.source_access.source_access_id"],
            name="fk_processing_request_attempt_index", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "request_id", "attempt_no", name="uq_processing_request_attempt_no"
        ),
        sa.CheckConstraint(
            f"attempt_id ~ '^dpra_{_ULID}$'", name="ck_processing_request_attempt_id"
        ),
        sa.CheckConstraint(
            "attempt_no BETWEEN 1 AND 10", name="ck_processing_request_attempt_no"
        ),
        # One running->finished transition: both columns are set together, and
        # a finished attempt carries the evidence its outcome needs. The lease
        # is the executor's authority: its own outcomes are recorded only
        # before the lease ends, and only an expired lease may be closed as
        # abandoned. Only a retrying outcome may schedule the next attempt.
        # Every clause is NULL-safe: a CHECK that evaluates to UNKNOWN passes.
        sa.CheckConstraint(
            "(finished_at IS NULL) = (outcome IS NULL) "
            "AND (outcome IS NULL OR outcome IN "
            "('matched','ambiguous','not_found','failed','abandoned','deferred')) "
            "AND lease_expires_at > started_at "
            "AND (outcome IS NULL "
            "OR (outcome = 'abandoned') = (finished_at >= lease_expires_at)) "
            "AND (next_attempt_at IS NULL "
            "OR (outcome IS NOT NULL AND outcome IN ('failed','abandoned','deferred'))) "
            "AND (outcome IS NULL OR outcome IN ('matched','ambiguous','not_found') "
            "OR (error IS NOT NULL AND jsonb_typeof(error) = 'object')) "
            "AND (outcome IS DISTINCT FROM 'ambiguous' "
            "OR (choices IS NOT NULL AND jsonb_typeof(choices) = 'array')) "
            "AND (outcome IS DISTINCT FROM 'matched' "
            "OR index_source_access_id IS NOT NULL) "
            "AND (provider_row_count IS NULL OR provider_row_count >= 0) "
            "AND (choices IS NULL OR jsonb_typeof(choices) = 'array') "
            "AND (error IS NULL OR jsonb_typeof(error) = 'object')",
            name="ck_processing_request_attempt_outcome",
        ),
        schema=OPS_SCHEMA,
    )
    op.create_index(
        "uq_processing_request_attempt_open", _ATTEMPT, ["request_id"],
        unique=True, schema=OPS_SCHEMA,
        postgresql_where=sa.text("finished_at IS NULL"),
    )

    op.create_table(
        _TARGET,
        sa.Column("target_id", sa.String(64), primary_key=True),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("resolution_method", sa.String(32), nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("provider_document_id", sa.String(128), nullable=True),
        sa.Column("document_id", sa.String(64), nullable=True),
        sa.Column("raw_file_hash", sa.String(71), nullable=True),
        sa.Column("index_source_access_id", sa.String(64), nullable=True),
        sa.Column("admit_download", sa.Boolean(), nullable=False),
        sa.Column("admit_parse", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], [f"{OPS_SCHEMA}.{_REQUEST}.request_id"],
            name="fk_processing_request_target_request", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], [f"{OPS_SCHEMA}.{_ATTEMPT}.attempt_id"],
            name="fk_processing_request_target_attempt", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], [f"{CORE_SCHEMA}.document.document_id"],
            name="fk_processing_request_target_document", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["index_source_access_id"],
            [f"{CORE_SCHEMA}.source_access.source_access_id"],
            name="fk_processing_request_target_index", ondelete="RESTRICT",
        ),
        # One exact filing per request, bound once.
        sa.UniqueConstraint(
            "request_id", name="uq_processing_request_target_request"
        ),
        sa.CheckConstraint(
            f"target_id ~ '^dprt_{_ULID}$'", name="ck_processing_request_target_id"
        ),
        sa.CheckConstraint(
            "resolution_method IN "
            "('document_id','local_document','local_candidate','provider_discovery') "
            "AND (resolution_method = 'provider_discovery') = (attempt_id IS NOT NULL) "
            "AND (resolution_method NOT IN ('document_id','local_document') "
            "OR document_id IS NOT NULL) "
            "AND (resolution_method <> 'local_candidate' "
            "OR (document_id IS NULL AND index_source_access_id IS NOT NULL)) "
            "AND (resolution_method <> 'provider_discovery' "
            "OR index_source_access_id IS NOT NULL)",
            name="ck_processing_request_target_method",
        ),
        sa.CheckConstraint(
            "(document_id IS NULL) = (raw_file_hash IS NULL) "
            f"AND (raw_file_hash IS NULL OR raw_file_hash ~ '{_SHA256}') "
            "AND (provider IS NULL OR provider = 'cninfo') "
            "AND (provider IS NULL) = (provider_document_id IS NULL) "
            "AND (document_id IS NOT NULL OR provider_document_id IS NOT NULL) "
            "AND (provider_document_id IS NULL OR btrim(provider_document_id) <> '') "
            "AND (NOT admit_download OR (document_id IS NULL "
            "AND provider_document_id IS NOT NULL "
            "AND index_source_access_id IS NOT NULL))",
            name="ck_processing_request_target_identity",
        ),
        schema=OPS_SCHEMA,
    )
    op.create_index(
        "ix_processing_request_target_download", _TARGET,
        ["provider", "provider_document_id"], schema=OPS_SCHEMA,
        postgresql_where=sa.text("admit_download"),
    )
    op.create_index(
        "ix_processing_request_target_parse_document", _TARGET,
        ["document_id"], schema=OPS_SCHEMA,
        postgresql_where=sa.text("admit_parse AND document_id IS NOT NULL"),
    )
    op.create_index(
        "ix_processing_request_target_parse_provider", _TARGET,
        ["provider", "provider_document_id"], schema=OPS_SCHEMA,
        postgresql_where=sa.text("admit_parse AND document_id IS NULL"),
    )

    for table in (_REQUEST, _ATTEMPT, _TARGET):
        op.execute(
            f"REVOKE ALL ON {OPS_SCHEMA}.{table} "
            f"FROM PUBLIC, {READER_ROLE}, {FUTURE_L2_READER_ROLE}"
        )
    op.execute(f"GRANT SELECT, INSERT ON {OPS_SCHEMA}.{_REQUEST} TO {APP_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON {OPS_SCHEMA}.{_ATTEMPT} TO {APP_ROLE}"
    )
    op.execute(f"GRANT SELECT, INSERT ON {OPS_SCHEMA}.{_TARGET} TO {APP_ROLE}")


def downgrade() -> None:
    for index in (
        "ix_processing_request_target_parse_provider",
        "ix_processing_request_target_parse_document",
        "ix_processing_request_target_download",
    ):
        op.drop_index(index, table_name=_TARGET, schema=OPS_SCHEMA)
    op.drop_table(_TARGET, schema=OPS_SCHEMA)
    op.drop_index(
        "uq_processing_request_attempt_open", table_name=_ATTEMPT,
        schema=OPS_SCHEMA,
    )
    op.drop_table(_ATTEMPT, schema=OPS_SCHEMA)
    op.drop_index(
        "ix_processing_request_created", table_name=_REQUEST, schema=OPS_SCHEMA,
    )
    op.drop_table(_REQUEST, schema=OPS_SCHEMA)
