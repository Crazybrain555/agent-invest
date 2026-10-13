"""Independent bounded recovery witness for a storage-managed accepted task."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from disclosure_anchor.application.contracts.mineru_capacity_config import (
    MineruResultStoragePolicy,
    retained_zip_upper_bound,
)
from scripts.windows.mineru_heap_trim_compat.agent_task_protocol_v2 import (
    DurableTaskRegistry,
    RETAINED_INVENTORY_NAME,
    RETAINED_RESULT_NAME,
    ResultInventory,
    ResultSelection,
    SourceGrowthPermit,
    SplitTaskExecutor,
    TaskExecutionStopped,
    TaskProtocolConflict,
    TaskStorageWait,
    build_result_inventory,
    load_inventory_file,
    verify_result_inventory,
    write_inventory_file,
)
from tests._m6_registry_lab import RegistryLab, UNACKED_LIMIT, result_owner
from tests.unit.test_result_storage_policy_independent import policy_values


MIB = 1024 * 1024
KEY = "original-accepted-key"
TASK = "original-accepted-task"
SOURCE = b"synthetic selected content" * 3


def _policy() -> MineruResultStoragePolicy:
    # P=4MiB, C=4MiB: one accepted source plus one 2MiB producer
    # leaves C free, but cannot grant a second producer from P.
    return MineruResultStoragePolicy(**policy_values(
        native_source_pool_bytes=4 * MIB,
        native_growing_producer_limit=3,
    ))


def _accept(lab: RegistryLab, registry: DurableTaskRegistry, key: str, task: str) -> None:
    record, created = registry.reconcile_or_create(
        idempotency_key=key, task_id=task,
        attempt_identity="attempt-" + key, fence_identity="fence-" + key,
    )
    if not created or record.task_id != task:
        raise AssertionError("synthetic task did not retain its original acceptance")
    registry.bind_task_payload(key, lab.make_task_tree(task, upload=b"synthetic input"))


class NativeStorageRecoveryIndependentTests(unittest.TestCase):
    def test_sealed_wait_abort_reopen_finish_and_escrow_isolation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="native-storage-recovery-independent-") as scratch:
            root = Path(scratch)
            lab = RegistryLab(root / "managed")
            policy = _policy()
            registry = DurableTaskRegistry(
                lab.registry_path, output_root=lab.output_root, storage_policy=policy,
            )
            _accept(lab, registry, KEY, TASK)
            task_root = lab.task_dir(TASK)
            selected = task_root / "parse" / "data.txt"
            selections = (ResultSelection(
                pdf_name="synthetic-input.pdf", parse_dir_parts=("parse",),
                arc_prefix="synthetic", named_files=("data.txt",),
                image_suffixes=None, origin_prefix=None,
            ),)
            parse_calls = [0]
            reopen_calls = [0]
            finalize_calls = [0]

            async def parse(permit: SourceGrowthPermit) -> None:
                parse_calls[0] += 1
                selected.parent.mkdir()
                permit.before_write(str(selected), len(SOURCE))
                selected.write_bytes(SOURCE)

            async def seal() -> ResultInventory:
                return build_result_inventory(
                    task_id=TASK, task_root=task_root,
                    root_identity=registry.task_root_identity(KEY),
                    selections=selections, policy=policy,
                    zip_upper_bound=retained_zip_upper_bound,
                )

            async def write_seal(inventory: ResultInventory) -> str:
                return write_inventory_file(
                    task_root, registry.task_root_identity(KEY), inventory,
                )

            async def reopen_seal(expected_sha256: str) -> ResultInventory:
                reopen_calls[0] += 1
                inventory = load_inventory_file(
                    task_root, reopened.task_root_identity(KEY),
                    expected_sha256=expected_sha256, task_id=TASK, policy=policy,
                )
                verify_result_inventory(
                    task_root=task_root, root_identity=reopened.task_root_identity(KEY),
                    inventory=inventory, selections=selections,
                )
                return inventory

            async def never_parse(_permit: SourceGrowthPermit) -> None:
                parse_calls[0] += 1
                raise AssertionError("sealed source was reparsed")

            async def never_seal() -> ResultInventory:
                raise AssertionError("sealed source was resealed")

            async def finalize(inventory: ResultInventory, grant: int) -> tuple[Path, str, int, str]:
                finalize_calls[0] += 1
                path = task_root / RETAINED_RESULT_NAME
                with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr(inventory.members[0].arcname, SOURCE)
                exact = path.read_bytes()
                self.assertLessEqual(len(exact), grant)
                digest = hashlib.sha256(exact).hexdigest()
                return path, digest, len(exact), result_owner(TASK, digest, len(exact))

            executor = SplitTaskExecutor(
                parse_slots=1, finalizer_slots=1, storage_policy=policy,
                storage_rescan_seconds=0.02,
            )
            probe_count = [0]

            async def initially_low_completion_free() -> int:
                probe_count[0] += 1
                return 100 * MIB if probe_count[0] == 1 else policy.native_free_floor_bytes

            async def abundant_free() -> int:
                return 100 * MIB

            async def wait_until_sealed_waiting() -> None:
                while True:
                    record = registry.get(KEY)
                    if (
                        record is not None and record.storage is not None
                        and record.storage["phase"] == "source_sealed"
                        and record.storage["wait_reason"] == "free_floor"
                        and executor.stage_snapshot()["completion_waiting"] > 0
                    ):
                        return
                    await asyncio.sleep(0.005)

            async def exercise() -> None:
                pending = asyncio.create_task(executor.run_storage(
                    registry=registry, key=KEY, permit_root=task_root,
                    parse=parse, seal=seal, reopen_seal=reopen_seal,
                    write_seal=write_seal, finalize=finalize,
                    probe_free=initially_low_completion_free,
                ))
                try:
                    await asyncio.wait_for(wait_until_sealed_waiting(), timeout=2)
                    stage = executor.stage_snapshot()
                    self.assertEqual(stage["parse_active"], 0)
                    self.assertEqual(stage["finalizer_active"], 0)
                    self.assertEqual(stage["parse_waiting"], 0)
                    self.assertEqual(stage["finalizer_waiting"], 0)
                    self.assertEqual(parse_calls[0], 1)
                    self.assertEqual(finalize_calls[0], 0)
                    self.assertEqual(selected.read_bytes(), SOURCE)
                    self.assertTrue((task_root / RETAINED_INVENTORY_NAME).is_file())
                    waiting = registry.get(KEY)
                    self.assertIsNotNone(waiting)
                    self.assertEqual(waiting.task_id, TASK)
                    self.assertEqual(waiting.state, "finalizing")
                    self.assertEqual(waiting.storage["phase"], "source_sealed")
                    self.assertGreater(waiting.storage["source_bytes"], 0)
                    self.assertEqual(waiting.storage["zip_grant_bytes"], 0)
                    self.assertIsNone(waiting.error)
                    executor.begin_shutdown(abort_pending=True)
                    with self.assertRaises(TaskExecutionStopped):
                        await asyncio.wait_for(pending, timeout=1)
                finally:
                    if not pending.done():
                        executor.begin_shutdown(abort_pending=True)
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)

            asyncio.run(asyncio.wait_for(exercise(), timeout=4))
            held = registry.get(KEY)
            self.assertEqual((held.task_id, held.state), (TASK, "finalizing"))
            self.assertEqual(held.storage["phase"], "source_sealed")
            self.assertEqual(held.storage["wait_reason"], "free_floor")
            self.assertEqual(selected.read_bytes(), SOURCE)

            # Reopen the real v4 registry and its saved source seal. The
            # existing accepted key and task must finish without parse.
            reopened = DurableTaskRegistry(
                lab.registry_path, output_root=lab.output_root, storage_policy=policy,
            )
            replay = reopened.recoverable_payloads()
            self.assertEqual(len(replay), 1)
            self.assertEqual(reopened.get(KEY).task_id, TASK)
            recovered_executor = SplitTaskExecutor(
                parse_slots=1, finalizer_slots=1, storage_policy=policy,
                storage_rescan_seconds=0.02,
            )
            asyncio.run(asyncio.wait_for(recovered_executor.run_storage(
                registry=reopened, key=KEY, permit_root=task_root,
                parse=never_parse, seal=never_seal, reopen_seal=reopen_seal,
                write_seal=write_seal, finalize=finalize, probe_free=abundant_free,
            ), timeout=2))
            completed = reopened.get(KEY)
            self.assertEqual((parse_calls[0], reopen_calls[0], finalize_calls[0]), (1, 1, 1))
            self.assertEqual((completed.task_id, completed.state), (TASK, "completed"))
            self.assertEqual(completed.storage["phase"], "zip_sealed")
            self.assertEqual(completed.storage["zip_bytes"], completed.result_bytes)
            self.assertEqual(selected.read_bytes(), SOURCE)
            self.assertEqual(recovered_executor.stage_snapshot()["parse_active"], 0)
            self.assertEqual(recovered_executor.stage_snapshot()["finalizer_active"], 0)
            persisted = DurableTaskRegistry(
                lab.registry_path, output_root=lab.output_root, storage_policy=policy,
            )
            existing, created = persisted.reconcile_or_create(
                idempotency_key=KEY, task_id=TASK,
                attempt_identity="attempt-" + KEY, fence_identity="fence-" + KEY,
            )
            self.assertFalse(created)
            self.assertEqual(existing.task_id, TASK)

            # Two admitted producers would exceed P even though the unused C
            # escrow could hold the second permit. The real registry decides.
            for number in (1, 2):
                _accept(lab, persisted, f"other-key-{number}", f"other-task-{number}")
            first_permit = persisted.reserve_source_growth(
                "other-key-1", live_free_bytes=100 * MIB,
                outstanding_promise_bytes=0, completion_head_waiting=False,
            )
            self.assertEqual(first_permit, policy.native_source_single_limit_bytes)
            used = persisted.storage_usage()
            self.assertGreater(used["source"] + first_permit, policy.native_source_pool_bytes)
            self.assertLessEqual(
                used["source"] + used["result"] + first_permit,
                policy.native_source_pool_bytes + policy.native_completion_escrow_bytes,
            )
            with self.assertRaises(TaskStorageWait) as refused:
                persisted.reserve_source_growth(
                    "other-key-2", live_free_bytes=100 * MIB,
                    outstanding_promise_bytes=0, completion_head_waiting=False,
                )
            self.assertEqual(refused.exception.reason, "source_growth_capacity")
            self.assertEqual(persisted.get("other-key-2").storage["phase"], "admitted")

            with self.assertRaises(TaskProtocolConflict):
                DurableTaskRegistry(
                    lab.registry_path, output_root=lab.output_root,
                    max_unacked_result_bytes=UNACKED_LIMIT,
                )

            # The old strict v3 reader still accepts its own canonical live
            # record; a v4 reader must not adopt live v3 without a transition.
            old_lab = RegistryLab(root / "legacy")
            old = old_lab.open()
            _accept(old_lab, old, "legacy-key", "legacy-task")
            self.assertEqual(json.loads(old_lab.disk_bytes())["schema"], "mineru-task-registry.v3")
            self.assertEqual(old_lab.open().get("legacy-key").task_id, "legacy-task")
            with self.assertRaises(TaskProtocolConflict):
                DurableTaskRegistry(
                    old_lab.registry_path, output_root=old_lab.output_root,
                    storage_policy=policy,
                )


if __name__ == "__main__":
    unittest.main()
