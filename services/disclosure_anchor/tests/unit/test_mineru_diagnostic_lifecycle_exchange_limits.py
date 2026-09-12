"""Independent R2 regressions for proof size and reply-independent retry authority."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import httpx

from tests._mineru_diagnostic_lifecycle_fixture import (
    ChunkStream, LifecycleFixture, SimulatedCrash, crash_after_phase, digest, journal_records,
)


class _MaximumAckFixture(LifecycleFixture):
    """Valid closed JSON can carry raw trailing whitespace up to the wire bound."""

    def response(self, status: int, payload: object) -> httpx.Response:
        consumed = isinstance(payload, dict) and payload.get("status") == "consumed"
        if not consumed and payload != {"detail": "Task not found"}:
            return super().response(status, payload)
        raw = json.dumps(payload, separators=(",", ":")).encode()
        raw += b" " * (1024 * 1024 - len(raw))
        self.responses.append((status, raw))
        stream = ChunkStream(raw)
        self.streams.append(stream)
        return httpx.Response(status, stream=stream)


class _UnsuccessfulAckFixture(LifecycleFixture):
    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/ack"):
            self.events.append((request.method, request.url.path))
            self.requests.append(request)
            return self.response(503, {"detail": "synthetic ACK temporarily unavailable"})
        return super().handle(request)


class MinerUDiagnosticLifecycleExchangeLimitsTests(unittest.TestCase):
    def fixture(self, *, maximum: bool = False) -> LifecycleFixture:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return (_MaximumAckFixture if maximum else LifecycleFixture)(Path(temporary.name))

    def assert_compact_seal_with_exact_returned_proof(self, fixture: LifecycleFixture,
                                                    receipt: dict[str, object]) -> None:
        proof = receipt["ack_proof"]
        self.assertEqual(proof["kind"], "actual_response")
        for name, status in (("response", 200), ("absence", 404)):
            evidence = proof[name]
            raw = bytes.fromhex(evidence["response_hex"])
            self.assertEqual(len(raw), 1024 * 1024)
            self.assertEqual(evidence["response_sha256"], digest(raw))
            self.assertIn((status, raw), fixture.responses)
        files = sorted(fixture.journal.glob("[0-9][0-9][0-9][0-9]-*.json"))
        self.assertTrue(all(path.stat().st_size <= 2 * 1024 * 1024 + 8192 for path in files))
        disposed = json.loads(next(path for path in files if path.name.endswith("-disposed.json")).read_bytes())
        exact_proof = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        self.assertEqual(disposed["value"]["proof_sha256"], digest(exact_proof))
        self.assertEqual(disposed["value"]["validation_record_sha256"], receipt["validation_record_sha256"])

        def keys(value: object) -> set[str]:
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(child) for child in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(child) for child in value))
            return set()

        self.assertNotIn("response_hex", keys(disposed["value"]))
        self.assertNotIn("report", keys(disposed["value"]))
        self.assertFalse((fixture.journal / "resources").exists())
        self.assertFalse((fixture.journal / "resources-reclaim").exists())
        self.assertEqual(fixture.ack_effects, 1)

    def test_maximum_ack_and_absence_fit_compact_final_seal_preserving_return_api(self) -> None:
        for outcome in ("completed", "failed"):
            with self.subTest(outcome=outcome):
                fixture = self.fixture(maximum=True)
                fixture.terminal_status = outcome
                options = {}
                if outcome == "completed":
                    options = {"quality_verifier": lambda *args: {
                        "status": "unverified", "reason": "large retained synthetic quality evidence",
                        "report": {"observation": "q" * 300_000},
                    }, "quality_verifier_sha256": "sha256:" + "d" * 64}
                receipt = fixture.run(**options)
                self.assertEqual(receipt["outcome"], outcome)
                self.assert_compact_seal_with_exact_returned_proof(fixture, receipt)
                before = list(fixture.events)
                replay = fixture.run(resume=True, **options)
                self.assertEqual(replay, receipt)
                self.assertEqual(fixture.events, before)
                if outcome == "completed":
                    self.assertEqual(receipt["quality"]["report"]["observation"], "q" * 300_000)

    def test_maximum_remote_proof_resume_after_absence_does_not_repeat_remote_effect(self) -> None:
        fixture = self.fixture(maximum=True)
        with crash_after_phase("remote_absent"), self.assertRaises(SimulatedCrash):
            fixture.run()
        before = list(fixture.events)
        receipt = fixture.run(resume=True)
        self.assert_compact_seal_with_exact_returned_proof(fixture, receipt)
        self.assertEqual(fixture.events, before)

    def test_lost_reconciliation_gets_consume_durable_exchange_budget_before_io(self) -> None:
        for phase in ("submission", "ack"):
            with self.subTest(phase=phase):
                fixture = self.fixture()
                if phase == "submission":
                    fixture.lose_submit_response = True
                else:
                    fixture.ack_loss = "before_effect"
                with self.assertRaises(httpx.ReadError):
                    fixture.run()
                prior_count = len(journal_records(fixture.journal))
                counts_observed_at_transport = []

                def lose_lookup(request: httpx.Request) -> None:
                    if request.method == "GET" and "/tasks/by-idempotency/" in request.url.path:
                        counts_observed_at_transport.append(len(journal_records(fixture.journal)))
                        raise httpx.ReadError("original-key GET sent but every response lost", request=request)

                fixture.before_request = lose_lookup
                for _ in range(4):
                    with self.assertRaises(httpx.ReadError):
                        fixture.run(resume=True)
                before = list(fixture.events)
                with self.assertRaises(ValueError):
                    fixture.run(resume=True)
                self.assertEqual(fixture.events, before)
                self.assertEqual(len(counts_observed_at_transport), 4)
                self.assertTrue(all(after > previous for previous, after in zip(
                    [prior_count, *counts_observed_at_transport[:-1]], counts_observed_at_transport, strict=True,
                )))
                self.assertEqual(fixture.events.count(("POST", "/tasks")), 1)
                self.assertTrue(fixture.task_exists)
                self.assertEqual(fixture.ack_effects, 0)
                self.assertNotIn("disposed", [record["step"] for record in journal_records(fixture.journal)])

    def test_initial_ack_does_not_consume_one_of_four_resumed_exchange_slots(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = _UnsuccessfulAckFixture(Path(temporary.name))
        with self.assertRaises(ValueError):
            fixture.run()
        self.assertEqual(sum(path.endswith("/ack") for _, path in fixture.events), 1)
        for exchange in range(1, 5):
            with self.assertRaises(ValueError):
                fixture.run(resume=True)
            self.assertEqual(sum(path.endswith("/ack") for _, path in fixture.events), 1 + exchange)
        before = list(fixture.events)
        with self.assertRaises(ValueError):
            fixture.run(resume=True)
        self.assertEqual(fixture.events, before)
        self.assertTrue(fixture.task_exists)
        self.assertEqual(fixture.ack_effects, 0)
