"""Versioned NormalizedIR contract checks."""

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import MinerUArtifactReader
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
    MinerUToNormalizedIRMapper,
)
from disclosure_anchor.adapters.parsers.mineru.table_reconciler import (
    TableReconciliationStats,
)
from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    validate_table_reconciliation_diagnostics,
    validate_table_reconciliation_payload,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    NormalizedIRVersionError,
    validate_normalized_ir_contract,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATHS = {
    version: REPO_ROOT / "contracts" / "normalized_ir" / f"{version}.json"
    for version in ("normalized_ir.v2", "normalized_ir.v3")
}
PHASE00_ROOT = REPO_ROOT / "tests" / "fixtures" / "phase00"
CLEAN_CHECKOUT_SAMPLE_KEYS = (
    "annual_report_excerpt",
    "ir_activity",
    "short_announcement",
)


def _content_list_from_ref(sample_key: str) -> Path | None:
    ref = PHASE00_ROOT / sample_key / "parser_artifacts_ref.txt"
    if not ref.is_file():
        return None
    for line in ref.read_text(encoding="utf-8").splitlines():
        if line.startswith("Content list: "):
            return Path(line.removeprefix("Content list: ").strip())
    return None


def _load_fixture(sample_key: str) -> dict:
    return json.loads(
        (PHASE00_ROOT / sample_key / "normalized_ir.v2.json").read_text(
            encoding="utf-8"
        )
    )


def _table_elements(payload: dict) -> list[dict]:
    return [element for element in payload["elements"] if element["kind"] == "table"]


