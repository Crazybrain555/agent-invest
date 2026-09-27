"""Independent synthetic v5 unpack continuation and ownership checks."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile
import zlib

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import MinerUHttpStagedV4
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializationUnpackContinuesV4,
    V4ResourceOwnershipError,
)
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseLost
from tests.unit.test_mineru_http_staged_v4 import (
    _Guard, _StepGuard, _Transport, _official_zip, _published_test_root,
)
from tests.unit.test_mineru_materialize_grant_v5 import _v5_fixture, STORAGE_POLICY

MIB = 1024 * 1024


class _SyntheticHardStop(BaseException):
    """Model a process interruption after a durable partial-member write."""


class _StopAfterFirstMember(_StepGuard):
    def __init__(self, first_member: Path) -> None:
        super().__init__()
        self.first_member = first_member

    def remaining_seconds(self) -> float:
        self.checkpoint()
        return 9.0 if self.first_member.exists() else 60.0


def _same_crc_mutation(original: bytes) -> bytes:
    """Change content but solve a four-byte suffix for the same ZIP CRC32."""
    changed = bytearray(original)
    changed[0] ^= 1
    base = zlib.crc32(changed)
    basis: dict[int, tuple[int, int]] = {}
    for bit in range(32):
        candidate = bytearray(changed)
        candidate[-4 + bit // 8] ^= 1 << (bit % 8)
        effect, mask = zlib.crc32(candidate) ^ base, 1 << bit
        while effect:
            pivot = effect.bit_length() - 1
            if pivot not in basis:
                basis[pivot] = effect, mask
                break
            effect ^= basis[pivot][0]
            mask ^= basis[pivot][1]
    delta, bits = base ^ zlib.crc32(original), 0
    while delta:
        effect, mask = basis[delta.bit_length() - 1]
        delta ^= effect
        bits ^= mask
    for bit in range(32):
        if bits & (1 << bit):
            changed[-4 + bit // 8] ^= 1 << (bit % 8)
    result = bytes(changed)
    assert result != original and len(result) == len(original)
    assert zlib.crc32(result) == zlib.crc32(original)
    return result


class MemberUnpackContinuationIndependentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.archive = _official_zip()
        self.fixture = _v5_fixture(self.archive, working_set=4 * MIB, input_limit=MIB)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "scratch"
        self.root.mkdir(mode=0o700)
        self.transport = _Transport(self.archive)
        self.published = _published_test_root(self.root)

    def _backend(self) -> MinerUHttpStagedV4:
        return MinerUHttpStagedV4(
            scratch_root=self.root, published_root=self.published,
            transport=self.transport, clock=lambda: 1.0,
            storage_policy=STORAGE_POLICY,
        )

    def _call(self, backend: MinerUHttpStagedV4, *, stage_guard=None):
        arguments = self.fixture.arguments()
        if stage_guard is not None:
            arguments["stage_guard"] = stage_guard
        return backend.materialize_v4(**arguments, claim_guard=_Guard())

    def _paths(self):
        staging = self.root / self.fixture.intent.staging_relpath
        return (
            self.root / self.fixture.intent.spool_relpath,
            staging,
            staging / ".unpack" / "images" / "continuation.jpg",
            staging / ".unpack-partial" / "member",
            self.root / self.fixture.intent.output_relpath,
        )

    def _assert_original_spool(self) -> None:
        spool = self._paths()[0]
        self.assertEqual(spool.read_bytes(), self.archive)
        self.assertEqual(
            "sha256:" + hashlib.sha256(spool.read_bytes()).hexdigest(),
            self.fixture.terminal.artifact_sha256,
        )

    def _assert_exact_output(self) -> None:
        output = self._paths()[4]
        with zipfile.ZipFile(io.BytesIO(self.archive)) as zipped:
            source_names = {info.filename for info in zipped.infolist() if not info.is_dir()}
            for name in source_names:
                self.assertEqual((output / name).read_bytes(), zipped.read(name))
        actual = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
        self.assertEqual(actual, source_names | {
            self.fixture.intent.provider_envelope_relpath,
            self.fixture.intent.output_manifest_relpath,
        })

    def test_hard_stop_mid_second_member_reuses_completed_first_and_rewrites_partial(self) -> None:
        backend = self._backend()
        original = backend._write_all
        with zipfile.ZipFile(io.BytesIO(self.archive)) as zipped:
            interrupted_member = zipped.read("images/owner.jpg")
        interrupted = False

        def interrupt_second_member(fd: int, data: bytes) -> None:
            nonlocal interrupted
            if not interrupted and data == interrupted_member:
                interrupted = True
                original(fd, data[:4])
                os.fsync(fd)
                raise _SyntheticHardStop()
            original(fd, data)

        with mock.patch.object(backend, "_write_all", side_effect=interrupt_second_member):
            with self.assertRaises(_SyntheticHardStop):
                self._call(backend)
        self.assertTrue(interrupted)
        spool, staging, first, partial, output = self._paths()
        self._assert_original_spool()
        self.assertTrue(staging.is_dir())
        self.assertFalse(output.exists())
        self.assertEqual(first.read_bytes(), b"\xff\xd8\xffcontinuation-crop")
        self.assertEqual(partial.stat().st_size, 4)
        first_identity = (first.stat().st_dev, first.stat().st_ino)
        self.assertEqual(self.transport.downloads, 1)

        materialized = self._call(self._backend())
        self.assertEqual(self.transport.downloads, 1, "sealed ZIP was downloaded again")
        self._assert_original_spool()
        self._assert_exact_output()
        promoted_first = output / "images" / "continuation.jpg"
        self.assertEqual((promoted_first.stat().st_dev, promoted_first.stat().st_ino), first_identity)
        self.assertFalse(partial.exists())
        self.assertGreater(materialized.receipt.output_byte_count, 0)

    def test_tampered_completed_member_or_marker_is_not_adopted(self) -> None:
        for mutation in ("member", "marker"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                previous = (self.root, self.published, self.transport)
                self.root = Path(directory) / "scratch"
                self.root.mkdir(mode=0o700)
                self.published = _published_test_root(self.root)
                self.transport = _Transport(self.archive)
                try:
                    first = self._paths()[2]
                    with self.assertRaises(MaterializationUnpackContinuesV4) as continued:
                        self._call(self._backend(), stage_guard=_StopAfterFirstMember(first))
                    self.assertEqual(continued.exception.completed_members, 1)
                    target = first if mutation == "member" else (
                        self._paths()[1] / Path(self.fixture.intent.staging_marker_relpath).name
                    )
                    with target.open("r+b") as file:
                        file.seek(0)
                        first_byte = file.read(1)
                        file.seek(0)
                        file.write(b"X" if first_byte != b"X" else b"Y")
                    with self.assertRaises(V4ResourceOwnershipError):
                        self._call(self._backend())
                    self._assert_original_spool()
                    self.assertTrue(target.exists())
                    self.assertFalse(self._paths()[4].exists())
                    self.assertEqual(self.transport.downloads, 1)
                finally:
                    self.root, self.published, self.transport = previous

    def test_claim_or_logical_loss_at_member_boundary_retains_materials(self) -> None:
        for provenance in ("ownership_lost", "deadline_exhausted"):
            with self.subTest(provenance=provenance), tempfile.TemporaryDirectory() as directory:
                previous = (self.root, self.published, self.transport)
                self.root = Path(directory) / "scratch"
                self.root.mkdir(mode=0o700)
                self.published = _published_test_root(self.root)
                self.transport = _Transport(self.archive)
                try:
                    first = self._paths()[2]

                    class LoseAfterFirst(_StepGuard):
                        def remaining_seconds(self_inner) -> float:
                            if first.exists():
                                raise StageLeaseLost(provenance=provenance)
                            return 60.0

                    with self.assertRaises(StageLeaseLost) as lost:
                        self._call(self._backend(), stage_guard=LoseAfterFirst())
                    self.assertEqual(lost.exception.provenance, provenance)
                    self._assert_original_spool()
                    self.assertTrue(first.exists())
                    self.assertFalse(self._paths()[4].exists())
                    first_identity = (first.stat().st_dev, first.stat().st_ino)
                    self._call(self._backend())
                    self._assert_exact_output()
                    promoted = self._paths()[4] / "images" / "continuation.jpg"
                    self.assertEqual((promoted.stat().st_dev, promoted.stat().st_ino), first_identity)
                    self.assertEqual(self.transport.downloads, 1)
                finally:
                    self.root, self.published, self.transport = previous

    def test_same_size_crc_colliding_completed_member_is_not_adopted(self) -> None:
        # notes.bin is parser evidence but has no image magic check; a later
        # semantic validator must not mask a weak completed-member proof.
        first = self._paths()[1] / ".unpack" / "notes.bin"
        with self.assertRaises(MaterializationUnpackContinuesV4):
            self._call(self._backend(), stage_guard=_StopAfterFirstMember(first))
        with zipfile.ZipFile(io.BytesIO(self.archive)) as zipped:
            original = zipped.read("notes.bin")
        self.assertEqual(first.read_bytes(), original)
        forged = _same_crc_mutation(original)
        first.write_bytes(forged)  # same inode, length and CRC, different SHA/content
        self.assertEqual(first.stat().st_size, len(original))
        self.assertEqual(zlib.crc32(first.read_bytes()), zlib.crc32(original))
        with self.assertRaises(V4ResourceOwnershipError):
            self._call(self._backend())
        self._assert_original_spool()
        self.assertTrue(first.exists())
        self.assertFalse(self._paths()[4].exists())
