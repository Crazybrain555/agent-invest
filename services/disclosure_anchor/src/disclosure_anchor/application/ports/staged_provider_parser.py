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
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    LocalCleanupPlanV4,
    LocalCleanupReceiptV4,
    LocalMaterializationReceiptV4,
    MaterializationIntentV4,
    ProviderAckReceiptV4,
    RemoteParseCheckpointV4,
    ResourceReservationV4,
    provider_ack_request_v4_bytes,
    provider_ack_request_v4_identity,
    validate_local_cleanup_plan_v4,
    validate_materialized_provider_evidence_v4,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    EncodedRemoteParseEvidenceV4,
    PreparationIntentV4,
    SnapshotReceiptV4,
    TerminalReceiptV4,
    validate_durable_remote_parse_evidence_bundle_v4,
    validate_remote_parse_evidence_bundle_v4,
)
from disclosure_anchor.application.contracts.provider_document import ProviderDocument
from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelope,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LocalMaterializationManifestV4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    PerAttemptResourceAllowance,
    ResourceCreditVector,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


_MAX_INT = (1 << 63) - 1


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
_PREPARED_CHECKPOINT_VERSIONS = frozenset({2, 3})


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

    @classmethod
    def from_checkpoint(
        cls,
        *,
        identity: PreparedSubmissionIdentity,
        witness: DurableCheckpointWitness,
        snapshot_path: Path,
        snapshot_sha256: str,
        snapshot_bytes: int,
        snapshot_device: int,
        snapshot_inode: int,
        snapshot_mode: int,
        snapshot_uid: int,
        snapshot_nlink: int,
        snapshot_mtime_ns: int,
        snapshot_ctime_ns: int,
    ) -> PreparedLocalSubmission:
        """Build the sole prepared snapshot projection from durable evidence."""

        if witness.state not in {"prepared", "reconciling"} or not (
            witness.attempt_identity == identity.attempt_identity
            and witness.fence_identity == identity.fence_identity
            and witness.prepared_submission_sha256 == identity.sha256
            and witness.source_pdf_sha256 == identity.source_pdf_sha256
            and witness.parser_target_identity_sha256
            == identity.parser_target_identity_sha256
            and witness.runtime_bundle_identity_sha256
            == identity.runtime_bundle_identity_sha256
            and witness.request_sha256 == identity.request_sha256
            and witness.client_submit_key == identity.client_submit_key
            and witness.submission_epoch_unix == identity.submission_epoch_unix
        ):
            raise ValueError("prepared snapshot witness drifted from identity")
        return cls(
            identity=identity,
            checkpoint_contract_version=witness.checkpoint_contract_version,
            row_version=witness.row_version,
            claim_generation=witness.claim_generation,
            snapshot_path=snapshot_path,
            snapshot_sha256=snapshot_sha256,
            snapshot_bytes=snapshot_bytes,
            snapshot_device=snapshot_device,
            snapshot_inode=snapshot_inode,
            snapshot_mode=snapshot_mode,
            snapshot_uid=snapshot_uid,
            snapshot_nlink=snapshot_nlink,
            snapshot_mtime_ns=snapshot_mtime_ns,
            snapshot_ctime_ns=snapshot_ctime_ns,
            upload_filename=f"{identity.source_pdf_sha256[7:]}.pdf",
        )

    def __post_init__(self) -> None:
        _require_sha256(self.snapshot_sha256, "submission snapshot")
        if self.checkpoint_contract_version not in _PREPARED_CHECKPOINT_VERSIONS:
            raise ValueError("submission checkpoint contract version is unsupported")
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
class RecoveredV3ResumeSecret:
    """Claim-bound private v3 token recovered from the private repository."""

    attempt_identity: str
    secret_kind: str
    token_bytes: bytes = field(repr=False)
    token_sha256: str
    token_byte_count: int
    checkpoint_row_version: int
    claim_owner_identity: str
    claim_generation: int

    def __post_init__(self) -> None:
        if self.secret_kind not in {
            "prepared_reconcile", "accepted_submission", "terminal", "materialization"
        }:
            raise ValueError("recovered v3 secret kind is unsupported")
        if (
            not self.attempt_identity.strip()
            or not self.claim_owner_identity.strip()
            or type(self.token_bytes) is not bytes
            or not self.token_bytes
            or len(self.token_bytes) > 65_536
            or self.token_byte_count != len(self.token_bytes)
            or type(self.checkpoint_row_version) is not int
            or self.checkpoint_row_version < 1
            or type(self.claim_generation) is not int
            or self.claim_generation < 1
        ):
            raise ValueError("recovered v3 secret identity is invalid")
        _require_sha256(self.token_sha256, "recovered v3 secret")
        if self.token_sha256 != "sha256:" + hashlib.sha256(self.token_bytes).hexdigest():
            raise ValueError("recovered v3 secret hash drifted")

    def submitted_task_token(self) -> PrivateSubmittedTaskResume:
        if self.secret_kind != "accepted_submission":
            raise ValueError("submitted resume requires accepted-submission secret")
        return PrivateSubmittedTaskResume(
            token_bytes=self.token_bytes,
            token_sha256=self.token_sha256,
        )


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

    @property
    def public_sha256(self) -> str:
        payload = {
            name: getattr(self, name)
            for name in (
                "attempt_identity", "fence_identity", "source_pdf_sha256",
                "terminal_receipt_sha256", "spool_sha256", "compressed_bytes",
                "uncompressed_bytes", "member_count", "disk_bytes",
                "decoded_bytes", "private_token_sha256",
            )
        }
        return "sha256:" + hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        ).hexdigest()


