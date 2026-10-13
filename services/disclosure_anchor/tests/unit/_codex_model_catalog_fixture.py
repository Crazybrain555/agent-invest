"""Synthetic Codex model catalogs shaped like the pinned CLI's bundled metadata."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from disclosure_anchor.adapters.semantics.codex_model_catalog import (
    CodexModelCatalog,
    codex_model_catalog_path,
    neutralize_bundled_catalog,
)


# Tool-surface values of the codex-cli 0.156.1 bundled gpt-6-luna entry.  The
# remaining fields stand in for vendor metadata that must survive unchanged.
BUNDLED_TOOL_FIELDS: dict[str, object] = {
    "tool_mode": "code_mode_only",
    "shell_type": "unified_exec",
    "apply_patch_tool_type": "freeform",
    "experimental_supported_tools": ["send_user_message_async", "clock"],
    "supports_search_tool": True,
}


def bundled_entry(slug: str) -> dict[str, object]:
    return {
        "slug": slug,
        "display_name": slug.upper(),
        "context_window": 272000,
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [{"effort": "low", "description": "fast"}],
        "base_instructions": f"vendor instructions for {slug}",
        "use_responses_lite": True,
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        **copy.deepcopy(BUNDLED_TOOL_FIELDS),
    }


def bundled_catalog_bytes(*slugs: str) -> bytes:
    return json.dumps({"models": [bundled_entry(slug) for slug in slugs]}).encode("utf-8")


def neutral_catalog(model: str = "gpt-5.6-luna") -> CodexModelCatalog:
    raw = neutralize_bundled_catalog(
        bundled_catalog_bytes(model, "gpt-5.6-terra"),
        model=model,
    )
    return CodexModelCatalog(
        model=model,
        sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
        raw=raw,
    )


def write_prepared_catalog(runtime_root: Path, catalog: CodexModelCatalog) -> Path:
    path = codex_model_catalog_path(runtime_root, catalog.sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(catalog.raw)
    return path


def prepare_catalog_sha256(runtime_root: Path, model: str = "gpt-5.6-luna") -> str:
    """Place a prepared catalog where composition looks and return its config value."""

    catalog = neutral_catalog(model)
    write_prepared_catalog(runtime_root, catalog)
    return catalog.sha256
