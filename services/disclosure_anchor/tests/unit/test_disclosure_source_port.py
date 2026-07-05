"""Tests for disclosure source port primitives and CNINFO fixtures."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import unittest

from disclosure_anchor.application.ports.disclosure_source import (
    DisclosureWindow,
    SourceSecurity,
)


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "cninfo"
FORBIDDEN_SECRET_KEYS = {"access_token", "client_id", "client_secret"}


class DisclosureSourcePortTests(unittest.TestCase):
    def test_disclosure_window_rejects_inverted_dates(self) -> None:
        with self.assertRaisesRegex(ValueError, "end must be on or after start"):
            DisclosureWindow(start=date(2026, 7, 2), end=date(2026, 7, 1))

    def test_source_security_keeps_provider_query_identity(self) -> None:
        security = SourceSecurity(
            security_code="000001",
            exchange="SZSE",
            security_name="平安银行",
        )

        self.assertEqual(security.security_code, "000001")
        self.assertEqual(security.exchange, "SZSE")

    def test_cninfo_json_fixtures_match_result_envelope(self) -> None:
        for name in (
            "p_info3015_sample.json",
            "p_info3015_empty.json",
            "p_stock2100_sample.json",
        ):
            with self.subTest(name=name):
                payload = json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
                self.assertEqual(payload["resultcode"], 200)
                self.assertIsInstance(payload["resultmsg"], str)
                self.assertIsInstance(payload["total"], int)
                self.assertIsInstance(payload["count"], int)
                self.assertIsInstance(payload["records"], list)
                self.assertFalse(_contains_secret_key(payload))

    def test_cninfo_p_info3015_fixture_contains_required_fields(self) -> None:
        payload = json.loads(
            (FIXTURE_ROOT / "p_info3015_sample.json").read_text(encoding="utf-8")
        )

        first = payload["records"][0]

        self.assertEqual(first["TEXTID"], "cninfo-test-000001-20260701-annual")
        self.assertIn("||", first["F006V"])
        for field in (
            "TEXTID",
            "RECID",
            "SECCODE",
            "SECNAME",
            "F001D",
            "F002V",
            "F003V",
            "F005N",
            "F006V",
            "OBJECTID",
            "RECTIME",
        ):
            self.assertIn(field, first)

    def test_cninfo_sample_pdf_has_pdf_magic(self) -> None:
        self.assertTrue(
            (FIXTURE_ROOT / "sample_announcement.pdf").read_bytes().startswith(b"%PDF-")
        )


def _contains_secret_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_SECRET_KEYS:
                return True
            if _contains_secret_key(item):
                return True
    if isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


if __name__ == "__main__":
    unittest.main()
