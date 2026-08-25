"""Public research-watchlist source-bundle validation tests."""

from __future__ import annotations

import json
import unittest

from scripts.fetch_research_watchlist_inputs import _eastmoney_rows, _json_rows
from scripts.assemble_research_watchlist_screen import (
    _canonical_row_sha256,
    _exchange,
    _number,
    _scaled_cny,
)


class FetchResearchWatchlistInputsTests(unittest.TestCase):
    def test_screen_scalar_and_exchange_mapping_is_fail_closed(self) -> None:
        self.assertEqual(_number("12.5"), 12.5)
        self.assertIsNone(_number("nan"))
        self.assertEqual(_scaled_cny("1083630.121409"), 10_836_301_214.09)
        self.assertEqual(_exchange("920001", {"F004V": "012046"}), "BSE")
        self.assertEqual(_exchange("600001", None), "SSE")
        self.assertIsNone(_exchange("100001", None))

    def test_raw_row_hash_is_canonical_and_binds_complete_row(self) -> None:
        left = {"F003V": "A股", "SECCODE": "920001"}
        right = {"SECCODE": "920001", "F003V": "A股"}
        self.assertEqual(_canonical_row_sha256(left), _canonical_row_sha256(right))
        right["ORGNAME"] = "Company"
        self.assertNotEqual(_canonical_row_sha256(left), _canonical_row_sha256(right))
        self.assertIsNone(_canonical_row_sha256(None))

    def test_sina_requires_a_list_of_objects(self) -> None:
        self.assertEqual(
            _json_rows(b'[{"code":"000001"}]', source="sina"), [{"code": "000001"}]
        )
        for payload in (b"{}", b"[1]", b"not-json"):
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                _json_rows(payload, source="sina")

    def test_eastmoney_requires_success_rows_and_positive_page_count(self) -> None:
        payload = json.dumps(
            {
                "success": True,
                "result": {"data": [{"SECURITY_CODE": "000001"}], "pages": 2},
            }
        ).encode()
        self.assertEqual(
            _eastmoney_rows(payload, year=2025, page=1),
            ([{"SECURITY_CODE": "000001"}], 2),
        )

        for invalid in (
            {"success": False, "message": "blocked"},
            {"success": True, "result": {"data": {}, "pages": 1}},
            {"success": True, "result": {"data": [], "pages": 0}},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                _eastmoney_rows(json.dumps(invalid).encode(), year=2025, page=1)


if __name__ == "__main__":
    unittest.main()
