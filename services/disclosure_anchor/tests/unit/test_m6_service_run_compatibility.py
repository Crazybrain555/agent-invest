"""Independent old API receipt oracle and its formerly missing family guard."""

import unittest

from disclosure_anchor.application.services.m6_run_accounting import reduce_m6_run
from tests._m6_service_quality_fixture import canonical, sha
from tests._m6_service_run_fixture import Journal, RunFixture, T0, receipt_payload, simple_run


class ServiceRunCompatibilityTest(unittest.TestCase):
    def test_old_valid_service_output_remains_literal_v1_full_quality_bytes(self):
        fixture, journal, evidence = simple_run(legacy=True)
        actual = reduce_m6_run(spec=fixture.spec, manifest=fixture.manifest, quality_plan=fixture.plan,
                               journal_lines=journal.lines, qualifications=(evidence,), history=())
        expected = receipt_payload(legacy=True)
        expected.update(spec_sha256=sha(canonical(fixture.spec_payload)),
                        journal_prefix_sha256=sha(b''.join(journal.lines)),
                        journal_bytes_consumed=sum(map(len, journal.lines)), events_total=len(journal.lines),
                        sources=[{'source_pdf_sha256': fixture.entries['a']['source_pdf_sha256'],
                                  'attempt_id': 'attempt-a', 'outcome': 'credited_window', 'page_count': 3,
                                  'ready_received_ticks': T0 + 3000, 'admission_received_ticks': T0 + 1000}])
        expected['metrics'].update(window_pages=3, whole_run_pages=3)
        self.assertEqual(actual.canonical_bytes(), canonical(expected))

    def test_old_api_rejects_new_family_even_for_empty_or_failed_only_run(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                fixture = RunFixture()
                journal = Journal(fixture)
                journal.start()
                if failed:
                    journal.admit(accepted=False)
                    journal.final(failed=True, submitted=False, tick=journal.at(4))
                journal.close()
                with self.assertRaises(ValueError):
                    reduce_m6_run(spec=fixture.spec, manifest=fixture.manifest, quality_plan=fixture.plan,
                                  journal_lines=journal.lines, qualifications=(), history=())
        legacy = RunFixture(legacy=True)

        class Untouched:
            def __iter__(self):
                raise AssertionError('old family error must be detected before journal consumption')

        with self.assertRaises(ValueError):
            reduce_m6_run(spec=legacy.spec, manifest=legacy.manifest, quality_plan=legacy.plan,
                          journal_lines=Untouched(), qualifications=(RunFixture().evidence(),), history=())


if __name__ == '__main__':
    unittest.main()
