from datetime import date
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from fastapi.testclient import TestClient

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
from disclosure_anchor.application.use_cases.sync_disclosure_index import (
    CompanyNotTrackedError,
)
from disclosure_anchor.domain.errors import PublishRunError
from disclosure_anchor.main import create_app
from disclosure_anchor.settings import Settings
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
        self.track_result: TrackCompaniesResult | None = None
        self.untrack_codes = None
        self.untracked = True
        self.sync_allowed = True
        self.sync_args = None
        self.sync_error: Exception | None = None
        self.publish_error: Exception | None = None
        self.track_called = False

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
        if self.publish_error is not None:
            raise self.publish_error
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

    def sync_company(
        self, *, security_code, exchange, window_days,
        window_start=None, window_end=None,
    ):
        self.sync_args = (security_code, exchange, window_days)
        self.sync_window_range = (window_start, window_end)
        if self.sync_error is not None:
            raise self.sync_error
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
        self.track_called = True
        if self.track_error is not None:
            raise self.track_error
        if self.track_result is not None:
            return self.track_result
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

    def test_track_companies_echoes_cleared_overrides(self) -> None:
        deps = _Deps()
        deps.track_result = TrackCompaniesResult(
            results=(
                TrackEntryResult(
                    security_code="600519",
                    exchange="SSE",
                    tracked_company_id="tc_1",
                    company_id="co_1",
                    created=False,
                    action="updated",
                    cleared_overrides=("lookback", "process_classes"),
                    status_change="active->paused",
                ),
            ),
            drift=(),
            dry_run=False,
        )

        response = track_companies(
            _request(deps),
            TrackCompaniesRequest(
                entries=[TrackEntryRequest(security_code="600519", exchange="SSE")]
            ),
        )

        entry = response.results[0]
        self.assertEqual(entry.action, "updated")
        self.assertEqual(entry.cleared_overrides, ["lookback", "process_classes"])
        self.assertEqual(entry.status_change, "active->paused")
        self.assertEqual(response.created_count, 0)

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


_ADMIN_TOKEN = "test-admin-token"
_AUTH_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def _admin_app(deps: _Deps):
    root = Path(tempfile.gettempdir()) / "disclosure_admin_api_test"
    settings = Settings(
        disclosure_data_root=root / "data",
        disclosure_shared_root=root / "shared",
        disclosure_runtime_root=root / "runtime",
        mineru_model_cache=root / "mineru",
        hf_home=root / "hf",
        modelscope_cache=root / "modelscope",
        disclosure_enable_admin_api=True,
        disclosure_admin_token=_ADMIN_TOKEN,
    )
    app = create_app(settings, validate_runtime=False)
    app.state.admin_deps = deps
    return app


class FilingApiAdminAppTests(unittest.TestCase):
    """End-to-end (ASGI) admin behaviour: request validation and the
    domain-error -> structured envelope mapping installed on the app."""

    def test_empty_entries_rejected_before_use_case(self) -> None:
        deps = _Deps()
        with TestClient(_admin_app(deps)) as client:
            response = client.put(
                "/v1/admin/tracked-companies",
                json={"entries": []},
                headers=_AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["error_code"], "VALIDATION_ERROR")
        # The empty batch must never reach the reconcile/prune use case.
        self.assertFalse(deps.track_called)

    def test_publish_unknown_run_is_404(self) -> None:
        deps = _Deps()
        deps.publish_error = PublishRunError(
            {
                "stage": "publish",
                "error_code": "RUN_NOT_FOUND",
                "retryable": False,
                "message": "processing run not found: run_x",
            }
        )
        with TestClient(_admin_app(deps)) as client:
            response = client.post(
                "/v1/admin/runs/run_x/publish",
                json={"allow_empty": False},
                headers=_AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["error_code"], "NOT_FOUND")
        self.assertEqual(body["detail"]["error"]["error_code"], "RUN_NOT_FOUND")

    def test_publish_empty_run_is_409(self) -> None:
        deps = _Deps()
        deps.publish_error = PublishRunError(
            {
                "stage": "publish",
                "error_code": "EMPTY_RUN",
                "retryable": False,
                "message": "cannot publish empty unit run without allow_empty",
            }
        )
        with TestClient(_admin_app(deps)) as client:
            response = client.post(
                "/v1/admin/runs/run_1/publish",
                json={"allow_empty": False},
                headers=_AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["error_code"], "CONFLICT")
        self.assertEqual(body["detail"]["error"]["error_code"], "EMPTY_RUN")

    def test_missing_or_wrong_token_is_401(self) -> None:
        deps = _Deps()
        with TestClient(_admin_app(deps)) as client:
            missing = client.put(
                "/v1/admin/tracked-companies",
                json={"entries": [{"security_code": "000001", "exchange": "SZSE"}]},
            )
            wrong = client.put(
                "/v1/admin/tracked-companies",
                json={"entries": [{"security_code": "000001", "exchange": "SZSE"}]},
                headers={"Authorization": "Bearer nope"},
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()["error_code"], "UNAUTHORIZED")
        self.assertEqual(wrong.status_code, 401)
        self.assertFalse(deps.track_called)

    def test_admin_router_refuses_to_mount_without_token(self) -> None:
        # Fail-closed (user decision 2026-07-14): enabling the admin surface
        # without DISCLOSURE_ADMIN_TOKEN must not expose bare endpoints.
        deps = _Deps()
        root = Path(tempfile.gettempdir()) / "disclosure_admin_api_test"
        settings = Settings(
            disclosure_data_root=root / "data",
            disclosure_shared_root=root / "shared",
            disclosure_runtime_root=root / "runtime",
            mineru_model_cache=root / "mineru",
            hf_home=root / "hf",
            modelscope_cache=root / "modelscope",
            disclosure_enable_admin_api=True,
            # Explicit None beats any DISCLOSURE_ADMIN_TOKEN in the ambient
            # env (worker.env is sourced in DB-mode test shells).
            disclosure_admin_token=None,
        )
        app = create_app(settings, validate_runtime=False)
        app.state.admin_deps = deps
        with TestClient(app) as client:
            response = client.put(
                "/v1/admin/tracked-companies",
                json={"entries": [{"security_code": "000001", "exchange": "SZSE"}]},
                headers=_AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 404)

    def test_sync_untracked_company_is_404(self) -> None:
        deps = _Deps()
        deps.sync_error = CompanyNotTrackedError("300012", "SZSE")
        with TestClient(_admin_app(deps)) as client:
            response = client.post(
                "/v1/admin/tracked-companies/300012/sync",
                params={"exchange": "SZSE"},
                json={},
                headers=_AUTH_HEADERS,
            )

        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["error_code"], "NOT_FOUND")
        self.assertIn("not in the tracked pool", body["message"])


if __name__ == "__main__":
    unittest.main()
