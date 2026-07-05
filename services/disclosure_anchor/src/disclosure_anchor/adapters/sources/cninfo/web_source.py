"""Credential-free CNINFO fallback source via the public website endpoint.

Insurance channel for WebAPI quota/credential outages. Verified live
2026-07-06 against the same announcement (TEXTID 1225406051):

- ``announcementId`` equals the WebAPI ``TEXTID``;
- ``adjunctSize`` is KB, same as ``F005N``;

so the dedup key (provider, provider_document_id, raw_file_hash) and the B7
file-signature comparison are shared across channels — the same announcement
synced through either channel is absorbed idempotently.

Channel limitations (documented, accepted for a fallback):
- no company profile (no legal name / USCC); SubjectResolver falls back to the
  existing security or a placeholder company name;
- no F006V category codes: ``categories`` filtering is ignored and
  filing_type is classified from the announcement title through the same rule
  bundle (titles carry 年度报告/季度报告/说明会/问询…).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone, timedelta
import random
import time
from typing import Any

import httpx

from disclosure_anchor.adapters.sources.cninfo.client import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    BACKOFF_FACTOR,
    TokenBucket,
)
from disclosure_anchor.adapters.sources.cninfo.mapper import (
    CninfoCompanyProfile,
    derive_report_period,
    load_filing_type_rule_bundle,
    map_filing_type,
)
from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    DisclosureWindow,
    SourceSecurity,
)
from disclosure_anchor.domain.errors import SourceRequestError


QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
# The query endpoint returns nothing unless stock is the "code,orgId" pair;
# this public static list maps code → orgId for all A-share securities.
STOCK_LIST_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
STATIC_BASE = "http://static.cninfo.com.cn"
PAGE_SIZE = 30
SHANGHAI_TZ = timezone(timedelta(hours=8))


class CninfoWebSourceError(SourceRequestError):
    """Raised when the public endpoint fails under retry policy."""


class CninfoWebSource:
    """DisclosureSourcePort implementation over the public website JSON."""

    def __init__(
        self,
        *,
        max_qps: float = 1.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._bucket = TokenBucket(max_qps=max_qps, sleep=sleep)
        self._sleep = sleep or time.sleep
        self._jitter = jitter or (lambda upper: random.uniform(0.0, upper))
        self._client = httpx.Client(
            transport=transport,
            timeout=30.0,
            trust_env=False,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "http://www.cninfo.com.cn/new/commonUrl",
            },
        )
        self._rule_bundle = load_filing_type_rule_bundle()
        self._org_ids: dict[str, str] | None = None

    def close(self) -> None:
        self._client.close()

    def search_announcements(
        self,
        security: SourceSecurity,
        window: DisclosureWindow,
        categories: Sequence[str] | None = None,
    ) -> list[AnnouncementRef]:
        del categories  # no F006V on this channel; filtering happens downstream
        refs: list[AnnouncementRef] = []
        seen: set[str] = set()
        page = 1
        while True:
            payload = self._query_page(
                security_code=security.security_code,
                exchange=security.exchange,
                window=window,
                page=page,
            )
            announcements = payload.get("announcements")
            if not isinstance(announcements, list) or not announcements:
                break
            for record in announcements:
                if not isinstance(record, dict):
                    continue
                ref = self._ref_from_record(record)
                if ref is None or ref.provider_document_id in seen:
                    continue
                seen.add(ref.provider_document_id)
                refs.append(ref)
            if not payload.get("hasMore") and len(announcements) < PAGE_SIZE:
                break
            page += 1
        return refs

    def profile_for_security(self, security_code: str) -> CninfoCompanyProfile | None:
        """The public channel has no company-profile endpoint."""

        del security_code
        return None

    def download_pdf(self, ref: AnnouncementRef) -> bytes:
        attempt = 0
        while True:
            self._bucket.take()
            response = self._client.get(ref.download_url)
            if response.status_code < 400:
                return response.content
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt >= self._max_retries:
                raise CninfoWebSourceError(
                    "CNINFO web download failed",
                    error_code=f"http_{response.status_code}",
                    retryable=retryable,
                )
            self._sleep(self._next_delay(attempt))
            attempt += 1

    def _query_page(
        self,
        *,
        security_code: str,
        exchange: str,
        window: DisclosureWindow,
        page: int,
    ) -> dict[str, Any]:
        data = {
            "stock": self._stock_param(security_code),
            "tabName": "fulltext",
            "pageSize": str(PAGE_SIZE),
            "pageNum": str(page),
            "column": "sse" if exchange == "SSE" else "szse",
            "category": "",
            "seDate": f"{window.start.isoformat()}~{window.end.isoformat()}",
            "isHLtitle": "false",
        }
        attempt = 0
        while True:
            self._bucket.take()
            response = self._client.post(QUERY_URL, data=data)
            payload: dict[str, Any] | None = None
            if response.status_code < 400:
                try:
                    parsed = response.json()
                    if isinstance(parsed, dict):
                        payload = parsed
                except ValueError:
                    payload = None
            if payload is not None:
                return payload
            retryable = response.status_code < 400 or (
                response.status_code == 429 or response.status_code >= 500
            )
            if not retryable or attempt >= self._max_retries:
                raise CninfoWebSourceError(
                    f"CNINFO web index failed (http_status={response.status_code})",
                    error_code=(
                        "non_json_response"
                        if response.status_code < 400
                        else f"http_{response.status_code}"
                    ),
                    retryable=retryable,
                )
            self._sleep(self._next_delay(attempt))
            attempt += 1

    def _stock_param(self, security_code: str) -> str:
        org_id = self._org_ids_cached().get(security_code)
        return f"{security_code},{org_id}" if org_id else security_code

    def _org_ids_cached(self) -> dict[str, str]:
        if self._org_ids is None:
            attempt = 0
            while True:
                self._bucket.take()
                response = self._client.get(STOCK_LIST_URL)
                if response.status_code < 400:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict):
                        rows = payload.get("stockList")
                        mapping: dict[str, str] = {}
                        if isinstance(rows, list):
                            for row in rows:
                                if not isinstance(row, dict):
                                    continue
                                code = row.get("code")
                                org_id = row.get("orgId")
                                if isinstance(code, str) and isinstance(org_id, str):
                                    mapping[code] = org_id
                        self._org_ids = mapping
                        break
                if attempt >= self._max_retries:
                    raise CninfoWebSourceError(
                        "CNINFO stock list fetch failed "
                        f"(http_status={response.status_code})",
                        error_code="stock_list_unavailable",
                        retryable=True,
                    )
                self._sleep(self._next_delay(attempt))
                attempt += 1
        return self._org_ids

    def _ref_from_record(self, record: dict[str, Any]) -> AnnouncementRef | None:
        announcement_id = record.get("announcementId")
        title = record.get("announcementTitle")
        adjunct_url = record.get("adjunctUrl")
        sec_code = record.get("secCode")
        time_ms = record.get("announcementTime")
        if not announcement_id or not isinstance(title, str) or not title:
            return None
        if not isinstance(adjunct_url, str) or not adjunct_url:
            return None
        if not isinstance(sec_code, str) or not isinstance(time_ms, (int, float)):
            return None
        announcement_date = datetime.fromtimestamp(
            time_ms / 1000.0, tz=SHANGHAI_TZ
        ).date()
        filing_type = map_filing_type(
            title, category_names_by_code={}, rule_bundle=self._rule_bundle
        )
        adjunct_size = record.get("adjunctSize")
        return AnnouncementRef(
            provider="cninfo",
            provider_document_id=str(announcement_id),
            title=title,
            download_url=f"{STATIC_BASE}/{adjunct_url.lstrip('/')}",
            raw_category="",
            announcement_date=announcement_date,
            security_code=sec_code,
            security_name=(
                str(record["secName"]) if record.get("secName") else None
            ),
            file_size=adjunct_size if isinstance(adjunct_size, (int, float)) else None,
            index_updated_at=None,
            filing_type=filing_type,
            report_period=derive_report_period(title, filing_type=filing_type),
            object_id=None,
            rec_id=None,
            provider_org_id=(
                str(record["orgId"]) if record.get("orgId") else None
            ),
            raw_record={str(key): value for key, value in record.items()},
        )

    def _next_delay(self, attempt: int) -> float:
        upper = min(
            BACKOFF_CAP_SECONDS,
            BACKOFF_BASE_SECONDS * (BACKOFF_FACTOR**attempt),
        )
        return self._jitter(upper)
