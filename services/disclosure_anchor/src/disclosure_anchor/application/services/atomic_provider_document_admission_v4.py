"""M4 readiness gate decorating the greenfield provider admission seam."""

from __future__ import annotations

from disclosure_anchor.application.contracts.provider_document_admission import (
    AdmittedProviderDocument,
    ProviderDocumentAdmissionError,
)
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import (
    AtomicPublicationWinnerReaderV4Port,
)
from disclosure_anchor.application.ports.atomic_publication_artifact_readiness_v4 import (
    AtomicPublicationArtifactReadinessV4Port,
)
from disclosure_anchor.application.services.provider_document_admission import (
    ProviderDocumentAdmission,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import ParserOutputContractError


class AtomicReadyProviderDocumentAdmissionV4:
    """Require one exact winner-v2 bundle before any provider source IO."""

    def __init__(
        self,
        *,
        delegate: ProviderDocumentAdmission | None,
        winner_reader: AtomicPublicationWinnerReaderV4Port | None,
        publication_readiness: AtomicPublicationArtifactReadinessV4Port | None,
    ) -> None:
        if delegate is None or winner_reader is None or publication_readiness is None:
            raise ValueError(
                "atomic publication admission requires delegate, winner, and "
                "readiness readers"
            )
        self._delegate = delegate
        self._winner_reader = winner_reader
        self._publication_readiness = publication_readiness

    def admit(
        self,
        *,
        document: e.Document,
        run: e.ProcessingRun,
        artifact_owner: e.ProcessingRun,
        security_code: str,
    ) -> AdmittedProviderDocument:
        self._verify_atomic_publication(
            document=document,
            run=run,
            artifact_owner=artifact_owner,
        )
        return self._delegate.admit(
            document=document,
            run=run,
            artifact_owner=artifact_owner,
            security_code=security_code,
        )

    def _verify_atomic_publication(
        self,
        *,
        document: e.Document,
        run: e.ProcessingRun,
        artifact_owner: e.ProcessingRun,
    ) -> None:
        if (
            run.document_id != document.document_id
            or artifact_owner.processing_run_id != run.artifact_owner_processing_run_id
        ):
            raise ProviderDocumentAdmissionError(
                "atomic_publication_readiness_invalid",
                "provider run identity is not closed before readiness verification",
            )
        try:
            winner = self._winner_reader.reload_commit_winner_by_processing_run_id(
                processing_run_id=run.processing_run_id,
            )
            if (
                winner is None
                or winner.winner_row_version != 2
                or winner.artifact_readiness is None
                or winner.document_id != document.document_id
                or winner.processing_run_id != run.processing_run_id
            ):
                raise ProviderDocumentAdmissionError(
                    "atomic_publication_readiness_missing",
                    "provider run lacks one M4-admissible transaction-P winner",
                )
            witness = self._publication_readiness.verify_ready(
                reference=winner.artifact_readiness,
                expected_winner=winner,
            )
        except ProviderDocumentAdmissionError:
            raise
        except (OSError, ParserOutputContractError, RuntimeError, ValueError) as exc:
            raise ProviderDocumentAdmissionError(
                "atomic_publication_readiness_invalid",
                str(exc),
            ) from exc
        request = witness.request
        preparation = witness.preparation
        if (
            request.identity.document_id != document.document_id
            or request.identity.processing_run_id != run.processing_run_id
            or preparation.artifact_owner_processing_run_id
            != artifact_owner.processing_run_id
            or preparation.provider_document_plan.relpath
            != run.provider_document_relpath
            or preparation.provider_document_plan.sha256 != run.artifact_hash
        ):
            raise ProviderDocumentAdmissionError(
                "atomic_publication_readiness_invalid",
                "provider run identity drifted from its readiness bundle",
            )


__all__ = ["AtomicReadyProviderDocumentAdmissionV4"]
