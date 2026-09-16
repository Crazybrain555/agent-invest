"""Independent checks at the real durable V4 lifecycle notification boundaries."""

from dataclasses import replace
import hashlib
import unittest
from unittest import mock

from disclosure_anchor.application.services.staged_coordinator_backend_v4 import (
    DurableStagedCoordinatorBackendV4,
)
from disclosure_anchor.application.services.staged_parse_coordinator import AdmissionInterrupted
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.ports.staged_provider_parser import MaterializedProviderDocumentV4
from tests.unit import test_staged_coordinator_backend_v4 as backend_fixture
from tests.unit import test_staged_new_work_admission_v4 as admission_fixture
from tests.unit import test_atomic_document_publication_v4 as publication_fixture


class _Facts:
    def __init__(self, before_record=lambda: None):
        self.items = []
        self.before_record = before_record
        self.failed = False
        self.lost_fact_count = 0
        self.conflict_count = 0

    def attempt_admitted(self, fact):
        self.before_record()
        self.items.append(("attempt_admitted", fact))

    def remote_accepted(self, fact):
        self.before_record()
        self.items.append(("remote_accepted", fact))

    def publication_committed(self, fact):
        self.before_record()
        self.items.append(("publication_committed", fact))

    def attempt_final(self, fact):
        self.before_record()
        self.items.append(("attempt_final", fact))

    def fact_unavailable(self, kind, attempt_id, reason):
        self.failed = True
        self.lost_fact_count += 1
        self.items.append(("unavailable", (kind, attempt_id, reason)))


def _ack_backend(facts, receipt):
    authority = backend_fixture._authority("ack_pending")
    materialization = mock.Mock()
    materialization.acknowledge_v4.return_value = receipt
    base, persistence, inputs, _ = backend_fixture._backend(
        authority, materialization=materialization,
    )
    backend = DurableStagedCoordinatorBackendV4(
        persistence=persistence, inputs=inputs, remote=base._remote,
        materialization=materialization, secret_cipher=base._secret_cipher,
        claim_guard=base._claim_guard, publisher=base._publisher,
        poll_seconds=0.25, lifecycle_facts=facts,
    )
    return authority, backend, persistence


