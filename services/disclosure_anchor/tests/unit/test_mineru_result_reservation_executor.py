"""Independent finite executor tests; all callbacks are local synthetic work."""
from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol
from tests._mineru_result_reservation_fixture import ReservationLab


class ResultReservationExecutorTests(unittest.IsolatedAsyncioTestCase):
    def lab(self, limit=73):
        temporary = tempfile.TemporaryDirectory(prefix='independent-r6-executor-')
        self.addCleanup(temporary.cleanup)
        return ReservationLab(Path(temporary.name), limit=limit)

    def executor(self, budget=31):
        return protocol.SplitTaskExecutor(parse_slots=1, finalizer_slots=1, result_reservation_bytes=budget)

    async def owned(self, coroutine):
        task = asyncio.create_task(coroutine)
        async def close():
            if not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(task, 2)
            except (asyncio.CancelledError, protocol.TaskExecutionStopped):
                pass
            except Exception:
                if not task.done():
                    raise
        self.addAsyncCleanup(close)
        return task

    def finalizer(self, lab, key, size=11):
        async def finalize():
            result = lab.result(key, size)
            return (result['result_path'], result['result_sha256'], result['result_bytes'], result['result_owner'])
        return finalize

    def test_budget_is_exact_positive_and_readonly(self):
        for value in (0, -1, True, False, 31.0, '31', None):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                self.executor(value)
        executor = self.executor(31)
        self.assertEqual(executor.result_reservation_bytes, 31)
        with self.assertRaises(AttributeError):
            executor.result_reservation_bytes = 32

    async def test_full_wait_is_pending_without_parse_or_semaphore_then_real_ack_releases(self):
        lab = self.lab(limit=73)
        lab.completed('old', budget=73, size=65)
        lab.pending('waiting')
        executor = self.executor()
        saw_full = asyncio.Event()
        parse_calls = []
        original = lab.registry.reserve_result_for_parse
        def reserve(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except protocol.TaskResultCapacityFull:
                saw_full.set()
                raise
        async def parse():
            parse_calls.append('waiting')
            self.assertEqual(lab.registry.get('waiting').reserved_result_bytes, 31)
        with patch.object(lab.registry, 'reserve_result_for_parse', side_effect=reserve):
            task = await self.owned(executor.run(registry=lab.registry, key='waiting', parse=parse,
                                                finalize=self.finalizer(lab, 'waiting')))
            await asyncio.wait_for(saw_full.wait(), 2)
            self.assertFalse(task.done())
            self.assertEqual(parse_calls, [])
            self.assertEqual(lab.registry.get('waiting').state, 'pending')
            self.assertFalse(executor._parse.locked())
            self.assertFalse(executor._finalize.locked())
            lab.registry.acknowledge('old')
            self.assertEqual(lab.registry.unacked_result_bytes, 65)
            self.assertFalse(task.done())
            self.assertEqual(lab.registry.cleanup_consumed(), 1)
            executor.notify_result_capacity_changed()
            await asyncio.wait_for(task, 2)
        self.assertEqual(parse_calls, ['waiting'])
        self.assertEqual(lab.registry.get('waiting').state, 'completed')
        self.assertEqual((lab.registry.unacked_result_bytes, lab.registry.reserved_result_bytes), (11, 0))

    async def test_pending_parse_slot_wait_already_holds_distinct_result_budget(self):
        lab = self.lab(limit=62)
        lab.pending('first')
        lab.pending('second')
        executor = self.executor()
        first_entered, release_first, second_reserved = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = []
        original = lab.registry.reserve_result_for_parse
        def reserve(key, **kwargs):
            result = original(key, **kwargs)
            if key == 'second':
                second_reserved.set()
            return result
        async def first_parse():
            seen.append('first')
            first_entered.set()
            await release_first.wait()
        async def second_parse():
            seen.append('second')
        with patch.object(lab.registry, 'reserve_result_for_parse', side_effect=reserve):
            first = await self.owned(executor.run(registry=lab.registry, key='first', parse=first_parse,
                                                 finalize=self.finalizer(lab, 'first')))
            await asyncio.wait_for(first_entered.wait(), 2)
            second = await self.owned(executor.run(registry=lab.registry, key='second', parse=second_parse,
                                                  finalize=self.finalizer(lab, 'second')))
            await asyncio.wait_for(second_reserved.wait(), 2)
            self.assertEqual(lab.registry.get('second').state, 'pending')
            self.assertEqual(lab.registry.reserved_result_bytes, 62)
            self.assertEqual(seen, ['first'])
            release_first.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(seen, ['first', 'second'])
        self.assertEqual((lab.registry.unacked_result_bytes, lab.registry.reserved_result_bytes), (22, 0))

    async def test_nonpending_record_cannot_execute_parse_again_even_with_same_reservation(self):
        for state in ('processing', 'finalizing'):
            with self.subTest(state=state):
                lab = self.lab(limit=31)
                lab.pending('old')
                lab.registry.reserve_result_for_parse('old', byte_budget=31)
                lab.registry.transition('old', 'processing')
                if state == 'finalizing':
                    lab.registry.transition('old', state)
                before = lab.path.read_bytes()
                calls = []
                async def parse():
                    calls.append('forbidden')
                with self.assertRaises(protocol.TaskProtocolConflict):
                    await asyncio.wait_for(self.executor().run(registry=lab.registry, key='old', parse=parse,
                                                              finalize=self.finalizer(lab, 'old')), 2)
                self.assertEqual(calls, [])
                self.assertEqual(lab.path.read_bytes(), before)

    async def test_shutdown_releases_only_waiter_and_keeps_original_pending_responsibility(self):
        lab = self.lab(limit=31)
        lab.completed('old', budget=31, size=31)
        lab.pending('waiting')
        executor = self.executor()
        waiting = asyncio.Event()
        original = lab.registry.reserve_result_for_parse
        def reserve(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except protocol.TaskResultCapacityFull:
                waiting.set()
                raise
        calls = []
        async def parse():
            calls.append('parse')
        with patch.object(lab.registry, 'reserve_result_for_parse', side_effect=reserve):
            task = await self.owned(executor.run(registry=lab.registry, key='waiting', parse=parse,
                                                finalize=self.finalizer(lab, 'waiting')))
            await asyncio.wait_for(waiting.wait(), 2)
            before = lab.path.read_bytes()
            executor.begin_shutdown()
            with self.assertRaises(protocol.TaskExecutionStopped):
                await asyncio.wait_for(task, 2)
        self.assertEqual(calls, [])
        self.assertEqual(lab.path.read_bytes(), before)
        self.assertEqual(lab.registry.get('waiting').state, 'pending')
        executor.start()
        lab.registry.acknowledge('old')
        lab.registry.cleanup_consumed()
        executor.notify_result_capacity_changed()
        await asyncio.wait_for(executor.run(registry=lab.registry, key='waiting', parse=parse,
                                           finalize=self.finalizer(lab, 'waiting')), 2)
        self.assertEqual(calls, ['parse'])

    async def test_release_notification_immediately_before_full_return_is_not_lost(self):
        lab = self.lab(limit=31)
        lab.completed('old', budget=31, size=31)
        lab.pending('next')
        executor = self.executor()
        original = lab.registry.reserve_result_for_parse
        crossed = False
        def reserve(*args, **kwargs):
            nonlocal crossed
            try:
                return original(*args, **kwargs)
            except protocol.TaskResultCapacityFull:
                if not crossed:
                    crossed = True
                    lab.registry.acknowledge('old')
                    lab.registry.cleanup_consumed()
                    executor.notify_result_capacity_changed()
                raise
        seen = []
        async def parse():
            seen.append('parse')
        with patch.object(lab.registry, 'reserve_result_for_parse', side_effect=reserve):
            await asyncio.wait_for(executor.run(registry=lab.registry, key='next', parse=parse,
                                               finalize=self.finalizer(lab, 'next')), 2)
        self.assertTrue(crossed)
        self.assertEqual(seen, ['parse'])

    async def test_original_parse_and_finalizer_errors_leave_result_responsibility(self):
        for stage in ('parse', 'finalize'):
            with self.subTest(stage=stage):
                lab = self.lab(limit=31)
                task_root = lab.pending('failed')
                executor = self.executor()
                marker = RuntimeError('original-' + stage)
                async def parse():
                    self.assertEqual(lab.registry.get('failed').reserved_result_bytes, 31)
                    if stage == 'parse':
                        (task_root / 'partial-parse').write_bytes(b'partial')
                        raise marker
                async def finalize():
                    self.assertEqual(executor.result_reservation_bytes, 31)
                    (task_root / '.retained-result.zip.part').write_bytes(b'partial')
                    raise marker
                with self.assertRaises(RuntimeError) as caught:
                    await asyncio.wait_for(executor.run(registry=lab.registry, key='failed', parse=parse,
                                                       finalize=finalize), 2)
                self.assertIs(caught.exception, marker)
                row = lab.registry.get('failed')
                self.assertEqual((row.state, row.reserved_result_bytes), ('failed', 31))
                self.assertTrue(task_root.exists())
                lab.registry.acknowledge_failed('failed')
                self.assertFalse(task_root.exists())
                self.assertEqual(lab.registry.reserved_result_bytes, 0)

    async def test_soft_shutdown_drains_accepted_work_with_free_or_existing_reservation(self):
        for already_reserved in (False, True):
            with self.subTest(already_reserved=already_reserved):
                lab = self.lab(limit=31)
                lab.pending('accepted')
                if already_reserved:
                    lab.registry.reserve_result_for_parse('accepted', byte_budget=31)
                original = lab.registry.get('accepted')
                executor = self.executor()
                seen = []
                async def parse():
                    row = lab.registry.get('accepted')
                    self.assertEqual((row.state, row.reserved_result_bytes), ('processing', 31))
                    self.assertEqual(row.task_id, original.task_id)
                    self.assertEqual(row.task_payload, original.task_payload)
                    seen.append('parse')
                executor.begin_shutdown()
                await asyncio.wait_for(executor.run(registry=lab.registry, key='accepted', parse=parse,
                                                   finalize=self.finalizer(lab, 'accepted')), 2)
                self.assertEqual(seen, ['parse'])
                self.assertEqual(lab.registry.get('accepted').state, 'completed')
                self.assertEqual((lab.registry.unacked_result_bytes, lab.registry.reserved_result_bytes), (11, 0))

    async def test_soft_and_hard_stop_differ_for_already_reserved_parse_slot_waiter(self):
        for hard in (False, True):
            with self.subTest(hard=hard):
                lab = self.lab(limit=62)
                lab.pending('first')
                lab.pending('second')
                executor = self.executor()
                first_entered, release_first, second_reserved = asyncio.Event(), asyncio.Event(), asyncio.Event()
                seen = []
                original = lab.registry.reserve_result_for_parse
                def reserve(key, **kwargs):
                    result = original(key, **kwargs)
                    if key == 'second':
                        second_reserved.set()
                    return result
                async def first_parse():
                    seen.append('first')
                    first_entered.set()
                    await release_first.wait()
                async def second_parse():
                    seen.append('second')
                with patch.object(lab.registry, 'reserve_result_for_parse', side_effect=reserve):
                    first = await self.owned(executor.run(registry=lab.registry, key='first', parse=first_parse,
                                                         finalize=self.finalizer(lab, 'first')))
                    await asyncio.wait_for(first_entered.wait(), 2)
                    second = await self.owned(executor.run(registry=lab.registry, key='second', parse=second_parse,
                                                          finalize=self.finalizer(lab, 'second')))
                    await asyncio.wait_for(second_reserved.wait(), 2)
                    self.assertEqual(lab.registry.get('second').state, 'pending')
                    self.assertEqual(lab.registry.reserved_result_bytes, 62)
                    if hard:
                        executor.begin_shutdown(abort_pending=True)
                    else:
                        executor.begin_shutdown()
                    release_first.set()
                    await asyncio.wait_for(first, 2)
                    if hard:
                        with self.assertRaises(protocol.TaskExecutionStopped):
                            await asyncio.wait_for(second, 2)
                    else:
                        await asyncio.wait_for(second, 2)
                self.assertEqual(seen, ['first'] if hard else ['first', 'second'])
                self.assertEqual(lab.registry.get('first').state, 'completed')
                self.assertEqual(lab.registry.get('second').state, 'pending' if hard else 'completed')
                self.assertEqual((lab.registry.unacked_result_bytes, lab.registry.reserved_result_bytes),
                                 (11, 31) if hard else (22, 0))

    async def test_soft_wake_rechecks_freed_capacity_but_hard_stop_remains_latched(self):
        for hard in (False, True):
            with self.subTest(hard=hard):
                lab = self.lab(limit=31)
                lab.completed('old', budget=31, size=31)
                lab.pending('waiting')
                executor = self.executor()
                full = asyncio.Event()
                seen = []
                original = lab.registry.reserve_result_for_parse
                def reserve(*args, **kwargs):
                    try:
                        return original(*args, **kwargs)
                    except protocol.TaskResultCapacityFull:
                        full.set()
                        raise
                async def parse():
                    seen.append('waiting')
                with patch.object(lab.registry, 'reserve_result_for_parse', side_effect=reserve):
                    waiting = await self.owned(executor.run(registry=lab.registry, key='waiting', parse=parse,
                                                           finalize=self.finalizer(lab, 'waiting')))
                    await asyncio.wait_for(full.wait(), 2)
                    self.assertFalse(executor._parse.locked())
                    self.assertFalse(executor._finalize.locked())
                    if hard:
                        executor.begin_shutdown(abort_pending=True)
                    # A later ordinary shutdown must never erase an earlier hard stop.
                    executor.begin_shutdown()
                    lab.registry.acknowledge('old')
                    self.assertEqual(lab.registry.unacked_result_bytes, 31)
                    self.assertEqual(lab.registry.cleanup_consumed(), 1)
                    executor.notify_result_capacity_changed()
                    if hard:
                        with self.assertRaises(protocol.TaskExecutionStopped):
                            await asyncio.wait_for(waiting, 2)
                        self.assertEqual(seen, [])
                        row = lab.registry.get('waiting')
                        self.assertEqual((row.state, row.reserved_result_bytes), ('pending', 0))
                    else:
                        await asyncio.wait_for(waiting, 2)
                if hard:
                    executor.start()
                    await asyncio.wait_for(executor.run(registry=lab.registry, key='waiting', parse=parse,
                                                       finalize=self.finalizer(lab, 'waiting')), 2)
                self.assertEqual(seen, ['waiting'])
                self.assertEqual((lab.registry.unacked_result_bytes, lab.registry.reserved_result_bytes), (11, 0))


if __name__ == '__main__':
    unittest.main(verbosity=2)
