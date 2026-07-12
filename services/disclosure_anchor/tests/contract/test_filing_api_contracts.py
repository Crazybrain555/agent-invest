"""Filing API exported contract artifacts are generated from code."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from disclosure_anchor.cli.export_contracts import PUBLIC_MODELS, export_contracts


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_ROOT = REPO_ROOT / "contracts"
PUBLIC_MODELS_ROOT = CONTRACTS_ROOT / "public_models"
DERIVED = {
    "document_unit": {"asset_uri", "is_active_run"},
    "tracked_company": {
        "effective_lookback_days",
        "effective_sync_seconds",
        "effective_process_classes",
        "sync_state",
    },
}


class FilingApiContractsTests(unittest.TestCase):
    def test_exported_contracts_match_fresh_export_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "contracts"
            export_contracts(output_root)
            expected_paths = [
                CONTRACTS_ROOT / "filing_api.openapi.yaml",
                *(
                    PUBLIC_MODELS_ROOT / f"{name}.v1.json"
                    for name in sorted(PUBLIC_MODELS)
                ),
            ]
            for expected in expected_paths:
                actual = output_root / expected.relative_to(CONTRACTS_ROOT)
                self.assertEqual(
                    actual.read_bytes(),
                    expected.read_bytes(),
                    f"{expected.relative_to(REPO_ROOT)} drifted; rerun export_contracts",
                )

    def test_public_model_schemas_match_pydantic_model_fields(self) -> None:
        for name, model in PUBLIC_MODELS.items():
            schema = json.loads(
                (PUBLIC_MODELS_ROOT / f"{name}.v1.json").read_text(encoding="utf-8")
            )
            schema_fields = set(schema["properties"])
            model_fields = set(model.model_fields)
            self.assertEqual(schema_fields, model_fields, name)
            self.assertTrue(DERIVED.get(name, set()).issubset(schema_fields), name)

    def test_openapi_uses_public_error_contract_inputs(self) -> None:
        openapi = yaml.safe_load(
            (CONTRACTS_ROOT / "filing_api.openapi.yaml").read_text(encoding="utf-8")
        )
        schemas = openapi["components"]["schemas"]
        self.assertIn("ErrorEnvelope", schemas)
        self.assertNotIn("HTTPValidationError", schemas)
        self.assertNotIn("ValidationError", schemas)
        self.assertEqual(
            set(schemas["ErrorEnvelope"]["properties"]["error_code"]["enum"]),
            {
                "NOT_FOUND",
                "GONE_SUPERSEDED",
                "L1_PROCESSING_REQUIRED",
                "CONTRACT_VERSION_MISMATCH",
                "VALIDATION_ERROR",
            },
        )

        for path, operations in openapi["paths"].items():
            for operation in operations.values():
                if not isinstance(operation, dict):
                    continue
                parameters = operation.get("parameters", [])
                self.assertIn(
                    {"$ref": "#/components/parameters/XContractVersion"},
                    parameters,
                    path,
                )
                for response in operation.get("responses", {}).values():
                    encoded = json.dumps(response, sort_keys=True)
                    self.assertNotIn("HTTPValidationError", encoded)
                if "422" in operation.get("responses", {}):
                    self.assertEqual(
                        operation["responses"]["422"]["content"]["application/json"][
                            "schema"
                        ],
                        {"$ref": "#/components/schemas/ErrorEnvelope"},
                        path,
                    )

        for path in (
            "/v1/documents/{document_id}",
            "/v1/documents/{document_id}/units",
        ):
            parameters = openapi["paths"][path]["get"]["parameters"]
            self.assertTrue(
                any(param.get("name") == "reject_superseded" for param in parameters),
                path,
            )


if __name__ == "__main__":
    unittest.main()
