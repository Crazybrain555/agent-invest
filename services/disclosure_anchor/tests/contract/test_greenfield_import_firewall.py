from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest


_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _SERVICE_ROOT / "src" / "disclosure_anchor"
_GREENFIELD_FILES = (
    _SOURCE_ROOT / "application" / "contracts" / "provider_document.py",
    *sorted(
        (_SOURCE_ROOT / "adapters" / "parsers" / "mineru_medium").rglob("*.py")
    ),
)
_ALLOWED_DISCLOSURE_IMPORTS = (
    "disclosure_anchor.adapters.parsers.mineru_medium",
    "disclosure_anchor.application.contracts.provider_document",
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


if __name__ == "__main__":
    unittest.main()
