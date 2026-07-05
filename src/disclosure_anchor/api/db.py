"""FastAPI request helpers for database-backed routers."""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Engine

try:
    from fastapi import HTTPException, Request
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    HTTPException = None  # type: ignore[assignment, misc]
    Request = None  # type: ignore[assignment, misc]


def reader_engine_from_request(request: Request) -> Engine:
    engine = getattr(request.app.state, "reader_db_engine", None)
    if engine is None:
        if HTTPException is None:  # pragma: no cover
            raise RuntimeError("reader database engine is not configured")
        raise HTTPException(status_code=503, detail="reader database engine is not configured")
    return engine


def app_state_value(request: Request, name: str) -> Any:
    return getattr(request.app.state, name, None)
