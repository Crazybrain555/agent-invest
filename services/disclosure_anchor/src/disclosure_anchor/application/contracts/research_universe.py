"""Frozen operator contract for the deployment research-priority universe."""

from __future__ import annotations

from typing import Any


SCREEN_SCHEMA = "public_a_share_screen_input.v6"
MANIFEST_SCHEMA = "research_priority_universe.v5"
PURPOSE = "research_priority_universe"
SELECTION_RULE_VERSION = "a_share_research_priority.v10"
CNINFO_BOARD_BY_MARKET_CODE = {
    "012001": "SSE_MAIN",
    "012002": "SZSE_MAIN",
    "012015": "CHINEXT",
    "012029": "STAR",
    "012046": "BSE",
}
RESEARCH_BOARDS = frozenset({"SSE_MAIN", "SZSE_MAIN", "STAR", "CHINEXT"})
EXCLUSION_REASONS = frozenset(
    {
        "cninfo_not_a_share",
        "cninfo_not_normal_listed",
        "fewer_than_two_positive_parent_profit_years",
        "latest_parent_profit_nonpositive",
        "listing_age_lt_3_5y",
        "market_cap_lt_2bn",
        "missing_annual_financial",
        "missing_cninfo_identity",
        "missing_listing_date",
        "missing_or_low_2025_bps",
        "missing_or_low_2025_roe",
        "outside_research_boards",
        "no_future_research_signal",
        "risk_warning_name",
        "unsupported_exchange",
    }
)
EXPECTED_RULES: dict[str, Any] = {
    "markets": ["SSE", "SZSE"],
    "boards": ["SSE_MAIN", "SZSE_MAIN", "STAR", "CHINEXT"],
    "board_source": "CNINFO_F004V",
    "security_type": "A股",
    "normal_listing_required": True,
    "risk_warning_name_excluded": True,
    "minimum_listing_age_months": 42,
    "listing_date_on_or_before": "2023-02-23",
    "market_cap_min_cny": 2_000_000_000,
    "current_trade_price_required": False,
    "annual_years": [2023, 2024, 2025],
    "complete_annual_profit_revenue_roe_required": True,
    "latest_parent_profit_positive_required": True,
    "minimum_positive_parent_profit_years": 2,
    "future_research_signals": [
        "revenue_cagr_meets_floor",
        "parent_profit_cagr_meets_floor_and_base_quality",
        "latest_parent_profit_growth_meets_floor_and_base_quality",
        "durable_quality_compounder",
        "profitable_turnaround_with_quality_floor",
    ],
    "revenue_cagr_2023_to_2025_min_ratio": 0.05,
    "parent_profit_cagr_2023_to_2025_min_ratio": 0.10,
    "parent_profit_growth_2024_to_2025_min_ratio": 0.10,
    "profit_signal_prior_year_net_margin_min_ratio": 0.01,
    "durable_quality_average_roe_min_pct": 15.0,
    "durable_quality_revenue_cagr_min_ratio": 0.02,
    "durable_quality_parent_profit_cagr_min_ratio": 0.02,
    "profitable_turnaround_roe_2025_min_pct": 8.0,
    "turnaround_continuity_required": True,
    "roe_2025_min_pct": 5.0,
    "bps_2025_min_cny": 1.0,
    "ranking": "future_evidence_strength_then_growth_quality_then_security_code",
    "csv_order_semantics": "deterministic_audit_presentation_not_runtime_scheduling",
    "selection_count_min": 1_400,
    "selection_count_max": 1_600,
    "selection_count_semantics": "threshold_outcome_not_forced_fill",
}
EVIDENCE_LIMITATIONS = [
    "audit_opinion_quality_not_screened",
    "regulatory_penalty_history_not_screened",
    "historical_liquidity_stability_not_screened",
    "management_integrity_not_screened",
    "governance_score_not_screened",
    "accounting_restatement_risk_not_screened",
    "state_ownership_not_used_as_governance_proxy",
    "forward_consensus_estimates_not_screened",
    "industry_runway_and_catalysts_not_screened",
    "valuation_and_price_momentum_not_used_for_company_potential",
    "signals_are_realized_backward_looking_annual_accounting_not_forecasts",
    "industry_cycle_classification_not_screened",
    "snapshot_is_not_point_in_time_and_must_not_be_used_for_backtesting",
    "screen_is_research_priority_not_an_investment_recommendation",
]


__all__ = [
    "CNINFO_BOARD_BY_MARKET_CODE",
    "EVIDENCE_LIMITATIONS",
    "EXCLUSION_REASONS",
    "EXPECTED_RULES",
    "MANIFEST_SCHEMA",
    "PURPOSE",
    "RESEARCH_BOARDS",
    "SCREEN_SCHEMA",
    "SELECTION_RULE_VERSION",
]
