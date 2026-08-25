"""CninfoSource window chunking and category-name fallback tests."""

from __future__ import annotations

from datetime import date
import unittest

import httpx

from disclosure_anchor.adapters.sources.cninfo.client import CninfoClient
from disclosure_anchor.adapters.sources.cninfo.source import (
    CninfoSource,
    CninfoWebIndexSource,
    _window_chunks,
)
from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    DisclosureWindow,
    SourceSecurity,
)
from disclosure_anchor.domain.errors import SourceRequestError


ACCESS_TOKEN = "unit-access-token"


def _ref(security_code: str) -> AnnouncementRef:
    return AnnouncementRef(
        provider="cninfo",
        provider_document_id=f"doc-{security_code}",
        title="test",
        download_url="https://static.cninfo.example/test.pdf",
        raw_category="test",
        announcement_date=date(2026, 8, 23),
        security_code=security_code,
        security_name=None,
        file_size=None,
        index_updated_at=None,
    )


def _record(textid: str, title: str, category: str) -> dict[str, object]:
    return {
        "TEXTID": textid,
        "F002V": title,
        "F003V": f"http://static.cninfo.com.cn/{textid}.PDF",
        "F006V": category,
        "F001D": "2026-05-01",
        "SECCODE": "600519",
        "SECNAME": "贵州茅台",
        "F005N": 186,
        "RECTIME": "2026-05-01 10:00:00",
    }


class WindowChunkTests(unittest.TestCase):
    def test_window_shorter_than_chunk_is_single_call(self) -> None:
        chunks = list(_window_chunks(date(2026, 6, 29), date(2026, 7, 6), 30))
        self.assertEqual(chunks, [(date(2026, 6, 29), date(2026, 7, 6))])

    def test_long_window_splits_into_inclusive_disjoint_chunks(self) -> None:
        chunks = list(_window_chunks(date(2026, 3, 28), date(2026, 7, 6), 30))
        self.assertEqual(
            chunks,
            [
                (date(2026, 3, 28), date(2026, 4, 26)),
                (date(2026, 4, 27), date(2026, 5, 26)),
                (date(2026, 5, 27), date(2026, 6, 25)),
                (date(2026, 6, 26), date(2026, 7, 6)),
            ],
        )


