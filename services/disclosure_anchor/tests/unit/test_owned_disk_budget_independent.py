"""Independent shared-D regression with real, bounded owned scratch extents.

The real coordinator supplies grant admission and aggregates durable recovery
credits. A tiny backend supplies deterministic file effects instead of a
parser, database, provider or publication service. Its exact byte extents
match the durable projection used by the scheduler.
"""

from __future__ import annotations

from dataclasses import fields, replace
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest

from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.contracts.mineru_capacity_config import mac_work_file_margin_bytes
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorResult, CoordinatorTerminal, CoordinatorWork, StageHeavyWorkRequired,
    StagedParseCoordinator, work_disk_footprint,
)
from tests.unit.test_mineru_materialize_grant_v5 import MIB, synthetic_storage_policy
from tests.unit.test_staged_parse_coordinator import _Backend, _LIMIT, _limits, _work


FACTOR = 30_000  # snapshot/spool 3 MB, output 12 MB, local temp promise 15 MB
BYTE_FIELDS = frozenset(item.name for item in fields(ResourceCreditVector)
                        if item.name.endswith("_bytes"))
BLOCK = hashlib.sha256(b"independent owned extent").digest() * 32_768  # 1 MiB


def _scaled(credits: ResourceCreditVector) -> ResourceCreditVector:
    return ResourceCreditVector(**{
        item.name: getattr(credits, item.name) * (FACTOR if item.name in BYTE_FIELDS else 1)
        for item in fields(ResourceCreditVector)
    })


def _scaled_work(attempt: str, state: str, version: int = 5) -> CoordinatorWork:
    base = _work(attempt, state, version)
    return replace(base, credits=_scaled(base.credits),
                   credit_reservation=_scaled(base.credit_reservation))


