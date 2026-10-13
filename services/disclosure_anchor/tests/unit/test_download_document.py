"""Tests for pending CNINFO download and register-document reuse."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from datetime import date, datetime, timezone
import errno
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx
from sqlalchemy.exc import OperationalError

from disclosure_anchor.adapters.sources.cninfo.web_source import CninfoWebSource
from disclosure_anchor.adapters.storage import raw_document_store
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import (
    FreeSpaceFloor,
    OwnedPdfDownload,
    RawDocumentStore,
    VolumeSpace,
)
from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    CompletedPdfTransfer,
    DisclosureWindow,
    PdfDownloadSink,
    SourceSecurity,
)
from disclosure_anchor.application.ports.file_store import (
    AcquisitionCapacityError,
    QuarantineResult,
    RawDocumentVerification,
    RawDocumentWriteResult,
)
from disclosure_anchor.application.use_cases.download_document import (
    DOWNLOAD_INTERFACE,
    DownloadDocument,
    DownloadDocumentCommand,
)
from disclosure_anchor.application.use_cases.sync_disclosure_index import INDEX_INTERFACE
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import InvalidRawDocumentError, SourceRequestError
from tests._pdf_download_fixture import ChunkStream, DirectoryVolume, acquisition_settings
from tests.unit._fakes import FakeUnitOfWork


class DownloadDocumentTests(unittest.TestCase):
    def test_lists_pending_candidates_from_persisted_source_access(self) -> None:
        uow = _uow_with_subject()
        candidate = _candidate()
        uow.source_accesses.add(
            e.SourceAccess(
                source_access_id="sa_index",
                provider="cninfo",
                provider_interface=INDEX_INTERFACE,
                accessed_at=datetime.now(timezone.utc),
                status="ok",
                result_snapshot={"result": "ok", "candidates": [candidate]},
                company_id="co_1",
                security_id="sec_1",
            )
        )
        use_case = _use_case(uow, [b"%PDF-1.4\nsame\n%%EOF\n"])

        pending = use_case.list_pending_candidates(
            max_retries=3, overlap_start=date(2026, 6, 25)
        )

        self.assertEqual(pending[0]["provider_document_id"], "pid-1")

    def test_download_archives_and_registers_document(self) -> None:
        uow = _uow_with_subject()
        use_case = _use_case(uow, [b"%PDF-1.4\none\n%%EOF\n"])

        result = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))

        self.assertIsNotNone(result.document_id)
        document = uow.documents.get(result.document_id)
        self.assertEqual(document.provider_document_id, "pid-1")
        self.assertEqual(document.provider_metadata["raw_category"], "010301")
        self.assertEqual(document.provider_metadata["file_signature"]["file_size"], 2048)
        self.assertNotIn("oversized", document.provider_metadata)
        source_access = uow.source_accesses.get(result.source_access_id)
        self.assertEqual(source_access.provider_interface, DOWNLOAD_INTERFACE)
        self.assertEqual(
            source_access.result_snapshot["byte_count"],
            len(b"%PDF-1.4\none\n%%EOF\n"),
        )
        self.assertEqual(uow.commit_count, 1)

    def test_same_provider_document_changed_file_supersedes_via_register_core(self) -> None:
        uow = _uow_with_subject()
        use_case = _use_case(
            uow,
            [
                b"%PDF-1.4\nold\n%%EOF\n",
                b"%PDF-1.4\nnew\n%%EOF\n",
            ],
        )

        first = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))
        second = use_case.execute(DownloadDocumentCommand(candidate=_candidate(file_size=4096)))

        self.assertNotEqual(second.document_id, first.document_id)
        self.assertEqual(
            uow.documents.get(second.document_id).supersedes_document_id,
            first.document_id,
        )

    def test_same_provider_document_same_hash_reuses_existing_document(self) -> None:
        uow = _uow_with_subject()
        payload = b"%PDF-1.4\nsame\n%%EOF\n"
        use_case = _use_case(uow, [payload, payload])

        first = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))
        second = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))

        self.assertTrue(second.reused_existing_document)
        self.assertEqual(second.document_id, first.document_id)

    def test_non_pdf_is_quarantined_and_records_failed_source_access(self) -> None:
        uow = _uow_with_subject()
        raw_store = FakeRawStore()
        use_case = DownloadDocument(
            source=FakeDownloadSource([b"not a pdf"]),
            raw_store=raw_store,
            path_builder=FakePathBuilder(),
            uow_factory=lambda: uow,
        )

        result = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))

        self.assertIsNone(result.document_id)
        self.assertEqual(result.quarantine_reason, "invalid_raw_document")
        source_access = uow.source_accesses.get(result.source_access_id)
        self.assertEqual(source_access.status, "failed")
        self.assertEqual(source_access.query_params["provider_document_id"], "pid-1")
        self.assertIn('"retryable":false', source_access.error)

    def test_download_http_failure_records_failed_source_access(self) -> None:
        uow = _uow_with_subject()
        use_case = DownloadDocument(
            source=FailingDownloadSource(
                SourceRequestError(
                    "CNINFO download request failed",
                    error_code="http_404",
                    retryable=True,
                )
            ),
            raw_store=FakeRawStore(),
            path_builder=FakePathBuilder(),
            uow_factory=lambda: uow,
        )

        result = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))

        self.assertIsNone(result.document_id)
        self.assertIsNone(result.quarantined_path)
        self.assertEqual(result.error_code, "http_404")
        self.assertTrue(result.retryable)
        source_access = uow.source_accesses.get(result.source_access_id)
        self.assertEqual(source_access.status, "failed")
        self.assertIn('"error_code":"http_404"', source_access.error)
        self.assertIn('"retryable":true', source_access.error)
        self.assertIn('"stage":"download"', source_access.error)

    def test_transfer_deadline_records_retryable_failure_without_archiving_partial_data(self) -> None:
        uow = _uow_with_subject()
        raw_store = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            paths = mock.Mock()
            paths.runtime_tmp_path.return_value = Path(directory) / "partial.pdf"
            use_case = DownloadDocument(
                source=FailingDownloadSource(SourceRequestError(
                    "logical download expired", error_code="transfer_deadline_exceeded",
                    retryable=True,
                )),
                raw_store=raw_store,
                path_builder=paths,
                uow_factory=lambda: uow,
            )
            result = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertIsNone(result.document_id)
        self.assertIsNone(result.raw_file_hash)
        self.assertEqual(result.error_code, "transfer_deadline_exceeded")
        self.assertTrue(result.retryable)
        source_access = uow.source_accesses.get(result.source_access_id)
        self.assertEqual(source_access.status, "failed")
        self.assertIn('"retryable":true', source_access.error)
        archive_snapshot = source_access.result_snapshot.get("archive", {})
        self.assertIs(archive_snapshot.get("archive_completed"), False)
        self.assertNotIn("raw_file_hash", archive_snapshot)
        raw_store.put_raw_document.assert_not_called()

    def test_malformed_candidate_records_terminal_failure_not_raise(self) -> None:
        # Escaping exceptions leave no failed source_access, so the candidate
        # never accrues retry budget and re-downloads every round (round23).
        uow = _uow_with_subject()
        candidate = _candidate()
        candidate["announcement_date"] = "not-a-date"
        use_case = _use_case(uow, [b"%PDF-1.4\nx\n%%EOF\n"])

        result = use_case.execute(DownloadDocumentCommand(candidate=candidate))

        self.assertIsNone(result.document_id)
        self.assertEqual(result.error_code, "invalid_candidate_snapshot")
        self.assertFalse(result.retryable)
        source_access = uow.source_accesses.get(result.source_access_id)
        self.assertEqual(source_access.status, "failed")
        self.assertEqual(source_access.query_params["provider_document_id"], "pid-1")
        self.assertIn('"retryable":false', source_access.error)

    def test_unsynced_security_records_terminal_failure_not_raise(self) -> None:
        uow = FakeUnitOfWork()  # no security/company seeded
        use_case = _use_case(uow, [b"%PDF-1.4\nx\n%%EOF\n"])

        result = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))

        self.assertIsNone(result.document_id)
        self.assertEqual(result.error_code, "registration_metadata_error")
        self.assertFalse(result.retryable)
        source_access = uow.source_accesses.get(result.source_access_id)
        self.assertEqual(source_access.status, "failed")
        self.assertIn("security must be synced", source_access.result_snapshot["reason"])

    def test_registration_failure_retains_completed_archive_identity(self) -> None:
        payload = b"%PDF-1.4\nretained original\n%%EOF\n"
        for security_code, exchange in (("300001", "SZSE"), ("600001", "SSE")):
            with self.subTest(security_code=security_code):
                uow = FakeUnitOfWork()  # candidate code is not silently attached
                candidate = _candidate()
                candidate["security_code"] = security_code
                candidate["exchange"] = exchange
                archived: list[RawDocumentWriteResult] = []

                class RecordingRawStore(FakeRawStore):
                    def put_raw_document(self, **kwargs):  # type: ignore[override]
                        receipt = super().put_raw_document(**kwargs)
                        archived.append(receipt)
                        return receipt

                with tempfile.TemporaryDirectory() as directory:
                    paths = mock.Mock()
                    paths.runtime_tmp_path.return_value = Path(directory) / "download.pdf"
                    result = DownloadDocument(
                        source=FakeDownloadSource([payload]),
                        raw_store=RecordingRawStore(),
                        path_builder=paths,
                        uow_factory=lambda: uow,
                    ).execute(DownloadDocumentCommand(candidate=candidate))
                    self.assertEqual(list(Path(directory).iterdir()), [])

                self.assertEqual(result.error_code, "registration_metadata_error")
                self.assertIsNone(result.document_id)
                self.assertEqual(len(archived), 1)
                raw = archived[0]
                self.assertEqual(raw.raw_file_hash, "sha256:" + hashlib.sha256(payload).hexdigest())
                access = uow.source_accesses.get(result.source_access_id)
                self.assertEqual(access.status, "failed")
                snapshot = access.result_snapshot
                self.assertIn("archive", snapshot)
                archive_snapshot = snapshot["archive"]
                self.assertIs(archive_snapshot["archive_completed"], True)
                self.assertEqual(archive_snapshot["raw_file_relpath"], raw.relpath.as_posix())
                self.assertEqual(archive_snapshot["raw_file_hash"], raw.raw_file_hash)
                self.assertEqual(archive_snapshot["byte_count"], len(payload))
                self.assertNotEqual(archive_snapshot["byte_count"], candidate["file_signature_hint"]["file_size"])

    def test_raw_archive_conflict_quarantines_and_records_terminal_failure(self) -> None:
        from disclosure_anchor.domain.errors import RawDocumentError

        class ArchiveConflictRawStore(FakeRawStore):
            def put_raw_document(self, **kwargs):  # type: ignore[override]
                raise RawDocumentError("existing archived file hash mismatch")

        uow = _uow_with_subject()
        use_case = DownloadDocument(
            source=FakeDownloadSource([b"%PDF-1.4\nx\n%%EOF\n"]),
            raw_store=ArchiveConflictRawStore(),
            path_builder=FakePathBuilder(),
            uow_factory=lambda: uow,
        )

        result = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))

        self.assertIsNone(result.document_id)
        self.assertEqual(result.error_code, "raw_archive_error")
        self.assertFalse(result.retryable)
        self.assertEqual(result.quarantine_reason, "raw_archive_conflict")
        source_access = uow.source_accesses.get(result.source_access_id)
        self.assertEqual(source_access.status, "failed")
        self.assertIn(
            "quarantine_filename", source_access.result_snapshot
        )

    def test_missing_exchange_uses_inferred_mainland_identity(self) -> None:
        for code, exchange in (("600519", "SSE"), ("830001", "BSE")):
            with self.subTest(code=code, exchange=exchange):
                uow = _uow_with_listed_subject(code, exchange)
                candidate = _candidate()
                candidate["security_code"] = code
                candidate.pop("exchange")

                result = _use_case(
                    uow, [b"%PDF-1.4\nlisted\n%%EOF\n"]
                ).execute(DownloadDocumentCommand(candidate=candidate))

                self.assertIsNotNone(result.document_id)
                document = uow.documents.get(result.document_id)
                self.assertEqual(document.company_id, "co_listed")


class StreamingAcquisitionTests(unittest.TestCase):
    """Real staging, archive and quarantine under a synthetic volume."""

    def _use_case(
        self,
        root: Path,
        source: object,
        *,
        free: int | None = None,
        floor: int = 0,
        store_cls: type[RawDocumentStore] = RawDocumentStore,
        probe: Callable[[Path], VolumeSpace] | None = None,
        uow: FakeUnitOfWork | None = None,
    ) -> tuple[DownloadDocument, FakeUnitOfWork, FileStorePathBuilder]:
        paths = FileStorePathBuilder(acquisition_settings(root))
        if probe is None:
            probe = (
                _ample_space
                if free is None
                else DirectoryVolume(paths.runtime_tmp_path(), free=free).probe
            )
        uow = _uow_with_subject(uow)
        use_case = DownloadDocument(
            source=source,  # type: ignore[arg-type]
            raw_store=store_cls(paths, free_floor_bytes=floor, space_probe=probe),
            path_builder=paths,
            uow_factory=lambda: uow,
        )
        return use_case, uow, paths

    def test_unfinished_transfers_record_progress_and_leave_no_partial(self) -> None:
        body = b"%PDF-1.7\n" + b"x" * 1000
        floor, chunk = 1_000, 4_096

        def deadline_at_eof(sink: PdfDownloadSink) -> CompletedPdfTransfer:
            sink.begin_attempt(declared_byte_count=None)
            sink.write(body)
            raise SourceRequestError(
                "logical download expired waiting for EOF",
                error_code="transfer_deadline_exceeded",
                retryable=True,
            )

        def past_the_floor(sink: PdfDownloadSink) -> CompletedPdfTransfer:
            sink.begin_attempt(declared_byte_count=None)
            for _ in range(3):
                sink.write(b"x" * chunk)
            raise AssertionError("the floor must stop the third chunk")

        def declared_too_large(sink: PdfDownloadSink) -> CompletedPdfTransfer:
            sink.begin_attempt(declared_byte_count=10 * chunk)
            raise AssertionError("the declared length must be refused first")

        def miscounted(sink: PdfDownloadSink) -> CompletedPdfTransfer:
            sink.begin_attempt(declared_byte_count=None)
            sink.write(body)
            return CompletedPdfTransfer(byte_count=len(body) + 1, declared_byte_count=None)

        progress = {"complete": False, "attempts": 1}
        cases = {
            # (script, error code, capacity phase, transfer snapshot)
            "deadline_at_eof": (deadline_at_eof, "transfer_deadline_exceeded", None,
                                {**progress, "attempt_byte_count": len(body),
                                 "declared_byte_count": None}),
            "floor_mid_stream": (past_the_floor, "local_space_shortfall", "download",
                                 {**progress, "attempt_byte_count": 2 * chunk,
                                  "declared_byte_count": None}),
            "declared_length": (declared_too_large, "local_space_shortfall", "declared_length",
                                {**progress, "attempt_byte_count": 0,
                                 "declared_byte_count": 10 * chunk}),
            "miscounted": (miscounted, "incomplete_transfer", None,
                           {**progress, "attempt_byte_count": len(body),
                            "declared_byte_count": None}),
        }
        for case, (script, error_code, phase, transfer) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                use_case, uow, paths = self._use_case(
                    Path(tmp), ScriptedSource(script), free=floor + 2 * chunk, floor=floor
                )
                result = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))
                self.assertEqual(
                    (result.document_id, result.error_code, result.retryable),
                    (None, error_code, True),
                )
                access = uow.source_accesses.get(result.source_access_id)
                self.assertEqual(access.status, "failed")
                self.assertEqual(access.result_snapshot["archive"], {"archive_completed": False})
                self.assertEqual(access.result_snapshot.get("transfer"), transfer)
                self.assertEqual(
                    access.result_snapshot.get("capacity", {}).get("phase"), phase
                )
                self.assertEqual(_files(paths.runtime_tmp_path()), [])
                self.assertEqual(_files(paths.data_path(Path("raw_documents"))), [])

    def test_space_below_floor_refuses_before_provider_contact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = ScriptedSource()
            use_case, uow, paths = self._use_case(
                Path(tmp), source, free=999, floor=1_000
            )
            # Local disk state must not spend this candidate's retry budget.
            with self.assertRaises(AcquisitionCapacityError) as raised:
                use_case.execute(DownloadDocumentCommand(candidate=_candidate()))
            self.assertEqual(raised.exception.phase, "admission")
            self.assertEqual(source.calls, 0)
            self.assertEqual(uow.source_accesses.items, {})
            self.assertEqual(_files(paths.runtime_tmp_path()), [])

    def test_sealed_download_is_retained_until_a_durable_copy_holds_it(self) -> None:
        invalid = b"<html>provider error page, not a PDF</html>" * 50
        valid = b"%PDF-1.7\n" + b"z" * 2_000 + b"\n%%EOF\n"

        def tight_under(part: str, size: int) -> Callable[[Path], VolumeSpace]:
            tight = VolumeSpace(available_bytes=size - 1, total_bytes=1 << 40)
            return lambda d: tight if part in d.parts else _ample_space(d)

        class UnwritableQuarantine(RawDocumentStore):
            def quarantine_raw_document(self, **kwargs: object) -> QuarantineResult:
                raise PermissionError(errno.EACCES, "quarantine is not writable")

        class FailingArchive(RawDocumentStore):
            def put_raw_document(self, **kwargs: object) -> RawDocumentWriteResult:
                raise OSError(errno.EIO, "archive device error")

        def seal_read_eio_once():
            real_open = Path.open
            state = {"failed": False}

            def open_(path: Path, mode: str = "r", *args: object, **kwargs: object):
                if path.name.startswith("cninfo_") and mode == "rb" and not state["failed"]:
                    state["failed"] = True
                    raise OSError(errno.EIO, "Input/output error")
                return real_open(path, mode, *args, **kwargs)

            return mock.patch.object(Path, "open", open_)

        cases = {
            # (body, store, probe, error code) — only a complete quarantine
            # copy lets the staged download go.
            "quarantine_complete": (invalid, RawDocumentStore, _ample_space,
                                    "invalid_raw_document"),
            "quarantine_no_room": (invalid, RawDocumentStore,
                                   tight_under("quarantine", len(invalid)),
                                   "invalid_raw_document"),
            "quarantine_unwritable": (invalid, UnwritableQuarantine, _ample_space,
                                      "invalid_raw_document"),
            # The download fits but its archive copy would cross the floor.
            "archive_no_room": (valid, RawDocumentStore, None, "local_space_shortfall"),
            "archive_io_error": (valid, FailingArchive, _ample_space, "io_error"),
            # One transient EIO reading the sealed file says nothing about it.
            "archive_input_eio": (valid, RawDocumentStore, _ample_space, "io_error"),
        }
        for case, (body, store_cls, probe, error_code) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                use_case, uow, paths = self._use_case(
                    Path(tmp),
                    ScriptedSource(*[lambda sink: _deliver(sink, body)] * 2),
                    store_cls=store_cls,
                    probe=probe,
                    free=2 * len(body) - 1 if probe is None else None,
                )
                fault = (
                    seal_read_eio_once() if case == "archive_input_eio"
                    else contextlib.nullcontext()
                )
                logs = (
                    self.assertNoLogs(raw_document_store.LOGGER, "WARNING")
                    if case == "quarantine_complete"
                    else self.assertLogs(raw_document_store.LOGGER, "WARNING")
                )
                with logs, fault:
                    result = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))
                self.assertEqual(result.error_code, error_code)
                self.assertIs(result.retryable, error_code != "invalid_raw_document")
                snapshot = uow.source_accesses.get(result.source_access_id).result_snapshot
                self.assertEqual(snapshot["archive"], {"archive_completed": False})
                retained = _files(paths.runtime_tmp_path())
                if case == "quarantine_complete":
                    self.assertTrue(snapshot["quarantine_complete"])
                    self.assertEqual(retained, [])
                    assert result.quarantined_path is not None
                    self.assertEqual(result.quarantined_path.read_bytes(), body)
                    continue
                # The sealed download is the only complete copy: it stays,
                # findable by name, hash and length from the failure record.
                self.assertEqual(retained, [snapshot["retained_filename"]])
                self.assertEqual(paths.runtime_tmp_path(retained[0]).read_bytes(), body)
                self.assertEqual(
                    (snapshot["retained_raw_file_hash"], snapshot["retained_byte_count"]),
                    ("sha256:" + hashlib.sha256(body).hexdigest(), len(body)),
                )
                if case.startswith("quarantine"):
                    self.assertFalse(snapshot["quarantine_complete"])
                if case == "quarantine_unwritable":
                    self.assertIn("PermissionError", snapshot["quarantine_error"])
                    self.assertIsNone(result.quarantined_path)
                if case == "archive_no_room":
                    self.assertEqual(snapshot["capacity"]["phase"], "archive")
                if case == "archive_input_eio":
                    # Not quarantined, reason path-free, and the existing finite
                    # retry re-downloads and archives the same identity.
                    self.assertIsNone(result.quarantined_path)
                    self.assertEqual(snapshot["reason"], "OSError: [Errno 5] Input/output error")
                    retry = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))
                    self.assertEqual(
                        uow.documents.get(retry.document_id).raw_file_hash,
                        snapshot["retained_raw_file_hash"],
                    )
                    self.assertEqual(len(_files(paths.data_path(Path("raw_documents")))), 1)
                    self.assertEqual(_files(paths.runtime_tmp_path()), retained)

    def test_archive_and_registration_reply_loss_replay_the_same_identity(self) -> None:
        payload = b"%PDF-1.7\nreply loss\n%%EOF\n"
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        command = DownloadDocumentCommand(candidate=_candidate())

        with self.subTest(boundary="archive"), tempfile.TemporaryDirectory() as tmp:
            use_case, uow, paths = self._use_case(
                Path(tmp), ScriptedSource(lambda sink: _deliver(sink, payload))
            )
            real_fsync_dir = raw_document_store._fsync_dir
            lost = {"archive": 1}

            def link_made_then_reply_lost(path: Path) -> None:
                if "raw_documents" in path.parts and lost["archive"]:
                    lost["archive"] -= 1
                    raise OSError(errno.EIO, "directory fsync reply lost")
                real_fsync_dir(path)

            with mock.patch.object(
                raw_document_store, "_fsync_dir", side_effect=link_made_then_reply_lost
            ), self.assertLogs(raw_document_store.LOGGER, "WARNING"):
                first = use_case.execute(command)
            self.assertEqual((first.error_code, first.retryable), ("io_error", True))
            archived = _files(paths.data_path(Path("raw_documents")))
            self.assertEqual(len(archived), 1)
            # Unknown archive outcome: the sealed download is kept, not dropped.
            snapshot = uow.source_accesses.get(first.source_access_id).result_snapshot
            retained = _files(paths.runtime_tmp_path())
            self.assertEqual(retained, [snapshot["retained_filename"]])
            self.assertEqual(snapshot["retained_raw_file_hash"], digest)

            # Replay where the retained file plus a new download leave less room
            # than one more archive copy: the held address is reused, so the
            # replay needs no archive space at all.
            tight = DirectoryVolume(paths.runtime_tmp_path(), free=3 * len(payload) - 1)
            replay = DownloadDocument(
                source=ScriptedSource(lambda sink: _deliver(sink, payload)),
                raw_store=RawDocumentStore(paths, free_floor_bytes=0, space_probe=tight.probe),
                path_builder=paths,
                uow_factory=lambda: uow,
            )
            second = replay.execute(command)
            self.assertIsNotNone(second.document_id)
            self.assertEqual(second.raw_file_hash, digest)
            self.assertEqual(_files(paths.data_path(Path("raw_documents"))), archived)
            self.assertEqual(_files(paths.runtime_tmp_path()), retained)

        class CommitReplyLost(FakeUnitOfWork):
            lost = 1

            def commit(self) -> None:
                super().commit()
                if self.lost:
                    self.lost -= 1
                    raise OperationalError(
                        "COMMIT", {}, Exception("server closed the connection unexpectedly")
                    )

        with self.subTest(boundary="registration"), tempfile.TemporaryDirectory() as tmp:
            use_case, uow, paths = self._use_case(
                Path(tmp),
                ScriptedSource(*[lambda sink: _deliver(sink, payload)] * 2),
                uow=CommitReplyLost(),
            )
            with self.assertRaises(OperationalError):
                use_case.execute(command)
            # The archive already holds the bytes; nothing is destroyed.
            archived = _files(paths.data_path(Path("raw_documents")))
            self.assertEqual(len(archived), 1)
            self.assertEqual(_files(paths.runtime_tmp_path()), [])
            (committed,) = uow.documents.items.values()

            replay = use_case.execute(command)
            self.assertTrue(replay.reused_existing_document)
            self.assertEqual(replay.document_id, committed.document_id)
            self.assertEqual(replay.raw_file_hash, digest)
            self.assertEqual(_files(paths.data_path(Path("raw_documents"))), archived)

    def test_web_channel_streams_through_real_staging_into_the_archive(self) -> None:
        body = b"%PDF-1.7\n" + bytes(range(256)) * 512 + b"\n%%EOF\n"
        pieces = [body[start : start + 40_000] for start in range(0, len(body), 40_000)]
        for encoded in (False, True):
            with self.subTest(encoded=encoded), tempfile.TemporaryDirectory() as tmp:
                requests: list[str | None] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    requests.append(request.headers.get("Accept-Encoding"))
                    if encoded:
                        headers = {"Content-Encoding": "gzip"}
                        return httpx.Response(200, headers=headers, stream=ChunkStream(body))
                    # No Content-Length; the first attempt resets mid-body.
                    fail_at = 2 if len(requests) == 1 else None
                    return httpx.Response(200, stream=ChunkStream(*pieces, fail_at=fail_at))

                source = CninfoWebSource(
                    transport=httpx.MockTransport(handler), sleep=lambda _: None
                )
                use_case, uow, paths = self._use_case(Path(tmp), source)
                # No stage of the real path reads a whole file into memory.
                with mock.patch.object(Path, "read_bytes", side_effect=AssertionError):
                    result = use_case.execute(DownloadDocumentCommand(candidate=_candidate()))
                source.close()
                self.assertEqual(_files(paths.runtime_tmp_path()), [])
                if encoded:
                    self.assertEqual(
                        (result.error_code, result.retryable),
                        ("unsupported_content_encoding", True),
                    )
                    self.assertEqual(requests, ["identity"])
                    self.assertEqual(_files(paths.data_path(Path("raw_documents"))), [])
                    continue
                self.assertEqual(requests, ["identity", "identity"])
                document = uow.documents.get(result.document_id)
                self.assertEqual(
                    document.raw_file_hash, "sha256:" + hashlib.sha256(body).hexdigest()
                )
                self.assertEqual(
                    paths.data_path(Path(document.raw_file_relpath)).read_bytes(), body
                )


class ScriptedSource:
    """Plays one scripted transfer per call against the real staging sink."""

    def __init__(self, *scripts: Callable[[PdfDownloadSink], CompletedPdfTransfer]) -> None:
        self.scripts = list(scripts)
        self.calls = 0

    def search_announcements(
        self, security: SourceSecurity, window: DisclosureWindow
    ) -> list[AnnouncementRef]:
        raise AssertionError("download use case must not search")

    def download_pdf_to(
        self, ref: AnnouncementRef, sink: PdfDownloadSink
    ) -> CompletedPdfTransfer:
        self.calls += 1
        return self.scripts.pop(0)(sink)


def _deliver(sink: PdfDownloadSink, payload: bytes) -> CompletedPdfTransfer:
    sink.begin_attempt(declared_byte_count=len(payload))
    sink.write(payload)
    return CompletedPdfTransfer(byte_count=len(payload), declared_byte_count=len(payload))


def _files(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


class FailingDownloadSource:
    def __init__(self, error: SourceRequestError) -> None:
        self._error = error

    def search_announcements(
        self,
        security: SourceSecurity,
        window: DisclosureWindow,
        categories: tuple[str, ...] | None = None,
    ) -> list[AnnouncementRef]:
        raise AssertionError("download use case must not search")

    def download_pdf_to(
        self, ref: AnnouncementRef, sink: PdfDownloadSink
    ) -> CompletedPdfTransfer:
        raise self._error


class FakeDownloadSource:
    def __init__(self, payloads: list[bytes]) -> None:
        self.payloads = payloads

    def search_announcements(
        self,
        security: SourceSecurity,
        window: DisclosureWindow,
        categories: tuple[str, ...] | None = None,
    ) -> list[AnnouncementRef]:
        raise AssertionError("download use case must not search")

    def download_pdf_to(
        self, ref: AnnouncementRef, sink: PdfDownloadSink
    ) -> CompletedPdfTransfer:
        payload = self.payloads.pop(0)
        sink.begin_attempt(declared_byte_count=len(payload))
        sink.write(payload)
        return CompletedPdfTransfer(
            byte_count=len(payload), declared_byte_count=len(payload)
        )


def _ample_space(_path: Path) -> VolumeSpace:
    return VolumeSpace(available_bytes=1 << 40, total_bytes=1 << 41)


class FakeRawStore:
    def put_raw_document(
        self,
        *,
        provider: str,
        security_code: str,
        year: int | str,
        provider_document_id: str,
        input_file: Path,
        expected_raw_file_hash: str | None = None,
    ) -> RawDocumentWriteResult:
        payload = input_file.read_bytes()
        if not payload.startswith(b"%PDF-"):
            raise InvalidRawDocumentError("input file is not a PDF")
        raw_hash = "sha256:" + hashlib.sha256(payload).hexdigest()
        return RawDocumentWriteResult(
            relpath=Path("raw_documents") / provider / security_code / str(year) / provider_document_id / "sample.pdf",
            raw_file_hash=raw_hash,
            byte_count=len(payload),
            created=True,
        )

    def verify_raw_document(
        self, *, relpath: Path, expected_hash: str
    ) -> RawDocumentVerification:
        raise AssertionError("not used")

    def quarantine_raw_document(
        self,
        *,
        provider: str,
        provider_document_id: str,
        input_file: Path,
        reason: str,
    ) -> QuarantineResult:
        payload = input_file.read_bytes()
        return QuarantineResult(
            path=Path("quarantine") / f"{provider_document_id}.bin",
            reason=reason,
            byte_count=len(payload),
            transfer_complete=True,
            payload_sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
        )

    def open_pdf_download(self, path: Path) -> OwnedPdfDownload:
        return OwnedPdfDownload(
            path, floor=FreeSpaceFloor(path.parent, floor_bytes=0, probe=_ample_space)
        )


class FakePathBuilder:
    def __init__(self) -> None:
        self.root = Path("/private/tmp") / f"download-doc-{ids.new_ulid()}"

    def runtime_tmp_path(self, name: str | None = None) -> Path:
        return self.root / (name or "")

    def raw_document_relpath(self, **_) -> Path:
        raise AssertionError("not used")

    def data_path(self, relpath: Path) -> Path:
        raise AssertionError("not used")

    def parser_artifacts_root_relpath(self, **_) -> Path:
        raise AssertionError("not used")

    def parser_run_artifacts_relpath(self, **_) -> Path:
        raise AssertionError("not used")

    def normalized_ir_relpath(self, **_) -> Path:
        raise AssertionError("not used")

    def normalized_ir_run_relpath(self, **_) -> Path:
        raise AssertionError("not used")

    def document_units_snapshot_relpath(self, **_) -> Path:
        raise AssertionError("not used")

    def runtime_quarantine_path(self, **_) -> Path:
        raise AssertionError("not used")


def _use_case(uow: FakeUnitOfWork, payloads: list[bytes]) -> DownloadDocument:
    return DownloadDocument(
        source=FakeDownloadSource(payloads),
        raw_store=FakeRawStore(),
        path_builder=FakePathBuilder(),
        uow_factory=lambda: uow,
    )


def _uow_with_subject(uow: FakeUnitOfWork | None = None) -> FakeUnitOfWork:
    uow = uow or FakeUnitOfWork()
    company = uow.companies.add(
        e.Company(company_id="co_1", legal_name="P5 Test Co")
    )
    uow.securities.add(
        e.Security(
            security_id="sec_1",
            company_id=company.company_id,
            security_code="T07SYNC",
            exchange="LOCAL",
            status="active",
        )
    )
    return uow


def _uow_with_listed_subject(code: str, exchange: str) -> FakeUnitOfWork:
    uow = FakeUnitOfWork()
    company = uow.companies.add(
        e.Company(company_id="co_listed", legal_name="Listed Test Co")
    )
    uow.securities.add(
        e.Security(
            security_id="sec_listed",
            company_id=company.company_id,
            security_code=code,
            exchange=exchange,
            status="active",
        )
    )
    return uow


def _candidate(*, file_size: int = 2048) -> dict[str, object]:
    return {
        "provider_document_id": "pid-1",
        "title": "P5 Test Annual Report",
        "download_url": "https://static.cninfo.example/pid-1.PDF",
        "raw_category": "010301",
        "filing_type": "other",
        "announcement_date": "2026-07-01",
        "security_code": "T07SYNC",
        "exchange": "LOCAL",
        "security_name": "P5 Test",
        "provider_org_id": "org-p5",
        "object_id": 123,
        "rec_id": "rec-p5",
        "file_signature_hint": {
            "file_size": file_size,
            "etag": None,
            "last_modified": None,
            "index_updated_at": "2026-07-01T12:00:00+08:00",
        },
    }


if __name__ == "__main__":
    unittest.main()
