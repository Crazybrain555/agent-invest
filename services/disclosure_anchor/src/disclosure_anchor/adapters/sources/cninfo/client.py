"""CNINFO WebAPI HTTP client with token, rate-limit, retry, and redaction."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
import random
import time
from typing import Any

import httpx

from disclosure_anchor.domain.errors import ConfigurationError, SourceRequestError
from disclosure_anchor.settings import Settings


LOGGER = logging.getLogger(__name__)
HttpParamValue = str | int | float | bool | None

TOKEN_ENDPOINT = "https://webapi.cninfo.com.cn/api-cloud-platform/oauth2/token"
BASE_URL = "https://webapi.cninfo.com.cn"
SENSITIVE_PARAM_KEYS = frozenset({"access_token", "client_id", "client_secret"})
RETRYABLE_RESULT_CODES = frozenset({-1, 403, 404, 405})
TOKEN_REFRESH_RESULT_CODES = frozenset({404, 405})
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_FACTOR = 2.0
BACKOFF_CAP_SECONDS = 30.0


@dataclass(frozen=True)
class RequestAudit:
    provider_interface: str
    query_params: dict[str, object]
    http_status: int
    resultcode: int | None
    row_count: int | None
    elapsed_ms: int


@dataclass(frozen=True)
class CninfoResponse:
    payload: dict[str, Any]
    audit: RequestAudit


class CninfoClientError(SourceRequestError):
    """Raised when a CNINFO request cannot be completed under retry policy."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool,
        audit: RequestAudit | None = None,
    ) -> None:
        self.audit = audit
        super().__init__(message, error_code=error_code, retryable=retryable)


