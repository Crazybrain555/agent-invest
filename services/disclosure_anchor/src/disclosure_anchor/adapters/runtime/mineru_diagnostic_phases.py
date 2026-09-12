"""Closed private phase replay for diagnostic v2; no publication authority."""

from __future__ import annotations

import json
import re
import secrets
import stat
from typing import Any

from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    MAX_WIRE_JSON_BYTES, TaskProtocolV2Observation, decode_closed_json_v2,
    parse_result_lease_v2, parse_task_payload_v2, validate_absence_payload_v2,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import (
    DiagnosticJournal, DiagnosticJournalError, DiagnosticJournalRecord,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_resources import (
    _relative, validate_resource_identity,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_store import _canonical, _digest
from disclosure_anchor.adapters.runtime.mineru_diagnostic_quality_phases import (
    QUALITY_CREATION_STEPS, owned_quality_configuration, validate_quality_creation,
)
from disclosure_anchor.application.contracts.mineru_api_health import MINERU_API_RESULT_RESERVATION_BYTES

_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_STEPS = (
    "binding", "resources_intent", "resources_created", "snapshot_intent", "snapshot_created",
    "snapshot_sealed", "source_probe_intent", "source_observed", "submit_intent", "submit_reply", "lookup_intent", "lookup_reply", "accepted", "terminal",
    "lease_intent", "lease_reply", "archive_intent", "archive_created", "archive_sealed",
    "output_intent", "output_created", "output_sealed", "validated", "cleanup_intent",
    "local_removed", "ack_intent", "ack_exchange_intent", "ack_reply", "ack_lookup", "remote_absent", "disposed",
)
_BASES = {
    "resources_intent": "binding", "snapshot_intent": "resources_created",
    "source_probe_intent": "snapshot_sealed",
    "submit_intent": "source_observed", "lookup_intent": "submit_intent", "lease_intent": "terminal",
    "archive_intent": "lease_reply", "output_intent": "archive_sealed",
    "cleanup_intent": "validated", "ack_intent": "local_removed", "ack_exchange_intent": "ack_intent",
}
_WIRE = {"submit_reply", "lookup_reply", "terminal", "ack_reply", "ack_lookup", "remote_absent"}
_WIRE_FIELDS = {"http_status", "response_hex", "response_sha256"}


class DiagnosticUnresolved(DiagnosticJournalError):
    """Original evidence and resources remain; no guessed replacement action."""


def _closed(value: object, fields: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise DiagnosticJournalError("diagnostic phase fields are not closed")
    return value


def wire_evidence(status: int, raw: bytes) -> dict[str, Any]:
    return {"http_status": status, "response_hex": raw.hex(), "response_sha256": _digest(raw)}


def wire_bytes(value: dict[str, Any]) -> bytes:
    if (type(value["http_status"]) is not int or not 100 <= value["http_status"] <= 599
            or type(value["response_hex"]) is not str or len(value["response_hex"]) > 2 * MAX_WIRE_JSON_BYTES):
        raise DiagnosticJournalError("diagnostic HTTP evidence bounds differ")
    raw = bytes.fromhex(value["response_hex"])
    if raw.hex() != value["response_hex"] or _digest(raw) != value["response_sha256"]:
        raise DiagnosticJournalError("diagnostic exact HTTP evidence hash differs")
    return raw


def validate_inventory(value: object) -> list[dict[str, Any]]:
    if type(value) is not list or not value:
        raise DiagnosticJournalError("diagnostic output inventory is missing")
    names: set[str] = set()
    for item in value:
        _closed(item, {"path", "identity", "bytes", "sha256"})
        _relative(item["path"])
        if item["path"] in names or not (item["path"] == "output" or item["path"].startswith("output/")):
            raise DiagnosticJournalError("diagnostic output inventory path differs")
        names.add(item["path"])
        identity = validate_resource_identity(item["identity"])
        if stat.S_ISDIR(identity[2]):
            if item["bytes"] is not None or item["sha256"] is not None:
                raise DiagnosticJournalError("diagnostic directory has invented content")
        elif (type(item["bytes"]) is not int or item["bytes"] < 0
              or type(item["sha256"]) is not str or not _HASH.fullmatch(item["sha256"])):
            raise DiagnosticJournalError("diagnostic inventory file seal differs")
    directories = {item["path"] for item in value if stat.S_ISDIR(item["identity"][2])}
    if "output" not in directories:
        raise DiagnosticJournalError("diagnostic inventory output root missing")
    for name in names - {"output"}:
        if name.rsplit("/", 1)[0] not in directories:
            raise DiagnosticJournalError("diagnostic inventory parent missing")
    return value


class DiagnosticPhases:
    """Validate every prior receipt and prerequisite before another side effect."""

    def __init__(self, journal: DiagnosticJournal, binding: dict[str, Any]) -> None:
        self.journal, self.binding = journal, binding
        self.owned_quality = owned_quality_configuration(journal, binding)
        split = _STEPS.index("validated")
        self._steps = (_STEPS if self.owned_quality is None
                       else _STEPS[:split] + QUALITY_CREATION_STEPS + _STEPS[split:])
        self.latest: dict[str, DiagnosticJournalRecord] = {}
        self.history: list[DiagnosticJournalRecord] = []
        for record in journal.records:
            self._validate(record.step, record.value)
            self.latest[record.step] = record
            self.history.append(record)

    def has(self, step: str) -> bool:
        return step in self.latest

    def value(self, step: str) -> dict[str, Any]:
        if step not in self.latest:
            raise DiagnosticJournalError("diagnostic phase prerequisite missing: " + step)
        return json.loads(_canonical(self.latest[step].value))

    def intent(self, step: str) -> None:
        value = {"basis_sha256": self.latest[_BASES[step]].sha256}
        if step == "cleanup_intent":
            value["quarantine_nonce"] = secrets.token_hex(16)
        self.append(step, value)

    def append(self, step: str, value: dict[str, Any]) -> DiagnosticJournalRecord:
        self._validate(step, value)
        record = self.journal.append(step, value)
        self.latest[step] = record
        self.history.append(record)
        return record

    def reserve_wire(self, step: str) -> None:
        # A bounded raw observation must fit before issuing its effect. Keep
        # space for a small final decision; exhaustion remains unresolved.
        current = sum(len(_canonical(record.value)) + 2048 for record in self.history) + 8192
        limit = 5 if step == "ack_reply" else 4 if step in {"lookup_reply", "ack_lookup"} else 1
        if (sum(r.step == step for r in self.history) >= limit
                or len(self.history) + 4 > 96 or current + 2 * MAX_WIRE_JSON_BYTES + 16384 > 16 * 1024 * 1024):
            raise DiagnosticUnresolved("diagnostic wire evidence budget exhausted before request")
        self.journal.remaining_seconds()

    def observation(self, value: dict[str, Any], task_id: str | None = None) -> TaskProtocolV2Observation:
        prepared = self.binding["prepared"]
        return parse_task_payload_v2(
            wire_bytes(value), api_origin=self.binding["api_url"],
            idempotency_key=prepared["client_submit_key"], attempt_identity=prepared["attempt_identity"],
            fence_identity=prepared["fence_identity"], expected_task_id=task_id,
            artifact_byte_limit=MINERU_API_RESULT_RESERVATION_BYTES,
        )

    def accepted(self) -> TaskProtocolV2Observation:
        sha = self.value("accepted")["wire_record_sha256"]
        for record in self.history:
            if record.sha256 == sha and record.step in {"submit_reply", "lookup_reply"}:
                return self.observation(record.value)
        raise DiagnosticJournalError("diagnostic accepted wire observation is missing")

    def terminal(self) -> TaskProtocolV2Observation:
        return self.observation(self.value("terminal"), self.accepted().task_id)

    def inventory(self) -> list[dict[str, Any]]:
        items = [{"path": "source.pdf", **self.value("snapshot_sealed")}]
        if self.terminal().status == "completed":
            items.append({"path": "result.zip", **self.value("archive_sealed")})
            items.extend(self.value("output_sealed")["inventory"])
        return items

    def _validate(self, step: str, value: dict[str, Any]) -> None:
        if step not in self._steps or self.has("disposed"):
            raise DiagnosticJournalError("unknown or closed diagnostic phase")
        if self.owned_quality is not None and step in _STEPS[_STEPS.index("validated"):]:
            raise DiagnosticJournalError("owned quality validation/disposal requires the complete owned runtime")
        repeated = step in {"lookup_intent", "lookup_reply", "ack_exchange_intent", "ack_reply", "ack_lookup"}
        exchanges = ({"lookup_intent", "lookup_reply"}, {"ack_exchange_intent", "ack_reply", "ack_lookup"})
        ack_exchange = self.history and any(step in group and self.history[-1].step in group for group in exchanges)
        if (self.has(step) and not repeated or repeated and sum(r.step == step for r in self.history) >= (5 if step == "ack_reply" else 4)
                or self.history and not ack_exchange and self._steps.index(step) < self._steps.index(self.history[-1].step)):
            raise DiagnosticJournalError("diagnostic duplicate, regressed or exhausted phase")
        if step in QUALITY_CREATION_STEPS:
            validate_quality_creation(self, step, value)
        elif step == "binding":
            if value != self.binding:
                raise DiagnosticJournalError("diagnostic bound configuration changed")
        elif step in _BASES:
            _closed(value, {"basis_sha256"} | ({"quarantine_nonce"} if step == "cleanup_intent" else set()))
            self.value(_BASES[step])
            if value["basis_sha256"] != self.latest[_BASES[step]].sha256:
                raise DiagnosticJournalError("diagnostic action intent basis changed")
            if step == "cleanup_intent" and (type(value["quarantine_nonce"]) is not str
                    or re.fullmatch(r"[0-9a-f]{32}", value["quarantine_nonce"]) is None):
                raise DiagnosticJournalError("diagnostic cleanup namespace nonce differs")
            if step == "lease_intent" and self.terminal().status != "completed":
                raise DiagnosticJournalError("failed diagnostic has no result lease")
        elif step == "source_observed":
            self.value("source_probe_intent")
            _closed(value, {"snapshot_record_sha256", "response_hex", "response_sha256"})
            raw = bytes.fromhex(value["response_hex"])
            expected = {"kind": "valid", "sha256": self.binding["source_pdf_sha256"],
                        "byte_count": self.binding["source_byte_count"], "page_count": self.binding["source_page_count"]}
            payload = decode_closed_json_v2(raw, required=frozenset(expected), allowed=frozenset(expected))
            if (len(raw) > 8192 or raw.hex() != value["response_hex"] or _digest(raw) != value["response_sha256"]
                    or value["snapshot_record_sha256"] != self.latest["snapshot_sealed"].sha256
                    or type(payload["byte_count"]) is not int or type(payload["page_count"]) is not int or payload != expected):
                raise DiagnosticJournalError("diagnostic physical source observation differs")
        elif step.endswith("_created"):
            self.value(step.removesuffix("_created") + "_intent")
            _closed(value, {"identity"})
            identity = validate_resource_identity(value["identity"])
            directory = step in {"resources_created", "output_created"}
            if stat.S_ISDIR(identity[2]) != directory:
                raise DiagnosticJournalError("diagnostic created object type differs")
        elif step in {"snapshot_sealed", "archive_sealed"}:
            prefix = step.removesuffix("_sealed")
            created = self.value(prefix + "_created")
            _closed(value, {"identity", "bytes", "sha256"})
            if prefix == "snapshot":
                size, sha = self.binding["source_byte_count"], self.binding["source_pdf_sha256"]
            else:
                terminal = self.terminal()
                size, sha = terminal.artifact_byte_count, "sha256:" + str(terminal.artifact_sha256)
            if (value["identity"] != created["identity"] or type(value["bytes"]) is not int
                    or value["bytes"] != size or value["sha256"] != sha):
                raise DiagnosticJournalError("diagnostic sealed payload/source/result differs")
        elif step in _WIRE or step == "lease_reply":
            _closed(value, _WIRE_FIELDS | ({"observed_unix"} if step == "lease_reply" else set()))
            wire_bytes(value)
            prerequisite = {"submit_reply": "submit_intent", "lookup_reply": "lookup_intent",
                            "terminal": "accepted", "lease_reply": "lease_intent",
                            "ack_reply": "ack_intent", "ack_lookup": "ack_exchange_intent", "remote_absent": "ack_intent"}[step]
            self.value(prerequisite)
            if step == "terminal":
                if value["http_status"] != 200 or self.observation(value, self.accepted().task_id).status not in {"completed", "failed"}:
                    raise DiagnosticJournalError("diagnostic terminal is not proved")
            elif step == "lease_reply":
                if value["http_status"] != 200:
                    raise DiagnosticJournalError("diagnostic lease response unsuccessful")
                parse_result_lease_v2(wire_bytes(value), task_id=self.terminal().task_id,
                                     observed_at_unix=value["observed_unix"])
            elif step == "remote_absent":
                if value["http_status"] != 404:
                    raise DiagnosticJournalError("diagnostic remote absence not proved")
                validate_absence_payload_v2(wire_bytes(value))
        elif step == "accepted":
            _closed(value, {"wire_record_sha256"})
            source = next((r for r in self.history if r.sha256 == value["wire_record_sha256"]), None)
            if source is None or source.step not in {"submit_reply", "lookup_reply"} or source.value["http_status"] not in {200, 202}:
                raise DiagnosticJournalError("diagnostic acceptance has no original wire proof")
            self.observation(source.value)
        elif step == "output_sealed":
            created = self.value("output_created")
            _closed(value, {"inventory", "inventory_sha256"})
            inventory = validate_inventory(value["inventory"])
            if (_digest(_canonical(inventory)) != value["inventory_sha256"]
                    or next(item for item in inventory if item["path"] == "output")["identity"] != created["identity"]):
                raise DiagnosticJournalError("diagnostic materialization seal changed")
        elif step == "validated":
            terminal = self.terminal()
            _closed(value, {"outcome", "provider", "quality"})
            if value["outcome"] != terminal.status:
                raise DiagnosticJournalError("diagnostic validation terminal outcome differs")
            quality = _closed(value["quality"], {"status", "reason", "verifier_sha256", "report"})
            if (quality["status"] not in {"pass", "fail", "needs_review", "unverified", "not_applicable"}
                    or type(quality["reason"]) is not str or not quality["reason"]
                    or type(quality["report"]) is not dict
                    or quality["verifier_sha256"] is not None and not _HASH.fullmatch(quality["verifier_sha256"])):
                raise DiagnosticJournalError("diagnostic quality evidence differs")
            if terminal.status == "completed":
                self.value("output_sealed")
                provider = _closed(value["provider"], {"target_identity", "provider_bundle_sha256", "page_count", "block_count", "artifact_count"})
                if (type(provider["page_count"]) is not int or provider["page_count"] != self.binding["source_page_count"]
                        or any(type(provider[k]) is not int or provider[k] < 0 for k in ("block_count", "artifact_count"))
                        or type(provider["provider_bundle_sha256"]) is not str or not _HASH.fullmatch(provider["provider_bundle_sha256"])
                        or provider["target_identity"] != self.binding["target_identity"]
                        or quality["status"] == "not_applicable"):
                    raise DiagnosticJournalError("diagnostic provider validation source/profile differs")
            elif value["provider"] is not None or quality["status"] != "not_applicable":
                raise DiagnosticJournalError("failed provider cannot invent result or quality")
            if quality["status"] not in {"unverified", "not_applicable"} and quality["verifier_sha256"] is None:
                raise DiagnosticJournalError("diagnostic quality lacks explicit verifier identity")
            if terminal.status == "completed" and quality["verifier_sha256"] != self.binding["quality_verifier_sha256"]:
                raise DiagnosticJournalError("diagnostic quality verifier differs from bound configuration")
        elif step == "local_removed":
            _closed(value, {"cleanup_intent_sha256", "resources_identity"})
            self.value("cleanup_intent")
            if (value["cleanup_intent_sha256"] != self.latest["cleanup_intent"].sha256
                    or value["resources_identity"] != self.value("resources_created")["identity"]):
                raise DiagnosticJournalError("diagnostic local closure authority differs")
        elif step == "disposed":
            if value != self.disposal_seal():
                raise DiagnosticJournalError("diagnostic final receipt differs from exact evidence")

    def reserve_exchange(self, step: str) -> None:
        if sum(record.step == step for record in self.history) >= 4:
            raise DiagnosticUnresolved("diagnostic reconciliation request budget exhausted before request")
        self.reserve_wire("lookup_reply" if step == "lookup_intent" else "ack_lookup")
        self.intent(step)

    def disposal_seal(self) -> dict[str, Any]:
        # Exact wire/report bytes already live in bounded immutable records.
        # Do not duplicate several maximum-size bodies in one final record.
        return {"contract_version": "mineru-diagnostic-disposal-seal.v2",
                "proof_sha256": _digest(_canonical(self.final_proof())),
                "validation_record_sha256": self.latest["validated"].sha256,
                "local_closure_record_sha256": self.latest["local_removed"].sha256,
                "ack_intent_record_sha256": self.latest["ack_intent"].sha256,
                "absence_record_sha256": self.latest["remote_absent"].sha256}

    def final_proof(self) -> dict[str, Any]:
        self.value("remote_absent")
        validated = self.value("validated")
        actual: DiagnosticJournalRecord | None = None
        expected = {"schema": "mineru-task-protocol.v2", "task_id": self.terminal().task_id, "status": "consumed"}
        for record in self.history:
            if record.step == "ack_reply" and record.value["http_status"] == 200:
                raw = wire_bytes(record.value)
                try:
                    payload = decode_closed_json_v2(raw, required=frozenset(expected), allowed=frozenset(expected))
                except ValueError:
                    continue
                if payload == expected:
                    actual = record
        return {
            "contract_version": "mineru-diagnostic-disposal.v2", "authority": "diagnostic-no-publication.v2",
            "outcome": validated["outcome"], "prepared": self.binding["prepared"],
            "source_pdf_sha256": self.binding["source_pdf_sha256"], "source_page_count": self.binding["source_page_count"],
            "task_id": self.terminal().task_id, "provider": validated["provider"], "quality": validated["quality"],
            "validation_record_sha256": self.latest["validated"].sha256,
            "cleanup_record_sha256": self.latest["local_removed"].sha256,
            "ack_proof": {"kind": "actual_response" if actual is not None else "reconciled_absence",
                          "intent_record_sha256": self.latest["ack_intent"].sha256,
                          "response": None if actual is None else actual.value,
                          "absence": self.value("remote_absent")},
        }
