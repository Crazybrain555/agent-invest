from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest


_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _SERVICE_ROOT / "src" / "disclosure_anchor"
_GREENFIELD_FILES = (
    _SOURCE_ROOT / "application" / "contracts" / "document_outline.py",
    _SOURCE_ROOT / "application" / "contracts" / "provider_document.py",
    _SOURCE_ROOT / "application" / "contracts" / "provider_document_envelope.py",
    _SOURCE_ROOT / "application" / "services" / "document_outline.py",
    *sorted((_SOURCE_ROOT / "adapters" / "parsers" / "mineru_medium").rglob("*.py")),
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
    "disclosure_anchor.application.contracts.document_outline",
    "disclosure_anchor.application.contracts.parser_target",
    "disclosure_anchor.application.contracts.provider_document",
    "disclosure_anchor.application.contracts.provider_document_envelope",
    "disclosure_anchor.application.services.document_outline",
    "disclosure_anchor.domain.errors",
)


class GreenfieldImportFirewallTest(unittest.TestCase):
    def test_greenfield_seam_imports_no_legacy_or_third_party_module(self) -> None:
        violations: list[str] = []
        for path in _GREENFIELD_FILES:
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
