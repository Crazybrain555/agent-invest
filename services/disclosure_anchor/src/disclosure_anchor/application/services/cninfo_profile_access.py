"""Durable CNINFO profile observations shared by every enrichment entrypoint."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import json

from disclosure_anchor.application.ports.disclosure_source import SourceCompanyProfile
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import DisclosureAnchorError, SourceRequestError


CNINFO_PROVIDER = "cninfo"
CNINFO_PROFILE_INTERFACE = "cninfo:p_stock2100"


def add_cninfo_profile_access(
    *,
    uow: UnitOfWork,
    security_code: str,
    profile: SourceCompanyProfile | None,
    accessed_at: datetime,
) -> e.SourceAccess:
    """Persist the exact successful or empty profile response before use."""

    if profile is None:
        status = "warning"
        snapshot: dict[str, object] = {
            "warning": "p_stock2100 profile unavailable"
        }
        error = _json(
            {
                "stage": "profile",
                "error_code": "profile_unavailable",
                "retryable": True,
            }
        )
    else:
        status = "ok"
        snapshot = {"profile": asdict(profile)}
        error = None
    return uow.source_accesses.add(
        e.SourceAccess(
            source_access_id=ids.new_source_access_id(),
            provider=CNINFO_PROVIDER,
            provider_interface=CNINFO_PROFILE_INTERFACE,
            dataset_key="p_stock2100",
            query_params={"scode": security_code},
            accessed_at=accessed_at,
            status=status,
            result_hash=_canonical_sha256(snapshot),
            error=error,
            result_snapshot=snapshot,
        )
    )


def add_failed_cninfo_profile_access(
    *,
    uow: UnitOfWork,
    security_code: str,
    error: DisclosureAnchorError,
    accessed_at: datetime,
) -> e.SourceAccess:
    """Persist a sanitized failed profile attempt without losing its cause."""

    return uow.source_accesses.add(
        e.SourceAccess(
            source_access_id=ids.new_source_access_id(),
            provider=CNINFO_PROVIDER,
            provider_interface=CNINFO_PROFILE_INTERFACE,
            dataset_key="p_stock2100",
            query_params={"scode": security_code},
            accessed_at=accessed_at,
            status="failed",
            error=_json(
                error.to_error(stage="profile")
                if isinstance(error, SourceRequestError)
                else {
                    "stage": "profile",
                    "error_code": type(error).__name__,
                    "retryable": False,
                }
            ),
            result_snapshot={"reason": str(error)},
        )
    )


def _canonical_sha256(payload: dict[str, object]) -> str:
    canonical = _json(payload)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
