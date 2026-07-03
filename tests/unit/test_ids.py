import unittest

from disclosure_anchor.domain.ids import (
    is_internal_id,
    is_ulid,
    new_document_id,
    new_asset_id,
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
            new_document_id(),
            new_processing_run_id(),
            new_asset_id(),
        ):
            self.assertTrue(is_internal_id(str(value)))

    def test_invalid_prefix_rejected(self) -> None:
        with self.assertRaises(ValueError):
            new_id("../doc")


if __name__ == "__main__":
    unittest.main()
