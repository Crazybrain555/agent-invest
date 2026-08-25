#!/usr/bin/env python3
"""Compare an offline Unit replay with the active public Unit generation.

The emitted receipt is self-describing and falsifiable: it binds the replay
file by hash, names every compared field, records the active processing-run
generation, and includes canonical aggregate hashes for both sides.  A zero
mismatch count is therefore not accepted as evidence unless the two row-set
hashes are also identical.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Literal, cast

from sqlalchemy import bindparam, text

from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
)
from disclosure_anchor.settings import load_settings


Identity = tuple[str, int]
COMPARED_FIELDS = (
    "provider_document_id",
    "unit_index",
    "title",
    "heading_path",
    "semantic_keys",
    "section_keys",
    "content_hash",
    "query_projection_hash",
    "body_status",
    "applicability",
)
_HASH_FIELDS = {"content_hash", "query_projection_hash"}
_ARRAY_FIELDS = {"heading_path", "semantic_keys", "section_keys"}
_BODY_STATUSES = {"content", "heading_only", "empty"}
_APPLICABILITY_VALUES = {None, "applicable", "not_applicable"}
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
NORMALIZATIONS = {
    "semantic_keys": "database null is the public absence form and canonicalizes to []",
    "section_keys": "database null is the public absence form and canonicalizes to []",
}


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_text_array(
    value: object, *, field: str, nullable_empty: bool = False
) -> list[str]:
    if value is None and nullable_empty:
        return []
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{field} must be an array of non-empty strings")
    result = list(cast(Sequence[str], value))
    if field != "heading_path" and len(result) != len(set(result)):
        raise ValueError(f"{field} repeats a key")
    return result


def canonical_unit_row(
    raw: Mapping[str, object],
    *,
    label: str,
    source: Literal["replay", "live"],
) -> dict[str, Any]:
    missing_fields = [field for field in COMPARED_FIELDS if field not in raw]
    if missing_fields:
        raise ValueError(f"{label} is missing compared fields: {missing_fields!r}")

    provider_document_id = raw["provider_document_id"]
    unit_index = raw["unit_index"]
    if (
        not isinstance(provider_document_id, str)
        or not provider_document_id
        or type(unit_index) is not int
        or unit_index < 0
    ):
        raise ValueError(f"{label} has an invalid Unit identity")

    title = raw["title"]
    if title is not None and not isinstance(title, str):
        raise ValueError(f"{label}.title must be text or null")

    row: dict[str, Any] = {
        "provider_document_id": provider_document_id,
        "unit_index": unit_index,
        "title": title,
    }
    for field in _ARRAY_FIELDS:
        row[field] = _canonical_text_array(
            raw[field],
            field=f"{label}.{field}",
            nullable_empty=source == "live" and field in NORMALIZATIONS,
        )
    for field in _HASH_FIELDS:
        value = raw[field]
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            raise ValueError(f"{label}.{field} is not a canonical SHA-256")
        row[field] = value
    body_status = raw["body_status"]
    if not isinstance(body_status, str) or body_status not in _BODY_STATUSES:
        raise ValueError(f"{label}.body_status is unsupported")
    row["body_status"] = body_status
    applicability = raw["applicability"]
    if applicability not in _APPLICABILITY_VALUES:
        raise ValueError(f"{label}.applicability is unsupported")
    row["applicability"] = applicability
    return {field: row[field] for field in COMPARED_FIELDS}


def _row_map(
    rows: Sequence[Mapping[str, object]],
    *,
    label: str,
    source: Literal["replay", "live"],
) -> dict[Identity, dict[str, Any]]:
    result: dict[Identity, dict[str, Any]] = {}
    for index, raw in enumerate(rows):
        row = canonical_unit_row(
            raw,
            label=f"{label}[{index}]",
            source=source,
        )
        identity = (row["provider_document_id"], row["unit_index"])
        if identity in result:
            raise ValueError(f"{label} repeats Unit identity {identity!r}")
        result[identity] = row
    if not result:
        raise ValueError(f"{label} is empty")
    return result


def _aggregate(rows: Mapping[Identity, Mapping[str, object]]) -> str:
    ordered = [rows[identity] for identity in sorted(rows)]
    return _sha256_bytes(
        _canonical_json(
            {
                "compared_fields": list(COMPARED_FIELDS),
                "rows": ordered,
            }
        )
    )


def replay_provider_scope(
    replay_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Bind a complete-document live query to the providers present in replay."""

    rows = _row_map(replay_rows, label="replay rows", source="replay")
    provider_document_ids = sorted({identity[0] for identity in rows})
    for provider_document_id in provider_document_ids:
        unit_indices = sorted(
            identity[1]
            for identity in rows
            if identity[0] == provider_document_id
        )
        if unit_indices != list(range(len(unit_indices))):
            raise ValueError(
                "replay provider scope must contain contiguous unit_index values "
                f"from zero for {provider_document_id!r}"
            )
    return {
        "type": "replay_provider_documents",
        "provider_document_ids": provider_document_ids,
        "provider_document_count": len(provider_document_ids),
        "provider_document_ids_sha256": _sha256_bytes(
            _canonical_json(provider_document_ids)
        ),
        "completeness_rule": (
            "replay unit_index is contiguous from zero for every provider; "
            "live query reads every active Unit for the complete provider set"
        ),
    }


