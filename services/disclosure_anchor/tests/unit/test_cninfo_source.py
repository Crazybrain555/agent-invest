"""CninfoSource window chunking and category-name fallback tests."""

from __future__ import annotations

from datetime import date
import unittest

import httpx

from disclosure_anchor.adapters.sources.cninfo.client import CninfoClient
from disclosure_anchor.adapters.sources.cninfo.source import (
    CninfoSource,
    _window_chunks,
)
from disclosure_anchor.application.ports.disclosure_source import (
    DisclosureWindow,
    SourceSecurity,
)


ACCESS_TOKEN = "unit-access-token"


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


if __name__ == "__main__":
    unittest.main()
