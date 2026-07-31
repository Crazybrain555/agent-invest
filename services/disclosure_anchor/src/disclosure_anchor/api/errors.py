"""Filing API error envelope and contract-version guard."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

from disclosure_anchor.application.use_cases.sync_disclosure_index import (
    CompanyNotTrackedError,
)
from disclosure_anchor.domain.errors import (
    BuildUnitsError,
    ParseDocumentError,
    PublishRunError,
    RawDocumentError,
    RegistrationMetadataError,
    SubjectIdentityConflictError,
)

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
EVIDENCE_INTEGRITY_ERROR = "EVIDENCE_INTEGRITY_ERROR"
# Local-ops admin-only conflict code. NOT part of the public error-code
# 全集 (api/CLAUDE.md) and deliberately kept out of the exported OpenAPI
# ErrorEnvelope enum: the admin write surface is not in the public contract,
# so its operational 409s carry this code in the same envelope shape without
# widening the public read-side vocabulary.
CONFLICT = "CONFLICT"
UNAUTHORIZED = "UNAUTHORIZED"
FORBIDDEN = "FORBIDDEN"


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


def evidence_integrity_error(reason: str) -> NoReturn:
    raise FilingApiError(
        status_code=500,
        error_code=EVIDENCE_INTEGRITY_ERROR,
        message="published evidence failed integrity verification",
        detail={"reason": reason},
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


# Domain error codes (BuildUnitsError/PublishRunError structured .error dict)
# that mean "the referenced entity does not exist" and therefore map to 404.
_NOT_FOUND_DOMAIN_CODES = frozenset({"RUN_NOT_FOUND"})


def _looks_like_not_found(text: str) -> bool:
    return "not found" in text.lower()


def _domain_validation_error(field: str, message: str) -> FilingApiError:
    return FilingApiError(
        status_code=422,
        error_code=VALIDATION_ERROR,
        message="request validation failed",
        detail={"errors": [{"field": field, "message": message}]},
    )


def filing_error_from_domain_error(exc: Exception) -> FilingApiError | None:
    """Translate a use-case domain error into the structured error envelope.

    Admin write endpoints (register-local-pdf/parse/build-units/publish/sync)
    do not catch use-case errors inline; without this an unknown id surfaced as
    a bare FastAPI 500. Only the specific, expected domain errors are mapped —
    anything else returns None so it keeps failing loudly (service boundary 7).
    """

    if isinstance(exc, CompanyNotTrackedError):
        # Message carries the operator guidance ("track it first via …"); pass
        # it through verbatim.
        return FilingApiError(status_code=404, error_code=NOT_FOUND, message=str(exc))
    # SubjectIdentityConflictError is a RegistrationMetadataError subclass;
    # check it first so an identity conflict maps to 409, not 422.
    if isinstance(exc, SubjectIdentityConflictError):
        return FilingApiError(status_code=409, error_code=CONFLICT, message=str(exc))
    if isinstance(exc, RegistrationMetadataError):
        return _domain_validation_error("registration", str(exc))
    if isinstance(exc, RawDocumentError):
        return _domain_validation_error("raw_document", str(exc))
    if isinstance(exc, (BuildUnitsError, PublishRunError)):
        error = exc.error if isinstance(exc.error, dict) else {}
        code = str(error.get("error_code", ""))
        message = str(error.get("message") or exc)
        detail = {"error": error}
        if (
            code in _NOT_FOUND_DOMAIN_CODES
            or _looks_like_not_found(code)
            or (_looks_like_not_found(message))
        ):
            return FilingApiError(
                status_code=404, error_code=NOT_FOUND, message=message, detail=detail
            )
        # EMPTY_RUN / RUN_NOT_SUCCEEDED / UNITS_NOT_BUILT / … — publishable-state
        # conflicts.
        return FilingApiError(
            status_code=409, error_code=CONFLICT, message=message, detail=detail
        )
    if isinstance(exc, ParseDocumentError):
        message = str(exc)
        if _looks_like_not_found(message):
            return FilingApiError(
                status_code=404, error_code=NOT_FOUND, message=message
            )
        return FilingApiError(status_code=409, error_code=CONFLICT, message=message)
    return None


# Base types registered with the app so any subclass raised by an admin write
# endpoint is translated by filing_error_from_domain_error. SubjectIdentity*
# and InvalidRawDocumentError reach these via their base classes.
_DOMAIN_ERROR_TYPES: tuple[type[Exception], ...] = (
    CompanyNotTrackedError,
    RegistrationMetadataError,
    RawDocumentError,
    BuildUnitsError,
    PublishRunError,
    ParseDocumentError,
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
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "message": error["msg"],
            }
            for error in exc.errors()
        ]
        api_error = FilingApiError(
            status_code=422,
            error_code=VALIDATION_ERROR,
            message="request validation failed",
            detail={"errors": errors},
        )
        return JSONResponse(status_code=api_error.status_code, content=api_error.body())

    async def _domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
        api_error = filing_error_from_domain_error(exc)
        if api_error is None:  # pragma: no cover - only registered types reach here
            raise exc
        return JSONResponse(status_code=api_error.status_code, content=api_error.body())

    for exc_type in _DOMAIN_ERROR_TYPES:
        app.add_exception_handler(exc_type, _domain_error_handler)

    @app.middleware("http")
    async def _contract_version_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        requested = request.headers.get("X-Contract-Version")
        if requested is not None and requested not in SUPPORTED_CONTRACT_VERSIONS:
            api_error = contract_version_mismatch(requested)
            return JSONResponse(
                status_code=api_error.status_code, content=api_error.body()
            )
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
                    EVIDENCE_INTEGRITY_ERROR,
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
            if path == "/v1/units/{asset_id}/evidence/{sha256}":
                _replace_error_response(
                    operation,
                    "500",
                    "Published evidence failed integrity verification",
                )


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


def strict_query_params(request: "Request") -> None:
    """Reject unknown query parameters with the standard 422 envelope.

    FastAPI silently ignores undeclared query params — an AI caller passing
    a misspelled filter (e.g. ``content_category`` before it existed) got
    HTTP 200 with UNFILTERED results and no signal that the filter never
    applied (round24). Applied as a router-level dependency on the read
    routers; write/admin and health stay lenient.
    """

    route = request.scope.get("route")
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return
    allowed: set[str] = set()
    stack = [dependant]
    while stack:
        node = stack.pop()
        for param in node.query_params:
            allowed.add(param.alias or param.name)
        stack.extend(node.dependencies)
    unknown = [key for key in request.query_params.keys() if key not in allowed]
    if unknown:
        raise FilingApiError(
            status_code=422,
            error_code=VALIDATION_ERROR,
            message=(
                f"unknown query parameter(s): {', '.join(sorted(unknown))}; "
                f"supported: {', '.join(sorted(allowed))}"
            ),
        )
