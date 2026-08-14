#!/usr/bin/env python3
"""Review an offline semantic replay against current public lexical projections.

This is a read-only acceptance tool.  It models the planned L2/L3 retrieval
union: explicit catalog route filters first, normalized section routes second,
and the existing source-bound lexical projection as fallback/tie-breaker.
It does not infer route keys from natural language and is not a public search
endpoint or production ranking implementation.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from typing import Any, cast

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
)
from disclosure_anchor.adapters.retrieval import tokenizer
from disclosure_anchor.settings import load_settings


Identity = tuple[str, int]


def _object(payload: object, *, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object")
    return payload


def _array(payload: object, *, label: str) -> list[object]:
    if not isinstance(payload, list):
        raise ValueError(f"{label} must be an array")
    return payload


def _text_array(payload: object, *, label: str) -> tuple[str, ...]:
    values = _array(payload, label=label)
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"{label} must contain non-empty strings")
    result = tuple(cast(str, value) for value in values)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} repeats a value")
    return result


def _identities(payload: object, *, label: str) -> tuple[Identity, ...]:
    values = _array(payload, label=label)
    identities: list[Identity] = []
    for index, raw in enumerate(values):
        pair = _array(raw, label=f"{label}[{index}]")
        if (
            len(pair) != 2
            or not isinstance(pair[0], str)
            or not pair[0]
            or type(pair[1]) is not int
            or pair[1] < 0
        ):
            raise ValueError(f"{label}[{index}] is not a Unit identity")
        identities.append((pair[0], pair[1]))
    if len(identities) != len(set(identities)):
        raise ValueError(f"{label} repeats a Unit identity")
    return tuple(identities)


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")), label=label)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON") from exc


def _semantic_rows(evaluation: Mapping[str, Any]) -> dict[Identity, dict[str, Any]]:
    if evaluation.get("contract_version") != "semantic_route_model_eval.v1":
        raise ValueError("evaluation contract version is unsupported")
    rows: dict[Identity, dict[str, Any]] = {}
    for index, raw in enumerate(_array(evaluation.get("rows"), label="evaluation rows")):
        row = _object(raw, label=f"evaluation rows[{index}]")
        provider_document_id = row.get("provider_document_id")
        unit_index = row.get("unit_index")
        if (
            not isinstance(provider_document_id, str)
            or type(unit_index) is not int
            or unit_index < 0
        ):
            raise ValueError("evaluation row has an invalid Unit identity")
        identity = (provider_document_id, unit_index)
        if identity in rows:
            raise ValueError("evaluation repeats a Unit identity")
        _text_array(row.get("semantic_keys"), label="semantic keys")
        _text_array(row.get("section_keys"), label="section keys")
        rows[identity] = row
    if not rows or len(rows) != evaluation.get("row_count"):
        raise ValueError("evaluation row count drifted")
    return rows


def _public_search_rows() -> dict[Identity, dict[str, Any]]:
    engine = create_db_engine(app_database_url(load_settings()))
    sql = text(
        """
        WITH windows AS (
            SELECT asset_id,
                   string_agg(body_tokens, ' ' ORDER BY window_index) AS window_tokens
              FROM disclosure_public.unit_body_search_windows_v1
             GROUP BY asset_id
        ),
        atoms AS (
            SELECT asset_id,
                   string_agg(atom_text, ' ' ORDER BY atom_index) AS atom_text
              FROM disclosure_public.unit_search_atoms_v1
             GROUP BY asset_id
        )
        SELECT u.provider_document_id,
               u.order_index - 1 AS unit_index,
               p.title_text,
               p.heading_path_text,
               concat_ws(' ', p.title_tokens, p.path_tokens, p.body_tokens,
                         windows.window_tokens) AS search_tokens,
               coalesce(atoms.atom_text, '') AS atom_text
          FROM disclosure_public.document_units_v1 u
          JOIN disclosure_public.unit_search_projection_v1 p USING (asset_id)
          LEFT JOIN windows USING (asset_id)
          LEFT JOIN atoms USING (asset_id)
         WHERE u.is_active_run
        """
    )
    with engine.connect() as connection:
        raw_rows = connection.execute(sql).mappings().all()
    rows: dict[Identity, dict[str, Any]] = {}
    for raw in raw_rows:
        row = dict(raw)
        identity = (str(row["provider_document_id"]), int(row["unit_index"]))
        if identity in rows:
            raise ValueError("public search projection repeats a Unit identity")
        rows[identity] = row
    return rows


def _lexical_score(query: str, row: Mapping[str, Any]) -> int:
    normalized = tokenizer.normalize_search_text(query)
    title = tokenizer.normalize_search_text(str(row.get("title_text") or ""))
    path = tokenizer.normalize_search_text(str(row.get("heading_path_text") or ""))
    atom_text = tokenizer.normalize_search_text(str(row.get("atom_text") or ""))
    stored_tokens = str(row.get("search_tokens") or "")
    query_tokens = tokenizer.query_word_tokens(query)
    return (
        (60 if normalized in title else 0)
        + (30 if normalized in path else 0)
        + (20 if normalized in atom_text else 0)
        + (10 if query_tokens and all(token in stored_tokens for token in query_tokens) else 0)
    )


def _case_result(
    case: Mapping[str, Any],
    *,
    semantic_rows: Mapping[Identity, Mapping[str, Any]],
    search_rows: Mapping[Identity, Mapping[str, Any]],
) -> dict[str, Any]:
    allowed = {
        "forbidden",
        "id",
        "min_precision",
        "must_include",
        "query",
        "relevant",
        "review_k",
        "section_keys_any",
        "semantic_keys_all",
    }
    if set(case) != allowed:
        raise ValueError("query-gold case fields drifted")
    case_id = case.get("id")
    query = case.get("query")
    review_k = case.get("review_k")
    min_precision = case.get("min_precision")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("query-gold case controls are invalid")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query-gold case controls are invalid")
    if type(review_k) is not int or not 1 <= review_k <= 10:
        raise ValueError("query-gold case controls are invalid")
    if type(min_precision) not in {int, float}:
        raise ValueError("query-gold case controls are invalid")
    precision_floor = float(cast(int | float, min_precision))
    if not 0 <= precision_floor <= 1:
        raise ValueError("query-gold case controls are invalid")
    semantic_keys_all = set(
        _text_array(case.get("semantic_keys_all"), label="semantic_keys_all")
    )
    section_keys_any = set(
        _text_array(case.get("section_keys_any"), label="section_keys_any")
    )
    relevant = set(_identities(case.get("relevant"), label="relevant"))
    must_include = set(
        _identities(case.get("must_include"), label="must_include")
    )
    forbidden = set(_identities(case.get("forbidden"), label="forbidden"))
    if not relevant or not must_include <= relevant or relevant & forbidden:
        raise ValueError("query-gold relevance sets are inconsistent")

    ranked: list[tuple[int, Identity, Mapping[str, Any]]] = []
    for identity, semantic in semantic_rows.items():
        search = search_rows[identity]
        direct_keys = set(
            _text_array(semantic.get("semantic_keys"), label="semantic keys")
        )
        section_keys = set(
            _text_array(semantic.get("section_keys"), label="section keys")
        )
        direct_match = bool(semantic_keys_all) and semantic_keys_all <= direct_keys
        section_match = bool(section_keys_any & section_keys)
        lexical = _lexical_score(query, search)
        if direct_match or section_match or lexical:
            score = (200 if direct_match else 0) + (100 if section_match else 0) + lexical
            ranked.append((score, identity, semantic))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    top = ranked[:review_k]
    top_identities = tuple(item[1] for item in top)
    precision = (
        sum(identity in relevant for identity in top_identities) / len(top_identities)
        if top_identities
        else 0.0
    )
    findings: list[str] = []
    missing = sorted(must_include - set(top_identities))
    forbidden_hits = sorted(forbidden & set(top_identities))
    if missing:
        findings.append(f"missing required Units: {missing}")
    if forbidden_hits:
        findings.append(f"forbidden Units ranked in top {review_k}: {forbidden_hits}")
    if precision < precision_floor:
        findings.append(
            f"precision@{review_k} {precision:.3f} is below {precision_floor:.3f}"
        )
    return {
        "id": case_id,
        "query": query,
        "review_k": review_k,
        "precision": round(precision, 6),
        "passed": not findings,
        "findings": findings,
        "top": [
            {
                "provider_document_id": identity[0],
                "unit_index": identity[1],
                "score": score,
                "title": semantic.get("title"),
                "semantic_keys": semantic.get("semantic_keys"),
                "section_keys": semantic.get("section_keys"),
            }
            for score, identity, semantic in top
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evaluation", type=Path)
    parser.add_argument(
        "--gold",
        type=Path,
        default=Path(
            "docs/implementation/checks/semantic-retrieval-query-gold.v1.json"
        ),
    )
    args = parser.parse_args(argv)
    evaluation = _load_json(args.evaluation, label="semantic evaluation")
    gold = _load_json(args.gold, label="semantic retrieval query gold")
    if set(gold) != {"_about", "cases", "contract_version"} or gold.get(
        "contract_version"
    ) != "semantic_retrieval_query_gold.v1":
        raise ValueError("semantic retrieval query gold contract drifted")
    semantic_rows = _semantic_rows(evaluation)
    search_rows = _public_search_rows()
    if set(semantic_rows) != set(search_rows):
        raise ValueError("offline semantic rows and public search rows do not align")
    cases = [
        _object(raw, label=f"gold cases[{index}]")
        for index, raw in enumerate(_array(gold.get("cases"), label="gold cases"))
    ]
    results = [
        _case_result(case, semantic_rows=semantic_rows, search_rows=search_rows)
        for case in cases
    ]
    payload = {
        "contract_version": "semantic_retrieval_query_review.v1",
        "evaluation_id": evaluation.get("evaluation_id"),
        "taxonomy_version": evaluation.get("taxonomy_version"),
        "router_version": evaluation.get("router_version"),
        "row_count": len(semantic_rows),
        "case_count": len(results),
        "passed": sum(bool(result["passed"]) for result in results),
        "failed": sum(not bool(result["passed"]) for result in results),
        "results": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