class NormalizedIRContractTests(unittest.TestCase):
    def _schema(self, version: str = "normalized_ir.v3") -> dict:
        return json.loads(SCHEMA_PATHS[version].read_text(encoding="utf-8"))

    def _validator(self, version: str) -> Draft202012Validator:
        schema = self._schema(version)
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())

    def _assert_valid(self, payload: dict, *, label: str) -> None:
        version = str(payload.get("contract_version") or "normalized_ir.v3")
        errors = sorted(
            self._validator(version).iter_errors(payload),
            key=lambda error: list(error.path),
        )
        if errors:
            details = "\n".join(
                f"{label}:{'/'.join(map(str, error.path))}: {error.message}"
                for error in errors[:10]
            )
            self.fail(details)

    def _assert_invalid(self, payload: dict, *, label: str, path: tuple[str, ...]) -> None:
        version = str(payload.get("contract_version") or "normalized_ir.v3")
        errors = sorted(
            self._validator(version).iter_errors(payload),
            key=lambda error: list(error.path),
        )
        if not any(tuple(error.path) == path for error in errors):
            details = "\n".join(
                f"{label}:{'/'.join(map(str, error.path))}: {error.message}"
                for error in errors[:10]
            )
            self.fail(
                f"{label}: expected schema error at {'/'.join(path)}, got:\n{details}"
            )

    def test_schema_has_phase04_required_contract_shape(self) -> None:
        schema = self._schema()
        required = set(schema["required"])
        self.assertIn("parser_artifacts", required)
        self.assertIn("elements", required)
        element_required = set(schema["properties"]["elements"]["items"]["required"])
        self.assertEqual(
            element_required,
            {"ir_id", "kind", "raw_kind", "order_index", "source_item_index"},
        )
        kind_enum = set(
            schema["properties"]["elements"]["items"]["properties"]["kind"]["enum"]
        )
        self.assertEqual(
            kind_enum,
            {
                "text",
                "heading",
                "table",
                "image",
                "equation",
                "page_furniture",
                "unknown",
            },
        )
        Draft202012Validator.check_schema(schema)

    def test_clean_checkout_phase00_fixtures_validate_against_schema(self) -> None:
        for sample_key in CLEAN_CHECKOUT_SAMPLE_KEYS:
            data = json.loads(
                (PHASE00_ROOT / sample_key / "normalized_ir.v2.json").read_text(
                    encoding="utf-8"
                )
            )
            self._assert_valid(data, label=sample_key)
            self.assertGreater(len(data["elements"]), 0, sample_key)

    def test_current_mapper_output_is_v3_only(self) -> None:
        current = MinerUToNormalizedIRMapper().map_content_list(
            content_list=[{"type": "text", "text": "正文", "page_idx": 0}],
            parser_info=MinerUParserInfo(
                name="MinerU",
                package_version="3.4.0",
                backend="pipeline",
                method="auto",
                language="ch",
                formula=False,
                table=True,
            ),
            document_metadata={
                "document_id": "v3_contract_probe",
                "source_pdf": "raw/probe.pdf",
                "title": "probe",
            },
            parser_artifacts={
                "artifact_root_relpath": "parser_artifacts/probe",
                "content_list_relpath": "parser_artifacts/probe/content.json",
            },
        )

        self._assert_valid(current, label="current_v3")
        v2_errors = list(self._validator("normalized_ir.v2").iter_errors(current))
        self.assertTrue(
            any(tuple(error.path) == ("contract_version",) for error in v2_errors)
        )

    def test_v3_rejects_legacy_native_shadow_fields(self) -> None:
        current = _load_fixture("short_announcement")
        current["contract_version"] = "normalized_ir.v3"
        current["native_text"] = {
            "status": "empty",
            "extractor": {"name": "legacy", "version": "1"},
            "content_hash": "sha256:" + "0" * 64,
            "non_whitespace_chars": 0,
            "pages": [],
        }
        current["parser_diagnostics"] = {
            "native_text_shadow": {"status": "empty", "error_code": None}
        }

        self._assert_invalid(
            current,
            label="v3_rejects_native_text",
            path=(),
        )
        without_root_shadow = copy.deepcopy(current)
        del without_root_shadow["native_text"]
        self._assert_invalid(
            without_root_shadow,
            label="v3_rejects_native_text_diagnostic",
            path=("parser_diagnostics",),
        )

    def test_runtime_contract_rejects_invalid_element_identities(self) -> None:
        for label, mutate in (
            (
                "negative_order",
                lambda value: value["elements"][0].update({"order_index": -1}),
            ),
            (
                "boolean_source_index",
                lambda value: value["elements"][0].update(
                    {"source_item_index": True}
                ),
            ),
            (
                "duplicate_ir_id",
                lambda value: value["elements"][1].update(
                    {"ir_id": value["elements"][0]["ir_id"]}
                ),
            ),
        ):
            with self.subTest(label=label):
                payload = _load_fixture("annual_report_excerpt")
                mutate(payload)
                with self.assertRaises(NormalizedIRVersionError):
                    validate_normalized_ir_contract(payload)

    def test_optional_native_text_shadow_validates_strict_shape(self) -> None:
        data = _load_fixture("ir_activity")
        data["native_text"] = {
            "status": "ok",
            "extractor": {"name": "pdfplumber", "version": "0.11.10"},
            "content_hash": "sha256:" + "0" * 64,
            "non_whitespace_chars": 8,
            "pages": [
                {
                    "page_no": 1,
                    "text": "一、经营情况",
                    "non_whitespace_chars": 8,
                }
            ],
        }
        self._assert_valid(data, label="native_text_shadow")

        data["native_text"]["unexpected"] = True
        self._assert_invalid(
            data,
            label="native_text_shadow_extra_field",
            path=("native_text",),
        )

    def test_table_reconciliation_diagnostics_and_model_provenance_validate(
        self,
    ) -> None:
        data = _load_fixture("annual_report_excerpt")
        data["contract_version"] = "normalized_ir.v3"
        data["parser_artifacts"]["model_relpath"] = (
            "parser_artifacts/sample/sample_model.json"
        )
        data["parser_diagnostics"] = {
            "table_reconciliation": TableReconciliationStats(
                model_status="supported",
                content_tables=2,
                model_hash="sha256:" + "a" * 64,
                model_tables=2,
                uniquely_matched_tables=2,
                candidate_groups=1,
                proven_groups=1,
                locator_only_groups=1,
                locator_only_tables=2,
                located_groups=1,
                located_tables=2,
            ).as_dict()
        }
        table = _table_elements(data)[0]
        table.update(
            {
                "page_span": [1, 2],
                "page_bboxes": [
                    {"page_no": 1, "bbox": [100, 700, 900, 900]},
                    {"page_no": 2, "bbox": [100, 100, 900, 300]},
                ],
                "model_table_indices": [0, 1],
                "continuation_source_item_indices": [table["source_item_index"] + 1],
                "table_locator_algorithm": "mineru-aggregate-table-locator.v4",
            }
        )
        self._assert_valid(data, label="table_reconciliation")

        # normalized_ir.v2 already published restore.v3 diagnostics before
        # locator-only v4. Keep that structural shape schema-readable even
        # though BuildUnits classifies it as requiring a fresh parse.
        legacy = copy.deepcopy(data)
        legacy["contract_version"] = "normalized_ir.v2"
        legacy["parser_diagnostics"]["table_reconciliation"] = {
            "algorithm_version": "mineru-aggregate-table-restore.v3",
            "table_builder_semantics_version": "table-builder-semantics.v2",
            "model_status": "supported",
            "model_hash": "sha256:" + "a" * 64,
            "content_tables": 2,
            "model_tables": 2,
            "uniquely_matched_tables": 2,
            "ambiguous_matches": 0,
            "candidate_groups": 1,
            "proven_groups": 1,
            "unproven_groups": 0,
            "restoration_rejected_groups": 1,
            "unresolved_groups": 1,
            "located_groups": 1,
            "located_tables": 2,
            "restored_groups": 0,
            "restored_tables": 0,
        }
        legacy_table = _table_elements(legacy)[0]
        legacy_table["table_locator_algorithm"] = (
            "mineru-aggregate-table-restore.v3"
        )
        self._assert_valid(legacy, label="legacy_restore_v3")

        element_index = data["elements"].index(table)
        for field in (
            "page_span",
            "page_bboxes",
            "model_table_indices",
            "continuation_source_item_indices",
            "table_locator_algorithm",
        ):
            with self.subTest(missing_locator_field=field):
                partial = copy.deepcopy(data)
                del partial["elements"][element_index][field]
                self._assert_invalid(
                    partial,
                    label=f"table_locator_missing_{field}",
                    path=("elements", element_index),
                )

        wrong_kind = copy.deepcopy(data)
        wrong_kind["elements"][element_index]["kind"] = "text"
        self._assert_invalid(
            wrong_kind,
            label="table_locator_wrong_kind",
            path=("elements", element_index, "kind"),
        )

        for field, duplicate in (
            ("page_span", [1, 1]),
            (
                "page_bboxes",
                [
                    {"page_no": 1, "bbox": [100, 700, 900, 900]},
                    {"page_no": 1, "bbox": [100, 700, 900, 900]},
                ],
            ),
            ("model_table_indices", [0, 0]),
            ("continuation_source_item_indices", [2, 2]),
        ):
            with self.subTest(duplicate_locator_field=field):
                duplicated = copy.deepcopy(data)
                duplicated["elements"][element_index][field] = duplicate
                self._assert_invalid(
                    duplicated,
                    label=f"table_locator_duplicate_{field}",
                    path=("elements", element_index, field),
                )

        del data["parser_diagnostics"]["table_reconciliation"][
            "located_tables"
        ]
        self._assert_invalid(
            data,
            label="table_reconciliation_missing_counter",
            path=("parser_diagnostics", "table_reconciliation"),
        )

    def test_non_supported_reconciliation_diagnostics_require_zero_counters(
        self,
    ) -> None:
        data = _load_fixture("annual_report_excerpt")
        data["contract_version"] = "normalized_ir.v3"
        stats = TableReconciliationStats(
            model_status="absent", content_tables=2
        ).as_dict()
        stats["restored_groups"] = 99
        data["parser_diagnostics"] = {"table_reconciliation": stats}
        self._assert_invalid(
            data,
            label="absent_model_nonzero_counter",
            path=(
                "parser_diagnostics",
                "table_reconciliation",
                "restored_groups",
            ),
        )

    def test_runtime_reconciliation_validator_enforces_cross_field_formulas(
        self,
    ) -> None:
        valid = TableReconciliationStats(
            model_status="supported",
            content_tables=2,
            model_hash="sha256:" + "a" * 64,
            model_tables=2,
            uniquely_matched_tables=2,
            candidate_groups=1,
            proven_groups=1,
            locator_only_groups=1,
            locator_only_tables=2,
            located_groups=1,
            located_tables=2,
        ).as_dict()
        validate_table_reconciliation_diagnostics(valid)

        for field, invalid_value in (
            ("algorithm_version", 3),
            ("unresolved_groups", 1),
            ("restored_groups", 1),
            ("uniquely_matched_tables", 1),
        ):
            with self.subTest(field=field):
                invalid = copy.deepcopy(valid)
                invalid[field] = invalid_value
                with self.assertRaises(ValueError):
                    validate_table_reconciliation_diagnostics(invalid)

        shared_model = {
            **TableReconciliationStats(
                model_status="supported",
                content_tables=2,
                model_hash="sha256:" + "b" * 64,
                model_tables=1,
            ).as_dict(),
            "ambiguous_matches": 2,
        }
        validate_table_reconciliation_diagnostics(shared_model)
        for model_tables, ambiguous_matches in ((0, 1), (1, 1)):
            invalid = {
                **shared_model,
                "model_tables": model_tables,
                "ambiguous_matches": ambiguous_matches,
            }
            with self.assertRaises(ValueError):
                validate_table_reconciliation_diagnostics(invalid)

    def test_runtime_reconciliation_validator_binds_locators_to_elements(
        self,
    ) -> None:
        algorithm = "mineru-aggregate-table-locator.v4"
        stats = TableReconciliationStats(
            model_status="supported",
            content_tables=2,
            model_hash="sha256:" + "c" * 64,
            model_tables=2,
            uniquely_matched_tables=2,
            candidate_groups=1,
            proven_groups=1,
            locator_only_groups=1,
            locator_only_tables=2,
            located_groups=1,
            located_tables=2,
        ).as_dict()
        payload = {
            "elements": [
                {
                    "kind": "table",
                    "raw_kind": "table",
                    "source_item_index": 0,
                    "page_no": 1,
                    "bbox": [0, 0, 10, 10],
                    "table_html": "<table><tr><td>A</td></tr></table>",
                    "table": {"headers": [], "rows": [["A"]]},
                    "page_span": [1, 2],
                    "page_bboxes": [
                        {"page_no": 1, "bbox": [0, 0, 10, 10]},
                        {"page_no": 2, "bbox": [0, 0, 10, 10]},
                    ],
                    "model_table_indices": [0, 1],
                    "continuation_source_item_indices": [1],
                    "table_locator_algorithm": algorithm,
                },
                {
                    "kind": "table",
                    "raw_kind": "table",
                    "source_item_index": 1,
                    "page_no": 2,
                    "bbox": [0, 0, 10, 10],
                    "table_html": "",
                    "table": {"headers": [], "rows": []},
                },
            ],
            "parser_diagnostics": {"table_reconciliation": stats},
        }
        validate_table_reconciliation_payload(payload)

        variants = {
            "missing_diagnostics": {key: value for key, value in payload.items() if key != "parser_diagnostics"},
            "null_diagnostics": {**payload, "parser_diagnostics": None},
            "null_reconciliation": {
                **payload,
                "parser_diagnostics": {"table_reconciliation": None},
            },
            "content_count": {
                **payload,
                "parser_diagnostics": {
                    "table_reconciliation": {**stats, "content_tables": 3}
                },
            },
            "model_index_out_of_range": {
                **payload,
                "elements": [
                    {**payload["elements"][0], "model_table_indices": [0, 2]},
                    payload["elements"][1],
                ],
            },
            "continuation_not_empty_table": {
                **payload,
                "elements": [
                    payload["elements"][0],
                    {**payload["elements"][1], "kind": "text"},
                ],
            },
            "boolean_root_page": {
                **payload,
                "elements": [
                    {**payload["elements"][0], "page_no": True},
                    payload["elements"][1],
                ],
            },
            "non_string_root_html": {
                **payload,
                "elements": [
                    {**payload["elements"][0], "table_html": 7},
                    payload["elements"][1],
                ],
            },
            "missing_root_grid": {
                **payload,
                "elements": [
                    {
                        key: value
                        for key, value in payload["elements"][0].items()
                        if key != "table"
                    },
                    payload["elements"][1],
                ],
            },
            "nonempty_ghost_grid": {
                **payload,
                "elements": [
                    payload["elements"][0],
                    {
                        **payload["elements"][1],
                        "table": {"headers": [], "rows": [["ghost"]]},
                    },
                ],
            },
        }
        for label, invalid in variants.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                validate_table_reconciliation_payload(invalid)

    def test_native_text_shadow_diagnostic_validates(self) -> None:
        data = _load_fixture("short_announcement")
        data["parser_diagnostics"] = {
            "native_text_shadow": {
                "status": "unavailable",
                "error_code": "pdf_parse_error",
            }
        }
        self._assert_valid(data, label="native_text_shadow_unavailable")

        invalid_type = copy.deepcopy(data)
        invalid_type["parser_diagnostics"]["native_text_shadow"]["error_code"] = 42
        self._assert_invalid(
            invalid_type,
            label="native_text_shadow_bad_error_code",
            path=("parser_diagnostics", "native_text_shadow", "error_code"),
        )

        unavailable_without_error = copy.deepcopy(data)
        unavailable_without_error["parser_diagnostics"]["native_text_shadow"][
            "error_code"
        ] = None
        self._assert_invalid(
            unavailable_without_error,
            label="native_text_shadow_unavailable_requires_error",
            path=("parser_diagnostics", "native_text_shadow"),
        )

        for status in ("ok", "empty"):
            available = copy.deepcopy(data)
            available["parser_diagnostics"]["native_text_shadow"] = {
                "status": status,
                "error_code": None,
            }
            self._assert_valid(available, label=f"native_text_shadow_{status}")

            available["parser_diagnostics"]["native_text_shadow"][
                "error_code"
            ] = "unexpected_error"
            self._assert_invalid(
                available,
                label=f"native_text_shadow_{status}_rejects_error",
                path=("parser_diagnostics", "native_text_shadow"),
            )

    def test_optional_full_annual_fixture_validates_when_present(self) -> None:
        path = PHASE00_ROOT / "annual_report" / "normalized_ir.v2.json"
        if not path.is_file():
            self.skipTest("optional full annual_report normalized_ir fixture is absent")
        data = json.loads(path.read_text(encoding="utf-8"))
        self._assert_valid(data, label="annual_report")

    def test_parser_artifacts_reject_extra_absolute_paths(self) -> None:
        data = json.loads(
            (
                PHASE00_ROOT / "short_announcement" / "normalized_ir.v2.json"
            ).read_text(encoding="utf-8")
        )
        data["parser_artifacts"]["legacy_root"] = "/Volumes/AgentSSD/leak"

        self._assert_invalid(
            data,
            label="parser_artifacts_extra_absolute_path",
            path=("parser_artifacts", "legacy_root"),
        )

    def test_mapper_accepts_real_phase00_mineru_content_list_when_available(self) -> None:
        content_list_path = _content_list_from_ref("short_announcement")
        if content_list_path is None or not content_list_path.is_file():
            self.skipTest("local Phase 00 MinerU content_list artifact is absent")

        reader = MinerUArtifactReader()
        content_list = reader.read_content_list(content_list_path)
        normalized = MinerUToNormalizedIRMapper().map_content_list(
            content_list=content_list,
            parser_info=MinerUParserInfo(
                name="MinerU",
                package_version="3.4.0",
                backend="pipeline",
                method="auto",
                language="ch",
                formula=False,
                table=True,
            ),
            document_metadata={
                "document_id": "phase04_real_mapper_smoke",
                "source_pdf": "tmp/sample_filings/real.pdf",
                "title": "real mapper smoke",
            },
            parser_artifacts={
                "artifact_root_relpath": "parser_artifacts/short",
                "content_list_relpath": "parser_artifacts/short/content_list.json",
            },
        )
        self._assert_valid(normalized, label="real_mapper_smoke")
        self.assertEqual(len(normalized["elements"]), len(content_list))
        self.assertEqual(normalized["elements"][0]["text"], content_list[0]["text"])
        self.assertIn("raw_kind", normalized["elements"][0])
        self.assertEqual(normalized["parsed_pages"]["start_page_no"], 1)
        self.assertGreaterEqual(normalized["parsed_pages"]["end_page_no"], 1)

    def test_mapper_synthetic_output_validates_against_schema(self) -> None:
        normalized = MinerUToNormalizedIRMapper().map_content_list(
            content_list=[
                {"type": "text", "text": "正文", "page_idx": 0},
                {
                    "type": "table",
                    "page_idx": 0,
                    "table_body": (
                        "<table><tr><th>项目</th><th>金额</th></tr>"
                        "<tr><td>收入</td><td>10</td></tr></table>"
                    ),
                },
                {
                    "type": "list",
                    "list_items": ["1、第一项", "2、第二项"],
                    "page_idx": 0,
                },
            ],
            parser_info=MinerUParserInfo(
                name="MinerU",
                package_version="3.4.0",
                backend="pipeline",
                method="auto",
                language="ch",
                formula=False,
                table=True,
            ),
            document_metadata={
                "document_id": "phase04_synthetic_mapper",
                "source_pdf": "raw_documents/local/sample.pdf",
                "title": "synthetic mapper",
            },
            parser_artifacts={
                "artifact_root_relpath": "parser_artifacts/local/sample",
                "content_list_relpath": (
                    "parser_artifacts/local/sample/sample_content_list.json"
                ),
            },
        )
        self._assert_valid(normalized, label="synthetic_mapper")
        self.assertEqual(normalized["contract_version"], "normalized_ir.v3")
        self.assertEqual(normalized["elements"][0]["raw_kind"], "text")
        self.assertEqual(normalized["elements"][1]["kind"], "table")
        self.assertEqual(
            normalized["elements"][1]["table"],
            {"headers": ["项目", "金额"], "rows": [["收入", "10"]]},
        )
        self.assertEqual(normalized["elements"][2]["kind"], "text")
        self.assertEqual(normalized["elements"][2]["raw_kind"], "list")
        self.assertEqual(normalized["elements"][2]["text"], "1、第一项\n2、第二项")

    def test_ir_activity_first_table_preserves_embedded_qa_text(self) -> None:
        data = _load_fixture("ir_activity")
        first_table = min(_table_elements(data), key=lambda item: item["order_index"])
        rows = first_table["table"]["rows"]

        self.assertGreater(len(rows), 0)
        joined_cells = "".join(cell for row in rows for cell in row)
        self.assertIn("？", joined_cells)

    def test_annual_report_excerpt_has_one_structured_table(self) -> None:
        data = _load_fixture("annual_report_excerpt")
        tables = _table_elements(data)

        self.assertEqual(len(tables), 1)
        self._assert_table_structured_or_failed(
            tables[0], label="annual_excerpt", require_content=True
        )

    def test_optional_annual_report_tables_are_structured_when_present(self) -> None:
        path = PHASE00_ROOT / "annual_report" / "normalized_ir.v2.json"
        if not path.is_file():
            self.skipTest("optional full annual_report normalized_ir fixture is absent")
        data = json.loads(path.read_text(encoding="utf-8"))
        tables = _table_elements(data)
        if not tables:
            self.skipTest("optional full annual_report fixture has no table elements")

        unfailed = [
            table for table in tables if not table.get("table_parse_failed", False)
        ]
        self.assertGreaterEqual(len(unfailed) / len(tables), 0.95)
        for index, table in enumerate(unfailed):
            self._assert_table_structured_or_failed(
                table, label=f"annual_report_table_{index}", require_content=False
            )

    def _assert_table_structured_or_failed(
        self, element: dict, *, label: str, require_content: bool
    ) -> None:
        if element.get("table_parse_failed"):
            return
        table = element.get("table") or {}
        headers = table.get("headers") or []
        rows = table.get("rows") or []
        if require_content:
            # Headers carry <th> evidence only (MinerU emits td-only tables),
            # so content presence is asserted on rows; header promotion is a
            # 05 builder rule, not an IR fact.
            self.assertGreater(len(rows), 0, label)
        widths = {len(row) for row in rows}
        if headers:
            widths.add(len(headers))
        self.assertLessEqual(len(widths), 1, label)

    def test_v3_element_visual_fields_validate_and_reject_mistyped_values(self) -> None:
        data = _load_fixture("short_announcement")
        data["contract_version"] = "normalized_ir.v3"
        index = len(data["elements"])
        data["elements"].append(
            {
                "ir_id": "ir_visual_probe",
                "kind": "image",
                "raw_kind": "image",
                "order_index": index,
                "source_item_index": index,
                "image_path": "images/a.jpg",
                "image_caption": ["x"],
                "image_footnote": ["y"],
                "visual_subtype": "seal",
            }
        )
        self._assert_valid(data, label="v3_visual_fields")

        for field, invalid_value in (
            ("image_caption", "x"),
            ("visual_subtype", 7),
            ("image_path", 7),
        ):
            with self.subTest(field=field):
                mutated = copy.deepcopy(data)
                mutated["elements"][index][field] = invalid_value
                self._assert_invalid(
                    mutated,
                    label=f"v3_visual_{field}_mistyped",
                    path=("elements", index, field),
                )


if __name__ == "__main__":
    unittest.main()
