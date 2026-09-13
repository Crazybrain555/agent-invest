"""Independent scoped accounting; no physical owner or runtime claims."""

import json
import unittest

from disclosure_anchor.application.contracts.m6_service_quality import M6ServiceQualificationEvidence
from tests._m6_service_quality_fixture import canonical, h, sha
from tests._m6_service_run_fixture import (
    BOOT, OWNER, T0, Journal, RunFixture, decoded, simple_run,
)


def close_after_final(journal):
    deadline = journal.fixture.spec.deadline_ticks
    journal.emit({'kind': 'verifier_drained', 'drain_receipt_sha256': h('0')}, deadline + 7000)
    journal.emit({'kind': 'resources_closed', 'residual_count': 0, 'children_exited': True,
                  'ownership_receipt_sha256': h('1')}, deadline + 8000)
    journal.emit({'kind': 'run_closed', 'tclose_ticks': deadline + 10_000,
                  'reason': 'deadline_drained'}, deadline + 10_000)


class ServiceRunAccountingTest(unittest.TestCase):
    def test_literal_window_whole_replay_carry_and_repeated_source_totals(self):
        fixture = RunFixture({'a': (3, 'fresh'), 'b': (5, 'fresh'),
                              'c': (7, 'carry_in'), 'd': (11, 'replay')}, carry_ids=('carry-c',))
        journal = Journal(fixture)
        journal.start()
        names = (('a', 'attempt-a'), ('a', 'retry-a'), ('b', 'attempt-b'), ('c', 'carry-c'), ('d', 'attempt-d'))
        evidence = {attempt: fixture.evidence(label, attempt) for label, attempt in names}
        for index, (label, attempt) in enumerate(names, 1):
            journal.admit(label, attempt, tick=journal.at(index))
        for index, attempt in enumerate(('attempt-a', 'attempt-d', 'retry-a', 'carry-c'), 1):
            journal.qualified(evidence[attempt], attempt=attempt, tick=journal.at(index * 10))
            journal.validated(evidence[attempt], attempt=attempt, tick=journal.at(index * 10 + 1))
        deadline = fixture.spec.deadline_ticks
        journal.validated(evidence['attempt-b'], attempt='attempt-b', tick=deadline - 1)
        journal.qualified(evidence['attempt-b'], attempt='attempt-b', tick=deadline)
        journal.emit({'kind': 'stop_admission_effective'}, deadline + 1)
        for index, (_, attempt) in enumerate(names, 1):
            journal.final(attempt, tick=deadline + index * 1000)
        close_after_final(journal)
        result = fixture.reduce(journal, evidence.values())
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.metrics.model_dump(mode='json'), {
            'kind': 'service_provider_integrity_source_pages', 'window_pages': 14,
            'whole_run_pages': 19, 'replay_pages': 11, 'carry_in_pages': 7})
        rows = {row.attempt_id: row for row in result.sources}
        self.assertEqual(rows['attempt-b'].ready_received_ticks, deadline)
        self.assertEqual(rows['attempt-b'].outcome, 'credited_whole_run_only')
        self.assertEqual(rows['carry-c'].outcome, 'carry_in')
        self.assertEqual(rows['attempt-d'].outcome, 'credited_window')
        self.assertEqual(result.elapsed_ticks, 3_610_000)
        self.assertEqual(result.tclose_ticks, deadline + 10_000)
        self.assertEqual([row.attempt_id for row in result.sources], sorted(evidence))
        self.assertEqual(result.canonical_bytes(), fixture.reduce(journal, evidence.values()).canonical_bytes())

    def test_validation_event_must_reference_exact_same_evidence_before_terminal_or_verdict_shortcuts(self):
        for shortcut in ('none', 'failed', 'quality_fail'):
            with self.subTest(shortcut=shortcut):
                changes = None
                if shortcut == 'quality_fail':
                    base = RunFixture().evidence().model_dump(mode='json')['observation']['checks']
                    base[0]['outcome'] = 'fail'
                    changes = {'checks': base}
                fixture, journal, evidence = simple_run(
                    evidence_changes=changes, failed=shortcut == 'failed', validation_reference=h('0'))
                # This other, complete evidence has the same bundle/source but another attempt.
                other = fixture.evidence(attempt='other-attempt')
                for index, raw in enumerate(journal.lines):
                    record = json.loads(raw)
                    if record['event']['payload']['kind'] == 'service_validated':
                        record['event']['payload']['validation_receipt_sha256'] = other.canonical_sha256()
                        record['stamp']['producer_event_sha256'] = sha(canonical(record['event']))
                        journal.lines[index] = canonical(record) + b'\n'
                result = fixture.reduce(journal, (evidence, other))
                self.assertEqual(result.status, 'invalid')
                self.assertIn('service_validation_evidence_mismatch', result.invalid_reasons)
                self.assertIsNone(result.metrics)

    def test_correctly_hashed_evidence_still_binds_attempt_source_target_verifier_and_bundle(self):
        mutations = {
            'attempt': ({'attempt_id': 'different-attempt'}, 'qualification_source_mismatch'),
            'source': ({'source_pdf_sha256': h('f')}, 'qualification_source_mismatch'),
            'bytes': ({'source_byte_count': 1004}, 'qualification_source_mismatch'),
            'pages': ({'source_page_count': 4, 'provider_page_count': 4}, 'qualification_source_mismatch'),
            'target': ({'parser_target_sha256': h('0')}, 'service_quality_identity_mismatch'),
            'verifier': ({'quality_verifier_sha256': h('0')}, 'service_quality_identity_mismatch'),
            'bundle': ({}, 'service_provider_bundle_mismatch'),
        }
        for failure, (changes, reason) in mutations.items():
            with self.subTest(failure=failure):
                fixture, journal, evidence = simple_run(evidence_changes=changes)
                if failure == 'bundle':
                    for index, raw in enumerate(journal.lines):
                        record = json.loads(raw)
                        if record['event']['payload']['kind'] == 'service_validated':
                            record['event']['payload']['provider_bundle_sha256'] = h('0')
                            record['stamp']['producer_event_sha256'] = sha(canonical(record['event']))
                            journal.lines[index] = canonical(record) + b'\n'
                result = fixture.reduce(journal, (evidence,))
                self.assertEqual(result.status, 'invalid')
                self.assertIn(reason, result.invalid_reasons)
                self.assertIsNone(result.metrics)

    def test_unverified_missing_check_and_unresolved_review_give_zero_whole_document_credit(self):
        for case in ('unverified', 'missing', 'review', 'page_mismatch'):
            with self.subTest(case=case):
                source = RunFixture().evidence().model_dump(mode='json')['observation']
                if case == 'unverified':
                    source['checks'][0].update(outcome='unverified', evidence_sha256=None)
                elif case == 'missing':
                    source['checks'].pop()
                elif case == 'review':
                    source['review_reasons'] = ['unplanned-review']
                else:
                    source['provider_page_count'] = 2
                fixture, journal, _ = simple_run(evidence_changes=source)
                evidence = decoded(M6ServiceQualificationEvidence,
                                   {'contract_version': 'm6.service-qualification-evidence.v1',
                                    'observation': source, 'reviews': []})
                result = fixture.reduce(journal, (evidence,))
                self.assertEqual(result.status, 'complete')
                self.assertEqual(result.metrics.whole_run_pages, 0)
                self.assertEqual(result.metrics.window_pages, 0)
                self.assertEqual(result.sources[0].page_count, 3)
                self.assertEqual(result.sources[0].outcome,
                                 'quality_review_pending' if case == 'review' else 'quality_not_scorable')

    def test_absent_facts_and_referenced_missing_evidence_keep_distinct_existing_outcomes(self):
        for case in ('no_qualified', 'no_validated', 'missing_evidence', 'failed_no_quality'):
            with self.subTest(case=case):
                fixture, journal, evidence = simple_run(
                    qualify=case not in ('no_qualified', 'failed_no_quality'),
                    validate=case not in ('no_validated', 'failed_no_quality'), failed=case == 'failed_no_quality')
                result = fixture.reduce(journal, () if case == 'missing_evidence' else (evidence,))
                if case == 'missing_evidence':
                    self.assertEqual(result.status, 'incomplete')
                    self.assertIn('qualification_evidence_missing', result.incomplete_reasons)
                    self.assertIsNone(result.metrics)
                else:
                    self.assertEqual(result.status, 'complete')
                    self.assertEqual(result.metrics.whole_run_pages, 0)
                self.assertEqual(result.sources[0].outcome, {
                    'no_qualified': 'qualification_missing', 'no_validated': 'confirmation_missing',
                    'missing_evidence': 'qualification_missing', 'failed_no_quality': 'failed'}[case])

    def test_missing_ack_or_residual_resources_prevents_any_trusted_numerator(self):
        for case in ('missing_final', 'residual'):
            with self.subTest(case=case):
                fixture, journal, evidence = simple_run(missing_final=case == 'missing_final')
                if case == 'residual':
                    for index, raw in enumerate(journal.lines):
                        record = json.loads(raw)
                        if record['event']['payload']['kind'] == 'resources_closed':
                            record['event']['payload']['residual_count'] = 1
                            record['stamp']['producer_event_sha256'] = sha(canonical(record['event']))
                            journal.lines[index] = canonical(record) + b'\n'
                result = fixture.reduce(journal, (evidence,))
                self.assertEqual(result.status, 'incomplete')
                self.assertIsNone(result.metrics)
                self.assertIn('cleanup_or_ack_unresolved' if case == 'missing_final' else 'residual_resources',
                              result.incomplete_reasons)

    def test_owner_and_producer_retries_deduplicate_but_wrong_producer_role_is_invalid(self):
        for case in ('owner_retry', 'producer_retry', 'wrong_role'):
            with self.subTest(case=case):
                fixture = RunFixture()
                evidence = fixture.evidence()
                journal = Journal(fixture)
                journal.start()
                journal.admit()
                journal.validated(evidence)
                record = journal.qualified(evidence, role='service_runner' if case == 'wrong_role' else None)
                if case == 'owner_retry':
                    journal.lines.append(journal.lines[-1])
                elif case == 'producer_retry':
                    journal.receive(record['event'], journal.at(3) + 1)
                journal.emit({'kind': 'stop_admission_effective'}, fixture.spec.deadline_ticks)
                journal.final()
                close_after_final(journal)
                result = fixture.reduce(journal, (evidence,))
                if case == 'wrong_role':
                    self.assertEqual(result.status, 'invalid')
                    self.assertIn('event_producer_role_mismatch', result.invalid_reasons)
                    self.assertIsNone(result.metrics)
                else:
                    self.assertEqual(result.status, 'complete')
                    self.assertEqual(result.duplicate_events, 1)
                    self.assertEqual(result.metrics.whole_run_pages, 3)

    def test_same_boot_resume_keeps_original_interval_and_cross_boot_or_drift_is_unqualified(self):
        for case in ('same', 'cross_boot', 'deadline_drift'):
            with self.subTest(case=case):
                fixture = RunFixture()
                evidence = fixture.evidence()
                journal = Journal(fixture)
                journal.start()
                journal.owner_epoch = h('0')
                journal.boot = h('0') if case == 'cross_boot' else BOOT
                journal.emit({'kind': 'owner_resumed', 'clock': fixture.spec_payload['clock'],
                              't0_ticks': T0, 'deadline_ticks': fixture.spec.deadline_ticks + (case == 'deadline_drift'),
                              'previous_owner_epoch_sha256': OWNER}, T0 + 500)
                journal.admit()
                journal.validated(evidence)
                journal.qualified(evidence)
                journal.emit({'kind': 'stop_admission_effective'}, fixture.spec.deadline_ticks)
                journal.final()
                close_after_final(journal)
                result = fixture.reduce(journal, (evidence,))
                if case == 'same':
                    self.assertEqual(result.status, 'complete')
                    self.assertEqual(result.elapsed_ticks, 3_610_000)
                    self.assertEqual(result.deadline_ticks, fixture.spec.deadline_ticks)
                else:
                    self.assertIsNone(result.metrics)
                    if case == 'cross_boot':
                        self.assertIsNone(result.elapsed_ticks)
                        self.assertIn('boot_identity_changed', result.incomplete_reasons)
                    else:
                        self.assertIn('original_clock_or_deadline_drift', result.invalid_reasons)

    def test_formal_interval_requires_3600_and_close_coverage_while_short_batch_can_be_short(self):
        for phase in ('hour_baseline', 'stability_repeat'):
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                RunFixture(phase=phase, seconds=3599)
        fixture = RunFixture(seconds=3600)
        journal = Journal(fixture)
        journal.start()
        deadline = fixture.spec.deadline_ticks
        journal.emit({'kind': 'stop_admission_effective'}, deadline - 4)
        journal.emit({'kind': 'verifier_drained', 'drain_receipt_sha256': h('0')}, deadline - 3)
        journal.emit({'kind': 'resources_closed', 'residual_count': 0, 'children_exited': True,
                      'ownership_receipt_sha256': h('1')}, deadline - 2)
        journal.emit({'kind': 'run_closed', 'tclose_ticks': deadline - 1, 'reason': 'stop_requested'}, deadline - 1)
        result = fixture.reduce(journal)
        self.assertEqual(result.status, 'incomplete')
        self.assertIn('formal_interval_not_covered', result.incomplete_reasons)
        self.assertIsNone(result.metrics)
        short = RunFixture(phase='short_batch', seconds=3)
        short_journal = Journal(short)
        short_journal.start()
        short_journal.close()
        self.assertEqual(short.reduce(short_journal).status, 'complete')

    def test_admission_at_deadline_is_retained_but_never_credited(self):
        fixture = RunFixture()
        evidence = fixture.evidence()
        journal = Journal(fixture)
        journal.start()
        deadline = fixture.spec.deadline_ticks
        journal.admit(tick=deadline)
        journal.validated(evidence, tick=deadline + 100)
        journal.qualified(evidence, tick=deadline + 200)
        journal.emit({'kind': 'stop_admission_effective'}, deadline + 500)
        journal.final()
        close_after_final(journal)
        result = fixture.reduce(journal, (evidence,))
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.sources[0].outcome, 'admitted_after_deadline')
        self.assertEqual(result.metrics.whole_run_pages, 0)
        self.assertEqual(result.metrics.window_pages, 0)

    def test_consumed_prefix_bounds_partial_tail_and_iterator_errors_are_not_repaired(self):
        for case in ('partial', 'oversized', 'event_bound', 'malformed'):
            with self.subTest(case=case):
                limits = {'max_events': 6, 'max_attempts': 1} if case == 'event_bound' else None
                fixture = RunFixture(resources=limits)
                journal = Journal(fixture)
                journal.start()
                journal.close()
                before = b''.join(journal.lines)
                tail = {'partial': b'{"partial":', 'oversized': b'x' * 8194,
                        'event_bound': journal.lines[0], 'malformed': b'{}\n'}[case]
                journal.lines.append(tail)
                result = fixture.reduce(journal)
                consumed = before if case in ('oversized', 'event_bound') else before + tail
                self.assertEqual(result.journal_prefix_sha256, sha(consumed))
                self.assertEqual(result.journal_bytes_consumed, len(consumed))
                self.assertEqual(result.events_total, 6 if case in ('oversized', 'event_bound') else 7)
                self.assertIsNone(result.metrics)
                self.assertIn({'partial': 'event_log_truncated', 'oversized': 'event_log_bound_exceeded',
                               'event_bound': 'event_log_bound_exceeded', 'malformed': 'malformed_event_record'}[case],
                              result.invalid_reasons if case == 'malformed' else result.incomplete_reasons)
        fixture = RunFixture()
        journal = Journal(fixture)
        failure = OSError('independent original journal read error')

        def failed_iterator():
            yield b'{}\n'
            raise failure

        with self.assertRaises(OSError) as caught:
            fixture.reduce(journal, journal_lines=failed_iterator())
        self.assertIs(caught.exception, failure)
        with self.assertRaises(TypeError):
            fixture.reduce(journal, journal_lines=['not bytes'])


if __name__ == '__main__':
    unittest.main()
