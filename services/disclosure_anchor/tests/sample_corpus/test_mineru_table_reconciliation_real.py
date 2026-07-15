"""Opt-in regression checks against real MinerU 3.4 artifact pairs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
import unittest

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import (
    MinerUArtifactReader,
)
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
    MinerUToNormalizedIRMapper,
)
from disclosure_anchor.adapters.parsers.mineru.parser import (
    map_reconciled_mineru_content_list,
)
from disclosure_anchor.adapters.unit_builder.builder import (
    UnitDraft,
    build_unit_drafts_s1_s7,
)


DATA_ROOT = Path(os.environ.get("DISCLOSURE_DATA_ROOT", "/__absent__")) / "data"
POSITIVE_ROOT = Path(
    "parser_artifacts/cninfo/300012/1217576500/"
    "run_01KXJGVWYXY40E0W4DHAKRMNXF/"
    "sha256_c12d0d323ccb648fd7a3959de79f18c51ac57dbaced3f19dec659cb9784ae3ff/"
    "vlm"
)
POSITIVE_STEM = (
    "sha256_c12d0d323ccb648fd7a3959de79f18c51ac57dbaced3f19dec659cb9784ae3ff"
)
NEGATIVE_ROOT = Path(
    "parser_artifacts/cninfo/000651/1218206761/"
    "run_01KX8ABMBWDDR2BD4HXEJ7GD8V/"
    "sha256_d55f8d14d8864d88c82d6f36dca35ea974fc9f5a60451f5c6119f772ffc49d43/"
    "auto"
)
NEGATIVE_STEM = (
    "sha256_d55f8d14d8864d88c82d6f36dca35ea974fc9f5a60451f5c6119f772ffc49d43"
)
RUNNING_FURNITURE_ROOT = Path(
    "parser_artifacts/cninfo/688077/1224557820/"
    "run_01KXB5VVEM852TKXY10Q62ZYE1/"
    "sha256_62efbae9aa57765825bb75e8a20a0707c8238af94cb38c0ff605076771f4f945/"
    "vlm"
)
RUNNING_FURNITURE_STEM = (
    "sha256_62efbae9aa57765825bb75e8a20a0707c8238af94cb38c0ff605076771f4f945"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unit_semantics(unit: UnitDraft) -> tuple[object, ...]:
    return (
        unit.payload_kind,
        _without_continuation_provenance(unit.payload),
        unit.heading_path,
        unit.title,
        unit.semantic_key,
        unit.semantic_keys,
        unit.quality_status,
        unit.applicability,
    )


def _without_continuation_provenance(value: object) -> object:
    """Ignore only S5 merge spans while retaining cell geometry semantics.

    Page-local restoration intentionally lets S5 prove and record a
    ``continued_table`` page span that an aggregate carrier could not expose.
    Those two locator keys are provenance, not payload semantics.  Keep every
    other locator field -- especially ``merged_cells`` -- in the equality
    check so a lossy grid restoration still fails this real-artifact gate.
    """

    if isinstance(value, list):
        return [_without_continuation_provenance(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {
        key: _without_continuation_provenance(item) for key, item in value.items()
    }
    locator = normalized.get("artifact_locator")
    if isinstance(locator, dict):
        locator.pop("merge_reason", None)
        locator.pop("page_span", None)
    return normalized


@unittest.skipUnless(
    (DATA_ROOT / POSITIVE_ROOT).is_dir()
    and (DATA_ROOT / NEGATIVE_ROOT).is_dir()
    and (DATA_ROOT / RUNNING_FURNITURE_ROOT).is_dir(),
    "real MinerU reconciliation artifacts are absent",
)
class RealMinerUTableReconciliationTests(unittest.TestCase):
    def test_1217576500_restores_attachment_pages_and_remerges_semantics(self) -> None:
        root = DATA_ROOT / POSITIVE_ROOT
        content_path = root / f"{POSITIVE_STEM}_content_list.json"
        model_path = root / f"{POSITIVE_STEM}_model.json"
        self.assertEqual(
            _sha256(content_path),
            "c5fc441621031d18d232ba8e448ae7310bc0eb52b1e7ba59e72e6364e34a65f6",
        )
        self.assertEqual(
            _sha256(model_path),
            "e26ff561ee529014c12de2f1e5636f324907f71391c3a0a2695e0a83a92c62e5",
        )
        content = MinerUArtifactReader().read_content_list(content_path)
        parser_info = MinerUParserInfo(
            name="MinerU",
            package_version="3.4.0",
            backend="vlm-http-client",
            method="auto",
            language="ch",
            formula=False,
            table=True,
        )
        metadata = {
            "document_id": "real_1217576500",
            "source_pdf": "raw/1217576500.pdf",
            "title": "投资者关系活动记录表",
        }
        mapper = MinerUToNormalizedIRMapper()
        before_ir = mapper.map_content_list(
            content_list=content,
            parser_info=parser_info,
            document_metadata=metadata,
        )
        after_ir, result = map_reconciled_mineru_content_list(
            content_list=content,
            model_path=model_path,
            mapper=mapper,
            parser_info=parser_info,
            document_metadata=metadata,
        )
        diagnostics = after_ir["parser_diagnostics"]["table_reconciliation"]
        self.assertEqual(
            diagnostics["algorithm_version"],
            "mineru-aggregate-table-restore.v3",
        )
        self.assertEqual(
            diagnostics["model_hash"],
            "sha256:e26ff561ee529014c12de2f1e5636f324907f71391c3a0a2695e0a83a92c62e5",
        )
        self.assertEqual(diagnostics["located_groups"], 1)
        self.assertEqual(diagnostics["located_tables"], 11)
        self.assertEqual(diagnostics["restored_groups"], 1)
        self.assertEqual(diagnostics["restored_tables"], 11)
        self.assertTrue(
            all(result.content_list[index].get("table_body") for index in range(39, 50))
        )
        before, _ = build_unit_drafts_s1_s7(
            before_ir,
            filing_type="investor_relations",
            document_title=str(metadata["title"]),
        )
        after, _ = build_unit_drafts_s1_s7(
            after_ir,
            filing_type="investor_relations",
            document_title=str(metadata["title"]),
        )
        before_tables = [unit.payload for unit in before if unit.payload_kind == "table"]
        after_tables = [unit.payload for unit in after if unit.payload_kind == "table"]
        self.assertTrue(after_tables)
        self.assertEqual(after_tables, before_tables)
        attachment = next(
            unit for unit in after if unit.title and "参与机构名单" in unit.title
        )
        self.assertEqual(
            (attachment.artifact_locator or {}).get("page_span"),
            [8, 18],
        )

    def test_1218206761_restores_only_equal_width_groups(self) -> None:
        root = DATA_ROOT / NEGATIVE_ROOT
        content_path = root / f"{NEGATIVE_STEM}_content_list.json"
        model_path = root / f"{NEGATIVE_STEM}_model.json"
        self.assertEqual(
            _sha256(content_path),
            "6dc324d613e8074dac2b5d4d414284da4228c2ba73758589bac441efe05f566e",
        )
        self.assertEqual(
            _sha256(model_path),
            "b58ff2334e4321d7325df189ccd80a022330d8ebf2cabe6ee4c6ef0038a46eba",
        )
        content = MinerUArtifactReader().read_content_list(content_path)
        mapper = MinerUToNormalizedIRMapper()
        parser_info = MinerUParserInfo(
            name="MinerU",
            package_version="3.4.0",
            backend="pipeline",
            method="auto",
            language="ch",
            formula=False,
            table=True,
        )
        metadata = {
            "document_id": "real_1218206761",
            "source_pdf": "raw/1218206761.pdf",
            "title": "复杂跨页表负例",
        }
        before_ir = mapper.map_content_list(
            content_list=content,
            parser_info=parser_info,
            document_metadata=metadata,
        )
        normalized, result = map_reconciled_mineru_content_list(
            content_list=content,
            model_path=model_path,
            mapper=mapper,
            parser_info=parser_info,
            document_metadata=metadata,
        )
        # The first candidate changes from seven to five columns and is not an
        # exact aggregate concatenation. It remains aggregate + empty ghost.
        self.assertEqual(
            result.content_list[35].get("table_body"),
            content[35].get("table_body"),
        )
        self.assertEqual(
            result.content_list[38].get("table_body"),
            content[38].get("table_body"),
        )
        diagnostics = normalized["parser_diagnostics"]["table_reconciliation"]
        self.assertEqual(
            diagnostics["model_hash"],
            "sha256:b58ff2334e4321d7325df189ccd80a022330d8ebf2cabe6ee4c6ef0038a46eba",
        )
        self.assertEqual(diagnostics["unproven_groups"], 1)
        self.assertEqual(diagnostics["restored_groups"], 3)
        self.assertEqual(diagnostics["restored_tables"], 7)
        before_units, _ = build_unit_drafts_s1_s7(
            before_ir, filing_type="quarterly_report"
        )
        after_units, _ = build_unit_drafts_s1_s7(
            normalized, filing_type="quarterly_report"
        )
        self.assertEqual(
            [_unit_semantics(unit) for unit in after_units],
            [_unit_semantics(unit) for unit in before_units],
        )

    def test_1224557820_restoration_recovers_diluted_eps_orphan(self) -> None:
        root = DATA_ROOT / RUNNING_FURNITURE_ROOT
        content_path = root / f"{RUNNING_FURNITURE_STEM}_content_list.json"
        model_path = root / f"{RUNNING_FURNITURE_STEM}_model.json"
        self.assertEqual(
            _sha256(content_path),
            "b866e765e2412bd05b55243355bc165a906db7cc4b80a282cafad5f75a3b2dc4",
        )
        self.assertEqual(
            _sha256(model_path),
            "ff6693ca2701142f010dd1a54ba6a6c63504fedc151f267b4b47c6be432c4ddd",
        )
        content = MinerUArtifactReader().read_content_list(content_path)
        normalized, result = map_reconciled_mineru_content_list(
            content_list=content,
            model_path=model_path,
            mapper=MinerUToNormalizedIRMapper(),
            parser_info=MinerUParserInfo(
                name="MinerU",
                package_version="3.4.0",
                backend="vlm-http-client",
                method="auto",
                language="ch",
                formula=False,
                table=True,
            ),
            document_metadata={
                "document_id": "real_1224557820",
                "source_pdf": "raw/1224557820.pdf",
                "title": "大地熊：大地熊2025年半年度报告",
            },
        )
        self.assertEqual(result.stats.restored_groups, 41)
        self.assertEqual(result.stats.restored_tables, 88)
        diagnostics = normalized["parser_diagnostics"]["table_reconciliation"]
        self.assertEqual(
            diagnostics["algorithm_version"],
            "mineru-aggregate-table-restore.v3",
        )
        self.assertEqual(
            diagnostics["unresolved_groups"], diagnostics["unproven_groups"]
        )
        units, stats = build_unit_drafts_s1_s7(
            normalized,
            filing_type="semiannual_report",
            document_title="大地熊：大地熊2025年半年度报告",
        )
        self.assertEqual(stats.recovered_statement_orphan_rows, 1)
        table_payloads: list[dict[str, Any]] = []
        for unit in units:
            if unit.payload_kind == "table":
                table_payloads.append(unit.payload)
            elif unit.payload_kind == "mixed":
                parts = unit.payload.get("parts")
                if isinstance(parts, list):
                    table_payloads.extend(
                        part
                        for part in parts
                        if isinstance(part, dict) and part.get("kind") == "table"
                    )
        table_cells = [
            str(cell).replace("（", "(").replace("）", ")").replace("：", ":")
            for payload in table_payloads
            for row in [payload.get("headers") or [], *payload.get("rows", [])]
            for cell in row
        ]
        self.assertIn("(二)稀释每股收益(元/股)", table_cells)
        self.assertFalse(
            any(
                "稀释每股收益" in " > ".join([*unit.heading_path, unit.title or ""])
                for unit in units
            )
        )


if __name__ == "__main__":
    unittest.main()
