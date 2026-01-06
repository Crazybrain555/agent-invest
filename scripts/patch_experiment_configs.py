#!/usr/bin/env python3
"""
Patch existing experiment_config.json files to ensure required fields exist.

Usage:
  python scripts/patch_experiment_configs.py \
      outputs/DFZQ_GRU_MODEL_vd_20190101_20211231_t_20080101_20181231_l2_lr3e-05_attn_pv_v5_pv_v5_pvh_20250731_163942 \
      outputs/DFZQ_GRU_MODEL_vd_20190101_20211231_t_20080101_20181231_l2_lr4e-05_attn_pv_v5_pv_v5_pvhflow_20250806_173247 \
      outputs/DFZQ_GRU_MODEL_vd_20190101_20211231_t_20080101_20181231_l2_lr4e-05_attn_pv_v5_pv_v5_pvhflow_20250806_205144 \
      outputs/DFZQ_GRU_MODEL_vd_20190101_20211231_t_20080101_20181231_l2_lr4e-05_attn_pv_v5_pv_v5_pvhflow_v2_20250807_085223

This script will:
  - Load experiment_config.json
  - Backfill missing training_config fields using dataset schema.json where possible
  - Ensure features_tables, labels_table, stats_table, restricted_table exist
  - Preserve existing values; only fill missing ones
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict


REQUIRED_KEYS = {
    # DB/fetch alignment
    "features_tables": list,
    "labels_table": str,
    "stats_table": str,
    "restricted_table": str,
    # preprocessing
    "clip_std": bool,
    "factor_based_nan_handling": bool,
    "consecutive_nan_threshold": (int, type(None)),
    "winsorise_labels": bool,
    "label_shift": int,
}


def load_json(p: Path) -> Dict[str, Any]:
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(p: Path, obj: Dict[str, Any]) -> None:
    with p.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def backfill_from_schema(exp: Dict[str, Any], exp_dir: Path) -> bool:
    changed = False
    training = exp.setdefault("training_config", {})

    ds_path = exp.get("experiment_info", {}).get("dataset_path") or training.get("dataset_path")
    if not ds_path:
        return changed

    schema_path = Path(ds_path) / "meta" / "schema.json"
    if not schema_path.exists():
        return changed

    schema = load_json(schema_path)
    tables = schema.get("tables", {})

    # features_tables
    if "features_tables" not in training:
        feats = tables.get("features")
        if isinstance(feats, list):
            training["features_tables"] = [d.get("name") for d in feats if isinstance(d, dict) and d.get("name")]
        elif isinstance(feats, dict) and feats.get("name"):
            training["features_tables"] = [feats.get("name")]
        if training.get("features_tables"):
            changed = True

    # labels_table
    if "labels_table" not in training:
        name = (tables.get("labels") or {}).get("name")
        if name:
            training["labels_table"] = name
            changed = True

    # stats_table
    if "stats_table" not in training:
        name = (tables.get("stats") or {}).get("name")
        if name:
            training["stats_table"] = name
            changed = True

    # restricted_table
    if "restricted_table" not in training:
        name = (tables.get("restricted") or {}).get("name")
        if name:
            training["restricted_table"] = name
            changed = True

    # defaults for preprocessing flags (if not present)
    if "clip_std" not in training:
        val = bool(schema.get("clip_std", True))
        training["clip_std"] = val
        changed = True

    if "factor_based_nan_handling" not in training:
        val = bool(schema.get("factor_based_nan_handling", True))
        training["factor_based_nan_handling"] = val
        changed = True

    if "consecutive_nan_threshold" not in training:
        training["consecutive_nan_threshold"] = schema.get("consecutive_nan_threshold", None)
        changed = True

    if "winsorise_labels" not in training:
        training["winsorise_labels"] = bool(schema.get("winsorise_labels", True))
        changed = True

    if "label_shift" not in training:
        # 优先尝试从 schema 无法直接得到，回退常用默认 10
        training["label_shift"] = 10
        changed = True

    return changed


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/patch_experiment_configs.py <exp_dir1> [exp_dir2 ...]")
        return 1

    any_changed = False
    for arg in sys.argv[1:]:
        exp_dir = Path(arg)
        cfg_path = exp_dir / "experiment_config.json"
        if not cfg_path.exists():
            print(f"Skip (no config): {exp_dir}")
            continue
        try:
            exp = load_json(cfg_path)
            changed = backfill_from_schema(exp, exp_dir)
            if changed:
                save_json(cfg_path, exp)
                any_changed = True
                print(f"Patched: {cfg_path}")
            else:
                print(f"No changes: {cfg_path}")
        except Exception as e:
            print(f"Error patching {cfg_path}: {e}")
            continue

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


