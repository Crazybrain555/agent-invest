"""Executable, explicitly resumed single-attempt diagnostic v2 owner.

This private path never publishes or fabricates a production completion witness.
Every recovery uses the original key, clock and absolute deadline. Ambiguous
creation or unsealed transfers are retained, not repaired by pathname adoption.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, BinaryIO, cast

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import MinerUMediumArtifactReader
from disclosure_anchor.adapters.parsers.mineru_medium.http_staged import prepare_submission_identity_v2
from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    decode_closed_json_v2, parse_result_lease_v2, result_lease_url_v2,
    submission_form_v2, task_ack_url_v2, task_lookup_url_v2,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournal, DiagnosticJournalError
from disclosure_anchor.adapters.runtime.mineru_diagnostic_phases import (
    DiagnosticPhases, DiagnosticUnresolved, wire_bytes, wire_evidence,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_resources import DiagnosticResources, _stream
from disclosure_anchor.adapters.runtime.mineru_diagnostic_store import _canonical, _digest
from disclosure_anchor.adapters.runtime.mineru_diagnostic_source import observe_diagnostic_source
from disclosure_anchor.adapters.runtime.mineru_diagnostic_wire import DiagnosticWireClient
from disclosure_anchor.application.contracts.provider_document import ProviderDocument
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions

_MAX_INPUT_BYTES = 512 * 1024 * 1024
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
QualityVerifier = Callable[[Path, Path, ProviderDocument], dict[str, Any]]


class _Upload:
    """A borrowed stream; multipart size/seek/read remain under the owner guard."""

    def __init__(self, stream: BinaryIO, checkpoint: Callable[[], float]) -> None:
        self.stream, self.checkpoint = stream, checkpoint

    def read(self, size: int = -1) -> bytes:
        self.checkpoint()
        result = self.stream.read(size)
        self.checkpoint()
        return result

    def seek(self, offset: int, whence: int = 0) -> int:
        self.checkpoint()
        return self.stream.seek(offset, whence)

    def tell(self) -> int:
        return self.stream.tell()


class _Attempt:
    def __init__(self, journal: DiagnosticJournal, binding: dict[str, Any], *, resume: bool,
                 wire: DiagnosticWireClient, options: ParserOptions, unix_time: Callable[[], float],
                 pause: Callable[[float], None]) -> None:
        self.journal, self.binding, self.resume = journal, binding, resume
        self.wire, self.options, self.unix_time, self.pause = wire, options, unix_time, pause
        self.phases = DiagnosticPhases(journal, binding)

    def refresh(self) -> None:
        self.phases = DiagnosticPhases(self.journal, self.binding)

    def raw(self, step: str, status: int, raw: bytes, **extra: Any) -> None:
        # Preserve actual bounded bytes before protocol interpretation. Invalid
        # responses remain in the chain and prevent a later blind continuation.
        value = {**wire_evidence(status, raw), **extra}
        wire_bytes(value)
        self.journal.append(step, value)
        self.refresh()

    def request(self, step: str, method: str, url: str) -> None:
        self.phases.reserve_wire(step)
        status, raw = self.wire.request(method, url)
        self.raw(step, status, raw, **({"observed_unix": self.unix_time()} if step == "lease_reply" else {}))

    def new_intent(self, prefix: str) -> None:
        if self.phases.has(prefix + "_intent"):
            raise DiagnosticUnresolved("interrupted diagnostic " + prefix + "; preserve original objects")
        self.phases.intent(prefix + "_intent")

    def snapshot(self, resources: DiagnosticResources, input_pdf: Path) -> None:
        if self.phases.has("snapshot_sealed"):
            resources.verify_payload("source.pdf", self.phases.value("snapshot_sealed"))
            return
        self.new_intent("snapshot")
        fd = os.open(input_pdf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with _stream(fd, "rb") as source, resources.create_payload("source.pdf", step="snapshot") as sink:
            self.refresh()
            before = os.fstat(source.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                    or before.st_size != self.binding["source_byte_count"]):
                raise DiagnosticJournalError("diagnostic original source ownership/size differs")
            digest, count = hashlib.sha256(), 0
            while chunk := source.read(1024 * 1024):
                resources.checkpoint()
                count += len(chunk)
                if count > self.binding["source_byte_count"]:
                    raise DiagnosticJournalError("diagnostic source exceeds original byte envelope")
                digest.update(chunk)
                sink.write(chunk)
            after = os.fstat(source.fileno())
            fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
            if (any(getattr(before, key) != getattr(after, key) for key in fields)
                    or count != self.binding["source_byte_count"]
                    or "sha256:" + digest.hexdigest() != self.binding["source_pdf_sha256"]):
                raise DiagnosticJournalError("diagnostic source changed or hash differs")
        self.phases.append("snapshot_sealed", resources.seal_payload(
            "source.pdf", identity=self.phases.value("snapshot_created")["identity"]))

    def submit(self, resources: DiagnosticResources) -> None:
        if self.phases.has("accepted"):
            return
        if not self.phases.has("submit_intent"):
            self.phases.intent("submit_intent")
        prepared = self.binding["prepared"]
        if self.resume:
            self.phases.reserve_exchange("lookup_intent")
            self.request("lookup_reply", "GET", task_lookup_url_v2(
                api_origin=self.binding["api_url"], idempotency_key=prepared["client_submit_key"]))
            step = "lookup_reply"
        else:
            self.phases.reserve_wire("submit_reply")
            data = submission_form_v2(self.options, server_url=self.binding["server_url"])
            data.update(agent_idempotency_key=prepared["client_submit_key"],
                        agent_attempt_identity=prepared["attempt_identity"], agent_fence_identity=prepared["fence_identity"])
            receipt = self.phases.value("snapshot_sealed")
            resources.verify_payload("source.pdf", receipt)
            with resources.open_payload("source.pdf", identity=receipt["identity"]) as source:
                with self.wire.client.stream(
                    "POST", self.binding["api_url"].rstrip("/") + "/tasks", data=data,
                    files={"files": ("sha256_" + self.binding["source_pdf_sha256"][7:] + ".pdf",
                                      cast(BinaryIO, _Upload(source, resources.checkpoint)), "application/pdf")},
                    timeout=resources.checkpoint(), headers={"Accept-Encoding": "identity"},
                ) as response:
                    status, raw = response.status_code, self.wire.read_response(response)
            self.raw("submit_reply", status, raw)
            resources.verify_payload("source.pdf", receipt)
            step = "submit_reply"
        if self.phases.value(step)["http_status"] not in {200, 202}:
            raise DiagnosticUnresolved("original-key submission outcome unresolved; never resubmit")
        self.phases.append("accepted", {"wire_record_sha256": self.phases.latest[step].sha256})

    def terminal(self) -> None:
        if self.phases.has("terminal"):
            return
        accepted = self.phases.accepted()
        while True:
            self.phases.reserve_wire("terminal")
            status, raw = self.wire.request("GET", accepted.status_url)
            value = wire_evidence(status, raw)
            try:
                observation = self.phases.observation(value, accepted.task_id)
            except ValueError:
                self.raw("terminal", status, raw)
                raise
            if status != 200 or observation.status in {"completed", "failed"}:
                self.raw("terminal", status, raw)
                return
            self.pause(min(1.0, self.journal.remaining_seconds()))

    def artifacts(self, resources: DiagnosticResources) -> None:
        terminal = self.phases.terminal()
        if not self.phases.has("archive_sealed"):
            if not self.phases.has("lease_reply"):
                self.new_intent("lease")
                self.request("lease_reply", "POST", result_lease_url_v2(
                    api_origin=self.binding["api_url"], task_id=terminal.task_id))
            lease = parse_result_lease_v2(wire_bytes(self.phases.value("lease_reply")),
                                         task_id=terminal.task_id, observed_at_unix=self.unix_time())
            self.new_intent("archive")
            self.phases.reserve_wire("archive_sealed")
            with resources.create_payload("result.zip", step="archive") as sink:
                self.refresh()
                with self.wire.client.stream("GET", terminal.result_url, timeout=resources.checkpoint(),
                                             headers={"Accept-Encoding": "identity"}) as response:
                    if (response.status_code != 200
                            or response.headers.get("x-mineru-result-sha256") != terminal.artifact_sha256
                            or response.headers.get("x-mineru-result-owner") != terminal.artifact_owner_identity
                            or response.headers.get("content-encoding", "identity") != "identity"):
                        raise DiagnosticJournalError("diagnostic result response identity differs")
                    count, digest = 0, hashlib.sha256()
                    for chunk in response.iter_raw():
                        resources.checkpoint()
                        if self.unix_time() >= lease.lease_until_unix:
                            raise DiagnosticJournalError("diagnostic result lease expired during download")
                        count += len(chunk)
                        if terminal.artifact_byte_count is None or count > terminal.artifact_byte_count:
                            raise DiagnosticJournalError("diagnostic result exceeds terminal byte reservation")
                        digest.update(chunk)
                        sink.write(chunk)
                    if count != terminal.artifact_byte_count or digest.hexdigest() != terminal.artifact_sha256:
                        raise DiagnosticJournalError("diagnostic result actual bytes/hash differ")
            self.phases.append("archive_sealed", resources.seal_payload(
                "result.zip", identity=self.phases.value("archive_created")["identity"]))
        resources.verify_payload("result.zip", self.phases.value("archive_sealed"))
        if not self.phases.has("output_sealed"):
            self.new_intent("output")
            inventory = resources.extract_archive(self.phases.value("archive_sealed"))
            self.refresh()
            self.phases.append("output_sealed", {"inventory": inventory, "inventory_sha256": _digest(_canonical(inventory))})
        resources.verify_inventory(self.phases.value("output_sealed")["inventory"], prefix="output", partial=False)

    def validate(self, resources: DiagnosticResources, reader: MinerUMediumArtifactReader,
                 quality_verifier: QualityVerifier | None) -> None:
        resources.verify_inventory(self.phases.inventory(), partial=False)
        if self.phases.has("validated"):
            return
        terminal = self.phases.terminal()
        provider: dict[str, Any] | None = None
        quality: dict[str, Any] = {"status": "not_applicable", "reason": "provider terminal failed",
                                   "verifier_sha256": None, "report": {}}
        if terminal.status == "completed":
            document = reader.read_with_location(resources.path / "output",
                                                source_pdf_sha256=self.binding["source_pdf_sha256"]).document
            if (document.parser_version != "3.4.4" or document.backend != "hybrid"
                    or document.effort != "medium" or len(document.pages) != self.binding["source_page_count"]):
                raise DiagnosticJournalError("diagnostic source page/profile closure differs")
            resources.verify_inventory(self.phases.inventory(), partial=False)
            provider = {"target_identity": self.binding["target_identity"], "provider_bundle_sha256": document.bundle_sha256,
                        "page_count": len(document.pages), "block_count": len(document.blocks), "artifact_count": len(document.artifacts)}
            quality = {"status": "unverified", "reason": "independent semantic verifier not supplied", "report": {}}
            if quality_verifier is not None:
                quality = quality_verifier(resources.path / "source.pdf", resources.path / "output", document)
                if (type(quality) is not dict or set(quality) != {"status", "reason", "report"}
                        or quality["status"] not in {"pass", "fail", "needs_review", "unverified"}):
                    raise DiagnosticJournalError("diagnostic verifier result is not a closed quality observation")
            quality = {**quality, "verifier_sha256": self.binding["quality_verifier_sha256"]}
            resources.verify_inventory(self.phases.inventory(), partial=False)
        self.phases.append("validated", {"outcome": terminal.status, "provider": provider, "quality": quality})

    def cleanup(self) -> None:
        names = (self.journal.root / "resources", self.journal.root / "resources-reclaim")
        present = any(path.exists() or path.is_symlink() for path in names)
        if self.phases.has("local_removed"):
            if present:
                raise DiagnosticJournalError("resource names reappeared after sealed local closure")
            return
        if not self.phases.has("cleanup_intent"):
            raise DiagnosticUnresolved("cleanup has no prior durable authority")
        identity = self.phases.value("resources_created")["identity"]
        if present:
            with DiagnosticResources(self.journal, identity=identity, cleanup=True) as resources:
                resources.remove(self.phases.inventory(), quarantine_nonce=self.phases.value("cleanup_intent")["quarantine_nonce"])
        self.journal.remaining_seconds()
        self.phases.append("local_removed", {"cleanup_intent_sha256": self.phases.latest["cleanup_intent"].sha256,
                                              "resources_identity": identity})

    def ack(self) -> dict[str, Any]:
        if self.phases.has("disposed"):
            return self.phases.final_proof()
        terminal = self.phases.terminal()
        lookup = task_lookup_url_v2(api_origin=self.binding["api_url"],
                                   idempotency_key=self.binding["prepared"]["client_submit_key"])
        recovering_ack = self.phases.has("ack_intent")
        if not recovering_ack:
            self.phases.intent("ack_intent")
        if not self.phases.has("remote_absent"):
            if recovering_ack:
                self.phases.reserve_exchange("ack_exchange_intent")
                self.phases.reserve_wire("ack_lookup")
                status, raw = self.wire.request("GET", lookup)
                if status == 404:
                    self.raw("remote_absent", status, raw)
                else:
                    self.raw("ack_lookup", status, raw)
                    if status != 200 or self.phases.observation(wire_evidence(status, raw), terminal.task_id) != terminal:
                        raise DiagnosticUnresolved("ACK reconciliation original terminal task differs")
            if not self.phases.has("remote_absent"):
                self.request("ack_reply", "POST", task_ack_url_v2(api_origin=self.binding["api_url"], task_id=terminal.task_id))
                value = self.phases.value("ack_reply")
                expected = {"schema": "mineru-task-protocol.v2", "task_id": terminal.task_id, "status": "consumed"}
                if value["http_status"] != 200 or decode_closed_json_v2(
                    wire_bytes(value), required=frozenset(expected), allowed=frozenset(expected)
                ) != expected:
                    raise DiagnosticUnresolved("diagnostic actual ACK response is not proved")
                self.request("remote_absent", "GET", lookup)
        self.phases.append("disposed", self.phases.disposal_seal())
        return self.phases.final_proof()


def run_diagnostic_attempt_v2(
    *, input_pdf: Path, source_pdf_sha256: str, source_byte_count: int, source_page_count: int,
    api_url: str, server_url: str, options: ParserOptions, journal_root: Path,
    attempt_identity: str, fence_identity: str, submission_epoch_unix: int,
    clock_identity_sha256: str, deadline_ns: int, continuous_ns: Callable[[], int],
    resume: bool = False, transport: httpx.BaseTransport | None = None,
    reader: MinerUMediumArtifactReader | None = None, unix_time: Callable[[], float] = time.time,
    pause: Callable[[float], None] = time.sleep, quality_verifier: QualityVerifier | None = None,
    quality_verifier_sha256: str | None = None,
) -> dict[str, Any]:
    """Run or explicitly reconcile one diagnostic; no new-key retry on resume."""
    if (type(resume) is not bool or type(source_byte_count) is not int or not 0 < source_byte_count <= _MAX_INPUT_BYTES
            or type(source_page_count) is not int or not 0 < source_page_count <= 2**31 - 1
            or (quality_verifier is None) != (quality_verifier_sha256 is None)
            or quality_verifier_sha256 is not None and (type(quality_verifier_sha256) is not str
                                                        or not _HASH.fullmatch(quality_verifier_sha256))):
        raise DiagnosticJournalError("diagnostic source bounds or explicit verifier binding differ")
    prepared = prepare_submission_identity_v2(
        api_url=api_url, server_url=server_url, options=options, source_pdf_sha256=source_pdf_sha256,
        attempt_identity=attempt_identity, fence_identity=fence_identity, submission_epoch_unix=submission_epoch_unix)
    binding = {"contract_version": "mineru-diagnostic-binding.v2", "prepared": json.loads(prepared.exact_bytes),
               "api_url": api_url, "server_url": server_url, "options": asdict(options),
               "source_pdf_sha256": source_pdf_sha256, "source_byte_count": source_byte_count,
               "source_page_count": source_page_count, "quality_verifier_sha256": quality_verifier_sha256,
               "target_identity": options.target_identity(ParserIdentity("MinerU", "3.4.4")).to_payload()}
    with DiagnosticJournal(journal_root, create=not resume, attempt_id=attempt_identity,
                           configuration_sha256=_digest(_canonical(binding)), clock_identity_sha256=clock_identity_sha256,
                           deadline_ns=deadline_ns, continuous_ns=continuous_ns) as journal:
        with DiagnosticWireClient(checkpoint=journal.remaining_seconds, transport=transport) as wire:
            attempt = _Attempt(journal, binding, resume=resume, wire=wire, options=options, unix_time=unix_time, pause=pause)
            if not attempt.phases.has("binding"):
                if resume:
                    raise DiagnosticUnresolved("diagnostic binding is not durably established")
                attempt.phases.append("binding", binding)
            if not attempt.phases.has("cleanup_intent"):
                identity = None
                if attempt.phases.has("resources_created"):
                    identity = attempt.phases.value("resources_created")["identity"]
                elif resume:
                    raise DiagnosticUnresolved("diagnostic resource creation has no original identity")
                else:
                    attempt.new_intent("resources")
                with DiagnosticResources(journal, identity=identity) as resources:
                    attempt.refresh()
                    attempt.snapshot(resources, input_pdf)
                    if not attempt.phases.has("source_observed"):
                        attempt.new_intent("source_probe")
                        raw = observe_diagnostic_source(resources, source_pdf_sha256=source_pdf_sha256,
                                                        source_byte_count=source_byte_count)
                        attempt.journal.append("source_observed", {
                            "snapshot_record_sha256": attempt.phases.latest["snapshot_sealed"].sha256,
                            "response_hex": raw.hex(), "response_sha256": _digest(raw)})
                        attempt.refresh()
                        resources.verify_payload("source.pdf", attempt.phases.value("snapshot_sealed"))
                    attempt.submit(resources)
                    attempt.terminal()
                    if attempt.phases.terminal().status == "completed":
                        attempt.artifacts(resources)
                    attempt.validate(resources, reader or MinerUMediumArtifactReader(), quality_verifier)
                    attempt.phases.intent("cleanup_intent")
            attempt.cleanup()
            return attempt.ack()
