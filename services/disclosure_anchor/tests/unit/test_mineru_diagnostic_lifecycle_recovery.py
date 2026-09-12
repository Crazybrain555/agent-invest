"""Independent recovery, exact deletion authority and durable wire-proof tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournal
from disclosure_anchor.adapters.runtime.mineru_diagnostic_resources import DiagnosticResources
from tests._mineru_diagnostic_lifecycle_fixture import (
    LifecycleFixture, SimulatedCrash, crash_after_phase, digest, journal_records,
)


def _fifo_replacement_case(case: str) -> None:
    """Child-process case: absent nonblocking-open protection must not hang unittest."""
    with tempfile.TemporaryDirectory() as temporary:
        fixture = LifecycleFixture(Path(temporary))
        if case == "archive":
            try:
                with crash_after_phase("archive_sealed"):
                    fixture.run()
            except SimulatedCrash:
                pass
            else:
                raise AssertionError("archive phase cut did not occur")
            target = fixture.journal / "resources" / "result.zip"
        else:
            target = fixture.source
        target.rename(fixture.root / "preserved-original")
        os.mkfifo(target, mode=0o600)
        info = target.stat()
        before = list(fixture.events)
        try:
            fixture.run(resume=case == "archive")
        except ValueError:
            pass
        else:
            raise AssertionError("FIFO was accepted as a regular owned payload")
        assert fixture.events == before, "FIFO replacement authorized network effects"
        assert (target.stat().st_dev, target.stat().st_ino) == (info.st_dev, info.st_ino), "FIFO was removed/replaced"
        assert (fixture.root / "preserved-original").is_file(), "original regular payload was removed"
        print(json.dumps({"case": case, "fifo_preserved": True, "network_effects": 0}))


class MinerUDiagnosticLifecycleRecoveryTests(unittest.TestCase):
    def fixture_at(self, phase: str | None = None) -> LifecycleFixture:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = LifecycleFixture(Path(temporary.name))
        if phase is not None:
            with crash_after_phase(phase), self.assertRaises(SimulatedCrash):
                fixture.run()
        return fixture

    def reopen_journal(self, fixture: LifecycleFixture) -> DiagnosticJournal:
        header = json.loads((fixture.journal / "00-journal.json").read_bytes())
        return DiagnosticJournal(
            fixture.journal, create=False, attempt_id=header["attempt_id"],
            configuration_sha256=header["configuration_sha256"],
            clock_identity_sha256=header["clock_identity_sha256"], deadline_ns=header["deadline_ns"],
            continuous_ns=lambda: fixture.now_ns,
        )

    def test_ack_observes_durable_quality_local_closure_and_closed_streams(self) -> None:
        fixture = self.fixture_at()

        def inspect_effect(request: httpx.Request) -> None:
            if request.url.path.endswith("/ack"):
                self.assertFalse((fixture.journal / "resources").exists())
                self.assertFalse((fixture.journal / "resources-reclaim").exists())
                steps = [record["step"] for record in journal_records(fixture.journal)]
                self.assertLess(steps.index("validated"), steps.index("cleanup_intent"))
                self.assertLess(steps.index("cleanup_intent"), steps.index("local_removed"))
                self.assertLess(steps.index("local_removed"), steps.index("ack_intent"))
                self.assertTrue(all(stream.closed for stream in fixture.streams))

        fixture.before_request = inspect_effect
        receipt = fixture.run()
        self.assertEqual(receipt["quality"]["status"], "unverified")
        self.assertIsNone(receipt["quality"]["verifier_sha256"])
        self.assertIsInstance(receipt["quality"]["report"], dict)
        self.assertTrue({"unit_count", "unusable_unit_count", "needs_review_unit_count"}.isdisjoint(
            receipt["quality"]["report"],
        ))
        self.assertEqual(receipt["ack_proof"]["kind"], "actual_response")
        proof = receipt["ack_proof"]
        for key in ("response", "absence"):
            raw = bytes.fromhex(proof[key]["response_hex"])
            self.assertEqual(proof[key]["response_sha256"], digest(raw))
            self.assertIn((proof[key]["http_status"], raw), fixture.responses)
        self.assertEqual(json.loads(bytes.fromhex(proof["response"]["response_hex"])), {
            "schema": "mineru-task-protocol.v2", "task_id": fixture.task_id, "status": "consumed",
        })

    def test_ack_effect_with_lost_response_uses_absence_without_fabricating_response(self) -> None:
        fixture = self.fixture_at()
        fixture.ack_loss = "after_effect"
        with self.assertRaises(httpx.ReadError):
            fixture.run()
        self.assertFalse((fixture.journal / "resources").exists())
        self.assertEqual(fixture.ack_effects, 1)
        fixture.ack_loss = ""
        receipt = fixture.run(resume=True)
        self.assertEqual(receipt["ack_proof"]["kind"], "reconciled_absence")
        self.assertIsNone(receipt["ack_proof"]["response"])
        self.assertEqual(fixture.ack_effects, 1)
        self.assertEqual(sum(path.endswith("/ack") for _, path in fixture.events), 1)

    def test_ack_not_applied_can_retry_only_same_known_task_after_local_closure(self) -> None:
        fixture = self.fixture_at()
        fixture.ack_loss = "before_effect"
        with self.assertRaises(httpx.ReadError):
            fixture.run()
        self.assertTrue(fixture.task_exists)
        self.assertEqual(fixture.ack_effects, 0)
        fixture.ack_loss = ""
        receipt = fixture.run(resume=True)
        self.assertEqual(receipt["ack_proof"]["kind"], "actual_response")
        self.assertEqual(fixture.events.count(("POST", "/tasks")), 1)
        ack_paths = [path for _, path in fixture.events if path.endswith("/ack")]
        self.assertEqual(ack_paths, ["/tasks/" + fixture.task_id + "/ack"] * 2)
        self.assertEqual(fixture.ack_effects, 1)

    def test_ack_reconciliation_bound_stops_before_another_request(self) -> None:
        fixture = self.fixture_at()
        fixture.ack_loss = "before_effect"
        with self.assertRaises(httpx.ReadError):
            fixture.run()
        for _ in range(4):
            with self.assertRaises(httpx.ReadError):
                fixture.run(resume=True)
        before = list(fixture.events)
        with self.assertRaisesRegex(ValueError, "budget"):
            fixture.run(resume=True)
        self.assertEqual(fixture.events, before)
        self.assertEqual(fixture.events.count(("POST", "/tasks")), 1)
        self.assertEqual(fixture.ack_effects, 0)
        self.assertTrue(fixture.task_exists)
        self.assertNotIn("disposed", [record["step"] for record in journal_records(fixture.journal)])

    def test_noncanonical_absence_cannot_close_even_after_ack_intent(self) -> None:
        fixture = self.fixture_at()
        fixture.ack_loss = "after_effect"
        with self.assertRaises(httpx.ReadError):
            fixture.run()
        fixture.absence_payload = {"detail": "No such task"}
        with self.assertRaises((ValueError, RuntimeError)):
            fixture.run(resume=True)
        self.assertNotIn("disposed", [record["step"] for record in journal_records(fixture.journal)])

    def test_absence_before_cleanup_ack_authority_does_not_dispose(self) -> None:
        fixture = self.fixture_at("accepted")
        fixture.task_exists = False
        with self.assertRaises((ValueError, RuntimeError)):
            fixture.run(resume=True)
        self.assertTrue((fixture.journal / "resources" / "source.pdf").is_file())
        self.assertFalse(any(path.endswith("/ack") for _, path in fixture.events))

    def test_complete_receipt_resume_reuses_identical_proof_without_network(self) -> None:
        fixture = self.fixture_at()
        receipt = fixture.run()
        before = list(fixture.events)
        replay = fixture.run(resume=True)
        self.assertEqual(replay, receipt)
        self.assertEqual(fixture.events, before)

    def test_final_record_write_loss_preserves_exact_remote_proof_for_retry(self) -> None:
        fixture = self.fixture_at("remote_absent")
        before = list(fixture.events)
        receipt = fixture.run(resume=True)
        self.assertEqual(receipt["outcome"], "completed")
        self.assertEqual(receipt["ack_proof"]["kind"], "actual_response")
        self.assertEqual(fixture.events, before)

    def test_sealed_zip_and_output_content_corruption_are_retained_not_repaired(self) -> None:
        for phase, relative in (("archive_sealed", "result.zip"),
                                ("output_sealed", "output/notes.bin")):
            with self.subTest(phase=phase):
                fixture = self.fixture_at(phase)
                payload = fixture.journal / "resources" / relative
                original = payload.read_bytes()
                changed = bytes([original[0] ^ 1]) + original[1:]
                payload.write_bytes(changed)
                downloads = sum(path.endswith("/result") for _, path in fixture.events)
                with self.assertRaises((ValueError, RuntimeError)):
                    fixture.run(resume=True)
                self.assertEqual(payload.read_bytes(), changed)
                self.assertEqual(sum(path.endswith("/result") for _, path in fixture.events), downloads)
                self.assertFalse(any(path.endswith("/ack") for _, path in fixture.events))

    def test_materialization_seal_reuse_skips_extraction(self) -> None:
        fixture = self.fixture_at("output_sealed")
        with patch.object(DiagnosticResources, "extract_archive", side_effect=AssertionError("repeat extraction")):
            receipt = fixture.run(resume=True)
        self.assertEqual(receipt["outcome"], "completed")

    def test_replaced_resource_root_and_children_are_never_adopted_or_removed(self) -> None:
        for relative, is_directory in (("resources", True), ("resources/result.zip", False),
                                       ("resources/output/notes.bin", False)):
            with self.subTest(replaced=relative):
                fixture = self.fixture_at("validated")
                target = fixture.journal / relative
                preserved_original = fixture.root / "original-object"
                target.rename(preserved_original)
                if is_directory:
                    target.mkdir(mode=0o700)
                    marker = target / "foreign.bin"
                else:
                    marker = target
                marker.write_bytes(b"foreign object must remain exact")
                marker.chmod(0o600)
                with self.assertRaises((ValueError, RuntimeError)):
                    fixture.run(resume=True)
                self.assertEqual(marker.read_bytes(), b"foreign object must remain exact")
                self.assertTrue(preserved_original.exists())
                self.assertFalse(any(path.endswith("/ack") for _, path in fixture.events))

    def test_unsealed_created_archive_is_not_overwritten_or_redownloaded(self) -> None:
        fixture = self.fixture_at("archive_created")
        target = fixture.journal / "resources" / "result.zip"
        target.write_bytes(b"ambiguous partial transfer; preserve")
        before = list(fixture.events)
        with self.assertRaises((ValueError, RuntimeError)):
            fixture.run(resume=True)
        self.assertEqual(target.read_bytes(), b"ambiguous partial transfer; preserve")
        self.assertFalse(any(path.endswith(("/result", "/ack")) for _, path in fixture.events[len(before):]))

    def test_creation_without_durable_identity_is_preserved_before_payload_writes(self) -> None:
        for phase, relative, directory in (("resources_created", "resources", True),
                                            ("snapshot_created", "resources/source.pdf", False),
                                            ("archive_created", "resources/result.zip", False),
                                            ("output_created", "resources/output", True)):
            with self.subTest(phase=phase):
                fixture = self.fixture_at()
                original_append = DiagnosticJournal.append

                def stop_before_identity(journal: DiagnosticJournal, step: str,
                                         value: dict[str, object]) -> object:
                    if step == phase:
                        raise SimulatedCrash("created object not yet journaled")
                    return original_append(journal, step, value)

                with patch.object(DiagnosticJournal, "append", stop_before_identity), self.assertRaises(SimulatedCrash):
                    fixture.run()
                target = fixture.journal / relative
                info = target.stat()
                if directory:
                    self.assertEqual(list(target.iterdir()), [])
                else:
                    self.assertEqual(target.read_bytes(), b"")
                before = list(fixture.events)
                with self.assertRaises((ValueError, RuntimeError, FileExistsError)):
                    fixture.run(resume=True)
                self.assertEqual((target.stat().st_dev, target.stat().st_ino), (info.st_dev, info.st_ino))
                self.assertNotIn(phase, [record["step"] for record in journal_records(fixture.journal)])
                self.assertFalse(any(method == "POST" or path.endswith("/result")
                                     for method, path in fixture.events[len(before):]))

    def test_valid_suffix_loss_does_not_authorize_a_second_transfer(self) -> None:
        fixture = self.fixture_at("archive_sealed")
        target = fixture.journal / "resources" / "result.zip"
        original = target.read_bytes()
        next(fixture.journal.glob("*-archive_sealed.json")).unlink()
        with self.assertRaises((ValueError, RuntimeError)):
            fixture.run(resume=True)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(sum(path.endswith("/result") for _, path in fixture.events), 1)
        self.assertEqual(fixture.events.count(("POST", "/tasks")), 1)

    def test_partial_cleanup_can_resume_only_from_original_inventory(self) -> None:
        fixture = self.fixture_at("cleanup_intent")
        unlink = os.unlink
        triggered = False
        source_info = (fixture.journal / "resources" / "source.pdf").stat()

        def stop_after_source(path: object, *args: object, **kwargs: object) -> None:
            nonlocal triggered
            before = os.stat(path, dir_fd=kwargs.get("dir_fd"), follow_symlinks=False)
            unlink(path, *args, **kwargs)
            if (before.st_dev, before.st_ino) == (source_info.st_dev, source_info.st_ino):
                triggered = True
                raise SimulatedCrash("after source unlink")

        with patch("disclosure_anchor.adapters.runtime.mineru_diagnostic_resources.os.unlink", stop_after_source):
            with self.assertRaises(SimulatedCrash):
                fixture.run(resume=True)
        self.assertTrue(triggered)
        self.assertFalse((fixture.journal / "resources" / "source.pdf").exists())
        self.assertFalse(any(path.endswith("/ack") for _, path in fixture.events))
        receipt = fixture.run(resume=True)
        self.assertEqual(receipt["outcome"], "completed")
        self.assertFalse((fixture.journal / "resources").exists())

    def test_raw_name_replacement_at_cleanup_move_preserves_foreign_bytes(self) -> None:
        fixture = self.fixture_at("cleanup_intent")
        original_source = fixture.journal / "resources" / "source.pdf"
        original_info = original_source.stat()
        detached = fixture.root / "detached-original-source"
        rename = os.rename
        triggered = False
        foreign = b"foreign bytes raced into old source pathname"

        def replace_before_move(source: object, destination: object, *args: object, **kwargs: object) -> None:
            nonlocal triggered
            source_fd = kwargs.get("src_dir_fd")
            info = os.stat(source, dir_fd=source_fd, follow_symlinks=False)
            if not triggered and (info.st_dev, info.st_ino) == (original_info.st_dev, original_info.st_ino):
                triggered = True
                rename(source, detached, src_dir_fd=source_fd)
                fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=source_fd)
                with os.fdopen(fd, "wb") as file:
                    file.write(foreign)
            rename(source, destination, *args, **kwargs)

        with patch("disclosure_anchor.adapters.runtime.mineru_diagnostic_resources.os.rename", replace_before_move):
            with self.assertRaises(ValueError):
                fixture.run(resume=True)
        self.assertTrue(triggered)
        self.assertEqual(detached.read_bytes(), fixture.source_bytes)
        retained = [path for path in (fixture.journal / "resources").rglob("*")
                    if path.is_file() and path.read_bytes() == foreign]
        self.assertEqual(len(retained), 1)
        with self.assertRaises((ValueError, RuntimeError)):
            fixture.run(resume=True)
        self.assertEqual(retained[0].read_bytes(), foreign)
        self.assertFalse(any(path.endswith("/ack") for _, path in fixture.events))

    def test_cleanup_removed_before_receipt_reconciles_under_existing_intent(self) -> None:
        fixture = self.fixture_at("cleanup_intent")
        append = DiagnosticJournal.append

        def stop_before_removed(journal: DiagnosticJournal, step: str, value: dict[str, object]) -> object:
            if step == "local_removed":
                raise SimulatedCrash("local files already removed, receipt not written")
            return append(journal, step, value)

        with patch.object(DiagnosticJournal, "append", stop_before_removed), self.assertRaises(SimulatedCrash):
            fixture.run(resume=True)
        self.assertFalse((fixture.journal / "resources").exists())
        self.assertFalse(any(path.endswith("/ack") for _, path in fixture.events))
        receipt = fixture.run(resume=True)
        self.assertEqual(receipt["outcome"], "completed")

    def test_cleanup_root_move_crash_resumes_original_quarantine_identity(self) -> None:
        fixture = self.fixture_at("cleanup_intent")
        original = (fixture.journal / "resources").stat()
        rmdir = os.rmdir

        def stop_before_root_delete(path: object, *args: object, **kwargs: object) -> None:
            if path == "resources-reclaim":
                raise SimulatedCrash("owned root moved, namespace removal unfinished")
            rmdir(path, *args, **kwargs)

        with patch("disclosure_anchor.adapters.runtime.mineru_diagnostic_resources.os.rmdir", stop_before_root_delete):
            with self.assertRaises(SimulatedCrash):
                fixture.run(resume=True)
        moved = fixture.journal / "resources-reclaim"
        self.assertEqual((moved.stat().st_dev, moved.stat().st_ino), (original.st_dev, original.st_ino))
        self.assertFalse(any(path.endswith("/ack") for _, path in fixture.events))
        receipt = fixture.run(resume=True)
        self.assertEqual(receipt["outcome"], "completed")
        self.assertFalse(moved.exists())
        self.assertFalse((fixture.journal / "resources").exists())

    def test_phase_semantic_failure_is_rejected_even_with_valid_generic_hash_chain(self) -> None:
        for step, value in (("not_a_lifecycle_phase", {}), ("accepted", {"wire_record_sha256": "sha256:" + "1" * 64}),
                            ("cleanup_intent", {"basis_sha256": "sha256:" + "2" * 64})):
            with self.subTest(step=step):
                fixture = self.fixture_at("submit_intent")
                with self.reopen_journal(fixture) as journal:
                    journal.append(step, value)
                before = list(fixture.events)
                with self.assertRaises(ValueError):
                    fixture.run(resume=True)
                self.assertEqual(fixture.events, before)
                self.assertTrue((fixture.journal / "resources" / "source.pdf").is_file())

    def test_exclusive_attempt_owner_blocks_second_lifecycle_without_network(self) -> None:
        fixture = self.fixture_at("submit_intent")
        with self.reopen_journal(fixture):
            with self.assertRaises((BlockingIOError, ValueError)):
                fixture.run(resume=True)
        self.assertFalse(fixture.events)

    def test_fifo_replacements_fail_promptly_and_preserve_exact_foreign_inode(self) -> None:
        for case in ("input", "archive"):
            with self.subTest(case=case):
                code = ("from tests.unit.test_mineru_diagnostic_lifecycle_recovery import _fifo_replacement_case; "
                        f"_fifo_replacement_case({case!r})")
                result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {"case": case, "fifo_preserved": True, "network_effects": 0})
