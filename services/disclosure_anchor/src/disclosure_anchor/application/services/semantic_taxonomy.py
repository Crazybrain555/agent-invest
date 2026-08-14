"""Load the tracked semantic-route vocabulary as a closed application contract."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
import json
from typing import Any

from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_FALLBACK_KEY,
    SemanticRouteContractError,
    SemanticRouteDefinition,
    SemanticRouteTaxonomy,
)


SEMANTIC_TAXONOMY_VERSION = "semantic-taxonomy-2026-08-r35"
_FINANCIAL_RESOURCE = "semantic_financial_routes.v1.json"
_EVENT_RESOURCE = "semantic_event_routes.v1.json"
_PERIODIC_SCOPES = ("annual_report", "semiannual_report", "quarterly_report")


@lru_cache(maxsize=1)
def load_semantic_route_taxonomy() -> SemanticRouteTaxonomy:
    """Return the exact packaged taxonomy; malformed resources fail closed."""

    package = resources.files("disclosure_anchor.application.contracts")
    financial = _json_object(
        package.joinpath(_FINANCIAL_RESOURCE).read_text(encoding="utf-8"),
        label="financial semantic taxonomy",
    )
    events = _json_object(
        package.joinpath(_EVENT_RESOURCE).read_text(encoding="utf-8"),
        label="event semantic taxonomy",
    )
    if set(financial) != {
        "_about",
        "context_container_keys",
        "exclusive_container_keys",
        "keys",
        "version",
    }:
        raise SemanticRouteContractError("financial semantic taxonomy fields drift")
    if set(events) != {
        "_about",
        "entries",
        "exclusive_container_keys",
        "fallback_key",
        "overview_container_keys",
        "quantitative_fact_keys",
        "version",
    }:
        raise SemanticRouteContractError("event semantic taxonomy fields drift")
    if financial.get("version") != "semantic-financial-2026-08-r17":
        raise SemanticRouteContractError("financial semantic taxonomy version drift")
    if events.get("version") != "semantic-events-2026-08-r25":
        raise SemanticRouteContractError("event semantic taxonomy version drift")
    if events.get("fallback_key") != SEMANTIC_FALLBACK_KEY:
        raise SemanticRouteContractError("event semantic fallback key drift")

    definitions: list[SemanticRouteDefinition] = []
    raw_keys = financial.get("keys")
    if not isinstance(raw_keys, dict) or len(raw_keys) != 182:
        raise SemanticRouteContractError(
            "financial semantic taxonomy must contain exactly 182 routes"
        )
    financial_containers = _key_set(
        financial.get("exclusive_container_keys"),
        label="financial exclusive container keys",
    )
    if not financial_containers.issubset(raw_keys):
        raise SemanticRouteContractError(
            "financial exclusive container key is not defined"
        )
    financial_context_containers = _key_set(
        financial.get("context_container_keys"),
        label="financial context container keys",
    )
    if not financial_context_containers.issubset(raw_keys):
        raise SemanticRouteContractError(
            "financial context container key is not defined"
        )
    if financial_containers & financial_context_containers:
        raise SemanticRouteContractError(
            "financial context container cannot be exclusive"
        )
    for key, raw_entry in raw_keys.items():
        if not isinstance(key, str) or not isinstance(raw_entry, dict):
            raise SemanticRouteContractError("financial semantic route is invalid")
        if set(raw_entry) != {"names", "aliases"}:
            raise SemanticRouteContractError(
                f"financial semantic route {key} fields are not closed"
            )
        names = _text_array(raw_entry["names"], label=f"{key} names")
        aliases = _text_array(raw_entry["aliases"], label=f"{key} aliases")
        labels = tuple(dict.fromkeys((*names, *aliases)))
        definitions.append(
            SemanticRouteDefinition(
                key=key,
                description=f"财务披露主题：{names[0]}",
                labels=labels,
                scopes=_PERIODIC_SCOPES,
                exclusive_container=key in financial_containers,
                context_container=key in financial_context_containers,
            )
        )

    raw_entries = events.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise SemanticRouteContractError("event semantic taxonomy entries are invalid")
    event_keys = {
        raw_entry.get("key")
        for raw_entry in raw_entries
        if isinstance(raw_entry, dict)
    }
    event_containers = _key_set(
        events.get("exclusive_container_keys"),
        label="event exclusive container keys",
    )
    event_overviews = _key_set(
        events.get("overview_container_keys"),
        label="event overview container keys",
    )
    event_quantitative_facts = _key_set(
        events.get("quantitative_fact_keys"),
        label="event quantitative fact keys",
    )
    if not event_containers.issubset(event_keys):
        raise SemanticRouteContractError("event exclusive container key is not defined")
    if not event_overviews.issubset(event_keys):
        raise SemanticRouteContractError("event overview container key is not defined")
    if not event_quantitative_facts.issubset(event_keys):
        raise SemanticRouteContractError(
            "event quantitative fact key is not defined"
        )
    if event_containers & event_overviews:
        raise SemanticRouteContractError(
            "event container cannot be both exclusive and overview"
        )
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "key",
            "description",
            "labels",
            "scopes",
        }:
            raise SemanticRouteContractError("event semantic route fields are not closed")
        key = raw_entry["key"]
        description = raw_entry["description"]
        if not isinstance(key, str) or not isinstance(description, str):
            raise SemanticRouteContractError("event semantic route identity is invalid")
        definitions.append(
            SemanticRouteDefinition(
                key=key,
                description=description,
                labels=_text_array(raw_entry["labels"], label=f"{key} labels"),
                scopes=_text_array(raw_entry["scopes"], label=f"{key} scopes"),
                exclusive_container=key in event_containers,
                overview_container=key in event_overviews,
                quantitative_fact=key in event_quantitative_facts,
            )
        )
    return SemanticRouteTaxonomy(
        version=SEMANTIC_TAXONOMY_VERSION,
        definitions=tuple(definitions),
    )


def _json_object(raw: str, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SemanticRouteContractError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SemanticRouteContractError(f"{label} must be an object")
    return payload


def _text_array(payload: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(payload, list) or any(
        not isinstance(item, str) or not item.strip() for item in payload
    ):
        raise SemanticRouteContractError(f"{label} must be a text array")
    return tuple(payload)


def _key_set(payload: object, *, label: str) -> set[str]:
    values = _text_array(payload, label=label)
    if len(values) != len(set(values)):
        raise SemanticRouteContractError(f"{label} repeats a key")
    return set(values)


__all__ = ["SEMANTIC_TAXONOMY_VERSION", "load_semantic_route_taxonomy"]
