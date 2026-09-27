"""Durable state machine for the sole MinerU staged-task protocol."""

from __future__ import annotations

import asyncio
import contextvars
import copy
import errno
import gc
import hashlib
import hmac
import json
import os
import stat
import sys
import tempfile
import threading
import time
import zipfile
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, fields
from functools import wraps
from pathlib import Path
from threading import RLock, get_ident
from typing import Any, Literal

TaskState = Literal[
    "ingress", "ingress_cleanup", "pending", "processing", "finalizing", "completed", "failed",
    "cleanup_pending", "consumed"
]
CleanupKind = Literal["result", "task_tree"]
_MAX_REGISTRY_BYTES = 16 * 1024 * 1024
_MAX_RECORDS = 128
_MAX_TOMBSTONES = 8192
_MAX_TASK_PAYLOAD_BYTES = 64 * 1024
_MAX_CLOCK_SKEW_SECONDS = 300


class TaskProtocolConflict(RuntimeError):
    pass


class TaskRegistryObservationBusy(TaskProtocolConflict):
    """A fresh consistent read could not obtain the data lock within its budget."""


class TaskAdmissionFull(TaskProtocolConflict):
    """A new key was rejected before it acquired ingress responsibility."""


class TaskResultCapacityFull(TaskProtocolConflict):
    """Accepted work must wait for result responsibility to be released."""


class TaskResultCapacityRecoveryRequired(TaskProtocolConflict):
    """Existing result responsibility must be reconciled before more parsing."""


class TaskExecutionStopped(TaskProtocolConflict):
    """Parsing did not start; the accepted pending responsibility is retained."""

    def __init__(self, message: str, *, capacity_wait: bool = False) -> None:
        super().__init__(message)
        self.capacity_wait = capacity_wait


class TaskStorageWait(TaskProtocolConflict):
    """A storage grant is not available now; nothing was committed or written."""

    def __init__(self, reason: str) -> None:
        if reason not in STORAGE_WAIT_REASONS or reason in STORAGE_BLOCKED_REASONS:
            raise ValueError("task storage wait reason is outside the closed vocabulary")
        super().__init__("task storage grant waits: " + reason)
        self.reason = reason


class TaskStorageBlocked(TaskProtocolConflict):
    """The accepted task is held with its bytes for an explicit operator decision."""

    def __init__(self, reason: str) -> None:
        if reason not in STORAGE_BLOCKED_REASONS:
            raise ValueError("task storage block reason is outside the closed vocabulary")
        super().__init__("task storage is blocked: " + reason)
        self.reason = reason


class SourceGrowthLimitExceeded(RuntimeError):
    """A parser write was refused before it could exceed the growth permit."""


class SourceTreeIntegrityError(SourceGrowthLimitExceeded, TaskProtocolConflict):
    """A parser write was refused because its path, root or leaf is not the owned tree.

    An integrity refusal, not byte exhaustion: the task is held as
    ``tree_integrity``. It remains a growth refusal for callers that only need
    to stop writing.
    """


class ResultGrantExceeded(RuntimeError):
    """A ZIP write was refused before its extent could exceed the grant."""


class TaskRegistryPersistenceError(OSError):
    """A content-free registry persistence outcome with an explicit commit boundary."""

    def __init__(
        self,
        *,
        operation: str,
        phase: str,
        outcome: str,
        committed: bool,
    ) -> None:
        self.operation = operation
        self.phase = phase
        self.outcome = outcome
        self.committed = committed
        super().__init__(
            "task registry persistence "
            f"{outcome} during {operation} at {phase}"
        )


TASK_FAILURE_CAUSE_SCHEMA = "mineru-task-failure-cause.v1"
# The generated http_client's final-request owner attaches this literal marker
# to the exception instance that left the VLM request; nothing else is evidence.
VLM_REQUEST_FAILURE_ATTRIBUTE = "_agent_vlm_request_failure"
VLM_REQUEST_FAILURE_SCHEMA = "mineru-vlm-request-failure.v1"
TRANSIENT_VLM_HTTP_STATUSES = frozenset({429, 502, 503, 504})
TRANSIENT_VLM_TRANSPORT_ERRORS = frozenset(
    {"ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout"}
)
VLM_TRANSPORT_ERRORS = TRANSIENT_VLM_TRANSPORT_ERRORS | frozenset(
    {
        "ReadError",
        "WriteError",
        "CloseError",
        "RemoteProtocolError",
        "LocalProtocolError",
        "ProxyError",
        "UnsupportedProtocol",
        "TransportError",
    }
)
_TASK_FAILURE_CAUSE_FIELDS = frozenset(
    {"schema", "task_id", "retry_class", "code", "http_status", "transport_error"}
)

# Result storage (capacity config v2 only). One closed record per accepted task
# carries its physical occupancy and phase; waits and blocks are reasons inside
# the phase, never a failed task. ``admitted`` owns only its uploads.
RETAINED_RESULT_NAME = ".retained-result.zip"
RETAINED_RESULT_PART_NAME = ".retained-result.zip.part"
RETAINED_INVENTORY_NAME = ".retained-inventory.json"
REGISTRY_SCHEMA_V3 = "mineru-task-registry.v3"
REGISTRY_SCHEMA_V4 = "mineru-task-registry.v4"
TASK_STORAGE_SCHEMA = "mineru.task-storage.v1"
TASK_STORAGE_STATUS_SCHEMA = "mineru.task-storage-status.v1"
RESULT_INVENTORY_SCHEMA = "mineru.result-inventory.v1"
STORAGE_PHASES = ("admitted", "source_growing", "source_sealed", "zip_writing", "zip_sealed")
STORAGE_BLOCKED_REASONS = frozenset(
    {"hard_envelope_exceeded", "codec_bound_exceeded", "seal_integrity", "tree_integrity"}
)
STORAGE_WAIT_REASONS = frozenset(
    {"source_growth_capacity", "completion_capacity", "free_floor", *STORAGE_BLOCKED_REASONS}
)
# An operator's explicit terminal decision for one held task, never automatic
# and never a resume: the task fails with a closed cause naming the decision and
# keeps its bytes and seal until the ordinary failed-task ACK removes its tree.
STORAGE_HOLD_PREVIEW_SCHEMA = "mineru.storage-hold-preview.v1"
STORAGE_HOLD_DECISION_SCHEMA = "mineru.storage-hold-decision.v1"
STORAGE_HOLD_RECEIPT_SCHEMA = "mineru.storage-hold-decision-receipt.v1"
STORAGE_HOLD_TERMINATED_CODE = "storage_hold_terminated"
# The cause keeps the operator's canonical decision beside its digest, so a
# lost response or a later reader recovers the exact attribution, not a hash.
_STORAGE_HOLD_CAUSE_FIELDS = _TASK_FAILURE_CAUSE_FIELDS | frozenset(
    {"hold_reason", "decision_sha256", "decision"}
)
_STORAGE_HOLD_DECISION_FIELDS = frozenset({"schema", "preview_sha256", "decided_by", "reason", "fixed_by"})
_MAX_DECISION_TEXT = 512
# The hold route is shared with ordinary work (one TCP/SSH origin), so only a
# presented operator credential reaches it. Its verifier (the credential's
# sha256, never the credential) lives in the container's own filesystem, not a
# bind mount: only an operator with docker exec on the host enrolls it, and a
# recreated container starts with the route disabled.
STORAGE_HOLD_OPERATOR_SCHEMA = "mineru.storage-hold-operator.v1"
STORAGE_HOLD_OPERATOR_PATH = Path("/run/agent-invest-operator/storage-hold-operator.json")
_MAX_OPERATOR_FILE_BYTES = 512
_TASK_STORAGE_FIELDS = frozenset(
    {
        "schema", "policy_sha256", "phase", "upload_bytes", "growth_permit_bytes",
        "source_bytes", "selected_bytes", "member_count", "inventory_sha256",
        "zip_upper_bound_bytes", "zip_grant_bytes", "zip_bytes", "wait_reason",
        "wait_since_unix",
    }
)
_STORAGE_INT_FIELDS = (
    "upload_bytes", "growth_permit_bytes", "source_bytes", "selected_bytes",
    "member_count", "zip_upper_bound_bytes", "zip_grant_bytes", "zip_bytes",
)
_MAX_STORAGE_INT = (1 << 63) - 1


