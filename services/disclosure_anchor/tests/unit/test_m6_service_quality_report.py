"""Independent strict replay of a sealed service report, not an execution claim."""

from copy import deepcopy
import unittest

from disclosure_anchor.application.contracts import m6_service_quality as contract
from tests._m6_service_verifier_fixture import CHECKS, canonical, digest, h


def report_fixture(*, check_outcome='pass', review=False, hard_fail=False):
    plan = {'contract_version': 'm6.service-quality-plan.v1', 'mode': 'service_diagnostic',
            'parser_target_sha256': h('a'), 'parser_baseline_evidence_sha256': h('b'),
            'quality_verifier_sha256': h('c'), 'required_checks': list(CHECKS),
            'reason_policies': ([{'reason': 'review-x', 'disposition': (
                'score_hard_fail' if hard_fail else 'review_required')}] if review else [])}
    observation = {
        'mode': 'service_diagnostic', 'attempt_id': 'attempt-1',
        'source_pdf_sha256': h('d'), 'source_byte_count': 2000, 'source_page_count': 3,
        'provider_page_count': 3, 'provider_bundle_sha256': h('e'),
        'parser_target_sha256': h('a'), 'quality_verifier_sha256': h('c'),
        'source_observed_record_sha256': h('f'), 'output_sealed_record_sha256': h('0'),
        'review_reasons': ['review-x'] if review else [],
        'checks': [{'check_id': name, 'outcome': check_outcome if name == 'source_identity' else 'pass',
                    'evidence_sha256': None if name == 'source_identity' and check_outcome == 'unverified'
                    else h('f') if name == 'source_identity' else h('0')} for name in CHECKS],
    }
    evidence = {'contract_version': 'm6.service-qualification-evidence.v1',
                'observation': observation, 'reviews': []}
    reasons = []
    if check_outcome != 'pass':
        reasons.append('check_' + check_outcome + ':source_identity')
    if review:
        reasons.append(('review_rejected:' if hard_fail else 'review_pending:') + 'review-x')
    verdict = 'not_scorable' if check_outcome != 'pass' or hard_fail else 'review_pending' if review else 'scorable'
    qualification = {'contract_version': 'm6.service-document-qualification.v1',
                     'evidence_sha256': digest(canonical(evidence)), 'plan_sha256': digest(canonical(plan)),
                     'verdict': verdict, 'reasons': sorted(reasons),
                     'scorable_page_count': 3 if verdict == 'scorable' else None}
    parsed_plan = contract.M6ServiceQualityPlan.from_canonical_bytes(canonical(plan), maximum_bytes=8192)
    return parsed_plan, {'evidence': evidence, 'qualification': qualification}


class ServiceQualityReportTest(unittest.TestCase):
    def test_closed_replay_preserves_literal_complete_evidence_and_qualification(self):
        plan, report = report_fixture()
        evidence, qualification = contract.verify_service_quality_report(report, plan=plan)
        self.assertEqual(evidence.canonical_bytes(), canonical(report['evidence']))
        self.assertEqual(qualification.canonical_bytes(), canonical(report['qualification']))
        self.assertEqual(qualification.scorable_page_count, 3)
        report['evidence']['observation']['checks'].clear()
        self.assertEqual(len(evidence.observation.checks), 4)

    def test_replay_projects_unverified_pending_and_hard_failure_without_granting_pages(self):
        for values in ({'check_outcome': 'unverified'}, {'review': True},
                       {'review': True, 'hard_fail': True}, {'check_outcome': 'fail', 'review': True}):
            with self.subTest(values=values):
                plan, report = report_fixture(**values)
                _, qualification = contract.verify_service_quality_report(report, plan=plan)
                self.assertEqual(qualification.canonical_bytes(), canonical(report['qualification']))
                self.assertIsNone(qualification.scorable_page_count)

    def test_well_hashed_semantically_false_report_and_wrong_family_are_rejected(self):
        plan, original = report_fixture()
        for failure in ('extra', 'missing', 'old_family', 'target', 'false_pages', 'false_pass', 'wrong_plan'):
            with self.subTest(failure=failure):
                report = deepcopy(original)
                if failure == 'extra':
                    report['accepted'] = True
                elif failure == 'missing':
                    del report['qualification']
                elif failure == 'old_family':
                    report['evidence']['contract_version'] = 'm6.qualification-evidence.v1'
                elif failure == 'target':
                    report['evidence']['observation']['parser_target_sha256'] = h('0')
                    report['qualification']['evidence_sha256'] = digest(canonical(report['evidence']))
                elif failure == 'false_pages':
                    report['qualification']['scorable_page_count'] = 1
                elif failure == 'false_pass':
                    report['evidence']['observation']['checks'][0]['outcome'] = 'fail'
                    report['qualification']['evidence_sha256'] = digest(canonical(report['evidence']))
                else:
                    report['qualification']['plan_sha256'] = h('0')
                with self.assertRaises(ValueError):
                    contract.verify_service_quality_report(report, plan=plan)


if __name__ == '__main__':
    unittest.main()
