"""Independent synthetic HTTP Range checks for the V4 retained result port."""

from __future__ import annotations

import unittest

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import MinerUHttpRemoteV4
from disclosure_anchor.application.ports.remote_provider_v4 import (
    RemoteProviderProtocolErrorV4,
    RemoteProviderUnavailableV4,
    RemoteResultRangeIgnoredV4,
    RemoteResultRangeUnsatisfiableV4,
)
from tests.unit import test_mineru_http_remote_v4 as wire_fixtures


class ResultRangeTransportIndependentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wire = wire_fixtures.MinerUHttpRemoteV4Tests()
        self.wire.setUp()
        self.addCleanup(self.wire.tearDown)
        self.body = b"abcdefghij"
        self.accepted, self.terminal, self.capability = self.wire._result_evidence(self.body)
        self.etag = '"' + self.terminal.artifact_sha256.removeprefix("sha256:") + '"'

    def _headers(self, *, start: int) -> dict[str, str]:
        return {
            **self.wire._result_headers(self.terminal),
            "Content-Length": str(len(self.body) - start),
            "Content-Range": f"bytes {start}-{len(self.body) - 1}/{len(self.body)}",
            "ETag": self.etag,
        }

    def _read(self, handler, *, start: int = 4, validator: str | None = None):
        with MinerUHttpRemoteV4(
            transport=httpx.MockTransport(handler),
            wall_clock=lambda: 10_000.0,
            request_timeout_seconds=30.0,
        ) as provider:
            return b"".join(provider.stream_result(
                accepted_submission=self.accepted,
                terminal_receipt=self.terminal,
                provider_capability=self.capability,
                result_lease_seconds=300,
                step_guard=self.wire.guard,
                before_result_get=lambda: None,
                range_start=start,
                strong_validator=self.etag if validator is None and start else validator,
            ))

    def test_exact_206_suffix_preserves_original_owner_and_closes_response(self) -> None:
        calls: list[str] = []
        stream = wire_fixtures._RecordingStream((self.body[4:],))

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            if request.method == "POST":
                self.assertEqual(request.url.params["seconds"], "300")
                return httpx.Response(200, json=self.wire._lease_payload(10_100.0))
            self.assertEqual(request.headers["Range"], "bytes=4-")
            self.assertEqual(request.headers["If-Range"], self.etag)
            self.assertEqual(request.headers["Accept-Encoding"], "identity")
            return httpx.Response(206, headers=self._headers(start=4), stream=stream)

        self.assertEqual(self._read(handler), self.body[4:])
        self.assertEqual(calls, ["POST /tasks/task-1/lease", "GET /tasks/task-1/result"])
        self.assertTrue(stream.read)
        self.assertTrue(stream.closed)

    def test_206_header_and_identity_drift_rejected_before_body(self) -> None:
        cases = (
            ("owner", {"X-MinerU-Result-Owner": "other-owner"}),
            ("sha", {"X-MinerU-Result-SHA256": "0" * 64}),
            ("weak-etag", {"ETag": "W/" + self.etag}),
            ("wrong-etag", {"ETag": '"other"'}),
            ("missing-etag", {"ETag": None}),
            ("range-start", {"Content-Range": "bytes 3-9/10"}),
            ("range-total", {"Content-Range": "bytes 4-9/11"}),
            ("range-end", {"Content-Range": "bytes 4-8/10"}),
            ("range-missing", {"Content-Range": None}),
            ("length-short", {"Content-Length": "5"}),
            ("length-long", {"Content-Length": "7"}),
            ("length-missing", {"Content-Length": None}),
            ("encoded", {"Content-Encoding": "gzip"}),
        )
        for name, overrides in cases:
            with self.subTest(case=name):
                stream = wire_fixtures._RecordingStream((self.body[4:],))
                headers = self._headers(start=4)
                for key, value in overrides.items():
                    if value is None:
                        headers.pop(key)
                    else:
                        headers[key] = value

                def handler(request: httpx.Request) -> httpx.Response:
                    if request.method == "POST":
                        return httpx.Response(200, json=self.wire._lease_payload(10_100.0))
                    return httpx.Response(206, headers=headers, stream=stream)

                with self.assertRaises(RemoteProviderProtocolErrorV4):
                    self._read(handler)
                self.assertFalse(stream.read)
                self.assertTrue(stream.closed)

    def test_206_short_and_long_body_rejected_and_closed(self) -> None:
        for name, chunks in (
            ("short", (self.body[4:8],)),
            ("long", (self.body[4:] + b"x",)),
        ):
            with self.subTest(case=name):
                stream = wire_fixtures._RecordingStream(chunks)

                def handler(request: httpx.Request) -> httpx.Response:
                    if request.method == "POST":
                        return httpx.Response(200, json=self.wire._lease_payload(10_100.0))
                    return httpx.Response(206, headers=self._headers(start=4), stream=stream)

                with self.assertRaises(RemoteProviderProtocolErrorV4):
                    self._read(handler)
                self.assertTrue(stream.read)
                self.assertTrue(stream.closed)

    def test_ignored_200_and_416_are_not_streamed_as_suffix(self) -> None:
        for status, same_owner in ((200, True), (200, False), (416, True)):
            with self.subTest(status=status, same_owner=same_owner):
                stream = wire_fixtures._RecordingStream((self.body,))

                def handler(request: httpx.Request) -> httpx.Response:
                    if request.method == "POST":
                        return httpx.Response(200, json=self.wire._lease_payload(10_100.0))
                    headers = {
                        **self.wire._result_headers(self.terminal), "ETag": self.etag,
                    }
                    if not same_owner:
                        headers["X-MinerU-Result-Owner"] = "other-owner"
                    return httpx.Response(status, headers=headers, stream=stream)

                expected = (
                    RemoteResultRangeIgnoredV4 if status == 200
                    else RemoteResultRangeUnsatisfiableV4
                )
                with self.assertRaises(expected) as raised:
                    self._read(handler)
                if status == 200:
                    self.assertEqual(raised.exception.same_identity, same_owner)
                self.assertFalse(stream.read)
                self.assertTrue(stream.closed)

    def test_close_or_stream_error_releases_range_response(self) -> None:
        chunk = b"a" * (1024 * 1024)
        body = chunk + chunk
        accepted, terminal, capability = self.wire._result_evidence(body)
        etag = '"' + terminal.artifact_sha256.removeprefix("sha256:") + '"'
        headers = {
            **self.wire._result_headers(terminal),
            "Content-Length": str(len(body) - 1),
            "Content-Range": f"bytes 1-{len(body) - 1}/{len(body)}",
            "ETag": etag,
        }

        class InterruptingStream(httpx.SyncByteStream):
            def __init__(self, *, fail: bool) -> None:
                self.fail = fail
                self.closed = False

            def __iter__(self):
                yield chunk
                if self.fail:
                    raise httpx.ReadError("stream interrupted")
                yield chunk[1:]

            def close(self) -> None:
                self.closed = True

        for fail in (False, True):
            with self.subTest(fail=fail):
                stream = InterruptingStream(fail=fail)

                def handler(request: httpx.Request) -> httpx.Response:
                    if request.method == "POST":
                        return httpx.Response(200, json=self.wire._lease_payload(10_100.0))
                    return httpx.Response(206, headers=headers, stream=stream)

                with MinerUHttpRemoteV4(
                    transport=httpx.MockTransport(handler), wall_clock=lambda: 10_000.0,
                    request_timeout_seconds=30.0,
                ) as provider:
                    iterator = iter(provider.stream_result(
                        accepted_submission=accepted, terminal_receipt=terminal,
                        provider_capability=capability, result_lease_seconds=300,
                        step_guard=self.wire.guard, before_result_get=lambda: None,
                        range_start=1, strong_validator=etag,
                    ))
                    self.assertEqual(next(iterator), chunk)
                    if fail:
                        with self.assertRaises(RemoteProviderUnavailableV4):
                            next(iterator)
                    else:
                        iterator.close()
                self.assertTrue(stream.closed)

    def test_original_complete_get_still_works_and_bad_range_stays_local(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "POST":
                return httpx.Response(200, json=self.wire._lease_payload(10_100.0))
            self.assertNotIn("Range", request.headers)
            self.assertNotIn("If-Range", request.headers)
            return httpx.Response(200, content=self.body,
                headers=self.wire._result_headers(self.terminal))

        self.assertEqual(self._read(handler, start=0), self.body)
        self.assertEqual(calls, ["POST", "GET"])
        calls.clear()
        for start, validator in ((-1, self.etag), (len(self.body), self.etag),
                                 (4, '"wrong"'), (4, "W/" + self.etag)):
            with self.subTest(start=start, validator=validator):
                with self.assertRaises(RemoteProviderProtocolErrorV4):
                    self._read(handler, start=start, validator=validator)
                self.assertEqual(calls, [])
