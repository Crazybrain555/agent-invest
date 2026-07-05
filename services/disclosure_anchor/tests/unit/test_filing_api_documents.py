import base64
from datetime import date, datetime, timezone
import json
from types import SimpleNamespace
import unittest

from fastapi import HTTPException

from disclosure_anchor.api.pagination import (
    DocumentCursor,
    decode_document_cursor,
    encode_document_cursor,
)
from disclosure_anchor.api.routers.documents import (
    get_document,
    list_document_runs,
    list_documents,
)
from disclosure_anchor.api.routers.filings import latest_filings


def _document_row(document_id: str, announcement_date: date | None) -> dict:
    now = datetime(2026, 7, 5, 12, tzinfo=timezone.utc)
    return {
        "document_id": document_id,
        "provider": "cninfo",
        "provider_document_id": f"pid-{document_id}",
        "security_code": "002484",
        "exchange": "szse",
        "filing_type": "annual_report",
        "title": "annual report",
        "announcement_date": announcement_date,
        "report_period": "2025A",
        "raw_file_hash": "sha256:" + "a" * 64,
        "status": "published",
        "current_processing_run_id": "run_active",
        "created_at": now,
        "updated_at": now,
        "contract_version": "document.v1",
        "company_ref": "co_1",
        "security_ref": "sec_1",
        "source_ref": "sa_1",
        "supersedes_document_id": None,
        "correction_of_document_id": None,
        "superseded_by_document_id": None,
        "provider_metadata": {},
    }


def _run_row(processing_run_id: str) -> dict:
    now = datetime(2026, 7, 5, 12, tzinfo=timezone.utc)
    return {
        "processing_run_id": processing_run_id,
        "document_id": "doc_1",
        "run_kind": "full",
        "status": "succeeded",
        "parser_name": "MinerU",
        "parser_version": "3.4.0",
        "artifact_hash": "sha256:" + "b" * 64,
        "content_hash_aggregate": "sha256:" + "c" * 64,
        "structure_hash": "sha256:" + "d" * 64,
        "is_active": True,
        "started_at": now,
        "finished_at": now,
        "created_at": now,
        "parser_backend": "pipeline",
        "input_raw_file_hash": "sha256:" + "e" * 64,
        "parser_method": "auto",
        "parser_language": "ch",
        "unit_build_status": "succeeded",
        "unit_build_attempt_count": 1,
        "unit_built_at": now,
        "builder_rules_version": "ub-2026.07-1",
    }


class _Result:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> "_Result":
        return self

    def all(self) -> list[dict]:
        return self._rows

    def one_or_none(self) -> dict | None:
        return self._rows[0] if self._rows else None


class _Connection:
    def __init__(self, engine: "_Engine") -> None:
        self._engine = engine

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute(self, statement: object, params: dict | None = None) -> _Result:
        self._engine.statements.append(str(statement))
        self._engine.params.append(params or {})
        return _Result(self._engine.rows)


class _Engine:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.statements: list[str] = []
        self.params: list[dict] = []

    def connect(self) -> _Connection:
        return _Connection(self)


def _request(engine: _Engine) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(reader_db_engine=engine)),
        query_params={},
    )


def _request_with_query(engine: _Engine, query_params: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(reader_db_engine=engine)),
        query_params=query_params,
    )


