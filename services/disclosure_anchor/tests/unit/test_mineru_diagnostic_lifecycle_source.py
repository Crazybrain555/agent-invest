"""Independent physical source preflight and exact observer-child drainage tests."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

from tests._mineru_diagnostic_lifecycle_fixture import LifecycleFixture, digest, journal_records


class MinerUDiagnosticLifecycleSourceTests(unittest.TestCase):
    def fixture(self, **kwargs: object) -> LifecycleFixture:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return LifecycleFixture(Path(temporary.name), **kwargs)

    def test_real_two_page_source_observation_is_durable_before_submission(self) -> None:
        fixture = self.fixture()

        def inspect_post(request: httpx.Request) -> None:
            if request.method == "POST" and request.url.path == "/tasks":
                records = journal_records(fixture.journal)
                steps = [record["step"] for record in records]
                self.assertLess(steps.index("source_observed"), steps.index("submit_intent"))
                observation = next(record["value"] for record in records if record["step"] == "source_observed")
                self.assertEqual(observation["response_sha256"], digest(bytes.fromhex(observation["response_hex"])))
                raw = json.loads(bytes.fromhex(observation["response_hex"]))
                self.assertEqual(raw["page_count"], 2)
                self.assertEqual(raw["sha256"], fixture.source_sha)
                self.assertEqual(raw["byte_count"], len(fixture.source_bytes))

        fixture.before_request = inspect_post
        receipt = fixture.run()
        self.assertEqual(receipt["outcome"], "completed")

    def test_malformed_pdf_and_wrong_declared_physical_pages_have_zero_posts(self) -> None:
        for options in ({"malformed_source": True}, {"source_pages": 1}):
            with self.subTest(source=options):
                fixture = self.fixture(**options)
                with self.assertRaises((ValueError, RuntimeError)):
                    fixture.run()
                self.assertFalse(fixture.events)
                self.assertEqual(fixture.source.read_bytes(), fixture.source_bytes)
                self.assertTrue((fixture.journal / "resources" / "source.pdf").is_file())
                self.assertNotIn("submit_intent", [record["step"] for record in journal_records(fixture.journal)])

    def test_original_deadline_kills_reaps_source_child_and_closes_both_pipes(self) -> None:
        fixture = self.fixture()
        popen = subprocess.Popen
        children = []

        def blocked_observer(command: object, **kwargs: object) -> subprocess.Popen:
            self.assertIn("disclosure_anchor.adapters.parsers.pdf_source_observation_process", command)
            child = popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            children.append(child)
            fixture.now_ns = fixture.deadline_ns
            return child

        with patch("disclosure_anchor.adapters.runtime.mineru_diagnostic_source.subprocess.Popen", blocked_observer):
            with self.assertRaises(TimeoutError):
                fixture.run()
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertIsNotNone(child.returncode)
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdout.closed)
        self.assertTrue(child.stderr.closed)
        self.assertFalse(fixture.events)
        self.assertTrue((fixture.journal / "resources" / "source.pdf").is_file())

    def test_failed_kill_retains_exact_live_child_and_unclosed_probe_cannot_respawn(self) -> None:
        for wait_error in ("timeout", "oserror"):
            with self.subTest(wait_error=wait_error):
                self._assert_failed_kill_retains_child(wait_error=wait_error)

    def _assert_failed_kill_retains_child(self, *, wait_error: str) -> None:
        from disclosure_anchor.adapters.runtime.mineru_diagnostic_source import DiagnosticSourceChildUnresolved

        fixture = self.fixture()
        popen = subprocess.Popen
        children = []
        waits = []
        observed_wait_errors = []

        def unkillable_observer(command: object, **kwargs: object) -> subprocess.Popen:
            steps = [record["step"] for record in journal_records(fixture.journal)]
            self.assertIn("source_probe_intent", steps)
            child = popen([sys.executable, "-c", "import os,time; os.write(1,b'x'*8193); time.sleep(60)"], **kwargs)
            children.append(child)
            kill, wait = child.kill, child.wait

            def cleanup_owned_child() -> None:
                if child.poll() is None:
                    kill()
                wait(timeout=5)
                for stream in (child.stdout, child.stderr):
                    if not stream.closed:
                        stream.close()

            self.addCleanup(cleanup_owned_child)

            def fail_kill() -> None:
                raise OSError("synthetic signal failure; exact child remains live")

            def bounded_wait(timeout: float | None = None) -> int:
                waits.append(timeout)
                self.assertIsNotNone(timeout, "cleanup attempted an unbounded wait")
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, 1)
                try:
                    if wait_error == "oserror":
                        raise OSError("synthetic wait syscall failure; exact child remains live")
                    return wait(timeout=timeout)
                except (OSError, subprocess.TimeoutExpired) as error:
                    observed_wait_errors.append(error)
                    raise

            child.kill = fail_kill
            child.wait = bounded_wait
            return child

        with patch("disclosure_anchor.adapters.runtime.mineru_diagnostic_source.subprocess.Popen", unkillable_observer):
            with self.assertRaises(BaseException) as raised:
                fixture.run()
            self.assertEqual(len(children), 1)
            child = children[0]

            def leaves(error: BaseException) -> list[BaseException]:
                if isinstance(error, BaseExceptionGroup):
                    return [leaf for branch in error.exceptions for leaf in leaves(branch)]
                return [error]

            errors = leaves(raised.exception)
            retained = [error for error in errors if isinstance(error, DiagnosticSourceChildUnresolved)]
            self.assertEqual(len(retained), 1)
            self.assertIs(retained[0].process, child)
            self.assertTrue(any(isinstance(error, OSError) and "signal failure" in str(error) for error in errors))
            self.assertEqual(len(observed_wait_errors), 1)
            self.assertTrue(any(error is observed_wait_errors[0] for error in errors), "original wait error was replaced")
            self.assertIsInstance(observed_wait_errors[0], OSError if wait_error == "oserror" else subprocess.TimeoutExpired)
            self.assertIsNone(child.poll())
            self.assertTrue(child.stdout.closed)
            self.assertTrue(child.stderr.closed)
            self.assertEqual(len(waits), 1)
            self.assertFalse(fixture.events)
            self.assertTrue((fixture.journal / "resources" / "source.pdf").is_file())
            with self.assertRaises(ValueError):
                fixture.run(resume=True)
            self.assertEqual(len(children), 1)
            self.assertFalse(fixture.events)
