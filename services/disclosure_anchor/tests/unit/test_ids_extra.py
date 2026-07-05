"""Additional edge-case tests for internal ID helpers."""

import unittest

from disclosure_anchor.domain.ids import (
    is_internal_id,
    is_ulid,
    new_company_id,
    new_company_identifier_id,
    new_document_id,
    new_asset_id,
    new_id,
    new_outbox_event_id,
    new_processing_run_id,
    new_security_id,
    new_source_access_id,
    new_source_checkpoint_id,
    new_tracked_company_id,
    new_ulid,
)


class UlidTests(unittest.TestCase):
    def test_ulids_are_unique(self) -> None:
        values = {new_ulid() for _ in range(2000)}
        self.assertEqual(len(values), 2000)

    def test_is_ulid_rejects_lowercase_and_wrong_length(self) -> None:
        valid = new_ulid()
        self.assertFalse(is_ulid(valid.lower()))
        self.assertFalse(is_ulid(valid[:-1]))
        self.assertFalse(is_ulid(valid + "0"))

    def test_is_ulid_rejects_excluded_crockford_letters(self) -> None:
        # I, L, O, U are excluded from the Crockford alphabet.
        self.assertFalse(is_ulid("I" * 26))


class InternalIdTests(unittest.TestCase):
    def test_new_id_payload_is_a_valid_ulid(self) -> None:
        value = new_id("doc")
        prefix, _, payload = value.partition("_")
        self.assertEqual(prefix, "doc")
        self.assertTrue(is_ulid(payload))

    def test_new_id_rejects_bad_prefixes(self) -> None:
        for bad in ("", "Doc", "1doc", "../doc", "doc-1", "do c"):
            with self.assertRaises(ValueError):
                new_id(bad)

    def test_is_internal_id_negatives(self) -> None:
        self.assertFalse(is_internal_id(new_ulid()))  # missing prefix
        self.assertFalse(is_internal_id("doc_short"))
        self.assertFalse(is_internal_id("_" + new_ulid()))
        self.assertFalse(is_internal_id("doc_" + new_ulid().lower()))


class TypedIdHelperTests(unittest.TestCase):
    def test_each_helper_uses_its_prefix_and_a_valid_payload(self) -> None:
        cases = {
            "co": new_company_id,
            "ci": new_company_identifier_id,
            "sec": new_security_id,
            "tc": new_tracked_company_id,
            "sa": new_source_access_id,
            "sc": new_source_checkpoint_id,
            "doc": new_document_id,
            "run": new_processing_run_id,
            "du": new_asset_id,
            "oe": new_outbox_event_id,
        }
        for prefix, factory in cases.items():
            value = factory()
            self.assertTrue(is_internal_id(value), prefix)
            self.assertEqual(value.partition("_")[0], prefix)


if __name__ == "__main__":
    unittest.main()
