"""M6 evidence/control boundaries; no business database write authority."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from disclosure_anchor.application.contracts.m6_document_qualification import M6QualificationEvidence
from disclosure_anchor.application.contracts.m6_run import M6SourceHistoryFact
from disclosure_anchor.application.contracts.m6_run_events import (
    M6AttemptAdmitted, M6ProducerEvent, M6PublicConfirmation, M6RunEvent,
)


class M6RunEventSink(Protocol):
    def append(self, event: M6ProducerEvent) -> M6RunEvent:
        """Authenticate producer, stamp owner QPC and durably append before ACK.

        Producer bytes cannot choose a receiver tick or owner sequence. A
        persistence failure propagates and stops new admission; it never
        returns a successful stamp for an unrecorded observation.
        """
        ...


class M6RunEventSource(Protocol):
    def journal_lines(self, *, maximum_record_bytes: int) -> Iterator[bytes]:
        """Read the original append order using bounded readline, retaining tail."""
        ...


class M6SourceHistoryPort(Protocol):
    def first_ledger_for(self, sources: tuple[str, ...]) -> tuple[M6SourceHistoryFact, ...]:
        """Identity-scoped audit of complete global history, not campaign-filtered first."""
        ...


class M6PublicConsumerCheckPort(Protocol):
    def confirm(self, admission: M6AttemptAdmitted) -> tuple[M6PublicConfirmation, M6SourceHistoryFact]:
        """Independently read public v1 and all evidence; bind exact audit receipt.

        The adapter uses a separate read-only principal/process, validates
        winner/base/ledger/source, paginates the complete public result and
        checks immutable artifacts. A private SQL probe alone is insufficient.
        """
        ...


class M6QualificationSource(Protocol):
    def qualify(self, admission: M6AttemptAdmitted) -> M6QualificationEvidence:
        """Rebuild/check complete source evidence, including explicit unresolved review.

        Emit document_qualified only when this attempt's final qualification
        evidence is frozen. Intermediate review progress is not a credit event.
        """
        ...
