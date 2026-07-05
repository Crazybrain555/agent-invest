"""Contract checks for the Phase 00 golden fixtures.

These fixtures are the reusable parser-output baseline kept in-repo. They are
validated structurally so a parser/regeneration change that breaks the shape is
caught here instead of silently drifting. Per the fixture-and-test policy, the
fixtures are golden samples; this guards their structure, not market-wide quality.
"""

import json
import unittest
from pathlib import Path

from disclosure_anchor.domain.value_objects.common import ContentHash

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "phase00"

CLEAN_CHECKOUT_SAMPLE_KEYS = (
    "annual_report_excerpt",
    "ir_activity",
    "short_announcement",
)
OPTIONAL_LOCAL_SAMPLE_KEYS = ("annual_report",)

NORMALIZED_IR_REQUIRED_KEYS = {
    "contract_version",
    "created_at",
    "document_id",
    "elements",
    "parsed_pages",
    "parser",
    "parser_artifacts",
    "sample_key",
    "source_pdf",
    "title",
}

UNIT_REQUIRED_KEYS = {
    "artifact_locator",
    "content_hash",
    "document_id",
    "heading_path",
    "order_index",
    "payload",
    "quality_status",
    "semantic_key",
    "title",
    "asset_id",
    "payload_kind",
}

ALLOWED_PAYLOAD_KINDS = {"text", "table", "qa"}


def _is_relative_locator(value: str) -> bool:
    return not (
        value.startswith("/")
        or value.startswith("file:")
        or ".." in Path(value).parts
        or (len(value) > 2 and value[1] == ":" and value[2] in {"\\", "/"})
    )


def _read_jsonl(path: Path) -> list[dict]:
    units: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                units.append(json.loads(line))
            except json.JSONDecodeError as exc:  # pragma: no cover - failure detail
                raise AssertionError(f"{path}:{line_no} is not valid JSON: {exc}") from exc
    return units


@unittest.skipUnless(FIXTURE_ROOT.is_dir(), f"phase00 fixtures absent: {FIXTURE_ROOT}")
class Phase00FixtureContractTests(unittest.TestCase):
    def test_every_sample_has_the_expected_artifacts(self) -> None:
        for key in CLEAN_CHECKOUT_SAMPLE_KEYS:
            sample_dir = FIXTURE_ROOT / key
            self.assertTrue((sample_dir / "normalized_ir.v2.json").is_file(), key)
            self.assertTrue((sample_dir / "document_units.v1.jsonl").is_file(), key)
            self.assertTrue((sample_dir / "manual_review.md").is_file(), key)
            self.assertTrue((sample_dir / "parser_artifacts_ref.txt").is_file(), key)

    def test_normalized_ir_has_required_keys_and_matching_sample_key(self) -> None:
        for key in CLEAN_CHECKOUT_SAMPLE_KEYS:
            data = json.loads((FIXTURE_ROOT / key / "normalized_ir.v2.json").read_text("utf-8"))
            missing = NORMALIZED_IR_REQUIRED_KEYS - data.keys()
            self.assertFalse(missing, f"{key} missing keys: {sorted(missing)}")
            self.assertEqual(data["sample_key"], key)
            self.assertIsInstance(data["elements"], list)
            self.assertGreater(len(data["elements"]), 0, key)

    def test_document_units_are_well_formed(self) -> None:
        for key in CLEAN_CHECKOUT_SAMPLE_KEYS:
            ir = json.loads((FIXTURE_ROOT / key / "normalized_ir.v2.json").read_text("utf-8"))
            units = _read_jsonl(FIXTURE_ROOT / key / "document_units.v1.jsonl")
            self.assertGreater(len(units), 0, key)

            seen_asset_ids: set[str] = set()
            last_order = 0
            for unit in units:
                missing = UNIT_REQUIRED_KEYS - unit.keys()
                self.assertFalse(missing, f"{key} unit missing keys: {sorted(missing)}")

                # document_id is consistent with the normalized IR header.
                self.assertEqual(unit["document_id"], ir["document_id"], key)

                # asset_id is non-empty and unique within the document.
                asset_id = unit["asset_id"]
                self.assertTrue(asset_id)
                self.assertNotIn(asset_id, seen_asset_ids, f"duplicate asset_id {asset_id}")
                seen_asset_ids.add(asset_id)

                self.assertIn(unit["payload_kind"], ALLOWED_PAYLOAD_KINDS, key)
                self.assertIsInstance(unit["heading_path"], list)
                self.assertIsInstance(unit["payload"], dict)
                artifact_path = (unit["artifact_locator"] or {}).get("artifact_path")
                if artifact_path is not None:
                    self.assertTrue(
                        _is_relative_locator(artifact_path),
                        f"{key} has absolute artifact path: {artifact_path}",
                    )

                # content_hash parses through the domain value object (sha256 + hex).
                content_hash = ContentHash.parse(unit["content_hash"])
                self.assertEqual(content_hash.algorithm, "sha256")
                self.assertRegex(content_hash.digest, r"^[a-f0-9]{64}$")

                # order_index is strictly increasing within the document.
                order_index = unit["order_index"]
                self.assertIsInstance(order_index, int)
                self.assertGreater(order_index, last_order, key)
                last_order = order_index

    def test_optional_full_annual_fixture_is_valid_when_present(self) -> None:
        for key in OPTIONAL_LOCAL_SAMPLE_KEYS:
            sample_dir = FIXTURE_ROOT / key
            if not (sample_dir / "normalized_ir.v2.json").is_file():
                self.skipTest(f"optional local fixture absent: {key}")
            self.assertTrue((sample_dir / "document_units.v1.jsonl").is_file(), key)
            self.assertTrue((sample_dir / "manual_review.md").is_file(), key)


if __name__ == "__main__":
    unittest.main()
