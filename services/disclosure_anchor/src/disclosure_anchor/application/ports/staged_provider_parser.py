"""Optional two-stage provider parser contract.

The legacy parser port remains authoritative until an adapter can prove the
remote task's terminal state and result-artifact ownership independently of
local download and persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.ports.provider_parser import ProviderParserResult


@dataclass(frozen=True, slots=True)
class RemoteArtifactReceipt:
    """Content-free, durable identity for one terminal remote result."""

    attempt_identity: str
    fence_identity: str
    artifact_owner_identity: str
    artifact_byte_count: int
    artifact_sha256: str = ""
    source_pdf_sha256: str = ""
    resume_token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.attempt_identity,
                self.fence_identity,
                self.artifact_owner_identity,
            )
        ):
            raise ValueError("remote artifact identities must be non-empty")
        if self.artifact_byte_count < 0:
            raise ValueError("remote artifact byte count must be non-negative")
        for value, label in (
            (self.artifact_sha256, "remote artifact identity"),
            (self.source_pdf_sha256, "remote source identity"),
        ):
            if not value:
                continue
            candidate = value[7:] if label == "remote source identity" and value.startswith("sha256:") else value
            if (
                len(candidate) != 64
                or candidate != candidate.lower()
                or any(char not in "0123456789abcdef" for char in candidate)
                or (label == "remote source identity" and not value.startswith("sha256:"))
            ):
                raise ValueError(f"{label} must be canonical sha256")


class RemoteProviderParseHandle(Protocol):
    """One accepted remote task whose local result has not been materialized."""

    def wait_terminal(self) -> RemoteArtifactReceipt:
        """Return only after terminal state and result ownership are proven."""

    def materialize(
        self,
        *,
        receipt: RemoteArtifactReceipt,
        output_dir: Path,
        source_pdf_sha256: str,
    ) -> ProviderParserResult:
        """Download, verify and decode the owned result into local artifacts."""

    def cancel_and_drain(self) -> None:
        """Close admission and prove the accepted remote task terminal."""

    def acknowledge_after_finish_committed(
        self, *, receipt: RemoteArtifactReceipt, checkpoint_state: str
    ) -> None:
        """ACK only after the durable DB checkpoint is exactly finish_committed."""

    def acknowledge_after_failure_committed(self, *, checkpoint_state: str) -> None:
        """ACK a remote failure only after its durable failure checkpoint."""


class StagedProviderDocumentParserPort(Protocol):
    """Capability port; absence keeps the existing synchronous parser path."""

    def begin_remote_parse(
        self,
        *,
        input_pdf: Path,
        options: ParserOptions,
        source_pdf_sha256: str,
        attempt_identity: str,
        fence_identity: str,
        submission_epoch_unix: int,
    ) -> RemoteProviderParseHandle:
        ...

    def resume_remote_parse(
        self,
        *,
        receipt: RemoteArtifactReceipt,
        options: ParserOptions,
    ) -> RemoteProviderParseHandle:
        """Rehydrate a terminal result from durable state after restart."""


__all__ = [
    "RemoteArtifactReceipt",
    "RemoteProviderParseHandle",
    "StagedProviderDocumentParserPort",
]
