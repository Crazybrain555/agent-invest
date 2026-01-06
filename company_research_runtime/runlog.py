# -*- coding: utf-8 -*-
"""Run log helpers for writing meta/result/needs files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from zoneinfo import ZoneInfo

from .atomic_io import atomic_write_yaml


def default_run_id(timezone: str = "America/New_York", now: datetime | None = None) -> str:
    """Return a timestamp-based run id (YYYYMMDD_HHMMSS)."""
    current = now or datetime.now(ZoneInfo(timezone))
    return current.strftime("%Y%m%d_%H%M%S")


def _normalize_as_of(as_of: date | str | None, timezone: str) -> str:
    if isinstance(as_of, str):
        return as_of
    if isinstance(as_of, date):
        return as_of.isoformat()
    return datetime.now(ZoneInfo(timezone)).date().isoformat()


@dataclass(frozen=True)
class RunContext:
    """Basic run metadata shared between meta/result files."""

    skill: str
    ticker: str
    run_id: str
    as_of: str
    timezone: str = "America/New_York"


def build_run_meta(
    *,
    skill: str,
    ticker: str,
    run_id: str,
    as_of: date | str | None = None,
    timezone: str = "America/New_York",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "skill": skill,
        "ticker": ticker,
        "run_id": run_id,
        "as_of": _normalize_as_of(as_of, timezone),
        "timezone": timezone,
    }
    payload.update(extra)
    return payload


def build_run_result(
    *,
    skill: str,
    ticker: str,
    run_id: str,
    status: str,
    as_of: date | str | None = None,
    timezone: str = "America/New_York",
    requires: Mapping[str, Any] | None = None,
    missing: list[str] | None = None,
    warnings: list[str] | None = None,
    outputs: list[str] | None = None,
    next_suggested_skills: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "skill": skill,
        "ticker": ticker,
        "run_id": run_id,
        "as_of": _normalize_as_of(as_of, timezone),
        "timezone": timezone,
        "status": status,
        "requires": dict(requires) if requires is not None else {"hard": [], "soft": []},
        "missing": missing or [],
        "warnings": warnings or [],
        "outputs": outputs or [],
    }
    if next_suggested_skills is not None:
        payload["next_suggested_skills"] = list(next_suggested_skills)
    payload.update(extra)
    return payload


def build_needs(
    *,
    blocked_by: list[dict[str, Any]],
    suggested_plan: list[str] | None = None,
    priority: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"blocked_by": blocked_by}
    if suggested_plan is not None:
        payload["suggested_plan"] = list(suggested_plan)
    if priority is not None:
        payload["priority"] = priority
    payload.update(extra)
    return payload


def init_run_dir(run_dir: str | Path) -> Path:
    target = Path(run_dir)
    target.mkdir(parents=True, exist_ok=True)
    return target


def write_meta(run_dir: str | Path, payload: Mapping[str, Any]) -> Path:
    run_path = init_run_dir(run_dir)
    return atomic_write_yaml(run_path / "meta.yaml", dict(payload))


def write_result(run_dir: str | Path, payload: Mapping[str, Any]) -> Path:
    run_path = init_run_dir(run_dir)
    return atomic_write_yaml(run_path / "result.yaml", dict(payload))


def write_needs(run_dir: str | Path, payload: Mapping[str, Any]) -> Path:
    run_path = init_run_dir(run_dir)
    return atomic_write_yaml(run_path / "needs.yaml", dict(payload))


def write_run_logs(
    run_dir: str | Path,
    *,
    meta: Mapping[str, Any] | None = None,
    result: Mapping[str, Any] | None = None,
    needs: Mapping[str, Any] | None = None,
) -> dict[str, Path]:
    run_path = init_run_dir(run_dir)
    outputs: dict[str, Path] = {}
    if meta is not None:
        outputs["meta"] = atomic_write_yaml(run_path / "meta.yaml", dict(meta))
    if result is not None:
        outputs["result"] = atomic_write_yaml(run_path / "result.yaml", dict(result))
    if needs is not None:
        outputs["needs"] = atomic_write_yaml(run_path / "needs.yaml", dict(needs))
    return outputs
