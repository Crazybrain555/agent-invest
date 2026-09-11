"""Shared bounded wire reads for legacy and explicit v2 diagnostic owners."""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import MAX_WIRE_JSON_BYTES


class DiagnosticWireClient:
    """Own one client; each read uses the caller's original operation budget.

    This class neither retries requests nor interprets provider responses. The
    caller preserves raw evidence and validates task/source/phase identity before
    another side effect. Legacy upload/download loops share this client's stream
    while retaining their existing per-chunk guards and byte envelopes.
    """

    def __init__(self, *, checkpoint: Callable[[], float], transport: httpx.BaseTransport | None = None) -> None:
        self._checkpoint = checkpoint
        self.client = httpx.Client(trust_env=False, follow_redirects=False, transport=transport)

    def read_response(self, response: httpx.Response) -> bytes:
        parts: list[bytes] = []
        size = 0
        if response.headers.get("content-encoding", "identity") != "identity":
            raise ValueError("diagnostic response content encoding is unsupported")
        for chunk in response.iter_raw():
            self._checkpoint()
            size += len(chunk)
            if size > MAX_WIRE_JSON_BYTES:
                raise ValueError("diagnostic wire response exceeds byte envelope")
            parts.append(chunk)
        return b"".join(parts)

    def request(self, method: str, url: str) -> tuple[int, bytes]:
        with self.client.stream(method, url, timeout=self._checkpoint(),
                                headers={"Accept-Encoding": "identity"}) as response:
            status = response.status_code
            exact = self.read_response(response)
        self._checkpoint()
        return status, exact

    def __enter__(self) -> DiagnosticWireClient:
        self.client.__enter__()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.client.__exit__(exc_type, exc, traceback)
