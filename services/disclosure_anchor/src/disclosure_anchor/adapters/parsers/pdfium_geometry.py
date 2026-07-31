"""One PDFium geometry contract for structure and paint evidence."""

from __future__ import annotations

from dataclasses import dataclass
import math

import pypdfium2 as pdfium

from disclosure_anchor.domain.errors import ParserOutputContractError


BBox = tuple[float, float, float, float]
_DEVICE_EXTENT = 1_000_000


@dataclass(frozen=True, slots=True)
class PageScreenGeometry:
    display_box: BBox
    rotation: int
    converter: pdfium.PdfPosConv


def page_screen_geometry(page: pdfium.PdfPage) -> PageScreenGeometry:
    display_box = _valid_rect(page.get_bbox(), allow_degenerate=False)
    rotation = page.get_rotation()
    if rotation not in {0, 90, 180, 270}:
        raise ParserOutputContractError(
            f"PDFium returned an unsupported page rotation: {rotation}"
        )
    return PageScreenGeometry(
        display_box=display_box,
        rotation=rotation,
        converter=pdfium.PdfPosConv(
            page,
            (0, 0, _DEVICE_EXTENT, _DEVICE_EXTENT, 0),
        ),
    )


def compose_form_ancestor(
    form_matrix: pdfium.PdfMatrix,
    parent_ancestor: pdfium.PdfMatrix,
) -> pdfium.PdfMatrix:
    """Compose child-local Form then outer Form in PDF row-vector order."""

    _validate_matrix(form_matrix)
    _validate_matrix(parent_ancestor)
    output = form_matrix.multiply(parent_ancestor)
    _validate_matrix(output)
    return output


def normalized_screen_point(
    geometry: PageScreenGeometry,
    x: float,
    y: float,
) -> tuple[float, float]:
    if not math.isfinite(x) or not math.isfinite(y):
        raise ParserOutputContractError("PDF point coordinates are not finite")
    try:
        device_x, device_y = geometry.converter.to_bitmap(x, y)
    except pdfium.PdfiumError as exc:
        raise ParserOutputContractError(
            "PDFium cannot map a PDF point into screen coordinates"
        ) from exc
    if not math.isfinite(device_x) or not math.isfinite(device_y):
        raise ParserOutputContractError(
            "PDFium mapped a PDF point to non-finite coordinates"
        )
    return (
        1000.0 * device_x / _DEVICE_EXTENT,
        1000.0 * device_y / _DEVICE_EXTENT,
    )


def normalized_screen_bbox(
    geometry: PageScreenGeometry,
    bounds: BBox,
    *,
    ancestor: pdfium.PdfMatrix | None = None,
) -> BBox | None:
    """Map one occurrence into the rendered page, preserving off-page objects."""

    source = _valid_rect(bounds, allow_degenerate=True)
    if source[0] == source[2] or source[1] == source[3]:
        return None
    if ancestor is not None:
        _validate_matrix(ancestor)
        source = _valid_rect(
            ancestor.on_rect(*source),
            allow_degenerate=True,
        )
        if source[0] == source[2] or source[1] == source[3]:
            return None
    display = geometry.display_box
    clipped = (
        max(source[0], display[0]),
        max(source[1], display[1]),
        min(source[2], display[2]),
        min(source[3], display[3]),
    )
    if clipped[0] >= clipped[2] or clipped[1] >= clipped[3]:
        return None
    corners = (
        normalized_screen_point(geometry, clipped[0], clipped[1]),
        normalized_screen_point(geometry, clipped[0], clipped[3]),
        normalized_screen_point(geometry, clipped[2], clipped[1]),
        normalized_screen_point(geometry, clipped[2], clipped[3]),
    )
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    return (
        min(1000.0, max(0.0, min(xs))),
        min(1000.0, max(0.0, min(ys))),
        min(1000.0, max(0.0, max(xs))),
        min(1000.0, max(0.0, max(ys))),
    )


def matrix_values(matrix: pdfium.PdfMatrix) -> tuple[float, ...]:
    _validate_matrix(matrix)
    return (
        float(matrix.a),
        float(matrix.b),
        float(matrix.c),
        float(matrix.d),
        float(matrix.e),
        float(matrix.f),
    )


def _validate_matrix(matrix: pdfium.PdfMatrix) -> None:
    if not all(
        math.isfinite(value)
        for value in (
            matrix.a,
            matrix.b,
            matrix.c,
            matrix.d,
            matrix.e,
            matrix.f,
        )
    ):
        raise ParserOutputContractError("PDF Form matrix is not finite")


def _valid_rect(
    value: tuple[float, float, float, float],
    *,
    allow_degenerate: bool,
) -> BBox:
    left, bottom, right, top = (float(item) for item in value)
    if not all(math.isfinite(item) for item in (left, bottom, right, top)):
        raise ParserOutputContractError("PDF rectangle coordinates are not finite")
    invalid = (
        left > right or bottom > top
        if allow_degenerate
        else left >= right or bottom >= top
    )
    if invalid:
        raise ParserOutputContractError("PDF rectangle has inverted or empty bounds")
    return left, bottom, right, top


__all__ = [
    "BBox",
    "PageScreenGeometry",
    "compose_form_ancestor",
    "matrix_values",
    "normalized_screen_bbox",
    "normalized_screen_point",
    "page_screen_geometry",
]
