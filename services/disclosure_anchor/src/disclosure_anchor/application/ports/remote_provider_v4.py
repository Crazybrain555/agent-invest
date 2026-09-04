"""One-episode provider boundary for remote-parse lifecycle v4.

The port deliberately exposes no blocking remote handle and no durable resume
token.  A submission call performs at most one provider POST.  A later episode
may replay the same canonical request identity and source bytes only after an
exact absence proof; the provider protocol linearizes that key to at most one
distinct durable task. Polling performs exactly one status observation and, only for a completed
result, one result-lease acquisition.  Scheduling, retry budgets, persistence,
and backpressure remain outside this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from math import isfinite
from contextlib import AbstractContextManager
from typing import BinaryIO, Literal, Protocol

from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    SnapshotReceiptV4,
    SubmissionAbsenceProofV4,
    SubmissionIntentV4,
    TerminalReceiptV4,
)
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.staged_provider_parser import (
    PrivateProviderCapabilityV4,
    V4StageGuard,
)

_MAX_REQUEST_BYTES = 64 * 1024
_MAX_INT = (1 << 63) - 1


class PinnedSnapshotSourceV4(Protocol):
    """Opaque, claim-bound stream issued by the sole V4 scratch-root owner."""

    def validates(
        self,
        *,
        submission_intent: SubmissionIntentV4,
        snapshot_receipt: SnapshotReceiptV4,
    ) -> bool: ...

    def open(
        self, *, step_guard: V4StageGuard
    ) -> AbstractContextManager[BinaryIO]: ...


class RemoteProviderV4Error(RuntimeError):
    """Base class for typed, token-free V4 provider failures."""


class RemoteProviderProtocolErrorV4(RemoteProviderV4Error):
    """The provider response or exact request spec violated the closed contract."""


class RemoteProviderUnavailableV4(RemoteProviderV4Error):
    """No provider side effect was made, or a poll observation was unavailable."""


class RemoteSubmissionAmbiguousV4(RemoteProviderV4Error):
    """POST began but acceptance was not reconciled before the stage deadline."""


@dataclass(frozen=True, slots=True)
class RemoteSubmissionCommandV4:
    """Ephemeral exact submission spec closed against durable V4 evidence."""

    submission_intent: SubmissionIntentV4
    snapshot_receipt: SnapshotReceiptV4
    snapshot_source: PinnedSnapshotSourceV4 = field(repr=False, compare=False)
    source_byte_count: int
    parser_identity: ParserIdentity
    parser_options: ParserOptions
    upload_filename: str
    request_exact_bytes: bytes = field(repr=False)
    step_guard: V4StageGuard = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.submission_intent) is not SubmissionIntentV4
            or type(self.snapshot_receipt) is not SnapshotReceiptV4
            or type(self.parser_identity) is not ParserIdentity
            or type(self.parser_options) is not ParserOptions
        ):
            raise ValueError("remote submission command shape is invalid")
        if (
            isinstance(self.source_byte_count, bool)
            or not isinstance(self.source_byte_count, int)
            or not 0 < self.source_byte_count <= _MAX_INT
        ):
            raise ValueError("remote submission source byte count is invalid")
        intent = self.submission_intent
        snapshot = self.snapshot_receipt
        if (
            snapshot.attempt_id != intent.attempt_id
            or snapshot.fence_identity != intent.fence_identity
            or snapshot.sha256 != intent.snapshot_receipt_sha256
            or snapshot.snapshot_sha256 != intent.source_pdf_sha256
            or snapshot.snapshot_byte_count != self.source_byte_count
        ):
            raise ValueError("remote submission snapshot evidence drifted")
        if (
            type(self.upload_filename) is not str
            or self.upload_filename
            != f"{intent.source_pdf_sha256.removeprefix('sha256:')}.pdf"
        ):
            raise ValueError("remote submission filename is not deterministic")
        if (
            type(self.request_exact_bytes) is not bytes
            or not 1 <= len(self.request_exact_bytes) <= _MAX_REQUEST_BYTES
            or _digest(self.request_exact_bytes) != intent.request_sha256
        ):
            raise ValueError("remote submission exact request does not close")
        target = self.parser_options.target_identity(self.parser_identity)
        if _target_digest(target) != intent.parser_target_sha256:
            raise ValueError("remote submission parser target drifted")
        if (
            self.parser_options.runtime_bundle_identity_sha256
            != intent.runtime_epoch_sha256
        ):
            raise ValueError("remote submission runtime epoch drifted")
        validates = getattr(self.snapshot_source, "validates", None)
        opener = getattr(self.snapshot_source, "open", None)
        if (
            not callable(validates)
            or not callable(opener)
            or validates(
                submission_intent=intent,
                snapshot_receipt=snapshot,
            )
            is not True
        ):
            raise ValueError("remote submission snapshot source is not bound")
        if not callable(getattr(self.step_guard, "checkpoint", None)) or not callable(
            getattr(self.step_guard, "remaining_seconds", None)
        ):
            raise ValueError("remote submission stage guard is invalid")


@dataclass(frozen=True, slots=True)
class AcceptedProviderSubmissionV4:
    submission_intent: SubmissionIntentV4
    receipt: AcceptedSubmissionReceiptV4
    provider_capability: PrivateProviderCapabilityV4 = field(repr=False)
    absence_proof: SubmissionAbsenceProofV4 | None = None

    def __post_init__(self) -> None:
        if (
            type(self.submission_intent) is not SubmissionIntentV4
            or type(self.receipt) is not AcceptedSubmissionReceiptV4
            or type(self.provider_capability) is not PrivateProviderCapabilityV4
            or self.receipt.submission_intent_sha256
            != self.submission_intent.sha256
            or self.receipt.attempt_id != self.submission_intent.attempt_id
            or self.receipt.fence_identity
            != self.submission_intent.fence_identity
            or self.receipt.provider_protocol_version
            != self.submission_intent.provider_protocol_version
            or not self.provider_capability.validates_accepted_submission(self.receipt)
            or self.provider_capability.capability_purpose
            != "submitted_task_resume"
            or (
                self.absence_proof is not None
                and type(self.absence_proof) is not SubmissionAbsenceProofV4
            )
        ):
            raise ValueError("accepted provider submission is not exactly bound")
        if self.absence_proof is not None and (
            self.absence_proof.client_submit_key
            != self.submission_intent.client_submit_key
            or self.absence_proof.provider_protocol_version
            != self.receipt.provider_protocol_version
        ):
            raise ValueError("accepted submission absence proof protocol drifted")


@dataclass(frozen=True, slots=True)
class RemotePollCommandV4:
    submission_intent: SubmissionIntentV4
    accepted_submission: AcceptedSubmissionReceiptV4
    provider_capability: PrivateProviderCapabilityV4 = field(repr=False)
    artifact_byte_limit: int
    result_lease_seconds: int
    step_guard: V4StageGuard = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        intent = self.submission_intent
        accepted = self.accepted_submission
        if (
            type(intent) is not SubmissionIntentV4
            or type(accepted) is not AcceptedSubmissionReceiptV4
            or type(self.provider_capability) is not PrivateProviderCapabilityV4
            or accepted.submission_intent_sha256 != intent.sha256
            or accepted.attempt_id != intent.attempt_id
            or accepted.fence_identity != intent.fence_identity
            or accepted.provider_protocol_version
            != intent.provider_protocol_version
            or not self.provider_capability.validates_accepted_submission(accepted)
            or self.provider_capability.capability_purpose
            != "submitted_task_resume"
        ):
            raise ValueError("remote poll evidence is not exactly bound")
        if (
            isinstance(self.artifact_byte_limit, bool)
            or not isinstance(self.artifact_byte_limit, int)
            or not 0 < self.artifact_byte_limit <= _MAX_INT
            or isinstance(self.result_lease_seconds, bool)
            or not isinstance(self.result_lease_seconds, int)
            or not 1 <= self.result_lease_seconds <= 3600
        ):
            raise ValueError("remote poll limits are invalid")
        if not callable(getattr(self.step_guard, "checkpoint", None)) or not callable(
            getattr(self.step_guard, "remaining_seconds", None)
        ):
            raise ValueError("remote poll stage guard is invalid")


@dataclass(frozen=True, slots=True)
class RemoteProviderWaitingV4:
    remote_task_identity: str
    status: Literal["pending", "processing"]
    response_sha256: str
    response_byte_count: int

    def __post_init__(self) -> None:
        _identity(self.remote_task_identity, "waiting remote task")
        if self.status not in {"pending", "processing"}:
            raise ValueError("remote waiting status is invalid")
        _response_identity(self.response_sha256, self.response_byte_count)


@dataclass(frozen=True, slots=True)
class RemoteProviderCompletedV4:
    receipt: TerminalReceiptV4
    result_lease_until_unix: float
    lease_observed_at_unix: float
    lease_response_sha256: str
    lease_response_byte_count: int

    def __post_init__(self) -> None:
        if type(self.receipt) is not TerminalReceiptV4:
            raise ValueError("remote completed receipt is invalid")
        if (
            isinstance(self.result_lease_until_unix, bool)
            or not isinstance(self.result_lease_until_unix, (int, float))
            or not isfinite(float(self.result_lease_until_unix))
            or isinstance(self.lease_observed_at_unix, bool)
            or not isinstance(self.lease_observed_at_unix, (int, float))
            or not isfinite(float(self.lease_observed_at_unix))
            or self.result_lease_until_unix
            <= float(self.lease_observed_at_unix)
        ):
            raise ValueError("remote result lease time is invalid")
        _response_identity(
            self.lease_response_sha256,
            self.lease_response_byte_count,
        )


@dataclass(frozen=True, slots=True)
class RemoteProviderFailedV4:
    remote_task_identity: str
    provider_error: str = field(repr=False)
    response_sha256: str
    response_byte_count: int

    def __post_init__(self) -> None:
        _identity(self.remote_task_identity, "failed remote task")
        if (
            type(self.provider_error) is not str
            or not self.provider_error
            or len(self.provider_error.encode("utf-8")) > 4096
        ):
            raise ValueError("remote provider failure message is invalid")
        _response_identity(self.response_sha256, self.response_byte_count)


RemoteProviderPollOutcomeV4 = (
    RemoteProviderWaitingV4 | RemoteProviderCompletedV4 | RemoteProviderFailedV4
)


class RemoteProviderV4Port(Protocol):
    def reconcile_or_submit(
        self, command: RemoteSubmissionCommandV4
    ) -> AcceptedProviderSubmissionV4: ...

    def poll_once(
        self, command: RemotePollCommandV4
    ) -> RemoteProviderPollOutcomeV4: ...


def _target_digest(target: ParserTargetIdentity) -> str:
    exact = json.dumps(
        target.to_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _digest(exact)


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _identity(value: str, label: str) -> None:
    if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > 1024:
        raise ValueError(f"{label} identity is invalid")


def _response_identity(sha256: str, byte_count: int) -> None:
    if (
        type(sha256) is not str
        or not sha256.startswith("sha256:")
        or len(sha256) != 71
        or any(char not in "0123456789abcdef" for char in sha256[7:])
        or isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count < 0
    ):
        raise ValueError("remote provider response identity is invalid")


__all__ = [
    "AcceptedProviderSubmissionV4",
    "PinnedSnapshotSourceV4",
    "RemotePollCommandV4",
    "RemoteProviderCompletedV4",
    "RemoteProviderFailedV4",
    "RemoteProviderPollOutcomeV4",
    "RemoteProviderProtocolErrorV4",
    "RemoteProviderUnavailableV4",
    "RemoteProviderV4Error",
    "RemoteProviderV4Port",
    "RemoteProviderWaitingV4",
    "RemoteSubmissionAmbiguousV4",
    "RemoteSubmissionCommandV4",
]
