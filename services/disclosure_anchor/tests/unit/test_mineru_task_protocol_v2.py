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

    @staticmethod
    def _lifecycle_key(index: int, now: float) -> str:
        bucket = int(now)
        digest = hashlib.sha256(f"task-key-{index}".encode()).hexdigest()
        return f"{bucket:x}.{digest}"

    def test_configured_output_root_rejects_task_from_other_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_root = root / "owned"
            output_root.mkdir()
            other = root / "other" / "task-key"
            uploads = other / "uploads"
            uploads.mkdir(parents=True)
            upload = uploads / "input.pdf"
            upload.write_bytes(b"pdf")
            registry = DurableTaskRegistry(
                root / "registry.json",
                max_unacked_result_bytes=100,
                output_root=output_root,
            )
            self._create(registry)
            with self.assertRaisesRegex(TaskProtocolConflict, "escaped"):
                registry.bind_task_payload(
                    "key",
                    {
                        "task_id": "task-key",
                        "output_dir": str(other),
                        "uploads": [str(upload)],
                    },
                )

    def test_registry_restart_rejects_replaced_configured_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_root = root / "owned"
            output_root.mkdir()
            registry_path = root / "registry.json"
            registry = DurableTaskRegistry(
                registry_path,
                max_unacked_result_bytes=100,
                output_root=output_root,
            )
            self._create(registry)
            output_root.rename(root / "replaced-owned")
            output_root.mkdir()
            with self.assertRaisesRegex(TaskProtocolConflict, "identity drifted"):
                DurableTaskRegistry(
                    registry_path,
                    max_unacked_result_bytes=100,
                    output_root=output_root,
                )

    def test_bounded_tombstone_lifecycle_allows_more_than_128_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = [1_000_000.0]
            registry_path = root / "registry.json"
            registry = DurableTaskRegistry(
                registry_path,
                max_unacked_result_bytes=1024,
                output_root=root,
                tombstone_retention_seconds=7200,
                enforce_key_lifecycle=True,
                clock=lambda: now[0],
            )
            first_key = self._lifecycle_key(0, now[0])
            for index in range(130):
                key = self._lifecycle_key(index, now[0])
                task_id = f"task-{index}"
                registry.reconcile_or_create(
                    idempotency_key=key,
                    task_id=task_id,
                    attempt_identity="attempt",
                    fence_identity="fence",
                )
                task_root = root / task_id
                uploads = task_root / "uploads"
                uploads.mkdir(parents=True)
                upload = uploads / "input.pdf"
                upload.write_bytes(b"pdf")
                registry.bind_task_payload(
                    key,
                    {
                        "task_id": task_id,
                        "output_dir": str(task_root),
                        "uploads": [str(upload)],
                    },
                )
                registry.transition(key, "processing")
                registry.transition(key, "finalizing")
                registry.reserve_finalizer(key, byte_budget=1)
                result = task_root / ".retained-result.zip"
                result.write_bytes(b"x")
                digest = hashlib.sha256(b"x").hexdigest()
                owner = hashlib.sha256(
                    f"{task_id}\0{digest}\0{1}".encode()
                ).hexdigest()
                registry.complete(
                    key,
                    result_path=result,
                    result_sha256=digest,
                    result_bytes=1,
                    result_owner=owner,
                )
                registry.acknowledge(key)
                self.assertEqual(registry.cleanup_consumed(), 1)

            restarted = DurableTaskRegistry(
                registry_path,
                max_unacked_result_bytes=1024,
                output_root=root,
                tombstone_retention_seconds=7200,
                enforce_key_lifecycle=True,
                clock=lambda: now[0],
            )
            replay, created = restarted.reconcile_or_create(
                idempotency_key=first_key,
                task_id="ignored",
                attempt_identity="attempt",
                fence_identity="fence",
            )
            self.assertFalse(created)
            self.assertEqual(replay.state, "consumed")
            with self.assertRaisesRegex(TaskProtocolConflict, "different attempt"):
                restarted.reconcile_or_create(
                    idempotency_key=first_key,
                    task_id="ignored",
                    attempt_identity="changed",
                    fence_identity="fence",
                )
            now[0] += 10800
            fresh_key = self._lifecycle_key(131, now[0])
            _, created = restarted.reconcile_or_create(
                idempotency_key=fresh_key,
                task_id="task-131",
                attempt_identity="attempt",
                fence_identity="fence",
            )
            self.assertTrue(created)
            with self.assertRaisesRegex(TaskProtocolConflict, "expired"):
                restarted.reconcile_or_create(
                    idempotency_key=first_key,
                    task_id="new",
                    attempt_identity="attempt",
                    fence_identity="fence",
                )

    def test_failed_terminal_ack_allows_more_than_128_tasks_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = [1_000_000.0]
            registry_path = root / "registry.json"
            registry = DurableTaskRegistry(
                registry_path,
                max_unacked_result_bytes=100,
                output_root=root,
                tombstone_retention_seconds=7200,
                enforce_key_lifecycle=True,
                clock=lambda: now[0],
            )
            first_key = self._lifecycle_key(0, now[0])
            for index in range(130):
                key = self._lifecycle_key(index, now[0])
                registry.reconcile_or_create(
                    idempotency_key=key,
                    task_id=f"failed-{index}",
                    attempt_identity="attempt",
                    fence_identity="fence",
                )
                registry.fail(key, error="visible")
                registry.acknowledge_failed(key)
            restarted = DurableTaskRegistry(
                registry_path,
                max_unacked_result_bytes=100,
                output_root=root,
                tombstone_retention_seconds=7200,
                enforce_key_lifecycle=True,
                clock=lambda: now[0],
            )
            record, created = restarted.reconcile_or_create(
                idempotency_key=first_key,
                task_id="ignored",
                attempt_identity="attempt",
                fence_identity="fence",
            )
            self.assertFalse(created)
            self.assertEqual(record.state, "consumed")
            with self.assertRaisesRegex(TaskProtocolConflict, "different attempt"):
                restarted.reconcile_or_create(
                    idempotency_key=first_key,
                    task_id="ignored",
                    attempt_identity="changed",
                    fence_identity="fence",
                )

    def test_submission_epoch_skew_and_server_clock_rollback_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = [1_000_000.0]
            registry = DurableTaskRegistry(
                root / "registry.json",
                max_unacked_result_bytes=100,
                output_root=root,
                tombstone_retention_seconds=7200,
                enforce_key_lifecycle=True,
                clock=lambda: now[0],
            )
            for index, epoch in enumerate((now[0] - 300, now[0] + 300)):
                registry.reconcile_or_create(
                    idempotency_key=self._lifecycle_key(index, epoch),
                    task_id=f"skew-{index}",
                    attempt_identity="attempt",
                    fence_identity="fence",
                )
            now[0] += 1000
            registry.reconcile_or_create(
                idempotency_key=self._lifecycle_key(3, now[0]),
                task_id="forward",
                attempt_identity="attempt",
                fence_identity="fence",
            )
            now[0] -= 301
            with self.assertRaisesRegex(TaskProtocolConflict, "server clock rolled back"):
                registry.reconcile_or_create(
                    idempotency_key=self._lifecycle_key(4, now[0]),
                    task_id="rollback",
                    attempt_identity="attempt",
                    fence_identity="fence",
                )

    def test_reconcile_prune_watermark_and_record_are_one_atomic_persist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = [1_000_000.0]
            registry_path = root / "registry.json"
            registry = DurableTaskRegistry(
                registry_path,
                max_unacked_result_bytes=100,
                output_root=root,
                tombstone_retention_seconds=3600,
                enforce_key_lifecycle=True,
                clock=lambda: now[0],
            )
            old_key = self._lifecycle_key(1, now[0])
            registry.reconcile_or_create(
                idempotency_key=old_key,
                task_id="old-task",
                attempt_identity="attempt",
                fence_identity="fence",
            )
            registry.fail(old_key, error="durable failure")
            registry.acknowledge_failed(old_key)
            old_watermark = registry._submission_watermark_bucket
            now[0] += 3601
            new_key = self._lifecycle_key(2, now[0])
            persisted = registry._persist
            registry._persist = lambda: (_ for _ in ()).throw(
                OSError("synthetic persist crash")
            )
            with self.assertRaisesRegex(OSError, "synthetic persist crash"):
                registry.reconcile_or_create(
                    idempotency_key=new_key,
                    task_id="new-task",
                    attempt_identity="attempt",
                    fence_identity="fence",
                )
            self.assertIsNotNone(registry.get(old_key))
            self.assertIsNone(registry.get(new_key))
            self.assertEqual(registry._submission_watermark_bucket, old_watermark)
            restarted = DurableTaskRegistry(
                registry_path,
                max_unacked_result_bytes=100,
                output_root=root,
                tombstone_retention_seconds=3600,
                enforce_key_lifecycle=True,
                clock=lambda: now[0],
            )
            self.assertIsNotNone(restarted.get(old_key))
            self.assertIsNone(restarted.get(new_key))
            self.assertEqual(restarted._submission_watermark_bucket, old_watermark)

            registry._persist = persisted
            created_record, created = registry.reconcile_or_create(
                idempotency_key=new_key,
                task_id="new-task",
                attempt_identity="attempt",
                fence_identity="fence",
            )
            self.assertTrue(created)
            self.assertIsNone(registry.get(old_key))
            replay, created = registry.reconcile_or_create(
                idempotency_key=new_key,
                task_id="ignored",
                attempt_identity="attempt",
                fence_identity="fence",
            )
            self.assertFalse(created)
            self.assertEqual(replay.task_id, created_record.task_id)

    def test_cleanup_intent_resumes_after_unlink_then_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._create(registry)
            registry.transition("key", "processing")
            registry.transition("key", "finalizing")
            registry.reserve_finalizer("key", byte_budget=1)
            result = root / "result.zip"
            result.write_bytes(b"x")
            digest = hashlib.sha256(b"x").hexdigest()
            registry.complete(
                "key",
                result_path=result,
                result_sha256=digest,
                result_bytes=1,
                result_owner=hashlib.sha256(
                    f"task-key\0{digest}\0{1}".encode()
                ).hexdigest(),
            )
            registry.acknowledge("key")
            result.unlink()  # crash after unlink, before compact persistence
            restarted = self._registry(root)
            self.assertEqual(restarted.cleanup_consumed(Path.unlink), 1)
            self.assertEqual(restarted.get("key").state, "consumed")  # type: ignore[union-attr]

    def test_cleanup_persist_failure_keeps_retryable_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._create(registry)
            registry.transition("key", "processing")
            registry.transition("key", "finalizing")
            registry.reserve_finalizer("key", byte_budget=1)
            result = root / "result.zip"
            result.write_bytes(b"x")
            digest = hashlib.sha256(b"x").hexdigest()
            registry.complete(
                "key",
                result_path=result,
                result_sha256=digest,
                result_bytes=1,
                result_owner=hashlib.sha256(
                    f"task-key\0{digest}\0{1}".encode()
                ).hexdigest(),
            )
            registry.acknowledge("key")
            original_persist = registry._persist
            registry._persist = lambda: (_ for _ in ()).throw(OSError("persist"))
            with self.assertRaisesRegex(OSError, "persist"):
                registry.cleanup_consumed(Path.unlink)
            self.assertEqual(registry.get("key").state, "cleanup_pending")  # type: ignore[union-attr]
            registry._persist = original_persist
            self.assertEqual(registry.cleanup_consumed(Path.unlink), 1)

    def test_bound_task_tree_cleanup_resumes_after_persist_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._create(registry)
            task_root = root / "task-key"
            uploads = task_root / "uploads"
            uploads.mkdir(parents=True)
            upload = uploads / "input.pdf"
            upload.write_bytes(b"pdf")
            registry.bind_task_payload(
                "key",
                {
                    "task_id": "task-key",
                    "output_dir": str(task_root),
                    "uploads": [str(upload)],
                },
            )
            intermediate = task_root / "intermediate"
            intermediate.mkdir()
            (intermediate / "page.json").write_bytes(b"intermediate")
            registry.transition("key", "processing")
            registry.transition("key", "finalizing")
            registry.reserve_finalizer("key", byte_budget=1)
            result = task_root / ".retained-result.zip"
            result.write_bytes(b"x")
            digest = hashlib.sha256(b"x").hexdigest()
            registry.complete(
                "key",
                result_path=result,
                result_sha256=digest,
                result_bytes=1,
                result_owner=hashlib.sha256(
                    f"task-key\0{digest}\0{1}".encode()
                ).hexdigest(),
            )
            registry.acknowledge("key")
            original_persist = registry._persist
            registry._persist = lambda: (_ for _ in ()).throw(OSError("persist"))
            with self.assertRaisesRegex(OSError, "persist"):
                registry.cleanup_consumed()
            self.assertFalse(task_root.exists())
            registry._persist = original_persist

            restarted = self._registry(root)
            self.assertEqual(restarted.cleanup_consumed(), 1)
            record = restarted.get("key")
            self.assertIsNotNone(record)
            self.assertEqual(record.state, "consumed")  # type: ignore[union-attr]

    def test_cleanup_only_tolerates_missing_result_leaf(self) -> None:
        for mutation in ("root", "task", "uploads", "leaf"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                output_root = base / "output"
                output_root.mkdir()
                now = 1_000_000.0
                key = self._lifecycle_key(1, now)
                registry = DurableTaskRegistry(
                    base / "registry.json",
                    max_unacked_result_bytes=100,
                    output_root=output_root,
                    tombstone_retention_seconds=7200,
                    enforce_key_lifecycle=True,
                    clock=lambda: now,
                )
                task_root = output_root / "task-owned"
                uploads = task_root / "uploads"
                uploads.mkdir(parents=True)
                upload = uploads / "input.pdf"
                upload.write_bytes(b"pdf")
                registry.reconcile_or_create(
                    idempotency_key=key,
                    task_id="task-owned",
                    attempt_identity="attempt",
                    fence_identity="fence",
                )
                registry.bind_task_payload(
                    key,
                    {
                        "task_id": "task-owned",
                        "output_dir": str(task_root),
                        "uploads": [str(upload)],
                    },
                )
                registry.transition(key, "processing")
                registry.transition(key, "finalizing")
                registry.reserve_finalizer(key, byte_budget=1)
                result = task_root / ".retained-result.zip"
                result.write_bytes(b"x")
                digest = hashlib.sha256(b"x").hexdigest()
                registry.complete(
                    key,
                    result_path=result,
                    result_sha256=digest,
                    result_bytes=1,
                    result_owner=hashlib.sha256(
                        f"task-owned\0{digest}\0{1}".encode()
                    ).hexdigest(),
                )
                registry.acknowledge(key)
                if mutation == "root":
                    output_root.rename(base / "old-output")
                    output_root.mkdir()
                elif mutation == "task":
                    task_root.rename(output_root / "old-task")
                    (task_root / "uploads").mkdir(parents=True)
                elif mutation == "uploads":
                    uploads.rename(task_root / "old-uploads")
                    uploads.mkdir()
                else:
                    result.unlink()
                if mutation == "leaf":
                    self.assertEqual(registry.cleanup_consumed(), 1)
                else:
                    with self.assertRaises(
                        (FileNotFoundError, TaskProtocolConflict)
                    ):
                        registry.cleanup_consumed()
                    self.assertEqual(
                        registry.get(key).state, "cleanup_pending"  # type: ignore[union-attr]
                    )

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

    def test_completed_unacked_result_survives_beyond_legacy_600_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = [1_000_000.0]
            registry = DurableTaskRegistry(
                root / "registry.json",
                max_unacked_result_bytes=100,
                output_root=root,
                clock=lambda: now[0],
            )
            self._create(registry)
            registry.transition("key", "processing")
            registry.transition("key", "finalizing")
            registry.reserve_finalizer("key", byte_budget=1)
            result = root / "result.zip"
            result.write_bytes(b"x")
            registry.complete(
                "key",
                result_path=result,
                result_sha256=hashlib.sha256(b"x").hexdigest(),
                result_bytes=1,
                result_owner="b" * 64,
            )
            now[0] += 601
            self.assertEqual(registry.cleanup_consumed(), 0)
            self.assertTrue(result.exists())
            self.assertEqual(registry.get("key").state, "completed")  # type: ignore[union-attr]

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
            self.assertEqual(registry.get("key").state, "cleanup_pending")  # type: ignore[union-attr]
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
            with self.assertRaisesRegex(TaskProtocolConflict, "symlink"):
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

    def test_executor_failure_is_content_free_and_survives_restart(self) -> None:
        async def exercise(root: Path, registry: DurableTaskRegistry) -> None:
            executor = SplitTaskExecutor(parse_slots=1, finalizer_slots=1)

            async def parse() -> None:
                raise ValueError("private document text")

            async def finalize() -> tuple[Path, str, int, str]:
                raise AssertionError("finalizer must not run")

            with self.assertRaisesRegex(ValueError, "private document text"):
                await executor.run(
                    registry=registry,
                    key="key",
                    parse=parse,
                    finalize=finalize,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(root)
            self._create(registry)
            task_root = root / "task-key"
            uploads = task_root / "uploads"
            uploads.mkdir(parents=True)
            upload = uploads / "input.pdf"
            upload.write_bytes(b"pdf")
            registry.bind_task_payload(
                "key",
                {
                    "task_id": "task-key",
                    "output_dir": str(task_root),
                    "uploads": [str(upload)],
                },
            )
            asyncio.run(exercise(root, registry))
            failed = registry.get("key")
            self.assertIsNotNone(failed)
            assert failed is not None
            expected = (
                '{"code":"parse_or_finalize_failed","detail":"ValueError",'
                '"schema":"mineru-task-failure.v1"}'
            )
            self.assertEqual(failed.error, expected)
            self.assertNotIn("private document text", failed.error)

            recovered = self._registry(root).recoverable_payloads()[0]
            self.assertEqual(recovered["status"], "failed")
            self.assertEqual(recovered["error"], expected)

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

    def test_restart_rejects_renamed_and_replaced_task_directory(self) -> None:
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
            output.rename(root / "original-task-key")
            replacement_uploads = output / "uploads"
            replacement_uploads.mkdir(parents=True)
            (replacement_uploads / "input.pdf").write_bytes(b"pdf")
            with self.assertRaisesRegex(TaskProtocolConflict, "directory identity"):
                self._registry(root).recoverable_payloads()


if __name__ == "__main__":
    unittest.main()
