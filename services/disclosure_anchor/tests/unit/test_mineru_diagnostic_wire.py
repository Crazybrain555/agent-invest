"""Independent tests for the extracted DiagnosticWireClient.

The client owns one HTTP client, applies the caller's original operation budget
to every request and chunk, and refuses non-identity encodings and oversize
bodies.  It never retries and never interprets responses.  A MockTransport with
explicit chunked streams stands in for the provider; nothing is sent anywhere.
The legacy v1 composition in ``test_mineru_diagnostic.py`` remains untouched and
is the end-to-end compatibility evidence; this module covers the client alone.
"""

from __future__ import annotations

import unittest
from collections.abc import Iterator

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import MAX_WIRE_JSON_BYTES
from disclosure_anchor.adapters.runtime.mineru_diagnostic_wire import DiagnosticWireClient

CHUNK = 64 * 1024


class _ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes], *, close_error: BaseException | None = None) -> None:
        self.chunks = chunks
        self.yielded = 0
        self.closed = False
        self.close_error = close_error

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _Checkpoint:
    def __init__(self, budget: float = 7.5, *, fail_at: int | None = None) -> None:
        self.budget = budget
        self.fail_at = fail_at
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise TimeoutError("diagnostic deadline expired; preserve exact attempt")
        return self.budget


class _Provider:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.streams: list[_ChunkStream] = []

    def stream(self, chunks: list[bytes], **kwargs: object) -> _ChunkStream:
        created = _ChunkStream(chunks, **kwargs)  # type: ignore[arg-type]
        self.streams.append(created)
        return created

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/three":
            return httpx.Response(200, stream=self.stream([b"a", b"b", b"c"]))
        if path == "/tasks/absent":
            return httpx.Response(
                404,
                stream=self.stream([b'{"detail":', b'"Task not found"}']),
                headers={"content-type": "application/json"},
            )
        if path == "/exact":
            full, rest = divmod(MAX_WIRE_JSON_BYTES, CHUNK)
            chunks = [b"e" * CHUNK] * full + ([b"e" * rest] if rest else [])
            return httpx.Response(200, stream=self.stream(chunks))
        if path == "/over":
            full, rest = divmod(MAX_WIRE_JSON_BYTES + 1, CHUNK)
            chunks = [b"o" * CHUNK] * full + ([b"o" * rest] if rest else []) + [b"tail"] * 3
            return httpx.Response(200, stream=self.stream(chunks))
        if path == "/gzip":
            return httpx.Response(200, stream=self.stream([b"\x1f\x8b"]), headers={"content-encoding": "gzip"})
        if path == "/identity":
            return httpx.Response(200, stream=self.stream([b"plain"]), headers={"content-encoding": "identity"})
        if path == "/redirect":
            return httpx.Response(302, stream=self.stream([b"moved"]), headers={"location": "/tasks/absent"})
        if path == "/close-error":
            return httpx.Response(200, stream=self.stream([b"ok"], close_error=OSError("synthetic close failure")))
        if path == "/upload":
            body = request.read()
            return httpx.Response(202, stream=self.stream([str(len(body)).encode()]))
        raise AssertionError(path)


class DiagnosticWireClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = _Provider()
        self.transport = httpx.MockTransport(self.provider.handle)

    def client(self, checkpoint: _Checkpoint) -> DiagnosticWireClient:
        wire = DiagnosticWireClient(checkpoint=checkpoint, transport=self.transport)
        self.addCleanup(wire.client.close)
        return wire

    def test_request_returns_exact_status_and_raw_bytes_under_the_original_budget(self) -> None:
        checkpoint = _Checkpoint(7.5)
        wire = self.client(checkpoint)
        self.assertEqual(wire.request("GET", "http://provider.invalid/three"), (200, b"abc"))
        request = self.provider.requests[-1]
        self.assertEqual(request.headers["accept-encoding"], "identity")
        timeout = request.extensions["timeout"]
        self.assertEqual((timeout["connect"], timeout["read"], timeout["write"], timeout["pool"]), (7.5, 7.5, 7.5, 7.5))
        self.assertEqual(checkpoint.calls, 5)
        self.assertTrue(self.provider.streams[-1].closed)

        checkpoint.budget = 0.25
        status, exact = wire.request("GET", "http://provider.invalid/tasks/absent")
        self.assertEqual((status, exact), (404, b'{"detail":"Task not found"}'))
        self.assertEqual(self.provider.requests[-1].extensions["timeout"]["read"], 0.25)
        self.assertEqual(len(self.provider.requests), 2)

    def test_byte_envelope_is_exact_and_oversize_bodies_stop_before_the_tail(self) -> None:
        wire = self.client(_Checkpoint())
        status, exact = wire.request("GET", "http://provider.invalid/exact")
        self.assertEqual((status, len(exact)), (200, MAX_WIRE_JSON_BYTES))
        self.assertTrue(self.provider.streams[-1].closed)

        with self.assertRaisesRegex(ValueError, "byte envelope"):
            wire.request("GET", "http://provider.invalid/over")
        over = self.provider.streams[-1]
        self.assertTrue(over.closed)
        self.assertLess(over.yielded, len(over.chunks))
        self.assertEqual(over.yielded, len(over.chunks) - 3)

    def test_non_identity_content_encoding_is_rejected_without_reading_the_body(self) -> None:
        wire = self.client(_Checkpoint())
        with self.assertRaisesRegex(ValueError, "content encoding"):
            wire.request("GET", "http://provider.invalid/gzip")
        rejected = self.provider.streams[-1]
        self.assertEqual(rejected.yielded, 0)
        self.assertTrue(rejected.closed)
        self.assertEqual(wire.request("GET", "http://provider.invalid/identity"), (200, b"plain"))

    def test_checkpoint_deadline_mid_stream_propagates_and_closes_the_response(self) -> None:
        checkpoint = _Checkpoint(fail_at=3)
        wire = self.client(checkpoint)
        with self.assertRaisesRegex(TimeoutError, "deadline expired"):
            wire.request("GET", "http://provider.invalid/three")
        stream = self.provider.streams[-1]
        self.assertEqual(stream.yielded, 2)
        self.assertTrue(stream.closed)
        self.assertEqual(checkpoint.calls, 3)
        self.assertFalse(wire.client.is_closed)

        with self.assertRaisesRegex(TimeoutError, "deadline expired"):
            _Checkpoint(fail_at=1)()
        first_call = _Checkpoint(fail_at=1)
        before = len(self.provider.requests)
        with self.assertRaises(TimeoutError):
            self.client(first_call).request("GET", "http://provider.invalid/three")
        self.assertEqual(len(self.provider.requests), before)

    def test_redirects_are_not_followed_and_environment_is_not_trusted(self) -> None:
        wire = self.client(_Checkpoint())
        self.assertEqual(wire.request("GET", "http://provider.invalid/redirect"), (302, b"moved"))
        self.assertEqual([request.url.path for request in self.provider.requests], ["/redirect"])
        self.assertFalse(wire.client.follow_redirects)
        self.assertFalse(wire.client.trust_env)

    def test_context_exit_closes_the_owned_client_and_closure_failures_are_visible(self) -> None:
        with DiagnosticWireClient(checkpoint=_Checkpoint(), transport=self.transport) as wire:
            self.assertIsInstance(wire, DiagnosticWireClient)
            self.assertEqual(wire.request("GET", "http://provider.invalid/three"), (200, b"abc"))
            self.assertFalse(wire.client.is_closed)
        self.assertTrue(wire.client.is_closed)
        with self.assertRaises(RuntimeError):
            wire.request("GET", "http://provider.invalid/three")
        self.assertEqual(len(self.provider.requests), 1)

        wire = self.client(_Checkpoint())
        with self.assertRaisesRegex(OSError, "synthetic close failure"):
            wire.request("GET", "http://provider.invalid/close-error")
        self.assertTrue(self.provider.streams[-1].closed)
        self.assertEqual(wire.request("GET", "http://provider.invalid/three"), (200, b"abc"))

    def test_read_response_serves_legacy_streaming_loops_with_the_same_guards(self) -> None:
        checkpoint = _Checkpoint()
        wire = self.client(checkpoint)
        with wire.client.stream(
            "POST",
            "http://provider.invalid/upload",
            files={"files": ("sha256_synthetic.pdf", b"%PDF-synthetic", "application/pdf")},
            timeout=checkpoint(),
            headers={"Accept-Encoding": "identity"},
        ) as response:
            status, exact = response.status_code, wire.read_response(response)
        self.assertEqual(status, 202)
        self.assertGreater(int(exact), len(b"%PDF-synthetic"))
        self.assertEqual(checkpoint.calls, 2)
        self.assertEqual(self.provider.requests[-1].headers["accept-encoding"], "identity")

        with wire.client.stream("GET", "http://provider.invalid/gzip") as response:
            with self.assertRaisesRegex(ValueError, "content encoding"):
                wire.read_response(response)
        self.assertEqual(self.provider.streams[-1].yielded, 0)


if __name__ == "__main__":
    unittest.main()
