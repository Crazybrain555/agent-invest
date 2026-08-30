"""Optional two-stage provider parser contract.

The production worker keeps its existing synchronous parser integration until
an adapter can durably prove remote terminal/result ownership and recover the
local continuation. Any staged adapter implements the sole protocol-v2 wire
contract; there is no staged-protocol fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
from pathlib import Path
from pathlib import PurePosixPath
from typing import Protocol
from urllib.parse import urlsplit

from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.ports.provider_parser import ProviderParserResult
from disclosure_anchor.application.contracts.remote_parse_checkpoint import (
    EncodedCheckpointReceipt,
    PreparedReconcileReceipt,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


class SubmissionAcceptanceAmbiguous(ParserOutputContractError):
    """Remote POST began but its acceptance cannot yet be reconciled."""


_DURABLE_CHECKPOINT_STATES = frozenset(
    {
        "prepared",
        "reconciling",
        "submitted",
        "pre_submission_failed",
        "remote_failure_committed",
        "local_failure_committed",
        "finish_committed",
    }
)


@dataclass(frozen=True, slots=True)
class ProviderAckCompletionWitness:
    schema: str
    attempt_identity: str
    fence_identity: str
    remote_task_identity: str
    source_pdf_sha256: str
    committed_state: str
    terminal_receipt_sha256: str | None
    failure_receipt_sha256: str | None
    http_status: int
    exact_bytes: bytes = field(repr=False)
    sha256: str
    mac_sha256: str

    def __post_init__(self) -> None:
        if self.schema != "provider-ack-completion.v1":
            raise ValueError("provider ACK witness schema is unsupported")
        if self.committed_state not in {
            "finish_committed", "remote_failure_committed", "local_failure_committed"
        }:
            raise ValueError("provider ACK witness committed state is unsupported")
        for value in (
            self.attempt_identity, self.fence_identity, self.remote_task_identity
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ValueError("provider ACK witness identity is invalid")
        _require_sha256(self.source_pdf_sha256, "provider ACK source")
        for optional_hash in (
            self.terminal_receipt_sha256, self.failure_receipt_sha256
        ):
            if optional_hash is not None:
                _require_sha256(optional_hash, "provider ACK receipt")
        expected = {
            "finish_committed": (True, False),
            "remote_failure_committed": (False, True),
            "local_failure_committed": (True, True),
        }[self.committed_state]
        if (
            self.terminal_receipt_sha256 is not None,
            self.failure_receipt_sha256 is not None,
        ) != expected or self.http_status not in {200, 204}:
            raise ValueError("provider ACK witness shape is invalid")
        canonical = _provider_ack_canonical_bytes(self)
        if self.exact_bytes != canonical or self.sha256 != (
            "sha256:" + hashlib.sha256(canonical).hexdigest()
        ):
            raise ValueError("provider ACK witness canonical bytes drifted")
        _require_sha256(self.mac_sha256, "provider ACK MAC")


def _issue_provider_ack_completion_witness(
    *, attempt_identity: str, fence_identity: str, remote_task_identity: str,
    source_pdf_sha256: str, committed_state: str,
    terminal_receipt_sha256: str | None,
    failure_receipt_sha256: str | None, http_status: int,
    accepted_secret: bytes,
) -> ProviderAckCompletionWitness:
    projection = {
        "schema": "provider-ack-completion.v1",
        "attempt_identity": attempt_identity,
        "fence_identity": fence_identity,
        "remote_task_identity": remote_task_identity,
        "source_pdf_sha256": source_pdf_sha256,
        "committed_state": committed_state,
        "terminal_receipt_sha256": terminal_receipt_sha256,
        "failure_receipt_sha256": failure_receipt_sha256,
        "http_status": http_status,
    }
    canonical = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    return ProviderAckCompletionWitness(
        schema="provider-ack-completion.v1",
        attempt_identity=attempt_identity,
        fence_identity=fence_identity,
        remote_task_identity=remote_task_identity,
        source_pdf_sha256=source_pdf_sha256,
        committed_state=committed_state,
        terminal_receipt_sha256=terminal_receipt_sha256,
        failure_receipt_sha256=failure_receipt_sha256,
        http_status=http_status,
        exact_bytes=canonical,
        sha256="sha256:" + hashlib.sha256(canonical).hexdigest(),
        mac_sha256="sha256:" + hmac.new(
            accepted_secret, canonical, hashlib.sha256
        ).hexdigest(),
    )


def verify_provider_ack_completion_witness(
    value: object, *, accepted_secret: bytes,
) -> bool:
    if type(value) is not ProviderAckCompletionWitness:
        return False
    try:
        canonical = _provider_ack_canonical_bytes(value)
    except (AttributeError, TypeError, ValueError):
        return False
    expected_sha = "sha256:" + hashlib.sha256(canonical).hexdigest()
    expected_mac = "sha256:" + hmac.new(
        accepted_secret, canonical, hashlib.sha256
    ).hexdigest()
    return (
        value.exact_bytes == canonical
        and hmac.compare_digest(value.sha256, expected_sha)
        and hmac.compare_digest(value.mac_sha256, expected_mac)
    )


def _provider_ack_canonical_bytes(value: ProviderAckCompletionWitness) -> bytes:
    projection = {
        "schema": value.schema,
        "attempt_identity": value.attempt_identity,
        "fence_identity": value.fence_identity,
        "remote_task_identity": value.remote_task_identity,
        "source_pdf_sha256": value.source_pdf_sha256,
        "committed_state": value.committed_state,
        "terminal_receipt_sha256": value.terminal_receipt_sha256,
        "failure_receipt_sha256": value.failure_receipt_sha256,
        "http_status": value.http_status,
    }
    return json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True, slots=True)
class DurableCheckpointWitness:
    """State-discriminated projection returned after repository CAS/commit."""

    schema: str
    attempt_identity: str
    fence_identity: str
    checkpoint_contract_version: int
    row_version: int
    claim_generation: int
    state: str
    prepared_submission_sha256: str
    source_pdf_sha256: str
    parser_target_identity_sha256: str
    runtime_bundle_identity_sha256: str
    request_sha256: str
    client_submit_key: str
    submission_epoch_unix: int
    accepted_submission_receipt_sha256: str | None
    terminal_receipt_sha256: str | None
    failure_receipt_sha256: str | None
    remote_task_identity: str | None
    exact_bytes: bytes = field(repr=False)
    sha256: str

    def __post_init__(self) -> None:
        if self.schema != "durable-checkpoint-witness.v1":
            raise ValueError("durable checkpoint witness schema is unsupported")
        if self.state not in _DURABLE_CHECKPOINT_STATES:
            raise ValueError("durable checkpoint witness state is unsupported")
        for identity in (self.attempt_identity, self.fence_identity):
            if (
                not isinstance(identity, str)
                or not identity.strip()
                or len(identity) > 1024
            ):
                raise ValueError("durable checkpoint identity is invalid")
        if self.checkpoint_contract_version not in {2, 3}:
            raise ValueError("checkpoint contract version must be 2 or 3")
        for count, label in (
            (self.row_version, "row version"),
            (self.claim_generation, "claim generation"),
        ):
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError(f"durable {label} must be positive")
        for value in (
            self.prepared_submission_sha256,
            self.source_pdf_sha256,
            self.parser_target_identity_sha256,
            self.runtime_bundle_identity_sha256,
            self.request_sha256,
        ):
            _require_sha256(value, "prepared checkpoint identity")
        if not self.client_submit_key.strip() or len(self.client_submit_key) > 128:
            raise ValueError("durable checkpoint submit key is invalid")
        if type(self.submission_epoch_unix) is not int or self.submission_epoch_unix < 0:
            raise ValueError("durable checkpoint submission epoch is invalid")
        hashes = (
            self.accepted_submission_receipt_sha256,
            self.terminal_receipt_sha256,
            self.failure_receipt_sha256,
        )
        for receipt_hash in hashes:
            if receipt_hash is not None:
                _require_sha256(
                    receipt_hash, "state-discriminated checkpoint receipt"
                )
        expected_presence = {
            "prepared": (False, False, False, False),
            "reconciling": (False, False, False, False),
            "submitted": (True, False, False, True),
            "pre_submission_failed": (False, False, True, False),
            "remote_failure_committed": (True, False, True, True),
            "local_failure_committed": (True, True, True, True),
            "finish_committed": (True, True, False, True),
        }[self.state]
        actual_presence = tuple(value is not None for value in (*hashes, self.remote_task_identity))
        if actual_presence != expected_presence:
            raise ValueError("durable checkpoint fields do not match its state")
        if self.remote_task_identity is not None and (
            not self.remote_task_identity.strip() or len(self.remote_task_identity) > 1024
        ):
            raise ValueError("durable checkpoint remote task is invalid")
        projection = {
            "schema": self.schema,
            "attempt_identity": self.attempt_identity,
            "fence_identity": self.fence_identity,
            "checkpoint_contract_version": self.checkpoint_contract_version,
            "row_version": self.row_version,
            "claim_generation": self.claim_generation,
            "state": self.state,
            "prepared_submission_sha256": self.prepared_submission_sha256,
            "source_pdf_sha256": self.source_pdf_sha256,
            "parser_target_identity_sha256": self.parser_target_identity_sha256,
            "runtime_bundle_identity_sha256": self.runtime_bundle_identity_sha256,
            "request_sha256": self.request_sha256,
            "client_submit_key": self.client_submit_key,
            "submission_epoch_unix": self.submission_epoch_unix,
            "accepted_submission_receipt_sha256": self.accepted_submission_receipt_sha256,
            "terminal_receipt_sha256": self.terminal_receipt_sha256,
            "failure_receipt_sha256": self.failure_receipt_sha256,
            "remote_task_identity": self.remote_task_identity,
        }
        canonical = json.dumps(
            projection, sort_keys=True, separators=(",", ":")
        ).encode()
        if self.exact_bytes != canonical or self.sha256 != (
            "sha256:" + hashlib.sha256(canonical).hexdigest()
        ):
            raise ValueError("durable checkpoint witness exact bytes drifted")


def encode_durable_checkpoint_witness(
    *,
    attempt_identity: str,
    fence_identity: str,
    checkpoint_contract_version: int,
    row_version: int,
    claim_generation: int,
    state: str,
    prepared_identity: PreparedSubmissionIdentity,
    accepted_submission_receipt_sha256: str | None,
    terminal_receipt_sha256: str | None,
    failure_receipt_sha256: str | None,
    remote_task_identity: str | None,
) -> DurableCheckpointWitness:
    projection = {
        "schema": "durable-checkpoint-witness.v1",
        "attempt_identity": attempt_identity,
        "fence_identity": fence_identity,
        "checkpoint_contract_version": checkpoint_contract_version,
        "row_version": row_version,
        "claim_generation": claim_generation,
        "state": state,
        "prepared_submission_sha256": prepared_identity.sha256,
        "source_pdf_sha256": prepared_identity.source_pdf_sha256,
        "parser_target_identity_sha256": (
            prepared_identity.parser_target_identity_sha256
        ),
        "runtime_bundle_identity_sha256": (
            prepared_identity.runtime_bundle_identity_sha256
        ),
        "request_sha256": prepared_identity.request_sha256,
        "client_submit_key": prepared_identity.client_submit_key,
        "submission_epoch_unix": prepared_identity.submission_epoch_unix,
        "accepted_submission_receipt_sha256": (
            accepted_submission_receipt_sha256
        ),
        "terminal_receipt_sha256": terminal_receipt_sha256,
        "failure_receipt_sha256": failure_receipt_sha256,
        "remote_task_identity": remote_task_identity,
    }
    exact = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    return DurableCheckpointWitness(
        schema="durable-checkpoint-witness.v1",
        attempt_identity=attempt_identity,
        fence_identity=fence_identity,
        checkpoint_contract_version=checkpoint_contract_version,
        row_version=row_version,
        claim_generation=claim_generation,
        state=state,
        prepared_submission_sha256=prepared_identity.sha256,
        source_pdf_sha256=prepared_identity.source_pdf_sha256,
        parser_target_identity_sha256=prepared_identity.parser_target_identity_sha256,
        runtime_bundle_identity_sha256=prepared_identity.runtime_bundle_identity_sha256,
        request_sha256=prepared_identity.request_sha256,
        client_submit_key=prepared_identity.client_submit_key,
        submission_epoch_unix=prepared_identity.submission_epoch_unix,
        accepted_submission_receipt_sha256=accepted_submission_receipt_sha256,
        terminal_receipt_sha256=terminal_receipt_sha256,
        failure_receipt_sha256=failure_receipt_sha256,
        remote_task_identity=remote_task_identity,
        exact_bytes=exact,
        sha256="sha256:" + hashlib.sha256(exact).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class RemoteArtifactReceipt:
    """Content-free, durable identity for one terminal remote result."""

    attempt_identity: str
    fence_identity: str
    artifact_owner_identity: str
    artifact_byte_count: int
    artifact_sha256: str = ""
    source_pdf_sha256: str = ""
    resume_token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.attempt_identity,
                self.fence_identity,
                self.artifact_owner_identity,
            )
        ):
            raise ValueError("remote artifact identities must be non-empty")
        if self.artifact_byte_count <= 0:
            raise ValueError("remote artifact byte count must be positive")
        for value, label in (
            (self.artifact_sha256, "remote artifact identity"),
            (self.source_pdf_sha256, "remote source identity"),
        ):
            if not value:
                continue
            candidate = value[7:] if label == "remote source identity" and value.startswith("sha256:") else value
            if (
                len(candidate) != 64
                or candidate != candidate.lower()
                or any(char not in "0123456789abcdef" for char in candidate)
                or (label == "remote source identity" and not value.startswith("sha256:"))
            ):
                raise ValueError(f"{label} must be canonical sha256")


@dataclass(frozen=True, slots=True)
class PersistedSubmissionReceipt:
    """Closed public projection written before a submitted task is resumed."""

    schema: str
    attempt_identity: str
    fence_identity: str
    source_pdf_sha256: str
    client_submit_key: str
    submission_epoch_unix: int
    remote_task_identity: str
    status_url: str
    result_url: str
    exact_bytes: bytes = field(repr=False)
    sha256: str

    def __post_init__(self) -> None:
        if self.schema != "mineru-staged-submission.v1":
            raise ValueError("submission receipt schema is unsupported")
        if not self.exact_bytes or len(self.exact_bytes) > 65_536:
            raise ValueError("submission receipt bytes are outside the closed envelope")
        _require_sha256(self.source_pdf_sha256, "submission source")
        _require_sha256(self.sha256, "submission receipt")
        if (
            isinstance(self.submission_epoch_unix, bool)
            or not isinstance(self.submission_epoch_unix, int)
            or self.submission_epoch_unix < 0
        ):
            raise ValueError("submission epoch must be non-negative")
        for value in (
            self.attempt_identity,
            self.fence_identity,
            self.client_submit_key,
            self.remote_task_identity,
            self.status_url,
            self.result_url,
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("submission identities must be non-empty")
        status = urlsplit(self.status_url)
        result = urlsplit(self.result_url)
        if (
            status.scheme not in {"http", "https"}
            or status.scheme != result.scheme
            or status.netloc != result.netloc
            or status.username is not None
            or result.username is not None
            or status.fragment
            or result.fragment
        ):
            raise ValueError("submission URLs must share a closed HTTP origin")
        projection = {
            "schema": self.schema,
            "attempt_identity": self.attempt_identity,
            "fence_identity": self.fence_identity,
            "source_pdf_sha256": self.source_pdf_sha256,
            "client_submit_key": self.client_submit_key,
            "submission_epoch_unix": self.submission_epoch_unix,
            "remote_task_identity": self.remote_task_identity,
            "status_url": self.status_url,
            "result_url": self.result_url,
        }
        canonical = json.dumps(
            projection, sort_keys=True, separators=(",", ":")
        ).encode()
        if self.exact_bytes != canonical or self.sha256 != (
            "sha256:" + hashlib.sha256(canonical).hexdigest()
        ):
            raise ValueError("submission receipt exact bytes drifted")


@dataclass(frozen=True, slots=True)
class PreparedSubmissionIdentity:
    """Pure closed identity persisted before any remote submission IO."""

    schema: str
    attempt_identity: str
    fence_identity: str
    source_pdf_sha256: str
    parser_target_identity_sha256: str
    runtime_bundle_identity_sha256: str
    request_sha256: str
    client_submit_key: str
    submission_epoch_unix: int
    exact_bytes: bytes = field(repr=False)
    sha256: str

    def __post_init__(self) -> None:
        if self.schema != "mineru-prepared-submission.v1":
            raise ValueError("prepared submission schema is unsupported")
        for value, label in (
            (self.source_pdf_sha256, "prepared submission source"),
            (self.parser_target_identity_sha256, "prepared parser target"),
            (self.runtime_bundle_identity_sha256, "prepared runtime bundle"),
            (self.request_sha256, "prepared request"),
            (self.sha256, "prepared submission"),
        ):
            _require_sha256(value, label)
        if (
            isinstance(self.submission_epoch_unix, bool)
            or not isinstance(self.submission_epoch_unix, int)
            or self.submission_epoch_unix < 0
        ):
            raise ValueError("prepared submission epoch is invalid")
        for value in (
            self.attempt_identity,
            self.fence_identity,
            self.client_submit_key,
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ValueError("prepared submission identity is invalid")
        projection = {
            "schema": self.schema,
            "attempt_identity": self.attempt_identity,
            "fence_identity": self.fence_identity,
            "source_pdf_sha256": self.source_pdf_sha256,
            "parser_target_identity_sha256": self.parser_target_identity_sha256,
            "runtime_bundle_identity_sha256": self.runtime_bundle_identity_sha256,
            "request_sha256": self.request_sha256,
            "client_submit_key": self.client_submit_key,
            "submission_epoch_unix": self.submission_epoch_unix,
        }
        canonical = json.dumps(
            projection, sort_keys=True, separators=(",", ":")
        ).encode()
        if (
            not self.exact_bytes
            or len(self.exact_bytes) > 65_536
            or self.exact_bytes != canonical
            or self.sha256 != "sha256:" + hashlib.sha256(canonical).hexdigest()
        ):
            raise ValueError("prepared submission exact bytes drifted")


def prepared_submission_identity_from_reconcile(
    receipt: PreparedReconcileReceipt,
) -> PreparedSubmissionIdentity:
    """Map the DB canonical prepared receipt to the provider identity exactly."""

    projection = {
        "schema": "mineru-prepared-submission.v1",
        "attempt_identity": receipt.attempt_identity,
        "fence_identity": receipt.fence_identity,
        "source_pdf_sha256": receipt.source_pdf_sha256,
        "parser_target_identity_sha256": receipt.parser_target_sha256,
        "runtime_bundle_identity_sha256": receipt.runtime_epoch_sha256,
        "request_sha256": receipt.request_sha256,
        "client_submit_key": receipt.client_submit_key,
        "submission_epoch_unix": receipt.submission_epoch_unix,
    }
    exact = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    return PreparedSubmissionIdentity(
        schema="mineru-prepared-submission.v1",
        attempt_identity=receipt.attempt_identity,
        fence_identity=receipt.fence_identity,
        source_pdf_sha256=receipt.source_pdf_sha256,
        parser_target_identity_sha256=receipt.parser_target_sha256,
        runtime_bundle_identity_sha256=receipt.runtime_epoch_sha256,
        request_sha256=receipt.request_sha256,
        client_submit_key=receipt.client_submit_key,
        submission_epoch_unix=receipt.submission_epoch_unix,
        exact_bytes=exact,
        sha256="sha256:" + hashlib.sha256(exact).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class PreparedLocalSubmission:
    """Attempt-owned immutable upload snapshot completed before remote IO."""

    identity: PreparedSubmissionIdentity
    checkpoint_contract_version: int
    row_version: int
    claim_generation: int
    snapshot_path: Path = field(repr=False)
    snapshot_sha256: str
    snapshot_bytes: int
    snapshot_device: int
    snapshot_inode: int
    snapshot_mode: int
    snapshot_uid: int
    snapshot_nlink: int
    snapshot_mtime_ns: int
    snapshot_ctime_ns: int
    upload_filename: str

    def __post_init__(self) -> None:
        _require_sha256(self.snapshot_sha256, "submission snapshot")
        if self.checkpoint_contract_version != 2:
            raise ValueError("submission checkpoint contract must be version 2")
        for count in (self.row_version, self.claim_generation):
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError("submission checkpoint facts are invalid")
        for count in (
            self.snapshot_bytes,
            self.snapshot_device,
            self.snapshot_inode,
            self.snapshot_mode,
            self.snapshot_uid,
            self.snapshot_nlink,
            self.snapshot_mtime_ns,
            self.snapshot_ctime_ns,
        ):
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("submission snapshot facts are invalid")
        if self.snapshot_bytes < 1 or self.snapshot_nlink != 1:
            raise ValueError("submission snapshot identity is unsafe")
        if self.upload_filename != f"{self.identity.source_pdf_sha256[7:]}.pdf":
            raise ValueError("submission upload filename drifted")


@dataclass(frozen=True, slots=True)
class PrivateSubmittedTaskResume:
    """Opaque private token; persist only in the private resume-token store."""

    token_bytes: bytes = field(repr=False)
    token_sha256: str

    def __post_init__(self) -> None:
        if not self.token_bytes or len(self.token_bytes) > 65_536:
            raise ValueError("private resume token is outside the closed envelope")
        _require_sha256(self.token_sha256, "private resume token")
        if self.token_sha256 != "sha256:" + hashlib.sha256(self.token_bytes).hexdigest():
            raise ValueError("private resume token hash drifted")


@dataclass(frozen=True, slots=True)
class PreparedMaterialization:
    """Verified local admission facts for a downloaded retained result."""

    attempt_identity: str
    fence_identity: str
    source_pdf_sha256: str
    terminal_receipt_sha256: str
    spool_sha256: str
    compressed_bytes: int
    uncompressed_bytes: int
    member_count: int
    disk_bytes: int
    decoded_bytes: int
    private_token_bytes: bytes = field(repr=False)
    private_token_sha256: str

    def __post_init__(self) -> None:
        if not self.attempt_identity.strip() or not self.fence_identity.strip():
            raise ValueError("prepared identities must be non-empty")
        for value, label in (
            (self.source_pdf_sha256, "prepared source"),
            (self.terminal_receipt_sha256, "terminal receipt"),
            (self.spool_sha256, "prepared spool"),
            (self.private_token_sha256, "prepared private token"),
        ):
            _require_sha256(value, label)
        for count in (
            self.compressed_bytes,
            self.uncompressed_bytes,
            self.member_count,
            self.disk_bytes,
            self.decoded_bytes,
        ):
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("prepared projection must be a non-negative integer")
        if not self.private_token_bytes or len(self.private_token_bytes) > 65_536:
            raise ValueError("prepared private token is outside the closed envelope")
        if self.private_token_sha256 != (
            "sha256:" + hashlib.sha256(self.private_token_bytes).hexdigest()
        ):
            raise ValueError("prepared private token hash drifted")


def _require_sha256(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise ValueError(f"{label} must be canonical sha256")


@dataclass(frozen=True, slots=True)
class ProviderMaterializationEvidence:
    """Content-free evidence returned with a locally materialized parse."""

    attempt_identity: str
    fence_identity: str
    source_pdf_sha256: str
    parser_target_identity_sha256: str
    producer_claim_generation: int
    terminal_owner_identity: str
    terminal_artifact_sha256: str
    terminal_artifact_bytes: int
    artifact_root_relpath: str
    manifest_relpath: str
    manifest_sha256: str
    manifest_bytes: int
    provider_envelope_relpath: str | None = None
    provider_envelope_sha256: str | None = None
    provider_envelope_bytes: int | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.source_pdf_sha256, "evidence source"),
            (self.parser_target_identity_sha256, "evidence parser target"),
            ("sha256:" + self.terminal_artifact_sha256, "terminal artifact"),
            (self.manifest_sha256, "evidence manifest"),
        ):
            _require_sha256(value, label)
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.attempt_identity,
                self.fence_identity,
                self.terminal_owner_identity,
                self.artifact_root_relpath,
                self.manifest_relpath,
            )
        ):
            raise ValueError("materialization evidence identities must be non-empty")
        for count in (
            self.producer_claim_generation,
            self.terminal_artifact_bytes,
            self.manifest_bytes,
        ):
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError("materialization evidence counts are invalid")
        for relpath in (self.artifact_root_relpath, self.manifest_relpath):
            pure = PurePosixPath(relpath)
            if pure.is_absolute() or ".." in pure.parts:
                raise ValueError("materialization evidence path is unsafe")
        envelope = (
            self.provider_envelope_relpath,
            self.provider_envelope_sha256,
            self.provider_envelope_bytes,
        )
        if any(value is not None for value in envelope):
            if not all(value is not None for value in envelope):
                raise ValueError("provider envelope evidence must be all-or-none")
            _require_sha256(self.provider_envelope_sha256 or "", "provider envelope")
            pure = PurePosixPath(self.provider_envelope_relpath or "")
            if pure.is_absolute() or ".." in pure.parts:
                raise ValueError("provider envelope path is unsafe")
            if (
                isinstance(self.provider_envelope_bytes, bool)
                or not isinstance(self.provider_envelope_bytes, int)
                or self.provider_envelope_bytes < 1
            ):
                raise ValueError("provider envelope byte count is invalid")


@dataclass(frozen=True, slots=True)
class StagedProviderParserResult:
    result: ProviderParserResult
    evidence: ProviderMaterializationEvidence


class RemoteProviderParseHandle(Protocol):
    """One accepted remote task whose local result has not been materialized."""

    def wait_terminal(self) -> RemoteArtifactReceipt:
        """Return only after terminal state and result ownership are proven."""

    def submission_checkpoint(
        self,
    ) -> tuple[PersistedSubmissionReceipt, PrivateSubmittedTaskResume]: ...

    def prepare_materialization(
        self, *, receipt: RemoteArtifactReceipt, source_pdf_sha256: str
    ) -> PreparedMaterialization: ...

    def materialize_prepared(
        self,
        *,
        prepared: PreparedMaterialization,
        receipt: RemoteArtifactReceipt,
        output_dir: Path,
        source_pdf_sha256: str,
        parser_target_identity_sha256: str,
        producer_claim_generation: int,
    ) -> StagedProviderParserResult: ...

    def cancel_and_drain(self) -> None:
        """Close admission and prove the accepted remote task terminal."""

    def acknowledge_after_finish_committed(
        self, *, receipt: RemoteArtifactReceipt, witness: DurableCheckpointWitness
    ) -> ProviderAckCompletionWitness:
        """ACK only after the durable DB checkpoint is exactly finish_committed."""

    def acknowledge_after_failure_committed(
        self,
        *,
        witness: DurableCheckpointWitness,
        failure_receipt: EncodedCheckpointReceipt,
    ) -> ProviderAckCompletionWitness:
        """ACK only after remote_failure_committed/local_failure_committed."""


class StagedProviderDocumentParserPort(Protocol):
    """Capability port; absence keeps the existing synchronous parser path."""

    def prepare_submission_identity(
        self,
        *,
        options: ParserOptions,
        source_pdf_sha256: str,
        attempt_identity: str,
        fence_identity: str,
        submission_epoch_unix: int,
    ) -> PreparedSubmissionIdentity:
        """Compute the complete durable submission identity without IO."""

    def begin_remote_parse(
        self,
        *,
        options: ParserOptions,
        prepared_submission: PreparedLocalSubmission,
    ) -> RemoteProviderParseHandle:
        ...

    def prepare_local_submission(
        self,
        *,
        input_pdf: Path,
        options: ParserOptions,
        identity: PreparedSubmissionIdentity,
        witness: DurableCheckpointWitness,
    ) -> PreparedLocalSubmission:
        """Complete all local source/snapshot IO before remote reconciliation."""

    def discard_local_submission(
        self,
        *,
        prepared_submission: PreparedLocalSubmission | PreparedSubmissionIdentity,
        witness: DurableCheckpointWitness,
        submission_receipt: PersistedSubmissionReceipt | None = None,
        accepted_receipt: EncodedCheckpointReceipt | None = None,
        failure_receipt: EncodedCheckpointReceipt | None = None,
    ) -> None:
        """Discard only after a DB state proves POST replay no longer needs it."""

    def resume_remote_parse(
        self,
        *,
        receipt: RemoteArtifactReceipt,
        options: ParserOptions,
    ) -> RemoteProviderParseHandle:
        """Rehydrate a terminal result from durable state after restart."""

    def resume_submitted_parse(
        self,
        *,
        receipt: PersistedSubmissionReceipt,
        secret: PrivateSubmittedTaskResume,
        options: ParserOptions,
    ) -> RemoteProviderParseHandle: ...


__all__ = [
    "DurableCheckpointWitness",
    "encode_durable_checkpoint_witness",
    "RemoteArtifactReceipt",
    "PersistedSubmissionReceipt",
    "PreparedSubmissionIdentity",
    "prepared_submission_identity_from_reconcile",
    "PreparedLocalSubmission",
    "PrivateSubmittedTaskResume",
    "PreparedMaterialization",
    "ProviderMaterializationEvidence",
    "ProviderAckCompletionWitness",
    "verify_provider_ack_completion_witness",
    "RemoteProviderParseHandle",
    "StagedProviderParserResult",
    "StagedProviderDocumentParserPort",
    "SubmissionAcceptanceAmbiguous",
]
