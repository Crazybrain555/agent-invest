from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from disclosure_anchor.adapters.runtime.mineru_task_protocol_v2 import (
    DurableTaskRegistry,
    SplitTaskExecutor,
    TaskProtocolConflict,
)


class MinerUTaskProtocolV2Tests(unittest.TestCase):
    def _registry(self, root: Path, *, limit: int = 100) -> DurableTaskRegistry:
        return DurableTaskRegistry(root / "registry.json", max_unacked_result_bytes=limit)

    def _create(self, registry: DurableTaskRegistry, key: str = "key") -> None:
        record, created = registry.reconcile_or_create(idempotency_key=key, task_id=f"task-{key}", attempt_identity="attempt", fence_identity="fence")
        self.assertTrue(created)
        self.assertEqual(record.state, "pending")

    def test_idempotency_reconciles_exact_and_rejects_conflict_before_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            self._create(registry)
            same, created = registry.reconcile_or_create(idempotency_key="key", task_id="ignored", attempt_identity="attempt", fence_identity="fence")
            self.assertFalse(created)
            self.assertEqual(same.task_id, "task-key")
            with self.assertRaisesRegex(TaskProtocolConflict, "different attempt/fence"):
                registry.reconcile_or_create(idempotency_key="key", task_id="new", attempt_identity="other", fence_identity="fence")

    def test_registry_recovers_completed_result_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._create(registry)
            registry.transition("key", "processing")
            registry.transition("key", "finalizing")
            registry.complete("key", result_path=root / "result.zip", result_sha256="a" * 64, result_bytes=12, result_owner="b" * 64)
            recovered = self._registry(root).get("key")
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.state, "completed")
            self.assertEqual(recovered.result_bytes, 12)

    def test_lease_reader_blocks_ack_and_cleanup_until_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._create(registry)
            registry.transition("key", "processing")
            registry.transition("key", "finalizing")
            result = root / "result.zip"
            result.write_bytes(b"result")
            registry.complete("key", result_path=result, result_sha256="a" * 64, result_bytes=6, result_owner="b" * 64)
            registry.lease("key", seconds=10)
            with registry.open_result("key") as opened:
                self.assertEqual(opened, result)
                with self.assertRaisesRegex(TaskProtocolConflict, "in use"):
                    registry.acknowledge("key")
            registry.acknowledge("key")
            self.assertEqual(registry.cleanup_consumed(Path.unlink), 1)
            self.assertFalse(result.exists())

    def test_unacked_result_bytes_apply_backpressure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root, limit=10)
            for key in ("a", "b"):
                self._create(registry, key)
                registry.transition(key, "processing")
                registry.transition(key, "finalizing")
            registry.complete("a", result_path=root / "a", result_sha256="a" * 64, result_bytes=7, result_owner="b" * 64)
            with self.assertRaisesRegex(TaskProtocolConflict, "capacity exhausted"):
                registry.complete("b", result_path=root / "b", result_sha256="c" * 64, result_bytes=4, result_owner="d" * 64)

    def test_parse_credit_is_released_while_previous_finalizer_waits(self) -> None:
        async def exercise() -> None:
            with tempfile.TemporaryDirectory() as directory:
                registry = self._registry(Path(directory), limit=100)
                self._create(registry, "a")
                self._create(registry, "b")
                executor = SplitTaskExecutor(parse_slots=1, finalizer_slots=1)
                finalizer_release = asyncio.Event()
                second_parse_started = asyncio.Event()

                async def parse_a() -> None:
                    return None

                async def finalize_a() -> tuple[Path, str, int, str]:
                    await finalizer_release.wait()
                    return Path(directory) / "a", "a" * 64, 1, "b" * 64

                async def parse_b() -> None:
                    second_parse_started.set()

                async def finalize_b() -> tuple[Path, str, int, str]:
                    return Path(directory) / "b", "c" * 64, 1, "d" * 64

                first = asyncio.create_task(executor.run(registry=registry, key="a", parse=parse_a, finalize=finalize_a))
                while registry.get("a").state != "finalizing":  # type: ignore[union-attr]
                    await asyncio.sleep(0)
                second = asyncio.create_task(executor.run(registry=registry, key="b", parse=parse_b, finalize=finalize_b))
                await asyncio.wait_for(second_parse_started.wait(), timeout=1)
                self.assertFalse(first.done())
                finalizer_release.set()
                await asyncio.gather(first, second)

        asyncio.run(exercise())

    def test_restart_recovers_route_payload_and_requeues_interrupted_parse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._create(registry)
            registry.bind_task_payload(
                "key",
                {"task_id": "task-key", "output_dir": "task-key"},
            )
            registry.transition("key", "processing")
            recovered_registry = self._registry(root)
            payloads = recovered_registry.recoverable_payloads()
            self.assertEqual(payloads[0]["task_id"], "task-key")
            record = recovered_registry.get("key")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.state, "pending")
            by_task = recovered_registry.get_by_task_id("task-key")
            self.assertIsNotNone(by_task)
            assert by_task is not None
            self.assertEqual(by_task.idempotency_key, "key")


if __name__ == "__main__":
    unittest.main()
