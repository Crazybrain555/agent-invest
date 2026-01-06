from __future__ import annotations

import re
from typing import List, Optional


def short_factor_name(name: str) -> str:
    return name.split("::", 1)[-1] if "::" in name else name


def _join_items(items: List[str], max_items: int = 3) -> str:
    if not items:
        return ""
    if len(items) <= max_items:
        return "+".join(items)
    return "+".join(items[:max_items]) + f"+{len(items) - max_items}more"


def _sanitize_tag(tag: str, max_len: int = 80) -> str:
    if not tag:
        return "all_factors"
    tag = tag.strip().replace(" ", "_")
    tag = re.sub(r"[^A-Za-z0-9._+-]+", "_", tag)
    tag = tag.strip("._-")
    if not tag:
        return "all_factors"
    if len(tag) <= max_len:
        return tag
    return tag[:max_len].rstrip("._-")


def build_group_tag(group_specs: List[str]) -> str:
    group_tag = _join_items(sorted({g.strip() for g in group_specs if g.strip()}))
    return _sanitize_tag(group_tag) if group_tag else "all_groups"


def build_focus_tag(
    mode: str,
    group_specs: List[str],
    factor_specs: List[str],
    target_factor_key: Optional[str],
    candidate_keys: List[str],
    factor_keys: List[str],
) -> str:
    group_tag = _join_items(sorted({g.strip() for g in group_specs if g.strip()}))
    if mode == "pair" and len(candidate_keys) >= 2:
        left = short_factor_name(candidate_keys[0])
        right = short_factor_name(candidate_keys[1])
        return _sanitize_tag(f"{left}_vs_{right}")
    if mode == "one_to_many" and target_factor_key:
        target = short_factor_name(target_factor_key)
        if group_tag:
            return _sanitize_tag(f"{target}_vs_{group_tag}")
        if candidate_keys:
            return _sanitize_tag(f"{target}_vs_{len(candidate_keys)}factors")
        return _sanitize_tag(f"{target}_vs_others")
    if group_tag:
        return _sanitize_tag(group_tag)
    if factor_specs:
        specs = [short_factor_name(spec) for spec in factor_specs if spec.strip()]
        return _sanitize_tag(_join_items(specs))
    return _sanitize_tag(f"{len(factor_keys)}factors")


def build_run_dir_name(focus_tag: str, mode: str, ts: str) -> str:
    return f"{focus_tag}__{mode}__{ts}"
