import unittest

from asset_intake.application.register_dataset import compute_content_hash, compute_semantic_key
from asset_intake.providers.registry import load_dataset_entries

ENTRY = load_dataset_entries()["cn_equity.eod_quote"]

PARAMS = {
    "security": "000001.SZ",
    "start_date": "2026-07-01",
    "end_date": "2026-07-03",
    "price_adjustment": "raw_plus_factor",
}

ROW = {
    "security": "000001.SZ", "trade_date": "2026-07-03", "open": 10.29, "high": 10.4,
    "low": 10.18, "close": 10.29, "pre_close": 10.28, "volume": 86332664.0,
    "amount": 888789393.0, "adj_factor": 85.329579,
}


class HashingTests(unittest.TestCase):
    def test_semantic_key_is_stable_and_scoped(self) -> None:
        key = compute_semantic_key(ENTRY, PARAMS)
        self.assertIn("cn_equity.eod_quote", key)
        self.assertIn("security=000001.SZ", key)
        self.assertEqual(key, compute_semantic_key(ENTRY, dict(PARAMS)))

    def test_content_hash_ignores_record_order(self) -> None:
        row2 = {**ROW, "trade_date": "2026-07-02", "close": 10.28}
        self.assertEqual(
            compute_content_hash(ENTRY, [ROW, row2]),
            compute_content_hash(ENTRY, [row2, ROW]),
        )

    def test_content_hash_ignores_fields_outside_contract(self) -> None:
        noisy = {**ROW, "fetched_at": "2026-07-06T01:02:03"}
        self.assertEqual(compute_content_hash(ENTRY, [ROW]), compute_content_hash(ENTRY, [noisy]))

    def test_content_hash_changes_with_content(self) -> None:
        changed = {**ROW, "adj_factor": 86.0}
        self.assertNotEqual(compute_content_hash(ENTRY, [ROW]), compute_content_hash(ENTRY, [changed]))


if __name__ == "__main__":
    unittest.main()
