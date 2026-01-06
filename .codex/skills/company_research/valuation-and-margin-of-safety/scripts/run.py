#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""valuation-and-margin-of-safety skill runner (v0.1-phase1)."""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

SKILL_NAME = "valuation-and-margin-of-safety"
DEFAULT_TIMEZONE = "America/New_York"


def _find_repo_root(start: Path) -> Path:
    for parent in [start] + list(start.parents):
        if (parent / "company_research_runtime").exists():
            return parent
    return start.parents[4]


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from company_research_runtime import (  # noqa: E402
    append_evidence,
    atomic_write_text,
    atomic_write_yaml,
    build_needs,
    build_run_meta,
    build_run_result,
    company_paths,
    default_run_id,
    fingerprint_data,
    hash_file,
    update_artifacts_state,
    write_meta,
    write_needs,
    write_result,
)
from model import (  # noqa: E402
    compute_valuation,
    extract_latest_metrics,
    has_owner_earnings,
    load_policy,
    normalize_policy,
    select_owner_earnings_row,
)
from render_memo import render_investment_memo  # noqa: E402

DEFAULT_POLICY_PATH = Path(__file__).with_name("valuation_policy_phase1.yaml")
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "assets" / "investment_memo_template.md"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _parse_as_of(value: str | None) -> date | str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return value


def _as_of_str(value: date | str | None, *, tz: str) -> str:
    if value is None:
        return datetime.now(timezone.utc).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return number


def _generate_record_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"


def _append_evidence_record(
    *,
    evidence_path: Path,
    ticker: str,
    claim: str,
    sources: list[dict[str, Any]],
    confidence: float = 0.5,
) -> None:
    record = {
        "id": _generate_record_id("E"),
        "created_at": datetime.now(timezone.utc).date().isoformat(),
        "ticker": ticker,
        "skill": SKILL_NAME,
        "claim": claim,
        "confidence": confidence,
        "sources": sources,
    }
    append_evidence(evidence_path, record)


def _should_skip(
    *,
    force_refresh: bool,
    valuation_path: Path,
    outputs: list[Path],
    policy_hash: str,
    market_hash: str | None,
    core_hash: str | None,
    economic_hash: str | None,
    model_type: str,
) -> bool:
    if force_refresh:
        return False
    if not all(path.exists() for path in outputs):
        return False
    if not valuation_path.exists():
        return False
    existing = _load_yaml(valuation_path)
    inputs = existing.get("inputs") or {}
    if inputs.get("policy_hash") != policy_hash:
        return False
    if market_hash and inputs.get("market_snapshot_hash") != market_hash:
        return False
    if core_hash and inputs.get("core_metrics_hash") != core_hash:
        return False
    if economic_hash and inputs.get("economic_statements_hash") != economic_hash:
        return False
    methods = existing.get("methods") or {}
    if methods.get("model_type") != model_type:
        return False
    return True


