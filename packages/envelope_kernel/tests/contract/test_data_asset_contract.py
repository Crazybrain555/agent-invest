import unittest
from datetime import UTC, datetime

from envelope_kernel.contracts import (
    data_asset_json_schema,
    default_contract_path,
    render_contract,
    validate_envelope,
)


class DataAssetContractTests(unittest.TestCase):
    def test_committed_contract_matches_export_byte_for_byte(self) -> None:
        path = default_contract_path()
        self.assertTrue(path.is_file(), f"missing contract artifact: {path} (run make export-contracts)")
        self.assertEqual(path.read_text(encoding="utf-8"), render_contract())

    def test_schema_declares_minimal_core_required(self) -> None:
        schema = data_asset_json_schema()
        self.assertEqual(schema["$id"], "data_asset.v1")
        self.assertEqual(
            sorted(schema["required"]),
            ["asset_id", "asset_kind", "observed_at", "source_ref", "source_tier", "trace_level"],
        )
        self.assertFalse(schema["additionalProperties"])

    def test_schema_is_valid_json_schema(self) -> None:
        import jsonschema

        jsonschema.Draft202012Validator.check_schema(data_asset_json_schema())

    def test_validate_envelope_entry_point(self) -> None:
        asset = validate_envelope(
            {
                "asset_id": "da_0002",
                "asset_kind": "tool_result",
                "payload_kind": "search_result",
                "observed_at": datetime(2026, 7, 5, tzinfo=UTC),
                "source_ref": "sa_0002",
                "source_tier": "tier_2",
                "trace_level": "G2",
                "payload": {"items": [{"url": "https://example.com", "title": "t"}]},
            }
        )
        self.assertEqual(asset.asset_kind, "tool_result")


if __name__ == "__main__":
    unittest.main()
