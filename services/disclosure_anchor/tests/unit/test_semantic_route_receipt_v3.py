from __future__ import annotations

from dataclasses import replace
import unittest

from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_ROUTE_RECEIPT_VERSION,
    SemanticRouteContractError,
    SemanticRouteReceiptRowV3,
    semantic_route_receipt_row_v3_from_payload,
    semantic_route_receipt_row_v3_to_payload,
    validate_semantic_route_receipt_rows_v3,
)
from tests.unit._semantic_routes import _fallback_receipt


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


class SemanticRouteReceiptV3Tests(unittest.TestCase):
    def test_pre_id_row_round_trips_without_asset_id(self) -> None:
        row = _row(1)
        payload = semantic_route_receipt_row_v3_to_payload(row)
        self.assertNotIn("asset_id", payload)
        self.assertEqual(semantic_route_receipt_row_v3_from_payload(payload), row)

    def test_unknown_asset_id_and_hash_drift_fail_closed(self) -> None:
        payload = semantic_route_receipt_row_v3_to_payload(_row(1))
        with self.assertRaisesRegex(SemanticRouteContractError, "closed"):
            semantic_route_receipt_row_v3_from_payload(
                {**payload, "asset_id": "du_01AAAAAAAAAAAAAAAAAAAAAAAA"}
            )
        with self.assertRaisesRegex(SemanticRouteContractError, "hash"):
            semantic_route_receipt_row_v3_from_payload(
                {**payload, "provider_locator_sha256": "sha256:UPPER"}
            )

    def test_rows_require_one_run_and_contiguous_unique_order(self) -> None:
        rows = (_row(1), _row(2))
        validate_semantic_route_receipt_rows_v3(rows, processing_run_id="run-1")
        with self.assertRaisesRegex(SemanticRouteContractError, "contiguous"):
            validate_semantic_route_receipt_rows_v3(
                (rows[0], replace(rows[1], unit_order_index=3)),
                processing_run_id="run-1",
            )
        with self.assertRaisesRegex(SemanticRouteContractError, "mix"):
            validate_semantic_route_receipt_rows_v3(
                (rows[0], replace(rows[1], processing_run_id="run-2")),
                processing_run_id="run-1",
            )
        with self.assertRaisesRegex(SemanticRouteContractError, "identity"):
            replace(rows[0], unit_order_index=True)

    def test_v3_requires_v2_route_semantics(self) -> None:
        with self.assertRaisesRegex(SemanticRouteContractError, "exact v2"):
            SemanticRouteReceiptRowV3(
                processing_run_id="run-1",
                unit_order_index=1,
                provider_locator_sha256=SHA_A,
                routed_draft_sha256=SHA_B,
                receipt=_fallback_receipt(0),
            )


def _row(index: int) -> SemanticRouteReceiptRowV3:
    receipt = replace(
        _fallback_receipt(index),
        contract_version=SEMANTIC_ROUTE_RECEIPT_VERSION,
        semantic_keys=(),
        evidence=(),
    )
    return SemanticRouteReceiptRowV3(
        processing_run_id="run-1",
        unit_order_index=index,
        provider_locator_sha256=SHA_A,
        routed_draft_sha256=(
            "sha256:" + f"{index:064x}"[-64:]
        ),
        receipt=receipt,
    )


if __name__ == "__main__":
    unittest.main()
