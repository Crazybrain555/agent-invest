"""Provider port (framework v1.2 §3, frozen).

Adapters implement DatasetProvider and never touch the database; the registrar
is the only writer. Locators must never contain hosts or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from envelope_kernel import SourceTier, TraceLevel


@dataclass(frozen=True)
class DatasetRequest:
    dataset_key: str
    query_params: dict[str, Any]


@dataclass(frozen=True)
class ScopeHints:
    subject_candidates: list[str] | None = None
    report_period: str | None = None
    event_time: datetime | None = None
    published_at: datetime | None = None
    title: str | None = None
    semantic_key: str | None = None


@dataclass(frozen=True)
class DatasetResult:
    records: list[dict[str, Any]]
    returned_fields: list[str]
    provider_as_of: str | None
    locator: str | None
    raw_asset_ref: str | None = None
    scope: ScopeHints = field(default_factory=ScopeHints)
    warnings: tuple[str, ...] = ()
    stats: dict[str, int] | None = None


class ProviderError(Exception):
    """Provider-side failure (permission, rate limit, connection, bad response).

    The registrar records these as source_access.result_status='error'; an empty
    result set is NOT an error (result_status='empty', per protocol §3.9).
    """


class DatasetProvider(Protocol):
    provider_name: str
    adapter_name: str
    adapter_version: str
    source_tier: SourceTier
    trace_level: TraceLevel

    def fetch(self, request: DatasetRequest) -> DatasetResult: ...
