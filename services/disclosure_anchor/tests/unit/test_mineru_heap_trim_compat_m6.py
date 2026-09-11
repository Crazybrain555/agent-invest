"""M6 additions for the MinerU 3.4.4 compatibility patcher: P1 persistence integration.

The baseline compatibility module retains its original cases with the installer
assertion updated for API-only upgrades. This module covers fail-closed transform
anchors, idempotency, HTTP 503/409 routing and failed-allocation cleanup. Waiter
and processor outcomes, cancellation and pinned-preimage guards are owned by
``test_mineru_task_registry_persistence.py`` rather than duplicated here. Endpoint
fragments run against a real disposable registry with a stubbed HTTP layer.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.windows.mineru_heap_trim_compat.agent_task_protocol_v2 import (
    TaskProtocolConflict,
    TaskRegistryPersistenceError,
)
from scripts.windows.mineru_heap_trim_compat.patch_mineru_344 import (
    _patch_registry_persistence_behavior,
    patch_source,
)
from tests._m6_fast_api_fixture import (
    FAST_API_PERSISTENCE_FIXTURE,
    HTTPExceptionStub,
    generate_fast_api_source,
    load_generated_fast_api,
)
from tests._m6_registry_lab import KEY, RegistryLab, drive, pre_commit_fault

IMPORT_BLOCK = (
    "from mineru.cli.agent_task_protocol_v2 import (\n"
    "    DurableTaskRegistry, SplitTaskExecutor, TaskProtocolConflict,\n"
    "    TaskRegistryPersistenceError,\n"
    "    evict_consumed_routes, task_protocol_runtime_status,\n"
    ")\n"
)
WAIT_FAILURE_MARKER = "self.task_wait_failures: dict[str, TaskRegistryPersistenceError]"


class GeneratedPersistenceIntegrationTests(unittest.TestCase):
    def test_persistence_transform_is_idempotent_and_fails_closed_on_drifted_anchors(self) -> None:
        generated = generate_fast_api_source()
        compile(generated, "generated-fast-api", "exec")
        self.assertEqual(_patch_registry_persistence_behavior(generated), generated)

        with self.subTest(drift="protocol_import_without_persistence_symbol"):
            without_symbol = FAST_API_PERSISTENCE_FIXTURE.replace("    TaskRegistryPersistenceError,\n", "")
            self.assertNotIn("TaskRegistryPersistenceError", without_symbol.split("_configured_max_concurrent_requests")[0])
            with self.assertRaisesRegex(RuntimeError, "import is absent"):
                patch_source("mineru/cli/fast_api.py", without_symbol)

        with self.subTest(drift="waiter_helper_block_changed"):
            drifted = FAST_API_PERSISTENCE_FIXTURE.replace(
                "        pending: set[asyncio.Task[Any]] = set()\n", ""
            )
            with self.assertRaisesRegex(RuntimeError, "anchor count drifted"):
                patch_source("mineru/cli/fast_api.py", drifted)

        with self.subTest(drift="pre_sleep_check_anchor_changed"):
            drifted = FAST_API_PERSISTENCE_FIXTURE.replace(
                "            return task\n\n        task_event = self.task_events.get(task_id)\n",
                "            return task\n        task_event = self.task_events.get(task_id)\n",
            )
            with self.assertRaisesRegex(RuntimeError, "anchor count drifted"):
                patch_source("mineru/cli/fast_api.py", drifted)

        with self.subTest(drift="reduced_fixture_without_protocol_import_is_left_alone"):
            reduced = FAST_API_PERSISTENCE_FIXTURE.replace(IMPORT_BLOCK, "")
            self.assertEqual(_patch_registry_persistence_behavior(reduced), reduced)
            generated = patch_source("mineru/cli/fast_api.py", reduced)
            self.assertIn("done, pending = await asyncio.wait", generated)
            self.assertNotIn("task_wait_failures", generated)
            self.assertIn("get_max_pending_tasks", generated)

        marker_line = f"        {WAIT_FAILURE_MARKER} = {{}}\n"
        with self.subTest(drift="worker_guard_cannot_be_placed"):
            source = (
                IMPORT_BLOCK
                + "class AsyncTaskManager:\n"
                + "    def __init__(self):\n"
                + marker_line
                + "    async def _process_task(self, task_id: str) -> None:\n"
                + "        try:\n            await self._run_task(task_id)\n"
                + "        except Exception as exc:\n            logger.exception(str(exc))\n"
            )
            with self.assertRaisesRegex(RuntimeError, "worker guard was not patched"):
                _patch_registry_persistence_behavior(source)

        with self.subTest(drift="waiter_guard_cannot_be_placed"):
            source = (
                IMPORT_BLOCK
                + "class AsyncTaskManager:\n"
                + "    def __init__(self):\n"
                + marker_line
                + "    async def wait_for_terminal_state(self, task_id: str) -> AsyncParseTask:\n"
                + "        return self.tasks[task_id]\n"
            )
            with self.assertRaisesRegex(RuntimeError, "waiter guard was not patched"):
                _patch_registry_persistence_behavior(source)

        with self.subTest(drift="cleanup_guard_cannot_be_placed"):
            source = (
                IMPORT_BLOCK
                + "class AsyncTaskManager:\n"
                + "    def __init__(self):\n"
                + marker_line
                + "async def create_task():\n"
                + "    try:\n        pass\n"
                + "    except Exception:\n"
                + "        with suppress(TaskProtocolConflict):\n"
                + "            registry.abandon_unbound(key)\n"
                + "        cleanup_file(task_output_dir)\n"
                + "        raise\n"
            )
            with self.assertRaisesRegex(RuntimeError, "cleanup guard was not patched"):
                _patch_registry_persistence_behavior(source)


class GeneratedEndpointBoundaryTests(unittest.TestCase):
    def lab(self) -> RegistryLab:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return RegistryLab(Path(temporary.name))

    def test_generated_http_boundary_maps_persistence_failures_to_503_and_conflicts_to_409(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "completed")
        namespace = load_generated_fast_api(registry=registry)
        lease = namespace["lease_task_fixture"]
        manager = SimpleNamespace(task_protocol_v2=registry)

        granted = asyncio.run(lease(manager, KEY, 60))
        self.assertIsInstance(granted["lease_until_unix"], float)
        self.assertEqual(registry.get(KEY).lease_until_unix, granted["lease_until_unix"])

        with pre_commit_fault(registry, "write"), self.assertRaises(HTTPExceptionStub) as unavailable:
            asyncio.run(lease(manager, KEY, 120))
        self.assertEqual(unavailable.exception.status_code, 503)
        self.assertIsInstance(unavailable.exception.__cause__, TaskRegistryPersistenceError)
        self.assertEqual(unavailable.exception.__cause__.outcome, "not_committed")
        self.assertIn("not_committed", unavailable.exception.detail)
        self.assertEqual(registry.get(KEY).lease_until_unix, granted["lease_until_unix"])
        self.assertEqual(lab.disk_records()[KEY]["lease_until_unix"], granted["lease_until_unix"])

        with self.assertRaises(HTTPExceptionStub) as conflict:
            asyncio.run(lease(manager, "unknown-key", 60))
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertIsInstance(conflict.exception.__cause__, TaskProtocolConflict)
        self.assertEqual(registry.persistence_status()["state"], "degraded")

    def test_generated_allocation_failure_deletes_input_only_after_durable_abandon(self) -> None:
        lab = self.lab()
        registry = lab.open()
        deleted: list[str] = []
        namespace = load_generated_fast_api(registry=registry, cleanup_file=deleted.append)
        allocate = namespace["allocate_task_fixture"]
        manager = SimpleNamespace(task_protocol_v2=registry)

        with self.subTest(case="no_protocol_record"):
            with self.assertRaises(HTTPExceptionStub):
                asyncio.run(allocate(manager, None, "/tmp/synthetic/no-record"))
            self.assertEqual(deleted, ["/tmp/synthetic/no-record"])
            deleted.clear()

        with self.subTest(case="durable_abandon_then_delete"):
            drive(lab, registry, "pending_unbound")
            record = registry.get(KEY)
            with self.assertRaises(HTTPExceptionStub):
                asyncio.run(allocate(manager, record, "/tmp/synthetic/abandoned"))
            self.assertEqual(deleted, ["/tmp/synthetic/abandoned"])
            self.assertIsNone(registry.get(KEY))
            self.assertNotIn(KEY, lab.disk_records())
            deleted.clear()

        with self.subTest(case="persistence_failure_preserves_input"):
            drive(lab, registry, "pending_unbound")
            record = registry.get(KEY)
            with pre_commit_fault(registry, "write"), self.assertRaises(TaskRegistryPersistenceError) as raised:
                asyncio.run(allocate(manager, record, "/tmp/synthetic/kept"))
            self.assertEqual(raised.exception.outcome, "not_committed")
            self.assertEqual(deleted, [])
            self.assertEqual(registry.get(KEY).state, "pending")
            self.assertEqual(lab.disk_records()[KEY]["state"], "pending")
            registry.abandon_unbound(KEY)

        with self.subTest(case="conflict_preserves_registry_owned_input"):
            drive(lab, registry, "bound")
            record = registry.get(KEY)
            with self.assertRaises(HTTPExceptionStub):
                asyncio.run(allocate(manager, record, "/tmp/synthetic/owned"))
            self.assertEqual(deleted, [])
            self.assertEqual(registry.get(KEY).state, "pending")
            self.assertIsNotNone(registry.get(KEY).task_payload)


if __name__ == "__main__":
    unittest.main()
