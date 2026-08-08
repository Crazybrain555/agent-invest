"""Shared synthetic native-layout support for structure-proof fixtures.

The current structure proof requires native source pages and validated
carrier support; fixtures model that boundary explicitly. A carrier is
``native_exact`` only when a complete native atom run reproduces its
comparison value — a provider bbox never mints a native-layout witness.
"""

from __future__ import annotations

from typing import Any, Mapping

from disclosure_anchor.adapters.parsers.comparison import comparison_text
from disclosure_anchor.adapters.parsers.mineru.source_evidence import (
    CarrierSourceSupport,
    ResolvedTableRole,
    iter_mineru_text_carriers,
)


def test_carrier_source_support(
    content_list: list[dict[str, Any]],
    *,
    source_pages: tuple[Any, ...],
    table_role_overrides: tuple[ResolvedTableRole, ...] = (),
) -> Mapping[tuple[int, str, int | None], CarrierSourceSupport]:
    """Build explicit source support for synthetic native-layout tests."""

    atoms_by_page = {
        page.page_idx: tuple(sorted(page.atoms, key=lambda atom: atom.order))
        for page in source_pages
    }
    used: dict[int, set[int]] = {}
    output: dict[
        tuple[int, str, int | None],
        CarrierSourceSupport,
    ] = {}
    for carrier in iter_mineru_text_carriers(
        content_list,
        table_role_overrides=table_role_overrides,
    ):
        if carrier.page_idx is None or carrier.bbox is None:
            continue
        target = carrier.comparison_value
        available = atoms_by_page.get(carrier.page_idx, ())
        selected: tuple[Any, ...] = ()
        for start in range(len(available)):
            parts: list[Any] = []
            for atom in available[start:]:
                if atom.order in used.setdefault(carrier.page_idx, set()):
                    if parts:
                        break
                    continue
                parts.append(atom)
                value = comparison_text("".join(item.text for item in parts))
                if value == target:
                    selected = tuple(parts)
                    break
                if target and len(value) > len(target):
                    break
            if selected:
                break
        if selected:
            used[carrier.page_idx].update(atom.order for atom in selected)
        key = (carrier.source_item_index, carrier.field, carrier.index)
        output[key] = CarrierSourceSupport(
            source_item_index=carrier.source_item_index,
            field=carrier.field,
            index=carrier.index,
            page_idx=carrier.page_idx,
            bbox=carrier.bbox,
            kind="native_exact" if selected else "visual_bound",
            source_atom_orders=tuple(atom.order for atom in selected),
            artifact_role=None if selected else "test_visual_occurrence",
            artifact_sha256=(
                None if selected else "sha256:" + "f" * 64
            ),
        )
    return output


