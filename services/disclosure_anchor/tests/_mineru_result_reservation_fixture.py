"""Independent real filesystem/registry lab; bytes are not parsed PDFs or valid ZIPs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol


class ReservationLab:
    def __init__(self, root: Path, *, limit: int = 73):
        self.output = root / 'owned-output'
        self.output.mkdir()
        self.path = self.output / '.agent-task-protocol-v2' / 'registry.json'
        self.limit = limit
        self.registry = self.cold()

    def cold(self):
        return protocol.DurableTaskRegistry(self.path, max_unacked_result_bytes=self.limit,
                                            output_root=self.output)

    def pending(self, name: str):
        self.registry.reconcile_or_create(idempotency_key=name, task_id=name,
            attempt_identity='attempt-' + name, fence_identity='fence-' + name)
        task = self.output / name
        uploads = task / 'uploads'
        uploads.mkdir(parents=True)
        source = uploads / 'source.pdf'
        source.write_bytes(b'independent unparsed ownership bytes\n')
        payload = {'task_id': name, 'output_dir': str(task), 'uploads': [str(source)]}
        self.registry.bind_task_payload(name, payload)
        return task

    def old_reserved(self, name: str, budget: int):
        """Use accepted pre-R6 public transitions to expose original release defects."""
        task = self.pending(name)
        self.registry.transition(name, 'processing')
        self.registry.transition(name, 'finalizing')
        self.registry.reserve_finalizer(name, byte_budget=budget)
        return task

    def result(self, name: str, size: int):
        raw = b'R' * size
        path = self.output / name / '.retained-result.zip'
        path.write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        owner = hashlib.sha256(f'{name}\0{sha}\0{size}'.encode()).hexdigest()
        return {'result_path': path, 'result_sha256': sha, 'result_bytes': size, 'result_owner': owner}

    def completed(self, name: str, *, budget: int, size: int):
        self.old_reserved(name, budget)
        result = self.result(name, size)
        self.registry.complete(name, **result)
        return result

    def disk(self):
        return json.loads(self.path.read_bytes())

    def rows(self):
        return {row['idempotency_key']: row for row in self.disk()['records']}

    def legacy_wire(self, schema: str):
        """Explicit historical wire fixture, retaining all actual ownership identities."""
        data = self.disk()
        data['schema'] = schema
        if schema == 'mineru-task-registry.v2':
            for row in data['records']:
                row.pop('ingress_owner')
        self.path.write_bytes(json.dumps(data, sort_keys=True, separators=(',', ':')).encode())
