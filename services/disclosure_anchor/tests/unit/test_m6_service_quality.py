"""Independent source/provider qualification contract; no adapter authority claim."""

from copy import deepcopy
import json
import unittest

from disclosure_anchor.application.contracts.m6_document_qualification import (
    M6QualificationEvidence, M6QualityPlan, qualify_document,
)
from disclosure_anchor.application.contracts.m6_service_quality import (
    M6_SERVICE_PROVIDER_CHECKS, M6ServiceCheckResult, M6ServiceDocumentQualification,
    M6ServiceQualificationEvidence, M6ServiceQualificationObservation,
    M6ServiceQualityPlan, qualify_service_document,
)
from tests._m6_service_quality_fixture import (
    SERVICE_CHECKS, canonical, evidence_payload, h, legacy_evidence_payload,
    legacy_plan_payload, observation_payload, plan_payload, review_payload, sha,
)


def decode(cls, payload):
    raw = canonical(payload)
    return cls.from_canonical_bytes(raw, maximum_bytes=len(raw))


def qualify(evidence=None, plan=None):
    return qualify_service_document(
        decode(M6ServiceQualificationEvidence, evidence or evidence_payload()),
        decode(M6ServiceQualityPlan, plan or plan_payload()),
    )


def projection(evidence, plan, verdict='scorable', reasons=(), pages=2):
    return {'contract_version': 'm6.service-document-qualification.v1',
            'evidence_sha256': sha(canonical(evidence)), 'plan_sha256': sha(canonical(plan)),
            'verdict': verdict, 'reasons': list(reasons), 'scorable_page_count': pages}


