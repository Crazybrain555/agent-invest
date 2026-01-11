# -*- coding: utf-8 -*-
"""Shared runtime helpers for company research skills."""

from .artifacts_state import (
    load_artifacts_state,
    update_artifacts_state,
    update_artifacts_state_bulk,
)
from .atomic_io import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_jsonl,
    atomic_write_parquet,
    atomic_write_text,
    atomic_write_yaml,
    ensure_parent_dir,
)
from .evidence import append_evidence, append_question, append_records, append_jsonl, ensure_jsonl
from .hashing import (
    fingerprint_data,
    fingerprint_inputs,
    hash_bytes,
    hash_file,
    hash_text,
    stable_json_dumps,
)
from .paths import CompanyPaths, company_paths, research_root
from .runlog import (
    RunContext,
    build_needs,
    build_run_meta,
    build_run_result,
    default_run_id,
    init_run_dir,
    write_meta,
    write_needs,
    write_result,
    write_run_logs,
)

__all__ = [
    "CompanyPaths",
    "RunContext",
    "append_evidence",
    "append_jsonl",
    "append_question",
    "append_records",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_jsonl",
    "atomic_write_parquet",
    "atomic_write_text",
    "atomic_write_yaml",
    "build_needs",
    "build_run_meta",
    "build_run_result",
    "company_paths",
    "default_run_id",
    "ensure_jsonl",
    "ensure_parent_dir",
    "fingerprint_data",
    "fingerprint_inputs",
    "hash_bytes",
    "hash_file",
    "hash_text",
    "init_run_dir",
    "load_artifacts_state",
    "research_root",
    "stable_json_dumps",
    "update_artifacts_state",
    "update_artifacts_state_bulk",
    "write_meta",
    "write_needs",
    "write_result",
    "write_run_logs",
]
