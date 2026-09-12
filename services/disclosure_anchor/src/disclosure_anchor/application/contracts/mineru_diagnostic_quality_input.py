"""Original-input and per-role request data for the owned quality boundary.

These declarations do not replay a journal, open a file or authorize a child.
The owner must compare them with its actual original evidence and held objects.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Self, TypeVar, cast

from disclosure_anchor.application.contracts.diagnostic_json import bounded_json_bytes, bounded_json_value
from disclosure_anchor.application.contracts.mineru_diagnostic_quality import (
    QualityRole, RetainedFileSeal, _integer, _sha,
)
from disclosure_anchor.application.contracts.mineru_diagnostic_quality_config import (
    OwnedDiagnosticQualityConfig, _array, _data, _path, _record_payload,
    _require_quality_projection_budget, _tuple,
)
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity


_ORIGINAL_RECORD_BYTES = 2 * 1024 * 1024 + 8192
_MAX_LIFETIME_NS = 7200 * 1_000_000_000
_WORK_ID = re.compile(r"[0-9a-f]{32}\Z")
_PRIVATE_FILE_MODE = 0o100600
_PRIVATE_DIRECTORY_MODE = 0o40700


def _identity(value: object, *, directory: bool | None) -> tuple[int, int, int, int]:
    identity = _tuple(value, 4, minimum=4)
    for index, component in enumerate(identity):
        _integer(component, minimum=1 if index == 1 else 0)
    modes = {_PRIVATE_DIRECTORY_MODE} if directory is True else (
        {_PRIVATE_FILE_MODE} if directory is False else {_PRIVATE_DIRECTORY_MODE, _PRIVATE_FILE_MODE}
    )
    if identity[2] not in modes:
        raise ValueError("owned quality original private identity mode differs")
    return cast(tuple[int, int, int, int], identity)


def _wire_identity(value: object) -> tuple[int, int, int, int]:
    return cast(tuple[int, int, int, int], tuple(_array(value, 4, minimum=4)))


@dataclass(frozen=True, slots=True)
class QualitySourceSeal:
    identity: tuple[int, int, int, int]
    bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _identity(self.identity, directory=False)
        _integer(self.bytes, minimum=1)
        _sha(self.sha256)

    def to_payload(self) -> dict[str, Any]:
        return {**_data(self), "identity": list(self.identity)}

    @classmethod
    def from_payload(cls, value: object) -> Self:
        data = _record_payload(value, cls)
        return cls(**{**data, "identity": _wire_identity(data["identity"])})


@dataclass(frozen=True, slots=True)
class QualityOutputEntry:
    path: str
    identity: tuple[int, int, int, int]
    bytes: int | None
    sha256: str | None

    def __post_init__(self) -> None:
        _path(self.path, absolute=False)
        if self.path != "output" and not self.path.startswith("output/"):
            raise ValueError("owned quality inventory must remain below original output")
        identity = _identity(self.identity, directory=None)
        if identity[2] == _PRIVATE_DIRECTORY_MODE:
            if self.bytes is not None or self.sha256 is not None:
                raise ValueError("owned quality directory cannot invent file contents")
        else:
            _integer(self.bytes)
            _sha(self.sha256)

    def to_payload(self) -> dict[str, Any]:
        return {**_data(self), "identity": list(self.identity)}

    @classmethod
    def from_payload(cls, value: object) -> Self:
        data = _record_payload(value, cls)
        return cls(**{**data, "identity": _wire_identity(data["identity"])})


@dataclass(frozen=True, slots=True)
class QualityInputManifest:
    contract_version: str
    attempt_id: str
    configuration_sha256: str
    clock_identity_sha256: str
    started_ns: int
    deadline_ns: int
    journal_root_identity: tuple[int, int, int, int]
    journal_header_sha256: str
    binding_record_sha256: str
    resources_identity: tuple[int, int, int, int]
    snapshot_seal: QualitySourceSeal
    snapshot_record_sha256: str
    source_observed_record_sha256: str
    source_page_count: int
    output_inventory: tuple[QualityOutputEntry, ...]
    output_inventory_sha256: str
    output_record_sha256: str
    target_identity: ParserTargetIdentity
    configuration: OwnedDiagnosticQualityConfig

    def __post_init__(self) -> None:
        if type(self.contract_version) is not str or self.contract_version != "mineru-owned-quality.input.v1":
            raise ValueError("owned quality input version differs")
        if (type(self.attempt_id) is not str or not 1 <= len(self.attempt_id) <= 128
                or any(ord(c) < 33 for c in self.attempt_id)):
            raise ValueError("owned quality attempt differs from original journal grammar")
        # The existing character bound makes this at most 512 bytes. Preserve
        # its Unicode/DEL grammar while rejecting nonrepresentable surrogates.
        try:
            self.attempt_id.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("owned quality attempt must be representable as UTF-8") from error
        _integer(self.started_ns)
        _integer(self.deadline_ns, minimum=1)
        if not 0 < self.deadline_ns - self.started_ns <= _MAX_LIFETIME_NS:
            raise ValueError("owned quality original lifetime differs")
        for digest in (self.configuration_sha256, self.clock_identity_sha256, self.journal_header_sha256,
                       self.binding_record_sha256, self.snapshot_record_sha256, self.source_observed_record_sha256,
                       self.output_inventory_sha256, self.output_record_sha256):
            _sha(digest)
        _identity(self.journal_root_identity, directory=True)
        _identity(self.resources_identity, directory=True)
        if type(self.snapshot_seal) is not QualitySourceSeal:
            raise ValueError("exact original quality source seal required")
        self.snapshot_seal.__post_init__()
        _integer(self.source_page_count, minimum=1)
        if type(self.configuration) is not OwnedDiagnosticQualityConfig:
            raise ValueError("exact original owned quality configuration required")
        self.configuration.__post_init__()
        if type(self.target_identity) is not ParserTargetIdentity:
            raise ValueError("exact original parser target required")
        _require_quality_projection_budget(self.target_identity, maximum_bytes=_ORIGINAL_RECORD_BYTES,
                                           record_types=(ParserTargetIdentity,))
        target = _data(self.target_identity)
        bounded_json_bytes(target, maximum_bytes=_ORIGINAL_RECORD_BYTES)
        ParserTargetIdentity.from_payload(target)
        _validate_inventory(self.output_inventory, self.output_inventory_sha256)

    def to_payload(self) -> dict[str, Any]:
        return {**_data(self), "journal_root_identity": list(self.journal_root_identity),
                "resources_identity": list(self.resources_identity),
                "snapshot_seal": self.snapshot_seal.to_payload(),
                "output_inventory": [entry.to_payload() for entry in self.output_inventory],
                "target_identity": _data(self.target_identity), "configuration": self.configuration.to_payload()}

    @classmethod
    def from_payload(cls, value: object) -> Self:
        data = _record_payload(value, cls)
        # The original inventory and target already had to fit in one E1 record.
        # Bound their direct decoder before constructing an expanded DTO graph.
        inventory = _array(data["output_inventory"], _ORIGINAL_RECORD_BYTES)
        bounded_json_bytes(inventory, maximum_bytes=_ORIGINAL_RECORD_BYTES)
        target = _record_payload(data["target_identity"], ParserTargetIdentity)
        bounded_json_bytes(target, maximum_bytes=_ORIGINAL_RECORD_BYTES)
        return cls(**{**data,
                      "journal_root_identity": _wire_identity(data["journal_root_identity"]),
                      "resources_identity": _wire_identity(data["resources_identity"]),
                      "snapshot_seal": QualitySourceSeal.from_payload(data["snapshot_seal"]),
                      "output_inventory": tuple(QualityOutputEntry.from_payload(entry) for entry in inventory),
                      "target_identity": ParserTargetIdentity.from_payload(target),
                      "configuration": OwnedDiagnosticQualityConfig.from_payload(data["configuration"])})


def _validate_inventory(inventory: tuple[QualityOutputEntry, ...], expected_sha: str) -> None:
    _tuple(inventory, _ORIGINAL_RECORD_BYTES)
    _require_quality_projection_budget(inventory, maximum_bytes=_ORIGINAL_RECORD_BYTES,
                                       record_types=(QualityOutputEntry,))
    names: set[str] = set()
    directories: set[str] = set()
    for entry in inventory:
        if type(entry) is not QualityOutputEntry:
            raise ValueError("exact original quality output entry required")
        entry.__post_init__()
        if entry.path in names:
            raise ValueError("owned quality output inventory path is duplicated")
        names.add(entry.path)
        if entry.identity[2] == _PRIVATE_DIRECTORY_MODE:
            directories.add(entry.path)
    if "output" not in directories:
        raise ValueError("owned quality output inventory lacks original root directory")
    for name in names - {"output"}:
        if name.rsplit("/", 1)[0] not in directories:
            raise ValueError("owned quality output inventory parent directory is missing")
    raw = bounded_json_bytes([entry.to_payload() for entry in inventory], maximum_bytes=_ORIGINAL_RECORD_BYTES)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("owned quality original ordered inventory hash differs")


@dataclass(frozen=True, slots=True)
class QualityChildRequest:
    contract_version: str
    role: QualityRole
    work_id: str
    input_file: RetainedFileSeal
    retained_root_identity: tuple[int, int, int, int]
    resources_identity: tuple[int, int, int, int]
    source_path: str
    output_path: str
    input_path: str

    def __post_init__(self) -> None:
        if type(self.contract_version) is not str or self.contract_version != "mineru-owned-quality.request.v1":
            raise ValueError("owned quality request version differs")
        if type(self.role) is not str or self.role not in {"producer", "verifier"}:
            raise ValueError("owned quality request role differs")
        if type(self.work_id) is not str or _WORK_ID.fullmatch(self.work_id) is None:
            raise ValueError("owned quality request work identity differs")
        if type(self.input_file) is not RetainedFileSeal:
            raise ValueError("exact sealed quality input file required")
        self.input_file.__post_init__()
        if (self.input_file.slot != "input.json" or self.input_file.evidence_kind != "complete"
                or self.input_file.byte_count < 1):
            raise ValueError("owned quality request requires complete nonempty input slot")
        _identity(self.retained_root_identity, directory=True)
        _identity(self.resources_identity, directory=True)
        for path in (self.source_path, self.output_path, self.input_path):
            _path(path, absolute=True)

    def to_payload(self) -> dict[str, Any]:
        return {**_data(self), "input_file": self.input_file.to_payload(),
                "retained_root_identity": list(self.retained_root_identity),
                "resources_identity": list(self.resources_identity)}

    @classmethod
    def from_payload(cls, value: object) -> Self:
        data = _record_payload(value, cls)
        return cls(**{**data, "input_file": RetainedFileSeal.from_payload(data["input_file"]),
                      "retained_root_identity": _wire_identity(data["retained_root_identity"]),
                      "resources_identity": _wire_identity(data["resources_identity"])})


QualityInputValue = QualitySourceSeal | QualityOutputEntry | QualityInputManifest | QualityChildRequest
_INPUT_TYPES = (QualitySourceSeal, QualityOutputEntry, QualityInputManifest, QualityChildRequest)
_QualityInputT = TypeVar("_QualityInputT", bound=QualityInputValue)


def encode_quality_input_value(value: QualityInputValue, *, maximum_bytes: int) -> bytes:
    if type(value) not in _INPUT_TYPES:
        raise ValueError("exact owned quality input value required")
    _require_quality_projection_budget(value, maximum_bytes=maximum_bytes,
                                       record_types=(*_INPUT_TYPES, ParserTargetIdentity, RetainedFileSeal))
    if type(value) is QualityInputManifest:
        # A large caller grant cannot expand forged original-record fields past
        # the immutable E1 envelope before their direct decoder sees them.
        _require_quality_projection_budget(value.output_inventory, maximum_bytes=_ORIGINAL_RECORD_BYTES,
                                           record_types=(QualityOutputEntry,))
        _require_quality_projection_budget(value.target_identity, maximum_bytes=_ORIGINAL_RECORD_BYTES,
                                           record_types=(ParserTargetIdentity,))
    payload = value.to_payload()
    type(value).from_payload(payload)
    return bounded_json_bytes(payload, maximum_bytes=maximum_bytes)


def decode_quality_input_value(raw: bytes, record_type: type[_QualityInputT], *, maximum_bytes: int) -> _QualityInputT:
    if not any(record_type is known for known in _INPUT_TYPES):
        raise ValueError("exact owned quality input decoder required")
    payload = bounded_json_value(raw, maximum_bytes=maximum_bytes)
    return cast(_QualityInputT, record_type.from_payload(payload))


__all__ = [
    "QualitySourceSeal", "QualityOutputEntry", "QualityInputManifest", "QualityChildRequest",
    "QualityInputValue", "encode_quality_input_value", "decode_quality_input_value",
]