def auto_native_pages(
    content_list: list[dict[str, Any]],
    *,
    heading_texts: frozenset[str] = frozenset(),
) -> tuple[Any, ...]:
    """One native atom per typed text carrier, at the carrier's own bbox.

    Gives legacy-era producer fixtures a real native lane without changing
    their provider semantics: every typed text value exists natively at the
    provider geometry, so source support closes as ``native_exact``.
    Carriers whose comparison value appears in ``heading_texts`` render as a
    centered display line (taller than the modal body height), matching how
    a real filing paints the headings these fixtures assert on.  One
    ``NativeTextPage`` is built per provider page.
    """

    from disclosure_anchor.adapters.parsers.pdf_native_text import (
        NativeTextAtom,
        NativeTextLayoutRef,
        NativeTextPage,
    )

    by_page: dict[int, list[tuple[Any, bool]]] = {}
    for carrier in iter_mineru_text_carriers(content_list):
        if carrier.page_idx is None or carrier.bbox is None:
            continue
        if not carrier.source_value:
            continue
        is_heading = (
            carrier.field == "text"
            and carrier.comparison_value in heading_texts
        )
        by_page.setdefault(carrier.page_idx, []).append((carrier, is_heading))

    filler_count = 4
    filler_height = 10.0
    pages: list[Any] = []
    for page_idx in sorted(by_page):
        selected = by_page[page_idx]
        modal_votes = [filler_height] * filler_count + [
            round(float(carrier.bbox[3]) - float(carrier.bbox[1]), 2)
            for carrier, is_heading in selected
            if not is_heading
        ]
        modal_body = max(
            set(modal_votes),
            key=lambda height: (
                sum(1 for item in modal_votes if item == height),
                -height,
            ),
        )
        max_x = max(
            [float(carrier.bbox[2]) + 10.0 for carrier, _ in selected],
            default=100.0,
        )
        max_x = max(max_x, 100.0)

        atoms: list[Any] = []
        parts: list[str] = []
        offset = 0
        max_y = 100.0
        for carrier, is_heading in selected:
            text = carrier.source_value
            if parts:
                parts.append("\n")
                offset += 1
            start = offset
            parts.append(text)
            offset += len(text)
            box = tuple(float(value) for value in carrier.bbox)
            if is_heading:
                width = min(box[2] - box[0], 0.4 * max_x) or 0.4 * max_x
                x0 = (max_x - width) / 2.0
                box = (x0, box[1], x0 + width, box[1] + 1.5 * modal_body)
            max_y = max(max_y, box[3] + 10.0)
            atoms.append(
                NativeTextAtom(
                    page_idx=page_idx,
                    order=len(atoms),
                    bbox=(box[0], box[1], box[2], box[3]),
                    char_span=(start, offset),
                    text=text,
                    layout=NativeTextLayoutRef(0, len(atoms), 0, 0),
                )
            )
        # Unclaimed body ballast: real pages carry plain paragraph lines no
        # provider carrier claims; they pin the page's modal line height so
        # a display heading stays taller-than-body, as in a real filing.
        for index in range(filler_count):
            text = f"·基准正文填充第{index}行·"
            if parts:
                parts.append("\n")
                offset += 1
            start = offset
            parts.append(text)
            offset += len(text)
            y0 = max_y + index * (2 * filler_height)
            atoms.append(
                NativeTextAtom(
                    page_idx=page_idx,
                    order=len(atoms),
                    bbox=(10.0, y0, max_x - 10.0, y0 + filler_height),
                    char_span=(start, offset),
                    text=text,
                    layout=NativeTextLayoutRef(0, len(atoms), 0, 0),
                )
            )
        page_height = max_y + filler_count * (2 * filler_height) + 10.0
        pages.append(
            NativeTextPage(
                page_idx=page_idx,
                width=max_x,
                height=page_height,
                text="".join(parts),
                atoms=tuple(atoms),
                geometry_issues=(),
            )
        )
    if not pages:
        pages.append(
            NativeTextPage(
                page_idx=0,
                width=100.0,
                height=100.0,
                text="",
                atoms=(),
                geometry_issues=(),
            )
        )
    return tuple(pages)


