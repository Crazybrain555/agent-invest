"""Operational capacity schemas are closed, generated and Draft 2020-12 valid."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from jsonschema.validators import validator_for

from disclosure_anchor.application.contracts.capacity import (
    operational_schema_documents,
)


CONTRACTS_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "operational"


class CapacityJsonSchemaTests(unittest.TestCase):
    def test_operational_schemas_are_valid_closed_and_byte_exact(self) -> None:
        for filename, generated in operational_schema_documents().items():
            tracked_path = CONTRACTS_ROOT / filename
            tracked = json.loads(tracked_path.read_text(encoding="utf-8"))
            self.assertEqual(tracked, generated)
            validator_for(tracked).check_schema(tracked)
            self.assertTrue(str(tracked["$id"]).endswith(filename))
            self.assertFalse(tracked["additionalProperties"])

    def test_operational_schema_registry_has_only_observation_v1(self) -> None:
        self.assertEqual(
            set(operational_schema_documents()),
            {
                "capacity-observation-interval.v1.schema.json",
                "capacity-observation-run.v1.schema.json",
            },
        )


if __name__ == "__main__":
    unittest.main()