class ServiceQualityContractTests(unittest.TestCase):
    def test_literal_canonical_plan_observation_evidence_and_qualified_projection(self):
        plan, evidence = plan_payload(), evidence_payload()
        result = projection(evidence, plan)
        for cls, payload in (
            (M6ServiceCheckResult, observation_payload()['checks'][0]),
            (M6ServiceQualityPlan, plan),
            (M6ServiceQualificationObservation, evidence['observation']),
            (M6ServiceQualificationEvidence, evidence),
            (M6ServiceDocumentQualification, result),
        ):
            with self.subTest(record=cls.__name__):
                self.assertEqual(decode(cls, payload).canonical_bytes(), canonical(payload))
        self.assertEqual(M6_SERVICE_PROVIDER_CHECKS, SERVICE_CHECKS)
        self.assertEqual(qualify(evidence, plan).canonical_bytes(), canonical(result))
        self.assertNotIn('unit_count', result)
        self.assertNotIn('public_units_sha256', result)

    def test_complete_wire_rejects_extras_omissions_wrong_versions_and_noncanonical_json(self):
        for cls, payload in (
            (M6ServiceQualityPlan, plan_payload()),
            (M6ServiceQualificationEvidence, evidence_payload()),
            (M6ServiceDocumentQualification, projection(evidence_payload(), plan_payload())),
        ):
            for edit in ('extra', 'missing_version', 'old_version'):
                changed = deepcopy(payload)
                if edit == 'extra':
                    changed['unit_count'] = 0
                elif edit == 'missing_version':
                    changed.pop('contract_version')
                else:
                    changed['contract_version'] = 'm6.quality-plan.v1'
                with self.subTest(record=cls.__name__, edit=edit), self.assertRaises(ValueError):
                    decode(cls, changed)
        for raw in (canonical(plan_payload()) + b'\n', json.dumps(plan_payload(), indent=2).encode(),
                    b'{"mode":"service_diagnostic",' + canonical(plan_payload())[1:]):
            with self.subTest(raw=raw[:25]), self.assertRaises(ValueError):
                M6ServiceQualityPlan.from_canonical_bytes(raw, maximum_bytes=len(raw))

    def test_strict_scalar_types_ranges_modes_and_absent_unit_fields(self):
        for key in ('source_byte_count', 'source_page_count', 'provider_page_count'):
            for value in (True, False, 0, -1, 2**63, 2.0, '2', None):
                payload = observation_payload()
                payload[key] = value
                with self.subTest(field=key, value=value), self.assertRaises(ValueError):
                    decode(M6ServiceQualificationObservation, payload)
        for key in ('source_pdf_sha256', 'provider_bundle_sha256', 'parser_target_sha256',
                    'quality_verifier_sha256', 'source_observed_record_sha256', 'output_sealed_record_sha256'):
            for value in ('a' * 64, 'sha256:' + 'A' * 64, None, True):
                payload = observation_payload()
                payload[key] = value
                with self.subTest(field=key, value=value), self.assertRaises(ValueError):
                    decode(M6ServiceQualificationObservation, payload)
        for key, value in (('mode', 'e2e_publication'), ('attempt_id', 'bad id'),
                           ('processing_run_id', None), ('public_units_sha256', None),
                           ('unit_count', 0), ('unusable_unit_count', 0), ('needs_review_unit_count', 0)):
            payload = observation_payload()
            payload[key] = value
            with self.subTest(field=key), self.assertRaises(ValueError):
                decode(M6ServiceQualificationObservation, payload)

    def test_plan_has_exact_four_sorted_checks_and_no_legacy_invariant_substitution(self):
        invalid = [list(reversed(SERVICE_CHECKS)), list(SERVICE_CHECKS[:-1]), [],
                   [*SERVICE_CHECKS, 'public_units_hash_match'],
                   [*SERVICE_CHECKS[:3], SERVICE_CHECKS[0]],
                   [*SERVICE_CHECKS[:3], 'independent_rebuild_match']]
        for checks in invalid:
            payload = plan_payload()
            payload['required_checks'] = checks
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                decode(M6ServiceQualityPlan, payload)
        for key in ('parser_target_sha256', 'parser_baseline_evidence_sha256', 'quality_verifier_sha256'):
            payload = plan_payload()
            del payload[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                decode(M6ServiceQualityPlan, payload)

    def test_pass_and_fail_require_proof_but_an_unverified_hash_never_means_pass(self):
        for outcome in ('pass', 'fail'):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                decode(M6ServiceCheckResult, {'check_id': 'source_identity', 'outcome': outcome,
                                              'evidence_sha256': None})
        for evidence_sha in (None, h('f')):
            obs = observation_payload()
            obs['checks'][-1].update(outcome='unverified', evidence_sha256=evidence_sha)
            result = qualify(evidence_payload(obs))
            self.assertEqual(result.verdict, 'not_scorable')
            self.assertEqual(result.reasons, ('check_unverified:source_identity',))
            self.assertIsNone(result.scorable_page_count)
        with self.assertRaises(ValueError):
            decode(M6ServiceCheckResult, {'check_id': 'source_identity', 'outcome': 'not_applicable',
                                          'evidence_sha256': None})

    def test_each_missing_failed_or_unverified_check_blocks_all_source_pages(self):
        for check in SERVICE_CHECKS:
            for state in ('missing', 'fail', 'unverified'):
                obs = observation_payload()
                if state == 'missing':
                    obs['checks'] = [item for item in obs['checks'] if item['check_id'] != check]
                    expected = 'required_check_missing:' + check
                else:
                    next(item for item in obs['checks'] if item['check_id'] == check)['outcome'] = state
                    expected = 'check_' + state + ':' + check
                with self.subTest(check=check, state=state):
                    raw_evidence = evidence_payload(obs)
                    self.assertEqual(qualify(raw_evidence).canonical_bytes(), canonical(projection(
                        raw_evidence, plan_payload(), 'not_scorable', (expected,), None)))
        obs = observation_payload()
        obs['checks'] = []
        result = qualify(evidence_payload(obs))
        self.assertEqual(result.reasons, tuple('required_check_missing:' + name for name in SERVICE_CHECKS))

    def test_check_order_duplicates_and_foreign_check_ids_are_not_repaired(self):
        for checks in (list(reversed(observation_payload()['checks'])),
                       observation_payload()['checks'] + [observation_payload()['checks'][0]],
                       [observation_payload()['checks'][0]] * 2,
                       [{'check_id': 'page_closure', 'outcome': 'pass', 'evidence_sha256': h('1')} ]):
            obs = observation_payload()
            obs['checks'] = checks
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                decode(M6ServiceQualificationObservation, obs)

    def test_matching_page_counts_preserve_the_entire_source_and_mismatch_credits_none(self):
        for pages, source in ((1, '1'), (39, '2'), (2**63 - 1, '3')):
            obs = observation_payload()
            obs.update(source_pdf_sha256=h(source), source_page_count=pages, provider_page_count=pages)
            with self.subTest(pages=pages):
                self.assertEqual(qualify(evidence_payload(obs)).scorable_page_count, pages)
        for provider_pages in (1, 3):
            obs = observation_payload()
            obs['provider_page_count'] = provider_pages
            result = qualify(evidence_payload(obs))
            self.assertEqual(result.reasons, ('page_count_mismatch',))
            self.assertEqual(result.verdict, 'not_scorable')
            self.assertIsNone(result.scorable_page_count)

    def test_wrong_target_or_verifier_is_binding_error_not_soft_disposition(self):
        for key in ('parser_target_sha256', 'quality_verifier_sha256'):
            obs = observation_payload()
            obs[key] = h('9')
            with self.subTest(field=key), self.assertRaises(ValueError):
                qualify(evidence_payload(obs))

    def test_existing_reason_policy_and_review_decisions_keep_their_exact_meaning(self):
        reason = 'source_finding:source_pdf_native_text_quality.v3:numeric_token_truncation'
        cases = (
            (None, None, 'review_pending', 'unplanned_reason:'),
            (None, 'accept', 'review_pending', 'unplanned_reason:'),
            ('accepted_noncritical', None, 'scorable', None),
            ('accepted_noncritical', 'reject', 'not_scorable', 'review_rejected:'),
            ('review_required', None, 'review_pending', 'review_pending:'),
            ('review_required', 'accept', 'scorable', None),
            ('review_required', 'reject', 'not_scorable', 'review_rejected:'),
            ('score_hard_fail', 'accept', 'not_scorable', 'review_rejected:'),
        )
        for policy, decision, verdict, prefix in cases:
            plan, obs = plan_payload(), observation_payload()
            obs['review_reasons'] = [reason]
            if policy:
                plan['reason_policies'] = [{'reason': reason, 'disposition': policy}]
            reviews = [review_payload(obs, reason, decision)] if decision else []
            evidence = evidence_payload(obs, reviews)
            with self.subTest(policy=policy, decision=decision):
                self.assertEqual(qualify(evidence, plan).canonical_bytes(), canonical(projection(
                    evidence, plan, verdict, () if prefix is None else (prefix + reason,),
                    2 if verdict == 'scorable' else None)))

    def test_review_is_bound_to_exact_observation_bytes_and_actual_reported_reason(self):
        obs = observation_payload()
        obs['review_reasons'] = ['known']
        review = review_payload(obs, 'known')
        decode(M6ServiceQualificationEvidence, evidence_payload(obs, [review]))
        for changed in ('source', 'attempt', 'check_evidence', 'reason'):
            new_obs, new_review = deepcopy(obs), deepcopy(review)
            if changed == 'source':
                new_obs['source_byte_count'] += 1
            elif changed == 'attempt':
                new_obs['attempt_id'] = 'attempt-02'
            elif changed == 'check_evidence':
                new_obs['checks'][0]['evidence_sha256'] = h('8')
            else:
                new_review['reason'] = 'not-in-observation'
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                decode(M6ServiceQualificationEvidence, evidence_payload(new_obs, [new_review]))

    def test_review_order_uniqueness_and_any_reject_override_other_accepts(self):
        obs = observation_payload()
        obs['review_reasons'] = ['known']
        first = review_payload(obs, 'known', 'accept', 'reviewer-01')
        second = review_payload(obs, 'known', 'reject', 'reviewer-02')
        plan = plan_payload()
        plan['reason_policies'] = [{'reason': 'known', 'disposition': 'accepted_noncritical'}]
        result = qualify(evidence_payload(obs, [first, second]), plan)
        self.assertEqual(result.reasons, ('review_rejected:known',))
        self.assertIsNone(result.scorable_page_count)
        for reviews in ([second, first], [first, first]):
            with self.subTest(reviews=reviews), self.assertRaises(ValueError):
                decode(M6ServiceQualificationEvidence, evidence_payload(obs, reviews))

    def test_reason_and_review_256_bounds_are_exact_not_silently_truncated(self):
        names = [f'reason-{index:03}' for index in range(256)]
        plan, obs = plan_payload(), observation_payload()
        plan['reason_policies'] = [{'reason': name, 'disposition': 'review_required'} for name in names]
        obs['review_reasons'] = names
        reviews = [review_payload(obs, name) for name in names]
        self.assertEqual(qualify(evidence_payload(obs, reviews), plan).verdict, 'scorable')
        extra_plan = deepcopy(plan)
        extra_plan['reason_policies'].append({'reason': 'reason-256', 'disposition': 'review_required'})
        with self.assertRaises(ValueError):
            decode(M6ServiceQualityPlan, extra_plan)
        extra_obs = deepcopy(obs)
        extra_obs['review_reasons'].append('reason-256')
        with self.assertRaises(ValueError):
            decode(M6ServiceQualificationObservation, extra_obs)
        extra_reviews = sorted([*reviews, review_payload(obs, names[-1], reviewer='reviewer-02')],
                               key=lambda item: (item['reason'], item['reviewer_identity']))
        with self.assertRaises(ValueError):
            decode(M6ServiceQualificationEvidence, evidence_payload(obs, extra_reviews))
        for field in ('reason_policies', 'review_reasons'):
            payload = deepcopy(plan if field == 'reason_policies' else obs)
            payload[field] = [payload[field][1], payload[field][0]]
            cls = M6ServiceQualityPlan if field == 'reason_policies' else M6ServiceQualificationObservation
            with self.subTest(field=field), self.assertRaises(ValueError):
                decode(cls, payload)

    def test_hard_failures_survive_accepted_noncritical_and_completed_review(self):
        obs, plan = observation_payload(), plan_payload()
        obs['provider_page_count'] = 1
        obs['checks'][0]['outcome'] = 'fail'
        obs['review_reasons'] = ['accepted', 'pending']
        plan['reason_policies'] = [{'reason': 'accepted', 'disposition': 'accepted_noncritical'},
                                   {'reason': 'pending', 'disposition': 'review_required'}]
        result = qualify(evidence_payload(obs, [review_payload(obs, 'accepted')]), plan)
        self.assertEqual(result.verdict, 'not_scorable')
        self.assertEqual(result.reasons, ('check_fail:provider_artifact_closure',
                                          'page_count_mismatch', 'review_pending:pending'))
        self.assertIsNone(result.scorable_page_count)

    def test_projection_cannot_expose_pages_for_pending_or_failed_document(self):
        good = projection(evidence_payload(), plan_payload())
        for updates in ({'verdict': 'not_scorable'}, {'verdict': 'review_pending'},
                        {'scorable_page_count': None}, {'scorable_page_count': True},
                        {'scorable_page_count': 0}, {'reasons': ['unverified']},
                        {'verdict': 'not_scorable', 'scorable_page_count': None, 'reasons': []},
                        {'verdict': 'not_scorable', 'scorable_page_count': None, 'reasons': ['z', 'a']}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                decode(M6ServiceDocumentQualification, {**good, **updates})

    def test_new_and_old_families_and_subclasses_are_rejected_in_both_directions(self):
        new_plan = decode(M6ServiceQualityPlan, plan_payload())
        new_evidence = decode(M6ServiceQualificationEvidence, evidence_payload())
        old_plan = decode(M6QualityPlan, legacy_plan_payload())
        old_evidence = decode(M6QualificationEvidence, legacy_evidence_payload())
        for function in (qualify_service_document, qualify_document):
            for evidence, plan in ((new_evidence, old_plan), (old_evidence, new_plan)):
                with self.subTest(function=function.__name__), self.assertRaises(ValueError):
                    function(evidence, plan)
        with self.assertRaises(ValueError):
            qualify_service_document(old_evidence, old_plan)
        with self.assertRaises(ValueError):
            qualify_document(new_evidence, new_plan)
        class ForeignServicePlan(M6ServiceQualityPlan):
            pass
        class ForeignServiceEvidence(M6ServiceQualificationEvidence):
            pass
        for evidence, plan in ((new_evidence, decode(ForeignServicePlan, plan_payload())),
                               (decode(ForeignServiceEvidence, evidence_payload()), new_plan)):
            with self.subTest(subclass=type(plan).__name__), self.assertRaises(ValueError):
                qualify_service_document(evidence, plan)
        with self.assertRaises(ValueError):
            decode(M6QualityPlan, plan_payload())
        with self.assertRaises(ValueError):
            decode(M6ServiceQualificationEvidence, legacy_evidence_payload())

    def test_unicode_canonical_boundary_is_measured_in_bytes_and_inputs_remain_frozen(self):
        obs, plan = observation_payload(), plan_payload()
        obs['review_reasons'] = ['既有问题']
        plan['reason_policies'] = [{'reason': '既有问题', 'disposition': 'accepted_noncritical'}]
        evidence = evidence_payload(obs)
        raw = canonical(evidence)
        self.assertGreater(len(raw), len(raw.decode('utf-8')))
        value = M6ServiceQualificationEvidence.from_canonical_bytes(raw, maximum_bytes=len(raw))
        for maximum in (len(raw) - 1, 0, True, 1.0):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                M6ServiceQualificationEvidence.from_canonical_bytes(raw, maximum_bytes=maximum)
        planned = decode(M6ServiceQualityPlan, plan)
        before = value.canonical_bytes(), planned.canonical_bytes()
        self.assertEqual(qualify_service_document(value, planned).verdict, 'scorable')
        self.assertEqual((value.canonical_bytes(), planned.canonical_bytes()), before)
        with self.assertRaises(ValueError):
            value.observation.source_page_count = 999

    def test_pure_baseline_reference_changes_digest_but_is_not_external_attestation(self):
        first = qualify()
        plan = plan_payload()
        plan['parser_baseline_evidence_sha256'] = h('9')
        second = qualify(plan=plan)
        self.assertNotEqual(first.plan_sha256, second.plan_sha256)
        self.assertEqual(first.evidence_sha256, second.evidence_sha256)
        self.assertEqual(second.verdict, 'scorable')
        # This only proves the pure data rule. Actual baseline applicability and
        # every check's file/process provenance belong to the later adapter.


if __name__ == '__main__':
    unittest.main()
