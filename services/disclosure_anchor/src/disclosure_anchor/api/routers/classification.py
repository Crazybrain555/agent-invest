"""Classification vocabulary catalog endpoint.

GET /v1/classification exposes the vocabulary behind
documents_v1.disclosure_topics / filing_type: the full class set
(class_map.json) with each class's processing disposition
(processing_policy.json layer-2 default), plus the classification_rule
versions actually loaded in the DB (doctor口径). The route is registered on
the documents router (both concern the same derived classification) so no new
top-level router mount is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.schema import CORE_SCHEMA
from disclosure_anchor.adapters.sources.cninfo.mapper import load_class_map
from disclosure_anchor.api.db import reader_engine_from_request
from disclosure_anchor.api.schemas.public import (
    ClassificationResponse,
    ClassificationRuleSetV1,
    ProcessingClassV1,
    SemanticRouteCatalogResponse,
    SemanticRouteV1,
)
from disclosure_anchor.application.services.semantic_taxonomy import (
    load_semantic_route_taxonomy,
)
from disclosure_anchor.settings import Settings

try:
    from fastapi import Request
except ModuleNotFoundError:  # pragma: no cover - exercised by app-start validation
    Request = None  # type: ignore[assignment, misc]


def get_classification(request: Request) -> ClassificationResponse:
    class_map = load_class_map()
    version, dispositions, note = _policy_dispositions(
        getattr(request.app.state, "settings", None)
    )
    classes = [
        ProcessingClassV1(
            name=str(name),
            zh=(str(spec.get("zh")) if spec.get("zh") is not None else None),
            priority=int(spec["priority"]),
            disposition=(
                dispositions.get(str(name), "unknown_disposition")
                if dispositions is not None
                else "unknown_disposition"
            ),
        )
        for name, spec in class_map["classes"].items()
    ]
    return ClassificationResponse(
        class_map_version=str(class_map["version"]),
        processing_policy_version=version,
        processing_policy_available=dispositions is not None,
        classes=classes,
        rule_sets=_rule_sets(request),
        note=note,
    )


def get_semantic_routes() -> SemanticRouteCatalogResponse:
    """Expose the one vocabulary used by Unit route filters and section keys."""

    taxonomy = load_semantic_route_taxonomy()
    routes = [
        SemanticRouteV1(
            key=definition.key,
            description=definition.description,
            labels=list(definition.labels),
            scopes=list(definition.scopes),
            # _section_keys accepts an exact, scope-valid heading for every
            # exposed taxonomy definition.  Container flags govern inheritance
            # and direct-route behavior; they are not section-key eligibility.
            usable_as_section_key=True,
        )
        for definition in taxonomy.definitions
    ]
    return SemanticRouteCatalogResponse(
        contract_version="semantic_routes_catalog.v1",
        taxonomy_version=taxonomy.version,
        route_count=len(routes),
        routes=routes,
    )


def _rule_sets(request: Request) -> list[ClassificationRuleSetV1]:
    engine = reader_engine_from_request(request)
    sql = (
        "SELECT rule_set, string_agg(DISTINCT version, ',') AS version, "
        "count(*) AS rule_count "
        f"FROM {CORE_SCHEMA}.classification_rule "
        "GROUP BY rule_set ORDER BY rule_set"
    )
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).mappings().all()
    return [
        ClassificationRuleSetV1(
            rule_set=str(row["rule_set"]),
            version=str(row["version"]),
            rule_count=int(row["rule_count"]),
        )
        for row in rows
    ]


def _policy_dispositions(
    settings: Settings | None,
) -> tuple[str | None, dict[str, str] | None, str | None]:
    """Resolve (policy_version, disposition_by_class, note).

    Degrades (returns None dispositions + a note) when settings or the policy
    file are unreachable/malformed — the class set and DB rule versions stay
    available. Only the specific file/parse errors are caught; anything else
    fails loudly (service boundary 7).
    """

    if settings is None:
        return None, None, "settings unavailable; processing dispositions omitted"
    try:
        payload: Any = json.loads(
            Path(settings.disclosure_processing_policy_path).read_text(encoding="utf-8")
        )
        policy_version = str(payload["version"])
        process = [str(name) for name in payload["process"]]
        register_only = [str(name) for name in payload.get("register_only", [])]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return None, None, f"processing_policy unreadable: {exc}"
    dispositions: dict[str, str] = {name: "process" for name in process}
    for name in register_only:
        dispositions[name] = "register_only"
    return policy_version, dispositions, None
