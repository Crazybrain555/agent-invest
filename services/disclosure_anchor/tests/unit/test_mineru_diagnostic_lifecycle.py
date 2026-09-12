"""Independent executable E1 tests from the accepted phase/ownership contract."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium import MinerUMediumArtifactReader
from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import canonical_result_owner_v2
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.domain.errors import ParserOutputContractError
from tests._mineru_diagnostic_lifecycle_fixture import (
    CLOCK_SHA, LifecycleFixture, SimulatedCrash, crash_after_phase, journal_records,
)


class MinerUDiagnosticLifecycleTests(unittest.TestCase):
    def fixture(self, **kwargs: object) -> LifecycleFixture:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return LifecycleFixture(Path(temporary.name), **kwargs)

    def assert_no_ack(self, fixture: LifecycleFixture) -> None:
        self.assertFalse(any(path.endswith("/ack") for _, path in fixture.events))
        self.assertEqual(fixture.ack_effects, 0)

    def assert_closed_resources(self, fixture: LifecycleFixture) -> None:
        self.assertFalse((fixture.journal / "resources").exists())
        self.assertFalse((fixture.journal / "resources-reclaim").exists())
        self.assertEqual(fixture.source.read_bytes(), fixture.source_bytes)
        self.assertTrue(all(stream.closed for stream in fixture.streams))

    def test_complete_wire_archive_reader_cleanup_and_actual_ack(self) -> None:
        fixture = self.fixture()
        receipt = fixture.run()
        self.assertEqual(receipt["outcome"], "completed")
        self.assertEqual(receipt["provider"]["page_count"], 2)
        self.assertEqual(receipt["provider"]["block_count"], 3)
        self.assertEqual(fixture.events.count(("POST", "/tasks")), 1)
        self.assertEqual(fixture.ack_effects, 1)
        self.assertIn(fixture.source_bytes, fixture.uploaded_bodies[0])
        self.assertIn((fixture.source_sha.replace(":", "_") + ".pdf").encode(), fixture.uploaded_bodies[0])
        self.assert_closed_resources(fixture)

    def test_verified_failed_terminal_can_cleanup_ack_without_result_download(self) -> None:
        fixture = self.fixture()
        fixture.terminal_status = "failed"
        receipt = fixture.run()
        self.assertEqual(receipt["outcome"], "failed")
        self.assertIsNone(receipt["provider"])
        self.assertFalse(any(path.endswith(("/result", "/lease")) for _, path in fixture.events))
        self.assertEqual(fixture.ack_effects, 1)
        self.assert_closed_resources(fixture)

    def test_lost_post_and_404_preserve_obligation_until_same_key_materializes(self) -> None:
        fixture = self.fixture()
        fixture.lose_submit_response = True
        with self.assertRaises(httpx.ReadError):
            fixture.run()
        key = fixture.fields["agent_idempotency_key"]
        fixture.lookup_absent = True
        with self.assertRaises((ValueError, RuntimeError)):
            fixture.run(resume=True)
        self.assert_no_ack(fixture)
        self.assertTrue((fixture.journal / "resources" / "source.pdf").is_file())
        fixture.lookup_absent = False
        receipt = fixture.run(resume=True)
        self.assertEqual(receipt["outcome"], "completed")
        self.assertEqual(fixture.events.count(("POST", "/tasks")), 1)
        lookups = [path for method, path in fixture.events if method == "GET" and "/by-idempotency/" in path]
        self.assertTrue(lookups)
        self.assertEqual(set(lookups), {"/tasks/by-idempotency/" + key})

    def test_false_resume_flag_cannot_repost_existing_attempt(self) -> None:
        fixture = self.fixture()
        fixture.lose_submit_response = True
        with self.assertRaises(httpx.ReadError):
            fixture.run()
        before = len(fixture.events)
        with self.assertRaises((FileExistsError, ValueError)):
            fixture.run()
        self.assertEqual(len(fixture.events), before)
        self.assertEqual(fixture.events.count(("POST", "/tasks")), 1)

    def test_resume_rejects_changed_original_binding_before_network(self) -> None:
        cases = {
            "attempt": {"attempt_identity": "foreign-attempt"},
            "fence": {"fence_identity": "foreign-fence"},
            "epoch": {"submission_epoch_unix": 1_001},
            "clock": {"clock_identity_sha256": "sha256:" + "d" * 64},
            "deadline": {"deadline_ns": 71_000_000_000},
            "source": {"source_pdf_sha256": "sha256:" + "e" * 64},
            "bytes": {"source_byte_count": 8},
            "pages": {"source_page_count": 3},
            "api": {"api_url": "http://foreign.invalid"},
            "server": {"server_url": "http://foreign.invalid/v1"},
            "runtime": {"options": ParserOptions(runtime_bundle_identity_sha256="sha256:" + "f" * 64,
                                                 timeout_seconds=60)},
        }
        for name, override in cases.items():
            with self.subTest(binding=name):
                fixture = self.fixture()
                fixture.lose_submit_response = True
                with self.assertRaises(httpx.ReadError):
                    fixture.run()
                before = len(fixture.events)
                preserved = (fixture.journal / "resources" / "source.pdf").read_bytes()
                with self.assertRaises((ValueError, RuntimeError)):
                    fixture.run(resume=True, **override)
                self.assertEqual(len(fixture.events), before)
                self.assertEqual((fixture.journal / "resources" / "source.pdf").read_bytes(), preserved)

    def test_resume_original_deadline_and_clock_regression_are_not_reset(self) -> None:
        for now in (70_000_000_000, 9_999_999_999):
            with self.subTest(now=now):
                fixture = self.fixture()
                fixture.lose_submit_response = True
                with self.assertRaises(httpx.ReadError):
                    fixture.run()
                fixture.now_ns = now
                before = len(fixture.events)
                with self.assertRaises((TimeoutError, ValueError)):
                    fixture.run(resume=True, clock_identity_sha256=CLOCK_SHA)
                self.assertEqual(len(fixture.events), before)
                self.assert_no_ack(fixture)

    def test_input_bytes_and_full_page_identity_fail_before_disposal(self) -> None:
        for change in ({"source_byte_count": 1}, {"source_pdf_sha256": "sha256:" + "a" * 64},
                       {"source_page_count": 3}):
            with self.subTest(change=change):
                fixture = self.fixture()
                with self.assertRaises((ValueError, RuntimeError)):
                    fixture.run(**change)
                self.assert_no_ack(fixture)
                self.assertEqual(fixture.source.read_bytes(), fixture.source_bytes)

    def test_wrong_protocol_identity_routes_and_artifact_receipts_never_ack(self) -> None:
        mutations = (
            ("fence_identity", "foreign-fence"),
            ("status_url", "http://foreign.invalid/tasks/independent-task-1"),
            ("status_url", "http://mineru.invalid/tasks/some-other-task"),
            ("task_protocol_schema", "mineru-task-protocol.v1"),
            ("unknown_field", True),
        )
        for name, value in mutations:
            with self.subTest(field=name, value=value):
                fixture = self.fixture()
                fixture.response_mutator = lambda payload, name=name, value=value: payload.update({name: value})
                with self.assertRaises((ValueError, RuntimeError)):
                    fixture.run()
                self.assert_no_ack(fixture)
                self.assertTrue((fixture.journal / "resources").is_dir())

    def test_download_hash_owner_encoding_and_expired_lease_fail_without_ack(self) -> None:
        for headers, expiry in (({"x-mineru-result-sha256": "0" * 64}, 2_000),
                                ({"x-mineru-result-owner": "1" * 64}, 2_000),
                                ({"content-encoding": "gzip"}, 2_000), ({}, 999)):
            with self.subTest(headers=headers, expiry=expiry):
                fixture = self.fixture()
                fixture.download_headers = headers
                fixture.lease_expiry = expiry
                with self.assertRaises((ValueError, RuntimeError)):
                    fixture.run()
                self.assert_no_ack(fixture)
                self.assertTrue(all(stream.closed for stream in fixture.streams))

    def test_unsafe_zip_cannot_escape_or_create_disposal_authority(self) -> None:
        fixture = self.fixture(unsafe_member="../outside-sentinel")
        with self.assertRaises((ValueError, RuntimeError, ParserOutputContractError)):
            fixture.run()
        self.assert_no_ack(fixture)
        self.assertFalse((fixture.journal / "resources" / "outside-sentinel").exists())
        self.assertFalse((fixture.root / "outside-sentinel").exists())

    def test_download_deadline_check_stops_later_chunks_and_closes_response(self) -> None:
        fixture = self.fixture()

        def expire_after_chunk(index: int) -> None:
            if index:
                fixture.now_ns = fixture.deadline_ns

        fixture.download_checkpoint = expire_after_chunk
        with self.assertRaises(TimeoutError):
            fixture.run()
        self.assert_no_ack(fixture)
        self.assertTrue(all(stream.closed for stream in fixture.streams))
        self.assertTrue((fixture.journal / "resources" / "result.zip").exists())

    def test_actual_download_byte_count_must_match_terminal_reservation(self) -> None:
        fixture = self.fixture()

        def overstate(payload: dict[str, object]) -> None:
            if payload["status"] == "completed":
                payload["result_artifact_bytes"] = len(fixture.archive) + 1
                payload["result_artifact_owner"] = canonical_result_owner_v2(
                    task_id=fixture.task_id, artifact_sha256=fixture.archive_sha,
                    artifact_byte_count=len(fixture.archive) + 1,
                )

        fixture.response_mutator = overstate
        fixture.download_headers["x-mineru-result-owner"] = canonical_result_owner_v2(
            task_id=fixture.task_id, artifact_sha256=fixture.archive_sha,
            artifact_byte_count=len(fixture.archive) + 1,
        )
        with self.assertRaises((ValueError, RuntimeError)):
            fixture.run()
        self.assert_no_ack(fixture)
        self.assertNotIn("archive_sealed", [record["step"] for record in journal_records(fixture.journal)])

    def test_artifact_reader_failure_retains_sealed_inputs_without_validation_or_ack(self) -> None:
        fixture = self.fixture()
        with patch.object(MinerUMediumArtifactReader, "read_with_location",
                          side_effect=ParserOutputContractError("synthetic invalid provider evidence")):
            with self.assertRaises(ParserOutputContractError):
                fixture.run()
        self.assert_no_ack(fixture)
        self.assertTrue((fixture.journal / "resources" / "result.zip").is_file())
        steps = [record["step"] for record in journal_records(fixture.journal)]
        self.assertIn("output_sealed", steps)
        self.assertNotIn("validated", steps)

    def test_many_pending_polls_do_not_exhaust_bounded_journal(self) -> None:
        fixture = self.fixture()
        fixture.pending_polls = 120
        receipt = fixture.run()
        self.assertEqual(receipt["outcome"], "completed")
        self.assertLess(len(journal_records(fixture.journal)), 96)
        self.assertEqual(fixture.events.count(("GET", "/tasks/" + fixture.task_id)), 121)

    def test_large_valid_raw_evidence_and_oversize_response_have_distinct_outcomes(self) -> None:
        for length, fits in ((1024 * 1024 - 2048, True), (1024 * 1024, False)):
            with self.subTest(response_padding=length):
                fixture = self.fixture()
                fixture.task_response_padding = length
                if fits:
                    receipt = fixture.run()
                    self.assertEqual(receipt["outcome"], "completed")
                    self.assertTrue(any(len(raw) > 1_000_000 for _, raw in fixture.responses))
                else:
                    with self.assertRaises(ValueError):
                        fixture.run()
                    self.assert_no_ack(fixture)
                    self.assertFalse(any(path.endswith("/result") for _, path in fixture.events))
                self.assertTrue(all(stream.closed for stream in fixture.streams))

    def test_sealed_phase_resume_does_not_repeat_completed_io(self) -> None:
        for phase in ("accepted", "terminal", "archive_sealed", "output_sealed", "validated"):
            with self.subTest(phase=phase):
                fixture = self.fixture()
                with crash_after_phase(phase), self.assertRaises(SimulatedCrash):
                    fixture.run()
                before = list(fixture.events)
                receipt = fixture.run(resume=True)
                self.assertEqual(receipt["outcome"], "completed")
                self.assertEqual(fixture.events.count(("POST", "/tasks")), 1)
                if phase in {"archive_sealed", "output_sealed", "validated"}:
                    result_path = "/tasks/" + fixture.task_id + "/result"
                    self.assertEqual(fixture.events.count(("GET", result_path)), before.count(("GET", result_path)))
                self.assert_closed_resources(fixture)

    def test_new_attempt_requires_runtime_identity(self) -> None:
        fixture = self.fixture()
        with self.assertRaises(ParserOutputContractError):
            fixture.run(options=ParserOptions(timeout_seconds=60))
        self.assertFalse(fixture.events)
