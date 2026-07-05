"""CNINFO client token, retry, rate-limit, and redaction tests."""

from __future__ import annotations

import json
import unittest

import httpx

from disclosure_anchor.adapters.sources.cninfo.client import (
    CninfoClient,
    CninfoClientError,
    TokenBucket,
    redact_params,
)


ACCESS_KEY = "unit-access-key"
ACCESS_SECRET = "unit-access-secret"
ACCESS_TOKEN = "unit-access-token"


class CninfoClientTests(unittest.TestCase):
    def test_token_is_cached_across_requests(self) -> None:
        token_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            if request.url.path.endswith("/oauth2/token"):
                token_calls += 1
                return httpx.Response(200, json={"access_token": ACCESS_TOKEN})
            return httpx.Response(
                200,
                json={"resultcode": 200, "resultmsg": "success", "count": 0, "records": []},
            )

        client = _client(handler)
        for _ in range(3):
            client.get_json(
                provider_interface="cninfo:p_info3015",
                path="/api/info/p_info3015",
                params={"scode": "000001"},
            )

        self.assertEqual(token_calls, 1)

    def test_fetches_token_before_json_request(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": ACCESS_TOKEN})
            return httpx.Response(
                200,
                json={"resultcode": 200, "resultmsg": "success", "count": 0, "records": []},
            )

        client = _client(handler)

        response = client.get_json(
            provider_interface="cninfo:p_info3015",
            path="/api/info/p_info3015",
            params={"scode": "000001"},
        )

        self.assertEqual(response.audit.query_params, {"format": "json", "scode": "000001"})
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].method, "POST")
        self.assertIn("access_token", dict(requests[1].url.params))
        client.close()

    def test_retries_429_with_backoff_parameters(self) -> None:
        attempts = 0
        jitter_caps: list[float] = []
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": ACCESS_TOKEN})
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, json={"resultcode": -1, "resultmsg": "busy"})
            return httpx.Response(
                200,
                json={"resultcode": 200, "resultmsg": "success", "count": 1, "records": [{}]},
            )

        client = _client(
            handler,
            sleep=sleeps.append,
            jitter=lambda upper: _record_jitter(jitter_caps, upper),
        )

        response = client.get_json(
            provider_interface="cninfo:p_info3015",
            path="/api/info/p_info3015",
            params={"scode": "000001"},
        )

        self.assertEqual(response.audit.row_count, 1)
        self.assertEqual(jitter_caps, [1.0])
        self.assertEqual(sleeps, [0.25])
        client.close()

    def test_400_is_not_retryable(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": ACCESS_TOKEN})
            calls += 1
            return httpx.Response(400, json={"resultcode": 402, "resultmsg": "bad param"})

        client = _client(handler)

        with self.assertRaises(CninfoClientError) as raised:
            client.get_json(
                provider_interface="cninfo:p_info3015",
                path="/api/info/p_info3015",
                params={"scode": "000001"},
            )

        self.assertEqual(calls, 1)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.error_code, "http_400")
        client.close()

    def test_token_expiry_resultcode_refreshes_once(self) -> None:
        token_count = 0
        api_tokens: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_count
            if request.url.path.endswith("/oauth2/token"):
                token_count += 1
                return httpx.Response(200, json={"access_token": f"token-{token_count}"})
            api_tokens.append(dict(request.url.params)["access_token"])
            if len(api_tokens) == 1:
                return httpx.Response(
                    200,
                    json={"resultcode": 405, "resultmsg": "token expired", "count": 0},
                )
            return httpx.Response(
                200,
                json={"resultcode": 200, "resultmsg": "success", "count": 0, "records": []},
            )

        client = _client(handler)

        response = client.get_json(
            provider_interface="cninfo:p_info3015",
            path="/api/info/p_info3015",
            params={"scode": "000001"},
        )

        self.assertEqual(response.audit.resultcode, 200)
        self.assertEqual(api_tokens, ["token-1", "token-2"])
        client.close()

    def test_token_bucket_rate_limits_all_requests(self) -> None:
        now = 0.0
        sleeps: list[float] = []

        def clock() -> float:
            return now

        def sleep(seconds: float) -> None:
            nonlocal now
            sleeps.append(seconds)
            now += seconds

        bucket = TokenBucket(max_qps=2, clock=clock, sleep=sleep)

        bucket.take()
        bucket.take()
        bucket.take()

        self.assertEqual(sleeps, [0.5, 0.5])

    def test_secrets_never_logged_or_persisted(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/token"):
                return httpx.Response(200, json={"access_token": ACCESS_TOKEN})
            return httpx.Response(400, json={"resultcode": 402, "resultmsg": "bad param"})

        client = _client(handler)

        with self.assertLogs(
            "disclosure_anchor.adapters.sources.cninfo.client", level="DEBUG"
        ) as logs:
            with self.assertRaises(CninfoClientError) as raised:
                client.get_json(
                    provider_interface="cninfo:p_info3015",
                    path="/api/info/p_info3015",
                    params={
                        "scode": "000001",
                        "access_token": "must-not-persist",
                        "client_id": ACCESS_KEY,
                        "client_secret": ACCESS_SECRET,
                    },
                )

        serialized = json.dumps(
            {
                "logs": logs.output,
                "audit": raised.exception.audit.query_params
                if raised.exception.audit
                else {},
                "error": raised.exception.to_error(stage="index"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn(ACCESS_KEY, serialized)
        self.assertNotIn(ACCESS_SECRET, serialized)
        self.assertNotIn(ACCESS_TOKEN, serialized)
        self.assertNotIn("must-not-persist", serialized)
        self.assertEqual(raised.exception.audit.query_params, {"format": "json", "scode": "000001"})
        client.close()

    def test_redact_params_removes_sensitive_keys_case_insensitively(self) -> None:
        self.assertEqual(
            redact_params(
                {
                    "access_token": "a",
                    "CLIENT_ID": "b",
                    "client_secret": "c",
                    "scode": "000001",
                }
            ),
            {"scode": "000001"},
        )


def _client(
    handler: httpx.MockTransport | httpx.SyncHandler,
    *,
    sleep: object | None = None,
    jitter: object | None = None,
) -> CninfoClient:
    transport = handler if isinstance(handler, httpx.MockTransport) else httpx.MockTransport(handler)
    return CninfoClient(
        access_key=ACCESS_KEY,
        access_secret=ACCESS_SECRET,
        access_token=None,
        max_qps=1000,
        max_retries=3,
        transport=transport,
        bucket=_no_wait_bucket(),
        sleep=sleep,  # type: ignore[arg-type]
        jitter=jitter,  # type: ignore[arg-type]
    )


def _record_jitter(caps: list[float], upper: float) -> float:
    caps.append(upper)
    return 0.25


def _no_wait_bucket() -> TokenBucket:
    tick = 0.0

    def clock() -> float:
        nonlocal tick
        tick += 1.0
        return tick

    return TokenBucket(max_qps=1000, clock=clock, sleep=lambda _: None)


if __name__ == "__main__":
    unittest.main()
