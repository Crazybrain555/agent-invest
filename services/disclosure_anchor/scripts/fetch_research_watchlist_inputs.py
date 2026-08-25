"""Freeze complete public-source inputs for a research-watchlist generation.

This command is deliberately separate from selection. It records the complete
CNINFO identity payload as deterministic canonical JSON and every Sina/Eastmoney
page as exact response bytes, plus a new-only hash receipt. Credentials are read
from the environment and are never written to the evidence bundle.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, NotRequired, TypedDict

import httpx

from disclosure_anchor.adapters.sources.cninfo.client import CninfoClient


_SCHEMA = "research-watchlist-source-bundle.v1"
_CNINFO_PATH = "/api/stock/p_stock2101"
_SINA_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
_EASTMONEY_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_ANNUAL_YEARS = (2023, 2024, 2025)


class _ResponseReceipt(TypedDict):
    bytes: int
    provider: str
    relpath: str
    request: dict[str, str | int]
    rows: int
    sha256: str
    audit: NotRequired[dict[str, int | str | None]]


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _json_object(payload: bytes, *, source: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{source} did not return valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{source} JSON root must be an object")
    return parsed


def _json_rows(payload: bytes, *, source: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{source} did not return valid UTF-8 JSON") from exc
    if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
        raise RuntimeError(f"{source} JSON root must be a list of objects")
    return parsed


def _eastmoney_rows(
    payload: bytes, *, year: int, page: int
) -> tuple[list[dict[str, Any]], int]:
    source = f"Eastmoney {year} page {page}"
    parsed = _json_object(payload, source=source)
    if parsed.get("success") is not True:
        raise RuntimeError(f"{source} returned failure: {parsed.get('message')!r}")
    result = parsed.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{source} result must be an object")
    rows = result.get("data")
    pages = result.get("pages")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(f"{source} result.data must be a list of objects")
    if not isinstance(pages, int) or pages < 1:
        raise RuntimeError(f"{source} result.pages must be a positive integer")
    return rows, pages


def _write_response(
    root: Path,
    *,
    relpath: str,
    payload: bytes,
    provider: str,
    rows: int,
    request: Mapping[str, str | int],
) -> _ResponseReceipt:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {
        "bytes": len(payload),
        "provider": provider,
        "relpath": relpath,
        "request": dict(request),
        "rows": rows,
        "sha256": _sha256(payload),
    }


def _fetch_cninfo(stage: Path) -> _ResponseReceipt:
    client = CninfoClient(
        access_key=os.getenv("CNINFO_ACCESS_KEY"),
        access_secret=os.getenv("CNINFO_ACCESS_SECRET"),
        access_token=os.getenv("CNINFO_ACCESS_TOKEN"),
        max_qps=1.0,
        max_retries=3,
    )
    try:
        response = client.get_json(
            provider_interface="p_stock2101",
            path=_CNINFO_PATH,
            params={"@limit": 20_000},
        )
    finally:
        client.close()
    payload = response.payload
    rows = payload.get("records")
    if payload.get("resultcode") != 200 or not isinstance(rows, list):
        raise RuntimeError(
            "CNINFO p_stock2101 did not return a successful records list"
        )
    if payload.get("total") != len(rows) or len(rows) < 5_000:
        raise RuntimeError(
            "CNINFO p_stock2101 returned an incomplete full-market response"
        )
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    item = _write_response(
        stage,
        relpath="cninfo/p-stock2101.json",
        payload=encoded,
        provider="CNINFO p_stock2101",
        rows=len(rows),
        request={"@limit": 20_000, "format": "json"},
    )
    item["audit"] = {
        "http_status": response.audit.http_status,
        "resultcode": response.audit.resultcode,
        "row_count": response.audit.row_count,
    }
    return item


def _fetch_sina(stage: Path, client: httpx.Client) -> list[_ResponseReceipt]:
    receipts: list[_ResponseReceipt] = []
    total_rows = 0
    for page in range(1, 101):
        params: dict[str, str | int] = {
            "_s_r_a": "page",
            "asc": 1,
            "node": "hs_a",
            "num": 100,
            "page": page,
            "sort": "symbol",
            "symbol": "",
        }
        response = client.get(_SINA_URL, params=params)
        response.raise_for_status()
        rows = _json_rows(response.content, source=f"Sina hs_a page {page}")
        receipts.append(
            _write_response(
                stage,
                relpath=f"sina/page-{page:04d}.json",
                payload=response.content,
                provider="Sina Market Center hs_a",
                rows=len(rows),
                request=params,
            )
        )
        total_rows += len(rows)
        if not rows:
            break
    else:
        raise RuntimeError("Sina hs_a did not terminate within 100 pages")
    if total_rows < 5_000:
        raise RuntimeError(f"Sina hs_a full-market response is too small: {total_rows}")
    return receipts


def _fetch_eastmoney(stage: Path, client: httpx.Client) -> list[_ResponseReceipt]:
    receipts: list[_ResponseReceipt] = []
    for year in _ANNUAL_YEARS:
        expected_pages: int | None = None
        page = 1
        while expected_pages is None or page <= expected_pages:
            params: dict[str, str | int] = {
                "columns": "ALL",
                "filter": f"(REPORTDATE='{year}-12-31')",
                "pageNumber": page,
                "pageSize": 500,
                "reportName": "RPT_LICO_FN_CPD",
                "sortColumns": "SECURITY_CODE,REPORTDATE",
                "sortTypes": "1,-1",
            }
            response = client.get(_EASTMONEY_URL, params=params)
            response.raise_for_status()
            rows, pages = _eastmoney_rows(response.content, year=year, page=page)
            if expected_pages is None:
                expected_pages = pages
            elif pages != expected_pages:
                raise RuntimeError(f"Eastmoney {year} page count changed during fetch")
            receipts.append(
                _write_response(
                    stage,
                    relpath=f"eastmoney/{year}/page-{page:04d}.json",
                    payload=response.content,
                    provider="Eastmoney RPT_LICO_FN_CPD",
                    rows=len(rows),
                    request=params,
                )
            )
            page += 1
    return receipts


def _freeze_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def fetch_bundle(output: Path) -> dict[str, Any]:
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}.", dir=output.parent
    ) as tmp:
        stage = Path(tmp) / "bundle"
        stage.mkdir()
        identity = _fetch_cninfo(stage)
        with httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": "agent-invest disclosure-anchor evidence fetch"},
            timeout=60.0,
            trust_env=False,
        ) as client:
            quotes = _fetch_sina(stage, client)
            annual = _fetch_eastmoney(stage, client)
        receipt: dict[str, Any] = {
            "completed_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "credentials_persisted": False,
            "identity": identity,
            "requests": [*quotes, *annual],
            "schema": _SCHEMA,
            "started_at_utc": started_at,
            "summary": {
                "annual_rows": sum(item["rows"] for item in annual),
                "cninfo_rows": identity["rows"],
                "quote_rows": sum(item["rows"] for item in quotes),
            },
        }
        receipt_bytes = (
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        (stage / "source-fetch-receipt.json").write_bytes(receipt_bytes)
        stage.replace(output)
    _freeze_tree(output)
    return {
        "output": str(output),
        "receipt_sha256": _sha256((output / "source-fetch-receipt.json").read_bytes()),
        **receipt["summary"],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    result = fetch_bundle(_parse_args().output)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
