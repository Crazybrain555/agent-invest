"""Offline operator-config validation regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import config_check


class WatchlistConfigCheckTests(unittest.TestCase):
    def test_rejects_code_exchange_prefix_mismatch_before_track(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            watchlist = Path(tmp) / "watchlist.csv"
            watchlist.write_text(
                "security_code,exchange,status,joined_date,process_classes\n"
                "000001,SSE,active,,\n"
                "600519,SZSE,active,,\n"
                "920001,BSE,active,,\n",
                encoding="utf-8",
            )
            errors: list[str] = []
            with mock.patch.object(config_check, "WATCHLIST", watchlist):
                config_check.check_watchlist(errors, frozenset())

        self.assertEqual(len(errors), 2, errors)
        self.assertIn("belongs to SZSE, not SSE", errors[0])
        self.assertIn("belongs to SSE, not SZSE", errors[1])


if __name__ == "__main__":
    unittest.main()
