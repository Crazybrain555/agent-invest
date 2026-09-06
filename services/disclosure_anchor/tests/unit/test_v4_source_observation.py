"""Real child-process source inspection, identity and cancellation boundaries."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import time
import unittest
from unittest import mock

import pypdfium2 as pdfium

from disclosure_anchor.adapters.parsers.pdf_source_observation_process import inspect_source
from disclosure_anchor.adapters.storage import v4_source_observation as observer_module
from disclosure_anchor.adapters.storage.v4_source_observation import BoundedV4SourcePdfObserver
from disclosure_anchor.application.contracts.provider_document_admission import SourcePdfObservation
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.ports.staged_new_work_v4 import (
    V4AdmissionObservationRequest, V4OrdinaryParseCandidate, V4RejectedSourcePdf, V4SourcePdfOverLimit,
)
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseGuard, StageLeaseLost


class _Paths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def data_path(self, relpath: Path) -> Path:
        return self.root / relpath


def _guard() -> StageLeaseGuard:
    return StageLeaseGuard(
        deadline_monotonic=time.monotonic() + 10, _revoked=Event(), _monotonic=time.monotonic,
    )


def _request(path: Path) -> V4AdmissionObservationRequest:
    payload = path.read_bytes()
    return V4AdmissionObservationRequest(
        candidate=V4OrdinaryParseCandidate(
            document_id="doc-observe", provider="cninfo", provider_document_id="observe",
            security_id="security", security_code="000001", raw_file_relpath=path.name,
            raw_file_hash="sha256:" + hashlib.sha256(payload).hexdigest(), archived_raw_byte_count=len(payload),
        ), credits=ResourceCreditVector(documents=1, snapshot_items=1, snapshot_bytes=len(payload)),
    )


class V4SourceObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "source.pdf"
        with pdfium.PdfDocument.new() as pdf:
            for _ in range(2):
                page = pdf.new_page(100, 100)
                page.close()
            pdf.save(self.path)
        self.observer = BoundedV4SourcePdfObserver(paths=_Paths(self.root))  # type: ignore[arg-type]

    def test_real_child_counts_complete_pdf_and_returns_exact_source(self) -> None:
        request = _request(self.path)
        result = self.observer.observe(request, stage_guard=_guard())
        self.assertEqual(result.source, SourcePdfObservation(
            sha256=request.candidate.raw_file_hash, byte_count=request.credits.snapshot_bytes, page_count=2,
        ))

    def test_rejection_is_verified_content_not_missing_file_or_hash_drift(self) -> None:
        for payload in (b"not a PDF", b"%PDF-1.7\nmalformed\n%%EOF\n"):
            with self.subTest(payload=payload):
                self.path.write_bytes(payload)
                request = _request(self.path)
                result = self.observer.observe(request, stage_guard=_guard())
                self.assertIs(type(result.source), V4RejectedSourcePdf)
                self.assertEqual(result.source.sha256, request.candidate.raw_file_hash)
                self.assertFalse(hasattr(result.source, "page_count"))
                drift = replace(request, candidate=replace(request.candidate, raw_file_hash="sha256:" + "f" * 64))
                with self.assertRaisesRegex(RuntimeError, "archived hash or length drifted"):
                    self.observer.observe(drift, stage_guard=_guard())
        request = _request(self.path)
        self.path.unlink()
        with self.assertRaisesRegex(RuntimeError, "child failed"):
            self.observer.observe(request, stage_guard=_guard())

    def test_unknown_oversize_is_stat_only_and_does_not_invent_digest(self) -> None:
        original = _request(self.path)
        request = replace(
            original, candidate=replace(original.candidate, archived_raw_byte_count=None),
            credits=ResourceCreditVector(documents=1, snapshot_items=1, snapshot_bytes=5),
        )
        result = self.observer.observe(request, stage_guard=_guard())
        self.assertEqual(result.source, V4SourcePdfOverLimit(self.path.stat().st_size))
        self.assertFalse(hasattr(result.source, "sha256"))

    def test_cancelled_stalled_child_is_killed_and_reaped_before_return(self) -> None:
        request = _request(self.path)
        guard = _guard()
        started = Event()
        processes = []
        popen = subprocess.Popen

        def delayed(_command, **kwargs):
            process = popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            processes.append(process)
            started.set()
            return process

        with mock.patch.object(observer_module.subprocess, "Popen", side_effect=delayed):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self.observer.observe, request, stage_guard=guard)
                try:
                    self.assertTrue(started.wait(timeout=2))
                    guard.revoke()
                    with self.assertRaises(StageLeaseLost):
                        future.result(timeout=2)
                    self.assertIsNotNone(processes[0].poll())
                    self.assertLess(processes[0].returncode, 0)
                finally:
                    guard.revoke()
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                        process.wait()

    def test_same_bytes_replacement_during_count_is_not_a_stable_observation(self) -> None:
        request = _request(self.path)
        original = self.path.read_bytes()

        def replace_path(_stream):
            # Original descriptor remains held by inspect_source, preventing
            # inode reuse from turning this into a platform-dependent fixture.
            self.path.rename(self.root / "original.pdf")
            self.path.write_bytes(original)
            document = mock.MagicMock()
            document.__enter__.return_value.__len__.return_value = 2
            return document

        with mock.patch.object(pdfium, "PdfDocument", side_effect=replace_path):
            with self.assertRaisesRegex(ValueError, "file identity changed"):
                inspect_source(
                    root=self.root, relpath=Path(self.path.name),
                    byte_limit=request.credits.snapshot_bytes,
                    expected_sha256=request.candidate.raw_file_hash,
                    expected_byte_count=request.candidate.archived_raw_byte_count,
                )


if __name__ == "__main__":
    unittest.main()
