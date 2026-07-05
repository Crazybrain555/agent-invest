"""Filing API exported contract artifacts are generated from code."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from disclosure_anchor.cli.export_contracts import PUBLIC_MODELS, export_contracts


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_ROOT = REPO_ROOT / "contracts"
PUBLIC_MODELS_ROOT = CONTRACTS_ROOT / "public_models"
DERIVED = {"document_unit": {"asset_uri", "is_active_run"}}


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


if __name__ == "__main__":
    unittest.main()
