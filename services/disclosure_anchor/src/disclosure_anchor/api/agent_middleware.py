"""ASGI guard of the private agent API: authorize before any body is parsed.

Read token for GET/HEAD, request token for POST; loopback peer, no Origin and
an allowed Host are required first. POST bodies must declare a bounded length.
The access log records method, route template, status and duration only —
never the query string, headers or body.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
import hmac
import json
import logging
import time
from typing import Any

from disclosure_anchor.api.agent_auth import AgentApiAccess
from disclosure_anchor.api.agent_errors import LENGTH_REQUIRED, PAYLOAD_TOO_LARGE
from disclosure_anchor.api.errors import FORBIDDEN, UNAUTHORIZED

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

MAX_POST_BODY_BYTES = 16 * 1024
_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "testclient"})
ACCESS_LOG = logging.getLogger("disclosure_anchor.agent_api.access")


class AgentApiGuard:
    def __init__(self, app: ASGIApp, *, access: AgentApiAccess) -> None:
        self._app = app
        self._access = access

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        started = time.monotonic()
        status_holder = {"status": 0}

        async def logged_send(message: Message) -> None:
            if message.get("type") == "http.response.start":
                status_holder["status"] = int(message.get("status", 0))
            await send(message)

        refusal = self._refusal(scope)
        try:
            if refusal is not None:
                await _send_error(logged_send, *refusal)
            else:
                await self._app(scope, receive, logged_send)
        finally:
            route = scope.get("route")
            ACCESS_LOG.info(
                "agent_api method=%s route=%s status=%s duration_ms=%d",
                scope.get("method", "-"),
                getattr(route, "path", "<unmatched>"),
                status_holder["status"],
                int((time.monotonic() - started) * 1000),
            )

    def _refusal(self, scope: Scope) -> tuple[int, str, str] | None:
        client = scope.get("client")
        peer = client[0] if client else None
        if peer is not None and peer not in _LOOPBACK_PEERS:
            return 403, FORBIDDEN, "agent API is loopback-only"
        headers = _headers(scope)
        if "origin" in headers:
            return 403, FORBIDDEN, "browser origins are not accepted"
        if headers.get("host", "") not in self._access.allowed_hosts:
            return 403, FORBIDDEN, "host is not allowed"
        method = str(scope.get("method", "")).upper()
        scheme, _, presented = headers.get("authorization", "").partition(" ")
        token = presented.strip().encode("utf-8")
        if scheme.lower() != "bearer" or not token:
            return 401, UNAUTHORIZED, "missing or invalid bearer token"
        is_read = hmac.compare_digest(token, self._access.read_token)
        is_request = hmac.compare_digest(token, self._access.request_token)
        if not (is_read or is_request):
            return 401, UNAUTHORIZED, "missing or invalid bearer token"
        if method in ("GET", "HEAD") and not is_read:
            return 403, FORBIDDEN, "token scope does not permit this route"
        if method == "POST":
            if not is_request:
                return 403, FORBIDDEN, "token scope does not permit this route"
            length = headers.get("content-length")
            if length is None or not length.isdigit():
                return 411, LENGTH_REQUIRED, "a bounded Content-Length is required"
            if int(length) > MAX_POST_BODY_BYTES:
                return 413, PAYLOAD_TOO_LARGE, "request body is too large"
        return None


def _headers(scope: Scope) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers") or []:
        name = raw_name.decode("latin-1").lower()
        if name in headers:
            # A repeated security header is ambiguous; keep a value no check accepts.
            headers[name] = "\x00"
            continue
        headers[name] = raw_value.decode("latin-1")
    return headers


async def _send_error(send: Send, status_code: int, error_code: str, message: str) -> None:
    body = json.dumps(
        {"error_code": error_code, "message": message, "detail": {}},
        separators=(",", ":"),
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
