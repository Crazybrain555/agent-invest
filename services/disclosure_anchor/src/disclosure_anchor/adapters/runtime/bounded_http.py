"""Thread-owned persistent HTTP transport with bounded response reads."""

from __future__ import annotations

import http.client
import socket
import threading
import time
from typing import Callable, Mapping
from urllib.parse import SplitResult, urlsplit


class BoundedHTTPTransportError(RuntimeError):
    """A direct HTTP transport could not complete inside its logical budget."""


class BoundedHTTPProtocolError(RuntimeError):
    """A direct HTTP response violated the bounded transport contract."""


class _AttemptDeadlineWatchdog:
    """Interrupt a blocked socket operation at one attempt's wall deadline."""

    def __init__(
        self,
        connection: http.client.HTTPConnection,
        *,
        timeout_seconds: float,
    ) -> None:
        self._connection = connection
        self._lock = threading.Lock()
        self._active = True
        self._expired = False
        self._timer = threading.Timer(timeout_seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self) -> None:
        with self._lock:
            if not self._active:
                return
            self._expired = True
            connection = self._connection
        sock = connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()

    def finish(self) -> bool:
        """Disarm the watchdog and report whether its deadline fired."""

        with self._lock:
            self._active = False
            expired = self._expired
        self._timer.cancel()
        self._timer.join(timeout=1.0)
        return expired


