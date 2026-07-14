"""Credential-free web fallback source tests."""

from __future__ import annotations

from datetime import date
import unittest

import httpx

from disclosure_anchor.adapters.sources.cninfo.web_source import (
    CninfoWebSource,
    CninfoWebSourceError,
)
from disclosure_anchor.application.ports.disclosure_source import (
    DisclosureWindow,
    SourceSecurity,
)


def _record(ann_id: int, title: str, *, size: int = 118) -> dict[str, object]:
    return {
        "announcementId": str(ann_id),
        "announcementTitle": title,
        "adjunctUrl": f"finalpage/2026-07-03/{ann_id}.PDF",
        "adjunctSize": size,
        "secCode": "000001",
        "secName": "平安银行",
        "orgId": "gssz0000001",
        "announcementTime": 1783008000000,
    }


STOCK_LIST = {"stockList": [{"code": "000001", "orgId": "gssz0000001", "zwjc": "平安银行"}]}


def _source(handler) -> CninfoWebSource:
    def routing(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/data/szse_stock.json"):
            return httpx.Response(200, json=STOCK_LIST)
        return handler(request)

    return CninfoWebSource(
        transport=httpx.MockTransport(routing), sleep=lambda _: None
    )


class CninfoWebSourceTests(unittest.TestCase):
    def test_maps_public_record_to_shared_provider_namespace(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            self.assertIn("seDate=2026-06-29~2026-07-06", body)
            self.assertIn("column=szse", body)
            self.assertIn("stock=000001%2Cgssz0000001", body)
            return httpx.Response(
                200,
                json={
                    "announcements": [
                        _record(1225406051, "2025年半年度报告"),
                    ],
                    "hasMore": False,
                },
            )

        refs = _source(handler).search_announcements(
            SourceSecurity(security_code="000001", exchange="SZSE", security_name=None),
            DisclosureWindow(date(2026, 6, 29), date(2026, 7, 6)),
        )

        self.assertEqual(len(refs), 1)
        ref = refs[0]
        self.assertEqual(ref.provider, "cninfo")
        self.assertEqual(ref.provider_document_id, "1225406051")
        self.assertEqual(
            ref.download_url,
            "http://static.cninfo.com.cn/finalpage/2026-07-03/1225406051.PDF",
        )
        self.assertEqual(ref.announcement_date, date(2026, 7, 3))
        self.assertEqual(ref.file_size, 118)
        self.assertEqual(ref.filing_type, "semiannual_report")
        self.assertEqual(ref.report_period, "2025Q2")
        self.assertEqual(ref.provider_org_id, "gssz0000001")

    def test_malformed_record_fails_loud_instead_of_silent_drop(self) -> None:
        # A silently dropped record would land behind the advanced checkpoint
        # and become a permanent index hole (round23): shape drift must raise.
        def handler(request: httpx.Request) -> httpx.Response:
            bad = _record(1225406052, "关于回购股份的公告")
            bad["announcementTime"] = "2026-07-03"  # drifted: string, not ms
            return httpx.Response(
                200,
                json={"announcements": [bad], "hasMore": False},
            )

        with self.assertRaises(CninfoWebSourceError) as caught:
            _source(handler).search_announcements(
                SourceSecurity(
                    security_code="000001", exchange="SZSE", security_name=None
                ),
                DisclosureWindow(date(2026, 6, 29), date(2026, 7, 6)),
            )

        self.assertEqual(caught.exception.error_code, "index_record_shape")
        self.assertFalse(caught.exception.retryable)
        self.assertIn("announcementTime", str(caught.exception))

    def test_topic_class_prevents_fake_quarterly_report_period(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "announcements": [
                        _record(
                            1225406052,
                            "中国人寿偿付能力季度报告摘要（2026年第一季度）",
                        )
                    ],
                    "hasMore": False,
                },
            )

        refs = _source(handler).search_announcements(
            SourceSecurity(security_code="000001", exchange="SZSE", security_name=None),
            DisclosureWindow(date(2026, 6, 29), date(2026, 7, 6)),
        )

        self.assertEqual(len(refs), 1)
        self.assertIsNone(refs[0].report_period)

    def test_paginates_until_short_page(self) -> None:
        pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            page = int(dict(p.split("=") for p in body.split("&"))["pageNum"])
            pages.append(page)
            if page == 1:
                records = [_record(1000 + i, f"公告{i}") for i in range(30)]
                return httpx.Response(
                    200, json={"announcements": records, "hasMore": True}
                )
            return httpx.Response(
                200,
                json={
                    "announcements": [_record(2000, "尾页公告")],
                    "hasMore": False,
                },
            )

        refs = _source(handler).search_announcements(
            SourceSecurity(security_code="000001", exchange="SZSE", security_name=None),
            DisclosureWindow(date(2026, 4, 1), date(2026, 7, 6)),
        )

        self.assertEqual(pages, [1, 2])
        self.assertEqual(len(refs), 31)

    def test_non_json_flap_is_retried(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(200, text="<!DOCTYPE html>blocked")
            return httpx.Response(
                200, json={"announcements": [_record(3000, "公告")], "hasMore": False}
            )

        refs = _source(handler).search_announcements(
            SourceSecurity(security_code="000001", exchange="SZSE", security_name=None),
            DisclosureWindow(date(2026, 6, 29), date(2026, 7, 6)),
        )

        self.assertEqual(attempts["n"], 2)
        self.assertEqual(len(refs), 1)

    def test_download_404_raises_structured_source_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "static.cninfo.com.cn":
                return httpx.Response(404, text="not found")
            return httpx.Response(
                200, json={"announcements": [_record(4000, "公告")], "hasMore": False}
            )

        source = _source(handler)
        refs = source.search_announcements(
            SourceSecurity(security_code="000001", exchange="SZSE", security_name=None),
            DisclosureWindow(date(2026, 6, 29), date(2026, 7, 6)),
        )
        with self.assertRaises(CninfoWebSourceError) as ctx:
            source.download_pdf(refs[0])
        self.assertEqual(ctx.exception.error_code, "http_404")
        self.assertFalse(ctx.exception.retryable)

    def test_profile_is_unavailable_on_this_channel(self) -> None:
        source = _source(lambda request: httpx.Response(500))
        self.assertIsNone(source.profile_for_security("000001"))

    def test_bse_fails_closed_instead_of_routing_to_shenzhen(self) -> None:
        requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(500)

        with self.assertRaises(CninfoWebSourceError) as ctx:
            _source(handler).search_announcements(
                SourceSecurity(
                    security_code="920001", exchange="BSE", security_name=None
                ),
                DisclosureWindow(date(2026, 6, 29), date(2026, 7, 6)),
            )
        self.assertEqual(ctx.exception.error_code, "unsupported_exchange")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(requests, 0)


if __name__ == "__main__":
    unittest.main()