def heading_intent_texts(kwargs: Mapping[str, Any]) -> frozenset[str]:
    """Collect the comparison values every structural lane proposes.

    Mirrors the producer's own candidate sources — legacy ``text_level``,
    v2 ``title`` blocks through the projection bijection, bookmarks, and
    StructTree heading roles — without deciding admission for any of them.
    """

    texts: set[str] = set()
    for item in kwargs.get("content_list", ()):
        level = item.get("text_level")
        if (
            not isinstance(level, bool)
            and isinstance(level, int)
            and level >= 1
            and isinstance(item.get("text"), str)
        ):
            texts.add(comparison_text(item["text"]))
    content_list_v2 = kwargs.get("content_list_v2")
    projections = kwargs.get("text_projections")
    if content_list_v2 is not None and projections is not None:
        content_list = kwargs.get("content_list", [])
        for page_idx, blocks in enumerate(content_list_v2):
            for block_idx, block in enumerate(blocks):
                if block.get("type") != "title":
                    continue
                try:
                    index = projections.legacy_index(page_idx, block_idx)
                except Exception:
                    continue
                if 0 <= index < len(content_list) and isinstance(
                    content_list[index].get("text"), str
                ):
                    texts.add(comparison_text(content_list[index]["text"]))
    native = kwargs.get("native")
    if native is not None:
        # Bookmarks are navigation claims, not rendering intent: a fixture
        # whose line is claimed only by a bookmark models a style-abused
        # body line and must stay at body geometry unless the test says
        # otherwise via ``heading_display_texts``.
        heading_refs = {
            ref
            for node in native.nodes
            if node.standard_role in {"H", "H1", "H2", "H3", "H4", "H5", "H6"}
            for ref in node.mcid_refs
        }
        for ref, objects in native.marked_objects.items():
            if ref not in heading_refs:
                continue
            for marked in objects:
                if marked.text:
                    texts.add(comparison_text(marked.text))
    return frozenset(value for value in texts if value)


def build_proof_with_auto_native(**kwargs: Any) -> dict[str, Any]:
    """Call the real producer, injecting a synthetic native lane if absent.

    ``heading_display_texts`` (test-only) adds carriers the fixture wants
    rendered as display lines beyond what the structural lanes imply — e.g.
    a bookmark-only fixture whose real-world analogue is a painted heading.
    ``body_texts`` (test-only) forces body geometry on carriers a lane
    claims as a heading — the analogue of a false provider title painted as
    ordinary body text.
    """

    from disclosure_anchor.adapters.parsers.mineru.structure_proof import (
        build_mineru_structure_proof,
    )

    extra_display = frozenset(
        comparison_text(value)
        for value in kwargs.pop("heading_display_texts", ())
    )
    forced_body = frozenset(
        comparison_text(value) for value in kwargs.pop("body_texts", ())
    )
    if "source_pages" not in kwargs or kwargs.get("source_pages") is None:
        content_list = kwargs["content_list"]
        pages = auto_native_pages(
            content_list,
            heading_texts=(
                (heading_intent_texts(kwargs) | extra_display) - forced_body
            ),
        )
        kwargs["source_pages"] = pages
        if kwargs.get("carrier_source_support") is None:
            kwargs["carrier_source_support"] = test_carrier_source_support(
                content_list,
                source_pages=pages,
                table_role_overrides=kwargs.get("table_role_overrides", ()),
            )
    return build_mineru_structure_proof(**kwargs)


def native_page_from_lines(
    lines: list[tuple[str, tuple[float, float, float, float]]],
    *,
    page_idx: int = 0,
    width: float = 400.0,
    height: float = 400.0,
) -> Any:
    """Build one native page from explicitly stated line geometry.

    Unlike :func:`auto_native_pages`, nothing is inferred: the fixture
    states each rendered line's text and box, so a test can model precise
    physical facts (a stacked display component, a body-band line) without
    deriving the geometry from the claim under test.
    """

    from disclosure_anchor.adapters.parsers.pdf_native_text import (
        NativeTextAtom,
        NativeTextLayoutRef,
        NativeTextPage,
    )

    atoms: list[Any] = []
    parts: list[str] = []
    offset = 0
    for text, box in lines:
        if parts:
            parts.append("\n")
            offset += 1
        start = offset
        parts.append(text)
        offset += len(text)
        atoms.append(
            NativeTextAtom(
                page_idx=page_idx,
                order=len(atoms),
                bbox=(
                    float(box[0]),
                    float(box[1]),
                    float(box[2]),
                    float(box[3]),
                ),
                char_span=(start, offset),
                text=text,
                layout=NativeTextLayoutRef(0, len(atoms), 0, 0),
            )
        )
    return NativeTextPage(
        page_idx=page_idx,
        width=width,
        height=height,
        text="".join(parts),
        atoms=tuple(atoms),
        geometry_issues=(),
    )
