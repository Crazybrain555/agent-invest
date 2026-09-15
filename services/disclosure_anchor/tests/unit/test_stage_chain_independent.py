"""Real router/executor/use-case orchestration; only external IO is substituted."""
import json
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.application.contracts.semantic_routes import SemanticDocumentContext, semantic_route_receipt_to_payload
from disclosure_anchor.application.ports.staged_execution import current_semantic_group, semantic_group_scope
from disclosure_anchor.application.services.semantic_adjudication import OrderedSemanticAdjudicationExecutor
from disclosure_anchor.application.services.semantic_router import SemanticRouter
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseLost
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import AtomicPublicationCommitResponseLost
from tests.unit.test_semantic_execution_guard import _Provider, _configured
from tests.unit import test_semantic_execution_guard as guard_fixtures
from tests.unit.test_semantic_adjudication import _Cache, _batch
from tests.unit.test_semantic_router import _two_model_drafts, _taxonomy, _select_forecast_summary
from tests.unit.test_prepare_and_publish_whole_document_v4 import _Publisher
from tests.unit import test_prepare_and_publish_whole_document_v4 as p_fixtures
from tests.unit.test_stage_observation_independent import Collector, guard


class SemanticObservationTests(unittest.TestCase):
    def test_real_two_group_route_receipts_unchanged_and_replay_preserves_sources(self):
        admitted, drafts = _two_model_drafts()
        document = SemanticDocumentContext(title='某公司业绩预告', filing_type='performance_forecast')
        results=[]
        for observer in (None, Collector()):
            provider = _Provider(decide=_select_forecast_summary)
            router = SemanticRouter(taxonomy=_taxonomy(), batch_size=1,
                executor=OrderedSemanticAdjudicationExecutor((_configured(provider,_Cache()),)))
            result=router.route(admitted=admitted, drafts=drafts, document=document, stage_guard=guard(observer))
            replay=router.replay(admitted=admitted,drafts=drafts,document=document,receipts=result.receipts)
            self.assertEqual(replay.units,result.units)
            self.assertEqual(provider.calls,2)
            results.append(result)
            if observer:
                kinds=[r.kind for r in observer.records]
                self.assertLess(kinds.index('semantic_inputs_prepared'),kinds.index('provider_call_started'))
                self.assertEqual(kinds.count('provider_call_started'),2)
                self.assertEqual(kinds.count('provider_call_ended'),2)
        self.assertEqual(results[0],results[1])
        self.assertEqual([json.dumps(semantic_route_receipt_to_payload(r), sort_keys=True, separators=(",", ":")) for r in results[0].receipts],
                         [json.dumps(semantic_route_receipt_to_payload(r), sort_keys=True, separators=(",", ":")) for r in results[1].receipts])

    def test_real_cache_hit_has_no_second_provider_event_and_context_restores(self):
        provider=_Provider()
        seen=Collector()
        groups=[]
        provider.on_result=lambda: groups.append(current_semantic_group())
        executor=OrderedSemanticAdjudicationExecutor((_configured(provider,_Cache()),))
        with semantic_group_scope('outer'):
            executor.adjudicate(_batch(),group_hash='sha256:'+'9'*64,stage_guard=guard(seen))
            self.assertEqual(current_semantic_group(),'outer')
            seen.records.clear()
            executor.adjudicate(_batch(),group_hash='sha256:'+'9'*64,stage_guard=guard(seen))
        self.assertEqual(groups,['sha256:'+'9'*64])
        self.assertEqual(provider.calls,1)
        self.assertIn('cache_hit',[r.kind for r in seen.records])
        self.assertNotIn('provider_call_started',[r.kind for r in seen.records])
        self.assertIsNone(current_semantic_group())

    def test_provider_guard_loss_keeps_exception_and_closes_started_observation(self):
        seen=Collector()
        live=guard(seen)
        provider=_Provider()
        error=StageLeaseLost('exact business error')
        def fail(_batch):
            raise error
        provider.decide=fail
        executor=OrderedSemanticAdjudicationExecutor((_configured(provider,_Cache()),))
        with self.assertRaises(StageLeaseLost) as caught:
            executor.adjudicate(_batch(),group_hash='sha256:'+'9'*64,stage_guard=live)
        self.assertIs(caught.exception,error)
        self.assertIsNone(current_semantic_group())
        self.assertEqual([r.kind for r in seen.records].count('provider_call_started'),1)
        self.assertEqual([r.kind for r in seen.records].count('provider_call_ended'),1)

    def test_both_real_adapters_release_slots_on_success_and_failure(self):
        harness=guard_fixtures.SemanticAdapterGuardTests()
        with tempfile.TemporaryDirectory() as tmp:
            for adapter in harness._adapters(tmp):
                for failure in (False,True):
                    with self.subTest(adapter=type(adapter).__name__,failure=failure):
                        seen=Collector()
                        side=RuntimeError('external process failure') if failure else harness._success
                        with mock.patch('disclosure_anchor.adapters.semantics.codex_cli._run_process',side_effect=side):
                            if failure:
                                with self.assertRaises(RuntimeError):
                                    adapter.adjudicate_with_result(_batch(),stage_guard=guard(seen))
                            else:
                                adapter.adjudicate_with_result(_batch(),stage_guard=guard(seen))
                        self.assertEqual([r.kind for r in seen.records if r.kind.startswith('slot_')],
                                         ['slot_requested','slot_acquired','slot_released'])


class TransactionObservationTests(unittest.TestCase):
    def test_response_loss_reconciliation_does_not_double_end_one_p(self):
        for found in (True,False):
            with self.subTest(found=found):
                harness=p_fixtures.PrepareAndPublishWholeDocumentV4Tests()
                harness.setUp()
                # Existing fixture has only document_id; observations need the real identity shape.
                harness.checkpoint.processing_run_id='run_1'
                harness.checkpoint.attempt_id='attempt_1'
                seen=Collector()
                lost=AtomicPublicationCommitResponseLost('lost')
                publisher=_Publisher(harness.events,commits=[lost] if found else [lost,harness.winner],
                                     reloads=[harness.winner if found else None])
                result=harness._use_case(publisher).execute(checkpoint=harness.checkpoint,
                    materialized=harness.materialized,claim=harness.claim,claim_guard=harness.guard,
                    stage_guard=guard(seen))
                self.assertIs(result,harness.winner)
                events=[r for r in seen.records if r.kind.startswith('transaction_p_')]
                self.assertEqual([r.kind for r in events],
                    ['transaction_p_started','transaction_p_ended']*(1 if found else 2))
                self.assertEqual(harness.events.count('commit'),1 if found else 2)

    def test_unexpected_p_failure_preserved_and_started_interval_closed(self):
        harness=p_fixtures.PrepareAndPublishWholeDocumentV4Tests()
        harness.setUp()
        harness.checkpoint.processing_run_id='run_1'
        harness.checkpoint.attempt_id='attempt_1'
        error=RuntimeError('publisher failed')
        seen=Collector()
        with self.assertRaises(RuntimeError) as caught:
            harness._use_case(_Publisher(harness.events,commits=[error])).execute(
                checkpoint=harness.checkpoint,materialized=harness.materialized,claim=harness.claim,
                claim_guard=harness.guard,stage_guard=guard(seen))
        self.assertIs(caught.exception,error)
        self.assertEqual([r.kind for r in seen.records if r.kind.startswith('transaction_p_')],
                         ['transaction_p_started','transaction_p_ended'])
