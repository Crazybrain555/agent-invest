"""Authenticated agent-only GET; inventory never submits or resolves work."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import SQLAlchemyError

from disclosure_anchor.api.agent_auth import require_agent_read
from disclosure_anchor.api.errors import FilingApiError, strict_query_params, validation_error
from disclosure_anchor.api.schemas.disclosure_inventory import DisclosureInventoryOut
from disclosure_anchor.application.contracts.disclosure_inventory import InventoryQueryInvalid
from disclosure_anchor.application.use_cases.read_disclosure_inventory import ReadDisclosureInventory


def get_disclosure_inventory(
    request: Request, status: str | None = None, security_code: str | None = None,
    limit: int = 50, cursor: str | None = None,
) -> dict[str, Any]:
    service = getattr(request.app.state, "disclosure_inventory", None)
    if not isinstance(service, ReadDisclosureInventory):
        raise FilingApiError(status_code=503, error_code="SERVICE_UNAVAILABLE", message="Inventory reader is unavailable")
    try:
        return service.read(status=status, security_code=security_code, limit=limit, cursor=cursor)
    except InventoryQueryInvalid as exc:
        raise validation_error(exc.field_name, exc.message) from exc
    except SQLAlchemyError as exc:
        raise FilingApiError(status_code=503, error_code="SERVICE_UNAVAILABLE", message="Inventory snapshot is unavailable") from exc


def _inventory_query_params(request: Request) -> None:
    strict_query_params(request)
    for key in request.query_params:
        if len(request.query_params.getlist(key)) != 1:
            raise validation_error(key, "query parameter must not be repeated")


router = APIRouter(dependencies=[Depends(require_agent_read), Depends(_inventory_query_params)])
router.add_api_route(
    "/v1/disclosure-inventory", get_disclosure_inventory, methods=["GET"],
    response_model=DisclosureInventoryOut,
)
