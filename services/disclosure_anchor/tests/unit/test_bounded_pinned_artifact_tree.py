"""Independent tests for bounded tree IO, original descriptors and checkpoints."""
from __future__ import annotations

import errno
import os
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.adapters.parsers.mineru_medium import artifacts as module
from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import PinnedArtifactTree
from disclosure_anchor.domain.errors import ParserOutputContractError
from tests._bounded_pinned_tree_fixture import (
    LITERAL_BYTE_COUNT, LITERAL_DIRECTORIES, LITERAL_ENTRY_COUNT, LITERAL_FILES,
    TracedScandir, digest, errors_in, identity, make_tree,
)


class BoundedPinnedArtifactTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.parent = Path(self.directory.name)
        self.root = self.parent / "output"
        make_tree(self.root)
        self.caller_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.caller_identity = identity(self.caller_fd)
        self.addCleanup(os.close, self.caller_fd)

    def tree(self, *, checkpoint=lambda: None, max_entries=LITERAL_ENTRY_COUNT):
        return PinnedArtifactTree.from_root_fd(
            display_root=self.root, root_fd=self.caller_fd,
            checkpoint=checkpoint, max_entries=max_entries,
            max_files=3, max_bytes=LITERAL_BYTE_COUNT,
            require_private_modes=True, allow_empty_directories=True,
        )

    def assert_caller_fd(self) -> None:
        self.assertEqual(identity(self.caller_fd), self.caller_identity)

    def source_fd(self, raw=b"abcdef"):
        path = self.parent / "stream.bin"
        path.write_bytes(raw)
        fd = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        return path, fd

    def test_exact_literal_tree_matches_default_sorted_inventory_and_preserves_caller_fd(self) -> None:
        with PinnedArtifactTree.open_path(self.root, allow_empty_directories=True) as baseline:
            old = tuple((item.relative_path.as_posix(), item.size_bytes, item.sha256) for item in baseline.files)
        with self.tree(checkpoint=lambda: object()) as tree:
            actual = tuple((item.relative_path.as_posix(), item.size_bytes, item.sha256) for item in tree.files)
            self.assertEqual(actual, tuple((name, len(raw), digest(raw)) for name, raw in sorted(LITERAL_FILES.items())))
            self.assertEqual(actual, old)
            self.assertEqual(tuple(path.as_posix() for path in tree.directory_paths), LITERAL_DIRECTORIES)
            self.assert_caller_fd()
            self.assertIsNone(tree.verify_contents_unchanged())
        self.assert_caller_fd()
        for name, raw in LITERAL_FILES.items():
            self.assertEqual((self.root / name).read_bytes(), raw)

    def test_invalid_new_keywords_reject_before_any_new_descriptor(self) -> None:
        variants = ({"checkpoint": 1}, {"checkpoint": False}, {"max_entries": True},
                    {"max_entries": 0}, {"max_entries": -1}, {"max_entries": 1.5})
        for parameters in variants:
            for entry in ("constructor", "open_path", "from_root_fd"):
                with self.subTest(parameters=parameters, entry=entry):
                    with mock.patch.object(module.os, "open") as opened, mock.patch.object(module.os, "dup") as duplicated:
                        with self.assertRaises((ParserOutputContractError, TypeError, ValueError)):
                            if entry == "constructor":
                                PinnedArtifactTree(display_root=self.root, root_fd=self.caller_fd, **parameters)
                            elif entry == "open_path":
                                PinnedArtifactTree.open_path(self.root, **parameters)
                            else:
                                PinnedArtifactTree.from_root_fd(display_root=self.root, root_fd=self.caller_fd, **parameters)
                        opened.assert_not_called()
                        duplicated.assert_not_called()
                    self.assert_caller_fd()

    def test_entry_limit_counts_root_empty_directories_and_files_without_rescan_overcharge(self) -> None:
        with self.assertRaises(ParserOutputContractError):
            self.tree(max_entries=LITERAL_ENTRY_COUNT - 1)
        self.assert_caller_fd()
        with self.tree() as tree:
            for _ in range(3):
                tree.verify_unchanged()
                tree.verify_pinned_topology_unchanged()
                tree.verify_contents_unchanged()
        self.assert_caller_fd()
        empty = self.parent / "empty-root"
        empty.mkdir(mode=0o700)
        with PinnedArtifactTree.open_path(empty, checkpoint=lambda: None, max_entries=1) as tree:
            self.assertEqual(tree.directory_paths, (PurePosixPath("."),))
            self.assertEqual(tree.files, ())
            tree.verify_contents_unchanged()

    def test_bounded_enumeration_uses_real_scandir_and_closes_iterators(self) -> None:
        trace = TracedScandir(os.scandir)
        with mock.patch.object(module.os, "scandir", side_effect=trace), mock.patch.object(
            module.os, "listdir", side_effect=AssertionError("unbounded listdir in checkpointed path"),
        ):
            with self.tree() as tree:
                tree.verify_unchanged()
                tree.verify_contents_unchanged()
        trace.assert_all_closed(self)
        self.assert_caller_fd()

    def test_metadata_entry_limit_stops_incremental_foreign_directory_enumeration(self) -> None:
        with self.tree() as tree:
            for index in range(12):
                (self.root / f"foreign-{index:02d}").mkdir(mode=0o700)
            trace = TracedScandir(os.scandir)
            with mock.patch.object(module.os, "scandir", side_effect=trace):
                with self.assertRaises(ParserOutputContractError):
                    tree.verify_pinned_topology_unchanged()
            self.assertGreater(trace.entries_seen, 0)
            self.assertLessEqual(trace.entries_seen, LITERAL_ENTRY_COUNT)
            trace.assert_all_closed(self)
        self.assert_caller_fd()
        self.assertTrue((self.root / "foreign-11").is_dir())

    def test_constructor_callback_error_is_original_and_scandir_always_closes(self) -> None:
        for error in (OSError("original checkpoint IO"), ValueError("original checkpoint value")):
            with self.subTest(error=type(error).__name__):
                trace = TracedScandir(os.scandir)

                def checkpoint():
                    if trace.entries_seen >= 2:
                        raise error

                with mock.patch.object(module.os, "scandir", side_effect=trace):
                    with self.assertRaises(type(error)) as caught:
                        self.tree(checkpoint=checkpoint)
                self.assertIs(caught.exception, error)
                self.assertEqual(trace.entries_seen, 2)
                trace.assert_all_closed(self)
                self.assert_caller_fd()

    def test_metadata_callback_cancel_closes_iterator_without_consuming_caller_fd(self) -> None:
        trace = TracedScandir(os.scandir)
        armed = False
        error = TimeoutError("original deadline expired during names")

        def checkpoint():
            if armed and trace.entries_seen >= 2:
                raise error

        with self.tree(checkpoint=checkpoint) as tree:
            armed = True
            with mock.patch.object(module.os, "scandir", side_effect=trace):
                with self.assertRaises(TimeoutError) as caught:
                    tree.verify_unchanged()
            self.assertIs(caught.exception, error)
            self.assertEqual(trace.entries_seen, 2)
            trace.assert_all_closed(self)
        self.assert_caller_fd()

    def test_close_does_not_consult_expired_checkpoint(self) -> None:
        calls = 0
        expired = False

        def checkpoint():
            nonlocal calls
            calls += 1
            if expired:
                raise TimeoutError("expired close-only context")

        tree = self.tree(checkpoint=checkpoint)
        self.addCleanup(tree.close)
        expired = True
        before = calls
        tree.close()
        self.assertEqual(calls, before)
        self.assert_caller_fd()
        with self.assertRaises(ParserOutputContractError):
            _ = tree.files

    def test_digest_short_reads_request_only_remaining_plus_one_and_checkpoint_each_read(self) -> None:
        _path, fd = self.source_fd()
        original_read = os.read
        events = []
        requests = []

        def checkpoint():
            events.append("checkpoint")
            return object()

        def read(source_fd, count):
            self.assertEqual(source_fd, fd)
            requests.append(count)
            value = original_read(source_fd, min(2, count))
            events.append(("read", value))
            return value

        with mock.patch.object(module.os, "read", side_effect=read):
            result = module._stream_digest(fd, checkpoint=checkpoint, maximum_bytes=6)
        self.assertEqual(result, (digest(b"abcdef"), 6, b"ab"))
        self.assertEqual(requests, [7, 5, 3, 1])
        for index, event in enumerate(events):
            if isinstance(event, tuple):
                self.assertEqual(events[index - 1], "checkpoint")
                self.assertEqual(events[index + 1], "checkpoint")
        self.assertEqual(os.fstat(fd).st_size, 6)

    def test_digest_caps_each_read_at_one_mebibyte_with_exact_last_byte_probe(self) -> None:
        raw = b"a" * (1024 * 1024) + b"b"
        _path, fd = self.source_fd(raw)
        original_read = os.read
        requests = []

        def read(source_fd, count):
            requests.append(count)
            return original_read(source_fd, count)

        with mock.patch.object(module.os, "read", side_effect=read):
            result = module._stream_digest(fd, checkpoint=lambda: None, maximum_bytes=len(raw))
        self.assertEqual(result, (digest(raw), len(raw), b"a" * 16))
        self.assertEqual(requests, [1024 * 1024, 2, 1])
        self.assertEqual(os.fstat(fd).st_size, len(raw))

    def test_digest_growth_detects_one_extra_byte_and_never_reads_again(self) -> None:
        path, fd = self.source_fd()
        original_read = os.read
        requests = []
        extra_returned = False

        def read(source_fd, count):
            nonlocal extra_returned
            self.assertFalse(extra_returned, "read continued after the overbound byte")
            requests.append(count)
            raw = original_read(source_fd, count)
            if not raw:
                extra_returned = True
                return b"X"  # controlled stream growth at the exact-size EOF probe
            return raw

        with mock.patch.object(module.os, "read", side_effect=read):
            with self.assertRaises(ParserOutputContractError):
                module._stream_digest(fd, checkpoint=lambda: None, maximum_bytes=6)
        self.assertTrue(extra_returned)
        self.assertEqual(requests, [7, 1])
        self.assertEqual(path.read_bytes(), b"abcdef")
        self.assertEqual(os.fstat(fd).st_size, 6)

    def test_digest_real_truncation_rejects_early_eof_without_reading_to_new_limit(self) -> None:
        path, fd = self.source_fd()
        original_read = os.read
        requests = []

        def read(source_fd, count):
            requests.append(count)
            value = original_read(source_fd, min(2, count))
            if len(requests) == 1:
                path.write_bytes(b"ab")
            return value

        with mock.patch.object(module.os, "read", side_effect=read):
            with self.assertRaises(ParserOutputContractError):
                module._stream_digest(fd, checkpoint=lambda: None, maximum_bytes=6)
        self.assertGreaterEqual(len(requests), 1)
        self.assertLessEqual(len(requests), 2)
        self.assertEqual(requests[0], 7)
        self.assertEqual(path.read_bytes(), b"ab")
        self.assertEqual(os.fstat(fd).st_size, 2)

    def test_digest_deadline_after_first_short_read_blocks_the_second_read(self) -> None:
        _path, fd = self.source_fd()
        original_read = os.read
        reads = []
        error = TimeoutError("original absolute deadline")

        def read(source_fd, count):
            reads.append(count)
            return original_read(source_fd, min(2, count))

        def checkpoint():
            if reads:
                raise error

        with mock.patch.object(module.os, "read", side_effect=read):
            with self.assertRaises(TimeoutError) as caught:
                module._stream_digest(fd, checkpoint=checkpoint, maximum_bytes=6)
        self.assertIs(caught.exception, error)
        self.assertEqual(reads, [7])
        self.assertEqual(os.fstat(fd).st_size, 6)

    def test_digest_callback_oserror_after_read_is_not_wrapped_as_content_failure(self) -> None:
        _path, fd = self.source_fd()
        original_read = os.read
        reads = []
        error = OSError("callback owner identity failure")

        def read(source_fd, count):
            reads.append(count)
            return original_read(source_fd, count)

        def checkpoint():
            if reads:
                raise error

        with mock.patch.object(module.os, "read", side_effect=read):
            with self.assertRaises(OSError) as caught:
                module._stream_digest(fd, checkpoint=checkpoint, maximum_bytes=6)
        self.assertIs(caught.exception, error)
        self.assertEqual(reads, [7])

    def test_zero_byte_digest_still_has_one_bounded_eof_probe_and_checks_after_it(self) -> None:
        _path, fd = self.source_fd(b"")
        original_read = os.read
        events = []

        def checkpoint():
            events.append("checkpoint")

        def read(source_fd, count):
            self.assertEqual(count, 1)
            events.append("read")
            return original_read(source_fd, count)

        with mock.patch.object(module.os, "read", side_effect=read):
            self.assertEqual(module._stream_digest(fd, checkpoint=checkpoint, maximum_bytes=0),
                             (digest(b""), 0, b""))
        position = events.index("read")
        self.assertEqual(events[position - 1], "checkpoint")
        self.assertEqual(events[position + 1], "checkpoint")
        self.assertEqual(events.count("read"), 1)

    def test_read_bytes_uses_pinned_size_not_callers_larger_limit_for_growth_probe(self) -> None:
        with self.tree() as tree:
            target = self.root / "sub/b.txt"
            target_identity = (target.stat().st_dev, target.stat().st_ino)
            original_read = os.read
            requests = []
            extra_returned = False

            def read(fd, count):
                nonlocal extra_returned
                if identity(fd) != target_identity:
                    return original_read(fd, count)
                self.assertFalse(extra_returned, "public read continued after growth")
                requests.append(count)
                raw = original_read(fd, count)
                if not raw:
                    extra_returned = True
                    return b"X"
                return raw

            with mock.patch.object(module.os, "read", side_effect=read):
                with self.assertRaises(ParserOutputContractError):
                    tree.read_bytes(PurePosixPath("sub/b.txt"), max_bytes=1024 * 1024)
            self.assertEqual(requests, [7, 1])
            self.assertEqual(target.read_bytes(), b"bravo\n")
        self.assert_caller_fd()

    def test_utf8_read_is_bounded_and_preserves_callback_exception(self) -> None:
        armed = False
        reads = []
        error = ValueError("original owner checkpoint")

        def checkpoint():
            if armed and reads:
                raise error

        with self.tree(checkpoint=checkpoint) as tree:
            original_read = os.read

            def read(fd, count):
                reads.append(count)
                return original_read(fd, count)

            armed = True
            with mock.patch.object(module.os, "read", side_effect=read):
                with self.assertRaises(ValueError) as caught:
                    tree.validate_utf8(PurePosixPath("sub/b.txt"), label="literal")
            self.assertIs(caught.exception, error)
            self.assertEqual(reads, [7])
        self.assert_caller_fd()

    def test_checkpointed_json_uses_bounded_bytes_and_literal_object(self) -> None:
        with self.tree() as tree:
            with mock.patch.object(tree, "read_bytes", wraps=tree.read_bytes) as read, mock.patch.object(
                module.json, "load", side_effect=AssertionError("checkpointed JSON used unbounded stream load"),
            ):
                result = tree.load_json(PurePosixPath("a.json"), label="literal", max_bytes=13)
            self.assertEqual(result, {"answer": 7})
            self.assertEqual(read.call_count, 1)
            self.assertEqual(read.call_args.args[0], PurePosixPath("a.json"))
            self.assertEqual(read.call_args.kwargs["max_bytes"], 13)
        self.assert_caller_fd()

    def test_checkpointed_json_preserves_original_content_rejections(self) -> None:
        for index, raw in enumerate((b'{"answer":NaN}', b"\xff", b"{")):
            with self.subTest(raw=raw):
                root = self.parent / f"invalid-json-{index}"
                root.mkdir(mode=0o700)
                path = root / "value.json"
                path.write_bytes(raw)
                for checkpoint in (None, lambda: None):
                    with PinnedArtifactTree.open_path(root, checkpoint=checkpoint) as tree:
                        with self.assertRaises(ParserOutputContractError):
                            tree.load_json(PurePosixPath("value.json"), label="invalid literal")
                self.assertEqual(path.read_bytes(), raw)

    def test_checkpoint_after_json_parse_keeps_original_callback_valueerror(self) -> None:
        parsed = False
        error = ValueError("owner failed after JSON CPU work")

        def checkpoint():
            if parsed:
                raise error

        with self.tree(checkpoint=checkpoint) as tree:
            original_loads = module.json.loads

            def loads(*args, **kwargs):
                nonlocal parsed
                result = original_loads(*args, **kwargs)
                parsed = True
                return result

            with mock.patch.object(module.json, "loads", side_effect=loads):
                with self.assertRaises(ValueError) as caught:
                    tree.load_json(PurePosixPath("a.json"), label="literal")
            self.assertIs(caught.exception, error)
        self.assert_caller_fd()

    def test_callback_that_closes_tree_cannot_return_successful_read(self) -> None:
        armed = False
        reads = []
        owner = None

        def checkpoint():
            if armed and reads:
                owner.close()

        owner = self.tree(checkpoint=checkpoint)
        self.addCleanup(owner.close)
        original_read = os.read

        def read(fd, count):
            reads.append(count)
            return original_read(fd, count)

        armed = True
        with mock.patch.object(module.os, "read", side_effect=read):
            with self.assertRaises(ParserOutputContractError):
                owner.read_bytes(PurePosixPath("sub/b.txt"), max_bytes=6)
        self.assertEqual(reads, [7])
        self.assert_caller_fd()

    def test_content_verification_rehashes_every_file_without_allocating_read_bytes(self) -> None:
        with self.tree() as tree:
            expected = {(path.stat().st_dev, path.stat().st_ino) for path in (self.root / name for name in LITERAL_FILES)}
            observed = set()
            original_read = os.read

            def read(fd, count):
                raw = original_read(fd, count)
                if raw:
                    observed.add(identity(fd))
                self.assertLessEqual(count, 1024 * 1024)
                return raw

            with mock.patch.object(module.os, "read", side_effect=read), mock.patch.object(
                tree, "read_bytes", side_effect=AssertionError("content check allocated full file payload"),
            ):
                self.assertIsNone(tree.verify_contents_unchanged())
            self.assertEqual(observed, expected)
        self.assert_caller_fd()

    def test_contents_check_detects_real_changed_bytes_under_explicit_static_metadata_fault(self) -> None:
        target = self.root / "sub/b.txt"
        with self.tree() as tree:
            before = target.stat()
            original_from_stat = module._FileIdentity.from_stat
            pinned = original_from_stat(before)
            target.write_bytes(b"BRAVO\n")

            def hold_metadata(observed):
                if (observed.st_dev, observed.st_ino) == (before.st_dev, before.st_ino):
                    return pinned
                return original_from_stat(observed)

            # Only a fault seam for the hash oracle: real new bytes, deliberately
            # frozen metadata. It is not evidence that real FS timestamps froze.
            with mock.patch.object(module._FileIdentity, "from_stat", side_effect=hold_metadata):
                tree.verify_unchanged()
                with self.assertRaises(ParserOutputContractError):
                    tree.verify_contents_unchanged()
            self.assertEqual(target.read_bytes(), b"BRAVO\n")
        self.assert_caller_fd()

    def test_failed_close_invalidates_owner_before_fd_number_is_reused(self) -> None:
        tree = self.tree()
        original_close = os.close
        replacement = self.parent / "recycled.bin"
        replacement.write_bytes(b"do not close the replacement")
        replacement_fd = os.open(replacement, os.O_RDONLY)
        recycled = None
        calls = []
        error = OSError(errno.EIO, "close result uncertain after release")
        try:
            def close_and_recycle(fd):
                nonlocal recycled
                calls.append(fd)
                self.assertNotEqual(fd, self.caller_fd)
                original_close(fd)
                os.dup2(replacement_fd, fd)
                recycled = fd
                raise error

            with mock.patch.object(module.os, "close", side_effect=close_and_recycle):
                with self.assertRaises(BaseException) as caught:
                    tree.close()
                self.assertIn(error, errors_in(caught.exception))
                tree.close()
            self.assertEqual(len(calls), 1)
            self.assertIsNotNone(recycled)
            self.assertEqual(identity(recycled), identity(replacement_fd))
            with self.assertRaises(ParserOutputContractError):
                _ = tree.files
            self.assert_caller_fd()
        finally:
            if recycled is not None:
                try:
                    original_close(recycled)
                except OSError as cleanup:
                    if cleanup.errno != errno.EBADF:
                        raise
            else:
                tree.close()
            original_close(replacement_fd)

    def test_constructor_error_and_close_error_preserve_originals_without_retrying_recycled_fd(self) -> None:
        original_dup, original_close = os.dup, os.close
        replacement = self.parent / "constructor-recycled.bin"
        replacement.write_bytes(b"foreign to failed tree")
        replacement_fd = os.open(replacement, os.O_RDONLY)
        duplicated = []
        owner_fd = None
        recycled = None
        closes = []
        primary = TimeoutError("deadline expired after descriptor acquisition")
        cleanup_error = OSError(errno.EIO, "failed constructor close after release")
        tree = object.__new__(PinnedArtifactTree)
        try:
            def dup(fd):
                nonlocal owner_fd
                result = original_dup(fd)
                duplicated.append(result)
                if owner_fd is None:
                    owner_fd = result
                return result

            def checkpoint():
                if duplicated:
                    raise primary

            def close(fd):
                nonlocal recycled
                closes.append(fd)
                original_close(fd)
                if fd == owner_fd and recycled is None:
                    os.dup2(replacement_fd, fd)
                    recycled = fd
                    raise cleanup_error

            with mock.patch.object(module.os, "dup", side_effect=dup), mock.patch.object(module.os, "close", side_effect=close):
                with self.assertRaises(BaseException) as caught:
                    tree.__init__(display_root=self.root, root_fd=self.caller_fd,
                                  checkpoint=checkpoint, max_entries=LITERAL_ENTRY_COUNT,
                                  allow_empty_directories=True)
                self.assertIn(primary, errors_in(caught.exception))
                self.assertIn(cleanup_error, errors_in(caught.exception))
                tree.close()
            self.assertIsNotNone(recycled)
            self.assertEqual(closes.count(owner_fd), 1)
            self.assertEqual(identity(recycled), identity(replacement_fd))
            self.assert_caller_fd()
        finally:
            if recycled is not None:
                try:
                    original_close(recycled)
                except OSError as cleanup:
                    if cleanup.errno != errno.EBADF:
                        raise
            original_close(replacement_fd)


    def test_growth_after_open_validation_cannot_replace_the_original_hash_size_pin(self) -> None:
        root = self.parent / "growth-window"
        root.mkdir(mode=0o700)
        path = root / "data.bin"
        path.write_bytes(b"abc")
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        original_root_identity = identity(root_fd)
        original_open = module._open_regular_at
        changed = False

        def opened(*args, **kwargs):
            nonlocal changed
            fd = original_open(*args, **kwargs)
            if not changed:
                changed = True
                with path.open("ab") as writer:
                    writer.write(b"DEFGH")
            return fd

        try:
            with mock.patch.object(module, "_open_regular_at", side_effect=opened), mock.patch.object(
                module.os, "read", wraps=os.read,
            ) as read:
                with self.assertRaises(ParserOutputContractError):
                    with PinnedArtifactTree.from_root_fd(
                        display_root=root, root_fd=root_fd, max_files=1, max_bytes=3,
                        max_entries=2, checkpoint=lambda: None,
                    ):
                        pass
                self.assertTrue(changed)
                read.assert_not_called()
            self.assertEqual(path.read_bytes(), b"abcDEFGH")
            self.assertEqual(identity(root_fd), original_root_identity)
        finally:
            os.close(root_fd)

    def test_parent_descent_close_failure_preserves_reused_prior_fd_and_closes_child(self) -> None:
        original_dup, original_close = os.dup, os.close
        original_open = module._open_directory_at
        replacement = self.parent / "descent-replacement.bin"
        replacement.write_bytes(b"independent replacement descriptor")
        replacement_fd = os.open(replacement, os.O_RDONLY)
        prior_fd = None
        child_fds = []
        closed = []
        recycled = False
        error = OSError(errno.EIO, "prior directory close consumed descriptor")

        def dup(fd):
            nonlocal prior_fd
            duplicated = original_dup(fd)
            if prior_fd is None:
                prior_fd = duplicated
            return duplicated

        def opened(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            child_fds.append(fd)
            return fd

        def close(fd):
            nonlocal recycled
            closed.append(fd)
            original_close(fd)
            if fd == prior_fd and not recycled:
                os.dup2(replacement_fd, fd)
                recycled = True
                raise error

        try:
            with mock.patch.object(module.os, "dup", side_effect=dup), mock.patch.object(
                module, "_open_directory_at", side_effect=opened,
            ), mock.patch.object(module.os, "close", side_effect=close):
                with self.assertRaises(BaseException) as caught:
                    module._open_parent_directory(
                        self.caller_fd, ("sub",), root_device=self.caller_identity[0],
                    )
            self.assertIn(error, errors_in(caught.exception))
            self.assertTrue(recycled)
            self.assertEqual(closed.count(prior_fd), 1)
            self.assertEqual(identity(prior_fd), identity(replacement_fd))
            self.assertEqual(len(child_fds), 1)
            for child_fd in child_fds:
                self.assertEqual(closed.count(child_fd), 1)
                with self.assertRaises(OSError) as closed_error:
                    os.fstat(child_fd)
                self.assertEqual(closed_error.exception.errno, errno.EBADF)
            self.assert_caller_fd()
        finally:
            # Every descriptor below was created by this test or its own call.
            # A broken implementation may leak a child or consume the recycled
            # descriptor; cleanup records neither condition as a passing proof.
            for fd in set(child_fds + ([prior_fd] if prior_fd is not None else [])):
                try:
                    original_close(fd)
                except OSError as cleanup:
                    if cleanup.errno != errno.EBADF:
                        raise
            original_close(replacement_fd)

    def test_open_payload_is_closed_when_its_parent_release_fails(self) -> None:
        original_open = module._open_regular_at
        original_close = os.close
        payload_fds = []
        after_payload_closes = []
        injected_parent = None
        error = OSError(errno.EIO, "payload already open when parent release failed")

        def opened(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            payload_fds.append(fd)
            return fd

        def close(fd):
            nonlocal injected_parent
            if payload_fds:
                after_payload_closes.append(fd)
            original_close(fd)
            if payload_fds and fd != payload_fds[-1] and injected_parent is None:
                injected_parent = fd
                raise error

        with self.tree() as tree:
            try:
                with mock.patch.object(module, "_open_regular_at", side_effect=opened), mock.patch.object(
                    module.os, "close", side_effect=close,
                ), mock.patch.object(module.os, "read", wraps=os.read) as read:
                    with self.assertRaises(BaseException) as caught:
                        tree.read_bytes(PurePosixPath("sub/b.txt"), max_bytes=6)
                    read.assert_not_called()
                self.assertIn(error, errors_in(caught.exception))
                self.assertIsNotNone(injected_parent)
                self.assertEqual(len(payload_fds), 1)
                self.assertEqual(after_payload_closes.count(injected_parent), 1)
                for fd in payload_fds:
                    self.assertEqual(after_payload_closes.count(fd), 1)
                    with self.assertRaises(OSError) as closed_error:
                        os.fstat(fd)
                    self.assertEqual(closed_error.exception.errno, errno.EBADF)
                self.assert_caller_fd()
            finally:
                for fd in set(payload_fds):
                    try:
                        original_close(fd)
                    except OSError as cleanup:
                        if cleanup.errno != errno.EBADF:
                            raise

    def test_json_postparse_real_mutation_cannot_return_pre_mutation_object(self) -> None:
        target = self.root / "a.json"
        changed = False
        original_loads = module.json.loads

        def loads(*args, **kwargs):
            nonlocal changed
            value = original_loads(*args, **kwargs)
            self.assertEqual(value, {"answer": 7})
            target.write_bytes(b'{"answer":8}\n')
            changed = True
            return value

        with self.tree() as tree:
            with mock.patch.object(module.json, "loads", side_effect=loads):
                with self.assertRaises(ParserOutputContractError):
                    tree.load_json(PurePosixPath("a.json"), label="mutation", max_bytes=13)
            self.assertTrue(changed)
            self.assertEqual(target.read_bytes(), b'{"answer":8}\n')
            self.assert_caller_fd()


    def test_reopen_stops_after_first_parent_when_original_checkpoint_expires(self) -> None:
        root = self.parent / "nested-reopen"
        (root / "a/b/c").mkdir(parents=True)
        (root / "a/b/c/value.bin").write_bytes(b"abc")
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        root_identity = identity(root_fd)
        expired = False
        error = TimeoutError("original deadline expired after opening a")
        opened_directories = []
        opened_fds = []
        original_open = module._open_directory_at

        def checkpoint():
            if expired:
                raise error

        def opened(*args, **kwargs):
            nonlocal expired
            fd = original_open(*args, **kwargs)
            relative = kwargs["relative"].as_posix()
            opened_directories.append(relative)
            opened_fds.append(fd)
            if relative == "a":
                expired = True
            return fd

        try:
            with PinnedArtifactTree.from_root_fd(
                display_root=root, root_fd=root_fd, checkpoint=checkpoint,
                max_entries=5, max_files=1, max_bytes=3,
            ) as tree:
                with mock.patch.object(module, "_open_directory_at", side_effect=opened), mock.patch.object(
                    module.os, "read", wraps=os.read,
                ) as read:
                    with self.assertRaises(TimeoutError) as caught:
                        tree.read_bytes(PurePosixPath("a/b/c/value.bin"), max_bytes=3)
                    read.assert_not_called()
                self.assertIs(caught.exception, error)
                self.assertEqual(opened_directories, ["a"])
                for fd in opened_fds:
                    with self.assertRaises(OSError) as closed_error:
                        os.fstat(fd)
                    self.assertEqual(closed_error.exception.errno, errno.EBADF)
                self.assertEqual(identity(root_fd), root_identity)
            self.assertEqual(identity(root_fd), root_identity)
        finally:
            os.close(root_fd)


if __name__ == "__main__":
    unittest.main()
