"""Independent owned-file setup; protocol/observation records simulate prerequisites.

No source child, provider or semantic reader executes. These records permit
local handle-boundary tests and cannot establish real parsing or qualification.
"""
from dataclasses import asdict
import hashlib
import io
import json
import os
from pathlib import Path
import zipfile

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged import prepare_submission_identity_v2
from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import canonical_result_owner_v2
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournal
from disclosure_anchor.adapters.runtime.mineru_diagnostic_phases import DiagnosticPhases
from disclosure_anchor.adapters.runtime.mineru_diagnostic_resources import DiagnosticResources
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions


API = 'http://owned-fixture.invalid'
CLOCK = 'sha256:' + 'e' * 64
SOURCE = b'local ownership fixture bytes; not a parser observation\n'
OUTPUT = {'parser/document.txt': b'literal parser payload\n', 'outside.bin': b'outside parser subroot\n', 'zero.bin': b''}
DIRECTORIES = ('empty/', 'parser/empty/')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return 'sha256:' + hashlib.sha256(value).hexdigest()


def identity(path):
    info = path.stat(follow_symlinks=False)
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid]


def seal(path):
    raw = path.read_bytes()
    return {'identity': identity(path), 'bytes': len(raw), 'sha256': digest(raw)}


def wire(payload):
    raw = canonical(payload)
    return {'http_status': 200, 'response_hex': raw.hex(), 'response_sha256': digest(raw)}


def private_write(path, raw):
    with path.open('xb') as stream:
        stream.write(raw)
    path.chmod(0o600)


def leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [item for child in error.exceptions for item in leaves(child)]
    return [error]


class HeldInputFixture:
    def __init__(self, root, *, source_bytes=SOURCE, output_files=None, directories=DIRECTORIES, finish=True):
        self.root = Path(root)
        self.root.mkdir(mode=0o700)
        self.now = 100
        self.deadline = 9_000_000_000
        self.source_bytes = source_bytes
        self.output_files = dict(OUTPUT if output_files is None else output_files)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as out:
            for name in directories:
                out.writestr(name, b'')
            for name, data in sorted(self.output_files.items()):
                out.writestr(name, data)
        self.archive_bytes = archive.getvalue()
        self.task_id = 'held-input-task'
        options = ParserOptions(runtime_bundle_identity_sha256='sha256:' + 'b' * 64, timeout_seconds=60)
        prepared = prepare_submission_identity_v2(
            api_url=API, server_url='http://vlm.invalid/v1', options=options,
            source_pdf_sha256=digest(source_bytes), attempt_identity='held-input-attempt',
            fence_identity='held-input-fence', submission_epoch_unix=999)
        self.binding = {'contract_version': 'mineru-diagnostic-binding.v2', 'prepared': json.loads(prepared.exact_bytes),
                        'api_url': API, 'server_url': 'http://vlm.invalid/v1', 'options': asdict(options),
                        'source_pdf_sha256': digest(source_bytes), 'source_byte_count': len(source_bytes),
                        'source_page_count': 2, 'quality_verifier_sha256': None,
                        'target_identity': options.target_identity(ParserIdentity('MinerU', '3.4.4')).to_payload()}
        self.journal = DiagnosticJournal(self.root/'journal', create=True, attempt_id='held-input-attempt',
                                         configuration_sha256=digest(canonical(self.binding)), clock_identity_sha256=CLOCK,
                                         deadline_ns=self.deadline, continuous_ns=lambda: self.now)
        self.resources = None
        try:
            self.refresh()
            self.phases.append('binding', self.binding)
            self.phases.intent('resources_intent')
            self.resources = DiagnosticResources(self.journal, identity=None)
            self.refresh()
            self.phases.intent('snapshot_intent')
            with self.resources.create_payload('source.pdf', step='snapshot') as out:
                out.write(source_bytes)
            self.refresh()
            self.source_path = self.resources.path/'source.pdf'
            self.snapshot = seal(self.source_path)
            self.phases.append('snapshot_sealed', self.snapshot)
            if finish:
                self.finish_output()
        except BaseException:
            self.close()
            raise

    def refresh(self):
        self.phases = DiagnosticPhases(self.journal, self.binding)

    def finish_output(self):
        self.phases.intent('source_probe_intent')
        # Simulated accepted physical-observation record, not an executed probe.
        observed = canonical({'kind': 'valid', 'sha256': digest(self.source_bytes),
                              'byte_count': len(self.source_bytes), 'page_count': 2})
        self.phases.append('source_observed', {'snapshot_record_sha256': self.phases.latest['snapshot_sealed'].sha256,
                                              'response_hex': observed.hex(), 'response_sha256': digest(observed)})
        self.phases.intent('submit_intent')
        prepared = self.binding['prepared']
        common = {'task_protocol_schema': 'mineru-task-protocol.v2', 'task_id': self.task_id,
                  'status_url': API + '/tasks/' + self.task_id, 'result_url': API + '/tasks/' + self.task_id + '/result',
                  'idempotency_key': prepared['client_submit_key'], 'attempt_identity': prepared['attempt_identity'],
                  'fence_identity': prepared['fence_identity']}
        response = self.phases.append('submit_reply', {**wire({**common, 'status': 'pending', 'protocol_state': 'pending'}),
                                                       'http_status': 202})
        self.phases.append('accepted', {'wire_record_sha256': response.sha256})
        archive_sha = hashlib.sha256(self.archive_bytes).hexdigest()
        owner = canonical_result_owner_v2(task_id=self.task_id, artifact_sha256=archive_sha,
                                           artifact_byte_count=len(self.archive_bytes))
        self.phases.append('terminal', wire({**common, 'status': 'completed', 'protocol_state': 'completed',
                                            'result_artifact_schema': 'mineru-retained-result.v1',
                                            'result_artifact_sha256': archive_sha, 'result_artifact_bytes': len(self.archive_bytes),
                                            'result_artifact_owner': owner}))
        self.phases.intent('lease_intent')
        self.phases.append('lease_reply', {**wire({'schema': 'mineru-task-protocol.v2', 'task_id': self.task_id,
                                                  'lease_until_unix': 2000}), 'observed_unix': 1000.0})
        self.phases.intent('archive_intent')
        with self.resources.create_payload('result.zip', step='archive') as out:
            out.write(self.archive_bytes)
        self.refresh()
        archive_seal = seal(self.resources.path/'result.zip')
        self.phases.append('archive_sealed', archive_seal)
        self.phases.intent('output_intent')
        inventory = self.resources.extract_archive(archive_seal)
        self.refresh()
        self.phases.append('output_sealed', {'inventory': inventory, 'inventory_sha256': digest(canonical(inventory))})
        self.output_path = self.resources.path/'output'

    def begin_cleanup(self):
        # A synthetic validated prerequisite only exercises the durable branch.
        self.phases.append('validated', {'outcome': 'completed',
            'provider': {'target_identity': self.binding['target_identity'], 'provider_bundle_sha256': 'sha256:'+'f'*64,
                         'page_count': 2, 'block_count': 0, 'artifact_count': 0},
            'quality': {'status': 'unverified', 'reason': 'synthetic boundary fixture', 'verifier_sha256': None, 'report': {}}})
        self.phases.intent('cleanup_intent')

    def persisted(self):
        return {path.name:path.read_bytes() for path in self.journal.root.iterdir() if path.is_file()}

    def close(self):
        if self.resources is not None:
            self.resources.close()
        self.journal.close()


def descriptor_identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino
