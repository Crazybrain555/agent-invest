"""Closed private contracts for publish evidence and relay-head persistence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SUPPLEMENT_ID = re.compile(r"pes_[0-9A-HJKMNP-TV-Z]{26}\Z")


class PublishEvidenceConflict(RuntimeError):
    """An append-only evidence identity was replayed with different facts."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _hash(value: str, label: str) -> None:
    if _SHA.fullmatch(value) is None:
        raise ValueError(f"{label} is not a canonical SHA-256")


def _utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{label} must be UTC")


class DurablePublishBaseEvidence(_Closed):
    processing_run_id: str = Field(min_length=1, max_length=64)
    document_id: str = Field(min_length=1, max_length=64)
    source_identity_sha256: str
    source_page_count: int = Field(gt=0)
    publish_precommit_at: datetime

    @model_validator(mode="after")
    def _valid(self) -> "DurablePublishBaseEvidence":
        if isinstance(self.source_page_count, bool):
            raise ValueError("source_page_count must be an integer")
        _hash(self.source_identity_sha256, "source_identity_sha256")
        _utc(self.publish_precommit_at, "publish_precommit_at")
        return self


class DurablePublishSupplementEvidence(_Closed):
    supplement_id: str = Field(min_length=1, max_length=64)
    processing_run_id: str = Field(min_length=1, max_length=64)
    source_identity_sha256: str
    source_page_count: int = Field(gt=0)
    publish_precommit_at: datetime
    host_assignment_identity_sha256: str
    boot_identity_sha256: str
    runtime_bundle_identity_sha256: str
    process_profile_sha256: str
    observer_run_id: str
    observer_receipt_sha256: str
    observer_seal_sha256: str
    observer_contract_version: str
    publish_durable_observed_at: datetime

    @model_validator(mode="after")
    def _valid(self) -> "DurablePublishSupplementEvidence":
        if _SUPPLEMENT_ID.fullmatch(self.supplement_id) is None:
            raise ValueError("supplement_id is not canonical")
        if isinstance(self.source_page_count, bool):
            raise ValueError("source_page_count must be an integer")
        for name in (
            "source_identity_sha256", "host_assignment_identity_sha256",
            "boot_identity_sha256", "runtime_bundle_identity_sha256",
            "process_profile_sha256",
            "observer_receipt_sha256", "observer_seal_sha256",
        ):
            _hash(getattr(self, name), name)
        _utc(self.publish_precommit_at, "publish_precommit_at")
        _utc(self.publish_durable_observed_at, "publish_durable_observed_at")
        if self.publish_durable_observed_at < self.publish_precommit_at:
            raise ValueError("supplement predates the publish commit")
        if self.observer_contract_version != "mineru.synchronized-telemetry-receipt.v2":
            raise ValueError("observer contract version is unsupported")
        try:
            observer_run = uuid.UUID(self.observer_run_id)
        except ValueError as exc:
            raise ValueError("observer_run_id is not a UUID") from exc
        if str(observer_run) != self.observer_run_id or observer_run.variant != uuid.RFC_4122:
            raise ValueError("observer_run_id is not canonical")
        return self


