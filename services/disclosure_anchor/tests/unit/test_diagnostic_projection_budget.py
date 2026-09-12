"""Regression: reject unavailable projection capacity before deriving/expanding DTOs."""

from copy import deepcopy
from unittest.mock import patch
import unittest

from disclosure_anchor.application.contracts.diagnostic_json import bounded_json_bytes, require_projection_budget
from disclosure_anchor.application.services.source_semantic_build import encode_source_semantic_build
from disclosure_anchor.application.services.source_semantic_record import encode_source_semantic_record
from tests._source_semantic_build_fixture import build_from_literal, literal_build_record, refresh_literal_hashes
from tests._source_semantic_record_fixture import canonical, encoder_arguments, literal_record


class DiagnosticProjectionBudgetTests(unittest.TestCase):
    def test_256_units_tiny_invalid_budgets_reject_before_first_unit_or_locator_projection(self):
        record = literal_build_record()
        first = record["build"]["units"][0]
        units = []
        for index in range(256):
            unit = deepcopy(first)
            unit["unit_index"] = unit["locator"]["unit_index"] = index
            refresh_literal_hashes(unit)
            units.append(unit)
        build = build_from_literal({**record["build"], "units": units})
        for budget in (0, 1, 128, True, -1):
            with self.subTest(budget=budget), patch(
                "disclosure_anchor.application.services.source_semantic_build.source_unit_to_payload",
                side_effect=AssertionError("Unit projection must not run without capacity"),
            ) as project, patch(
                "disclosure_anchor.application.services.source_semantic_build.provider_unit_locator_to_payload",
                side_effect=AssertionError("Locator projection must not run without capacity"),
            ) as locator:
                with self.assertRaises(ValueError):
                    encode_source_semantic_build(semantic_record_sha256=record["semantic_record_sha256"], build=build,
                                                   quality_occurrences=(), maximum_bytes=budget)
                project.assert_not_called()
                locator.assert_not_called()

    def test_source_tiny_invalid_budgets_reject_before_semantic_derivation_or_document_projection(self):
        arguments = encoder_arguments(literal_record())
        for budget in (0, 1, 128, True, -1):
            with self.subTest(budget=budget), patch(
                "disclosure_anchor.application.services.source_semantic_record.derive_source_semantics",
                side_effect=AssertionError("Derivation must not run without capacity"),
            ) as derive, patch(
                "disclosure_anchor.application.services.source_semantic_record.provider_document_to_payload",
                side_effect=AssertionError("Document projection must not run without capacity"),
            ) as project:
                with self.assertRaises(ValueError):
                    encode_source_semantic_record(**arguments, maximum_bytes=budget)
                derive.assert_not_called()
                project.assert_not_called()
        expected = canonical(literal_record())
        self.assertEqual(encode_source_semantic_record(**arguments, maximum_bytes=len(expected)), expected)

    def test_preflight_accepts_exact_wire_capacity_for_empty_unicode_deep_and_wide_values(self):
        nested = 0
        for _ in range(64):
            nested = [nested]
        for value in ({}, [], [[], {}, []], "中€𐀀", ["中€"] * 512, {str(i): [] for i in range(512)}, nested):
            raw = canonical(value)
            with self.subTest(shape=type(value).__name__, size=len(raw)):
                require_projection_budget(value, maximum_bytes=len(raw))
                self.assertEqual(bounded_json_bytes(value, maximum_bytes=len(raw)), raw)
        with self.assertRaises(ValueError):
            require_projection_budget([nested], maximum_bytes=1024)

    def test_preflight_bounds_empty_container_inventory_and_utf8_scalar_before_expansion(self):
        for value in ([[]] * 10_000, {str(i): [] for i in range(10_000)}, "中" * 100_000):
            with self.subTest(shape=type(value).__name__), self.assertRaises(ValueError):
                require_projection_budget(value, maximum_bytes=128)
        # UTF-8 lower bound alone exceeds the budget; character count alone would fit.
        with self.assertRaises(ValueError):
            require_projection_budget("中€", maximum_bytes=7)
        require_projection_budget("中€", maximum_bytes=8)


if __name__ == "__main__":
    unittest.main()