def _require_sha256(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise ValueError(f"{label} must be canonical sha256")


def _require_exact_sha256_v4(value: object, label: str) -> None:
    """Reject scalar subclasses at the v4 authorization boundary only."""

    if type(value) is not str:
        raise ValueError(f"{label} must be an exact sha256 string")
    _require_sha256(value, label)


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


@dataclass(frozen=True, slots=True)
class V4ClaimWitness:
    """Operational authorization; deliberately excluded from durable evidence."""

    attempt_id: str
    fence_identity: str
    state: str
    lifecycle_version: int
    checkpoint_sha256: str
    claim_owner_identity: str
    claim_generation: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.attempt_id, "attempt"),
            (self.fence_identity, "fence"),
            (self.state, "state"),
            (self.claim_owner_identity, "claim owner"),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"v4 {label} identity is invalid")
        if (
            type(self.lifecycle_version) is not int
            or not 0 <= self.lifecycle_version <= _MAX_INT
        ):
            raise ValueError("v4 lifecycle version is invalid")
        if (
            type(self.claim_generation) is not int
            or not 1 <= self.claim_generation <= _MAX_INT
        ):
            raise ValueError("v4 claim generation is invalid")
        _require_exact_sha256_v4(self.checkpoint_sha256, "v4 checkpoint")

    def validates(self, checkpoint: RemoteParseCheckpointV4) -> bool:
        return (
            self.attempt_id == checkpoint.attempt_id
            and self.fence_identity == checkpoint.fence_identity
            and self.state == checkpoint.state
            and self.lifecycle_version == checkpoint.lifecycle_version
            and self.checkpoint_sha256 == checkpoint.sha256
        )


@dataclass(frozen=True, slots=True)
class PrivateProviderCapabilityV4:
    """Versioned private provider capability loaded under the current claim.

    The token bytes never enter checkpoint/evidence canonical bytes.  The
    public facts are sufficient to bind a private-row reload to one attempt,
    one accepted remote task and one provider protocol without inventing a
    local signing or encryption contract.
    """

    attempt_id: str
    remote_task_identity: str
    provider_protocol_version: str
    secret_kind: str
    secret_version: int
    capability_purpose: str
    token_bytes: bytes = field(repr=False)
    token_sha256: str
    token_byte_count: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.attempt_id, "attempt"),
            (self.remote_task_identity, "remote task"),
            (self.provider_protocol_version, "provider protocol"),
            (self.secret_kind, "secret kind"),
            (self.capability_purpose, "capability purpose"),
        ):
            if type(value) is not str or not value.strip() or len(value) > 1024:
                raise ValueError(f"v4 provider {label} identity is invalid")
        if self.capability_purpose not in {
            "submitted_task_resume",
            "result_download",
            "result_acknowledgement",
        }:
            raise ValueError("v4 provider capability purpose is unsupported")
        if type(self.secret_version) is not int or self.secret_version < 1:
            raise ValueError("v4 provider secret version is invalid")
        if (
            type(self.token_bytes) is not bytes
            or not self.token_bytes
            or len(self.token_bytes) > 65_536
            or type(self.token_byte_count) is not int
            or self.token_byte_count != len(self.token_bytes)
        ):
            raise ValueError("v4 provider capability token envelope is invalid")
        _require_exact_sha256_v4(
            self.token_sha256, "v4 provider capability token"
        )
        if self.token_sha256 != (
            "sha256:" + hashlib.sha256(self.token_bytes).hexdigest()
        ):
            raise ValueError("v4 provider capability token hash drifted")

    def validates_accepted_submission(
        self, accepted: AcceptedSubmissionReceiptV4
    ) -> bool:
        return (
            type(accepted) is AcceptedSubmissionReceiptV4
            and self.attempt_id == accepted.attempt_id
            and self.remote_task_identity == accepted.remote_task_identity
            and self.provider_protocol_version
            == accepted.provider_protocol_version
            and self.secret_kind == accepted.secret_kind
            and self.secret_version == accepted.secret_version
            and self.token_sha256 == accepted.token_sha256
            and self.token_byte_count == accepted.token_byte_count
        )


