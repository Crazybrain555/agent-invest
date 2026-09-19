"""Independent campaign-boundary tests against durable admission semantics."""
from threading import Event
import time
import unittest
from datetime import UTC, datetime, timedelta

from tests.unit import test_staged_parse_coordinator as coordinator_fixture

from disclosure_anchor.application.services import staged_campaign_runner as campaign_runner
from disclosure_anchor.application.services.staged_parse_coordinator import (
    StageLeaseGuard, StagedParseCoordinator, CoordinatorTerminal, ResourceCreditVector,
)
from tests._staged_campaign_sql import campaign
from tests.unit import test_staged_new_work_admission_v4 as admission_fixture


class CampaignRunnerIndependentTests(unittest.TestCase):
    def test_spool_fact_bound_counts_every_member_including_carry_in(self):
        from disclosure_anchor.application.contracts.m6_campaign import M6CampaignScope
        from tests import m6_support as m6
        entries = [m6.entry(f"carry-{i:02}", pages=2, mode="e2e_publication", origin="carry_in") for i in range(6)]
        entries.append(m6.entry("fresh-00", pages=2, mode="e2e_publication", origin="fresh"))
        manifest = m6.manifest("e2e_publication", *entries)
        request = campaign_runner.CampaignRunRequest(
            manifest=manifest, scope=M6CampaignScope.from_manifest(manifest), max_seconds=60,
        )
        self.assertEqual(len(request.admission_scope.ordinary_document_ids), 1)
        # six recovered attempts x (admitted, publication, final) alone need 18 facts; the old
        # ordinary-only bound (4 * 1 + 8 = 12) would have starved the spool.
        self.assertEqual(campaign_runner.campaign_spool_fact_bound(request), 4 * 7 + 8)
        fresh_only = campaign(count=8)
        fresh_request = campaign_runner.CampaignRunRequest(
            manifest=fresh_only.manifest, scope=fresh_only.scope, max_seconds=60,
        )
        self.assertEqual(campaign_runner.campaign_spool_fact_bound(fresh_request), 4 * 8 + 8)

    def test_observation_is_not_durable_admission_and_cannot_stop_first_claim(self):
        admitter, _source, claims, _work, envelope = admission_fixture.StagedV4NewWorkAdmitterTests()._fixture()
        scope = campaign((("doc-1", admission_fixture._SOURCE_SHA),))
        request = campaign_runner.CampaignRunRequest(
            manifest=scope.manifest, scope=scope.scope, max_seconds=60,
        )
        first = admitter.admit_new(limit=1, available_credits=envelope.reservation)
        self.assertEqual(first.work, ())
        self.assertIsNotNone(first.observation_request)
        result = admitter.observe(first.observation_request, stage_guard=StageLeaseGuard(
            deadline_monotonic=time.monotonic()+10, _revoked=Event(), _monotonic=time.monotonic,
        ))
        admitter.accept_observation(result)
        self.assertEqual(claims.claims, [], "only immutable PDF metadata was inspected")
        self.assertEqual(len(request.admission_scope.ordinary_document_ids), 1)
        state = campaign_runner.CampaignStopState()
        stop = campaign_runner.campaign_stop_predicate(
            monotonic=lambda: 0, deadline_monotonic=60, external_stop=lambda: False, state=state,
        )
        self.assertFalse(stop(), "one-member scope still permits its first durable claim after observation")
        actual = admitter.admit_new(limit=1, available_credits=envelope.reservation)
        self.assertEqual(len(actual.work), 1)
        self.assertTrue(claims.claims)

    def test_exact_manifest_scope_pins_fail_closed_before_runtime(self):
        scope = campaign(count=12)
        values = dict(manifest_bytes=scope.manifest.canonical_bytes(),
                      manifest_sha256=scope.manifest.canonical_sha256(),
                      scope_bytes=scope.scope.canonical_bytes(), scope_sha256=scope.scope.canonical_sha256())
        self.assertEqual(campaign_runner.load_campaign_inputs(**values), (scope.manifest, scope.scope))
        for mutation in ({'manifest_sha256': 'sha256:'+'0'*64}, {'scope_sha256': 'sha256:'+'0'*64},
                         {'manifest_bytes': values['manifest_bytes']+b'\n'},
                         {'scope_bytes': values['scope_bytes']+b'\n'}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                campaign_runner.load_campaign_inputs(**{**values, **mutation})

    def test_stop_deadline_and_external_signal_are_latched(self):
        for cause in ('deadline', 'external_stop'):
            with self.subTest(cause=cause):
                now, external = [0], [False]
                state = campaign_runner.CampaignStopState()
                stop = campaign_runner.campaign_stop_predicate(
                    monotonic=lambda: now[0], deadline_monotonic=60,
                    external_stop=lambda: external[0], state=state,
                )
                self.assertFalse(stop())
                if cause == 'deadline':
                    now[0] = 60
                else:
                    external[0] = True
                self.assertTrue(stop())
                self.assertEqual(state.reason, cause)
                now[0], external[0] = 0, False
                self.assertTrue(stop(), 'closed supply must not reopen after a transient stop signal')

    def test_twelve_members_flow_through_one_coordinator_without_eight_item_cutoff(self):
        scope = campaign(count=12)
        request = campaign_runner.CampaignRunRequest(manifest=scope.manifest, scope=scope.scope, max_seconds=60)
        backend = coordinator_fixture._Backend(new=tuple(
            coordinator_fixture._work(document, 'prepared') for document in request.admission_scope.document_ids
        ))
        state = campaign_runner.CampaignStopState()
        result = StagedParseCoordinator(backend=backend, limits=coordinator_fixture._limits(
            admission_batch_size=4, recovery_page_size=4,
        )).run(stop_requested=campaign_runner.campaign_stop_predicate(
            monotonic=lambda: 0, deadline_monotonic=60, external_stop=lambda: False, state=state,
        ))
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual((result.admitted, result.completed), (12, 12))
        self.assertEqual(result.credits_in_use, ResourceCreditVector())
        self.assertIsNone(state.reason)
        now = datetime(2026, 9, 16, tzinfo=UTC)
        identity = campaign_runner.CampaignRuntimeIdentity('owner', 'worker', 'process', 'runtime', 'capacity', 'activation')
        receipt = campaign_runner.campaign_receipt(request=request, identity=identity, result=result,
            started_utc=now, finished_utc=now+timedelta(seconds=4), monotonic_elapsed_s=4, stop_reason='quiescent')
        self.assertEqual((receipt['ordinary_member_count'], receipt['admitted'], receipt['completed']), (12, 12, 12))
        self.assertTrue(receipt['clean'])
        self.assertNotIn('admitted_document_ids', receipt, 'bounded final sample is not a complete membership history')
        self.assertIn('not a formal M6', receipt['authority'])

    def test_real_coordinator_drains_accepted_work_after_campaign_stop(self):
        for cause in ('deadline', 'external_stop'):
            with self.subTest(cause=cause):
                backend = coordinator_fixture._Backend(new=(coordinator_fixture._work('attempt-one', 'prepared'),))
                def reached_remote():
                    return any(call.startswith('remote:attempt-one') for call in backend.calls)
                state = campaign_runner.CampaignStopState()
                stop = campaign_runner.campaign_stop_predicate(
                    monotonic=lambda: 60 if cause == 'deadline' and reached_remote() else 0,
                    deadline_monotonic=60, external_stop=lambda: cause == 'external_stop' and reached_remote(), state=state,
                )
                result = StagedParseCoordinator(backend=backend, limits=coordinator_fixture._limits()).run(stop_requested=stop)
                self.assertEqual(state.reason, cause)
                self.assertEqual(result.final_states, (('attempt-one', 'acked'),))
                self.assertEqual(result.credits_in_use, ResourceCreditVector())
                self.assertNotIn('cancelled', ' '.join(backend.calls))



if __name__ == '__main__':
    unittest.main()
