"""Independent, offline acceptance of the real CNINFO streaming download paths."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

from disclosure_anchor.adapters.sources.cninfo.client import CninfoClient
from disclosure_anchor.adapters.sources.cninfo.source import CninfoSource
from disclosure_anchor.adapters.sources.cninfo.web_source import CninfoWebSource
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore, VolumeSpace
from disclosure_anchor.application.ports.disclosure_source import AnnouncementRef
from disclosure_anchor.application.ports.file_store import AcquisitionCapacityError
from disclosure_anchor.application.use_cases.download_document import (
    DownloadDocument,
    DownloadDocumentCommand,
)
from disclosure_anchor.domain.errors import SourceRequestError
from disclosure_anchor.settings import Settings
from tests.unit.test_download_document import _candidate, _uow_with_subject


def _ref() -> AnnouncementRef:
    return AnnouncementRef(
        provider="cninfo",
        provider_document_id="synthetic-one",
        title="Synthetic byte-stream test",
        download_url="https://static.cninfo.example/one.PDF",
        raw_category="test",
        announcement_date=date(2026, 9, 27),
        security_code="000001",
        security_name=None,
        file_size=None,
        index_updated_at=None,
    )


class _ObservedFileSink:
    """Small owned file: measures actual writes and refuses a test capacity."""

    def __init__(self, path: Path, *, max_bytes: int = 1024 * 1024) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.attempts = 0
        self.declarations: list[int | None] = []
        self.writes: list[int] = []

    def begin_attempt(self, *, declared_byte_count: int | None) -> None:
        self.attempts += 1
        self.declarations.append(declared_byte_count)
        self.path.write_bytes(b"")

    def write(self, chunk: bytes) -> None:
        if self.path.stat().st_size + len(chunk) > self.max_bytes:
            raise OSError("synthetic sink capacity refusal")
        with self.path.open("ab") as output:
            output.write(chunk)
        self.writes.append(len(chunk))


class _GatedStream(httpx.SyncByteStream):
    """The second network chunk requires the first sink write already happened."""

    def __init__(
        self,
        chunks: tuple[bytes, ...],
        observed_path: Path,
        *,
        advance_at_eof: Callable[[], None] | None = None,
        fail_after_first: bool = False,
    ) -> None:
        self.chunks = chunks
        self.observed_path = observed_path
        self.advance_at_eof = advance_at_eof
        self.fail_after_first = fail_after_first
        self.closed = False
        self.iterated = False

    def __iter__(self) -> Iterator[bytes]:
        self.iterated = True
        for position, chunk in enumerate(self.chunks):
            if position == 1 and self.observed_path.stat().st_size != len(self.chunks[0]):
                raise AssertionError("body was buffered before the first sink write")
            yield chunk
            if position == 0 and self.fail_after_first:
                raise httpx.ReadError("synthetic interrupted body")
        if self.advance_at_eof is not None:
            self.advance_at_eof()

    def close(self) -> None:
        self.closed = True


def _api_source(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    clock: Callable[[], float] | None = None,
    max_retries: int = 1,
) -> CninfoSource:
    return CninfoSource(
        CninfoClient(
            access_key=None,
            access_secret=None,
            access_token="synthetic-token",
            transport=httpx.MockTransport(handler),
            sleep=lambda _: None,
            jitter=lambda _: 0.0,
            clock=clock,
            max_retries=max_retries,
            download_deadline_seconds=5.0,
        )
    )


def _web_source(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    clock: Callable[[], float] | None = None,
    max_retries: int = 1,
) -> CninfoWebSource:
    return CninfoWebSource(
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
        jitter=lambda _: 0.0,
        clock=clock,
        max_retries=max_retries,
        download_deadline_seconds=5.0,
    )


def _settings(root: Path) -> Settings:
    service_root = root / "service"
    shared_root = root / "shared"
    return Settings(
        disclosure_data_root=service_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=service_root / "runtime",
        mineru_model_cache=shared_root / "mineru",
        hf_home=shared_root / "huggingface",
        modelscope_cache=shared_root / "modelscope",
    )


def _raw_store(
    root: Path, *, available_bytes: int, occupied_path: Path | None = None
) -> RawDocumentStore:
    def space_probe(_: Path) -> VolumeSpace:
        occupied = occupied_path.stat().st_size if occupied_path and occupied_path.exists() else 0
        return VolumeSpace(
            available_bytes=available_bytes - occupied, total_bytes=1000
        )

    return RawDocumentStore(
        FileStorePathBuilder(_settings(root)),
        free_floor_bytes=0,
        space_probe=space_probe,
    )


class AcquisitionStreamingIndependentTests(unittest.TestCase):
    def test_api_and_web_write_each_chunk_before_reading_next(self) -> None:
        payload = b"%PDF-1.4\nsynthetic\n%%EOF\n"
        for channel, factory in (("api", _api_source), ("web", _web_source)):
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as tmp:
                sink = _ObservedFileSink(Path(tmp) / "download.pdf")
                stream = _GatedStream((payload[:9], payload[9:]), sink.path)
                seen_headers: list[str | None] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    seen_headers.append(request.headers.get("accept-encoding"))
                    return httpx.Response(200, stream=stream)

                source = factory(handler)
                try:
                    receipt = source.download_pdf_to(_ref(), sink)
                finally:
                    source.close()
                self.assertEqual(sink.path.read_bytes(), payload)
                self.assertEqual(receipt.byte_count, len(payload))
                self.assertIsNone(receipt.declared_byte_count)
                self.assertEqual(sink.writes, [9, len(payload) - 9])
                self.assertEqual(seen_headers, ["identity"])
                self.assertTrue(stream.closed)

    def test_partial_first_attempt_is_reset_before_retry(self) -> None:
        for channel, factory in (("api", _api_source), ("web", _web_source)):
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as tmp:
                sink = _ObservedFileSink(Path(tmp) / "download.pdf")
                first = _GatedStream((b"%PDF-old",), sink.path, fail_after_first=True)
                final = b"%PDF-1.4\nnew\n%%EOF\n"
                second = _GatedStream((final[:8], final[8:]), sink.path)
                attempts = 0

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal attempts
                    attempts += 1
                    return httpx.Response(200, stream=first if attempts == 1 else second)

                source = factory(handler)
                try:
                    receipt = source.download_pdf_to(_ref(), sink)
                finally:
                    source.close()
                self.assertEqual(attempts, 2)
                self.assertEqual(sink.attempts, 2)
                self.assertEqual(sink.path.read_bytes(), final)
                self.assertEqual(receipt.byte_count, len(final))
                self.assertTrue(first.closed and second.closed)

    def test_eof_after_deadline_does_not_claim_completion(self) -> None:
        for channel, factory in (("api", _api_source), ("web", _web_source)):
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as tmp:
                now = 0.0

                def expire() -> None:
                    nonlocal now
                    now = 5.0

                sink = _ObservedFileSink(Path(tmp) / "download.pdf")
                stream = _GatedStream((b"%PDF-1.4\n%%EOF\n",), sink.path, advance_at_eof=expire)
                source = factory(
                    lambda request: httpx.Response(200, stream=stream),
                    clock=lambda: now,
                    max_retries=0,
                )
                try:
                    with self.assertRaises(SourceRequestError) as caught:
                        source.download_pdf_to(_ref(), sink)
                finally:
                    source.close()
                self.assertEqual(caught.exception.error_code, "transfer_deadline_exceeded")
                self.assertEqual(sink.path.read_bytes(), b"%PDF-1.4\n%%EOF\n")
                self.assertTrue(stream.closed)

    def test_declared_length_cannot_claim_completion_for_short_body(self) -> None:
        body = b"%PDF-1.4\nshort"
        for channel, factory in (("api", _api_source), ("web", _web_source)):
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as tmp:
                sink = _ObservedFileSink(Path(tmp) / "download.pdf")
                stream = _GatedStream((body,), sink.path)
                source = factory(
                    lambda request: httpx.Response(
                        200,
                        headers={"Content-Length": str(len(body) + 4)},
                        stream=stream,
                    ),
                    max_retries=0,
                )
                try:
                    with self.assertRaises(SourceRequestError):
                        source.download_pdf_to(_ref(), sink)
                finally:
                    source.close()
                self.assertEqual(sink.declarations, [len(body) + 4])
                self.assertEqual(sink.path.read_bytes(), body)
                self.assertTrue(stream.closed)

    def test_actual_bytes_hit_sink_capacity_without_completion(self) -> None:
        for channel, factory in (("api", _api_source), ("web", _web_source)):
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as tmp:
                sink = _ObservedFileSink(Path(tmp) / "download.pdf", max_bytes=12)
                stream = _GatedStream((b"%PDF-123", b"4567890"), sink.path)
                source = factory(lambda request: httpx.Response(200, stream=stream))
                try:
                    with self.assertRaises(OSError):
                        source.download_pdf_to(_ref(), sink)
                finally:
                    source.close()
                self.assertEqual(sink.path.read_bytes(), b"%PDF-123")
                self.assertEqual(sink.attempts, 1)
                self.assertTrue(stream.closed)

    def test_real_owned_staging_seals_exact_bytes_and_refuses_prewrite_shortfall(self) -> None:
        payload = b"%PDF-1.4\nsynthetic\n%%EOF\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _raw_store(root, available_bytes=1000)
            target = root / "completed.pdf"
            sink = store.open_pdf_download(target)
            stream = _GatedStream((payload[:9], payload[9:]), target)
            source = _api_source(lambda request: httpx.Response(200, stream=stream))
            try:
                transfer = source.download_pdf_to(_ref(), sink)
                sealed = sink.seal(transfer)
                self.assertEqual(sealed.path.read_bytes(), payload)
                self.assertEqual(sealed.byte_count, len(payload))
                self.assertEqual(
                    sealed.raw_file_hash,
                    "sha256:" + hashlib.sha256(payload).hexdigest(),
                )
            finally:
                source.close()
                sink.discard()
                sink.close()
            self.assertFalse(target.exists())

            partial_path = root / "partial.pdf"
            limited = _raw_store(root, available_bytes=12, occupied_path=partial_path)
            partial = limited.open_pdf_download(partial_path)
            stream = _GatedStream((b"%PDF-123", b"4567890"), partial_path)
            source = _web_source(lambda request: httpx.Response(200, stream=stream))
            try:
                with self.assertRaises(AcquisitionCapacityError):
                    source.download_pdf_to(_ref(), partial)
                self.assertEqual(partial_path.read_bytes(), b"%PDF-123")
            finally:
                source.close()
                retained = partial.close()
            self.assertIsNone(retained)
            self.assertFalse(partial_path.exists())

    def test_real_staging_zero_byte_write_fails_without_spinning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "zero-write.pdf"
            sink = _raw_store(root, available_bytes=1000).open_pdf_download(target)
            sink.begin_attempt(declared_byte_count=None)
            calls = 0

            def zero_then_trip(fd: int, chunk: memoryview) -> int:
                del fd, chunk
                nonlocal calls
                calls += 1
                if calls == 1:
                    return 0
                raise AssertionError("zero-byte os.write was retried without progress")

            try:
                with mock.patch(
                    "disclosure_anchor.adapters.storage.raw_document_store.os.write",
                    side_effect=zero_then_trip,
                ):
                    with self.assertRaises(Exception):
                        sink.write(b"X")
                self.assertEqual(calls, 1)
                self.assertEqual(target.stat().st_size, 0)
            finally:
                sink.close()
            self.assertFalse(target.exists())

    def test_content_coding_is_refused_before_reading_or_writing(self) -> None:
        for channel, factory in (("api", _api_source), ("web", _web_source)):
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as tmp:
                sink = _ObservedFileSink(Path(tmp) / "download.pdf")
                stream = _GatedStream((b"compressed-body",), sink.path)
                source = factory(
                    lambda request: httpx.Response(
                        200, headers={"Content-Encoding": "gzip"}, stream=stream
                    ),
                    max_retries=0,
                )
                try:
                    with self.assertRaises(SourceRequestError) as caught:
                        source.download_pdf_to(_ref(), sink)
                finally:
                    source.close()
                self.assertEqual(caught.exception.error_code, "unsupported_content_encoding")
                self.assertEqual(sink.attempts, 0)
                self.assertFalse(sink.path.exists())
                self.assertFalse(stream.iterated)
                self.assertTrue(stream.closed)

    def test_incomplete_quarantine_copy_cannot_replace_only_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "only-original.pdf"
            payload = b"%PDF-1.4\nonly source\n%%EOF\n"
            source.write_bytes(payload)
            store = _raw_store(root, available_bytes=1000)
            original_read = os.read
            reads = 0

            def interrupted_read(fd: int, count: int) -> bytes:
                nonlocal reads
                reads += 1
                if reads == 1:
                    return original_read(fd, 7)
                raise OSError(errno.EIO, "synthetic source read failure")

            with mock.patch(
                "disclosure_anchor.adapters.storage.raw_document_store.os.read",
                side_effect=interrupted_read,
            ):
                result = store.quarantine_raw_document(
                    provider="cninfo",
                    provider_document_id="synthetic-one",
                    input_file=source,
                    reason="invalid_raw_document",
                )
            manifest = json.loads(
                result.path.with_suffix(result.path.suffix + ".json").read_text()
            )
            self.assertFalse(result.transfer_complete)
            self.assertFalse(result.input_missing)
            self.assertEqual(source.read_bytes(), payload)
            self.assertIs(manifest["payload_complete"], False)
            self.assertEqual(manifest["input_byte_count"], len(payload))
            self.assertIn("input read failed after 7 bytes", manifest["copy_error"])

    def test_download_use_case_uses_real_sink_and_archives_complete_pdf(self) -> None:
        payload = b"%PDF-1.4\nuse-case stream\n%%EOF\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = FileStorePathBuilder(_settings(root))
            store = RawDocumentStore(paths, free_floor_bytes=0)
            source = _api_source(
                lambda request: httpx.Response(
                    200,
                    stream=_GatedStream(
                        (payload[:9], payload[9:]),
                        next(paths.runtime_tmp_path().glob("cninfo_*.pdf")),
                    ),
                )
            )
            uow = _uow_with_subject()
            try:
                result = DownloadDocument(
                    source=source,
                    raw_store=store,
                    path_builder=paths,
                    uow_factory=lambda: uow,
                ).execute(DownloadDocumentCommand(candidate=_candidate()))
            finally:
                source.close()
            self.assertIsNotNone(result.document_id)
            self.assertEqual(
                result.raw_file_hash,
                "sha256:" + hashlib.sha256(payload).hexdigest(),
            )
            access = uow.source_accesses.get(result.source_access_id)
            self.assertEqual(access.status, "ok")
            self.assertEqual(access.result_snapshot["byte_count"], len(payload))
            self.assertEqual(list(paths.runtime_tmp_path().glob("cninfo_*.pdf")), [])

    def test_failed_quarantine_copy_keeps_sealed_download_as_only_material(self) -> None:
        payload = b"invalid synthetic PDF body"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = FileStorePathBuilder(_settings(root))
            store = RawDocumentStore(paths, free_floor_bytes=0)
            source = _api_source(
                lambda request: httpx.Response(
                    200, stream=_GatedStream((payload[:7], payload[7:]),
                                            next(paths.runtime_tmp_path().glob("cninfo_*.pdf")))
                )
            )
            uow = _uow_with_subject()
            original_read = os.read
            reads = 0

            def interrupted_read(fd: int, count: int) -> bytes:
                nonlocal reads
                reads += 1
                if reads == 1:
                    return original_read(fd, 5)
                raise OSError(errno.EIO, "synthetic quarantine source failure")

            try:
                with mock.patch(
                    "disclosure_anchor.adapters.storage.raw_document_store.os.read",
                    side_effect=interrupted_read,
                ):
                    result = DownloadDocument(
                        source=source,
                        raw_store=store,
                        path_builder=paths,
                        uow_factory=lambda: uow,
                    ).execute(DownloadDocumentCommand(candidate=_candidate()))
            finally:
                source.close()
            self.assertEqual(result.error_code, "invalid_raw_document")
            self.assertIsNone(result.document_id)
            retained = list(paths.runtime_tmp_path().glob("cninfo_*.pdf"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), payload)
            self.assertIsNotNone(result.quarantined_path)
            self.assertEqual(result.quarantined_path.read_bytes(), b"")
            access = uow.source_accesses.get(result.source_access_id)
            self.assertEqual(access.status, "failed")
            self.assertIs(access.result_snapshot["quarantine_complete"], False)
            self.assertEqual(access.result_snapshot["retained_filename"], retained[0].name)

    def test_archive_space_refusal_retains_sealed_only_source(self) -> None:
        payload = b"%PDF-1.4\narchive capacity\n%%EOF\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = FileStorePathBuilder(_settings(root))
            probes = 0

            def space_probe(_: Path) -> VolumeSpace:
                nonlocal probes
                probes += 1
                # Admission + two body writes fit; the archive's separate
                # copy does not. The actual pre-archive require_space decides.
                return VolumeSpace(
                    available_bytes=0 if probes >= 4 else 1000,
                    total_bytes=1000,
                )

            store = RawDocumentStore(
                paths, free_floor_bytes=0, space_probe=space_probe
            )
            source = _api_source(
                lambda request: httpx.Response(
                    200,
                    stream=_GatedStream(
                        (payload[:9], payload[9:]),
                        next(paths.runtime_tmp_path().glob("cninfo_*.pdf")),
                    ),
                )
            )
            uow = _uow_with_subject()
            try:
                result = DownloadDocument(
                    source=source,
                    raw_store=store,
                    path_builder=paths,
                    uow_factory=lambda: uow,
                ).execute(DownloadDocumentCommand(candidate=_candidate()))
            finally:
                source.close()
            self.assertEqual(result.error_code, "local_space_shortfall")
            self.assertIsNone(result.document_id)
            access = uow.source_accesses.get(result.source_access_id)
            self.assertEqual(access.status, "failed")
            self.assertEqual(access.result_snapshot["capacity"]["phase"], "archive")
            retained = list(paths.runtime_tmp_path().glob("cninfo_*.pdf"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), payload)
            self.assertEqual(
                "sha256:" + hashlib.sha256(retained[0].read_bytes()).hexdigest(),
                "sha256:" + hashlib.sha256(payload).hexdigest(),
            )
            self.assertEqual(access.result_snapshot["retained_filename"], retained[0].name)
            self.assertFalse((root / "service" / "data" / "raw_documents").exists())

    def test_archive_io_failure_retains_sealed_only_source(self) -> None:
        payload = b"%PDF-1.4\narchive I/O\n%%EOF\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = FileStorePathBuilder(_settings(root))

            class FailingArchiveStore(RawDocumentStore):
                def put_raw_document(self, **kwargs):  # type: ignore[override]
                    del kwargs
                    raise OSError(errno.EIO, "synthetic archive destination failure")

            store = FailingArchiveStore(paths, free_floor_bytes=0)
            source = _api_source(
                lambda request: httpx.Response(
                    200,
                    stream=_GatedStream(
                        (payload[:9], payload[9:]),
                        next(paths.runtime_tmp_path().glob("cninfo_*.pdf")),
                    ),
                )
            )
            uow = _uow_with_subject()
            try:
                result = DownloadDocument(
                    source=source,
                    raw_store=store,
                    path_builder=paths,
                    uow_factory=lambda: uow,
                ).execute(DownloadDocumentCommand(candidate=_candidate()))
            finally:
                source.close()
            self.assertEqual(result.error_code, "io_error")
            self.assertIsNone(result.document_id)
            access = uow.source_accesses.get(result.source_access_id)
            self.assertEqual(access.status, "failed")
            retained = list(paths.runtime_tmp_path().glob("cninfo_*.pdf"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), payload)
            self.assertEqual(access.result_snapshot["retained_filename"], retained[0].name)
            self.assertFalse((root / "service" / "data" / "raw_documents").exists())

    def test_real_archive_fsync_enospc_is_retryable_and_retains_source(self) -> None:
        payload = b"%PDF-1.4\nreal archive fsync failure\n%%EOF\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = FileStorePathBuilder(_settings(root))
            archive_failed = False

            def space_probe(_: Path) -> VolumeSpace:
                return VolumeSpace(
                    available_bytes=0 if archive_failed else 1000,
                    total_bytes=1000,
                )

            store = RawDocumentStore(
                paths, free_floor_bytes=0, space_probe=space_probe
            )
            source = _api_source(
                lambda request: httpx.Response(
                    200,
                    stream=_GatedStream(
                        (payload[:9], payload[9:]),
                        next(paths.runtime_tmp_path().glob("cninfo_*.pdf")),
                    ),
                )
            )
            uow = _uow_with_subject()
            original_fsync = os.fsync

            def fail_archive_fsync(fd: int) -> None:
                nonlocal archive_failed
                if list(paths.runtime_tmp_path().glob("raw_*.tmp")):
                    archive_failed = True
                    raise OSError(errno.ENOSPC, "synthetic archive fsync shortfall")
                original_fsync(fd)

            try:
                with mock.patch(
                    "disclosure_anchor.adapters.storage.raw_document_store.os.fsync",
                    side_effect=fail_archive_fsync,
                ):
                    result = DownloadDocument(
                        source=source,
                        raw_store=store,
                        path_builder=paths,
                        uow_factory=lambda: uow,
                    ).execute(DownloadDocumentCommand(candidate=_candidate()))
            finally:
                source.close()
            self.assertTrue(archive_failed)
            access = uow.source_accesses.get(result.source_access_id)
            self.assertEqual(access.status, "failed")
            self.assertEqual(result.error_code, "io_error")
            self.assertIs(result.retryable, True)
            self.assertIsNone(result.document_id)
            retained = list(paths.runtime_tmp_path().glob("cninfo_*.pdf"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), payload)
            self.assertEqual(access.result_snapshot["retained_filename"], retained[0].name)
            self.assertFalse((root / "service" / "data" / "raw_documents").exists())

    def test_real_archive_rechecks_free_space_before_later_copy_chunk(self) -> None:
        payload = b"%PDF-1.4\n" + b"A" * (1024 * 1024 + 32)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = FileStorePathBuilder(_settings(root))
            late_checks = 0

            def space_probe(_: Path) -> VolumeSpace:
                nonlocal late_checks
                staged = list(paths.runtime_tmp_path().glob("raw_*.tmp"))
                if staged and staged[0].stat().st_size >= 1024 * 1024:
                    late_checks += 1
                    return VolumeSpace(available_bytes=0, total_bytes=4 * 1024 * 1024)
                return VolumeSpace(
                    available_bytes=4 * 1024 * 1024,
                    total_bytes=4 * 1024 * 1024,
                )

            store = RawDocumentStore(
                paths, free_floor_bytes=0, space_probe=space_probe
            )
            source = _api_source(
                lambda request: httpx.Response(200, stream=_GatedStream(
                    (payload,), next(paths.runtime_tmp_path().glob("cninfo_*.pdf"))
                ))
            )
            uow = _uow_with_subject()
            try:
                result = DownloadDocument(
                    source=source,
                    raw_store=store,
                    path_builder=paths,
                    uow_factory=lambda: uow,
                ).execute(DownloadDocumentCommand(candidate=_candidate()))
            finally:
                source.close()
            self.assertGreater(
                late_checks,
                0,
                f"no late archive probe; result={result.error_code}, "
                f"document_id={result.document_id}",
            )
            self.assertEqual(result.error_code, "local_space_shortfall")
            self.assertIsNone(result.document_id)
            access = uow.source_accesses.get(result.source_access_id)
            self.assertEqual(access.status, "failed")
            retained = list(paths.runtime_tmp_path().glob("cninfo_*.pdf"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), payload)
            self.assertFalse((root / "service" / "data" / "raw_documents").exists())


if __name__ == "__main__":
    unittest.main()
