from __future__ import annotations

from dataclasses import dataclass, replace
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import pickle
import shutil
import stat
import tempfile
import threading
import time
import tracemalloc
from typing import Any, Iterator, cast
import unittest
from unittest import mock
import zipfile

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import (
    MinerUHttpStagedV4,
    ProviderAckTransportResponseV4,
    _ZipPathCollisionIndex,
)
from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import (
    PinnedArtifactTree,
)
from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_FILENAME,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    FailureReceiptV4,
    SnapshotReceiptV4,
    SubmissionIntentV4,
    TerminalReceiptV4,
    build_preparation_intent_v4,
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    CleanupResourceEntryV4,
    MaterializationIntentV4,
    RemoteParseCheckpointV4,
    ResourceReservationV4,
    advance_remote_parse_checkpoint_v4,
    build_initial_remote_parse_checkpoint_v4,
    build_local_cleanup_plan_v4,
    build_materialization_intent_v4,
    build_resource_reservation_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    PerAttemptResourceAllowance,
    ResourceCreditVector,
    encode_resource_reservation_input,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    PrivateProviderCapabilityV4,
    ProviderAckCommandV4,
    V4ClaimWitness,
    V4EvidenceReplayContext,
    seal_provider_ack_command_v4,
)
from disclosure_anchor.domain.errors import ParserOutputContractError
from tests.unit.test_mineru_medium_artifacts import _write_bundle
from tests.unit.test_remote_parse_evidence_v4 import (
    _exact_materialization_reservation_and_allowance,
    _typed_pre_submission_failure_bundle,
)
from tests.unit.test_remote_parse_lifecycle_v4 import (
    _provider_envelope_context,
    _snapshot_credit,
    _submitted_credit,
)
from tests.unit.test_staged_provider_parser_v4 import (
    _ack_replay,
    _happy_path_for_port,
)


@dataclass(frozen=True)
class _MaterializeFixture:
    reservation: ResourceReservationV4
    allowance: PerAttemptResourceAllowance
    preparation: Any
    accepted: AcceptedSubmissionReceiptV4
    terminal: TerminalReceiptV4
    intent: MaterializationIntentV4
    checkpoint: RemoteParseCheckpointV4
    capability: PrivateProviderCapabilityV4
    claim: V4ClaimWitness
    replay: V4EvidenceReplayContext
    evidence_values: tuple[Any, ...]
    history: tuple[RemoteParseCheckpointV4, ...]

    def arguments(self) -> Any:
        return {
            "checkpoint": self.checkpoint,
            "reservation": self.reservation,
            "preparation_intent": self.preparation,
            "intent": self.intent,
            "accepted_submission": self.accepted,
            "terminal_receipt": self.terminal,
            "provider_capability": self.capability,
            "claim": self.claim,
            "stage_guard": _StepGuard(),
            "result_lease_seconds": 300,
            "allowance": self.allowance,
            "replay_context": self.replay,
        }


@dataclass(frozen=True)
class _SubmissionSnapshotFixture:
    source: bytes
    reservation: ResourceReservationV4
    snapshot: SnapshotReceiptV4
    submission: SubmissionIntentV4
    checkpoint: RemoteParseCheckpointV4
    evidence: tuple[Any, ...]
    history: tuple[RemoteParseCheckpointV4, ...]

    def source_arguments(self, *, claim_guard: _Guard) -> dict[str, Any]:
        return {
            "checkpoint": self.checkpoint,
            "reservation": self.reservation,
            "snapshot_receipt": self.snapshot,
            "submission_intent": self.submission,
            "evidence": tuple(
                encode_remote_parse_evidence_v4(value) for value in self.evidence
            ),
            "resourceful_checkpoint_history": self.history,
            "claim": _claim(self.checkpoint),
            "claim_guard": claim_guard,
        }


@dataclass(frozen=True, slots=True)
class _ProjectionFile:
    relative_path: PurePosixPath
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _ProjectionTree:
    files: tuple[_ProjectionFile, ...]
    directory_paths: tuple[PurePosixPath, ...]


class _Guard:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.calls = 0
        self.fail_at = fail_at

    def assert_current_under_resource_lock(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        claim: V4ClaimWitness,
    ) -> None:
        self.calls += 1
        if not claim.validates(checkpoint):
            raise AssertionError("test supplied a stale claim")
        if self.fail_at is not None and self.calls >= self.fail_at:
            raise RuntimeError("claim lost")


class _StepGuard:
    def __init__(self) -> None:
        self.calls = 0

    def checkpoint(self) -> None:
        self.calls += 1

    def remaining_seconds(self) -> float:
        self.checkpoint()
        return 60.0


