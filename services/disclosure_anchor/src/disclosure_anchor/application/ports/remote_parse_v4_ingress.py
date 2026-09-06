"""Atomic PostgreSQL handoff from the ordinary parse backlog into V4."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import PurePosixPath
from typing import Protocol

from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)
from disclosure_anchor.application.contracts.provider_document_admission import (
    SourcePdfObservation,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RemoteParseV4Authority,
    V4PreparedProposal,
    bind_v4_prepared_proposal,
)
from disclosure_anchor.domain.entities import OutboxEvent, ProcessingRun
from disclosure_anchor.application.ports.remote_parse_v4_source_rejection import V4SourceRejectionCommit


class V4InitialIngressError(RuntimeError):
    """Base class for one initial V4 ingress failure."""


class V4InitialIngressNotEligible(V4InitialIngressError):
    """The document no longer belongs to the ordinary parse backlog."""


class V4InitialIngressNotCommitted(V4InitialIngressError):
    """No part of the exact initial ingress packet was committed."""


class V4InitialIngressDrift(V4InitialIngressError):
    """A partial or different durable ingress packet exists."""


@dataclass(frozen=True, slots=True)
class V4InitialIngressCommit:
    proposal: V4PreparedProposal
    source_observation: SourcePdfObservation
    expected_provider: str
    expected_provider_document_id: str
    expected_security_id: str
    expected_raw_file_relpath: str
    expected_raw_file_hash: str
    parser_target: ParserTargetIdentity
    parser_artifact_relpath: str
    provider_document_relpath: str
    started_at: datetime
    created_outbox_event_id: str
    max_retries: int
    scope_classes: tuple[str, ...] | None

    def __post_init__(self) -> None:
        if type(self.proposal) is not V4PreparedProposal:
            raise ValueError("v4 ingress proposal must be exact")
        if type(self.source_observation) is not SourcePdfObservation:
            raise ValueError("v4 ingress source observation must be exact")
        for value, label in (
            (self.expected_provider, "provider"),
            (self.expected_provider_document_id, "provider document"),
            (self.expected_security_id, "security"),
            (self.expected_raw_file_relpath, "raw relpath"),
            (self.expected_raw_file_hash, "raw hash"),
            (self.parser_artifact_relpath, "parser artifact relpath"),
            (self.provider_document_relpath, "provider document relpath"),
            (self.created_outbox_event_id, "created outbox event"),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"v4 ingress {label} is invalid")
        for value, label in (
            (self.expected_raw_file_relpath, "raw"),
            (self.parser_artifact_relpath, "parser artifact"),
            (self.provider_document_relpath, "provider document"),
        ):
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"v4 ingress {label} path is unsafe")
        if type(self.parser_target) is not ParserTargetIdentity:
            raise ValueError("v4 ingress parser target must be exact")
        target_exact = json.dumps(
            self.parser_target.to_payload(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        target_sha256 = "sha256:" + hashlib.sha256(target_exact).hexdigest()
        source = self.proposal.credit_envelope.reservation_input.value
        if (
            target_sha256 != self.proposal.parser_target_sha256
            or self.parser_target.runtime_bundle_identity_sha256
            != self.proposal.runtime_epoch_sha256
            or self.expected_raw_file_hash != source.source_pdf_sha256
            or self.source_observation.sha256 != source.source_pdf_sha256
            or self.source_observation.byte_count != source.source_byte_count
            or self.source_observation.page_count != source.source_page_count
            or not self.parser_target.full_pdf
            or self.parser_target.start_page is not None
            or self.parser_target.end_page is not None
        ):
            raise ValueError("v4 ingress immutable identities drifted")
        if (
            not isinstance(self.started_at, datetime)
            or self.started_at.tzinfo is None
            or self.started_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("v4 ingress start time must be UTC aware")
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or self.max_retries < 1
        ):
            raise ValueError("v4 ingress retry limit is invalid")
        if self.scope_classes is not None and (
            type(self.scope_classes) is not tuple
            or not self.scope_classes
            or any(type(value) is not str or not value for value in self.scope_classes)
        ):
            raise ValueError("v4 ingress scope classes are invalid")


@dataclass(frozen=True, slots=True)
class V4InitialIngressReconciliation:
    authority: RemoteParseV4Authority
    processing_run: ProcessingRun
    outbox_event: OutboxEvent

    def __post_init__(self) -> None:
        if (
            type(self.authority) is not RemoteParseV4Authority
            or type(self.processing_run) is not ProcessingRun
            or type(self.outbox_event) is not OutboxEvent
        ):
            raise ValueError("v4 ingress reconciliation is invalid")


def initial_processing_run_v4(command: V4InitialIngressCommit) -> ProcessingRun:
    proposal = command.proposal
    target = command.parser_target
    return ProcessingRun(
        processing_run_id=proposal.processing_run_id,
        document_id=proposal.document_id,
        artifact_owner_processing_run_id=proposal.processing_run_id,
        run_kind="parse",
        status="running",
        parser_name=target.name,
        parser_version=target.package_version,
        parser_backend=target.backend,
        parser_method=target.method,
        parser_language=target.language,
        parser_target_identity=target.to_payload(),
        input_raw_file_hash=command.expected_raw_file_hash,
        parser_artifact_relpath=command.parser_artifact_relpath,
        provider_document_relpath=command.provider_document_relpath,
        started_at=command.started_at,
        is_active=False,
    )


def initial_processing_run_created_event_v4(
    command: V4InitialIngressCommit,
) -> OutboxEvent:
    proposal = command.proposal
    return OutboxEvent(
        event_id=command.created_outbox_event_id,
        event_kind="processing_run_created",
        change_kind="observed",
        subject_kind="processing_run",
        subject_ref=proposal.processing_run_id,
        document_id=proposal.document_id,
        processing_run_id=proposal.processing_run_id,
        payload={"document_id": proposal.document_id, "status": "running"},
        occurred_at=command.started_at,
    )


def initial_prepared_authority_matches_v4(
    authority: RemoteParseV4Authority,
    proposal: V4PreparedProposal,
) -> bool:
    if authority.attempt_id != proposal.attempt_id:
        return False
    expected = bind_v4_prepared_proposal(
        proposal,
        attempt_generation=authority.attempt_generation,
    )
    expected_evidence = encode_remote_parse_evidence_v4(
        expected.preparation_intent
    )
    return (
        authority.checkpoint_history[0] == expected.checkpoint
        and authority.reservation == expected.reservation
        and authority.parser_target_sha256 == expected.parser_target_sha256
        and authority.client_submit_key == expected.client_submit_key
        and authority.execution_spec == expected.execution_spec
        and any(item == expected_evidence for item in authority.evidence)
    )


class RemoteParseV4IngressCommitter(Protocol):
    def reject_source(self, command: V4SourceRejectionCommit) -> None: ...

    def reconcile_source_rejection(self, command: V4SourceRejectionCommit) -> None: ...

    def commit(
        self,
        command: V4InitialIngressCommit,
    ) -> RemoteParseV4Authority: ...

    def reconcile(
        self,
        command: V4InitialIngressCommit,
    ) -> V4InitialIngressReconciliation: ...


__all__ = [
    "RemoteParseV4IngressCommitter",
    "V4InitialIngressCommit",
    "V4InitialIngressDrift",
    "V4InitialIngressError",
    "V4InitialIngressNotCommitted",
    "V4InitialIngressNotEligible",
    "V4InitialIngressReconciliation",
    "initial_prepared_authority_matches_v4",
    "initial_processing_run_created_event_v4",
    "initial_processing_run_v4",
]
