"""Independent E1 protocol fixtures; synthetic bytes do not qualify real PDFs.

The provider artifact builder is an unchanged pre-M6 baseline dependency. The
stateful transport below models remote effects separately from delivery of their
responses, so losing a response cannot silently erase the server-side task.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import re
from typing import Any
from unittest.mock import patch
import zipfile

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    canonical_result_owner_v2,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournal
from disclosure_anchor.application.ports.parser import ParserOptions
from tests.unit.test_mineru_medium_artifacts import _write_bundle


RUNTIME_SHA = "sha256:" + "b" * 64
CLOCK_SHA = "sha256:" + "c" * 64
API = "http://mineru.invalid"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def minimal_pdf(page_count: int) -> bytes:
    """Actual blank-page PDF structure for physical observation, not semantic evidence."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{index + 3} 0 R" for index in range(page_count))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode())
    objects.extend(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>" for _ in range(page_count))
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(data)


def journal_records(root: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_bytes()) for path in sorted(root.glob("[0-9][0-9][0-9][0-9]-*.json"))]


class SimulatedCrash(BaseException):
    """A deterministic process-stop seam, never a recoverable provider failure."""


@contextmanager
def crash_after_phase(phase: str) -> Iterator[None]:
    original = DiagnosticJournal.append

    def append(journal: DiagnosticJournal, step: str, value: dict[str, Any]) -> Any:
        result = original(journal, step, value)
        if step == phase:
            raise SimulatedCrash(phase)
        return result

    with patch.object(DiagnosticJournal, "append", append):
        yield


class ChunkStream(httpx.SyncByteStream):
    def __init__(self, data: bytes, checkpoint: Callable[[int], None] | None = None) -> None:
        self.data = data
        self.checkpoint = checkpoint
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for index in range(0, len(self.data), 997):
            if self.checkpoint is not None:
                self.checkpoint(index)
            yield self.data[index:index + 997]

    def close(self) -> None:
        self.closed = True


