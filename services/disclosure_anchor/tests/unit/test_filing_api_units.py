from datetime import date, datetime, timezone
from types import SimpleNamespace
import unittest

from fastapi import HTTPException

from disclosure_anchor.api.pagination import (
    UnitCursor,
    decode_unit_cursor,
    encode_unit_cursor,
)
from disclosure_anchor.api.routers.units import (
    get_unit,
    get_unit_context,
    get_unit_source_ref,
    list_document_units,
)
from disclosure_anchor.domain.services.unit_hashing import canonical_json, sha256_prefixed


def _document_row() -> dict:
    now = datetime(2026, 7, 5, 12, tzinfo=timezone.utc)
    return {
        "document_id": "doc_1",
        "provider": "cninfo",
        "provider_document_id": "pid-doc_1",
        "security_code": "002484",
        "exchange": "szse",
        "filing_type": "annual_report",
        "title": "annual report",
        "announcement_date": date(2026, 7, 5),
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


def _unit_row(
    asset_id: str = "asset_1",
    *,
    processing_run_id: str = "run_active",
    order_index: int = 1,
    is_active_run: bool = True,
) -> dict:
    now = datetime(2026, 7, 5, 12, tzinfo=timezone.utc)
    return {
        "asset_id": asset_id,
        "document_id": "doc_1",
        "processing_run_id": processing_run_id,
        "provider_document_id": "pid-doc_1",
        "payload_kind": "text",
        "heading_path": ["第一节", "风险"],
        "title": "风险提示",
        "order_index": order_index,
        "semantic_key": "risk",
        "semantic_keys": ["risk"],
        "payload": {"b": 2, "a": "披露"},
        "content_hash": "sha256:" + "b" * 64,
        "structure_hash": "sha256:" + "c" * 64,
        "quality_status": "ok",
        "applicability": None,
        "page_no": None,
        "artifact_locator": None,
        "created_at": now,
        "contract_version": "document_unit.v1",
        "company_ref": "co_1",
        "security_ref": "sec_1",
        "security_code": "002484",
        "exchange": "szse",
        "filing_type": "annual_report",
        "report_period": "2025A",
        "announcement_date": date(2026, 7, 5),
        "producer_action_ref": processing_run_id,
        "source_ref": "sa_1",
        "parent_ref": "doc_1",
        "asset_kind": "document_unit",
        "observed_at": now,
        "source_tier": "tier_0a",
        "trace_level": "G0",
        "raw_file_hash": "sha256:" + "a" * 64,
        "query_projection_hash": "sha256:" + "d" * 64,
        "asset_uri": f"asset://disclosure_anchor/v1/document_unit/{asset_id}",
        "is_active_run": is_active_run,
    }


def _source_ref_row() -> dict:
    return {
        "service": "disclosure_anchor",
        "contract_version": "source_ref.v1",
        "asset_id": "asset_1",
        "source_access_id": "sa_1",
        "document_id": "doc_1",
        "provider": "cninfo",
        "provider_document_id": "pid-doc_1",
        "raw_file_hash": "sha256:" + "a" * 64,
        "processing_run_id": "run_active",
        "is_active_run": True,
        "payload_kind": "text",
        "heading_path": ["第一节", "风险"],
        "title": "风险提示",
        "unit_content_hash": "sha256:" + "b" * 64,
        "quality_status": "ok",
        "applicability": None,
        "page_no": None,
        "artifact_locator": None,
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

    def scalar_one_or_none(self) -> object | None:
        if not self._rows:
            return None
        return next(iter(self._rows[0].values()))


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
        return _Result(self._engine.result_sets.pop(0))


class _Engine:
    def __init__(self, result_sets: list[list[dict]]) -> None:
        self.result_sets = result_sets
        self.statements: list[str] = []
        self.params: list[dict] = []

    def connect(self) -> _Connection:
        return _Connection(self)


def _request(engine: _Engine) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(reader_db_engine=engine)),
        query_params={},
    )


