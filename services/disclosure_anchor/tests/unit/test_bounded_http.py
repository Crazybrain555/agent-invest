from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import time
from typing import cast
import unittest
from unittest.mock import Mock, patch

from disclosure_anchor.adapters.runtime.bounded_http import (
    BoundedHTTPProtocolError,
    BoundedHTTPTransportError,
    ThreadOwnedPersistentHTTPClient,
)


class _CountingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        self.accepted_connections = 0
        self.last_post_body = b""
        self.last_post_content_type: str | None = None
        super().__init__(("127.0.0.1", 0), _KeepAliveHandler)

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        request, address = super().get_request()
        self.accepted_connections += 1
        return request, address

    def handle_error(self, _request: object, _client_address: object) -> None:
        return


class _KeepAliveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        raw_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_length or "-1")
        except ValueError:
            self.send_error(400)
            return
        if content_length < 0:
            self.send_error(411)
            return
        server = cast(_CountingHTTPServer, self.server)
        server.last_post_body = self.rfile.read(content_length)
        server.last_post_content_type = self.headers.get("Content-Type")
        self._respond()

    def _respond(self) -> None:
        if self.path in {"/slow-drip", "/slow-header-body"}:
            payload = b"abcdefgh"
            if self.path == "/slow-header-body":
                time.sleep(0.08)
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                for value in payload:
                    self.wfile.write(bytes((value,)))
                    self.wfile.flush()
                    time.sleep(0.08)
            except OSError:
                pass
            return
        if self.path == "/ambiguous-framing":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Content-Length", "2")
            self.end_headers()
            try:
                self.wfile.write(b"2\r\nok\r\n0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass
            return
        if self.path == "/short":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"{}")
            self.close_connection = True
            return
        if self.path == "/chunked":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in (b"o", b"k"):
                self.wfile.write(f"{len(chunk):X}\r\n".encode())
                self.wfile.write(chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            return
        if self.path == "/close-delimited":
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in (b"o", b"k"):
                self.wfile.write(chunk)
                self.wfile.flush()
            self.close_connection = True
            return
        if self.path == "/status503":
            payload = b"down"
            self.send_response(503)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = b"x" * 128 if self.path == "/oversize" else b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        if self.path == "/close":
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ThreadOwnedPersistentHTTPClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _CountingHTTPServer()
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.server_thread.start()
        host = cast(str, self.server.server_address[0])
        port = cast(int, self.server.server_address[1])
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)

    def test_thousand_samples_reuse_one_tcp_connection(self) -> None:
        client = ThreadOwnedPersistentHTTPClient(
            self.base_url,
            maximum_response_bytes=64,
        )
        try:
            for _ in range(1000):
                status, payload = client.get_bytes(
                    "/ok",
                    timeout_seconds=2,
                    transport_attempts=2,
                )
                self.assertEqual((status, payload), (200, b"ok"))
        finally:
            client.close()
        self.assertEqual(self.server.accepted_connections, 1)

    def test_server_close_and_oversize_drop_before_reconnect(self) -> None:
        client = ThreadOwnedPersistentHTTPClient(
            self.base_url,
            maximum_response_bytes=64,
        )
        try:
            self.assertEqual(client.get_bytes("/close", timeout_seconds=2)[0], 200)
            self.assertEqual(client.get_bytes("/ok", timeout_seconds=2)[1], b"ok")
            with self.assertRaisesRegex(BoundedHTTPProtocolError, "safety limit"):
                client.get_bytes("/oversize", timeout_seconds=2)
            self.assertEqual(client.get_bytes("/ok", timeout_seconds=2)[1], b"ok")
        finally:
            client.close()
        self.assertEqual(self.server.accepted_connections, 3)

    def test_chunked_and_close_delimited_multiread_responses_are_complete(
        self,
    ) -> None:
        client = ThreadOwnedPersistentHTTPClient(
            self.base_url,
            maximum_response_bytes=64,
        )
        try:
            self.assertEqual(
                client.get_bytes("/chunked", timeout_seconds=2),
                (200, b"ok"),
            )
            self.assertEqual(
                client.get_bytes("/close-delimited", timeout_seconds=2),
                (200, b"ok"),
            )
        finally:
            client.close()

    def test_client_rejects_cross_thread_use(self) -> None:
        client = ThreadOwnedPersistentHTTPClient(
            self.base_url,
            maximum_response_bytes=64,
        )
        self.assertEqual(client.get_bytes("/ok", timeout_seconds=2)[1], b"ok")
        observed: list[BaseException] = []

        def cross_thread_request() -> None:
            try:
                client.get_bytes("/ok", timeout_seconds=2)
            except BaseException as exc:
                observed.append(exc)

        thread = threading.Thread(target=cross_thread_request)
        thread.start()
        thread.join(timeout=5)
        client.close()
        self.assertEqual(len(observed), 1)
        self.assertIsInstance(observed[0], RuntimeError)

    def test_transport_failure_reconnects_inside_one_budget(self) -> None:
        response = Mock()
        response.getheader.side_effect = (
            lambda name: "2" if name == "Content-Length" else None
        )
        response.read.return_value = b"ok"
        response.status = 200
        response.will_close = False
        first = Mock()
        first.request.side_effect = OSError(49, "address unavailable")
        second = Mock()
        second.getresponse.return_value = response
        client = ThreadOwnedPersistentHTTPClient(
            "http://127.0.0.1:9",
            maximum_response_bytes=64,
        )
        with patch.object(client, "_new_connection", side_effect=(first, second)):
            self.assertEqual(
                client.get_bytes(
                    "/ok",
                    timeout_seconds=2,
                    transport_attempts=2,
                ),
                (200, b"ok"),
            )
        first.close.assert_called_once()
        client.close()
        second.close.assert_called_once()

    def test_socket_timeout_update_failure_drops_and_retries(self) -> None:
        response = Mock()
        response.getheader.side_effect = (
            lambda name: "2" if name == "Content-Length" else None
        )
        response.read.return_value = b"ok"
        response.status = 200
        response.will_close = False
        first = Mock()
        first.sock.settimeout.side_effect = OSError(9, "stale socket")
        second = Mock()
        second.getresponse.return_value = response
        client = ThreadOwnedPersistentHTTPClient(
            "http://127.0.0.1:9",
            maximum_response_bytes=64,
        )
        with patch.object(client, "_new_connection", side_effect=(first, second)):
            self.assertEqual(
                client.get_bytes(
                    "/ok",
                    timeout_seconds=2,
                    transport_attempts=2,
                ),
                (200, b"ok"),
            )
        first.close.assert_called_once()
        client.close()

    def test_deadline_close_exceptions_become_transport_and_drop(self) -> None:
        for exception_type in (ValueError, AttributeError):
            with self.subTest(exception_type=exception_type.__name__):
                closed = threading.Event()
                response = Mock()
                response.getheader.side_effect = (
                    lambda name: "1" if name == "Content-Length" else None
                )

                def fail_after_deadline(_amount: int) -> bytes:
                    self.assertTrue(closed.wait(timeout=1))
                    raise exception_type("response closed by watchdog")

                response.read.side_effect = fail_after_deadline
                response.will_close = False
                connection = Mock()
                connection.sock = None
                connection.getresponse.return_value = response
                connection.close.side_effect = closed.set
                client = ThreadOwnedPersistentHTTPClient(
                    "http://127.0.0.1:9",
                    maximum_response_bytes=64,
                )
                with (
                    patch.object(
                        client,
                        "_new_connection",
                        return_value=connection,
                    ),
                    self.assertRaises(BoundedHTTPTransportError),
                ):
                    client.get_bytes(
                        "/ok",
                        timeout_seconds=0.05,
                        transport_attempts=1,
                    )
                self.assertIsNone(client._connection)

    def test_deadline_close_exception_retries_inside_remaining_budget(self) -> None:
        closed = threading.Event()
        first_response = Mock()
        first_response.getheader.side_effect = (
            lambda name: "1" if name == "Content-Length" else None
        )

        def fail_after_deadline(_amount: int) -> bytes:
            self.assertTrue(closed.wait(timeout=1))
            raise ValueError("response closed by watchdog")

        first_response.read.side_effect = fail_after_deadline
        first_response.will_close = False
        first = Mock()
        first.sock = None
        first.getresponse.return_value = first_response
        first.close.side_effect = closed.set

        second_response = Mock()
        second_response.getheader.side_effect = (
            lambda name: "2" if name == "Content-Length" else None
        )
        second_response.read.return_value = b"ok"
        second_response.status = 200
        second_response.will_close = False
        second = Mock()
        second.sock = None
        second.getresponse.return_value = second_response
        client = ThreadOwnedPersistentHTTPClient(
            "http://127.0.0.1:9",
            maximum_response_bytes=64,
        )
        with patch.object(client, "_new_connection", side_effect=(first, second)):
            self.assertEqual(
                client.get_bytes(
                    "/ok",
                    timeout_seconds=0.5,
                    transport_attempts=2,
                    maximum_attempt_timeout_seconds=0.05,
                ),
                (200, b"ok"),
            )
        client.close()

    def test_predeadline_value_error_is_not_retried_but_drops_connection(self) -> None:
        response = Mock()
        response.getheader.side_effect = (
            lambda name: "1" if name == "Content-Length" else None
        )
        response.read.side_effect = ValueError("ordinary reader bug")
        response.will_close = False
        connection = Mock()
        connection.sock = None
        connection.getresponse.return_value = response
        client = ThreadOwnedPersistentHTTPClient(
            "http://127.0.0.1:9",
            maximum_response_bytes=64,
        )
        with (
            patch.object(
                client,
                "_new_connection",
                return_value=connection,
            ) as new_connection,
            self.assertRaisesRegex(ValueError, "ordinary reader bug"),
        ):
            client.get_bytes(
                "/ok",
                timeout_seconds=0.5,
                transport_attempts=2,
            )
        new_connection.assert_called_once()
        self.assertIsNone(client._connection)

    def test_wall_deadline_interrupts_slow_drip_and_drops_connection(self) -> None:
        client = ThreadOwnedPersistentHTTPClient(
            self.base_url,
            maximum_response_bytes=64,
        )
        started = time.monotonic()
        try:
            with self.assertRaises(BoundedHTTPTransportError):
                client.get_bytes(
                    "/slow-drip",
                    timeout_seconds=0.2,
                    transport_attempts=1,
                )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.5)
            self.assertEqual(client.get_bytes("/ok", timeout_seconds=2)[1], b"ok")
        finally:
            client.close()
        self.assertEqual(self.server.accepted_connections, 2)

    def test_post_sends_exact_body_and_reuses_the_direct_connection(self) -> None:
        client = ThreadOwnedPersistentHTTPClient(
            self.base_url,
            maximum_response_bytes=64,
        )
        payload = b'{"value":"M7"}'
        try:
            self.assertEqual(
                client.post_bytes(
                    "/ok",
                    payload,
                    content_type="application/json",
                    timeout_seconds=2,
                ),
                (200, b"ok"),
            )
            self.assertEqual(client.get_bytes("/ok", timeout_seconds=2)[1], b"ok")
        finally:
            client.close()
        self.assertEqual(self.server.last_post_body, payload)
        self.assertEqual(self.server.last_post_content_type, "application/json")
        self.assertEqual(self.server.accepted_connections, 1)

    def test_post_wall_deadline_covers_header_and_slow_drip_body(self) -> None:
        for path in ("/slow-drip", "/slow-header-body"):
            with self.subTest(path=path):
                client = ThreadOwnedPersistentHTTPClient(
                    self.base_url,
                    maximum_response_bytes=64,
                )
                started = time.monotonic()
                try:
                    with self.assertRaises(BoundedHTTPTransportError):
                        client.post_bytes(
                            path,
                            b"{}",
                            content_type="application/json",
                            timeout_seconds=0.2,
                        )
                    self.assertLess(time.monotonic() - started, 0.5)
                    self.assertIsNone(client._connection)
                finally:
                    client.close()

    def test_post_reuses_response_framing_bounds_and_exposes_status(self) -> None:
        for path, error, maximum_bytes in (
            ("/ambiguous-framing", BoundedHTTPProtocolError, 64),
            ("/oversize", BoundedHTTPProtocolError, 64),
            ("/short", BoundedHTTPTransportError, 128),
        ):
            with self.subTest(path=path):
                client = ThreadOwnedPersistentHTTPClient(
                    self.base_url,
                    maximum_response_bytes=maximum_bytes,
                )
                with self.assertRaises(error):
                    client.post_bytes(
                        path,
                        b"{}",
                        content_type="application/json",
                        timeout_seconds=2,
                    )
                self.assertIsNone(client._connection)
                client.close()
        client = ThreadOwnedPersistentHTTPClient(
            self.base_url,
            maximum_response_bytes=64,
        )
        try:
            self.assertEqual(
                client.post_bytes(
                    "/status503",
                    b"{}",
                    content_type="application/json",
                    timeout_seconds=2,
                ),
                (503, b"down"),
            )
        finally:
            client.close()

    def test_request_header_and_body_share_one_attempt_deadline(self) -> None:
        client = ThreadOwnedPersistentHTTPClient(
            self.base_url,
            maximum_response_bytes=64,
        )
        started = time.monotonic()
        try:
            with self.assertRaises(BoundedHTTPTransportError):
                client.get_bytes(
                    "/slow-header-body",
                    timeout_seconds=0.2,
                    transport_attempts=1,
                )
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            client.close()

    def test_attempt_deadlines_share_one_logical_budget(self) -> None:
        client = ThreadOwnedPersistentHTTPClient(
            self.base_url,
            maximum_response_bytes=64,
        )
        started = time.monotonic()
        try:
            with self.assertRaises(BoundedHTTPTransportError):
                client.get_bytes(
                    "/slow-drip",
                    timeout_seconds=0.25,
                    transport_attempts=2,
                    maximum_attempt_timeout_seconds=0.15,
                )
            self.assertLess(time.monotonic() - started, 0.55)
        finally:
            client.close()

    def test_ambiguous_framing_is_rejected_before_body_read(self) -> None:
        client = ThreadOwnedPersistentHTTPClient(
            self.base_url,
            maximum_response_bytes=64,
        )
        try:
            with self.assertRaisesRegex(BoundedHTTPProtocolError, "ambiguous"):
                client.get_bytes("/ambiguous-framing", timeout_seconds=2)
            self.assertEqual(client.get_bytes("/ok", timeout_seconds=2)[1], b"ok")
        finally:
            client.close()
        self.assertEqual(self.server.accepted_connections, 2)

    def test_exhausted_transport_budget_fails_closed(self) -> None:
        connection = Mock()
        connection.request.side_effect = OSError(49, "address unavailable")
        client = ThreadOwnedPersistentHTTPClient(
            "http://127.0.0.1:9",
            maximum_response_bytes=64,
        )
        with (
            patch.object(client, "_new_connection", return_value=connection),
            self.assertRaises(BoundedHTTPTransportError),
        ):
            client.get_bytes(
                "/ok",
                timeout_seconds=2,
                transport_attempts=2,
            )
        self.assertEqual(connection.request.call_count, 2)

    def test_real_short_content_length_response_is_transport_failure(self) -> None:
        client = ThreadOwnedPersistentHTTPClient(
            self.base_url,
            maximum_response_bytes=128,
        )
        try:
            with self.assertRaises(BoundedHTTPTransportError):
                client.get_bytes("/short", timeout_seconds=2)
            self.assertEqual(client.get_bytes("/ok", timeout_seconds=2)[1], b"ok")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
