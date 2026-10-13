"""Storage admission for publication text before any immutable preparation.

PostgreSQL ``text`` and ``jsonb`` cannot store U+0000, and UTF-8 cannot encode
a surrogate code point, yet U+0000 survives strict JSON decoding, canonical
encoding and the Unit hash closure. This pure check runs on the complete,
already closed publication request, fresh or reopened from its sealed bytes,
before readiness seals or promotes anything and before transaction P. It never
rewrites, normalizes or re-hashes the request: a representable request passes
as the same object with the same bytes.

Only candidate Unit content (title, heading path, payload keys and values) is
the typed attempt-local ``PublicationTextUnrepresentableError``. The same code
points in a control or identity field mean the request itself is corrupt and
raise the existing integrity error, which keeps the worker stop.

A current build marks every unrepaired provider U+0000 as U+FFFD before Unit
hashing (``provider_text_nul_substitution.v1``), so this is the backstop for
requests sealed by an earlier builder and for a native repair text that still
holds U+0000; it does not replace that policy.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import re

from disclosure_anchor.application.contracts.atomic_document_publication_v4 import (
    AtomicPublicationRequestV4,
    PreIdUnitPublicationV4,
    WholeDocumentPublicationV4Error,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads


PUBLICATION_TEXT_REPRESENTABILITY_POLICY = "publication_text_representability.v1"
PUBLICATION_TEXT_UNREPRESENTABLE_ERROR_CODE = "publication_text_unrepresentable"

# Unit request fields and the DocumentUnit column each one reaches through
# transaction P (``atomic_document_publisher_v4._document_unit``). Content is
# provider text; control fields are closed taxonomy or builder identities.
CONTENT_UNIT_FIELDS = (
    ("title", "title"),
    ("heading_path", "heading_path"),
    ("canonical_payload_json", "payload"),
)
CONTROL_UNIT_FIELDS = (
    ("semantic_keys", "semantic_keys"),
    ("section_keys", "section_keys"),
    ("canonical_artifact_locator_json", "artifact_locator"),
)
# Already closed by the request contract (``_identity`` refuses every code
# point below U+0020, hashes are canonical, the rest are enums or integers).
CLOSED_UNIT_FIELDS = (
    "document_id",
    "processing_run_id",
    "provider_document_id",
    "payload_kind",
    "unit_index",
    "content_hash",
    "structure_hash",
    "quality_status",
    "applicability",
    "page_no",
    "query_projection_hash",
)
# Every processing-run projection value transaction P persists
# (``atomic_document_publisher_v4._apply_processing_projection``).
PERSISTED_PROCESSING_RUN_PROJECTION_FIELDS = (
    "artifact_owner_processing_run_id",
    "builder_rules_version",
    "content_hash_aggregate",
    "document_units_relpath",
    "normalized_ir_relpath",
    "parser_artifact_relpath",
    "parser_backend",
    "parser_language",
    "parser_method",
    "parser_name",
    "parser_target_identity",
    "parser_version",
    "provider_document_relpath",
    "provider_document_sha256",
    "semantic_adjudication_status",
    "semantic_adjudication_summary",
    "semantic_degraded_unit_count",
    "semantic_failover_group_count",
    "semantic_route_receipts_contract_version",
    "semantic_route_receipts_relpath",
    "semantic_route_receipts_sha256",
    "status",
    "structure_hash_aggregate",
    "unit_build_attempt_count",
    "unit_build_status",
)

_MESSAGE_LIMIT = 4096  # FailureReceiptV4.message ceiling
_SHOWN_FINDINGS_LIMIT = 12
_PATH_TEXT_LIMIT = 120
_CONTENT_FIELDS = frozenset(name for _field, name in CONTENT_UNIT_FIELDS)
# Array positions render as ``3``; object keys as ``#3``, their position in
# sorted key order. Key text never enters the message: a key can be content.
_SAFE_SEGMENT = re.compile(r"#?[0-9]+\Z", re.ASCII)
_CODEPOINT = re.compile(r"U\+[0-9A-F]{4}\Z", re.ASCII)
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_SURROGATE = re.compile("[\ud800-\udfff]")


@dataclass(frozen=True, slots=True)
class TextStorageFinding:
    """One unrepresentable code point in one content string, without the text."""

    unit_index: int
    field: str
    path: tuple[str, ...]
    in_key: bool
    codepoint: str
    count: int

    def __post_init__(self) -> None:
        if (
            type(self.unit_index) is not int
            or self.unit_index < 1
            or self.field not in _CONTENT_FIELDS
            or type(self.path) is not tuple
            or any(type(item) is not str or not _SAFE_SEGMENT.match(item) for item in self.path)
            or type(self.in_key) is not bool
            or type(self.codepoint) is not str
            or not _CODEPOINT.match(self.codepoint)
            or type(self.count) is not int
            or self.count < 1
        ):
            raise ValueError("publication text finding is not a bounded content-free location")

    def render(self) -> str:
        parts = [f"unit={self.unit_index}", f"field={self.field}"]
        if self.path:
            path = "/" + "/".join(self.path)
            if len(path) > _PATH_TEXT_LIMIT:
                path = path[: _PATH_TEXT_LIMIT - 3] + "..."
            parts.append(f"path={path}")
        if self.in_key:
            parts.append("in=key")
        parts.extend((f"codepoint={self.codepoint}", f"count={self.count}"))
        return " ".join(parts)


class PublicationTextUnrepresentableError(ValueError):
    """Candidate Unit content the publication store cannot represent.

    Raised only by this module's content predicate. It is attempt-local and
    permanent for these exact bytes: the COMMIT lane records it as a
    non-retryable failure and closes the attempt through cleanup and ACK. Its
    message is bounded ASCII built from locations, counts and the request's
    existing SHA-256; the offending text stays only in the sealed request.
    """

    def __init__(
        self,
        *,
        request_sha256: str,
        findings: tuple[TextStorageFinding, ...],
        finding_count: int,
        occurrence_count: int,
    ) -> None:
        if (
            type(request_sha256) is not str
            or not _SHA256.match(request_sha256)
            or type(findings) is not tuple
            or not 1 <= len(findings) <= _SHOWN_FINDINGS_LIMIT
            or any(type(item) is not TextStorageFinding for item in findings)
            or type(finding_count) is not int
            or finding_count < len(findings)
            or type(occurrence_count) is not int
            or occurrence_count < finding_count
        ):
            raise ValueError("publication text failure evidence is invalid")
        self.policy = PUBLICATION_TEXT_REPRESENTABILITY_POLICY
        self.request_sha256 = request_sha256
        self.findings = findings
        self.finding_count = finding_count
        self.occurrence_count = occurrence_count
        self._summary = _render_summary(
            request_sha256=request_sha256,
            findings=findings,
            finding_count=finding_count,
            occurrence_count=occurrence_count,
        )
        super().__init__(self._summary)

    def safe_summary(self) -> str:
        return self._summary


def validate_publication_text_representability(request: AtomicPublicationRequestV4) -> None:
    """Refuse a closed request whose persisted text PostgreSQL cannot store.

    Control fields are checked first: a corrupt identity makes the whole
    request untrustworthy, so it is never reported as candidate content.
    """

    if type(request) is not AtomicPublicationRequestV4:
        raise WholeDocumentPublicationV4Error("publication text check requires an exact V4 request")
    _require_representable_control(request)
    shown: list[TextStorageFinding] = []
    finding_count = 0
    occurrence_count = 0
    for unit in request.units:
        for field, path, in_key, text in _content_strings(unit):
            for codepoint, count in _unrepresentable_counts(text):
                finding_count += 1
                occurrence_count += count
                if len(shown) < _SHOWN_FINDINGS_LIMIT:
                    shown.append(TextStorageFinding(
                        unit_index=unit.unit_index, field=field, path=path, in_key=in_key,
                        codepoint=codepoint, count=count,
                    ))
    if finding_count:
        raise PublicationTextUnrepresentableError(
            request_sha256=request.request_sha256,
            findings=tuple(shown),
            finding_count=finding_count,
            occurrence_count=occurrence_count,
        )


def _content_strings(
    unit: PreIdUnitPublicationV4,
) -> Iterator[tuple[str, tuple[str, ...], bool, str]]:
    if unit.title is not None:
        yield "title", (), False, unit.title
    for position, element in enumerate(unit.heading_path):
        yield "heading_path", (str(position),), False, element
    # The one known JSON field is decoded exactly once, as transaction P does.
    for path, in_key, text in _json_strings(strict_json_loads(unit.canonical_payload_json)):
        yield "payload", path, in_key, text


def _require_representable_control(request: AtomicPublicationRequestV4) -> None:
    projection = strict_json_loads(request.processing_run_projection_json)
    if type(projection) is not dict:
        raise WholeDocumentPublicationV4Error("processing-run projection must be an object")
    for name in PERSISTED_PROCESSING_RUN_PROJECTION_FIELDS:
        if name not in projection:
            raise WholeDocumentPublicationV4Error(f"processing-run projection lacks {name}")
        if _has_unrepresentable(projection[name]):
            raise WholeDocumentPublicationV4Error(
                f"{PUBLICATION_TEXT_REPRESENTABILITY_POLICY}: control field processing_run.{name} "
                "carries an unrepresentable code point"
            )
    for unit in request.units:
        for label, value in (
            ("semantic_keys", unit.semantic_keys),
            ("section_keys", unit.section_keys),
            ("artifact_locator", strict_json_loads(unit.canonical_artifact_locator_json)),
        ):
            if value is not None and _has_unrepresentable(list(value) if type(value) is tuple else value):
                raise WholeDocumentPublicationV4Error(
                    f"{PUBLICATION_TEXT_REPRESENTABILITY_POLICY}: control field {label} of unit "
                    f"{unit.unit_index} carries an unrepresentable code point"
                )


def _has_unrepresentable(value: object) -> bool:
    return any(_unrepresentable_counts(text) for _path, _in_key, text in _json_strings(value))


def _unrepresentable_counts(text: str) -> list[tuple[str, int]]:
    counts: list[tuple[str, int]] = []
    nul = text.count("\x00")
    if nul:
        counts.append(("U+0000", nul))
    if _SURROGATE.search(text) is not None:
        surrogates: dict[int, int] = {}
        for match in _SURROGATE.finditer(text):
            codepoint = ord(match.group())
            surrogates[codepoint] = surrogates.get(codepoint, 0) + 1
        counts.extend((f"U+{codepoint:04X}", count) for codepoint, count in sorted(surrogates.items()))
    return counts


def _json_strings(value: object) -> Iterator[tuple[tuple[str, ...], bool, str]]:
    """Every string of one decoded JSON value, keys before their values.

    Depth-first and iterative: canonical JSON can nest deeper than a safe
    Python recursion. Strings are yielded as decoded; none is decoded again.
    """

    stack: list[tuple[tuple[str, ...], bool, object]] = [((), False, value)]
    while stack:
        path, is_key, item = stack.pop()
        if is_key:
            yield path, True, str(item)
        elif type(item) is str:
            yield path, False, item
        elif type(item) is dict:
            children: list[tuple[tuple[str, ...], bool, object]] = []
            for ordinal, key in enumerate(sorted(item)):
                child_path = path + (f"#{ordinal}",)
                children.append((child_path, True, key))
                children.append((child_path, False, item[key]))
            stack.extend(reversed(children))
        elif type(item) is list:
            stack.extend(
                reversed([(path + (str(index),), False, child) for index, child in enumerate(item)])
            )


def _render_summary(
    *,
    request_sha256: str,
    findings: tuple[TextStorageFinding, ...],
    finding_count: int,
    occurrence_count: int,
) -> str:
    rendered: list[str] = []
    budget = _MESSAGE_LIMIT - 256  # the header below is far shorter than 256 characters
    for finding in findings:
        piece = "; " + finding.render()
        if len(piece) > budget:
            break
        rendered.append(piece)
        budget -= len(piece)
    header = (
        f"{PUBLICATION_TEXT_REPRESENTABILITY_POLICY}: request={request_sha256} "
        f"findings={finding_count} occurrences={occurrence_count} shown={len(rendered)}"
    )
    summary = header + "".join(rendered)
    if len(summary) > _MESSAGE_LIMIT or not summary.isascii() or not summary.isprintable():
        raise ValueError("publication text failure summary is not bounded printable ASCII")
    return summary


__all__ = [
    "CLOSED_UNIT_FIELDS",
    "CONTENT_UNIT_FIELDS",
    "CONTROL_UNIT_FIELDS",
    "PERSISTED_PROCESSING_RUN_PROJECTION_FIELDS",
    "PUBLICATION_TEXT_REPRESENTABILITY_POLICY",
    "PUBLICATION_TEXT_UNREPRESENTABLE_ERROR_CODE",
    "PublicationTextUnrepresentableError",
    "TextStorageFinding",
    "validate_publication_text_representability",
]
