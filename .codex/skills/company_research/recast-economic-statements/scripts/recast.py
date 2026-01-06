#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recast Statement Atlas facts into economic statements (v0.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

REQUIRED_COLUMNS = [
    "period_end",
    "fiscal_period",
    "statement_type",
    "concept",
    "label",
    "value",
    "accession",
]


@dataclass(frozen=True)
class MatchResult:
    value: float | None
    label: str | None
    concept: str | None
    match_type: str | None
    matched_on: str | None


def load_policy(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def normalize_label(text: str | None) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"[^a-z0-9]+", " ", str(text).lower())
    return " ".join(cleaned.split())


def _prepare_facts(facts_df: pd.DataFrame) -> pd.DataFrame:
    facts = facts_df.copy()
    for column in REQUIRED_COLUMNS:
        if column not in facts.columns:
            facts[column] = None
    facts["label_norm"] = facts["label"].apply(normalize_label)
    facts["concept_norm"] = facts["concept"].astype(str).str.lower()
    facts["period_end_str"] = facts["period_end"].astype(str)
    return facts


def _pick_best(matches: pd.DataFrame, *, match_type: str, matched_on: str) -> MatchResult:
    candidates = matches.copy()
    candidates["_value_num"] = pd.to_numeric(candidates["value"], errors="coerce")
    candidates["_abs_value"] = candidates["_value_num"].abs()
    candidates = candidates.sort_values(
        by=["_abs_value", "label_norm"],
        ascending=[False, True],
        kind="mergesort",
    )
    row = candidates.iloc[0]
    value = row["_value_num"]
    if pd.isna(value):
        value = None
    return MatchResult(
        value=value,
        label=row.get("label"),
        concept=row.get("concept"),
        match_type=match_type,
        matched_on=matched_on,
    )


def select_fact(period_facts: pd.DataFrame, rule: dict[str, Any]) -> MatchResult | None:
    subset = period_facts
    statement_type = rule.get("statement_type")
    if statement_type:
        subset = subset[subset["statement_type"] == statement_type]
    if subset.empty:
        return None

    concept_matches = rule.get("concept_matches") or []
    for concept in concept_matches:
        if not concept:
            continue
        mask = subset["concept_norm"] == str(concept).lower()
        if mask.any():
            return _pick_best(subset[mask], match_type="concept", matched_on=concept)

    label_matches = rule.get("label_matches") or []
    for label in label_matches:
        key = normalize_label(label)
        if not key:
            continue
        mask = subset["label_norm"].str.contains(key, na=False)
        if mask.any():
            return _pick_best(subset[mask], match_type="label", matched_on=label)

    return None


