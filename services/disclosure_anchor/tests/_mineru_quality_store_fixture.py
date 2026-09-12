"""Actual temporary E1 journal/resources with a v3 binding established first.

The source/HTTP observations are explicitly synthetic prerequisites, inherited
from the independent held-input fixture. No provider, physical PDF observer,
semantic child or quality qualification executes. This owns no retained store.
"""

from dataclasses import asdict
import io
import json
from pathlib import Path
import zipfile

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged import prepare_submission_identity_v2
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournal
from disclosure_anchor.adapters.runtime.mineru_diagnostic_resources import DiagnosticResources
from disclosure_anchor.application.contracts.mineru_diagnostic_quality_config import OwnedDiagnosticQualityConfig
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from tests._mineru_held_inputs_fixture import (
    API, CLOCK, DIRECTORIES, OUTPUT, SOURCE, HeldInputFixture, canonical, digest, seal,
)
from tests._mineru_quality_config_fixture import config_payload


class QualityStoreFixture(HeldInputFixture):
    """Share actual E1 setup with store tests; do not call the old v2 initializer."""

    def __init__(self, root, *, budget=None, retained_name=None, finish=True):
        self.root = Path(root)
        self.root.mkdir(mode=0o700)
        self.now = 100
        self.deadline = 9_000_000_000
        self.source_bytes = SOURCE
        self.output_files = dict(OUTPUT)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
            for name in DIRECTORIES:
                output.writestr(name, b"")
            for name, raw in sorted(self.output_files.items()):
                output.writestr(name, raw)
        self.archive_bytes = archive.getvalue()
        self.task_id = "held-input-task"
        configuration = config_payload()
        configuration["retained_name"] = "journal.quality" if retained_name is None else retained_name
        configuration["budget"] = dict(budget) if budget is not None else {
            "semantic_record_bytes": 1024 * 1024,
            "build_record_bytes": 1024 * 1024,
            "comparison_evidence_bytes": 1024 * 1024,
            "child_control_bytes": 64 * 1024,
            "child_stderr_bytes": 4096,
            "retained_total_bytes": 8 * 1024 * 1024,
        }
        self.config = OwnedDiagnosticQualityConfig.from_payload(configuration)
        options = ParserOptions(runtime_bundle_identity_sha256="sha256:" + "b" * 64, timeout_seconds=60)
        prepared = prepare_submission_identity_v2(
            api_url=API, server_url="http://vlm.invalid/v1", options=options,
            source_pdf_sha256=digest(self.source_bytes), attempt_identity="held-input-attempt",
            fence_identity="held-input-fence", submission_epoch_unix=999,
        )
        self.binding = {
            "contract_version": "mineru-diagnostic-binding.v3", "prepared": json.loads(prepared.exact_bytes),
            "api_url": API, "server_url": "http://vlm.invalid/v1", "options": asdict(options),
            "source_pdf_sha256": digest(self.source_bytes), "source_byte_count": len(self.source_bytes),
            "source_page_count": 2, "owned_quality": configuration,
            "target_identity": options.target_identity(ParserIdentity("MinerU", "3.4.4")).to_payload(),
        }
        self.journal = DiagnosticJournal(
            self.root / "journal", create=True, attempt_id="held-input-attempt",
            configuration_sha256=digest(canonical(self.binding)), clock_identity_sha256=CLOCK,
            deadline_ns=self.deadline, continuous_ns=lambda: self.now,
        )
        self.resources = None
        self.output_inventory = None
        try:
            self.refresh()
            self.phases.append("binding", self.binding)
            self.phases.intent("resources_intent")
            self.resources = DiagnosticResources(self.journal, identity=None)
            self.refresh()
            self.phases.intent("snapshot_intent")
            with self.resources.create_payload("source.pdf", step="snapshot") as output:
                output.write(self.source_bytes)
            self.refresh()
            self.source_path = self.resources.path / "source.pdf"
            self.snapshot = seal(self.source_path)
            self.phases.append("snapshot_sealed", self.snapshot)
            if finish:
                self.finish_output()
        except BaseException:
            self.close()
            raise

    def finish_output(self):
        super().finish_output()
        self.output_inventory = self.phases.value("output_sealed")["inventory"]
