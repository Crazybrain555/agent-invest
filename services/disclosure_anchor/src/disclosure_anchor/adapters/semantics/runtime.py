"""Composition helper for the sole semantic-routing runtime."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
import time

from disclosure_anchor.adapters.semantics.codex_cli import (
    CodexCliSemanticAdjudicator,
    semantic_shutdown_requested,
)
from disclosure_anchor.adapters.semantics.claude_cli import (
    ClaudeCliSemanticAdjudicator,
)
from disclosure_anchor.adapters.semantics.codex_model_catalog import load_codex_model_catalog
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
    SemanticNetworkWaitPort,
)
from disclosure_anchor.application.services.semantic_router import SemanticRouter
from disclosure_anchor.application.services.semantic_adjudication import (
    ConfiguredSemanticProvider,
    OrderedSemanticAdjudicationExecutor,
    SemanticNetworkTransition,
    SharedSemanticNetworkCooldown,
)
from disclosure_anchor.application.services.semantic_taxonomy import (
    load_semantic_route_taxonomy,
)
from disclosure_anchor.settings import Settings


_LOGGER = logging.getLogger(__name__)
_GROUP_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


@dataclass(frozen=True, slots=True)
class SemanticRuntime:
    router: SemanticRouter
    receipts: SemanticRouteReceiptStore
    # The shared network cooldown of a scheduled (staged) composition, else None.
    network_wait: SemanticNetworkWaitPort | None = None


def log_semantic_network_transition(transition: SemanticNetworkTransition) -> None:
    """Write one bounded ``semantic_network_wait.v1`` record to the worker's stderr log.

    Only closed tokens, counts, durations and the group hash: no provider text,
    prompt, URL or credential.
    """

    record = {
        "event": "semantic_network_wait.v1",
        "transition": transition.event,
        "observed_at_unix_ns": time.time_ns(),
        "consecutive_outages": transition.consecutive_outages,
        "outage_seconds": round(transition.outage_seconds, 3),
        "retry_after_seconds": (
            None if transition.retry_after_seconds is None
            else round(transition.retry_after_seconds, 3)
        ),
        "group_hash": (
            transition.group_hash
            if transition.group_hash is not None and _GROUP_HASH_RE.fullmatch(transition.group_hash)
            else None
        ),
        "attempts": [
            [
                provider_id if _TOKEN_RE.fullmatch(provider_id) else "unrecognized",
                reason_code if _TOKEN_RE.fullmatch(reason_code) else "unrecognized",
            ]
            for provider_id, reason_code in transition.reasons[:8]
        ],
    }
    _LOGGER.warning("semantic_network_wait %s", json.dumps(record, sort_keys=True))


def build_semantic_runtime(
    *,
    settings: Settings,
    paths: FileStorePathPort,
    artifacts: ArtifactStorePort,
    network_wait: bool = False,
) -> SemanticRuntime:
    """Load the taxonomy once and inject external mechanisms at the edge.

    ``network_wait`` is only for a scheduled caller (the staged V4 worker) that
    turns a semantic network outage into a later run of the same group. Every
    other caller keeps the degraded_unavailable result for such an outage.
    """

    taxonomy = load_semantic_route_taxonomy()
    configured_providers: list[ConfiguredSemanticProvider] = []
    for config in settings.semantic_provider_configs:
        adapter: SemanticAdjudicatorAdapterPort
        if config.kind == "codex_cli":
            assert config.model_catalog_sha256 is not None  # validated provider configuration
            catalog = load_codex_model_catalog(
                settings.disclosure_runtime_root,
                sha256=config.model_catalog_sha256,
                model=config.canonical_model,
            )
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
                model_catalog=catalog,
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
    cooldown = (
        SharedSemanticNetworkCooldown(
            on_transition=log_semantic_network_transition,
            # An operator stop also ends network waits at once, as it ends
            # running provider calls, instead of keeping the drain open.
            shutdown_requested=semantic_shutdown_requested,
        )
        if network_wait
        else None
    )
    executor = OrderedSemanticAdjudicationExecutor(
        tuple(configured_providers),
        policy_version=settings.disclosure_semantic_failover_policy,
        network_cooldown=cooldown,
    )
    return SemanticRuntime(
        router=SemanticRouter(
            taxonomy=taxonomy,
            executor=executor,
            batch_size=settings.disclosure_semantic_batch_size,
        ),
        receipts=SemanticRouteReceiptStore(paths=paths, artifacts=artifacts),
        network_wait=cooldown,
    )


__all__ = ["SemanticRuntime", "build_semantic_runtime", "log_semantic_network_transition"]
