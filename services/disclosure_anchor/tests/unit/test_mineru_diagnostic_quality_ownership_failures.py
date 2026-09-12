"""Independent regressions for pre-exposure ownership and callback changes.

All owners, files and descriptor replacements belong to new temporary roots.
The reused fixture simulates E1 prerequisites; it executes no source child,
provider, semantic parser or quality qualification.
"""

from contextlib import ExitStack
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_diagnostic_journal as journal_module
from disclosure_anchor.adapters.runtime import mineru_diagnostic_quality_inputs as held_module
from disclosure_anchor.adapters.runtime import mineru_diagnostic_resources as resource_module
from tests._mineru_held_inputs_fixture import HeldInputFixture, descriptor_identity, private_write


def visible_errors(error):
    """Keep original object identity through groups and visible Python chaining."""
    pending, seen, result = [error], set(), []
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        result.append(item)
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)
        cause = item.__cause__
        if cause is not None:
            pending.append(cause)
        elif not item.__suppress_context__ and item.__context__ is not None:
            pending.append(item.__context__)
    return result


class DiagnosticQualityOwnershipFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="independent-quality-ownership-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def assert_closed(self, fd):
        with self.assertRaises(OSError) as caught:
            os.fstat(fd)
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def close_if_original(self, fd, identity):
        # Failed assertions must also clean test-owned originals, never an
        # unrecognised replacement at the same integer descriptor.
        try:
            current = descriptor_identity(fd)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            if current == identity:
                os.close(fd)

    def new_journal(self, root, clock):
        owner = journal_module.DiagnosticJournal(
            root, create=True, attempt_id="ownership-callback-attempt",
            configuration_sha256="sha256:" + "a" * 64,
            clock_identity_sha256="sha256:" + "b" * 64,
            deadline_ns=1_000_000, continuous_ns=clock,
        )
        self.addCleanup(owner.close)
        return owner

    def test_capacity_cannot_succeed_when_original_clock_callback_closes_owner(self):
        owner = None
        armed = False
        calls = []

        def clock():
            nonlocal armed
            calls.append(100)
            if armed:
                armed = False
                owner.close()
            return 100

        root = self.root / "journal"
        owner = self.new_journal(root, clock)
        before = {path.name: path.read_bytes() for path in root.iterdir()}
        root_fd, lock_fd = owner._root_fd, owner._lock_fd
        prior_calls = len(calls)
        armed = True
        with self.assertRaises(journal_module.DiagnosticJournalError):
            owner.require_capacity(additional_records=0, additional_bytes=0)
        self.assertEqual(len(calls), prior_calls + 1)
        self.assert_closed(root_fd)
        self.assert_closed(lock_fd)
        self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, before)
        with self.assertRaises(journal_module.DiagnosticJournalError):
            owner.require_capacity(additional_records=0, additional_bytes=0)
        self.assertEqual(len(calls), prior_calls + 1)

    def test_capacity_rechecks_pending_created_during_clock_without_opening_or_repair(self):
        actual_open = os.open
        for kind in ("regular", "symlink", "fifo"):
            with self.subTest(kind=kind):
                root = self.root / kind
                pending = root / "append-pending.json"
                armed = False

                def clock():
                    nonlocal armed
                    if armed:
                        armed = False
                        if kind == "regular":
                            private_write(pending, b"original unknown append outcome\n")
                        elif kind == "symlink":
                            pending.symlink_to("0000-header.json")
                        else:
                            os.mkfifo(pending, 0o600)
                    return 100

                def reject_pending_open(name, flags, *args, **kwargs):
                    if Path(os.fspath(name)).name == "append-pending.json":
                        self.fail("new pending evidence must be rejected without opening it")
                    return actual_open(name, flags, *args, **kwargs)

                owner = self.new_journal(root, clock)
                before = {path.name: path.read_bytes() for path in root.iterdir()}
                armed = True
                with patch.object(journal_module.os, "open", side_effect=reject_pending_open):
                    with self.assertRaises(journal_module.DiagnosticJournalError):
                        owner.require_capacity(additional_records=0, additional_bytes=0)
                identity = pending.lstat()
                with self.assertRaises(journal_module.DiagnosticJournalError):
                    owner.require_capacity(additional_records=0, additional_bytes=0)
                after = pending.lstat()
                self.assertEqual((after.st_dev, after.st_ino, after.st_mode, after.st_size),
                                 (identity.st_dev, identity.st_ino, identity.st_mode, identity.st_size))
                self.assertEqual({name: (root / name).read_bytes() for name in before}, before)
                self.assertEqual(set(path.name for path in root.iterdir()), set(before) | {pending.name})
                self.assertEqual(owner.records, ())
                if kind == "regular":
                    self.assertEqual(pending.read_bytes(), b"original unknown append outcome\n")
                elif kind == "symlink":
                    self.assertEqual(os.readlink(pending), "0000-header.json")
                owner.close()

    def test_unexposed_stream_initial_fstat_failure_closes_raw_fd_without_second_fstat(self):
        path = self.root / "source.bin"
        path.write_bytes(b"unexposed original bytes\n")
        fd = os.open(path, os.O_RDONLY)
        identity = descriptor_identity(fd)
        self.addCleanup(self.close_if_original, fd, identity)
        primary = OSError(errno.EIO, "original initial identity failure")
        actual_fstat, actual_close = os.fstat, os.close
        stats, closes = [], []

        def failing_fstat(candidate):
            if candidate == fd:
                stats.append(candidate)
                raise primary  # Every later fstat would fail too.
            return actual_fstat(candidate)

        def close(candidate):
            closes.append(candidate)
            return actual_close(candidate)

        with patch.object(resource_module.os, "fstat", side_effect=failing_fstat), \
                patch.object(resource_module.os, "close", side_effect=close), \
                patch.object(resource_module.os, "fdopen", side_effect=AssertionError("must not wrap failed identity")):
            with self.assertRaises(OSError) as caught:
                with resource_module._stream(fd, "rb"):
                    self.fail("failed first identity cannot expose a stream")
        self.assertIs(caught.exception, primary)
        self.assertEqual(stats, [fd])
        self.assertEqual(closes, [fd])
        self.assert_closed(fd)
        self.assertEqual(path.read_bytes(), b"unexposed original bytes\n")

    def test_unexposed_stream_retains_primary_and_consumed_close_error_without_reclosing_replacement(self):
        path, replacement = self.root / "source.bin", self.root / "replacement.bin"
        path.write_bytes(b"original")
        replacement.write_bytes(b"replacement must stay open")
        fd = os.open(path, os.O_RDONLY)
        self.addCleanup(self.close_if_original, fd, descriptor_identity(fd))
        replacement_fd = os.open(replacement, os.O_RDONLY)
        replacement_identity = descriptor_identity(replacement_fd)
        self.addCleanup(self.close_if_original, replacement_fd, replacement_identity)
        self.addCleanup(self.close_if_original, fd, replacement_identity)
        primary = OSError(errno.EIO, "initial identity unavailable")
        cleanup = OSError(errno.EIO, "close consumed original but reported failure")
        actual_fstat, actual_close, actual_dup2 = os.fstat, os.close, os.dup2
        stats, closes = [], []

        def failing_fstat(candidate):
            if candidate == fd:
                stats.append(candidate)
                raise primary
            return actual_fstat(candidate)

        def close(candidate):
            closes.append(candidate)
            actual_close(candidate)
            if candidate == fd:
                actual_dup2(replacement_fd, fd)
                raise cleanup

        with patch.object(resource_module.os, "fstat", side_effect=failing_fstat), \
                patch.object(resource_module.os, "close", side_effect=close):
            with self.assertRaises(BaseExceptionGroup) as caught:
                with resource_module._stream(fd, "rb"):
                    self.fail("failed first identity cannot expose a stream")
        errors = visible_errors(caught.exception)
        self.assertIn(primary, errors)
        self.assertIn(cleanup, errors)
        self.assertEqual(stats, [fd])
        self.assertEqual(closes, [fd])
        self.assertEqual(descriptor_identity(fd), replacement_identity)
        self.assertEqual(descriptor_identity(replacement_fd), replacement_identity)
        self.assertEqual(os.read(fd, 100), b"replacement must stay open")

    def test_failed_stream_wrapper_construction_releases_already_identified_raw_fd(self):
        path = self.root / "source.bin"
        path.write_bytes(b"wrapper allocation input")
        fd = os.open(path, os.O_RDONLY)
        self.addCleanup(self.close_if_original, fd, descriptor_identity(fd))
        primary = MemoryError("original wrapper allocation failure")
        actual_close = os.close
        closes = []

        def close(candidate):
            closes.append(candidate)
            return actual_close(candidate)

        with patch.object(resource_module.os, "fdopen", side_effect=primary), \
                patch.object(resource_module.os, "close", side_effect=close):
            with self.assertRaises(MemoryError) as caught:
                with resource_module._stream(fd, "rb"):
                    self.fail("failed wrapper construction cannot expose a stream")
        self.assertIs(caught.exception, primary)
        self.assertEqual(closes, [fd])
        self.assert_closed(fd)

    def allocation_failure(self, site, *, fail_cleanup):
        fixture = HeldInputFixture(self.root / (site + ("-cleanup" if fail_cleanup else "-plain")))
        self.addCleanup(fixture.close)
        before = fixture.persisted()
        owner_identity = fixture.journal.original_identity
        resource_fd = fixture.resources._fd
        resource_identity = descriptor_identity(resource_fd)
        primary = MemoryError("original " + site + " allocation failure")
        output_error = OSError(errno.EIO, "transferred output close consumed fd but reported failure")
        source_error = OSError(errno.EIO, "transferred source context closed then reported failure")
        captured, exits, closes = {}, [], []
        actual_acquire = held_module.HeldQualityInputs._acquire.__func__
        actual_initialize = held_module.HeldQualityInputs._initialize
        actual_close, actual_dup2 = os.close, os.dup2
        replacement = self.root / (site + "-replacement.bin")
        replacement.write_bytes(b"owned replacement survives consumed close")
        replacement_fd = os.open(replacement, os.O_RDONLY)
        replacement_identity = descriptor_identity(replacement_fd)
        self.addCleanup(self.close_if_original, replacement_fd, replacement_identity)

        class ExitObserver:
            def __init__(self, original):
                self.original = original

            def __exit__(self, *args):
                exits.append(args)
                result = self.original.__exit__(*args)
                if fail_cleanup:
                    raise source_error
                return result

        class InventoryFailure(list):
            def __iter__(self):
                raise primary

        class AllocationFailure:
            def __new__(cls):
                raise primary

        def acquire(cls, **kwargs):
            captured.update(kwargs)
            captured["source_fd"] = kwargs["source"].fileno()
            captured["source_identity"] = descriptor_identity(captured["source_fd"])
            kwargs["source_context"] = ExitObserver(kwargs["source_context"])
            return actual_acquire(cls, **kwargs)

        def initialize(held, **kwargs):
            captured["held"] = held
            if site == "first_attribute":
                raise primary  # Before even the first ordinary ownership field.
            kwargs["inventory"] = InventoryFailure(kwargs["inventory"])
            return actual_initialize(held, **kwargs)

        def close(candidate):
            if captured and candidate == captured["output_fd"]:
                closes.append(candidate)
                actual_close(candidate)
                if fail_cleanup:
                    actual_dup2(replacement_fd, candidate)
                    raise output_error
            else:
                actual_close(candidate)

        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(held_module.HeldQualityInputs, "_acquire", classmethod(acquire)))
                stack.enter_context(patch.object(held_module.os, "close", side_effect=close))
                if site == "object":
                    stack.enter_context(patch.object(held_module, "object", AllocationFailure, create=True))
                else:
                    stack.enter_context(patch.object(held_module.HeldQualityInputs, "_initialize", initialize))
                with self.assertRaises(BaseException) as caught:
                    with held_module.hold_quality_inputs(resources=fixture.resources, phases=fixture.phases):
                        self.fail("allocation failure must not expose a held-input lease")
                # A partly initialized owner that installed its normal close
                # guard must not retry any consumed descriptor on a later close.
                if site == "inventory" and "held" in captured:
                    captured["held"].close()
            self.assertTrue(captured, "actual factory must transfer both real handles")
            errors = visible_errors(caught.exception)
            self.assertIn(primary, errors)
            if fail_cleanup:
                self.assertIn(output_error, errors)
                self.assertIn(source_error, errors)
                self.assertEqual(descriptor_identity(captured["output_fd"]), replacement_identity)
                self.assertEqual(os.read(captured["output_fd"], 100), b"owned replacement survives consumed close")
            else:
                self.assertIs(caught.exception, primary)
                self.assert_closed(captured["output_fd"])
            self.assertEqual(closes, [captured["output_fd"]])
            self.assertEqual(len(exits), 1)
            self.assertTrue(captured["source"].closed)
            self.assert_closed(captured["source_fd"])
            self.assertEqual(descriptor_identity(resource_fd), resource_identity)
            self.assertEqual(fixture.journal.original_identity, owner_identity)
            self.assertEqual(fixture.persisted(), before)
            self.assertEqual(fixture.source_path.read_bytes(), fixture.source_bytes)
            for name, raw in fixture.output_files.items():
                self.assertEqual((fixture.output_path / name).read_bytes(), raw)
        finally:
            # A regression may leak the exact transferred objects. Keep its
            # failure visible, then release only those observed owned originals.
            if captured:
                if not captured["source"].closed:
                    captured["source_context"].__exit__(None, None, None)
                self.close_if_original(captured["source_fd"], captured["source_identity"])
                self.close_if_original(captured["output_fd"], captured["output_acquired"])
                if fail_cleanup:
                    self.close_if_original(captured["output_fd"], replacement_identity)

    def test_inventory_allocation_failure_after_transfer_releases_both_inputs(self):
        self.allocation_failure("inventory", fail_cleanup=False)

    def test_failure_before_first_initialization_attribute_releases_both_inputs(self):
        self.allocation_failure("first_attribute", fail_cleanup=False)

    def test_object_allocation_failure_releases_both_transferred_inputs(self):
        self.allocation_failure("object", fail_cleanup=False)

    def test_allocation_and_each_cleanup_error_survive_without_double_closing_transferred_handles(self):
        for site in ("inventory", "first_attribute", "object"):
            with self.subTest(site=site):
                self.allocation_failure(site, fail_cleanup=True)

    def test_last_accessor_checkpoint_cannot_return_any_handle_under_newly_invalid_owner(self):
        for kind in ("closed", "pending"):
            for accessor in ("source", "output_root_fd", "output_tree"):
                with self.subTest(kind=kind, accessor=accessor):
                    fixture = HeldInputFixture(self.root / (kind + "-" + accessor))
                    self.addCleanup(fixture.close)
                    lease = held_module.hold_quality_inputs(resources=fixture.resources, phases=fixture.phases)
                    held = lease.__enter__()
                    source, output_fd, tree = held.source, held.output_root_fd, held.output_tree
                    source_fd, tree_fd = source.fileno(), tree._root_fd
                    borrowed_fd = fixture.resources._fd
                    borrowed_identity = descriptor_identity(borrowed_fd)
                    before = fixture.persisted()
                    pending = fixture.journal.root / "append-pending.json"
                    actual_verify = fixture.resources._verify_identity
                    active, armed = False, True
                    changes = []

                    def verify(relative, identity):
                        nonlocal active
                        active = relative == "output"
                        try:
                            return actual_verify(relative, identity)
                        finally:
                            active = False

                    def clock():
                        nonlocal armed
                        if active and armed:
                            armed = False
                            changes.append(kind)
                            if kind == "closed":
                                fixture.journal.close()
                            else:
                                private_write(pending, b"late original append uncertainty\n")
                        return fixture.now

                    try:
                        with patch.object(fixture.resources, "_verify_identity", side_effect=verify), \
                                patch.object(fixture.journal, "_clock", side_effect=clock):
                            with self.assertRaises(journal_module.DiagnosticJournalError):
                                getattr(held, accessor)
                        self.assertEqual(changes, [kind])
                    finally:
                        # A revoked original journal also rejects the source
                        # context's final checkpoint. It remains a visible
                        # closure error, and does not waive other handle closes.
                        with self.assertRaises(journal_module.DiagnosticJournalError):
                            lease.__exit__(None, None, None)
                    self.assertTrue(source.closed)
                    for fd in (source_fd, output_fd, tree_fd):
                        self.assert_closed(fd)
                    self.assertEqual(descriptor_identity(borrowed_fd), borrowed_identity)
                    self.assertEqual({name: (fixture.journal.root / name).read_bytes() for name in before}, before)
                    self.assertEqual(fixture.source_path.read_bytes(), fixture.source_bytes)
                    if kind == "pending":
                        self.assertEqual(pending.read_bytes(), b"late original append uncertainty\n")
                    held.close()

    def test_original_deadline_stops_nested_inventory_descent_after_first_opened_parent(self):
        fixture = HeldInputFixture(self.root / "nested-checkpoint",
                                   output_files={"a/b/c/value.bin": b"literal nested bytes\n"},
                                   directories=("a/b/c/",))
        self.addCleanup(fixture.close)
        before = fixture.persisted()
        actual_verify = fixture.resources._verify_identity
        actual_open, actual_close = os.open, os.close
        active, expired = False, False
        opened, closes = [], []
        failure = TimeoutError("original clock expired immediately after first inventory parent open")

        def verify(relative, identity):
            nonlocal active
            active = relative == "output/a/b/c"
            try:
                return actual_verify(relative, identity)
            finally:
                active = False

        def open_directory(name, flags, *args, **kwargs):
            nonlocal expired
            fd = actual_open(name, flags, *args, **kwargs)
            if active:
                opened.append((os.fspath(name), fd, descriptor_identity(fd)))
                expired = True
            return fd

        def close(fd):
            # The integer may have served earlier owners; only count closes
            # after acquisition at the fault boundary.
            if opened and fd == opened[0][1]:
                closes.append(fd)
            return actual_close(fd)

        def clock():
            if expired:
                raise failure
            return fixture.now

        with held_module.hold_quality_inputs(resources=fixture.resources, phases=fixture.phases) as held:
            source = held.source
            borrowed_fd = fixture.resources._fd
            borrowed_identity = descriptor_identity(borrowed_fd)
            with patch.object(fixture.resources, "_verify_identity", side_effect=verify), \
                    patch.object(resource_module.os, "open", side_effect=open_directory), \
                    patch.object(resource_module.os, "close", side_effect=close), \
                    patch.object(fixture.journal, "_clock", side_effect=clock):
                with self.assertRaises(TimeoutError) as caught:
                    held.verify_unchanged()
            self.assertIs(caught.exception, failure)
            self.assertEqual([name for name, _, _ in opened], ["output"])
            self.assertEqual(closes, [opened[0][1]])
            self.assert_closed(opened[0][1])
            self.assertEqual(descriptor_identity(borrowed_fd), borrowed_identity)
            self.assertFalse(source.closed)
            # Restoring the test callback does not change the original start
            # or deadline, and the previous failure must not mutate evidence.
            self.assertEqual(fixture.persisted(), before)
        self.assertTrue(source.closed)
        self.assertEqual((fixture.output_path / "a/b/c/value.bin").read_bytes(), b"literal nested bytes\n")


if __name__ == "__main__":
    unittest.main()
