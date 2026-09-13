"""Independent versioned receipt and new-family entry boundary."""

from copy import deepcopy
import unittest

from disclosure_anchor.application.contracts.m6_run import M6RunReceipt
from tests._m6_service_quality_fixture import canonical
from tests._m6_service_run_fixture import Journal, RunFixture, receipt_payload


class ServiceRunContractTest(unittest.TestCase):
    def test_literal_receipt_roundtrip_is_new_scoped_family_and_complete_zero(self):
        from disclosure_anchor.application.contracts.m6_service_run import M6ServiceRunReceipt

        payload = receipt_payload()
        receipt = M6ServiceRunReceipt.from_canonical_bytes(canonical(payload), maximum_bytes=8192)
        self.assertEqual(receipt.canonical_bytes(), canonical(payload))
        self.assertEqual(receipt.metrics.whole_run_pages, 0)
        self.assertEqual(receipt.contract_version, 'm6.service-run-receipt.v1')
        with self.assertRaises(ValueError):
            M6RunReceipt.from_canonical_bytes(canonical(payload), maximum_bytes=8192)
        with self.assertRaises(ValueError):
            M6ServiceRunReceipt.from_canonical_bytes(canonical(receipt_payload(legacy=True)), maximum_bytes=8192)
        payload['metrics']['whole_run_pages'] = 99
        self.assertEqual(receipt.metrics.whole_run_pages, 0)

    def test_closed_strict_projection_refuses_old_metrics_false_closure_and_partial_elapsed(self):
        from disclosure_anchor.application.contracts.m6_service_run import M6ServiceRunReceipt

        for failure in ('extra', 'mode', 'bool', 'old_kind', 'publication_kind', 'incomplete_with_metrics',
                        'missing_metrics', 'elapsed', 'zero_elapsed', 'window_subset', 'replay_subset', 'version'):
            with self.subTest(failure=failure):
                payload = receipt_payload()
                if failure == 'extra':
                    payload['unit_count'] = 0
                elif failure == 'mode':
                    payload['mode'] = 'e2e_publication'
                elif failure == 'bool':
                    payload['metrics']['whole_run_pages'] = True
                elif failure == 'old_kind':
                    payload['metrics']['kind'] = 'service_validated_source_pages'
                elif failure == 'publication_kind':
                    payload['metrics'] = {'kind': 'first_qualified_publication', 'window_pages': 0,
                                          'whole_run_pages': 0, 'carry_in_pages': 0}
                elif failure == 'incomplete_with_metrics':
                    payload.update(status='incomplete', incomplete_reasons=['cleanup_or_ack_unresolved'])
                elif failure == 'missing_metrics':
                    payload['metrics'] = None
                elif failure == 'elapsed':
                    payload['elapsed_ticks'] += 1
                elif failure == 'zero_elapsed':
                    payload.update(tclose_ticks=payload['t0_ticks'], elapsed_ticks=0)
                elif failure in ('window_subset', 'replay_subset'):
                    payload['metrics']['window_pages' if failure == 'window_subset' else 'replay_pages'] = 1
                else:
                    payload['contract_version'] = 'm6.run-receipt.v1'
                with self.assertRaises(ValueError):
                    M6ServiceRunReceipt.from_canonical_bytes(canonical(payload), maximum_bytes=8192)
        for status in ('incomplete', 'invalid'):
            payload = receipt_payload()
            payload.update(status=status, metrics=None, tclose_ticks=None, elapsed_ticks=None)
            payload[status + '_reasons'] = ['original_evidence_missing']
            result = M6ServiceRunReceipt.from_canonical_bytes(canonical(payload), maximum_bytes=8192)
            self.assertEqual(result.status, status)
            self.assertIsNone(result.metrics)

    def test_new_input_type_family_binding_and_evidence_bounds_refuse_before_iterator_consumption(self):
        class Untouched:
            def __iter__(self):
                raise AssertionError('invalid new reducer input consumed the journal')

        from disclosure_anchor.application.services.m6_service_run_accounting import reduce_m6_service_run

        fixture = RunFixture()
        legacy = RunFixture(legacy=True)
        evidence = fixture.evidence()
        cases = (
            {'quality_plan': legacy.plan}, {'qualifications': (legacy.evidence(),)},
            {'qualifications': [evidence]}, {'qualifications': (evidence, evidence)},
            {'qualifications': tuple(fixture.evidence(attempt='attempt-' + str(index)) for index in range(17))},
            {'spec': fixture.spec_payload}, {'manifest': fixture.manifest_payload},
        )
        for values in cases:
            with self.subTest(fields=tuple(values)), self.assertRaises(ValueError):
                arguments = {'spec': fixture.spec, 'manifest': fixture.manifest,
                             'quality_plan': fixture.plan, 'qualifications': (),
                             'journal_lines': Untouched()}
                arguments.update(values)
                reduce_m6_service_run(**arguments)
        # A valid but different spec binding is a programmer/configuration error.
        changed = deepcopy(fixture.spec_payload)
        changed['quality_plan_sha256'] = 'sha256:' + '0' * 64
        from disclosure_anchor.application.contracts.m6_run import M6RunSpec
        different = M6RunSpec.from_canonical_bytes(canonical(changed), maximum_bytes=8192)
        with self.assertRaises(ValueError):
            fixture.reduce(Journal(fixture), journal_lines=Untouched(), spec=different)


if __name__ == '__main__':
    unittest.main()
