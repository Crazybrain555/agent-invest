"""Exercise real verifier spools/senders against the existing accounting contract.

Only the SSH boundary is scripted. The emitted producer events are replayed by
the actual reducer, so a mocked successful finish cannot hide a role/order bug.
"""

from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime import m6_e2e_run as run_module
from disclosure_anchor.adapters.runtime.m6_e2e_run import M6RunDirectory, M6RunRole, M6VerifierAssembly
from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig
from disclosure_anchor.application.contracts.m6_run import M6PublicationMetrics
from disclosure_anchor.application.contracts.m6_run_events import M6DocumentQualified
from tests import m6_owner_support as owner
from tests import m6_support as m6


class VerifierCompletionIndependentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def directory(self, fixture):
        return M6RunDirectory(
            path=self.root, anchor=owner.anchor_for(fixture.spec), spec=fixture.spec,
            roles={"public_verifier": M6RunRole(m6.PUBLIC_EPOCH, "/fixture/public-token"),
                   "quality_verifier": M6RunRole(m6.QUALITY_EPOCH, "/fixture/quality-token")},
            ssh=ResidentSSHConfig(address="127.0.0.1", port=22, username="fixture",
                                  private_key_path="/fixture/key", known_hosts_path="/fixture/hosts"),
            remote_port=4444, lease=owner.policy(), pins={},
        )

    def assembly(self, fixture, role, transport):
        run = self.directory(fixture)
        epoch = run.epoch(role)
        with mock.patch.object(run_module, "m6_owner_client_factory",
                               return_value=lambda: owner.client_for(transport, role=role, epoch=epoch)):
            assembly = M6VerifierAssembly(
                run, role=role, spool_dir=self.root / (fixture.spec.mode + role),
                max_events=8, continuous_ns=transport.clock,
            )
        assembly.start()
        self.addCleanup(lambda: assembly.complete(deadline_seconds=2))
        return assembly

    def transport(self, fixture):
        return owner.ScriptedOwner(fixture.spec, owner.anchor_for(fixture.spec), owner.ManualClock())

    def test_wrong_mode_role_cannot_send_terminal_drain(self):
        for mode, role in (("e2e_publication", "quality_verifier"),
                           ("service_diagnostic", "public_verifier")):
            with self.subTest(mode=mode, role=role):
                fixture = m6.make_fixture(mode)
                transport = self.transport(fixture)
                assembly = self.assembly(fixture, role, transport)
                with self.assertRaises(ValueError):
                    assembly.finish(m6.digest("drain"), deadline_seconds=2)
                completed = assembly.complete(deadline_seconds=2)
                self.assertEqual(transport.requests, [], "invalid drain must be refused before SSH")
                self.assertEqual(completed["status"], "complete")
                self.assertFalse(completed["terminal_drain"])

    def test_ongoing_evidence_then_single_drain_reduces_to_complete(self):
        fixture = m6.make_fixture("e2e_publication", {"a": (7, "fresh"), "b": (5, "fresh")})
        journal = m6.Journal(fixture)
        journal.start()
        journal.opened(journal.at(1))
        admissions = [journal.admit(fixture.entries[key], "att-" + key, journal.at(2 + i))
                      for i, key in enumerate(("a", "b"))]
        for i, admission in enumerate(admissions):
            journal.accept(admission.attempt_id, journal.at(4 + i))
        transports = {role: self.transport(fixture) for role in ("public_verifier", "quality_verifier")}
        public = self.assembly(fixture, "public_verifier", transports["public_verifier"])
        quality = self.assembly(fixture, "quality_verifier", transports["quality_verifier"])

        def expect_delivery(role, tick):
            delivered = threading.Event()
            transport = transports[role]

            def respond(request):
                stamped = journal.stamp(request.command.event, journal.at(tick))
                reply = transport.reply(request, transport.status(
                    observed=journal.at(tick), last_sequence=stamped.stamp.sequence), record=stamped)
                delivered.set()
                return reply

            transport.answer_raw(respond)
            return delivered

        proofs = []
        history = []
        for i, admission in enumerate(admissions):
            tick = 10 + 10 * i
            source = fixture.entries[("a", "b")[i]]
            journal.commit(admission, journal.at(tick), ledger_seq=i + 1)
            # Reuse the established independent fixture's payload, not its stamp.
            payloads = m6.Journal(fixture)
            confirmation = payloads.confirm(admission, journal.at(tick + 1), ledger_seq=i + 1)
            public_seen = expect_delivery("public_verifier", tick + 1)
            public.record(confirmation, attempt_id=admission.attempt_id)
            self.assertTrue(public_seen.wait(3), "public evidence must flow before whole-campaign completion")
            proof = m6.qualification_for(source, "e2e_publication", admission.attempt_id)
            quality_seen = expect_delivery("quality_verifier", tick + 2)
            quality.record(M6DocumentQualified(
                attempt_id=admission.attempt_id, qualification_evidence_sha256=proof.canonical_sha256(),
            ), attempt_id=admission.attempt_id)
            self.assertTrue(quality_seen.wait(3), "quality must not wait for the final batch")
            journal.final(admission.attempt_id, journal.at(tick + 3))
            proofs.append(proof)
            history.append(m6.history_fact(source, attempt_id=admission.attempt_id, ledger_seq=i + 1))
        completed = quality.complete(deadline_seconds=3)
        self.assertEqual(completed["status"], "complete")
        self.assertFalse(completed["terminal_drain"])
        journal.stop_requested(journal.at(60))
        journal.stop_effective(journal.at(61))
        expect_delivery("public_verifier", 64)
        drained = public.finish(m6.digest("persisted-drain"), deadline_seconds=3)
        self.assertEqual(drained["status"], "complete")
        self.assertTrue(drained["terminal_drain"])
        journal.resources_closed(journal.at(65))
        journal.closed(journal.at(66))

        receipt = m6.reduce(fixture, journal, history=tuple(history), qualifications=tuple(proofs))
        self.assertEqual(receipt.status, "complete", receipt)
        self.assertEqual(receipt.metrics, M6PublicationMetrics(window_pages=12, whole_run_pages=12, carry_in_pages=0))
        for role, expected in (("quality_verifier", [1, 2]), ("public_verifier", [1, 2, 3])):
            events = [request.command.event for request in transports[role].requests]
            self.assertEqual([event.producer_sequence for event in events], expected)
        drains = [record.event for record in journal.records if record.event.payload.kind == "verifier_drained"]
        self.assertEqual(len(drains), 1)
        self.assertEqual(drains[0].producer_kind, "public_verifier")


if __name__ == "__main__":
    unittest.main()
