# -*- coding: utf-8 -*-
"""Path helpers for company research runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT_ENV = "COMPANY_RESEARCH_ROOT"
DEFAULT_ROOT = Path(os.environ.get(DEFAULT_ROOT_ENV, "/home/help/mcp/work/company_research"))


def research_root(root: str | Path | None = None) -> Path:
    """Resolve the base root for company research artifacts."""
    if root is None:
        return DEFAULT_ROOT
    return Path(root)


@dataclass(frozen=True)
class CompanyPaths:
    """Convenience paths for a single company."""

    root: Path
    ticker: str

    @property
    def registry_jsonl(self) -> Path:
        return self.root / "registry.jsonl"

    @property
    def value_summary_csv(self) -> Path:
        return self.root / "value_summary.csv"

    @property
    def company_dir(self) -> Path:
        return self.root / "company" / self.ticker

    @property
    def current_dir(self) -> Path:
        return self.company_dir / "current"

    @property
    def raw_dir(self) -> Path:
        return self.company_dir / "raw"

    @property
    def runs_dir(self) -> Path:
        return self.company_dir / "runs"

    @property
    def logs_dir(self) -> Path:
        return self.company_dir / "logs"

    @property
    def company_yaml(self) -> Path:
        return self.company_dir / "company.yaml"

    @property
    def latest_json(self) -> Path:
        return self.company_dir / "latest.json"

    @property
    def artifacts_state_yaml(self) -> Path:
        return self.current_dir / "artifacts_state.yaml"

    @property
    def evidence_jsonl(self) -> Path:
        return self.current_dir / "evidence.jsonl"

    @property
    def questions_jsonl(self) -> Path:
        return self.current_dir / "questions.jsonl"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def run_meta(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "meta.yaml"

    def run_result(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "result.yaml"

    def run_needs(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "needs.yaml"

    def ensure_base_dirs(self) -> None:
        self.current_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


def company_paths(ticker: str, *, root: str | Path | None = None) -> CompanyPaths:
    return CompanyPaths(root=research_root(root), ticker=ticker)
