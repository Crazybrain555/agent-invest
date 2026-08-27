"""Export generated API and public-model contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from disclosure_anchor.api.schemas.public import (
    ChangeEventV1,
    DocumentCategoryV1,
    DocumentUnitV1,
    DocumentV1,
    ProcessingRunV1,
    SourceRefV1,
    TrackedCompanyV1,
)
from disclosure_anchor.application.contracts.capacity import (
    operational_schema_documents,
)
from disclosure_anchor.main import create_app
from disclosure_anchor.settings import Settings


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONTRACTS_ROOT = REPO_ROOT / "contracts"

PUBLIC_MODELS: dict[str, type[Any]] = {
    "document": DocumentV1,
    "document_unit": DocumentUnitV1,
    "document_category": DocumentCategoryV1,
    "processing_run": ProcessingRunV1,
    "source_ref": SourceRefV1,
    "change_event": ChangeEventV1,
    "tracked_company": TrackedCompanyV1,
}

def export_contracts(output_root: Path = DEFAULT_CONTRACTS_ROOT) -> list[Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    public_models_root = output_root / "public_models"
    public_models_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    openapi_path = output_root / "filing_api.openapi.yaml"
    openapi = create_app(_contract_settings(), validate_runtime=False).openapi()
    openapi_path.write_text(
        yaml.safe_dump(openapi, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )
    written.append(openapi_path)

    for name, model in PUBLIC_MODELS.items():
        path = public_models_root / f"{name}.v1.json"
        schema = model.model_json_schema()
        path.write_text(
            json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written.append(path)

    operational_root = output_root / "operational"
    operational_root.mkdir(parents=True, exist_ok=True)
    for filename, schema in sorted(operational_schema_documents().items()):
        path = operational_root / filename
        path.write_text(
            json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written.append(path)

    return written


def _contract_settings() -> Settings:
    root = REPO_ROOT / ".contract_export_runtime"
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    return Settings(
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
        mineru_model_cache=shared_root / "model_cache" / "mineru",
        hf_home=shared_root / "model_cache" / "huggingface",
        modelscope_cache=shared_root / "model_cache" / "modelscope",
    )


def main() -> int:
    written = export_contracts()
    for path in written:
        print(path.relative_to(REPO_ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
