"""Measure the local MinerU client venv with a deterministic package digest.

This is one input to an operator/provider runtime attestation, not the complete
remote Hybrid runtime identity. It does not observe the server image, model
revision, MinerU config, or content-affecting server environment and therefore
must never be copied directly into
``DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256``. It never mutates state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def client_bundle_digest(mineru_bin: Path) -> str:
    python = mineru_bin.parent / "python"
    if not python.is_file():
        raise SystemExit(f"[abort] venv python not found next to {mineru_bin}")
    listing = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import json, sys\n"
                "from importlib.metadata import distributions\n"
                "names = sorted(\n"
                "    f\"{d.metadata['Name']}=={d.version}\"\n"
                "    for d in distributions()\n"
                "    if d.metadata['Name']\n"
                ")\n"
                "json.dump({'python_version': sys.version.split()[0], "
                "'packages': names}, sys.stdout)\n"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    manifest = json.loads(listing)
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    digest = client_bundle_digest(args.mineru_bin)
    print(digest)
    print("local client digest only; not a remote runtime attestation", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
