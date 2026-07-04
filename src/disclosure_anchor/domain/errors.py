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


class DocumentIdentityConflictError(DisclosureAnchorError):
    """Raised when a document identity unique constraint is hit."""


class ParserError(DisclosureAnchorError):
    """Raised when parser execution or artifact mapping fails."""


class ParseDocumentError(DisclosureAnchorError):
    """Raised when a document cannot be parsed under the current contract."""
