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
import math
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
_FILING_TYPE_PREFERENCE_SCORE = 50


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


def _semantic_rows(
    evaluation: Mapping[str, Any],
    *,
    require_query_projection_hash: bool = False,
    require_content_hash: bool = False,
) -> dict[Identity, dict[str, Any]]:
    if evaluation.get("contract_version") != "semantic_route_model_eval.v1":
        raise ValueError("evaluation contract version is unsupported")
    rows: dict[Identity, dict[str, Any]] = {}
    for index, raw in enumerate(
        _array(evaluation.get("rows"), label="evaluation rows")
    ):
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
        query_projection_hash = row.get("query_projection_hash")
        if require_query_projection_hash and (
            not isinstance(query_projection_hash, str)
            or not query_projection_hash.startswith("sha256:")
            or len(query_projection_hash) != 71
        ):
            raise ValueError(
                "evaluation row is missing a canonical query_projection_hash"
            )
        content_hash = row.get("content_hash")
        if require_content_hash and (
            not isinstance(content_hash, str)
            or not content_hash.startswith("sha256:")
            or len(content_hash) != 71
        ):
            raise ValueError("evaluation row is missing a canonical content_hash")
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
               u.contract_version,
               u.content_hash,
               u.query_projection_hash,
               p.title_text,
               p.heading_path_text,
               concat_ws(' ', p.title_tokens, p.path_tokens, p.body_tokens,
                         windows.window_tokens) AS search_tokens,
               coalesce(atoms.atom_text, '') AS atom_text
          FROM disclosure_public.document_units_v2 u
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


def _graded_qrels(payload: object, *, label: str) -> dict[Identity, int]:
    values = _array(payload, label=label)
    qrels: dict[Identity, int] = {}
    for index, raw in enumerate(values):
        item = _array(raw, label=f"{label}[{index}]")
        if (
            len(item) != 3
            or not isinstance(item[0], str)
            or not item[0]
            or type(item[1]) is not int
            or item[1] < 0
            or type(item[2]) is not int
            or not 0 <= item[2] <= 3
        ):
            raise ValueError(f"{label}[{index}] is not a graded Unit identity")
        identity = (item[0], item[1])
        if identity in qrels:
            raise ValueError(f"{label} repeats a Unit identity")
        qrels[identity] = item[2]
    if not qrels or 3 not in qrels.values():
        raise ValueError(f"{label} must contain at least one grade-3 Unit")
    return qrels


def _judged_unit_hashes(
    payload: object,
    *,
    label: str,
) -> dict[Identity, tuple[str, str]]:
    values = _array(payload, label=label)
    hashes: dict[Identity, tuple[str, str]] = {}
    for index, raw in enumerate(values):
        item = _array(raw, label=f"{label}[{index}]")
        if (
            len(item) != 4
            or not isinstance(item[0], str)
            or not item[0]
            or type(item[1]) is not int
            or item[1] < 0
            or not isinstance(item[2], str)
            or not item[2].startswith("sha256:")
            or len(item[2]) != 71
            or not isinstance(item[3], str)
            or not item[3].startswith("sha256:")
            or len(item[3]) != 71
        ):
            raise ValueError(f"{label}[{index}] is not a content-bound Unit identity")
        identity = (item[0], item[1])
        if identity in hashes:
            raise ValueError(f"{label} repeats a Unit identity")
        hashes[identity] = (item[2], item[3])
    if not hashes:
        raise ValueError(f"{label} must not be empty")
    return hashes


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
        + (
            10
            if query_tokens and all(token in stored_tokens for token in query_tokens)
            else 0
        )
    )


