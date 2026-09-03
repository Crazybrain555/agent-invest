"""File-store port contracts."""

from __future__ import annotations

from collections.abc import Sequence

from pathlib import Path
from typing import Protocol

from dataclasses import dataclass

from disclosure_anchor.domain.value_objects import QuarantineReason


@dataclass(frozen=True)
class RawDocumentWriteResult:
    relpath: Path
    raw_file_hash: str
    byte_count: int
    created: bool


@dataclass(frozen=True)
class RawDocumentVerification:
    relpath: Path
    expected_hash: str
    actual_hash: str | None
    ok: bool
    message: str


@dataclass(frozen=True)
class QuarantineResult:
    path: Path
    reason: QuarantineReason
    byte_count: int


@dataclass(frozen=True)
class ArtifactWriteResult:
    relpath: Path
    artifact_hash: str
    byte_count: int


class FileStorePathPort(Protocol):
    def raw_document_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        year: int | str,
        provider_document_id: str,
        raw_file_hash: str,
        extension: str = ".pdf",
    ) -> Path:
        ...

    def data_path(self, relpath: Path) -> Path:
        ...

    def parser_artifacts_root_relpath(self, *, document_id: str, processing_run_id: str) -> Path:
        ...

    def parser_run_artifacts_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        ...

    def normalized_ir_relpath(self, *, document_id: str, processing_run_id: str) -> Path:
        ...

    def normalized_ir_run_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        ...

    def provider_document_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        artifact_owner_processing_run_id: str,
    ) -> Path:
        ...

    def document_units_snapshot_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        ...

    def semantic_route_receipts_v3_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        ...

    def atomic_publication_preparation_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        ...

    def atomic_publication_readiness_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        ...

    def runtime_tmp_path(self, name: str | None = None) -> Path:
        ...

    def runtime_quarantine_path(
        self,
        *,
        provider: str,
        provider_document_id: str,
        name: str,
    ) -> Path:
        ...


class RawDocumentStorePort(Protocol):
    def put_raw_document(
        self,
        *,
        provider: str,
        security_code: str,
        year: int | str,
        provider_document_id: str,
        input_file: Path,
        expected_raw_file_hash: str | None = None,
    ) -> RawDocumentWriteResult:
        ...

    def verify_raw_document(
        self, *, relpath: Path, expected_hash: str
    ) -> RawDocumentVerification:
        ...

    def quarantine_raw_document(
        self,
        *,
        provider: str,
        provider_document_id: str,
        input_file: Path,
        reason: QuarantineReason,
    ) -> QuarantineResult:
        ...


class ArtifactStorePort(Protocol):
    def write_json_atomic(self, *, relpath: Path, payload: object) -> ArtifactWriteResult:
        ...

    def write_jsonl_atomic(
        self, *, relpath: Path, rows: Sequence[object]
    ) -> ArtifactWriteResult:
        ...

    def write_text_atomic(self, *, relpath: Path, text: str) -> ArtifactWriteResult:
        ...
