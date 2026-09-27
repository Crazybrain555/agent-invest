"""Member-level unpack continuation of a granted (v5) result.

The official synthetic MinerU bundle, in-memory V4 evidence and a stage guard
whose budget the test lowers after the first published member. No network,
provider or database.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest import mock
import zipfile

from disclosure_anchor.adapters.parsers.mineru_medium import http_staged_v4
from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import MinerUHttpStagedV4
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializationUnpackContinuesV4,
    V4ResourceOwnershipError,
)
from tests.unit.test_mineru_http_staged_v4 import _Guard, _official_zip, _published_test_root, _Transport
from tests.unit.test_mineru_materialize_grant_v5 import MIB, STORAGE_POLICY, _v5_fixture


class _Budget:
    """A stage guard with a settable remaining budget."""

    def __init__(self) -> None:
        self.remaining = 3_600.0

    def remaining_seconds(self) -> float:
        return self.remaining

    def checkpoint(self) -> None:
        pass

    def note(self, kind: str, **scalars: object) -> None:
        del kind, scalars


class ResumableUnpackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.archive = _official_zip()
        self.fixture = _v5_fixture(self.archive, working_set=4 * MIB, input_limit=MIB)

    def scratch(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "scratch"
        root.mkdir(mode=0o700)
        return root

    def materialize(self, root: Path, budget: _Budget) -> Any:
        backend = MinerUHttpStagedV4(
            scratch_root=root, published_root=_published_test_root(root),
            transport=_Transport(self.archive), clock=lambda: 1.0, storage_policy=STORAGE_POLICY,
        )
        arguments = self.fixture.arguments()
        arguments["stage_guard"] = budget
        return backend.materialize_v4(**arguments, claim_guard=_Guard())

    def stop_after_first_member(self, root: Path) -> MaterializationUnpackContinuesV4:
        budget = _Budget()
        original = MinerUHttpStagedV4._exclusive_rename

        def publish(self: MinerUHttpStagedV4, source: Path, target: Path, **kwargs: Any) -> None:
            original(self, source, target, **kwargs)
            if ".unpack" in target.parts:
                budget.remaining = 5.0  # The next member boundary is inside the stop margin.

        with mock.patch.object(MinerUHttpStagedV4, "_exclusive_rename", publish):
            with self.assertRaises(MaterializationUnpackContinuesV4) as stopped:
                self.materialize(root, budget)
        return stopped.exception

    def staged_members(self, root: Path) -> set[str]:
        unpack = root / self.fixture.intent.staging_relpath / ".unpack"
        return {path.relative_to(unpack).as_posix() for path in unpack.rglob("*") if path.is_file()}

    def test_stop_keeps_published_members_and_resume_extracts_only_the_rest(self) -> None:
        root = self.scratch()
        stopped = self.stop_after_first_member(root)
        self.assertEqual(stopped.completed_members, 1)
        first = self.staged_members(root)
        self.assertEqual(len(first), 1)
        partial = root / self.fixture.intent.staging_relpath / http_staged_v4._UNPACK_PARTIAL_DIRNAME
        # At a member boundary nothing is in flight; only the size/SHA-256
        # records of the published members remain beside it.
        self.assertEqual([path.name for path in partial.iterdir()], [http_staged_v4._UNPACK_JOURNAL_NAME])

        opened: list[str] = []
        original_open = zipfile.ZipFile.open

        def counted(archive: zipfile.ZipFile, name: Any, *args: Any, **kwargs: Any) -> Any:
            opened.append(name.filename if isinstance(name, zipfile.ZipInfo) else name)
            return original_open(archive, name, *args, **kwargs)

        with mock.patch.object(zipfile.ZipFile, "open", counted):
            resumed = self.materialize(root, _Budget())
        self.assertFalse(first & {name.rstrip("/") for name in opened})
        fresh = self.materialize(self.scratch(), _Budget())
        self.assertEqual(resumed.provider_envelope, fresh.provider_envelope)
        self.assertEqual(resumed.manifest, fresh.manifest)
        self.assertEqual(
            (resumed.receipt.member_count, resumed.receipt.uncompressed_byte_count, resumed.receipt.output_byte_count),
            (fresh.receipt.member_count, fresh.receipt.uncompressed_byte_count, fresh.receipt.output_byte_count),
        )
        self.assertFalse(partial.exists())

    def test_in_flight_leftover_is_redone_and_a_tampered_member_is_never_adopted(self) -> None:
        root = self.scratch()
        self.stop_after_first_member(root)
        staging = root / self.fixture.intent.staging_relpath
        leftover = staging / http_staged_v4._UNPACK_PARTIAL_DIRNAME / http_staged_v4._UNPACK_PARTIAL_NAME
        leftover.write_bytes(b"torn member")
        leftover.chmod(0o600)
        self.materialize(root, _Budget())
        self.assertFalse(leftover.exists())

        other = self.scratch()
        self.stop_after_first_member(other)
        (published,) = [path for path in (other / self.fixture.intent.staging_relpath / ".unpack").rglob("*")
                        if path.is_file()]
        published.write_bytes(bytes(reversed(published.read_bytes())))  # Same size, other content.
        outcomes: list[object] = []
        original_resume = MinerUHttpStagedV4._resume_partial_unpack

        def recording(self: MinerUHttpStagedV4, **kwargs: Any) -> Any:
            outcome = original_resume(self, **kwargs)
            outcomes.append(outcome)
            return outcome

        tampered = published.read_bytes()
        with mock.patch.object(MinerUHttpStagedV4, "_resume_partial_unpack", recording):
            with self.assertRaises(V4ResourceOwnershipError):
                self.materialize(other, _Budget())
        # Never adopted: the unchanged classifier retains the ambiguous
        # staging, bytes intact, for an operator instead of redoing it.
        self.assertEqual(outcomes, [None])
        self.assertEqual(published.read_bytes(), tampered)


if __name__ == "__main__":
    unittest.main()
