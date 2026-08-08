"""Central identity gate every current-publication entry point shares.

BuildUnits builds new units and PublishRun activates historically built
runs; both must prove the same facts before any placement or activation:
one immutable source PDF across document/run/NormalizedIR, the current
structure algorithm, the current v2 parser target with an explicitly
resolved remote model for HTTP backends, and a semantically valid
run-bound parse receipt. The gate raises a neutral typed violation that
each use case wraps into its own structured error.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
import re
from typing import Any

from disclosure_anchor.application.contracts.document_structure import (
    DocumentStructureContractError,
    require_current_document_structure,
)
from disclosure_anchor.application.contracts.parse_receipt import (
    PARSE_RECEIPT_ARTIFACT_ROLE,
    ParseReceiptContractError,
    validate_parse_receipt,
)
from disclosure_anchor.application.contracts.parser_target import (
    CURRENT_PARSER_TARGET_CONTRACT_VERSION,
    ParserTargetIdentity,
    ParserTargetIdentityError,
)


class PublicationIdentityViolation(Exception):
    """One publication identity fact failed to close."""

    def __init__(
        self,
        *,
        error_code: str,
        reason_code: str | None,
        message: str,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.reason_code = reason_code
        self.message = message


_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def require_publishable_run_identity(
    *,
    document_raw_file_hash: str | None,
    run_input_raw_file_hash: str | None,
    run_normalized_ir_sha256: str | None,
    actual_normalized_ir_sha256: str,
    normalized_ir: Mapping[str, Any],
    read_artifact_bytes: Callable[[str], bytes],
) -> None:
    """Close the run identity chain for one publication entry point.

    ``read_artifact_bytes`` receives a data-root-relative path and returns
    the artifact bytes; a ``FileNotFoundError`` means the artifact truly
    does not exist (a terminal state), while any other exception —
    storage, mount, or permission trouble — propagates to the caller,
    which owns retryability. The gate verifies receipt bytes against the
    hashed manifest itself, so callers need no prior trust in their
    loader.
    """

    if (
        run_normalized_ir_sha256 is None
        or _SHA256_RE.fullmatch(run_normalized_ir_sha256) is None
    ):
        # A run without a recorded NormalizedIR hash proves nothing about
        # which bytes it built; current publication requires the binding.
        raise PublicationIdentityViolation(
            error_code="RUN_ARTIFACT_HASH_MISSING",
            reason_code="run_artifact_hash_missing",
            message=(
                "current publication requires the processing run to bind "
                "its NormalizedIR bytes via a recorded artifact hash"
            ),
        )
    if actual_normalized_ir_sha256 != run_normalized_ir_sha256:
        raise PublicationIdentityViolation(
            error_code="IR_HASH_MISMATCH",
            reason_code="run_artifact_hash_mismatch",
            message=(
                "NormalizedIR bytes do not hash to the processing run's "
                f"recorded artifact hash: {actual_normalized_ir_sha256} != "
                f"{run_normalized_ir_sha256}"
            ),
        )
    hashes = {
        "document.raw_file_hash": document_raw_file_hash,
        "run.input_raw_file_hash": run_input_raw_file_hash,
        "normalized_ir.source_pdf_sha256": normalized_ir.get(
            "source_pdf_sha256"
        ),
    }
    if None in hashes.values() or len(set(hashes.values())) != 1:
        raise PublicationIdentityViolation(
            error_code="SOURCE_PDF_IDENTITY_MISMATCH",
            reason_code="source_pdf_identity_mismatch",
            message=(
                "source PDF identity chain is broken across "
                f"document/run/NormalizedIR: {hashes}"
            ),
        )
    structure_proof = normalized_ir.get("structure_proof")
    try:
        require_current_document_structure(
            structure_proof if isinstance(structure_proof, Mapping) else {}
        )
    except DocumentStructureContractError as exc:
        raise PublicationIdentityViolation(
            error_code="IR_CONTRACT_TOO_OLD",
            reason_code="structure_proof_reparse_required",
            message=str(exc),
        ) from exc
    try:
        target = ParserTargetIdentity.from_payload(normalized_ir.get("parser"))
    except ParserTargetIdentityError as exc:
        raise PublicationIdentityViolation(
            error_code="PARSER_TARGET_IDENTITY_INVALID",
            reason_code="parser_target_identity_invalid",
            message=str(exc),
        ) from exc
    if target.target_contract_version != CURRENT_PARSER_TARGET_CONTRACT_VERSION:
        raise PublicationIdentityViolation(
            error_code="IR_CONTRACT_TOO_OLD",
            reason_code="parser_target_reparse_required",
            message=(
                "a legacy parser target cannot drive current publication; "
                "reparse the document"
            ),
        )
    if (
        target.backend.endswith("-http-client")
        and target.remote_selection_mode != "explicit"
    ):
        raise PublicationIdentityViolation(
            error_code="REMOTE_MODEL_UNATTESTED",
            reason_code="remote_model_unattested",
            message=(
                "HTTP-backend publication requires an explicitly resolved "
                "remote model identity"
            ),
        )
    _require_valid_receipt(
        normalized_ir,
        read_artifact_bytes=read_artifact_bytes,
    )


def _require_valid_receipt(
    normalized_ir: Mapping[str, Any],
    *,
    read_artifact_bytes: Callable[[str], bytes],
) -> None:
    artifacts = normalized_ir.get("parser_artifacts")
    files = artifacts.get("files") if isinstance(artifacts, Mapping) else None
    descriptor = (
        files.get(PARSE_RECEIPT_ARTIFACT_ROLE)
        if isinstance(files, Mapping)
        else None
    )
    if not isinstance(descriptor, Mapping) or descriptor.get(
        "availability"
    ) != "present":
        raise PublicationIdentityViolation(
            error_code="PARSE_RECEIPT_MISSING",
            reason_code="parse_receipt_missing",
            message=(
                "current publication requires a present run-bound parse "
                "receipt artifact"
            ),
        )
    relpath = descriptor.get("relpath")
    expected_hash = descriptor.get("sha256")
    if not isinstance(relpath, str) or not isinstance(expected_hash, str):
        raise PublicationIdentityViolation(
            error_code="PARSE_RECEIPT_INVALID",
            reason_code="parse_receipt_invalid",
            message="parse receipt manifest descriptor is malformed",
        )
    try:
        raw = read_artifact_bytes(relpath)
    except FileNotFoundError as exc:
        raise PublicationIdentityViolation(
            error_code="PARSE_RECEIPT_MISSING",
            reason_code="parse_receipt_missing",
            message=f"parse receipt artifact does not exist: {exc}",
        ) from exc
    expected_size = descriptor.get("size_bytes")
    if expected_size is not None and expected_size != len(raw):
        raise PublicationIdentityViolation(
            error_code="PARSE_RECEIPT_INVALID",
            reason_code="parse_receipt_invalid",
            message=(
                "parse receipt size differs from the hashed manifest: "
                f"{len(raw)} != {expected_size}"
            ),
        )
    actual_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    if actual_hash != expected_hash:
        raise PublicationIdentityViolation(
            error_code="PARSE_RECEIPT_INVALID",
            reason_code="parse_receipt_invalid",
            message=(
                "parse receipt bytes differ from the hashed manifest: "
                f"{actual_hash} != {expected_hash}"
            ),
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationIdentityViolation(
            error_code="PARSE_RECEIPT_INVALID",
            reason_code="parse_receipt_invalid",
            message=f"parse receipt is not valid UTF-8 JSON: {exc}",
        ) from exc
    parser_payload = normalized_ir.get("parser")
    assert isinstance(parser_payload, Mapping)
    try:
        validate_parse_receipt(
            payload,
            source_pdf_sha256=str(normalized_ir.get("source_pdf_sha256")),
            parser_target_payload=parser_payload,
        )
    except ParseReceiptContractError as exc:
        raise PublicationIdentityViolation(
            error_code="PARSE_RECEIPT_INVALID",
            reason_code="parse_receipt_invalid",
            message=str(exc),
        ) from exc


__all__ = [
    "PublicationIdentityViolation",
    "require_publishable_run_identity",
]
