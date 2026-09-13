"""Known failed scheduling must not masquerade as an executable pending route."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol
from tests._mineru_admission_fixture import (
    UPLOAD_BYTES,
    AdmissionFixture,
    BoundaryHTTPException,
)


class KnownPendingFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_processing_commit_refusal_preserves_pending_but_get_and_reconcile_report_original_failure(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix="mineru-known-pending-") as temp:
            fx = AdmissionFixture(Path(temp))
            try:
                manager = fx.manager
                registry = manager.task_protocol_v2
                await manager.start()
                manager._schedule_changed.clear()
                original_flush = registry._flush_registry_stream
                storage_failure = OSError("injected transition flush refusal")
                fault_calls = []
                parsed = []

                async def parse_forbidden(**kwargs):
                    parsed.append(kwargs["request_options"].task_id)
                    self.fail(
                        "parse must not run when processing transition was not committed"
                    )

                fx.module.run_parse_job = parse_forbidden

                def fail_transition_flush(stream):
                    if registry._active_operation == "transition":
                        fault_calls.append(registry._active_operation)
                        raise storage_failure
                    return original_flush(stream)

                request = fx.options("pending-with-known-failure")
                with patch.object(
                    registry,
                    "_flush_registry_stream",
                    side_effect=fail_transition_flush,
                ):
                    task = await fx.create(request)
                    # The real generated completion callback sets this event.
                    await asyncio.wait_for(manager._schedule_changed.wait(), 1)
                self.assertEqual(fault_calls, ["transition"])
                self.assertEqual(parsed, [])
                self.assertEqual(manager._scheduled_task_ids, set())
                self.assertEqual(manager.active_tasks, set())
                self.assertIsNotNone(manager.last_worker_error)
                failure = manager.task_wait_failures[task.task_id]
                self.assertIsInstance(failure, protocol.TaskRegistryPersistenceError)
                self.assertEqual(
                    (
                        failure.operation,
                        failure.phase,
                        failure.outcome,
                        failure.committed,
                    ),
                    ("transition", "flush", "not_committed", False),
                )
                self.assertIs(failure.__cause__, storage_failure)
                retained = registry.get(request.agent_idempotency_key)
                self.assertEqual(retained.state, "pending")
                original_payload = retained.task_payload
                original_files = fx.files()
                self.assertIn(UPLOAD_BYTES, original_files.values())

                for surface in ("get", "same_key_reconcile"):
                    with self.subTest(surface=surface):
                        with self.assertRaises(BoundaryHTTPException) as caught:
                            if surface == "get":
                                manager.get(task.task_id)
                            else:
                                await fx.create(
                                    fx.options("pending-with-known-failure")
                                )
                        self.assertEqual(caught.exception.status_code, 503)
                        self.assertEqual(
                            caught.exception.detail,
                            {
                                "code": "accepted_recovery_required",
                                "task_id": task.task_id,
                                "accepted": True,
                                "phase": failure.phase,
                                "outcome": failure.outcome,
                                "committed": failure.committed,
                            },
                        )
                        self.assertIs(caught.exception.__cause__, failure)
                self.assertEqual(
                    registry.get(request.agent_idempotency_key).task_payload,
                    original_payload,
                )
                self.assertEqual(fx.files(), original_files)
                self.assertEqual(manager.queue.qsize(), 0)
            finally:
                await fx.dispose_test_tasks()
                fx.close()


if __name__ == "__main__":
    unittest.main()
