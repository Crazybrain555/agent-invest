"""Cascade resolution for the tracked-companies read endpoint (round22)."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from disclosure_anchor.api.routers.tracked import _tracked_company


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "tracked_company_id": "tc_1",
        "company_ref": "co_1",
        "security_ref": "sec_1",
        "security_code": "600519",
        "exchange": "SSE",
        "legal_name": "贵州茅台酒股份有限公司",
        "legal_name_status": "resolved",
        "status": "active",
        "lookback_days": None,
        "sync_frequency": None,
        "process_classes": None,
        "last_synced_at": None,
        "synced_through": None,
        "created_at": datetime(2026, 7, 8, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 7, 8, tzinfo=timezone.utc),
        "contract_version": "tracked_company.v1",
    }
    row.update(overrides)
    return row


class TrackedCompanyCascadeTests(unittest.TestCase):
    def test_null_overrides_inherit_global_defaults(self) -> None:
        item = _tracked_company(
            _row(),
            global_classes=["annual_report", "dividend"],
            default_lookback_days=1095,
            default_sync_seconds=86400,
        )

        self.assertEqual(item.effective_lookback_days, 1095)
        self.assertEqual(item.effective_sync_seconds, 86400)
        self.assertEqual(item.effective_process_classes, ["annual_report", "dividend"])
        self.assertIsNone(item.process_classes)

    def test_company_overrides_replace_globals(self) -> None:
        item = _tracked_company(
            _row(
                lookback_days=30,
                sync_frequency="hourly",
                process_classes=["annual_report"],
            ),
            global_classes=["annual_report", "dividend"],
            default_lookback_days=1095,
            default_sync_seconds=86400,
        )

        self.assertEqual(item.effective_lookback_days, 30)
        self.assertEqual(item.effective_sync_seconds, 3600)
        # Replacement semantics, not merge.
        self.assertEqual(item.effective_process_classes, ["annual_report"])

    def test_zero_lookback_override_is_respected(self) -> None:
        item = _tracked_company(
            _row(lookback_days=0),
            global_classes=[],
            default_lookback_days=1095,
            default_sync_seconds=86400,
        )

        self.assertEqual(item.effective_lookback_days, 0)

    def test_sync_state_never_synced_when_no_checkpoint(self) -> None:
        item = _tracked_company(
            _row(),
            global_classes=[],
            default_lookback_days=1095,
            default_sync_seconds=86400,
        )

        self.assertEqual(item.sync_state, "never_synced")

    def test_sync_state_fresh_within_interval_and_due_beyond_it(self) -> None:
        now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
        base = _row(last_synced_at=datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc))

        fresh = _tracked_company(
            dict(base),
            global_classes=[],
            default_lookback_days=1095,
            default_sync_seconds=86400,
            now=now,
        )
        self.assertEqual(fresh.sync_state, "fresh")

        # hourly override: the 2h-old checkpoint is overdue.
        due = _tracked_company(
            dict(base, sync_frequency="hourly"),
            global_classes=[],
            default_lookback_days=1095,
            default_sync_seconds=86400,
            now=now,
        )
        self.assertEqual(due.sync_state, "due")


if __name__ == "__main__":
    unittest.main()
