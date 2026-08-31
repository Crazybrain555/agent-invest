"""Checkpoint-v4 credits for real, reconstructible staged-parse resources.

The historical :mod:`staged_credit` v1 contract remains unchanged so every
0056/v3 row stays exactly decodable. This parallel v2 policy counts only
provider or filesystem resources that an attempt can reconstruct and prove.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
import hashlib
import json
from types import MappingProxyType
from typing import Literal

from disclosure_anchor.application.contracts.mineru_process_profile import (
    MineruProcessProfile,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads


STAGED_RESOURCE_CREDIT_POLICY_CONTRACT = "staged-resource-credit-policy.v2"
STAGED_RESOURCE_RESERVATION_INPUT_CONTRACT = (
    "staged-resource-credit-reservation-input.v2"
)
_MAX_INT = (1 << 63) - 1
_MAX_CANONICAL_BYTES = 64 * 1024
_SHA_PREFIX = "sha256:"
ResourceCreditBucket = Literal["regular", "heavy", "huge"]
CleanupOutcome = Literal[
    "success",
    "remote_failure",
    "local_failure",
    "pre_submission_failure",
    "superseded",
]


_BUCKETS: tuple[tuple[ResourceCreditBucket, int, int, int, int], ...] = (
    ("regular", 1, 8, 1, 2),
    ("heavy", 1, 2, 2, 3),
    ("huge", 1, 1, 4, 4),
)
_RESERVATION_MECHANICS = MappingProxyType(
    {
        "ack_items": "literal:1",
        "compressed_bytes": "provider_result_bytes",
        "decoded_bytes": "min(profile.decoded_payload_bytes_limit,source_page_count*ceil(profile.rasterized_page_bytes_limit/profile.resident_pages_limit))",
        "documents": "literal:1",
        "materialization_items": "literal:1",
        "output_bytes": "min(profile.temporary_disk_bytes_limit,provider_result_bytes*bucket.temp_expansion_multiplier)",
        "output_items": "literal:1",
        "output_pages": "source_page_count",
        "provider_result_bytes": "min(profile.result_reservation_bytes*bucket.result_reservation_multiplier,profile.terminal_output_bytes_limit,profile.max_unacked_result_bytes)",
        "provider_tasks": "literal:1",
        "remote_waits": "literal:1",
        "snapshot_bytes": "source_byte_count",
        "snapshot_items": "literal:1",
        "temp_disk_bytes": "min(profile.temporary_disk_bytes_limit,provider_result_bytes+output_bytes)",
    }
)

STAGED_RESOURCE_STATE_TRANSITIONS = MappingProxyType(
    {
        "prepared": frozenset({"reconciling", "cleanup_pending"}),
        "reconciling": frozenset({"submitted", "cleanup_pending"}),
        "submitted": frozenset({"remote_terminal", "cleanup_pending"}),
        "remote_terminal": frozenset({"materializing", "cleanup_pending"}),
        "materializing": frozenset({"local_materialized", "cleanup_pending"}),
        "local_materialized": frozenset({"publish_committed", "cleanup_pending"}),
        "publish_committed": frozenset({"cleanup_pending"}),
        "cleanup_pending": frozenset(
            {"ack_pending", "pre_submission_failed", "superseded"}
        ),
        "ack_pending": frozenset(
            {"acked", "remote_failed", "local_failed", "superseded"}
        ),
    }
)
_FINAL_STATES = frozenset(
    {
        "acked",
        "local_failed",
        "pre_submission_failed",
        "preparation_failed",
        "remote_failed",
        "superseded",
    }
)


@dataclass(frozen=True, slots=True)
class ResourceCreditVector:
    documents: int = 0
    snapshot_items: int = 0
    snapshot_bytes: int = 0
    remote_waits: int = 0
    provider_tasks: int = 0
    provider_result_bytes: int = 0
    materialization_items: int = 0
    compressed_bytes: int = 0
    decoded_bytes: int = 0
    temp_disk_bytes: int = 0
    output_items: int = 0
    output_bytes: int = 0
    output_pages: int = 0
    ack_items: int = 0

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_INT
            ):
                raise ValueError(
                    f"resource credit {item.name} must be a non-negative "
                    "bounded integer"
                )

    def __add__(self, other: ResourceCreditVector) -> ResourceCreditVector:
        return ResourceCreditVector(
            **{
                item.name: _checked_add(
                    getattr(self, item.name), getattr(other, item.name)
                )
                for item in fields(self)
            }
        )

    def __sub__(self, other: ResourceCreditVector) -> ResourceCreditVector:
        values = {
            item.name: getattr(self, item.name) - getattr(other, item.name)
            for item in fields(self)
        }
        if any(value < 0 for value in values.values()):
            raise ValueError("resource credit release would make ownership negative")
        return ResourceCreditVector(**values)

    def fits(self, limit: ResourceCreditVector) -> bool:
        return all(
            getattr(self, item.name) <= getattr(limit, item.name)
            for item in fields(self)
        )

    def nonzero(self) -> dict[str, int]:
        return {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if getattr(self, item.name)
        }


@dataclass(frozen=True, slots=True)
class StagedResourceCreditPolicy:
    contract_version: str = STAGED_RESOURCE_CREDIT_POLICY_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != STAGED_RESOURCE_CREDIT_POLICY_CONTRACT:
            raise ValueError("staged resource credit policy contract is unsupported")

    @property
    def exact_bytes(self) -> bytes:
        payload = {
            "contract_version": self.contract_version,
            "credit_dimensions": [item.name for item in fields(ResourceCreditVector)],
            "max_integer": _MAX_INT,
            "ordered_bucket_selection": [
                {
                    "bucket": name,
                    "source_fraction_denominator": denominator,
                    "source_fraction_numerator": numerator,
                    "result_reservation_multiplier": result_multiplier,
                    "temp_expansion_multiplier": temp_multiplier,
                }
                for name, numerator, denominator, result_multiplier, temp_multiplier
                in _BUCKETS
            ],
            "reservation_mechanics": dict(_RESERVATION_MECHANICS),
            "reader_policy": {
                "full_tree_copy": "forbidden",
                "read_source": "verified staged-or-output tree under the materialization lock",
                "byte_limit": "output_bytes",
            },
            "state_mechanics": {
                "ack_pending": "local cleanup is durable; only provider task/result/ACK ownership remains",
                "cleanup_pending": "exact outcome variant from persisted cleanup plan and local resource manifest",
                "local_materialized": "snapshot + provider result + closed output-file manifest",
                "materializing": "snapshot + provider result + deterministic spool/staging plan",
                "prepared": "whole-PDF upload snapshot",
                "publish_committed": "database commit is durable; snapshot and output files remain owned until verified cleanup",
                "reconciling": "snapshot + remote wait",
                "remote_terminal": "snapshot + retained provider result",
                "submitted": "snapshot + remote wait + provider task",
            },
            "state_transitions": {
                state: sorted(next_states)
                for state, next_states in sorted(
                    STAGED_RESOURCE_STATE_TRANSITIONS.items()
                )
            },
        }
        return _canonical_json(payload)

    @property
    def sha256(self) -> str:
        return _sha256(self.exact_bytes)


STAGED_RESOURCE_CREDIT_POLICY_V2 = StagedResourceCreditPolicy()


@dataclass(frozen=True, slots=True)
class ResourceReservationInput:
    source_pdf_sha256: str
    source_byte_count: int
    source_page_count: int
    process_profile_sha256: str
    credit_policy_sha256: str
    bucket: ResourceCreditBucket
    reservation: ResourceCreditVector
    contract_version: str = STAGED_RESOURCE_RESERVATION_INPUT_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != STAGED_RESOURCE_RESERVATION_INPUT_CONTRACT:
            raise ValueError("resource reservation input contract is unsupported")
        for value, label in (
            (self.source_pdf_sha256, "source PDF"),
            (self.process_profile_sha256, "process profile"),
            (self.credit_policy_sha256, "credit policy"),
        ):
            _require_sha(value, label)
        _require_positive_int(self.source_byte_count, "source byte count")
        _require_positive_int(self.source_page_count, "source page count")
        if self.bucket not in {item[0] for item in _BUCKETS}:
            raise ValueError("resource reservation bucket is unsupported")
        if type(self.reservation) is not ResourceCreditVector:
            raise ValueError("resource reservation requires an exact credit vector")


@dataclass(frozen=True, slots=True)
class EncodedResourceReservationInput:
    value: ResourceReservationInput
    exact_bytes: bytes = field(repr=False)
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        if type(self.exact_bytes) is not bytes or not (
            1 <= len(self.exact_bytes) <= _MAX_CANONICAL_BYTES
        ):
            raise ValueError("resource reservation bytes are outside the envelope")
        if isinstance(self.byte_count, bool) or self.byte_count != len(
            self.exact_bytes
        ):
            raise ValueError("resource reservation byte count drifted")
        if self.sha256 != _sha256(self.exact_bytes):
            raise ValueError("resource reservation hash drifted")
        if self.exact_bytes != _canonical_json(asdict(self.value)):
            raise ValueError("resource reservation projection drifted")


@dataclass(frozen=True, slots=True)
class StagedResourceCreditEnvelope:
    process_profile_sha256: str
    credit_policy_sha256: str
    reservation_input: EncodedResourceReservationInput
    reservation: ResourceCreditVector

    def __post_init__(self) -> None:
        if type(self.reservation_input) is not EncodedResourceReservationInput:
            raise ValueError("resource envelope requires exact reservation input")
        if type(self.reservation) is not ResourceCreditVector:
            raise ValueError("resource envelope requires an exact credit vector")
        if (
            self.process_profile_sha256
            != self.reservation_input.value.process_profile_sha256
            or self.credit_policy_sha256
            != self.reservation_input.value.credit_policy_sha256
            or self.reservation != self.reservation_input.value.reservation
        ):
            raise ValueError("resource envelope identity drifted")


@dataclass(frozen=True, slots=True)
class PerAttemptResourceAllowance:
    """Immutable hard ceilings which adapters must enforce before every write.

    The allowance is deliberately the same closed vector as the durable
    reservation.  Keeping a second set of byte ceilings would permit the
    scheduler and filesystem adapter to disagree about the same attempt.
    """

    reservation_input_sha256: str
    reservation_input: EncodedResourceReservationInput = field(repr=False)
    limits: ResourceCreditVector

    def __post_init__(self) -> None:
        _require_sha(self.reservation_input_sha256, "reservation input")
        if type(self.reservation_input) is not EncodedResourceReservationInput:
            raise ValueError("attempt allowance requires exact reservation input")
        if type(self.limits) is not ResourceCreditVector:
            raise ValueError("attempt allowance requires an exact credit vector")
        if (
            self.reservation_input_sha256 != self.reservation_input.sha256
            or self.limits != self.reservation_input.value.reservation
        ):
            raise ValueError("attempt allowance drifted from reservation input")

    def require_fits(self, observed: ResourceCreditVector) -> None:
        if type(observed) is not ResourceCreditVector or not observed.fits(self.limits):
            raise ValueError("observed resource use exceeds the per-attempt allowance")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(
            {
                "limits": asdict(self.limits),
                "reservation_input_sha256": self.reservation_input_sha256,
            }
        )

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes)


def per_attempt_resource_allowance(
    envelope: StagedResourceCreditEnvelope,
) -> PerAttemptResourceAllowance:
    """Return the sole download/inspect/extract/output hard allowance."""

    if type(envelope) is not StagedResourceCreditEnvelope:
        raise ValueError("attempt allowance requires an exact credit envelope")
    return PerAttemptResourceAllowance(
        reservation_input_sha256=envelope.reservation_input.sha256,
        reservation_input=envelope.reservation_input,
        limits=envelope.reservation,
    )


@dataclass(frozen=True, slots=True)
class ResourceCreditFacts:
    snapshot_byte_count: int = 0
    provider_task_retained: bool = False
    provider_result_byte_count: int = 0
    compressed_byte_count: int = 0
    uncompressed_byte_count: int = 0
    decoded_byte_count: int = 0
    temporary_disk_byte_count: int = 0
    output_artifact_byte_count: int = 0
    source_page_count: int = 0
    materialization_prepared: bool = False
    local_materialization_completed: bool = False
    cleanup_outcome: CleanupOutcome | None = None
    resources_cleaned: bool = False
    publication_committed: bool = False
    reservation_input: EncodedResourceReservationInput | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name in {
                "provider_task_retained",
                "materialization_prepared",
                "local_materialization_completed",
                "resources_cleaned",
                "publication_committed",
            }:
                if type(value) is not bool:
                    raise ValueError("resource ownership fact must be boolean")
            elif item.name == "cleanup_outcome":
                if value not in {
                    None,
                    "success",
                    "remote_failure",
                    "local_failure",
                    "pre_submission_failure",
                    "superseded",
                }:
                    raise ValueError("resource cleanup outcome is unsupported")
            elif item.name == "reservation_input":
                if value is not None and type(value) is not EncodedResourceReservationInput:
                    raise ValueError(
                        "resource facts require an exact reservation input"
                    )
            elif (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_INT
            ):
                raise ValueError(f"resource credit fact {item.name} is invalid")
        if self.local_materialization_completed and not self.materialization_prepared:
            raise ValueError("completed local materialization requires prepared facts")
        if self.publication_committed and not self.local_materialization_completed:
            raise ValueError("publication commit requires completed local output")
        if self.resources_cleaned and self.cleanup_outcome is None:
            raise ValueError("cleaned resources require an exact cleanup outcome")


def resource_credit_shape(
    state: str, facts: ResourceCreditFacts
) -> ResourceCreditVector:
    """Project persisted evidence into exact currently-owned resources."""

    if state == "prepared":
        _require_snapshot_only(facts)
        return _snapshot_vector(facts)
    if state == "reconciling":
        _require_snapshot_only(facts)
        return _snapshot_vector(facts) + ResourceCreditVector(remote_waits=1)
    if state == "submitted":
        _require_snapshot(facts)
        _require_provider_task(facts)
        if facts.provider_result_byte_count:
            raise ValueError("submitted state cannot retain provider result bytes")
        _require_no_materialization(facts)
        return (
            _snapshot_vector(facts)
            + _provider_vector(facts)
            + ResourceCreditVector(remote_waits=1)
        )
    if state == "remote_terminal":
        _require_snapshot(facts)
        _require_provider_result(facts)
        _require_no_materialization(facts)
        return _snapshot_vector(facts) + _provider_vector(facts)
    if state == "materializing":
        _require_snapshot(facts)
        _require_provider_result(facts)
        _require_materializing(facts)
        return (
            _snapshot_vector(facts)
            + _provider_vector(facts)
            + _materializing_vector(facts)
        )
    if state == "local_materialized":
        _require_snapshot(facts)
        _require_provider_result(facts)
        _require_local_output(facts)
        if facts.publication_committed:
            raise ValueError("local output was marked committed before publication")
        return (
            _snapshot_vector(facts)
            + _provider_vector(facts)
            + _output_vector(facts)
        )
    if state == "publish_committed":
        _require_snapshot(facts)
        _require_provider_result(facts)
        _require_local_output(facts)
        if not facts.publication_committed:
            raise ValueError("publication state lacks an exact durable commit")
        return (
            _snapshot_vector(facts)
            + _provider_vector(facts)
            + _output_vector(facts)
        )
    if state == "cleanup_pending":
        return _cleanup_pending_vector(facts)
    if state == "ack_pending":
        return _ack_pending_vector(facts)
    if state in _FINAL_STATES:
        if state == "preparation_failed":
            if facts != ResourceCreditFacts():
                raise ValueError("preparation failure cannot own resources")
            return ResourceCreditVector()
        if state == "superseded" and facts == ResourceCreditFacts():
            return ResourceCreditVector()
        expected_outcome = {
            "acked": "success",
            "remote_failed": "remote_failure",
            "local_failed": "local_failure",
            "pre_submission_failed": "pre_submission_failure",
            "superseded": "superseded",
        }[state]
        if facts.cleanup_outcome != expected_outcome:
            raise ValueError("terminal checkpoint cleanup outcome drifted")
        if not facts.resources_cleaned:
            raise ValueError("terminal checkpoint still owns local resources")
        _cleanup_pending_vector(replace(facts, resources_cleaned=False))
        return ResourceCreditVector()
    raise ValueError("resource credit state is unsupported")


def encode_resource_reservation_input(
    value: ResourceReservationInput,
) -> EncodedResourceReservationInput:
    exact = _canonical_json(asdict(value))
    return EncodedResourceReservationInput(
        value=value,
        exact_bytes=exact,
        sha256=_sha256(exact),
        byte_count=len(exact),
    )


def decode_resource_reservation_input(
    exact_bytes: bytes,
) -> EncodedResourceReservationInput:
    if type(exact_bytes) is not bytes or not (
        1 <= len(exact_bytes) <= _MAX_CANONICAL_BYTES
    ):
        raise ValueError("resource reservation bytes are outside the envelope")
    decoded = strict_json_loads(exact_bytes)
    expected = {item.name for item in fields(ResourceReservationInput)}
    if not isinstance(decoded, dict) or set(decoded) != expected:
        raise ValueError("resource reservation fields are not closed")
    nested = decoded.get("reservation")
    if not isinstance(nested, dict) or set(nested) != {
        item.name for item in fields(ResourceCreditVector)
    }:
        raise ValueError("resource reservation credit vector is not closed")
    value = ResourceReservationInput(
        **{**decoded, "reservation": ResourceCreditVector(**nested)}
    )
    encoded = encode_resource_reservation_input(value)
    if encoded.exact_bytes != exact_bytes:
        raise ValueError("resource reservation JSON is not canonical")
    return encoded


def build_staged_resource_credit_envelope(
    *,
    profile: MineruProcessProfile,
    source_pdf_sha256: str,
    source_byte_count: int,
    source_page_count: int,
    policy: StagedResourceCreditPolicy = STAGED_RESOURCE_CREDIT_POLICY_V2,
) -> StagedResourceCreditEnvelope:
    _require_sha(source_pdf_sha256, "source PDF")
    _require_positive_int(source_byte_count, "source byte count")
    _require_positive_int(source_page_count, "source page count")
    page_ceiling = profile.unpublished_pages_limit
    if (
        source_byte_count > profile.source_pdf_bytes_limit
        or source_page_count > page_ceiling
    ):
        raise ValueError("source facts exceed process profile global ceilings")
    selected: tuple[ResourceCreditBucket, int, int, int, int] | None = None
    for candidate in _BUCKETS:
        _, numerator, denominator, _, _ = candidate
        if (
            source_byte_count
            <= _floor_fraction(
                profile.source_pdf_bytes_limit, numerator, denominator
            )
            and source_page_count
            <= _floor_fraction(page_ceiling, numerator, denominator)
        ):
            selected = candidate
            break
    if selected is None:
        raise ValueError("source facts do not fit a resource credit bucket")
    bucket, _, _, result_multiplier, temp_multiplier = selected
    provider_cap = _capped_mul(
        profile.result_reservation_bytes,
        result_multiplier,
        min(profile.terminal_output_bytes_limit, profile.max_unacked_result_bytes),
    )
    raster_per_page = _ceil_div(
        profile.rasterized_page_bytes_limit, profile.resident_pages_limit
    )
    decoded_cap = min(
        profile.decoded_payload_bytes_limit,
        _capped_mul(
            source_page_count,
            raster_per_page,
            profile.decoded_payload_bytes_limit,
        ),
    )
    output_cap = min(
        profile.temporary_disk_bytes_limit,
        _capped_mul(
            provider_cap,
            temp_multiplier,
            profile.temporary_disk_bytes_limit,
        ),
    )
    temp_cap = min(
        profile.temporary_disk_bytes_limit,
        _capped_add(provider_cap, output_cap, profile.temporary_disk_bytes_limit),
    )
    reservation = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=source_byte_count,
        remote_waits=1,
        provider_tasks=1,
        provider_result_bytes=provider_cap,
        materialization_items=1,
        compressed_bytes=provider_cap,
        decoded_bytes=decoded_cap,
        temp_disk_bytes=temp_cap,
        output_items=1,
        output_bytes=output_cap,
        output_pages=source_page_count,
        ack_items=1,
    )
    reservation_input = encode_resource_reservation_input(
        ResourceReservationInput(
            source_pdf_sha256=source_pdf_sha256,
            source_byte_count=source_byte_count,
            source_page_count=source_page_count,
            process_profile_sha256=profile.sha256,
            credit_policy_sha256=policy.sha256,
            bucket=bucket,
            reservation=reservation,
        )
    )
    return StagedResourceCreditEnvelope(
        process_profile_sha256=profile.sha256,
        credit_policy_sha256=policy.sha256,
        reservation_input=reservation_input,
        reservation=reservation,
    )


def validate_staged_resource_credit_envelope(
    envelope: StagedResourceCreditEnvelope,
    *,
    profile: MineruProcessProfile,
    policy: StagedResourceCreditPolicy = STAGED_RESOURCE_CREDIT_POLICY_V2,
) -> None:
    rebuilt = build_staged_resource_credit_envelope(
        profile=profile,
        source_pdf_sha256=envelope.reservation_input.value.source_pdf_sha256,
        source_byte_count=envelope.reservation_input.value.source_byte_count,
        source_page_count=envelope.reservation_input.value.source_page_count,
        policy=policy,
    )
    if rebuilt != envelope:
        raise ValueError("staged resource credit envelope drifted")


def _snapshot_vector(facts: ResourceCreditFacts) -> ResourceCreditVector:
    return ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=facts.snapshot_byte_count,
    )


def _provider_vector(facts: ResourceCreditFacts) -> ResourceCreditVector:
    return ResourceCreditVector(
        provider_tasks=1,
        provider_result_bytes=facts.provider_result_byte_count,
        ack_items=1,
    )


def _materializing_vector(facts: ResourceCreditFacts) -> ResourceCreditVector:
    reservation_input = facts.reservation_input
    if reservation_input is None:
        raise ValueError("materializing state lacks exact reservation input")
    reservation = reservation_input.value.reservation
    return ResourceCreditVector(
        materialization_items=1,
        compressed_bytes=facts.compressed_byte_count,
        decoded_bytes=reservation.decoded_bytes,
        temp_disk_bytes=reservation.temp_disk_bytes,
    )


def _output_vector(facts: ResourceCreditFacts) -> ResourceCreditVector:
    return ResourceCreditVector(
        compressed_bytes=facts.provider_result_byte_count,
        output_items=1,
        output_bytes=facts.output_artifact_byte_count,
        output_pages=facts.source_page_count,
    )


def _cleanup_pending_vector(facts: ResourceCreditFacts) -> ResourceCreditVector:
    _require_snapshot(facts)
    if facts.resources_cleaned or facts.cleanup_outcome is None:
        raise ValueError("cleanup-pending state lacks an uncommitted cleanup plan")
    outcome = facts.cleanup_outcome
    if outcome == "pre_submission_failure":
        _require_snapshot_only(facts, allow_cleanup=True)
        return _snapshot_vector(facts)
    if outcome == "remote_failure":
        _require_provider_task(facts)
        if facts.provider_result_byte_count:
            raise ValueError("remote failure cannot retain provider result bytes")
        _require_no_materialization(facts, allow_cleanup=True)
        return _snapshot_vector(facts) + _provider_vector(facts)
    if outcome == "success":
        _require_provider_result(facts)
        _require_local_output(facts, allow_cleanup=True)
        if not facts.publication_committed:
            raise ValueError("successful cleanup lacks a durable publication commit")
        return (
            _snapshot_vector(facts)
            + _provider_vector(facts)
            + _output_vector(facts)
        )
    if outcome == "local_failure":
        _require_provider_result(facts)
        if facts.publication_committed:
            raise ValueError("local-failure cleanup cannot drain a committed publication")
        base = _snapshot_vector(facts) + _provider_vector(facts)
        if facts.local_materialization_completed:
            _require_local_output(facts, allow_cleanup=True)
            return base + _output_vector(facts)
        if facts.materialization_prepared:
            _require_materializing(facts, allow_cleanup=True)
            return base + _materializing_vector(facts)
        _require_no_materialization(facts, allow_cleanup=True)
        return base
    if outcome == "superseded":
        if facts.publication_committed:
            raise ValueError("superseded cleanup cannot drain a committed publication")
        if not facts.provider_task_retained:
            _require_snapshot_only(facts, allow_cleanup=True)
            return _snapshot_vector(facts)
        base = _snapshot_vector(facts) + _provider_vector(facts)
        if facts.provider_result_byte_count < 1:
            _require_no_materialization(facts, allow_cleanup=True)
            return base
        if facts.local_materialization_completed:
            _require_local_output(facts, allow_cleanup=True)
            return base + _output_vector(facts)
        if facts.materialization_prepared:
            _require_materializing(facts, allow_cleanup=True)
            return base + _materializing_vector(facts)
        _require_no_materialization(facts, allow_cleanup=True)
        return base
    raise ValueError("cleanup-pending outcome is unsupported")


def _ack_pending_vector(facts: ResourceCreditFacts) -> ResourceCreditVector:
    if not facts.resources_cleaned or facts.cleanup_outcome is None:
        raise ValueError("ack-pending state lacks exact cleanup evidence")
    if facts.cleanup_outcome == "success" and not facts.publication_committed:
        raise ValueError("successful cleanup lost publication commit evidence")
    if facts.cleanup_outcome == "pre_submission_failure":
        raise ValueError("pre-submission cleanup cannot enter ack_pending")
    _cleanup_pending_vector(replace(facts, resources_cleaned=False))
    _require_provider_task(facts)
    return ResourceCreditVector(
        documents=1,
        provider_tasks=1,
        provider_result_bytes=facts.provider_result_byte_count,
        ack_items=1,
    )


def _require_snapshot(facts: ResourceCreditFacts) -> None:
    if facts.snapshot_byte_count < 1:
        raise ValueError("resource shape requires the whole-PDF snapshot")


def _require_snapshot_only(
    facts: ResourceCreditFacts, *, allow_cleanup: bool = False
) -> None:
    _require_snapshot(facts)
    expected = ResourceCreditFacts(
        snapshot_byte_count=facts.snapshot_byte_count,
        cleanup_outcome=facts.cleanup_outcome if allow_cleanup else None,
    )
    if facts != expected:
        raise ValueError("snapshot-only state has incompatible resource facts")


def _require_provider_task(facts: ResourceCreditFacts) -> None:
    if not facts.provider_task_retained:
        raise ValueError("resource shape requires a retained provider task")


def _require_provider_result(facts: ResourceCreditFacts) -> None:
    _require_provider_task(facts)
    if facts.provider_result_byte_count < 1:
        raise ValueError("resource shape requires retained provider result bytes")


def _require_no_materialization(
    facts: ResourceCreditFacts, *, allow_cleanup: bool = False
) -> None:
    if (
        facts.materialization_prepared
        or facts.local_materialization_completed
        or facts.compressed_byte_count
        or facts.uncompressed_byte_count
        or facts.decoded_byte_count
        or facts.temporary_disk_byte_count
        or facts.output_artifact_byte_count
        or facts.source_page_count
        or facts.publication_committed
        or facts.resources_cleaned
        or facts.reservation_input is not None
        or (facts.cleanup_outcome is not None and not allow_cleanup)
    ):
        raise ValueError("resource state has incompatible materialization facts")


def _require_materializing(
    facts: ResourceCreditFacts, *, allow_cleanup: bool = False
) -> None:
    reservation_input = facts.reservation_input
    if (
        not facts.materialization_prepared
        or facts.local_materialization_completed
        or facts.compressed_byte_count != facts.provider_result_byte_count
        or facts.uncompressed_byte_count < 1
        or facts.decoded_byte_count < 1
        or facts.source_page_count < 1
        or facts.temporary_disk_byte_count
        != _checked_add(
            facts.compressed_byte_count, facts.uncompressed_byte_count
        )
        or facts.output_artifact_byte_count
        or facts.publication_committed
        or facts.resources_cleaned
        or reservation_input is None
        or (facts.cleanup_outcome is not None and not allow_cleanup)
    ):
        raise ValueError("resource shape lacks exact deterministic staging facts")
    reservation_value = reservation_input.value
    reservation = reservation_value.reservation
    observed = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=facts.snapshot_byte_count,
        provider_tasks=1,
        provider_result_bytes=facts.provider_result_byte_count,
        materialization_items=1,
        compressed_bytes=facts.compressed_byte_count,
        decoded_bytes=facts.decoded_byte_count,
        temp_disk_bytes=facts.temporary_disk_byte_count,
        ack_items=1,
    )
    if (
        reservation_value.source_byte_count != facts.snapshot_byte_count
        or reservation_value.source_page_count != facts.source_page_count
        or reservation.output_pages != facts.source_page_count
        or not observed.fits(reservation)
    ):
        raise ValueError("materializing observations drifted from reservation input")


def _require_local_output(
    facts: ResourceCreditFacts, *, allow_cleanup: bool = False
) -> None:
    if (
        not facts.materialization_prepared
        or not facts.local_materialization_completed
        or facts.compressed_byte_count != facts.provider_result_byte_count
        or facts.uncompressed_byte_count
        or facts.decoded_byte_count
        or facts.temporary_disk_byte_count
        or facts.output_artifact_byte_count < 1
        or facts.source_page_count < 1
        or facts.resources_cleaned
        or facts.reservation_input is not None
        or (facts.cleanup_outcome is not None and not allow_cleanup)
    ):
        raise ValueError("resource shape lacks the closed output-file manifest")


def _checked_add(left: int, right: int) -> int:
    result = left + right
    if result > _MAX_INT:
        raise ValueError("resource credit integer arithmetic overflowed")
    return result


def _capped_mul(value: int, multiplier: int, cap: int) -> int:
    if cap == 0 or value > cap // multiplier:
        return cap
    return value * multiplier


def _capped_add(left: int, right: int, cap: int) -> int:
    if left >= cap or right > cap - left:
        return cap
    return left + right


def _floor_fraction(value: int, numerator: int, denominator: int) -> int:
    return value // denominator * numerator + (
        value % denominator * numerator // denominator
    )


def _ceil_div(value: int, divisor: int) -> int:
    return value // divisor + (1 if value % divisor else 0)


def _require_positive_int(value: object, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_INT
    ):
        raise ValueError(f"{label} must be a positive bounded integer")


def _require_sha(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith(_SHA_PREFIX)
        or len(value) != 71
    ):
        raise ValueError(f"{label} hash is not canonical")
    try:
        int(value[len(_SHA_PREFIX) :], 16)
    except ValueError as exc:
        raise ValueError(f"{label} hash is not canonical") from exc


def _canonical_json(payload: object) -> bytes:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if not 1 <= len(encoded) <= _MAX_CANONICAL_BYTES:
        raise ValueError("resource credit canonical bytes are outside the envelope")
    return encoded


def _sha256(payload: bytes) -> str:
    return _SHA_PREFIX + hashlib.sha256(payload).hexdigest()


__all__ = [
    "CleanupOutcome",
    "EncodedResourceReservationInput",
    "PerAttemptResourceAllowance",
    "ResourceCreditBucket",
    "ResourceCreditFacts",
    "ResourceCreditVector",
    "ResourceReservationInput",
    "STAGED_RESOURCE_CREDIT_POLICY_CONTRACT",
    "STAGED_RESOURCE_CREDIT_POLICY_V2",
    "STAGED_RESOURCE_RESERVATION_INPUT_CONTRACT",
    "STAGED_RESOURCE_STATE_TRANSITIONS",
    "StagedResourceCreditEnvelope",
    "StagedResourceCreditPolicy",
    "build_staged_resource_credit_envelope",
    "decode_resource_reservation_input",
    "encode_resource_reservation_input",
    "per_attempt_resource_allowance",
    "resource_credit_shape",
    "validate_staged_resource_credit_envelope",
]