class ThreadOwnedPersistentHTTPClient:
    """Reuse one direct connection from exactly one owning thread.

    The caller supplies the logical request budget and retry count.  A broken,
    partial, closing, or oversized response always drops the connection before
    the error is exposed.  No environment proxy settings are consulted.
    """

    def __init__(
        self,
        base_url: str,
        *,
        maximum_response_bytes: int,
        user_agent: str = "disclosure-anchor/1",
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maximum_response_bytes <= 0:
            raise ValueError("maximum response bytes must be positive")
        parsed = urlsplit(base_url)
        self._parsed = self._validate_base_url(parsed)
        hostname = self._parsed.hostname
        if hostname is None:  # guarded by _validate_base_url
            raise AssertionError("validated HTTP base URL lost its hostname")
        self._host = hostname
        self._maximum_response_bytes = maximum_response_bytes
        self._user_agent = user_agent
        self._clock = monotonic_clock
        self._connection: http.client.HTTPConnection | None = None
        self._owner_thread_id: int | None = None

    @staticmethod
    def _validate_base_url(parsed: SplitResult) -> SplitResult:
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("persistent HTTP base URL must use http or https")
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("persistent HTTP base URL is invalid")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("persistent HTTP base URL port is invalid") from exc
        return parsed

    def _bind_owner(self) -> None:
        owner = threading.get_ident()
        if self._owner_thread_id is None:
            self._owner_thread_id = owner
        elif self._owner_thread_id != owner:
            raise RuntimeError("persistent HTTP client crossed thread ownership")

    def _new_connection(self, timeout_seconds: float) -> http.client.HTTPConnection:
        port = self._parsed.port
        connection_type: type[http.client.HTTPConnection]
        if self._parsed.scheme == "https":
            connection_type = http.client.HTTPSConnection
        else:
            connection_type = http.client.HTTPConnection
        return connection_type(
            self._host,
            port=port,
            timeout=timeout_seconds,
        )

    def _drop_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def _set_operation_timeout(
        self,
        connection: http.client.HTTPConnection,
        *,
        attempt_deadline: float,
    ) -> None:
        remaining = attempt_deadline - self._clock()
        if remaining <= 0:
            raise BoundedHTTPTransportError(
                "HTTP attempt exceeded its wall-clock deadline"
            )
        connection.timeout = remaining
        if connection.sock is not None:
            connection.sock.settimeout(remaining)

    @staticmethod
    def _canonical_content_length(raw: str) -> int:
        if (
            not raw
            or not raw.isascii()
            or not raw.isdigit()
            or (len(raw) > 1 and raw.startswith("0"))
        ):
            raise BoundedHTTPProtocolError(
                "HTTP response Content-Length is invalid"
            )
        return int(raw)

    def _read_bounded_response(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        *,
        attempt_deadline: float,
    ) -> bytes:
        content_length = response.getheader("Content-Length")
        transfer_encoding = response.getheader("Transfer-Encoding")
        if content_length is not None and transfer_encoding is not None:
            raise BoundedHTTPProtocolError(
                "HTTP response framing is ambiguous"
            )
        if transfer_encoding is not None and transfer_encoding.strip().lower() != "chunked":
            raise BoundedHTTPProtocolError(
                "HTTP response Transfer-Encoding is unsupported"
            )

        if content_length is not None:
            declared_bytes = self._canonical_content_length(content_length)
            if declared_bytes > self._maximum_response_bytes:
                raise BoundedHTTPProtocolError(
                    "HTTP response exceeds the safety limit"
                )
            chunks: list[bytes] = []
            remaining_bytes = declared_bytes
            while remaining_bytes:
                self._set_operation_timeout(
                    connection,
                    attempt_deadline=attempt_deadline,
                )
                chunk = response.read(min(64 * 1024, remaining_bytes))
                if not chunk:
                    raise BoundedHTTPTransportError(
                        "HTTP response ended before its declared length"
                    )
                chunks.append(chunk)
                remaining_bytes -= len(chunk)
            return b"".join(chunks)

        if transfer_encoding is None and not response.will_close:
            raise BoundedHTTPProtocolError(
                "HTTP response has no complete framing"
            )
        chunks = []
        observed_bytes = 0
        while True:
            self._set_operation_timeout(
                connection,
                attempt_deadline=attempt_deadline,
            )
            chunk = response.read(
                min(
                    64 * 1024,
                    self._maximum_response_bytes + 1 - observed_bytes,
                )
            )
            if not chunk:
                break
            chunks.append(chunk)
            observed_bytes += len(chunk)
            if observed_bytes > self._maximum_response_bytes:
                raise BoundedHTTPProtocolError(
                    "HTTP response exceeds the safety limit"
                )
        return b"".join(chunks)

    def _request_target(self, path: str) -> str:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("persistent HTTP request path is invalid")
        base_path = self._parsed.path.rstrip("/")
        return f"{base_path}{path}" or "/"

    def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout_seconds: float,
        transport_attempts: int = 1,
        maximum_attempt_timeout_seconds: float | None = None,
    ) -> tuple[int, bytes]:
        self._bind_owner()
        if (
            method not in {"GET", "POST"}
            or (method == "GET" and body is not None)
            or (method == "POST" and body is None)
            or timeout_seconds <= 0
            or transport_attempts <= 0
            or (
                maximum_attempt_timeout_seconds is not None
                and maximum_attempt_timeout_seconds <= 0
            )
        ):
            raise ValueError("persistent HTTP request budget is invalid")
        request_headers = {
            "Accept": "*/*",
            "User-Agent": self._user_agent,
            "Connection": "keep-alive",
            **headers,
        }
        target = self._request_target(path)
        deadline = self._clock() + timeout_seconds
        last_error: BaseException | None = None
        for attempt in range(1, transport_attempts + 1):
            attempt_started = self._clock()
            remaining = deadline - attempt_started
            if remaining <= 0:
                break
            attempt_timeout = (
                remaining
                if maximum_attempt_timeout_seconds is None
                else min(remaining, maximum_attempt_timeout_seconds)
            )
            attempt_deadline = attempt_started + attempt_timeout
            if self._connection is None:
                self._connection = self._new_connection(attempt_timeout)
            connection = self._connection
            active_watchdog = _AttemptDeadlineWatchdog(
                connection,
                timeout_seconds=attempt_timeout,
            )
            watchdog: _AttemptDeadlineWatchdog | None = active_watchdog
            try:
                self._set_operation_timeout(
                    connection,
                    attempt_deadline=attempt_deadline,
                )
                connection.request(
                    method,
                    target,
                    body=body,
                    headers=request_headers,
                )
                self._set_operation_timeout(
                    connection,
                    attempt_deadline=attempt_deadline,
                )
                response = connection.getresponse()
                payload = self._read_bounded_response(
                    response,
                    connection,
                    attempt_deadline=attempt_deadline,
                )
                expired = active_watchdog.finish()
                watchdog = None
                if expired or self._clock() > attempt_deadline:
                    raise BoundedHTTPTransportError(
                        "HTTP attempt exceeded its wall-clock deadline"
                    )
                status = response.status
                if response.will_close:
                    self._drop_connection()
                return status, payload
            except BoundedHTTPProtocolError:
                self._drop_connection()
                raise
            except (
                BoundedHTTPTransportError,
                OSError,
                TimeoutError,
                http.client.HTTPException,
            ) as exc:
                last_error = exc
                self._drop_connection()
                if attempt == transport_attempts:
                    break
            except Exception as exc:
                expired = active_watchdog.finish()
                watchdog = None
                self._drop_connection()
                if not expired:
                    raise
                last_error = exc
                if attempt == transport_attempts:
                    break
            finally:
                if watchdog is not None:
                    watchdog.finish()
        raise BoundedHTTPTransportError(
            "HTTP transport unavailable inside the logical request budget"
        ) from last_error

    def get_bytes(
        self,
        path: str,
        *,
        timeout_seconds: float,
        transport_attempts: int = 1,
        maximum_attempt_timeout_seconds: float | None = None,
    ) -> tuple[int, bytes]:
        """Return one complete bounded GET, reconnecting only on transport.

        ``timeout_seconds`` is a logical deadline shared by all attempts.
        HTTP status handling stays with the endpoint-specific caller.
        """

        return self._request_bytes(
            "GET",
            path,
            body=None,
            headers={},
            timeout_seconds=timeout_seconds,
            transport_attempts=transport_attempts,
            maximum_attempt_timeout_seconds=maximum_attempt_timeout_seconds,
        )

    def post_bytes(
        self,
        path: str,
        payload: bytes,
        *,
        content_type: str,
        timeout_seconds: float,
        transport_attempts: int = 1,
        maximum_attempt_timeout_seconds: float | None = None,
    ) -> tuple[int, bytes]:
        """Return one complete bounded POST without consulting proxy state."""

        if (
            not isinstance(payload, bytes)
            or not content_type
            or not content_type.isascii()
            or "\r" in content_type
            or "\n" in content_type
        ):
            raise ValueError("persistent HTTP POST payload metadata is invalid")
        return self._request_bytes(
            "POST",
            path,
            body=payload,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(payload)),
            },
            timeout_seconds=timeout_seconds,
            transport_attempts=transport_attempts,
            maximum_attempt_timeout_seconds=maximum_attempt_timeout_seconds,
        )

    def close(self) -> None:
        """Close the connection from its owner, or an as-yet-unbound client."""
        if self._connection is None and self._owner_thread_id is not None:
            return
        self._bind_owner()
        self._drop_connection()


__all__ = [
    "BoundedHTTPProtocolError",
    "BoundedHTTPTransportError",
    "ThreadOwnedPersistentHTTPClient",
]
