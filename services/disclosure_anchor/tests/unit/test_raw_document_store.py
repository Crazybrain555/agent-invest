import tempfile
import unittest
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import IO
from unittest import mock

from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage import raw_document_store
from disclosure_anchor.adapters.storage.raw_document_store import (
    RawDocumentStore,
    VolumeSpace,
)
from disclosure_anchor.application.ports.disclosure_source import CompletedPdfTransfer
from disclosure_anchor.application.ports.file_store import (
    AcquisitionCapacityError,
    IncompletePdfDownloadError,
)
from disclosure_anchor.domain.errors import InvalidRawDocumentError, RawDocumentError
from disclosure_anchor.settings import Settings


def _settings(root: Path) -> Settings:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    return Settings(
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
        mineru_model_cache=shared_root / "model_cache" / "mineru",
        hf_home=shared_root / "model_cache" / "huggingface",
        modelscope_cache=shared_root / "model_cache" / "modelscope",
    )


class _Volume:
    """Synthetic volume whose free space shrinks with the files it tracks."""

    def __init__(self, *, free: int, total: int) -> None:
        self.free = free
        self.total = total
        self.tracked: list[Path] = []

    def probe(self, _directory: Path) -> VolumeSpace:
        used = sum(path.stat().st_size for path in self.tracked if path.exists())
        return VolumeSpace(available_bytes=self.free - used, total_bytes=self.total)


def _ample(_directory: Path) -> VolumeSpace:
    return VolumeSpace(available_bytes=1 << 40, total_bytes=1 << 41)


