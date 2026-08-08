"""Closed run-bound parse receipt for the current publication lane.

The receipt binds one parse run's immutable input, its closed v2 parser
target, and the resolved endpoint selection into a hashed artifact. The
processing-run row completes the binding topology: ``run.artifact_hash``
covers the NormalizedIR bytes, whose parser-artifact manifest covers the
receipt bytes. Current v2 write/build/publish require the receipt; frozen
v1 generations stay readable without one.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

PARSE_RECEIPT_CONTRACT_VERSION = "parse-receipt.v1"
PARSE_RECEIPT_ARTIFACT_ROLE = "parse_receipt"
_ENDPOINT_DOMAIN_TAG = "disclosure-anchor.parse-receipt.endpoint.v1"

_RECEIPT_FIELDS = frozenset(
    {
        "receipt_contract_version",
        "source_pdf_sha256",
        "parser_target",
        "endpoint",
        "request_profile",
    }
)
_ENDPOINT_FIELDS = frozenset(
    {"server_url", "remote_model_name", "endpoint_selection_sha256"}
)
_REQUEST_PROFILE_FIELDS = frozenset(
    {"http_request_concurrency", "timeout_seconds"}
)


class ParseReceiptContractError(ValueError):
    """A parse receipt is missing, malformed, or contradicts its run."""

    reason_code = "parse_receipt_invalid"


def endpoint_selection_sha256(
    *, server_url: str | None, remote_model_name: str | None
) -> str:
    """Domain-tagged fingerprint of the endpoint selection.

    This closes which server and served model the run selected; it makes
    no claim about the binary, weights, or runtime behind the URL.
    """

    preimage = json.dumps(
        {
            "domain": _ENDPOINT_DOMAIN_TAG,
            "server_url": _normalized_server_url(server_url),
            "remote_model_name": remote_model_name,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def _normalized_server_url(server_url: str | None) -> str | None:
    if server_url is None:
        return None
    return server_url.strip().rstrip("/")


def build_parse_receipt(
    *,
    source_pdf_sha256: str,
    parser_target_payload: Mapping[str, Any],
    server_url: str | None,
    http_request_concurrency: int | None,
    timeout_seconds: int | None,
) -> dict[str, Any]:
    """Construct the closed receipt payload the parser persists."""

    remote_model_name = parser_target_payload.get("remote_model_name")
    backend = parser_target_payload.get("backend")
    if not (isinstance(backend, str) and backend.endswith("-http-client")):
        server_url = None
    return {
        "receipt_contract_version": PARSE_RECEIPT_CONTRACT_VERSION,
        "source_pdf_sha256": source_pdf_sha256,
        "parser_target": dict(parser_target_payload),
        "endpoint": {
            "server_url": _normalized_server_url(server_url),
            "remote_model_name": remote_model_name,
            "endpoint_selection_sha256": endpoint_selection_sha256(
                server_url=server_url,
                remote_model_name=(
                    remote_model_name
                    if isinstance(remote_model_name, str)
                    else None
                ),
            ),
        },
        "request_profile": {
            "http_request_concurrency": http_request_concurrency,
            "timeout_seconds": timeout_seconds,
        },
    }


def validate_parse_receipt(
    payload: Any,
    *,
    source_pdf_sha256: str,
    parser_target_payload: Mapping[str, Any],
) -> None:
    """Replay the receipt semantics against the run's own identity facts.

    Every field set is closed; the endpoint fingerprint is recomputed, and
    the receipt must name exactly the source PDF and parser target the run
    is publishing. Any disagreement fails closed.
    """

    if not isinstance(payload, Mapping):
        raise ParseReceiptContractError("parse receipt must be an object")
    if set(payload) != _RECEIPT_FIELDS:
        raise ParseReceiptContractError("parse receipt fields are not closed")
    if payload["receipt_contract_version"] != PARSE_RECEIPT_CONTRACT_VERSION:
        raise ParseReceiptContractError(
            "parse receipt contract version is unsupported"
        )
    if payload["source_pdf_sha256"] != source_pdf_sha256:
        raise ParseReceiptContractError(
            "parse receipt names a different source PDF"
        )
    target = payload["parser_target"]
    if not isinstance(target, Mapping) or dict(target) != dict(
        parser_target_payload
    ):
        raise ParseReceiptContractError(
            "parse receipt parser target differs from the run target"
        )
    endpoint = payload["endpoint"]
    if not isinstance(endpoint, Mapping) or set(endpoint) != _ENDPOINT_FIELDS:
        raise ParseReceiptContractError(
            "parse receipt endpoint fields are not closed"
        )
    server_url = endpoint["server_url"]
    backend = parser_target_payload.get("backend")
    http_backend = isinstance(backend, str) and backend.endswith(
        "-http-client"
    )
    if http_backend:
        # An HTTP run's receipt must name its endpoint: a normalized,
        # non-empty http(s) URL with no trailing slash.
        if (
            not isinstance(server_url, str)
            or not server_url
            or server_url != _normalized_server_url(server_url)
            or not server_url.lower().startswith(("http://", "https://"))
        ):
            raise ParseReceiptContractError(
                "parse receipt for an HTTP backend requires a normalized "
                f"http(s) server_url, got {server_url!r}"
            )
    elif server_url is not None:
        # Local backends have no endpoint selection to bind.
        raise ParseReceiptContractError(
            "parse receipt for a local backend must carry a null server_url"
        )
    if endpoint["remote_model_name"] != parser_target_payload.get(
        "remote_model_name"
    ):
        raise ParseReceiptContractError(
            "parse receipt endpoint model differs from the parser target"
        )
    expected = endpoint_selection_sha256(
        server_url=server_url,
        remote_model_name=endpoint["remote_model_name"],
    )
    if endpoint["endpoint_selection_sha256"] != expected:
        raise ParseReceiptContractError(
            "parse receipt endpoint fingerprint does not replay"
        )
    profile = payload["request_profile"]
    if not isinstance(profile, Mapping) or set(profile) != (
        _REQUEST_PROFILE_FIELDS
    ):
        raise ParseReceiptContractError(
            "parse receipt request profile fields are not closed"
        )
    for field in sorted(_REQUEST_PROFILE_FIELDS):
        value = profile[field]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ParseReceiptContractError(
                f"parse receipt {field} must be a non-negative integer or null"
            )


__all__ = [
    "PARSE_RECEIPT_ARTIFACT_ROLE",
    "PARSE_RECEIPT_CONTRACT_VERSION",
    "ParseReceiptContractError",
    "build_parse_receipt",
    "endpoint_selection_sha256",
    "validate_parse_receipt",
]
