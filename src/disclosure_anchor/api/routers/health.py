"""Health endpoint."""

from __future__ import annotations

from disclosure_anchor import __version__
from disclosure_anchor.api.schemas.health import HealthResponse

from typing import Any

try:
    from fastapi import APIRouter
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    APIRouter = None  # type: ignore[assignment, misc]


def health_payload() -> HealthResponse:
    return HealthResponse(status="ok", service="disclosure_anchor", version=__version__)


def get_health() -> HealthResponse:
    return health_payload()


router: Any
if APIRouter is not None:
    router = APIRouter()
    router.add_api_route("/v1/health", get_health, methods=["GET"], response_model=HealthResponse)
else:
    router = None
