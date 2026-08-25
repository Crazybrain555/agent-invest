from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest


_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _SERVICE_ROOT / "src" / "disclosure_anchor"
_GREENFIELD_CORE_FILES = (
    _SOURCE_ROOT / "application" / "contracts" / "applicability_selector.py",
    _SOURCE_ROOT / "application" / "contracts" / "document_outline.py",
    _SOURCE_ROOT / "application" / "contracts" / "html_visible_text.py",
    _SOURCE_ROOT / "application" / "contracts" / "provider_document.py",
    _SOURCE_ROOT / "application" / "contracts" / "provider_document_admission.py",
    _SOURCE_ROOT / "application" / "contracts" / "provider_document_envelope.py",
    _SOURCE_ROOT / "application" / "contracts" / "provider_table_projection.py",
    _SOURCE_ROOT / "application" / "contracts" / "provider_unit.py",
    _SOURCE_ROOT / "application" / "contracts" / "retrieval_primary.py",
    _SOURCE_ROOT / "application" / "services" / "document_outline.py",
    _SOURCE_ROOT / "application" / "services" / "provider_document_admission.py",
    _SOURCE_ROOT / "application" / "services" / "provider_table_projection.py",
    _SOURCE_ROOT / "application" / "services" / "provider_unit_builder.py",
    _SOURCE_ROOT / "application" / "services" / "retrieval_primary.py",
    _SOURCE_ROOT / "adapters" / "parsers" / "mineru_medium" / "artifacts.py",
    _SOURCE_ROOT / "adapters" / "parsers" / "mineru_medium" / "parser.py",
    _SOURCE_ROOT / "adapters" / "storage" / "provider_document_source.py",
)
_HISTORICAL_EVIDENCE_FILES = (
    _SOURCE_ROOT / "api" / "unit_evidence.py",
    _SOURCE_ROOT / "adapters" / "storage" / "path_builder.py",
)
_HISTORICAL_EVIDENCE_CONTRACT = (
    _SOURCE_ROOT
    / "application"
    / "contracts"
    / "normalized_ir_v4_evidence.py"
)
_ALLOWED_DISCLOSURE_IMPORTS = (
    "disclosure_anchor.adapters.parsers.mineru_medium",
    "disclosure_anchor.application.contracts.applicability_selector",
    "disclosure_anchor.application.contracts.document_outline",
    "disclosure_anchor.application.contracts.html_visible_text",
    "disclosure_anchor.application.contracts.parser_target",
    "disclosure_anchor.application.contracts.provider_document",
    "disclosure_anchor.application.contracts.provider_document_admission",
    "disclosure_anchor.application.contracts.provider_document_envelope",
    "disclosure_anchor.application.contracts.provider_table_projection",
    "disclosure_anchor.application.contracts.provider_unit",
    "disclosure_anchor.application.contracts.retrieval_primary",
    "disclosure_anchor.application.services.document_outline",
    "disclosure_anchor.application.services.provider_document_admission",
    "disclosure_anchor.application.services.provider_table_projection",
    "disclosure_anchor.application.services.provider_unit_builder",
    "disclosure_anchor.application.services.retrieval_primary",
    "disclosure_anchor.application.ports.parser",
    "disclosure_anchor.application.ports.file_store",
    "disclosure_anchor.application.ports.provider_document_source",
    "disclosure_anchor.application.ports.provider_parser",
    "disclosure_anchor.adapters.parsers.pdf_page_probe",
    "disclosure_anchor.domain.errors",
    "disclosure_anchor.domain",
    "disclosure_anchor.domain.services.unit_hashing",
)
_BANNED_IMPORT_PREFIXES = (
    "disclosure_anchor.adapters.parsers.mineru",
    "disclosure_anchor.adapters.parsers.comparison",
    "disclosure_anchor.adapters.parsers.pdf_native_structure",
    "disclosure_anchor.adapters.parsers.pdf_native_text",
    "disclosure_anchor.adapters.parsers.pdf_visual_evidence",
    "disclosure_anchor.adapters.parsers.pdfium_geometry",
    "disclosure_anchor.adapters.parsers.printed_toc",
    "disclosure_anchor.application.contracts.canonical_occurrence",
    "disclosure_anchor.application.contracts.document_structure",
    "disclosure_anchor.application.contracts.normalized_ir",
    "disclosure_anchor.application.contracts.source_evidence",
    "disclosure_anchor.application.contracts.unit_source_projection",
    "disclosure_anchor.application.contracts.visual_semantics",
    "disclosure_anchor.application.ports.source_evidence",
    "disclosure_anchor.application.services.document_unit_audit",
    "disclosure_anchor.application.services.unit_builder",
    "disclosure_anchor.application.services.unit_preparation",
)
_DELETED_PATHS = (
    _SOURCE_ROOT / "adapters" / "parsers" / "mineru",
    _SOURCE_ROOT / "application" / "contracts" / "normalized_ir.py",
    _SOURCE_ROOT / "application" / "contracts" / "source_evidence.py",
    _SOURCE_ROOT / "application" / "contracts" / "unit_source_projection.py",
    _SOURCE_ROOT / "application" / "services" / "unit_builder",
    _SERVICE_ROOT / "contracts" / "normalized_ir",
    _SERVICE_ROOT / "scripts" / "audit_unit_corpus.py",
    _SERVICE_ROOT / "scripts" / "corpus_reparse_manifest.py",
    _SERVICE_ROOT / "scripts" / "corpus_reset_backup.py",
    _SERVICE_ROOT / "scripts" / "corpus_reset_quiescence.py",
    _SERVICE_ROOT / "scripts" / "reparse_corpus.py",
    _SERVICE_ROOT / "scripts" / "reset_derived_corpus.py",
    _SERVICE_ROOT / "tests" / "fixtures" / "phase00",
)