class FilingApiDocumentTests(unittest.TestCase):
    def test_document_cursor_json_shape_is_fixed(self) -> None:
        cursor = encode_document_cursor(
            DocumentCursor(announcement_date=date(2026, 7, 5), document_id="doc_1")
        )
        payload = json.loads(base64.b64decode(cursor).decode("utf-8"))
        self.assertEqual(
            payload,
            {"announcement_date": "2026-07-05", "document_id": "doc_1"},
        )
        self.assertEqual(
            decode_document_cursor(cursor),
            DocumentCursor(announcement_date=date(2026, 7, 5), document_id="doc_1"),
        )

    def test_documents_list_uses_keyset_order_and_next_cursor(self) -> None:
        engine = _Engine(
            [
                _document_row("doc_2", date(2026, 7, 5)),
                _document_row("doc_1", date(2026, 7, 4)),
            ]
        )

        response = list_documents(_request(engine), limit=1)

        self.assertEqual([item.document_id for item in response.items], ["doc_2"])
        self.assertEqual(
            decode_document_cursor(response.next_cursor),
            DocumentCursor(announcement_date=date(2026, 7, 5), document_id="doc_2"),
        )
        self.assertIn(
            "ORDER BY announcement_date DESC NULLS LAST, document_id DESC",
            engine.statements[0],
        )
        self.assertNotIn("OFFSET", engine.statements[0].upper())
        self.assertEqual(engine.params[0]["limit_plus_one"], 2)

    def test_documents_cursor_predicate_handles_non_null_announcement_date(self) -> None:
        engine = _Engine([_document_row("doc_1", None)])
        cursor = encode_document_cursor(
            DocumentCursor(announcement_date=date(2026, 7, 5), document_id="doc_2")
        )

        list_documents(_request(engine), cursor=cursor)

        self.assertIn(
            "announcement_date IS NULL OR announcement_date < :cursor_announcement_date",
            engine.statements[0],
        )
        self.assertEqual(engine.params[0]["cursor_announcement_date"], date(2026, 7, 5))
        self.assertEqual(engine.params[0]["cursor_document_id"], "doc_2")

    def test_documents_cursor_predicate_handles_null_announcement_date(self) -> None:
        engine = _Engine([_document_row("doc_1", None)])
        cursor = encode_document_cursor(
            DocumentCursor(announcement_date=None, document_id="doc_2")
        )

        list_documents(_request(engine), cursor=cursor)

        self.assertIn(
            "announcement_date IS NULL AND document_id < :cursor_document_id",
            engine.statements[0],
        )
        self.assertNotIn("cursor_announcement_date", engine.params[0])

    def test_documents_limit_over_max_returns_422(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            list_documents(_request(_Engine([])), limit=1001)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["error_code"], "VALIDATION_ERROR")

    def test_documents_bad_cursor_returns_422(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            list_documents(_request(_Engine([])), cursor="not-base64")
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["error_code"], "VALIDATION_ERROR")

    def test_get_document_404s_when_missing(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            get_document("doc_missing", _request(_Engine([])))
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.detail["error_code"], "NOT_FOUND")

    def test_get_document_reject_superseded_returns_410(self) -> None:
        row = _document_row("doc_old", date(2026, 7, 5))
        row["superseded_by_document_id"] = "doc_new"

        with self.assertRaises(HTTPException) as caught:
            get_document(
                "doc_old",
                _request_with_query(_Engine([row]), {"reject_superseded": "true"}),
            )

        self.assertEqual(caught.exception.status_code, 410)
        self.assertEqual(caught.exception.detail["error_code"], "GONE_SUPERSEDED")
        self.assertEqual(caught.exception.detail["detail"], {"superseded_by": "doc_new"})

    def test_document_runs_order_is_pinned(self) -> None:
        engine = _Engine([_run_row("run_2"), _run_row("run_1")])

        response = list_document_runs("doc_1", _request(engine))

        self.assertEqual([item.processing_run_id for item in response], ["run_2", "run_1"])
        self.assertIn(
            "ORDER BY started_at DESC, processing_run_id DESC",
            engine.statements[0],
        )

    def test_latest_filings_sql_uses_distinct_on_and_outer_keyset_order(self) -> None:
        engine = _Engine([_document_row("doc_2", date(2026, 7, 5))])

        response = latest_filings(_request(engine), company_ref="co_1")

        self.assertEqual([item.document_id for item in response.items], ["doc_2"])
        sql = engine.statements[0]
        self.assertIn(
            "SELECT DISTINCT ON (company_ref, filing_type, report_period)",
            sql,
        )
        self.assertIn("superseded_by_document_id IS NULL", sql)
        self.assertIn("company_ref = :company_ref", sql)
        self.assertIn(
            "ORDER BY company_ref, filing_type, report_period, "
            "announcement_date DESC NULLS LAST, document_id DESC",
            sql,
        )
        self.assertTrue(
            sql.rstrip().endswith(
                "ORDER BY announcement_date DESC NULLS LAST, document_id DESC "
                "LIMIT :limit_plus_one"
            )
        )


if __name__ == "__main__":
    unittest.main()
