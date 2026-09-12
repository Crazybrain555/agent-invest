"""Closed original-binding and creation receipts for the owned quality store.

This first IO seam does not authorize semantic children, validation or disposal.
Those later phases must acquire their own closed schemas before being enabled.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING, Any

from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import (
    DiagnosticJournal, DiagnosticJournalError,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_store import _digest
from disclosure_anchor.application.contracts.diagnostic_json import bounded_json_bytes
from disclosure_anchor.application.contracts.mineru_diagnostic_quality import (
    QUALITY_FILE_SLOTS, RetainedFileSeal,
)
from disclosure_anchor.application.contracts.mineru_diagnostic_quality_config import (
    OwnedDiagnosticQualityConfig,
)

if TYPE_CHECKING:
    from disclosure_anchor.adapters.runtime.mineru_diagnostic_phases import DiagnosticPhases

QUALITY_CREATION_STEPS = (
    "quality_intent", "quality_root_created", "quality_files_created", "quality_input_sealed",
)
_BASIS = dict(zip(QUALITY_CREATION_STEPS, ("output_sealed", *QUALITY_CREATION_STEPS[:-1]), strict=True))
_EXTRA = {
    "quality_intent": {"retained_parent_identity", "retained_name", "snapshot_record_sha256", "output_record_sha256"},
    "quality_root_created": {"root_identity"},
    "quality_files_created": {"files"},
    "quality_input_sealed": {"input_file"},
}
_BINDING_FIELDS = {"contract_version", "prepared", "api_url", "server_url", "options", "source_pdf_sha256",
                   "source_byte_count", "source_page_count", "target_identity", "owned_quality"}
_MAX_RECORD_BYTES = 2 * 1024 * 1024 + 8192


def owned_quality_configuration(
    journal: DiagnosticJournal, binding: dict[str, Any],
) -> OwnedDiagnosticQualityConfig | None:
    if binding.get("contract_version") == "mineru-diagnostic-binding.v2":
        return None
    if binding.get("contract_version") != "mineru-diagnostic-binding.v3":
        raise DiagnosticJournalError("unknown diagnostic binding version")
    if set(binding) != _BINDING_FIELDS:
        raise DiagnosticJournalError("owned diagnostic binding fields differ")
    raw = bounded_json_bytes(binding, maximum_bytes=_MAX_RECORD_BYTES)
    if _digest(raw) != journal.original_identity.configuration_sha256:
        raise DiagnosticJournalError("owned diagnostic binding differs from original header")
    config = OwnedDiagnosticQualityConfig.from_payload(binding["owned_quality"])
    if config.retained_name != journal.root.name + ".quality":
        raise DiagnosticJournalError("owned retained name differs from original journal sibling")
    return config


def quality_creation_value(phases: DiagnosticPhases, step: str, **extra: Any) -> dict[str, Any]:
    if step not in _BASIS or phases.owned_quality is None:
        raise DiagnosticJournalError("owned quality creation branch required")
    return {"contract_version": "mineru-owned-quality.phase.v1",
            "configuration_sha256": phases.journal.original_identity.configuration_sha256,
            "basis_record_sha256": phases.latest[_BASIS[step]].sha256, **extra}


def _identity(value: object, *, mode: int | None) -> list[int]:
    if (type(value) is not list or len(value) != 4
            or any(type(n) is not int or not 0 <= n <= 2**63 - 1 for n in value)
            or value[1] < 1):
        raise DiagnosticJournalError("owned quality creation identity shape differs")
    if mode is None:
        if not stat.S_ISDIR(value[2]):
            raise DiagnosticJournalError("owned quality parent is not a directory")
    elif value[2] != mode or value[3] != os.getuid():
        raise DiagnosticJournalError("owned quality created identity mode/owner differs")
    return value


def validate_quality_creation(phases: DiagnosticPhases, step: str, value: dict[str, Any]) -> None:
    config = phases.owned_quality
    if config is None or step not in QUALITY_CREATION_STEPS:
        raise DiagnosticJournalError("owned quality creation requires the original v3 branch")
    fields = {"contract_version", "configuration_sha256", "basis_record_sha256"} | _EXTRA[step]
    if type(value) is not dict or set(value) != fields:
        raise DiagnosticJournalError("owned quality creation phase fields differ")
    phases.value(_BASIS[step])
    if (value["contract_version"] != "mineru-owned-quality.phase.v1"
            or value["configuration_sha256"] != phases.journal.original_identity.configuration_sha256
            or value["basis_record_sha256"] != phases.latest[_BASIS[step]].sha256):
        raise DiagnosticJournalError("owned quality phase original configuration/basis differs")
    if step == "quality_intent":
        phases.value("source_observed")
        if (phases.terminal().status != "completed" or value["retained_name"] != config.retained_name
                or value["snapshot_record_sha256"] != phases.latest["snapshot_sealed"].sha256
                or value["output_record_sha256"] != phases.latest["output_sealed"].sha256):
            raise DiagnosticJournalError("owned quality intent source/output differs")
        _identity(value["retained_parent_identity"], mode=None)
    elif step == "quality_root_created":
        _identity(value["root_identity"], mode=0o40700)
    elif step == "quality_files_created":
        files = value["files"]
        if type(files) is not list or len(files) != len(QUALITY_FILE_SLOTS):
            raise DiagnosticJournalError("owned quality requires all sixteen original slots")
        seen: set[tuple[int, int]] = set()
        for slot, item in zip(QUALITY_FILE_SLOTS, files, strict=True):
            if type(item) is not dict or set(item) != {"slot", "identity"} or item["slot"] != slot:
                raise DiagnosticJournalError("owned quality fixed slot order differs")
            identity = _identity(item["identity"], mode=0o100600)
            key = (identity[0], identity[1])
            if key in seen:
                raise DiagnosticJournalError("owned quality distinct slots share an original inode")
            seen.add(key)
    else:
        seal = RetainedFileSeal.from_payload(value["input_file"])
        created = phases.value("quality_files_created")["files"][0]
        if (seal.slot != "input.json" or seal.evidence_kind != "complete"
                or not 0 < seal.byte_count <= min(config.budget.slot_limit(seal.slot), config.budget.retained_total_bytes)
                or list(seal.identity) != created["identity"]):
            raise DiagnosticJournalError("owned quality input seal differs from original slot/budget")
