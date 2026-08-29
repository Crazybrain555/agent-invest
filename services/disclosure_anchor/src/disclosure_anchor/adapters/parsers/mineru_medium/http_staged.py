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

    def token(self) -> str:
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
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(raw).decode()

    @classmethod
    def from_token(cls, token: str) -> _Task:
        try:
            payload = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
        except (ValueError, json.JSONDecodeError) as exc:
            raise _fail("invalid durable resume token") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "v", "base_url", "task_id", "status_url", "result_url",
            "source_pdf_sha256", "attempt_identity", "fence_identity",
        } or payload["v"] != 1:
            raise _fail("invalid durable resume token shape")
        values = {key: payload[key] for key in payload if key != "v"}
        if not all(isinstance(value, str) for value in values.values()):
            raise _fail("invalid durable resume token values")
        task = cls(**values)
        task.validate()
        return task

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
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        task.validate()
        self._task = task
        self._options = options
        self._reader = reader
        self._transport = transport
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
                return self._owned_result_receipt(client)
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
                return self._owned_result_receipt(client)
            if status_value not in {"pending", "processing"}:
                raise _fail(f"remote task drained as {status_value!r}")
            time.sleep(_POLL_SECONDS)
        raise _fail("remote task did not drain before deadline")

    def _owned_result_receipt(self, client: httpx.Client) -> RemoteArtifactReceipt:
        # FileResponse exposes Content-Length. Opening/closing the stream proves
        # ownership without materializing the ZIP in the remote stage.
        with client.stream("GET", self._task.result_url) as response:
            if response.status_code != 200:
                raise _fail(f"result returned HTTP {response.status_code}")
            if "application/zip" not in response.headers.get("content-type", ""):
                raise _fail("result is not application/zip")
            try:
                byte_count = int(response.headers["content-length"])
            except (KeyError, ValueError) as exc:
                raise _fail("result has no valid Content-Length") from exc
        if not 0 < byte_count <= _MAX_RESULT_BYTES:
            raise _fail("result byte count is outside the closed envelope")
        owner = hashlib.sha256(
            f"{self._task.base_url}\0{self._task.task_id}\0{self._task.result_url}\0"
            f"{self._task.source_pdf_sha256}\0{self._task.attempt_identity}\0"
            f"{self._task.fence_identity}".encode()
        ).hexdigest()
        return RemoteArtifactReceipt(
            attempt_identity=self._task.attempt_identity,
            fence_identity=self._task.fence_identity,
            artifact_owner_identity=owner,
            artifact_byte_count=byte_count,
            source_pdf_sha256=self._task.source_pdf_sha256,
            resume_token=self._task.token(),
        )

    def materialize(
        self, *, receipt: RemoteArtifactReceipt, output_dir: Path,
        source_pdf_sha256: str,
    ) -> ProviderParserResult:
        with self._client(30.0) as ownership_client:
            expected = self._owned_result_receipt(ownership_client)
        if receipt != expected or source_pdf_sha256 != self._task.source_pdf_sha256:
            raise _fail("receipt/source ownership drifted before materialization")
        output_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".mineru-result-", suffix=".zip", dir=output_dir)
        os.close(fd)
        zip_path = Path(temp_name)
        received = 0
        try:
            with (
                self._client(float(self._options.timeout_seconds or 600)) as client,
                client.stream("GET", self._task.result_url) as response,
            ):
                    if response.status_code != 200 or "application/zip" not in response.headers.get("content-type", ""):
                        raise _fail("result changed during materialization")
                    with zip_path.open("wb") as zip_sink:
                        for chunk in response.iter_bytes():
                            received += len(chunk)
                            if received > receipt.artifact_byte_count:
                                raise _fail("result exceeded attested byte count")
                            zip_sink.write(chunk)
            if received != receipt.artifact_byte_count:
                raise _fail("result byte count drifted")
            _safe_extract(zip_path, output_dir)
        finally:
            zip_path.unlink(missing_ok=True)
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
    def __init__(self, *, api_url: str, server_url: str, reader: MinerUMediumArtifactReader | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self._api_url = api_url.rstrip("/")
        self._server_url = server_url
        self._reader = reader or MinerUMediumArtifactReader()
        self._transport = transport

    def begin_remote_parse(self, *, input_pdf: Path, options: ParserOptions, source_pdf_sha256: str, attempt_identity: str, fence_identity: str) -> MinerUHttpRemoteHandle:
        if hashlib.sha256(input_pdf.read_bytes()).hexdigest() != source_pdf_sha256:
            raise _fail("source changed before submission")
        data: dict[str, Any] = {
            "lang_list": options.language, "backend": options.backend,
            "effort": options.effort, "parse_method": options.method,
            "formula_enable": str(options.formula).lower(), "table_enable": str(options.table).lower(),
            "image_analysis": str(options.image_analysis).lower(), "return_md": "true",
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
        return MinerUHttpRemoteHandle(task=task, options=options, reader=self._reader, transport=self._transport)

    def resume_remote_parse(self, *, receipt: RemoteArtifactReceipt, options: ParserOptions) -> MinerUHttpRemoteHandle:
        task = _Task.from_token(receipt.resume_token)
        if task.base_url != self._api_url or task.source_pdf_sha256 != receipt.source_pdf_sha256 or task.attempt_identity != receipt.attempt_identity or task.fence_identity != receipt.fence_identity:
            raise _fail("resume receipt drifted")
        return MinerUHttpRemoteHandle(task=task, options=options, reader=self._reader, transport=self._transport)


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
            if pure.is_absolute() or ".." in pure.parts or key in seen or (mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
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


__all__ = ["MinerUHttpRemoteHandle", "MinerUHttpStagedParser"]
