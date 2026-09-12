"""Bounded private quality configuration data, without runtime authority.

Program coverage, actual loaded origins and original journal binding belong to
the owner. A valid file inventory here proves only its closed representation.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import re
from typing import Any, Self, TypeVar, cast

from disclosure_anchor.application.contracts.diagnostic_json import (
    MAXIMUM_JSON_DEPTH, bounded_json_bytes, bounded_json_value,
)
from disclosure_anchor.application.contracts.m6_document_qualification import (
    M6QualityPlan, M6ReasonPolicy,
)
from disclosure_anchor.application.contracts.mineru_diagnostic_quality import (
    H2ByteBudget, _closed, _integer, _sha,
)


QUALITY_WORKER_MODULE = "disclosure_anchor.adapters.runtime.mineru_diagnostic_quality_worker"
_DISTRIBUTION = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_IMPORT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_RUNTIME_LABEL = re.compile(r"[A-Za-z0-9_-]+\Z")
_MAX_PROGRAM_FILES = 16384
_MAX_PLAN_BYTES = 1024 * 1024


def _text(value: object, maximum: int, *, ascii_only: bool = False) -> str:
    if type(value) is not str or not 0 < len(value) <= maximum:
        raise ValueError("owned quality text type or bound differs")
    try:
        if len(value.encode("utf-8")) > maximum:
            raise ValueError("owned quality text byte bound exceeded")
    except UnicodeEncodeError as error:
        raise ValueError("owned quality text must be UTF-8") from error
    if "\x00" in value or ascii_only and any(not 32 <= ord(c) <= 126 for c in value):
        raise ValueError("owned quality text character domain differs")
    return value


def _path(value: object, *, absolute: bool) -> str:
    text = _text(value, 4096)
    if "\\" in text or text.startswith("/") != absolute:
        raise ValueError("owned quality path domain differs")
    if text == "/" and absolute:
        return text
    pieces = text[1:].split("/") if absolute else text.split("/")
    if any(part in {"", ".", ".."} for part in pieces):
        raise ValueError("owned quality path spelling is not canonical")
    return text


def _tuple(value: object, maximum: int, *, minimum: int = 1) -> tuple[Any, ...]:
    if type(value) is not tuple or not minimum <= len(value) <= maximum:
        raise ValueError("owned quality tuple type or count differs")
    return value


def _array(value: object, maximum: int, *, minimum: int = 1) -> list[Any]:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ValueError("owned quality array type or count differs")
    return value


def _ordered(names: tuple[str, ...]) -> None:
    if names != tuple(sorted(set(names))):
        raise ValueError("owned quality inventory must be sorted and unique")


def _data(value: object) -> dict[str, Any]:
    return {field.name: getattr(value, field.name) for field in fields(value)}  # type: ignore[arg-type]


def _record_payload(value: object, cls: type[Any]) -> dict[str, Any]:
    return _closed(value, {field.name for field in fields(cls)})


@dataclass(frozen=True, slots=True)
class QualityCodeFilePin:
    relative_path: str
    byte_count: int
    sha256: str

    def __post_init__(self) -> None:
        _path(self.relative_path, absolute=False)
        _integer(self.byte_count)
        _sha(self.sha256)

    def to_payload(self) -> dict[str, Any]:
        return _data(self)

    @classmethod
    def from_payload(cls, value: object) -> Self:
        return cls(**_record_payload(value, cls))


def _file_pins(value: object, maximum: int) -> tuple[QualityCodeFilePin, ...]:
    pins = _tuple(value, maximum)
    for pin in pins:
        if type(pin) is not QualityCodeFilePin:
            raise ValueError("exact owned quality file pin required")
        pin.__post_init__()
    _ordered(tuple(pin.relative_path for pin in pins))
    return pins


@dataclass(frozen=True, slots=True)
class QualityDependencyPin:
    distribution_name: str
    version: str
    install_root: str
    import_names: tuple[str, ...]
    files: tuple[QualityCodeFilePin, ...]

    def __post_init__(self) -> None:
        if _DISTRIBUTION.fullmatch(_text(self.distribution_name, 128, ascii_only=True)) is None:
            raise ValueError("owned quality distribution name is not canonical")
        _text(self.version, 128, ascii_only=True)
        _path(self.install_root, absolute=True)
        names = _tuple(self.import_names, 64)
        for name in names:
            if _IMPORT_NAME.fullmatch(_text(name, 256, ascii_only=True)) is None:
                raise ValueError("owned quality import name must be a top-level Python identifier")
        _ordered(names)
        _file_pins(self.files, 8192)

    def to_payload(self) -> dict[str, Any]:
        return {**_data(self), "import_names": list(self.import_names),
                "files": [pin.to_payload() for pin in self.files]}

    @classmethod
    def from_payload(cls, value: object) -> Self:
        data = _record_payload(value, cls)
        return cls(**{**data, "import_names": tuple(_array(data["import_names"], 64)),
                      "files": tuple(QualityCodeFilePin.from_payload(item)
                                     for item in _array(data["files"], 8192))})


@dataclass(frozen=True, slots=True)
class QualityProgramPin:
    contract_version: str
    worker_module: str
    interpreter_path: str
    resolved_interpreter_path: str
    interpreter_byte_count: int
    interpreter_sha256: str
    python_version: str
    python_cache_tag: str
    sys_platform: str
    source_root: str
    code_files: tuple[QualityCodeFilePin, ...]
    python_runtime_root: str
    python_runtime_files: tuple[QualityCodeFilePin, ...]
    dependency_pins: tuple[QualityDependencyPin, ...]

    def __post_init__(self) -> None:
        _program_shape(self)
        if type(self.contract_version) is not str or self.contract_version != "mineru-owned-quality.program.v1":
            raise ValueError("owned quality program version differs")
        if type(self.worker_module) is not str or self.worker_module != QUALITY_WORKER_MODULE:
            raise ValueError("owned quality worker module is fixed")
        for path in (self.interpreter_path, self.resolved_interpreter_path,
                     self.source_root, self.python_runtime_root):
            _path(path, absolute=True)
        _integer(self.interpreter_byte_count, minimum=1)
        _sha(self.interpreter_sha256)
        if _VERSION.fullmatch(_text(self.python_version, 32, ascii_only=True)) is None:
            raise ValueError("owned quality Python version requires three numeric components")
        for value, maximum in ((self.python_cache_tag, 64), (self.sys_platform, 32)):
            if _RUNTIME_LABEL.fullmatch(_text(value, maximum, ascii_only=True)) is None:
                raise ValueError("owned quality Python runtime label differs")
        dependencies = self.dependency_pins
        _file_pins(self.code_files, 1024)
        _file_pins(self.python_runtime_files, 8192)
        owned_files: set[tuple[str, str]] = set()
        owned_imports: set[str] = set()
        for dependency in dependencies:
            dependency.__post_init__()
            for name in dependency.import_names:
                if name in owned_imports:
                    raise ValueError("owned quality dependency import ownership overlaps")
                owned_imports.add(name)
            for pin in dependency.files:
                key = dependency.install_root, pin.relative_path
                if key in owned_files:
                    raise ValueError("owned quality dependency file ownership overlaps")
                owned_files.add(key)
        _ordered(tuple(dependency.distribution_name for dependency in dependencies))

    def to_payload(self) -> dict[str, Any]:
        return {**_data(self), "code_files": [pin.to_payload() for pin in self.code_files],
                "python_runtime_files": [pin.to_payload() for pin in self.python_runtime_files],
                "dependency_pins": [pin.to_payload() for pin in self.dependency_pins]}

    @classmethod
    def from_payload(cls, value: object) -> Self:
        data = _record_payload(value, cls)
        code = _array(data["code_files"], 1024)
        runtime = _array(data["python_runtime_files"], 8192)
        dependencies = _array(data["dependency_pins"], 32)
        count = len(code) + len(runtime)
        for dependency in dependencies:
            record = _record_payload(dependency, QualityDependencyPin)
            count += len(_array(record["files"], 8192))
        if count > _MAX_PROGRAM_FILES:
            raise ValueError("owned quality aggregate program file count exceeded")
        return cls(**{**data,
                      "code_files": tuple(QualityCodeFilePin.from_payload(item) for item in code),
                      "python_runtime_files": tuple(QualityCodeFilePin.from_payload(item) for item in runtime),
                      "dependency_pins": tuple(QualityDependencyPin.from_payload(item) for item in dependencies)})


def _program_shape(program: QualityProgramPin) -> None:
    """Reject forged oversized containers before traversal or projection."""
    count = len(_tuple(program.code_files, 1024)) + len(_tuple(program.python_runtime_files, 8192))
    for dependency in _tuple(program.dependency_pins, 32):
        if type(dependency) is not QualityDependencyPin:
            raise ValueError("exact owned quality dependency pin required")
        count += len(_tuple(dependency.files, 8192))
    if count > _MAX_PROGRAM_FILES:
        raise ValueError("owned quality aggregate program file count exceeded")


def _plan_shape(plan: object) -> M6QualityPlan:
    if type(plan) is not M6QualityPlan:
        raise ValueError("exact original M6 quality plan required")
    _text(plan.contract_version, 128)
    _text(plan.mode, 128)
    for check in _tuple(plan.required_checks, 13):
        _text(check, 128)
    for policy in _tuple(plan.reason_policies, 256, minimum=0):
        if type(policy) is not M6ReasonPolicy:
            raise ValueError("exact original M6 reason policy required")
        # M6Id is a character bound. Charge its actual UTF-8 representation in
        # the shared preflight; do not impose a new ASCII-only reason policy.
        if type(policy.reason) is not str or not 1 <= len(policy.reason) <= 128:
            raise ValueError("owned quality reason shape exceeds original M6 bounds")
        _text(policy.disposition, 128)
    return plan


def _plan_payload(plan: M6QualityPlan) -> dict[str, Any]:
    _plan_shape(plan)
    return {"contract_version": plan.contract_version, "mode": plan.mode,
            "required_checks": list(plan.required_checks),
            "reason_policies": [{"reason": policy.reason, "disposition": policy.disposition}
                                for policy in plan.reason_policies]}


def _validated_plan(plan: M6QualityPlan) -> None:
    payload = _plan_payload(plan)
    raw = bounded_json_bytes(payload, maximum_bytes=_MAX_PLAN_BYTES)
    # All meaning stays with the existing model, including all required checks,
    # ordering, closed reason dispositions and complete canonical fields.
    M6QualityPlan.from_canonical_bytes(raw, maximum_bytes=_MAX_PLAN_BYTES)


@dataclass(frozen=True, slots=True)
class OwnedDiagnosticQualityConfig:
    contract_version: str
    plan: M6QualityPlan
    budget: H2ByteBudget
    program: QualityProgramPin
    retained_name: str

    def __post_init__(self) -> None:
        if type(self.contract_version) is not str or self.contract_version != "mineru-owned-quality.config.v1":
            raise ValueError("owned quality configuration version differs")
        _validated_plan(self.plan)
        if self.plan.mode != "service_diagnostic":
            raise ValueError("owned quality requires the original service diagnostic plan")
        if type(self.budget) is not H2ByteBudget or type(self.program) is not QualityProgramPin:
            raise ValueError("exact owned quality budget and program required")
        self.budget.__post_init__()
        self.program.__post_init__()
        name = _text(self.retained_name, 255)
        if "/" in name or "\\" in name or not name.endswith(".quality"):
            raise ValueError("owned quality retained name must be one .quality basename")

    def to_payload(self) -> dict[str, Any]:
        return {**_data(self), "plan": _plan_payload(self.plan),
                "budget": self.budget.to_payload(), "program": self.program.to_payload()}

    @classmethod
    def from_payload(cls, value: object) -> Self:
        data = _record_payload(value, cls)
        plan = _closed(data["plan"], {"contract_version", "mode", "required_checks", "reason_policies"})
        _array(plan["required_checks"], 13)
        for policy in _array(plan["reason_policies"], 256, minimum=0):
            _closed(policy, {"reason", "disposition"})
        plan_raw = bounded_json_bytes(plan, maximum_bytes=_MAX_PLAN_BYTES)
        return cls(**{**data,
                      "plan": M6QualityPlan.from_canonical_bytes(plan_raw, maximum_bytes=_MAX_PLAN_BYTES),
                      "budget": H2ByteBudget.from_payload(data["budget"]),
                      "program": QualityProgramPin.from_payload(data["program"])})


QualityConfigValue = QualityCodeFilePin | QualityDependencyPin | QualityProgramPin | OwnedDiagnosticQualityConfig
_CONFIG_TYPES = (QualityCodeFilePin, QualityDependencyPin, QualityProgramPin, OwnedDiagnosticQualityConfig)
_QualityConfigT = TypeVar("_QualityConfigT", bound=QualityConfigValue)


def _require_quality_projection_budget(
    value: object, *, maximum_bytes: int, record_types: tuple[type[Any], ...] = (),
) -> None:
    """One shared preflight, with only the two original M6 model exceptions.

    All visited fields are retained on the wire. This conservative lower bound
    bounds expansion; the final canonical serializer charges exact JSON bytes.
    No model_dump, asdict, arbitrary iterator or projection method runs here.
    """
    if type(maximum_bytes) is not int or maximum_bytes < 1:
        raise ValueError("owned quality projection budget must be a positive integer")
    remaining = maximum_bytes

    def charge(size: int) -> None:
        nonlocal remaining
        if size > remaining:
            raise ValueError("owned quality shared projection byte budget exceeded")
        remaining -= size

    def visit(item: object, depth: int) -> None:
        if item is None:
            charge(4)
        elif type(item) is bool:
            charge(4 if item else 5)
        elif type(item) is int:
            if max(0, item.bit_length() - 1) // 4 > remaining:
                raise ValueError("owned quality shared projection byte budget exceeded")
            charge(len(str(item)))
        elif type(item) is str:
            if len(item) + 2 > remaining:
                raise ValueError("owned quality shared projection byte budget exceeded")
            charge(2)
            for start in range(0, len(item), 4096):
                charge(len(item[start:start + 4096].encode("utf-8")))
        else:
            if depth >= MAXIMUM_JSON_DEPTH:
                raise ValueError("owned quality projection nesting budget exceeded")
            charge(1)
            if type(item) is M6QualityPlan:
                plan = _plan_shape(item)
                for name in ("contract_version", "mode", "required_checks", "reason_policies"):
                    visit(getattr(plan, name), depth + 1)
            elif type(item) is M6ReasonPolicy:
                visit(item.reason, depth + 1)
                visit(item.disposition, depth + 1)
            elif type(item) in (*_CONFIG_TYPES, H2ByteBudget, *record_types):
                if type(item) is QualityProgramPin:
                    _program_shape(item)
                elif type(item) is QualityDependencyPin:
                    _tuple(item.files, 8192)
                    _tuple(item.import_names, 64)
                for field in fields(item):  # type: ignore[arg-type]
                    visit(getattr(item, field.name), depth + 1)
            elif type(item) is tuple:
                if len(item) > remaining:
                    raise ValueError("owned quality shared projection byte budget exceeded")
                for child in item:
                    visit(child, depth + 1)
            else:
                raise ValueError("owned quality projection requires exact supported values")

    visit(value, 0)


def encode_quality_config_value(value: QualityConfigValue, *, maximum_bytes: int) -> bytes:
    if type(value) not in _CONFIG_TYPES:
        raise ValueError("exact owned quality configuration value required")
    _require_quality_projection_budget(value, maximum_bytes=maximum_bytes)
    payload = value.to_payload()
    type(value).from_payload(payload)
    return bounded_json_bytes(payload, maximum_bytes=maximum_bytes)


def decode_quality_config_value(raw: bytes, record_type: type[_QualityConfigT], *, maximum_bytes: int) -> _QualityConfigT:
    if not any(record_type is known for known in _CONFIG_TYPES):
        raise ValueError("exact owned quality configuration decoder required")
    payload = bounded_json_value(raw, maximum_bytes=maximum_bytes)
    return cast(_QualityConfigT, record_type.from_payload(payload))


__all__ = [
    "QUALITY_WORKER_MODULE", "QualityCodeFilePin", "QualityDependencyPin", "QualityProgramPin",
    "OwnedDiagnosticQualityConfig", "QualityConfigValue", "encode_quality_config_value",
    "decode_quality_config_value",
]
