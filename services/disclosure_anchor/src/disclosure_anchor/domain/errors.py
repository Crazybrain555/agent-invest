"""Domain and runtime error hierarchy."""

from __future__ import annotations

from typing import Literal


ParserRetryBudgetClass = Literal["item", "infrastructure", "neutral"]


class DisclosureAnchorError(Exception):
    """Base exception for service-defined failures."""


class ConfigurationError(DisclosureAnchorError):
    """Raised when service configuration is missing or unsafe."""


class PathSafetyError(DisclosureAnchorError, ValueError):
    """Raised when a path component escapes a controlled root."""


class MissingDependencyError(DisclosureAnchorError):
    """Raised when an optional runtime dependency is not installed."""


class RawDocumentError(DisclosureAnchorError):
    """Raised when raw document storage fails."""


class InvalidRawDocumentError(RawDocumentError):
    """Raised when an input cannot become an immutable raw document."""


class RegistrationMetadataError(DisclosureAnchorError):
    """Raised when registration metadata conflicts with existing records."""


class SubjectIdentityConflictError(RegistrationMetadataError):
    """Raised when subject identifiers or securities conflict."""


class SubjectIdentityRaceError(SubjectIdentityConflictError):
    """Raised when a subject unique constraint race should be retried once."""


class DocumentIdentityConflictError(DisclosureAnchorError):
    """Raised when a document identity unique constraint is hit."""


class SourceRequestError(DisclosureAnchorError):
    """Raised when a provider source request fails under the retry policy.

    Provider adapters raise a subclass; use cases persist ``to_error`` output
    without importing adapter modules.
    """

    def __init__(self, message: str, *, error_code: str, retryable: bool) -> None:
        self.error_code = error_code
        self.retryable = retryable
        super().__init__(message)

    def to_error(
        self, *, stage: str, provider_document_id: str | None = None
    ) -> dict[str, object]:
        return {
            "stage": stage,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "provider_document_id": provider_document_id,
        }


class ParserError(DisclosureAnchorError):
    """Raised when parser execution or artifact mapping fails."""

    retry_budget_class: ParserRetryBudgetClass = "item"


class ParserTimeoutError(ParserError):
    """Raised when parser execution exceeds the configured timeout."""

    retry_budget_class: ParserRetryBudgetClass = "infrastructure"


class ParserInvocationError(ParserError):
    """Raised when parser process invocation fails."""

    retry_budget_class: ParserRetryBudgetClass = "infrastructure"


class ParserLocalInvocationError(ParserInvocationError):
    """Raised when the local parser process cannot be started."""


class ParserTaskError(ParserInvocationError):
    """Raised when a parser task fails after the local process starts."""

    retry_budget_class: ParserRetryBudgetClass = "item"


class ParserTaskDeadlineError(ParserTaskError):
    """Raised when the parser backend exceeds its item-local task deadline."""


class ParserCancelledError(ParserInvocationError):
    """Raised when the worker intentionally cancels MinerU during shutdown."""

    retry_budget_class: ParserRetryBudgetClass = "neutral"


class ParserBackendOverloadedError(ParserInvocationError):
    """Raised only for an explicit remote capacity rejection."""


class ParserVersionProbeError(ParserError):
    """Raised when parser version probing fails."""

    retry_budget_class: ParserRetryBudgetClass = "infrastructure"


class ParserOutputContractError(ParserError):
    """Raised when parser output artifacts violate the adapter contract."""

    reason_code = "parser_output_contract_error"


class StructureNativeEvidenceRequiredError(ParserOutputContractError):
    """Raised when the current structure proof lacks its native source lane.

    A missing native lane is not a provider contract defect and never
    downgrades to a legacy algorithm: the document simply cannot enter the
    canonical publication lane until its native evidence exists. The typed
    terminal lets operations tell this state apart from genuinely broken
    parser output.
    """

    reason_code = "structure_native_evidence_required"


class RemoteModelAmbiguousError(ParserError):
    """Raised when the remote backend does not serve exactly one model.

    Zero or multiple served models make the parse target's model identity
    undecidable before the run exists; this is a configuration state the
    operator must resolve, not a transient outage.
    """

    reason_code = "remote_model_ambiguous"


class RemoteModelChangedError(ParserOutputContractError):
    """Raised when the served remote model changed during a parse run."""

    reason_code = "remote_model_changed"


class ParserUnknownError(ParserError):
    """Raised when a parser adapter wraps an unexpected parser failure."""


class ParseDocumentError(DisclosureAnchorError):
    """Raised when a document cannot be parsed under the current contract."""


class BuildUnitsError(DisclosureAnchorError):
    """Raised when document_unit building fails under the current contract."""

    def __init__(self, error: dict) -> None:
        self.error = error
        super().__init__(str(error))


class PublishRunError(DisclosureAnchorError):
    """Raised when active-run publication fails under the current contract."""

    def __init__(self, error: dict) -> None:
        self.error = error
        super().__init__(str(error))
