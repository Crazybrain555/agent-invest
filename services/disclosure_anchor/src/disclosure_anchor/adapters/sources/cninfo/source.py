"""CNINFO DisclosureSourcePort adapter."""

from __future__ import annotations

from collections.abc import Sequence

from disclosure_anchor.adapters.sources.cninfo.client import CninfoClient
from disclosure_anchor.adapters.sources.cninfo.mapper import (
    CninfoCompanyProfile,
    category_prefix_matches,
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
        refs = [
            map_p_info3015_record(record)
            for record in records
            if isinstance(record, dict)
        ]
        return [
            ref
            for ref in refs
            if category_prefix_matches(ref.raw_category, categories)
        ]

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
