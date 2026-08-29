"""Private durable checkpoint contract for staged whole-document parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Literal


AttemptState = Literal[
    "prepared",
    "submitted",
    "remote_terminal",
    "materializing",
    "local_materialized",
    "finish_committed",
    "acked",
    "remote_failed",
    "local_failed",
    "superseded",
]

NONFINAL_STATES = frozenset(
    {"prepared", "submitted", "remote_terminal", "materializing", "local_materialized", "finish_committed"}
)
FINAL_STATES = frozenset({"acked", "remote_failed", "local_failed", "superseded"})
ALLOWED_TRANSITIONS = frozenset(
    {
        ("prepared", "remote_failed"),
        ("prepared", "superseded"),
        ("submitted", "remote_failed"),
        ("submitted", "superseded"),
        ("remote_terminal", "materializing"),
        ("remote_terminal", "local_failed"),
        ("remote_terminal", "superseded"),
        ("materializing", "local_materialized"),
        ("materializing", "local_failed"),
        ("local_materialized", "finish_committed"),
        ("local_materialized", "local_failed"),
        ("finish_committed", "acked"),
    }
)
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "attempt_identity",
        "fence_identity",
        "source_pdf_sha256",
        "artifact_owner_identity",
        "artifact_byte_count",
        "artifact_sha256",
        "resume_token_sha256",
    }
)


class RemoteParseCheckpointConflict(RuntimeError):
    """A stale fence/version or conflicting terminal observation lost CAS."""


@dataclass(frozen=True, slots=True)
class TerminalReceipt:
    attempt_identity: str
    fence_identity: str
    source_pdf_sha256: str
    artifact_owner_identity: str
    artifact_byte_count: int
    artifact_sha256: str
    resume_token_sha256: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.attempt_identity, "attempt identity"),
            (self.fence_identity, "fence identity"),
            (self.artifact_owner_identity, "artifact owner identity"),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ValueError(f"invalid {label}")
        for value, label in (
            (self.source_pdf_sha256, "source PDF SHA"),
            (self.artifact_sha256, "artifact SHA"),
            (self.resume_token_sha256, "resume token SHA"),
        ):
            if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                raise ValueError(f"invalid canonical {label}")
        if isinstance(self.artifact_byte_count, bool) or not isinstance(
            self.artifact_byte_count, int
        ) or self.artifact_byte_count < 1:
            raise ValueError("artifact byte count must be a positive exact integer")


@dataclass(frozen=True, slots=True)
class EncodedTerminalReceipt:
    receipt: TerminalReceipt
    exact_bytes: bytes = field(repr=False)
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.exact_bytes, bytes):
            raise ValueError("terminal receipt exact bytes must be bytes")
        if not self.exact_bytes or len(self.exact_bytes) > 65536:
            raise ValueError("terminal receipt bytes are outside the closed envelope")
        if isinstance(self.byte_count, bool) or self.byte_count != len(self.exact_bytes):
            raise ValueError("terminal receipt byte count differs from exact bytes")
        expected_sha = "sha256:" + hashlib.sha256(self.exact_bytes).hexdigest()
        if self.sha256 != expected_sha:
            raise ValueError("terminal receipt SHA differs from exact bytes")
        if self.exact_bytes != _canonical_terminal_receipt_bytes(self.receipt):
            raise ValueError("terminal receipt projection differs from exact bytes")


@dataclass(frozen=True, slots=True)
class RemoteParseAttempt:
    attempt_id: str
    processing_run_id: str
    document_id: str
    attempt_generation: int
    fence_identity: str
    source_pdf_sha256: str
    parser_target_sha256: str
    request_sha256: str
    runtime_epoch_sha256: str
    client_submit_key: str
    state: AttemptState = "prepared"
    is_current: bool = True
    row_version: int = 0
    remote_task_identity: str | None = None
    terminal_receipt_sha256: str | None = None
    terminal_receipt_bytes: bytes | None = field(default=None, repr=False)
    terminal_receipt_byte_count: int | None = None
    result_owner_identity: str | None = None
    result_artifact_sha256: str | None = None
    result_artifact_bytes: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.attempt_id, "attempt id"),
            (self.processing_run_id, "processing run id"),
            (self.document_id, "document id"),
            (self.fence_identity, "fence identity"),
            (self.client_submit_key, "client submit key"),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"invalid {label}")
        for value in (
            self.source_pdf_sha256,
            self.parser_target_sha256,
            self.request_sha256,
            self.runtime_epoch_sha256,
        ):
            if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                raise ValueError("remote parse attempt hash is not canonical")
        if isinstance(self.attempt_generation, bool) or self.attempt_generation < 1:
            raise ValueError("attempt generation must be a positive exact integer")
        if isinstance(self.row_version, bool) or self.row_version < 0:
            raise ValueError("row version must be a non-negative exact integer")


@dataclass(frozen=True, slots=True)
class RemoteParseResumeSecret:
    attempt_id: str
    secret_kind: Literal["submission", "terminal", "ack"]
    token_bytes: bytes = field(repr=False)
    token_sha256: str
    token_byte_count: int

    def __post_init__(self) -> None:
        if not self.token_bytes or len(self.token_bytes) > 65536:
            raise ValueError("resume token bytes are outside the private envelope")
        expected = "sha256:" + hashlib.sha256(self.token_bytes).hexdigest()
        if self.token_sha256 != expected or self.token_byte_count != len(self.token_bytes):
            raise ValueError("resume token identity differs from exact bytes")


def _canonical_terminal_receipt_bytes(receipt: TerminalReceipt) -> bytes:
    payload = {
        "schema": "remote_parse_terminal_receipt.v1",
        "attempt_identity": receipt.attempt_identity,
        "fence_identity": receipt.fence_identity,
        "source_pdf_sha256": receipt.source_pdf_sha256,
        "artifact_owner_identity": receipt.artifact_owner_identity,
        "artifact_byte_count": receipt.artifact_byte_count,
        "artifact_sha256": receipt.artifact_sha256,
        "resume_token_sha256": receipt.resume_token_sha256,
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def encode_terminal_receipt(receipt: TerminalReceipt) -> EncodedTerminalReceipt:
    exact = _canonical_terminal_receipt_bytes(receipt)
    return EncodedTerminalReceipt(
        receipt=receipt,
        exact_bytes=exact,
        sha256="sha256:" + hashlib.sha256(exact).hexdigest(),
        byte_count=len(exact),
    )


def decode_terminal_receipt(exact_bytes: bytes) -> EncodedTerminalReceipt:
    if not exact_bytes or len(exact_bytes) > 65536:
        raise ValueError("terminal receipt bytes are outside the closed envelope")

    def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate terminal receipt field: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            exact_bytes.decode("utf-8"),
            object_pairs_hook=closed_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("terminal receipt is not strict UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _RECEIPT_KEYS:
        raise ValueError("terminal receipt fields are not closed")
    if payload.get("schema") != "remote_parse_terminal_receipt.v1":
        raise ValueError("terminal receipt schema drifted")
    receipt = TerminalReceipt(
        attempt_identity=payload["attempt_identity"],
        fence_identity=payload["fence_identity"],
        source_pdf_sha256=payload["source_pdf_sha256"],
        artifact_owner_identity=payload["artifact_owner_identity"],
        artifact_byte_count=payload["artifact_byte_count"],
        artifact_sha256=payload["artifact_sha256"],
        resume_token_sha256=payload["resume_token_sha256"],
    )
    encoded = encode_terminal_receipt(receipt)
    if encoded.exact_bytes != exact_bytes:
        raise ValueError("terminal receipt is not canonical JSON bytes")
    return encoded


__all__ = [
    "AttemptState",
    "ALLOWED_TRANSITIONS",
    "EncodedTerminalReceipt",
    "FINAL_STATES",
    "NONFINAL_STATES",
    "RemoteParseAttempt",
    "RemoteParseCheckpointConflict",
    "RemoteParseResumeSecret",
    "TerminalReceipt",
    "decode_terminal_receipt",
    "encode_terminal_receipt",
]
