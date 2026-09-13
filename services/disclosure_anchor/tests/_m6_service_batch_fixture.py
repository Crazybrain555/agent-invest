"""Independent complete tiny PDFs and stateful HTTP simulation for batch tests.

The pre-existing E1 fixture remains unchanged. Only simulation dependencies are
injected; every returned disposal proof comes from the actual v2 lifecycle.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
from threading import get_ident
from typing import Any
from unittest.mock import patch
import zipfile

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import canonical_result_owner_v2
from disclosure_anchor.adapters.runtime import m6_service_batch as batch
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournal
from disclosure_anchor.adapters.runtime.mineru_diagnostic_lifecycle import run_diagnostic_attempt_v2
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.ports.parser import ParserOptions
from tests._mineru_diagnostic_lifecycle_fixture import API, CLOCK_SHA, RUNTIME_SHA, LifecycleFixture


JOURNAL_BYTES = 16_777_216
RESULT_BYTES = 268_435_456
OUTPUT_BYTES = 17_179_869_184
DECODED_BYTES = 4_294_967_296


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode()


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def expected_reservation(item: batch.ServiceBatchInput) -> ResourceCreditVector:
    # Literal pre-existing E1 ceilings; never call service_work_reservation.
    return ResourceCreditVector(
        documents=1, snapshot_items=1, snapshot_bytes=item.source_byte_count,
        remote_waits=1, provider_tasks=1, provider_result_bytes=RESULT_BYTES,
        materialization_items=1, compressed_bytes=RESULT_BYTES, decoded_bytes=DECODED_BYTES,
        temp_disk_bytes=item.source_byte_count + RESULT_BYTES + OUTPUT_BYTES + JOURNAL_BYTES,
        output_items=1, output_bytes=OUTPUT_BYTES, output_pages=2, ack_items=1,
    )


def journal_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class BatchFixture:
    def __init__(self, root: Path, *, count: int = 2, max_in_flight: int = 2) -> None:
        self.root = root
        self.journal = root / "batch"
        self.now_ns = 10_000_000_000
        self.deadline_ns = 70_000_000_000
        self.simulations: dict[str, LifecycleFixture] = {}
        self.calls: list[dict[str, Any]] = []
        self.proofs: dict[str, dict[str, Any]] = {}
        items = []
        for index in range(count):
            identity = f"independent-batch-{index}"
            location = root / f"input-{index}"
            location.mkdir()
            simulation = LifecycleFixture(location)
            old_sha = simulation.source_sha
            # A legal trailing PDF comment changes full-source identity without
            # changing its two physical blank pages or synthesizing a page claim.
            simulation.source_bytes += f"\n% independent original PDF {index}\n".encode()
            simulation.source.write_bytes(simulation.source_bytes)
            simulation.source_sha = digest(simulation.source_bytes)
            archive = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(simulation.archive)) as previous, zipfile.ZipFile(
                archive, "w", compression=zipfile.ZIP_STORED
            ) as target:
                for member in previous.infolist():
                    name = member.filename.replace(old_sha.replace(":", "_"),
                                                   simulation.source_sha.replace(":", "_"))
                    target.writestr(name, previous.read(member))
            simulation.archive = archive.getvalue()
            simulation.archive_sha = hashlib.sha256(simulation.archive).hexdigest()
            simulation.task_id = f"independent-batch-task-{index}"
            simulation.owner = canonical_result_owner_v2(
                task_id=simulation.task_id, artifact_sha256=simulation.archive_sha,
                artifact_byte_count=len(simulation.archive),
            )
            self.simulations[identity] = simulation
            items.append(batch.ServiceBatchInput(
                identity, f"independent-fence-{index}", 999, simulation.source,
                simulation.source_sha, len(simulation.source_bytes), 2,
            ))
        self.inputs = tuple(items)
        limit = ResourceCreditVector(temp_disk_bytes=JOURNAL_BYTES)
        for item in self.inputs:
            limit += expected_reservation(item)
        self.kwargs: dict[str, Any] = {
            "batch_id": "independent-batch", "inputs": self.inputs,
            "api_url": API, "server_url": "http://vlm.invalid/v1",
            "options": ParserOptions(runtime_bundle_identity_sha256=RUNTIME_SHA, timeout_seconds=60),
            "journal_root": self.journal, "clock_identity_sha256": CLOCK_SHA,
            "deadline_ns": self.deadline_ns, "continuous_ns": lambda: self.now_ns,
            "max_in_flight": max_in_flight, "credits_limit": limit,
            "stop_requested": lambda: False, "before_submit": lambda: None,
        }

    def attempt_root(self, identity: str) -> Path:
        return self.root / ("batch-attempt-" + hashlib.sha256(identity.encode()).hexdigest())

    @contextmanager
    def transport(self) -> Iterator[None]:
        def actual_lifecycle(**kwargs: Any) -> dict[str, Any]:
            self.calls.append({**kwargs, "thread": get_ident()})
            simulation = self.simulations[kwargs["attempt_identity"]]
            proof = run_diagnostic_attempt_v2(
                **kwargs, transport=httpx.MockTransport(simulation.handle),
                unix_time=lambda: 1_000.0, pause=lambda duration: None,
            )
            self.proofs[kwargs["attempt_identity"]] = proof
            return proof

        with patch.object(batch, "run_diagnostic_attempt_v2", side_effect=actual_lifecycle):
            yield

    def run(self, **overrides: Any) -> batch.ServiceBatchResult:
        with self.transport():
            return batch.run_service_batch(**{**self.kwargs, **overrides})

    def append_batch(self, step: str, value: dict[str, Any]) -> None:
        header = json.loads((self.journal / "00-journal.json").read_bytes())
        with DiagnosticJournal(
            self.journal, create=False, attempt_id=header["attempt_id"],
            configuration_sha256=header["configuration_sha256"],
            clock_identity_sha256=CLOCK_SHA, deadline_ns=self.deadline_ns,
            continuous_ns=lambda: self.now_ns,
        ) as owner:
            owner.append(step, value)

    def dispatch_intent(self, identity: str) -> None:
        header = json.loads((self.journal / "00-journal.json").read_bytes())
        self.append_batch("dispatch_intent", {
            "attempt_id": identity, "binding_sha256": header["configuration_sha256"],
        })

    @property
    def events(self) -> dict[str, tuple[tuple[str, str], ...]]:
        return {identity: tuple(simulation.events) for identity, simulation in self.simulations.items()}

    def original_binding_inputs(self) -> list[dict[str, Any]]:
        return [{**asdict(item), "input_pdf": str(item.input_pdf)} for item in self.inputs]
