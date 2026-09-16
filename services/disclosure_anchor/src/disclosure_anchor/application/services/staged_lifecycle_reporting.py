"""Build lifecycle facts from durable V4 evidence and hand them to the facts port.

Every helper runs after the durable transition it describes. A fact that
cannot be built from the evidence at hand is reported as unavailable instead
of raising into the supply path; only a missing or broken port is a
programming error and surfaces immediately.
"""

from __future__ import annotations

import hashlib

from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import AcceptedSubmissionReceiptV4
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    LocalCleanupReceiptV4, ProviderAckReceiptV4,
)
from disclosure_anchor.application.ports.atomic_document_publisher_v4 import AtomicPublicationWinnerV4
from disclosure_anchor.application.ports.remote_parse_v4_repository import RemoteParseV4Authority
from disclosure_anchor.application.ports.staged_lifecycle_facts import (
    AttemptAdmittedFact, AttemptFinalFact, AttemptFinalOutcome, PublicationCommittedFact,
    RemoteAcceptedFact, RemoteDisposition, StagedLifecycleFactsPort,
)

_FINAL_OUTCOME_BY_STATE: dict[str, AttemptFinalOutcome] = {
    "acked": "published",
    "remote_failed": "failed",
    "local_failed": "failed",
    "pre_submission_failed": "failed",
    "preparation_failed": "failed",
    "superseded": "superseded",
}


def remote_task_identity_sha256(remote_task_identity: str) -> str:
    return "sha256:" + hashlib.sha256(remote_task_identity.encode("utf-8")).hexdigest()


def process_profile_sha256(authority: RemoteParseV4Authority) -> str | None:
    """The durable reservation value first; the execution spec bytes only as a fallback."""
    reservation = authority.reservation
    if reservation is not None:
        return reservation.process_profile_sha256
    spec = authority.execution_spec
    if spec is None:
        return None
    return "sha256:" + hashlib.sha256(spec.process_profile_exact_bytes).hexdigest()


def report_attempt_admitted(port: StagedLifecycleFactsPort | None, authority: RemoteParseV4Authority) -> None:
    if port is None:
        return
    profile = process_profile_sha256(authority)
    if profile is None:
        port.fact_unavailable("attempt_admitted", authority.attempt_id, "execution spec absent")
        return
    checkpoint = authority.checkpoint
    try:
        fact = AttemptAdmittedFact(
            attempt_id=authority.attempt_id, fence_identity=authority.fence_identity,
            document_id=authority.document_id, processing_run_id=authority.processing_run_id,
            source_pdf_sha256=authority.source_pdf_sha256, source_byte_count=checkpoint.source_byte_count,
            source_page_count=checkpoint.source_page_count, process_profile_sha256=profile,
        )
    except ValueError as exc:
        port.fact_unavailable("attempt_admitted", authority.attempt_id, str(exc))
        return
    port.attempt_admitted(fact)


def report_remote_accepted(
    port: StagedLifecycleFactsPort | None, authority: RemoteParseV4Authority, accepted: AcceptedSubmissionReceiptV4,
) -> None:
    if port is None:
        return
    try:
        fact = RemoteAcceptedFact(
            attempt_id=authority.attempt_id,
            remote_task_identity_sha256=remote_task_identity_sha256(accepted.remote_task_identity),
            acceptance_receipt_sha256=accepted.sha256,
        )
    except ValueError as exc:
        port.fact_unavailable("remote_accepted", authority.attempt_id, str(exc))
        return
    port.remote_accepted(fact)


def report_publication_committed(
    port: StagedLifecycleFactsPort | None, authority: RemoteParseV4Authority,
    winner: AtomicPublicationWinnerV4, ledger_seq: int | None, *, ledger_error: str | None = None,
) -> None:
    if port is None:
        return
    if ledger_seq is None:
        reason = "durable publish ledger seq unavailable"
        if ledger_error:
            reason += ": " + ledger_error[:200]
        port.fact_unavailable("publication_committed", authority.attempt_id, reason)
        return
    try:
        fact = PublicationCommittedFact(
            attempt_id=authority.attempt_id, processing_run_id=winner.processing_run_id,
            document_id=winner.document_id, source_pdf_sha256=authority.source_pdf_sha256,
            source_page_count=winner.durable_base_commit.source_page_count, ledger_seq=ledger_seq,
            winner_sha256=winner.sha256, durable_base_sha256=winner.durable_base_commit.durable_base_sha256,
        )
    except ValueError as exc:
        port.fact_unavailable("publication_committed", authority.attempt_id, str(exc))
        return
    port.publication_committed(fact)


def report_attempt_final_unsubmitted(
    port: StagedLifecycleFactsPort | None, authority: RemoteParseV4Authority, *, final_state: str,
    cleanup_receipt: LocalCleanupReceiptV4,
) -> None:
    """A final reached without any provider ACK: nothing was submitted or acknowledged."""
    if port is None:
        return
    outcome = _FINAL_OUTCOME_BY_STATE.get(final_state)
    if outcome is None:
        port.fact_unavailable("attempt_final", authority.attempt_id, f"unsupported final state {final_state}")
        return
    try:
        fact = AttemptFinalFact(
            attempt_id=authority.attempt_id, outcome=outcome, remote_disposition="not_submitted",
            remote_receipt_sha256=None, remote_task_identity_sha256=None,
            cleanup_receipt_sha256=cleanup_receipt.sha256,
        )
    except ValueError as exc:
        port.fact_unavailable("attempt_final", authority.attempt_id, str(exc))
        return
    port.attempt_final(fact)


def report_attempt_final_acknowledged(
    port: StagedLifecycleFactsPort | None, authority: RemoteParseV4Authority, *, final_state: str,
    accepted: AcceptedSubmissionReceiptV4, ack_receipt: ProviderAckReceiptV4, cleanup_receipt: LocalCleanupReceiptV4,
) -> None:
    """A final reached through the provider ACK lane; the ACK receipt is the closure evidence."""
    if port is None:
        return
    outcome = _FINAL_OUTCOME_BY_STATE.get(final_state)
    disposition: RemoteDisposition | None = (
        "consumed" if ack_receipt.ack_kind == "consumed" else "absent" if ack_receipt.ack_kind == "absent" else None
    )
    if outcome is None or disposition is None:
        port.fact_unavailable("attempt_final", authority.attempt_id, f"unsupported final {final_state}/{ack_receipt.ack_kind}")
        return
    try:
        fact = AttemptFinalFact(
            attempt_id=authority.attempt_id, outcome=outcome, remote_disposition=disposition,
            remote_receipt_sha256=ack_receipt.sha256,
            remote_task_identity_sha256=remote_task_identity_sha256(accepted.remote_task_identity),
            cleanup_receipt_sha256=cleanup_receipt.sha256,
        )
    except ValueError as exc:
        port.fact_unavailable("attempt_final", authority.attempt_id, str(exc))
        return
    port.attempt_final(fact)


def require_lifecycle_facts_port(port: object) -> StagedLifecycleFactsPort | None:
    """Validate an optional port once at composition time."""
    if port is None:
        return None
    if any(
        not callable(getattr(port, name, None))
        for name in ("attempt_admitted", "remote_accepted", "publication_committed", "attempt_final", "fact_unavailable")
    ):
        raise ValueError("lifecycle facts port is incomplete")
    return port  # type: ignore[return-value]


__all__ = [
    "process_profile_sha256", "remote_task_identity_sha256", "report_attempt_admitted",
    "report_attempt_final_acknowledged", "report_attempt_final_unsubmitted", "report_publication_committed",
    "report_remote_accepted", "require_lifecycle_facts_port",
]
