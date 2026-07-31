"""Prove MinerU v2 visible text against its legacy serializer output.

MinerU writes the same ordered page blocks twice: ``content_list_v2`` keeps
typed text spans, while legacy ``content_list`` renders those spans as
Markdown-oriented strings.  This module validates the two representations
block-for-block and returns an in-memory canonical view with only serializer
syntax removed.  Neither representation is rewritten on disk.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

from disclosure_anchor.domain.errors import ParserOutputContractError


_SPECIAL_CHARS = frozenset("*_`~$")
_TEXT_SPAN_TYPES = frozenset({"text", "phonetic"})
_INLINE_EQUATION = "equation_inline"
MinerUSerializerBackend = Literal["pipeline", "vlm"]

_V2_LEGACY_TYPES: dict[str, frozenset[str]] = {
    "title": frozenset({"text"}),
    "paragraph": frozenset({"text", "phonetic"}),
    "page_header": frozenset({"header"}),
    "page_footer": frozenset({"footer"}),
    "page_number": frozenset({"page_number"}),
    "page_aside_text": frozenset({"aside_text"}),
    "page_footnote": frozenset({"page_footnote"}),
    "equation_interline": frozenset({"equation"}),
    "image": frozenset({"image"}),
    "table": frozenset({"table"}),
    "chart": frozenset({"chart"}),
    "code": frozenset({"code"}),
    "algorithm": frozenset({"code"}),
    # Legacy may flatten a v2 list to one text block ("\n"-joined items,
    # same bbox); the exact-alignment proof below still gates the pairing.
    "list": frozenset({"list", "ref_text", "text"}),
    "index": frozenset({"text"}),
}

_PAGE_TEXT_FIELDS = {
    "page_header": "page_header_content",
    "page_footer": "page_footer_content",
    "page_number": "page_number_content",
    "page_aside_text": "page_aside_text_content",
    "page_footnote": "page_footnote_content",
}

_SEQUENCE_FIELDS = {
    "image": (
        ("image_caption", "image_caption"),
        ("image_footnote", "image_footnote"),
    ),
    "table": (
        ("table_caption", "table_caption"),
        ("table_footnote", "table_footnote"),
    ),
    "chart": (
        ("chart_caption", "chart_caption"),
        ("chart_footnote", "chart_footnote"),
    ),
    "code": (
        ("code_caption", "code_caption"),
        ("code_footnote", "code_footnote"),
    ),
    "algorithm": (
        ("code_caption", "algorithm_caption"),
        ("code_footnote", "algorithm_footnote"),
    ),
}


@dataclass(frozen=True, slots=True)
class MinerUTextProjectionSet:
    """Canonical items plus the exact v2-block to legacy-item bijection."""

    canonical_items: tuple[dict[str, Any], ...]
    legacy_indices_by_v2_page: tuple[tuple[int, ...], ...]

    def legacy_index(self, page_index: int, block_index: int) -> int:
        try:
            return self.legacy_indices_by_v2_page[page_index][block_index]
        except IndexError as exc:
            raise ParserOutputContractError(
                "MinerU v2 block locator is outside the proved projection"
            ) from exc


def mineru_serializer_backend(value: str) -> MinerUSerializerBackend:
    """Resolve the official serializer lane from a parser backend identity."""

    root = value.split("-", 1)[0]
    if root == "pipeline":
        return "pipeline"
    # MinerU 3.4 dispatches both VLM and hybrid backends through
    # ``vlm_middle_json_mkcontent.union_make``.  This is provider identity,
    # not a content-based fallback.
    if root in {"vlm", "hybrid"}:
        return "vlm"
    raise ParserOutputContractError(
        "MinerU parser identity has an unsupported serializer backend"
    )


def build_mineru_text_projections(
    legacy_items: Sequence[Mapping[str, Any]],
    v2_pages: Sequence[Sequence[Mapping[str, Any]]],
    *,
    serializer_backend: MinerUSerializerBackend,
    page_offset: int,
    expected_page_count: int,
) -> MinerUTextProjectionSet:
    """Return the sole exact canonical view of one MinerU artifact pair."""

    if serializer_backend not in {"pipeline", "vlm"}:
        raise ParserOutputContractError(
            "MinerU text projection has an unsupported serializer backend"
        )
    if (
        isinstance(page_offset, bool)
        or not isinstance(page_offset, int)
        or page_offset < 0
        or isinstance(expected_page_count, bool)
        or not isinstance(expected_page_count, int)
        or expected_page_count < 1
        or page_offset + len(v2_pages) > expected_page_count
    ):
        raise ParserOutputContractError(
            "MinerU text projection has an invalid PDF page range"
        )

    legacy_by_page: list[list[tuple[int, Mapping[str, Any]]]] = [
        [] for _ in v2_pages
    ]
    previous_page = page_offset
    for legacy_index, item in enumerate(legacy_items):
        page_idx = item.get("page_idx")
        if (
            isinstance(page_idx, bool)
            or not isinstance(page_idx, int)
            or not page_offset <= page_idx < page_offset + len(v2_pages)
            or page_idx < previous_page
        ):
            raise ParserOutputContractError(
                "MinerU legacy items do not form the requested ordered page range"
            )
        previous_page = page_idx
        legacy_by_page[page_idx - page_offset].append((legacy_index, item))

    canonical_items = [dict(item) for item in legacy_items]
    indices_by_page: list[tuple[int, ...]] = []
    for local_page_index, (legacy_page, v2_page) in enumerate(
        zip(legacy_by_page, v2_pages, strict=True)
    ):
        if len(legacy_page) != len(v2_page):
            raise ParserOutputContractError(
                "MinerU legacy/v2 page block counts differ "
                f"(page_idx={page_offset + local_page_index})"
            )
        page_indices: list[int] = []
        for block_index, ((legacy_index, legacy), v2) in enumerate(
            zip(legacy_page, v2_page, strict=True)
        ):
            canonical_items[legacy_index] = _project_item(
                legacy,
                v2,
                serializer_backend=serializer_backend,
                page_idx=page_offset + local_page_index,
                block_index=block_index,
            )
            page_indices.append(legacy_index)
        indices_by_page.append(tuple(page_indices))
    return MinerUTextProjectionSet(
        canonical_items=tuple(canonical_items),
        legacy_indices_by_v2_page=tuple(indices_by_page),
    )


def _project_item(
    legacy: Mapping[str, Any],
    v2: Mapping[str, Any],
    *,
    serializer_backend: MinerUSerializerBackend,
    page_idx: int,
    block_index: int,
) -> dict[str, Any]:
    legacy_type = legacy.get("type")
    v2_type = v2.get("type")
    if (
        not isinstance(legacy_type, str)
        or not isinstance(v2_type, str)
        or v2_type not in _V2_LEGACY_TYPES
        or legacy_type not in _V2_LEGACY_TYPES[v2_type]
        or legacy.get("bbox") != v2.get("bbox")
    ):
        raise ParserOutputContractError(
            "MinerU legacy/v2 block identity differs "
            f"(page_idx={page_idx}, block_index={block_index})"
        )
    content = v2.get("content")
    if not isinstance(content, Mapping):
        raise ParserOutputContractError(
            "MinerU v2 block content must be an object"
        )

    output = dict(legacy)
    if v2_type == "title":
        level = content.get("level")
        if (
            isinstance(level, bool)
            or not isinstance(level, int)
            or level < 1
            or legacy.get("text_level") != level
        ):
            raise ParserOutputContractError(
                "MinerU v2 title level differs from its legacy carrier"
            )
        output["text"] = _project_scalar(
            legacy.get("text"),
            content.get("title_content"),
            allow_text_prefix=False,
        )
        return output
    if v2_type == "paragraph":
        text_level = legacy.get("text_level")
        if text_level not in {None, 0}:
            raise ParserOutputContractError(
                "MinerU v2 paragraph is paired with a legacy heading"
            )
        output["text"] = _project_scalar(
            legacy.get("text"),
            content.get("paragraph_content"),
            allow_text_prefix=legacy_type == "text",
        )
        return output
    if v2_type in _PAGE_TEXT_FIELDS:
        output["text"] = _project_scalar(
            legacy.get("text"),
            content.get(_PAGE_TEXT_FIELDS[v2_type]),
            allow_text_prefix=False,
        )
        return output
    if v2_type == "list":
        if legacy_type == "list":
            output["list_items"] = _project_list_items(
                legacy.get("list_items"),
                content.get("list_items"),
            )
        else:
            output["text"] = _project_index(
                legacy.get("text"),
                content.get("list_items"),
            )
        return output
    if v2_type == "index":
        output["text"] = _project_index(
            legacy.get("text"),
            content.get("list_items"),
        )
        return output
    if v2_type in {"code", "algorithm"}:
        _project_sequence_fields(output, content, v2_type=v2_type)
        body_field = "code_content" if v2_type == "code" else "algorithm_content"
        if v2_type == "code":
            output["code_body"] = _project_code_body(
                legacy.get("code_body"),
                content.get(body_field),
                content.get("code_language"),
                serializer_backend=serializer_backend,
            )
        else:
            output["code_body"] = _project_scalar(
                legacy.get("code_body"),
                content.get(body_field),
                allow_text_prefix=False,
            )
        return output
    if v2_type in _SEQUENCE_FIELDS:
        _project_sequence_fields(output, content, v2_type=v2_type)
    return output


def _project_sequence_fields(
    output: dict[str, Any],
    content: Mapping[str, Any],
    *,
    v2_type: str,
) -> None:
    for legacy_field, v2_field in _SEQUENCE_FIELDS[v2_type]:
        legacy_present = legacy_field in output
        v2_present = v2_field in content
        if not legacy_present and not v2_present:
            continue
        if legacy_present != v2_present:
            raise ParserOutputContractError(
                "MinerU legacy/v2 sequence field presence differs"
            )
        output[legacy_field] = _project_sequence(
            output[legacy_field],
            content[v2_field],
        )


def _project_scalar(
    legacy_value: object,
    parts: object,
    *,
    allow_text_prefix: bool,
) -> str:
    if not isinstance(legacy_value, str):
        raise ParserOutputContractError(
            "MinerU legacy text projection target must be text"
        )
    return _canonicalize_legacy(
        legacy_value,
        _typed_chars(parts),
        allow_text_prefix=allow_text_prefix,
    )


def _project_sequence(legacy_values: object, parts: object) -> list[str]:
    if not isinstance(legacy_values, list) or not all(
        isinstance(value, str) for value in legacy_values
    ):
        raise ParserOutputContractError(
            "MinerU legacy sequence projection target must be text[]"
        )
    if not isinstance(parts, list):
        raise ParserOutputContractError(
            "MinerU v2 sequence projection source must be an array"
        )
    visible_legacy_values = [
        value for value in legacy_values if value.strip()
    ]
    if not visible_legacy_values:
        if parts:
            raise ParserOutputContractError(
                "MinerU v2 sequence has no legacy target"
            )
        return []

    # A sequence is a unique partition of the ordered v2 spans into the
    # non-blank legacy fields.  Retain at most two paths per boundary: one
    # proves uniqueness, two prove ambiguity.  This bounds the search by
    # legacy_count * span_count^2 instead of enumerating every partition.
    states: dict[int, tuple[int, tuple[str, ...] | None]] = {0: (1, ())}
    for legacy_index, legacy_value in enumerate(visible_legacy_values):
        next_states: dict[int, tuple[int, tuple[str, ...] | None]] = {}
        remaining_values = len(visible_legacy_values) - legacy_index - 1
        for part_index, (path_count, path) in states.items():
            max_end = len(parts) - remaining_values
            for end in range(part_index + 1, max_end + 1):
                try:
                    canonical = _canonicalize_legacy(
                        legacy_value,
                        _typed_chars(parts[part_index:end]),
                        allow_text_prefix=False,
                    )
                except ParserOutputContractError:
                    continue
                existing_count, _ = next_states.get(end, (0, None))
                combined_count = min(2, existing_count + path_count)
                unique_path = (
                    (*path, canonical)
                    if existing_count == 0 and path_count == 1 and path is not None
                    else None
                )
                next_states[end] = (combined_count, unique_path)
        states = next_states
        if not states:
            break

    solution_count, solution = states.get(len(parts), (0, None))
    if solution_count != 1 or solution is None:
        raise ParserOutputContractError(
            "MinerU v2 sequence projection is unaligned or ambiguous"
        )
    return list(solution)


def _project_list_items(legacy_values: object, raw_items: object) -> list[str]:
    if not isinstance(legacy_values, list) or not isinstance(raw_items, list):
        raise ParserOutputContractError(
            "MinerU list projection requires paired item arrays"
        )
    if len(legacy_values) != len(raw_items):
        raise ParserOutputContractError(
            "MinerU list projection item counts differ"
        )
    projected: list[str] = []
    for legacy_value, raw_item in zip(legacy_values, raw_items, strict=True):
        if (
            not isinstance(raw_item, Mapping)
            or raw_item.get("item_type") != "text"
        ):
            raise ParserOutputContractError(
                "MinerU v2 list item has an unsupported shape"
            )
        projected.append(
            _project_scalar(
                legacy_value,
                raw_item.get("item_content"),
                allow_text_prefix=False,
            )
        )
    return projected


def _project_index(legacy_value: object, raw_items: object) -> str:
    if not isinstance(legacy_value, str) or not isinstance(raw_items, list):
        raise ParserOutputContractError(
            "MinerU index projection requires text and item arrays"
        )
    canonical_items: list[str] = []
    for raw_item in raw_items:
        if (
            not isinstance(raw_item, Mapping)
            or raw_item.get("item_type") != "text"
        ):
            raise ParserOutputContractError(
                "MinerU v2 index item has an unsupported shape"
            )
        canonical_items.append(
            "".join(
                char.value for char in _typed_chars(raw_item.get("item_content"))
            ).rstrip()
        )
    typed = _typed_chars(
        [
            {
                "type": "text",
                "content": "\n".join(canonical_items),
            }
        ]
    )
    return _canonicalize_legacy(
        legacy_value,
        typed,
        allow_text_prefix=False,
    )


def _project_code_body(
    legacy_value: object,
    parts: object,
    language: object,
    *,
    serializer_backend: MinerUSerializerBackend,
) -> str:
    if not isinstance(legacy_value, str) or not isinstance(language, str):
        raise ParserOutputContractError(
            "MinerU code projection requires a language and legacy body"
        )
    canonical = "".join(char.value for char in _typed_chars(parts))
    if serializer_backend == "pipeline":
        canonical = canonical.rstrip()
        canonical = "\n".join(
            line.rstrip() for line in canonical.split("\n")
        )
    if f"```{language}\n{canonical}\n```" != legacy_value:
        raise ParserOutputContractError(
            "MinerU v2 code content does not replay its legacy fenced body"
        )
    return canonical


class _TypedChar(NamedTuple):
    value: str
    serializer_escape_allowed: bool


def _typed_chars(raw: object) -> tuple[_TypedChar, ...]:
    if not isinstance(raw, list):
        raise ParserOutputContractError(
            "MinerU v2 visible content must be a span array"
        )
    output: list[_TypedChar] = []
    for part in raw:
        if (
            not isinstance(part, Mapping)
            or set(part) != {"type", "content"}
            or not isinstance(part.get("content"), str)
        ):
            raise ParserOutputContractError(
                "MinerU v2 visible span has an unsupported shape"
            )
        span_type = part.get("type")
        content = str(part["content"])
        if span_type in _TEXT_SPAN_TYPES:
            preceding_backslashes = 0
            for char in content:
                output.append(
                    _TypedChar(
                        char,
                        char in _SPECIAL_CHARS
                        and preceding_backslashes % 2 == 0,
                    )
                )
                preceding_backslashes = (
                    preceding_backslashes + 1 if char == "\\" else 0
                )
            continue
        if span_type == _INLINE_EQUATION:
            equation = content.strip()
            if not equation:
                raise ParserOutputContractError(
                    "MinerU v2 inline equation is blank"
                )
            output.extend(
                _TypedChar(char, False) for char in f"${equation}$"
            )
            continue
        raise ParserOutputContractError(
            f"MinerU v2 visible span type is unsupported: {span_type!r}"
        )
    return tuple(output)


def _canonicalize_legacy(
    legacy: str,
    typed: tuple[_TypedChar, ...],
    *,
    allow_text_prefix: bool,
) -> str:
    """Delete only v2-proved serializer slashes; retain provider whitespace."""

    legacy_chars = [
        (index, char)
        for index, char in enumerate(legacy)
        if not char.isspace()
    ]
    typed_chars = [
        (index, char)
        for index, char in enumerate(typed)
        if not char.value.isspace()
    ]
    states: list[dict[int, list[tuple[int, ...]]]] = [
        {} for _ in range(len(legacy_chars) + 1)
    ]
    states[0][0] = [()]
    for legacy_index, (raw_index, legacy_char) in enumerate(legacy_chars):
        for typed_index, paths in tuple(states[legacy_index].items()):
            if typed_index >= len(typed_chars):
                continue
            _, typed_char = typed_chars[typed_index]
            if legacy_char == typed_char.value:
                _extend_projection_paths(
                    states[legacy_index + 1],
                    typed_index + 1,
                    paths,
                )
            if (
                legacy_char == "\\"
                and raw_index + 1 < len(legacy)
                and legacy[raw_index + 1] == typed_char.value
                and (
                    typed_char.serializer_escape_allowed
                    or (
                        allow_text_prefix
                        and _is_text_prefix_escape(legacy, raw_index)
                    )
                )
            ):
                _extend_projection_paths(
                    states[legacy_index + 1],
                    typed_index,
                    ((*path, raw_index) for path in paths),
                )
    solutions = states[-1].get(len(typed_chars), [])
    if len(solutions) != 1:
        raise ParserOutputContractError(
            "MinerU v2 spans do not uniquely prove their legacy text"
        )
    deleted = frozenset(solutions[0])
    return "".join(
        char for index, char in enumerate(legacy) if index not in deleted
    )


def _extend_projection_paths(
    target: dict[int, list[tuple[int, ...]]],
    typed_index: int,
    paths: Iterable[tuple[int, ...]],
) -> None:
    values = target.setdefault(typed_index, [])
    for path in paths:
        if path not in values:
            values.append(path)
        if len(values) == 2:
            return


def _is_text_prefix_escape(content: str, slash_index: int) -> bool:
    if slash_index > 3 or content[slash_index] != "\\":
        return False
    if any(char not in " \t" for char in content[:slash_index]):
        return False
    marker_start = slash_index + 1
    if marker_start >= len(content):
        return False
    if content[marker_start] in "+-":
        after = marker_start + 1
        return after < len(content) and content[after] in " \t"
    if content[marker_start] != "#":
        return False
    after = marker_start
    while (
        after < len(content)
        and content[after] == "#"
        and after - marker_start < 6
    ):
        after += 1
    return after < len(content) and content[after] in " \t"


__all__ = [
    "MinerUSerializerBackend",
    "MinerUTextProjectionSet",
    "build_mineru_text_projections",
    "mineru_serializer_backend",
]
