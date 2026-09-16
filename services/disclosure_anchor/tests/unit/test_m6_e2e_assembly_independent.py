"""Independent durability/transport tests; scripted owner is not Windows evidence."""

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime.m6_e2e_assembly import M6E2EAssemblyWorker, M6LifecycleSpool
from disclosure_anchor.application.ports.staged_lifecycle_facts import AttemptAdmittedFact
from tests import m6_owner_support as owner_support
from tests import m6_support as m6


class AssemblyIndependentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "spool"
        self.fixture = m6.make_fixture("e2e_publication", {"a": (7, "fresh")})
        self.spec = self.fixture.spec
        self.spool = M6LifecycleSpool(
            self.directory, run_id=self.spec.run_id, spec_sha256=self.spec.canonical_sha256(),
            producer_epoch_sha256=m6.RUNNER_EPOCH, max_facts=2,
        )
        self.addCleanup(self.spool.close)
        member = self.fixture.entries["a"]
        self.fact = AttemptAdmittedFact(
            attempt_id="att-1", fence_identity="fence-1", document_id=member.document_id,
            processing_run_id="prun-1", source_pdf_sha256=member.source_pdf_sha256,
            source_byte_count=member.source_byte_count, source_page_count=7,
            process_profile_sha256=self.spec.runtime.process_profile_sha256,
        )

    def records(self):
        return [json.loads(line) for line in (self.directory / "spool.jsonl").read_bytes().splitlines()]

    def owner(self):
        return owner_support.ScriptedOwner(
            self.spec, owner_support.anchor_for(self.spec), owner_support.ManualClock(),
        )

    def answer(self, owner, *, outcome="ok"):
        owner.answer(
            owner.status(observed=self.fixture.at(10), state="failed" if outcome == "conflict" else "open",
                         last_sequence=1), outcome=outcome,
            error_code="producer_conflict" if outcome == "conflict" else None,
            record_for=lambda request: owner.stamp(request.command.event, sequence=1, tick=self.fixture.at(10)),
        )

    def run_worker(self, owner):
        threads = []

        def factory():
            threads.append(threading.get_ident())
            return owner_support.client_for(owner)

        worker = M6E2EAssemblyWorker(self.spool, client_factory=factory, retry_limit=2,
                                     backoff_seconds=0.05, poll_seconds=0.05)
        worker.start()
        status = worker.close(3)
        self.assertFalse(worker._thread.is_alive())
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())
        self.assertEqual(owner.close_calls, 1)
        return status

    def test_exact_replay_deduplicates_but_changed_fact_invalidates_run(self):
        self.spool.attempt_admitted(self.fact)
        self.spool.attempt_admitted(self.fact)
        entries = self.spool.pending()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].producer_sequence, 1)
        persisted = [row for row in self.records() if row["kind"] == "fact"]
        self.assertEqual(persisted[0]["event_utf8"], entries[0].event_utf8)
        self.spool.attempt_admitted(replace(self.fact, fence_identity="fence-changed"))
        self.assertTrue(self.spool.failed)
        self.assertEqual(self.spool.status()["conflicts"], 1)
        self.assertEqual(self.spool.pending(), entries)

    def test_short_os_write_must_complete_the_entire_durable_record(self):
        original_write = os.write

        def short_write(fd, data):
            return original_write(fd, data[:17])

        with mock.patch("disclosure_anchor.adapters.runtime.m6_e2e_assembly.os.write", side_effect=short_write):
            self.spool.attempt_admitted(self.fact)
        if self.spool.failed:
            self.assertEqual(self.spool.pending(), (), "an incomplete record cannot enter the delivery queue")
        else:
            rows = self.records()  # partial JSON must never be reported as durable
            self.assertEqual(rows[-1]["event_utf8"], self.spool.pending()[0].event_utf8)

    def test_fsync_failure_is_visible_and_never_queued(self):
        with mock.patch("disclosure_anchor.adapters.runtime.m6_e2e_assembly.os.fsync", side_effect=OSError("disk failed")):
            self.spool.attempt_admitted(self.fact)
        self.assertTrue(self.spool.failed)
        self.assertEqual(self.spool.status()["lost_facts"], 1)
        self.assertEqual(self.spool.pending(), ())

    def test_fact_bound_closed_spool_and_existing_run_fail_visibly(self):
        self.spool.attempt_admitted(self.fact)
        self.spool.attempt_admitted(replace(self.fact, attempt_id="att-2"))
        self.spool.attempt_admitted(replace(self.fact, attempt_id="att-3"))
        self.assertEqual(self.spool.status()["recorded"], 2)
        self.assertEqual(self.spool.status()["lost_facts"], 1)
        self.assertTrue(self.spool.failed)
        original = (self.directory / "spool.jsonl").read_bytes()
        with self.assertRaises(FileExistsError):
            M6LifecycleSpool(self.directory, run_id=self.spec.run_id, spec_sha256=self.spec.canonical_sha256(),
                             producer_epoch_sha256=m6.RUNNER_EPOCH, max_facts=2)
        self.assertEqual((self.directory / "spool.jsonl").read_bytes(), original)
        self.spool.close()
        self.spool.attempt_admitted(replace(self.fact, attempt_id="att-4"))
        self.assertEqual(self.spool.status()["lost_facts"], 2)

    def test_lost_reply_retries_original_event_bytes_and_sequence_on_actual_client(self):
        for error in (TimeoutError("lost reply"), EOFError("lost channel")):
            with self.subTest(error=type(error).__name__):
                # Each case gets a distinct producer spool, never an implicit resume.
                spool = M6LifecycleSpool(
                    Path(self.temp.name) / type(error).__name__, run_id=self.spec.run_id,
                    spec_sha256=self.spec.canonical_sha256(), producer_epoch_sha256=m6.RUNNER_EPOCH, max_facts=2,
                )
                self.addCleanup(spool.close)
                previous, self.spool = self.spool, spool
                try:
                    spool.attempt_admitted(self.fact)
                    owner = self.owner()
                    owner.raise_on_exchange(error)
                    self.answer(owner)
                    status = self.run_worker(owner)
                    self.assertFalse(status["failed"], status)
                    self.assertEqual(status["delivered"], 1)
                    self.assertEqual(status["pending"], 0)
                    self.assertEqual(len(owner.requests), 2)
                    self.assertEqual(owner.requests[0].command.event.canonical_bytes(),
                                     owner.requests[1].command.event.canonical_bytes())
                    self.assertEqual(owner.requests[1].command.event.producer_sequence, 1)
                    journal = Path(spool.status()["path"]).read_text()
                    self.assertIn(str(error), journal,
                                  "successful exact replay must retain the original transport error for diagnosis")
                finally:
                    self.spool = previous

    def test_owner_conflict_matching_submitted_variant_is_never_delivery(self):
        self.spool.attempt_admitted(self.fact)
        owner = self.owner()
        self.answer(owner, outcome="conflict")
        status = self.run_worker(owner)
        self.assertTrue(status["failed"])
        self.assertEqual(status["delivered"], 0)
        self.assertEqual(status["refused"], 1)
        self.assertEqual(len(owner.requests), 1)
        self.assertEqual(self.records()[-1]["kind"], "refused")

    def test_transport_exhaustion_is_bounded_and_no_fact_is_counted_delivered(self):
        self.spool.attempt_admitted(self.fact)
        owner = self.owner()
        for _ in range(3):
            owner.raise_on_exchange(TimeoutError("no response"))
        status = self.run_worker(owner)
        self.assertTrue(status["failed"])
        self.assertEqual(status["delivered"], 0)
        self.assertEqual(status["refused"], 1)
        self.assertEqual(len(owner.requests), 3)
        self.assertEqual(self.records()[-1]["error_code"], "transport_exhausted")

    def test_failed_or_stopped_sender_never_retains_new_admission_permission(self):
        """A once-granted lease must not leave the coordinator admitting after sender failure."""
        owner = self.owner()
        owner.answer(owner.status(observed=self.fixture.at(10), lease=self.fixture.at(10.5)))
        owner.raise_on_exchange(TimeoutError("control channel lost after lease"))
        self.spool.attempt_admitted(self.fact)
        worker = M6E2EAssemblyWorker(
            self.spool, client_factory=lambda: owner_support.client_for(owner),
            retry_limit=0, poll_seconds=0.05, backoff_seconds=0.05, lease_refresh_seconds=0.1,
        )
        worker.start()
        status = worker.close(3)
        self.assertFalse(worker._thread.is_alive())
        self.assertTrue(status["failed"])
        self.assertEqual([item.command.kind for item in owner.requests], ["lease", "append"])
        self.assertFalse(worker.admission_allowed(), "failed sender kept its previous granted lease visible")


    def test_blocked_exchange_cannot_extend_the_owner_lease_on_coordinator_thread(self):
        owner = self.owner()
        owner.answer(owner.status(observed=self.fixture.at(10), lease=self.fixture.at(10.5)))
        entered, release = threading.Event(), threading.Event()

        def delayed_reply(request):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test response withheld")
            record = owner.stamp(request.command.event, sequence=1, tick=self.fixture.at(10.1))
            return owner.reply(request, owner.status(observed=self.fixture.at(10.1), last_sequence=1), record=record)

        owner.handlers.append(delayed_reply)
        self.spool.attempt_admitted(self.fact)
        worker = M6E2EAssemblyWorker(
            self.spool, client_factory=lambda: owner_support.client_for(owner), retry_limit=0,
            poll_seconds=0.05, backoff_seconds=0.05, lease_refresh_seconds=30,
        )
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(worker.admission_allowed(), "the validated lease must initially be usable")
            owner.clock.advance(owner_support.NS)
            self.assertFalse(worker.admission_allowed(), "blocked control IO must not extend the lease")
        finally:
            release.set()
            status = worker.close(3)
        self.assertFalse(worker._thread.is_alive())
        self.assertFalse(status["failed"], status)
        self.assertEqual(status["delivered"], 1)
        self.assertFalse(worker.admission_allowed())

    def test_spool_io_failure_closes_admission_without_waiting_for_another_refresh(self):
        owner = self.owner()
        owner.answer(owner.status(observed=self.fixture.at(10), lease=self.fixture.at(10.5)))
        granted = threading.Event()

        def factory():
            client = owner_support.client_for(owner)
            refresh = client.refresh_admission

            def report_refresh():
                result = refresh()
                granted.set()
                return result

            client.refresh_admission = report_refresh
            return client

        worker = M6E2EAssemblyWorker(self.spool, client_factory=factory, lease_refresh_seconds=30,
                                     poll_seconds=0.05)
        worker.start()
        try:
            self.assertTrue(granted.wait(2))
            self.assertTrue(worker.admission_allowed())
            with mock.patch("disclosure_anchor.adapters.runtime.m6_e2e_assembly.os.fsync",
                            side_effect=OSError("disk failed after lease")):
                self.spool.attempt_admitted(self.fact)
            self.assertTrue(self.spool.failed)
            self.assertFalse(worker.admission_allowed())
        finally:
            worker.close(3)
        self.assertFalse(worker._thread.is_alive())



if __name__ == "__main__":
    unittest.main()
