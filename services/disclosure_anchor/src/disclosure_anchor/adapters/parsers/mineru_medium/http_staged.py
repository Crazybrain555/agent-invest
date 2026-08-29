"""Closed MinerU 3.4.4 task HTTP adapter.

Remote completion and local ZIP materialization are deliberately separate.
The opaque resume token is private checkpoint data; callers must not log it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Event
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

    def token(self, *, spool_path: Path, artifact_sha256: str) -> str:
        raw = json.dumps(
            {
                "v": 1,
                "base_url": self.base_url,
                "task_id": self.task_id,
                "status_url": self.status_url,
                "result_url": self.result_url,
                "source_pdf_sha256": self.source_pdf_sha256,
                "attempt_identity": self.attempt_identity,
                "fence_identity": self.fence_identity,
                "spool_path": str(spool_path),
                "artifact_sha256": artifact_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(raw).decode()

    @classmethod
    def from_token(cls, token: str) -> tuple[_Task, Path, str]:
        try:
            payload = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
        except (ValueError, json.JSONDecodeError) as exc:
            raise _fail("invalid durable resume token") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "v", "base_url", "task_id", "status_url", "result_url",
            "source_pdf_sha256", "attempt_identity", "fence_identity",
            "spool_path", "artifact_sha256",
        } or payload["v"] != 1:
            raise _fail("invalid durable resume token shape")
        values = {
            key: payload[key]
            for key in payload
            if key not in {"v", "spool_path", "artifact_sha256"}
        }
        if not all(isinstance(value, str) for value in values.values()):
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
        return task, Path(spool_path), artifact_sha256

    def validate(self) -> None:
        _identity(self.task_id, "task id")
        _identity(self.attempt_identity, "attempt identity")
        _identity(self.fence_identity, "fence identity")
        if len(self.source_pdf_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.source_pdf_sha256
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

    def _client(self, timeout: float) -> httpx.Client:
        # MinerU is a private LAN/Tailnet endpoint. Never inherit proxy env.
        return httpx.Client(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        )

    def wait_terminal(self) -> RemoteArtifactReceipt:
        timeout = float(self._options.timeout_seconds or 3600)
        deadline = time.monotonic() + timeout
        with self._client(min(30.0, timeout)) as client:
            while time.monotonic() < deadline:
                response = client.get(self._task.status_url)
                if response.status_code != 200:
                    raise _fail(f"status returned HTTP {response.status_code}")
                payload = _closed_json(response, required={"status"})
                status_value = payload["status"]
                if status_value in {"pending", "processing"}:
                    if self._stop.wait(min(_POLL_SECONDS, max(0.0, deadline-time.monotonic()))):
                        return self._drain(client, deadline)
                    continue
                if status_value != "completed":
                    raise _fail(f"remote task terminated as {status_value!r}")
                return self._spool_result(client)
        raise _fail("remote task deadline expired")

    def _drain(self, client: httpx.Client, deadline: float) -> RemoteArtifactReceipt:
        drain_deadline = max(
            deadline,
            time.monotonic() + float(self._options.api_drain_timeout_seconds),
        )
        while time.monotonic() < drain_deadline:
            response = client.get(self._task.status_url)
            if response.status_code != 200:
                raise _fail(f"drain status returned HTTP {response.status_code}")
            status_value = _closed_json(response, required={"status"})["status"]
            if status_value == "completed":
                return self._spool_result(client)
            if status_value not in {"pending", "processing"}:
                raise _fail(f"remote task drained as {status_value!r}")
            time.sleep(_POLL_SECONDS)
        raise _fail("remote task did not drain before deadline")

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
        part_path = self._spool_root / f".{owner_seed}.zip.part"
        final_path = self._spool_root / f"{owner_seed}.zip"
        digest = hashlib.sha256()
        byte_count = 0
        with client.stream("GET", self._task.result_url) as response:
            if response.status_code != 200:
                raise _fail(f"result returned HTTP {response.status_code}")
            if "application/zip" not in response.headers.get("content-type", ""):
                raise _fail("result is not application/zip")
            with part_path.open("wb") as sink:
                for chunk in response.iter_bytes():
                    byte_count += len(chunk)
                    if byte_count > _MAX_RESULT_BYTES:
                        raise _fail("result exceeds the closed spool envelope")
                    digest.update(chunk)
                    sink.write(chunk)
                sink.flush()
                os.fsync(sink.fileno())
        if not 0 < byte_count <= _MAX_RESULT_BYTES:
            raise _fail("result byte count is outside the closed envelope")
        artifact_sha256 = digest.hexdigest()
        os.replace(part_path, final_path)
        self._terminal_spool = (final_path, artifact_sha256)
        return RemoteArtifactReceipt(
            attempt_identity=self._task.attempt_identity,
            fence_identity=self._task.fence_identity,
            artifact_owner_identity=hashlib.sha256(
                f"{owner_seed}\0{artifact_sha256}".encode()
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
        self, *, receipt: RemoteArtifactReceipt, output_dir: Path,
        source_pdf_sha256: str,
    ) -> ProviderParserResult:
        if source_pdf_sha256 != self._task.source_pdf_sha256:
            raise _fail("receipt/source ownership drifted before materialization")
        task, spool_path, artifact_sha256 = _Task.from_token(receipt.resume_token)
        if task != self._task or artifact_sha256 != receipt.artifact_sha256:
            raise _fail("receipt ownership drifted before materialization")
        resolved_spool = spool_path.resolve()
        if self._spool_root not in resolved_spool.parents:
            raise _fail("spool path escaped its private root")
        digest = hashlib.sha256()
        received = 0
        with resolved_spool.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                received += len(chunk)
                digest.update(chunk)
        if received != receipt.artifact_byte_count or digest.hexdigest() != receipt.artifact_sha256:
            raise _fail("spooled result identity drifted")
        if output_dir.exists():
            raise _fail("materialization output must not already exist")
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-stage-", dir=output_dir.parent))
        try:
            _safe_extract(resolved_spool, staging)
            _fsync_tree(staging)
            os.replace(staging, output_dir)
            staging = output_dir.parent / f".{output_dir.name}-promoted"
        finally:
            if staging.exists():
                for root, dirs, files in os.walk(staging, topdown=False):
                    for name in files:
                        Path(root, name).unlink(missing_ok=True)
                    for name in dirs:
                        Path(root, name).rmdir()
                staging.rmdir()
        provider_document = self._reader.read(output_dir, source_pdf_sha256=source_pdf_sha256)
        target_identity = ParserTargetIdentity(
            name="MinerU", package_version="3.4.4", backend=self._options.backend,
            method=self._options.method, language=self._options.language,
            formula=self._options.formula, table=self._options.table,
            effort=self._options.effective_effort,
            image_analysis=self._options.effective_image_analysis,
            full_pdf=True, start_page=None, end_page=None,
            runtime_bundle_identity_sha256=self._options.runtime_bundle_identity_sha256 or "",
        )
        return ProviderParserResult(target_identity=target_identity, artifact_root=self._reader.locate_artifact_root(output_dir), provider_document=provider_document)

    def cancel_and_drain(self) -> None:
        self._stop.set()
        # MinerU 3.4.4 exposes no cancel endpoint. The caller must not regain
        # admission merely because local waiting was cancelled.
        self.wait_terminal()


class MinerUHttpStagedParser:
    def __init__(self, *, api_url: str, server_url: str, spool_root: Path, reader: MinerUMediumArtifactReader | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self._api_url = api_url.rstrip("/")
        self._server_url = server_url
        self._reader = reader or MinerUMediumArtifactReader()
        self._transport = transport
        self._spool_root = spool_root

    def begin_remote_parse(self, *, input_pdf: Path, options: ParserOptions, source_pdf_sha256: str, attempt_identity: str, fence_identity: str) -> MinerUHttpRemoteHandle:
        before = input_pdf.stat()
        digest = hashlib.sha256()
        with input_pdf.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        after = input_pdf.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise _fail("source changed while hashing")
        if digest.hexdigest() != source_pdf_sha256:
            raise _fail("source changed before submission")
        data: dict[str, Any] = {
            "lang_list": options.language, "backend": options.backend,
            "effort": options.effective_effort or "medium", "parse_method": options.method,
            "formula_enable": str(options.formula).lower(), "table_enable": str(options.table).lower(),
            "image_analysis": str(options.effective_image_analysis).lower(), "return_md": "true",
            "return_middle_json": "true", "return_model_output": "true",
            "return_content_list": "true", "return_images": "true",
            "response_format_zip": "true", "return_original_file": "true",
            "client_side_output_generation": "false", "start_page_id": "0",
            "end_page_id": "99999", "server_url": self._server_url,
        }
        with (
            httpx.Client(timeout=httpx.Timeout(float(options.timeout_seconds or 300)), follow_redirects=False, trust_env=False, transport=self._transport) as client,
            input_pdf.open("rb") as source,
        ):
            response = client.post(f"{self._api_url}/tasks", data=data, files={"files": (input_pdf.name, source, "application/pdf")})
        if response.status_code != 202:
            raise _fail(f"submit returned HTTP {response.status_code}")
        payload = _closed_json(response, required={"task_id", "status_url", "result_url"})
        if not all(isinstance(payload[key], str) for key in ("task_id", "status_url", "result_url")):
            raise _fail("submit identities are not strings")
        task = _Task(base_url=self._api_url, task_id=payload["task_id"], status_url=_same_origin_url(self._api_url, payload["status_url"], "status URL"), result_url=_same_origin_url(self._api_url, payload["result_url"], "result URL"), source_pdf_sha256=source_pdf_sha256, attempt_identity=attempt_identity, fence_identity=fence_identity)
        return MinerUHttpRemoteHandle(task=task, options=options, reader=self._reader, spool_root=self._spool_root, transport=self._transport)

    def resume_remote_parse(self, *, receipt: RemoteArtifactReceipt, options: ParserOptions) -> MinerUHttpRemoteHandle:
        task, spool_path, artifact_sha256 = _Task.from_token(receipt.resume_token)
        if task.base_url != self._api_url or task.source_pdf_sha256 != receipt.source_pdf_sha256 or task.attempt_identity != receipt.attempt_identity or task.fence_identity != receipt.fence_identity:
            raise _fail("resume receipt drifted")
        return MinerUHttpRemoteHandle(task=task, options=options, reader=self._reader, spool_root=self._spool_root, transport=self._transport, terminal_spool=(spool_path, artifact_sha256))


def _closed_json(response: httpx.Response, *, required: set[str]) -> dict[str, Any]:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise _fail("response is not JSON") from exc
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise _fail("response JSON shape is invalid")
    return payload


def _safe_extract(zip_path: Path, output_dir: Path) -> None:
    root = output_dir.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        members = archive.infolist()
        if len(members) > _MAX_ZIP_MEMBERS or sum(m.file_size for m in members) > _MAX_UNCOMPRESSED_BYTES:
            raise _fail("ZIP exceeds extraction envelope")
        seen: set[str] = set()
        for member in members:
            pure = PurePosixPath(member.filename)
            key = member.filename.casefold()
            mode = member.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if pure.is_absolute() or ".." in pure.parts or key in seen or (
                file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
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
    directory_fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


__all__ = ["MinerUHttpRemoteHandle", "MinerUHttpStagedParser"]