class ProgressRelayResume(_Closed):
    contract_version: str = "mineru.capacity-progress-relay-resume.v1"
    run_id: str
    process_epoch_sha256: str
    runtime_bundle_identity_sha256: str
    process_profile_sha256: str
    clock_domain_identity_sha256: str
    next_sequence: int = Field(ge=0)
    cumulative_unique_source_pages: int = Field(ge=0)
    durable_sources: tuple[tuple[str, str, int], ...]
    previous_checkpoint_sha256: str | None = None

    @model_validator(mode="after")
    def _valid(self) -> "ProgressRelayResume":
        if self.contract_version != "mineru.capacity-progress-relay-resume.v1":
            raise ValueError("progress relay resume version is unsupported")
        try:
            parsed_run = uuid.UUID(self.run_id)
        except ValueError as exc:
            raise ValueError("progress relay run_id is not a UUID") from exc
        if str(parsed_run) != self.run_id or parsed_run.variant != uuid.RFC_4122:
            raise ValueError("progress relay run_id is not canonical")
        for name in (
            "process_epoch_sha256", "runtime_bundle_identity_sha256",
            "process_profile_sha256", "clock_domain_identity_sha256",
        ):
            _hash(getattr(self, name), name)
        if self.previous_checkpoint_sha256 is not None:
            _hash(self.previous_checkpoint_sha256, "previous_checkpoint_sha256")
        for source, profile, pages in self.durable_sources:
            _hash(source, "durable source identity")
            if profile != self.process_profile_sha256 or isinstance(pages, bool) or pages < 1:
                raise ValueError("durable source replay identity is invalid")
        if tuple(sorted(self.durable_sources)) != self.durable_sources:
            raise ValueError("durable source replay is not canonically sorted")
        if len({source for source, _profile, _pages in self.durable_sources}) != len(
            self.durable_sources
        ):
            raise ValueError("durable source replay contains duplicates")
        if sum(pages for _source, _profile, pages in self.durable_sources) != self.cumulative_unique_source_pages:
            raise ValueError("durable source replay total disagrees with cumulative pages")
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        if len(payload) > 1_048_576:
            raise ValueError("progress relay checkpoint exceeds durable byte budget")
        return self


def canonical_resume_bytes(resume: ProgressRelayResume) -> bytes:
    return json.dumps(
        resume.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()


def decode_progress_relay_resume(payload: bytes) -> ProgressRelayResume:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate JSON key {key}")
            result[key] = value
        return result
    try:
        value = json.loads(
            payload, object_pairs_hook=pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("progress relay checkpoint is invalid JSON") from exc
    resume = ProgressRelayResume.model_validate(value)
    if canonical_resume_bytes(resume) != payload:
        raise ValueError("progress relay checkpoint is non-canonical")
    return resume


class EncodedProgressRelayCheckpoint(_Closed):
    relay_id: str = Field(min_length=1, max_length=128)
    row_version: int = Field(ge=0)
    previous_checkpoint_sha256: str | None
    checkpoint_sha256: str
    checkpoint_bytes: bytes
    checkpoint_byte_count: int = Field(gt=0, le=1_048_576)

    @model_validator(mode="after")
    def _valid(self) -> "EncodedProgressRelayCheckpoint":
        _hash(self.checkpoint_sha256, "checkpoint_sha256")
        if self.previous_checkpoint_sha256 is not None:
            _hash(self.previous_checkpoint_sha256, "previous_checkpoint_sha256")
        if (self.row_version == 0) != (self.previous_checkpoint_sha256 is None):
            raise ValueError("relay predecessor shape is invalid")
        if type(self.checkpoint_bytes) is not bytes:
            raise ValueError("checkpoint_bytes must be exact bytes")
        if self.checkpoint_byte_count != len(self.checkpoint_bytes):
            raise ValueError("checkpoint byte count drifted")
        actual = "sha256:" + hashlib.sha256(self.checkpoint_bytes).hexdigest()
        if actual != self.checkpoint_sha256:
            raise ValueError("checkpoint hash drifted")
        resume = decode_progress_relay_resume(self.checkpoint_bytes)
        if self.relay_id != f"{resume.run_id}:{resume.process_epoch_sha256}":
            raise ValueError("relay id is not bound to run/process epoch")
        if self.previous_checkpoint_sha256 != resume.previous_checkpoint_sha256:
            raise ValueError("relay predecessor differs from checkpoint content")
        return self


__all__ = [
    "DurablePublishBaseEvidence", "DurablePublishSupplementEvidence",
    "EncodedProgressRelayCheckpoint", "PublishEvidenceConflict",
    "ProgressRelayResume", "canonical_resume_bytes", "decode_progress_relay_resume",
]
