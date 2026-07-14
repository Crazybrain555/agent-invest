"""Common domain value objects."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Literal


FILING_TYPES = frozenset(
    {
        "annual_report",
        "semiannual_report",
        "quarterly_report",
        "performance_forecast",
        "performance_flash",
        "investor_relations",
        "performance_briefing",
        "inquiry_reply",
        "other",
    }
)
PERIOD_REQUIRED_FILING_TYPES = frozenset(
    {"annual_report", "semiannual_report", "quarterly_report"}
)
PERIOD_RECOMMENDED_FILING_TYPES = frozenset(
    {"performance_forecast", "performance_flash", "performance_briefing"}
)
OFFICIAL_DISCLOSURE_PROVIDERS = frozenset({"cninfo"})
QuarantineReason = Literal[
    "invalid_raw_document",
    "expected_hash_mismatch",
    "io_error",
    # Archive already holds different bytes for this identity (round23):
    # the fresh download is preserved as evidence, operator resolves.
    "raw_archive_conflict",
]

_REPORT_PERIOD_RE = re.compile(r"^\d{4}(A|Q[1-4])$")
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderRef:
    provider: str
    provider_document_id: str

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError("provider is required")
        if not self.provider_document_id:
            raise ValueError("provider_document_id is required")


@dataclass(frozen=True)
class ContentHash:
    algorithm: str
    digest: str

    def __post_init__(self) -> None:
        if self.algorithm != "sha256":
            raise ValueError("only sha256 is supported")
        if not self.digest:
            raise ValueError("digest is required")

    @classmethod
    def parse(cls, value: str) -> "ContentHash":
        algorithm, separator, digest = value.partition(":")
        if not separator:
            raise ValueError("hash must use '<algorithm>:<digest>' format")
        return cls(algorithm=algorithm, digest=digest.lower())

    def __str__(self) -> str:
        return f"{self.algorithm}:{self.digest}"


@dataclass(frozen=True)
class ReportPeriod:
    label: str

    @classmethod
    def parse(cls, value: str) -> "ReportPeriod":
        if not _REPORT_PERIOD_RE.fullmatch(value):
            raise ValueError(
                "report_period must match YYYY(A|Q1|Q2|Q3|Q4)"
            )
        return cls(label=value)

    def __str__(self) -> str:
        return self.label


def validate_filing_type(value: str) -> str:
    if value not in FILING_TYPES:
        raise ValueError(f"unsupported filing_type: {value!r}")
    return value


def validate_official_provider(value: str) -> str:
    if value not in OFFICIAL_DISCLOSURE_PROVIDERS:
        raise ValueError(f"unsupported disclosure provider: {value!r}")
    return value


def validate_report_period_for_filing_type(
    *, filing_type: str, report_period: ReportPeriod | None
) -> None:
    if filing_type in PERIOD_REQUIRED_FILING_TYPES and report_period is None:
        raise ValueError(f"report_period is required for {filing_type}")
    if filing_type in PERIOD_RECOMMENDED_FILING_TYPES and report_period is None:
        _LOGGER.warning("report_period is recommended for filing_type=%s", filing_type)
