"""Literal old-v1 behavior must survive the new source/provider-only family."""

from copy import deepcopy
from types import SimpleNamespace
import unittest

from disclosure_anchor.application.contracts.m6_document_qualification import (
    M6QualificationEvidence, M6QualityPlan, qualify_document,
)
from tests._m6_service_quality_fixture import (
    LEGACY_CHECKS, canonical, legacy_evidence_payload, legacy_observation_payload,
    legacy_plan_payload, sha,
)


def decode(cls, payload):
    raw = canonical(payload)
    return cls.from_canonical_bytes(raw, maximum_bytes=len(raw))


class LegacyServiceQualityCompatibilityTests(unittest.TestCase):
    def test_legacy_service_and_publication_full_contract_canonical_output_is_unchanged(self):
        for mode in ('service_diagnostic', 'e2e_publication'):
            with self.subTest(mode=mode):
                plan = legacy_plan_payload(mode)
                observation = legacy_observation_payload(mode)
                evidence = legacy_evidence_payload(observation)
                actual = qualify_document(decode(M6QualificationEvidence, evidence), decode(M6QualityPlan, plan))
                self.assertEqual(actual.canonical_bytes(), canonical({
                    'contract_version': 'm6.document-qualification.v1',
                    'evidence_sha256': sha(canonical(evidence)), 'plan_sha256': sha(canonical(plan)),
                    'verdict': 'scorable', 'reasons': [], 'scorable_page_count': 2,
                }))
                self.assertEqual(decode(M6QualityPlan, plan).canonical_bytes(), canonical(plan))
                self.assertEqual(decode(M6QualificationEvidence, evidence).canonical_bytes(), canonical(evidence))

    def test_legacy_twelve_checks_and_unverified_unit_boundaries_do_not_shrink(self):
        for check in LEGACY_CHECKS:
            with self.subTest(check=check):
                plan = legacy_plan_payload()
                plan['required_checks'].remove(check)
                with self.assertRaises(ValueError):
                    decode(M6QualityPlan, plan)
        plan = decode(M6QualityPlan, legacy_plan_payload())
        for field, value, expected in (
            ('unusable_unit_count', 1, ['unusable_units_present']),
            ('needs_review_unit_count', 1, ['needs_review_reason_missing']),
        ):
            with self.subTest(field=field):
                obs = legacy_observation_payload()
                obs[field] = value
                result = qualify_document(decode(M6QualificationEvidence, legacy_evidence_payload(obs)), plan)
                self.assertEqual(result.reasons, tuple(expected))
                self.assertIsNone(result.scorable_page_count)
        obs = legacy_observation_payload()
        for item in obs['checks']:
            if item['check_id'] == 'independent_rebuild_match':
                item['outcome'] = 'unverified'
        result = qualify_document(decode(M6QualificationEvidence, legacy_evidence_payload(obs)), plan)
        self.assertEqual(result.reasons, ('check_unverified:independent_rebuild_match',))
        self.assertEqual(result.verdict, 'not_scorable')
        public = legacy_plan_payload('e2e_publication')
        public['required_checks'].remove('public_units_hash_match')
        with self.assertRaises(ValueError):
            decode(M6QualityPlan, public)

    def test_legacy_public_function_rejects_structurally_compatible_foreign_families(self):
        # The new family must not gain old authority by duck-typing. This is an
        # explicit new guard expectation, not claimed historical v1 behavior.
        plan = decode(M6QualityPlan, legacy_plan_payload())
        evidence = decode(M6QualificationEvidence, legacy_evidence_payload())
        foreign_plan = SimpleNamespace(**deepcopy(vars(plan)), canonical_sha256=plan.canonical_sha256)
        foreign_evidence = SimpleNamespace(**deepcopy(vars(evidence)), canonical_sha256=evidence.canonical_sha256)
        for left, right in ((foreign_evidence, plan), (evidence, foreign_plan)):
            with self.subTest(foreign=type(left).__name__ + '/' + type(right).__name__):
                with self.assertRaises(ValueError):
                    qualify_document(left, right)


if __name__ == '__main__':
    unittest.main()
