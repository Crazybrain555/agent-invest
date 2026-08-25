"""Filing API exported contract artifacts are generated from code."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Literal, get_args, get_origin
import unittest

import yaml

from disclosure_anchor.api.schemas.public import DocumentUnitV1
from disclosure_anchor.cli.export_contracts import (
    PUBLIC_MODELS,
    export_contracts,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_ROOT = REPO_ROOT / "contracts"
PUBLIC_MODELS_ROOT = CONTRACTS_ROOT / "public_models"
DERIVED = {
    "document_unit": {"asset_uri", "evidence_refs"},
    "source_ref": {"evidence_refs"},
    "tracked_company": {
        "effective_lookback_days",
        "effective_sync_seconds",
        "effective_process_classes",
        "sync_state",
    },
}
DOCUMENT_UNIT_V1_FIELDS = (
    "asset_id",
    "document_id",
    "processing_run_id",
    "provider_document_id",
    "payload_kind",
    "heading_path",
    "heading_path_text",
    "title",
    "order_index",
    "semantic_keys",
    "section_keys",
    "payload",
    "content_hash",
    "structure_hash",
    "quality_status",
    "applicability",
    "page_no",
    "artifact_locator",
    "created_at",
    "contract_version",
    "company_ref",
    "security_ref",
    "security_code",
    "exchange",
    "filing_type",
    "disclosure_topics",
    "report_period",
    "announcement_date",
    "producer_action_ref",
    "source_ref",
    "parent_ref",
    "asset_kind",
    "observed_at",
    "source_tier",
    "trace_level",
    "raw_file_hash",
    "query_projection_hash",
    "body_status",
    "asset_uri",
    "is_active_run",
    "evidence_refs",
)


class FilingApiContractsTests(unittest.TestCase):
    def test_document_unit_v1_shape_is_an_explicit_frozen_contract(self) -> None:
        self.assertEqual(tuple(DocumentUnitV1.model_fields), DOCUMENT_UNIT_V1_FIELDS)
        self.assertTrue(
            all(field.is_required() for field in DocumentUnitV1.model_fields.values())
        )
        for name in ("semantic_keys", "section_keys"):
            annotation = DocumentUnitV1.model_fields[name].annotation
            self.assertIs(get_origin(annotation), list)
            self.assertEqual(get_args(annotation), (str,))
        body_status = DocumentUnitV1.model_fields["body_status"].annotation
        self.assertIs(get_origin(body_status), Literal)
        self.assertEqual(get_args(body_status), ("content", "heading_only", "empty"))

        schema = json.loads(
            (PUBLIC_MODELS_ROOT / "document_unit.v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            tuple(schema["properties"]), tuple(sorted(DOCUMENT_UNIT_V1_FIELDS))
        )
        self.assertEqual(tuple(schema["required"]), DOCUMENT_UNIT_V1_FIELDS)
        self.assertNotIn("semantic_key", schema["properties"])
        self.assertNotIn("content_categories", schema["properties"])

    def test_exported_contracts_match_fresh_export_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "contracts"
            export_contracts(output_root)
            expected_paths = [
                CONTRACTS_ROOT / "filing_api.openapi.yaml",
                *(
                    PUBLIC_MODELS_ROOT / f"{name}.v1.json"
                    for name in sorted(PUBLIC_MODELS)
                ),
            ]
            for expected in expected_paths:
                actual = output_root / expected.relative_to(CONTRACTS_ROOT)
                self.assertEqual(
                    actual.read_bytes(),
                    expected.read_bytes(),
                    f"{expected.relative_to(REPO_ROOT)} drifted; rerun export_contracts",
                )

    def test_public_model_schemas_match_pydantic_model_fields(self) -> None:
        for name, model in PUBLIC_MODELS.items():
            schema = json.loads(
                (PUBLIC_MODELS_ROOT / f"{name}.v1.json").read_text(encoding="utf-8")
            )
            schema_fields = set(schema["properties"])
            model_fields = set(model.model_fields)
            self.assertEqual(schema_fields, model_fields, name)
            self.assertTrue(DERIVED.get(name, set()).issubset(schema_fields), name)
            if name in {"document_unit", "source_ref"}:
                self.assertEqual(
                    set(schema["$defs"]["EvidenceRefV1"]["properties"]),
                    {"uri", "sha256", "size_bytes", "media_type"},
                )

    def test_openapi_uses_public_error_contract_inputs(self) -> None:
        openapi = yaml.safe_load(
            (CONTRACTS_ROOT / "filing_api.openapi.yaml").read_text(encoding="utf-8")
        )
        schemas = openapi["components"]["schemas"]
        self.assertIn("ErrorEnvelope", schemas)
        self.assertNotIn("HTTPValidationError", schemas)
        self.assertNotIn("ValidationError", schemas)
        self.assertEqual(
            set(schemas["ErrorEnvelope"]["properties"]["error_code"]["enum"]),
            {
                "NOT_FOUND",
                "GONE_SUPERSEDED",
                "L1_PROCESSING_REQUIRED",
                "CONTRACT_VERSION_MISMATCH",
                "VALIDATION_ERROR",
                "EVIDENCE_INTEGRITY_ERROR",
            },
        )

        for path, operations in openapi["paths"].items():
            for operation in operations.values():
                if not isinstance(operation, dict):
                    continue
                parameters = operation.get("parameters", [])
                self.assertIn(
                    {"$ref": "#/components/parameters/XContractVersion"},
                    parameters,
                    path,
                )
                for response in operation.get("responses", {}).values():
                    encoded = json.dumps(response, sort_keys=True)
                    self.assertNotIn("HTTPValidationError", encoded)

                if "422" in operation.get("responses", {}):
                    self.assertEqual(
                        operation["responses"]["422"]["content"]["application/json"][
                            "schema"
                        ],
                        {"$ref": "#/components/schemas/ErrorEnvelope"},
                        path,
                    )

        for path in (
            "/v1/documents/{document_id}",
            "/v1/documents/{document_id}/units",
        ):
            parameters = openapi["paths"][path]["get"]["parameters"]
            self.assertTrue(
                any(param.get("name") == "reject_superseded" for param in parameters),
                path,
            )

        evidence = openapi["paths"]["/v1/units/{asset_id}/evidence/{sha256}"]["get"]
        self.assertEqual(
            evidence["responses"]["500"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/ErrorEnvelope"},
        )
        self.assertEqual(
            set(evidence["responses"]["200"]["content"]),
            {"image/gif", "image/jpeg", "image/png", "image/webp"},
        )

    def test_document_unit_v1_api_filter_names_are_frozen(self) -> None:
        openapi = yaml.safe_load(
            (CONTRACTS_ROOT / "filing_api.openapi.yaml").read_text(encoding="utf-8")
        )
        parameters = openapi["paths"]["/v1/documents/{document_id}/units"][
            "get"
        ]["parameters"]
        query_names = {
            item["name"]
            for item in parameters
            if isinstance(item, dict) and item.get("in") == "query"
        }
        self.assertEqual(
            query_names,
            {
                "processing_run_id",
                "reject_superseded",
                "payload_kind",
                "semantic_keys_any",
                "semantic_keys_all",
                "section_keys_any",
                "section_keys_all",
                "quality_status",
                "heading_prefix",
                "cursor",
                "limit",
            },
        )
        self.assertNotIn("semantic_key", query_names)


if __name__ == "__main__":
    unittest.main()
