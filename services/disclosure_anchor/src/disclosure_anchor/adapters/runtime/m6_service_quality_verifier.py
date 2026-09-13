"""Fixed service-only checks over the original diagnostic owner's held inputs.

No Unit construction, publication, remote request or cleanup authority. The
baseline is an accepted stock held-out receipt, checked against independently
supplied deployment expectations before an attempt can acquire resources.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import MinerUMediumArtifactReader
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import verify_mineru_heldout_validation
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournalError
from disclosure_anchor.adapters.runtime.mineru_diagnostic_phases import DiagnosticPhases
from disclosure_anchor.adapters.runtime.mineru_diagnostic_quality_inputs import hold_quality_inputs
from disclosure_anchor.adapters.runtime.mineru_diagnostic_resources import DiagnosticResources, _stream
from disclosure_anchor.adapters.runtime.mineru_diagnostic_store import _canonical, _digest
from disclosure_anchor.adapters.runtime.mineru_identity import _WRITER_CODE_RELPATHS, writer_code_digest
from disclosure_anchor.application.contracts._provider_content import validate_provider_content
from disclosure_anchor.application.contracts.diagnostic_json import bounded_json_bytes
from disclosure_anchor.application.contracts.m6_service_quality import (
    M6_SERVICE_PROVIDER_CHECKS, M6ServiceCheckResult, M6ServiceDocumentQualification,
    M6ServiceQualificationEvidence, M6ServiceQualificationObservation, M6ServiceQualityPlan,
    qualify_service_document,
)
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.contracts.provider_document import ProviderDocument
from disclosure_anchor.application.contracts.strict_json import strict_json_loads

_MAX_PLAN = 2 * 1024 * 1024 + 8192
_MAX_BASELINE = 16 * 1024 * 1024
_CONTEXT_FIELDS = frozenset({
    "expected_identity", "expected_topology", "expected_runtime_manifest", "runtime_identity",
    "task_slots", "task_retention_seconds", "cleanup_interval_seconds", "observability_url",
    "max_age_seconds", "current",
})
_EXTRA_SOURCE_PATHS = (
    "adapters/runtime/m6_service_quality_verifier.py",
    "adapters/runtime/m6_service_quality_phases.py",
    "adapters/runtime/mineru_diagnostic_lifecycle.py",
    "adapters/runtime/mineru_diagnostic_phases.py",
    "adapters/runtime/mineru_diagnostic_source.py",
    "adapters/runtime/mineru_diagnostic_journal.py",
    "adapters/runtime/mineru_diagnostic_resources.py",
    "adapters/runtime/mineru_diagnostic_store.py",
    "adapters/runtime/mineru_diagnostic_quality_inputs.py",
    "adapters/runtime/mineru_diagnostic_quality_phases.py",
    "application/contracts/_provider_content.py",
    "application/contracts/provider_document.py",
    "application/contracts/parser_target.py",
    "application/contracts/diagnostic_json.py",
    "application/contracts/m6_service_quality.py",
    "application/contracts/m6_common.py",
    "application/contracts/m6_document_qualification.py",
)


def service_quality_verifier_identity_sha256() -> str:
    """Finite source identity; not proof of every loaded/native dependency."""
    service = Path(__file__).resolve().parents[4]
    digest = hashlib.sha256(b"m6.service-quality-verifier.v1\0")
    digest.update(writer_code_digest().encode("ascii"))
    for relative in _EXTRA_SOURCE_PATHS:
        name = "src/disclosure_anchor/" + relative
        if name in _WRITER_CODE_RELPATHS:
            continue
        path = service / name
        if not path.is_file() or path.is_symlink():
            raise DiagnosticJournalError("service verifier source missing or unsafe: " + name)
        raw = path.read_bytes()
        digest.update(name.encode("utf-8") + b"\0" + str(len(raw)).encode("ascii") + b"\0" + raw)
    return "sha256:" + digest.hexdigest()


def _read_private(path: Path, maximum: int) -> bytes:
    with _stream(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC), "rb") as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_uid != os.getuid() or before.st_nlink != 1
                or not 0 < before.st_size <= maximum):
            raise DiagnosticJournalError("service verifier input must be bounded, private and owned")
        raw = source.read(maximum + 1)
        after = os.fstat(source.fileno())
        named = path.stat(follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        def identity(info: os.stat_result) -> tuple[int, ...]:
            return tuple(getattr(info, name) for name in fields)
        if len(raw) != before.st_size or identity(before) != identity(after) or identity(before) != identity(named):
            raise DiagnosticJournalError("service verifier input changed during its original read")
    return raw


def _freeze_context(value: Mapping[str, object]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CONTEXT_FIELDS:
        raise DiagnosticJournalError("service baseline expectations must have the exact existing fields")
    current = value["current"]
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise DiagnosticJournalError("service baseline current time must be an actual aware datetime")
    for name in ("task_slots", "task_retention_seconds", "cleanup_interval_seconds", "max_age_seconds"):
        item = value[name]
        if type(item) is not int or item <= 0:
            raise DiagnosticJournalError("service baseline integer expectation is invalid: " + name)
    for name in ("expected_identity", "expected_topology", "expected_runtime_manifest"):
        if type(value[name]) is not dict or not value[name]:
            raise DiagnosticJournalError("service baseline expected object is missing: " + name)
    for name in ("runtime_identity", "observability_url"):
        if type(value[name]) is not str or not value[name]:
            raise DiagnosticJournalError("service baseline text expectation is missing: " + name)
    return json.loads(bounded_json_bytes(
        {**value, "current": current.astimezone(UTC).isoformat()}, maximum_bytes=_MAX_PLAN,
    ))


@dataclass(frozen=True, slots=True)
class ServiceQualityVerification:
    document: ProviderDocument
    evidence: M6ServiceQualificationEvidence
    qualification: M6ServiceDocumentQualification

    def quality_payload(self) -> dict[str, object]:
        status, reason = {
            "scorable": ("pass", "source/provider contract checks passed"),
            "review_pending": ("needs_review", "source/provider review pending"),
            "not_scorable": ("fail", "source/provider checks failed"),
        }[self.qualification.verdict]
        return {"status": status, "reason": reason, "report": {
            "evidence": self.evidence.model_dump(mode="json"),
            "qualification": self.qualification.model_dump(mode="json"),
        }}


class ServiceQualityVerifier:
    """Factory-checked immutable bytes, rather than a caller-supplied verdict."""

    __slots__ = ("_plan_raw", "_binding_raw")
    _plan_raw: bytes
    _binding_raw: bytes

    def __init__(self) -> None:
        raise TypeError("load the fixed service verifier from actual plan and baseline files")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("service verifier configuration is immutable")

    @property
    def plan(self) -> M6ServiceQualityPlan:
        return M6ServiceQualityPlan.from_canonical_bytes(self._plan_raw, maximum_bytes=_MAX_PLAN)

    @property
    def identity_sha256(self) -> str:
        return self.plan.quality_verifier_sha256

    def binding_payload(self) -> dict[str, Any]:
        return json.loads(self._binding_raw)

    def verify_result(self, *, resources: DiagnosticResources, phases: DiagnosticPhases) -> ServiceQualityVerification:
        if type(resources) is not DiagnosticResources or type(phases) is not DiagnosticPhases:
            raise DiagnosticJournalError("fixed service verifier requires original resources and phases")
        journal = resources.journal
        if phases.journal is not journal:
            raise DiagnosticJournalError("service verifier inputs have different journal owners")
        identity = journal.original_identity
        resources.checkpoint()
        original = DiagnosticPhases(journal, phases.binding)
        binding = original.binding
        plan = self.plan
        if (_digest(_canonical(binding)) != identity.configuration_sha256
                or binding.get("service_quality") != self.binding_payload()
                or binding.get("quality_verifier_sha256") != self.identity_sha256
                or service_quality_verifier_identity_sha256() != self.identity_sha256):
            raise DiagnosticJournalError("service verifier differs from original configuration or source")
        if (original.has("cleanup_intent") or not original.has("source_observed")
                or not original.has("output_sealed") or original.terminal().status != "completed"):
            raise DiagnosticJournalError("service checks require completed original inputs before cleanup")
        target = ParserTargetIdentity.from_payload(binding["target_identity"])
        if _digest(_canonical(target.to_payload())) != plan.parser_target_sha256:
            raise DiagnosticJournalError("service target differs from accepted baseline")
        with hold_quality_inputs(resources=resources, phases=original) as held:
            document = MinerUMediumArtifactReader().read_pinned(
                held.output_tree, source_pdf_sha256=binding["source_pdf_sha256"],
            ).document
            resources.checkpoint()
            validate_provider_content(document, target)
            if (document.source_pdf_sha256 != binding["source_pdf_sha256"]
                    or len(document.pages) != binding["source_page_count"]
                    or tuple(page.page_index for page in document.pages) != tuple(range(binding["source_page_count"]))):
                raise DiagnosticJournalError("service source/provider complete page identity differs")
            held.verify_unchanged()
        resources.checkpoint()
        source_record = original.latest["source_observed"].sha256
        output_record = original.latest["output_sealed"].sha256
        evidence = M6ServiceQualificationEvidence(
            observation=M6ServiceQualificationObservation(
                mode="service_diagnostic", attempt_id=identity.attempt_id,
                source_pdf_sha256=binding["source_pdf_sha256"], source_byte_count=binding["source_byte_count"],
                source_page_count=binding["source_page_count"], provider_page_count=len(document.pages),
                provider_bundle_sha256=document.bundle_sha256, parser_target_sha256=plan.parser_target_sha256,
                quality_verifier_sha256=self.identity_sha256, source_observed_record_sha256=source_record,
                output_sealed_record_sha256=output_record, review_reasons=(),
                checks=tuple(M6ServiceCheckResult(check_id=name, outcome="pass", evidence_sha256=(
                    source_record if name == "source_identity" else output_record
                )) for name in M6_SERVICE_PROVIDER_CHECKS),
            ), reviews=(),
        )
        return ServiceQualityVerification(document, evidence, qualify_service_document(evidence, plan))


def load_service_quality_verifier(
    plan_path: Path, baseline_path: Path, *, baseline_contract: Mapping[str, object],
) -> ServiceQualityVerifier:
    plan_raw = _read_private(plan_path, _MAX_PLAN)
    plan = M6ServiceQualityPlan.from_canonical_bytes(plan_raw, maximum_bytes=_MAX_PLAN)
    if plan.quality_verifier_sha256 != service_quality_verifier_identity_sha256():
        raise DiagnosticJournalError("service plan does not identify the fixed verifier implementation")
    baseline_raw = _read_private(baseline_path, _MAX_BASELINE)
    if _digest(baseline_raw) != plan.parser_baseline_evidence_sha256:
        raise DiagnosticJournalError("service baseline original bytes differ from the frozen plan")
    frozen = _freeze_context(baseline_contract)
    context = {**frozen, "current": datetime.fromisoformat(frozen["current"])}
    baseline = strict_json_loads(baseline_raw)
    if type(baseline) is not dict:
        raise DiagnosticJournalError("service baseline must be an existing held-out object")
    verified = verify_mineru_heldout_validation(baseline, **context)
    for item in baseline["documents"]:
        target = ParserTargetIdentity.from_payload(item["receipt"]["provider"]["target_identity"])
        if (_digest(_canonical(target.to_payload())) != plan.parser_target_sha256
                or target.runtime_bundle_identity_sha256 != verified.runtime_identity_sha256):
            raise DiagnosticJournalError("held-out baseline target differs from the frozen service plan")
    binding = {"contract_version": "m6.service-verifier-binding.v1", "plan": json.loads(plan_raw),
               "baseline_contract": frozen}
    raw = _canonical(binding)
    if len(raw) > _MAX_PLAN:
        raise DiagnosticJournalError("service binding exceeds the original diagnostic record bound")
    verifier = object.__new__(ServiceQualityVerifier)
    object.__setattr__(verifier, "_plan_raw", plan_raw)
    object.__setattr__(verifier, "_binding_raw", raw)
    return verifier
