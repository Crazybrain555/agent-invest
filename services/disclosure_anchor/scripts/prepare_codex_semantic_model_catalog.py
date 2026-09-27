#!/usr/bin/env python3
"""Prepare a pinned, tool-free catalog without contacting a model provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from disclosure_anchor.adapters.semantics.codex_model_catalog import (
    NEUTRAL_TOOL_FIELDS,
    CodexModelCatalog,
    _entries,
    codex_model_catalog_path,
    neutralize_bundled_catalog,
)


def _write_once(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
            raise ValueError("Existing catalog artifact differs; refusing overwrite")
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--expect-version", required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args()
    executable = args.codex.resolve(strict=True)
    executable_sha = hashlib.sha256(executable.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="codex-catalog-") as raw_tmp:
        root = Path(raw_tmp)
        env = {"HOME": str(root), "CODEX_HOME": str(root / ".codex"), "PATH": os.defpath}
        version = subprocess.run(
            [str(executable), "--version"], check=True, capture_output=True,
            text=True, timeout=30, env=env, cwd=root,
        ).stdout.strip()
        if version != f"codex-cli {args.expect_version}":
            raise ValueError("Pinned Codex version differs from --expect-version")
        bundled = subprocess.run(
            [str(executable), "debug", "models", "--bundled"], check=True,
            capture_output=True, timeout=30, env=env, cwd=root,
        ).stdout
    if hashlib.sha256(executable.read_bytes()).hexdigest() != executable_sha:
        raise ValueError("Codex executable changed while preparing catalog")
    raw = neutralize_bundled_catalog(bundled, model=args.model)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    catalog = CodexModelCatalog(model=args.model, sha256=digest, raw=raw)
    path = codex_model_catalog_path(args.runtime_root, catalog.sha256)
    before = next(item for item in _entries(bundled) if item["slug"] == args.model)
    provenance = {
        "codex_version": version,
        "executable": str(executable),
        "executable_sha256": "sha256:" + executable_sha,
        "bundled_sha256": "sha256:" + hashlib.sha256(bundled).hexdigest(),
        "model": args.model,
        "model_catalog_sha256": digest,
        "tool_fields": {key: {"before": before[key], "after": value} for key, value in NEUTRAL_TOOL_FIELDS.items()},
    }
    _write_once(path, catalog.raw)
    # Different qualified binaries may carry identical model entries; retain each
    # provenance without changing or overwriting the content-addressed catalog.
    provenance_path = path.with_name(f"{path.stem}.{executable_sha}.provenance.json")
    _write_once(provenance_path, json.dumps(provenance, sort_keys=True, indent=2).encode())
    print(json.dumps({"model_catalog_sha256": digest, "path": str(path), "provenance": str(provenance_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