@dataclass(frozen=True, slots=True)
class V4EvidenceReplayContext:
    """Exact pure-evidence replay inputs for one v4 side effect.

    This proves canonical coherence only.  It is not named or treated as
    authority: 0057 must still reload these exact rows under lock, establish
    currentness, and CAS the mutable head in the same transaction.
    """

    evidence: tuple[EncodedRemoteParseEvidenceV4, ...]
    reservation: ResourceReservationV4
    resourceful_checkpoint_history: tuple[RemoteParseCheckpointV4, ...]
    cleanup_source_checkpoint: RemoteParseCheckpointV4 | None = None
    cleanup_pending_checkpoint: RemoteParseCheckpointV4 | None = None
    ack_pending_checkpoint: RemoteParseCheckpointV4 | None = None
    superseding_checkpoint: RemoteParseCheckpointV4 | None = None
    superseding_reservation: ResourceReservationV4 | None = None
    superseding_preparation_intent: PreparationIntentV4 | None = None
    superseding_snapshot_receipt: SnapshotReceiptV4 | None = None
    local_materialization_manifest: LocalMaterializationManifestV4 | None = None
    provider_envelope: ProviderDocumentEnvelope | None = None

    def __post_init__(self) -> None:
        optional_types = (
            (self.cleanup_source_checkpoint, RemoteParseCheckpointV4),
            (self.cleanup_pending_checkpoint, RemoteParseCheckpointV4),
            (self.ack_pending_checkpoint, RemoteParseCheckpointV4),
            (self.superseding_checkpoint, RemoteParseCheckpointV4),
            (self.superseding_reservation, ResourceReservationV4),
            (self.superseding_preparation_intent, PreparationIntentV4),
            (self.superseding_snapshot_receipt, SnapshotReceiptV4),
            (self.local_materialization_manifest, LocalMaterializationManifestV4),
            (self.provider_envelope, ProviderDocumentEnvelope),
        )
        if (
            type(self.evidence) is not tuple
            or any(
                type(item) is not EncodedRemoteParseEvidenceV4
                for item in self.evidence
            )
            or type(self.reservation) is not ResourceReservationV4
            or type(self.resourceful_checkpoint_history) is not tuple
            or not self.resourceful_checkpoint_history
            or any(
                type(item) is not RemoteParseCheckpointV4
                for item in self.resourceful_checkpoint_history
            )
            or any(
                value is not None and type(value) is not expected_type
                for value, expected_type in optional_types
            )
        ):
            raise ValueError("v4 evidence replay context type drifted")

    def evidence_value(
        self,
        kind: str,
        expected_type: type[object],
    ) -> object | None:
        matches = tuple(item.value for item in self.evidence if item.kind == kind)
        if not matches:
            return None
        if len(matches) != 1 or type(matches[0]) is not expected_type:
            raise ValueError("v4 evidence replay context kind/type drifted")
        return matches[0]

    def require_evidence(self, kind: str, expected: object) -> None:
        if self.evidence_value(kind, type(expected)) != expected:
            raise ValueError("v4 evidence replay context value drifted")

    def validate_current(self, checkpoint: RemoteParseCheckpointV4) -> None:
        self._validate_current_position(checkpoint)
        validate_remote_parse_evidence_bundle_v4(
            checkpoint=checkpoint,
            evidence=self.evidence,
            reservation=self.reservation,
            cleanup_source_checkpoint=self.cleanup_source_checkpoint,
            resourceful_checkpoint_history=self.resourceful_checkpoint_history,
            cleanup_pending_checkpoint=self.cleanup_pending_checkpoint,
            # The side effect starts from pre-ACK `checkpoint`; the validator's
            # similarly named auxiliary is only for a post-ACK receipt bundle.
            ack_pending_checkpoint=None,
            superseding_checkpoint=self.superseding_checkpoint,
            superseding_reservation=self.superseding_reservation,
            superseding_preparation_intent=self.superseding_preparation_intent,
            superseding_snapshot_receipt=self.superseding_snapshot_receipt,
            local_materialization_manifest=self.local_materialization_manifest,
            provider_envelope=self.provider_envelope,
        )

    def validate_durable_current(self, checkpoint: RemoteParseCheckpointV4) -> None:
        """Replay only PostgreSQL facts before filesystem evidence is reopened."""

        self._validate_current_position(checkpoint)
        validate_durable_remote_parse_evidence_bundle_v4(
            checkpoint=checkpoint,
            evidence=self.evidence,
            reservation=self.reservation,
            cleanup_source_checkpoint=self.cleanup_source_checkpoint,
            resourceful_checkpoint_history=self.resourceful_checkpoint_history,
            cleanup_pending_checkpoint=self.cleanup_pending_checkpoint,
            ack_pending_checkpoint=None,
            superseding_checkpoint=self.superseding_checkpoint,
            superseding_reservation=self.superseding_reservation,
            superseding_preparation_intent=self.superseding_preparation_intent,
            superseding_snapshot_receipt=self.superseding_snapshot_receipt,
        )

    def _validate_current_position(
        self,
        checkpoint: RemoteParseCheckpointV4,
    ) -> None:
        if type(checkpoint) is not RemoteParseCheckpointV4 or checkpoint.state not in {
            "materializing",
            "local_materialized",
            "publish_committed",
            "cleanup_pending",
            "ack_pending",
        }:
            raise ValueError("v4 evidence replay checkpoint state is unsupported")
        if checkpoint.state in {
            "materializing",
            "local_materialized",
            "publish_committed",
        } and any(
            item is not None
            for item in (
                self.cleanup_source_checkpoint,
                self.cleanup_pending_checkpoint,
                self.ack_pending_checkpoint,
            )
        ):
            raise ValueError("materializing replay invented cleanup evidence")
        if checkpoint.state == "cleanup_pending" and any(
            item is not None
            for item in (
                self.cleanup_pending_checkpoint,
                self.ack_pending_checkpoint,
            )
        ):
            raise ValueError("cleanup replay invented post-cleanup evidence")
        if checkpoint.state == "ack_pending" and (
            self.cleanup_pending_checkpoint is None
            or self.ack_pending_checkpoint != checkpoint
        ):
            raise ValueError("ACK replay lacks its exact pending checkpoints")


class V4ClaimGuard(Protocol):
    """Revalidate DB claim/lease while the backend holds its filesystem lock."""

    def assert_current_under_resource_lock(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        claim: V4ClaimWitness,
    ) -> None:
        """Raise unless checkpoint, claim owner/generation and lease are current."""


class V4StageGuard(Protocol):
    """Cooperative monotonic deadline guard for one remote/file stage."""

    def checkpoint(self) -> None: ...

    def remaining_seconds(self) -> float: ...


