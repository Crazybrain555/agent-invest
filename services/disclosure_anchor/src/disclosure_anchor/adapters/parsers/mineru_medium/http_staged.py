"""Closed MinerU 3.4.4 task HTTP adapter.

Remote completion and local ZIP materialization are deliberately separate.
The opaque resume token is private checkpoint data; callers must not log it.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import fcntl
from functools import wraps
import hashlib
import json
import os
import secrets
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Event, Lock
from typing import Any, Callable, Iterator, TypeVar, cast
from urllib.parse import urljoin, urlsplit

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import (
    MinerUMediumArtifactReader,
)
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.contracts.remote_parse_checkpoint import (
    AcceptedSubmissionReceipt,
    EncodedCheckpointReceipt,
    FailureReceipt,
    TerminalReceipt,
    encode_terminal_receipt,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.ports.provider_parser import ProviderParserResult
from disclosure_anchor.application.ports.staged_provider_parser import (
    DurableCheckpointWitness,
    PersistedSubmissionReceipt,
    PreparedLocalSubmission,
    PreparedMaterialization,
    PreparedSubmissionIdentity,
    PrivateSubmittedTaskResume,
    RecoveredV3ResumeSecret,
    ProviderMaterializationEvidence,
    ProviderAckCompletionWitness,
    RemoteArtifactReceipt,
    RemoteProviderParseHandle,
    StagedProviderParserResult,
    SubmissionAcceptanceAmbiguous,
    _issue_provider_ack_completion_witness,
)
from disclosure_anchor.domain.errors import ParserOutputContractError

_POLL_SECONDS = 1.0
_MAX_RESULT_BYTES = 4 * 1024 * 1024 * 1024
_MAX_ZIP_MEMBERS = 100_000
_MAX_UNCOMPRESSED_BYTES = 16 * 1024 * 1024 * 1024
_MAX_WIRE_JSON_BYTES = 1024 * 1024
_MANIFEST_NAME = ".agent-materialization-manifest.v1.json"
_INFLIGHT_MARKER_NAME = ".agent-materialization-inflight.v1.json"
_MAX_DECODED_BYTES = 4 * 1024 * 1024 * 1024
_F = TypeVar("_F", bound=Callable[..., object])
_PROCESS_LOCKS_GUARD = Lock()
_PROCESS_LOCKS: dict[str, Lock] = {}


def _make_idempotency_key(
    source_pdf_sha256: str,
    attempt_identity: str,
    fence_identity: str,
    *,
    observed_unix: float,
) -> str:
    epoch = int(observed_unix)
    digest = hashlib.sha256(
        f"{epoch:x}\0{source_pdf_sha256}\0{attempt_identity}\0{fence_identity}".encode()
    ).hexdigest()
    return f"{epoch:x}.{digest}"


def _submission_form(options: ParserOptions, *, server_url: str) -> dict[str, str]:
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


def _validate_submission_facts(
    *,
    options: ParserOptions,
    source_pdf_sha256: str,
    attempt_identity: str,
    fence_identity: str,
    submission_epoch_unix: int,
) -> None:
    _identity(attempt_identity, "attempt identity")
    _identity(fence_identity, "fence identity")
    if (
        isinstance(submission_epoch_unix, bool)
        or not isinstance(submission_epoch_unix, int)
        or submission_epoch_unix < 0
    ):
        raise _fail("durable submission epoch is required")
    if not source_pdf_sha256.startswith("sha256:") or len(source_pdf_sha256) != 71:
        raise _fail("source identity is not canonical sha256")
    if (
        options.backend != "hybrid-http-client"
        or options.method != "auto"
        or options.language != "ch"
        or not options.formula
        or not options.table
        or options.effective_effort != "medium"
        or options.effective_image_analysis
        or options.start_page is not None
        or options.end_page is not None
        or not options.runtime_bundle_identity_sha256
    ):
        raise _fail("request is outside the pinned full-PDF Medium profile")


def _target_identity(options: ParserOptions) -> ParserTargetIdentity:
    return ParserTargetIdentity(
        name="MinerU",
        package_version="3.4.4",
        backend=options.backend,
        method=options.method,
        language=options.language,
        formula=options.formula,
        table=options.table,
        effort=options.effective_effort,
        image_analysis=options.effective_image_analysis,
        full_pdf=True,
        start_page=None,
        end_page=None,
        runtime_bundle_identity_sha256=options.runtime_bundle_identity_sha256 or "",
    )


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
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


def _snapshot_stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return _stat_identity(value)


def _prepared_snapshot_identity(value: PreparedLocalSubmission) -> tuple[int, ...]:
    return (
        value.snapshot_device,
        value.snapshot_inode,
        value.snapshot_mode,
        value.snapshot_uid,
        value.snapshot_nlink,
        value.snapshot_bytes,
        value.snapshot_mtime_ns,
        value.snapshot_ctime_ns,
    )


def _derived_snapshot_name(identity: PreparedSubmissionIdentity) -> str:
    digest = hashlib.sha256(
        (
            identity.attempt_identity
            + "\0"
            + identity.fence_identity
            + "\0"
            + identity.source_pdf_sha256
        ).encode()
    ).hexdigest()
    return f".upload-{digest}.pdf"


def _derived_retained_spool_paths(
    *,
    spool_root: Path,
    attempt_identity: str,
    fence_identity: str,
    artifact_owner_identity: str,
    artifact_sha256: str,
) -> tuple[Path, Path, Path]:
    """Return deterministic final, partial and lock paths for one remote artifact."""

    digest = hashlib.sha256(
        "\0".join(
            (
                attempt_identity,
                fence_identity,
                artifact_owner_identity,
                artifact_sha256,
            )
        ).encode()
    ).hexdigest()
    final = spool_root / f".retained-{digest}.zip"
    return final, final.with_suffix(".zip.part"), final.with_suffix(".zip.lock")


def _derived_materialization_staging_path(
    *,
    output_dir: Path,
    attempt_identity: str,
    fence_identity: str,
    artifact_sha256: str,
) -> Path:
    """Return the sole attempt-owned staging path beside the final output."""

    digest = hashlib.sha256(
        "\0".join(
            (output_dir.name, attempt_identity, fence_identity, artifact_sha256)
        ).encode()
    ).hexdigest()
    return output_dir.parent / f".{output_dir.name}.materializing-{digest}"


def _derived_materialization_lock_path(
    *,
    output_dir: Path,
    attempt_identity: str,
    fence_identity: str,
    artifact_sha256: str,
) -> Path:
    staging = _derived_materialization_staging_path(
        output_dir=output_dir,
        attempt_identity=attempt_identity,
        fence_identity=fence_identity,
        artifact_sha256=artifact_sha256,
    )
    return staging.with_suffix(".lock")


def _freeze_private_spool_root(path: Path) -> Path:
    configured = path.expanduser().absolute()
    parent = configured.parent.resolve(strict=True)
    absolute = parent / configured.name
    if not parent.exists():
        raise _fail("submission spool parent does not exist")
    for candidate in (parent, absolute):
        if candidate.exists() and candidate.is_symlink():
            raise _fail("submission spool path cannot contain a symlink endpoint")
    parent_stat = parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.getuid()
        or stat.S_IMODE(parent_stat.st_mode) & 0o022
    ):
        raise _fail("submission spool parent is not private")
    try:
        absolute.mkdir(mode=0o700)
    except FileExistsError:
        pass
    observed = absolute.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) & 0o077
    ):
        raise _fail("submission spool root is not private")
    return absolute.resolve(strict=True)


def _verify_snapshot_fd(fd: int, *, expected_sha256: str) -> tuple[os.stat_result, int]:
    observed = os.fstat(fd)
    _validate_snapshot_stat(observed)
    digest = hashlib.sha256()
    total = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1024 * 1024):
        total += len(chunk)
        digest.update(chunk)
    if "sha256:" + digest.hexdigest() != expected_sha256:
        raise _SnapshotContentDrift
    return observed, total


def _validate_snapshot_stat(observed: os.stat_result) -> None:
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o600
    ):
        raise _fail("submission snapshot identity is unsafe")


def _write_snapshot_from_source(
    *, source_fd: int, snapshot_fd: int, expected_sha256: str
) -> os.stat_result:
    os.lseek(source_fd, 0, os.SEEK_SET)
    while chunk := os.read(source_fd, 1024 * 1024):
        _write_all(snapshot_fd, chunk)
    os.fsync(snapshot_fd)
    observed, _ = _verify_snapshot_fd(snapshot_fd, expected_sha256=expected_sha256)
    return observed


def _unlink_owned_snapshot(
    path: Path, *, expected: PreparedLocalSubmission | None
) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return
    try:
        observed = os.fstat(fd)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise _fail("snapshot discard refused unsafe identity")
        if expected is not None and _stat_identity(observed) != (
            _prepared_snapshot_identity(expected)
        ):
            raise _fail("snapshot discard identity drifted")
        current = path.stat(follow_symlinks=False)
        if _stat_identity(current) != _stat_identity(observed):
            raise _fail("snapshot discard path changed during verification")
        path.unlink()
    finally:
        os.close(fd)


def _cleanup_stale_snapshot_temps(root: Path, snapshot: Path) -> None:
    for candidate in root.glob(snapshot.stem + ".tmp-*"):
        try:
            fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            continue
        try:
            _validate_snapshot_stat(os.fstat(fd))
            current = candidate.stat(follow_symlinks=False)
            if _stat_identity(current) != _stat_identity(os.fstat(fd)):
                raise _fail("stale snapshot temporary path drifted")
            candidate.unlink()
        finally:
            os.close(fd)
    _fsync_directory(root)


def _write_all(fd: int, chunk: bytes) -> None:
    remaining = memoryview(chunk)
    while remaining:
        written = os.write(fd, remaining)
        if written < 1:
            raise OSError("snapshot write made no progress")
        remaining = remaining[written:]


def _open_private_lock(path: Path) -> int:
    try:
        fd = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise _fail("private lock is unavailable") from exc
    observed = os.fstat(fd)
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or observed.st_nlink != 1
        or stat.S_IMODE(observed.st_mode) != 0o600
    ):
        os.close(fd)
        raise _fail("private lock identity is unsafe")
    return fd


def _assert_private_lock_path(path: Path, fd: int) -> None:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise _fail("private lock path disappeared") from exc
    if _stat_identity(current) != _stat_identity(os.fstat(fd)):
        raise _fail("private lock path inode drifted")


def _process_lock_for(path: Path) -> Lock:
    key = str(path.absolute())
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, Lock())


@contextmanager
def _held_private_lock(path: Path) -> Iterator[None]:
    process_lock = _process_lock_for(path)
    with process_lock:
        fd = _open_private_lock(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _assert_private_lock_path(path, fd)
            yield
            _assert_private_lock_path(path, fd)
        finally:
            os.close(fd)


def _stable_materialization_lock_root(spool_root: Path) -> Path:
    lock_root = spool_root / ".materialization-locks"
    try:
        lock_root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    observed = lock_root.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o700
    ):
        raise _fail("materialization lock namespace is unsafe")
    return lock_root


def _with_materialization_lock(method: _F) -> _F:
    @wraps(method)
    def locked(self: object, *args: object, **kwargs: object) -> object:
        prepared = kwargs.get("prepared")
        output_dir = kwargs.get("output_dir")
        if not isinstance(prepared, PreparedMaterialization) or not isinstance(
            output_dir, Path
        ):
            raise _fail("materialization lock requires closed keyword inputs")
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        spool_root = getattr(self, "_spool_root", None)
        if not isinstance(spool_root, Path):
            raise _fail("materialization lock namespace is unavailable")
        lock_root = _stable_materialization_lock_root(spool_root)
        artifact_sha256 = prepared.spool_sha256.removeprefix("sha256:")
        derived = _derived_materialization_lock_path(
            output_dir=output_dir,
            attempt_identity=prepared.attempt_identity,
            fence_identity=prepared.fence_identity,
            artifact_sha256=artifact_sha256,
        )
        lock_path = lock_root / derived.name
        with _held_private_lock(lock_path):
            return method(self, *args, **kwargs)

    return cast(_F, locked)


def _open_verified_artifact_file(
    path: Path, *, expected_bytes: int, expected_sha256: str
) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise _fail("retained spool identity is unavailable") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size != expected_bytes
        ):
            raise _fail("retained spool identity drifted")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        if (
            _stat_identity(before) != _stat_identity(after)
            or _stat_identity(after) != _stat_identity(current)
            or digest.hexdigest() != expected_sha256
        ):
            raise _fail("retained spool content drifted")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _verify_owned_artifact_file(
    path: Path, *, expected_bytes: int, expected_sha256: str
) -> None:
    fd = _open_verified_artifact_file(
        path, expected_bytes=expected_bytes, expected_sha256=expected_sha256
    )
    os.close(fd)


def _promote_exact_open_file(
    path: Path, final: Path, fd: int, *, label: str
) -> None:
    before = os.fstat(fd)
    current = path.stat(follow_symlinks=False)
    if _stable_stat_identity(before) != _stable_stat_identity(current):
        raise _fail(f"{label} path changed before promotion")
    if final.exists() or final.is_symlink():
        raise _fail(f"{label} final path is occupied")
    os.replace(path, final)
    promoted = final.stat(follow_symlinks=False)
    if _stable_stat_identity(before) != _stable_stat_identity(promoted):
        raise _fail(f"{label} promoted inode drifted")
    _fsync_directory(final.parent)


def _promote_or_remove_owned_part(
    part: Path,
    final: Path,
    *,
    owner_path: Path,
    expected_owner: bytes,
    expected_bytes: int,
    expected_sha256: str,
) -> bool:
    owner_fd, _ = _open_private_owner_record(owner_path, expected=expected_owner)
    try:
        fd = _open_verified_or_partial_artifact(part)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
            ):
                raise _fail("retained partial identity is unsafe")
            digest = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(fd)
            current = part.stat(follow_symlinks=False)
            if _stat_identity(before) != _stat_identity(after) or _stat_identity(
                after
            ) != _stat_identity(current):
                raise _fail("retained partial path changed during verification")
            if (
                after.st_size == expected_bytes
                and digest.hexdigest() == expected_sha256
            ):
                _promote_exact_open_file(part, final, fd, label="retained partial")
                _unlink_exact_open_file(
                    owner_path, owner_fd, label="retained partial owner record"
                )
                return True
            _unlink_exact_open_file(part, fd, label="retained partial")
            _unlink_exact_open_file(
                owner_path, owner_fd, label="retained partial owner record"
            )
            return False
        finally:
            os.close(fd)
    finally:
        os.close(owner_fd)


def _retained_part_owner_path(part: Path) -> Path:
    return part.with_suffix(part.suffix + ".owner.json")


def _retained_part_owner_record(
    *, receipt: RemoteArtifactReceipt, final: Path, part: Path
) -> bytes:
    return json.dumps(
        {
            "artifact_byte_count": receipt.artifact_byte_count,
            "artifact_owner_identity": receipt.artifact_owner_identity,
            "artifact_sha256": receipt.artifact_sha256,
            "attempt_identity": receipt.attempt_identity,
            "fence_identity": receipt.fence_identity,
            "final_name": final.name,
            "part_name": part.name,
            "schema": "mineru-retained-part-owner.v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _open_verified_or_partial_artifact(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise _fail("retained partial identity is unavailable") from exc
    observed = os.fstat(fd)
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or observed.st_nlink != 1
        or stat.S_IMODE(observed.st_mode) != 0o600
    ):
        os.close(fd)
        raise _fail("retained partial identity is unsafe")
    return fd


def _open_private_owner_record(path: Path, *, expected: bytes) -> tuple[int, bytes]:
    fd, content = _open_private_record(
        path, label="retained partial owner record"
    )
    if content != expected:
        os.close(fd)
        raise _fail("retained partial owner record drifted")
    return fd, content


def _verify_private_owner_record(path: Path, *, expected: bytes) -> None:
    fd, _ = _open_private_owner_record(path, expected=expected)
    os.close(fd)


def _cleanup_exact_retained_residue(
    *, part: Path, owner_path: Path, expected_owner: bytes
) -> None:
    part_present = part.exists() or part.is_symlink()
    owner_present = owner_path.exists() or owner_path.is_symlink()
    if not part_present and not owner_present:
        return
    owner_fd, _ = _open_private_owner_record(owner_path, expected=expected_owner)
    try:
        if part_present:
            part_fd = _open_verified_or_partial_artifact(part)
            try:
                _unlink_exact_open_file(part, part_fd, label="retained partial")
            finally:
                os.close(part_fd)
        _unlink_exact_open_file(
            owner_path, owner_fd, label="retained partial owner record"
        )
    finally:
        os.close(owner_fd)


def _open_owned_directory(path: Path, *, label: str) -> int:
    try:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise _fail(f"{label} directory is unavailable") from exc
    observed = os.fstat(fd)
    if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != os.getuid():
        os.close(fd)
        raise _fail(f"{label} directory identity is unsafe")
    return fd


def _assert_directory_path(path: Path, fd: int, *, label: str) -> None:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise _fail(f"{label} directory path disappeared") from exc
    if _stat_identity(current) != _stat_identity(os.fstat(fd)):
        raise _fail(f"{label} directory inode drifted")


def _materialization_inflight_marker(
    *,
    output_dir: Path,
    attempt_identity: str,
    fence_identity: str,
    artifact_sha256: str,
    producer_claim_generation: int,
) -> bytes:
    return json.dumps(
        {
            "artifact_sha256": artifact_sha256,
            "attempt_identity": attempt_identity,
            "fence_identity": fence_identity,
            "output_name": output_dir.name,
            "producer_claim_generation": producer_claim_generation,
            "schema": "mineru-materialization-inflight.v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _stable_stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid)


def _open_private_record(
    path: Path, *, label: str, max_bytes: int = _MAX_WIRE_JSON_BYTES
) -> tuple[int, bytes]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise _fail(f"{label} is unavailable") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise _fail(f"{label} identity is unsafe")
        content = b""
        while chunk := os.read(fd, 64 * 1024):
            content += chunk
            if len(content) > max_bytes:
                raise _fail(f"{label} is oversized")
        after = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        if (
            _stat_identity(before) != _stat_identity(after)
            or _stat_identity(after) != _stat_identity(current)
        ):
            raise _fail(f"{label} path changed during verification")
        return fd, content
    except BaseException:
        os.close(fd)
        raise


def _quarantine_open_path(path: Path, fd: int, *, label: str) -> Path:
    before = os.fstat(fd)
    current = path.stat(follow_symlinks=False)
    if _stable_stat_identity(before) != _stable_stat_identity(current):
        raise _fail(f"{label} path changed before quarantine")
    quarantine = path.with_name(
        f".{path.name}.reclaim-{secrets.token_hex(16)}"
    )
    os.replace(path, quarantine)
    observed = quarantine.stat(follow_symlinks=False)
    if _stable_stat_identity(before) != _stable_stat_identity(observed):
        raise _fail(f"{label} quarantine inode drifted")
    return quarantine


def _unlink_exact_open_file(path: Path, fd: int, *, label: str) -> None:
    quarantine = _quarantine_open_path(path, fd, label=label)
    current = quarantine.stat(follow_symlinks=False)
    if _stable_stat_identity(current) != _stable_stat_identity(os.fstat(fd)):
        raise _fail(f"{label} quarantine path drifted")
    quarantine.unlink()
    _fsync_directory(path.parent)


def _remove_directory_contents_fd(directory_fd: int, *, label: str) -> None:
    for name in sorted(os.listdir(directory_fd)):
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise _fail(f"{label} contains an unsafe entry")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            child_fd = os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise _fail(f"{label} entry is unavailable") from exc
        try:
            child_stat = os.fstat(child_fd)
            quarantine_name = f".{name}.reclaim-{secrets.token_hex(16)}"
            os.rename(
                name,
                quarantine_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            current = os.stat(
                quarantine_name, dir_fd=directory_fd, follow_symlinks=False
            )
            if _stable_stat_identity(child_stat) != _stable_stat_identity(current):
                raise _fail(f"{label} entry quarantine inode drifted")
            if stat.S_ISDIR(child_stat.st_mode):
                _remove_directory_contents_fd(child_fd, label=label)
                current = os.stat(
                    quarantine_name, dir_fd=directory_fd, follow_symlinks=False
                )
                if _stable_stat_identity(os.fstat(child_fd)) != _stable_stat_identity(
                    current
                ):
                    raise _fail(f"{label} directory quarantine path drifted")
                os.rmdir(quarantine_name, dir_fd=directory_fd)
            elif stat.S_ISREG(child_stat.st_mode):
                current = os.stat(
                    quarantine_name, dir_fd=directory_fd, follow_symlinks=False
                )
                if _stable_stat_identity(os.fstat(child_fd)) != _stable_stat_identity(
                    current
                ):
                    raise _fail(f"{label} file quarantine path drifted")
                os.unlink(quarantine_name, dir_fd=directory_fd)
            else:
                raise _fail(f"{label} contains an unsafe entry")
        finally:
            os.close(child_fd)
    os.fsync(directory_fd)


def _remove_exact_open_directory(path: Path, fd: int, *, label: str) -> None:
    quarantine = _quarantine_open_path(path, fd, label=label)
    _remove_directory_contents_fd(fd, label=label)
    current = quarantine.stat(follow_symlinks=False)
    if _stable_stat_identity(current) != _stable_stat_identity(os.fstat(fd)):
        raise _fail(f"{label} quarantine path drifted")
    parent_fd = os.open(
        path.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        try:
            os.rmdir(quarantine.name, dir_fd=parent_fd)
        except OSError as exc:
            raise _fail(f"{label} quarantine removal raced") from exc
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _remove_exact_materialization_staging(
    staging: Path, *, expected_marker: bytes, current_generation: int
) -> None:
    if not staging.exists() and not staging.is_symlink():
        return
    if staging.is_symlink() or not staging.is_dir():
        raise _fail("materialization staging identity is unsafe")
    staging_stat = staging.stat(follow_symlinks=False)
    if (
        staging_stat.st_uid != os.getuid()
        or stat.S_IMODE(staging_stat.st_mode) != 0o700
    ):
        raise _fail("materialization staging ownership is unsafe")
    staging_fd = _open_owned_directory(staging, label="materialization staging")
    try:
        marker = staging / _INFLIGHT_MARKER_NAME
        marker_fd, marker_bytes = _open_private_record(
            marker, label="materialization staging owner marker"
        )
        try:
            marker_value = json.loads(marker_bytes)
            expected_value = json.loads(expected_marker)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _fail("materialization staging owner marker is invalid") from exc
        produced_generation = (
            marker_value.get("producer_claim_generation")
            if isinstance(marker_value, dict)
            else None
        )
        if isinstance(expected_value, dict):
            expected_value["producer_claim_generation"] = produced_generation
        canonical_expected = json.dumps(
            expected_value, sort_keys=True, separators=(",", ":")
        ).encode()
        if (
            type(produced_generation) is not int
            or not 1 <= produced_generation <= current_generation
            or marker_bytes != canonical_expected
        ):
            raise _fail("materialization staging owner marker drifted")
        _assert_directory_path(staging, staging_fd, label="materialization staging")
        current_marker = marker.stat(follow_symlinks=False)
        if _stat_identity(current_marker) != _stat_identity(os.fstat(marker_fd)):
            raise _fail("materialization staging owner marker path drifted")
        _remove_exact_open_directory(
            staging, staging_fd, label="materialization staging"
        )
        os.close(marker_fd)
        marker_fd = -1
        _fsync_directory(staging.parent)
    finally:
        if "marker_fd" in locals() and marker_fd >= 0:
            os.close(marker_fd)
        os.close(staging_fd)


def _fail(message: str) -> ParserOutputContractError:
    return ParserOutputContractError(f"MinerU staged HTTP contract: {message}")


class _SnapshotContentDrift(Exception):
    """A safely-owned deterministic snapshot is incomplete or corrupt."""


def _identity(value: str, label: str) -> str:
    value = value.strip()
    if not value or len(value) > 1024:
        raise _fail(f"invalid {label}")
    return value


def _same_origin_url(base_url: str, value: str, label: str) -> str:
    resolved = urljoin(base_url.rstrip("/") + "/", value)
    base = urlsplit(base_url)
    target = urlsplit(resolved)
    if (
        base.scheme not in {"http", "https"}
        or target.scheme != base.scheme
        or target.netloc != base.netloc
        or target.username is not None
        or target.password is not None
        or target.fragment
    ):
        raise _fail(f"{label} escaped the configured API origin")
    return resolved


@dataclass(frozen=True, slots=True)
class _Task:
    base_url: str
    task_id: str
    status_url: str
    result_url: str
    source_pdf_sha256: str
    attempt_identity: str
    fence_identity: str
    idempotency_key: str
    submission_epoch_unix: int
    ack_nonce_hex: str

    def token(self, *, spool_path: Path | None, artifact_sha256: str) -> str:
        raw = json.dumps(
            {
                "v": 3,
                "base_url": self.base_url,
                "task_id": self.task_id,
                "status_url": self.status_url,
                "result_url": self.result_url,
                "source_pdf_sha256": self.source_pdf_sha256,
                "attempt_identity": self.attempt_identity,
                "fence_identity": self.fence_identity,
                "idempotency_key": self.idempotency_key,
                "submission_epoch_unix": self.submission_epoch_unix,
                "ack_nonce_hex": self.ack_nonce_hex,
                "spool_path": "" if spool_path is None else str(spool_path),
                "artifact_sha256": artifact_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(raw).decode()

    @classmethod
    def from_token(cls, token: str) -> tuple[_Task, Path | None, str]:
        try:
            payload = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
        except (ValueError, json.JSONDecodeError) as exc:
            raise _fail("invalid durable resume token") from exc
        if not isinstance(payload, dict) or payload.get("v") != 3:
            raise _fail("invalid durable resume token shape")
        expected = {
            "v",
            "base_url",
            "task_id",
            "status_url",
            "result_url",
            "source_pdf_sha256",
            "attempt_identity",
            "fence_identity",
            "spool_path",
            "artifact_sha256",
            "idempotency_key",
            "submission_epoch_unix",
            "ack_nonce_hex",
        }
        if set(payload) != expected:
            raise _fail("invalid durable resume token shape")
        values = {
            key: payload[key]
            for key in payload
            if key
            not in {
                "v",
                "spool_path",
                "artifact_sha256",
            }
        }
        if (
            not all(
                isinstance(value, str)
                for key, value in values.items()
                if key != "submission_epoch_unix"
            )
            or type(values.get("submission_epoch_unix")) is not int
        ):
            raise _fail("invalid durable resume token values")
        task = cls(**values)
        task.validate()
        artifact_sha256 = payload["artifact_sha256"]
        spool_path = payload["spool_path"]
        if not isinstance(artifact_sha256, str) or not isinstance(spool_path, str):
            raise _fail("invalid durable spool identity")
        if len(artifact_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in artifact_sha256
        ):
            raise _fail("invalid durable artifact sha256")
        return task, Path(spool_path) if spool_path else None, artifact_sha256

    def validate(self) -> None:
        _identity(self.task_id, "task id")
        _identity(self.attempt_identity, "attempt identity")
        _identity(self.fence_identity, "fence identity")
        bucket, separator, digest = self.idempotency_key.partition(".")
        if (
            not separator
            or not bucket
            or any(char not in "0123456789abcdef" for char in bucket)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise _fail("invalid idempotency key")
        if (
            not self.source_pdf_sha256.startswith("sha256:")
            or len(self.source_pdf_sha256) != 71
            or any(
                char not in "0123456789abcdef" for char in self.source_pdf_sha256[7:]
            )
        ):
            raise _fail("invalid source sha256")
        _same_origin_url(self.base_url, self.status_url, "status URL")
        _same_origin_url(self.base_url, self.result_url, "result URL")
        if self.submission_epoch_unix < 0:
            raise _fail("invalid submission epoch")
        if len(self.ack_nonce_hex) != 64 or any(
            char not in "0123456789abcdef" for char in self.ack_nonce_hex
        ):
            raise _fail("invalid ACK nonce")

    def submission_checkpoint(
        self,
    ) -> tuple[PersistedSubmissionReceipt, PrivateSubmittedTaskResume]:
        projection = {
            "schema": "mineru-staged-submission.v1",
            "attempt_identity": self.attempt_identity,
            "fence_identity": self.fence_identity,
            "source_pdf_sha256": self.source_pdf_sha256,
            "client_submit_key": self.idempotency_key,
            "submission_epoch_unix": self.submission_epoch_unix,
            "remote_task_identity": self.task_id,
            "status_url": self.status_url,
            "result_url": self.result_url,
        }
        exact = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
        secret = self.token(spool_path=None, artifact_sha256="0" * 64).encode()
        return (
            PersistedSubmissionReceipt(
                schema="mineru-staged-submission.v1",
                attempt_identity=self.attempt_identity,
                fence_identity=self.fence_identity,
                source_pdf_sha256=self.source_pdf_sha256,
                client_submit_key=self.idempotency_key,
                submission_epoch_unix=self.submission_epoch_unix,
                remote_task_identity=self.task_id,
                status_url=self.status_url,
                result_url=self.result_url,
                exact_bytes=exact,
                sha256="sha256:" + hashlib.sha256(exact).hexdigest(),
            ),
            PrivateSubmittedTaskResume(
                token_bytes=secret,
                token_sha256="sha256:" + hashlib.sha256(secret).hexdigest(),
            ),
        )


class MinerUHttpRemoteHandle(RemoteProviderParseHandle):
    def __init__(
        self,
        *,
        task: _Task,
        options: ParserOptions,
        reader: MinerUMediumArtifactReader,
        spool_root: Path,
        transport: httpx.BaseTransport | None = None,
        terminal_spool: tuple[Path, str] | None = None,
    ) -> None:
        task.validate()
        self._task = task
        self._options = options
        self._reader = reader
        self._transport = transport
        self._spool_root = spool_root.resolve()
        self._terminal_spool = terminal_spool
        self._stop = Event()
        self._terminal_lock = Lock()
        self._terminal_receipt: RemoteArtifactReceipt | None = None
        self._terminal_error: BaseException | None = None

    def _client(self, timeout: float) -> httpx.Client:
        # MinerU is a private LAN/Tailnet endpoint. Never inherit proxy env.
        return httpx.Client(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        )

    def submission_checkpoint(
        self,
    ) -> tuple[PersistedSubmissionReceipt, PrivateSubmittedTaskResume]:
        return self._task.submission_checkpoint()

    def wait_terminal(self) -> RemoteArtifactReceipt:
        with self._terminal_lock:
            if self._terminal_receipt is not None:
                return self._terminal_receipt
            if self._terminal_error is not None:
                raise self._terminal_error
            try:
                receipt = self._wait_terminal_once()
            except BaseException as exc:
                self._terminal_error = exc
                raise
            self._terminal_receipt = receipt
            return receipt

    def _wait_terminal_once(self) -> RemoteArtifactReceipt:
        timeout = float(self._options.timeout_seconds or 3600)
        deadline = time.monotonic() + timeout
        retry_delay = 0.25
        with self._client(min(30.0, timeout)) as client:
            while time.monotonic() < deadline:
                try:
                    request = client.build_request("GET", self._task.status_url)
                    response = client.send(request, stream=True)
                except httpx.TransportError:
                    self._stop.wait(
                        min(retry_delay, max(0.0, deadline - time.monotonic()))
                    )
                    retry_delay = min(5.0, retry_delay * 2)
                    continue
                if 500 <= response.status_code <= 599:
                    response.close()
                    self._stop.wait(
                        min(retry_delay, max(0.0, deadline - time.monotonic()))
                    )
                    retry_delay = min(5.0, retry_delay * 2)
                    continue
                if response.status_code != 200:
                    response.close()
                    raise _fail(f"status returned HTTP {response.status_code}")
                retry_delay = 0.25
                try:
                    payload = _closed_json(response, required={"status"})
                finally:
                    response.close()
                status_value = payload["status"]
                if status_value in {"pending", "processing"}:
                    if self._stop.wait(
                        min(_POLL_SECONDS, max(0.0, deadline - time.monotonic()))
                    ):
                        return self._drain(client, deadline)
                    continue
                if status_value != "completed":
                    raise _fail(f"remote task terminated as {status_value!r}")
                return self._retained_receipt(payload, client=client)
        raise _fail("remote task deadline expired")

    def _drain(self, client: httpx.Client, deadline: float) -> RemoteArtifactReceipt:
        drain_deadline = max(
            deadline,
            time.monotonic() + float(self._options.api_drain_timeout_seconds),
        )
        while time.monotonic() < drain_deadline:
            with client.stream("GET", self._task.status_url) as response:
                if response.status_code != 200:
                    raise _fail(f"drain status returned HTTP {response.status_code}")
                payload = _closed_json(response, required={"status"})
            status_value = payload["status"]
            if status_value == "completed":
                return self._retained_receipt(payload, client=client)
            if status_value not in {"pending", "processing"}:
                raise _fail(f"remote task drained as {status_value!r}")
            time.sleep(_POLL_SECONDS)
        raise _fail("remote task did not drain before deadline")

    def _retained_receipt(
        self,
        payload: dict[str, Any],
        *,
        client: httpx.Client,
    ) -> RemoteArtifactReceipt:
        if payload.get("result_artifact_schema") != "mineru-retained-result.v1":
            raise _fail("unsupported retained result schema")
        artifact_sha256 = payload.get("result_artifact_sha256")
        artifact_bytes = payload.get("result_artifact_bytes")
        owner = payload.get("result_artifact_owner")
        if (
            not isinstance(artifact_sha256, str)
            or len(artifact_sha256) != 64
            or any(char not in "0123456789abcdef" for char in artifact_sha256)
            or type(artifact_bytes) is not int
            or not 0 < artifact_bytes <= _MAX_RESULT_BYTES
            or not isinstance(owner, str)
            or len(owner) != 64
            or any(char not in "0123456789abcdef" for char in owner)
        ):
            raise _fail("invalid retained result identity")
        expected_owner = hashlib.sha256(
            f"{self._task.task_id}\0{artifact_sha256}\0{artifact_bytes}".encode()
        ).hexdigest()
        if owner != expected_owner:
            raise _fail("retained result owner is not canonical")
        expected_protocol = {
            "task_protocol_schema": "mineru-task-protocol.v2",
            "protocol_state": "completed",
            "idempotency_key": self._task.idempotency_key,
            "attempt_identity": self._task.attempt_identity,
            "fence_identity": self._task.fence_identity,
        }
        if any(payload.get(key) != value for key, value in expected_protocol.items()):
            raise _fail("task protocol v2 status identity drifted")
        with client.stream(
            "POST", f"{self._task.base_url}/tasks/{self._task.task_id}/lease"
        ) as lease:
            if lease.status_code != 200:
                raise _fail(f"result lease returned HTTP {lease.status_code}")
            lease_payload = _closed_json(
                lease,
                required={"schema", "task_id", "lease_until_unix"},
                allowed={"schema", "task_id", "lease_until_unix"},
            )
        if (
            lease_payload.get("schema") != "mineru-task-protocol.v2"
            or lease_payload.get("task_id") != self._task.task_id
            or not isinstance(lease_payload.get("lease_until_unix"), (int, float))
        ):
            raise _fail("result lease identity drifted")
        return RemoteArtifactReceipt(
            attempt_identity=self._task.attempt_identity,
            fence_identity=self._task.fence_identity,
            artifact_owner_identity=owner,
            artifact_byte_count=artifact_bytes,
            artifact_sha256=artifact_sha256,
            source_pdf_sha256=self._task.source_pdf_sha256,
            resume_token=self._task.token(
                spool_path=None,
                artifact_sha256=artifact_sha256,
            ),
        )

    def prepare_materialization(
        self, *, receipt: RemoteArtifactReceipt, source_pdf_sha256: str
    ) -> PreparedMaterialization:
        task, spool_path, artifact_sha256 = self._validate_receipt(
            receipt, source_pdf_sha256=source_pdf_sha256
        )
        resolved_spool = spool_path.resolve() if spool_path is not None else None
        if (
            resolved_spool is not None
            and self._spool_root not in resolved_spool.parents
        ):
            raise _fail("spool path escaped its private root")
        if resolved_spool is None:
            resolved_spool = self._download_retained_result(receipt)
        try:
            compressed, uncompressed, members, decoded = _inspect_zip(resolved_spool)
            if compressed != receipt.artifact_byte_count:
                raise _fail("prepared compressed byte count drifted")
            if _sha256_file(resolved_spool) != receipt.artifact_sha256:
                raise _fail("prepared spool content drifted")
        except BaseException:
            # A verified deterministic final is durable crash-recovery input.
            # Only the downloader owns and removes its exact ``.part`` path.
            raise
        terminal_exact = _terminal_receipt_exact(receipt)
        token = task.token(
            spool_path=resolved_spool, artifact_sha256=artifact_sha256
        ).encode("ascii")
        self._terminal_spool = (resolved_spool, artifact_sha256)
        return PreparedMaterialization(
            attempt_identity=task.attempt_identity,
            fence_identity=task.fence_identity,
            source_pdf_sha256=task.source_pdf_sha256,
            terminal_receipt_sha256="sha256:"
            + hashlib.sha256(terminal_exact).hexdigest(),
            spool_sha256="sha256:" + artifact_sha256,
            compressed_bytes=compressed,
            uncompressed_bytes=uncompressed,
            member_count=members,
            disk_bytes=uncompressed,
            decoded_bytes=decoded,
            private_token_bytes=token,
            private_token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
        )

    @_with_materialization_lock
    def materialize_prepared(
        self,
        *,
        prepared: PreparedMaterialization,
        receipt: RemoteArtifactReceipt,
        output_dir: Path,
        source_pdf_sha256: str,
        parser_target_identity_sha256: str,
        producer_claim_generation: int,
    ) -> StagedProviderParserResult:
        if isinstance(producer_claim_generation, bool) or producer_claim_generation < 1:
            raise _fail("producer claim generation is invalid")
        if (
            not parser_target_identity_sha256.startswith("sha256:")
            or len(parser_target_identity_sha256) != 71
        ):
            raise _fail("parser target identity is invalid")
        task, spool_path, artifact_sha256 = _Task.from_token(
            prepared.private_token_bytes.decode("ascii")
        )
        self._validate_receipt(receipt, source_pdf_sha256=source_pdf_sha256)
        terminal_sha = (
            "sha256:" + hashlib.sha256(_terminal_receipt_exact(receipt)).hexdigest()
        )
        if (
            task != self._task
            or prepared.attempt_identity != task.attempt_identity
            or prepared.fence_identity != task.fence_identity
            or prepared.source_pdf_sha256 != task.source_pdf_sha256
            or prepared.terminal_receipt_sha256 != terminal_sha
            or prepared.spool_sha256 != "sha256:" + artifact_sha256
            or prepared.private_token_sha256
            != "sha256:" + hashlib.sha256(prepared.private_token_bytes).hexdigest()
            or spool_path is None
        ):
            raise _fail("prepared materialization identity drifted")
        resolved_spool = spool_path.resolve()
        if self._spool_root not in resolved_spool.parents:
            raise _fail("prepared spool escaped its private root")
        compressed = prepared.compressed_bytes
        uncompressed = prepared.uncompressed_bytes
        members = prepared.member_count
        decoded = prepared.decoded_bytes
        if artifact_sha256 != receipt.artifact_sha256:
            raise _fail("prepared materialization projections drifted")
        if not output_dir.exists():
            compressed, uncompressed, members, decoded = _inspect_zip(resolved_spool)
            if (compressed, uncompressed, members, uncompressed, decoded) != (
                prepared.compressed_bytes,
                prepared.uncompressed_bytes,
                prepared.member_count,
                prepared.disk_bytes,
                prepared.decoded_bytes,
            ) or _sha256_file(resolved_spool) != receipt.artifact_sha256:
                raise _fail("prepared materialization projections drifted")

        target_identity = self._target_identity()
        manifest_projection = {
            "schema": "mineru-local-materialization.v1",
            "attempt_identity": task.attempt_identity,
            "fence_identity": task.fence_identity,
            "source_pdf_sha256": task.source_pdf_sha256,
            "terminal_receipt_sha256": terminal_sha,
            "terminal_owner_identity": receipt.artifact_owner_identity,
            "terminal_artifact_sha256": receipt.artifact_sha256,
            "terminal_artifact_bytes": receipt.artifact_byte_count,
            "parser_target_identity_sha256": parser_target_identity_sha256,
            "produced_generation": producer_claim_generation,
            "projections": {
                "compressed_bytes": compressed,
                "uncompressed_bytes": uncompressed,
                "member_count": members,
                "disk_bytes": uncompressed,
                "decoded_bytes": decoded,
            },
        }
        output_fd = -1
        if output_dir.exists():
            if output_dir.is_symlink() or not output_dir.is_dir():
                raise _fail("existing output is not an owned directory")
            output_fd = _open_owned_directory(output_dir, label="materialized output")
            try:
                manifest_exact, manifest = _read_and_verify_manifest(
                    output_dir,
                    expected=manifest_projection,
                    current_generation=producer_claim_generation,
                )
                _assert_directory_path(
                    output_dir, output_fd, label="materialized output"
                )
            except BaseException:
                os.close(output_fd)
                output_fd = -1
                raise
        else:
            output_dir.parent.mkdir(parents=True, exist_ok=True)
            staging = _derived_materialization_staging_path(
                output_dir=output_dir,
                attempt_identity=task.attempt_identity,
                fence_identity=task.fence_identity,
                artifact_sha256=artifact_sha256,
            )
            inflight_marker = _materialization_inflight_marker(
                output_dir=output_dir,
                attempt_identity=task.attempt_identity,
                fence_identity=task.fence_identity,
                artifact_sha256=artifact_sha256,
                producer_claim_generation=producer_claim_generation,
            )
            _remove_exact_materialization_staging(
                staging,
                expected_marker=inflight_marker,
                current_generation=producer_claim_generation,
            )
            staging.mkdir(mode=0o700)
            _write_fsynced(staging / _INFLIGHT_MARKER_NAME, inflight_marker)
            (staging / _INFLIGHT_MARKER_NAME).chmod(0o600)
            with (staging / _INFLIGHT_MARKER_NAME).open("rb") as marker_source:
                os.fsync(marker_source.fileno())
            _fsync_directory(staging)
            _fsync_directory(output_dir.parent)
            promoted = False
            try:
                _safe_extract(resolved_spool, staging)
                files = [
                    item
                    for item in _tree_file_receipts(staging)
                    if item["path"] != _INFLIGHT_MARKER_NAME
                ]
                manifest = {**manifest_projection, "files": files}
                manifest_exact = json.dumps(
                    manifest, sort_keys=True, separators=(",", ":")
                ).encode()
                _write_fsynced(staging / _MANIFEST_NAME, manifest_exact)
                _fsync_tree(staging)
                os.replace(staging, output_dir)
                _fsync_directory(output_dir.parent)
                promoted = True
                (output_dir / _INFLIGHT_MARKER_NAME).unlink(missing_ok=True)
                _fsync_directory(output_dir)
            finally:
                if not promoted and staging.exists():
                    _remove_exact_materialization_staging(
                        staging,
                        expected_marker=inflight_marker,
                        current_generation=producer_claim_generation,
                    )
            output_fd = _open_owned_directory(output_dir, label="materialized output")
            try:
                manifest_exact, manifest = _read_and_verify_manifest(
                    output_dir,
                    expected=manifest_projection,
                    current_generation=producer_claim_generation,
                )
                _assert_directory_path(
                    output_dir, output_fd, label="materialized output"
                )
            except BaseException:
                os.close(output_fd)
                output_fd = -1
                raise
        try:
            _assert_directory_path(output_dir, output_fd, label="materialized output")
            provider_document, artifact_root = _read_from_closed_output_snapshot(
                reader=self._reader,
                output_dir=output_dir,
                source_pdf_sha256=source_pdf_sha256,
                manifest_exact=manifest_exact,
                manifest=manifest,
            )
            _assert_directory_path(output_dir, output_fd, label="materialized output")
            after_exact, after_manifest = _read_and_verify_manifest(
                output_dir,
                expected=manifest_projection,
                current_generation=producer_claim_generation,
            )
            if after_exact != manifest_exact or after_manifest != manifest:
                raise _fail("materialized output changed during reader admission")
            _assert_directory_path(output_dir, output_fd, label="materialized output")
        finally:
            if output_fd >= 0:
                os.close(output_fd)
        resolved_spool.unlink(missing_ok=True)
        _fsync_directory(resolved_spool.parent)
        result = ProviderParserResult(
            target_identity=target_identity,
            artifact_root=artifact_root,
            provider_document=provider_document,
        )
        try:
            artifact_root_relpath = artifact_root.relative_to(output_dir).as_posix()
        except ValueError as exc:
            raise _fail("reader artifact root escaped materialized output") from exc
        evidence = ProviderMaterializationEvidence(
            attempt_identity=task.attempt_identity,
            fence_identity=task.fence_identity,
            source_pdf_sha256=task.source_pdf_sha256,
            parser_target_identity_sha256=manifest["parser_target_identity_sha256"],
            producer_claim_generation=manifest["produced_generation"],
            terminal_owner_identity=receipt.artifact_owner_identity,
            terminal_artifact_sha256=receipt.artifact_sha256,
            terminal_artifact_bytes=receipt.artifact_byte_count,
            artifact_root_relpath=artifact_root_relpath or ".",
            manifest_relpath=_MANIFEST_NAME,
            manifest_sha256="sha256:" + hashlib.sha256(manifest_exact).hexdigest(),
            manifest_bytes=len(manifest_exact),
        )
        return StagedProviderParserResult(result=result, evidence=evidence)

    def _validate_receipt(
        self, receipt: RemoteArtifactReceipt, *, source_pdf_sha256: str
    ) -> tuple[_Task, Path | None, str]:
        if source_pdf_sha256 != self._task.source_pdf_sha256:
            raise _fail("receipt/source ownership drifted before materialization")
        task, spool_path, artifact_sha256 = _Task.from_token(receipt.resume_token)
        expected_owner = hashlib.sha256(
            f"{self._task.task_id}\0{receipt.artifact_sha256}\0{receipt.artifact_byte_count}".encode()
        ).hexdigest()
        if (
            task != self._task
            or artifact_sha256 != receipt.artifact_sha256
            or receipt.attempt_identity != self._task.attempt_identity
            or receipt.fence_identity != self._task.fence_identity
            or receipt.source_pdf_sha256 != self._task.source_pdf_sha256
            or receipt.artifact_owner_identity != expected_owner
            or receipt.artifact_byte_count <= 0
        ):
            raise _fail("receipt ownership drifted before materialization")
        return task, spool_path, artifact_sha256

    def _target_identity(self) -> ParserTargetIdentity:
        return _target_identity(self._options)

    def acknowledge_after_finish_committed(
        self,
        *,
        receipt: RemoteArtifactReceipt,
        witness: DurableCheckpointWitness,
    ) -> ProviderAckCompletionWitness:
        if (
            witness.state != "finish_committed"
            or witness.attempt_identity != receipt.attempt_identity
            or witness.fence_identity != receipt.fence_identity
            or witness.remote_task_identity != self._task.task_id
            or witness.terminal_receipt_sha256
            != "sha256:" + hashlib.sha256(_terminal_receipt_exact(receipt)).hexdigest()
        ):
            raise _fail("remote ACK requires a durable finish_committed checkpoint")
        task, _spool_path, artifact_sha256 = _Task.from_token(receipt.resume_token)
        if task != self._task or artifact_sha256 != receipt.artifact_sha256:
            raise _fail("remote ACK receipt ownership drifted")
        status = self._ack_terminal()
        return _issue_provider_ack_completion_witness(
            attempt_identity=receipt.attempt_identity,
            fence_identity=receipt.fence_identity,
            remote_task_identity=self._task.task_id,
            source_pdf_sha256=receipt.source_pdf_sha256,
            committed_state="finish_committed",
            terminal_receipt_sha256=witness.terminal_receipt_sha256,
            failure_receipt_sha256=None,
            http_status=status,
            accepted_secret=self._task.submission_checkpoint()[1].token_bytes,
        )

    def acknowledge_after_failure_committed(
        self,
        *,
        witness: DurableCheckpointWitness,
        failure_receipt: EncodedCheckpointReceipt,
    ) -> ProviderAckCompletionWitness:
        failure = failure_receipt.receipt
        if (
            not isinstance(failure, FailureReceipt)
            or witness.state
            not in {
                "remote_failure_committed",
                "local_failure_committed",
            }
            or witness.attempt_identity != self._task.attempt_identity
            or witness.fence_identity != self._task.fence_identity
            or witness.remote_task_identity != self._task.task_id
            or witness.source_pdf_sha256 != self._task.source_pdf_sha256
            or witness.client_submit_key != self._task.idempotency_key
            or witness.submission_epoch_unix != self._task.submission_epoch_unix
            or failure.attempt_identity != self._task.attempt_identity
            or failure.fence_identity != self._task.fence_identity
            or failure.remote_task_identity != self._task.task_id
            or failure_receipt.sha256 != witness.failure_receipt_sha256
        ):
            raise _fail(
                "remote failure ACK requires a durable remote_failure_committed "
                "or local_failure_committed checkpoint"
            )
        expected_stage = (
            "remote" if witness.state == "remote_failure_committed" else "local"
        )
        if failure.stage != expected_stage or not failure.ack_required:
            raise _fail("failure receipt stage/ACK contract drifted")
        if witness.state == "local_failure_committed" and (
            failure.terminal_receipt_sha256 != witness.terminal_receipt_sha256
        ):
            raise _fail("local failure terminal receipt drifted")
        status = self._ack_terminal()
        return _issue_provider_ack_completion_witness(
            attempt_identity=failure.attempt_identity,
            fence_identity=failure.fence_identity,
            remote_task_identity=self._task.task_id,
            source_pdf_sha256=witness.source_pdf_sha256,
            committed_state=witness.state,
            terminal_receipt_sha256=witness.terminal_receipt_sha256,
            failure_receipt_sha256=witness.failure_receipt_sha256,
            http_status=status,
            accepted_secret=self._task.submission_checkpoint()[1].token_bytes,
        )

    def _ack_terminal(self) -> int:
        with self._client(30.0) as client:
            with client.stream(
                "POST", f"{self._task.base_url}/tasks/{self._task.task_id}/ack"
            ) as response:
                if response.status_code not in {200, 204}:
                    raise _fail(
                        f"result acknowledgement returned HTTP {response.status_code}"
                    )
                payload = (
                    None
                    if response.status_code == 204
                    else _closed_json(
                        response,
                        required={"schema", "task_id", "status"},
                        allowed={"schema", "task_id", "status"},
                    )
                )
            if payload is not None and (
                set(payload) != {"schema", "task_id", "status"}
                or payload
                != {
                    "schema": "mineru-task-protocol.v2",
                    "task_id": self._task.task_id,
                    "status": "consumed",
                }
            ):
                raise _fail("result acknowledgement identity drifted")
            return response.status_code

    def _download_retained_result(self, receipt: RemoteArtifactReceipt) -> Path:
        self._spool_root.mkdir(parents=True, exist_ok=True)
        final, part, lock = _derived_retained_spool_paths(
            spool_root=self._spool_root,
            attempt_identity=receipt.attempt_identity,
            fence_identity=receipt.fence_identity,
            artifact_owner_identity=receipt.artifact_owner_identity,
            artifact_sha256=receipt.artifact_sha256,
        )
        owner_path = _retained_part_owner_path(part)
        owner_record = _retained_part_owner_record(
            receipt=receipt, final=final, part=part
        )
        with _held_private_lock(lock):
            if final.exists() or final.is_symlink():
                _verify_owned_artifact_file(
                    final,
                    expected_bytes=receipt.artifact_byte_count,
                    expected_sha256=receipt.artifact_sha256,
                )
                _cleanup_exact_retained_residue(
                    part=part,
                    owner_path=owner_path,
                    expected_owner=owner_record,
                )
                return final
            if part.exists() or part.is_symlink():
                if _promote_or_remove_owned_part(
                    part,
                    final,
                    owner_path=owner_path,
                    expected_owner=owner_record,
                    expected_bytes=receipt.artifact_byte_count,
                    expected_sha256=receipt.artifact_sha256,
                ):
                    return final
            elif owner_path.exists() or owner_path.is_symlink():
                owner_fd, _ = _open_private_owner_record(
                    owner_path, expected=owner_record
                )
                try:
                    _unlink_exact_open_file(
                        owner_path,
                        owner_fd,
                        label="retained partial owner record",
                    )
                finally:
                    os.close(owner_fd)
            _write_fsynced(owner_path, owner_record)
            owner_path.chmod(0o600)
            owner_fd, _ = _open_private_owner_record(
                owner_path, expected=owner_record
            )
            os.fsync(owner_fd)
            os.close(owner_fd)
            _fsync_directory(self._spool_root)
            part_fd = os.open(
                part,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            digest = hashlib.sha256()
            received = 0
            try:
                with (
                    self._client(float(self._options.timeout_seconds or 600)) as client,
                    client.stream("GET", self._task.result_url) as response,
                ):
                    if (
                        response.status_code != 200
                        or "application/zip"
                        not in response.headers.get("content-type", "")
                    ):
                        raise _fail("retained result is unavailable")
                    if (
                        response.headers.get("x-mineru-result-sha256")
                        != receipt.artifact_sha256
                        or response.headers.get("x-mineru-result-owner")
                        != receipt.artifact_owner_identity
                    ):
                        raise _fail("retained result headers drifted")
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        received += len(chunk)
                        if received > receipt.artifact_byte_count:
                            raise _fail("retained result exceeded attested bytes")
                        digest.update(chunk)
                        _write_all(part_fd, chunk)
                    os.fsync(part_fd)
                if (
                    received != receipt.artifact_byte_count
                    or digest.hexdigest() != receipt.artifact_sha256
                ):
                    raise _fail("retained result content drifted")
            except BaseException:
                owner_fd, _ = _open_private_owner_record(
                    owner_path, expected=owner_record
                )
                try:
                    _unlink_exact_open_file(
                        part, part_fd, label="retained partial"
                    )
                    _unlink_exact_open_file(
                        owner_path,
                        owner_fd,
                        label="retained partial owner record",
                    )
                finally:
                    os.close(owner_fd)
                    os.close(part_fd)
                    part_fd = -1
                raise
            finally:
                if part_fd >= 0:
                    os.close(part_fd)
            verified_part_fd = _open_verified_artifact_file(
                part,
                expected_bytes=receipt.artifact_byte_count,
                expected_sha256=receipt.artifact_sha256,
            )
            owner_fd, _ = _open_private_owner_record(
                owner_path, expected=owner_record
            )
            try:
                _promote_exact_open_file(
                    part, final, verified_part_fd, label="retained partial"
                )
                _unlink_exact_open_file(
                    owner_path,
                    owner_fd,
                    label="retained partial owner record",
                )
            finally:
                os.close(owner_fd)
                os.close(verified_part_fd)
            _verify_owned_artifact_file(
                final,
                expected_bytes=receipt.artifact_byte_count,
                expected_sha256=receipt.artifact_sha256,
            )
            return final

    def cancel_and_drain(self) -> None:
        self._stop.set()
        # MinerU 3.4.4 exposes no cancel endpoint. The caller must not regain
        # admission merely because local waiting was cancelled.
        self.wait_terminal()


class MinerUHttpStagedParser:
    def __init__(
        self,
        *,
        api_url: str,
        server_url: str,
        spool_root: Path,
        reader: MinerUMediumArtifactReader | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._server_url = server_url
        self._reader = reader or MinerUMediumArtifactReader()
        self._transport = transport
        self._spool_root = _freeze_private_spool_root(spool_root)

    def prepare_submission_identity(
        self,
        *,
        options: ParserOptions,
        source_pdf_sha256: str,
        attempt_identity: str,
        fence_identity: str,
        submission_epoch_unix: int,
    ) -> PreparedSubmissionIdentity:
        _validate_submission_facts(
            options=options,
            source_pdf_sha256=source_pdf_sha256,
            attempt_identity=attempt_identity,
            fence_identity=fence_identity,
            submission_epoch_unix=submission_epoch_unix,
        )
        target = _target_identity(options)
        target_exact = json.dumps(
            target.to_payload(), sort_keys=True, separators=(",", ":")
        ).encode()
        request_exact = json.dumps(
            {
                "schema": "mineru-staged-request.v1",
                "api_origin": self._api_url,
                "form": _submission_form(options, server_url=self._server_url),
                "upload_filename": f"{source_pdf_sha256[7:]}.pdf",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        parser_target_sha256 = "sha256:" + hashlib.sha256(target_exact).hexdigest()
        request_sha256 = "sha256:" + hashlib.sha256(request_exact).hexdigest()
        client_submit_key = _make_idempotency_key(
            source_pdf_sha256,
            attempt_identity,
            fence_identity,
            observed_unix=float(submission_epoch_unix),
        )
        projection = {
            "schema": "mineru-prepared-submission.v1",
            "attempt_identity": attempt_identity,
            "fence_identity": fence_identity,
            "source_pdf_sha256": source_pdf_sha256,
            "parser_target_identity_sha256": parser_target_sha256,
            "runtime_bundle_identity_sha256": options.runtime_bundle_identity_sha256,
            "request_sha256": request_sha256,
            "client_submit_key": client_submit_key,
            "submission_epoch_unix": submission_epoch_unix,
        }
        exact = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
        return PreparedSubmissionIdentity(
            schema="mineru-prepared-submission.v1",
            attempt_identity=attempt_identity,
            fence_identity=fence_identity,
            source_pdf_sha256=source_pdf_sha256,
            parser_target_identity_sha256=parser_target_sha256,
            runtime_bundle_identity_sha256=options.runtime_bundle_identity_sha256 or "",
            request_sha256=request_sha256,
            client_submit_key=client_submit_key,
            submission_epoch_unix=submission_epoch_unix,
            exact_bytes=exact,
            sha256="sha256:" + hashlib.sha256(exact).hexdigest(),
        )

    def prepare_local_submission(
        self,
        *,
        input_pdf: Path,
        options: ParserOptions,
        identity: PreparedSubmissionIdentity,
        witness: DurableCheckpointWitness,
    ) -> PreparedLocalSubmission:
        expected = self.prepare_submission_identity(
            options=options,
            source_pdf_sha256=identity.source_pdf_sha256,
            attempt_identity=identity.attempt_identity,
            fence_identity=identity.fence_identity,
            submission_epoch_unix=identity.submission_epoch_unix,
        )
        if identity != expected:
            raise _fail("durable prepared submission identity drifted")
        if (
            witness.state not in {"prepared", "reconciling"}
            or witness.attempt_identity != identity.attempt_identity
            or witness.fence_identity != identity.fence_identity
            or witness.prepared_submission_sha256 != identity.sha256
            or witness.source_pdf_sha256 != identity.source_pdf_sha256
            or witness.parser_target_identity_sha256
            != identity.parser_target_identity_sha256
            or witness.runtime_bundle_identity_sha256
            != identity.runtime_bundle_identity_sha256
            or witness.request_sha256 != identity.request_sha256
            or witness.client_submit_key != identity.client_submit_key
            or witness.submission_epoch_unix != identity.submission_epoch_unix
        ):
            raise _fail("local submission requires its durable prepared checkpoint")
        snapshot = self._spool_root / _derived_snapshot_name(identity)
        if witness.state == "reconciling":
            lock_path = snapshot.with_suffix(".lock")
            lock_fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            try:
                _validate_snapshot_stat(os.fstat(lock_fd))
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                snapshot_fd = os.open(
                    snapshot, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    snapshot_stat, _ = _verify_snapshot_fd(
                        snapshot_fd, expected_sha256=identity.source_pdf_sha256
                    )
                    current = snapshot.stat(follow_symlinks=False)
                    if _stat_identity(current) != _stat_identity(snapshot_stat):
                        raise _fail("reconciling snapshot path drifted")
                finally:
                    os.close(snapshot_fd)
            finally:
                os.close(lock_fd)
            return PreparedLocalSubmission.from_checkpoint(
                identity=identity,
                witness=witness,
                snapshot_path=snapshot,
                snapshot_sha256=identity.source_pdf_sha256,
                snapshot_bytes=snapshot_stat.st_size,
                snapshot_device=snapshot_stat.st_dev,
                snapshot_inode=snapshot_stat.st_ino,
                snapshot_mode=snapshot_stat.st_mode,
                snapshot_uid=snapshot_stat.st_uid,
                snapshot_nlink=snapshot_stat.st_nlink,
                snapshot_mtime_ns=snapshot_stat.st_mtime_ns,
                snapshot_ctime_ns=snapshot_stat.st_ctime_ns,
            )
        source_fd = os.open(input_pdf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            os.close(source_fd)
            raise _fail("source PDF identity is unsafe")
        source_digest = hashlib.sha256()
        while chunk := os.read(source_fd, 1024 * 1024):
            source_digest.update(chunk)
        after_hash = os.fstat(source_fd)
        if (
            _stat_identity(before) != _stat_identity(after_hash)
            or "sha256:" + source_digest.hexdigest() != identity.source_pdf_sha256
        ):
            os.close(source_fd)
            raise _fail("source differs from prepared submission")
        lock_path = snapshot.with_suffix(".lock")
        temp_path: Path | None = None
        try:
            lock_fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                _validate_snapshot_stat(os.fstat(lock_fd))
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                _cleanup_stale_snapshot_temps(self._spool_root, snapshot)
                if snapshot.exists():
                    snapshot_fd = os.open(
                        snapshot, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    )
                    try:
                        snapshot_stat, _ = _verify_snapshot_fd(
                            snapshot_fd,
                            expected_sha256=identity.source_pdf_sha256,
                        )
                    except _SnapshotContentDrift as exc:
                        raise _fail("published submission snapshot drifted") from exc
                    finally:
                        os.close(snapshot_fd)
                else:
                    temp_fd, temp_name = tempfile.mkstemp(
                        prefix=snapshot.stem + ".tmp-", dir=self._spool_root
                    )
                    temp_path = Path(temp_name)
                    try:
                        _write_snapshot_from_source(
                            source_fd=source_fd,
                            snapshot_fd=temp_fd,
                            expected_sha256=identity.source_pdf_sha256,
                        )
                        after = os.fstat(source_fd)
                        if _stat_identity(before) != _stat_identity(after):
                            raise _fail(
                                "source changed while preparing upload snapshot"
                            )
                    finally:
                        os.close(temp_fd)
                    os.replace(temp_path, snapshot)
                    temp_path = None
                    _fsync_directory(self._spool_root)
                    snapshot_fd = os.open(
                        snapshot, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    )
                    try:
                        snapshot_stat, _ = _verify_snapshot_fd(
                            snapshot_fd,
                            expected_sha256=identity.source_pdf_sha256,
                        )
                    finally:
                        os.close(snapshot_fd)
            finally:
                os.close(lock_fd)
        finally:
            os.close(source_fd)
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return PreparedLocalSubmission.from_checkpoint(
            identity=identity,
            witness=witness,
            snapshot_path=snapshot,
            snapshot_sha256=identity.source_pdf_sha256,
            snapshot_bytes=snapshot_stat.st_size,
            snapshot_device=snapshot_stat.st_dev,
            snapshot_inode=snapshot_stat.st_ino,
            snapshot_mode=snapshot_stat.st_mode,
            snapshot_uid=snapshot_stat.st_uid,
            snapshot_nlink=snapshot_stat.st_nlink,
            snapshot_mtime_ns=snapshot_stat.st_mtime_ns,
            snapshot_ctime_ns=snapshot_stat.st_ctime_ns,
        )

    def discard_local_submission(
        self,
        *,
        prepared_submission: PreparedLocalSubmission | PreparedSubmissionIdentity,
        witness: DurableCheckpointWitness,
        submission_receipt: PersistedSubmissionReceipt | None = None,
        accepted_receipt: EncodedCheckpointReceipt | None = None,
        failure_receipt: EncodedCheckpointReceipt | None = None,
    ) -> None:
        identity = (
            prepared_submission.identity
            if isinstance(prepared_submission, PreparedLocalSubmission)
            else prepared_submission
        )
        if (
            witness.state
            not in {
                "submitted",
                "pre_submission_failed",
                "remote_failure_committed",
                "local_failure_committed",
            }
            or witness.attempt_identity != identity.attempt_identity
            or witness.fence_identity != identity.fence_identity
            or witness.prepared_submission_sha256 != identity.sha256
            or witness.source_pdf_sha256 != identity.source_pdf_sha256
            or witness.client_submit_key != identity.client_submit_key
            or witness.submission_epoch_unix != identity.submission_epoch_unix
        ):
            raise _fail("snapshot discard requires an exact durable checkpoint state")
        if witness.state == "submitted":
            accepted = None if accepted_receipt is None else accepted_receipt.receipt
            if (
                submission_receipt is None
                or not isinstance(accepted, AcceptedSubmissionReceipt)
                or accepted_receipt is None
                or failure_receipt is not None
                or submission_receipt.attempt_identity != witness.attempt_identity
                or submission_receipt.fence_identity != witness.fence_identity
                or submission_receipt.source_pdf_sha256 != witness.source_pdf_sha256
                or submission_receipt.client_submit_key != witness.client_submit_key
                or submission_receipt.submission_epoch_unix
                != witness.submission_epoch_unix
                or submission_receipt.remote_task_identity
                != witness.remote_task_identity
                or accepted_receipt.sha256 != witness.accepted_submission_receipt_sha256
                or accepted.attempt_identity != submission_receipt.attempt_identity
                or accepted.fence_identity != submission_receipt.fence_identity
                or accepted.source_pdf_sha256 != submission_receipt.source_pdf_sha256
                or accepted.client_submit_key != submission_receipt.client_submit_key
                or accepted.submission_epoch_unix
                != submission_receipt.submission_epoch_unix
                or accepted.remote_task_identity
                != submission_receipt.remote_task_identity
            ):
                raise _fail("submitted snapshot discard receipt drifted")
        else:
            failure = None if failure_receipt is None else failure_receipt.receipt
            failure_sha256 = None if failure_receipt is None else failure_receipt.sha256
            if (
                not isinstance(failure, FailureReceipt)
                or submission_receipt is not None
                or accepted_receipt is not None
                or failure.attempt_identity != witness.attempt_identity
                or failure.fence_identity != witness.fence_identity
                or failure.remote_task_identity != witness.remote_task_identity
                or failure.terminal_receipt_sha256 != witness.terminal_receipt_sha256
                or failure_sha256 != witness.failure_receipt_sha256
            ):
                raise _fail("failure snapshot discard receipt drifted")
        expected_path = self._spool_root / _derived_snapshot_name(identity)
        if (
            isinstance(prepared_submission, PreparedLocalSubmission)
            and prepared_submission.snapshot_path != expected_path
        ):
            raise _fail("snapshot discard path drifted")
        lock_path = expected_path.with_suffix(".lock")
        try:
            lock_fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            if expected_path.exists():
                raise
            return
        try:
            _validate_snapshot_stat(os.fstat(lock_fd))
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if isinstance(prepared_submission, PreparedLocalSubmission):
                _unlink_owned_snapshot(expected_path, expected=prepared_submission)
            else:
                try:
                    snapshot_fd = os.open(
                        expected_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    )
                except FileNotFoundError:
                    snapshot_fd = None
                if snapshot_fd is None:
                    _cleanup_stale_snapshot_temps(self._spool_root, expected_path)
                    lock_stat = os.fstat(lock_fd)
                    current = lock_path.stat(follow_symlinks=False)
                    if _stat_identity(current) != _stat_identity(lock_stat):
                        raise _fail("snapshot lock path drifted")
                    _fsync_directory(self._spool_root)
                    return
                try:
                    snapshot_stat, _ = _verify_snapshot_fd(
                        snapshot_fd, expected_sha256=identity.source_pdf_sha256
                    )
                    current = expected_path.stat(follow_symlinks=False)
                    if _stat_identity(current) != _stat_identity(snapshot_stat):
                        raise _fail("snapshot path drifted during recovery discard")
                    expected_path.unlink()
                finally:
                    os.close(snapshot_fd)
            _cleanup_stale_snapshot_temps(self._spool_root, expected_path)
            lock_stat = os.fstat(lock_fd)
            current = lock_path.stat(follow_symlinks=False)
            if _stat_identity(current) != _stat_identity(lock_stat):
                raise _fail("snapshot lock path drifted")
        finally:
            os.close(lock_fd)
        _fsync_directory(self._spool_root)

    def begin_remote_parse(
        self,
        *,
        options: ParserOptions,
        prepared_submission: PreparedLocalSubmission,
    ) -> MinerUHttpRemoteHandle:
        submission_identity = prepared_submission.identity
        source_pdf_sha256 = submission_identity.source_pdf_sha256
        attempt_identity = submission_identity.attempt_identity
        fence_identity = submission_identity.fence_identity
        expected_submission = self.prepare_submission_identity(
            options=options,
            source_pdf_sha256=source_pdf_sha256,
            attempt_identity=attempt_identity,
            fence_identity=fence_identity,
            submission_epoch_unix=submission_identity.submission_epoch_unix,
        )
        if submission_identity != expected_submission:
            raise SubmissionAcceptanceAmbiguous(
                "MinerU staged HTTP contract: durable prepared submission identity drifted"
            )
        snapshot = prepared_submission.snapshot_path
        try:
            snapshot_fd = os.open(snapshot, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            snapshot_stat = os.fstat(snapshot_fd)
        except OSError as exc:
            raise SubmissionAcceptanceAmbiguous(
                "MinerU staged HTTP contract: durable upload snapshot is unavailable"
            ) from exc
        observed_snapshot = _snapshot_stat_identity(snapshot_stat)
        expected_snapshot = _prepared_snapshot_identity(prepared_submission)
        if observed_snapshot != expected_snapshot:
            os.close(snapshot_fd)
            raise SubmissionAcceptanceAmbiguous(
                "MinerU staged HTTP contract: durable upload snapshot drifted"
            )
        data: dict[str, Any] = _submission_form(options, server_url=self._server_url)
        idempotency_key = submission_identity.client_submit_key
        data.update(
            {
                "agent_idempotency_key": idempotency_key,
                "agent_attempt_identity": attempt_identity,
                "agent_fence_identity": fence_identity,
            }
        )
        submit_allowed = {
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
        try:
            with (
                httpx.Client(
                    timeout=httpx.Timeout(float(options.timeout_seconds or 300)),
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                ) as client,
                os.fdopen(snapshot_fd, "rb") as source,
            ):
                # Reconcile the durable key before POST. A failed lookup is not
                # permission to submit: only an exact 404 proves absence.
                try:
                    lookup = client.send(
                        client.build_request(
                            "GET",
                            f"{self._api_url}/tasks/by-idempotency/{idempotency_key}",
                        ),
                        stream=True,
                    )
                except httpx.TransportError:
                    payload = _reconcile_ambiguous_submission(
                        client=client,
                        api_url=self._api_url,
                        idempotency_key=idempotency_key,
                        allowed=submit_allowed,
                    )
                else:
                    lookup_status = lookup.status_code
                    if lookup_status == 200:
                        try:
                            payload = _closed_json(
                                lookup,
                                required={"task_id", "status_url", "result_url"},
                                allowed=submit_allowed,
                            )
                        except ParserOutputContractError:
                            lookup.close()
                            payload = _reconcile_ambiguous_submission(
                                client=client,
                                api_url=self._api_url,
                                idempotency_key=idempotency_key,
                                allowed=submit_allowed,
                            )
                        else:
                            lookup.close()
                    elif lookup_status == 404:
                        lookup.close()
                        request = client.build_request(
                            "POST",
                            f"{self._api_url}/tasks",
                            data=data,
                            files={
                                "files": (
                                    prepared_submission.upload_filename,
                                    source,
                                    "application/pdf",
                                )
                            },
                        )
                        try:
                            response = client.send(request, stream=True)
                        except httpx.TransportError:
                            payload = _reconcile_ambiguous_submission(
                                client=client,
                                api_url=self._api_url,
                                idempotency_key=idempotency_key,
                                allowed=submit_allowed,
                            )
                        else:
                            status = response.status_code
                            if status in {200, 202}:
                                try:
                                    payload = _closed_json(
                                        response,
                                        required={
                                            "task_id",
                                            "status_url",
                                            "result_url",
                                        },
                                        allowed=submit_allowed,
                                    )
                                except ParserOutputContractError:
                                    response.close()
                                    payload = _reconcile_ambiguous_submission(
                                        client=client,
                                        api_url=self._api_url,
                                        idempotency_key=idempotency_key,
                                        allowed=submit_allowed,
                                    )
                                else:
                                    response.close()
                            else:
                                response.close()
                                payload = _reconcile_ambiguous_submission(
                                    client=client,
                                    api_url=self._api_url,
                                    idempotency_key=idempotency_key,
                                    allowed=submit_allowed,
                                )
                    else:
                        lookup.close()
                        payload = _reconcile_ambiguous_submission(
                            client=client,
                            api_url=self._api_url,
                            idempotency_key=idempotency_key,
                            allowed=submit_allowed,
                        )
                try:
                    task = _task_from_submission_payload(
                        payload=payload,
                        api_url=self._api_url,
                        source_pdf_sha256=source_pdf_sha256,
                        attempt_identity=attempt_identity,
                        fence_identity=fence_identity,
                        idempotency_key=idempotency_key,
                        submission_epoch_unix=submission_identity.submission_epoch_unix,
                    )
                except ParserOutputContractError:
                    payload = _reconcile_ambiguous_submission(
                        client=client,
                        api_url=self._api_url,
                        idempotency_key=idempotency_key,
                        allowed=submit_allowed,
                    )
                    try:
                        task = _task_from_submission_payload(
                            payload=payload,
                            api_url=self._api_url,
                            source_pdf_sha256=source_pdf_sha256,
                            attempt_identity=attempt_identity,
                            fence_identity=fence_identity,
                            idempotency_key=idempotency_key,
                            submission_epoch_unix=(
                                submission_identity.submission_epoch_unix
                            ),
                        )
                    except ParserOutputContractError as exc:
                        raise SubmissionAcceptanceAmbiguous(
                            "MinerU staged HTTP contract: reconciled submission "
                            "identity remains ambiguous"
                        ) from exc
        except SubmissionAcceptanceAmbiguous:
            raise
        except Exception as exc:
            raise SubmissionAcceptanceAmbiguous(
                "MinerU staged HTTP contract: submission outcome remains ambiguous"
            ) from exc
        return MinerUHttpRemoteHandle(
            task=task,
            options=options,
            reader=self._reader,
            spool_root=self._spool_root,
            transport=self._transport,
        )

    def resume_remote_parse(
        self, *, receipt: RemoteArtifactReceipt, options: ParserOptions
    ) -> MinerUHttpRemoteHandle:
        task, spool_path, artifact_sha256 = _Task.from_token(receipt.resume_token)
        if (
            task.base_url != self._api_url
            or task.source_pdf_sha256 != receipt.source_pdf_sha256
            or task.attempt_identity != receipt.attempt_identity
            or task.fence_identity != receipt.fence_identity
        ):
            raise _fail("resume receipt drifted")
        terminal_spool = (
            (spool_path, artifact_sha256) if spool_path is not None else None
        )
        return MinerUHttpRemoteHandle(
            task=task,
            options=options,
            reader=self._reader,
            spool_root=self._spool_root,
            transport=self._transport,
            terminal_spool=terminal_spool,
        )

    def resume_submitted_parse(
        self,
        *,
        receipt: PersistedSubmissionReceipt,
        secret: PrivateSubmittedTaskResume | RecoveredV3ResumeSecret,
        options: ParserOptions,
    ) -> MinerUHttpRemoteHandle:
        if isinstance(secret, RecoveredV3ResumeSecret):
            if secret.attempt_identity != receipt.attempt_identity:
                raise _fail("recovered submitted secret attempt drifted")
            secret = secret.submitted_task_token()
        if hashlib.sha256(
            receipt.exact_bytes
        ).hexdigest() != receipt.sha256.removeprefix("sha256:") or hashlib.sha256(
            secret.token_bytes
        ).hexdigest() != secret.token_sha256.removeprefix("sha256:"):
            raise _fail("submitted checkpoint hash drifted")
        task, spool_path, artifact_sha256 = _Task.from_token(
            secret.token_bytes.decode("ascii")
        )
        expected, _ = task.submission_checkpoint()
        if receipt != expected or spool_path is not None or artifact_sha256 != "0" * 64:
            raise _fail("submitted checkpoint identity drifted")
        if task.base_url != self._api_url:
            raise _fail("submitted checkpoint API origin drifted")
        return MinerUHttpRemoteHandle(
            task=task,
            options=options,
            reader=self._reader,
            spool_root=self._spool_root,
            transport=self._transport,
        )


def _closed_json(
    response: httpx.Response,
    *,
    required: set[str],
    allowed: set[str] | None = None,
) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes(chunk_size=64 * 1024):
        total += len(chunk)
        if total > _MAX_WIRE_JSON_BYTES:
            raise _fail("response JSON exceeds the closed wire envelope")
        chunks.append(chunk)
    content = b"".join(chunks)

    def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise _fail("response JSON contains duplicate fields")
            value[key] = item
        return value

    try:
        payload = json.loads(
            content,
            object_pairs_hook=closed_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                _fail(f"response JSON contains non-finite value {value}")
            ),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise _fail("response is not JSON") from exc
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise _fail("response JSON shape is invalid")
    if allowed is not None and not set(payload).issubset(allowed):
        raise _fail("response JSON fields are not closed")
    return payload


def _reconcile_ambiguous_submission(
    *,
    client: httpx.Client,
    api_url: str,
    idempotency_key: str,
    allowed: set[str],
) -> dict[str, Any]:
    delays = (0.0, 0.01, 0.02, 0.04, 0.08, 0.16)
    last_reason = "not yet visible"
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            response = client.send(
                client.build_request(
                    "GET", f"{api_url}/tasks/by-idempotency/{idempotency_key}"
                ),
                stream=True,
            )
        except httpx.TransportError:
            last_reason = "transport failure"
            continue
        try:
            if response.status_code == 404:
                last_reason = "not yet visible"
                continue
            if response.status_code == 200:
                try:
                    return _closed_json(
                        response,
                        required={"task_id", "status_url", "result_url"},
                        allowed=allowed,
                    )
                except ParserOutputContractError:
                    last_reason = "invalid reconcile response"
                    continue
            if response.status_code in {408, 429} or 500 <= response.status_code <= 599:
                last_reason = f"HTTP {response.status_code}"
                continue
            last_reason = f"unexpected HTTP {response.status_code}"
        finally:
            response.close()
    raise SubmissionAcceptanceAmbiguous(
        "MinerU staged HTTP contract: submission acceptance remains ambiguous "
        f"after bounded reconcile ({last_reason})"
    )


def _task_from_submission_payload(
    *,
    payload: dict[str, Any],
    api_url: str,
    source_pdf_sha256: str,
    attempt_identity: str,
    fence_identity: str,
    idempotency_key: str,
    submission_epoch_unix: int,
) -> _Task:
    if not all(
        isinstance(payload.get(key), str)
        for key in ("task_id", "status_url", "result_url")
    ):
        raise _fail("submit identities are not strings")
    expected_identity = {
        "task_protocol_schema": "mineru-task-protocol.v2",
        "idempotency_key": idempotency_key,
        "attempt_identity": attempt_identity,
        "fence_identity": fence_identity,
    }
    if any(payload.get(key) != value for key, value in expected_identity.items()):
        raise _fail("submit/reconcile protocol identity drifted")
    return _Task(
        base_url=api_url,
        task_id=payload["task_id"],
        status_url=_same_origin_url(api_url, payload["status_url"], "status URL"),
        result_url=_same_origin_url(api_url, payload["result_url"], "result URL"),
        source_pdf_sha256=source_pdf_sha256,
        attempt_identity=attempt_identity,
        fence_identity=fence_identity,
        idempotency_key=idempotency_key,
        submission_epoch_unix=submission_epoch_unix,
        ack_nonce_hex=os.urandom(32).hex(),
    )


def _terminal_receipt_exact(receipt: RemoteArtifactReceipt) -> bytes:
    return encode_terminal_receipt(
        TerminalReceipt(
            attempt_identity=receipt.attempt_identity,
            fence_identity=receipt.fence_identity,
            source_pdf_sha256=receipt.source_pdf_sha256,
            artifact_owner_identity=receipt.artifact_owner_identity,
            artifact_byte_count=receipt.artifact_byte_count,
            artifact_sha256="sha256:" + receipt.artifact_sha256,
            resume_token_sha256="sha256:"
            + hashlib.sha256(receipt.resume_token.encode("ascii")).hexdigest(),
        )
    ).exact_bytes


def _inspect_zip(zip_path: Path) -> tuple[int, int, int, int]:
    digest = hashlib.sha256()
    compressed = 0
    with zip_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            compressed += len(chunk)
            if compressed > _MAX_RESULT_BYTES:
                raise _fail("ZIP exceeds compressed-byte envelope")
            digest.update(chunk)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            if len(members) > _MAX_ZIP_MEMBERS:
                raise _fail("ZIP exceeds member envelope")
            uncompressed = 0
            decoded = 0
            seen: set[str] = set()
            for member in members:
                _validate_zip_member(member, seen=seen)
                uncompressed += member.file_size
                if uncompressed > _MAX_UNCOMPRESSED_BYTES:
                    raise _fail("ZIP exceeds uncompressed-byte envelope")
                if Path(member.filename).suffix.lower() in {".json", ".md", ".txt"}:
                    # Parsing and JSON object graphs can amplify source text.
                    # Reserve a conservative 4x decoded-memory envelope.
                    decoded += member.file_size * 4
                    if decoded > _MAX_DECODED_BYTES:
                        raise _fail("ZIP exceeds decoded-byte envelope")
    except (zipfile.BadZipFile, OSError) as exc:
        raise _fail("retained result is not a readable ZIP") from exc
    return compressed, uncompressed, len(members), decoded


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_zip_member(member: zipfile.ZipInfo, *, seen: set[str]) -> None:
    pure = PurePosixPath(member.filename)
    key = member.filename.casefold()
    mode = member.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if (
        not pure.parts
        or pure.is_absolute()
        or ".." in pure.parts
        or key in seen
        or (file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))
    ):
        raise _fail("unsafe ZIP member")
    seen.add(key)


def _tree_file_receipts(root: Path) -> list[dict[str, object]]:
    receipts: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise _fail("materialized tree contains a symlink")
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        receipts.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": size,
                "sha256": digest.hexdigest(),
            }
        )
    return receipts


def _write_fsynced(path: Path, content: bytes) -> None:
    with path.open("xb") as sink:
        sink.write(content)
        sink.flush()
        os.fsync(sink.fileno())


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_closed_regular_file(
    path: Path, *, label: str, max_bytes: int
) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise _fail(f"{label} is unavailable") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
        ):
            raise _fail(f"{label} identity is unsafe")
        content = b""
        while chunk := os.read(fd, 64 * 1024):
            content += chunk
            if len(content) > max_bytes:
                raise _fail(f"{label} exceeds its closed byte count")
        after = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        if (
            _stat_identity(before) != _stat_identity(after)
            or _stat_identity(after) != _stat_identity(current)
        ):
            raise _fail(f"{label} path changed during verification")
        return content
    finally:
        os.close(fd)


def _closed_output_snapshot_bytes(
    root: Path, *, manifest_exact: bytes, manifest: dict[str, Any]
) -> dict[str, bytes]:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise _fail("materialization manifest lacks closed files")
    captured: dict[str, bytes] = {_MANIFEST_NAME: manifest_exact}
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise _fail("materialization manifest file receipt is invalid")
        relpath = item.get("path")
        expected_bytes = item.get("bytes")
        expected_sha256 = item.get("sha256")
        pure = PurePosixPath(relpath) if isinstance(relpath, str) else None
        if (
            pure is None
            or not pure.parts
            or pure.is_absolute()
            or ".." in pure.parts
            or relpath in captured
            or isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes < 0
            or not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_sha256)
        ):
            raise _fail("materialization manifest file receipt is invalid")
        assert isinstance(relpath, str)
        content = _read_closed_regular_file(
            root / Path(*pure.parts),
            label="materialized output file",
            max_bytes=expected_bytes,
        )
        if (
            len(content) != expected_bytes
            or hashlib.sha256(content).hexdigest() != expected_sha256
        ):
            raise _fail("materialized output file differs from its manifest")
        captured[relpath] = content
    return captured


def _write_closed_reader_snapshot(
    *, output_dir: Path, files: dict[str, bytes]
) -> tuple[Path, int]:
    snapshot = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.reader-", dir=output_dir.parent
        )
    )
    snapshot.chmod(0o700)
    try:
        for relpath, content in sorted(files.items()):
            pure = PurePosixPath(relpath)
            target = snapshot / Path(*pure.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_fsynced(target, content)
            target.chmod(0o600)
        _fsync_tree(snapshot)
        snapshot_fd = _open_owned_directory(
            snapshot, label="materialization reader snapshot"
        )
        return snapshot, snapshot_fd
    except BaseException:
        snapshot_fd = _open_owned_directory(
            snapshot, label="materialization reader snapshot"
        )
        try:
            _remove_exact_open_directory(
                snapshot,
                snapshot_fd,
                label="materialization reader snapshot",
            )
        finally:
            os.close(snapshot_fd)
        raise


def _read_from_closed_output_snapshot(
    *,
    reader: Any,
    output_dir: Path,
    source_pdf_sha256: str,
    manifest_exact: bytes,
    manifest: dict[str, Any],
) -> tuple[Any, Path]:
    captured = _closed_output_snapshot_bytes(
        output_dir, manifest_exact=manifest_exact, manifest=manifest
    )
    snapshot, snapshot_fd = _write_closed_reader_snapshot(
        output_dir=output_dir, files=captured
    )
    primary_failure: BaseException | None = None
    try:
        provider_document = reader.read(
            snapshot, source_pdf_sha256=source_pdf_sha256
        )
        artifact_root = reader.locate_artifact_root(snapshot)
        try:
            artifact_relpath = artifact_root.relative_to(snapshot)
        except ValueError as exc:
            raise _fail("reader artifact root escaped its closed snapshot") from exc
        _assert_directory_path(
            snapshot, snapshot_fd, label="materialization reader snapshot"
        )
        return provider_document, output_dir / artifact_relpath
    except BaseException as exc:
        primary_failure = exc
        raise
    finally:
        cleanup_failure: BaseException | None = None
        try:
            _remove_exact_open_directory(
                snapshot,
                snapshot_fd,
                label="materialization reader snapshot",
            )
        except BaseException as exc:
            cleanup_failure = exc
        finally:
            os.close(snapshot_fd)
        if primary_failure is None and cleanup_failure is not None:
            raise cleanup_failure


def _read_and_verify_manifest(
    root: Path, *, expected: dict[str, object], current_generation: int
) -> tuple[bytes, dict[str, Any]]:
    manifest_path = root / _MANIFEST_NAME
    exact = _read_closed_regular_file(
        manifest_path,
        label="materialization manifest",
        max_bytes=_MAX_WIRE_JSON_BYTES,
    )
    try:
        manifest = json.loads(exact)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail("materialization manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != set(expected) | {"files"}:
        raise _fail("materialization manifest shape drifted")
    immutable_keys = set(expected) - {"produced_generation"}
    if any(manifest.get(key) != expected[key] for key in immutable_keys):
        raise _fail("materialization manifest identity drifted")
    produced_generation = manifest.get("produced_generation")
    if (
        isinstance(produced_generation, bool)
        or not isinstance(produced_generation, int)
        or produced_generation < 1
        or produced_generation > current_generation
    ):
        raise _fail("materialization manifest claim generation drifted")
    files = manifest.get("files")
    if not isinstance(files, list) or files != _tree_file_receipts_excluding_manifest(
        root
    ):
        raise _fail("existing materialization output drifted")
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if exact != canonical:
        raise _fail("materialization manifest is not canonical")
    return exact, manifest


def _tree_file_receipts_excluding_manifest(root: Path) -> list[dict[str, object]]:
    return [
        item
        for item in _tree_file_receipts(root)
        if item["path"] not in {_MANIFEST_NAME, _INFLIGHT_MARKER_NAME}
    ]


def _safe_extract(zip_path: Path, output_dir: Path) -> None:
    root = output_dir.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        members = archive.infolist()
        if len(members) > _MAX_ZIP_MEMBERS:
            raise _fail("ZIP exceeds extraction envelope")
        seen: set[str] = set()
        written = 0
        for member in members:
            _validate_zip_member(member, seen=seen)
            pure = PurePosixPath(member.filename)
            target = (root / Path(*pure.parts)).resolve()
            if target != root and root not in target.parents:
                raise _fail("ZIP member escaped output root")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("xb") as sink:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > _MAX_UNCOMPRESSED_BYTES:
                            raise _fail("ZIP exceeded extraction byte envelope")
                        sink.write(chunk)


def _fsync_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as source:
                os.fsync(source.fileno())
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in (*reversed(directories), root):
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


__all__ = ["MinerUHttpRemoteHandle", "MinerUHttpStagedParser"]
