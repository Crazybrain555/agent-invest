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
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.ports.provider_parser import ProviderParserResult
from disclosure_anchor.application.ports.staged_provider_parser import (
    RemoteArtifactReceipt,
    RemoteProviderParseHandle,
)
from disclosure_anchor.domain.errors import ParserOutputContractError

_POLL_SECONDS = 1.0
_MAX_RESULT_BYTES = 4 * 1024 * 1024 * 1024
_MAX_ZIP_MEMBERS = 100_000
_MAX_UNCOMPRESSED_BYTES = 16 * 1024 * 1024 * 1024
_MAX_WIRE_JSON_BYTES = 1024 * 1024
_IDEMPOTENCY_BUCKET_SECONDS = 3600


def _make_idempotency_key(
    source_pdf_sha256: str,
    attempt_identity: str,
    fence_identity: str,
    *,
    observed_unix: float,
) -> str:
    bucket = int(observed_unix // _IDEMPOTENCY_BUCKET_SECONDS)
    digest = hashlib.sha256(
        f"{bucket:x}\0{source_pdf_sha256}\0{attempt_identity}\0{fence_identity}".encode()
    ).hexdigest()
    return f"{bucket:x}.{digest}"


def _fail(message: str) -> ParserOutputContractError:
    return ParserOutputContractError(f"MinerU staged HTTP contract: {message}")


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
    idempotency_key: str = ""
    task_protocol_v2: bool = False

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
                "spool_path": "" if spool_path is None else str(spool_path),
                "artifact_sha256": artifact_sha256,
                "task_protocol_v2": self.task_protocol_v2,
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
        if not isinstance(payload, dict) or payload.get("v") not in {1, 2, 3}:
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
        }
        if payload["v"] == 2:
            expected.add("task_protocol_v2")
        if payload["v"] == 3:
            expected.update({"task_protocol_v2", "idempotency_key"})
        if set(payload) != expected:
            raise _fail("invalid durable resume token shape")
        values = {
            key: payload[key]
            for key in payload
            if key not in {
                "v", "spool_path", "artifact_sha256", "task_protocol_v2",
                "idempotency_key",
            }
        }
        if not all(isinstance(value, str) for value in values.values()):
            raise _fail("invalid durable resume token values")
        protocol_v2 = payload.get("task_protocol_v2", False)
        if not isinstance(protocol_v2, bool):
            raise _fail("invalid durable resume token values")
        values["task_protocol_v2"] = protocol_v2
        values["idempotency_key"] = payload.get("idempotency_key", "")
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
        if self.idempotency_key:
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
                retained = self._retained_receipt(payload, client=client)
                if retained is not None:
                    return retained
                return self._spool_result(client)
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
                retained = self._retained_receipt(payload, client=client)
                if retained is not None:
                    return retained
                return self._spool_result(client)
            if status_value not in {"pending", "processing"}:
                raise _fail(f"remote task drained as {status_value!r}")
            time.sleep(_POLL_SECONDS)
        raise _fail("remote task did not drain before deadline")

    def _retained_receipt(
        self,
        payload: dict[str, Any],
        *,
        client: httpx.Client,
    ) -> RemoteArtifactReceipt | None:
        if "result_artifact_schema" not in payload:
            return None
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
        if self._task.task_protocol_v2:
            expected_idempotency = self._task.idempotency_key
            expected_protocol = {
                "task_protocol_schema": "mineru-task-protocol.v2",
                "protocol_state": "completed",
                "idempotency_key": expected_idempotency,
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

    def _spool_result(self, client: httpx.Client) -> RemoteArtifactReceipt:
        # Exact MinerU 3.4.4 creates a new temporary ZIP for every result GET.
        # Starlette's FileResponse ETag is therefore not an immutable result
        # version. Terminal credit is returned only after one result is spooled
        # and content-addressed locally.
        self._spool_root.mkdir(parents=True, exist_ok=True)
        owner_seed = hashlib.sha256(
            f"{self._task.base_url}\0{self._task.task_id}\0{self._task.source_pdf_sha256}\0"
            f"{self._task.attempt_identity}\0{self._task.fence_identity}".encode()
        ).hexdigest()
        part_fd, part_name = tempfile.mkstemp(
            prefix=f".{owner_seed}-",
            suffix=".zip.part",
            dir=self._spool_root,
        )
        os.close(part_fd)
        part_path = Path(part_name)
        final_path = self._spool_root / f"{owner_seed}.zip"
        digest = hashlib.sha256()
        byte_count = 0
        try:
            with client.stream("GET", self._task.result_url) as response:
                if response.status_code != 200:
                    raise _fail(f"result returned HTTP {response.status_code}")
                if "application/zip" not in response.headers.get("content-type", ""):
                    raise _fail("result is not application/zip")
                with part_path.open("wb") as sink:
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        byte_count += len(chunk)
                        if byte_count > _MAX_RESULT_BYTES:
                            raise _fail("result exceeds the closed spool envelope")
                        digest.update(chunk)
                        sink.write(chunk)
                    sink.flush()
                    os.fsync(sink.fileno())
        except BaseException:
            part_path.unlink(missing_ok=True)
            raise
        if not 0 < byte_count <= _MAX_RESULT_BYTES:
            raise _fail("result byte count is outside the closed envelope")
        artifact_sha256 = digest.hexdigest()
        os.replace(part_path, final_path)
        self._terminal_spool = (final_path, artifact_sha256)
        return RemoteArtifactReceipt(
            attempt_identity=self._task.attempt_identity,
            fence_identity=self._task.fence_identity,
            artifact_owner_identity=hashlib.sha256(
                f"{self._task.task_id}\0{artifact_sha256}\0{byte_count}".encode()
            ).hexdigest(),
            artifact_byte_count=byte_count,
            artifact_sha256=artifact_sha256,
            source_pdf_sha256=self._task.source_pdf_sha256,
            resume_token=self._task.token(
                spool_path=final_path,
                artifact_sha256=artifact_sha256,
            ),
        )

    def materialize(
        self,
        *,
        receipt: RemoteArtifactReceipt,
        output_dir: Path,
        source_pdf_sha256: str,
    ) -> ProviderParserResult:
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
        resolved_spool = spool_path.resolve() if spool_path is not None else None
        if (
            resolved_spool is not None
            and self._spool_root not in resolved_spool.parents
        ):
            raise _fail("spool path escaped its private root")
        if resolved_spool is None:
            resolved_spool = self._download_retained_result(receipt)
        digest = hashlib.sha256()
        received = 0
        with resolved_spool.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                received += len(chunk)
                digest.update(chunk)
        if (
            received != receipt.artifact_byte_count
            or digest.hexdigest() != receipt.artifact_sha256
        ):
            raise _fail("spooled result identity drifted")
        if output_dir.exists():
            raise _fail("materialization output must not already exist")
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{output_dir.name}-stage-", dir=output_dir.parent)
        )
        try:
            _safe_extract(resolved_spool, staging)
            _fsync_tree(staging)
            os.replace(staging, output_dir)
            parent_fd = os.open(output_dir.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            staging = output_dir.parent / f".{output_dir.name}-promoted"
        except BaseException:
            if spool_path is None:
                resolved_spool.unlink(missing_ok=True)
            raise
        finally:
            if staging.exists():
                for root, dirs, files in os.walk(staging, topdown=False):
                    for name in files:
                        Path(root, name).unlink(missing_ok=True)
                    for name in dirs:
                        Path(root, name).rmdir()
                staging.rmdir()
        try:
            provider_document = self._reader.read(
                output_dir, source_pdf_sha256=source_pdf_sha256
            )
        except BaseException:
            shutil.rmtree(output_dir, ignore_errors=True)
            if spool_path is None:
                resolved_spool.unlink(missing_ok=True)
            raise
        resolved_spool.unlink(missing_ok=True)
        target_identity = ParserTargetIdentity(
            name="MinerU",
            package_version="3.4.4",
            backend=self._options.backend,
            method=self._options.method,
            language=self._options.language,
            formula=self._options.formula,
            table=self._options.table,
            effort=self._options.effective_effort,
            image_analysis=self._options.effective_image_analysis,
            full_pdf=True,
            start_page=None,
            end_page=None,
            runtime_bundle_identity_sha256=self._options.runtime_bundle_identity_sha256
            or "",
        )
        return ProviderParserResult(
            target_identity=target_identity,
            artifact_root=self._reader.locate_artifact_root(output_dir),
            provider_document=provider_document,
        )

    def acknowledge_after_finish_committed(
        self, *, receipt: RemoteArtifactReceipt, checkpoint_state: str
    ) -> None:
        if checkpoint_state != "finish_committed":
            raise _fail("remote ACK requires a durable finish_committed checkpoint")
        task, _spool_path, artifact_sha256 = _Task.from_token(receipt.resume_token)
        if task != self._task or artifact_sha256 != receipt.artifact_sha256:
            raise _fail("remote ACK receipt ownership drifted")
        if not self._task.task_protocol_v2:
            return
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
        task_protocol_v2: bool = False,
        clock: Any = time.time,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._server_url = server_url
        self._reader = reader or MinerUMediumArtifactReader()
        self._transport = transport
        self._spool_root = spool_root
        self._task_protocol_v2 = task_protocol_v2
        self._clock = clock

    def begin_remote_parse(
        self,
        *,
        input_pdf: Path,
        options: ParserOptions,
        source_pdf_sha256: str,
        attempt_identity: str,
        fence_identity: str,
    ) -> MinerUHttpRemoteHandle:
        _identity(attempt_identity, "attempt identity")
        _identity(fence_identity, "fence identity")
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
        self._spool_root.mkdir(parents=True, exist_ok=True)
        source_fd = os.open(input_pdf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            os.close(source_fd)
            raise _fail("source PDF identity is unsafe")
        digest = hashlib.sha256()
        snapshot_fd, snapshot_name = tempfile.mkstemp(
            prefix=".upload-",
            suffix=".pdf",
            dir=self._spool_root,
        )
        snapshot = Path(snapshot_name)
        try:
            with os.fdopen(snapshot_fd, "wb") as target:
                while chunk := os.read(source_fd, 1024 * 1024):
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            try:
                os.close(snapshot_fd)
            except OSError:
                pass
            snapshot.unlink(missing_ok=True)
            raise
        finally:
            after = os.fstat(source_fd)
            os.close(source_fd)
        def identity(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                value.st_nlink, value.st_size, value.st_mtime_ns,
                value.st_ctime_ns,
            )
        if identity(before) != identity(after):
            snapshot.unlink(missing_ok=True)
            raise _fail("source changed while hashing")
        if "sha256:" + digest.hexdigest() != source_pdf_sha256:
            snapshot.unlink(missing_ok=True)
            raise _fail("source changed before submission")
        data: dict[str, Any] = {
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
            "server_url": self._server_url,
        }
        idempotency_key = _make_idempotency_key(
            source_pdf_sha256, attempt_identity, fence_identity,
            observed_unix=float(self._clock()),
        )
        if self._task_protocol_v2:
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
        reconciled = False
        try:
            with (
                httpx.Client(
                    timeout=httpx.Timeout(float(options.timeout_seconds or 300)),
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                ) as client,
                snapshot.open("rb") as source,
            ):
                try:
                    request = client.build_request(
                        "POST", f"{self._api_url}/tasks", data=data,
                        files={"files": (input_pdf.name, source, "application/pdf")},
                    )
                    response = client.send(request, stream=True)
                except httpx.TransportError:
                    if not self._task_protocol_v2:
                        raise
                    request = client.build_request(
                        "GET", f"{self._api_url}/tasks/by-idempotency/{idempotency_key}"
                    )
                    response = client.send(request, stream=True)
                    reconciled = True
                expected_statuses = (
                    {200} if reconciled
                    else ({200, 202} if self._task_protocol_v2 else {202})
                )
                try:
                    if response.status_code not in expected_statuses:
                        raise _fail(f"submit returned HTTP {response.status_code}")
                    payload = _closed_json(
                        response,
                        required={"task_id", "status_url", "result_url"},
                        allowed=submit_allowed if self._task_protocol_v2 else None,
                    )
                finally:
                    response.close()
        finally:
            snapshot.unlink(missing_ok=True)
        if not all(
            isinstance(payload[key], str)
            for key in ("task_id", "status_url", "result_url")
        ):
            raise _fail("submit identities are not strings")
        if self._task_protocol_v2:
            expected_identity = {
                "task_protocol_schema": "mineru-task-protocol.v2",
                "idempotency_key": idempotency_key,
                "attempt_identity": attempt_identity,
                "fence_identity": fence_identity,
            }
            if any(
                payload.get(key) != value for key, value in expected_identity.items()
            ):
                raise _fail("submit/reconcile protocol identity drifted")
        task = _Task(
            base_url=self._api_url,
            task_id=payload["task_id"],
            status_url=_same_origin_url(
                self._api_url, payload["status_url"], "status URL"
            ),
            result_url=_same_origin_url(
                self._api_url, payload["result_url"], "result URL"
            ),
            source_pdf_sha256=source_pdf_sha256,
            attempt_identity=attempt_identity,
            fence_identity=fence_identity,
            idempotency_key=idempotency_key if self._task_protocol_v2 else "",
            task_protocol_v2=self._task_protocol_v2,
        )
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


def _safe_extract(zip_path: Path, output_dir: Path) -> None:
    root = output_dir.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        members = archive.infolist()
        if (
            len(members) > _MAX_ZIP_MEMBERS
            or sum(m.file_size for m in members) > _MAX_UNCOMPRESSED_BYTES
        ):
            raise _fail("ZIP exceeds extraction envelope")
        seen: set[str] = set()
        for member in members:
            pure = PurePosixPath(member.filename)
            key = member.filename.casefold()
            mode = member.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or key in seen
                or (file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))
            ):
                raise _fail("unsafe ZIP member")
            seen.add(key)
            target = (root / Path(*pure.parts)).resolve()
            if target != root and root not in target.parents:
                raise _fail("ZIP member escaped output root")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("xb") as sink:
                    while chunk := source.read(1024 * 1024):
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
