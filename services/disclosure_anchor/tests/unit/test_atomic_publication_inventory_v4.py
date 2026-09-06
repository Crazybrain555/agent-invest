from __future__ import annotations

from dataclasses import replace
import unittest

from disclosure_anchor.application.services.atomic_publication_inventory_v4 import (
    PreviousActiveUnitInventoryV4Error,
    previous_active_unit_inventory_v4,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.services.unit_hashing import compute_unit_hashes


class AtomicPublicationInventoryV4Tests(unittest.TestCase):
    def test_projects_the_exact_stored_query_basis_without_reordering(self) -> None:
        first = _unit(order_index=1, asset_suffix="0")
        second = _unit(order_index=2, asset_suffix="1")

        result = previous_active_unit_inventory_v4((first, second))

        self.assertEqual(
            tuple(item.asset_id for item in result), (first.asset_id, second.asset_id)
        )
        self.assertEqual(tuple(item.order_index for item in result), (1, 2))
        self.assertEqual(result[0].query_projection_hash, first.query_projection_hash)
        self.assertEqual(
            result[0].canonical_query_projection_json,
            (
                '{"applicability":"applicable","heading_path":[],"payload_kind":"text",'
                '"quality_status":"ok","section_keys":["section"],"semantic_key":null,'
                '"title":null}'
            ),
        )

    def test_rejects_stored_query_projection_drift(self) -> None:
        unit = replace(
            _unit(order_index=1, asset_suffix="0"),
            query_projection_hash="sha256:" + "0" * 64,
        )

        with self.assertRaisesRegex(
            PreviousActiveUnitInventoryV4Error,
            "projection drifted",
        ):
            previous_active_unit_inventory_v4((unit,))


def _unit(*, order_index: int, asset_suffix: str) -> e.DocumentUnit:
    payload = {"text": f"unit {order_index}"}
    hashes = compute_unit_hashes(
        payload_kind="text",
        payload=payload,
        title=None,
        heading_path=[],
        semantic_keys=None,
        section_keys=["section"],
        quality_status="ok",
        applicability="applicable",
        order_index=order_index,
    )
    return e.DocumentUnit(
        asset_id="du_01K0000000000000000000000" + asset_suffix,
        document_id="doc-1",
        processing_run_id="run-previous",
        provider_document_id="provider-1",
        payload_kind="text",
        order_index=order_index,
        payload=payload,
        content_hash=hashes.content_hash,
        structure_hash=hashes.structure_hash,
        quality_status="ok",
        applicability="applicable",
        page_no=1,
        query_projection_hash=hashes.query_projection_hash,
        section_keys=["section"],
    )


if __name__ == "__main__":
    unittest.main()
