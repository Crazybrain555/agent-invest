"""Bind fixed service reports to their original diagnostic journal records."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournalError
from disclosure_anchor.adapters.runtime.mineru_diagnostic_store import _digest
from disclosure_anchor.application.contracts.diagnostic_json import bounded_json_bytes
from disclosure_anchor.application.contracts.m6_service_quality import M6ServiceQualityPlan, verify_service_quality_report
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity

if TYPE_CHECKING:
    from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournal
    from disclosure_anchor.adapters.runtime.mineru_diagnostic_phases import DiagnosticPhases

_MAXIMUM = 2 * 1024 * 1024 + 8192
_BINDING_FIELDS = {"contract_version", "prepared", "api_url", "server_url", "options", "source_pdf_sha256",
                   "source_byte_count", "source_page_count", "target_identity", "quality_verifier_sha256", "service_quality"}
_CONTEXT_FIELDS = {"expected_identity", "expected_topology", "expected_runtime_manifest", "runtime_identity",
                   "task_slots", "task_retention_seconds", "cleanup_interval_seconds", "observability_url",
                   "max_age_seconds", "current"}


def service_quality_plan(journal: DiagnosticJournal, binding: dict[str, Any]) -> M6ServiceQualityPlan | None:
    if "service_quality" not in binding:
        return None
    if binding.get("contract_version") != "mineru-diagnostic-binding.v2" or set(binding) != _BINDING_FIELDS:
        raise DiagnosticJournalError("fixed service quality requires the exact v2 binding")
    raw = bounded_json_bytes(binding, maximum_bytes=_MAXIMUM)
    if _digest(raw) != journal.original_identity.configuration_sha256:
        raise DiagnosticJournalError("service binding differs from original journal header")
    value = binding["service_quality"]
    if (type(value) is not dict or set(value) != {"contract_version", "plan", "baseline_contract"}
            or value["contract_version"] != "m6.service-verifier-binding.v1"):
        raise DiagnosticJournalError("service verifier binding fields differ")
    plan = M6ServiceQualityPlan.from_canonical_bytes(
        bounded_json_bytes(value["plan"], maximum_bytes=_MAXIMUM), maximum_bytes=_MAXIMUM,
    )
    context = value["baseline_contract"]
    if type(context) is not dict or set(context) != _CONTEXT_FIELDS:
        raise DiagnosticJournalError("frozen service baseline expectations differ")
    for name in ("expected_identity", "expected_topology", "expected_runtime_manifest"):
        if type(context[name]) is not dict or not context[name]:
            raise DiagnosticJournalError("frozen service baseline expected object missing")
    for name in ("task_slots", "task_retention_seconds", "cleanup_interval_seconds", "max_age_seconds"):
        if type(context[name]) is not int or context[name] <= 0:
            raise DiagnosticJournalError("frozen service baseline integer expectation invalid")
    for name in ("current", "runtime_identity", "observability_url"):
        if type(context[name]) is not str or not context[name]:
            raise DiagnosticJournalError("frozen service baseline text expectation invalid")
    current = datetime.fromisoformat(context["current"])
    if current.tzinfo is None or current.utcoffset() is None:
        raise DiagnosticJournalError("frozen service baseline current time is not aware")
    target = ParserTargetIdentity.from_payload(binding["target_identity"])
    if (plan.quality_verifier_sha256 != binding["quality_verifier_sha256"]
            or _digest(bounded_json_bytes(target.to_payload(), maximum_bytes=_MAXIMUM)) != plan.parser_target_sha256
            or target.runtime_bundle_identity_sha256 != context["runtime_identity"]):
        raise DiagnosticJournalError("service target/verifier differs from frozen baseline")
    return plan


def validate_service_quality_report(phases: DiagnosticPhases, value: dict[str, Any]) -> None:
    plan = phases.service_quality
    if plan is None or value["outcome"] != "completed":
        return
    quality, provider = value["quality"], value["provider"]
    evidence, qualification = verify_service_quality_report(quality["report"], plan=plan)
    observation = evidence.observation
    binding = phases.binding
    source_ref, output_ref = phases.latest["source_observed"].sha256, phases.latest["output_sealed"].sha256
    expected = {
        "attempt_id": phases.journal.original_identity.attempt_id,
        "source_pdf_sha256": binding["source_pdf_sha256"], "source_byte_count": binding["source_byte_count"],
        "source_page_count": binding["source_page_count"], "provider_page_count": provider["page_count"],
        "provider_bundle_sha256": provider["provider_bundle_sha256"],
        "source_observed_record_sha256": source_ref, "output_sealed_record_sha256": output_ref,
    }
    if any(getattr(observation, name) != actual for name, actual in expected.items()):
        raise DiagnosticJournalError("service quality report refers to different original source/output records")
    if any(check.evidence_sha256 != (source_ref if check.check_id == "source_identity" else output_ref)
           for check in observation.checks):
        raise DiagnosticJournalError("service quality check basis differs from original records")
    status, reason = {
        "scorable": ("pass", "source/provider contract checks passed"),
        "review_pending": ("needs_review", "source/provider review pending"),
        "not_scorable": ("fail", "source/provider checks failed"),
    }[qualification.verdict]
    if quality["status"] != status or quality["reason"] != reason:
        raise DiagnosticJournalError("service quality status/reason differs from its sealed projection")
