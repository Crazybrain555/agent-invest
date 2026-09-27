"""Independent, synthetic continuation checks for a frozen v5 result spool."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
import random
import tempfile
import unittest
import zipfile

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import (
    MinerUHttpStagedV4,
    _SPOOL_OWNER_HEADER_BYTES,
    _SPOOL_PROGRESS_SLOT_BYTES,
)
from disclosure_anchor.application.ports.remote_provider_v4 import (
    RemoteProviderUnavailableV4,
    RemoteResultRangeIgnoredV4,
    RemoteResultRangeUnsatisfiableV4,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializationTransferContinuesV4,
    MaterializationTransferHeldV4,
)
from tests.unit.test_mineru_http_staged_v4 import (
    _Guard, _StepGuard, _official_zip, _published_test_root,
)
from tests.unit.test_mineru_materialize_grant_v5 import (
    _v5_fixture, synthetic_storage_policy,
)

MIB = 1024 * 1024


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _ResultTransport:
    """Scripted retained bytes; each event still enters the real spool writer."""

    def __init__(self, archive: bytes, clock: _Clock, events: list[tuple[str, int, float]]) -> None:
        self.archive = archive
        self.clock = clock
        self.events = events
        self.calls: list[tuple[int, str | None, str, str]] = []
        self.closed = 0

    def stream_result(self, **kwargs):
        start = kwargs["range_start"]
        self.calls.append((
            start, kwargs["strong_validator"],
            kwargs["accepted_submission"].remote_task_identity,
            kwargs["terminal_receipt"].result_owner_identity,
        ))
        action, length, elapsed = self.events.pop(0)
        try:
            self.clock.advance(elapsed)
            if action == "ignored":
                raise RemoteResultRangeIgnoredV4(same_identity=True)
            if action == "unsatisfiable":
                raise RemoteResultRangeUnsatisfiableV4("synthetic 416")
            if length:
                yield self.archive[start:start + length]
            if action == "interrupt":
                raise RemoteProviderUnavailableV4("synthetic read interrupted")
            if action == "complete":
                remaining = self.archive[start + length:]
                if remaining:
                    yield remaining
        finally:
            self.closed += 1


class ResultSpoolContinuationIndependentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.archive = _official_zip()
        self.policy = synthetic_storage_policy()
        self.fixture = _v5_fixture(
            self.archive, working_set=4 * MIB, input_limit=MIB,
            policy_sha256=self.policy.sha256,
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "scratch"
        self.root.mkdir(mode=0o700)
        self.published = _published_test_root(self.root)
        self.clock = _Clock()

    def _backend(self, transport: _ResultTransport) -> MinerUHttpStagedV4:
        return MinerUHttpStagedV4(
            scratch_root=self.root, published_root=self.published,
            transport=transport, clock=lambda: 1.0,
            monotonic=self.clock, storage_policy=self.policy,
        )

    def _paths(self):
        intent = self.fixture.intent
        return tuple(self.root / path for path in (
            intent.spool_relpath, intent.spool_part_relpath,
            intent.spool_part_owner_relpath,
        ))

    def _progress(self, backend: MinerUHttpStagedV4):
        return backend._spool_progress(self.fixture.intent, self._paths()[2].read_bytes())

    def _spool(self, backend: MinerUHttpStagedV4, guard: _Guard | None = None):
        fixture = self.fixture
        spool, part, owner = self._paths()
        with backend._locked(
            self.root / fixture.intent.spool_lock_relpath,
            "spool", backend._resource_binding(fixture.intent),
        ):
            return backend._ensure_spool(
                checkpoint=fixture.checkpoint, claim=fixture.claim,
                claim_guard=guard or _Guard(), intent=fixture.intent,
                accepted=fixture.accepted, terminal=fixture.terminal,
                capability=fixture.capability, stage_guard=_StepGuard(),
                result_lease_seconds=300, spool=spool, part=part, owner=owner,
            )

    def _assert_identity(self, transport: _ResultTransport, offsets: list[int]) -> None:
        intent = self.fixture.intent
        validator = '"' + intent.artifact_sha256.removeprefix("sha256:") + '"'
        self.assertEqual([item[0] for item in transport.calls], offsets)
        for start, observed_validator, task, owner in transport.calls:
            self.assertEqual(observed_validator, validator if start else None)
            self.assertEqual(task, self.fixture.accepted.remote_task_identity)
            self.assertEqual(owner, self.fixture.terminal.result_owner_identity)

    def test_two_interrupted_stages_resume_exact_prefix_then_materialize_same_zip(self) -> None:
        a, b = 400, 600
        transport = _ResultTransport(self.archive, self.clock, [
            ("interrupt", a, 1.0), ("interrupt", b, 1.0), ("complete", 0, 0.0),
        ])
        for expected in (a, a + b):
            with self.assertRaises(MaterializationTransferContinuesV4) as raised:
                self._spool(self._backend(transport))
            self.assertEqual(raised.exception.durable_offset, expected)
            self.assertEqual(self._progress(self._backend(transport)).durable_offset, expected)
        backend = self._backend(transport)  # a new backend instance, as after process reload
        materialized = backend.materialize_v4(**self.fixture.arguments(), claim_guard=_Guard())
        spool, part, owner = self._paths()
        self.assertEqual(spool.read_bytes(), self.archive)
        self.assertEqual("sha256:" + hashlib.sha256(spool.read_bytes()).hexdigest(),
                         self.fixture.terminal.artifact_sha256)
        self.assertFalse(part.exists())
        self.assertFalse(owner.exists())
        self.assertTrue((self.root / self.fixture.intent.output_relpath).is_dir())
        self.assertGreater(materialized.receipt.output_byte_count, 0)
        self._assert_identity(transport, [0, a, a + b])
        self.assertEqual(transport.closed, 3)

    def test_long_unrecorded_tail_is_cut_but_short_or_tampered_prefix_is_held(self) -> None:
        for mutation in ("long", "short", "tamper"):
            with self.subTest(mutation=mutation):
                # Each variant owns a fresh isolated namespace.
                with tempfile.TemporaryDirectory() as directory:
                    previous = (self.root, self.published)
                    self.root = Path(directory) / "scratch"
                    self.root.mkdir(mode=0o700)
                    self.published = _published_test_root(self.root)
                    try:
                        transport = _ResultTransport(self.archive, self.clock, [
                            ("interrupt", 400, 0.0), ("complete", 0, 0.0),
                        ])
                        with self.assertRaises(MaterializationTransferContinuesV4):
                            self._spool(self._backend(transport))
                        _, part, owner = self._paths()
                        if mutation == "long":
                            with part.open("ab") as file:
                                file.write(b"unrecorded tail")
                            self._spool(self._backend(transport))
                            self.assertEqual(self._paths()[0].read_bytes(), self.archive)
                            self._assert_identity(transport, [0, 400])
                        else:
                            with part.open("r+b") as file:
                                if mutation == "short":
                                    file.truncate(399)
                                else:
                                    file.seek(0)
                                    file.write(b"X")
                            reason = "spool_part_short" if mutation == "short" else "spool_prefix_mismatch"
                            with self.assertRaises(MaterializationTransferHeldV4) as raised:
                                self._spool(self._backend(transport))
                            self.assertEqual(raised.exception.reason, reason)
                            self.assertTrue(part.exists())
                            self.assertTrue(owner.exists())
                            self._assert_identity(transport, [0])
                    finally:
                        self.root, self.published = previous

    def test_ignored_range_and_416_get_one_restart_never_suffix_append(self) -> None:
        for action in ("ignored", "unsatisfiable"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                previous = (self.root, self.published)
                self.root = Path(directory) / "scratch"
                self.root.mkdir(mode=0o700)
                self.published = _published_test_root(self.root)
                try:
                    transport = _ResultTransport(self.archive, self.clock, [
                        ("interrupt", 500, 0.0), (action, 0, 0.0),
                        ("interrupt", 500, 0.0), (action, 0, 0.0),
                    ])
                    with self.assertRaises(MaterializationTransferContinuesV4):
                        self._spool(self._backend(transport))
                    expected = RemoteResultRangeIgnoredV4 if action == "ignored" else RemoteProviderUnavailableV4
                    with self.assertRaises(expected):
                        self._spool(self._backend(transport))
                    _, part, _ = self._paths()
                    self.assertEqual(part.read_bytes(), b"")
                    self.assertEqual(self._progress(self._backend(transport)).restarts, 1)
                    with self.assertRaises(MaterializationTransferContinuesV4):
                        self._spool(self._backend(transport))
                    with self.assertRaises(MaterializationTransferHeldV4) as raised:
                        self._spool(self._backend(transport))
                    self.assertEqual(raised.exception.reason, "transfer_range_unsupported")
                    self.assertEqual(part.read_bytes(), self.archive[:500])
                    self._assert_identity(transport, [0, 500, 0, 500])
                finally:
                    self.root, self.published = previous

    def test_trickle_and_zero_progress_window_survive_reopen(self) -> None:
        # This finite policy permits the hard result at its stated minimum rate.
        self.policy = synthetic_storage_policy(
            transfer_logical_deadline_seconds=30, progress_window_seconds=10,
            minimum_progress_bytes=4 * MIB,
        )
        self.fixture = _v5_fixture(
            self.archive, working_set=4 * MIB, input_limit=MIB,
            policy_sha256=self.policy.sha256,
        )
        transport = _ResultTransport(self.archive, self.clock, [
            ("interrupt", 1, 5.0), ("interrupt", 1, 6.0),
        ])
        with self.assertRaises(MaterializationTransferContinuesV4):
            self._spool(self._backend(transport))
        with self.assertRaises(MaterializationTransferHeldV4) as raised:
            self._spool(self._backend(transport))
        self.assertEqual(raised.exception.reason, "transfer_progress")
        self.assertEqual(self._progress(self._backend(transport)).window_start_offset, 0)
        self.assertEqual(self._paths()[1].read_bytes(), self.archive[:2])
        self._assert_identity(transport, [0, 1])

        # A separate owner with no bytes cannot turn repeated unavailability
        # into an unbounded succession of fresh progress windows.
        with tempfile.TemporaryDirectory() as directory:
            previous = (self.root, self.published)
            self.root = Path(directory) / "scratch"
            self.root.mkdir(mode=0o700)
            self.published = _published_test_root(self.root)
            self.clock = _Clock()
            try:
                idle = _ResultTransport(self.archive, self.clock, [
                    ("interrupt", 0, 11.0), ("complete", 0, 0.0),
                ])
                with self.assertRaises(RemoteProviderUnavailableV4):
                    self._spool(self._backend(idle))
                self.assertEqual(self._progress(self._backend(idle)).window_active_ms, 11_000)
                with self.assertRaises(MaterializationTransferHeldV4) as held:
                    self._spool(self._backend(idle))
                self.assertEqual(held.exception.reason, "transfer_progress")
                self._assert_identity(idle, [0])
            finally:
                self.root, self.published = previous

    def test_torn_newest_slot_falls_back_and_claim_loss_preserves_prefix(self) -> None:
        transport = _ResultTransport(self.archive, self.clock, [
            ("interrupt", 300, 0.0), ("interrupt", 200, 0.0), ("complete", 0, 0.0),
        ])
        with self.assertRaises(MaterializationTransferContinuesV4):
            self._spool(self._backend(transport))
        with self.assertRaises(MaterializationTransferContinuesV4):
            self._spool(self._backend(transport))
        _, part, owner = self._paths()
        before = self._progress(self._backend(transport))
        self.assertEqual(before.durable_offset, 500)
        # Tear the newest fixed-size slot; the older slot still proves 300 bytes.
        slot_index = before.sequence % 2
        with owner.open("r+b") as file:
            file.seek(_SPOOL_OWNER_HEADER_BYTES + slot_index * _SPOOL_PROGRESS_SLOT_BYTES)
            file.write(b"!")
        with self.assertRaises(RuntimeError):
            self._spool(self._backend(transport), guard=_Guard(fail_at=1))
        self.assertEqual(part.read_bytes(), self.archive[:500])
        self.assertEqual(self._progress(self._backend(transport)).durable_offset, 300)
        self._spool(self._backend(transport))
        self.assertEqual(self._paths()[0].read_bytes(), self.archive)
        self._assert_identity(transport, [0, 300, 300])

    def test_absolute_transfer_deadline_is_not_refreshed_by_reopening(self) -> None:
        # A ~54 KiB incompressible ZIP makes three genuine minimum-progress
        # windows possible under a 64 KiB hard result grant.
        with io.BytesIO() as output:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                zipped.writestr("payload.bin", random.Random(17).randbytes(54_000))
            self.archive = output.getvalue()
        self.policy = synthetic_storage_policy(
            native_source_single_limit_bytes=1024,
            native_result_hard_limit_bytes=64 * 1024,
            native_normal_unacked_target_bytes=64 * 1024,
            initial_result_estimate_bytes=16 * 1024,
            transfer_logical_deadline_seconds=40,
            progress_window_seconds=10,
            minimum_progress_bytes=16 * 1024,
        )
        self.fixture = _v5_fixture(
            self.archive, working_set=4 * MIB, input_limit=MIB,
            policy_sha256=self.policy.sha256,
        )
        transport = _ResultTransport(self.archive, self.clock, [
            ("interrupt", 16 * 1024, 9.0),
            ("interrupt", 16 * 1024, 1.0),
            ("interrupt", 16 * 1024, 10.0),
            ("complete", 0, 20.0),
        ])
        for expected in (16 * 1024, 32 * 1024, 48 * 1024):
            with self.assertRaises(MaterializationTransferContinuesV4) as continued:
                self._spool(self._backend(transport))
            self.assertEqual(continued.exception.durable_offset, expected)
        with self.assertRaises(MaterializationTransferHeldV4) as held:
            self._spool(self._backend(transport))
        self.assertEqual(held.exception.reason, "transfer_logical_deadline")
        self.assertEqual(self._progress(self._backend(transport)).active_ms, 40_000)
        self.assertEqual(self._paths()[1].read_bytes(), self.archive[:48 * 1024])
        self._assert_identity(transport, [0, 16 * 1024, 32 * 1024, 48 * 1024])
