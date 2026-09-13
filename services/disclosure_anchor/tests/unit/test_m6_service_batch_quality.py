"""Independent fixed-quality batch wiring through actual local E1 lifecycle.

Synthetic baseline context and HTTP effects never qualify a real deployment.
The actual fixed verifier factory, owner, source child, reader and phases run.
"""

import base64
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import m6_service_batch as batch
from disclosure_anchor.adapters.runtime.m6_service_quality_verifier import ServiceQualityVerifier
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournalError
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from tests._m6_service_batch_fixture import (
    BatchFixture, JOURNAL_BYTES, canonical, digest, expected_reservation, journal_bytes,
)
from tests._m6_service_verifier_fixture import (
    CHECKS, NOW, VerifierFiles, alter_archive, leaves,
)
from tests._mineru_diagnostic_lifecycle_fixture import crash_after_phase, journal_records


class FixedQualityBatchTests(unittest.TestCase):
    def fixture(self, *, count=2):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        fixture = BatchFixture(root, count=count, max_in_flight=1)
        pixel = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')

        def real_tiny_media(files):
            for name in tuple(files):
                if name.endswith('.jpg'):
                    files[name] = pixel

        for simulated in fixture.simulations.values():
            alter_archive(simulated, real_tiny_media)
        config = VerifierFiles(root / 'verifier')
        verifier = config.load()
        self.assertIs(type(verifier), ServiceQualityVerifier)
        return fixture, config, verifier

    def expected_binding(self, fixture, service=None):
        value = {'contract_version': 'm6.service-batch-binding.v1', 'batch_id': 'independent-batch',
                 'inputs': fixture.original_binding_inputs(), 'api_url': fixture.kwargs['api_url'],
                 'server_url': 'http://vlm.invalid/v1', 'options': asdict(fixture.kwargs['options']),
                 'max_in_flight': 1, 'credits_limit': asdict(fixture.kwargs['credits_limit']),
                 'attempt_journals': {item.attempt_id: str(fixture.attempt_root(item.attempt_id)) for item in fixture.inputs},
                 'qualification_scope': 'functional_lifecycle_only_quality_unverified' if service is None else 'service_provider_integrity_only'}
        if service is not None:
            value['service_quality'] = {'contract_version': 'm6.service-verifier-binding.v1',
                                        'plan': deepcopy(service.plan),
                                        'baseline_contract': {**deepcopy(service.context), 'current': NOW.isoformat()}}
        return value

    def assert_pre_effect_rejection(self, fixture, overrides):
        with self.assertRaises((ValueError, DiagnosticJournalError)):
            fixture.run(**overrides)
        self.assertFalse(fixture.journal.exists())
        self.assertEqual(fixture.calls, [])
        self.assertTrue(all(not events for events in fixture.events.values()))

    def test_fixed_factory_two_input_refill_binds_and_forwards_actual_quality(self):
        fixture, config, verifier = self.fixture()
        result = fixture.run(service_quality_verifier=verifier)
        expected = self.expected_binding(fixture, config)
        records = journal_records(fixture.journal)
        self.assertEqual([r['step'] for r in records], ['batch_binding', 'dispatch_intent', 'attempt_disposed', 'dispatch_intent', 'attempt_disposed'])
        self.assertEqual(records[0]['value'], expected)
        header = json.loads((fixture.journal / '00-journal.json').read_bytes())
        self.assertEqual(header['configuration_sha256'], digest(canonical(expected)))
        self.assertEqual(result.qualification_scope, 'service_provider_integrity_only')
        self.assertEqual(result.controller.terminal, 'exhausted')
        self.assertEqual((result.unreconciled, result.not_dispatched), ((), ()))
        self.assertEqual(result.retained_or_unresolved_credits, ResourceCreditVector(temp_disk_bytes=3 * JOURNAL_BYTES))
        self.assertEqual(len(fixture.calls), 2)
        for call, item in zip(fixture.calls, fixture.inputs, strict=True):
            self.assertIs(call['service_quality_verifier'], verifier)
            self.assertFalse(call['resume'])
            self.assertFalse(call['require_disposed'])
            phases = journal_records(fixture.attempt_root(item.attempt_id))
            self.assertEqual(phases[0]['value']['service_quality'], expected['service_quality'])
            self.assertEqual(phases[0]['value']['quality_verifier_sha256'], verifier.identity_sha256)
            quality = fixture.proofs[item.attempt_id]['quality']
            self.assertEqual(quality['status'], 'pass')
            self.assertEqual(quality['verifier_sha256'], verifier.identity_sha256)
            report = quality['report']
            self.assertEqual(report['qualification']['verdict'], 'scorable')
            observation = report['evidence']['observation']
            self.assertEqual(observation['attempt_id'], item.attempt_id)
            self.assertEqual(observation['source_pdf_sha256'], item.source_pdf_sha256)
            self.assertEqual(observation['source_page_count'], 2)
            self.assertEqual([(c['check_id'], c['outcome']) for c in observation['checks']], [(name, 'pass') for name in CHECKS])
            self.assertEqual(observation['review_reasons'], [])
            self.assertEqual(phases[-1]['step'], 'disposed')
            simulated = fixture.simulations[item.attempt_id]
            self.assertEqual(simulated.events.count(('POST', '/tasks')), 1)
            self.assertEqual(simulated.ack_effects, 1)
            self.assertFalse(simulated.task_exists)
            self.assertFalse((fixture.attempt_root(item.attempt_id) / 'resources').exists())

    def test_default_omitted_and_none_keep_old_binding_bytes_and_call_signature(self):
        for explicit in (False, True):
            with self.subTest(explicit_none=explicit):
                fixture, _, _ = self.fixture(count=1)
                result = fixture.run(**({'service_quality_verifier': None} if explicit else {}))
                record = journal_records(fixture.journal)[0]
                expected = self.expected_binding(fixture)
                self.assertEqual(canonical(record['value']), canonical(expected))
                self.assertEqual(result.qualification_scope, 'functional_lifecycle_only_quality_unverified')
                self.assertNotIn('service_quality_verifier', fixture.calls[0])
                self.assertNotIn('service_quality', journal_records(fixture.attempt_root(fixture.inputs[0].attempt_id))[0]['value'])
                self.assertEqual(fixture.proofs[fixture.inputs[0].attempt_id]['quality']['status'], 'unverified')

    def test_wrong_type_subclass_source_and_target_reject_before_journal_or_child(self):
        class WrongSubclass(ServiceQualityVerifier):
            pass

        for wrong in (object(), object.__new__(WrongSubclass)):
            fixture, _, _ = self.fixture(count=1)
            self.assert_pre_effect_rejection(fixture, {'service_quality_verifier': wrong})
        fixture, _, verifier = self.fixture(count=1)
        with patch.object(batch, 'service_quality_verifier_identity_sha256', return_value='sha256:' + '0' * 64):
            self.assert_pre_effect_rejection(fixture, {'service_quality_verifier': verifier})
        fixture, _, verifier = self.fixture(count=1)
        self.assert_pre_effect_rejection(fixture, {'service_quality_verifier': verifier,
                                                  'options': replace(fixture.kwargs['options'], effort='high')})

    def test_successful_resume_forwards_same_factory_to_each_readonly_original_proof(self):
        fixture, _, verifier = self.fixture()
        first = fixture.run(service_quality_verifier=verifier)
        before = journal_bytes(fixture.root)
        events = fixture.events
        fixture.calls.clear()
        resumed = fixture.run(resume=True, service_quality_verifier=verifier)
        self.assertEqual(resumed.qualification_scope, 'service_provider_integrity_only')
        self.assertIsNone(resumed.controller)
        self.assertEqual(resumed.previously_disposed, first.controller.completed)
        self.assertEqual(resumed.retained_or_unresolved_credits, ResourceCreditVector(temp_disk_bytes=3 * JOURNAL_BYTES))
        self.assertEqual(journal_bytes(fixture.root), before)
        self.assertEqual(fixture.events, events)
        self.assertEqual(len(fixture.calls), 2)
        self.assertTrue(all(call['resume'] and call['require_disposed'] for call in fixture.calls))
        self.assertTrue(all(call['service_quality_verifier'] is verifier for call in fixture.calls))

    def test_resume_cannot_remove_or_replace_original_real_factory_binding(self):
        fixture, config, verifier = self.fixture()
        fixture.run(service_quality_verifier=verifier, stop_requested=lambda: True)
        original = journal_bytes(fixture.journal)
        alternate = VerifierFiles(fixture.root / 'alternate-verifier', raw=canonical(config.payload) + b'\n\n').load()
        self.assertNotEqual(alternate.binding_payload(), verifier.binding_payload())
        for changed in (None, alternate):
            with self.subTest(removing=changed is None), self.assertRaises(DiagnosticJournalError):
                fixture.run(resume=True, service_quality_verifier=changed)
            self.assertEqual(journal_bytes(fixture.journal), original)
            self.assertEqual(fixture.calls, [])
            self.assertTrue(all(not events for events in fixture.events.values()))

    def test_unknown_original_admission_failure_keeps_scope_error_and_full_ownership(self):
        fixture, _, verifier = self.fixture()
        marker = RuntimeError('independent guard at original POST rejects')

        def reject():
            raise marker

        with self.assertRaises(batch.ServiceBatchFailure) as caught:
            fixture.run(service_quality_verifier=verifier, before_submit=reject)
        result = caught.exception.result
        self.assertEqual(result.qualification_scope, 'service_provider_integrity_only')
        self.assertEqual(result.unreconciled, (fixture.inputs[0].attempt_id,))
        self.assertEqual(result.not_dispatched, (fixture.inputs[1].attempt_id,))
        self.assertEqual(result.retained_or_unresolved_credits,
                         expected_reservation(fixture.inputs[0]) + ResourceCreditVector(temp_disk_bytes=JOURNAL_BYTES))
        self.assertTrue(any(error is marker for error in leaves(caught.exception.__cause__.__cause__)))
        self.assertIs(fixture.calls[0]['service_quality_verifier'], verifier)
        self.assertEqual(fixture.calls[0]['deadline_ns'], fixture.deadline_ns)
        self.assertFalse(any(('POST', '/tasks') in events for events in fixture.events.values()))
        phases = journal_records(fixture.attempt_root(fixture.inputs[0].attempt_id))
        self.assertEqual(phases[-1]['step'], 'submit_intent')
        self.assertTrue((fixture.attempt_root(fixture.inputs[0].attempt_id) / 'resources/source.pdf').exists())

    def test_previously_dispatched_resume_forwards_factory_without_new_pdf_admission(self):
        fixture, _, verifier = self.fixture()
        with crash_after_phase('submit_reply'), self.assertRaises(batch.ServiceBatchFailure):
            fixture.run(service_quality_verifier=verifier)
        initial_events = fixture.events
        fixture.calls.clear()
        result = fixture.run(resume=True, service_quality_verifier=verifier)
        self.assertEqual(result.qualification_scope, 'service_provider_integrity_only')
        self.assertEqual(result.not_dispatched, (fixture.inputs[1].attempt_id,))
        self.assertEqual(result.unreconciled, ())
        self.assertEqual(len(fixture.calls), 1)
        call = fixture.calls[0]
        self.assertTrue(call['resume'])
        self.assertFalse(call['require_disposed'])
        self.assertIs(call['service_quality_verifier'], verifier)
        self.assertEqual(call['attempt_identity'], fixture.inputs[0].attempt_id)
        self.assertEqual(call['submission_epoch_unix'], 999)
        self.assertEqual(call['deadline_ns'], fixture.deadline_ns)
        self.assertEqual(fixture.events[fixture.inputs[1].attempt_id], initial_events[fixture.inputs[1].attempt_id])
        self.assertEqual(fixture.simulations[fixture.inputs[0].attempt_id].events.count(('POST', '/tasks')), 1)
        self.assertEqual(fixture.proofs[fixture.inputs[0].attempt_id]['quality']['status'], 'pass')

    def test_closed_failed_provider_does_not_invent_quality_and_refills_next(self):
        fixture, _, verifier = self.fixture()
        fixture.simulations[fixture.inputs[0].attempt_id].terminal_status = 'failed'
        result = fixture.run(service_quality_verifier=verifier)
        self.assertEqual(result.qualification_scope, 'service_provider_integrity_only')
        self.assertEqual([c.outcome for c in result.controller.completed], ['failed', 'completed'])
        self.assertEqual(result.unreconciled, ())
        first = fixture.proofs[fixture.inputs[0].attempt_id]
        self.assertIsNone(first['provider'])
        self.assertEqual(first['quality']['status'], 'not_applicable')
        self.assertEqual(fixture.proofs[fixture.inputs[1].attempt_id]['quality']['status'], 'pass')
        self.assertTrue(all(simulation.ack_effects == 1 and not simulation.task_exists for simulation in fixture.simulations.values()))


if __name__ == '__main__':
    unittest.main()
