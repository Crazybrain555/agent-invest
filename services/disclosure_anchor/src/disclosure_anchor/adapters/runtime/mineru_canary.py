"""DB-free MinerU multimodal canary and cache identity."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from disclosure_anchor.adapters.runtime.bounded_http import (
    BoundedHTTPProtocolError,
    BoundedHTTPTransportError,
    ThreadOwnedPersistentHTTPClient,
)


CANARY_SCHEMA = "mineru_multimodal_canary.v2"
CANARY_EXPECTED_TEXT = "M7"
_CANARY_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAGAAAAAwCAIAAABhdOiYAAADIElEQVR42u2aP0h6URTH"
    "v9mvp/EqoiC0wYhoC4LAIQiLIEg0oj9mNVRbW1MuYWhDQUU01xTVpBBphUJEQ1sQ"
    "NNTQHhQNJdXLXmL3N1wQqZQbv997opzv9ODec57vwzn3nHuwhDEGUnYZCAEBIkAE"
    "iAARIAJEgAgQiQARIAJEgAgQASJABOifVJKh7e3t3Jvdbnd689TU1I9OcktrQH80"
    "9R4KhSYmJrKtvr29RaPRH5daW1tzuE2lUldXVwC6u7s1DyH2v8Xdms1mAJIkxePx"
    "bDtDoRAAq9XKTSYnJwVfsbGxwaPs4uKCaSytzqCuri5Zlj8+PsLhcI74AjAwMPAr"
    "z4qi+P1+AOPj421tbYV6SBuNRofDASAYDP64IZFIHB0dARgcHPyV55WVlfv7e6PR"
    "uLi4WNhVbGhoCMDx8XE8Hv++Go1GFUWpq6vr6OgQ93l3d7e2tgZgZmamoaGhsAG5"
    "XC6TyZQty3h+DQ8PGwy/+A3z8/OKotTU1MzNzRV8H1RRUeF0OtMsMvX+/n54eAhg"
    "ZGRE3OH19fXW1hYAn89XXV2tUyOkURXjJYkfQJIkPT09Ze7Z29sDYLFYUqnUF5Mc"
    "6u3tBdDY2KiqKtNL2nbSLpersrLye5bxmHK73eL5dXJyEovFACwtLUmSVCRXjfLy"
    "8v7+/i+1TFVVnl8ej0fQz+fn5+zsLACbzSZuVRh3sbGxsS+1LBaLvby8WK3W9vZ2"
    "QSc7OzuXl5cAVldXdbhe6Aqop6entra2mUzu7+9n5pfH4xH81EQi4fP5APT19XV2"
    "dhbbbb6srIw3RJyLqqoHBwcARkdHBT2sr6/f3t6WlpYuLy/n4TqvaRXjOj095aQe"
    "Hx8jkQiA5ubm3CZpPTw8VFVVAZienmb5kB7zILvdXl9fn0wmw+FwOr8EbQOBwPPz"
    "syzLgUCgaAdmBoOBE9nd3eURJJhfNzc3m5ubALxeLx8PFGeKMcbOz8/Tb2xpaREx"
    "YYzxFsFsNr++vrI8SaeRq81ma2pq4s+C4XN2dsbby4WFBVmW8zVyLaH/KNLQngAR"
    "IAJEgAgQASJAJAJEgAiQ7voLQALBt0Fva2UAAAAASUVORK5CYII="
)
_MAX_RESPONSE_BYTES = 1024 * 1024
_RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class MinerUCanaryError(RuntimeError):
    """The remote OpenAI-compatible multimodal path failed closed."""


class MinerUCanaryUnavailableError(MinerUCanaryError):
    """The remote model endpoint was temporarily unreachable."""


class _CanaryHTTPStatusError(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


@dataclass(frozen=True)
class MinerUCanaryEvidence:
    model_id: str
    attempts: int
    request_sha256: str
    response_sha256: tuple[str, ...]

    def cache_payload(
        self, *, observability_url: str, runtime_bundle_identity_sha256: str
    ) -> dict[str, Any]:
        return {
            "schema": CANARY_SCHEMA,
            "passed_at_utc": datetime.now(UTC).isoformat(),
            "observability_endpoint_sha256": _sha256(
                observability_url.rstrip("/").encode("utf-8")
            ),
            "runtime_bundle_identity_sha256": runtime_bundle_identity_sha256,
            "model_id_sha256": model_id_sha256(self.model_id),
            "attempts": self.attempts,
            "request_sha256": self.request_sha256,
            "response_sha256": list(self.response_sha256),
        }


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def model_id_sha256(model_id: str) -> str:
    if not model_id:
        raise ValueError("served MinerU model identity is empty")
    return _sha256(model_id.encode("utf-8"))


def canary_request_sha256(model_id: str) -> str:
    return _sha256(_canary_request_bytes(model_id))


def probe_mineru_served_model(
    server_url: str,
    *,
    expected_model_id: str | None = None,
    timeout_seconds: float = 15,
) -> str:
    """Read the singleton served-model identity without an OCR request."""

    client: ThreadOwnedPersistentHTTPClient | None = None
    try:
        client = _direct_client(server_url)
        model_id = _read_served_model(
            client,
            timeout_seconds=timeout_seconds,
        )
    except _CanaryHTTPStatusError as exc:
        if exc.status in _RETRYABLE_HTTP_STATUS_CODES:
            raise MinerUCanaryUnavailableError(str(exc)) from exc
        raise MinerUCanaryError(
            f"served-model endpoint returned non-retryable HTTP {exc.status}"
        ) from exc
    except BoundedHTTPTransportError as exc:
        raise MinerUCanaryUnavailableError(str(exc)) from exc
    except (
        BoundedHTTPProtocolError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise MinerUCanaryError(str(exc)) from exc
    finally:
        if client is not None:
            client.close()
    if expected_model_id is not None and model_id != expected_model_id:
        raise MinerUCanaryError(
            "served MinerU model identity drifted from attestation"
        )
    return model_id


def run_mineru_multimodal_canary(
    server_url: str,
    *,
    attempts: int = 1,
    expected_model_id: str | None = None,
) -> MinerUCanaryEvidence:
    if attempts < 1:
        raise ValueError("canary attempts must be positive")
    client: ThreadOwnedPersistentHTTPClient | None = None
    try:
        client = _direct_client(server_url)
        model_id = _read_served_model(client, timeout_seconds=15)
        if expected_model_id is not None and model_id != expected_model_id:
            raise ValueError("served MinerU model identity drifted from attestation")
        request_bytes = _canary_request_bytes(model_id)
        response_hashes: list[str] = []
        for _ in range(attempts):
            status, completion_bytes = client.post_bytes(
                "/chat/completions",
                request_bytes,
                content_type="application/json",
                timeout_seconds=90,
                transport_attempts=1,
            )
            if status != 200:
                raise _CanaryHTTPStatusError(status)
            completion_payload = json.loads(completion_bytes)
            if not isinstance(completion_payload, dict):
                raise ValueError("multimodal canary response root must be an object")
            choices = completion_payload.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("multimodal canary returned no unique choice")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ValueError("multimodal canary choice must be an object")
            message = choice.get("message")
            if not isinstance(message, dict):
                raise ValueError("multimodal canary returned no assistant message")
            content = message.get("content")
            normalized_content = (
                content.strip().removesuffix(".")
                if isinstance(content, str)
                else None
            )
            if (
                message.get("role") != "assistant"
                or normalized_content != CANARY_EXPECTED_TEXT
            ):
                raise ValueError(
                    "multimodal canary did not OCR the exact expected M7 token"
                )
            response_hashes.append(_sha256(completion_bytes))
    except (
        _CanaryHTTPStatusError,
        BoundedHTTPProtocolError,
        BoundedHTTPTransportError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise MinerUCanaryError(str(exc)) from exc
    finally:
        if client is not None:
            client.close()
    return MinerUCanaryEvidence(
        model_id=model_id,
        attempts=attempts,
        request_sha256=_sha256(request_bytes),
        response_sha256=tuple(response_hashes),
    )


def _api_root(server_url: str) -> str:
    api_root = server_url.rstrip("/")
    if not api_root.endswith("/v1"):
        api_root += "/v1"
    return api_root


def _direct_client(server_url: str) -> ThreadOwnedPersistentHTTPClient:
    return ThreadOwnedPersistentHTTPClient(
        _api_root(server_url),
        maximum_response_bytes=_MAX_RESPONSE_BYTES,
        user_agent="disclosure-anchor-mineru-canary/1",
    )


def _read_served_model(
    client: ThreadOwnedPersistentHTTPClient,
    *,
    timeout_seconds: float,
) -> str:
    status, payload = client.get_bytes(
        "/models",
        timeout_seconds=timeout_seconds,
        transport_attempts=1,
    )
    if status != 200:
        raise _CanaryHTTPStatusError(status)
    models_payload = json.loads(payload)
    if not isinstance(models_payload, dict):
        raise ValueError("served-model response root must be an object")
    models = models_payload.get("data")
    if not isinstance(models, list) or len(models) != 1:
        raise ValueError("expected exactly one served MinerU model")
    model = models[0]
    if not isinstance(model, dict):
        raise ValueError("served MinerU model entry must be an object")
    model_id = model.get("id")
    if not isinstance(model_id, str) or not model_id:
        raise ValueError("served MinerU model identity is invalid")
    return model_id


def _canary_request_bytes(model_id: str) -> bytes:
    if not model_id:
        raise ValueError("served MinerU model identity is empty")
    request_payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Read the black text in the image and reply "
                            "with exactly those two characters, nothing else."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": _CANARY_PNG_DATA_URL},
                    },
                ],
            }
        ],
        "max_tokens": 8,
        "temperature": 0,
    }
    return json.dumps(
        request_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canary_cache_is_fresh(
    payload: object,
    *,
    observability_url: str,
    runtime_bundle_identity_sha256: str,
    max_age_seconds: int,
    now: datetime | None = None,
) -> bool:
    if not isinstance(payload, dict) or payload.get("schema") != CANARY_SCHEMA:
        return False
    if max_age_seconds < 0:
        return False
    if payload.get("observability_endpoint_sha256") != _sha256(
        observability_url.rstrip("/").encode("utf-8")
    ):
        return False
    request_sha256 = payload.get("request_sha256")
    model_sha256 = payload.get("model_id_sha256")
    if not all(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
        for value in (request_sha256, model_sha256)
    ):
        return False
    if (
        payload.get("runtime_bundle_identity_sha256")
        != runtime_bundle_identity_sha256
    ):
        return False
    attempts = payload.get("attempts")
    responses = payload.get("response_sha256")
    if (
        isinstance(attempts, bool)
        or not isinstance(attempts, int)
        or attempts < 1
        or not isinstance(responses, list)
        or len(responses) != attempts
        or not all(
            isinstance(item, str)
            and len(item) == 64
            and all(character in "0123456789abcdef" for character in item)
            for item in responses
        )
    ):
        return False
    passed_at_raw = payload.get("passed_at_utc")
    if not isinstance(passed_at_raw, str):
        return False
    try:
        passed_at = datetime.fromisoformat(passed_at_raw)
    except ValueError:
        return False
    if passed_at.tzinfo is None:
        return False
    current = now or datetime.now(UTC)
    age = (current - passed_at.astimezone(UTC)).total_seconds()
    return 0 <= age <= max_age_seconds


__all__ = [
    "CANARY_EXPECTED_TEXT",
    "CANARY_SCHEMA",
    "MinerUCanaryError",
    "MinerUCanaryUnavailableError",
    "MinerUCanaryEvidence",
    "canary_request_sha256",
    "canary_cache_is_fresh",
    "model_id_sha256",
    "probe_mineru_served_model",
    "run_mineru_multimodal_canary",
]
