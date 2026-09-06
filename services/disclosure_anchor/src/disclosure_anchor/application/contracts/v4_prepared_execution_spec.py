"""Closed immutable execution identity for one prepared remote V4 attempt."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import base64
import binascii
import hashlib
import json
import re
from typing import Any, cast
from urllib.parse import urlsplit

from disclosure_anchor.application.contracts.mineru_process_profile import (
    MineruProcessProfile,
    decode_mineru_process_profile,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import (
    StagedWorkerProfileV4, decode_staged_worker_profile_v4,
)
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.staged_provider_parser import (
    PreparedSubmissionIdentity,
)


V4_PREPARED_EXECUTION_SPEC_CONTRACT = "v4-prepared-execution-spec.v2"
MAX_V4_PREPARED_EXECUTION_SPEC_BYTES = 512 * 1024

_MAX_REQUEST_BYTES = 64 * 1024
_MAX_IDENTITY_BYTES = 1024
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_INT32 = (1 << 31) - 1
_MAX_INT64 = (1 << 63) - 1
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SPEC_FIELDS = frozenset(
    {
        "contract_version",
        "prepared_submission_exact_b64",
        "prepared_submission_sha256",
        "parser_identity",
        "parser_options",
        "api_origin",
        "server_url",
        "request_exact_b64",
        "request_sha256",
        "process_profile_exact_b64",
        "process_profile_sha256",
        "worker_profile_exact_b64",
        "worker_profile_sha256",
        "result_lease_seconds",
        "remote_runaway_seconds",
        "archive_member_count_limit",
        "archive_uncompressed_byte_limit",
    }
)
_PARSER_IDENTITY_FIELDS = frozenset({"name", "version"})
_PARSER_OPTIONS_FIELDS = frozenset(
    {
        "method",
        "backend",
        "language",
        "formula",
        "table",
        "effort",
        "image_analysis",
        "start_page",
        "end_page",
        "timeout_seconds",
        "api_url",
        "api_drain_timeout_seconds",
        "server_url",
        "http_request_concurrency",
        "runtime_bundle_identity_sha256",
    }
)
_PREPARED_FIELDS = frozenset(
    {
        "schema",
        "attempt_identity",
        "fence_identity",
        "source_pdf_sha256",
        "parser_target_identity_sha256",
        "runtime_bundle_identity_sha256",
        "request_sha256",
        "client_submit_key",
        "submission_epoch_unix",
    }
)


@dataclass(frozen=True, slots=True)
class V4PreparedExecutionSpec:
    """Every execution-affecting input fixed before the first remote POST."""

    contract_version: str
    prepared_submission: PreparedSubmissionIdentity
    parser_identity: ParserIdentity
    parser_options: ParserOptions
    api_origin: str
    server_url: str
    request_exact_bytes: bytes = field(repr=False)
    request_sha256: str
    process_profile_exact_bytes: bytes = field(repr=False)
    process_profile_sha256: str
    worker_profile: StagedWorkerProfileV4
    result_lease_seconds: int
    remote_runaway_seconds: int
    archive_member_count_limit: int
    archive_uncompressed_byte_limit: int

    def __post_init__(self) -> None:
        if self.contract_version != V4_PREPARED_EXECUTION_SPEC_CONTRACT:
            raise ValueError("V4 prepared execution spec contract is unsupported")
        if type(self.prepared_submission) is not PreparedSubmissionIdentity:
            raise ValueError("V4 prepared submission identity must be exact")
        if type(self.parser_identity) is not ParserIdentity:
            raise ValueError("V4 parser identity must be exact")
        if type(self.parser_options) is not ParserOptions:
            raise ValueError("V4 parser options must be exact")
        _validate_parser(self.parser_identity, self.parser_options)
        _validate_api_origin(self.api_origin)
        _validate_server_url(self.server_url)
        if self.parser_options.api_url != self.api_origin:
            raise ValueError("V4 API origin drifted from parser options")
        if self.parser_options.server_url != self.server_url:
            raise ValueError("V4 server URL drifted from parser options")
        _require_exact_bytes(
            self.request_exact_bytes,
            label="V4 request",
            maximum=_MAX_REQUEST_BYTES,
        )
        _require_sha256(self.request_sha256, "V4 request")
        if self.request_sha256 != _digest(self.request_exact_bytes):
            raise ValueError("V4 request exact bytes drifted from its digest")
        if self.prepared_submission.request_sha256 != self.request_sha256:
            raise ValueError("V4 request drifted from prepared submission")

        _require_exact_bytes(
            self.process_profile_exact_bytes,
            label="V4 process profile",
            maximum=64 * 1024,
        )
        _require_sha256(self.process_profile_sha256, "V4 process profile")
        if self.process_profile_sha256 != _digest(self.process_profile_exact_bytes):
            raise ValueError("V4 process profile exact bytes drifted from its digest")
        profile = decode_mineru_process_profile(self.process_profile_exact_bytes)
        if profile.sha256 != self.process_profile_sha256:
            raise ValueError("V4 process profile canonical identity drifted")
        if (type(self.worker_profile) is not StagedWorkerProfileV4
                or self.worker_profile.process_profile_sha256 != profile.sha256):
            raise ValueError("V4 worker composition does not bind the exact process profile")
        runtime_hash = self.parser_options.runtime_bundle_identity_sha256
        if (
            runtime_hash != self.prepared_submission.runtime_bundle_identity_sha256
            or runtime_hash != profile.runtime_bundle_identity_sha256
        ):
            raise ValueError("V4 runtime bundle identity is not closed")

        target = self.parser_options.target_identity(self.parser_identity)
        target_sha256 = _digest(
            json.dumps(
                target.to_payload(),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        if target_sha256 != self.prepared_submission.parser_target_identity_sha256:
            raise ValueError("V4 parser target drifted from prepared submission")

        _positive_bounded(
            self.result_lease_seconds,
            label="V4 result lease",
            maximum=3600,
        )
        _positive_bounded(
            self.remote_runaway_seconds,
            label="V4 remote runaway",
            maximum=_MAX_INT32,
        )
        _positive_bounded(
            self.archive_member_count_limit,
            label="V4 archive member limit",
            maximum=_MAX_ARCHIVE_MEMBERS,
        )
        _positive_bounded(
            self.archive_uncompressed_byte_limit,
            label="V4 archive uncompressed limit",
            maximum=_MAX_INT64,
        )
        if self.archive_uncompressed_byte_limit > profile.temporary_disk_bytes_limit:
            raise ValueError("V4 archive limit exceeds the frozen temporary-disk budget")

    @property
    def exact_bytes(self) -> bytes:
        return encode_v4_prepared_execution_spec(self)

    @property
    def sha256(self) -> str:
        return _digest(self.exact_bytes)

    @property
    def byte_count(self) -> int:
        return len(self.exact_bytes)

    @property
    def process_profile(self) -> MineruProcessProfile:
        return decode_mineru_process_profile(self.process_profile_exact_bytes)


def encode_v4_prepared_execution_spec(spec: V4PreparedExecutionSpec) -> bytes:
    """Return the sole canonical representation of an exact spec."""

    if type(spec) is not V4PreparedExecutionSpec:
        raise ValueError("V4 prepared execution spec must be exact")
    payload: dict[str, Any] = {
        "contract_version": spec.contract_version,
        "prepared_submission_exact_b64": _encode_bytes(
            spec.prepared_submission.exact_bytes
        ),
        "prepared_submission_sha256": spec.prepared_submission.sha256,
        "parser_identity": asdict(spec.parser_identity),
        "parser_options": asdict(spec.parser_options),
        "api_origin": spec.api_origin,
        "server_url": spec.server_url,
        "request_exact_b64": _encode_bytes(spec.request_exact_bytes),
        "request_sha256": spec.request_sha256,
        "process_profile_exact_b64": _encode_bytes(
            spec.process_profile_exact_bytes
        ),
        "process_profile_sha256": spec.process_profile_sha256,
        "worker_profile_exact_b64": _encode_bytes(spec.worker_profile.exact_bytes),
        "worker_profile_sha256": spec.worker_profile.sha256,
        "result_lease_seconds": spec.result_lease_seconds,
        "remote_runaway_seconds": spec.remote_runaway_seconds,
        "archive_member_count_limit": spec.archive_member_count_limit,
        "archive_uncompressed_byte_limit": spec.archive_uncompressed_byte_limit,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_V4_PREPARED_EXECUTION_SPEC_BYTES:
        raise ValueError("V4 prepared execution spec exceeds the closed envelope")
    return encoded


def decode_v4_prepared_execution_spec(payload: bytes) -> V4PreparedExecutionSpec:
    """Decode exact canonical bytes and reject aliases, omissions, and extras."""

    _require_exact_bytes(
        payload,
        label="V4 prepared execution spec",
        maximum=MAX_V4_PREPARED_EXECUTION_SPEC_BYTES,
    )
    try:
        decoded = strict_json_loads(payload)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("V4 prepared execution spec is not strict UTF-8 JSON") from exc
    if type(decoded) is not dict or set(decoded) != _SPEC_FIELDS:
        raise ValueError("V4 prepared execution spec fields are not closed")
    value = cast(dict[str, object], decoded)
    parser_identity_value = value["parser_identity"]
    parser_options_value = value["parser_options"]
    if (
        type(parser_identity_value) is not dict
        or set(parser_identity_value) != _PARSER_IDENTITY_FIELDS
    ):
        raise ValueError("V4 parser identity fields are not closed")
    if (
        type(parser_options_value) is not dict
        or set(parser_options_value) != _PARSER_OPTIONS_FIELDS
    ):
        raise ValueError("V4 parser option fields are not closed")
    prepared_bytes = _decode_bytes(
        value["prepared_submission_exact_b64"],
        label="V4 prepared submission",
        maximum=65_536,
    )
    prepared_sha256 = value["prepared_submission_sha256"]
    _require_sha256(prepared_sha256, "V4 prepared submission")
    prepared = _decode_prepared_submission(prepared_bytes, prepared_sha256)
    request = _decode_bytes(
        value["request_exact_b64"],
        label="V4 request",
        maximum=_MAX_REQUEST_BYTES,
    )
    process_profile = _decode_bytes(
        value["process_profile_exact_b64"],
        label="V4 process profile",
        maximum=64 * 1024,
    )
    worker_profile = decode_staged_worker_profile_v4(_decode_bytes(
        value["worker_profile_exact_b64"], label="V4 worker profile", maximum=4096,
    ))
    if worker_profile.sha256 != value["worker_profile_sha256"]:
        raise ValueError("V4 worker profile exact bytes drifted from its digest")
    try:
        identity = ParserIdentity(**cast(dict[str, Any], parser_identity_value))
        options = ParserOptions(**cast(dict[str, Any], parser_options_value))
        spec = V4PreparedExecutionSpec(
            contract_version=cast(str, value["contract_version"]),
            prepared_submission=prepared,
            parser_identity=identity,
            parser_options=options,
            api_origin=cast(str, value["api_origin"]),
            server_url=cast(str, value["server_url"]),
            request_exact_bytes=request,
            request_sha256=cast(str, value["request_sha256"]),
            process_profile_exact_bytes=process_profile,
            process_profile_sha256=cast(str, value["process_profile_sha256"]),
            worker_profile=worker_profile,
            result_lease_seconds=cast(int, value["result_lease_seconds"]),
            remote_runaway_seconds=cast(int, value["remote_runaway_seconds"]),
            archive_member_count_limit=cast(
                int, value["archive_member_count_limit"]
            ),
            archive_uncompressed_byte_limit=cast(
                int, value["archive_uncompressed_byte_limit"]
            ),
        )
    except TypeError as exc:
        raise ValueError("V4 prepared execution spec field types are invalid") from exc
    if spec.exact_bytes != payload:
        raise ValueError("V4 prepared execution spec bytes are not canonical")
    return spec


def _decode_prepared_submission(
    payload: bytes,
    expected_sha256: object,
) -> PreparedSubmissionIdentity:
    try:
        decoded = strict_json_loads(payload)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("V4 prepared submission is not strict UTF-8 JSON") from exc
    if type(decoded) is not dict or set(decoded) != _PREPARED_FIELDS:
        raise ValueError("V4 prepared submission fields are not closed")
    values = cast(dict[str, Any], decoded)
    try:
        return PreparedSubmissionIdentity(
            **values,
            exact_bytes=payload,
            sha256=cast(str, expected_sha256),
        )
    except TypeError as exc:
        raise ValueError("V4 prepared submission field types are invalid") from exc


def _validate_parser(identity: ParserIdentity, options: ParserOptions) -> None:
    for text_value, text_label in (
        (identity.name, "name"),
        (identity.version, "version"),
    ):
        if (
            type(text_value) is not str
            or not text_value
            or len(text_value.encode("utf-8")) > _MAX_IDENTITY_BYTES
        ):
            raise ValueError(f"V4 parser {text_label} is invalid")
    # ParserTargetIdentity closes every content-affecting value and its relations.
    options.target_identity(identity)
    for optional_count, count_label in (
        (options.timeout_seconds, "timeout"),
        (options.http_request_concurrency, "HTTP request concurrency"),
    ):
        if optional_count is not None:
            _positive_bounded(
                optional_count,
                label=f"V4 parser {count_label}",
                maximum=_MAX_INT32,
            )
    _positive_bounded(
        options.api_drain_timeout_seconds,
        label="V4 parser API drain timeout",
        maximum=_MAX_INT32,
    )
    _require_sha256(options.runtime_bundle_identity_sha256, "V4 parser runtime bundle")


def _validate_api_origin(value: object) -> None:
    if type(value) is not str or not value or len(value) > 4096:
        raise ValueError("V4 API origin is invalid")
    parsed = urlsplit(value)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("V4 API origin port is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("V4 API origin must be one closed HTTP origin")


def _validate_server_url(value: object) -> None:
    if type(value) is not str or not value or len(value) > 4096:
        raise ValueError("V4 server URL is invalid")
    parsed = urlsplit(value)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("V4 server URL port is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("V4 server URL must be one closed HTTP URL")


def _encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_bytes(value: object, *, label: str, maximum: int) -> bytes:
    if type(value) is not str or not value:
        raise ValueError(f"{label} base64 is invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{label} base64 is invalid") from exc
    if _encode_bytes(decoded) != value:
        raise ValueError(f"{label} base64 is not canonical")
    _require_exact_bytes(decoded, label=label, maximum=maximum)
    return decoded


def _require_exact_bytes(value: object, *, label: str, maximum: int) -> None:
    if type(value) is not bytes or not value or len(value) > maximum:
        raise ValueError(f"{label} bytes are outside the closed envelope")


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be canonical sha256")


def _positive_bounded(value: object, *, label: str, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be within 1..{maximum}")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


__all__ = [
    "MAX_V4_PREPARED_EXECUTION_SPEC_BYTES",
    "V4_PREPARED_EXECUTION_SPEC_CONTRACT",
    "V4PreparedExecutionSpec",
    "decode_v4_prepared_execution_spec",
    "encode_v4_prepared_execution_spec",
]
