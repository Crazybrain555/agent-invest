"""Pure, closed MinerU task-protocol-v2 wire facts.

This module owns no HTTP client, retry loop, filesystem handle, durable receipt,
or lifecycle authority.  Both legacy and V4 transports may use these codecs,
but neither can turn a wire payload into durable state without its own exact
application-layer evidence checks.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from math import isfinite
import re
from typing import Any
from urllib.parse import SplitResult, quote, urljoin, urlsplit

from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.ports.parser import ParserOptions

TASK_PROTOCOL_V2 = "mineru-task-protocol.v2"
RETAINED_RESULT_V1 = "mineru-retained-result.v1"
STAGED_REQUEST_V1 = "mineru-staged-request.v1"
TASK_LOOKUP_REQUEST_V1 = "mineru-task-lookup-request.v1"
MAX_WIRE_JSON_BYTES = 1024 * 1024

_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDEMPOTENCY_KEY = re.compile(r"[0-9a-f]+\.[0-9a-f]{64}\Z")
_TASK_STATUSES = frozenset({"pending", "processing", "completed", "failed"})
_PROTOCOL_STATE_BY_TASK_STATUS = {
    "pending": frozenset({"pending"}),
    "processing": frozenset({"processing", "finalizing"}),
    "completed": frozenset({"completed"}),
    "failed": frozenset({"failed"}),
}
_SUBMISSION_FORM_FIELDS = frozenset(
    {
        "lang_list",
        "backend",
        "effort",
        "parse_method",
        "formula_enable",
        "table_enable",
        "image_analysis",
        "return_md",
        "return_middle_json",
        "return_model_output",
        "return_content_list",
        "return_images",
        "response_format_zip",
        "return_original_file",
        "client_side_output_generation",
        "start_page_id",
        "end_page_id",
        "server_url",
    }
)
TASK_PAYLOAD_FIELDS_V2 = frozenset(
    {
        "task_id",
        "status",
        "backend",
        "file_names",
        "created_at",
        "started_at",
        "completed_at",
        "error",
        "status_url",
        "result_url",
        "queued_ahead",
        "task_protocol_schema",
        "idempotency_key",
        "attempt_identity",
        "fence_identity",
        "result_artifact_schema",
        "result_artifact_sha256",
        "result_artifact_bytes",
        "result_artifact_owner",
        "protocol_state",
    }
)


class MinerUProtocolV2WireError(ValueError):
    """A request or response escaped the closed task-protocol-v2 envelope."""


class MinerUResultLeaseExpiredV2(MinerUProtocolV2WireError):
    """A structurally valid provider lease was stale when fully observed."""


@dataclass(frozen=True, slots=True)
class TaskProtocolV2Observation:
    task_id: str
    status: str
    protocol_state: str
    status_url: str
    result_url: str
    idempotency_key: str
    attempt_identity: str
    fence_identity: str
    artifact_sha256: str | None
    artifact_byte_count: int | None
    artifact_owner_identity: str | None
    provider_error: str | None

    def __post_init__(self) -> None:
        if (
            self.status not in _TASK_STATUSES
            or self.protocol_state
            not in _PROTOCOL_STATE_BY_TASK_STATUS.get(self.status, frozenset())
        ):
            raise MinerUProtocolV2WireError("task status is unsupported")
        for value, label in (
            (self.task_id, "task"),
            (self.attempt_identity, "attempt"),
            (self.fence_identity, "fence"),
        ):
            _identity(value, label)
        if _IDEMPOTENCY_KEY.fullmatch(self.idempotency_key) is None:
            raise MinerUProtocolV2WireError("idempotency key is invalid")
        terminal_shape = (
            self.artifact_sha256 is not None,
            self.artifact_byte_count is not None,
            self.artifact_owner_identity is not None,
        )
        if self.status == "completed":
            if terminal_shape != (True, True, True):
                raise MinerUProtocolV2WireError(
                    "completed task lacks retained-result identity"
                )
        elif terminal_shape != (False, False, False):
            raise MinerUProtocolV2WireError(
                "non-completed task invents retained-result identity"
            )
        if self.provider_error is not None and (
            type(self.provider_error) is not str
            or len(self.provider_error.encode("utf-8")) > 4096
        ):
            raise MinerUProtocolV2WireError("provider error is outside envelope")


@dataclass(frozen=True, slots=True)
class ResultLeaseV2:
    task_id: str
    lease_until_unix: float

    def __post_init__(self) -> None:
        _identity(self.task_id, "lease task")
        if (
            isinstance(self.lease_until_unix, bool)
            or not isinstance(self.lease_until_unix, (int, float))
            or not isfinite(float(self.lease_until_unix))
            or self.lease_until_unix <= 0
        ):
            raise MinerUProtocolV2WireError("result lease time is invalid")


def submission_form_v2(options: ParserOptions, *, server_url: str) -> dict[str, str]:
    if type(options) is not ParserOptions:
        raise MinerUProtocolV2WireError("parser options are not exact")
    _http_url(server_url, "VLM server URL", allow_path=True)
    return {
        "lang_list": options.language,
        "backend": options.backend,
        "effort": options.effective_effort or "medium",
        "parse_method": options.method,
        "formula_enable": str(options.formula).lower(),
        "table_enable": str(options.table).lower(),
        "image_analysis": str(options.effective_image_analysis).lower(),
        "return_md": "true",
        "return_middle_json": "true",
        "return_model_output": "true",
        "return_content_list": "true",
        "return_images": "true",
        "response_format_zip": "true",
        "return_original_file": "true",
        "client_side_output_generation": "false",
        "start_page_id": "0",
        "end_page_id": "99999",
        "server_url": server_url,
    }


def submission_request_exact_bytes_v2(
    *, api_origin: str, form: Mapping[str, str], upload_filename: str
) -> bytes:
    origin = normalize_api_origin_v2(api_origin)
    if (
        not isinstance(form, Mapping)
        or set(form) != _SUBMISSION_FORM_FIELDS
        or any(type(key) is not str or type(value) is not str for key, value in form.items())
    ):
        raise MinerUProtocolV2WireError("submission form is not closed")
    if (
        type(upload_filename) is not str
        or len(upload_filename) != 68
        or not upload_filename.endswith(".pdf")
        or _HEX_64.fullmatch(upload_filename[:-4]) is None
    ):
        raise MinerUProtocolV2WireError("upload filename is not canonical")
    return _canonical_json(
        {
            "schema": STAGED_REQUEST_V1,
            "api_origin": origin,
            "form": dict(form),
            "upload_filename": upload_filename,
        }
    )


def lookup_request_exact_bytes_v2(*, api_origin: str, idempotency_key: str) -> bytes:
    lookup_url = task_lookup_url_v2(
        api_origin=api_origin,
        idempotency_key=idempotency_key,
    )
    return _canonical_json(
        {
            "schema": TASK_LOOKUP_REQUEST_V1,
            "method": "GET",
            "url": lookup_url,
        }
    )


def task_lookup_url_v2(*, api_origin: str, idempotency_key: str) -> str:
    origin = normalize_api_origin_v2(api_origin)
    if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
        raise MinerUProtocolV2WireError("idempotency key is invalid")
    return f"{origin}/tasks/by-idempotency/{quote(idempotency_key, safe='')}"


def canonical_client_submit_key_v2(
    *,
    source_pdf_sha256: str,
    attempt_identity: str,
    fence_identity: str,
    submission_epoch_unix: int,
) -> str:
    """Derive the sole protocol-v2 key without sampling a recovery-time epoch."""

    if _SHA256.fullmatch(source_pdf_sha256) is None:
        raise MinerUProtocolV2WireError("source PDF identity is invalid")
    _identity(attempt_identity, "attempt")
    _identity(fence_identity, "fence")
    if (
        isinstance(submission_epoch_unix, bool)
        or not isinstance(submission_epoch_unix, int)
        or submission_epoch_unix < 0
    ):
        raise MinerUProtocolV2WireError("submission epoch is invalid")
    epoch_hex = format(submission_epoch_unix, "x")
    digest = hashlib.sha256(
        (
            f"{epoch_hex}\0{source_pdf_sha256}\0"
            f"{attempt_identity}\0{fence_identity}"
        ).encode("utf-8")
    ).hexdigest()
    return f"{epoch_hex}.{digest}"


def task_status_url_v2(*, api_origin: str, task_id: str) -> str:
    origin = normalize_api_origin_v2(api_origin)
    _identity(task_id, "lease task")
    return f"{origin}/tasks/{quote(task_id, safe='')}"


def task_result_url_v2(*, api_origin: str, task_id: str) -> str:
    return task_status_url_v2(api_origin=api_origin, task_id=task_id) + "/result"


def result_lease_url_v2(*, api_origin: str, task_id: str) -> str:
    return task_status_url_v2(api_origin=api_origin, task_id=task_id) + "/lease"


def task_ack_url_v2(*, api_origin: str, task_id: str) -> str:
    return task_status_url_v2(api_origin=api_origin, task_id=task_id) + "/ack"


def api_origin_from_task_routes_v2(
    *,
    status_url: str,
    result_url: str,
    task_id: str,
) -> str:
    """Recover an origin only from the one canonical status/result route pair."""

    _http_url(status_url, "status URL", allow_path=True)
    parsed = _split_http_url(status_url, "status URL")
    origin = normalize_api_origin_v2(f"{parsed.scheme}://{parsed.netloc}")
    if (
        status_url != task_status_url_v2(api_origin=origin, task_id=task_id)
        or result_url != task_result_url_v2(api_origin=origin, task_id=task_id)
    ):
        raise MinerUProtocolV2WireError("persisted task routes are not canonical")
    return origin


def normalize_api_origin_v2(value: str) -> str:
    _http_url(value, "API origin", allow_path=True)
    parsed = _split_http_url(value, "API origin")
    if parsed.path not in {"", "/"}:
        raise MinerUProtocolV2WireError("API origin must not contain a path")
    return value.rstrip("/")


def same_origin_url_v2(*, api_origin: str, value: str, label: str) -> str:
    origin = normalize_api_origin_v2(api_origin)
    if type(value) is not str or not value:
        raise MinerUProtocolV2WireError(f"{label} is invalid")
    resolved = urljoin(origin + "/", value)
    base = _split_http_url(origin, "API origin")
    target = _split_http_url(resolved, label)
    if (
        target.scheme != base.scheme
        or target.netloc != base.netloc
        or target.username is not None
        or target.password is not None
        or target.query
        or target.fragment
        or not target.path.startswith("/")
    ):
        raise MinerUProtocolV2WireError(f"{label} escaped the configured API origin")
    return resolved


def decode_closed_json_v2(
    exact_bytes: bytes,
    *,
    required: frozenset[str],
    allowed: frozenset[str] | None,
) -> dict[str, Any]:
    if (
        type(exact_bytes) is not bytes
        or len(exact_bytes) > MAX_WIRE_JSON_BYTES
    ):
        raise MinerUProtocolV2WireError("response JSON exceeds the wire envelope")
    try:
        value = strict_json_loads(exact_bytes)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise MinerUProtocolV2WireError("response is not strict JSON") from exc
    if (
        type(value) is not dict
        or not required.issubset(value)
        or (allowed is not None and not set(value).issubset(allowed))
    ):
        raise MinerUProtocolV2WireError("response JSON shape is not closed")
    return value


def validate_absence_payload_v2(exact_bytes: bytes) -> None:
    payload = decode_closed_json_v2(
        exact_bytes,
        required=frozenset({"detail"}),
        allowed=frozenset({"detail"}),
    )
    if payload["detail"] != "Task not found":
        raise MinerUProtocolV2WireError("lookup 404 is not the protocol absence proof")


def parse_task_payload_v2(
    exact_bytes: bytes,
    *,
    api_origin: str,
    idempotency_key: str,
    attempt_identity: str,
    fence_identity: str,
    expected_task_id: str | None = None,
    expected_status_url: str | None = None,
    expected_result_url: str | None = None,
    artifact_byte_limit: int | None = None,
) -> TaskProtocolV2Observation:
    payload = decode_closed_json_v2(
        exact_bytes,
        required=frozenset(
            {
                "task_id",
                "status",
                "status_url",
                "result_url",
                "task_protocol_schema",
                "idempotency_key",
                "attempt_identity",
                "fence_identity",
                "protocol_state",
            }
        ),
        allowed=TASK_PAYLOAD_FIELDS_V2,
    )
    expected = {
        "task_protocol_schema": TASK_PROTOCOL_V2,
        "idempotency_key": idempotency_key,
        "attempt_identity": attempt_identity,
        "fence_identity": fence_identity,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise MinerUProtocolV2WireError("task protocol identity drifted")
    task_id = payload["task_id"]
    status = payload["status"]
    protocol_state = payload["protocol_state"]
    if (
        type(task_id) is not str
        or type(status) is not str
        or type(protocol_state) is not str
        or protocol_state
        not in _PROTOCOL_STATE_BY_TASK_STATUS.get(status, frozenset())
    ):
        raise MinerUProtocolV2WireError("task status identity drifted")
    _identity(task_id, "task")
    if expected_task_id is not None and task_id != expected_task_id:
        raise MinerUProtocolV2WireError("task identity drifted")
    status_url = same_origin_url_v2(
        api_origin=api_origin,
        value=payload["status_url"],
        label="status URL",
    )
    result_url = same_origin_url_v2(
        api_origin=api_origin,
        value=payload["result_url"],
        label="result URL",
    )
    canonical_status = task_status_url_v2(
        api_origin=api_origin,
        task_id=task_id,
    )
    canonical_result = task_result_url_v2(
        api_origin=api_origin,
        task_id=task_id,
    )
    if status_url != canonical_status or result_url != canonical_result:
        raise MinerUProtocolV2WireError("task routes are not canonical")
    if expected_status_url is not None and status_url != expected_status_url:
        raise MinerUProtocolV2WireError("status URL drifted")
    if expected_result_url is not None and result_url != expected_result_url:
        raise MinerUProtocolV2WireError("result URL drifted")

    artifact_sha256: str | None = None
    artifact_byte_count: int | None = None
    artifact_owner: str | None = None
    if status == "completed":
        artifact_sha256 = payload.get("result_artifact_sha256")
        artifact_byte_count = payload.get("result_artifact_bytes")
        artifact_owner = payload.get("result_artifact_owner")
        if (
            payload.get("result_artifact_schema") != RETAINED_RESULT_V1
            or type(artifact_sha256) is not str
            or _HEX_64.fullmatch(artifact_sha256) is None
            or isinstance(artifact_byte_count, bool)
            or not isinstance(artifact_byte_count, int)
            or artifact_byte_count <= 0
            or type(artifact_owner) is not str
            or _HEX_64.fullmatch(artifact_owner) is None
            or artifact_owner
            != canonical_result_owner_v2(
                task_id=task_id,
                artifact_sha256=artifact_sha256,
                artifact_byte_count=artifact_byte_count,
            )
        ):
            raise MinerUProtocolV2WireError("retained result identity is invalid")
        if (
            artifact_byte_limit is not None
            and (
                isinstance(artifact_byte_limit, bool)
                or not isinstance(artifact_byte_limit, int)
                or artifact_byte_limit <= 0
                or artifact_byte_count > artifact_byte_limit
            )
        ):
            raise MinerUProtocolV2WireError("retained result exceeds exact allowance")
    elif any(
        payload.get(key) is not None
        for key in (
            "result_artifact_schema",
            "result_artifact_sha256",
            "result_artifact_bytes",
            "result_artifact_owner",
        )
    ):
        raise MinerUProtocolV2WireError(
            "non-completed task carries retained-result fields"
        )

    error = payload.get("error")
    if error is not None and type(error) is not str:
        raise MinerUProtocolV2WireError("provider task error is not text")
    if status == "failed" and not error:
        error = "MinerU remote task failed without provider detail"
    _validate_optional_task_fields_v2(payload)
    return TaskProtocolV2Observation(
        task_id=task_id,
        status=status,
        protocol_state=protocol_state,
        status_url=status_url,
        result_url=result_url,
        idempotency_key=idempotency_key,
        attempt_identity=attempt_identity,
        fence_identity=fence_identity,
        artifact_sha256=artifact_sha256,
        artifact_byte_count=artifact_byte_count,
        artifact_owner_identity=artifact_owner,
        provider_error=error,
    )


def parse_result_lease_v2(
    exact_bytes: bytes,
    *,
    task_id: str,
    observed_at_unix: float,
) -> ResultLeaseV2:
    payload = decode_closed_json_v2(
        exact_bytes,
        required=frozenset({"schema", "task_id", "lease_until_unix"}),
        allowed=frozenset({"schema", "task_id", "lease_until_unix"}),
    )
    if payload["schema"] != TASK_PROTOCOL_V2 or payload["task_id"] != task_id:
        raise MinerUProtocolV2WireError("result lease identity drifted")
    if (
        isinstance(observed_at_unix, bool)
        or not isinstance(observed_at_unix, (int, float))
        or not isfinite(float(observed_at_unix))
    ):
        raise MinerUProtocolV2WireError("lease observation time is invalid")
    lease = ResultLeaseV2(
        task_id=task_id,
        lease_until_unix=payload["lease_until_unix"],
    )
    if lease.lease_until_unix <= float(observed_at_unix):
        raise MinerUResultLeaseExpiredV2("result lease is already expired")
    return lease


def canonical_result_owner_v2(
    *, task_id: str, artifact_sha256: str, artifact_byte_count: int
) -> str:
    _identity(task_id, "result owner task")
    if (
        type(artifact_sha256) is not str
        or _HEX_64.fullmatch(artifact_sha256) is None
        or isinstance(artifact_byte_count, bool)
        or not isinstance(artifact_byte_count, int)
        or artifact_byte_count <= 0
    ):
        raise MinerUProtocolV2WireError("result owner inputs are invalid")
    return hashlib.sha256(
        f"{task_id}\0{artifact_sha256}\0{artifact_byte_count}".encode("utf-8")
    ).hexdigest()


def response_identity_v2(exact_bytes: bytes) -> tuple[str, int]:
    if type(exact_bytes) is not bytes or len(exact_bytes) > MAX_WIRE_JSON_BYTES:
        raise MinerUProtocolV2WireError("response is outside the wire envelope")
    return "sha256:" + hashlib.sha256(exact_bytes).hexdigest(), len(exact_bytes)


def _canonical_json(value: object) -> bytes:
    exact = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if not 1 <= len(exact) <= MAX_WIRE_JSON_BYTES:
        raise MinerUProtocolV2WireError("canonical wire request is outside envelope")
    return exact


def _identity(value: object, label: str) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value.encode("utf-8")) > 1024
        or any(ord(char) < 32 for char in value)
    ):
        raise MinerUProtocolV2WireError(f"{label} identity is invalid")


def _validate_optional_task_fields_v2(payload: Mapping[str, Any]) -> None:
    for name in ("backend", "created_at", "started_at", "completed_at"):
        value = payload.get(name)
        if value is not None and type(value) is not str:
            raise MinerUProtocolV2WireError(f"task {name} is invalid")
    file_names = payload.get("file_names")
    if file_names is not None and (
        type(file_names) is not list
        or any(type(value) is not str for value in file_names)
    ):
        raise MinerUProtocolV2WireError("task file names are invalid")
    queued_ahead = payload.get("queued_ahead")
    if queued_ahead is not None and (
        isinstance(queued_ahead, bool)
        or not isinstance(queued_ahead, int)
        or queued_ahead < 0
    ):
        raise MinerUProtocolV2WireError("task queued-ahead count is invalid")


def _split_http_url(value: str, label: str) -> SplitResult:
    try:
        parsed = urlsplit(value)
        _ = parsed.hostname
        _ = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise MinerUProtocolV2WireError(
            f"{label} is not a closed HTTP URL"
        ) from exc
    return parsed


def _http_url(value: object, label: str, *, allow_path: bool) -> None:
    if type(value) is not str or not value:
        raise MinerUProtocolV2WireError(f"{label} is invalid")
    parsed = _split_http_url(value, label)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (not allow_path and parsed.path not in {"", "/"})
    ):
        raise MinerUProtocolV2WireError(f"{label} is not a closed HTTP URL")


__all__ = [
    "MAX_WIRE_JSON_BYTES",
    "MinerUProtocolV2WireError",
    "MinerUResultLeaseExpiredV2",
    "RETAINED_RESULT_V1",
    "ResultLeaseV2",
    "STAGED_REQUEST_V1",
    "TASK_LOOKUP_REQUEST_V1",
    "TASK_PAYLOAD_FIELDS_V2",
    "TASK_PROTOCOL_V2",
    "TaskProtocolV2Observation",
    "api_origin_from_task_routes_v2",
    "canonical_client_submit_key_v2",
    "canonical_result_owner_v2",
    "decode_closed_json_v2",
    "lookup_request_exact_bytes_v2",
    "normalize_api_origin_v2",
    "parse_result_lease_v2",
    "parse_task_payload_v2",
    "response_identity_v2",
    "result_lease_url_v2",
    "same_origin_url_v2",
    "submission_form_v2",
    "submission_request_exact_bytes_v2",
    "task_lookup_url_v2",
    "task_result_url_v2",
    "task_status_url_v2",
    "validate_absence_payload_v2",
]
