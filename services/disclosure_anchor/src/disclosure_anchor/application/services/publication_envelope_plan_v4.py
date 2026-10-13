"""Whole-plan envelope measurement before the first durable publication write.

Readiness writes the preparation (with its request text), the parser tree, the
provider document, Unit snapshot and semantic receipt files, then the readiness
manifest; transaction P later writes the winner.  This pure step measures every
one of those records for one planned preparation, including a conservative
bound of the winner P can write, and refuses the first record outside the
policy with the typed capacity fact while nothing has been written yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
    PublicationEnvelopeExceededError,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactPreparationV1,
    AtomicPublicationReadinessReferenceV1,
)
from disclosure_anchor.application.contracts.publication_envelope_policy import (
    PublicationEnvelopePolicyV1,
)
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    atomic_publication_winner_byte_upper_bound_v4,
)


@dataclass(frozen=True, slots=True)
class PublicationPlanMeasurementV1:
    """Byte counts of one planned publication; counts only, never record content."""

    request_bytes: int
    preparation_bytes: int
    snapshot_bytes: int
    semantic_bytes: int
    readiness_bytes: int
    winner_upper_bound_bytes: int
    unit_count: int
    previous_unit_count: int
    policy_identity: str

    def note_scalars(self) -> dict[str, int | str | None]:
        return {
            "request_bytes": self.request_bytes,
            "preparation_bytes": self.preparation_bytes,
            "snapshot_bytes": self.snapshot_bytes,
            "semantic_bytes": self.semantic_bytes,
            "readiness_bytes": self.readiness_bytes,
            "winner_upper_bound_bytes": self.winner_upper_bound_bytes,
            "units": self.unit_count,
            "previous_units": self.previous_unit_count,
            "policy": self.policy_identity,
        }


def measure_atomic_publication_plan_v4(
    *,
    request: AtomicPublicationRequestV4,
    preparation: AtomicPublicationArtifactPreparationV1,
    preparation_byte_count: int,
    snapshot_byte_count: int,
    semantic_byte_count: int,
    readiness: AtomicPublicationReadinessReferenceV1,
    policy: PublicationEnvelopePolicyV1,
) -> PublicationPlanMeasurementV1:
    """Measure the whole plan in write order and refuse the first record outside its budget.

    Exact counts come from the encodings about to be written.  The winner
    count is an upper bound from the same projection functions transaction P
    uses, so its refusal is conservative: it does not show that the winner P
    would write is larger than the limit.  P still checks its exact winner
    inside the transaction.
    """

    if type(policy) is not PublicationEnvelopePolicyV1:
        raise TypeError("publication plan measurement requires an exact policy")
    if (
        preparation.request_sha256 != request.request_sha256
        or len(preparation.unit_bindings) != len(request.units)
    ):
        raise ValueError("publication plan measurement mixes two publications")
    exact = (
        ("request", preparation.request_byte_count),
        ("preparation", preparation_byte_count),
        ("snapshot", snapshot_byte_count),
        ("semantic", semantic_byte_count),
        ("readiness", readiness.manifest_byte_count),
    )
    for record_kind, byte_count in exact:
        fact = policy.exceeded(record_kind, byte_count)
        if fact is not None:
            raise PublicationEnvelopeExceededError(fact)
    winner_bound = atomic_publication_winner_byte_upper_bound_v4(
        request=request,
        unit_bindings=preparation.unit_bindings,
        artifact_readiness=readiness,
    )
    fact = policy.exceeded("winner", winner_bound, bound="upper_bound")
    if fact is not None:
        raise PublicationEnvelopeExceededError(fact)
    return PublicationPlanMeasurementV1(
        request_bytes=preparation.request_byte_count,
        preparation_bytes=preparation_byte_count,
        snapshot_bytes=snapshot_byte_count,
        semantic_bytes=semantic_byte_count,
        readiness_bytes=readiness.manifest_byte_count,
        winner_upper_bound_bytes=winner_bound,
        unit_count=len(request.units),
        previous_unit_count=len(request.previous_active_units),
        policy_identity=policy.identity,
    )


__all__ = [
    "PublicationPlanMeasurementV1",
    "measure_atomic_publication_plan_v4",
]
