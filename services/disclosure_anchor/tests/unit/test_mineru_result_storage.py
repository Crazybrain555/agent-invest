"""Physical result storage on the native task protocol (capacity config v2).

Synthetic temporary trees only: no PDF, parser, model, GPU, network or shared
runtime path. Policies are small synthetic allocations, not machine sizing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

from disclosure_anchor.application.contracts.mineru_capacity_config import (
    MineruResultStoragePolicy,
    retained_zip_upper_bound,
)
from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol
from tests._m6_registry_lab import RegistryLab, result_owner

MIB = 1024 * 1024
UPLOAD = b"%PDF-synthetic-placeholder"
SELECTIONS = (
    protocol.ResultSelection(
        pdf_name="doc",
        parse_dir_parts=("doc", "hybrid_auto"),
        arc_prefix="doc/hybrid_auto",
        named_files=("doc.md", "doc_content_list.json"),
        image_suffixes=frozenset({"png", "jpg"}),
        origin_prefix=None,
    ),
)


def storage_policy(**changes: object) -> MineruResultStoragePolicy:
    values: dict[str, object] = {
        "contract_version": "mineru.result-storage-policy.v1",
        "native_volume_identity": "scratch-native-volume",
        "native_volume_total_bytes": 100 * MIB,
        "native_work_disk_limit_bytes": 12 * MIB,
        "native_free_floor_bytes": 20 * MIB,
        "native_source_pool_bytes": 6 * MIB,
        "native_completion_escrow_bytes": 4 * MIB,
        "native_metadata_reserve_bytes": MIB,
        "native_source_single_limit_bytes": 2 * MIB,
        "native_growing_producer_limit": 1,
        "native_result_hard_limit_bytes": 3 * MIB,
        "native_normal_unacked_target_bytes": 2 * MIB,
        "initial_result_estimate_bytes": MIB,
        "native_allocation_unit_bytes": 4096,
        "native_file_overhead_bytes": 4096,
        "source_pdf_bytes_limit": MIB,
        "mac_volume_identity": "scratch-mac-volume",
        "mac_volume_total_bytes": 100 * MIB,
        "mac_work_disk_limit_bytes": 16 * MIB,
        "mac_free_floor_bytes": 20 * MIB,
        "mac_normal_output_target_bytes": 2 * MIB,
        "mac_decode_input_limit_bytes": MIB // 4,
        "mac_decode_working_set_budget_bytes": 2 * MIB,
        "mac_decode_expansion_factor": 4,
        "mac_decode_stage_seconds": 300,
        "max_members": 64,
        "max_name_bytes": 128,
        "max_inventory_bytes": 64 * 1024,
        "transfer_logical_deadline_seconds": 60,
        "progress_window_seconds": 10,
        "minimum_progress_bytes": MIB,
    }
    values.update(changes)
    return MineruResultStoragePolicy(**values)  # type: ignore[arg-type]


PLENTY = 1 << 50  # Synthetic live free bytes far above every floor.


async def until(predicate, *, timeout: float = 5.0) -> None:
    """Poll a durable fact; a regression fails the test instead of hanging it."""
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), timeout)


class StorageLab:
    def __init__(self, case: unittest.TestCase, policy: MineruResultStoragePolicy | None = None) -> None:
        temporary = tempfile.TemporaryDirectory()
        case.addCleanup(temporary.cleanup)
        self.lab = RegistryLab(Path(temporary.name))
        self.policy = policy or storage_policy()

    def open(self) -> protocol.DurableTaskRegistry:
        return self.lab.open(max_unacked_result_bytes=None, storage_policy=self.policy)

    def accept(self, registry: protocol.DurableTaskRegistry, key: str, task_id: str) -> Path:
        registry.reconcile_or_create(
            idempotency_key=key, task_id=task_id,
            attempt_identity=f"attempt-{key}", fence_identity=f"fence-{key}",
        )
        registry.bind_task_payload(key, self.lab.make_task_tree(task_id, upload=UPLOAD))
        return self.lab.task_dir(task_id)

    def executor(self, **overrides: object) -> protocol.SplitTaskExecutor:
        options: dict[str, object] = {
            "parse_slots": 2, "finalizer_slots": 1, "storage_policy": self.policy,
            "storage_rescan_seconds": 0.05,
        }
        options.update(overrides)
        executor = protocol.SplitTaskExecutor(**options)  # type: ignore[arg-type]
        executor.start()
        return executor


def write_output(permit: protocol.SourceGrowthPermit, root: Path, relative: str, data: bytes) -> None:
    """The granted-writer order: charge the whole payload, then create the file."""
    path = root / relative
    permit.before_write(str(path), len(data))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def parse_writing(root: Path, files: dict[str, bytes], *, gate: asyncio.Event | None = None):
    async def parse(permit: protocol.SourceGrowthPermit) -> None:
        if gate is not None:
            await gate.wait()
        for relative, data in files.items():
            write_output(permit, root, relative, data)
    return parse


def stages(executor: protocol.SplitTaskExecutor, registry: protocol.DurableTaskRegistry,
           root: Path, task_id: str, parse):
    identity = registry.task_root_identity(next(
        record.idempotency_key for record in registry.durable_view().records if record.task_id == task_id
    ))

    async def seal():
        return protocol.build_result_inventory(
            task_id=task_id, task_root=root, root_identity=identity, selections=SELECTIONS,
            policy=executor.storage_policy, zip_upper_bound=retained_zip_upper_bound,
        )

    async def write_seal(inventory):
        return protocol.write_inventory_file(root, identity, inventory)

    async def reopen_seal(expected):
        inventory = protocol.load_inventory_file(
            root, identity, expected_sha256=expected, task_id=task_id, policy=executor.storage_policy,
        )
        protocol.verify_result_inventory(
            task_root=root, root_identity=identity, inventory=inventory, selections=SELECTIONS,
        )
        return inventory

    async def finalize(inventory, grant):
        path, digest, size = protocol.write_retained_zip(
            task_root=root, root_identity=identity, inventory=inventory,
            selections=SELECTIONS, grant_bytes=grant,
        )
        return path, digest, size, result_owner(task_id, digest, size)

    async def probe_free():
        return PLENTY

    return dict(
        permit_root=root, parse=parse, seal=seal, reopen_seal=reopen_seal,
        write_seal=write_seal, finalize=finalize, probe_free=probe_free,
    )


OUTPUTS = {
    "doc/hybrid_auto/doc.md": b"# heading\n\nbody\n" * 64,
    "doc/hybrid_auto/doc_content_list.json": json.dumps([{"type": "text", "text": "x"}] * 32).encode(),
    "doc/hybrid_auto/images/a.png": bytes(range(256)) * 16,
    "doc/hybrid_auto/images/b.jpg": b"\xff\xd8synthetic" * 50,
    "doc/hybrid_auto/doc_model.json": b"{}",  # Not selected: never packed.
}


class AcceptedStorageTests(unittest.TestCase):
    def test_acceptance_charges_the_physical_upload_and_writes_registry_v4(self) -> None:
        lab = StorageLab(self)
        registry = lab.open()
        lab.accept(registry, "key-a", "task-a")
        record = registry.get("key-a")
        assert record is not None and record.storage is not None
        self.assertEqual((record.state, record.storage["phase"]), ("pending", "admitted"))
        self.assertEqual(record.storage["upload_bytes"], lab.policy.physical_charge(len(UPLOAD)))
        self.assertEqual(record.storage["policy_sha256"], lab.policy.sha256)
        on_disk = json.loads(lab.lab.disk_bytes() or b"{}")
        self.assertEqual(on_disk["schema"], protocol.REGISTRY_SCHEMA_V4)
        status = protocol.storage_status_payload(record)
        self.assertEqual(set(status or {}), {
            "schema", "policy_sha256", "phase", "wait_reason", "wait_since_unix", "blocked",
            "selected_bytes", "member_count", "inventory_sha256", "zip_bytes",
        })
        self.assertEqual(registry.storage_usage()["source"], record.storage["upload_bytes"])

    def test_upload_over_the_source_limit_is_never_accepted(self) -> None:
        lab = StorageLab(self, storage_policy(source_pdf_bytes_limit=len(UPLOAD) - 1))
        registry = lab.open()
        registry.reconcile_or_create(
            idempotency_key="key-a", task_id="task-a", attempt_identity="a", fence_identity="f",
        )
        with self.assertRaises(protocol.TaskProtocolConflict):
            registry.bind_task_payload("key-a", lab.lab.make_task_tree("task-a", upload=UPLOAD))
        self.assertIsNone(registry.get("key-a").storage)

    def test_ingress_charge_is_released_by_abort(self) -> None:
        lab = StorageLab(self)
        registry = lab.open()
        registry.reconcile_or_create(
            idempotency_key="key-a", task_id="task-a", attempt_identity="a", fence_identity="f",
            max_nonterminal_tasks=4,
        )
        charge = registry.reserve_ingress_storage("key-a", live_free_bytes=PLENTY, outstanding_promise_bytes=0)
        self.assertEqual(charge, lab.policy.physical_charge(lab.policy.source_pdf_bytes_limit))
        self.assertEqual(registry.storage_usage()["ingress"], charge)
        with self.assertRaises(protocol.TaskProtocolConflict):
            registry.reserve_ingress_storage("key-a", live_free_bytes=PLENTY, outstanding_promise_bytes=0)
        registry.abort_ingress("key-a")
        self.assertIsNone(registry.get("key-a"))
        self.assertEqual(registry.storage_usage()["ingress"], 0)

    def test_ingress_waits_on_pool_and_free_floor_instead_of_writing(self) -> None:
        lab = StorageLab(self)
        registry = lab.open()
        registry.reconcile_or_create(
            idempotency_key="key-a", task_id="task-a", attempt_identity="a", fence_identity="f",
            max_nonterminal_tasks=4,
        )
        floor = lab.policy.native_free_floor_bytes
        charge = lab.policy.physical_charge(lab.policy.source_pdf_bytes_limit)
        with self.assertRaises(protocol.TaskStorageWait) as waited:
            registry.reserve_ingress_storage(
                "key-a", live_free_bytes=floor + charge - 1, outstanding_promise_bytes=0,
            )
        self.assertEqual(waited.exception.reason, "free_floor")
        with self.assertRaises(protocol.TaskStorageWait) as promised:
            registry.reserve_ingress_storage(
                "key-a", live_free_bytes=floor + charge, outstanding_promise_bytes=1,
            )
        self.assertEqual(promised.exception.reason, "free_floor")
        self.assertEqual(registry.storage_usage()["ingress"], 0)

    def test_a_legacy_registry_with_live_work_never_opens_in_storage_mode(self) -> None:
        lab = StorageLab(self)
        legacy = lab.lab.open()
        legacy.reconcile_or_create(
            idempotency_key="key-a", task_id="task-a", attempt_identity="a", fence_identity="f",
        )
        legacy.bind_task_payload("key-a", lab.lab.make_task_tree("task-a", upload=UPLOAD))
        with self.assertRaises(protocol.TaskProtocolConflict):
            lab.open()
        # The legacy bytes stay exactly readable by the legacy reader.
        self.assertEqual(lab.lab.open().get("key-a").state, "pending")

    def test_storage_mode_has_no_aggregate_unacked_limit_or_b_reservation(self) -> None:
        lab = StorageLab(self)
        with self.assertRaises(ValueError):
            lab.lab.open(max_unacked_result_bytes=2 * MIB, storage_policy=lab.policy)
        registry = lab.open()
        lab.accept(registry, "key-a", "task-a")
        with self.assertRaises(protocol.TaskProtocolConflict):
            registry.reserve_result_for_parse("key-a", byte_budget=MIB)


class StorageExecutionTests(unittest.TestCase):
    def test_permit_seal_grant_and_zip_complete_one_task(self) -> None:
        lab = StorageLab(self)
        registry = lab.open()
        root = lab.accept(registry, "key-a", "task-a")
        executor = lab.executor()
        asyncio.run(executor.run_storage(
            registry=registry, key="key-a", **stages(executor, registry, root, "task-a", parse_writing(root, OUTPUTS)),
        ))
        record = registry.get("key-a")
        assert record is not None and record.storage is not None
        self.assertEqual((record.state, record.storage["phase"]), ("completed", "zip_sealed"))
        result = Path(record.result_path or "")
        raw = result.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), record.result_sha256)
        self.assertEqual(record.storage["zip_bytes"], len(raw))
        selected = {name: data for name, data in OUTPUTS.items() if not name.endswith("_model.json")}
        with zipfile.ZipFile(result) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(
                {info.filename: archive.read(info) for info in archive.infolist()},
                {name: data for name, data in selected.items()},
            )
        bound = retained_zip_upper_bound(tuple(
            (len(data), len(name.encode())) for name, data in sorted(selected.items())
        ))
        self.assertLessEqual(len(raw), bound)
        self.assertEqual(record.storage["zip_upper_bound_bytes"], bound)
        self.assertEqual(record.storage["selected_bytes"], sum(map(len, selected.values())))
        self.assertEqual(record.storage["member_count"], len(selected))
        inventory = (root / protocol.RETAINED_INVENTORY_NAME).read_bytes()
        self.assertEqual(record.storage["inventory_sha256"], "sha256:" + hashlib.sha256(inventory).hexdigest())
        self.assertFalse((root / protocol.RETAINED_RESULT_PART_NAME).exists())
        usage = registry.storage_usage()
        self.assertEqual(usage["result"], lab.policy.physical_charge(len(raw)))
        self.assertEqual(usage["growing"], 0)
        self.assertEqual(executor.outstanding_promise_bytes(), 0)
        # The source charge is the permit's physical accounting of every
        # parser file and the seal file, not the selected subset.
        self.assertGreaterEqual(
            record.storage["source_bytes"],
            sum(lab.policy.physical_charge(len(data)) for data in OUTPUTS.values())
            + lab.policy.physical_charge(len(inventory)),
        )

    def test_growth_past_the_permit_is_refused_before_the_write_and_held(self) -> None:
        lab = StorageLab(self)
        registry = lab.open()
        root = lab.accept(registry, "key-a", "task-a")
        executor = lab.executor()
        oversized = {"doc/hybrid_auto/images/huge.png": b"\0" * (2 * MIB)}
        with self.assertRaises(protocol.TaskStorageBlocked) as held:
            asyncio.run(executor.run_storage(
                registry=registry, key="key-a",
                **stages(executor, registry, root, "task-a", parse_writing(root, oversized)),
            ))
        self.assertEqual(held.exception.reason, "hard_envelope_exceeded")
        self.assertFalse((root / "doc/hybrid_auto/images/huge.png").exists())
        record = registry.get("key-a")
        assert record is not None and record.storage is not None
        self.assertEqual(record.state, "processing")
        self.assertEqual(record.storage["wait_reason"], "hard_envelope_exceeded")
        self.assertTrue(protocol.storage_status_payload(record)["blocked"])
        # A held task keeps its accepted bytes and is skipped by recovery: no
        # failure, reparse or eviction ever turns the hold into free space.
        charged = registry.storage_usage()
        restarted = lab.open()
        hydrated = {item["task_id"]: item["status"] for item in restarted.recoverable_payloads()}
        self.assertEqual(hydrated, {"task-a": "processing"})
        held_again = restarted.get("key-a")
        assert held_again is not None and held_again.storage is not None
        self.assertEqual(held_again.state, "processing")
        self.assertEqual(held_again.storage["wait_reason"], "hard_envelope_exceeded")
        self.assertEqual(restarted.storage_usage(), charged)

    def run_held(self, lab: StorageLab, parse_for) -> tuple[str, Path, protocol.DurableTaskRegistry]:  # type: ignore[no-untyped-def]
        registry = lab.open()
        root = lab.accept(registry, "key-a", "task-a")
        executor = lab.executor()
        with self.assertRaises(protocol.TaskStorageBlocked) as held:
            asyncio.run(executor.run_storage(
                registry=registry, key="key-a",
                **stages(executor, registry, root, "task-a", parse_for(root)),
            ))
        return held.exception.reason, root, registry

    def test_identity_refusal_is_held_as_tree_integrity_not_capacity(self) -> None:
        def parse_for(root: Path):  # type: ignore[no-untyped-def]
            async def parse(permit: protocol.SourceGrowthPermit) -> None:
                outside = root.parent / "outside"
                outside.mkdir()
                (root / "doc").mkdir()
                (root / "doc" / "hybrid_auto").symlink_to(outside, target_is_directory=True)
                permit.write_file(str(root / "doc/hybrid_auto/doc.md"), b"# redirected")
            return parse

        reason, root, registry = self.run_held(StorageLab(self), parse_for)
        self.assertEqual(reason, "tree_integrity")
        self.assertFalse((root.parent / "outside" / "doc.md").exists())
        self.assertEqual(registry.get("key-a").storage["wait_reason"], "tree_integrity")

    def test_a_refusal_the_parser_catches_still_holds_and_is_never_sealed(self) -> None:
        for label, attempt, expected in (
            ("capacity", "doc/hybrid_auto/images/huge.png", "hard_envelope_exceeded"),
            ("integrity", "../escaped.md", "tree_integrity"),
        ):
            def parse_for(root: Path, attempt: str = attempt):  # type: ignore[no-untyped-def]
                async def parse(permit: protocol.SourceGrowthPermit) -> None:
                    write_output(permit, root, "doc/hybrid_auto/doc.md", b"# kept\n")
                    try:
                        permit.write_file(str(root / attempt), b"\0" * (2 * MIB))
                    except protocol.SourceGrowthLimitExceeded:
                        pass  # A parser that swallows the refusal and returns.
                return parse

            with self.subTest(label):
                reason, root, _ = self.run_held(StorageLab(self), parse_for)
                self.assertEqual(reason, expected)
                self.assertFalse((root / protocol.RETAINED_INVENTORY_NAME).exists())

    def test_single_producer_waits_without_a_parse_slot_then_starts(self) -> None:
        lab = StorageLab(self)
        registry = lab.open()
        first_root = lab.accept(registry, "key-a", "task-a")
        second_root = lab.accept(registry, "key-b", "task-b")
        executor = lab.executor()

        async def scenario() -> None:
            gate = asyncio.Event()
            first = asyncio.create_task(executor.run_storage(
                registry=registry, key="key-a",
                **stages(executor, registry, first_root, "task-a", parse_writing(first_root, OUTPUTS, gate=gate)),
            ))
            await until(lambda: registry.get("key-a").state == "processing")
            second = asyncio.create_task(executor.run_storage(
                registry=registry, key="key-b",
                **stages(executor, registry, second_root, "task-b", parse_writing(second_root, OUTPUTS)),
            ))
            await until(lambda: registry.get("key-b").storage["wait_reason"] is not None)
            waiting = registry.get("key-b")
            self.assertEqual((waiting.state, waiting.storage["phase"]), ("pending", "admitted"))
            self.assertEqual(waiting.storage["wait_reason"], "source_growth_capacity")
            counters = executor.stage_snapshot()
            self.assertEqual(counters["parse_waiting"], 0)
            self.assertEqual(counters["parse_active"], 1)
            gate.set()
            await asyncio.wait_for(asyncio.gather(first, second), 10)

        asyncio.run(scenario())
        for key in ("key-a", "key-b"):
            self.assertEqual(registry.get(key).state, "completed")
            self.assertIsNone(registry.get(key).storage["wait_reason"])

    def test_zip_that_would_pass_its_grant_is_refused_before_the_byte(self) -> None:
        lab = StorageLab(self)
        registry = lab.open()
        root = lab.accept(registry, "key-a", "task-a")
        for relative, data in OUTPUTS.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        identity = registry.task_root_identity("key-a")
        inventory = protocol.build_result_inventory(
            task_id="task-a", task_root=root, root_identity=identity, selections=SELECTIONS,
            policy=lab.policy, zip_upper_bound=retained_zip_upper_bound,
        )
        with self.assertRaises(protocol.ResultGrantExceeded):
            protocol.write_retained_zip(
                task_root=root, root_identity=identity, inventory=inventory,
                selections=SELECTIONS, grant_bytes=1024,
            )
        self.assertFalse((root / protocol.RETAINED_RESULT_PART_NAME).exists())
        self.assertFalse((root / protocol.RETAINED_RESULT_NAME).exists())
        writer_fd = os.open(root / "probe", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            writer = protocol.BudgetedSeekableWriter(writer_fd, grant_bytes=8)
            writer.write(b"12345678")
            writer.seek(2)
            writer.write(b"ab")  # An in-place rewrite never grows the extent.
            with self.assertRaises(protocol.ResultGrantExceeded):
                writer.write(b"123456789")
            self.assertEqual(os.fstat(writer_fd).st_size, 8)
        finally:
            os.close(writer_fd)


class CompletionAccountingTests(unittest.TestCase):
    def _sealed(self, lab: StorageLab, registry, key: str, task_id: str, *, source: int, bound: int) -> None:
        lab.accept(registry, key, task_id)
        registry.reserve_source_growth(
            key, live_free_bytes=PLENTY, outstanding_promise_bytes=0, completion_head_waiting=False,
        )
        registry.transition(key, "processing")
        registry.seal_source(
            key, inventory_sha256="sha256:" + "a" * 64, source_bytes=source, selected_bytes=source,
            member_count=1, zip_upper_bound_bytes=bound,
        )

    def test_completion_waits_for_physical_escrow_and_never_fails(self) -> None:
        lab = StorageLab(self)
        registry = lab.open()
        self._sealed(lab, registry, "key-a", "task-a", source=2 * MIB, bound=3 * MIB)
        self._sealed(lab, registry, "key-b", "task-b", source=2 * MIB, bound=3 * MIB)
        registry.reserve_completion("key-a", grant_bytes=3 * MIB, live_free_bytes=PLENTY, outstanding_promise_bytes=0)
        # Sources 4 MiB + uploads, one physical 3 MiB grant: a second grant
        # would exceed P + C and must wait, not fail.
        with self.assertRaises(protocol.TaskStorageWait) as waited:
            registry.reserve_completion(
                "key-b", grant_bytes=3 * MIB, live_free_bytes=PLENTY, outstanding_promise_bytes=0,
            )
        self.assertEqual(waited.exception.reason, "completion_capacity")
        self.assertEqual(registry.get("key-b").state, "finalizing")
        charged = registry.storage_usage()["result"]
        self.assertEqual(charged, lab.policy.physical_charge(3 * MIB))
        with self.assertRaises(protocol.TaskProtocolConflict):
            registry.reserve_completion(
                "key-a", grant_bytes=2 * MIB, live_free_bytes=PLENTY, outstanding_promise_bytes=0,
            )

    def test_escrow_holds_a_hard_completion_while_sources_fill_the_pool(self) -> None:
        # Measure one sealed task's physical source occupancy, then choose a
        # boundary policy whose pool it fills exactly and whose escrow is the
        # hard result's physical charge: that completion must still fit.
        probe = StorageLab(self)
        registry = probe.open()
        self._sealed(probe, registry, "key-a", "task-a", source=2 * MIB, bound=3 * MIB)
        pool = registry.storage_usage()["source"]
        hard = 3 * MIB
        escrow = probe.policy.physical_charge(hard)
        self.assertGreater(escrow, hard)
        work = pool + escrow + MIB
        boundary = dict(
            native_source_pool_bytes=pool, native_completion_escrow_bytes=escrow,
            native_work_disk_limit_bytes=work, native_volume_total_bytes=work + 20 * MIB,
        )
        lab = StorageLab(self, storage_policy(**boundary))
        registry = lab.open()
        self._sealed(lab, registry, "key-a", "task-a", source=2 * MIB, bound=hard)
        self.assertEqual(registry.storage_usage()["source"], pool)
        registry.reserve_completion("key-a", grant_bytes=hard, live_free_bytes=PLENTY, outstanding_promise_bytes=0)
        self.assertEqual(registry.storage_usage()["result"], escrow)
        for label, escrow_bytes in (("one byte short", escrow - 1), ("logical hard only", hard)):
            with self.subTest(label), self.assertRaisesRegex(ValueError, "physical completion charge"):
                storage_policy(**{**boundary, "native_completion_escrow_bytes": escrow_bytes})

    def test_new_producers_yield_to_a_waiting_completion(self) -> None:
        lab = StorageLab(self, storage_policy(native_growing_producer_limit=2))
        registry = lab.open()
        lab.accept(registry, "key-a", "task-a")
        with self.assertRaises(protocol.TaskStorageWait) as waited:
            registry.reserve_source_growth(
                "key-a", live_free_bytes=PLENTY, outstanding_promise_bytes=0, completion_head_waiting=True,
            )
        self.assertEqual(waited.exception.reason, "completion_capacity")
        floor = lab.policy.native_free_floor_bytes
        permit = lab.policy.native_source_single_limit_bytes
        with self.assertRaises(protocol.TaskStorageWait) as low:
            registry.reserve_source_growth(
                "key-a", live_free_bytes=floor + permit, outstanding_promise_bytes=1,
                completion_head_waiting=False,
            )
        self.assertEqual(low.exception.reason, "free_floor")
        self.assertEqual(
            registry.reserve_source_growth(
                "key-a", live_free_bytes=floor + permit, outstanding_promise_bytes=0,
                completion_head_waiting=False,
            ),
            permit,
        )


class StorageHoldDecisionTests(unittest.TestCase):
    """An operator's exact terminal decision for one held task (never automatic)."""

    RUNTIME = "sha256:" + "d" * 64

    def held(self, reason: str = "hard_envelope_exceeded"):  # type: ignore[no-untyped-def]
        lab = StorageLab(self)
        registry = lab.open()
        task_dir = lab.accept(registry, "key-held", "task-held")
        registry.reserve_source_growth(
            "key-held", live_free_bytes=PLENTY, outstanding_promise_bytes=0, completion_head_waiting=False,
        )
        registry.transition("key-held", "processing")
        registry.block_storage("key-held", reason=reason)
        return lab, registry, task_dir

    def decide(self, registry, preview, **changes):  # type: ignore[no-untyped-def]
        values = dict(
            runtime_identity_sha256=self.RUNTIME, expected_preview_sha256=preview["preview_sha256"],
            decided_by="root", reason="envelope stays as declared", fixed_by="no fix; terminal",
        )
        values.update(changes)
        return registry.decide_storage_hold("key-held", **values)

    def test_reviewed_hold_fails_once_keeps_its_bytes_until_ack_and_replays(self) -> None:
        lab, registry, task_dir = self.held()
        preview = registry.storage_hold_preview("key-held", runtime_identity_sha256=self.RUNTIME)
        self.assertEqual(
            (preview["hold_reason"], preview["state"], preview["attempt_identity"], preview["fence_identity"]),
            ("hard_envelope_exceeded", "processing", "attempt-key-held", "fence-key-held"),
        )
        charged = registry.storage_usage()["source"]
        for label, changes in (
            ("stale review", {"expected_preview_sha256": "sha256:" + "0" * 64}),
            ("other runtime", {"runtime_identity_sha256": "sha256:" + "e" * 64}),
        ):
            with self.subTest(label), self.assertRaises(protocol.TaskProtocolConflict):
                self.decide(registry, preview, **changes)
        self.assertEqual(registry.get("key-held").state, "processing")
        receipt = self.decide(registry, preview)
        record = registry.get("key-held")
        assert record is not None and record.failure_cause is not None
        self.assertEqual(
            (record.state, record.failure_cause["code"], record.failure_cause["retry_class"],
             record.failure_cause["hold_reason"], record.failure_cause["decision_sha256"], receipt["replayed"]),
            ("failed", "storage_hold_terminated", "permanent", "hard_envelope_exceeded",
             receipt["decision_sha256"], False),
        )
        # The durable cause keeps the canonical decision, the digest's exact preimage.
        decision = {
            "schema": protocol.STORAGE_HOLD_DECISION_SCHEMA, "preview_sha256": preview["preview_sha256"],
            "decided_by": "root", "reason": "envelope stays as declared", "fixed_by": "no fix; terminal",
        }
        self.assertEqual((record.failure_cause["decision"], receipt["decision"]), (decision, decision))
        self.assertEqual(
            receipt["decision_sha256"],
            "sha256:" + hashlib.sha256(
                json.dumps(decision, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            ).hexdigest(),
        )
        # Bytes and tree stay charged and present until the ordinary ACK.
        self.assertEqual(registry.storage_usage()["source"], charged)
        self.assertTrue(task_dir.is_dir())
        # A lost response replays the same receipt; another decision is refused.
        self.assertEqual({**self.decide(registry, preview), "replayed": False}, receipt)
        with self.assertRaises(protocol.TaskProtocolConflict):
            self.decide(registry, preview, reason="a different decision")
        reopened = lab.open()
        self.assertEqual(reopened.get("key-held").failure_cause, record.failure_cause)
        # After a restart the lost answer replays from the durable record alone.
        self.assertEqual({**self.decide(reopened, preview), "replayed": False}, receipt)
        reopened.acknowledge_failed("key-held")
        self.assertEqual(reopened.get("key-held").state, "consumed")
        self.assertFalse(task_dir.exists())
        with self.assertRaises(protocol.TaskProtocolConflict):
            self.decide(reopened, preview)

    def test_only_a_currently_held_task_with_an_attributed_decision_is_decided(self) -> None:
        lab = StorageLab(self)
        registry = lab.open()
        lab.accept(registry, "key-open", "task-open")
        registry.reserve_source_growth(
            "key-open", live_free_bytes=PLENTY, outstanding_promise_bytes=0, completion_head_waiting=False,
        )
        registry.transition("key-open", "processing")
        with self.assertRaises(protocol.TaskProtocolConflict):  # Growing, not held.
            registry.storage_hold_preview("key-open", runtime_identity_sha256=self.RUNTIME)
        registry.fail("key-open", error="parser failed")
        with self.assertRaises(protocol.TaskProtocolConflict):  # Failed, not held.
            registry.storage_hold_preview("key-open", runtime_identity_sha256=self.RUNTIME)
        _, held, _ = self.held("seal_integrity")
        preview = held.storage_hold_preview("key-held", runtime_identity_sha256=self.RUNTIME)
        for field in ("decided_by", "reason", "fixed_by"):
            with self.subTest(field), self.assertRaises(ValueError):
                self.decide(held, preview, **{field: " "})
        decision, digest = protocol.storage_hold_decision(
            preview_sha256=preview["preview_sha256"], decided_by="root", reason="seal broke", fixed_by="none",
        )
        cause = {
            "schema": protocol.TASK_FAILURE_CAUSE_SCHEMA, "task_id": "task-held", "retry_class": "permanent",
            "code": "storage_hold_terminated", "http_status": None, "transport_error": None,
            "hold_reason": "seal_integrity", "decision_sha256": digest, "decision": decision,
        }
        protocol.validate_task_failure_cause(dict(cause), task_id="task-held")
        for label, broken in (
            ("not a hold reason", {**cause, "hold_reason": "free_floor"}),
            ("digest of another decision", {**cause, "decision_sha256": "sha256:" + "a" * 64}),
            ("attribution edited under its digest", {**cause, "decision": {**decision, "reason": "other"}}),
            ("digest only, no attribution", {key: value for key, value in cause.items() if key != "decision"}),
            ("another decision schema", {**cause, "decision": {**decision, "schema": "other.v1"}}),
            ("open decision fields", {**cause, "decision": {**decision, "note": "x"}}),
            ("missing hold reason", {key: value for key, value in cause.items() if key != "hold_reason"}),
            ("retry class drift", {**cause, "retry_class": "unknown"}),
            ("fields on another code", {**cause, "code": "unclassified", "retry_class": "unknown"}),
        ):
            with self.subTest(label), self.assertRaises(protocol.TaskProtocolConflict):
                protocol.validate_task_failure_cause(broken, task_id="task-held")


class StorageHoldOperatorGateTests(unittest.TestCase):
    """The hold route's operator credential gate (synthetic credentials only)."""

    CREDENTIAL = "synthetic-operator-credential-" + "C" * 20

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "agent-invest-operator" / "storage-hold-operator.json"

    def refusal(self, authorization: str | None) -> tuple[int, str] | None:
        try:
            protocol.require_storage_hold_operator(authorization, path=self.path)
        except protocol.StorageHoldOperatorRefused as exc:
            return exc.status, exc.code
        return None

    def test_route_is_disabled_until_enrolled_and_after_revocation(self) -> None:
        disabled = (403, "storage_hold_operator_disabled")
        self.assertEqual(self.refusal(f"Bearer {self.CREDENTIAL}"), disabled)  # No directory.
        verifier = protocol.storage_hold_operator_verifier(self.CREDENTIAL)
        self.assertEqual(protocol.enroll_storage_hold_operator(verifier, path=self.path),
                         {"schema": protocol.STORAGE_HOLD_OPERATOR_SCHEMA, "credential_sha256": verifier})
        # Only the verifier is stored, owner-only, in an owner-only directory.
        self.assertNotIn(self.CREDENTIAL.encode(), self.path.read_bytes())
        self.assertEqual((os.stat(self.path).st_mode & 0o777, os.stat(self.path.parent).st_mode & 0o777),
                         (0o600, 0o700))
        self.assertEqual(sorted(os.listdir(self.path.parent)), [self.path.name])
        for authorization in (f"Bearer {self.CREDENTIAL}", f"bearer {self.CREDENTIAL} "):
            self.assertIsNone(self.refusal(authorization))
        self.assertTrue(protocol.revoke_storage_hold_operator(path=self.path))
        self.assertFalse(protocol.revoke_storage_hold_operator(path=self.path))
        self.assertEqual(self.refusal(f"Bearer {self.CREDENTIAL}"), disabled)  # Directory, no file.
        with self.assertRaises(ValueError):
            protocol.enroll_storage_hold_operator("sha256:" + "A" * 64, path=self.path)

    def test_missing_malformed_or_wrong_credentials_are_refused_in_constant_time(self) -> None:
        protocol.enroll_storage_hold_operator(
            protocol.storage_hold_operator_verifier(self.CREDENTIAL), path=self.path,
        )
        compared: list[tuple[bytes, bytes]] = []
        original = protocol.hmac.compare_digest

        def spy(left: bytes, right: bytes) -> bool:
            compared.append((left, right))
            return original(left, right)

        unauthorized = (401, "storage_hold_operator_unauthorized")
        with unittest.mock.patch.object(protocol.hmac, "compare_digest", side_effect=spy):
            for authorization in (
                None, "", "Bearer", "Bearer  ", f"Basic {self.CREDENTIAL}", self.CREDENTIAL,
                f"Bearer {self.CREDENTIAL}x", "Bearer ☃", f"Bearer {self.CREDENTIAL.upper()}",
            ):
                with self.subTest(authorization=authorization):
                    self.assertEqual(self.refusal(authorization), unauthorized)
        # Every refusal compared digests, never the raw credential.
        self.assertEqual(len(compared), 9)
        self.assertTrue(all(right == protocol.storage_hold_operator_verifier(self.CREDENTIAL).encode()
                            for _, right in compared))
        # A replacement enrollment retires the earlier credential at once.
        replacement = self.CREDENTIAL.replace("C", "D")
        protocol.enroll_storage_hold_operator(
            protocol.storage_hold_operator_verifier(replacement), path=self.path,
        )
        self.assertEqual(self.refusal(f"Bearer {self.CREDENTIAL}"), unauthorized)
        self.assertIsNone(self.refusal(f"Bearer {replacement}"))

    def test_an_unsafe_enrollment_fails_closed(self) -> None:
        verifier = protocol.storage_hold_operator_verifier(self.CREDENTIAL)
        misconfigured = (403, "storage_hold_operator_misconfigured")
        valid = json.dumps({"schema": protocol.STORAGE_HOLD_OPERATOR_SCHEMA, "credential_sha256": verifier})
        for label, content, file_mode, directory_mode in (
            ("readable by others", valid, 0o644, 0o700),
            ("directory open to others", valid, 0o600, 0o755),
            ("another schema", valid.replace(".v1", ".v0"), 0o600, 0o700),
            ("extra field", valid[:-1] + ', "note": 1}', 0o600, 0o700),
            ("not a verifier", valid.replace("sha256:", "sha1:"), 0o600, 0o700),
            ("not JSON", "enabled", 0o600, 0o700),
            ("oversized", valid + " " * 600, 0o600, 0o700),
        ):
            with self.subTest(label):
                protocol.revoke_storage_hold_operator(path=self.path)
                self.path.parent.mkdir(mode=0o700, exist_ok=True)
                self.path.write_text(content)
                os.chmod(self.path, file_mode)
                os.chmod(self.path.parent, directory_mode)
                self.assertEqual(self.refusal(f"Bearer {self.CREDENTIAL}"), misconfigured)
                os.chmod(self.path.parent, 0o700)
        protocol.revoke_storage_hold_operator(path=self.path)
        target = self.path.parent / "elsewhere.json"
        target.write_text(valid)
        os.chmod(target, 0o600)
        self.path.symlink_to(target)
        self.assertEqual(self.refusal(f"Bearer {self.CREDENTIAL}"), misconfigured)


class StorageRecoveryTests(unittest.TestCase):
    def test_restart_repacks_a_sealed_source_without_parsing(self) -> None:
        lab = StorageLab(self)
        registry = lab.open()
        root = lab.accept(registry, "key-a", "task-a")
        executor = lab.executor()
        for relative, data in OUTPUTS.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        identity = registry.task_root_identity("key-a")
        registry.reserve_source_growth(
            "key-a", live_free_bytes=PLENTY, outstanding_promise_bytes=0, completion_head_waiting=False,
        )
        registry.transition("key-a", "processing")
        inventory = protocol.build_result_inventory(
            task_id="task-a", task_root=root, root_identity=identity, selections=SELECTIONS,
            policy=lab.policy, zip_upper_bound=retained_zip_upper_bound,
        )
        digest = protocol.write_inventory_file(root, identity, inventory)
        registry.seal_source(
            "key-a", inventory_sha256=digest, source_bytes=MIB, selected_bytes=inventory.selected_bytes,
            member_count=len(inventory.members), zip_upper_bound_bytes=inventory.zip_upper_bound_bytes,
        )
        registry.reserve_completion(
            "key-a", grant_bytes=inventory.zip_upper_bound_bytes, live_free_bytes=PLENTY,
            outstanding_promise_bytes=0,
        )
        (root / protocol.RETAINED_RESULT_PART_NAME).write_bytes(b"partial")

        restarted = lab.open()
        hydrated = {item["task_id"]: item["status"] for item in restarted.recoverable_payloads()}
        self.assertEqual(hydrated, {"task-a": "pending"})
        recovered = restarted.get("key-a")
        self.assertEqual((recovered.state, recovered.storage["phase"]), ("finalizing", "source_sealed"))
        self.assertFalse((root / protocol.RETAINED_RESULT_PART_NAME).exists())
        self.assertTrue((root / "doc/hybrid_auto/doc.md").exists())

        async def never_parse(_permit):
            raise AssertionError("a sealed source must never be parsed again")

        asyncio.run(executor.run_storage(
            registry=restarted, key="key-a", **stages(executor, restarted, root, "task-a", never_parse),
        ))
        done = restarted.get("key-a")
        self.assertEqual((done.state, done.storage["phase"]), ("completed", "zip_sealed"))

    def test_restart_replays_an_interrupted_producer_from_its_upload_only(self) -> None:
        lab = StorageLab(self)
        registry = lab.open()
        root = lab.accept(registry, "key-a", "task-a")
        registry.reserve_source_growth(
            "key-a", live_free_bytes=PLENTY, outstanding_promise_bytes=0, completion_head_waiting=False,
        )
        registry.transition("key-a", "processing")
        partial = root / "doc/hybrid_auto/doc.md"
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b"partial")
        restarted = lab.open()
        restarted.recoverable_payloads()
        record = restarted.get("key-a")
        self.assertEqual((record.state, record.storage["phase"]), ("pending", "admitted"))
        self.assertEqual(record.storage["growth_permit_bytes"], 0)
        self.assertFalse(partial.exists())
        self.assertEqual((root / "uploads/source.pdf").read_bytes(), UPLOAD)

    def test_changed_sealed_source_is_held_not_repacked(self) -> None:
        lab = StorageLab(self)
        registry = lab.open()
        root = lab.accept(registry, "key-a", "task-a")
        executor = lab.executor()

        async def run_to_seal() -> None:
            steps = stages(executor, registry, root, "task-a", parse_writing(root, OUTPUTS))

            async def never_grant():
                # Space for the producer, none for its completion: the sealed
                # source waits on the free floor.
                return PLENTY if registry.get("key-a").state == "pending" else 0

            steps["probe_free"] = never_grant
            await executor.run_storage(registry=registry, key="key-a", **steps)

        async def bounded() -> None:
            task = asyncio.create_task(run_to_seal())
            await until(lambda: registry.get("key-a").state == "finalizing"
                        and registry.get("key-a").storage["wait_reason"] is not None)
            self.assertEqual(registry.get("key-a").storage["wait_reason"], "free_floor")
            executor.begin_shutdown(abort_pending=True)
            with self.assertRaises(protocol.TaskExecutionStopped):
                await asyncio.wait_for(task, 5)

        asyncio.run(bounded())
        (root / "doc/hybrid_auto/doc.md").write_bytes(b"tampered")
        restarted = lab.open()
        restarted.recoverable_payloads()
        executor = lab.executor()

        async def never_parse(_permit):
            raise AssertionError("a sealed source must never be parsed again")

        with self.assertRaises(protocol.TaskStorageBlocked) as held:
            asyncio.run(executor.run_storage(
                registry=restarted, key="key-a", **stages(executor, restarted, root, "task-a", never_parse),
            ))
        self.assertEqual(held.exception.reason, "seal_integrity")
        record = restarted.get("key-a")
        self.assertEqual((record.state, record.storage["wait_reason"]), ("finalizing", "seal_integrity"))


