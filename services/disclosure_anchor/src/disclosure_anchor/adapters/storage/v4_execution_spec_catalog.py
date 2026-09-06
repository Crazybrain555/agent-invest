"""Read-only legacy execution specs for explicit offline backfill only."""

from __future__ import annotations

from pathlib import Path

from disclosure_anchor.application.contracts.v4_prepared_execution_spec import (
    MAX_V4_PREPARED_EXECUTION_SPEC_BYTES,
    V4PreparedExecutionSpec,
    decode_v4_prepared_execution_spec,
)
from disclosure_anchor.application.ports.atomic_publication_artifact_readiness_v4 import (
    ImmutableArtifactStorePort,
)
from disclosure_anchor.application.ports.v4_execution_spec_catalog import (
    V4ExecutionSpecCatalogPathPort,
    V4ExecutionSpecCatalogReference,
)


class ImmutableV4ExecutionSpecCatalog:
    """Reopen old files by exact reference; all new specs belong in the H0 UoW."""

    def __init__(
        self,
        *,
        paths: V4ExecutionSpecCatalogPathPort,
        immutable_store: ImmutableArtifactStorePort,
    ) -> None:
        self._paths = paths
        self._store = immutable_store

    def load(
        self,
        *,
        reference: V4ExecutionSpecCatalogReference,
    ) -> V4PreparedExecutionSpec:
        if type(reference) is not V4ExecutionSpecCatalogReference:
            raise ValueError("V4 execution spec catalog reference must be exact")
        exact = self._store.read_exact(
            relpath=self._relpath(reference.spec_sha256),
            expected_sha256=reference.spec_sha256,
            expected_byte_count=reference.byte_count,
            max_byte_count=MAX_V4_PREPARED_EXECUTION_SPEC_BYTES,
        )
        spec = decode_v4_prepared_execution_spec(exact)
        if spec.sha256 != reference.spec_sha256 or spec.byte_count != reference.byte_count:
            raise ValueError("V4 execution spec reference drifted from durable bytes")
        return spec

    def _relpath(self, spec_sha256: str) -> Path:
        relpath = self._paths.v4_execution_spec_relpath(spec_sha256=spec_sha256)
        if (
            not isinstance(relpath, Path)
            or relpath.is_absolute()
            or not relpath.parts
            or any(part in {"", ".", ".."} for part in relpath.parts)
        ):
            raise ValueError("V4 execution spec catalog path is not closed relative authority")
        return relpath


__all__ = ["ImmutableV4ExecutionSpecCatalog"]