def _rank_rows(
    *,
    query: str,
    lexical_queries_any: tuple[str, ...],
    semantic_keys_all: set[str],
    semantic_keys_any: set[str],
    section_keys_any: set[str],
    filing_types_preferred: set[str],
    semantic_rows: Mapping[Identity, Mapping[str, Any]],
    search_rows: Mapping[Identity, Mapping[str, Any]],
    neighbor_radius: int,
    lexical_min_score: int,
    use_direct: bool = True,
    use_section: bool = True,
    use_lexical: bool = True,
    use_neighbors: bool = True,
) -> list[tuple[int, Identity, Mapping[str, Any], tuple[str, ...]]]:
    ranked: dict[Identity, tuple[int, Mapping[str, Any], tuple[str, ...]]] = {}
    for identity, semantic in semantic_rows.items():
        search = search_rows[identity]
        direct_keys = set(
            _text_array(semantic.get("semantic_keys"), label="semantic keys")
        )
        section_keys = set(
            _text_array(semantic.get("section_keys"), label="section keys")
        )
        direct_match = (
            use_direct
            and bool(semantic_keys_all or semantic_keys_any)
            and (not semantic_keys_all or semantic_keys_all <= direct_keys)
            and (not semantic_keys_any or bool(semantic_keys_any & direct_keys))
        )
        section_match = use_section and bool(section_keys_any & section_keys)
        lexical = (
            max(
                _lexical_score(lexical_query, search)
                for lexical_query in lexical_queries_any
            )
            if use_lexical and lexical_queries_any
            else _lexical_score(query, search)
            if use_lexical
            else 0
        )
        if lexical < lexical_min_score:
            lexical = 0
        if not direct_match and not section_match and not lexical:
            continue
        filing_type_preferred = bool(
            filing_types_preferred
            and semantic.get("effective_filing_type") in filing_types_preferred
        )
        score = (
            (200 if direct_match else 0)
            + (100 if section_match else 0)
            + lexical
            + (_FILING_TYPE_PREFERENCE_SCORE if filing_type_preferred else 0)
        )
        reasons = tuple(
            reason
            for reason, present in (
                ("direct", direct_match),
                ("section", section_match),
                ("lexical", bool(lexical)),
                ("filing_type_preference", filing_type_preferred),
            )
            if present
        )
        ranked[identity] = (score, semantic, reasons)

    if use_neighbors and neighbor_radius:
        seed_identities = tuple(ranked)
        for seed in seed_identities:
            for distance in range(1, neighbor_radius + 1):
                for offset in (-distance, distance):
                    unit_index = seed[1] + offset
                    if unit_index < 0:
                        continue
                    identity = (seed[0], unit_index)
                    if identity in ranked or identity not in semantic_rows:
                        continue
                    ranked[identity] = (
                        max(1, 6 - distance),
                        semantic_rows[identity],
                        ("neighbor",),
                    )

    result = [
        (score, identity, semantic, reasons)
        for identity, (score, semantic, reasons) in ranked.items()
    ]
    result.sort(key=lambda item: (-item[0], item[1]))
    return result


def _returned_precision_at(
    ranked: Sequence[Identity],
    qrels: Mapping[Identity, int],
    *,
    k: int,
    minimum_grade: int,
) -> float:
    top = tuple(ranked[:k])
    if not top:
        return 0.0
    return sum(qrels.get(identity, 0) >= minimum_grade for identity in top) / len(top)


def _recall_at(
    ranked: Sequence[Identity],
    qrels: Mapping[Identity, int],
    *,
    k: int,
    minimum_grade: int,
) -> float:
    relevant = {identity for identity, grade in qrels.items() if grade >= minimum_grade}
    if not relevant:
        return 1.0
    return len(relevant & set(ranked[:k])) / len(relevant)


def _ndcg_at(
    ranked: Sequence[Identity],
    qrels: Mapping[Identity, int],
    *,
    k: int,
) -> float:
    def _dcg(grades: Sequence[int]) -> float:
        return float(
            sum(
                ((2**grade) - 1) / math.log2(index + 2)
                for index, grade in enumerate(grades)
            )
        )

    actual = [qrels.get(identity, 0) for identity in ranked[:k]]
    ideal = sorted(qrels.values(), reverse=True)[:k]
    ideal_dcg = _dcg(ideal)
    return _dcg(actual) / ideal_dcg if ideal_dcg else 1.0


