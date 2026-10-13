"""B1 receipt and retained-registration checks in the managed scratch DB only."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.application.contracts.historical_security_registration import (
    failed_access_projection_sha256,
    HistoricalSecurityBindingPlanV1,
    load_binding,
    load_request,
    RetainedRegistrationPlanV1,
)
from disclosure_anchor.application.use_cases.historical_security_binding import (
    BindingEvidence,
    HistoricalSecurityBinding,
)
from disclosure_anchor.application.use_cases.recover_archived_registration import (
    RetainedRegistrationExecutor,
    RetainedRegistrationPreview,
    RetainedRegistrationReconciler,
)
from disclosure_anchor.application.worker import queries
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from tests.integration._support import engine_or_skip
from tests.unit.test_historical_security_contract import _binding_document, _json_bytes
from tests.unit.test_raw_document_store import _settings


_CODE_IDENTITY = "sha256:" + "d" * 64
_INDEX_TIME = datetime(2024, 2, 1, tzinfo=timezone.utc)
_FAILED_TIME = datetime(2024, 4, 1, tzinfo=timezone.utc)
_NOW = datetime(2025, 9, 25, tzinfo=timezone.utc)


def _execute_in_process(
    database_url: str, root: str, plan_document: dict[str, object],
    plan_sha256: str, start: object, result_queue: object,
) -> None:
    engine = create_engine(database_url)
    try:
        plan = RetainedRegistrationPlanV1.model_validate(plan_document)
        start.wait(timeout=10)
        store = RawDocumentStore(FileStorePathBuilder(_settings(Path(root))))
        result = RetainedRegistrationExecutor(
            uow_factory=lambda: SqlAlchemyUnitOfWork(engine=engine),
            retained_archive=store, code_identity=_CODE_IDENTITY,
            max_download_retries=3,
        ).execute(plan, plan_sha256=plan_sha256, max_items=1)
        item = result.items[0]
        result_queue.put((item.state, item.receipt_source_access_id, item.document_id, item.error_code))
    except Exception as exc:
        result_queue.put(("error", None, None, type(exc).__name__))
    finally:
        engine.dispose()


class HistoricalSecurityScratchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.tmpdir = tempfile.TemporaryDirectory(prefix="b1-itest-")
        self.root = Path(self.tmpdir.name)
        self.store = RawDocumentStore(FileStorePathBuilder(_settings(self.root)))
        self.company_id: str | None = None
        self.security_ids: list[str] = []
        self.access_ids: list[str] = []
        self.provider_document_id: str | None = None
        self.provider_document_ids: list[str] = []
        self.addCleanup(self._cleanup)

    def _uow(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(engine=self.engine)

    def _cleanup(self) -> None:
        try:
            with self.engine.begin() as conn:
                for provider_document_id in self.provider_document_ids:
                    document_ids = conn.execute(text(
                        "SELECT document_id FROM disclosure_core.document "
                        "WHERE provider='cninfo' AND provider_document_id=:pid"
                    ), {"pid": provider_document_id}).scalars().all()
                    for document_id in document_ids:
                        conn.execute(text(
                            "DELETE FROM disclosure_ops.outbox_event WHERE document_id=:id"
                        ), {"id": document_id})
                        conn.execute(text(
                            "DELETE FROM disclosure_core.document WHERE document_id=:id"
                        ), {"id": document_id})
                if self.company_id is not None:
                    conn.execute(text(
                        "DELETE FROM disclosure_core.company_identifier WHERE company_id=:id"
                    ), {"id": self.company_id})
                    conn.execute(text(
                        "DELETE FROM disclosure_core.tracked_company WHERE company_id=:id"
                    ), {"id": self.company_id})
                for access_id in self.access_ids:
                    conn.execute(text(
                        "DELETE FROM disclosure_core.source_access "
                        "WHERE recovery_of_source_access_id=:id"
                    ), {"id": access_id})
                for access_id in reversed(self.access_ids):
                    conn.execute(text(
                        "DELETE FROM disclosure_core.source_access WHERE source_access_id=:id"
                    ), {"id": access_id})
                if self.company_id is not None:
                    for security_id in reversed(self.security_ids):
                        conn.execute(text(
                            "DELETE FROM disclosure_core.security WHERE security_id=:id"
                        ), {"id": security_id})
                    conn.execute(text(
                        "DELETE FROM disclosure_core.company WHERE company_id=:id"
                    ), {"id": self.company_id})
        finally:
            self.engine.dispose()
            self.tmpdir.cleanup()

    def _seed_unbound_source_chain(
        self,
    ) -> tuple[
        str, str, HistoricalSecurityBinding, HistoricalSecurityBindingPlanV1,
        BindingEvidence, bytes,
    ]:
        self.company_id = str(ids.new_company_id())
        current_id = str(ids.new_security_id())
        self.security_ids.append(current_id)
        profile_id = str(ids.new_source_access_id())
        index_id = str(ids.new_source_access_id())
        failed_id = str(ids.new_source_access_id())
        self.access_ids.extend((profile_id, index_id, failed_id))
        uscc_id = str(ids.new_company_identifier_id())
        org_id = str(ids.new_company_identifier_id())
        uscc = "91320000" + ids.new_ulid()[:10]
        self.provider_document_id = "b1-" + ids.new_ulid().lower()
        self.provider_document_ids.append(self.provider_document_id)
        payload = b"%PDF-1.4\nretained-archive-test\n%%EOF\n"
        original = self.root / "original.pdf"
        original.write_bytes(payload)
        self.store.put_raw_document(
            provider="cninfo", security_code="300114", year=2024,
            provider_document_id=self.provider_document_id, input_file=original,
        )
        candidate = {
            "provider_document_id": self.provider_document_id,
            "security_code": "300114", "exchange": "SZSE",
            "announcement_date": "2024-03-01",
            "title": "中文原始标题", "download_url": "https://example.org/original.pdf",
            "raw_category": "", "report_period": "",
            "provider_org_id": "profile-org-context",
            "file_signature_hint": {"file_size": len(payload)},
        }
        with self._uow() as uow:
            uow.companies.add(e.Company(
                company_id=self.company_id, legal_name="上市主体 " + self.provider_document_id,
                unified_social_credit_code=uscc,
            ))
            uow.securities.add(e.Security(
                security_id=current_id, company_id=self.company_id,
                security_code="302132", exchange="SZSE", status="active",
            ))
            uow.tracked_companies.add(e.TrackedCompany(
                tracked_company_id=str(ids.new_tracked_company_id()),
                company_id=self.company_id, security_id=current_id,
            ))
            uow.source_accesses.add(e.SourceAccess(
                source_access_id=profile_id, provider="cninfo",
                provider_interface="cninfo:p_stock2100",
                accessed_at=_INDEX_TIME, status="ok",
                result_snapshot={"profile": {"uscc": uscc, "security_code": "302132"}},
            ))
            uow.company_identifiers.add(e.CompanyIdentifier(
                identifier_id=uscc_id, company_id=self.company_id, scheme="uscc",
                raw_value=uscc, normalized_value=uscc, observed_at=_INDEX_TIME,
                source_access_id=profile_id,
            ))
            uow.company_identifiers.add(e.CompanyIdentifier(
                identifier_id=org_id, company_id=self.company_id, scheme="cninfo_org_id",
                raw_value="profile-org-context", normalized_value="profile-org-context",
                observed_at=_INDEX_TIME, source_access_id=profile_id,
            ))
            uow.source_accesses.add(e.SourceAccess(
                source_access_id=index_id, provider="cninfo",
                provider_interface="cninfo:p_info3015", accessed_at=_INDEX_TIME,
                status="ok", result_hash="sha256:" + "e" * 64,
                company_id=self.company_id, security_id=current_id,
                result_snapshot={"candidates": [candidate]},
            ))
            uow.source_accesses.add(e.SourceAccess(
                source_access_id=failed_id, provider="cninfo",
                provider_interface="cninfo:download_pdf",
                accessed_at=_FAILED_TIME, status="failed",
                query_params={
                    "provider_document_id": self.provider_document_id,
                    "index_source_access_id": index_id,
                },
                error=json.dumps({
                    "stage": "download", "failure_phase": "registration",
                    "error_code": "registration_metadata_error",
                    "retryable": False,
                    "provider_document_id": self.provider_document_id,
                }),
                result_snapshot={"reason": "historical code not yet bound"},
            ))
            uow.commit()
        binding_doc = _binding_document()
        binding_doc["target_company_id"] = self.company_id
        binding_doc["current_security_id"] = current_id
        binding_doc["approved_query"] = {
            "company_id": self.company_id, "security_id": current_id,
        }
        binding_doc["expected_uscc"] = {
            "identifier_id": uscc_id, "value": uscc,
            "profile_source_access_id": profile_id,
        }
        binding_doc["query_org_observation"] = {
            "value": "profile-org-context", "provenance": "profile_context",
            "source_access_id": profile_id,
        }
        evidence = b"%PDF-1.4\nofficial-evidence\n%%EOF\n"
        evidence_hash = "sha256:" + hashlib.sha256(evidence).hexdigest()
        binding_doc["official_evidence"] = {
            **binding_doc["official_evidence"],
            "sha256": evidence_hash,
            "byte_count": len(evidence),
        }
        binding = load_binding(_json_bytes(binding_doc))
        binding_case = HistoricalSecurityBinding(
            uow_factory=self._uow, code_identity=_CODE_IDENTITY, clock=lambda: _NOW,
        )
        measured = BindingEvidence(sha256=evidence_hash, byte_count=len(evidence))
        binding_plan = binding_case.preview(binding, evidence=measured)
        return failed_id, index_id, binding_case, binding_plan, measured, payload

    def _seed_source_chain(self) -> tuple[str, str, str, bytes]:
        failed_id, index_id, binding_case, binding_plan, measured, payload = (
            self._seed_unbound_source_chain()
        )
        bound = binding_case.execute(
            binding_plan, evidence=measured, decided_by="fixture reviewer",
        )
        self.security_ids.append(bound.historical_security_id)
        self.access_ids.append(bound.binding_source_access_id)
        return failed_id, index_id, bound.binding_source_access_id, payload

    def _append_second_failure(self, first_index_id: str) -> tuple[str, str, bytes, Path]:
        second_pid = "b1-" + ids.new_ulid().lower()
        self.provider_document_ids.append(second_pid)
        second_index_id = str(ids.new_source_access_id())
        second_failed_id = str(ids.new_source_access_id())
        self.access_ids.extend((second_index_id, second_failed_id))
        payload = b"%PDF-1.4\nsecond-retained-original\n%%EOF\n"
        source = self.root / "second.pdf"
        source.write_bytes(payload)
        archived = self.store.put_raw_document(
            provider="cninfo", security_code="300114", year=2024,
            provider_document_id=second_pid, input_file=source,
        )
        with self._uow() as uow:
            first_index = uow.source_accesses.get(first_index_id)
            candidate = dict(first_index.result_snapshot["candidates"][0])
            candidate.update({
                "provider_document_id": second_pid,
                "title": "第二份保留原件",
                "file_signature_hint": {"file_size": len(payload)},
            })
            uow.source_accesses.add(e.SourceAccess(
                source_access_id=second_index_id, provider="cninfo",
                provider_interface="cninfo:p_info3015",
                accessed_at=_INDEX_TIME, status="ok",
                result_hash="sha256:" + "f" * 64,
                company_id=self.company_id,
                security_id=first_index.security_id,
                result_snapshot={"candidates": [candidate]},
            ))
            uow.source_accesses.add(e.SourceAccess(
                source_access_id=second_failed_id, provider="cninfo",
                provider_interface="cninfo:download_pdf",
                accessed_at=_FAILED_TIME, status="failed",
                query_params={
                    "provider_document_id": second_pid,
                    "index_source_access_id": second_index_id,
                },
                error=json.dumps({
                    "stage": "download", "failure_phase": "registration",
                    "error_code": "registration_metadata_error",
                    "retryable": False, "provider_document_id": second_pid,
                }),
                result_snapshot={"reason": "historical code not yet bound"},
            ))
            uow.commit()
        archive_path = _settings(self.root).disclosure_data_root / "data" / archived.relpath
        return second_failed_id, second_index_id, payload, archive_path

    def test_binding_security_and_evidence_roll_back_together_before_commit(self) -> None:
        failed_id, _, binding_case, plan, evidence, _ = (
            self._seed_unbound_source_chain()
        )
        with self._uow() as uow:
            failure_before = failed_access_projection_sha256(
                uow.source_accesses.get(failed_id)
            )
        staged_both = [False]
        company_id = self.company_id

        class FlushThenFailBindingUow(SqlAlchemyUnitOfWork):
            def commit(self) -> None:
                self.session.flush()
                security_count = self.session.execute(text(
                    "SELECT count(*) FROM disclosure_core.security "
                    "WHERE company_id=:company AND security_code='300114'"
                ), {"company": company_id}).scalar_one()
                binding_count = self.session.execute(text(
                    "SELECT count(*) FROM disclosure_core.source_access "
                    "WHERE company_id=:company "
                    "AND provider_interface='local:historical_security_binding.v1'"
                ), {"company": company_id}).scalar_one()
                staged_both[0] = security_count == 1 and binding_count == 1
                raise RuntimeError("forced binding precommit failure")

        failing_binding = HistoricalSecurityBinding(
            uow_factory=lambda: FlushThenFailBindingUow(engine=self.engine),
            code_identity=_CODE_IDENTITY, clock=lambda: _NOW,
        )
        with self.assertRaisesRegex(RuntimeError, "forced binding precommit failure"):
            failing_binding.execute(
                plan, evidence=evidence, decided_by="fixture reviewer",
            )
        self.assertTrue(staged_both[0], "both rows were actually flushed before rollback")
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_core.security "
                "WHERE company_id=:company AND security_code='300114'"
            ), {"company": company_id}).scalar_one(), 0)
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_core.source_access "
                "WHERE company_id=:company "
                "AND provider_interface='local:historical_security_binding.v1'"
            ), {"company": company_id}).scalar_one(), 0)
        with self._uow() as uow:
            self.assertEqual(
                failed_access_projection_sha256(uow.source_accesses.get(failed_id)),
                failure_before,
            )
        clean = binding_case.execute(
            plan, evidence=evidence, decided_by="fixture reviewer",
        )
        self.assertTrue(clean.created_historical_security)
        self.assertTrue(clean.recorded)
        self.security_ids.append(clean.historical_security_id)
        self.access_ids.append(clean.binding_source_access_id)
        repeated = binding_case.execute(
            plan, evidence=evidence, decided_by="fixture reviewer",
        )
        self.assertFalse(repeated.recorded)
        self.assertEqual(repeated.binding_source_access_id, clean.binding_source_access_id)

    def test_registration_document_receipt_and_event_roll_back_together(self) -> None:
        failed_id, index_id, binding_id, payload = self._seed_source_chain()
        with self._uow() as uow:
            failure_before = failed_access_projection_sha256(
                uow.source_accesses.get(failed_id)
            )
        with self.engine.connect() as conn:
            outbox_before = conn.execute(text(
                "SELECT count(*) FROM disclosure_ops.outbox_event"
            )).scalar_one()
        request, request_sha = load_request(_json_bytes({
            "schema": "retained-registration-request.v1",
            "items": [{
                "failed_source_access_id": failed_id,
                "index_source_access_id": index_id,
                "expected_raw_file_hash": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "expected_byte_count": len(payload),
            }],
        }))
        preview = RetainedRegistrationPreview(
            uow_factory=self._uow, retained_archive=self.store,
            code_identity=_CODE_IDENTITY, max_download_retries=3,
        ).preview(
            request, request_sha256=request_sha,
            binding_source_access_id=binding_id, max_items=1,
        )
        self.assertEqual(preview.refusals, ())
        plan = preview.plan
        self.assertIsNotNone(plan)
        staged_triple = [False]

        class FlushThenFailRegistrationUow(SqlAlchemyUnitOfWork):
            def commit(self) -> None:
                self.session.flush()
                receipt_count = self.session.execute(text(
                    "SELECT count(*) FROM disclosure_core.source_access "
                    "WHERE recovery_of_source_access_id=:failed"
                ), {"failed": failed_id}).scalar_one()
                if receipt_count:
                    document_count = self.session.execute(text(
                        "SELECT count(*) FROM disclosure_core.document "
                        "WHERE provider='cninfo' AND provider_document_id=:pid"
                    ), {"pid": plan.items[0].provider_document_id}).scalar_one()
                    event_count = self.session.execute(text(
                        "SELECT count(*) FROM disclosure_ops.outbox_event"
                    )).scalar_one()
                    staged_triple[0] = (
                        receipt_count == 1 and document_count == 1
                        and event_count == outbox_before + 1
                    )
                    raise RuntimeError("forced registration precommit failure")
                super().commit()

        interrupted = RetainedRegistrationExecutor(
            uow_factory=lambda: FlushThenFailRegistrationUow(engine=self.engine),
            retained_archive=self.store, code_identity=_CODE_IDENTITY,
            max_download_retries=3,
        ).execute(plan, plan_sha256=plan.sha256(), max_items=1)
        self.assertTrue(staged_triple[0], "document, receipt and event were flushed")
        self.assertEqual(interrupted.items[0].state, "stopped")
        self.assertEqual(interrupted.items[0].error_code, "COMMIT_NOT_APPLIED")
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_core.document "
                "WHERE provider='cninfo' AND provider_document_id=:pid"
            ), {"pid": self.provider_document_id}).scalar_one(), 0)
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_core.source_access "
                "WHERE recovery_of_source_access_id=:failed"
            ), {"failed": failed_id}).scalar_one(), 0)
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_ops.outbox_event"
            )).scalar_one(), outbox_before)
        with self._uow() as uow:
            self.assertEqual(
                failed_access_projection_sha256(uow.source_accesses.get(failed_id)),
                failure_before,
            )
        self.assertFalse(self.store.verify_retained_raw_document(
            relpath=Path(plan.items[0].raw_file_relpath),
            expected_hash=plan.items[0].raw_file_hash,
            expected_byte_count=plan.items[0].byte_count,
        ).created)
        clean = RetainedRegistrationExecutor(
            uow_factory=self._uow, retained_archive=self.store,
            code_identity=_CODE_IDENTITY, max_download_retries=3,
        ).execute(plan, plan_sha256=plan.sha256(), max_items=1)
        self.assertEqual(clean.items[0].state, "committed")
        if clean.items[0].receipt_source_access_id is not None:
            self.access_ids.append(clean.items[0].receipt_source_access_id)
        repeated = RetainedRegistrationExecutor(
            uow_factory=self._uow, retained_archive=self.store,
            code_identity=_CODE_IDENTITY, max_download_retries=3,
        ).execute(plan, plan_sha256=plan.sha256(), max_items=1)
        self.assertEqual(repeated.items[0].state, "already_completed")
        self.assertEqual(repeated.items[0].receipt_source_access_id,
                         clean.items[0].receipt_source_access_id)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_ops.outbox_event"
            )).scalar_one(), outbox_before + 1)

    def test_recovery_commit_readback_repeat_and_future_failure(self) -> None:
        failed_id, index_id, binding_id, payload = self._seed_source_chain()
        with self._uow() as uow:
            before_projection = failed_access_projection_sha256(
                uow.source_accesses.get(failed_id)
            )
        with self.engine.connect() as conn:
            before = queries.download_failure_resolution_summary(conn, max_retries=3)
            fact = conn.execute(text(
                "SELECT resolved_by_source_access_id FROM "
                "disclosure_ops.download_failure_resolution_v1 "
                "WHERE source_access_id=:id"
            ), {"id": failed_id}).scalar_one()
            self.assertIsNone(fact)

        request, request_sha = load_request(_json_bytes({
            "schema": "retained-registration-request.v1",
            "items": [{
                "failed_source_access_id": failed_id,
                "index_source_access_id": index_id,
                "expected_raw_file_hash": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "expected_byte_count": len(payload),
            }],
        }))
        preview = RetainedRegistrationPreview(
            uow_factory=self._uow, retained_archive=self.store,
            code_identity=_CODE_IDENTITY, max_download_retries=3,
        ).preview(
            request, request_sha256=request_sha,
            binding_source_access_id=binding_id, max_items=1,
        )
        self.assertEqual(preview.refusals, ())
        plan = preview.plan
        self.assertIsNotNone(plan)
        self.assertEqual(plan.item_count, 1)

        unknown_commit_raised = [False]

        class LostAcknowledgmentUow(SqlAlchemyUnitOfWork):
            def commit(self) -> None:
                self.session.flush()
                has_receipt = bool(self.session.execute(text(
                    "SELECT EXISTS (SELECT 1 FROM disclosure_core.source_access "
                    "WHERE recovery_of_source_access_id=:failed)"
                ), {"failed": failed_id}).scalar_one())
                super().commit()
                if has_receipt and not unknown_commit_raised[0]:
                    unknown_commit_raised[0] = True
                    raise OSError("test-only lost commit acknowledgment")

        def uncertain_factory() -> SqlAlchemyUnitOfWork:
            return LostAcknowledgmentUow(engine=self.engine)

        uncertain = RetainedRegistrationExecutor(
            uow_factory=uncertain_factory, retained_archive=self.store,
            code_identity=_CODE_IDENTITY, max_download_retries=3,
        ).execute(plan, plan_sha256=plan.sha256(), max_items=1)
        self.assertTrue(unknown_commit_raised[0])
        self.assertEqual(uncertain.items[0].state, "committed")
        self.assertTrue(uncertain.items[0].readback_after_unknown_commit)
        document_id = uncertain.items[0].document_id
        receipt_id = uncertain.items[0].receipt_source_access_id
        self.assertIsNotNone(document_id)
        if receipt_id is not None:
            self.access_ids.append(receipt_id)

        repeated = RetainedRegistrationExecutor(
            uow_factory=self._uow, retained_archive=self.store,
            code_identity=_CODE_IDENTITY, max_download_retries=3,
        ).execute(plan, plan_sha256=plan.sha256(), max_items=1)
        self.assertEqual(repeated.items[0].state, "already_completed")
        self.assertEqual(repeated.items[0].document_id, document_id)
        reconciled = RetainedRegistrationReconciler(
            uow_factory=self._uow,
        ).reconcile(plan, plan_sha256=plan.sha256())
        self.assertEqual(reconciled.items[0].state, "resolved")
        self.assertTrue(reconciled.items[0].failure_history_unchanged)
        with self._uow() as uow:
            self.assertEqual(
                failed_access_projection_sha256(uow.source_accesses.get(failed_id)),
                before_projection,
            )
            document = uow.documents.get(document_id)
            self.assertEqual(document.title, "中文原始标题")
            self.assertEqual(document.report_period, None)
            self.assertEqual(document.provider_metadata["raw_category"], "")
            self.assertEqual(document.raw_file_hash, plan.items[0].raw_file_hash)
            facts = uow.source_accesses.download_failure_resolutions(
                provider_document_id=self.provider_document_id,
            )
            self.assertEqual(facts[0].resolved_by_source_access_id, receipt_id)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_core.document "
                "WHERE provider='cninfo' AND provider_document_id=:pid"
            ), {"pid": self.provider_document_id}).scalar_one(), 1)
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_ops.outbox_event WHERE document_id=:id"
            ), {"id": document_id}).scalar_one(), 1)
            parse_candidates = queries.pending_parse(
                conn, max_retries=3, limit=1, document_ids=(document_id,),
                scope_classes=None, require_active_company_scope=True,
            )
            self.assertEqual(len(parse_candidates), 1)
            self.assertEqual(parse_candidates[0]["document_id"], document_id)
            self.assertEqual(parse_candidates[0]["raw_byte_count"], len(payload))
            self.assertEqual(
                parse_candidates[0]["raw_file_hash"], plan.items[0].raw_file_hash,
            )
            after = queries.download_failure_resolution_summary(conn, max_retries=3)
            self.assertEqual(after["resolved_failures"], before["resolved_failures"] + 1)
            self.assertEqual(after["dead_letter_candidates"], before["dead_letter_candidates"] - 1)

        future_id = str(ids.new_source_access_id())
        self.access_ids.append(future_id)
        with self._uow() as uow:
            uow.source_accesses.add(e.SourceAccess(
                source_access_id=future_id, provider="cninfo",
                provider_interface="cninfo:download_pdf",
                accessed_at=_NOW, status="failed",
                query_params={"provider_document_id": self.provider_document_id},
                error=json.dumps({
                    "stage": "download", "failure_phase": "registration",
                    "error_code": "registration_metadata_error", "retryable": False,
                }),
            ))
            uow.commit()
        with self.engine.connect() as conn:
            future_fact = conn.execute(text(
                "SELECT resolved_by_source_access_id FROM "
                "disclosure_ops.download_failure_resolution_v1 "
                "WHERE source_access_id=:id"
            ), {"id": future_id}).scalar_one()
            self.assertIsNone(future_fact)
            final = queries.download_failure_resolution_summary(conn, max_retries=3)
            self.assertEqual(final["dead_letter_candidates"], before["dead_letter_candidates"])

    def test_recovery_receipt_constraint_rejects_null_interface(self) -> None:
        company_id = str(ids.new_company_id())
        security_id = str(ids.new_security_id())
        failed_id = str(ids.new_source_access_id())
        receipt_id = str(ids.new_source_access_id())
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO disclosure_core.company (company_id,legal_name) "
                "VALUES (:id,'scratch company')"
            ), {"id": company_id})
            conn.execute(text(
                "INSERT INTO disclosure_core.security "
                "(security_id,company_id,security_code,exchange,status) "
                "VALUES (:id,:company,'300114','SZSE','historical')"
            ), {"id": security_id, "company": company_id})
            conn.execute(text(
                "INSERT INTO disclosure_core.source_access "
                "(source_access_id,provider,provider_interface,accessed_at,status) "
                "VALUES (:id,'cninfo','cninfo:download_pdf',:at,'failed')"
            ), {"id": failed_id, "at": _FAILED_TIME})
        try:
            with self.assertRaises(IntegrityError):
                with self.engine.begin() as conn:
                    conn.execute(text(
                        "INSERT INTO disclosure_core.source_access "
                        "(source_access_id,provider,provider_interface,accessed_at,"
                        " status,result_hash,company_id,security_id,"
                        " recovery_of_source_access_id) "
                        "VALUES (:id,'cninfo',NULL,:at,'ok',:hash,:company,:security,:failed)"
                    ), {
                        "id": receipt_id, "at": _NOW,
                        "hash": "sha256:" + "a" * 64,
                        "company": company_id, "security": security_id,
                        "failed": failed_id,
                    })
        finally:
            with self.engine.begin() as conn:
                conn.execute(text(
                    "DELETE FROM disclosure_core.source_access WHERE source_access_id=:id"
                ), {"id": receipt_id})
                conn.execute(text(
                    "DELETE FROM disclosure_core.source_access WHERE source_access_id=:id"
                ), {"id": failed_id})
                conn.execute(text(
                    "DELETE FROM disclosure_core.security WHERE security_id=:id"
                ), {"id": security_id})
                conn.execute(text(
                    "DELETE FROM disclosure_core.company WHERE company_id=:id"
                ), {"id": company_id})

    def test_successful_prefix_then_missing_archive_resumes_without_duplicate_event(self) -> None:
        first_failed, first_index, binding_id, first_payload = self._seed_source_chain()
        second_failed, second_index, second_payload, second_archive = (
            self._append_second_failure(first_index)
        )
        request, request_sha = load_request(_json_bytes({
            "schema": "retained-registration-request.v1",
            "items": [
                {
                    "failed_source_access_id": first_failed,
                    "index_source_access_id": first_index,
                    "expected_raw_file_hash": "sha256:" + hashlib.sha256(first_payload).hexdigest(),
                    "expected_byte_count": len(first_payload),
                },
                {
                    "failed_source_access_id": second_failed,
                    "index_source_access_id": second_index,
                    "expected_raw_file_hash": "sha256:" + hashlib.sha256(second_payload).hexdigest(),
                    "expected_byte_count": len(second_payload),
                },
            ],
        }))
        preview = RetainedRegistrationPreview(
            uow_factory=self._uow, retained_archive=self.store,
            code_identity=_CODE_IDENTITY, max_download_retries=3,
        ).preview(
            request, request_sha256=request_sha,
            binding_source_access_id=binding_id, max_items=2,
        )
        self.assertEqual(preview.refusals, ())
        plan = preview.plan
        self.assertIsNotNone(plan)
        hidden = self.root / "temporarily-hidden-second.pdf"
        second_archive.rename(hidden)
        executor = RetainedRegistrationExecutor(
            uow_factory=self._uow, retained_archive=self.store,
            code_identity=_CODE_IDENTITY, max_download_retries=3,
        )
        try:
            prefix = executor.execute(plan, plan_sha256=plan.sha256(), max_items=2)
            self.assertEqual([item.state for item in prefix.items], ["committed", "stopped"])
            self.assertEqual(prefix.items[1].error_code, "RAW_ARCHIVE_NOT_VERIFIED")
            if prefix.items[0].receipt_source_access_id is not None:
                self.access_ids.append(prefix.items[0].receipt_source_access_id)
        finally:
            hidden.rename(second_archive)
        completed = executor.execute(plan, plan_sha256=plan.sha256(), max_items=2)
        self.assertEqual(
            [item.state for item in completed.items], ["already_completed", "committed"],
        )
        self.assertEqual(
            completed.items[0].receipt_source_access_id,
            prefix.items[0].receipt_source_access_id,
        )
        if completed.items[1].receipt_source_access_id is not None:
            self.access_ids.append(completed.items[1].receipt_source_access_id)
        reconciled = RetainedRegistrationReconciler(
            uow_factory=self._uow,
        ).reconcile(plan, plan_sha256=plan.sha256())
        self.assertEqual([item.state for item in reconciled.items], ["resolved", "resolved"])
        with self.engine.connect() as conn:
            for provider_document_id in self.provider_document_ids:
                count = conn.execute(text(
                    "SELECT count(*) FROM disclosure_core.document "
                    "WHERE provider='cninfo' AND provider_document_id=:pid"
                ), {"pid": provider_document_id}).scalar_one()
                self.assertEqual(count, 1)
            for document_id in (
                completed.items[0].document_id, completed.items[1].document_id,
            ):
                self.assertEqual(conn.execute(text(
                    "SELECT count(*) FROM disclosure_ops.outbox_event WHERE document_id=:id"
                ), {"id": document_id}).scalar_one(), 1)

    def test_two_processes_resolve_one_failure_once(self) -> None:
        failed_id, index_id, binding_id, payload = self._seed_source_chain()
        request, request_sha = load_request(_json_bytes({
            "schema": "retained-registration-request.v1",
            "items": [{
                "failed_source_access_id": failed_id,
                "index_source_access_id": index_id,
                "expected_raw_file_hash": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "expected_byte_count": len(payload),
            }],
        }))
        preview = RetainedRegistrationPreview(
            uow_factory=self._uow, retained_archive=self.store,
            code_identity=_CODE_IDENTITY, max_download_retries=3,
        ).preview(
            request, request_sha256=request_sha,
            binding_source_access_id=binding_id, max_items=1,
        )
        self.assertEqual(preview.refusals, ())
        plan = preview.plan
        self.assertIsNotNone(plan)
        database_url = os.environ["DISCLOSURE_TEST_DATABASE_URL"]
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        outcomes = context.Queue()
        processes = [
            context.Process(
                target=_execute_in_process,
                args=(
                    database_url, str(self.root), plan.to_document(),
                    plan.sha256(), start, outcomes,
                ),
            )
            for _ in range(2)
        ]
        try:
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(timeout=20)
                self.assertFalse(process.is_alive(), "concurrent recovery exceeded bounded wait")
                self.assertEqual(process.exitcode, 0)
            results = [outcomes.get(timeout=2) for _ in processes]
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
            outcomes.close()
            outcomes.join_thread()
        self.assertEqual(
            sorted(item[0] for item in results), ["already_completed", "committed"],
            results,
        )
        self.assertEqual(len({item[1] for item in results}), 1)
        self.assertEqual(len({item[2] for item in results}), 1)
        receipt_id = results[0][1]
        document_id = results[0][2]
        if receipt_id is not None:
            self.access_ids.append(receipt_id)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_core.source_access "
                "WHERE recovery_of_source_access_id=:id"
            ), {"id": failed_id}).scalar_one(), 1)
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_ops.outbox_event WHERE document_id=:id"
            ), {"id": document_id}).scalar_one(), 1)

    def test_ordinary_registration_wins_before_replay_without_false_supersession(self) -> None:
        failed_id, index_id, binding_id, payload = self._seed_source_chain()
        request, request_sha = load_request(_json_bytes({
            "schema": "retained-registration-request.v1",
            "items": [{
                "failed_source_access_id": failed_id,
                "index_source_access_id": index_id,
                "expected_raw_file_hash": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "expected_byte_count": len(payload),
            }],
        }))
        preview = RetainedRegistrationPreview(
            uow_factory=self._uow, retained_archive=self.store,
            code_identity=_CODE_IDENTITY, max_download_retries=3,
        ).preview(
            request, request_sha256=request_sha,
            binding_source_access_id=binding_id, max_items=1,
        )
        self.assertEqual(preview.refusals, ())
        plan = preview.plan
        self.assertIsNotNone(plan)
        ordinary_document_id = str(ids.new_document_id())
        newer_hash = "sha256:" + "b" * 64
        with self._uow() as uow:
            uow.documents.add(e.Document(
                document_id=ordinary_document_id, status="registered",
                title="ordinary registrar's newer bytes",
                company_id=self.company_id,
                security_id=plan.items[0].target_security_id,
                provider="cninfo", provider_document_id=self.provider_document_id,
                raw_file_hash=newer_hash,
                raw_file_relpath="raw_documents/cninfo/300114/2024/"
                + self.provider_document_id + "/sha256_" + "b" * 64 + ".pdf",
            ))
            uow.commit()
        result = RetainedRegistrationExecutor(
            uow_factory=self._uow, retained_archive=self.store,
            code_identity=_CODE_IDENTITY, max_download_retries=3,
        ).execute(plan, plan_sha256=plan.sha256(), max_items=1)
        self.assertEqual(result.items[0].state, "stopped")
        self.assertEqual(result.items[0].error_code, "NEWER_VERSION_EXISTS")
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT document_id,raw_file_hash,supersedes_document_id "
                "FROM disclosure_core.document "
                "WHERE provider='cninfo' AND provider_document_id=:pid"
            ), {"pid": self.provider_document_id}).all()
            self.assertEqual(rows, [(ordinary_document_id, newer_hash, None)])
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM disclosure_core.source_access "
                "WHERE recovery_of_source_access_id=:id"
            ), {"id": failed_id}).scalar_one(), 0)


if __name__ == "__main__":
    unittest.main()
