"""Read-only lifecycle facts the staged V4 runtime reports after durable transitions.

Each fact mirrors one M6 producer event payload without run, spec or producer
identity. Facts are emitted only after the corresponding durable transition
exists, so an implementation may persist, forward or ignore them but never
influences claims, credits or lane scheduling. Transport or storage failure
must not propagate back into the supply path: implementations turn it into
visible state and only raise for programming errors.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal, Protocol

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTITY_MAX = 128

AttemptFinalOutcome = Literal["published", "diagnostic_disposed", "failed", "superseded"]
RemoteDisposition = Literal["not_submitted", "consumed", "absent"]


def _identity(value: object, label: str) -> None:
    if type(value) is not str or not value.strip() or len(value) > _IDENTITY_MAX or value != value.strip():
        raise ValueError(f"lifecycle fact {label} is not an exact identity")


def _sha(value: object, label: str) -> None:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"lifecycle fact {label} must be a canonical sha256")


def _positive(value: object, label: str) -> None:
    if isinstance(value, bool) or type(value) is not int or value < 1:
        raise ValueError(f"lifecycle fact {label} must be a positive integer")


@dataclass(frozen=True, slots=True)
class AttemptAdmittedFact:
    attempt_id: str
    fence_identity: str
    document_id: str | None
    processing_run_id: str | None
    source_pdf_sha256: str
    source_byte_count: int
    source_page_count: int
    process_profile_sha256: str

    def __post_init__(self) -> None:
        _identity(self.attempt_id, "attempt id")
        _identity(self.fence_identity, "fence identity")
        if self.document_id is not None:
            _identity(self.document_id, "document id")
        if self.processing_run_id is not None:
            _identity(self.processing_run_id, "processing run id")
        _sha(self.source_pdf_sha256, "source pdf sha256")
        _positive(self.source_byte_count, "source byte count")
        _positive(self.source_page_count, "source page count")
        _sha(self.process_profile_sha256, "process profile sha256")


@dataclass(frozen=True, slots=True)
class RemoteAcceptedFact:
    attempt_id: str
    remote_task_identity_sha256: str
    acceptance_receipt_sha256: str

    def __post_init__(self) -> None:
        _identity(self.attempt_id, "attempt id")
        _sha(self.remote_task_identity_sha256, "remote task identity sha256")
        _sha(self.acceptance_receipt_sha256, "acceptance receipt sha256")


@dataclass(frozen=True, slots=True)
class PublicationCommittedFact:
    attempt_id: str
    processing_run_id: str
    document_id: str
    source_pdf_sha256: str
    source_page_count: int
    ledger_seq: int
    winner_sha256: str
    durable_base_sha256: str

    def __post_init__(self) -> None:
        _identity(self.attempt_id, "attempt id")
        _identity(self.processing_run_id, "processing run id")
        _identity(self.document_id, "document id")
        _sha(self.source_pdf_sha256, "source pdf sha256")
        _positive(self.source_page_count, "source page count")
        _positive(self.ledger_seq, "ledger seq")
        _sha(self.winner_sha256, "winner sha256")
        _sha(self.durable_base_sha256, "durable base sha256")


@dataclass(frozen=True, slots=True)
class AttemptFinalFact:
    """Terminal disposition bound to the real remote ACK or absence receipt.

    ``remote_receipt_sha256`` is the provider ACK receipt (consumed or absent
    kind); a terminal receipt only establishes parse status and never stands
    in for closure. ``not_submitted`` carries no remote receipt or identity.
    """

    attempt_id: str
    outcome: AttemptFinalOutcome
    remote_disposition: RemoteDisposition
    remote_receipt_sha256: str | None
    remote_task_identity_sha256: str | None
    cleanup_receipt_sha256: str

    def __post_init__(self) -> None:
        _identity(self.attempt_id, "attempt id")
        if self.outcome not in {"published", "diagnostic_disposed", "failed", "superseded"}:
            raise ValueError("lifecycle fact outcome is not closed")
        if self.remote_disposition not in {"not_submitted", "consumed", "absent"}:
            raise ValueError("lifecycle fact remote disposition is not closed")
        submitted = self.remote_disposition != "not_submitted"
        if submitted != (self.remote_receipt_sha256 is not None):
            raise ValueError("lifecycle fact remote disposition lacks exact receipt")
        if submitted != (self.remote_task_identity_sha256 is not None):
            raise ValueError("lifecycle fact remote disposition lacks task identity")
        if self.outcome in {"published", "diagnostic_disposed"} and not submitted:
            raise ValueError("successful parse requires remote closure")
        if self.remote_receipt_sha256 is not None:
            _sha(self.remote_receipt_sha256, "remote receipt sha256")
        if self.remote_task_identity_sha256 is not None:
            _sha(self.remote_task_identity_sha256, "remote task identity sha256")
        _sha(self.cleanup_receipt_sha256, "cleanup receipt sha256")


LifecycleFact = AttemptAdmittedFact | RemoteAcceptedFact | PublicationCommittedFact | AttemptFinalFact


class StagedLifecycleFactsPort(Protocol):
    """Receives facts after their durable transition; never raises for IO."""

    def attempt_admitted(self, fact: AttemptAdmittedFact) -> None: ...

    def remote_accepted(self, fact: RemoteAcceptedFact) -> None: ...

    def publication_committed(self, fact: PublicationCommittedFact) -> None: ...

    def attempt_final(self, fact: AttemptFinalFact) -> None: ...

    def fact_unavailable(self, kind: str, attempt_id: str, reason: str) -> None:
        """The emitter could not build a fact after a durable transition; keep that visible."""
        ...


__all__ = [
    "AttemptAdmittedFact", "AttemptFinalFact", "AttemptFinalOutcome", "LifecycleFact",
    "PublicationCommittedFact", "RemoteAcceptedFact", "RemoteDisposition", "StagedLifecycleFactsPort",
]
