"""Measure the local MinerU client venv with a deterministic package digest.

This is one input to an operator/provider runtime attestation, not the complete
remote Hybrid runtime identity. It does not observe the persistent orchestrator,
inference-server image, model revision, topology, or content-affecting remote
configuration and therefore must never be copied directly into
``DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256``. It never mutates state.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from disclosure_anchor.adapters.runtime.mineru_identity import client_bundle_digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="attest_mineru_runtime", description=__doc__
    )
    parser.add_argument(
        "--mineru-bin",
        type=Path,
        required=True,
        help="path to the venv's mineru executable (DISCLOSURE_MINERU_BIN)",
    )
    args = parser.parse_args(argv)
    try:
        digest = client_bundle_digest(args.mineru_bin)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[abort] {exc}") from exc
    print(digest)
    print("local client digest only; not a remote runtime attestation", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
