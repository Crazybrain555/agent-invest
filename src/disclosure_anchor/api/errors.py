"""Filing API error envelope and contract-version guard."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse, Response
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    FastAPI = None  # type: ignore[assignment, misc]
    HTTPException = Exception  # type: ignore[assignment, misc]
    Request = None  # type: ignore[assignment, misc]
    RequestValidationError = Exception  # type: ignore[assignment, misc]
    JSONResponse = None  # type: ignore[assignment, misc]
    Response = object  # type: ignore[assignment, misc]


SUPPORTED_CONTRACT_VERSIONS = ("v1",)

NOT_FOUND = "NOT_FOUND"
GONE_SUPERSEDED = "GONE_SUPERSEDED"
L1_PROCESSING_REQUIRED = "L1_PROCESSING_REQUIRED"
CONTRACT_VERSION_MISMATCH = "CONTRACT_VERSION_MISMATCH"
VALIDATION_ERROR = "VALIDATION_ERROR"


class FilingApiError(HTTPException):
    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.error_code = error_code
        self.message = message
        self.error_detail = detail or {}
        super().__init__(status_code=status_code, detail=self.body())

    def body(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "message": self.message,
            "detail": self.error_detail,
        }


def not_found(message: str = "resource not found") -> NoReturn:
    raise FilingApiError(status_code=404, error_code=NOT_FOUND, message=message)


def gone_superseded(superseded_by: str) -> NoReturn:
    raise FilingApiError(
        status_code=410,
        error_code=GONE_SUPERSEDED,
        message="document has been superseded",
        detail={"superseded_by": superseded_by},
    )


def l1_processing_required(status: str) -> NoReturn:
    raise FilingApiError(
        status_code=409,
        error_code=L1_PROCESSING_REQUIRED,
        message="L1 processing is required before units can be read",
        detail={"status": status},
    )


def validation_error(field: str, message: str) -> FilingApiError:
    return FilingApiError(
        status_code=422,
        error_code=VALIDATION_ERROR,
        message="request validation failed",
        detail={"errors": [{"field": field, "message": message}]},
    )


def contract_version_mismatch(requested: str) -> FilingApiError:
    return FilingApiError(
        status_code=400,
        error_code=CONTRACT_VERSION_MISMATCH,
        message="unsupported contract version",
        detail={"requested": requested, "supported": list(SUPPORTED_CONTRACT_VERSIONS)},
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(FilingApiError)
    async def _filing_api_error_handler(
        request: Request, exc: FilingApiError
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body())

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
            for error in exc.errors()
        ]
        api_error = FilingApiError(
            status_code=422,
            error_code=VALIDATION_ERROR,
            message="request validation failed",
            detail={"errors": errors},
        )
        return JSONResponse(status_code=api_error.status_code, content=api_error.body())

    @app.middleware("http")
    async def _contract_version_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        requested = request.headers.get("X-Contract-Version")
        if requested is not None and requested not in SUPPORTED_CONTRACT_VERSIONS:
            api_error = contract_version_mismatch(requested)
            return JSONResponse(status_code=api_error.status_code, content=api_error.body())
        return await call_next(request)