class CninfoSourceTests(unittest.TestCase):
    def _source(self, handler) -> CninfoSource:
        client = CninfoClient(
            access_key=None,
            access_secret=None,
            access_token=ACCESS_TOKEN,
            transport=httpx.MockTransport(handler),
            sleep=lambda _: None,
        )
        return CninfoSource(client)

    def test_long_window_is_chunked_and_results_are_merged(self) -> None:
        index_windows: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/p_info3005"):
                return httpx.Response(
                    200,
                    json={
                        "resultcode": 200,
                        "records": [{"SORTCODE": "010301", "SORTNAME": "年度报告"}],
                    },
                )
            if path.endswith("/p_info3015"):
                params = dict(request.url.params)
                index_windows.append((params["sdate"], params["edate"]))
                textid = f"tid-{len(index_windows)}"
                return httpx.Response(
                    200,
                    json={
                        "resultcode": 200,
                        "total": 2,
                        "count": 2,
                        "records": [
                            _record(textid, "贵州茅台：2025年年度报告", "010301"),
                            # Duplicate id across chunks must be absorbed once.
                            _record("tid-dup", "贵州茅台：临时公告", "019901"),
                        ],
                    },
                )
            raise AssertionError(f"unexpected path {path}")

        source = self._source(handler)
        refs = source.search_announcements(
            SourceSecurity(security_code="600519", exchange="SSE", security_name=None),
            DisclosureWindow(date(2026, 3, 28), date(2026, 7, 6)),
        )

        self.assertEqual(len(index_windows), 4)
        self.assertEqual(index_windows[0], ("2026-03-28", "2026-04-26"))
        self.assertEqual(index_windows[-1], ("2026-06-26", "2026-07-06"))
        textids = [ref.provider_document_id for ref in refs]
        self.assertEqual(len([t for t in textids if t == "tid-dup"]), 1)
        annual = [ref for ref in refs if ref.provider_document_id == "tid-1"][0]
        self.assertEqual(annual.filing_type, "annual_report")
        self.assertEqual(annual.report_period, "2025A")

    def test_category_names_fall_back_to_snapshot_when_3005_unreachable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/p_info3005"):
                # Persistent gateway block page: HTTP 403 with an HTML body.
                return httpx.Response(403, text="<!DOCTYPE html><html>blocked</html>")
            if path.endswith("/p_info3015"):
                return httpx.Response(
                    200,
                    json={
                        "resultcode": 200,
                        "total": 1,
                        "count": 1,
                        "records": [
                            _record("tid-a", "贵州茅台：2025年年度报告", "010301")
                        ],
                    },
                )
            raise AssertionError(f"unexpected path {path}")

        source = self._source(handler)
        refs = source.search_announcements(
            SourceSecurity(security_code="600519", exchange="SSE", security_name=None),
            DisclosureWindow(date(2026, 6, 29), date(2026, 7, 6)),
        )

        # 010301 resolves via the shipped fallback snapshot, not live p_info3005.
        self.assertEqual(refs[0].filing_type, "annual_report")
        self.assertEqual(refs[0].report_period, "2025A")

    def test_category_quota_stops_before_index_request(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("/p_info3005"):
                return httpx.Response(200, json={"resultcode": 407, "records": []})
            raise AssertionError("index endpoint must not be called after quota")

        source = self._source(handler)
        with self.assertRaises(SourceRequestError) as caught:
            source.search_announcements(
                SourceSecurity(
                    security_code="600519", exchange="SSE", security_name=None
                ),
                DisclosureWindow(date(2026, 6, 29), date(2026, 7, 6)),
            )

        self.assertEqual(caught.exception.error_code, "quota_exhausted")
        self.assertEqual(len(calls), 1)

    def test_report_period_uses_topic_aware_priority_class(self) -> None:
        """Generic/multi-code rows must not persist a contradictory period."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/p_info3005"):
                return httpx.Response(200, json={"resultcode": 200, "records": []})
            if request.url.path.endswith("/p_info3015"):
                return httpx.Response(
                    200,
                    json={
                        "resultcode": 200,
                        "total": 2,
                        "count": 2,
                        "records": [
                            _record(
                                "tid-solvency",
                                "中国人寿偿付能力季度报告摘要（2026年第一季度）",
                                "01010501||010113||012399",
                            ),
                            _record(
                                "tid-multicode",
                                "某公司2024年年度报告",
                                "012111||010301",
                            ),
                        ],
                    },
                )
            raise AssertionError(f"unexpected path {request.url.path}")

        refs = self._source(handler).search_announcements(
            SourceSecurity(security_code="600519", exchange="SSE", security_name=None),
            DisclosureWindow(date(2026, 6, 29), date(2026, 7, 6)),
        )
        by_id = {ref.provider_document_id: ref for ref in refs}

        self.assertIsNone(by_id["tid-solvency"].report_period)
        self.assertEqual(by_id["tid-multicode"].report_period, "2024A")

    def test_intermittent_non_json_index_response_is_retried(self) -> None:
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/p_info3005"):
                return httpx.Response(200, json={"resultcode": 200, "records": []})
            if path.endswith("/p_info3015"):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    return httpx.Response(
                        403, text="<!DOCTYPE html><html>flap</html>"
                    )
                return httpx.Response(
                    200,
                    json={
                        "resultcode": 200,
                        "total": 1,
                        "count": 1,
                        "records": [
                            _record("tid-b", "贵州茅台：临时公告", "019901")
                        ],
                    },
                )
            raise AssertionError(f"unexpected path {path}")

        source = self._source(handler)
        refs = source.search_announcements(
            SourceSecurity(security_code="600519", exchange="SSE", security_name=None),
            DisclosureWindow(date(2026, 6, 29), date(2026, 7, 6)),
        )

        self.assertEqual(attempts["count"], 2)
        self.assertEqual(len(refs), 1)

    def test_success_envelope_with_non_array_records_fails_closed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/p_info3005"):
                return httpx.Response(200, json={"resultcode": 200, "records": []})
            if request.url.path.endswith("/p_info3015"):
                return httpx.Response(
                    200,
                    json={
                        "resultcode": 200,
                        "total": 1,
                        "count": 1,
                        "records": {"unexpected": []},
                    },
                )
            raise AssertionError(f"unexpected path {request.url.path}")

        with self.assertRaises(SourceRequestError) as raised:
            self._source(handler).search_announcements(
                SourceSecurity(
                    security_code="600519", exchange="SSE", security_name=None
                ),
                DisclosureWindow(date(2026, 6, 29), date(2026, 7, 6)),
            )

        self.assertEqual(raised.exception.error_code, "invalid_response_shape")
        self.assertTrue(raised.exception.retryable)

    def test_non_object_record_and_partial_count_fail_closed(self) -> None:
        payloads = (
            {
                "resultcode": 200,
                "total": 1,
                "count": 1,
                "records": ["not-an-object"],
            },
            {
                "resultcode": 200,
                "total": 2,
                "count": 1,
                "records": [_record("tid-partial", "临时公告", "019901")],
            },
        )
        for payload in payloads:
            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/p_info3005"):
                    return httpx.Response(
                        200, json={"resultcode": 200, "records": []}
                    )
                if request.url.path.endswith("/p_info3015"):
                    return httpx.Response(200, json=payload)
                raise AssertionError(f"unexpected path {request.url.path}")

            with self.subTest(payload=payload), self.assertRaises(SourceRequestError):
                self._source(handler).search_announcements(
                    SourceSecurity(
                        security_code="600519", exchange="SSE", security_name=None
                    ),
                    DisclosureWindow(date(2026, 6, 29), date(2026, 7, 6)),
                )


if __name__ == "__main__":
    unittest.main()


class CninfoWebIndexSourceTests(unittest.TestCase):
    def test_sse_index_and_download_route_to_web_profile_to_api(self) -> None:
        calls: list[str] = []

        class _Web:
            def search_announcements(self, security, window):
                calls.append("web.search")
                return []

            def download_pdf(self, ref):
                calls.append("web.download")
                return b"pdf"

            def close(self):
                calls.append("web.close")

        class _Api:
            def __init__(self, fail: bool) -> None:
                self.fail = fail

            def profile_for_security(self, security_code):
                calls.append("api.profile")
                if self.fail:
                    raise SourceRequestError(
                        "quota", error_code="quota_exhausted", retryable=True
                    )
                return "profile"

            def close(self):
                calls.append("api.close")

        hybrid = CninfoWebIndexSource(web=_Web(), api_profile_source=_Api(False))
        hybrid.search_announcements(
            SourceSecurity("600941", "SSE"),
            DisclosureWindow(date(2026, 8, 1), date(2026, 8, 23)),
        )
        self.assertEqual(hybrid.download_pdf(_ref("600941")), b"pdf")
        self.assertEqual(hybrid.profile_for_security("600941"), "profile")
        hybrid.close()
        self.assertEqual(
            calls,
            ["web.search", "web.download", "api.profile", "web.close", "api.close"],
        )

    def test_bse_index_and_download_route_to_credentialed_api(self) -> None:
        calls: list[str] = []

        class _Web:
            def search_announcements(self, security, window):
                calls.append("web.search")
                return []

            def download_pdf(self, ref):
                calls.append("web.download")
                return b"web"

            def close(self):
                calls.append("web.close")

        class _Api:
            def search_announcements(self, security, window):
                calls.append(f"api.search:{security.security_code}")
                return []

            def download_pdf(self, ref):
                calls.append(f"api.download:{ref.security_code}")
                return b"api"

            def profile_for_security(self, security_code):
                calls.append("api.profile")
                return "profile"

            def close(self):
                calls.append("api.close")

        hybrid = CninfoWebIndexSource(web=_Web(), api_profile_source=_Api())
        window = DisclosureWindow(date(2026, 8, 1), date(2026, 8, 23))
        for code in ("920001", "430001", "830001"):
            hybrid.search_announcements(SourceSecurity(code, "BSE"), window)
            self.assertEqual(hybrid.download_pdf(_ref(code)), b"api")

        self.assertEqual(
            calls,
            [
                "api.search:920001",
                "api.download:920001",
                "api.search:430001",
                "api.download:430001",
                "api.search:830001",
                "api.download:830001",
            ],
        )

    def test_bse_index_and_download_fail_closed_without_api(self) -> None:
        calls: list[str] = []

        class _Web:
            def search_announcements(self, security, window):
                calls.append("web.search")
                return []

            def download_pdf(self, ref):
                calls.append("web.download")
                return b"web"

            def close(self):
                pass

        hybrid = CninfoWebIndexSource(web=_Web(), api_profile_source=None)
        with self.assertRaises(SourceRequestError) as search_error:
            hybrid.search_announcements(
                SourceSecurity("920001", "BSE"),
                DisclosureWindow(date(2026, 8, 1), date(2026, 8, 23)),
            )
        with self.assertRaises(SourceRequestError) as download_error:
            hybrid.download_pdf(_ref("920001"))

        for error in (search_error.exception, download_error.exception):
            self.assertEqual(error.error_code, "bse_api_required")
            self.assertFalse(error.retryable)
        self.assertEqual(calls, [])

    def test_search_fails_closed_on_code_exchange_mismatch(self) -> None:
        class _Web:
            def close(self):
                pass

        hybrid = CninfoWebIndexSource(web=_Web(), api_profile_source=None)
        with self.assertRaises(SourceRequestError) as caught:
            hybrid.search_announcements(
                SourceSecurity("920001", "SSE"),
                DisclosureWindow(date(2026, 8, 1), date(2026, 8, 23)),
            )

        self.assertEqual(caught.exception.error_code, "invalid_security_identity")
        self.assertFalse(caught.exception.retryable)

    def test_profile_degrades_to_none_on_api_refusal_or_absence(self) -> None:
        class _Web:
            def close(self):
                pass

        class _RefusingApi:
            def profile_for_security(self, security_code):
                raise SourceRequestError(
                    "quota", error_code="quota_exhausted", retryable=True
                )

            def close(self):
                pass

        with_api = CninfoWebIndexSource(
            web=_Web(), api_profile_source=_RefusingApi()
        )
        self.assertIsNone(with_api.profile_for_security("600941"))
        without_api = CninfoWebIndexSource(web=_Web(), api_profile_source=None)
        self.assertIsNone(without_api.profile_for_security("600941"))
