# -*- coding: utf-8 -*-
"""Helpers for updating artifacts_state.yaml."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .atomic_io import atomic_write_yaml


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_artifacts_state(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {"artifacts": {}}
    with open(target, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if "artifacts" not in data or data["artifacts"] is None:
        data["artifacts"] = {}
    return data


def _apply_update(state: dict[str, Any], update: Mapping[str, Any]) -> None:
    artifacts: dict[str, Any] = state["artifacts"]
    artifact = update["artifact"]
    entry = dict(artifacts.get(artifact, {}))

    entry["updated_at"] = update.get("updated_at") or _now_iso()
    run_id = update.get("run_id")
    if run_id is not None:
        entry["run_id"] = run_id
    skill = update.get("skill")
    if skill is not None:
        entry["skill"] = skill

    file_hash = update.get("file_hash")
    file_path = update.get("file_path")
    if file_hash is None and file_path is not None:
        file_target = Path(file_path)
        if file_target.exists():
            from .hashing import hash_file

            file_hash = hash_file(file_target)

    if file_hash is not None:
        entry["hash"] = file_hash

    extra = update.get("extra")
    if extra:
        entry.update(dict(extra))

    artifacts[artifact] = entry


def update_artifacts_state(
    path: str | Path,
    *,
    artifact: str,
    run_id: str | None = None,
    skill: str | None = None,
    updated_at: str | None = None,
    file_path: str | Path | None = None,
    file_hash: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    state = load_artifacts_state(path)
    update = {
        "artifact": artifact,
        "run_id": run_id,
        "skill": skill,
        "updated_at": updated_at,
        "file_path": file_path,
        "file_hash": file_hash,
        "extra": extra,
    }
    _apply_update(state, update)
    state["updated_at"] = _now_iso()

    atomic_write_yaml(path, state)
    return state


def update_artifacts_state_bulk(
    path: str | Path,
    updates: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    state = load_artifacts_state(path)
    for update in updates:
        _apply_update(state, update)
    state["updated_at"] = _now_iso()
    atomic_write_yaml(path, state)
    return state
