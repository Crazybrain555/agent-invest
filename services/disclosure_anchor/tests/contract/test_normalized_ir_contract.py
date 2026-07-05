"""NormalizedIR v2 contract checks."""

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import MinerUArtifactReader
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
    MinerUToNormalizedIRMapper,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "contracts" / "normalized_ir" / "normalized_ir.v2.json"
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
    def _schema(self) -> dict:
        return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def _validator(self) -> Draft202012Validator:
        schema = self._schema()
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())

    def _assert_valid(self, payload: dict, *, label: str) -> None:
        errors = sorted(
            self._validator().iter_errors(payload),
            key=lambda error: list(error.path),
        )
        if errors:
            details = "\n".join(
                f"{label}:{'/'.join(map(str, error.path))}: {error.message}"
                for error in errors[:10]
            )
            self.fail(details)

    def _assert_invalid(self, payload: dict, *, label: str, path: tuple[str, ...]) -> None:
        errors = sorted(
            self._validator().iter_errors(payload),
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
        self.assertEqual(normalized["contract_version"], "normalized_ir.v2")
        self.assertEqual(normalized["elements"][0]["raw_kind"], "text")
        self.assertEqual(normalized["elements"][1]["kind"], "table")
        self.assertEqual(
            normalized["elements"][1]["table"],
            {"headers": ["项目", "金额"], "rows": [["收入", "10"]]},
        )

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


if __name__ == "__main__":
    unittest.main()
