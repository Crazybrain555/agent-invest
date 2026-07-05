"""Domain and runtime error hierarchy."""

from __future__ import annotations


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


class ParserError(DisclosureAnchorError):
    """Raised when parser execution or artifact mapping fails."""


class ParserTimeoutError(ParserError):
    """Raised when parser execution exceeds the configured timeout."""


class ParserInvocationError(ParserError):
    """Raised when parser process invocation fails."""


class ParserVersionProbeError(ParserError):
    """Raised when parser version probing fails."""


class ParserOutputContractError(ParserError):
    """Raised when parser output artifacts violate the adapter contract."""


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
