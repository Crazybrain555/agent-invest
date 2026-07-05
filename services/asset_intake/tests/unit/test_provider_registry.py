import unittest

from asset_intake.providers.registry import (
    RegistryError,
    TableAlias,
    TableCandidate,
    load_dataset_entries,
    load_provider_catalogs,
    resolve_active_table,
    preflight_provider,
    validate_cross_references,
    validate_request,
)

ENTRIES = load_dataset_entries()
CATALOGS = load_provider_catalogs()


class RegistryLoadingTests(unittest.TestCase):
    def test_four_datasets_and_two_catalogs_load(self) -> None:
        self.assertEqual(
            sorted(ENTRIES),
            [
                "cn_equity.earnings_event",
                "cn_equity.eod_quote",
                "cn_equity.fin_metric",
                "cn_equity.fin_statement",
            ],
        )
        self.assertEqual(sorted(CATALOGS), ["tushare_api", "wind_rds"])

    def test_cross_references_pass(self) -> None:
        validate_cross_references(ENTRIES, CATALOGS)

    def test_all_first_wave_entries_active(self) -> None:
        for entry in ENTRIES.values():
            self.assertEqual(entry.status, "active", entry.dataset_key)

    def test_wind_catalog_pins_probed_active_tables(self) -> None:
        aliases = CATALOGS["wind_rds"].table_aliases
        self.assertEqual(aliases["earnings_forecast"].active_table, "AShareProfitNoticeNew")
        self.assertEqual(aliases["fin_balance"].active_table, "AShareBalancesheet")
        self.assertEqual(aliases["fin_cashflow"].active_table, "AShareCashflow")

    def test_deleting_a_required_mapping_is_caught(self) -> None:
        entries = load_dataset_entries()
        entry = entries["cn_equity.eod_quote"]
        entry.providers["wind_rds"].field_map.pop("adj_factor")
        with self.assertRaises(RegistryError) as ctx:
            validate_cross_references(entries, CATALOGS)
        self.assertIn("adj_factor", str(ctx.exception))

    def test_unknown_provider_is_caught(self) -> None:
        entries = load_dataset_entries()
        entry = entries["cn_equity.eod_quote"]
        entry.providers["ghost_provider"] = entry.providers["wind_rds"]
        with self.assertRaises(RegistryError) as ctx:
            validate_cross_references(entries, CATALOGS)
        self.assertIn("ghost_provider", str(ctx.exception))


class RequestValidationTests(unittest.TestCase):
    def test_defaults_and_normalization(self) -> None:
        entry = ENTRIES["cn_equity.eod_quote"]
        params = validate_request(
            entry, {"security": "000001.SZ", "start_date": "2026-01-01", "end_date": "2026-06-30"}
        )
        self.assertEqual(params["price_adjustment"], "raw_plus_factor")

    def test_missing_required_and_unknown_and_enum(self) -> None:
        entry = ENTRIES["cn_equity.fin_statement"]
        with self.assertRaises(RegistryError):
            validate_request(entry, {"security": "000001.SZ"})
        with self.assertRaises(RegistryError):
            validate_request(entry, {"security": "x", "start_period": "1", "end_period": "2",
                                     "statement": "income", "bogus": 1})
        with self.assertRaises(RegistryError):
            validate_request(entry, {"security": "x", "start_period": "1", "end_period": "2",
                                     "statement": "indicator"})


class FakeProber:
    def __init__(self, tables: dict[str, set[str]], dates: dict[str, str] | None = None) -> None:
        self._tables = tables
        self._dates = dates or {}

    def table_exists(self, table: str) -> bool:
        return table in self._tables

    def columns(self, table: str) -> set[str]:
        return self._tables[table]

    def max_date(self, table: str, date_column: str) -> str | None:
        return self._dates.get(table)


def _forecast_alias(active: str | None) -> TableAlias:
    cols = {
        "S_INFO_WINDCODE", "S_PROFITNOTICE_DATE", "S_PROFITNOTICE_PERIOD",
        "S_PROFITNOTICE_STYLE", "S_PROFITNOTICE_NETPROFITMIN",
        "S_PROFITNOTICE_NETPROFITMAX", "S_PROFITNOTICE_FIRSTANNDATE",
    }
    return TableAlias(
        semantic_datasets=["cn_equity.earnings_event"],
        active_table=active,
        activation_rule=["table_exists", "required_columns_present", "max_date_freshness"],
        candidates=[
            TableCandidate(table="AShareProfitNoticeNew", role="active",
                           date_column="S_PROFITNOTICE_DATE", required_columns=sorted(cols)),
            TableCandidate(table="AShareProfitNotice", role="stale_candidate",
                           date_column="S_PROFITNOTICE_DATE", required_columns=sorted(cols)),
        ],
    )


class ActivationTests(unittest.TestCase):
    COLS = _forecast_alias(None).candidates[0].required_columns

    def test_freshness_picks_live_table(self) -> None:
        prober = FakeProber(
            {"AShareProfitNoticeNew": set(self.COLS), "AShareProfitNotice": set(self.COLS)},
            {"AShareProfitNoticeNew": "20260706", "AShareProfitNotice": "20240905"},
        )
        self.assertEqual(
            resolve_active_table("earnings_forecast", _forecast_alias(None), prober),
            "AShareProfitNoticeNew",
        )

    def test_missing_table_fails_fast_with_specifics(self) -> None:
        prober = FakeProber({})
        with self.assertRaises(RegistryError) as ctx:
            resolve_active_table("earnings_forecast", _forecast_alias(None), prober)
        self.assertIn("table missing", str(ctx.exception))

    def test_missing_columns_fail_fast_with_names(self) -> None:
        prober = FakeProber({"AShareProfitNoticeNew": {"S_INFO_WINDCODE"},
                             "AShareProfitNotice": {"S_INFO_WINDCODE"}})
        with self.assertRaises(RegistryError) as ctx:
            resolve_active_table("earnings_forecast", _forecast_alias(None), prober)
        self.assertIn("S_PROFITNOTICE_STYLE", str(ctx.exception))

    def test_pinned_active_table_must_qualify(self) -> None:
        prober = FakeProber({"AShareProfitNotice": set(self.COLS)},
                            {"AShareProfitNotice": "20240905"})
        with self.assertRaises(RegistryError) as ctx:
            resolve_active_table("earnings_forecast", _forecast_alias("AShareProfitNoticeNew"), prober)
        self.assertIn("pinned", str(ctx.exception))

    def test_preflight_reports_all_alias_failures(self) -> None:
        catalog = CATALOGS["wind_rds"]
        with self.assertRaises(RegistryError) as ctx:
            preflight_provider(catalog, ENTRIES, FakeProber({}))
        message = str(ctx.exception)
        for alias in ("eod_prices", "fin_income", "earnings_forecast"):
            self.assertIn(alias, message)


if __name__ == "__main__":
    unittest.main()
