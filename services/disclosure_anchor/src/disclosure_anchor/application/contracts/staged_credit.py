"""Closed credit and lease contracts for durable staged parsing."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
import hashlib
import json
from math import ceil, isfinite
from types import MappingProxyType
from typing import Literal

from disclosure_anchor.application.contracts.mineru_process_profile import (
    MineruProcessProfile,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads


STAGED_CREDIT_POLICY_CONTRACT = "staged-credit-policy.v1"
RESERVATION_INPUT_CONTRACT = "staged-credit-reservation-input.v1"
_MAX_INT = (1 << 63) - 1
_MAX_CANONICAL_BYTES = 64 * 1024
_SHA_PREFIX = "sha256:"
CreditBucket = Literal["regular", "heavy", "huge"]


def _freeze_shapes(
    shapes: dict[str, dict[str, str]],
) -> MappingProxyType[str, MappingProxyType[str, str]]:
    return MappingProxyType(
        {state: MappingProxyType(dict(shape)) for state, shape in shapes.items()}
    )

# Fractions are exact numerator/denominator pairs.  No float participates in
# bucket selection or reservation derivation.
_BUCKETS: tuple[tuple[CreditBucket, int, int, int, int], ...] = (
    ("regular", 1, 8, 1, 2),
    ("heavy", 1, 2, 2, 3),
    ("huge", 1, 1, 4, 4),
)
_RESERVATION_MECHANICS = MappingProxyType(
    {
        "ack_items": "literal:1",
        "compressed_bytes": "retained_bytes",
        "db_stage_items": "literal:1",
        "db_staged_bytes": "profile.db_staged_bytes_limit (right-sized only by exact LocalMaterializationReceiptV2.db_staged_byte_count)",
        "decoded_bytes": "min(profile.decoded_payload_bytes_limit,source_page_count*ceil(profile.rasterized_page_bytes_limit/profile.resident_pages_limit))",
        "documents": "literal:1",
        "local_items": "literal:1",
        "remote_waits": "literal:1",
        "retained_bytes": "min(profile.result_reservation_bytes*bucket.result_reservation_multiplier,profile.terminal_output_bytes_limit,profile.max_unacked_result_bytes)",
        "retained_results": "literal:1",
        "temporary_disk_bytes": "min(profile.temporary_disk_bytes_limit,retained_bytes+min(decoded_bytes,retained_bytes*bucket.temp_expansion_multiplier))",
        "unpublished_pages": "source_page_count",
    }
)

STAGED_STATE_TRANSITIONS = MappingProxyType({
    "prepared": frozenset({"reconciling", "pre_submission_failed"}),
    "reconciling": frozenset({"submitted"}),
    "submitted": frozenset({"remote_terminal", "remote_failure_committed"}),
    "remote_terminal": frozenset({"materializing", "local_failure_committed"}),
    "materializing": frozenset({"local_materialized", "local_failure_committed"}),
    "local_materialized": frozenset({"finish_committed"}),
    "finish_committed": frozenset({"acked"}),
    "remote_failure_committed": frozenset({"remote_failed"}),
    "local_failure_committed": frozenset({"local_failed"}),
})
_STATE_CREDIT_SHAPES = _freeze_shapes({
    "prepared": {"documents": "one"},
    "reconciling": {"documents": "one", "remote_waits": "one"},
    "submitted": {"documents": "one", "remote_waits": "one"},
    "remote_terminal": {
        "documents": "one",
        "retained_results": "one",
        "retained_bytes": "terminal_byte_count",
    },
    "materializing": {
        "documents": "one",
        "retained_results": "one",
        "retained_bytes": "terminal_byte_count",
        "local_items": "one",
        "compressed_bytes": "compressed_byte_count",
        "decoded_bytes": "decoded_byte_count",
        "temp_disk_bytes": "temporary_disk_byte_count",
    },
    "local_materialized": {
        "documents": "one",
        "retained_results": "one",
        "retained_bytes": "terminal_byte_count",
        "db_stage_items": "one",
        "db_staged_bytes": "db_staged_byte_count",
        "unpublished_pages": "source_page_count",
    },
    "finish_committed": {
        "documents": "one",
        "retained_results": "one",
        "retained_bytes": "terminal_byte_count",
        "ack_items": "one",
    },
    "remote_failure_committed": {
        "documents": "one",
        "retained_results": "one",
        "ack_items": "one",
    },
    "local_failure_committed": {
        "documents": "one",
        "retained_results": "one",
        "retained_bytes": "terminal_byte_count",
        "ack_items": "one",
    },
    "acked": {},
    "remote_failed": {},
    "local_failed": {},
    "pre_submission_failed": {},
    "superseded": {},
})
_LOCAL_FAILURE_POST_MATERIALIZATION_SHAPE = MappingProxyType(
    {
        **dict(_STATE_CREDIT_SHAPES["materializing"]),
        "ack_items": "one",
    }
)
_LOCAL_FAILURE_COMPLETED_SHAPE = MappingProxyType(
    {
        **dict(_LOCAL_FAILURE_POST_MATERIALIZATION_SHAPE),
        "db_stage_items": "one",
        "db_staged_bytes": "db_staged_byte_count",
    }
)


@dataclass(frozen=True, slots=True)
class CreditVector:
    documents: int = 0
    remote_waits: int = 0
    retained_results: int = 0
    retained_bytes: int = 0
    local_items: int = 0
    compressed_bytes: int = 0
    decoded_bytes: int = 0
    temp_disk_bytes: int = 0
    db_stage_items: int = 0
    db_staged_bytes: int = 0
    ack_items: int = 0
    unpublished_pages: int = 0

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_INT
            ):
                raise ValueError(
                    f"credit {item.name} must be a non-negative bounded integer"
                )

    def __add__(self, other: CreditVector) -> CreditVector:
        return CreditVector(
            **{
                item.name: _checked_add(
                    getattr(self, item.name), getattr(other, item.name)
                )
                for item in fields(self)
            }
        )

    def __sub__(self, other: CreditVector) -> CreditVector:
        values = {
            item.name: getattr(self, item.name) - getattr(other, item.name)
            for item in fields(self)
        }
        if any(value < 0 for value in values.values()):
            raise ValueError("credit release would make ownership negative")
        return CreditVector(**values)

    def fits(self, limit: CreditVector) -> bool:
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
class StagedCreditPolicy:
    contract_version: str = STAGED_CREDIT_POLICY_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != STAGED_CREDIT_POLICY_CONTRACT:
            raise ValueError("staged credit policy contract is unsupported")

    @property
    def exact_bytes(self) -> bytes:
        payload = {
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
            "contract_version": self.contract_version,
            "credit_dimensions": [item.name for item in fields(CreditVector)],
            "max_integer": _MAX_INT,
            "reservation_mechanics": dict(_RESERVATION_MECHANICS),
            "state_credit_shapes": {
                state: dict(shape)
                for state, shape in sorted(_STATE_CREDIT_SHAPES.items())
            },
            "state_transitions": {
                state: sorted(next_states)
                for state, next_states in sorted(STAGED_STATE_TRANSITIONS.items())
            },
            "state_variants": {
                "local_failure_committed": {
                    "local_materialization_completed": dict(
                        _LOCAL_FAILURE_COMPLETED_SHAPE
                    ),
                    "post_materialization": dict(
                        _LOCAL_FAILURE_POST_MATERIALIZATION_SHAPE
                    ),
                    "pre_materialization": dict(
                        _STATE_CREDIT_SHAPES["local_failure_committed"]
                    ),
                }
            },
        }
        return _canonical_json(payload)

    @property
    def sha256(self) -> str:
        return _sha256(self.exact_bytes)


STAGED_CREDIT_POLICY_V1 = StagedCreditPolicy()


@dataclass(frozen=True, slots=True)
class ReservationInput:
    source_pdf_sha256: str
    source_byte_count: int
    source_page_count: int
    process_profile_sha256: str
    credit_policy_sha256: str
    bucket: CreditBucket
    reservation: CreditVector
    contract_version: str = RESERVATION_INPUT_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != RESERVATION_INPUT_CONTRACT:
            raise ValueError("reservation input contract is unsupported")
        for value, label in (
            (self.source_pdf_sha256, "source PDF"),
            (self.process_profile_sha256, "process profile"),
            (self.credit_policy_sha256, "credit policy"),
        ):
            _require_sha(value, label)
        for numeric_value, label in (
            (self.source_byte_count, "source byte count"),
            (self.source_page_count, "source page count"),
        ):
            _require_positive_int(numeric_value, label)
        if self.bucket not in {item[0] for item in _BUCKETS}:
            raise ValueError("reservation input bucket is unsupported")
        if type(self.reservation) is not CreditVector:
            raise ValueError("reservation input requires an exact credit vector")


@dataclass(frozen=True, slots=True)
class EncodedReservationInput:
    value: ReservationInput
    exact_bytes: bytes = field(repr=False)
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        if type(self.exact_bytes) is not bytes or not (
            1 <= len(self.exact_bytes) <= _MAX_CANONICAL_BYTES
        ):
            raise ValueError("reservation input bytes are outside the closed envelope")
        if isinstance(self.byte_count, bool) or self.byte_count != len(self.exact_bytes):
            raise ValueError("reservation input byte count drifted")
        if self.sha256 != _sha256(self.exact_bytes):
            raise ValueError("reservation input hash drifted")
        if self.exact_bytes != _canonical_json(asdict(self.value)):
            raise ValueError("reservation input bytes differ from their projection")


@dataclass(frozen=True, slots=True)
class StagedCreditEnvelope:
    process_profile_sha256: str
    credit_policy_sha256: str
    reservation_input: EncodedReservationInput
    reservation: CreditVector

    def __post_init__(self) -> None:
        if type(self.reservation_input) is not EncodedReservationInput:
            raise ValueError("credit envelope requires exact encoded reservation input")
        if type(self.reservation) is not CreditVector:
            raise ValueError("credit envelope requires an exact credit vector")
        _require_sha(self.process_profile_sha256, "envelope process profile")
        _require_sha(self.credit_policy_sha256, "envelope credit policy")
        if (
            self.process_profile_sha256
            != self.reservation_input.value.process_profile_sha256
            or self.credit_policy_sha256
            != self.reservation_input.value.credit_policy_sha256
        ):
            raise ValueError("credit envelope identity drifted from reservation input")
        if self.reservation != self.reservation_input.value.reservation:
            raise ValueError("credit envelope reservation drifted from exact input bytes")


@dataclass(frozen=True, slots=True)
class CreditShapeFacts:
    terminal_byte_count: int = 0
    compressed_byte_count: int = 0
    uncompressed_byte_count: int = 0
    decoded_byte_count: int = 0
    temporary_disk_byte_count: int = 0
    db_staged_byte_count: int = 0
    source_page_count: int = 0
    materialization_prepared: bool = False
    local_materialization_completed: bool = False

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name in {
                "materialization_prepared",
                "local_materialization_completed",
            }:
                if type(value) is not bool:
                    raise ValueError("materialization prepared must be boolean")
            elif isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_INT:
                raise ValueError(f"credit shape fact {item.name} is invalid")


def credit_shape(state: str, facts: CreditShapeFacts) -> CreditVector:
    """Return the sole current-credit projection for a durable state."""

    if state in {"prepared", "reconciling", "submitted"}:
        _require_empty_facts(facts)
    elif state == "remote_terminal":
        _require_terminal_facts(facts)
    elif state == "materializing":
        _require_materialization_facts(facts)
        if facts.local_materialization_completed:
            raise ValueError("materializing state has completed local facts")
    elif state == "local_materialized":
        _require_local_facts(facts)
    elif state == "finish_committed":
        _require_terminal_facts(facts)
    elif state == "remote_failure_committed":
        _require_empty_facts(facts)
    elif state == "local_failure_committed":
        _require_terminal_facts(facts)
        if facts.materialization_prepared:
            _require_materialization_facts(facts)
        elif any(
            (
                facts.compressed_byte_count,
                facts.uncompressed_byte_count,
                facts.decoded_byte_count,
                facts.temporary_disk_byte_count,
                facts.source_page_count,
            )
        ):
            raise ValueError("pre-materialization local failure has stale facts")
    elif state not in _STATE_CREDIT_SHAPES:
        raise ValueError("credit shape state is unsupported")
    shape = (
        _LOCAL_FAILURE_COMPLETED_SHAPE
        if state == "local_failure_committed" and facts.local_materialization_completed
        else _LOCAL_FAILURE_POST_MATERIALIZATION_SHAPE
        if state == "local_failure_committed" and facts.materialization_prepared
        else _STATE_CREDIT_SHAPES[state]
    )
    values: dict[str, int] = {}
    for dimension, source in shape.items():
        values[dimension] = 1 if source == "one" else getattr(facts, source)
    return CreditVector(**values)


@dataclass(frozen=True, slots=True)
class DatabaseLeaseSnapshot:
    database_observed_at_utc: datetime
    lease_until_utc: datetime
    remaining_microseconds: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.database_observed_at_utc, "database observation"),
            (self.lease_until_utc, "database lease"),
        ):
            if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
                raise ValueError(f"{label} must be UTC-aware")
        if (
            isinstance(self.remaining_microseconds, bool)
            or not isinstance(self.remaining_microseconds, int)
            or not -_MAX_INT <= self.remaining_microseconds <= _MAX_INT
        ):
            raise ValueError("database lease remaining microseconds is invalid")
        exact = self.lease_until_utc - self.database_observed_at_utc
        recomputed = exact.days * 86_400_000_000 + exact.seconds * 1_000_000 + exact.microseconds
        if recomputed != self.remaining_microseconds:
            raise ValueError("database lease remaining duration drifted")


def conservative_monotonic_deadline(
    snapshot: DatabaseLeaseSnapshot,
    *,
    monotonic_before: float,
    monotonic_after: float,
) -> float:
    for value in (monotonic_before, monotonic_after):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError("monotonic lease bracket is invalid")
    if monotonic_after < monotonic_before:
        raise ValueError("monotonic lease bracket moved backwards")
    bracket_seconds = monotonic_after - monotonic_before
    if not isfinite(bracket_seconds) or bracket_seconds > _MAX_INT / 1_000_000:
        raise ValueError("monotonic lease bracket is outside the closed range")
    bracket_us = ceil(bracket_seconds * 1_000_000)
    safe_us = max(0, snapshot.remaining_microseconds - bracket_us)
    if safe_us <= 0:
        raise ValueError("database lease is not safely runnable")
    deadline = monotonic_after + safe_us / 1_000_000
    if not isfinite(deadline):
        raise ValueError("monotonic lease deadline is outside the closed range")
    return deadline


def encode_reservation_input(value: ReservationInput) -> EncodedReservationInput:
    exact = _canonical_json(asdict(value))
    return EncodedReservationInput(
        value=value, exact_bytes=exact, sha256=_sha256(exact), byte_count=len(exact)
    )


def decode_reservation_input(exact_bytes: bytes) -> EncodedReservationInput:
    if type(exact_bytes) is not bytes or not 1 <= len(exact_bytes) <= _MAX_CANONICAL_BYTES:
        raise ValueError("reservation input bytes are outside the closed envelope")
    decoded = strict_json_loads(exact_bytes)
    expected = {item.name for item in fields(ReservationInput)}
    if not isinstance(decoded, dict) or set(decoded) != expected:
        raise ValueError("reservation input fields are not closed")
    nested = decoded.get("reservation")
    if not isinstance(nested, dict) or set(nested) != {
        item.name for item in fields(CreditVector)
    }:
        raise ValueError("reservation input credit vector is not closed")
    value = ReservationInput(
        **{**decoded, "reservation": CreditVector(**nested)}
    )
    encoded = encode_reservation_input(value)
    if encoded.exact_bytes != exact_bytes:
        raise ValueError("reservation input JSON is not canonical")
    return encoded


def build_staged_credit_envelope(
    *,
    profile: MineruProcessProfile,
    source_pdf_sha256: str,
    source_byte_count: int,
    source_page_count: int,
    policy: StagedCreditPolicy = STAGED_CREDIT_POLICY_V1,
) -> StagedCreditEnvelope:
    _require_sha(source_pdf_sha256, "source PDF")
    _require_positive_int(source_byte_count, "source byte count")
    _require_positive_int(source_page_count, "source page count")
    page_ceiling = profile.unpublished_pages_limit
    if source_byte_count > profile.source_pdf_bytes_limit or source_page_count > page_ceiling:
        raise ValueError("source facts exceed process profile global ceilings")
    selected: tuple[CreditBucket, int, int, int, int] | None = None
    for candidate in _BUCKETS:
        _, numerator, denominator, _, _ = candidate
        if (
            source_byte_count
            <= _floor_fraction(profile.source_pdf_bytes_limit, numerator, denominator)
            and source_page_count
            <= _floor_fraction(page_ceiling, numerator, denominator)
        ):
            selected = candidate
            break
    if selected is None:
        raise ValueError("source facts do not fit a staged credit bucket")
    bucket, numerator, denominator, result_multiplier, temp_multiplier = selected
    retained_global_cap = min(
        profile.terminal_output_bytes_limit, profile.max_unacked_result_bytes
    )
    retained_cap = _capped_mul(
        profile.result_reservation_bytes,
        result_multiplier,
        retained_global_cap,
    )
    raster_per_page = _ceil_div(
        profile.rasterized_page_bytes_limit, profile.resident_pages_limit
    )
    decoded_cap = min(
        profile.decoded_payload_bytes_limit,
        _capped_mul(
            source_page_count, raster_per_page, profile.decoded_payload_bytes_limit
        ),
    )
    uncompressed_staging_cap = min(
        decoded_cap,
        _capped_mul(retained_cap, temp_multiplier, decoded_cap),
    )
    temp_cap = min(
        profile.temporary_disk_bytes_limit,
        _capped_add(
            retained_cap,
            uncompressed_staging_cap,
            profile.temporary_disk_bytes_limit,
        ),
    )
    reservation = CreditVector(
        documents=1,
        remote_waits=1,
        retained_results=1,
        retained_bytes=retained_cap,
        local_items=1,
        compressed_bytes=retained_cap,
        decoded_bytes=decoded_cap,
        temp_disk_bytes=temp_cap,
        db_stage_items=1,
        db_staged_bytes=profile.db_staged_bytes_limit,
        ack_items=1,
        unpublished_pages=source_page_count,
    )
    reservation_input = encode_reservation_input(
        ReservationInput(
            source_pdf_sha256=source_pdf_sha256,
            source_byte_count=source_byte_count,
            source_page_count=source_page_count,
            process_profile_sha256=profile.sha256,
            credit_policy_sha256=policy.sha256,
            bucket=bucket,
            reservation=reservation,
        )
    )
    return StagedCreditEnvelope(
        process_profile_sha256=profile.sha256,
        credit_policy_sha256=policy.sha256,
        reservation_input=reservation_input,
        reservation=reservation,
    )


def validate_staged_credit_envelope(
    envelope: StagedCreditEnvelope,
    *,
    profile: MineruProcessProfile,
    policy: StagedCreditPolicy = STAGED_CREDIT_POLICY_V1,
) -> None:
    rebuilt = build_staged_credit_envelope(
        profile=profile,
        source_pdf_sha256=envelope.reservation_input.value.source_pdf_sha256,
        source_byte_count=envelope.reservation_input.value.source_byte_count,
        source_page_count=envelope.reservation_input.value.source_page_count,
        policy=policy,
    )
    if rebuilt != envelope:
        raise ValueError("staged credit envelope differs from profile/policy derivation")


def _require_terminal_facts(facts: CreditShapeFacts) -> None:
    if facts.terminal_byte_count < 1:
        raise ValueError("terminal credit shape requires exact retained bytes")


def _require_materialization_facts(facts: CreditShapeFacts) -> None:
    _require_terminal_facts(facts)
    if (
        not facts.materialization_prepared
        or facts.compressed_byte_count != facts.terminal_byte_count
        or facts.uncompressed_byte_count < 1
        or facts.decoded_byte_count < 1
        or facts.source_page_count < 1
        or facts.temporary_disk_byte_count
        != _checked_add(facts.compressed_byte_count, facts.uncompressed_byte_count)
    ):
        raise ValueError("materializing credit shape lacks exact prepared facts")
    if facts.local_materialization_completed and facts.db_staged_byte_count < 1:
        raise ValueError("completed local materialization lacks exact DB staged bytes")


def _require_empty_facts(facts: CreditShapeFacts) -> None:
    if facts != CreditShapeFacts():
        raise ValueError("credit state has incompatible stale facts")


def _require_local_facts(facts: CreditShapeFacts) -> None:
    _require_materialization_facts(facts)
    if not facts.local_materialization_completed or facts.db_staged_byte_count < 1:
        raise ValueError("local credit shape requires completed materialization")
    if facts.source_page_count < 1:
        raise ValueError("local credit shape requires exact source pages")


def _checked_add(left: int, right: int) -> int:
    result = left + right
    if result > _MAX_INT:
        raise ValueError("credit integer arithmetic overflowed")
    return result


def _checked_mul(left: int, right: int) -> int:
    result = left * right
    if result > _MAX_INT:
        raise ValueError("credit integer arithmetic overflowed")
    return result


def _capped_mul(value: int, multiplier: int, cap: int) -> int:
    if cap == 0 or value > cap // multiplier:
        return cap
    return value * multiplier


def _capped_add(left: int, right: int, cap: int) -> int:
    if left >= cap or right > cap - left:
        return cap
    return left + right


def _ceil_div(value: int, denominator: int) -> int:
    quotient, remainder = divmod(value, denominator)
    return quotient + int(remainder != 0)


def _floor_fraction(value: int, numerator: int, denominator: int) -> int:
    quotient, remainder = divmod(value, denominator)
    return quotient * numerator + (remainder * numerator) // denominator


def _require_positive_int(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_INT:
        raise ValueError(f"{label} must be a positive bounded integer")


def _require_sha(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.startswith(_SHA_PREFIX) or len(value) != 71:
        raise ValueError(f"{label} must be canonical sha256")
    if any(character not in "0123456789abcdef" for character in value[7:]):
        raise ValueError(f"{label} must be canonical sha256")


def _canonical_json(payload: object) -> bytes:
    exact = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(exact) > _MAX_CANONICAL_BYTES:
        raise ValueError("canonical staged credit bytes exceed the closed envelope")
    return exact


def _sha256(payload: bytes) -> str:
    return _SHA_PREFIX + hashlib.sha256(payload).hexdigest()


__all__ = [
    "CreditBucket",
    "CreditShapeFacts",
    "CreditVector",
    "DatabaseLeaseSnapshot",
    "EncodedReservationInput",
    "RESERVATION_INPUT_CONTRACT",
    "ReservationInput",
    "STAGED_CREDIT_POLICY_CONTRACT",
    "STAGED_CREDIT_POLICY_V1",
    "STAGED_STATE_TRANSITIONS",
    "StagedCreditEnvelope",
    "StagedCreditPolicy",
    "build_staged_credit_envelope",
    "conservative_monotonic_deadline",
    "credit_shape",
    "decode_reservation_input",
    "encode_reservation_input",
    "validate_staged_credit_envelope",
]