class TokenBucket:
    """Simple process-local QPS limiter."""

    def __init__(
        self,
        *,
        max_qps: float,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if max_qps <= 0:
            raise ValueError("CNINFO max_qps must be greater than zero")
        self._interval_seconds = 1.0 / max_qps
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._next_available_at = 0.0

    def take(self) -> None:
        now = self._clock()
        if now < self._next_available_at:
            wait_seconds = self._next_available_at - now
            self._sleep(wait_seconds)
            now = self._clock()
        self._next_available_at = max(now, self._next_available_at) + self._interval_seconds


class CninfoClient:
    """Small CNINFO client used by source adapter and sync use cases."""

    def __init__(
        self,
        *,
        access_key: str | None,
        access_secret: str | None,
        access_token: str | None,
        max_qps: float = 1.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        bucket: TokenBucket | None = None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("CNINFO max_retries must be non-negative")
        if not access_token and not (access_key and access_secret):
            raise ConfigurationError(
                "CNINFO credentials require CNINFO_ACCESS_TOKEN or key/secret"
            )
        self._access_key = access_key
        self._access_secret = access_secret
        self._access_token = access_token
        self._max_retries = max_retries
        self._bucket = bucket or TokenBucket(max_qps=max_qps, sleep=sleep)
        self._sleep = sleep or time.sleep
        self._jitter = jitter or (lambda upper: random.uniform(0.0, upper))
        self._client = httpx.Client(transport=transport, timeout=30.0, trust_env=False)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[float], float] | None = None,
    ) -> "CninfoClient":
        return cls(
            access_key=_secret_value(settings.cninfo_access_key),
            access_secret=_secret_value(settings.cninfo_access_secret),
            access_token=_secret_value(settings.cninfo_access_token),
            max_qps=settings.cninfo_max_qps,
            max_retries=settings.cninfo_max_retries,
            transport=transport,
            sleep=sleep,
            jitter=jitter,
        )

    def get_json(
        self,
        *,
        provider_interface: str,
        path: str,
        params: Mapping[str, object],
    ) -> CninfoResponse:
        token = self._ensure_token()
        request_params = {"format": "json", **dict(params), "access_token": token}
        return self._request_json_with_retries(
            provider_interface=provider_interface,
            path=path,
            params=request_params,
        )

    def download_bytes(
        self,
        *,
        provider_interface: str,
        url: str,
        params: Mapping[str, object] | None = None,
    ) -> tuple[bytes, RequestAudit]:
        request_params = dict(params or {})
        response = self._request_bytes_with_retries(
            provider_interface=provider_interface,
            url=url,
            params=request_params,
        )
        return response

    def close(self) -> None:
        self._client.close()

    def _ensure_token(self) -> str:
        # Reuse the cached token; expiry is handled by the refresh-on-resultcode
        # path in _request_json_with_retries, so fetching per call would only
        # double traffic against the token endpoint.
        if not self._access_token and self._access_key and self._access_secret:
            self._access_token = self._fetch_token()
        if not self._access_token:
            raise ConfigurationError("CNINFO access token is missing")
        return self._access_token

    def _fetch_token(self) -> str:
        body = {
            "grant_type": "client_credentials",
            "client_id": self._access_key,
            "client_secret": self._access_secret,
        }
        started = time.perf_counter()
        self._bucket.take()
        response = self._client.post(TOKEN_ENDPOINT, data=body)
        elapsed_ms = _elapsed_ms(started)
        audit = RequestAudit(
            provider_interface="cninfo:token",
            query_params=redact_params(body),
            http_status=response.status_code,
            resultcode=None,
            row_count=None,
            elapsed_ms=elapsed_ms,
        )
        self._log_audit(audit)
        if response.status_code >= 400:
            raise CninfoClientError(
                "CNINFO token request failed",
                error_code=f"http_{response.status_code}",
                retryable=response.status_code == 429 or response.status_code >= 500,
                audit=audit,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CninfoClientError(
                "CNINFO token response body is not JSON",
                error_code="non_json_response",
                retryable=True,
                audit=audit,
            ) from exc
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise CninfoClientError(
                "CNINFO token response did not contain access_token",
                error_code="missing_access_token",
                retryable=False,
                audit=audit,
            )
        return token

    def _request_json_with_retries(
        self,
        *,
        provider_interface: str,
        path: str,
        params: Mapping[str, object],
    ) -> CninfoResponse:
        attempt = 0
        refreshed_after_token_error = False
        while True:
            try:
                response = self._request_json_once(
                    provider_interface=provider_interface,
                    path=path,
                    params=params,
                )
            except CninfoClientError as exc:
                if not exc.retryable or attempt >= self._max_retries:
                    raise
                self._sleep(self._next_delay(attempt))
                attempt += 1
                continue
            resultcode = response.audit.resultcode
            if response.audit.http_status < 400 and resultcode == 200:
                return response
            retryable = _is_retryable(
                http_status=response.audit.http_status, resultcode=resultcode
            )
            if (
                resultcode in TOKEN_REFRESH_RESULT_CODES
                and not refreshed_after_token_error
                and self._access_key
                and self._access_secret
            ):
                self._access_token = self._fetch_token()
                params = {**dict(params), "access_token": self._access_token}
                refreshed_after_token_error = True
                retryable = True
            if not retryable or attempt >= self._max_retries:
                raise CninfoClientError(
                    "CNINFO JSON request failed",
                    error_code=_error_code(
                        http_status=response.audit.http_status, resultcode=resultcode
                    ),
                    retryable=retryable,
                    audit=response.audit,
                )
            self._sleep(self._next_delay(attempt))
            attempt += 1

    def _request_json_once(
        self,
        *,
        provider_interface: str,
        path: str,
        params: Mapping[str, object],
    ) -> CninfoResponse:
        url = f"{BASE_URL}{path}"
        started = time.perf_counter()
        self._bucket.take()
        http_response = self._client.get(url, params=_http_params(params))
        elapsed_ms = _elapsed_ms(started)
        payload = _json_payload(http_response, provider_interface=provider_interface)
        resultcode = _resultcode(payload)
        audit = RequestAudit(
            provider_interface=provider_interface,
            query_params=redact_params(params),
            http_status=http_response.status_code,
            resultcode=resultcode,
            row_count=_row_count(payload),
            elapsed_ms=elapsed_ms,
        )
        self._log_audit(audit)
        return CninfoResponse(payload=payload, audit=audit)

    def _request_bytes_with_retries(
        self,
        *,
        provider_interface: str,
        url: str,
        params: Mapping[str, object],
    ) -> tuple[bytes, RequestAudit]:
        attempt = 0
        while True:
            started = time.perf_counter()
            self._bucket.take()
            response = self._client.get(url, params=_http_params(params))
            audit = RequestAudit(
                provider_interface=provider_interface,
                query_params=redact_params(params),
                http_status=response.status_code,
                resultcode=None,
                row_count=None,
                elapsed_ms=_elapsed_ms(started),
            )
            self._log_audit(audit)
            if response.status_code < 400:
                return response.content, audit
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt >= self._max_retries:
                raise CninfoClientError(
                    "CNINFO download request failed",
                    error_code=f"http_{response.status_code}",
                    retryable=retryable,
                    audit=audit,
                )
            self._sleep(self._next_delay(attempt))
            attempt += 1

    def _next_delay(self, attempt: int) -> float:
        upper = min(
            BACKOFF_CAP_SECONDS,
            BACKOFF_BASE_SECONDS * (BACKOFF_FACTOR**attempt),
        )
        return self._jitter(upper)

    def _log_audit(self, audit: RequestAudit) -> None:
        LOGGER.debug(
            "cninfo request provider_interface=%s http_status=%s resultcode=%s "
            "row_count=%s elapsed_ms=%s query_params=%s",
            audit.provider_interface,
            audit.http_status,
            audit.resultcode,
            audit.row_count,
            audit.elapsed_ms,
            audit.query_params,
        )


def redact_params(params: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in params.items()
        if key.lower() not in SENSITIVE_PARAM_KEYS
    }


def _http_params(params: Mapping[str, object]) -> dict[str, HttpParamValue]:
    converted: dict[str, HttpParamValue] = {}
    for key, value in params.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            converted[key] = value
        else:
            converted[key] = str(value)
    return converted


def _secret_value(value: object) -> str | None:
    if value is None:
        return None
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        return str(get_secret_value())
    return str(value)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _json_payload(response: httpx.Response, *, provider_interface: str) -> dict[str, Any]:
    """Parse a JSON body; non-JSON (e.g. gateway 403 HTML pages) is retryable.

    Observed 2026-07-06: the CNINFO gateway intermittently answers otherwise
    valid requests with an HTML block page, so a non-JSON body means "try
    again", not "bad contract".
    """

    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError as exc:
        raise CninfoClientError(
            f"CNINFO returned a non-JSON body for {provider_interface} "
            f"(http_status={response.status_code})",
            error_code="non_json_response",
            retryable=True,
        ) from exc
    if not isinstance(payload, dict):
        raise CninfoClientError(
            "CNINFO response JSON root must be an object",
            error_code="invalid_json_root",
            retryable=False,
        )
    return payload


def _resultcode(payload: Mapping[str, Any]) -> int | None:
    value = payload.get("resultcode")
    return value if isinstance(value, int) else None


def _row_count(payload: Mapping[str, Any]) -> int | None:
    value = payload.get("count")
    return value if isinstance(value, int) else None


def _is_retryable(*, http_status: int, resultcode: int | None) -> bool:
    if http_status == 429 or http_status >= 500:
        return True
    if http_status >= 400:
        return False
    return resultcode in RETRYABLE_RESULT_CODES


def _error_code(*, http_status: int, resultcode: int | None) -> str:
    if http_status >= 400:
        return f"http_{http_status}"
    return f"resultcode_{resultcode}"