def _graded_case_result(
    case: Mapping[str, Any],
    *,
    semantic_rows: Mapping[Identity, Mapping[str, Any]],
    search_rows: Mapping[Identity, Mapping[str, Any]],
) -> dict[str, Any]:
    required = {
        "id",
        "intent",
        "mechanical_forbidden",
        "neighbor_radius",
        "qrels",
        "query",
        "section_keys_any",
        "semantic_keys_all",
    }
    allowed = required | {
        "filing_types_preferred",
        "lexical_operator",
        "lexical_queries_any",
        "lexical_query",
        "semantic_keys_any",
    }
    if not required <= set(case) or not set(case) <= allowed:
        raise ValueError("graded query-gold case fields drifted")
    case_id = case.get("id")
    query = case.get("query")
    intent = case.get("intent")
    neighbor_radius = case.get("neighbor_radius")
    if (
        not isinstance(case_id, str)
        or not case_id
        or not isinstance(query, str)
        or not query.strip()
        or intent not in {"narrow", "broad"}
        or type(neighbor_radius) is not int
        or not 0 <= neighbor_radius <= 2
    ):
        raise ValueError("graded query-gold case controls are invalid")
    semantic_keys_all = set(
        _text_array(case.get("semantic_keys_all"), label="semantic_keys_all")
    )
    semantic_keys_any = set(
        _text_array(case.get("semantic_keys_any", []), label="semantic_keys_any")
    )
    filing_types_preferred = set(
        _text_array(
            case.get("filing_types_preferred", []),
            label="filing_types_preferred",
        )
    )
    observed_filing_types = {
        filing_type
        for semantic in semantic_rows.values()
        if isinstance(
            filing_type := semantic.get("effective_filing_type"),
            str,
        )
        and filing_type
    }
    if not filing_types_preferred <= observed_filing_types:
        raise ValueError("graded query filing_types_preferred is not represented")
    lexical_queries_any = _text_array(
        case.get("lexical_queries_any", []), label="lexical_queries_any"
    )
    if lexical_queries_any and ("lexical_query" in case or "lexical_operator" in case):
        raise ValueError(
            "graded query lexical_queries_any conflicts with lexical_query/operator"
        )
    lexical_operator = case.get("lexical_operator", "phrase")
    if lexical_operator not in {"and", "phrase"}:
        raise ValueError("graded query lexical_operator is invalid")
    lexical_query = case.get("lexical_query", query)
    if not isinstance(lexical_query, str) or not lexical_query.strip():
        raise ValueError("graded query lexical_query is invalid")
    section_keys_any = set(
        _text_array(case.get("section_keys_any"), label="section_keys_any")
    )
    qrels = _graded_qrels(case.get("qrels"), label="qrels")
    mechanical_forbidden = set(
        _identities(case.get("mechanical_forbidden"), label="mechanical_forbidden")
    )
    if set(qrels) & mechanical_forbidden:
        raise ValueError("graded qrels overlap mechanical forbidden Units")

    modes = {
        "full": {},
        "without_direct": {"use_direct": False},
        "without_section": {"use_section": False},
        "without_lexical": {"use_lexical": False},
        "without_neighbors": {"use_neighbors": False},
    }
    rankings: dict[
        str, list[tuple[int, Identity, Mapping[str, Any], tuple[str, ...]]]
    ] = {}
    for mode, overrides in modes.items():
        rankings[mode] = _rank_rows(
            query=lexical_query,
            lexical_queries_any=lexical_queries_any,
            semantic_keys_all=semantic_keys_all,
            semantic_keys_any=semantic_keys_any,
            section_keys_any=section_keys_any,
            filing_types_preferred=filing_types_preferred,
            semantic_rows=semantic_rows,
            search_rows=search_rows,
            neighbor_radius=neighbor_radius,
            lexical_min_score=(
                20 if lexical_queries_any else 10 if lexical_operator == "and" else 20
            ),
            **overrides,
        )

    grades = dict(qrels)
    grades.update((identity, 0) for identity in mechanical_forbidden)
    evaluated_pool = {
        identity
        for ranking in rankings.values()
        for _score, identity, _semantic, _reasons in ranking[:20]
    }
    unjudged = sorted(evaluated_pool - set(grades))
    if unjudged:
        raise ValueError(
            f"graded query {case_id} evaluated pool contains unjudged Units: "
            f"{unjudged[:5]}"
        )

    full = rankings["full"]
    identities = tuple(item[1] for item in full)
    top5 = identities[:5]
    top10 = identities[:10]
    metrics = {
        "success_at_5": any(grades[identity] == 3 for identity in top5),
        "grade3_recall_at_20": round(
            _recall_at(identities, grades, k=20, minimum_grade=3), 6
        ),
        "grade2_recall_at_10": round(
            _recall_at(identities, grades, k=10, minimum_grade=2), 6
        ),
        "grade2_recall_at_20": round(
            _recall_at(identities, grades, k=20, minimum_grade=2), 6
        ),
        "ndcg_at_10": round(_ndcg_at(identities, grades, k=10), 6),
        "returned_precision_at_5": round(
            _returned_precision_at(identities, grades, k=5, minimum_grade=1), 6
        ),
        "returned_precision_at_10": round(
            _returned_precision_at(identities, grades, k=10, minimum_grade=1), 6
        ),
        "grade0_top5": sum(grades[identity] == 0 for identity in top5),
        "mechanical_top10": sum(identity in mechanical_forbidden for identity in top10),
    }
    return {
        "id": case_id,
        "query": query,
        "lexical_query": None if lexical_queries_any else lexical_query,
        "lexical_queries_any": list(lexical_queries_any),
        "filing_types_preferred": sorted(filing_types_preferred),
        "intent": intent,
        "metrics": metrics,
        "ablations": {
            mode: [
                [identity[0], identity[1]]
                for _score, identity, _semantic, _reasons in ranking[:20]
            ]
            for mode, ranking in rankings.items()
            if mode != "full"
        },
        "top": [
            {
                "provider_document_id": identity[0],
                "unit_index": identity[1],
                "grade": grades[identity],
                "score": score,
                "reasons": list(reasons),
                "title": semantic.get("title"),
                "semantic_keys": semantic.get("semantic_keys"),
                "section_keys": semantic.get("section_keys"),
            }
            for score, identity, semantic, reasons in full[:20]
        ],
    }


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
    must_include = set(_identities(case.get("must_include"), label="must_include"))
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
            score = (
                (200 if direct_match else 0) + (100 if section_match else 0) + lexical
            )
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


