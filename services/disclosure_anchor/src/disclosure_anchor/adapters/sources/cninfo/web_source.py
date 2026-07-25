"""Credential-free CNINFO fallback source via the public website endpoint.

Insurance channel for WebAPI quota/credential outages. Verified live
2026-07-06 against the same announcement (TEXTID 1225406051):

- ``announcementId`` equals the WebAPI ``TEXTID``;
- ``adjunctSize`` and ``F005N`` are provider signature hints, but their
  numeric scale is not unit-stable in the production corpus.

so the dedup key (provider, provider_document_id, raw_file_hash) and the B7
file-signature comparison preserve the provider values verbatim. Scheduling
cost uses the measured archive byte_count instead; the same announcement
synced through either channel is still absorbed idempotently by raw hash.

Channel limitations (documented, accepted for a fallback):
- no company profile (no legal name / USCC); SubjectResolver falls back to the
  existing security or a placeholder company name;
- no F006V category codes: classification falls back to title rules and
  filing_type is classified from the announcement title through the same rule
  bundle (titles carry 年度报告/季度报告/说明会/问询…).
"""

from __future__ import annotations

from collections.abc import Callable
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
    derive_primary_class,
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


def _website_column(exchange: str) -> str:
    """Map only website channels whose behavior is verified.

    CNINFO's public fallback has separate Shanghai/Shenzhen columns.  Routing
    every non-SSE exchange to Shenzhen silently returned the wrong universe
    for BSE codes, so BSE now fails closed and stays on the WebAPI channel.
    """

    normalized = exchange.strip().upper()
    if normalized == "SSE":
        return "sse"
    if normalized == "SZSE":
        return "szse"
    raise CninfoWebSourceError(
        f"CNINFO website fallback does not support exchange={normalized!r}",
        error_code="unsupported_exchange",
        retryable=False,
    )


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
    ) -> list[AnnouncementRef]:
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
                # Shape drift must fail loudly: a silently dropped record
                # lands behind the advanced checkpoint and becomes a
                # permanent, trace-free index hole. The API channel already
                # fail-louds on shape (source.py); this channel must match
                # (round23).
                if not isinstance(record, dict):
                    raise CninfoWebSourceError(
                        "CNINFO web index record is not an object; "
                        "response shape drifted",
                        error_code="index_record_shape",
                        retryable=False,
                    )
                ref = self._ref_from_record(record)
                if ref.provider_document_id in seen:
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
            # Transport failures (connect/read timeouts — routine on the
            # public site) must surface as SourceRequestError like the API
            # channel does (client.py), or they escape the download retry
            # budget entirely (round23 review S1).
            try:
                response = self._client.get(ref.download_url)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise CninfoWebSourceError(
                        f"CNINFO web download transport failure: {exc}",
                        error_code="transport_error",
                        retryable=True,
                    ) from exc
                self._sleep(self._next_delay(attempt))
                attempt += 1
                continue
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
        column = _website_column(exchange)
        data = {
            "stock": self._stock_param(security_code),
            "tabName": "fulltext",
            "pageSize": str(PAGE_SIZE),
            "pageNum": str(page),
            "column": column,
            "category": "",
            "seDate": f"{window.start.isoformat()}~{window.end.isoformat()}",
            "isHLtitle": "false",
        }
        attempt = 0
        while True:
            self._bucket.take()
            try:
                response = self._client.post(QUERY_URL, data=data)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise CninfoWebSourceError(
                        f"CNINFO web index transport failure: {exc}",
                        error_code="transport_error",
                        retryable=True,
                    ) from exc
                self._sleep(self._next_delay(attempt))
                attempt += 1
                continue
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
                try:
                    response = self._client.get(STOCK_LIST_URL)
                except httpx.TransportError as exc:
                    if attempt >= self._max_retries:
                        raise CninfoWebSourceError(
                            f"CNINFO stock list transport failure: {exc}",
                            error_code="transport_error",
                            retryable=True,
                        ) from exc
                    self._sleep(self._next_delay(attempt))
                    attempt += 1
                    continue
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

    def _ref_from_record(self, record: dict[str, Any]) -> AnnouncementRef:
        announcement_id = record.get("announcementId")
        title = record.get("announcementTitle")
        adjunct_url = record.get("adjunctUrl")
        sec_code = record.get("secCode")
        time_ms = record.get("announcementTime")
        missing = [
            name
            for name, ok in (
                ("announcementId", bool(announcement_id)),
                ("announcementTitle", isinstance(title, str) and bool(title)),
                ("adjunctUrl", isinstance(adjunct_url, str) and bool(adjunct_url)),
                ("secCode", isinstance(sec_code, str)),
                ("announcementTime", isinstance(time_ms, (int, float))),
            )
            if not ok
        ]
        if missing:
            # Fail loudly instead of silently dropping the record: the sync
            # would otherwise mark the access 'ok', advance the checkpoint,
            # and this announcement would never be seen again (round23).
            raise CninfoWebSourceError(
                "CNINFO web index record missing/invalid fields "
                f"{missing} (announcementId={record.get('announcementId')!r}, "
                f"secCode={record.get('secCode')!r})",
                error_code="index_record_shape",
                retryable=False,
            )
        assert isinstance(title, str)
        assert isinstance(adjunct_url, str)
        assert isinstance(sec_code, str)
        assert isinstance(time_ms, (int, float))
        announcement_date = datetime.fromtimestamp(
            time_ms / 1000.0, tz=SHANGHAI_TZ
        ).date()
        filing_type = map_filing_type(
            title, category_names_by_code={}, rule_bundle=self._rule_bundle
        )
        primary_class = derive_primary_class(None, title)
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
            report_period=derive_report_period(title, filing_type=primary_class),
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
