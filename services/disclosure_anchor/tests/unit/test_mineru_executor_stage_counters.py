"""Actual local executor waits/owners, independently observed with finite barriers."""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol
from tests._mineru_result_reservation_fixture import ReservationLab


ZERO = {
    "result_capacity_waiting": 0,
    "parse_waiting": 0,
    "parse_active": 0,
    "finalizer_waiting": 0,
    "finalizer_active": 0,
}


class MineruExecutorStageCounterTests(unittest.IsolatedAsyncioTestCase):
    def lab(self, limit=100):
        temporary = tempfile.TemporaryDirectory(prefix="executor-stage-counter-")
        self.addCleanup(temporary.cleanup)
        return ReservationLab(Path(temporary.name), limit=limit)

    def executor(self, parse_slots=2):
        return protocol.SplitTaskExecutor(
            parse_slots=parse_slots, finalizer_slots=1, result_reservation_bytes=31
        )

    def snapshot(self, executor, **counts):
        value = executor.stage_snapshot()
        self.assertEqual(value, {**ZERO, **counts})
        self.assertTrue(
            all(type(count) is int and count >= 0 for count in value.values())
        )
        return value

    def own(self, coroutine):
        task = asyncio.create_task(coroutine)

        async def cleanup():
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)

        self.addAsyncCleanup(cleanup)
        return task

    async def wait(self, event):
        await asyncio.wait_for(event.wait(), 2)

    def result(self, lab, key):
        result = lab.result(key, 1)
        return (
            result["result_path"],
            result["result_sha256"],
            result["result_bytes"],
            result["result_owner"],
        )

    def test_slot_properties_are_readonly_and_snapshot_ignores_unowned_registry_states(
        self,
    ):
        executor = self.executor()
        self.assertEqual((executor.parse_slots, executor.finalizer_slots), (2, 1))
        for field in ("parse_slots", "finalizer_slots"):
            with self.subTest(field=field), self.assertRaises(AttributeError):
                setattr(executor, field, 99)
        first = self.snapshot(executor)
        first["parse_active"] = 999
        first["invented"] = 1
        self.snapshot(executor)
        lab = self.lab()
        for key, state in (
            ("pending", "pending"),
            ("parsing", "processing"),
            ("final", "finalizing"),
        ):
            lab.pending(key)
            if state != "pending":
                lab.registry.transition(key, "processing")
            if state == "finalizing":
                lab.registry.transition(key, "finalizing")
        self.snapshot(executor)
        self.assertEqual(len(lab.rows()), 3)

    async def test_two_parse_owners_three_responsibilities_and_one_finalizer_have_distinct_counts(
        self,
    ):
        lab = self.lab()
        keys = ("first", "second", "third")
        for key in keys:
            lab.pending(key)
        executor = self.executor()
        parse_entered = {key: asyncio.Event() for key in keys}
        parse_release = {key: asyncio.Event() for key in keys}
        final_entered = {key: asyncio.Event() for key in keys}
        final_release = {key: asyncio.Event() for key in keys}
        finalizing = {key: asyncio.Event() for key in keys}
        third_reserved = asyncio.Event()
        original_reserve = lab.registry.reserve_result_for_parse
        original_transition = lab.registry.transition

        def reserve(key, **kwargs):
            result = original_reserve(key, **kwargs)
            if key == "third":
                third_reserved.set()
            return result

        def transition(key, state, **kwargs):
            result = original_transition(key, state, **kwargs)
            if state == "finalizing":
                finalizing[key].set()
            return result

        def callbacks(key):
            async def parse():
                self.assertEqual(lab.registry.get(key).reserved_result_bytes, 31)
                parse_entered[key].set()
                await parse_release[key].wait()

            async def finalize():
                final_entered[key].set()
                await final_release[key].wait()
                return self.result(lab, key)

            return parse, finalize

        with (
            patch.object(lab.registry, "reserve_result_for_parse", side_effect=reserve),
            patch.object(lab.registry, "transition", side_effect=transition),
        ):
            tasks = {}
            for key in keys:
                parse, finalize = callbacks(key)
                tasks[key] = self.own(
                    executor.run(
                        registry=lab.registry, key=key, parse=parse, finalize=finalize
                    )
                )
                await self.wait(
                    third_reserved if key == "third" else parse_entered[key]
                )
            self.snapshot(executor, parse_active=2, parse_waiting=1)
            self.assertFalse(parse_entered["third"].is_set())
            self.assertEqual(lab.registry.reserved_result_bytes, 93)
            parse_release["first"].set()
            await self.wait(final_entered["first"])
            await self.wait(parse_entered["third"])
            self.snapshot(executor, parse_active=2, finalizer_active=1)
            parse_release["second"].set()
            await self.wait(finalizing["second"])
            self.snapshot(
                executor, parse_active=1, finalizer_active=1, finalizer_waiting=1
            )
            parse_release["third"].set()
            await self.wait(finalizing["third"])
            self.snapshot(executor, finalizer_active=1, finalizer_waiting=2)
            self.assertFalse(final_entered["second"].is_set())
            self.assertFalse(final_entered["third"].is_set())
            final_release["first"].set()
            await self.wait(final_entered["second"])
            self.snapshot(executor, finalizer_active=1, finalizer_waiting=1)
            final_release["second"].set()
            await self.wait(final_entered["third"])
            self.snapshot(executor, finalizer_active=1)
            final_release["third"].set()
            await asyncio.wait_for(asyncio.gather(*tasks.values()), 2)
        self.snapshot(executor)
        self.assertEqual(
            [lab.registry.get(key).state for key in keys], ["completed"] * 3
        )
        self.assertEqual(
            (lab.registry.unacked_result_bytes, lab.registry.reserved_result_bytes),
            (3, 0),
        )

    async def test_real_full_wait_counts_only_wait_and_clears_after_cancel_stop_or_ack(
        self,
    ):
        for ending in ("cancel", "soft_stop", "ack"):
            with self.subTest(ending=ending):
                lab = self.lab(limit=73)
                lab.completed("old", budget=73, size=65)
                lab.pending("waiting")
                executor = self.executor()
                full = (asyncio.Event(), asyncio.Event())
                attempts = []
                calls = []
                original = lab.registry.reserve_result_for_parse

                def reserve(*args, **kwargs):
                    try:
                        return original(*args, **kwargs)
                    except protocol.TaskResultCapacityFull:
                        attempts.append("full")
                        if len(attempts) <= 2:
                            full[len(attempts) - 1].set()
                        raise

                async def parse():
                    self.snapshot(executor, parse_active=1)
                    calls.append("parse")

                async def finalize():
                    self.snapshot(executor, finalizer_active=1)
                    return self.result(lab, "waiting")

                with patch.object(
                    lab.registry, "reserve_result_for_parse", side_effect=reserve
                ):
                    task = self.own(
                        executor.run(
                            registry=lab.registry,
                            key="waiting",
                            parse=parse,
                            finalize=finalize,
                        )
                    )
                    await self.wait(full[0])
                    self.snapshot(executor, result_capacity_waiting=1)
                    self.assertEqual(
                        lab.registry.get("waiting").reserved_result_bytes, 0
                    )
                    executor.notify_result_capacity_changed()
                    await self.wait(full[1])
                    self.snapshot(executor, result_capacity_waiting=1)
                    self.assertEqual(calls, [])
                    if ending == "cancel":
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 2)
                    elif ending == "soft_stop":
                        executor.begin_shutdown()
                        with self.assertRaises(protocol.TaskExecutionStopped):
                            await asyncio.wait_for(task, 2)
                    else:
                        lab.registry.acknowledge("old")
                        self.assertEqual(lab.registry.cleanup_consumed(), 1)
                        executor.notify_result_capacity_changed()
                        await asyncio.wait_for(task, 2)
                self.snapshot(executor)
                self.assertEqual(calls, ["parse"] if ending == "ack" else [])
                self.assertEqual(
                    lab.registry.get("waiting").state,
                    "completed" if ending == "ack" else "pending",
                )

    async def test_actual_task_cancellation_in_each_semaphore_wait_or_active_stage_releases_observation(
        self,
    ):
        for stage in (
            "parse_waiting",
            "parse_active",
            "finalizer_waiting",
            "finalizer_active",
        ):
            with self.subTest(stage=stage):
                lab = self.lab()
                executor = self.executor(parse_slots=1)
                lab.pending("target")
                holder_entered, holder_release = asyncio.Event(), asyncio.Event()
                target_entered, target_release = asyncio.Event(), asyncio.Event()
                target_reserved, target_finalizing = asyncio.Event(), asyncio.Event()
                holder = None
                original_reserve = lab.registry.reserve_result_for_parse
                original_transition = lab.registry.transition

                def reserve(key, **kwargs):
                    result = original_reserve(key, **kwargs)
                    if key == "target":
                        target_reserved.set()
                    return result

                def transition(key, state, **kwargs):
                    result = original_transition(key, state, **kwargs)
                    if key == "target" and state == "finalizing":
                        target_finalizing.set()
                    return result

                async def holder_parse():
                    if stage == "parse_waiting":
                        holder_entered.set()
                        await holder_release.wait()

                async def holder_finalize():
                    if stage == "finalizer_waiting":
                        holder_entered.set()
                        await holder_release.wait()
                    return self.result(lab, "holder")

                async def target_parse():
                    if stage == "parse_active":
                        target_entered.set()
                        await target_release.wait()

                async def target_finalize():
                    if stage == "finalizer_active":
                        target_entered.set()
                        await target_release.wait()
                    return self.result(lab, "target")

                with (
                    patch.object(
                        lab.registry, "reserve_result_for_parse", side_effect=reserve
                    ),
                    patch.object(lab.registry, "transition", side_effect=transition),
                ):
                    if stage.endswith("waiting"):
                        lab.pending("holder")
                        holder = self.own(
                            executor.run(
                                registry=lab.registry,
                                key="holder",
                                parse=holder_parse,
                                finalize=holder_finalize,
                            )
                        )
                        await self.wait(holder_entered)
                    target = self.own(
                        executor.run(
                            registry=lab.registry,
                            key="target",
                            parse=target_parse,
                            finalize=target_finalize,
                        )
                    )
                    ready = (
                        target_reserved
                        if stage == "parse_waiting"
                        else target_finalizing
                        if stage == "finalizer_waiting"
                        else target_entered
                    )
                    await self.wait(ready)
                    expected = {stage: 1}
                    if stage.endswith("waiting"):
                        expected[stage.replace("waiting", "active")] = 1
                    self.snapshot(executor, **expected)
                    target.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(target, 2)
                    remaining = (
                        {stage.replace("waiting", "active"): 1} if holder else {}
                    )
                    self.snapshot(executor, **remaining)
                    row = lab.registry.get("target")
                    self.assertEqual(
                        row.state, "pending" if stage == "parse_waiting" else "failed"
                    )
                    self.assertEqual(row.reserved_result_bytes, 31)
                    if holder:
                        holder_release.set()
                        await asyncio.wait_for(holder, 2)
                self.snapshot(executor)

    async def test_original_callback_error_releases_active_counter_without_erasing_result_responsibility(
        self,
    ):
        for stage in ("parse", "finalizer"):
            with self.subTest(stage=stage):
                lab = self.lab()
                lab.pending("failed")
                executor = self.executor()
                error = RuntimeError("independent-" + stage)

                async def parse():
                    self.snapshot(executor, parse_active=1)
                    if stage == "parse":
                        raise error

                async def finalize():
                    self.snapshot(executor, finalizer_active=1)
                    raise error

                with self.assertRaises(RuntimeError) as caught:
                    await asyncio.wait_for(
                        executor.run(
                            registry=lab.registry,
                            key="failed",
                            parse=parse,
                            finalize=finalize,
                        ),
                        2,
                    )
                self.assertIs(caught.exception, error)
                self.snapshot(executor)
                row = lab.registry.get("failed")
                self.assertEqual((row.state, row.reserved_result_bytes), ("failed", 31))
