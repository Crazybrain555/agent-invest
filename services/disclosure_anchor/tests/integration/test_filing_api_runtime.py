"""DB-gated Filing API runtime checkpoints for milestone 06."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
import tempfile
import unittest

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from disclosure_anchor.adapters.db.postgres.schema import READER_ROLE
from disclosure_anchor.api.errors import GONE_SUPERSEDED, VALIDATION_ERROR
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.services.unit_hashing import canonical_json, sha256_prefixed
from disclosure_anchor.main import create_app
from disclosure_anchor.settings import Settings
from tests.integration._support import engine_or_skip
from tests.integration.smoke_real_mineru_build_publish import _mineru_bin_or_skip


@dataclass(frozen=True)
class ApiResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


@dataclass(frozen=True)
class MinerUGate:
    engine: Any
    mineru: Path
    sample_pdf: Path


@dataclass(frozen=True)
class AdminSample:
    label: str
    company_legal_name: str
    security_code: str
    exchange: str
    filing_type: str
    title: str
    announcement_date: date
    report_period: str | None = None


ADMIN_SAMPLES = (
    AdminSample(
        label="annual_report",
        company_legal_name="南通江海电容器股份有限公司",
        security_code="002484",
        exchange="szse",
        filing_type="annual_report",
        title="江海股份：2025年年度报告",
        announcement_date=date(2026, 4, 10),
        report_period="2025A",
    ),
    AdminSample(
        label="ir_activity",
        company_legal_name="美的集团股份有限公司",
        security_code="000333",
        exchange="szse",
        filing_type="investor_relations",
        title="美的集团：2025年4月11日投资者关系活动记录表",
        announcement_date=date(2025, 4, 11),
    ),
    AdminSample(
        label="short_announcement",
        company_legal_name="南通江海电容器股份有限公司",
        security_code="002484",
        exchange="szse",
        filing_type="other",
        title="江海股份：关于股票交易异常波动的公告",
        announcement_date=date(2026, 6, 18),
    ),
)


def require_mineru_and_sample(sample: AdminSample) -> MinerUGate:
    engine = engine_or_skip()
    mineru = _mineru_bin_or_skip()
    sample_pdf = _sample_pdf_or_skip(sample.label)
    return MinerUGate(engine=engine, mineru=mineru, sample_pdf=sample_pdf)


def _sample_pdf_or_skip(label: str) -> Path:
    ref = Path("tests/fixtures/phase00") / label / "parser_artifacts_ref.txt"
    try:
        lines = ref.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise unittest.SkipTest(f"sample PDF reference unreadable: {ref}: {exc}") from exc
    source_lines = [line for line in lines if line.startswith("Source PDF:")]
    if not source_lines:
        raise unittest.SkipTest(f"sample PDF reference missing Source PDF line: {ref}")
    sample_pdf = Path(source_lines[0].split(": ", 1)[1])
    if not sample_pdf.is_file():
        raise unittest.SkipTest(f"sample PDF absent: {sample_pdf}")
    return sample_pdf


def _settings(root: Path, *, mineru: Path | None = None) -> Settings:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    return Settings(
        disclosure_enable_admin_api=True,
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
        disclosure_mineru_bin=mineru,
        mineru_model_cache=Path(
            os.environ.get(
                "MINERU_MODEL_CACHE", str(shared_root / "model_cache" / "mineru")
            )
        ),
        hf_home=Path(
            os.environ.get(
                "HF_HOME", str(shared_root / "model_cache" / "huggingface")
            )
        ),
        modelscope_cache=Path(
            os.environ.get(
                "MODELSCOPE_CACHE", str(shared_root / "model_cache" / "modelscope")
            )
        ),
    )


def _create_test_app(settings: Settings, engine: Any) -> Any:
    app = create_app(settings=settings, validate_runtime=False)
    app.state.app_db_engine = engine
    app.state.db_engine = engine
    app.state.reader_db_engine = engine
    return app


def _api_request(
    app: Any,
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> ApiResponse:
    body = b""
    request_headers = [(b"host", b"testserver")]
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        request_headers.append((b"content-type", b"application/json"))
        request_headers.append((b"content-length", str(len(body)).encode("ascii")))
    for key, value in (headers or {}).items():
        request_headers.append((key.lower().encode("ascii"), value.encode("utf-8")))

    messages: list[dict[str, Any]] = []
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if received:
            return {"type": "http.request", "body": b"", "more_body": False}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": urlencode(query or {}, doseq=True).encode("utf-8"),
        "headers": request_headers,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))

    start = next(message for message in messages if message["type"] == "http.response.start")
    chunks = [
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    ]
    response_headers = {
        key.decode("latin1"): value.decode("latin1")
        for key, value in start.get("headers", [])
    }
    return ApiResponse(
        status_code=int(start["status"]),
        headers=response_headers,
        body=b"".join(chunks),
    )


def _unit_texts(unit: dict[str, Any]) -> str:
    payload = unit.get("payload") or {}
    if unit.get("payload_kind") == "mixed":
        return " ".join(
            str(part.get("text") or "") for part in payload.get("parts", [])
        ).strip()
    return str(payload.get("text") or "").strip()


def _unit_table_parts(unit: dict[str, Any]) -> list[dict[str, Any]]:
    payload = unit.get("payload") or {}
    if unit.get("payload_kind") == "table":
        return [payload]
    if unit.get("payload_kind") == "mixed":
        return [p for p in payload.get("parts", []) if p.get("kind") == "table"]
    return []


def _assert_no_leaks(test: unittest.TestCase, settings: Settings, payload: Any) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    for forbidden in (
        str(settings.disclosure_data_root),
        str(settings.disclosure_shared_root),
        "/Users/",
        "Traceback",
    ):
        test.assertNotIn(forbidden, encoded)


class FilingApiRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.tmpdir = tempfile.TemporaryDirectory(prefix="m06-api-runtime-")
        self.settings = _settings(Path(self.tmpdir.name))
        self.app = _create_test_app(self.settings, self.engine)
        self.company_ids: list[str] = []
        self.security_ids: list[str] = []
        self.source_access_ids: list[str] = []
        self.document_ids: list[str] = []
        self.run_ids: list[str] = []
        self.asset_ids: list[str] = []
        self.event_ids: list[str] = []

    def tearDown(self) -> None:
        self._cleanup_rows()
        self.engine.dispose()
        self.tmpdir.cleanup()

    def test_documents_keyset_paginates_null_announcement_date(self) -> None:
        company_id, security_id, source_access_id = self._seed_subject("page")
        docs = [
            self._insert_document(
                company_id=company_id,
                security_id=security_id,
                source_access_id=source_access_id,
                provider_document_id="m06-page-a",
                filing_type="other",
                report_period=None,
                announcement_date=date(2026, 7, 5),
            ),
            self._insert_document(
                company_id=company_id,
                security_id=security_id,
                source_access_id=source_access_id,
                provider_document_id="m06-page-b",
                filing_type="other",
                report_period=None,
                announcement_date=date(2026, 7, 4),
            ),
            self._insert_document(
                company_id=company_id,
                security_id=security_id,
                source_access_id=source_access_id,
                provider_document_id="m06-page-null",
                filing_type="other",
                report_period=None,
                announcement_date=None,
            ),
        ]

        seen: list[str] = []
        payloads: list[dict[str, Any]] = []
        cursor = None
        for _ in range(3):
            query = {"company_ref": company_id, "limit": 1}
            if cursor is not None:
                query["cursor"] = cursor
            response = _api_request(self.app, "GET", "/v1/documents", query=query)
            self.assertEqual(response.status_code, 200, response.body)
            payload = response.json()
            payloads.append(payload)
            self.assertEqual(len(payload["items"]), 1)
            seen.append(payload["items"][0]["document_id"])
            cursor = payload["next_cursor"]
        self.assertEqual(seen, docs)
        self.assertIsNone(cursor)
        for payload in payloads:
            _assert_no_leaks(self, self.settings, payload)

    def test_latest_filings_supersession_and_same_day_tie_break(self) -> None:
        company_id, security_id, source_access_id = self._seed_subject("latest")
        old_doc = self._insert_document(
            company_id=company_id,
            security_id=security_id,
            source_access_id=source_access_id,
            provider_document_id="m06-latest-old",
            filing_type="other",
            report_period=None,
            announcement_date=date(2026, 7, 1),
        )
        superseding_doc = self._insert_document(
            company_id=company_id,
            security_id=security_id,
            source_access_id=source_access_id,
            provider_document_id="m06-latest-new",
            filing_type="other",
            report_period=None,
            announcement_date=date(2026, 7, 2),
            supersedes_document_id=old_doc,
        )
        tie_a = self._insert_document(
            document_id=f"doc_m06tie_{ids.new_ulid().lower()}_a",
            company_id=company_id,
            security_id=security_id,
            source_access_id=source_access_id,
            provider_document_id="m06-tie-a",
            filing_type="annual_report",
            report_period="2025A",
            announcement_date=date(2026, 7, 5),
        )
        tie_z = self._insert_document(
            document_id=tie_a[:-1] + "z",
            company_id=company_id,
            security_id=security_id,
            source_access_id=source_access_id,
            provider_document_id="m06-tie-z",
            filing_type="annual_report",
            report_period="2025A",
            announcement_date=date(2026, 7, 5),
        )

        latest_other = _api_request(
            self.app,
            "GET",
            "/v1/filings/latest",
            query={"company_ref": company_id, "filing_type": "other"},
        ).json()
        self.assertEqual(latest_other["items"][0]["document_id"], superseding_doc)
        self.assertNotEqual(latest_other["items"][0]["document_id"], old_doc)

        latest_tie = _api_request(
            self.app,
            "GET",
            "/v1/filings/latest",
            query={
                "company_ref": company_id,
                "filing_type": "annual_report",
                "report_period": "2025A",
            },
        ).json()
        self.assertEqual(latest_tie["items"][0]["document_id"], tie_z)

        gone = _api_request(
            self.app,
            "GET",
            f"/v1/documents/{old_doc}",
            query={"reject_superseded": "true"},
        )
        self.assertEqual(gone.status_code, 410)
        self.assertEqual(gone.json()["error_code"], GONE_SUPERSEDED)
        self.assertEqual(gone.json()["detail"], {"superseded_by": superseding_doc})
        for payload in (latest_other, latest_tie, gone.json()):
            _assert_no_leaks(self, self.settings, payload)

    def test_changes_reread_is_idempotent_and_cursor_advances(self) -> None:
        first = self._insert_change_event("m06_changes_first")
        second = self._insert_change_event("m06_changes_second")
        after_seq = min(first, second) - 1

        query = {"after_seq": after_seq, "limit": 1}
        first_page = _api_request(self.app, "GET", "/v1/changes", query=query).json()
        reread = _api_request(self.app, "GET", "/v1/changes", query=query).json()
        self.assertEqual(reread, first_page)
        self.assertEqual(first_page["items"][0]["seq"], first)
        self.assertIsNotNone(first_page["next_cursor"])

        second_page = _api_request(
            self.app,
            "GET",
            "/v1/changes",
            query={"cursor": first_page["next_cursor"], "after_seq": 0, "limit": 1},
        ).json()
        self.assertEqual(second_page["items"][0]["seq"], second)
        for payload in (first_page, reread, second_page):
            _assert_no_leaks(self, self.settings, payload)

    def test_units_context_errors_permissions_and_no_leaks(self) -> None:
        seeded = self._seed_unit_document()

        units = _api_request(
            self.app,
            "GET",
            f"/v1/documents/{seeded['document_id']}/units",
            query={"heading_prefix": ["第一节", "风险"], "limit": 10},
        )
        self.assertEqual(units.status_code, 200, units.body)
        unit_payload = units.json()
        self.assertEqual(
            [item["asset_id"] for item in unit_payload["items"]],
            [seeded["prefix_asset_id"]],
        )
        self.assertEqual(unit_payload["warning"], "LATEST_PROCESSING_FAILED")

        active_unit = _api_request(
            self.app,
            "GET",
            f"/v1/units/{seeded['prefix_asset_id']}",
        ).json()
        self.assertEqual(
            active_unit["asset_uri"],
            f"asset://disclosure_anchor/v1/document_unit/{seeded['prefix_asset_id']}",
        )
        self.assertTrue(active_unit["is_active_run"])

        history_unit = _api_request(
            self.app,
            "GET",
            f"/v1/units/{seeded['history_asset_id']}",
        ).json()
        self.assertFalse(history_unit["is_active_run"])

        source_ref = _api_request(
            self.app,
            "GET",
            f"/v1/units/{seeded['prefix_asset_id']}/source-ref",
        ).json()
        self.assertEqual(source_ref["source_access_id"], seeded["source_access_id"])

        context = _api_request(
            self.app,
            "GET",
            f"/v1/units/{seeded['prefix_asset_id']}/context",
            query={"max_chars": 12},
        )
        self.assertEqual(context.status_code, 200, context.body)
        context_payload = context.json()
        expected_excerpt = canonical_json(seeded["prefix_payload"])[:12]
        self.assertEqual(context_payload["excerpt"], expected_excerpt)
        self.assertEqual(context_payload["start"], 0)
        self.assertEqual(context_payload["end"], len(expected_excerpt))
        self.assertEqual(context_payload["excerpt_hash"], sha256_prefixed(expected_excerpt))

        bad_cursor = _api_request(
            self.app,
            "GET",
            "/v1/documents",
            query={"cursor": "not-base64"},
        )
        self.assertEqual(bad_cursor.status_code, 422)
        self.assertEqual(bad_cursor.json()["error_code"], VALIDATION_ERROR)

        with self.engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text(f'SET ROLE "{READER_ROLE}"'))
                with self.assertRaises(ProgrammingError):
                    conn.execute(
                        text(
                            "INSERT INTO disclosure_core.company "
                            "(company_id, legal_name) VALUES ('co_api_denied', 'x')"
                        )
                    )
            finally:
                trans.rollback()

        for payload in (
            unit_payload,
            active_unit,
            history_unit,
            source_ref,
            context_payload,
            bad_cursor.json(),
        ):
            _assert_no_leaks(self, self.settings, payload)

    def _seed_subject(self, label: str) -> tuple[str, str, str]:
        company_id = ids.new_company_id()
        security_id = ids.new_security_id()
        source_access_id = ids.new_source_access_id()
        security_code = f"M06{ids.new_ulid()[-8:]}"
        self.company_ids.append(company_id)
        self.security_ids.append(security_id)
        self.source_access_ids.append(source_access_id)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company "
                    "(company_id, legal_name) VALUES (:company_id, :legal_name)"
                ),
                {"company_id": company_id, "legal_name": f"M06 {label} Co"},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.security "
                    "(security_id, company_id, security_code, exchange) "
                    "VALUES (:security_id, :company_id, :security_code, 'SZSE')"
                ),
                {
                    "security_id": security_id,
                    "company_id": company_id,
                    "security_code": security_code,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.source_access "
                    "(source_access_id, provider, accessed_at, status, company_id, "
                    "security_id) "
                    "VALUES (:source_access_id, 'cninfo', :accessed_at, 'succeeded', "
                    ":company_id, :security_id)"
                ),
                {
                    "source_access_id": source_access_id,
                    "accessed_at": datetime(2026, 7, 5, tzinfo=timezone.utc),
                    "company_id": company_id,
                    "security_id": security_id,
                },
            )
        return company_id, security_id, source_access_id

    def _insert_document(
        self,
        *,
        company_id: str,
        security_id: str,
        source_access_id: str,
        provider_document_id: str,
        filing_type: str,
        report_period: str | None,
        announcement_date: date | None,
        document_id: str | None = None,
        current_processing_run_id: str | None = None,
        supersedes_document_id: str | None = None,
    ) -> str:
        document_id = document_id or ids.new_document_id()
        self.document_ids.append(document_id)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.document "
                    "(document_id, status, company_id, security_id, source_access_id, "
                    "provider, provider_document_id, filing_type, report_period, "
                    "title, announcement_date, raw_file_hash, raw_file_relpath, "
                    "current_processing_run_id, supersedes_document_id) "
                    "VALUES (:document_id, 'published', :company_id, :security_id, "
                    ":source_access_id, 'cninfo', :provider_document_id, "
                    ":filing_type, :report_period, :title, :announcement_date, "
                    ":raw_file_hash, :raw_file_relpath, :current_processing_run_id, "
                    ":supersedes_document_id)"
                ),
                {
                    "document_id": document_id,
                    "company_id": company_id,
                    "security_id": security_id,
                    "source_access_id": source_access_id,
                    "provider_document_id": provider_document_id,
                    "filing_type": filing_type,
                    "report_period": report_period,
                    "title": f"Milestone 06 {provider_document_id}",
                    "announcement_date": announcement_date,
                    "raw_file_hash": f"sha256:{ids.new_ulid().lower()}",
                    "raw_file_relpath": f"raw_documents/cninfo/{document_id}.pdf",
                    "current_processing_run_id": current_processing_run_id,
                    "supersedes_document_id": supersedes_document_id,
                },
            )
        return document_id

    def _insert_run(
        self,
        *,
        document_id: str,
        run_id: str,
        is_active: bool,
        status: str,
        unit_build_status: str,
        started_at: datetime,
    ) -> None:
        self.run_ids.append(run_id)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.processing_run "
                    "(processing_run_id, document_id, run_kind, status, is_active, "
                    "unit_build_status, unit_build_attempt_count, started_at, "
                    "finished_at, builder_rules_version) "
                    "VALUES (:run_id, :document_id, 'parse', :status, :is_active, "
                    ":unit_build_status, 1, :started_at, :started_at, 'm06')"
                ),
                {
                    "run_id": run_id,
                    "document_id": document_id,
                    "status": status,
                    "is_active": is_active,
                    "unit_build_status": unit_build_status,
                    "started_at": started_at,
                },
            )

    def _insert_unit(
        self,
        *,
        asset_id: str,
        document_id: str,
        run_id: str,
        provider_document_id: str,
        order_index: int,
        heading_path: list[str],
        payload: dict[str, Any],
        title: str,
    ) -> None:
        self.asset_ids.append(asset_id)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.document_unit "
                    "(asset_id, document_id, processing_run_id, provider_document_id, "
                    "payload_kind, heading_path, title, order_index, semantic_key, "
                    "payload, content_hash, query_projection_hash) "
                    "VALUES (:asset_id, :document_id, :run_id, :provider_document_id, "
                    "'text', CAST(:heading_path AS jsonb), :title, :order_index, "
                    "'risk_factor', CAST(:payload AS jsonb), :content_hash, "
                    ":query_projection_hash)"
                ),
                {
                    "asset_id": asset_id,
                    "document_id": document_id,
                    "run_id": run_id,
                    "provider_document_id": provider_document_id,
                    "heading_path": json.dumps(heading_path, ensure_ascii=False),
                    "title": title,
                    "order_index": order_index,
                    "payload": json.dumps(payload, ensure_ascii=False),
                    "content_hash": f"sha256:{asset_id}",
                    "query_projection_hash": f"sha256:qp-{asset_id}",
                },
            )

    def _seed_unit_document(self) -> dict[str, Any]:
        company_id, security_id, source_access_id = self._seed_subject("units")
        active_run_id = ids.new_processing_run_id()
        failed_run_id = ids.new_processing_run_id()
        history_run_id = ids.new_processing_run_id()
        provider_document_id = "m06-units"
        document_id = self._insert_document(
            company_id=company_id,
            security_id=security_id,
            source_access_id=source_access_id,
            provider_document_id=provider_document_id,
            filing_type="other",
            report_period=None,
            announcement_date=date(2026, 7, 5),
            current_processing_run_id=active_run_id,
        )
        base_time = datetime(2026, 7, 5, tzinfo=timezone.utc)
        self._insert_run(
            document_id=document_id,
            run_id=history_run_id,
            is_active=False,
            status="succeeded",
            unit_build_status="succeeded",
            started_at=base_time - timedelta(hours=1),
        )
        self._insert_run(
            document_id=document_id,
            run_id=active_run_id,
            is_active=True,
            status="succeeded",
            unit_build_status="succeeded",
            started_at=base_time,
        )
        self._insert_run(
            document_id=document_id,
            run_id=failed_run_id,
            is_active=False,
            status="failed",
            unit_build_status="failed",
            started_at=base_time + timedelta(hours=1),
        )
        prefix_asset_id = ids.new_asset_id()
        counter_asset_id = ids.new_asset_id()
        history_asset_id = ids.new_asset_id()
        prefix_payload = {"text": "风险提示正文", "page": 1}
        self._insert_unit(
            asset_id=prefix_asset_id,
            document_id=document_id,
            run_id=active_run_id,
            provider_document_id=provider_document_id,
            order_index=1,
            heading_path=["第一节", "风险", "详情"],
            payload=prefix_payload,
            title="风险提示",
        )
        self._insert_unit(
            asset_id=counter_asset_id,
            document_id=document_id,
            run_id=active_run_id,
            provider_document_id=provider_document_id,
            order_index=2,
            heading_path=["风险", "第一节"],
            payload={"text": "containment counterexample"},
            title="误匹配样本",
        )
        self._insert_unit(
            asset_id=history_asset_id,
            document_id=document_id,
            run_id=history_run_id,
            provider_document_id=provider_document_id,
            order_index=1,
            heading_path=["历史"],
            payload={"text": "historical"},
            title="历史版本",
        )
        return {
            "document_id": document_id,
            "source_access_id": source_access_id,
            "prefix_asset_id": prefix_asset_id,
            "history_asset_id": history_asset_id,
            "prefix_payload": prefix_payload,
        }

    def _insert_change_event(self, event_kind: str) -> int:
        event_id = ids.new_outbox_event_id()
        self.event_ids.append(event_id)
        with self.engine.begin() as conn:
            seq = conn.execute(
                text(
                    "INSERT INTO disclosure_ops.outbox_event "
                    "(event_id, event_kind, change_kind, subject_kind, subject_ref, "
                    "payload) "
                    "VALUES (:event_id, :event_kind, 'observed', 'source_access', "
                    ":subject_ref, CAST(:payload AS jsonb)) "
                    "RETURNING seq"
                ),
                {
                    "event_id": event_id,
                    "event_kind": event_kind,
                    "subject_ref": event_id,
                    "payload": json.dumps({"event": event_kind}),
                },
            ).scalar_one()
        return int(seq)

    def _cleanup_rows(self) -> None:
        with self.engine.begin() as conn:
            if self.document_ids:
                # Register-path documents create company/security rows through
                # SubjectResolver; harvest them before deleting the documents
                # or they leak into the shared corpus (observed 2026-07-06).
                for row in conn.execute(
                    text(
                        "SELECT DISTINCT company_id, security_id "
                        "FROM disclosure_core.document "
                        "WHERE document_id = ANY(:ids)"
                    ),
                    {"ids": self.document_ids},
                ).all():
                    if row.company_id and row.company_id not in self.company_ids:
                        self.company_ids.append(row.company_id)
                    if row.security_id and row.security_id not in self.security_ids:
                        self.security_ids.append(row.security_id)
                conn.execute(
                    text(
                        "DELETE FROM disclosure_ops.outbox_event "
                        "WHERE document_id = ANY(:ids)"
                    ),
                    {"ids": self.document_ids},
                )
            if self.event_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_ops.outbox_event "
                        "WHERE event_id = ANY(:ids)"
                    ),
                    {"ids": self.event_ids},
                )
            if self.asset_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.document_unit "
                        "WHERE asset_id = ANY(:ids)"
                    ),
                    {"ids": self.asset_ids},
                )
            if self.run_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.processing_run "
                        "WHERE processing_run_id = ANY(:ids)"
                    ),
                    {"ids": self.run_ids},
                )
            if self.document_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.document "
                        "WHERE document_id = ANY(:ids)"
                    ),
                    {"ids": self.document_ids},
                )
            if self.source_access_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.source_access "
                        "WHERE source_access_id = ANY(:ids)"
                    ),
                    {"ids": self.source_access_ids},
                )
            if self.security_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.security "
                        "WHERE security_id = ANY(:ids)"
                    ),
                    {"ids": self.security_ids},
                )
            if self.company_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.company_identifier "
                        "WHERE company_id = ANY(:ids)"
                    ),
                    {"ids": self.company_ids},
                )
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.company "
                        "WHERE company_id = ANY(:ids)"
                    ),
                    {"ids": self.company_ids},
                )


class FilingApiAdminFullChainTests(unittest.TestCase):
    def setUp(self) -> None:
        gate = require_mineru_and_sample(ADMIN_SAMPLES[0])
        self.engine = gate.engine
        self.mineru = gate.mineru
        self.tmpdir = tempfile.TemporaryDirectory(prefix="m06-api-admin-")
        self.settings = _settings(Path(self.tmpdir.name), mineru=self.mineru)
        self.app = _create_test_app(self.settings, self.engine)
        self.document_ids: list[str] = []
        self.source_access_ids: list[str] = []

    def tearDown(self) -> None:
        self._cleanup_rows()
        self.engine.dispose()
        self.tmpdir.cleanup()

    def test_admin_three_sample_chain_is_http_readable(self) -> None:
        for sample in ADMIN_SAMPLES:
            with self.subTest(sample=sample.label):
                sample_pdf = _sample_pdf_or_skip(sample.label)
                self._run_admin_sample(sample=sample, sample_pdf=sample_pdf)

    def _run_admin_sample(self, *, sample: AdminSample, sample_pdf: Path) -> None:
        provider_document_id = sample_pdf.stem
        self._assert_provider_document_id_available(provider_document_id)

        registered = _api_request(
            self.app,
            "POST",
            "/v1/admin/documents/register-local-pdf",
            json_body={
                "file_path": str(sample_pdf),
                "company_legal_name": sample.company_legal_name,
                "security_code": sample.security_code,
                "exchange": sample.exchange,
                "filing_type": sample.filing_type,
                "title": sample.title,
                "announcement_date": sample.announcement_date.isoformat(),
                "provider_document_id": provider_document_id,
                "provider": "cninfo",
                **({"report_period": sample.report_period} if sample.report_period else {}),
            },
        )
        self.assertEqual(registered.status_code, 200, registered.body)
        register_payload = registered.json()
        document_id = register_payload["document_id"]
        self.assertIsNotNone(document_id)
        self.assertFalse(register_payload["reused_existing_document"])
        self.document_ids.append(document_id)
        self.source_access_ids.append(register_payload["source_access_id"])

        parsed = _api_request(
            self.app,
            "POST",
            f"/v1/admin/documents/{document_id}/parse",
            json_body={},
        )
        self.assertEqual(parsed.status_code, 200, parsed.body)
        parse_payload = parsed.json()
        self.assertEqual(parse_payload["status"], "succeeded", parse_payload)
        processing_run_id = parse_payload["processing_run_id"]

        built = _api_request(
            self.app,
            "POST",
            f"/v1/admin/documents/{document_id}/build-units",
        )
        self.assertEqual(built.status_code, 200, built.body)
        build_payload = built.json()
        self.assertEqual(build_payload["unit_build_status"], "succeeded")
        self.assertGreater(build_payload["unit_count"], 0)

        published = _api_request(
            self.app,
            "POST",
            f"/v1/admin/runs/{processing_run_id}/publish",
            json_body={"allow_empty": False},
        )
        self.assertEqual(published.status_code, 200, published.body)
        publish_payload = published.json()
        self.assertEqual(publish_payload["document_id"], document_id)
        self.assertTrue(publish_payload["is_active"])

        units = _api_request(
            self.app,
            "GET",
            f"/v1/documents/{document_id}/units",
            query={"limit": 1000},
        )
        self.assertEqual(units.status_code, 200, units.body)
        units_payload = units.json()
        self.assertGreater(len(units_payload["items"]), 0)
        self._assert_sample_units(sample=sample, units=units_payload["items"])
        asset_id = units_payload["items"][0]["asset_id"]

        source_ref = _api_request(
            self.app,
            "GET",
            f"/v1/units/{asset_id}/source-ref",
        )
        self.assertEqual(source_ref.status_code, 200, source_ref.body)
        self.assertEqual(source_ref.json()["document_id"], document_id)

        after_seq = self._seq_before_document_changes(document_id)
        changes = _api_request(
            self.app,
            "GET",
            "/v1/changes",
            query={"after_seq": after_seq, "limit": 1000},
        )
        self.assertEqual(changes.status_code, 200, changes.body)
        changed_documents = {
            item["document_id"]
            for item in changes.json()["items"]
            if item["document_id"] is not None
        }
        self.assertIn(document_id, changed_documents)

        for payload in (
            register_payload,
            parse_payload,
            build_payload,
            publish_payload,
            units_payload,
            source_ref.json(),
            changes.json(),
        ):
            _assert_no_leaks(self, self.settings, payload)

    def _assert_sample_units(
        self, *, sample: AdminSample, units: list[dict[str, Any]]
    ) -> None:
        if sample.label == "annual_report":
            # ub-2026.07-5+ semantic grouping: business sections are mixed
            # units with ordered parts; recall keys live in semantic_keys.
            self.assertTrue(
                any(
                    "管理层讨论与分析" in " ".join(unit["heading_path"])
                    and _unit_texts(unit)
                    for unit in units
                )
            )
            receivable_units = [
                unit
                for unit in units
                if "receivable_aging" in (unit.get("semantic_keys") or [])
                or unit.get("semantic_key") == "receivable_aging"
            ]
            self.assertTrue(receivable_units)
            tables = [
                part
                for unit in receivable_units
                for part in _unit_table_parts(unit)
            ]
            self.assertTrue(any(part.get("headers") for part in tables))
            self.assertTrue(any(part.get("rows") for part in tables))
            self.assertTrue(any(part.get("unit") for part in tables))
        elif sample.label == "ir_activity":
            qa_units = [unit for unit in units if unit["payload_kind"] == "qa"]
            self.assertGreaterEqual(len(qa_units), 30)
            self.assertTrue(
                any(
                    "美国加征关税" in unit["payload"].get("question", "")
                    and "美国收入占比很低" in unit["payload"].get("answer", "")
                    for unit in qa_units
                )
            )

    def _assert_provider_document_id_available(self, provider_document_id: str) -> None:
        with self.engine.connect() as conn:
            existing = conn.execute(
                text(
                    "SELECT 1 FROM disclosure_core.document "
                    "WHERE provider='cninfo' AND provider_document_id=:pid"
                ),
                {"pid": provider_document_id},
            ).scalar_one_or_none()
        self.assertIsNone(
            existing,
            "fixed provider_document_id already exists",
        )

    def _seq_before_document_changes(self, document_id: str) -> int:
        with self.engine.connect() as conn:
            first_seq = conn.execute(
                text(
                    "SELECT min(seq) FROM disclosure_ops.outbox_event "
                    "WHERE document_id=:document_id"
                ),
                {"document_id": document_id},
            ).scalar_one()
        self.assertIsNotNone(first_seq)
        return int(first_seq) - 1

    def _cleanup_rows(self) -> None:
        with self.engine.begin() as conn:
            # Harvest SubjectResolver-created company/security rows before the
            # documents disappear — the register path creates them and this
            # class leaked one company per zero-skip suite run (2026-07-06).
            subject_rows = conn.execute(
                text(
                    "SELECT DISTINCT company_id, security_id "
                    "FROM disclosure_core.document WHERE document_id = ANY(:ids)"
                ),
                {"ids": list(self.document_ids)},
            ).all() if self.document_ids else []
            for document_id in self.document_ids:
                conn.execute(
                    text("DELETE FROM disclosure_ops.outbox_event WHERE document_id=:id"),
                    {"id": document_id},
                )
                conn.execute(
                    text("DELETE FROM disclosure_core.document_unit WHERE document_id=:id"),
                    {"id": document_id},
                )
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.processing_run "
                        "WHERE document_id=:id"
                    ),
                    {"id": document_id},
                )
                conn.execute(
                    text("DELETE FROM disclosure_core.document WHERE document_id=:id"),
                    {"id": document_id},
                )
            for source_access_id in self.source_access_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.source_access "
                        "WHERE source_access_id=:id"
                    ),
                    {"id": source_access_id},
                )
            for row in subject_rows:
                if row.security_id:
                    conn.execute(
                        text(
                            "DELETE FROM disclosure_core.security "
                            "WHERE security_id=:id AND NOT EXISTS ("
                            "  SELECT 1 FROM disclosure_core.document d "
                            "  WHERE d.security_id=:id)"
                        ),
                        {"id": row.security_id},
                    )
                if row.company_id:
                    conn.execute(
                        text(
                            "DELETE FROM disclosure_core.company_identifier "
                            "WHERE company_id=:id AND NOT EXISTS ("
                            "  SELECT 1 FROM disclosure_core.document d "
                            "  WHERE d.company_id=:id)"
                        ),
                        {"id": row.company_id},
                    )
                    conn.execute(
                        text(
                            "DELETE FROM disclosure_core.company "
                            "WHERE company_id=:id AND NOT EXISTS ("
                            "  SELECT 1 FROM disclosure_core.document d "
                            "  WHERE d.company_id=:id) AND NOT EXISTS ("
                            "  SELECT 1 FROM disclosure_core.security s "
                            "  WHERE s.company_id=:id)"
                        ),
                        {"id": row.company_id},
                    )


if __name__ == "__main__":
    unittest.main()
