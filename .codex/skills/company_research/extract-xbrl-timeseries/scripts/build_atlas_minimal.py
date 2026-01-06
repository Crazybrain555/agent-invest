#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a minimal Statement Atlas from SEC financials payloads (v0.1)."""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd


def _find_repo_root(start: Path) -> Path:
    for parent in [start] + list(start.parents):
        if (parent / "company_research_runtime").exists():
            return parent
    return start.parents[4]


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from company_research_runtime import hash_text  # noqa: E402

FACT_COLUMNS = [
    "period_end",
    "fiscal_period",
    "statement_type",
    "role_uri",
    "concept",
    "label",
    "value",
    "unit",
    "decimals",
    "accession",
    "context_id",
    "fact_id",
    "dimensions",
]
NODE_COLUMNS = [
    "node_id",
    "statement_type",
    "role_uri",
    "concept",
    "label",
    "depth",
    "order",
]
EDGE_COLUMNS = [
    "parent_node_id",
    "child_node_id",
    "arcrole",
    "weight",
]
PATH_COLUMNS = [
    "node_id",
    "period_end",
    "statement_type",
    "path_str",
    "value",
    "accession",
]

STATEMENT_LABELS = {
    "IS": "Income Statement",
    "BS": "Balance Sheet",
    "CF": "Cash Flow",
    "CI": "Comprehensive Income",
    "Equity": "Equity",
    "OTHER": "Other Statement",
}

STATEMENT_TYPE_MAP = {
    "is": "IS",
    "bs": "BS",
    "cf": "CF",
    "ci": "CI",
    "equity": "Equity",
    "other": "OTHER",
    "income_statement": "IS",
    "income": "IS",
    "statement_of_income": "IS",
    "statement_of_operations": "IS",
    "profit_loss": "IS",
    "balance_sheet": "BS",
    "statement_of_financial_position": "BS",
    "financial_position": "BS",
    "cash_flow": "CF",
    "cash_flow_statement": "CF",
    "statement_of_cash_flows": "CF",
    "cash_flows": "CF",
    "comprehensive_income": "CI",
    "statement_of_comprehensive_income": "CI",
    "equity": "Equity",
    "statement_of_equity": "Equity",
    "shareholders_equity": "Equity",
}