def build_economic_statements(
    facts_df: pd.DataFrame,
    periods_payload: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    facts = _prepare_facts(facts_df)
    periods = periods_payload.get("periods") or []
    rules = policy.get("mapping_rules") or []

    mapping_summary: dict[str, Any] = {}
    for rule in rules:
        target = rule.get("target")
        if not target:
            continue
        mapping_summary[target] = {
            "matched_periods": 0,
            "total_periods": len(periods),
            "match_types": {"concept": 0, "label": 0},
            "chosen_labels": set(),
            "chosen_concepts": set(),
            "statement_type": rule.get("statement_type"),
            "selector": {
                "concept_matches": rule.get("concept_matches") or [],
                "label_matches": rule.get("label_matches") or [],
            },
            "rationale": rule.get("rationale"),
            "fallback_used": False,
        }

    rows: list[dict[str, Any]] = []
    for period_info in periods:
        period_end = str(period_info.get("period_end"))
        period_facts = facts[facts["period_end_str"] == period_end]
        row = {
            "period_end": period_end,
            "fiscal_period": period_info.get("fiscal_period"),
            "accession": period_info.get("accession"),
        }

        for rule in rules:
            target = rule.get("target")
            if not target:
                continue
            result = select_fact(period_facts, rule)
            if result is None:
                row[target] = None
                row[f"{target}_label"] = None
                row[f"{target}_concept"] = None
                row[f"{target}_match"] = None
                mapping_summary[target]["fallback_used"] = True
                continue

            row[target] = result.value
            row[f"{target}_label"] = result.label
            row[f"{target}_concept"] = result.concept
            row[f"{target}_match"] = result.match_type

            mapping_summary[target]["matched_periods"] += 1
            if result.label:
                mapping_summary[target]["chosen_labels"].add(result.label)
            if result.concept:
                mapping_summary[target]["chosen_concepts"].add(result.concept)
            if result.match_type in mapping_summary[target]["match_types"]:
                mapping_summary[target]["match_types"][result.match_type] += 1

        rows.append(row)

    economic_df = pd.DataFrame(rows)
    for target, summary in mapping_summary.items():
        summary["chosen_labels"] = sorted(summary["chosen_labels"])
        summary["chosen_concepts"] = sorted(summary["chosen_concepts"])
        summary["missing_periods"] = summary["total_periods"] - summary["matched_periods"]
        if summary["missing_periods"] > 0:
            summary["fallback_used"] = True

    return economic_df, mapping_summary


def _get_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return number


def _calc_tax_rate(pretax: float | None, tax: float | None, defaults: dict[str, Any]) -> float:
    if pretax is None or pretax == 0 or tax is None:
        return float(defaults.get("tax_rate_default", 0.25))
    rate = tax / pretax
    floor = float(defaults.get("tax_rate_floor", 0.0))
    cap = float(defaults.get("tax_rate_cap", 0.35))
    return min(max(rate, floor), cap)


def build_core_metrics(
    economic_df: pd.DataFrame,
    policy: dict[str, Any],
) -> pd.DataFrame:
    defaults = policy.get("defaults") or {}
    maintenance = policy.get("maintenance_capex_method") or {}
    floor_ratio = float(maintenance.get("floor_ratio", 0.8))
    capex_floor_ratio = float(maintenance.get("capex_floor_ratio", 0.5))
    capex_abs = bool(defaults.get("capex_abs", True))
    normalized_wc = float(defaults.get("normalized_wc", 0.0))

    metrics_rows: list[dict[str, Any]] = []
    for _, row in economic_df.iterrows():
        cfo = _get_number(row.get("cfo"))
        capex = _get_number(row.get("capex"))
        if capex is not None and capex_abs:
            capex = abs(capex)
        depr = _get_number(row.get("depreciation"))

        maint_candidates: list[float] = []
        if depr is not None:
            maint_candidates.append(depr * floor_ratio)
        if capex is not None:
            maint_candidates.append(capex * capex_floor_ratio)
        maintenance_capex = max(maint_candidates) if maint_candidates else None

        pretax = _get_number(row.get("pretax_income"))
        tax = _get_number(row.get("tax_expense"))
        tax_rate = _calc_tax_rate(pretax, tax, defaults)

        operating_income = _get_number(row.get("operating_income"))
        nopat = operating_income * (1 - tax_rate) if operating_income is not None else None

        total_assets = _get_number(row.get("total_assets"))
        cash = _get_number(row.get("cash"))
        nibcl = _get_number(row.get("non_interest_bearing_current_liabilities"))
        total_debt = _get_number(row.get("total_debt"))
        total_equity = _get_number(row.get("total_equity"))

        invested_source = None
        invested_capital = None
        if total_assets is not None:
            invested_source = "assets"
            invested_capital = total_assets - (cash or 0.0) - (nibcl or 0.0)
        elif total_debt is not None or total_equity is not None:
            invested_source = "debt_equity"
            invested_capital = (total_debt or 0.0) + (total_equity or 0.0) - (cash or 0.0)
        if invested_capital is not None and invested_capital <= 0:
            invested_capital = None

        roic = None
        if nopat is not None and invested_capital:
            roic = nopat / invested_capital

        fcf = None
        if cfo is not None or capex is not None:
            fcf = (cfo or 0.0) - (capex or 0.0)

        owner_earnings = None
        if cfo is not None or maintenance_capex is not None:
            owner_earnings = (cfo or 0.0) - (maintenance_capex or 0.0) + normalized_wc

        metrics_rows.append(
            {
                "period_end": row.get("period_end"),
                "fiscal_period": row.get("fiscal_period"),
                "revenue": _get_number(row.get("revenue")),
                "cfo": cfo,
                "capex": capex,
                "maintenance_capex": maintenance_capex,
                "fcf": fcf,
                "owner_earnings": owner_earnings,
                "operating_income": operating_income,
                "tax_rate": tax_rate,
                "nopat": nopat,
                "invested_capital": invested_capital,
                "invested_capital_source": invested_source,
                "roic": roic,
            }
        )

    return pd.DataFrame(metrics_rows)