def _mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("graded query gold is missing an intent class")
    return sum(values) / len(values)


def review(
    *,
    evaluation: Mapping[str, Any],
    gold: Mapping[str, Any],
    search_rows: Mapping[Identity, Mapping[str, Any]],
) -> dict[str, Any]:
    version = gold.get("contract_version")
    if version not in {
        "semantic_retrieval_query_gold.v1",
        "semantic_retrieval_query_gold.v4",
    }:
        raise ValueError("semantic retrieval query gold contract drifted")
    expected_fields = (
        {"_about", "cases", "contract_version"}
        if version == "semantic_retrieval_query_gold.v1"
        else {
            "_about",
            "cases",
            "contract_version",
            "evaluation_id",
            "judged_units",
            "router_version",
            "taxonomy_version",
            "thresholds",
        }
    )
    if set(gold) != expected_fields:
        raise ValueError("semantic retrieval query gold fields drifted")
    require_hashes = version == "semantic_retrieval_query_gold.v4"
    semantic_rows = _semantic_rows(
        evaluation,
        require_query_projection_hash=require_hashes,
        require_content_hash=require_hashes,
    )
    if set(semantic_rows) != set(search_rows):
        raise ValueError("offline semantic rows and public search rows do not align")
    invalid_contracts = sorted(
        identity
        for identity, row in search_rows.items()
        if row.get("contract_version") != "document_unit.v2"
    )
    if invalid_contracts:
        raise ValueError(
            "public search rows are not from document_unit.v2: "
            f"{invalid_contracts[:5]}"
        )
    if require_hashes:
        query_mismatches = [
            identity
            for identity, semantic in semantic_rows.items()
            if semantic.get("query_projection_hash")
            != search_rows[identity].get("query_projection_hash")
        ]
        if query_mismatches:
            raise ValueError(
                "offline semantic rows and public search query hashes do not align: "
                f"{query_mismatches[:5]}"
            )
        content_mismatches = [
            identity
            for identity, semantic in semantic_rows.items()
            if semantic.get("content_hash") != search_rows[identity].get("content_hash")
        ]
        if content_mismatches:
            raise ValueError(
                "offline semantic rows and public Units content hashes do not align: "
                f"{content_mismatches[:5]}"
            )

    cases = [
        _object(raw, label=f"gold cases[{index}]")
        for index, raw in enumerate(_array(gold.get("cases"), label="gold cases"))
    ]
    case_ids = [case.get("id") for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("semantic retrieval gold repeats a case id")

    if version == "semantic_retrieval_query_gold.v1":
        results = [
            _case_result(
                case,
                semantic_rows=semantic_rows,
                search_rows=search_rows,
            )
            for case in cases
        ]
        return {
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

    thresholds = _object(gold.get("thresholds"), label="thresholds")
    expected_thresholds = {
        "broad_returned_precision_at_10",
        "grade2_recall_at_10",
        "grade2_recall_at_20",
        "grade3_recall_at_20",
        "max_grade0_top5",
        "max_mechanical_top10",
        "narrow_returned_precision_at_5",
        "ndcg_at_10",
        "success_at_5",
    }
    if set(thresholds) != expected_thresholds or any(
        type(value) not in {int, float} for value in thresholds.values()
    ):
        raise ValueError("graded query thresholds drifted")
    for field in ("evaluation_id", "taxonomy_version", "router_version"):
        expected = gold.get(field)
        if not isinstance(expected, str) or not expected:
            raise ValueError(f"graded query gold {field} is invalid")
        if evaluation.get(field) != expected:
            raise ValueError(f"graded query gold {field} does not match evaluation")
    judged_unit_hashes = _judged_unit_hashes(
        gold.get("judged_units"),
        label="judged units",
    )
    judged_identities = {
        identity
        for case in cases
        for identity in (
            *tuple(_graded_qrels(case.get("qrels"), label="qrels")),
            *_identities(
                case.get("mechanical_forbidden"), label="mechanical_forbidden"
            ),
        )
    }
    if set(judged_unit_hashes) != judged_identities:
        raise ValueError("judged Unit hashes do not exactly cover qrels and exclusions")
    judgment_mismatches = [
        identity
        for identity, expected_hashes in judged_unit_hashes.items()
        if identity not in semantic_rows
        or semantic_rows[identity].get("query_projection_hash") != expected_hashes[0]
        or search_rows[identity].get("query_projection_hash") != expected_hashes[0]
        or semantic_rows[identity].get("content_hash") != expected_hashes[1]
        or search_rows[identity].get("content_hash") != expected_hashes[1]
    ]
    if judgment_mismatches:
        raise ValueError(
            "judged Unit hashes do not match the evaluated content: "
            f"{judgment_mismatches[:5]}"
        )
    results = [
        _graded_case_result(
            case,
            semantic_rows=semantic_rows,
            search_rows=search_rows,
        )
        for case in cases
    ]
    metrics = {
        "success_at_5": _mean(
            [float(result["metrics"]["success_at_5"]) for result in results]
        ),
        "grade3_recall_at_20": _mean(
            [float(result["metrics"]["grade3_recall_at_20"]) for result in results]
        ),
        "grade2_recall_at_10": _mean(
            [float(result["metrics"]["grade2_recall_at_10"]) for result in results]
        ),
        "grade2_recall_at_20": _mean(
            [float(result["metrics"]["grade2_recall_at_20"]) for result in results]
        ),
        "ndcg_at_10": _mean(
            [float(result["metrics"]["ndcg_at_10"]) for result in results]
        ),
        "narrow_returned_precision_at_5": _mean(
            [
                float(result["metrics"]["returned_precision_at_5"])
                for result in results
                if result["intent"] == "narrow"
            ]
        ),
        "broad_returned_precision_at_10": _mean(
            [
                float(result["metrics"]["returned_precision_at_10"])
                for result in results
                if result["intent"] == "broad"
            ]
        ),
        "max_grade0_top5": max(
            int(result["metrics"]["grade0_top5"]) for result in results
        ),
        "max_mechanical_top10": max(
            int(result["metrics"]["mechanical_top10"]) for result in results
        ),
    }
    rounded_metrics = {
        key: round(value, 6) if isinstance(value, float) else value
        for key, value in metrics.items()
    }
    findings = [
        f"{key}={rounded_metrics[key]} misses threshold {thresholds[key]}"
        for key in sorted(expected_thresholds)
        if (
            rounded_metrics[key] > thresholds[key]
            if key.startswith("max_")
            else rounded_metrics[key] < thresholds[key]
        )
    ]
    return {
        "contract_version": "semantic_retrieval_query_review.v4",
        "evaluation_id": evaluation.get("evaluation_id"),
        "taxonomy_version": evaluation.get("taxonomy_version"),
        "router_version": evaluation.get("router_version"),
        "row_count": len(semantic_rows),
        "case_count": len(results),
        "judgment_hash_bound": True,
        "judgment_content_hash_bound": True,
        "judgment_pool_complete": True,
        "query_hash_bound": True,
        "metrics": rounded_metrics,
        "thresholds": thresholds,
        "passed": not findings,
        "findings": findings,
        "results": results,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evaluation", type=Path)
    parser.add_argument(
        "--gold",
        type=Path,
        default=Path(
            "docs/implementation/checks/semantic-retrieval-query-gold.v4.json"
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    evaluation = _load_json(args.evaluation, label="semantic evaluation")
    gold = _load_json(args.gold, label="semantic retrieval query gold")
    search_rows = _public_search_rows()
    payload = review(evaluation=evaluation, gold=gold, search_rows=search_rows)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered, encoding="utf-8")
        print(
            json.dumps(
                {
                    "contract_version": payload["contract_version"],
                    "output": str(args.output),
                    "passed": payload.get("passed"),
                    "failed": payload.get("failed"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    if payload["contract_version"] == "semantic_retrieval_query_review.v1":
        return 0 if payload["failed"] == 0 else 1
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
