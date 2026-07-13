"""Map CNINFO WebAPI records into source adapter DTOs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from datetime import date, datetime
import json
from importlib import resources
from pathlib import Path
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo

from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    SourceCompanyProfile,
)
from disclosure_anchor.domain.errors import DisclosureAnchorError
from disclosure_anchor.domain.value_objects import ReportPeriod


CNINFO_PROVIDER = "cninfo"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
# Provider-owned generic buckets: no stable semantic class can be inferred
# from the code alone; narrow title_topic evidence may still classify a row.
ACCEPTED_MISC_CATEGORY_CODES = frozenset(
    {"0123", "012399", "01239999", "352399"}
)


class CninfoMappingError(DisclosureAnchorError):
    """Raised when a CNINFO record violates the pinned field contract."""


@dataclass(frozen=True)
class FilingTypeRule:
    filing_type: str
    keywords: tuple[str, ...]
    match: Literal["any", "all"] = "any"


@dataclass(frozen=True)
class TopicRule:
    """Additive class rule matched against the title regardless of codes.

    Fills provider-code blind spots (e.g. CNINFO files monthly operating
    data under 012305 and never uses 010309): a hit ADDS the class to
    disclosure_topics / the filing_type argmax, it never replaces code hits.
    """

    class_name: str
    keywords: tuple[str, ...]
    match: Literal["any", "all"] = "any"


@dataclass(frozen=True)
class NoiseRule:
    """Absolute processing-exclusion rule matched against the title.

    A hit keeps the document out of download AND parse regardless of codes
    or per-company overrides (phase-1 user ruling: no boilerplate/routine
    filings). Classification views are unaffected — metadata stays visible.
    """

    keywords: tuple[str, ...]
    match: Literal["any", "all"] = "any"


@dataclass(frozen=True)
class FilingTypeRuleBundle:
    version: str
    rules: tuple[FilingTypeRule, ...]
    topic_rules: tuple[TopicRule, ...] = ()
    noise_rules: tuple[NoiseRule, ...] = ()


# CNINFO profiles use the provider-neutral port DTO directly.
CninfoCompanyProfile = SourceCompanyProfile


def load_filing_type_rule_bundle() -> FilingTypeRuleBundle:
    raw = (
        resources.files("disclosure_anchor.adapters.sources.cninfo")
        .joinpath("filing_type_map.json")
        .read_text(encoding="utf-8")
    )
    payload = json.loads(raw)
    rules = tuple(_rule_from_payload(item) for item in payload["rules"])
    topic_rules = tuple(
        TopicRule(
            class_name=str(item["class"]),
            keywords=tuple(str(keyword) for keyword in item["keywords"]),
            match="all" if item.get("match") == "all" else "any",
        )
        for item in payload.get("topic_rules", [])
    )
    noise_rules = tuple(
        NoiseRule(
            keywords=tuple(str(keyword) for keyword in item["keywords"]),
            match="all" if item.get("match") == "all" else "any",
        )
        for item in payload.get("noise_rules", [])
    )
    return FilingTypeRuleBundle(
        version=str(payload["version"]),
        rules=rules,
        topic_rules=topic_rules,
        noise_rules=noise_rules,
    )


def map_p_info3015_record(record: Mapping[str, Any]) -> AnnouncementRef:
    """Map one p_info3015 row. TEXTID is the provider document id."""

    provider_document_id = _required_str(record, "TEXTID")
    return AnnouncementRef(
        provider=CNINFO_PROVIDER,
        provider_document_id=provider_document_id,
        title=_required_str(record, "F002V"),
        download_url=_required_str(record, "F003V"),
        raw_category=_required_str(record, "F006V"),
        announcement_date=_parse_cninfo_date(_required_str(record, "F001D")),
        security_code=_required_str(record, "SECCODE"),
        security_name=_optional_str(record.get("SECNAME")),
        file_size=record.get("F005N"),
        index_updated_at=_parse_cninfo_datetime(record.get("RECTIME")),
        object_id=_optional_int_or_str(record.get("OBJECTID")),
        rec_id=_optional_str(record.get("RECID")),
        format=_optional_str(record.get("F004V")),
        market_code=_optional_str(record.get("F009V")),
        market_name=_optional_str(record.get("F010V")),
        raw_record={str(key): value for key, value in record.items()},
    )


def map_p_stock2100_record(record: Mapping[str, Any]) -> CninfoCompanyProfile:
    return CninfoCompanyProfile(
        security_code=_required_str(record, "SECCODE"),
        security_name=_required_str(record, "SECNAME"),
        legal_name=_required_str(record, "ORGNAME"),
        provider_org_id=_optional_str(record.get("ORGID")),
        uscc=_optional_str(record.get("F050V")),
    )


def map_filing_type(
    raw_category: str,
    *,
    category_names_by_code: Mapping[str, str],
    rule_bundle: FilingTypeRuleBundle | None = None,
) -> str:
    bundle = rule_bundle or load_filing_type_rule_bundle()
    for segment in split_category_segments(raw_category):
        category_name = category_names_by_code.get(segment, "")
        haystacks = (segment, category_name)
        for rule in bundle.rules:
            if _rule_matches(rule=rule, haystacks=haystacks):
                return rule.filing_type
    return "other"


_TITLE_YEAR_RE = re.compile(r"(20\d{2})\s*年")
_TITLE_QUARTER_RE = re.compile(r"第?([一二三四1-4])季")
_QUARTER_BY_TOKEN = {"一": "1", "二": "2", "三": "3", "四": "4", "1": "1", "2": "2", "3": "3", "4": "4"}


def derive_report_period(title: str, *, filing_type: str) -> str | None:
    """Derive report_period from the announcement title (07 §3.2 closed rule).

    p_info3015 has no report-period field, so the fiscal period comes from the
    title text only — never from the announcement date (annual reports are
    published the following year). Underivable titles return None; a null
    period must not block registration.
    """

    year_match = _TITLE_YEAR_RE.search(title)
    if year_match is None:
        return None
    year = year_match.group(1)
    if filing_type == "annual_report":
        label = f"{year}A"
    elif filing_type == "semiannual_report":
        label = f"{year}Q2"
    elif filing_type == "quarterly_report":
        quarter_match = _TITLE_QUARTER_RE.search(title)
        if quarter_match is None:
            return None
        label = f"{year}Q{_QUARTER_BY_TOKEN[quarter_match.group(1)]}"
    else:
        return None
    try:
        ReportPeriod.parse(label)
    except ValueError:
        return None
    return label


def split_category_segments(raw_category: str) -> list[str]:
    return [segment.strip() for segment in raw_category.split("||") if segment.strip()]


@lru_cache(maxsize=1)
def load_class_map() -> dict[str, Any]:
    """The unified class vocabulary (one map, two outputs — design doc §3.1)."""

    return json.loads(
        resources.files("disclosure_anchor.adapters.sources.cninfo")
        .joinpath("class_map.json")
        .read_text(encoding="utf-8")
    )


@lru_cache(maxsize=1)
def load_facet_map() -> dict[str, Any]:
    return json.loads(
        resources.files("disclosure_anchor.adapters.sources.cninfo")
        .joinpath("facet_map.json")
        .read_text(encoding="utf-8")
    )


def load_processing_policy(path: Path) -> tuple[str, ...]:
    """Global processing default (cascade layer 2) — fail-closed on typos.

    The ONE loader for config/processing_policy.json: worker CLI and the
    Filing API resolve the effective-class cascade through this. Validates
    against the unified class vocabulary; not cached so a policy edit is
    picked up by the next run/request.
    """

    payload = json.loads(path.read_text(encoding="utf-8"))
    process = tuple(str(t) for t in payload["process"])
    known = set(load_class_map()["classes"])
    unknown = [c for c in (*process, *payload.get("register_only", ())) if c not in known]
    if unknown:
        raise ValueError(
            f"processing_policy.json 含未知类名 {unknown} — 先跑 make config-check"
        )
    return process


def derive_primary_class(raw_category: str | None, title: str | None) -> str:
    """Python evaluator of the shared vocabulary (view parity, 0021).

    Argmax over the UNION of code-rule hits (class_map priorities) and
    title_topic hits (same priority scale), then the code-less title
    keyword fallback (filing_type_map, file order = precedence), else
    'other'. The views compute the same thing in SQL from
    classification_rule; the 0021 view tests assert parity on the
    topic-rule scenarios.
    """

    classes = load_class_map()["classes"]
    bundle = load_filing_type_rule_bundle()
    best: tuple[int, str] | None = None
    if raw_category:
        for segment in split_category_segments(raw_category):
            for name, spec in classes.items():
                if any(segment.startswith(str(p)) for p in spec["prefixes"]):
                    key = (int(spec["priority"]), name)
                    if best is None or (key[0], key[1]) > best:
                        best = key
    if title:
        for topic_rule in bundle.topic_rules:
            topic_hit = (
                _ordered_keywords_in_text(topic_rule.keywords, title)
                if topic_rule.match == "all"
                else any(keyword in title for keyword in topic_rule.keywords)
            )
            if topic_hit:
                spec = classes[topic_rule.class_name]
                key = (int(spec["priority"]), topic_rule.class_name)
                if best is None or (key[0], key[1]) > best:
                    best = key
    if best is not None:
        return best[1]
    if title and (raw_category is None or not raw_category.strip()):
        for rule in bundle.rules:
            if rule.match == "all":
                if _ordered_keywords_in_text(rule.keywords, title):
                    return rule.filing_type
            elif any(keyword in title for keyword in rule.keywords):
                return rule.filing_type
    return "other"


def _rule_from_payload(payload: Mapping[str, Any]) -> FilingTypeRule:
    filing_type = str(payload["filing_type"])
    keywords = tuple(str(item) for item in payload["keywords"])
    if not keywords:
        raise CninfoMappingError(f"filing_type rule has no keywords: {filing_type}")
    match = payload.get("match", "any")
    if match not in ("any", "all"):
        raise CninfoMappingError(f"unsupported filing_type rule match: {match!r}")
    return FilingTypeRule(
        filing_type=filing_type,
        keywords=keywords,
        match=match,
    )


def _rule_matches(*, rule: FilingTypeRule, haystacks: tuple[str, str]) -> bool:
    if rule.match == "all":
        return any(
            _ordered_keywords_in_text(rule.keywords, haystack)
            for haystack in haystacks
        )
    return any(keyword in haystack for keyword in rule.keywords for haystack in haystacks)


def _ordered_keywords_in_text(keywords: tuple[str, ...], text: str) -> bool:
    """Mirror the SQL ``keyword1%keyword2`` pattern used by the rule loader."""

    offset = 0
    for keyword in keywords:
        position = text.find(keyword, offset)
        if position < 0:
            return False
        offset = position + len(keyword)
    return True


def _required_str(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if value is None or value == "":
        raise CninfoMappingError(f"CNINFO record missing required field {field}")
    return str(value)


def _optional_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _optional_int_or_str(value: object) -> int | str | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    return str(value)


def _parse_cninfo_date(value: str) -> date:
    normalized = value.strip().replace("/", "-")
    date_part = normalized.split(" ", 1)[0]
    if len(date_part) == 8 and date_part.isdigit():
        date_part = f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]}"
    return date.fromisoformat(date_part)


def _parse_cninfo_datetime(value: object) -> datetime | None:
    text = _optional_str(value)
    if text is None:
        return None
    normalized = text.replace("/", "-")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(SHANGHAI_TZ)
