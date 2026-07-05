"""Filing API error envelope and contract-version guard."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.openapi.utils import get_openapi
    from fastapi.responses import JSONResponse, Response
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    FastAPI = None  # type: ignore[assignment, misc]
    HTTPException = Exception  # type: ignore[assignment, misc]
    Request = None  # type: ignore[assignment, misc]
    RequestValidationError = Exception  # type: ignore[assignment, misc]
    get_openapi = None  # type: ignore[assignment]
    JSONResponse = None  # type: ignore[assignment, misc]
    Response = object  # type: ignore[assignment, misc]


SUPPORTED_CONTRACT_VERSIONS = ("v1",)
ERROR_RESPONSE_REF = {"$ref": "#/components/schemas/ErrorEnvelope"}
CONTRACT_VERSION_HEADER_REF = {"$ref": "#/components/parameters/XContractVersion"}

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

    _install_openapi_contract(app)


def _install_openapi_contract(app: FastAPI) -> None:
    if get_openapi is None:  # pragma: no cover
        return

    def _custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        _add_error_components(schema)
        _apply_operation_contract(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = _custom_openapi  # type: ignore[method-assign]


def _add_error_components(schema: dict[str, Any]) -> None:
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["ErrorEnvelope"] = {
        "type": "object",
        "title": "ErrorEnvelope",
        "additionalProperties": False,
        "required": ["error_code", "message", "detail"],
        "properties": {
            "error_code": {
                "type": "string",
                "enum": [
                    NOT_FOUND,
                    GONE_SUPERSEDED,
                    L1_PROCESSING_REQUIRED,
                    CONTRACT_VERSION_MISMATCH,
                    VALIDATION_ERROR,
                ],
            },
            "message": {"type": "string"},
            "detail": {"type": "object", "additionalProperties": True},
        },
    }
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)
    parameters = components.setdefault("parameters", {})
    parameters["XContractVersion"] = {
        "name": "X-Contract-Version",
        "in": "header",
        "required": False,
        "schema": {"type": "string", "enum": list(SUPPORTED_CONTRACT_VERSIONS)},
        "description": "Supported contract version. Omit to use v1.",
    }


def _apply_operation_contract(schema: dict[str, Any]) -> None:
    for path, operations in schema.get("paths", {}).items():
        if not isinstance(operations, dict) or not path.startswith("/v1/"):
            continue
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            if not isinstance(operation, dict):
                continue
            _add_contract_version_header(operation)
            _replace_error_response(operation, "400", "Contract version mismatch")
            _replace_error_response(operation, "422", "Validation error")
            if "{" in path:
                _replace_error_response(operation, "404", "Not found")
            if path in {
                "/v1/documents/{document_id}",
                "/v1/documents/{document_id}/units",
            }:
                _replace_error_response(operation, "410", "Gone superseded")
            if path == "/v1/documents/{document_id}/units":
                _replace_error_response(operation, "409", "L1 processing required")


def _add_contract_version_header(operation: dict[str, Any]) -> None:
    parameters = operation.setdefault("parameters", [])
    if any(
        isinstance(param, dict)
        and (
            param.get("$ref") == CONTRACT_VERSION_HEADER_REF["$ref"]
            or param.get("name") == "X-Contract-Version"
        )
        for param in parameters
    ):
        return
    parameters.append(CONTRACT_VERSION_HEADER_REF)


def _replace_error_response(
    operation: dict[str, Any], status_code: str, description: str
) -> None:
    responses = operation.setdefault("responses", {})
    responses[status_code] = {
        "description": description,
        "content": {"application/json": {"schema": ERROR_RESPONSE_REF}},
    }
