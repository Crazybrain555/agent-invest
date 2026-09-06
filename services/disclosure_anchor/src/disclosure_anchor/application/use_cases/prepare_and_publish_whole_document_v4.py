"""Prepare immutable artifacts and select one whole-document DB winner."""

from __future__ import annotations

from collections.abc import Callable

from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactsReadyV4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    RemoteParseCheckpointV4,
)
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationCommitResponseLost,
    AtomicPublicationWinnerV4,
    AtomicWholeDocumentPublisherV4Port,
)
from disclosure_anchor.application.ports.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactReadinessV4Port,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializedProviderDocumentV4,
    V4ClaimGuard,
    V4ClaimWitness,
    V4StageGuard,
)
from disclosure_anchor.application.services.atomic_publication_request_factory_v4 import (
    RecoverableAtomicPublicationRequestFactoryV4,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.worker.locks import exclusive_document_producer


class PrepareAndPublishWholeDocumentV4:
    """Hold one producer lease across immutable preparation, R, and P.

    A lost commit response is resolved by a read-only winner lookup. If no
    winner exists, one exact retry is admitted with the same request, Unit
    IDs, readiness reference, and claim. A second unresolved response loss is
    returned to the caller instead of creating an unbounded retry loop.
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        publication_requests: RecoverableAtomicPublicationRequestFactoryV4,
        readiness: AtomicPublicationArtifactReadinessV4Port,
        publisher: AtomicWholeDocumentPublisherV4Port,
    ) -> None:
        self._uow_factory = uow_factory
        self._publication_requests = publication_requests
        self._readiness = readiness
        self._publisher = publisher

    def execute(
        self,
        *,
        checkpoint: RemoteParseCheckpointV4,
        materialized: MaterializedProviderDocumentV4,
        claim: V4ClaimWitness,
        claim_guard: V4ClaimGuard,
        stage_guard: V4StageGuard,
    ) -> AtomicPublicationWinnerV4:
        document_id = checkpoint.document_id
        with exclusive_document_producer(self._uow_factory, document_id):
            request = self._publication_requests.build_or_reopen(
                checkpoint=checkpoint,
                materialized=materialized,
                stage_guard=stage_guard,
            )
            stage_guard.checkpoint()
            reference = self._readiness.prepare_or_replay(
                request=request,
                checkpoint=checkpoint,
                materialized=materialized,
                claim=claim,
                claim_guard=claim_guard,
                stage_guard=stage_guard,
            )
            ready = self._readiness.verify_ready(
                reference=reference,
                expected_request=request,
            )
            winner = self._commit_or_resolve(
                request=request,
                claim=claim,
                ready=ready,
                stage_guard=stage_guard,
            )
            # The winner is not usable until its exact readiness bundle still
            # verifies after P (including response-loss recovery).
            self._readiness.verify_ready(
                reference=reference,
                expected_request=request,
                expected_winner=winner,
            )
            return winner

    def _commit_or_resolve(
        self,
        *,
        request: AtomicPublicationRequestV4,
        claim: V4ClaimWitness,
        ready: AtomicPublicationArtifactsReadyV4,
        stage_guard: V4StageGuard,
    ) -> AtomicPublicationWinnerV4:
        # Keep the retry bound literal and visible: the first P plus at most
        # one exact retry after a read-only lookup proves there is no winner.
        for attempt in range(2):
            # Preparation or the preceding response-loss lookup may outlive
            # this stage. Revocation forbids another write, not read-only
            # reconciliation of a write whose outcome is already unknown.
            stage_guard.checkpoint()
            try:
                return self._publisher.commit_whole_document(
                    request,
                    claim=claim,
                    artifacts_ready=ready,
                    stage_guard=stage_guard,
                )
            except AtomicPublicationCommitResponseLost:
                winner = self._publisher.reload_commit_winner(
                    processing_run_id=request.identity.processing_run_id,
                    attempt_id=request.identity.attempt_id,
                )
                if winner is not None:
                    return winner
                if attempt == 1:
                    raise
        raise AssertionError("bounded transaction-P recovery did not terminate")


__all__ = ["PrepareAndPublishWholeDocumentV4"]
