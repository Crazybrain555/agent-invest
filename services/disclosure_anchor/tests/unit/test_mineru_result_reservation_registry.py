"""Independent R6 result-credit invariants on real private registry files.

Small hand-derived byte totals deliberately differ from deployment defaults.
No parser, network, database, GPU or production output root is used.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol
from tests._mineru_result_reservation_fixture import ReservationLab


class ResultReservationRegistryTests(unittest.TestCase):
    def lab(self, limit=73):
        temporary = tempfile.TemporaryDirectory(prefix='independent-r6-registry-')
        self.addCleanup(temporary.cleanup)
        return ReservationLab(Path(temporary.name), limit=limit)

    def reserve(self, registry, key, value):
        registry.reserve_result_for_parse(key, byte_budget=value)

    def test_exact_boundary_and_one_more_byte_use_retained_plus_all_reservations(self):
        lab = self.lab()
        lab.completed('retained', budget=19, size=11)
        for name in ('left', 'right', 'excess'):
            lab.pending(name)
        self.reserve(lab.registry, 'left', 31)
        self.reserve(lab.registry, 'right', 31)
        self.assertEqual((lab.registry.unacked_result_bytes, lab.registry.reserved_result_bytes), (11, 62))
        before = lab.path.read_bytes()
        with self.assertRaises(protocol.TaskResultCapacityFull):
            self.reserve(lab.registry, 'excess', 1)
        self.assertEqual(lab.path.read_bytes(), before)
        self.assertEqual(lab.registry.get('excess').state, 'pending')
        self.assertEqual(lab.registry.get('excess').reserved_result_bytes, 0)

    def test_same_key_same_budget_is_a_no_write_idempotent_operation(self):
        lab = self.lab()
        lab.pending('same')
        self.reserve(lab.registry, 'same', 37)
        before = lab.path.read_bytes()
        with patch.object(lab.registry, '_persist', side_effect=AssertionError('duplicate reservation persisted')):
            self.reserve(lab.registry, 'same', 37)
        self.assertEqual(lab.path.read_bytes(), before)
        self.assertEqual(lab.registry.reserved_result_bytes, 37)
        self.assertEqual(lab.registry.get('same').state, 'pending')
        for other in (1, 36, 38):
            with self.subTest(other=other), self.assertRaises(protocol.TaskProtocolConflict):
                self.reserve(lab.registry, 'same', other)
            self.assertEqual(lab.path.read_bytes(), before)

    def test_invalid_budget_and_nonpending_identity_cannot_acquire_a_reservation(self):
        lab = self.lab()
        lab.pending('new')
        before = lab.path.read_bytes()
        for value in (0, -1, True, False, 1.0, '1', None):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                self.reserve(lab.registry, 'new', value)
            self.assertEqual(lab.path.read_bytes(), before)
        with self.assertRaises((ValueError, protocol.TaskProtocolConflict)):
            self.reserve(lab.registry, 'absent', 1)
        lab.registry.fail('new', error='failure before parse')
        before = lab.path.read_bytes()
        with self.assertRaises(protocol.TaskProtocolConflict):
            self.reserve(lab.registry, 'new', 1)
        self.assertEqual(lab.path.read_bytes(), before)

    def test_same_budget_in_processing_and_finalizing_does_not_redebit(self):
        lab = self.lab(limit=37)
        lab.pending('running')
        self.reserve(lab.registry, 'running', 37)
        for state in ('processing', 'finalizing'):
            lab.registry.transition('running', state)
            before = lab.path.read_bytes()
            with patch.object(lab.registry, '_persist', side_effect=AssertionError('existing budget persisted')):
                self.reserve(lab.registry, 'running', 37)
            self.assertEqual(lab.path.read_bytes(), before)
            self.assertEqual(lab.registry.reserved_result_bytes, 37)

    def test_detached_record_mutation_cannot_free_credit(self):
        lab = self.lab(limit=37)
        lab.pending('held')
        lab.pending('next')
        self.reserve(lab.registry, 'held', 37)
        forged = lab.registry.get('held')
        forged.reserved_result_bytes = 0
        forged.state = 'failed'
        with self.assertRaises(protocol.TaskResultCapacityFull):
            self.reserve(lab.registry, 'next', 1)
        self.assertEqual(lab.registry.reserved_result_bytes, 37)

    def test_two_real_threads_cannot_both_take_last_capacity(self):
        lab = self.lab(limit=37)
        for name in ('one', 'two'):
            lab.pending(name)
        barrier = threading.Barrier(2, timeout=2)
        outcomes = []
        errors = []
        def contender(name):
            try:
                barrier.wait()
                try:
                    self.reserve(lab.registry, name, 37)
                except protocol.TaskResultCapacityFull:
                    outcomes.append('full')
                else:
                    outcomes.append('reserved')
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=contender, args=(name,), daemon=True) for name in ('one', 'two')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertFalse(any(thread.is_alive() for thread in threads), 'owned finite contenders did not close')
        self.assertEqual(errors, [])
        self.assertCountEqual(outcomes, ['reserved', 'full'])
        self.assertEqual(lab.registry.reserved_result_bytes, 37)
        self.assertEqual(sum(row['reserved_result_bytes'] for row in lab.rows().values()), 37)

    def test_complete_exchanges_reserved_credit_for_actual_bytes_atomically(self):
        lab = self.lab(limit=37)
        lab.pending('done')
        lab.pending('next')
        self.reserve(lab.registry, 'done', 37)
        lab.registry.transition('done', 'processing')
        lab.registry.transition('done', 'finalizing')
        lab.registry.complete('done', **lab.result('done', 11))
        self.assertEqual((lab.registry.unacked_result_bytes, lab.registry.reserved_result_bytes), (11, 0))
        self.reserve(lab.registry, 'next', 26)
        self.assertEqual((lab.registry.unacked_result_bytes, lab.registry.reserved_result_bytes), (11, 26))
        self.assertEqual(lab.rows()['done']['reserved_result_bytes'], 0)

    def test_finalizer_oversize_keeps_original_reservation_and_partial_file(self):
        lab = self.lab(limit=31)
        lab.old_reserved('large', 31)
        result = lab.result('large', 32)
        before = lab.path.read_bytes()
        with self.assertRaises(protocol.TaskProtocolConflict):
            lab.registry.complete('large', **result)
        self.assertEqual(lab.path.read_bytes(), before)
        self.assertTrue(result['result_path'].exists())
        self.assertEqual(lab.registry.reserved_result_bytes, 31)
        lab.registry.fail('large', error='actual result exceeded the envelope')
        self.assertEqual(lab.registry.reserved_result_bytes, 31)

    def test_fail_and_generic_failed_transition_keep_reservation_without_cleanup(self):
        for generic in (False, True):
            with self.subTest(generic=generic):
                lab = self.lab(limit=31)
                task = lab.old_reserved('failed', 31)
                partial = task / '.retained-result.zip.part'
                partial.write_bytes(b'partial')
                if generic:
                    lab.registry.transition('failed', 'failed')
                else:
                    lab.registry.fail('failed', error='original parser failure')
                self.assertEqual(lab.registry.get('failed').state, 'failed')
                self.assertEqual(lab.registry.reserved_result_bytes, 31)
                self.assertEqual(lab.rows()['failed']['reserved_result_bytes'], 31)
                self.assertEqual(partial.read_bytes(), b'partial')

    def test_failed_ack_deletion_failure_holds_budget_until_real_tree_cleanup(self):
        lab = self.lab(limit=31)
        task = lab.old_reserved('failed', 31)
        partial = task / '.retained-result.zip.part'
        partial.write_bytes(b'partial')
        lab.pending('next')
        lab.registry.fail('failed', error='parse failed')
        marker = OSError('exact-partial-delete-marker')
        with patch.object(lab.registry, '_remove_at', side_effect=marker):
            with self.assertRaises(OSError) as caught:
                lab.registry.acknowledge_failed('failed')
        self.assertIs(caught.exception, marker)
        self.assertTrue(partial.exists())
        row = lab.registry.get('failed')
        self.assertEqual((row.state, row.cleanup_kind, row.reserved_result_bytes), ('cleanup_pending', 'task_tree', 31))
        with self.assertRaises(protocol.TaskResultCapacityFull):
            self.reserve(lab.registry, 'next', 1)
        self.assertEqual(lab.registry.cleanup_consumed(), 1)
        self.assertFalse(task.exists())
        self.assertEqual(lab.registry.reserved_result_bytes, 0)
        self.reserve(lab.registry, 'next', 31)
        self.assertEqual(lab.registry.reserved_result_bytes, 31)

    def test_tree_absence_before_consumed_persist_does_not_release_credit(self):
        lab = self.lab(limit=31)
        task = lab.old_reserved('failed', 31)
        (task / 'partial').write_bytes(b'partial')
        lab.registry.fail('failed', error='parse failed')
        flush = lab.registry._flush_registry_stream
        calls = 0
        marker = OSError('consumed-flush-failure-after-physical-delete')
        def fail_second_flush(stream):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise marker
            return flush(stream)
        with patch.object(lab.registry, '_flush_registry_stream', side_effect=fail_second_flush):
            with self.assertRaises(protocol.TaskRegistryPersistenceError) as caught:
                lab.registry.acknowledge_failed('failed')
        self.assertEqual(caught.exception.outcome, 'not_committed')
        self.assertIs(caught.exception.__cause__, marker)
        self.assertFalse(task.exists())
        self.assertEqual(lab.rows()['failed']['state'], 'cleanup_pending')
        self.assertEqual(lab.registry.reserved_result_bytes, 31)
        cold = lab.cold()
        self.assertEqual(cold.reserved_result_bytes, 31)
        self.assertEqual(cold.cleanup_consumed(), 1)
        self.assertEqual(cold.reserved_result_bytes, 0)
        self.assertEqual(cold.get('failed').state, 'consumed')

    def test_cold_replay_cleanup_failure_preserves_original_budget_and_generation(self):
        lab = self.lab(limit=31)
        task = lab.old_reserved('interrupted', 31)
        original = lab.registry.get('interrupted')
        partial = task / '.retained-result.zip.part'
        partial.write_bytes(b'partial')
        cold = lab.cold()
        marker = OSError('replay-cleanup-before-delete')
        with patch.object(cold, '_remove_at', side_effect=marker):
            with self.assertRaises(OSError) as caught:
                cold.recoverable_payloads()
        self.assertIs(caught.exception, marker)
        observed = cold.get('interrupted')
        self.assertEqual((observed.state, observed.reserved_result_bytes), ('pending', 31))
        self.assertEqual(observed.recovery_generation, original.recovery_generation + 1)
        self.assertTrue(partial.exists())
        recovered = lab.cold()
        payloads = recovered.recoverable_payloads()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]['task_id'], 'interrupted')
        self.assertEqual(set(p.name for p in task.iterdir()), {'uploads'})
        self.assertEqual(recovered.reserved_result_bytes, 31)
        self.reserve(recovered, 'interrupted', 31)
        self.assertEqual(recovered.reserved_result_bytes, 31)
        self.assertEqual(recovered.get('interrupted').recovery_generation, original.recovery_generation + 1)

    def test_empty_replay_retry_must_fsync_even_after_last_partial_was_deleted(self):
        lab = self.lab(limit=31)
        task = lab.old_reserved('interrupted', 31)
        partial = task / '.retained-result.zip.part'
        partial.write_bytes(b'partial')
        cold = lab.cold()
        marker = OSError('namespace-fsync-after-last-unlink')
        real_fsync = os.fsync
        task_identity = (task.stat().st_dev, task.stat().st_ino)
        def task_fsync_failure(fd, error):
            metadata = os.fstat(fd)
            if stat.S_ISDIR(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == task_identity:
                raise error
            return real_fsync(fd)
        with patch.object(os, 'fsync', side_effect=lambda fd: task_fsync_failure(fd, marker)):
            with self.assertRaises(OSError) as first:
                cold.recoverable_payloads()
        self.assertIs(first.exception, marker)
        self.assertFalse(partial.exists())
        self.assertEqual(set(p.name for p in task.iterdir()), {'uploads'})
        retry_marker = OSError('namespace-fsync-on-empty-retry')
        with patch.object(os, 'fsync', side_effect=lambda fd: task_fsync_failure(fd, retry_marker)):
            with self.assertRaises(OSError) as retry:
                cold.recoverable_payloads()
        self.assertIs(retry.exception, retry_marker)
        with self.assertRaises(protocol.TaskResultCapacityRecoveryRequired):
            self.reserve(cold, 'interrupted', 31)
        self.assertEqual(cold.reserved_result_bytes, 31)
        cold.recoverable_payloads()
        self.reserve(cold, 'interrupted', 31)
        self.assertEqual(cold.reserved_result_bytes, 31)

    def test_replay_uncertainty_reconciled_to_pending_still_requires_physical_cleanup(self):
        lab = self.lab(limit=31)
        task = lab.old_reserved('interrupted', 31)
        partial = task / '.retained-result.zip.part'
        partial.write_bytes(b'partial')
        marker = OSError('uncertain-replay-state-parent-fsync')
        with patch.object(lab.registry, '_fsync_parent_descriptor', side_effect=marker):
            with self.assertRaises(protocol.TaskRegistryPersistenceError) as caught:
                lab.registry.recoverable_payloads()
        self.assertEqual(caught.exception.outcome, 'durability_uncertain')
        self.assertIs(caught.exception.__cause__, marker)
        lab.registry.recover_persistence_uncertainty()
        self.assertEqual(lab.registry.get('interrupted').state, 'pending')
        self.assertTrue(partial.exists())
        self.assertEqual(lab.registry.reserved_result_bytes, 31)
        with self.assertRaises(protocol.TaskResultCapacityRecoveryRequired):
            self.reserve(lab.registry, 'interrupted', 31)
        self.assertTrue(partial.exists())
        lab.registry.recoverable_payloads()
        self.assertFalse(partial.exists())
        self.reserve(lab.registry, 'interrupted', 31)
        self.assertEqual(lab.registry.reserved_result_bytes, 31)

    def test_each_real_precommit_reserve_failure_preserves_exact_bytes_and_original_cause(self):
        hooks = ('_write_registry_stream', '_flush_registry_stream', '_fsync_registry_file', '_replace_registry_file')
        for hook in hooks:
            with self.subTest(hook=hook):
                lab = self.lab(limit=31)
                lab.pending('new')
                before = lab.path.read_bytes()
                marker = OSError('reserve-storage-marker-' + hook)
                with patch.object(lab.registry, hook, side_effect=marker):
                    with self.assertRaises(protocol.TaskRegistryPersistenceError) as caught:
                        self.reserve(lab.registry, 'new', 31)
                self.assertEqual(caught.exception.outcome, 'not_committed')
                self.assertFalse(caught.exception.committed)
                self.assertIs(caught.exception.__cause__, marker)
                self.assertEqual(lab.path.read_bytes(), before)
                self.assertEqual(lab.registry.reserved_result_bytes, 0)
                self.assertEqual(lab.registry.get('new').state, 'pending')
                self.reserve(lab.registry, 'new', 31)
                self.assertEqual(lab.registry.reserved_result_bytes, 31)

    def test_uncertain_reserve_blocks_new_work_until_explicit_byte_reconciliation(self):
        lab = self.lab(limit=31)
        lab.pending('new')
        lab.pending('other')
        marker = OSError('parent-fsync-remains-unknown')
        with patch.object(lab.registry, '_fsync_parent_descriptor', side_effect=marker):
            with self.assertRaises(protocol.TaskRegistryPersistenceError) as caught:
                self.reserve(lab.registry, 'new', 31)
        self.assertEqual(caught.exception.outcome, 'durability_uncertain')
        self.assertFalse(caught.exception.committed)
        self.assertIs(caught.exception.__cause__, marker)
        self.assertEqual(lab.registry.reserved_result_bytes, 31)
        self.assertEqual(lab.rows()['new']['reserved_result_bytes'], 31)
        with self.assertRaises(protocol.TaskRegistryPersistenceError):
            self.reserve(lab.registry, 'other', 1)
        status = lab.registry.recover_persistence_uncertainty()
        self.assertNotEqual(status['state'], 'durability_uncertain')
        self.reserve(lab.registry, 'new', 31)
        self.assertEqual(lab.registry.reserved_result_bytes, 31)
        with self.assertRaises(protocol.TaskResultCapacityFull):
            self.reserve(lab.registry, 'other', 1)

    def test_committed_parent_close_failure_is_visible_but_not_double_debited(self):
        lab = self.lab(limit=31)
        lab.pending('new')
        marker = OSError('parent-close-consumed-descriptor-marker')
        def close_then_fail(fd):
            os.close(fd)
            raise marker
        with patch.object(lab.registry, '_close_parent_descriptor', side_effect=close_then_fail):
            self.reserve(lab.registry, 'new', 31)
        self.assertEqual(lab.registry.reserved_result_bytes, 31)
        before = lab.path.read_bytes()
        self.reserve(lab.registry, 'new', 31)
        self.assertEqual(lab.path.read_bytes(), before)
        with self.assertRaises(protocol.TaskRegistryPersistenceError) as caught:
            lab.registry.assert_persistence_healthy()
        self.assertTrue(caught.exception.committed)
        self.assertEqual(caught.exception.outcome, 'committed_cleanup_failed')
        self.assertIs(caught.exception.__cause__, marker)

    def test_legacy_v2_and_v3_zero_debt_dirty_failure_blocks_new_parse_until_owned_ack(self):
        for schema in ('mineru-task-registry.v2', 'mineru-task-registry.v3'):
            with self.subTest(schema=schema):
                lab = self.lab(limit=31)
                task = lab.pending('legacy-failed')
                lab.registry.transition('legacy-failed', 'processing')
                (task / '.retained-result.zip.part').write_bytes(b'old-unbudgeted-partial')
                lab.registry.fail('legacy-failed', error='historical failure before R6')
                lab.pending('new')
                lab.legacy_wire(schema)
                cold = lab.cold()
                cold.recoverable_payloads()
                self.assertEqual(cold.get('legacy-failed').state, 'failed')
                self.assertEqual(cold.reserved_result_bytes, 0)
                before = lab.path.read_bytes()
                with self.assertRaises(protocol.TaskResultCapacityRecoveryRequired):
                    self.reserve(cold, 'new', 1)
                self.assertEqual(lab.path.read_bytes(), before)
                self.assertTrue(task.exists())
                cold.acknowledge_failed('legacy-failed')
                self.assertFalse(task.exists())
                self.reserve(cold, 'new', 31)
                self.assertEqual(cold.reserved_result_bytes, 31)

    def test_legacy_active_zero_budget_requires_original_clean_replay_before_reserve(self):
        for schema in ('mineru-task-registry.v2', 'mineru-task-registry.v3'):
            for state in ('processing', 'finalizing'):
                with self.subTest(schema=schema, state=state):
                    lab = self.lab(limit=31)
                    task = lab.pending('old')
                    lab.registry.transition('old', 'processing')
                    if state == 'finalizing':
                        lab.registry.transition('old', state)
                    partial = task / '.retained-result.zip.part'
                    partial.write_bytes(b'old-unbudgeted-partial')
                    lab.legacy_wire(schema)
                    cold = lab.cold()
                    before = lab.path.read_bytes()
                    with self.assertRaises(protocol.TaskResultCapacityRecoveryRequired):
                        self.reserve(cold, 'old', 31)
                    self.assertEqual(lab.path.read_bytes(), before)
                    self.assertTrue(partial.exists())
                    self.assertEqual(cold.reserved_result_bytes, 0)
                    payloads = cold.recoverable_payloads()
                    self.assertEqual([item['task_id'] for item in payloads], ['old'])
                    self.assertEqual(cold.get('old').state, 'pending')
                    self.assertFalse(partial.exists())
                    self.assertTrue((task / 'uploads/source.pdf').exists())
                    self.reserve(cold, 'old', 31)
                    self.assertEqual(cold.reserved_result_bytes, 31)

    def test_legacy_cleanup_pending_zero_budget_blocks_until_actual_task_tree_removal(self):
        for schema in ('mineru-task-registry.v2', 'mineru-task-registry.v3'):
            with self.subTest(schema=schema):
                lab = self.lab(limit=31)
                task = lab.pending('old-failed')
                partial = task / 'unbudgeted-partial'
                partial.write_bytes(b'old-partial')
                lab.registry.fail('old-failed', error='legacy unbudgeted failure')
                marker = OSError('legacy-failed-ack-removal')
                with patch.object(lab.registry, '_remove_at', side_effect=marker):
                    with self.assertRaises(OSError) as caught:
                        lab.registry.acknowledge_failed('old-failed')
                self.assertIs(caught.exception, marker)
                lab.pending('new')
                lab.legacy_wire(schema)
                cold = lab.cold()
                cold.recoverable_payloads()
                row = cold.get('old-failed')
                self.assertEqual((row.state, row.cleanup_kind, row.reserved_result_bytes),
                                 ('cleanup_pending', 'task_tree', 0))
                before = lab.path.read_bytes()
                with self.assertRaises(protocol.TaskResultCapacityRecoveryRequired):
                    self.reserve(cold, 'new', 1)
                self.assertEqual(lab.path.read_bytes(), before)
                self.assertEqual(partial.read_bytes(), b'old-partial')
                self.assertEqual(cold.cleanup_consumed(), 1)
                self.assertFalse(task.exists())
                self.reserve(cold, 'new', 31)
                self.assertEqual(cold.reserved_result_bytes, 31)

    def test_loaded_retained_and_reserved_debt_over_new_limit_never_gets_fresh_credit(self):
        lab = self.lab(limit=73)
        lab.completed('retained', budget=41, size=41)
        lab.old_reserved('interrupted', 17)
        lab.pending('new')
        lab.limit = 37
        cold = lab.cold()
        cold.recoverable_payloads()
        self.assertEqual((cold.unacked_result_bytes, cold.reserved_result_bytes), (41, 17))
        before = lab.path.read_bytes()
        for key, budget in (('new', 1), ('interrupted', 17)):
            with self.subTest(key=key), self.assertRaises(protocol.TaskResultCapacityRecoveryRequired):
                self.reserve(cold, key, budget)
            self.assertEqual(lab.path.read_bytes(), before)
        cold.acknowledge('retained')
        self.assertEqual(cold.unacked_result_bytes, 41)
        self.assertEqual(cold.cleanup_consumed(), 1)
        self.assertEqual((cold.unacked_result_bytes, cold.reserved_result_bytes), (0, 17))
        self.reserve(cold, 'interrupted', 17)
        self.reserve(cold, 'new', 20)
        self.assertEqual(cold.reserved_result_bytes, 37)


if __name__ == '__main__':
    unittest.main(verbosity=2)
