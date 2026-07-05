"""CNINFO DisclosureSourcePort adapter."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from disclosure_anchor.adapters.sources.cninfo.client import CninfoClient
from disclosure_anchor.adapters.sources.cninfo.mapper import (
    CninfoCompanyProfile,
    category_prefix_matches,
    derive_report_period,
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


class CninfoSource:
    """CNINFO source adapter backed by CninfoClient."""

    def __init__(self, client: CninfoClient) -> None:
        self._client = client
        self._rule_bundle = load_filing_type_rule_bundle()
        self._category_names: dict[str, str] | None = None

    def search_announcements(
        self,
        security: SourceSecurity,
        window: DisclosureWindow,
        categories: Sequence[str] | None = None,
    ) -> list[AnnouncementRef]:
        response = self._client.get_json(
            provider_interface="cninfo:p_info3015",
            path="/api/info/p_info3015",
            params={
                "scode": security.security_code,
                "sdate": window.start.isoformat(),
                "edate": window.end.isoformat(),
            },
        )
        records = response.payload.get("records", [])
        if not isinstance(records, list):
            return []
        names = self._category_names_cached()
        refs: list[AnnouncementRef] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            mapped = map_p_info3015_record(record)
            filing_type = map_filing_type(
                mapped.raw_category,
                category_names_by_code=names,
                rule_bundle=self._rule_bundle,
            )
            refs.append(
                replace(
                    mapped,
                    filing_type=filing_type,
                    report_period=derive_report_period(
                        mapped.title, filing_type=filing_type
                    ),
                )
            )
        return [
            ref
            for ref in refs
            if category_prefix_matches(ref.raw_category, categories)
        ]

    def _category_names_cached(self) -> dict[str, str]:
        if self._category_names is None:
            self._category_names = self.category_names_by_code()
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
