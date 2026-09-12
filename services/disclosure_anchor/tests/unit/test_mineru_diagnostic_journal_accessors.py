"""Independent journal accessor contracts with owned temporary files, no runtime IO."""

from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_diagnostic_journal as journal_module
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import (
    DiagnosticJournal, DiagnosticJournalError, DiagnosticJournalIdentity,
)


RECORD_LIMIT = 96
TOTAL_LIMIT = 16 * 1024 * 1024
RECORD_BYTE_LIMIT = 2 * 1024 * 1024 + 8192
CONFIGURATION = 'sha256:' + 'c' * 64
CLOCK_ID = 'sha256:' + 'd' * 64
START = 100
DEADLINE = 9_000_000_000
HEADER = '00-journal.json'
PENDING = 'append-pending.json'


def canonical(value):
    # Independent oracle: complete persisted wire fields, never production
    # helpers, records cache, private counters, or projected output as expected.
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def identity(path):
    info = path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


def expected_header(root):
    return canonical({'contract_version': 'mineru-diagnostic-journal.v2', 'attempt_id': 'accessor-attempt',
                      'configuration_sha256': CONFIGURATION, 'clock_identity_sha256': CLOCK_ID,
                      'started_ns': START, 'deadline_ns': DEADLINE, 'root_identity': list(identity(root))})


def expected_record(sequence, step, payload, previous, observed_ns):
    return canonical({'contract_version': 'mineru-diagnostic-record.v2', 'sequence': sequence,
                      'step': step, 'previous_sha256': previous, 'value_sha256': digest(canonical(payload)),
                      'value': payload, 'observed_ns': observed_ns})


def files(root):
    return {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}


def private_write(path, raw):
    with path.open('xb') as stream:
        stream.write(raw)
    path.chmod(0o600)


class Clock:
    def __init__(self):
        self.value = START
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


class JournalAccessorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='journal-accessor-independent-')
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.clock = Clock()
        self.root = self.parent / 'journal'
        self.journal = self.open_journal(create=True)

    def open_journal(self, *, create, root=None, clock=None):
        journal = DiagnosticJournal(root or self.root, create=create, attempt_id='accessor-attempt',
                                    configuration_sha256=CONFIGURATION, clock_identity_sha256=CLOCK_ID,
                                    deadline_ns=DEADLINE, continuous_ns=clock or self.clock)
        self.addCleanup(journal.close)
        return journal

    def both_refuse(self, journal=None):
        owner = journal or self.journal
        with self.assertRaises((DiagnosticJournalError, OSError)):
            _ = owner.original_identity
        with self.assertRaises((DiagnosticJournalError, OSError)):
            owner.require_capacity(additional_records=0, additional_bytes=0)

    def test_original_identity_uses_literal_accepted_header_across_appends_checkpoints_and_reopen(self):
        raw = expected_header(self.root)
        self.assertEqual((self.root / HEADER).read_bytes(), raw)
        expected = DiagnosticJournalIdentity('accessor-attempt', CONFIGURATION, CLOCK_ID, START,
                                             DEADLINE, identity(self.root), digest(raw))
        original = self.journal.original_identity
        self.assertIs(type(original), DiagnosticJournalIdentity)
        self.assertEqual(original, expected)
        tail = digest(raw)
        for sequence, (when, payload) in enumerate(((130, {'literal': '中'}), (190, {'ready': False})), 1):
            self.clock.value = when
            wire = expected_record(sequence, 'stage', payload, tail, when)
            appended = self.journal.append('stage', payload)
            self.assertEqual((self.root / f'{sequence:04d}-stage.json').read_bytes(), wire)
            self.assertEqual(appended.sha256, digest(wire))
            self.assertEqual(self.journal.original_identity, expected)
            tail = digest(wire)
        self.clock.value = 240
        self.journal.require_capacity(additional_records=0, additional_bytes=0)
        self.assertNotEqual(tail, original.header_sha256)
        self.journal.close()
        reopened = self.open_journal(create=False)
        self.assertEqual(reopened.original_identity, expected)
        self.assertEqual(reopened.original_identity.started_ns, START)
        self.assertEqual((self.root / HEADER).read_bytes(), raw)
        for field in ('attempt_id', 'started_ns', 'root_identity', 'header_sha256'):
            with self.subTest(field=field), self.assertRaises((FrozenInstanceError, AttributeError)):
                setattr(original, field, None)
        self.assertIs(type(original.root_identity), tuple)
        with self.assertRaises(TypeError):
            original.root_identity[0] = 0
        self.assertFalse(hasattr(original, '__dict__'))

    def test_identity_getter_is_informational_and_never_samples_or_extends_original_clock(self):
        expected = self.journal.original_identity
        self.clock.value = DEADLINE
        count = self.clock.calls
        self.assertEqual(self.journal.original_identity, expected)
        self.assertEqual(self.clock.calls, count)
        with self.assertRaises(TimeoutError):
            self.journal.require_capacity(additional_records=0, additional_bytes=0)
        self.assertGreater(self.clock.calls, count)
        self.assertEqual(self.journal.original_identity, expected)
        self.assertEqual(expected.deadline_ns, DEADLINE)
        self.assertEqual(expected.started_ns, START)

    def test_capacity_uses_complete_canonical_envelopes_and_repeated_checks_consume_nothing(self):
        header = expected_header(self.root)
        tail, used = digest(header), len(header)
        for sequence, payload in enumerate(({'text': '中\n"'}, {'flag': True, 'items': [None, 7]}), 1):
            self.clock.value += 1
            raw = expected_record(sequence, 'observed', payload, tail, self.clock.value)
            self.journal.append('observed', payload)
            self.assertEqual((self.root / f'{sequence:04d}-observed.json').read_bytes(), raw)
            used += len(raw)
            tail = digest(raw)
        before = files(self.root)
        self.assertGreater(used, sum(len(canonical(p)) for p in ({'text': '中\n"'}, {'flag': True, 'items': [None, 7]})))
        for _ in range(3):
            self.journal.require_capacity(additional_records=94, additional_bytes=TOTAL_LIMIT - used)
        self.assertEqual(files(self.root), before)
        for records, byte_count in ((95, 0), (0, TOTAL_LIMIT - used + 1)):
            with self.subTest(records=records, bytes=byte_count), self.assertRaises(DiagnosticJournalError):
                self.journal.require_capacity(additional_records=records, additional_bytes=byte_count)
        payload = {'after_preflight': 'still writable'}
        raw = expected_record(3, 'observed', payload, tail, self.clock.value)
        self.journal.append('observed', payload)
        used += len(raw)
        self.journal.require_capacity(additional_records=93, additional_bytes=TOTAL_LIMIT - used)
        with self.assertRaises(DiagnosticJournalError):
            self.journal.require_capacity(additional_records=0, additional_bytes=TOTAL_LIMIT - used + 1)
        self.assertEqual((self.root / '0003-observed.json').read_bytes(), raw)
        self.journal.close()
        reopened = self.open_journal(create=False)
        reopened.require_capacity(additional_records=93, additional_bytes=TOTAL_LIMIT - used)
        with self.assertRaises(DiagnosticJournalError):
            reopened.require_capacity(additional_records=0, additional_bytes=TOTAL_LIMIT - used + 1)

    def test_actual_96_record_boundary_is_reconstructed_and_cannot_be_reserved_past(self):
        header = expected_header(self.root)
        tail, used = digest(header), len(header)
        for sequence in range(1, RECORD_LIMIT + 1):
            payload = {'n': sequence}
            raw = expected_record(sequence, 'counted', payload, tail, START)
            self.journal.append('counted', payload)
            self.assertEqual((self.root / f'{sequence:04d}-counted.json').read_bytes(), raw)
            used += len(raw)
            tail = digest(raw)
        before = files(self.root)
        self.journal.require_capacity(additional_records=0, additional_bytes=TOTAL_LIMIT - used)
        with self.assertRaises(DiagnosticJournalError):
            self.journal.require_capacity(additional_records=1, additional_bytes=0)
        with self.assertRaises(DiagnosticJournalError):
            self.journal.append('extra', {})
        self.assertEqual(files(self.root), before)
        self.journal.close()
        reopened = self.open_journal(create=False)
        reopened.require_capacity(additional_records=0, additional_bytes=TOTAL_LIMIT - used)
        with self.assertRaises(DiagnosticJournalError):
            reopened.require_capacity(additional_records=1, additional_bytes=0)

    def test_actual_16_mib_boundary_includes_header_and_reopens_without_budget_repayment(self):
        header = expected_header(self.root)
        tail, used = digest(header), len(header)
        for sequence in range(1, 9):
            # Seven actual 2 MiB envelopes followed by exactly the remaining
            # bytes; hashes have a fixed-width field but are independently real.
            target = 2 * 1024 * 1024 if sequence < 8 else TOTAL_LIMIT - used
            overhead = len(expected_record(sequence, 'filled', {'padding': ''}, tail, START))
            payload = {'padding': 'x' * (target - overhead)}
            raw = expected_record(sequence, 'filled', payload, tail, START)
            self.assertEqual(len(raw), target)
            self.assertLessEqual(len(raw), RECORD_BYTE_LIMIT)
            self.journal.append('filled', payload)
            self.assertEqual((self.root / f'{sequence:04d}-filled.json').read_bytes(), raw)
            used += len(raw)
            tail = digest(raw)
        self.assertEqual(used, TOTAL_LIMIT)
        self.journal.require_capacity(additional_records=88, additional_bytes=0)
        with self.assertRaises(DiagnosticJournalError):
            self.journal.require_capacity(additional_records=0, additional_bytes=1)
        names = sorted(path.name for path in self.root.iterdir())
        with self.assertRaises(DiagnosticJournalError):
            self.journal.append('extra', {})
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), names)
        self.journal.close()
        reopened = self.open_journal(create=False)
        reopened.require_capacity(additional_records=88, additional_bytes=0)
        with self.assertRaises(DiagnosticJournalError):
            reopened.require_capacity(additional_records=0, additional_bytes=1)

    def test_aggregate_preflight_does_not_waive_final_individual_record_enforcement(self):
        requested = RECORD_BYTE_LIMIT + 1
        self.journal.require_capacity(additional_records=1, additional_bytes=requested)
        before = files(self.root)
        with self.assertRaises(DiagnosticJournalError):
            self.journal.append('too_large', {'padding': 'x' * RECORD_BYTE_LIMIT})
        self.assertEqual(files(self.root), before)
        self.journal.require_capacity(additional_records=96, additional_bytes=TOTAL_LIMIT - len(expected_header(self.root)))

    def test_capacity_requires_nonnegative_exact_integer_keyword_demands_without_big_allocation(self):
        before = files(self.root)
        for field in ('additional_records', 'additional_bytes'):
            for invalid in (True, False, -1, 0.0, 1.5, '1', None, [], {}):
                request = {'additional_records': 0, 'additional_bytes': 0, field: invalid}
                with self.subTest(field=field, value=invalid), self.assertRaises((DiagnosticJournalError, TypeError)):
                    self.journal.require_capacity(**request)
            with self.subTest(field=field, value='huge integer'), self.assertRaises(DiagnosticJournalError):
                self.journal.require_capacity(**{'additional_records': 0, 'additional_bytes': 0, field: 10**1000})
        with self.assertRaises(TypeError):
            self.journal.require_capacity(0, 0)
        self.journal.require_capacity(additional_records=0, additional_bytes=0)
        self.assertEqual(files(self.root), before)

    def test_capacity_advances_original_checkpoint_and_a_regression_poison_cannot_be_reset(self):
        self.clock.value = 300
        self.journal.require_capacity(additional_records=0, additional_bytes=0)
        self.clock.value = 299
        with self.assertRaises(DiagnosticJournalError):
            self.journal.require_capacity(additional_records=0, additional_bytes=0)
        self.clock.value = 301
        self.both_refuse()
        self.assertEqual((self.root / HEADER).read_bytes(), expected_header(self.root))

    def test_invalid_capacity_clock_samples_poison_each_owned_journal_without_new_evidence(self):
        for index, invalid in enumerate((False, True, -1, 100.0, None)):
            clock = Clock()
            root = self.parent / f'invalid-clock-{index}'
            owner = self.open_journal(create=True, root=root, clock=clock)
            before = files(root)
            clock.value = invalid
            with self.subTest(clock=invalid), self.assertRaises(DiagnosticJournalError):
                owner.require_capacity(additional_records=0, additional_bytes=0)
            clock.value = START + 1
            self.both_refuse(owner)
            self.assertEqual(files(root), before)

    def test_closed_owner_rejects_both_accessors_without_sample_or_reopening(self):
        before = files(self.root)
        self.journal.close()
        count = self.clock.calls
        with patch.object(journal_module.os, 'open', side_effect=AssertionError('closed owner reopened a path')):
            self.both_refuse()
        self.assertEqual(self.clock.calls, count)
        self.assertEqual(files(self.root), before)

    def test_pending_regular_symlink_and_fifo_names_reject_without_opening_or_adoption(self):
        real_open = os.open
        for kind in ('regular', 'symlink', 'fifo'):
            pending = self.root / PENDING
            if kind == 'regular':
                private_write(pending, b'independent uncertainty')
            elif kind == 'symlink':
                pending.symlink_to(self.root / HEADER)
            else:
                os.mkfifo(pending, 0o600)
            before = pending.lstat()
            def guarded_open(name, flags, *args, **kwargs):
                if os.fspath(name) == PENDING:
                    raise AssertionError('pending evidence must not be opened')
                return real_open(name, flags, *args, **kwargs)
            with self.subTest(kind=kind), patch.object(journal_module.os, 'open', side_effect=guarded_open):
                self.both_refuse()
            after = pending.lstat()
            self.assertEqual((before.st_ino, before.st_mode, before.st_size),
                             (after.st_ino, after.st_mode, after.st_size))
            pending.unlink()  # Only this test's newly created obstruction.
        self.assertEqual(self.journal.original_identity.header_sha256, digest(expected_header(self.root)))
        self.journal.require_capacity(additional_records=0, additional_bytes=0)

    def test_uncertain_append_poison_blocks_accessors_with_and_without_retained_pending(self):
        for index, after_pending in enumerate((False, True)):
            root = self.parent / f'uncertain-{index}'
            owner = self.open_journal(create=True, root=root)
            actual_write = owner._write_new
            failure = OSError('independent injected persistence uncertainty')
            def uncertain_write(name, raw):
                if after_pending and name == PENDING:
                    return actual_write(name, raw)
                raise failure
            with patch.object(owner, '_write_new', side_effect=uncertain_write):
                with self.assertRaises(OSError) as caught:
                    owner.append('uncertain', {'observed': 1})
            self.assertIs(caught.exception, failure)
            self.assertEqual((root / PENDING).exists(), after_pending)
            before = files(root)
            self.both_refuse(owner)
            self.assertEqual(files(root), before)
            self.assertEqual(owner.records, ())

    def test_header_byte_change_is_detected_but_same_bytes_at_new_inode_remain_accepted(self):
        header = self.root / HEADER
        original = header.read_bytes()
        expected = self.journal.original_identity
        replacement = self.root / 'header-replacement'
        private_write(replacement, original)
        old_ino, new_ino = header.stat().st_ino, replacement.stat().st_ino
        self.assertNotEqual(old_ino, new_ino)
        replacement.replace(header)
        self.assertEqual(self.journal.original_identity, expected)
        changed = json.loads(original)
        changed['started_ns'] = START + 1
        for raw in (canonical(changed), original + b'\n'):
            header.write_bytes(raw)
            with self.subTest(raw_size=len(raw)), self.assertRaises(DiagnosticJournalError):
                _ = self.journal.original_identity
            self.assertEqual(header.read_bytes(), raw)
        header.write_bytes(original)
        self.assertEqual(self.journal.original_identity, expected)

    def test_header_fifo_is_opened_nonblocking_and_rejected_before_any_content_read(self):
        header = self.root / HEADER
        header.unlink()
        os.mkfifo(header, 0o600)
        real_open = os.open
        checked = []
        def safe_open(name, flags, *args, **kwargs):
            if os.fspath(name) == HEADER:
                # Fail before a real blocking syscall if O_NONBLOCK regresses.
                self.assertTrue(flags & os.O_NONBLOCK)
                self.assertTrue(flags & os.O_NOFOLLOW)
                checked.append(flags)
            return real_open(name, flags, *args, **kwargs)
        before = header.lstat()
        with patch.object(journal_module.os, 'open', side_effect=safe_open):
            with self.assertRaises(DiagnosticJournalError):
                _ = self.journal.original_identity
        self.assertEqual(len(checked), 1)
        self.assertEqual(header.lstat().st_ino, before.st_ino)

    def test_header_symlink_hardlink_mode_and_oversize_fail_without_rewriting_evidence(self):
        header = self.root / HEADER
        original = header.read_bytes()
        target = self.parent / 'foreign-header'
        private_write(target, original)
        for kind in ('symlink', 'hardlink', 'nonprivate', 'oversize'):
            header.unlink()
            if kind == 'symlink':
                header.symlink_to(target)
            elif kind == 'hardlink':
                os.link(target, header)
            else:
                private_write(header, original if kind == 'nonprivate' else b'x' * (RECORD_BYTE_LIMIT + 1))
                if kind == 'nonprivate':
                    header.chmod(0o644)
            before = header.lstat()
            with self.subTest(kind=kind), self.assertRaises((DiagnosticJournalError, OSError)):
                _ = self.journal.original_identity
            self.assertEqual((header.lstat().st_ino, header.lstat().st_size), (before.st_ino, before.st_size))
            self.assertEqual(target.read_bytes(), original)

    def test_root_and_writer_lock_replacement_reject_both_accessors_and_preserve_foreign_names(self):
        for index, kind in enumerate(('root', 'lock')):
            root = self.parent / f'replaced-{index}'
            owner = self.open_journal(create=True, root=root)
            if kind == 'root':
                root.rename(self.parent / f'original-{index}')
                root.mkdir(mode=0o700)
                private_write(root / 'foreign', b'foreign directory is not adopted')
            else:
                replacement = root / 'replacement-lock'
                private_write(replacement, b'foreign lock is not adopted')
                replacement.replace(root / 'owner.lock')
            before = files(root)
            self.both_refuse(owner)
            self.assertEqual(files(root), before)

    def test_identity_rechecks_uncertain_or_closed_owner_after_reading_header(self):
        for index, kind in enumerate(('pending', 'closed', 'lock')):
            root = self.parent / f'read-race-{index}'
            owner = self.open_journal(create=True, root=root)
            actual_read = owner._read
            def read_then_change(name):
                raw = actual_read(name)
                if kind == 'pending':
                    private_write(root / PENDING, b'new uncertainty during header read')
                elif kind == 'closed':
                    owner.close()
                else:
                    replacement = root / 'new-lock'
                    private_write(replacement, b'replaced during header read')
                    replacement.replace(root / 'owner.lock')
                return raw
            with self.subTest(kind=kind), patch.object(owner, '_read', side_effect=read_then_change):
                with self.assertRaises((DiagnosticJournalError, OSError)):
                    _ = owner.original_identity
            self.assertEqual((root / HEADER).read_bytes(), expected_header(root))


if __name__ == '__main__':
    unittest.main()
