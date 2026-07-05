import unittest

from asset_intake.contracts import ARTIFACTS, CONTRACTS_DIR, PUBLIC_VIEW_COLUMNS


class IntakeContractTests(unittest.TestCase):
    def test_committed_artifacts_match_export_byte_for_byte(self) -> None:
        for name, render in ARTIFACTS.items():
            path = CONTRACTS_DIR / name
            self.assertTrue(path.is_file(), f"missing {path} (run make export-contracts)")
            self.assertEqual(path.read_text(encoding="utf-8"), render(), name)

    def test_registry_schemas_are_valid_json_schema(self) -> None:
        import json

        import jsonschema

        for name in ("dataset_registry.v1.schema.json", "provider_catalog.v1.schema.json"):
            schema = json.loads((CONTRACTS_DIR / name).read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator.check_schema(schema)

    def test_views_contract_covers_three_views_and_four_error_codes(self) -> None:
        import json

        data = json.loads((CONTRACTS_DIR / "intake_views.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(data["views"]), sorted(PUBLIC_VIEW_COLUMNS))
        self.assertEqual(
            sorted(data["error_codes"]),
            ["CONTRACT_VERSION_MISMATCH", "GONE_SUPERSEDED", "L1_PROCESSING_REQUIRED", "NOT_FOUND"],
        )


if __name__ == "__main__":
    unittest.main()