def validate_v4_materialization_authorization(
    *,
    checkpoint: RemoteParseCheckpointV4,
    reservation: ResourceReservationV4,
    preparation_intent: PreparationIntentV4,
    intent: MaterializationIntentV4,
    accepted_submission: AcceptedSubmissionReceiptV4,
    terminal_receipt: TerminalReceiptV4,
    provider_capability: PrivateProviderCapabilityV4,
    claim: V4ClaimWitness,
    allowance: PerAttemptResourceAllowance,
    replay_context: V4EvidenceReplayContext,
) -> None:
    if (
        type(checkpoint) is not RemoteParseCheckpointV4
        or type(reservation) is not ResourceReservationV4
        or type(preparation_intent) is not PreparationIntentV4
        or type(intent) is not MaterializationIntentV4
        or type(accepted_submission) is not AcceptedSubmissionReceiptV4
        or type(terminal_receipt) is not TerminalReceiptV4
        or type(provider_capability) is not PrivateProviderCapabilityV4
        or type(claim) is not V4ClaimWitness
        or type(allowance) is not PerAttemptResourceAllowance
        or type(replay_context) is not V4EvidenceReplayContext
    ):
        raise ValueError("v4 materialization authorization drifted")
    if replay_context.reservation != reservation:
        raise ValueError("v4 materialization replay reservation drifted")
    replay_context.require_evidence("preparation_intent", preparation_intent)
    replay_context.require_evidence("accepted_submission", accepted_submission)
    replay_context.require_evidence("terminal_receipt", terminal_receipt)
    replay_context.require_evidence("materialization_intent", intent)
    replay_context.validate_current(checkpoint)
    reservation_input = allowance.reservation_input.value
    expected_held_resource_credit = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=reservation.source_byte_count,
        provider_tasks=1,
        provider_result_bytes=intent.artifact_byte_count,
        materialization_items=1,
        compressed_bytes=intent.artifact_byte_count,
        decoded_bytes=reservation.reserved_credit.decoded_bytes,
        temp_disk_bytes=reservation.reserved_credit.temp_disk_bytes,
        ack_items=1,
    )
    checkpoint_identity = (
        checkpoint.attempt_id,
        checkpoint.attempt_generation,
        checkpoint.fence_identity,
        checkpoint.document_id,
        checkpoint.processing_run_id,
        checkpoint.source_pdf_sha256,
        checkpoint.source_byte_count,
        checkpoint.source_page_count,
        checkpoint.request_sha256,
        checkpoint.runtime_epoch_sha256,
        checkpoint.process_profile_sha256,
        checkpoint.credit_policy_sha256,
        checkpoint.reservation_input_sha256,
    )
    reservation_identity = (
        reservation.attempt_id,
        reservation.attempt_generation,
        reservation.fence_identity,
        reservation.document_id,
        reservation.processing_run_id,
        reservation.source_pdf_sha256,
        reservation.source_byte_count,
        reservation.source_page_count,
        reservation.request_sha256,
        reservation.runtime_epoch_sha256,
        reservation.process_profile_sha256,
        reservation.credit_policy_sha256,
        reservation.reservation_input_sha256,
    )
    if (
        checkpoint.state != "materializing"
        or reservation_identity != checkpoint_identity
        or checkpoint.preparation_intent_sha256 != preparation_intent.sha256
        or preparation_intent.reservation_sha256 != reservation.sha256
        or (
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
            preparation_intent.snapshot_relpath,
            preparation_intent.snapshot_part_relpath,
            preparation_intent.snapshot_part_owner_relpath,
            preparation_intent.snapshot_lock_relpath,
        )
        != (
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
            reservation.snapshot_relpath,
            reservation.snapshot_part_relpath,
            reservation.snapshot_part_owner_relpath,
            reservation.snapshot_lock_relpath,
        )
        or preparation_intent.parser_target_sha256 != intent.parser_target_sha256
        or intent.reservation_sha256 != reservation.sha256
        or (
            intent.attempt_id,
            intent.fence_identity,
            intent.document_id,
            intent.processing_run_id,
            intent.source_pdf_sha256,
            intent.source_page_count,
            intent.snapshot_relpath,
        )
        != (
            reservation.attempt_id,
            reservation.fence_identity,
            reservation.document_id,
            reservation.processing_run_id,
            reservation.source_pdf_sha256,
            reservation.source_page_count,
            reservation.snapshot_relpath,
        )
        or checkpoint.materialization_intent_sha256 != intent.sha256
        or checkpoint.previous_checkpoint_sha256 != intent.source_checkpoint_sha256
        or checkpoint.lifecycle_version != intent.source_lifecycle_version + 1
        or checkpoint.accepted_submission_sha256 != accepted_submission.sha256
        or checkpoint.submission_intent_sha256
        != accepted_submission.submission_intent_sha256
        or accepted_submission.attempt_id != checkpoint.attempt_id
        or accepted_submission.fence_identity != checkpoint.fence_identity
        or checkpoint.terminal_receipt_sha256 != terminal_receipt.sha256
        or intent.terminal_receipt_sha256 != terminal_receipt.sha256
        or terminal_receipt.accepted_submission_receipt_sha256
        != accepted_submission.sha256
        or (
            terminal_receipt.attempt_id,
            terminal_receipt.fence_identity,
            terminal_receipt.remote_task_identity,
            terminal_receipt.result_owner_identity,
            terminal_receipt.artifact_sha256,
            terminal_receipt.artifact_byte_count,
            terminal_receipt.provider_protocol_version,
        )
        != (
            intent.attempt_id,
            intent.fence_identity,
            intent.remote_task_identity,
            intent.artifact_owner_identity,
            intent.artifact_sha256,
            intent.artifact_byte_count,
            accepted_submission.provider_protocol_version,
        )
        or not claim.validates(checkpoint)
        or not provider_capability.validates_accepted_submission(
            accepted_submission
        )
        or provider_capability.capability_purpose != "result_download"
        or provider_capability.secret_kind != intent.provider_capability_kind
        or provider_capability.token_sha256
        != intent.provider_capability_sha256
        or provider_capability.token_byte_count
        != intent.provider_capability_byte_count
        or provider_capability.attempt_id != checkpoint.attempt_id
        or provider_capability.remote_task_identity
        != intent.remote_task_identity
        or allowance.sha256 != intent.allowance_sha256
        or allowance.reservation_input_sha256
        != checkpoint.reservation_input_sha256
        or allowance.limits != reservation.reserved_credit
        or reservation_input.source_pdf_sha256 != reservation.source_pdf_sha256
        or reservation_input.source_byte_count != reservation.source_byte_count
        or reservation_input.source_page_count != reservation.source_page_count
        or reservation_input.process_profile_sha256
        != reservation.process_profile_sha256
        or reservation_input.credit_policy_sha256
        != reservation.credit_policy_sha256
        or reservation_input.bucket != reservation.reservation_bucket
        or reservation_input.reservation != reservation.reserved_credit
        or intent.held_resource_credit != expected_held_resource_credit
        or checkpoint.held_resource_credit != expected_held_resource_credit
    ):
        raise ValueError("v4 materialization authorization drifted")
    allowance.require_fits(intent.held_resource_credit)
    limits = allowance.limits
    if (
        intent.result_byte_limit > limits.provider_result_bytes
        or intent.decoded_byte_limit > limits.decoded_bytes
        or intent.temporary_disk_byte_limit > limits.temp_disk_bytes
        or intent.output_byte_limit > limits.output_bytes
        or intent.output_page_limit > limits.output_pages
        or intent.uncompressed_byte_limit
        > min(limits.temp_disk_bytes, limits.output_bytes)
        or intent.member_count_limit > limits.decoded_bytes
    ):
        raise ValueError("v4 materialization limits exceed exact allowance")


