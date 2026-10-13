"""Coordinator resource lifetime: the shared heavy-work permit and the work-disk quota D.

A synthetic backend drives the real coordinator's dispatch. The permit spans
only stages that hold whole objects (COMMIT; LOCAL once it reaches its decode)
and never ACK, CLEANUP, renewal or remote reconcile. The work-disk witness
writes real owned files at scale (1 KiB stands for 1 GiB) and measures the
directory at every write: with D the volume never passes it, although every
per-dimension limit alone would let the Pro R3 counterexample through.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorResult,
    CoordinatorTerminal,
    CoordinatorWork,
    ResourceCreditVector,
    StageCapacityBlocked,
    StagedParseCoordinator,
    StageHeavyWorkRequired,
    StageWaiting,
    work_disk_footprint,
)
from disclosure_anchor.application.services.worker_stop_latch import InProcessWorkerStopLatch
from tests.unit.test_staged_parse_coordinator import _Backend, _limits, _work

KIB = 1024


class _PermitBackend(_Backend):
    """Records which stages held whole objects at once, and each stage's permit."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.lock = threading.Lock()
        self.heavy_active: set[str] = set()
        self.overlaps: list[frozenset[str]] = []
        self.events: list[str] = []
        self.permits: list[tuple[str, str, bool | None]] = []
        self.light_ids: set[str] = set()
        self.light_done = threading.Event()
        self.commit_waits: dict[str, int] = {}

    def _record(self, lane: str, work: CoordinatorWork, permitted: bool | None) -> None:
        with self.lock:
            self.permits.append((lane, work.attempt_id, permitted))

    def _heavy(self, attempt_id: str, entering: bool) -> None:
        with self.lock:
            if entering:
                self.heavy_active.add(attempt_id)
                self.overlaps.append(frozenset(self.heavy_active))
            else:
                self.heavy_active.discard(attempt_id)
            self.events.append(("enter:" if entering else "exit:") + attempt_id)

    def run_local(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        self._record("local", work, stage_guard.heavy_work_permitted)
        if not stage_guard.heavy_work_permitted:
            # Transfer and unpack are durable; the whole decode needs the permit.
            raise StageHeavyWorkRequired("decode waits for the permit", retry_after_seconds=0.001)
        self._heavy(work.attempt_id, True)
        try:
            return super().run_local(work, credit_allowance=credit_allowance, stage_guard=stage_guard)
        finally:
            self._heavy(work.attempt_id, False)

    def commit(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        self._record("commit", work, stage_guard.heavy_work_permitted)
        waits = self.commit_waits.get(work.attempt_id, 0)
        if waits:
            # Readiness could not take its files now: a healthy wait.
            self.commit_waits[work.attempt_id] = waits - 1
            raise StageWaiting("publication waits for free space", retry_after_seconds=0.001)
        self._heavy(work.attempt_id, True)
        try:
            # Keep the permit while the light stages run beside it.
            self.light_done.wait(timeout=5)
            return super().commit(work, credit_allowance=credit_allowance, stage_guard=stage_guard)
        finally:
            self._heavy(work.attempt_id, False)

    def _light_finished(self, attempt_id: str) -> None:
        with self.lock:
            self.light_ids.discard(attempt_id)
            if not self.light_ids:
                self.light_done.set()

    def cleanup(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        self._record("cleanup", work, stage_guard.heavy_work_permitted)
        return super().cleanup(work, credit_allowance=credit_allowance, stage_guard=stage_guard)

    def acknowledge(self, work, *, stage_guard):  # type: ignore[no-untyped-def]
        self._record("ack", work, stage_guard.heavy_work_permitted)
        acked = super().acknowledge(work, stage_guard=stage_guard)
        self._light_finished(work.attempt_id)
        return acked


class HeavyWorkPermitTests(unittest.TestCase):
    def test_decode_and_commit_never_overlap_while_cleanup_and_ack_run_beside_them(self) -> None:
        backend = _PermitBackend(recoverable=(
            _work("a-commit", "local_materialized", 5),
            _work("b-decode", "materializing", 4),
            _work("c-cleanup", "cleanup_pending", 7),
            _work("d-ack", "ack_pending", 8),
        ))
        backend.light_ids = {"c-cleanup", "d-ack"}
        result = StagedParseCoordinator(backend=backend, limits=_limits()).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        # One whole-object holder at a time, across the separate lane pools.
        self.assertTrue(backend.overlaps)
        self.assertTrue(all(len(active) == 1 for active in backend.overlaps), backend.overlaps)
        # The first commit held the permit until the light stages had finished beside it.
        self.assertTrue(backend.light_done.is_set())
        first_exit = backend.events.index("exit:a-commit")
        self.assertLess(backend.events.index("enter:a-commit"), first_exit)
        # The LOCAL stopped before its decode without the permit, then decoded
        # only after the commit released it; COMMIT always held one.
        local = [permitted for lane, attempt, permitted in backend.permits if (lane, attempt) == ("local", "b-decode")]
        self.assertEqual(local, [False, True])
        self.assertGreater(backend.events.index("enter:b-decode"), first_exit)
        self.assertTrue(all(permitted for lane, _, permitted in backend.permits if lane == "commit"))
        self.assertTrue(all(permitted is False for lane, _, permitted in backend.permits
                            if lane in {"cleanup", "ack"}))

    def test_the_permit_ends_with_its_stage_when_the_holder_waits(self) -> None:
        backend = _PermitBackend(recoverable=(
            _work("a-commit", "local_materialized", 5),
            _work("b-decode", "materializing", 4),
        ))
        backend.light_done.set()
        backend.commit_waits["a-commit"] = 1
        result = StagedParseCoordinator(backend=backend, limits=_limits()).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertTrue(all(len(active) == 1 for active in backend.overlaps), backend.overlaps)
        commits = [permitted for lane, attempt, permitted in backend.permits if (lane, attempt) == ("commit", "a-commit")]
        self.assertEqual(commits[:2], [True, True])  # Waited, then held it again.
        self.assertIn("enter:b-decode", backend.events)


_CREDIT_LIMIT = ResourceCreditVector(
    documents=8, snapshot_items=8, snapshot_bytes=32 * KIB, remote_waits=4, provider_tasks=8,
    provider_result_bytes=32 * KIB, materialization_items=8, compressed_bytes=32 * KIB,
    decoded_bytes=32 * KIB, temp_disk_bytes=32 * KIB, output_items=8, output_bytes=32 * KIB,
    ack_items=8, output_pages=1_000,
)


def _document(zip_bytes: int, selected: int, working: int) -> ResourceCreditVector:
    """A document's lifecycle reservation: its grant's Z + S + W on the Mac volume."""
    return ResourceCreditVector(
        documents=1, snapshot_items=1, snapshot_bytes=0, remote_waits=1, provider_tasks=1,
        provider_result_bytes=zip_bytes, materialization_items=1, compressed_bytes=zip_bytes,
        decoded_bytes=working, temp_disk_bytes=zip_bytes + selected + working, output_items=1,
        output_bytes=selected + working, output_pages=1, ack_items=1,
    )


def _held(
    state: str, reservation: ResourceCreditVector, output: int = 0, snapshot: int = 0,
) -> ResourceCreditVector:
    """Durable credits per state; the source snapshot stays owned until cleanup."""
    base = dict(documents=1, snapshot_items=1, snapshot_bytes=snapshot, provider_tasks=1, ack_items=1,
                provider_result_bytes=reservation.provider_result_bytes)
    if state == "materializing":
        return ResourceCreditVector(
            **base, materialization_items=1, compressed_bytes=reservation.compressed_bytes,
            decoded_bytes=reservation.decoded_bytes, temp_disk_bytes=reservation.temp_disk_bytes,
        )
    if state in {"local_materialized", "publish_committed", "cleanup_pending"}:
        return ResourceCreditVector(
            **base, compressed_bytes=reservation.compressed_bytes, output_items=1, output_bytes=output,
            output_pages=1,
        )
    if state == "ack_pending":
        return ResourceCreditVector(documents=1, provider_tasks=1, ack_items=1,
                                    provider_result_bytes=reservation.provider_result_bytes)
    return ResourceCreditVector(**base)  # remote_terminal


class _OwnedFileBackend(_Backend):
    """Every durable transition writes or removes this attempt's real, scaled files."""

    def __init__(self, root: Path, documents: dict[str, tuple[int, int, int]], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.root = root
        self.documents = documents
        self.peak = 0
        self.lock = threading.Lock()
        self.b_wrote = threading.Event()

    def _write(self, attempt_id: str, name: str, size: int) -> None:
        path = self.root / attempt_id / name
        # The fixture's physical-byte observation must be atomic with every
        # write/remove; otherwise another lane can delete an inode between
        # is_file() and stat(), inventing a production stage failure.
        with self.lock:
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"\0" * size)
            total = sum(item.stat().st_size for item in self.root.rglob("*") if item.is_file())
            self.peak = max(self.peak, total)

    def _remove_all(self, attempt_id: str) -> None:
        with self.lock:
            for item in (self.root / attempt_id).glob("*"):
                item.unlink()

    def _next(self, work: CoordinatorWork, state: str, output: int = 0) -> CoordinatorWork:
        return replace(
            _work(work.attempt_id, state, work.lifecycle_version + 1),
            claim_generation=work.claim_generation, claim_owner_identity=work.claim_owner_identity,
            lease_expires_monotonic=work.lease_expires_monotonic,
            credit_reservation=work.credit_reservation,
            credits=_held(state, work.credit_reservation, output, snapshot=work.credits.snapshot_bytes),
        )

    def prepare_local_io(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        self.calls.append(f"local_prepare:{work.attempt_id}")
        updated = self._next(work, "materializing")
        self._assert_credit_grant(work, updated, credit_allowance)
        return updated

    def run_local(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        zip_bytes, selected, working = self.documents.get(work.attempt_id, (100, 200, 100))
        self._write(work.attempt_id, "spool.zip", zip_bytes)
        self._write(work.attempt_id, "selected.bin", selected)
        self._write(work.attempt_id, "envelope.json", working)  # Promoted in place: same extents.
        self.b_wrote.set()
        updated = self._next(work, "local_materialized", output=selected + working)
        self._assert_credit_grant(work, updated, credit_allowance)
        return updated

    def commit(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        # Readiness files go to the published corpus, outside the scratch quota.
        self.calls.append(f"commit:{work.attempt_id}")
        return self._next(work, "publish_committed", output=work.credits.output_bytes)

    def cleanup(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        self.calls.append(f"cleanup:{work.attempt_id}:{work.state}")
        if work.state == "publish_committed":
            self.outcome_by_attempt[work.attempt_id] = "success"
            return self._next(work, "cleanup_pending", output=work.credits.output_bytes)
        if work.attempt_id == "a":
            # Give an unquota'd second document every chance to write first.
            self.b_wrote.wait(timeout=0.3)
        self._remove_all(work.attempt_id)
        return self._next(work, "ack_pending")


class WorkDiskQuotaTests(unittest.TestCase):
    """Pro R3 at 1/2**20 scale: every dimension fits 32, the distinct extents reach 40."""

    def run_pair(self, work_disk_bytes: int | None) -> tuple[int, _OwnedFileBackend]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        a_reservation = _document(1 * KIB, 12 * KIB, 4 * KIB)
        b_reservation = _document(11 * KIB, 12 * KIB, 4 * KIB)
        a = replace(
            _work("a", "local_materialized", 5), credit_reservation=a_reservation,
            credits=_held("local_materialized", a_reservation, output=12 * KIB),
        )
        b = replace(_work("b", "remote_terminal", 3), credit_reservation=b_reservation,
                    credits=_held("remote_terminal", b_reservation))
        backend = _OwnedFileBackend(root, {"b": (11 * KIB, 12 * KIB, 4 * KIB)}, recoverable=(a, b))
        # A's recovered, verified extents: its retained spool and private output.
        backend._write("a", "spool.zip", 1 * KIB)
        backend._write("a", "output.bin", 12 * KIB)
        limits = _limits(credits=_CREDIT_LIMIT, work_disk_bytes=work_disk_bytes)
        result = StagedParseCoordinator(backend=backend, limits=limits).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        return backend.peak, backend

    def test_quota_holds_the_second_grant_until_the_first_document_releases_its_extents(self) -> None:
        peak, backend = self.run_pair(32 * KIB)
        self.assertLessEqual(peak, 32 * KIB)
        self.assertEqual(peak, 27 * KIB)  # B's own Z + S + W, never beside A's 13.
        # A's COMMIT ran while B waited; B's grant followed A's release.
        order = backend.calls
        self.assertLess(order.index("commit:a"), order.index("local_prepare:b"))
        self.assertLess(order.index("cleanup:a:cleanup_pending"), order.index("local_prepare:b"))

    def test_without_the_quota_the_same_valid_limits_let_the_volume_pass_d(self) -> None:
        peak, _ = self.run_pair(None)
        self.assertEqual(peak, 40 * KIB)  # A 13 + B 27 > D 32: the gap D closes.

    def test_recovered_ownership_counts_once_and_never_blocks_its_own_release(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        a_reservation = _document(1 * KIB, 12 * KIB, 4 * KIB)
        c_reservation = _document(4 * KIB, 12 * KIB, 4 * KIB)
        b_reservation = _document(2 * KIB, 4 * KIB, 2 * KIB)
        recovered = (  # Keyset order, as recovery reads it.
            replace(_work("a", "local_materialized", 5), credit_reservation=a_reservation,
                    credits=_held("local_materialized", a_reservation, output=12 * KIB)),
            replace(_work("b", "remote_terminal", 3), credit_reservation=b_reservation,
                    credits=_held("remote_terminal", b_reservation)),
            replace(_work("c", "cleanup_pending", 7), credit_reservation=c_reservation,
                    credits=_held("cleanup_pending", c_reservation, output=16 * KIB)),
        )
        backend = _OwnedFileBackend(root, {"b": (2 * KIB, 4 * KIB, 2 * KIB)}, recoverable=recovered)
        backend.b_wrote.set()
        # D below what recovery already owns (13 + 20): owned bytes are counted,
        # never reserved again, and only the new grant waits.
        limits = _limits(credits=_CREDIT_LIMIT, work_disk_bytes=16 * KIB)
        result = StagedParseCoordinator(backend=backend, limits=limits).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        order = backend.calls
        # The owners past D still commit and clean up: nothing strands release.
        self.assertIn("commit:a", order)
        self.assertIn("cleanup:c:cleanup_pending", order)
        # The new grant waits until both released their extents (13 + 20, then
        # 13, never room for its 8 inside 16).
        grant = order.index("local_prepare:b")
        self.assertLess(order.index("cleanup:c:cleanup_pending"), grant)
        self.assertLess(order.index("cleanup:a:cleanup_pending"), grant)

    def test_admission_offers_only_d_above_the_local_reserve_after_each_documents_margin(self) -> None:
        a_reservation = _document(1 * KIB, 12 * KIB, 4 * KIB)
        a = replace(_work("a", "local_materialized", 5), credit_reservation=a_reservation,
                    credits=_held("local_materialized", a_reservation, output=12 * KIB))
        new = replace(
            _work("n", "prepared"),
            credit_reservation=replace(_work("n", "prepared").credit_reservation, snapshot_bytes=KIB + 512),
            credits=ResourceCreditVector(documents=1, snapshot_items=1, snapshot_bytes=KIB + 512),
        )
        offered: list[tuple[int, int]] = []

        class _Admission(_OwnedFileBackend):
            def admit_new(self, *, limit, available_credits):  # type: ignore[no-untyped-def]
                offered.append((available_credits.documents, available_credits.snapshot_bytes))
                return super().admit_new(limit=limit, available_credits=available_credits)

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        backend = _Admission(Path(directory.name), {}, recoverable=(a,), new=(new,))
        backend.b_wrote.set()
        control = InProcessWorkerStopLatch()
        limits = _limits(credits=_CREDIT_LIMIT, work_disk_bytes=20 * KIB, work_disk_margin_bytes=KIB,
                         work_disk_local_reserve_bytes=4 * KIB)
        result = StagedParseCoordinator(backend=backend, limits=limits, stop_control=control).run()
        # A owns 13 + 1 of 20 and 4 stay reserved: one more document fits, and
        # its margin comes first, so the 1.5 KiB snapshot waits (offering the
        # 2 KiB headroom as snapshot bytes would carry D past its limit). Every
        # offer keeps each offered document's margin aside within the 16 KiB
        # that admission may ever use.
        self.assertEqual(offered[0], (1, KIB))
        self.assertTrue(all(documents * KIB + snapshot <= 16 * KIB for documents, snapshot in offered), offered)
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertIsNone(control.first_cause())
        self.assertEqual(dict(result.final_states)["n"], "acked")
        self.assertLess(backend.calls.index("cleanup:a:cleanup_pending"), backend.calls.index("preflight:n"))

    def test_a_waiting_grant_starts_once_local_work_drains_because_admission_keeps_the_reserve(self) -> None:
        def run(reserve: int) -> tuple[CoordinatorResult, _Backend]:
            # Eight new documents own a 100-byte snapshot each; each LOCAL grant
            # adds 500. Every per-dimension limit admits all eight at once.
            backend = _Backend(new=tuple(_work(f"n{index}", "prepared") for index in range(8)))
            stop = threading.Event()
            # A safety bound only: a live run is quiescent long before it.
            timer = threading.Timer(0.5 if reserve == 0 else 10.0, stop.set)
            timer.start()
            self.addCleanup(timer.cancel)
            limits = _limits(work_disk_bytes=1_200, work_disk_local_reserve_bytes=reserve,
                             admission_batch_size=8, idle_open_circuit_seconds=0.05)
            return StagedParseCoordinator(backend=backend, limits=limits).run(stop_requested=stop.is_set), backend

        result, backend = run(reserve=500)
        # Snapshots stay within D minus one grant, so the waiting grant starts
        # as soon as the LOCAL work ahead of it drains: every document completes.
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(dict(result.final_states), {f"n{index}": "acked" for index in range(8)})
        # Without the reserve the same valid limits admit 800 of snapshots and
        # no 500-byte grant ever fits beside them: nothing reaches LOCAL.
        result, backend = run(reserve=0)
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertFalse([call for call in backend.calls if call.startswith("local_prepare:")])

    def test_recovered_snapshots_beyond_the_reserve_let_fitting_work_drain_before_the_waiting_head(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        big = replace(_document(1 * KIB, 4 * KIB, 4 * KIB), snapshot_bytes=KIB)  # A 9 KiB grant.
        small = replace(_document(1 * KIB, 0, 1 * KIB), snapshot_bytes=KIB)  # A 2 KiB grant.
        names = ("big", "s1", "s2", "s3", "s4")
        recovered = tuple(  # Keyset order: the big grant heads LOCAL_PREPARE.
            replace(_work(name, "remote_terminal", 3), credit_reservation=big if name == "big" else small,
                    credits=_held("remote_terminal", big if name == "big" else small, snapshot=KIB))
            for name in names
        )
        backend = _OwnedFileBackend(
            Path(directory.name),
            {name: (1 * KIB, 4 * KIB, 4 * KIB) if name == "big" else (1 * KIB, 0, 1 * KIB) for name in names},
            recoverable=recovered,
        )
        backend.b_wrote.set()
        # Recovery owns 5 KiB of snapshots, past D minus the reserve (3 KiB):
        # the big grant cannot fit even once LOCAL work drains, so the small
        # grants run past it and release their snapshots, then it runs.
        limits = _limits(credits=_CREDIT_LIMIT, work_disk_bytes=12 * KIB, work_disk_local_reserve_bytes=9 * KIB,
                         idle_open_circuit_seconds=0.05)
        stop = threading.Event()
        timer = threading.Timer(10.0, stop.set)  # A stalled run ends here, visibly.
        timer.start()
        self.addCleanup(timer.cancel)
        result = StagedParseCoordinator(backend=backend, limits=limits).run(stop_requested=stop.is_set)
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(dict(result.final_states), {name: "acked" for name in names})
        big_start = backend.calls.index("local_prepare:big")
        self.assertTrue(any(backend.calls.index(f"cleanup:{name}:cleanup_pending") < big_start
                            for name in names[1:]))
        self.assertLessEqual(backend.peak, 12 * KIB)

    def test_footprint_counts_each_extent_once(self) -> None:
        reservation = _document(11 * KIB, 12 * KIB, 4 * KIB)
        granted = _held("materializing", reservation)
        # Spool and unpacked tree live inside the grant; the promoted output is
        # that tree renamed, so the grant alone counts while LOCAL runs.
        self.assertEqual(work_disk_footprint(granted), 27 * KIB)
        self.assertEqual(work_disk_footprint(granted + ResourceCreditVector(output_bytes=16 * KIB)), 27 * KIB)
        after = _held("local_materialized", reservation, output=16 * KIB)
        self.assertEqual(work_disk_footprint(after), 27 * KIB)
        self.assertEqual(work_disk_footprint(after, margin=KIB), 28 * KIB)
        self.assertEqual(work_disk_footprint(ResourceCreditVector(), margin=KIB), 0)



class PublicationEnvelopeHoldTests(unittest.TestCase):
    def test_an_oversized_publication_holds_only_its_own_document(self) -> None:
        class _Backend2(_Backend):
            def commit(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
                if work.attempt_id == "big":
                    self.calls.append("commit-hold:big")
                    raise StageCapacityBlocked(
                        "publication record bytes are outside the envelope", dimensions=("publication_envelope",),
                    )
                return super().commit(work, credit_allowance=credit_allowance, stage_guard=stage_guard)

        backend = _Backend2(recoverable=(
            _work("big", "local_materialized", 5), _work("small", "local_materialized", 5),
        ))
        calls_at_trip: list[int] = []
        control = InProcessWorkerStopLatch(on_first_trip=lambda _cause: calls_at_trip.append(len(backend.calls)))
        stop = threading.Event()
        timer = threading.Timer(10.0, stop.set)  # A safety bound only; a live run stops long before it.
        timer.start()
        self.addCleanup(timer.cancel)
        result = StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=control).run(
            stop_requested=stop.is_set,
        )
        # The held document keeps its claim and output while the other one
        # finishes; only when nothing else can run does the site stop, once.
        self.assertEqual(dict(result.final_states), {"small": "acked"})
        self.assertLess(backend.calls.index("ack:small:ack_pending"), calls_at_trip[0])
        self.assertEqual(backend.calls.count("commit-hold:big"), 1)
        self.assertFalse([call for call in backend.calls if call.startswith(("cleanup:big", "ack:big"))])
        self.assertIn("big:commit:stage_capacity_hold:publication_envelope", result.errors)
        cause = control.first_cause()
        assert cause is not None
        self.assertEqual((cause.kind, cause.reason_code, cause.attempt_id, cause.lane, cause.state_at_dispatch),
                         ("coordinator_circuit", "capacity_holds_exhausted", "big", "commit", "local_materialized"))


if __name__ == "__main__":
    unittest.main()
