"""Independent file-to-fixed-config boundary; synthetic heldout is not live evidence."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import m6_service_quality_verifier as adapter
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import MinerUDeploymentGateError
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournalError
from tests._m6_service_verifier_fixture import (
    NOW, VerifierFiles, baseline_payload, canonical, digest, h, wrapper,
)


class ServiceVerifierConfigTest(unittest.TestCase):
    def test_actual_private_bytes_produce_detached_closed_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = VerifierFiles(Path(directory) / 'config')
            verifier = fixture.load()
            self.assertEqual(verifier.plan.canonical_bytes(), canonical(fixture.plan))
            self.assertEqual(verifier.identity_sha256, fixture.plan['quality_verifier_sha256'])
            self.assertEqual(verifier.plan.parser_baseline_evidence_sha256, digest(fixture.raw))
            self.assertNotEqual(digest(fixture.raw), digest(canonical(fixture.payload)))
            expected = {'contract_version': 'm6.service-verifier-binding.v1', 'plan': fixture.plan,
                        'baseline_contract': {**deepcopy(fixture.context), 'current': NOW.isoformat()}}
            self.assertEqual(verifier.binding_payload(), expected)
            fixture.context['expected_identity']['served_model_id'] = 'mutated-caller'
            returned = verifier.binding_payload()
            returned['plan']['reason_policies'].append({'reason': 'x', 'disposition': 'score_hard_fail'})
            self.assertEqual(verifier.binding_payload(), expected)
            with self.assertRaises((AttributeError, TypeError)):
                verifier._binding_raw = b'{}'
            with self.assertRaises(TypeError):
                adapter.ServiceQualityVerifier()

    def test_baseline_raw_hash_and_implementation_identity_cannot_be_self_declared(self):
        for failure in ('baseline_bytes', 'baseline_hash', 'implementation', 'target'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                fixture = VerifierFiles(Path(directory) / 'config')
                if failure == 'baseline_bytes':
                    fixture.baseline.write_bytes(canonical(fixture.payload))
                else:
                    field = {'baseline_hash': 'parser_baseline_evidence_sha256',
                             'implementation': 'quality_verifier_sha256',
                             'target': 'parser_target_sha256'}[failure]
                    fixture.plan[field] = h('0')
                    fixture.plan_path.write_bytes(canonical(fixture.plan))
                with self.assertRaises((ValueError, DiagnosticJournalError)):
                    fixture.load()

    def test_nested_heldout_closure_is_checked_even_with_correct_wrapper_and_file_hash(self):
        for failure in ('target', 'page', 'cleanup', 'ack', 'epoch', 'repeated_source', 'stale'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                baseline = baseline_payload()
                receipt = baseline['documents'][0]['receipt']
                if failure == 'target':
                    receipt['provider']['target_identity']['effort'] = 'high'
                elif failure == 'page':
                    receipt['provider']['page_count'] = 1
                elif failure == 'cleanup':
                    receipt['cleanup']['temporary_tree_removed'] = False
                elif failure == 'ack':
                    receipt['diagnostic_disposal']['ack_response']['task_id'] = 'other-task'
                elif failure == 'epoch':
                    changed = baseline['epoch_after']['receipt']
                    changed['service_epoch']['api_container_id'] = 'a' * 64
                    changed['service_epoch_sha256'] = digest(canonical(changed['service_epoch']))
                    baseline['epoch_after'] = wrapper(changed)
                elif failure == 'repeated_source':
                    baseline['documents'][1] = deepcopy(baseline['documents'][0])
                else:
                    baseline['created_at_utc'] = '2020-01-01T00:00:00+00:00'
                baseline['documents'][0] = wrapper(receipt)
                fixture = VerifierFiles(Path(directory) / 'config', payload=baseline)
                with self.assertRaises((ValueError, DiagnosticJournalError, MinerUDeploymentGateError)):
                    fixture.load()

    def test_baseline_duplicate_json_names_do_not_hide_a_conflicting_original_value(self):
        raw = canonical(baseline_payload())
        # Last-value-wins json.loads would erase the original fail value here.
        raw = b'{"status":"fail",' + raw[1:]
        with tempfile.TemporaryDirectory() as directory:
            fixture = VerifierFiles(Path(directory) / 'config', raw=raw)
            with self.assertRaises((ValueError, DiagnosticJournalError, MinerUDeploymentGateError)):
                fixture.load()

    def test_plan_requires_complete_canonical_family_before_acceptance(self):
        for failure in ('extra', 'old_family', 'bool', 'pretty'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                fixture = VerifierFiles(Path(directory) / 'config')
                if failure == 'extra':
                    fixture.plan['qualified'] = True
                elif failure == 'old_family':
                    fixture.plan['contract_version'] = 'm6.quality-plan.v1'
                elif failure == 'bool':
                    fixture.plan['parser_baseline_evidence_sha256'] = True
                raw = json.dumps(fixture.plan, indent=2).encode() if failure == 'pretty' else canonical(fixture.plan)
                fixture.plan_path.write_bytes(raw)
                with self.assertRaises((ValueError, DiagnosticJournalError)):
                    fixture.load()

    def test_expected_context_is_closed_strict_and_independent_of_receipt(self):
        for failure in ('extra', 'missing', 'bool', 'naive', 'model', 'runtime', 'topology'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                fixture = VerifierFiles(Path(directory) / 'config')
                context = deepcopy(fixture.context)
                if failure == 'extra':
                    context['already_validated'] = True
                elif failure == 'missing':
                    del context['expected_runtime_manifest']
                elif failure == 'bool':
                    context['task_slots'] = True
                elif failure == 'naive':
                    context['current'] = datetime(2026, 9, 13, 6, 1)
                elif failure == 'model':
                    context['expected_identity']['served_model_id'] = 'not-the-observed-model'
                elif failure == 'runtime':
                    context['runtime_identity'] = h('0')
                else:
                    context['expected_topology']['api_endpoint_sha256'] = h('0')
                with self.assertRaises((ValueError, DiagnosticJournalError, MinerUDeploymentGateError)):
                    fixture.load(context=context)

    def test_bounded_private_regular_original_file_is_required(self):
        for failure in ('mode', 'symlink', 'hardlink', 'fifo', 'oversize'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                fixture = VerifierFiles(Path(directory) / 'config')
                path = fixture.baseline
                if failure == 'mode':
                    path.chmod(0o644)
                elif failure == 'hardlink':
                    os.link(path, path.with_suffix('.alias'))
                elif failure in ('symlink', 'fifo'):
                    path.rename(path.with_suffix('.saved'))
                    if failure == 'symlink':
                        path.symlink_to(path.with_suffix('.saved'))
                    else:
                        os.mkfifo(path, 0o600)
                else:
                    with path.open('r+b') as stream:
                        stream.truncate(16 * 1024 * 1024 + 1)
                with self.assertRaises((OSError, ValueError, DiagnosticJournalError)):
                    fixture.load()

    def test_original_file_replacement_during_read_is_rejected(self):
        for failure in ('replace', 'same_size'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                fixture = VerifierFiles(Path(directory) / 'config')
                original = adapter._stream
                baseline_inode = fixture.baseline.stat().st_ino

                class ReadBoundary:
                    def __init__(self, stream):
                        self.stream = stream

                    def fileno(self):
                        return self.stream.fileno()

                    def read(self, count):
                        raw = self.stream.read(count)
                        if os.fstat(self.fileno()).st_ino == baseline_inode:
                            if failure == 'replace':
                                fixture.baseline.rename(fixture.baseline.with_suffix('.saved'))
                                fixture.baseline.write_bytes(raw)
                                fixture.baseline.chmod(0o600)
                            else:
                                changed = raw.replace(b'"status":"pass"', b'"status":"fail"', 1)
                                self_test.assertEqual(len(changed), len(raw))
                                fixture.baseline.write_bytes(changed)
                        return raw

                @contextmanager
                def stream_boundary(fd, mode):
                    with original(fd, mode) as stream:
                        yield ReadBoundary(stream)

                self_test = self
                with patch.object(adapter, '_stream', stream_boundary):
                    with self.assertRaises(DiagnosticJournalError):
                        fixture.load()


if __name__ == '__main__':
    unittest.main()