def audit_rows(
    *,
    replay_rows: Sequence[Mapping[str, object]],
    live_rows: Sequence[Mapping[str, object]],
) -> dict[str, Any]:
    expected = _row_map(replay_rows, label="replay rows", source="replay")
    actual = _row_map(live_rows, label="live rows", source="live")
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    mismatches: list[dict[str, object]] = []
    for identity in sorted(set(expected) & set(actual)):
        differing_fields = [
            field
            for field in COMPARED_FIELDS
            if expected[identity][field] != actual[identity][field]
        ]
        if differing_fields:
            mismatches.append(
                {
                    "identity": list(identity),
                    "differing_fields": differing_fields,
                    "replay_row_sha256": _sha256_bytes(
                        _canonical_json(expected[identity])
                    ),
                    "live_row_sha256": _sha256_bytes(
                        _canonical_json(actual[identity])
                    ),
                    "replay_values": {
                        field: expected[identity][field]
                        for field in differing_fields
                    },
                    "live_values": {
                        field: actual[identity][field]
                        for field in differing_fields
                    },
                }
            )

    replay_aggregate = _aggregate(expected)
    live_aggregate = _aggregate(actual)
    passed = (
        not missing
        and not unexpected
        and not mismatches
        and replay_aggregate == live_aggregate
    )
    return {
        "passed": passed,
        "compared_fields": list(COMPARED_FIELDS),
        "normalizations": NORMALIZATIONS,
        "replay_row_count": len(expected),
        "live_row_count": len(actual),
        "replay_aggregate_sha256": replay_aggregate,
        "live_aggregate_sha256": live_aggregate,
        "missing": [list(identity) for identity in missing],
        "unexpected": [list(identity) for identity in unexpected],
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
    }


