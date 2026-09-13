"""Independent literal data contracts, not actual PDF/verifier evidence."""

from copy import deepcopy
import hashlib
import json


SERVICE_CHECKS = (
    'provider_artifact_closure', 'provider_content_integrity',
    'provider_page_closure', 'source_identity',
)
LEGACY_CHECKS = (
    'artifact_closure', 'block_conservation', 'finding_binding',
    'heading_occurrence_closure', 'independent_rebuild_match',
    'logical_table_conservation', 'page_closure', 'reading_order_contiguity',
    'repair_binding', 'retrieval_target_binding', 'source_identity',
    'table_segment_conservation',
)


def h(character):
    return 'sha256:' + character * 64


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def plan_payload():
    return {
        'contract_version': 'm6.service-quality-plan.v1',
        'mode': 'service_diagnostic',
        'parser_target_sha256': h('a'),
        'parser_baseline_evidence_sha256': h('b'),
        'quality_verifier_sha256': h('c'),
        'required_checks': list(SERVICE_CHECKS),
        'reason_policies': [],
    }


def observation_payload():
    return {
        'mode': 'service_diagnostic', 'attempt_id': 'attempt-01',
        'source_pdf_sha256': h('d'), 'source_byte_count': 1277,
        'source_page_count': 2, 'provider_page_count': 2,
        'provider_bundle_sha256': h('e'), 'parser_target_sha256': h('a'),
        'quality_verifier_sha256': h('c'),
        'source_observed_record_sha256': h('f'), 'output_sealed_record_sha256': h('0'),
        'review_reasons': [],
        'checks': [{'check_id': name, 'outcome': 'pass', 'evidence_sha256': h(str(index))}
                   for index, name in enumerate(SERVICE_CHECKS, 1)],
    }


def evidence_payload(observation=None, reviews=()):
    return {'contract_version': 'm6.service-qualification-evidence.v1',
            'observation': deepcopy(observation or observation_payload()),
            'reviews': deepcopy(list(reviews))}


def review_payload(observation, reason, decision='accept', reviewer='reviewer-01'):
    return {'observation_sha256': sha(canonical(observation)), 'reason': reason,
            'reviewer_identity': reviewer, 'decision': decision, 'evidence_sha256': h('9')}


def legacy_plan_payload(mode='service_diagnostic'):
    return {'contract_version': 'm6.quality-plan.v1', 'mode': mode,
            'required_checks': sorted((*LEGACY_CHECKS, *(
                ('public_units_hash_match',) if mode == 'e2e_publication' else ()))),
            'reason_policies': []}


def legacy_observation_payload(mode='service_diagnostic'):
    return {
        'mode': mode, 'source_pdf_sha256': h('d'), 'source_byte_count': 1277,
        'source_page_count': 2, 'provider_page_count': 2,
        'processing_run_id': 'run-old' if mode == 'e2e_publication' else None,
        'provider_bundle_sha256': h('e'), 'provider_document_sha256': h('f'),
        'public_units_sha256': h('8') if mode == 'e2e_publication' else None,
        'unit_count': 3, 'unusable_unit_count': 0, 'needs_review_unit_count': 0,
        'review_reasons': [],
        'checks': [{'check_id': name, 'outcome': 'pass', 'evidence_sha256': h('1')}
                   for name in legacy_plan_payload(mode)['required_checks']],
    }


def legacy_evidence_payload(observation=None):
    return {'contract_version': 'm6.qualification-evidence.v1',
            'observation': deepcopy(observation or legacy_observation_payload()), 'reviews': []}
