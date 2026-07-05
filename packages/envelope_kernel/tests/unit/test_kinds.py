import unittest

from envelope_kernel.kinds import (
    VALID_PAYLOAD_KINDS,
    AssetKind,
    PayloadKind,
    SourceTier,
    TraceLevel,
    is_valid_combination,
    validate_combination,
)


class KindMatrixTests(unittest.TestCase):
    def test_matrix_covers_every_asset_kind(self) -> None:
        self.assertEqual(set(VALID_PAYLOAD_KINDS), set(AssetKind))

    def test_matrix_partitions_every_payload_kind_exactly_once(self) -> None:
        seen: list[PayloadKind] = []
        for allowed in VALID_PAYLOAD_KINDS.values():
            seen.extend(allowed)
        self.assertEqual(sorted(seen), sorted(PayloadKind))
        self.assertEqual(len(seen), len(set(seen)))

    def test_protocol_2_2_matrix_exact(self) -> None:
        self.assertEqual(
            VALID_PAYLOAD_KINDS[AssetKind.DOCUMENT_UNIT],
            {PayloadKind.TEXT, PayloadKind.TABLE, PayloadKind.QA},
        )
        self.assertEqual(
            VALID_PAYLOAD_KINDS[AssetKind.DATASET_SNAPSHOT], {PayloadKind.RECORDSET}
        )
        self.assertEqual(
            VALID_PAYLOAD_KINDS[AssetKind.TOOL_RESULT],
            {PayloadKind.SEARCH_RESULT, PayloadKind.API_RESPONSE, PayloadKind.PAGE_SNIPPET},
        )
        self.assertEqual(
            VALID_PAYLOAD_KINDS[AssetKind.ARTIFACT_UNIT],
            {
                PayloadKind.CALCULATION_TABLE,
                PayloadKind.MODEL_TABLE,
                PayloadKind.CHECKLIST,
                PayloadKind.NOTE,
            },
        )

    def test_valid_and_invalid_combinations(self) -> None:
        self.assertTrue(is_valid_combination(AssetKind.DOCUMENT_UNIT, PayloadKind.TABLE))
        self.assertFalse(is_valid_combination(AssetKind.DATASET_SNAPSHOT, PayloadKind.TEXT))
        validate_combination(AssetKind.TOOL_RESULT, PayloadKind.API_RESPONSE)
        with self.assertRaises(ValueError):
            validate_combination(AssetKind.ARTIFACT_UNIT, PayloadKind.RECORDSET)


class ProvenanceEnumTests(unittest.TestCase):
    def test_source_tier_values_follow_blueprint_contract(self) -> None:
        self.assertEqual(
            [t.value for t in SourceTier],
            ["tier_0a", "tier_0b", "tier_1", "tier_2", "tier_3", "tier_f"],
        )

    def test_trace_levels(self) -> None:
        self.assertEqual([t.value for t in TraceLevel], ["G0", "G1", "G2", "G3", "G4"])


if __name__ == "__main__":
    unittest.main()
