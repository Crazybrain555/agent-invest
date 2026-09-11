"""M6 P1 registry persistence, recovery, reader and cleanup regressions."""

from __future__ import annotations

import asyncio
import ast
import copy
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SERVICE_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    SERVICE_ROOT
    / "scripts"
    / "windows"
    / "mineru_heap_trim_compat"
    / "agent_task_protocol_v2.py"
)
SPEC = importlib.util.spec_from_file_location(
    "m6_p1_agent_task_protocol_v2", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
protocol = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = protocol
SPEC.loader.exec_module(protocol)


class RegistryPersistenceFaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="m6-p1-registry-persistence-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry_path = self.root / "task-registry.json"

    @staticmethod
    def registry_at(root: Path, *, limit: int = 4096):
        return protocol.DurableTaskRegistry(
            root / "task-registry.json",
            max_unacked_result_bytes=limit,
            output_root=root,
        )

    def registry(self, *, limit: int = 4096):
        return self.registry_at(self.root, limit=limit)

    @staticmethod
    def identities(suffix: str) -> dict[str, str]:
        return {
            "idempotency_key": f"idem-{suffix}",
            "task_id": f"task-{suffix}",
            "attempt_identity": f"attempt-{suffix}",
            "fence_identity": f"fence-{suffix}",
        }

    def create(self, registry, suffix: str = "a"):
        return registry.reconcile_or_create(**self.identities(suffix))

    @staticmethod
    def record_key(suffix: str) -> str:
        return f"idem-{suffix}"

    def bind(self, registry, root: Path, suffix: str = "a") -> dict[str, object]:
        task_root = root / f"task-{suffix}"
        uploads = task_root / "uploads"
        uploads.mkdir(parents=True)
        upload = uploads / "input.pdf"
        upload.write_bytes(f"pdf-{suffix}".encode())
        payload: dict[str, object] = {
            "task_id": f"task-{suffix}",
            "output_dir": str(task_root),
            "uploads": [str(upload)],
            "options": {
                "pages": [1, 2],
                "nested": {"mode": "strict", "flags": [True, False]},
            },
        }
        registry.bind_task_payload(self.record_key(suffix), payload)
        return payload

    def prepare_finalizing(
        self,
        registry,
        suffix: str = "a",
        *,
        byte_budget: int = 128,
    ) -> None:
        key = self.record_key(suffix)
        registry.transition(key, "processing")
        registry.transition(key, "finalizing")
        registry.reserve_finalizer(key, byte_budget=byte_budget)

    def complete(
        self,
        registry,
        root: Path,
        suffix: str = "a",
        *,
        data: bytes = b"durable-result",
        bound: bool = False,
    ) -> Path:
        self.create(registry, suffix)
        task_root = root / f"task-{suffix}"
        if bound:
            self.bind(registry, root, suffix)
            result = task_root / ".retained-result.zip"
        else:
            result = root / f"result-{suffix}.zip"
        self.prepare_finalizing(
            registry,
            suffix,
            byte_budget=max(128, len(data)),
        )
        result.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        owner = hashlib.sha256(
            f"task-{suffix}\0{digest}\0{len(data)}".encode()
        ).hexdigest()
        registry.complete(
            self.record_key(suffix),
            result_path=result,
            result_sha256=digest,
            result_bytes=len(data),
            result_owner=owner,
        )
        return result

    @staticmethod
    def disk_record(path: Path, key: str) -> dict[str, object]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return next(
            row for row in payload["records"] if row["idempotency_key"] == key
        )

    def test_direct_persist_override_rolls_back_every_mutator(self) -> None:
        cases = (
            "reconcile_or_create",
            "bind_task_payload",
            "transition",
            "reserve_finalizer",
            "complete",
            "fail",
            "lease",
            "acquire_result",
            "release_result",
            "acknowledge",
            "cleanup_consumed",
            "abandon_unbound",
            "acknowledge_failed",
            "recoverable_payloads",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(
                prefix=f"m6-p1-mutator-{case}-"
            ) as directory:
                root = Path(directory)
                registry = self.registry_at(root)
                cleanup_probe = None

                if case == "reconcile_or_create":
                    def operation():
                        return self.create(registry)
                elif case == "bind_task_payload":
                    self.create(registry)
                    task_root = root / "task-a"
                    uploads = task_root / "uploads"
                    uploads.mkdir(parents=True)
                    upload = uploads / "input.pdf"
                    upload.write_bytes(b"pdf")
                    payload = {
                        "task_id": "task-a",
                        "output_dir": str(task_root),
                        "uploads": [str(upload)],
                        "options": {"nested": {"value": 1}},
                    }

                    def operation():
                        return registry.bind_task_payload("idem-a", payload)
                elif case == "transition":
                    self.create(registry)

                    def operation():
                        return registry.transition("idem-a", "processing")
                elif case == "reserve_finalizer":
                    self.create(registry)
                    registry.transition("idem-a", "processing")
                    registry.transition("idem-a", "finalizing")

                    def operation():
                        return registry.reserve_finalizer(
                            "idem-a", byte_budget=64
                        )
                elif case == "complete":
                    self.create(registry)
                    self.prepare_finalizing(registry)
                    result = root / "result.zip"
                    data = b"result"
                    result.write_bytes(data)
                    digest = hashlib.sha256(data).hexdigest()
                    owner = hashlib.sha256(
                        f"task-a\0{digest}\0{len(data)}".encode()
                    ).hexdigest()

                    def operation():
                        return registry.complete(
                            "idem-a",
                            result_path=result,
                            result_sha256=digest,
                            result_bytes=len(data),
                            result_owner=owner,
                        )
                elif case == "fail":
                    self.create(registry)
                    registry.transition("idem-a", "processing")

                    def operation():
                        return registry.fail(
                            "idem-a", error='{"code":"synthetic"}'
                        )
                elif case == "lease":
                    self.complete(registry, root)

                    def operation():
                        return registry.lease("idem-a", seconds=60)
                elif case == "acquire_result":
                    self.complete(registry, root)
                    registry.lease("idem-a", seconds=60)

                    def operation():
                        return registry.acquire_result("idem-a")
                elif case == "release_result":
                    self.complete(registry, root)
                    registry.lease("idem-a", seconds=60)
                    registry.acquire_result("idem-a")

                    def operation():
                        return registry.release_result("idem-a")
                elif case == "acknowledge":
                    self.complete(registry, root)

                    def operation():
                        return registry.acknowledge("idem-a")
                elif case == "cleanup_consumed":
                    self.complete(registry, root)
                    registry.acknowledge("idem-a")
                    cleanup_probe = mock.patch.object(
                        registry, "_unlink_owned_result", return_value=None
                    )
                    cleanup_probe.start()
                    self.addCleanup(cleanup_probe.stop)
                    operation = registry.cleanup_consumed
                elif case == "abandon_unbound":
                    self.create(registry)

                    def operation():
                        return registry.abandon_unbound("idem-a")
                elif case == "acknowledge_failed":
                    self.create(registry)
                    registry.fail("idem-a", error='{"code":"synthetic"}')

                    def operation():
                        return registry.acknowledge_failed("idem-a")
                elif case == "recoverable_payloads":
                    self.create(registry)
                    self.bind(registry, root)
                    registry.transition("idem-a", "processing")
                    cleanup_probe = mock.patch.object(
                        registry, "_prepare_clean_replay"
                    )
                    cleanup_mock = cleanup_probe.start()
                    self.addCleanup(cleanup_probe.stop)
                    operation = registry.recoverable_payloads
                else:  # pragma: no cover - closed case table
                    raise AssertionError(case)

                before_records = copy.deepcopy(registry._records)
                before_watermark = registry._submission_watermark_bucket
                before_bytes = (
                    (root / "task-registry.json").read_bytes()
                    if (root / "task-registry.json").exists()
                    else None
                )
                injection_hits = 0

                def fail_before_write() -> None:
                    nonlocal injection_hits
                    injection_hits += 1
                    raise OSError(f"injected-{case}")

                with mock.patch.object(
                    registry, "_persist", side_effect=fail_before_write
                ):
                    with self.assertRaisesRegex(OSError, f"injected-{case}"):
                        operation()

                self.assertEqual(injection_hits, 1)
                self.assertEqual(registry._records, before_records)
                self.assertEqual(
                    registry._submission_watermark_bucket, before_watermark
                )
                observed_bytes = (
                    (root / "task-registry.json").read_bytes()
                    if (root / "task-registry.json").exists()
                    else None
                )
                self.assertEqual(observed_bytes, before_bytes)
                if case == "recoverable_payloads":
                    cleanup_mock.assert_not_called()
                if cleanup_probe is not None:
                    cleanup_probe.stop()
                    self._cleanups.pop()

    def test_write_flush_file_fsync_close_and_replace_fail_before_commit(self) -> None:
        stages = (
            "_write_registry_stream",
            "_flush_registry_stream",
            "_fsync_registry_file",
            "_close_registry_stream",
            "_replace_registry_file",
        )
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(
                prefix=f"m6-p1-stage-{stage}-"
            ) as directory:
                root = Path(directory)
                registry = self.registry_at(root)
                calls = 0
                original = getattr(registry, stage)

                def injected(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if stage == "_close_registry_stream" and calls > 1:
                        return original(*args, **kwargs)
                    raise OSError(f"injected-{stage}")

                with mock.patch.object(registry, stage, side_effect=injected):
                    with self.assertRaises(
                        protocol.TaskRegistryPersistenceError
                    ) as raised:
                        self.create(registry)
                self.assertGreaterEqual(calls, 1)
                self.assertEqual(raised.exception.outcome, "not_committed")
                self.assertFalse(raised.exception.committed)
                self.assertIsInstance(raised.exception.__cause__, OSError)
                self.assertFalse((root / "task-registry.json").exists())
                self.assertIsNone(registry.get("idem-a"))

    def test_short_write_is_not_committed(self) -> None:
        registry = self.registry()
        injection_hits = 0

        def short_write(stream, payload: bytes) -> None:
            nonlocal injection_hits
            injection_hits += 1
            stream.write(payload[: max(1, len(payload) // 2)])
            raise OSError("injected-short-write")

        with mock.patch.object(
            registry, "_write_registry_stream", side_effect=short_write
        ):
            with self.assertRaises(protocol.TaskRegistryPersistenceError) as raised:
                self.create(registry)
        self.assertEqual(injection_hits, 1)
        self.assertEqual(raised.exception.outcome, "not_committed")
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertFalse(self.registry_path.exists())

    def test_replace_then_raise_is_fsynced_and_committed(self) -> None:
        registry = self.registry()
        original = registry._replace_registry_file
        injection_hits = 0

        def replace_then_raise(source: Path, destination: Path) -> None:
            nonlocal injection_hits
            injection_hits += 1
            original(source, destination)
            raise OSError("injected-after-replace")

        with mock.patch.object(
            registry, "_replace_registry_file", side_effect=replace_then_raise
        ):
            record, created = self.create(registry)
        self.assertEqual(injection_hits, 1)
        self.assertTrue(created)
        self.assertEqual(record.idempotency_key, "idem-a")
        self.assertEqual(self.disk_record(self.registry_path, "idem-a")["task_id"], "task-a")
        status = registry.persistence_status()
        self.assertEqual(status["state"], "degraded")
        self.assertEqual(
            status["last_event"]["outcome"], "committed_after_recovery"
        )
        self.assertEqual(status["last_event"]["cause_type"], "OSError")

    def test_parent_fsync_transient_failure_commits_only_after_retry(self) -> None:
        registry = self.registry()
        original = registry._fsync_parent_descriptor
        calls = 0

        def fail_once(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("first-parent-fsync-failed")
            original(fd)

        with mock.patch.object(
            registry, "_fsync_parent_descriptor", side_effect=fail_once
        ):
            self.create(registry)
        self.assertEqual(calls, 2)
        status = registry.persistence_status()
        self.assertEqual(
            status["last_event"]["outcome"], "committed_after_recovery"
        )
        self.assertEqual(status["last_event"]["phase"], "parent_fsync_retry")

    def test_permanent_parent_fsync_failure_recovers_candidate_explicitly(self) -> None:
        registry = self.registry()
        calls = 0

        def fail_parent_fsync(_fd: int) -> None:
            nonlocal calls
            calls += 1
            raise OSError("permanent-parent-fsync-failure")

        with mock.patch.object(
            registry,
            "_fsync_parent_descriptor",
            side_effect=fail_parent_fsync,
        ):
            with self.assertRaises(
                protocol.TaskRegistryPersistenceError
            ) as raised:
                self.create(registry)
        self.assertEqual(calls, 2)
        self.assertEqual(raised.exception.outcome, "durability_uncertain")
        self.assertEqual(
            registry.persistence_status()["recovery_action"],
            "call recover_persistence_uncertainty",
        )
        with self.assertRaises(protocol.TaskRegistryPersistenceError):
            registry.get("idem-a")
        with self.assertRaises(protocol.TaskRegistryPersistenceError):
            self.create(registry, "b")

        recovered_status = registry.recover_persistence_uncertainty()
        self.assertEqual(recovered_status["state"], "degraded")
        self.assertEqual(
            recovered_status["last_event"]["outcome"],
            "committed_after_explicit_recovery",
        )
        self.assertEqual(
            recovered_status["last_event"]["operation"],
            "reconcile_or_create",
        )
        recovered = registry.get("idem-a")
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.attempt_identity, "attempt-a")
        registry.transition("idem-a", "processing")
        self.assertEqual(registry.persistence_status()["state"], "healthy")

    def test_explicit_recovery_can_select_previous_durable_bytes(self) -> None:
        registry = self.registry()
        self.create(registry, "a")
        replace_hits = 0
        fsync_hits = 0

        def fail_before_replace(_source: Path, _destination: Path) -> None:
            nonlocal replace_hits
            replace_hits += 1
            raise OSError("replace-call-failed-before-exchange")

        def fail_parent(_fd: int) -> None:
            nonlocal fsync_hits
            fsync_hits += 1
            raise OSError("cannot-establish-parent-durability")

        with mock.patch.object(
            registry, "_replace_registry_file", side_effect=fail_before_replace
        ), mock.patch.object(
            registry, "_fsync_parent_descriptor", side_effect=fail_parent
        ):
            with self.assertRaises(
                protocol.TaskRegistryPersistenceError
            ) as raised:
                self.create(registry, "b")
        self.assertEqual(replace_hits, 1)
        self.assertGreaterEqual(fsync_hits, 1)
        self.assertEqual(raised.exception.outcome, "durability_uncertain")

        status = registry.recover_persistence_uncertainty()
        self.assertEqual(
            status["last_event"]["outcome"],
            "not_committed_after_explicit_recovery",
        )
        self.assertIsNone(registry.get("idem-b"))
        record, created = self.create(registry, "b")
        self.assertTrue(created)
        self.assertEqual(record.task_id, "task-b")
        self.assertEqual(registry.persistence_status()["state"], "healthy")

    def test_post_commit_temp_cleanup_degrades_health_without_rollback(self) -> None:
        registry = self.registry()
        injection_hits = 0

        def fail_cleanup(_path: Path) -> None:
            nonlocal injection_hits
            injection_hits += 1
            raise OSError("post-commit-temp-cleanup")

        with mock.patch.object(
            registry, "_cleanup_temp_path", side_effect=fail_cleanup
        ):
            record, created = self.create(registry)
        self.assertTrue(created)
        self.assertEqual(record.task_id, "task-a")
        self.assertEqual(injection_hits, 1)
        status = registry.persistence_status()
        self.assertEqual(status["last_event"]["outcome"], "committed_cleanup_failed")
        self.assertEqual(
            status["recovery_action"], "do_not_retry_committed_operation"
        )
        with self.assertRaises(
            protocol.TaskRegistryPersistenceError
        ) as health_error:
            registry.assert_persistence_healthy()
        self.assertTrue(health_error.exception.committed)
        self.assertIsInstance(health_error.exception.__cause__, OSError)
        self.assertIsNotNone(registry.get("idem-a"))
        self.assertEqual(
            self.disk_record(self.registry_path, "idem-a")["task_id"], "task-a"
        )
        restarted = self.registry()
        self.assertIsNotNone(restarted.get("idem-a"))

    def test_post_commit_parent_close_degrades_health_without_rollback(self) -> None:
        registry = self.registry()
        original = registry._close_parent_descriptor
        injection_hits = 0

        def close_then_raise(fd: int) -> None:
            nonlocal injection_hits
            injection_hits += 1
            original(fd)
            raise OSError("post-commit-parent-close")

        with mock.patch.object(
            registry, "_close_parent_descriptor", side_effect=close_then_raise
        ):
            record, created = self.create(registry)
        self.assertTrue(created)
        self.assertEqual(record.task_id, "task-a")
        self.assertEqual(injection_hits, 1)
        self.assertIsNotNone(registry.get("idem-a"))
        with self.assertRaises(
            protocol.TaskRegistryPersistenceError
        ) as health_error:
            registry.assert_persistence_healthy()
        self.assertTrue(health_error.exception.committed)
        self.assertIsInstance(health_error.exception.__cause__, OSError)
        self.assertIsNotNone(self.registry().get("idem-a"))

    def test_mutable_observations_and_nested_payloads_are_detached(self) -> None:
        registry = self.registry()
        returned, created = self.create(registry)
        self.assertTrue(created)
        returned.state = "failed"
        self.assertEqual(registry.get("idem-a").state, "pending")

        original_payload = self.bind(registry, self.root)
        original_payload["options"]["nested"]["mode"] = "mutated"  # type: ignore[index]
        observed = registry.get("idem-a")
        assert observed is not None and observed.task_payload is not None
        self.assertEqual(
            observed.task_payload["options"]["nested"]["mode"], "strict"
        )
        observed.task_payload["options"]["nested"]["flags"].append("bad")
        observed.task_payload["_agent_protocol"]["uploads"][0]["sha256"] = "0" * 64
        by_task = registry.get_by_task_id("task-a")
        assert by_task is not None and by_task.task_payload is not None
        self.assertEqual(
            by_task.task_payload["options"]["nested"]["flags"], [True, False]
        )
        self.assertNotEqual(
            by_task.task_payload["_agent_protocol"]["uploads"][0]["sha256"],
            "0" * 64,
        )

        route = registry.recoverable_payloads()[0]
        self.assertNotIn("_agent_protocol", route)
        route["options"]["nested"]["mode"] = "route-mutated"
        again = registry.get("idem-a")
        assert again is not None and again.task_payload is not None
        self.assertEqual(
            again.task_payload["options"]["nested"]["mode"], "strict"
        )

    def test_recovery_persists_generation_before_filesystem_cleanup(self) -> None:
        registry = self.registry()
        self.create(registry)
        self.bind(registry, self.root)
        stale = self.root / "task-a" / "partial" / "stale.bin"
        stale.parent.mkdir()
        stale.write_bytes(b"stale")
        registry.transition("idem-a", "processing")
        before = self.registry_path.read_bytes()

        with mock.patch.object(
            registry, "_persist", side_effect=OSError("pre-cleanup-persist")
        ), mock.patch.object(registry, "_prepare_clean_replay") as cleanup:
            with self.assertRaisesRegex(OSError, "pre-cleanup-persist"):
                registry.recoverable_payloads()
        cleanup.assert_not_called()
        self.assertTrue(stale.exists())
        self.assertEqual(self.registry_path.read_bytes(), before)
        record = registry.get("idem-a")
        assert record is not None
        self.assertEqual(record.state, "processing")
        self.assertEqual(record.recovery_generation, 1)

        payload = registry.recoverable_payloads()[0]
        self.assertEqual(payload["status"], "pending")
        self.assertFalse(stale.exists())
        recovered = registry.get("idem-a")
        assert recovered is not None
        self.assertEqual(recovered.recovery_generation, 2)

    def test_live_reader_survives_unrelated_uncertainty_and_recovery(self) -> None:
        registry = self.registry()
        result = self.complete(registry, self.root)
        registry.lease("idem-a", seconds=60)
        self.assertEqual(registry.acquire_result("idem-a"), result)
        self.assertEqual(registry.get("idem-a").active_readers, 1)

        with mock.patch.object(
            registry,
            "_fsync_parent_descriptor",
            side_effect=OSError("unrelated-parent-fsync"),
        ):
            with self.assertRaises(protocol.TaskRegistryPersistenceError):
                self.create(registry, "b")
        with self.assertRaises(protocol.TaskRegistryPersistenceError):
            registry.get("idem-a")

        status = registry.recover_persistence_uncertainty()
        self.assertEqual(
            status["last_event"]["outcome"],
            "committed_after_explicit_recovery",
        )
        observed = registry.get("idem-a")
        assert observed is not None
        self.assertEqual(observed.active_readers, 1)
        with self.assertRaisesRegex(
            protocol.TaskProtocolConflict, "live result readers"
        ):
            registry.recoverable_payloads()
        with self.assertRaisesRegex(protocol.TaskProtocolConflict, "in use"):
            registry.acknowledge("idem-a")
        registry.release_result("idem-a")
        registry.acknowledge("idem-a")
        self.assertEqual(registry.get("idem-a").state, "cleanup_pending")
        self.assertTrue(result.exists())

    def test_reader_mutation_uncertainty_requires_cold_restart(self) -> None:
        registry = self.registry()
        result = self.complete(registry, self.root)
        registry.lease("idem-a", seconds=60)
        with mock.patch.object(
            registry,
            "_fsync_parent_descriptor",
            side_effect=OSError("reader-parent-fsync"),
        ):
            with self.assertRaises(
                protocol.TaskRegistryPersistenceError
            ) as raised:
                registry.acquire_result("idem-a")
        self.assertEqual(raised.exception.outcome, "durability_uncertain")
        self.assertEqual(
            registry.persistence_status()["recovery_action"],
            "restart_registry_process",
        )
        with self.assertRaises(
            protocol.TaskRegistryPersistenceError
        ) as recovery_error:
            registry.recover_persistence_uncertainty()
        self.assertEqual(
            recovery_error.exception.phase,
            "reader_mutation_requires_cold_restart",
        )

        restarted = self.registry()
        observed = restarted.get("idem-a")
        assert observed is not None
        self.assertEqual(observed.active_readers, 0)
        self.assertEqual(restarted.acquire_result("idem-a"), result)
        restarted.release_result("idem-a")

    def test_committed_reader_cleanup_errors_return_the_durable_outcome(self) -> None:
        registry = self.registry()
        result = self.complete(registry, self.root)
        registry.lease("idem-a", seconds=60)

        with mock.patch.object(
            registry,
            "_cleanup_temp_path",
            side_effect=OSError("acquire-temp-cleanup"),
        ):
            self.assertEqual(registry.acquire_result("idem-a"), result)
        self.assertEqual(registry.get("idem-a").active_readers, 1)
        self.assertEqual(
            registry.persistence_status()["last_event"]["outcome"],
            "committed_cleanup_failed",
        )
        with self.assertRaises(
            protocol.TaskRegistryPersistenceError
        ) as health_error:
            registry.assert_persistence_healthy()
        self.assertIsInstance(health_error.exception.__cause__, OSError)

        registry.release_result("idem-a")
        self.assertEqual(registry.get("idem-a").active_readers, 0)
        self.assertEqual(registry.persistence_status()["state"], "healthy")

        registry.acquire_result("idem-a")
        original_close = registry._close_parent_descriptor

        def close_then_raise(fd: int) -> None:
            original_close(fd)
            raise OSError("release-parent-close")

        with mock.patch.object(
            registry, "_close_parent_descriptor", side_effect=close_then_raise
        ):
            registry.release_result("idem-a")
        self.assertEqual(registry.get("idem-a").active_readers, 0)
        with self.assertRaises(
            protocol.TaskRegistryPersistenceError
        ) as release_health_error:
            registry.assert_persistence_healthy()
        self.assertIsInstance(release_health_error.exception.__cause__, OSError)
        registry.acknowledge("idem-a")
        self.assertEqual(registry.get("idem-a").state, "cleanup_pending")
        self.assertEqual(registry.persistence_status()["state"], "healthy")

    def test_acquisition_failure_release_failure_and_underflow_preserve_counts(self) -> None:
        registry = self.registry()
        self.complete(registry, self.root)
        registry.lease("idem-a", seconds=60)
        with mock.patch.object(
            registry, "_persist", side_effect=OSError("acquire-persist")
        ):
            with self.assertRaisesRegex(OSError, "acquire-persist"):
                registry.acquire_result("idem-a")
        self.assertEqual(registry.get("idem-a").active_readers, 0)

        registry.acquire_result("idem-a")
        with mock.patch.object(
            registry, "_persist", side_effect=OSError("release-persist")
        ):
            with self.assertRaisesRegex(OSError, "release-persist"):
                registry.release_result("idem-a")
        self.assertEqual(registry.get("idem-a").active_readers, 1)
        registry.release_result("idem-a")
        self.assertEqual(registry.get("idem-a").active_readers, 0)
        with self.assertRaisesRegex(RuntimeError, "underflowed"):
            registry.release_result("idem-a")
        self.assertEqual(registry.get("idem-a").active_readers, 0)

    def test_open_result_preserves_primary_error_when_release_also_fails(self) -> None:
        registry = self.registry()
        self.complete(registry, self.root)
        registry.lease("idem-a", seconds=60)
        with mock.patch.object(
            registry,
            "release_result",
            side_effect=OSError("secondary-release-error"),
        ):
            with self.assertRaisesRegex(ValueError, "primary-download-error") as raised:
                with registry.open_result("idem-a"):
                    raise ValueError("primary-download-error")
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertTrue(
            any("release also failed" in note for note in raised.exception.__notes__)
        )
        self.assertEqual(registry.get("idem-a").active_readers, 1)
        registry.release_result("idem-a")

    def test_cleanup_retry_handles_disappeared_result_and_upload_tree(self) -> None:
        registry = self.registry()
        result = self.complete(registry, self.root, bound=True)
        task_root = self.root / "task-a"
        uploads = task_root / "uploads"
        intermediate = task_root / "intermediate.bin"
        intermediate.write_bytes(b"partial")
        registry.acknowledge("idem-a")

        for child in uploads.iterdir():
            child.unlink()
        uploads.rmdir()
        result.unlink()
        self.assertEqual(registry.cleanup_consumed(), 1)
        self.assertFalse(task_root.exists())
        observed = registry.get("idem-a")
        assert observed is not None
        self.assertEqual(observed.state, "consumed")

    def test_cleanup_retry_rejects_renamed_upload_inode(self) -> None:
        registry = self.registry()
        self.complete(registry, self.root, bound=True)
        task_root = self.root / "task-a"
        (task_root / "uploads").rename(task_root / "uploads-renamed")
        registry.acknowledge("idem-a")
        with self.assertRaisesRegex(
            protocol.TaskProtocolConflict, "uploads directory was renamed"
        ):
            registry.cleanup_consumed()
        observed = registry.get("idem-a")
        assert observed is not None
        self.assertEqual(observed.state, "cleanup_pending")

    def test_cleanup_persist_failure_retains_intent_capacity_and_retries(self) -> None:
        registry = self.registry()
        result = self.complete(
            registry,
            self.root,
            bound=True,
            data=b"retained-result-bytes",
        )
        expected_bytes = len(b"retained-result-bytes")
        registry.acknowledge("idem-a")
        with mock.patch.object(
            registry, "_persist", side_effect=OSError("persist-after-unlink")
        ):
            with self.assertRaisesRegex(OSError, "persist-after-unlink"):
                registry.cleanup_consumed()
        self.assertFalse(result.exists())
        observed = registry.get("idem-a")
        assert observed is not None
        self.assertEqual(observed.state, "cleanup_pending")
        self.assertEqual(registry.unacked_result_bytes, expected_bytes)
        self.assertEqual(registry.cleanup_consumed(), 1)
        self.assertEqual(registry.get("idem-a").state, "consumed")

    def test_executor_does_not_overwrite_persistence_failure_as_parse_failure(self) -> None:
        registry = self.registry()
        self.create(registry)
        executor = protocol.SplitTaskExecutor(
            parse_slots=1,
            finalizer_slots=1,
            result_reservation_bytes=32,
        )
        calls = {"parse": 0, "finalize": 0}

        async def parse() -> None:
            calls["parse"] += 1

        async def finalize():
            calls["finalize"] += 1
            raise AssertionError("finalize must not run")

        with mock.patch.object(
            registry,
            "_write_registry_stream",
            side_effect=OSError("processing-transition-persist"),
        ):
            with self.assertRaises(protocol.TaskRegistryPersistenceError):
                asyncio.run(
                    executor.run(
                        registry=registry,
                        key="idem-a",
                        parse=parse,
                        finalize=finalize,
                    )
                )
        observed = registry.get("idem-a")
        assert observed is not None
        self.assertEqual(observed.state, "pending")
        self.assertIsNone(observed.error)
        self.assertEqual(calls, {"parse": 0, "finalize": 0})
        with self.assertRaises(protocol.TaskRegistryPersistenceError):
            protocol.task_protocol_runtime_status(registry, executor)

    def test_cleanup_syncs_namespaces_before_consumed_capacity_release(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="m6-p1-cleanup-order-"
        ) as directory:
            root = Path(directory)
            control = root / ".agent-task-protocol-v2"
            control.mkdir()
            registry = protocol.DurableTaskRegistry(
                control / "registry.json",
                max_unacked_result_bytes=4096,
                output_root=root,
            )
            result = self.complete(registry, root, bound=True)
            task_root = root / "task-a"
            uploads_root = task_root / "uploads"
            registry.acknowledge("idem-a")

            identities = {
                os.stat(root).st_ino: "output-root",
                os.stat(control).st_ino: "registry-parent",
                os.stat(task_root).st_ino: "task-root",
                os.stat(uploads_root).st_ino: "uploads-root",
            }
            self.assertNotEqual(os.stat(root).st_ino, os.stat(control).st_ino)
            observed: list[str] = []
            original_fsync = os.fsync
            original_persist = registry._persist

            def trace_fsync(descriptor: int) -> None:
                observed.append(
                    identities.get(
                        os.fstat(descriptor).st_ino,
                        "registry-temp-or-other",
                    )
                )
                original_fsync(descriptor)

            def persist_after_namespace_barrier() -> None:
                self.assertIn("output-root", observed)
                original_persist()

            with mock.patch.object(
                protocol.os, "fsync", side_effect=trace_fsync
            ), mock.patch.object(
                registry,
                "_persist",
                side_effect=persist_after_namespace_barrier,
            ):
                self.assertEqual(registry.cleanup_consumed(), 1)

            self.assertFalse(result.exists())
            self.assertFalse(task_root.exists())
            self.assertIn("uploads-root", observed)
            self.assertIn("task-root", observed)
            self.assertIn("output-root", observed)
            self.assertIn("registry-parent", observed)
            self.assertLess(
                observed.index("output-root"),
                observed.index("registry-parent"),
            )
            record = registry.get("idem-a")
            assert record is not None
            self.assertEqual(record.state, "consumed")
            self.assertIsNone(record.task_payload)
            self.assertEqual(registry.unacked_result_bytes, 0)

    def test_cleanup_namespace_failures_retain_intent_and_retry_absence(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="m6-p1-cleanup-fsync-"
        ) as directory:
            root = Path(directory)
            control = root / ".agent-task-protocol-v2"
            control.mkdir()
            registry = protocol.DurableTaskRegistry(
                control / "registry.json",
                max_unacked_result_bytes=4096,
                output_root=root,
            )
            result_bytes = b"namespace-fsync-result"
            self.complete(
                registry,
                root,
                bound=True,
                data=result_bytes,
            )
            task_root = root / "task-a"
            registry.acknowledge("idem-a")
            output_inode = os.stat(root).st_ino
            original_namespace_fsync = registry._fsync_namespace_directory
            original_close = os.close
            injection_hits = 0
            failing_descriptor: int | None = None

            def fail_output_parent(descriptor: int) -> None:
                nonlocal failing_descriptor, injection_hits
                if os.fstat(descriptor).st_ino == output_inode:
                    injection_hits += 1
                    failing_descriptor = descriptor
                    raise OSError("injected-output-namespace-fsync")
                original_namespace_fsync(descriptor)

            def close_after_primary(descriptor: int) -> None:
                original_close(descriptor)
                if descriptor == failing_descriptor:
                    raise OSError("injected-output-close")

            with mock.patch.object(
                registry,
                "_fsync_namespace_directory",
                side_effect=fail_output_parent,
            ), mock.patch.object(
                protocol.os,
                "close",
                side_effect=close_after_primary,
            ):
                with self.assertRaisesRegex(
                    OSError, "injected-output-namespace-fsync"
                ) as raised:
                    registry.cleanup_consumed()

            self.assertEqual(injection_hits, 1)
            self.assertIsNotNone(failing_descriptor)
            self.assertTrue(
                any(
                    "cleanup output root close also failed: OSError" in note
                    for note in raised.exception.__notes__
                )
            )
            self.assertFalse(task_root.exists())
            retained = registry.get("idem-a")
            assert retained is not None
            self.assertEqual(retained.state, "cleanup_pending")
            self.assertIsNotNone(retained.task_payload)
            self.assertEqual(
                registry.unacked_result_bytes,
                len(result_bytes),
            )

            retry_syncs = 0

            def trace_absent_retry(descriptor: int) -> None:
                nonlocal retry_syncs
                if os.fstat(descriptor).st_ino == output_inode:
                    retry_syncs += 1
                original_namespace_fsync(descriptor)

            with mock.patch.object(
                registry,
                "_fsync_namespace_directory",
                side_effect=trace_absent_retry,
            ):
                self.assertEqual(registry.cleanup_consumed(), 1)
            self.assertEqual(retry_syncs, 1)
            consumed = registry.get("idem-a")
            assert consumed is not None
            self.assertEqual(consumed.state, "consumed")
            self.assertEqual(registry.unacked_result_bytes, 0)

    def test_partial_cleanup_deletion_failure_retains_identity_and_retries(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="m6-p1-cleanup-partial-"
        ) as directory:
            root = Path(directory)
            registry = self.registry_at(root)
            result_bytes = b"partial-deletion-result"
            self.complete(
                registry,
                root,
                bound=True,
                data=result_bytes,
            )
            task_root = root / "task-a"
            initial_entries = set(os.listdir(task_root))
            self.assertEqual(initial_entries, {"uploads", ".retained-result.zip"})
            registry.acknowledge("idem-a")
            original_remove = registry._remove_at
            injection_hits = 0

            def remove_one_then_fail(parent_fd: int, name: str) -> None:
                nonlocal injection_hits
                injection_hits += 1
                if injection_hits == 1:
                    original_remove(parent_fd, name)
                    return
                raise OSError("injected-partial-namespace-delete")

            with mock.patch.object(
                registry,
                "_remove_at",
                side_effect=remove_one_then_fail,
            ):
                with self.assertRaisesRegex(
                    OSError, "injected-partial-namespace-delete"
                ):
                    registry.cleanup_consumed()

            self.assertEqual(injection_hits, 2)
            self.assertTrue(task_root.is_dir())
            self.assertLess(len(set(os.listdir(task_root))), len(initial_entries))
            retained = registry.get("idem-a")
            assert retained is not None
            self.assertEqual(retained.state, "cleanup_pending")
            self.assertIsNotNone(retained.task_payload)
            self.assertIsNotNone(retained.result_owner)
            self.assertEqual(
                registry.unacked_result_bytes,
                len(result_bytes),
            )

            self.assertEqual(registry.cleanup_consumed(), 1)
            self.assertFalse(task_root.exists())
            consumed = registry.get("idem-a")
            assert consumed is not None
            self.assertEqual(consumed.state, "consumed")
            self.assertEqual(registry.unacked_result_bytes, 0)

    def test_generated_fastapi_waiters_receive_persistence_outcomes(self) -> None:
        patcher_path = (
            SERVICE_ROOT
            / "scripts"
            / "windows"
            / "mineru_heap_trim_compat"
            / "patch_mineru_344.py"
        )
        patcher_spec = importlib.util.spec_from_file_location(
            "m6_p1_waiter_patch_mineru_344", patcher_path
        )
        assert patcher_spec is not None and patcher_spec.loader is not None
        patcher = importlib.util.module_from_spec(patcher_spec)
        sys.modules[patcher_spec.name] = patcher
        patcher_spec.loader.exec_module(patcher)
        preimage = (
            SERVICE_ROOT
            / "tests"
            / "fixtures"
            / "mineru_344_preimages"
            / "mineru"
            / "cli"
            / "fast_api.py"
        ).read_text(encoding="utf-8")
        patched = patcher.patch_source("mineru/cli/fast_api.py", preimage)
        tree = ast.parse(patched)
        manager_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "AsyncTaskManager"
        )
        selected_names = {
            "_on_processor_done",
            "_process_task",
            "_raise_task_wait_failure",
            "_signal_task_event",
            "_wake_waiters",
            "shutdown",
            "wait_for_terminal_state",
        }
        selected = [
            node
            for node in manager_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in selected_names
        ]
        self.assertEqual({node.name for node in selected}, selected_names)

        class TaskWaitAbortedError(RuntimeError):
            pass

        class LoggerStub:
            def exception(self, *_args: object) -> None:
                return None

            def error(self, *_args: object) -> None:
                return None

        async def build_retained_task_result(_task: object) -> None:
            return None

        namespace: dict[str, object] = {
            "Any": object,
            "AsyncParseTask": object,
            "Path": Path,
            "TASK_COMPLETED": "completed",
            "TASK_FAILED": "failed",
            "TaskRegistryPersistenceError": (
                protocol.TaskRegistryPersistenceError
            ),
            "TaskWaitAbortedError": TaskWaitAbortedError,
            "asyncio": asyncio,
            "build_retained_task_result": build_retained_task_result,
            "is_task_terminal": lambda status: status in {"completed", "failed"},
            "logger": LoggerStub(),
            "suppress": __import__("contextlib").suppress,
            "utc_now_iso": lambda: "now",
        }
        module = ast.Module(body=selected, type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, "generated-fast-api-methods.py", "exec"), namespace)
        manager_type = type(
            "GeneratedAsyncTaskManager",
            (),
            {name: namespace[name] for name in selected_names},
        )

        class Executor:
            def __init__(self, outcome: BaseException | None) -> None:
                self.outcome = outcome

            async def run(self, **_kwargs: object) -> None:
                if isinstance(
                    self.outcome,
                    protocol.TaskRegistryPersistenceError,
                ):
                    raise self.outcome from OSError(
                        "original-registry-storage-error"
                    )
                if self.outcome is not None:
                    raise self.outcome

        def task(task_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                task_id=task_id,
                agent_idempotency_key=f"idem-{task_id}",
                status="processing",
                error=None,
                completed_at=None,
                result_artifact_path=None,
                result_artifact_sha256=None,
                result_artifact_bytes=None,
                result_artifact_owner=None,
            )

        def manager(outcome: BaseException | None):
            value = manager_type()
            value.tasks = {}
            value.task_events = {}
            value.task_wait_failures = {}
            value.manager_wakeup = asyncio.Event()
            value.last_worker_error = None
            value.is_shutting_down = False
            value.active_tasks = set()
            value.queue = asyncio.Queue()
            value.dispatcher_task = None
            value.cleanup_task = None
            value.task_protocol_executor = Executor(outcome)
            value.task_protocol_v2 = SimpleNamespace(
                persistence_status=lambda: {
                    "state": "degraded",
                    "recovery_action": "call recover_persistence_uncertainty",
                },
                cleanup_consumed=lambda: 0,
            )
            return value

        def register(value, *tasks: SimpleNamespace) -> None:
            for current in tasks:
                value.tasks[current.task_id] = current
                value.task_events[current.task_id] = asyncio.Event()

        async def start_processor(value, task_id: str):
            value.queue.put_nowait(task_id)
            self.assertEqual(value.queue.get_nowait(), task_id)
            processor = asyncio.create_task(value._process_task(task_id))
            value.active_tasks.add(processor)
            processor.add_done_callback(value._on_processor_done)
            return processor

        async def exercise() -> None:
            persistence_error = protocol.TaskRegistryPersistenceError(
                operation="transition",
                phase="file_fsync",
                outcome="not_committed",
                committed=False,
            )
            persistence_manager = manager(persistence_error)
            affected = task("affected")
            unrelated = task("unrelated")
            register(persistence_manager, affected, unrelated)
            existing_waiter = asyncio.create_task(
                persistence_manager.wait_for_terminal_state("affected")
            )
            unrelated_waiter = asyncio.create_task(
                persistence_manager.wait_for_terminal_state("unrelated")
            )
            await asyncio.sleep(0)
            processor = await start_processor(
                persistence_manager, "affected"
            )
            with self.assertRaises(
                protocol.TaskRegistryPersistenceError
            ) as processor_error:
                await processor
            self.assertIs(processor_error.exception, persistence_error)
            await asyncio.sleep(0)

            with self.assertRaises(TaskWaitAbortedError) as existing_error:
                await asyncio.wait_for(existing_waiter, timeout=0.25)
            self.assertIs(existing_error.exception.__cause__, persistence_error)
            self.assertIsInstance(persistence_error.__cause__, OSError)
            self.assertIn("outcome=not_committed", str(existing_error.exception))
            self.assertIn(
                "recovery=call recover_persistence_uncertainty",
                str(existing_error.exception),
            )
            with self.assertRaises(TaskWaitAbortedError) as later_error:
                await asyncio.wait_for(
                    persistence_manager.wait_for_terminal_state("affected"),
                    timeout=0.25,
                )
            self.assertIs(later_error.exception.__cause__, persistence_error)
            await asyncio.sleep(0)
            self.assertFalse(unrelated_waiter.done())
            self.assertFalse(
                persistence_manager.task_events["unrelated"].is_set()
            )
            current_task = asyncio.current_task()
            assert current_task is not None
            helper_tasks = {
                candidate
                for candidate in asyncio.all_tasks()
                if candidate not in {current_task, unrelated_waiter}
                and not candidate.done()
                and getattr(candidate.get_coro(), "__qualname__", "")
                == "Event.wait"
            }
            self.assertEqual(len(helper_tasks), 2)
            unrelated_waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await unrelated_waiter
            self.assertTrue(all(helper.done() for helper in helper_tasks))
            self.assertTrue(all(helper.cancelled() for helper in helper_tasks))
            self.assertFalse(helper_tasks & asyncio.all_tasks())
            self.assertEqual(affected.status, "processing")
            self.assertIsNone(affected.error)
            self.assertEqual(
                persistence_manager.last_worker_error,
                str(persistence_error),
            )
            self.assertNotIn(processor, persistence_manager.active_tasks)

            successful_manager = manager(None)
            successful = task("successful")
            register(successful_manager, successful)
            success_waiter = asyncio.create_task(
                successful_manager.wait_for_terminal_state("successful")
            )
            await asyncio.sleep(0)
            success_processor = await start_processor(
                successful_manager, "successful"
            )
            await success_processor
            self.assertIs(
                await asyncio.wait_for(success_waiter, timeout=0.25),
                successful,
            )
            self.assertEqual(successful.status, "completed")
            self.assertEqual(successful_manager.task_wait_failures, {})

            parser_manager = manager(ValueError("ordinary-parser-failure"))
            parser_task = task("parser")
            register(parser_manager, parser_task)
            parser_waiter = asyncio.create_task(
                parser_manager.wait_for_terminal_state("parser")
            )
            await asyncio.sleep(0)
            parser_processor = await start_processor(parser_manager, "parser")
            await parser_processor
            self.assertIs(
                await asyncio.wait_for(parser_waiter, timeout=0.25),
                parser_task,
            )
            self.assertEqual(parser_task.status, "failed")
            self.assertEqual(parser_task.error, "ordinary-parser-failure")
            self.assertEqual(parser_manager.task_wait_failures, {})

            cancelled_manager = manager(asyncio.CancelledError())
            cancelled_task = task("cancelled")
            register(cancelled_manager, cancelled_task)
            cancelled_waiter = asyncio.create_task(
                cancelled_manager.wait_for_terminal_state("cancelled")
            )
            await asyncio.sleep(0)
            cancelled_processor = await start_processor(
                cancelled_manager, "cancelled"
            )
            with self.assertRaises(asyncio.CancelledError):
                await cancelled_processor
            self.assertIs(
                await asyncio.wait_for(cancelled_waiter, timeout=0.25),
                cancelled_task,
            )
            self.assertEqual(cancelled_task.status, "failed")
            self.assertEqual(
                cancelled_task.error,
                "Task processor was cancelled",
            )
            self.assertEqual(cancelled_manager.task_wait_failures, {})

            shutdown_manager = manager(None)
            shutdown_task = task("shutdown")
            register(shutdown_manager, shutdown_task)
            shutdown_waiter = asyncio.create_task(
                shutdown_manager.wait_for_terminal_state("shutdown")
            )
            await asyncio.sleep(0)
            with self.assertRaisesRegex(
                RuntimeError,
                "accepted tasks did not reach terminal state",
            ):
                await shutdown_manager.shutdown()
            with self.assertRaisesRegex(
                TaskWaitAbortedError,
                "shutting down",
            ):
                await asyncio.wait_for(shutdown_waiter, timeout=0.25)

        asyncio.run(exercise())

    def test_generated_fastapi_concurrent_waiter_cancellation_interleaving(self) -> None:
        patcher_path = (
            SERVICE_ROOT
            / "scripts"
            / "windows"
            / "mineru_heap_trim_compat"
            / "patch_mineru_344.py"
        )
        patcher_spec = importlib.util.spec_from_file_location(
            "m6_p1_concurrent_waiter_patch_mineru_344", patcher_path
        )
        assert patcher_spec is not None and patcher_spec.loader is not None
        patcher = importlib.util.module_from_spec(patcher_spec)
        sys.modules[patcher_spec.name] = patcher
        patcher_spec.loader.exec_module(patcher)
        preimage = (
            SERVICE_ROOT
            / "tests"
            / "fixtures"
            / "mineru_344_preimages"
            / "mineru"
            / "cli"
            / "fast_api.py"
        ).read_text(encoding="utf-8")
        patched = patcher.patch_source("mineru/cli/fast_api.py", preimage)
        tree = ast.parse(patched)
        manager_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "AsyncTaskManager"
        )
        selected_names = {
            "_process_task",
            "_raise_task_wait_failure",
            "_signal_task_event",
            "wait_for_terminal_state",
        }
        selected = [
            node
            for node in manager_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in selected_names
        ]
        self.assertEqual({node.name for node in selected}, selected_names)

        helper_barrier: asyncio.Event | None = None
        helper_target = 0
        helper_tasks: list[asyncio.Task[object]] = []
        helper_tasks_by_owner: dict[
            asyncio.Task[object], list[asyncio.Task[object]]
        ] = {}

        def tracking_create_task(coroutine):
            owner = asyncio.current_task()
            created = asyncio.create_task(coroutine)
            if getattr(created.get_coro(), "__qualname__", "") == "Event.wait":
                if owner is None:
                    raise AssertionError("Event.wait helper has no owning waiter")
                helper_tasks.append(created)
                helper_tasks_by_owner.setdefault(owner, []).append(created)
                if helper_barrier is not None and len(helper_tasks) >= helper_target:
                    helper_barrier.set()
            return created

        asyncio_proxy = SimpleNamespace(
            CancelledError=asyncio.CancelledError,
            FIRST_COMPLETED=asyncio.FIRST_COMPLETED,
            Task=asyncio.Task,
            create_task=tracking_create_task,
            gather=asyncio.gather,
            wait=asyncio.wait,
        )

        class TaskWaitAbortedError(RuntimeError):
            pass

        class LoggerStub:
            def exception(self, *_args: object) -> None:
                return None

        async def build_retained_task_result(current: SimpleNamespace) -> None:
            data = f"result-{current.task_id}".encode()
            result = self.root / f"generated-{current.task_id}.zip"
            result.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            owner = hashlib.sha256(
                f"{current.task_id}\0{digest}\0{len(data)}".encode()
            ).hexdigest()
            current.result_artifact_path = str(result)
            current.result_artifact_sha256 = digest
            current.result_artifact_bytes = len(data)
            current.result_artifact_owner = owner

        namespace: dict[str, object] = {
            "Any": object,
            "AsyncParseTask": object,
            "Path": Path,
            "TASK_COMPLETED": "completed",
            "TASK_FAILED": "failed",
            "TaskRegistryPersistenceError": protocol.TaskRegistryPersistenceError,
            "TaskWaitAbortedError": TaskWaitAbortedError,
            "asyncio": asyncio_proxy,
            "build_retained_task_result": build_retained_task_result,
            "is_task_terminal": lambda status: status in {"completed", "failed"},
            "logger": LoggerStub(),
            "suppress": __import__("contextlib").suppress,
            "utc_now_iso": lambda: "now",
        }
        module = ast.Module(body=selected, type_ignores=[])
        ast.fix_missing_locations(module)
        exec(
            compile(module, "generated-fast-api-concurrent-methods.py", "exec"),
            namespace,
        )

        async def run_parse_stage(
            _manager: object, _task: SimpleNamespace
        ) -> None:
            return None

        manager_type = type(
            "GeneratedConcurrentAsyncTaskManager",
            (),
            {
                **{name: namespace[name] for name in selected_names},
                "_run_parse_stage": run_parse_stage,
            },
        )

        registry = self.registry()
        self.create(registry, "affected")
        self.create(registry, "unrelated")

        def task(task_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                task_id=task_id,
                agent_idempotency_key=f"idem-{task_id}",
                status="processing",
                error=None,
                completed_at=None,
                result_artifact_path=None,
                result_artifact_sha256=None,
                result_artifact_bytes=None,
                result_artifact_owner=None,
            )

        async def exercise() -> None:
            nonlocal helper_barrier, helper_target

            inner_executor = protocol.SplitTaskExecutor(
                parse_slots=2,
                finalizer_slots=2,
                result_reservation_bytes=64,
            )

            class ControlledExecutor:
                def __init__(self) -> None:
                    self.affected_entered = asyncio.Event()
                    self.release_failure = asyncio.Event()
                    self.injection_hits = 0
                    self.persistence_error: (
                        protocol.TaskRegistryPersistenceError | None
                    ) = None

                def inject_storage_failure(self, *_args: object) -> None:
                    self.injection_hits += 1
                    raise OSError("generated-waiter-storage-failure")

                async def run(self, **kwargs: object) -> None:
                    key = kwargs.get("key")
                    if key == "idem-affected":
                        self.affected_entered.set()
                        await self.release_failure.wait()
                        try:
                            with mock.patch.object(
                                registry,
                                "_write_registry_stream",
                                side_effect=self.inject_storage_failure,
                            ):
                                await inner_executor.run(**kwargs)  # type: ignore[arg-type]
                        except protocol.TaskRegistryPersistenceError as exc:
                            self.persistence_error = exc
                            raise
                        raise AssertionError("affected persistence fault did not fire")
                    if key == "idem-unrelated":
                        await inner_executor.run(**kwargs)  # type: ignore[arg-type]
                        return
                    raise AssertionError(f"unexpected executor key: {key!r}")

            controlled = ControlledExecutor()
            value = manager_type()
            value.tasks = {}
            value.task_events = {}
            value.task_wait_failures = {}
            value.manager_wakeup = asyncio.Event()
            value.last_worker_error = None
            value.is_shutting_down = False
            value.queue = asyncio.Queue()
            value.task_protocol_executor = controlled
            value.task_protocol_v2 = registry

            affected = task("affected")
            unrelated = task("unrelated")
            for current in (affected, unrelated):
                value.tasks[current.task_id] = current
                value.task_events[current.task_id] = asyncio.Event()

            helper_barrier = asyncio.Event()
            helper_target = 6
            affected_waiters = [
                asyncio.create_task(value.wait_for_terminal_state("affected"))
                for _ in range(3)
            ]
            await asyncio.wait_for(helper_barrier.wait(), timeout=0.5)
            self.assertEqual(len(helper_tasks), 6)
            self.assertTrue(
                all(len(helper_tasks_by_owner[waiter]) == 2 for waiter in affected_waiters)
            )

            value.queue.put_nowait("affected")
            affected_processor = asyncio.create_task(value._process_task("affected"))
            await asyncio.wait_for(controlled.affected_entered.wait(), timeout=0.5)
            self.assertEqual(controlled.injection_hits, 0)

            cancelled_waiter, *surviving_waiters = affected_waiters
            cancelled_helpers = tuple(helper_tasks_by_owner[cancelled_waiter])
            cancelled_waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled_waiter
            self.assertTrue(all(helper.done() for helper in cancelled_helpers))
            self.assertTrue(all(helper.cancelled() for helper in cancelled_helpers))
            self.assertFalse(value.task_events["affected"].is_set())
            self.assertTrue(all(not waiter.done() for waiter in surviving_waiters))
            self.assertTrue(
                all(
                    not helper.done()
                    for waiter in surviving_waiters
                    for helper in helper_tasks_by_owner[waiter]
                )
            )

            helper_target = 8
            helper_barrier.clear()
            unrelated_waiter = asyncio.create_task(
                value.wait_for_terminal_state("unrelated")
            )
            await asyncio.wait_for(helper_barrier.wait(), timeout=0.5)
            value.queue.put_nowait("unrelated")
            unrelated_processor = asyncio.create_task(
                value._process_task("unrelated")
            )
            await asyncio.wait_for(unrelated_processor, timeout=0.5)
            self.assertIs(
                await asyncio.wait_for(unrelated_waiter, timeout=0.5),
                unrelated,
            )
            unrelated_record = registry.get("idem-unrelated")
            assert unrelated_record is not None
            self.assertEqual(unrelated_record.state, "completed")
            self.assertTrue(Path(unrelated_record.result_path or "").is_file())
            self.assertTrue(all(not waiter.done() for waiter in surviving_waiters))

            controlled.release_failure.set()
            with self.assertRaises(
                protocol.TaskRegistryPersistenceError
            ) as processor_error:
                await asyncio.wait_for(affected_processor, timeout=0.5)
            persistence_error = controlled.persistence_error
            assert persistence_error is not None
            self.assertIs(processor_error.exception, persistence_error)
            self.assertEqual(controlled.injection_hits, 1)
            self.assertEqual(persistence_error.outcome, "not_committed")
            self.assertFalse(persistence_error.committed)
            self.assertIsInstance(persistence_error.__cause__, OSError)
            self.assertEqual(
                str(persistence_error.__cause__),
                "generated-waiter-storage-failure",
            )

            waiter_results = await asyncio.wait_for(
                asyncio.gather(*surviving_waiters, return_exceptions=True),
                timeout=0.5,
            )
            status = registry.persistence_status()
            expected_recovery = (
                status.get("recovery_action") or "restart task manager"
            )
            for result in waiter_results:
                self.assertIs(type(result), TaskWaitAbortedError)
                assert isinstance(result, TaskWaitAbortedError)
                self.assertIs(result.__cause__, persistence_error)
                self.assertIn("outcome=not_committed", str(result))
                self.assertIn(f"recovery={expected_recovery}", str(result))

            affected_record = registry.get("idem-affected")
            assert affected_record is not None
            self.assertEqual(affected_record.state, "pending")
            self.assertIsNone(affected_record.error)
            self.assertEqual(affected.status, "processing")
            self.assertIsNone(affected.error)

            helper_count_before_late_wait = len(helper_tasks)
            with self.assertRaises(TaskWaitAbortedError) as late_error:
                await value.wait_for_terminal_state("affected")
            self.assertIs(late_error.exception.__cause__, persistence_error)
            self.assertEqual(len(helper_tasks), helper_count_before_late_wait)

            self.assertIs(
                await value.wait_for_terminal_state("unrelated"),
                unrelated,
            )
            self.assertEqual(len(helper_tasks), helper_count_before_late_wait)
            self.assertEqual(unrelated.status, "completed")

            self.assertEqual(len(helper_tasks), 8)
            self.assertTrue(all(helper.done() for helper in helper_tasks))
            self.assertFalse(set(helper_tasks) & asyncio.all_tasks())

        asyncio.run(exercise())

    def test_real_fastapi_preimage_has_closed_persistence_integration(self) -> None:
        patcher_path = (
            SERVICE_ROOT
            / "scripts"
            / "windows"
            / "mineru_heap_trim_compat"
            / "patch_mineru_344.py"
        )
        patcher_spec = importlib.util.spec_from_file_location(
            "m6_p1_patch_mineru_344", patcher_path
        )
        assert patcher_spec is not None and patcher_spec.loader is not None
        patcher = importlib.util.module_from_spec(patcher_spec)
        sys.modules[patcher_spec.name] = patcher
        patcher_spec.loader.exec_module(patcher)
        preimages = SERVICE_ROOT / "tests" / "fixtures" / "mineru_344_preimages"
        fastapi_source = (preimages / "mineru" / "cli" / "fast_api.py").read_text(
            encoding="utf-8"
        )
        patched_fastapi = patcher.patch_source(
            "mineru/cli/fast_api.py", fastapi_source
        )
        self.assertIn("    TaskRegistryPersistenceError,", patched_fastapi)
        self.assertIn(
            "Task registry persistence failed; task status remains nonterminal",
            patched_fastapi,
        )
        self.assertIn(
            "self.task_wait_failures: dict[str, TaskRegistryPersistenceError]",
            patched_fastapi,
        )
        self.assertIn(
            "self.task_wait_failures[task_id] = exc\n"
            "            self._signal_task_event(task_id)",
            patched_fastapi,
        )
        self.assertEqual(
            patched_fastapi.count("self._raise_task_wait_failure(task_id)"),
            2,
        )
        self.assertIn(
            "outcome={failure.outcome}; recovery={recovery_action}",
            patched_fastapi,
        )
        self.assertRegex(
            patched_fastapi,
            re.compile(r"except TaskProtocolConflict:\n\s+pass"),
        )
        compile(patched_fastapi, "patched-fast-api.py", "exec")

        api_source = (preimages / "mineru" / "cli" / "api_request.py").read_text(
            encoding="utf-8"
        )
        patched_api = patcher.patch_source(
            "mineru/cli/api_request.py", api_source
        )
        self.assertNotIn("TaskRegistryPersistenceError", patched_api)
        compile(patched_api, "patched-api-request.py", "exec")

    def test_attached_root_reproductions_are_fixed_regressions(self) -> None:
        regression_root = (
            SERVICE_ROOT
            / "tests"
            / "regressions"
            / "m6_p1_registry_persistence"
        )
        env = os.environ.copy()
        env["M6_PROTOCOL_MODULE"] = str(MODULE_PATH)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for wrapper in (
            regression_root / "run_registry_fault_regression.py",
            regression_root / "run_postreplace_fault_regression.py",
        ):
            completed = subprocess.run(
                [sys.executable, str(wrapper)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=f"{wrapper.name} failed:\n{completed.stdout}",
            )
            observation = json.loads(completed.stdout)
            self.assertEqual(observation["injection_hits"], 1)
            self.assertEqual(
                observation["source_sha256"],
                hashlib.sha256(MODULE_PATH.read_bytes()).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
