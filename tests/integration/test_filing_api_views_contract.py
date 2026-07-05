"""DB-gated public view columns match exported Filing API schemas."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from sqlalchemy import text

from tests.integration._support import engine_or_skip


REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_MODELS_ROOT = REPO_ROOT / "contracts" / "public_models"
DERIVED = {"document_unit": {"asset_uri", "is_active_run"}}
VIEW_BY_MODEL = {
    "document": "documents_v1",
    "document_unit": "document_units_v1",
    "processing_run": "processing_runs_v1",
    "source_ref": "source_refs_v1",
    "change_event": "change_events_v1",
}


class FilingApiViewContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_public_view_columns_match_exported_schema_minus_derived_fields(self) -> None:
        with self.engine.connect() as conn:
            for model_name, view_name in VIEW_BY_MODEL.items():
                schema = json.loads(
                    (PUBLIC_MODELS_ROOT / f"{model_name}.v1.json").read_text(
                        encoding="utf-8"
                    )
                )
                schema_fields = set(schema["properties"]) - DERIVED.get(model_name, set())
                columns = {
                    row.column_name
                    for row in conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'disclosure_public' "
                            "AND table_name = :view_name"
                        ),
                        {"view_name": view_name},
                    )
                }
                self.assertEqual(columns, schema_fields, model_name)


if __name__ == "__main__":
    unittest.main()
