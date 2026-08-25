"""Load the tracked semantic-route vocabulary as a closed application contract."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
import json
from typing import Any

from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_FALLBACK_KEY,
    SemanticRouteContractError,
    SemanticCompositeSection,
    SemanticRouteDefinition,
    SemanticRouteTaxonomy,
)


SEMANTIC_TAXONOMY_VERSION = "semantic-taxonomy-2026-08-r64"
_FINANCIAL_RESOURCE = "semantic_financial_routes.v1.json"
_EVENT_RESOURCE = "semantic_event_routes.v1.json"
_PERIODIC_SCOPES = ("annual_report", "semiannual_report", "quarterly_report")
_ALLOWED_FINANCIAL_EVENT_SCOPE_EXTENSIONS = frozenset({"risk_alert"})


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
        "composite_direct_labels",
        "composite_context_labels",
        "context_container_keys",
        "event_scope_extensions",
        "exclusive_container_keys",
        "keys",
        "quantitative_topic_keys",
        "version",
    }:
        raise SemanticRouteContractError("financial semantic taxonomy fields drift")
    if set(events) != {
        "_about",
        "entries",
        "exclusive_container_keys",
        "fallback_key",
        "overview_container_keys",
        "quantitative_topic_keys",
        "role_anchor_keys",
        "section_container_keys",
        "version",
    }:
        raise SemanticRouteContractError("event semantic taxonomy fields drift")
    if financial.get("version") != "semantic-financial-2026-08-r34":
        raise SemanticRouteContractError("financial semantic taxonomy version drift")
    if events.get("version") != "semantic-events-2026-08-r49":
        raise SemanticRouteContractError("event semantic taxonomy version drift")
    if events.get("fallback_key") != SEMANTIC_FALLBACK_KEY:
        raise SemanticRouteContractError("event semantic fallback key drift")

    definitions: list[SemanticRouteDefinition] = []
    raw_keys = financial.get("keys")
    if not isinstance(raw_keys, dict) or len(raw_keys) != 198:
        raise SemanticRouteContractError(
            "financial semantic taxonomy must contain exactly 198 routes"
        )
    raw_scope_extensions = financial.get("event_scope_extensions")
    if not isinstance(raw_scope_extensions, dict) or not set(
        raw_scope_extensions
    ).issubset(raw_keys):
        raise SemanticRouteContractError(
            "financial event scope extensions are invalid"
        )
    financial_scope_extensions: dict[str, tuple[str, ...]] = {}
    for key, raw_scopes in raw_scope_extensions.items():
        scopes = _text_array(raw_scopes, label=f"{key} event scope extensions")
        if len(scopes) != len(set(scopes)) or not set(scopes).issubset(
            _ALLOWED_FINANCIAL_EVENT_SCOPE_EXTENSIONS
        ):
            raise SemanticRouteContractError(
                f"financial event scope extensions for {key} are invalid"
            )
        financial_scope_extensions[key] = scopes
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
    financial_quantitative_topics = _key_set(
        financial.get("quantitative_topic_keys"),
        label="financial quantitative topic keys",
    )
    if not financial_quantitative_topics.issubset(raw_keys):
        raise SemanticRouteContractError(
            "financial quantitative topic key is not defined"
        )
    for key, raw_entry in raw_keys.items():
        if not isinstance(key, str) or not isinstance(raw_entry, dict):
            raise SemanticRouteContractError("financial semantic route is invalid")
        if set(raw_entry) not in (
            {"names", "aliases"},
            {"names", "aliases", "heading_aliases"},
        ):
            raise SemanticRouteContractError(
                f"financial semantic route {key} fields are not closed"
            )
        names = _text_array(raw_entry["names"], label=f"{key} names")
        aliases = _text_array(raw_entry["aliases"], label=f"{key} aliases")
        heading_aliases = _text_array(
            raw_entry.get("heading_aliases", []),
            label=f"{key} heading aliases",
        )
        labels = tuple(dict.fromkeys((*names, *aliases)))
        definitions.append(
            SemanticRouteDefinition(
                key=key,
                description=f"财务披露主题：{names[0]}",
                labels=labels,
                heading_labels=heading_aliases,
                scopes=tuple(
                    dict.fromkeys(
                        (*_PERIODIC_SCOPES, *financial_scope_extensions.get(key, ()))
                    )
                ),
                exclusive_container=key in financial_containers,
                context_container=key in financial_context_containers,
                quantitative_topic=key in financial_quantitative_topics,
            )
        )

    raw_composites = financial.get("composite_context_labels")
    if not isinstance(raw_composites, list):
        raise SemanticRouteContractError(
            "financial composite context labels must be an array"
        )
    composite_sections: list[SemanticCompositeSection] = []
    for raw_composite in raw_composites:
        if not isinstance(raw_composite, dict) or set(raw_composite) != {
            "keys",
            "label",
        }:
            raise SemanticRouteContractError(
                "financial composite context label fields are invalid"
            )
        label = raw_composite["label"]
        if not isinstance(label, str):
            raise SemanticRouteContractError(
                "financial composite context label is invalid"
            )
        composite_sections.append(
            SemanticCompositeSection(
                label=label,
                keys=_text_array(
                    raw_composite["keys"],
                    label="financial composite context keys",
                ),
            )
        )
    raw_direct_composites = financial.get("composite_direct_labels")
    if not isinstance(raw_direct_composites, list):
        raise SemanticRouteContractError(
            "financial composite direct labels must be an array"
        )
    direct_composites: list[SemanticCompositeSection] = []
    for raw_composite in raw_direct_composites:
        if not isinstance(raw_composite, dict) or set(raw_composite) != {
            "keys",
            "label",
        }:
            raise SemanticRouteContractError(
                "financial composite direct label fields are invalid"
            )
        label = raw_composite["label"]
        if not isinstance(label, str):
            raise SemanticRouteContractError(
                "financial composite direct label is invalid"
            )
        direct_composites.append(
            SemanticCompositeSection(
                label=label,
                keys=_text_array(
                    raw_composite["keys"],
                    label="financial composite direct keys",
                ),
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
    event_quantitative_topics = _key_set(
        events.get("quantitative_topic_keys"),
        label="event quantitative topic keys",
    )
    event_role_anchors = _key_set(
        events.get("role_anchor_keys"),
        label="event role anchor keys",
    )
    event_section_containers = _key_set(
        events.get("section_container_keys"),
        label="event section container keys",
    )
    if not event_containers.issubset(event_keys):
        raise SemanticRouteContractError("event exclusive container key is not defined")
    if not event_overviews.issubset(event_keys):
        raise SemanticRouteContractError("event overview container key is not defined")
    if not event_quantitative_topics.issubset(event_keys):
        raise SemanticRouteContractError(
            "event quantitative topic key is not defined"
        )
    if not event_role_anchors.issubset(event_keys):
        raise SemanticRouteContractError("event role anchor key is not defined")
    if not event_section_containers.issubset(event_keys):
        raise SemanticRouteContractError("event section container key is not defined")
    if event_section_containers & event_containers:
        raise SemanticRouteContractError(
            "event section container conflicts with an exclusive container policy"
        )
    if event_containers & event_overviews:
        raise SemanticRouteContractError(
            "event container cannot be both exclusive and overview"
        )
    if event_role_anchors & (
        event_containers | event_overviews | event_section_containers
    ):
        raise SemanticRouteContractError(
            "event role anchor conflicts with a container policy"
        )
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict) or set(raw_entry) not in (
            {"key", "description", "labels", "scopes"},
            {"key", "description", "labels", "heading_labels", "scopes"},
        ):
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
                heading_labels=_text_array(
                    raw_entry.get("heading_labels", []),
                    label=f"{key} heading labels",
                ),
                scopes=_text_array(raw_entry["scopes"], label=f"{key} scopes"),
                exclusive_container=key in event_containers,
                overview_container=key in event_overviews,
                section_container=key in event_section_containers,
                quantitative_topic=key in event_quantitative_topics,
                role_anchor=key in event_role_anchors,
            )
        )
    return SemanticRouteTaxonomy(
        version=SEMANTIC_TAXONOMY_VERSION,
        definitions=tuple(definitions),
        composite_sections=tuple(composite_sections),
        direct_composites=tuple(direct_composites),
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
