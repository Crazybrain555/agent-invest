"""Composition helper for the sole semantic-routing runtime."""

from __future__ import annotations

from dataclasses import dataclass

from disclosure_anchor.adapters.semantics.codex_cli import (
    CodexCliSemanticAdjudicator,
)
from disclosure_anchor.adapters.semantics.claude_cli import (
    ClaudeCliSemanticAdjudicator,
)
from disclosure_anchor.adapters.storage.semantic_route_store import (
    SemanticRouteGroupFileCache,
    SemanticRouteReceiptStore,
)
from disclosure_anchor.application.ports.file_store import (
    ArtifactStorePort,
    FileStorePathPort,
)
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticAdjudicatorAdapterPort,
)
from disclosure_anchor.application.services.semantic_router import SemanticRouter
from disclosure_anchor.application.services.semantic_adjudication import (
    ConfiguredSemanticProvider,
    OrderedSemanticAdjudicationExecutor,
)
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
    configured_providers: list[ConfiguredSemanticProvider] = []
    for config in settings.semantic_provider_configs:
        adapter: SemanticAdjudicatorAdapterPort
        if config.kind == "codex_cli":
            adapter = CodexCliSemanticAdjudicator(
                executable=config.executable,
                runtime_tmp_root=(
                    settings.disclosure_runtime_root / "tmp" / "semantic_routes"
                ),
                model=config.canonical_model,
                reasoning_effort=config.profile,
                timeout_seconds=config.timeout_seconds,
                provider_id=config.id,
                max_concurrency=config.max_concurrency,
            )
        elif config.kind == "claude_cli":
            adapter = ClaudeCliSemanticAdjudicator(
                executable=config.executable,
                model=config.canonical_model,
                reasoning_effort=config.profile,
                timeout_seconds=config.timeout_seconds,
                provider_id=config.id,
                max_concurrency=config.max_concurrency,
            )
        else:  # pragma: no cover - Pydantic closes the vocabulary at startup.
            raise ValueError(f"unsupported semantic provider kind: {config.kind}")
        configured_providers.append(
            ConfiguredSemanticProvider(
                adapter=adapter,
                cache=SemanticRouteGroupFileCache(
                    settings.disclosure_runtime_root
                    / "cache"
                    / "semantic_routes"
                    / "v2"
                    / taxonomy.version
                    / config.id
                ),
            )
        )
    executor = OrderedSemanticAdjudicationExecutor(
        tuple(configured_providers),
        policy_version=settings.disclosure_semantic_failover_policy,
    )
    return SemanticRuntime(
        router=SemanticRouter(
            taxonomy=taxonomy,
            executor=executor,
            batch_size=settings.disclosure_semantic_batch_size,
        ),
        receipts=SemanticRouteReceiptStore(paths=paths, artifacts=artifacts),
    )


__all__ = ["SemanticRuntime", "build_semantic_runtime"]
