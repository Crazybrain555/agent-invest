"""Private durable checkpoint contract for staged whole-document parsing."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Literal

from disclosure_anchor.application.contracts.staged_credit import (
    CreditShapeFacts,
    CreditVector,
    credit_shape,
    decode_reservation_input,
)


AttemptState = Literal[
    "prepared",
    "reconciling",
    "submitted",
    "remote_terminal",
    "materializing",
    "local_materialized",
    "finish_committed",
    "remote_failure_committed",
    "local_failure_committed",
    "pre_submission_failed",
    "acked",
    "remote_failed",
    "local_failed",
    "superseded",
]

NONFINAL_STATES = frozenset(
    {"prepared", "reconciling", "submitted", "remote_terminal", "materializing", "local_materialized", "finish_committed", "remote_failure_committed", "local_failure_committed"}
)
FINAL_STATES = frozenset(
    {"acked", "remote_failed", "local_failed", "pre_submission_failed", "superseded"}
)
ALLOWED_TRANSITIONS = frozenset(
    {
        ("prepared", "reconciling"),
        ("remote_terminal", "materializing"),
        ("finish_committed", "acked"),
        ("remote_failure_committed", "remote_failed"),
        ("local_failure_committed", "local_failed"),
    }
)
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_EVIDENCE_INT = (1 << 63) - 1
_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "attempt_identity",
        "fence_identity",
        "source_pdf_sha256",
        "artifact_owner_identity",
        "artifact_byte_count",
        "artifact_sha256",
        "resume_token_sha256",
    }
)


class RemoteParseCheckpointConflict(RuntimeError):
    """A stale fence/version or conflicting terminal observation lost CAS."""


@dataclass(frozen=True, slots=True)
class TerminalReceipt:
    attempt_identity: str
    fence_identity: str
    source_pdf_sha256: str
    artifact_owner_identity: str
    artifact_byte_count: int
    artifact_sha256: str
    resume_token_sha256: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.attempt_identity, "attempt identity"),
            (self.fence_identity, "fence identity"),
            (self.artifact_owner_identity, "artifact owner identity"),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ValueError(f"invalid {label}")
        for value, label in (
            (self.source_pdf_sha256, "source PDF SHA"),
            (self.artifact_sha256, "artifact SHA"),
            (self.resume_token_sha256, "resume token SHA"),
        ):
            if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                raise ValueError(f"invalid canonical {label}")
        if isinstance(self.artifact_byte_count, bool) or not isinstance(
            self.artifact_byte_count, int
        ) or self.artifact_byte_count < 1:
            raise ValueError("artifact byte count must be a positive exact integer")


@dataclass(frozen=True, slots=True)
class EncodedTerminalReceipt:
    receipt: TerminalReceipt
    exact_bytes: bytes = field(repr=False)
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.exact_bytes, bytes):
            raise ValueError("terminal receipt exact bytes must be bytes")
        if not self.exact_bytes or len(self.exact_bytes) > 65536:
            raise ValueError("terminal receipt bytes are outside the closed envelope")
        if isinstance(self.byte_count, bool) or self.byte_count != len(self.exact_bytes):
            raise ValueError("terminal receipt byte count differs from exact bytes")
        expected_sha = "sha256:" + hashlib.sha256(self.exact_bytes).hexdigest()
        if self.sha256 != expected_sha:
            raise ValueError("terminal receipt SHA differs from exact bytes")
        if self.exact_bytes != _canonical_terminal_receipt_bytes(self.receipt):
            raise ValueError("terminal receipt projection differs from exact bytes")


@dataclass(frozen=True, slots=True)
class LocalMaterializationReceipt:
    attempt_identity: str
    fence_identity: str
    claim_generation: int
    source_pdf_sha256: str
    parser_target_sha256: str
    terminal_receipt_sha256: str
    artifact_owner_identity: str
    artifact_sha256: str
    artifact_byte_count: int
    output_manifest_sha256: str
    output_manifest_relpath: str
    output_manifest_byte_count: int
    artifact_root_relpath: str
    provider_envelope_relpath: str
    provider_envelope_sha256: str
    provider_envelope_byte_count: int
    compressed_byte_count: int
    uncompressed_byte_count: int
    member_count: int
    disk_byte_count: int
    decoded_byte_count: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.attempt_identity, "attempt identity"),
            (self.fence_identity, "fence identity"),
            (self.artifact_owner_identity, "artifact owner identity"),
            (self.output_manifest_relpath, "output manifest relpath"),
            (self.artifact_root_relpath, "artifact root relpath"),
            (self.provider_envelope_relpath, "provider envelope relpath"),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ValueError(f"invalid local receipt {label}")
            if label.endswith("relpath") and (value.startswith("/") or ".." in value.split("/")):
                raise ValueError(f"invalid local receipt {label}")
        for value in (
            self.source_pdf_sha256, self.parser_target_sha256,
            self.terminal_receipt_sha256, self.artifact_sha256,
            self.output_manifest_sha256, self.provider_envelope_sha256,
        ):
            if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                raise ValueError("local receipt hash is not canonical")
        for numeric_value, label, minimum in (
            (self.claim_generation, "claim generation", 1),
            (self.artifact_byte_count, "artifact bytes", 1),
            (self.compressed_byte_count, "compressed bytes", 1),
            (self.uncompressed_byte_count, "uncompressed bytes", 0),
            (self.member_count, "member count", 1),
            (self.output_manifest_byte_count, "output manifest bytes", 1),
            (self.provider_envelope_byte_count, "provider envelope bytes", 1),
            (self.disk_byte_count, "disk bytes", 0),
            (self.decoded_byte_count, "decoded bytes", 0),
        ):
            if (
                isinstance(numeric_value, bool)
                or not isinstance(numeric_value, int)
                or numeric_value < minimum
            ):
                raise ValueError(f"local receipt {label} is invalid")
        if self.compressed_byte_count != self.artifact_byte_count:
            raise ValueError("local receipt compressed/artifact byte counts differ")


@dataclass(frozen=True, slots=True)
class PreparedMaterializationReceiptV2:
    attempt_identity: str
    fence_identity: str
    source_pdf_sha256: str
    source_page_count: int
    terminal_receipt_sha256: str
    process_profile_sha256: str
    credit_policy_sha256: str
    reservation_input_sha256: str
    spool_relpath: str
    spool_sha256: str
    spool_byte_count: int
    compressed_byte_count: int
    uncompressed_byte_count: int
    member_count: int
    temporary_disk_byte_count: int
    decoded_byte_count: int
    private_token_sha256: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.attempt_identity, "attempt identity"),
            (self.fence_identity, "fence identity"),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"invalid prepared materialization {label}")
        _validate_safe_relpath(self.spool_relpath, "prepared spool relpath")
        for value in (
            self.source_pdf_sha256,
            self.terminal_receipt_sha256,
            self.process_profile_sha256,
            self.credit_policy_sha256,
            self.reservation_input_sha256,
            self.spool_sha256,
            self.private_token_sha256,
        ):
            if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                raise ValueError("prepared materialization hash is not canonical")
        for numeric_value, label, minimum in (
            (self.source_page_count, "source pages", 1),
            (self.spool_byte_count, "spool bytes", 1),
            (self.compressed_byte_count, "compressed bytes", 1),
            (self.uncompressed_byte_count, "uncompressed bytes", 1),
            (self.member_count, "member count", 1),
            (self.temporary_disk_byte_count, "temporary disk bytes", 1),
            (self.decoded_byte_count, "decoded bytes", 1),
        ):
            if (
                isinstance(numeric_value, bool)
                or not isinstance(numeric_value, int)
                or not minimum <= numeric_value <= _MAX_EVIDENCE_INT
            ):
                raise ValueError(f"prepared materialization {label} is invalid")
        if self.spool_byte_count != self.compressed_byte_count:
            raise ValueError("prepared materialization spool/compressed bytes differ")
        if self.temporary_disk_byte_count != _checked_temporary_disk_peak(
            self.spool_byte_count, self.uncompressed_byte_count
        ):
            raise ValueError("prepared materialization temporary disk peak drifted")


@dataclass(frozen=True, slots=True)
class LocalMaterializationReceiptV2:
    attempt_identity: str
    fence_identity: str
    claim_generation: int
    source_pdf_sha256: str
    source_page_count: int
    parser_target_sha256: str
    terminal_receipt_sha256: str
    process_profile_sha256: str
    credit_policy_sha256: str
    reservation_input_sha256: str
    prepared_materialization_sha256: str
    artifact_owner_identity: str
    artifact_sha256: str
    artifact_byte_count: int
    output_manifest_sha256: str
    output_manifest_relpath: str
    output_manifest_byte_count: int
    artifact_root_relpath: str
    provider_envelope_relpath: str
    provider_envelope_sha256: str
    provider_envelope_byte_count: int
    compressed_byte_count: int
    uncompressed_byte_count: int
    member_count: int
    temporary_disk_byte_count: int
    decoded_byte_count: int
    db_staged_byte_count: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.attempt_identity, "attempt identity"),
            (self.fence_identity, "fence identity"),
            (self.artifact_owner_identity, "artifact owner identity"),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ValueError(f"invalid v2 local receipt {label}")
        for value, label in (
            (self.output_manifest_relpath, "output manifest relpath"),
            (self.artifact_root_relpath, "artifact root relpath"),
            (self.provider_envelope_relpath, "provider envelope relpath"),
        ):
            _validate_safe_relpath(value, label)
        for value in (
            self.source_pdf_sha256,
            self.parser_target_sha256,
            self.terminal_receipt_sha256,
            self.process_profile_sha256,
            self.credit_policy_sha256,
            self.reservation_input_sha256,
            self.prepared_materialization_sha256,
            self.artifact_sha256,
            self.output_manifest_sha256,
            self.provider_envelope_sha256,
        ):
            if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                raise ValueError("v2 local receipt hash is not canonical")
        for numeric_value, label, minimum in (
            (self.claim_generation, "claim generation", 1),
            (self.source_page_count, "source pages", 1),
            (self.artifact_byte_count, "artifact bytes", 1),
            (self.output_manifest_byte_count, "manifest bytes", 1),
            (self.provider_envelope_byte_count, "provider envelope bytes", 1),
            (self.compressed_byte_count, "compressed bytes", 1),
            (self.uncompressed_byte_count, "uncompressed bytes", 1),
            (self.member_count, "member count", 1),
            (self.temporary_disk_byte_count, "temporary disk bytes", 1),
            (self.decoded_byte_count, "decoded bytes", 1),
            (self.db_staged_byte_count, "DB staged bytes", 1),
        ):
            if (
                isinstance(numeric_value, bool)
                or not isinstance(numeric_value, int)
                or not minimum <= numeric_value <= _MAX_EVIDENCE_INT
            ):
                raise ValueError(f"v2 local receipt {label} is invalid")
        if self.compressed_byte_count != self.artifact_byte_count:
            raise ValueError("v2 local receipt compressed/artifact bytes differ")
        if self.temporary_disk_byte_count != _checked_temporary_disk_peak(
            self.compressed_byte_count, self.uncompressed_byte_count
        ):
            raise ValueError("v2 local receipt temporary disk peak drifted")


@dataclass(frozen=True, slots=True)
class PreparedReconcileReceipt:
    attempt_identity: str
    fence_identity: str
    source_pdf_sha256: str
    client_submit_key: str
    submission_epoch_unix: int
    parser_target_sha256: str
    request_sha256: str
    runtime_epoch_sha256: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.attempt_identity, "attempt identity"),
            (self.fence_identity, "fence identity"),
            (self.client_submit_key, "client submit key"),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"invalid prepared reconcile {label}")
        for value in (
            self.source_pdf_sha256, self.parser_target_sha256,
            self.request_sha256, self.runtime_epoch_sha256,
        ):
            if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                raise ValueError("prepared reconcile hash is not canonical")
        if (
            isinstance(self.submission_epoch_unix, bool)
            or not isinstance(self.submission_epoch_unix, int)
            or self.submission_epoch_unix < 0
        ):
            raise ValueError("prepared reconcile epoch is invalid")


@dataclass(frozen=True, slots=True)
class AcceptedSubmissionReceipt:
    attempt_identity: str
    fence_identity: str
    source_pdf_sha256: str
    client_submit_key: str
    submission_epoch_unix: int
    remote_task_identity: str
    status_url: str
    result_url: str
    resume_token_sha256: str

    def __post_init__(self) -> None:
        for value, label, limit in (
            (self.attempt_identity, "attempt identity", 128),
            (self.fence_identity, "fence identity", 128),
            (self.client_submit_key, "client submit key", 128),
            (self.remote_task_identity, "remote task identity", 1024),
            (self.status_url, "status URL", 4096),
            (self.result_url, "result URL", 4096),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError(f"invalid accepted submission {label}")
        if _SHA.fullmatch(self.source_pdf_sha256) is None:
            raise ValueError("accepted submission source hash is not canonical")
        if _SHA.fullmatch(self.resume_token_sha256) is None:
            raise ValueError("accepted submission token hash is not canonical")
        if (
            isinstance(self.submission_epoch_unix, bool)
            or not isinstance(self.submission_epoch_unix, int)
            or self.submission_epoch_unix < 0
        ):
            raise ValueError("accepted submission epoch is invalid")


@dataclass(frozen=True, slots=True)
class FailureReceipt:
    """Closed failure evidence.

    ``pre_submission`` is intentionally narrower than an HTTP rejection: it
    is valid only when the application proves no remote lookup or POST was
    invoked for this attempt.  Once any remote IO begins, every uncertain
    outcome remains recoverable and may not use this final shape.
    """
    attempt_identity: str
    fence_identity: str
    stage: Literal["remote", "local"]
    accepted: bool
    ack_required: bool
    submission_was_attempted: bool
    remote_task_identity: str | None
    claim_generation: int
    terminal_receipt_sha256: str | None
    error_code: str
    error_stage: str
    error_class: Literal["pre_submission", "remote_terminal", "local_materialization"]
    retryable: bool
    retry_budget_class: Literal["item", "infrastructure", "neutral"]
    message: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.attempt_identity, "attempt identity"),
            (self.fence_identity, "fence identity"),
            (self.error_code, "error code"),
            (self.error_stage, "error stage"),
            (self.message, "message"),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > (
                4096 if label == "message" else 128
            ):
                raise ValueError(f"invalid failure receipt {label}")
        if (
            type(self.accepted) is not bool
            or type(self.ack_required) is not bool
            or type(self.submission_was_attempted) is not bool
        ):
            raise ValueError("failure receipt acceptance flags must be exact booleans")
        if type(self.retryable) is not bool or self.retry_budget_class not in {
            "item", "infrastructure", "neutral"
        }:
            raise ValueError("failure receipt retry contract is invalid")
        if (
            isinstance(self.claim_generation, bool)
            or not isinstance(self.claim_generation, int)
            or self.claim_generation < 1
        ):
            raise ValueError("failure receipt claim generation is invalid")
        if self.remote_task_identity is not None and (
            not self.remote_task_identity.strip() or len(self.remote_task_identity) > 1024
        ):
            raise ValueError("failure receipt remote task identity is invalid")
        valid_shape = (
            self.stage == "remote"
            and self.error_class == "pre_submission"
            and not self.accepted
            and not self.ack_required
            and not self.submission_was_attempted
            and self.remote_task_identity is None
            and self.terminal_receipt_sha256 is None
        ) or (
            self.stage == "remote"
            and self.error_class == "remote_terminal"
            and self.accepted
            and self.ack_required
            and self.submission_was_attempted
            and self.remote_task_identity is not None
            and self.terminal_receipt_sha256 is None
        ) or (
            self.stage == "local"
            and self.error_class == "local_materialization"
            and self.accepted
            and self.ack_required
            and self.submission_was_attempted
            and self.remote_task_identity is not None
            and isinstance(self.terminal_receipt_sha256, str)
            and _SHA.fullmatch(self.terminal_receipt_sha256) is not None
        )
        if not valid_shape:
            raise ValueError("failure receipt stage/class/acceptance shape is invalid")
        if self.accepted != self.ack_required:
            raise ValueError("accepted remote failure must require ACK")
        if self.accepted != (self.remote_task_identity is not None):
            raise ValueError("failure receipt remote task disagrees with acceptance")


@dataclass(frozen=True, slots=True)
class EncodedCheckpointReceipt:
    receipt: (
        PreparedReconcileReceipt | AcceptedSubmissionReceipt
        | PreparedMaterializationReceiptV2 | LocalMaterializationReceipt
        | LocalMaterializationReceiptV2 | FailureReceipt
    )
    exact_bytes: bytes = field(repr=False)
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        if type(self.exact_bytes) is not bytes or not self.exact_bytes or len(self.exact_bytes) > 65536:
            raise ValueError("checkpoint receipt bytes are outside the closed envelope")
        if isinstance(self.byte_count, bool) or self.byte_count != len(self.exact_bytes):
            raise ValueError("checkpoint receipt byte count differs from exact bytes")
        if self.sha256 != "sha256:" + hashlib.sha256(self.exact_bytes).hexdigest():
            raise ValueError("checkpoint receipt SHA differs from exact bytes")
        if self.exact_bytes != _canonical_checkpoint_receipt_bytes(self.receipt):
            raise ValueError("checkpoint receipt projection differs from exact bytes")


@dataclass(frozen=True, slots=True)
class RemoteParseAttempt:
    attempt_id: str
    processing_run_id: str
    document_id: str
    attempt_generation: int
    fence_identity: str
    source_pdf_sha256: str
    parser_target_sha256: str
    request_sha256: str
    runtime_epoch_sha256: str
    client_submit_key: str
    checkpoint_contract_version: int = 2
    state: AttemptState = "prepared"
    is_current: bool = True
    row_version: int = 0
    remote_task_identity: str | None = None
    submitted_receipt_sha256: str | None = None
    submitted_receipt_bytes: bytes | None = field(default=None, repr=False)
    submitted_receipt_byte_count: int | None = None
    terminal_receipt_sha256: str | None = None
    terminal_receipt_bytes: bytes | None = field(default=None, repr=False)
    terminal_receipt_byte_count: int | None = None
    result_owner_identity: str | None = None
    result_artifact_sha256: str | None = None
    result_artifact_bytes: int | None = None
    claim_generation: int = 0
    claim_owner_identity: str | None = None
    claim_lease_until: datetime | None = None
    local_receipt_sha256: str | None = None
    local_receipt_bytes: bytes | None = field(default=None, repr=False)
    local_receipt_byte_count: int | None = None
    failure_receipt_sha256: str | None = None
    failure_receipt_bytes: bytes | None = field(default=None, repr=False)
    failure_receipt_byte_count: int | None = None
    failure_stage: Literal["remote", "local"] | None = None
    process_profile_sha256: str | None = None
    credit_policy_sha256: str | None = None
    reservation_input_bytes: bytes | None = field(default=None, repr=False)
    reservation_input_sha256: str | None = None
    reservation_input_byte_count: int | None = None
    reservation_source_byte_count: int | None = None
    reservation_source_page_count: int | None = None
    reservation_bucket: str | None = None
    reservation: CreditVector | None = None
    current_credits: CreditVector | None = None
    materialization_receipt_bytes: bytes | None = field(default=None, repr=False)
    materialization_receipt_sha256: str | None = None
    materialization_receipt_byte_count: int | None = None
    local_db_staged_byte_count: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.attempt_id, "attempt id"),
            (self.processing_run_id, "processing run id"),
            (self.document_id, "document id"),
            (self.fence_identity, "fence identity"),
            (self.client_submit_key, "client submit key"),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"invalid {label}")
        for value in (
            self.source_pdf_sha256,
            self.parser_target_sha256,
            self.request_sha256,
            self.runtime_epoch_sha256,
        ):
            if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                raise ValueError("remote parse attempt hash is not canonical")
        if isinstance(self.attempt_generation, bool) or self.attempt_generation < 1:
            raise ValueError("attempt generation must be a positive exact integer")
        if self.checkpoint_contract_version not in {1, 2, 3} or isinstance(
            self.checkpoint_contract_version, bool
        ):
            raise ValueError("checkpoint contract version is unsupported")
        if isinstance(self.row_version, bool) or self.row_version < 0:
            raise ValueError("row version must be a non-negative exact integer")
        if isinstance(self.claim_generation, bool) or self.claim_generation < 0:
            raise ValueError("claim generation must be a non-negative exact integer")
        if (self.claim_owner_identity is None) != (self.claim_lease_until is None):
            raise ValueError("claim owner and lease must be present together")
        if self.claim_lease_until is not None and self.claim_lease_until.tzinfo is None:
            raise ValueError("claim lease must be timezone aware")
        self._validate_receipt_shape(
            "submitted", self.submitted_receipt_sha256, self.submitted_receipt_bytes,
            self.submitted_receipt_byte_count,
        )
        self._validate_receipt_shape(
            "local", self.local_receipt_sha256, self.local_receipt_bytes,
            self.local_receipt_byte_count,
        )
        self._validate_receipt_shape(
            "failure", self.failure_receipt_sha256, self.failure_receipt_bytes,
            self.failure_receipt_byte_count,
        )
        if self.checkpoint_contract_version < 3:
            if any(
                value is not None
                for value in (
                    self.process_profile_sha256,
                    self.credit_policy_sha256,
                    self.reservation_input_bytes,
                    self.reservation_input_sha256,
                    self.reservation_input_byte_count,
                    self.reservation_source_byte_count,
                    self.reservation_source_page_count,
                    self.reservation_bucket,
                    self.reservation,
                    self.current_credits,
                    self.materialization_receipt_bytes,
                    self.materialization_receipt_sha256,
                    self.materialization_receipt_byte_count,
                    self.local_db_staged_byte_count,
                )
            ):
                raise ValueError("v1/v2 checkpoint cannot contain v3 evidence")
        if self.checkpoint_contract_version == 1:
            if any(
                value is not None
                for value in (
                    self.claim_owner_identity, self.claim_lease_until,
                    self.submitted_receipt_bytes, self.local_receipt_bytes,
                    self.failure_receipt_bytes,
                    self.failure_stage,
                )
            ) or self.claim_generation != 0:
                raise ValueError("v1 checkpoint cannot contain v2 recovery evidence")
            return
        if self.checkpoint_contract_version == 3:
            if not all(
                isinstance(value, str) and _SHA.fullmatch(value)
                for value in (self.process_profile_sha256, self.credit_policy_sha256)
            ) or type(self.reservation_input_bytes) is not bytes:
                raise ValueError("v3 checkpoint lacks closed profile/reservation evidence")
            encoded_input = decode_reservation_input(self.reservation_input_bytes)
            if (
                encoded_input.sha256 != self.reservation_input_sha256
                or encoded_input.byte_count != self.reservation_input_byte_count
                or encoded_input.value.process_profile_sha256 != self.process_profile_sha256
                or encoded_input.value.credit_policy_sha256 != self.credit_policy_sha256
                or encoded_input.value.source_pdf_sha256 != self.source_pdf_sha256
                or encoded_input.value.source_byte_count
                != self.reservation_source_byte_count
                or encoded_input.value.source_page_count
                != self.reservation_source_page_count
                or encoded_input.value.bucket != self.reservation_bucket
                or encoded_input.value.reservation != self.reservation
                or type(self.current_credits) is not CreditVector
                or not self.current_credits.fits(encoded_input.value.reservation)
            ):
                raise ValueError("v3 reservation evidence drifted from attempt")
            if self.materialization_receipt_bytes is not None:
                encoded_materialization = decode_checkpoint_receipt(
                    self.materialization_receipt_bytes
                )
                prepared_projection = encoded_materialization.receipt
                if not isinstance(prepared_projection, PreparedMaterializationReceiptV2) or (
                    encoded_materialization.sha256 != self.materialization_receipt_sha256
                    or encoded_materialization.byte_count
                    != self.materialization_receipt_byte_count
                    or prepared_projection.attempt_identity != self.attempt_id
                    or prepared_projection.fence_identity != self.fence_identity
                    or prepared_projection.source_pdf_sha256 != self.source_pdf_sha256
                    or prepared_projection.process_profile_sha256
                    != self.process_profile_sha256
                    or prepared_projection.credit_policy_sha256 != self.credit_policy_sha256
                    or prepared_projection.reservation_input_sha256
                    != self.reservation_input_sha256
                ):
                    raise ValueError("v3 materialization evidence drifted from attempt")
            decoded_materialization = (
                None
                if self.materialization_receipt_bytes is None
                else decode_checkpoint_receipt(self.materialization_receipt_bytes).receipt
            )
            local_v2 = (
                None
                if self.local_receipt_bytes is None
                else decode_checkpoint_receipt(self.local_receipt_bytes).receipt
            )
            if local_v2 is not None and not isinstance(
                local_v2, LocalMaterializationReceiptV2
            ):
                raise ValueError("v3 local receipt must use the v2 credit contract")
            materialization = (
                decoded_materialization
                if isinstance(decoded_materialization, PreparedMaterializationReceiptV2)
                else None
            )
            materialization_required = self.state in {
                "materializing", "local_materialized", "finish_committed", "acked"
            }
            if materialization_required != (materialization is not None):
                raise ValueError(
                    "materialization receipt presence disagrees with v3 state"
                )
            if self.state in {"local_failure_committed", "local_failed"}:
                if local_v2 is not None and materialization is None:
                    raise ValueError("completed local failure lacks prepared evidence")
            elif self.state not in {"local_materialized", "finish_committed", "acked"}:
                if local_v2 is not None:
                    raise ValueError("v3 local receipt is stale for attempt state")
            facts = CreditShapeFacts(
                terminal_byte_count=self.result_artifact_bytes or 0,
                compressed_byte_count=(
                    materialization.compressed_byte_count
                    if isinstance(materialization, PreparedMaterializationReceiptV2)
                    else 0
                ),
                uncompressed_byte_count=(
                    materialization.uncompressed_byte_count
                    if isinstance(materialization, PreparedMaterializationReceiptV2)
                    else 0
                ),
                decoded_byte_count=(
                    materialization.decoded_byte_count
                    if isinstance(materialization, PreparedMaterializationReceiptV2)
                    else 0
                ),
                temporary_disk_byte_count=(
                    materialization.temporary_disk_byte_count
                    if isinstance(materialization, PreparedMaterializationReceiptV2)
                    else 0
                ),
                db_staged_byte_count=self.local_db_staged_byte_count or 0,
                source_page_count=(
                    self.reservation_source_page_count or 0
                    if materialization is not None
                    else 0
                ),
                materialization_prepared=materialization is not None,
                local_materialization_completed=local_v2 is not None,
            )
            if credit_shape(self.state, facts) != self.current_credits:
                raise ValueError("v3 current credit projection drifted from state evidence")
        submitted_required = self.state not in {
            "prepared", "reconciling", "pre_submission_failed", "superseded"
        }
        if submitted_required != (self.submitted_receipt_bytes is not None):
            raise ValueError("submitted receipt presence disagrees with attempt state")
        local_required = self.state in {"local_materialized", "finish_committed", "acked"}
        if local_required != (self.local_receipt_bytes is not None):
            raise ValueError("local receipt presence disagrees with attempt state")
        failure_required = self.state in {
            "remote_failure_committed", "local_failure_committed",
            "remote_failed", "local_failed", "pre_submission_failed",
        }
        if failure_required != (self.failure_receipt_bytes is not None):
            raise ValueError("failure receipt presence disagrees with attempt state")
        expected_stage = (
            "remote" if self.state in {
                "remote_failure_committed", "remote_failed", "pre_submission_failed"
            }
            else "local" if self.state in {"local_failure_committed", "local_failed"}
            else None
        )
        if self.failure_stage != expected_stage:
            raise ValueError("failure stage disagrees with attempt state")

    @staticmethod
    def _validate_receipt_shape(
        label: str, sha256: str | None, exact_bytes: bytes | None, byte_count: int | None,
    ) -> None:
        absent = sha256 is None and exact_bytes is None and byte_count is None
        if absent:
            return
        if (
            not isinstance(sha256, str)
            or _SHA.fullmatch(sha256) is None
            or type(exact_bytes) is not bytes
            or not exact_bytes
            or len(exact_bytes) > 65536
            or isinstance(byte_count, bool)
            or byte_count != len(exact_bytes)
            or sha256 != "sha256:" + hashlib.sha256(exact_bytes).hexdigest()
        ):
            raise ValueError(f"{label} receipt identity differs from exact bytes")


@dataclass(frozen=True, slots=True)
class RemoteParseResumeSecret:
    attempt_id: str
    secret_kind: Literal[
        "submission", "prepared_reconcile", "accepted_submission", "terminal", "ack",
        "materialization",
    ]
    token_bytes: bytes = field(repr=False)
    token_sha256: str
    token_byte_count: int
    secret_contract_version: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id.strip():
            raise ValueError("invalid resume token attempt id")
        if self.secret_contract_version not in {1, 2, 3} or isinstance(
            self.secret_contract_version, bool
        ):
            raise ValueError("invalid resume token contract version")
        allowed = {
            1: {"submission", "terminal", "ack"},
            2: {"prepared_reconcile", "accepted_submission", "terminal"},
            3: {
                "prepared_reconcile", "accepted_submission", "terminal",
                "materialization",
            },
        }[self.secret_contract_version]
        if self.secret_kind not in allowed:
            raise ValueError("invalid resume token secret kind")
        if type(self.token_bytes) is not bytes or not self.token_bytes or len(self.token_bytes) > 65536:
            raise ValueError("resume token bytes are outside the private envelope")
        expected = "sha256:" + hashlib.sha256(self.token_bytes).hexdigest()
        if (
            self.token_sha256 != expected
            or isinstance(self.token_byte_count, bool)
            or not isinstance(self.token_byte_count, int)
            or self.token_byte_count != len(self.token_bytes)
        ):
            raise ValueError("resume token identity differs from exact bytes")


def _canonical_terminal_receipt_bytes(receipt: TerminalReceipt) -> bytes:
    payload = {
        "schema": "remote_parse_terminal_receipt.v1",
        "attempt_identity": receipt.attempt_identity,
        "fence_identity": receipt.fence_identity,
        "source_pdf_sha256": receipt.source_pdf_sha256,
        "artifact_owner_identity": receipt.artifact_owner_identity,
        "artifact_byte_count": receipt.artifact_byte_count,
        "artifact_sha256": receipt.artifact_sha256,
        "resume_token_sha256": receipt.resume_token_sha256,
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _canonical_checkpoint_receipt_bytes(
    receipt: (
        PreparedReconcileReceipt | AcceptedSubmissionReceipt
        | PreparedMaterializationReceiptV2 | LocalMaterializationReceipt
        | LocalMaterializationReceiptV2 | FailureReceipt
    ),
) -> bytes:
    payload: dict[str, object]
    if isinstance(receipt, PreparedReconcileReceipt):
        payload = {
            "schema": "remote_parse_prepared_reconcile.v1",
            **{item.name: getattr(receipt, item.name) for item in fields(receipt)},
        }
    elif isinstance(receipt, AcceptedSubmissionReceipt):
        payload = {
            "schema": "remote_parse_accepted_submission.v1",
            **{item.name: getattr(receipt, item.name) for item in fields(receipt)},
        }
    elif isinstance(receipt, PreparedMaterializationReceiptV2):
        payload = {
            "schema": "remote_parse_prepared_materialization.v2",
            **{item.name: getattr(receipt, item.name) for item in fields(receipt)},
        }
    elif isinstance(receipt, LocalMaterializationReceiptV2):
        payload = {
            "schema": "remote_parse_local_receipt.v2",
            **{item.name: getattr(receipt, item.name) for item in fields(receipt)},
        }
    elif isinstance(receipt, LocalMaterializationReceipt):
        payload = {
            "schema": "remote_parse_local_receipt.v1",
            **{
                item.name: getattr(receipt, item.name)
                for item in fields(receipt)
            },
        }
    else:
        payload = {
            "schema": "remote_parse_failure_receipt.v1",
            **{
                item.name: getattr(receipt, item.name)
                for item in fields(receipt)
            },
        }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def encode_checkpoint_receipt(
    receipt: (
        PreparedReconcileReceipt | AcceptedSubmissionReceipt
        | PreparedMaterializationReceiptV2 | LocalMaterializationReceipt
        | LocalMaterializationReceiptV2 | FailureReceipt
    ),
) -> EncodedCheckpointReceipt:
    exact = _canonical_checkpoint_receipt_bytes(receipt)
    return EncodedCheckpointReceipt(
        receipt=receipt,
        exact_bytes=exact,
        sha256="sha256:" + hashlib.sha256(exact).hexdigest(),
        byte_count=len(exact),
    )


def decode_checkpoint_receipt(exact_bytes: bytes) -> EncodedCheckpointReceipt:
    if type(exact_bytes) is not bytes or not exact_bytes or len(exact_bytes) > 65536:
        raise ValueError("checkpoint receipt bytes are outside the closed envelope")

    def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate checkpoint receipt field: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            exact_bytes.decode("utf-8"),
            object_pairs_hook=closed_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("checkpoint receipt is not closed JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("checkpoint receipt must be an object")
    schema = payload.pop("schema", None)
    receipt_type: (
        type[PreparedReconcileReceipt]
        | type[AcceptedSubmissionReceipt]
        | type[PreparedMaterializationReceiptV2]
        | type[LocalMaterializationReceipt]
        | type[LocalMaterializationReceiptV2]
        | type[FailureReceipt]
    )
    if schema == "remote_parse_prepared_reconcile.v1":
        receipt_type = PreparedReconcileReceipt
    elif schema == "remote_parse_accepted_submission.v1":
        receipt_type = AcceptedSubmissionReceipt
    elif schema == "remote_parse_prepared_materialization.v2":
        receipt_type = PreparedMaterializationReceiptV2
    elif schema == "remote_parse_local_receipt.v1":
        receipt_type = LocalMaterializationReceipt
    elif schema == "remote_parse_local_receipt.v2":
        receipt_type = LocalMaterializationReceiptV2
    elif schema == "remote_parse_failure_receipt.v1":
        receipt_type = FailureReceipt
    else:
        raise ValueError("checkpoint receipt schema is unsupported")
    expected = frozenset(item.name for item in fields(receipt_type))
    if frozenset(payload) != expected:
        raise ValueError("checkpoint receipt fields are not closed")
    receipt = receipt_type(**payload)
    encoded = encode_checkpoint_receipt(receipt)
    if encoded.exact_bytes != exact_bytes:
        raise ValueError("checkpoint receipt JSON is not canonical")
    return encoded


def _validate_safe_relpath(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"invalid {label}")


def _checked_temporary_disk_peak(compressed: int, uncompressed: int) -> int:
    peak = compressed + uncompressed
    if peak > _MAX_EVIDENCE_INT:
        raise ValueError("materialization temporary disk peak overflowed")
    return peak


def encode_terminal_receipt(receipt: TerminalReceipt) -> EncodedTerminalReceipt:
    exact = _canonical_terminal_receipt_bytes(receipt)
    return EncodedTerminalReceipt(
        receipt=receipt,
        exact_bytes=exact,
        sha256="sha256:" + hashlib.sha256(exact).hexdigest(),
        byte_count=len(exact),
    )


def decode_terminal_receipt(exact_bytes: bytes) -> EncodedTerminalReceipt:
    if not exact_bytes or len(exact_bytes) > 65536:
        raise ValueError("terminal receipt bytes are outside the closed envelope")

    def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate terminal receipt field: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            exact_bytes.decode("utf-8"),
            object_pairs_hook=closed_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("terminal receipt is not strict UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _RECEIPT_KEYS:
        raise ValueError("terminal receipt fields are not closed")
    if payload.get("schema") != "remote_parse_terminal_receipt.v1":
        raise ValueError("terminal receipt schema drifted")
    receipt = TerminalReceipt(
        attempt_identity=payload["attempt_identity"],
        fence_identity=payload["fence_identity"],
        source_pdf_sha256=payload["source_pdf_sha256"],
        artifact_owner_identity=payload["artifact_owner_identity"],
        artifact_byte_count=payload["artifact_byte_count"],
        artifact_sha256=payload["artifact_sha256"],
        resume_token_sha256=payload["resume_token_sha256"],
    )
    encoded = encode_terminal_receipt(receipt)
    if encoded.exact_bytes != exact_bytes:
        raise ValueError("terminal receipt is not canonical JSON bytes")
    return encoded


__all__ = [
    "AttemptState",
    "AcceptedSubmissionReceipt",
    "ALLOWED_TRANSITIONS",
    "EncodedTerminalReceipt",
    "EncodedCheckpointReceipt",
    "FailureReceipt",
    "FINAL_STATES",
    "NONFINAL_STATES",
    "RemoteParseAttempt",
    "RemoteParseCheckpointConflict",
    "RemoteParseResumeSecret",
    "LocalMaterializationReceipt",
    "LocalMaterializationReceiptV2",
    "PreparedMaterializationReceiptV2",
    "PreparedReconcileReceipt",
    "TerminalReceipt",
    "decode_terminal_receipt",
    "decode_checkpoint_receipt",
    "encode_checkpoint_receipt",
    "encode_terminal_receipt",
]
