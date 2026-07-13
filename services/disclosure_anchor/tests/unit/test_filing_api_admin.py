from datetime import date
from pathlib import Path
from types import SimpleNamespace
import unittest

from disclosure_anchor.api.errors import FilingApiError
from disclosure_anchor.api.routers.admin import (
    build_document_units,
    parse_document,
    publish_run,
    register_local_pdf,
    sync_company,
    track_companies,
    untrack_company,
)
from disclosure_anchor.api.schemas.admin import (
    ParserOptionsRequest,
    PublishRunRequest,
    PublishRunResponse,
    RegisterLocalPdfRequest,
    SyncCompanyRequest,
    SyncCompanyResponse,
    TrackCompaniesRequest,
    TrackEntryRequest,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.use_cases.build_units import BuildUnitsResult
from disclosure_anchor.application.use_cases.parse_document import ParseDocumentResult
from disclosure_anchor.application.use_cases.register_local_pdf import RegisterLocalPdfResult
from disclosure_anchor.application.use_cases.track_companies import (
    DriftEntry,
    TrackCompaniesResult,
    TrackEntryResult,
    UntrackCompaniesResult,
    UntrackEntryResult,
)


class _Deps:
    def __init__(self) -> None:
        self.register_command = None
        self.parse_document_id: str | None = None
        self.parse_options: ParserOptions | None = None
        self.build_document_id: str | None = None
        self.publish_args: tuple[str, bool, str | None] | None = None
        self.track_command = None
        self.track_error: Exception | None = None
        self.untrack_codes = None
        self.untracked = True
        self.sync_allowed = True
        self.sync_args = None

    def register_local_pdf(self, command):
        self.register_command = command
        return RegisterLocalPdfResult(
            document_id=None,
            raw_file_relpath=None,
            raw_file_hash=None,
            source_access_id="sa_1",
            outbox_event_id=None,
            quarantined_path=Path("/private/tmp/full/leak/bad.pdf"),
            quarantine_reason="invalid_raw_document",
        )

    def parse_document(self, *, document_id: str, options: ParserOptions):
        self.parse_document_id = document_id
        self.parse_options = options
        return ParseDocumentResult(
            processing_run_id="run_1",
            status="succeeded",
            parser_artifact_relpath="parser/run_1",
            normalized_ir_relpath="normalized/run_1.json",
            artifact_hash="sha256:" + "a" * 64,
        )

    def build_units(self, *, document_id: str):
        self.build_document_id = document_id
        return BuildUnitsResult(
            processing_run_id="run_1",
            status="succeeded",
            unit_count=7,
        )

    def publish_run(self, *, processing_run_id: str, allow_empty: bool, reason: str | None):
        self.publish_args = (processing_run_id, allow_empty, reason)
        return PublishRunResponse(
            document_id="doc_1",
            processing_run_id=processing_run_id,
            is_active=True,
        )

    def untrack_companies(self, codes):
        self.untrack_codes = codes
        if not self.untracked:
            return UntrackCompaniesResult(
                removed=(), not_tracked=tuple(f"{c}.{e}" for c, e in codes)
            )
        return UntrackCompaniesResult(
            removed=tuple(
                UntrackEntryResult(
                    security_code=code,
                    exchange=exchange,
                    tracked_company_id="tc_1",
                    company_id="co_1",
                )
                for code, exchange in codes
            ),
            not_tracked=(),
        )

    def document_count(self, company_id):
        return 7

    def resolve_profiles(self, codes):
        self.resolved_codes = codes

    def can_sync(self):
        return self.sync_allowed

    def sync_company(self, *, security_code, exchange, window_days):
        self.sync_args = (security_code, exchange, window_days)
        return SyncCompanyResponse(
            sync_status="ok",
            security_code=security_code,
            exchange=exchange,
            window_start=date(2026, 7, 1),
            window_end=date(2026, 7, 8),
            company_id="co_1",
            candidate_count=5,
            empty=False,
            checkpoint_id="cp_1",
        )

    def track_companies(self, command):
        self.track_command = command
        if self.track_error is not None:
            raise self.track_error
        return TrackCompaniesResult(
            results=tuple(
                TrackEntryResult(
                    security_code=entry.security_code,
                    exchange=entry.exchange,
                    tracked_company_id="tc_1",
                    company_id="co_1",
                    created=True,
                )
                for entry in command.entries
            ),
            drift=(
                DriftEntry(
                    tracked_company_id="tc_2",
                    company_id="co_2",
                    security_code="000002",
                    status="paused",
                    action="paused",
                ),
            )
            if command.reconcile
            else (),
            dry_run=command.dry_run,
        )


def _request(deps: _Deps) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(admin_deps=deps)))


