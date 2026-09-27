"""Credential-free web fallback source tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
import unittest
from unittest import mock

import httpx

from disclosure_anchor.adapters.sources.cninfo.web_source import (
    CninfoWebSource,
    CninfoWebSourceError,
)
from disclosure_anchor.adapters.sources.cninfo.client import TokenBucket
from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    DisclosureWindow,
    SourceSecurity,
)
from tests._pdf_download_fixture import ChunkStream, RecordingSink


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

    def test_download_streams_identity_and_restarts_after_partial_body(self) -> None:
        body = b"%PDF-1.4\nweb second attempt\n%%EOF\n"
        ref = AnnouncementRef(
            provider="cninfo",
            provider_document_id="4100",
            title="公告",
            download_url="http://static.cninfo.com.cn/finalpage/4100.PDF",
            raw_category="",
            announcement_date=date(2026, 7, 1),
            security_code="000001",
            security_name=None,
            file_size=None,
            index_updated_at=None,
        )
        for encoded in (False, True):
            with self.subTest(encoded=encoded):
                encodings: list[str | None] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    encodings.append(request.headers.get("Accept-Encoding"))
                    if encoded:
                        return httpx.Response(
                            200, headers={"Content-Encoding": "gzip"}, stream=ChunkStream(body)
                        )
                    if len(encodings) == 1:
                        return httpx.Response(
                            200, stream=ChunkStream(body[:6], b"stale", fail_at=1)
                        )
                    return httpx.Response(200, stream=ChunkStream(body[:6], body[6:]))

                source = _source(handler)
                sink = RecordingSink()
                if encoded:
                    with self.assertRaises(CninfoWebSourceError) as raised:
                        source.download_pdf_to(ref, sink)
                    self.assertEqual(
                        (raised.exception.error_code, raised.exception.retryable),
                        ("unsupported_content_encoding", True),
                    )
                    self.assertEqual((encodings, sink.attempts), (["identity"], []))
                else:
                    transfer = source.download_pdf_to(ref, sink)
                    self.assertEqual(encodings, ["identity", "identity"])
                    self.assertEqual([bytes(a) for a in sink.attempts], [body[:6], body])
                    self.assertEqual(transfer.byte_count, len(body))
                source.close()

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


class _TimedChunks(httpx.SyncByteStream):
    def __init__(
        self,
        advance: Callable[[float], None],
        chunks: tuple[tuple[float, bytes], ...],
        *,
        eof_delay: float = 0.0,
    ) -> None:
        self.advance = advance
        self.chunks = chunks
        self.eof_delay = eof_delay
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for elapsed, chunk in self.chunks:
            self.advance(elapsed)
            yield chunk
        self.advance(self.eof_delay)

    def close(self) -> None:
        self.closed = True


class WebDownloadDeadlineAcceptanceTests(unittest.TestCase):
    def _ref(self, source: CninfoWebSource) -> AnnouncementRef:
        return source.search_announcements(
            SourceSecurity(security_code="000001", exchange="SZSE", security_name=None),
            DisclosureWindow(date(2026, 6, 29), date(2026, 7, 6)),
        )[0]

    def test_slow_body_expires_without_returning_partial_bytes(self) -> None:
        now = 0.0
        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds
        stream = _TimedChunks(advance, ((900.0, b"%PDF-"), (901.0, b"partial")))
        downloads = 0
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal downloads
            if request.url.path.endswith("/data/szse_stock.json"):
                return httpx.Response(200, json=STOCK_LIST)
            if request.url.host == "static.cninfo.com.cn":
                downloads += 1
                return httpx.Response(200, stream=stream)
            return httpx.Response(200, json={"announcements": [_record(4001, "公告")], "hasMore": False})
        with mock.patch("disclosure_anchor.adapters.sources.cninfo.web_source.time.monotonic", side_effect=lambda: now):
            source = CninfoWebSource(transport=httpx.MockTransport(handler), sleep=advance)
            ref = self._ref(source)
            with self.assertRaises(CninfoWebSourceError) as raised:
                source.download_pdf(ref)
            source.close()
        self.assertEqual((raised.exception.error_code, raised.exception.retryable),
                         ("transfer_deadline_exceeded", True))
        self.assertEqual(downloads, 1)
        self.assertTrue(stream.closed)

    def test_late_eof_after_valid_prefix_expires_without_returning_bytes(self) -> None:
        now = 0.0
        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds
        # Every individual read waits under HTTPX's 30s read timeout. The
        # last wait returns EOF, with no final chunk for a per-yield check.
        chunks = ((0.0, b"%PDF-1.4\n"),) + ((29.9, b"x"),) * 60
        stream = _TimedChunks(advance, chunks, eof_delay=10.0)
        downloads = 0
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal downloads
            if request.url.path.endswith("/data/szse_stock.json"):
                return httpx.Response(200, json=STOCK_LIST)
            if request.url.host == "static.cninfo.com.cn":
                downloads += 1
                return httpx.Response(200, stream=stream)
            return httpx.Response(200, json={"announcements": [_record(4005, "公告")], "hasMore": False})
        with mock.patch("disclosure_anchor.adapters.sources.cninfo.web_source.time.monotonic", side_effect=lambda: now):
            source = CninfoWebSource(transport=httpx.MockTransport(handler), sleep=advance)
            try:
                with self.assertRaises(CninfoWebSourceError) as raised:
                    source.download_pdf(self._ref(source))
            finally:
                source.close()
        self.assertEqual((raised.exception.error_code, raised.exception.retryable),
                         ("transfer_deadline_exceeded", True))
        self.assertGreater(now, 1800.0)
        self.assertEqual(downloads, 1)
        self.assertTrue(stream.closed)

    def test_retry_backoff_and_bucket_wait_share_one_budget(self) -> None:
        for delay_kind in ("backoff", "bucket"):
            with self.subTest(delay_kind=delay_kind):
                now = 0.0
                def advance(seconds: float) -> None:
                    nonlocal now
                    now += seconds
                downloads = 0
                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal downloads
                    if request.url.path.endswith("/data/szse_stock.json"):
                        return httpx.Response(200, json=STOCK_LIST)
                    if request.url.host == "static.cninfo.com.cn":
                        downloads += 1
                        if delay_kind == "backoff" and downloads == 1:
                            advance(1799.5)
                        return httpx.Response(503 if delay_kind == "backoff" and downloads == 1 else 200,
                                              content=b"%PDF-1.4\n%%EOF\n")
                    return httpx.Response(200, json={"announcements": [_record(4002, "公告")], "hasMore": False})
                with mock.patch("disclosure_anchor.adapters.sources.cninfo.web_source.time.monotonic", side_effect=lambda: now):
                    source = CninfoWebSource(transport=httpx.MockTransport(handler), sleep=advance,
                                             jitter=lambda _: 1.0)
                    ref = self._ref(source)
                    if delay_kind == "bucket":
                        source._bucket = TokenBucket(max_qps=1 / 1801, clock=lambda: now, sleep=advance)
                        source.download_pdf(ref)
                    with self.assertRaises(CninfoWebSourceError) as raised:
                        source.download_pdf(ref)
                    source.close()
                self.assertEqual(raised.exception.error_code, "transfer_deadline_exceeded")
                self.assertTrue(raised.exception.retryable)
                self.assertEqual(downloads, 1)

    def test_normal_stream_preserves_exact_bytes(self) -> None:
        payload = b"%PDF-1.4\nidentity\n%%EOF\n"
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/data/szse_stock.json"):
                return httpx.Response(200, json=STOCK_LIST)
            if request.url.host == "static.cninfo.com.cn":
                return httpx.Response(200, stream=_TimedChunks(lambda _: None,
                                      ((0, payload[:8]), (0, payload[8:]))))
            return httpx.Response(200, json={"announcements": [_record(4003, "公告")], "hasMore": False})
        source = CninfoWebSource(transport=httpx.MockTransport(handler), sleep=lambda _: None)
        self.assertEqual(source.download_pdf(self._ref(source)), payload)
        source.close()

    def test_late_retry_clamps_request_read_timeout_to_remaining_budget(self) -> None:
        now = 0.0
        seen_timeout: list[float] = []
        attempts = 0
        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            if request.url.path.endswith("/data/szse_stock.json"):
                return httpx.Response(200, json=STOCK_LIST)
            if request.url.host == "static.cninfo.com.cn":
                attempts += 1
                if attempts == 1:
                    advance(1790.0)
                    return httpx.Response(503)
                seen_timeout.append(request.extensions["timeout"]["read"])
                return httpx.Response(200, content=b"%PDF-1.4\n%%EOF\n")
            return httpx.Response(200, json={"announcements": [_record(4004, "公告")], "hasMore": False})
        with mock.patch("disclosure_anchor.adapters.sources.cninfo.web_source.time.monotonic", side_effect=lambda: now):
            source = CninfoWebSource(transport=httpx.MockTransport(handler), sleep=advance,
                                     jitter=lambda _: 1.0)
            payload = source.download_pdf(self._ref(source))
            source.close()
        self.assertEqual(payload, b"%PDF-1.4\n%%EOF\n")
        self.assertEqual(attempts, 2)
        self.assertEqual(len(seen_timeout), 1)
        self.assertGreater(seen_timeout[0], 0)
        self.assertLessEqual(seen_timeout[0], 9.0)
