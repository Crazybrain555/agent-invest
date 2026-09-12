"""Independent literal configuration data shared by pure config/input tests.

All pins are synthetic declarations. No package code, runtime paths or native
libraries are inspected or imported; these values cannot establish IO authority.
"""

import hashlib
import json


DIGEST = 'sha256:' + 'a' * 64
WORKER = 'disclosure_anchor.adapters.runtime.mineru_diagnostic_quality_worker'
SERVICE_CHECKS = [
    'artifact_closure', 'block_conservation', 'finding_binding', 'heading_occurrence_closure',
    'independent_rebuild_match', 'logical_table_conservation', 'page_closure',
    'reading_order_contiguity', 'repair_binding', 'retrieval_target_binding',
    'source_identity', 'table_segment_conservation',
]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def file_payload(path='disclosure_anchor/__init__.py', size=0):
    return {'relative_path': path, 'byte_count': size, 'sha256': DIGEST}


def dependency_payload(name='pypdfium2'):
    return {
        'distribution_name': name, 'version': '5.13.0',
        'install_root': '/declared/environment/lib/python3.13/site-packages',
        'import_names': ['pypdfium2', 'pypdfium2_cfg', 'pypdfium2_raw'],
        'files': [file_payload('pypdfium2/__init__.py', 251), file_payload('pypdfium2_raw/libpdfium.dylib', 7191008)],
    }


def program_payload():
    return {
        'contract_version': 'mineru-owned-quality.program.v1', 'worker_module': WORKER,
        'interpreter_path': '/declared/environment/bin/python',
        'resolved_interpreter_path': '/declared/python/bin/python3.13',
        'interpreter_byte_count': 6128784, 'interpreter_sha256': DIGEST,
        'python_version': '3.13.13', 'python_cache_tag': 'cpython-313', 'sys_platform': 'darwin',
        'source_root': '/declared/service/src', 'code_files': [file_payload()],
        'python_runtime_root': '/declared/python',
        'python_runtime_files': [file_payload('lib/python3.13/os.py', 123)],
        'dependency_pins': [dependency_payload()],
    }


def plan_payload():
    return {
        'contract_version': 'm6.quality-plan.v1', 'mode': 'service_diagnostic',
        'required_checks': list(SERVICE_CHECKS),
        'reason_policies': [
            {'reason': 'a', 'disposition': 'accepted_noncritical'},
            {'reason': 'b', 'disposition': 'review_required'},
            {'reason': '复核', 'disposition': 'score_hard_fail'},
        ],
    }


def budget_payload():
    return {'semantic_record_bytes': 11, 'build_record_bytes': 13, 'comparison_evidence_bytes': 17,
            'child_control_bytes': 19, 'child_stderr_bytes': 23, 'retained_total_bytes': 29}


def config_payload():
    return {'contract_version': 'mineru-owned-quality.config.v1', 'plan': plan_payload(),
            'budget': budget_payload(), 'program': program_payload(), 'retained_name': 'attempt.quality'}
