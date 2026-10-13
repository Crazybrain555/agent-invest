"""CNINFO WebAPI HTTP client with token, rate-limit, retry, and redaction."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
import logging
import random
import time
from typing import Any

import httpx

from disclosure_anchor.application.ports.disclosure_source import (
    CompletedPdfTransfer,
    PdfDownloadSink,
)
from disclosure_anchor.domain.errors import ConfigurationError, SourceRequestError
from disclosure_anchor.settings import Settings


LOGGER = logging.getLogger(__name__)
HttpParamValue = str | int | float | bool | None

TOKEN_ENDPOINT = "https://webapi.cninfo.com.cn/api-cloud-platform/oauth2/token"
BASE_URL = "https://webapi.cninfo.com.cn"
SENSITIVE_PARAM_KEYS = frozenset({"access_token", "client_id", "client_secret"})
RETRYABLE_RESULT_CODES = frozenset({-1, 403, 404, 405})
# 配额/限流（信封 resultcode=429）：参照 edgartools 对 SEC 429 的处理——请求内
# 立即失败（重试只会烧配额/延长封禁），但 retryable=true 留给下一轮；worker 侧
# 另有轮级熔断（design/watchlist-operations.md §5.3）。
# 430 = daily call-volume wall (probe 2026-07-19: 30/30 flat 430 on the
# listing API at any pacing — a per-day quota, not a rate verdict).
QUOTA_RESULT_CODES = frozenset({407, 408, 412, 430})
QUOTA_ERROR_CODE = "quota_exhausted"
# 429 is a short-window rate verdict (probe 2026-07-18: ~70 calls at 1 rps
# trip it and it clears within minutes) — waiting briefly helps, unlike a
# billing wall.
RATE_LIMIT_RESULT_CODES = frozenset({429})
RATE_LIMIT_ERROR_CODE = "rate_limited"
TOKEN_REFRESH_RESULT_CODES = frozenset({404, 405})
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_FACTOR = 2.0
BACKOFF_CAP_SECONDS = 30.0
# HTTPX applies this to each connect/read/write/pool wait separately; a read
# timeout is the wait for the next chunk, never a whole-response bound.
IO_TIMEOUT_SECONDS = 30.0
DEFAULT_DOWNLOAD_DEADLINE_SECONDS = 1800.0
DOWNLOAD_DEADLINE_ERROR_CODE = "transfer_deadline_exceeded"
# PDFs are fetched as identity: the stored bytes and their hash are exactly
# the framed wire bytes, and no content decoder runs. HTTPX decodes a whole
# network read before any chunking, so a decoder's output has no bound of its
# own; an encoded answer is refused instead (same rule as the V4 ZIP GET).
PDF_DOWNLOAD_HEADERS = {"Accept-Encoding": "identity"}
UNSUPPORTED_CONTENT_ENCODING_ERROR_CODE = "unsupported_content_encoding"


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

    def to_error(
        self, *, stage: str, provider_document_id: str | None = None
    ) -> dict[str, object]:
        payload = super().to_error(
            stage=stage, provider_document_id=provider_document_id
        )
        if self.audit is not None:
            # The raw provider verdict is what separates a rate limit (wait a
            # moment) from a billing wall (waiting is useless).
            payload["resultcode"] = self.audit.resultcode
            payload["http_status"] = self.audit.http_status
        return payload


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


class UnsupportedContentEncoding(Exception):
    """A PDF response applied a content coding despite the identity request."""

    def __init__(self, content_encoding: str) -> None:
        self.content_encoding = content_encoding
        super().__init__(f"content coding {content_encoding!r} instead of identity")


class DownloadDeadline:
    """One monotonic budget for a logical PDF download.

    The token wait, every attempt, the retry backoff and the streamed body all
    spend the same budget; it is never reset per attempt. Synchronous socket
    IO cannot be cancelled, so the bound comes from the blocking waits
    themselves: each request's connect/read/write/pool timeout is clamped to
    ``min(IO_TIMEOUT_SECONDS, remaining)``, the budget is checked after every
    received body chunk and before every retry sleep, and no request starts
    once it is spent. A call can therefore overrun by at most one clamped wait
    (<= 30s) plus one token-bucket interval. Not covered: the response header
    block and chunked-encoding framing, which httpcore reads through repeated
    ``recv`` calls that each obey the clamped timeout without yielding to this
    check, and name resolution inside connect, which only the system resolver
    bounds.
    """

    def __init__(self, seconds: float, *, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._expires_at = clock() + seconds

    def io_timeout(self) -> httpx.Timeout | None:
        """Per-request timeout clamped to the budget; ``None`` once it is spent."""

        remaining = self._expires_at - self._clock()
        if remaining <= 0:
            return None
        return httpx.Timeout(min(IO_TIMEOUT_SECONDS, remaining))

    def allows_sleep(self, seconds: float) -> bool:
        return self._clock() + seconds < self._expires_at

    def stream_body(
        self, response: httpx.Response, sink: PdfDownloadSink
    ) -> CompletedPdfTransfer | None:
        """Stream one identity body into ``sink`` as it arrives.

        ``None`` means the budget ran out before EOF; what this attempt wrote
        stays uncommitted in the sink. ``UnsupportedContentEncoding`` is raised
        before any body byte. A byte count that contradicts the declared
        framing raises ``httpx.RemoteProtocolError``, as the transport itself
        does for a short Content-Length body (RFC 9112 section 6.3).
        """

        if self._clock() >= self._expires_at:
            return None
        declared = _identity_body_length(response)
        sink.begin_attempt(declared_byte_count=declared)
        written = 0
        for chunk in _identity_chunks(response):
            if self._clock() >= self._expires_at:
                return None
            written += len(chunk)
            if declared is not None and written > declared:
                raise httpx.RemoteProtocolError(
                    "PDF body exceeds its declared Content-Length",
                    request=response.request,
                )
            sink.write(chunk)
        # Waiting for EOF can consume the remaining budget without yielding
        # another chunk; completion must satisfy the same deadline.
        if self._clock() >= self._expires_at:
            return None
        if declared is not None and written != declared:
            raise httpx.RemoteProtocolError(
                "PDF body ended before its declared Content-Length",
                request=response.request,
            )
        return CompletedPdfTransfer(byte_count=written, declared_byte_count=declared)


class MemoryPdfSink:
    """In-memory sink behind the byte-returning compatibility calls.

    It buffers a whole body, so the acquisition path never uses it; those
    calls keep the exact transfer semantics of the streaming path.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def begin_attempt(self, *, declared_byte_count: int | None) -> None:
        del declared_byte_count
        self._buffer.clear()

    def write(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    def payload(self) -> bytes:
        return bytes(self._buffer)


def _identity_chunks(response: httpx.Response) -> Iterator[bytes]:
    """Body chunks exactly as framed; no content decoder ever runs."""

    if response.is_stream_consumed:
        # HTTPX reads a Response built from bytes when it is constructed (mock
        # transports do this); with identity coding its content is the body.
        if response.content:
            yield response.content
        return
    # iter_raw, not iter_bytes: every network chunk is one transport read
    # (64 KiB in httpcore's HTTP/1.1), so memory never holds more of the body.
    yield from response.iter_raw()


def _identity_body_length(response: httpx.Response) -> int | None:
    """Declared body length of an identity response; refuse content codings."""

    codings = [
        value.strip().lower()
        for value in response.headers.get_list("content-encoding", split_commas=True)
    ]
    applied = [coding for coding in codings if coding and coding != "identity"]
    if applied:
        raise UnsupportedContentEncoding(", ".join(applied))
    if "transfer-encoding" in response.headers:
        # Transfer-Encoding overrides Content-Length (RFC 9112 section 6.3).
        return None
    lengths = {
        value.strip()
        for value in response.headers.get_list("content-length", split_commas=True)
    }
    if not lengths:
        return None
    length = lengths.pop() if len(lengths) == 1 else ""
    if not (length.isascii() and length.isdigit()):
        raise httpx.RemoteProtocolError(
            "PDF response has an invalid Content-Length", request=response.request
        )
    return int(length)


class AdaptiveTokenBucket:
    """AIMD client-side rate limiter (AWS SDK adaptive-retry-mode style).

    Successes grow the send rate additively; a provider rate verdict halves
    it. Configuration sets only the bounds — the operating point is
    discovered at runtime against the provider's actual tolerance.
    """

    def __init__(
        self,
        *,
        max_qps: float,
        min_qps: float = 0.1,
        initial_qps: float | None = None,
        increase_step_qps: float = 0.05,
        successes_per_increase: int = 10,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if max_qps <= 0:
            raise ValueError("CNINFO max_qps must be greater than zero")
        self._max_qps = max_qps
        self._min_qps = min(min_qps, max_qps)
        self._rate = initial_qps if initial_qps is not None else max(
            self._min_qps, max_qps / 2
        )
        self._rate = min(max(self._rate, self._min_qps), self._max_qps)
        self._increase_step = increase_step_qps
        self._successes_per_increase = successes_per_increase
        self._successes = 0
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._next_available_at = 0.0

    @property
    def current_qps(self) -> float:
        return self._rate

    def take(self) -> None:
        now = self._clock()
        if now < self._next_available_at:
            self._sleep(self._next_available_at - now)
            now = self._clock()
        self._next_available_at = max(now, self._next_available_at) + (
            1.0 / self._rate
        )

    def on_success(self) -> None:
        self._successes += 1
        if self._successes >= self._successes_per_increase:
            self._successes = 0
            self._rate = min(self._max_qps, self._rate + self._increase_step)

    def on_throttle(self) -> None:
        self._successes = 0
        self._rate = max(self._min_qps, self._rate / 2)


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
        download_deadline_seconds: float = DEFAULT_DOWNLOAD_DEADLINE_SECONDS,
        transport: httpx.BaseTransport | None = None,
        bucket: TokenBucket | None = None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[float], float] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("CNINFO max_retries must be non-negative")
        if not download_deadline_seconds > 0:
            raise ValueError("CNINFO download deadline must be greater than zero")
        if not access_token and not (access_key and access_secret):
            raise ConfigurationError(
                "CNINFO credentials require CNINFO_ACCESS_TOKEN or key/secret"
            )
        self._access_key = access_key
        self._access_secret = access_secret
        self._access_token = access_token
        self._max_retries = max_retries
        self._download_deadline_seconds = download_deadline_seconds
        self._clock = clock or time.monotonic
        self._bucket = bucket or AdaptiveTokenBucket(
            max_qps=max_qps, clock=clock, sleep=sleep
        )
        self._sleep = sleep or time.sleep
        self._jitter = jitter or (lambda upper: random.uniform(0.0, upper))
        self._client = httpx.Client(
            transport=transport, timeout=IO_TIMEOUT_SECONDS, trust_env=False
        )

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
            download_deadline_seconds=settings.cninfo_download_deadline_seconds,
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

    def download_to(
        self,
        *,
        provider_interface: str,
        url: str,
        sink: PdfDownloadSink,
        params: Mapping[str, object] | None = None,
    ) -> tuple[CompletedPdfTransfer, RequestAudit]:
        """Stream one logical download into ``sink`` under a single deadline."""

        request_params = dict(params or {})
        deadline = DownloadDeadline(self._download_deadline_seconds, clock=self._clock)
        attempt = 0
        while True:
            started = time.perf_counter()
            self._bucket.take()
            timeout = deadline.io_timeout()
            if timeout is None:
                raise self._download_deadline_error(provider_interface)
            refused: UnsupportedContentEncoding | None = None
            transfer: CompletedPdfTransfer | None = None
            try:
                with self._client.stream(
                    "GET",
                    url,
                    params=_http_params(request_params),
                    headers=PDF_DOWNLOAD_HEADERS,
                    timeout=timeout,
                ) as response:
                    status = response.status_code
                    if status < 400:
                        try:
                            transfer = deadline.stream_body(response, sink)
                        except UnsupportedContentEncoding as exc:
                            refused = exc
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise CninfoClientError(
                        f"CNINFO transport failed for {provider_interface}",
                        error_code="transport_error",
                        retryable=True,
                    ) from exc
                self._sleep_before_download_retry(attempt, deadline, provider_interface)
                attempt += 1
                continue
            audit = RequestAudit(
                provider_interface=provider_interface,
                query_params=redact_params(request_params),
                http_status=status,
                resultcode=None,
                row_count=None,
                elapsed_ms=_elapsed_ms(started),
            )
            self._log_audit(audit)
            if refused is not None:
                raise CninfoClientError(
                    f"CNINFO download for {provider_interface} answered with "
                    f"{refused}",
                    error_code=UNSUPPORTED_CONTENT_ENCODING_ERROR_CODE,
                    retryable=True,
                    audit=audit,
                ) from refused
            if status < 400:
                if transfer is None:
                    raise self._download_deadline_error(provider_interface, audit=audit)
                return transfer, audit
            retryable = status == 429 or status >= 500
            if not retryable or attempt >= self._max_retries:
                raise CninfoClientError(
                    "CNINFO download request failed",
                    error_code=f"http_{status}",
                    retryable=retryable,
                    audit=audit,
                )
            self._sleep_before_download_retry(attempt, deadline, provider_interface)
            attempt += 1

    def download_bytes(
        self,
        *,
        provider_interface: str,
        url: str,
        params: Mapping[str, object] | None = None,
    ) -> tuple[bytes, RequestAudit]:
        """Compatibility call returning the whole body; see ``MemoryPdfSink``."""

        sink = MemoryPdfSink()
        _, audit = self.download_to(
            provider_interface=provider_interface, url=url, sink=sink, params=params
        )
        return sink.payload(), audit

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
        try:
            response = self._client.post(TOKEN_ENDPOINT, data=body)
        except httpx.TransportError as exc:
            raise CninfoClientError(
                "CNINFO token transport failed",
                error_code="transport_error",
                retryable=True,
            ) from exc
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
                on_success = getattr(self._bucket, "on_success", None)
                if on_success is not None:
                    on_success()
                return response
            if resultcode in RATE_LIMIT_RESULT_CODES:
                on_throttle = getattr(self._bucket, "on_throttle", None)
                if on_throttle is not None:
                    on_throttle()
                raise CninfoClientError(
                    f"CNINFO rate limit hit (resultcode {resultcode})",
                    error_code=RATE_LIMIT_ERROR_CODE,
                    retryable=True,
                    audit=response.audit,
                )
            if resultcode in QUOTA_RESULT_CODES:
                raise CninfoClientError(
                    f"CNINFO quota/billing limit reached (resultcode {resultcode})",
                    error_code=QUOTA_ERROR_CODE,
                    retryable=True,
                    audit=response.audit,
                )
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
        try:
            http_response = self._client.get(url, params=_http_params(params))
        except httpx.TransportError as exc:
            raise CninfoClientError(
                f"CNINFO transport failed for {provider_interface}",
                error_code="transport_error",
                retryable=True,
            ) from exc
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

    def _sleep_before_download_retry(
        self, attempt: int, deadline: DownloadDeadline, provider_interface: str
    ) -> None:
        # A backoff that would outlive the download budget cannot lead to a
        # completed download, so the budget verdict is final now.
        delay = self._next_delay(attempt)
        if not deadline.allows_sleep(delay):
            raise self._download_deadline_error(provider_interface)
        self._sleep(delay)

    def _download_deadline_error(
        self, provider_interface: str, *, audit: RequestAudit | None = None
    ) -> CninfoClientError:
        return CninfoClientError(
            f"CNINFO download for {provider_interface} exceeded its "
            f"{self._download_deadline_seconds:g}s deadline",
            error_code=DOWNLOAD_DEADLINE_ERROR_CODE,
            retryable=True,
            audit=audit,
        )

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
