"""A1 actual generated lifecycle; unparsed synthetic uploads, no model/network IO."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from tests._mineru_capacity_lifecycle_fixture import CapacityLifecycleFixture, Upload, BoundaryHTTPException

BUDGET = 2097152
EMPTY_STAGES = {'result_capacity_waiting': 0, 'parse_waiting': 0, 'parse_active': 0,
                'finalizer_waiting': 0, 'finalizer_active': 0}


async def until(predicate):
    async def waiting():
        while not predicate():
            await asyncio.sleep(.001)
    await asyncio.wait_for(waiting(), 2)


class MineruCapacityLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, **kwargs):
        temporary = tempfile.TemporaryDirectory(prefix='independent-a1-lifecycle-')
        self.addCleanup(temporary.cleanup)
        fixture = CapacityLifecycleFixture(Path(temporary.name).resolve(), **kwargs)
        self.addCleanup(fixture.close)
        self.addAsyncCleanup(fixture.dispose_test_tasks)
        return fixture

    def parser(self, fixture, *, fail=False):
        entered = asyncio.Queue()
        release = asyncio.Event()
        self.addCleanup(release.set)
        async def boundary(**kwargs):
            task = kwargs['request_options']
            record = fixture.manager.task_protocol_v2.get(task.agent_idempotency_key)
            self.assertEqual((record.state, record.reserved_result_bytes), ('processing', BUDGET))
            await entered.put(task)
            await release.wait()
            if fail:
                raise RuntimeError('synthetic parser boundary; never parsed a PDF')
            directory = Path(fixture.module.get_parse_dir(task.output_dir, 'paper', task.backend, task.parse_method))
            directory.mkdir(parents=True)
            (directory/'paper.md').write_bytes(b'synthetic parser-boundary artifact\n')
        fixture.module.run_parse_job = boundary
        return entered, release

    async def completed(self, fixture, tasks):
        await until(lambda: all(t.status == 'completed' for t in tasks))
        for task in tasks:
            record = fixture.manager.task_protocol_v2.get(task.agent_idempotency_key)
            self.assertEqual((record.state, record.reserved_result_bytes), ('completed', 0))
            raw = Path(record.result_path).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), record.result_sha256)
            self.assertEqual(len(raw), record.result_bytes)
            with zipfile.ZipFile(record.result_path) as archive:
                self.assertEqual(archive.namelist(), ['paper/auto/paper.md'])
                self.assertEqual(archive.read('paper/auto/paper.md'), b'synthetic parser-boundary artifact\n')
            reply = await fixture.module.ack_async_task_result(task.task_id)
            self.assertEqual(reply['status'], 'consumed')
            self.assertFalse(Path(task.output_dir).exists())
        self.assertEqual(fixture.manager.task_protocol_v2.reserved_result_bytes, 0)
        self.assertEqual(fixture.manager.task_protocol_v2.unacked_result_bytes, 0)

    async def test_real_file_configuration_enters_original_objects_and_full_v3_health_stays_lazy(self):
        fx = self.fixture()
        manager = fx.manager
        self.assertEqual(manager.capacity_config.exact_bytes, fx.config_path.read_bytes())
        self.assertEqual(manager.capacity_config.sha256, fx.config_sha)
        self.assertEqual((manager.max_nonterminal_tasks, manager.queue.maxsize), (3, 3))
        self.assertEqual((manager.task_protocol_executor.parse_slots, manager.task_protocol_executor.finalizer_slots), (2, 1))
        self.assertEqual((manager.task_protocol_executor.result_reservation_bytes, manager.task_protocol_v2._limit), (BUDGET, BUDGET*3))
        await manager.start()
        health = await fx.module.health_check()
        self.assertEqual((health['max_concurrent_requests'], health['max_pending_tasks_effective'], health['processing_window_size']), (2, 3, 8))
        self.assertEqual(health['task_protocol_runtime'], {
            'schema':'mineru-task-runtime.v3', 'enabled':True, 'task_registry_max_records':128,
            'task_result_reservation_bytes':BUDGET, 'max_unacked_result_bytes':BUDGET*3,
            'registry_schema':'mineru-task-registry.v3', 'admission_scope':'post_form_owned_upload',
            'capacity_config_sha256':fx.config_sha})
        observed = health['capacity_observation']
        self.assertEqual(set(observed), {'schema','capacity_config_sha256','owner','resolved_limits',
            'http_limiter_state','stage_counters','http_counters','owner_control','framework_limits','observed_at'})
        self.assertEqual(observed['schema'], 'mineru.capacity-observation.v1')
        self.assertEqual(observed['capacity_config_sha256'], fx.config_sha)
        self.assertEqual(observed['resolved_limits'], {'parse_active_limit': 2, 'total_nonterminal_limit': 3,
            'finalizer_active_limit': 1, 'result_reservation_bytes': BUDGET,
            'max_unacked_result_bytes': BUDGET*3, 'final_http_limit_per_loop': None})
        self.assertEqual(observed['stage_counters'], EMPTY_STAGES)
        self.assertEqual(observed['http_limiter_state'], 'not_initialized')
        self.assertEqual(fx.http._PROCESS_ASYNC_REQUEST_LIMITERS, {})
        for name in ('mkl_threads', 'openblas_threads'):
            self.assertEqual(observed['framework_limits'][name], {'state':'unavailable','value':None,'reason':'no_serving_getter'})
        self.assertEqual(observed['owner']['process_start_ticks'], 27182)  # Explicit synthetic proc text only.
        await manager.shutdown()

    async def test_three_owned_ingress_are_atomic_limit_and_fourth_does_not_read_upload(self):
        fx = self.fixture()
        entered, parse_release = self.parser(fx, fail=True)
        await fx.manager.start()
        uploads = [Upload(entered=asyncio.Event(), release=asyncio.Event()) for _ in range(3)]
        creators = [asyncio.create_task(fx.create(fx.options('ingress-'+str(i), upload=u))) for i,u in enumerate(uploads)]
        try:
            await asyncio.wait_for(asyncio.gather(*(u.entered.wait() for u in uploads)), 2)
            health = await fx.module.health_check()
            self.assertEqual(health['task_admission']['ingress_tasks'], 3)
            self.assertEqual(health['task_admission']['durable_nonterminal_tasks'], 3)
            duplicate = Upload()
            with self.assertRaises(BoundaryHTTPException) as repeated:
                await fx.create(fx.options('ingress-0', upload=duplicate))
            self.assertEqual(repeated.exception.status_code, 503)
            self.assertEqual(repeated.exception.detail['code'], 'task_ingress_in_progress')
            self.assertEqual(duplicate.reads, 0)
            extra = Upload()
            with self.assertRaises(BoundaryHTTPException) as caught:
                await fx.create(fx.options('fourth', upload=extra))
            self.assertEqual(caught.exception.status_code, 429)
            self.assertEqual(extra.reads, 0)
            self.assertEqual(fx.manager.task_protocol_v2.admission_status(set())['durable_nonterminal_tasks'], 3)
        finally:
            for upload in uploads:
                upload.release.set()
            parse_release.set()
            tasks = await asyncio.wait_for(asyncio.gather(*creators), 2)
        await until(lambda: all(t.status == 'failed' for t in tasks))
        self.assertEqual(entered.qsize(), 3)
        for task in tasks:
            await fx.module.ack_async_task_result(task.task_id)
        await fx.manager.shutdown()

    async def test_actual_parse_two_and_finalizer_one_overlap_with_original_zip_and_ack(self):
        fx = self.fixture()
        entered, parse_release = self.parser(fx)
        await fx.manager.start()
        final_entered, final_release = threading.Event(), threading.Event()
        self.addCleanup(final_release.set)
        original = fx.module._write_retained_zip_from_fds
        active = 0
        peak = 0
        lock = threading.Lock()
        def zip_boundary(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                final_entered.set()
                if not final_release.wait(2):
                    raise AssertionError('independent finalizer barrier not released')
                return original(*args)
            finally:
                with lock:
                    active -= 1
        with patch.object(fx.module, '_write_retained_zip_from_fds', zip_boundary):
            tasks = [await fx.create(fx.options('nf-'+str(i))) for i in range(3)]
            try:
                await until(lambda: entered.qsize() == 2
                            and fx.manager.task_protocol_executor.stage_snapshot()["parse_waiting"] == 1)
                self.assertEqual(fx.manager.task_protocol_executor.stage_snapshot(),
                    {**EMPTY_STAGES, 'parse_active':2, 'parse_waiting':1})
                self.assertEqual(fx.manager.task_protocol_v2.reserved_result_bytes, BUDGET*3)
                parse_release.set()
                await until(lambda: final_entered.is_set() and fx.manager.task_protocol_executor.stage_snapshot()['finalizer_waiting'] == 2)
                stages = (await fx.module.health_check())['capacity_observation']['stage_counters']
                self.assertEqual(stages, {**EMPTY_STAGES, 'finalizer_active':1, 'finalizer_waiting':2})
                self.assertEqual(entered.qsize(), 3)
            finally:
                parse_release.set()
                final_release.set()
            await self.completed(fx, tasks)
        self.assertEqual(peak, 1)
        await fx.manager.shutdown()

    async def test_result_budget_wait_is_before_parse_slot_and_real_ack_wakes_original_key(self):
        fx = self.fixture(limit=BUDGET)
        entered, release = self.parser(fx, fail=True)
        await fx.manager.start()
        tasks = [await fx.create(fx.options('budget-'+str(i))) for i in range(3)]
        await until(lambda: entered.qsize() == 1 and fx.manager.task_protocol_executor.stage_snapshot()['result_capacity_waiting'] == 2)
        observed = (await fx.module.health_check())['capacity_observation']
        self.assertEqual(observed['stage_counters'], {**EMPTY_STAGES, 'parse_active':1, 'result_capacity_waiting':2})
        self.assertEqual(fx.manager.task_protocol_executor._parse._value, 1)
        self.assertEqual(fx.manager.task_protocol_executor._finalize._value, 1)
        first = entered.get_nowait()
        for task in tasks:
            if task is not first:
                record = fx.manager.task_protocol_v2.get(task.agent_idempotency_key)
                self.assertEqual((record.state, record.reserved_result_bytes), ('pending', 0))
        release.set()
        await until(lambda: first.status == 'failed')
        self.assertEqual(fx.manager.task_protocol_v2.reserved_result_bytes, BUDGET)
        self.assertEqual(entered.qsize(), 0)
        await fx.module.ack_async_task_result(first.task_id)
        for _ in range(2):
            current = await asyncio.wait_for(entered.get(), 2)
            await until(lambda: current.status == 'failed')
            await fx.module.ack_async_task_result(current.task_id)
        self.assertTrue(all(fx.manager.task_protocol_v2.get(t.agent_idempotency_key).state == 'consumed' for t in tasks))
        self.assertIsNone(fx.manager.last_worker_error)
        await fx.manager.shutdown()

    async def test_lazy_http_owner_becomes_one_real_shared_seven_credit_limiter(self):
        fx = self.fixture()
        await fx.manager.start()
        before = (await fx.module.health_check())['capacity_observation']
        self.assertIsNone(before['resolved_limits']['final_http_limit_per_loop'])
        release = asyncio.Event()
        entered = asyncio.Queue()
        async def hold(number):
            limiter = fx.http._process_async_request_limiter(7)
            async with limiter:
                entered.put_nowait((number, limiter))
                await release.wait()
        tasks = [asyncio.create_task(hold(i)) for i in range(8)]
        try:
            await until(lambda: entered.qsize() == 7)
            actual = (await fx.module.health_check())['capacity_observation']
            self.assertEqual(actual['http_limiter_state'], 'initialized')
            self.assertEqual(actual['resolved_limits']['final_http_limit_per_loop'], 7)
            self.assertEqual(actual['http_counters'], {'active_requests':7,'pending_requests':1})
            first = entered.get_nowait()[1]
            self.assertTrue(all(entered.get_nowait()[1] is first for _ in range(6)))
            self.assertEqual(first.peak, 7)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
        self.assertEqual(fx.http._process_async_request_snapshot()['active_requests'], 0)
        self.assertEqual(fx.http._process_async_request_snapshot()['pending_requests'], 0)
        await fx.manager.shutdown()

    async def test_bad_cli_http_limit_rejects_before_recovery_dispatch_or_observer_birth(self):
        fx = self.fixture(cli_h=8)
        registry = fx.manager.task_protocol_v2
        with (patch.object(registry, 'cleanup_consumed', wraps=registry.cleanup_consumed) as cleanup,
              patch.object(registry, 'recoverable_payloads', wraps=registry.recoverable_payloads) as replay,
              patch.object(fx.observation, 'CapacityServingObservation', wraps=fx.observation.CapacityServingObservation) as observation):
            with self.assertRaisesRegex(ValueError, 'HTTP concurrency'):
                await fx.manager.start()
        cleanup.assert_not_called()
        replay.assert_not_called()
        observation.assert_not_called()
        self.assertIsNone(fx.manager.dispatcher_task)
        self.assertIsNone(fx.manager.cleanup_task)
        self.assertEqual(fx.manager.active_tasks, set())
        self.assertIsNone(fx.http._CAPACITY_OWNER)
        self.assertEqual(fx.http._PROCESS_ASYNC_REQUEST_LIMITERS, {})

    async def test_foreign_http_loop_applies_soft_drain_without_hard_failing_accepted_work(self):
        fx = self.fixture()
        entered, release = self.parser(fx)
        await fx.manager.start()
        tasks = [await fx.create(fx.options('foreign-'+str(i))) for i in range(3)]
        await until(lambda: entered.qsize() == 2)
        async def foreign():
            with self.assertRaisesRegex(RuntimeError, 'outside the capacity serving loop'):
                fx.http._process_async_request_limiter(7)
        await asyncio.wait_for(asyncio.to_thread(lambda: asyncio.run(foreign())), 2)
        await until(lambda: fx.manager.is_shutting_down)
        observation = fx.manager.capacity_observer.snapshot()
        self.assertEqual(observation['owner_control'], {'foreign_loop_observed':True,
            'soft_drain_requested':True, 'soft_drain_applied':True, 'trigger':'foreign_event_loop'})
        self.assertIsNone(fx.manager.last_worker_error)
        self.assertFalse(fx.manager.task_protocol_executor._abort_pending)
        extra = Upload()
        with self.assertRaises(BoundaryHTTPException) as caught:
            await fx.create(fx.options('after-drain', upload=extra))
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(extra.reads, 0)
        stopping = asyncio.create_task(fx.manager.shutdown())
        try:
            await asyncio.sleep(0)
            self.assertFalse(stopping.done())
            release.set()
            await asyncio.wait_for(stopping, 2)
        finally:
            release.set()
        self.assertEqual(entered.qsize(), 3)
        self.assertIsNone(fx.manager.last_worker_error)
        await self.completed(fx, tasks)
