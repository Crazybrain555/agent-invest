"""Closed immutable evidence records for the remote-parse v4 lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LocalMaterializationManifestV4,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelope,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    CheckpointStateV4,
    LocalCleanupPlanV4,
    LocalCleanupReceiptV4,
    LocalMaterializationReceiptV4,
    MaterializationIntentV4,
    ProviderAckReceiptV4,
    RemoteParseCheckpointV4,
    ResourceReservationV4,
    advance_remote_parse_checkpoint_v4,
    build_initial_remote_parse_checkpoint_v4,
    decode_local_cleanup_plan_v4,
    decode_local_cleanup_receipt_v4,
    decode_local_materialization_receipt_v4,
    decode_materialization_intent_v4,
    decode_provider_ack_receipt_v4,
    validate_local_cleanup_plan_v4,
    validate_materialized_provider_evidence_v4,
    validate_resource_reservation_checkpoint_binding_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
)
from disclosure_anchor.application.contracts.staged_resource_paths import (
    validate_relative_resource_path_v4,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads

PREPARATION_INTENT_V4_CONTRACT = "remote-parse-preparation-intent.v4"
SNAPSHOT_RECEIPT_V4_CONTRACT = "remote-parse-snapshot-receipt.v4"
SUBMISSION_INTENT_V4_CONTRACT = "remote-parse-submission-intent.v4"
SUBMISSION_ABSENCE_PROOF_V4_CONTRACT = "submission-absence-proof.v4"
ACCEPTED_SUBMISSION_V4_CONTRACT = "accepted-submission-receipt.v4"
TERMINAL_RECEIPT_V4_CONTRACT = "remote-terminal-receipt.v4"
FAILURE_RECEIPT_V4_CONTRACT = "remote-parse-failure-receipt.v4"
SUPERSESSION_RECEIPT_V4_CONTRACT = "remote-parse-supersession-receipt.v4"

ACCEPTED_SUBMISSION_SECRET_KIND_MAX_BYTES = 128
ACCEPTED_SUBMISSION_TOKEN_MAX_BYTES = 65_536

EvidenceKindV4 = Literal[
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
]
FailureOutcomeV4 = Literal[
    "preparation_failure",
    "pre_submission_failure",
    "remote_failure",
    "local_failure",
]

_MAX_BYTES = 1024 * 1024
_MAX_INT = (1 << 63) - 1
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class PreparationIntentV4:
    attempt_id: str
    fence_identity: str
    document_id: str
    processing_run_id: str
    source_pdf_sha256: str
    source_byte_count: int
    source_page_count: int
    parser_target_sha256: str
    request_sha256: str
    runtime_epoch_sha256: str
    process_profile_sha256: str
    reservation_sha256: str
    snapshot_relpath: str
    snapshot_part_relpath: str
    snapshot_part_owner_relpath: str
    snapshot_lock_relpath: str
    contract_version: str = PREPARATION_INTENT_V4_CONTRACT

    def __post_init__(self) -> None:
        _contract(self.contract_version, PREPARATION_INTENT_V4_CONTRACT)
        _identities(
            self.attempt_id,
            self.fence_identity,
            self.document_id,
            self.processing_run_id,
        )
        for value, label in (
            (self.source_pdf_sha256, "source PDF"),
            (self.parser_target_sha256, "parser target"),
            (self.request_sha256, "request"),
            (self.runtime_epoch_sha256, "runtime epoch"),
            (self.process_profile_sha256, "process profile"),
            (self.reservation_sha256, "reservation"),
        ):
            _sha(value, label)
        _positive(self.source_byte_count, "source byte count")
        _positive(self.source_page_count, "source page count")
        for value, label in (
            (self.snapshot_relpath, "snapshot"),
            (self.snapshot_part_relpath, "snapshot part"),
            (self.snapshot_part_owner_relpath, "snapshot part owner"),
            (self.snapshot_lock_relpath, "snapshot lock"),
        ):
            _relpath(value, label)

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical(asdict(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


@dataclass(frozen=True, slots=True)
class SnapshotReceiptV4:
    attempt_id: str
    fence_identity: str
    preparation_intent_sha256: str
    snapshot_relpath: str
    snapshot_sha256: str
    snapshot_byte_count: int
    part_path_absent: bool
    part_owner_path_absent: bool
    file_fsync_completed: bool
    parent_fsync_completed: bool
    contract_version: str = SNAPSHOT_RECEIPT_V4_CONTRACT

    def __post_init__(self) -> None:
        _contract(self.contract_version, SNAPSHOT_RECEIPT_V4_CONTRACT)
        _identities(self.attempt_id, self.fence_identity)
        _sha(self.preparation_intent_sha256, "preparation intent")
        _sha(self.snapshot_sha256, "snapshot")
        _relpath(self.snapshot_relpath, "snapshot")
        _positive(self.snapshot_byte_count, "snapshot byte count")
        if not all(
            value is True
            for value in (
                self.part_path_absent,
                self.part_owner_path_absent,
                self.file_fsync_completed,
                self.parent_fsync_completed,
            )
        ):
            raise ValueError("snapshot receipt lacks durable closure")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical(asdict(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


def validate_superseding_checkpoint_seed_evidence_v4(
    *,
    checkpoint: RemoteParseCheckpointV4,
    reservation: ResourceReservationV4,
    preparation_intent: PreparationIntentV4,
    snapshot_receipt: SnapshotReceiptV4,
) -> None:
    """Close a superseder's minimal resourceful seed without recursive replay.

    This proves only internal canonical-object coherence.  It does not prove
    that the checkpoint is committed, current, unique, or race-free; the 0057
    repository transaction must establish those facts with an exact CAS.  The
    prepared-submission and parser-target digests also remain upstream root
    claims for 0057 to close against its authoritative persisted rows.
    """

    if (
        type(checkpoint) is not RemoteParseCheckpointV4
        or type(reservation) is not ResourceReservationV4
        or type(preparation_intent) is not PreparationIntentV4
        or type(snapshot_receipt) is not SnapshotReceiptV4
    ):
        raise ValueError("superseding checkpoint seed evidence type is invalid")
    if checkpoint.state == "preparation_failed" or (
        checkpoint.state == "superseded" and checkpoint.lifecycle_version == 0
    ):
        raise ValueError("superseding checkpoint seed is resource-free")
    validate_resource_reservation_checkpoint_binding_v4(
        reservation=reservation,
        checkpoint=checkpoint,
    )
    preparation_facts = (
        preparation_intent.attempt_id,
        preparation_intent.fence_identity,
        preparation_intent.document_id,
        preparation_intent.processing_run_id,
        preparation_intent.source_pdf_sha256,
        preparation_intent.source_byte_count,
        preparation_intent.source_page_count,
        preparation_intent.request_sha256,
        preparation_intent.runtime_epoch_sha256,
        preparation_intent.process_profile_sha256,
        preparation_intent.reservation_sha256,
        preparation_intent.snapshot_relpath,
        preparation_intent.snapshot_part_relpath,
        preparation_intent.snapshot_part_owner_relpath,
        preparation_intent.snapshot_lock_relpath,
    )
    reservation_facts = (
        reservation.attempt_id,
        reservation.fence_identity,
        reservation.document_id,
        reservation.processing_run_id,
        reservation.source_pdf_sha256,
        reservation.source_byte_count,
        reservation.source_page_count,
        reservation.request_sha256,
        reservation.runtime_epoch_sha256,
        reservation.process_profile_sha256,
        reservation.sha256,
        reservation.snapshot_relpath,
        reservation.snapshot_part_relpath,
        reservation.snapshot_part_owner_relpath,
        reservation.snapshot_lock_relpath,
    )
    snapshot_facts = (
        snapshot_receipt.attempt_id,
        snapshot_receipt.fence_identity,
        snapshot_receipt.preparation_intent_sha256,
        snapshot_receipt.snapshot_relpath,
        snapshot_receipt.snapshot_sha256,
        snapshot_receipt.snapshot_byte_count,
    )
    expected_snapshot_facts = (
        reservation.attempt_id,
        reservation.fence_identity,
        preparation_intent.sha256,
        reservation.snapshot_relpath,
        reservation.source_pdf_sha256,
        reservation.source_byte_count,
    )
    if (
        checkpoint.preparation_intent_sha256 != preparation_intent.sha256
        or checkpoint.snapshot_receipt_sha256 != snapshot_receipt.sha256
        or preparation_facts != reservation_facts
        or snapshot_facts != expected_snapshot_facts
    ):
        raise ValueError("superseding checkpoint seed evidence drifted")


@dataclass(frozen=True, slots=True)
class SubmissionAbsenceProofV4:
    client_submit_key: str
    lookup_request_sha256: str
    provider_protocol_version: str
    http_status: int
    response_sha256: str
    response_byte_count: int
    contract_version: str = SUBMISSION_ABSENCE_PROOF_V4_CONTRACT

    def __post_init__(self) -> None:
        _contract(self.contract_version, SUBMISSION_ABSENCE_PROOF_V4_CONTRACT)
        _identities(self.client_submit_key, self.provider_protocol_version)
        _sha(self.lookup_request_sha256, "absence lookup request")
        _sha(self.response_sha256, "absence response")
        if type(self.http_status) is not int or self.http_status != 404:
            raise ValueError("submission absence proof requires exact HTTP 404")
        _nonnegative(self.response_byte_count, "absence response byte count")


@dataclass(frozen=True, slots=True)
class SubmissionIntentV4:
    attempt_id: str
    fence_identity: str
    snapshot_receipt_sha256: str
    source_pdf_sha256: str
    parser_target_sha256: str
    request_sha256: str
    runtime_epoch_sha256: str
    client_submit_key: str
    submission_epoch_unix: int
    provider_protocol_version: str
    contract_version: str = SUBMISSION_INTENT_V4_CONTRACT

    def __post_init__(self) -> None:
        _contract(self.contract_version, SUBMISSION_INTENT_V4_CONTRACT)
        _identities(
            self.attempt_id,
            self.fence_identity,
            self.client_submit_key,
            self.provider_protocol_version,
        )
        for value, label in (
            (self.snapshot_receipt_sha256, "snapshot receipt"),
            (self.source_pdf_sha256, "source PDF"),
            (self.parser_target_sha256, "parser target"),
            (self.request_sha256, "request"),
            (self.runtime_epoch_sha256, "runtime epoch"),
        ):
            _sha(value, label)
        _nonnegative(self.submission_epoch_unix, "submission epoch")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical(asdict(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


@dataclass(frozen=True, slots=True)
class AcceptedSubmissionReceiptV4:
    attempt_id: str
    fence_identity: str
    submission_intent_sha256: str
    remote_task_identity: str
    status_url: str
    result_url: str
    secret_kind: str
    secret_version: int
    token_sha256: str
    token_byte_count: int
    provider_protocol_version: str
    contract_version: str = ACCEPTED_SUBMISSION_V4_CONTRACT

    def __post_init__(self) -> None:
        _contract(self.contract_version, ACCEPTED_SUBMISSION_V4_CONTRACT)
        _identities(
            self.attempt_id,
            self.fence_identity,
            self.remote_task_identity,
            self.status_url,
            self.result_url,
            self.provider_protocol_version,
        )
        _identity(
            self.secret_kind,
            max_bytes=ACCEPTED_SUBMISSION_SECRET_KIND_MAX_BYTES,
        )
        _sha(self.submission_intent_sha256, "submission intent")
        _sha(self.token_sha256, "provider token")
        _positive(self.secret_version, "secret version")
        _positive(self.token_byte_count, "token byte count")
        if self.token_byte_count > ACCEPTED_SUBMISSION_TOKEN_MAX_BYTES:
            raise ValueError(
                "accepted-submission token exceeds the durable envelope"
            )
        status = urlsplit(self.status_url)
        result = urlsplit(self.result_url)
        if (
            status.scheme not in {"http", "https"}
            or status.scheme != result.scheme
            or status.netloc != result.netloc
            or not status.netloc
            or status.username is not None
            or result.username is not None
            or status.query
            or result.query
            or status.fragment
            or result.fragment
        ):
            raise ValueError("accepted-submission URLs must share a closed HTTP origin")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical(asdict(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


@dataclass(frozen=True, slots=True)
class TerminalReceiptV4:
    attempt_id: str
    fence_identity: str
    accepted_submission_receipt_sha256: str
    remote_task_identity: str
    result_owner_identity: str
    artifact_sha256: str
    artifact_byte_count: int
    provider_protocol_version: str
    contract_version: str = TERMINAL_RECEIPT_V4_CONTRACT

    def __post_init__(self) -> None:
        _contract(self.contract_version, TERMINAL_RECEIPT_V4_CONTRACT)
        _identities(
            self.attempt_id,
            self.fence_identity,
            self.remote_task_identity,
            self.result_owner_identity,
            self.provider_protocol_version,
        )
        _sha(self.accepted_submission_receipt_sha256, "accepted submission")
        _sha(self.artifact_sha256, "terminal artifact")
        _positive(self.artifact_byte_count, "terminal artifact byte count")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical(asdict(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


@dataclass(frozen=True, slots=True)
class FailureReceiptV4:
    attempt_id: str
    fence_identity: str
    outcome: FailureOutcomeV4
    source_state: str
    source_lifecycle_version: int
    source_checkpoint_sha256: str | None
    submission_was_attempted: bool
    submission_absence_proof: SubmissionAbsenceProofV4 | None
    accepted_submission_receipt_sha256: str | None
    terminal_receipt_sha256: str | None
    materialization_intent_sha256: str | None
    local_materialization_receipt_sha256: str | None
    error_code: str
    error_stage: str
    error_class: str
    retryable: bool
    retry_budget_class: str
    message: str
    contract_version: str = FAILURE_RECEIPT_V4_CONTRACT

    def __post_init__(self) -> None:
        _contract(self.contract_version, FAILURE_RECEIPT_V4_CONTRACT)
        _identities(
            self.attempt_id,
            self.fence_identity,
            self.source_state,
            self.error_code,
            self.error_stage,
            self.error_class,
            self.retry_budget_class,
        )
        if self.outcome not in {
            "preparation_failure",
            "pre_submission_failure",
            "remote_failure",
            "local_failure",
        }:
            raise ValueError("failure receipt outcome is unsupported")
        _nonnegative(self.source_lifecycle_version, "failure source version")
        for value, label in (
            (self.source_checkpoint_sha256, "failure source checkpoint"),
            (self.accepted_submission_receipt_sha256, "accepted submission"),
            (self.terminal_receipt_sha256, "terminal receipt"),
            (self.materialization_intent_sha256, "materialization intent"),
            (self.local_materialization_receipt_sha256, "local receipt"),
        ):
            _optional_sha(value, label)
        if type(self.submission_was_attempted) is not bool or type(self.retryable) is not bool:
            raise ValueError("failure receipt boolean is invalid")
        if not isinstance(self.message, str) or not self.message or len(self.message) > 4096:
            raise ValueError("failure receipt message is invalid")
        if self.outcome == "preparation_failure":
            if self.source_state != "not_prepared" or self.source_lifecycle_version != 0:
                raise ValueError(
                    "preparation failure source state is not resource-free"
                )
            if any(
                value is not None
                for value in (
                    self.source_checkpoint_sha256,
                    self.submission_absence_proof,
                    self.accepted_submission_receipt_sha256,
                    self.terminal_receipt_sha256,
                    self.materialization_intent_sha256,
                    self.local_materialization_receipt_sha256,
                )
            ) or self.submission_was_attempted:
                raise ValueError("preparation failure is not resource-free")
        elif self.source_checkpoint_sha256 is None:
            raise ValueError("resourceful failure lacks source checkpoint")
        if self.outcome == "pre_submission_failure":
            if self.accepted_submission_receipt_sha256 is not None or any(
                value is not None
                for value in (
                    self.terminal_receipt_sha256,
                    self.materialization_intent_sha256,
                    self.local_materialization_receipt_sha256,
                )
            ):
                raise ValueError("pre-submission failure owns accepted resources")
            if self.submission_was_attempted:
                if type(self.submission_absence_proof) is not SubmissionAbsenceProofV4:
                    raise ValueError(
                        "ambiguous submission requires exact 404 absence proof"
                    )
            elif self.submission_absence_proof is not None:
                raise ValueError("never-POSTed failure cannot carry absence proof")
        elif self.outcome == "remote_failure":
            if (
                self.submission_was_attempted is not True
                or self.accepted_submission_receipt_sha256 is None
                or self.submission_absence_proof is not None
                or self.terminal_receipt_sha256 is not None
                or self.materialization_intent_sha256 is not None
                or self.local_materialization_receipt_sha256 is not None
            ):
                raise ValueError("remote failure evidence shape is invalid")
        elif self.outcome == "local_failure":
            if (
                self.submission_was_attempted is not True
                or self.accepted_submission_receipt_sha256 is None
                or self.terminal_receipt_sha256 is None
                or self.submission_absence_proof is not None
            ):
                raise ValueError("local failure evidence shape is invalid")

    @property
    def canonical_bytes(self) -> bytes:
        payload = asdict(self)
        payload["submission_absence_proof"] = (
            None
            if self.submission_absence_proof is None
            else asdict(self.submission_absence_proof)
        )
        return _canonical(payload)

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


@dataclass(frozen=True, slots=True)
class SupersessionReceiptV4:
    attempt_id: str
    fence_identity: str
    source_document_id: str
    source_attempt_generation: int
    source_state: str
    source_lifecycle_version: int
    source_checkpoint_sha256: str | None
    superseding_attempt_id: str
    superseding_attempt_generation: int
    superseding_document_id: str
    superseding_checkpoint_sha256: str
    reason_code: str
    contract_version: str = SUPERSESSION_RECEIPT_V4_CONTRACT

    def __post_init__(self) -> None:
        _contract(self.contract_version, SUPERSESSION_RECEIPT_V4_CONTRACT)
        _identities(
            self.attempt_id,
            self.fence_identity,
            self.source_document_id,
            self.source_state,
            self.superseding_attempt_id,
            self.superseding_document_id,
            self.reason_code,
        )
        _nonnegative(self.source_lifecycle_version, "supersession source version")
        _positive(self.source_attempt_generation, "supersession source generation")
        _optional_sha(self.source_checkpoint_sha256, "supersession source checkpoint")
        _positive(
            self.superseding_attempt_generation,
            "superseding attempt generation",
        )
        _sha(self.superseding_checkpoint_sha256, "superseding checkpoint")
        if self.superseding_attempt_id == self.attempt_id:
            raise ValueError("supersession cannot supersede itself")
        if self.superseding_attempt_generation <= self.source_attempt_generation:
            raise ValueError("superseding attempt generation did not advance")
        if self.superseding_document_id != self.source_document_id:
            raise ValueError("superseding attempt crossed the document chain")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical(asdict(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


EvidenceValueV4 = (
    PreparationIntentV4
    | SnapshotReceiptV4
    | SubmissionIntentV4
    | AcceptedSubmissionReceiptV4
    | TerminalReceiptV4
    | MaterializationIntentV4
    | LocalMaterializationReceiptV4
    | FailureReceiptV4
    | SupersessionReceiptV4
    | LocalCleanupPlanV4
    | LocalCleanupReceiptV4
    | ProviderAckReceiptV4
)


@dataclass(frozen=True, slots=True)
class EncodedRemoteParseEvidenceV4:
    kind: EvidenceKindV4
    value: EvidenceValueV4
    exact_bytes: bytes = field(repr=False)
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        if self.kind not in _EVIDENCE_TYPES:
            raise ValueError("remote-parse v4 evidence kind is unsupported")
        if type(self.value) is not _EVIDENCE_TYPES[self.kind]:
            raise ValueError("remote-parse v4 evidence kind/type drifted")
        if type(self.exact_bytes) is not bytes or not 1 <= len(self.exact_bytes) <= _MAX_BYTES:
            raise ValueError("remote-parse v4 evidence bytes are outside envelope")
        if self.byte_count != len(self.exact_bytes) or isinstance(self.byte_count, bool):
            raise ValueError("remote-parse v4 evidence byte count drifted")
        if self.sha256 != _digest(self.exact_bytes):
            raise ValueError("remote-parse v4 evidence hash drifted")
        if self.exact_bytes != self.value.canonical_bytes:
            raise ValueError("remote-parse v4 evidence projection drifted")


def build_preparation_intent_v4(
    *, reservation: ResourceReservationV4, parser_target_sha256: str
) -> PreparationIntentV4:
    return PreparationIntentV4(
        attempt_id=reservation.attempt_id,
        fence_identity=reservation.fence_identity,
        document_id=reservation.document_id,
        processing_run_id=reservation.processing_run_id,
        source_pdf_sha256=reservation.source_pdf_sha256,
        source_byte_count=reservation.source_byte_count,
        source_page_count=reservation.source_page_count,
        parser_target_sha256=parser_target_sha256,
        request_sha256=reservation.request_sha256,
        runtime_epoch_sha256=reservation.runtime_epoch_sha256,
        process_profile_sha256=reservation.process_profile_sha256,
        reservation_sha256=reservation.sha256,
        snapshot_relpath=reservation.snapshot_relpath,
        snapshot_part_relpath=reservation.snapshot_part_relpath,
        snapshot_part_owner_relpath=reservation.snapshot_part_owner_relpath,
        snapshot_lock_relpath=reservation.snapshot_lock_relpath,
    )


_EVIDENCE_TYPES: dict[str, type[Any]] = {
    "preparation_intent": PreparationIntentV4,
    "snapshot_receipt": SnapshotReceiptV4,
    "submission_intent": SubmissionIntentV4,
    "accepted_submission": AcceptedSubmissionReceiptV4,
    "terminal_receipt": TerminalReceiptV4,
    "materialization_intent": MaterializationIntentV4,
    "local_materialization_receipt": LocalMaterializationReceiptV4,
    "failure_receipt": FailureReceiptV4,
    "supersession_receipt": SupersessionReceiptV4,
    "cleanup_plan": LocalCleanupPlanV4,
    "cleanup_receipt": LocalCleanupReceiptV4,
    "ack_receipt": ProviderAckReceiptV4,
}
_TYPE_KINDS = {value: cast(EvidenceKindV4, key) for key, value in _EVIDENCE_TYPES.items()}


def encode_remote_parse_evidence_v4(
    value: EvidenceValueV4,
) -> EncodedRemoteParseEvidenceV4:
    kind = _TYPE_KINDS.get(type(value))
    if kind is None:
        raise ValueError("remote-parse v4 evidence type is unsupported")
    exact = value.canonical_bytes
    return EncodedRemoteParseEvidenceV4(
        kind=kind,
        value=value,
        exact_bytes=exact,
        sha256=_digest(exact),
        byte_count=len(exact),
    )


def decode_remote_parse_evidence_v4(
    kind: EvidenceKindV4, exact_bytes: bytes
) -> EncodedRemoteParseEvidenceV4:
    decoders = {
        "preparation_intent": lambda value: _decode_dataclass(
            value, PreparationIntentV4
        ),
        "snapshot_receipt": lambda value: _decode_dataclass(
            value, SnapshotReceiptV4
        ),
        "submission_intent": lambda value: _decode_dataclass(
            value, SubmissionIntentV4
        ),
        "accepted_submission": lambda value: _decode_dataclass(
            value, AcceptedSubmissionReceiptV4
        ),
        "terminal_receipt": lambda value: _decode_dataclass(
            value, TerminalReceiptV4
        ),
        "materialization_intent": decode_materialization_intent_v4,
        "local_materialization_receipt": decode_local_materialization_receipt_v4,
        "failure_receipt": _decode_failure,
        "supersession_receipt": lambda value: _decode_dataclass(
            value, SupersessionReceiptV4
        ),
        "cleanup_plan": decode_local_cleanup_plan_v4,
        "cleanup_receipt": decode_local_cleanup_receipt_v4,
        "ack_receipt": decode_provider_ack_receipt_v4,
    }
    decoder = decoders.get(kind)
    if decoder is None:
        raise ValueError("remote-parse v4 evidence kind is unsupported")
    value = decoder(exact_bytes)
    encoded = encode_remote_parse_evidence_v4(value)
    if encoded.kind != kind or encoded.exact_bytes != exact_bytes:
        raise ValueError("remote-parse v4 evidence is not canonical")
    return encoded


def _expected_held_resource_credit_v4(
    *,
    state: str,
    source_byte_count: int,
    accepted: AcceptedSubmissionReceiptV4 | None,
    terminal: TerminalReceiptV4 | None,
    intent: MaterializationIntentV4 | None,
    local_receipt: LocalMaterializationReceiptV4 | None,
) -> ResourceCreditVector | None:
    base_credit = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=source_byte_count,
    )
    if state == "prepared":
        return base_credit
    if state == "reconciling":
        return ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=source_byte_count,
            remote_waits=1,
        )
    if state == "submitted":
        return ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=source_byte_count,
            remote_waits=1,
            provider_tasks=1,
            ack_items=1,
        )
    if state == "remote_terminal":
        if terminal is None:
            raise ValueError("remote-terminal credit lacks exact terminal evidence")
        return ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=source_byte_count,
            provider_tasks=1,
            provider_result_bytes=terminal.artifact_byte_count,
            ack_items=1,
        )
    if state == "materializing":
        if intent is None:
            raise ValueError("materializing credit lacks exact intent evidence")
        return intent.held_resource_credit
    if state in {"local_materialized", "publish_committed"}:
        if intent is None or local_receipt is None:
            raise ValueError("closed-output credit lacks exact local evidence")
        return ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=source_byte_count,
            provider_tasks=1,
            provider_result_bytes=intent.artifact_byte_count,
            compressed_bytes=intent.artifact_byte_count,
            output_items=1,
            output_bytes=local_receipt.output_byte_count,
            output_pages=local_receipt.source_page_count,
            ack_items=1,
        )
    if state == "ack_pending":
        if accepted is None:
            raise ValueError("ack-pending credit lacks accepted-task evidence")
        return ResourceCreditVector(
            documents=1,
            provider_tasks=1,
            provider_result_bytes=(
                terminal.artifact_byte_count if terminal is not None else 0
            ),
            ack_items=1,
        )
    return None


_RESOURCEFUL_PREFIX_STATES: tuple[CheckpointStateV4, ...] = (
    "prepared",
    "reconciling",
    "submitted",
    "remote_terminal",
    "materializing",
    "local_materialized",
    "publish_committed",
)


def _validate_resourceful_checkpoint_history_v4(
    *,
    history: tuple[RemoteParseCheckpointV4, ...],
    reservation: ResourceReservationV4,
    target_checkpoint: RemoteParseCheckpointV4,
    preparation_intent: PreparationIntentV4,
    snapshot_receipt: SnapshotReceiptV4 | None,
    submission_intent: SubmissionIntentV4 | None,
    accepted_submission: AcceptedSubmissionReceiptV4 | None,
    terminal_receipt: TerminalReceiptV4 | None,
    materialization_intent: MaterializationIntentV4 | None,
    local_materialization_receipt: LocalMaterializationReceiptV4 | None,
    publication_winner_sha256: str | None,
) -> None:
    """Replay one ordinary resourceful prefix from its typed roots.

    This proves only canonical object coherence.  It does not prove that any
    supplied checkpoint is committed, current, unique, or race-free.  The 0057
    repository transaction must load the reservation, evidence, and complete
    append-only checkpoint rows under lock, compare those authoritative rows
    byte-for-byte with this history, and CAS the locked head in the same
    transaction.  The publication-winner digest remains an upstream root claim
    until that transaction also matches the locked atomic-publication row.
    """

    if type(history) is not tuple or not history:
        raise ValueError("resourceful checkpoint history must be a non-empty tuple")
    if any(type(item) is not RemoteParseCheckpointV4 for item in history):
        raise ValueError("resourceful checkpoint history item type is invalid")
    if (
        type(reservation) is not ResourceReservationV4
        or type(target_checkpoint) is not RemoteParseCheckpointV4
        or type(preparation_intent) is not PreparationIntentV4
    ):
        raise ValueError("resourceful checkpoint history root type is invalid")
    try:
        source_index = _RESOURCEFUL_PREFIX_STATES.index(
            target_checkpoint.state
        )
    except ValueError as exc:
        raise ValueError(
            "history target is not an ordinary resourceful checkpoint"
        ) from exc
    expected_states = _RESOURCEFUL_PREFIX_STATES[: source_index + 1]
    if len(history) != len(expected_states):
        raise ValueError("resourceful checkpoint history length drifted")

    if snapshot_receipt is not None and type(snapshot_receipt) is not SnapshotReceiptV4:
        raise ValueError("resourceful checkpoint history snapshot type is invalid")
    required: tuple[tuple[int, object | None, type[Any], str], ...] = (
        (1, submission_intent, SubmissionIntentV4, "submission intent"),
        (
            2,
            accepted_submission,
            AcceptedSubmissionReceiptV4,
            "accepted submission",
        ),
        (3, terminal_receipt, TerminalReceiptV4, "terminal receipt"),
        (
            4,
            materialization_intent,
            MaterializationIntentV4,
            "materialization intent",
        ),
        (
            5,
            local_materialization_receipt,
            LocalMaterializationReceiptV4,
            "local materialization receipt",
        ),
    )
    if source_index >= 1 and type(snapshot_receipt) is not SnapshotReceiptV4:
        raise ValueError("resourceful checkpoint history lacks exact snapshot receipt")
    for required_index, value, expected_type, label in required:
        if source_index >= required_index and type(value) is not expected_type:
            raise ValueError(f"resourceful checkpoint history lacks exact {label}")
        if source_index < required_index and value is not None:
            raise ValueError(f"resourceful checkpoint history contains future {label}")
    if source_index == 6:
        if publication_winner_sha256 is None:
            raise ValueError("resourceful checkpoint history lacks publication winner")
        _sha(publication_winner_sha256, "publication winner")
    elif publication_winner_sha256 is not None:
        raise ValueError("resourceful checkpoint history contains a future winner")

    expected_credit = _expected_held_resource_credit_v4(
        state="prepared",
        source_byte_count=reservation.source_byte_count,
        accepted=accepted_submission,
        terminal=terminal_receipt,
        intent=materialization_intent,
        local_receipt=local_materialization_receipt,
    )
    assert expected_credit is not None
    current = build_initial_remote_parse_checkpoint_v4(
        reservation=reservation,
        preparation_intent_sha256=preparation_intent.sha256,
        snapshot_receipt_sha256=(
            snapshot_receipt.sha256 if snapshot_receipt is not None else None
        ),
        held_resource_credit=expected_credit,
    )
    expected_history = [current]
    updates: dict[CheckpointStateV4, tuple[str, str]] = {}
    if submission_intent is not None:
        updates["reconciling"] = (
            "submission_intent_sha256",
            submission_intent.sha256,
        )
    if accepted_submission is not None:
        updates["submitted"] = (
            "accepted_submission_sha256",
            accepted_submission.sha256,
        )
    if terminal_receipt is not None:
        updates["remote_terminal"] = (
            "terminal_receipt_sha256",
            terminal_receipt.sha256,
        )
    if materialization_intent is not None:
        updates["materializing"] = (
            "materialization_intent_sha256",
            materialization_intent.sha256,
        )
    if local_materialization_receipt is not None:
        updates["local_materialized"] = (
            "local_materialization_receipt_sha256",
            local_materialization_receipt.sha256,
        )
    if publication_winner_sha256 is not None:
        updates["publish_committed"] = (
            "publication_winner_sha256",
            publication_winner_sha256,
        )

    for state in expected_states[1:]:
        expected_credit = _expected_held_resource_credit_v4(
            state=state,
            source_byte_count=reservation.source_byte_count,
            accepted=accepted_submission,
            terminal=terminal_receipt,
            intent=materialization_intent,
            local_receipt=local_materialization_receipt,
        )
        if expected_credit is None or state not in updates:
            raise ValueError("resourceful checkpoint history evidence is incomplete")
        field_name, digest = updates[state]
        current = advance_remote_parse_checkpoint_v4(
            current,
            state=state,
            held_resource_credit=expected_credit,
            **{field_name: digest},
        )
        expected_history.append(current)

    expected = tuple(expected_history)
    if history != expected or target_checkpoint != expected[-1]:
        raise ValueError("resourceful checkpoint history drifted from exact replay")


def validate_remote_parse_evidence_bundle_v4(
    *,
    checkpoint: RemoteParseCheckpointV4,
    evidence: tuple[EncodedRemoteParseEvidenceV4, ...],
    reservation: ResourceReservationV4,
    cleanup_source_checkpoint: RemoteParseCheckpointV4 | None = None,
    resourceful_checkpoint_history: tuple[RemoteParseCheckpointV4, ...] | None = None,
    cleanup_pending_checkpoint: RemoteParseCheckpointV4 | None = None,
    ack_pending_checkpoint: RemoteParseCheckpointV4 | None = None,
    superseding_checkpoint: RemoteParseCheckpointV4 | None = None,
    superseding_reservation: ResourceReservationV4 | None = None,
    superseding_preparation_intent: PreparationIntentV4 | None = None,
    superseding_snapshot_receipt: SnapshotReceiptV4 | None = None,
    local_materialization_manifest: LocalMaterializationManifestV4 | None = None,
    provider_envelope: ProviderDocumentEnvelope | None = None,
) -> None:
    """Replay one typed evidence frontier without claiming database authority.

    A root-replayed resourceful history closes every ordinary current
    checkpoint and, after cleanup begins, the exact cleanup source plus the
    cleanup/ACK DAG and held credit.  It does not prove that any checkpoint is
    committed or current; the 0057 repository transaction must load the
    authoritative append-only rows under lock, compare the exact canonical
    history, and establish the head with a compare-and-swap.
    """

    if type(checkpoint) is not RemoteParseCheckpointV4 or type(evidence) is not tuple:
        raise ValueError("remote-parse v4 evidence bundle type is invalid")
    if type(reservation) is not ResourceReservationV4:
        raise ValueError("remote-parse v4 evidence bundle lacks exact reservation")
    try:
        validate_resource_reservation_checkpoint_binding_v4(
            reservation=reservation,
            checkpoint=checkpoint,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "remote-parse v4 evidence bundle reservation drifted from checkpoint"
        ) from exc
    by_kind: dict[EvidenceKindV4, EncodedRemoteParseEvidenceV4] = {}
    for item in evidence:
        if type(item) is not EncodedRemoteParseEvidenceV4:
            raise ValueError("remote-parse v4 evidence bundle item is invalid")
        if item.kind in by_kind:
            raise ValueError("remote-parse v4 evidence bundle repeats a kind")
        by_kind[item.kind] = item
    refs: dict[EvidenceKindV4, str | None] = {
        "preparation_intent": checkpoint.preparation_intent_sha256,
        "snapshot_receipt": checkpoint.snapshot_receipt_sha256,
        "submission_intent": checkpoint.submission_intent_sha256,
        "accepted_submission": checkpoint.accepted_submission_sha256,
        "terminal_receipt": checkpoint.terminal_receipt_sha256,
        "materialization_intent": checkpoint.materialization_intent_sha256,
        "local_materialization_receipt": (
            checkpoint.local_materialization_receipt_sha256
        ),
        "failure_receipt": checkpoint.failure_receipt_sha256,
        "supersession_receipt": checkpoint.supersession_receipt_sha256,
        "cleanup_plan": checkpoint.cleanup_plan_sha256,
        "cleanup_receipt": checkpoint.cleanup_receipt_sha256,
        "ack_receipt": checkpoint.ack_receipt_sha256,
    }
    expected_kinds = {kind for kind, digest in refs.items() if digest is not None}
    if set(by_kind) != expected_kinds:
        raise ValueError("remote-parse v4 evidence bundle kind set drifted")
    for kind, digest in refs.items():
        if digest is not None and by_kind[kind].sha256 != digest:
            raise ValueError("remote-parse v4 evidence bundle hash drifted")
    for item in by_kind.values():
        value = item.value
        if hasattr(value, "attempt_id") and value.attempt_id != checkpoint.attempt_id:
            raise ValueError("remote-parse v4 evidence attempt drifted")
        if (
            hasattr(value, "fence_identity")
            and value.fence_identity != checkpoint.fence_identity
        ):
            raise ValueError("remote-parse v4 evidence fence drifted")
        if hasattr(value, "document_id") and value.document_id != checkpoint.document_id:
            raise ValueError("remote-parse v4 evidence document drifted")
        if (
            hasattr(value, "processing_run_id")
            and value.processing_run_id != checkpoint.processing_run_id
        ):
            raise ValueError("remote-parse v4 evidence run drifted")
    preparation_item = by_kind.get("preparation_intent")
    snapshot_item = by_kind.get("snapshot_receipt")
    submission_item = by_kind.get("submission_intent")
    accepted_item = by_kind.get("accepted_submission")
    terminal_item = by_kind.get("terminal_receipt")
    intent_item = by_kind.get("materialization_intent")
    local_item = by_kind.get("local_materialization_receipt")
    failure_item = by_kind.get("failure_receipt")
    supersession_item = by_kind.get("supersession_receipt")
    cleanup_plan_item = by_kind.get("cleanup_plan")
    cleanup_receipt_item = by_kind.get("cleanup_receipt")
    ack_item = by_kind.get("ack_receipt")
    preparation = (
        cast(PreparationIntentV4, preparation_item.value)
        if preparation_item is not None
        else None
    )
    snapshot = (
        cast(SnapshotReceiptV4, snapshot_item.value)
        if snapshot_item is not None
        else None
    )
    submission = (
        cast(SubmissionIntentV4, submission_item.value)
        if submission_item is not None
        else None
    )
    accepted = (
        cast(AcceptedSubmissionReceiptV4, accepted_item.value)
        if accepted_item is not None
        else None
    )
    terminal = (
        cast(TerminalReceiptV4, terminal_item.value)
        if terminal_item is not None
        else None
    )
    intent = (
        cast(MaterializationIntentV4, intent_item.value)
        if intent_item is not None
        else None
    )
    local_receipt = (
        cast(LocalMaterializationReceiptV4, local_item.value)
        if local_item is not None
        else None
    )
    failure = (
        cast(FailureReceiptV4, failure_item.value)
        if failure_item is not None
        else None
    )
    supersession = (
        cast(SupersessionReceiptV4, supersession_item.value)
        if supersession_item is not None
        else None
    )
    cleanup_plan = (
        cast(LocalCleanupPlanV4, cleanup_plan_item.value)
        if cleanup_plan_item is not None
        else None
    )
    cleanup_receipt = (
        cast(LocalCleanupReceiptV4, cleanup_receipt_item.value)
        if cleanup_receipt_item is not None
        else None
    )
    ack = (
        cast(ProviderAckReceiptV4, ack_item.value)
        if ack_item is not None
        else None
    )
    if preparation is not None:
        if (
            preparation.source_pdf_sha256 != checkpoint.source_pdf_sha256
            or preparation.source_byte_count != checkpoint.source_byte_count
            or preparation.source_page_count != checkpoint.source_page_count
            or preparation.request_sha256 != checkpoint.request_sha256
            or preparation.runtime_epoch_sha256 != checkpoint.runtime_epoch_sha256
            or preparation.process_profile_sha256 != checkpoint.process_profile_sha256
        ):
            raise ValueError("preparation evidence drifted from checkpoint")
        if (
            preparation.reservation_sha256 != reservation.sha256
        ):
            raise ValueError("preparation evidence drifted from reservation")
    if snapshot is not None and (
            preparation_item is None
            or snapshot.preparation_intent_sha256 != preparation_item.sha256
            or snapshot.snapshot_sha256 != checkpoint.source_pdf_sha256
            or snapshot.snapshot_byte_count != checkpoint.source_byte_count
            or preparation is None
            or snapshot.snapshot_relpath != preparation.snapshot_relpath
    ):
        raise ValueError("snapshot evidence chain drifted")
    if submission is not None and (
            snapshot_item is None
            or submission.snapshot_receipt_sha256 != snapshot_item.sha256
            or submission.source_pdf_sha256 != checkpoint.source_pdf_sha256
            or submission.request_sha256 != checkpoint.request_sha256
            or submission.runtime_epoch_sha256 != checkpoint.runtime_epoch_sha256
            or preparation is None
            or submission.parser_target_sha256
            != preparation.parser_target_sha256
    ):
        raise ValueError("submission evidence chain drifted")
    if accepted is not None and (
            submission is None
            or submission_item is None
            or accepted.submission_intent_sha256 != submission_item.sha256
            or accepted.provider_protocol_version
            != submission.provider_protocol_version
    ):
        raise ValueError("accepted-submission evidence chain drifted")
    if terminal is not None and (
            accepted is None
            or accepted_item is None
            or terminal.accepted_submission_receipt_sha256 != accepted_item.sha256
            or terminal.remote_task_identity != accepted.remote_task_identity
            or terminal.provider_protocol_version
            != accepted.provider_protocol_version
    ):
        raise ValueError("terminal evidence chain drifted")
    if intent is not None and (
            terminal is None
            or terminal_item is None
            or intent.terminal_receipt_sha256 != terminal_item.sha256
            or intent.source_pdf_sha256 != checkpoint.source_pdf_sha256
            or intent.source_page_count != checkpoint.source_page_count
            or intent.remote_task_identity
            != terminal.remote_task_identity
            or intent.artifact_owner_identity
            != terminal.result_owner_identity
            or intent.artifact_sha256
            != terminal.artifact_sha256
            or intent.artifact_byte_count
            != terminal.artifact_byte_count
            or accepted is None
            or intent.provider_capability_kind
            != accepted.secret_kind
            or intent.provider_capability_sha256
            != accepted.token_sha256
            or intent.provider_capability_byte_count
            != accepted.token_byte_count
            or preparation is None
            or intent.parser_target_sha256
            != preparation.parser_target_sha256
            or intent.reservation_sha256 != reservation.sha256
    ):
        raise ValueError("materialization-intent evidence chain drifted")
    if local_receipt is not None:
        if (
            intent is None
            or intent_item is None
            or local_receipt.materialization_intent_sha256 != intent_item.sha256
            or local_receipt.terminal_receipt_sha256
            != intent.terminal_receipt_sha256
            or local_receipt.source_pdf_sha256 != checkpoint.source_pdf_sha256
            or local_receipt.source_page_count != checkpoint.source_page_count
            or local_receipt.parser_target_sha256 != intent.parser_target_sha256
            or local_receipt.spool_relpath != intent.spool_relpath
            or local_receipt.spool_sha256 != intent.artifact_sha256
            or local_receipt.spool_byte_count != intent.artifact_byte_count
            or local_receipt.output_relpath != intent.output_relpath
            or local_receipt.provider_envelope_relpath
            != intent.provider_envelope_relpath
            or local_receipt.output_manifest_relpath
            != intent.output_manifest_relpath
            or local_receipt.member_count > intent.member_count_limit
            or local_receipt.uncompressed_byte_count
            > intent.uncompressed_byte_limit
            or local_receipt.decoded_byte_count > intent.decoded_byte_limit
            or local_receipt.temporary_disk_peak_byte_count
            > intent.temporary_disk_byte_limit
            or local_receipt.output_byte_count > intent.output_byte_limit
        ):
            raise ValueError("local-materialization evidence chain drifted")
        if (
            type(local_materialization_manifest)
            is not LocalMaterializationManifestV4
            or type(provider_envelope) is not ProviderDocumentEnvelope
            or intent is None
        ):
            raise ValueError(
                "local-materialization evidence lacks exact manifest or envelope"
            )
        validate_materialized_provider_evidence_v4(
            intent=intent,
            receipt=local_receipt,
            manifest=local_materialization_manifest,
            provider_envelope=provider_envelope,
        )
    elif local_materialization_manifest is not None or provider_envelope is not None:
        raise ValueError("materialization bytes supplied without a local receipt")

    credit_state: str = checkpoint.state
    if credit_state == "cleanup_pending":
        if cleanup_plan is None:
            raise ValueError("cleanup-pending credit lacks its exact cleanup plan")
        credit_state = cleanup_plan.source_state
    expected_credit = _expected_held_resource_credit_v4(
        state=credit_state,
        source_byte_count=checkpoint.source_byte_count,
        accepted=accepted,
        terminal=terminal,
        intent=intent,
        local_receipt=local_receipt,
    )
    if (
        expected_credit is not None
        and checkpoint.held_resource_credit != expected_credit
    ):
        raise ValueError("checkpoint held resource credit drifted from exact evidence")

    if failure is not None:
        if (
            failure.accepted_submission_receipt_sha256
            != (accepted_item.sha256 if accepted_item is not None else None)
            or failure.terminal_receipt_sha256
            != (terminal_item.sha256 if terminal_item is not None else None)
            or failure.materialization_intent_sha256
            != (intent_item.sha256 if intent_item is not None else None)
            or failure.local_materialization_receipt_sha256
            != (local_item.sha256 if local_item is not None else None)
            or failure.source_lifecycle_version > checkpoint.lifecycle_version
        ):
            raise ValueError("failure evidence chain drifted")
        if failure.submission_absence_proof is not None and (
            submission is None
            or (
                failure.submission_absence_proof.client_submit_key
                != submission.client_submit_key
                or failure.submission_absence_proof.provider_protocol_version
                != submission.provider_protocol_version
            )
        ):
            raise ValueError("submission absence proof drifted from intent")

    if supersession is not None:
        if (
            supersession.source_document_id != checkpoint.document_id
            or supersession.superseding_document_id != checkpoint.document_id
            or supersession.source_attempt_generation != checkpoint.attempt_generation
        ):
            raise ValueError("supersession source attempt chain drifted")
        if supersession.source_lifecycle_version > checkpoint.lifecycle_version:
            raise ValueError("supersession evidence source version is in the future")
        if (
            type(superseding_checkpoint) is not RemoteParseCheckpointV4
            or type(superseding_reservation) is not ResourceReservationV4
            or type(superseding_preparation_intent) is not PreparationIntentV4
            or type(superseding_snapshot_receipt) is not SnapshotReceiptV4
        ):
            raise ValueError("supersession lacks its exact superseding seed evidence")
        if (
            supersession.superseding_checkpoint_sha256
            != superseding_checkpoint.sha256
            or supersession.superseding_attempt_id
            != superseding_checkpoint.attempt_id
            or supersession.superseding_attempt_generation
            != superseding_checkpoint.attempt_generation
            or supersession.superseding_document_id
            != superseding_checkpoint.document_id
            or superseding_checkpoint.attempt_id == checkpoint.attempt_id
            or superseding_checkpoint.attempt_generation
            <= checkpoint.attempt_generation
            or superseding_checkpoint.sha256 == checkpoint.sha256
            or superseding_checkpoint.sha256
            == supersession.source_checkpoint_sha256
        ):
            raise ValueError("superseding checkpoint drifted or reused the source")
        validate_superseding_checkpoint_seed_evidence_v4(
            checkpoint=superseding_checkpoint,
            reservation=superseding_reservation,
            preparation_intent=superseding_preparation_intent,
            snapshot_receipt=superseding_snapshot_receipt,
        )
        if (
            checkpoint.state == "superseded"
            and checkpoint.lifecycle_version == 0
            and (
                supersession.source_state != "not_prepared"
                or supersession.source_checkpoint_sha256 is not None
                or supersession.source_lifecycle_version != 0
            )
        ):
            raise ValueError("resource-free supersession source state drifted")
    elif any(
        item is not None
        for item in (
            superseding_checkpoint,
            superseding_reservation,
            superseding_preparation_intent,
            superseding_snapshot_receipt,
        )
    ):
        raise ValueError("superseding seed evidence supplied without supersession")

    is_resource_free_root = (
        checkpoint.lifecycle_version == 0
        and checkpoint.state in {"preparation_failed", "superseded"}
    )
    if is_resource_free_root:
        if resourceful_checkpoint_history is not None:
            raise ValueError("resource-free lifecycle cannot carry resourceful history")
    else:
        history_target = checkpoint
        if cleanup_plan is not None:
            if type(cleanup_source_checkpoint) is not RemoteParseCheckpointV4:
                raise ValueError("cleanup plan lacks its exact source checkpoint")
            history_target = cleanup_source_checkpoint
        elif cleanup_source_checkpoint is not None:
            raise ValueError("cleanup source checkpoint supplied without cleanup plan")
        if type(resourceful_checkpoint_history) is not tuple:
            raise ValueError("resourceful bundle lacks exact checkpoint history")
        if preparation is None:
            raise ValueError("resourceful history lacks exact preparation evidence")
        _validate_resourceful_checkpoint_history_v4(
            history=resourceful_checkpoint_history,
            reservation=reservation,
            target_checkpoint=history_target,
            preparation_intent=preparation,
            snapshot_receipt=snapshot,
            submission_intent=submission,
            accepted_submission=accepted,
            terminal_receipt=terminal,
            materialization_intent=intent,
            local_materialization_receipt=local_receipt,
            publication_winner_sha256=checkpoint.publication_winner_sha256,
        )

    if cleanup_plan is not None:
        assert type(cleanup_source_checkpoint) is RemoteParseCheckpointV4
        validate_local_cleanup_plan_v4(
            plan=cleanup_plan,
            reservation=reservation,
            source_checkpoint=cleanup_source_checkpoint,
            materialization_intent=intent,
            local_receipt=local_receipt,
        )
        expected_cleanup_source_credit = _expected_held_resource_credit_v4(
            state=cleanup_source_checkpoint.state,
            source_byte_count=cleanup_source_checkpoint.source_byte_count,
            accepted=accepted,
            terminal=terminal,
            intent=intent,
            local_receipt=local_receipt,
        )
        if (
            expected_cleanup_source_credit is None
            or cleanup_source_checkpoint.held_resource_credit
            != expected_cleanup_source_credit
        ):
            raise ValueError(
                "cleanup source held resource credit drifted from exact evidence"
            )
        if (
            cleanup_plan.terminal_receipt_sha256
            != (terminal_item.sha256 if terminal_item is not None else None)
            or cleanup_plan.materialization_intent_sha256
            != (intent_item.sha256 if intent_item is not None else None)
            or cleanup_plan.local_materialization_receipt_sha256
            != (local_item.sha256 if local_item is not None else None)
            or cleanup_plan.failure_receipt_sha256
            != (failure_item.sha256 if failure_item is not None else None)
            or cleanup_plan.supersession_receipt_sha256
            != (
                supersession_item.sha256
                if supersession_item is not None
                else None
            )
            or cleanup_plan.publication_winner_sha256
            != checkpoint.publication_winner_sha256
            or cleanup_plan.source_lifecycle_version >= checkpoint.lifecycle_version
        ):
            raise ValueError("cleanup-plan evidence chain drifted")
        if failure_item is not None:
            assert failure is not None
            if (
                cleanup_plan.source_checkpoint_sha256
                != failure.source_checkpoint_sha256
                or cleanup_plan.source_state != failure.source_state
                or cleanup_plan.source_lifecycle_version
                != failure.source_lifecycle_version
            ):
                raise ValueError("failure cleanup source checkpoint drifted")
        if supersession_item is not None:
            assert supersession is not None
            if (
                cleanup_plan.source_checkpoint_sha256
                != supersession.source_checkpoint_sha256
                or cleanup_plan.source_state != supersession.source_state
                or cleanup_plan.source_lifecycle_version
                != supersession.source_lifecycle_version
            ):
                raise ValueError("supersession cleanup source checkpoint drifted")
        if accepted_item is None:
            if cleanup_plan.remote_task_identity is not None:
                raise ValueError("cleanup plan invented a provider task")
        elif accepted is None:
            raise ValueError("cleanup plan accepted receipt type drifted")
        elif cleanup_plan.remote_task_identity != accepted.remote_task_identity:
            raise ValueError("cleanup plan provider task drifted")
        if checkpoint.state == "cleanup_pending" and (
            checkpoint.previous_checkpoint_sha256
            != cleanup_plan.source_checkpoint_sha256
            or checkpoint.lifecycle_version
            != cleanup_plan.source_lifecycle_version + 1
        ):
            raise ValueError("cleanup-pending checkpoint chain drifted")
    if cleanup_receipt is not None:
        if (
            cleanup_plan is None
            or cleanup_plan_item is None
            or type(cleanup_pending_checkpoint) is not RemoteParseCheckpointV4
            or type(cleanup_source_checkpoint) is not RemoteParseCheckpointV4
            or (
                cleanup_receipt.cleanup_plan_sha256 != cleanup_plan_item.sha256
                or cleanup_receipt.outcome != cleanup_plan.outcome
                or cleanup_receipt.cleanup_pending_lifecycle_version
                != cleanup_plan.source_lifecycle_version + 1
            )
        ):
            raise ValueError("cleanup-receipt evidence chain drifted")
        validate_resource_reservation_checkpoint_binding_v4(
            reservation=reservation,
            checkpoint=cleanup_pending_checkpoint,
        )
        expected_cleanup_pending = advance_remote_parse_checkpoint_v4(
            cleanup_source_checkpoint,
            state="cleanup_pending",
            held_resource_credit=cleanup_source_checkpoint.held_resource_credit,
            failure_receipt_sha256=cleanup_plan.failure_receipt_sha256,
            supersession_receipt_sha256=cleanup_plan.supersession_receipt_sha256,
            cleanup_plan_sha256=cleanup_plan.sha256,
        )
        if (
            cleanup_pending_checkpoint != expected_cleanup_pending
            or cleanup_receipt.cleanup_pending_checkpoint_sha256
            != cleanup_pending_checkpoint.sha256
            or cleanup_receipt.cleanup_pending_lifecycle_version
            != cleanup_pending_checkpoint.lifecycle_version
        ):
            raise ValueError(
                "cleanup receipt lacks its exact cleanup-pending checkpoint"
            )
        planned = {
            (
                item.kind,
                item.relpath,
                "absent" if item.action == "delete" else "transferred",
                item.target_owner_identity,
                item.target_relpath,
            )
            for item in cleanup_plan.resources
        }
        observed = {
            (
                item.kind,
                item.relpath,
                item.disposition,
                item.target_owner_identity,
                item.target_relpath,
            )
            for item in cleanup_receipt.results
        }
        if planned != observed:
            raise ValueError("cleanup receipt does not close the exact plan")
        if checkpoint.state == "ack_pending" and (
            checkpoint.previous_checkpoint_sha256
            != cleanup_receipt.cleanup_pending_checkpoint_sha256
            or checkpoint.lifecycle_version
            != cleanup_receipt.cleanup_pending_lifecycle_version + 1
        ):
            raise ValueError("ack-pending checkpoint chain drifted")
    elif cleanup_pending_checkpoint is not None:
        raise ValueError(
            "cleanup-pending checkpoint supplied without cleanup receipt"
        )

    if ack is not None:
        if (
            accepted is None
            or accepted_item is None
            or cleanup_plan is None
            or cleanup_receipt is None
            or type(cleanup_pending_checkpoint) is not RemoteParseCheckpointV4
            or type(ack_pending_checkpoint) is not RemoteParseCheckpointV4
        ):
            raise ValueError("provider ACK lacks its accepted cleanup chain")
        validate_resource_reservation_checkpoint_binding_v4(
            reservation=reservation,
            checkpoint=ack_pending_checkpoint,
        )
        expected_ack_pending_credit = _expected_held_resource_credit_v4(
            state="ack_pending",
            source_byte_count=ack_pending_checkpoint.source_byte_count,
            accepted=accepted,
            terminal=terminal,
            intent=intent,
            local_receipt=local_receipt,
        )
        assert expected_ack_pending_credit is not None
        expected_ack_pending = advance_remote_parse_checkpoint_v4(
            cleanup_pending_checkpoint,
            state="ack_pending",
            held_resource_credit=expected_ack_pending_credit,
            cleanup_receipt_sha256=cleanup_receipt.sha256,
        )
        if (
            ack_pending_checkpoint != expected_ack_pending
            or ack.ack_pending_checkpoint_sha256
            != ack_pending_checkpoint.sha256
            or ack.ack_pending_lifecycle_version
            != ack_pending_checkpoint.lifecycle_version
            or ack.accepted_submission_sha256 != accepted_item.sha256
            or ack.remote_task_identity != accepted.remote_task_identity
            or ack.provider_protocol_version
            != accepted.provider_protocol_version
            or cleanup_plan_item is None
            or cleanup_receipt_item is None
            or ack.cleanup_plan_sha256 != cleanup_plan_item.sha256
            or ack.cleanup_receipt_sha256 != cleanup_receipt_item.sha256
            or ack.outcome != cleanup_plan.outcome
            or ack.terminal_receipt_sha256
            != (terminal_item.sha256 if terminal_item is not None else None)
            or ack.failure_receipt_sha256
            != (failure_item.sha256 if failure_item is not None else None)
            or ack.supersession_receipt_sha256
            != (
                supersession_item.sha256
                if supersession_item is not None
                else None
            )
            or ack.local_materialization_receipt_sha256
            != (local_item.sha256 if local_item is not None else None)
            or ack.publication_winner_sha256
            != checkpoint.publication_winner_sha256
            or ack.ack_pending_lifecycle_version
            != cleanup_receipt.cleanup_pending_lifecycle_version + 1
            or ack.result_owner_identity
            != (terminal.result_owner_identity if terminal is not None else None)
        ):
            raise ValueError("provider ACK evidence chain drifted")
        final_state = cast(
            CheckpointStateV4,
            {
                "success": "acked",
                "remote_failure": "remote_failed",
                "local_failure": "local_failed",
                "superseded": "superseded",
            }[ack.outcome],
        )
        expected_final = advance_remote_parse_checkpoint_v4(
            ack_pending_checkpoint,
            state=final_state,
            held_resource_credit=ResourceCreditVector(),
            ack_receipt_sha256=ack.sha256,
        )
        if checkpoint != expected_final:
            raise ValueError("final ACK checkpoint chain drifted")
    elif ack_pending_checkpoint is not None:
        raise ValueError("ack-pending checkpoint supplied without provider ACK")

    expected_final_outcome = {
        "acked": "success",
        "remote_failed": "remote_failure",
        "local_failed": "local_failure",
        "pre_submission_failed": "pre_submission_failure",
        "superseded": "superseded",
    }.get(checkpoint.state)
    if (
        expected_final_outcome is not None
        and cleanup_plan is not None
        and cleanup_plan.outcome != expected_final_outcome
    ):
        raise ValueError("final checkpoint cleanup outcome drifted")
    if (
        cleanup_receipt is not None
        and (
            checkpoint.state == "pre_submission_failed"
            or (
                checkpoint.state == "superseded"
                and accepted_item is None
            )
        )
        and (
            checkpoint.previous_checkpoint_sha256
            != cleanup_receipt.cleanup_pending_checkpoint_sha256
            or checkpoint.lifecycle_version
            != cleanup_receipt.cleanup_pending_lifecycle_version + 1
        )
    ):
        raise ValueError("non-ACK final cleanup checkpoint chain drifted")
    if checkpoint.state == "pre_submission_failed" and ack_item is not None:
        raise ValueError("pre-submission failure cannot carry provider ACK")
    if (
        checkpoint.state == "superseded"
        and accepted_item is not None
        and ack_item is None
    ):
        raise ValueError("accepted supersession lacks provider ACK")


def _decode_dataclass(exact_bytes: bytes, item_type: type[Any]) -> Any:
    payload = _decode_object(exact_bytes)
    _closed(payload, item_type)
    value = item_type(**payload)
    if value.canonical_bytes != exact_bytes:
        raise ValueError("remote-parse v4 evidence JSON is not canonical")
    return value


def _decode_failure(exact_bytes: bytes) -> FailureReceiptV4:
    payload = _decode_object(exact_bytes)
    _closed(payload, FailureReceiptV4)
    raw_proof = payload["submission_absence_proof"]
    proof = None
    if raw_proof is not None:
        if not isinstance(raw_proof, dict):
            raise ValueError("submission absence proof must be an object")
        nested = cast(dict[str, Any], raw_proof)
        _closed(nested, SubmissionAbsenceProofV4)
        proof = SubmissionAbsenceProofV4(**nested)
    value = FailureReceiptV4(
        **{
            **payload,
            "submission_absence_proof": proof,
        }
    )
    if value.canonical_bytes != exact_bytes:
        raise ValueError("remote-parse v4 failure JSON is not canonical")
    return value


def _decode_object(exact_bytes: bytes) -> dict[str, Any]:
    if type(exact_bytes) is not bytes or not 1 <= len(exact_bytes) <= _MAX_BYTES:
        raise ValueError("remote-parse v4 evidence bytes are outside envelope")
    decoded = strict_json_loads(exact_bytes)
    if not isinstance(decoded, dict):
        raise ValueError(  # noqa: TRY004
            "remote-parse v4 evidence must be an object"
        )
    return cast(dict[str, Any], decoded)


def _closed(payload: dict[str, Any], item_type: type[Any]) -> None:
    if set(payload) != {item.name for item in fields(item_type)}:
        raise ValueError(f"{item_type.__name__} fields are not closed")


def _canonical(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("remote-parse v4 evidence is not strict JSON") from exc
    if not 1 <= len(encoded) <= _MAX_BYTES:
        raise ValueError("remote-parse v4 evidence bytes are outside envelope")
    return encoded


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _contract(observed: str, expected: str) -> None:
    if type(observed) is not str or observed != expected:
        raise ValueError("remote-parse v4 evidence contract is unsupported")


def _identities(*values: str) -> None:
    for value in values:
        _identity(value, max_bytes=1024)


def _identity(value: str, *, max_bytes: int) -> None:
    if type(value) is not str:
        raise ValueError("remote-parse v4 evidence identity is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("remote-parse v4 evidence identity is invalid") from None
    if (
        not value
        or value != value.strip()
        or len(encoded) > max_bytes
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError("remote-parse v4 evidence identity is invalid")


def _sha(value: str, label: str) -> None:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError(f"{label} hash is not canonical")


def _optional_sha(value: str | None, label: str) -> None:
    if value is not None:
        _sha(value, label)


def _positive(value: int, label: str) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_INT:
        raise ValueError(f"{label} must be positive")


def _nonnegative(value: int, label: str) -> None:
    if type(value) is not int or not 0 <= value <= _MAX_INT:
        raise ValueError(f"{label} must be non-negative")


def _relpath(value: str, label: str) -> None:
    validate_relative_resource_path_v4(value, label)


__all__ = [
    "ACCEPTED_SUBMISSION_SECRET_KIND_MAX_BYTES",
    "ACCEPTED_SUBMISSION_TOKEN_MAX_BYTES",
    "ACCEPTED_SUBMISSION_V4_CONTRACT",
    "FAILURE_RECEIPT_V4_CONTRACT",
    "PREPARATION_INTENT_V4_CONTRACT",
    "SNAPSHOT_RECEIPT_V4_CONTRACT",
    "SUBMISSION_ABSENCE_PROOF_V4_CONTRACT",
    "SUBMISSION_INTENT_V4_CONTRACT",
    "SUPERSESSION_RECEIPT_V4_CONTRACT",
    "TERMINAL_RECEIPT_V4_CONTRACT",
    "AcceptedSubmissionReceiptV4",
    "EncodedRemoteParseEvidenceV4",
    "EvidenceKindV4",
    "EvidenceValueV4",
    "FailureReceiptV4",
    "PreparationIntentV4",
    "SnapshotReceiptV4",
    "SubmissionAbsenceProofV4",
    "SubmissionIntentV4",
    "SupersessionReceiptV4",
    "TerminalReceiptV4",
    "build_preparation_intent_v4",
    "decode_remote_parse_evidence_v4",
    "encode_remote_parse_evidence_v4",
    "validate_remote_parse_evidence_bundle_v4",
    "validate_superseding_checkpoint_seed_evidence_v4",
]
