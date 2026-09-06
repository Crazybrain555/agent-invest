"""Read-only authority for ordinary parse work entering staged V4."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from disclosure_anchor.application.contracts.provider_document_admission import SourcePdfObservation
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.ports.staged_provider_parser import V4StageGuard


class V4InitialIngressCapacityBlocked(RuntimeError):
    def __init__(
        self, blocked_dimensions: tuple[str, ...], *,
        ineligible_dimensions: tuple[str, ...] = (),
    ) -> None:
        super().__init__("V4 initial ingress exceeds available staged capacity")
        if not blocked_dimensions:
            raise ValueError("blocked ingress must name at least one dimension")
        self.blocked_dimensions = blocked_dimensions
        if not set(ineligible_dimensions).issubset(blocked_dimensions):
            raise ValueError("ineligible dimensions must be capacity-blocked dimensions")
        self.ineligible_dimensions = ineligible_dimensions


@dataclass(frozen=True, slots=True)
class V4OrdinaryParseCandidate:
    document_id: str
    provider: str
    provider_document_id: str
    security_id: str
    security_code: str
    raw_file_relpath: str
    raw_file_hash: str
    archived_raw_byte_count: int | None

    def __post_init__(self) -> None:
        for value, label in (
            (self.document_id, "document"),
            (self.provider, "provider"),
            (self.provider_document_id, "provider document"),
            (self.security_id, "security"),
            (self.security_code, "security code"),
            (self.raw_file_relpath, "raw relpath"),
            (self.raw_file_hash, "raw hash"),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"V4 ordinary candidate {label} is invalid")
        relpath = PurePosixPath(self.raw_file_relpath)
        if relpath.is_absolute() or not relpath.parts or ".." in relpath.parts:
            raise ValueError("V4 ordinary candidate raw relpath is unsafe")
        if (
            not self.raw_file_hash.startswith("sha256:")
            or len(self.raw_file_hash) != 71
            or any(char not in "0123456789abcdef" for char in self.raw_file_hash[7:])
        ):
            raise ValueError("V4 ordinary candidate raw hash is invalid")
        if self.archived_raw_byte_count is not None and (
            isinstance(self.archived_raw_byte_count, bool)
            or not isinstance(self.archived_raw_byte_count, int)
            or self.archived_raw_byte_count < 1
        ):
            raise ValueError("V4 ordinary candidate archived byte count is invalid")


@dataclass(frozen=True, slots=True)
class V4OrdinaryParseCandidatePage:
    candidates: tuple[V4OrdinaryParseCandidate, ...]
    has_more: bool

    def __post_init__(self) -> None:
        if (
            type(self.candidates) is not tuple
            or any(type(item) is not V4OrdinaryParseCandidate for item in self.candidates)
            or type(self.has_more) is not bool
            or (not self.candidates and self.has_more)
        ):
            raise ValueError("V4 ordinary candidate page is invalid")
        identities = tuple(item.document_id for item in self.candidates)
        if identities != tuple(sorted(identities)) or len(identities) != len(set(identities)):
            raise ValueError("V4 ordinary candidate page is not ordered and unique")


class V4OrdinaryParseCandidateSourcePort(Protocol):
    def list_candidates(
        self,
        *,
        after_document_id: str | None,
        limit: int,
    ) -> V4OrdinaryParseCandidatePage: ...


@dataclass(frozen=True, slots=True)
class V4AdmissionObservationRequest:
    """One ephemeral source read; this is not a claim or a durable H0."""

    candidate: V4OrdinaryParseCandidate
    credits: ResourceCreditVector

    def __post_init__(self) -> None:
        if (
            type(self.candidate) is not V4OrdinaryParseCandidate
            or type(self.credits) is not ResourceCreditVector
            or self.credits.snapshot_bytes < 1
            or self.credits != ResourceCreditVector(
                documents=1, snapshot_items=1, snapshot_bytes=self.credits.snapshot_bytes,
            )
        ):
            raise ValueError("V4 source observation request lacks exact bounded credit")


@dataclass(frozen=True, slots=True)
class V4RejectedSourcePdf:
    """Verified raw identity with no admissible page count; never a fake H0."""

    sha256: str
    byte_count: int
    reason_code: str

    def __post_init__(self) -> None:
        if (
            type(self.sha256) is not str or len(self.sha256) != 71
            or not self.sha256.startswith("sha256:")
            or any(char not in "0123456789abcdef" for char in self.sha256[7:])
            or type(self.byte_count) is not int or self.byte_count < 1
            or self.reason_code not in {
                "source_pdf_invalid_format", "source_pdf_password_required",
                "source_pdf_security_unsupported", "source_pdf_no_usable_pages",
            }
        ):
            raise ValueError("V4 rejected PDF lacks a closed source observation")


@dataclass(frozen=True, slots=True)
class V4SourcePdfOverLimit:
    """A stat-only size observation, not a verified digest or content failure."""

    byte_count: int

    def __post_init__(self) -> None:
        if type(self.byte_count) is not int or self.byte_count < 1:
            raise ValueError("V4 overlimit observation requires a positive size")


@dataclass(frozen=True, slots=True)
class V4AdmissionObservationResult:
    request: V4AdmissionObservationRequest
    source: SourcePdfObservation | V4RejectedSourcePdf | V4SourcePdfOverLimit

    def __post_init__(self) -> None:
        if type(self.request) is not V4AdmissionObservationRequest:
            raise ValueError("V4 source observation lacks an exact request")
        if type(self.source) is V4SourcePdfOverLimit:
            if (self.request.candidate.archived_raw_byte_count is not None
                    or self.source.byte_count <= self.request.credits.snapshot_bytes):
                raise ValueError("V4 overlimit observation is not an unknown-size excess")
            return
        if not isinstance(self.source, (SourcePdfObservation, V4RejectedSourcePdf)):
            raise ValueError("V4 source observation result type is not closed")
        if (
            type(self.source) not in {SourcePdfObservation, V4RejectedSourcePdf}
            or self.source.sha256 != self.request.candidate.raw_file_hash
            or self.source.byte_count > self.request.credits.snapshot_bytes
            or (
                self.request.candidate.archived_raw_byte_count is not None
                and self.source.byte_count != self.request.candidate.archived_raw_byte_count
            )
        ):
            raise ValueError("V4 source observation drifted from the exact request")


class V4AdmissionObservationPort(Protocol):
    def observe(
        self, request: V4AdmissionObservationRequest, *, stage_guard: V4StageGuard,
    ) -> V4AdmissionObservationResult: ...

    def accept_observation(self, result: V4AdmissionObservationResult) -> None: ...

    def abandon_observation(self, request: V4AdmissionObservationRequest) -> None: ...


class V4SourcePdfObserverPort(Protocol):
    def observe(
        self, request: V4AdmissionObservationRequest, *, stage_guard: V4StageGuard,
    ) -> V4AdmissionObservationResult: ...


__all__ = [
    "V4InitialIngressCapacityBlocked",
    "V4OrdinaryParseCandidate",
    "V4OrdinaryParseCandidatePage",
    "V4OrdinaryParseCandidateSourcePort",
    "V4AdmissionObservationRequest",
    "V4AdmissionObservationResult",
    "V4AdmissionObservationPort",
    "V4SourcePdfObserverPort",
    "V4RejectedSourcePdf",
    "V4SourcePdfOverLimit",
]
