"""Independent ongoing-verification boundaries using actual runner-spool bytes."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime.m6_e2e_assembly import M6LifecycleSpool
from disclosure_anchor.adapters.runtime.m6_verifier_supervisor import (
    PublicOutcome, QualityOutcome, ReadyAttempt, RunnerSpoolTail, SpoolTailError,
    SupervisorRefused, VerifierSupervisor,
)
from disclosure_anchor.application.contracts.m6_run_events import M6DocumentQualified
from tests import m6_support as m6


class VerifierSupervisorIndependentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = m6.make_fixture("e2e_publication", {"a": (7, "fresh"), "b": (5, "fresh")})
        self.spool = M6LifecycleSpool(
            self.root / "runner", run_id=self.fixture.spec.run_id,
            spec_sha256=self.fixture.spec.canonical_sha256(), producer_epoch_sha256=m6.RUNNER_EPOCH,
            max_facts=16,
        )
        self.addCleanup(self.spool.close)
        self.path = Path(self.spool.status()["path"])

    def tail(self, path=None, **kwargs):
        return RunnerSpoolTail(path or self.path, run_id=self.fixture.spec.run_id,
                               spec_sha256=self.fixture.spec.canonical_sha256(), **kwargs)

    def payloads(self, key="a"):
        journal = m6.Journal(self.fixture)
        source = self.fixture.entries[key]
        admission = journal.admission(source, "att-" + key)
        committed = journal.commit(admission, journal.at(10), ledger_seq=1)
        public = journal.confirm(admission, journal.at(11), ledger_seq=1)
        proof = m6.qualification_for(source, "e2e_publication", admission.attempt_id)
        qualified = M6DocumentQualified(attempt_id=admission.attempt_id,
                                       qualification_evidence_sha256=proof.canonical_sha256())
        return admission, committed, public, qualified

    def add_facts(self):
        admission, committed, _, _ = self.payloads()
        self.spool.record_event(admission, attempt_id=admission.attempt_id)
        self.spool.record_event(committed, attempt_id=admission.attempt_id)

    def test_runner_facts_wait_for_both_owner_receipts_and_release_once(self):
        tail = self.tail()
        self.add_facts()
        self.assertEqual(tail.poll(), (), "local publication alone is not an owner acknowledgement")
        self.spool.mark_delivered(1, 10)
        self.assertEqual(tail.poll(), ())
        self.spool.mark_delivered(2, 11)
        released = tail.poll()
        self.assertEqual([(r.attempt_id, r.admitted_owner_sequence, r.committed_owner_sequence) for r in released],
                         [("att-a", 10, 11)])
        self.assertEqual(tail.poll(), ())
        self.spool.close()
        self.assertIsNone(tail.finalize()["failed"])

    def test_live_partial_line_waits_but_final_truncation_is_visible(self):
        raw = self.path.read_bytes()
        path = self.root / "growing.jsonl"
        path.write_bytes(raw[:-4])
        tail = self.tail(path)
        self.assertEqual(tail.poll(), ())
        with path.open("ab") as handle:
            handle.write(raw[-4:])
        self.assertEqual(tail.poll(), ())
        with path.open("ab") as handle:
            handle.write(b'{"kind":"fact"')
        self.assertEqual(tail.poll(), ())
        with self.assertRaises(SpoolTailError):
            tail.finalize()
        # Only a not-yet-created producer file may be waited for. Losing an
        # already observed spool must not be mistaken for no new work.
        vanished = self.root / "vanished.jsonl"
        vanished.write_bytes(raw)
        existing = self.tail(vanished)
        existing.poll()
        vanished.unlink()
        with self.assertRaises(SpoolTailError):
            existing.poll()

    def test_corrupt_or_oversized_complete_records_never_become_eligible(self):
        self.add_facts()
        self.spool.mark_delivered(1, 10)
        self.spool.mark_delivered(2, 11)
        rows = [json.loads(line) for line in self.path.read_bytes().splitlines()]
        changed = json.loads(json.dumps(rows))
        embedded = json.loads(changed[1]["event_utf8"])
        embedded["run_id"] = "other-run"
        changed[1]["event_utf8"] = json.dumps(embedded, sort_keys=True, separators=(",", ":"))
        cases = {
            "embedded-run": b"\n".join(json.dumps(row).encode() for row in changed) + b"\n",
            "oversized": self.path.read_bytes() + json.dumps({"kind": "transport_retry", "error": "x" * 9000}).encode() + b"\n",
            "malformed": self.path.read_bytes() + b'{"kind":!}\n',
        }
        for name, raw in cases.items():
            path = self.root / (name + ".jsonl")
            path.write_bytes(raw)
            with self.subTest(case=name), self.assertRaises(SpoolTailError):
                self.tail(path, max_line_bytes=8192).poll()

    def assemblies(self):
        public, quality = mock.Mock(), mock.Mock()
        for assembly in (public, quality):
            assembly.failed = False
            assembly.complete.return_value = {"status": "complete", "spool": {"pending": 0}}
            assembly.finish.return_value = {"status": "complete"}
            assembly.abort.return_value = {"status": "failed"}
        public.is_drain_role, quality.is_drain_role = True, False
        return public, quality

    def supervisor(self, batches, *, confirm=None, max_attempts=2, monotonic_ns=None):
        public, quality = self.assemblies()
        def source():
            return batches.pop(0) if batches else ()

        def confirmation(attempt):
            _, _, payload, _ = self.payloads(attempt.attempt_id[-1])
            return PublicOutcome(payload, b"public-bytes")

        def qualification(attempt, observed):
            self.assertEqual(observed.receipt, b"public-bytes")
            return QualityOutcome(self.payloads(attempt.attempt_id[-1])[3])

        qualify = mock.Mock(side_effect=qualification)
        supervisor = VerifierSupervisor(public=public, quality=quality, source=source,
                                        confirm=confirm or confirmation, qualify=qualify, max_attempts=max_attempts,
                                        **({} if monotonic_ns is None else {"monotonic_ns": monotonic_ns}))
        return supervisor, public, quality, qualify

    def test_public_instant_and_identity_precede_quality_and_spool_wait(self):
        clock = [100]
        payload = self.payloads("a")[2]

        def confirm(_attempt):
            clock[0] = 140
            return PublicOutcome(payload, b"public-bytes")

        supervisor, public, _, qualify = self.supervisor(
            [(ReadyAttempt("att-a", 1, 2, 2),)], confirm=confirm, monotonic_ns=lambda: clock[0],
        )
        public.record.side_effect = lambda *args, **kwargs: clock.__setitem__(0, 400)

        def qualification(*_args):
            clock[0] = 900
            return QualityOutcome(self.payloads("a")[3])

        qualify.side_effect = qualification
        supervisor.step()
        record = supervisor.status()["attempts"][0]
        self.assertEqual(record["public_ns"], 140)
        self.assertEqual(record["finished_ns"], 900)
        self.assertEqual(record["public_confirmation_sha256"], payload.canonical_sha256())

    def test_evidence_flows_on_each_poll_and_drain_requires_exact_closed_set(self):
        supervisor, public, quality, _ = self.supervisor([
            (ReadyAttempt("att-a", 1, 2, 2),), (ReadyAttempt("att-b", 3, 4, 4),),
        ])
        self.assertEqual(supervisor.step(), 1)
        public.record.assert_called_once()
        quality.record.assert_called_once()
        public.finish.assert_not_called()
        with self.assertRaises(SupervisorRefused):
            supervisor.close(expected_attempts=frozenset({"att-a"}), write_receipt=mock.Mock(), runner_complete=False)
        self.assertEqual(supervisor.step(), 1)
        with self.assertRaises(SupervisorRefused):
            supervisor.close(expected_attempts=frozenset({"att-a"}), write_receipt=mock.Mock(), runner_complete=True)
        receipt = mock.Mock(return_value=m6.digest("actual-receipt"))
        result = supervisor.close(expected_attempts=frozenset({"att-a", "att-b"}),
                                  write_receipt=receipt, runner_complete=True)
        self.assertEqual(result["status"], "complete")
        quality.complete.assert_called_once()
        quality.finish.assert_not_called()
        public.finish.assert_called_once()
        self.assertEqual(json.loads(receipt.call_args.args[0])["attempt_set"], ["att-a", "att-b"])

    def test_verification_failures_are_visible_and_cannot_be_closed_as_success(self):
        for phase in ("public", "quality"):
            with self.subTest(phase=phase):
                error = OSError(phase + " disk failed")
                confirm = mock.Mock(side_effect=error) if phase == "public" else None
                supervisor, public, _, qualify = self.supervisor(
                    [(ReadyAttempt("att-a", 1, 2, 2),)], confirm=confirm,
                )
                if phase == "quality":
                    qualify.side_effect = error
                try:
                    supervisor.step()
                except SupervisorRefused:
                    pass
                if phase == "public":
                    qualify.assert_not_called()
                    self.assertIsNone(supervisor.status()["attempts"][0].get("public_ns"))
                    self.assertIsNone(supervisor.status()["attempts"][0].get("public_confirmation_sha256"))
                self.assertIn(str(error), json.dumps(supervisor.status()))
                self.assertIsNotNone(supervisor.failed, "a caught per-attempt error must prevent successful terminal closure")
                try:
                    result = supervisor.close(expected_attempts=frozenset({"att-a"}),
                                              write_receipt=mock.Mock(return_value=m6.digest("receipt")), runner_complete=True)
                except SupervisorRefused:
                    pass
                else:
                    self.assertEqual(result["status"], "failed")
                public.finish.assert_not_called()

    def test_bound_and_failed_drain_write_do_not_send_success(self):
        supervisor, public, _, _ = self.supervisor(
            [(ReadyAttempt("att-a", 1, 2, 2), ReadyAttempt("att-b", 3, 4, 4))], max_attempts=1,
        )
        with self.assertRaises(SupervisorRefused):
            supervisor.step()
        public.record.assert_not_called()
        supervisor, public, quality, _ = self.supervisor([(ReadyAttempt("att-a", 1, 2, 2),)])
        supervisor.step()
        with self.assertRaisesRegex(OSError, "receipt disk failed"):
            supervisor.close(expected_attempts=frozenset({"att-a"}),
                             write_receipt=mock.Mock(side_effect=OSError("receipt disk failed")), runner_complete=True)
        quality.complete.assert_called_once()
        public.abort.assert_called_once()
        public.finish.assert_not_called()
        for phase in ("quality-close", "receipt-and-abort"):
            with self.subTest(cleanup_failure=phase):
                supervisor, public, quality, _ = self.supervisor([(ReadyAttempt("att-a", 1, 2, 2),)])
                supervisor.step()
                write = mock.Mock(return_value=m6.digest("receipt"))
                if phase == "quality-close":
                    quality.complete.side_effect = OSError("quality close failed")
                    message = "quality close failed"
                else:
                    write.side_effect = OSError("first receipt failure")
                    public.abort.side_effect = RuntimeError("secondary cleanup failure")
                    message = "first receipt failure"
                with self.assertRaisesRegex(OSError, message):
                    supervisor.close(expected_attempts=frozenset({"att-a"}),
                                     write_receipt=write, runner_complete=True)
                public.abort.assert_called_once()
                public.finish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
