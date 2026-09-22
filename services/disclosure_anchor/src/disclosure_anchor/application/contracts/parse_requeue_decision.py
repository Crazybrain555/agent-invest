"""Closed private contract for the append-only parse requeue decision."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Optional, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from disclosure_anchor.domain.errors import ParserRetryBudgetClass

# The scheduler owns these classes; it retries them under the parse budgets.
AUTOMATIC_PARSE_RETRY_BUDGET_CLASSES = frozenset(get_args(ParserRetryBudgetClass))
# The closed set of contract classes the V4 coordinator actually persists
# (staged_coordinator_backend_v4: provider_terminal, provider_runaway,
# provider_artifact_contract, provider_protocol, semantic_route_contract).
# The queue excludes anything it cannot read as a known retry class; that
# exclusion is not evidence that an operator may re-admit it, so an unknown or
# malformed class is refused here rather than released.
RELEASABLE_PARSE_RETRY_BUDGET_CLASSES = frozenset(
    {
        "provider_artifact_contract",
        "provider_protocol",
        "provider_runaway",
        "provider_terminal",
        "semantic_route_contract",
    }
)

_DECISION_ID = re.compile(r"prq_[0-9A-HJKMNP-TV-Z]{26}\Z")
_EVIDENCE_FIELDS = (
    "failure_error_code",
    "failure_retry_budget_class",
    "fixed_by",
    "reason",
    "decided_by",
)


class ParseRequeueDecisionRecord(BaseModel):
    """One operator judgement that a contract-class parse failure is fixed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str = Field(min_length=1, max_length=64)
    document_id: str = Field(min_length=1, max_length=64)
    processing_run_id: str = Field(min_length=1, max_length=64)
    failure_error_code: str = Field(min_length=1, max_length=128)
    failure_retry_budget_class: str = Field(min_length=1, max_length=64)
    fixed_by: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1)
    decided_by: str = Field(min_length=1, max_length=128)
    # Assigned by the database on insert; absent until the row exists.
    decided_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _valid(self) -> "ParseRequeueDecisionRecord":
        if _DECISION_ID.fullmatch(self.decision_id) is None:
            raise ValueError("decision_id is not canonical")
        for name in _EVIDENCE_FIELDS:
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be blank")
        if (
            self.failure_retry_budget_class
            not in RELEASABLE_PARSE_RETRY_BUDGET_CLASSES
        ):
            raise ValueError(
                "failure_retry_budget_class "
                f"{self.failure_retry_budget_class} is not a releasable "
                "contract class"
            )
        return self
