"""Independent configured CPU-policy compatibility; no thread getter/live claim."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import mineru_deployment_gate as gate, mineru_identity as identity
from scripts import attest_mineru_remote_runtime as attester, freeze_mineru_campaign_epoch as freezer
from tests._mineru_cpu_thread_fixture import (
    POLICY, V10, build, canonical, literal_variant, observation, sha, upgrade_deployment_fixture, verify,
)
from tests.unit.test_mineru_identity import _manifest, _staged_manifest
from tests.unit import test_mineru_deployment_gate as gate_fixture
from tests.unit.test_capacity_host_observer import COLLECTOR, NODE, _payload as host_payload


class CpuThreadManifestTest(unittest.TestCase):
    def test_default_and_explicit_one_preserve_pinned_v9_complete_bytes(self):
        # S6 adds one independently observed module to compatibility evidence.
        # Only that evidence hash and outer identity changed; CPU policy and
        # every other v9 field retain the pre-S6 shape. Not a v10 oracle.
        expected = '2d346fe5d3d90c10ca454434deba26179c83464265c05af83ec57baa292ea698'
        default = build()
        explicit = build(expected_api_cpu_threads=1)
        self.assertEqual(canonical(default), canonical(explicit))
        self.assertEqual(hashlib.sha256(canonical(default)).hexdigest(), expected)
        self.assertEqual(default['manifest']['contract_version'], 'mineru-runtime-bundle.v9')
        self.assertNotIn('cpu_thread_policy', default['manifest']['orchestrator'])

    def test_two_requires_observation_and_hashes_only_configured_cpu_policy(self):
        seen = observation(2)
        result = build(seen, expected_api_cpu_threads=2)
        manifest = result['manifest']
        self.assertEqual(manifest['contract_version'], V10)
        self.assertEqual(manifest['orchestrator']['cpu_thread_policy'], POLICY)
        self.assertEqual(manifest['orchestrator']['content_environment_sha256'], sha(seen['api']['environment']))
        self.assertEqual(result['identity_sha256'], sha(manifest))
        self.assertNotEqual(result['identity_sha256'], build()['identity_sha256'])
        self.assertEqual(verify(manifest).contract_version, V10)
        actual = manifest['orchestrator']
        for name, expected in {'max_concurrent_requests': 1, 'max_pending_tasks_effective': 1,
                               'max_pending_tasks_requested': 1, 'hybrid_batch_ratio': 1,
                               'processing_window_size': 16, 'inference_max_concurrency': 7}.items():
            self.assertEqual(actual[name], expected)
        self.assertNotIn('actual_framework_threads', json.dumps(result))
        self.assertEqual(seen, observation(2), 'builder cannot rewrite original collected values')

    def test_expected_selection_is_exact_one_or_two_and_cannot_be_a_verdict(self):
        for invalid in (True, False, 0, 3, -1, 1.0, 2.0, '2', None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                build(observation(2), expected_api_cpu_threads=invalid)

    def test_missing_mixed_or_differently_rendered_environment_cannot_be_selected_as_two(self):
        for name, value in (('OMP_NUM_THREADS', '1'), ('MKL_NUM_THREADS', '1'),
                            ('OMP_NUM_THREADS', '02'), ('MKL_NUM_THREADS', 2),
                            ('OPENBLAS_NUM_THREADS', '2'), ('MINERU_PDF_RENDER_THREADS', '4'),
                            ('OMP_NUM_THREADS', None)):
            with self.subTest(name=name, value=value):
                seen = observation(2)
                if value is None:
                    del seen['api']['environment'][name]
                else:
                    seen['api']['environment'][name] = value
                with self.assertRaises(ValueError):
                    build(seen, expected_api_cpu_threads=2)
        with self.assertRaises(ValueError):
            build(observation(2))
        with self.assertRaises(ValueError):
            build(observation(1), expected_api_cpu_threads=2)

    def test_old_v8_v9_remain_closed_and_are_not_upgraded_by_a_valid_new_policy(self):
        for original in (_manifest(), _staged_manifest()):
            with self.subTest(version=original['contract_version']):
                self.assertEqual(verify(original).manifest, original)
                changed = deepcopy(original)
                changed['orchestrator']['cpu_thread_policy'] = deepcopy(POLICY)
                with self.assertRaises(ValueError):
                    verify(changed)  # Recomputes both full wrapper hashes.

    def test_v10_policy_is_closed_versioned_and_exact_integer_configuration(self):
        for field in POLICY:
            variants = (None, 'wrong') if field == 'contract_version' else (True, 2.0, '2', 0, POLICY[field] + 1)
            for bad in variants:
                with self.subTest(field=field, value=bad):
                    manifest = literal_variant(_staged_manifest())
                    manifest['orchestrator']['cpu_thread_policy'][field] = bad
                    with self.assertRaises(ValueError):
                        verify(manifest)
        for mutation in ('missing_policy', 'missing_field', 'extra_field', 'unknown_version', 'tuple_policy'):
            with self.subTest(mutation=mutation):
                manifest = literal_variant(_staged_manifest())
                if mutation == 'missing_policy':
                    del manifest['orchestrator']['cpu_thread_policy']
                elif mutation == 'missing_field':
                    del manifest['orchestrator']['cpu_thread_policy']['openblas_num_threads']
                elif mutation == 'extra_field':
                    manifest['orchestrator']['cpu_thread_policy']['actual_torch_threads'] = 2
                elif mutation == 'unknown_version':
                    manifest['contract_version'] = 'mineru-runtime-bundle.v11'
                else:
                    manifest['orchestrator']['cpu_thread_policy'] = list(POLICY.items())
                with self.assertRaises(ValueError):
                    verify(manifest)

    def test_v10_keeps_all_original_staged_capacity_obligations(self):
        for field in ('task_registry_max_records', 'task_result_reservation_bytes', 'max_unacked_result_bytes'):
            for value in (None, 0):
                with self.subTest(field=field, value=value):
                    manifest = literal_variant(_staged_manifest())
                    if value is None:
                        del manifest['orchestrator'][field]
                    else:
                        manifest['orchestrator'][field] = value
                    with self.assertRaises(ValueError):
                        verify(manifest)

    def test_policy_helper_does_not_infer_two_from_a_hash_or_unsupported_version(self):
        helper = identity.verified_cpu_thread_policy
        self.assertEqual(helper(_manifest()), 1)
        self.assertEqual(helper(_staged_manifest()), 1)
        self.assertEqual(helper(literal_variant(_staged_manifest())), 2)
        for bad in ({}, POLICY, {'contract_version': V10, 'identity_sha256': 'sha256:' + 'a' * 64}):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                helper(bad)


class CpuThreadCompositionTest(unittest.TestCase):
    def test_real_local_gate_receipts_bind_profile_count_to_verified_manifest(self):
        borrowed = gate_fixture.MinerUDeploymentGateTests()
        for version, count, allowed in ((9, 1, True), (9, 2, False), (10, 2, True), (10, 1, False), (10, 3, False)):
            with self.subTest(version=version, count=count), tempfile.TemporaryDirectory() as temp:
                now = datetime.now(UTC)
                settings, client, _ = borrowed._fixture(Path(temp), now=now)
                if version == 10:
                    settings = upgrade_deployment_fixture(settings)
                profile = replace(borrowed._staged_profile(settings), omp_thread_count=count)
                client_patch, writer_patch = borrowed._identity_patches(client)
                with client_patch, writer_patch:
                    if allowed:
                        result = gate.verify_mineru_deployment_gate(settings, parse_enabled=True,
                                                                    process_profile=profile, now=now)
                        self.assertIsNotNone(result)
                    else:
                        with self.assertRaises(gate.MinerUDeploymentGateError):
                            gate.verify_mineru_deployment_gate(settings, parse_enabled=True,
                                                               process_profile=profile, now=now)

    def test_cli_forwards_explicit_cpu_selection_and_original_collector_object(self):
        for selected in (None, 2):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                names = ('mineru', 'identity', 'known', 'compose', 'collector', 'patcher', 'docker', 'task')
                for name in names:
                    (root / name).write_bytes(('independent-' + name).encode())
                    (root / name).chmod(0o600)
                observed = observation(selected or 1)
                output = {'identity_sha256': 'sha256:' + 'a' * 64, 'manifest': {'synthetic_cli_forwarding': True}}
                args = ['--mineru-bin', str(root/'mineru'), '--ssh-host', 'unreachable.invalid', '--ssh-user', 'placeholder',
                        '--identity-file', str(root/'identity'), '--known-hosts-file', str(root/'known'),
                        '--manifest-out', str(root/'manifest.json'), '--observation-out', str(root/'observation.json'),
                        '--expected-compose', str(root/'compose'), '--collector-source', str(root/'collector'),
                        '--compat-patcher-source', str(root/'patcher'), '--compat-dockerfile-source', str(root/'docker'),
                        '--task-protocol-v2-source', str(root/'task')]
                if selected is not None:
                    args += ['--expected-api-cpu-threads', str(selected)]
                with patch.object(attester, '_known_host_key_sha256', return_value='sha256:'+'6'*64), \
                     patch.object(attester, '_read_remote_file', side_effect=[(root/'compose').read_bytes(), (root/'collector').read_bytes()]), \
                     patch.object(attester.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(observed), '')) as remote, \
                     patch.object(attester, 'build_manifest', return_value=output) as builder, redirect_stdout(io.StringIO()):
                    self.assertEqual(attester.main(args), 0)
                self.assertEqual(builder.call_args.args[0], observed)
                self.assertEqual(builder.call_args.kwargs['expected_api_cpu_threads'], selected or 1)
                self.assertEqual(json.loads((root/'observation.json').read_bytes()), observed)
                self.assertEqual(json.loads((root/'manifest.json').read_bytes()), output)
                self.assertEqual(remote.call_count, 1)
                with patch.object(attester.subprocess, 'run') as remote, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    attester.main(args + ['--expected-api-cpu-threads', '3'])
                self.assertEqual(caught.exception.code, 2)
                remote.assert_not_called()

    def test_epoch_freezer_accepts_closed_v10_and_rejects_rehashed_policy_before_sampling(self):
        for bad in (False, True):
            with self.subTest(tampered=bad), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                manifest = literal_variant(_staged_manifest())
                manifest['topology'].update(windows_collector_sha256=COLLECTOR, windows_node_identity_sha256=NODE)
                if bad:
                    manifest['orchestrator']['cpu_thread_policy']['omp_num_threads'] = 2.0
                wrapper = {'manifest': manifest, 'identity_sha256': sha(manifest)}
                (root/'manifest.json').write_bytes(canonical(wrapper))
                (root/'manifest.json').chmod(0o600)
                args = ['--runtime-manifest', str(root/'manifest.json'), '--receipt-out', str(root/'receipt.json'),
                        '--ssh-host', 'unreachable.invalid', '--ssh-user', 'placeholder',
                        '--ssh-identity', str(root/'unused-key'), '--ssh-known-hosts', str(root/'unused-known')]
                with patch.object(freezer, 'build_host_observer_ssh_command', return_value=['no-external-execution']), \
                     patch.object(freezer.MineruHostCapacitySampler, 'sample_payload', return_value=host_payload()) as sampler, \
                     redirect_stdout(io.StringIO()):
                    if bad:
                        with self.assertRaises(SystemExit):
                            freezer.main(args)
                        sampler.assert_not_called()
                        self.assertFalse((root/'receipt.json').exists())
                    else:
                        self.assertEqual(freezer.main(args), 0)
                        receipt = json.loads((root/'receipt.json').read_bytes())
                        self.assertEqual(receipt['service_epoch']['runtime_manifest_identity_sha256'], sha(manifest))
                        self.assertEqual(sampler.call_count, 1)


if __name__ == '__main__':
    unittest.main()
