"""Independent synthetic witnesses for one native storage-hold decision.

Only disposable registry trees and the generated API fixture are used. No
parser, database, network, native service or shared runtime is involved.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as native
from tests._mineru_capacity_lifecycle_fixture import BoundaryHTTPException
from tests._mineru_result_storage_fixture import HoldRouteRequest, MIB, ResultStorageApiFixture
from tests.unit.test_mineru_result_storage import (
    OUTPUTS, PLENTY, StorageLab, parse_writing, stages,
)


RUNTIME = "sha256:" + "a" * 64
OTHER_RUNTIME = "sha256:" + "b" * 64
KEY = "accepted-original-key"
TASK = "accepted-original-task"
OPERATOR_CREDENTIAL = "independent-storage-hold-operator-" + "x" * 24


def _decision(preview: dict[str, object]) -> dict[str, str]:
    return {
        "runtime_identity_sha256": RUNTIME,
        "expected_preview_sha256": str(preview["preview_sha256"]),
        "decided_by": "synthetic-operator",
        "reason": "verified tree integrity cannot progress",
        "fixed_by": "no repair; explicit terminal decision",
    }


class NativeHoldDecisionRegistryIndependentTests(unittest.TestCase):
    def held(self) -> tuple[StorageLab, native.DurableTaskRegistry, Path]:
        lab = StorageLab(self)
        registry = lab.open()
        root = lab.accept(registry, KEY, TASK)
        registry.reserve_source_growth(
            KEY, live_free_bytes=PLENTY, outstanding_promise_bytes=0,
            completion_head_waiting=False,
        )
        registry.transition(KEY, "processing")
        retained = root / "retained-source.bin"
        retained.write_bytes(b"accepted bytes that ACK alone may remove")
        registry.block_storage(KEY, reason="tree_integrity")
        return lab, registry, retained

    def test_exact_decision_is_durable_replayable_and_keeps_bytes_until_ack(self) -> None:
        lab, registry, retained = self.held()
        before = registry.get(KEY)
        assert before is not None and before.storage is not None
        usage = registry.storage_usage()
        preview = registry.storage_hold_preview(KEY, runtime_identity_sha256=RUNTIME)
        fact_bytes = json.dumps(
            {key: value for key, value in preview.items() if key != "preview_sha256"},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()
        self.assertEqual(preview["preview_sha256"], "sha256:" + hashlib.sha256(fact_bytes).hexdigest())
        self.assertEqual(
            (preview["task_id"], preview["idempotency_key"], preview["attempt_identity"],
             preview["fence_identity"], preview["state"], preview["hold_reason"]),
            (TASK, KEY, before.attempt_identity, before.fence_identity, "processing", "tree_integrity"),
        )
        self.assertEqual(registry.get(KEY), before)  # Preview is read-only.
        decision = _decision(preview)
        first = registry.decide_storage_hold(KEY, **decision)
        failed = registry.get(KEY)
        assert failed is not None and failed.failure_cause is not None
        self.assertEqual((first["state"], first["replayed"]), ("failed", False))
        decision_bytes = json.dumps({
            "schema": native.STORAGE_HOLD_DECISION_SCHEMA,
            "preview_sha256": decision["expected_preview_sha256"],
            "decided_by": decision["decided_by"], "reason": decision["reason"],
            "fixed_by": decision["fixed_by"],
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        self.assertEqual(first["decision_sha256"],
                         "sha256:" + hashlib.sha256(decision_bytes).hexdigest())
        self.assertEqual((failed.state, failed.failure_cause["code"],
                          failed.failure_cause["retry_class"]),
                         ("failed", "storage_hold_terminated", "permanent"))
        self.assertEqual(failed.failure_cause["decision_sha256"], first["decision_sha256"])
        self.assertEqual(failed.failure_cause["hold_reason"], "tree_integrity")
        self.assertEqual((failed.task_id, failed.attempt_identity, failed.fence_identity),
                         (before.task_id, before.attempt_identity, before.fence_identity))
        self.assertEqual((failed.storage["phase"], failed.storage["source_bytes"]),
                         (before.storage["phase"], before.storage["source_bytes"]))
        self.assertEqual(registry.storage_usage(), usage)
        self.assertTrue(retained.is_file())

        # The first HTTP response may be lost. Reopen the real registry and
        # replay exactly one decision, without a new task or altered receipt.
        reopened = lab.open()
        replay = reopened.decide_storage_hold(KEY, **decision)
        self.assertEqual({**replay, "replayed": False}, first)
        with self.assertRaises(native.TaskProtocolConflict):
            reopened.decide_storage_hold(KEY, **{**_decision(preview), "reason": "changed"})
        self.assertTrue(retained.is_file())
        self.assertEqual(reopened.acknowledge_terminal_intent(KEY), "cleanup_pending")
        pending = reopened.get(KEY)
        self.assertEqual((pending.state, pending.cleanup_kind), ("cleanup_pending", "task_tree"))
        self.assertEqual(pending.failure_cause["decision_sha256"], first["decision_sha256"])
        self.assertTrue(retained.is_file())
        self.assertEqual(reopened.cleanup_consumed(idempotency_key=KEY), 1)
        self.assertEqual(reopened.get(KEY).state, "consumed")
        self.assertFalse(retained.exists())
        # A receipt already issued to the operator remains exact even after
        # ordinary ACK clears the task's private failure fields.
        self.assertEqual(first["decision_sha256"], replay["decision_sha256"])

    def test_stale_wrong_task_nonblocked_and_completed_result_are_refused(self) -> None:
        lab, registry, retained = self.held()
        preview = registry.storage_hold_preview(KEY, runtime_identity_sha256=RUNTIME)
        other_lab = StorageLab(self)
        other_registry = other_lab.open()
        other_lab.accept(other_registry, "other-key", "other-task")
        other_registry.reserve_source_growth(
            "other-key", live_free_bytes=PLENTY, outstanding_promise_bytes=0,
            completion_head_waiting=False,
        )
        other_registry.transition("other-key", "processing")
        other_registry.block_storage("other-key", reason="seal_integrity")
        with self.assertRaises(native.TaskProtocolConflict):
            other_registry.decide_storage_hold("other-key", **_decision(preview))
        with self.assertRaises(native.TaskProtocolConflict):
            registry.decide_storage_hold(KEY, **{**_decision(preview),
                                                  "runtime_identity_sha256": OTHER_RUNTIME})
        registry.block_storage(KEY, reason="seal_integrity")
        with self.assertRaises(native.TaskProtocolConflict):
            registry.decide_storage_hold(KEY, **_decision(preview))
        self.assertEqual(registry.get(KEY).state, "processing")
        self.assertEqual(other_registry.get("other-key").state, "processing")
        self.assertTrue(retained.is_file())

        ordinary = StorageLab(self)
        normal = ordinary.open()
        normal_root = ordinary.accept(normal, "normal-key", "normal-task")
        with self.assertRaises(native.TaskProtocolConflict):
            normal.storage_hold_preview("normal-key", runtime_identity_sha256=RUNTIME)
        executor = ordinary.executor()
        asyncio.run(asyncio.wait_for(executor.run_storage(
            registry=normal, key="normal-key",
            **stages(executor, normal, normal_root, "normal-task",
                     parse_writing(normal_root, OUTPUTS)),
        ), timeout=5))
        good = normal.get("normal-key")
        self.assertEqual(good.state, "completed")
        with self.assertRaises(native.TaskProtocolConflict):
            normal.storage_hold_preview("normal-key", runtime_identity_sha256=RUNTIME)
        self.assertEqual(normal.get("normal-key"), good)
        self.assertTrue(Path(good.result_path).is_file())

    def test_failed_precommit_persistence_keeps_original_held_record(self) -> None:
        lab, registry, retained = self.held()
        preview = registry.storage_hold_preview(KEY, runtime_identity_sha256=RUNTIME)
        exact_before = lab.lab.disk_bytes()
        with patch.object(registry, "_write_registry_stream", side_effect=OSError("synthetic precommit")):
            with self.assertRaises(native.TaskRegistryPersistenceError) as failed:
                registry.decide_storage_hold(KEY, **_decision(preview))
        self.assertEqual(failed.exception.outcome, "not_committed")
        self.assertEqual(lab.lab.disk_bytes(), exact_before)
        fresh = lab.open()
        observed = fresh.get(KEY)
        self.assertEqual((observed.state, observed.storage["wait_reason"]),
                         ("processing", "tree_integrity"))
        self.assertIsNone(observed.failure_cause)
        self.assertTrue(retained.is_file())
        self.assertFalse(any(path.name.endswith(".tmp") for path in lab.lab.registry_path.parent.iterdir()))

    def test_live_reader_count_defensively_refuses_a_held_decision(self) -> None:
        _, registry, retained = self.held()
        # Public result readers only arise after completion. A v4 record can
        # nevertheless carry a positive reader count, so exercise this exact
        # fail-closed guard without replacing the registry decision method.
        with registry._lock:
            registry._records[KEY].active_readers = 1
            registry._persist()
        preview = registry.storage_hold_preview(KEY, runtime_identity_sha256=RUNTIME)
        with self.assertRaises(native.TaskProtocolConflict):
            registry.decide_storage_hold(KEY, **_decision(preview))
        observed = registry.get(KEY)
        self.assertEqual((observed.state, observed.active_readers), ("processing", 1))
        self.assertTrue(retained.is_file())


class NativeHoldDecisionGeneratedBoundaryIndependentTests(unittest.IsolatedAsyncioTestCase):
    async def held_route(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-hold-generated-independent-")
        self.addCleanup(temporary.cleanup)
        fixture = ResultStorageApiFixture(
            Path(temporary.name), outputs={"{name}.md": b"#\n", "images/huge.png": b"\0" * (2 * MIB)},
            single_limit=2 * MIB,
        )
        self.addCleanup(fixture.close)
        self.addAsyncCleanup(fixture.dispose_test_tasks)
        await fixture.manager.start()
        task = await fixture.create(fixture.options("held"))

        async def wait_hold() -> None:
            while True:
                current = fixture.manager.task_protocol_v2.get_by_task_id(task.task_id)
                if current.storage and current.storage["wait_reason"] == "hard_envelope_exceeded":
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_hold(), timeout=5)
        routes = [node for node in ast.parse(fixture.generated).body
                  if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "agent_storage_hold_decision"]
        self.assertEqual(len(routes), 1)
        route_node = routes[0]
        route_node.decorator_list = []
        namespace = {
            "get_task_manager": lambda: fixture.manager,
            "HTTPException": BoundaryHTTPException,
        }
        exec(compile(ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            route_node,
        ], type_ignores=[])), "<actual-generated-hold-route>", "exec"), namespace)
        return fixture, task, namespace["agent_storage_hold_decision"]

    async def test_generated_manager_refuses_inflight_and_wrong_task_before_decision(self) -> None:
        fixture, task, route = await self.held_route()
        fixture.enroll_operator(OPERATOR_CREDENTIAL)
        preview_request = HoldRouteRequest.json(
            {"mode": "preview"}, credential=OPERATOR_CREDENTIAL,
        )
        preview = await route(task.task_id, preview_request)
        self.assertEqual(preview_request.read, len(preview_request._body))
        request = {
            "mode": "execute", "expected_preview_sha256": preview["preview_sha256"],
            "decided_by": "synthetic-operator", "reason": "declared bound remains",
            "fixed_by": "no repair; terminal decision",
        }
        with self.assertRaises(BoundaryHTTPException) as missing:
            await route("another-task", HoldRouteRequest.json(
                request, credential=OPERATOR_CREDENTIAL,
            ))
        self.assertEqual(missing.exception.status_code, 404)
        fixture.manager._scheduled_task_ids.add(task.task_id)
        try:
            with self.assertRaises(BoundaryHTTPException) as busy:
                await route(task.task_id, HoldRouteRequest.json(
                    request, credential=OPERATOR_CREDENTIAL,
                ))
            self.assertEqual(busy.exception.status_code, 409)
        finally:
            fixture.manager._scheduled_task_ids.discard(task.task_id)
        self.assertEqual(fixture.manager.task_protocol_v2.get_by_task_id(task.task_id).state, "processing")
        self.assertTrue(Path(task.output_dir).is_dir())
        decision = await route(task.task_id, HoldRouteRequest.json(
            request, credential=OPERATOR_CREDENTIAL,
        ))
        self.assertEqual(
            (decision["schema"], decision["task_id"], decision["preview_sha256"],
             decision["state"], decision["replayed"]),
            (native.STORAGE_HOLD_RECEIPT_SCHEMA, task.task_id,
             preview["preview_sha256"], "failed", False),
        )
        self.assertEqual(decision["idempotency_key"], preview["idempotency_key"])
        self.assertEqual(decision["attempt_identity"], preview["attempt_identity"])
        self.assertEqual(decision["fence_identity"], preview["fence_identity"])

    async def test_operator_gate_precedes_body_and_registry_and_stream_is_bounded(self) -> None:
        fixture, task, route = await self.held_route()
        registry = fixture.manager.task_protocol_v2
        held = registry.get_by_task_id(task.task_id)
        exact_registry = registry._path.read_bytes()

        # The route is disabled before enrollment, even for a syntactically
        # valid bearer. Routine worker access to this API has no operator token.
        disabled = HoldRouteRequest.json({"mode": "preview"}, credential=OPERATOR_CREDENTIAL)
        with patch.object(native, "STORAGE_HOLD_OPERATOR_PATH", fixture.root / "missing-operator" / "hold.json"):
            with self.assertRaises(BoundaryHTTPException) as refused:
                await route(task.task_id, disabled)
        self.assertEqual((refused.exception.status_code, refused.exception.detail["code"]),
                         (403, "storage_hold_operator_disabled"))
        self.assertEqual(disabled.read, 0)

        fixture.enroll_operator(OPERATOR_CREDENTIAL)
        for request in (
            HoldRouteRequest.json({"mode": "preview"}),
            HoldRouteRequest.json({"mode": "preview"}, credential=OPERATOR_CREDENTIAL + "wrong"),
        ):
            with self.subTest(headers=request.headers):
                with self.assertRaises(BoundaryHTTPException) as unauthorized:
                    await route(task.task_id, request)
                self.assertEqual((unauthorized.exception.status_code,
                                  unauthorized.exception.detail["code"]),
                                 (401, "storage_hold_operator_unauthorized"))
                self.assertEqual(request.read, 0)
        self.assertEqual(registry.get_by_task_id(task.task_id), held)
        self.assertEqual(registry._path.read_bytes(), exact_registry)

        declared = HoldRouteRequest.json(
            {"mode": "preview"}, credential=OPERATOR_CREDENTIAL,
        )
        declared.headers["content-length"] = "16385"
        with self.assertRaises(BoundaryHTTPException) as too_large:
            await route(task.task_id, declared)
        self.assertEqual((too_large.exception.status_code, too_large.exception.detail["code"]),
                         (413, "storage_hold_request_too_large"))
        self.assertEqual(declared.read, 0)

        streamed = HoldRouteRequest(
            b" " * 16385,
            headers={"Authorization": f"Bearer {OPERATOR_CREDENTIAL}"}, chunk=4096,
        )
        with self.assertRaises(BoundaryHTTPException) as too_large:
            await route(task.task_id, streamed)
        self.assertEqual((too_large.exception.status_code, too_large.exception.detail["code"]),
                         (413, "storage_hold_request_too_large"))
        self.assertEqual(streamed.read, 16385)
        self.assertEqual(registry.get_by_task_id(task.task_id), held)
        self.assertEqual(registry._path.read_bytes(), exact_registry)

        at_limit = HoldRouteRequest(
            b'{"mode":"preview"}' + b" " * (16384 - len(b'{"mode":"preview"}')),
            headers={"Authorization": f"Bearer {OPERATOR_CREDENTIAL}",
                     "Content-Length": "16384"}, chunk=4096,
        )
        preview = await route(task.task_id, at_limit)
        self.assertEqual((at_limit.read, preview["schema"], preview["task_id"]),
                         (16384, native.STORAGE_HOLD_PREVIEW_SCHEMA, task.task_id))
        self.assertEqual(registry.get_by_task_id(task.task_id), held)
        self.assertEqual(registry._path.read_bytes(), exact_registry)


if __name__ == "__main__":
    unittest.main()
