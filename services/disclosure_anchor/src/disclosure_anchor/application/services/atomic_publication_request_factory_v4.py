"""Restart-safe acquisition of one existing atomic publication request."""

from __future__ import annotations

from typing import Protocol

from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    RemoteParseCheckpointV4,
)
from disclosure_anchor.application.ports.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactReadinessV4Port,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
    V4StageGuard,
)


class NewAtomicPublicationRequestBuilderV4(Protocol):
    """Build a first request only from durable input and pinned configuration."""

    def build(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        materialized: MaterializedProviderDocumentV4,
        stage_guard: V4StageGuard,
    ) -> AtomicPublicationRequestV4: ...


class RecoverableAtomicPublicationRequestFactoryV4:
    """Prefer an immutable prior request; build only before one exists.

    Artifact readiness persists the complete canonical request before any
    transaction-P mutation.  A fresh worker therefore reopens those exact
    bytes after a crash or lost response instead of re-running mutable
    routing/configuration logic.
    """

    def __init__(
        self,
        *,
        readiness: AtomicPublicationArtifactReadinessV4Port,
        new_request_builder: NewAtomicPublicationRequestBuilderV4,
    ) -> None:
        if not callable(getattr(readiness, "reopen_prepared_request", None)):
            raise ValueError("publication request factory lacks readiness recovery")
        if not callable(getattr(new_request_builder, "build", None)):
            raise ValueError("publication request factory lacks a first builder")
        self._readiness = readiness
        self._new_request_builder = new_request_builder

    def build_or_reopen(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        materialized: MaterializedProviderDocumentV4,
        stage_guard: V4StageGuard,
    ) -> AtomicPublicationRequestV4:
        if (
            type(checkpoint) is not RemoteParseCheckpointV4
            or checkpoint.state != "local_materialized"
            or type(materialized) is not MaterializedProviderDocumentV4
            or not callable(getattr(stage_guard, "checkpoint", None))
        ):
            raise ValueError("publication request factory input is invalid")
        stage_guard.checkpoint()
        reopened = self._readiness.reopen_prepared_request(
            checkpoint=checkpoint,
            materialized=materialized,
        )
        stage_guard.checkpoint()
        if reopened is not None:
            return reopened
        built = self._new_request_builder.build(
            checkpoint=checkpoint,
            materialized=materialized,
            stage_guard=stage_guard,
        )
        stage_guard.checkpoint()
        if type(built) is not AtomicPublicationRequestV4:
            raise ValueError("publication request builder returned a non-V4 request")
        identity = built.identity
        upstream = built.upstream_evidence
        if (
            (
                identity.attempt_id,
                identity.attempt_generation,
                identity.fence_identity,
                identity.document_id,
                identity.processing_run_id,
                identity.expected_checkpoint_sha256,
                identity.expected_local_materialization_receipt_sha256,
            )
            != (
                checkpoint.attempt_id,
                checkpoint.attempt_generation,
                checkpoint.fence_identity,
                checkpoint.document_id,
                checkpoint.processing_run_id,
                checkpoint.sha256,
                materialized.receipt.sha256,
            )
            or upstream.materialization_intent_sha256 != materialized.intent.sha256
            or upstream.provider_envelope_sha256
            != materialized.receipt.provider_envelope_sha256
        ):
            raise ValueError("publication request builder drifted from durable input")
        return built


__all__ = [
    "NewAtomicPublicationRequestBuilderV4",
    "RecoverableAtomicPublicationRequestFactoryV4",
]
