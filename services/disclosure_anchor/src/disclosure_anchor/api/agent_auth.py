"""Scoped bearer access for the private agent API.

Two tokens, two scopes: the read token admits every GET route, the request
token admits only the processing-request write. Both are required at startup
and must differ. Every request must come from a loopback peer, carry no Origin
header and name an allowed Host; any failure is refused before the route runs.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
from typing import Literal

from disclosure_anchor.api.errors import FORBIDDEN, UNAUTHORIZED, FilingApiError
from disclosure_anchor.domain.errors import ConfigurationError
from disclosure_anchor.settings import Settings

try:
    from fastapi import Request
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    Request = None  # type: ignore[assignment, misc]


MIN_TOKEN_CHARS = 32
_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "testclient"})

Scope = Literal["read", "request"]


@dataclass(frozen=True)
class AgentApiAccess:
    read_token: bytes
    request_token: bytes
    allowed_hosts: frozenset[str]

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentApiAccess:
        read = settings.disclosure_agent_api_read_token
        request = settings.disclosure_agent_api_request_token
        if read is None or request is None:
            raise ConfigurationError(
                "DISCLOSURE_AGENT_API_READ_TOKEN and "
                "DISCLOSURE_AGENT_API_REQUEST_TOKEN are both required"
            )
        read_value = read.get_secret_value()
        request_value = request.get_secret_value()
        if len(read_value) < MIN_TOKEN_CHARS or len(request_value) < MIN_TOKEN_CHARS:
            raise ConfigurationError(
                f"agent API tokens must be at least {MIN_TOKEN_CHARS} characters"
            )
        if hmac.compare_digest(read_value.encode("utf-8"), request_value.encode("utf-8")):
            raise ConfigurationError("agent API read and request tokens must differ")
        hosts = frozenset(
            item.strip()
            for item in settings.disclosure_agent_api_allowed_hosts.split(",")
            if item.strip()
        )
        if not hosts:
            raise ConfigurationError("DISCLOSURE_AGENT_API_ALLOWED_HOSTS is empty")
        return cls(
            read_token=read_value.encode("utf-8"),
            request_token=request_value.encode("utf-8"),
            allowed_hosts=hosts,
        )


def require_agent_read(request: Request) -> None:
    _authorize(request, "read")


def require_agent_request(request: Request) -> None:
    _authorize(request, "request")


def _authorize(request: Request, scope: Scope) -> None:
    access = getattr(request.app.state, "agent_api_access", None)
    if not isinstance(access, AgentApiAccess):
        raise _refused(401, UNAUTHORIZED, "agent API access is not configured")
    peer = request.client.host if request.client else None
    if peer is not None and peer not in _LOOPBACK_PEERS:
        raise _refused(403, FORBIDDEN, "agent API is loopback-only")
    if "origin" in request.headers:
        raise _refused(403, FORBIDDEN, "browser origins are not accepted")
    if request.headers.get("host", "") not in access.allowed_hosts:
        raise _refused(403, FORBIDDEN, "host is not allowed")
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    token = presented.strip().encode("utf-8")
    if scheme.lower() != "bearer" or not token:
        raise _refused(401, UNAUTHORIZED, "missing or invalid bearer token")
    expected = access.read_token if scope == "read" else access.request_token
    other = access.request_token if scope == "read" else access.read_token
    if hmac.compare_digest(token, expected):
        return
    if hmac.compare_digest(token, other):
        raise _refused(403, FORBIDDEN, "token scope does not permit this route")
    raise _refused(401, UNAUTHORIZED, "missing or invalid bearer token")


def _refused(status_code: int, error_code: str, message: str) -> FilingApiError:
    return FilingApiError(status_code=status_code, error_code=error_code, message=message)
