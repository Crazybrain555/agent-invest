"""Independent durability/transport tests; scripted owner is not Windows evidence."""

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime.m6_e2e_assembly import (
    M6E2EAssemblyWorker, M6LeaseStartupError, M6LifecycleSpool,
)
from disclosure_anchor.application.ports.staged_lifecycle_facts import AttemptAdmittedFact
from disclosure_anchor.application.services.staged_campaign_runner import (
    CampaignStopState, campaign_stop_predicate,
)
from disclosure_anchor.cli.staged_campaign import _await_first_owner_lease
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

    def campaign_stop(self, worker):
        """The stop predicate `cli/staged_campaign.py` composes around this worker.

        Same shape as the entry: `inner() or worker.failed or not worker.admission_allowed()`,
        evaluated by the product's own predicate, whose first reason latches for the run.
        """
        state = CampaignStopState()
        stop_requested = campaign_stop_predicate(
            monotonic=lambda: 0.0, deadline_monotonic=1e9,
            external_stop=lambda: worker.failed or not worker.admission_allowed(), state=state)
        return stop_requested, state

    def new_spool(self, name):
        """One more spool in this test's own directory; each sender needs its own."""
        spool = M6LifecycleSpool(
            Path(self.temp.name) / name, run_id=self.spec.run_id,
            spec_sha256=self.spec.canonical_sha256(), producer_epoch_sha256=m6.RUNNER_EPOCH, max_facts=2,
        )
        self.addCleanup(spool.close)
        return spool

    def handshake(self, worker, *, bound_seconds):
        """Run the entry's own bounded handshake on its own thread; returns (thread, outcome)."""
        outcome = []

        def wait():
            try:
                _await_first_owner_lease(worker, bound_seconds=bound_seconds)
            except BaseException as exc:  # noqa: BLE001 - the case asserts the exact type
                outcome.append(exc)
            else:
                outcome.append("ready")

        thread = threading.Thread(target=wait, name="m6-handshake", daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        return thread, outcome

    def granting_owner(self):
        """A scripted owner that grants one lease inside the guard and keeps granting."""
        owner = self.owner()

        def grant(request):
            owner.handlers.append(grant)
            return owner.reply(request, owner.status(observed=self.fixture.at(10),
                                                     lease=self.fixture.at(10.5)))

        owner.handlers.append(grant)
        return owner

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

    def test_the_entry_waits_for_the_first_grant_before_its_stop_can_latch(self):
        """Startup sequencing: the bounded handshake runs before the latching predicate exists.

        Admission is closed while the sender is still building its client, and that is correct.
        What must not happen is the campaign reading that as an external stop: the entry holds
        in `_await_first_owner_lease` until the sender holds a grant, and only then builds the
        predicate whose first reason latches for the whole run.
        """
        owner = self.granting_owner()
        release, granted = threading.Event(), threading.Event()

        def factory():
            if not release.wait(3):
                raise TimeoutError("test withheld the client")
            client = owner_support.client_for(owner)
            original = client.refresh_admission

            def refresh():
                result = original()
                granted.set()
                return result

            client.refresh_admission = refresh
            return client

        worker = M6E2EAssemblyWorker(self.spool, client_factory=factory, lease_refresh_seconds=30,
                                     poll_seconds=0.05)
        worker.start()
        try:
            thread, outcome = self.handshake(worker, bound_seconds=10)
            self.assertFalse(worker.admission_allowed(), "no grant yet is not permission")
            self.assertFalse(worker.failed, "and it is not a failure either")
            # Returning here would need either a grant (withheld) or the whole 10 s bound.
            thread.join(0.2)
            self.assertTrue(thread.is_alive(), "the entry must still be holding the campaign")
            self.assertEqual(outcome, [])
            release.set()
            self.assertTrue(granted.wait(3))
            thread.join(3)
            self.assertEqual(outcome, ["ready"], "the handshake succeeds once the grant lands")
            self.assertTrue(worker.admission_allowed())
            # Only now does the campaign install its latching stop - and it does not latch.
            stop_requested, state = self.campaign_stop(worker)
            self.assertFalse(stop_requested())
            self.assertIsNone(state.reason)
        finally:
            release.set()
            worker.close(3)
        self.assertFalse(worker.admission_allowed())

    def test_a_sender_without_a_first_grant_fails_the_handshake_instead_of_latching(self):
        """Failure, bound and early exit each end the wait with a visible startup error."""
        for label in ("sender_failed", "no_grant_inside_the_bound", "sender_exited_first"):
            with self.subTest(case=label):
                owner = self.owner()
                spool = self.new_spool("spool-" + label)
                settled = threading.Event()
                if label == "sender_failed":
                    owner.raise_on_exchange(TimeoutError("control channel lost before the first lease"))
                else:
                    def refuse(request):
                        owner.handlers.append(refuse)
                        settled.set()
                        return owner.reply(request, owner.status(observed=self.fixture.at(10), lease=None))

                    owner.handlers.append(refuse)
                worker = M6E2EAssemblyWorker(spool, client_factory=lambda: owner_support.client_for(owner),
                                             lease_refresh_seconds=0.1, poll_seconds=0.05)
                worker.start()
                try:
                    bound = 0.05 if label == "no_grant_inside_the_bound" else 10
                    if label == "sender_exited_first":
                        self.assertTrue(settled.wait(3))
                        worker.close(3)
                    thread, outcome = self.handshake(worker, bound_seconds=bound)
                    thread.join(3)
                    self.assertEqual(len(outcome), 1, "the bounded wait must have ended")
                    self.assertIsInstance(outcome[0], M6LeaseStartupError)
                    message = str(outcome[0])
                    self.assertIn("new admission never opened", message)
                    if label == "sender_failed":
                        self.assertIn("the sender failed before holding a lease", message)
                    else:
                        self.assertIn("no lease was granted", message)
                    self.assertFalse(worker.admission_allowed())
                finally:
                    worker.close(3)

        # A sender with no configured refresh never requests a lease at all, so this handshake
        # has no meaning there: it is refused by name rather than silently timing out, and that
        # mode's own permission - a live sender - is untouched.
        owner = self.owner()
        worker = M6E2EAssemblyWorker(self.new_spool("spool-no-refresh"),
                                     client_factory=lambda: owner_support.client_for(owner),
                                     poll_seconds=0.05)
        worker.start()
        try:
            with self.assertRaises(RuntimeError) as refusal:
                worker.wait_for_first_lease(10)
            self.assertIn("requires a configured lease refresh", str(refusal.exception))
            self.assertTrue(worker.admission_allowed(), "a live sender is this mode's permission")
        finally:
            worker.close(3)

    def test_a_refresh_in_flight_keeps_an_unexpired_grant_and_only_expiry_closes_it(self):
        """The refresh window: pending is not lost, and a grant that really expires still closes."""
        owner = self.owner()
        clients, refreshed = [], []
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()

        def grant(request):
            owner.handlers.append(grant)
            return owner.reply(request, owner.status(observed=self.fixture.at(10),
                                                     lease=self.fixture.at(10.5)))

        def held_grant(request):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test withheld the lease reply")
            owner.handlers.append(grant)
            return owner.reply(request, owner.status(observed=self.fixture.at(10),
                                                     lease=self.fixture.at(10.5)))

        owner.handlers.extend([grant, held_grant])

        def factory():
            client = owner_support.client_for(owner)
            clients.append(client)
            original = client.refresh_admission

            def refresh():
                result = original()
                # Recorded on the sender thread, so no reading below is a race.
                refreshed.append((client.lease_until_ns, owner.clock.value))
                returned.set()
                return result

            client.refresh_admission = refresh
            return client

        worker = M6E2EAssemblyWorker(self.spool, client_factory=factory, lease_refresh_seconds=0.1,
                                     poll_seconds=0.05)
        stop_requested, state = self.campaign_stop(worker)
        worker.start()
        try:
            self.assertTrue(returned.wait(3), "the first refresh must grant a lease")
            first_grant, clock_at_grant = refreshed[0]
            self.assertGreater(first_grant, clock_at_grant)
            self.assertTrue(entered.wait(3), "the second refresh must reach the owner")
            client = clients[0]
            # The window r8 recorded as a false stop: the grant is still there and still valid.
            self.assertEqual(client.lease_until_ns, first_grant, "a pending refresh keeps the grant")
            self.assertEqual(owner.clock.value, clock_at_grant, "no time has passed")
            self.assertTrue(worker.admission_allowed(), "an unexpired grant stays admitted")
            self.assertFalse(stop_requested(), "the campaign does not stop for a pending refresh")
            self.assertIsNone(state.reason)
            # The same window closes the moment the grant actually expires, still in flight.
            owner.clock.advance(owner_support.NS)
            self.assertFalse(worker.admission_allowed(), "an expired grant is not kept alive")
            self.assertTrue(stop_requested(), "real expiry still closes new admission")
            self.assertEqual(state.reason, "external_stop")
        finally:
            release.set()
            worker.close(3)
        self.assertFalse(worker.admission_allowed())

    def test_a_due_refresh_is_taken_during_idle_and_backoff_without_disturbing_the_retry(self):
        """The refresh interval is a fraction of the grant; a long idle or backoff cannot eat it.

        Both phases are decided by construction rather than by wall time: the scripted owner
        answers in order, so a refresh that did not happen inside the wait would hand a lease
        reply to an append and the sender would fail on the spot.
        """
        owner = self.owner()
        idle_refresh, backoff_refresh = threading.Event(), threading.Event()

        def granting(event=None, last_sequence=0, repeat=False):
            def handler(request):
                if repeat:
                    owner.handlers.append(handler)
                if event is not None:
                    event.set()
                return owner.reply(request, owner.status(
                    observed=self.fixture.at(10), last_sequence=last_sequence,
                    lease=self.fixture.at(10.5)))

            return handler

        # Idle: the poll is ten times the refresh interval, so a second grant inside 0.4 s can
        # only come from the idle wait honouring the due refresh.
        owner.handlers.extend([granting(), granting(idle_refresh, repeat=True)])
        idle_worker = M6E2EAssemblyWorker(
            self.new_spool("idle-spool"), client_factory=lambda: owner_support.client_for(owner),
            lease_refresh_seconds=0.1, poll_seconds=1.0)
        idle_worker.start()
        try:
            self.assertTrue(idle_refresh.wait(0.4), "an idle poll must not swallow a due refresh")
        finally:
            idle_worker.close(3)
        self.assertEqual({item.command.kind for item in owner.requests}, {"lease"})

        def lose_the_reply(request):
            raise TimeoutError("reply lost")

        def stamped_append(request):
            return owner.reply(
                request, owner.status(observed=self.fixture.at(10), last_sequence=1),
                record=owner.stamp(request.command.event, sequence=1, tick=self.fixture.at(10)))

        # Backoff: one lost append reply, one due refresh inside the backoff, then the retry
        # with the original bytes. The backoff is shorter than two refresh intervals, so the
        # interleaving is exactly one lease between the two appends.
        owner.handlers.clear()
        owner.requests.clear()
        owner.handlers.extend([granting(), lose_the_reply, granting(backoff_refresh),
                               stamped_append, granting(last_sequence=1, repeat=True)])
        self.spool.attempt_admitted(self.fact)
        worker = M6E2EAssemblyWorker(self.spool, client_factory=lambda: owner_support.client_for(owner),
                                     retry_limit=2, lease_refresh_seconds=0.1, poll_seconds=0.1,
                                     backoff_seconds=0.15)
        worker.start()
        try:
            self.assertTrue(backoff_refresh.wait(3), "a retry backoff must not swallow a due refresh")
        finally:
            status = worker.close(3)
        kinds = [item.command.kind for item in owner.requests]
        self.assertEqual(kinds[:4], ["lease", "append", "lease", "append"], kinds)
        appends = [item for item in owner.requests if item.command.kind == "append"]
        self.assertEqual(appends[0].command.event.canonical_bytes(),
                         appends[1].command.event.canonical_bytes(),
                         "the retry re-sends the original producer bytes")
        self.assertFalse(status["failed"], status)
        self.assertEqual(status["delivered"], 1)

    def test_a_first_grant_that_already_expired_is_never_credited_as_admission(self):
        """The handshake reports that a grant happened, never that one is still live.

        `wait_for_first_lease` is sequencing, as its contract says. If that first grant has
        already expired when the entry looks, admission is still closed and the campaign stops
        on its own guard - no credit is invented anywhere.
        """
        owner = self.granting_owner()
        granted = threading.Event()

        def factory():
            client = owner_support.client_for(owner)
            original = client.refresh_admission

            def refresh():
                result = original()
                granted.set()
                return result

            client.refresh_admission = refresh
            return client

        worker = M6E2EAssemblyWorker(self.spool, client_factory=factory, lease_refresh_seconds=30,
                                     poll_seconds=0.05)
        worker.start()
        try:
            self.assertTrue(granted.wait(3))
            self.assertTrue(worker.admission_allowed())
            owner.clock.advance(owner_support.NS)
            self.assertTrue(worker.wait_for_first_lease(0.05),
                            "the handshake records that a grant happened")
            self.assertFalse(worker.admission_allowed(), "an expired grant is never admission")
            stop_requested, state = self.campaign_stop(worker)
            self.assertTrue(stop_requested(), "the campaign closes on the live guard, not the handshake")
            self.assertEqual(state.reason, "external_stop")
        finally:
            worker.close(3)

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