def _canonical_sha256(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _storage_sha256(value: object, *, label: str) -> str:
    if (
        type(value) is not str or len(value) != 71 or not value.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise TaskProtocolConflict(f"task storage {label} is not a canonical sha256")
    return value


class StorageHoldOperatorRefused(Exception):
    """The operator gate refused the caller; nothing else was read or run."""

    def __init__(self, code: str, *, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _is_operator_verifier(value: object) -> bool:
    return (
        type(value) is str and len(value) == 71 and value.startswith("sha256:")
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def storage_hold_operator_verifier(credential: str) -> str:
    """The enrolled identity of one operator credential: its sha256, never the credential."""
    return "sha256:" + hashlib.sha256(credential.encode("latin-1")).hexdigest()


def _misconfigured_operator() -> StorageHoldOperatorRefused:
    return StorageHoldOperatorRefused("storage_hold_operator_misconfigured", status=403)


def _read_storage_hold_operator_verifier(path: Path) -> str | None:
    """The enrolled verifier, or None when none is enrolled; anything unsafe refuses."""
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise _misconfigured_operator() from None
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise _misconfigured_operator()
        try:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory,
            )
        except FileNotFoundError:
            return None
        except OSError:
            raise _misconfigured_operator() from None
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                or not 0 < metadata.st_size <= _MAX_OPERATOR_FILE_BYTES
            ):
                raise _misconfigured_operator()
            raw = os.read(descriptor, _MAX_OPERATOR_FILE_BYTES + 1)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    try:
        document = json.loads(raw)
    except ValueError:
        document = None
    if (
        len(raw) != metadata.st_size or type(document) is not dict
        or set(document) != {"schema", "credential_sha256"}
        or document["schema"] != STORAGE_HOLD_OPERATOR_SCHEMA
        or not _is_operator_verifier(document["credential_sha256"])
    ):
        raise _misconfigured_operator()
    return document["credential_sha256"]


def require_storage_hold_operator(authorization: str | None, *, path: Path | None = None) -> None:
    """Admit only a bearer of the enrolled operator credential, before anything else runs.

    Disabled (403) while no verifier is enrolled or the enrollment is unsafe;
    unauthorized (401) for a missing, malformed or wrong credential. The digests
    are compared in constant time.
    """
    verifier = _read_storage_hold_operator_verifier(
        STORAGE_HOLD_OPERATOR_PATH if path is None else path
    )
    if verifier is None:
        raise StorageHoldOperatorRefused("storage_hold_operator_disabled", status=403)
    scheme, _, presented = (authorization or "").partition(" ")
    presented = presented.strip()
    candidate = ""
    if scheme.lower() == "bearer" and presented:
        try:
            candidate = storage_hold_operator_verifier(presented)
        except UnicodeEncodeError:
            candidate = ""
    if not hmac.compare_digest(candidate.encode("ascii"), verifier.encode("ascii")):
        raise StorageHoldOperatorRefused("storage_hold_operator_unauthorized", status=401)


def enroll_storage_hold_operator(credential_sha256: str, *, path: Path | None = None) -> dict[str, str]:
    """Operator only, inside the API container: enable the hold route for one credential.

    Takes the credential's verifier, never the credential, and atomically
    replaces any earlier enrollment. Revoke, or recreate the container, to disable.
    """
    if not _is_operator_verifier(credential_sha256):
        raise ValueError("operator credential verifier must be sha256:<64 lowercase hex>")
    target = STORAGE_HOLD_OPERATOR_PATH if path is None else path
    document = {"schema": STORAGE_HOLD_OPERATOR_SCHEMA, "credential_sha256": credential_sha256}
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    try:
        os.mkdir(target.parent, 0o700)
    except FileExistsError:
        pass
    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(directory).st_uid != os.geteuid():
            raise ValueError("operator credential directory belongs to another user")
        os.fchmod(directory, 0o700)
        temporary = f".{target.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
            dir_fd=directory,
        )
        try:
            try:
                os.fchmod(descriptor, 0o600)
                view = memoryview(payload)
                while view:
                    view = view[os.write(descriptor, view):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, target.name, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            raise
        os.fsync(directory)
    finally:
        os.close(directory)
    return document


def revoke_storage_hold_operator(*, path: Path | None = None) -> bool:
    """Disable the hold route; True when an enrollment was removed."""
    target = STORAGE_HOLD_OPERATOR_PATH if path is None else path
    try:
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            os.unlink(target.name, dir_fd=directory)
        except FileNotFoundError:
            return False
        os.fsync(directory)
        return True
    finally:
        os.close(directory)


def validate_task_storage(value: object, *, state: str) -> dict[str, Any]:
    """Return one closed storage record consistent with its task state."""
    if type(value) is not dict or set(value) != _TASK_STORAGE_FIELDS:
        raise TaskProtocolConflict("task storage fields are not closed")
    if value["schema"] != TASK_STORAGE_SCHEMA:
        raise TaskProtocolConflict("task storage schema is unsupported")
    _storage_sha256(value["policy_sha256"], label="policy")
    phase = value["phase"]
    if phase not in STORAGE_PHASES:
        raise TaskProtocolConflict("task storage phase is unsupported")
    for name in _STORAGE_INT_FIELDS:
        item = value[name]
        if type(item) is not int or not 0 <= item <= _MAX_STORAGE_INT:
            raise TaskProtocolConflict(f"task storage {name} is invalid")
    reason = value["wait_reason"]
    since = value["wait_since_unix"]
    if (reason is None) != (since is None):
        raise TaskProtocolConflict("task storage wait reason and time must be paired")
    if reason is not None and reason not in STORAGE_WAIT_REASONS:
        raise TaskProtocolConflict("task storage wait reason is unsupported")
    if since is not None and (
        isinstance(since, bool) or not isinstance(since, (int, float)) or since < 0
    ):
        raise TaskProtocolConflict("task storage wait time is invalid")
    sealed = phase in {"source_sealed", "zip_writing", "zip_sealed"}
    inventory = value["inventory_sha256"]
    if sealed:
        _storage_sha256(inventory, label="inventory")
        if value["member_count"] < 1 or value["zip_upper_bound_bytes"] < 1:
            raise TaskProtocolConflict("sealed task storage lacks its inventory facts")
    elif inventory is not None or any(
        value[name] for name in ("source_bytes", "selected_bytes", "member_count", "zip_upper_bound_bytes")
    ):
        raise TaskProtocolConflict("unsealed task storage carries sealed facts")
    if (phase == "source_growing") != (value["growth_permit_bytes"] > 0):
        raise TaskProtocolConflict("task storage growth permit escaped its phase")
    if (phase == "zip_writing") != (value["zip_grant_bytes"] > 0):
        raise TaskProtocolConflict("task storage completion grant escaped its phase")
    if (phase == "zip_sealed") != (value["zip_bytes"] > 0):
        raise TaskProtocolConflict("task storage sealed result escaped its phase")
    if phase == "zip_writing" and value["zip_grant_bytes"] > value["zip_upper_bound_bytes"]:
        raise TaskProtocolConflict("task storage completion grant exceeds its bound")
    allowed_phases = {
        "pending": {"admitted", "source_growing"},
        "processing": {"source_growing"},
        "finalizing": {"source_sealed", "zip_writing"},
        "completed": {"zip_sealed"},
        "failed": set(STORAGE_PHASES),
        "cleanup_pending": set(STORAGE_PHASES),
    }.get(state)
    if allowed_phases is None or phase not in allowed_phases:
        raise TaskProtocolConflict("task storage phase contradicts the task state")
    if reason in STORAGE_BLOCKED_REASONS and state not in {"processing", "finalizing"}:
        raise TaskProtocolConflict("a storage block belongs to accepted in-flight work")
    return value


def _require_storage_amount(value: object, label: str) -> None:
    if type(value) is not int or not 0 <= value <= _MAX_STORAGE_INT:
        raise ValueError(f"task storage {label} must be a bounded non-negative integer")


def storage_occupancy(storage: dict[str, Any], physical: Callable[[int], int]) -> tuple[int, int]:
    """(source-pool bytes, result bytes) one record owns or has been promised.

    Source amounts are recorded as physical charges already. A ZIP extent is
    logical, so it is charged at its allocation-rounded size plus file overhead.
    """
    phase = storage["phase"]
    source = storage["upload_bytes"] + (
        storage["growth_permit_bytes"] if phase == "source_growing" else storage["source_bytes"]
    )
    extent = storage["zip_grant_bytes"] if phase == "zip_writing" else storage["zip_bytes"]
    return source, physical(extent) if extent else 0


def storage_status_payload(record: "DurableTaskRecord") -> dict[str, Any] | None:
    """The closed wire projection of one storage-managed task, else None.

    Carries what a consumer needs to size and verify its own work (selected
    S/M, inventory and policy identity, the sealed ZIP extent) and makes a
    capacity wait or an operator hold visible without turning it into failure.
    """
    storage = record.storage
    if storage is None:
        return None
    return {
        "schema": TASK_STORAGE_STATUS_SCHEMA,
        "policy_sha256": storage["policy_sha256"],
        "phase": storage["phase"],
        "wait_reason": storage["wait_reason"],
        "wait_since_unix": storage["wait_since_unix"],
        "blocked": storage["wait_reason"] in STORAGE_BLOCKED_REASONS,
        "selected_bytes": storage["selected_bytes"],
        "member_count": storage["member_count"],
        "inventory_sha256": storage["inventory_sha256"],
        "zip_bytes": storage["zip_bytes"],
    }


def _vlm_http_status_retry_class(status: int) -> str:
    if status in TRANSIENT_VLM_HTTP_STATUSES:
        return "transient"
    if 400 <= status <= 499 and status not in {408, 425}:
        return "permanent"
    return "unknown"


def _exception_note_count(failure: BaseException) -> int:
    notes = getattr(failure, "__notes__", None)
    return len(notes) if type(notes) is list else 0


def task_failure_cause(failure: BaseException, *, task_id: str) -> dict[str, Any]:
    """Classify one terminal task failure from its typed request origin only.

    Messages and exception classes are never evidence. A marker whose explicit
    cause or notes changed after the request owner set it now also carries a
    later secondary failure, so it no longer proves a transient request outcome.
    """
    cause: dict[str, Any] = {
        "schema": TASK_FAILURE_CAUSE_SCHEMA,
        "task_id": task_id,
        "retry_class": "unknown",
        "code": "unclassified",
        "http_status": None,
        "transport_error": None,
    }
    marker = getattr(failure, VLM_REQUEST_FAILURE_ATTRIBUTE, None)
    if (
        type(marker) is not tuple
        or len(marker) != 5
        or type(marker[0]) is not str
        or marker[0] != VLM_REQUEST_FAILURE_SCHEMA
        or type(marker[4]) is not int
        or failure.__cause__ is not marker[3]
        or _exception_note_count(failure) != marker[4]
    ):
        return cause
    kind, detail = marker[1], marker[2]
    if (
        kind == "http_status"
        and type(detail) is int
        and 100 <= detail <= 599
        and detail != 200
    ):
        return {
            **cause,
            "retry_class": _vlm_http_status_retry_class(detail),
            "code": "vlm_http_status",
            "http_status": detail,
        }
    if kind == "transport" and type(detail) is str and detail in VLM_TRANSPORT_ERRORS:
        return {
            **cause,
            "retry_class": (
                "transient" if detail in TRANSIENT_VLM_TRANSPORT_ERRORS else "unknown"
            ),
            "code": "vlm_transport_error",
            "transport_error": detail,
        }
    return cause


def storage_hold_decision(
    *, preview_sha256: str, decided_by: str, reason: str, fixed_by: str,
) -> tuple[dict[str, str], str]:
    """One operator's canonical hold decision (the exact digest preimage) and its digest."""
    _storage_sha256(preview_sha256, label="reviewed preview")
    for label, text in (("decided_by", decided_by), ("reason", reason), ("fixed_by", fixed_by)):
        if (
            type(text) is not str or not text.strip() or len(text) > _MAX_DECISION_TEXT
            or not text.isprintable()
        ):
            raise ValueError(f"storage hold decision {label} is invalid")
    decision = {
        "schema": STORAGE_HOLD_DECISION_SCHEMA,
        "preview_sha256": preview_sha256,
        "decided_by": decided_by,
        "reason": reason,
        "fixed_by": fixed_by,
    }
    return decision, _canonical_sha256(decision)


def validate_task_failure_cause(value: object, *, task_id: str) -> dict[str, Any]:
    """Return one closed, task-bound failure cause or refuse it."""
    if type(value) is not dict or set(value) != (
        _STORAGE_HOLD_CAUSE_FIELDS if value.get("code") == STORAGE_HOLD_TERMINATED_CODE
        else _TASK_FAILURE_CAUSE_FIELDS
    ):
        raise TaskProtocolConflict("task failure cause fields are not closed")
    if value["schema"] != TASK_FAILURE_CAUSE_SCHEMA or value["task_id"] != task_id:
        raise TaskProtocolConflict("task failure cause identity is invalid")
    code = value["code"]
    status = value["http_status"]
    transport = value["transport_error"]
    if (
        code == "vlm_http_status"
        and type(status) is int
        and 100 <= status <= 599
        and status != 200
        and transport is None
    ):
        expected = _vlm_http_status_retry_class(status)
    elif (
        code == "vlm_transport_error"
        and status is None
        and type(transport) is str
        and transport in VLM_TRANSPORT_ERRORS
    ):
        expected = "transient" if transport in TRANSIENT_VLM_TRANSPORT_ERRORS else "unknown"
    elif code == "unclassified" and status is None and transport is None:
        expected = "unknown"
    elif (
        code == STORAGE_HOLD_TERMINATED_CODE
        and status is None
        and transport is None
        and type(value["hold_reason"]) is str
        and value["hold_reason"] in STORAGE_BLOCKED_REASONS
    ):
        decision = value["decision"]
        if (
            type(decision) is not dict or set(decision) != _STORAGE_HOLD_DECISION_FIELDS
            or decision["schema"] != STORAGE_HOLD_DECISION_SCHEMA
        ):
            raise TaskProtocolConflict("task failure cause hold decision is not closed")
        try:
            _, digest = storage_hold_decision(
                preview_sha256=decision["preview_sha256"], decided_by=decision["decided_by"],
                reason=decision["reason"], fixed_by=decision["fixed_by"],
            )
        except ValueError as exc:
            raise TaskProtocolConflict("task failure cause hold decision is invalid") from exc
        if value["decision_sha256"] != digest:
            raise TaskProtocolConflict("task failure cause hold decision digest drifted")
        expected = "permanent"
    else:
        raise TaskProtocolConflict("task failure cause code shape is invalid")
    if value["retry_class"] != expected:
        raise TaskProtocolConflict("task failure cause retry class drifted")
    return value


@dataclass(slots=True)
class DurableTaskRecord:
    idempotency_key: str
    task_id: str
    attempt_identity: str
    fence_identity: str
    state: TaskState = "pending"
    result_path: str | None = None
    result_sha256: str | None = None
    result_bytes: int | None = None
    result_owner: str | None = None
    lease_until_unix: float | None = None
    active_readers: int = 0
    error: str | None = None
    task_payload: dict[str, Any] | None = None
    reserved_result_bytes: int = 0
    recovery_generation: int = 1
    consumed_at_unix: float | None = None
    cleanup_kind: CleanupKind | None = None
    ingress_owner: dict[str, Any] | None = None
    failure_cause: dict[str, Any] | None = None
    storage: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        for value in (
            self.idempotency_key,
            self.task_id,
            self.attempt_identity,
            self.fence_identity,
        ):
            if not isinstance(value, str) or not value or len(value) > 256:
                raise TaskProtocolConflict("task registry identity is invalid")
        if (
            self.task_id in {".", ".."}
            or "/" in self.task_id
            or "\\" in self.task_id
        ):
            raise TaskProtocolConflict("task id is not one safe path component")
        if (
            not isinstance(self.active_readers, int)
            or isinstance(self.active_readers, bool)
            or self.active_readers < 0
        ):
            raise TaskProtocolConflict("task registry reader count is invalid")
        if (
            not isinstance(self.reserved_result_bytes, int)
            or isinstance(self.reserved_result_bytes, bool)
            or self.reserved_result_bytes < 0
        ):
            raise TaskProtocolConflict("task registry reservation is invalid")
        if self.result_bytes is not None and (
            not isinstance(self.result_bytes, int)
            or isinstance(self.result_bytes, bool)
            or self.result_bytes < 1
        ):
            raise TaskProtocolConflict("task registry result bytes are invalid")
        if (
            not isinstance(self.recovery_generation, int)
            or isinstance(self.recovery_generation, bool)
            or self.recovery_generation < 1
        ):
            raise TaskProtocolConflict("task recovery generation is invalid")
        if self.lease_until_unix is not None and (
            isinstance(self.lease_until_unix, bool)
            or not isinstance(self.lease_until_unix, (int, float))
        ):
            raise TaskProtocolConflict("task registry lease is invalid")
        if self.consumed_at_unix is not None and (
            isinstance(self.consumed_at_unix, bool)
            or not isinstance(self.consumed_at_unix, (int, float))
        ):
            raise TaskProtocolConflict("task registry consumed time is invalid")
        if (self.state == "consumed") != (self.consumed_at_unix is not None):
            raise TaskProtocolConflict("task registry consumed lifecycle is invalid")
        if self.error is not None and not isinstance(self.error, str):
            raise TaskProtocolConflict("task registry error is invalid")
        if self.task_payload is not None and not isinstance(self.task_payload, dict):
            raise TaskProtocolConflict("task registry payload is invalid")
        if self.ingress_owner is not None and not isinstance(self.ingress_owner, dict):
            raise TaskProtocolConflict("task ingress owner is invalid")
        if self.ingress_owner is not None:
            owner = self.ingress_owner
            if (
                set(owner) != {"schema", "task_root", "task_root_identity", "uploads_root_identity"}
                or owner.get("schema") != "mineru-task-ingress-owner.v1"
                or not isinstance(owner.get("task_root"), str)
                or not Path(owner["task_root"]).is_absolute()
                or Path(owner["task_root"]).name != self.task_id
            ):
                raise TaskProtocolConflict("task ingress owner fields are not closed")
            for name in ("task_root_identity", "uploads_root_identity"):
                identity = owner[name]
                if (
                    not isinstance(identity, dict)
                    or set(identity) != {"device", "inode", "uid", "mode"}
                    or any(type(value) is not int or value < 0 for value in identity.values())
                    or not stat.S_ISDIR(identity["mode"])
                ):
                    raise TaskProtocolConflict("task ingress directory identity is invalid")
        if self.state in {"ingress", "ingress_cleanup"} and self.task_payload is not None:
            raise TaskProtocolConflict("unaccepted ingress contains an executable payload")
        if self.state not in {"ingress", "ingress_cleanup"} and self.ingress_owner is not None:
            raise TaskProtocolConflict("ingress ownership escaped its preparation state")
        identities = (self.result_sha256, self.result_owner)
        if any(value is not None for value in identities) and not all(
            isinstance(value, str)
            and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)
            for value in identities
        ):
            raise TaskProtocolConflict("task registry result identity is invalid")
        has_result_identity = all(
            value is not None
            for value in (
                self.result_sha256,
                self.result_bytes,
                self.result_owner,
            )
        )
        if self.state == "completed" and not has_result_identity:
            raise TaskProtocolConflict("terminal task result identity is incomplete")
        if self.state == "cleanup_pending" and self.cleanup_kind not in {
            "result", "task_tree"
        }:
            raise TaskProtocolConflict("cleanup intent kind is absent")
        if self.state != "cleanup_pending" and self.cleanup_kind is not None:
            raise TaskProtocolConflict("cleanup intent escaped pending state")
        if self.cleanup_kind == "result" and not has_result_identity:
            raise TaskProtocolConflict("result cleanup identity is incomplete")
        if self.cleanup_kind == "task_tree" and (
            has_result_identity or self.result_path is not None
        ):
            raise TaskProtocolConflict("task-tree cleanup carried result identity")
        if self.failure_cause is not None:
            validate_task_failure_cause(self.failure_cause, task_id=self.task_id)
            if not (
                self.state == "failed"
                or (self.state == "cleanup_pending" and self.cleanup_kind == "task_tree")
            ):
                raise TaskProtocolConflict("task failure cause escaped its failed state")
        if self.state == "consumed" and any(value is not None for value in identities) != has_result_identity:
            raise TaskProtocolConflict("consumed result identity is incomplete")
        if self.storage is not None:
            validate_task_storage(self.storage, state=self.state)
            if self.reserved_result_bytes:
                raise TaskProtocolConflict("storage-managed task carries a legacy reservation")
            if self.state == "completed" and self.storage["zip_bytes"] != self.result_bytes:
                raise TaskProtocolConflict("storage result bytes disagree with the result identity")
        if (
            self.state == "completed" or self.cleanup_kind == "result"
        ) and not isinstance(self.result_path, str):
            raise TaskProtocolConflict("completed task result path is absent")
        if self.state not in {"completed", "cleanup_pending", "consumed"} and any(
            value is not None
            for value in (
                self.result_path,
                self.result_sha256,
                self.result_bytes,
                self.result_owner,
            )
        ):
            raise TaskProtocolConflict("non-result task contains result identity")


@dataclass(frozen=True, slots=True)
class DurableRegistryView:
    """One projection of the last committed registry state.

    The container is frozen and its records and persistence event are deep
    copied once, when the view is published, not on each read.  Readers share
    those objects and must not mutate them.
    """

    records: tuple[DurableTaskRecord, ...]
    submission_watermark_bucket: int
    persistence_generation: int
    persistence_event: dict[str, Any] | None
    durability_uncertain: bool
    published_monotonic_ns: int
    registry_schema: str = REGISTRY_SCHEMA_V3


def admission_counts(
    records: Iterable[DurableTaskRecord],
    route_task_ids: set[str],
    live_ingress_ids: set[str] | None = None,
    *,
    registry_schema: str = REGISTRY_SCHEMA_V3,
) -> dict[str, Any]:
    """Count durable responsibilities independently of the derived route index."""
    if registry_schema not in {REGISTRY_SCHEMA_V3, REGISTRY_SCHEMA_V4}:
        raise TaskProtocolConflict("admission registry schema is unsupported")
    counted = tuple(records)
    ingress = sum(r.state in {"ingress", "ingress_cleanup"} for r in counted)
    accepted = [r for r in counted if r.state in {"pending", "processing", "finalizing"}]
    live_ingress_ids = live_ingress_ids or set()
    return {
        "schema": "mineru-task-admission.v1",
        "registry_schema": registry_schema,
        "ingress_tasks": ingress,
        "accepted_pending_tasks": sum(r.state == "pending" for r in accepted),
        "accepted_processing_tasks": sum(r.state == "processing" for r in accepted),
        "accepted_finalizing_tasks": sum(r.state == "finalizing" for r in accepted),
        "durable_nonterminal_tasks": ingress + len(accepted),
        "routeless_accepted_tasks": sum(r.task_id not in route_task_ids for r in accepted),
        "ingress_cleanup_tasks": sum(r.state == "ingress_cleanup" for r in counted),
        "unowned_ingress_tasks": sum(
            r.state in {"ingress", "ingress_cleanup"} and r.task_id not in live_ingress_ids
            for r in counted
        ),
    }


def registry_record_payload(record: DurableTaskRecord) -> dict[str, Any]:
    """Encode one record; absent optional fields keep the v3 bytes unchanged."""
    payload = asdict(record)
    if payload["failure_cause"] is None:
        del payload["failure_cause"]
    if payload["storage"] is None:
        del payload["storage"]
    return payload


class DurableTaskRegistry:
    """Atomic registry with reconcile, leases, ACK and reader-safe cleanup."""

    def __init__(
        self,
        path: Path,
        *,
        max_unacked_result_bytes: int | None = None,
        output_root: Path | None = None,
        tombstone_retention_seconds: int = 86400,
        enforce_key_lifecycle: bool = False,
        clock: Callable[[], float] = time.time,
        storage_policy: Any = None,
    ) -> None:
        if storage_policy is not None:
            # Physical storage replaces the aggregate L; accepting one here
            # would leave a second, unenforced result limit in the process.
            if max_unacked_result_bytes is not None:
                raise ValueError("storage-managed registries have no aggregate unacked limit")
        elif type(max_unacked_result_bytes) is not int or max_unacked_result_bytes < 1:
            raise ValueError("unacked result byte limit must be positive")
        # A capacity-config-v2 process manages physical result storage and
        # writes registry v4; every other process keeps the exact v3 behavior.
        self._storage_policy = storage_policy
        self._registry_schema = REGISTRY_SCHEMA_V3 if storage_policy is None else REGISTRY_SCHEMA_V4
        self._storage_policy_sha256: str | None = None
        if storage_policy is not None:
            self._storage_policy_sha256 = _storage_sha256(
                getattr(storage_policy, "sha256", None), label="policy",
            )
        # Process-local ingress upload reservations; an interrupted ingress
        # is aborted on restart, so these never need a durable record.
        self._ingress_storage: dict[str, int] = {}
        # Pre-body request charges: token -> [framework body spool, upload copy].
        self._request_ingress: dict[str, list[int]] = {}
        if not 3600 <= tombstone_retention_seconds <= 30 * 86400:
            raise ValueError("tombstone retention must be between one hour and 30 days")
        self._path = path
        # Storage mode refuses every legacy reservation before reading L.
        self._limit: int = max_unacked_result_bytes or 0
        self._clock = clock
        self._retention = tombstone_retention_seconds
        self._enforce_key_lifecycle = enforce_key_lifecycle
        self._output_root = (output_root or path.parent).resolve()
        root_fd = os.open(
            self._output_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            root_meta = os.fstat(root_fd)
            if not stat.S_ISDIR(root_meta.st_mode) or root_meta.st_uid != os.getuid():
                raise TaskProtocolConflict("configured output root is unsafe")
            self._output_root_identity = (
                root_meta.st_dev, root_meta.st_ino, root_meta.st_uid, root_meta.st_mode
            )
        finally:
            os.close(root_fd)
        self._lock = RLock()
        # Process-local exclusion only. Durable cleanup_pending/ingress_cleanup
        # remains the sole replay authority; no second cleanup ledger.
        # Lock order is always cleanup_lock -> _lock, never the reverse.
        self._cleanup_lock = RLock()
        self._active_operation = "initial_load"
        self._last_persistence_event: dict[str, Any] | None = None
        self._last_persistence_cause: BaseException | None = None
        self._last_persistence_cleanup_cause: BaseException | None = None
        self._uncertain_records: dict[str, DurableTaskRecord] | None = None
        self._uncertain_watermark_bucket: int | None = None
        self._uncertain_payload: bytes | None = None
        self._persistence_generation = 0
        self._submission_watermark_bucket = -1
        self._records = self._load()
        self._replay_required_keys = {
            key for key, record in self._records.items()
            if record.state in {"pending", "processing", "finalizing"}
            and record.task_payload is not None
        }
        self._durable_payload = self._read_current_registry_bytes()
        self._last_durable_records = self._clone_records(self._records)
        self._last_durable_watermark_bucket = self._submission_watermark_bucket
        self._publish_durable_view()

    def reconcile_or_create(
        self,
        *,
        idempotency_key: str,
        task_id: str,
        attempt_identity: str,
        fence_identity: str,
        max_nonterminal_tasks: int | None = None,
        allow_create: bool = True,
    ) -> tuple[DurableTaskRecord, bool]:
        values = (idempotency_key, task_id, attempt_identity, fence_identity)
        if not all(value.strip() for value in values):
            raise ValueError("task protocol identities must be non-empty")
        with self._lock:
            observed_server_epoch = self._validate_key_lifecycle(idempotency_key)
            proposed_records = self._records_without_expired_tombstones()
            proposed_watermark = self._submission_watermark_bucket
            if observed_server_epoch is not None:
                proposed_watermark = max(proposed_watermark, observed_server_epoch)
            existing = proposed_records.get(idempotency_key)
            if existing is not None:
                if (
                    existing.attempt_identity != attempt_identity
                    or existing.fence_identity != fence_identity
                ):
                    raise TaskProtocolConflict(
                        "idempotency key was reused with different attempt/fence"
                    )
                if (
                    proposed_records != self._records
                    or proposed_watermark != self._submission_watermark_bucket
                ):
                    self._commit_registry_transition(
                        proposed_records, proposed_watermark
                    )
                return existing, False
            if type(allow_create) is not bool:
                raise ValueError("allow_create must be boolean")
            if not allow_create:
                raise TaskProtocolConflict("new task admission is closed")
            if max_nonterminal_tasks is not None:
                if type(max_nonterminal_tasks) is not int or not 1 <= max_nonterminal_tasks <= _MAX_RECORDS:
                    raise ValueError("task admission limit is invalid")
                if sum(
                    record.state in {"ingress", "ingress_cleanup", "pending", "processing", "finalizing"}
                    for record in proposed_records.values()
                ) >= max_nonterminal_tasks:
                    raise TaskAdmissionFull("Task admission capacity exhausted")
            if sum(record.state != "consumed" for record in proposed_records.values()) >= _MAX_RECORDS:
                raise TaskProtocolConflict("active task registry capacity exhausted")
            if sum(record.state == "consumed" for record in proposed_records.values()) >= _MAX_TOMBSTONES:
                raise TaskProtocolConflict("task tombstone retention capacity exhausted")
            if any(item.task_id == task_id for item in proposed_records.values()):
                raise TaskProtocolConflict("task id is already owned by another key")
            record = DurableTaskRecord(
                idempotency_key=idempotency_key,
                task_id=task_id,
                attempt_identity=attempt_identity,
                fence_identity=fence_identity,
                state="pending" if max_nonterminal_tasks is None else "ingress",
            )
            proposed_records[idempotency_key] = record
            self._commit_registry_transition(proposed_records, proposed_watermark)
            return record, True


    def get(self, idempotency_key: str) -> DurableTaskRecord | None:
        with self._lock:
            self.assert_observation_safe()
            return copy.deepcopy(self._records.get(idempotency_key))



    def get_by_task_id(self, task_id: str) -> DurableTaskRecord | None:
        with self._lock:
            self.assert_observation_safe()
            matches = [
                record for record in self._records.values() if record.task_id == task_id
            ]
            if len(matches) > 1:
                raise TaskProtocolConflict("task id is not unique")
            return copy.deepcopy(matches[0]) if matches else None


    def bind_ingress_root(self, idempotency_key: str) -> None:
        """Pin empty owned directories before any asynchronous upload writes."""
        with self._cleanup_lock:
            with self._lock:
                self._ensure_mutation_allowed("bind_ingress_root")
                record = copy.deepcopy(self._required(idempotency_key))
            if record.state != "ingress" or record.ingress_owner is not None:
                raise TaskProtocolConflict("ingress directory ownership already resolved")
            root_fd, task_fd = self._open_task_dir(record.task_id)
            upload_fd = -1
            primary_error: BaseException | None = None
            try:
                if set(os.listdir(task_fd)) != {"uploads"}:
                    raise TaskProtocolConflict("ingress task directory is not empty")
                upload_fd = os.open(
                    "uploads", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0), dir_fd=task_fd,
                )
                if os.listdir(upload_fd):
                    raise TaskProtocolConflict("ingress upload directory is not empty")
                for descriptor in (task_fd, upload_fd):
                    if os.fstat(descriptor).st_uid != os.getuid():
                        raise TaskProtocolConflict("ingress directory owner drifted")
                owner = {
                    "schema": "mineru-task-ingress-owner.v1",
                    "task_root": str(self._output_root / record.task_id),
                    "task_root_identity": self._directory_identity(os.fstat(task_fd)),
                    "uploads_root_identity": self._directory_identity(os.fstat(upload_fd)),
                }
                for descriptor in (upload_fd, task_fd, root_fd):
                    self._fsync_namespace_directory(descriptor)
                self._commit_ingress_root(idempotency_key, record, owner)
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                self._close_namespace_descriptors(
                    ((upload_fd, "ingress uploads"), (task_fd, "ingress task"),
                     (root_fd, "ingress output root")), primary_error,
                )

    def _commit_ingress_root(self, key: str, expected: DurableTaskRecord, owner: dict[str, Any]) -> None:
        record = self._required(key)
        if record != expected or record.state != "ingress" or record.ingress_owner is not None:
            raise TaskProtocolConflict("ingress ownership changed during directory IO")
        record.ingress_owner = owner
        self._persist()

    def _confirm_unowned_task_absent(self, record: DurableTaskRecord) -> None:
        """A crash before the owner receipt must never authorize path-only deletion."""
        root_fd = os.open(
            self._output_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        primary_error: BaseException | None = None
        try:
            metadata = os.fstat(root_fd)
            if (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode) != self._output_root_identity:
                raise TaskProtocolConflict("configured output root identity drifted")
            try:
                os.stat(record.task_id, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                self._fsync_namespace_directory(root_fd)
            else:
                raise TaskProtocolConflict("unaccepted task directory requires ownership recovery")
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            self._close_namespace_descriptors(((root_fd, "ingress output root"),), primary_error)

    def abort_ingress(self, idempotency_key: str) -> None:
        """Persist intent, remove owned bytes without the data lock, commit absence."""
        with self._cleanup_lock:
            self._mark_ingress_cleanup(idempotency_key)
            with self._lock:
                self._ensure_mutation_allowed("abort_ingress")
                expected = copy.deepcopy(self._required(idempotency_key))
            if expected.ingress_owner is None:
                self._confirm_unowned_task_absent(expected)
            else:
                self._unlink_owned_result(expected, before_unlink=None)
            self._finish_ingress_cleanup(idempotency_key, expected)

    def _mark_ingress_cleanup(self, idempotency_key: str) -> None:
        record = self._required(idempotency_key)
        if record.state not in {"ingress", "ingress_cleanup"}:
            raise TaskProtocolConflict("accepted task cannot be abandoned as ingress")
        if record.state == "ingress":
            record.state = "ingress_cleanup"
            self._persist()

    def _finish_ingress_cleanup(self, key: str, expected: DurableTaskRecord) -> None:
        if self._required(key) != expected or expected.state != "ingress_cleanup":
            raise TaskProtocolConflict("ingress cleanup responsibility changed")
        del self._records[key]
        self._persist()
        # The upload bytes are gone; their process-local charge goes with them.
        self._ingress_storage.pop(key, None)

    def task_payload_for_route(self, idempotency_key: str) -> dict[str, Any] | None:
        """Project one accepted task without replay, cleanup or generation changes."""
        with self._lock:
            self.assert_observation_safe()
            record = self._required(idempotency_key)
            if record.task_payload is None or record.state == "consumed":
                return None
            result = copy.deepcopy(record.task_payload)
            result.pop("_agent_protocol", None)
            wire_state = record.state
            if wire_state == "finalizing":
                wire_state = "processing"
            elif wire_state == "cleanup_pending":
                wire_state = "completed" if record.cleanup_kind == "result" else "failed"
            result.update(
                status=wire_state,
                result_artifact_path=record.result_path,
                result_artifact_sha256=record.result_sha256,
                result_artifact_bytes=record.result_bytes,
                result_artifact_owner=record.result_owner,
                error=record.error,
            )
            return result

    def admission_status(
        self, route_task_ids: set[str], live_ingress_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Count durable responsibilities independently of the derived route index."""
        with self._lock:
            self.assert_observation_safe()
            return admission_counts(
                self._records.values(), route_task_ids, live_ingress_ids,
                registry_schema=self._registry_schema,
            )

    @classmethod
    def admission_status_from_view(
        cls,
        view: DurableRegistryView,
        route_task_ids: set[str],
        live_ingress_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Count the published durable view without waiting for the data lock."""
        cls._raise_view_persistence_error(
            view.persistence_event, durability_uncertain=view.durability_uncertain
        )
        return admission_counts(
            view.records, route_task_ids, live_ingress_ids,
            registry_schema=view.registry_schema,
        )

    def durable_view(self) -> DurableRegistryView:
        """Return the last durable state without acquiring the data lock.

        The reference may lag one commit that is still in flight, so it never
        carries a mutation that did not commit.
        """
        return self._durable_view

    def bind_task_payload(
        self,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> None:
        # Round-trip now so restart cannot discover a non-JSON task payload.
        normalized = json.loads(json.dumps(payload, sort_keys=True))
        if not isinstance(normalized, dict):
            raise TypeError("task payload must be one JSON object")
        with self._cleanup_lock:
            with self._lock:
                self._ensure_mutation_allowed("bind_task_payload")
                record = copy.deepcopy(self._required(idempotency_key))
            if record.state not in {"ingress", "pending"}:
                raise TaskProtocolConflict("task payload cannot bind in this state")
            if record.state == "ingress" and record.ingress_owner is None:
                raise TaskProtocolConflict("ingress directory ownership is absent")
            if normalized.get("task_id") != record.task_id:
                raise TaskProtocolConflict("task payload identity drifted")
            output_value = normalized.get("output_dir")
            uploads_value = normalized.get("uploads")
            if not isinstance(output_value, str) or not isinstance(uploads_value, list):
                raise TaskProtocolConflict("task payload ownership paths are absent")
            output = Path(output_value)
            if output.parent.resolve() != self._output_root:
                raise TaskProtocolConflict("task output root escaped configured parent")
            root_fd, task_fd = self._open_task_dir(record.task_id)
            output_meta = os.fstat(task_fd)
            if (
                output.name != record.task_id
                or not stat.S_ISDIR(output_meta.st_mode)
                or output_meta.st_uid != os.getuid()
            ):
                os.close(task_fd)
                os.close(root_fd)
                raise TaskProtocolConflict("task output root identity is unsafe")
            upload_root = output / "uploads"
            try:
                if set(os.listdir(task_fd)) != {"uploads"}:
                    raise TaskProtocolConflict(
                        "task root contained data before generation ownership began"
                    )
                upload_fd = os.open(
                    "uploads", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0), dir_fd=task_fd,
                )
                try:
                    upload_meta = os.fstat(upload_fd)
                    upload_identities = [
                        self._stable_file_identity_at(
                            upload_fd, Path(value), expected_parent=upload_root,
                            sync_source=True,
                        )
                        for value in uploads_value if isinstance(value, str)
                    ]
                    for descriptor in (upload_fd, task_fd, root_fd):
                        self._fsync_namespace_directory(descriptor)
                finally:
                    os.close(upload_fd)
            finally:
                os.close(task_fd)
                os.close(root_fd)
            if len(upload_identities) != len(uploads_value):
                raise TaskProtocolConflict("task upload paths are invalid")
            normalized["_agent_protocol"] = {
                "schema": "mineru-task-payload-owner.v1",
                "task_root": str(output.resolve()),
                "task_root_identity": self._directory_identity(output_meta),
                "uploads_root_identity": self._directory_identity(upload_meta),
                "generation": record.recovery_generation,
                "uploads": upload_identities,
            }
            if record.state == "ingress":
                ingress_owner = record.ingress_owner
                if ingress_owner is None or any(
                    normalized["_agent_protocol"][name] != ingress_owner.get(name)
                    for name in ("task_root", "task_root_identity", "uploads_root_identity")
                ):
                    raise TaskProtocolConflict("ingress directory identity drifted before acceptance")
            if len(json.dumps(normalized, sort_keys=True).encode()) > _MAX_TASK_PAYLOAD_BYTES:
                raise TaskProtocolConflict("task payload exceeds the closed envelope")
            if record.task_payload is not None and record.task_payload != normalized:
                raise TaskProtocolConflict("task payload drifted after allocation")
            self._commit_task_payload(idempotency_key, record, normalized)


    def _commit_task_payload(self, key: str, expected: DurableTaskRecord, payload: dict[str, Any]) -> None:
        record = self._required(key)
        if record != expected or record.state not in {"ingress", "pending"}:
            raise TaskProtocolConflict("task ownership changed during upload verification")
        storage = record.storage
        policy = self._storage_policy
        if policy is not None and storage is None:
            uploads = payload["_agent_protocol"]["uploads"]
            if any(item["bytes"] > policy.source_pdf_bytes_limit for item in uploads):
                raise TaskProtocolConflict("task upload exceeds the storage policy source envelope")
            storage = {
                "schema": TASK_STORAGE_SCHEMA, "policy_sha256": self._storage_policy_sha256,
                "phase": "admitted",
                "upload_bytes": sum(policy.physical_charge(item["bytes"]) for item in uploads),
                "growth_permit_bytes": 0, "source_bytes": 0, "selected_bytes": 0,
                "member_count": 0, "inventory_sha256": None, "zip_upper_bound_bytes": 0,
                "zip_grant_bytes": 0, "zip_bytes": 0, "wait_reason": None, "wait_since_unix": None,
            }
            validate_task_storage(storage, state="pending")
        record.task_payload = payload
        record.state = "pending"
        record.ingress_owner = None
        record.storage = storage
        self._ingress_storage.pop(key, None)
        self._persist()

    def recoverable_payloads(self) -> tuple[dict[str, Any], ...]:
        with self._cleanup_lock:
            return self._recoverable_payloads_transaction()

    def _recoverable_payloads_transaction(self) -> tuple[dict[str, Any], ...]:
        """Hydrate routes and durably prepare interrupted work for replay.

        Live reader counts are process-local barriers.  A cold constructor may
        clear persisted counts, but an in-process recovery must never discard a
        handle that is still open.  Interrupted-state changes are committed
        before any filesystem cleanup, so a cleanup failure remains retryable.
        """
        with self._lock:
            if any(record.active_readers for record in self._records.values()):
                raise TaskProtocolConflict(
                    "live result readers prevent in-process task recovery"
                )
            for key, record in tuple(self._records.items()):
                if record.state in {"ingress", "ingress_cleanup"}:
                    self.abort_ingress(key)
            proposed_records = self._clone_records(self._records)
            changed = False
            replay_keys: list[str] = []
            unsealed_result_keys: list[str] = []
            for key, record in list(proposed_records.items()):
                if record.state == "pending" and record.task_payload is None:
                    self._confirm_unowned_task_absent(record)
                    del proposed_records[key]
                    changed = True
                    continue
                if record.state in {"pending", "processing", "finalizing"}:
                    if record.task_payload is None:
                        raise TaskProtocolConflict(
                            "nonterminal task has no durable replay payload"
                        )
                    storage = record.storage
                    if storage is not None and storage["wait_reason"] in STORAGE_BLOCKED_REASONS:
                        # Held for an operator decision: bytes, seal and state
                        # stay exactly as recorded; nothing is replayed.
                        continue
                    if storage is not None and record.state == "finalizing":
                        # A sealed source is re-packed only; the parser never
                        # reruns. An unsealed ZIP attempt is discarded.
                        if storage["phase"] == "zip_writing":
                            record.storage = {
                                **storage, "phase": "source_sealed", "zip_grant_bytes": 0,
                                "wait_reason": None, "wait_since_unix": None,
                            }
                            validate_task_storage(record.storage, state="finalizing")
                            changed = True
                        unsealed_result_keys.append(key)
                        continue
                    if storage is not None and storage["phase"] == "source_growing":
                        # An interrupted parser has no durable intermediate
                        # state: its permit is released and the source replays.
                        record.storage = {
                            **storage, "phase": "admitted", "growth_permit_bytes": 0,
                            "wait_reason": None, "wait_since_unix": None,
                        }
                        changed = True
                    if record.state in {"processing", "finalizing"}:
                        record.state = "pending"
                        record.recovery_generation += 1
                        record.error = None
                        changed = True
                    ownership = record.task_payload.get("_agent_protocol")
                    if (
                        not isinstance(ownership, dict)
                        or ownership.get("schema")
                        != "mineru-task-payload-owner.v1"
                    ):
                        raise TaskProtocolConflict(
                            "recoverable task payload ownership receipt is invalid"
                        )
                    if ownership.get("generation") != record.recovery_generation:
                        ownership["generation"] = record.recovery_generation
                        changed = True
                    replay_keys.append(key)

            # Reconciliation may later select the pending snapshot after an uncertain
            # commit. Keep the physical replay barrier before attempting that commit.
            self._replay_required_keys.update(replay_keys)
            if changed:
                self._commit_registry_transition(
                    proposed_records,
                    self._submission_watermark_bucket,
                )

            # Filesystem replay cleanup is intentionally after the registry
            # transition.  The durable pending state and ownership receipt then
            # make a partial cleanup failure safe to retry on the next call.
            for key in replay_keys:
                current_record = self._records.get(key)
                if (
                    current_record is None
                    or current_record.state != "pending"
                    or current_record.task_payload is None
                ):
                    continue
                self._prepare_clean_replay(copy.deepcopy(current_record))
                self._replay_required_keys.discard(key)
            for key in unsealed_result_keys:
                current_record = self._records.get(key)
                if current_record is None or current_record.state != "finalizing":
                    continue
                self._discard_unsealed_result(copy.deepcopy(current_record))

            hydrated: list[dict[str, Any]] = []
            for record in self._records.values():
                if record.task_payload is None or record.state == "consumed":
                    continue
                recovered = copy.deepcopy(record.task_payload)
                recovered.pop("_agent_protocol", None)
                blocked = (
                    record.storage is not None
                    and record.storage["wait_reason"] in STORAGE_BLOCKED_REASONS
                )
                recovered["status"] = (
                    "processing" if blocked
                    else "pending"
                    if record.state in {"pending", "processing", "finalizing"}
                    else record.state
                )
                recovered["result_artifact_path"] = record.result_path
                recovered["result_artifact_sha256"] = record.result_sha256
                recovered["result_artifact_bytes"] = record.result_bytes
                recovered["result_artifact_owner"] = record.result_owner
                recovered["error"] = record.error
                hydrated.append(recovered)
            return tuple(hydrated)


    def _prepare_clean_replay(self, record: DurableTaskRecord) -> None:
        payload = record.task_payload or {}
        output_value = payload.get("output_dir")
        uploads_value = payload.get("uploads")
        if not isinstance(output_value, str) or not isinstance(uploads_value, list):
            raise TaskProtocolConflict("restart replay paths are absent")
        protocol = payload.get("_agent_protocol")
        if not isinstance(protocol, dict) or protocol.get("schema") != "mineru-task-payload-owner.v1":
            raise TaskProtocolConflict("restart task ownership receipt is absent")
        output = Path(output_value)
        if output.parent.resolve() != self._output_root:
            raise TaskProtocolConflict("restart task root escaped configured parent")
        root_fd, task_fd = self._open_task_dir(record.task_id)
        output_meta = os.fstat(task_fd)
        if (
            output.name != record.task_id
            or str(output.resolve()) != protocol.get("task_root")
            or not stat.S_ISDIR(output_meta.st_mode)
            or output_meta.st_uid != os.getuid()
        ):
            raise TaskProtocolConflict("restart task root identity drifted")
        upload_root = output / "uploads"
        expected_uploads = protocol.get("uploads")
        if not isinstance(expected_uploads, list) or len(expected_uploads) != len(uploads_value):
            raise TaskProtocolConflict("restart upload receipt drifted")
        try:
            upload_fd = os.open(
                "uploads", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0), dir_fd=task_fd,
            )
            try:
                if self._directory_identity(output_meta) != protocol.get(
                    "task_root_identity"
                ) or self._directory_identity(os.fstat(upload_fd)) != protocol.get(
                    "uploads_root_identity"
                ):
                    raise TaskProtocolConflict("restart task directory identity drifted")
                observed = [
                    self._stable_file_identity_at(
                        upload_fd, Path(value), expected_parent=upload_root
                    ) for value in uploads_value if isinstance(value, str)
                ]
            finally:
                os.close(upload_fd)
            if observed != expected_uploads:
                raise TaskProtocolConflict("restart upload snapshot identity drifted")
            for child in os.listdir(task_fd):
                if child == "uploads":
                    continue
                self._remove_at(task_fd, child)
            # A prior attempt may have unlinked the last child before its fsync failed.
            # Successful replay must close that namespace boundary even on an empty retry.
            os.fsync(task_fd)
            protocol["generation"] = record.recovery_generation
        finally:
            os.close(task_fd)
            os.close(root_fd)

    def _discard_unsealed_result(self, record: DurableTaskRecord) -> None:
        """Remove only this task's unrecorded ZIP part/final before re-packing.

        A final ZIP whose commit never became durable is not a result: the
        sealed source is re-packed. Nothing else in the task tree is touched.
        """
        root_fd, task_fd = self._open_task_dir(record.task_id)
        primary_error: BaseException | None = None
        try:
            payload = record.task_payload or {}
            protocol = payload.get("_agent_protocol")
            if (
                not isinstance(protocol, dict)
                or self._directory_identity(os.fstat(task_fd)) != protocol.get("task_root_identity")
            ):
                raise TaskProtocolConflict("unsealed result task directory identity drifted")
            removed = False
            for name in (RETAINED_RESULT_PART_NAME, RETAINED_RESULT_NAME):
                try:
                    metadata = os.stat(name, dir_fd=task_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise TaskProtocolConflict("unsealed result file identity is unsafe")
                os.unlink(name, dir_fd=task_fd)
                removed = True
            if removed:
                self._fsync_namespace_directory(task_fd)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            self._close_namespace_descriptors(
                ((task_fd, "unsealed result task directory"), (root_fd, "output root")),
                primary_error,
            )

    @staticmethod
    def _directory_identity(metadata: os.stat_result) -> dict[str, int]:
        return {
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "uid": metadata.st_uid,
            "mode": metadata.st_mode,
        }

    @staticmethod
    def _stable_file_identity_at(
        parent_fd: int, path: Path, *, expected_parent: Path, sync_source: bool = False
    ) -> dict[str, Any]:
        if path.parent.resolve() != expected_parent.resolve() or path.name in {"", ".", ".."}:
            raise TaskProtocolConflict("task upload escaped configured task root")
        descriptor = os.open(
            path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd
        )
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or path.resolve().parent != expected_parent.resolve()
            ):
                raise TaskProtocolConflict("task upload snapshot identity is unsafe")
            digest = hashlib.sha256()
            total = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
                total += len(chunk)
            if sync_source:
                os.fsync(descriptor)
            after = os.fstat(descriptor)
            def identity(value: os.stat_result) -> tuple[int, ...]:
                return (
                    value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                    value.st_nlink, value.st_size, value.st_mtime_ns,
                    value.st_ctime_ns,
                )
            if total != before.st_size or identity(before) != identity(after):
                raise TaskProtocolConflict("task upload changed while hashing")
            return {"path": str(path.resolve()), "bytes": total, "sha256": digest.hexdigest()}
        finally:
            os.close(descriptor)

    def _open_task_dir(self, task_id: str) -> tuple[int, int]:
        root_fd = os.open(
            self._output_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            root_meta = os.fstat(root_fd)
            if (
                root_meta.st_dev, root_meta.st_ino, root_meta.st_uid, root_meta.st_mode
            ) != self._output_root_identity:
                raise TaskProtocolConflict("configured output root identity drifted")
            task_fd = os.open(
                task_id,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd,
            )
        except BaseException as exc:
            self._close_namespace_descriptors(((root_fd, "output root"),), exc)
            raise
        return root_fd, task_fd

    @staticmethod
    def _fsync_namespace_directory(fd: int) -> None:
        """Commit directory-entry changes before durable ownership is released."""
        os.fsync(fd)

    @staticmethod
    def _close_namespace_descriptors(
        descriptors: tuple[tuple[int, str], ...],
        primary_error: BaseException | None,
    ) -> None:
        """Close every fd without replacing an earlier delete/fsync failure."""
        close_error: BaseException | None = None
        for descriptor, label in descriptors:
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except BaseException as exc:
                if primary_error is not None:
                    primary_error.add_note(
                        f"{label} close also failed: {type(exc).__name__}"
                    )
                elif close_error is None:
                    close_error = exc
                else:
                    close_error.add_note(
                        f"{label} close also failed: {type(exc).__name__}"
                    )
        if primary_error is None and close_error is not None:
            raise close_error

    @classmethod
    def _sync_namespace_path(cls, directory: Path) -> None:
        descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        primary_error: BaseException | None = None
        try:
            cls._fsync_namespace_directory(descriptor)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cls._close_namespace_descriptors(
                ((descriptor, "cleanup namespace directory"),),
                primary_error,
            )

    @classmethod
    def _remove_at(cls, parent_fd: int, name: str) -> None:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise TaskProtocolConflict("restart cleanup target is a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(
                name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd,
            )
            primary_error: BaseException | None = None
            try:
                for child in os.listdir(child_fd):
                    cls._remove_at(child_fd, child)
                cls._fsync_namespace_directory(child_fd)
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                cls._close_namespace_descriptors(
                    ((child_fd, "cleanup child directory"),),
                    primary_error,
                )
            os.rmdir(name, dir_fd=parent_fd)
        else:
            os.unlink(name, dir_fd=parent_fd)
        cls._fsync_namespace_directory(parent_fd)

    def _validate_key_lifecycle(self, key: str) -> int | None:
        if not self._enforce_key_lifecycle:
            return None
        try:
            bucket_text, digest = key.split(".", 1)
            bucket = int(bucket_text, 16)
        except (ValueError, TypeError) as exc:
            raise TaskProtocolConflict("idempotency key lifecycle is invalid") from exc
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise TaskProtocolConflict("idempotency key digest is invalid")
        now = self._clock()
        oldest = int(now - self._retention)
        if bucket < oldest or bucket > int(now + _MAX_CLOCK_SKEW_SECONDS):
            raise TaskProtocolConflict("idempotency key lifecycle expired")
        if int(now) < self._submission_watermark_bucket - _MAX_CLOCK_SKEW_SECONDS:
            raise TaskProtocolConflict("server clock rolled back behind watermark")
        return int(now)

    def _records_without_expired_tombstones(
        self,
    ) -> dict[str, DurableTaskRecord]:
        proposed = self._clone_records(self._records)
        if not self._enforce_key_lifecycle:
            return proposed
        cutoff = self._clock() - self._retention
        expired = [
            key for key, record in proposed.items()
            if record.state == "consumed"
            and record.consumed_at_unix is not None
            and record.consumed_at_unix < cutoff
        ]
        for key in expired:
            del proposed[key]
        return proposed

    def _commit_registry_transition(
        self,
        proposed_records: dict[str, DurableTaskRecord],
        proposed_watermark: int,
    ) -> None:
        previous_records = self._records
        previous_watermark = self._submission_watermark_bucket
        self._records = proposed_records
        self._submission_watermark_bucket = proposed_watermark
        try:
            self._persist()
        except BaseException:
            self._records = previous_records
            self._submission_watermark_bucket = previous_watermark
            raise

    def abandon_unbound(self, idempotency_key: str) -> None:
        with self._cleanup_lock:
            with self._lock:
                self._ensure_mutation_allowed("abandon_unbound")
                expected = copy.deepcopy(self._required(idempotency_key))
                if expected.state != "pending" or expected.task_payload is not None:
                    raise TaskProtocolConflict("only an unbound pending task may be abandoned")
            self._confirm_unowned_task_absent(expected)
            self._finish_abandon_unbound(idempotency_key, expected)

    def _finish_abandon_unbound(self, key: str, expected: DurableTaskRecord) -> None:
        if self._required(key) != expected:
            raise TaskProtocolConflict("unbound responsibility changed during absence check")
        del self._records[key]
        self._persist()

    def fail(
        self,
        idempotency_key: str,
        *,
        error: str,
        failure_cause: dict[str, Any] | None = None,
    ) -> None:
        """Commit the terminal failure, its original error and typed cause at once."""
        if not error.strip():
            raise ValueError("task failure must be visible")
        with self._lock:
            record = self._required(idempotency_key)
            if record.state not in {"pending", "processing", "finalizing"}:
                raise TaskProtocolConflict("terminal task cannot fail again")
            if failure_cause is not None:
                failure_cause = validate_task_failure_cause(
                    copy.deepcopy(failure_cause), task_id=record.task_id
                )
            record.state = "failed"
            record.error = error
            record.failure_cause = failure_cause
            if record.storage is not None:
                # Owned bytes stay charged until the task-tree cleanup.
                record.storage = {**record.storage, "wait_reason": None, "wait_since_unix": None}
                validate_task_storage(record.storage, state="failed")
            self._persist()

    def acknowledge_terminal_intent(self, idempotency_key: str) -> str:
        """Persist the exact terminal cleanup intent in one short transaction."""
        record = self._required(idempotency_key)
        if record.state == "consumed":
            return "consumed"
        if record.state == "cleanup_pending":
            if record.cleanup_kind not in {"result", "task_tree"}:
                raise TaskProtocolConflict("cleanup intent is invalid")
            return "cleanup_pending"
        if record.state == "completed":
            if record.active_readers:
                raise TaskProtocolConflict("result cannot be ACKed while in use")
            record.state = "cleanup_pending"
            record.cleanup_kind = "result"
        elif record.state == "failed":
            record.state = "cleanup_pending"
            record.cleanup_kind = "task_tree"
        else:
            raise TaskProtocolConflict("only terminal tasks can be ACKed")
        self._persist()
        return "cleanup_pending"

    def acknowledge_failed(self, idempotency_key: str) -> None:
        self._acknowledge_failed_intent(idempotency_key)
        self.cleanup_consumed(idempotency_key=idempotency_key)

    def _acknowledge_failed_intent(self, idempotency_key: str) -> None:
        record = self._required(idempotency_key)
        if record.state == "consumed":
            return
        if record.state == "cleanup_pending" and record.cleanup_kind == "task_tree":
            return
        if record.state != "failed":
            raise TaskProtocolConflict("only failed tasks can use failed ACK")
        record.state = "cleanup_pending"
        record.cleanup_kind = "task_tree"
        self._persist()

    def transition(self, idempotency_key: str, target: TaskState) -> None:
        allowed: dict[TaskState, frozenset[TaskState]] = {
            "pending": frozenset({"processing", "failed"}),
            "processing": frozenset({"finalizing", "failed"}),
            "finalizing": frozenset({"completed", "failed"}),
            "completed": frozenset(),
            "failed": frozenset(),
            "cleanup_pending": frozenset(),
            "consumed": frozenset(),
        }
        with self._lock:
            record = self._required(idempotency_key)
            if target not in allowed[record.state]:
                raise TaskProtocolConflict(
                    f"invalid task transition {record.state}->{target}"
                )
            if record.storage is not None:
                # Storage-managed work enters the parser only under its growth
                # permit and leaves it only through the source seal.
                if target == "finalizing" or (
                    target == "processing" and record.storage["phase"] != "source_growing"
                ):
                    raise TaskProtocolConflict("storage-managed transition needs its storage grant")
                validate_task_storage(record.storage, state=target)
            record.state = target
            self._persist()

    def complete(
        self,
        idempotency_key: str,
        *,
        result_path: Path,
        result_sha256: str,
        result_bytes: int,
        result_owner: str,
    ) -> None:
        if (
            isinstance(result_bytes, bool)
            or not isinstance(result_bytes, int)
            or result_bytes < 1
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
                for value in (result_sha256, result_owner)
            )
        ):
            raise ValueError("result identity is invalid")
        with self._lock:
            record = self._required(idempotency_key)
            if record.state != "finalizing":
                raise TaskProtocolConflict("only finalizing tasks may complete")
            if result_bytes > record.reserved_result_bytes:
                raise TaskProtocolConflict("result exceeded its reserved byte envelope")
            record.result_path = str(result_path)
            record.result_sha256 = result_sha256
            record.result_bytes = result_bytes
            record.result_owner = result_owner
            record.state = "completed"
            record.reserved_result_bytes = 0
            self._persist()

    def reserve_result_for_parse(self, idempotency_key: str, *, byte_budget: int) -> None:
        """Acquire one durable result budget before invoking the parser."""
        if self._storage_policy is not None:
            raise TaskProtocolConflict("storage-managed tasks use growth permits, not B reservations")
        if type(byte_budget) is not int or not 1 <= byte_budget <= self._limit:
            raise ValueError("result reservation must be positive and within its limit")
        with self._lock:
            record = self._required(idempotency_key)
            if record.state not in {"pending", "processing", "finalizing"}:
                raise TaskProtocolConflict("result reservation state is invalid")
            if any(
                key in self._records and self._records[key].state != "consumed"
                for key in self._replay_required_keys
            ):
                raise TaskResultCapacityRecoveryRequired(
                    "cold task responsibility requires successful replay cleanup"
                )
            if any(
                item.reserved_result_bytes == 0
                and (
                    item.state in {"processing", "finalizing", "failed"}
                    or item.state == "cleanup_pending" and item.cleanup_kind == "task_tree"
                )
                for item in self._records.values()
            ):
                raise TaskResultCapacityRecoveryRequired(
                    "legacy task responsibility requires owned cleanup before new parse"
                )
            used = self.unacked_result_bytes + self.reserved_result_bytes
            if used > self._limit:
                raise TaskResultCapacityRecoveryRequired(
                    "existing result responsibility exceeds the configured limit"
                )
            if record.reserved_result_bytes:
                if record.reserved_result_bytes != byte_budget:
                    raise TaskProtocolConflict("held result reservation differs from requested budget")
                return
            if record.state != "pending":
                raise TaskResultCapacityRecoveryRequired("legacy active parse requires replay cleanup")
            if used + byte_budget > self._limit:
                raise TaskResultCapacityFull("result byte capacity is awaiting owned cleanup")
            record.reserved_result_bytes = byte_budget
            self._persist()

    def reserve_finalizer(self, idempotency_key: str, *, byte_budget: int) -> None:
        if self._storage_policy is not None:
            raise TaskProtocolConflict("storage-managed tasks use completion grants, not B reservations")
        if (
            isinstance(byte_budget, bool)
            or not isinstance(byte_budget, int)
            or byte_budget < 1
        ):
            raise ValueError("finalizer byte reservation must be a positive integer")
        with self._lock:
            record = self._required(idempotency_key)
            if record.state != "finalizing":
                raise TaskProtocolConflict("finalizer reservation state is invalid")
            if record.reserved_result_bytes:
                if record.reserved_result_bytes != byte_budget:
                    raise TaskProtocolConflict("finalizer budget differs from held result reservation")
                return
            if (
                self.unacked_result_bytes + self.reserved_result_bytes + byte_budget
                > self._limit
            ):
                raise TaskProtocolConflict("unacked result byte capacity exhausted")
            record.reserved_result_bytes = byte_budget
            self._persist()


    @property
    def reserved_result_bytes(self) -> int:
        with self._lock:
            durable = sum(
                record.reserved_result_bytes for record in self._records.values()
            )
            if self._uncertain_records is None:
                return durable
            candidate = sum(
                record.reserved_result_bytes
                for record in self._uncertain_records.values()
            )
            return max(durable, candidate)



    @property
    def unacked_result_bytes(self) -> int:
        def usage(records: dict[str, DurableTaskRecord]) -> int:
            return sum(
                record.result_bytes or 0
                for record in records.values()
                if record.state in {"completed", "cleanup_pending"}
            )

        with self._lock:
            durable = usage(self._records)
            if self._uncertain_records is None:
                return durable
            return max(durable, usage(self._uncertain_records))


    # ----------------------------------------------------------------- storage
    @property
    def storage_policy(self) -> Any:
        return self._storage_policy

    @property
    def storage_policy_sha256(self) -> str | None:
        return self._storage_policy_sha256

    @property
    def registry_schema(self) -> str:
        return self._registry_schema

    def _require_storage_mode(self) -> Any:
        policy = self._storage_policy
        if policy is None:
            raise TaskProtocolConflict("result storage is not managed by this registry")
        return policy

    def storage_observation(self, view: DurableRegistryView, executor: "SplitTaskExecutor") -> dict[str, Any]:
        """Lock-free storage facts from one published durable view.

        Runs on the serving loop, so it never takes the registry data lock;
        live ingress charges are read as one atomic copy.
        """
        policy = self._require_storage_mode()
        if executor.storage_policy is not policy:
            raise TaskProtocolConflict("executor and registry storage policies differ")
        source = result = growing = blocked = 0
        waiting = {reason: 0 for reason in sorted(STORAGE_WAIT_REASONS - STORAGE_BLOCKED_REASONS)}
        for record in view.records:
            if record.storage is None:
                continue
            owned_source, owned_result = storage_occupancy(record.storage, policy.physical_charge)
            source += owned_source
            result += owned_result
            growing += record.storage["phase"] == "source_growing"
            reason = record.storage["wait_reason"]
            if reason in STORAGE_BLOCKED_REASONS:
                blocked += 1
            elif reason is not None:
                waiting[reason] += 1
        ingress = sum(tuple(self._ingress_storage.values())) + sum(
            sum(held) for held in tuple(self._request_ingress.values())
        )
        return {
            "policy_sha256": self._storage_policy_sha256,
            "source_bytes": source + ingress,
            "ingress_bytes": ingress,
            "result_bytes": result,
            "growing_producers": growing,
            "outstanding_promise_bytes": executor.outstanding_promise_bytes(),
            "completion_queue_depth": len(executor.completion_queue_snapshot()),
            "waiting_tasks": waiting,
            "blocked_tasks": blocked,
        }

    def task_root_identity(self, idempotency_key: str) -> dict[str, int]:
        """The task root identity pinned at acceptance, for descriptor-relative IO."""
        self._require_storage_mode()
        with self._lock:
            self.assert_observation_safe()
            record = self._required(idempotency_key)
            protocol = None if record.task_payload is None else record.task_payload.get("_agent_protocol")
            identity = None if not isinstance(protocol, dict) else protocol.get("task_root_identity")
            if (
                record.storage is None or type(identity) is not dict
                or set(identity) != {"device", "inode", "uid", "mode"}
                or any(type(value) is not int for value in identity.values())
            ):
                raise TaskProtocolConflict("accepted task root identity is absent")
            return dict(identity)

    def storage_usage(self) -> dict[str, int]:
        """Durable occupancy (the larger of durable/uncertain) plus live ingress.

        ``source`` is the source pool charge, ``result`` retained/granted ZIP
        bytes, ``growing`` the producers holding growth permits.
        """
        with self._lock:
            return self._storage_usage_locked()

    def _storage_usage_locked(self) -> dict[str, int]:
        physical = self._require_storage_mode().physical_charge

        def usage(records: dict[str, DurableTaskRecord]) -> tuple[int, int, int]:
            source = result = growing = 0
            for record in records.values():
                if record.storage is None:
                    continue
                owned_source, owned_result = storage_occupancy(record.storage, physical)
                source += owned_source
                result += owned_result
                growing += record.storage["phase"] == "source_growing"
            return source, result, growing

        source, result, growing = usage(self._records)
        if self._uncertain_records is not None:
            other = usage(self._uncertain_records)
            source, result, growing = (max(source, other[0]), max(result, other[1]), max(growing, other[2]))
        ingress = sum(self._ingress_storage.values()) + sum(
            sum(held) for held in self._request_ingress.values()
        )
        return {
            "source": source + ingress,
            "result": result,
            "growing": growing,
            "ingress": ingress,
        }

    def _admit_ingress_locked(
        self, policy: Any, charge: int, live_free_bytes: int, outstanding_promise_bytes: int,
    ) -> None:
        used = self._storage_usage_locked()
        if (
            used["source"] + charge > policy.native_source_pool_bytes
            or used["source"] + used["result"] + charge
            > policy.native_source_pool_bytes + policy.native_completion_escrow_bytes
        ):
            raise TaskStorageWait("source_growth_capacity")
        if live_free_bytes < policy.native_free_floor_bytes + outstanding_promise_bytes + charge:
            raise TaskStorageWait("free_floor")

    def reserve_ingress_storage(
        self, idempotency_key: str, *, live_free_bytes: int, outstanding_promise_bytes: int,
    ) -> int:
        """Charge one maximal upload before any upload byte is written.

        Process-local: an interrupted ingress is aborted and its bytes removed on
        restart. The acceptance commit replaces it with the actual upload charge.
        """
        policy = self._require_storage_mode()
        _require_storage_amount(live_free_bytes, "live free bytes")
        _require_storage_amount(outstanding_promise_bytes, "outstanding promises")
        with self._lock:
            if idempotency_key in self._ingress_storage:
                raise TaskProtocolConflict("ingress storage is already reserved")
            charge = policy.physical_charge(policy.source_pdf_bytes_limit)
            self._admit_ingress_locked(policy, charge, live_free_bytes, outstanding_promise_bytes)
            self._ingress_storage[idempotency_key] = charge
            return charge

    def release_ingress_storage(self, idempotency_key: str) -> None:
        with self._lock:
            self._ingress_storage.pop(idempotency_key, None)

    def reserve_request_ingress(
        self, token: str, *, body_bytes: int, live_free_bytes: int, outstanding_promise_bytes: int,
    ) -> int:
        """Charge one request body before any byte of it is read.

        The framework spools the whole multipart body before the endpoint runs
        and the endpoint then copies its single upload: both copies are charged
        in this one admission decision, and the upload share later moves to the
        key the body names. Process-local, like every ingress charge.
        """
        policy = self._require_storage_mode()
        if type(token) is not str or not token:
            raise ValueError("request ingress token is invalid")
        _require_storage_amount(body_bytes, "request body bytes")
        _require_storage_amount(live_free_bytes, "live free bytes")
        _require_storage_amount(outstanding_promise_bytes, "outstanding promises")
        spool = policy.physical_charge(body_bytes)
        upload = policy.physical_charge(min(body_bytes, policy.source_pdf_bytes_limit))
        with self._lock:
            if token in self._request_ingress:
                raise TaskProtocolConflict("request ingress is already reserved")
            self._admit_ingress_locked(policy, spool + upload, live_free_bytes, outstanding_promise_bytes)
            self._request_ingress[token] = [spool, upload]
            return spool + upload

    def transfer_request_ingress(self, token: str, idempotency_key: str) -> int:
        """Move a request's reserved upload share to the key its body named."""
        self._require_storage_mode()
        with self._lock:
            held = self._request_ingress.get(token)
            if held is None or held[1] == 0:
                raise TaskProtocolConflict("request has no reserved upload share")
            if idempotency_key in self._ingress_storage:
                raise TaskProtocolConflict("ingress storage is already reserved")
            charge, held[1] = held[1], 0
            self._ingress_storage[idempotency_key] = charge
            return charge

    def release_request_ingress(self, token: str) -> None:
        with self._lock:
            self._request_ingress.pop(token, None)

    def reserve_source_growth(
        self,
        idempotency_key: str,
        *,
        live_free_bytes: int,
        outstanding_promise_bytes: int,
        completion_head_waiting: bool,
    ) -> int:
        """Grant one producer's whole growth permit before its parser starts.

        Completed results are served first: while a sealed source waits for
        completion space no new producer may take source-pool space.
        """
        policy = self._require_storage_mode()
        _require_storage_amount(live_free_bytes, "live free bytes")
        _require_storage_amount(outstanding_promise_bytes, "outstanding promises")
        if type(completion_head_waiting) is not bool:
            raise ValueError("completion head flag must be boolean")
        with self._lock:
            record = self._required(idempotency_key)
            storage = record.storage
            if record.state != "pending" or storage is None:
                raise TaskProtocolConflict("growth permit requires an accepted storage-managed task")
            if storage["phase"] == "source_growing":
                return int(storage["growth_permit_bytes"])
            if storage["phase"] != "admitted":
                raise TaskProtocolConflict("growth permit phase is invalid")
            if any(
                key in self._records and self._records[key].state != "consumed"
                for key in self._replay_required_keys
            ):
                raise TaskResultCapacityRecoveryRequired(
                    "cold task responsibility requires successful replay cleanup"
                )
            permit = policy.native_source_single_limit_bytes
            used = self._storage_usage_locked()
            estimate = policy.initial_result_estimate_bytes
            unzipped = sum(
                other.storage is not None
                and other.storage["phase"] in {"source_growing", "source_sealed"}
                for other in self._records.values()
            )
            reason: str | None = None
            if completion_head_waiting:
                reason = "completion_capacity"
            elif used["growing"] >= policy.native_growing_producer_limit:
                reason = "source_growth_capacity"
            elif (
                used["source"] + permit > policy.native_source_pool_bytes
                or used["source"] + used["result"] + permit
                > policy.native_source_pool_bytes + policy.native_completion_escrow_bytes
            ):
                reason = "source_growth_capacity"
            elif used["result"] + estimate * (unzipped + 1) > policy.native_normal_unacked_target_bytes:
                # Soft back-pressure before the retained results reach their
                # normal target; admitted work keeps its uploads meanwhile.
                reason = "completion_capacity"
            elif live_free_bytes < policy.native_free_floor_bytes + outstanding_promise_bytes + permit:
                reason = "free_floor"
            if reason is not None:
                raise TaskStorageWait(reason)
            record.storage = {
                **storage, "phase": "source_growing", "growth_permit_bytes": permit,
                "wait_reason": None, "wait_since_unix": None,
            }
            validate_task_storage(record.storage, state="pending")
            self._persist()
            return permit

    def seal_source(
        self,
        idempotency_key: str,
        *,
        inventory_sha256: str,
        source_bytes: int,
        selected_bytes: int,
        member_count: int,
        zip_upper_bound_bytes: int,
    ) -> None:
        """Commit the verified source tree and release the unused growth promise."""
        policy = self._require_storage_mode()
        _storage_sha256(inventory_sha256, label="inventory")
        for value, label in (
            (source_bytes, "source bytes"), (selected_bytes, "selected bytes"),
            (member_count, "member count"), (zip_upper_bound_bytes, "ZIP upper bound"),
        ):
            _require_storage_amount(value, label)
        with self._lock:
            record = self._required(idempotency_key)
            storage = record.storage
            if record.state != "processing" or storage is None or storage["phase"] != "source_growing":
                raise TaskProtocolConflict("only a growing source may be sealed")
            if source_bytes > storage["growth_permit_bytes"]:
                raise TaskProtocolConflict("sealed source exceeds its growth permit")
            if member_count > policy.max_members or member_count < 1:
                raise TaskProtocolConflict("sealed source member count is outside the policy")
            record.storage = {
                **storage, "phase": "source_sealed", "growth_permit_bytes": 0,
                "source_bytes": source_bytes, "selected_bytes": selected_bytes,
                "member_count": member_count, "inventory_sha256": inventory_sha256,
                "zip_upper_bound_bytes": zip_upper_bound_bytes,
                "wait_reason": None, "wait_since_unix": None,
            }
            validate_task_storage(record.storage, state="finalizing")
            record.state = "finalizing"
            self._persist()

    def reserve_completion(
        self,
        idempotency_key: str,
        *,
        grant_bytes: int,
        live_free_bytes: int,
        outstanding_promise_bytes: int,
    ) -> None:
        """Grant the completion extent before the ZIP writer creates its part."""
        policy = self._require_storage_mode()
        _require_storage_amount(grant_bytes, "completion grant")
        _require_storage_amount(live_free_bytes, "live free bytes")
        _require_storage_amount(outstanding_promise_bytes, "outstanding promises")
        with self._lock:
            record = self._required(idempotency_key)
            storage = record.storage
            if record.state != "finalizing" or storage is None:
                raise TaskProtocolConflict("completion grant requires a sealed storage-managed task")
            if storage["phase"] == "zip_writing":
                if storage["zip_grant_bytes"] != grant_bytes:
                    raise TaskProtocolConflict("held completion grant differs from the request")
                return
            if storage["phase"] != "source_sealed":
                raise TaskProtocolConflict("completion grant phase is invalid")
            if not 1 <= grant_bytes <= min(storage["zip_upper_bound_bytes"], policy.native_result_hard_limit_bytes):
                raise TaskProtocolConflict("completion grant is outside its bound")
            charge = policy.physical_charge(grant_bytes)
            used = self._storage_usage_locked()
            if (
                used["source"] + used["result"] + charge
                > policy.native_source_pool_bytes + policy.native_completion_escrow_bytes
            ):
                raise TaskStorageWait("completion_capacity")
            if live_free_bytes < policy.native_free_floor_bytes + outstanding_promise_bytes + charge:
                raise TaskStorageWait("free_floor")
            record.storage = {
                **storage, "phase": "zip_writing", "zip_grant_bytes": grant_bytes,
                "wait_reason": None, "wait_since_unix": None,
            }
            validate_task_storage(record.storage, state="finalizing")
            self._persist()

    def release_completion(self, idempotency_key: str, *, wait_reason: str) -> None:
        """Return an unsealed ZIP attempt to its source seal after removing the part."""
        self._require_storage_mode()
        if wait_reason not in STORAGE_WAIT_REASONS or wait_reason in STORAGE_BLOCKED_REASONS:
            raise ValueError("completion release reason is invalid")
        with self._lock:
            record = self._required(idempotency_key)
            storage = record.storage
            if record.state != "finalizing" or storage is None or storage["phase"] != "zip_writing":
                raise TaskProtocolConflict("only a writing completion can be released")
            record.storage = {
                **storage, "phase": "source_sealed", "zip_grant_bytes": 0,
                "wait_reason": wait_reason, "wait_since_unix": self._clock(),
            }
            validate_task_storage(record.storage, state="finalizing")
            self._persist()

    def complete_storage(
        self,
        idempotency_key: str,
        *,
        result_path: Path,
        result_sha256: str,
        result_bytes: int,
        result_owner: str,
    ) -> None:
        """Seal the retained ZIP and true-up the grant in one durable commit."""
        self._require_storage_mode()
        if (
            isinstance(result_bytes, bool) or not isinstance(result_bytes, int) or result_bytes < 1
            or any(
                not isinstance(value, str) or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
                for value in (result_sha256, result_owner)
            )
        ):
            raise ValueError("result identity is invalid")
        with self._lock:
            record = self._required(idempotency_key)
            storage = record.storage
            if record.state != "finalizing" or storage is None or storage["phase"] != "zip_writing":
                raise TaskProtocolConflict("only a writing completion may complete")
            if result_bytes > storage["zip_grant_bytes"]:
                raise TaskProtocolConflict("result exceeded its completion grant")
            record.result_path = str(result_path)
            record.result_sha256 = result_sha256
            record.result_bytes = result_bytes
            record.result_owner = result_owner
            record.storage = {
                **storage, "phase": "zip_sealed", "zip_grant_bytes": 0, "zip_bytes": result_bytes,
                "wait_reason": None, "wait_since_unix": None,
            }
            validate_task_storage(record.storage, state="completed")
            record.state = "completed"
            self._persist()

    def record_storage_wait(self, idempotency_key: str, *, reason: str) -> None:
        """Make a capacity wait durable and visible; never a failure."""
        self._require_storage_mode()
        if reason not in STORAGE_WAIT_REASONS or reason in STORAGE_BLOCKED_REASONS:
            raise ValueError("storage wait reason is invalid")
        with self._lock:
            record = self._required(idempotency_key)
            storage = record.storage
            if storage is None or record.state not in {"pending", "finalizing"}:
                raise TaskProtocolConflict("only accepted storage-managed work can wait")
            if storage["wait_reason"] == reason:
                return
            record.storage = {**storage, "wait_reason": reason, "wait_since_unix": self._clock()}
            validate_task_storage(record.storage, state=record.state)
            self._persist()

    def block_storage(self, idempotency_key: str, *, reason: str) -> None:
        """Hold accepted in-flight work with its bytes for an operator decision."""
        self._require_storage_mode()
        if reason not in STORAGE_BLOCKED_REASONS:
            raise ValueError("storage block reason is invalid")
        with self._lock:
            record = self._required(idempotency_key)
            storage = record.storage
            if storage is None or record.state not in {"processing", "finalizing"}:
                raise TaskProtocolConflict("only accepted in-flight storage-managed work can block")
            if storage["wait_reason"] == reason:
                return
            record.storage = {**storage, "wait_reason": reason, "wait_since_unix": self._clock()}
            validate_task_storage(record.storage, state=record.state)
            self._persist()

    def storage_hold_preview(self, idempotency_key: str, *, runtime_identity_sha256: str) -> dict[str, Any]:
        """The exact reviewable facts of one held task; nothing changes."""
        self._require_storage_mode()
        with self._lock:
            return self._storage_hold_preview_locked(self._required(idempotency_key), runtime_identity_sha256)

    def _storage_hold_preview_locked(
        self, record: DurableTaskRecord, runtime_identity_sha256: str,
    ) -> dict[str, Any]:
        _storage_sha256(runtime_identity_sha256, label="runtime identity")
        storage = record.storage
        if (
            record.state not in {"processing", "finalizing"}
            or storage is None
            or storage["wait_reason"] not in STORAGE_BLOCKED_REASONS
        ):
            raise TaskProtocolConflict("only a held non-terminal storage task has a hold decision")
        facts = {
            "schema": STORAGE_HOLD_PREVIEW_SCHEMA,
            "task_id": record.task_id,
            "idempotency_key": record.idempotency_key,
            "attempt_identity": record.attempt_identity,
            "fence_identity": record.fence_identity,
            "state": record.state,
            "recovery_generation": record.recovery_generation,
            "hold_reason": storage["wait_reason"],
            "storage": copy.deepcopy(storage),
            "registry_schema": self._registry_schema,
            "runtime_identity_sha256": runtime_identity_sha256,
        }
        return {**facts, "preview_sha256": _canonical_sha256(facts)}

    def decide_storage_hold(
        self,
        idempotency_key: str,
        *,
        runtime_identity_sha256: str,
        expected_preview_sha256: str,
        decided_by: str,
        reason: str,
        fixed_by: str,
    ) -> dict[str, Any]:
        """Fail exactly one reviewed held task with the operator's recorded decision.

        The failure and its closed cause are durable before this returns; the
        task's bytes and seal stay charged until the ordinary failed-task ACK.
        The same decision replays to the same receipt; another decision, or a
        task that is no longer exactly the reviewed hold, is refused.
        """
        self._require_storage_mode()
        decision, decision_sha256 = storage_hold_decision(
            preview_sha256=expected_preview_sha256, decided_by=decided_by, reason=reason, fixed_by=fixed_by,
        )
        with self._cleanup_lock:
            with self._lock:
                self._ensure_mutation_allowed("decide_storage_hold")
                record = self._required(idempotency_key)
                applied = record.failure_cause
                if applied is not None and applied["code"] == STORAGE_HOLD_TERMINATED_CODE:
                    if applied["decision_sha256"] != decision_sha256:
                        raise TaskProtocolConflict("held task was already terminated by another decision")
                    return self._storage_hold_receipt(record, applied, replayed=True)
                preview = self._storage_hold_preview_locked(record, runtime_identity_sha256)
                if preview["preview_sha256"] != expected_preview_sha256:
                    raise TaskProtocolConflict("held task no longer matches its reviewed preview")
                if record.active_readers:
                    raise TaskProtocolConflict("held task has a live reader")
                cause = {
                    "schema": TASK_FAILURE_CAUSE_SCHEMA,
                    "task_id": record.task_id,
                    "retry_class": "permanent",
                    "code": STORAGE_HOLD_TERMINATED_CODE,
                    "http_status": None,
                    "transport_error": None,
                    "hold_reason": preview["hold_reason"],
                    "decision_sha256": decision_sha256,
                    "decision": decision,
                }
                self.fail(
                    idempotency_key,
                    error=f"storage hold {preview['hold_reason']} terminated by operator decision {decision_sha256}",
                    failure_cause=cause,
                )
                return self._storage_hold_receipt(self._required(idempotency_key), cause, replayed=False)

    @staticmethod
    def _storage_hold_receipt(
        record: DurableTaskRecord, cause: dict[str, Any], *, replayed: bool,
    ) -> dict[str, Any]:
        """Built only from the durable cause: a replay returns the original attribution."""
        return {
            "schema": STORAGE_HOLD_RECEIPT_SCHEMA,
            "task_id": record.task_id,
            "idempotency_key": record.idempotency_key,
            "attempt_identity": record.attempt_identity,
            "fence_identity": record.fence_identity,
            "hold_reason": cause["hold_reason"],
            "preview_sha256": cause["decision"]["preview_sha256"],
            "decision_sha256": cause["decision_sha256"],
            "decision": dict(cause["decision"]),
            "state": record.state,
            "replayed": replayed,
        }

    def lease(self, idempotency_key: str, *, seconds: float) -> float:
        if seconds <= 0:
            raise ValueError("lease duration must be positive")
        with self._lock:
            record = self._required(idempotency_key)
            if record.state != "completed":
                raise TaskProtocolConflict("only completed results can be leased")
            record.lease_until_unix = time.time() + seconds
            self._persist()
            return record.lease_until_unix

    @contextmanager
    def open_result(self, idempotency_key: str) -> Iterator[Path]:
        path = self.acquire_result(idempotency_key)
        try:
            yield path
        except BaseException as primary_error:
            try:
                self.release_result(idempotency_key)
            except BaseException as release_error:
                primary_error.add_note(
                    "result reader release also failed: "
                    f"{type(release_error).__name__}"
                )
                raise primary_error from release_error
            raise
        else:
            self.release_result(idempotency_key)

    def acquire_result(self, idempotency_key: str) -> Path:
        with self._lock:
            record = self._required(idempotency_key)
            if record.state != "completed" or not record.result_path:
                raise TaskProtocolConflict("result is unavailable")
            if not record.lease_until_unix or record.lease_until_unix <= time.time():
                raise TaskProtocolConflict("result lease is absent or expired")
            record.active_readers += 1
            self._persist()
            return Path(record.result_path)

    def acquire_inline_result(self, idempotency_key: str) -> Path:
        """Pin one completed tree for the service's own synchronous response.

        This is not an external lease bypass: no HTTP route exposes it directly.
        It uses the same reader count, ACK exclusion and durable release as leases.
        """
        with self._lock:
            record = self._required(idempotency_key)
            if record.state != "completed" or not record.result_path:
                raise TaskProtocolConflict("inline result is unavailable")
            record.active_readers += 1
            self._persist()
            return Path(record.result_path)

    def release_result(self, idempotency_key: str) -> None:
        with self._lock:
            current = self._required(idempotency_key)
            if current.active_readers < 1:
                raise RuntimeError("result reader count underflowed")
            current.active_readers -= 1
            self._persist()

    def acknowledge(self, idempotency_key: str) -> None:
        with self._lock:
            record = self._required(idempotency_key)
            if record.state in {"cleanup_pending", "consumed"}:
                return
            if record.state != "completed" or record.active_readers:
                raise TaskProtocolConflict(
                    "result cannot be ACKed while unavailable/in use"
                )
            record.state = "cleanup_pending"
            record.cleanup_kind = "result"
            try:
                self._persist()
            except BaseException:
                record.state = "completed"
                record.cleanup_kind = None
                raise

    def cleanup_consumed(
        self, unlink: Callable[[Path], None] | None = None,
        *, idempotency_key: str | None = None,
    ) -> int:
        with self._cleanup_lock:
            with self._lock:
                self._ensure_mutation_allowed("cleanup_consumed")
                removable = [key for key, record in self._records.items()
                             if record.state == "cleanup_pending" and record.active_readers == 0
                             and (idempotency_key is None or key == idempotency_key)]
            cleaned = 0
            for key in removable:
                with self._lock:
                    self._ensure_mutation_allowed("cleanup_consumed")
                    current = self._required(key)
                    if current.state != "cleanup_pending" or current.active_readers:
                        raise TaskProtocolConflict("cleanup responsibility changed before IO")
                    expected = copy.deepcopy(current)
                self._unlink_owned_result(expected, before_unlink=unlink)
                self._finish_cleanup(key, expected)
                cleaned += 1
            return cleaned

    def _finish_cleanup(self, key: str, expected: DurableTaskRecord) -> None:
        record = self._required(key)
        if record != expected or record.state != "cleanup_pending" or record.active_readers:
            raise TaskProtocolConflict("cleanup responsibility changed during IO")
        record.result_path = None
        record.task_payload = None
        record.lease_until_unix = None
        record.error = None
        record.failure_cause = None
        record.reserved_result_bytes = 0
        record.storage = None
        record.state = "consumed"
        record.consumed_at_unix = self._clock()
        record.cleanup_kind = None
        self._persist()

    async def observe(self, reader: Callable[[], Any], *, timeout_seconds: float = 0.7) -> Any:
        if not 0 < timeout_seconds <= 0.7:
            raise ValueError("registry observation budget is invalid")
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TaskRegistryObservationBusy("task registry observation is busy")
            if self._lock.acquire(blocking=False):
                break
            await asyncio.sleep(min(0.002, remaining))
        try:
            self.assert_observation_safe()
            result = reader()
            if hasattr(result, "__await__"):
                if asyncio.iscoroutine(result):
                    result.close()
                raise TypeError("registry observation callback must not await")
            return result
        finally:
            self._lock.release()

    def _unlink_owned_result(
        self,
        record: DurableTaskRecord,
        *,
        before_unlink: Callable[[Path], None] | None,
    ) -> None:
        payload = record.task_payload or {}
        if record.state == "ingress_cleanup" and record.ingress_owner is not None:
            payload = {
                "output_dir": str(self._output_root / record.task_id),
                "_agent_protocol": record.ingress_owner,
            }
        protocol = payload.get("_agent_protocol")
        output_value = payload.get("output_dir")
        if not isinstance(protocol, dict) or not isinstance(output_value, str):
            if record.task_payload is None and record.result_path is None:
                return
            if not self._enforce_key_lifecycle:
                if record.result_path:
                    result_path = Path(record.result_path)
                    if before_unlink is None:
                        result_path.unlink(missing_ok=True)
                    else:
                        try:
                            before_unlink(result_path)
                        except FileNotFoundError:
                            pass
                    self._sync_namespace_path(result_path.parent)
                return
            raise TaskProtocolConflict("cleanup task ownership receipt is absent")
        output = Path(output_value)
        result = Path(record.result_path or "")
        if record.cleanup_kind == "result" and (
            result.parent.resolve() != output.resolve()
            or result.name in {"", ".", ".."}
        ):
            raise TaskProtocolConflict("cleanup result escaped task root")
        try:
            root_fd, task_fd = self._open_task_dir(record.task_id)
        except FileNotFoundError:
            root_fd = os.open(
                self._output_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            primary_error: BaseException | None = None
            try:
                root_meta = os.fstat(root_fd)
                if (
                    root_meta.st_dev,
                    root_meta.st_ino,
                    root_meta.st_uid,
                    root_meta.st_mode,
                ) != self._output_root_identity:
                    raise TaskProtocolConflict("configured output root identity drifted")
                expected = protocol.get("task_root_identity")
                if not isinstance(expected, dict):
                    raise TaskProtocolConflict("cleanup task identity is absent")
                entries = os.listdir(root_fd)
                if len(entries) > _MAX_RECORDS + _MAX_TOMBSTONES:
                    raise TaskProtocolConflict("cleanup output-root scan exceeded bound")
                for entry in entries:
                    metadata = os.stat(
                        entry, dir_fd=root_fd, follow_symlinks=False
                    )
                    if (
                        metadata.st_dev == expected.get("device")
                        and metadata.st_ino == expected.get("inode")
                    ):
                        raise TaskProtocolConflict(
                            "owned task directory was renamed during cleanup"
                        )
                # A prior attempt may have removed the task entry before its
                # output-parent fsync.  Syncing the still-pinned output root
                # makes the observed absence durable before consuming intent.
                self._fsync_namespace_directory(root_fd)
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                self._close_namespace_descriptors(
                    ((root_fd, "cleanup output root"),),
                    primary_error,
                )
            return
        cleanup_error: BaseException | None = None
        try:
            if self._directory_identity(os.fstat(task_fd)) != protocol.get(
                "task_root_identity"
            ):
                raise TaskProtocolConflict("cleanup task directory identity drifted")
            expected_upload_root = protocol.get("uploads_root_identity")
            if not isinstance(expected_upload_root, dict):
                raise TaskProtocolConflict("cleanup uploads identity is absent")
            try:
                upload_fd = os.open(
                    "uploads",
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=task_fd,
                )
            except FileNotFoundError:
                # A prior cleanup attempt may already have removed the owned
                # uploads tree.  Reject a rename of that same inode, but allow
                # exact intent replay to continue with the remaining children.
                entries = os.listdir(task_fd)
                if len(entries) > _MAX_RECORDS + _MAX_TOMBSTONES:
                    raise TaskProtocolConflict(
                        "cleanup task-root scan exceeded bound"
                    )
                for entry in entries:
                    metadata = os.stat(
                        entry, dir_fd=task_fd, follow_symlinks=False
                    )
                    if (
                        metadata.st_dev == expected_upload_root.get("device")
                        and metadata.st_ino == expected_upload_root.get("inode")
                    ):
                        raise TaskProtocolConflict(
                            "owned uploads directory was renamed during cleanup"
                        )
            else:
                upload_error: BaseException | None = None
                try:
                    if self._directory_identity(os.fstat(upload_fd)) != expected_upload_root:
                        raise TaskProtocolConflict(
                            "cleanup uploads directory identity drifted"
                        )
                except BaseException as exc:
                    upload_error = exc
                    raise
                finally:
                    self._close_namespace_descriptors(
                        ((upload_fd, "cleanup uploads directory"),),
                        upload_error,
                    )
            if record.cleanup_kind == "result":
                result_metadata: os.stat_result | None
                try:
                    result_metadata = os.stat(
                        result.name, dir_fd=task_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    result_metadata = None
                if result_metadata is not None and (
                    not stat.S_ISREG(result_metadata.st_mode)
                    or result_metadata.st_nlink != 1
                ):
                    raise TaskProtocolConflict("cleanup result identity is unsafe")
                if before_unlink is not None and result_metadata is not None:
                    before_unlink(result)
            for child in os.listdir(task_fd):
                self._remove_at(task_fd, child)
            self._fsync_namespace_directory(task_fd)
            closing_task_fd = task_fd
            task_fd = -1
            self._close_namespace_descriptors(
                ((closing_task_fd, "cleanup task directory"),),
                None,
            )
            os.rmdir(record.task_id, dir_fd=root_fd)
            self._fsync_namespace_directory(root_fd)
        except BaseException as exc:
            cleanup_error = exc
            raise
        finally:
            self._close_namespace_descriptors(
                (
                    (task_fd, "cleanup task directory"),
                    (root_fd, "cleanup output root"),
                ),
                cleanup_error,
            )

    def _required(self, key: str) -> DurableTaskRecord:
        try:
            return self._records[key]
        except KeyError as exc:
            raise TaskProtocolConflict("task is unknown") from exc

    def _load(self) -> dict[str, DurableTaskRecord]:
        try:
            descriptor = os.open(
                self._path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
        except FileNotFoundError:
            return {}
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or not 0 < metadata.st_size <= _MAX_REGISTRY_BYTES
            ):
                raise TaskProtocolConflict("task registry file identity is unsafe")
            chunks = []
            remaining = _MAX_REGISTRY_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
            def identity(value: os.stat_result) -> tuple[int, ...]:
                return (
                    value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                    value.st_nlink, value.st_size, value.st_mtime_ns,
                    value.st_ctime_ns,
                )
            if len(raw) != metadata.st_size or identity(metadata) != identity(after):
                raise TaskProtocolConflict("task registry changed while reading")
        finally:
            os.close(descriptor)

        return self._decode_registry(raw)

    def _decode_registry(
        self, raw: bytes, *, require_quiescent: bool = False
    ) -> dict[str, DurableTaskRecord]:
        """Decode without repair when used by a read-only commissioning probe."""
        def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise TaskProtocolConflict(
                        "task registry contains duplicate fields"
                    )
                value[key] = item
            return value

        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=closed_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                TaskProtocolConflict(f"non-finite registry value: {value}")
            ),
        )
        storage_mode = getattr(self, "_storage_policy", None) is not None
        if require_quiescent:
            # A quiescent proof owns no resources, so every version is readable.
            accepted_schemas = {"mineru-task-registry.v2", REGISTRY_SCHEMA_V3, REGISTRY_SCHEMA_V4}
        elif storage_mode:
            accepted_schemas = {REGISTRY_SCHEMA_V3, REGISTRY_SCHEMA_V4}
        else:
            accepted_schemas = {"mineru-task-registry.v2", REGISTRY_SCHEMA_V3}
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema", "output_root", "submission_watermark_bucket", "records"}
            or payload.get("schema") not in accepted_schemas
        ):
            raise TaskProtocolConflict("task registry schema is invalid")
        expected_root = {
            "path": str(self._output_root),
            "device": self._output_root_identity[0],
            "inode": self._output_root_identity[1],
            "uid": self._output_root_identity[2],
            "mode": self._output_root_identity[3],
        }
        if require_quiescent and (
            not isinstance(payload.get("output_root"), dict)
            or any(type(payload["output_root"].get(key)) is not int
                   for key in ("device", "inode", "uid", "mode"))
            or json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() != raw
        ):
            raise TaskProtocolConflict("quiescent registry is not canonical")
        if payload.get("output_root") != expected_root:
            raise TaskProtocolConflict("configured output root identity drifted")
        watermark = payload.get("submission_watermark_bucket")
        if not isinstance(watermark, int) or isinstance(watermark, bool) or watermark < -1:
            raise TaskProtocolConflict("submission watermark is invalid")
        self._submission_watermark_bucket = watermark
        records = payload.get("records")
        if not isinstance(records, list) or len(records) > _MAX_RECORDS + _MAX_TOMBSTONES:
            raise TaskProtocolConflict("task registry records are invalid")
        # A v3 record carries failure_cause only while a typed failed task awaits
        # ACK; every other record keeps the exact pre-cause v3 field set. Only
        # v4 records may carry the closed storage field.
        expected = {item.name for item in fields(DurableTaskRecord)} - {"failure_cause", "storage"}
        legacy = payload["schema"] == "mineru-task-registry.v2"
        v4 = payload["schema"] == REGISTRY_SCHEMA_V4
        if legacy:
            expected -= {"ingress_owner"}
        allowed = expected if legacy else expected | {"failure_cause"}
        if v4:
            allowed = allowed | {"storage"}
        if any(
            not isinstance(item, dict) or not expected <= set(item) <= allowed
            for item in records
        ):
            raise TaskProtocolConflict("task registry record fields are not closed")
        loaded = {}
        task_ids = set()
        for item in records:
            record = DurableTaskRecord(**item)
            if legacy and record.state in {"ingress", "ingress_cleanup"}:
                raise TaskProtocolConflict("v2 registry cannot contain ingress states")
            if require_quiescent and (
                record.state != "consumed"
                or record.active_readers != 0
                or record.reserved_result_bytes != 0
                or record.consumed_at_unix is None
                or record.consumed_at_unix < 0
                or any(value is not None for value in (
                    record.result_path, record.task_payload, record.lease_until_unix,
                    record.error, record.cleanup_kind, record.failure_cause,
                ))
            ):
                raise TaskProtocolConflict("output registry still owns task resources")
            if record.idempotency_key in loaded or record.task_id in task_ids:
                raise TaskProtocolConflict("task registry identities are not unique")
            if storage_mode and not require_quiescent and record.state != "consumed":
                # v3 in-flight work was accepted under the retired B/L rules;
                # a storage-managed process never adopts it silently.
                if not v4 or record.storage is None:
                    raise TaskProtocolConflict(
                        "storage-managed registry requires every live task to carry storage"
                    )
                if record.storage["policy_sha256"] != self._storage_policy_sha256:
                    raise TaskProtocolConflict(
                        "live task storage was granted under a different storage policy"
                    )
            if record.state not in {
                "ingress",
                "ingress_cleanup",
                "pending",
                "processing",
                "finalizing",
                "completed",
                "failed",
                "cleanup_pending",
                "consumed",
            }:
                raise TaskProtocolConflict("task registry state is invalid")
            loaded[record.idempotency_key] = record
            task_ids.add(record.task_id)
        for record in loaded.values():
            if not require_quiescent:
                record.active_readers = 0
            if record.state in {"completed", "cleanup_pending", "consumed"} and record.result_path:
                result_path = Path(record.result_path or "")
                try:
                    metadata = result_path.lstat()
                except FileNotFoundError:
                    if record.state == "cleanup_pending":
                        continue
                    raise TaskProtocolConflict(
                        "retained result disappeared outside cleanup intent"
                    ) from None
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise TaskProtocolConflict("retained result file identity is unsafe")
                digest = hashlib.sha256()
                total = 0
                with result_path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        total += len(chunk)
                expected_owner = hashlib.sha256(
                    f"{record.task_id}\0{digest.hexdigest()}\0{total}".encode()
                ).hexdigest()
                if (
                    digest.hexdigest() != record.result_sha256
                    or total != record.result_bytes
                    or expected_owner != record.result_owner
                ):
                    raise TaskProtocolConflict("retained result identity drifted")
        return loaded


    @staticmethod
    def _clone_records(
        records: dict[str, DurableTaskRecord],
    ) -> dict[str, DurableTaskRecord]:
        return copy.deepcopy(records)

    def _restore_last_durable_state(self) -> None:
        self._records = self._clone_records(self._last_durable_records)
        self._submission_watermark_bucket = (
            self._last_durable_watermark_bucket
        )
        self._publish_durable_view()

    def _publish_durable_view(self) -> None:
        """Replace the observable projection with the current durable state."""
        view = DurableRegistryView(
            records=tuple(
                copy.deepcopy(record)
                for record in sorted(
                    self._last_durable_records.values(),
                    key=lambda item: item.idempotency_key,
                )
            ),
            submission_watermark_bucket=self._last_durable_watermark_bucket,
            persistence_generation=self._persistence_generation,
            persistence_event=copy.deepcopy(self._last_persistence_event),
            durability_uncertain=self._uncertain_records is not None,
            published_monotonic_ns=time.monotonic_ns(),
            registry_schema=self._registry_schema,
        )
        self._durable_view = view

    def _current_state_differs_from_last_durable(self) -> bool:
        return (
            self._submission_watermark_bucket
            != self._last_durable_watermark_bucket
            or self._records != self._last_durable_records
        )

    def _record_persistence_event(
        self,
        *,
        outcome: str,
        phase: str,
        committed: bool,
        operation: str | None = None,
        cause: BaseException | None = None,
        cleanup_cause: BaseException | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "outcome": outcome,
            "phase": phase,
            "committed": committed,
            "operation": operation or self._active_operation,
        }
        if cause is not None:
            event["cause_type"] = type(cause).__name__
        if cleanup_cause is not None:
            event["cleanup_cause_type"] = type(cleanup_cause).__name__
        self._last_persistence_event = event
        self._last_persistence_cause = cause
        self._last_persistence_cleanup_cause = cleanup_cause
        self._publish_durable_view()

    def persistence_status(self) -> dict[str, Any]:
        with self._lock:
            if self._uncertain_records is not None:
                state = "durability_uncertain"
                event = self._last_persistence_event or {}
                if event.get("operation") in {"acquire_result", "release_result"}:
                    recovery_action = "restart_registry_process"
                else:
                    recovery_action = "call recover_persistence_uncertainty"
            elif self._last_persistence_event is not None:
                state = "degraded"
                event = self._last_persistence_event
                if bool(event.get("committed")):
                    recovery_action = "do_not_retry_committed_operation"
                else:
                    recovery_action = "retry_idempotent_operation"
            else:
                state = "healthy"
                recovery_action = None
            return {
                "state": state,
                "last_event": copy.deepcopy(self._last_persistence_event),
                "candidate_idempotency_keys": sorted(
                    self._uncertain_records or {}
                ),
                "recovery_action": recovery_action,
            }

    def recover_persistence_uncertainty(self) -> dict[str, Any]:
        with self._cleanup_lock:
            return self._recover_persistence_uncertainty_locked()

    def _recover_persistence_uncertainty_locked(self) -> dict[str, Any]:
        """Durably reconcile an ambiguous replace without cold-decoding readers.

        Exact saved snapshots are selected only after a successful parent fsync
        and stable byte reread.  Reader-count mutations require a cold process
        restart because a failed acquire may not have returned a handle and a
        failed release may already have relinquished one.
        """
        with self._lock:
            if self._uncertain_records is None:
                return self.persistence_status()
            event = self._last_persistence_event or {}
            operation = str(event.get("operation", "unknown"))
            original_cause = self._last_persistence_cause
            original_cleanup_cause = self._last_persistence_cleanup_cause
            if operation in {"acquire_result", "release_result"}:
                error = TaskRegistryPersistenceError(
                    operation=operation,
                    phase="reader_mutation_requires_cold_restart",
                    outcome="durability_uncertain",
                    committed=False,
                )
                cause = original_cleanup_cause or original_cause
                if cause is None:
                    raise error
                raise error from cause
            candidate_payload = self._uncertain_payload
            candidate_records = self._uncertain_records
            candidate_watermark = self._uncertain_watermark_bucket
            if candidate_payload is None or candidate_watermark is None:
                raise TaskProtocolConflict(
                    "task registry uncertainty snapshot is incomplete"
                )
            try:
                visible = self._read_current_registry_bytes()
                if visible not in {candidate_payload, self._durable_payload}:
                    raise TaskProtocolConflict(
                        "task registry bytes changed during explicit recovery"
                    )
                close_error = self._sync_parent_directory()
                visible_after = self._read_current_registry_bytes()
                if visible_after != visible:
                    raise TaskProtocolConflict(
                        "task registry bytes changed during explicit recovery"
                    )
            except BaseException as exc:
                self._record_persistence_event(
                    outcome="durability_uncertain",
                    phase="explicit_parent_fsync",
                    committed=False,
                    operation=operation,
                    cause=exc,
                )
                raise TaskRegistryPersistenceError(
                    operation=operation,
                    phase="explicit_parent_fsync",
                    outcome="durability_uncertain",
                    committed=False,
                ) from exc

            if visible == candidate_payload:
                self._records = self._clone_records(candidate_records)
                self._submission_watermark_bucket = candidate_watermark
                if close_error is None:
                    self._mark_durable_commit(
                        candidate_payload,
                        outcome="committed_after_explicit_recovery",
                        phase="explicit_parent_fsync",
                        operation=operation,
                    )
                else:
                    self._mark_durable_commit(
                        candidate_payload,
                        outcome="committed_cleanup_failed",
                        phase="explicit_parent_close",
                        cause=original_cause,
                        cleanup_cause=close_error,
                        operation=operation,
                    )
            else:
                self._restore_last_durable_state()
                self._uncertain_records = None
                self._uncertain_watermark_bucket = None
                self._uncertain_payload = None
                self._persistence_generation += 1
                outcome = "not_committed_after_explicit_recovery"
                phase = "explicit_parent_fsync"
                if close_error is not None:
                    outcome = "not_committed_cleanup_failed"
                    phase = "explicit_parent_close"
                self._record_persistence_event(
                    outcome=outcome,
                    phase=phase,
                    committed=False,
                    operation=operation,
                    cause=original_cause,
                    cleanup_cause=close_error or original_cleanup_cause,
                )
            return self.persistence_status()

    def _raise_current_persistence_error(
        self,
        *,
        operation: str,
        phase: str,
        outcome: str,
        committed: bool,
    ) -> None:
        error = TaskRegistryPersistenceError(
            operation=operation,
            phase=phase,
            outcome=outcome,
            committed=committed,
        )
        cause = self._last_persistence_cleanup_cause or self._last_persistence_cause
        if cause is None:
            raise error
        raise error from cause

    @staticmethod
    def _raise_view_persistence_error(
        event: dict[str, Any] | None, *, durability_uncertain: bool
    ) -> None:
        """Fail a published projection exactly as assert_persistence_healthy would.

        A projection carries no exception objects, so the structured error is
        raised without the original cause.
        """
        if event is None:
            if not durability_uncertain:
                return
            raise TaskRegistryPersistenceError(
                operation="unknown",
                phase="replace_reconciliation",
                outcome="durability_uncertain",
                committed=False,
            )
        raise TaskRegistryPersistenceError(
            operation=str(event["operation"]),
            phase=str(event["phase"]),
            outcome=str(event["outcome"]),
            committed=bool(event["committed"]),
        )

    def assert_observation_safe(self) -> None:
        if self._uncertain_records is None:
            return
        event = self._last_persistence_event or {}
        self._raise_current_persistence_error(
            operation=str(event.get("operation", self._active_operation)),
            phase=str(event.get("phase", "replace_reconciliation")),
            outcome="durability_uncertain",
            committed=False,
        )

    def assert_persistence_healthy(self) -> None:
        with self._lock:
            event = self._last_persistence_event
            if event is None:
                return
            self._raise_current_persistence_error(
                operation=str(event["operation"]),
                phase=str(event["phase"]),
                outcome=str(event["outcome"]),
                committed=bool(event["committed"]),
            )

    def _ensure_mutation_allowed(self, operation: str) -> None:
        if self._uncertain_records is None:
            return
        event = self._last_persistence_event or {}
        self._raise_current_persistence_error(
            operation=operation,
            phase=str(event.get("phase", "replace_reconciliation")),
            outcome="durability_uncertain",
            committed=False,
        )

    def _read_current_registry_bytes(self) -> bytes | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._path, flags)
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or not 0 < before.st_size <= _MAX_REGISTRY_BYTES
            ):
                raise TaskProtocolConflict(
                    "task registry file identity is unsafe"
                )
            chunks: list[bytes] = []
            remaining = _MAX_REGISTRY_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(fd)

            def identity(value: os.stat_result) -> tuple[int, ...]:
                return (
                    value.st_dev,
                    value.st_ino,
                    value.st_mode,
                    value.st_uid,
                    value.st_nlink,
                    value.st_size,
                    value.st_mtime_ns,
                    value.st_ctime_ns,
                )

            if len(raw) != before.st_size or identity(before) != identity(after):
                raise TaskProtocolConflict(
                    "task registry changed while reading"
                )
            return raw
        finally:
            os.close(fd)

    @staticmethod
    def _write_registry_stream(stream: Any, payload: bytes) -> None:
        written = stream.write(payload)
        if written != len(payload):
            raise OSError("short task registry write")

    @staticmethod
    def _flush_registry_stream(stream: Any) -> None:
        stream.flush()

    @staticmethod
    def _fsync_registry_file(fd: int) -> None:
        os.fsync(fd)

    @staticmethod
    def _close_registry_stream(stream: Any) -> None:
        stream.close()

    @staticmethod
    def _replace_registry_file(source: Path, destination: Path) -> None:
        os.replace(source, destination)

    @staticmethod
    def _fsync_parent_descriptor(fd: int) -> None:
        os.fsync(fd)

    @staticmethod
    def _close_parent_descriptor(fd: int) -> None:
        os.close(fd)

    @staticmethod
    def _cleanup_temp_path(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _sync_parent_directory(self) -> BaseException | None:
        directory_fd = os.open(
            self._path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        fsync_error: BaseException | None = None
        close_error: BaseException | None = None
        try:
            self._fsync_parent_descriptor(directory_fd)
        except BaseException as exc:
            fsync_error = exc
        try:
            self._close_parent_descriptor(directory_fd)
        except BaseException as exc:
            close_error = exc
        if fsync_error is not None:
            if close_error is not None:
                fsync_error.add_note(
                    "task registry parent close also failed: "
                    f"{type(close_error).__name__}"
                )
            raise fsync_error
        return close_error

    def _mark_durable_commit(
        self,
        payload: bytes,
        *,
        outcome: str | None = None,
        phase: str = "parent_fsync",
        cause: BaseException | None = None,
        cleanup_cause: BaseException | None = None,
        operation: str | None = None,
    ) -> None:
        self._durable_payload = payload
        self._last_durable_records = self._clone_records(self._records)
        self._last_durable_watermark_bucket = (
            self._submission_watermark_bucket
        )
        self._uncertain_records = None
        self._uncertain_watermark_bucket = None
        self._uncertain_payload = None
        self._persistence_generation += 1
        if outcome is None:
            self._last_persistence_event = None
            self._last_persistence_cause = None
            self._last_persistence_cleanup_cause = None
            self._publish_durable_view()
        else:
            self._record_persistence_event(
                outcome=outcome,
                phase=phase,
                committed=True,
                operation=operation,
                cause=cause,
                cleanup_cause=cleanup_cause,
            )

    def _raise_persistence_failure(
        self,
        *,
        outcome: str,
        phase: str,
        candidate_payload: bytes,
        cause: BaseException | None = None,
        cleanup_cause: BaseException | None = None,
    ) -> None:
        if outcome == "durability_uncertain":
            self._uncertain_records = self._clone_records(self._records)
            self._uncertain_watermark_bucket = (
                self._submission_watermark_bucket
            )
            self._uncertain_payload = candidate_payload
        self._record_persistence_event(
            outcome=outcome,
            phase=phase,
            committed=False,
            cause=cause,
            cleanup_cause=cleanup_cause,
        )
        error = TaskRegistryPersistenceError(
            operation=self._active_operation,
            phase=phase,
            outcome=outcome,
            committed=False,
        )
        if cause is None:
            raise error
        raise error from cause

    def _resolve_replace_outcome(
        self,
        *,
        candidate_payload: bytes,
        phase: str,
        primary_error: BaseException,
        cleanup_error: BaseException | None,
    ) -> None:
        try:
            visible = self._read_current_registry_bytes()
        except BaseException as exc:
            self._raise_persistence_failure(
                outcome="durability_uncertain",
                phase=f"{phase}_readback",
                candidate_payload=candidate_payload,
                cause=exc,
                cleanup_cause=cleanup_error,
            )
        previous_payload = self._durable_payload
        if visible == candidate_payload:
            visible_kind = "candidate"
        elif visible == previous_payload:
            visible_kind = "previous"
        else:
            self._raise_persistence_failure(
                outcome="durability_uncertain",
                phase=f"{phase}_ambiguous_bytes",
                candidate_payload=candidate_payload,
                cause=primary_error,
                cleanup_cause=cleanup_error,
            )
            raise AssertionError("unreachable")
        retry_phase = (
            "parent_fsync_retry"
            if phase == "parent_fsync"
            else f"{phase}_parent_fsync_retry"
        )
        try:
            close_error = self._sync_parent_directory()
            visible_after = self._read_current_registry_bytes()
        except BaseException as exc:
            self._raise_persistence_failure(
                outcome="durability_uncertain",
                phase=retry_phase,
                candidate_payload=candidate_payload,
                cause=exc,
                cleanup_cause=cleanup_error,
            )
            raise AssertionError("unreachable")
        if visible_after != visible:
            self._raise_persistence_failure(
                outcome="durability_uncertain",
                phase=f"{phase}_changed_during_reconciliation",
                candidate_payload=candidate_payload,
                cause=primary_error,
                cleanup_cause=cleanup_error,
            )
        if visible_kind == "previous":
            self._raise_persistence_failure(
                outcome="not_committed",
                phase=f"{phase}_previous_durable",
                candidate_payload=candidate_payload,
                cause=primary_error,
                cleanup_cause=cleanup_error or close_error,
            )
        warning = cleanup_error or close_error
        if warning is None:
            self._mark_durable_commit(
                candidate_payload,
                outcome="committed_after_recovery",
                phase=retry_phase,
                cause=primary_error,
            )
            return
        self._mark_durable_commit(
            candidate_payload,
            outcome="committed_cleanup_failed",
            phase=f"{phase}_cleanup_after_recovery",
            cause=primary_error,
            cleanup_cause=warning,
        )

    def _persist_serialized_payload(self, payload: bytes) -> None:
        temp_path: Path | None = None
        stream: Any | None = None
        raw_fd: int | None = None
        phase = "temp_create"
        replace_attempted = False
        durability_established = False
        primary_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        parent_close_error: BaseException | None = None
        try:
            raw_fd, temp_name = tempfile.mkstemp(
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                dir=self._path.parent,
            )
            temp_path = Path(temp_name)
            stream = os.fdopen(raw_fd, "wb")
            raw_fd = None
            phase = "write"
            self._write_registry_stream(stream, payload)
            phase = "flush"
            self._flush_registry_stream(stream)
            phase = "file_fsync"
            self._fsync_registry_file(stream.fileno())
            phase = "file_close"
            self._close_registry_stream(stream)
            stream = None
            phase = "replace"
            replace_attempted = True
            self._replace_registry_file(temp_path, self._path)
            phase = "parent_fsync"
            parent_close_error = self._sync_parent_directory()
            durability_established = True
        except BaseException as exc:
            primary_error = exc
        if stream is not None:
            try:
                self._close_registry_stream(stream)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if raw_fd is not None:
            try:
                os.close(raw_fd)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if temp_path is not None:
            try:
                self._cleanup_temp_path(temp_path)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc

        if durability_established:
            warning = cleanup_error or parent_close_error
            if warning is None:
                self._mark_durable_commit(payload)
                return
            self._mark_durable_commit(
                payload,
                outcome="committed_cleanup_failed",
                phase="post_commit_cleanup",
                cleanup_cause=warning,
            )
            return

        if primary_error is None:
            primary_error = cleanup_error or OSError(
                "registry persistence did not commit"
            )
        if replace_attempted:
            self._resolve_replace_outcome(
                candidate_payload=payload,
                phase=phase,
                primary_error=primary_error,
                cleanup_error=cleanup_error,
            )
            return
        if cleanup_error is not None and cleanup_error is not primary_error:
            primary_error.add_note(
                "task registry temp cleanup also failed: "
                f"{type(cleanup_error).__name__}"
            )
        # Before replace, the target name was never exchanged.
        self._raise_persistence_failure(
            outcome="not_committed",
            phase=phase if cleanup_error is None else f"{phase}_temp_cleanup",
            candidate_payload=payload,
            cause=primary_error,
            cleanup_cause=cleanup_error,
        )

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {
                    "schema": self._registry_schema,
                    "output_root": {
                        "path": str(self._output_root),
                        "device": self._output_root_identity[0],
                        "inode": self._output_root_identity[1],
                        "uid": self._output_root_identity[2],
                        "mode": self._output_root_identity[3],
                    },
                    "submission_watermark_bucket": self._submission_watermark_bucket,
                    "records": [
                        registry_record_payload(record)
                        for record in sorted(
                            self._records.values(), key=lambda item: item.idempotency_key
                        )
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            if len(payload) > _MAX_REGISTRY_BYTES:
                raise TaskProtocolConflict(
                    "task registry exceeds the closed envelope"
                )
        except TaskProtocolConflict:
            raise
        except BaseException as exc:
            self._record_persistence_event(
                outcome="not_committed",
                phase="serialize_or_prepare",
                committed=False,
                cause=exc,
            )
            raise TaskRegistryPersistenceError(
                operation=self._active_operation,
                phase="serialize_or_prepare",
                outcome="not_committed",
                committed=False,
            ) from exc
        self._persist_serialized_payload(payload)



def inspect_quiescent_output_root(
    root: Path, *, allow_empty: bool = False
) -> dict[str, Any]:
    """Read-only point-in-time proof under operator writer exclusion, not a lock.

    Preserve physical inventory and consumed tombstones. Pin/recheck ancestors,
    both directories and registry; never repair, delete, or recursively scan.
    """
    if not root.is_absolute() or ".." in root.parts:
        raise TaskProtocolConflict("output root must be an absolute canonical path")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    opened: list[int] = []
    edges: list[tuple[int, str, int]] = []

    def identity(value: os.stat_result) -> tuple[int, ...]:
        return (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

    def entries(descriptor: int) -> set[str]:
        result: set[str] = set()
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                result.add(entry.name)
                if len(result) > 1:
                    raise TaskProtocolConflict("output directory has unexpected entries")
        return result

    try:
        current = os.open("/", directory_flags)
        opened.append(current)
        for component in root.parts[1:]:
            child = os.open(component, directory_flags, dir_fd=current)
            opened.append(child)
            edges.append((current, component, child))
            current = child
        root_fd = current
        root_meta = os.fstat(root_fd)
        if root_meta.st_uid != os.getuid():
            raise TaskProtocolConflict("output root owner drifted")
        root_identity = {
            "path": str(root), "device": root_meta.st_dev, "inode": root_meta.st_ino,
            "uid": root_meta.st_uid, "mode": root_meta.st_mode,
        }
        root_entries = entries(root_fd)
        control = ".agent-task-protocol-v2"
        proof: dict[str, Any] = {
            "schema": "mineru-output-quiescence.v1", "root_identity": root_identity,
            "registry_sha256": None, "record_count": 0,
            "submission_watermark_bucket": None,
        }
        file_count = total_bytes = 0
        pinned: list[tuple[int, tuple[int, ...]]] = [(root_fd, identity(root_meta))]
        if not root_entries:
            if not allow_empty:
                raise TaskProtocolConflict("commissioned task registry is absent")
        else:
            if root_entries != {control}:
                raise TaskProtocolConflict("output root has unknown or retained task entries")
            control_fd = os.open(control, directory_flags, dir_fd=root_fd)
            opened.append(control_fd)
            edges.append((root_fd, control, control_fd))
            control_meta = os.fstat(control_fd)
            if control_meta.st_uid != os.getuid() or entries(control_fd) != {"registry.json"}:
                raise TaskProtocolConflict("task control directory is not quiescent")
            pinned.append((control_fd, identity(control_meta)))
            descriptor = os.open(
                "registry.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=control_fd,
            )
            opened.append(descriptor)
            edges.append((control_fd, "registry.json", descriptor))
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600
                or not 0 < metadata.st_size <= _MAX_REGISTRY_BYTES
            ):
                raise TaskProtocolConflict("task registry file identity is unsafe")
            pinned.append((descriptor, identity(metadata)))
            chunks: list[bytes] = []
            remaining = metadata.st_size + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) != metadata.st_size:
                raise TaskProtocolConflict("task registry changed while reading")
            reader = object.__new__(DurableTaskRegistry)
            reader._output_root = root
            reader._output_root_identity = (
                root_meta.st_dev, root_meta.st_ino, root_meta.st_uid, root_meta.st_mode
            )
            records = reader._decode_registry(raw, require_quiescent=True)
            proof.update(
                registry_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                record_count=len(records),
                submission_watermark_bucket=reader._submission_watermark_bucket,
            )
            file_count, total_bytes = 1, len(raw)
            if entries(control_fd) != {"registry.json"}:
                raise TaskProtocolConflict("task control inventory changed")
        if entries(root_fd) != root_entries:
            raise TaskProtocolConflict("output inventory changed")
        for descriptor, before in pinned:
            if identity(os.fstat(descriptor)) != before:
                raise TaskProtocolConflict("output evidence changed during inspection")
        for parent, name, descriptor in edges:
            linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(descriptor)
            if (linked.st_dev, linked.st_ino, linked.st_mode, linked.st_uid) != (
                held.st_dev, held.st_ino, held.st_mode, held.st_uid
            ):
                raise TaskProtocolConflict("output evidence path was replaced")
        return {"file_count": file_count, "total_bytes": total_bytes, "quiescence": proof}
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


_REGISTRY_MUTATOR_NAMES = (
    "_mark_ingress_cleanup",
    "_finish_ingress_cleanup",
    "_finish_abandon_unbound",
    "acknowledge",
    "_acknowledge_failed_intent",
    "acknowledge_terminal_intent",
    "acquire_result",
    "acquire_inline_result",
    "_commit_task_payload",
    "_commit_ingress_root",
    "_finish_cleanup",
    "complete",
    "fail",
    "lease",
    "reconcile_or_create",
    "_recoverable_payloads_transaction",
    "release_result",
    "reserve_finalizer",
    "reserve_result_for_parse",
    "transition",
    "reserve_source_growth",
    "seal_source",
    "reserve_completion",
    "release_completion",
    "complete_storage",
    "record_storage_wait",
    "block_storage",
)


def _transactional_registry_mutator(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def wrapped(self: DurableTaskRegistry, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            operation = {
                "_finish_cleanup": "cleanup_consumed",
                "_finish_abandon_unbound": "abandon_unbound",
                "_commit_task_payload": "bind_task_payload",
                "_commit_ingress_root": "bind_ingress_root",
                "_mark_ingress_cleanup": "abort_ingress",
                "_finish_ingress_cleanup": "abort_ingress",
                "_acknowledge_failed_intent": "acknowledge_failed",
                "_recoverable_payloads_transaction": "recoverable_payloads",
            }.get(method.__name__, method.__name__)
            self._ensure_mutation_allowed(operation)
            previous_operation = self._active_operation
            starting_generation = self._persistence_generation
            self._active_operation = operation
            try:
                result = method(self, *args, **kwargs)
            except BaseException as exc:
                changed_without_commit = (
                    self._persistence_generation == starting_generation
                    and self._current_state_differs_from_last_durable()
                )
                self._restore_last_durable_state()
                if (
                    changed_without_commit
                    and not isinstance(
                        exc,
                        (TaskProtocolConflict, TaskRegistryPersistenceError),
                    )
                ):
                    # Test/fault probes may replace _persist itself and thereby
                    # bypass phase classification.  Roll back safely and retain
                    # the original injected exception for compatibility.
                    self._record_persistence_event(
                        outcome="not_committed",
                        phase="persist_call",
                        committed=False,
                        operation=operation,
                    )
                raise
            finally:
                self._active_operation = previous_operation
            return copy.deepcopy(result)

    return wrapped


for _registry_mutator_name in _REGISTRY_MUTATOR_NAMES:
    setattr(
        DurableTaskRegistry,
        _registry_mutator_name,
        _transactional_registry_mutator(
            getattr(DurableTaskRegistry, _registry_mutator_name)
        ),
    )


# ------------------------------------------------------------ result storage IO
# Growth is granted before it happens. Every parser output write reaches the
# granted DataWriter with its full payload, so a write that would pass the
# producer's permit is refused before ``open``; the ZIP writer refuses any byte
# past its grant, central directory included. Neither polls the disk.

_SOURCE_GROWTH_PERMIT: contextvars.ContextVar["SourceGrowthPermit | None"] = contextvars.ContextVar(
    "mineru_source_growth_permit", default=None,
)
_STORAGE_MANAGED_OUTPUT = False
# prepare_env creates the parse and image directories before any writer exists.
_PERMIT_DIRECTORY_ALLOWANCE = 8


def require_storage_managed_output() -> None:
    """Latch this process: from now on parser output needs a growth permit."""
    global _STORAGE_MANAGED_OUTPUT
    _STORAGE_MANAGED_OUTPUT = True


def storage_managed_output_required() -> bool:
    return _STORAGE_MANAGED_OUTPUT


def current_source_growth_permit() -> "SourceGrowthPermit | None":
    return _SOURCE_GROWTH_PERMIT.get()


@contextmanager
def bind_source_growth_permit(permit: "SourceGrowthPermit") -> Iterator["SourceGrowthPermit"]:
    if type(permit) is not SourceGrowthPermit:
        raise TaskProtocolConflict("only an exact growth permit can be bound")
    token = _SOURCE_GROWTH_PERMIT.set(permit)
    try:
        yield permit
    finally:
        _SOURCE_GROWTH_PERMIT.reset(token)


_GRANTED_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_GRANTED_LEAF_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
# What the kernel reports when a no-follow open meets a link, a non-directory
# component, or a leaf that is not a regular file (FIFO without a reader).
_GRANTED_IDENTITY_ERRNOS = frozenset({errno.ELOOP, errno.EMLINK, errno.ENOTDIR, errno.EISDIR, errno.ENXIO})


class SourceGrowthPermit:
    """One producer's enforced growth grant for its task tree.

    Charges are physical: each file is rounded up to the allocation unit plus a
    per-file overhead, and a rewrite only charges its growth. Once a write is
    refused the permit stays tripped, so the parser cannot continue around it.
    """

    def __init__(self, *, root: Path, limit_bytes: int, allocation_unit: int, file_overhead: int) -> None:
        _require_storage_amount(limit_bytes, "growth permit")
        _require_storage_amount(allocation_unit, "allocation unit")
        _require_storage_amount(file_overhead, "file overhead")
        if limit_bytes < 1 or allocation_unit < 1 or not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("growth permit bounds or root are invalid")
        self._root = os.path.normpath(str(root))
        root_metadata = os.lstat(self._root)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise ValueError("growth permit root is not a real directory")
        self._root_identity = (root_metadata.st_dev, root_metadata.st_ino)
        self._limit = limit_bytes
        self._unit = allocation_unit
        self._overhead = file_overhead
        self._lock = threading.Lock()
        self._files: dict[str, int] = {}
        self._charged = _PERMIT_DIRECTORY_ALLOWANCE * (allocation_unit + file_overhead)
        self._tripped = False
        # The first identity refusal, kept even if the parser catches it.
        self._integrity_failure: str | None = None
        if self._charged > limit_bytes:
            raise ValueError("growth permit cannot cover its directory allowance")

    @property
    def limit_bytes(self) -> int:
        return self._limit

    @property
    def charged_bytes(self) -> int:
        with self._lock:
            return self._charged

    @property
    def remaining_bytes(self) -> int:
        with self._lock:
            return self._limit - self._charged

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._tripped

    @property
    def integrity_failure(self) -> str | None:
        with self._lock:
            return self._integrity_failure

    def _integrity_locked(self, message: str) -> SourceTreeIntegrityError:
        self._tripped = True
        if self._integrity_failure is None:
            self._integrity_failure = message
        return SourceTreeIntegrityError(message)

    def _trip_integrity(self, message: str) -> None:
        with self._lock:
            error = self._integrity_locked(message)
        raise error

    def physical(self, logical_bytes: int) -> int:
        return -(-logical_bytes // self._unit) * self._unit + self._overhead

    def before_write(self, path: str, size: int) -> None:
        """Charge a whole-file write of ``size`` bytes before it happens.

        Every existing path component below the root must be a real directory
        and an existing leaf a single-link regular file: a write through a
        symlink or into a shared inode would grow or change bytes outside the
        charged task tree.
        """
        if type(size) is not int or size < 0:
            raise ValueError("granted write size is invalid")
        target = os.path.normpath(os.path.abspath(path))
        if os.path.commonpath((self._root, target)) != self._root or target == self._root:
            self._trip_integrity("parser output escaped its task tree")
        with self._lock:
            if self._integrity_failure is not None:
                raise SourceTreeIntegrityError(self._integrity_failure)
            if self._tripped:
                raise SourceGrowthLimitExceeded("growth permit was already exhausted")
            missing_directories = 0
            current = self._root
            components = os.path.relpath(target, self._root).split(os.sep)
            for index, component in enumerate(components[:-1]):
                current = os.path.join(current, component)
                try:
                    metadata = os.lstat(current)
                except FileNotFoundError:
                    missing_directories = len(components) - 1 - index
                    break
                if not stat.S_ISDIR(metadata.st_mode):
                    raise self._integrity_locked("parser output path component is not a real directory")
            previous = self._files.get(target)
            if previous is None:
                try:
                    existing = os.lstat(target) if missing_directories == 0 else None
                except FileNotFoundError:
                    existing = None
                if existing is None:
                    previous = 0
                else:
                    if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                        raise self._integrity_locked("parser output target is not a single-link regular file")
                    previous = self.physical(existing.st_size)
            growth = self.physical(size) - previous + missing_directories * (self._unit + self._overhead)
            if self._charged + max(0, growth) > self._limit:
                self._tripped = True
                raise SourceGrowthLimitExceeded("parser output would exceed its growth permit")
            self._charged += max(0, growth)
            self._files[target] = max(previous, self.physical(size))

    def _open_child_directory(self, parent: int, name: str) -> int:
        for create in (False, True):
            if create:
                try:
                    os.mkdir(name, 0o777, dir_fd=parent)
                except FileExistsError:
                    pass  # Created meanwhile: still opened without following a link.
            try:
                return os.open(name, _GRANTED_DIRECTORY_FLAGS, dir_fd=parent)
            except FileNotFoundError:
                if create:
                    raise
            except OSError as exc:
                if exc.errno in _GRANTED_IDENTITY_ERRNOS:
                    self._trip_integrity("parser output path component is not a real directory")
                raise
        raise AssertionError("unreachable")

    def write_file(self, path: str, data: bytes | bytearray | memoryview) -> None:
        """Charge one whole-file write, then perform it through descriptors.

        The charge is checked on paths, but the bytes only go through the
        pinned root descriptor, one no-follow directory at a time, to a leaf
        opened without following a link and checked on its own descriptor
        before truncation: a link or swap made after the charge cannot move or
        alias the write.
        """
        view = memoryview(data).cast("B")
        target = os.path.normpath(os.path.abspath(path))
        self.before_write(target, view.nbytes)
        components = os.path.relpath(target, self._root).split(os.sep)
        current = os.open(self._root, _GRANTED_DIRECTORY_FLAGS)
        try:
            metadata = os.fstat(current)
            if (metadata.st_dev, metadata.st_ino) != self._root_identity:
                self._trip_integrity("parser output root identity drifted")
            for component in components[:-1]:
                child = self._open_child_directory(current, component)
                previous, current = current, child
                os.close(previous)
            try:
                leaf = os.open(components[-1], _GRANTED_LEAF_FLAGS, 0o666, dir_fd=current)
            except OSError as exc:
                if exc.errno in _GRANTED_IDENTITY_ERRNOS:
                    self._trip_integrity("parser output target is not a single-link regular file")
                raise
        finally:
            os.close(current)
        try:
            metadata = os.fstat(leaf)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                self._trip_integrity("parser output target is not a single-link regular file")
            os.ftruncate(leaf, 0)
            while view:
                view = view[os.write(leaf, view):]
        finally:
            os.close(leaf)


def _permit_hold_reason(permit: SourceGrowthPermit, refused: BaseException | None) -> str:
    """An identity refusal (raised or latched) is integrity; otherwise exhaustion."""
    if isinstance(refused, SourceTreeIntegrityError) or permit.integrity_failure is not None:
        return "tree_integrity"
    return "hard_envelope_exceeded"


class BudgetedSeekableWriter:
    """A seekable file writer that refuses any byte beyond its granted extent.

    ``zipfile`` rewrites each local header in place and appends its central
    directory on close; both go through ``write`` and are checked against the
    high-water extent before any byte reaches the file.
    """

    def __init__(self, descriptor: int, *, grant_bytes: int) -> None:
        if type(descriptor) is not int or descriptor < 0:
            raise ValueError("budgeted writer descriptor is invalid")
        _require_storage_amount(grant_bytes, "writer grant")
        self._fd = descriptor
        self._grant = grant_bytes
        self._position = 0
        self._extent = 0
        # A failed write leaves an unknown descriptor offset: every later
        # write is refused, so no retry or close record can pass the grant.
        # The refusal repeats the first errno, so a full disk stays a full disk.
        self._failed_errno: int | None = None
        self.name = None

    def _refuse_after_failure(self) -> None:
        if self._failed_errno is not None:
            raise OSError(self._failed_errno, "retained ZIP writer failed earlier")

    @property
    def extent(self) -> int:
        return self._extent

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = 0) -> int:
        self._refuse_after_failure()
        if whence == 0:
            target = offset
        elif whence == 1:
            target = self._position + offset
        elif whence == 2:
            target = self._extent + offset
        else:
            raise ValueError("unsupported seek origin")
        if type(target) is not int or target < 0:
            raise ValueError("budgeted writer seek is invalid")
        os.lseek(self._fd, target, os.SEEK_SET)
        self._position = target
        return target

    def write(self, data: Any) -> int:
        self._refuse_after_failure()
        view = memoryview(data).cast("B")
        end = self._position + len(view)
        if max(self._extent, end) > self._grant:
            raise ResultGrantExceeded("retained ZIP would exceed its completion grant")
        written = 0
        try:
            while written < len(view):
                count = os.write(self._fd, view[written:])
                if count <= 0:
                    raise OSError(errno.EIO, "retained ZIP write made no progress")
                written += count
        except BaseException as exc:
            failed_errno = getattr(exc, "errno", None)
            self._failed_errno = failed_errno if type(failed_errno) is int else errno.EIO
            raise
        finally:
            # Bytes that reached the file are accounted even when the write failed.
            self._extent = max(self._extent, self._position + written)
            self._position += written
        return len(view)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        # The descriptor belongs to the caller, which fsyncs and closes it.
        return None

    def truncate(self, size: int | None = None) -> int:
        raise OSError(errno.EPERM, "retained ZIP writer never truncates")


@dataclass(frozen=True, slots=True)
class ResultSelection:
    """One document's selected result files, relative to the task root."""

    pdf_name: str
    parse_dir_parts: tuple[str, ...]
    arc_prefix: str
    named_files: tuple[str, ...]
    image_suffixes: frozenset[str] | None
    origin_prefix: str | None


@dataclass(frozen=True, slots=True)
class InventoryMember:
    arcname: str
    parts: tuple[str, ...]
    size: int
    sha256: str
    identity: tuple[int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class ResultInventory:
    task_id: str
    policy_sha256: str
    members: tuple[InventoryMember, ...]
    directories: tuple[tuple[tuple[str, ...], tuple[int, int]], ...]
    selected_bytes: int
    zip_upper_bound_bytes: int

    def exact_bytes(self) -> bytes:
        # Directory and member inodes are bound too: a replaced parent or leaf
        # with identical bytes is still not the tree this seal attests.
        return json.dumps(
            {
                "schema": RESULT_INVENTORY_SCHEMA,
                "task_id": self.task_id,
                "policy_sha256": self.policy_sha256,
                "selected_bytes": self.selected_bytes,
                "zip_upper_bound_bytes": self.zip_upper_bound_bytes,
                "directories": [
                    {"path": "/".join(parts), "device": identity[0], "inode": identity[1]}
                    for parts, identity in self.directories
                ],
                "members": [
                    {"arcname": item.arcname, "path": "/".join(item.parts),
                     "bytes": item.size, "sha256": item.sha256,
                     "device": item.identity[0], "inode": item.identity[1]}
                    for item in self.members
                ],
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")

    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.exact_bytes()).hexdigest()


def _open_directory_at(parent_fd: int, name: str) -> int:
    if name in {"", ".", ".."} or "/" in name or "\x00" in name:
        raise TaskProtocolConflict("result directory component is unsafe")
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise TaskProtocolConflict("result directory identity is unsafe")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_nlink,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def _regular_member_stat(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        raise TaskProtocolConflict("result source tree contains a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        return None
    if metadata.st_nlink != 1 or metadata.st_uid != os.getuid():
        raise TaskProtocolConflict("result source tree identity is unsafe")
    return metadata


def _selected_candidates(
    task_fd: int, selections: tuple[ResultSelection, ...],
) -> tuple[list[tuple[str, tuple[str, ...]]], list[tuple[tuple[str, ...], tuple[int, int]]]]:
    """List selected members with one directory FD per level, never following links."""
    candidates: list[tuple[str, tuple[str, ...]]] = []
    directories: list[tuple[tuple[str, ...], tuple[int, int]]] = []
    for selection in selections:
        opened: list[int] = []
        try:
            current = task_fd
            walked: tuple[str, ...] = ()
            for component in selection.parse_dir_parts:
                current = _open_directory_at(current, component)
                opened.append(current)
                walked += (component,)
                metadata = os.fstat(current)
                directories.append((walked, (metadata.st_dev, metadata.st_ino)))
            for name in selection.named_files:
                if _regular_member_stat(current, name) is not None:
                    candidates.append((f"{selection.arc_prefix}/{name}", walked + (name,)))
            if selection.image_suffixes is not None:
                try:
                    images = _open_directory_at(current, "images")
                except FileNotFoundError:
                    images = -1
                if images >= 0:
                    opened.append(images)
                    metadata = os.fstat(images)
                    directories.append((walked + ("images",), (metadata.st_dev, metadata.st_ino)))
                    for name in sorted(os.listdir(images)):
                        if Path(name).suffix.lstrip(".").lower() not in selection.image_suffixes:
                            continue
                        if _regular_member_stat(images, name) is not None:
                            candidates.append(
                                (f"{selection.arc_prefix}/images/{name}", walked + ("images", name))
                            )
            if selection.origin_prefix is not None:
                for name in sorted(os.listdir(current)):
                    if name.startswith(selection.origin_prefix) and _regular_member_stat(current, name) is not None:
                        candidates.append((f"{selection.arc_prefix}/{name}", walked + (name,)))
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)
    return candidates, directories


def _open_member(task_fd: int, parts: tuple[str, ...]) -> tuple[int, list[int]]:
    opened: list[int] = []
    try:
        current = task_fd
        for component in parts[:-1]:
            current = _open_directory_at(current, component)
            opened.append(current)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current)
    except BaseException:
        for item in reversed(opened):
            os.close(item)
        raise
    return descriptor, opened


def _hash_member(task_fd: int, parts: tuple[str, ...], sink: Callable[[bytes], Any] | None = None,
                 ) -> tuple[str, int, tuple[int, int, int, int, int, int]]:
    """Stream one member through one FD, proving it did not change meanwhile."""
    descriptor, opened = _open_member(task_fd, parts)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid():
            raise TaskProtocolConflict("result member identity is unsafe")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            total += len(chunk)
            if total > before.st_size:
                raise TaskProtocolConflict("result member grew while it was read")
            if sink is not None:
                sink(chunk)
        after = os.fstat(descriptor)
        if total != before.st_size or _file_identity(after) != _file_identity(before) or after.st_size != total:
            raise TaskProtocolConflict("result member changed while it was read")
        return "sha256:" + digest.hexdigest(), total, _file_identity(before)
    finally:
        os.close(descriptor)
        for item in reversed(opened):
            os.close(item)


def _open_owned_task_root(task_root: Path, root_identity: dict[str, int]) -> int:
    descriptor = os.open(task_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if {
            "device": metadata.st_dev, "inode": metadata.st_ino,
            "uid": metadata.st_uid, "mode": metadata.st_mode,
        } != root_identity:
            raise TaskProtocolConflict("result task root identity drifted")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def build_result_inventory(
    *,
    task_id: str,
    task_root: Path,
    root_identity: dict[str, int],
    selections: tuple[ResultSelection, ...],
    policy: Any,
    zip_upper_bound: Callable[[tuple[tuple[int, int], ...]], int],
) -> ResultInventory:
    """Seal the selected source members by content, one member FD at a time."""
    task_fd = _open_owned_task_root(task_root, root_identity)
    try:
        candidates, directories = _selected_candidates(task_fd, selections)
        if not candidates:
            raise TaskProtocolConflict("retained result has no selected members")
        names = [name for name, _parts in candidates]
        if len(set(names)) != len(names):
            raise TaskProtocolConflict("result ZIP member names are not unique")
        if len(candidates) > policy.max_members or any(
            len(name.encode("utf-8")) > policy.max_name_bytes for name in names
        ):
            raise TaskStorageBlocked("hard_envelope_exceeded")
        members: list[InventoryMember] = []
        for arcname, parts in sorted(candidates):
            digest, size, identity = _hash_member(task_fd, parts)
            members.append(InventoryMember(arcname, parts, size, digest, identity))
        selected = sum(item.size for item in members)
        bound = zip_upper_bound(tuple((item.size, len(item.arcname.encode("utf-8"))) for item in members))
        inventory = ResultInventory(
            task_id=task_id, policy_sha256=policy.sha256, members=tuple(members),
            directories=tuple(directories), selected_bytes=selected, zip_upper_bound_bytes=bound,
        )
        if len(inventory.exact_bytes()) > policy.max_inventory_bytes:
            raise TaskStorageBlocked("hard_envelope_exceeded")
        return inventory
    finally:
        os.close(task_fd)


def verify_result_inventory(
    *, task_root: Path, root_identity: dict[str, int], inventory: ResultInventory,
    selections: tuple[ResultSelection, ...],
) -> None:
    """Prove the selected tree still is its seal: directories, path set, inodes and content."""
    task_fd = _open_owned_task_root(task_root, root_identity)
    try:
        candidates, directories = _selected_candidates(task_fd, selections)
        if tuple(directories) != inventory.directories:
            raise TaskProtocolConflict("sealed result source directories were replaced")
        if sorted(candidates) != [(item.arcname, item.parts) for item in inventory.members]:
            raise TaskProtocolConflict("sealed result source path set changed")
        for item in inventory.members:
            digest, size, identity = _hash_member(task_fd, item.parts)
            if (digest, size, identity[:2]) != (item.sha256, item.size, item.identity[:2]):
                raise TaskProtocolConflict("sealed result source content changed")
    finally:
        os.close(task_fd)


def write_inventory_file(task_root: Path, root_identity: dict[str, int], inventory: ResultInventory) -> str:
    """Durably write the seal inventory beside the tree; return its SHA."""
    raw = inventory.exact_bytes()
    task_fd = _open_owned_task_root(task_root, root_identity)
    try:
        try:
            os.unlink(RETAINED_INVENTORY_NAME, dir_fd=task_fd)
        except FileNotFoundError:
            pass
        descriptor = os.open(
            RETAINED_INVENTORY_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
            dir_fd=task_fd,
        )
        try:
            view = memoryview(raw)
            while view:
                view = view[os.write(descriptor, view):]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(task_fd)
    finally:
        os.close(task_fd)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def load_inventory_file(
    task_root: Path, root_identity: dict[str, int], *, expected_sha256: str, task_id: str,
    policy: Any,
) -> ResultInventory:
    """Reopen a durable seal only if its exact bytes match the registry record."""
    task_fd = _open_owned_task_root(task_root, root_identity)
    try:
        descriptor = os.open(RETAINED_INVENTORY_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=task_fd)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > policy.max_inventory_bytes:
                raise TaskProtocolConflict("sealed inventory identity is unsafe")
            chunks = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(task_fd)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise TaskProtocolConflict("sealed inventory bytes differ from the registry seal")
    value = json.loads(raw.decode("utf-8"))
    if (
        type(value) is not dict
        or set(value) != {
            "schema", "task_id", "policy_sha256", "selected_bytes", "zip_upper_bound_bytes",
            "directories", "members",
        }
        or value["schema"] != RESULT_INVENTORY_SCHEMA or value["task_id"] != task_id
        or value["policy_sha256"] != policy.sha256
    ):
        raise TaskProtocolConflict("sealed inventory identity is invalid")
    members = tuple(
        InventoryMember(
            arcname=item["arcname"], parts=tuple(item["path"].split("/")),
            size=item["bytes"], sha256=item["sha256"],
            identity=(item["device"], item["inode"], 0, 0, 0, 0),
        )
        for item in value["members"]
    )
    directories = tuple(
        (tuple(item["path"].split("/")), (item["device"], item["inode"]))
        for item in value["directories"]
    )
    inventory = ResultInventory(
        task_id=task_id, policy_sha256=policy.sha256, members=members, directories=directories,
        selected_bytes=value["selected_bytes"], zip_upper_bound_bytes=value["zip_upper_bound_bytes"],
    )
    if inventory.exact_bytes() != raw:
        raise TaskProtocolConflict("sealed inventory is not canonical")
    return inventory


def write_retained_zip(
    *, task_root: Path, root_identity: dict[str, int], inventory: ResultInventory,
    selections: tuple[ResultSelection, ...], grant_bytes: int,
) -> tuple[Path, str, int]:
    """Pack the sealed members inside the grant; return the sealed ZIP identity.

    Order, names, timestamps, permissions and DEFLATE parameters are the
    retained-result v1 constants, so a fixed tree yields fixed bytes. Only an
    owned exclusive part is ever written; the final name appears by rename.
    """
    task_fd = _open_owned_task_root(task_root, root_identity)
    part_fd = -1
    primary: BaseException | None = None
    try:
        part_fd = os.open(
            RETAINED_RESULT_PART_NAME, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
            dir_fd=task_fd,
        )
        part_identity = os.fstat(part_fd)
        writer = BudgetedSeekableWriter(part_fd, grant_bytes=grant_bytes)
        with zipfile.ZipFile(writer, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for item in inventory.members:
                info = zipfile.ZipInfo(item.arcname, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                with archive.open(info, "w", force_zip64=True) as member:
                    member_sha256, size, identity = _hash_member(task_fd, item.parts, member.write)
                if (member_sha256, size, identity[:2]) != (item.sha256, item.size, item.identity[:2]):
                    raise TaskProtocolConflict("sealed result member changed while packing")
        os.fsync(part_fd)
        candidates, directories = _selected_candidates(task_fd, selections)
        if (
            sorted(candidates) != [(item.arcname, item.parts) for item in inventory.members]
            or tuple(directories) != inventory.directories
        ):
            raise TaskProtocolConflict("sealed result source path set changed while packing")
        os.lseek(part_fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(part_fd, 1024 * 1024):
            digest.update(chunk)
            total += len(chunk)
        if total != writer.extent or total < 1:
            raise TaskProtocolConflict("retained ZIP extent differs from its written bytes")
        try:
            os.stat(RETAINED_RESULT_NAME, dir_fd=task_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise TaskProtocolConflict("a retained ZIP already exists for this task")
        os.rename(RETAINED_RESULT_PART_NAME, RETAINED_RESULT_NAME, src_dir_fd=task_fd, dst_dir_fd=task_fd)
        sealed = os.stat(RETAINED_RESULT_NAME, dir_fd=task_fd, follow_symlinks=False)
        if (sealed.st_dev, sealed.st_ino) != (part_identity.st_dev, part_identity.st_ino):
            raise TaskProtocolConflict("retained ZIP seal was replaced")
        os.fsync(task_fd)
        return task_root / RETAINED_RESULT_NAME, digest.hexdigest(), total
    except BaseException as exc:
        primary = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        if part_fd >= 0:
            try:
                os.close(part_fd)
            except BaseException as exc:
                cleanup_error = exc
        if primary is not None:
            # Only this attempt's unsealed part is removed; the source seal stays.
            try:
                os.unlink(RETAINED_RESULT_PART_NAME, dir_fd=task_fd)
            except FileNotFoundError:
                pass
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        try:
            os.close(task_fd)
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            if primary is not None:
                primary.add_note("retained ZIP cleanup failed: " + repr(cleanup_error))
            else:
                raise cleanup_error


def live_free_bytes(path: Path) -> int:
    """Free bytes available to this unprivileged process on ``path``'s volume."""
    usage = os.statvfs(path)
    return usage.f_bavail * usage.f_frsize



class SplitTaskExecutor:
    """Separate parse and finalizer credits with explicit state transitions."""

    def __init__(
        self,
        *,
        parse_slots: int,
        finalizer_slots: int,
        result_reservation_bytes: int = 268435456,
        storage_policy: Any = None,
        storage_rescan_seconds: float = 5.0,
    ) -> None:
        if any(type(value) is not int or value < 1 for value in (parse_slots, finalizer_slots)):
            raise ValueError("executor slots must be positive")
        if type(result_reservation_bytes) is not int or result_reservation_bytes < 1:
            raise ValueError("result reservation must be a positive integer")
        if (
            isinstance(storage_rescan_seconds, bool)
            or not isinstance(storage_rescan_seconds, (int, float))
            or not 0 < storage_rescan_seconds <= 60
        ):
            raise ValueError("storage rescan interval is invalid")
        self._parse = asyncio.Semaphore(parse_slots)
        self._finalize = asyncio.Semaphore(finalizer_slots)
        self._parse_slots = parse_slots
        self._finalizer_slots = finalizer_slots
        self._stage_counts = {
            "result_capacity_waiting": 0,
            "parse_waiting": 0,
            "parse_active": 0,
            "finalizer_waiting": 0,
            "finalizer_active": 0,
        }
        self._storage_policy = storage_policy
        if storage_policy is not None:
            # Storage-managed waits hold no parse or finalizer slot; they are
            # counted separately from the legacy B reservation wait.
            self._stage_counts.update(source_growth_waiting=0, completion_waiting=0)
        self._storage_rescan_seconds = float(storage_rescan_seconds)
        # Live (not yet written) promises of granted permits and ZIP writers.
        self._active_permits: set[SourceGrowthPermit] = set()
        self._active_writers: dict[str, int] = {}
        # Sealed sources wait for completion in acceptance order.
        self._completion_queue: list[str] = []
        self._result_reservation_bytes = result_reservation_bytes
        self._capacity_changed = asyncio.Event()
        self._stopping = False
        self._abort_pending = False

    @property
    def result_reservation_bytes(self) -> int:
        return self._result_reservation_bytes

    @property
    def storage_policy(self) -> Any:
        return self._storage_policy

    def outstanding_promise_bytes(self) -> int:
        """Bytes granted to live producers and writers but not yet on disk."""
        return (
            sum(permit.remaining_bytes for permit in tuple(self._active_permits))
            + sum(self._active_writers.values())
        )

    def completion_head_waiting(self) -> bool:
        return bool(self._completion_queue)

    def completion_queue_snapshot(self) -> tuple[str, ...]:
        return tuple(self._completion_queue)

    @property
    def parse_slots(self) -> int:
        return self._parse_slots

    @property
    def finalizer_slots(self) -> int:
        return self._finalizer_slots

    def stage_snapshot(self) -> dict[str, int]:
        """Durably backed stage ownership, read on the executor's serving loop.

        Every counted owner is carried by the registry's published durable view:
        ``parse_waiting`` and ``result_capacity_waiting`` are backed by durable
        pending tasks; ``parse_active`` starts after the processing commit.
        Both finalizer counters are backed by the finalizing commit. A task
        that holds a parse slot while its ``processing`` commit is still in
        flight is counted in neither stage, so the closed health invariant
        ``stage owners <= durable responsibility`` holds at every serving-loop
        step even though ``/health`` reads the last durable view.
        """
        return dict(self._stage_counts)

    @asynccontextmanager
    async def _stage_slot(
        self,
        semaphore: asyncio.Semaphore,
        stage: str,
        *,
        enter: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[None]:
        waiting = stage + "_waiting"
        active = stage + "_active"
        self._stage_counts[waiting] += 1
        try:
            await semaphore.acquire()
        finally:
            self._stage_counts[waiting] -= 1
        if enter is not None:
            try:
                await enter()
            except BaseException:
                semaphore.release()
                raise
        self._stage_counts[active] += 1
        try:
            yield
        finally:
            self._stage_counts[active] -= 1
            semaphore.release()

    def start(self) -> None:
        self._stopping = False
        self._abort_pending = False
        self._capacity_changed.set()

    def notify_result_capacity_changed(self) -> None:
        self._capacity_changed.set()

    def begin_shutdown(self, *, abort_pending: bool = False) -> None:
        self._stopping = True
        self._abort_pending = self._abort_pending or abort_pending
        self._capacity_changed.set()

    async def run(
        self,
        *,
        registry: DurableTaskRegistry,
        key: str,
        parse: Callable[[], Awaitable[None]],
        finalize: Callable[[], Awaitable[tuple[Path, str, int, str]]],
        registry_io: "RegistryServiceIO | None" = None,
    ) -> None:
        """Run accepted work without blocking the serving loop on registry IO.

        The None fallback exists for direct protocol unit tests only. Production
        generated API code must pass the lifespan-owned RegistryServiceIO.
        """
        async def read_record() -> DurableTaskRecord | None:
            if registry_io is None:
                return registry.get(key)
            return await registry.observe(lambda: registry.get(key))

        async def write(function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
            if registry_io is None:
                return function(*args, **kwargs)
            return await registry_io.call(
                function, *args, required=True, lane="metadata", **kwargs
            )

        record = await read_record()
        if record is None or record.state != "pending":
            raise TaskProtocolConflict("only pending work may enter the parser")
        while True:
            if self._abort_pending:
                raise TaskExecutionStopped("result capacity wait stopped with pending responsibility")
            self._capacity_changed.clear()
            try:
                await write(
                    registry.reserve_result_for_parse,
                    key,
                    byte_budget=self._result_reservation_bytes,
                )
            except TaskResultCapacityFull:
                if self._stopping:
                    raise TaskExecutionStopped(
                        "result capacity unavailable during accepted-work drain",
                        capacity_wait=True,
                    ) from None
                self._stage_counts["result_capacity_waiting"] += 1
                try:
                    await self._capacity_changed.wait()
                finally:
                    self._stage_counts["result_capacity_waiting"] -= 1
            else:
                break
        async def enter_parse() -> None:
            # Durable first, then counted: the slot is held but uncounted until the
            # ``processing`` commit has published, so the stage counters never claim
            # an owner the durable view does not yet carry.
            if self._abort_pending:
                raise TaskExecutionStopped("parse slot wait stopped with pending responsibility")
            await write(registry.transition, key, "processing")

        try:
            async with self._stage_slot(self._parse, "parse", enter=enter_parse):
                await parse()
            await write(registry.transition, key, "finalizing")
            async with self._stage_slot(self._finalize, "finalizer"):
                path, digest, byte_count, owner = await finalize()
            await write(
                registry.complete,
                key,
                result_path=path,
                result_sha256=digest,
                result_bytes=byte_count,
                result_owner=owner,
            )
            self.notify_result_capacity_changed()
        except TaskRegistryPersistenceError:
            raise
        except BaseException as exc:
            record = await read_record()
            if record is not None and record.state in {"processing", "finalizing"}:
                failure = json.dumps(
                    {
                        "code": "parse_or_finalize_failed",
                        "detail": type(exc).__name__[:64],
                        "schema": "mineru-task-failure.v1",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                failure_cause = task_failure_cause(exc, task_id=record.task_id)
                try:
                    await write(
                        registry.fail, key, error=failure, failure_cause=failure_cause
                    )
                except BaseException as persistence_error:
                    persistence_error.add_note(
                        "original parse/finalize failure: " + type(exc).__name__[:64]
                    )
                    raise persistence_error from exc
            raise

    async def _storage_wait(self, counter: str) -> None:
        """Wait for a capacity change, or rescan: external free space moves too."""
        self._stage_counts[counter] += 1
        try:
            try:
                await asyncio.wait_for(self._capacity_changed.wait(), self._storage_rescan_seconds)
            except asyncio.TimeoutError:
                pass
        finally:
            self._stage_counts[counter] -= 1

    async def run_storage(
        self,
        *,
        registry: DurableTaskRegistry,
        key: str,
        permit_root: Path,
        parse: Callable[[SourceGrowthPermit], Awaitable[None]],
        seal: Callable[[], Awaitable[ResultInventory]],
        reopen_seal: Callable[[str], Awaitable[ResultInventory]],
        write_seal: Callable[[ResultInventory], Awaitable[str]],
        finalize: Callable[[ResultInventory, int], Awaitable[tuple[Path, str, int, str]]],
        probe_free: Callable[[], Awaitable[int]],
        registry_io: "RegistryServiceIO | None" = None,
    ) -> None:
        """Run one storage-managed task: permit, parse, seal, completion grant, ZIP.

        Capacity waits hold no parse or finalizer slot and never fail the task.
        A source that exceeds its permit, or a ZIP that exceeds its hard grant,
        is held (blocked) with its bytes instead of being failed and deleted.
        """
        policy = self._storage_policy
        if policy is None or registry.storage_policy is not policy:
            raise TaskProtocolConflict("storage execution requires one shared storage policy")

        async def read_record() -> DurableTaskRecord | None:
            if registry_io is None:
                return registry.get(key)
            return await registry.observe(lambda: registry.get(key))

        async def write(function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
            if registry_io is None:
                return function(*args, **kwargs)
            return await registry_io.call(function, *args, required=True, lane="metadata", **kwargs)

        async def note_wait(reason: str) -> None:
            current = await read_record()
            if current is not None and current.storage is not None and current.storage["wait_reason"] != reason:
                await write(registry.record_storage_wait, key, reason=reason)

        record = await read_record()
        if record is None or record.storage is None or record.state not in {"pending", "finalizing"}:
            raise TaskProtocolConflict("only accepted storage-managed work can run")
        if record.storage["wait_reason"] in STORAGE_BLOCKED_REASONS:
            raise TaskStorageBlocked(record.storage["wait_reason"])
        inventory: ResultInventory | None = None
        try:
            if record.state == "pending":
                while True:
                    if self._abort_pending or self._stopping:
                        raise TaskExecutionStopped(
                            "storage capacity wait stopped with pending responsibility", capacity_wait=True,
                        )
                    self._capacity_changed.clear()
                    try:
                        permit_bytes = await write(
                            registry.reserve_source_growth, key,
                            live_free_bytes=await probe_free(),
                            outstanding_promise_bytes=self.outstanding_promise_bytes(),
                            completion_head_waiting=self.completion_head_waiting(),
                        )
                    except TaskStorageWait as waiting:
                        await note_wait(waiting.reason)
                        await self._storage_wait("source_growth_waiting")
                    else:
                        break
                permit = SourceGrowthPermit(
                    root=permit_root, limit_bytes=permit_bytes,
                    allocation_unit=policy.native_allocation_unit_bytes,
                    file_overhead=policy.native_file_overhead_bytes,
                )

                async def enter_parse() -> None:
                    if self._abort_pending:
                        raise TaskExecutionStopped("parse slot wait stopped with pending responsibility")
                    await write(registry.transition, key, "processing")

                self._active_permits.add(permit)
                try:
                    async with self._stage_slot(self._parse, "parse", enter=enter_parse):
                        try:
                            await parse(permit)
                        except SourceGrowthLimitExceeded as refused:
                            reason = _permit_hold_reason(permit, refused)
                            await write(registry.block_storage, key, reason=reason)
                            raise TaskStorageBlocked(reason) from None
                        if permit.tripped:
                            # The parser caught a refusal and returned: its
                            # output is incomplete and is never sealed.
                            reason = _permit_hold_reason(permit, None)
                            await write(registry.block_storage, key, reason=reason)
                            raise TaskStorageBlocked(reason)
                    try:
                        inventory = await seal()
                        # The seal file is output of this producer too: it is
                        # charged to the same permit before it is written.
                        permit.before_write(
                            str(permit_root / RETAINED_INVENTORY_NAME), len(inventory.exact_bytes()),
                        )
                    except SourceGrowthLimitExceeded as refused:
                        reason = _permit_hold_reason(permit, refused)
                        await write(registry.block_storage, key, reason=reason)
                        raise TaskStorageBlocked(reason) from None
                    except TaskStorageBlocked as blocked:
                        await write(registry.block_storage, key, reason=blocked.reason)
                        raise
                    inventory_sha256 = await write_seal(inventory)
                    await write(
                        registry.seal_source, key,
                        inventory_sha256=inventory_sha256,
                        source_bytes=permit.charged_bytes,
                        selected_bytes=inventory.selected_bytes,
                        member_count=len(inventory.members),
                        zip_upper_bound_bytes=inventory.zip_upper_bound_bytes,
                    )
                finally:
                    self._active_permits.discard(permit)
                    self.notify_result_capacity_changed()
            else:
                sealed = record.storage
                if sealed["phase"] != "source_sealed" or sealed["inventory_sha256"] is None:
                    raise TaskProtocolConflict("recovered completion lacks its durable source seal")
                try:
                    inventory = await reopen_seal(sealed["inventory_sha256"])
                except TaskProtocolConflict:
                    await write(registry.block_storage, key, reason="seal_integrity")
                    raise TaskStorageBlocked("seal_integrity") from None
            assert inventory is not None
            grant = min(inventory.zip_upper_bound_bytes, policy.native_result_hard_limit_bytes)
            while True:
                self._completion_queue.append(key)
                try:
                    while True:
                        if self._abort_pending:
                            raise TaskExecutionStopped(
                                "completion wait stopped with a sealed source", capacity_wait=True,
                            )
                        self._capacity_changed.clear()
                        if self._completion_queue[0] == key:
                            try:
                                await write(
                                    registry.reserve_completion, key, grant_bytes=grant,
                                    live_free_bytes=await probe_free(),
                                    outstanding_promise_bytes=self.outstanding_promise_bytes(),
                                )
                            except TaskStorageWait as waiting:
                                await note_wait(waiting.reason)
                            else:
                                break
                        else:
                            await note_wait("completion_capacity")
                        await self._storage_wait("completion_waiting")
                finally:
                    self._completion_queue.remove(key)
                    self.notify_result_capacity_changed()
                # Promise the allocation-rounded extent until the seal commits.
                self._active_writers[key] = policy.physical_charge(grant)
                try:
                    async with self._stage_slot(self._finalize, "finalizer"):
                        try:
                            path, digest, byte_count, owner = await finalize(inventory, grant)
                        except ResultGrantExceeded:
                            reason = (
                                "codec_bound_exceeded" if grant == inventory.zip_upper_bound_bytes
                                else "hard_envelope_exceeded"
                            )
                            await write(registry.release_completion, key, wait_reason="completion_capacity")
                            await write(registry.block_storage, key, reason=reason)
                            raise TaskStorageBlocked(reason) from None
                        except OSError as error:
                            if error.errno not in {errno.ENOSPC, errno.EDQUOT}:
                                raise
                            # External pressure took the space: keep the seal
                            # and wait for completion capacity again.
                            await write(registry.release_completion, key, wait_reason="free_floor")
                            continue
                    await write(
                        registry.complete_storage, key, result_path=path, result_sha256=digest,
                        result_bytes=byte_count, result_owner=owner,
                    )
                    return
                finally:
                    self._active_writers.pop(key, None)
                    self.notify_result_capacity_changed()
        except (TaskRegistryPersistenceError, TaskStorageBlocked, TaskExecutionStopped):
            raise
        except BaseException as exc:
            record = await read_record()
            if record is not None and record.state in {"processing", "finalizing"}:
                failure = json.dumps(
                    {
                        "code": "parse_or_finalize_failed",
                        "detail": type(exc).__name__[:64],
                        "schema": "mineru-task-failure.v1",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                failure_cause = task_failure_cause(exc, task_id=record.task_id)
                try:
                    await write(registry.fail, key, error=failure, failure_cause=failure_cause)
                except BaseException as persistence_error:
                    persistence_error.add_note(
                        "original parse/finalize failure: " + type(exc).__name__[:64]
                    )
                    raise persistence_error from exc
            raise


# The host observer validates the same closed envelope independently.
def validate_mineru_task_admission(
    decoded: object, *, queued_tasks: int, processing_tasks: int, nonterminal_limit: int,
) -> None:
    """Validate durable responsibilities before projecting the legacy load gauges.

    queued_tasks covers ingress and accepted pending, not physical queue depth.
    A cold backlog remains readable on the wire but is not qualified healthy.
    """
    counters = {
        "nonterminal_limit", "ingress_tasks", "accepted_pending_tasks",
        "accepted_processing_tasks", "accepted_finalizing_tasks", "durable_nonterminal_tasks",
        "routeless_accepted_tasks", "ingress_cleanup_tasks", "unowned_ingress_tasks",
        "scheduled_tasks", "queue_depth", "active_processors",
    }
    if (
        not isinstance(decoded, dict)
        or set(decoded) != counters | {
            "schema", "registry_schema", "recovery_overcommitted", "admission_open", "blocked_reason",
        }
        or decoded.get("schema") != "mineru-task-admission.v1"
        or decoded.get("registry_schema") != "mineru-task-registry.v3"
        or any(type(decoded.get(key)) is not int or not 0 <= decoded[key] <= 128 for key in counters)
        or type(decoded.get("recovery_overcommitted")) is not bool
        or type(decoded.get("admission_open")) is not bool
        or decoded["nonterminal_limit"] != nonterminal_limit
        or nonterminal_limit < 1
    ):
        raise ValueError("MinerU admission evidence fields or types drifted")
    ingress = decoded["ingress_tasks"]
    accepted = sum(decoded[key] for key in (
        "accepted_pending_tasks", "accepted_processing_tasks", "accepted_finalizing_tasks",
    ))
    total = ingress + accepted
    if (
        total != decoded["durable_nonterminal_tasks"]
        or queued_tasks != ingress + decoded["accepted_pending_tasks"]
        or processing_tasks != decoded["accepted_processing_tasks"] + decoded["accepted_finalizing_tasks"]
        or decoded["routeless_accepted_tasks"] > accepted
        or max(decoded["ingress_cleanup_tasks"], decoded["unowned_ingress_tasks"]) > ingress
        or decoded["queue_depth"] + decoded["active_processors"] > decoded["scheduled_tasks"]
        or decoded["scheduled_tasks"] > nonterminal_limit
        or decoded["recovery_overcommitted"] != (total > nonterminal_limit)
    ):
        raise ValueError("MinerU admission responsibility counters disagree")
    reason = decoded["blocked_reason"]
    expected_reason = (
        "ingress_recovery_required" if decoded["unowned_ingress_tasks"] or decoded["ingress_cleanup_tasks"]
        else "accepted_recovery_required" if decoded["routeless_accepted_tasks"]
        else "recovery_overcommitted" if total > nonterminal_limit
        else "capacity_full" if total == nonterminal_limit
        else None
    )
    if (
        reason not in ("shutting_down", "worker_unavailable", expected_reason)
        or decoded["admission_open"] != (reason is None)
    ):
        raise ValueError("MinerU admission availability contradicts its responsibilities")


class _Unset:
    """Separate an absent persistence argument from an explicit healthy None."""


_UNSET = _Unset()


def task_protocol_runtime_status(
    registry: DurableTaskRegistry, executor: SplitTaskExecutor,
    *, capacity_config_sha256: str | None = None,
    persistence_event: dict[str, Any] | None | _Unset = _UNSET,
    durability_uncertain: bool = False,
) -> dict[str, Any]:
    """Content-free facts from the serving process's initialized objects."""
    if not isinstance(registry, DurableTaskRegistry) or not isinstance(executor, SplitTaskExecutor):
        raise TaskProtocolConflict("task protocol runtime is not initialized")
    if registry.storage_policy is not None:
        # Storage-managed: no per-task B or aggregate L exists; the bound
        # storage policy and registry v4 are the runtime's result authority.
        if isinstance(persistence_event, _Unset):
            registry.assert_persistence_healthy()
        else:
            DurableTaskRegistry._raise_view_persistence_error(
                persistence_event, durability_uncertain=durability_uncertain
            )
        if (
            type(capacity_config_sha256) is not str
            or len(capacity_config_sha256) != 71
            or not capacity_config_sha256.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in capacity_config_sha256[7:])
        ):
            raise TaskProtocolConflict("storage-managed runtime requires its capacity config SHA")
        if executor.storage_policy is not registry.storage_policy:
            raise TaskProtocolConflict("executor and registry storage policies differ")
        return {
            "schema": "mineru-task-runtime.v4", "enabled": True,
            "task_registry_max_records": _MAX_RECORDS,
            "registry_schema": REGISTRY_SCHEMA_V4,
            "admission_scope": "post_form_owned_upload",
            "capacity_config_sha256": capacity_config_sha256,
            "result_storage_policy_sha256": registry.storage_policy_sha256,
        }
    limits = {
        "task_registry_max_records": _MAX_RECORDS,
        "task_result_reservation_bytes": executor._result_reservation_bytes,
        "max_unacked_result_bytes": registry._limit,
    }
    if any(type(value) is not int or value < 1 for value in limits.values()):
        raise TaskProtocolConflict("task protocol runtime limits are invalid")
    if isinstance(persistence_event, _Unset):
        registry.assert_persistence_healthy()
    else:
        DurableTaskRegistry._raise_view_persistence_error(
            persistence_event, durability_uncertain=durability_uncertain
        )
    result = {
        "schema": "mineru-task-runtime.v2", "enabled": True, **limits,
        "registry_schema": "mineru-task-registry.v3",
        "admission_scope": "post_form_owned_upload",
    }
    if capacity_config_sha256 is not None:
        if (type(capacity_config_sha256) is not str
                or len(capacity_config_sha256) != 71
                or not capacity_config_sha256.startswith("sha256:")
                or any(ch not in "0123456789abcdef" for ch in capacity_config_sha256[7:])):
            raise TaskProtocolConflict("task runtime capacity config SHA is invalid")
        result["schema"] = "mineru-task-runtime.v3"
        result["capacity_config_sha256"] = capacity_config_sha256
    return result


def evict_consumed_routes(
    registry: DurableTaskRegistry,
    tasks: dict[str, Any],
    task_events: dict[str, Any],
) -> int:
    """Remove only routes whose durable terminal was compacted to consumed."""
    evicted = 0
    for task_id in tuple(tasks):
        record = registry.get_by_task_id(task_id)
        if record is not None and record.state == "consumed":
            tasks.pop(task_id, None)
            task_events.pop(task_id, None)
            evicted += 1
    return evicted


__all__ = [
    "TaskAdmissionFull",
    "TaskRegistryObservationBusy",
    "RegistryServiceIO",
    "ServingLoopProbe",
    "TaskRegistryPersistenceError",
    "DurableRegistryView",
    "DurableTaskRecord",
    "DurableTaskRegistry",
    "SplitTaskExecutor",
    "TaskProtocolConflict",
    "admission_counts",
    "evict_consumed_routes",
    "inspect_quiescent_output_root",
    "task_protocol_runtime_status",
]


class RegistryServiceIO:
    """Lifespan-owned bounded bridge
    durable registry remains the sole authority."""

    def __init__(self, *, drain: Callable[..., Awaitable[Any]], max_pending: int) -> None:
        from concurrent.futures import ThreadPoolExecutor
        if not callable(drain) or type(max_pending) is not int or not 1 <= max_pending <= 256:
            raise ValueError("registry IO ownership/bound is invalid")
        self._drain = drain
        self._loop: asyncio.AbstractEventLoop | None = None
        self._accepting = True
        self._closing = False
        self._request_counts = {"mutation": 0, "reader": 0}
        self._requests_accepting = True
        self._requests_idle = asyncio.Event()
        self._requests_idle.set()
        self._max_pending = max_pending
        self._pools = {
            "metadata": ThreadPoolExecutor(max_workers=1, thread_name_prefix="mineru-registry"),
            "bulk": ThreadPoolExecutor(max_workers=2, thread_name_prefix="mineru-owned-files"),
            "observe": ThreadPoolExecutor(max_workers=1, thread_name_prefix="mineru-observe"),
        }
        self._slots = {
            "metadata": asyncio.Semaphore(1),
            "bulk": asyncio.Semaphore(2),
            "observe": asyncio.Semaphore(1),
        }
        self._counts = {"metadata": 0, "bulk": 0, "observe": 0}
        self._required_counts = {"metadata": 0, "bulk": 0, "observe": 0}
        self._idle = asyncio.Event()
        self._idle.set()
        self._close_task: asyncio.Task[None] | None = None

    def _serving_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("registry IO serving loop changed")
        return loop

    async def call(
        self, function: Callable[..., Any], /, *args: Any, lane: str = "metadata",
        required: bool = False, on_cancel_result: Callable[[Any], Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        import contextvars
        from functools import partial
        loop = self._serving_loop()
        if lane not in self._pools:
            raise ValueError("unknown registry IO lane")
        if not self._accepting or (self._closing and not required):
            raise TaskRegistryObservationBusy("registry IO is closing")
        if type(required) is not bool:
            raise ValueError("required ownership flag must be boolean")
        normal_count = self._counts[lane] - self._required_counts[lane]
        if not required and normal_count >= self._max_pending:
            raise TaskRegistryObservationBusy("registry IO is at its bounded admission limit")
        if required and self._required_counts[lane] >= 4 * self._max_pending:
            raise RuntimeError("registry IO continuation ownership bound drifted")
        self._counts[lane] += 1
        self._required_counts[lane] += int(required)
        self._idle.clear()
        acquired = False
        future: asyncio.Future[Any] | None = None
        try:
            await self._slots[lane].acquire()
            acquired = True
            context = contextvars.copy_context()
            future = loop.run_in_executor(
                self._pools[lane], partial(context.run, function, *args, **kwargs)
            )
            try:
                return await self._drain(future)
            except asyncio.CancelledError as cancellation:
                if (on_cancel_result is not None and future.done()
                        and not future.cancelled() and future.exception() is None):
                    compensation = loop.run_in_executor(
                        self._pools[lane], on_cancel_result, future.result()
                    )
                    try:
                        await self._drain(compensation)
                    except BaseException as cleanup_error:
                        cancellation.add_note("owned IO cancellation compensation failed")
                        raise cancellation from cleanup_error
                raise
        finally:
            if acquired:
                self._slots[lane].release()
            self._counts[lane] -= 1
            self._required_counts[lane] -= int(required)
            if not any(self._counts.values()):
                self._idle.set()

    async def wait_idle(self) -> None:
        self._serving_loop()
        await self._idle.wait()

    def open_request(self, kind: str) -> "RegistryRequestResources":
        self._serving_loop()
        if kind not in self._request_counts:
            raise ValueError("unknown request resource category")
        if not self._requests_accepting or self._request_counts[kind] >= self._max_pending:
            raise TaskRegistryObservationBusy("request resources are closing or at capacity")
        self._request_counts[kind] += 1
        self._requests_idle.clear()
        return RegistryRequestResources(self, kind)

    async def quiesce_requests(self) -> None:
        self._serving_loop()
        self._requests_accepting = False
        await self._requests_idle.wait()

    async def _close(self) -> None:
        await self.quiesce_requests()
        await self.wait_idle()
        self._accepting = False
        for pool in self._pools.values():
            await self._drain(asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=False))

    async def close(self) -> None:
        self._serving_loop()
        self._closing = True
        self._requests_accepting = False
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="mineru-registry-io-close")
        await self._drain(self._close_task)


class RegistryRequestResources:
    """Finite transient request ownership; it never changes durable task truth."""

    def __init__(self, owner: RegistryServiceIO, kind: str) -> None:
        self.owner = owner
        self._kind = kind
        self._extra_reader = False
        self._cleanups: list[tuple[Callable[..., Any], tuple[Any, ...], str]] = []
        self._close_task: asyncio.Task[None] | None = None
        # The pre-body storage charge of this request, if its route takes one.
        self.ingress_token: str | None = None

    def reserve_reader(self) -> None:
        self.owner._serving_loop()
        if self._kind == "reader" or self._extra_reader:
            return
        if self.owner._request_counts["reader"] >= self.owner._max_pending:
            raise TaskRegistryObservationBusy("result reader scopes are at capacity")
        self.owner._request_counts["reader"] += 1
        self._extra_reader = True

    def defer(self, function: Callable[..., Any], /, *args: Any, lane: str = "metadata") -> None:
        self.owner._serving_loop()
        if self._close_task is not None or lane not in self.owner._pools:
            raise RuntimeError("resource registration after close or unknown IO lane")
        self._cleanups.append((function, args, lane))

    async def _close(self) -> None:
        primary: BaseException | None = None
        try:
            while self._cleanups:
                function, args, lane = self._cleanups.pop()
                try:
                    await self.owner.call(function, *args, lane=lane, required=True)
                except BaseException as exc:
                    if primary is None:
                        primary = exc
                    else:
                        primary.add_note("additional owned resource cleanup failure: " + repr(exc))
        finally:
            self.owner._request_counts[self._kind] -= 1
            if self._extra_reader:
                self.owner._request_counts["reader"] -= 1
            if not any(self.owner._request_counts.values()):
                self.owner._requests_idle.set()
        if primary is not None:
            raise primary

    async def close(self) -> None:
        self.owner._serving_loop()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="mineru-request-resource-close")
        await self.owner._drain(self._close_task)


_LOOP_TRACE_PREFIX = "MINERU_LOOP_TRACE "
_LOOP_TRACE_SCHEMA = "mineru-loop-trace.v1"
_LOOP_TRACE_TICK_SECONDS = 0.1
_LOOP_TRACE_LAG_NS = 100_000_000
_LOOP_TRACE_GC_PAUSE_NS = 50_000_000
_LOOP_TRACE_SUMMARY_NS = 60_000_000_000
_LOOP_TRACE_RATE_WINDOW_NS = 10_000_000_000
_LOOP_TRACE_RATE_LIMIT = 20
_LOOP_TRACE_OUTPUT_LOCK = RLock()


def is_phase_trace_enabled() -> bool:
    """Return the default-off, closed-vocabulary phase-trace switch."""
    value = os.getenv("MINERU_PHASE_TRACE")
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError("MINERU_PHASE_TRACE has an invalid value")


def _trace_milliseconds(duration_ns: int) -> float:
    return round(max(0, duration_ns) / 1_000_000, 3)


class ServingLoopProbe:
    """Observe serving-loop scheduling lag and collector pauses, owning no work.

    Collector callbacks run on whichever thread triggered the collection, so
    every counter update and rate-limit decision happens under one re-entrant
    state lock.  It stays re-entrant because a collection can begin inside the
    critical section on the same thread.  Lock order is strict: the state lock
    is never held while the output lock is taken, and a thread that is already
    serializing or writing its own line counts a nested observation as dropped
    instead of re-entering the write path.
    """

    def __init__(self) -> None:
        self._state = RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_ident: int | None = None
        self._handle: asyncio.TimerHandle | None = None
        self._gc_callback: Callable[[str, dict[str, int]], None] | None = None
        self._gc_started_ns: dict[int, int] = {}
        self._expected_ns = 0
        self._emitted_ns: deque[int] = deque()
        self._summary_started_ns = 0
        self._max_lag_ns = 0
        self._lag_count = 0
        self._gc_max_pause_ns = 0
        self._gc_count = 0
        self._dropped = 0
        self._stopped = False
        self._emitting: set[int] = set()
        self._pending_failure: str | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach to one running serving loop while the phase trace is enabled."""
        if not is_phase_trace_enabled():
            return
        # Binding the collector hook allocates a tracked object and registering
        # it makes this thread reachable from a collection: both stay outside
        # the critical section so no collection can begin while the state lock
        # is held.
        callback = self._on_collection
        with self._state:
            if self._stopped or self._loop is not None:
                return
            self._loop = loop
            self._loop_thread_ident = get_ident()
            self._summary_started_ns = time.monotonic_ns()
            self._gc_callback = callback
        gc.callbacks.append(callback)
        with self._state:
            stopped = self._stopped
        if stopped:
            # A close that raced the registration must not leave the hook live.
            if callback in gc.callbacks:
                gc.callbacks.remove(callback)
            return
        self._schedule()

    def close(self) -> None:
        """Release the tick and the collector hook; repeated calls are inert."""
        with self._state:
            self._stopped = True
            handle, self._handle = self._handle, None
            loop = self._loop
            on_loop_thread = self._loop_thread_ident == get_ident()
            callback, self._gc_callback = self._gc_callback, None
            self._gc_started_ns.clear()
        if callback is not None and callback in gc.callbacks:
            gc.callbacks.remove(callback)
        if handle is None:
            return
        if on_loop_thread or loop is None:
            handle.cancel()
            return
        try:
            loop.call_soon_threadsafe(handle.cancel)
        except RuntimeError:
            # A closed serving loop can no longer run the cancellation; the
            # stopped probe neither reschedules nor emits from that tick.
            return

    def _schedule(self) -> None:
        with self._state:
            if self._stopped or self._loop is None:
                return
            loop = self._loop
            self._expected_ns = time.monotonic_ns() + int(
                _LOOP_TRACE_TICK_SECONDS * 1_000_000_000
            )
        handle = loop.call_later(_LOOP_TRACE_TICK_SECONDS, self._tick)
        with self._state:
            if not self._stopped:
                self._handle = handle
                return
        handle.cancel()

    def _tick(self) -> None:
        try:
            now_ns = time.monotonic_ns()
            with self._state:
                if self._stopped:
                    return
                lag_ns = max(0, now_ns - self._expected_ns)
                self._max_lag_ns = max(self._max_lag_ns, lag_ns)
                summary_due = now_ns - self._summary_started_ns >= _LOOP_TRACE_SUMMARY_NS
            if lag_ns >= _LOOP_TRACE_LAG_NS:
                self._emit(
                    {"event": "lag", "lag_ms": _trace_milliseconds(lag_ns)},
                    now_ns,
                    droppable=True,
                )
            if summary_due:
                self._emit_summary(now_ns)
            self._schedule()
        except Exception as error:
            self._fail(error, in_collection=False)

    def _on_collection(self, phase: str, info: dict[str, int]) -> None:
        try:
            now_ns = time.monotonic_ns()
            generation = int(info.get("generation", 0))
            with self._state:
                if self._stopped:
                    return
                if phase == "start":
                    self._gc_started_ns[generation] = now_ns
                    return
                if phase != "stop":
                    return
                started_ns = self._gc_started_ns.pop(generation, None)
                if started_ns is None:
                    return
                pause_ns = max(0, now_ns - started_ns)
                self._gc_max_pause_ns = max(self._gc_max_pause_ns, pause_ns)
            if pause_ns >= _LOOP_TRACE_GC_PAUSE_NS:
                self._emit(
                    {
                        "event": "gc",
                        "generation": generation,
                        "pause_ms": _trace_milliseconds(pause_ns),
                        "collected": int(info.get("collected", 0)),
                    },
                    now_ns,
                    droppable=True,
                )
        except Exception as error:
            self._fail(error, in_collection=True)

    def _emit_summary(self, now_ns: int) -> None:
        with self._state:
            # Read and zero the window before allocating anything: an
            # allocation here could start a collection on this thread whose
            # own event would then belong to neither window.
            max_lag_ns = self._max_lag_ns
            lag_count = self._lag_count
            gc_max_pause_ns = self._gc_max_pause_ns
            gc_count = self._gc_count
            dropped = self._dropped
            self._summary_started_ns = now_ns
            self._max_lag_ns = 0
            self._lag_count = 0
            self._gc_max_pause_ns = 0
            self._gc_count = 0
            self._dropped = 0
        # The summary carries the window's drop count, so the rate limit does
        # not apply to it.
        self._emit(
            {
                "event": "summary",
                "window_seconds": _LOOP_TRACE_SUMMARY_NS // 1_000_000_000,
                "max_lag_ms": _trace_milliseconds(max_lag_ns),
                "lag_count": lag_count,
                "gc_max_pause_ms": _trace_milliseconds(gc_max_pause_ns),
                "gc_count": gc_count,
                "dropped": dropped,
            },
            now_ns,
            droppable=False,
        )

    def _emit(self, payload: dict[str, Any], now_ns: int, *, droppable: bool) -> None:
        # Counting an observation and deciding its admission share one critical
        # section, so a summary can never split an event between the two.  The
        # section allocates no collector-tracked object, and the record is built,
        # serialized and written after it, so the output lock is never taken
        # while the state lock is held.  A collection that starts on this thread
        # while it serializes or writes is counted and dropped: writing it here
        # would land inside the line in progress.
        ident = get_ident()
        added = False
        try:
            with self._state:
                if self._stopped:
                    return
                if payload["event"] == "lag":
                    self._lag_count += 1
                elif payload["event"] == "gc":
                    self._gc_count += 1
                while self._emitted_ns and now_ns - self._emitted_ns[0] >= _LOOP_TRACE_RATE_WINDOW_NS:
                    self._emitted_ns.popleft()
                if ident in self._emitting:
                    # A line is already in progress on this thread: count the
                    # nested observation and drop it rather than write inside
                    # that line.  Only collector events can arrive this way.
                    if not droppable:
                        raise RuntimeError("a non-droppable trace line re-entered its own write path")
                    self._dropped += 1
                    return
                if droppable and len(self._emitted_ns) >= _LOOP_TRACE_RATE_LIMIT:
                    self._dropped += 1
                    return
                self._emitted_ns.append(now_ns)
                self._emitting.add(ident)
                added = True
            self._write(self._line({**payload, "schema": _LOOP_TRACE_SCHEMA, "monotonic_ns": now_ns}))
        finally:
            # Only the frame that added the ident may discard it: a nested
            # dropped emission must not disarm the outer frame's guard.
            if added:
                with self._state:
                    self._emitting.discard(ident)
                    pending, self._pending_failure = self._pending_failure, None
                if pending is not None:
                    self._write(pending)

    def _fail(self, error: Exception, *, in_collection: bool) -> None:
        line: str | None = self._line({
            "schema": _LOOP_TRACE_SCHEMA,
            "event": "probe_failed",
            "reason": type(error).__name__,
            "monotonic_ns": time.monotonic_ns(),
        })
        with self._state:
            if self._stopped:
                return
            self._stopped = True
            loop = self._loop
            if get_ident() in self._emitting:
                # The failure happened inside this thread's own write path; the
                # outer frame writes the report after the line in progress.
                self._pending_failure, line = line, None
        if line is not None:
            self._write(line)
        if not in_collection:
            self.close()
            return
        if loop is None:
            # No loop means start() never attached the collector hook.
            return
        try:
            # The collector is iterating its callback list right now; release
            # the hook from the serving loop instead.
            loop.call_soon_threadsafe(self.close)
        except RuntimeError:
            # The serving loop is closed and cannot run the deferred release.
            # The collector re-reads its callback list on every iteration, so
            # removing the hook in place is safe and does not leak the probe.
            self.close()

    @staticmethod
    def _line(payload: dict[str, Any]) -> str:
        return (
            _LOOP_TRACE_PREFIX
            + json.dumps(payload, sort_keys=True, separators=(",", ":"))
            + "\n"
        )

    @staticmethod
    def _write(line: str) -> None:
        with _LOOP_TRACE_OUTPUT_LOCK:
            sys.stderr.write(line)
            sys.stderr.flush()


# An API-process GC lifecycle, not a second task/receipt authority.
_API_GC_POLICY = "api-static-prefix-freeze.v1"
_API_GC_EPOCH: ApiGcEpoch | None = None


class ApiGcEpoch:
    """Seal once before a serving loop/registry exists; never freeze requests.

    Automatic GC and its thresholds remain unchanged. The phase is only a
    process-local cleanup invariant. A failed/partial shutdown must not report
    this epoch as closed, and may leave reclamation to process termination.
    """

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.thread_id = get_ident()
        self.phase = "new"
        self.thresholds = gc.get_threshold()
        self.frozen_count = 0
        self.owns_freeze = False

    def _owner(self) -> None:
        if (os.getpid(), get_ident()) != (self.pid, self.thread_id):
            raise RuntimeError("API GC lifecycle owner changed")

    def _automatic_policy(self) -> None:
        if not gc.isenabled() or gc.get_threshold() != self.thresholds:
            raise RuntimeError("API GC automatic policy changed")

    def prepare(self, initialize: Callable[[], object]) -> None:
        self._owner()
        if self.phase != "new":
            raise RuntimeError("API GC bootstrap is one-shot")
        self._automatic_policy()
        # A nonzero freeze count is not a foreign freeze: CPython 3.12
        # collections move immortal containers (static builtin types' base and
        # MRO tuples) into the permanent generation before any gc.freeze().
        if gc.get_debug() & gc.DEBUG_SAVEALL or gc.garbage:
            raise RuntimeError("API GC debug/uncollectable state is not clean")
        self.phase = "initializing"
        try:
            # No PDF, HTTP client, task manager, registry or serving loop here.
            # initialize returns only after its owned work and temporary scopes
            # have completed; it must not return a request/resource graph.
            if initialize() is not None:
                raise RuntimeError("static initializer must return None")
            self._automatic_policy()
            # These are generation 0/1/2, not three full collections.
            for generation in (0, 1, 2):
                gc.collect(generation)
            if gc.garbage:
                raise RuntimeError("uncollectable objects after static initialization")
            self._automatic_policy()
            # Mark ownership first so a failure at this boundary is unwound.
            self.owns_freeze = True
            gc.freeze()
            self.frozen_count = gc.get_freeze_count()
            self._automatic_policy()
            if self.frozen_count <= 0:
                raise RuntimeError("API GC freeze produced no permanent generation")
            self.phase = "frozen"
        except BaseException as primary:
            self.phase = "failed"
            if self.owns_freeze:
                try:
                    gc.unfreeze()
                    self.owns_freeze = False
                except BaseException as cleanup:
                    primary.add_note("API GC bootstrap unfreeze failed: " + repr(cleanup))
            raise

    def enter_runtime(self) -> None:
        self._owner()
        self._automatic_policy()
        if self.phase != "frozen" or not self.owns_freeze:
            raise RuntimeError("API serving requires a fresh sealed GC epoch")
        self.phase = "runtime"

    def quiesced(self) -> None:
        self._owner()
        if self.phase != "runtime":
            raise RuntimeError("API GC quiescence without an owned runtime")
        self.phase = "quiesced"

    def close(self, clear_static: Callable[[], None]) -> None:
        self._owner()
        if self.phase == "closed":
            return
        if self.phase not in {"frozen", "quiesced"}:
            raise RuntimeError("API GC close refused: runtime not proven quiescent")
        # No live requests, native work, RegistryServiceIO or render executor.
        # Keep original automatic policy; unfreeze also restores eligibility
        # of any cycle crossing the static prefix before its roots are removed.
        try:
            gc.unfreeze()
            self.owns_freeze = False
            clear_static()
            gc.collect(2)
            self._automatic_policy()
        except BaseException:
            self.phase = "failed"
            raise
        self.phase = "closed"


def _api_gc_managed() -> bool:
    return ("MINERU_CAPACITY_CONFIG_PATH" in os.environ
            or "MINERU_CAPACITY_CONFIG_SHA256" in os.environ)


def _initialize_api_static_models() -> None:
    """Constructor prewarm of EXACT Hybrid keys used by 3.4.4, not PDF warmup.

    _predict_layout_for_window does not pass lang; its keys are (None, bool).
    Native forward/render/HTTP warmup remains the existing post-boot canary.
    Objects first created by that canary stay in the collectable generations.
    """
    from mineru.backend.pipeline.model_init import HybridModelSingleton
    from mineru.utils.config_reader import get_device
    import torch

    models = HybridModelSingleton()
    for formula_enabled in (False, True):
        models.get_model(lang=None, formula_enable=formula_enabled)
    device = get_device()
    if str(device).startswith("cuda"):
        torch.cuda.synchronize(device)


def _clear_api_static_models() -> None:
    # Called only before a runtime was started or after its owned cleanup.
    from mineru.backend.pipeline.model_init import (
        AtomModelSingleton, HybridModelSingleton, PIPELINE_MODEL_INIT_LOCK,
    )
    with PIPELINE_MODEL_INIT_LOCK:
        # Drop roots outside the lock; no per-request cache clearing is added.
        retained = (tuple(HybridModelSingleton._models.values()),
                    tuple(AtomModelSingleton._models.values()))
        HybridModelSingleton._models.clear()
        AtomModelSingleton._models.clear()
    del retained


def shutdown_pdf_render_executor_verified() -> None:
    """Final API shutdown of the PDF render pool; every failure stays visible.

    The upstream shutdown recycles best-effort: it drops the pool root, only logs
    terminate/kill/join or executor-shutdown errors and never re-checks the
    workers, so its return proves nothing. This runs the same terminate, grace
    join, kill and join sequence with the upstream timeouts, keeps each error and
    re-checks every worker it took over before returning. ``shutdown(wait=False)``
    also drops the executor's reference to its manager thread, which can still be
    closing queues and joining workers after they exit; that thread is taken over
    first and must finish within the same final join bound. Recycling a failed
    pool while serving keeps the upstream best-effort path.
    """
    from mineru.utils import pdf_image_tools as render

    grace_seconds = render.PDF_RENDER_TERMINATE_GRACE_PERIOD_SECONDS
    kill_join_seconds = render.PDF_RENDER_KILL_JOIN_TIMEOUT_SECONDS
    with render._pdf_render_executor_lock:
        executor, render._pdf_render_executor = render._pdf_render_executor, None
    if executor is None:
        return
    failures: list[BaseException] = []
    workers = list((getattr(executor, "_processes", None) or {}).values())
    manager = getattr(executor, "_executor_manager_thread", None)
    signalled = []
    for worker in workers:
        try:
            if not worker.is_alive():
                continue
        except BaseException as exc:
            failures.append(exc)
        signalled.append(worker)
        try:
            worker.terminate()
        except BaseException as exc:
            failures.append(exc)
    deadline = time.monotonic() + grace_seconds
    for worker in signalled:
        try:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        except BaseException as exc:
            failures.append(exc)
    for worker in signalled:
        try:
            if worker.is_alive():
                worker.kill()
        except BaseException as exc:
            failures.append(exc)
    for worker in signalled:
        try:
            if worker.is_alive():
                worker.join(timeout=kill_join_seconds)
        except BaseException as exc:
            failures.append(exc)
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except BaseException as exc:
        failures.append(exc)
    verdicts: list[str] = []
    if manager is not None:
        # Settled before the workers are re-checked, since its own joins reap them.
        try:
            manager.join(timeout=kill_join_seconds)
            manager_alive = manager.is_alive()
        except BaseException as exc:
            failures.append(exc)
            manager_alive = True
        if manager_alive:
            verdicts.append("PDF render executor manager thread not proven finished")
    unproven = 0
    for worker in workers:
        try:
            if worker.is_alive():
                unproven += 1
        except BaseException as exc:
            failures.append(exc)
            unproven += 1
    if unproven:
        verdicts.insert(0, f"{unproven} PDF render worker(s) not proven terminated")
    if verdicts:
        if failures:
            for verdict in verdicts:
                failures[0].add_note(verdict)
        else:
            failures.append(RuntimeError("; ".join(verdicts)))
    if failures:
        for other in failures[1:]:
            failures[0].add_note("PDF render final shutdown also failed: " + repr(other))
        raise failures[0]


def bootstrap_api_gc(fastapi_app: Any, *, reload: bool) -> None:
    """CLI-only; imports of fast_api and legacy/unmanaged callers are inert."""
    global _API_GC_EPOCH
    if not _api_gc_managed():
        return
    import multiprocessing
    import threading
    if (sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12)
            or not sys.platform.startswith("linux")):
        raise RuntimeError("managed API GC policy requires Linux CPython 3.12")
    if (reload or multiprocessing.current_process().name != "MainProcess"
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("managed API GC requires a non-reloading main process")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("API GC bootstrap must precede the serving event loop")
    if _API_GC_EPOCH is not None or fastapi_app.state.task_manager is not None:
        raise RuntimeError("API GC cannot freeze a previously started runtime")
    if fastapi_app.state.service_config.get("enable_vlm_preload", False):
        raise RuntimeError("managed Hybrid API must not preload a local VLM engine")
    from mineru.cli.agent_capacity_bootstrap import get_process_capacity
    capacity = get_process_capacity()
    if capacity is None or capacity.api_process_limit != 1 or capacity.api_event_loop_limit != 1:
        raise RuntimeError("API GC policy requires one process and one serving loop")
    epoch = ApiGcEpoch()
    _API_GC_EPOCH = epoch
    try:
        epoch.prepare(_initialize_api_static_models)
        print("MINERU_GC_LIFECYCLE " + json.dumps({
            "policy": _API_GC_POLICY, "phase": "frozen", "pid": epoch.pid,
            "freeze_count": epoch.frozen_count, "automatic_gc": gc.isenabled(),
            "thresholds": epoch.thresholds,
        }, sort_keys=True), file=sys.stderr, flush=True)
    except BaseException as primary:
        try:
            if epoch.phase == "frozen":
                epoch.close(_clear_api_static_models)
            else:
                _clear_api_static_models()
        except BaseException as cleanup:
            primary.add_note("API static bootstrap cleanup failed: " + repr(cleanup))
        raise


def enter_api_gc_runtime() -> None:
    if _api_gc_managed():
        if _API_GC_EPOCH is None:
            raise RuntimeError("managed API must start through its GC-bootstrap CLI")
        _API_GC_EPOCH.enter_runtime()


def mark_api_gc_quiesced() -> None:
    if _api_gc_managed():
        if _API_GC_EPOCH is None:
            raise RuntimeError("API GC epoch is missing during shutdown")
        _API_GC_EPOCH.quiesced()


def close_api_gc() -> None:
    if _API_GC_EPOCH is None:
        return
    _API_GC_EPOCH.close(_clear_api_static_models)
    print("MINERU_GC_LIFECYCLE " + json.dumps({
        "policy": _API_GC_POLICY, "phase": "closed", "pid": os.getpid(),
        "automatic_gc": gc.isenabled(),
    }, sort_keys=True), file=sys.stderr, flush=True)
