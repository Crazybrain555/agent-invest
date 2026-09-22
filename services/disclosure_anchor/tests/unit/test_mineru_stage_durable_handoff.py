"""Independent stage/durable-state handoff boundaries under the real asynchronous service IO.

The adjacent stage-counter tests drive the executor's synchronous
``registry_io=None`` fallback, where a transition commits inside one awaitless
call and no observer can run between the slot and its durable state. These cases
cover the combination that fallback cannot reach: a real ``RegistryServiceIO``
commit in flight while the real generated ``/health`` route publishes the last
durable view, judged by the shared closed validator the Mac-side collector
applies to the wire.

No PDF is parsed and no model, GPU, network, database or MinerU installation is
involved; uploads are synthetic bytes under a per-test temporary directory.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from disclosure_anchor.application.contracts.mineru_capacity_config import (
    decode_mineru_capacity_config,
)
from disclosure_anchor.application.contracts.mineru_capacity_health import (
    parse_mineru_capacity_wire_health,
)
from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol
from tests import _m6_registry_lab as faults
from tests._mineru_capacity_lifecycle_fixture import CapacityLifecycleFixture
from tests._mineru_stage_handoff_fixture import (
    RESERVATION,
    ZERO,
    DurableCommitHold,
    StageExecutorHarness,
    durable_state,
)

# The lifecycle fixture's own startup identity, asserted below so the wire
# comparison stays bound to observed startup values rather than a guess.
RETENTION_SECONDS = 0
CLEANUP_INTERVAL_SECONDS = 300
ARTIFACT = b"synthetic parser-boundary artifact; no PDF was parsed\n"


class StageHandoffCase(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    async def until(self, predicate, timeout: float = 5.0, what: str = "") -> None:
        async def waiting() -> None:
            while not predicate():
                await asyncio.sleep(0.001)

        try:
            await asyncio.wait_for(waiting(), timeout)
        except asyncio.TimeoutError:
            self.fail("barrier never reached: " + (what or "predicate"))

    def temporary(self, prefix: str) -> Path:
        directory = tempfile.TemporaryDirectory(prefix=prefix)
        self.addCleanup(directory.cleanup)
        return Path(directory.name).resolve()


class StageWireHealthTests(StageHandoffCase):
    """The real generated route and the unchanged closed capacity validator."""

    def fixture(self, **kwargs) -> CapacityLifecycleFixture:
        fixture = CapacityLifecycleFixture(self.temporary("stage-wire-"), **kwargs)
        self.addCleanup(fixture.close)
        self.addAsyncCleanup(fixture.dispose_test_tasks)
        return fixture

    def parse_boundary(self, fixture, *, failing: frozenset[str] = frozenset()):
        entered: dict[str, asyncio.Event] = {}
        release: dict[str, asyncio.Event] = {}
        started: list[str] = []

        def event(store: dict[str, asyncio.Event], key: str) -> asyncio.Event:
            return store.setdefault(key, asyncio.Event())

        async def boundary(**kwargs):
            options = kwargs["request_options"]
            key = options.agent_idempotency_key
            started.append(key)
            event(entered, key).set()
            await event(release, key).wait()
            if key in failing:
                raise RuntimeError("synthetic parser boundary failure; no PDF was parsed")
            directory = Path(
                fixture.module.get_parse_dir(
                    options.output_dir, "paper", options.backend, options.parse_method
                )
            )
            directory.mkdir(parents=True)
            (directory / "paper.md").write_bytes(ARTIFACT)

        fixture.module.run_parse_job = boundary
        self.addCleanup(lambda: [item.set() for item in release.values()])
        return lambda key: event(entered, key), lambda key: event(release, key), started

    async def wire_health(self, fixture) -> dict:
        """Return the real /health response once the closed validator accepts it."""
        health = await asyncio.wait_for(fixture.module.health_check(), 5)
        self.assertEqual(health["task_retention_seconds"], RETENTION_SECONDS)
        self.assertEqual(health["task_cleanup_interval_seconds"], CLEANUP_INTERVAL_SECONDS)
        payload = json.dumps(health, sort_keys=True, separators=(",", ":")).encode()
        capacity = decode_mineru_capacity_config(fixture.config_path.read_bytes())
        try:
            parse_mineru_capacity_wire_health(
                payload,
                expected_capacity=capacity,
                expected_task_retention_seconds=RETENTION_SECONDS,
                expected_cleanup_interval_seconds=CLEANUP_INTERVAL_SECONDS,
            )
        except ValueError as exc:
            self.fail(
                "closed capacity-health validation rejected the real /health response: "
                + str(exc)
                + " | stage_counters="
                + json.dumps(health["capacity_observation"]["stage_counters"], sort_keys=True)
                + " | task_admission="
                + json.dumps(
                    {
                        name: health["task_admission"][name]
                        for name in (
                            "accepted_pending_tasks",
                            "accepted_processing_tasks",
                            "accepted_finalizing_tasks",
                            "durable_nonterminal_tasks",
                        )
                    },
                    sort_keys=True,
                )
            )
        return health

    async def test_health_published_while_the_processing_commit_is_in_flight(self):
        fixture = self.fixture()
        _entered, release, started = self.parse_boundary(fixture)
        registry = fixture.manager.task_protocol_v2
        await fixture.manager.start()
        await self.wire_health(fixture)
        with DurableCommitHold(registry, target="processing") as hold:
            hold.arm()
            task = await fixture.create(fixture.options("held"))
            key = task.agent_idempotency_key
            await self.until(hold.entered.is_set, what="processing commit held")
            # The commit is in flight: the published durable view still reads
            # pending and the parser has not been handed the task.
            self.assertEqual(durable_state(registry, key), "pending")
            self.assertEqual(started, [])
            health = await self.wire_health(fixture)
            self.assertEqual(
                health["capacity_observation"]["stage_counters"]["parse_active"], 0
            )
            self.assertEqual(health["task_admission"]["accepted_processing_tasks"], 0)
            self.assertEqual(started, [])
            hold.release.set()
            await self.until(lambda: started == [key], what="parser entered")
            # Only a committed transition admits the parser and the counter.
            self.assertEqual(durable_state(registry, key), "processing")
            health = await self.wire_health(fixture)
            self.assertEqual(
                health["capacity_observation"]["stage_counters"]["parse_active"], 1
            )
            release(key).set()
            await self.until(lambda: task.status == "completed", what="task completed")
        self.assertEqual(fixture.manager.task_protocol_executor.stage_snapshot(), ZERO)
        await self.wire_health(fixture)
        await fixture.module.ack_async_task_result(task.task_id)
        await fixture.manager.shutdown()

    async def test_health_during_a_held_commit_beside_a_durably_active_parse(self):
        fixture = self.fixture()
        entered, release, started = self.parse_boundary(fixture)
        registry = fixture.manager.task_protocol_v2
        await fixture.manager.start()
        running = await fixture.create(fixture.options("running"))
        running_key = running.agent_idempotency_key
        await asyncio.wait_for(entered(running_key).wait(), 5)
        self.assertEqual(registry.get(running_key).state, "processing")
        with DurableCommitHold(registry, target="processing") as hold:
            hold.arm()
            held = await fixture.create(fixture.options("held-beside"))
            held_key = held.agent_idempotency_key
            await self.until(hold.entered.is_set, what="second commit held")
            self.assertEqual(hold.held_key, held_key)
            self.assertEqual(durable_state(registry, held_key), "pending")
            self.assertEqual(started, [running_key])
            health = await self.wire_health(fixture)
            # One durably committed parse owner, one slot held across an
            # in-flight commit: only the committed owner may be counted, and the
            # uncounted holder is still visible as durable responsibility.
            self.assertEqual(
                health["capacity_observation"]["stage_counters"],
                {**ZERO, "parse_active": 1},
            )
            self.assertEqual(health["task_admission"]["accepted_processing_tasks"], 1)
            self.assertEqual(health["task_admission"]["accepted_pending_tasks"], 1)
            self.assertEqual(health["task_admission"]["durable_nonterminal_tasks"], 2)
            hold.release.set()
            await self.until(
                lambda: sorted(started) == sorted([running_key, held_key]),
                what="both parsers entered",
            )
            health = await self.wire_health(fixture)
            self.assertEqual(
                health["capacity_observation"]["stage_counters"],
                {**ZERO, "parse_active": 2},
            )
            for key in (running_key, held_key):
                release(key).set()
            await self.until(
                lambda: running.status == "completed" and held.status == "completed",
                what="both tasks completed",
            )
        self.assertEqual(fixture.manager.task_protocol_executor.stage_snapshot(), ZERO)
        await self.wire_health(fixture)
        for finished in (running, held):
            await fixture.module.ack_async_task_result(finished.task_id)
        await fixture.manager.shutdown()

    async def test_health_across_the_finalizing_commit_and_the_finalizer_slot(self):
        fixture = self.fixture()
        entered, release, _started = self.parse_boundary(fixture)
        registry = fixture.manager.task_protocol_v2
        await fixture.manager.start()
        task = await fixture.create(fixture.options("final"))
        key = task.agent_idempotency_key
        await asyncio.wait_for(entered(key).wait(), 5)
        zipped, proceed = threading.Event(), threading.Event()
        self.addCleanup(proceed.set)
        original_zip = fixture.module._write_retained_zip_from_fds

        def zip_boundary(*args):
            zipped.set()
            if not proceed.wait(20):
                raise AssertionError("finalizer barrier never released")
            return original_zip(*args)

        with (
            DurableCommitHold(registry, target="finalizing") as hold,
            patch.object(fixture.module, "_write_retained_zip_from_fds", zip_boundary),
        ):
            hold.arm(key)
            release(key).set()
            await self.until(hold.entered.is_set, what="finalizing commit held")
            # The parse slot is released and the finalizer slot is not entered
            # yet, so the task owns no stage while its commit is in flight.
            self.assertEqual(fixture.manager.task_protocol_executor.stage_snapshot(), ZERO)
            self.assertEqual(durable_state(registry, key), "processing")
            await self.wire_health(fixture)
            hold.release.set()
            await self.until(zipped.is_set, what="finalizer entered")
            self.assertEqual(registry.get(key).state, "finalizing")
            health = await self.wire_health(fixture)
            self.assertEqual(
                health["capacity_observation"]["stage_counters"],
                {**ZERO, "finalizer_active": 1},
            )
            self.assertEqual(health["task_admission"]["accepted_finalizing_tasks"], 1)
            proceed.set()
            await self.until(lambda: task.status == "completed", what="task completed")
        self.assertEqual(fixture.manager.task_protocol_executor.stage_snapshot(), ZERO)
        await self.wire_health(fixture)
        await fixture.module.ack_async_task_result(task.task_id)
        await fixture.manager.shutdown()

    async def test_health_stays_valid_when_one_of_two_parse_owners_fails(self):
        fixture = self.fixture()
        bad_key = fixture.options("failing").agent_idempotency_key
        entered, release, started = self.parse_boundary(
            fixture, failing=frozenset({bad_key})
        )
        registry = fixture.manager.task_protocol_v2
        await fixture.manager.start()
        good = await fixture.create(fixture.options("survivor"))
        bad = await fixture.create(fixture.options("failing"))
        good_key = good.agent_idempotency_key
        self.assertEqual(bad.agent_idempotency_key, bad_key)
        await asyncio.wait_for(entered(bad_key).wait(), 5)
        await asyncio.wait_for(entered(good_key).wait(), 5)
        health = await self.wire_health(fixture)
        self.assertEqual(
            health["capacity_observation"]["stage_counters"], {**ZERO, "parse_active": 2}
        )
        release(bad_key).set()
        await self.until(lambda: bad.status == "failed", what="failing task settled")
        self.assertEqual(registry.get(bad_key).state, "failed")
        health = await self.wire_health(fixture)
        self.assertEqual(
            health["capacity_observation"]["stage_counters"], {**ZERO, "parse_active": 1}
        )
        release(good_key).set()
        await self.until(lambda: good.status == "completed", what="survivor completed")
        self.assertEqual(registry.get(good_key).state, "completed")
        self.assertEqual(sorted(started), sorted([good_key, bad_key]))
        self.assertEqual(fixture.manager.task_protocol_executor.stage_snapshot(), ZERO)
        await self.wire_health(fixture)
        for finished in (good, bad):
            await fixture.module.ack_async_task_result(finished.task_id)
        await fixture.manager.shutdown()


class StageExecutorBoundaryTests(StageHandoffCase):
    """Permit ownership and failure release on the path the handoff rewrote."""

    def harness(self, *, parse_slots: int = 2) -> StageExecutorHarness:
        harness = StageExecutorHarness(
            self.temporary("stage-executor-"), parse_slots=parse_slots
        )
        self.addAsyncCleanup(harness.close)
        return harness

    def own(self, task: asyncio.Task) -> asyncio.Task:
        async def cleanup() -> None:
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)

        self.addAsyncCleanup(cleanup)
        return task

    def assert_closed_clean(self, harness: StageExecutorHarness) -> None:
        """No counter and no permit may leak, and none may be released twice."""
        self.assertEqual(harness.stages(), ZERO)
        self.assertEqual(harness.free_parse_permits(), harness.parse_slots)
        self.assertEqual(harness.free_finalizer_permits(), harness.finalizer_slots)

    async def assert_not_starved(self, harness: StageExecutorHarness, prefix: str) -> None:
        """Exactly parse_slots fresh tasks may parse at once, and every one finishes."""
        keys = [f"{prefix}-{index}" for index in range(harness.parse_slots + 1)]
        tasks = [self.own(harness.run(key)) for key in keys]
        def entered() -> int:
            return sum(harness.event(harness.entered, key).is_set() for key in keys)

        await self.until(
            lambda: entered() == harness.parse_slots, what="fresh tasks reached the parser"
        )
        await asyncio.sleep(0.05)
        self.assertEqual(
            entered(),
            harness.parse_slots,
            "a permit released twice would admit more than the configured parse owners",
        )
        harness.release_all()
        await asyncio.wait_for(asyncio.gather(*tasks), 20)
        self.assertEqual(
            [harness.registry.get(key).state for key in keys], ["completed"] * len(keys)
        )
        self.assert_closed_clean(harness)

    async def test_a_pre_commit_holder_keeps_its_permit_and_the_next_task_queued(self):
        harness = self.harness(parse_slots=2)
        keys = ("alpha", "beta", "gamma", "delta")
        tasks = {key: self.own(harness.run(key, blocking_finalize=True)) for key in keys}
        await self.until(
            lambda: len(harness.parsed) == 2 and harness.stages()["parse_waiting"] == 2,
            what="two parse owners and two queued tasks",
        )
        # Every task is durably accepted and holds its reservation, so the queued
        # pair waits on the parse semaphore and nothing else.
        self.assertEqual(harness.registry.reserved_result_bytes, RESERVATION * 4)
        owners = sorted(harness.parsed)
        queued = sorted(set(keys) - set(owners))
        with DurableCommitHold(harness.registry, target="processing") as hold:
            hold.arm()
            released = owners[0]
            harness.event(harness.release, released).set()
            await self.until(hold.entered.is_set, what="a queued task's commit held")
            await self.until(
                lambda: harness.event(harness.final_entered, released).is_set(),
                what="the released owner reached its finalizer",
            )
            await asyncio.sleep(0.05)
            promoted = hold.held_key
            self.assertIn(promoted, queued)
            self.assertEqual(sorted(harness.parsed), owners)
            for key in queued:
                self.assertEqual(durable_state(harness.registry, key), "pending")
            self.assertEqual(harness.stages()["parse_waiting"], 1)
            self.assertEqual(harness.stages()["finalizer_active"], 1)
            self.assertEqual(harness.free_parse_permits(), 0)
            hold.release.set()
            harness.release_all()
            await asyncio.wait_for(asyncio.gather(*tasks.values()), 20)
        self.assertEqual(sorted(harness.parsed), sorted(keys))
        self.assertEqual(
            [harness.registry.get(key).state for key in keys], ["completed"] * 4
        )
        self.assert_closed_clean(harness)

    async def test_every_parse_permit_can_be_held_by_an_uncounted_pre_commit_task(self):
        """One metadata write in flight is not one pre-commit holder.

        The metadata lane serializes commits, but a task that is already queued
        for that lane owns its parse permit, so every permit can be held by an
        uncounted task while only one write is in flight.
        """
        harness = self.harness(parse_slots=2)
        blockers = ("blocker-a", "blocker-b")
        candidates = ("candidate-a", "candidate-b")
        waiter = "queued"
        tasks = {key: self.own(harness.run(key)) for key in blockers}
        await self.until(lambda: len(harness.parsed) == 2, what="both permits occupied")
        tasks.update({key: self.own(harness.run(key)) for key in (*candidates, waiter)})
        await self.until(
            lambda: harness.stages()["parse_waiting"] == 3,
            what="three tasks queued on the parse semaphore",
        )
        self.assertEqual(harness.registry.reserved_result_bytes, RESERVATION * 5)
        with DurableCommitHold(harness.registry, target="processing") as hold:
            hold.arm()
            for key in blockers:
                harness.event(harness.release, key).set()
            await self.until(hold.entered.is_set, what="a promoted commit held")
            # Both candidates left the wait, so both hold a permit; only the
            # third task is still queued behind them.
            await self.until(
                lambda: harness.stages()["parse_waiting"] == 1,
                what="both candidates took a permit",
            )
            await asyncio.sleep(0.05)
            self.assertIn(hold.held_key, candidates)
            self.assertEqual(sorted(harness.parsed), sorted(blockers))
            self.assertEqual(harness.stages()["parse_active"], 0)
            self.assertEqual(harness.free_parse_permits(), 0)
            for key in (*candidates, waiter):
                self.assertEqual(durable_state(harness.registry, key), "pending")
            hold.release.set()
            harness.release_all()
            await asyncio.wait_for(asyncio.gather(*tasks.values()), 20)
        self.assertEqual(sorted(harness.parsed), sorted([*blockers, *candidates, waiter]))
        self.assert_closed_clean(harness)

    async def test_cancelling_a_pre_commit_holder_releases_its_permit_once(self):
        harness = self.harness(parse_slots=1)
        with DurableCommitHold(harness.registry, target="processing") as hold:
            hold.arm("cancelled")
            task = self.own(harness.run("cancelled"))
            await self.until(hold.entered.is_set, what="processing commit held")
            task.cancel()
            await asyncio.sleep(0.05)
            hold.release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 20)
        self.assertEqual(harness.parsed, [])
        record = harness.registry.get("cancelled")
        self.assertIn(record.state, {"processing", "failed"})
        self.assertEqual(record.reserved_result_bytes, RESERVATION)
        self.assert_closed_clean(harness)
        await self.assert_not_starved(harness, "after-cancel")

    async def test_a_refused_processing_transition_keeps_its_own_exception(self):
        harness = self.harness(parse_slots=2)
        refusal = protocol.TaskProtocolConflict("declared durable transition refusal")
        original = harness.registry.transition

        def transition(key: str, target: str) -> None:
            if key == "refused" and target == "processing":
                raise refusal
            return original(key, target)

        with patch.object(harness.registry, "transition", side_effect=transition):
            task = self.own(harness.run("refused"))
            with self.assertRaises(protocol.TaskProtocolConflict) as caught:
                await asyncio.wait_for(task, 20)
        self.assertIs(caught.exception, refusal)
        self.assertEqual(harness.parsed, [])
        record = harness.registry.get("refused")
        self.assertEqual((record.state, record.reserved_result_bytes), ("pending", RESERVATION))
        self.assert_closed_clean(harness)
        await self.assert_not_starved(harness, "after-refusal")

    async def test_a_failed_processing_write_releases_the_permit_and_stays_degraded(self):
        """A real pre-commit storage fault, not a refusal before the write."""
        harness = self.harness(parse_slots=2)
        original = harness.registry.transition

        def transition(key: str, target: str) -> None:
            if key == "unwritten" and target == "processing":
                with faults.pre_commit_fault(harness.registry, "file_fsync"):
                    return original(key, target)
            return original(key, target)

        with patch.object(harness.registry, "transition", side_effect=transition):
            task = self.own(harness.run("unwritten"))
            with self.assertRaises(protocol.TaskRegistryPersistenceError) as caught:
                await asyncio.wait_for(task, 20)
        # The storage fault stays visible through the protocol error.
        self.assertIsInstance(caught.exception.__cause__, faults.SyntheticStorageFault)
        self.assertEqual(harness.parsed, [])
        # The slot is released exactly once and the responsibility is retained.
        self.assert_closed_clean(harness)
        record = harness.registry.get("unwritten")
        self.assertEqual((record.state, record.reserved_result_bytes), ("pending", RESERVATION))
        status = harness.registry.persistence_status()
        self.assertEqual(status["state"], "degraded")
        self.assertEqual(status["recovery_action"], "retry_idempotent_operation")
        self.assertEqual(status["last_event"]["phase"], "file_fsync")
        self.assertEqual(status["last_event"]["operation"], "transition")
        self.assertIs(status["last_event"]["committed"], False)
        # The published projection fails closed, so /health reports the degraded
        # registry instead of a projection built on an uncommitted write.
        with self.assertRaises(protocol.TaskRegistryPersistenceError):
            protocol.DurableTaskRegistry.admission_status_from_view(
                harness.registry.durable_view(), set()
            )


if __name__ == "__main__":
    unittest.main()