def _normalize_key(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def slugify(text: str) -> str:
    return _normalize_key(text)


def normalize_statement_type(value: str | None) -> str | None:
    if not value:
        return None
    key = _normalize_key(value)
    if key in STATEMENT_TYPE_MAP:
        return STATEMENT_TYPE_MAP[key]
    if value in {"IS", "BS", "CF", "CI", "Equity", "OTHER"}:
        return value
    return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    candidate = str(value)
    if len(candidate) >= 10 and candidate[4] == "-":
        try:
            return date.fromisoformat(candidate[:10])
        except ValueError:
            return None
    if len(candidate) == 8 and candidate.isdigit():
        try:
            return date(int(candidate[:4]), int(candidate[4:6]), int(candidate[6:]))
        except ValueError:
            return None
    return None


def _as_of_date(value: date | str | None) -> date:
    if isinstance(value, date):
        return value
    parsed = _parse_date(str(value)) if value else None
    return parsed or date.today()


def _coerce_number(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        negative = text.startswith("(") and text.endswith(")")
        text = text.strip("()")
        text = text.replace(",", "")
        try:
            number = float(text)
        except ValueError:
            return value
        return -number if negative else number
    return value


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _first(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _normalize_accession(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("accession") or value.get("accession_number")
    text = str(value).strip()
    return text or None


def _normalize_dimensions(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True)
    except TypeError:
        return str(value)


def _build_accession_period_map(filings_index: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(filings_index, dict):
        return {}
    filings = filings_index.get("filings")
    if not isinstance(filings, list):
        return {}
    mapping: dict[str, str] = {}
    for filing in filings:
        if not isinstance(filing, dict):
            continue
        accession = _normalize_accession(filing.get("accession") or filing.get("accession_number"))
        period_end = filing.get("period_end") or filing.get("periodOfReport")
        if accession and period_end:
            mapping[accession] = str(period_end)
    return mapping


def _extract_items(statement_payload: Any) -> list[dict[str, Any]]:
    if isinstance(statement_payload, list):
        return [item for item in statement_payload if isinstance(item, dict)]
    if isinstance(statement_payload, dict):
        for key in ("line_items", "lineItems", "items", "data", "rows"):
            value = statement_payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        for value in statement_payload.values():
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _iter_statement_payloads(payload: Any) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = []
    if payload is None:
        return statements
    if isinstance(payload, list):
        if all(isinstance(item, dict) for item in payload):
            if any(
                key in item
                for item in payload
                for key in ("statement_type", "statement", "statementType", "type")
            ):
                grouped: dict[str, list[dict[str, Any]]] = {}
                for item in payload:
                    statement_type = _first(
                        item.get("statement_type"),
                        item.get("statement"),
                        item.get("statementType"),
                        item.get("type"),
                    )
                    statement_key = statement_type or "unknown"
                    grouped.setdefault(str(statement_key), []).append(item)
                for statement_type, items in grouped.items():
                    statements.append({
                        "statement_type": statement_type,
                        "payload": items,
                        "meta": {},
                    })
                return statements
        statements.append({"statement_type": None, "payload": payload, "meta": {}})
        return statements

    if isinstance(payload, dict):
        for key in ("statements", "financials", "data"):
            value = payload.get(key)
            if isinstance(value, dict):
                for subkey, subval in value.items():
                    statements.append({
                        "statement_type": subkey,
                        "payload": subval,
                        "meta": payload,
                    })
            elif isinstance(value, list) and key == "data":
                statements.extend(_iter_statement_payloads(value))
        if statements:
            return statements
        for key, value in payload.items():
            if normalize_statement_type(key):
                statements.append({
                    "statement_type": key,
                    "payload": value,
                    "meta": payload,
                })
        if statements:
            return statements
        if any(key in payload for key in ("line_items", "items", "rows")):
            statements.append({
                "statement_type": payload.get("statement_type") or payload.get("statement"),
                "payload": payload,
                "meta": payload,
            })
    return statements


def _iter_item_values(item: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for key in ("values", "periods", "data"):
        value = item.get(key)
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    yield entry
            return
        if isinstance(value, dict):
            for period_end, amount in value.items():
                yield {"period_end": period_end, "value": amount}
            return
    yield item


def _infer_fiscal_period(period_end: str | None) -> str | None:
    if not period_end:
        return None
    parsed = _parse_date(period_end)
    if not parsed:
        return None
    if parsed.month == 12:
        return "FY"
    return None


def _build_fact_id(
    *,
    accession: str | None,
    period_end: str | None,
    statement_type: str,
    concept: str,
    label: str,
) -> str:
    key = "|".join([
        accession or "",
        period_end or "",
        statement_type,
        concept,
        label,
    ])
    return f"fact_{hash_text(key)[:16]}"


def build_minimal_atlas(
    financials_payloads: Iterable[Any],
    *,
    filings_index: Mapping[str, Any] | None = None,
    lookback_years: int = 10,
    as_of: date | str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any], list[str]]:
    warnings: list[str] = []
    accession_period_map = _build_accession_period_map(filings_index)
    cutoff = _as_of_date(as_of) - timedelta(days=lookback_years * 365)

    facts: list[dict[str, Any]] = []
    label_order: dict[str, list[str]] = {}

    for payload in financials_payloads:
        for statement in _iter_statement_payloads(payload):
            statement_type_raw = _first(
                statement.get("statement_type"),
                statement.get("meta", {}).get("statement_type"),
                statement.get("meta", {}).get("statement"),
            )
            statement_type = normalize_statement_type(str(statement_type_raw)) if statement_type_raw else None
            items = _extract_items(statement.get("payload"))
            if not items:
                continue

            statement_meta = statement.get("payload") if isinstance(statement.get("payload"), dict) else {}
            statement_accession = _normalize_accession(
                _first(
                    statement_meta.get("accession"),
                    statement_meta.get("accession_number"),
                    statement.get("meta", {}).get("accession"),
                )
            )
            statement_period_end = _first(
                statement_meta.get("period_end"),
                statement_meta.get("periodOfReport"),
                statement_meta.get("period_end_date"),
                statement.get("meta", {}).get("period_end"),
            )
            statement_fiscal_period = _first(
                statement_meta.get("fiscal_period"),
                statement_meta.get("fiscalPeriod"),
                statement_meta.get("fp"),
            )
            statement_role = _first(
                statement_meta.get("role_uri"),
                statement_meta.get("role"),
                statement_meta.get("roleUri"),
            )

            for item in items:
                item_statement_type = normalize_statement_type(
                    _first(
                        item.get("statement_type"),
                        item.get("statement"),
                        item.get("statementType"),
                        item.get("type"),
                        statement_type,
                    )
                )
                if not item_statement_type:
                    warnings.append("Unknown statement_type for some items")
                    continue

                label = _first(
                    item.get("label"),
                    item.get("name"),
                    item.get("description"),
                    item.get("line_item"),
                    item.get("account"),
                )
                if not label:
                    warnings.append("Missing label for some line items")
                    continue

                label = str(label)
                label_order.setdefault(item_statement_type, [])
                if label not in label_order[item_statement_type]:
                    label_order[item_statement_type].append(label)

                for value_entry in _iter_item_values(item):
                    accession = _normalize_accession(
                        _first(
                            value_entry.get("accession"),
                            value_entry.get("accession_number"),
                            item.get("accession"),
                            statement_accession,
                        )
                    )
                    period_end = _first(
                        value_entry.get("period_end"),
                        value_entry.get("period_end_date"),
                        value_entry.get("periodOfReport"),
                        item.get("period_end"),
                        item.get("period_end_date"),
                        statement_period_end,
                        accession_period_map.get(accession) if accession else None,
                    )
                    period_end = str(period_end) if period_end else None

                    parsed_period_end = _parse_date(period_end) if period_end else None
                    if parsed_period_end and parsed_period_end < cutoff:
                        continue

                    concept = _first(
                        value_entry.get("concept"),
                        value_entry.get("tag"),
                        value_entry.get("xbrl_tag"),
                        item.get("concept"),
                        item.get("tag"),
                        item.get("xbrl_tag"),
                    )
                    concept = str(concept) if concept else f"synthetic:{slugify(label)}"

                    value = _coerce_number(_first(value_entry.get("value"), item.get("value"), value_entry.get("amount")))
                    unit = _first(value_entry.get("unit"), item.get("unit"), item.get("units")) or "USD"
                    decimals = _coerce_int(_first(value_entry.get("decimals"), item.get("decimals")))
                    fiscal_period = _first(
                        value_entry.get("fiscal_period"),
                        value_entry.get("fp"),
                        item.get("fiscal_period"),
                        statement_fiscal_period,
                        _infer_fiscal_period(period_end),
                    )
                    role_uri = _first(
                        value_entry.get("role_uri"),
                        item.get("role_uri"),
                        statement_role,
                    )
                    context_id = _first(
                        value_entry.get("context_id"),
                        value_entry.get("contextId"),
                        item.get("context_id"),
                        item.get("contextId"),
                    )
                    dimensions = _normalize_dimensions(
                        _first(value_entry.get("dimensions"), item.get("dimensions"), item.get("axes"))
                    )

                    fact_id = _build_fact_id(
                        accession=accession,
                        period_end=period_end,
                        statement_type=item_statement_type,
                        concept=concept,
                        label=label,
                    )

                    facts.append(
                        {
                            "period_end": period_end,
                            "fiscal_period": fiscal_period,
                            "statement_type": item_statement_type,
                            "role_uri": role_uri,
                            "concept": concept,
                            "label": label,
                            "value": value,
                            "unit": unit,
                            "decimals": decimals,
                            "accession": accession,
                            "context_id": context_id,
                            "fact_id": fact_id,
                            "dimensions": dimensions,
                        }
                    )

    facts_df = pd.DataFrame(facts, columns=FACT_COLUMNS)

    nodes, edges, node_map = _build_shallow_tree(facts_df, label_order)
    nodes_df = pd.DataFrame(nodes, columns=NODE_COLUMNS)
    edges_df = pd.DataFrame(edges, columns=EDGE_COLUMNS)
    paths_df = _build_paths(facts_df, node_map)
    periods_payload = _build_periods(facts_df)

    return facts_df, nodes_df, edges_df, paths_df, periods_payload, warnings


def _build_shallow_tree(
    facts_df: pd.DataFrame,
    label_order: Mapping[str, list[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, str], str]]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_map: dict[tuple[str, str], str] = {}

    statement_types = sorted(
        {*label_order.keys(), *facts_df.get("statement_type", pd.Series(dtype=str)).dropna().tolist()}
    )
    if not statement_types:
        statement_types = ["IS", "BS", "CF"]

    concept_map: dict[tuple[str, str], str] = {}
    if not facts_df.empty and "concept" in facts_df.columns:
        grouped = facts_df.dropna(subset=["label"]).groupby(["statement_type", "label"], sort=False)
        for (statement_type, label), group in grouped:
            concept_series = group.get("concept").dropna() if "concept" in group else None
            if concept_series is not None and not concept_series.empty:
                concept_map[(statement_type, label)] = str(concept_series.iloc[0])

    for statement_type in statement_types:
        root_id = f"{statement_type}_root"
        nodes.append(
            {
                "node_id": root_id,
                "statement_type": statement_type,
                "role_uri": None,
                "concept": root_id,
                "label": STATEMENT_LABELS.get(statement_type, statement_type),
                "depth": 0,
                "order": 0,
            }
        )

        labels = label_order.get(statement_type, [])
        if not labels and not facts_df.empty:
            labels = [
                label
                for label in facts_df.loc[facts_df["statement_type"] == statement_type, "label"].dropna().unique()
            ]
        used_ids: set[str] = set()
        for order, label in enumerate(labels, start=1):
            base_id = f"{statement_type}_{slugify(label)}"
            node_id = base_id
            suffix = 1
            while node_id in used_ids:
                suffix += 1
                node_id = f"{base_id}_{suffix}"
            used_ids.add(node_id)
            node_map[(statement_type, label)] = node_id
            concept = concept_map.get((statement_type, label), base_id)
            nodes.append(
                {
                    "node_id": node_id,
                    "statement_type": statement_type,
                    "role_uri": None,
                    "concept": concept,
                    "label": label,
                    "depth": 1,
                    "order": order,
                }
            )
            edges.append(
                {
                    "parent_node_id": root_id,
                    "child_node_id": node_id,
                    "arcrole": "presentation",
                    "weight": 1.0,
                }
            )

    return nodes, edges, node_map


def _build_paths(facts_df: pd.DataFrame, node_map: Mapping[tuple[str, str], str]) -> pd.DataFrame:
    if facts_df.empty:
        return pd.DataFrame(columns=PATH_COLUMNS)

    records: list[dict[str, Any]] = []
    for _, row in facts_df.iterrows():
        statement_type = row.get("statement_type")
        label = row.get("label")
        node_id = None
        if statement_type and label:
            node_id = node_map.get((statement_type, label))
        if not node_id:
            node_id = f"{statement_type}_{slugify(str(label))}" if label else None
        records.append(
            {
                "node_id": node_id,
                "period_end": row.get("period_end"),
                "statement_type": statement_type,
                "path_str": f"{statement_type}/{label}" if statement_type and label else None,
                "value": row.get("value"),
                "accession": row.get("accession"),
            }
        )
    return pd.DataFrame(records, columns=PATH_COLUMNS)


def _build_periods(facts_df: pd.DataFrame) -> dict[str, Any]:
    periods: list[dict[str, Any]] = []
    if facts_df.empty:
        return {"periods": periods}

    grouped = facts_df.dropna(subset=["period_end"]).groupby("period_end", sort=True)
    for period_end, group in grouped:
        accession = None
        fiscal_period = None
        if "accession" in group.columns:
            accession_series = group["accession"].dropna()
            accession = accession_series.iloc[0] if not accession_series.empty else None
        if "fiscal_period" in group.columns:
            fiscal_series = group["fiscal_period"].dropna()
            fiscal_period = fiscal_series.iloc[0] if not fiscal_series.empty else None
        periods.append(
            {
                "period_end": str(period_end),
                "fiscal_period": fiscal_period,
                "accession": accession,
            }
        )

    periods.sort(key=lambda item: item.get("period_end") or "", reverse=True)
    return {"periods": periods}


__all__ = ["build_minimal_atlas", "slugify", "normalize_statement_type"]
