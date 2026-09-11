from __future__ import annotations

import json
from pathlib import Path
import unittest

from jsonschema.validators import validator_for

from disclosure_anchor.application.contracts.m6_schemas import operational_m6_schema_documents
from disclosure_anchor.application.contracts.synchronized_telemetry import operational_schema_documents


class M6RunJsonSchemaTests(unittest.TestCase):
    def test_closed_schemas_are_valid_exact_exports_and_in_complete_registry(self):
        schemas = operational_m6_schema_documents()
        self.assertEqual(set(schemas), {
            "m6-campaign-scope.v1.schema.json", "m6-corpus-manifest.v1.schema.json",
            "m6-quality-plan.v1.schema.json", "m6-qualification-evidence.v1.schema.json",
            "m6-document-qualification.v1.schema.json", "m6-source-history-fact.v1.schema.json",
            "m6-run-spec.v1.schema.json", "m6-producer-event.v1.schema.json",
            "m6-run-event.v1.schema.json", "m6-run-receipt.v1.schema.json",
        })
        root = Path(__file__).resolve().parents[2] / "contracts" / "operational"
        all_schemas = operational_schema_documents()
        for name, schema in schemas.items():
            with self.subTest(name=name):
                validator_for(schema).check_schema(schema)
                self.assertEqual(all_schemas[name], schema)
                self.assertEqual((root / name).read_text(), json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2)+"\n")
                for definition in (schema, *schema.get("$defs", {}).values()):
                    if definition.get("type") == "object":
                        self.assertFalse(definition["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
