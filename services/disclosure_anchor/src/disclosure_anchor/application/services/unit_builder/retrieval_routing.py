"""L2 retrieval taxonomy projection, isolated from L1 source structure.

The functions here run only after evidence units and their boundaries already
exist.  They may label a unit for retrieval, but cannot mutate its title,
heading path, payload, source ownership, ordering, or grouping.
"""

from __future__ import annotations

from dataclasses import dataclass

from disclosure_anchor.application.services.unit_builder import rules as taxonomy_rules


FALLBACK_KEY = taxonomy_rules.SEMANTIC_FALLBACK_KEY


@dataclass(frozen=True)
class RoutingMemberEvidence:
    """One completed mixed-part scope exposed to retrieval routing."""

    title: str | None
    heading_path: tuple[str, ...]
    table_caption: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoutingEvidence:
    """Read-only fields exposed after an evidence unit is fully assembled."""

    title: str | None
    heading_path: tuple[str, ...]
    table_caption: tuple[str, ...] = ()
    members: tuple[RoutingMemberEvidence, ...] = ()


def note_keys(evidence: RoutingEvidence) -> list[str]:
    """Return controlled note facets without changing source structure."""

    keys: list[str] = []
    scopes = (
        RoutingMemberEvidence(
            title=evidence.title,
            heading_path=evidence.heading_path,
            table_caption=evidence.table_caption,
        ),
        *evidence.members,
    )
    for scope in scopes:
        candidates = [
            scope.title,
            *reversed(scope.heading_path),
        ]
        for candidate in candidates:
            for key in taxonomy_rules.note_keys_for_title(candidate):
                if key not in keys:
                    keys.append(key)
    return keys


def semantic_keys(
    evidence: RoutingEvidence,
    *,
    filing_type: str | None,
) -> list[str]:
    """Project topic facets from an already assembled evidence unit."""

    scopes = (
        RoutingMemberEvidence(
            title=evidence.title,
            heading_path=evidence.heading_path,
            table_caption=evidence.table_caption,
        ),
        *evidence.members,
    )
    keys: list[str] = []
    for scope in scopes:
        source_path = scope.heading_path
        captions = " ".join(scope.table_caption)
        text = " ".join(
            part
            for part in [
                scope.title or "",
                " ".join(source_path),
                captions,
            ]
            if part
        )
        leaf_text = " ".join(
            part
            for part in [
                scope.title or "",
                source_path[-1] if source_path else "",
                captions,
            ]
            if part
        )
        for rule in taxonomy_rules.SEMANTIC_KEY_RULES:
            if (
                rule.filing_type_limited
                and filing_type not in taxonomy_rules.SEMANTIC_LIMITED_FILING_TYPES
            ):
                continue
            haystack = leaf_text if rule.leaf_only else text
            if all(token in haystack for token in rule.required) and (
                not rule.any_required
                or any(token in haystack for token in rule.any_required)
            ):
                if rule.semantic_key not in keys:
                    keys.append(rule.semantic_key)
    return keys
