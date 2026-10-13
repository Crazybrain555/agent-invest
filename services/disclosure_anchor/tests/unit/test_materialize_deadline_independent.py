"""Independent v5 pre-read envelope and LOCAL logical-deadline witnesses."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest import mock

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import MinerUHttpStagedV4
from disclosure_anchor.application.ports.staged_provider_parser import MaterializationCapacityBlockedV4
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseGuard, StageLeaseLost
from tests.unit.test_mineru_http_staged_v4 import _Guard, _Transport, _official_zip, _published_test_root
from tests.unit.test_mineru_materialize_grant_v5 import (
    _archive_facts, _v5_fixture, STORAGE_POLICY,
)

MIB = 1024 * 1024


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class MaterializeDeadlineIndependentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.archive = _official_zip()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "scratch"
        self.root.mkdir(mode=0o700)
        self.clock = _Clock()
        self.transport = _Transport(self.archive)
        self.backend = MinerUHttpStagedV4(
            scratch_root=self.root,
            published_root=_published_test_root(self.root),
            transport=self.transport,
            clock=lambda: 1.0,
            monotonic=self.clock,
            storage_policy=STORAGE_POLICY,
        )

    def _guard(self) -> StageLeaseGuard:
        return StageLeaseGuard(
            deadline_monotonic=30.0,
            claim_deadline_monotonic=60.0,
            _revoked=Event(),
            _monotonic=self.clock,
        )

    def _call(self, fixture, guard: StageLeaseGuard):
        return self.backend.materialize_v4(
            **{**fixture.arguments(), "stage_guard": guard},
            claim_guard=_Guard(),
        )

    def _assert_original_zip(self, fixture) -> None:
        spool = self.root / fixture.intent.spool_relpath
        self.assertEqual(spool.read_bytes(), self.archive)
        self.assertEqual(
            "sha256:" + hashlib.sha256(spool.read_bytes()).hexdigest(),
            fixture.terminal.artifact_sha256,
        )

    def test_j_plus_one_holds_before_reader_and_keeps_original_zip(self) -> None:
        _, _, decode_input = _archive_facts(self.archive)
        fixture = _v5_fixture(
            self.archive, working_set=4 * MIB, input_limit=decode_input - 1,
        )
        with mock.patch.object(
            self.backend._reader, "read_pinned",
            wraps=self.backend._reader.read_pinned,
        ) as reader:
            with self.assertRaises(MaterializationCapacityBlockedV4) as raised:
                self._call(fixture, self._guard())
        self.assertEqual(raised.exception.dimension, "decode_input_bytes")
        reader.assert_not_called()
        self._assert_original_zip(fixture)
        self.assertTrue((self.root / fixture.intent.staging_relpath).is_dir())
        self.assertFalse((self.root / fixture.intent.output_relpath).exists())

    def test_live_local_deadline_allows_one_reader_and_exact_output(self) -> None:
        fixture = _v5_fixture(self.archive, working_set=4 * MIB, input_limit=MIB)
        stage_guard = self._guard()
        with mock.patch.object(
            self.backend._reader, "read_pinned",
            wraps=self.backend._reader.read_pinned,
        ) as reader:
            result = self._call(fixture, stage_guard)
        self.assertEqual(reader.call_count, 1)
        self._assert_original_zip(fixture)
        self.assertTrue((self.root / fixture.intent.output_relpath).is_dir())
        self.assertGreater(result.receipt.output_byte_count, 0)
        stage_guard.checkpoint()

    def test_reader_crossing_logical_deadline_cannot_write_or_promote_output(self) -> None:
        fixture = _v5_fixture(self.archive, working_set=4 * MIB, input_limit=MIB)
        stage_guard = self._guard()
        original_read = self.backend._reader.read_pinned

        def expire_after_real_read(tree, *, source_pdf_sha256):
            result = original_read(tree, source_pdf_sha256=source_pdf_sha256)
            self.clock.now = 31.0  # logical 30s spent; claim fence still live to 60s
            return result

        failure: Exception | None = None
        with mock.patch.object(
            self.backend._reader, "read_pinned", side_effect=expire_after_real_read,
        ) as reader:
            try:
                self._call(fixture, stage_guard)
            except Exception as exc:
                failure = exc
        self.assertEqual(reader.call_count, 1)
        self._assert_original_zip(fixture)
        staging = self.root / fixture.intent.staging_relpath
        output = self.root / fixture.intent.output_relpath
        postdeadline_effects = (
            output,
            staging / fixture.intent.provider_envelope_relpath,
            staging / fixture.intent.output_manifest_relpath,
            output / fixture.intent.provider_envelope_relpath,
            output / fixture.intent.output_manifest_relpath,
        )
        self.assertEqual(
            tuple(path for path in postdeadline_effects if path.exists()), (),
            "expired LOCAL stage wrote or promoted output after reader returned",
        )
        self.assertIsInstance(failure, StageLeaseLost)
        self.assertEqual(failure.provenance, "deadline_exhausted")