def _write_extent(path: Path, byte_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        while byte_count:
            piece = BLOCK[:min(len(BLOCK), byte_count)]
            output.write(piece)
            byte_count -= len(piece)
        output.flush()
        os.fsync(output.fileno())


def _identity(path: Path) -> tuple[int, int, int, str]:
    observed = path.stat(follow_symlinks=False)
    return (observed.st_dev, observed.st_ino, observed.st_size,
            hashlib.sha256(path.read_bytes()).hexdigest())


class _OwnedFileBackend(_Backend):
    def __init__(self, root: Path, *, seed_files: bool = False) -> None:
        super().__init__(recoverable=(
            _scaled_work("a-retained", "local_materialized"),
            _scaled_work("b-waiting", "remote_terminal"),
        ))
        self.root = root
        self.cleanup_entered = threading.Event()
        self.release_cleanup = threading.Event()
        self.local_entered = threading.Event()
        self.local_trace: list[tuple[str, bool | None, ResourceCreditVector]] = []
        if seed_files:
            self._write_initial()

    def path(self, attempt: str, name: str) -> Path:
        return self.root / attempt / name

    def _write_initial(self) -> None:
        # Recovery opens these exact already-owned files. The original A
        # materialization has a snapshot, retained spool and private output;
        # B has only its source snapshot before the local write grant.
        for attempt, sizes in (
            ("a-retained", (("snapshot", 3_000_000), ("spool", 3_000_000),
                            ("output", 12_000_000))),
            ("b-waiting", (("snapshot", 3_000_000),)),
        ):
            for name, size in sizes:
                _write_extent(self.path(attempt, name), size)

    def prepare_local_io(self, work, *, credit_allowance, stage_guard):
        stage_guard.checkpoint()
        target = _scaled_work(work.attempt_id, "materializing", work.lifecycle_version + 1)
        updated = replace(target, claim_generation=work.claim_generation,
                          claim_owner_identity=work.claim_owner_identity,
                          lease_expires_monotonic=work.lease_expires_monotonic,
                          credit_reservation=work.credit_reservation)
        self._assert_credit_grant(work, updated, credit_allowance)
        return updated

    def run_local(self, work, *, credit_allowance, stage_guard):
        stage_guard.checkpoint()
        self.local_entered.set()
        self.local_trace.append((work.state, getattr(stage_guard, "heavy_work_permitted", None),
                                 credit_allowance))
        if work.attempt_id != "b-waiting":
            raise AssertionError("unexpected local attempt")
        # LOCAL may transfer a bounded spool first; the actual decode waits
        # until the coordinator redispatches it with the heavy permit.
        spool = self.path(work.attempt_id, "spool")
        if not spool.exists():
            _write_extent(spool, 3_000_000)
        if getattr(stage_guard, "heavy_work_permitted", None) is False:
            raise StageHeavyWorkRequired("synthetic decode awaits permit",
                                         retry_after_seconds=0.001)
        _write_extent(self.path(work.attempt_id, "output"), 12_000_000)
        target = _scaled_work(work.attempt_id, "local_materialized", work.lifecycle_version + 1)
        updated = replace(target, claim_generation=work.claim_generation,
                          claim_owner_identity=work.claim_owner_identity,
                          lease_expires_monotonic=work.lease_expires_monotonic,
                          credit_reservation=work.credit_reservation)
        self._assert_credit_grant(work, updated, credit_allowance)
        return updated

    def commit(self, work, *, credit_allowance, stage_guard):
        stage_guard.checkpoint()
        # Promotion is an ownership handoff by rename, not physical deletion.
        self.path(work.attempt_id, "output").rename(
            self.path(work.attempt_id, "published")
        )
        target = _scaled_work(work.attempt_id, "publish_committed", work.lifecycle_version + 1)
        updated = replace(target, claim_generation=work.claim_generation,
                          claim_owner_identity=work.claim_owner_identity,
                          lease_expires_monotonic=work.lease_expires_monotonic,
                          credit_reservation=work.credit_reservation)
        self._assert_credit_grant(work, updated, credit_allowance)
        return updated

    def cleanup(self, work, *, credit_allowance, stage_guard):
        stage_guard.checkpoint()
        if work.attempt_id == "a-retained" and work.state == "cleanup_pending":
            self.cleanup_entered.set()
            while not self.release_cleanup.wait(0.001):
                stage_guard.checkpoint()
        if work.state == "cleanup_pending":
            self.path(work.attempt_id, "snapshot").unlink()
            self.path(work.attempt_id, "spool").unlink()
        target_state = "cleanup_pending" if work.state == "publish_committed" else "ack_pending"
        target = _scaled_work(work.attempt_id, target_state, work.lifecycle_version + 1)
        updated = replace(target, claim_generation=work.claim_generation,
                          claim_owner_identity=work.claim_owner_identity,
                          lease_expires_monotonic=work.lease_expires_monotonic,
                          credit_reservation=work.credit_reservation)
        self._assert_credit_grant(work, updated, credit_allowance)
        return updated


class OwnedDiskBudgetIndependentTests(unittest.TestCase):
    def test_recovered_owned_extents_hold_second_write_until_cleanup_handoff(self) -> None:
        policy = synthetic_storage_policy(mac_work_disk_limit_bytes=30 * MIB)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # A separate instance opens the original files and durable work
            # after the writer goes away, as on a process restart.
            writer = _OwnedFileBackend(root, seed_files=True)
            del writer
            backend = _OwnedFileBackend(root)
            before = {name: _identity(backend.path("b-waiting", name))
                      for name in ("snapshot",)}
            a_before = {name: _identity(backend.path("a-retained", name))
                        for name in ("snapshot", "spool", "output")}
            a_owned = sum(backend.path("a-retained", name).stat().st_size
                          for name in ("snapshot", "spool", "output"))
            b_owned = backend.path("b-waiting", "snapshot").stat().st_size
            b_promise = 15_000_000
            self.assertEqual((a_owned, b_owned), (18_000_000, 3_000_000))
            self.assertLessEqual(a_owned + b_owned, policy.mac_work_disk_limit_bytes)
            self.assertGreater(a_owned + b_owned + b_promise,
                               policy.mac_work_disk_limit_bytes)
            margin = mac_work_file_margin_bytes(policy)
            charged = a_owned + b_owned + 2 * margin
            self.assertEqual(work_disk_footprint(
                _scaled_work("a-retained", "local_materialized").credits, margin),
                a_owned + margin)
            self.assertEqual(work_disk_footprint(
                _scaled_work("b-waiting", "remote_terminal").credits, margin),
                b_owned + margin)
            self.assertGreater(charged + b_promise, policy.mac_work_disk_limit_bytes)
            inode_keys = {(path.stat().st_dev, path.stat().st_ino)
                          for path in root.rglob("*") if path.is_file()}
            self.assertEqual(len(inode_keys), 4, "the four retained files must be distinct extents")
            allocated = sum(path.stat().st_blocks * 512 for path in root.rglob("*")
                            if path.is_file())
            self.assertLessEqual(allocated, charged,
                                 "allocation rounding exceeded the policy's file margin")

            results: list[CoordinatorResult] = []
            failures: list[BaseException] = []
            def run() -> None:
                try:
                    results.append(StagedParseCoordinator(
                        backend=backend,
                        limits=_limits(
                            credits=_scaled(_LIMIT), work_disk_bytes=policy.mac_work_disk_limit_bytes,
                            work_disk_margin_bytes=margin,
                            local_workers=1, commit_workers=1, cleanup_workers=1,
                            max_stage_step_seconds=10, idle_open_circuit_seconds=1,
                        ),
                    ).run())
                except BaseException as exc:
                    failures.append(exc)
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                self.assertTrue(backend.cleanup_entered.wait(2),
                                f"A did not reach held cleanup: {failures!r} {results!r}")
                self.assertFalse(backend.local_entered.wait(0.05),
                                 "B wrote while A's retained extents still occupied D")
                self.assertEqual(_identity(backend.path("b-waiting", "snapshot")),
                                 before["snapshot"])
                self.assertEqual(_identity(backend.path("a-retained", "snapshot")),
                                 a_before["snapshot"])
                self.assertEqual(_identity(backend.path("a-retained", "spool")),
                                 a_before["spool"])
                self.assertFalse(backend.path("b-waiting", "spool").exists())
                self.assertFalse(backend.path("b-waiting", "output").exists())
                self.assertTrue(backend.path("a-retained", "published").is_file(),
                                "promotion must retain the exact output before cleanup")
                self.assertEqual(_identity(backend.path("a-retained", "published")),
                                 a_before["output"])
            finally:
                backend.release_cleanup.set()
                thread.join(3)
            self.assertFalse(thread.is_alive(), "coordinator failed to drain")
            self.assertEqual(failures, [])
            self.assertEqual(results[0].terminal, CoordinatorTerminal.QUIESCENT,
                             f"{results[0]!r}; local_trace={backend.local_trace!r}")
            self.assertEqual(dict(results[0].final_states),
                             {"a-retained": "acked", "b-waiting": "acked"})
            self.assertTrue(backend.local_entered.is_set())
            self.assertEqual([permitted for _state, permitted, _allowance in backend.local_trace],
                             [False, True], "LOCAL must reuse its spool after one permit wait")
            self.assertTrue(backend.path("a-retained", "published").is_file())
            self.assertTrue(backend.path("b-waiting", "published").is_file())
            self.assertLessEqual(sum(path.stat().st_size for path in root.rglob("*") if path.is_file()),
                                 64 * MIB)


if __name__ == "__main__":
    unittest.main()
