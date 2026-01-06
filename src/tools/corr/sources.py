from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml


def normalize_table_name(table: str) -> str:
    table = table.strip()
    if "." not in table:
        return f"ai_is.{table}"
    return table


def load_factor_mapping(mapping_path: Path) -> Dict[str, Any]:
    with mapping_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data or {}


def iter_groups(mapping: Dict[str, Any]) -> Iterable[Tuple[str, str, List[str]]]:
    for level1, level2_map in mapping.items():
        if not isinstance(level2_map, dict):
            continue
        for level2, factors in level2_map.items():
            if not isinstance(factors, list):
                continue
            yield level1, level2, factors


def build_group_table_name(level1: str, level2: str) -> str:
    return f"ai_is.quantitative_{level1}_{level2}_signals"


def build_table_factor_map(
    mapping: Dict[str, Any],
    include_tables: Iterable[str],
    exclude_tables: Iterable[str],
    groups: Optional[Iterable[str]] = None,
) -> Tuple[Dict[str, List[str]], List[str]]:
    include_set = {normalize_table_name(t) for t in include_tables}
    exclude_set = {normalize_table_name(t) for t in exclude_tables}
    group_set = {g.strip() for g in groups} if groups else None

    table_map: Dict[str, List[str]] = defaultdict(list)
    skipped: List[str] = []

    for level1, level2, factors in iter_groups(mapping):
        group_name = f"{level1}.{level2}"
        if group_set and group_name not in group_set:
            continue
        table = normalize_table_name(build_group_table_name(level1, level2))
        if table in exclude_set or (include_set and table not in include_set):
            skipped.append(table)
            continue
        seen = set(table_map[table])
        for factor in factors:
            if factor not in seen:
                table_map[table].append(factor)
                seen.add(factor)

    return dict(table_map), skipped


def build_factor_index(
    mapping: Dict[str, Any],
    include_tables: Iterable[str],
    exclude_tables: Iterable[str],
) -> Dict[str, List[str]]:
    include_set = {normalize_table_name(t) for t in include_tables}
    exclude_set = {normalize_table_name(t) for t in exclude_tables}
    index: Dict[str, List[str]] = defaultdict(list)

    for level1, level2, factors in iter_groups(mapping):
        table = normalize_table_name(build_group_table_name(level1, level2))
        if table in exclude_set or (include_set and table not in include_set):
            continue
        for factor in factors:
            index[factor].append(table)

    return dict(index)


def resolve_factor_specs(
    specs: Iterable[str],
    factor_index: Dict[str, List[str]],
    strict_name: bool,
    include_tables: Iterable[str],
    exclude_tables: Iterable[str],
) -> Tuple[Dict[str, List[str]], List[str], List[str]]:
    include_set = {normalize_table_name(t) for t in include_tables}
    exclude_set = {normalize_table_name(t) for t in exclude_tables}

    table_map: Dict[str, List[str]] = defaultdict(list)
    missing: List[str] = []
    ambiguous: List[str] = []

    for spec in specs:
        spec = spec.strip()
        if not spec:
            continue
        if "::" in spec:
            table, field = spec.split("::", 1)
            table = normalize_table_name(table)
            if table in exclude_set or (include_set and table not in include_set):
                missing.append(spec)
                continue
            if field not in table_map[table]:
                table_map[table].append(field)
            continue

        tables = factor_index.get(spec, [])
        if not tables:
            missing.append(spec)
            continue
        if strict_name and len(tables) > 1:
            ambiguous.append(spec)
            continue
        for table in tables:
            if table not in table_map:
                table_map[table] = []
            if spec not in table_map[table]:
                table_map[table].append(spec)

    if ambiguous:
        raise ValueError(f"Ambiguous factor names: {', '.join(sorted(set(ambiguous)))}")

    return dict(table_map), missing, []


def merge_table_maps(base: Dict[str, List[str]], extra: Dict[str, List[str]]) -> Dict[str, List[str]]:
    merged: Dict[str, List[str]] = {k: list(v) for k, v in base.items()}
    for table, factors in extra.items():
        if table not in merged:
            merged[table] = list(factors)
            continue
        seen = set(merged[table])
        for factor in factors:
            if factor not in seen:
                merged[table].append(factor)
                seen.add(factor)
    return merged


def flatten_table_factors(table_factors: Dict[str, List[str]]) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for table, factors in table_factors.items():
        for factor in factors:
            pairs.append((table, factor))
    return pairs


def build_factor_meta(table_factors: Dict[str, List[str]]) -> Dict[str, Dict[str, str]]:
    meta: Dict[str, Dict[str, str]] = {}
    for table, factors in table_factors.items():
        for factor in factors:
            key = f"{table}::{factor}"
            meta[key] = {"source_table": table, "field_name": factor}
    return meta
