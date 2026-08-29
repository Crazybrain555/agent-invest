"""Operational capacity schemas are closed, generated and Draft 2020-12 valid."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from jsonschema.validators import validator_for

from disclosure_anchor.application.contracts.capacity import (
    operational_schema_documents,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    operational_telemetry_schema_documents,
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
            if "additionalProperties" in tracked:
                self.assertFalse(tracked["additionalProperties"])
            else:
                object_definitions = [
                    value
                    for value in tracked.get("$defs", {}).values()
                    if value.get("type") == "object"
                ]
                self.assertTrue(object_definitions)
                self.assertTrue(
                    all(
                        value.get("additionalProperties") is False
                        for value in object_definitions
                    )
                )

    def test_operational_schema_registry_has_only_observation_v1(self) -> None:
        self.assertEqual(
            set(operational_schema_documents()),
            {
                "capacity-observation-interval.v1.schema.json",
                "capacity-observation-run.v1.schema.json",
            },
        )

    def test_synchronized_telemetry_schemas_are_closed_and_byte_exact(self) -> None:
        generated_documents = operational_telemetry_schema_documents()
        self.assertEqual(
            set(generated_documents),
            {
                "capacity-progress-event.v1.schema.json",
                "capacity-vector-credit-event.v1.schema.json",
                "phase-clock-binding.v1.schema.json",
                "synchronized-phase-summary.v1.schema.json",
                "synchronized-telemetry-frame.v1.schema.json",
                "synchronized-telemetry-receipt.v1.schema.json",
                "synchronized-telemetry-frame.v2.schema.json",
                "synchronized-telemetry-receipt.v2.schema.json",
                "synchronized-telemetry-seal.v2.schema.json",
            },
        )
        for filename, generated in generated_documents.items():
            tracked_path = CONTRACTS_ROOT / filename
            tracked = json.loads(tracked_path.read_text(encoding="utf-8"))
            self.assertEqual(tracked, generated)
            validator_for(tracked).check_schema(tracked)
            self.assertTrue(str(tracked["$id"]).endswith(filename))
            if "additionalProperties" in tracked:
                self.assertFalse(tracked["additionalProperties"])
            else:
                object_definitions = [
                    value
                    for value in tracked.get("$defs", {}).values()
                    if value.get("type") == "object"
                ]
                self.assertTrue(object_definitions)
                self.assertTrue(
                    all(
                        value.get("additionalProperties") is False
                        for value in object_definitions
                    )
                )


if __name__ == "__main__":
    unittest.main()
