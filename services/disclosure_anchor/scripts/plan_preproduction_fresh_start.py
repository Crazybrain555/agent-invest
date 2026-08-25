"""Retired destructive pre-production reset planner.

The former planner proposed deleting immutable disclosure evidence and moving
raw/parser artifacts out of the active lineage. That conflicts with the
repository and service contracts. Keep this command as a fail-closed tombstone
so an old runbook, shell history entry, or automation cannot recreate an
executable-looking destructive manifest.
"""

from __future__ import annotations

import sys


_RETIREMENT_MESSAGE = (
    "destructive fresh-start planning is retired: preserve source_access, raw "
    "documents, documents, published processing runs/Units, and their lineage; "
    "prepare rollout through subscription reconciliation and separately reviewed "
    "cleanup of only mutable/transient or unreferenced derived state"
)


def main() -> int:
    print(f"[STOP] {_RETIREMENT_MESSAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
