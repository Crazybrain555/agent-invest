"""Actual generated FastAPI R6 paths with a synthetic parser boundary only."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol
from tests._mineru_result_capacity_fastapi_fixture import BoundaryHTTPException, ResultCapacityApiFixture


class ResultReservationFastApiTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, **options):
        temporary = tempfile.TemporaryDirectory(prefix='independent-r6-api-')
        self.addCleanup(temporary.cleanup)
        fixture = ResultCapacityApiFixture(Path(temporary.name), **options)
        self.addCleanup(fixture.close)
        self.addAsyncCleanup(fixture.dispose_test_tasks)
        return fixture

    def retained(self, fixture, name='retained', size=31):
        task = fixture.seed_legacy_pending(name)
        registry = fixture.manager.task_protocol_v2
        key = task.agent_idempotency_key
        registry.transition(key, 'processing')
        registry.transition(key, 'finalizing')
        registry.reserve_finalizer(key, byte_budget=size)
        raw = b'R' * size
        path = Path(task.output_dir) / '.retained-result.zip'
        path.write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        owner = hashlib.sha256(f'{task.task_id}\0{sha}\0{size}'.encode()).hexdigest()
        registry.complete(key, result_path=path, result_sha256=sha, result_bytes=size, result_owner=owner)
        return task

    async def full_waiters(self, fixture, names):
        self.retained(fixture)
        pending = [fixture.seed_legacy_pending(name) for name in names]
        seen, all_waiting = set(), asyncio.Event()
        registry = fixture.manager.task_protocol_v2
        original = registry.reserve_result_for_parse
        def reserve(key, **kwargs):
            try:
                return original(key, **kwargs)
            except protocol.TaskResultCapacityFull:
                seen.add(key)
                if len(seen) == len(pending):
                    all_waiting.set()
                raise
        observer = patch.object(registry, 'reserve_result_for_parse', side_effect=reserve)
        observer.start()
        self.addCleanup(observer.stop)
        parse_calls = []
        fixture.fail_parser(seen=parse_calls)
        await fixture.manager.start()
        await asyncio.wait_for(all_waiting.wait(), 2)
        return pending, parse_calls

    def test_manager_rejects_budget_larger_than_limit_before_acceptance(self):
        with tempfile.TemporaryDirectory(prefix='independent-r6-invalid-config-') as name:
            with self.assertRaises(ValueError):
                ResultCapacityApiFixture(Path(name), budget=74, limit=73)
            self.assertFalse((Path(name) / '.agent-task-protocol-v2/registry.json').exists())

    async def test_real_ack_endpoint_releases_full_waiter_and_does_not_map_full_to_failure(self):
        fixture = self.fixture(budget=31, limit=31)
        (task,), calls = await self.full_waiters(fixture, ['waiting'])
        manager = fixture.manager
        self.assertEqual(manager.get(task.task_id).status, 'pending')
        self.assertEqual(manager.task_wait_failures, {})
        self.assertIsNone(manager.last_worker_error)
        self.assertEqual(calls, [])
        result = await fixture.module.ack_async_task_result('legacy-retained')
        self.assertEqual(result, {'schema': 'mineru-task-protocol.v2', 'task_id': 'legacy-retained', 'status': 'consumed'})
        done = await asyncio.wait_for(manager.wait_for_terminal_state(task.task_id), 2)
        self.assertEqual(done.status, 'failed')
        self.assertEqual(calls, [task.task_id])
        self.assertEqual(manager.task_protocol_v2.get(task.agent_idempotency_key).reserved_result_bytes, 31)
        await fixture.module.ack_async_task_result(task.task_id)
        await asyncio.wait_for(manager.shutdown(), 2)
        self.assertEqual(manager.task_protocol_v2.reserved_result_bytes, 0)

    async def test_real_manager_and_zip_receive_original_budget_not_later_environment(self):
        fixture = self.fixture(budget=2097152, limit=4194304)
        manager = fixture.manager
        seen = []
        async def parser_boundary(**kwargs):
            task = kwargs['request_options']
            record = manager.task_protocol_v2.get(task.agent_idempotency_key)
            self.assertEqual((record.state, record.reserved_result_bytes), ('processing', 2097152))
            parse_dir = Path(fixture.module.get_parse_dir(task.output_dir, 'paper', task.backend, task.parse_method))
            parse_dir.mkdir(parents=True)
            (parse_dir / 'paper.md').write_bytes(b'independent synthetic parser boundary\n')
            os.environ['MINERU_TASK_PROTOCOL_V2_RESULT_RESERVATION_BYTES'] = '1'
            seen.append(task.task_id)
        fixture.module.run_parse_job = parser_boundary
        await manager.start()
        original = fixture.module._retained_result_sources
        with patch.object(fixture.module, '_retained_result_sources', wraps=original) as sources:
            task = await fixture.create(fixture.options('actual-zip'))
            done = await asyncio.wait_for(manager.wait_for_terminal_state(task.task_id), 3)
        self.assertEqual(done.status, 'completed', done.error)
        self.assertEqual(seen, [task.task_id])
        self.assertEqual(sources.call_args.kwargs, {'byte_budget': 2097152})
        record = manager.task_protocol_v2.get(task.agent_idempotency_key)
        self.assertEqual(record.reserved_result_bytes, 0)
        self.assertGreater(record.result_bytes, 0)
        self.assertLessEqual(record.result_bytes, 2097152)
        raw = Path(record.result_path).read_bytes()
        self.assertEqual(record.result_sha256, hashlib.sha256(raw).hexdigest())
        with zipfile.ZipFile(record.result_path) as archive:
            self.assertEqual(archive.namelist(), ['paper/auto/paper.md'])
            self.assertEqual(archive.read('paper/auto/paper.md'), b'independent synthetic parser boundary\n')
        await fixture.module.ack_async_task_result(task.task_id)
        self.assertFalse(Path(task.output_dir).exists())
        await asyncio.wait_for(manager.shutdown(), 2)

    async def test_recovery_error_keeps_accepted_pending_and_returns_exact_503_without_fake_phase(self):
        fixture = self.fixture(budget=31, limit=73)
        legacy = fixture.seed_legacy_pending('unbudgeted')
        registry = fixture.manager.task_protocol_v2
        registry.transition(legacy.agent_idempotency_key, 'processing')
        (Path(legacy.output_dir) / 'unbudgeted-partial').write_bytes(b'partial')
        registry.fail(legacy.agent_idempotency_key, error='old failure without result budget')
        task = fixture.seed_legacy_pending('blocked')
        calls = []
        fixture.fail_parser(seen=calls)
        await fixture.manager.start()
        await asyncio.wait_for(fixture.manager.task_events[task.task_id].wait(), 2)
        self.assertEqual(calls, [])
        self.assertEqual(registry.get(task.agent_idempotency_key).state, 'pending')
        self.assertEqual(fixture.manager.tasks[task.task_id].status, 'pending')
        with self.assertRaises(BoundaryHTTPException) as caught:
            fixture.manager.get(task.task_id)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail, {'code': 'result_capacity_recovery_required',
                         'task_id': task.task_id, 'accepted': True})
        self.assertIsInstance(caught.exception.__cause__, protocol.TaskResultCapacityRecoveryRequired)
        with self.assertRaises(fixture.module.TaskWaitAbortedError):
            await asyncio.wait_for(fixture.manager.wait_for_terminal_state(task.task_id), 2)

    async def test_actual_cleanup_loop_failure_wakes_capacity_waiter_without_parse(self):
        fixture = self.fixture(budget=31, limit=31)
        (task,), calls = await self.full_waiters(fixture, ['waiting'])
        manager = fixture.manager
        waiter = next(t for t in manager.active_tasks if t.get_name().endswith(task.task_id))
        manager.task_cleanup_interval_seconds = 0
        marker = OSError('actual-cleanup-loop-fault')
        with patch.object(manager, 'cleanup_expired_tasks', side_effect=marker):
            with self.assertRaises(OSError) as caught:
                await manager._cleanup_loop()
        self.assertIs(caught.exception, marker)
        with self.assertRaises(protocol.TaskExecutionStopped):
            await asyncio.wait_for(waiter, 0.5)
        self.assertEqual(calls, [])
        self.assertEqual(manager.task_protocol_v2.get(task.agent_idempotency_key).state, 'pending')
        self.assertEqual(manager.tasks[task.task_id].status, 'pending')

    async def test_actual_cancelled_processor_callback_wakes_other_capacity_waiter(self):
        fixture = self.fixture(budget=31, limit=31, pending=2)
        tasks, calls = await self.full_waiters(fixture, ['victim', 'other'])
        manager = fixture.manager
        active = {task.get_name(): task for task in manager.active_tasks}
        victim = active['mineru-fastapi-task-' + tasks[0].task_id]
        other = active['mineru-fastapi-task-' + tasks[1].task_id]
        victim.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await victim
        with self.assertRaises(protocol.TaskExecutionStopped):
            await asyncio.wait_for(other, 0.5)
        self.assertEqual(calls, [])
        for task in tasks:
            self.assertEqual(manager.task_protocol_v2.get(task.agent_idempotency_key).state, 'pending')
            self.assertEqual(manager.tasks[task.task_id].status, 'pending')

    async def test_actual_shutdown_with_full_waiter_reports_incomplete_and_preserves_pending(self):
        fixture = self.fixture(budget=31, limit=31)
        (task,), calls = await self.full_waiters(fixture, ['waiting'])
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(fixture.manager.shutdown(), 2)
        self.assertEqual(calls, [])
        self.assertEqual(fixture.manager.task_protocol_v2.get(task.agent_idempotency_key).state, 'pending')
        self.assertEqual(fixture.manager.tasks[task.task_id].status, 'pending')
        self.assertEqual(fixture.manager.task_protocol_v2.unacked_result_bytes, 31)

    async def test_soft_full_deferral_does_not_abort_reserved_companion_or_refill_itself(self):
        # Explicit offline N1/P3 interleaving; this does not qualify P3 deployment.
        fixture = self.fixture(budget=31, limit=62, pending=3)
        tasks = [fixture.seed_legacy_pending(name) for name in ('a-active', 'b-reserved', 'c-full')]
        manager, registry = fixture.manager, fixture.manager.task_protocol_v2
        first_entered, release_first, second_reserved, third_full = (
            asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
        )
        calls = []
        third_attempts = 0
        original = registry.reserve_result_for_parse
        def reserve(key, **kwargs):
            nonlocal third_attempts
            if key == tasks[2].agent_idempotency_key:
                third_attempts += 1
            try:
                result = original(key, **kwargs)
            except protocol.TaskResultCapacityFull:
                if key == tasks[2].agent_idempotency_key:
                    third_full.set()
                raise
            if key == tasks[1].agent_idempotency_key:
                second_reserved.set()
            return result
        async def parser_boundary(**kwargs):
            task = kwargs['request_options']
            calls.append(task.task_id)
            if task.task_id == tasks[0].task_id:
                first_entered.set()
                await release_first.wait()
            raise RuntimeError('synthetic parser failure after owned soft drain')
        fixture.module.run_parse_job = parser_boundary
        shutdown = None
        with patch.object(registry, 'reserve_result_for_parse', side_effect=reserve):
            try:
                await manager.start()
                await asyncio.wait_for(first_entered.wait(), 2)
                await asyncio.wait_for(second_reserved.wait(), 2)
                await asyncio.wait_for(third_full.wait(), 2)
                active = {task.get_name(): task for task in manager.active_tasks}
                first, second, third = [active['mineru-fastapi-task-' + item.task_id] for item in tasks]
                self.assertEqual([registry.get(item.agent_idempotency_key).reserved_result_bytes
                                  for item in tasks], [31, 31, 0])
                third_finished = asyncio.Event()
                third.add_done_callback(lambda done: third_finished.set())
                shutdown = asyncio.create_task(manager.shutdown())
                await asyncio.wait_for(third_finished.wait(), 2)
                self.assertIsNone(manager.last_worker_error,
                                  'ordinary Full deferral was promoted into a worker fault')
                self.assertFalse(shutdown.done(), 'shutdown returned before affordable accepted work drained')
                attempts_at_deferral = third_attempts
                self.assertEqual(calls, [tasks[0].task_id])
                release_first.set()
                await asyncio.wait_for(asyncio.gather(first, second), 2)
                with self.assertRaisesRegex(RuntimeError, 'durable task responsibilities'):
                    await asyncio.wait_for(shutdown, 2)
                self.assertEqual(calls, [tasks[0].task_id, tasks[1].task_id])
                self.assertEqual(third_attempts, attempts_at_deferral,
                                 'soft-deferred Full task was repeatedly rescheduled')
                self.assertIsNone(manager.last_worker_error)
                self.assertEqual(manager._scheduled_task_ids, set())
                self.assertEqual([registry.get(item.agent_idempotency_key).state for item in tasks],
                                 ['failed', 'failed', 'pending'])
                self.assertEqual([registry.get(item.agent_idempotency_key).reserved_result_bytes
                                  for item in tasks], [31, 31, 0])
                self.assertTrue((Path(tasks[2].output_dir) / 'uploads/paper.pdf').exists())
                for item in tasks[:2]:
                    await fixture.module.ack_async_task_result(item.task_id)
                    self.assertFalse(Path(item.output_dir).exists())
                self.assertEqual(registry.reserved_result_bytes, 0)
                self.assertEqual(registry.get(tasks[2].agent_idempotency_key).state, 'pending')
                manager._refill_pending_queue()
                self.assertEqual(manager._scheduled_task_ids, set())
                # Only an explicit restart of this owner re-enables the original
                # deferred responsibility after the old failed trees are gone.
                await manager.start()
                resumed = await asyncio.wait_for(manager.wait_for_terminal_state(tasks[2].task_id), 2)
                self.assertEqual(resumed.status, 'failed')
                self.assertEqual(calls, [item.task_id for item in tasks])
                self.assertEqual(registry.get(tasks[2].agent_idempotency_key).reserved_result_bytes, 31)
                await fixture.module.ack_async_task_result(tasks[2].task_id)
                self.assertFalse(Path(tasks[2].output_dir).exists())
                await asyncio.wait_for(manager.shutdown(), 2)
                self.assertEqual(registry.reserved_result_bytes, 0)
            finally:
                release_first.set()
                if shutdown is not None:
                    # Test-owned cleanup retrieves the observed failure; it never
                    # substitutes for the shutdown/closure assertions above.
                    if not shutdown.done():
                        shutdown.cancel()
                    await asyncio.wait_for(asyncio.gather(shutdown, return_exceptions=True), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