class FilingApiAdminTests(unittest.TestCase):
    def test_register_local_pdf_returns_quarantine_basename_only(self) -> None:
        deps = _Deps()
        response = register_local_pdf(
            _request(deps),
            RegisterLocalPdfRequest(
                file_path=Path("/service/input.pdf"),
                company_legal_name="江海股份",
                security_code="002484",
                exchange="szse",
                filing_type="other",
                title="短公告",
                announcement_date=date(2026, 7, 5),
                provider_document_id="1225376481",
                provider="cninfo",
            ),
        )

        self.assertEqual(response.quarantined_path, "bad.pdf")
        self.assertNotIn("/", response.quarantined_path)
        self.assertEqual(deps.register_command.provider, "cninfo")
        self.assertEqual(deps.register_command.security_code, "002484")
        self.assertEqual(deps.register_command.exchange, "SZSE")

    def test_register_local_pdf_rejects_non_textid_as_422(self) -> None:
        deps = _Deps()
        with self.assertRaises(FilingApiError) as raised:
            register_local_pdf(
                _request(deps),
                RegisterLocalPdfRequest(
                    file_path=Path("/service/input.pdf"),
                    company_legal_name="江海股份",
                    security_code="002484",
                    exchange="SZSE",
                    filing_type="other",
                    title="短公告",
                    announcement_date=date(2026, 7, 5),
                    provider_document_id="年度报告目录",
                    provider="cninfo",
                ),
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIsNone(deps.register_command)

    def test_parse_uses_parser_options_defaults_and_overrides(self) -> None:
        deps = _Deps()

        response = parse_document(
            "doc_1",
            _request(deps),
            ParserOptionsRequest(method="ocr", table=False, timeout_seconds=30),
        )

        self.assertEqual(response.processing_run_id, "run_1")
        self.assertEqual(deps.parse_document_id, "doc_1")
        self.assertEqual(deps.parse_options.method, "ocr")
        self.assertEqual(deps.parse_options.backend, "pipeline")
        self.assertEqual(deps.parse_options.language, "ch")
        self.assertFalse(deps.parse_options.table)
        self.assertEqual(deps.parse_options.timeout_seconds, 30)

    def test_build_units_response_shape(self) -> None:
        deps = _Deps()

        response = build_document_units("doc_1", _request(deps))

        self.assertEqual(deps.build_document_id, "doc_1")
        self.assertEqual(response.processing_run_id, "run_1")
        self.assertEqual(response.unit_build_status, "succeeded")
        self.assertEqual(response.unit_count, 7)

    def test_publish_response_shape(self) -> None:
        deps = _Deps()

        response = publish_run(
            "run_1",
            _request(deps),
            PublishRunRequest(allow_empty=True, reason="manual"),
        )

        self.assertEqual(deps.publish_args, ("run_1", True, "manual"))
        self.assertEqual(response.document_id, "doc_1")
        self.assertEqual(response.processing_run_id, "run_1")
        self.assertTrue(response.is_active)

    def test_track_companies_maps_entries_and_response(self) -> None:
        deps = _Deps()

        response = track_companies(
            _request(deps),
            TrackCompaniesRequest(
                entries=[
                    TrackEntryRequest(
                        security_code="600519",
                        exchange="SSE",
                        lookback_days=30,
                        sync_frequency="daily",
                        process_classes=["annual_report", "dividend"],
                    ),
                    TrackEntryRequest(
                        security_code="000001", exchange="SZSE", status="paused"
                    ),
                ],
                reconcile=True,
                dry_run=True,
            ),
        )

        command = deps.track_command
        self.assertTrue(command.reconcile)
        self.assertFalse(command.prune_drift)
        self.assertTrue(command.dry_run)
        first, second = command.entries
        self.assertEqual(first.lookback_days, 30)
        self.assertEqual(first.process_classes, ("annual_report", "dividend"))
        # Absent optional field maps to None (clear-to-inherit), not empty.
        self.assertIsNone(second.process_classes)
        self.assertEqual(second.status, "paused")
        self.assertEqual(len(response.results), 2)
        self.assertEqual(response.created_count, 2)
        self.assertEqual(response.drift[0].action, "paused")
        self.assertTrue(response.dry_run)

    def test_untrack_company_returns_removal_with_retained_documents(self) -> None:
        deps = _Deps()

        response = untrack_company("600887", _request(deps), "SSE")

        self.assertEqual(deps.untrack_codes, (("600887", "SSE"),))
        self.assertEqual(response.security_code, "600887")
        self.assertEqual(response.documents_retained, 7)

    def test_sync_company_maps_args_and_response(self) -> None:
        deps = _Deps()

        response = sync_company(
            "300012", _request(deps), "SZSE", SyncCompanyRequest(window_days=30)
        )

        self.assertEqual(deps.sync_args, ("300012", "SZSE", 30))
        self.assertEqual(response.sync_status, "ok")
        self.assertEqual(response.candidate_count, 5)

    def test_sync_company_without_credentials_is_422(self) -> None:
        deps = _Deps()
        deps.sync_allowed = False

        with self.assertRaises(FilingApiError) as ctx:
            sync_company("300012", _request(deps), "SZSE", SyncCompanyRequest())

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.error_code, "VALIDATION_ERROR")

    def test_sync_company_negative_window_is_422(self) -> None:
        deps = _Deps()

        with self.assertRaises(FilingApiError) as ctx:
            sync_company(
                "300012", _request(deps), "SZSE", SyncCompanyRequest(window_days=-1)
            )

        self.assertEqual(ctx.exception.status_code, 422)

    def test_untrack_company_not_tracked_is_404(self) -> None:
        deps = _Deps()
        deps.untracked = False

        with self.assertRaises(FilingApiError) as ctx:
            untrack_company("999999", _request(deps), "SSE")

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.error_code, "NOT_FOUND")

    def test_track_companies_value_error_becomes_validation_error(self) -> None:
        deps = _Deps()
        deps.track_error = ValueError("unknown process_classes ['nope']")

        with self.assertRaises(FilingApiError) as ctx:
            track_companies(
                _request(deps),
                TrackCompaniesRequest(
                    entries=[TrackEntryRequest(security_code="600519", exchange="SSE")]
                ),
            )

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.error_code, "VALIDATION_ERROR")


if __name__ == "__main__":
    unittest.main()
