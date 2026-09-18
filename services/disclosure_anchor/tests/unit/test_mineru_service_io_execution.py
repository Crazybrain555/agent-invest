"""R24 implementation acceptance probes, not a claim of independent authorship."""
from __future__ import annotations

import asyncio
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from scripts.windows.mineru_heap_trim_compat.agent_task_protocol_v2 import TaskRegistryObservationBusy
from tests._mineru_service_io_asgi_fixture import ServiceIOASGIFixture, until


class ServiceIOExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = ServiceIOASGIFixture()
        self.addAsyncCleanup(self.fx.close)
        await self.fx.start()

    async def test_actual_asgi_result_bytes_then_ack_close_original_tree(self):
        task = await self.fx.completed()
        raw = Path(task.result_artifact_path).read_bytes()
        response = await self.fx.client.get(f"/tasks/{task.task_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, raw)
        self.assertEqual(self.fx.manager.task_protocol_v2.get(task.agent_idempotency_key).active_readers, 0)
        ack = await self.fx.client.post(f"/tasks/{task.task_id}/ack")
        self.assertEqual(ack.status_code, 200)
        self.assertEqual(ack.json()["status"], "consumed")
        self.assertFalse(Path(task.output_dir).exists())

    async def test_response_construction_failure_releases_acquired_pin(self):
        task = await self.fx.completed()
        error = RuntimeError("controlled response construction failure")
        with patch.object(self.fx.module, "FileResponse", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                await self.fx.client.get(f"/tasks/{task.task_id}/result")
        self.assertIs(caught.exception, error)
        self.assertEqual(self.fx.manager.task_protocol_v2.get(task.agent_idempotency_key).active_readers, 0)
        self.assertTrue(Path(task.result_artifact_path).is_file())

    async def test_send_failure_releases_reader_without_ack_or_deletion(self):
        task = await self.fx.completed()
        calls = []
        failure = OSError("controlled ASGI send failure")
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            calls.append(message["type"])
            if message["type"] == "http.response.body":
                raise failure
        scope = {"type":"http", "asgi":{"version":"3.0", "spec_version":"2.4"},
                 "http_version":"1.1", "method":"GET", "scheme":"http",
                 "path":f"/tasks/{task.task_id}/result", "raw_path":f"/tasks/{task.task_id}/result".encode(),
                 "query_string":b"", "headers":[], "server":("service.test",80),
                 "client":("127.0.0.1",1234), "root_path":"", "extensions":{}}
        with self.assertRaises(OSError) as caught:
            await self.fx.app(scope, receive, send)
        self.assertIs(caught.exception, failure)
        self.assertIn("http.response.body", calls)
        record = self.fx.manager.task_protocol_v2.get(task.agent_idempotency_key)
        self.assertEqual((record.state, record.active_readers), ("completed", 0))
        self.assertTrue(Path(task.result_artifact_path).exists())

    async def test_raw_cancel_waits_for_send_then_releases_reader_once(self):
        task = await self.fx.completed()
        entered, release = asyncio.Event(), asyncio.Event()
        self.fx.releases.append(release)
        async def receive():
            return {"type":"http.request", "body":b"", "more_body":False}
        async def send(message):
            if message["type"] == "http.response.body":
                entered.set()
                await release.wait()
        scope = {"type":"http", "asgi":{"version":"3.0", "spec_version":"2.4"},
                 "http_version":"1.1", "method":"GET", "scheme":"http",
                 "path":f"/tasks/{task.task_id}/result", "raw_path":b"/result", "query_string":b"",
                 "headers":[], "server":("service.test",80), "client":("127.0.0.1",1234), "root_path":""}
        request = self.fx.spawn(self.fx.app(scope, receive, send))
        await asyncio.wait_for(entered.wait(), 2)
        request.cancel()
        await asyncio.sleep(0)
        request.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(request.done())
        self.assertEqual(self.fx.manager.task_protocol_v2.get(task.agent_idempotency_key).active_readers, 1)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertEqual(self.fx.manager.task_protocol_v2.get(task.agent_idempotency_key).active_readers, 0)
        self.assertTrue(Path(task.result_artifact_path).exists())

    async def test_long_unlink_does_not_block_real_health_or_heartbeat(self):
        task = await self.fx.completed()
        registry = self.fx.manager.task_protocol_v2
        entered, release = threading.Event(), threading.Event()
        self.fx.releases.append(release)
        original = registry._unlink_owned_result
        def blocked(record, *, before_unlink):
            entered.set()
            if not release.wait(3):
                raise AssertionError("controlled unlink barrier expired")
            return original(record, before_unlink=before_unlink)
        with patch.object(registry, "_unlink_owned_result", side_effect=blocked):
            ack = self.fx.spawn(self.fx.client.post(f"/tasks/{task.task_id}/ack"))
            await until(entered.is_set)
            ticks = []
            async def heartbeat():
                for _ in range(8):
                    ticks.append(time.monotonic())
                    await asyncio.sleep(0.01)
            start = time.monotonic()
            response, _ = await asyncio.gather(self.fx.client.get("/health"), heartbeat())
            self.assertEqual(response.status_code, 200)
            self.assertLess(time.monotonic() - start, 0.9)
            self.assertEqual(len(ticks), 8)
            self.assertFalse(ack.done())
            self.assertEqual(registry.get(task.agent_idempotency_key).state, "cleanup_pending")
            release.set()
            self.assertEqual((await ack).status_code, 200)
        self.assertEqual(registry.get(task.agent_idempotency_key).state, "consumed")

    async def test_cancel_ack_after_intent_waits_until_consumed(self):
        task = await self.fx.completed()
        registry = self.fx.manager.task_protocol_v2
        entered, release = threading.Event(), threading.Event()
        self.fx.releases.append(release)
        original = registry._unlink_owned_result
        def blocked(record, *, before_unlink):
            entered.set()
            if not release.wait(3):
                raise AssertionError("controlled unlink barrier expired")
            return original(record, before_unlink=before_unlink)
        with patch.object(registry, "_unlink_owned_result", side_effect=blocked):
            ack = self.fx.spawn(self.fx.module.ack_async_task_result(task.task_id))
            await until(entered.is_set)
            ack.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(ack.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await ack
        self.assertEqual(registry.get(task.agent_idempotency_key).state, "consumed")
        self.assertFalse(Path(task.output_dir).exists())

    async def test_cleanup_commit_failure_keeps_intent_and_other_task_commit(self):
        task = await self.fx.completed()
        registry = self.fx.manager.task_protocol_v2
        entered, release = threading.Event(), threading.Event()
        self.fx.releases.append(release)
        unlink = registry._unlink_owned_result
        persist = registry._persist
        fault = OSError("controlled post-unlink persist refusal")
        def blocked(record, *, before_unlink):
            entered.set()
            if not release.wait(3):
                raise AssertionError("controlled unlink barrier expired")
            return unlink(record, before_unlink=before_unlink)
        def fail_commit():
            if registry._records[task.agent_idempotency_key].state == "consumed":
                raise fault
            return persist()
        with patch.object(registry,"_unlink_owned_result",side_effect=blocked), patch.object(registry,"_persist",side_effect=fail_commit):
            ack = self.fx.spawn(self.fx.module.ack_async_task_result(task.task_id))
            await until(entered.is_set)
            other = self.fx.fx.options("other-commit")
            await self.fx.manager.service_io.call(registry.reconcile_or_create,
                idempotency_key=other.agent_idempotency_key, task_id="other-task", attempt_identity="other-attempt",
                fence_identity="other-fence", max_nonterminal_tasks=3)
            release.set()
            with self.assertRaises((OSError, HTTPException)):
                await ack
        self.assertIsNotNone(registry.get(other.agent_idempotency_key))
        self.assertEqual(registry.get(task.agent_idempotency_key).state,"cleanup_pending")
        await self.fx.manager.service_io.call(registry.cleanup_consumed, lane="bulk", required=True)
        self.assertEqual(registry.get(task.agent_idempotency_key).state,"consumed")

    async def test_request_saturation_keeps_health_live_and_has_no_ack_effect(self):
        io = self.fx.manager.service_io
        held = [io.open_request("mutation") for _ in range(io._max_pending)]
        try:
            with self.assertRaises(TaskRegistryObservationBusy):
                io.open_request("mutation")
            response = await self.fx.client.post("/tasks/absent/ack")
            self.assertEqual(response.status_code, 503)
            self.assertEqual((await self.fx.client.get("/health")).status_code, 200)
        finally:
            for scope in held:
                await scope.close()
        self.assertEqual(io._request_counts, {"mutation":0,"reader":0})

    async def test_shutdown_waits_for_scoped_response_before_closing_executors(self):
        io = self.fx.manager.service_io
        scope = io.open_request("reader")
        events = []
        scope.defer(lambda:events.append("released"))
        closing = self.fx.spawn(self.fx.manager.shutdown())
        await asyncio.sleep(0.02)
        self.assertFalse(closing.done())
        with self.assertRaises(TaskRegistryObservationBusy):
            io.open_request("reader")
        await scope.close()
        await asyncio.wait_for(closing, 2)
        self.assertEqual(events, ["released"])

    async def test_anyio_cancel_scope_preserves_send_and_releases_reader(self):
        import anyio
        task = await self.fx.completed()
        entered, release = asyncio.Event(), asyncio.Event()
        self.fx.releases.append(release)
        cancel_scope = anyio.CancelScope()
        async def receive():
            return {"type":"http.request", "body":b"", "more_body":False}
        async def send(message):
            if message["type"] == "http.response.body":
                entered.set()
                await release.wait()
        scope = {"type":"http", "asgi":{"version":"3.0", "spec_version":"2.4"},
                 "http_version":"1.1", "method":"GET", "scheme":"http",
                 "path":f"/tasks/{task.task_id}/result", "raw_path":b"/result", "query_string":b"",
                 "headers":[], "server":("service.test",80), "client":("127.0.0.1",1234), "root_path":""}
        async def request():
            with cancel_scope:
                await self.fx.app(scope, receive, send)
        running = self.fx.spawn(request())
        await asyncio.wait_for(entered.wait(), 2)
        cancel_scope.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(running.done())
        self.assertEqual(self.fx.manager.task_protocol_v2.get(task.agent_idempotency_key).active_readers, 1)
        release.set()
        await asyncio.wait_for(running, 2)
        self.assertEqual(self.fx.manager.task_protocol_v2.get(task.agent_idempotency_key).active_readers, 0)

    async def test_range_keeps_file_response_contract_and_releases_pin(self):
        task = await self.fx.completed()
        raw = Path(task.result_artifact_path).read_bytes()
        response = await self.fx.client.get(f"/tasks/{task.task_id}/result", headers={"Range":"bytes=0-7"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, raw[:8])
        self.assertEqual(response.headers["content-range"], f"bytes 0-7/{len(raw)}")
        self.assertEqual(self.fx.manager.task_protocol_v2.get(task.agent_idempotency_key).active_readers, 0)

    async def test_duplicate_ack_and_periodic_cleaner_delete_exactly_once(self):
        task = await self.fx.completed()
        registry = self.fx.manager.task_protocol_v2
        entered, release = threading.Event(), threading.Event()
        self.fx.releases.append(release)
        original = registry._unlink_owned_result
        deletions = []
        def blocked(record, *, before_unlink):
            deletions.append(record.idempotency_key)
            entered.set()
            if not release.wait(3):
                raise AssertionError("controlled cleaner barrier expired")
            return original(record, before_unlink=before_unlink)
        with patch.object(registry, "_unlink_owned_result", side_effect=blocked):
            first = self.fx.spawn(self.fx.client.post(f"/tasks/{task.task_id}/ack"))
            await until(entered.is_set)
            second = self.fx.spawn(self.fx.client.post(f"/tasks/{task.task_id}/ack"))
            periodic = self.fx.spawn(self.fx.manager.cleanup_expired_tasks())
            await asyncio.sleep(0.01)
            self.assertFalse(first.done())
            release.set()
            responses = await asyncio.gather(first, second, periodic)
        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(responses[1].status_code, 200)
        self.assertEqual(deletions, [task.agent_idempotency_key])
        self.assertEqual(registry.get(task.agent_idempotency_key).state, "consumed")
        self.assertEqual(registry.unacked_result_bytes, 0)

    async def test_slow_persist_lock_returns_busy_not_cached_healthy(self):
        registry = self.fx.manager.task_protocol_v2
        entered, release = threading.Event(), threading.Event()
        self.fx.releases.append(release)
        def hold_registry_lock():
            with registry._lock:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("controlled registry lock barrier expired")
        holding = self.fx.spawn(self.fx.manager.service_io.call(hold_registry_lock))
        await until(entered.is_set)
        try:
            response = await asyncio.wait_for(self.fx.client.get("/health"), 1.5)
            self.assertEqual(response.status_code, 503)
            self.assertNotEqual(response.json().get("status"), "healthy")
            self.assertFalse(holding.done())
        finally:
            release.set()
            await holding
        self.assertEqual((await self.fx.client.get("/health")).status_code, 200)

    async def test_missing_retained_zip_returns_410_without_pinning_a_reader(self):
        task = await self.fx.completed()
        retained = Path(task.result_artifact_path)
        raw = retained.read_bytes()
        retained.unlink()
        try:
            response = await self.fx.client.get(f"/tasks/{task.task_id}/result")
        finally:
            retained.write_bytes(raw)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["detail"], "Retained task result is unavailable")
        self.assertEqual(self.fx.manager.task_protocol_v2.get(task.agent_idempotency_key).active_readers, 0)

    async def test_absent_task_manager_is_503_not_500_on_scoped_routes(self):
        self.fx.app.state.task_manager = None
        try:
            response = await self.fx.client.post("/tasks/absent/ack")
        finally:
            self.fx.app.state.task_manager = self.fx.manager
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Task manager is not initialized")


class ServiceStopProjectionTests(unittest.TestCase):
    def test_api_only_explicit_stop_budget_is_part_of_real_projection(self):
        from copy import deepcopy
        from disclosure_anchor.application.services.mineru_release_plan import compose_document, verify_compose_projection
        from disclosure_anchor.application.contracts.mineru_deployment_profile import decode_mineru_deployment_profile
        from tests.unit.test_mineru_release_compose_independent import DEPLOYMENT, _wire
        from tests.unit.test_mineru_release_projection_independent import _capacity
        profile = decode_mineru_deployment_profile(_wire(DEPLOYMENT))
        capacity = _capacity()
        document = compose_document(profile, capacity)
        self.assertEqual(document["services"]["mineru-api"]["stop_grace_period"], "10s")
        self.assertNotIn("stop_grace_period", document["services"]["mineru-api-proxy"])
        self.assertNotIn("stop_grace_period", document["services"]["mineru-openai-server"])
        for bad in (None, "30s", -1):
            changed = deepcopy(document)
            if bad is None:
                del changed["services"]["mineru-api"]["stop_grace_period"]
            else:
                changed["services"]["mineru-api"]["stop_grace_period"] = bad
            self.assertTrue(verify_compose_projection(changed, capacity, profile))
