"""Controlled filesystem path construction."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from disclosure_anchor.domain.errors import PathSafetyError
from disclosure_anchor.settings import Settings


_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe_component(value: str, *, label: str) -> str:
    if not _SAFE_COMPONENT_RE.fullmatch(value):
        raise PathSafetyError(f"unsafe {label}: {value!r}")
    if value in {".", ".."}:
        raise PathSafetyError(f"unsafe {label}: {value!r}")
    return value


def _safe_provider_document_id(value: str) -> str:
    if _SAFE_COMPONENT_RE.fullmatch(value):
        return value
    if not value or value in {".", ".."}:
        raise PathSafetyError(f"unsafe provider_document_id: {value!r}")
    if "/" in value or "\\" in value or "\x00" in value:
        raise PathSafetyError(f"unsafe provider_document_id: {value!r}")
    if value[0] == ".":
        raise PathSafetyError(f"unsafe provider_document_id: {value!r}")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise PathSafetyError(f"unsafe provider_document_id: {value!r}")
    return value


def _safe_extension(extension: str) -> str:
    if not re.fullmatch(r"\.[A-Za-z0-9]+", extension):
        raise PathSafetyError(f"unsafe extension: {extension!r}")
    return extension.lower()


def _hash_digest(raw_file_hash: str) -> str:
    digest = raw_file_hash.partition(":")[2] or raw_file_hash
    digest = digest.lower()
    if not re.fullmatch(r"[a-f0-9]{16,128}", digest):
        raise PathSafetyError("raw_file_hash must contain a hex digest")
    return digest


def _assert_relative(path: Path) -> Path:
    if path.is_absolute() or ".." in path.parts:
        raise PathSafetyError(f"path is not a safe relative path: {path}")
    return path


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


class FileStorePathBuilder:
    """Build storage relpaths and controlled runtime paths."""

    def __init__(self, settings: Settings):
        self._settings = settings

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
        provider_part = _safe_component(provider, label="provider")
        security_part = _safe_component(security_code, label="security_code")
        year_part = _safe_component(str(year), label="year")
        provider_document_part = _safe_provider_document_id(provider_document_id)
        digest = _hash_digest(raw_file_hash)
        relpath = (
            Path("raw_documents")
            / provider_part
            / security_part
            / year_part
            / provider_document_part
            / f"sha256_{digest}{_safe_extension(extension)}"
        )
        return _assert_relative(relpath)

    def data_path(self, relpath: Path) -> Path:
        relpath = _assert_relative(relpath)
        path = self._settings.disclosure_data_root / "data" / relpath
        data_root = self._settings.disclosure_data_root / "data"
        if not _is_relative_to(path, data_root):
            raise PathSafetyError(f"data path escapes root: {path}")
        return path

    def parser_artifacts_root_relpath(self, *, document_id: str, processing_run_id: str) -> Path:
        relpath = Path("parser_artifacts") / _safe_component(
            document_id, label="document_id"
        ) / _safe_component(processing_run_id, label="processing_run_id")
        return _assert_relative(relpath)

    def parser_run_artifacts_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        relpath = (
            Path("parser_artifacts")
            / _safe_component(provider, label="provider")
            / _safe_component(security_code, label="security_code")
            / _safe_provider_document_id(provider_document_id)
            / _safe_component(processing_run_id, label="processing_run_id")
        )
        return _assert_relative(relpath)

    def normalized_ir_relpath(self, *, document_id: str, processing_run_id: str) -> Path:
        relpath = (
            Path("derived")
            / "normalized_ir"
            / _safe_component(document_id, label="document_id")
            / _safe_component(processing_run_id, label="processing_run_id")
            / "normalized_ir.v2.json"
        )
        return _assert_relative(relpath)

    def normalized_ir_run_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        relpath = (
            Path("derived")
            / "normalized_ir"
            / _safe_component(provider, label="provider")
            / _safe_component(security_code, label="security_code")
            / _safe_provider_document_id(provider_document_id)
            / _safe_component(processing_run_id, label="processing_run_id")
            / "normalized_ir.v2.json"
        )
        return _assert_relative(relpath)

    def document_units_snapshot_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        relpath = (
            Path("derived")
            / "document_unit_snapshots"
            / _safe_component(provider, label="provider")
            / _safe_component(security_code, label="security_code")
            / _safe_provider_document_id(provider_document_id)
            / _safe_component(processing_run_id, label="processing_run_id")
            / "document_units.v1.jsonl"
        )
        return _assert_relative(relpath)

    def runtime_tmp_path(self, name: str | None = None) -> Path:
        tmp_root = self._settings.disclosure_runtime_root / "tmp"
        if name is None:
            return tmp_root
        path = tmp_root / _safe_component(name, label="runtime_tmp_name")
        if not _is_relative_to(path, tmp_root):
            raise PathSafetyError(f"runtime tmp path escapes root: {path}")
        return path

    def runtime_quarantine_path(
        self,
        *,
        provider: str,
        provider_document_id: str,
        name: str,
    ) -> Path:
        quarantine_root = self._settings.disclosure_runtime_root / "quarantine"
        path = (
            quarantine_root
            / _safe_component(provider, label="provider")
            / _safe_provider_document_id(provider_document_id)
            / _safe_component(name, label="quarantine_name")
        )
        if not _is_relative_to(path, quarantine_root):
            raise PathSafetyError(f"quarantine path escapes root: {path}")
        return path
