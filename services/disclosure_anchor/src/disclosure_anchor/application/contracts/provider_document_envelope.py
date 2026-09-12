"""Encode one closed MinerU Medium provider-document record without legacy NIR.

This module is a structural codec, not the source-admission boundary.  The
sole-writer path must re-read the immutable MinerU bundle and require exact
``ProviderDocument`` equality before any decoded record reaches Build,
retrieval, or publication.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re
import unicodedata

from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)

from disclosure_anchor.application.contracts.provider_document import ProviderDocument
from disclosure_anchor.application.contracts._provider_content import (
    ProviderDocumentEnvelopeError,
    provider_document_to_payload as _provider_document_payload,
    provider_document_from_payload as _provider_document_from_payload,
    validate_provider_content,
    _canonical_json_bytes,
    _exact_mapping,
    _integer,
    _reject_json_constant,
    _text,
    _unique_json_object,
)


PROVIDER_DOCUMENT_CONTRACT_VERSION = "provider_document.v1"
PROVIDER_DOCUMENT_FILENAME = "provider_document.v1.json"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
@dataclass(frozen=True, slots=True)
class ProviderDocumentEnvelope:
    """One canonical parse-owner record awaiting source admission."""

    document_id: str
    artifact_owner_processing_run_id: str
    provider: str
    provider_document_id: str
    source_pdf_relpath: str
    input_raw_file_hash: str
    source_pdf_page_count: int
    parser_artifact_root_relpath: str
    parser_target_identity: ParserTargetIdentity
    provider_document: ProviderDocument
    contract_version: str = PROVIDER_DOCUMENT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != PROVIDER_DOCUMENT_CONTRACT_VERSION:
            raise ProviderDocumentEnvelopeError(
                "provider document contract version is unsupported"
            )
        _identifier(self.document_id, label="document_id")
        _identifier(
            self.artifact_owner_processing_run_id,
            label="artifact_owner_processing_run_id",
        )
        _identifier(self.provider, label="provider")
        _safe_provider_document_id(self.provider_document_id)
        _safe_relpath(
            self.source_pdf_relpath,
            label="source_pdf_relpath",
            root="raw_documents",
        )
        _safe_relpath(
            self.parser_artifact_root_relpath,
            label="parser_artifact_root_relpath",
            root="parser_artifacts",
        )
        source_parts = PurePosixPath(self.source_pdf_relpath).parts
        if (
            len(source_parts) != 6
            or source_parts[1] != self.provider
            or source_parts[4] != self.provider_document_id
        ):
            raise ProviderDocumentEnvelopeError(
                "source PDF path does not bind provider and provider document"
            )
        _identifier(source_parts[2], label="source security_code")
        _identifier(source_parts[3], label="source year")
        parser_parts = PurePosixPath(self.parser_artifact_root_relpath).parts
        source_digest_name = "sha256_" + self.input_raw_file_hash.removeprefix(
            "sha256:"
        )
        if (
            len(parser_parts) != 7
            or parser_parts[1] != self.provider
            or parser_parts[2] != source_parts[2]
            or parser_parts[3] != self.provider_document_id
            or parser_parts[4] != self.artifact_owner_processing_run_id
            or parser_parts[5] != source_digest_name
            or parser_parts[6] != "hybrid_auto"
        ):
            raise ProviderDocumentEnvelopeError(
                "parser artifact root does not bind provider and artifact owner"
            )
        _identifier(parser_parts[2], label="parser security_code")
        if not _SHA256_RE.fullmatch(self.input_raw_file_hash):
            raise ProviderDocumentEnvelopeError("input raw file hash is invalid")
        if self.input_raw_file_hash != self.provider_document.source_pdf_sha256:
            raise ProviderDocumentEnvelopeError(
                "input raw file hash differs from provider source identity"
            )
        source_name = PurePosixPath(self.source_pdf_relpath).name
        if source_name != source_digest_name + ".pdf":
            raise ProviderDocumentEnvelopeError(
                "source PDF path does not match its registered hash"
            )
        if (
            isinstance(self.source_pdf_page_count, bool)
            or not isinstance(self.source_pdf_page_count, int)
            or self.source_pdf_page_count < 1
        ):
            raise ProviderDocumentEnvelopeError(
                "source PDF page count must be a positive integer"
            )
        if self.source_pdf_page_count != len(self.provider_document.pages):
            raise ProviderDocumentEnvelopeError(
                "source PDF page count differs from provider pages"
            )
        validate_provider_content(
            self.provider_document, self.parser_target_identity
        )

    @classmethod
    def build(
        cls,
        *,
        document_id: str,
        artifact_owner_processing_run_id: str,
        provider: str,
        provider_document_id: str,
        source_pdf_relpath: str,
        source_pdf_page_count: int,
        parser_artifact_root_relpath: str,
        parser_target_identity: ParserTargetIdentity,
        provider_document: ProviderDocument,
    ) -> "ProviderDocumentEnvelope":
        return cls(
            document_id=document_id,
            artifact_owner_processing_run_id=artifact_owner_processing_run_id,
            provider=provider,
            provider_document_id=provider_document_id,
            source_pdf_relpath=source_pdf_relpath,
            input_raw_file_hash=provider_document.source_pdf_sha256,
            source_pdf_page_count=source_pdf_page_count,
            parser_artifact_root_relpath=parser_artifact_root_relpath,
            parser_target_identity=parser_target_identity,
            provider_document=provider_document,
        )


def provider_document_envelope_to_payload(
    envelope: ProviderDocumentEnvelope,
) -> dict[str, object]:
    """Return the closed JSON value written by the artifact store."""

    payload = _provider_document_envelope_payload(envelope)
    provider_document_envelope_from_payload(payload)
    return payload


def _provider_document_envelope_payload(
    envelope: ProviderDocumentEnvelope,
) -> dict[str, object]:
    return {
        "artifact_owner_processing_run_id": (envelope.artifact_owner_processing_run_id),
        "contract_version": envelope.contract_version,
        "document_id": envelope.document_id,
        "input_raw_file_hash": envelope.input_raw_file_hash,
        "parser_artifact_root_relpath": envelope.parser_artifact_root_relpath,
        "parser_target_identity": envelope.parser_target_identity.to_payload(),
        "provider": envelope.provider,
        "provider_document": _provider_document_payload(envelope.provider_document),
        "provider_document_id": envelope.provider_document_id,
        "source_pdf_page_count": envelope.source_pdf_page_count,
        "source_pdf_relpath": envelope.source_pdf_relpath,
    }


def provider_document_envelope_from_payload(value: object) -> ProviderDocumentEnvelope:
    """Structurally decode one closed ``provider_document.v1`` JSON value.

    This checks the record's own shape, paths, identities, and canonical raw
    fragment hashes.  It deliberately does not prove that duplicated typed
    projections equal the hash-bound MinerU files; that stronger check belongs
    to the sole-writer source-admission path.
    """

    payload = _exact_mapping(
        value,
        keys={
            "artifact_owner_processing_run_id",
            "contract_version",
            "document_id",
            "input_raw_file_hash",
            "parser_artifact_root_relpath",
            "parser_target_identity",
            "provider",
            "provider_document",
            "provider_document_id",
            "source_pdf_page_count",
            "source_pdf_relpath",
        },
        label="provider document envelope",
    )
    try:
        target = ParserTargetIdentity.from_payload(payload["parser_target_identity"])
    except ValueError as exc:
        raise ProviderDocumentEnvelopeError(
            "parser target identity is invalid"
        ) from exc
    return ProviderDocumentEnvelope(
        contract_version=_text(payload["contract_version"], "contract_version"),
        document_id=_text(payload["document_id"], "document_id"),
        artifact_owner_processing_run_id=_text(
            payload["artifact_owner_processing_run_id"],
            "artifact_owner_processing_run_id",
        ),
        provider=_text(payload["provider"], "provider"),
        provider_document_id=_text(
            payload["provider_document_id"], "provider_document_id"
        ),
        source_pdf_relpath=_text(payload["source_pdf_relpath"], "source_pdf_relpath"),
        input_raw_file_hash=_text(
            payload["input_raw_file_hash"], "input_raw_file_hash"
        ),
        source_pdf_page_count=_integer(
            payload["source_pdf_page_count"], "source_pdf_page_count"
        ),
        parser_artifact_root_relpath=_text(
            payload["parser_artifact_root_relpath"],
            "parser_artifact_root_relpath",
        ),
        parser_target_identity=target,
        provider_document=_provider_document_from_payload(payload["provider_document"]),
    )


def provider_document_envelope_to_bytes(
    envelope: ProviderDocumentEnvelope,
) -> bytes:
    """Encode the one canonical byte representation bound by artifact_hash."""

    return _canonical_json_bytes(provider_document_envelope_to_payload(envelope))


def provider_document_envelope_from_bytes(
    value: bytes,
) -> ProviderDocumentEnvelope:
    """Structurally decode only the canonical bytes of one envelope.

    Decoding alone never authorizes the returned object for Build, retrieval,
    or publication.  See the module-level source-admission requirement.
    """

    try:
        decoded = json.loads(
            value.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProviderDocumentEnvelopeError(
            "provider document envelope is not valid canonical JSON"
        ) from exc
    envelope = provider_document_envelope_from_payload(decoded)
    if provider_document_envelope_to_bytes(envelope) != value:
        raise ProviderDocumentEnvelopeError(
            "provider document envelope bytes are not canonical"
        )
    return envelope


def _identifier(value: str, *, label: str) -> None:
    if not _IDENTIFIER_RE.fullmatch(value) or value in {".", ".."}:
        raise ProviderDocumentEnvelopeError(f"{label} is unsafe")


def _safe_relpath(value: str, *, label: str, root: str) -> None:
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ProviderDocumentEnvelopeError(f"{label} is unsafe")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or path.parts[0] != root
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ProviderDocumentEnvelopeError(f"{label} is unsafe")


def _safe_provider_document_id(value: str) -> None:
    if (
        not value
        or value in {".", ".."}
        or value[0] == "."
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ProviderDocumentEnvelopeError("provider_document_id is unsafe")


__all__ = [
    "PROVIDER_DOCUMENT_CONTRACT_VERSION",
    "PROVIDER_DOCUMENT_FILENAME",
    "ProviderDocumentEnvelope",
    "ProviderDocumentEnvelopeError",
    "provider_document_envelope_from_bytes",
    "provider_document_envelope_from_payload",
    "provider_document_envelope_to_bytes",
    "provider_document_envelope_to_payload",
]
