"""CNINFO DisclosureSourcePort adapter."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date, timedelta
import json
from importlib import resources

from disclosure_anchor.adapters.sources.cninfo.client import CninfoClient
from disclosure_anchor.adapters.sources.cninfo.mapper import (
    CninfoCompanyProfile,
    derive_primary_class,
    derive_report_period,
    split_category_segments,
    load_filing_type_rule_bundle,
    map_filing_type,
    map_p_info3015_record,
    map_p_stock2100_record,
)
from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    DisclosureWindow,
    SourceSecurity,
)
from disclosure_anchor.domain.errors import SourceRequestError


# Observed 2026-07-06: the gateway intermittently rejects large-span windows
# with 403 HTML pages; the official doc also recommends small windows. Chunking
# keeps each request well inside safe territory.
INDEX_WINDOW_CHUNK_DAYS = 30


class CninfoSource:
    """CNINFO source adapter backed by CninfoClient."""

    def __init__(self, client: CninfoClient) -> None:
        self._client = client
        self._rule_bundle = load_filing_type_rule_bundle()
        self._category_names: dict[str, str] | None = None

    def close(self) -> None:
        self._client.close()

    def search_announcements(
        self,
        security: SourceSecurity,
        window: DisclosureWindow,
    ) -> list[AnnouncementRef]:
        names = self._category_names_cached()
        refs: list[AnnouncementRef] = []
        seen_provider_document_ids: set[str] = set()
        for chunk_start, chunk_end in _window_chunks(
            window.start, window.end, INDEX_WINDOW_CHUNK_DAYS
        ):
            response = self._client.get_json(
                provider_interface="cninfo:p_info3015",
                path="/api/info/p_info3015",
                params={
                    "scode": security.security_code,
                    "sdate": chunk_start.isoformat(),
                    "edate": chunk_end.isoformat(),
                },
            )
            records = response.payload.get("records", [])
            if not isinstance(records, list):
                # A resultcode=200 envelope with a malformed chunk is not an
                # empty result. Treating it as success advances the checkpoint
                # across a permanent announcement hole.
                raise SourceRequestError(
                    "CNINFO p_info3015 records is not an array",
                    error_code="invalid_response_shape",
                    retryable=True,
                )
            if any(not isinstance(record, dict) for record in records):
                raise SourceRequestError(
                    "CNINFO p_info3015 records contains a non-object row",
                    error_code="invalid_response_shape",
                    retryable=True,
                )
            count = response.payload.get("count")
            total = response.payload.get("total")
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or not isinstance(total, int)
                or isinstance(total, bool)
                or count != len(records)
                or total != len(records)
            ):
                # This adapter never requests page/pagesize and each request is
                # one stock over <=30 days. Therefore count/total must describe
                # the complete returned window; a mismatch is partial data,
                # not a successful empty/tiny chunk.
                raise SourceRequestError(
                    "CNINFO p_info3015 count/total does not match records",
                    error_code="incomplete_response",
                    retryable=True,
                )
            for record in records:
                mapped = map_p_info3015_record(record)
                if mapped.provider_document_id in seen_provider_document_ids:
                    continue
                seen_provider_document_ids.add(mapped.provider_document_id)
                filing_type = map_filing_type(
                    mapped.raw_category,
                    category_names_by_code=names,
                    rule_bundle=self._rule_bundle,
                )
                if filing_type == "other":
                    # cninfo sometimes codes a real event as bare 012399
                    # 其它事项 (业绩说明会/持股计划 observed); the title still
                    # carries the class — same closed keyword bundle the web
                    # channel runs (title passed as the haystack segment).
                    filing_type = map_filing_type(
                        mapped.title,
                        category_names_by_code={},
                        rule_bundle=self._rule_bundle,
                    )
                segments = split_category_segments(mapped.raw_category)
                category_names = [
                    names[segment] for segment in segments if segment in names
                ]
                refs.append(
                    replace(
                        mapped,
                        filing_type=filing_type,
                        report_period=derive_report_period(
                            mapped.title,
                            filing_type=derive_primary_class(
                                mapped.raw_category, mapped.title
                            ),
                        ),
                        category_names=category_names or None,
                    )
                )
        return refs

    def _category_names_cached(self) -> dict[str, str]:
        """Fetch live category names; fall back to the shipped snapshot.

        filing_type classification depends on code→name resolution, so a
        transient p_info3005 outage must degrade to the snapshot instead of
        classifying every announcement as `other`.
        """

        if self._category_names is None:
            try:
                self._category_names = self.category_names_by_code()
            except SourceRequestError as exc:
                if exc.error_code == "quota_exhausted":
                    # Quota is provider-wide, not a category-only outage. Do
                    # not hide it behind the snapshot and spend another call
                    # on p_info3015 before the worker can trip its breaker.
                    raise
                self._category_names = {}
            if not self._category_names:
                self._category_names = _fallback_category_names()
        return self._category_names

    def profile_for_security(self, security_code: str) -> CninfoCompanyProfile | None:
        response = self._client.get_json(
            provider_interface="cninfo:p_stock2100",
            path="/api/stock/p_stock2100",
            params={"scode": security_code},
        )
        records = response.payload.get("records", [])
        if not isinstance(records, list) or not records:
            return None
        first = records[0]
        if not isinstance(first, dict):
            return None
        return map_p_stock2100_record(first)

    def category_names_by_code(self) -> dict[str, str]:
        response = self._client.get_json(
            provider_interface="cninfo:p_info3005",
            path="/api/info/p_info3005",
            params={},
        )
        records = response.payload.get("records", [])
        if not isinstance(records, list):
            return {}
        names: dict[str, str] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            code = record.get("SORTCODE")
            name = record.get("SORTNAME")
            if isinstance(code, str) and isinstance(name, str):
                names[code] = name
        return names

    def download_pdf(self, ref: AnnouncementRef) -> bytes:
        payload, _ = self._client.download_bytes(
            provider_interface="cninfo:download_pdf",
            url=ref.download_url,
            params={},
        )
        return payload


def _window_chunks(
    start: date, end: date, chunk_days: int
) -> Iterator[tuple[date, date]]:
    """Split [start, end] (inclusive) into consecutive chunks of chunk_days."""

    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def _fallback_category_names() -> dict[str, str]:
    raw = (
        resources.files("disclosure_anchor.adapters.sources.cninfo")
        .joinpath("category_names_fallback.json")
        .read_text(encoding="utf-8")
    )
    payload = json.loads(raw)
    names = payload.get("names", {})
    return {
        str(code): str(name)
        for code, name in names.items()
        if isinstance(code, str) and isinstance(name, str)
    }
