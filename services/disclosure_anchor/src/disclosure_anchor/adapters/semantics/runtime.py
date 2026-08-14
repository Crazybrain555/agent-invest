"""Composition helper for the sole semantic-routing runtime."""

from __future__ import annotations

from dataclasses import dataclass

from disclosure_anchor.adapters.semantics.codex_cli import (
    CodexCliSemanticAdjudicator,
)
from disclosure_anchor.adapters.storage.semantic_route_store import (
    SemanticRouteFileCache,
    SemanticRouteReceiptStore,
)
from disclosure_anchor.application.ports.file_store import (
    ArtifactStorePort,
    FileStorePathPort,
)
from disclosure_anchor.application.services.semantic_router import SemanticRouter
from disclosure_anchor.application.services.semantic_taxonomy import (
    load_semantic_route_taxonomy,
)
from disclosure_anchor.settings import Settings


@dataclass(frozen=True, slots=True)
class SemanticRuntime:
    router: SemanticRouter
    receipts: SemanticRouteReceiptStore


def build_semantic_runtime(
    *,
    settings: Settings,
    paths: FileStorePathPort,
    artifacts: ArtifactStorePort,
) -> SemanticRuntime:
    """Load the taxonomy once and inject external mechanisms at the edge."""

    taxonomy = load_semantic_route_taxonomy()
    adjudicator = CodexCliSemanticAdjudicator(
        executable=settings.disclosure_semantic_codex_bin,
        runtime_tmp_root=(
            settings.disclosure_runtime_root / "tmp" / "semantic_routes"
        ),
        model=settings.disclosure_semantic_model,
        reasoning_effort=settings.disclosure_semantic_reasoning_effort,
        timeout_seconds=settings.disclosure_semantic_timeout_seconds,
    )
    cache = SemanticRouteFileCache(
        settings.disclosure_runtime_root
        / "cache"
        / "semantic_routes"
        / taxonomy.version
        / settings.disclosure_semantic_model
    )
    return SemanticRuntime(
        router=SemanticRouter(
            taxonomy=taxonomy,
            adjudicator=adjudicator,
            cache=cache,
            batch_size=settings.disclosure_semantic_batch_size,
        ),
        receipts=SemanticRouteReceiptStore(paths=paths, artifacts=artifacts),
    )


__all__ = ["SemanticRuntime", "build_semantic_runtime"]
