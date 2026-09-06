"""Database authority port for the recoverable remote-parse V4 lifecycle.

The aggregate returned here contains only facts that can be replayed from the
durable PostgreSQL authority.  Filesystem manifests, provider envelopes and
opened provider capabilities deliberately remain outside this port.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Protocol

from disclosure_anchor.application.contracts.provider_secret_envelope_v4 import (
    SealedProviderSecretV4,
    bind_provider_secret_v4,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    EncodedRemoteParseEvidenceV4,
    FailureReceiptV4,
    MaterializationIntentV4,
    LocalCleanupReceiptV4,
    PreparationIntentV4,
    SnapshotReceiptV4,
    SupersessionReceiptV4,
    build_preparation_intent_v4,
    encode_remote_parse_evidence_v4,
    validate_durable_remote_parse_evidence_bundle_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    RemoteParseCheckpointV4,
    ResourceReservationV4,
    build_initial_remote_parse_checkpoint_v4,
    build_resource_reservation_v4,
)
from disclosure_anchor.application.contracts.staged_credit import (
    DatabaseLeaseSnapshot,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
    StagedResourceCreditEnvelope,
)
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationWinnerV4,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    PreparedSubmissionIdentity,
    V4ClaimWitness,
)
from disclosure_anchor.application.contracts.v4_prepared_execution_spec import (
    V4PreparedExecutionSpec,
)

_MAX_INT = (1 << 63) - 1
_RECOVERY_FINAL_STATES = frozenset(
    {
        "acked",
        "remote_failed",
        "local_failed",
        "pre_submission_failed",
        "preparation_failed",
        "superseded",
    }
)


class RemoteParseV4RepositoryError(RuntimeError):
    """Base class for typed V4 persistence failures."""


class RemoteParseV4AuthorityViolation(RemoteParseV4RepositoryError):
    """Persisted rows do not reconstruct one canonical V4 authority."""


class V4HeadNotFound(RemoteParseV4RepositoryError):
    pass


class V4DocumentCurrentConflict(RemoteParseV4RepositoryError):
    pass


class V4GenerationConflict(RemoteParseV4RepositoryError):
    pass


class V4HeadStale(RemoteParseV4RepositoryError):
    pass


class V4AttemptFinal(RemoteParseV4RepositoryError):
    pass


class V4ClaimHeldByOther(RemoteParseV4RepositoryError):
    pass


class V4ClaimLost(RemoteParseV4RepositoryError):
    pass


class V4ClaimGenerationExhausted(RemoteParseV4RepositoryError):
    pass


class V4DifferentSuccessorCommitted(RemoteParseV4RepositoryError):
    pass


class V4SuccessorNotCommitted(RemoteParseV4RepositoryError):
    pass


class V4SecretRevisionConflict(RemoteParseV4RepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class RecoveryCandidate:
    """Read-only observation of one current nonfinal V4 head.

    The projection is only a recovery hint.  Claim acquisition must re-read
    the current head under the repository's durable authority.  An observed
    owned lease may already be expired, so its remaining duration may be zero
    or negative.
    """

    attempt_id: str
    state: str
    lifecycle_version: int
    claim_generation: int
    claim_owner_identity: str | None
    lease_remaining_seconds: float | None

    def __post_init__(self) -> None:
        if (
            type(self.attempt_id) is not str
            or not self.attempt_id.strip()
            or len(self.attempt_id) > 128
        ):
            raise ValueError("recovery candidate attempt identity is invalid")
        if (
            type(self.state) is not str
            or not self.state.strip()
            or len(self.state) > 64
        ):
            raise ValueError("recovery candidate state is invalid")
        if self.state in _RECOVERY_FINAL_STATES:
            raise ValueError("recovery candidate must be a nonfinal current head")
        for value, label in (
            (self.lifecycle_version, "lifecycle version"),
            (self.claim_generation, "claim generation"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"recovery candidate {label} is invalid")
        if (self.claim_owner_identity is None) != (
            self.lease_remaining_seconds is None
        ):
            raise ValueError("recovery candidate owner and lease must be paired")
        if (self.claim_owner_identity is None) != (self.claim_generation == 0):
            raise ValueError(
                "recovery candidate owner and claim generation disagree"
            )
        if self.claim_owner_identity is None and (
            self.state != "prepared" or self.lifecycle_version != 0
        ):
            raise ValueError(
                "unclaimed recovery candidate must be prepared at lifecycle version zero"
            )
        if self.claim_owner_identity is not None and (
            type(self.claim_owner_identity) is not str
            or not self.claim_owner_identity.strip()
            or len(self.claim_owner_identity) > 128
            or isinstance(self.lease_remaining_seconds, bool)
            or not isinstance(self.lease_remaining_seconds, (int, float))
            or not isfinite(self.lease_remaining_seconds)
        ):
            raise ValueError("recovery candidate claim observation is invalid")


@dataclass(frozen=True, slots=True)
class V4HeadExpectation:
    attempt_id: str
    fence_identity: str
    state: str
    lifecycle_version: int
    checkpoint_sha256: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.attempt_id, "attempt"),
            (self.fence_identity, "fence"),
            (self.state, "state"),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"v4 head {label} is invalid")
        if (
            type(self.lifecycle_version) is not int
            or not 0 <= self.lifecycle_version <= _MAX_INT
        ):
            raise ValueError("v4 head lifecycle version is invalid")
        _require_sha256(self.checkpoint_sha256, "v4 head checkpoint")

    @classmethod
    def from_authority(
        cls,
        authority: RemoteParseV4Authority,
    ) -> V4HeadExpectation:
        if type(authority) is not RemoteParseV4Authority:
            raise ValueError("v4 head expectation requires exact authority")
        return cls(
            attempt_id=authority.attempt_id,
            fence_identity=authority.fence_identity,
            state=authority.state,
            lifecycle_version=authority.lifecycle_version,
            checkpoint_sha256=authority.checkpoint_sha256,
        )


@dataclass(frozen=True, slots=True)
class V4SupersessionLinkAuthority:
    source_attempt_id: str
    source_fence_identity: str
    source_supersession_receipt_sha256: str
    superseding_attempt_id: str
    superseding_fence_identity: str
    superseding_checkpoint_sha256: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.source_attempt_id, "source attempt"),
            (self.source_fence_identity, "source fence"),
            (self.superseding_attempt_id, "superseding attempt"),
            (self.superseding_fence_identity, "superseding fence"),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"v4 supersession {label} is invalid")
        if self.source_attempt_id == self.superseding_attempt_id:
            raise ValueError("v4 supersession link self-references")
        _require_sha256(
            self.source_supersession_receipt_sha256,
            "v4 supersession receipt",
        )
        _require_sha256(
            self.superseding_checkpoint_sha256,
            "v4 superseding checkpoint",
        )


@dataclass(frozen=True, slots=True)
class LegacyCurrentRemoteParseAuthority:
    attempt_id: str
    document_id: str
    checkpoint_contract_version: int
    state: str

    def __post_init__(self) -> None:
        if (
            type(self.attempt_id) is not str
            or not self.attempt_id
            or type(self.document_id) is not str
            or not self.document_id
            or type(self.checkpoint_contract_version) is not int
            or self.checkpoint_contract_version not in {1, 2, 3}
            or type(self.state) is not str
            or not self.state
        ):
            raise ValueError("legacy current remote-parse authority is invalid")


@dataclass(frozen=True, slots=True)
class RemoteParseV4Authority:
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
    state: str
    is_current: bool
    lifecycle_version: int
    checkpoint_sha256: str
    claim_generation: int
    claim_owner_identity: str | None
    claim_lease_until: datetime | None
    checkpoint_history: tuple[RemoteParseCheckpointV4, ...]
    reservation: ResourceReservationV4 | None
    evidence: tuple[EncodedRemoteParseEvidenceV4, ...]
    publication_winner: AtomicPublicationWinnerV4 | None
    secret_history: tuple[SealedProviderSecretV4, ...]
    source_supersession_link: V4SupersessionLinkAuthority | None
    staged_by_link: V4SupersessionLinkAuthority | None
    database_lease: DatabaseLeaseSnapshot | None
    execution_spec: V4PreparedExecutionSpec | None = None

    def __post_init__(self) -> None:
        if type(self.checkpoint_history) is not tuple or not self.checkpoint_history:
            raise ValueError("v4 authority lacks exact checkpoint history")
        current = self.checkpoint_history[-1]
        if (
            current.attempt_id != self.attempt_id
            or current.fence_identity != self.fence_identity
            or current.document_id != self.document_id
            or current.processing_run_id != self.processing_run_id
            or current.attempt_generation != self.attempt_generation
            or current.state != self.state
            or current.lifecycle_version != self.lifecycle_version
            or current.sha256 != self.checkpoint_sha256
        ):
            raise ValueError("v4 authority head drifted from checkpoint history")
        if tuple(item.lifecycle_version for item in self.checkpoint_history) != tuple(
            range(self.lifecycle_version + 1)
        ):
            raise ValueError("v4 authority checkpoint history is not contiguous")
        if type(self.is_current) is not bool:
            raise ValueError("v4 authority currentness is invalid")
        if (
            type(self.claim_generation) is not int
            or not 0 <= self.claim_generation <= _MAX_INT
        ):
            raise ValueError("v4 authority claim generation is invalid")
        if (self.claim_owner_identity is None) != (self.claim_lease_until is None):
            raise ValueError("v4 authority claim owner and lease are not paired")
        if self.database_lease is not None and (
            type(self.database_lease) is not DatabaseLeaseSnapshot
            or self.claim_lease_until != self.database_lease.lease_until_utc
        ):
            raise ValueError("v4 authority database lease drifted")
        if self.execution_spec is not None and type(self.execution_spec) is not V4PreparedExecutionSpec:
            raise ValueError("v4 authority execution spec is not exact")

    @property
    def checkpoint(self) -> RemoteParseCheckpointV4:
        return self.checkpoint_history[-1]

    @property
    def claim_witness(self) -> V4ClaimWitness:
        if self.claim_owner_identity is None or self.claim_generation < 1:
            raise ValueError("v4 authority is not claimed")
        return V4ClaimWitness(
            attempt_id=self.attempt_id,
            fence_identity=self.fence_identity,
            state=self.state,
            lifecycle_version=self.lifecycle_version,
            checkpoint_sha256=self.checkpoint_sha256,
            claim_owner_identity=self.claim_owner_identity,
            claim_generation=self.claim_generation,
        )


@dataclass(frozen=True, slots=True)
class V4PreparedCreation:
    checkpoint: RemoteParseCheckpointV4
    reservation: ResourceReservationV4
    preparation_intent: PreparationIntentV4
    snapshot_receipt: SnapshotReceiptV4 | None
    parser_target_sha256: str
    client_submit_key: str
    execution_spec: V4PreparedExecutionSpec

    def __post_init__(self) -> None:
        if (
            type(self.checkpoint) is not RemoteParseCheckpointV4
            or self.checkpoint.state != "prepared"
            or self.checkpoint.lifecycle_version != 0
            or type(self.reservation) is not ResourceReservationV4
            or type(self.preparation_intent) is not PreparationIntentV4
            or (
                self.snapshot_receipt is not None
                and type(self.snapshot_receipt) is not SnapshotReceiptV4
            )
        ):
            raise ValueError("v4 prepared creation shape is invalid")
        if (
            self.checkpoint.attempt_id != self.reservation.attempt_id
            or self.checkpoint.sha256 == ""
            or self.preparation_intent.sha256
            != self.checkpoint.preparation_intent_sha256
            or (
                self.snapshot_receipt is None
                and self.checkpoint.snapshot_receipt_sha256 is not None
            )
            or (
                self.snapshot_receipt is not None
                and self.snapshot_receipt.sha256
                != self.checkpoint.snapshot_receipt_sha256
            )
            or self.preparation_intent.parser_target_sha256
            != self.parser_target_sha256
        ):
            raise ValueError("v4 prepared creation evidence drifted")
        _require_sha256(self.parser_target_sha256, "v4 parser target")
        if (
            type(self.client_submit_key) is not str
            or not self.client_submit_key.strip()
            or len(self.client_submit_key.encode("utf-8")) > 128
        ):
            raise ValueError("v4 client submit key is invalid")
        evidence = [encode_remote_parse_evidence_v4(self.preparation_intent)]
        if self.snapshot_receipt is not None:
            evidence.append(encode_remote_parse_evidence_v4(self.snapshot_receipt))
        validate_durable_remote_parse_evidence_bundle_v4(
            checkpoint=self.checkpoint,
            evidence=tuple(evidence),
            reservation=self.reservation,
            resourceful_checkpoint_history=(self.checkpoint,),
        )
        require_v4_execution_spec_binding(
            self.execution_spec, self.reservation, self.preparation_intent,
            parser_target_sha256=self.parser_target_sha256,
            client_submit_key=self.client_submit_key,
        )


@dataclass(frozen=True, slots=True)
class V4PreparedProposal:
    """Frozen generation-independent facts for one initial V4 head.

    The repository allocates the document generation while holding the
    document creation lock.  This value is deliberately data-only so no
    caller-controlled IO, clock sampling, configuration lookup, or nested
    transaction can run inside that lock.
    """

    document_id: str
    processing_run_id: str
    prepared_submission: PreparedSubmissionIdentity
    credit_envelope: StagedResourceCreditEnvelope
    execution_spec: V4PreparedExecutionSpec

    def __post_init__(self) -> None:
        for value, label in (
            (self.document_id, "document"),
            (self.processing_run_id, "processing run"),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"v4 prepared proposal {label} is invalid")
        if type(self.prepared_submission) is not PreparedSubmissionIdentity:
            raise ValueError("v4 prepared proposal submission identity is invalid")
        if type(self.credit_envelope) is not StagedResourceCreditEnvelope:
            raise ValueError("v4 prepared proposal credit envelope is invalid")
        if (type(self.execution_spec) is not V4PreparedExecutionSpec
            or self.execution_spec.prepared_submission != self.prepared_submission):
            raise ValueError("v4 prepared proposal execution spec is invalid")
        source = self.credit_envelope.reservation_input.value
        if self.prepared_submission.source_pdf_sha256 != source.source_pdf_sha256:
            raise ValueError("v4 prepared proposal source identity drifted")

    @property
    def attempt_id(self) -> str:
        return self.prepared_submission.attempt_identity

    @property
    def fence_identity(self) -> str:
        return self.prepared_submission.fence_identity

    @property
    def prepared_submission_identity_sha256(self) -> str:
        return self.prepared_submission.sha256

    @property
    def parser_target_sha256(self) -> str:
        return self.prepared_submission.parser_target_identity_sha256

    @property
    def request_sha256(self) -> str:
        return self.prepared_submission.request_sha256

    @property
    def runtime_epoch_sha256(self) -> str:
        return self.prepared_submission.runtime_bundle_identity_sha256

    @property
    def client_submit_key(self) -> str:
        return self.prepared_submission.client_submit_key


def bind_v4_prepared_proposal(
    proposal: V4PreparedProposal,
    *,
    attempt_generation: int,
) -> V4PreparedCreation:
    """Purely bind one locked document generation into a prepared H0."""

    if type(proposal) is not V4PreparedProposal:
        raise ValueError("v4 prepared proposal must be exact")
    reservation_input = proposal.credit_envelope.reservation_input
    source = reservation_input.value
    reservation = build_resource_reservation_v4(
        attempt_id=proposal.attempt_id,
        attempt_generation=attempt_generation,
        fence_identity=proposal.fence_identity,
        document_id=proposal.document_id,
        processing_run_id=proposal.processing_run_id,
        source_pdf_sha256=source.source_pdf_sha256,
        source_byte_count=source.source_byte_count,
        source_page_count=source.source_page_count,
        prepared_submission_identity_sha256=(
            proposal.prepared_submission_identity_sha256
        ),
        request_sha256=proposal.request_sha256,
        runtime_epoch_sha256=proposal.runtime_epoch_sha256,
        process_profile_sha256=proposal.credit_envelope.process_profile_sha256,
        credit_policy_sha256=proposal.credit_envelope.credit_policy_sha256,
        reservation_bucket=source.bucket,
        reservation_input_sha256=reservation_input.sha256,
        reserved_credit=proposal.credit_envelope.reservation,
    )
    preparation_intent = build_preparation_intent_v4(
        reservation=reservation,
        parser_target_sha256=proposal.parser_target_sha256,
        execution_spec_sha256=proposal.execution_spec.sha256,
        execution_spec_byte_count=proposal.execution_spec.byte_count,
    )
    checkpoint = build_initial_remote_parse_checkpoint_v4(
        reservation=reservation,
        preparation_intent_sha256=preparation_intent.sha256,
        held_resource_credit=ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=source.source_byte_count,
        ),
    )
    return V4PreparedCreation(
        checkpoint=checkpoint,
        reservation=reservation,
        preparation_intent=preparation_intent,
        snapshot_receipt=None,
        parser_target_sha256=proposal.parser_target_sha256,
        client_submit_key=proposal.client_submit_key,
        execution_spec=proposal.execution_spec,
    )


def require_v4_execution_spec_binding(
    spec: V4PreparedExecutionSpec | None,
    reservation: ResourceReservationV4,
    preparation: PreparationIntentV4,
    *,
    parser_target_sha256: str,
    client_submit_key: str,
) -> None:
    """Bind immutable control bytes to the original H0, never current settings."""
    if type(spec) is not V4PreparedExecutionSpec:
        raise ValueError("resourceful v4 authority lacks an exact execution spec")
    prepared = spec.prepared_submission
    if (
        spec.sha256 != preparation.execution_spec_sha256
        or spec.byte_count != preparation.execution_spec_byte_count
        or prepared.attempt_identity != reservation.attempt_id
        or prepared.fence_identity != reservation.fence_identity
        or prepared.source_pdf_sha256 != reservation.source_pdf_sha256
        or prepared.sha256 != reservation.prepared_submission_identity_sha256
        or prepared.request_sha256 != reservation.request_sha256
        or prepared.runtime_bundle_identity_sha256 != reservation.runtime_epoch_sha256
        or prepared.parser_target_identity_sha256 != parser_target_sha256
        or prepared.client_submit_key != client_submit_key
        or spec.process_profile_sha256 != reservation.process_profile_sha256
        or preparation.reservation_sha256 != reservation.sha256
    ):
        raise ValueError("v4 execution spec drifted from immutable H0 authority")


@dataclass(frozen=True, slots=True)
class V4ResourceFreeFailureCreation:
    checkpoint: RemoteParseCheckpointV4
    failure_receipt: FailureReceiptV4
    parser_target_sha256: str
    client_submit_key: str

    def __post_init__(self) -> None:
        if (
            type(self.checkpoint) is not RemoteParseCheckpointV4
            or self.checkpoint.state != "preparation_failed"
            or self.checkpoint.lifecycle_version != 0
            or type(self.failure_receipt) is not FailureReceiptV4
            or self.failure_receipt.outcome != "preparation_failure"
            or self.failure_receipt.sha256
            != self.checkpoint.failure_receipt_sha256
            or self.failure_receipt.attempt_id != self.checkpoint.attempt_id
            or self.failure_receipt.fence_identity
            != self.checkpoint.fence_identity
        ):
            raise ValueError("v4 resource-free failure creation drifted")
        _require_creation_head_roots(
            parser_target_sha256=self.parser_target_sha256,
            client_submit_key=self.client_submit_key,
        )
        validate_durable_remote_parse_evidence_bundle_v4(
            checkpoint=self.checkpoint,
            evidence=(encode_remote_parse_evidence_v4(self.failure_receipt),),
            reservation=None,
        )


@dataclass(frozen=True, slots=True)
class V4ResourceFreeSupersessionCreation:
    source_checkpoint: RemoteParseCheckpointV4
    supersession_receipt: SupersessionReceiptV4
    source_parser_target_sha256: str
    source_client_submit_key: str
    superseding: V4PreparedCreation

    def __post_init__(self) -> None:
        if (
            type(self.source_checkpoint) is not RemoteParseCheckpointV4
            or self.source_checkpoint.state != "superseded"
            or self.source_checkpoint.lifecycle_version != 0
            or type(self.supersession_receipt) is not SupersessionReceiptV4
            or type(self.superseding) is not V4PreparedCreation
            or self.source_checkpoint.supersession_receipt_sha256
            != self.supersession_receipt.sha256
            or self.supersession_receipt.attempt_id
            != self.source_checkpoint.attempt_id
            or self.supersession_receipt.fence_identity
            != self.source_checkpoint.fence_identity
            or self.supersession_receipt.source_document_id
            != self.source_checkpoint.document_id
            or self.supersession_receipt.source_attempt_generation
            != self.source_checkpoint.attempt_generation
            or self.supersession_receipt.superseding_attempt_id
            != self.superseding.checkpoint.attempt_id
            or self.supersession_receipt.superseding_attempt_generation
            != self.superseding.checkpoint.attempt_generation
            or self.supersession_receipt.superseding_document_id
            != self.superseding.checkpoint.document_id
            or self.supersession_receipt.superseding_checkpoint_sha256
            != self.superseding.checkpoint.sha256
        ):
            raise ValueError("v4 resource-free supersession creation drifted")
        _require_creation_head_roots(
            parser_target_sha256=self.source_parser_target_sha256,
            client_submit_key=self.source_client_submit_key,
        )
        validate_durable_remote_parse_evidence_bundle_v4(
            checkpoint=self.source_checkpoint,
            evidence=(encode_remote_parse_evidence_v4(self.supersession_receipt),),
            reservation=None,
            superseding_checkpoint=self.superseding.checkpoint,
            superseding_reservation=self.superseding.reservation,
            superseding_preparation_intent=self.superseding.preparation_intent,
            superseding_snapshot_receipt=self.superseding.snapshot_receipt,
        )


@dataclass(frozen=True, slots=True)
class V4SuccessorAppend:
    claim: V4ClaimWitness
    successor: RemoteParseCheckpointV4
    new_evidence: tuple[EncodedRemoteParseEvidenceV4, ...] = ()
    sealed_secret: SealedProviderSecretV4 | None = None
    publication_winner: AtomicPublicationWinnerV4 | None = None
    staged_superseder: V4PreparedCreation | None = None

    def __post_init__(self) -> None:
        if (
            type(self.claim) is not V4ClaimWitness
            or type(self.successor) is not RemoteParseCheckpointV4
            or type(self.new_evidence) is not tuple
            or any(
                type(item) is not EncodedRemoteParseEvidenceV4
                for item in self.new_evidence
            )
            or (
                self.sealed_secret is not None
                and type(self.sealed_secret) is not SealedProviderSecretV4
            )
            or (
                self.publication_winner is not None
                and type(self.publication_winner) is not AtomicPublicationWinnerV4
            )
            or (
                self.staged_superseder is not None
                and type(self.staged_superseder) is not V4PreparedCreation
            )
        ):
            raise ValueError("v4 successor append shape is invalid")
        # The exact predecessor bytes are loaded by the repository.  The port
        # can still close every identity carried by the witness and successor.
        if (
            self.successor.attempt_id != self.claim.attempt_id
            or self.successor.fence_identity != self.claim.fence_identity
            or self.successor.lifecycle_version != self.claim.lifecycle_version + 1
            or self.successor.previous_checkpoint_sha256
            != self.claim.checkpoint_sha256
        ):
            raise ValueError("v4 successor append drifted from claim")
        kinds = tuple(item.kind for item in self.new_evidence)
        if len(kinds) != len(set(kinds)):
            raise ValueError("v4 successor append repeats an evidence kind")
        if any(
            item.value.attempt_id != self.claim.attempt_id
            or item.value.fence_identity != self.claim.fence_identity
            or getattr(self.successor, f"{item.kind}_sha256") != item.sha256
            for item in self.new_evidence
        ):
            raise ValueError("v4 successor evidence drifted from claim")
        by_kind = {item.kind: item for item in self.new_evidence}
        accepted = by_kind.get("accepted_submission")
        if self.sealed_secret is not None:
            if (
                accepted is None
                or type(accepted.value) is not AcceptedSubmissionReceiptV4
                or self.sealed_secret.encryption_revision != 1
                or self.sealed_secret.binding
                != bind_provider_secret_v4(accepted.value)
            ):
                raise ValueError("v4 initial sealed secret drifted from acceptance")
        elif accepted is not None:
            raise ValueError("v4 accepted successor lacks its sealed secret")
        if (self.successor.state == "publish_committed") != (
            self.publication_winner is not None
        ):
            raise ValueError("v4 publication winner presence is not exact")
        if self.publication_winner is not None:
            winner = self.publication_winner
            if (
                self.successor.state != "publish_committed"
                or self.successor.publication_winner_sha256 != winner.sha256
                or winner.attempt_id != self.claim.attempt_id
                or winner.fence_identity != self.claim.fence_identity
                or winner.local_checkpoint_sha256 != self.claim.checkpoint_sha256
                or winner.lifecycle_version_before != self.claim.lifecycle_version
                or winner.lifecycle_version_after != self.successor.lifecycle_version
            ):
                raise ValueError("v4 publication winner drifted from successor")
        supersession_started = (
            self.successor.supersession_receipt_sha256 is not None
            and "supersession_receipt" in by_kind
        )
        if supersession_started != (self.staged_superseder is not None):
            raise ValueError("v4 staged superseder presence is not exact")


@dataclass(frozen=True, slots=True)
class V4SuccessorReconciliation:
    authority: RemoteParseV4Authority
    authorization_still_live: bool

    def __post_init__(self) -> None:
        if (
            type(self.authority) is not RemoteParseV4Authority
            or type(self.authorization_still_live) is not bool
        ):
            raise ValueError("v4 successor reconciliation shape is invalid")


@dataclass(frozen=True, slots=True)
class V4SecretRewrap:
    attempt_id: str
    fence_identity: str
    rewrapped: SealedProviderSecretV4

    def __post_init__(self) -> None:
        if (
            type(self.attempt_id) is not str
            or not self.attempt_id.strip()
            or type(self.fence_identity) is not str
            or not self.fence_identity.strip()
            or type(self.rewrapped) is not SealedProviderSecretV4
            or self.rewrapped.binding.attempt_id != self.attempt_id
            or self.rewrapped.binding.fence_identity != self.fence_identity
            or self.rewrapped.encryption_revision < 2
        ):
            raise ValueError("v4 secret rewrap shape is invalid")


@dataclass(frozen=True, slots=True)
class V4ExecutionSpecBackfillCandidate:
    preparation: PreparationIntentV4
    execution_spec: V4PreparedExecutionSpec | None


@dataclass(frozen=True, slots=True)
class V4HistoricalLocalResources:
    intent: MaterializationIntentV4
    cleanup_receipt: LocalCleanupReceiptV4 | None


class RemoteParseV4Repository(Protocol):
    """Transaction-scoped exact-CAS authority.

    A caller that composes a row-locking method with ``append_successor`` in
    one outer transaction must acquire the exclusive ``DOC_NS`` document
    transaction lock first.
    Claim/renew/reload/rewrap never acquire that lock after taking a head lock;
    document-authority mutations own the ``DOC_NS -> head`` order.
    """

    def list_historical_local_resources(
        self, *, after_attempt_id: str | None, limit: int,
    ) -> tuple[V4HistoricalLocalResources, ...]:
        """Exact all-intent history, including final and superseded attempts."""

    def list_execution_spec_backfill(
        self, *, after_attempt_id: str | None, limit: int,
    ) -> tuple[V4ExecutionSpecBackfillCandidate, ...]:
        """One all-history H0 page, including final/noncurrent attempts."""

    def backfill_execution_spec(
        self, *, attempt_id: str, spec: V4PreparedExecutionSpec,
    ) -> None:
        """Insert or reconcile exact legacy bytes; validate complete authority."""

    def require_execution_spec_cutover(self) -> None:
        """Require the historical spec FK validation before legacy retirement."""

    def require_legacy_execution_spec_retirable(self, spec: V4PreparedExecutionSpec) -> None:
        """Exact PG copy or no attempt at all; callers must prove old-writer drain."""

    def list_recoverable_heads(
        self,
        *,
        after_attempt_id: str | None,
        limit: int,
    ) -> tuple[RecoveryCandidate, ...]:
        """Return one side-effect-free byte-ordered page of current V4 heads.

        The query is version-scoped, so current legacy heads on other
        documents are ignored.  It must apply every eligibility predicate
        before ``LIMIT``, use one database-clock observation for the whole
        page, and never lock or mutate the returned rows.  Lease durations are
        hints only; a later claim must re-read and compare the durable head.
        Exhaustive pagination assumes the process-wide worker singleton is
        the only producer of new current V4 heads during the startup barrier.
        """

    def list_unclaimed_prepared_heads(
        self,
        *,
        after_attempt_id: str | None,
        limit: int,
    ) -> tuple[RecoveryCandidate, ...]:
        """Return one byte-ordered page of runtime-admissible V4 heads.

        This is a narrower projection over the same PostgreSQL authority, not
        a second durable queue.  Every eligibility predicate is applied before
        ``LIMIT`` so live or expired claimed rows cannot starve a newly
        activated generation-zero superseder.  The returned rows are hints;
        admission still reloads and claims the exact durable head.
        """

    def load(self, attempt_id: str) -> RemoteParseV4Authority: ...

    def load_current_for_document(
        self,
        document_id: str,
    ) -> RemoteParseV4Authority | LegacyCurrentRemoteParseAuthority | None: ...

    def create_prepared(
        self,
        creation: V4PreparedCreation,
    ) -> RemoteParseV4Authority: ...

    def create_next_prepared(
        self,
        proposal: V4PreparedProposal,
    ) -> RemoteParseV4Authority:
        """Allocate the next generation and create H0 under one chain lock."""

    def create_resource_free_failure(
        self,
        creation: V4ResourceFreeFailureCreation,
    ) -> RemoteParseV4Authority: ...

    def create_resource_free_supersession(
        self,
        creation: V4ResourceFreeSupersessionCreation,
    ) -> tuple[RemoteParseV4Authority, RemoteParseV4Authority]: ...

    def claim(
        self,
        expectation: V4HeadExpectation,
        *,
        owner_identity: str,
        lease_seconds: int,
    ) -> RemoteParseV4Authority: ...

    def renew(
        self,
        claim: V4ClaimWitness,
        *,
        lease_seconds: int,
    ) -> RemoteParseV4Authority: ...

    def reload_claimed(
        self,
        claim: V4ClaimWitness,
        *,
        lock_for_transition: bool = False,
    ) -> RemoteParseV4Authority: ...

    def append_successor(
        self,
        append: V4SuccessorAppend,
    ) -> RemoteParseV4Authority: ...

    def reconcile_successor(
        self,
        append: V4SuccessorAppend,
    ) -> V4SuccessorReconciliation: ...

    def rewrap_secret(
        self,
        rewrap: V4SecretRewrap,
    ) -> tuple[SealedProviderSecretV4, ...]: ...


def _require_sha256(value: str, label: str) -> None:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{label} is not canonical")


def _require_creation_head_roots(
    *,
    parser_target_sha256: str,
    client_submit_key: str,
) -> None:
    _require_sha256(parser_target_sha256, "v4 parser target")
    if (
        type(client_submit_key) is not str
        or not client_submit_key.strip()
        or len(client_submit_key.encode("utf-8")) > 128
    ):
        raise ValueError("v4 client submit key is invalid")


__all__ = [
    "LegacyCurrentRemoteParseAuthority",
    "RecoveryCandidate",
    "RemoteParseV4Authority",
    "RemoteParseV4AuthorityViolation",
    "RemoteParseV4Repository",
    "RemoteParseV4RepositoryError",
    "V4AttemptFinal",
    "V4ClaimGenerationExhausted",
    "V4ClaimHeldByOther",
    "V4ClaimLost",
    "V4DifferentSuccessorCommitted",
    "V4DocumentCurrentConflict",
    "V4GenerationConflict",
    "V4HeadExpectation",
    "V4HeadNotFound",
    "V4HeadStale",
    "V4PreparedCreation",
    "V4PreparedProposal",
    "V4ResourceFreeFailureCreation",
    "V4ResourceFreeSupersessionCreation",
    "V4SecretRevisionConflict",
    "V4SecretRewrap",
    "V4SuccessorAppend",
    "V4SuccessorNotCommitted",
    "V4SuccessorReconciliation",
    "V4SupersessionLinkAuthority",
    "bind_v4_prepared_proposal",
]
