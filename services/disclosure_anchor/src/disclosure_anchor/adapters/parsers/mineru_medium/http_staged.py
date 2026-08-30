"""Closed MinerU 3.4.4 task HTTP adapter.

Remote completion and local ZIP materialization are deliberately separate.
The opaque resume token is private checkpoint data; callers must not log it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Event, Lock
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import (
    MinerUMediumArtifactReader,
)
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.contracts.remote_parse_checkpoint import (
    TerminalReceipt,
    encode_terminal_receipt,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.ports.provider_parser import ProviderParserResult
from disclosure_anchor.application.ports.staged_provider_parser import (
    PersistedSubmissionReceipt,
    PreparedLocalSubmission,
    PreparedMaterialization,
    PreparedSubmissionIdentity,
    PrivateSubmittedTaskResume,
    ProviderMaterializationEvidence,
    RemoteArtifactReceipt,
    RemoteProviderParseHandle,
    StagedProviderParserResult,
    SubmissionAcceptanceAmbiguous,
)
from disclosure_anchor.domain.errors import ParserOutputContractError

_POLL_SECONDS = 1.0
_MAX_RESULT_BYTES = 4 * 1024 * 1024 * 1024
_MAX_ZIP_MEMBERS = 100_000
_MAX_UNCOMPRESSED_BYTES = 16 * 1024 * 1024 * 1024
_MAX_WIRE_JSON_BYTES = 1024 * 1024
_MANIFEST_NAME = ".agent-materialization-manifest.v1.json"
_MAX_DECODED_BYTES = 4 * 1024 * 1024 * 1024


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


def _verify_snapshot_fd(
    fd: int, *, expected_sha256: str
) -> tuple[os.stat_result, int]:
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
    observed, _ = _verify_snapshot_fd(
        snapshot_fd, expected_sha256=expected_sha256
    )
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


def _write_all(fd: int, chunk: bytes) -> None:
    remaining = memoryview(chunk)
    while remaining:
        written = os.write(fd, remaining)
        if written < 1:
            raise OSError("snapshot write made no progress")
        remaining = remaining[written:]


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
        }
        if set(payload) != expected:
            raise _fail("invalid durable resume token shape")
        values = {
            key: payload[key]
            for key in payload
            if key not in {
                "v", "spool_path", "artifact_sha256",
            }
        }
        if not all(
            isinstance(value, str) for key, value in values.items()
            if key != "submission_epoch_unix"
        ) or type(values.get("submission_epoch_unix")) is not int:
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
            not separator or not bucket
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
        if resolved_spool is not None and self._spool_root not in resolved_spool.parents:
            raise _fail("spool path escaped its private root")
        downloaded = resolved_spool is None
        if resolved_spool is None:
            resolved_spool = self._download_retained_result(receipt)
        try:
            compressed, uncompressed, members, decoded = _inspect_zip(resolved_spool)
            if compressed != receipt.artifact_byte_count:
                raise _fail("prepared compressed byte count drifted")
            if _sha256_file(resolved_spool) != receipt.artifact_sha256:
                raise _fail("prepared spool content drifted")
        except BaseException:
            if downloaded:
                resolved_spool.unlink(missing_ok=True)
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
            terminal_receipt_sha256="sha256:" + hashlib.sha256(terminal_exact).hexdigest(),
            spool_sha256="sha256:" + artifact_sha256,
            compressed_bytes=compressed,
            uncompressed_bytes=uncompressed,
            member_count=members,
            disk_bytes=uncompressed,
            decoded_bytes=decoded,
            private_token_bytes=token,
            private_token_sha256="sha256:" + hashlib.sha256(token).hexdigest(),
        )

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
        terminal_sha = "sha256:" + hashlib.sha256(
            _terminal_receipt_exact(receipt)
        ).hexdigest()
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
            if (
                (compressed, uncompressed, members, uncompressed, decoded)
                != (
                    prepared.compressed_bytes,
                    prepared.uncompressed_bytes,
                    prepared.member_count,
                    prepared.disk_bytes,
                    prepared.decoded_bytes,
                )
                or _sha256_file(resolved_spool) != receipt.artifact_sha256
            ):
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
        if output_dir.exists():
            if output_dir.is_symlink() or not output_dir.is_dir():
                raise _fail("existing output is not an owned directory")
            manifest_exact, manifest = _read_and_verify_manifest(
                output_dir,
                expected=manifest_projection,
                current_generation=producer_claim_generation,
            )
        else:
            output_dir.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".{output_dir.name}-{hashlib.sha256(task.attempt_identity.encode()).hexdigest()[:12]}-",
                    dir=output_dir.parent,
                )
            )
            promoted = False
            try:
                _safe_extract(resolved_spool, staging)
                files = _tree_file_receipts(staging)
                manifest = {**manifest_projection, "files": files}
                manifest_exact = json.dumps(
                    manifest, sort_keys=True, separators=(",", ":")
                ).encode()
                _write_fsynced(staging / _MANIFEST_NAME, manifest_exact)
                _fsync_tree(staging)
                os.replace(staging, output_dir)
                _fsync_directory(output_dir.parent)
                promoted = True
            finally:
                if not promoted and staging.exists():
                    shutil.rmtree(staging)
            manifest_exact, manifest = _read_and_verify_manifest(
                output_dir,
                expected=manifest_projection,
                current_generation=producer_claim_generation,
            )
        provider_document = self._reader.read(
            output_dir, source_pdf_sha256=source_pdf_sha256
        )
        resolved_spool.unlink(missing_ok=True)
        result = ProviderParserResult(
            target_identity=target_identity,
            artifact_root=self._reader.locate_artifact_root(output_dir),
            provider_document=provider_document,
        )
        artifact_root = self._reader.locate_artifact_root(output_dir)
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
        self, *, receipt: RemoteArtifactReceipt, checkpoint_state: str
    ) -> None:
        if checkpoint_state != "finish_committed":
            raise _fail("remote ACK requires a durable finish_committed checkpoint")
        task, _spool_path, artifact_sha256 = _Task.from_token(receipt.resume_token)
        if task != self._task or artifact_sha256 != receipt.artifact_sha256:
            raise _fail("remote ACK receipt ownership drifted")
        self._ack_terminal()

    def acknowledge_after_failure_committed(self, *, checkpoint_state: str) -> None:
        if checkpoint_state not in {
            "remote_failure_committed",
            "local_failure_committed",
        }:
            raise _fail(
                "remote failure ACK requires a durable remote_failure_committed "
                "or local_failure_committed checkpoint"
            )
        self._ack_terminal()

    def _ack_terminal(self) -> None:
        with self._client(30.0) as client:
            with client.stream(
                "POST", f"{self._task.base_url}/tasks/{self._task.task_id}/ack"
            ) as response:
                if response.status_code not in {200, 204}:
                    raise _fail(
                        f"result acknowledgement returned HTTP {response.status_code}"
                    )
                payload = None if response.status_code == 204 else _closed_json(
                    response,
                    required={"schema", "task_id", "status"},
                    allowed={"schema", "task_id", "status"},
                )
            if payload is not None and (
                set(payload) != {"schema", "task_id", "status"} or payload != {
                    "schema": "mineru-task-protocol.v2",
                    "task_id": self._task.task_id,
                    "status": "consumed",
                }
            ):
                raise _fail("result acknowledgement identity drifted")

    def _download_retained_result(self, receipt: RemoteArtifactReceipt) -> Path:
        self._spool_root.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".retained-", suffix=".zip", dir=self._spool_root
        )
        os.close(fd)
        path = Path(temp_name)
        digest = hashlib.sha256()
        received = 0
        try:
            with (
                self._client(float(self._options.timeout_seconds or 600)) as client,
                client.stream("GET", self._task.result_url) as response,
            ):
                if (
                    response.status_code != 200
                    or "application/zip" not in response.headers.get("content-type", "")
                ):
                    raise _fail("retained result is unavailable")
                if (
                    response.headers.get("x-mineru-result-sha256")
                    != receipt.artifact_sha256
                    or response.headers.get("x-mineru-result-owner")
                    != receipt.artifact_owner_identity
                ):
                    raise _fail("retained result headers drifted")
                with path.open("wb") as sink:
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        received += len(chunk)
                        if received > receipt.artifact_byte_count:
                            raise _fail("retained result exceeded attested bytes")
                        digest.update(chunk)
                        sink.write(chunk)
                    sink.flush()
                    os.fsync(sink.fileno())
            if (
                received != receipt.artifact_byte_count
                or digest.hexdigest() != receipt.artifact_sha256
            ):
                raise _fail("retained result content drifted")
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise

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
        self._spool_root = spool_root

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
            runtime_bundle_identity_sha256=options.runtime_bundle_identity_sha256
            or "",
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
        self._spool_root.mkdir(parents=True, exist_ok=True)
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
        os.lseek(source_fd, 0, os.SEEK_SET)
        snapshot = self._spool_root / _derived_snapshot_name(identity)
        created = False
        try:
            try:
                snapshot_fd = os.open(
                    snapshot,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except FileExistsError:
                snapshot_fd = os.open(
                    snapshot, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    _validate_snapshot_stat(os.fstat(snapshot_fd))
                    try:
                        snapshot_stat, _ = _verify_snapshot_fd(
                            snapshot_fd,
                            expected_sha256=identity.source_pdf_sha256,
                        )
                    except _SnapshotContentDrift:
                        os.close(snapshot_fd)
                        snapshot_fd = -1
                        _unlink_owned_snapshot(snapshot, expected=None)
                        _fsync_directory(self._spool_root)
                        snapshot_fd = os.open(
                            snapshot,
                            os.O_RDWR
                            | os.O_CREAT
                            | os.O_EXCL
                            | getattr(os, "O_NOFOLLOW", 0),
                            0o600,
                        )
                        created = True
                        snapshot_stat = _write_snapshot_from_source(
                            source_fd=source_fd,
                            snapshot_fd=snapshot_fd,
                            expected_sha256=identity.source_pdf_sha256,
                        )
                finally:
                    if snapshot_fd >= 0:
                        os.close(snapshot_fd)
            else:
                created = True
                try:
                    snapshot_stat = _write_snapshot_from_source(
                        source_fd=source_fd,
                        snapshot_fd=snapshot_fd,
                        expected_sha256=identity.source_pdf_sha256,
                    )
                except BaseException:
                    os.close(snapshot_fd)
                    _unlink_owned_snapshot(snapshot, expected=None)
                    raise
                else:
                    os.close(snapshot_fd)
            after = os.fstat(source_fd)
        finally:
            os.close(source_fd)
        if _stat_identity(before) != _stat_identity(after):
            if created:
                _unlink_owned_snapshot(snapshot, expected=None)
            raise _fail("source changed while preparing upload snapshot")
        _fsync_directory(self._spool_root)
        return PreparedLocalSubmission(
            identity=identity,
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
            upload_filename=f"{identity.source_pdf_sha256[7:]}.pdf",
        )

    def discard_local_submission(
        self,
        *,
        prepared_submission: PreparedLocalSubmission,
        checkpoint_state: str,
    ) -> None:
        if checkpoint_state not in {
            "submitted",
            "pre_submission_failed",
            "remote_failure_committed",
            "local_failure_committed",
        }:
            raise _fail("snapshot discard requires an exact durable checkpoint state")
        expected_path = self._spool_root / _derived_snapshot_name(
            prepared_submission.identity
        )
        if prepared_submission.snapshot_path != expected_path:
            raise _fail("snapshot discard path drifted")
        _unlink_owned_snapshot(expected_path, expected=prepared_submission)
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
            snapshot_fd = os.open(
                snapshot, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
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
            "task_id", "status", "backend", "file_names", "created_at",
            "started_at", "completed_at", "error", "status_url", "result_url",
            "queued_ahead", "task_protocol_schema", "idempotency_key",
            "attempt_identity", "fence_identity", "result_artifact_schema",
            "result_artifact_sha256", "result_artifact_bytes",
            "result_artifact_owner", "protocol_state",
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
                            "POST", f"{self._api_url}/tasks", data=data,
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
                                        required={"task_id", "status_url", "result_url"},
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
        secret: PrivateSubmittedTaskResume,
        options: ParserOptions,
    ) -> MinerUHttpRemoteHandle:
        if (
            hashlib.sha256(receipt.exact_bytes).hexdigest()
            != receipt.sha256.removeprefix("sha256:")
            or hashlib.sha256(secret.token_bytes).hexdigest()
            != secret.token_sha256.removeprefix("sha256:")
        ):
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


def _read_and_verify_manifest(
    root: Path, *, expected: dict[str, object], current_generation: int
) -> tuple[bytes, dict[str, Any]]:
    manifest_path = root / _MANIFEST_NAME
    try:
        exact = manifest_path.read_bytes()
    except OSError as exc:
        raise _fail("existing output has no closed materialization manifest") from exc
    if len(exact) > _MAX_WIRE_JSON_BYTES:
        raise _fail("materialization manifest exceeds the closed envelope")
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
    if not isinstance(files, list) or files != _tree_file_receipts_excluding_manifest(root):
        raise _fail("existing materialization output drifted")
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if exact != canonical:
        raise _fail("materialization manifest is not canonical")
    return exact, manifest


def _tree_file_receipts_excluding_manifest(root: Path) -> list[dict[str, object]]:
    return [
        item
        for item in _tree_file_receipts(root)
        if item["path"] != _MANIFEST_NAME
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