class RawDocumentStoreTests(unittest.TestCase):
    def test_retained_archive_verifies_exact_complete_bytes_without_republishing(self) -> None:
        payload = b"%PDF-1.4\nretained original\n%%EOF\n"
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            store = RawDocumentStore(FileStorePathBuilder(settings))
            source = Path(tmp) / "original.pdf"
            source.write_bytes(payload)
            archive = store.put_raw_document(
                provider="cninfo", security_code="300114", year=2024,
                provider_document_id="pid-1", input_file=source,
            )

            located = store.locate_retained_raw_document(
                provider="cninfo", security_code="300114", year=2024,
                provider_document_id="pid-1",
            )
            self.assertEqual(located.relpath, archive.relpath)
            self.assertEqual(located.raw_file_hash, "sha256:" + hashlib.sha256(payload).hexdigest())
            self.assertEqual(located.byte_count, len(payload))
            checked = store.verify_retained_raw_document(
                relpath=located.relpath, expected_hash=located.raw_file_hash,
                expected_byte_count=located.byte_count,
            )
            self.assertFalse(checked.created)
            self.assertEqual(checked.relpath, archive.relpath)
            self.assertEqual((settings.disclosure_data_root / "data" / archive.relpath).read_bytes(), payload)

            with self.assertRaises(RawDocumentError):
                store.verify_retained_raw_document(
                    relpath=located.relpath, expected_hash=located.raw_file_hash,
                    expected_byte_count=len(payload) + 1,
                )

    def test_retained_archive_refuses_ambiguous_or_linked_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            store = RawDocumentStore(FileStorePathBuilder(settings))
            source = Path(tmp) / "original.pdf"
            source.write_bytes(b"%PDF-1.4\none\n%%EOF\n")
            archive = store.put_raw_document(
                provider="cninfo", security_code="300114", year=2024,
                provider_document_id="pid-1", input_file=source,
            )
            archive_path = settings.disclosure_data_root / "data" / archive.relpath
            other = b"%PDF-1.4\ntwo\n%%EOF\n"
            other_path = archive_path.parent / ("sha256_" + hashlib.sha256(other).hexdigest() + ".pdf")
            other_path.write_bytes(other)
            with self.assertRaises(RawDocumentError):
                store.locate_retained_raw_document(
                    provider="cninfo", security_code="300114", year=2024,
                    provider_document_id="pid-1",
                )

            other_path.unlink()
            outside = Path(tmp) / "outside.pdf"
            outside.write_bytes(source.read_bytes())
            archive_path.unlink()
            archive_path.symlink_to(outside)
            with self.assertRaises(RawDocumentError):
                store.locate_retained_raw_document(
                    provider="cninfo", security_code="300114", year=2024,
                    provider_document_id="pid-1",
                )
            with self.assertRaises(RawDocumentError):
                store.verify_retained_raw_document(
                    relpath=archive.relpath, expected_hash=archive.raw_file_hash,
                    expected_byte_count=len(source.read_bytes()),
                )

    def test_retained_archive_fifo_refusal_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            store = RawDocumentStore(FileStorePathBuilder(settings))
            source = Path(tmp) / "original.pdf"
            source.write_bytes(b"%PDF-1.4\noriginal\n%%EOF\n")
            archive = store.put_raw_document(
                provider="cninfo", security_code="300114", year=2024,
                provider_document_id="pid-1", input_file=source,
            )
            archive_path = settings.disclosure_data_root / "data" / archive.relpath
            archive_path.unlink()
            os.mkfifo(archive_path)
            child = """
import sys
from pathlib import Path
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.domain.errors import RawDocumentError
from tests.unit.test_raw_document_store import _settings
store = RawDocumentStore(FileStorePathBuilder(_settings(Path(sys.argv[1]))))
try:
    store.verify_retained_raw_document(
        relpath=Path(sys.argv[2]), expected_hash=sys.argv[3],
        expected_byte_count=int(sys.argv[4]),
    )
except RawDocumentError:
    raise SystemExit(0)
raise SystemExit(1)
"""
            try:
                completed = subprocess.run(
                    [sys.executable, "-c", child, tmp, str(archive.relpath),
                     archive.raw_file_hash, str(archive.byte_count)],
                    env=os.environ.copy(), check=False, capture_output=True,
                    timeout=2,
                )
            except subprocess.TimeoutExpired:
                self.fail("retained verification blocked on a FIFO instead of refusing it")
            self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))

    def test_put_verify_and_reuse_existing_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            store = RawDocumentStore(FileStorePathBuilder(settings))
            input_file = Path(tmp) / "sample.pdf"
            input_file.write_bytes(b"%PDF-1.4\nsample\n%%EOF\n")

            first = store.put_raw_document(
                provider="local",
                security_code="002484",
                year=2025,
                provider_document_id="local-001",
                input_file=input_file,
            )
            self.assertTrue(first.created)
            self.assertEqual(first.relpath.name, first.raw_file_hash.replace(":", "_") + ".pdf")
            self.assertTrue((settings.disclosure_data_root / "data" / first.relpath).is_file())

            verification = store.verify_raw_document(
                relpath=first.relpath, expected_hash=first.raw_file_hash
            )
            self.assertTrue(verification.ok)

            second = store.put_raw_document(
                provider="local",
                security_code="002484",
                year=2025,
                provider_document_id="local-001",
                input_file=input_file,
            )
            self.assertFalse(second.created)
            self.assertEqual(second.relpath, first.relpath)
            self.assertEqual(second.raw_file_hash, first.raw_file_hash)

    def test_rejects_non_pdf_before_raw_archive_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            store = RawDocumentStore(FileStorePathBuilder(settings))
            input_file = Path(tmp) / "not.pdf"
            input_file.write_bytes(b"not a pdf")

            with self.assertRaises(InvalidRawDocumentError):
                store.put_raw_document(
                    provider="local",
                    security_code="002484",
                    year=2025,
                    provider_document_id="local-002",
                    input_file=input_file,
                )

            raw_root = settings.disclosure_data_root / "data" / "raw_documents"
            self.assertFalse(
                any(path.is_file() for path in raw_root.rglob("*"))
                if raw_root.exists()
                else False
            )

    def test_quarantine_is_complete_only_for_a_verified_streamed_copy(self) -> None:
        # Three 1 MiB reads: the copy streams and is verified as it goes.
        payload = b"not a pdf " + bytes(range(256)) * 12_300
        real_read = os.read

        def fail_second_read() -> mock.Mock:
            calls = {"n": 0}

            def read(fd: int, size: int) -> bytes:
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError(errno.EIO, "input device error")
                return real_read(fd, size)

            return mock.Mock(side_effect=read)

        def grow_input_after_first_read(path: Path) -> mock.Mock:
            calls = {"n": 0}

            def read(fd: int, size: int) -> bytes:
                calls["n"] += 1
                chunk = real_read(fd, size)
                if calls["n"] == 1:
                    with path.open("ab") as handle:
                        handle.write(b"appended while copying")
                return chunk

            return mock.Mock(side_effect=read)

        cases = ("complete", "missing", "read_error", "changed", "floor", "directory")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                settings = _settings(Path(tmp))
                volume = _Volume(free=len(payload) - 1, total=1 << 30)
                store = RawDocumentStore(
                    FileStorePathBuilder(settings),
                    free_floor_bytes=0,
                    space_probe=volume.probe if case == "floor" else _ample,
                )
                input_file = Path(tmp) / "bad.pdf"
                if case == "directory":
                    input_file.mkdir()
                elif case != "missing":
                    input_file.write_bytes(payload)
                patches = {
                    "read_error": fail_second_read(),
                    "changed": grow_input_after_first_read(input_file),
                }
                with (
                    mock.patch.object(raw_document_store.os, "read", patches[case])
                    if case in patches
                    else mock.patch.object(Path, "read_bytes", side_effect=AssertionError)
                ):
                    result = store.quarantine_raw_document(
                        provider="local",
                        provider_document_id="local-003",
                        input_file=input_file,
                        reason="invalid_raw_document",
                    )
                manifest = json.loads(
                    result.path.with_suffix(result.path.suffix + ".json").read_text()
                )
                leftovers = [p.name for p in result.path.parent.iterdir() if ".tmp-" in p.name]
                self.assertEqual(leftovers, [])
                if case == "complete":
                    self.assertTrue(result.transfer_complete)
                    self.assertEqual(result.path.read_bytes(), payload)
                    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
                    self.assertEqual(result.payload_sha256, digest)
                    self.assertEqual(result.byte_count, len(payload))
                    self.assertEqual(
                        (manifest["payload_complete"], manifest["payload_sha256"]),
                        (True, digest),
                    )
                    continue
                # Anything less than a verified copy is an explicit empty
                # marker; it never claims to be the input.
                self.assertFalse(result.transfer_complete)
                self.assertIsNone(result.payload_sha256)
                self.assertEqual((result.byte_count, result.path.read_bytes()), (0, b""))
                self.assertFalse(manifest["payload_complete"])
                self.assertTrue(manifest["copy_error"])
                self.assertIs(result.input_missing, case == "missing")
                self.assertIs(manifest["input_missing"], case == "missing")
                if case in {"read_error", "floor"}:
                    self.assertEqual(input_file.read_bytes(), payload)

    def test_archive_copy_separates_storage_failures_from_invalid_input(self) -> None:
        # Two 1 MiB reads, so the floor is rechecked before a later chunk.
        payload = b"%PDF-1.7\n" + b"B" * (1024 * 1024 + 64)
        real_open = Path.open

        class _Faulty:
            """A real handle whose writes hit a full disk and reads fail after the first."""

            def __init__(self, handle: IO[bytes]) -> None:
                self._handle = handle
                self._reads = 0

            def __enter__(self) -> "_Faulty":
                return self

            def __exit__(self, *exc: object) -> None:
                self._handle.close()

            def write(self, chunk: bytes) -> int:
                raise OSError(errno.ENOSPC, "No space left on device")

            def read(self, size: int = -1) -> bytes:
                self._reads += 1
                if self._reads > 1:
                    raise OSError(errno.EIO, "Input/output error")
                return self._handle.read(size)

        def faulty_open(case: str, input_file: Path):
            # Input opens: first for the hash pass, second for the archive copy.
            opens = {"input": 0}

            def open_(path: Path, mode: str = "r", *args: object, **kwargs: object):
                if path == input_file and mode == "rb":
                    opens["input"] += 1
                    if (case, opens["input"]) in {("input_open_eio", 1), ("input_copy_open_eio", 2)}:
                        raise OSError(errno.EIO, "Input/output error")
                    if (case, opens["input"]) == ("input_later_read_eio", 2):
                        return _Faulty(real_open(path, mode, *args, **kwargs))
                handle = real_open(path, mode, *args, **kwargs)
                if case == "write_enospc" and mode == "xb" and path.name.startswith("raw_"):
                    return _Faulty(handle)
                return handle

            return mock.patch.object(Path, "open", open_)

        def refuse_after_first_chunk(tmp_root: Path) -> mock.Mock:
            def probe(_directory: Path) -> VolumeSpace:
                copied = sum(p.stat().st_size for p in tmp_root.glob("raw_*.tmp"))
                return VolumeSpace(
                    available_bytes=0 if copied >= 1024 * 1024 else 1 << 30,
                    total_bytes=1 << 30,
                )

            return mock.Mock(side_effect=probe)

        # Local I/O on either side of the copy is an OSError; only facts about
        # the input itself make it invalid.
        storage_errno = {
            "write_enospc": errno.ENOSPC, "fsync_eio": errno.EIO, "input_stat_eio": errno.EIO,
            "input_open_eio": errno.EIO, "input_copy_open_eio": errno.EIO,
            "input_later_read_eio": errno.EIO,
        }
        invalid = ("invalid_first", "wrong_hash", "missing_input")
        cases = (*storage_errno, *invalid, "floor_first", "floor_later_chunk", "reuse_needs_no_space")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                refuse_all = mock.Mock(
                    return_value=VolumeSpace(available_bytes=0, total_bytes=1 << 30)
                )
                paths = FileStorePathBuilder(_settings(Path(tmp)))
                tmp_root = paths.runtime_tmp_path()
                raw_root = paths.data_path(Path("raw_documents"))
                input_file = Path(tmp) / "input.pdf"
                original = b"<html>not a pdf</html>" if case == "invalid_first" else payload
                if case != "missing_input":
                    input_file.write_bytes(original)
                probe = (
                    refuse_all
                    if case in (*invalid, "floor_first", "reuse_needs_no_space")
                    else refuse_after_first_chunk(tmp_root)
                    if case == "floor_later_chunk"
                    else mock.Mock(side_effect=_ample)
                )
                if case == "reuse_needs_no_space":
                    first = RawDocumentStore(paths, free_floor_bytes=0, space_probe=_ample)
                    archived = first.put_raw_document(
                        provider="cninfo", security_code="300114", year=2024,
                        provider_document_id="pid-1", input_file=input_file,
                    )
                store = RawDocumentStore(paths, free_floor_bytes=0, space_probe=probe)
                real_stat = Path.stat

                def failing_stat(path: Path, *args: object, **kwargs: object):
                    if path == input_file:
                        raise OSError(errno.EIO, "Input/output error")
                    return real_stat(path, *args, **kwargs)

                fault = (
                    mock.patch.object(
                        raw_document_store.os, "fsync",
                        side_effect=OSError(errno.EIO, "Input/output error"),
                    )
                    if case == "fsync_eio"
                    else mock.patch.object(Path, "stat", failing_stat)
                    if case == "input_stat_eio"
                    else faulty_open(case, input_file)
                )
                with fault:
                    try:
                        result = store.put_raw_document(
                            provider="cninfo", security_code="300114", year=2024,
                            provider_document_id="pid-1", input_file=input_file,
                            expected_raw_file_hash=(
                                "sha256:" + "0" * 64 if case == "wrong_hash" else None
                            ),
                        )
                        raised: Exception | None = None
                    except Exception as exc:  # noqa: BLE001 - the class is the assertion
                        raised = exc
                self.assertEqual(sorted(p.name for p in tmp_root.glob("raw_*")), [])
                if case == "reuse_needs_no_space":
                    self.assertIsNone(raised)
                    self.assertFalse(result.created)
                    self.assertEqual(result.relpath, archived.relpath)
                    probe.assert_not_called()
                    continue
                self.assertFalse(raw_root.exists())
                if case in invalid:
                    self.assertIsInstance(raised, InvalidRawDocumentError)
                    probe.assert_not_called()
                elif case.startswith("floor"):
                    self.assertIsInstance(raised, AcquisitionCapacityError)
                    assert isinstance(raised, AcquisitionCapacityError)
                    self.assertEqual(raised.phase, "archive")
                    self.assertEqual(
                        raised.required_bytes,
                        len(payload) if case == "floor_first" else len(payload) - 1024 * 1024,
                    )
                else:
                    # A failed read or write is never an InvalidRawDocumentError.
                    self.assertIs(type(raised), OSError)
                    assert isinstance(raised, OSError)
                    self.assertEqual(raised.errno, storage_errno[case])
                    if case in ("input_stat_eio", "input_open_eio"):
                        probe.assert_not_called()
                if case != "missing_input":
                    self.assertEqual(input_file.read_bytes(), original)

    def test_download_staging_restarts_each_attempt_and_seals_exact_bytes(self) -> None:
        final = b"%PDF-1.7\nfinal body\n%%EOF\n"
        with tempfile.TemporaryDirectory() as tmp:
            paths = FileStorePathBuilder(_settings(Path(tmp)))
            store = RawDocumentStore(paths, free_floor_bytes=0, space_probe=_ample)
            path = paths.runtime_tmp_path("cninfo_staging.pdf")
            path.parent.mkdir(parents=True)
            staging = store.open_pdf_download(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                store.open_pdf_download(path)
            with self.assertRaises(RuntimeError):
                staging.write(b"before any attempt")

            staging.begin_attempt(declared_byte_count=None)
            staging.write(b"%PDF-1.7\nstale partial from a failed attempt")
            staging.begin_attempt(declared_byte_count=len(final))
            staging.write(final[:9])
            staging.write(final[9:])
            with mock.patch.object(
                raw_document_store, "_fsync_dir", wraps=raw_document_store._fsync_dir
            ) as synced:
                sealed = staging.seal(
                    CompletedPdfTransfer(byte_count=len(final), declared_byte_count=len(final))
                )
            # The retained name must survive a crash, not only its bytes.
            synced.assert_called_once_with(path.parent)

            self.assertEqual(staging.attempts, 2)
            self.assertEqual(path.read_bytes(), final)
            self.assertEqual(
                (sealed.path, sealed.raw_file_hash, sealed.byte_count),
                (path, "sha256:" + hashlib.sha256(final).hexdigest(), len(final)),
            )
            # Sealed bytes are material: close keeps them until discarded.
            with self.assertLogs(raw_document_store.LOGGER, "WARNING") as retained:
                self.assertEqual(staging.close(), path)
            self.assertIn("cninfo_staging.pdf", retained.output[0])
            self.assertEqual(path.read_bytes(), final)
            staging.discard()
            self.assertFalse(path.exists())

            partial = store.open_pdf_download(path)
            partial.begin_attempt(declared_byte_count=None)
            partial.write(b"abc")
            with self.assertRaises(IncompletePdfDownloadError):
                partial.seal(CompletedPdfTransfer(byte_count=4, declared_byte_count=None))
            self.assertIsNone(partial.close())
            self.assertFalse(path.exists())

            replaced = store.open_pdf_download(path)
            replaced.begin_attempt(declared_byte_count=None)
            replaced.write(final)
            replaced.seal(CompletedPdfTransfer(byte_count=len(final), declared_byte_count=None))
            path.unlink()
            path.write_bytes(b"someone else's file")
            replaced.discard()
            self.assertEqual(path.read_bytes(), b"someone else's file")

    def test_free_floor_bounds_physical_growth_not_document_size(self) -> None:
        floor, headroom, chunk = 1_000, 8_192, 4_096
        with tempfile.TemporaryDirectory() as tmp:
            paths = FileStorePathBuilder(_settings(Path(tmp)))
            tmp_root = paths.runtime_tmp_path()
            tmp_root.mkdir(parents=True)
            volume = _Volume(free=floor + headroom, total=1 << 30)
            store = RawDocumentStore(paths, free_floor_bytes=floor, space_probe=volume.probe)

            # A document exactly as large as the headroom lands; one byte more
            # is refused. Nothing else about its size matters.
            for size, fits in ((headroom, True), (headroom + 1, False)):
                path = paths.runtime_tmp_path(f"size-{size}.pdf")
                volume.tracked = [path]
                staging = store.open_pdf_download(path)
                staging.begin_attempt(declared_byte_count=None)
                refused: AcquisitionCapacityError | None = None
                try:
                    for start in range(0, size, chunk):
                        staging.write(b"x" * min(chunk, size - start))
                except AcquisitionCapacityError as exc:
                    refused = exc
                self.assertIs(refused is None, fits)
                if refused is not None:
                    self.assertEqual(refused.phase, "download")
                    self.assertEqual(refused.floor_bytes, floor)
                    # Growth stopped before the chunk that would cross the floor.
                    self.assertLessEqual(path.stat().st_size, headroom)
                staging.close()
                self.assertFalse(path.exists())

            path = paths.runtime_tmp_path("declared.pdf")
            volume.tracked = [path]
            staging = store.open_pdf_download(path)
            with self.assertRaises(AcquisitionCapacityError) as declared:
                staging.begin_attempt(declared_byte_count=headroom + 1)
            self.assertEqual(declared.exception.phase, "declared_length")
            staging.close()

            volume.free = floor - 1
            with self.assertRaises(AcquisitionCapacityError) as admission:
                store.open_pdf_download(paths.runtime_tmp_path("admission.pdf"))
            self.assertEqual(admission.exception.phase, "admission")
            self.assertFalse(paths.runtime_tmp_path("admission.pdf").exists())

            # Unset floor: 10% of the volume stays free.
            unset_floor = raw_document_store.FreeSpaceFloor(
                tmp_root,
                floor_bytes=None,
                probe=lambda _d: VolumeSpace(available_bytes=1_500, total_bytes=10_000),
            )
            unset_floor.require(500, phase="archive")
            with self.assertRaises(AcquisitionCapacityError) as unset:
                unset_floor.require(501, phase="archive")
            self.assertEqual(unset.exception.floor_bytes, 1_000)


if __name__ == "__main__":
    unittest.main()
