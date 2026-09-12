"""Closed private owned-quality primitives; data validity grants no IO authority.

The owner must bind these values to actual retained descriptors and held child
processes. In particular, a valid receipt is not proof of a read or a closure.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import re
from typing import Any, Literal, Self, TypeVar, cast

from disclosure_anchor.application.contracts.diagnostic_json import (
    bounded_json_bytes, bounded_json_value, require_projection_budget,
)


QualityRole = Literal["producer", "verifier"]
QualityFrameKind = Literal["S", "B", "M", "C"]
QUALITY_ROLES: tuple[QualityRole, ...] = ("producer", "verifier")
FRAME_MAGIC = b"M6Q1"
FRAME_HEADER_BYTES = 13
FRAME_KINDS: tuple[QualityFrameKind, ...] = ("S", "B", "M", "C")
QUALITY_FILE_SLOTS = (
    "input.json",
    "producer.request.json", "producer.source.json", "producer.build.json",
    "producer.reads.json", "producer.control.raw", "producer.stderr.raw",
    "verifier.request.json", "verifier.source.json", "verifier.build.json",
    "verifier.reads.json", "verifier.control.raw", "verifier.stderr.raw",
    "comparison.json", "qualification.json", "result.json",
)
_MAX_COUNT = 2**63 - 1
_MAX_FRAME = 2**64 - 1
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _integer(value: object, *, minimum: int = 0, maximum: int = _MAX_COUNT) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("owned quality integer type or bound differs")


def _boolean(value: object) -> None:
    if type(value) is not bool:
        raise ValueError("owned quality boolean type differs")


def _kind(value: object) -> None:
    if type(value) is not str or value not in FRAME_KINDS:
        raise ValueError("owned quality frame kind differs")


def _sha(value: object) -> None:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise ValueError("owned quality SHA-256 reference differs")


def _closed(value: object, expected: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise ValueError("owned quality primitive fields are not closed")
    return value


def _payload(value: Any) -> dict[str, Any]:
    # Only the fixed six primitive types call this helper. No deepcopy or
    # recursive arbitrary object conversion belongs at the wire boundary.
    return {field.name: getattr(value, field.name) for field in fields(value)}


@dataclass(frozen=True, slots=True)
class H2ByteBudget:
    semantic_record_bytes: int
    build_record_bytes: int
    comparison_evidence_bytes: int
    child_control_bytes: int
    child_stderr_bytes: int
    retained_total_bytes: int

    def __post_init__(self) -> None:
        for field in fields(self):
            _integer(getattr(self, field.name), minimum=1)

    def slot_limit(self, slot: str) -> int:
        if type(slot) is not str or slot not in QUALITY_FILE_SLOTS:
            raise ValueError("owned quality retained slot differs")
        if slot.endswith(".source.json"):
            return self.semantic_record_bytes
        if slot.endswith(".build.json"):
            return self.build_record_bytes
        if slot.endswith((".request.json", ".control.raw")):
            return self.child_control_bytes
        if slot.endswith(".stderr.raw"):
            return self.child_stderr_bytes
        return self.comparison_evidence_bytes

    @property
    def slot_ceiling_total(self) -> int:
        return sum(self.slot_limit(slot) for slot in QUALITY_FILE_SLOTS)

    def to_payload(self) -> dict[str, Any]:
        return _payload(self)

    @classmethod
    def from_payload(cls, value: object) -> Self:
        return cls(**_closed(value, {field.name for field in fields(cls)}))


@dataclass(frozen=True, slots=True)
class QualityFrameHeader:
    kind: QualityFrameKind
    payload_bytes: int

    def __post_init__(self) -> None:
        _kind(self.kind)
        _integer(self.payload_bytes, maximum=_MAX_FRAME)

    def to_payload(self) -> dict[str, Any]:
        return _payload(self)

    @classmethod
    def from_payload(cls, value: object) -> Self:
        return cls(**_closed(value, {"kind", "payload_bytes"}))


def encode_quality_frame_header(header: QualityFrameHeader) -> bytes:
    if type(header) is not QualityFrameHeader:
        raise ValueError("exact owned quality header required")
    # Validate again before deriving bytes, including deliberately altered
    # dataclass instances; this still provides no external attestation.
    QualityFrameHeader.from_payload(header.to_payload())
    return FRAME_MAGIC + header.kind.encode("ascii") + header.payload_bytes.to_bytes(8, "big")


def decode_quality_frame_header(raw: bytes) -> QualityFrameHeader:
    if type(raw) is not bytes or len(raw) != FRAME_HEADER_BYTES or raw[:4] != FRAME_MAGIC:
        raise ValueError("owned quality frame header length or magic differs")
    try:
        kind = raw[4:5].decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("owned quality frame kind differs") from error
    return QualityFrameHeader(cast(QualityFrameKind, kind), int.from_bytes(raw[5:], "big"))


@dataclass(frozen=True, slots=True)
class QualityStreamReceipt:
    observed_bytes: int
    retained_bytes: int
    discarded_bytes: int
    eof_observed: bool
    total_bytes: int | None

    def __post_init__(self) -> None:
        for value in (self.observed_bytes, self.retained_bytes, self.discarded_bytes):
            _integer(value)
        _boolean(self.eof_observed)
        if self.observed_bytes != self.retained_bytes + self.discarded_bytes:
            raise ValueError("owned quality observed stream byte conservation differs")
        if self.total_bytes is not None:
            _integer(self.total_bytes)
        if (self.eof_observed and self.total_bytes != self.observed_bytes
                or not self.eof_observed and self.total_bytes is not None):
            raise ValueError("owned quality stream total requires observed EOF")

    def to_payload(self) -> dict[str, Any]:
        return _payload(self)

    @classmethod
    def from_payload(cls, value: object) -> Self:
        return cls(**_closed(value, {field.name for field in fields(cls)}))


@dataclass(frozen=True, slots=True)
class QualityFrameReceipt:
    header_observed_bytes: int
    kind: QualityFrameKind | None
    declared_payload_bytes: int | None
    payload_observed_bytes: int
    payload_retained_bytes: int
    payload_discarded_bytes: int
    complete: bool
    payload_sha256: str | None

    def __post_init__(self) -> None:
        _integer(self.header_observed_bytes, maximum=FRAME_HEADER_BYTES)
        for value in (self.payload_observed_bytes, self.payload_retained_bytes, self.payload_discarded_bytes):
            _integer(value)
        _boolean(self.complete)
        if self.payload_observed_bytes != self.payload_retained_bytes + self.payload_discarded_bytes:
            raise ValueError("owned quality observed frame byte conservation differs")
        if self.kind is None:
            if (self.declared_payload_bytes is not None or self.payload_observed_bytes
                    or self.complete or self.payload_sha256 is not None):
                raise ValueError("owned quality incomplete header has invented payload facts")
            return
        _kind(self.kind)
        _integer(self.declared_payload_bytes, maximum=_MAX_FRAME)
        assert self.declared_payload_bytes is not None
        if (self.header_observed_bytes != FRAME_HEADER_BYTES
                or self.payload_observed_bytes > self.declared_payload_bytes
                or self.complete != (self.payload_observed_bytes == self.declared_payload_bytes)):
            raise ValueError("owned quality frame declaration and completion differ")
        if self.complete and not self.payload_discarded_bytes:
            _sha(self.payload_sha256)
        elif self.payload_sha256 is not None:
            raise ValueError("owned quality partial or discarded frame cannot seal full payload")

    def to_payload(self) -> dict[str, Any]:
        return _payload(self)

    @classmethod
    def from_payload(cls, value: object) -> Self:
        return cls(**_closed(value, {field.name for field in fields(cls)}))


@dataclass(frozen=True, slots=True)
class QualityError:
    stage: str
    exception_type: str
    message: str
    message_truncated: bool
    original_message_bytes: int | None

    def __post_init__(self) -> None:
        for value, limit in ((self.stage, 64), (self.exception_type, 128)):
            if (type(value) is not str or not 0 < len(value) <= limit
                    or any(not 32 <= ord(character) <= 126 for character in value)):
                raise ValueError("owned quality error label type or bound differs")
        if type(self.message) is not str or len(self.message) > 2048:
            raise ValueError("owned quality error message type or bound differs")
        try:
            length = len(self.message.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("owned quality error message is not UTF-8") from error
        if length > 2048:
            raise ValueError("owned quality error message byte bound exceeded")
        _boolean(self.message_truncated)
        if self.original_message_bytes is not None:
            _integer(self.original_message_bytes)
        if (not self.message_truncated and self.original_message_bytes != length
                or self.message_truncated and self.original_message_bytes is not None
                and self.original_message_bytes <= length):
            raise ValueError("owned quality error message truncation facts differ")

    def to_payload(self) -> dict[str, Any]:
        return _payload(self)

    @classmethod
    def from_payload(cls, value: object) -> Self:
        return cls(**_closed(value, {field.name for field in fields(cls)}))


@dataclass(frozen=True, slots=True)
class RetainedFileSeal:
    slot: str
    identity: tuple[int, int, int, int]
    byte_count: int
    sha256: str
    evidence_kind: Literal["complete", "failure_prefix"]

    def __post_init__(self) -> None:
        if type(self.slot) is not str or self.slot not in QUALITY_FILE_SLOTS:
            raise ValueError("owned quality retained slot differs")
        if type(self.identity) is not tuple or len(self.identity) != 4:
            raise ValueError("owned quality file identity shape differs")
        for index, value in enumerate(self.identity):
            _integer(value, minimum=1 if index == 1 else 0)
        if self.identity[2] != 0o100600:
            raise ValueError("owned quality seal requires an original private regular file")
        _integer(self.byte_count)
        _sha(self.sha256)
        if type(self.evidence_kind) is not str or self.evidence_kind not in {"complete", "failure_prefix"}:
            raise ValueError("owned quality retained evidence kind differs")

    def to_payload(self) -> dict[str, Any]:
        return {**_payload(self), "identity": list(self.identity)}

    @classmethod
    def from_payload(cls, value: object) -> Self:
        data = _closed(value, {field.name for field in fields(cls)})
        if type(data["identity"]) is not list or len(data["identity"]) != 4:
            raise ValueError("owned quality wire file identity must be a four-element list")
        return cls(**{**data, "identity": tuple(data["identity"])})


QualityValue = H2ByteBudget | QualityFrameHeader | QualityStreamReceipt | QualityFrameReceipt | QualityError | RetainedFileSeal
_QUALITY_VALUE_TYPES = (H2ByteBudget, QualityFrameHeader, QualityStreamReceipt, QualityFrameReceipt, QualityError, RetainedFileSeal)
_QualityValueT = TypeVar("_QualityValueT", bound=QualityValue)


def encode_quality_value(value: QualityValue, *, maximum_bytes: int) -> bytes:
    if type(value) not in _QUALITY_VALUE_TYPES:
        raise ValueError("exact owned quality primitive required")
    require_projection_budget(value, maximum_bytes=maximum_bytes)
    payload = value.to_payload()
    type(value).from_payload(payload)
    return bounded_json_bytes(payload, maximum_bytes=maximum_bytes)


def decode_quality_value(raw: bytes, record_type: type[_QualityValueT], *, maximum_bytes: int) -> _QualityValueT:
    if not any(record_type is known for known in _QUALITY_VALUE_TYPES):
        raise ValueError("exact owned quality primitive decoder required")
    payload = bounded_json_value(raw, maximum_bytes=maximum_bytes)
    return cast(_QualityValueT, record_type.from_payload(payload))


__all__ = [
    "QualityRole", "QualityFrameKind", "QUALITY_ROLES", "QUALITY_FILE_SLOTS",
    "FRAME_MAGIC", "FRAME_HEADER_BYTES", "FRAME_KINDS", "H2ByteBudget",
    "QualityFrameHeader", "QualityStreamReceipt", "QualityFrameReceipt", "QualityError",
    "RetainedFileSeal", "QualityValue", "encode_quality_frame_header", "decode_quality_frame_header",
    "encode_quality_value", "decode_quality_value",
]
