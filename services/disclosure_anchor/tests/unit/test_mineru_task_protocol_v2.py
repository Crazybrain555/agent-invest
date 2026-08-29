from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "scripts/windows/mineru_heap_trim_compat/agent_task_protocol_v2.py"
)
_SPEC = importlib.util.spec_from_file_location("agent_task_protocol_v2", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
DurableTaskRegistry = _MODULE.DurableTaskRegistry
SplitTaskExecutor = _MODULE.SplitTaskExecutor
TaskProtocolConflict = _MODULE.TaskProtocolConflict


class MinerUTaskProtocolV2Tests(unittest.TestCase):
    def _registry(self, root: Path, *, limit: int = 100) -> DurableTaskRegistry:
        return DurableTaskRegistry(
            root / "registry.json", max_unacked_result_bytes=limit
        )

    def _create(self, registry: DurableTaskRegistry, key: str = "key") -> None:
        record, created = registry.reconcile_or_create(
            idempotency_key=key,
            task_id=f"task-{key}",
            attempt_identity="attempt",
            fence_identity="fence",
        )
        self.assertTrue(created)
        self.assertEqual(record.state, "pending")

    def test_idempotency_reconciles_exact_and_rejects_conflict_before_allocation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = self._registry(Path(directory))
            self._create(registry)
            same, created = registry.reconcile_or_create(
                idempotency_key="key",
                task_id="ignored",
                attempt_identity="attempt",
                fence_identity="fence",
            )
            self.assertFalse(created)
            self.assertEqual(same.task_id, "task-key")
            with self.assertRaisesRegex(
                TaskProtocolConflict, "different attempt/fence"
            ):
                registry.reconcile_or_create(
                    idempotency_key="key",
                    task_id="new",
                    attempt_identity="other",
                    fence_identity="fence",
                )

    def test_registry_recovers_completed_result_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._create(registry)
            output = root / "task-key"
            uploads = output / "uploads"
            uploads.mkdir(parents=True)
            upload = uploads / "input.pdf"
            upload.write_bytes(b"pdf")
            registry.bind_task_payload(
                "key",
                {
                    "task_id": "task-key",
                    "output_dir": str(output),
                    "uploads": [str(upload)],
                },
            )
            registry.transition("key", "processing")
            registry.transition("key", "finalizing")
            registry.reserve_finalizer("key", byte_budget=12)
            result = root / "result.zip"
            result.write_bytes(b"result-bytes")
            result_sha256 = hashlib.sha256(b"result-bytes").hexdigest()
            result_owner = hashlib.sha256(
                f"task-key\0{result_sha256}\0{len(b'result-bytes')}".encode()
            ).hexdigest()
            registry.complete(
                "key",
                result_path=result,
                result_sha256=result_sha256,
                result_bytes=len(b"result-bytes"),
                result_owner=result_owner,
            )
            recovered = self._registry(root).get("key")
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.state, "completed")
            self.assertEqual(recovered.result_bytes, 12)
            payload = self._registry(root).recoverable_payloads()[0]
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["result_artifact_sha256"], result_sha256)

    def test_lease_reader_blocks_ack_and_cleanup_until_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._create(registry)
            registry.transition("key", "processing")
            registry.transition("key", "finalizing")
            registry.reserve_finalizer("key", byte_budget=6)
            result = root / "result.zip"
            result.write_bytes(b"result")
            registry.complete(
                "key",
                result_path=result,
                result_sha256="a" * 64,
                result_bytes=6,
                result_owner="b" * 64,
            )
            registry.lease("key", seconds=10)
            with registry.open_result("key") as opened:
                self.assertEqual(opened, result)
                with self.assertRaisesRegex(TaskProtocolConflict, "in use"):
                    registry.acknowledge("key")
            registry.acknowledge("key")
            self.assertEqual(registry.cleanup_consumed(Path.unlink), 1)
            self.assertFalse(result.exists())

    def test_expired_lease_fails_closed_without_consuming_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._create(registry)
            registry.transition("key", "processing")
            registry.transition("key", "finalizing")
            registry.reserve_finalizer("key", byte_budget=6)
            result = root / "result.zip"
            result.write_bytes(b"result")
            registry.complete(
                "key",
                result_path=result,
                result_sha256="a" * 64,
                result_bytes=6,
                result_owner="b" * 64,
            )
            registry.lease("key", seconds=0.001)
            time.sleep(0.005)
            with self.assertRaisesRegex(TaskProtocolConflict, "expired"):
                registry.acquire_result("key")
            record = registry.get("key")
            self.assertIsNotNone(record)
            self.assertEqual(record.state, "completed")

    def test_ack_is_idempotent_and_cleanup_failure_keeps_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._create(registry)
            registry.transition("key", "processing")
            registry.transition("key", "finalizing")
            registry.reserve_finalizer("key", byte_budget=6)
            result = root / "result.zip"
            result.write_bytes(b"result")
            registry.complete(
                "key",
                result_path=result,
                result_sha256="a" * 64,
                result_bytes=6,
                result_owner="b" * 64,
            )
            registry.acknowledge("key")
            registry.acknowledge("key")
            with self.assertRaisesRegex(OSError, "blocked"):
                registry.cleanup_consumed(
                    lambda _path: (_ for _ in ()).throw(OSError("blocked"))
                )
            self.assertEqual(registry.get("key").state, "consumed")  # type: ignore[union-attr]
            self.assertEqual(registry.cleanup_consumed(Path.unlink), 1)
            self.assertIsNotNone(registry.get("key"))

    def test_registry_rejects_unsafe_mode_and_duplicate_json_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "registry.json"
            path.write_text(
                '{"schema":"mineru-task-registry.v2","schema":"x","records":[]}'
            )
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(TaskProtocolConflict, "duplicate"):
                self._registry(root)
            path.write_text('{"schema":"mineru-task-registry.v2","records":[]}')
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(TaskProtocolConflict, "unsafe"):
                self._registry(root)

    def test_registry_open_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text('{"schema":"mineru-task-registry.v2","records":[]}')
            target.chmod(0o600)
            (root / "registry.json").symlink_to(target)
            with self.assertRaises(OSError):
                self._registry(root)

    def test_unacked_result_bytes_apply_backpressure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root, limit=10)
            for key in ("a", "b"):
                self._create(registry, key)
                registry.transition(key, "processing")
                registry.transition(key, "finalizing")
            registry.reserve_finalizer("a", byte_budget=7)
            registry.complete(
                "a",
                result_path=root / "a",
                result_sha256="a" * 64,
                result_bytes=7,
                result_owner="b" * 64,
            )
            with self.assertRaisesRegex(TaskProtocolConflict, "capacity exhausted"):
                registry.reserve_finalizer("b", byte_budget=4)

    def test_consumed_bytes_remain_charged_until_unlink_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root, limit=10)
            self._create(registry)
            registry.transition("key", "processing")
            registry.transition("key", "finalizing")
            registry.reserve_finalizer("key", byte_budget=6)
            result = root / "result.zip"
            result.write_bytes(b"result")
            registry.complete(
                "key", result_path=result, result_sha256="a" * 64,
                result_bytes=6, result_owner="b" * 64,
            )
            registry.acknowledge("key")
            self.assertEqual(registry.unacked_result_bytes, 6)
            registry.cleanup_consumed(Path.unlink)
            self.assertEqual(registry.unacked_result_bytes, 0)

    def test_restart_rejects_upload_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._create(registry)
            output = root / "task-key"
            uploads = output / "uploads"
            uploads.mkdir(parents=True)
            upload = uploads / "input.pdf"
            upload.write_bytes(b"abc")
            registry.bind_task_payload(
                "key", {"task_id": "task-key", "output_dir": str(output),
                        "uploads": [str(upload)]},
            )
            registry.transition("key", "processing")
            upload.write_bytes(b"xyz")
            with self.assertRaisesRegex(TaskProtocolConflict, "identity drifted"):
                self._registry(root).recoverable_payloads()

    def test_restart_cleanup_rejects_symlink_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._create(registry)
            output = root / "task-key"
            uploads = output / "uploads"
            uploads.mkdir(parents=True)
            upload = uploads / "input.pdf"
            upload.write_bytes(b"abc")
            registry.bind_task_payload(
                "key", {"task_id": "task-key", "output_dir": str(output),
                        "uploads": [str(upload)]},
            )
            registry.transition("key", "processing")
            (output / "escape").symlink_to(root)
            with self.assertRaisesRegex(TaskProtocolConflict, "escaped"):
                self._registry(root).recoverable_payloads()

    def test_parse_credit_is_released_while_previous_finalizer_waits(self) -> None:
        async def exercise() -> None:
            with tempfile.TemporaryDirectory() as directory:
                registry = self._registry(Path(directory), limit=100)
                self._create(registry, "a")
                self._create(registry, "b")
                executor = SplitTaskExecutor(
                    parse_slots=1, finalizer_slots=1, result_reservation_bytes=2
                )
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

                first = asyncio.create_task(
                    executor.run(
                        registry=registry, key="a", parse=parse_a, finalize=finalize_a
                    )
                )
                while registry.get("a").state != "finalizing":  # type: ignore[union-attr]
                    await asyncio.sleep(0)
                second = asyncio.create_task(
                    executor.run(
                        registry=registry, key="b", parse=parse_b, finalize=finalize_b
                    )
                )
                await asyncio.wait_for(second_parse_started.wait(), timeout=1)
                self.assertFalse(first.done())
                finalizer_release.set()
                await asyncio.gather(first, second)

        asyncio.run(exercise())

    def test_restart_recovers_route_payload_and_requeues_interrupted_parse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._create(registry)
            output = root / "task-key"
            uploads = output / "uploads"
            uploads.mkdir(parents=True)
            upload = uploads / "input.pdf"
            upload.write_bytes(b"pdf")
            registry.bind_task_payload(
                "key",
                {
                    "task_id": "task-key",
                    "output_dir": str(output),
                    "uploads": [str(upload)],
                },
            )
            partial = output / "partial"
            partial.mkdir()
            (partial / "stale").write_bytes(b"stale")
            registry.transition("key", "processing")
            recovered_registry = self._registry(root)
            payloads = recovered_registry.recoverable_payloads()
            self.assertEqual(payloads[0]["task_id"], "task-key")
            record = recovered_registry.get("key")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.state, "pending")
            self.assertEqual(record.recovery_generation, 2)
            self.assertFalse(partial.exists())
            by_task = recovered_registry.get_by_task_id("task-key")
            self.assertIsNotNone(by_task)
            assert by_task is not None
            self.assertEqual(by_task.idempotency_key, "key")


if __name__ == "__main__":
    unittest.main()
