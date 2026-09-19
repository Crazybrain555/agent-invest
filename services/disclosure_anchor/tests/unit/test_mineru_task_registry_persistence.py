"""Independent durability tests for the MinerU task-registry persistence boundary.

Scope is the M6 P1 contract in
``docs/implementation/design/mineru-task-registry-persistence.md``.  Every case
uses a disposable temporary root, synthetic placeholders, the registry's public
surface and the bytes it leaves on disk.  Storage faults are injected through
the registry's narrow storage hooks; expected outcomes come from the contract,
not from re-deriving the implementation.  Nothing here qualifies GPU
throughput, real PDF processing, PostgreSQL publication, live service operation
or M6 as a whole.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from contextlib import suppress
from dataclasses import asdict
from functools import partial
from pathlib import Path
from unittest.mock import patch

from scripts.windows.mineru_heap_trim_compat.agent_task_protocol_v2 import (
    DurableTaskRegistry,
    SplitTaskExecutor,
    TaskProtocolConflict,
    TaskRegistryPersistenceError,
    admission_counts,
)
from scripts.windows.mineru_heap_trim_compat.patch_mineru_344 import (
    TARGET_PREIMAGE_SHA256,
    patch_source,
)
from tests._m6_fast_api_fixture import (
    LoggerStub,
    TaskStub,
    build_manager,
    load_generated_fast_api,
    stop_manager,
)
from tests._m6_registry_lab import (
    FINALIZER_BUDGET,
    KEY,
    MUTATOR_PRECONDITIONS,
    OTHER_KEY,
    OTHER_TASK,
    PERMANENT,
    PRE_COMMIT_PHASES,
    TASK,
    CountedFault,
    RegistryLab,
    SyntheticStorageFault,
    close_descriptor_then_fail,
    drive,
    fail_with,
    instance_hook,
    persist_override,
    pre_commit_fault,
    prepare_mutation,
    replace_then_fail,
    snapshot,
    without_live_readers,
    write_foreign_then_fail,
)


class _RegistryCase(unittest.TestCase):
    def lab(self) -> RegistryLab:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return RegistryLab(Path(temporary.name))

    def assert_outcome(
        self,
        raised: BaseException,
        *,
        outcome: str,
        committed: bool,
        phase_prefix: str | None = None,
        cause_type: type[BaseException] | None = SyntheticStorageFault,
    ) -> None:
        self.assertIsInstance(raised, TaskRegistryPersistenceError)
        assert isinstance(raised, TaskRegistryPersistenceError)
        self.assertEqual(raised.outcome, outcome)
        self.assertIs(raised.committed, committed)
        if phase_prefix is not None:
            self.assertTrue(raised.phase.startswith(phase_prefix), raised.phase)
        if cause_type is not None:
            self.assertIsInstance(raised.__cause__, cause_type)

    def assert_healthy(self, registry: DurableTaskRegistry) -> None:
        status = registry.persistence_status()
        self.assertEqual(status["state"], "healthy")
        self.assertIsNone(status["last_event"])
        self.assertIsNone(status["recovery_action"])
        registry.assert_persistence_healthy()


class MutatorRollbackTests(_RegistryCase):
    def test_direct_persist_override_rolls_back_every_mutator(self) -> None:
        for name in MUTATOR_PRECONDITIONS:
            with self.subTest(mutator=name):
                lab = self.lab()
                registry = lab.open()
                invoke = prepare_mutation(lab, registry, name)
                before = snapshot(registry)
                disk = lab.disk_bytes()
                with persist_override(registry), self.assertRaises(SyntheticStorageFault):
                    invoke()
                self.assertEqual(snapshot(registry), before)
                self.assertEqual(lab.disk_bytes(), disk)
                self.assertEqual(lab.stray_names(), [])
                status = registry.persistence_status()
                self.assertNotEqual(status["state"], "durability_uncertain")
                if status["last_event"] is not None:
                    self.assertEqual(status["last_event"]["phase"], "persist_call")
                    self.assertEqual(status["last_event"]["operation"], name)
                    self.assertFalse(status["last_event"]["committed"])
                    self.assertEqual(status["recovery_action"], "retry_idempotent_operation")
                invoke()
                self.assert_healthy(registry)
                after = snapshot(registry)
                self.assertNotEqual(after, before)
                self.assertNotEqual(lab.disk_bytes(), disk)
                self.assertEqual(snapshot(lab.open()), without_live_readers(after))

    def test_write_flush_file_fsync_close_and_replace_fail_before_commit(self) -> None:
        expected_phase = {
            "temp_create": "temp_create",
            "write": "write",
            "flush": "flush",
            "file_fsync": "file_fsync",
            "file_close": "file_close",
            "replace_before_rename": "replace",
        }
        self.assertEqual(set(expected_phase) | {"short_write"}, set(PRE_COMMIT_PHASES))
        for phase, prefix in expected_phase.items():
            with self.subTest(phase=phase):
                lab = self.lab()
                registry = lab.open()
                drive(lab, registry, "bound")
                before = snapshot(registry)
                disk = lab.disk_bytes()
                self.assertIsNotNone(disk)
                with pre_commit_fault(registry, phase), self.assertRaises(
                    TaskRegistryPersistenceError
                ) as raised:
                    registry.transition(KEY, "processing")
                self.assert_outcome(
                    raised.exception,
                    outcome="not_committed",
                    committed=False,
                    phase_prefix=prefix,
                )
                self.assertEqual(raised.exception.operation, "transition")
                self.assertEqual(registry.get(KEY).state, "pending")
                self.assertEqual(snapshot(registry), before)
                self.assertEqual(lab.disk_bytes(), disk)
                self.assertEqual(lab.stray_names(), [])
                status = registry.persistence_status()
                self.assertEqual(status["state"], "degraded")
                self.assertEqual(status["recovery_action"], "retry_idempotent_operation")
                self.assertEqual(status["last_event"]["cause_type"], "SyntheticStorageFault")
                self.assertNotIn(KEY, json.dumps(status))
                registry.transition(KEY, "processing")
                self.assert_healthy(registry)
                self.assertEqual(lab.disk_records()[KEY]["state"], "processing")
                self.assertEqual(lab.open().get(KEY).state, "processing")

        with self.subTest(phase="first_persist_write"):
            lab = self.lab()
            registry = lab.open()
            self.assertIsNone(lab.disk_bytes())
            with pre_commit_fault(registry, "write"), self.assertRaises(
                TaskRegistryPersistenceError
            ) as raised:
                registry.reconcile_or_create(
                    idempotency_key=KEY,
                    task_id=TASK,
                    attempt_identity="a",
                    fence_identity="f",
                )
            self.assert_outcome(raised.exception, outcome="not_committed", committed=False)
            self.assertIsNone(lab.disk_bytes())
            self.assertIsNone(registry.get(KEY))
            self.assertEqual(lab.stray_names(), [])

    def test_short_write_is_not_committed(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "bound")
        disk = lab.disk_bytes()
        with pre_commit_fault(registry, "short_write"), self.assertRaises(
            TaskRegistryPersistenceError
        ) as raised:
            registry.transition(KEY, "processing")
        self.assert_outcome(
            raised.exception,
            outcome="not_committed",
            committed=False,
            phase_prefix="write",
            cause_type=OSError,
        )
        self.assertIn("short", str(raised.exception.__cause__))
        self.assertEqual(registry.get(KEY).state, "pending")
        self.assertEqual(lab.disk_bytes(), disk)
        self.assertEqual(lab.stray_names(), [])
        registry.transition(KEY, "processing")
        self.assertEqual(lab.open().get(KEY).state, "processing")


class ReplaceAndParentFsyncTests(_RegistryCase):
    def test_replace_then_raise_is_fsynced_and_committed(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "bound")
        with instance_hook(registry, "_replace_registry_file", replace_then_fail):
            self.assertIsNone(registry.transition(KEY, "processing"))
        self.assertEqual(registry.get(KEY).state, "processing")
        self.assertEqual(lab.disk_records()[KEY]["state"], "processing")
        self.assertEqual(lab.stray_names(), [])
        status = registry.persistence_status()
        self.assertEqual(status["state"], "degraded")
        self.assertEqual(status["recovery_action"], "do_not_retry_committed_operation")
        self.assertEqual(status["last_event"]["outcome"], "committed_after_recovery")
        self.assertTrue(status["last_event"]["committed"])
        self.assertEqual(status["last_event"]["cause_type"], "SyntheticStorageFault")
        self.assertEqual(status["candidate_idempotency_keys"], [])
        self.assertNotIn(KEY, json.dumps(status))
        with self.assertRaises(TaskRegistryPersistenceError) as raised:
            registry.assert_persistence_healthy()
        self.assert_outcome(
            raised.exception, outcome="committed_after_recovery", committed=True
        )
        self.assertEqual(lab.open().get(KEY).state, "processing")
        registry.transition(KEY, "finalizing")
        self.assert_healthy(registry)

    def test_parent_fsync_transient_failure_commits_only_after_retry(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "bound")
        fault = CountedFault(1, os.fsync, message="parent fsync transient failure")
        with instance_hook(registry, "_fsync_parent_descriptor", fault):
            registry.transition(KEY, "processing")
        self.assertEqual(fault.calls, 2)
        status = registry.persistence_status()
        self.assertEqual(status["state"], "degraded")
        self.assertEqual(status["last_event"]["outcome"], "committed_after_recovery")
        self.assertEqual(status["last_event"]["phase"], "parent_fsync_retry")
        self.assertTrue(status["last_event"]["committed"])
        self.assertEqual(registry.get(KEY).state, "processing")
        self.assertEqual(lab.disk_records()[KEY]["state"], "processing")
        self.assertEqual(lab.open().get(KEY).state, "processing")

    def test_permanent_parent_fsync_failure_recovers_candidate_explicitly(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "bound")
        drive(lab, registry, "pending_unbound", key=OTHER_KEY, task_id=OTHER_TASK)
        fault = CountedFault(PERMANENT, message="parent fsync permanently failing")
        with instance_hook(registry, "_fsync_parent_descriptor", fault), self.assertRaises(
            TaskRegistryPersistenceError
        ) as raised:
            registry.transition(KEY, "processing")
        self.assert_outcome(
            raised.exception,
            outcome="durability_uncertain",
            committed=False,
            phase_prefix="parent_fsync",
        )
        self.assertGreaterEqual(fault.calls, 2)
        self.assertEqual(lab.disk_records()[KEY]["state"], "processing")

        status = registry.persistence_status()
        self.assertEqual(status["state"], "durability_uncertain")
        self.assertEqual(status["recovery_action"], "call recover_persistence_uncertainty")
        self.assertEqual(status["candidate_idempotency_keys"], sorted([KEY, OTHER_KEY]))
        for closed_read in (
            partial(registry.get, KEY),
            partial(registry.get, OTHER_KEY),
            partial(registry.get_by_task_id, TASK),
        ):
            with self.assertRaises(TaskRegistryPersistenceError) as read_error:
                closed_read()
            self.assert_outcome(
                read_error.exception, outcome="durability_uncertain", committed=False
            )
        with self.assertRaises(TaskRegistryPersistenceError) as mutation_error:
            registry.transition(OTHER_KEY, "failed")
        self.assert_outcome(
            mutation_error.exception, outcome="durability_uncertain", committed=False
        )
        with self.assertRaises(TaskRegistryPersistenceError):
            registry.assert_persistence_healthy()

        recovered = registry.recover_persistence_uncertainty()
        self.assertEqual(recovered["state"], "degraded")
        self.assertEqual(recovered["last_event"]["outcome"], "committed_after_explicit_recovery")
        self.assertTrue(recovered["last_event"]["committed"])
        self.assertEqual(recovered["last_event"]["operation"], "transition")
        self.assertEqual(recovered["recovery_action"], "do_not_retry_committed_operation")
        self.assertEqual(registry.get(KEY).state, "processing")
        self.assertEqual(registry.get(OTHER_KEY).state, "pending")
        self.assertEqual(lab.open().get(KEY).state, "processing")
        registry.transition(KEY, "finalizing")
        self.assert_healthy(registry)
        self.assertEqual(registry.recover_persistence_uncertainty()["state"], "healthy")

    def test_explicit_recovery_can_select_previous_durable_bytes(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "bound")
        disk = lab.disk_bytes()
        fsync_fault = CountedFault(PERMANENT, message="parent fsync retry failing")
        with instance_hook(
            registry, "_replace_registry_file", fail_with("replace failed before rename")
        ), instance_hook(registry, "_fsync_parent_descriptor", fsync_fault), self.assertRaises(
            TaskRegistryPersistenceError
        ) as raised:
            registry.transition(KEY, "processing")
        self.assert_outcome(
            raised.exception,
            outcome="durability_uncertain",
            committed=False,
            phase_prefix="replace",
        )
        self.assertEqual(lab.disk_bytes(), disk)
        self.assertEqual(lab.stray_names(), [])
        self.assertEqual(registry.persistence_status()["state"], "durability_uncertain")

        recovered = registry.recover_persistence_uncertainty()
        self.assertEqual(recovered["state"], "degraded")
        self.assertEqual(
            recovered["last_event"]["outcome"], "not_committed_after_explicit_recovery"
        )
        self.assertFalse(recovered["last_event"]["committed"])
        self.assertEqual(recovered["recovery_action"], "retry_idempotent_operation")
        self.assertEqual(recovered["candidate_idempotency_keys"], [])
        self.assertEqual(registry.get(KEY).state, "pending")
        self.assertEqual(lab.disk_bytes(), disk)
        registry.transition(KEY, "processing")
        self.assert_healthy(registry)
        self.assertEqual(lab.open().get(KEY).state, "processing")

    def test_uncertainty_keeps_the_larger_capacity_charge(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "reserved")
        path, digest, size, owner = lab.make_result(TASK)
        self.assertEqual(
            (registry.reserved_result_bytes, registry.unacked_result_bytes),
            (FINALIZER_BUDGET, 0),
        )
        complete = partial(
            registry.complete,
            KEY,
            result_path=path,
            result_sha256=digest,
            result_bytes=size,
            result_owner=owner,
        )
        with instance_hook(
            registry, "_fsync_parent_descriptor", CountedFault(PERMANENT)
        ), self.assertRaises(TaskRegistryPersistenceError):
            complete()
        self.assertEqual(registry.persistence_status()["state"], "durability_uncertain")
        self.assertEqual(registry.reserved_result_bytes, FINALIZER_BUDGET)
        self.assertEqual(registry.unacked_result_bytes, size)
        registry.recover_persistence_uncertainty()
        self.assertEqual(registry.get(KEY).state, "completed")
        self.assertEqual((registry.reserved_result_bytes, registry.unacked_result_bytes), (0, size))

        with self.subTest(selected="previous"):
            lab = self.lab()
            registry = lab.open()
            drive(lab, registry, "reserved")
            path, digest, size, owner = lab.make_result(TASK)
            with instance_hook(
                registry, "_replace_registry_file", fail_with("replace failed before rename")
            ), instance_hook(
                registry, "_fsync_parent_descriptor", CountedFault(PERMANENT)
            ), self.assertRaises(TaskRegistryPersistenceError):
                registry.complete(
                    KEY,
                    result_path=path,
                    result_sha256=digest,
                    result_bytes=size,
                    result_owner=owner,
                )
            self.assertEqual(registry.reserved_result_bytes, FINALIZER_BUDGET)
            self.assertEqual(registry.unacked_result_bytes, size)
            registry.recover_persistence_uncertainty()
            self.assertEqual(registry.get(KEY).state, "finalizing")
            self.assertEqual(
                (registry.reserved_result_bytes, registry.unacked_result_bytes),
                (FINALIZER_BUDGET, 0),
            )

    def test_foreign_bytes_keep_the_registry_closed_until_cold_validation(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "bound")
        with instance_hook(
            registry, "_replace_registry_file", write_foreign_then_fail
        ), self.assertRaises(TaskRegistryPersistenceError) as raised:
            registry.transition(KEY, "processing")
        self.assert_outcome(
            raised.exception,
            outcome="durability_uncertain",
            committed=False,
            phase_prefix="replace_ambiguous_bytes",
        )
        self.assertEqual(lab.disk_bytes(), b'{"schema":"foreign-bytes"}')
        with self.assertRaises(TaskRegistryPersistenceError) as recovery:
            registry.recover_persistence_uncertainty()
        self.assert_outcome(
            recovery.exception,
            outcome="durability_uncertain",
            committed=False,
            phase_prefix="explicit_parent_fsync",
            cause_type=TaskProtocolConflict,
        )
        self.assertEqual(registry.persistence_status()["state"], "durability_uncertain")
        with self.assertRaises(TaskRegistryPersistenceError):
            registry.get(KEY)
        with self.assertRaises(TaskProtocolConflict):
            lab.open()

    def test_bytes_changed_during_reconciliation_is_uncertain_then_recoverable(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "bound")
        previous = lab.disk_bytes()
        assert previous is not None
        fired = []

        def restore_previous_then_fsync(descriptor: int) -> None:
            if not fired:
                fired.append(True)
                lab.registry_path.write_bytes(previous)
            os.fsync(descriptor)

        with instance_hook(registry, "_replace_registry_file", replace_then_fail), instance_hook(
            registry, "_fsync_parent_descriptor", restore_previous_then_fsync
        ), self.assertRaises(TaskRegistryPersistenceError) as raised:
            registry.transition(KEY, "processing")
        self.assert_outcome(
            raised.exception,
            outcome="durability_uncertain",
            committed=False,
            phase_prefix="replace_changed_during_reconciliation",
        )
        recovered = registry.recover_persistence_uncertainty()
        self.assertEqual(
            recovered["last_event"]["outcome"], "not_committed_after_explicit_recovery"
        )
        self.assertEqual(registry.get(KEY).state, "pending")
        self.assertEqual(lab.disk_bytes(), previous)
        registry.transition(KEY, "processing")
        self.assertEqual(lab.open().get(KEY).state, "processing")


class CommittedCleanupFailureTests(_RegistryCase):
    def test_post_commit_temp_cleanup_degrades_health_without_rollback(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "bound")
        with instance_hook(
            registry, "_cleanup_temp_path", fail_with("temp cleanup failed after commit")
        ):
            self.assertIsNone(registry.transition(KEY, "processing"))
        self.assertEqual(registry.get(KEY).state, "processing")
        self.assertEqual(lab.disk_records()[KEY]["state"], "processing")
        status = registry.persistence_status()
        self.assertEqual(status["state"], "degraded")
        self.assertEqual(status["recovery_action"], "do_not_retry_committed_operation")
        self.assertEqual(status["last_event"]["outcome"], "committed_cleanup_failed")
        self.assertEqual(status["last_event"]["phase"], "post_commit_cleanup")
        self.assertTrue(status["last_event"]["committed"])
        self.assertEqual(status["last_event"]["cleanup_cause_type"], "SyntheticStorageFault")
        with self.assertRaises(TaskRegistryPersistenceError) as raised:
            registry.assert_persistence_healthy()
        self.assert_outcome(
            raised.exception, outcome="committed_cleanup_failed", committed=True
        )
        self.assertEqual(lab.open().get(KEY).state, "processing")
        registry.transition(KEY, "finalizing")
        self.assert_healthy(registry)

    def test_post_commit_parent_close_degrades_health_without_rollback(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "bound")
        with instance_hook(registry, "_close_parent_descriptor", close_descriptor_then_fail):
            self.assertIsNone(registry.transition(KEY, "processing"))
        self.assertEqual(registry.get(KEY).state, "processing")
        self.assertEqual(lab.disk_records()[KEY]["state"], "processing")
        status = registry.persistence_status()
        self.assertEqual(status["state"], "degraded")
        self.assertEqual(status["last_event"]["outcome"], "committed_cleanup_failed")
        self.assertTrue(status["last_event"]["committed"])
        with self.assertRaises(TaskRegistryPersistenceError) as raised:
            registry.assert_persistence_healthy()
        self.assert_outcome(
            raised.exception, outcome="committed_cleanup_failed", committed=True
        )
        self.assertEqual(lab.stray_names(), [])
        registry.transition(KEY, "finalizing")
        self.assert_healthy(registry)


class ObservationAndRecoveryTests(_RegistryCase):
    def test_mutable_observations_and_nested_payloads_are_detached(self) -> None:
        lab = self.lab()
        registry = lab.open()
        facts = drive(lab, registry, "bound")
        facts["payload"]["options"]["nested"]["list"].append(99)
        self.assertEqual(
            registry.get(KEY).task_payload["options"]["nested"]["list"], [1, 2, 3]
        )
        observed = registry.get(KEY)
        observed.state = "failed"
        observed.task_payload["uploads"].append("/nowhere")
        observed.task_payload["options"]["nested"]["list"].clear()
        again = registry.get(KEY)
        self.assertEqual(again.state, "pending")
        self.assertEqual(len(again.task_payload["uploads"]), 1)
        self.assertEqual(again.task_payload["options"]["nested"]["list"], [1, 2, 3])

        reconciled, created = registry.reconcile_or_create(
            idempotency_key=KEY,
            task_id=TASK,
            attempt_identity=f"attempt-{KEY}",
            fence_identity=f"fence-{KEY}",
        )
        self.assertFalse(created)
        reconciled.task_payload["options"]["nested"]["list"].append(5)
        self.assertEqual(registry.get(KEY).task_payload["options"]["nested"]["list"], [1, 2, 3])

        hydrated = registry.recoverable_payloads()
        self.assertEqual(len(hydrated), 1)
        self.assertNotIn("_agent_protocol", hydrated[0])
        self.assertEqual(hydrated[0]["status"], "pending")
        hydrated[0]["options"]["nested"]["list"].append(7)
        self.assertEqual(registry.get(KEY).task_payload["options"]["nested"]["list"], [1, 2, 3])
        receipt = lab.disk_records()[KEY]["task_payload"]["_agent_protocol"]
        self.assertEqual(receipt["schema"], "mineru-task-payload-owner.v1")
        self.assertEqual(receipt["generation"], 1)
        self.assertEqual(len(receipt["uploads"]), 1)

    def test_recovery_persists_generation_before_filesystem_cleanup(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "processing")
        stale = lab.task_dir(TASK) / "stale-output.json"
        stale.write_bytes(b"{}")

        def refuse_removal(_cls: type, _parent_fd: int, name: str) -> None:
            raise SyntheticStorageFault(f"refusing to remove {name}")

        with patch.object(
            DurableTaskRegistry, "_remove_at", classmethod(refuse_removal)
        ), self.assertRaises(SyntheticStorageFault):
            registry.recoverable_payloads()
        self.assertTrue(stale.exists())
        on_disk = lab.disk_records()[KEY]
        self.assertEqual(on_disk["state"], "pending")
        self.assertEqual(on_disk["recovery_generation"], 2)
        self.assertEqual(on_disk["task_payload"]["_agent_protocol"]["generation"], 2)
        self.assertEqual(on_disk["reserved_result_bytes"], 0)
        record = registry.get(KEY)
        self.assertEqual((record.state, record.recovery_generation), ("pending", 2))
        self.assertNotEqual(registry.persistence_status()["state"], "durability_uncertain")

        hydrated = registry.recoverable_payloads()
        self.assertFalse(stale.exists())
        self.assertTrue((lab.task_dir(TASK) / "uploads" / "source.pdf").exists())
        self.assertEqual([item["status"] for item in hydrated], ["pending"])
        self.assertEqual(registry.get(KEY).recovery_generation, 2)
        self.assertEqual(lab.open().get(KEY).recovery_generation, 2)


class DurableViewTests(_RegistryCase):
    def test_published_view_carries_the_durable_bytes_through_a_blocked_commit(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "bound")
        drive(lab, registry, "pending_unbound", key=OTHER_KEY, task_id=OTHER_TASK)
        committed = registry.durable_view()
        durable = json.loads(lab.disk_bytes())
        self.assertEqual(
            [asdict(record) for record in committed.records], durable["records"]
        )
        self.assertEqual(
            committed.submission_watermark_bucket,
            durable["submission_watermark_bucket"],
        )
        self.assertFalse(committed.durability_uncertain)
        self.assertIsNone(committed.persistence_event)
        self.assertGreater(committed.published_monotonic_ns, 0)

        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = DurableTaskRegistry._persist_serialized_payload

        def blocked(payload: bytes) -> None:
            entered.set()
            if not release.wait(3):
                raise AssertionError("controlled commit barrier expired")
            original(registry, payload)

        with instance_hook(registry, "_persist_serialized_payload", blocked):
            committing = threading.Thread(
                target=registry.transition, args=(KEY, "processing")
            )
            committing.start()
            try:
                self.assertTrue(entered.wait(3))
                started = time.monotonic()
                in_flight = registry.durable_view()
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(
                    in_flight.persistence_generation,
                    committed.persistence_generation,
                )
                self.assertEqual(
                    [asdict(record) for record in in_flight.records],
                    durable["records"],
                )
            finally:
                release.set()
                committing.join(5)
        self.assertFalse(committing.is_alive())
        after = registry.durable_view()
        self.assertEqual(
            after.persistence_generation, committed.persistence_generation + 1
        )
        self.assertEqual(
            {record.idempotency_key: record.state for record in after.records}[KEY],
            "processing",
        )

    def test_refused_and_uncertain_commits_pin_the_view_and_close_admission(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "bound")
        committed = registry.durable_view()
        with pre_commit_fault(registry, "write"), self.assertRaises(
            TaskRegistryPersistenceError
        ):
            registry.transition(KEY, "processing")
        refused = registry.durable_view()
        self.assertEqual(
            refused.persistence_generation, committed.persistence_generation
        )
        self.assertFalse(refused.durability_uncertain)
        self.assertEqual(refused.persistence_event["outcome"], "not_committed")
        self.assertEqual(
            [asdict(record) for record in refused.records],
            [asdict(record) for record in committed.records],
        )
        with self.assertRaises(TaskRegistryPersistenceError) as degraded:
            DurableTaskRegistry.admission_status_from_view(refused, set())
        self.assert_outcome(
            degraded.exception,
            outcome="not_committed",
            committed=False,
            phase_prefix="write",
            cause_type=None,
        )
        self.assertIsNone(degraded.exception.__cause__)

        with instance_hook(
            registry, "_replace_registry_file", write_foreign_then_fail
        ), self.assertRaises(TaskRegistryPersistenceError):
            registry.transition(KEY, "processing")
        uncertain = registry.durable_view()
        self.assertTrue(uncertain.durability_uncertain)
        self.assertEqual(
            uncertain.persistence_generation, committed.persistence_generation
        )
        with self.assertRaises(TaskRegistryPersistenceError) as closed:
            DurableTaskRegistry.admission_status_from_view(uncertain, set())
        self.assert_outcome(
            closed.exception,
            outcome="durability_uncertain",
            committed=False,
            phase_prefix="replace_ambiguous_bytes",
            cause_type=None,
        )
        self.assertIsNone(closed.exception.__cause__)

    def test_lock_free_counts_equal_the_locked_admission_status(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "processing")
        drive(lab, registry, "bound", key=OTHER_KEY, task_id=OTHER_TASK)
        routes, live = {TASK}, {OTHER_TASK}
        locked = registry.admission_status(routes, live)
        view = registry.durable_view()
        self.assertEqual(admission_counts(view.records, routes, live), locked)
        self.assertEqual(
            DurableTaskRegistry.admission_status_from_view(view, routes, live), locked
        )
        self.assertEqual(
            (
                locked["durable_nonterminal_tasks"],
                locked["accepted_processing_tasks"],
                locked["accepted_pending_tasks"],
                locked["routeless_accepted_tasks"],
            ),
            (2, 1, 1, 1),
        )


class ReaderAndAckTests(_RegistryCase):
    def test_live_reader_survives_unrelated_uncertainty_and_recovery(self) -> None:
        lab = self.lab()
        registry = lab.open()
        facts = drive(lab, registry, "leased")
        drive(lab, registry, "bound", key=OTHER_KEY, task_id=OTHER_TASK)
        result_path = facts["result"][0]
        self.assertEqual(registry.acquire_result(KEY), result_path)
        self.assertEqual(registry.get(KEY).active_readers, 1)
        with self.assertRaisesRegex(TaskProtocolConflict, "live result readers"):
            registry.recoverable_payloads()

        with instance_hook(
            registry, "_fsync_parent_descriptor", CountedFault(PERMANENT)
        ), self.assertRaises(TaskRegistryPersistenceError):
            registry.transition(OTHER_KEY, "processing")
        self.assertEqual(registry.persistence_status()["state"], "durability_uncertain")
        with self.assertRaises(TaskRegistryPersistenceError):
            registry.get(KEY)
        self.assertEqual(registry.unacked_result_bytes, facts["result"][2])

        recovered = registry.recover_persistence_uncertainty()
        self.assertEqual(recovered["last_event"]["outcome"], "committed_after_explicit_recovery")
        self.assertEqual(registry.get(KEY).active_readers, 1)
        self.assertEqual(registry.get(OTHER_KEY).state, "processing")
        with self.assertRaisesRegex(TaskProtocolConflict, "live result readers"):
            registry.recoverable_payloads()
        registry.release_result(KEY)
        self.assertEqual(registry.get(KEY).active_readers, 0)
        self.assertEqual(lab.disk_records()[KEY]["active_readers"], 0)
        self.assertEqual(len(registry.recoverable_payloads()), 2)

    def test_reader_mutation_uncertainty_requires_cold_restart(self) -> None:
        for operation in ("acquire_result", "release_result"):
            with self.subTest(operation=operation):
                lab = self.lab()
                registry = lab.open()
                stage = "leased" if operation == "acquire_result" else "acquired"
                drive(lab, registry, stage)
                mutate = getattr(registry, operation)
                with instance_hook(
                    registry, "_fsync_parent_descriptor", CountedFault(PERMANENT)
                ), self.assertRaises(TaskRegistryPersistenceError) as raised:
                    mutate(KEY)
                self.assert_outcome(
                    raised.exception, outcome="durability_uncertain", committed=False
                )
                status = registry.persistence_status()
                self.assertEqual(status["state"], "durability_uncertain")
                self.assertEqual(status["recovery_action"], "restart_registry_process")
                with self.assertRaises(TaskRegistryPersistenceError) as refused:
                    registry.recover_persistence_uncertainty()
                self.assert_outcome(
                    refused.exception,
                    outcome="durability_uncertain",
                    committed=False,
                    phase_prefix="reader_mutation_requires_cold_restart",
                )
                self.assertEqual(refused.exception.operation, operation)
                self.assertEqual(registry.persistence_status()["state"], "durability_uncertain")
                with self.assertRaises(TaskRegistryPersistenceError):
                    registry.release_result(KEY)
                with self.assertRaises(TaskRegistryPersistenceError):
                    registry.get(KEY)
                cold = lab.open()
                self.assertEqual(cold.persistence_status()["state"], "healthy")
                self.assertEqual(cold.get(KEY).state, "completed")
                self.assertEqual(cold.get(KEY).active_readers, 0)
                self.assertTrue(cold.acquire_result(KEY).is_file())
                self.assertEqual(cold.get(KEY).active_readers, 1)

    def test_committed_reader_cleanup_errors_return_the_durable_outcome(self) -> None:
        lab = self.lab()
        registry = lab.open()
        facts = drive(lab, registry, "leased")
        cleanup_fault = fail_with("temp cleanup failed after commit")
        with instance_hook(registry, "_cleanup_temp_path", cleanup_fault):
            self.assertEqual(registry.acquire_result(KEY), facts["result"][0])
        self.assertEqual(registry.get(KEY).active_readers, 1)
        self.assertEqual(lab.disk_records()[KEY]["active_readers"], 1)
        status = registry.persistence_status()
        self.assertEqual(status["state"], "degraded")
        self.assertEqual(status["recovery_action"], "do_not_retry_committed_operation")
        self.assertEqual(status["last_event"]["operation"], "acquire_result")
        with self.assertRaises(TaskRegistryPersistenceError) as raised:
            registry.assert_persistence_healthy()
        self.assert_outcome(
            raised.exception, outcome="committed_cleanup_failed", committed=True
        )
        with self.assertRaises(TaskProtocolConflict):
            registry.acknowledge(KEY)
        with instance_hook(registry, "_cleanup_temp_path", cleanup_fault):
            self.assertIsNone(registry.release_result(KEY))
        self.assertEqual(registry.get(KEY).active_readers, 0)
        self.assertEqual(lab.disk_records()[KEY]["active_readers"], 0)
        registry.lease(KEY, seconds=1)
        self.assert_healthy(registry)

    def test_acquisition_failure_release_failure_and_underflow_preserve_counts(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "leased")
        with pre_commit_fault(registry, "write"), self.assertRaises(
            TaskRegistryPersistenceError
        ) as raised:
            registry.acquire_result(KEY)
        self.assert_outcome(raised.exception, outcome="not_committed", committed=False)
        self.assertEqual(registry.get(KEY).active_readers, 0)
        self.assertEqual(lab.disk_records()[KEY]["active_readers"], 0)
        disk = lab.disk_bytes()
        with self.assertRaisesRegex(RuntimeError, "underflow"):
            registry.release_result(KEY)
        self.assertEqual(registry.get(KEY).active_readers, 0)
        self.assertEqual(lab.disk_bytes(), disk)

        registry.acquire_result(KEY)
        self.assertEqual(lab.disk_records()[KEY]["active_readers"], 1)
        with pre_commit_fault(registry, "flush"), self.assertRaises(
            TaskRegistryPersistenceError
        ):
            registry.release_result(KEY)
        self.assertEqual(registry.get(KEY).active_readers, 1)
        self.assertEqual(lab.disk_records()[KEY]["active_readers"], 1)
        with self.assertRaisesRegex(TaskProtocolConflict, "in use"):
            registry.acknowledge(KEY)
        self.assertEqual(registry.get(KEY).state, "completed")
        registry.release_result(KEY)
        self.assertEqual(registry.get(KEY).active_readers, 0)
        self.assertEqual(lab.open().get(KEY).active_readers, 0)
        registry.acknowledge(KEY)
        self.assertEqual(registry.get(KEY).state, "cleanup_pending")

    def test_open_result_preserves_primary_error_when_release_also_fails(self) -> None:
        lab = self.lab()
        registry = lab.open()
        facts = drive(lab, registry, "leased")
        with registry.open_result(KEY) as path:
            self.assertEqual(path, facts["result"][0])
            self.assertEqual(registry.get(KEY).active_readers, 1)
        self.assertEqual(registry.get(KEY).active_readers, 0)

        original_write = DurableTaskRegistry._write_registry_stream
        writes: list[int] = []

        def fail_release_persist(stream: object, payload: bytes) -> None:
            writes.append(1)
            if len(writes) == 2:
                raise SyntheticStorageFault("release persist failed")
            original_write(stream, payload)

        with instance_hook(registry, "_write_registry_stream", fail_release_persist), self.assertRaises(
            ValueError
        ) as raised:
            with registry.open_result(KEY):
                self.assertEqual(registry.get(KEY).active_readers, 1)
                raise ValueError("synthetic download failure")
        self.assertEqual(len(writes), 2)
        self.assertEqual(str(raised.exception), "synthetic download failure")
        self.assertIsInstance(raised.exception.__cause__, TaskRegistryPersistenceError)
        self.assertTrue(
            any(
                "release also failed" in note
                for note in getattr(raised.exception, "__notes__", [])
            )
        )
        self.assertEqual(registry.get(KEY).active_readers, 1)
        self.assertEqual(lab.disk_records()[KEY]["active_readers"], 1)
        registry.release_result(KEY)
        self.assertEqual(registry.get(KEY).active_readers, 0)

        with self.assertRaises(ValueError) as plain:
            with registry.open_result(KEY):
                raise ValueError("release succeeds")
        self.assertIsNone(plain.exception.__cause__)
        self.assertEqual(registry.get(KEY).active_readers, 0)


class CleanupIntentTests(_RegistryCase):
    def test_cleanup_retry_handles_disappeared_result_and_upload_tree(self) -> None:
        with self.subTest(missing="result_and_uploads"):
            lab = self.lab()
            registry = lab.open()
            facts = drive(lab, registry, "acknowledged")
            facts["result"][0].unlink()
            shutil.rmtree(lab.task_dir(TASK) / "uploads")
            self.assertEqual(registry.cleanup_consumed(), 1)
            self.assertEqual(registry.get(KEY).state, "consumed")
            self.assertFalse(lab.task_dir(TASK).exists())
            self.assertEqual(registry.unacked_result_bytes, 0)
            self.assertEqual(lab.open().get(KEY).state, "consumed")
        with self.subTest(missing="whole_task_directory"):
            lab = self.lab()
            registry = lab.open()
            drive(lab, registry, "acknowledged")
            shutil.rmtree(lab.task_dir(TASK))
            self.assertEqual(registry.cleanup_consumed(), 1)
            self.assertEqual(registry.get(KEY).state, "consumed")
            self.assertEqual(lab.disk_records()[KEY]["state"], "consumed")
            self.assertIsNone(lab.disk_records()[KEY]["result_path"])

    def test_cleanup_retry_rejects_renamed_upload_inode(self) -> None:
        lab = self.lab()
        registry = lab.open()
        facts = drive(lab, registry, "acknowledged")
        size = facts["result"][2]
        uploads = lab.task_dir(TASK) / "uploads"
        moved = lab.task_dir(TASK) / "uploads-moved"
        uploads.rename(moved)
        with self.assertRaisesRegex(TaskProtocolConflict, "renamed"):
            registry.cleanup_consumed()
        record = registry.get(KEY)
        self.assertEqual(record.state, "cleanup_pending")
        self.assertEqual(record.result_sha256, facts["result"][1])
        self.assertTrue(facts["result"][0].exists())
        self.assertTrue((moved / "source.pdf").exists())
        self.assertEqual(registry.unacked_result_bytes, size)
        self.assertEqual(lab.disk_records()[KEY]["state"], "cleanup_pending")

        with self.subTest(renamed="task_directory"):
            task_moved = lab.output_root / "task-moved"
            moved.rename(uploads)
            lab.task_dir(TASK).rename(task_moved)
            with self.assertRaisesRegex(TaskProtocolConflict, "renamed"):
                registry.cleanup_consumed()
            self.assertEqual(registry.get(KEY).state, "cleanup_pending")
            self.assertTrue((task_moved / "result.zip").exists())
            task_moved.rename(lab.task_dir(TASK))

        self.assertEqual(registry.cleanup_consumed(), 1)
        self.assertEqual(registry.get(KEY).state, "consumed")
        self.assertFalse(lab.task_dir(TASK).exists())
        self.assertEqual(registry.unacked_result_bytes, 0)

    def test_cleanup_persist_failure_retains_intent_capacity_and_retries(self) -> None:
        lab = self.lab()
        registry = lab.open()
        facts = drive(lab, registry, "acknowledged")
        size = facts["result"][2]
        self.assertEqual(registry.unacked_result_bytes, size)
        with pre_commit_fault(registry, "write"), self.assertRaises(
            TaskRegistryPersistenceError
        ) as raised:
            registry.cleanup_consumed()
        self.assert_outcome(raised.exception, outcome="not_committed", committed=False)
        self.assertEqual(raised.exception.operation, "cleanup_consumed")
        record = registry.get(KEY)
        self.assertEqual(record.state, "cleanup_pending")
        self.assertEqual(record.cleanup_kind, "result")
        self.assertEqual(
            (record.result_sha256, record.result_bytes, record.result_owner),
            (facts["result"][1], size, facts["result"][3]),
        )
        self.assertEqual(registry.unacked_result_bytes, size)
        self.assertEqual(lab.disk_records()[KEY]["state"], "cleanup_pending")
        self.assertFalse(lab.task_dir(TASK).exists())
        self.assertEqual(registry.cleanup_consumed(), 1)
        self.assertEqual(registry.get(KEY).state, "consumed")
        self.assertEqual(registry.unacked_result_bytes, 0)
        self.assertEqual(lab.open().get(KEY).state, "consumed")

    def test_cleanup_syncs_namespaces_before_consumed_capacity_release(self) -> None:
        lab = self.lab()
        registry = lab.open()
        facts = drive(lab, registry, "acknowledged")
        size = facts["result"][2]
        root_inode = os.stat(lab.output_root).st_ino
        task_inode = os.stat(lab.task_dir(TASK)).st_ino
        uploads_inode = os.stat(lab.task_dir(TASK) / "uploads").st_ino
        events: list[tuple[str, int, int]] = []
        original_persist = registry._persist

        def record_fsync(descriptor: int) -> None:
            events.append(("fsync", os.fstat(descriptor).st_ino, registry.unacked_result_bytes))
            os.fsync(descriptor)

        def record_persist() -> None:
            events.append(("persist", 0, registry.get(KEY).result_bytes or 0))
            original_persist()

        with patch.object(
            DurableTaskRegistry, "_fsync_namespace_directory", staticmethod(record_fsync)
        ), instance_hook(registry, "_persist", record_persist):
            self.assertEqual(registry.cleanup_consumed(), 1)

        kinds = [kind for kind, _inode, _charge in events]
        self.assertEqual(kinds.count("persist"), 1)
        self.assertEqual(kinds[-1], "persist")
        fsyncs = [(inode, charge) for kind, inode, charge in events if kind == "fsync"]
        self.assertTrue(fsyncs)
        self.assertTrue(all(charge == size for _inode, charge in fsyncs))
        self.assertEqual(fsyncs[-1][0], root_inode)
        self.assertIn(task_inode, [inode for inode, _charge in fsyncs])
        self.assertIn(uploads_inode, [inode for inode, _charge in fsyncs])
        self.assertEqual(registry.get(KEY).state, "consumed")
        self.assertEqual(registry.unacked_result_bytes, 0)
        self.assertFalse(lab.task_dir(TASK).exists())

    def test_cleanup_namespace_failures_retain_intent_and_retry_absence(self) -> None:
        lab = self.lab()
        registry = lab.open()
        facts = drive(lab, registry, "acknowledged")
        size = facts["result"][2]
        task_inode = os.stat(lab.task_dir(TASK)).st_ino
        original_close = os.close

        def fsync_fails_for_task_dir(descriptor: int) -> None:
            if os.fstat(descriptor).st_ino == task_inode:
                raise SyntheticStorageFault("task directory fsync failed")
            os.fsync(descriptor)

        def close_fails_for_task_dir(descriptor: int) -> None:
            try:
                inode = os.fstat(descriptor).st_ino
            except OSError:
                inode = None
            original_close(descriptor)
            if inode == task_inode:
                raise OSError("synthetic descriptor close failure")

        with patch.object(
            DurableTaskRegistry,
            "_fsync_namespace_directory",
            staticmethod(fsync_fails_for_task_dir),
        ), patch("scripts.windows.mineru_heap_trim_compat.agent_task_protocol_v2.os.close", close_fails_for_task_dir), self.assertRaises(
            SyntheticStorageFault
        ) as raised:
            registry.cleanup_consumed()
        self.assertTrue(
            any(
                "close also failed: OSError" in note
                for note in getattr(raised.exception, "__notes__", [])
            ),
            getattr(raised.exception, "__notes__", None),
        )
        record = registry.get(KEY)
        self.assertEqual(record.state, "cleanup_pending")
        self.assertEqual(record.result_sha256, facts["result"][1])
        self.assertEqual(registry.unacked_result_bytes, size)
        self.assertEqual(lab.disk_records()[KEY]["state"], "cleanup_pending")
        self.assertNotEqual(registry.persistence_status()["state"], "durability_uncertain")
        self.assertTrue(lab.task_dir(TASK).exists())

        with self.subTest(retry="after_task_entry_removed_before_root_barrier"):
            root_inode = os.stat(lab.output_root).st_ino
            root_fsyncs: list[int] = []

            def fail_root_barrier(descriptor: int) -> None:
                if os.fstat(descriptor).st_ino == root_inode:
                    raise SyntheticStorageFault("output root fsync failed")
                os.fsync(descriptor)

            with patch.object(
                DurableTaskRegistry,
                "_fsync_namespace_directory",
                staticmethod(fail_root_barrier),
            ), self.assertRaises(SyntheticStorageFault):
                registry.cleanup_consumed()
            self.assertFalse(lab.task_dir(TASK).exists())
            self.assertEqual(registry.get(KEY).state, "cleanup_pending")
            self.assertEqual(registry.unacked_result_bytes, size)

            def observe_root_barrier(descriptor: int) -> None:
                if os.fstat(descriptor).st_ino == root_inode:
                    root_fsyncs.append(descriptor)
                os.fsync(descriptor)

            with patch.object(
                DurableTaskRegistry,
                "_fsync_namespace_directory",
                staticmethod(observe_root_barrier),
            ):
                self.assertEqual(registry.cleanup_consumed(), 1)
            self.assertEqual(len(root_fsyncs), 1)
            self.assertEqual(registry.get(KEY).state, "consumed")
            self.assertEqual(registry.unacked_result_bytes, 0)
            self.assertEqual(lab.open().get(KEY).state, "consumed")

    def test_partial_cleanup_deletion_failure_retains_identity_and_retries(self) -> None:
        lab = self.lab()
        registry = lab.open()
        facts = drive(lab, registry, "acknowledged")
        result_path, digest, size, owner = facts["result"]
        original_remove = DurableTaskRegistry.__dict__["_remove_at"].__func__
        refused: list[str] = []

        def refuse_result_once(cls: type, parent_fd: int, name: str) -> None:
            if name == "result.zip" and not refused:
                refused.append(name)
                raise SyntheticStorageFault("result unlink failed")
            original_remove(cls, parent_fd, name)

        with patch.object(
            DurableTaskRegistry, "_remove_at", classmethod(refuse_result_once)
        ), self.assertRaises(SyntheticStorageFault):
            registry.cleanup_consumed()
        self.assertEqual(refused, ["result.zip"])
        self.assertTrue(result_path.exists())
        record = registry.get(KEY)
        self.assertEqual(record.state, "cleanup_pending")
        self.assertEqual(record.cleanup_kind, "result")
        self.assertEqual((record.result_sha256, record.result_bytes, record.result_owner), (digest, size, owner))
        self.assertEqual(registry.unacked_result_bytes, size)
        self.assertEqual(lab.disk_records()[KEY]["state"], "cleanup_pending")
        self.assertEqual(registry.cleanup_consumed(), 1)
        self.assertFalse(lab.task_dir(TASK).exists())
        self.assertEqual(registry.get(KEY).state, "consumed")
        self.assertEqual(registry.unacked_result_bytes, 0)


class ExecutorTests(_RegistryCase):
    @staticmethod
    def executor() -> SplitTaskExecutor:
        # One executor per event loop: its semaphores bind to the running loop.
        return SplitTaskExecutor(
            parse_slots=1, finalizer_slots=1, result_reservation_bytes=FINALIZER_BUDGET
        )

    def test_executor_does_not_overwrite_persistence_failure_as_parse_failure(self) -> None:
        async def parse_ok() -> None:
            return None

        with self.subTest(failure="first_transition"):
            lab = self.lab()
            registry = lab.open()
            drive(lab, registry, "bound")

            parsed: list[str] = []
            finalized: list[str] = []
            marker = SyntheticStorageFault("processing transition write failed")
            original_write = DurableTaskRegistry._write_registry_stream

            def fail_processing_write(stream: object, payload: bytes) -> None:
                records = json.loads(payload)["records"]
                record = next(item for item in records if item["idempotency_key"] == KEY)
                if record["state"] == "processing":
                    raise marker
                original_write(stream, payload)

            async def parse_first() -> None:
                parsed.append("parse")

            async def finalize() -> tuple[Path, str, int, str]:
                finalized.append("finalize")
                return lab.make_result(TASK)

            with instance_hook(registry, "_write_registry_stream", fail_processing_write), self.assertRaises(
                TaskRegistryPersistenceError
            ) as raised:
                asyncio.run(
                    self.executor().run(
                        registry=registry, key=KEY, parse=parse_first, finalize=finalize
                    )
                )
            self.assert_outcome(raised.exception, outcome="not_committed", committed=False,
                                phase_prefix="write")
            self.assertEqual(raised.exception.operation, "transition")
            self.assertIs(raised.exception.__cause__, marker)
            self.assertEqual((parsed, finalized), ([], []))
            record = registry.get(KEY)
            self.assertEqual((record.state, record.error), ("pending", None))
            self.assertEqual(record.reserved_result_bytes, FINALIZER_BUDGET)

        with self.subTest(failure="after_parse_before_finalizing"):
            lab = self.lab()
            registry = lab.open()
            drive(lab, registry, "bound")
            original_write = DurableTaskRegistry._write_registry_stream
            parsed = []
            finalized = []
            after_parse_disk: list[bytes | None] = []
            marker = SyntheticStorageFault("finalizing transition write failed after parse")

            def fail_finalizing_write(stream: object, payload: bytes) -> None:
                records = json.loads(payload)["records"]
                record = next(item for item in records if item["idempotency_key"] == KEY)
                if record["state"] == "finalizing":
                    self.assertEqual(parsed, ["parse"])
                    raise marker
                original_write(stream, payload)

            async def parse_second() -> None:
                self.assertEqual(registry.get(KEY).state, "processing")
                self.assertEqual(registry.reserved_result_bytes, FINALIZER_BUDGET)
                parsed.append("parse")
                after_parse_disk.append(lab.disk_bytes())

            async def finalize_two() -> tuple[Path, str, int, str]:
                finalized.append("finalize")
                return lab.make_result(TASK)

            with instance_hook(registry, "_write_registry_stream", fail_finalizing_write), self.assertRaises(
                TaskRegistryPersistenceError
            ) as raised:
                asyncio.run(
                    self.executor().run(
                        registry=registry, key=KEY, parse=parse_second, finalize=finalize_two
                    )
                )
            self.assert_outcome(raised.exception, outcome="not_committed", committed=False,
                                phase_prefix="write")
            self.assertEqual(raised.exception.operation, "transition")
            self.assertIs(raised.exception.__cause__, marker)
            self.assertEqual((parsed, finalized), (["parse"], []))
            self.assertEqual(after_parse_disk, [lab.disk_bytes()])
            record = registry.get(KEY)
            self.assertEqual((record.state, record.error), ("processing", None))
            self.assertEqual(record.reserved_result_bytes, FINALIZER_BUDGET)

        with self.subTest(failure="ordinary_parse_error"):
            lab = self.lab()
            registry = lab.open()
            drive(lab, registry, "bound")
            partial_path = lab.task_dir(TASK) / "partial-parser-output"
            marker = RuntimeError("synthetic parse failure")
            finalized = []

            async def parse_broken() -> None:
                partial_path.write_bytes(b"synthetic owned partial output")
                raise marker

            async def finalize_three() -> tuple[Path, str, int, str]:
                finalized.append("finalize")
                return lab.make_result(TASK)

            with self.assertRaisesRegex(RuntimeError, "synthetic parse failure") as raised:
                asyncio.run(
                    self.executor().run(
                        registry=registry, key=KEY, parse=parse_broken, finalize=finalize_three
                    )
                )
            self.assertIs(raised.exception, marker)
            self.assertEqual(finalized, [])
            record = registry.get(KEY)
            self.assertEqual(record.state, "failed")
            self.assertEqual(json.loads(record.error)["code"], "parse_or_finalize_failed")
            self.assertEqual(record.reserved_result_bytes, FINALIZER_BUDGET)
            self.assertEqual(registry.reserved_result_bytes, FINALIZER_BUDGET)
            self.assertEqual(partial_path.read_bytes(), b"synthetic owned partial output")
            cold = lab.open()
            self.assertEqual(cold.get(KEY).state, "failed")
            self.assertEqual(cold.reserved_result_bytes, FINALIZER_BUDGET)
            cold.acknowledge_failed(KEY)
            self.assertFalse(lab.task_dir(TASK).exists())
            self.assertEqual((cold.get(KEY).state, cold.reserved_result_bytes), ("consumed", 0))
            self.assertEqual(lab.disk_records()[KEY]["reserved_result_bytes"], 0)

        with self.subTest(failure="none"):
            lab = self.lab()
            registry = lab.open()
            drive(lab, registry, "bound")

            async def finalize_four() -> tuple[Path, str, int, str]:
                return lab.make_result(TASK)

            asyncio.run(
                self.executor().run(
                    registry=registry, key=KEY, parse=parse_ok, finalize=finalize_four
                )
            )
            record = registry.get(KEY)
            self.assertEqual(record.state, "completed")
            self.assertEqual(registry.unacked_result_bytes, record.result_bytes)
            self.assertEqual(registry.reserved_result_bytes, 0)


class GeneratedFastApiWaiterTests(_RegistryCase):
    """Live generated waiter/processor code over a real registry; HTTP layer is stubbed."""

    def test_generated_fastapi_waiters_receive_persistence_outcomes(self) -> None:
        for family, fault_name, expected_outcome, expected_recovery in (
            ("not_committed", "write", "not_committed", "retry_idempotent_operation"),
            (
                "durability_uncertain",
                "parent_fsync",
                "durability_uncertain",
                "call recover_persistence_uncertainty",
            ),
        ):
            with self.subTest(family=family):
                lab = self.lab()
                registry = lab.open()
                drive(lab, registry, "bound")
                logger = LoggerStub()
                namespace = load_generated_fast_api(registry=registry, logger=logger)
                aborted = namespace["TaskWaitAbortedError"]
                if fault_name == "write":
                    fault = pre_commit_fault(registry, "write")
                else:
                    fault = instance_hook(
                        registry, "_fsync_parent_descriptor", CountedFault(PERMANENT)
                    )

                async def scenario() -> None:
                    manager = await build_manager(namespace)
                    try:
                        task = TaskStub(TASK, outcome=partial(registry.transition, KEY, "processing"))
                        await manager.submit(task)
                        while task.status != "processing":
                            await asyncio.sleep(0)
                        processor = next(iter(manager.active_tasks))
                        waiter = asyncio.create_task(manager.wait_for_terminal_state(TASK))
                        for _ in range(5):
                            await asyncio.sleep(0)
                        self.assertFalse(waiter.done())
                        other = TaskStub(OTHER_TASK)
                        manager.tasks[OTHER_TASK] = other
                        manager.task_events[OTHER_TASK] = asyncio.Event()

                        with fault:
                            task.release.set()
                            with self.assertRaises(aborted) as raised:
                                await waiter
                            with suppress(BaseException):
                                await processor
                            failure = processor.exception()
                            self.assertIsInstance(failure, TaskRegistryPersistenceError)
                            self.assertIs(raised.exception.__cause__, failure)
                            self.assertEqual(failure.outcome, expected_outcome)
                            self.assertIn(f"outcome={expected_outcome}", str(raised.exception))
                            self.assertIn(f"recovery={expected_recovery}", str(raised.exception))
                            self.assertEqual(task.status, "processing")
                            self.assertIsNone(task.error)
                            self.assertIs(manager.task_wait_failures[TASK], failure)
                            self.assertFalse(manager.task_events[OTHER_TASK].is_set())
                            self.assertTrue(manager.task_events[TASK].is_set())
                            self.assertTrue(
                                any("remains nonterminal" in message for message in logger.messages)
                            )
                            before = len(asyncio.all_tasks())
                            with self.assertRaises(aborted) as late:
                                await manager.wait_for_terminal_state(TASK)
                            self.assertIs(late.exception.__cause__, failure)
                            self.assertEqual(len(asyncio.all_tasks()), before)
                            self.assertEqual(manager.queue._unfinished_tasks, 0)
                    finally:
                        await stop_manager(manager)

                asyncio.run(asyncio.wait_for(scenario(), timeout=10))
                if family == "not_committed":
                    self.assertEqual(registry.get(KEY).state, "pending")
                else:
                    self.assertEqual(registry.persistence_status()["state"], "durability_uncertain")
                    registry.recover_persistence_uncertainty()
                    self.assertEqual(registry.get(KEY).state, "processing")

        with self.subTest(family="ordinary_parse_error_stays_terminal"):
            lab = self.lab()
            registry = lab.open()
            drive(lab, registry, "bound")
            namespace = load_generated_fast_api(registry=registry)

            def explode() -> None:
                raise RuntimeError("synthetic parse failure")

            async def ordinary() -> None:
                manager = await build_manager(namespace)
                try:
                    task = TaskStub(TASK, outcome=explode)
                    await manager.submit(task)
                    waiter = asyncio.create_task(manager.wait_for_terminal_state(TASK))
                    for _ in range(5):
                        await asyncio.sleep(0)
                    task.release.set()
                    terminal = await waiter
                    self.assertIs(terminal, task)
                    self.assertEqual((task.status, task.error), ("failed", "synthetic parse failure"))
                    self.assertEqual(manager.task_wait_failures, {})
                    self.assertIs(await manager.wait_for_terminal_state(TASK), task)
                    await manager.shutdown()
                    self.assertEqual(manager.queue._unfinished_tasks, 0)
                finally:
                    await stop_manager(manager)

            asyncio.run(asyncio.wait_for(ordinary(), timeout=10))
            self.assertEqual(registry.get(KEY).state, "pending")

        with self.subTest(family="start_clears_only_the_process_local_map"):
            lab = self.lab()
            registry = lab.open()
            namespace = load_generated_fast_api(registry=registry)

            async def restart() -> None:
                manager = await build_manager(namespace)
                try:
                    manager.task_wait_failures["stale"] = TaskRegistryPersistenceError(
                        operation="transition", phase="write", outcome="not_committed", committed=False
                    )
                    manager.tasks["stale"] = TaskStub("stale")
                    await stop_manager(manager)
                    await manager.start()
                    self.assertEqual(manager.task_wait_failures, {})
                    self.assertIn("stale", manager.tasks)
                finally:
                    await stop_manager(manager)

            asyncio.run(asyncio.wait_for(restart(), timeout=10))

    def test_generated_fastapi_concurrent_waiter_cancellation_interleaving(self) -> None:
        lab = self.lab()
        registry = lab.open()
        drive(lab, registry, "bound")
        namespace = load_generated_fast_api(registry=registry)
        aborted = namespace["TaskWaitAbortedError"]
        self.assertIn("wait_helpers = (event_wait_task, manager_wait_task)", namespace["__generated_source__"])
        self.assertNotIn("done, pending = await asyncio.wait", namespace["__generated_source__"])

        async def helpers_of(baseline: set[asyncio.Task[object]], waiter: asyncio.Task[object]) -> set[asyncio.Task[object]]:
            for _ in range(50):
                await asyncio.sleep(0)
                helpers = {
                    task for task in asyncio.all_tasks() if task not in baseline and task is not waiter
                }
                if len(helpers) == 2:
                    return helpers
            raise AssertionError("waiter never created both helper tasks")

        async def scenario() -> None:
            manager = await build_manager(namespace)
            try:
                task = TaskStub(TASK, outcome=partial(registry.transition, KEY, "processing"))
                await manager.submit(task)
                while task.status != "processing":
                    await asyncio.sleep(0)
                baseline = set(asyncio.all_tasks())

                with self.subTest(interleaving="outer_cancel_while_wait_suspended"):
                    waiter = asyncio.create_task(manager.wait_for_terminal_state(TASK))
                    helpers = await helpers_of(baseline, waiter)
                    self.assertTrue(all(not helper.done() for helper in helpers))
                    waiter.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await waiter
                    self.assertTrue(all(helper.done() and helper.cancelled() for helper in helpers))
                    await asyncio.sleep(0)
                    self.assertEqual(set(asyncio.all_tasks()) - baseline, set())
                    self.assertEqual(task.status, "processing")

                with self.subTest(interleaving="second_waiter_survives_first_cancellation"):
                    first = asyncio.create_task(manager.wait_for_terminal_state(TASK))
                    first_helpers = await helpers_of(baseline, first)
                    second = asyncio.create_task(manager.wait_for_terminal_state(TASK))
                    second_helpers = await helpers_of(baseline | first_helpers | {first}, second)
                    first.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await first
                    self.assertTrue(all(helper.cancelled() for helper in first_helpers))
                    self.assertTrue(all(not helper.done() for helper in second_helpers))
                    self.assertFalse(second.done())
                    task.release.set()
                    self.assertIs(await second, task)
                    self.assertEqual(task.status, "completed")
                    self.assertTrue(all(helper.done() for helper in second_helpers))
                    await asyncio.sleep(0)
                    self.assertEqual(set(asyncio.all_tasks()) - baseline, set())

                with self.subTest(interleaving="shutdown_wake_uses_the_same_cleanup_path"):
                    self.assertIs(await manager.wait_for_terminal_state(TASK), task)
                    stuck = TaskStub(OTHER_TASK)
                    await manager.submit(stuck)
                    while stuck.status != "processing":
                        await asyncio.sleep(0)
                    baseline = set(asyncio.all_tasks())
                    waiter = asyncio.create_task(manager.wait_for_terminal_state(OTHER_TASK))
                    helpers = await helpers_of(baseline, waiter)
                    manager.is_shutting_down = True
                    manager._wake_waiters()
                    with self.assertRaisesRegex(aborted, "shutting down"):
                        await waiter
                    self.assertTrue(all(helper.done() for helper in helpers))
                    await asyncio.sleep(0)
                    self.assertEqual(set(asyncio.all_tasks()) - baseline, set())
                    self.assertEqual(stuck.status, "processing")
                    stuck.release.set()
                    while stuck.status != "completed":
                        await asyncio.sleep(0)
            finally:
                await stop_manager(manager)

        asyncio.run(asyncio.wait_for(scenario(), timeout=10))
        self.assertEqual(registry.get(KEY).state, "processing")


class RealPreimageTests(unittest.TestCase):
    def test_real_fastapi_preimage_has_closed_persistence_integration(self) -> None:
        relative = "mineru/cli/fast_api.py"
        preimage = Path(__file__).resolve().parents[1] / "fixtures" / "mineru_344_preimages" / relative
        if not preimage.is_file():
            self.skipTest(
                "missing precondition: pinned MinerU 3.4.4 preimage "
                "tests/fixtures/mineru_344_preimages/mineru/cli/fast_api.py is not present"
            )
        payload = preimage.read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), TARGET_PREIMAGE_SHA256[relative])
        source = payload.decode("utf-8")
        preimage_tree = ast.parse(source)
        self.assertIn(
            "TaskWaitAbortedError",
            {node.name for node in preimage_tree.body if isinstance(node, ast.ClassDef)},
        )
        generated = patch_source(relative, source)
        compile(generated, relative, "exec")
        tree = ast.parse(generated)

        import_block = next(
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "mineru.cli.agent_task_protocol_v2"
        )
        self.assertTrue({"TaskRegistryPersistenceError", "TaskResultCapacityRecoveryRequired"}
                        <= {alias.name for alias in import_block.names})
        self.assertEqual(generated.count("self._raise_task_wait_failure(task_id)"), 2)
        failure_maps = [node for node in ast.walk(tree)
                        if isinstance(node, ast.AnnAssign)
                        and isinstance(node.target, ast.Attribute)
                        and isinstance(node.target.value, ast.Name)
                        and node.target.value.id == "self" and node.target.attr == "task_wait_failures"]
        self.assertEqual(len(failure_maps), 1)
        expected_annotation = ast.parse(
            "dict[str, TaskRegistryPersistenceError | TaskResultCapacityRecoveryRequired]", mode="eval"
        ).body
        self.assertEqual(ast.dump(failure_maps[0].annotation), ast.dump(expected_annotation))
        self.assertEqual(ast.dump(failure_maps[0].value), ast.dump(ast.Dict(keys=[], values=[])))
        self.assertIn("self.task_wait_failures.clear()", generated)
        self.assertIn("self.task_wait_failures[task_id] = exc", generated)
        self.assertIn("task status remains nonterminal", generated)
        self.assertIn("wait_helpers = (event_wait_task, manager_wait_task)", generated)
        self.assertIn("await asyncio.gather(*wait_helpers, return_exceptions=True)", generated)
        self.assertNotIn("done, pending = await asyncio.wait", generated)

        manager = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AsyncTaskManager"
        )
        methods = {node.name: node for node in manager.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertIn("_raise_task_wait_failure", methods)
        processor = methods["_process_task"]
        handlers = [
            handler
            for node in ast.walk(processor)
            if isinstance(node, ast.Try)
            for handler in node.handlers
        ]
        type_names = [{node.id for node in ast.walk(handler.type) if isinstance(node, ast.Name)}
                      if handler.type is not None else set() for handler in handlers]
        persistence_positions = [index for index, names in enumerate(type_names)
                                 if "TaskRegistryPersistenceError" in names]
        self.assertEqual(len(persistence_positions), 1)
        position = persistence_positions[0]
        self.assertEqual(type_names[position],
                         {"TaskRegistryPersistenceError", "TaskResultCapacityRecoveryRequired"})
        self.assertLess(position, type_names.index({"Exception"}))

        conflict_handlers = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            typed = [ast.unparse(handler.type) if handler.type is not None else None for handler in node.handlers]
            for position, handler in enumerate(node.handlers):
                if typed[position] == "TaskProtocolConflict" and handler.name == "exc":
                    conflict_handlers += 1
                    self.assertIn("TaskRegistryPersistenceError", typed[:position])
                    persistence = node.handlers[typed.index("TaskRegistryPersistenceError")]
                    self.assertIn("status_code=503", ast.unparse(persistence))
        self.assertGreaterEqual(conflict_handlers, 1)

        allocation = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_async_parse_task"
        )
        allocation_source = ast.get_source_segment(generated, allocation) or ""
        self.assertNotIn("with suppress(TaskProtocolConflict):", allocation_source)
        definitions = {node.name: node for node in tree.body
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        expected_calls = ("begin_submission", "create_task_output_dir", "save_upload_files",
                          "bind_task_payload", "submit", "abort_ingress")
        calls = {name: [] for name in expected_calls}
        for node in ast.walk(allocation):
            if not isinstance(node, ast.Call):
                continue
            name = (node.func.id if isinstance(node.func, ast.Name)
                    else node.func.attr if isinstance(node.func, ast.Attribute) else None)
            if name == "call" and node.args:
                target = node.args[0]
                name = (target.id if isinstance(target, ast.Name)
                        else target.attr if isinstance(target, ast.Attribute) else None)
            if name in calls:
                calls[name].append(node.lineno)
            if name == "_prepare_ingress_tree":
                helper = definitions[name]
                direct = [item for item in ast.walk(helper) if isinstance(item, ast.Call)
                          and isinstance(item.func, ast.Name) and item.func.id == "create_task_output_dir"]
                self.assertEqual(len(direct), 1)
                self.assertNotIn("cleanup_file", ast.get_source_segment(generated, helper))
                calls["create_task_output_dir"].append(node.lineno)
        for name, lines in calls.items():
            self.assertEqual(len(lines), 1, name)
        ordered = ("begin_submission", "create_task_output_dir", "save_upload_files",
                   "bind_task_payload", "submit")
        for earlier, later in zip(ordered, ordered[1:]):
            self.assertLess(calls[earlier][0], calls[later][0])
        self.assertNotIn("cleanup_file(task_output_dir)", allocation_source)
        self.assertIn("isinstance(exc, TaskRegistryPersistenceError)", allocation_source)
        self.assertIn("status_code=503", allocation_source)


if __name__ == "__main__":
    unittest.main()
