"""A recovery receipt must resolve the same provider's failed obligation."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest import TestCase, mock

from disclosure_anchor.application.contracts.historical_security_registration import (
    RetainedRegistrationPlanV1,
    failed_access_projection_sha256,
)
from disclosure_anchor.application.ports.file_store import RawDocumentWriteResult
from disclosure_anchor.application.use_cases.recover_archived_registration import (
    RetainedRegistrationExecutor,
)
from disclosure_anchor.domain import entities as e
from tests.unit._fakes import FakeUnitOfWork
from tests.unit.test_historical_security_contract import (
    _BINDING_ACCESS,
    _COMPANY,
    _FAILED_ACCESS,
    _HASH_A,
    _INDEX_ACCESS,
    _OLD_SECURITY,
    _retained_plan_document,
)


class RetainedReceiptAcceptanceTests(TestCase):
    def _execute_with_receipt(self, provider: str) -> tuple[str, str | None]:
        uow = FakeUnitOfWork()
        uow.securities.add(e.Security(
            security_id=_OLD_SECURITY, company_id=_COMPANY, security_code="300114",
            exchange="SZSE", status="historical",
        ))
        uow.source_accesses.add(e.SourceAccess(
            source_access_id=_BINDING_ACCESS, provider="cninfo",
            provider_interface="local:historical_security_binding.v1",
            accessed_at=datetime(2025, 9, 25, tzinfo=timezone.utc),
            status="ok", result_hash=_HASH_A, security_id=_OLD_SECURITY,
        ))
        plan_document = _retained_plan_document()
        plan_items = plan_document["items"]
        assert isinstance(plan_items, list)
        plan_item = plan_items[0]
        assert isinstance(plan_item, dict)
        provider_document_id = plan_item["provider_document_id"]
        assert isinstance(provider_document_id, str)
        failed = e.SourceAccess(
            source_access_id=_FAILED_ACCESS, provider="cninfo",
            provider_interface="cninfo:download_pdf",
            accessed_at=datetime(2024, 3, 1, tzinfo=timezone.utc),
            status="failed",
            query_params={
                "provider_document_id": provider_document_id,
                "index_source_access_id": _INDEX_ACCESS,
            },
            error=json.dumps({
                "stage": "download", "failure_phase": "registration",
                "error_code": "registration_metadata_error", "retryable": False,
                "provider_document_id": provider_document_id,
            }),
            result_snapshot={"reason": "未登记历史证券"},
        )
        plan_item["failed_access_projection_sha256"] = (
            failed_access_projection_sha256(failed)
        )
        plan = RetainedRegistrationPlanV1.model_validate(plan_document)
        item = plan.items[0]
        uow.source_accesses.add(failed)
        receipt = e.SourceAccess(
            source_access_id="sa_" + "A" * 26, provider=provider,
            provider_interface="local:register_retained_pdf.v1",
            query_params={"provider_document_id": item.provider_document_id},
            accessed_at=datetime(2025, 9, 25, tzinfo=timezone.utc),
            status="ok", result_hash=item.raw_file_hash,
            company_id=item.target_company_id, security_id=item.target_security_id,
            recovery_of_source_access_id=item.failed_source_access_id,
        )
        uow.source_accesses.add(receipt)
        uow.source_accesses.get_for_update = uow.source_accesses.get
        uow.source_accesses.successful_recovery_for = lambda failed_id: (
            receipt if failed_id == _FAILED_ACCESS else None
        )
        uow.documents.add(e.Document(
            document_id="doc_" + "B" * 26, status="registered",
            company_id=item.target_company_id, security_id=item.target_security_id,
            provider="cninfo", provider_document_id=item.provider_document_id,
            raw_file_relpath=item.raw_file_relpath, raw_file_hash=item.raw_file_hash,
        ))
        archive = mock.Mock()
        archive.verify_retained_raw_document.return_value = RawDocumentWriteResult(
            relpath=Path(item.raw_file_relpath), raw_file_hash=item.raw_file_hash,
            byte_count=item.byte_count, created=False,
        )
        executor = RetainedRegistrationExecutor(
            uow_factory=lambda: uow, retained_archive=archive,
            code_identity=plan.code_identity, max_download_retries=3,
        )
        with mock.patch(
            "disclosure_anchor.application.use_cases.recover_archived_registration."
            "shared_corpus_writer",
            return_value=nullcontext(),
        ):
            result = executor.execute(
                plan, plan_sha256=plan.sha256(), max_items=1,
            )
        self.assertEqual(uow.commit_count, 0)
        archive.verify_retained_raw_document.assert_called_once()
        return result.items[0].state, result.items[0].error_code

    def test_same_provider_receipt_is_idempotent_control(self) -> None:
        self.assertEqual(
            self._execute_with_receipt("cninfo"), ("already_completed", None),
        )

    def test_other_provider_receipt_does_not_complete_cninfo_failure(self) -> None:
        state, error_code = self._execute_with_receipt("other-provider")
        self.assertEqual(state, "stopped")
        self.assertEqual(error_code, "RECEIPT_CONFLICT")