def validate_v4_cleanup_authorization(
    *,
    checkpoint: RemoteParseCheckpointV4,
    source_checkpoint: RemoteParseCheckpointV4,
    reservation: ResourceReservationV4,
    intent: MaterializationIntentV4 | None,
    local_receipt: LocalMaterializationReceiptV4 | None,
    plan: LocalCleanupPlanV4,
    claim: V4ClaimWitness,
    replay_context: V4EvidenceReplayContext,
) -> None:
    if (
        type(checkpoint) is not RemoteParseCheckpointV4
        or checkpoint.state != "cleanup_pending"
        or type(source_checkpoint) is not RemoteParseCheckpointV4
        or type(reservation) is not ResourceReservationV4
        or (intent is not None and type(intent) is not MaterializationIntentV4)
        or (
            local_receipt is not None
            and type(local_receipt) is not LocalMaterializationReceiptV4
        )
        or type(plan) is not LocalCleanupPlanV4
        or type(claim) is not V4ClaimWitness
        or type(replay_context) is not V4EvidenceReplayContext
        or replay_context.reservation != reservation
        or replay_context.cleanup_source_checkpoint != source_checkpoint
        or checkpoint.cleanup_plan_sha256 != plan.sha256
        or checkpoint.previous_checkpoint_sha256 != source_checkpoint.sha256
        or source_checkpoint.sha256 != plan.source_checkpoint_sha256
        or checkpoint.lifecycle_version != plan.source_lifecycle_version + 1
        or not claim.validates(checkpoint)
    ):
        raise ValueError("v4 cleanup authorization drifted")
    replay_context.require_evidence("cleanup_plan", plan)
    if intent is not None:
        replay_context.require_evidence("materialization_intent", intent)
    elif replay_context.evidence_value(
        "materialization_intent", MaterializationIntentV4
    ) is not None:
        raise ValueError("v4 cleanup replay invented a materialization intent")
    if local_receipt is not None:
        replay_context.require_evidence(
            "local_materialization_receipt", local_receipt
        )
    elif replay_context.evidence_value(
        "local_materialization_receipt", LocalMaterializationReceiptV4
    ) is not None:
        raise ValueError("v4 cleanup replay invented a materialization receipt")
    # Cleanup may delete the exact materialized output described by the local
    # receipt.  From this boundary onward durable evidence is authoritative;
    # requiring the filesystem materialization to remain reopenable would make
    # a successful cleanup impossible to resume after response loss.
    replay_context.validate_durable_current(checkpoint)
    validate_local_cleanup_plan_v4(
        plan=plan,
        reservation=reservation,
        source_checkpoint=source_checkpoint,
        materialization_intent=intent,
        local_receipt=local_receipt,
    )


def validate_v4_ack_authorization(
    *,
    command: ProviderAckCommandV4,
    provider_capability: PrivateProviderCapabilityV4,
    claim: V4ClaimWitness,
) -> None:
    if (
        type(command) is not ProviderAckCommandV4
        or type(provider_capability) is not PrivateProviderCapabilityV4
        or type(claim) is not V4ClaimWitness
    ):
        raise ValueError("v4 ACK authorization drifted")
    command.replay_context.validate_durable_current(
        command.ack_pending_checkpoint
    )
    exact_request = command.ack_request_exact_bytes
    expected_request_sha256 = "sha256:" + hashlib.sha256(exact_request).hexdigest()
    expected_request_identity = provider_ack_request_v4_identity(
        expected_request_sha256
    )
    if (
        not claim.validates(command.ack_pending_checkpoint)
        or provider_capability.capability_purpose
        != "result_acknowledgement"
        or not provider_capability.validates_accepted_submission(
            command.accepted_submission
        )
        or command.ack_request_exact_bytes != exact_request
        or command.ack_request_sha256 != expected_request_sha256
        or command.request_identity != expected_request_identity
    ):
        raise ValueError("v4 ACK authorization drifted")


