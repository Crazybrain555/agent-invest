"""Durable worker operational stop: control files, launchd readback, release.

The application latch (``InProcessWorkerStopLatch``) makes a public fault
visible to every worker plane first. This adapter then persists it through two
independent channels, in this order, by the first tripping thread only:

1. bounded ``launchctl disable`` of the supervised resident label plus a
   ``print-disabled`` readback (only over the supervised runtime root on
   macOS, whether launchd or an operator started the process; every other
   root never touches launchd);
2. one create-only active record ``control/worker-circuit-stop.json`` under the
   canonical ``DISCLOSURE_RUNTIME_ROOT`` (0700 directory, 0600 files, complete
   write + fsync, hard-link install that never overwrites, directory fsync).

A failed channel never prevents the other and never erases the cause. The
active record is the start gate; its SHA-256 over raw bytes is its identity
(the record never hashes itself). Only an explicit, hash-bound operator release
archives it and removes it; a release decision alone never permits a start.

Over the supervised runtime root on macOS (the one root the worker label owns,
``DISCLOSURE_WORKER_SUPERVISED_RUNTIME_ROOT``) the gate also reads that label
back, because a public stop whose record failed survives only as the native
disable. Known disabled refuses as ``OPERATOR_DISABLED`` (no cause is
invented), unknown refuses as ``CONTROL_UNAVAILABLE``; only a known-enabled
label is runnable. Every other root (temp, test, scratch, offline, non-macOS)
has no native supervisor and never runs launchctl. The production label is
bound to the production runtime root alone: pairing either with anything else
is unknown and refuses, without running launchctl.

Nothing here reads PostgreSQL, MinerU or a model. Unreadable, untrusted,
symlinked, non-regular, oversized or corrupt control state is a stop, never
"no stop".
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from typing import Literal

from disclosure_anchor.adapters.runtime.exact_file_write import write_new_exact
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.ports.worker_stop_control import (
    PublicStopCause,
    WorkerOperationalStopError,
)
from disclosure_anchor.application.services.worker_stop_latch import InProcessWorkerStopLatch
from disclosure_anchor.settings import PRODUCTION_WORKER_RUNTIME_ROOT, Settings


STOP_RECORD_SCHEMA = "worker-circuit-stop.v1"
RELEASE_RECORD_SCHEMA = "worker-circuit-release.v1"
CONTROL_SCOPE = "worker-operational-control"
CONTROL_STATUS_CONTRACT = "worker-operational-control.v1"
DEFAULT_WORKER_LAUNCHD_LABEL = "com.agentinvest.disclosure-worker"

# Process exit codes owned by the worker control boundary.
EXIT_PUBLIC_STOP = 78
EXIT_BUSY = 75
EXIT_CONTROL_REFUSED = 3

ControlState = Literal[
    "RUNNABLE",
    "OPERATOR_DISABLED",
    "PUBLIC_STOP",
    "INVALID_STOP",
    "CONTROL_UNAVAILABLE",
    "SUPERVISOR_ONLY_STOP",
]
NativeDisableStatus = Literal["verified_disabled", "failed", "unknown"]
RecordOrigin = Literal["automatic_fault", "operator_reconstructed"]
SupervisionScope = Literal[
    "supervised", "not_macos", "unsupervised_runtime_root", "label_root_mismatch",
]
NativeClosure = Literal["closed", "running", "unknown"]

MAX_RECORD_BYTES = 64 * 1024
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_HASHED_INVALID_BYTES = 16 * 1024 * 1024
NATIVE_COMMAND_TIMEOUT_SECONDS = 10.0
CONTROL_LOCK_TIMEOUT_SECONDS = 10.0
# `launchctl print gui/<uid>/<label>` for an unloaded service, observed on
# macOS (Darwin 25.6.0): exit 113 with `Could not find service "<label>" in
# domain for user gui: <uid>` on stderr. Only that exact pair means "not
# loaded"; any other failure is unknown, never absent.
LAUNCHCTL_SERVICE_NOT_FOUND_EXIT = 113

_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_EXIT_CODE_RE = re.compile(r"^(-?[0-9]{1,10})(?::.*)?$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TARGET_RE = re.compile(r"^gui/[0-9]{1,10}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DECISION_TEXT_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")
_FINGERPRINT_KEYS = (
    "process_profile_sha256",
    "runtime_bundle_identity_sha256",
    "worker_profile_sha256",
)
_STOP_KEYS = (
    "cause",
    "fingerprints",
    "native_disable",
    "reconstruction",
    "record_origin",
    "recorded_at",
    "schema",
    "scope",
    "worker",
)
_RECONSTRUCTION_KEYS = (
    "decided_by",
    "evidence_record_origin",
    "evidence_recorded_at",
    "evidence_sha256",
    "evidence_worker",
    "reason",
)


class WorkerControlBusy(RuntimeError):
    """An owner (worker, wrapper, owned child or control lock) is still live."""


class WorkerControlRefused(RuntimeError):
    """The requested control mutation does not match the current evidence."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")