class GrowthPermitTests(unittest.TestCase):
    def test_permit_charges_physical_rewrites_directories_and_trips(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "task"
            root.mkdir()
            unit = overhead = 4096
            allowance = 8 * (unit + overhead)
            permit = protocol.SourceGrowthPermit(
                root=root, limit_bytes=allowance + 4 * (unit + overhead),
                allocation_unit=unit, file_overhead=overhead,
            )
            write_output(permit, root, "a/b.txt", b"x")  # New file and one new directory.
            self.assertEqual(permit.charged_bytes, allowance + 2 * (unit + overhead))
            write_output(permit, root, "a/b.txt", b"y" * 10)  # Same rounded size: no growth.
            self.assertEqual(permit.charged_bytes, allowance + 2 * (unit + overhead))
            with self.assertRaises(protocol.SourceGrowthLimitExceeded):
                permit.before_write(str(root / "a/c.bin"), 3 * unit + 1)
            self.assertTrue(permit.tripped)
            with self.assertRaises(protocol.SourceGrowthLimitExceeded):
                permit.before_write(str(root / "a/tiny"), 0)
            with self.assertRaises(protocol.SourceGrowthLimitExceeded):
                protocol.SourceGrowthPermit(
                    root=root, limit_bytes=allowance, allocation_unit=unit, file_overhead=overhead,
                ).before_write(str(Path(scratch) / "outside"), 1)

    def test_bound_permit_is_visible_in_copied_thread_contexts_only(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            permit = protocol.SourceGrowthPermit(
                root=Path(scratch), limit_bytes=MIB, allocation_unit=4096, file_overhead=0,
            )

            async def scenario() -> tuple[object, object]:
                with protocol.bind_source_growth_permit(permit):
                    inside = await asyncio.to_thread(protocol.current_source_growth_permit)
                outside = await asyncio.to_thread(protocol.current_source_growth_permit)
                return inside, outside

            inside, outside = asyncio.run(scenario())
            self.assertIs(inside, permit)
            self.assertIsNone(outside)


class InventoryBoundTests(unittest.TestCase):
    def test_selection_beyond_member_or_name_bounds_is_held(self) -> None:
        lab = StorageLab(self, storage_policy(max_members=2))
        registry = lab.open()
        root = lab.accept(registry, "key-a", "task-a")
        for relative, data in OUTPUTS.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        with self.assertRaises(protocol.TaskStorageBlocked) as held:
            protocol.build_result_inventory(
                task_id="task-a", task_root=root, root_identity=registry.task_root_identity("key-a"),
                selections=SELECTIONS, policy=lab.policy, zip_upper_bound=retained_zip_upper_bound,
            )
        self.assertEqual(held.exception.reason, "hard_envelope_exceeded")

    def test_incompressible_member_stays_within_the_computed_bound(self) -> None:
        lab = StorageLab(self)
        registry = lab.open()
        root = lab.accept(registry, "key-a", "task-a")
        noisy = dict(OUTPUTS)
        noisy["doc/hybrid_auto/images/noise.png"] = random.Random(7).randbytes(512 * 1024)
        executor = lab.executor()
        asyncio.run(executor.run_storage(
            registry=registry, key="key-a", **stages(executor, registry, root, "task-a", parse_writing(root, noisy)),
        ))
        record = registry.get("key-a")
        self.assertLessEqual(record.storage["zip_bytes"], record.storage["zip_upper_bound_bytes"])
        self.assertGreater(record.storage["zip_bytes"], 512 * 1024)


if __name__ == "__main__":
    unittest.main()