class _Transport:
    def __init__(
        self,
        result: bytes,
        *,
        response: ProviderAckTransportResponseV4 | BaseException | None = None,
    ) -> None:
        self.result = result
        self.response = response or ProviderAckTransportResponseV4(204, b"")
        self.downloads = 0
        self.stream_closes = 0
        self.acks = 0
    def stream_result(self, **_: object) -> Iterator[bytes]:
        self.downloads += 1
        midpoint = max(1, len(self.result) // 2)
        try:
            yield self.result[:midpoint]
            if self.result[midpoint:]:
                yield self.result[midpoint:]
        finally:
            self.stream_closes += 1

    def acknowledge(
        self,
        *,
        command: ProviderAckCommandV4,
        provider_capability: PrivateProviderCapabilityV4,
        step_guard: _StepGuard,
        before_ack_post: Any,
    ) -> ProviderAckTransportResponseV4:
        del command, provider_capability
        step_guard.checkpoint()
        before_ack_post()
        self.acks += 1
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class MinerUHttpStagedV4Tests(unittest.TestCase):
    def test_submission_snapshot_source_is_opaque_and_holds_no_lock_on_upload(
        self,
    ) -> None:
        fixture = _submission_snapshot_fixture(
            b"%PDF-1.7\nexact pinned submission snapshot\n%%EOF\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(b"unused"),
                clock=lambda: 1.0,
            )
            snapshot = root / fixture.reservation.snapshot_relpath
            with backend._open_dir(snapshot.parent, create=True):
                pass
            snapshot.write_bytes(fixture.source)
            snapshot.chmod(0o600)
            claim_guard = _Guard()
            source = backend.submission_snapshot_source_v4(
                **fixture.source_arguments(claim_guard=claim_guard)
            )

            self.assertTrue(
                source.validates(
                    submission_intent=fixture.submission,
                    snapshot_receipt=fixture.snapshot,
                )
            )
            self.assertNotIn(str(root), repr(source))
            self.assertNotIn(fixture.reservation.snapshot_relpath, repr(source))
            with self.assertRaisesRegex(TypeError, "non-serializable"):
                pickle.dumps(source)

            step_guard = _StepGuard()
            entered_same_resource_lock = threading.Event()
            thread_errors: list[BaseException] = []
            with source.open(step_guard=step_guard) as stream:
                self.assertEqual(stream.read(), fixture.source)
                self.assertEqual(backend._active_lock_records(), [])
                self.assertEqual(
                    int(getattr(backend._root_lock_coordinator.local, "depth", 0)),
                    0,
                )

                def enter_same_resource_lock() -> None:
                    try:
                        with backend._locked(
                            root / fixture.reservation.snapshot_lock_relpath,
                            "snapshot",
                            {
                                "attempt_id": fixture.checkpoint.attempt_id,
                                "fence_identity": fixture.checkpoint.fence_identity,
                            },
                        ):
                            entered_same_resource_lock.set()
                    except BaseException as exc:  # pragma: no cover - assertion path
                        thread_errors.append(exc)

                thread = threading.Thread(target=enter_same_resource_lock)
                thread.start()
                self.assertTrue(entered_same_resource_lock.wait(timeout=2.0))
                thread.join(timeout=2.0)
                self.assertFalse(thread.is_alive())

            self.assertEqual(thread_errors, [])
            self.assertGreaterEqual(step_guard.calls, 6)
            self.assertGreaterEqual(claim_guard.calls, 6)

    def test_submission_snapshot_source_rejects_path_replacement_on_close(
        self,
    ) -> None:
        fixture = _submission_snapshot_fixture(
            b"%PDF-1.7\npath replacement witness\n%%EOF\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(b"unused"),
                clock=lambda: 1.0,
            )
            snapshot = root / fixture.reservation.snapshot_relpath
            with backend._open_dir(snapshot.parent, create=True):
                pass
            snapshot.write_bytes(fixture.source)
            snapshot.chmod(0o600)
            source = backend.submission_snapshot_source_v4(
                **fixture.source_arguments(claim_guard=_Guard())
            )

            with self.assertRaisesRegex(
                ParserOutputContractError,
                "snapshot is unsafe|path changed",
            ):
                with source.open(step_guard=_StepGuard()) as stream:
                    self.assertEqual(stream.read(), fixture.source)
                    snapshot.unlink()
                    snapshot.write_bytes(fixture.source)
                    snapshot.chmod(0o600)

    def test_submission_snapshot_waiter_never_holds_the_root_gate(self) -> None:
        fixture = _submission_snapshot_fixture(
            b"%PDF-1.7\ncontended pinned snapshot\n%%EOF\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(b"unused"),
                clock=lambda: 1.0,
            )
            snapshot = root / fixture.reservation.snapshot_relpath
            with backend._open_dir(snapshot.parent, create=True):
                pass
            snapshot.write_bytes(fixture.source)
            snapshot.chmod(0o600)
            source = backend.submission_snapshot_source_v4(
                **fixture.source_arguments(claim_guard=_Guard())
            )
            snapshot_lock = root / fixture.reservation.snapshot_lock_relpath
            binding = {
                "attempt_id": fixture.checkpoint.attempt_id,
                "fence_identity": fixture.checkpoint.fence_identity,
            }
            holder_entered = threading.Event()
            release_holder = threading.Event()
            opener_at_flock = threading.Event()
            opener_done = threading.Event()
            unrelated_entered = threading.Event()
            errors: list[BaseException] = []
            real_flock = fcntl.flock
            opener_thread: threading.Thread

            def hold_snapshot_lock() -> None:
                try:
                    with backend._locked(snapshot_lock, "snapshot", binding):
                        holder_entered.set()
                        release_holder.wait(timeout=3.0)
                except BaseException as exc:  # pragma: no cover - assertion path
                    errors.append(exc)

            def open_snapshot() -> None:
                try:
                    with source.open(step_guard=_StepGuard()) as stream:
                        self.assertEqual(stream.read(), fixture.source)
                    opener_done.set()
                except BaseException as exc:  # pragma: no cover - assertion path
                    errors.append(exc)

            def observe_flock(fd: int, operation: int) -> Any:
                if (
                    threading.current_thread() is opener_thread
                    and operation & fcntl.LOCK_EX
                ):
                    opener_at_flock.set()
                return real_flock(fd, operation)

            def enter_unrelated_lock() -> None:
                try:
                    with backend._locked(
                        root / "locks" / "unrelated.lock",
                        "spool",
                        {"resource": "unrelated"},
                    ):
                        unrelated_entered.set()
                except BaseException as exc:  # pragma: no cover - assertion path
                    errors.append(exc)

            holder = threading.Thread(target=hold_snapshot_lock, daemon=True)
            holder.start()
            self.assertTrue(holder_entered.wait(timeout=2.0))
            opener_thread = threading.Thread(target=open_snapshot, daemon=True)
            unrelated = threading.Thread(target=enter_unrelated_lock, daemon=True)
            with mock.patch(
                "disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4.fcntl.flock",
                side_effect=observe_flock,
            ):
                opener_thread.start()
                self.assertTrue(opener_at_flock.wait(timeout=2.0))
                self.assertFalse(opener_done.is_set())
                unrelated.start()
                self.assertTrue(unrelated_entered.wait(timeout=2.0))
                release_holder.set()
                holder.join(timeout=2.0)
                opener_thread.join(timeout=2.0)
                unrelated.join(timeout=2.0)
            self.assertFalse(holder.is_alive())
            self.assertFalse(opener_thread.is_alive())
            self.assertFalse(unrelated.is_alive())
            self.assertTrue(opener_done.is_set())
            self.assertEqual(errors, [])

    def test_submission_snapshot_source_rejects_unsafe_or_partial_source(
        self,
    ) -> None:
        fixture = _submission_snapshot_fixture(
            b"%PDF-1.7\nunsafe source witness\n%%EOF\n"
        )
        for case in ("mode", "part", "claim"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                backend = MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=_Transport(b"unused"),
                    clock=lambda: 1.0,
                )
                snapshot = root / fixture.reservation.snapshot_relpath
                with backend._open_dir(snapshot.parent, create=True):
                    pass
                snapshot.write_bytes(fixture.source)
                snapshot.chmod(0o644 if case == "mode" else 0o600)
                if case == "part":
                    part = root / fixture.reservation.snapshot_part_relpath
                    part.write_bytes(b"partial")
                    part.chmod(0o600)
                claim_guard = _Guard(fail_at=1 if case == "claim" else None)
                source = backend.submission_snapshot_source_v4(
                    **fixture.source_arguments(claim_guard=claim_guard)
                )

                expected = {
                    "mode": "snapshot is unsafe",
                    "part": "coexists with a partial file",
                    "claim": "claim lost",
                }[case]
                with self.assertRaisesRegex(
                    (ParserOutputContractError, RuntimeError),
                    expected,
                ):
                    with source.open(step_guard=_StepGuard()):
                        self.fail("unsafe snapshot source became usable")

    def test_lock_inode_replacement_cannot_enter_a_second_critical_section(
        self,
    ) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            lock = root / fixture.intent.spool_lock_relpath
            binding = backend._resource_binding(fixture.intent)
            entered = False
            with self.assertRaisesRegex(ParserOutputContractError, "lock path"):
                with backend._locked(lock, "spool", binding):
                    exact = lock.read_bytes()
                    lock.unlink()
                    lock.write_bytes(exact)
                    lock.chmod(0o600)
                    with backend._locked(lock, "spool", binding):
                        entered = True
            self.assertFalse(entered)

    def test_active_lock_parent_replacement_blocks_nested_lock(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            spool_lock = root / fixture.intent.spool_lock_relpath
            staging_lock = root / fixture.intent.staging_lock_relpath
            moved_parent = root / "spool-moved-lock-test"
            binding = backend._resource_binding(fixture.intent)
            entered = False
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "scratch directory path was replaced",
            ):
                with backend._locked(spool_lock, "spool", binding):
                    spool_lock.parent.rename(moved_parent)
                    spool_lock.parent.mkdir(mode=0o700)
                    with backend._locked(staging_lock, "staging", binding):
                        entered = True
            self.assertFalse(entered)
            self.assertTrue((moved_parent / spool_lock.name).is_file())
            self.assertFalse(staging_lock.exists())

    def test_resource_locks_serialize_only_the_same_resource(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(b"unused"),
                clock=lambda: 1.0,
            )
            first = root / "locks" / "first.lock"
            second = root / "locks" / "second.lock"
            first_entered = threading.Event()
            release_first = threading.Event()
            same_entered = threading.Event()
            errors: list[BaseException] = []

            def hold_first() -> None:
                try:
                    with backend._locked(first, "spool", {"resource": "first"}):
                        first_entered.set()
                        release_first.wait(timeout=3.0)
                except BaseException as exc:  # pragma: no cover - assertion path
                    errors.append(exc)

            def wait_for_first() -> None:
                try:
                    with backend._locked(first, "spool", {"resource": "first"}):
                        same_entered.set()
                except BaseException as exc:  # pragma: no cover - assertion path
                    errors.append(exc)

            holder = threading.Thread(target=hold_first)
            waiter = threading.Thread(target=wait_for_first)
            holder.start()
            self.assertTrue(first_entered.wait(timeout=2.0))
            waiter.start()
            self.assertFalse(same_entered.wait(timeout=0.1))

            # A different exact resource must not wait behind the global root gate.
            with backend._locked(second, "spool", {"resource": "second"}):
                self.assertFalse(same_entered.is_set())

            release_first.set()
            holder.join(timeout=2.0)
            waiter.join(timeout=2.0)
            self.assertFalse(holder.is_alive())
            self.assertFalse(waiter.is_alive())
            self.assertTrue(same_entered.is_set())
            self.assertEqual(errors, [])

    def test_promotion_rejects_same_bytes_new_inode_after_exact_fsync(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            changed = False

            def replace_after_fsync(phase: str) -> None:
                nonlocal changed
                if phase != "after_staging_fsync" or changed:
                    return
                changed = True
                staging = root / fixture.intent.staging_relpath
                reserved = {
                    fixture.intent.provider_envelope_relpath,
                    fixture.intent.output_manifest_relpath,
                    Path(fixture.intent.staging_marker_relpath).name,
                }
                victim = next(
                    path
                    for path in staging.rglob("*")
                    if path.is_file()
                    and path.relative_to(staging).as_posix() not in reserved
                )
                exact = victim.read_bytes()
                victim.unlink()
                victim.write_bytes(exact)
                victim.chmod(0o600)

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
                fault_hook=replace_after_fsync,
            )
            with self.assertRaisesRegex(ParserOutputContractError, "changed"):
                backend.materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )
            self.assertFalse((root / fixture.intent.output_relpath).exists())

    def test_invalid_promoted_marker_tree_is_rebuilt_from_retained_spool(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _Transport(archive)
            damaged = False

            def damage_promoted_tree(phase: str) -> None:
                nonlocal damaged
                if phase != "after_promotion_rename" or damaged:
                    return
                damaged = True
                output = root / fixture.intent.output_relpath
                reserved = {
                    fixture.intent.provider_envelope_relpath,
                    fixture.intent.output_manifest_relpath,
                    Path(fixture.intent.staging_marker_relpath).name,
                }
                victim = next(
                    path
                    for path in output.rglob("*")
                    if path.is_file()
                    and path.relative_to(output).as_posix() not in reserved
                )
                exact = victim.read_bytes()
                victim.write_bytes(exact[:-1] + bytes([exact[-1] ^ 1]))
                victim.chmod(0o600)

            with self.assertRaises(ParserOutputContractError):
                MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 1.0,
                    fault_hook=damage_promoted_tree,
                ).materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )
            output = root / fixture.intent.output_relpath
            marker = output / Path(fixture.intent.staging_marker_relpath).name
            self.assertTrue(output.is_dir())
            self.assertTrue(marker.is_file())
            recovered = MinerUHttpStagedV4(
                scratch_root=root,
                transport=transport,
                clock=lambda: 2.0,
            ).materialize_v4(
                **fixture.arguments(),
                claim_guard=_Guard(),
            )
            self.assertEqual(
                recovered.receipt.spool_sha256, fixture.intent.artifact_sha256
            )
            self.assertEqual(transport.downloads, 1)
            self.assertFalse(marker.exists())

    def test_invalid_output_recovery_rename_fault_windows_replay(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        phases = (
            "before_invalid_output_quarantine_rename",
            "after_invalid_output_quarantine_rename",
        )
        for crash_phase in phases:
            with (
                self.subTest(phase=crash_phase),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                transport = _Transport(archive)
                damaged = False

                def damage(phase: str) -> None:
                    nonlocal damaged
                    if phase != "after_promotion_rename" or damaged:
                        return
                    damaged = True
                    output = root / fixture.intent.output_relpath
                    victim = next(
                        path
                        for path in output.rglob("*")
                        if path.is_file()
                        and path.relative_to(output).as_posix()
                        not in {
                            fixture.intent.provider_envelope_relpath,
                            fixture.intent.output_manifest_relpath,
                            Path(fixture.intent.staging_marker_relpath).name,
                        }
                    )
                    exact = victim.read_bytes()
                    victim.write_bytes(exact[:-1] + bytes([exact[-1] ^ 1]))
                    victim.chmod(0o600)

                with self.assertRaises(ParserOutputContractError):
                    MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 1.0,
                        fault_hook=damage,
                    ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())

                def crash(phase: str) -> None:
                    if phase == crash_phase:
                        raise RuntimeError(crash_phase)

                with self.assertRaisesRegex(RuntimeError, crash_phase):
                    MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 2.0,
                        fault_hook=crash,
                    ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                recovered = MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 3.0,
                ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                self.assertEqual(
                    recovered.receipt.spool_sha256, fixture.intent.artifact_sha256
                )
                self.assertEqual(transport.downloads, 1)

    def test_invalid_output_recovery_fails_closed_on_staging_race_and_claim_loss(
        self,
    ) -> None:
        archive = _official_zip()
        for mode in ("staging-race", "claim-loss"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = _materialize_fixture(archive)
                transport = _Transport(archive)
                damaged = False

                def damage(phase: str) -> None:
                    nonlocal damaged
                    if phase != "after_promotion_rename" or damaged:
                        return
                    damaged = True
                    output = root / fixture.intent.output_relpath
                    victim = next(
                        path
                        for path in output.rglob("*")
                        if path.is_file()
                        and path.relative_to(output).as_posix()
                        not in {
                            fixture.intent.provider_envelope_relpath,
                            fixture.intent.output_manifest_relpath,
                            Path(fixture.intent.staging_marker_relpath).name,
                        }
                    )
                    exact = victim.read_bytes()
                    victim.write_bytes(exact[:-1] + bytes([exact[-1] ^ 1]))
                    victim.chmod(0o600)

                with self.assertRaises(ParserOutputContractError):
                    MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 1.0,
                        fault_hook=damage,
                    ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                output = root / fixture.intent.output_relpath
                quarantine = _quarantine_path(root, fixture)
                if mode == "staging-race":

                    def create_quarantine(phase: str) -> None:
                        if phase != "before_invalid_output_quarantine_rename":
                            return
                        quarantine.mkdir(mode=0o700)
                        quarantine.chmod(0o700)

                    backend = MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 2.0,
                        fault_hook=create_quarantine,
                    )
                    with self.assertRaises(ParserOutputContractError):
                        backend.materialize_v4(
                            **fixture.arguments(), claim_guard=_Guard()
                        )
                    self.assertTrue(quarantine.is_dir())
                else:
                    backend = MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 2.0,
                    )
                    with self.assertRaisesRegex(RuntimeError, "claim lost"):
                        backend.materialize_v4(
                            **fixture.arguments(), claim_guard=_Guard(fail_at=3)
                        )
                    self.assertFalse(quarantine.exists())
                self.assertTrue(output.is_dir())

    def test_invalid_output_recovery_refuses_noncanonical_marker_in_place(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            damaged = False

            def damage(phase: str) -> None:
                nonlocal damaged
                if phase != "after_promotion_rename" or damaged:
                    return
                damaged = True
                output = root / fixture.intent.output_relpath
                reserved = {
                    fixture.intent.provider_envelope_relpath,
                    fixture.intent.output_manifest_relpath,
                    Path(fixture.intent.staging_marker_relpath).name,
                }
                victim = next(
                    path
                    for path in output.rglob("*")
                    if path.is_file()
                    and path.relative_to(output).as_posix() not in reserved
                )
                exact = victim.read_bytes()
                victim.write_bytes(exact[:-1] + bytes([exact[-1] ^ 1]))
                victim.chmod(0o600)

            with self.assertRaises(ParserOutputContractError):
                MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=_Transport(archive),
                    clock=lambda: 1.0,
                    fault_hook=damage,
                ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
            output = root / fixture.intent.output_relpath
            staging = root / fixture.intent.staging_relpath
            marker = output / Path(fixture.intent.staging_marker_relpath).name
            marker.write_bytes(b"foreign-marker")
            marker.chmod(0o600)

            with self.assertRaisesRegex(
                ParserOutputContractError,
                "promoted materialization marker drifted",
            ):
                MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=_Transport(archive),
                    clock=lambda: 2.0,
                ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
            self.assertTrue(output.is_dir())
            self.assertFalse(staging.exists())
            self.assertEqual(marker.read_bytes(), b"foreign-marker")

    def test_invalid_output_recovery_pins_full_tree_across_rename(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            damaged = False

            def damage(phase: str) -> None:
                nonlocal damaged
                if phase != "after_promotion_rename" or damaged:
                    return
                damaged = True
                output = root / fixture.intent.output_relpath
                reserved = {
                    fixture.intent.provider_envelope_relpath,
                    fixture.intent.output_manifest_relpath,
                    Path(fixture.intent.staging_marker_relpath).name,
                }
                victim = next(
                    path
                    for path in output.rglob("*")
                    if path.is_file()
                    and path.relative_to(output).as_posix() not in reserved
                )
                exact = victim.read_bytes()
                victim.write_bytes(exact[:-1] + bytes([exact[-1] ^ 1]))
                victim.chmod(0o600)

            with self.assertRaises(ParserOutputContractError):
                MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=_Transport(archive),
                    clock=lambda: 1.0,
                    fault_hook=damage,
                ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
            output = root / fixture.intent.output_relpath
            staging = root / fixture.intent.staging_relpath
            injected = output / "same-uid-injected.bin"

            def inject_after_final_admission(phase: str) -> None:
                if phase != "before_invalid_output_quarantine_rename":
                    return
                injected.write_bytes(b"foreign")
                injected.chmod(0o600)

            recovered = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 2.0,
                fault_hook=inject_after_final_admission,
            ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
            quarantine = _quarantine_path(root, fixture)
            self.assertTrue(output.is_dir())
            self.assertFalse(staging.exists())
            self.assertEqual((quarantine / injected.name).read_bytes(), b"foreign")
            self.assertEqual(
                recovered.receipt.spool_sha256,
                fixture.intent.artifact_sha256,
            )

    def test_invalid_output_recovery_rejects_late_staging_injection(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _Transport(archive)

            damaged = False

            def damage(phase: str) -> None:
                nonlocal damaged
                if phase != "after_promotion_rename" or damaged:
                    return
                damaged = True
                output = root / fixture.intent.output_relpath
                reserved = {
                    fixture.intent.provider_envelope_relpath,
                    fixture.intent.output_manifest_relpath,
                    Path(fixture.intent.staging_marker_relpath).name,
                }
                victim = next(
                    path
                    for path in output.rglob("*")
                    if path.is_file()
                    and path.relative_to(output).as_posix() not in reserved
                )
                exact = victim.read_bytes()
                victim.write_bytes(exact[:-1] + bytes([exact[-1] ^ 1]))
                victim.chmod(0o600)

            with self.assertRaises(ParserOutputContractError):
                MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 1.0,
                    fault_hook=damage,
                ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
            output = root / fixture.intent.output_relpath
            injected = output / "late-same-uid-injected.bin"

            def inject_after_recovery_rename(phase: str) -> None:
                if phase != "before_invalid_output_quarantine_rename":
                    return
                injected.write_bytes(b"foreign")
                injected.chmod(0o600)

            recovered = MinerUHttpStagedV4(
                scratch_root=root,
                transport=transport,
                clock=lambda: 2.0,
                fault_hook=inject_after_recovery_rename,
            ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
            quarantine = _quarantine_path(root, fixture)
            self.assertEqual(
                recovered.receipt.spool_sha256, fixture.intent.artifact_sha256
            )
            self.assertEqual(
                (quarantine / injected.relative_to(output)).read_bytes(),
                b"foreign",
            )
            self.assertEqual(transport.downloads, 1)

    def test_invalid_output_recovery_partial_exact_cleanup_replays(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _Transport(archive)

            def crash_after_seal(phase: str) -> None:
                if phase == "after_staging_fsync":
                    raise RuntimeError("sealed crash")

            with self.assertRaisesRegex(RuntimeError, "sealed crash"):
                MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 1.0,
                    fault_hook=crash_after_seal,
                ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
            staging = root / fixture.intent.staging_relpath

            class LoseClaimAfterNestedFiles(_Guard):
                def assert_current_under_resource_lock(
                    self,
                    *,
                    checkpoint: RemoteParseCheckpointV4,
                    claim: V4ClaimWitness,
                ) -> None:
                    super().assert_current_under_resource_lock(
                        checkpoint=checkpoint,
                        claim=claim,
                    )
                    images = staging / "images"
                    if images.is_dir() and not any(images.iterdir()):
                        raise RuntimeError("claim lost")

            with self.assertRaisesRegex(RuntimeError, "claim lost"):
                MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 2.0,
                ).materialize_v4(
                    **fixture.arguments(),
                    claim_guard=LoseClaimAfterNestedFiles(),
                )
            self.assertTrue(staging.is_dir())
            self.assertTrue((staging / "images").is_dir())
            self.assertFalse(any((staging / "images").iterdir()))
            self.assertTrue(
                (staging / fixture.intent.output_manifest_relpath).is_file()
            )
            self.assertTrue(
                (staging / Path(fixture.intent.staging_marker_relpath).name).is_file()
            )

            recovered = MinerUHttpStagedV4(
                scratch_root=root,
                transport=transport,
                clock=lambda: 3.0,
            ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
            self.assertEqual(
                recovered.receipt.spool_sha256, fixture.intent.artifact_sha256
            )
            self.assertEqual(transport.downloads, 1)

    def test_marker_only_recovery_restart_is_exact_and_never_recursive(self) -> None:
        archive = _official_zip()
        for polluted in (False, True):
            with (
                self.subTest(polluted=polluted),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                fixture = _materialize_fixture(archive)
                transport = _Transport(archive)

                def crash_after_seal(phase: str) -> None:
                    if phase == "after_staging_fsync":
                        raise RuntimeError("sealed crash")

                with self.assertRaisesRegex(RuntimeError, "sealed crash"):
                    MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 1.0,
                        fault_hook=crash_after_seal,
                    ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                staging = root / fixture.intent.staging_relpath
                manifest = staging / fixture.intent.output_manifest_relpath
                marker = staging / Path(fixture.intent.staging_marker_relpath).name

                class LoseClaimBeforeMarker(_Guard):
                    def assert_current_under_resource_lock(
                        self,
                        *,
                        checkpoint: RemoteParseCheckpointV4,
                        claim: V4ClaimWitness,
                    ) -> None:
                        super().assert_current_under_resource_lock(
                            checkpoint=checkpoint,
                            claim=claim,
                        )
                        if marker.is_file() and not manifest.exists():
                            raise RuntimeError("claim lost")

                with self.assertRaisesRegex(RuntimeError, "claim lost"):
                    MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 2.0,
                    ).materialize_v4(
                        **fixture.arguments(),
                        claim_guard=LoseClaimBeforeMarker(),
                    )
                self.assertTrue(staging.is_dir())
                self.assertFalse(manifest.exists())
                self.assertTrue(marker.is_file())
                self.assertEqual(
                    {
                        path.relative_to(staging).as_posix()
                        for path in staging.rglob("*")
                    },
                    {marker.name},
                )

                if polluted:
                    foreign = staging / "late-foreign.bin"
                    foreign.write_bytes(b"foreign")
                    foreign.chmod(0o600)
                    recovered = MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 3.0,
                    ).materialize_v4(
                        **fixture.arguments(),
                        claim_guard=_Guard(),
                    )
                    quarantine = _quarantine_path(root, fixture)
                    self.assertEqual(
                        (quarantine / foreign.relative_to(staging)).read_bytes(),
                        b"foreign",
                    )
                    self.assertEqual(
                        recovered.receipt.spool_sha256,
                        fixture.intent.artifact_sha256,
                    )
                else:
                    recovered = MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 3.0,
                    ).materialize_v4(
                        **fixture.arguments(),
                        claim_guard=_Guard(),
                    )
                    self.assertEqual(
                        recovered.receipt.spool_sha256,
                        fixture.intent.artifact_sha256,
                    )
                    self.assertFalse(staging.exists())
                self.assertEqual(transport.downloads, 1)

    def test_marker_only_recovery_rechecks_marker_inside_pinned_admission(
        self,
    ) -> None:
        archive = _official_zip()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _materialize_fixture(archive)
            transport = _Transport(archive)
            backend = _leave_staging_after_mkdir_crash(
                root=root,
                fixture=fixture,
                transport=transport,
            )
            staging = root / fixture.intent.staging_relpath
            marker = staging / Path(fixture.intent.staging_marker_relpath).name
            marker.write_bytes(backend._marker_bytes(fixture.intent))
            marker.chmod(0o600)

            def replace_after_path_read(phase: str) -> None:
                if phase != "before_marker_only_recovery_admission":
                    return
                marker.unlink()
                marker.write_bytes(b"foreign-marker")
                marker.chmod(0o600)

            with self.assertRaisesRegex(
                ParserOutputContractError,
                "marker drifted",
            ):
                MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 2.0,
                    fault_hook=replace_after_path_read,
                ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
            self.assertEqual(marker.read_bytes(), b"foreign-marker")
            self.assertTrue(staging.is_dir())
            self.assertFalse(_quarantine_path(root, fixture).exists())
            self.assertEqual(transport.downloads, 1)

    def test_materialize_recovers_after_promoted_marker_and_replays_exactly(
        self,
    ) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _Transport(archive)

            def crash(phase: str) -> None:
                if phase == "after_promotion":
                    raise RuntimeError("crash after promotion")

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=transport,
                clock=lambda: 1.0,
                fault_hook=crash,
            )
            with self.assertRaisesRegex(RuntimeError, "after promotion"):
                backend.materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )
            output = root / fixture.intent.output_relpath
            marker = output / Path(fixture.intent.staging_marker_relpath).name
            self.assertTrue(marker.is_file())
            self.assertTrue((root / fixture.intent.spool_relpath).is_file())

            restarted = MinerUHttpStagedV4(
                scratch_root=root,
                transport=transport,
                clock=lambda: 2.0,
            )
            guard = _Guard()
            first = restarted.materialize_v4(
                **fixture.arguments(),
                claim_guard=guard,
            )
            second = restarted.materialize_v4(
                **fixture.arguments(),
                claim_guard=guard,
            )
            self.assertEqual(first, second)
            self.assertFalse(marker.exists())
            self.assertFalse((root / fixture.intent.staging_relpath).exists())
            self.assertFalse((root / fixture.intent.spool_part_relpath).exists())
            self.assertFalse((root / fixture.intent.spool_part_owner_relpath).exists())
            self.assertTrue((root / fixture.intent.spool_lock_relpath).is_file())
            self.assertTrue((root / fixture.intent.staging_lock_relpath).is_file())
            self.assertEqual(transport.downloads, 1)
            self.assertGreaterEqual(guard.calls, 4)

    def test_stream_oversize_leaves_exact_residue_then_restarts(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oversized = _Transport(archive + b"x")
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=oversized,
                clock=lambda: 1.0,
            )
            with self.assertRaisesRegex(ParserOutputContractError, "byte limit"):
                backend.materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )
            self.assertTrue((root / fixture.intent.spool_part_relpath).is_file())
            self.assertTrue((root / fixture.intent.spool_part_owner_relpath).is_file())
            self.assertEqual(oversized.stream_closes, 1)

            exact = _Transport(archive)
            recovered = MinerUHttpStagedV4(
                scratch_root=root,
                transport=exact,
                clock=lambda: 2.0,
            ).materialize_v4(
                **fixture.arguments(),
                claim_guard=_Guard(),
            )
            self.assertEqual(
                recovered.receipt.spool_sha256, fixture.intent.artifact_sha256
            )
            self.assertEqual(exact.downloads, 1)

    def test_replay_does_not_treat_dangling_residue_symlink_as_absent(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            backend.materialize_v4(
                **fixture.arguments(),
                claim_guard=_Guard(),
            )
            part = root / fixture.intent.spool_part_relpath
            part.symlink_to(root / "does-not-exist")
            with self.assertRaises((ParserOutputContractError, ValueError)):
                backend.materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )

    def test_rejects_symlink_zip_member_without_promotion(self) -> None:
        archive = _symlink_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            with self.assertRaisesRegex(ParserOutputContractError, "non-regular"):
                backend.materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )
            self.assertFalse((root / fixture.intent.output_relpath).exists())
            self.assertTrue((root / fixture.intent.spool_relpath).is_file())

    def test_rejects_nonportable_duplicate_and_ancestor_zip_paths(self) -> None:
        archives = (
            _zip_entries((("A.txt", b"a"), ("a.txt", b"b"))),
            _zip_entries((("parent", b"a"), ("parent/child", b"b"))),
            _zip_entries((("cafe\N{COMBINING ACUTE ACCENT}.txt", b"a"),)),
        )
        for archive in archives:
            with self.subTest(sha256=hashlib.sha256(archive).hexdigest()):
                fixture = _materialize_fixture(archive)
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    backend = MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=_Transport(archive),
                        clock=lambda: 1.0,
                    )
                    with self.assertRaises((ValueError, ParserOutputContractError)):
                        backend.materialize_v4(
                            **fixture.arguments(),
                            claim_guard=_Guard(),
                        )
                    self.assertFalse((root / fixture.intent.output_relpath).exists())

    def test_restart_after_spool_and_staging_fsync_faults(self) -> None:
        archive = _official_zip()
        for phase in (
            "after_spool_fsync",
            "after_spool_rename",
            "after_staging_mkdir",
            "after_staging_fsync",
            "after_promotion_rename",
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = _materialize_fixture(archive)

                def crash(observed: str) -> None:
                    if observed == phase:
                        raise RuntimeError(phase)

                transport = _Transport(archive)
                with self.assertRaisesRegex(RuntimeError, phase):
                    MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 1.0,
                        fault_hook=crash,
                    ).materialize_v4(
                        **fixture.arguments(),
                        claim_guard=_Guard(),
                    )
                replayed = MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 2.0,
                ).materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )
                self.assertEqual(
                    replayed.receipt.spool_sha256, fixture.intent.artifact_sha256
                )
                self.assertEqual(
                    transport.downloads,
                    2 if phase == "after_spool_fsync" else 1,
                )

    def test_markerless_empty_staging_restart_and_late_injection_fail_closed(
        self,
    ) -> None:
        archive = _official_zip()
        for injected in (False, True):
            with (
                self.subTest(injected=injected),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                fixture = _materialize_fixture(archive)
                transport = _Transport(archive)
                _leave_staging_after_mkdir_crash(
                    root=root,
                    fixture=fixture,
                    transport=transport,
                )
                staging = root / fixture.intent.staging_relpath
                self.assertTrue(staging.is_dir())
                self.assertFalse(any(staging.iterdir()))
                foreign = staging / "late-same-uid.bin"

                def inject_before_rmdir(phase: str) -> None:
                    if phase != "before_empty_staging_rmdir" or not injected:
                        return
                    foreign.write_bytes(b"foreign")
                    foreign.chmod(0o600)

                backend = MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 2.0,
                    fault_hook=inject_before_rmdir,
                )
                if injected:
                    with self.assertRaisesRegex(
                        ParserOutputContractError,
                        "contains foreign entries|changed before deletion",
                    ):
                        backend.materialize_v4(
                            **fixture.arguments(), claim_guard=_Guard()
                        )
                    self.assertEqual(foreign.read_bytes(), b"foreign")
                    with self.assertRaisesRegex(
                        ParserOutputContractError,
                        "markerless staging is not exactly empty",
                    ):
                        MinerUHttpStagedV4(
                            scratch_root=root,
                            transport=transport,
                            clock=lambda: 3.0,
                        ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                    self.assertEqual(foreign.read_bytes(), b"foreign")
                else:
                    value = backend.materialize_v4(
                        **fixture.arguments(), claim_guard=_Guard()
                    )
                    self.assertEqual(
                        value.receipt.spool_sha256,
                        fixture.intent.artifact_sha256,
                    )
                    self.assertFalse(staging.exists())
                self.assertEqual(transport.downloads, 1)

    def test_partial_unpack_and_torn_manifest_are_quarantined(self) -> None:
        archive = _official_zip()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _materialize_fixture(archive)
            transport = _Transport(archive)
            backend = _leave_staging_after_mkdir_crash(
                root=root,
                fixture=fixture,
                transport=transport,
            )
            staging = root / fixture.intent.staging_relpath
            marker = staging / Path(fixture.intent.staging_marker_relpath).name
            marker.write_bytes(backend._marker_bytes(fixture.intent))
            marker.chmod(0o600)
            partial = staging / ".unpack" / "partial.bin"
            partial.parent.mkdir(mode=0o700)
            partial.write_bytes(b"partial-unpack")
            partial.chmod(0o600)

            value = MinerUHttpStagedV4(
                scratch_root=root,
                transport=transport,
                clock=lambda: 2.0,
            ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
            quarantine = _quarantine_path(root, fixture)
            self.assertEqual(
                (quarantine / partial.relative_to(staging)).read_bytes(),
                b"partial-unpack",
            )
            self.assertEqual(value.receipt.spool_sha256, fixture.intent.artifact_sha256)
            self.assertEqual(transport.downloads, 1)

    def test_mutated_declared_file_and_noncleanup_subset_are_quarantined(self) -> None:
        archive = _official_zip()
        for mode in ("mutated", "missing-middle"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = _materialize_fixture(archive)
                transport = _Transport(archive)

                def crash_after_seal(phase: str) -> None:
                    if phase == "after_staging_fsync":
                        raise RuntimeError("sealed crash")

                with self.assertRaisesRegex(RuntimeError, "sealed crash"):
                    MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 1.0,
                        fault_hook=crash_after_seal,
                    ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                staging = root / fixture.intent.staging_relpath
                reserved = {
                    fixture.intent.provider_envelope_relpath,
                    fixture.intent.output_manifest_relpath,
                    Path(fixture.intent.staging_marker_relpath).name,
                }
                payload = sorted(
                    (
                        path
                        for path in staging.rglob("*")
                        if path.is_file()
                        and path.relative_to(staging).as_posix() not in reserved
                    ),
                    key=lambda path: path.relative_to(staging).as_posix(),
                )
                victim = payload[1]
                relpath = victim.relative_to(staging)
                if mode == "mutated":
                    exact = victim.read_bytes()
                    victim.write_bytes(exact[:-1] + bytes([exact[-1] ^ 1]))
                    victim.chmod(0o600)
                    expected = victim.read_bytes()
                else:
                    expected = b""
                    victim.unlink()

                value = MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 2.0,
                ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                quarantine = _quarantine_path(root, fixture)
                retained = quarantine / relpath
                if mode == "mutated":
                    self.assertEqual(retained.read_bytes(), expected)
                else:
                    self.assertFalse(retained.exists())
                    self.assertTrue(
                        (quarantine / payload[0].relative_to(staging)).is_file()
                    )
                self.assertEqual(
                    value.receipt.spool_sha256,
                    fixture.intent.artifact_sha256,
                )
                self.assertEqual(transport.downloads, 1)

    def test_deep_zip_member_is_rejected_before_directory_creation(self) -> None:
        deep_name = "/".join((*(("d",) * 33), "value.bin"))
        archive = _zip_entries(((deep_name, b"value"),))
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _Transport(archive)
            failures: list[str] = []
            for _ in range(4):
                with self.assertRaises(ParserOutputContractError) as caught:
                    MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 1.0,
                    ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                failures.append(str(caught.exception))
                self.assertFalse((root / fixture.intent.staging_relpath).exists())
                self.assertFalse(_quarantine_path(root, fixture).exists())
                self.assertFalse((root / fixture.intent.output_relpath).exists())
            self.assertEqual(failures, [failures[0]] * 4)
            self.assertIn("exceeds recovery depth", failures[0])
            self.assertEqual(transport.downloads, 1)
            self.assertTrue((root / fixture.intent.spool_lock_relpath).is_file())
            self.assertFalse((root / fixture.intent.staging_lock_relpath).exists())

    def test_invalid_provider_json_replays_without_staging_or_quarantine(self) -> None:
        archive = _replace_zip_suffix(
            _official_zip(),
            "_content_list_v2.json",
            b"{not-json",
        )
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _Transport(archive)
            failures: list[str] = []
            for _ in range(4):
                with self.assertRaises(ParserOutputContractError) as caught:
                    MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 1.0,
                    ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                failures.append(str(caught.exception))
                self.assertFalse((root / fixture.intent.staging_relpath).exists())
                self.assertFalse(_quarantine_path(root, fixture).exists())
                self.assertFalse((root / fixture.intent.output_relpath).exists())
                self.assertTrue((root / fixture.intent.spool_relpath).is_file())
            self.assertEqual(failures, [failures[0]] * 4)
            self.assertEqual(transport.downloads, 1)

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=transport,
                clock=lambda: 1.0,
            )
            cleanup, failure = _no_receipt_local_failure_cleanup_arguments(
                fixture=fixture
            )
            cleanup_receipt = backend.cleanup_v4(**cleanup)
            self.assertEqual(backend.cleanup_v4(**cleanup), cleanup_receipt)
            self.assertFalse((root / fixture.intent.spool_relpath).exists())

            transport.response = ProviderAckTransportResponseV4(
                404,
                _canonical({"detail": "Task not found"}),
            )
            acknowledged = _ack_no_receipt_local_failure(
                backend=backend,
                fixture=fixture,
                cleanup=cleanup,
                failure=failure,
                cleanup_receipt=cleanup_receipt,
            )
            self.assertEqual(acknowledged.ack_kind, "absent")
            self.assertEqual(transport.acks, 1)

    def test_provider_invalid_variants_replay_cleanup_and_ack_absent(self) -> None:
        official = _official_zip()
        with zipfile.ZipFile(io.BytesIO(official), "r") as archive_reader:
            middle_info = next(
                info
                for info in archive_reader.infolist()
                if info.filename.endswith("_middle.json")
            )
            middle = json.loads(archive_reader.read(middle_info))
        middle["pdf_info"][1]["page_idx"] = 0
        invalid_middle = _replace_zip_suffix(
            official,
            "_middle.json",
            _canonical(middle),
        )
        cases = (
            ("middle-identity", invalid_middle, None),
            (
                "missing-content-list",
                _drop_zip_suffix(official, "_content_list.json"),
                None,
            ),
            ("source-page-count", official, 3),
        )
        for label, archive, source_page_count in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = _materialize_fixture(
                    archive,
                    source_page_count=source_page_count,
                )
                transport = _Transport(archive)
                failures: list[str] = []
                for _ in range(4):
                    with self.assertRaises(ParserOutputContractError) as caught:
                        MinerUHttpStagedV4(
                            scratch_root=root,
                            transport=transport,
                            clock=lambda: 1.0,
                        ).materialize_v4(
                            **fixture.arguments(),
                            claim_guard=_Guard(),
                        )
                    failures.append(str(caught.exception))
                    self.assertFalse((root / fixture.intent.staging_relpath).exists())
                    self.assertFalse(_quarantine_path(root, fixture).exists())
                    self.assertFalse((root / fixture.intent.output_relpath).exists())
                    self.assertTrue((root / fixture.intent.spool_relpath).is_file())
                self.assertEqual(failures, [failures[0]] * 4)
                self.assertEqual(transport.downloads, 1)

                backend = MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 2.0,
                )
                cleanup, failure = _no_receipt_local_failure_cleanup_arguments(
                    fixture=fixture
                )
                cleanup_receipt = backend.cleanup_v4(**cleanup)
                self.assertEqual(backend.cleanup_v4(**cleanup), cleanup_receipt)
                self.assertTrue(
                    all(
                        item.disposition == "absent" for item in cleanup_receipt.results
                    )
                )
                transport.response = ProviderAckTransportResponseV4(
                    404,
                    _canonical({"detail": "Task not found"}),
                )
                acknowledged = _ack_no_receipt_local_failure(
                    backend=backend,
                    fixture=fixture,
                    cleanup=cleanup,
                    failure=failure,
                    cleanup_receipt=cleanup_receipt,
                )
                self.assertEqual(acknowledged.ack_kind, "absent")

    def test_user_wedge_sequence_quarantines_then_self_cleans_and_acks(self) -> None:
        archive = _replace_zip_suffix(
            _official_zip(),
            "_content_list_v2.json",
            b"{not-json",
        )
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _Transport(archive)

            def crash_after_extract(phase: str) -> None:
                if phase == "after_zip_extract":
                    raise RuntimeError("simulated process death")

            with self.assertRaisesRegex(RuntimeError, "simulated process death"):
                MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 1.0,
                    fault_hook=crash_after_extract,
                ).materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )
            self.assertTrue((root / fixture.intent.staging_relpath).is_dir())

            failures: list[str] = []
            for _ in range(2):
                with self.assertRaises(ParserOutputContractError) as caught:
                    MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 2.0,
                    ).materialize_v4(
                        **fixture.arguments(),
                        claim_guard=_Guard(),
                    )
                failures.append(str(caught.exception))
                self.assertFalse((root / fixture.intent.staging_relpath).exists())
                self.assertTrue(_quarantine_path(root, fixture).is_dir())
            self.assertEqual(failures, [failures[0], failures[0]])
            self.assertEqual(transport.downloads, 1)
            quarantine = _quarantine_path(root, fixture)
            retained = {
                path.relative_to(quarantine).as_posix(): path.read_bytes()
                for path in quarantine.rglob("*")
                if path.is_file()
            }

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=transport,
                clock=lambda: 3.0,
            )
            cleanup, failure = _no_receipt_local_failure_cleanup_arguments(
                fixture=fixture
            )
            cleanup_receipt = backend.cleanup_v4(**cleanup)
            self.assertEqual(backend.cleanup_v4(**cleanup), cleanup_receipt)
            transport.response = ProviderAckTransportResponseV4(
                404,
                _canonical({"detail": "Task not found"}),
            )
            acknowledged = _ack_no_receipt_local_failure(
                backend=backend,
                fixture=fixture,
                cleanup=cleanup,
                failure=failure,
                cleanup_receipt=cleanup_receipt,
            )
            self.assertEqual(acknowledged.ack_kind, "absent")
            self.assertEqual(
                {
                    path.relative_to(quarantine).as_posix(): path.read_bytes()
                    for path in quarantine.rglob("*")
                    if path.is_file()
                },
                retained,
            )

    def test_flatten_collision_is_quarantined_after_mutation_boundary(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def collide_before_flatten(phase: str) -> None:
                if phase != "before_flatten":
                    return
                collision = root / fixture.intent.staging_relpath / "images"
                collision.write_bytes(b"foreign collision")
                collision.chmod(0o600)

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
                fault_hook=collide_before_flatten,
            )
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "flattened MinerU artifact collision",
            ):
                backend.materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )
            quarantine = _quarantine_path(root, fixture)
            self.assertFalse((root / fixture.intent.staging_relpath).exists())
            self.assertEqual(
                (quarantine / "images").read_bytes(),
                b"foreign collision",
            )

    def test_crash_partial_staging_is_quarantined_by_cleanup_and_replays(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _Transport(archive)

            def crash_after_extract(phase: str) -> None:
                if phase == "after_zip_extract":
                    raise RuntimeError("simulated crash after extract")

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=transport,
                clock=lambda: 1.0,
                fault_hook=crash_after_extract,
            )
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                backend.materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )
            staging = root / fixture.intent.staging_relpath
            self.assertTrue(staging.is_dir())
            self.assertFalse(_quarantine_path(root, fixture).exists())

            cleanup, _failure = _no_receipt_local_failure_cleanup_arguments(
                fixture=fixture
            )
            first = backend.cleanup_v4(**cleanup)
            second = backend.cleanup_v4(**cleanup)
            self.assertEqual(second, first)
            self.assertFalse(staging.exists())
            quarantine = _quarantine_path(root, fixture)
            self.assertTrue(quarantine.is_dir())
            self.assertTrue((quarantine / ".unpack").is_dir())
            staging_result = next(
                item for item in first.results if item.kind == "staging"
            )
            self.assertEqual(staging_result.disposition, "absent")

    def test_cleanup_refuses_second_ambiguous_tree_with_occupied_quarantine(
        self,
    ) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def crash_after_extract(phase: str) -> None:
                if phase == "after_zip_extract":
                    raise RuntimeError("simulated crash after extract")

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
                fault_hook=crash_after_extract,
            )
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                backend.materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )
            cleanup, _failure = _no_receipt_local_failure_cleanup_arguments(
                fixture=fixture
            )
            backend.cleanup_v4(**cleanup)
            quarantine = _quarantine_path(root, fixture)
            retained_before = {
                path.relative_to(quarantine).as_posix(): path.read_bytes()
                for path in quarantine.rglob("*")
                if path.is_file()
            }

            staging = root / fixture.intent.staging_relpath
            staging.mkdir(parents=True, mode=0o700)
            marker = root / fixture.intent.staging_marker_relpath
            marker.write_bytes(backend._marker_bytes(fixture.intent))
            marker.chmod(0o600)
            partial = staging / "second-partial.bin"
            partial.write_bytes(b"second")
            partial.chmod(0o600)
            with self.assertRaisesRegex(
                ParserOutputContractError,
                rf"source={fixture.intent.staging_relpath}.*quarantine=",
            ):
                backend.cleanup_v4(**cleanup)
            self.assertEqual(partial.read_bytes(), b"second")
            self.assertEqual(
                {
                    path.relative_to(quarantine).as_posix(): path.read_bytes()
                    for path in quarantine.rglob("*")
                    if path.is_file()
                },
                retained_before,
            )

    def test_cleanup_refuses_markerless_nonempty_staging_in_place(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            staging = root / fixture.intent.staging_relpath
            backend._ensure_parent(staging)
            backend._mkdir_exact(staging)
            foreign = staging / "foreign.bin"
            foreign.write_bytes(b"foreign")
            foreign.chmod(0o600)
            cleanup, _failure = _no_receipt_local_failure_cleanup_arguments(
                fixture=fixture
            )
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "lacks its canonical marker",
            ):
                backend.cleanup_v4(**cleanup)
            self.assertEqual(foreign.read_bytes(), b"foreign")
            self.assertFalse(_quarantine_path(root, fixture).exists())

    def test_spool_is_pinned_against_replacement_and_same_inode_rewrite(self) -> None:
        archive = _official_zip()
        for mode in ("replace", "rewrite"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = _materialize_fixture(archive)
                original_inode: int | None = None
                mutated_inode: int | None = None

                def mutate_spool(phase: str) -> None:
                    nonlocal original_inode, mutated_inode
                    spool = root / fixture.intent.spool_relpath
                    if mode == "replace" and phase == "after_spool_rename":
                        original_inode = spool.stat().st_ino
                        exact = spool.read_bytes()
                        spool.unlink()
                        spool.write_bytes(exact)
                        spool.chmod(0o600)
                        mutated_inode = spool.stat().st_ino
                    elif mode == "rewrite" and phase == "after_zip_preflight":
                        original_inode = spool.stat().st_ino
                        with spool.open("ab", buffering=0) as stream:
                            stream.write(b"x")
                            stream.flush()
                            os.fsync(stream.fileno())
                        mutated_inode = spool.stat().st_ino

                backend = MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=_Transport(archive),
                    clock=lambda: 1.0,
                    fault_hook=mutate_spool,
                )
                with self.assertRaisesRegex(
                    ParserOutputContractError,
                    "provider result ZIP (identity drifted|changed)",
                ):
                    backend.materialize_v4(
                        **fixture.arguments(),
                        claim_guard=_Guard(),
                    )
                self.assertIsNotNone(original_inode)
                self.assertIsNotNone(mutated_inode)
                if mode == "replace":
                    self.assertNotEqual(mutated_inode, original_inode)
                else:
                    self.assertEqual(mutated_inode, original_inode)
                self.assertFalse((root / fixture.intent.staging_relpath).exists())
                self.assertFalse(_quarantine_path(root, fixture).exists())

    def test_spool_parent_replacement_after_preflight_cannot_publish(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _Transport(archive)
            moved_parent = root / "spool-moved-after-preflight"
            moved = False

            def replace_spool_parent(phase: str) -> None:
                nonlocal moved
                if phase != "after_zip_preflight" or moved:
                    return
                moved = True
                spool_parent = (root / fixture.intent.spool_relpath).parent
                spool_parent.rename(moved_parent)
                spool_parent.mkdir(mode=0o700)

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=transport,
                clock=lambda: 1.0,
                fault_hook=replace_spool_parent,
            )
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "scratch directory path was replaced",
            ):
                backend.materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )
            self.assertTrue(moved)
            moved_spool = moved_parent / Path(fixture.intent.spool_relpath).name
            moved_lock = moved_parent / Path(fixture.intent.spool_lock_relpath).name
            self.assertEqual(moved_spool.read_bytes(), archive)
            self.assertTrue(moved_lock.is_file())
            self.assertFalse((root / fixture.intent.spool_relpath).exists())
            self.assertFalse((root / fixture.intent.staging_lock_relpath).exists())
            self.assertFalse((root / fixture.intent.staging_relpath).exists())
            self.assertFalse((root / fixture.intent.output_relpath).exists())
            self.assertFalse(_quarantine_path(root, fixture).exists())
            self.assertEqual(transport.downloads, 1)

    def test_spool_hash_rejects_same_inode_append_before_stable_snapshot(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _Transport(archive)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=transport,
                clock=lambda: 1.0,
            )
            original_hash_fd = MinerUHttpStagedV4._hash_fd
            appended = False

            def hash_then_append(fd: int) -> tuple[str, int]:
                nonlocal appended
                result = original_hash_fd(fd)
                if not appended:
                    appended = True
                    spool = root / fixture.intent.spool_relpath
                    append_fd = os.open(
                        spool,
                        os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                    )
                    try:
                        os.write(append_fd, b"x")
                        os.fsync(append_fd)
                    finally:
                        os.close(append_fd)
                return result

            with mock.patch.object(
                MinerUHttpStagedV4,
                "_hash_fd",
                side_effect=hash_then_append,
            ):
                with self.assertRaisesRegex(
                    ParserOutputContractError,
                    "provider result ZIP changed while hashing",
                ):
                    backend.materialize_v4(
                        **fixture.arguments(),
                        claim_guard=_Guard(),
                    )
            self.assertTrue(appended)
            self.assertEqual(
                (root / fixture.intent.spool_relpath).read_bytes(),
                archive + b"x",
            )
            self.assertFalse((root / fixture.intent.staging_lock_relpath).exists())
            self.assertFalse((root / fixture.intent.staging_relpath).exists())
            self.assertFalse((root / fixture.intent.output_relpath).exists())
            self.assertEqual(transport.downloads, 1)

    def test_spool_session_hashes_once_for_download_and_existing_replay(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _Transport(archive)
            hash_calls = 0
            original_hash_fd = MinerUHttpStagedV4._hash_fd

            def count_hash(fd: int) -> tuple[str, int]:
                nonlocal hash_calls
                hash_calls += 1
                return original_hash_fd(fd)

            with mock.patch.object(
                MinerUHttpStagedV4,
                "_hash_fd",
                staticmethod(count_hash),
            ):
                first_backend = MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 1.0,
                )
                with mock.patch.object(
                    first_backend,
                    "_validate_zip_metadata",
                    wraps=first_backend._validate_zip_metadata,
                ) as first_validation:
                    first = first_backend.materialize_v4(
                        **fixture.arguments(),
                        claim_guard=_Guard(),
                    )
                self.assertEqual(hash_calls, 1)
                self.assertEqual(first_validation.call_count, 1)

                def forbid_extract(phase: str) -> None:
                    if phase == "after_zip_extract":
                        raise AssertionError("output replay unexpectedly extracted ZIP")

                replay_backend = MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 2.0,
                    fault_hook=forbid_extract,
                )
                with mock.patch.object(
                    replay_backend,
                    "_validate_zip_metadata",
                    wraps=replay_backend._validate_zip_metadata,
                ) as replay_validation:
                    replayed = replay_backend.materialize_v4(
                        **fixture.arguments(),
                        claim_guard=_Guard(),
                    )
                self.assertEqual(replay_validation.call_count, 1)
            self.assertEqual(replayed, first)
            self.assertEqual(hash_calls, 2)
            self.assertEqual(transport.downloads, 1)

    def test_zip_collision_index_is_linear_and_preserves_ancestor_semantics(
        self,
    ) -> None:
        for member_count in (8_000, 16_000):
            with self.subTest(member_count=member_count):
                index = _ZipPathCollisionIndex.empty()
                for number in range(member_count):
                    self.assertTrue(
                        index.admit((f"member-{number:05d}",), is_directory=False)
                    )
                self.assertEqual(index.probe_count, 2 * member_count)

        exact_duplicate = _ZipPathCollisionIndex.empty()
        self.assertTrue(exact_duplicate.admit(("a",), is_directory=False))
        self.assertFalse(exact_duplicate.admit(("a",), is_directory=False))

        file_ancestor = _ZipPathCollisionIndex.empty()
        self.assertTrue(file_ancestor.admit(("parent",), is_directory=False))
        self.assertFalse(file_ancestor.admit(("parent", "child"), is_directory=False))

        new_file_ancestor = _ZipPathCollisionIndex.empty()
        self.assertTrue(
            new_file_ancestor.admit(("parent", "child"), is_directory=False)
        )
        self.assertFalse(new_file_ancestor.admit(("parent",), is_directory=False))

        directory_ancestor = _ZipPathCollisionIndex.empty()
        self.assertTrue(directory_ancestor.admit(("parent",), is_directory=True))
        self.assertTrue(
            directory_ancestor.admit(("parent", "child"), is_directory=False)
        )

    def test_zip_member_nul_original_name_is_rejected_before_staging(self) -> None:
        archive = _nul_name_zip()
        with zipfile.ZipFile(io.BytesIO(archive)) as parsed:
            info = parsed.infolist()[0]
            self.assertIn("\x00", info.orig_filename)
            self.assertNotEqual(info.orig_filename, info.filename)
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _Transport(archive)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=transport,
                clock=lambda: 1.0,
            )
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "ZIP member name evidence is not exact",
            ):
                backend.materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )
            self.assertEqual(
                (root / fixture.intent.spool_relpath).read_bytes(),
                archive,
            )
            self.assertFalse((root / fixture.intent.staging_lock_relpath).exists())
            self.assertFalse((root / fixture.intent.staging_relpath).exists())
            self.assertFalse((root / fixture.intent.output_relpath).exists())
            self.assertFalse(_quarantine_path(root, fixture).exists())
            self.assertEqual(transport.downloads, 1)

    def test_cleanup_projection_is_linear_for_four_thousand_files(self) -> None:
        archive = _official_zip()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            digest = "sha256:" + hashlib.sha256(b"").hexdigest()
            paths = tuple(
                PurePosixPath(f"item-{index:04d}.bin") for index in range(4_000)
            )
            expected = {path: (digest, 0) for path in paths}
            peak = 0
            for removed in (0, 1, len(paths) // 2, len(paths) - 1, len(paths)):
                projection = _ProjectionTree(
                    files=tuple(
                        _ProjectionFile(path, digest, 0) for path in paths[removed:]
                    ),
                    directory_paths=(PurePosixPath("."),),
                )
                tracemalloc.start()
                started = time.perf_counter()
                try:
                    backend._validate_exact_cleanup_projection(
                        tree=cast(PinnedArtifactTree, projection),
                        expected_files=expected,
                        last_files=(),
                        require_full=False,
                    )
                    _current, observed_peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                elapsed = time.perf_counter() - started
                peak = max(peak, observed_peak)
                self.assertLess(elapsed, 2.0)
            self.assertLess(peak, 8 * 1024 * 1024)

            non_suffix = _ProjectionTree(
                files=tuple(
                    _ProjectionFile(path, digest, 0)
                    for index, path in enumerate(paths)
                    if index != len(paths) // 2
                ),
                directory_paths=(PurePosixPath("."),),
            )
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "deterministic cleanup suffix",
            ):
                backend._validate_exact_cleanup_projection(
                    tree=cast(PinnedArtifactTree, non_suffix),
                    expected_files=expected,
                    last_files=(),
                    require_full=False,
                )
            external_directory = replace(
                _ProjectionTree(
                    files=tuple(_ProjectionFile(path, digest, 0) for path in paths),
                    directory_paths=(PurePosixPath("."),),
                ),
                directory_paths=(PurePosixPath("."), PurePosixPath("foreign")),
            )
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "topology is not exact",
            ):
                backend._validate_exact_cleanup_projection(
                    tree=cast(PinnedArtifactTree, external_directory),
                    expected_files=expected,
                    last_files=(),
                    require_full=False,
                )

    def test_crc_failure_self_cleans_deterministically(self) -> None:
        archive = _corrupt_zip_member(_official_zip(), "_content_list_v2.json")
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _Transport(archive)
            failures: list[str] = []
            for _ in range(3):
                with self.assertRaises(ParserOutputContractError) as caught:
                    MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 1.0,
                    ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                failures.append(str(caught.exception))
                self.assertFalse((root / fixture.intent.staging_relpath).exists())
                self.assertFalse(_quarantine_path(root, fixture).exists())
            self.assertEqual(failures, [failures[0]] * 3)
            self.assertEqual(transport.downloads, 1)

    def test_semantic_failure_never_deletes_injected_or_replaced_file(self) -> None:
        archive = _replace_zip_suffix(
            _official_zip(),
            "_content_list_v2.json",
            b"{not-json",
        )
        for mode in ("inject", "replace"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = _materialize_fixture(archive)

                def interfere(phase: str) -> None:
                    if phase != "before_journaled_staging_cleanup":
                        return
                    staging = root / fixture.intent.staging_relpath
                    if mode == "inject":
                        foreign = staging / ".unpack" / "foreign.bin"
                        foreign.write_bytes(b"foreign")
                        foreign.chmod(0o600)
                        return
                    victim = next(
                        path
                        for path in (staging / ".unpack").rglob("*")
                        if path.is_file()
                    )
                    exact = victim.read_bytes()
                    victim.unlink()
                    victim.write_bytes(exact)
                    victim.chmod(0o600)

                backend = MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=_Transport(archive),
                    clock=lambda: 1.0,
                    fault_hook=interfere,
                )
                with self.assertRaises(ParserOutputContractError):
                    backend.materialize_v4(
                        **fixture.arguments(),
                        claim_guard=_Guard(),
                    )
                quarantine = _quarantine_path(root, fixture)
                self.assertFalse((root / fixture.intent.staging_relpath).exists())
                self.assertTrue(quarantine.is_dir())
                if mode == "inject":
                    self.assertEqual(
                        (quarantine / ".unpack" / "foreign.bin").read_bytes(),
                        b"foreign",
                    )

    def test_marker_bound_large_residual_uses_closed_recovery_envelope(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(
            archive,
            output_bytes=256 * 1024,
            uncompressed_byte_limit=256 * 1024,
            temp_disk_bytes=512 * 1024,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = _Transport(archive)
            backend = _leave_staging_after_mkdir_crash(
                root=root,
                fixture=fixture,
                transport=transport,
            )
            staging = root / fixture.intent.staging_relpath
            marker = staging / Path(fixture.intent.staging_marker_relpath).name
            marker.write_bytes(backend._marker_bytes(fixture.intent))
            marker.chmod(0o600)
            residual = staging / ".unpack" / "large-partial.bin"
            residual.parent.mkdir(mode=0o700)
            residual.write_bytes(b"x" * (160 * 1024))
            residual.chmod(0o600)

            value = MinerUHttpStagedV4(
                scratch_root=root,
                transport=transport,
                clock=lambda: 2.0,
            ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
            quarantine = _quarantine_path(root, fixture)
            self.assertEqual(
                (quarantine / residual.relative_to(staging)).stat().st_size,
                160 * 1024,
            )
            self.assertEqual(value.receipt.spool_sha256, fixture.intent.artifact_sha256)
            self.assertEqual(transport.downloads, 1)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _materialize_fixture(archive)
            transport = _Transport(archive)

            def crash_after_seal(phase: str) -> None:
                if phase == "after_staging_fsync":
                    raise RuntimeError("sealed crash")

            with self.assertRaisesRegex(RuntimeError, "sealed crash"):
                MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 1.0,
                    fault_hook=crash_after_seal,
                ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
            staging = root / fixture.intent.staging_relpath
            manifest = staging / fixture.intent.output_manifest_relpath
            manifest.write_bytes(b"{")
            manifest.chmod(0o600)
            value = MinerUHttpStagedV4(
                scratch_root=root,
                transport=transport,
                clock=lambda: 2.0,
            ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
            quarantine = _quarantine_path(root, fixture)
            self.assertEqual(
                (quarantine / fixture.intent.output_manifest_relpath).read_bytes(),
                b"{",
            )
            self.assertEqual(value.receipt.spool_sha256, fixture.intent.artifact_sha256)
            self.assertEqual(transport.downloads, 1)

    def test_quarantine_rename_fault_windows_and_occupied_slot(self) -> None:
        archive = _official_zip()
        for phase in (
            "before_staging_quarantine_rename",
            "after_staging_quarantine_rename",
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = _materialize_fixture(archive)
                transport = _Transport(archive)
                backend = _leave_staging_after_mkdir_crash(
                    root=root,
                    fixture=fixture,
                    transport=transport,
                )
                staging = root / fixture.intent.staging_relpath
                marker = staging / Path(fixture.intent.staging_marker_relpath).name
                marker.write_bytes(backend._marker_bytes(fixture.intent))
                marker.chmod(0o600)
                partial = staging / "partial.bin"
                partial.write_bytes(b"partial")
                partial.chmod(0o600)

                def crash(observed: str) -> None:
                    if observed == phase:
                        raise RuntimeError(phase)

                with self.assertRaisesRegex(RuntimeError, phase):
                    MinerUHttpStagedV4(
                        scratch_root=root,
                        transport=transport,
                        clock=lambda: 2.0,
                        fault_hook=crash,
                    ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                quarantine = _quarantine_path(root, fixture)
                if phase.startswith("before_"):
                    self.assertTrue(staging.is_dir())
                    self.assertFalse(quarantine.exists())
                else:
                    self.assertFalse(staging.exists())
                    self.assertEqual(
                        (quarantine / "partial.bin").read_bytes(), b"partial"
                    )
                value = MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 3.0,
                ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
                self.assertEqual(
                    value.receipt.spool_sha256,
                    fixture.intent.artifact_sha256,
                )
                self.assertEqual((quarantine / "partial.bin").read_bytes(), b"partial")
                self.assertEqual(transport.downloads, 1)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _materialize_fixture(archive)
            transport = _Transport(archive)
            backend = _leave_staging_after_mkdir_crash(
                root=root,
                fixture=fixture,
                transport=transport,
            )
            staging = root / fixture.intent.staging_relpath
            marker = staging / Path(fixture.intent.staging_marker_relpath).name
            marker.write_bytes(backend._marker_bytes(fixture.intent))
            marker.chmod(0o600)
            partial = staging / "partial.bin"
            partial.write_bytes(b"partial")
            partial.chmod(0o600)
            quarantine = _quarantine_path(root, fixture)
            quarantine.mkdir(mode=0o700)
            retained = quarantine / "prior.bin"
            retained.write_bytes(b"prior")
            retained.chmod(0o600)
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "quarantine collision: source=.*quarantine=",
            ):
                MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 2.0,
                ).materialize_v4(**fixture.arguments(), claim_guard=_Guard())
            self.assertEqual(partial.read_bytes(), b"partial")
            self.assertEqual(retained.read_bytes(), b"prior")

    def test_quarantine_captures_concurrent_injection_and_rejects_marker_swap(
        self,
    ) -> None:
        archive = _official_zip()
        for swap_marker in (False, True):
            with (
                self.subTest(swap_marker=swap_marker),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                fixture = _materialize_fixture(archive)
                transport = _Transport(archive)
                backend = _leave_staging_after_mkdir_crash(
                    root=root,
                    fixture=fixture,
                    transport=transport,
                )
                staging = root / fixture.intent.staging_relpath
                marker = staging / Path(fixture.intent.staging_marker_relpath).name
                marker.write_bytes(backend._marker_bytes(fixture.intent))
                marker.chmod(0o600)
                partial = staging / "partial.bin"
                partial.write_bytes(b"partial")
                partial.chmod(0o600)
                injected = staging / "concurrent.bin"

                def mutate_before_rename(phase: str) -> None:
                    if phase != "before_staging_quarantine_rename":
                        return
                    if swap_marker:
                        marker.unlink()
                        marker.write_bytes(b"foreign-marker")
                        marker.chmod(0o600)
                    else:
                        injected.write_bytes(b"concurrent")
                        injected.chmod(0o600)

                candidate = MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=transport,
                    clock=lambda: 2.0,
                    fault_hook=mutate_before_rename,
                )
                if swap_marker:
                    with self.assertRaisesRegex(
                        ParserOutputContractError, "marker drifted"
                    ):
                        candidate.materialize_v4(
                            **fixture.arguments(), claim_guard=_Guard()
                        )
                    self.assertEqual(marker.read_bytes(), b"foreign-marker")
                    self.assertTrue(staging.is_dir())
                    self.assertFalse(_quarantine_path(root, fixture).exists())
                else:
                    value = candidate.materialize_v4(
                        **fixture.arguments(), claim_guard=_Guard()
                    )
                    quarantine = _quarantine_path(root, fixture)
                    self.assertEqual(
                        (quarantine / "concurrent.bin").read_bytes(),
                        b"concurrent",
                    )
                    self.assertEqual(
                        value.receipt.spool_sha256,
                        fixture.intent.artifact_sha256,
                    )
                    self.assertEqual(transport.downloads, 1)

    def test_replay_rejects_private_mode_drift_and_lock_collision(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            value = backend.materialize_v4(
                **fixture.arguments(),
                claim_guard=_Guard(),
            )
            parser_file = next(
                item
                for item in value.receipt.output_files
                if item.relpath
                not in {
                    fixture.intent.provider_envelope_relpath,
                    fixture.intent.output_manifest_relpath,
                }
            )
            (root / fixture.intent.output_relpath / parser_file.relpath).chmod(0o644)
            with self.assertRaisesRegex(ParserOutputContractError, "unsafe|mode"):
                backend.materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / fixture.intent.spool_lock_relpath
            lock.parent.mkdir(parents=True, mode=0o700)
            lock.write_bytes(b"foreign")
            lock.chmod(0o600)
            with self.assertRaisesRegex(ParserOutputContractError, "metadata drifted"):
                MinerUHttpStagedV4(
                    scratch_root=root,
                    transport=_Transport(archive),
                    clock=lambda: 1.0,
                ).materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )
            self.assertEqual(lock.read_bytes(), b"foreign")

    def test_foreign_output_symlink_and_claim_loss_fail_closed(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / fixture.intent.output_relpath
            output.parent.mkdir(parents=True)
            target = root / "foreign"
            target.mkdir()
            output.symlink_to(target, target_is_directory=True)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            with self.assertRaises(ParserOutputContractError):
                backend.materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(),
                )
            self.assertTrue(output.is_symlink())

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "claim lost"):
                MinerUHttpStagedV4(
                    scratch_root=Path(directory),
                    transport=_Transport(archive),
                    clock=lambda: 1.0,
                ).materialize_v4(
                    **fixture.arguments(),
                    claim_guard=_Guard(fail_at=2),
                )

    def test_cleanup_absence_is_exact_idempotent_replay(self) -> None:
        (
            _final,
            reservation,
            values,
            source,
            cleanup_pending,
            history,
        ) = _typed_pre_submission_failure_bundle()
        preparation, snapshot, submission, failure, plan = values[:5]
        replay = V4EvidenceReplayContext(
            evidence=tuple(
                encode_remote_parse_evidence_v4(value)
                for value in (preparation, snapshot, submission, failure, plan)
            ),
            reservation=reservation,
            resourceful_checkpoint_history=history,
            cleanup_source_checkpoint=source,
        )
        claim = _claim(cleanup_pending)
        with tempfile.TemporaryDirectory() as directory:
            backend = MinerUHttpStagedV4(
                scratch_root=Path(directory),
                transport=_Transport(b"unused"),
                clock=lambda: 1.0,
            )
            common: Any = {
                "checkpoint": cleanup_pending,
                "source_checkpoint": source,
                "reservation": reservation,
                "intent": None,
                "local_receipt": None,
                "plan": plan,
                "claim": claim,
                "claim_guard": _Guard(),
                "replay_context": replay,
            }
            first = backend.cleanup_v4(**common)
            second = backend.cleanup_v4(**common)
            self.assertEqual(first, second)
            self.assertEqual(first.results[0].disposition, "absent")
            self.assertTrue(
                (Path(directory) / reservation.snapshot_lock_relpath).is_file()
            )

    def test_cleanup_deletes_spool_and_transfers_exact_output_idempotently(
        self,
    ) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            materialized = backend.materialize_v4(
                **fixture.arguments(),
                claim_guard=_Guard(),
            )
            local_credit = ResourceCreditVector(
                documents=1,
                snapshot_items=1,
                snapshot_bytes=fixture.reservation.source_byte_count,
                provider_tasks=1,
                provider_result_bytes=len(archive),
                compressed_bytes=len(archive),
                output_items=1,
                output_bytes=materialized.receipt.output_byte_count,
                output_pages=fixture.reservation.source_page_count,
                ack_items=1,
            )
            local = advance_remote_parse_checkpoint_v4(
                fixture.checkpoint,
                state="local_materialized",
                held_resource_credit=local_credit,
                local_materialization_receipt_sha256=materialized.receipt.sha256,
            )
            published = advance_remote_parse_checkpoint_v4(
                local,
                state="publish_committed",
                held_resource_credit=local_credit,
                publication_winner_sha256="sha256:" + "e" * 64,
            )
            resources = (
                CleanupResourceEntryV4(
                    kind="snapshot",
                    relpath=fixture.reservation.snapshot_relpath,
                    ownership_basis_sha256=fixture.reservation.sha256,
                    expected_sha256=fixture.reservation.source_pdf_sha256,
                    expected_byte_count=fixture.reservation.source_byte_count,
                    action="delete",
                ),
                CleanupResourceEntryV4(
                    kind="spool",
                    relpath=fixture.intent.spool_relpath,
                    ownership_basis_sha256=fixture.intent.sha256,
                    expected_sha256=fixture.intent.artifact_sha256,
                    expected_byte_count=fixture.intent.artifact_byte_count,
                    action="delete",
                ),
                CleanupResourceEntryV4(
                    kind="output",
                    relpath=fixture.intent.output_relpath,
                    ownership_basis_sha256=materialized.receipt.sha256,
                    expected_sha256=materialized.receipt.output_files_sha256,
                    expected_byte_count=materialized.receipt.output_byte_count,
                    action="transfer",
                    target_owner_identity=fixture.intent.processing_run_id,
                    target_relpath=(
                        fixture.intent.provider_envelope_context.parser_artifact_root_relpath
                    ),
                ),
            )
            plan = build_local_cleanup_plan_v4(
                reservation=fixture.reservation,
                source_checkpoint=published,
                outcome="success",
                remote_task_identity=fixture.intent.remote_task_identity,
                resources=resources,
                materialization_intent=fixture.intent,
                local_materialization_receipt=materialized.receipt,
            )
            cleanup_pending = advance_remote_parse_checkpoint_v4(
                published,
                state="cleanup_pending",
                held_resource_credit=published.held_resource_credit,
                cleanup_plan_sha256=plan.sha256,
            )
            replay = V4EvidenceReplayContext(
                evidence=tuple(
                    encode_remote_parse_evidence_v4(value)
                    for value in (
                        *fixture.evidence_values,
                        materialized.receipt,
                        plan,
                    )
                ),
                reservation=fixture.reservation,
                resourceful_checkpoint_history=(
                    *fixture.history,
                    local,
                    published,
                ),
                cleanup_source_checkpoint=published,
                local_materialization_manifest=materialized.manifest,
                provider_envelope=materialized.provider_envelope,
            )
            common: Any = {
                "checkpoint": cleanup_pending,
                "source_checkpoint": published,
                "reservation": fixture.reservation,
                "intent": fixture.intent,
                "local_receipt": materialized.receipt,
                "plan": plan,
                "claim": _claim(cleanup_pending),
                "claim_guard": _Guard(),
                "replay_context": replay,
            }
            first = backend.cleanup_v4(**common)
            second = backend.cleanup_v4(**common)
            self.assertEqual(first, second)
            self.assertFalse((root / fixture.intent.spool_relpath).exists())
            self.assertFalse((root / fixture.intent.output_relpath).exists())
            self.assertTrue(
                (
                    root
                    / fixture.intent.provider_envelope_context.parser_artifact_root_relpath
                ).is_dir()
            )
            self.assertTrue((root / fixture.intent.spool_lock_relpath).is_file())
            self.assertTrue((root / fixture.intent.staging_lock_relpath).is_file())

    def test_local_failure_output_delete_is_exact_idempotent_replay(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            materialized = backend.materialize_v4(
                **fixture.arguments(), claim_guard=_Guard()
            )
            common = _local_failure_cleanup_arguments(
                fixture=fixture,
                materialized=materialized,
            )
            first = backend.cleanup_v4(**common)
            second = backend.cleanup_v4(**common)
            self.assertEqual(first, second)
            self.assertFalse((root / fixture.intent.output_relpath).exists())
            self.assertFalse((root / fixture.intent.spool_relpath).exists())

    def test_local_failure_output_delete_resumes_exact_cleanup_suffix(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            materialized = backend.materialize_v4(
                **fixture.arguments(), claim_guard=_Guard()
            )
            common = _local_failure_cleanup_arguments(
                fixture=fixture,
                materialized=materialized,
            )
            output = root / fixture.intent.output_relpath
            full_count = materialized.receipt.output_file_count

            class LoseAfterFirstOutputFile(_Guard):
                def assert_current_under_resource_lock(
                    self,
                    *,
                    checkpoint: RemoteParseCheckpointV4,
                    claim: V4ClaimWitness,
                ) -> None:
                    super().assert_current_under_resource_lock(
                        checkpoint=checkpoint,
                        claim=claim,
                    )
                    if output.is_dir():
                        remaining = sum(
                            1 for path in output.rglob("*") if path.is_file()
                        )
                        if 0 < remaining < full_count:
                            raise RuntimeError("claim lost during output cleanup")

            common["claim_guard"] = LoseAfterFirstOutputFile()
            with self.assertRaisesRegex(RuntimeError, "output cleanup"):
                backend.cleanup_v4(**common)
            self.assertTrue(output.is_dir())
            self.assertLess(
                sum(1 for path in output.rglob("*") if path.is_file()),
                full_count,
            )
            common["claim_guard"] = _Guard()
            first = backend.cleanup_v4(**common)
            second = backend.cleanup_v4(**common)
            self.assertEqual(first, second)
            self.assertFalse(output.exists())

    def test_cleanup_rejects_both_transfer_source_and_target(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            materialized = backend.materialize_v4(
                **fixture.arguments(),
                claim_guard=_Guard(),
            )
            common = _successful_cleanup_arguments(
                fixture=fixture,
                materialized=materialized,
            )
            target = (
                root
                / fixture.intent.provider_envelope_context.parser_artifact_root_relpath
            )
            current = root
            for part in target.parent.relative_to(root).parts:
                current /= part
                current.mkdir(mode=0o700, exist_ok=True)
                current.chmod(0o700)
            shutil.copytree(root / fixture.intent.output_relpath, target)
            for path in target.rglob("*"):
                path.chmod(0o700 if path.is_dir() else 0o600)
            target.chmod(0o700)
            with self.assertRaisesRegex(ParserOutputContractError, "both source"):
                backend.cleanup_v4(**common)

    def test_publication_promotion_and_read_only_verification_replay_exactly(
        self,
    ) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            materialized = backend.materialize_v4(
                **fixture.arguments(),
                claim_guard=_Guard(),
            )
            cleanup = _successful_cleanup_arguments(
                fixture=fixture,
                materialized=materialized,
            )
            checkpoint = cleanup["replay_context"].resourceful_checkpoint_history[-2]
            published_relpath = (
                fixture.intent.provider_envelope_context.parser_artifact_root_relpath
            )
            arguments = {
                "checkpoint": checkpoint,
                "materialized": materialized,
                "published_relpath": published_relpath,
                "claim": _claim(checkpoint),
                "claim_guard": _Guard(),
            }

            backend.promote_or_replay(**arguments)
            backend.verify_published(
                published_relpath=published_relpath,
                expected_inventory_sha256=(materialized.receipt.output_files_sha256),
                expected_file_count=materialized.receipt.output_file_count,
                expected_byte_count=materialized.receipt.output_byte_count,
            )
            backend.promote_or_replay(**arguments)

            self.assertFalse((root / fixture.intent.output_relpath).exists())
            self.assertTrue((root / published_relpath).is_dir())

    def test_cleanup_replays_after_transfer_rename_before_parent_fsync(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def crash(phase: str) -> None:
                if phase == "after_cleanup_transfer_rename":
                    raise RuntimeError(phase)

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
                fault_hook=crash,
            )
            materialized = backend.materialize_v4(
                **fixture.arguments(),
                claim_guard=_Guard(),
            )
            common = _successful_cleanup_arguments(
                fixture=fixture,
                materialized=materialized,
            )
            with self.assertRaisesRegex(RuntimeError, "after_cleanup_transfer_rename"):
                backend.cleanup_v4(**common)
            replay = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 2.0,
            ).cleanup_v4(**common)
            self.assertEqual(replay.results[-1].disposition, "transferred")
            self.assertFalse((root / fixture.intent.output_relpath).exists())
            self.assertTrue(
                (
                    root
                    / fixture.intent.provider_envelope_context.parser_artifact_root_relpath
                ).is_dir()
            )

    def test_cleanup_rejects_missing_transfer_source_and_target(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            materialized = backend.materialize_v4(
                **fixture.arguments(),
                claim_guard=_Guard(),
            )
            common = _successful_cleanup_arguments(
                fixture=fixture,
                materialized=materialized,
            )
            shutil.rmtree(root / fixture.intent.output_relpath)
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "not an owned directory|evidence disappeared|lost both",
            ):
                backend.cleanup_v4(**common)

    def test_cleanup_rejects_source_directory_aba_before_transfer(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / fixture.intent.output_relpath
            backup = root / "aba-backup"
            swapped = False

            def swap(phase: str) -> None:
                nonlocal swapped
                if phase != "before_cleanup_transfer" or swapped:
                    return
                swapped = True
                source.rename(backup)
                shutil.copytree(backup, source)
                for path in source.rglob("*"):
                    path.chmod(0o700 if path.is_dir() else 0o600)
                source.chmod(0o700)

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
                fault_hook=swap,
            )
            materialized = backend.materialize_v4(
                **fixture.arguments(),
                claim_guard=_Guard(),
            )
            common = _successful_cleanup_arguments(
                fixture=fixture,
                materialized=materialized,
            )
            with self.assertRaisesRegex(ParserOutputContractError, "replaced"):
                backend.cleanup_v4(**common)
            self.assertTrue(source.is_dir())
            self.assertTrue(backup.is_dir())

    def test_cleanup_rejects_private_mode_race_before_transfer(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initial = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            materialized = initial.materialize_v4(
                **fixture.arguments(), claim_guard=_Guard()
            )
            source = root / fixture.intent.output_relpath
            changed = False

            def chmod_before_transfer(phase: str) -> None:
                nonlocal changed
                if phase != "before_cleanup_transfer" or changed:
                    return
                changed = True
                victim = next(path for path in source.rglob("*") if path.is_file())
                victim.chmod(0o644)

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 2.0,
                fault_hook=chmod_before_transfer,
            )
            with self.assertRaisesRegex(ParserOutputContractError, "mode"):
                backend.cleanup_v4(
                    **_successful_cleanup_arguments(
                        fixture=fixture, materialized=materialized
                    )
                )
            self.assertTrue(source.is_dir())

    def test_cleanup_rejects_target_parent_aba_after_transfer_rename(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initial = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            materialized = initial.materialize_v4(
                **fixture.arguments(), claim_guard=_Guard()
            )
            target = (
                root
                / fixture.intent.provider_envelope_context.parser_artifact_root_relpath
            )
            backup = root / "moved-target-parent"
            changed = False

            def swap_parent(phase: str) -> None:
                nonlocal changed
                if phase != "after_cleanup_transfer_rename" or changed:
                    return
                changed = True
                target.parent.rename(backup)
                target.parent.mkdir(parents=True, mode=0o700)
                target.parent.chmod(0o700)

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 2.0,
                fault_hook=swap_parent,
            )
            with self.assertRaises(ParserOutputContractError):
                backend.cleanup_v4(
                    **_successful_cleanup_arguments(
                        fixture=fixture, materialized=materialized
                    )
                )
            self.assertFalse(target.exists())
            self.assertTrue((backup / target.name).is_dir())

    def test_cleanup_refuses_foreign_snapshot_part_without_writer_proof(self) -> None:
        (
            _final,
            reservation,
            values,
            source,
            original_cleanup_pending,
            history,
        ) = _typed_pre_submission_failure_bundle()
        preparation, snapshot, submission, failure, original_plan = values[:5]
        resources = tuple(
            sorted(
                (
                    *original_plan.resources,
                    CleanupResourceEntryV4(
                        kind="snapshot_part",
                        relpath=reservation.snapshot_part_relpath,
                        ownership_basis_sha256=reservation.sha256,
                        expected_sha256=None,
                        expected_byte_count=None,
                        action="delete",
                    ),
                    CleanupResourceEntryV4(
                        kind="snapshot_part_owner",
                        relpath=reservation.snapshot_part_owner_relpath,
                        ownership_basis_sha256=reservation.sha256,
                        expected_sha256=None,
                        expected_byte_count=None,
                        action="delete",
                    ),
                ),
                key=lambda item: (
                    (
                        "snapshot snapshot_part snapshot_part_owner spool spool_part "
                        "spool_part_owner staging staging_marker output"
                    )
                    .split()
                    .index(item.kind)
                ),
            )
        )
        plan = build_local_cleanup_plan_v4(
            reservation=reservation,
            source_checkpoint=source,
            outcome=original_plan.outcome,
            resources=resources,
            failure_receipt_sha256=original_plan.failure_receipt_sha256,
        )
        cleanup_pending = replace(
            original_cleanup_pending,
            cleanup_plan_sha256=plan.sha256,
        )
        replay = V4EvidenceReplayContext(
            evidence=tuple(
                encode_remote_parse_evidence_v4(value)
                for value in (preparation, snapshot, submission, failure, plan)
            ),
            reservation=reservation,
            resourceful_checkpoint_history=history,
            cleanup_source_checkpoint=source,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            part = root / reservation.snapshot_part_relpath
            part.parent.mkdir(parents=True, mode=0o700)
            part.write_bytes(b"foreign")
            part.chmod(0o600)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(b"unused"),
                clock=lambda: 1.0,
            )
            with self.assertRaisesRegex(ParserOutputContractError, "writer proof"):
                backend.cleanup_v4(
                    checkpoint=cleanup_pending,
                    source_checkpoint=source,
                    reservation=reservation,
                    intent=None,
                    local_receipt=None,
                    plan=plan,
                    claim=_claim(cleanup_pending),
                    claim_guard=_Guard(),
                    replay_context=replay,
                )
            self.assertEqual(part.read_bytes(), b"foreign")

    def test_staging_cleanup_keeps_marker_last_across_claim_loss(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            staging = root / fixture.intent.staging_relpath
            with backend._open_dir(staging, create=True):
                pass
            marker = root / fixture.intent.staging_marker_relpath
            marker.write_bytes(backend._marker_bytes(fixture.intent))
            marker.chmod(0o600)
            proof = backend._path_identity(staging)

            with self.assertRaisesRegex(RuntimeError, "claim lost"):
                backend._delete_planned(
                    staging,
                    None,
                    None,
                    before_effect=lambda: (_ for _ in ()).throw(
                        RuntimeError("claim lost")
                    ),
                    volatile_proof=proof,
                    volatile_proof_required=True,
                    max_files=1,
                    last_file_name=marker.name,
                    last_file_bytes=backend._marker_bytes(fixture.intent),
                    expected_tree_files=None,
                )
            self.assertTrue(marker.is_file())
            backend._delete_planned(
                staging,
                None,
                None,
                before_effect=lambda: None,
                volatile_proof=backend._path_identity(staging),
                volatile_proof_required=True,
                max_files=1,
                last_file_name=marker.name,
                last_file_bytes=backend._marker_bytes(fixture.intent),
                expected_tree_files=None,
            )
            self.assertFalse(staging.exists())

    def test_staging_cleanup_refuses_ambiguous_partial_tree(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            staging = root / fixture.intent.staging_relpath
            with backend._open_dir(staging, create=True):
                pass
            marker = root / fixture.intent.staging_marker_relpath
            marker.write_bytes(backend._marker_bytes(fixture.intent))
            marker.chmod(0o600)
            junk = staging / "z-remains.bin"
            junk.write_bytes(b"junk")
            junk.chmod(0o600)
            with self.assertRaises(ParserOutputContractError):
                backend._delete_planned(
                    staging,
                    None,
                    None,
                    before_effect=lambda: None,
                    volatile_proof=backend._path_identity(staging),
                    volatile_proof_required=True,
                    max_files=2,
                    last_file_name=marker.name,
                    last_file_bytes=backend._marker_bytes(fixture.intent),
                    expected_tree_files=None,
                )
            self.assertEqual(marker.read_bytes(), backend._marker_bytes(fixture.intent))
            self.assertEqual(junk.read_bytes(), b"junk")

    def test_output_cleanup_rejects_late_same_uid_injection(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            injected = root / fixture.intent.output_relpath / "late-foreign.bin"

            def inject_after_admission(phase: str) -> None:
                if phase != "before_cleanup_exact_delete":
                    return
                injected.write_bytes(b"foreign")
                injected.chmod(0o600)

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
                fault_hook=inject_after_admission,
            )
            materialized = backend.materialize_v4(
                **fixture.arguments(), claim_guard=_Guard()
            )
            output = root / fixture.intent.output_relpath
            original_files = {
                item.relpath: (output / item.relpath).read_bytes()
                for item in materialized.receipt.output_files
            }
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "exact cleanup topology changed",
            ):
                backend._delete_planned(
                    output,
                    materialized.receipt.output_files_sha256,
                    materialized.receipt.output_byte_count,
                    before_effect=lambda: None,
                    volatile_proof=None,
                    volatile_proof_required=False,
                    max_files=materialized.receipt.output_file_count,
                    last_file_name=None,
                    last_file_bytes=None,
                    expected_tree_files=materialized.receipt.output_files,
                )
            self.assertEqual(injected.read_bytes(), b"foreign")
            self.assertEqual(
                {
                    relpath: (output / relpath).read_bytes()
                    for relpath in original_files
                },
                original_files,
            )

    def test_cleanup_rejects_concurrent_transfer_target_creation(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / fixture.intent.output_relpath
            target = (
                root
                / fixture.intent.provider_envelope_context.parser_artifact_root_relpath
            )
            injected = False

            def create_target(phase: str) -> None:
                nonlocal injected
                if phase != "before_cleanup_transfer" or injected:
                    return
                injected = True
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                shutil.copytree(source, target)
                for path in target.rglob("*"):
                    path.chmod(0o700 if path.is_dir() else 0o600)
                target.chmod(0o700)

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
                fault_hook=create_target,
            )
            materialized = backend.materialize_v4(
                **fixture.arguments(),
                claim_guard=_Guard(),
            )
            common = _successful_cleanup_arguments(
                fixture=fixture,
                materialized=materialized,
            )
            with self.assertRaisesRegex(
                ParserOutputContractError, "destination exists"
            ):
                backend.cleanup_v4(**common)
            self.assertTrue(source.is_dir())
            self.assertTrue(target.is_dir())

    def test_cleanup_rejects_spool_file_aba_before_delete(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spool = root / fixture.intent.spool_relpath
            backup = root / "spool-aba-backup.zip"
            swapped = False

            def swap(phase: str) -> None:
                nonlocal swapped
                if phase != "before_cleanup_delete" or swapped:
                    return
                if not spool.exists():
                    return
                swapped = True
                exact = spool.read_bytes()
                spool.rename(backup)
                spool.write_bytes(exact)
                spool.chmod(0o600)

            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
                fault_hook=swap,
            )
            materialized = backend.materialize_v4(
                **fixture.arguments(),
                claim_guard=_Guard(),
            )
            common = _successful_cleanup_arguments(
                fixture=fixture,
                materialized=materialized,
            )
            with self.assertRaisesRegex(ParserOutputContractError, "identity changed"):
                backend.cleanup_v4(**common)
            self.assertTrue(spool.is_file())
            self.assertTrue(backup.is_file())

    def test_cleanup_claim_loss_precedes_destructive_side_effect(self) -> None:
        archive = _official_zip()
        fixture = _materialize_fixture(archive)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = MinerUHttpStagedV4(
                scratch_root=root,
                transport=_Transport(archive),
                clock=lambda: 1.0,
            )
            materialized = backend.materialize_v4(
                **fixture.arguments(),
                claim_guard=_Guard(),
            )
            common = _successful_cleanup_arguments(
                fixture=fixture,
                materialized=materialized,
            )
            common["claim_guard"] = _Guard(fail_at=4)
            with self.assertRaisesRegex(RuntimeError, "claim lost"):
                backend.cleanup_v4(**common)
            self.assertTrue((root / fixture.intent.spool_relpath).is_file())
            self.assertTrue((root / fixture.intent.output_relpath).is_dir())

    def test_pinned_read_bytes_rejects_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "value.json"
            artifact.write_bytes(b'{"value":1}')
            with PinnedArtifactTree.open_path(root) as tree:
                self.assertEqual(
                    tree.read_bytes(PurePosixPath("value.json"), max_bytes=64),
                    b'{"value":1}',
                )
                artifact.unlink()
                artifact.write_bytes(b'{"value":1}')
                with self.assertRaisesRegex(
                    ParserOutputContractError, "identity changed"
                ):
                    tree.read_bytes(PurePosixPath("value.json"), max_bytes=64)

    def test_ack_exact_success_absence_and_response_loss_replay(self) -> None:
        fixture = _happy_path_for_port()
        checkpoint = fixture["chain"][8]
        command = seal_provider_ack_command_v4(
            ack_pending_checkpoint=checkpoint,
            accepted_submission=fixture["accepted"],
            terminal_receipt=fixture["terminal"],
            cleanup_plan=fixture["cleanup_plan"],
            cleanup_receipt=fixture["cleanup_receipt"],
            replay_context=_ack_replay(fixture),
        )
        capability = _capability(fixture, "result_acknowledgement")
        claim = _claim(checkpoint)
        exact_200 = (
            b'{"schema":"mineru-task-protocol.v2","task_id":"task-1",'
            b'"status":"consumed"}'
        )
        with tempfile.TemporaryDirectory() as directory:
            transport = _Transport(
                b"unused", response=ProviderAckTransportResponseV4(200, exact_200)
            )
            backend = MinerUHttpStagedV4(
                scratch_root=Path(directory),
                transport=transport,
                clock=lambda: 1.0,
            )
            consumed = backend.acknowledge_v4(
                command=command,
                provider_capability=capability,
                claim=claim,
                claim_guard=_Guard(),
                stage_guard=_StepGuard(),
            )
            self.assertEqual(consumed.ack_kind, "consumed")
            self.assertEqual(consumed.provider_receipt_identity, "task-1")

            transport.response = TimeoutError("response lost")
            with self.assertRaisesRegex(TimeoutError, "response lost"):
                backend.acknowledge_v4(
                    command=command,
                    provider_capability=capability,
                    claim=claim,
                    claim_guard=_Guard(),
                    stage_guard=_StepGuard(),
                )
            transport.response = ProviderAckTransportResponseV4(
                404, _canonical({"detail": "Task not found"})
            )
            absent = backend.acknowledge_v4(
                command=command,
                provider_capability=capability,
                claim=claim,
                claim_guard=_Guard(),
                stage_guard=_StepGuard(),
            )
            self.assertEqual(absent.ack_kind, "absent")
            self.assertEqual(absent.request_identity, consumed.request_identity)

            transport.response = ProviderAckTransportResponseV4(
                404, _canonical({"detail": "something else"})
            )
            with self.assertRaisesRegex(ParserOutputContractError, "absence"):
                backend.acknowledge_v4(
                    command=command,
                    provider_capability=capability,
                    claim=claim,
                    claim_guard=_Guard(),
                    stage_guard=_StepGuard(),
                )

    def test_ack_is_serialized_and_rejects_duck_response(self) -> None:
        fixture = _happy_path_for_port()
        checkpoint = fixture["chain"][8]
        command = seal_provider_ack_command_v4(
            ack_pending_checkpoint=checkpoint,
            accepted_submission=fixture["accepted"],
            terminal_receipt=fixture["terminal"],
            cleanup_plan=fixture["cleanup_plan"],
            cleanup_receipt=fixture["cleanup_receipt"],
            replay_context=_ack_replay(fixture),
        )
        capability = _capability(fixture, "result_acknowledgement")

        class ConcurrentTransport(_Transport):
            def __init__(self) -> None:
                super().__init__(b"unused")
                self.active = 0
                self.max_active = 0
                self.mutex = threading.Lock()

            def acknowledge(self, **_: object) -> ProviderAckTransportResponseV4:
                with self.mutex:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.02)
                with self.mutex:
                    self.active -= 1
                return ProviderAckTransportResponseV4(204, b"")

        with tempfile.TemporaryDirectory() as directory:
            transport = ConcurrentTransport()
            backend = MinerUHttpStagedV4(
                scratch_root=Path(directory),
                transport=transport,
                clock=lambda: 1.0,
            )
            errors: list[BaseException] = []

            def run() -> None:
                try:
                    backend.acknowledge_v4(
                        command=command,
                        provider_capability=capability,
                        claim=_claim(checkpoint),
                        claim_guard=_Guard(),
                        stage_guard=_StepGuard(),
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(transport.max_active, 1)

        class DuckTransport(_Transport):
            def acknowledge(self, **_: object) -> Any:
                return type("Duck", (), {"status_code": 204, "exact_bytes": b""})()

        with tempfile.TemporaryDirectory() as directory:
            backend = MinerUHttpStagedV4(
                scratch_root=Path(directory),
                transport=DuckTransport(b"unused"),
                clock=lambda: 1.0,
            )
            with self.assertRaisesRegex(ParserOutputContractError, "forged response"):
                backend.acknowledge_v4(
                    command=command,
                    provider_capability=capability,
                    claim=_claim(checkpoint),
                    claim_guard=_Guard(),
                    stage_guard=_StepGuard(),
                )

        class EvilBytes(bytes):
            def __ne__(self, _: object) -> bool:
                return False

        forged = ProviderAckTransportResponseV4(204, b"")
        object.__setattr__(forged, "exact_bytes", EvilBytes(b"NOT-EMPTY"))
        with tempfile.TemporaryDirectory() as directory:
            backend = MinerUHttpStagedV4(
                scratch_root=Path(directory),
                transport=_Transport(b"unused", response=forged),
                clock=lambda: 1.0,
            )
            with self.assertRaisesRegex(ParserOutputContractError, "forged response"):
                backend.acknowledge_v4(
                    command=command,
                    provider_capability=capability,
                    claim=_claim(checkpoint),
                    claim_guard=_Guard(),
                    stage_guard=_StepGuard(),
                )


def _leave_staging_after_mkdir_crash(
    *,
    root: Path,
    fixture: _MaterializeFixture,
    transport: _Transport,
) -> MinerUHttpStagedV4:
    def crash(phase: str) -> None:
        if phase == "after_staging_mkdir":
            raise RuntimeError("mkdir crash")

    backend = MinerUHttpStagedV4(
        scratch_root=root,
        transport=transport,
        clock=lambda: 1.0,
        fault_hook=crash,
    )
    with unittest.TestCase().assertRaisesRegex(RuntimeError, "mkdir crash"):
        backend.materialize_v4(**fixture.arguments(), claim_guard=_Guard())
    return backend


def _quarantine_path(root: Path, fixture: _MaterializeFixture) -> Path:
    staging = PurePosixPath(fixture.intent.staging_relpath)
    digest = hashlib.sha256(
        (fixture.intent.sha256 + "\x00" + staging.as_posix()).encode("utf-8")
    ).hexdigest()[:32]
    return root.joinpath(*(staging.parent / f".agent-v4-quarantine-{digest}").parts)


def _materialize_fixture(
    archive: bytes,
    *,
    output_bytes: int = 128 * 1024,
    uncompressed_byte_limit: int = 64 * 1024,
    temp_disk_bytes: int = 256 * 1024,
    source_page_count: int | None = None,
) -> _MaterializeFixture:
    base, base_allowance = _exact_materialization_reservation_and_allowance()
    exact_page_count = (
        base.source_page_count if source_page_count is None else source_page_count
    )
    reserved = replace(
        base.reserved_credit,
        provider_result_bytes=len(archive),
        compressed_bytes=len(archive),
        decoded_bytes=128 * 1024,
        temp_disk_bytes=temp_disk_bytes,
        output_bytes=output_bytes,
        output_pages=exact_page_count,
    )
    encoded_input = encode_resource_reservation_input(
        replace(
            base_allowance.reservation_input.value,
            source_page_count=exact_page_count,
            reservation=reserved,
        )
    )
    allowance = PerAttemptResourceAllowance(
        reservation_input_sha256=encoded_input.sha256,
        reservation_input=encoded_input,
        limits=reserved,
    )
    reservation = build_resource_reservation_v4(
        attempt_id=base.attempt_id,
        attempt_generation=base.attempt_generation,
        fence_identity=base.fence_identity,
        document_id=base.document_id,
        processing_run_id=base.processing_run_id,
        source_pdf_sha256=base.source_pdf_sha256,
        source_byte_count=base.source_byte_count,
        source_page_count=exact_page_count,
        prepared_submission_identity_sha256=base.prepared_submission_identity_sha256,
        request_sha256=base.request_sha256,
        runtime_epoch_sha256=base.runtime_epoch_sha256,
        process_profile_sha256=base.process_profile_sha256,
        credit_policy_sha256=base.credit_policy_sha256,
        reservation_bucket=base.reservation_bucket,
        reservation_input_sha256=encoded_input.sha256,
        reserved_credit=reserved,
    )
    context = replace(
        _provider_envelope_context(),
        source_page_count=exact_page_count,
    )
    preparation = build_preparation_intent_v4(
        reservation=reservation,
        parser_target_sha256=context.parser_target_sha256,
    )
    snapshot = SnapshotReceiptV4(
        attempt_id=reservation.attempt_id,
        fence_identity=reservation.fence_identity,
        preparation_intent_sha256=preparation.sha256,
        snapshot_relpath=reservation.snapshot_relpath,
        snapshot_sha256=reservation.source_pdf_sha256,
        snapshot_byte_count=reservation.source_byte_count,
        part_path_absent=True,
        part_owner_path_absent=True,
        file_fsync_completed=True,
        parent_fsync_completed=True,
    )
    prepared = build_initial_remote_parse_checkpoint_v4(
        reservation=reservation,
        preparation_intent_sha256=preparation.sha256,
        snapshot_receipt_sha256=snapshot.sha256,
        held_resource_credit=_snapshot_credit(),
    )
    submission = SubmissionIntentV4(
        attempt_id=reservation.attempt_id,
        fence_identity=reservation.fence_identity,
        snapshot_receipt_sha256=snapshot.sha256,
        source_pdf_sha256=reservation.source_pdf_sha256,
        parser_target_sha256=preparation.parser_target_sha256,
        request_sha256=reservation.request_sha256,
        runtime_epoch_sha256=reservation.runtime_epoch_sha256,
        client_submit_key="submit-v4-http-test",
        submission_epoch_unix=1,
        provider_protocol_version="mineru-task-protocol.v2",
    )
    reconciling = advance_remote_parse_checkpoint_v4(
        prepared,
        state="reconciling",
        held_resource_credit=replace(_snapshot_credit(), remote_waits=1),
        submission_intent_sha256=submission.sha256,
    )
    token = b"v4-http-private-token"
    token_sha = "sha256:" + hashlib.sha256(token).hexdigest()
    accepted = AcceptedSubmissionReceiptV4(
        attempt_id=reservation.attempt_id,
        fence_identity=reservation.fence_identity,
        submission_intent_sha256=submission.sha256,
        remote_task_identity="task-1",
        status_url="https://provider.invalid/tasks/task-1",
        result_url="https://provider.invalid/tasks/task-1/result",
        secret_kind="mineru-task-token.v1",
        secret_version=1,
        token_sha256=token_sha,
        token_byte_count=len(token),
        provider_protocol_version=submission.provider_protocol_version,
    )
    submitted = advance_remote_parse_checkpoint_v4(
        reconciling,
        state="submitted",
        held_resource_credit=_submitted_credit(),
        accepted_submission_sha256=accepted.sha256,
    )
    artifact_sha = "sha256:" + hashlib.sha256(archive).hexdigest()
    terminal = TerminalReceiptV4(
        attempt_id=reservation.attempt_id,
        fence_identity=reservation.fence_identity,
        accepted_submission_receipt_sha256=accepted.sha256,
        remote_task_identity=accepted.remote_task_identity,
        result_owner_identity="result-owner-1",
        artifact_sha256=artifact_sha,
        artifact_byte_count=len(archive),
        provider_protocol_version=accepted.provider_protocol_version,
    )
    terminal_credit = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=reservation.source_byte_count,
        provider_tasks=1,
        provider_result_bytes=len(archive),
        ack_items=1,
    )
    remote_terminal = advance_remote_parse_checkpoint_v4(
        submitted,
        state="remote_terminal",
        held_resource_credit=terminal_credit,
        terminal_receipt_sha256=terminal.sha256,
    )
    intent = build_materialization_intent_v4(
        reservation=reservation,
        source_checkpoint=remote_terminal,
        terminal_receipt_sha256=terminal.sha256,
        remote_task_identity=terminal.remote_task_identity,
        artifact_owner_identity=terminal.result_owner_identity,
        artifact_sha256=terminal.artifact_sha256,
        artifact_byte_count=terminal.artifact_byte_count,
        provider_envelope_context=context,
        allowance_sha256=allowance.sha256,
        provider_capability_kind=accepted.secret_kind,
        provider_capability_sha256=accepted.token_sha256,
        provider_capability_byte_count=accepted.token_byte_count,
        output_dir_name="output-http-v4",
        provider_envelope_relpath=PROVIDER_DOCUMENT_FILENAME,
        output_manifest_relpath=LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
        member_count_limit=64,
        uncompressed_byte_limit=uncompressed_byte_limit,
    )
    materializing = advance_remote_parse_checkpoint_v4(
        remote_terminal,
        state="materializing",
        held_resource_credit=intent.held_resource_credit,
        materialization_intent_sha256=intent.sha256,
    )
    capability = PrivateProviderCapabilityV4(
        attempt_id=reservation.attempt_id,
        remote_task_identity=accepted.remote_task_identity,
        provider_protocol_version=accepted.provider_protocol_version,
        secret_kind=accepted.secret_kind,
        secret_version=accepted.secret_version,
        capability_purpose="result_download",
        token_bytes=token,
        token_sha256=token_sha,
        token_byte_count=len(token),
    )
    replay = V4EvidenceReplayContext(
        evidence=tuple(
            encode_remote_parse_evidence_v4(value)
            for value in (
                preparation,
                snapshot,
                submission,
                accepted,
                terminal,
                intent,
            )
        ),
        reservation=reservation,
        resourceful_checkpoint_history=(
            prepared,
            reconciling,
            submitted,
            remote_terminal,
            materializing,
        ),
    )
    return _MaterializeFixture(
        reservation=reservation,
        allowance=allowance,
        preparation=preparation,
        accepted=accepted,
        terminal=terminal,
        intent=intent,
        checkpoint=materializing,
        capability=capability,
        claim=_claim(materializing),
        replay=replay,
        evidence_values=(
            preparation,
            snapshot,
            submission,
            accepted,
            terminal,
            intent,
        ),
        history=(
            prepared,
            reconciling,
            submitted,
            remote_terminal,
            materializing,
        ),
    )


def _submission_snapshot_fixture(source: bytes) -> _SubmissionSnapshotFixture:
    base, base_allowance = _exact_materialization_reservation_and_allowance()
    source_sha256 = "sha256:" + hashlib.sha256(source).hexdigest()
    reserved = replace(
        base.reserved_credit,
        snapshot_bytes=len(source),
    )
    encoded_input = encode_resource_reservation_input(
        replace(
            base_allowance.reservation_input.value,
            source_pdf_sha256=source_sha256,
            source_byte_count=len(source),
            reservation=reserved,
        )
    )
    reservation = build_resource_reservation_v4(
        attempt_id=base.attempt_id,
        attempt_generation=base.attempt_generation,
        fence_identity=base.fence_identity,
        document_id=base.document_id,
        processing_run_id=base.processing_run_id,
        source_pdf_sha256=source_sha256,
        source_byte_count=len(source),
        source_page_count=base.source_page_count,
        prepared_submission_identity_sha256=(
            base.prepared_submission_identity_sha256
        ),
        request_sha256=base.request_sha256,
        runtime_epoch_sha256=base.runtime_epoch_sha256,
        process_profile_sha256=base.process_profile_sha256,
        credit_policy_sha256=base.credit_policy_sha256,
        reservation_bucket=base.reservation_bucket,
        reservation_input_sha256=encoded_input.sha256,
        reserved_credit=reserved,
    )
    preparation = build_preparation_intent_v4(
        reservation=reservation,
        parser_target_sha256=_provider_envelope_context().parser_target_sha256,
    )
    snapshot = SnapshotReceiptV4(
        attempt_id=reservation.attempt_id,
        fence_identity=reservation.fence_identity,
        preparation_intent_sha256=preparation.sha256,
        snapshot_relpath=reservation.snapshot_relpath,
        snapshot_sha256=source_sha256,
        snapshot_byte_count=len(source),
        part_path_absent=True,
        part_owner_path_absent=True,
        file_fsync_completed=True,
        parent_fsync_completed=True,
    )
    snapshot_credit = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=len(source),
    )
    prepared = build_initial_remote_parse_checkpoint_v4(
        reservation=reservation,
        preparation_intent_sha256=preparation.sha256,
        snapshot_receipt_sha256=snapshot.sha256,
        held_resource_credit=snapshot_credit,
    )
    submission = SubmissionIntentV4(
        attempt_id=reservation.attempt_id,
        fence_identity=reservation.fence_identity,
        snapshot_receipt_sha256=snapshot.sha256,
        source_pdf_sha256=source_sha256,
        parser_target_sha256=preparation.parser_target_sha256,
        request_sha256=reservation.request_sha256,
        runtime_epoch_sha256=reservation.runtime_epoch_sha256,
        client_submit_key="submit-v4-snapshot-source-test",
        submission_epoch_unix=1,
        provider_protocol_version="mineru-task-protocol.v2",
    )
    reconciling = advance_remote_parse_checkpoint_v4(
        prepared,
        state="reconciling",
        held_resource_credit=replace(snapshot_credit, remote_waits=1),
        submission_intent_sha256=submission.sha256,
    )
    return _SubmissionSnapshotFixture(
        source=source,
        reservation=reservation,
        snapshot=snapshot,
        submission=submission,
        checkpoint=reconciling,
        evidence=(preparation, snapshot, submission),
        history=(prepared, reconciling),
    )


def _successful_cleanup_arguments(
    *,
    fixture: _MaterializeFixture,
    materialized: Any,
) -> Any:
    local_credit = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=fixture.reservation.source_byte_count,
        provider_tasks=1,
        provider_result_bytes=fixture.intent.artifact_byte_count,
        compressed_bytes=fixture.intent.artifact_byte_count,
        output_items=1,
        output_bytes=materialized.receipt.output_byte_count,
        output_pages=fixture.reservation.source_page_count,
        ack_items=1,
    )
    local = advance_remote_parse_checkpoint_v4(
        fixture.checkpoint,
        state="local_materialized",
        held_resource_credit=local_credit,
        local_materialization_receipt_sha256=materialized.receipt.sha256,
    )
    published = advance_remote_parse_checkpoint_v4(
        local,
        state="publish_committed",
        held_resource_credit=local_credit,
        publication_winner_sha256="sha256:" + "e" * 64,
    )
    resources = (
        CleanupResourceEntryV4(
            kind="snapshot",
            relpath=fixture.reservation.snapshot_relpath,
            ownership_basis_sha256=fixture.reservation.sha256,
            expected_sha256=fixture.reservation.source_pdf_sha256,
            expected_byte_count=fixture.reservation.source_byte_count,
            action="delete",
        ),
        CleanupResourceEntryV4(
            kind="spool",
            relpath=fixture.intent.spool_relpath,
            ownership_basis_sha256=fixture.intent.sha256,
            expected_sha256=fixture.intent.artifact_sha256,
            expected_byte_count=fixture.intent.artifact_byte_count,
            action="delete",
        ),
        CleanupResourceEntryV4(
            kind="output",
            relpath=fixture.intent.output_relpath,
            ownership_basis_sha256=materialized.receipt.sha256,
            expected_sha256=materialized.receipt.output_files_sha256,
            expected_byte_count=materialized.receipt.output_byte_count,
            action="transfer",
            target_owner_identity=fixture.intent.processing_run_id,
            target_relpath=(
                fixture.intent.provider_envelope_context.parser_artifact_root_relpath
            ),
        ),
    )
    plan = build_local_cleanup_plan_v4(
        reservation=fixture.reservation,
        source_checkpoint=published,
        outcome="success",
        remote_task_identity=fixture.intent.remote_task_identity,
        resources=resources,
        materialization_intent=fixture.intent,
        local_materialization_receipt=materialized.receipt,
    )
    cleanup_pending = advance_remote_parse_checkpoint_v4(
        published,
        state="cleanup_pending",
        held_resource_credit=published.held_resource_credit,
        cleanup_plan_sha256=plan.sha256,
    )
    replay = V4EvidenceReplayContext(
        evidence=tuple(
            encode_remote_parse_evidence_v4(value)
            for value in (
                *fixture.evidence_values,
                materialized.receipt,
                plan,
            )
        ),
        reservation=fixture.reservation,
        resourceful_checkpoint_history=(*fixture.history, local, published),
        cleanup_source_checkpoint=published,
        local_materialization_manifest=materialized.manifest,
        provider_envelope=materialized.provider_envelope,
    )
    return {
        "checkpoint": cleanup_pending,
        "source_checkpoint": published,
        "reservation": fixture.reservation,
        "intent": fixture.intent,
        "local_receipt": materialized.receipt,
        "plan": plan,
        "claim": _claim(cleanup_pending),
        "claim_guard": _Guard(),
        "replay_context": replay,
    }


def _no_receipt_local_failure_cleanup_arguments(
    *,
    fixture: _MaterializeFixture,
) -> tuple[dict[str, Any], FailureReceiptV4]:
    source = fixture.checkpoint
    failure = FailureReceiptV4(
        attempt_id=source.attempt_id,
        fence_identity=source.fence_identity,
        outcome="local_failure",
        source_state=source.state,
        source_lifecycle_version=source.lifecycle_version,
        source_checkpoint_sha256=source.sha256,
        submission_was_attempted=True,
        submission_absence_proof=None,
        accepted_submission_receipt_sha256=fixture.accepted.sha256,
        terminal_receipt_sha256=fixture.terminal.sha256,
        materialization_intent_sha256=fixture.intent.sha256,
        local_materialization_receipt_sha256=None,
        error_code="local_output_rejected",
        error_stage="local_materialization",
        error_class="ParserOutputContractError",
        retryable=True,
        retry_budget_class="bounded",
        message="test materialization failed before a local receipt",
    )
    resources = (
        CleanupResourceEntryV4(
            kind="snapshot",
            relpath=fixture.reservation.snapshot_relpath,
            ownership_basis_sha256=fixture.reservation.sha256,
            expected_sha256=fixture.reservation.source_pdf_sha256,
            expected_byte_count=fixture.reservation.source_byte_count,
            action="delete",
        ),
        CleanupResourceEntryV4(
            kind="spool",
            relpath=fixture.intent.spool_relpath,
            ownership_basis_sha256=fixture.intent.sha256,
            expected_sha256=fixture.intent.artifact_sha256,
            expected_byte_count=fixture.intent.artifact_byte_count,
            action="delete",
        ),
        *tuple(
            CleanupResourceEntryV4(
                kind=kind,
                relpath=getattr(fixture.intent, f"{kind}_relpath"),
                ownership_basis_sha256=fixture.intent.sha256,
                expected_sha256=None,
                expected_byte_count=None,
                action="delete",
            )
            for kind in (
                "spool_part",
                "spool_part_owner",
                "staging",
                "staging_marker",
            )
        ),
    )
    plan = build_local_cleanup_plan_v4(
        reservation=fixture.reservation,
        source_checkpoint=source,
        outcome="local_failure",
        resources=resources,
        materialization_intent=fixture.intent,
        remote_task_identity=fixture.intent.remote_task_identity,
        failure_receipt_sha256=failure.sha256,
    )
    cleanup_pending = advance_remote_parse_checkpoint_v4(
        source,
        state="cleanup_pending",
        held_resource_credit=source.held_resource_credit,
        failure_receipt_sha256=failure.sha256,
        cleanup_plan_sha256=plan.sha256,
    )
    replay = V4EvidenceReplayContext(
        evidence=tuple(
            encode_remote_parse_evidence_v4(value)
            for value in (*fixture.evidence_values, failure, plan)
        ),
        reservation=fixture.reservation,
        resourceful_checkpoint_history=fixture.history,
        cleanup_source_checkpoint=source,
    )
    return (
        {
            "checkpoint": cleanup_pending,
            "source_checkpoint": source,
            "reservation": fixture.reservation,
            "intent": fixture.intent,
            "local_receipt": None,
            "plan": plan,
            "claim": _claim(cleanup_pending),
            "claim_guard": _Guard(),
            "replay_context": replay,
        },
        failure,
    )


def _ack_no_receipt_local_failure(
    *,
    backend: MinerUHttpStagedV4,
    fixture: _MaterializeFixture,
    cleanup: dict[str, Any],
    failure: FailureReceiptV4,
    cleanup_receipt: Any,
) -> Any:
    cleanup_pending = cleanup["checkpoint"]
    if not isinstance(cleanup_pending, RemoteParseCheckpointV4):
        raise AssertionError("test cleanup checkpoint type drifted")
    ack_pending = advance_remote_parse_checkpoint_v4(
        cleanup_pending,
        state="ack_pending",
        held_resource_credit=ResourceCreditVector(
            documents=1,
            provider_tasks=1,
            provider_result_bytes=fixture.intent.artifact_byte_count,
            ack_items=1,
        ),
        cleanup_receipt_sha256=cleanup_receipt.sha256,
    )
    ack_replay = V4EvidenceReplayContext(
        evidence=tuple(
            encode_remote_parse_evidence_v4(value)
            for value in (
                *fixture.evidence_values,
                failure,
                cleanup["plan"],
                cleanup_receipt,
            )
        ),
        reservation=fixture.reservation,
        resourceful_checkpoint_history=fixture.history,
        cleanup_source_checkpoint=fixture.checkpoint,
        cleanup_pending_checkpoint=cleanup_pending,
        ack_pending_checkpoint=ack_pending,
    )
    command = seal_provider_ack_command_v4(
        ack_pending_checkpoint=ack_pending,
        accepted_submission=fixture.accepted,
        terminal_receipt=fixture.terminal,
        cleanup_plan=cleanup["plan"],
        cleanup_receipt=cleanup_receipt,
        replay_context=ack_replay,
    )
    return backend.acknowledge_v4(
        command=command,
        provider_capability=replace(
            fixture.capability,
            capability_purpose="result_acknowledgement",
        ),
        claim=_claim(ack_pending),
        claim_guard=_Guard(),
        stage_guard=_StepGuard(),
    )


def _local_failure_cleanup_arguments(
    *,
    fixture: _MaterializeFixture,
    materialized: Any,
) -> Any:
    local_credit = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=fixture.reservation.source_byte_count,
        provider_tasks=1,
        provider_result_bytes=fixture.intent.artifact_byte_count,
        compressed_bytes=fixture.intent.artifact_byte_count,
        output_items=1,
        output_bytes=materialized.receipt.output_byte_count,
        output_pages=fixture.reservation.source_page_count,
        ack_items=1,
    )
    local = advance_remote_parse_checkpoint_v4(
        fixture.checkpoint,
        state="local_materialized",
        held_resource_credit=local_credit,
        local_materialization_receipt_sha256=materialized.receipt.sha256,
    )
    failure = FailureReceiptV4(
        attempt_id=local.attempt_id,
        fence_identity=local.fence_identity,
        outcome="local_failure",
        source_state=local.state,
        source_lifecycle_version=local.lifecycle_version,
        source_checkpoint_sha256=local.sha256,
        submission_was_attempted=True,
        submission_absence_proof=None,
        accepted_submission_receipt_sha256=fixture.accepted.sha256,
        terminal_receipt_sha256=fixture.terminal.sha256,
        materialization_intent_sha256=fixture.intent.sha256,
        local_materialization_receipt_sha256=materialized.receipt.sha256,
        error_code="local_output_rejected",
        error_stage="local_materialization",
        error_class="ParserOutputContractError",
        retryable=True,
        retry_budget_class="bounded",
        message="test local materialization failure",
    )
    resources = (
        CleanupResourceEntryV4(
            kind="snapshot",
            relpath=fixture.reservation.snapshot_relpath,
            ownership_basis_sha256=fixture.reservation.sha256,
            expected_sha256=fixture.reservation.source_pdf_sha256,
            expected_byte_count=fixture.reservation.source_byte_count,
            action="delete",
        ),
        CleanupResourceEntryV4(
            kind="spool",
            relpath=fixture.intent.spool_relpath,
            ownership_basis_sha256=fixture.intent.sha256,
            expected_sha256=fixture.intent.artifact_sha256,
            expected_byte_count=fixture.intent.artifact_byte_count,
            action="delete",
        ),
        CleanupResourceEntryV4(
            kind="output",
            relpath=fixture.intent.output_relpath,
            ownership_basis_sha256=materialized.receipt.sha256,
            expected_sha256=materialized.receipt.output_files_sha256,
            expected_byte_count=materialized.receipt.output_byte_count,
            action="delete",
        ),
    )
    plan = build_local_cleanup_plan_v4(
        reservation=fixture.reservation,
        source_checkpoint=local,
        outcome="local_failure",
        remote_task_identity=fixture.intent.remote_task_identity,
        resources=resources,
        materialization_intent=fixture.intent,
        local_materialization_receipt=materialized.receipt,
        failure_receipt_sha256=failure.sha256,
    )
    cleanup_pending = advance_remote_parse_checkpoint_v4(
        local,
        state="cleanup_pending",
        held_resource_credit=local.held_resource_credit,
        failure_receipt_sha256=failure.sha256,
        cleanup_plan_sha256=plan.sha256,
    )
    replay = V4EvidenceReplayContext(
        evidence=tuple(
            encode_remote_parse_evidence_v4(value)
            for value in (
                *fixture.evidence_values,
                materialized.receipt,
                failure,
                plan,
            )
        ),
        reservation=fixture.reservation,
        resourceful_checkpoint_history=(*fixture.history, local),
        cleanup_source_checkpoint=local,
        local_materialization_manifest=materialized.manifest,
        provider_envelope=materialized.provider_envelope,
    )
    return {
        "checkpoint": cleanup_pending,
        "source_checkpoint": local,
        "reservation": fixture.reservation,
        "intent": fixture.intent,
        "local_receipt": materialized.receipt,
        "plan": plan,
        "claim": _claim(cleanup_pending),
        "claim_guard": _Guard(),
        "replay_context": replay,
    }


def _claim(checkpoint: RemoteParseCheckpointV4) -> V4ClaimWitness:
    return V4ClaimWitness(
        attempt_id=checkpoint.attempt_id,
        fence_identity=checkpoint.fence_identity,
        state=checkpoint.state,
        lifecycle_version=checkpoint.lifecycle_version,
        checkpoint_sha256=checkpoint.sha256,
        claim_owner_identity="worker-test",
        claim_generation=1,
    )


def _capability(fixture: Any, purpose: str) -> PrivateProviderCapabilityV4:
    accepted = fixture["accepted"]
    token = fixture["token"]
    return PrivateProviderCapabilityV4(
        attempt_id=accepted.attempt_id,
        remote_task_identity=accepted.remote_task_identity,
        provider_protocol_version=accepted.provider_protocol_version,
        secret_kind=accepted.secret_kind,
        secret_version=accepted.secret_version,
        capability_purpose=purpose,
        token_bytes=token,
        token_sha256=accepted.token_sha256,
        token_byte_count=accepted.token_byte_count,
    )


def _official_zip() -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _write_bundle(root)
        (root / "images" / "owner.jpg").write_bytes(b"\xff\xd8\xffowner-crop")
        (root / "images" / "continuation.jpg").write_bytes(
            b"\xff\xd8\xffcontinuation-crop"
        )
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        return output.getvalue()


def _symlink_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        info = zipfile.ZipInfo("unsafe-link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b"target")
    return output.getvalue()


def _nul_name_zip() -> bytes:
    safe_name = b"unsafe_00tail.bin"
    nul_name = b"unsafe\x0000tail.bin"
    if len(safe_name) != len(nul_name):
        raise AssertionError("NUL ZIP fixture names must be byte-length stable")
    raw = _zip_entries(((safe_name.decode("ascii"), b"payload"),))
    if raw.count(safe_name) != 2:
        raise AssertionError("NUL ZIP fixture expected local and central names")
    return raw.replace(safe_name, nul_name)


def _zip_entries(entries: tuple[tuple[str, bytes], ...]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return output.getvalue()


def _replace_zip_suffix(archive_bytes: bytes, suffix: str, replacement: bytes) -> bytes:
    source = io.BytesIO(archive_bytes)
    output = io.BytesIO()
    replaced = False
    with zipfile.ZipFile(source, "r") as source_archive:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target_archive:
            for info in source_archive.infolist():
                value = source_archive.read(info)
                if info.filename.endswith(suffix):
                    value = replacement
                    replaced = True
                target_archive.writestr(info, value)
    if not replaced:
        raise AssertionError(f"missing ZIP fixture suffix: {suffix}")
    return output.getvalue()


def _drop_zip_suffix(archive_bytes: bytes, suffix: str) -> bytes:
    source = io.BytesIO(archive_bytes)
    output = io.BytesIO()
    removed = False
    with zipfile.ZipFile(source, "r") as source_archive:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target_archive:
            for info in source_archive.infolist():
                if info.filename.endswith(suffix):
                    removed = True
                    continue
                target_archive.writestr(info, source_archive.read(info))
    if not removed:
        raise AssertionError(f"missing ZIP fixture suffix: {suffix}")
    return output.getvalue()


def _corrupt_zip_member(archive_bytes: bytes, suffix: str) -> bytes:
    corrupted = bytearray(archive_bytes)
    with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
        info = next(
            item for item in archive.infolist() if item.filename.endswith(suffix)
        )
        offset = info.header_offset
        name_bytes = int.from_bytes(corrupted[offset + 26 : offset + 28], "little")
        extra_bytes = int.from_bytes(corrupted[offset + 28 : offset + 30], "little")
        payload_start = offset + 30 + name_bytes + extra_bytes
        if info.compress_size < 1:
            raise AssertionError("ZIP fixture member has no payload")
        corrupted[payload_start + info.compress_size // 2] ^= 0x01
    return bytes(corrupted)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
