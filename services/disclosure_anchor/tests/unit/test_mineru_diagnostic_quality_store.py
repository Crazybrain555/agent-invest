"""Independent retained-store IO tests; synthetic E1 phases confer no runtime result."""

from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import signal
import stat
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_diagnostic_quality_store as store_module
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournalError
from disclosure_anchor.adapters.runtime.mineru_diagnostic_phases import DiagnosticPhases
from disclosure_anchor.adapters.runtime.mineru_diagnostic_quality_store import (
    BoundedQualitySink, DiagnosticQualityWriteError, OwnedQualityStore,
    create_quality_store, reopen_quality_store,
)
from disclosure_anchor.application.contracts.mineru_diagnostic_quality import H2ByteBudget
from tests._mineru_quality_config_fixture import sha
from tests._mineru_quality_store_fixture import QualityStoreFixture


SLOTS = (
    'input.json', 'producer.request.json', 'producer.source.json', 'producer.build.json',
    'producer.reads.json', 'producer.control.raw', 'producer.stderr.raw',
    'verifier.request.json', 'verifier.source.json', 'verifier.build.json',
    'verifier.reads.json', 'verifier.control.raw', 'verifier.stderr.raw',
    'comparison.json', 'qualification.json', 'result.json',
)


def budget(**changes):
    return {'semantic_record_bytes': 262144, 'build_record_bytes': 43, 'comparison_evidence_bytes': 47,
            'child_control_bytes': 53, 'child_stderr_bytes': 59, 'retained_total_bytes': 524288, **changes}


def inode(path):
    info = Path(path).stat(follow_symlinks=False)
    return info.st_dev, info.st_ino


def identity(path):
    info = Path(path).stat(follow_symlinks=False)
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


def error_chain(error):
    result, pending, seen = [], [error], set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        result.append(item)
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)
        if item.__cause__ is not None:
            pending.append(item.__cause__)
    return result


@contextmanager
def nonblocking_bound():
    # A replacement FIFO must not hang the deterministic test process. This is
    # a failure alarm, not an accepted timeout/ownership result from the store.
    def alarm(_number, _frame):
        raise AssertionError('retained-store operation blocked on a replacement object')
    previous = signal.signal(signal.SIGALRM, alarm)
    prior_timer = signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *prior_timer)
        signal.signal(signal.SIGALRM, previous)


class RetainedStoreIOTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='independent-quality-store-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.serial = 0

    def fixture(self, **kwargs):
        self.serial += 1
        kwargs.setdefault('budget', budget())
        value = QualityStoreFixture(self.root / f'case-{self.serial}', **kwargs)
        self.addCleanup(value.close)
        return value

    def create(self, fixture):
        value = create_quality_store(journal=fixture.journal, phases=fixture.phases)
        self.addCleanup(value.close)
        return value

    def seal_all(self, store):
        # Empty files are actual empty bytes. These storage seals are not
        # semantic role records, quality_input_sealed or a final disposition.
        return tuple(store.seal(slot, evidence_kind='complete') for slot in SLOTS)

    def capture_create(self, fixture):
        opened = []
        real_open, real_fstat = os.open, os.fstat
        def observe(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            info = real_fstat(fd)
            opened.append((fd, (info.st_dev, info.st_ino)))
            return fd
        with patch.object(store_module.os, 'open', observe):
            store = self.create(fixture)
        expected = {inode(fixture.root), inode(store.path), *(inode(store.path / name) for name in SLOTS)}
        held = {}
        for fd, acquired in opened:
            try:
                now = real_fstat(fd)
            except OSError:
                continue
            if (now.st_dev, now.st_ino) == acquired and acquired in expected:
                held[fd] = acquired
        self.assertEqual(set(held.values()), expected)
        self.assertEqual(len(held), 18)
        return store, held

    def test_factory_durably_records_original_root_and_all_16_empty_fds_before_return(self):
        fixture = self.fixture()
        fixture.root.chmod(0o755)  # Existing parent mode is observed, not rewritten.
        before = tuple(record.step for record in fixture.journal.records)
        append = DiagnosticPhases.append
        fsync = os.fsync
        synced, cuts = set(), []
        root = fixture.root / 'journal.quality'
        def sync(fd):
            result = fsync(fd)
            info = os.fstat(fd)
            synced.add((info.st_dev, info.st_ino))
            return result
        def witness(phases, step, value):
            if step == 'quality_intent':
                self.assertFalse(root.exists())
            elif step == 'quality_root_created':
                self.assertEqual(list(root.iterdir()), [])
                self.assertIn(inode(root), synced)
                self.assertIn(inode(fixture.root), synced)
            elif step == 'quality_files_created':
                self.assertEqual(sorted(path.name for path in root.iterdir()), sorted(SLOTS))
                self.assertEqual([item['slot'] for item in value['files']], list(SLOTS))
                for item in value['files']:
                    path = root / item['slot']
                    self.assertEqual(tuple(item['identity']), identity(path))
                    self.assertEqual(path.stat().st_size, 0)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.assertIn(inode(path), synced)
            result = append(phases, step, value)
            if step.startswith('quality_'):
                cuts.append(step)
                self.assertEqual(phases.journal.records[-1].step, step)
            return result
        with patch.object(DiagnosticPhases, 'append', witness), patch.object(store_module.os, 'fsync', sync):
            store = self.create(fixture)
        self.assertEqual(cuts, ['quality_intent', 'quality_root_created', 'quality_files_created'])
        self.assertEqual(tuple(record.step for record in fixture.journal.records), before + tuple(cuts))
        self.assertEqual(store.path, root)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(fixture.root.stat().st_mode), 0o755)
        self.assertEqual(store.retained_bytes, 0)
        self.assertEqual(store.budget, H2ByteBudget(**budget()))
        fresh = DiagnosticPhases(fixture.journal, fixture.binding)
        self.assertEqual(fresh.value('quality_intent')['retained_parent_identity'], list(identity(fixture.root)))
        self.assertEqual(fresh.value('quality_intent')['configuration_sha256'], fixture.journal.original_identity.configuration_sha256)

    def test_authority_comes_from_same_original_journal_binding_and_fresh_replay(self):
        first, second = self.fixture(), self.fixture()
        with self.assertRaises(DiagnosticJournalError):
            create_quality_store(journal=first.journal, phases=second.phases)
        self.assertFalse((first.root / 'journal.quality').exists())
        first.phases.latest.clear()
        first.phases.history.clear()
        store = self.create(first)  # Cache destruction cannot delete durable prerequisites.
        self.assertEqual(store.path.parent, first.root)
        wrong = deepcopy(second.binding)
        wrong['owned_quality']['budget']['retained_total_bytes'] += 1
        second.phases.binding = wrong
        with self.assertRaises(DiagnosticJournalError):
            create_quality_store(journal=second.journal, phases=second.phases)
        self.assertFalse((second.root / 'journal.quality').exists())

    def test_incomplete_prerequisites_wrong_name_existing_root_and_duplicate_owner_refuse_effects(self):
        unfinished = self.fixture(finish=False)
        with self.assertRaises(DiagnosticJournalError):
            create_quality_store(journal=unfinished.journal, phases=unfinished.phases)
        self.assertFalse((unfinished.root / 'journal.quality').exists())
        wrong_parent = self.root / f'case-{self.serial + 1}'
        with self.assertRaises(DiagnosticJournalError):
            wrong_name = self.fixture(retained_name='foreign.quality')
            create_quality_store(journal=wrong_name.journal, phases=wrong_name.phases)
        self.assertFalse((wrong_parent / 'foreign.quality').exists())
        existing = self.fixture()
        reserved = existing.root / 'journal.quality'
        reserved.mkdir(mode=0o700)
        saved = identity(reserved)
        with self.assertRaises((DiagnosticJournalError, OSError)):
            create_quality_store(journal=existing.journal, phases=existing.phases)
        self.assertEqual(identity(reserved), saved)
        self.assertEqual(list(reserved.iterdir()), [])
        live = self.fixture()
        self.create(live)
        before = live.persisted()
        with self.assertRaises(DiagnosticJournalError):
            create_quality_store(journal=live.journal, phases=live.phases)
        self.assertEqual(live.persisted(), before)
        for constructor in (OwnedQualityStore, BoundedQualitySink):
            with self.assertRaises((TypeError, DiagnosticJournalError)):
                constructor()

    def test_creation_cuts_preserve_every_successful_name_and_never_finish_on_reopen(self):
        original_append = DiagnosticPhases.append
        for cut, expected_names in (('quality_root_created', set()), ('quality_files_created', set(SLOTS))):
            fixture = self.fixture()
            original_error = OSError('injected append cut: ' + cut)
            def append(phases, step, value):
                if step == cut:
                    raise original_error
                return original_append(phases, step, value)
            with self.subTest(cut=cut), patch.object(DiagnosticPhases, 'append', append), self.assertRaises(BaseException) as caught:
                create_quality_store(journal=fixture.journal, phases=fixture.phases)
            self.assertIn(original_error, error_chain(caught.exception))
            path = fixture.root / 'journal.quality'
            self.assertEqual({entry.name for entry in path.iterdir()}, expected_names)
            saved = {entry.name: (identity(entry), entry.read_bytes()) for entry in path.iterdir()}
            for factory in (create_quality_store, reopen_quality_store):
                with self.assertRaises(DiagnosticJournalError):
                    factory(journal=fixture.journal, phases=fixture.phases)
            self.assertEqual({entry.name: (identity(entry), entry.read_bytes()) for entry in path.iterdir()}, saved)
            fixture.journal.remaining_seconds()  # Factory closure did not close the borrowed journal.

    def test_partial_slot_creation_and_capacity_refusal_never_delete_or_prewrite(self):
        fixture = self.fixture()
        original_open = os.open
        failure = OSError('fourth exclusive slot open failed')
        def open_file(path, flags, *args, **kwargs):
            if str(path) == SLOTS[3] and flags & os.O_CREAT:
                raise failure
            return original_open(path, flags, *args, **kwargs)
        with patch.object(store_module.os, 'open', open_file), self.assertRaises(BaseException) as caught:
            create_quality_store(journal=fixture.journal, phases=fixture.phases)
        self.assertIn(failure, error_chain(caught.exception))
        root = fixture.root / 'journal.quality'
        self.assertEqual({entry.name for entry in root.iterdir()}, set(SLOTS[:3]))
        self.assertTrue(all(entry.stat().st_size == 0 for entry in root.iterdir()))
        denied = self.fixture()
        before = denied.persisted()
        with patch.object(denied.journal, 'require_capacity', side_effect=DiagnosticJournalError('actual capacity denied')), \
                self.assertRaises(DiagnosticJournalError):
            create_quality_store(journal=denied.journal, phases=denied.phases)
        self.assertEqual(denied.persisted(), before)
        self.assertFalse((denied.root / 'journal.quality').exists())

    def test_slot_mapping_single_writer_retirement_and_no_raw_writer_interface(self):
        fixture = self.fixture(budget=budget(semantic_record_bytes=7, build_record_bytes=11,
            comparison_evidence_bytes=13, child_control_bytes=17, child_stderr_bytes=19, retained_total_bytes=1000))
        store = self.create(fixture)
        expected = (13, 17, 7, 11, 13, 17, 19, 17, 7, 11, 13, 17, 19, 13, 13, 13)
        self.assertEqual(tuple(store.remaining(slot) for slot in SLOTS), expected)
        sink = store.open_new_sink('producer.source.json')
        for name in ('seek', 'truncate', 'fileno'):
            self.assertFalse(hasattr(sink, name))
        self.assertEqual(sink.write(b''), 0)
        with self.assertRaises(DiagnosticJournalError):
            store.open_new_sink('producer.source.json')
        sink.close()
        sink.close()
        with self.assertRaises(DiagnosticJournalError):
            sink.write(b'x')
        with self.assertRaises(DiagnosticJournalError):
            store.open_new_sink('producer.source.json')
        self.assertEqual(store.retained_bytes, 0)
        self.assertEqual(store.seal('producer.source.json', evidence_kind='failure_prefix').byte_count, 0)

    def test_exact_chunk_slot_and_aggregate_limits_refuse_whole_chunk_without_poisoning(self):
        fixture = self.fixture(budget=budget(semantic_record_bytes=65536, retained_total_bytes=65538))
        store = self.create(fixture)
        sink = store.open_new_sink('producer.source.json')
        for wrong in (bytearray(b'x'), memoryview(b'x'), 'x', b'x' * 65537):
            with self.subTest(type=type(wrong).__name__), self.assertRaises(DiagnosticJournalError):
                sink.write(wrong)
        self.assertEqual(store.retained_bytes, 0)
        self.assertEqual(sink.write(b'x' * 65536), 65536)
        with self.assertRaises(DiagnosticJournalError):
            sink.write(b'x')
        self.assertEqual(store.retained_bytes, 65536)
        self.assertEqual(store.remaining('producer.source.json'), 0)
        other = store.open_new_sink('producer.stderr.raw')
        self.assertEqual(store.remaining('producer.stderr.raw'), 2)
        with self.assertRaises(DiagnosticJournalError):
            other.write(b'abc')
        self.assertEqual((store.path / 'producer.stderr.raw').read_bytes(), b'')
        self.assertEqual(other.write(b'ab'), 2)
        self.assertEqual(store.retained_bytes, 65538)
        self.assertEqual(store.remaining('result.json'), 0)
        for wrong in ('../input.json', 'producer.stdin.raw', '', None, True):
            with self.subTest(slot=wrong), self.assertRaises(DiagnosticJournalError):
                store.remaining(wrong)

    def test_positive_short_writes_continue_exact_suffixes_and_commit_actual_bytes(self):
        fixture = self.fixture()
        store = self.create(fixture)
        target = inode(store.path / 'producer.source.json')
        original_write = os.write
        calls = []
        def short_write(fd, chunk):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == target:
                calls.append(bytes(chunk))
                return original_write(fd, chunk[:3])
            return original_write(fd, chunk)
        with patch.object(store_module.os, 'write', short_write):
            self.assertEqual(store.open_new_sink('producer.source.json').write(b'abcdefgh'), 8)
        self.assertEqual(calls, [b'abcdefgh', b'defgh', b'gh'])
        self.assertEqual(store.retained_bytes, 8)
        self.assertEqual((store.path / 'producer.source.json').read_bytes(), b'abcdefgh')
        seal = store.seal('producer.source.json', evidence_kind='complete')
        self.assertEqual((seal.byte_count, seal.sha256), (8, sha(b'abcdefgh')))

    def test_unreported_write_preserves_known_call_prefix_and_poisoned_reservation(self):
        for hidden_progress in (False, True):
            fixture = self.fixture()
            store = self.create(fixture)
            path = store.path / 'producer.source.json'
            target = inode(path)
            sink = store.open_new_sink('producer.source.json')
            sink.write(b'prior')
            original_write = os.write
            failure = OSError('unreported next write outcome')
            calls = 0
            def fail_after_short(fd, chunk):
                nonlocal calls
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != target:
                    return original_write(fd, chunk)
                calls += 1
                if calls == 1:
                    return original_write(fd, chunk[:2])
                if hidden_progress:
                    original_write(fd, chunk[:1])
                raise failure
            with self.subTest(hidden_progress=hidden_progress), patch.object(store_module.os, 'write', fail_after_short), \
                    self.assertRaises(DiagnosticQualityWriteError) as caught:
                sink.write(b'abcdef')
            self.assertEqual(calls, 2)
            error = caught.exception
            self.assertEqual((error.requested_bytes, error.confirmed_bytes, error.unreported_write_outcome), (6, 2, True))
            self.assertIn(failure, error_chain(error))
            self.assertEqual(path.read_bytes(), b'priorab' + (b'c' if hidden_progress else b''))
            for operation in (lambda: sink.write(b'x'), lambda: store.open_new_sink('result.json'),
                              lambda: store.seal('producer.source.json', evidence_kind='failure_prefix')):
                with self.assertRaises(DiagnosticJournalError):
                    operation()

    def test_seal_rehashes_original_fd_in_bounded_reads_without_changing_write_offset(self):
        fixture = self.fixture()
        store = self.create(fixture)
        target = inode(store.path / 'producer.source.json')
        sink = store.open_new_sink('producer.source.json')
        original_write, original_pread = os.write, os.pread
        captured, requests = [], []
        def write(fd, chunk):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == target:
                captured.append(fd)
            return original_write(fd, chunk)
        raw = b'a' * 65536 + b'b' * 4465
        with patch.object(store_module.os, 'write', write):
            sink.write(raw[:65536])
            sink.write(raw[65536:])
        fd = captured[0]
        before_offset = os.lseek(fd, 0, os.SEEK_CUR)
        def read(fd, size, offset):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == target:
                requests.append((size, offset))
            return original_pread(fd, size, offset)
        with patch.object(store_module.os, 'pread', read):
            seal = store.seal('producer.source.json', evidence_kind='complete')
        self.assertEqual((seal.byte_count, seal.sha256), (len(raw), sha(raw)))
        self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), before_offset)
        self.assertTrue(requests)
        self.assertTrue(all(0 < size <= 65536 for size, _ in requests))
        self.assertEqual(requests[-1], (1, len(raw)))
        self.assertEqual(store.read_sealed('producer.source.json', maximum_bytes=len(raw)), raw)
        with self.assertRaises(DiagnosticJournalError):
            sink.write(b'x')

    def test_freeze_is_stable_kind_cannot_upgrade_and_full_inventory_requires_16_seals(self):
        fixture = self.fixture()
        store = self.create(fixture)
        slot = 'producer.stderr.raw'
        store.open_new_sink(slot).write(b'error prefix')
        original = store.seal(slot, evidence_kind='failure_prefix')
        self.assertEqual(store.seal(slot, evidence_kind='failure_prefix'), original)
        with self.assertRaises(DiagnosticJournalError):
            store.seal(slot, evidence_kind='complete')
        with self.assertRaises(DiagnosticJournalError):
            store.verify_unchanged()
        for name in SLOTS:
            if name != slot:
                store.seal(name, evidence_kind='complete')
        all_seals = store.verify_unchanged()
        self.assertEqual(tuple(item.slot for item in all_seals), SLOTS)
        self.assertEqual(sum(item.byte_count for item in all_seals), len(b'error prefix'))
        self.assertEqual(all_seals[SLOTS.index(slot)], original)
        for name in SLOTS:
            with self.subTest(slot=name), self.assertRaises(DiagnosticJournalError):
                store.open_new_sink(name)

    def test_read_requires_seal_and_exact_caller_budget_without_payload_read_on_refusal(self):
        fixture = self.fixture()
        store = self.create(fixture)
        slot = 'producer.source.json'
        store.open_new_sink(slot).write(b'abc')
        with self.assertRaises(DiagnosticJournalError):
            store.read_sealed(slot, maximum_bytes=3)
        store.seal(slot, evidence_kind='complete')
        target, pread = inode(store.path / slot), os.pread
        def refuse_payload_read(fd, size, offset):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == target:
                raise AssertionError('oversize sealed payload read before budget rejection')
            return pread(fd, size, offset)
        for maximum in (0, False, -1, 2, 3.0):
            with self.subTest(maximum=maximum), patch.object(store_module.os, 'pread', refuse_payload_read), self.assertRaises(DiagnosticJournalError):
                store.read_sealed(slot, maximum_bytes=maximum)
        self.assertEqual(store.read_sealed(slot, maximum_bytes=3), b'abc')

    def test_full_namespace_detects_extra_or_replaced_objects_and_hardlinks_without_blocking(self):
        for replacement in ('extra', 'same_bytes', 'symlink', 'fifo', 'directory', 'hardlink'):
            fixture = self.fixture()
            store = self.create(fixture)
            path = store.path
            self.seal_all(store)
            victim = path / 'producer.source.json'
            outside = fixture.root / 'outside'
            outside.write_bytes(b'')
            outside.chmod(0o600)
            if replacement == 'extra':
                (path / 'foreign').write_bytes(b'')
            elif replacement == 'hardlink':
                os.link(victim, fixture.root / 'extra-link')
            else:
                victim.rename(fixture.root / 'retained-original')
                if replacement == 'same_bytes':
                    victim.write_bytes(b'')
                    victim.chmod(0o600)
                elif replacement == 'symlink':
                    victim.symlink_to(outside)
                elif replacement == 'fifo':
                    os.mkfifo(victim, 0o600)
                else:
                    victim.mkdir(mode=0o700)
            with self.subTest(replacement=replacement), nonblocking_bound(), self.assertRaises((DiagnosticJournalError, OSError)):
                store.verify_unchanged()
            self.assertEqual(outside.read_bytes(), b'')

    def test_sealed_growth_truncation_and_same_size_content_change_remain_visible(self):
        for wrong in (b'abcd', b'ab', b'xyz'):
            fixture = self.fixture()
            store = self.create(fixture)
            slot = 'producer.source.json'
            store.open_new_sink(slot).write(b'abc')
            store.seal(slot, evidence_kind='complete')
            path = store.path / slot
            path.write_bytes(wrong)
            with self.subTest(wrong=wrong), self.assertRaises(DiagnosticJournalError):
                store.read_sealed(slot, maximum_bytes=10)
            self.assertEqual(path.read_bytes(), wrong)

    def test_early_reopen_never_adopts_empty_or_nonempty_unsealed_slots(self):
        for with_payload in (False, True):
            fixture = self.fixture()
            store = self.create(fixture)
            path = store.path
            if with_payload:
                store.open_new_sink('input.json').write(b'not durable input')
                store.seal('input.json', evidence_kind='complete')
            store.close()
            saved = {name: (identity(path / name), (path / name).read_bytes()) for name in SLOTS}
            with self.subTest(with_payload=with_payload), self.assertRaises(DiagnosticJournalError):
                reopen_quality_store(journal=fixture.journal, phases=fixture.phases)
            self.assertEqual({name: (identity(path / name), (path / name).read_bytes()) for name in SLOTS}, saved)

    def test_close_owns_all_18_handles_is_idempotent_and_leaves_original_journal_live(self):
        fixture = self.fixture()
        store, held = self.capture_create(fixture)
        sink = store.open_new_sink('producer.source.json')
        sink.write(b'a')
        store.close()
        store.close()
        sink.close()
        for fd in held:
            with self.subTest(fd=fd), self.assertRaises(OSError):
                os.fstat(fd)
        fixture.journal.remaining_seconds()
        self.assertEqual(fixture.journal.records[-1].step, 'quality_files_created')
        with self.assertRaises(DiagnosticJournalError):
            sink.write(b'b')

    def test_initial_fstat_and_slot_allocation_failures_close_every_acquired_descriptor(self):
        for cut in ('first_fstat', 'slot_allocation'):
            fixture = self.fixture()
            real_open, real_fstat, real_close = os.open, os.fstat, os.close
            acquired, closed = [], []
            target = None
            failure = OSError('first acquired descriptor identity unavailable') if cut == 'first_fstat' else MemoryError('slot allocation failed')
            def open_handle(path, flags, *args, **kwargs):
                nonlocal target
                fd = real_open(path, flags, *args, **kwargs)
                if str(path) == str(fixture.root):
                    target = fd
                acquired.append(fd)
                return fd
            failed_stat = False
            def fstat(fd):
                nonlocal failed_stat
                if cut == 'first_fstat' and fd == target:
                    if failed_stat:
                        raise AssertionError('initial failed fstat was repeated during closure')
                    failed_stat = True
                    raise failure
                return real_fstat(fd)
            def close(fd):
                closed.append(fd)
                return real_close(fd)
            with self.subTest(cut=cut), patch.object(store_module.os, 'open', open_handle), \
                    patch.object(store_module.os, 'fstat', fstat), patch.object(store_module.os, 'close', close):
                if cut == 'slot_allocation':
                    with patch.object(store_module, '_Slot', side_effect=failure), self.assertRaises(BaseException) as caught:
                        create_quality_store(journal=fixture.journal, phases=fixture.phases)
                else:
                    with self.assertRaises(BaseException) as caught:
                        create_quality_store(journal=fixture.journal, phases=fixture.phases)
            self.assertIn(failure, error_chain(caught.exception))
            for fd in set(acquired):
                with self.assertRaises(OSError):
                    real_fstat(fd)
            self.assertIsNotNone(target)
            if cut == 'first_fstat':
                self.assertEqual(closed.count(target), 1)
                self.create(fixture)  # Failed acquisition released the live-owner claim.
            else:
                path = fixture.root / 'journal.quality'
                self.assertEqual({entry.name for entry in path.iterdir()}, {'input.json'})
                self.assertEqual((path / 'input.json').read_bytes(), b'')
            fixture.journal.remaining_seconds()

    def test_store_closed_during_fresh_open_closes_unregistered_new_handle_once(self):
        fixture = self.fixture()
        real_open, real_close = os.open, os.close
        fresh, close_calls = [], []
        def open_then_close_owner(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if str(path) == 'input.json' and flags & os.O_CREAT:
                fresh.append(fd)
                # This is the actual adapter-created live owner, before its
                # factory returns. No fake owner or phase data is installed.
                store_module._LIVE_STORES[fixture.journal].close()
            return fd
        def close(fd):
            close_calls.append(fd)
            return real_close(fd)
        with patch.object(store_module.os, 'open', open_then_close_owner), patch.object(store_module.os, 'close', close), \
                self.assertRaises(BaseException):
            create_quality_store(journal=fixture.journal, phases=fixture.phases)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(close_calls.count(fresh[0]), 1)
        with self.assertRaises(OSError):
            os.fstat(fresh[0])
        self.assertEqual((fixture.root / 'journal.quality/input.json').read_bytes(), b'')
        fixture.journal.remaining_seconds()

    def test_clock_closing_writer_after_entry_guard_prevents_first_syscall(self):
        fixture = self.fixture()
        store = self.create(fixture)
        path = store.path / 'producer.source.json'
        target = inode(path)
        sink = store.open_new_sink('producer.source.json')
        original_guard, original_clock, original_write = store._guard, fixture.journal._clock, os.write
        guards, calls = 0, []
        armed = False
        def guard():
            nonlocal guards, armed
            result = original_guard()
            guards += 1
            if guards == 1:
                armed = True
            return result
        def clock():
            nonlocal armed
            if armed:
                armed = False
                sink.close()
            return original_clock()
        def write(fd, chunk):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == target:
                calls.append(bytes(chunk))
            return original_write(fd, chunk)
        with patch.object(store, '_guard', guard), patch.object(fixture.journal, '_clock', clock), \
                patch.object(store_module.os, 'write', write), self.assertRaises(DiagnosticJournalError):
            sink.write(b'forbidden')
        self.assertEqual(calls, [])
        self.assertEqual(path.read_bytes(), b'')

    def test_post_write_callback_close_commits_returned_prefix_before_reporting_failure(self):
        fixture = self.fixture()
        store = self.create(fixture)
        path = store.path / 'producer.source.json'
        target = inode(path)
        sink = store.open_new_sink('producer.source.json')
        original_write, original_clock = os.write, fixture.journal._clock
        armed, calls = False, 0
        def write(fd, chunk):
            nonlocal armed, calls
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == target:
                calls += 1
                returned = original_write(fd, chunk[:2])
                armed = True
                return returned
            return original_write(fd, chunk)
        def clock():
            nonlocal armed
            if armed:
                armed = False
                store.close()
            return original_clock()
        with patch.object(store_module.os, 'write', write), patch.object(fixture.journal, '_clock', clock), \
                self.assertRaises(DiagnosticQualityWriteError) as caught:
            sink.write(b'abcdef')
        self.assertEqual(calls, 1)
        self.assertEqual((caught.exception.requested_bytes, caught.exception.confirmed_bytes,
                          caught.exception.unreported_write_outcome), (6, 2, False))
        self.assertEqual(path.read_bytes(), b'ab')
        fixture.journal.remaining_seconds()

    def test_zero_return_and_impossible_report_have_distinct_unknown_outcome_truth(self):
        for impossible in (False, True):
            fixture = self.fixture()
            store = self.create(fixture)
            path = store.path / 'producer.source.json'
            target, real_write = inode(path), os.write
            calls = 0
            def invalid_write(fd, chunk):
                nonlocal calls
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != target:
                    return real_write(fd, chunk)
                calls += 1
                if impossible:
                    real_write(fd, chunk[:1])
                    return len(chunk) + 1
                return 0
            sink = store.open_new_sink('producer.source.json')
            with self.subTest(impossible=impossible), patch.object(store_module.os, 'write', invalid_write), \
                    self.assertRaises(DiagnosticQualityWriteError) as caught:
                sink.write(b'abc')
            self.assertEqual(calls, 1)
            self.assertEqual((caught.exception.requested_bytes, caught.exception.confirmed_bytes,
                              caught.exception.unreported_write_outcome), (3, 0, impossible))
            self.assertEqual(path.read_bytes(), b'a' if impossible else b'')
            with self.assertRaises(DiagnosticJournalError):
                store.seal('producer.source.json', evidence_kind='failure_prefix')

    def test_pending_or_expired_clock_blocks_io_but_close_never_uses_work_checkpoint(self):
        for fault in ('pending', 'expired'):
            fixture = self.fixture()
            store, held = self.capture_create(fixture)
            path = store.path
            if fault == 'pending':
                original_clock = fixture.journal._clock
                armed = True
                def clock():
                    nonlocal armed
                    if armed:
                        armed = False
                        pending = fixture.journal.root / 'append-pending.json'
                        pending.write_bytes(b'{}')
                        pending.chmod(0o600)
                    return original_clock()
                with patch.object(fixture.journal, '_clock', clock), self.assertRaises(DiagnosticJournalError):
                    store.remaining('input.json')
            else:
                fixture.now = fixture.deadline
                with self.assertRaises(TimeoutError):
                    store.remaining('input.json')
            with patch.object(fixture.journal, 'remaining_seconds', side_effect=AssertionError('close consulted expired work clock')):
                store.close()
            for fd in held:
                with self.assertRaises(OSError):
                    os.fstat(fd)
            self.assertTrue(all((path / name).read_bytes() == b'' for name in SLOTS))

    def test_returned_seals_cannot_mutate_registry_kind_hash_or_inventory(self):
        fixture = self.fixture()
        store = self.create(fixture)
        slot = 'producer.stderr.raw'
        store.open_new_sink(slot).write(b'abc')
        returned = store.seal(slot, evidence_kind='failure_prefix')
        object.__setattr__(returned, 'evidence_kind', 'complete')
        object.__setattr__(returned, 'sha256', sha(b'forged'))
        actual = store.seal(slot, evidence_kind='failure_prefix')
        self.assertEqual((actual.evidence_kind, actual.sha256), ('failure_prefix', sha(b'abc')))
        with self.assertRaises(DiagnosticJournalError):
            store.seal(slot, evidence_kind='complete')
        for name in SLOTS:
            if name != slot:
                store.seal(name, evidence_kind='complete')
        inventory = store.verify_unchanged()
        victim = inventory[SLOTS.index(slot)]
        object.__setattr__(victim, 'byte_count', 0)
        self.assertEqual(store.verify_unchanged()[SLOTS.index(slot)].byte_count, 3)
        self.assertEqual(store.read_sealed(slot, maximum_bytes=3), b'abc')

    def test_seal_fsync_failure_retires_writer_and_never_authorizes_later_seal(self):
        fixture = self.fixture()
        store = self.create(fixture)
        path = store.path / 'producer.source.json'
        target = inode(path)
        sink = store.open_new_sink('producer.source.json')
        sink.write(b'abc')
        original_fsync = os.fsync
        failure = OSError('actual held seal fsync failed')
        def sync(fd):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == target:
                raise failure
            return original_fsync(fd)
        with patch.object(store_module.os, 'fsync', sync), self.assertRaises(BaseException) as caught:
            store.seal('producer.source.json', evidence_kind='complete')
        self.assertIn(failure, error_chain(caught.exception))
        for operation in (lambda: sink.write(b'x'), lambda: store.seal('producer.source.json', evidence_kind='failure_prefix'),
                          lambda: store.read_sealed('producer.source.json', maximum_bytes=3)):
            with self.assertRaises(DiagnosticJournalError):
                operation()
        self.assertEqual(path.read_bytes(), b'abc')

    def test_replaced_root_or_parent_directory_never_adopts_matching_private_names(self):
        for replace_parent in (False, True):
            fixture = self.fixture()
            store = self.create(fixture)
            path = fixture.root if replace_parent else store.path
            mode = stat.S_IMODE(path.stat().st_mode)
            moved = self.root / f'original-{self.serial}'
            path.rename(moved)
            path.mkdir(mode=mode)
            with self.subTest(replace_parent=replace_parent), self.assertRaises((DiagnosticJournalError, OSError)):
                store.remaining('input.json')
            self.assertEqual(list(path.iterdir()), [])
            self.assertTrue(moved.exists())

    def test_consumed_or_pre_recycled_close_never_closes_new_owned_tempfile(self):
        for consumed_by_close in (False, True):
            fixture = self.fixture()
            store, held = self.capture_create(fixture)
            selected = inode(store.path / 'producer.source.json')
            target = next(fd for fd, value in held.items() if value == selected)
            original_close, original_open = os.close, os.open
            replacement_path = fixture.root / 'owned-replacement'
            replacement = None
            failure = OSError('close consumed original then failed')
            calls = 0
            def replace():
                fd = original_open(replacement_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                if fd != target:
                    os.dup2(fd, target)
                    original_close(fd)
                return target
            def close(fd):
                nonlocal calls, replacement
                if fd == target:
                    calls += 1
                    original_close(fd)
                    replacement = replace()
                    raise failure
                return original_close(fd)
            if not consumed_by_close:
                original_close(target)
                replacement = replace()
            try:
                with self.subTest(consumed_by_close=consumed_by_close), patch.object(store_module.os, 'close', close), \
                        self.assertRaises(BaseException) as caught:
                    store.close()
                if consumed_by_close:
                    self.assertIn(failure, error_chain(caught.exception))
                self.assertEqual(calls, int(consumed_by_close))
                self.assertEqual((os.fstat(target).st_dev, os.fstat(target).st_ino), inode(replacement_path))
                with patch.object(store_module.os, 'close', close):
                    store.close()
                self.assertEqual(calls, int(consumed_by_close))
                for fd in held:
                    if fd != target:
                        with self.assertRaises(OSError):
                            os.fstat(fd)
            finally:
                if replacement is not None:
                    original_close(replacement)

    def test_body_and_independent_close_errors_all_remain_visible_after_full_closure_attempt(self):
        fixture = self.fixture()
        store, held = self.capture_create(fixture)
        chosen = list(held)[:2]
        errors = {fd: OSError(f'close failed for owned fd {index}') for index, fd in enumerate(chosen)}
        body = RuntimeError('original body failure')
        original_close = os.close
        calls = []
        def close(fd):
            calls.append(fd)
            original_close(fd)
            if fd in errors:
                raise errors[fd]
        with patch.object(store_module.os, 'close', close), self.assertRaises(BaseException) as caught:
            with store:
                raise body
        chain = error_chain(caught.exception)
        self.assertIn(body, chain)
        for failure in errors.values():
            self.assertIn(failure, chain)
        for fd in held:
            self.assertEqual(calls.count(fd), 1)
            with self.assertRaises(OSError):
                os.fstat(fd)
        store.close()
        fixture.journal.remaining_seconds()


if __name__ == '__main__':
    unittest.main()