class LifecycleEmissionIndependentTests(unittest.TestCase):
    def test_publication_uses_committed_ledger_and_missing_read_does_not_undo_commit(self):
        for ledger in (173, OSError("ledger temporarily unavailable")):
            with self.subTest(ledger=ledger):
                facts = _Facts()
                authority = backend_fixture._authority("local_materialized")
                _, _, values, _, manifest, envelope, *_ = backend_fixture._typed_happy_bundle()
                materialization = mock.Mock()
                materialization.reopen_materialized_v4.return_value = MaterializedProviderDocumentV4(
                    receipt=values[6], intent=values[5], provider_envelope=envelope, manifest=manifest,
                )
                callback = mock.Mock()
                backend, persistence, _, _ = backend_fixture._backend(
                    authority, materialization=materialization, publication_committed=callback,
                )
                backend._lifecycle_facts = facts
                winner = publication_fixture._winner(publication_fixture._request())
                order = []

                def publish(**kwargs):
                    order.append("commit")
                    persistence.authority = backend_fixture._authority("publish_committed")
                    return winner

                def read_ledger(run_id):
                    self.assertEqual(persistence.authority.state, "publish_committed")
                    self.assertEqual(run_id, winner.processing_run_id)
                    order.append("read")
                    if isinstance(ledger, Exception):
                        raise ledger
                    return ledger

                backend._publisher.execute.side_effect = publish
                persistence.read_publication_ledger_seq = read_ledger
                result = backend.commit(backend_fixture._work(authority), credit_allowance=ResourceCreditVector(),
                                        stage_guard=backend_fixture._guard())
                self.assertEqual(result.state, "publish_committed")
                self.assertEqual(order, ["commit", "read"])
                callback.assert_called_once_with(False)
                self.assertEqual(len(facts.items), 1)
                if isinstance(ledger, int):
                    kind, fact = facts.items[0]
                    self.assertEqual(kind, "publication_committed")
                    self.assertEqual(fact.ledger_seq, 173)
                    self.assertEqual(fact.winner_sha256, winner.sha256)
                    self.assertEqual(fact.durable_base_sha256, winner.durable_base_commit.durable_base_sha256)
                    self.assertFalse(facts.failed)
                else:
                    self.assertTrue(facts.failed)
                    self.assertEqual(facts.items[0][0], "unavailable")

    def test_failed_publication_does_not_emit_a_committed_fact(self):
        facts = _Facts()
        authority = backend_fixture._authority("local_materialized")
        backend, _, _, _ = backend_fixture._backend(authority)
        backend._lifecycle_facts = facts
        backend._publisher.execute.side_effect = OSError("transaction failed")
        with mock.patch.object(backend, "_reopen_materialized", return_value=mock.sentinel.materialized):
            with self.assertRaisesRegex(OSError, "transaction failed"):
                backend.commit(backend_fixture._work(authority), credit_allowance=ResourceCreditVector(),
                               stage_guard=backend_fixture._guard())
        self.assertEqual(facts.items, [])

    def test_final_uses_durable_consumed_or_absence_ack_not_terminal_receipt(self):
        _, values, *_ = backend_fixture._fixture()
        consumed = values[9]
        absent = replace(consumed, ack_kind="absent", http_status=404, provider_receipt_identity=None)
        for ack in (consumed, absent):
            with self.subTest(kind=ack.ack_kind):
                facts = _Facts()
                authority, backend, persistence = _ack_backend(facts, ack)
                facts.before_record = lambda: self.assertEqual(len(persistence.appends), 1)
                with mock.patch.object(backend, "_capability", return_value=mock.sentinel.capability):
                    result = backend.acknowledge(
                        backend_fixture._work(authority), stage_guard=backend_fixture._guard(),
                    )
                self.assertEqual(result.state, "acked")
                self.assertEqual(len(facts.items), 1)
                kind, fact = facts.items[0]
                self.assertEqual(kind, "attempt_final")
                self.assertEqual(fact.outcome, "published")
                self.assertEqual(fact.remote_disposition, ack.ack_kind)
                self.assertEqual(fact.remote_receipt_sha256, ack.sha256)
                self.assertNotEqual(fact.remote_receipt_sha256, values[4].sha256)
                self.assertEqual(fact.cleanup_receipt_sha256, values[8].sha256)
                self.assertEqual(fact.remote_task_identity_sha256,
                                 "sha256:" + hashlib.sha256(ack.remote_task_identity.encode()).hexdigest())
                self.assertEqual(persistence.appends[0].successor.ack_receipt_sha256, ack.sha256)

    def test_failed_durable_ack_append_never_emits_final_fact(self):
        _, values, *_ = backend_fixture._fixture()
        facts = _Facts()
        authority, backend, persistence = _ack_backend(facts, values[9])
        with mock.patch.object(persistence, "append_successor", side_effect=OSError("commit unavailable")):
            with mock.patch.object(backend, "_capability", return_value=mock.sentinel.capability):
                with self.assertRaisesRegex(OSError, "commit unavailable"):
                    backend.acknowledge(backend_fixture._work(authority), stage_guard=backend_fixture._guard())
        self.assertEqual(facts.items, [])

    def test_post_claim_programming_error_retains_the_already_owned_work(self):
        helper = admission_fixture.StagedV4NewWorkAdmitterTests()
        admitter, _, claims, claimed, credit = helper._fixture()

        def reject():
            self.assertEqual(claims.claims, [claimed.attempt_id])
            raise ValueError("independent invalid notification")

        admitter._lifecycle_facts = _Facts(reject)
        with self.assertRaises(AdmissionInterrupted) as raised:
            helper._admit(admitter, limit=1, available_credits=credit.reservation)
        self.assertEqual(raised.exception.claimed_work, (claimed,))
        self.assertIn("independent invalid notification", str(raised.exception))

    def test_notification_io_failure_is_visible_without_losing_claimed_work(self):
        helper = admission_fixture.StagedV4NewWorkAdmitterTests()
        admitter, _, claims, claimed, credit = helper._fixture()
        facts = _Facts()

        def latch_io_failure():
            self.assertEqual(claims.claims, [claimed.attempt_id])
            facts.failed = True
            facts.lost_fact_count += 1

        facts.before_record = latch_io_failure
        admitter._lifecycle_facts = facts
        result = helper._admit(admitter, limit=1, available_credits=credit.reservation)
        self.assertEqual(result.work, (claimed,))
        self.assertTrue(facts.failed)
        self.assertEqual(facts.lost_fact_count, 1)
        self.assertEqual(len(facts.items), 1)


if __name__ == "__main__":
    unittest.main()
