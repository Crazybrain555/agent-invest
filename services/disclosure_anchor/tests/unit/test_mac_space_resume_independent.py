"""Physical free-space checks must not re-reserve already completed output."""
from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import MinerUHttpStagedV4
from disclosure_anchor.application.ports.staged_provider_parser import MaterializationCapacityWaitV4
from tests.unit.test_mineru_http_staged_v4 import _Guard, _Transport, _official_zip, _published_test_root
from tests.unit.test_mineru_materialize_grant_v5 import _v5_fixture, STORAGE_POLICY


class MacSpaceResumeIndependentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.archive = _official_zip()
        self.fixture = _v5_fixture(self.archive, working_set=4 * 1024**2, input_limit=1024**2)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "scratch"
        self.root.mkdir(mode=0o700)
        self.published = _published_test_root(self.root)
        self.transport = _Transport(self.archive)

    def _backend(self) -> MinerUHttpStagedV4:
        return MinerUHttpStagedV4(
            scratch_root=self.root, published_root=self.published,
            transport=self.transport, clock=lambda: 1.0, storage_policy=STORAGE_POLICY,
        )

    def _usage(self, available: int):
        return mock.patch(
            "disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4.os.statvfs",
            return_value=SimpleNamespace(
                f_blocks=STORAGE_POLICY.mac_volume_total_bytes,
                f_frsize=1, f_bavail=available,
            ),
        )

    def _call(self, backend: MinerUHttpStagedV4):
        return backend.materialize_v4(**self.fixture.arguments(), claim_guard=_Guard())

    def test_fresh_grant_waits_before_transfer_if_free_is_one_byte_short(self) -> None:
        limit = self.fixture.intent.resource_grant.limits.temp_disk_bytes
        with self._usage(STORAGE_POLICY.mac_free_floor_bytes + limit - 1):
            with self.assertRaises(MaterializationCapacityWaitV4):
                self._call(self._backend())
        self.assertEqual(self.transport.downloads, 0)
        self.assertFalse((self.root / self.fixture.intent.output_relpath).exists())

    def test_promoted_output_replay_needs_no_second_full_workspace_reservation(self) -> None:
        limit = self.fixture.intent.resource_grant.limits.temp_disk_bytes
        with self._usage(STORAGE_POLICY.mac_free_floor_bytes + limit):
            original = self._call(self._backend())
        output = self.root / self.fixture.intent.output_relpath
        before = {p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob("*") if p.is_file()}
        # A later unrelated consumer occupies space; this attempt already has
        # complete, verified output and must only recover its missing receipt.
        with self._usage(STORAGE_POLICY.mac_free_floor_bytes):
            recovered = self._call(self._backend())
        self.assertEqual(recovered.receipt, original.receipt)
        self.assertEqual(self.transport.downloads, 1)
        self.assertEqual(before, {p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob("*") if p.is_file()})
