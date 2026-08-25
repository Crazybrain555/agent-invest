"""Offline operator-config validation regressions."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import config_check
from disclosure_anchor.adapters.watchlist_config import (
    DEFAULT_SCREEN_MANIFEST,
    DEFAULT_WATCHLIST,
    load_watchlist_snapshot,
    validate_screen_manifest,
)


class WatchlistConfigCheckTests(unittest.TestCase):
    def test_screen_sidecar_provenance_and_counts_are_closed(self) -> None:
        snapshot = load_watchlist_snapshot(DEFAULT_WATCHLIST)
        baseline = json.loads(DEFAULT_SCREEN_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(
            validate_screen_manifest(snapshot, DEFAULT_SCREEN_MANIFEST),
            [],
        )
        for tamper in (
            "missing_input",
            "invalid_board_counts",
            "invalid_observed_at",
            "claim_not_verified",
        ):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as tmp:
                manifest = json.loads(json.dumps(baseline))
                if tamper == "missing_input":
                    del manifest["input"]
                elif tamper == "invalid_board_counts":
                    manifest["result"]["selected_board_counts"] = {"BSE": 1_500}
                elif tamper == "invalid_observed_at":
                    manifest["observed_at_utc"] = "not-a-timestamp"
                else:
                    manifest["verification"]["unique_identities_verified"] = False
                path = Path(tmp) / "sidecar.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                errors = validate_screen_manifest(snapshot, path)
                self.assertTrue(
                    any("exact Pro-reviewed bytes" in error for error in errors),
                    errors,
                )

    def test_manifest_exchange_counts_must_cover_every_csv_row(self) -> None:
        baseline = json.loads(DEFAULT_SCREEN_MANIFEST.read_text(encoding="utf-8"))
        original = DEFAULT_WATCHLIST.read_text(encoding="utf-8")
        mutated = original.replace(",SSE,active,", ",BSE,active,", 1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watchlist = root / "watchlist.csv"
            manifest_path = root / "watchlist-screen.v1.json"
            watchlist.write_text(mutated, encoding="utf-8")
            snapshot = load_watchlist_snapshot(watchlist)
            rows = [
                row
                for row in snapshot.rows
                if (row.get("security_code") or "").strip()
            ]
            identities = [
                {
                    "security_code": (row.get("security_code") or "").strip(),
                    "exchange": (row.get("exchange") or "").strip(),
                }
                for row in rows
            ]
            identity_bytes = json.dumps(
                identities,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            baseline["result"]["watchlist_csv_sha256"] = snapshot.sha256
            baseline["result"]["ordered_identity_sha256"] = hashlib.sha256(
                identity_bytes
            ).hexdigest()
            baseline["result"]["selected_exchange_counts"]["SSE"] -= 1
            manifest_path.write_text(json.dumps(baseline), encoding="utf-8")

            errors = validate_screen_manifest(snapshot, manifest_path)

        self.assertTrue(
            any("selected_exchange_counts" in error for error in errors),
            errors,
        )

    def test_rejects_duplicate_unknown_and_overflow_csv_fields(self) -> None:
        cases = (
            (
                "security_code,security_code,exchange,status,joined_date,"
                "process_classes\n600519,600519,SSE,active,2026-08-23,\n",
                "duplicate header fields",
            ),
            (
                "security_code,exchange,status,joined_date,process_classes,typo\n"
                "600519,SSE,active,2026-08-23,,x\n",
                "unknown header fields",
            ),
            (
                "security_code,exchange,status,joined_date,process_classes\n"
                "600519,SSE,active,2026-08-23,,overflow\n",
                "fields beyond the header",
            ),
        )
        for content, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                watchlist = Path(tmp) / "watchlist.csv"
                watchlist.write_text(content, encoding="utf-8")
                errors: list[str] = []
                config_check.check_watchlist(
                    errors,
                    frozenset(),
                    watchlist=watchlist,
                )
                self.assertTrue(any(expected in error for error in errors), errors)

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

    def test_custom_watchlist_does_not_validate_default_screen_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            watchlist = Path(tmp) / "custom.csv"
            watchlist.write_text(
                "security_code,exchange,status,joined_date,process_classes\n"
                "600519,SSE,active,2026-08-23,\n",
                encoding="utf-8",
            )
            with mock.patch.object(config_check, "check_policy"):
                result = config_check.main(["--watchlist", str(watchlist)])

        self.assertEqual(result, 0)

    def test_resolved_alias_to_default_cannot_bypass_screen_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "nested"
            nested.mkdir()
            watchlist = root / "watchlist.csv"
            watchlist.write_text(
                "security_code,exchange,status,joined_date,process_classes\n"
                "600519,SSE,active,2026-08-23,\n",
                encoding="utf-8",
            )
            manifest = root / "watchlist-screen.v1.json"
            alias = nested / ".." / "watchlist.csv"
            with (
                mock.patch.object(config_check, "WATCHLIST", watchlist),
                mock.patch.object(config_check, "SCREEN_MANIFEST", manifest),
                mock.patch.object(config_check, "check_policy"),
                mock.patch.object(
                    config_check, "check_screen_manifest"
                ) as check_manifest,
            ):
                result = config_check.main(["--watchlist", str(alias)])

        self.assertEqual(result, 0)
        check_manifest.assert_called_once()
        self.assertEqual(
            check_manifest.call_args.kwargs["snapshot"].resolved_path,
            watchlist.resolve(),
        )
        self.assertEqual(
            check_manifest.call_args.kwargs["manifest_path"],
            manifest,
        )


if __name__ == "__main__":
    unittest.main()