def load_replay(
    path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    replay_bytes = path.read_bytes()
    try:
        payload = json.loads(replay_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("replay is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("replay is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("replay root must be an object")
    if payload.get("contract_version") != "semantic_route_model_eval.v1":
        raise ValueError("replay contract version is unsupported")
    rows = payload.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("replay rows must be an array of objects")
    row_count = payload.get("row_count")
    if type(row_count) is not int or row_count != len(rows):
        raise ValueError("replay row_count drifted")
    return payload, cast(list[dict[str, Any]], rows), _sha256_bytes(replay_bytes)


def _live_rows(
    provider_document_ids: Sequence[str],
) -> tuple[list[dict[str, Any]], dict[str, object]]:
    if not provider_document_ids or len(provider_document_ids) != len(
        set(provider_document_ids)
    ):
        raise ValueError("live provider scope must be non-empty and unique")
    engine = create_db_engine(app_database_url(load_settings()))
    sql = text(
        """
        SELECT provider_document_id,
               order_index - 1 AS unit_index,
               processing_run_id::text AS processing_run_id,
               title,
               heading_path,
               semantic_keys,
               section_keys,
               content_hash,
               query_projection_hash,
               body_status,
               applicability,
               contract_version
          FROM disclosure_public.document_units_v1
         WHERE is_active_run
           AND provider_document_id IN :provider_document_ids
         ORDER BY provider_document_id, order_index
        """
    ).bindparams(bindparam("provider_document_ids", expanding=True))
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            transaction = dict(
                connection.execute(
                    text(
                        """
                        SELECT current_setting('transaction_isolation')
                                   AS transaction_isolation,
                               current_setting('transaction_read_only')
                                   AS transaction_read_only,
                               txid_current_snapshot()::text
                                   AS transaction_snapshot
                        """
                    )
                )
                .mappings()
                .one()
            )
            transaction_isolation = str(transaction["transaction_isolation"])
            transaction_read_only = str(transaction["transaction_read_only"])
            snapshot = str(transaction["transaction_snapshot"])
            if (
                transaction_isolation != "repeatable read"
                or transaction_read_only != "on"
            ):
                raise ValueError(
                    "live Unit audit transaction is not repeatable-read/read-only"
                )
            raw_rows = [
                dict(row)
                for row in connection.execute(
                    sql,
                    {"provider_document_ids": list(provider_document_ids)},
                ).mappings()
            ]
    finally:
        engine.dispose()
    contracts = sorted({str(row["contract_version"]) for row in raw_rows})
    if contracts != ["document_unit.v1"]:
        raise ValueError(f"live Unit contracts are unsupported: {contracts!r}")
    run_ids = sorted({str(row["processing_run_id"]) for row in raw_rows})
    processing_runs = sorted(
        {
            (
                str(row["provider_document_id"]),
                str(row["processing_run_id"]),
            )
            for row in raw_rows
        }
    )
    runs_by_provider: dict[str, list[str]] = {}
    for provider_document_id, processing_run_id in processing_runs:
        runs_by_provider.setdefault(provider_document_id, []).append(
            processing_run_id
        )
    ambiguous_runs = {
        provider_document_id: values
        for provider_document_id, values in runs_by_provider.items()
        if len(values) != 1
    }
    if ambiguous_runs:
        raise ValueError(
            "live provider scope contains multiple active processing runs: "
            f"{ambiguous_runs!r}"
        )
    return raw_rows, {
        "public_view": "disclosure_public.document_units_v1",
        "active_only": True,
        "transaction_isolation": transaction_isolation,
        "transaction_read_only": True,
        "transaction_snapshot": snapshot,
        "contract_versions": contracts,
        "processing_run_ids": run_ids,
        "processing_runs": [
            {
                "provider_document_id": provider_document_id,
                "processing_run_id": processing_run_id,
            }
            for provider_document_id, processing_run_id in processing_runs
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    replay_payload, replay_rows, replay_sha256 = load_replay(args.replay)
    provider_scope = replay_provider_scope(replay_rows)
    live_rows, live_metadata = _live_rows(
        cast(list[str], provider_scope["provider_document_ids"])
    )
    comparison = audit_rows(replay_rows=replay_rows, live_rows=live_rows)
    receipt = {
        "contract_version": "live_unit_replay_audit.v2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_revision": args.source_revision,
        "source_replay": {
            "path": str(args.replay.resolve()),
            "sha256": replay_sha256,
            "contract_version": replay_payload["contract_version"],
            "evaluation_id": replay_payload.get("evaluation_id"),
            "taxonomy_version": replay_payload.get("taxonomy_version"),
            "router_version": replay_payload.get("router_version"),
            "row_count": replay_payload["row_count"],
        },
        "provider_scope": provider_scope,
        "live_generation": live_metadata,
        "comparison": comparison,
    }
    args.output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0 if comparison["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
