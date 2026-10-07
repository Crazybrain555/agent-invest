"""Explicit processing requests: the agent API's only write route."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from disclosure_anchor.api.agent_auth import require_agent_read, require_agent_request
from disclosure_anchor.api.errors import strict_query_params, validation_error
from disclosure_anchor.api.schemas.agent import (
    ProcessingRequestIn,
    ProcessingRequestOut,
    ProcessingRequestWaitOut,
)
from disclosure_anchor.application.contracts.processing_request import (
    REQUEST_ID_RE,
    normalize_intent,
)
from disclosure_anchor.application.use_cases.processing_requests import (
    MAX_WAIT_SECONDS,
    ProcessingRequestNotFound,
    ProcessingRequestService,
)

try:
    from fastapi import APIRouter, Depends, Request, Response
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    APIRouter = None  # type: ignore[assignment, misc]
    Depends = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment, misc]
    Response = None  # type: ignore[assignment, misc]


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_SINCE_PREFIX = "sha256:"


def _service(request: Request) -> ProcessingRequestService:
    service = getattr(request.app.state, "processing_requests", None)
    if not isinstance(service, ProcessingRequestService):
        raise RuntimeError("processing request service is not configured")
    return service


def submit_processing_request(
    request: Request, response: Response, body: ProcessingRequestIn
) -> dict[str, Any]:
    service = _service(request)
    today = datetime.now(SHANGHAI_TZ).date()
    intent = normalize_intent(body.model_dump(mode="json"), today=today)
    view = service.submit(intent)
    response.status_code = 200 if view.replayed else 201
    return view.to_json()


def get_processing_request(request: Request, request_id: str) -> dict[str, Any]:
    _require_request_id(request_id)
    return _service(request).get(request_id).to_json()


def wait_processing_request(
    request: Request,
    request_id: str,
    timeout_s: int = MAX_WAIT_SECONDS,
    since: str | None = None,
) -> dict[str, Any]:
    _require_request_id(request_id)
    if isinstance(timeout_s, bool) or not 1 <= timeout_s <= MAX_WAIT_SECONDS:
        raise validation_error("timeout_s", f"must be 1-{MAX_WAIT_SECONDS}")
    if since is not None and (
        not since.startswith(_SINCE_PREFIX)
        or len(since) != len(_SINCE_PREFIX) + 64
        or any(char not in "0123456789abcdef" for char in since[len(_SINCE_PREFIX):])
    ):
        raise validation_error("since", "must be a status_sha256 value")
    outcome = _service(request).wait(request_id, timeout_s=timeout_s, since=since)
    body = outcome.view.to_json()
    body["wait"] = {"outcome": outcome.outcome, "waited_ms": outcome.waited_ms}
    return body


def _require_request_id(request_id: str) -> None:
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise ProcessingRequestNotFound(f"processing request {request_id} not found")


router: Any
if APIRouter is not None:
    router = APIRouter(dependencies=[Depends(strict_query_params)])
    router.add_api_route(
        "/v1/processing-requests",
        submit_processing_request,
        methods=["POST"],
        response_model=ProcessingRequestOut,
        response_model_exclude_unset=True,
        status_code=201,
        dependencies=[Depends(require_agent_request)],
    )
    router.add_api_route(
        "/v1/processing-requests/{request_id}",
        get_processing_request,
        methods=["GET"],
        response_model=ProcessingRequestOut,
        response_model_exclude_unset=True,
        dependencies=[Depends(require_agent_read)],
    )
    router.add_api_route(
        "/v1/processing-requests/{request_id}/wait",
        wait_processing_request,
        methods=["GET"],
        response_model=ProcessingRequestWaitOut,
        response_model_exclude_unset=True,
        dependencies=[Depends(require_agent_read)],
    )
else:  # pragma: no cover
    router = None
