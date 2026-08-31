"""Test-composition ratchet: growth requires a deliberate ledger update.

Enforces tests/AGENTS.md's audit-before-add rule mechanically (quality-ratchet
CI pattern): per-file test-method counts and private-symbol imports from
``disclosure_anchor`` are recorded in ``tests/composition_ledger.json``. Any
growth beyond the ledger fails the gate until the composition audit
(delete/merge/rewrite redundant tests first) has been done and the ledger is
consciously refreshed with ``--update``. Shrinking passes and prints a
reminder to sync the ledger.

Usage: audit_test_composition.py [--update]
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import TypedDict


class _FileState(TypedDict):
    tests: int
    private_imports: list[str]

REPO = Path(__file__).resolve().parents[1]
LEDGER_PATH = REPO / "tests" / "composition_ledger.json"
LEDGER_SCHEMA = "test-composition-ledger.v1"

_TEST_DEF_RE = re.compile(r"^\s*def (test_\w+)", re.MULTILINE)


def _private_disclosure_imports(text: str) -> list[str]:
    tree = ast.parse(text)
    private: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if node.module != "disclosure_anchor" and not node.module.startswith(
            "disclosure_anchor."
        ):
            continue
        private.update(alias.name for alias in node.names if alias.name.startswith("_"))
    return sorted(private)


def _scan() -> dict[str, _FileState]:
    state: dict[str, _FileState] = {}
    for path in sorted(REPO.glob("tests/**/test_*.py")):
        text = path.read_text(encoding="utf-8")
        tests = len(_TEST_DEF_RE.findall(text))
        state[str(path.relative_to(REPO))] = {
            "tests": tests,
            "private_imports": _private_disclosure_imports(text),
        }
    return state


def main(argv: list[str] | None = None) -> int:
    update = "--update" in (argv if argv is not None else sys.argv[1:])
    state = _scan()
    if update:
        LEDGER_PATH.write_text(
            json.dumps(
                {"schema": LEDGER_SCHEMA, "files": state},
                ensure_ascii=False,
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[ledger] updated: {sum(v['tests'] for v in state.values())} tests")
        return 0

    if not LEDGER_PATH.is_file():
        print("[fail] ledger missing — run scripts/audit_test_composition.py --update")
        return 1
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    if ledger.get("schema") != LEDGER_SCHEMA:
        print("[fail] ledger schema mismatch")
        return 1
    recorded = ledger.get("files", {})
    failures: list[str] = []
    shrunk: list[str] = []
    for rel, current in state.items():
        base = recorded.get(rel)
        if base is None:
            failures.append(f"new test file not in ledger: {rel}")
            continue
        if current["tests"] > base["tests"]:
            failures.append(
                f"{rel}: {base['tests']} -> {current['tests']} tests — audit "
                "composition per tests/AGENTS.md, then --update the ledger"
            )
        elif current["tests"] < base["tests"]:
            shrunk.append(rel)
        new_private = set(current["private_imports"]) - set(
            base.get("private_imports", [])
        )
        if new_private:
            failures.append(
                f"{rel}: new private-symbol imports {sorted(new_private)} — "
                "test observable contracts, not internals"
            )
    for rel in set(recorded) - set(state):
        shrunk.append(f"{rel} (deleted)")
    if failures:
        print("[fail] test-composition ratchet:")
        for line in failures:
            print("  -", line)
        return 1
    if shrunk:
        print(
            f"[ok] composition shrank in {len(shrunk)} file(s) — run --update to sync"
        )
    else:
        print("[ok] test composition within ledger")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