def _require_token(value: object, label: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise ValueError(f"worker stop record {label} is invalid")
    return value


def _require_optional_sha(value: object, label: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not _SHA256_RE.fullmatch(value)):
        raise ValueError(f"worker stop record {label} is invalid")
    return value


def _require_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"worker stop record {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"worker stop record {label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"worker stop record {label} must be timezone-aware")
    return value


def decision_text(value: object, label: str) -> str:
    """Operator decision text: bounded, single-line, no control characters."""

    if not isinstance(value, str) or not _DECISION_TEXT_RE.fullmatch(value) or not value.strip():
        raise ValueError(f"{label} must be 1..512 printable characters on one line")
    return value


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    pid: int
    started_at: str

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid < 1:
            raise ValueError("worker stop record pid is invalid")
        _require_timestamp(self.started_at, "worker start")

    def to_payload(self) -> dict[str, object]:
        return {"pid": self.pid, "started_at": self.started_at}

    @classmethod
    def from_payload(cls, payload: object) -> WorkerIdentity:
        if not isinstance(payload, Mapping) or tuple(sorted(payload)) != ("pid", "started_at"):
            raise ValueError("worker stop record worker shape is not closed")
        return cls(pid=payload["pid"], started_at=payload["started_at"])


@dataclass(frozen=True, slots=True)
class NativeDisableResult:
    status: NativeDisableStatus
    detail: str
    service_target: str | None

    def __post_init__(self) -> None:
        if self.status not in ("verified_disabled", "failed", "unknown"):
            raise ValueError("native disable status is outside the closed vocabulary")
        _require_token(self.detail, "native disable detail")
        if self.service_target is not None and (
            not isinstance(self.service_target, str) or not _TARGET_RE.fullmatch(self.service_target)
        ):
            raise ValueError("native disable service target is invalid")

    def to_payload(self) -> dict[str, object]:
        return {"detail": self.detail, "service_target": self.service_target, "status": self.status}

    @classmethod
    def from_payload(cls, payload: object) -> NativeDisableResult:
        if not isinstance(payload, Mapping) or tuple(sorted(payload)) != (
            "detail", "service_target", "status",
        ):
            raise ValueError("native disable shape is not closed")
        return cls(
            status=payload["status"],
            detail=payload["detail"],
            service_target=payload["service_target"],
        )


@dataclass(frozen=True, slots=True)
class StopRecord:
    """Closed ``worker-circuit-stop.v1`` envelope; parsed strictly."""

    record_origin: RecordOrigin
    recorded_at: str
    cause: PublicStopCause | None
    worker: WorkerIdentity | None
    fingerprints: tuple[tuple[str, str | None], ...]
    native_disable: NativeDisableResult
    reconstruction: tuple[tuple[str, object], ...] | None = None

    def __post_init__(self) -> None:
        if self.record_origin not in ("automatic_fault", "operator_reconstructed"):
            raise ValueError("worker stop record origin is outside the closed vocabulary")
        _require_timestamp(self.recorded_at, "recorded_at")
        if self.record_origin == "automatic_fault":
            if self.cause is None or self.worker is None or self.reconstruction is not None:
                raise ValueError("an automatic stop record needs its cause and worker only")
        elif self.worker is not None or self.reconstruction is None:
            raise ValueError("a reconstructed stop record needs reconstruction evidence")
        if tuple(name for name, _ in self.fingerprints) != _FINGERPRINT_KEYS:
            raise ValueError("worker stop record fingerprints are not closed")
        for name, value in self.fingerprints:
            _require_optional_sha(value, name)
        if self.reconstruction is not None:
            _validate_reconstruction(dict(self.reconstruction))

    def to_payload(self) -> dict[str, object]:
        return {
            "cause": None if self.cause is None else self.cause.to_payload(),
            "fingerprints": dict(self.fingerprints),
            "native_disable": self.native_disable.to_payload(),
            "reconstruction": None if self.reconstruction is None else dict(self.reconstruction),
            "record_origin": self.record_origin,
            "recorded_at": self.recorded_at,
            "schema": STOP_RECORD_SCHEMA,
            "scope": CONTROL_SCOPE,
            "worker": None if self.worker is None else self.worker.to_payload(),
        }

    def encode(self) -> bytes:
        payload = canonical_json_bytes(self.to_payload())
        if len(payload) > MAX_RECORD_BYTES:
            raise ValueError("worker stop record exceeds its bound")
        return payload

    @classmethod
    def decode(cls, raw: bytes) -> StopRecord:
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("worker stop record exceeds its bound")
        payload = strict_json_loads(raw)
        if not isinstance(payload, dict) or tuple(sorted(payload)) != _STOP_KEYS:
            raise ValueError("worker stop record shape is not closed")
        if payload["schema"] != STOP_RECORD_SCHEMA or payload["scope"] != CONTROL_SCOPE:
            raise ValueError("worker stop record schema or scope is unsupported")
        fingerprints = payload["fingerprints"]
        if not isinstance(fingerprints, dict) or tuple(sorted(fingerprints)) != _FINGERPRINT_KEYS:
            raise ValueError("worker stop record fingerprints are not closed")
        reconstruction = payload["reconstruction"]
        if reconstruction is not None and not isinstance(reconstruction, dict):
            raise ValueError("worker stop record reconstruction is invalid")
        return cls(
            record_origin=payload["record_origin"],
            recorded_at=payload["recorded_at"],
            cause=None if payload["cause"] is None else PublicStopCause.from_payload(payload["cause"]),
            worker=None if payload["worker"] is None else WorkerIdentity.from_payload(payload["worker"]),
            fingerprints=tuple((name, fingerprints[name]) for name in _FINGERPRINT_KEYS),
            native_disable=NativeDisableResult.from_payload(payload["native_disable"]),
            reconstruction=None if reconstruction is None else tuple(sorted(reconstruction.items())),
        )


def _validate_reconstruction(payload: Mapping[str, object]) -> None:
    if tuple(sorted(payload)) != _RECONSTRUCTION_KEYS:
        raise ValueError("worker stop reconstruction shape is not closed")
    decision_text(payload["decided_by"], "decided_by")
    decision_text(payload["reason"], "reason")
    if not isinstance(payload["evidence_sha256"], str) or not _SHA256_RE.fullmatch(
        payload["evidence_sha256"]
    ):
        raise ValueError("worker stop reconstruction evidence hash is invalid")
    origin = payload["evidence_record_origin"]
    if origin is not None and origin not in ("automatic_fault", "operator_reconstructed"):
        raise ValueError("worker stop reconstruction evidence origin is invalid")
    if payload["evidence_recorded_at"] is not None:
        _require_timestamp(payload["evidence_recorded_at"], "evidence recorded_at")
    if payload["evidence_worker"] is not None:
        WorkerIdentity.from_payload(payload["evidence_worker"])


@dataclass(frozen=True, slots=True)
class ActiveStopRead:
    """One read of the active record. ``raw`` is kept only for release."""

    status: Literal["absent", "valid", "invalid", "unavailable"]
    problem: str | None = None
    sha256: str | None = None
    byte_count: int | None = None
    record: StopRecord | None = None
    raw: bytes | None = None


@dataclass(frozen=True, slots=True)
class MarkerInstallResult:
    status: Literal["written", "existing", "failed"]
    detail: str
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class NativeSupervisorState:
    """One bounded launchd readback. ``None`` means unknown, never absent."""

    service_target: str | None
    available: bool
    disabled: bool | None = None
    disabled_detail: str = "not_read"
    loaded: bool | None = None
    running: bool | None = None
    pid: int | None = None
    last_exit_code: int | None = None
    print_detail: str = "not_read"

    @property
    def closure(self) -> NativeClosure:
        """Whether the supervised job is proven not running."""

        if self.running is True:
            return "running"
        if self.loaded is False or (self.loaded is True and self.running is False):
            return "closed"
        return "unknown"

    def to_payload(self) -> dict[str, object]:
        return {
            "available": self.available,
            "closure": self.closure,
            "disabled": self.disabled,
            "disabled_detail": self.disabled_detail,
            "last_exit_code": self.last_exit_code,
            "loaded": self.loaded,
            "pid": self.pid,
            "print_detail": self.print_detail,
            "running": self.running,
            "service_target": self.service_target,
        }


@dataclass(frozen=True, slots=True)
class StopPersistenceOutcome:
    native: NativeDisableResult
    marker: MarkerInstallResult
    record_bytes: bytes | None

    @property
    def durable(self) -> bool:
        return self.native.status == "verified_disabled" or self.marker.status in (
            "written",
            "existing",
        )


# --------------------------------------------------------------------------
# Control files
# --------------------------------------------------------------------------


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _errno_token(error: OSError) -> str:
    name = errno.errorcode.get(error.errno or 0, "unknown")
    return name.lower()


class _LinkedNotSynced(OSError):
    """The record was linked into place but its directory fsync failed."""

    def __init__(self, error: OSError) -> None:
        super().__init__(error.errno, str(error))
        self.error = error


class WorkerControlStore:
    """File mechanics for the canonical runtime-root control directory."""

    def __init__(
        self,
        paths: FileStorePathBuilder,
        *,
        runtime_root: Path,
        mount_sentinel: Path | None = None,
        uid: int | None = None,
    ) -> None:
        if not isinstance(runtime_root, Path):
            raise TypeError("worker control store requires the configured runtime root Path")
        if mount_sentinel is not None and not isinstance(mount_sentinel, Path):
            raise TypeError("worker control mount sentinel must be a Path")
        self._paths = paths
        self._runtime_root = runtime_root
        self._mount_sentinel = mount_sentinel
        self._uid = os.getuid() if uid is None else uid

    @classmethod
    def for_settings(cls, settings: Settings) -> WorkerControlStore:
        # The supervised production root reuses the service's existing mount
        # guard (doctor/health `sentinel_path`); other roots have no sentinel.
        return cls(
            FileStorePathBuilder(settings),
            runtime_root=settings.disclosure_runtime_root,
            mount_sentinel=settings.sentinel_path if is_supervised_runtime_root(settings) else None,
        )

    @property
    def active_path(self) -> Path:
        return self._paths.worker_circuit_stop_path()

    def _runtime_root_problem(self) -> str:
        """Empty when the runtime root itself is trusted, else a problem token.

        No create fallback and no symlink following: a missing, substituted,
        foreign-owned or group/world-writable root cannot prove "no stop". With
        a mount sentinel (the supervised root) the root must also share the
        sentinel's device, so a stand-in for an unmounted volume is refused.
        """

        try:
            root = os.lstat(self._runtime_root)
        except FileNotFoundError:
            return "runtime_root_missing"
        except OSError as exc:
            return "runtime_root_" + _errno_token(exc)
        if stat.S_ISLNK(root.st_mode):
            return "runtime_root_symlink"
        if not stat.S_ISDIR(root.st_mode):
            return "runtime_root_not_directory"
        if root.st_uid != self._uid:
            return "runtime_root_foreign_owner"
        if stat.S_IMODE(root.st_mode) & 0o022:
            return "runtime_root_group_or_world_writable"
        if self._mount_sentinel is None:
            return ""
        try:
            sentinel = os.lstat(self._mount_sentinel)
        except FileNotFoundError:
            return "mount_sentinel_missing"
        except OSError as exc:
            return "mount_sentinel_" + _errno_token(exc)
        if not stat.S_ISREG(sentinel.st_mode):
            return "mount_sentinel_not_regular"
        if sentinel.st_dev != root.st_dev:
            return "runtime_root_off_sentinel_volume"
        return ""

    def _control_dir_problem(self, *, must_exist: bool) -> tuple[str, bool]:
        """Return (problem, exists); problem is empty when trusted."""

        root_problem = self._runtime_root_problem()
        if root_problem:
            return root_problem, False
        directory = self._paths.worker_control_dir()
        try:
            info = os.lstat(directory)
        except FileNotFoundError:
            return ("control_dir_missing" if must_exist else ""), False
        except OSError as exc:
            return "control_dir_" + _errno_token(exc), False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return "control_dir_not_directory", True
        if info.st_uid != self._uid:
            return "control_dir_foreign_owner", True
        if stat.S_IMODE(info.st_mode) & 0o077:
            return "control_dir_mode_not_0700", True
        return "", True

    def read_active(self) -> ActiveStopRead:
        problem, exists = self._control_dir_problem(must_exist=False)
        if problem:
            return ActiveStopRead(status="unavailable", problem=problem)
        if not exists:
            return ActiveStopRead(status="absent")
        path = self.active_path
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return ActiveStopRead(status="absent")
        except OSError as exc:
            return ActiveStopRead(status="unavailable", problem="active_" + _errno_token(exc))
        if stat.S_ISLNK(info.st_mode):
            return ActiveStopRead(status="invalid", problem="active_symlink")
        if not stat.S_ISREG(info.st_mode):
            return ActiveStopRead(status="invalid", problem="active_not_regular")
        if info.st_size > MAX_HASHED_INVALID_BYTES:
            return ActiveStopRead(
                status="invalid", problem="active_oversized_unhashed", byte_count=info.st_size,
            )
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            return ActiveStopRead(status="invalid", problem="active_open_" + _errno_token(exc))
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_ino != info.st_ino
                or opened.st_dev != info.st_dev
            ):
                return ActiveStopRead(status="invalid", problem="active_replaced_during_read")
            chunks: list[bytes] = []
            remaining = MAX_HASHED_INVALID_BYTES + 1
            while remaining > 0:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError as exc:
            return ActiveStopRead(status="invalid", problem="active_read_" + _errno_token(exc))
        finally:
            os.close(fd)
        raw = b"".join(chunks)
        if len(raw) > MAX_HASHED_INVALID_BYTES:
            return ActiveStopRead(status="invalid", problem="active_oversized_unhashed",
                                  byte_count=len(raw))
        digest = _sha256(raw)

        def invalid(problem: str) -> ActiveStopRead:
            # Presence is still a stop; the raw bytes stay for a SHA-bound release.
            return ActiveStopRead(status="invalid", problem=problem, sha256=digest,
                                  byte_count=len(raw), raw=raw)

        if opened.st_uid != self._uid:
            return invalid("active_foreign_owner")
        if stat.S_IMODE(opened.st_mode) & 0o077:
            return invalid("active_mode_not_0600")
        if len(raw) > MAX_RECORD_BYTES:
            return invalid("active_oversized")
        try:
            record = StopRecord.decode(raw)
        except (UnicodeDecodeError, ValueError, TypeError, KeyError):
            return invalid("active_corrupt")
        return ActiveStopRead(status="valid", sha256=digest, byte_count=len(raw),
                              record=record, raw=raw)

    def ensure_control_dir(self) -> str:
        """Create the 0700 directory under an existing root; return a problem token."""

        problem, exists = self._control_dir_problem(must_exist=False)
        if problem:
            return problem
        if not exists:
            try:
                os.mkdir(self._paths.worker_control_dir(), 0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                return "control_dir_create_" + _errno_token(exc)
            try:
                os.chmod(self._paths.worker_control_dir(), 0o700, follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass
            try:
                _fsync_directory(self._runtime_root)
            except OSError as exc:
                return "runtime_root_fsync_" + _errno_token(exc)
        problem, _ = self._control_dir_problem(must_exist=True)
        return problem

    def _create_only(self, target: Path, payload: bytes) -> Literal["written", "existing"]:
        """Complete write + fsync, then link into place without overwriting."""

        directory = self._paths.worker_control_dir()
        partial = directory / f".{target.name}.{os.getpid()}.{secrets.token_hex(8)}.partial"
        try:
            write_new_exact(partial, payload)
            os.link(partial, target)
        except FileExistsError:
            return "existing"
        finally:
            try:
                os.unlink(partial)
            except FileNotFoundError:
                pass
        try:
            _fsync_directory(directory)
        except OSError as exc:
            raise _LinkedNotSynced(exc) from exc
        return "written"

    def install_active(self, payload: bytes) -> MarkerInstallResult:
        problem = self.ensure_control_dir()
        if problem:
            return MarkerInstallResult(status="failed", detail=problem)
        try:
            outcome = self._create_only(self.active_path, payload)
        except _LinkedNotSynced as exc:
            # Installed but not proven durable: reported as failed so the
            # operator verifies it; it still stops every later start.
            return MarkerInstallResult(
                status="failed", detail="installed_dir_fsync_" + _errno_token(exc.error),
                sha256=_sha256(payload),
            )
        except OSError as exc:
            return MarkerInstallResult(status="failed", detail="write_" + _errno_token(exc))
        if outcome == "existing":
            # A first cause already recorded by an earlier trip wins.
            existing = self.read_active()
            return MarkerInstallResult(
                status="existing", detail="first_cause_preserved", sha256=existing.sha256,
            )
        return MarkerInstallResult(status="written", detail="installed", sha256=_sha256(payload))

    @contextmanager
    def control_lock(self, timeout_seconds: float = CONTROL_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
        problem = self.ensure_control_dir()
        if problem:
            raise WorkerControlRefused(f"control storage is not trusted: {problem}")
        path = self._paths.worker_circuit_lock_path()
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self._uid:
                raise WorkerControlRefused("control lock is not an owned regular file")
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise WorkerControlBusy("worker control lock is held by another operator") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def read_exact(self, path: Path) -> bytes | None:
        """Read one archive/release file; bounded even if it grows while read."""

        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WorkerControlRefused(
                f"{path.name} cannot be opened safely: {_errno_token(exc)}"
            ) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_HASHED_INVALID_BYTES:
                raise WorkerControlRefused(f"{path.name} is not a bounded regular file")
            chunks: list[bytes] = []
            remaining = MAX_HASHED_INVALID_BYTES + 1
            while remaining > 0:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError as exc:
            raise WorkerControlRefused(f"{path.name} cannot be read: {_errno_token(exc)}") from exc
        finally:
            os.close(fd)
        raw = b"".join(chunks)
        if len(raw) > MAX_HASHED_INVALID_BYTES:
            raise WorkerControlRefused(f"{path.name} grew beyond its bound while being read")
        return raw

    def archive_active(self, stop_sha256: str, raw: bytes) -> Literal["written", "existing"]:
        path = self._paths.worker_circuit_stop_archive_path(stop_sha256)
        existing = self.read_exact(path)
        if existing is not None:
            if _sha256(existing) != stop_sha256:
                raise WorkerControlRefused("an archive with this name holds different bytes")
            return "existing"
        outcome = self._create_only(path, raw)
        if outcome == "existing" and _sha256(self.read_exact(path) or b"") != stop_sha256:
            raise WorkerControlRefused("a concurrent archive holds different bytes")
        return outcome

    def record_release(
        self, stop_sha256: str, payload: bytes, *, decision: Mapping[str, object],
    ) -> tuple[Literal["written", "existing"], str]:
        path = self._paths.worker_circuit_release_path(stop_sha256)
        existing = self.read_exact(path)
        if existing is None:
            outcome = self._create_only(path, payload)
            if outcome == "written":
                return "written", _sha256(payload)
            existing = self.read_exact(path) or b""
        try:
            prior = strict_json_loads(existing)
        except (UnicodeDecodeError, ValueError) as exc:
            raise WorkerControlRefused("the existing release record is corrupt") from exc
        if not isinstance(prior, dict) or any(prior.get(key) != value for key, value in decision.items()):
            raise WorkerControlRefused(
                "a different release decision already exists for this stop"
            )
        return "existing", _sha256(existing)

    def unlink_active(self) -> None:
        os.unlink(self.active_path)
        _fsync_directory(self._paths.worker_control_dir())

    def release_record_exists(self, stop_sha256: str) -> bool:
        # lexists: a planted symlink counts as present and is then refused by read_exact.
        return os.path.lexists(self._paths.worker_circuit_release_path(stop_sha256))

    def archive_exists(self, stop_sha256: str) -> bool:
        return os.path.lexists(self._paths.worker_circuit_stop_archive_path(stop_sha256))


# --------------------------------------------------------------------------
# launchd readback / disable
# --------------------------------------------------------------------------


CommandRunner = Callable[..., "subprocess.CompletedProcess[str]"]


class LaunchdSupervisor:
    """Bounded launchctl calls for one ``gui/<uid>/<label>`` service."""

    def __init__(
        self,
        label: str,
        *,
        uid: int | None = None,
        launchctl: str | None = None,
        runner: CommandRunner = subprocess.run,
        timeout_seconds: float = NATIVE_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(label, str) or not _LABEL_RE.fullmatch(label):
            raise ValueError("launchd label is invalid")
        self._label = label
        self._uid = os.getuid() if uid is None else uid
        self._launchctl = launchctl if launchctl is not None else shutil.which("launchctl")
        self._runner = runner
        self._timeout = timeout_seconds

    @property
    def label(self) -> str:
        return self._label

    @property
    def domain(self) -> str:
        return f"gui/{self._uid}"

    @property
    def service_target(self) -> str:
        return f"{self.domain}/{self._label}"

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str] | None:
        if self._launchctl is None:
            return None
        return self._runner(
            [self._launchctl, *arguments],
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )

    def _disabled(self) -> tuple[bool | None, str]:
        try:
            completed = self._run("print-disabled", self.domain)
        except subprocess.TimeoutExpired:
            return None, "print_disabled_timeout"
        except OSError as exc:
            return None, "print_disabled_" + _errno_token(exc)
        if completed is None:
            return None, "launchctl_unavailable"
        if completed.returncode != 0:
            return None, "print_disabled_failed"
        stdout = completed.stdout or ""
        if not any(line.strip() == "disabled services = {" for line in stdout.splitlines()):
            # Without the override table an unlisted label proves nothing.
            return None, "print_disabled_unrecognized"
        pattern = re.compile(r'^\s*"' + re.escape(self._label) + r'"\s*=>\s*(\S+)\s*$')
        for line in stdout.splitlines():
            match = pattern.match(line)
            if match is None:
                continue
            value = match.group(1)
            if value in ("disabled", "true"):
                return True, "readback"
            if value in ("enabled", "false"):
                return False, "readback"
            return None, "print_disabled_unrecognized"
        # launchd lists only labels with an explicit override; none means enabled.
        return False, "readback_default_enabled"

    def _printed(self) -> tuple[bool | None, bool | None, int | None, int | None, str]:
        """(loaded, running, pid, last exit code, detail) from ``launchctl print``."""

        try:
            completed = self._run("print", self.service_target)
        except subprocess.TimeoutExpired:
            return None, None, None, None, "print_timeout"
        except OSError as exc:
            return None, None, None, None, "print_" + _errno_token(exc)
        if completed is None:
            return None, None, None, None, "launchctl_unavailable"
        if completed.returncode == LAUNCHCTL_SERVICE_NOT_FOUND_EXIT and (
            f'Could not find service "{self._label}"' in (completed.stderr or "")
        ):
            return False, False, None, None, "service_not_found"
        if completed.returncode != 0:
            return None, None, None, None, "print_failed"
        running: bool | None = None
        pid: int | None = None
        last_exit: int | None = None
        for line in (completed.stdout or "").splitlines():
            if line.startswith("\tstate = "):
                state = line.removeprefix("\tstate = ").strip()
                # Any other state (e.g. spawn scheduled) is not proven closed.
                running = True if state == "running" else False if state == "not running" else None
            elif line.startswith("\tpid = "):
                value = line.removeprefix("\tpid = ").strip()
                pid = int(value) if value.isdigit() else None
            elif line.startswith("\tlast exit code = "):
                # launchd renders e.g. `78: EX_CONFIG` or `(never exited)`.
                match = _EXIT_CODE_RE.match(line.removeprefix("\tlast exit code = ").strip())
                last_exit = None if match is None else int(match.group(1))
        return True, running, pid, last_exit, "loaded"

    def readback(self) -> NativeSupervisorState:
        if self._launchctl is None:
            return NativeSupervisorState(
                service_target=self.service_target,
                available=False,
                disabled_detail="launchctl_unavailable",
                print_detail="launchctl_unavailable",
            )
        disabled, disabled_detail = self._disabled()
        loaded, running, pid, last_exit, print_detail = self._printed()
        return NativeSupervisorState(
            service_target=self.service_target,
            available=True,
            disabled=disabled,
            disabled_detail=disabled_detail,
            loaded=loaded,
            running=running,
            pid=pid,
            last_exit_code=last_exit,
            print_detail=print_detail,
        )

    def disable_and_verify(self) -> NativeDisableResult:
        try:
            completed = self._run("disable", self.service_target)
        except subprocess.TimeoutExpired:
            completed = None
            command_detail = "disable_timeout"
        except OSError as exc:
            completed = None
            command_detail = "disable_" + _errno_token(exc)
        else:
            if completed is None:
                return NativeDisableResult("failed", "launchctl_unavailable", self.service_target)
            command_detail = "disable_ok" if completed.returncode == 0 else "disable_nonzero_exit"
        disabled, readback_detail = self._disabled()
        if disabled is True:
            return NativeDisableResult("verified_disabled", command_detail, self.service_target)
        if disabled is False:
            return NativeDisableResult("failed", "readback_enabled", self.service_target)
        return NativeDisableResult("unknown", readback_detail, self.service_target)


def readback_launchd_label(settings: Settings) -> str:
    label = settings.disclosure_worker_launchd_label
    return label if isinstance(label, str) and _LABEL_RE.fullmatch(label) else DEFAULT_WORKER_LAUNCHD_LABEL


def is_supervised_runtime_root(settings: Settings) -> bool:
    """True only for the one runtime root the supervised worker label owns."""

    supervised = getattr(settings, "disclosure_worker_supervised_runtime_root", None)
    runtime_root = settings.disclosure_runtime_root
    return isinstance(supervised, Path) and isinstance(runtime_root, Path) and runtime_root == supervised


@dataclass(frozen=True, slots=True)
class WorkerSupervision:
    """Which launchd job, if any, supervises this runtime root."""

    scope: SupervisionScope
    supervisor: LaunchdSupervisor | None

    def __post_init__(self) -> None:
        if (self.scope == "supervised") != (self.supervisor is not None):
            raise ValueError("a supervised scope needs exactly one launchd supervisor")


def worker_supervision(settings: Settings) -> WorkerSupervision:
    """The one native binding shared by the start gate, status, doctor, release
    and the public-stop disable.

    Off macOS no native supervisor applies. On macOS only the supervised
    runtime root is bound to the worker label (the configured one, else the
    production label); every other root never runs launchctl. The binding is
    configuration, not the process's launchd identity, so a stop disables the
    label whether launchd or an operator started the process. The production
    label and the production runtime root are bound to each other only; any
    other pairing is ``label_root_mismatch``, which never runs launchctl and
    reads as unknown (refused).
    """

    if sys.platform != "darwin":
        return WorkerSupervision("not_macos", None)
    if not is_supervised_runtime_root(settings):
        return WorkerSupervision("unsupervised_runtime_root", None)
    label = readback_launchd_label(settings)
    if (label == DEFAULT_WORKER_LAUNCHD_LABEL) != (
        settings.disclosure_runtime_root == PRODUCTION_WORKER_RUNTIME_ROOT
    ):
        return WorkerSupervision("label_root_mismatch", None)
    return WorkerSupervision("supervised", LaunchdSupervisor(label))


def read_native_supervisor(supervision: WorkerSupervision) -> NativeSupervisorState | None:
    """Bounded readback; ``None`` when no native supervisor applies."""

    supervisor = supervision.supervisor
    if supervisor is None:
        if supervision.scope == "label_root_mismatch":
            # A supervised root whose label cannot be bound: a native stop
            # cannot be ruled out, and launchctl is never asked.
            return NativeSupervisorState(
                service_target=None,
                available=False,
                disabled_detail="label_root_mismatch",
                print_detail="label_root_mismatch",
            )
        return None
    try:
        return supervisor.readback()
    except Exception as exc:  # noqa: BLE001 - an unreadable supervisor is unknown, never enabled
        detail = "readback_" + type(exc).__name__.lower()
        return NativeSupervisorState(
            service_target=supervisor.service_target,
            available=False,
            disabled_detail=detail,
            print_detail=detail,
        )


# --------------------------------------------------------------------------
# The durable runtime control
# --------------------------------------------------------------------------


class RuntimeWorkerStopControl:
    """Process-wide latch plus ordered, bounded, crash-safe persistence."""

    def __init__(
        self,
        *,
        store: WorkerControlStore,
        supervisor: LaunchdSupervisor | None,
        supervisor_problem: str = "supervisor_target_unconfigured",
        clock: Callable[[], datetime] = utc_now,
        pid: int | None = None,
        fingerprints: Mapping[str, str | None] | None = None,
        emit: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._supervisor = supervisor
        self._supervisor_problem = supervisor_problem if _TOKEN_RE.fullmatch(supervisor_problem) else "supervisor_unavailable"
        self._clock = clock
        self._identity = WorkerIdentity(pid=os.getpid() if pid is None else pid, started_at=_iso(clock()))
        self._fingerprint_lock = threading.Lock()
        self._fingerprints: dict[str, str | None] = {name: None for name in _FINGERPRINT_KEYS}
        for name, value in (fingerprints or {}).items():
            self._set_fingerprint(name, value)
        self._emit = emit if emit is not None else _stderr_line
        self._outcome: StopPersistenceOutcome | None = None
        self._outcome_ready = threading.Event()
        self._latch = InProcessWorkerStopLatch(on_first_trip=self._persist)

    @classmethod
    def for_settings(
        cls,
        settings: Settings,
        *,
        supervision: WorkerSupervision | None = None,
    ) -> RuntimeWorkerStopControl:
        # The same binding as the start gate: launchd's XPC_SERVICE_NAME is
        # not the job label for launchd-spawned children on this host, so the
        # process's own launchd identity is never consulted.
        bound = worker_supervision(settings) if supervision is None else supervision
        return cls(
            store=WorkerControlStore.for_settings(settings),
            supervisor=bound.supervisor,
            supervisor_problem=bound.scope,
            fingerprints={
                "runtime_bundle_identity_sha256": settings.disclosure_mineru_runtime_bundle_identity_sha256,
            },
        )

    # -- WorkerStopControlPort -------------------------------------------------

    def trip(self, cause: PublicStopCause) -> bool:
        return self._latch.trip(cause)

    def is_tripped(self) -> bool:
        return self._latch.is_tripped()

    def first_cause(self) -> PublicStopCause | None:
        return self._latch.first_cause()

    def add_wake_callback(self, callback: Callable[[], None]) -> None:
        self._latch.add_wake_callback(callback)

    # -- adapter extras -------------------------------------------------------

    def bind_runtime_identity(self, **fingerprints: str | None) -> None:
        for name, value in fingerprints.items():
            self._set_fingerprint(name, value)

    def _set_fingerprint(self, name: str, value: str | None) -> None:
        if name not in _FINGERPRINT_KEYS:
            raise ValueError(f"unknown worker stop fingerprint: {name}")
        with self._fingerprint_lock:
            self._fingerprints[name] = value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else None

    def persistence_outcome(self, timeout: float | None = None) -> StopPersistenceOutcome | None:
        self._outcome_ready.wait(timeout)
        return self._outcome

    def _say(self, line: str) -> None:
        # Logging is visibility only; a closed or full log never skips a
        # durable channel.
        try:
            self._emit(line)
        except Exception:  # noqa: BLE001 - see above
            pass

    def _persist(self, cause: PublicStopCause) -> None:
        """First tripper only, after the halt: native first, then the record."""

        native = NativeDisableResult("unknown", "not_attempted", None)
        marker = MarkerInstallResult(status="failed", detail="not_attempted")
        record_bytes: bytes | None = None
        try:
            self._say(f"[worker-control] PUBLIC_STOP latched {cause.summary()}")
            try:
                if self._supervisor is None:
                    native = NativeDisableResult("failed", self._supervisor_problem, None)
                else:
                    native = self._supervisor.disable_and_verify()
            except Exception as exc:  # noqa: BLE001 - a broken channel never blocks the next one
                native = NativeDisableResult("unknown", "disable_exception", None)
                self._say(f"[worker-control] native disable raised {type(exc).__name__}: {exc}")
            try:
                with self._fingerprint_lock:
                    fingerprints = tuple((name, self._fingerprints[name]) for name in _FINGERPRINT_KEYS)
                record_bytes = StopRecord(
                    record_origin="automatic_fault",
                    recorded_at=_iso(self._clock()),
                    cause=cause,
                    worker=self._identity,
                    fingerprints=fingerprints,
                    native_disable=native,
                ).encode()
                marker = self._store.install_active(record_bytes)
            except Exception as exc:  # noqa: BLE001 - the latched cause stands regardless
                marker = MarkerInstallResult(status="failed", detail="record_" + type(exc).__name__.lower())
                self._say(f"[worker-control] stop record failed {type(exc).__name__}: {exc}")
        finally:
            self._outcome = StopPersistenceOutcome(native=native, marker=marker, record_bytes=record_bytes)
            self._outcome_ready.set()
        self._say(
            "[worker-control] PUBLIC_STOP persisted "
            f"native={native.status}:{native.detail} marker={marker.status}:{marker.detail}"
            + ("" if marker.sha256 is None else f" active={marker.sha256}")
        )
        if marker.status == "failed":
            channel = "native-only" if native.status == "verified_disabled" else "no durable channel"
            self._say(
                f"[worker-control] STOP_PERSISTENCE_FAILED ({channel}); keep this record as "
                "hash-bound evidence for `worker record-circuit-stop --from-disabled`"
            )
            if record_bytes is not None:
                self._say("[worker-control] STOP_RECORD " + record_bytes.decode("ascii").rstrip("\n"))


def _stderr_line(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Read gate and status
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkerControlSnapshot:
    state: ControlState
    observed_at: str
    active: ActiveStopRead
    native: NativeSupervisorState | None
    supervision: SupervisionScope = "unsupervised_runtime_root"

    @property
    def runnable(self) -> bool:
        return self.state == "RUNNABLE"


def derive_control_state(active: ActiveStopRead, native: NativeSupervisorState | None) -> ControlState:
    """The one decision shared by the start gate, status and doctor.

    ``native`` is ``None`` only when no native supervisor applies. For the
    supervised root the readback is tri-state: known disabled is
    ``OPERATOR_DISABLED`` (never runnable, never a guessed fault), unknown is
    ``CONTROL_UNAVAILABLE``, and only known enabled can be runnable.
    """

    if active.status == "unavailable":
        return "CONTROL_UNAVAILABLE"
    if active.status == "invalid":
        return "INVALID_STOP"
    if active.status == "valid":
        return "PUBLIC_STOP"
    if native is None:
        return "RUNNABLE"
    if native.disabled is True:
        # Native disabled alone has no cause; it is never reported as a fault.
        return "OPERATOR_DISABLED"
    if native.disabled is None or native.loaded is None:
        # Unknown is not enabled: a native-only stop cannot be ruled out.
        return "CONTROL_UNAVAILABLE"
    if (
        native.loaded is True
        and native.running is not True
        and native.last_exit_code == EXIT_PUBLIC_STOP
    ):
        return "SUPERVISOR_ONLY_STOP"
    return "RUNNABLE"


def observe_worker_control(
    settings: Settings,
    *,
    supervision: WorkerSupervision | None = None,
) -> WorkerControlSnapshot:
    """Files plus (supervised root on macOS only) the native readback."""

    bound = worker_supervision(settings) if supervision is None else supervision
    active = WorkerControlStore.for_settings(settings).read_active()
    native = read_native_supervisor(bound)
    return WorkerControlSnapshot(
        state=derive_control_state(active, native),
        observed_at=_iso(utc_now()),
        active=active,
        native=native,
        supervision=bound.scope,
    )


def require_worker_start_permitted(
    settings: Settings,
    *,
    control: object | None = None,
    supervision: WorkerSupervision | None = None,
) -> None:
    """Read-only start gate: in-process latch, active record, then native state.

    The record decides first; only an absent record consults the supervised
    label (tri-state, see ``derive_control_state``). Roots with no native
    supervisor never run launchctl.
    """

    is_tripped = getattr(control, "is_tripped", None)
    if callable(is_tripped) and is_tripped():
        first = getattr(control, "first_cause", lambda: None)()
        detail = "this process already latched a public stop"
        if isinstance(first, PublicStopCause):
            detail += f" ({first.summary()})"
        raise WorkerOperationalStopError(state="PUBLIC_STOP", detail=detail)
    active = WorkerControlStore.for_settings(settings).read_active()
    native: NativeSupervisorState | None = None
    if active.status == "absent":
        native = read_native_supervisor(
            worker_supervision(settings) if supervision is None else supervision
        )
    state = derive_control_state(active, native)
    if state == "RUNNABLE":
        return
    raise WorkerOperationalStopError(
        state=state,
        detail=control_state_detail(state, active, native),
        active_sha256=active.sha256,
    )


def control_state_detail(
    state: ControlState, active: ActiveStopRead, native: NativeSupervisorState | None,
) -> str:
    """One operator line for a non-runnable state; names no absolute path."""

    if active.status == "valid" and active.record is not None:
        record = active.record
        cause = "cause unknown" if record.cause is None else record.cause.summary()
        return (
            f"{record.record_origin} recorded_at={record.recorded_at} {cause} "
            f"native={record.native_disable.status}; release with "
            "`worker release-circuit --expect-sha256 <sha>` after the fix"
        )
    if active.status == "invalid":
        return (
            f"active stop record is invalid ({active.problem}); inspect and "
            "release or repair it by its exact SHA-256, never by deleting it"
        )
    if active.status == "unavailable":
        return f"control storage is unavailable ({active.problem}); a stop cannot be ruled out"
    target = "the supervised launchd job" if native is None or native.service_target is None else (
        native.service_target
    )
    if state == "OPERATOR_DISABLED":
        return (
            f"{target} is natively disabled and no stop record exists; the reason is not "
            "proven by any record (an ordinary maintenance disable, or a public stop whose "
            "record failed: check the worker log for STOP_PERSISTENCE_FAILED). Starting "
            "needs an explicit operator enable, or a reconstructed-then-released stop"
        )
    if state == "SUPERVISOR_ONLY_STOP":
        return (
            f"{target} last exited {EXIT_PUBLIC_STOP} with no stop record and is not "
            "disabled (a public stop whose persistence failed, or the wrapper's "
            "missing-env refusal); read the worker log, then kickstart or boot out the "
            "job explicitly"
        )
    if native is not None and native.disabled_detail == "label_root_mismatch":
        return (
            "the supervised runtime root and the worker launchd label are not bound to "
            "each other (the production label serves only the production runtime root; "
            "any other root names its own DISCLOSURE_WORKER_LAUNCHD_LABEL); launchctl was "
            "not asked, so a native-only stop cannot be ruled out"
        )
    if native is not None:
        return (
            f"launchd state of {target} is unknown (disabled={native.disabled_detail} "
            f"print={native.print_detail}); a native-only stop cannot be ruled out"
        )
    return "worker control state cannot be established"


def control_status_payload(snapshot: WorkerControlSnapshot) -> dict[str, object]:
    active = snapshot.active
    record = active.record
    return {
        "contract_version": CONTROL_STATUS_CONTRACT,
        "observed_at": snapshot.observed_at,
        "state": snapshot.state,
        "runnable": snapshot.runnable,
        "supervision": snapshot.supervision,
        "control_record": {
            "name": "control/worker-circuit-stop.json",
            "status": active.status,
            "problem": active.problem,
            "sha256": active.sha256,
            "byte_count": active.byte_count,
            "record_origin": None if record is None else record.record_origin,
            "recorded_at": None if record is None else record.recorded_at,
            "cause": None if record is None or record.cause is None else record.cause.to_payload(),
            "native_disable": None if record is None else record.native_disable.to_payload(),
        },
        "native_supervisor": None if snapshot.native is None else snapshot.native.to_payload(),
        "guidance": _guidance(snapshot),
    }


def _guidance(snapshot: WorkerControlSnapshot) -> str:
    return {
        "RUNNABLE": "no public stop is recorded",
        "OPERATOR_DISABLED": (
            "the launchd label is disabled and no record proves why: an ordinary "
            "maintenance disable, or a native-only public stop whose record failed (check "
            "the worker log for STOP_PERSISTENCE_FAILED). Every business start refuses "
            "until the operator enables it explicitly or reconstructs and releases the stop"
        ),
        "PUBLIC_STOP": (
            "fix the cause, then `make worker-release-circuit SHA=<sha> ...`; release never "
            "enables or starts the job"
        ),
        "INVALID_STOP": (
            "the active record is invalid; keep it, inspect it, and release it by its exact "
            "SHA-256 (symlink/non-regular/unhashed records need trusted-storage repair first)"
        ),
        "CONTROL_UNAVAILABLE": (
            "the runtime root, control directory or supervised launchd state is "
            "unavailable, unknown or untrusted; repair the mount/permissions or launchd "
            "readback, a stop cannot be ruled out"
        ),
        "SUPERVISOR_ONLY_STOP": (
            "the loaded job last exited 78 without a stop record: either a public stop whose "
            "persistence failed or the wrapper's missing-env refusal; read the worker log"
        ),
    }[snapshot.state]


def render_control_status_terminal(snapshot: WorkerControlSnapshot) -> str:
    active = snapshot.active
    lines = [f"[worker-control] state={snapshot.state} ({snapshot.observed_at})"]
    record = active.record
    if active.status == "absent":
        lines.append("  record: none")
    else:
        lines.append(
            f"  record: {active.status}"
            + ("" if active.problem is None else f" problem={active.problem}")
            + ("" if active.sha256 is None else f" sha256={active.sha256}")
        )
    if record is not None:
        cause = "unknown" if record.cause is None else record.cause.summary()
        lines.append(f"  origin={record.record_origin} recorded_at={record.recorded_at} {cause}")
        lines.append(
            f"  native_disable={record.native_disable.status}:{record.native_disable.detail}"
        )
    native = snapshot.native
    if native is None:
        lines.append(f"  launchd: no native supervisor applies ({snapshot.supervision})")
    else:
        lines.append(
            f"  launchd {native.service_target or 'unbound'}: available={native.available} "
            f"disabled={native.disabled} ({native.disabled_detail}) loaded={native.loaded} "
            f"running={native.running} pid={native.pid} last_exit={native.last_exit_code} "
            f"({native.print_detail})"
        )
    if not snapshot.runnable and snapshot.active.status == "absent":
        lines.append("  " + control_state_detail(snapshot.state, snapshot.active, native))
    lines.append("  " + _guidance(snapshot))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Release and reconstruction (callers hold the WORKER_NS singleton)
# --------------------------------------------------------------------------


ProcessLister = Callable[[], Sequence[tuple[int, str]]]


def list_processes() -> tuple[tuple[int, str], ...]:
    completed = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if completed.returncode != 0:
        raise WorkerControlBusy("process table unavailable; worker closure cannot be proven")
    rows: list[tuple[int, str]] = []
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[1]))
    return tuple(rows)


def owned_worker_processes(
    processes: Sequence[tuple[int, str]], *, own_pid: int | None = None,
) -> tuple[tuple[int, str], ...]:
    """Old wrapper/Python and owned MinerU/semantic children still alive."""

    me = os.getpid() if own_pid is None else own_pid
    found: list[tuple[int, str]] = []
    for pid, command in processes:
        if pid == me:
            continue
        padded = f" {command} "
        kind: str | None = None
        if " -m disclosure_anchor.cli.worker loop" in padded or " -m disclosure_anchor.cli.worker once" in padded:
            kind = "worker"
        elif "run_worker_once.sh" in padded:
            kind = "wrapper"
        elif "/bin/mineru -p " in padded or " -m mineru.cli.fast_api " in padded:
            kind = "mineru"
        elif "--output-last-message" in padded and "semantic-route-" in padded:
            kind = "semantic_codex"
        elif all(flag in padded for flag in ("--json-schema", "--no-session-persistence", "--safe-mode")):
            kind = "semantic_claude"
        if kind is not None:
            found.append((pid, kind))
    return tuple(found)


@dataclass(frozen=True, slots=True)
class WorkerClosure:
    """Evidence that the old owner and its known children have ended.

    The proofs are the WORKER_NS singleton (taken by the actual release), the
    exact launchd readback of the supervised job, and a scan for known owned
    children, which do not hold the singleton. The scan matches known command
    shapes only, so it can add blockers but never proves an unknown alias
    absent. Anything unknown is a blocker, never "closed".
    """

    supervision: SupervisionScope
    native: NativeSupervisorState | None
    process_closure: Literal["no_known_owner", "owners_alive", "unknown"]
    blockers: tuple[str, ...]

    @property
    def native_closure(self) -> Literal["not_applicable", "closed", "running", "unknown"]:
        return "not_applicable" if self.native is None else self.native.closure


def observe_worker_closure(
    supervision: WorkerSupervision, process_lister: ProcessLister,
) -> WorkerClosure:
    blockers: list[str] = []
    native = read_native_supervisor(supervision)
    if native is not None and native.closure == "running":
        blockers.append(f"launchd job {native.service_target} is still running (pid {native.pid})")
    elif native is not None and native.closure == "unknown":
        blockers.append(
            f"launchd job {native.service_target or 'unbound'} closure is unknown "
            f"(print={native.print_detail}); closure cannot be proven"
        )
    process_closure: Literal["no_known_owner", "owners_alive", "unknown"]
    try:
        owners = owned_worker_processes(process_lister())
    except Exception as exc:  # noqa: BLE001 - an unreadable process table is not closure
        process_closure = "unknown"
        blockers.append(
            f"process table unavailable ({type(exc).__name__}); owned-child closure cannot be proven"
        )
    else:
        process_closure = "owners_alive" if owners else "no_known_owner"
        blockers.extend(f"{kind} process {pid} is still alive" for pid, kind in owners)
    return WorkerClosure(
        supervision=supervision.scope,
        native=native,
        process_closure=process_closure,
        blockers=tuple(blockers),
    )


def _require_sha(value: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise WorkerControlRefused("--expect-sha256 must be sha256:<64 lowercase hex>")
    return value


def plan_release(
    settings: Settings,
    *,
    expect_sha256: str,
    supervision: WorkerSupervision | None = None,
    process_lister: ProcessLister = list_processes,
) -> dict[str, object]:
    """Truly read-only dry run: no lock file, DB claim, archive, record or enable.

    Unknown closure is reported as unknown (and blocks ``would_release``); the
    singleton owner is not probed, because probing it would take the lock.
    """

    expected = _require_sha(expect_sha256)
    store = WorkerControlStore.for_settings(settings)
    active = store.read_active()
    closure = observe_worker_closure(
        worker_supervision(settings) if supervision is None else supervision, process_lister,
    )
    problems: list[str] = []
    if active.status == "absent":
        problems.append("no active stop record")
    elif active.status == "unavailable":
        problems.append(f"control storage unavailable ({active.problem})")
    elif active.raw is None:
        problems.append(f"active record is not releasable ({active.problem}); repair storage first")
    elif active.sha256 != expected:
        problems.append(f"active record is {active.sha256}, not the expected hash")
    already = active.status == "absent" and store.release_record_exists(expected)
    return {
        "contract_version": "worker-circuit-release-plan.v1",
        "dry_run": True,
        "expected_sha256": expected,
        "active_status": active.status,
        "active_sha256": active.sha256,
        "active_problem": active.problem,
        "archive_exists": store.archive_exists(expected),
        "release_record_exists": store.release_record_exists(expected),
        "already_released": already,
        "singleton_owner": "unknown",
        "supervision": closure.supervision,
        "native_supervisor": None if closure.native is None else closure.native.to_payload(),
        "native_closure": closure.native_closure,
        "process_closure": closure.process_closure,
        "closure_blockers": list(closure.blockers),
        "problems": problems,
        "would_release": not problems and not closure.blockers,
    }


def release_worker_circuit(
    settings: Settings,
    *,
    expect_sha256: str,
    decided_by: str,
    reason: str,
    fixed_by: str,
    supervision: WorkerSupervision | None = None,
    process_lister: ProcessLister = list_processes,
    clock: Callable[[], datetime] = utc_now,
) -> dict[str, object]:
    """Archive, record, recheck, then remove the exact active stop.

    The caller holds the worker singleton. Ordering is fixed: singleton, then
    the short control lock. A running, unknown or unprovable old owner/child
    closure refuses (``WorkerControlBusy``) and leaves the stop active, as does
    any other failure. A crash is idempotent only for the exact same stop
    identity and decision.
    """

    expected = _require_sha(expect_sha256)
    decision = {
        "decided_by": decision_text(decided_by, "--decided-by"),
        "fixed_by": decision_text(fixed_by, "--fixed-by"),
        "reason": decision_text(reason, "--reason"),
        "released_stop_sha256": expected,
        "schema": RELEASE_RECORD_SCHEMA,
        "scope": CONTROL_SCOPE,
    }
    closure = observe_worker_closure(
        worker_supervision(settings) if supervision is None else supervision, process_lister,
    )
    if closure.blockers:
        raise WorkerControlBusy("; ".join(closure.blockers))
    store = WorkerControlStore.for_settings(settings)
    with store.control_lock():
        active = store.read_active()
        if active.status == "absent":
            if store.release_record_exists(expected):
                # Crash after removal: accept only the exact same decision.
                _, release_sha = store.record_release(
                    expected, canonical_json_bytes({**decision, "decided_at": _iso(clock())}),
                    decision=decision,
                )
                return _release_receipt(expected, release_sha, idempotent=True)
            raise WorkerControlRefused("no active stop record to release")
        if active.status == "unavailable":
            raise WorkerControlRefused(f"control storage unavailable ({active.problem})")
        if active.raw is None or active.sha256 is None:
            raise WorkerControlRefused(
                f"active record is not releasable ({active.problem}); repair trusted storage first"
            )
        if active.sha256 != expected:
            raise WorkerControlRefused(
                f"active record is {active.sha256}; a stale or different hash cannot release it"
            )
        store.archive_active(expected, active.raw)
        _, release_sha = store.record_release(
            expected, canonical_json_bytes({**decision, "decided_at": _iso(clock())}),
            decision=decision,
        )
        recheck = store.read_active()
        if recheck.sha256 != expected or recheck.raw != active.raw:
            raise WorkerControlRefused("the active record changed during release; nothing was removed")
        store.unlink_active()
    return _release_receipt(expected, release_sha, idempotent=False)


def _release_receipt(stop_sha256: str, release_sha256: str, *, idempotent: bool) -> dict[str, object]:
    digest = stop_sha256.removeprefix("sha256:")
    return {
        "contract_version": "worker-circuit-release-receipt.v1",
        "released": True,
        "idempotent_replay": idempotent,
        "released_stop_sha256": stop_sha256,
        "archive": f"control/worker-circuit-stop.{digest}.json",
        "release_record": f"control/worker-circuit-release.{digest}.json",
        "release_record_sha256": release_sha256,
        "note": (
            "release does not enable or start the job; enable/kickstart or bootstrap "
            "explicitly per the production runbook"
        ),
    }


def read_evidence(path: Path, expected_sha256: str) -> bytes:
    expected = _require_sha(expected_sha256)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise WorkerControlRefused(f"evidence cannot be opened: {_errno_token(exc)}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_EVIDENCE_BYTES:
            raise WorkerControlRefused("evidence must be a regular file of at most 64 KiB")
        raw = os.read(fd, MAX_EVIDENCE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_EVIDENCE_BYTES or _sha256(raw) != expected:
        raise WorkerControlRefused("evidence bytes do not match --evidence-sha256")
    return raw


def reconstruct_worker_circuit_stop(
    settings: Settings,
    *,
    evidence: bytes,
    evidence_sha256: str,
    decided_by: str,
    reason: str,
    supervisor: LaunchdSupervisor,
    dry_run: bool = False,
    clock: Callable[[], datetime] = utc_now,
) -> dict[str, object]:
    """Rebuild an active record for a native-only stop; never an automatic record."""

    expected = _require_sha(evidence_sha256)
    if _sha256(evidence) != expected:
        raise WorkerControlRefused("evidence bytes do not match --evidence-sha256")
    native = supervisor.readback()
    if native.disabled is not True:
        raise WorkerControlRefused(
            f"{supervisor.service_target} is not natively disabled; there is no native stop to record"
        )
    cause: PublicStopCause | None = None
    evidence_origin: str | None = None
    evidence_recorded_at: str | None = None
    evidence_worker: dict[str, object] | None = None
    try:
        original = StopRecord.decode(evidence)
    except (UnicodeDecodeError, ValueError, TypeError, KeyError):
        original = None
    if original is not None:
        cause = original.cause
        evidence_origin = original.record_origin
        evidence_recorded_at = original.recorded_at
        evidence_worker = None if original.worker is None else original.worker.to_payload()
    record = StopRecord(
        record_origin="operator_reconstructed",
        recorded_at=_iso(clock()),
        cause=cause,
        worker=None,
        fingerprints=tuple((name, None) for name in _FINGERPRINT_KEYS)
        if original is None else original.fingerprints,
        native_disable=NativeDisableResult("verified_disabled", "operator_readback", supervisor.service_target),
        reconstruction=tuple(sorted({
            "decided_by": decision_text(decided_by, "--decided-by"),
            "evidence_record_origin": evidence_origin,
            "evidence_recorded_at": evidence_recorded_at,
            "evidence_sha256": expected,
            "evidence_worker": evidence_worker,
            "reason": decision_text(reason, "--reason"),
        }.items())),
    )
    payload = record.encode()
    store = WorkerControlStore.for_settings(settings)
    receipt: dict[str, object] = {
        "contract_version": "worker-circuit-reconstruction-receipt.v1",
        "dry_run": dry_run,
        "service_target": supervisor.service_target,
        "evidence_sha256": expected,
        "evidence_parsed_as_stop_record": original is not None,
        "record_sha256": _sha256(payload),
    }
    if dry_run:
        active = store.read_active()
        receipt["active_status"] = active.status
        receipt["would_record"] = active.status == "absent"
        return receipt
    with store.control_lock():
        active = store.read_active()
        if active.status != "absent":
            raise WorkerControlRefused(
                f"an active stop record already exists ({active.status}); nothing to reconstruct"
            )
        installed = store.install_active(payload)
        if installed.status != "written":
            raise WorkerControlRefused(f"reconstructed record was not installed ({installed.detail})")
    receipt["recorded"] = True
    receipt["next_step"] = "release it with `worker release-circuit --expect-sha256 <record_sha256>`"
    return receipt


__all__ = [
    "CONTROL_STATUS_CONTRACT",
    "DEFAULT_WORKER_LAUNCHD_LABEL",
    "EXIT_BUSY",
    "EXIT_CONTROL_REFUSED",
    "EXIT_PUBLIC_STOP",
    "LAUNCHCTL_SERVICE_NOT_FOUND_EXIT",
    "ActiveStopRead",
    "LaunchdSupervisor",
    "MarkerInstallResult",
    "NativeDisableResult",
    "NativeSupervisorState",
    "RuntimeWorkerStopControl",
    "StopPersistenceOutcome",
    "StopRecord",
    "WorkerClosure",
    "WorkerControlBusy",
    "WorkerControlRefused",
    "WorkerControlSnapshot",
    "WorkerControlStore",
    "WorkerIdentity",
    "WorkerSupervision",
    "control_state_detail",
    "control_status_payload",
    "derive_control_state",
    "is_supervised_runtime_root",
    "list_processes",
    "observe_worker_closure",
    "observe_worker_control",
    "owned_worker_processes",
    "plan_release",
    "read_evidence",
    "read_native_supervisor",
    "readback_launchd_label",
    "reconstruct_worker_circuit_stop",
    "release_worker_circuit",
    "render_control_status_terminal",
    "require_worker_start_permitted",
    "worker_supervision",
]
