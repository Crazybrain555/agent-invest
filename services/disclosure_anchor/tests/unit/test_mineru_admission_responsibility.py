"""Independent state/ownership oracles from Pro next-plan R1, not bug-presence tests."""

import asyncio
import errno
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol

from tests._mineru_admission_fixture import (
    UPLOAD_BYTES,
    AdmissionFixture,
    BoundaryHTTPException,
    Upload,
)


class AdmissionResponsibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mineru-admission-test-")
        self.addCleanup(temporary.cleanup)
        self.fx = AdmissionFixture(Path(temporary.name))
        self.addCleanup(self.fx.close)
        self.addAsyncCleanup(self.fx.dispose_test_tasks)

    async def test_accepted_same_key_is_one_route_and_wrong_fence_cannot_rebind(self):
        request = self.fx.options("first")
        task = await self.fx.create(request)
        files = self.fx.files()
        duplicate = self.fx.options("first")
        duplicate.agent_idempotency_key = request.agent_idempotency_key
        replay = await self.fx.create(duplicate)
        self.assertIs(replay, task)
        self.assertEqual(self.fx.manager.queue.qsize(), 1)
        self.assertEqual(len(self.fx.manager.tasks), 1)
        self.assertEqual(self.fx.files(), files)
        self.assertIn(UPLOAD_BYTES, files.values())
        self.assertEqual(duplicate.files[0].reads, 0)
        duplicate.agent_fence_identity = "wrong-fence"
        with self.assertRaises(BoundaryHTTPException) as caught:
            await self.fx.create(duplicate)
        self.assertEqual(caught.exception.status_code, 409)
        record = self.fx.manager.task_protocol_v2.get(request.agent_idempotency_key)
        self.assertEqual(
            (record.task_id, record.attempt_identity, record.fence_identity),
            (task.task_id, "attempt-original", "fence-original"),
        )
        self.assertEqual(self.fx.files(), files)

    async def test_capacity_429_never_becomes_cold_executable_or_owned_upload(self):
        accepted = await self.fx.create(self.fx.options("first"))
        before_files = self.fx.files()
        rejected = self.fx.options("second")
        upload = rejected.files[0]
        with self.assertRaises(BoundaryHTTPException) as caught:
            await self.fx.create(rejected)
        self.assertEqual(caught.exception.status_code, 429)
        cold = self.fx.cold_registry()
        recoverable = cold.recoverable_payloads()
        self.assertEqual(
            [payload["task_id"] for payload in recoverable],
            [accepted.task_id],
            "429 means unaccepted: it must not create a cold executable responsibility",
        )
        self.assertIsNone(cold.get(rejected.agent_idempotency_key))
        self.assertEqual(self.fx.files(), before_files)
        self.assertEqual(upload.reads, 0, "reject before owned upload awaits")

    async def test_upload_await_does_not_allow_a_second_key_to_steal_capacity(self):
        entered, release = asyncio.Event(), asyncio.Event()
        first = self.fx.options("slow", upload=Upload(entered=entered, release=release))
        pending = asyncio.create_task(self.fx.create(first))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            second = self.fx.options("racer")
            with self.assertRaises(BoundaryHTTPException) as caught:
                await self.fx.create(second)
            self.assertEqual(caught.exception.status_code, 429)
            self.assertEqual(second.files[0].reads, 0)
        finally:
            release.set()
            result = await asyncio.gather(pending, return_exceptions=True)
        self.assertFalse(isinstance(result[0], BaseException), result[0])
        self.assertEqual(len(self.fx.manager.tasks), 1)

    async def test_same_key_during_upload_has_explicit_original_ingress_and_no_second_owner(
        self,
    ):
        entered, release = asyncio.Event(), asyncio.Event()
        request = self.fx.options(
            "slow", upload=Upload(entered=entered, release=release)
        )
        pending = asyncio.create_task(self.fx.create(request))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            original = self.fx.manager.task_protocol_v2.get(
                request.agent_idempotency_key
            )
            before = set(self.fx.root.iterdir())
            replay = self.fx.options("slow")
            with self.assertRaises(BoundaryHTTPException) as caught:
                await asyncio.wait_for(self.fx.create(replay), 1)
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(
                caught.exception.detail,
                {
                    "code": "task_ingress_in_progress",
                    "task_id": original.task_id,
                    "accepted": False,
                },
            )
            self.assertEqual(replay.files[0].reads, 0)
            self.assertEqual(set(self.fx.root.iterdir()), before)
            replay.agent_fence_identity = "wrong-fence"
            with self.assertRaises(BoundaryHTTPException) as wrong:
                await self.fx.create(replay)
            self.assertEqual(wrong.exception.status_code, 409)
        finally:
            release.set()
            results = await asyncio.gather(pending, return_exceptions=True)
        self.assertFalse(isinstance(results[0], BaseException), results[0])
        self.assertEqual(results[0].task_id, original.task_id)

    async def test_post_bind_route_failure_keeps_input_and_same_key_hydrates_without_cold_replay(
        self,
    ):
        request = self.fx.options("accepted-before-route")
        fault = RuntimeError("injected after durable bind before route")
        with patch.object(self.fx.manager, "submit", side_effect=fault):
            with self.assertRaises(Exception) as caught:
                await self.fx.create(request)
        self.assertFalse(
            isinstance(caught.exception, BoundaryHTTPException)
            and caught.exception.status_code == 429
        )
        registry = self.fx.manager.task_protocol_v2
        accepted = registry.get(request.agent_idempotency_key)
        self.assertEqual(accepted.state, "pending")
        self.assertIsNotNone(accepted.task_payload)
        self.assertNotIn(accepted.task_id, self.fx.manager.tasks)
        files = self.fx.files()
        self.assertIn(UPLOAD_BYTES, files.values())
        with patch.object(
            registry,
            "recoverable_payloads",
            side_effect=AssertionError("online retry invoked cold recovery"),
        ):
            task = await self.fx.create(self.fx.options("accepted-before-route"))
        after = registry.get(request.agent_idempotency_key)
        self.assertEqual(task.task_id, accepted.task_id)
        self.assertEqual(after.recovery_generation, accepted.recovery_generation)
        self.assertEqual(after.task_payload, accepted.task_payload)
        self.assertEqual(self.fx.files(), files)
        self.assertEqual(self.fx.manager.queue.qsize(), 1)

    async def test_upload_failure_cleans_original_owner_and_frees_same_key(self):
        failure = OSError("injected upload read failure")
        request = self.fx.options("upload-fails", upload=Upload(failure=failure))
        with self.assertRaises(OSError) as caught:
            await self.fx.create(request)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.fx.files(), {})
        self.assertIsNone(self.fx.cold_registry().get(request.agent_idempotency_key))
        self.assertEqual(self.fx.cold_registry().recoverable_payloads(), ())
        task = await self.fx.create(self.fx.options("upload-fails"))
        self.assertIs(self.fx.manager.get(task.task_id), task)
        self.assertEqual(self.fx.manager.queue.qsize(), 1)

    async def test_stop_waits_for_reserved_upload_then_real_failed_terminal_and_ack(
        self,
    ):
        self.fx.fail_parser()
        await self.fx.manager.start()
        entered, release = asyncio.Event(), asyncio.Event()
        request = self.fx.options(
            "owned-before-stop", upload=Upload(entered=entered, release=release)
        )
        submitting = asyncio.create_task(self.fx.create(request))
        shutdown = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            shutdown = asyncio.create_task(self.fx.manager.shutdown())
            # Scheduling turns, not a wall-clock timing assertion.
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertTrue(self.fx.manager.is_shutting_down)
            self.assertFalse(
                shutdown.done(),
                "shutdown returned while owned upload could still enqueue",
            )
            rejected = self.fx.options("arrives-after-stop")
            with self.assertRaises(BoundaryHTTPException) as caught:
                await self.fx.create(rejected)
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(rejected.files[0].reads, 0)
            self.assertIsNone(
                self.fx.manager.task_protocol_v2.get(rejected.agent_idempotency_key)
            )
        finally:
            release.set()
            results = await asyncio.gather(submitting, return_exceptions=True)
            if shutdown is not None:
                await asyncio.wait_for(shutdown, 3)
        self.assertFalse(isinstance(results[0], BaseException), results[0])
        task = results[0]
        registry = self.fx.manager.task_protocol_v2
        self.assertEqual(registry.get(request.agent_idempotency_key).state, "failed")
        self.assertEqual(task.status, "failed")
        self.assertEqual(self.fx.manager.active_tasks, set())
        registry.acknowledge_failed(request.agent_idempotency_key)
        registry.cleanup_consumed()
        self.assertEqual(registry.get(request.agent_idempotency_key).state, "consumed")
        self.assertFalse(Path(task.output_dir).exists())

    async def test_owned_ingress_cleanup_failure_stays_charged_until_cold_retry(self):
        registry = self.fx.manager.task_protocol_v2
        request = self.fx.options("unbound-owned")
        record, _ = registry.reconcile_or_create(
            idempotency_key=request.agent_idempotency_key,
            task_id="owned-ingress",
            attempt_identity=request.agent_attempt_identity,
            fence_identity=request.agent_fence_identity,
            max_nonterminal_tasks=1,
        )
        root = self.fx.root / record.task_id
        (root / "uploads").mkdir(parents=True)
        registry.bind_ingress_root(record.idempotency_key)
        original = root / "uploads/partial.pdf"
        original.write_bytes(UPLOAD_BYTES)
        # Fail actual owned namespace unlink, after the cleanup intent is durable.
        with patch.object(
            protocol.os, "unlink", side_effect=OSError("owned unlink refused")
        ):
            with self.assertRaises(OSError):
                registry.abort_ingress(record.idempotency_key)
        retained = self.fx.cold_registry().get(record.idempotency_key)
        self.assertEqual(retained.state, "ingress_cleanup")
        self.assertIsNone(retained.task_payload)
        self.assertEqual(original.read_bytes(), UPLOAD_BYTES)
        with self.assertRaises(protocol.TaskAdmissionFull):
            registry.reconcile_or_create(
                idempotency_key=self.fx.options("other").agent_idempotency_key,
                task_id="other",
                attempt_identity="attempt-original",
                fence_identity="fence-original",
                max_nonterminal_tasks=1,
            )
        cold = self.fx.cold_registry()
        self.assertEqual(cold.recoverable_payloads(), ())
        self.assertIsNone(cold.get(record.idempotency_key))
        self.assertFalse(root.exists())

    async def test_crash_before_ingress_owner_receipt_never_blindly_removes_directory(
        self,
    ):
        registry = self.fx.manager.task_protocol_v2
        request = self.fx.options("unowned")
        record, _ = registry.reconcile_or_create(
            idempotency_key=request.agent_idempotency_key,
            task_id="unowned-ingress",
            attempt_identity="attempt-original",
            fence_identity="fence-original",
            max_nonterminal_tasks=1,
        )
        root = self.fx.root / record.task_id
        root.mkdir()
        sentinel = root / "unproven-owner.txt"
        sentinel.write_bytes(b"must survive uncertain ownership")
        cold = self.fx.cold_registry()
        with self.assertRaises(protocol.TaskProtocolConflict):
            cold.recoverable_payloads()
        self.assertEqual(sentinel.read_bytes(), b"must survive uncertain ownership")
        retained = self.fx.cold_registry().get(record.idempotency_key)
        self.assertIn(retained.state, ("ingress", "ingress_cleanup"))
        self.assertIsNone(retained.task_payload)

    async def test_cold_legacy_backlog_over_capacity_drains_without_queue_expansion_or_new_admission(
        self,
    ):
        first = self.fx.seed_legacy_pending("a")
        second = self.fx.seed_legacy_pending("b")
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []
        self.fx.fail_parser(entered=entered, release=release, seen=seen)
        # Fresh actual manager with its strict capacity-one constructor; no limit override.
        self.fx.manager = self.fx.module.AsyncTaskManager(self.fx.manager.app)
        await self.fx.manager.start()
        try:
            await asyncio.wait_for(entered.wait(), 1)
            status = self.fx.manager.task_protocol_v2.admission_status(
                set(self.fx.manager.tasks)
            )
            self.assertEqual(status["durable_nonterminal_tasks"], 2)
            self.assertEqual(self.fx.manager.queue.maxsize, 1)
            self.assertLessEqual(self.fx.manager.queue.qsize(), 1)
            self.assertLessEqual(len(self.fx.manager.active_tasks), 1)
            rejected = self.fx.options("new-during-recovery")
            with self.assertRaises(BoundaryHTTPException) as caught:
                await self.fx.create(rejected)
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(caught.exception.detail, "recovery_overcommitted")
            self.assertEqual(rejected.files[0].reads, 0)
            self.assertIsNone(
                self.fx.manager.task_protocol_v2.get(rejected.agent_idempotency_key)
            )
        finally:
            release.set()
            await asyncio.wait_for(self.fx.manager.shutdown(), 3)
        self.assertCountEqual(seen, [first.task_id, second.task_id])
        self.assertEqual(len(seen), 2)
        registry = self.fx.manager.task_protocol_v2
        for task in (first, second):
            key = task.agent_idempotency_key
            self.assertEqual(registry.get(key).state, "failed")
            registry.acknowledge_failed(key)
        registry.cleanup_consumed()
        self.assertEqual(self.fx.files(), {})
        self.assertEqual(
            registry.admission_status(set())["durable_nonterminal_tasks"], 0
        )

    async def test_committed_bind_persistence_failure_is_visible_and_does_not_delete_accepted_input(
        self,
    ):
        registry = self.fx.manager.task_protocol_v2
        request = self.fx.options("bind-committed-error")
        bind = registry.bind_task_payload
        failure = protocol.TaskRegistryPersistenceError(
            operation="bind_task_payload",
            phase="directory_fsync",
            outcome="indeterminate",
            committed=True,
        )

        def committed_then_lost(*args, **kwargs):
            bind(*args, **kwargs)
            raise failure

        with patch.object(
            registry, "bind_task_payload", side_effect=committed_then_lost
        ):
            with self.assertRaises(Exception) as caught:
                await self.fx.create(request)
        current = caught.exception
        chain = []
        while current is not None and current not in chain:
            chain.append(current)
            current = current.__cause__ or current.__context__
        self.assertIn(failure, chain, "original persistence truth must remain visible")
        accepted = self.fx.cold_registry().get(request.agent_idempotency_key)
        self.assertEqual(accepted.state, "pending")
        self.assertIsNotNone(accepted.task_payload)
        self.assertEqual(
            Path(accepted.task_payload["uploads"][0]).read_bytes(), UPLOAD_BYTES
        )
        self.assertFalse(
            isinstance(caught.exception, BoundaryHTTPException)
            and caught.exception.status_code == 429
        )

    async def test_reservation_persistence_refusal_creates_no_owned_upload_and_retry_is_safe(
        self,
    ):
        request = self.fx.options("reservation-write-refused")
        upload = request.files[0]
        with patch.object(
            protocol.os, "replace", side_effect=OSError("injected storage refusal")
        ):
            with self.assertRaises(BoundaryHTTPException) as caught:
                await self.fx.create(request)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIsInstance(
            caught.exception.__cause__, protocol.TaskRegistryPersistenceError
        )
        self.assertFalse(caught.exception.__cause__.committed)
        self.assertEqual(upload.reads, 0)
        self.assertEqual(self.fx.files(), {})
        self.assertIsNone(self.fx.cold_registry().get(request.agent_idempotency_key))
        task = await self.fx.create(self.fx.options("reservation-write-refused"))
        self.assertIs(self.fx.manager.get(task.task_id), task)
        self.assertEqual(self.fx.manager.queue.qsize(), 1)

    async def test_result_credit_refusal_becomes_failed_terminal_and_requires_ack(self):
        registry = self.fx.manager.task_protocol_v2
        previous = self.fx.seed_legacy_pending("reserved")
        registry.transition(previous.agent_idempotency_key, "processing")
        registry.transition(previous.agent_idempotency_key, "finalizing")
        registry.reserve_finalizer(previous.agent_idempotency_key, byte_budget=1024)
        self.assertEqual(registry.reserved_result_bytes, 1024)
        # Old accepted second responsibility demonstrates downstream refusal,
        # without broadening current new-key admission or changing pool capacity.
        target = self.fx.seed_legacy_pending("result-refused")
        parsed = []

        async def parse_boundary(**kwargs):
            parsed.append(kwargs["request_options"].task_id)

        async def finalize_forbidden(task):
            self.fail("no output builder may run without retained-result credit")

        self.fx.module.run_parse_job = parse_boundary
        self.fx.module.build_retained_task_result = finalize_forbidden
        await self.fx.manager.submit(target)
        self.assertEqual(self.fx.manager.queue.get_nowait(), target.task_id)
        await self.fx.manager._process_task(target.task_id)
        record = registry.get(target.agent_idempotency_key)
        self.assertEqual(parsed, [target.task_id])
        self.assertEqual(record.state, "failed")
        self.assertEqual(json.loads(record.error)["code"], "parse_or_finalize_failed")
        self.assertEqual(registry.reserved_result_bytes, 1024)
        self.assertEqual(record.reserved_result_bytes, 0)
        self.assertTrue(Path(target.uploads[0]).exists())
        registry.acknowledge_failed(target.agent_idempotency_key)
        registry.cleanup_consumed()
        self.assertFalse(Path(target.output_dir).exists())
        self.assertTrue(Path(previous.output_dir).exists())
        self.assertEqual(
            registry.get(previous.agent_idempotency_key).state, "finalizing"
        )

    async def test_open_task_directory_first_fstat_failure_closes_the_acquired_root_fd(
        self,
    ):
        registry = self.fx.manager.task_protocol_v2
        task_root = self.fx.root / "directory-acquisition"
        task_root.mkdir()
        real_open, real_fstat = protocol.os.open, protocol.os.fstat
        acquired = []
        failure = OSError("injected first directory fstat failure")

        def observe_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if (
                kwargs.get("dir_fd") is None
                and Path(path).resolve() == self.fx.root.resolve()
            ):
                acquired.append(fd)
            return fd

        def fail_acquired_fstat(fd):
            if acquired and fd == acquired[0]:
                raise failure
            return real_fstat(fd)

        with (
            patch.object(protocol.os, "open", side_effect=observe_open),
            patch.object(protocol.os, "fstat", side_effect=fail_acquired_fstat),
        ):
            with self.assertRaises(OSError) as caught:
                registry._open_task_dir(task_root.name)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(acquired), 1)
        with self.assertRaises(OSError) as closed:
            real_fstat(acquired[0])
        self.assertEqual(closed.exception.errno, errno.EBADF)
        root_fd, task_fd = registry._open_task_dir(task_root.name)
        try:
            self.assertEqual(real_fstat(root_fd).st_ino, self.fx.root.stat().st_ino)
            self.assertEqual(real_fstat(task_fd).st_ino, task_root.stat().st_ino)
        finally:
            protocol.os.close(task_fd)
            protocol.os.close(root_fd)


if __name__ == "__main__":
    unittest.main()
