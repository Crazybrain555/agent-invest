"""Independent CPU-policy literals over unchanged existing observation fixtures."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from scripts import attest_mineru_remote_runtime as attester
from disclosure_anchor.adapters.runtime import mineru_identity
from tests.unit.test_attest_mineru_remote_runtime import (
    CLIENT, CODE_DIGEST, DOCKERFILE_DIGEST, PATCHER_DIGEST, TASK_PROTOCOL_DIGEST, _observation,
)

POLICY = {'contract_version': 'mineru.cpu-thread-policy.v1', 'omp_num_threads': 2,
          'mkl_num_threads': 2, 'openblas_num_threads': 1, 'pdf_render_threads': 3}
V10 = 'mineru-runtime-bundle.v10'


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def sha(value):
    return 'sha256:' + hashlib.sha256(canonical(value)).hexdigest()


def observation(threads=1):
    result = _observation()
    result['api']['environment'].update(OMP_NUM_THREADS=str(threads), MKL_NUM_THREADS=str(threads))
    return result


def build(value=None, **extra):
    arguments = {'mineru_bin': Path('/private/independent-no-execution/mineru'),
                 'ssh_host_key_sha256': 'sha256:' + '6' * 64,
                 'api_url': 'http://127.0.0.1:30002',
                 'observability_url': 'http://127.0.0.1:30001/v1',
                 'inference_upstream_url': 'http://mineru-openai-server:30000/v1',
                 'expected_compose_sha256': 'sha256:' + '3' * 64,
                 'expected_collector_sha256': 'sha256:' + '7' * 64,
                 'expected_compat_patcher_sha256': PATCHER_DIGEST,
                 'expected_compat_dockerfile_sha256': DOCKERFILE_DIGEST,
                 'expected_task_protocol_v2_sha256': TASK_PROTOCOL_DIGEST, **extra}
    with patch.object(attester, 'client_bundle_identity', return_value=CLIENT), \
         patch.object(attester, 'writer_code_digest', return_value=CODE_DIGEST):
        return attester.build_manifest(observation() if value is None else value, **arguments)


def literal_variant(original):
    manifest = deepcopy(original)
    manifest['contract_version'] = V10
    manifest['orchestrator']['cpu_thread_policy'] = deepcopy(POLICY)
    return manifest


def verify(manifest):
    return mineru_identity.verify_runtime_manifest_payload(
        {'manifest': manifest, 'identity_sha256': sha(manifest)}, configured_identity=sha(manifest),
        local_client_identity=CLIENT, local_processing_window_size=16, local_writer_code_digest=CODE_DIGEST)


def upgrade_deployment_fixture(settings):
    """Rebind the borrowed offline gate receipts to one literal new manifest.

    Every affected existing reference is recalculated; no validation function
    under test is bypassed. These are synthetic contract receipts, not live proof.
    """
    old_runtime = settings.disclosure_mineru_runtime_bundle_identity_sha256
    smoke = json.loads(settings.disclosure_mineru_smoke_receipt.read_bytes())
    old_orchestrator = sha(smoke['runtime_manifest']['orchestrator'])
    new_manifest = literal_variant(smoke['runtime_manifest'])
    new_runtime = sha(new_manifest)
    new_orchestrator = sha(new_manifest['orchestrator'])

    def transform(value):
        if isinstance(value, str):
            return {old_runtime: new_runtime, old_orchestrator: new_orchestrator}.get(value, value)
        if isinstance(value, list):
            return [transform(item) for item in value]
        if not isinstance(value, dict):
            return value
        changed = {key: transform(item) for key, item in value.items()}
        if changed.get('contract_version') == 'mineru-runtime-bundle.v9':
            changed = deepcopy(new_manifest)
        if 'receipt' in changed and 'receipt_sha256' in changed:
            changed['receipt_sha256'] = sha(changed['receipt'])
        if 'service_epoch' in changed and 'service_epoch_sha256' in changed:
            changed['service_epoch_sha256'] = sha(changed['service_epoch'])
        return changed

    for path in (settings.disclosure_mineru_smoke_receipt, settings.disclosure_mineru_canary_cache,
                 settings.disclosure_mineru_validation_receipt):
        path.write_bytes(canonical(transform(json.loads(path.read_bytes()))))
    return settings.model_copy(update={'disclosure_mineru_runtime_bundle_identity_sha256': new_runtime})
