"""Research-priority watchlist generation regressions."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from scripts.build_research_watchlist import (
    EXPECTED_RULES,
    _future_potential_metrics,
    _future_evidence_tier,
    _future_evidence_sort_key,
    _future_research_signals,
    build_manifest,
    cninfo_raw_row_sha256,
    identity_row_sha256,
    main as build_watchlist_main,
    recompute_selection,
    render_watchlist,
)
from scripts.config_check import check_screen_manifest
from disclosure_anchor.application.contracts.research_universe import SCREEN_SCHEMA


MARKET_CODE_AND_BOARD = {
    "BSE": ("012046", "BSE"),
    "SSE": ("012001", "SSE_MAIN"),
    "SZSE": ("012002", "SZSE_MAIN"),
}


def _refresh_future_fields(row: dict[str, Any]) -> None:
    signals = list(
        _future_research_signals(
            row,
            revenue_cagr_min_ratio=float(
                EXPECTED_RULES["revenue_cagr_2023_to_2025_min_ratio"]
            ),
            parent_profit_cagr_min_ratio=float(
                EXPECTED_RULES["parent_profit_cagr_2023_to_2025_min_ratio"]
            ),
            latest_parent_profit_growth_min_ratio=float(
                EXPECTED_RULES["parent_profit_growth_2024_to_2025_min_ratio"]
            ),
            profit_signal_prior_year_net_margin_min_ratio=float(
                EXPECTED_RULES["profit_signal_prior_year_net_margin_min_ratio"]
            ),
            durable_quality_average_roe_min_pct=float(
                EXPECTED_RULES["durable_quality_average_roe_min_pct"]
            ),
            durable_quality_revenue_cagr_min_ratio=float(
                EXPECTED_RULES["durable_quality_revenue_cagr_min_ratio"]
            ),
            durable_quality_parent_profit_cagr_min_ratio=float(
                EXPECTED_RULES["durable_quality_parent_profit_cagr_min_ratio"]
            ),
            profitable_turnaround_roe_min_pct=float(
                EXPECTED_RULES["profitable_turnaround_roe_2025_min_pct"]
            ),
        )
    )
    row["future_potential_metrics"] = _future_potential_metrics(row)
    row["future_research_signals"] = signals
    row["future_evidence_tier"] = _future_evidence_tier(tuple(signals))


def _row(
    code: str,
    exchange: str,
    market_cap: int,
    reasons: list[str],
    *,
    market_code: str | None = None,
) -> dict[str, Any]:
    default_market_code, default_board = MARKET_CODE_AND_BOARD[exchange]
    resolved_market_code = market_code or default_market_code
    board = {
        "012001": "SSE_MAIN",
        "012002": "SZSE_MAIN",
        "012015": "CHINEXT",
        "012029": "STAR",
        "012046": "BSE",
    }[resolved_market_code]
    raw_identity = {
        "F001V": f"mnemonic-{code}",
        "F002V": "001001",
        "F003V": "A股",
        "F004V": resolved_market_code,
        "F005V": f"{exchange} market",
        "F006D": "2010-01-01",
        "F010V": "013001",
        "F011V": "正常上市",
        "ORGNAME": f"Company {code}",
        "SECCODE": code,
        "SECNAME": f"company-{code}",
    }
    quote_raw = {
        "code": code,
        "name": f"company-{code}",
        "mktcap": market_cap / 10_000,
        "nmc": market_cap / 10_000,
        "trade": 10,
        "amount": 100,
        "turnoverratio": 1,
    }
    profits = {"2023": 1, "2024": 2, "2025": 3}
    revenues = {"2023": 10, "2024": 12, "2025": 15}
    roes = {"2023": 10, "2024": 10, "2025": 10}
    annual_rows = {
        year: {
            "SECURITY_CODE": code,
            "REPORTDATE": f"{year}-12-31 00:00:00",
            "PARENT_NETPROFIT": profits[year],
            "TOTAL_OPERATE_INCOME": revenues[year],
            "WEIGHTAVG_ROE": roes[year],
            "BPS": 2 if year == "2025" else 1,
            "MGJYXJJE": 1,
        }
        for year in ("2023", "2024", "2025")
    }
    row: dict[str, Any] = {
        "security_code": code,
        "exchange": exchange,
        "board": board or default_board,
        "name": f"company-{code}",
        "canonical_orgname": f"Company {code}",
        "list_date": "2010-01-01",
        "cninfo_sectype": "A股",
        "cninfo_status": "正常上市",
        "cninfo_list_date": "2010-01-01",
        "cninfo_source_sha256": "a" * 64,
        "cninfo_raw_row": raw_identity,
        "cninfo_raw_row_sha256": cninfo_raw_row_sha256(raw_identity),
        "market_cap_cny": market_cap,
        "float_market_cap_cny": market_cap,
        "last_price": 10,
        "daily_amount_cny": 100,
        "turnover_pct": 1,
        "profits": profits,
        "revenues": revenues,
        "roes": roes,
        "roe_2025": 10,
        "bps_2025": 2,
        "ocf_per_share_2025": 1,
        "latest_two_parent_losses": False,
        "quote_raw_row": quote_raw,
        "quote_raw_row_sha256": cninfo_raw_row_sha256(quote_raw),
        "annual_raw_rows": annual_rows,
        "annual_raw_row_sha256": {
            year: cninfo_raw_row_sha256(raw) for year, raw in annual_rows.items()
        },
        "exclude_reasons": reasons,
    }
    _refresh_future_fields(row)
    row["cninfo_row_sha256"] = identity_row_sha256(row)
    return row


def _set_financials(
    row: dict[str, Any],
    *,
    profits: dict[str, int | float],
    revenues: dict[str, int | float] | None = None,
) -> None:
    resolved_revenues = revenues or {"2023": 10, "2024": 12, "2025": 15}
    row["profits"] = profits
    row["revenues"] = resolved_revenues
    for year in ("2023", "2024", "2025"):
        raw = row["annual_raw_rows"][year]
        raw["PARENT_NETPROFIT"] = profits[year]
        raw["TOTAL_OPERATE_INCOME"] = resolved_revenues[year]
        row["annual_raw_row_sha256"][year] = cninfo_raw_row_sha256(raw)
    _refresh_future_fields(row)


def _screen() -> dict[str, Any]:
    rules = copy.deepcopy(EXPECTED_RULES)
    rules["selection_count_min"] = 2
    rules["selection_count_max"] = 2
    receipt_sha = hashlib.sha256(b"{}").hexdigest()
    excluded = _row("000001", "SZSE", 3_000_000_000, [])
    _set_financials(
        excluded,
        profits={"2023": 100, "2024": 101, "2025": 102},
        revenues={"2023": 100, "2024": 100, "2025": 101},
    )
    excluded["exclude_reasons"] = ["no_future_research_signal"]
    return {
        "schema": SCREEN_SCHEMA,
        "observed_at_utc": "2026-08-23T02:06:37+00:00",
        "identity_source": {
            "provider": "CNINFO p_stock2101",
            "evidence_relpath": (
                "watchlist/test/2026-08-23/sources/cninfo/p-stock2101.json"
            ),
            "rows": 3,
            "sha256": "a" * 64,
            "source_bundle_receipt_sha256": receipt_sha,
        },
        "quote_source": {
            "provider": "Sina Market Center hs_a",
            "evidence_relpath": "watchlist/test/2026-08-23/sources/sina",
            "rows": 3,
            "source_bundle_receipt_sha256": receipt_sha,
            "unit_note": "mktcap/nmc multiplied by 10,000 to CNY",
        },
        "annual_source": {
            "provider": "Eastmoney data center RPT_LICO_FN_CPD",
            "evidence_relpath": "watchlist/test/2026-08-23/sources/eastmoney",
            "raw_rows": 9,
            "source_bundle_receipt_sha256": receipt_sha,
            "years": [2023, 2024, 2025],
        },
        "rules_draft": rules,
        "selected": [
            _row("600001", "SSE", 4_000_000_000, []),
            _row(
                "688001",
                "SSE",
                5_000_000_000,
                [],
                market_code="012029",
            ),
        ],
        "excluded": [excluded],
    }


class ResearchWatchlistTests(unittest.TestCase):
    def test_v10_profit_growth_requires_a_nontrivial_prior_margin(self) -> None:
        row = _row("600001", "SSE", 4_000_000_000, [])
        _set_financials(
            row,
            profits={"2023": 1, "2024": 2, "2025": 3},
            revenues={"2023": 1_000, "2024": 1_000, "2025": 1_000},
        )

        self.assertEqual(row["future_research_signals"], [])
        self.assertIsNone(row["future_evidence_tier"])

    def test_v10_profit_growth_accepts_exact_one_percent_prior_margin(self) -> None:
        row = _row("600001", "SSE", 4_000_000_000, [])
        _set_financials(
            row,
            profits={"2023": 10, "2024": 10, "2025": 12},
            revenues={"2023": 1_000, "2024": 1_000, "2025": 1_000},
        )

        self.assertEqual(
            row["future_research_signals"],
            ["latest_parent_profit_growth_meets_floor_and_base_quality"],
        )

    def test_v10_accepts_exact_one_cny_bps_boundary(self) -> None:
        screen = _screen()
        row = screen["excluded"][0]
        _set_financials(
            row,
            profits={"2023": 1, "2024": 2, "2025": 3},
        )
        row["bps_2025"] = 1
        row["annual_raw_rows"]["2025"]["BPS"] = 1
        row["annual_raw_row_sha256"]["2025"] = cninfo_raw_row_sha256(
            row["annual_raw_rows"]["2025"]
        )
        row["exclude_reasons"] = []
        screen["selected"] = sorted(
            [*screen["selected"], row], key=_future_evidence_sort_key
        )
        screen["excluded"] = []
        screen["rules_draft"]["selection_count_min"] = 3
        screen["rules_draft"]["selection_count_max"] = 3

        with mock.patch.dict(
            EXPECTED_RULES,
            {"selection_count_min": 3, "selection_count_max": 3},
        ):
            selected, counts = recompute_selection(screen)

        self.assertEqual(len(selected), 3)
        self.assertEqual(counts, {})

    def test_selection_count_band_violation_fails_closed(self) -> None:
        screen = _screen()
        screen["rules_draft"]["selection_count_min"] = 3
        screen["rules_draft"]["selection_count_max"] = 4

        with mock.patch.dict(
            EXPECTED_RULES,
            {"selection_count_min": 3, "selection_count_max": 4},
        ):
            with self.assertRaisesRegex(
                ValueError, "outside the reviewed selection band"
            ):
                recompute_selection(screen)

    def test_v10_earlier_turnaround_must_not_fade_in_latest_year(self) -> None:
        row = _row("600001", "SSE", 4_000_000_000, [])
        _set_financials(
            row,
            profits={"2023": -1, "2024": 3, "2025": 2},
            revenues={"2023": 100, "2024": 100, "2025": 100},
        )

        self.assertEqual(row["future_research_signals"], [])
        self.assertIsNone(row["future_evidence_tier"])

    def test_v10_durable_quality_tiers_and_signal_ordering(self) -> None:
        quality = _row("600004", "SSE", 4_000_000_000, [])
        _set_financials(
            quality,
            profits={"2023": 100, "2024": 103, "2025": 106},
            revenues={"2023": 1_000, "2024": 1_030, "2025": 1_061},
        )
        quality["roes"] = {"2023": 20, "2024": 20, "2025": 20}
        _refresh_future_fields(quality)
        two_signal = _row("600003", "SSE", 4_000_000_000, [])
        _set_financials(
            two_signal,
            profits={"2023": 100, "2024": 110, "2025": 125},
            revenues={"2023": 1_000, "2024": 1_000, "2025": 1_000},
        )
        three_signal = _row("600002", "SSE", 4_000_000_000, [])
        four_signal = _row("600001", "SSE", 4_000_000_000, [])
        _set_financials(
            four_signal,
            profits={"2023": 100, "2024": 120, "2025": 150},
            revenues={"2023": 1_000, "2024": 1_200, "2025": 1_500},
        )
        four_signal["roes"] = {"2023": 20, "2024": 20, "2025": 20}
        _refresh_future_fields(four_signal)

        self.assertEqual(quality["future_research_signals"], ["durable_quality_compounder"])
        self.assertEqual(quality["future_evidence_tier"], "C")
        self.assertEqual(two_signal["future_evidence_tier"], "B")
        self.assertEqual(three_signal["future_evidence_tier"], "A")
        self.assertEqual(len(four_signal["future_research_signals"]), 4)
        self.assertEqual(
            [
                row["security_code"]
                for row in sorted(
                    [quality, two_signal, three_signal, four_signal],
                    key=_future_evidence_sort_key,
                )
            ],
            ["600001", "600002", "600003", "600004"],
        )

    def test_cli_outputs_are_new_only_and_read_only(self) -> None:
        screen = _screen()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            screen_path = root / "screen.json"
            receipt_path = root / "receipt.json"
            out = root / "watchlist.csv"
            manifest = root / "watchlist-screen.v1.json"
            screen_path.write_text(json.dumps(screen), encoding="utf-8")
            receipt_path.write_text("{}", encoding="utf-8")
            argv = [
                "--input",
                str(screen_path),
                "--out",
                str(out),
                "--manifest-out",
                str(manifest),
                "--fetch-receipt",
                str(receipt_path),
            ]
            with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
                self.assertEqual(build_watchlist_main(argv), 0)
                with self.assertRaisesRegex(FileExistsError, "new-only"):
                    build_watchlist_main(argv)

            self.assertEqual(out.stat().st_mode & 0o777, 0o444)
            self.assertEqual(manifest.stat().st_mode & 0o777, 0o444)

    def test_recomputes_order_and_binds_manifest_to_csv(self) -> None:
        screen = _screen()
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            selected, counts = recompute_selection(screen)
            self.assertEqual(
                [row["security_code"] for row in selected],
                ["600001", "688001"],
            )
            self.assertEqual(counts, {"no_future_research_signal": 1})
            csv_bytes = render_watchlist(
                selected, joined_date="2026-08-23"
            ).encode()

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                screen_path = root / "screen.json"
                receipt_path = root / "receipt.json"
                screen_bytes = json.dumps(screen).encode()
                screen_path.write_bytes(screen_bytes)
                receipt_path.write_text("{}", encoding="utf-8")
                manifest = build_manifest(
                    screen_path=screen_path,
                    screen=screen,
                    screen_bytes=screen_bytes,
                    selected=selected,
                    exclusion_counts=counts,
                    csv_bytes=csv_bytes,
                    receipt_path=receipt_path,
                    screen_evidence_relpath=(
                        "watchlist/test/2026-08-23/screen.json"
                    ),
                    fetch_receipt_evidence_relpath=(
                        "watchlist/test/2026-08-23/sources/receipt.json"
                    ),
                )
                watchlist = root / "watchlist.csv"
                manifest_path = root / "watchlist-screen.v1.json"
                watchlist.write_bytes(csv_bytes)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                errors: list[str] = []
                check_screen_manifest(
                    errors,
                    watchlist=watchlist,
                    manifest_path=manifest_path,
                )
                tampered = copy.deepcopy(manifest)
                tampered["rules"]["normal_listing_required"] = False
                tampered["evidence_limitations"] = []
                tampered["result"]["selected_min_market_cap_cny"] = 1
                manifest_path.write_text(
                    json.dumps(tampered), encoding="utf-8"
                )
                tamper_errors: list[str] = []
                check_screen_manifest(
                    tamper_errors,
                    watchlist=watchlist,
                    manifest_path=manifest_path,
                )

        self.assertEqual(errors, [])
        self.assertTrue(any("rules must exactly match" in item for item in tamper_errors))
        self.assertTrue(any("evidence_limitations" in item for item in tamper_errors))
        self.assertTrue(any("selected_min_market_cap" in item for item in tamper_errors))

    def test_rejects_recorded_reason_drift(self) -> None:
        screen = copy.deepcopy(_screen())
        screen["excluded"][0]["exclude_reasons"] = []
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(
                ValueError, "exclusion reason drift"
            ):
                recompute_selection(screen)

        duplicate = _screen()
        duplicate["excluded"][0]["exclude_reasons"].append(
            "no_future_research_signal"
        )
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
                recompute_selection(duplicate)

    def test_listing_cutoff_must_match_observation_minus_calendar_months(self) -> None:
        screen = _screen()
        screen["observed_at_utc"] = "2026-09-23T00:00:00+00:00"
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(
                ValueError,
                "listing_date_on_or_before does not match",
            ):
                recompute_selection(screen)

        naive = _screen()
        naive["observed_at_utc"] = "2026-08-23T00:00:00"
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "aware UTC timestamp"):
                recompute_selection(naive)

        selected_rank = _screen()
        selected_rank["selected"][0]["exclude_reasons"] = [
            "no_future_research_signal"
        ]
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "exclusion reason drift"):
                recompute_selection(selected_rank)

    def test_rejects_rule_description_drift(self) -> None:
        screen = _screen()
        screen["rules_draft"]["normal_listing_required"] = False
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "must exactly match"):
                recompute_selection(screen)

    def test_latest_parent_loss_is_excluded(self) -> None:
        screen = _screen()
        loss_row = screen["excluded"][0]
        _set_financials(
            loss_row,
            profits={"2023": 10, "2024": -1, "2025": -2},
        )
        loss_row["latest_two_parent_losses"] = True
        loss_row["exclude_reasons"] = [
            "fewer_than_two_positive_parent_profit_years",
            "latest_parent_profit_nonpositive",
        ]
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            selected, counts = recompute_selection(screen)

        self.assertEqual(len(selected), 2)
        self.assertEqual(
            counts,
            {
                "fewer_than_two_positive_parent_profit_years": 1,
                "latest_parent_profit_nonpositive": 1,
            },
        )

    def test_no_future_research_signal_is_excluded(self) -> None:
        screen = _screen()
        row = screen["excluded"][0]
        _set_financials(
            row,
            profits={"2023": 100, "2024": 101, "2025": 102},
            revenues={"2023": 100, "2024": 100, "2025": 101},
        )
        row["latest_two_parent_losses"] = False
        row["future_research_signals"] = []
        row["exclude_reasons"] = ["no_future_research_signal"]
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            selected, counts = recompute_selection(screen)

        self.assertEqual(len(selected), 2)
        self.assertEqual(counts, {"no_future_research_signal": 1})

    def test_two_loss_years_then_turnaround_is_still_excluded(self) -> None:
        screen = _screen()
        row = screen["excluded"][0]
        _set_financials(
            row,
            profits={"2023": -3, "2024": -1, "2025": 2},
        )
        row["latest_two_parent_losses"] = False
        row["exclude_reasons"] = [
            "fewer_than_two_positive_parent_profit_years"
        ]
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            selected, counts = recompute_selection(screen)

        self.assertEqual(len(selected), 2)
        self.assertEqual(
            counts,
            {"fewer_than_two_positive_parent_profit_years": 1},
        )

    def test_missing_or_low_bps_is_excluded(self) -> None:
        for bps in (None, 0, 0.99):
            with self.subTest(bps=bps):
                screen = _screen()
                row = screen["excluded"][0]
                row["bps_2025"] = bps
                row["annual_raw_rows"]["2025"]["BPS"] = bps
                row["annual_raw_row_sha256"]["2025"] = cninfo_raw_row_sha256(
                    row["annual_raw_rows"]["2025"]
                )
                row["exclude_reasons"] = ["missing_or_low_2025_bps"]
                with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
                    selected, counts = recompute_selection(screen)

                self.assertEqual(len(selected), 2)
                self.assertEqual(
                    counts,
                    {"missing_or_low_2025_bps": 1},
                )

    def test_latest_roe_below_quality_floor_is_excluded(self) -> None:
        screen = _screen()
        row = screen["excluded"][0]
        row["roe_2025"] = 4.99
        row["roes"]["2025"] = 4.99
        row["annual_raw_rows"]["2025"]["WEIGHTAVG_ROE"] = 4.99
        row["annual_raw_row_sha256"]["2025"] = cninfo_raw_row_sha256(
            row["annual_raw_rows"]["2025"]
        )
        _refresh_future_fields(row)
        row["exclude_reasons"] = ["missing_or_low_2025_roe"]
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            selected, counts = recompute_selection(screen)

        self.assertEqual(len(selected), 2)
        self.assertEqual(counts, {"missing_or_low_2025_roe": 1})

    def test_zero_trade_price_is_observed_but_not_a_quality_gate(self) -> None:
        screen = _screen()
        row = screen["excluded"][0]
        row["last_price"] = 0
        row["quote_raw_row"]["trade"] = 0
        row["quote_raw_row_sha256"] = cninfo_raw_row_sha256(
            row["quote_raw_row"]
        )
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            selected, counts = recompute_selection(screen)

        self.assertEqual(len(selected), 2)
        self.assertEqual(counts, {"no_future_research_signal": 1})

    def test_every_row_is_bound_to_identity_source_and_retained_row_hash(self) -> None:
        source_drift = _screen()
        source_drift["excluded"][0]["cninfo_source_sha256"] = "b" * 64
        source_drift["excluded"][0]["cninfo_row_sha256"] = identity_row_sha256(
            source_drift["excluded"][0]
        )
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "does not match identity source"):
                recompute_selection(source_drift)

        row_drift = _screen()
        row_drift["excluded"][0]["cninfo_status"] = "暂停上市"
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "does not match retained identity"):
                recompute_selection(row_drift)

        raw_drift = _screen()
        raw_drift["excluded"][0]["cninfo_raw_row"]["F007N"] = float("nan")
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "canonical-JSON serializable"):
                recompute_selection(raw_drift)

    def test_full_cninfo_raw_row_hash_and_retained_projection_are_verified(self) -> None:
        raw_hash_drift = _screen()
        raw_hash_drift["excluded"][0]["cninfo_raw_row_sha256"] = "b" * 64
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "does not match raw row"):
                recompute_selection(raw_hash_drift)

        retained_drift = _screen()
        row = retained_drift["excluded"][0]
        row["cninfo_raw_row"]["F011V"] = "暂停上市"
        row["cninfo_raw_row_sha256"] = cninfo_raw_row_sha256(
            row["cninfo_raw_row"]
        )
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "does not match raw row"):
                recompute_selection(retained_drift)

    def test_missing_cninfo_identity_uses_explicit_null_raw_row(self) -> None:
        screen = _screen()
        row = screen["excluded"][0]
        row["list_date"] = None
        row["cninfo_sectype"] = None
        row["cninfo_status"] = None
        row["cninfo_list_date"] = None
        row["cninfo_raw_row"] = None
        row["cninfo_raw_row_sha256"] = None
        row["board"] = None
        row["cninfo_row_sha256"] = identity_row_sha256(row)
        row["exclude_reasons"] = [
            "missing_cninfo_identity",
            "missing_listing_date",
            "outside_research_boards",
        ]
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            selected, counts = recompute_selection(screen)

        self.assertEqual(len(selected), 2)
        self.assertEqual(
            counts,
            {
                "missing_cninfo_identity": 1,
                "missing_listing_date": 1,
                "outside_research_boards": 1,
            },
        )

    def test_cninfo_identity_must_be_complete_and_list_date_must_match(self) -> None:
        omitted = _screen()
        omitted_row = omitted["excluded"][0]
        del omitted_row["cninfo_status"]
        omitted_row["cninfo_row_sha256"] = identity_row_sha256(omitted_row)
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "identity fields missing"):
                recompute_selection(omitted)

        partial = _screen()
        partial_row = partial["excluded"][0]
        partial_row["cninfo_status"] = None
        partial_row["cninfo_row_sha256"] = identity_row_sha256(partial_row)
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "complete or all null"):
                recompute_selection(partial)

        date_drift = _screen()
        date_row = date_drift["excluded"][0]
        date_row["list_date"] = "2011-01-01"
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "must exactly match"):
                recompute_selection(date_drift)

    def test_bse_is_canonical_but_outside_the_research_pool(self) -> None:
        for code in ("920001", "430001", "830001"):
            with self.subTest(code=code):
                screen = _screen()
                screen["excluded"][0] = _row(
                    code,
                    "BSE",
                    3_000_000_000,
                    ["outside_research_boards"],
                )
                with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
                    selected, counts = recompute_selection(screen)
                self.assertNotIn(code, {row["security_code"] for row in selected})
                self.assertEqual(counts, {"outside_research_boards": 1})

        mismatch = _screen()
        mismatch["excluded"][0] = _row(
            "920001",
            "BSE",
            3_000_000_000,
            ["outside_research_boards"],
        )
        mismatch["excluded"][0]["exchange"] = "SSE"
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "belongs to BSE, not SSE"):
                recompute_selection(mismatch)

    def test_board_is_bound_to_the_full_cninfo_market_code(self) -> None:
        screen = _screen()
        row = screen["excluded"][0]
        row["board"] = "CHINEXT"
        row["cninfo_row_sha256"] = identity_row_sha256(row)
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "does not match raw row"):
                recompute_selection(screen)

    def test_missing_market_value_fails_the_numeric_gate(self) -> None:
        screen = _screen()
        row = screen["excluded"][0]
        row["market_cap_cny"] = None
        row["quote_raw_row"]["mktcap"] = None
        row["quote_raw_row_sha256"] = cninfo_raw_row_sha256(
            row["quote_raw_row"]
        )
        row["exclude_reasons"] = ["market_cap_lt_2bn"]
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            selected, counts = recompute_selection(screen)

        self.assertEqual(len(selected), 2)
        self.assertEqual(counts, {"market_cap_lt_2bn": 1})

    def test_quote_and_annual_raw_rows_bind_every_screen_projection(self) -> None:
        quote_drift = _screen()
        quote_drift["excluded"][0]["market_cap_cny"] += 1
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "quote projection"):
                recompute_selection(quote_drift)

        annual_drift = _screen()
        annual_drift["excluded"][0]["revenues"]["2025"] += 1
        with mock.patch.dict(EXPECTED_RULES, {"selection_count_min": 2, "selection_count_max": 2}):
            with self.assertRaisesRegex(ValueError, "annual 2025 projection"):
                recompute_selection(annual_drift)


if __name__ == "__main__":
    unittest.main()
