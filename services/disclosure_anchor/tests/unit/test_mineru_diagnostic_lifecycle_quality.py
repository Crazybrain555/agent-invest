"""Explicit injected quality evidence is durable; it never invents M6 qualification."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests._mineru_diagnostic_lifecycle_fixture import (
    LifecycleFixture, SimulatedCrash, crash_after_phase, journal_records,
)


VERIFIER_SHA = "sha256:" + "d" * 64


class MinerUDiagnosticLifecycleQualityTests(unittest.TestCase):
    def fixture(self) -> LifecycleFixture:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return LifecycleFixture(Path(temporary.name))

    def test_quality_outcomes_are_preserved_with_original_verifier_identity(self) -> None:
        for status in ("pass", "fail", "needs_review", "unverified"):
            with self.subTest(status=status):
                fixture = self.fixture()
                calls = []
                result = {"status": status, "reason": "injected independent test observation",
                          "report": {"synthetic_fixture": True, "observation": "artifact structure only"}}

                def verify(source: Path, output: Path, document: object) -> dict[str, object]:
                    self.assertEqual(source, fixture.journal / "resources" / "source.pdf")
                    self.assertEqual(source.read_bytes(), fixture.source_bytes)
                    self.assertEqual(output, fixture.journal / "resources" / "output")
                    self.assertTrue((output / "notes.bin").is_file())
                    self.assertEqual(len(document.pages), 2)
                    calls.append(document)
                    return result

                receipt = fixture.run(quality_verifier=verify, quality_verifier_sha256=VERIFIER_SHA)
                self.assertEqual(receipt["quality"], {**result, "verifier_sha256": VERIFIER_SHA})
                self.assertEqual(receipt["outcome"], "completed")
                self.assertEqual(len(calls), 1)
                self.assertEqual(fixture.ack_effects, 1)
                self.assertFalse((fixture.journal / "resources").exists())
                result["report"]["observation"] = "caller changed after completion"
                self.assertEqual(receipt["quality"]["report"]["observation"], "artifact structure only")

    def test_quality_verifier_failure_preserves_payloads_and_does_not_ack(self) -> None:
        fixture = self.fixture()

        def broken(source: Path, output: Path, document: object) -> dict[str, object]:
            raise OSError("independent verifier process fixture failed")

        with self.assertRaisesRegex(OSError, "verifier process"):
            fixture.run(quality_verifier=broken, quality_verifier_sha256=VERIFIER_SHA)
        self.assertTrue((fixture.journal / "resources" / "source.pdf").is_file())
        self.assertTrue((fixture.journal / "resources" / "output").is_dir())
        self.assertFalse(any(path.endswith("/ack") for _, path in fixture.events))
        self.assertNotIn("validated", [record["step"] for record in journal_records(fixture.journal)])

    def test_callback_cannot_mutate_sealed_payload_and_obtain_cleanup_authority(self) -> None:
        for changed in ("source", "output"):
            with self.subTest(changed=changed):
                fixture = self.fixture()

                def tamper(source: Path, output: Path, document: object) -> dict[str, object]:
                    target = source if changed == "source" else output / "notes.bin"
                    target.write_bytes(b"tampered during quality callback")
                    return {"status": "pass", "reason": "cannot override artifact corruption", "report": {}}

                with self.assertRaises((ValueError, RuntimeError)):
                    fixture.run(quality_verifier=tamper, quality_verifier_sha256=VERIFIER_SHA)
                self.assertTrue((fixture.journal / "resources").is_dir())
                self.assertFalse(any(path.endswith("/ack") for _, path in fixture.events))

    def test_invalid_quality_shape_and_absent_verifier_identity_never_dispose(self) -> None:
        for report in ({"status": "pass", "reason": "", "report": {}},
                       {"status": "pass", "reason": "synthetic", "report": {}, "extra": True},
                       {"status": "not_applicable", "reason": "completed source", "report": {}},
                       {"status": "pass", "reason": "synthetic", "report": {1: "opaque key"}}):
            with self.subTest(report=report):
                fixture = self.fixture()
                with self.assertRaises((ValueError, RuntimeError, TypeError)):
                    fixture.run(quality_verifier=lambda *args: report, quality_verifier_sha256=VERIFIER_SHA)
                self.assertFalse(any(path.endswith("/ack") for _, path in fixture.events))
        fixture = self.fixture()
        with self.assertRaises(ValueError):
            fixture.run(quality_verifier=lambda *args: {"status": "pass", "reason": "synthetic", "report": {}})
        self.assertFalse(fixture.events)

    def test_quality_report_over_record_budget_preserves_resources_before_cleanup(self) -> None:
        fixture = self.fixture()
        with self.assertRaises(ValueError):
            fixture.run(quality_verifier=lambda *args: {
                "status": "unverified", "reason": "bounded evidence overflow",
                "report": {"oversized": "x" * (2 * 1024 * 1024 + 8192)},
            }, quality_verifier_sha256=VERIFIER_SHA)
        self.assertTrue((fixture.journal / "resources" / "output").is_dir())
        self.assertFalse(any(path.endswith("/ack") for _, path in fixture.events))
        self.assertNotIn("cleanup_intent", [record["step"] for record in journal_records(fixture.journal)])

    def test_validated_resume_uses_durable_quality_without_invoking_verifier_again(self) -> None:
        fixture = self.fixture()
        calls = []

        def verify(*args: object) -> dict[str, object]:
            calls.append(args)
            if len(calls) > 1:
                raise AssertionError("durably validated quality was repeated")
            return {"status": "needs_review", "reason": "retained test review obligation", "report": {}}

        with crash_after_phase("validated"), self.assertRaises(SimulatedCrash):
            fixture.run(quality_verifier=verify, quality_verifier_sha256=VERIFIER_SHA)
        before = list(fixture.events)
        with self.assertRaises(ValueError):
            fixture.run(resume=True, quality_verifier=verify, quality_verifier_sha256="sha256:" + "e" * 64)
        self.assertEqual(fixture.events, before)
        receipt = fixture.run(resume=True, quality_verifier=verify, quality_verifier_sha256=VERIFIER_SHA)
        self.assertEqual(receipt["quality"]["status"], "needs_review")
        self.assertEqual(len(calls), 1)
