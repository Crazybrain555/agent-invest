"""Independent guard-boundary tests; synthetic protocol fixtures, no live GPU.

Reuse the independently authored E1 fixture and its actual temporary journal,
source observer and artifact IO. Only HTTP is simulated. No quality qualification
is inferred from a permitted submit or a successful diagnostic disposal.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import tempfile
from typing import BinaryIO
import unittest
from unittest.mock import patch

import httpx

from disclosure_anchor.adapters.runtime.mineru_diagnostic_resources import DiagnosticResources
from tests._mineru_diagnostic_lifecycle_fixture import (
    LifecycleFixture, SimulatedCrash, crash_after_phase, journal_records,
)


class MinerUDiagnosticBeforeSubmitTests(unittest.TestCase):
    def fixture(self) -> LifecycleFixture:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return LifecycleFixture(Path(temporary.name))

    def test_default_omitted_and_explicit_none_keep_existing_disposal(self) -> None:
        for arguments in ({}, {"before_submit": None}):
            with self.subTest(arguments=arguments):
                fixture = self.fixture()
                result = fixture.run(**arguments)
                self.assertEqual(result["outcome"], "completed")
                self.assertEqual(result["quality"]["status"], "unverified")
                self.assertEqual(fixture.events.count(("POST", "/tasks")), 1)
                self.assertEqual(fixture.ack_effects, 1)
                self.assertFalse((fixture.journal / "resources").exists())

    def test_guard_runs_with_durable_intent_and_open_original_source_before_post(self) -> None:
        fixture = self.fixture()
        original_open = DiagnosticResources.open_payload
        active: list[BinaryIO] = []
        order: list[str] = []

        @contextmanager
        def observed_open(resources: DiagnosticResources, name: str, *, identity: list[int]) -> Iterator[BinaryIO]:
            with original_open(resources, name, identity=identity) as stream:
                if name == "source.pdf":
                    active.append(stream)
                try:
                    yield stream
                finally:
                    if name == "source.pdf":
                        active.remove(stream)

        def guard() -> None:
            self.assertEqual(order, [])
            self.assertEqual(fixture.events, [])
            self.assertEqual(len(active), 1)
            self.assertFalse(active[0].closed)
            self.assertEqual(active[0].tell(), 0)
            records = journal_records(fixture.journal)
            self.assertEqual(records[-1]["step"], "submit_intent")
            self.assertIn("source_observed", [record["step"] for record in records])
            self.assertEqual((fixture.journal / "resources" / "source.pdf").read_bytes(), fixture.source_bytes)
            order.append("guard")

        def before_request(request: httpx.Request) -> None:
            if request.method == "POST" and request.url.path == "/tasks":
                self.assertEqual(order, ["guard"])
                self.assertEqual(len(active), 1)
                self.assertFalse(active[0].closed)
                order.append("post")

        fixture.before_request = before_request
        with patch.object(DiagnosticResources, "open_payload", observed_open):
            result = fixture.run(before_submit=guard)
        self.assertEqual(order, ["guard", "post"])
        self.assertEqual(active, [])
        self.assertEqual(result["quality"]["status"], "unverified")
        self.assertEqual(fixture.ack_effects, 1)

    def test_rejected_guard_preserves_exception_and_original_unresolved_responsibility(self) -> None:
        fixture = self.fixture()
        marker = TimeoutError("original controller admission stopped")
        calls = 0

        def deny() -> None:
            nonlocal calls
            calls += 1
            raise marker

        with self.assertRaises(TimeoutError) as caught:
            fixture.run(before_submit=deny)
        self.assertIs(caught.exception, marker)
        self.assertEqual(calls, 1)
        self.assertEqual(fixture.events, [])
        records = journal_records(fixture.journal)
        steps = [record["step"] for record in records]
        self.assertEqual(steps[-1], "submit_intent")
        self.assertTrue({"snapshot_sealed", "source_observed"}.issubset(steps))
        self.assertTrue({"accepted", "cleanup_intent", "local_removed", "disposed"}.isdisjoint(steps))
        snapshot = fixture.journal / "resources" / "source.pdf"
        self.assertEqual(snapshot.read_bytes(), fixture.source_bytes)
        self.assertEqual(fixture.source.read_bytes(), fixture.source_bytes)
        self.assertEqual(fixture.ack_effects, 0)
        header = (fixture.journal / "00-journal.json").read_bytes()
        binding = next(record["value"] for record in records if record["step"] == "binding")
        original_key = binding["prepared"]["client_submit_key"]

        # Existing state never becomes another fresh submit, even with permission.
        with self.assertRaises((FileExistsError, ValueError)):
            fixture.run(before_submit=lambda: None)
        self.assertEqual(fixture.events, [])

        # Explicit resume consults the original key; pre-ACK absence is unresolved.
        with self.assertRaises((ValueError, RuntimeError)):
            fixture.run(resume=True, before_submit=deny)
        self.assertEqual(calls, 1)
        self.assertEqual(fixture.events, [("GET", "/tasks/by-idempotency/" + original_key)])
        self.assertEqual((fixture.journal / "00-journal.json").read_bytes(), header)
        self.assertEqual(snapshot.read_bytes(), fixture.source_bytes)
        self.assertNotIn("disposed", [record["step"] for record in journal_records(fixture.journal)])
        self.assertEqual(fixture.ack_effects, 0)

    def test_lost_post_resume_uses_original_key_without_rechecking_new_admission(self) -> None:
        fixture = self.fixture()
        fixture.lose_submit_response = True
        with self.assertRaises(httpx.ReadError):
            fixture.run(before_submit=lambda: None)
        original_key = fixture.fields["agent_idempotency_key"]
        header = (fixture.journal / "00-journal.json").read_bytes()

        def denied_new_admission() -> None:
            self.fail("same-key lookup/drain called fresh-submit guard")

        result = fixture.run(resume=True, before_submit=denied_new_admission)
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["prepared"]["client_submit_key"], original_key)
        self.assertEqual(fixture.events.count(("POST", "/tasks")), 1)
        lookup_paths = {path for method, path in fixture.events if method == "GET" and "/by-idempotency/" in path}
        self.assertEqual(lookup_paths, {"/tasks/by-idempotency/" + original_key})
        self.assertEqual((fixture.journal / "00-journal.json").read_bytes(), header)
        self.assertEqual(fixture.ack_effects, 1)

    def test_accepted_and_disposed_resume_do_not_call_submit_guard(self) -> None:
        fixture = self.fixture()
        with crash_after_phase("accepted"), self.assertRaises(SimulatedCrash):
            fixture.run()
        header = (fixture.journal / "00-journal.json").read_bytes()

        def denied_new_admission() -> None:
            self.fail("already accepted/disposed recovery called fresh-submit guard")

        result = fixture.run(resume=True, before_submit=denied_new_admission)
        self.assertEqual(result["outcome"], "completed")
        events = list(fixture.events)
        repeated = fixture.run(resume=True, before_submit=denied_new_admission)
        self.assertEqual(repeated, result)
        self.assertEqual(fixture.events, events)
        self.assertEqual(fixture.events.count(("POST", "/tasks")), 1)
        self.assertEqual((fixture.journal / "00-journal.json").read_bytes(), header)
        self.assertEqual(fixture.ack_effects, 1)


if __name__ == "__main__":
    unittest.main()