def run(
    ticker: str,
    *,
    model_type: str | None = None,
    as_of: date | str | None = None,
    policy_path: Path | None = None,
    force_refresh: bool = False,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    ticker = ticker.upper()
    as_of_value = as_of or date.today()
    as_of_label = _as_of_str(as_of_value, tz=timezone_name)

    paths = company_paths(ticker)
    paths.ensure_base_dirs()

    run_id = default_run_id(timezone=timezone_name)
    run_dir = paths.run_dir(run_id)
    outputs_dir = run_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    meta = build_run_meta(
        skill=SKILL_NAME,
        ticker=ticker,
        run_id=run_id,
        as_of=as_of_value,
        timezone=timezone_name,
        model_type=model_type,
        force_refresh=force_refresh,
    )
    write_meta(run_dir, meta)

    warnings: list[str] = []
    missing: list[str] = []

    current_dir = paths.current_dir
    economic_dir = current_dir / "economic"
    valuation_dir = current_dir / "valuation"
    valuation_dir.mkdir(parents=True, exist_ok=True)

    market_path = current_dir / "market_snapshot.yaml"
    core_path = economic_dir / "core_metrics.parquet"
    economic_path = economic_dir / "economic_statements.parquet"

    required_missing = [path for path in [market_path, core_path, economic_path] if not path.exists()]
    if required_missing:
        missing = [str(path.relative_to(current_dir)) for path in required_missing]
        needs = build_needs(
            blocked_by=[
                {
                    "artifact": str(path.relative_to(current_dir)),
                    "producer_skill": "company-foundation"
                    if path == market_path
                    else "recast-economic-statements",
                    "reason": "Required input missing",
                }
                for path in required_missing
            ],
            suggested_plan=["company-foundation", "recast-economic-statements", SKILL_NAME],
            priority="high",
        )
        write_needs(run_dir, needs)
        result = build_run_result(
            skill=SKILL_NAME,
            ticker=ticker,
            run_id=run_id,
            status="blocked",
            as_of=as_of_value,
            timezone=timezone_name,
            missing=missing,
            warnings=warnings,
            outputs=[
                "current/valuation/valuation.yaml",
                "current/valuation/valuation_model.csv",
                "current/valuation/value_state.yaml",
                "current/valuation/investment_memo.md",
            ],
        )
        write_result(run_dir, result)
        return result

    market = _load_yaml(market_path)
    price = _as_float(market.get("price"))
    if price is None:
        missing.append("market_snapshot.yaml with price")
        needs = build_needs(
            blocked_by=[
                {
                    "artifact": "current/market_snapshot.yaml",
                    "producer_skill": "company-foundation",
                    "reason": "Missing price",
                }
            ],
            suggested_plan=["company-foundation", SKILL_NAME],
            priority="high",
        )
        write_needs(run_dir, needs)
        result = build_run_result(
            skill=SKILL_NAME,
            ticker=ticker,
            run_id=run_id,
            status="blocked",
            as_of=as_of_value,
            timezone=timezone_name,
            missing=missing,
            warnings=warnings,
            outputs=[
                "current/valuation/valuation.yaml",
                "current/valuation/valuation_model.csv",
                "current/valuation/value_state.yaml",
                "current/valuation/investment_memo.md",
            ],
        )
        write_result(run_dir, result)
        return result

    shares = _as_float(market.get("shares_outstanding"))
    market_cap_raw = _as_float(market.get("market_cap"))
    if not shares and market_cap_raw and price:
        shares = market_cap_raw / price
        warnings.append("shares_outstanding missing; derived from market_cap/price")

    if not shares or shares <= 0:
        missing.append("market_snapshot.yaml with shares_outstanding")
        needs = build_needs(
            blocked_by=[
                {
                    "artifact": "current/market_snapshot.yaml",
                    "producer_skill": "company-foundation",
                    "reason": "Missing shares_outstanding",
                }
            ],
            suggested_plan=["company-foundation", SKILL_NAME],
            priority="high",
        )
        write_needs(run_dir, needs)
        result = build_run_result(
            skill=SKILL_NAME,
            ticker=ticker,
            run_id=run_id,
            status="blocked",
            as_of=as_of_value,
            timezone=timezone_name,
            missing=missing,
            warnings=warnings,
            outputs=[
                "current/valuation/valuation.yaml",
                "current/valuation/valuation_model.csv",
                "current/valuation/value_state.yaml",
                "current/valuation/investment_memo.md",
            ],
        )
        write_result(run_dir, result)
        return result

    economic_df = pd.read_parquet(economic_path)
    if economic_df.empty:
        missing.append("economic_statements.parquet empty")
        needs = build_needs(
            blocked_by=[
                {
                    "artifact": "current/economic/economic_statements.parquet",
                    "producer_skill": "recast-economic-statements",
                    "reason": "Economic statements empty",
                }
            ],
            suggested_plan=["recast-economic-statements", SKILL_NAME],
            priority="high",
        )
        write_needs(run_dir, needs)
        result = build_run_result(
            skill=SKILL_NAME,
            ticker=ticker,
            run_id=run_id,
            status="blocked",
            as_of=as_of_value,
            timezone=timezone_name,
            missing=missing,
            warnings=warnings,
            outputs=[
                "current/valuation/valuation.yaml",
                "current/valuation/valuation_model.csv",
                "current/valuation/value_state.yaml",
                "current/valuation/investment_memo.md",
            ],
        )
        write_result(run_dir, result)
        return result

    core_df = pd.read_parquet(core_path)
    if core_df.empty or not has_owner_earnings(core_df):
        missing.append("core_metrics.parquet with owner_earnings")
        needs = build_needs(
            blocked_by=[
                {
                    "artifact": "current/economic/core_metrics.parquet",
                    "producer_skill": "recast-economic-statements",
                    "reason": "Missing owner_earnings",
                }
            ],
            suggested_plan=["recast-economic-statements", SKILL_NAME],
            priority="high",
        )
        write_needs(run_dir, needs)
        result = build_run_result(
            skill=SKILL_NAME,
            ticker=ticker,
            run_id=run_id,
            status="blocked",
            as_of=as_of_value,
            timezone=timezone_name,
            missing=missing,
            warnings=warnings,
            outputs=[
                "current/valuation/valuation.yaml",
                "current/valuation/valuation_model.csv",
                "current/valuation/value_state.yaml",
                "current/valuation/investment_memo.md",
            ],
        )
        write_result(run_dir, result)
        return result

    policy_path = policy_path or DEFAULT_POLICY_PATH
    policy = normalize_policy(load_policy(policy_path))
    policy_version = policy.get("version", "v0.1-phase1")
    model_type_resolved = model_type or policy.get("method_defaults", {}).get("model_type", "hybrid")
    if model_type_resolved not in {"epv", "dcf", "hybrid"}:
        warnings.append(f"invalid model_type '{model_type_resolved}'; using hybrid")
        model_type_resolved = "hybrid"

    policy_hash = fingerprint_data(policy)
    market_hash = hash_file(market_path) if market_path.exists() else None
    core_hash = hash_file(core_path) if core_path.exists() else None
    economic_hash = hash_file(economic_path) if economic_path.exists() else None

    output_paths = [
        valuation_dir / "valuation.yaml",
        valuation_dir / "valuation_model.csv",
        valuation_dir / "value_state.yaml",
        valuation_dir / "investment_memo.md",
    ]

    if _should_skip(
        force_refresh=force_refresh,
        valuation_path=output_paths[0],
        outputs=output_paths,
        policy_hash=policy_hash,
        market_hash=market_hash,
        core_hash=core_hash,
        economic_hash=economic_hash,
        model_type=model_type_resolved,
    ):
        result = build_run_result(
            skill=SKILL_NAME,
            ticker=ticker,
            run_id=run_id,
            status="skipped",
            as_of=as_of_value,
            timezone=timezone_name,
            missing=missing,
            warnings=warnings,
            outputs=[
                "current/valuation/valuation.yaml",
                "current/valuation/valuation_model.csv",
                "current/valuation/value_state.yaml",
                "current/valuation/investment_memo.md",
            ],
        )
        write_result(run_dir, result)
        return result

    latest_row = select_owner_earnings_row(core_df)
    metrics = extract_latest_metrics(latest_row)
    owner_earnings_raw = metrics.get("owner_earnings")
    owner_earnings_used = owner_earnings_raw
    if owner_earnings_used is None:
        missing.append("owner_earnings not available in latest core_metrics")
        needs = build_needs(
            blocked_by=[
                {
                    "artifact": "current/economic/core_metrics.parquet",
                    "producer_skill": "recast-economic-statements",
                    "reason": "Owner earnings missing in latest period",
                }
            ],
            suggested_plan=["recast-economic-statements", SKILL_NAME],
            priority="high",
        )
        write_needs(run_dir, needs)
        result = build_run_result(
            skill=SKILL_NAME,
            ticker=ticker,
            run_id=run_id,
            status="blocked",
            as_of=as_of_value,
            timezone=timezone_name,
            missing=missing,
            warnings=warnings,
            outputs=[
                "current/valuation/valuation.yaml",
                "current/valuation/valuation_model.csv",
                "current/valuation/value_state.yaml",
                "current/valuation/investment_memo.md",
            ],
        )
        write_result(run_dir, result)
        return result

    if owner_earnings_used <= 0:
        warnings.append("owner_earnings <= 0; using absolute value")
        owner_earnings_used = abs(owner_earnings_used) if owner_earnings_used != 0 else 1.0

    valuation = compute_valuation(
        owner_earnings=owner_earnings_used,
        price=price,
        shares=shares,
        policy=policy,
        model_type=model_type_resolved,
    )

    if not valuation["methods_used"]:
        warnings.append("no valuation methods selected; defaulting to EPV")
        valuation = compute_valuation(
            owner_earnings=owner_earnings_used,
            price=price,
            shares=shares,
            policy=policy,
            model_type="epv",
        )

    iv_per_share = valuation["intrinsic_value_per_share"]
    margin_of_safety = valuation["margin_of_safety"]

    market_cap = market_cap_raw
    if market_cap is None:
        market_cap = price * shares
    enterprise_value = _as_float(market.get("enterprise_value"))
    net_debt = _as_float(market.get("net_debt"))
    if enterprise_value is None and net_debt is not None:
        enterprise_value = market_cap + net_debt

    valuation_yaml = {
        "as_of": as_of_label,
        "policy_version": policy_version,
        "inputs": {
            "policy_path": str(policy_path),
            "policy_hash": policy_hash,
            "market_snapshot_hash": market_hash,
            "core_metrics_hash": core_hash,
            "economic_statements_hash": economic_hash,
        },
        "methods": {
            "model_type": model_type_resolved,
            "method_weights": valuation["weights"],
            "methods_used": valuation["methods_used"],
        },
        "assumptions": {
            "epv": {
                "multiple": policy.get("epv", {}).get("multiple", {}),
                "owner_earnings_adjustment": policy.get("owner_earnings", {}).get("adjustment", {}),
            },
            "dcf": {
                "years": policy.get("dcf", {}).get("years"),
                "growth": policy.get("dcf", {}).get("growth", {}),
                "discount_rate": policy.get("dcf", {}).get("discount_rate", {}),
                "terminal_multiple": policy.get("dcf", {}).get("terminal_multiple", {}),
            },
        },
        "results": {
            "owner_earnings_raw": owner_earnings_raw,
            "owner_earnings_base": owner_earnings_used,
            "owner_earnings_scenarios": valuation["owner_earnings_by_scenario"],
            "epv_value": valuation["epv_values"],
            "dcf_value": valuation["dcf_values"],
            "intrinsic_value": valuation["combined_values"],
            "intrinsic_value_per_share": iv_per_share,
            "margin_of_safety": margin_of_safety,
        },
        "notes": {
            "margin_of_safety_formula": valuation["mos_formula"],
        },
    }

    quality_policy = policy.get("quality", {})
    quality_components = {
        "financial_quality": None,
        "moat": None,
        "governance_capital_allocation": None,
        "balance_sheet_resilience": None,
    }

    value_state = {
        "ticker": ticker,
        "as_of": as_of_label,
        "market": {
            "price": price,
            "shares_outstanding": shares,
            "shares_float": market.get("shares_float"),
            "market_cap": market_cap,
            "enterprise_value": enterprise_value,
        },
        "profit": {
            "base_period": policy.get("owner_earnings", {}).get("base_period", "TTM"),
            "owner_earnings": owner_earnings_used,
            "owner_earnings_per_share": owner_earnings_used / shares if shares else None,
            "nopat": metrics.get("nopat"),
            "invested_capital": metrics.get("invested_capital"),
            "roic": metrics.get("roic"),
            "fcf": metrics.get("fcf"),
            "maintenance_capex_estimate": metrics.get("maintenance_capex"),
        },
        "quality": {
            "coefficient_base": quality_policy.get("coefficient_base"),
            "implied_multiple_base": policy.get("epv", {}).get("multiple", {}).get("base"),
            "discount_rate_base": policy.get("dcf", {}).get("discount_rate", {}).get("base"),
            "components": quality_components,
            "confidence": quality_policy.get("confidence"),
        },
        "valuation": {
            "intrinsic_value_per_share": iv_per_share,
            "margin_of_safety_base": margin_of_safety.get("base"),
        },
        "links": {
            "memo": "current/valuation/investment_memo.md",
            "valuation_yaml": "current/valuation/valuation.yaml",
        },
    }

    verdict = "undervalued"
    mos_base = margin_of_safety.get("base", 0.0)
    if mos_base <= 0:
        verdict = "overvalued"
    elif mos_base <= 0.2:
        verdict = "fairly valued"

    summary = (
        f"{ticker} appears {verdict} with base IV of ${iv_per_share.get('base', 0.0):.2f} "
        f"and base MOS of {mos_base * 100:.1f}%."
    )

    memo_context = {
        "ticker": ticker,
        "as_of": as_of_label,
        "price": f"{price:.2f}",
        "mos_base_pct": f"{mos_base * 100:.1f}%",
        "summary": summary,
        "owner_earnings": f"${owner_earnings_used / 1e6:.1f}M",
        "owner_earnings_per_share": f"${owner_earnings_used / shares:.2f}",
        "roic": f"{(metrics.get('roic') or 0.0) * 100:.1f}%",
        "fcf": f"${(metrics.get('fcf') or 0.0) / 1e6:.1f}M",
        "iv_bear": f"${iv_per_share.get('bear', 0.0):.2f}",
        "iv_base": f"${iv_per_share.get('base', 0.0):.2f}",
        "iv_bull": f"${iv_per_share.get('bull', 0.0):.2f}",
        "mos_bear_pct": f"{margin_of_safety.get('bear', 0.0) * 100:.1f}%",
        "mos_bull_pct": f"{margin_of_safety.get('bull', 0.0) * 100:.1f}%",
        "epv_multiple_base": f"{policy.get('epv', {}).get('multiple', {}).get('base', 0)}",
        "discount_rate_base": f"{policy.get('dcf', {}).get('discount_rate', {}).get('base', 0.0) * 100:.1f}%",
        "growth_base": f"{policy.get('dcf', {}).get('growth', {}).get('base', 0.0) * 100:.1f}%",
        "terminal_multiple_base": f"{policy.get('dcf', {}).get('terminal_multiple', {}).get('base', 0)}",
        "phase1_notice": "Using default policy inputs; quality and growth drivers not yet evidence-backed.",
    }
    memo = render_investment_memo(template_path=TEMPLATE_PATH, context=memo_context)

    model_frame = valuation["model_frame"]
    model_csv = model_frame.to_csv(index=False, lineterminator="\n")

    atomic_write_yaml(outputs_dir / "valuation.yaml", valuation_yaml)
    atomic_write_yaml(outputs_dir / "value_state.yaml", value_state)
    atomic_write_text(outputs_dir / "investment_memo.md", memo)
    atomic_write_text(outputs_dir / "valuation_model.csv", model_csv)

    atomic_write_yaml(output_paths[0], valuation_yaml)
    atomic_write_yaml(output_paths[2], value_state)
    atomic_write_text(output_paths[3], memo)
    atomic_write_text(output_paths[1], model_csv)

    status = "ok" if not warnings else "partial"

    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/valuation/valuation.yaml",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=output_paths[0],
        extra={"status": status},
    )
    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/valuation/value_state.yaml",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=output_paths[2],
        extra={"status": status},
    )
    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/valuation/investment_memo.md",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=output_paths[3],
        extra={"status": status},
    )
    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/valuation/valuation_model.csv",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=output_paths[1],
        extra={"status": status, "count": len(model_frame)},
    )

    if status in {"ok", "partial"}:
        _append_evidence_record(
            evidence_path=paths.evidence_jsonl,
            ticker=ticker,
            claim=(
                f"Valuation completed: IV_base=${iv_per_share.get('base', 0.0):.2f}, "
                f"MOS_base={mos_base * 100:.1f}%"
            ),
            sources=[
                {"type": "market_snapshot", "path": str(market_path)},
                {"type": "core_metrics", "path": str(core_path)},
            ],
            confidence=0.5,
        )

    result = build_run_result(
        skill=SKILL_NAME,
        ticker=ticker,
        run_id=run_id,
        status=status,
        as_of=as_of_value,
        timezone=timezone_name,
        missing=missing,
        warnings=warnings,
        outputs=[
            "current/valuation/valuation.yaml",
            "current/valuation/valuation_model.csv",
            "current/valuation/value_state.yaml",
            "current/valuation/investment_memo.md",
        ],
    )
    write_result(run_dir, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="valuation-and-margin-of-safety runner")
    parser.add_argument("ticker", help="Stock ticker symbol")
    parser.add_argument("--model-type", choices=["epv", "dcf", "hybrid"], default=None)
    parser.add_argument("--as-of", dest="as_of", help="Snapshot date YYYY-MM-DD")
    parser.add_argument("--policy-path", type=Path)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    args = parser.parse_args()

    run(
        args.ticker,
        model_type=args.model_type,
        as_of=_parse_as_of(args.as_of),
        policy_path=args.policy_path,
        force_refresh=args.force_refresh,
        timezone_name=args.timezone,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
