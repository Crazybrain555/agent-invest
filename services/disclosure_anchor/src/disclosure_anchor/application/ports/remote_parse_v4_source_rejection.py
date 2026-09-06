"""Atomic, source-bound disposition before a usable PDF can authorize H0."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.ports.staged_new_work_v4 import (
    V4OrdinaryParseCandidate, V4RejectedSourcePdf,
)
from disclosure_anchor.domain.entities import OutboxEvent, ProcessingRun, outbox_events


@dataclass(frozen=True, slots=True)
class V4SourceRejectionCommit:
    candidate: V4OrdinaryParseCandidate
    rejection: V4RejectedSourcePdf
    parser_target: ParserTargetIdentity
    processing_run_id: str
    parser_artifact_relpath: str
    provider_document_relpath: str
    failed_at: datetime
    created_outbox_event_id: str
    failed_outbox_event_id: str
    max_retries: int
    scope_classes: tuple[str, ...] | None

    def __post_init__(self) -> None:
        if (
            type(self.candidate) is not V4OrdinaryParseCandidate
            or type(self.rejection) is not V4RejectedSourcePdf
            or type(self.parser_target) is not ParserTargetIdentity
            or not self.parser_target.full_pdf
            or self.parser_target.runtime_bundle_identity_sha256 is None
            or self.rejection.sha256 != self.candidate.raw_file_hash
            or (self.candidate.archived_raw_byte_count is not None
                and self.rejection.byte_count != self.candidate.archived_raw_byte_count)
        ):
            raise ValueError("source rejection identity is not closed")
        for value in (self.processing_run_id, self.created_outbox_event_id, self.failed_outbox_event_id):
            if type(value) is not str or not value.strip() or len(value.encode()) > 64:
                raise ValueError("source rejection episode identity is invalid")
        if self.created_outbox_event_id == self.failed_outbox_event_id:
            raise ValueError("source rejection event identities must differ")
        for value in (self.parser_artifact_relpath, self.provider_document_relpath):
            if (type(value) is not str or not value.strip() or PurePosixPath(value).is_absolute()
                    or ".." in PurePosixPath(value).parts):
                raise ValueError("source rejection artifact locator is unsafe")
        if (not isinstance(self.failed_at, datetime) or self.failed_at.tzinfo is None
                or self.failed_at.utcoffset() != timedelta(0)):
            raise ValueError("source rejection time must be UTC aware")
        if type(self.max_retries) is not int or self.max_retries < 1:
            raise ValueError("source rejection retry limit is invalid")
        if self.scope_classes is not None and (
            type(self.scope_classes) is not tuple or not self.scope_classes
            or any(type(value) is not str or not value for value in self.scope_classes)
        ):
            raise ValueError("source rejection scope classes are invalid")


def rejected_source_processing_run(command: V4SourceRejectionCommit) -> ProcessingRun:
    target = command.parser_target
    return ProcessingRun(
        processing_run_id=command.processing_run_id,
        document_id=command.candidate.document_id,
        artifact_owner_processing_run_id=command.processing_run_id,
        run_kind="parse", status="failed",
        parser_name=target.name, parser_version=target.package_version,
        parser_backend=target.backend, parser_method=target.method,
        parser_language=target.language, parser_target_identity=target.to_payload(),
        input_raw_file_hash=command.rejection.sha256,
        parser_artifact_relpath=command.parser_artifact_relpath,
        provider_document_relpath=command.provider_document_relpath,
        started_at=command.failed_at, finished_at=command.failed_at,
        error={
            "stage": "source_observation", "error_code": command.rejection.reason_code,
            "error_class": "source_content", "retryable": False,
            "retry_budget_class": "item", "message": "Archived PDF could not supply a usable whole-document page count.",
        },
        is_active=False,
    )


def rejected_source_events(command: V4SourceRejectionCommit) -> tuple[OutboxEvent, OutboxEvent]:
    error = rejected_source_processing_run(command).error
    assert error is not None
    return (
        replace(outbox_events.processing_run_created(
            document_id=command.candidate.document_id, processing_run_id=command.processing_run_id,
            occurred_at=command.failed_at,
        ), event_id=command.created_outbox_event_id),
        replace(outbox_events.processing_run_failed(
            document_id=command.candidate.document_id, processing_run_id=command.processing_run_id,
            error=error, occurred_at=command.failed_at,
        ), event_id=command.failed_outbox_event_id),
    )


__all__ = ["V4SourceRejectionCommit", "rejected_source_processing_run", "rejected_source_events"]
