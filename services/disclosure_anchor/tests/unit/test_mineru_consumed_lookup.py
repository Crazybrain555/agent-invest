"""ACK removes read routes but retains the idempotency tombstone for POST.

Independent local responsibility tests. Synthetic uploads/result bytes never
claim PDF parsing, FastAPI transport, provider content, or runtime qualification.
"""

import ast
import asyncio
import hashlib
import tempfile
import types
import unittest
from pathlib import Path

from tests._mineru_admission_fixture import (
    AdmissionFixture,
    BoundaryHTTPException,
    Upload,
)


class ConsumedLookupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mineru-consumed-lookup-")
        self.addCleanup(temporary.cleanup)
        self.fx = AdmissionFixture(Path(temporary.name))
        self.addCleanup(self.fx.close)
        self.addAsyncCleanup(self.fx.dispose_test_tasks)
        # Extract the actual generated routes, excluding decorators/module startup.
        names = {
            "reconcile_async_task",
            "ack_async_task_result",
            "get_async_task_status",
        }
        body = [
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            )
        ]
        found = set()
        for node in ast.parse(self.fx.generated).body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name in names:
                node.decorator_list = []
                body.append(node)
                found.add(node.name)
        self.assertEqual(found, names)
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                "<actual-generated-consumed-routes>",
                "exec",
            ),
            self.fx.module.__dict__,
        )
        self.request = types.SimpleNamespace(
            url_for=lambda name, **params: (
                f"http://fixture.invalid/{name}/{params['task_id']}"
            )
        )

    async def lookup(self, key):
        return await self.fx.module.reconcile_async_task(key, self.request)

    async def test_real_completed_ack_removes_both_get_routes_but_repost_stays_gone(
        self,
    ):
        options = self.fx.options("completed-original")
        task = await self.fx.create(options)
        registry = self.fx.manager.task_protocol_v2
        key = options.agent_idempotency_key
        result = Path(task.output_dir) / "result.zip"
        raw = b"synthetic-owned-result-not-a-provider-zip"

        async def parse():
            pass

        async def finalize():
            result.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            owner = hashlib.sha256(
                f"{task.task_id}\0{digest}\0{len(raw)}".encode()
            ).hexdigest()
            return result, digest, len(raw), owner

        await self.fx.manager.task_protocol_executor.run(
            registry=registry, key=key, parse=parse, finalize=finalize
        )
        self.assertEqual(registry.get(key).state, "completed")
        self.assertEqual((await self.lookup(key))["status"], "completed")
        ack = await self.fx.module.ack_async_task_result(task.task_id)
        self.assertEqual(
            ack,
            {
                "schema": "mineru-task-protocol.v2",
                "task_id": task.task_id,
                "status": "consumed",
            },
        )
        self.assertFalse(Path(task.output_dir).exists())
        self.assertNotIn(task.task_id, self.fx.manager.tasks)
        self.assertNotIn(task.task_id, self.fx.manager.task_events)
        tombstone = self.fx.cold_registry().get(key)
        self.assertEqual(
            (tombstone.state, tombstone.task_id), ("consumed", task.task_id)
        )
        original = (
            tombstone.attempt_identity,
            tombstone.fence_identity,
            tombstone.recovery_generation,
        )
        # Same-key POST still prevents execution; read-route absence is not a permit.
        replay = self.fx.options("completed-original")
        with self.assertRaises(BoundaryHTTPException) as post:
            await self.fx.create(replay)
        self.assertEqual(post.exception.status_code, 410)
        self.assertEqual(
            post.exception.detail, "Task responsibility requires reconciliation"
        )
        self.assertEqual(replay.files[0].reads, 0)
        for route, call in (
            ("by-idempotency", lambda: self.lookup(key)),
            (
                "task-id",
                lambda: self.fx.module.get_async_task_status(
                    task.task_id, self.request
                ),
            ),
        ):
            with self.subTest(route=route):
                with self.assertRaises(BoundaryHTTPException) as missing:
                    await call()
                self.assertEqual(
                    (missing.exception.status_code, missing.exception.detail),
                    (404, "Task not found"),
                )
        after = self.fx.cold_registry().get(key)
        self.assertEqual(
            (after.attempt_identity, after.fence_identity, after.recovery_generation),
            original,
        )
        self.assertEqual(after.state, "consumed")

    async def test_fresh_missing_is_404_and_live_pending_lookup_keeps_same_responsibility(
        self,
    ):
        missing = self.fx.options("never-submitted")
        with self.assertRaises(BoundaryHTTPException) as absent:
            await self.lookup(missing.agent_idempotency_key)
        self.assertEqual(
            (absent.exception.status_code, absent.exception.detail),
            (404, "Task not found"),
        )
        options = self.fx.options("pending")
        task = await self.fx.create(options)
        before = self.fx.files()
        for _ in range(2):
            value = await self.lookup(options.agent_idempotency_key)
            self.assertEqual(
                (value["task_id"], value["status"], value["protocol_state"]),
                (task.task_id, "pending", "pending"),
            )
            self.assertEqual(value["idempotency_key"], options.agent_idempotency_key)
            self.assertIs(self.fx.manager.tasks[task.task_id], task)
        self.assertEqual(self.fx.manager.queue.qsize(), 1)
        self.assertEqual(self.fx.files(), before)
        self.assertIsNone(self.fx.cold_registry().get(missing.agent_idempotency_key))

    async def test_ingress_and_unowned_accepted_recovery_still_return_503(self):
        entered, release = asyncio.Event(), asyncio.Event()
        options = self.fx.options(
            "uploading", upload=Upload(entered=entered, release=release)
        )
        creating = asyncio.create_task(self.fx.create(options))
        registry = self.fx.manager.task_protocol_v2
        try:
            await asyncio.wait_for(entered.wait(), 1)
            record = registry.get(options.agent_idempotency_key)
            with self.assertRaises(BoundaryHTTPException) as in_flight:
                await self.lookup(record.idempotency_key)
            self.assertEqual(in_flight.exception.status_code, 503)
            self.assertEqual(
                in_flight.exception.detail,
                {
                    "code": "task_ingress_in_progress",
                    "task_id": record.task_id,
                    "accepted": False,
                },
            )
            # A separate cold projection has no owner for the retained ingress.
            original_manager = self.fx.module.get_task_manager
            cold = self.fx.module.AsyncTaskManager(
                types.SimpleNamespace(state=types.SimpleNamespace(config={}))
            )
            self.fx.module.get_task_manager = lambda: cold
            try:
                with self.assertRaises(BoundaryHTTPException) as recovery:
                    await self.lookup(record.idempotency_key)
                self.assertEqual(recovery.exception.status_code, 503)
                self.assertEqual(
                    recovery.exception.detail,
                    {
                        "code": "ingress_recovery_required",
                        "task_id": record.task_id,
                        "accepted": False,
                    },
                )
            finally:
                self.fx.module.get_task_manager = original_manager
        finally:
            release.set()
            task = await asyncio.wait_for(creating, 2)
        registry.transition(options.agent_idempotency_key, "processing")
        # Real durable processing with no live processor must not become absence.
        self.fx.manager._scheduled_task_ids.discard(task.task_id)
        with self.assertRaises(BoundaryHTTPException) as accepted:
            await self.lookup(options.agent_idempotency_key)
        self.assertEqual(accepted.exception.status_code, 503)
        self.assertEqual(
            accepted.exception.detail,
            {
                "code": "accepted_recovery_required",
                "task_id": task.task_id,
                "accepted": True,
            },
        )
        self.assertEqual(
            registry.get(options.agent_idempotency_key).state, "processing"
        )
        self.assertTrue(Path(task.output_dir).exists())
