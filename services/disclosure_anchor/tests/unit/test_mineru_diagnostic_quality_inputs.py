"""Independent held-input lease tests; synthetic phase prerequisites grant no quality claim."""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.parsers.mineru_medium import artifacts as artifacts_module
from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import PinnedArtifactTree
from disclosure_anchor.adapters.runtime import mineru_diagnostic_quality_inputs as inputs_module
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournalError
from disclosure_anchor.adapters.runtime.mineru_diagnostic_phases import DiagnosticPhases
from disclosure_anchor.adapters.runtime.mineru_diagnostic_quality_inputs import HeldQualityInputs, hold_quality_inputs
from disclosure_anchor.domain.errors import ParserOutputContractError
from tests._mineru_held_inputs_fixture import (
    OUTPUT, SOURCE, HeldInputFixture, canonical, descriptor_identity, digest, identity, leaves, private_write,
)


MIB = 1024 * 1024


class HeldQualityInputTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='held-input-independent-')
        self.addCleanup(directory.cleanup)
        self.parent = Path(directory.name).resolve()
        self.serial = 0

    def fixture(self, **kwargs):
        self.serial += 1
        fixture = HeldInputFixture(self.parent / f'fixture-{self.serial}', **kwargs)
        self.addCleanup(fixture.close)
        return fixture

    @contextmanager
    def rejected(self):
        try:
            yield
        except BaseException as error:
            allowed = (DiagnosticJournalError, ParserOutputContractError, OSError, ValueError)
            self.assertTrue(all(isinstance(item, allowed) for item in leaves(error)), repr(error))
        else:
            self.fail('changed ownership/content was accepted')

    def assert_fd_closed(self, fd):
        with self.assertRaises(OSError):
            os.fstat(fd)

    def assert_borrowed_live(self, fixture):
        fixture.resources.checkpoint()
        self.assertEqual(fixture.journal.original_identity.configuration_sha256,
                         digest(canonical(fixture.binding)))

    @contextmanager
    def trace_source(self, fixture, *, after_read=None):
        original_open = fixture.resources.open_payload
        events, streams, requests = [], [], []
        checkpoint = fixture.resources.checkpoint
        def checked():
            events.append('checkpoint')
            return checkpoint()
        @contextmanager
        def open_payload(name, *, identity):
            with original_open(name, identity=identity) as stream:
                self.assertEqual(name, 'source.pdf')
                streams.append(stream)
                read = stream.read
                def observed(size=-1):
                    self.assertGreater(size, 0)
                    requests.append(size)
                    result = read(size)
                    events.append(('read', size, len(result)))
                    if after_read is not None:
                        after_read(stream, len(requests), result)
                    return result
                with patch.object(stream, 'read', side_effect=observed):
                    yield stream
        with patch.object(fixture.resources, 'checkpoint', side_effect=checked), patch.object(
                fixture.resources, 'open_payload', side_effect=open_payload):
            yield events, streams, requests

    @contextmanager
    def recycled(self, fd, name):
        path = self.parent / name
        os.close(fd)
        replacement = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        if replacement != fd:
            os.dup2(replacement, fd)
            os.close(replacement)
        os.write(fd, b'owned replacement must remain open')
        expected = descriptor_identity(fd)
        try:
            yield expected
        finally:
            try:
                current = descriptor_identity(fd)
            except OSError:
                pass
            else:
                if current == expected:
                    os.close(fd)

    def test_factory_holds_original_stream_and_complete_literal_inventory_until_context_exit(self):
        fixture = self.fixture()
        before = fixture.persisted()
        source_identity = tuple(fixture.snapshot['identity'][:2])
        with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases) as held:
            stream, output_fd, tree = held.source, held.output_root_fd, held.output_tree
            source_fd = stream.fileno()
            self.assertEqual(descriptor_identity(source_fd), source_identity)
            self.assertIs(held.source, stream)
            self.assertIs(held.output_tree, tree)
            self.assertEqual(stream.tell(), 0)
            self.assertEqual(stream.read(), SOURCE)
            expected_directories = tuple(map(PurePosixPath, ('.', 'empty', 'parser', 'parser/empty')))
            self.assertEqual(tree.directory_paths, expected_directories)
            self.assertEqual({item.relative_path.as_posix(): (item.size_bytes, item.sha256) for item in tree.files},
                             {name: (len(raw), digest(raw)) for name, raw in OUTPUT.items()})
            self.assertEqual(descriptor_identity(output_fd), tuple(identity(fixture.output_path)[:2]))
            held.verify_unchanged()
            self.assertEqual(stream.tell(), 0)
        self.assertTrue(stream.closed)
        self.assert_fd_closed(source_fd)
        self.assert_fd_closed(output_fd)
        with self.rejected():
            _ = tree.files
        for name in ('source', 'output_root_fd', 'output_tree'):
            with self.subTest(name=name), self.rejected():
                getattr(held, name)
        with self.rejected():
            held.verify_unchanged()
        self.assert_borrowed_live(fixture)
        self.assertEqual(fixture.persisted(), before)

    def test_manual_close_immediately_closes_all_returned_handles_and_context_exit_is_idempotent(self):
        fixture = self.fixture()
        with self.assertRaises(TypeError):
            HeldQualityInputs()
        with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases) as held:
            source, tree, fd = held.source, held.output_tree, held.output_root_fd
            source_fd = source.fileno()
            held.close()
            self.assertTrue(source.closed)
            self.assert_fd_closed(source_fd)
            self.assert_fd_closed(fd)
            with self.rejected():
                _ = tree.files
            held.close()
            with self.rejected():
                _ = held.source
        self.assert_borrowed_live(fixture)

    def test_complete_empty_output_tree_is_valid_without_manufactured_files_or_bytes(self):
        fixture = self.fixture(output_files={}, directories=())
        with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases) as held:
            self.assertEqual(held.output_tree.files, ())
            self.assertEqual(held.output_tree.directory_paths, (PurePosixPath('.'),))
            held.verify_unchanged()
            self.assertEqual(held.source.read(), SOURCE)

    def test_actual_phase_replay_accepts_stale_cache_but_rejects_foreign_journal_or_binding(self):
        fixture = self.fixture(finish=False)
        stale = DiagnosticPhases(fixture.journal, fixture.binding)
        fixture.finish_output()
        self.assertFalse(stale.has('output_sealed'))
        with hold_quality_inputs(resources=fixture.resources, phases=stale) as held:
            self.assertEqual(held.source.read(), SOURCE)
        foreign = self.fixture()
        for phases in (foreign.phases, DiagnosticPhases(fixture.journal, fixture.binding)):
            if phases.journal is fixture.journal:
                phases.binding = {**fixture.binding, 'source_byte_count': len(SOURCE) + 1}
            with self.subTest(same_journal=phases.journal is fixture.journal), patch.object(
                    fixture.resources, 'open_payload', side_effect=AssertionError('invalid authority opened source')):
                with self.rejected():
                    with hold_quality_inputs(resources=fixture.resources, phases=phases):
                        self.fail('foreign phases acquired lease')

    def test_forged_phase_cache_cannot_supply_missing_seal_and_stale_cache_cannot_hide_cleanup(self):
        missing = self.fixture(finish=False)
        full = self.fixture()
        missing.phases.latest.update(full.phases.latest)
        with patch.object(missing.resources, 'open_payload', side_effect=AssertionError('forged cache opened source')):
            with self.rejected():
                with hold_quality_inputs(resources=missing.resources, phases=missing.phases):
                    self.fail('forged cache acquired lease')
        stale = DiagnosticPhases(full.journal, full.binding)
        full.begin_cleanup()
        self.assertFalse(stale.has('cleanup_intent'))
        with patch.object(full.resources, 'open_payload', side_effect=AssertionError('cleanup reopened source')):
            with self.rejected():
                with hold_quality_inputs(resources=full.resources, phases=stale):
                    self.fail('cleanup permitted new lease')

    def test_source_growth_truncation_and_same_size_bytes_fail_before_acquire_and_on_recheck(self):
        for timing in ('before', 'held'):
            for change in ('grow', 'truncate', 'same_size'):
                fixture = self.fixture()
                replacement = SOURCE + b'growth' if change == 'grow' else SOURCE[:-1] if change == 'truncate' else b'x' * len(SOURCE)
                with self.subTest(timing=timing, change=change):
                    if timing == 'before':
                        fixture.source_path.write_bytes(replacement)
                        with self.rejected():
                            with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases):
                                self.fail('changed source acquired lease')
                    else:
                        with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases) as held:
                            source = held.source
                            fixture.source_path.write_bytes(replacement)
                            with self.rejected():
                                held.verify_unchanged()
                        self.assertTrue(source.closed)
                    self.assertEqual(fixture.source_path.read_bytes(), replacement)
                    self.assert_borrowed_live(fixture)

    def test_same_bytes_source_inode_replacement_is_not_adopted_and_original_stream_survives_until_close(self):
        for timing in ('before', 'held'):
            fixture = self.fixture()
            replacement = fixture.root / 'new-source'
            private_write(replacement, SOURCE)
            old_identity = identity(fixture.source_path)
            if timing == 'before':
                replacement.replace(fixture.source_path)
                with self.rejected():
                    with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases):
                        self.fail('replaced source adopted')
            else:
                with self.rejected():
                    with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases) as held:
                        stream = held.source
                        replacement.replace(fixture.source_path)
                        self.assertEqual(stream.read(), SOURCE)
                        with self.rejected():
                            _ = held.source
                        held.verify_unchanged()
                self.assertTrue(stream.closed)
            self.assertNotEqual(identity(fixture.source_path), old_identity)
            self.assertEqual(fixture.source_path.read_bytes(), SOURCE)

    def test_complete_output_detects_outside_subroot_changes_and_missing_or_extra_empty_entries(self):
        for change in ('outside_bytes', 'missing_file', 'extra_file', 'extra_empty_directory', 'missing_empty_directory'):
            fixture = self.fixture()
            with self.subTest(change=change), hold_quality_inputs(resources=fixture.resources, phases=fixture.phases) as held:
                if change == 'outside_bytes':
                    target = fixture.output_path/'outside.bin'
                    target.write_bytes(b'z' * len(OUTPUT['outside.bin']))
                elif change == 'missing_file':
                    (fixture.output_path/'zero.bin').unlink()
                elif change == 'extra_file':
                    private_write(fixture.output_path/'extra.bin', b'')
                elif change == 'extra_empty_directory':
                    (fixture.output_path/'extra-empty').mkdir(mode=0o700)
                else:
                    (fixture.output_path/'empty').rmdir()
                with self.rejected():
                    held.verify_unchanged()

    def test_original_output_root_nested_directory_and_same_byte_file_inodes_are_required(self):
        for location in ('root', 'empty_dir', 'file'):
            fixture = self.fixture()
            if location == 'root':
                target = fixture.output_path
                target.rename(fixture.root/'old-output')
                target.mkdir(mode=0o700)
            elif location == 'empty_dir':
                target = fixture.output_path/'parser/empty'
                target.rename(fixture.root/'old-empty')
                target.mkdir(mode=0o700)
            else:
                target = fixture.output_path/'outside.bin'
                replacement = fixture.root/'same-bytes'
                private_write(replacement, OUTPUT['outside.bin'])
                replacement.replace(target)
            expected = identity(target)
            with self.subTest(location=location), self.rejected():
                with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases):
                    self.fail('output replacement adopted')
            self.assertEqual(identity(target), expected)

    def test_source_and_output_symlink_fifo_hardlink_are_rejected_without_following_or_blocking(self):
        actual_open = os.open
        for location in ('source', 'output'):
            for kind in ('symlink', 'fifo', 'hardlink'):
                fixture = self.fixture()
                target = fixture.source_path if location == 'source' else fixture.output_path/'outside.bin'
                raw = target.read_bytes()
                foreign = fixture.root/'foreign'
                private_write(foreign, raw)
                target.unlink()
                if kind == 'symlink':
                    target.symlink_to(foreign)
                elif kind == 'fifo':
                    os.mkfifo(target, 0o600)
                else:
                    os.link(foreign, target)
                before = target.lstat()
                def bounded_open(name, flags, *args, **kwargs):
                    if os.fspath(name) == target.name:
                        self.assertTrue(flags & os.O_NOFOLLOW)
                        self.assertTrue(flags & os.O_NONBLOCK)
                    return actual_open(name, flags, *args, **kwargs)
                with self.subTest(location=location, kind=kind), patch.object(os, 'open', side_effect=bounded_open):
                    with self.rejected():
                        with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases):
                            self.fail('unsafe object adopted')
                self.assertEqual(target.lstat().st_ino, before.st_ino)
                self.assertEqual(foreign.read_bytes(), raw)

    def test_verify_rehashes_every_original_output_file_and_same_source_handle_without_materializing_tree(self):
        fixture = self.fixture()
        with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases) as held:
            source = held.source
            source.read(3)
            expected = {tuple(identity(fixture.output_path/name)[:2]): name for name in OUTPUT}
            observed = set()
            read = os.read
            def counted(fd, size):
                current = descriptor_identity(fd)
                if current in expected:
                    observed.add(expected[current])
                return read(fd, size)
            with patch.object(artifacts_module.os, 'read', side_effect=counted), patch.object(
                    held.output_tree, 'read_bytes', side_effect=AssertionError('whole tree file materialized')):
                held.verify_unchanged()
            self.assertEqual(observed, set(OUTPUT))
            self.assertIs(held.source, source)
            self.assertEqual(source.tell(), 0)

    def test_source_hashing_reads_remaining_plus_one_with_original_checkpoint_before_and_after_each_read(self):
        fixture = self.fixture(source_bytes=b's' * (2 * MIB + 17))
        with self.trace_source(fixture) as (events, streams, requests):
            with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases) as held:
                self.assertIs(held.source, streams[0])
                self.assertEqual(requests, [MIB, MIB, 18, 1] * 2)
                held.verify_unchanged()
                self.assertEqual(requests, [MIB, MIB, 18, 1] * 4)
            self.assertEqual(len(streams), 1)
            self.assertTrue(streams[0].closed)
        for index, event in enumerate(events):
            if isinstance(event, tuple) and event[0] == 'read':
                self.assertEqual(events[index - 1], 'checkpoint')
                self.assertEqual(events[index + 1], 'checkpoint')

    def test_source_growth_during_hash_stops_after_one_extra_byte_and_closes_acquired_stream(self):
        fixture = self.fixture(source_bytes=b's' * (MIB + 17))
        def grow(_stream, count, _chunk):
            if count == 1:
                with fixture.source_path.open('ab') as out:
                    out.write(b'growing beyond original seal')
        with self.trace_source(fixture, after_read=grow) as (_events, streams, requests):
            with self.rejected():
                with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases):
                    self.fail('growing source accepted')
            self.assertEqual(requests, [MIB, 18])
            self.assertTrue(streams[0].closed)
        self.assert_borrowed_live(fixture)

    def test_deadline_after_first_source_read_stops_next_read_and_closes_before_returning(self):
        fixture = self.fixture(source_bytes=b's' * (MIB + 17))
        def expire(_stream, _count, _chunk):
            fixture.now = fixture.deadline
        with self.trace_source(fixture, after_read=expire) as (_events, streams, requests):
            with self.assertRaises(BaseException) as caught:
                with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases):
                    self.fail('expired source accepted')
            self.assertTrue(all(isinstance(item, TimeoutError) for item in leaves(caught.exception)), repr(caught.exception))
            self.assertEqual(requests, [MIB])
            self.assertTrue(streams[0].closed)
        self.assertEqual(fixture.source_path.stat().st_size, MIB + 17)

    def test_tree_constructor_failure_closes_source_and_output_but_preserves_borrowed_owners(self):
        fixture = self.fixture()
        failure = OSError('independent tree-construction fault')
        opened = []
        def fail_tree(**kwargs):
            opened.append(kwargs['root_fd'])
            raise failure
        with self.trace_source(fixture) as (_events, streams, _requests), patch.object(
                PinnedArtifactTree, 'from_root_fd', side_effect=fail_tree):
            with self.assertRaises(BaseException) as caught:
                with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases):
                    self.fail('failed tree yielded lease')
            self.assertIn(failure, leaves(caught.exception))
            self.assertTrue(streams[0].closed)
            self.assertEqual(len(opened), 1)
            self.assert_fd_closed(opened[0])
        self.assert_borrowed_live(fixture)

    def test_original_callback_oserror_is_preserved_and_acquired_stream_is_closed(self):
        fixture = self.fixture()
        failure = OSError('original owner callback failure')
        actual = fixture.resources.checkpoint
        after = False
        def mark_after(_stream, _count, _chunk):
            nonlocal after
            after = True
        def checkpoint():
            if after:
                raise failure
            return actual()
        with patch.object(fixture.resources, 'checkpoint', side_effect=checkpoint), self.trace_source(
                fixture, after_read=mark_after) as (_events, streams, requests):
            with self.assertRaises(BaseException) as caught:
                with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases):
                    self.fail('callback failure accepted')
            self.assertIn(failure, leaves(caught.exception))
            self.assertTrue(streams[0].closed)
            self.assertEqual(len(requests), 1)
        self.assert_borrowed_live(fixture)

    def test_operation_and_each_independent_cleanup_error_are_preserved_while_all_handles_close(self):
        fixture = self.fixture()
        body_error = OSError('body primary')
        tree_error = OSError('tree close after release')
        output_error = OSError('output close after release')
        source_error = OSError('source close after release')
        close_directory = inputs_module._close_directory
        # Patch owners from an OUTER stack so the production factory, rather
        # than the test, combines the body failure with every cleanup failure.
        with ExitStack() as patches, self.assertRaises(BaseException) as caught:
            with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases) as held:
                source, tree, output_fd = held.source, held.output_tree, held.output_root_fd
                source_fd = source.fileno()
                tree_close, source_close = tree.close, source.close
                def fail_tree_close():
                    tree_close()
                    raise tree_error
                def fail_output_close(fd, original):
                    close_directory(fd, original)
                    raise output_error
                def fail_source_close():
                    source_close()
                    raise source_error
                patches.enter_context(patch.object(tree, 'close', side_effect=fail_tree_close))
                patches.enter_context(patch.object(inputs_module, '_close_directory', side_effect=fail_output_close))
                patches.enter_context(patch.object(source, 'close', side_effect=fail_source_close))
                raise body_error
        actual = leaves(caught.exception)
        for original in (body_error, tree_error, output_error, source_error):
            self.assertIn(original, actual)
        self.assertTrue(source.closed)
        self.assert_fd_closed(source_fd)
        self.assert_fd_closed(output_fd)
        self.assert_borrowed_live(fixture)

    def test_recycled_source_descriptor_replacement_remains_open_after_manual_and_context_close(self):
        fixture = self.fixture()
        with ExitStack() as replacements:
            with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases) as held:
                source = held.source
                fd = source.fileno()
                expected = replacements.enter_context(self.recycled(fd, 'recycled-source'))
                with self.rejected():
                    held.close()
                self.assertTrue(source.closed)
                self.assertEqual(descriptor_identity(fd), expected)
                held.close()
                self.assertEqual(descriptor_identity(fd), expected)
            # The original factory context has now exited, but this test still
            # owns the replacement. Neither close path may close its integer.
            self.assertEqual(descriptor_identity(fd), expected)
        self.assert_borrowed_live(fixture)

    def test_recycled_output_descriptor_replacement_remains_open_and_other_owned_handles_close(self):
        fixture = self.fixture()
        with ExitStack() as replacements:
            with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases) as held:
                source, fd, tree = held.source, held.output_root_fd, held.output_tree
                expected = replacements.enter_context(self.recycled(fd, 'recycled-output'))
                with self.rejected():
                    held.close()
                self.assertEqual(descriptor_identity(fd), expected)
                self.assertTrue(source.closed)
                with self.rejected():
                    _ = tree.files
                held.close()
                self.assertEqual(descriptor_identity(fd), expected)
            self.assertEqual(descriptor_identity(fd), expected)
        self.assert_borrowed_live(fixture)

    def test_borrowed_owner_close_or_resource_root_replacement_revokes_lease_without_adoption(self):
        for kind in ('journal', 'resources', 'resource_root'):
            fixture = self.fixture()
            with self.subTest(kind=kind), self.rejected():
                with hold_quality_inputs(resources=fixture.resources, phases=fixture.phases) as held:
                    source = held.source
                    if kind == 'journal':
                        fixture.journal.close()
                    elif kind == 'resources':
                        fixture.resources.close()
                    else:
                        fixture.resources.path.rename(fixture.root/'old-resources')
                        fixture.resources.path.mkdir(mode=0o700)
                        private_write(fixture.resources.path/'foreign', b'not adopted')
                    with self.rejected():
                        _ = held.output_root_fd
                    held.verify_unchanged()
            self.assertTrue(source.closed)


if __name__ == '__main__':
    unittest.main()
