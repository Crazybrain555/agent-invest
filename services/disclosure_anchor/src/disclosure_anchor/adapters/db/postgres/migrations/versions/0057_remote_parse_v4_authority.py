"""Add the append-only V4 staged-parse authority and encrypted secrets.

Revision ID: 0057_remote_parse_v4_authority
Revises: 0056_staged_credit_evidence
"""

from typing import Any, Mapping, Sequence, Union

import sqlalchemy as sa
from alembic import op

from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    CORE_SCHEMA,
    FUTURE_L2_READER_ROLE,
    OPS_SCHEMA,
    READER_ROLE,
)


revision: str = "0057_remote_parse_v4_authority"
down_revision: Union[str, None] = "0056_staged_credit_evidence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MAX_INT = (1 << 63) - 1
_MAX_CANONICAL_BYTES = 1024 * 1024
_MAX_WINNER_BYTES = 8 * 1024 * 1024
_SHA_CHECK = "~ '^sha256:[0-9a-f]{64}$'"

_V3_CREDITS = (
    "documents",
    "remote_waits",
    "retained_results",
    "retained_bytes",
    "local_items",
    "compressed_bytes",
    "decoded_bytes",
    "temp_disk_bytes",
    "db_stage_items",
    "db_staged_bytes",
    "ack_items",
    "unpublished_pages",
)
_V3_IDENTITY_COLUMNS = (
    "process_profile_sha256",
    "credit_policy_sha256",
    "reservation_input_sha256",
    "reservation_input_bytes",
    "reservation_input_byte_count",
    "reservation_source_byte_count",
    "reservation_source_page_count",
    "reservation_bucket",
)
_V3_MATERIALIZATION_COLUMNS = (
    "materialization_receipt_sha256",
    "materialization_receipt_bytes",
    "materialization_receipt_byte_count",
    "materialization_source_page_count",
    "materialization_spool_relpath",
    "materialization_spool_sha256",
    "materialization_spool_byte_count",
    "materialization_compressed_byte_count",
    "materialization_uncompressed_byte_count",
    "materialization_temp_disk_byte_count",
    "materialization_decoded_byte_count",
    "materialization_member_count",
    "materialization_token_sha256",
)
_V3_COLUMNS = (
    *_V3_IDENTITY_COLUMNS,
    *(f"reservation_{name}" for name in _V3_CREDITS),
    *(f"current_{name}" for name in _V3_CREDITS),
    *_V3_MATERIALIZATION_COLUMNS,
    "local_db_staged_byte_count",
)
_V4_LEGACY_COLUMNS = (
    "remote_task_identity",
    "submitted_receipt_sha256",
    "submitted_receipt_bytes",
    "submitted_receipt_byte_count",
    "terminal_receipt_sha256",
    "terminal_receipt_bytes",
    "terminal_receipt_byte_count",
    "result_owner_identity",
    "result_artifact_sha256",
    "result_artifact_bytes",
    "local_receipt_sha256",
    "local_receipt_bytes",
    "local_receipt_byte_count",
    "failure_receipt_sha256",
    "failure_receipt_bytes",
    "failure_receipt_byte_count",
    "failure_stage",
    *_V3_COLUMNS,
)
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
_V4_ATTEMPT_IDENTITY_COLUMNS = (
    "attempt_id",
    "processing_run_id",
    "document_id",
    "attempt_generation",
    "fence_identity",
    "source_pdf_sha256",
    "parser_target_sha256",
    "request_sha256",
    "runtime_epoch_sha256",
    "client_submit_key",
    "checkpoint_contract_version",
    "created_at",
)
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
_V4_ALLOWED_NEW_EVIDENCE_BY_TRANSITION = {
    ("prepared", "reconciling"): ("submission_intent_sha256",),
    ("reconciling", "submitted"): ("accepted_submission_sha256",),
    ("submitted", "remote_terminal"): ("terminal_receipt_sha256",),
    ("remote_terminal", "materializing"): ("materialization_intent_sha256",),
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
_V4_ORDINARY_EVIDENCE_FRONTIER = (
    ("prepared", ("preparation_intent_sha256", "snapshot_receipt_sha256")),
    ("reconciling", ("submission_intent_sha256",)),
    ("submitted", ("accepted_submission_sha256",)),
    ("remote_terminal", ("terminal_receipt_sha256",)),
    ("materializing", ("materialization_intent_sha256",)),
    ("local_materialized", ("local_materialization_receipt_sha256",)),
    ("publish_committed", ("publication_winner_sha256",)),
)
_V4_REQUIRED_EVIDENCE_BY_STATE = {
    "prepared": ("preparation_intent_sha256",),
    "reconciling": (
        "preparation_intent_sha256",
        "snapshot_receipt_sha256",
        "submission_intent_sha256",
    ),
    "submitted": (
        "preparation_intent_sha256",
        "snapshot_receipt_sha256",
        "submission_intent_sha256",
        "accepted_submission_sha256",
    ),
    "remote_terminal": (
        "preparation_intent_sha256",
        "snapshot_receipt_sha256",
        "submission_intent_sha256",
        "accepted_submission_sha256",
        "terminal_receipt_sha256",
    ),
    "materializing": (
        "preparation_intent_sha256",
        "snapshot_receipt_sha256",
        "accepted_submission_sha256",
        "terminal_receipt_sha256",
        "materialization_intent_sha256",
    ),
    "local_materialized": (
        "preparation_intent_sha256",
        "snapshot_receipt_sha256",
        "materialization_intent_sha256",
        "local_materialization_receipt_sha256",
    ),
    "publish_committed": (
        "preparation_intent_sha256",
        "snapshot_receipt_sha256",
        "local_materialization_receipt_sha256",
        "publication_winner_sha256",
    ),
    "cleanup_pending": (
        "preparation_intent_sha256",
        "cleanup_plan_sha256",
    ),
    "ack_pending": (
        "preparation_intent_sha256",
        "accepted_submission_sha256",
        "cleanup_plan_sha256",
        "cleanup_receipt_sha256",
    ),
    "acked": (
        "preparation_intent_sha256",
        "publication_winner_sha256",
        "cleanup_receipt_sha256",
        "ack_receipt_sha256",
    ),
    "remote_failed": (
        "preparation_intent_sha256",
        "failure_receipt_sha256",
        "cleanup_receipt_sha256",
        "ack_receipt_sha256",
    ),
    "local_failed": (
        "preparation_intent_sha256",
        "failure_receipt_sha256",
        "cleanup_receipt_sha256",
        "ack_receipt_sha256",
    ),
    "pre_submission_failed": (
        "preparation_intent_sha256",
        "failure_receipt_sha256",
        "cleanup_receipt_sha256",
    ),
    "superseded": (
        "preparation_intent_sha256",
        "supersession_receipt_sha256",
        "cleanup_receipt_sha256",
    ),
}
_LEGACY_STATES = (
    "prepared",
    "reconciling",
    "submitted",
    "remote_terminal",
    "materializing",
    "local_materialized",
    "finish_committed",
    "remote_failure_committed",
    "local_failure_committed",
    "acked",
    "remote_failed",
    "local_failed",
    "pre_submission_failed",
    "superseded",
)

_V1_TERMINAL = "(state IN ('remote_terminal','materializing','local_materialized','finish_committed','acked','local_failed') AND remote_task_identity IS NOT NULL AND terminal_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND terminal_receipt_bytes IS NOT NULL AND terminal_receipt_byte_count=octet_length(terminal_receipt_bytes) AND terminal_receipt_byte_count BETWEEN 1 AND 65536 AND result_owner_identity IS NOT NULL AND result_artifact_sha256 ~ '^sha256:[0-9a-f]{64}$' AND result_artifact_bytes>0) OR (state IN ('prepared','submitted','remote_failed') AND terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL) OR (state='superseded' AND ((terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL) OR (remote_task_identity IS NOT NULL AND terminal_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND terminal_receipt_bytes IS NOT NULL AND terminal_receipt_byte_count=octet_length(terminal_receipt_bytes) AND terminal_receipt_byte_count BETWEEN 1 AND 65536 AND result_owner_identity IS NOT NULL AND result_artifact_sha256 ~ '^sha256:[0-9a-f]{64}$' AND result_artifact_bytes>0)))"
_V2_TERMINAL = "(state IN ('remote_terminal','materializing','local_materialized','finish_committed','local_failure_committed','acked','local_failed') AND remote_task_identity IS NOT NULL AND terminal_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND terminal_receipt_bytes IS NOT NULL AND terminal_receipt_byte_count=octet_length(terminal_receipt_bytes) AND terminal_receipt_byte_count BETWEEN 1 AND 65536 AND result_owner_identity IS NOT NULL AND result_artifact_sha256 ~ '^sha256:[0-9a-f]{64}$' AND result_artifact_bytes>0) OR (state IN ('prepared','reconciling','submitted','remote_failure_committed','remote_failed','pre_submission_failed') AND terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL AND ((state IN ('remote_failure_committed','remote_failed') AND remote_task_identity IS NOT NULL) OR (state NOT IN ('remote_failure_committed','remote_failed') AND (remote_task_identity IS NULL OR state='submitted')))) OR (state='superseded' AND remote_task_identity IS NULL AND terminal_receipt_sha256 IS NULL AND terminal_receipt_bytes IS NULL AND terminal_receipt_byte_count IS NULL AND result_owner_identity IS NULL AND result_artifact_sha256 IS NULL AND result_artifact_bytes IS NULL)"
_V3_TERMINAL = (
    _V2_TERMINAL.replace(
        "terminal_receipt_sha256 ~",
        "terminal_receipt_sha256 IS NOT NULL AND terminal_receipt_sha256 ~",
    )
    .replace(
        "terminal_receipt_byte_count=",
        "terminal_receipt_byte_count IS NOT NULL AND terminal_receipt_byte_count=",
    )
    .replace(
        "result_artifact_sha256 ~",
        "result_artifact_sha256 IS NOT NULL AND result_artifact_sha256 ~",
    )
    .replace(
        "result_artifact_bytes>0",
        "result_artifact_bytes IS NOT NULL AND result_artifact_bytes>0",
    )
)


def _sql_values(values: Sequence[str]) -> str:
    return ",".join(f"'{value}'" for value in values)


def _all_null(columns: Sequence[str]) -> str:
    return " AND ".join(f"{name} IS NULL" for name in columns) or "TRUE"


def _all_present(columns: Sequence[str]) -> str:
    return " AND ".join(f"{name} IS NOT NULL" for name in columns) or "TRUE"


def _v3_zero() -> str:
    return " AND ".join(f"current_{name}=0" for name in _V3_CREDITS)


def _v3_shape(**values: str) -> str:
    return " AND ".join(
        f"current_{name}={values.get(name, '0')}" for name in _V3_CREDITS
    )


def _held_zero() -> str:
    return " AND ".join(f"held_{name}=0" for name in _HELD_CREDITS)


def _held_shape(
    values: Mapping[str, str],
    *,
    unconstrained: Sequence[str] = (),
) -> str:
    return " AND ".join(
        f"held_{name}={values.get(name, '0')}"
        for name in _HELD_CREDITS
        if name not in unconstrained
    )


def _v4_checkpoint_credit_shape() -> str:
    snapshot = {
        "documents": "1",
        "snapshot_items": "1",
        "snapshot_bytes": "source_byte_count",
    }
    provider = {
        **snapshot,
        "provider_tasks": "1",
        "ack_items": "1",
    }
    arms = [
        f"(state='prepared' AND {_held_shape(snapshot)})",
        "(state='reconciling' AND "
        f"{_held_shape({**snapshot, 'remote_waits': '1'})})",
        "(state='submitted' AND "
        f"{_held_shape({**provider, 'remote_waits': '1'})})",
        "(state='remote_terminal' AND "
        f"{_held_shape(provider, unconstrained=('provider_result_bytes',))} "
        "AND held_provider_result_bytes>0)",
        "(state='materializing' AND "
        f"{_held_shape({**provider, 'materialization_items': '1'}, unconstrained=('provider_result_bytes', 'compressed_bytes', 'decoded_bytes', 'temp_disk_bytes'))} "
        "AND held_provider_result_bytes>0 "
        "AND held_compressed_bytes>=held_provider_result_bytes "
        "AND held_decoded_bytes>0 AND held_temp_disk_bytes>0)",
        "(state IN ('local_materialized','publish_committed') AND "
        f"{_held_shape({**provider, 'output_items': '1', 'output_pages': 'source_page_count'}, unconstrained=('provider_result_bytes', 'compressed_bytes', 'output_bytes'))} "
        "AND held_provider_result_bytes>0 "
        "AND held_compressed_bytes=held_provider_result_bytes "
        "AND held_output_bytes>0)",
        "(state='cleanup_pending' AND "
        f"{_held_shape(snapshot, unconstrained=tuple(name for name in _HELD_CREDITS if name not in snapshot))})",
        "(state='ack_pending' AND "
        f"{_held_shape({'documents': '1', 'provider_tasks': '1', 'ack_items': '1'}, unconstrained=('provider_result_bytes',))} "
        "AND ((terminal_receipt_sha256 IS NULL AND held_provider_result_bytes=0) "
        "OR (terminal_receipt_sha256 IS NOT NULL AND held_provider_result_bytes>0)))",
        f"(state IN ({_sql_values(_V4_FINAL_STATES)}) AND {_held_zero()})",
    ]
    return (
        "source_byte_count>0 AND source_page_count>0 AND ("
        + " OR ".join(arms)
        + ")"
    )


def _v4_initial_checkpoint_shape() -> str:
    prepared_forbidden = tuple(
        name
        for name in _EVIDENCE_COLUMNS
        if name not in {"preparation_intent_sha256", "snapshot_receipt_sha256"}
    )
    preparation_failed_forbidden = tuple(
        name for name in _EVIDENCE_COLUMNS if name != "failure_receipt_sha256"
    )
    superseded_forbidden = tuple(
        name for name in _EVIDENCE_COLUMNS if name != "supersession_receipt_sha256"
    )
    return (
        "(lifecycle_version=0 AND state='prepared' AND "
        "preparation_intent_sha256 IS NOT NULL AND "
        f"{_all_null((*prepared_forbidden, 'publication_winner_sha256'))}) OR "
        "(lifecycle_version=0 AND state='preparation_failed' AND "
        "failure_receipt_sha256 IS NOT NULL AND "
        f"{_all_null((*preparation_failed_forbidden, 'publication_winner_sha256'))} AND "
        f"{_held_zero()}) OR "
        "(lifecycle_version=0 AND state='superseded' AND "
        "supersession_receipt_sha256 IS NOT NULL AND "
        f"{_all_null((*superseded_forbidden, 'publication_winner_sha256'))} AND "
        f"{_held_zero()}) OR "
        "(lifecycle_version>0 AND state NOT IN ('prepared','preparation_failed'))"
    )


def _v4_checkpoint_state_evidence_shape() -> str:
    all_fields = (*_EVIDENCE_COLUMNS, "publication_winner_sha256")
    allowed_by_state: dict[str, set[str]] = {}
    ordinary_allowed: set[str] = set()
    for state, introduced in _V4_ORDINARY_EVIDENCE_FRONTIER:
        ordinary_allowed.update(introduced)
        allowed_by_state[state] = set(ordinary_allowed)

    cleanup_allowed = set(ordinary_allowed)
    cleanup_allowed.update(
        {
            "failure_receipt_sha256",
            "supersession_receipt_sha256",
            "cleanup_plan_sha256",
        }
    )
    allowed_by_state["cleanup_pending"] = set(cleanup_allowed)
    allowed_by_state["ack_pending"] = {
        *cleanup_allowed,
        "cleanup_receipt_sha256",
    }
    final_allowed = {
        *cleanup_allowed,
        "cleanup_receipt_sha256",
        "ack_receipt_sha256",
    }
    for state in _V4_FINAL_STATES:
        allowed_by_state[state] = set(final_allowed)

    no_conflicting_outcome = (
        "(failure_receipt_sha256 IS NULL OR "
        "supersession_receipt_sha256 IS NULL) AND "
        "(publication_winner_sha256 IS NULL OR "
        "(failure_receipt_sha256 IS NULL AND "
        "supersession_receipt_sha256 IS NULL))"
    )
    arms = [
        "(lifecycle_version=0 AND state='preparation_failed' AND "
        "failure_receipt_sha256 IS NOT NULL AND "
        f"{_all_null(tuple(name for name in all_fields if name != 'failure_receipt_sha256'))})",
        "(lifecycle_version=0 AND state='superseded' AND "
        "supersession_receipt_sha256 IS NOT NULL AND "
        f"{_all_null(tuple(name for name in all_fields if name != 'supersession_receipt_sha256'))})",
    ]
    for state in (*_V4_CURRENT_STATES, *_V4_FINAL_STATES):
        if state == "preparation_failed":
            continue
        version_shape = (
            "lifecycle_version=0"
            if state == "prepared"
            else "lifecycle_version>0"
        )
        required = _V4_REQUIRED_EVIDENCE_BY_STATE[state]
        forbidden = tuple(
            name for name in all_fields if name not in allowed_by_state[state]
        )
        special = []
        if state in {"cleanup_pending", "ack_pending"}:
            special.append(
                "(publication_winner_sha256 IS NOT NULL OR "
                "failure_receipt_sha256 IS NOT NULL OR "
                "supersession_receipt_sha256 IS NOT NULL)"
            )
        if state == "pre_submission_failed":
            special.append(
                "accepted_submission_sha256 IS NULL AND "
                "ack_receipt_sha256 IS NULL"
            )
        if state == "superseded":
            special.append(
                "((accepted_submission_sha256 IS NULL AND "
                "ack_receipt_sha256 IS NULL) OR "
                "(accepted_submission_sha256 IS NOT NULL AND "
                "ack_receipt_sha256 IS NOT NULL))"
            )
        arm_parts = [
            version_shape,
            f"state='{state}'",
            _all_present(required),
            _all_null(forbidden),
            no_conflicting_outcome,
            *special,
        ]
        arms.append("(" + " AND ".join(arm_parts) + ")")
    return " OR ".join(arms)


_V3_MATERIALIZATION_PRESENT = (
    _all_present(_V3_MATERIALIZATION_COLUMNS)
    + " AND materialization_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND "
    "materialization_receipt_byte_count=octet_length(materialization_receipt_bytes) AND "
    "materialization_receipt_byte_count BETWEEN 1 AND 65536 AND "
    "materialization_source_page_count>0 AND materialization_spool_byte_count>0 "
    "AND materialization_compressed_byte_count=materialization_spool_byte_count "
    "AND materialization_uncompressed_byte_count>0 AND materialization_decoded_byte_count>0 "
    "AND materialization_temp_disk_byte_count="
    "materialization_spool_byte_count+materialization_uncompressed_byte_count "
    "AND materialization_member_count>0 AND materialization_spool_relpath !~ '(^/|(^|/)\\.\\.?(/|$)|\\\\)' "
    "AND materialization_spool_sha256 ~ '^sha256:[0-9a-f]{64}$' "
    "AND materialization_token_sha256 ~ '^sha256:[0-9a-f]{64}$'"
)


def _v3_contract_shape(*, include_v4: bool) -> str:
    versions = "1,2,3,4" if include_v4 else "1,2,3"
    v4_arm = (
        " OR (checkpoint_contract_version=4 AND "
        f"{_all_null(_V4_LEGACY_COLUMNS)} AND "
        "current_checkpoint_sha256 IS NOT NULL AND "
        "current_checkpoint_sha256 ~ '^sha256:[0-9a-f]{64}$')"
        if include_v4
        else ""
    )
    old_pointer = " AND current_checkpoint_sha256 IS NULL" if include_v4 else ""
    return (
        f"checkpoint_contract_version IN ({versions}) AND ("
        f"(checkpoint_contract_version<3 AND {_all_null(_V3_COLUMNS)}{old_pointer}) OR "
        "(checkpoint_contract_version=3 AND "
        f"{_all_present(_V3_IDENTITY_COLUMNS)} AND "
        "process_profile_sha256 ~ '^sha256:[0-9a-f]{64}$' AND "
        "credit_policy_sha256 ~ '^sha256:[0-9a-f]{64}$' AND "
        "reservation_input_sha256 ~ '^sha256:[0-9a-f]{64}$' AND "
        "reservation_input_byte_count=octet_length(reservation_input_bytes) AND "
        "reservation_input_byte_count BETWEEN 1 AND 65536 AND "
        "reservation_source_byte_count>0 AND reservation_source_page_count>0 AND "
        "reservation_bucket IN ('regular','heavy','huge') AND "
        f"{_all_present(tuple(f'reservation_{n}' for n in _V3_CREDITS) + tuple(f'current_{n}' for n in _V3_CREDITS))}{old_pointer})"
        f"{v4_arm})"
    )


def _v3_credit_bounds(*, old: bool = False) -> str:
    guard = "checkpoint_contract_version<3" if old else "checkpoint_contract_version<>3"
    return (
        guard
        + " OR ("
        + " AND ".join(
            f"reservation_{name}>=0 AND current_{name}>=0 AND current_{name}<=reservation_{name}"
            for name in _V3_CREDITS
        )
        + ")"
    )


def _v3_materialization_shape(*, old: bool = False) -> str:
    guard = "checkpoint_contract_version<3" if old else "checkpoint_contract_version<>3"
    return (
        guard + " OR "
        f"(state IN ('prepared','reconciling','submitted','remote_terminal','remote_failure_committed','remote_failed','pre_submission_failed','superseded') AND {_all_null(_V3_MATERIALIZATION_COLUMNS)}) OR "
        f"(state IN ('materializing','local_materialized','finish_committed','acked') AND {_V3_MATERIALIZATION_PRESENT}) OR "
        f"(state IN ('local_failure_committed','local_failed') AND (({_all_null(_V3_MATERIALIZATION_COLUMNS)}) OR ({_V3_MATERIALIZATION_PRESENT})))"
    )


def _v3_state_credit(*, old: bool = False) -> str:
    guard = "checkpoint_contract_version<3" if old else "checkpoint_contract_version<>3"
    return (
        guard + " OR "
        f"(state='prepared' AND {_v3_shape(documents='1')}) OR "
        f"(state IN ('reconciling','submitted') AND {_v3_shape(documents='1', remote_waits='1')}) OR "
        f"(state='remote_terminal' AND {_v3_shape(documents='1', retained_results='1', retained_bytes='result_artifact_bytes')}) OR "
        f"(state='materializing' AND {_v3_shape(documents='1', retained_results='1', retained_bytes='result_artifact_bytes', local_items='1', compressed_bytes='materialization_compressed_byte_count', decoded_bytes='materialization_decoded_byte_count', temp_disk_bytes='materialization_temp_disk_byte_count')}) OR "
        f"(state='local_materialized' AND {_v3_shape(documents='1', retained_results='1', retained_bytes='result_artifact_bytes', db_stage_items='1', db_staged_bytes='local_db_staged_byte_count', unpublished_pages='reservation_source_page_count')}) OR "
        f"(state='finish_committed' AND {_v3_shape(documents='1', retained_results='1', retained_bytes='result_artifact_bytes', ack_items='1')}) OR "
        f"(state='remote_failure_committed' AND {_v3_shape(documents='1', retained_results='1', ack_items='1')}) OR "
        f"(state='local_failure_committed' AND materialization_receipt_bytes IS NULL AND {_v3_shape(documents='1', retained_results='1', retained_bytes='result_artifact_bytes', ack_items='1')}) OR "
        f"(state='local_failure_committed' AND materialization_receipt_bytes IS NOT NULL AND local_receipt_bytes IS NULL AND {_v3_shape(documents='1', retained_results='1', retained_bytes='result_artifact_bytes', local_items='1', compressed_bytes='materialization_compressed_byte_count', decoded_bytes='materialization_decoded_byte_count', temp_disk_bytes='materialization_temp_disk_byte_count', ack_items='1')}) OR "
        f"(state='local_failure_committed' AND local_receipt_bytes IS NOT NULL AND {_v3_shape(documents='1', retained_results='1', retained_bytes='result_artifact_bytes', local_items='1', compressed_bytes='materialization_compressed_byte_count', decoded_bytes='materialization_decoded_byte_count', temp_disk_bytes='materialization_temp_disk_byte_count', db_stage_items='1', db_staged_bytes='local_db_staged_byte_count', ack_items='1', unpublished_pages='reservation_source_page_count')}) OR "
        f"(state IN ('acked','remote_failed','local_failed','pre_submission_failed','superseded') AND {_v3_zero()})"
    )


def _legacy_initial_shape() -> str:
    return "(checkpoint_contract_version=1 AND ((state='prepared' AND row_version=0 AND remote_task_identity IS NULL) OR (state<>'prepared' AND row_version>=1))) OR (checkpoint_contract_version IN (2,3) AND ((state IN ('prepared','reconciling') AND remote_task_identity IS NULL) OR (state NOT IN ('prepared','reconciling') AND row_version>=1)))"


def _legacy_claim_shape() -> str:
    return "(checkpoint_contract_version=1 AND claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (checkpoint_contract_version IN (2,3) AND state='prepared' AND ((claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (claim_generation>=1 AND claim_owner_identity IS NOT NULL AND claim_lease_until IS NOT NULL))) OR (checkpoint_contract_version IN (2,3) AND state IN ('reconciling','submitted','remote_terminal','materializing','local_materialized','finish_committed','remote_failure_committed','local_failure_committed') AND claim_generation>=1 AND claim_owner_identity IS NOT NULL AND claim_lease_until IS NOT NULL) OR (checkpoint_contract_version IN (2,3) AND state IN ('acked','remote_failed','local_failed','pre_submission_failed','superseded') AND claim_generation>=1 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL)"


def _legacy_local_receipt() -> str:
    return "checkpoint_contract_version=1 OR (checkpoint_contract_version=2 AND ((state IN ('local_materialized','finish_committed','acked') AND local_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND local_receipt_bytes IS NOT NULL AND local_receipt_byte_count=octet_length(local_receipt_bytes) AND local_receipt_byte_count BETWEEN 1 AND 65536) OR (state NOT IN ('local_materialized','finish_committed','acked') AND local_receipt_sha256 IS NULL AND local_receipt_bytes IS NULL AND local_receipt_byte_count IS NULL))) OR (checkpoint_contract_version=3 AND ((state IN ('local_materialized','finish_committed','acked') AND local_receipt_sha256 IS NOT NULL AND local_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND local_receipt_bytes IS NOT NULL AND local_receipt_byte_count IS NOT NULL AND local_receipt_byte_count=octet_length(local_receipt_bytes) AND local_receipt_byte_count BETWEEN 1 AND 65536 AND local_db_staged_byte_count IS NOT NULL AND local_db_staged_byte_count>0) OR (state IN ('local_failure_committed','local_failed') AND ((local_receipt_sha256 IS NULL AND local_receipt_bytes IS NULL AND local_receipt_byte_count IS NULL AND local_db_staged_byte_count IS NULL) OR (local_receipt_sha256 IS NOT NULL AND local_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND local_receipt_bytes IS NOT NULL AND local_receipt_byte_count IS NOT NULL AND local_receipt_byte_count=octet_length(local_receipt_bytes) AND local_receipt_byte_count BETWEEN 1 AND 65536 AND local_db_staged_byte_count IS NOT NULL AND local_db_staged_byte_count>0))) OR (state NOT IN ('local_materialized','finish_committed','acked','local_failure_committed','local_failed') AND local_receipt_sha256 IS NULL AND local_receipt_bytes IS NULL AND local_receipt_byte_count IS NULL AND local_db_staged_byte_count IS NULL)))"


def _legacy_submitted_shape() -> str:
    return "(checkpoint_contract_version=1 AND (state <> 'submitted' OR remote_task_identity IS NOT NULL)) OR (checkpoint_contract_version=2 AND ((state IN ('prepared','reconciling','pre_submission_failed','superseded') AND submitted_receipt_sha256 IS NULL AND submitted_receipt_bytes IS NULL AND submitted_receipt_byte_count IS NULL) OR (state NOT IN ('prepared','reconciling','pre_submission_failed','superseded') AND remote_task_identity IS NOT NULL AND submitted_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND submitted_receipt_bytes IS NOT NULL AND submitted_receipt_byte_count=octet_length(submitted_receipt_bytes) AND submitted_receipt_byte_count BETWEEN 1 AND 65536))) OR (checkpoint_contract_version=3 AND ((state IN ('prepared','reconciling','pre_submission_failed','superseded') AND submitted_receipt_sha256 IS NULL AND submitted_receipt_bytes IS NULL AND submitted_receipt_byte_count IS NULL) OR (state NOT IN ('prepared','reconciling','pre_submission_failed','superseded') AND remote_task_identity IS NOT NULL AND submitted_receipt_sha256 IS NOT NULL AND submitted_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND submitted_receipt_bytes IS NOT NULL AND submitted_receipt_byte_count IS NOT NULL AND submitted_receipt_byte_count=octet_length(submitted_receipt_bytes) AND submitted_receipt_byte_count BETWEEN 1 AND 65536)))"


def _legacy_failure_receipt() -> str:
    return "checkpoint_contract_version=1 OR (checkpoint_contract_version=2 AND ((state IN ('remote_failure_committed','remote_failed','pre_submission_failed') AND failure_stage='remote' AND failure_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND failure_receipt_bytes IS NOT NULL AND failure_receipt_byte_count=octet_length(failure_receipt_bytes) AND failure_receipt_byte_count BETWEEN 1 AND 65536) OR (state IN ('local_failure_committed','local_failed') AND failure_stage='local' AND failure_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND failure_receipt_bytes IS NOT NULL AND failure_receipt_byte_count=octet_length(failure_receipt_bytes) AND failure_receipt_byte_count BETWEEN 1 AND 65536) OR (state NOT IN ('remote_failure_committed','remote_failed','pre_submission_failed','local_failure_committed','local_failed') AND failure_receipt_sha256 IS NULL AND failure_receipt_bytes IS NULL AND failure_receipt_byte_count IS NULL AND failure_stage IS NULL))) OR (checkpoint_contract_version=3 AND ((state IN ('remote_failure_committed','remote_failed','pre_submission_failed') AND failure_stage='remote' AND failure_receipt_sha256 IS NOT NULL AND failure_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND failure_receipt_bytes IS NOT NULL AND failure_receipt_byte_count IS NOT NULL AND failure_receipt_byte_count=octet_length(failure_receipt_bytes) AND failure_receipt_byte_count BETWEEN 1 AND 65536) OR (state IN ('local_failure_committed','local_failed') AND failure_stage='local' AND failure_receipt_sha256 IS NOT NULL AND failure_receipt_sha256 ~ '^sha256:[0-9a-f]{64}$' AND failure_receipt_bytes IS NOT NULL AND failure_receipt_byte_count IS NOT NULL AND failure_receipt_byte_count=octet_length(failure_receipt_bytes) AND failure_receipt_byte_count BETWEEN 1 AND 65536) OR (state NOT IN ('remote_failure_committed','remote_failed','pre_submission_failed','local_failure_committed','local_failed') AND failure_receipt_sha256 IS NULL AND failure_receipt_bytes IS NULL AND failure_receipt_byte_count IS NULL AND failure_stage IS NULL)))"


def _upgrade_attempt_constraints() -> None:
    with op.batch_alter_table("remote_parse_attempt", schema=OPS_SCHEMA) as batch:
        for name in (
            "ck_remote_parse_attempt_versions",
            "ck_remote_parse_attempt_contract_version",
            "ck_remote_parse_attempt_v3_credit_bounds",
            "ck_remote_parse_attempt_v3_final_zero",
            "ck_remote_parse_attempt_v3_materialization",
            "ck_remote_parse_attempt_v3_local_projection",
            "ck_remote_parse_attempt_v3_state_credit",
            "ck_remote_parse_attempt_state",
            "ck_remote_parse_attempt_lifecycle_shape",
            "ck_remote_parse_attempt_initial_shape",
            "ck_remote_parse_attempt_claim_shape",
            "ck_remote_parse_attempt_local_receipt",
            "ck_remote_parse_attempt_failure_receipt",
            "ck_remote_parse_attempt_submitted_shape",
            "ck_remote_parse_attempt_terminal_shape",
        ):
            batch.drop_constraint(name, type_="check")
        batch.add_column(sa.Column("current_checkpoint_sha256", sa.String(71)))
        batch.create_unique_constraint(
            "uq_remote_parse_attempt_v4_parent_identity",
            ["attempt_id", "fence_identity"],
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_versions",
            f"attempt_generation >= 1 AND row_version BETWEEN 0 AND {_MAX_INT} AND claim_generation BETWEEN 0 AND {_MAX_INT}",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_contract_version",
            _v3_contract_shape(include_v4=True),
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_credit_bounds",
            _v3_credit_bounds(),
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_final_zero",
            "checkpoint_contract_version<>3 OR state NOT IN ('acked','remote_failed','local_failed','pre_submission_failed','superseded') OR ("
            + _v3_zero()
            + ")",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_materialization",
            _v3_materialization_shape(),
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_local_projection",
            "checkpoint_contract_version<>3 OR ((local_receipt_bytes IS NULL AND local_db_staged_byte_count IS NULL) OR (local_receipt_bytes IS NOT NULL AND local_db_staged_byte_count IS NOT NULL AND local_db_staged_byte_count>0))",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_state_credit",
            _v3_state_credit(),
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_state",
            f"(checkpoint_contract_version IN (1,2,3) AND state IN ({_sql_values(_LEGACY_STATES)})) OR (checkpoint_contract_version=4 AND state IN ({_sql_values((*_V4_CURRENT_STATES, *_V4_FINAL_STATES))}))",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_lifecycle_shape",
            f"(checkpoint_contract_version IN (1,2,3) AND ((state IN ('prepared','reconciling','submitted','remote_terminal','materializing','local_materialized','finish_committed','remote_failure_committed','local_failure_committed') AND is_current) OR (state IN ('acked','remote_failed','local_failed','pre_submission_failed','superseded') AND NOT is_current))) OR (checkpoint_contract_version=4 AND ((state IN ({_sql_values(_V4_CURRENT_STATES)}) AND is_current) OR (state IN ({_sql_values(_V4_FINAL_STATES)}) AND NOT is_current)))",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_initial_shape",
            "("
            + _legacy_initial_shape()
            + ") OR (checkpoint_contract_version=4 AND ((row_version=0 AND state IN ('prepared','preparation_failed','superseded')) OR (row_version>0 AND state NOT IN ('prepared','preparation_failed'))))",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_claim_shape",
            "("
            + _legacy_claim_shape()
            + f") OR (checkpoint_contract_version=4 AND state IN ({_sql_values(_V4_CURRENT_STATES)}) AND ((state='prepared' AND claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (claim_generation BETWEEN 1 AND {_MAX_INT} AND claim_owner_identity IS NOT NULL AND btrim(claim_owner_identity)<>'' AND claim_lease_until IS NOT NULL))) OR (checkpoint_contract_version=4 AND state IN ({_sql_values(_V4_FINAL_STATES)}) AND (((row_version=0 AND state IN ('preparation_failed','superseded')) AND claim_generation=0 AND claim_owner_identity IS NULL AND claim_lease_until IS NULL) OR (row_version>0 AND claim_generation BETWEEN 1 AND {_MAX_INT} AND claim_owner_identity IS NULL AND claim_lease_until IS NULL)))",
        )
        v4_legacy_null = _all_null(_V4_LEGACY_COLUMNS)
        batch.create_check_constraint(
            "ck_remote_parse_attempt_local_receipt",
            f"({_legacy_local_receipt()}) OR (checkpoint_contract_version=4 AND {v4_legacy_null})",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_failure_receipt",
            f"({_legacy_failure_receipt()}) OR (checkpoint_contract_version=4 AND {v4_legacy_null})",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_submitted_shape",
            f"({_legacy_submitted_shape()}) OR (checkpoint_contract_version=4 AND {v4_legacy_null})",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_terminal_shape",
            f"(checkpoint_contract_version=1 AND ({_V1_TERMINAL})) OR (checkpoint_contract_version=2 AND ({_V2_TERMINAL})) OR (checkpoint_contract_version=3 AND ({_V3_TERMINAL})) OR (checkpoint_contract_version=4 AND {v4_legacy_null})",
        )


def _create_v4_tables() -> None:
    op.create_table(
        "remote_parse_v4_evidence",
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("fence_identity", sa.String(128), nullable=False),
        sa.Column("evidence_kind", sa.String(64), nullable=False),
        sa.Column("evidence_sha256", sa.String(71), nullable=False),
        sa.Column("evidence_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("evidence_byte_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("attempt_id", "evidence_kind"),
        sa.UniqueConstraint(
            "attempt_id",
            "evidence_sha256",
            name="uq_remote_parse_v4_evidence_hash",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "fence_identity"],
            [
                f"{OPS_SCHEMA}.remote_parse_attempt.attempt_id",
                f"{OPS_SCHEMA}.remote_parse_attempt.fence_identity",
            ],
            name="fk_remote_parse_v4_evidence_parent",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            f"evidence_kind IN ({_sql_values(_EVIDENCE_KINDS)})",
            name="ck_remote_parse_v4_evidence_kind",
        ),
        sa.CheckConstraint(
            f"evidence_sha256 {_SHA_CHECK} AND evidence_byte_count=octet_length(evidence_bytes) AND evidence_byte_count BETWEEN 1 AND {_MAX_CANONICAL_BYTES}",
            name="ck_remote_parse_v4_evidence_identity",
        ),
        schema=OPS_SCHEMA,
    )

    checkpoint_columns: list[sa.Column[Any]] = [
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("fence_identity", sa.String(128), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("lifecycle_version", sa.BigInteger(), nullable=False),
        sa.Column("previous_checkpoint_sha256", sa.String(71)),
        sa.Column("checkpoint_sha256", sa.String(71), nullable=False),
        sa.Column("checkpoint_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("checkpoint_byte_count", sa.Integer(), nullable=False),
        sa.Column("resource_reservation_sha256", sa.String(71)),
        sa.Column("resource_reservation_bytes", sa.LargeBinary()),
        sa.Column("resource_reservation_byte_count", sa.Integer()),
        sa.Column("source_byte_count", sa.BigInteger(), nullable=False),
        sa.Column("source_page_count", sa.BigInteger(), nullable=False),
    ]
    checkpoint_columns.extend(
        sa.Column(f"held_{name}", sa.BigInteger(), nullable=False)
        for name in _HELD_CREDITS
    )
    checkpoint_columns.extend(
        sa.Column(name, sa.String(71))
        for name in (*_EVIDENCE_COLUMNS, "publication_winner_sha256")
    )
    checkpoint_columns.append(
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        )
    )
    op.create_table(
        "remote_parse_v4_checkpoint",
        *checkpoint_columns,
        sa.PrimaryKeyConstraint("attempt_id", "lifecycle_version"),
        sa.UniqueConstraint(
            "attempt_id",
            "lifecycle_version",
            "checkpoint_sha256",
            name="uq_remote_parse_v4_checkpoint_pointer",
        ),
        sa.UniqueConstraint(
            "attempt_id",
            "checkpoint_sha256",
            name="uq_remote_parse_v4_checkpoint_hash",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "fence_identity"],
            [
                f"{OPS_SCHEMA}.remote_parse_attempt.attempt_id",
                f"{OPS_SCHEMA}.remote_parse_attempt.fence_identity",
            ],
            name="fk_remote_parse_v4_checkpoint_parent",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "previous_checkpoint_sha256"],
            [
                f"{OPS_SCHEMA}.remote_parse_v4_checkpoint.attempt_id",
                f"{OPS_SCHEMA}.remote_parse_v4_checkpoint.checkpoint_sha256",
            ],
            name="fk_remote_parse_v4_checkpoint_predecessor",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            f"lifecycle_version BETWEEN 0 AND {_MAX_INT} AND ((lifecycle_version=0 AND previous_checkpoint_sha256 IS NULL) OR (lifecycle_version>0 AND previous_checkpoint_sha256 IS NOT NULL AND previous_checkpoint_sha256 {_SHA_CHECK}))",
            name="ck_remote_parse_v4_checkpoint_version",
        ),
        sa.CheckConstraint(
            f"state IN ({_sql_values((*_V4_CURRENT_STATES, *_V4_FINAL_STATES))})",
            name="ck_remote_parse_v4_checkpoint_state",
        ),
        sa.CheckConstraint(
            f"checkpoint_sha256 {_SHA_CHECK} AND checkpoint_byte_count=octet_length(checkpoint_bytes) AND checkpoint_byte_count BETWEEN 1 AND {_MAX_CANONICAL_BYTES}",
            name="ck_remote_parse_v4_checkpoint_identity",
        ),
        sa.CheckConstraint(
            "(lifecycle_version=0 AND state='prepared' AND resource_reservation_sha256 IS NOT NULL AND resource_reservation_sha256 ~ '^sha256:[0-9a-f]{64}$' AND resource_reservation_bytes IS NOT NULL AND resource_reservation_byte_count IS NOT NULL AND resource_reservation_byte_count=octet_length(resource_reservation_bytes) AND resource_reservation_byte_count BETWEEN 1 AND 1048576) OR ((lifecycle_version>0 OR state IN ('preparation_failed','superseded')) AND resource_reservation_sha256 IS NULL AND resource_reservation_bytes IS NULL AND resource_reservation_byte_count IS NULL)",
            name="ck_remote_parse_v4_checkpoint_reservation",
        ),
        sa.CheckConstraint(
            _v4_initial_checkpoint_shape(),
            name="ck_remote_parse_v4_checkpoint_initial_shape",
        ),
        sa.CheckConstraint(
            f"({_v4_checkpoint_state_evidence_shape()}) IS TRUE",
            name="ck_remote_parse_v4_checkpoint_state_evidence",
        ),
        sa.CheckConstraint(
            " AND ".join(
                f"held_{name} BETWEEN 0 AND {_MAX_INT}" for name in _HELD_CREDITS
            ),
            name="ck_remote_parse_v4_checkpoint_credit_bounds",
        ),
        sa.CheckConstraint(
            f"({_v4_checkpoint_credit_shape()}) IS TRUE",
            name="ck_remote_parse_v4_checkpoint_credit_shape",
        ),
        sa.CheckConstraint(
            f"state NOT IN ({_sql_values(_V4_FINAL_STATES)}) OR ({_held_zero()})",
            name="ck_remote_parse_v4_checkpoint_final_zero",
        ),
        schema=OPS_SCHEMA,
    )

    op.create_table(
        "atomic_publication_winner_v4",
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("fence_identity", sa.String(128), nullable=False),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("processing_run_id", sa.String(64), nullable=False),
        sa.Column("publish_attempt_generation", sa.BigInteger(), nullable=False),
        sa.Column("local_checkpoint_sha256", sa.String(71), nullable=False),
        sa.Column("lifecycle_version_before", sa.BigInteger(), nullable=False),
        sa.Column("lifecycle_version_after", sa.BigInteger(), nullable=False),
        sa.Column("request_sha256", sa.String(71), nullable=False),
        sa.Column("upstream_evidence_sha256", sa.String(71), nullable=False),
        sa.Column("final_units_sha256", sa.String(71), nullable=False),
        sa.Column("lineage_sha256", sa.String(71), nullable=False),
        sa.Column("processing_run_row_sha256", sa.String(71), nullable=False),
        sa.Column("previous_active_run_id", sa.String(64)),
        sa.Column("inserted_count", sa.BigInteger(), nullable=False),
        sa.Column("updated_count", sa.BigInteger(), nullable=False),
        sa.Column("deleted_count", sa.BigInteger(), nullable=False),
        sa.Column("publish_precommit_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("winner_sha256", sa.String(71), nullable=False),
        sa.Column("winner_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("winner_byte_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("attempt_id", "processing_run_id"),
        sa.UniqueConstraint(
            "attempt_id",
            "winner_sha256",
            name="uq_atomic_publication_winner_v4_hash",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "fence_identity"],
            [
                f"{OPS_SCHEMA}.remote_parse_attempt.attempt_id",
                f"{OPS_SCHEMA}.remote_parse_attempt.fence_identity",
            ],
            name="fk_atomic_publication_winner_v4_parent",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "lifecycle_version_before", "local_checkpoint_sha256"],
            [
                f"{OPS_SCHEMA}.remote_parse_v4_checkpoint.attempt_id",
                f"{OPS_SCHEMA}.remote_parse_v4_checkpoint.lifecycle_version",
                f"{OPS_SCHEMA}.remote_parse_v4_checkpoint.checkpoint_sha256",
            ],
            name="fk_atomic_publication_winner_v4_checkpoint",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["processing_run_id", "document_id"],
            [
                f"{CORE_SCHEMA}.processing_run.processing_run_id",
                f"{CORE_SCHEMA}.processing_run.document_id",
            ],
            name="fk_atomic_publication_winner_v4_run_owner",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"publish_attempt_generation BETWEEN 1 AND {_MAX_INT} AND lifecycle_version_before BETWEEN 0 AND {_MAX_INT - 1} AND lifecycle_version_after=lifecycle_version_before+1",
            name="ck_atomic_publication_winner_v4_versions",
        ),
        sa.CheckConstraint(
            " AND ".join(
                f"{name} {_SHA_CHECK}"
                for name in (
                    "local_checkpoint_sha256",
                    "request_sha256",
                    "upstream_evidence_sha256",
                    "final_units_sha256",
                    "lineage_sha256",
                    "processing_run_row_sha256",
                    "winner_sha256",
                )
            )
            + f" AND winner_byte_count=octet_length(winner_bytes) AND winner_byte_count BETWEEN 1 AND {_MAX_WINNER_BYTES}",
            name="ck_atomic_publication_winner_v4_identity",
        ),
        sa.CheckConstraint(
            f"inserted_count BETWEEN 1 AND {_MAX_INT} AND updated_count BETWEEN 0 AND {_MAX_INT} AND deleted_count BETWEEN 0 AND {_MAX_INT}",
            name="ck_atomic_publication_winner_v4_counts",
        ),
        schema=OPS_SCHEMA,
    )

    op.create_table(
        "remote_parse_v4_secret",
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("fence_identity", sa.String(128), nullable=False),
        sa.Column("accepted_submission_sha256", sa.String(71), nullable=False),
        sa.Column("secret_kind", sa.String(128), nullable=False),
        sa.Column("provider_secret_version", sa.BigInteger(), nullable=False),
        sa.Column("token_sha256", sa.String(71), nullable=False),
        sa.Column("token_byte_count", sa.Integer(), nullable=False),
        sa.Column("encryption_revision", sa.BigInteger(), nullable=False),
        sa.Column("kek_id", sa.String(64), nullable=False),
        sa.Column("wrap_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("data_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("attempt_id", "encryption_revision"),
        sa.ForeignKeyConstraint(
            ["attempt_id", "fence_identity"],
            [
                f"{OPS_SCHEMA}.remote_parse_attempt.attempt_id",
                f"{OPS_SCHEMA}.remote_parse_attempt.fence_identity",
            ],
            name="fk_remote_parse_v4_secret_parent",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "accepted_submission_sha256"],
            [
                f"{OPS_SCHEMA}.remote_parse_v4_evidence.attempt_id",
                f"{OPS_SCHEMA}.remote_parse_v4_evidence.evidence_sha256",
            ],
            name="fk_remote_parse_v4_secret_accepted_evidence",
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"octet_length(secret_kind) BETWEEN 1 AND 128 AND secret_kind=btrim(secret_kind) AND secret_kind !~ '[[:cntrl:]]' AND provider_secret_version BETWEEN 1 AND {_MAX_INT} AND token_sha256 {_SHA_CHECK} AND token_byte_count BETWEEN 1 AND 65536 AND encryption_revision BETWEEN 1 AND {_MAX_INT}",
            name="ck_remote_parse_v4_secret_binding",
        ),
        sa.CheckConstraint(
            "kek_id=btrim(kek_id) AND kek_id !~ '[[:cntrl:]]' AND kek_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$' AND octet_length(wrap_nonce)=12 AND octet_length(wrapped_dek)=48 AND octet_length(data_nonce)=12 AND octet_length(token_ciphertext)=token_byte_count+16",
            name="ck_remote_parse_v4_secret_ciphertext",
        ),
        schema=OPS_SCHEMA,
    )


