#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 1 valuation math helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import yaml

SCENARIOS = ("bear", "base", "bull")

DEFAULT_POLICY: dict[str, Any] = {
    "version": "v0.1-phase1",
    "method_defaults": {
        "model_type": "hybrid",
        "weights": {"epv": 0.6, "dcf": 0.4},
    },
    "owner_earnings": {
        "base_period": "TTM",
        "adjustment": {"bear": -0.2, "base": 0.0, "bull": 0.2},
    },
    "epv": {"multiple": {"bear": 10, "base": 14, "bull": 18}},
    "dcf": {
        "enabled": True,
        "years": 5,
        "growth": {"bear": 0.0, "base": 0.03, "bull": 0.06},
        "discount_rate": {"bear": 0.12, "base": 0.105, "bull": 0.095},
        "terminal_multiple": {"bear": 12, "base": 15, "bull": 18},
    },
    "quality": {"coefficient_base": 0.5, "confidence": 0.3},
    "margin_of_safety": {"formula": "(IV - price) / price"},
}


def load_policy(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_policy(policy: dict[str, Any]) -> dict[str, Any]:
    return _deep_merge(DEFAULT_POLICY, policy or {})


def select_latest_core_row(core_df: pd.DataFrame) -> pd.Series:
    df = core_df.copy()
    if "period_end" in df.columns:
        df["_period_dt"] = pd.to_datetime(df["period_end"], errors="coerce")
        df = df.sort_values("_period_dt", kind="mergesort")
    return df.iloc[-1]


def _extract_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return number


def owner_earnings_scenarios(base_owner_earnings: float, adjustments: Mapping[str, Any]) -> dict[str, float]:
    scenarios: dict[str, float] = {}
    for scenario in SCENARIOS:
        delta = float(adjustments.get(scenario, 0.0))
        scenarios[scenario] = base_owner_earnings * (1.0 + delta)
    return scenarios


def compute_epv(
    owner_earnings_by_scenario: Mapping[str, float],
    multiples: Mapping[str, Any],
) -> dict[str, float]:
    values: dict[str, float] = {}
    for scenario in SCENARIOS:
        multiple = float(multiples.get(scenario, 0.0))
        values[scenario] = owner_earnings_by_scenario[scenario] * multiple
    return values


def _calc_dcf_scenario(
    base_owner_earnings: float,
    growth: float,
    discount_rate: float,
    terminal_multiple: float,
    years: int,
) -> float:
    if years <= 0 or discount_rate <= 0:
        return 0.0
    cash_flows: list[float] = []
    for year in range(1, years + 1):
        cash_flow = base_owner_earnings * (1.0 + growth) ** year
        cash_flows.append(cash_flow)
    pv_stage1 = sum(cf / (1.0 + discount_rate) ** year for year, cf in enumerate(cash_flows, 1))
    terminal_value = (cash_flows[-1] if cash_flows else 0.0) * terminal_multiple
    pv_terminal = terminal_value / (1.0 + discount_rate) ** years
    return pv_stage1 + pv_terminal


def compute_dcf(
    base_owner_earnings: float,
    growth_rates: Mapping[str, Any],
    discount_rates: Mapping[str, Any],
    terminal_multiples: Mapping[str, Any],
    years: int,
) -> dict[str, float]:
    values: dict[str, float] = {}
    for scenario in SCENARIOS:
        values[scenario] = _calc_dcf_scenario(
            base_owner_earnings,
            float(growth_rates.get(scenario, 0.0)),
            float(discount_rates.get(scenario, 0.0)),
            float(terminal_multiples.get(scenario, 0.0)),
            int(years),
        )
    return values


def normalize_weights(weights: Mapping[str, Any], model_type: str, dcf_enabled: bool) -> dict[str, float]:
    if model_type == "epv" or not dcf_enabled:
        return {"epv": 1.0, "dcf": 0.0}
    if model_type == "dcf":
        return {"epv": 0.0, "dcf": 1.0}
    epv_weight = float(weights.get("epv", 0.0))
    dcf_weight = float(weights.get("dcf", 0.0))
    total = epv_weight + dcf_weight
    if total <= 0:
        return {"epv": 1.0, "dcf": 0.0}
    return {"epv": epv_weight / total, "dcf": dcf_weight / total}


def combine_values(
    epv_values: Mapping[str, float],
    dcf_values: Mapping[str, float],
    weights: Mapping[str, float],
) -> dict[str, float]:
    combined: dict[str, float] = {}
    for scenario in SCENARIOS:
        combined[scenario] = (
            epv_values.get(scenario, 0.0) * weights.get("epv", 0.0)
            + dcf_values.get(scenario, 0.0) * weights.get("dcf", 0.0)
        )
    return combined


def per_share(values: Mapping[str, float], shares: float) -> dict[str, float]:
    if shares <= 0:
        return {scenario: 0.0 for scenario in SCENARIOS}
    return {scenario: values[scenario] / shares for scenario in SCENARIOS}


def margin_of_safety(
    iv_per_share: Mapping[str, float],
    price: float,
    formula: str,
) -> dict[str, float]:
    if price == 0:
        return {scenario: 0.0 for scenario in SCENARIOS}
    if formula.strip().lower().startswith("(iv - price) / price"):
        return {scenario: (iv_per_share[scenario] - price) / price for scenario in SCENARIOS}
    return {scenario: (iv_per_share[scenario] - price) / price for scenario in SCENARIOS}


def build_model_frame(
    *,
    owner_earnings_by_scenario: Mapping[str, float],
    epv_values: Mapping[str, float],
    dcf_values: Mapping[str, float],
    combined_values: Mapping[str, float],
    iv_per_share: Mapping[str, float],
    margin_of_safety_values: Mapping[str, float],
    epv_multiples: Mapping[str, Any],
    oe_adjustments: Mapping[str, Any],
    dcf_growth: Mapping[str, Any],
    dcf_discount: Mapping[str, Any],
    dcf_terminal: Mapping[str, Any],
    weights: Mapping[str, float],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        rows.append(
            {
                "scenario": scenario,
                "owner_earnings": owner_earnings_by_scenario.get(scenario),
                "owner_earnings_adjustment": oe_adjustments.get(scenario),
                "epv_multiple": epv_multiples.get(scenario),
                "epv_value": epv_values.get(scenario),
                "dcf_growth": dcf_growth.get(scenario),
                "dcf_discount_rate": dcf_discount.get(scenario),
                "dcf_terminal_multiple": dcf_terminal.get(scenario),
                "dcf_value": dcf_values.get(scenario),
                "combined_value": combined_values.get(scenario),
                "intrinsic_value_per_share": iv_per_share.get(scenario),
                "margin_of_safety": margin_of_safety_values.get(scenario),
                "weight_epv": weights.get("epv"),
                "weight_dcf": weights.get("dcf"),
            }
        )
    return pd.DataFrame(rows)


def compute_valuation(
    *,
    owner_earnings: float,
    price: float,
    shares: float,
    policy: dict[str, Any],
    model_type: str,
) -> dict[str, Any]:
    policy = normalize_policy(policy)
    dcf_policy = policy.get("dcf") or {}
    dcf_enabled = bool(dcf_policy.get("enabled", True))

    oe_adjustments = policy.get("owner_earnings", {}).get("adjustment", {})
    epv_multiples = policy.get("epv", {}).get("multiple", {})
    oe_by_scenario = owner_earnings_scenarios(owner_earnings, oe_adjustments)
    epv_values = compute_epv(oe_by_scenario, epv_multiples)

    dcf_values = compute_dcf(
        owner_earnings,
        dcf_policy.get("growth", {}),
        dcf_policy.get("discount_rate", {}),
        dcf_policy.get("terminal_multiple", {}),
        int(dcf_policy.get("years", 5)),
    )

    weights = normalize_weights(policy.get("method_defaults", {}).get("weights", {}), model_type, dcf_enabled)
    combined = combine_values(epv_values, dcf_values, weights)
    iv_per_share = per_share(combined, shares)

    mos_formula = policy.get("margin_of_safety", {}).get("formula", "(IV - price) / price")
    mos = margin_of_safety(iv_per_share, price, mos_formula)

    model_frame = build_model_frame(
        owner_earnings_by_scenario=oe_by_scenario,
        epv_values=epv_values,
        dcf_values=dcf_values,
        combined_values=combined,
        iv_per_share=iv_per_share,
        margin_of_safety_values=mos,
        epv_multiples=epv_multiples,
        oe_adjustments=oe_adjustments,
        dcf_growth=dcf_policy.get("growth", {}),
        dcf_discount=dcf_policy.get("discount_rate", {}),
        dcf_terminal=dcf_policy.get("terminal_multiple", {}),
        weights=weights,
    )

    methods_used: list[str] = []
    if weights.get("epv", 0.0) > 0:
        methods_used.append("epv")
    if weights.get("dcf", 0.0) > 0:
        methods_used.append("dcf")

    return {
        "policy": policy,
        "owner_earnings_by_scenario": oe_by_scenario,
        "epv_values": epv_values,
        "dcf_values": dcf_values,
        "combined_values": combined,
        "intrinsic_value_per_share": iv_per_share,
        "margin_of_safety": mos,
        "weights": weights,
        "methods_used": methods_used,
        "model_frame": model_frame,
        "mos_formula": mos_formula,
    }


def extract_latest_metrics(row: pd.Series) -> dict[str, float | None]:
    return {
        "period_end": row.get("period_end"),
        "fiscal_period": row.get("fiscal_period"),
        "owner_earnings": _extract_float(row.get("owner_earnings")),
        "fcf": _extract_float(row.get("fcf")),
        "nopat": _extract_float(row.get("nopat")),
        "invested_capital": _extract_float(row.get("invested_capital")),
        "roic": _extract_float(row.get("roic")),
        "maintenance_capex": _extract_float(row.get("maintenance_capex")),
    }


def has_owner_earnings(core_df: pd.DataFrame) -> bool:
    if "owner_earnings" not in core_df.columns:
        return False
    series = pd.to_numeric(core_df["owner_earnings"], errors="coerce")
    return series.notna().any()


def select_owner_earnings_row(core_df: pd.DataFrame) -> pd.Series:
    if "owner_earnings" not in core_df.columns:
        return select_latest_core_row(core_df)
    df = core_df.copy()
    if "period_end" in df.columns:
        df["_period_dt"] = pd.to_datetime(df["period_end"], errors="coerce")
        df = df.sort_values("_period_dt", kind="mergesort")
    df["_oe_num"] = pd.to_numeric(df["owner_earnings"], errors="coerce")
    non_null = df[df["_oe_num"].notna()]
    if non_null.empty:
        return df.iloc[-1]
    return non_null.iloc[-1]
