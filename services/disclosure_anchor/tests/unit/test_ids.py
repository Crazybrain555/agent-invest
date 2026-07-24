import unittest

from disclosure_anchor.domain import ids
from disclosure_anchor.domain.ids import (
    is_internal_id,
    is_ulid,
    new_document_id,
    new_asset_id,
    new_company_identifier_id,
    new_id,
    new_processing_run_id,
    new_source_access_id,
    new_ulid,
)


class IdTests(unittest.TestCase):
    def test_new_ulid_shape(self) -> None:
        value = new_ulid()
        self.assertEqual(len(value), 26)
        self.assertTrue(is_ulid(value))

    def test_prefixed_ids_are_internal_ids(self) -> None:
        for value in (
            new_source_access_id(),
            new_company_identifier_id(),
            new_document_id(),
            new_processing_run_id(),
            new_asset_id(),
        ):
            self.assertTrue(is_internal_id(str(value)))

    def test_invalid_prefix_rejected(self) -> None:
        with self.assertRaises(ValueError):
            new_id("../doc")


class IdTimeFloorTests(unittest.TestCase):
    def test_floor_sorts_below_recent_ids_and_preserves_prefix(self) -> None:
        base = ids.new_asset_id()
        floor = ids.id_time_floor(base, backoff_ms=60_000)
        self.assertTrue(floor.startswith("du_"))
        self.assertTrue(ids.is_internal_id(floor))
        # The floor must sort at-or-below the id it was derived from, and
        # below any id minted afterwards (ULIDs are time-prefixed).
        self.assertLessEqual(floor, base)
        self.assertLessEqual(floor, ids.new_asset_id())

    def test_zero_backoff_is_the_timestamp_lower_bound(self) -> None:
        base = ids.new_asset_id()
        floor = ids.id_time_floor(base, backoff_ms=0)
        self.assertLessEqual(floor, base)
        # Same timestamp, minimum randomness: identical 10-char time prefix.
        self.assertEqual(floor[3:13], base[3:13])
        self.assertEqual(floor[13:], "0" * 16)

    def test_backoff_clamps_at_epoch_and_rejects_bad_input(self) -> None:
        base = ids.new_asset_id()
        clamped = ids.id_time_floor(base, backoff_ms=10**18)
        self.assertEqual(clamped, "du_" + "0" * 26)
        with self.assertRaises(ValueError):
            ids.id_time_floor("not-an-id", backoff_ms=1)
        with self.assertRaises(ValueError):
            ids.id_time_floor(base, backoff_ms=-1)


if __name__ == "__main__":
    unittest.main()
