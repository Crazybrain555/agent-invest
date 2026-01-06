from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


def resolve_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data or {}


def load_corr_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    repo_root = resolve_repo_root()
    default_path = repo_root / "configs" / "tools" / "corr" / "default.yaml"
    path = Path(config_path) if config_path else default_path
    if not path.is_absolute():
        path = (repo_root / path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    cfg = _load_yaml(path)
    if "corr_tool" not in cfg:
        raise ValueError("Config missing 'corr_tool' root")
    cfg["__config_path__"] = str(path)
    return cfg


def deep_update(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = deep_update(base[key], value)
        else:
            base[key] = value
    return base


def resolve_path(path_str: Optional[str], repo_root: Optional[Path] = None) -> Optional[Path]:
    if not path_str:
        return None
    repo_root = repo_root or resolve_repo_root()
    path = Path(path_str)
    if not path.is_absolute():
        path = (repo_root / path).resolve()
    return path


def load_include_tables(cfg: Dict[str, Any], repo_root: Optional[Path] = None) -> list[str]:
    sources_cfg = cfg.get("corr_tool", {}).get("sources", {})
    include_tables = sources_cfg.get("include_tables")
    if include_tables:
        return list(include_tables)

    ref_path = sources_cfg.get("include_tables_ref")
    if not ref_path:
        return []

    repo_root = repo_root or resolve_repo_root()
    path = resolve_path(ref_path, repo_root)
    if path is None or not path.exists():
        raise FileNotFoundError(f"include_tables_ref not found: {ref_path}")

    data = _load_yaml(path)
    if isinstance(data, dict) and "include_tables" in data:
        return list(data["include_tables"])
    if isinstance(data, list):
        return list(data)
    raise ValueError("Invalid include tables config format")


def snapshot_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = json.loads(json.dumps(cfg))
    snapshot.pop("__config_path__", None)
    return snapshot
