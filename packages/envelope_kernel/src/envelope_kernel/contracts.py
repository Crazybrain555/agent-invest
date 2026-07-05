"""data_asset.v1 JSON schema export and the reusable envelope validation entry point.

`contracts/data_asset.v1.json` is exported from the model (`make export-contracts`), never hand-written;
a contract test guards byte-for-byte equality between the export and the committed file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from envelope_kernel.envelope import CONTRACT_VERSION, DataAsset

CONTRACT_FILENAME = f"{CONTRACT_VERSION}.json"


def data_asset_json_schema() -> dict[str, Any]:
    schema = DataAsset.model_json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": CONTRACT_VERSION,
        **schema,
    }


def render_contract() -> str:
    return json.dumps(data_asset_json_schema(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def validate_envelope(data: dict[str, Any]) -> DataAsset:
    """Validate a dict against the data_asset.v1 envelope (fields, minimal core, kind matrix)."""
    return DataAsset.model_validate(data)


def default_contract_path() -> Path:
    return Path(__file__).resolve().parents[2] / "contracts" / CONTRACT_FILENAME


def main() -> None:
    path = default_contract_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_contract(), encoding="utf-8")
    print(f"[ok] wrote {path}")
