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
    PreparationIntentV4,
    SnapshotReceiptV4,
    SupersessionReceiptV4,
    encode_remote_parse_evidence_v4,
    validate_durable_remote_parse_evidence_bundle_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    RemoteParseCheckpointV4,
    ResourceReservationV4,
)
from disclosure_anchor.application.contracts.staged_credit import (
    DatabaseLeaseSnapshot,
)
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationWinnerV4,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    V4ClaimWitness,
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


class RemoteParseV4Repository(Protocol):
    """Transaction-scoped exact-CAS authority.

    A caller that composes a row-locking method with ``append_successor`` in
    one outer transaction must acquire the exclusive ``DOC_NS`` document
    transaction lock first.
    Claim/renew/reload/rewrap never acquire that lock after taking a head lock;
    document-authority mutations own the ``DOC_NS -> head`` order.
    """

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

    def load(self, attempt_id: str) -> RemoteParseV4Authority: ...

    def load_current_for_document(
        self,
        document_id: str,
    ) -> RemoteParseV4Authority | LegacyCurrentRemoteParseAuthority | None: ...

    def create_prepared(
        self,
        creation: V4PreparedCreation,
    ) -> RemoteParseV4Authority: ...

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
    "V4ResourceFreeFailureCreation",
    "V4ResourceFreeSupersessionCreation",
    "V4SecretRevisionConflict",
    "V4SecretRewrap",
    "V4SuccessorAppend",
    "V4SuccessorNotCommitted",
    "V4SuccessorReconciliation",
    "V4SupersessionLinkAuthority",
]
