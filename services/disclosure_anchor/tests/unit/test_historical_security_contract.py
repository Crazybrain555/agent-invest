"""Closed-input acceptance for historical bindings and retained replay plans."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import unittest

from disclosure_anchor.application.contracts.historical_security_registration import (
    ContractViolation,
    HistoricalSecurityBindingPlanV1,
    RetainedRegistrationPlanV1,
    candidate_sha256,
    failed_access_projection_sha256,
    load_binding,
    load_canonical_plan,
    load_request,
    plan_bytes,
)
from disclosure_anchor.domain.entities import SourceAccess


_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64
_COMPANY = "co_" + "0" * 26
_CURRENT_SECURITY = "sec_" + "1" * 26
_OLD_SECURITY = "sec_" + "2" * 26
_IDENTIFIER = "ci_" + "3" * 26
_PROFILE_ACCESS = "sa_" + "4" * 26
_INDEX_ACCESS = "sa_" + "5" * 26
_BINDING_ACCESS = "sa_" + "6" * 26
_FAILED_ACCESS = "sa_" + "7" * 26


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _binding_document() -> dict[str, object]:
    return {
        "schema": "historical-security-binding.v1",
        "provider": "cninfo",
        "event_kind": "same_legal_entity_security_code_change",
        "target_company_id": _COMPANY,
        "exchange": "SZSE",
        "current_security_id": _CURRENT_SECURITY,
        "current_code": "302132",
        "old_code": "300114",
        "security_code_effective_date": "2025-02-17",
        "previous_security_short_name": "旧证券简称",
        "current_security_short_name": "新证券简称",
        "expected_uscc": {
            "identifier_id": _IDENTIFIER,
            "value": "91320000TEST000001",
            "profile_source_access_id": _PROFILE_ACCESS,
        },
        "official_evidence": {
            "url": "https://example.org/announcements/code-change.pdf",
            "sha256": _HASH_A,
            "byte_count": 123,
            "announcement_id": "公告-2025-028",
            "pages": [1, 3],
            "local_evidence_ref": "evidence/code-change.pdf",
        },
        "approved_index_interfaces": ["cninfo:p_info3015"],
        "approved_query": {
            "company_id": _COMPANY,
            "security_id": _CURRENT_SECURITY,
        },
        "approved_announcement_range": {
            "from_inclusive": "2023-03-25",
            "to_exclusive": "2025-02-17",
        },
        "query_org_observation": {
            "value": "profile-org-context",
            "provenance": "profile_context",
            "source_access_id": _PROFILE_ACCESS,
        },
        "decided_by": "fixture reviewer",
        "decided_at": "2025-09-25T10:00:00+08:00",
        "reason": "官方公告与当前强键分别核验；子公司并非上市主体别名。",
        "decision_basis": [{"ref": "official-announcement", "sha256": _HASH_A}],
    }


def _retained_plan_document() -> dict[str, object]:
    return {
        "schema": "retained-registration-plan.v1",
        "provider": "cninfo",
        "binding_source_access_id": _BINDING_ACCESS,
        "binding_sha256": _HASH_A,
        "request_sha256": _HASH_B,
        "code_identity": _HASH_A,
        "max_items": 1,
        "item_count": 1,
        "total_byte_count": 17,
        "items": [{
            "sequence": 1,
            "failed_source_access_id": _FAILED_ACCESS,
            "failed_access_projection_sha256": _HASH_A,
            "failure_error_code": "registration_metadata_error",
            "failure_reason": "未登记历史证券",
            "provider_document_id": "pid-1",
            "index_source_access_id": _INDEX_ACCESS,
            "index_result_hash": _HASH_A,
            "index_provider_interface": "cninfo:p_info3015",
            "candidate_sha256": _HASH_B,
            "announcement_date": "2024-03-01",
            "original_candidate_code": "300114",
            "exchange": "SZSE",
            "acquisition_scope_company_id": _COMPANY,
            "acquisition_scope_security_id": _CURRENT_SECURITY,
            "target_company_id": _COMPANY,
            "target_security_id": _OLD_SECURITY,
            "raw_file_relpath": f"raw_documents/cninfo/300114/2024/pid-1/sha256_{'a' * 64}.pdf",
            "raw_file_hash": _HASH_A,
            "byte_count": 17,
            "association_basis": "post_failure_archive_inventory",
            "document_state": "absent",
            "existing_document_id": None,
            "preview_state": "ready",
            "existing_receipt_source_access_id": None,
        }],
    }


def _load_retained_plan(document: dict[str, object]) -> RetainedRegistrationPlanV1:
    payload = plan_bytes(document)
    return load_canonical_plan(
        payload, expected_sha256=_sha(payload),
        model=RetainedRegistrationPlanV1, label="retained plan",
    )


class HistoricalSecurityContractTests(unittest.TestCase):
    def test_binding_unicode_roundtrip_and_content_identity(self) -> None:
        document = _binding_document()
        first = load_binding(_json_bytes(document))
        second = load_binding(first.canonical_bytes())

        self.assertEqual(second.to_document(), document)
        self.assertEqual(first.sha256(), second.sha256())
        self.assertIn("官方公告".encode(), first.canonical_bytes())
        self.assertEqual(first.to_document()["official_evidence"]["announcement_id"], "公告-2025-028")

        changed = deepcopy(document)
        changed["reason"] = "另一项具名决定"
        self.assertNotEqual(load_binding(_json_bytes(changed)).sha256(), first.sha256())

    def test_closed_binding_rejects_ambiguous_or_unrepresentable_input(self) -> None:
        base = _binding_document()
        malformed = [
            ("unknown", {**base, "unreviewed_company": _COMPANY}),
            ("missing evidence", {key: value for key, value in base.items() if key != "official_evidence"}),
            ("missing evidence pages", {**base, "official_evidence": {
                **base["official_evidence"], "pages": [],
            }}),
            ("NUL", {**base, "reason": "reason\x00hidden"}),
            ("bad date", {**base, "security_code_effective_date": "2025-02-30"}),
            ("empty range", {**base, "approved_announcement_range": {
                "from_inclusive": "2024-01-01", "to_exclusive": "2024-01-01",
            }}),
            ("range after code change", {**base, "approved_announcement_range": {
                "from_inclusive": "2024-01-01", "to_exclusive": "2025-02-18",
            }}),
            ("escaping evidence reference", {**base, "official_evidence": {
                **base["official_evidence"], "local_evidence_ref": "../outside.pdf",
            }}),
        ]
        for label, document in malformed:
            with self.subTest(label=label), self.assertRaises(ContractViolation):
                load_binding(_json_bytes(document))

        raw = _json_bytes(base)
        duplicate = raw.replace(b'"schema":', b'"schema":"historical-security-binding.v1","schema":', 1)
        surrogate = deepcopy(base)
        surrogate["reason"] = "reason\ud800"
        for label, payload in (
            ("duplicate key", duplicate),
            ("NaN", raw.replace(b'"byte_count":123', b'"byte_count":NaN', 1)),
            ("surrogate JSON escape", json.dumps(surrogate, ensure_ascii=True).encode("ascii")),
        ):
            with self.subTest(label=label), self.assertRaises(ContractViolation):
                load_binding(payload)

    def test_binding_plan_hash_and_evidence_are_bound(self) -> None:
        binding = load_binding(_json_bytes(_binding_document()))
        plan = {
            "schema": "historical-security-binding-plan.v1",
            "binding": binding.to_document(),
            "binding_sha256": binding.sha256(),
            "evidence_file": {"sha256": _HASH_A, "byte_count": 123},
            "preflight": {
                "action": "create_historical_security",
                "historical_security_id": None,
                "existing_binding_source_access_ids": [],
                "company_legal_name": "上市主体",
                "tracked_security_id": _CURRENT_SECURITY,
            },
            "code_identity": _HASH_B,
        }
        payload = plan_bytes(plan)
        loaded = load_canonical_plan(
            payload, expected_sha256=_sha(payload),
            model=HistoricalSecurityBindingPlanV1, label="binding plan",
        )
        self.assertEqual(loaded.binding.to_document(), binding.to_document())

        changed = deepcopy(plan)
        changed["binding"]["reason"] = "变更决定但沿用旧 hash"
        with self.assertRaises(ContractViolation):
            changed_bytes = plan_bytes(changed)
            load_canonical_plan(
                changed_bytes, expected_sha256=_sha(changed_bytes),
                model=HistoricalSecurityBindingPlanV1, label="binding plan",
            )
        changed = deepcopy(plan)
        changed["evidence_file"]["byte_count"] = 124
        with self.assertRaises(ContractViolation):
            changed_bytes = plan_bytes(changed)
            load_canonical_plan(
                changed_bytes, expected_sha256=_sha(changed_bytes),
                model=HistoricalSecurityBindingPlanV1, label="binding plan",
            )

        with self.assertRaises(ContractViolation):
            load_canonical_plan(
                payload, expected_sha256=_HASH_A,
                model=HistoricalSecurityBindingPlanV1, label="binding plan",
            )
        with self.assertRaises(ContractViolation):
            load_canonical_plan(
                _json_bytes(plan), expected_sha256=_sha(_json_bytes(plan)),
                model=HistoricalSecurityBindingPlanV1, label="binding plan",
            )

    def test_retained_plan_roundtrip_and_unsafe_archive_paths(self) -> None:
        plan = _retained_plan_document()
        loaded = _load_retained_plan(plan)
        self.assertEqual(loaded.to_document(), plan)
        self.assertIn("未登记历史证券".encode(), loaded.canonical_bytes())

        original = plan["items"][0]["raw_file_relpath"]
        for label, unsafe in (
            ("parent pid", f"raw_documents/cninfo/300114/2024/../sha256_{'a' * 64}.pdf"),
            ("backslash", original.replace("/pid-1/", "/pid-1\\../")),
            ("absolute", "/" + original),
        ):
            changed = deepcopy(plan)
            changed["items"][0]["raw_file_relpath"] = unsafe
            with self.subTest(label=label), self.assertRaises(ContractViolation):
                _load_retained_plan(changed)

        changed = deepcopy(plan)
        changed["items"][0]["raw_file_hash"] = _HASH_B
        with self.assertRaises(ContractViolation):
            _load_retained_plan(changed)
        changed = deepcopy(plan)
        changed["total_byte_count"] = 18
        with self.assertRaises(ContractViolation):
            _load_retained_plan(changed)

    def test_exact_request_hash_and_candidate_identity_change_with_source(self) -> None:
        request = {"schema": "retained-registration-request.v1", "items": [{
            "failed_source_access_id": _FAILED_ACCESS,
            "index_source_access_id": _INDEX_ACCESS,
            "expected_raw_file_hash": _HASH_A,
            "expected_byte_count": 17,
        }]}
        loaded, first_hash = load_request(_json_bytes(request))
        self.assertEqual(loaded.to_document(), request)
        changed = deepcopy(request)
        changed["items"][0]["expected_raw_file_hash"] = _HASH_B
        self.assertNotEqual(load_request(_json_bytes(changed))[1], first_hash)

        candidate = {"provider_document_id": "pid-1", "title": "原中文标题"}
        first_candidate_hash = candidate_sha256(candidate)
        candidate["title"] = "不同标题"
        self.assertNotEqual(candidate_sha256(candidate), first_candidate_hash)
        with self.assertRaises(ContractViolation):
            candidate_sha256({"title": "invalid\x00value"})

    def test_failure_projection_excludes_later_nullable_column(self) -> None:
        access = SourceAccess(
            source_access_id=_FAILED_ACCESS,
            provider="cninfo",
            provider_interface="cninfo:download_pdf",
            dataset_key="disclosure_pdf",
            query_params={"provider_document_id": "pid-1"},
            accessed_at=datetime(2024, 3, 1, tzinfo=timezone.utc),
            status="failed",
            error='{"error_code":"registration_metadata_error"}',
            result_snapshot={"reason": "旧错误未改写"},
        )
        first = failed_access_projection_sha256(access)
        access.recovery_of_source_access_id = None
        self.assertEqual(failed_access_projection_sha256(access), first)
        access.recovery_of_source_access_id = _BINDING_ACCESS
        self.assertEqual(failed_access_projection_sha256(access), first)
        access.result_snapshot = {"reason": "不同旧错误"}
        self.assertNotEqual(failed_access_projection_sha256(access), first)


if __name__ == "__main__":
    unittest.main()
