"""Internal ID helpers.

The service keeps provider IDs separate from internal IDs. New internal IDs use
ULID payloads with short domain prefixes.
"""

from __future__ import annotations

import re
import secrets
import time
from typing import NewType


CompanyId = NewType("CompanyId", str)
SecurityId = NewType("SecurityId", str)
TrackedCompanyId = NewType("TrackedCompanyId", str)
SourceAccessId = NewType("SourceAccessId", str)
SourceCheckpointId = NewType("SourceCheckpointId", str)
DocumentId = NewType("DocumentId", str)
ProcessingRunId = NewType("ProcessingRunId", str)
AssetId = NewType("AssetId", str)
OutboxEventId = NewType("OutboxEventId", str)
CompanyIdentifierId = NewType("CompanyIdentifierId", str)

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*_[0-9A-HJKMNP-TV-Z]{26}$")


def _encode_crockford(value: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0b11111])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid() -> str:
    """Create a monotonic-sortable ULID string using stdlib primitives."""

    timestamp_ms = int(time.time() * 1000)
    if timestamp_ms >= 1 << 48:
        raise OverflowError("ULID timestamp exceeds 48 bits")
    randomness = secrets.randbits(80)
    return _encode_crockford(timestamp_ms, 10) + _encode_crockford(randomness, 16)


def new_id(prefix: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", prefix):
        raise ValueError(f"invalid id prefix: {prefix!r}")
    return f"{prefix}_{new_ulid()}"


def is_ulid(value: str) -> bool:
    return bool(_ULID_RE.fullmatch(value))


def is_internal_id(value: str) -> bool:
    return bool(_ID_RE.fullmatch(value))


def new_company_id() -> CompanyId:
    return CompanyId(new_id("co"))


def new_security_id() -> SecurityId:
    return SecurityId(new_id("sec"))


def new_tracked_company_id() -> TrackedCompanyId:
    return TrackedCompanyId(new_id("tc"))


def new_source_access_id() -> SourceAccessId:
    return SourceAccessId(new_id("sa"))


def new_source_checkpoint_id() -> SourceCheckpointId:
    return SourceCheckpointId(new_id("sc"))


def new_document_id() -> DocumentId:
    return DocumentId(new_id("doc"))


def new_processing_run_id() -> ProcessingRunId:
    return ProcessingRunId(new_id("run"))


def new_asset_id() -> AssetId:
    return AssetId(new_id("du"))


def new_outbox_event_id() -> OutboxEventId:
    return OutboxEventId(new_id("oe"))


def new_company_identifier_id() -> CompanyIdentifierId:
    return CompanyIdentifierId(new_id("ci"))
