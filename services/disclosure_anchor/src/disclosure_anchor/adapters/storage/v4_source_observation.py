"""Deadline/cancellation-bounded source observation with actual child drain."""

from __future__ import annotations

import os
from pathlib import Path
import selectors
import subprocess
import sys

from disclosure_anchor.application.contracts.provider_document_admission import SourcePdfObservation
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.application.ports.staged_new_work_v4 import (
    V4AdmissionObservationRequest, V4AdmissionObservationResult,
    V4RejectedSourcePdf, V4SourcePdfOverLimit,
)
from disclosure_anchor.application.ports.staged_provider_parser import V4StageGuard


_MAX_OUTPUT_BYTES = 8192


class BoundedV4SourcePdfObserver:
    def __init__(self, *, paths: FileStorePathPort) -> None:
        self._root = paths.data_path(Path())

    def observe(
        self, request: V4AdmissionObservationRequest, *, stage_guard: V4StageGuard,
    ) -> V4AdmissionObservationResult:
        if type(request) is not V4AdmissionObservationRequest:
            raise ValueError("source observer requires an exact request")
        stage_guard.checkpoint()
        candidate = request.candidate
        command = [
            sys.executable, "-B", "-m",
            "disclosure_anchor.adapters.parsers.pdf_source_observation_process",
            "--root", str(self._root), "--relpath", candidate.raw_file_relpath,
            "--byte-limit", str(request.credits.snapshot_bytes),
            "--expected-sha256", candidate.raw_file_hash,
        ]
        if candidate.archived_raw_byte_count is not None:
            command.extend(("--expected-byte-count", str(candidate.archived_raw_byte_count)))
        # The child needs no database/provider/authentication environment.
        environment = {key: value for key, value in os.environ.items()
                       if key in {"PATH", "PYTHONPATH", "PYTHONHOME", "LANG", "LC_ALL"}}
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment, start_new_session=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = bytearray()
        stderr = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                for stream, destination in ((process.stdout, stdout), (process.stderr, stderr)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, destination)
                while selector.get_map() or process.poll() is None:
                    timeout = min(0.05, stage_guard.remaining_seconds())
                    for key, _ in selector.select(timeout):
                        chunk = os.read(key.fd, _MAX_OUTPUT_BYTES + 1 - len(stdout) - len(stderr))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        key.data.extend(chunk)
                        if len(stdout) + len(stderr) > _MAX_OUTPUT_BYTES:
                            raise RuntimeError("source observer output exceeded its bounded receipt")
                    stage_guard.checkpoint()
            if process.returncode != 0:
                raise RuntimeError(
                    f"source observation child failed ({process.returncode}): "
                    + bytes(stderr).decode("utf-8", errors="replace")
                )
            stage_guard.checkpoint()
            payload = strict_json_loads(bytes(stdout))
            if not isinstance(payload, dict):
                raise ValueError("source observer receipt is not an object")
            source: SourcePdfObservation | V4RejectedSourcePdf | V4SourcePdfOverLimit
            if payload.get("kind") == "valid" and set(payload) == {"kind", "sha256", "byte_count", "page_count"}:
                source = SourcePdfObservation(
                    sha256=payload["sha256"], byte_count=payload["byte_count"], page_count=payload["page_count"],
                )
            elif payload.get("kind") == "rejected" and set(payload) == {"kind", "sha256", "byte_count", "reason_code"}:
                source = V4RejectedSourcePdf(
                    sha256=payload["sha256"], byte_count=payload["byte_count"], reason_code=payload["reason_code"],
                )
            elif payload.get("kind") == "overlimit" and set(payload) == {"kind", "byte_count"}:
                source = V4SourcePdfOverLimit(byte_count=payload["byte_count"])
            else:
                raise ValueError("source observer receipt shape is not closed")
            return V4AdmissionObservationResult(request=request, source=source)
        finally:
            # Timeout does not terminate a child by itself. Never release the
            # shared slot/credit merely because cancellation was requested.
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            process.stderr.close()


__all__ = ["BoundedV4SourcePdfObserver"]
