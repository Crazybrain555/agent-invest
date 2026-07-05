"""Exported public contracts: view columns, registry schemas, error codes.

``contracts/`` artifacts are generated from here (``make export-contracts``),
never hand-written; contract tests guard byte-for-byte equality.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from asset_intake.domain.errors import ErrorCode
from asset_intake.providers.registry import DatasetEntry, ProviderCatalog

SERVICE_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = SERVICE_ROOT / "contracts"

PUBLIC_VIEW_COLUMNS: dict[str, list[str]] = {
    "data_assets_v1": [
        "asset_id", "asset_uri", "asset_kind", "payload_kind", "contract_version",
        "content_hash", "subject_candidates", "title", "semantic_key", "material_type",
        "event_time", "published_at", "report_period", "observed_at", "source_ref",
        "provider", "adapter", "tool", "source_tier", "trace_level", "locator",
        "raw_asset_ref", "producer_action_ref", "sensitivity", "payload",
        "quality_status", "is_active", "superseded_by",
    ],
    "source_accesses_v1": [
        "access_id", "provider", "adapter", "adapter_version", "dataset_key", "tool",
        "query_params", "query_params_hash", "provider_as_of", "observed_at",
        "result_status", "result_count", "producer_action_ref",
    ],
    "change_events_v1": [
        "change_seq", "event_id", "source", "event_kind", "subject_ref", "asset_id",
        "processing_run_id", "payload", "occurred_at",
    ],
}


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def render_views_contract() -> str:
    return _dump({
        "contract": "intake_views.v1",
        "views": PUBLIC_VIEW_COLUMNS,
        "error_codes": [code.value for code in ErrorCode],
    })


def render_dataset_registry_schema() -> str:
    return _dump({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "dataset_registry.v1",
        **DatasetEntry.model_json_schema(),
    })


def render_provider_catalog_schema() -> str:
    return _dump({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "provider_catalog.v1",
        **ProviderCatalog.model_json_schema(),
    })


ARTIFACTS: dict[str, Any] = {
    "intake_views.v1.json": render_views_contract,
    "dataset_registry.v1.schema.json": render_dataset_registry_schema,
    "provider_catalog.v1.schema.json": render_provider_catalog_schema,
}


def main() -> None:
    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    for name, render in ARTIFACTS.items():
        path = CONTRACTS_DIR / name
        path.write_text(render(), encoding="utf-8")
        print(f"[ok] wrote {path}")