@dataclass(frozen=True, slots=True)
class MaterializedProviderDocumentV4:
    receipt: LocalMaterializationReceiptV4
    intent: MaterializationIntentV4
    provider_envelope: ProviderDocumentEnvelope
    manifest: LocalMaterializationManifestV4

    def __post_init__(self) -> None:
        if (
            type(self.receipt) is not LocalMaterializationReceiptV4
            or type(self.intent) is not MaterializationIntentV4
            or type(self.provider_envelope) is not ProviderDocumentEnvelope
            or type(self.manifest) is not LocalMaterializationManifestV4
        ):
            raise ValueError("materialized provider read lacks exact evidence")
        receipt = self.receipt
        intent = self.intent
        envelope = self.provider_envelope
        manifest = self.manifest
        validate_materialized_provider_evidence_v4(
            intent=intent,
            receipt=receipt,
            manifest=manifest,
            provider_envelope=envelope,
        )
        context = intent.provider_envelope_context
        envelope_bytes = provider_document_envelope_to_bytes(envelope)
        envelope_sha256 = "sha256:" + hashlib.sha256(envelope_bytes).hexdigest()
        manifest_bytes = manifest.canonical_bytes
        manifest_sha256 = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        parser_target_sha256 = "sha256:" + hashlib.sha256(
            json.dumps(
                envelope.parser_target_identity.to_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if (
            (
                receipt.attempt_id,
                receipt.fence_identity,
                receipt.document_id,
                receipt.processing_run_id,
                receipt.materialization_intent_sha256,
                receipt.terminal_receipt_sha256,
                receipt.source_pdf_sha256,
                receipt.source_page_count,
                receipt.parser_target_sha256,
                receipt.spool_relpath,
                receipt.spool_sha256,
                receipt.spool_byte_count,
                receipt.output_relpath,
            )
            != (
                manifest.attempt_id,
                manifest.fence_identity,
                manifest.document_id,
                manifest.processing_run_id,
                manifest.materialization_intent_sha256,
                manifest.terminal_receipt_sha256,
                manifest.source_pdf_sha256,
                manifest.source_page_count,
                manifest.parser_target_sha256,
                manifest.spool_relpath,
                manifest.artifact_sha256,
                manifest.artifact_byte_count,
                manifest.output_relpath,
            )
            or (
                manifest.attempt_id,
                manifest.fence_identity,
                manifest.document_id,
                manifest.processing_run_id,
                manifest.materialization_intent_sha256,
                manifest.terminal_receipt_sha256,
                manifest.remote_task_identity,
                manifest.artifact_owner_identity,
                manifest.artifact_sha256,
                manifest.artifact_byte_count,
                manifest.source_pdf_sha256,
                manifest.source_page_count,
                manifest.parser_target_sha256,
                manifest.spool_relpath,
                manifest.output_relpath,
                manifest.provider_envelope_relpath,
            )
            != (
                intent.attempt_id,
                intent.fence_identity,
                intent.document_id,
                intent.processing_run_id,
                intent.sha256,
                intent.terminal_receipt_sha256,
                intent.remote_task_identity,
                intent.artifact_owner_identity,
                intent.artifact_sha256,
                intent.artifact_byte_count,
                intent.source_pdf_sha256,
                intent.source_page_count,
                intent.parser_target_sha256,
                intent.spool_relpath,
                intent.output_relpath,
                intent.provider_envelope_relpath,
            )
            or (
                envelope.document_id,
                envelope.artifact_owner_processing_run_id,
                envelope.provider,
                envelope.provider_document_id,
                envelope.source_pdf_relpath,
                envelope.input_raw_file_hash,
                envelope.source_pdf_page_count,
                parser_target_sha256,
                envelope.parser_artifact_root_relpath,
            )
            != (
                context.document_id,
                context.processing_run_id,
                context.provider,
                context.provider_document_id,
                context.source_pdf_relpath,
                context.source_pdf_sha256,
                context.source_page_count,
                context.parser_target_sha256,
                context.parser_artifact_root_relpath,
            )
        ):
            raise ValueError("materialized provider identity chain drifted")
        observations = manifest.observations
        if (
            receipt.member_count != observations.member_count
            or receipt.uncompressed_byte_count
            != observations.uncompressed_byte_count
            or receipt.decoded_byte_count != observations.decoded_byte_count
            or receipt.temporary_disk_peak_byte_count
            != observations.temporary_disk_peak_byte_count
        ):
            raise ValueError("materialized provider observations drifted")
        if (
            receipt.provider_envelope_relpath
            != manifest.provider_envelope_relpath
            or receipt.provider_envelope_sha256 != envelope_sha256
            or manifest.provider_envelope_sha256 != envelope_sha256
            or receipt.provider_envelope_byte_count != len(envelope_bytes)
            or manifest.provider_envelope_byte_count != len(envelope_bytes)
            or receipt.output_manifest_relpath
            != intent.output_manifest_relpath
            or receipt.output_manifest_sha256 != manifest_sha256
            or receipt.output_manifest_byte_count != len(manifest_bytes)
        ):
            raise ValueError("materialized provider envelope or manifest drifted")
        receipt_payload = tuple(
            (item.relpath, item.sha256, item.byte_count)
            for item in receipt.output_files
            if item.relpath != receipt.output_manifest_relpath
        )
        manifest_payload = tuple(
            (item.relpath, item.sha256, item.byte_count)
            for item in manifest.payload_files
        )
        if receipt_payload != manifest_payload or (
            receipt.output_file_count != len(manifest.payload_files) + 1
            or receipt.output_byte_count
            != observations.output_byte_count + len(manifest_bytes)
        ):
            raise ValueError("materialized provider output inventory drifted")
        parser_payload = tuple(
            (item.relpath, item.sha256, item.byte_count)
            for item in manifest.payload_files
            if item.role == "parser_artifact"
        )
        document_artifacts = tuple(
            (item.relative_path, item.sha256, item.size_bytes)
            for item in envelope.provider_document.artifacts
        )
        if parser_payload != document_artifacts:
            raise ValueError("materialized provider artifact inventory drifted")

    @property
    def provider_document(self) -> ProviderDocument:
        return self.provider_envelope.provider_document

    @property
    def artifact_root_relpath(self) -> str:
        return self.provider_envelope.parser_artifact_root_relpath


@dataclass(frozen=True, slots=True)
class ProviderAckCommandV4:
    ack_pending_checkpoint: RemoteParseCheckpointV4
    accepted_submission: AcceptedSubmissionReceiptV4
    terminal_receipt: TerminalReceiptV4 | None
    cleanup_plan: LocalCleanupPlanV4
    cleanup_receipt: LocalCleanupReceiptV4
    replay_context: V4EvidenceReplayContext

    def __post_init__(self) -> None:
        if (
            type(self.ack_pending_checkpoint) is not RemoteParseCheckpointV4
            or type(self.accepted_submission) is not AcceptedSubmissionReceiptV4
            or type(self.cleanup_plan) is not LocalCleanupPlanV4
            or type(self.cleanup_receipt) is not LocalCleanupReceiptV4
            or type(self.replay_context) is not V4EvidenceReplayContext
        ):
            raise ValueError("v4 ACK command evidence drifted")
        terminal = self.terminal_receipt
        expected_terminal_sha256 = self.cleanup_plan.terminal_receipt_sha256
        checkpoint_identity = (
            self.ack_pending_checkpoint.attempt_id,
            self.ack_pending_checkpoint.fence_identity,
            self.ack_pending_checkpoint.document_id,
            self.ack_pending_checkpoint.processing_run_id,
        )
        plan_identity = (
            self.cleanup_plan.attempt_id,
            self.cleanup_plan.fence_identity,
            self.cleanup_plan.document_id,
            self.cleanup_plan.processing_run_id,
        )
        receipt_identity = (
            self.cleanup_receipt.attempt_id,
            self.cleanup_receipt.fence_identity,
            self.cleanup_receipt.document_id,
            self.cleanup_receipt.processing_run_id,
        )
        planned_results = tuple(
            (
                item.kind,
                item.relpath,
                "absent" if item.action == "delete" else "transferred",
                item.target_owner_identity,
                item.target_relpath,
            )
            for item in self.cleanup_plan.resources
        )
        observed_results = tuple(
            (
                item.kind,
                item.relpath,
                item.disposition,
                item.target_owner_identity,
                item.target_relpath,
            )
            for item in self.cleanup_receipt.results
        )
        if (
            self.ack_pending_checkpoint.state != "ack_pending"
            or plan_identity != checkpoint_identity
            or receipt_identity != checkpoint_identity
            or self.accepted_submission.sha256
            != self.ack_pending_checkpoint.accepted_submission_sha256
            or self.cleanup_plan.sha256
            != self.ack_pending_checkpoint.cleanup_plan_sha256
            or self.cleanup_receipt.sha256
            != self.ack_pending_checkpoint.cleanup_receipt_sha256
            or self.cleanup_receipt.cleanup_plan_sha256 != self.cleanup_plan.sha256
            or self.cleanup_receipt.outcome != self.cleanup_plan.outcome
            or self.cleanup_receipt.cleanup_pending_checkpoint_sha256
            != self.ack_pending_checkpoint.previous_checkpoint_sha256
            or self.cleanup_receipt.cleanup_pending_lifecycle_version + 1
            != self.ack_pending_checkpoint.lifecycle_version
            or planned_results != observed_results
            or self.cleanup_plan.outcome == "pre_submission_failure"
            or self.cleanup_plan.remote_task_identity
            != self.accepted_submission.remote_task_identity
            or self.accepted_submission.attempt_id
            != self.ack_pending_checkpoint.attempt_id
            or self.accepted_submission.fence_identity
            != self.ack_pending_checkpoint.fence_identity
            or expected_terminal_sha256
            != self.ack_pending_checkpoint.terminal_receipt_sha256
            or (terminal is None) != (expected_terminal_sha256 is None)
        ):
            raise ValueError("v4 ACK command evidence drifted")
        if terminal is not None and (
            type(terminal) is not TerminalReceiptV4
            or terminal.sha256 != expected_terminal_sha256
            or terminal.attempt_id != self.ack_pending_checkpoint.attempt_id
            or terminal.fence_identity != self.ack_pending_checkpoint.fence_identity
            or terminal.accepted_submission_receipt_sha256
            != self.accepted_submission.sha256
            or terminal.remote_task_identity
            != self.accepted_submission.remote_task_identity
            or terminal.provider_protocol_version
            != self.accepted_submission.provider_protocol_version
        ):
            raise ValueError("v4 ACK command terminal receipt drifted")
        if self.cleanup_plan.outcome in {"success", "local_failure"} and (
            terminal is None
        ):
            raise ValueError("v4 ACK command lacks exact terminal receipt")
        if self.cleanup_plan.outcome == "remote_failure" and terminal is not None:
            raise ValueError("remote-failure ACK cannot carry a terminal receipt")
        self.replay_context.require_evidence(
            "accepted_submission", self.accepted_submission
        )
        if terminal is not None:
            self.replay_context.require_evidence("terminal_receipt", terminal)
        elif self.replay_context.evidence_value(
            "terminal_receipt", TerminalReceiptV4
        ) is not None:
            raise ValueError("remote-failure ACK replay invented a terminal receipt")
        self.replay_context.require_evidence("cleanup_plan", self.cleanup_plan)
        self.replay_context.require_evidence(
            "cleanup_receipt", self.cleanup_receipt
        )
        # ACK is deliberately after cleanup.  Its authorization must close over
        # durable evidence rather than materialized files that cleanup already
        # transferred or removed.
        self.replay_context.validate_durable_current(
            self.ack_pending_checkpoint
        )
        expected_ack_pending_credit = ResourceCreditVector(
            documents=1,
            provider_tasks=1,
            provider_result_bytes=(
                0 if terminal is None else terminal.artifact_byte_count
            ),
            ack_items=1,
        )
        if (
            self.ack_pending_checkpoint.held_resource_credit
            != expected_ack_pending_credit
        ):
            raise ValueError(
                "v4 ACK command credit drifted from exact terminal evidence"
            )

    @property
    def remote_task_identity(self) -> str:
        return self.accepted_submission.remote_task_identity

    @property
    def result_owner_identity(self) -> str | None:
        terminal = self.terminal_receipt
        return None if terminal is None else terminal.result_owner_identity

    @property
    def provider_protocol_version(self) -> str:
        return self.accepted_submission.provider_protocol_version

    @property
    def ack_request_exact_bytes(self) -> bytes:
        terminal = self.terminal_receipt
        return provider_ack_request_v4_bytes(
            accepted_submission_sha256=self.accepted_submission.sha256,
            ack_pending_checkpoint_sha256=self.ack_pending_checkpoint.sha256,
            attempt_id=self.ack_pending_checkpoint.attempt_id,
            cleanup_plan_sha256=self.cleanup_plan.sha256,
            cleanup_receipt_sha256=self.cleanup_receipt.sha256,
            document_id=self.ack_pending_checkpoint.document_id,
            fence_identity=self.ack_pending_checkpoint.fence_identity,
            outcome=self.cleanup_plan.outcome,
            processing_run_id=self.ack_pending_checkpoint.processing_run_id,
            provider_protocol_version=(
                self.accepted_submission.provider_protocol_version
            ),
            remote_task_identity=self.accepted_submission.remote_task_identity,
            result_owner_identity=(
                None if terminal is None else terminal.result_owner_identity
            ),
            terminal_receipt_sha256=(
                None if terminal is None else terminal.sha256
            ),
        )

    @property
    def ack_request_sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.ack_request_exact_bytes).hexdigest()

    @property
    def request_identity(self) -> str:
        return provider_ack_request_v4_identity(self.ack_request_sha256)


def seal_provider_ack_command_v4(
    *,
    ack_pending_checkpoint: RemoteParseCheckpointV4,
    accepted_submission: AcceptedSubmissionReceiptV4,
    terminal_receipt: TerminalReceiptV4 | None,
    cleanup_plan: LocalCleanupPlanV4,
    cleanup_receipt: LocalCleanupReceiptV4,
    replay_context: V4EvidenceReplayContext,
) -> ProviderAckCommandV4:
    """Seal one closed, deterministic provider ACK request command."""

    return ProviderAckCommandV4(
        ack_pending_checkpoint=ack_pending_checkpoint,
        accepted_submission=accepted_submission,
        terminal_receipt=terminal_receipt,
        cleanup_plan=cleanup_plan,
        cleanup_receipt=cleanup_receipt,
        replay_context=replay_context,
    )


class V4MaterializationPort(Protocol):
    """Three idempotent v4 side-effect domains; no stage/promotion API split."""

    def create_or_reconcile_snapshot_v4(
        self, *, checkpoint: RemoteParseCheckpointV4,
        reservation: ResourceReservationV4,
        preparation_intent: PreparationIntentV4,
        source_pdf: Path,
        evidence: tuple[EncodedRemoteParseEvidenceV4, ...],
        resourceful_checkpoint_history: tuple[RemoteParseCheckpointV4, ...],
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        stage_guard: V4StageGuard,
    ) -> SnapshotReceiptV4: ...

    def materialize_v4(
        self, *, checkpoint: RemoteParseCheckpointV4,
        reservation: ResourceReservationV4,
        preparation_intent: PreparationIntentV4,
        intent: MaterializationIntentV4,
        accepted_submission: AcceptedSubmissionReceiptV4,
        terminal_receipt: TerminalReceiptV4,
        provider_capability: PrivateProviderCapabilityV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        stage_guard: V4StageGuard,
        result_lease_seconds: int,
        allowance: PerAttemptResourceAllowance,
        replay_context: V4EvidenceReplayContext,
    ) -> MaterializedProviderDocumentV4: ...

    def reopen_materialized_v4(
        self, *, checkpoint: RemoteParseCheckpointV4,
        reservation: ResourceReservationV4,
        intent: MaterializationIntentV4,
        local_receipt: LocalMaterializationReceiptV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        stage_guard: V4StageGuard,
        replay_context: V4EvidenceReplayContext,
    ) -> MaterializedProviderDocumentV4: ...

    def cleanup_v4(
        self, *, checkpoint: RemoteParseCheckpointV4,
        source_checkpoint: RemoteParseCheckpointV4,
        reservation: ResourceReservationV4,
        intent: MaterializationIntentV4 | None,
        local_receipt: LocalMaterializationReceiptV4 | None,
        plan: LocalCleanupPlanV4, claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        stage_guard: V4StageGuard,
        replay_context: V4EvidenceReplayContext,
    ) -> LocalCleanupReceiptV4: ...

    def acknowledge_v4(
        self, *, command: ProviderAckCommandV4,
        provider_capability: PrivateProviderCapabilityV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        stage_guard: V4StageGuard,
    ) -> ProviderAckReceiptV4: ...


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
        secret: PrivateSubmittedTaskResume | RecoveredV3ResumeSecret,
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
    "RecoveredV3ResumeSecret",
    "PreparedMaterialization",
    "ProviderMaterializationEvidence",
    "ProviderAckCompletionWitness",
    "ProviderAckCommandV4",
    "PrivateProviderCapabilityV4",
    "seal_provider_ack_command_v4",
    "verify_provider_ack_completion_witness",
    "RemoteProviderParseHandle",
    "MaterializedProviderDocumentV4",
    "StagedProviderParserResult",
    "StagedProviderDocumentParserPort",
    "V4MaterializationPort",
    "V4ClaimGuard",
    "V4ClaimWitness",
    "V4StageGuard",
    "validate_v4_ack_authorization",
    "validate_v4_cleanup_authorization",
    "validate_v4_materialization_authorization",
    "SubmissionAcceptanceAmbiguous",
]
