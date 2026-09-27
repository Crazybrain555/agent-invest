"""Independent completion-headroom witness through real coordinator dispatch.

Small owned files stand for the exact 100-byte source and 500-byte LOCAL
projection in the coordinator fixture. No provider, parser, DB or shared
runtime is involved.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from disclosure_anchor.application.contracts.mineru_capacity_config import (
    mac_document_disk_upper_bound,
    mac_work_file_margin_bytes,
)
from disclosure_anchor.application.contracts.mineru_process_profile import (
    RESULT_STORAGE_PROCESS_PROFILE_CONTRACT,
)
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import StagedWorkerProfileV4
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorTerminal,
    StagedParseCoordinator,
)
from disclosure_anchor.application.services.staged_v4_capacity import staged_v4_coordinator_limits
from tests.unit.test_mineru_materialize_grant_v5 import synthetic_storage_policy
from tests.unit.test_mineru_process_profile import _profile
from tests.unit.test_staged_parse_coordinator import _Backend, _limits, _work


class _FileBackend(_Backend):
    def __init__(self, root: Path, *, seed_files: bool) -> None:
        recovered = tuple(_work(f"r{index}", "remote_terminal", 3) for index in range(3))
        waiting = tuple(_work(f"n{index}", "prepared") for index in range(8))
        super().__init__(recoverable=recovered, new=waiting)
        self.root = root
        self.work_root = root / "work"
        self.published_root = root / "published"
        self.work_root.mkdir(exist_ok=True)
        self.published_root.mkdir(exist_ok=True)
        self.file_lock = threading.Lock()
        self.peak_owned = 0
        self.first_local_snapshot_ids: tuple[str, ...] | None = None
        self.first_local_admitted: int | None = None
        self.admitted_ids: list[str] = []
        if seed_files:
            for work in recovered:
                self._write(work.attempt_id, "snapshot", 100)
        else:
            self._measure_locked()

    def _path(self, attempt_id: str, name: str) -> Path:
        return self.work_root / attempt_id / name

    def _measure_locked(self) -> None:
        owned = sum(path.stat().st_size for path in self.work_root.rglob("*") if path.is_file())
        self.peak_owned = max(self.peak_owned, owned)

    def _write(self, attempt_id: str, name: str, size: int) -> None:
        with self.file_lock:
            path = self._path(attempt_id, name)
            path.parent.mkdir(exist_ok=True)
            with path.open("xb") as output:
                output.write(bytes(size))
            self._measure_locked()

    def admit_new(self, *, limit, available_credits):  # type: ignore[no-untyped-def]
        outcome = super().admit_new(limit=limit, available_credits=available_credits)
        for work in outcome.work:
            self._write(work.attempt_id, "snapshot", 100)
            self.admitted_ids.append(work.attempt_id)
        return outcome

    def prepare_local_io(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        with self.file_lock:
            if self.first_local_snapshot_ids is None:
                self.first_local_snapshot_ids = tuple(sorted(
                    path.parent.name for path in self.work_root.rglob("snapshot")
                ))
                self.first_local_admitted = len(self.admitted_ids)
        return super().prepare_local_io(
            work, credit_allowance=credit_allowance, stage_guard=stage_guard,
        )

    def run_local(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        updated = super().run_local(
            work, credit_allowance=credit_allowance, stage_guard=stage_guard,
        )
        self._write(work.attempt_id, "spool", 100)
        self._write(work.attempt_id, "output", 400)
        return updated

    def commit(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        updated = super().commit(
            work, credit_allowance=credit_allowance, stage_guard=stage_guard,
        )
        with self.file_lock:
            self._path(work.attempt_id, "output").rename(
                self.published_root / work.attempt_id
            )
        return updated

    def cleanup(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        updated = super().cleanup(
            work, credit_allowance=credit_allowance, stage_guard=stage_guard,
        )
        if work.state == "cleanup_pending":
            with self.file_lock:
                self._path(work.attempt_id, "snapshot").unlink()
                self._path(work.attempt_id, "spool").unlink()
        return updated


class CompletionHeadroomIndependentTests(unittest.TestCase):
    def test_recovered_snapshots_and_eight_eligible_new_items_all_finish_under_d(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = _FileBackend(root, seed_files=True)
            original_files = {
                work.attempt_id: writer._path(work.attempt_id, "snapshot").stat().st_ino
                for work in writer.recoverable
            }
            del writer
            backend = _FileBackend(root, seed_files=False)
            self.assertEqual(
                original_files,
                {work.attempt_id: backend._path(work.attempt_id, "snapshot").stat().st_ino
                 for work in backend.recoverable},
            )
            # A new coordinator sees three durable source files. One maximal
            # LOCAL grant exactly fills D: 3*100 + 500 = 800. Counting those
            # recovered snapshots twice would prevent this first grant.
            self.assertEqual(backend.peak_owned, 300)
            result = StagedParseCoordinator(
                backend=backend,
                limits=_limits(
                    work_disk_bytes=800,
                    work_disk_local_reserve_bytes=500,
                    admission_batch_size=8,
                    local_prepare_workers=1,
                    local_workers=1,
                    commit_workers=1,
                    cleanup_workers=1,
                    ack_workers=1,
                    idle_open_circuit_seconds=0.1,
                ),
            ).run()
            self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result)
            self.assertEqual(backend.first_local_snapshot_ids, ("r0", "r1", "r2"))
            self.assertEqual(backend.first_local_admitted, 0)
            self.assertEqual(backend.admitted_ids, [f"n{index}" for index in range(8)])
            self.assertEqual(
                dict(result.final_states),
                {name: "acked" for name in (*backend.first_local_snapshot_ids, *backend.admitted_ids)},
            )
            for name in (*backend.first_local_snapshot_ids, *backend.admitted_ids):
                self.assertIn(f"commit:{name}", backend.calls)
                self.assertIn(f"cleanup:{name}:cleanup_pending", backend.calls)
                self.assertIn(f"ack:{name}:ack_pending", backend.calls)
                self.assertEqual((backend.published_root / name).stat().st_size, 400)
            self.assertLessEqual(backend.peak_owned, 800)
            self.assertEqual(list(backend.work_root.rglob("snapshot")), [])
            self.assertEqual(list(backend.work_root.rglob("spool")), [])

    def test_policy_rejects_d_that_fits_grant_but_not_its_source_snapshot(self) -> None:
        policy = synthetic_storage_policy()
        grant = mac_document_disk_upper_bound(
            policy, policy.native_result_hard_limit_bytes,
            policy.native_source_single_limit_bytes,
        )
        self.assertGreater(policy.source_pdf_bytes_limit + mac_work_file_margin_bytes(policy), 0)
        profile = replace(
            _profile(),
            contract_version=RESULT_STORAGE_PROCESS_PROFILE_CONTRACT,
            result_reservation_bytes=None,
            max_unacked_result_bytes=None,
            result_storage_policy_sha256=policy.sha256,
            source_pdf_bytes_limit=policy.source_pdf_bytes_limit,
        )
        limits = staged_v4_coordinator_limits(
            profile,
            worker_profile=StagedWorkerProfileV4(profile.sha256, 1, 1),
            storage_policy=policy,
        )
        self.assertEqual(limits.work_disk_local_reserve_bytes, grant)
        self.assertEqual(limits.work_disk_bytes, policy.mac_work_disk_limit_bytes)
        with self.assertRaisesRegex(ValueError, "source snapshot"):
            synthetic_storage_policy(mac_work_disk_limit_bytes=grant)


if __name__ == "__main__":
    unittest.main()