def _create_v4_foreign_keys() -> None:
    op.create_foreign_key(
        "fk_remote_parse_attempt_v4_current_checkpoint",
        "remote_parse_attempt",
        "remote_parse_v4_checkpoint",
        ["attempt_id", "row_version", "current_checkpoint_sha256"],
        ["attempt_id", "lifecycle_version", "checkpoint_sha256"],
        source_schema=OPS_SCHEMA,
        referent_schema=OPS_SCHEMA,
        onupdate="RESTRICT",
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    for evidence_kind in _EVIDENCE_KINDS:
        op.create_foreign_key(
            f"fk_remote_parse_v4_checkpoint_{evidence_kind}",
            "remote_parse_v4_checkpoint",
            "remote_parse_v4_evidence",
            ["attempt_id", f"{evidence_kind}_sha256"],
            ["attempt_id", "evidence_sha256"],
            source_schema=OPS_SCHEMA,
            referent_schema=OPS_SCHEMA,
            onupdate="RESTRICT",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        )
    op.create_foreign_key(
        "fk_remote_parse_v4_checkpoint_publication_winner",
        "remote_parse_v4_checkpoint",
        "atomic_publication_winner_v4",
        ["attempt_id", "publication_winner_sha256"],
        ["attempt_id", "winner_sha256"],
        source_schema=OPS_SCHEMA,
        referent_schema=OPS_SCHEMA,
        onupdate="RESTRICT",
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )


def _create_v4_triggers() -> None:
    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.reject_remote_parse_v4_immutable_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'remote parse v4 immutable row cannot be changed';
        END $$
    """)
    for table in (
        "remote_parse_v4_evidence",
        "remote_parse_v4_checkpoint",
        "atomic_publication_winner_v4",
    ):
        op.execute(
            f"CREATE TRIGGER ck_{table}_immutable BEFORE UPDATE OR DELETE ON "
            f"{OPS_SCHEMA}.{table} FOR EACH ROW EXECUTE FUNCTION "
            f"{OPS_SCHEMA}.reject_remote_parse_v4_immutable_change()"
        )

    immutable_identity_drift = " OR ".join(
        f"OLD.{name} IS DISTINCT FROM NEW.{name}"
        for name in _V4_ATTEMPT_IDENTITY_COLUMNS
    )
    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.reject_remote_parse_v4_head_identity_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF (OLD.checkpoint_contract_version=4 OR NEW.checkpoint_contract_version=4)
             AND ({immutable_identity_drift})
          THEN
            RAISE EXCEPTION 'remote parse v4 head identity is immutable';
          END IF;
          IF OLD.checkpoint_contract_version=4
             AND NEW.checkpoint_contract_version=4
             AND OLD.row_version IS DISTINCT FROM NEW.row_version
             AND (OLD.row_version>={_MAX_INT} OR NEW.row_version<>OLD.row_version+1)
          THEN
            RAISE EXCEPTION 'remote parse v4 head lifecycle version must advance exactly once';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        f"CREATE TRIGGER ck_remote_parse_v4_head_identity BEFORE UPDATE ON "
        f"{OPS_SCHEMA}.remote_parse_attempt FOR EACH ROW EXECUTE FUNCTION "
        f"{OPS_SCHEMA}.reject_remote_parse_v4_head_identity_change()"
    )

    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v4_head_initial_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.checkpoint_contract_version=4
             AND (
               NEW.row_version<>0
               OR NEW.state NOT IN ('prepared','preparation_failed','superseded')
               OR NEW.claim_generation IS DISTINCT FROM 0
               OR NEW.claim_owner_identity IS NOT NULL
               OR NEW.claim_lease_until IS NOT NULL
             )
          THEN
            RAISE EXCEPTION 'remote parse v4 head must be inserted at lifecycle version zero';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        f"CREATE TRIGGER ck_remote_parse_v4_head_initial_insert BEFORE INSERT ON "
        f"{OPS_SCHEMA}.remote_parse_attempt FOR EACH ROW EXECUTE FUNCTION "
        f"{OPS_SCHEMA}.enforce_remote_parse_v4_head_initial_insert()"
    )

    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v4_child_parent()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent_row {OPS_SCHEMA}.remote_parse_attempt%ROWTYPE;
        BEGIN
          SELECT * INTO parent_row
            FROM {OPS_SCHEMA}.remote_parse_attempt
           WHERE attempt_id=NEW.attempt_id;
          IF NOT FOUND
             OR parent_row.checkpoint_contract_version<>4
             OR parent_row.fence_identity IS DISTINCT FROM NEW.fence_identity
          THEN
            RAISE EXCEPTION 'remote parse v4 child lacks exact v4 parent';
          END IF;
          IF TG_TABLE_NAME='remote_parse_v4_checkpoint' THEN
            IF parent_row.row_version < NEW.lifecycle_version THEN
              RAISE EXCEPTION 'remote parse v4 checkpoint is ahead of its head';
            END IF;
          ELSIF TG_TABLE_NAME='atomic_publication_winner_v4' THEN
            IF parent_row.document_id IS DISTINCT FROM NEW.document_id
               OR parent_row.processing_run_id IS DISTINCT FROM NEW.processing_run_id
               OR parent_row.attempt_generation IS DISTINCT FROM NEW.publish_attempt_generation
            THEN
              RAISE EXCEPTION 'remote parse v4 winner parent identity drifted';
            END IF;
          END IF;
          RETURN NEW;
        END $$
    """)
    for table in (
        "remote_parse_v4_evidence",
        "remote_parse_v4_checkpoint",
        "atomic_publication_winner_v4",
        "remote_parse_v4_secret",
    ):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER ck_{table}_v4_parent "
            f"AFTER INSERT ON {OPS_SCHEMA}.{table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
            f"{OPS_SCHEMA}.enforce_remote_parse_v4_child_parent()"
        )

    allowed_transition = " OR ".join(
        f"(predecessor.state='{old}' AND NEW.state IN ({_sql_values(new)}))"
        for old, new in _V4_STATE_TRANSITIONS.items()
    )
    evidence_drift = " OR ".join(
        f"(predecessor.{name} IS NOT NULL AND predecessor.{name} IS DISTINCT FROM NEW.{name})"
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
        for (old, new), allowed in _V4_ALLOWED_NEW_EVIDENCE_BY_TRANSITION.items()
    )
    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v4_checkpoint_chain()
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
    """)
    op.execute(
        f"CREATE CONSTRAINT TRIGGER ck_remote_parse_v4_checkpoint_chain "
        f"AFTER INSERT ON {OPS_SCHEMA}.remote_parse_v4_checkpoint "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
        f"{OPS_SCHEMA}.enforce_remote_parse_v4_checkpoint_chain()"
    )

    evidence_checks = "\n".join(
        f"IF NEW.{kind}_sha256 IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v4_evidence e WHERE e.attempt_id=NEW.attempt_id AND e.fence_identity=NEW.fence_identity AND e.evidence_kind='{kind}' AND e.evidence_sha256=NEW.{kind}_sha256) THEN RAISE EXCEPTION 'remote parse v4 checkpoint {kind} reference drifted'; END IF;"
        for kind in _EVIDENCE_KINDS
    )
    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v4_checkpoint_references()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          {evidence_checks}
          IF NEW.publication_winner_sha256 IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM {OPS_SCHEMA}.atomic_publication_winner_v4 w
             WHERE w.attempt_id=NEW.attempt_id
               AND w.fence_identity=NEW.fence_identity
               AND w.winner_sha256=NEW.publication_winner_sha256
          ) THEN RAISE EXCEPTION 'remote parse v4 publication winner reference drifted'; END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        f"CREATE CONSTRAINT TRIGGER ck_remote_parse_v4_checkpoint_references "
        f"AFTER INSERT ON {OPS_SCHEMA}.remote_parse_v4_checkpoint "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
        f"{OPS_SCHEMA}.enforce_remote_parse_v4_checkpoint_references()"
    )

    evidence_reference_arms = " OR ".join(
        f"(NEW.evidence_kind='{kind}' AND c.{kind}_sha256=NEW.evidence_sha256)"
        for kind in _EVIDENCE_KINDS
    )
    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v4_evidence_referenced()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v4_checkpoint c
             WHERE c.attempt_id=NEW.attempt_id
               AND c.fence_identity=NEW.fence_identity
               AND ({evidence_reference_arms})
          ) THEN
            RAISE EXCEPTION 'remote parse v4 evidence is not closed by a checkpoint';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        f"CREATE CONSTRAINT TRIGGER ck_remote_parse_v4_evidence_referenced "
        f"AFTER INSERT ON {OPS_SCHEMA}.remote_parse_v4_evidence "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
        f"{OPS_SCHEMA}.enforce_remote_parse_v4_evidence_referenced()"
    )

    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.enforce_atomic_publication_winner_v4_referenced()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v4_checkpoint c
             WHERE c.attempt_id=NEW.attempt_id
               AND c.fence_identity=NEW.fence_identity
               AND c.state='publish_committed'
               AND c.lifecycle_version=NEW.lifecycle_version_after
               AND c.previous_checkpoint_sha256=NEW.local_checkpoint_sha256
               AND c.publication_winner_sha256=NEW.winner_sha256
          ) THEN
            RAISE EXCEPTION 'remote parse v4 publication winner lacks its committed checkpoint';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        f"CREATE CONSTRAINT TRIGGER ck_atomic_publication_winner_v4_referenced "
        f"AFTER INSERT ON {OPS_SCHEMA}.atomic_publication_winner_v4 "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
        f"{OPS_SCHEMA}.enforce_atomic_publication_winner_v4_referenced()"
    )

    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v4_head()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE checkpoint_state text;
        DECLARE checkpoint_fence text;
        BEGIN
          IF NEW.checkpoint_contract_version=4 THEN
            SELECT state, fence_identity INTO checkpoint_state, checkpoint_fence
              FROM {OPS_SCHEMA}.remote_parse_v4_checkpoint
             WHERE attempt_id=NEW.attempt_id
               AND lifecycle_version=NEW.row_version
               AND checkpoint_sha256=NEW.current_checkpoint_sha256;
            IF NOT FOUND OR checkpoint_state IS DISTINCT FROM NEW.state OR checkpoint_fence IS DISTINCT FROM NEW.fence_identity THEN
              RAISE EXCEPTION 'remote parse v4 head/checkpoint pointer drifted';
            END IF;
            IF EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_resume_secret s WHERE s.attempt_id=NEW.attempt_id)
               OR EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v3_resume_secret s WHERE s.attempt_id=NEW.attempt_id)
            THEN RAISE EXCEPTION 'remote parse v4 attempt contains plaintext resume secret'; END IF;
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        f"CREATE CONSTRAINT TRIGGER ck_remote_parse_v4_head "
        f"AFTER INSERT OR UPDATE ON {OPS_SCHEMA}.remote_parse_attempt "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
        f"{OPS_SCHEMA}.enforce_remote_parse_v4_head()"
    )

    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v4_secret_lifecycle()
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
          ELSIF head_current IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'remote parse v4 nonfinal head is not current';
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
    op.execute(
        f"CREATE CONSTRAINT TRIGGER ck_remote_parse_v4_secret_lifecycle_head "
        f"AFTER INSERT OR UPDATE ON {OPS_SCHEMA}.remote_parse_attempt "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
        f"{OPS_SCHEMA}.enforce_remote_parse_v4_secret_lifecycle()"
    )
    op.execute(
        f"CREATE CONSTRAINT TRIGGER ck_remote_parse_v4_secret_lifecycle_row "
        f"AFTER INSERT OR DELETE ON {OPS_SCHEMA}.remote_parse_v4_secret "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
        f"{OPS_SCHEMA}.enforce_remote_parse_v4_secret_lifecycle()"
    )
    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.reject_remote_parse_v4_head_delete()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.checkpoint_contract_version=4 THEN
            RAISE EXCEPTION 'remote parse v4 attempt head is retained';
          END IF;
          RETURN OLD;
        END $$
    """)
    op.execute(
        f"CREATE TRIGGER ck_remote_parse_v4_head_retain BEFORE DELETE ON "
        f"{OPS_SCHEMA}.remote_parse_attempt FOR EACH ROW EXECUTE FUNCTION "
        f"{OPS_SCHEMA}.reject_remote_parse_v4_head_delete()"
    )

    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_legacy_secret_parent()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent_version integer;
        BEGIN
          SELECT checkpoint_contract_version INTO parent_version
            FROM {OPS_SCHEMA}.remote_parse_attempt
           WHERE attempt_id=NEW.attempt_id;
          IF NOT FOUND THEN RAISE EXCEPTION 'remote parse secret lacks parent attempt'; END IF;
          IF TG_TABLE_NAME='remote_parse_resume_secret' AND parent_version NOT IN (1,2) THEN
            RAISE EXCEPTION 'legacy resume secret parent contract is invalid';
          END IF;
          IF TG_TABLE_NAME='remote_parse_v3_resume_secret' AND parent_version<>3 THEN
            RAISE EXCEPTION 'v3 resume secret parent contract is invalid';
          END IF;
          RETURN NEW;
        END $$
    """)
    for table in ("remote_parse_resume_secret", "remote_parse_v3_resume_secret"):
        op.execute(
            f"CREATE TRIGGER ck_{table}_parent_contract BEFORE INSERT OR UPDATE ON "
            f"{OPS_SCHEMA}.{table} FOR EACH ROW EXECUTE FUNCTION "
            f"{OPS_SCHEMA}.enforce_remote_parse_legacy_secret_parent()"
        )
        op.execute(
            f"CREATE CONSTRAINT TRIGGER ck_{table}_parent_contract_deferred "
            f"AFTER INSERT OR UPDATE ON {OPS_SCHEMA}.{table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
            f"{OPS_SCHEMA}.enforce_remote_parse_legacy_secret_parent()"
        )

    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v4_secret_history()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE history_count bigint;
        DECLARE history_min bigint;
        DECLARE history_max bigint;
        DECLARE first_row {OPS_SCHEMA}.remote_parse_v4_secret%ROWTYPE;
        BEGIN
          SELECT count(*), min(encryption_revision), max(encryption_revision)
            INTO history_count, history_min, history_max
            FROM {OPS_SCHEMA}.remote_parse_v4_secret
           WHERE attempt_id=NEW.attempt_id;
          IF history_count<1 OR history_min<>1 OR history_max<>history_count THEN
            RAISE EXCEPTION 'remote parse v4 secret revisions are not contiguous';
          END IF;
          SELECT * INTO first_row FROM {OPS_SCHEMA}.remote_parse_v4_secret
           WHERE attempt_id=NEW.attempt_id ORDER BY encryption_revision LIMIT 1;
          IF first_row.fence_identity IS DISTINCT FROM NEW.fence_identity
             OR first_row.accepted_submission_sha256 IS DISTINCT FROM NEW.accepted_submission_sha256
             OR first_row.secret_kind IS DISTINCT FROM NEW.secret_kind
             OR first_row.provider_secret_version IS DISTINCT FROM NEW.provider_secret_version
             OR first_row.token_sha256 IS DISTINCT FROM NEW.token_sha256
             OR first_row.token_byte_count IS DISTINCT FROM NEW.token_byte_count
             OR first_row.data_nonce IS DISTINCT FROM NEW.data_nonce
             OR first_row.token_ciphertext IS DISTINCT FROM NEW.token_ciphertext
          THEN RAISE EXCEPTION 'remote parse v4 secret immutable data layer drifted'; END IF;
          IF NOT EXISTS (
            SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v4_evidence e
             WHERE e.attempt_id=NEW.attempt_id
               AND e.fence_identity=NEW.fence_identity
               AND e.evidence_kind='accepted_submission'
               AND e.evidence_sha256=NEW.accepted_submission_sha256
          ) THEN RAISE EXCEPTION 'remote parse v4 secret accepted evidence drifted'; END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        f"CREATE CONSTRAINT TRIGGER ck_remote_parse_v4_secret_history "
        f"AFTER INSERT ON {OPS_SCHEMA}.remote_parse_v4_secret "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
        f"{OPS_SCHEMA}.enforce_remote_parse_v4_secret_history()"
    )
    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.reject_remote_parse_v4_secret_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'remote parse v4 secret revision is immutable'; END $$
    """)
    op.execute(
        f"CREATE TRIGGER ck_remote_parse_v4_secret_immutable BEFORE UPDATE ON "
        f"{OPS_SCHEMA}.remote_parse_v4_secret FOR EACH ROW EXECUTE FUNCTION "
        f"{OPS_SCHEMA}.reject_remote_parse_v4_secret_update()"
    )
    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.enforce_remote_parse_v4_secret_delete()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE final_state text;
        DECLARE final_current boolean;
        DECLARE final_owner text;
        DECLARE final_lease timestamptz;
        BEGIN
          SELECT state, is_current, claim_owner_identity, claim_lease_until
            INTO final_state, final_current, final_owner, final_lease
            FROM {OPS_SCHEMA}.remote_parse_attempt
           WHERE attempt_id=OLD.attempt_id AND checkpoint_contract_version=4;
          IF NOT FOUND THEN RAISE EXCEPTION 'remote parse v4 secret delete lacks parent'; END IF;
          IF final_current IS DISTINCT FROM false
             OR final_state NOT IN ({_sql_values(_V4_FINAL_STATES)})
             OR final_owner IS NOT NULL OR final_lease IS NOT NULL
             OR EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v4_secret s WHERE s.attempt_id=OLD.attempt_id)
          THEN RAISE EXCEPTION 'remote parse v4 secret delete is not a complete final purge'; END IF;
          RETURN OLD;
        END $$
    """)
    op.execute(
        f"CREATE CONSTRAINT TRIGGER ck_remote_parse_v4_secret_delete "
        f"AFTER DELETE ON {OPS_SCHEMA}.remote_parse_v4_secret "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
        f"{OPS_SCHEMA}.enforce_remote_parse_v4_secret_delete()"
    )
    op.execute(f"""
        CREATE FUNCTION {OPS_SCHEMA}.purge_remote_parse_v4_secrets_final(
          p_attempt_id text,
          p_fence_identity text,
          p_lifecycle_version bigint,
          p_checkpoint_sha256 text,
          p_expected_revision_max bigint
        ) RETURNS bigint
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        DECLARE deleted_count bigint;
        DECLARE secret_count bigint;
        DECLARE secret_min bigint;
        DECLARE secret_max bigint;
        DECLARE final_row {OPS_SCHEMA}.remote_parse_attempt%ROWTYPE;
        BEGIN
          IF p_expected_revision_max IS NULL
             OR p_expected_revision_max<0
             OR p_expected_revision_max>{_MAX_INT}
          THEN
            RAISE EXCEPTION 'remote parse v4 expected secret revision is invalid';
          END IF;
          SELECT * INTO final_row FROM {OPS_SCHEMA}.remote_parse_attempt
           WHERE attempt_id=p_attempt_id FOR UPDATE;
          IF NOT FOUND
             OR final_row.checkpoint_contract_version<>4
             OR final_row.fence_identity IS DISTINCT FROM p_fence_identity
             OR final_row.row_version IS DISTINCT FROM p_lifecycle_version
             OR final_row.current_checkpoint_sha256 IS DISTINCT FROM p_checkpoint_sha256
             OR final_row.is_current IS DISTINCT FROM false
             OR final_row.state NOT IN ({_sql_values(_V4_FINAL_STATES)})
             OR final_row.claim_owner_identity IS NOT NULL
             OR final_row.claim_lease_until IS NOT NULL
          THEN RAISE EXCEPTION 'remote parse v4 secret purge lacks exact final head'; END IF;
          SELECT count(*), min(encryption_revision), max(encryption_revision)
            INTO secret_count, secret_min, secret_max
            FROM {OPS_SCHEMA}.remote_parse_v4_secret
           WHERE attempt_id=p_attempt_id;
          IF (p_expected_revision_max=0 AND secret_count<>0)
             OR (p_expected_revision_max>0
                 AND (secret_count<>p_expected_revision_max
                      OR secret_min<>1
                      OR secret_max<>p_expected_revision_max))
          THEN RAISE EXCEPTION 'remote parse v4 secret purge revision drifted'; END IF;
          DELETE FROM {OPS_SCHEMA}.remote_parse_v4_secret
           WHERE attempt_id=p_attempt_id;
          GET DIAGNOSTICS deleted_count=ROW_COUNT;
          IF deleted_count<>p_expected_revision_max THEN
            RAISE EXCEPTION 'remote parse v4 secret purge count drifted';
          END IF;
          RETURN deleted_count;
        END $$
    """)


def _upgrade_semantic_receipt_locator() -> None:
    bind = op.get_bind()
    invalid_locator_exists = bool(
        bind.execute(
            sa.text(
                f"""
                SELECT EXISTS (
                  SELECT 1 FROM {CORE_SCHEMA}.processing_run
                   WHERE (
                     (
                       semantic_route_receipts_relpath IS NULL
                       AND semantic_route_receipts_contract_version IS NULL
                     )
                     OR (
                       semantic_route_receipts_relpath IS NOT NULL
                       AND semantic_route_receipts_contract_version IS NOT NULL
                       AND semantic_route_receipts_contract_version IN (
                         'semantic_route_receipt.v2',
                         'semantic_route_receipt.v3'
                       )
                       AND semantic_route_receipts_hash IS NOT NULL
                     )
                   ) IS NOT TRUE
                )
                """
            )
        ).scalar_one()
    )
    if invalid_locator_exists:
        raise RuntimeError(
            "0057 refuses partial or unsupported semantic receipt locators"
        )
    with op.batch_alter_table("processing_run", schema=CORE_SCHEMA) as batch:
        batch.drop_constraint("ck_processing_run_semantic_receipt_hash", type_="check")
        batch.drop_constraint(
            "ck_processing_run_semantic_receipt_locator", type_="check"
        )
        batch.create_check_constraint(
            "ck_processing_run_semantic_receipt_hash",
            "semantic_route_receipts_hash IS NULL OR (semantic_route_receipts_hash ~ '^sha256:[0-9a-f]{64}$' AND document_units_relpath IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_processing_run_semantic_receipt_locator",
            "(semantic_route_receipts_relpath IS NULL AND semantic_route_receipts_contract_version IS NULL) OR (semantic_route_receipts_relpath IS NOT NULL AND semantic_route_receipts_contract_version IS NOT NULL AND semantic_route_receipts_contract_version IN ('semantic_route_receipt.v2','semantic_route_receipt.v3') AND semantic_route_receipts_hash IS NOT NULL)",
        )


def _grant_v4_access() -> None:
    for table in (
        "remote_parse_v4_checkpoint",
        "remote_parse_v4_evidence",
        "remote_parse_v4_secret",
        "atomic_publication_winner_v4",
    ):
        op.execute(
            f"REVOKE ALL ON TABLE {OPS_SCHEMA}.{table} FROM PUBLIC, "
            f"{READER_ROLE}, {FUTURE_L2_READER_ROLE}, {APP_ROLE}"
        )
        op.execute(f"GRANT SELECT, INSERT ON TABLE {OPS_SCHEMA}.{table} TO {APP_ROLE}")
    op.execute(
        f"REVOKE ALL ON FUNCTION {OPS_SCHEMA}.purge_remote_parse_v4_secrets_final(text,text,bigint,text,bigint) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {OPS_SCHEMA}.purge_remote_parse_v4_secrets_final(text,text,bigint,text,bigint) TO {APP_ROLE}"
    )


def upgrade() -> None:
    _upgrade_attempt_constraints()
    _create_v4_tables()
    _create_v4_foreign_keys()
    _create_v4_triggers()
    _upgrade_semantic_receipt_locator()
    _grant_v4_access()


def _restore_0056_attempt_constraints() -> None:
    with op.batch_alter_table("remote_parse_attempt", schema=OPS_SCHEMA) as batch:
        for name in (
            "ck_remote_parse_attempt_versions",
            "ck_remote_parse_attempt_contract_version",
            "ck_remote_parse_attempt_v3_credit_bounds",
            "ck_remote_parse_attempt_v3_final_zero",
            "ck_remote_parse_attempt_v3_materialization",
            "ck_remote_parse_attempt_v3_local_projection",
            "ck_remote_parse_attempt_v3_state_credit",
            "ck_remote_parse_attempt_state",
            "ck_remote_parse_attempt_lifecycle_shape",
            "ck_remote_parse_attempt_initial_shape",
            "ck_remote_parse_attempt_claim_shape",
            "ck_remote_parse_attempt_local_receipt",
            "ck_remote_parse_attempt_failure_receipt",
            "ck_remote_parse_attempt_submitted_shape",
            "ck_remote_parse_attempt_terminal_shape",
        ):
            batch.drop_constraint(name, type_="check")
        batch.drop_constraint(
            "uq_remote_parse_attempt_v4_parent_identity",
            type_="unique",
        )
        batch.drop_column("current_checkpoint_sha256")
        batch.create_check_constraint(
            "ck_remote_parse_attempt_versions",
            "attempt_generation >= 1 AND row_version >= 0",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_contract_version",
            _v3_contract_shape(include_v4=False),
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_credit_bounds",
            _v3_credit_bounds(old=True),
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_final_zero",
            "checkpoint_contract_version<3 OR state NOT IN ('acked','remote_failed','local_failed','pre_submission_failed','superseded') OR ("
            + _v3_zero()
            + ")",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_materialization",
            _v3_materialization_shape(old=True),
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_local_projection",
            "checkpoint_contract_version<3 OR ((local_receipt_bytes IS NULL AND local_db_staged_byte_count IS NULL) OR (local_receipt_bytes IS NOT NULL AND local_db_staged_byte_count IS NOT NULL AND local_db_staged_byte_count>0))",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_v3_state_credit",
            _v3_state_credit(old=True),
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_state",
            f"state IN ({_sql_values(_LEGACY_STATES)})",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_lifecycle_shape",
            "(state IN ('prepared','reconciling','submitted','remote_terminal','materializing','local_materialized','finish_committed','remote_failure_committed','local_failure_committed') AND is_current) OR (state IN ('acked','remote_failed','local_failed','pre_submission_failed','superseded') AND NOT is_current)",
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_initial_shape",
            _legacy_initial_shape(),
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_claim_shape",
            _legacy_claim_shape(),
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_local_receipt",
            _legacy_local_receipt(),
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_failure_receipt",
            _legacy_failure_receipt(),
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_submitted_shape",
            _legacy_submitted_shape(),
        )
        batch.create_check_constraint(
            "ck_remote_parse_attempt_terminal_shape",
            f"(checkpoint_contract_version=1 AND ({_V1_TERMINAL})) OR (checkpoint_contract_version=2 AND ({_V2_TERMINAL})) OR (checkpoint_contract_version=3 AND ({_V3_TERMINAL}))",
        )


def downgrade() -> None:
    bind = op.get_bind()
    guards = (
        f"SELECT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_attempt WHERE checkpoint_contract_version=4 OR current_checkpoint_sha256 IS NOT NULL)",
        f"SELECT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v4_checkpoint)",
        f"SELECT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v4_evidence)",
        f"SELECT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.remote_parse_v4_secret)",
        f"SELECT EXISTS (SELECT 1 FROM {OPS_SCHEMA}.atomic_publication_winner_v4)",
        f"SELECT EXISTS (SELECT 1 FROM {CORE_SCHEMA}.processing_run WHERE semantic_route_receipts_contract_version='semantic_route_receipt.v3')",
    )
    if any(bool(bind.execute(sa.text(query)).scalar_one()) for query in guards):
        raise RuntimeError("0057 downgrade would destroy v4 staged evidence")

    op.execute(
        f"DROP FUNCTION {OPS_SCHEMA}.purge_remote_parse_v4_secrets_final(text,text,bigint,text,bigint)"
    )
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_secret_lifecycle_row ON {OPS_SCHEMA}.remote_parse_v4_secret"
    )
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_secret_lifecycle_head ON {OPS_SCHEMA}.remote_parse_attempt"
    )
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_secret_delete ON {OPS_SCHEMA}.remote_parse_v4_secret"
    )
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_secret_immutable ON {OPS_SCHEMA}.remote_parse_v4_secret"
    )
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_secret_history ON {OPS_SCHEMA}.remote_parse_v4_secret"
    )
    for table in ("remote_parse_resume_secret", "remote_parse_v3_resume_secret"):
        op.execute(
            f"DROP TRIGGER ck_{table}_parent_contract_deferred ON {OPS_SCHEMA}.{table}"
        )
        op.execute(f"DROP TRIGGER ck_{table}_parent_contract ON {OPS_SCHEMA}.{table}")
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_head_retain ON {OPS_SCHEMA}.remote_parse_attempt"
    )
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_head ON {OPS_SCHEMA}.remote_parse_attempt"
    )
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_head_identity ON {OPS_SCHEMA}.remote_parse_attempt"
    )
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_head_initial_insert ON {OPS_SCHEMA}.remote_parse_attempt"
    )
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_checkpoint_references ON {OPS_SCHEMA}.remote_parse_v4_checkpoint"
    )
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_evidence_referenced ON {OPS_SCHEMA}.remote_parse_v4_evidence"
    )
    op.execute(
        f"DROP TRIGGER ck_atomic_publication_winner_v4_referenced ON {OPS_SCHEMA}.atomic_publication_winner_v4"
    )
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_checkpoint_chain ON {OPS_SCHEMA}.remote_parse_v4_checkpoint"
    )
    for table in (
        "remote_parse_v4_evidence",
        "remote_parse_v4_checkpoint",
        "atomic_publication_winner_v4",
    ):
        op.execute(f"DROP TRIGGER ck_{table}_v4_parent ON {OPS_SCHEMA}.{table}")
        op.execute(f"DROP TRIGGER ck_{table}_immutable ON {OPS_SCHEMA}.{table}")
    op.execute(
        f"DROP TRIGGER ck_remote_parse_v4_secret_v4_parent ON {OPS_SCHEMA}.remote_parse_v4_secret"
    )
    for function_name in (
        "enforce_remote_parse_v4_secret_lifecycle",
        "enforce_remote_parse_v4_secret_delete",
        "reject_remote_parse_v4_secret_update",
        "enforce_remote_parse_v4_secret_history",
        "enforce_remote_parse_legacy_secret_parent",
        "reject_remote_parse_v4_head_delete",
        "enforce_remote_parse_v4_head",
        "enforce_atomic_publication_winner_v4_referenced",
        "enforce_remote_parse_v4_evidence_referenced",
        "enforce_remote_parse_v4_checkpoint_references",
        "enforce_remote_parse_v4_checkpoint_chain",
        "enforce_remote_parse_v4_child_parent",
        "enforce_remote_parse_v4_head_initial_insert",
        "reject_remote_parse_v4_head_identity_change",
        "reject_remote_parse_v4_immutable_change",
    ):
        op.execute(f"DROP FUNCTION {OPS_SCHEMA}.{function_name}()")

    with op.batch_alter_table("processing_run", schema=CORE_SCHEMA) as batch:
        batch.drop_constraint("ck_processing_run_semantic_receipt_hash", type_="check")
        batch.drop_constraint(
            "ck_processing_run_semantic_receipt_locator", type_="check"
        )
        batch.create_check_constraint(
            "ck_processing_run_semantic_receipt_hash",
            "semantic_route_receipts_hash IS NULL OR (semantic_route_receipts_hash ~ '^sha256:[0-9a-f]{64}$' AND document_units_relpath IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_processing_run_semantic_receipt_locator",
            "(semantic_route_receipts_relpath IS NULL AND semantic_route_receipts_contract_version IS NULL) OR (semantic_route_receipts_relpath IS NOT NULL AND semantic_route_receipts_contract_version = 'semantic_route_receipt.v2' AND semantic_route_receipts_hash IS NOT NULL)",
        )

    op.drop_constraint(
        "fk_remote_parse_attempt_v4_current_checkpoint",
        "remote_parse_attempt",
        schema=OPS_SCHEMA,
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_remote_parse_v4_checkpoint_publication_winner",
        "remote_parse_v4_checkpoint",
        schema=OPS_SCHEMA,
        type_="foreignkey",
    )
    op.drop_table("remote_parse_v4_secret", schema=OPS_SCHEMA)
    op.drop_table("atomic_publication_winner_v4", schema=OPS_SCHEMA)
    op.drop_table("remote_parse_v4_checkpoint", schema=OPS_SCHEMA)
    op.drop_table("remote_parse_v4_evidence", schema=OPS_SCHEMA)
    _restore_0056_attempt_constraints()
