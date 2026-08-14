"""Small semantic-routing doubles for tests outside the routing boundary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_FALLBACK_KEY,
    SEMANTIC_ROUTER_VERSION,
    SemanticRouteContractError,
    SemanticRouteEvidence,
    SemanticRouteReceipt,
    SemanticRouteReceiptRow,
)
from disclosure_anchor.application.ports.file_store import ArtifactWriteResult
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticAdjudicatorIdentity,
)
from disclosure_anchor.application.services.semantic_router import (
    SemanticRouteBatchResult,
)


class PassthroughSemanticRouter:
    """Keep Unit fields unchanged while producing valid fallback receipts."""

    def __init__(self) -> None:
        self.taxonomy = SimpleNamespace(version="semantic-test.v1")
        self.adjudicator = SimpleNamespace(
            identity=SemanticAdjudicatorIdentity(
                adapter="semantic-test",
                model="semantic-test",
                prompt_version="semantic-test.v1",
            )
        )

    def route(self, *, drafts, **_kwargs):  # type: ignore[no-untyped-def]
        return SemanticRouteBatchResult(
            units=tuple(drafts),
            receipts=tuple(_fallback_receipt(index) for index in range(len(drafts))),
        )

    def replay(self, *, drafts, receipts, **_kwargs):  # type: ignore[no-untyped-def]
        return SemanticRouteBatchResult(
            units=tuple(drafts),
            receipts=tuple(receipts),
        )


class MemorySemanticReceiptStore:
    """Round-trip receipt rows without exercising the file-store boundary."""

    def __init__(self) -> None:
        self.rows: tuple[SemanticRouteReceiptRow, ...] = ()

    def write(
        self,
        *,
        relpath: Path,
        rows: tuple[SemanticRouteReceiptRow, ...],
    ) -> ArtifactWriteResult:
        self.rows = rows
        return ArtifactWriteResult(
            relpath=relpath,
            artifact_hash="sha256:" + "d" * 64,
            byte_count=max(1, len(rows)),
        )

    def read(
        self,
        *,
        relpath: Path,
        expected_hash: str,
    ) -> tuple[SemanticRouteReceiptRow, ...]:
        del relpath
        if expected_hash != "sha256:" + "d" * 64:
            raise SemanticRouteContractError("semantic receipt hash differs")
        return self.rows


def _fallback_receipt(unit_index: int) -> SemanticRouteReceipt:
    return SemanticRouteReceipt(
        taxonomy_version="semantic-test.v1",
        router_version=SEMANTIC_ROUTER_VERSION,
        input_hash="sha256:" + f"{unit_index:064x}"[-64:],
        candidate_keys=(),
        semantic_keys=(SEMANTIC_FALLBACK_KEY,),
        decision_source="fallback",
        evidence=(
            SemanticRouteEvidence(
                key=SEMANTIC_FALLBACK_KEY,
                kinds=("fallback",),
                source_ids=(),
            ),
        ),
    )


__all__ = ["MemorySemanticReceiptStore", "PassthroughSemanticRouter"]