class FilingApiUnitTests(unittest.TestCase):
    def test_unit_cursor_json_shape_is_fixed(self) -> None:
        cursor = encode_unit_cursor(UnitCursor(order_index=7, asset_id="asset_7"))
        self.assertEqual(
            decode_unit_cursor(cursor),
            UnitCursor(order_index=7, asset_id="asset_7"),
        )

    def test_document_units_default_to_active_run_and_carry_warning(self) -> None:
        engine = _Engine(
            [
                [_document_row()],
                [_unit_row("asset_1")],
                [{"processing_run_id": "run_failed", "status": "failed", "unit_build_status": "failed"}],
            ]
        )

        response = list_document_units("doc_1", _request(engine), limit=100)

        self.assertEqual(response.warning, "LATEST_PROCESSING_FAILED")
        self.assertEqual(response.items[0].asset_id, "asset_1")
        self.assertTrue(response.items[0].is_active_run)
        self.assertEqual(
            response.items[0].asset_uri,
            "asset://disclosure_anchor/v1/document_unit/asset_1",
        )
        self.assertEqual(engine.params[1]["processing_run_id"], "run_active")
        self.assertIn("ORDER BY u.order_index ASC, u.asset_id ASC", engine.statements[1])

    def test_document_units_explicit_history_run_is_resolved(self) -> None:
        engine = _Engine(
            [
                [_document_row()],
                [{"exists": 1}],
                [_unit_row("asset_old", processing_run_id="run_old", is_active_run=False)],
                [{"processing_run_id": "run_active", "status": "succeeded", "unit_build_status": "succeeded"}],
            ]
        )

        response = list_document_units(
            "doc_1",
            _request(engine),
            processing_run_id="run_old",
        )

        self.assertEqual(response.items[0].processing_run_id, "run_old")
        self.assertFalse(response.items[0].is_active_run)
        self.assertIn("processing_run_id = :processing_run_id", engine.statements[1])

    def test_document_units_without_active_run_returns_l1_required(self) -> None:
        document = _document_row()
        document["status"] = "parsed"
        document["current_processing_run_id"] = None

        with self.assertRaises(HTTPException) as caught:
            list_document_units("doc_1", _request(_Engine([[document]])))

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["error_code"], "L1_PROCESSING_REQUIRED")
        self.assertEqual(caught.exception.detail["detail"], {"status": "parsed"})

    def test_heading_prefix_uses_candidate_and_exact_prefix_predicates(self) -> None:
        engine = _Engine(
            [
                [_document_row()],
                [_unit_row("asset_1")],
                [{"processing_run_id": "run_active", "status": "succeeded", "unit_build_status": "succeeded"}],
            ]
        )

        list_document_units(
            "doc_1",
            _request(engine),
            heading_prefix=["第一节", "风险"],
            payload_kind="text",
        )

        sql = engine.statements[1]
        self.assertIn("u.heading_path @> CAST(:heading_prefix_json AS jsonb)", sql)
        self.assertIn("jsonb_array_length(u.heading_path) >= :heading_prefix_len", sql)
        self.assertIn("u.heading_path ->> 0 = :heading_prefix_0", sql)
        self.assertIn("u.heading_path ->> 1 = :heading_prefix_1", sql)
        self.assertEqual(engine.params[1]["heading_prefix_json"], '["第一节","风险"]')
        self.assertEqual(engine.params[1]["payload_kind"], "text")

    def test_unit_cursor_uses_row_comparison(self) -> None:
        engine = _Engine(
            [
                [_document_row()],
                [_unit_row("asset_2", order_index=2)],
                [{"processing_run_id": "run_active", "status": "succeeded", "unit_build_status": "succeeded"}],
            ]
        )

        list_document_units(
            "doc_1",
            _request(engine),
            cursor=encode_unit_cursor(UnitCursor(order_index=1, asset_id="asset_1")),
        )

        self.assertIn(
            "(u.order_index, u.asset_id) > (:cursor_order_index, :cursor_asset_id)",
            engine.statements[1],
        )
        self.assertEqual(engine.params[1]["cursor_order_index"], 1)
        self.assertEqual(engine.params[1]["cursor_asset_id"], "asset_1")

    def test_unit_get_and_source_ref_get(self) -> None:
        unit = get_unit("asset_1", _request(_Engine([[_unit_row("asset_1")]])))
        self.assertEqual(unit.asset_uri, "asset://disclosure_anchor/v1/document_unit/asset_1")

        source_ref = get_unit_source_ref("asset_1", _request(_Engine([[_source_ref_row()]])))
        self.assertEqual(source_ref.contract_version, "source_ref.v1")
        self.assertEqual(source_ref.unit_content_hash, "sha256:" + "b" * 64)

    def test_context_excerpt_uses_canonical_payload_json(self) -> None:
        engine = _Engine([[_unit_row("asset_1")], [_document_row()]])

        response = get_unit_context("asset_1", _request(engine), max_chars=10)

        source = canonical_json({"b": 2, "a": "披露"})
        excerpt = source[:10]
        self.assertEqual(response.excerpt, excerpt)
        self.assertEqual(response.start, 0)
        self.assertEqual(response.end, len(excerpt))
        self.assertEqual(response.excerpt_hash, sha256_prefixed(excerpt))
        self.assertEqual(response.document.document_id, "doc_1")

    def test_context_rejects_negative_max_chars(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            get_unit_context("asset_1", _request(_Engine([])), max_chars=-1)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["error_code"], "VALIDATION_ERROR")


if __name__ == "__main__":
    unittest.main()
