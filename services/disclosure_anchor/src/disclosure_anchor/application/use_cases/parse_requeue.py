"""Release one contract-class parse failure back into the parse queue.

Contract-class failures (semantic route, provider protocol/artifact contract,
provider runaway/terminal) are never retried automatically: the scheduler has
no way to know that the cause was fixed. This use case records that judgement
as an append-only decision. The failed run is evidence and is never rewritten.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from disclosure_anchor.application.contracts.parse_requeue_decision import (
    AUTOMATIC_PARSE_RETRY_BUDGET_CLASSES,
    RELEASABLE_PARSE_RETRY_BUDGET_CLASSES,
    ParseRequeueDecisionRecord,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import ParseRequeueError


@dataclass(frozen=True)
class ParseRequeueCommand:
    document_id: str
    processing_run_id: str
    fixed_by: str
    reason: str
    decided_by: str


@dataclass(frozen=True)
class ParseRequeueResult:
    document_id: str
    processing_run_id: str
    failure_error_code: str
    failure_retry_budget_class: str
    fixed_by: str
    decided_by: str
    # A dry run allocates no identity and no timestamp: nothing was written.
    decision_id: str | None = None
    decided_at: datetime | None = None
    dry_run: bool = False


class ParseRequeue:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        decision_id_factory: Callable[[], str] = lambda: ids.new_id("prq"),
    ) -> None:
        self._uow_factory = uow_factory
        self._decision_id_factory = decision_id_factory

    def execute(
        self, command: ParseRequeueCommand, *, dry_run: bool = False
    ) -> ParseRequeueResult:
        _validate_evidence(command)
        with self._uow_factory() as uow:
            run = uow.processing_runs.get(command.processing_run_id)
            if run is None or run.document_id != command.document_id:
                raise ParseRequeueError(
                    _structured_error(
                        error_code="RUN_NOT_FOUND",
                        message=(
                            "processing run not found for document: "
                            f"{command.processing_run_id}"
                        ),
                    )
                )
            _validate_contract_failure(run)
            error_code, retry_budget_class = _failure_identity(run)
            # Any succeeded run that produced a Unit generation counts, parse
            # and rebuild_units alike: re-admitting the document would re-parse
            # against a live generation.  Every such run must be provably older
            # than the failure; one with an unknown start time cannot be placed
            # and refuses the release rather than being skipped.
            for succeeded in uow.processing_runs.succeeded_provider_runs_for_document(
                command.document_id
            ):
                if not _is_earlier(succeeded, run):
                    raise ParseRequeueError(
                        _structured_error(
                            error_code="LATER_SUCCEEDED_PARSE_RUN",
                            message=(
                                "document has a succeeded provider run that is "
                                "not provably older than the failure: "
                                f"{succeeded.processing_run_id}"
                            ),
                        )
                    )
            existing = uow.processing_runs.parse_requeue_decision_for_run(
                command.processing_run_id
            )
            if existing is not None:
                raise ParseRequeueError(
                    _structured_error(
                        error_code="DECISION_ALREADY_EXISTS",
                        message=(
                            "processing run already has a requeue decision: "
                            f"{existing.decision_id}"
                        ),
                    )
                )
            if dry_run:
                return ParseRequeueResult(
                    document_id=command.document_id,
                    processing_run_id=command.processing_run_id,
                    failure_error_code=error_code,
                    failure_retry_budget_class=retry_budget_class,
                    fixed_by=command.fixed_by,
                    decided_by=command.decided_by,
                    dry_run=True,
                )
            decision = uow.processing_runs.add_parse_requeue_decision(
                ParseRequeueDecisionRecord(
                    decision_id=self._decision_id_factory(),
                    document_id=command.document_id,
                    processing_run_id=command.processing_run_id,
                    failure_error_code=error_code,
                    failure_retry_budget_class=retry_budget_class,
                    fixed_by=command.fixed_by,
                    reason=command.reason,
                    decided_by=command.decided_by,
                )
            )
            uow.commit()
            return ParseRequeueResult(
                document_id=decision.document_id,
                processing_run_id=decision.processing_run_id,
                failure_error_code=decision.failure_error_code,
                failure_retry_budget_class=decision.failure_retry_budget_class,
                fixed_by=decision.fixed_by,
                decided_by=decision.decided_by,
                decision_id=decision.decision_id,
                decided_at=decision.decided_at,
            )


def _validate_evidence(command: ParseRequeueCommand) -> None:
    for name in ("document_id", "processing_run_id", "fixed_by", "reason", "decided_by"):
        if not getattr(command, name).strip():
            raise ParseRequeueError(
                _structured_error(
                    error_code="DECISION_EVIDENCE_REQUIRED",
                    message=f"{name} must not be blank",
                    reason_code=name,
                )
            )


def _validate_contract_failure(run: e.ProcessingRun) -> None:
    if (
        run.run_kind != "parse"
        or run.provider_document_relpath is None
        or run.normalized_ir_relpath is not None
    ):
        raise ParseRequeueError(
            _structured_error(
                error_code="RUN_NOT_A_PROVIDER_PARSE_RUN",
                message=(
                    "only a provider-document parse run can be requeued: "
                    f"{run.processing_run_id}"
                ),
            )
        )
    if run.status != "failed":
        raise ParseRequeueError(
            _structured_error(
                error_code="RUN_NOT_FAILED",
                message=(
                    f"processing run status is {run.status}, not failed: "
                    f"{run.processing_run_id}"
                ),
            )
        )


def _failure_identity(run: e.ProcessingRun) -> tuple[str, str]:
    """Read the stored failure contract, refusing anything it cannot prove."""

    error = run.error
    if not isinstance(error, dict):
        raise ParseRequeueError(
            _structured_error(
                error_code="RUN_ERROR_CONTRACT_INVALID",
                message=f"failed run has no structured error: {run.processing_run_id}",
            )
        )
    error_code = error.get("error_code")
    retryable = error.get("retryable")
    retry_budget_class = error.get("retry_budget_class")
    if (
        not isinstance(error_code, str)
        or not error_code.strip()
        or not isinstance(retryable, bool)
        or not isinstance(retry_budget_class, str)
        or not retry_budget_class.strip()
    ):
        raise ParseRequeueError(
            _structured_error(
                error_code="RUN_ERROR_CONTRACT_INVALID",
                message=(
                    "failed run error must carry a boolean retryable, an "
                    "error_code and a retry_budget_class: "
                    f"{run.processing_run_id}"
                ),
            )
        )
    if retry_budget_class in AUTOMATIC_PARSE_RETRY_BUDGET_CLASSES:
        raise ParseRequeueError(
            _structured_error(
                error_code="RETRY_BUDGET_CLASS_IS_AUTOMATIC",
                message=(
                    f"retry budget class {retry_budget_class} is retried under "
                    "the parse budgets and needs no decision"
                ),
            )
        )
    if retry_budget_class not in RELEASABLE_PARSE_RETRY_BUDGET_CLASSES:
        # The queue excludes an unknown class too, but that exclusion is not
        # evidence that this failure is a fixed contract failure.
        raise ParseRequeueError(
            _structured_error(
                error_code="RETRY_BUDGET_CLASS_UNKNOWN",
                message=(
                    f"retry budget class {retry_budget_class} is outside the "
                    "releasable contract classes: "
                    f"{', '.join(sorted(RELEASABLE_PARSE_RETRY_BUDGET_CLASSES))}"
                ),
            )
        )
    return error_code, retry_budget_class


def _is_earlier(candidate: e.ProcessingRun, run: e.ProcessingRun) -> bool:
    """Order two runs of one document, refusing to guess an unknown start.

    Ordering is the repository's own ``(started_at, processing_run_id)``; the
    id breaks a tie, and run ids are time-sortable ULIDs. An unknown start time
    cannot prove the succeeded run came first, so it is reported as later and
    the requeue is refused.
    """

    if candidate.started_at is None or run.started_at is None:
        return False
    return (candidate.started_at, candidate.processing_run_id) < (
        run.started_at,
        run.processing_run_id,
    )


def _structured_error(
    *,
    error_code: str,
    message: str,
    reason_code: str | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "stage": "parse_requeue",
        "error_code": error_code,
        "retryable": False,
        "message": message,
    }
    if reason_code is not None:
        error["reason_code"] = reason_code
    return error
