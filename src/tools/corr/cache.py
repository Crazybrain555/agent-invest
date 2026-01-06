from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd


def _align_frames(existing: pd.DataFrame, incoming: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if existing.empty or incoming.empty:
        return existing, incoming

    schema: Dict[str, Any] = {}
    for col in existing.columns:
        schema[col] = existing[col].dtype
    for col in incoming.columns:
        if col not in schema:
            schema[col] = incoming[col].dtype

    def _apply_schema(df: pd.DataFrame) -> pd.DataFrame:
        aligned = df.copy()
        for col, dtype in schema.items():
            if col not in aligned.columns:
                aligned[col] = pd.Series([pd.NA] * len(aligned), dtype=dtype)
                continue
            if aligned[col].dtype == dtype:
                continue
            try:
                aligned[col] = aligned[col].astype(dtype)
            except Exception:  # noqa: BLE001
                if pd.api.types.is_numeric_dtype(dtype):
                    aligned[col] = pd.to_numeric(aligned[col], errors="coerce")
        ordered_cols = list(schema.keys())
        return aligned[ordered_cols]

    return _apply_schema(existing), _apply_schema(incoming)


def _stable_hash(payload: Any) -> str:
    normalized = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_signature(
    tool_version: Any,
    include_tables: Iterable[str],
    sampling_cfg: Dict[str, Any],
    universe_cfg: Dict[str, Any],
    method: str,
    corr_type: str,
    min_periods: int,
    factor_keys: List[str],
) -> Dict[str, Any]:
    include_tables_hash = _stable_hash(sorted(include_tables))
    sampling_hash = _stable_hash(sampling_cfg)
    universe_hash = _stable_hash(universe_cfg)

    payload = {
        "tool_version": tool_version,
        "include_tables_hash": include_tables_hash,
        "sampling_hash": sampling_hash,
        "universe_hash": universe_hash,
        "method": method,
        "corr_type": corr_type,
        "min_periods": min_periods,
        "factor_keys": sorted(factor_keys),
    }
    signature = _stable_hash(payload)
    return {
        "signature": signature,
        "payload": payload,
        "include_tables_hash": include_tables_hash,
        "sampling_hash": sampling_hash,
        "universe_hash": universe_hash,
    }


def update_factor_registry(path: Path, factor_keys: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_df = pd.DataFrame({"factor_key": factor_keys, "created_at": now})

    if path.exists():
        existing = pd.read_parquet(path)
        merged = pd.concat([existing, new_df], ignore_index=True)
        merged = merged.drop_duplicates(subset=["factor_key"], keep="last")
    else:
        merged = new_df

    merged.to_parquet(path, index=False)


def update_edges_cache(path: Path, edges: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = pd.read_parquet(path)
        if existing.empty:
            merged = edges
        elif edges.empty:
            merged = existing
        else:
            existing, edges = _align_frames(existing, edges)
            merged = pd.concat([existing, edges], ignore_index=True)
        merged = merged.drop_duplicates(subset=["factor_a", "factor_b", "signature"], keep="last")
    else:
        merged = edges
    merged.to_parquet(path, index=False)
