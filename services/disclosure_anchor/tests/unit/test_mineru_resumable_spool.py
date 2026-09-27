"""Resumable spool of a granted (v5) result: durable prefixes across stages.

A synthetic ranged transport serves the official synthetic MinerU bundle and a
fake monotonic clock drives stage deadlines and transfer budgets. Nothing here
reaches a network, provider or database; a synthetic interruption is not a
claim about real power or link failures.
"""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock

from disclosure_anchor.adapters.parsers.mineru_medium import http_staged_v4
from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import MinerUHttpStagedV4
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.ports.remote_provider_v4 import (
    RemoteProviderUnavailableV4,
    RemoteResultRangeIgnoredV4,
    RemoteResultRangeUnsatisfiableV4,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializationCapacityWaitV4,
    MaterializationTransferContinuesV4,
    MaterializationTransferHeldV4,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    RetryStage,
    StageCapacityBlocked,
    StageWaiting,
)
from disclosure_anchor.domain.errors import ParserOutputContractError
from tests.unit.test_mineru_http_staged_v4 import _Guard, _official_zip, _published_test_root
from tests.unit.test_mineru_materialize_grant_v5 import (
    MIB,
    STORAGE_POLICY,
    _v5_fixture,
    synthetic_storage_policy,
)

# A slow-link policy: 256 B per 10 s window still delivers the 12 MiB hard
# result inside its logical deadline, so window and deadline cases separate.
SLOW_LINK_POLICY = synthetic_storage_policy(
    transfer_logical_deadline_seconds=12 * MIB // 256 * 10, minimum_progress_bytes=256,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Stage:
    """A stage guard whose deadline is fixed on the fake clock."""

    def __init__(self, clock: _Clock, seconds: float = 3_600.0) -> None:
        self.clock = clock
        self.deadline = clock() + seconds

    def remaining_seconds(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise RuntimeError("stage lease lost")
        return remaining

    def checkpoint(self) -> None:
        self.remaining_seconds()

    def note(self, kind: str, **scalars: object) -> None:
        del kind, scalars


class _RangedTransport:
    """Serves ``result`` or its suffix; can drop, ignore Range or refuse it."""

    def __init__(self, result: bytes, clock: _Clock, *, chunk: int = 512) -> None:
        self.result = result
        self.clock = clock
        self.chunk = chunk
        self.advance = 0.0
        self.drop_after: int | None = None
        self.ignore_range: bool | None = None
        self.unsatisfiable = False
        self.requests: list[tuple[int, str | None]] = []
        self.closes = 0

    def stream_result(
        self,
        *,
        step_guard: Any,
        before_result_get: Any,
        range_start: int = 0,
        strong_validator: str | None = None,
        **_: object,
    ) -> Iterator[bytes]:
        self.requests.append((range_start, strong_validator))
        try:
            before_result_get()
            step_guard.checkpoint()
            if range_start and self.ignore_range is not None:
                raise RemoteResultRangeIgnoredV4(same_identity=self.ignore_range)
            if range_start and self.unsatisfiable:
                raise RemoteResultRangeUnsatisfiableV4("synthetic 416")
            sent = 0
            data = self.result[range_start:]
            for index in range(0, len(data), self.chunk):
                if self.drop_after is not None and sent >= self.drop_after:
                    raise RemoteProviderUnavailableV4("synthetic connection drop")
                step_guard.checkpoint()
                piece = data[index:index + self.chunk]
                self.clock.advance(self.advance)
                yield piece
                sent += len(piece)
        finally:
            self.closes += 1


class ResumableSpoolTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "scratch"
        self.root.mkdir(mode=0o700)
        self.archive = _official_zip()
        self.fixture = _v5_fixture(self.archive, working_set=4 * MIB, input_limit=MIB)
        self.clock = _Clock()
        self.transport = _RangedTransport(self.archive, self.clock)

    def backend(self, policy: Any = STORAGE_POLICY) -> MinerUHttpStagedV4:
        return MinerUHttpStagedV4(
            scratch_root=self.root, published_root=_published_test_root(self.root),
            transport=self.transport, clock=lambda: 1.0, storage_policy=policy, monotonic=self.clock,
        )

    def materialize(self, *, stage: _Stage | None = None, policy: Any = STORAGE_POLICY) -> Any:
        arguments = self.fixture.arguments()
        arguments["stage_guard"] = stage or _Stage(self.clock)
        return self.backend(policy).materialize_v4(**arguments, claim_guard=_Guard())

    @property
    def part(self) -> Path:
        return self.root / self.fixture.intent.spool_part_relpath

    @property
    def owner(self) -> Path:
        return self.root / self.fixture.intent.spool_part_owner_relpath

    @property
    def spool(self) -> Path:
        return self.root / self.fixture.intent.spool_relpath

    def progress(self) -> Any:
        records = []
        exact = self.owner.read_bytes()
        for index in (0, 1):
            start = http_staged_v4._SPOOL_OWNER_HEADER_BYTES + index * http_staged_v4._SPOOL_PROGRESS_SLOT_BYTES
            record = http_staged_v4._SpoolProgress.from_slot(
                exact[start:start + http_staged_v4._SPOOL_PROGRESS_SLOT_BYTES]
            )
            if record is not None:
                records.append(record)
        return max(records, key=lambda item: item.sequence)

    def interrupted(self, drop_after: int) -> int:
        self.transport.drop_after = drop_after
        with self.assertRaises(MaterializationTransferContinuesV4) as continued:
            self.materialize()
        self.transport.drop_after = None
        offset = continued.exception.durable_offset
        self.assertEqual(self.progress().durable_offset, offset)
        self.assertEqual(self.part.read_bytes(), self.archive[:offset])
        return offset

    def test_dropped_transfer_resumes_its_durable_prefix_to_the_same_result(self) -> None:
        third = len(self.archive) // 3
        first = self.interrupted(third)
        self.transport.drop_after = third
        with self.assertRaises(MaterializationTransferContinuesV4) as continued:
            self.materialize()
        second = continued.exception.durable_offset
        self.assertGreater(second, first)
        self.transport.drop_after = None
        materialized = self.materialize()
        validator = '"' + self.fixture.intent.artifact_sha256.removeprefix("sha256:") + '"'
        self.assertEqual(self.transport.requests, [(0, None), (first, validator), (second, validator)])
        # No byte was appended twice: the promoted spool is the exact result.
        self.assertEqual(self.spool.read_bytes(), self.archive)
        self.assertFalse(self.part.exists() or self.owner.exists())
        self.assertLessEqual(
            materialized.receipt.temporary_disk_peak_byte_count, self.fixture.intent.temporary_disk_byte_limit,
        )
        self.assertEqual(self.transport.closes, len(self.transport.requests))

    def test_segment_stops_before_its_stage_deadline_and_continues_later(self) -> None:
        self.fixture = _v5_fixture(
            self.archive, working_set=4 * MIB, input_limit=MIB, policy_sha256=SLOW_LINK_POLICY.sha256,
        )
        self.transport.chunk = len(self.archive) // 8 + 1
        self.transport.advance = 5.0
        with self.assertRaises(MaterializationTransferContinuesV4) as continued:
            # A 40 s stage minus the 10 s safe-stop margin leaves 30 s: five
            # 5 s chunks are written, the sixth arrives at the stop point.
            self.materialize(stage=_Stage(self.clock, 40.0), policy=SLOW_LINK_POLICY)
        offset = continued.exception.durable_offset
        self.assertEqual(offset, 5 * self.transport.chunk)
        record = self.progress()
        self.assertEqual((record.durable_offset, record.active_ms), (offset, 30_000))
        self.transport.advance = 0.0
        self.materialize(policy=SLOW_LINK_POLICY)
        self.assertEqual(self.spool.read_bytes(), self.archive)

    def test_prefix_tamper_is_held_and_the_part_retained(self) -> None:
        offset = self.interrupted(len(self.archive) // 2)
        tampered = bytearray(self.part.read_bytes())
        tampered[offset // 2] ^= 0xFF
        self.part.write_bytes(bytes(tampered))
        requests = list(self.transport.requests)
        with self.assertRaises(MaterializationTransferHeldV4) as held:
            self.materialize()
        self.assertEqual(held.exception.reason, "spool_prefix_mismatch")
        self.assertEqual(self.part.read_bytes(), bytes(tampered))
        self.assertEqual(self.transport.requests, requests)

    def test_unrecorded_tail_is_cut_before_the_suffix_is_appended(self) -> None:
        self.interrupted(len(self.archive) // 2)
        with self.part.open("ab") as handle:
            handle.write(b"never recorded")
        self.materialize()
        self.assertEqual(self.spool.read_bytes(), self.archive)

    def test_part_shorter_than_its_record_is_held(self) -> None:
        offset = self.interrupted(len(self.archive) // 2)
        with self.part.open("r+b") as handle:
            handle.truncate(offset - 1)
        with self.assertRaises(MaterializationTransferHeldV4) as held:
            self.materialize()
        self.assertEqual(held.exception.reason, "spool_part_short")
        self.assertTrue(self.part.exists())

    def test_part_without_a_proven_owner_is_held_not_restarted(self) -> None:
        for directory in reversed(self.part.relative_to(self.root).parents[:-1]):
            (self.root / directory).mkdir(mode=0o700, exist_ok=True)
        self.part.write_bytes(b"foreign partial")
        self.part.chmod(0o600)
        with self.assertRaises(MaterializationTransferHeldV4) as held:
            self.materialize()
        self.assertEqual(held.exception.reason, "spool_owner_unproven")
        self.assertEqual(self.part.read_bytes(), b"foreign partial")
        self.assertEqual(self.transport.requests, [])

    def test_ignored_range_restarts_once_from_zero_then_holds(self) -> None:
        self.interrupted(len(self.archive) // 2)
        self.transport.ignore_range = True
        with self.assertRaises(RemoteResultRangeIgnoredV4):
            self.materialize()  # A full body is never appended to a prefix.
        self.assertEqual(self.part.read_bytes(), b"")
        self.assertEqual((self.progress().durable_offset, self.progress().restarts), (0, 1))
        self.interrupted(len(self.archive) // 2)  # Offset 0: a plain GET, then a drop.
        with self.assertRaises(MaterializationTransferHeldV4) as held:
            self.materialize()
        self.assertEqual(held.exception.reason, "transfer_range_unsupported")
        self.assertTrue(self.part.exists())

    def test_unsatisfiable_range_restarts_like_an_ignored_range(self) -> None:
        self.interrupted(len(self.archive) // 2)
        self.transport.unsatisfiable = True
        with self.assertRaises(RemoteProviderUnavailableV4):
            self.materialize()
        self.assertEqual((self.part.read_bytes(), self.progress().restarts), (b"", 1))
        self.materialize()  # From zero: no Range, so the 416 path is not taken.
        self.assertEqual(self.spool.read_bytes(), self.archive)

    def test_full_body_naming_other_bytes_is_identity_drift(self) -> None:
        self.interrupted(len(self.archive) // 2)
        self.transport.ignore_range = False
        with self.assertRaises(ParserOutputContractError):
            self.materialize()

    def test_logical_budget_survives_restart_and_holds_without_new_requests(self) -> None:
        self.transport.advance = 700.0  # One chunk spends the 600 s logical budget.
        with self.assertRaises(MaterializationTransferHeldV4) as held:
            self.materialize()
        self.assertEqual(held.exception.reason, "transfer_logical_deadline")
        self.assertGreaterEqual(self.progress().active_ms, 600_000)
        requests = list(self.transport.requests)
        with self.assertRaises(MaterializationTransferHeldV4) as again:
            self.materialize()  # A fresh backend: the spent budget is durable.
        self.assertEqual(again.exception.reason, "transfer_logical_deadline")
        self.assertEqual(self.transport.requests, requests)

    def test_trickle_is_held_after_its_progress_window(self) -> None:
        self.transport.chunk = 1
        self.transport.advance = 1.0
        with self.assertRaises(MaterializationTransferHeldV4) as held:
            self.materialize()
        self.assertEqual(held.exception.reason, "transfer_progress")
        self.assertEqual(self.progress().durable_offset, len(self.part.read_bytes()))

    def test_torn_newest_record_falls_back_and_both_torn_is_held(self) -> None:
        self.interrupted(len(self.archive) // 2)
        newest = self.progress()
        exact = bytearray(self.owner.read_bytes())
        start = (
            http_staged_v4._SPOOL_OWNER_HEADER_BYTES
            + (newest.sequence % 2) * http_staged_v4._SPOOL_PROGRESS_SLOT_BYTES
        )
        exact[start + 3] ^= 0x01
        self.owner.write_bytes(bytes(exact))
        # The older record (offset 0) governs: the unrecorded tail is cut and
        # the transfer starts over with a plain GET.
        self.materialize()
        self.assertEqual(self.transport.requests[-1], (0, None))
        self.assertEqual(self.spool.read_bytes(), self.archive)

    def test_both_records_torn_is_held(self) -> None:
        self.interrupted(len(self.archive) // 2)
        exact = bytearray(self.owner.read_bytes())
        for index in (0, 1):
            exact[http_staged_v4._SPOOL_OWNER_HEADER_BYTES + index * http_staged_v4._SPOOL_PROGRESS_SLOT_BYTES] ^= 0x01
        self.owner.write_bytes(bytes(exact))
        with self.assertRaises(MaterializationTransferHeldV4) as held:
            self.materialize()
        self.assertEqual(held.exception.reason, "spool_progress_unproven")
        self.assertTrue(self.part.exists())

    def test_crash_after_promotion_keeps_the_spool_and_retires_its_receipt(self) -> None:
        def crash(phase: str) -> None:
            if phase == "after_spool_rename":
                raise RuntimeError("synthetic crash after promotion")

        backend = MinerUHttpStagedV4(
            scratch_root=self.root, published_root=_published_test_root(self.root),
            transport=self.transport, clock=lambda: 1.0, storage_policy=STORAGE_POLICY,
            monotonic=self.clock, fault_hook=crash,
        )
        arguments = self.fixture.arguments()
        arguments["stage_guard"] = _Stage(self.clock)
        with self.assertRaises(RuntimeError):
            backend.materialize_v4(**arguments, claim_guard=_Guard())
        self.assertEqual((self.spool.read_bytes(), self.owner.exists()), (self.archive, True))
        self.materialize()
        self.assertFalse(self.owner.exists())
        self.assertEqual(len(self.transport.requests), 1)  # The promoted spool is never fetched again.

    def test_materializer_without_the_granted_policy_refuses(self) -> None:
        other = synthetic_storage_policy(mac_decode_stage_seconds=301)
        with self.assertRaises(ParserOutputContractError):
            self.materialize(policy=other)
        self.assertEqual(self.transport.requests, [])


class MacSpaceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "scratch"
        self.root.mkdir(mode=0o700)
        self.archive = _official_zip()
        self.fixture = _v5_fixture(self.archive, working_set=4 * MIB, input_limit=MIB)
        self.transport = _RangedTransport(self.archive, _Clock())

    def backend(self, policy: Any = STORAGE_POLICY) -> MinerUHttpStagedV4:
        return MinerUHttpStagedV4(
            scratch_root=self.root, published_root=_published_test_root(self.root),
            transport=self.transport, clock=lambda: 1.0, storage_policy=policy,
        )

    def test_policy_must_describe_the_actual_work_volume(self) -> None:
        other = synthetic_storage_policy(mac_volume_total_bytes=STORAGE_POLICY.mac_volume_total_bytes + 4096)
        with self.assertRaises(ValueError):
            self.backend(other)

    def test_live_free_must_cover_the_floor_and_every_in_flight_promise(self) -> None:
        backend = self.backend()
        promise = self.fixture.intent.resource_grant.limits.temp_disk_bytes
        floor = STORAGE_POLICY.mac_free_floor_bytes
        real = os.statvfs(self.root)
        frsize = real.f_frsize

        def free(amount: int) -> Any:
            return mock.patch.object(
                http_staged_v4.os, "statvfs",
                return_value=SimpleNamespace(f_blocks=real.f_blocks, f_frsize=frsize, f_bavail=amount // frsize),
            )

        arguments = {**self.fixture.arguments(), "stage_guard": _Stage(_Clock())}
        backend._promises["another-attempt"] = 3 * frsize
        with free(floor + promise), self.assertRaises(MaterializationCapacityWaitV4) as waiting:
            backend.materialize_v4(**arguments, claim_guard=_Guard())
        self.assertEqual(waiting.exception.dimension, "mac_free_floor")
        self.assertEqual(self.transport.requests, [])  # Nothing written or fetched for it.
        self.assertFalse((self.root / self.fixture.intent.spool_part_owner_relpath).exists())
        with free(floor + promise + 3 * frsize + frsize):
            backend.materialize_v4(**arguments, claim_guard=_Guard())
        self.assertEqual(set(backend._promises), {"another-attempt"})  # Its own promise retired.


class TransferOutcomeMappingTests(unittest.TestCase):
    def run_local_raising(self, error: BaseException) -> BaseException:
        from tests.unit.test_mineru_http_staged_v4 import _exact_materialization_reservation_and_allowance
        from tests.unit.test_staged_coordinator_backend_v4 import (
            V4StageInputResolver,
            _authority,
            _backend,
            _guard,
            _work,
        )

        materializing = _authority("materializing")
        materialization = mock.Mock()
        materialization.materialize_v4.side_effect = error
        inputs = mock.Mock(spec=V4StageInputResolver)
        _, allowance = _exact_materialization_reservation_and_allowance()
        inputs.materialization_allowance.return_value = allowance
        inputs.result_lease_seconds.return_value = 300
        backend, persistence, _, _ = _backend(materializing, inputs=inputs, materialization=materialization)
        reservation = materializing.reservation
        assert reservation is not None
        grant = ResourceCreditVector(
            output_items=reservation.reserved_credit.output_items,
            output_bytes=reservation.reserved_credit.output_bytes,
            output_pages=reservation.reserved_credit.output_pages,
        )
        with mock.patch.object(backend, "_capability", return_value=mock.sentinel.capability):
            with self.assertRaises((StageWaiting, RetryStage)) as raised:
                backend.run_local(_work(materializing), credit_allowance=grant, stage_guard=_guard())
        self.assertEqual(persistence.appends, [])
        return raised.exception

    def test_space_shortfall_is_a_healthy_wait_of_the_same_attempt(self) -> None:
        waiting = self.run_local_raising(MaterializationCapacityWaitV4("mac_free_floor"))
        self.assertNotIsInstance(waiting, (RetryStage, StageCapacityBlocked))
        self.assertGreater(waiting.retry_after_seconds, 0)

    def test_progress_is_a_budget_free_wait_and_a_spent_budget_a_visible_hold(self) -> None:
        continued = self.run_local_raising(
            MaterializationTransferContinuesV4(durable_offset=10, artifact_byte_count=20)
        )
        self.assertNotIsInstance(continued, (RetryStage, StageCapacityBlocked))
        held = self.run_local_raising(MaterializationTransferHeldV4("transfer_progress"))
        assert isinstance(held, StageCapacityBlocked)
        self.assertEqual(held.dimensions, ("transfer_progress",))
        self.assertIsInstance(
            self.run_local_raising(RemoteProviderUnavailableV4("no progress")), RetryStage,
        )


class SpoolProgressRecordTests(unittest.TestCase):
    def test_records_are_closed_checked_and_fixed_width(self) -> None:
        record = http_staged_v4._SpoolProgress(
            sequence=3, durable_offset=10, prefix_sha256="sha256:" + hashlib.sha256(b"x").hexdigest(),
            part_device=1, part_inode=2, active_ms=5, window_active_ms=4, window_start_offset=9, restarts=0,
        )
        slot = record.slot_bytes()
        self.assertEqual(len(slot), http_staged_v4._SPOOL_PROGRESS_SLOT_BYTES)
        self.assertEqual(http_staged_v4._SpoolProgress.from_slot(slot), record)
        self.assertIsNone(http_staged_v4._SpoolProgress.from_slot(http_staged_v4._BLANK_PROGRESS_SLOT))
        for corrupt in (slot.replace(b'"restarts":0', b'"restarts":1'), slot[:-1] + b" ", slot[:100]):
            with self.subTest(corrupt=corrupt[:40]), self.assertRaises(ValueError):
                http_staged_v4._SpoolProgress.from_slot(corrupt)
        with self.assertRaises(ValueError):  # A window cannot start past the durable prefix.
            http_staged_v4._SpoolProgress(
                sequence=3, durable_offset=10, prefix_sha256=record.prefix_sha256, part_device=1,
                part_inode=2, active_ms=5, window_active_ms=4, window_start_offset=11, restarts=0,
            )


if __name__ == "__main__":
    unittest.main()