class LifecycleFixture:
    def __init__(self, root: Path, *, unsafe_member: str | None = None, source_pages: int = 2,
                 malformed_source: bool = False) -> None:
        self.root = root
        self.source = root / "source-input.pdf"
        self.source.write_bytes(b"not a PDF; malformed source fixture\n" if malformed_source else minimal_pdf(source_pages))
        self.source_bytes = self.source.read_bytes()
        self.source_sha = digest(self.source_bytes)
        self.journal = root / "attempt"
        bundle = root / "provider-fixture"
        bundle.mkdir()
        _write_bundle(bundle)
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, "w", compression=zipfile.ZIP_STORED) as archive:
            for file in sorted(bundle.rglob("*")):
                if file.is_file():
                    name = file.relative_to(bundle).as_posix().replace(
                        "sha256_" + "a" * 64, self.source_sha.replace(":", "_"),
                    )
                    archive.writestr(name, file.read_bytes())
            if unsafe_member is not None:
                archive.writestr(unsafe_member, b"foreign escaped payload")
        self.archive = zipped.getvalue()
        self.archive_sha = hashlib.sha256(self.archive).hexdigest()
        self.task_id = "independent-task-1"
        self.owner = canonical_result_owner_v2(
            task_id=self.task_id, artifact_sha256=self.archive_sha, artifact_byte_count=len(self.archive),
        )
        self.now_ns = 10_000_000_000
        self.deadline_ns = 70_000_000_000
        self.fields: dict[str, str] = {}
        self.events: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.uploaded_bodies: list[bytes] = []
        self.responses: list[tuple[int, bytes]] = []
        self.streams: list[ChunkStream] = []
        self.task_exists = False
        self.ack_effects = 0
        self.terminal_status = "completed"
        self.pending_polls = 0
        self.lose_submit_response = False
        self.lookup_absent = False
        self.ack_loss = ""
        self.absence_payload: object = {"detail": "Task not found"}
        self.response_mutator: Callable[[dict[str, object]], None] | None = None
        self.before_request: Callable[[httpx.Request], None] | None = None
        self.download_checkpoint: Callable[[int], None] | None = None
        self.download_headers: dict[str, str] = {}
        self.lease_expiry = 2_000
        self.task_response_padding = 0

    def response(self, status: int, payload: object) -> httpx.Response:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if isinstance(payload, dict) and "task_protocol_schema" in payload:
            raw += b" " * self.task_response_padding
        self.responses.append((status, raw))
        stream = ChunkStream(raw)
        self.streams.append(stream)
        return httpx.Response(status, stream=stream)

    def payload(self, status: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "task_protocol_schema": "mineru-task-protocol.v2",
            "task_id": self.task_id,
            "status": status,
            "protocol_state": status,
            "status_url": API + "/tasks/" + self.task_id,
            "result_url": API + "/tasks/" + self.task_id + "/result",
            "idempotency_key": self.fields["agent_idempotency_key"],
            "attempt_identity": self.fields["agent_attempt_identity"],
            "fence_identity": self.fields["agent_fence_identity"],
            "result_artifact_schema": "mineru-retained-result.v1" if status == "completed" else None,
            "result_artifact_sha256": self.archive_sha if status == "completed" else None,
            "result_artifact_bytes": len(self.archive) if status == "completed" else None,
            "result_artifact_owner": self.owner if status == "completed" else None,
        }
        if status == "failed":
            payload["error"] = "synthetic parser failure retained verbatim"
        if self.response_mutator is not None:
            self.response_mutator(payload)
        return payload

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.events.append((method, path))
        self.requests.append(request)
        if self.before_request is not None:
            self.before_request(request)
        if method == "POST" and path == "/tasks":
            body = request.read()
            self.uploaded_bodies.append(body)
            for name in ("agent_idempotency_key", "agent_attempt_identity", "agent_fence_identity"):
                match = re.search(rb'name="' + name.encode() + rb'"\r\n\r\n([^\r]+)', body)
                if match is None:
                    raise AssertionError("required multipart identity omitted: " + name)
                self.fields[name] = match.group(1).decode()
            self.task_exists = True
            if self.lose_submit_response:
                raise httpx.ReadError("submit effect committed but response lost", request=request)
            payload = self.payload("pending")
            payload["message"] = "Task submitted successfully"
            return self.response(202, payload)
        if method == "GET" and path.startswith("/tasks/by-idempotency/"):
            if not self.task_exists or self.lookup_absent:
                return self.response(404, self.absence_payload)
            return self.response(200, self.payload(self.terminal_status))
        if (path == "/tasks/" + self.task_id or path.startswith("/tasks/" + self.task_id + "/")) and not self.task_exists:
            return self.response(404, self.absence_payload)
        if method == "GET" and path == "/tasks/" + self.task_id:
            if self.pending_polls:
                self.pending_polls -= 1
                return self.response(200, self.payload("processing"))
            return self.response(200, self.payload(self.terminal_status))
        if method == "POST" and path == "/tasks/" + self.task_id + "/lease":
            return self.response(200, {
                "schema": "mineru-task-protocol.v2", "task_id": self.task_id,
                "lease_until_unix": self.lease_expiry,
            })
        if method == "GET" and path == "/tasks/" + self.task_id + "/result":
            stream = ChunkStream(self.archive, self.download_checkpoint)
            self.streams.append(stream)
            return httpx.Response(200, stream=stream, headers={
                "x-mineru-result-sha256": self.archive_sha,
                "x-mineru-result-owner": self.owner,
                **self.download_headers,
            })
        if method == "POST" and path == "/tasks/" + self.task_id + "/ack":
            if self.ack_loss == "before_effect":
                raise httpx.ReadError("ACK not applied and response lost", request=request)
            self.task_exists = False
            self.ack_effects += 1
            if self.ack_loss == "after_effect":
                raise httpx.ReadError("ACK applied but response lost", request=request)
            return self.response(200, {
                "schema": "mineru-task-protocol.v2", "task_id": self.task_id, "status": "consumed",
            })
        raise AssertionError("unmodeled request: " + method + " " + path)

    def run(self, *, resume: bool = False, **overrides: Any) -> dict[str, Any]:
        from disclosure_anchor.adapters.runtime.mineru_diagnostic_lifecycle import run_diagnostic_attempt_v2

        values: dict[str, Any] = {
            "input_pdf": self.source, "source_pdf_sha256": self.source_sha,
            "source_byte_count": len(self.source_bytes), "source_page_count": 2,
            "api_url": API, "server_url": "http://vlm.invalid/v1",
            "options": ParserOptions(runtime_bundle_identity_sha256=RUNTIME_SHA, timeout_seconds=60),
            "journal_root": self.journal, "attempt_identity": "independent-attempt",
            "fence_identity": "independent-fence", "submission_epoch_unix": 999,
            "clock_identity_sha256": CLOCK_SHA, "deadline_ns": self.deadline_ns,
            "continuous_ns": lambda: self.now_ns, "resume": resume,
            "transport": httpx.MockTransport(self.handle), "unix_time": lambda: 1_000.0,
            "pause": lambda duration: None,
        }
        values.update(overrides)
        return run_diagnostic_attempt_v2(**values)