class GreenfieldImportFirewallTest(unittest.TestCase):
    def test_greenfield_seam_imports_no_legacy_or_third_party_module(self) -> None:
        violations: list[str] = []
        for path in _GREENFIELD_CORE_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        violations.append(
                            f"{path.relative_to(_SERVICE_ROOT)}: "
                            f"relative import (level={node.level})"
                        )
                        continue
                    names = [] if node.module is None else [node.module]
                else:
                    continue
                for name in names:
                    root = name.split(".", 1)[0]
                    if root in sys.stdlib_module_names or root == "__future__":
                        continue
                    if any(
                        name == allowed or name.startswith(f"{allowed}.")
                        for allowed in _ALLOWED_DISCLOSURE_IMPORTS
                    ):
                        continue
                    violations.append(f"{path.relative_to(_SERVICE_ROOT)}: {name}")
        self.assertEqual(violations, [])

    def test_historical_evidence_reader_does_not_reimport_legacy_writer(self) -> None:
        banned = "disclosure_anchor.application.contracts.normalized_ir"
        violations: list[str] = []
        for path in _HISTORICAL_EVIDENCE_FILES:
            for name in _imports(path):
                if name == banned or name.startswith(f"{banned}."):
                    violations.append(f"{path.relative_to(_SERVICE_ROOT)}: {name}")
        self.assertEqual(violations, [])

    def test_historical_evidence_contract_is_stdlib_only(self) -> None:
        violations = [
            name
            for name in _imports(_HISTORICAL_EVIDENCE_CONTRACT)
            if name.split(".", 1)[0] not in sys.stdlib_module_names
            and name != "__future__"
        ]
        self.assertEqual(violations, [])

    def test_legacy_writer_paths_and_imports_are_dead(self) -> None:
        self.assertEqual(
            [str(path.relative_to(_SERVICE_ROOT)) for path in _DELETED_PATHS if path.exists()],
            [],
        )
        violations: list[str] = []
        for root in (
            _SOURCE_ROOT,
            _SERVICE_ROOT / "scripts",
            _SERVICE_ROOT / "tests",
        ):
            for path in root.rglob("*.py"):
                for name in _imports(path):
                    if any(
                        name == banned or name.startswith(f"{banned}.")
                        for banned in _BANNED_IMPORT_PREFIXES
                    ):
                        violations.append(
                            f"{path.relative_to(_SERVICE_ROOT)}: {name}"
                        )
        self.assertEqual(violations, [])


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module is not None:
                names.append(node.module)
    return tuple(names)


if __name__ == "__main__":
    unittest.main()
