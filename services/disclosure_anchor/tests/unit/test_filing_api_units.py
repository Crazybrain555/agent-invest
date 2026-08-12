import copy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from fastapi import HTTPException

from disclosure_anchor.api.pagination import (
    UnitCursor,
    decode_unit_cursor,
    encode_unit_cursor,
)
from disclosure_anchor.api.routers.units import (
    _validate_semantic_key,
    get_unit,
    get_unit_context,
    get_unit_evidence,
    get_unit_source_ref,
    list_document_units,
)
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.domain.services.unit_hashing import (
    canonical_json,
    sha256_prefixed,
)
from disclosure_anchor.settings import Settings
from tests.unit._historical_v4_fixture import write_text_ir_bundle


def _document_row() -> dict:
    now = datetime(2026, 7, 5, 12, tzinfo=timezone.utc)
    return {
        "document_id": "doc_1",
        "provider": "cninfo",
        "provider_document_id": "pid-doc_1",
        "security_code": "002484",
        "exchange": "szse",
        "filing_type": "annual_report",
        "disclosure_topics": None,
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
        "publisher_categories": None,
        "market": None,
        "content_categories": None,
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
        "heading_path_text": "第一节 > 风险",
        "title": "风险提示",
        "order_index": order_index,
        "semantic_key": "risk",
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
        "disclosure_topics": None,
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


def _request(
    engine: _Engine,
    *,
    settings: Settings | None = None,
) -> SimpleNamespace:
    state = SimpleNamespace(reader_db_engine=engine)
    if settings is not None:
        state.settings = settings
    return SimpleNamespace(
        app=SimpleNamespace(state=state),
        query_params={},
    )


def _settings(root: Path) -> Settings:
    service_root = root / "service"
    shared_root = root / "shared"
    return Settings(
        disclosure_data_root=service_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=service_root / "runtime",
        mineru_model_cache=shared_root / "mineru",
        hf_home=shared_root / "hf",
        modelscope_cache=shared_root / "modelscope",
    )


def _evidence_bundle(
    root: Path,
) -> tuple[Settings, dict, dict, Path, bytes, str]:
    settings = _settings(root)
    paths = FileStorePathBuilder(settings)
    ir_relpath = paths.normalized_ir_run_relpath(
        provider="cninfo",
        security_code="002484",
        provider_document_id="pid-doc_1",
        processing_run_id="run_parse_owner",
    )
    data_root = settings.disclosure_data_root / "data"
    normalized_ir = write_text_ir_bundle(data_root, ir_relpath)
    content = b"\x89PNG\r\n\x1a\nunit-evidence"
    evidence_path = data_root / "parser" / "a" / "evidence.png"
    evidence_path.write_bytes(content)
    evidence_sha256 = "sha256:" + hashlib.sha256(content).hexdigest()
    normalized_ir["parser_artifacts"]["files"]["source_bbox_visual_000001_000001"] = {
        "availability": "present",
        "relpath": str(evidence_path.relative_to(data_root)),
        "sha256": evidence_sha256,
        "size_bytes": len(content),
    }
    ir_path = data_root / ir_relpath
    ir_content = json.dumps(
        normalized_ir,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    ir_path.write_bytes(ir_content)
    locator = {
        "evidence_artifacts": [
            {
                "artifact_role": "source_bbox_visual_000001_000001",
                "sha256": evidence_sha256,
                "size_bytes": len(content),
                "media_type": "image/png",
                "pixel_width": 1200,
                "pixel_height": 1800,
            }
        ]
    }
    row = {
        "asset_id": "asset_1",
        "document_id": "doc_1",
        "processing_run_id": "run_rebuild_active",
        "artifact_owner_processing_run_id": "run_parse_owner",
        "resolved_artifact_owner_processing_run_id": "run_parse_owner",
        "artifact_owner_document_id": "doc_1",
        "artifact_owner_run_kind": "parse",
        "payload_kind": "text",
        "payload": {"text": "视觉证据"},
        "artifact_locator": locator,
        "provider": "cninfo",
        "provider_document_id": "pid-doc_1",
        "security_code": "002484",
        "raw_file_hash": normalized_ir["source_pdf_sha256"],
        "producer_input_raw_file_hash": normalized_ir["source_pdf_sha256"],
        "artifact_owner_input_raw_file_hash": normalized_ir["source_pdf_sha256"],
        "artifact_hash": "sha256:" + hashlib.sha256(ir_content).hexdigest(),
        "producer_artifact_hash": ("sha256:" + hashlib.sha256(ir_content).hexdigest()),
    }
    return (
        settings,
        row,
        normalized_ir,
        evidence_path,
        content,
        evidence_sha256,
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
                [
                    {
                        "processing_run_id": "run_failed",
                        "status": "failed",
                        "unit_build_status": "failed",
                    }
                ],
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
        self.assertIn(
            "ORDER BY u.order_index ASC, u.asset_id ASC", engine.statements[1]
        )

    def test_document_units_explicit_history_run_is_resolved(self) -> None:
        engine = _Engine(
            [
                [_document_row()],
                [{"exists": 1}],
                [
                    _unit_row(
                        "asset_old", processing_run_id="run_old", is_active_run=False
                    )
                ],
                [
                    {
                        "processing_run_id": "run_active",
                        "status": "succeeded",
                        "unit_build_status": "succeeded",
                    }
                ],
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
        self.assertEqual(
            caught.exception.detail["error_code"], "L1_PROCESSING_REQUIRED"
        )
        self.assertEqual(caught.exception.detail["detail"], {"status": "parsed"})

    def test_heading_prefix_uses_candidate_and_exact_prefix_predicates(self) -> None:
        engine = _Engine(
            [
                [_document_row()],
                [_unit_row("asset_1")],
                [
                    {
                        "processing_run_id": "run_active",
                        "status": "succeeded",
                        "unit_build_status": "succeeded",
                    }
                ],
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

    def test_semantic_filter_matches_only_the_optional_scalar_key(self) -> None:
        engine = _Engine(
            [
                [_document_row()],
                [_unit_row("asset_1")],
                [
                    {
                        "processing_run_id": "run_active",
                        "status": "succeeded",
                        "unit_build_status": "succeeded",
                    }
                ],
            ]
        )

        list_document_units(
            "doc_1",
            _request(engine),
            semantic_key="risk",
        )

        sql = engine.statements[1]
        self.assertIn("u.semantic_key = :semantic_key", sql)
        self.assertNotIn("semantic_keys", sql)

    def test_semantic_key_filters_reject_non_contract_and_control_characters(
        self,
    ) -> None:
        for value in (
            "risk\x00key",
            "risk\n,revenue",
            "risk\t,revenue",
            "risk\r,revenue",
            "risk\x85,revenue",
            "risk\u00a0,revenue",
            "Risk",
            "risk-key",
            "风险",
            "_risk",
        ):
            with self.subTest(value=repr(value)):
                with self.assertRaises(HTTPException) as caught:
                    _validate_semantic_key("semantic_key", value)
                self.assertEqual(caught.exception.status_code, 422)

        self.assertEqual(
            _validate_semantic_key("semantic_key", "cash_flow_note_2"),
            "cash_flow_note_2",
        )

    def test_unit_cursor_uses_row_comparison(self) -> None:
        engine = _Engine(
            [
                [_document_row()],
                [_unit_row("asset_2", order_index=2)],
                [
                    {
                        "processing_run_id": "run_active",
                        "status": "succeeded",
                        "unit_build_status": "succeeded",
                    }
                ],
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
        descriptor = {
            "artifact_role": "evidence_image_000001",
            "sha256": "sha256:" + "e" * 64,
            "size_bytes": 123,
            "media_type": "image/png",
        }
        unit_row = _unit_row("asset_1")
        unit_row["artifact_locator"] = {"evidence_artifacts": [descriptor]}
        unit = get_unit("asset_1", _request(_Engine([[unit_row]])))
        self.assertEqual(
            unit.asset_uri, "asset://disclosure_anchor/v1/document_unit/asset_1"
        )
        self.assertEqual(
            unit.evidence_refs[0].uri,
            "/v1/units/asset_1/evidence/" + "e" * 64,
        )
        self.assertNotIn("artifact_role", unit.evidence_refs[0].model_dump())
        self.assertNotIn("relpath", unit.evidence_refs[0].model_dump())

        source_row = _source_ref_row()
        source_row["payload_kind"] = "mixed"
        source_row["_unit_payload"] = {
            "parts": [
                {
                    "kind": "image",
                    "artifact_locator": {"evidence_artifacts": [descriptor]},
                }
            ]
        }
        source_engine = _Engine([[source_row]])
        source_ref = get_unit_source_ref("asset_1", _request(source_engine))
        self.assertEqual(source_ref.contract_version, "source_ref.v1")
        self.assertEqual(source_ref.unit_content_hash, "sha256:" + "b" * 64)
        self.assertEqual(source_ref.evidence_refs, unit.evidence_refs)
        self.assertIn(
            "disclosure_public.document_units_v1",
            source_engine.statements[0],
        )

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

        with self.assertRaises(HTTPException) as caught:
            get_unit_context("asset_1", _request(_Engine([])), max_chars=-1)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.detail["error_code"], "VALIDATION_ERROR")

    def test_unit_evidence_read_is_authorized_and_integrity_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (
                settings,
                row,
                normalized_ir,
                evidence_path,
                content,
                evidence_sha256,
            ) = _evidence_bundle(Path(tmp))
            digest = evidence_sha256.removeprefix("sha256:")

            engine = _Engine([[row]])
            response = get_unit_evidence(
                "asset_1",
                digest,
                _request(engine, settings=settings),
            )
            self.assertEqual(response.body, content)
            self.assertEqual(response.media_type, "image/png")
            self.assertEqual(response.headers["etag"], f'"{evidence_sha256}"')
            self.assertEqual(
                response.headers["cache-control"],
                "public, max-age=31536000, immutable",
            )
            self.assertIn("disclosure_public.document_units_v1", engine.statements[0])
            self.assertIn("disclosure_public.documents_v1", engine.statements[0])
            self.assertIn("disclosure_public.processing_runs_v1", engine.statements[0])
            self.assertEqual(
                engine.statements[0].count("disclosure_public.processing_runs_v1"),
                2,
            )
            self.assertNotIn("disclosure_core", engine.statements[0])
            self.assertNotIn("disclosure_ops", engine.statements[0])
            self.assertNotIn("IS_ACTIVE", engine.statements[0].upper())

            mixed_row = copy.deepcopy(row)
            mixed_row["payload_kind"] = "mixed"
            mixed_row["artifact_locator"] = None
            mixed_row["payload"] = {
                "parts": [
                    {
                        "kind": "image",
                        "artifact_locator": row["artifact_locator"],
                    }
                ]
            }
            mixed_response = get_unit_evidence(
                "asset_1",
                digest,
                _request(_Engine([[mixed_row]]), settings=settings),
            )
            self.assertEqual(mixed_response.body, content)

            with self.assertRaises(HTTPException) as malformed:
                get_unit_evidence(
                    "asset_1",
                    "A" * 64,
                    _request(_Engine([]), settings=settings),
                )
            self.assertEqual(malformed.exception.status_code, 422)

            with self.assertRaises(HTTPException) as unreferenced:
                get_unit_evidence(
                    "asset_1",
                    "f" * 64,
                    _request(_Engine([[row]]), settings=settings),
                )
            self.assertEqual(unreferenced.exception.status_code, 404)

            def assert_integrity_error(
                changed_row: dict,
                expected_reason: str,
            ) -> None:
                with self.assertRaises(HTTPException) as caught:
                    get_unit_evidence(
                        "asset_1",
                        digest,
                        _request(_Engine([[changed_row]]), settings=settings),
                    )
                self.assertEqual(caught.exception.status_code, 500)
                self.assertEqual(
                    caught.exception.detail["error_code"],
                    "EVIDENCE_INTEGRITY_ERROR",
                )
                self.assertEqual(
                    caught.exception.detail["detail"]["reason"],
                    expected_reason,
                )

            ir_hash_drift = copy.deepcopy(row)
            ir_hash_drift["artifact_hash"] = "sha256:" + "0" * 64
            assert_integrity_error(
                ir_hash_drift,
                "artifact_owner_hash_mismatch",
            )
            shared_hash_drift = copy.deepcopy(row)
            shared_hash_drift["artifact_hash"] = "sha256:" + "0" * 64
            shared_hash_drift["producer_artifact_hash"] = shared_hash_drift[
                "artifact_hash"
            ]
            assert_integrity_error(
                shared_hash_drift,
                "normalized_ir_hash_mismatch",
            )

            wrong_owner = copy.deepcopy(row)
            wrong_owner["artifact_owner_document_id"] = "doc_other"
            assert_integrity_error(wrong_owner, "artifact_owner_invalid")

            wrong_source = copy.deepcopy(row)
            wrong_source["raw_file_hash"] = "sha256:" + "0" * 64
            assert_integrity_error(
                wrong_source,
                "artifact_owner_source_hash_mismatch",
            )

            wrong_producer_source = copy.deepcopy(row)
            wrong_producer_source["producer_input_raw_file_hash"] = (
                "sha256:" + "0" * 64
            )
            assert_integrity_error(
                wrong_producer_source,
                "artifact_owner_source_hash_mismatch",
            )

            wrong_owner_source = copy.deepcopy(row)
            wrong_owner_source["artifact_owner_input_raw_file_hash"] = (
                "sha256:" + "0" * 64
            )
            assert_integrity_error(
                wrong_owner_source,
                "artifact_owner_source_hash_mismatch",
            )

            non_parse_owner = copy.deepcopy(row)
            non_parse_owner["artifact_owner_run_kind"] = "rebuild_units"
            assert_integrity_error(non_parse_owner, "artifact_owner_invalid")

            manifest_drift = copy.deepcopy(row)
            manifest_drift["artifact_locator"]["evidence_artifacts"][0][
                "artifact_role"
            ] = "source_page_visual_999999"
            assert_integrity_error(manifest_drift, "evidence_manifest_mismatch")

            unknown_descriptor = copy.deepcopy(row)
            unknown_descriptor["artifact_locator"]["evidence_artifacts"][0][
                "parser_path"
            ] = "private.png"
            assert_integrity_error(
                unknown_descriptor,
                "unit_evidence_locator_invalid",
            )

            data_root = settings.disclosure_data_root / "data"
            ir_path = data_root / FileStorePathBuilder(
                settings
            ).normalized_ir_run_relpath(
                provider="cninfo",
                security_code="002484",
                provider_document_id="pid-doc_1",
                processing_run_id="run_parse_owner",
            )
            invalid_current_ir = copy.deepcopy(normalized_ir)
            invalid_current_ir["elements"][1]["raw_kind"] = "unsupported_carrier"
            invalid_current_content = json.dumps(
                invalid_current_ir,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            ir_path.write_bytes(invalid_current_content)
            invalid_current_row = copy.deepcopy(row)
            invalid_current_row["artifact_hash"] = (
                "sha256:" + hashlib.sha256(invalid_current_content).hexdigest()
            )
            invalid_current_row["producer_artifact_hash"] = invalid_current_row[
                "artifact_hash"
            ]
            historical_response = get_unit_evidence(
                "asset_1",
                digest,
                _request(
                    _Engine([[invalid_current_row]]),
                    settings=settings,
                ),
            )
            self.assertEqual(historical_response.body, content)

            unsafe_ir = copy.deepcopy(normalized_ir)
            unsafe_ir["parser_artifacts"]["files"]["source_bbox_visual_000001_000001"][
                "relpath"
            ] = "../escape.png"
            unsafe_content = json.dumps(
                unsafe_ir,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            ir_path.write_bytes(unsafe_content)
            unsafe_row = copy.deepcopy(row)
            unsafe_row["artifact_hash"] = (
                "sha256:" + hashlib.sha256(unsafe_content).hexdigest()
            )
            unsafe_row["producer_artifact_hash"] = unsafe_row["artifact_hash"]
            assert_integrity_error(unsafe_row, "normalized_ir_invalid")

            valid_ir_content = json.dumps(
                normalized_ir,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            ir_path.write_bytes(valid_ir_content)

            outside_path = Path(tmp) / "outside.png"
            outside_path.write_bytes(content)
            evidence_path.unlink()
            evidence_path.symlink_to(outside_path)
            assert_integrity_error(row, "evidence_artifact_path_invalid")

            evidence_path.unlink()
            assert_integrity_error(row, "evidence_artifact_missing")

            evidence_path.write_bytes(content + b"x")
            assert_integrity_error(row, "evidence_artifact_size_mismatch")

            evidence_path.write_bytes(content[:-1] + b"f")
            assert_integrity_error(row, "evidence_artifact_hash_mismatch")

            evidence_path.write_bytes(content)
            media_drift = copy.deepcopy(row)
            media_drift["artifact_locator"]["evidence_artifacts"][0]["media_type"] = (
                "image/jpeg"
            )
            assert_integrity_error(
                media_drift,
                "evidence_artifact_media_type_mismatch",
            )


if __name__ == "__main__":
    unittest.main()
