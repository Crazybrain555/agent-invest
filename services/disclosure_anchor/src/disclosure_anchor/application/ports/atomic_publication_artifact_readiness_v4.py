"""Ports for immutable transaction-P artifact preparation and verification."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, TYPE_CHECKING

from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactPreparationV1,
    AtomicPublicationArtifactsReadyV4,
    AtomicPublicationReadinessReferenceV1,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    RemoteParseCheckpointV4,
)
from disclosure_anchor.application.ports.file_store import ArtifactWriteResult
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
    V4ClaimGuard,
    V4ClaimWitness,
)

if TYPE_CHECKING:
    from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
        AtomicPublicationWinnerV4,
    )


class ImmutableArtifactStorePort(Protocol):
    """Create exact bytes once or prove an exact prior write."""

    def create_or_verify(
        self,
        *,
        relpath: Path,
        payload: bytes,
    ) -> ArtifactWriteResult: ...

    def read_exact(
        self,
        *,
        relpath: Path,
        expected_sha256: str,
        expected_byte_count: int,
        max_byte_count: int,
    ) -> bytes: ...


class MaterializedOutputPromotionV4Port(Protocol):
    """Promote the exact parser output tree without replacement."""

    def promote_or_replay(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        materialized: MaterializedProviderDocumentV4,
        published_relpath: str,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
    ) -> None: ...

    def verify_published(
        self,
        *,
        published_relpath: str,
        expected_inventory_sha256: str,
        expected_file_count: int,
        expected_byte_count: int,
    ) -> None: ...


class AtomicPublicationArtifactReadinessV4Port(Protocol):
    """Install/replay an immutable bundle, then verify an opaque witness."""

    def prepare_or_replay(
        self,
        *,
        request: AtomicPublicationRequestV4,
        checkpoint: RemoteParseCheckpointV4,
        materialized: MaterializedProviderDocumentV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
    ) -> AtomicPublicationReadinessReferenceV1: ...

    def load_preparation(
        self,
        *,
        request: AtomicPublicationRequestV4,
    ) -> AtomicPublicationArtifactPreparationV1 | None: ...

    def reopen_prepared_request(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        materialized: MaterializedProviderDocumentV4,
    ) -> AtomicPublicationRequestV4 | None:
        """Reopen an exact request saved before a prior publication attempt."""
        ...

    def verify_ready(
        self,
        *,
        reference: AtomicPublicationReadinessReferenceV1,
        expected_request: AtomicPublicationRequestV4 | None = None,
        expected_winner: AtomicPublicationWinnerV4 | None = None,
    ) -> AtomicPublicationArtifactsReadyV4: ...


__all__ = [
    "AtomicPublicationArtifactReadinessV4Port",
    "ImmutableArtifactStorePort",
    "MaterializedOutputPromotionV4Port",
]
