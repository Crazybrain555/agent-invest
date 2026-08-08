"""NormalizedIR schema and runtime-contract checks."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator, FormatChecker

from disclosure_anchor.application.contracts.normalized_ir import (
    NormalizedIRVersionError,
    validate_current_normalized_ir_for_write,
    validate_normalized_ir_contract,
)
from tests.unit._current_ir import write_text_ir_bundle


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATHS = {
    version: REPO_ROOT / "contracts" / "normalized_ir" / f"{version}.json"
    for version in ("normalized_ir.v2", "normalized_ir.v3", "normalized_ir.v4")
}
V4_SOURCE = (
    REPO_ROOT
    / "src/disclosure_anchor/application/contracts/schema_sources"
    / "normalized_ir.v4.json"
)
PHASE00_ROOT = REPO_ROOT / "tests/fixtures/phase00"
LEGACY_FIXTURES = (
    "annual_report_excerpt",
    "ir_activity",
    "short_announcement",
)


def _schema(version: str) -> dict[str, object]:
    return json.loads(SCHEMA_PATHS[version].read_text(encoding="utf-8"))


def _validator(version: str) -> Draft202012Validator:
    schema = _schema(version)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _current_payload() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        relpath = Path("derived/normalized_ir/a/normalized_ir.v4.json")
        write_text_ir_bundle(root, relpath)
        return json.loads((root / relpath).read_text(encoding="utf-8"))


def _schema_errors(payload: dict[str, object]) -> list[object]:
    return list(_validator("normalized_ir.v4").iter_errors(payload))


class NormalizedIRContractTests(unittest.TestCase):
    def test_v4_schema_is_closed_and_proof_driven(self) -> None:
        schema = _schema("normalized_ir.v4")
        required = set(schema["required"])
        self.assertEqual(
            required,
            {
                "contract_version",
                "created_at",
                "document_id",
                "elements",
                "parsed_pages",
                "parser",
                "parser_artifacts",
                "parser_diagnostics",
                "source_pdf",
                "source_pdf_page_count",
                "source_pdf_sha256",
                "structure_proof",
                "title",
            },
        )
        self.assertFalse(schema["additionalProperties"])
        variants = schema["properties"]["elements"]["items"]["oneOf"]
        self.assertEqual(len(variants), 8)
        self.assertTrue(
            all(variant.get("unevaluatedProperties") is False for variant in variants)
        )
        kinds = {
            variant["allOf"][1]["properties"]["kind"]["const"] for variant in variants
        }
        self.assertNotIn("heading", kinds)
        files = schema["properties"]["parser_artifacts"]["properties"]["files"]
        self.assertEqual(
            set(files["required"]),
            {
                "content_list",
                "content_list_v2",
                "middle",
                "model",
                "pdf_structure",
                "source_evidence",
                "visual_semantics",
            },
        )
        self.assertEqual(
            schema["properties"]["structure_proof"]["$ref"],
            "#/$defs/structure_proof",
        )
        Draft202012Validator.check_schema(schema)

    def test_canonical_v4_source_matches_generated_contract(self) -> None:
        canonical = json.loads(V4_SOURCE.read_text(encoding="utf-8"))
        generated = _schema("normalized_ir.v4")
        Draft202012Validator.check_schema(canonical)
        self.assertEqual(generated, canonical)
        self.assertEqual(
            hashlib.sha256(SCHEMA_PATHS["normalized_ir.v3"].read_bytes()).hexdigest(),
            "bf9ecaa99ed4d0077d46ac686c1a51e2770db477a5dd034b7c9fd15830445511",
        )

    def test_legacy_v2_fixtures_and_frozen_schemas_remain_readable(self) -> None:
        for version in ("normalized_ir.v2", "normalized_ir.v3"):
            Draft202012Validator.check_schema(_schema(version))
        validator = _validator("normalized_ir.v2")
        for sample_key in LEGACY_FIXTURES:
            payload = json.loads(
                (PHASE00_ROOT / sample_key / "normalized_ir.v2.json").read_text(
                    encoding="utf-8"
                )
            )
            with self.subTest(sample_key=sample_key):
                self.assertEqual(list(validator.iter_errors(payload)), [])
                self.assertEqual(
                    validate_normalized_ir_contract(payload),
                    "normalized_ir.v2",
                )

    def test_frozen_schema_and_runtime_agree_on_required_root_fields(self) -> None:
        payload = _current_payload()
        self.assertEqual(_schema_errors(payload), [])
        validate_current_normalized_ir_for_write(payload)
        schema_required = set(_schema("normalized_ir.v4")["required"])
        self.assertEqual(set(payload), schema_required)
        for field in sorted(schema_required):
            with self.subTest(field=field):
                mutated = copy.deepcopy(payload)
                del mutated[field]
                self.assertTrue(_schema_errors(mutated))
                with self.assertRaises(NormalizedIRVersionError):
                    validate_current_normalized_ir_for_write(mutated)

    def test_current_fixture_passes_schema_and_runtime(self) -> None:
        payload = _current_payload()

        self.assertEqual(_schema_errors(payload), [])
        self.assertEqual(
            validate_current_normalized_ir_for_write(payload),
            "normalized_ir.v4",
        )
        self.assertEqual(
            payload["structure_proof"]["headings"][0]["evidence_kinds"],
            ["mineru_v2_title"],
        )
        self.assertEqual(payload["elements"][0]["kind"], "text")
        diagnostics = payload["parser_diagnostics"]["table_reconciliation"]
        self.assertEqual(
            diagnostics["algorithm_version"],
            "mineru-page-local-table-closure.v6",
        )
        stale = copy.deepcopy(payload)
        stale["parser_diagnostics"]["table_reconciliation"] = {
            "algorithm_version": "mineru-aggregate-table-locator.v4"
        }
        self.assertTrue(_schema_errors(stale))
        with self.assertRaisesRegex(
            NormalizedIRVersionError,
            "unsupported table reconciliation algorithm",
        ):
            validate_current_normalized_ir_for_write(stale)

    def test_provider_text_level_cannot_become_an_ir_heading(self) -> None:
        payload = _current_payload()
        element = payload["elements"][0]
        element["text_level"] = 5
        element["kind"] = "heading"
        element["heading_level"] = 5

        self.assertTrue(_schema_errors(payload))
        with self.assertRaisesRegex(
            NormalizedIRVersionError,
            "typed text carrier",
        ):
            validate_current_normalized_ir_for_write(payload)

    def test_current_root_and_parser_fields_are_closed(self) -> None:
        for label, mutate, pattern in (
            (
                "root_extra",
                lambda value: value.update({"future_guess": {}}),
                "unsupported root fields",
            ),
            (
                "parser_extra",
                lambda value: value["parser"].update({"future_guess": True}),
                "parser fields are not closed",
            ),
        ):
            with self.subTest(label=label):
                payload = _current_payload()
                mutate(payload)
                self.assertTrue(_schema_errors(payload))
                with self.assertRaisesRegex(NormalizedIRVersionError, pattern):
                    validate_current_normalized_ir_for_write(payload)

    def test_v2_parser_target_is_readable_without_changing_v1_write_shape(
        self,
    ) -> None:
        for model_name, selection in (
            (None, "server_singleton_unattested"),
            ("MinerU2.5-Pro-2605-1.2B", "explicit"),
        ):
            with self.subTest(selection=selection):
                payload = _current_payload()
                payload["parser"].update(
                    backend="vlm-http-client",
                    remote_model_name=model_name,
                    remote_selection_mode=selection,
                    target_contract_version="parser-target.v2",
                )

                self.assertEqual(_schema_errors(payload), [])
                self.assertEqual(
                    validate_current_normalized_ir_for_write(payload),
                    "normalized_ir.v4",
                )
                self.assertEqual(payload["parser"]["remote_model_name"], model_name)

        legacy = _current_payload()
        self.assertNotIn("remote_model_name", legacy["parser"])
        self.assertNotIn("remote_selection_mode", legacy["parser"])

    def test_schema_and_runtime_reject_invalid_v2_parser_target_shapes(self) -> None:
        def v2_payload() -> dict[str, object]:
            payload = _current_payload()
            payload["parser"].update(
                backend="vlm-http-client",
                remote_model_name=None,
                remote_selection_mode="server_singleton_unattested",
                target_contract_version="parser-target.v2",
            )
            return payload

        cases = {
            "missing_field": v2_payload(),
            "extra_field": v2_payload(),
            "null_explicit": v2_payload(),
            "local_singleton": v2_payload(),
            "blank_explicit": v2_payload(),
        }
        cases["missing_field"]["parser"].pop("remote_selection_mode")
        cases["extra_field"]["parser"]["future_guess"] = True
        cases["null_explicit"]["parser"]["remote_selection_mode"] = "explicit"
        cases["local_singleton"]["parser"]["backend"] = "pipeline"
        cases["blank_explicit"]["parser"].update(
            remote_model_name="   ",
            remote_selection_mode="explicit",
        )

        for label, payload in cases.items():
            with self.subTest(label=label):
                self.assertTrue(_schema_errors(payload))
                with self.assertRaises(NormalizedIRVersionError):
                    validate_current_normalized_ir_for_write(payload)

    def test_required_artifacts_must_be_present_and_hash_described(self) -> None:
        for role in (
            "content_list",
            "content_list_v2",
            "middle",
            "model",
            "pdf_structure",
            "source_evidence",
        ):
            with self.subTest(role=role):
                payload = _current_payload()
                payload["parser_artifacts"]["files"][role] = {
                    "availability": "not_emitted"
                }
                self.assertTrue(_schema_errors(payload))
                with self.assertRaisesRegex(
                    NormalizedIRVersionError,
                    "requires present parser artifacts",
                ):
                    validate_current_normalized_ir_for_write(payload)

    def test_artifact_paths_are_relative_and_confined_to_root(self) -> None:
        for relpath, schema_rejects in (
            ("/tmp/content.json", True),
            ("../content.json", True),
            ("parser/other/content.json", False),
            ("file:parser/a/content.json", True),
        ):
            with self.subTest(relpath=relpath):
                payload = _current_payload()
                payload["parser_artifacts"]["files"]["content_list"]["relpath"] = (
                    relpath
                )
                self.assertEqual(bool(_schema_errors(payload)), schema_rejects)
                with self.assertRaises(NormalizedIRVersionError):
                    validate_current_normalized_ir_for_write(payload)

    def test_structure_proof_is_bound_to_pdf_and_carriers(self) -> None:
        for label, mutate, pattern in (
            (
                "pdf_hash",
                lambda value: value["structure_proof"].update(
                    {"source_pdf_sha256": "sha256:" + "b" * 64}
                ),
                "source PDF hash differs",
            ),
            (
                "carrier_hash",
                lambda value: value["structure_proof"].update(
                    {"carrier_set_sha256": "sha256:" + "b" * 64}
                ),
                "carrier set hash differs",
            ),
            (
                "page_range",
                lambda value: value["elements"][0].update({"page_idx": 2}),
                "exceeds the source PDF",
            ),
        ):
            with self.subTest(label=label):
                payload = _current_payload()
                mutate(payload)
                with self.assertRaisesRegex(NormalizedIRVersionError, pattern):
                    validate_current_normalized_ir_for_write(payload)

    def test_structure_proof_fields_and_heading_sources_are_closed(self) -> None:
        for mutate in (
            lambda value: value["structure_proof"].update({"guess": True}),
            lambda value: value["structure_proof"]["headings"][0].update(
                {"guess": True}
            ),
            lambda value: value["structure_proof"]["headings"][0]["source_refs"][
                0
            ].update({"guess": True}),
        ):
            payload = _current_payload()
            heading_text = payload["elements"][0]["text"]
            payload["structure_proof"]["native"]["status"] = "usable"
            payload["structure_proof"]["headings"] = [
                {
                    "node_id": 1,
                    "parent_node_id": None,
                    "heading_level": 1,
                    "propagates": True,
                    "evidence_kinds": ["struct_tree"],
                    "native_node_id": 1,
                    "native_role": "H1",
                    "native_segment_id": "native_1",
                    "section_span": [0, 1],
                    "source_refs": [
                        {
                            "source_item_index": 0,
                            "field": "text",
                            "text_span": [0, len(heading_text)],
                        }
                    ],
                }
            ]
            payload["structure_proof"]["coverage"]["native_heading_candidates"] = 1
            payload["structure_proof"]["coverage"]["proven_heading_nodes"] = 1
            mutate(payload)
            self.assertTrue(_schema_errors(payload))
            with self.assertRaises(NormalizedIRVersionError):
                validate_current_normalized_ir_for_write(payload)

    def test_v3_rejects_retired_native_text_shadow(self) -> None:
        payload = json.loads(
            (PHASE00_ROOT / "short_announcement/normalized_ir.v2.json").read_text(
                encoding="utf-8"
            )
        )
        payload["contract_version"] = "normalized_ir.v3"
        payload["native_text"] = {"status": "empty"}

        with self.assertRaisesRegex(
            NormalizedIRVersionError,
            "retired native_text shadow",
        ):
            validate_normalized_ir_contract(payload)

    def test_element_identity_and_source_hash_fail_closed(self) -> None:
        for label, mutate, pattern in (
            (
                "duplicate_source_index",
                lambda value: value["elements"][1].update({"source_item_index": 0}),
                "a unique integer",
            ),
            (
                "source_hash",
                lambda value: value["elements"][0].update(
                    {"source_item_sha256": "sha256:" + "0" * 64}
                ),
                "carrier set hash differs",
            ),
            (
                "order",
                lambda value: value["elements"][0].update({"order_index": 4}),
                "strictly increasing",
            ),
        ):
            with self.subTest(label=label):
                payload = _current_payload()
                mutate(payload)
                with self.assertRaisesRegex(NormalizedIRVersionError, pattern):
                    validate_current_normalized_ir_for_write(payload)

    def test_mutating_one_copy_does_not_hide_schema_failure(self) -> None:
        payload = _current_payload()
        untouched = copy.deepcopy(payload)
        payload["elements"][0]["future"] = "x"

        self.assertTrue(_schema_errors(payload))
        self.assertEqual(_schema_errors(untouched), [])


if __name__ == "__main__":
    unittest.main()
