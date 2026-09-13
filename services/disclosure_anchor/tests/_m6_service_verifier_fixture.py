"""Independent literal service-verifier inputs; no deployment or GPU authority.

Receipt hashes below bind synthetic records to a separately declared context.
They do not observe a running provider. E1's borrowed transport separately owns
its simulated task state; source PDFs are real blank-page local documents.
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import base64
import hashlib
import io
import json
from pathlib import Path
import zipfile

from disclosure_anchor.adapters.runtime.mineru_canary import canary_request_sha256
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournal
from disclosure_anchor.adapters.runtime.mineru_diagnostic_phases import DiagnosticPhases
from disclosure_anchor.adapters.runtime.mineru_diagnostic_resources import DiagnosticResources
from tests._mineru_diagnostic_lifecycle_fixture import (
    CLOCK_SHA, LifecycleFixture, digest, journal_records,
)


CHECKS = ('provider_artifact_closure', 'provider_content_integrity',
          'provider_page_closure', 'source_identity')
RUNTIME = 'sha256:' + 'b' * 64
NOW = datetime(2026, 9, 13, 6, 1, tzinfo=timezone.utc)
OBSERVABILITY = 'http://observability.invalid/v1'
MODEL = 'independent-model'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def h(letter):
    return 'sha256:' + letter * 64


def target_payload():
    return {'name': 'MinerU', 'package_version': '3.4.4', 'backend': 'hybrid-http-client',
            'method': 'auto', 'language': 'ch', 'formula': True, 'table': True,
            'effort': 'medium', 'image_analysis': False, 'full_pdf': True,
            'start_page': None, 'end_page': None, 'runtime_bundle_identity_sha256': RUNTIME,
            'inline_equation_left': '$', 'inline_equation_right': '$',
            'target_contract_version': 'parser-target.v1'}


def baseline_context():
    """Declared test preflight input, not obtained from the receipt under test."""
    topology = {'api_endpoint_sha256': digest(b'http://api.invalid'),
                'observability_endpoint_sha256': digest(OBSERVABILITY.encode()),
                'inference_upstream_sha256': digest(b'http://vlm.invalid/v1')}
    manifest = {'client': {'writer_code_sha256': h('3')},
                'orchestrator': {'container_image_digest': h('4')},
                'topology': {**topology, 'windows_collector_sha256': h('5'),
                             'windows_node_identity_sha256': h('6'),
                             'windows_compose_sha256': h('7')}}
    identity = {'local_client_identity_sha256': h('1'),
                'local_content_package_versions': {'mineru_version': '3.4.4'},
                'local_processing_window_size': 16, 'local_writer_code_sha256': h('3'),
                'runtime_manifest_identity_sha256': RUNTIME,
                'orchestrator_runtime_identity_sha256': h('8'),
                'provider_runtime_identity_sha256': h('9'), 'served_model_id': MODEL,
                'orchestrator_task_slots': 1}
    return {'expected_identity': identity, 'expected_topology': topology,
            'expected_runtime_manifest': manifest, 'runtime_identity': RUNTIME,
            'task_slots': 1, 'task_retention_seconds': 600, 'cleanup_interval_seconds': 30,
            'observability_url': OBSERVABILITY, 'max_age_seconds': 120, 'current': NOW}


def wrapper(receipt):
    raw = canonical(receipt)
    return {'receipt': deepcopy(receipt), 'receipt_sha256': digest(raw),
            'source_bytes_sha256': digest(raw + b'\n')}


def baseline_payload():
    context = baseline_context()
    health = {'status': 'healthy', 'version': '3.4.4', 'protocol_version': 2,
              'queued_tasks': 0, 'processing_tasks': 0, 'completed_tasks': 0,
              'failed_tasks': 0, 'max_concurrent_requests': 1,
              'max_pending_tasks_requested': 1, 'max_pending_tasks_effective': 1,
              'processing_window_size': 16, 'task_retention_seconds': 600,
              'task_cleanup_interval_seconds': 30}
    epoch = {'schema': 'mineru-service-epoch.v1',
             'runtime_manifest_identity_sha256': RUNTIME, 'collector_sha256': h('5'),
             'windows_node_identity_sha256': h('6'), 'windows_compose_sha256': h('7'),
             'writer_code_sha256': h('3'), 'api_image_digest': h('4'),
             'container_epoch_sha256': h('8'), 'api_container_id': '9' * 64}

    def epoch_receipt(second):
        return wrapper({'schema': 'mineru-service-epoch-freeze.v2', 'status': 'pass',
                        'created_at_utc': f'2026-09-13T06:00:{second:02d}+00:00',
                        'database_access': 'none', 'queue_access': 'none',
                        'service_epoch': epoch, 'service_epoch_sha256': digest(canonical(epoch)),
                        'safety': {'restart_count_total': 0, 'oom_killed_count': 0,
                                   'unsafe_container_count': 0, 'cgroup_oom_total': 0,
                                   'cgroup_oom_kill_total': 0}})

    documents = []
    for index, pages in enumerate((2, 3), 1):
        source = h(str(index))
        attempt, fence, task = f'heldout-{index}', 'test-fence', f'heldout-task-{index}'
        artifact_hex, artifact_bytes = str(index + 3) * 64, 1024 + index
        key_input = f'3e7\0{source}\0{attempt}\0{fence}'.encode()
        owner_input = f'{task}\0{artifact_hex}\0{artifact_bytes}'.encode()
        disposal = {
            'schema': 'mineru-diagnostic-disposal.v1',
            'authority': 'validated-diagnostic-no-publication.v1',
            'source_pdf_sha256': source, 'runtime_bundle_identity_sha256': RUNTIME,
            'attempt_identity': attempt, 'fence_identity': fence,
            'submission_epoch_unix': 999, 'idempotency_key': '3e7.' + hashlib.sha256(key_input).hexdigest(),
            'task_id': task, 'terminal_artifact_sha256': artifact_hex,
            'terminal_artifact_bytes': artifact_bytes,
            'terminal_artifact_owner': hashlib.sha256(owner_input).hexdigest(),
            'provider_bundle_sha256': h(str(index + 4)), 'source_page_count': pages,
            'provider_page_count': pages, 'local_resources_removed': True,
            'ack_response': {'schema': 'mineru-task-protocol.v2', 'task_id': task, 'status': 'consumed'},
            'task_absence': {'detail': 'Task not found'},
        }
        receipt = {
            'schema': 'mineru_smoke_receipt.v6', 'status': 'pass',
            'started_at_utc': f'2026-09-13T06:00:{index * 10:02d}+00:00',
            'finished_at_utc': f'2026-09-13T06:00:{index * 10 + 5:02d}+00:00',
            'elapsed_seconds': 5.0, 'database_access': 'none', 'queue_access': 'none',
            'input': {'profile': 'diagnostic_custom', 'logical_name': f'literal-{index}.pdf',
                      'sha256': source, 'bytes': 1024 + index, 'page_count': pages},
            'identity': context['expected_identity'], 'topology': context['expected_topology'],
            'runtime_manifest': context['expected_runtime_manifest'],
            'canary': {'schema': 'mineru_multimodal_canary.v2',
                       'passed_at_utc': '2026-09-13T06:00:00+00:00',
                       'observability_endpoint_sha256': hashlib.sha256(OBSERVABILITY.encode()).hexdigest(),
                       'runtime_bundle_identity_sha256': RUNTIME,
                       'model_id_sha256': hashlib.sha256(MODEL.encode()).hexdigest(),
                       # Existing fixed request identity helper, not a service-verifier result oracle.
                       'request_sha256': canary_request_sha256(MODEL), 'attempts': 3,
                       'response_sha256': ['a' * 64, 'b' * 64, 'c' * 64]},
            'provider': {'target_identity': target_payload(), 'provider_bundle_sha256': h(str(index + 4)),
                         'page_count': pages, 'block_count': 0, 'artifact_count': 4},
            'orchestrator': {'task_registry_semantics': 'retained-terminal-gauges.v1',
                             'before': health, 'after': health, 'terminal_active_tasks': 0,
                             'stop_semantics': 'drain-not-cancel.v1'},
            'cleanup': {'external_api_temp_dirs_created': 0, 'external_mineru_processes_after': 0,
                        'temporary_tree_removed': True, 'retained_parse_artifacts': 0,
                        'remote_active_tasks_after': 0},
            'diagnostic_disposal': disposal,
        }
        documents.append(wrapper(receipt))
    return {'schema': 'mineru_heldout_validation_receipt.v2', 'status': 'pass',
            'created_at_utc': '2026-09-13T06:00:40+00:00',
            'policy': 'operator-held-out-complete-pdf.v1', 'database_access': 'none',
            'queue_access': 'none', 'document_count': 2, 'documents': documents,
            'epoch_before': epoch_receipt(0), 'epoch_after': epoch_receipt(30)}


class VerifierFiles:
    def __init__(self, root, *, payload=None, raw=None, changes=None):
        from disclosure_anchor.adapters.runtime.m6_service_quality_verifier import (
            service_quality_verifier_identity_sha256,
        )
        self.root = Path(root)
        self.root.mkdir()
        self.baseline = self.root / 'heldout.json'
        self.plan_path = self.root / 'plan.json'
        self.context = baseline_context()
        self.payload = deepcopy(baseline_payload() if payload is None else payload)
        self.raw = canonical(self.payload) + b'\n' if raw is None else raw
        self.plan = {'contract_version': 'm6.service-quality-plan.v1', 'mode': 'service_diagnostic',
                     'parser_target_sha256': digest(canonical(target_payload())),
                     'parser_baseline_evidence_sha256': digest(self.raw),
                     'quality_verifier_sha256': service_quality_verifier_identity_sha256(),
                     'required_checks': list(CHECKS), 'reason_policies': []}
        self.plan.update(changes or {})
        self.baseline.write_bytes(self.raw)
        self.plan_path.write_bytes(canonical(self.plan))
        self.baseline.chmod(0o600)
        self.plan_path.chmod(0o600)

    def load(self, **kwargs):
        from disclosure_anchor.adapters.runtime.m6_service_quality_verifier import load_service_quality_verifier
        return load_service_quality_verifier(self.plan_path, self.baseline,
                                            baseline_contract=kwargs.get('context', self.context))


def alter_archive(fixture, mutate):
    with zipfile.ZipFile(io.BytesIO(fixture.archive)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    mutate(files)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, raw in sorted(files.items()):
            archive.writestr(name, raw)
    fixture.archive = output.getvalue()
    fixture.archive_sha = hashlib.sha256(fixture.archive).hexdigest()
    fixture.owner = hashlib.sha256(
        f'{fixture.task_id}\0{fixture.archive_sha}\0{len(fixture.archive)}'.encode()).hexdigest()


def resources_path(fixture):
    return fixture.journal / 'resources'


@contextmanager
def open_attempt(fixture):
    records = journal_records(fixture.journal)
    binding = next(record['value'] for record in records if record['step'] == 'binding')
    with DiagnosticJournal(fixture.journal, create=False, attempt_id='independent-attempt',
                           configuration_sha256=digest(canonical(binding)), clock_identity_sha256=CLOCK_SHA,
                           deadline_ns=fixture.deadline_ns, continuous_ns=lambda: fixture.now_ns) as journal:
        phases = DiagnosticPhases(journal, binding)
        identity = phases.latest['resources_created'].value['identity']
        with DiagnosticResources(journal, identity=identity) as resources:
            yield resources, phases


def lifecycle_fixture(root):
    root.mkdir()
    fixture = LifecycleFixture(root)
    # The reader must close the full output inventory, including non-parser paths.
    def complete_media(files):
        # Actual tiny GIF bytes close the media contract; no claim these are PDF crops.
        pixel = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
        for name in tuple(files):
            if name.endswith('.jpg'):
                files[name] = pixel
        files.update({'outside/empty/': b'', 'outside/note.bin': b'outside'})

    alter_archive(fixture, complete_media)
    return fixture


def leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in leaves(child)]
    return [error]
