"""Hash-bound, fixed-configuration PDF page and bbox visual evidence."""

from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
from io import BytesIO
import math
import os
from pathlib import Path
import re
from typing import Sequence, cast

from PIL import Image
from PIL import __version__ as PILLOW_VERSION
import pypdfium2 as pdfium

from disclosure_anchor.adapters.parsers.pdfium_runtime import PDFIUM_LOCK


_DPI = 300
_PDF_POINTS_PER_INCH = 72
_MEDIA_TYPE = "image/png"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class PdfVisualEvidenceError(RuntimeError):
    """The source PDF or rendered evidence violated the closed contract."""

    reason_code = "pdf_visual_evidence_error"


@dataclass(frozen=True, slots=True)
class PdfiumRendererIdentity:
    library: str
    library_version: str
    engine: str
    engine_version: str
    engine_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FullPageRenderOptions:
    dpi: int
    scale_numerator: int
    scale_denominator: int
    rotation: int
    crop: tuple[int, int, int, int]
    may_draw_forms: bool
    color_scheme: str | None
    fill_to_stroke: bool
    fill_color: tuple[int, int, int, int]
    grayscale: bool
    optimize_mode: str | None
    draw_annots: bool
    no_smoothtext: bool
    no_smoothimage: bool
    no_smoothpath: bool
    force_halftone: bool
    limit_image_cache: bool
    rev_byteorder: bool
    force_bitmap_format: int
    extra_flags: int


@dataclass(frozen=True, slots=True)
class LosslessPngOptions:
    encoder: str
    encoder_version: str
    format: str
    color_mode: str
    optimize: bool
    compress_level: int
    interlace: int
    dpi: tuple[int, int]
    metadata_policy: str


@dataclass(frozen=True, slots=True)
class VisualPageEvidence:
    page_idx: int
    artifact_role: str
    artifact_path: Path
    sha256: str
    size_bytes: int
    pixel_width: int
    pixel_height: int
    media_type: str
    renderer: PdfiumRendererIdentity
    render_options: FullPageRenderOptions
    png_options: LosslessPngOptions
    bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True, slots=True)
class VisualRegionRequest:
    page_idx: int
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class VisualOccurrenceRequest:
    source_item_index: int
    page_idx: int
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class RenderedVisualEvidence:
    pages: tuple[VisualPageEvidence, ...]
    regions: tuple[VisualPageEvidence, ...]
    occurrences: tuple[VisualPageEvidence, ...]


@dataclass(frozen=True, slots=True)
class _VisualTarget:
    role: str
    page_idx: int
    bbox: tuple[float, float, float, float] | None


RENDERER_IDENTITY_FIELDS = frozenset(
    field.name for field in fields(PdfiumRendererIdentity)
)
RENDER_OPTIONS_FIELDS = frozenset(
    field.name for field in fields(FullPageRenderOptions)
)
PNG_OPTIONS_FIELDS = frozenset(field.name for field in fields(LosslessPngOptions))


RENDERER_IDENTITY = PdfiumRendererIdentity(
    library="pypdfium2",
    library_version=str(pdfium.PYPDFIUM_INFO),
    engine="PDFium",
    engine_version=str(pdfium.PDFIUM_INFO),
    engine_flags=tuple(str(flag) for flag in pdfium.PDFIUM_INFO.flags),
)
RENDER_OPTIONS = FullPageRenderOptions(
    dpi=_DPI,
    scale_numerator=_DPI,
    scale_denominator=_PDF_POINTS_PER_INCH,
    rotation=0,
    crop=(0, 0, 0, 0),
    may_draw_forms=True,
    color_scheme=None,
    fill_to_stroke=False,
    fill_color=(255, 255, 255, 255),
    grayscale=False,
    optimize_mode=None,
    draw_annots=True,
    no_smoothtext=False,
    no_smoothimage=False,
    no_smoothpath=False,
    force_halftone=False,
    limit_image_cache=False,
    rev_byteorder=True,
    force_bitmap_format=int(pdfium.raw.FPDFBitmap_BGR),
    extra_flags=0,
)
PNG_OPTIONS = LosslessPngOptions(
    encoder="Pillow",
    encoder_version=PILLOW_VERSION,
    format="PNG",
    color_mode="RGB",
    optimize=False,
    compress_level=9,
    interlace=0,
    dpi=(_DPI, _DPI),
    metadata_policy="fixed_dpi_only",
)


def render_pdf_visual_evidence(
    pdf_path: Path,
    expected_pdf_sha256: str,
    *,
    full_pages: Sequence[int],
    regions: Sequence[VisualRegionRequest],
    occurrences: Sequence[VisualOccurrenceRequest],
    artifact_dir: Path,
) -> RenderedVisualEvidence:
    """Render every requested view with one open and one raster per page."""

    if _SHA256_RE.fullmatch(expected_pdf_sha256) is None:
        raise PdfVisualEvidenceError("expected PDF sha256 is invalid")
    page_targets = tuple(
        _VisualTarget(_artifact_role(page_idx), page_idx, None)
        for page_idx in _validate_page_indices(full_pages)
    )
    components = merged_visual_region_components(regions)
    region_targets = tuple(
        _VisualTarget(
            (
                f"source_bbox_visual_{component.page_idx + 1:06d}_"
                f"{component_idx + 1:06d}"
            ),
            component.page_idx,
            component.bbox,
        )
        for component_idx, component in _page_component_indices(components)
    )
    seen_occurrences: set[int] = set()
    occurrence_targets: list[_VisualTarget] = []
    for occurrence in occurrences:
        source_index = occurrence.source_item_index
        if (
            isinstance(source_index, bool)
            or not isinstance(source_index, int)
            or source_index < 0
            or source_index in seen_occurrences
            or isinstance(occurrence.page_idx, bool)
            or not isinstance(occurrence.page_idx, int)
            or occurrence.page_idx < 0
        ):
            raise PdfVisualEvidenceError(
                "visual occurrence identity or page is invalid or duplicated"
            )
        seen_occurrences.add(source_index)
        occurrence_targets.append(
            _VisualTarget(
                f"source_visual_occurrence_{source_index:06d}",
                occurrence.page_idx,
                _normalized_bbox(occurrence.bbox),
            )
        )
    occurrence_targets.sort(
        key=lambda item: (
            item.page_idx,
            *(item.bbox or ()),
            item.role,
        )
    )
    all_targets = (*page_targets, *region_targets, *occurrence_targets)
    roles = [target.role for target in all_targets]
    if (
        len(roles) != len(set(roles))
        or any(_ARTIFACT_ROLE_RE.fullmatch(role) is None for role in roles)
    ):
        raise PdfVisualEvidenceError("visual target role is invalid or duplicated")

    created_paths: list[Path] = []
    created_dir = False
    document: pdfium.PdfDocument | None = None
    descriptors: dict[str, VisualPageEvidence] = {}
    try:
        _require_pdf_hash(pdf_path, expected_pdf_sha256, phase="before rendering")
        try:
            if all_targets:
                document, page_count = _open_pdf(pdf_path)
                page_indices = tuple(
                    dict.fromkeys(target.page_idx for target in all_targets)
                )
                _require_page_bounds(page_indices, page_count)
                target_paths = tuple(
                    artifact_dir / f"{target.role}.png"
                    for target in all_targets
                )
                created_dir = _prepare_artifact_dir(
                    artifact_dir,
                    target_paths,
                )
                path_by_role = {
                    target.role: path
                    for target, path in zip(
                        all_targets,
                        target_paths,
                        strict=True,
                    )
                }
                targets_by_page: dict[int, list[_VisualTarget]] = {}
                for target in all_targets:
                    targets_by_page.setdefault(target.page_idx, []).append(
                        target
                    )
                for page_idx in sorted(targets_by_page):
                    page_targets_for_render = targets_by_page[page_idx]
                    image = rasterize_pdf_visual_page(document, page_idx)
                    try:
                        for target in page_targets_for_render:
                            target_path = path_by_role[target.role]
                            if target.bbox is None:
                                descriptor = _write_page(
                                    image,
                                    page_idx,
                                    target_path,
                                    created_paths,
                                )
                            else:
                                crop = _crop_normalized_bbox(
                                    image,
                                    target.bbox,
                                )
                                try:
                                    descriptor = _write_region(
                                        crop,
                                        VisualRegionRequest(
                                            page_idx,
                                            target.bbox,
                                        ),
                                        target_path,
                                        created_paths,
                                    )
                                finally:
                                    crop.close()
                            descriptors[target.role] = descriptor
                    finally:
                        image.close()
        finally:
            try:
                if document is not None:
                    with PDFIUM_LOCK:
                        document.close()
            finally:
                _require_pdf_hash(
                    pdf_path,
                    expected_pdf_sha256,
                    phase="after rendering",
                )
    except Exception as exc:
        cleanup_errors = _cleanup_created(created_paths, artifact_dir, created_dir)
        if cleanup_errors:
            details = "; ".join(cleanup_errors)
            raise PdfVisualEvidenceError(
                f"visual evidence rendering failed and cleanup was incomplete: {details}"
            ) from exc
        if isinstance(exc, PdfVisualEvidenceError):
            raise
        raise PdfVisualEvidenceError(f"visual evidence rendering failed: {exc}") from exc
    return RenderedVisualEvidence(
        pages=tuple(descriptors[target.role] for target in page_targets),
        regions=tuple(descriptors[target.role] for target in region_targets),
        occurrences=tuple(
            descriptors[target.role] for target in occurrence_targets
        ),
    )


def _validate_page_indices(page_indices: Sequence[int]) -> tuple[int, ...]:
    try:
        indices = tuple(page_indices)
    except TypeError as exc:
        raise PdfVisualEvidenceError("page_indices must be a finite sequence") from exc
    if any(isinstance(value, bool) or not isinstance(value, int) for value in indices):
        raise PdfVisualEvidenceError("page_indices must contain only integers")
    if len(indices) != len(set(indices)):
        raise PdfVisualEvidenceError("page_indices must not contain duplicates")
    return indices


def merged_visual_region_components(
    regions: Sequence[VisualRegionRequest],
) -> tuple[VisualRegionRequest, ...]:
    pending: list[VisualRegionRequest] = []
    for region in regions:
        if (
            isinstance(region.page_idx, bool)
            or not isinstance(region.page_idx, int)
            or region.page_idx < 0
        ):
            raise PdfVisualEvidenceError("visual region page_idx is invalid")
        bbox = _normalized_bbox(region.bbox)
        pending.append(VisualRegionRequest(region.page_idx, bbox))
    pending.sort(key=lambda item: (item.page_idx, *item.bbox))

    components: list[VisualRegionRequest] = []
    for region in pending:
        merged = region
        retained: list[VisualRegionRequest] = []
        for component in components:
            if (
                component.page_idx == merged.page_idx
                and _bbox_overlaps(component.bbox, merged.bbox)
            ):
                merged = VisualRegionRequest(
                    merged.page_idx,
                    _bbox_union(component.bbox, merged.bbox),
                )
            else:
                retained.append(component)
        while True:
            overlap = next(
                (
                    component
                    for component in retained
                    if component.page_idx == merged.page_idx
                    and _bbox_overlaps(component.bbox, merged.bbox)
                ),
                None,
            )
            if overlap is None:
                break
            retained.remove(overlap)
            merged = VisualRegionRequest(
                merged.page_idx,
                _bbox_union(overlap.bbox, merged.bbox),
            )
        retained.append(merged)
        components = retained
    return tuple(sorted(components, key=lambda item: (item.page_idx, *item.bbox)))


def _page_component_indices(
    components: Sequence[VisualRegionRequest],
) -> tuple[tuple[int, VisualRegionRequest], ...]:
    page_counts: dict[int, int] = {}
    result: list[tuple[int, VisualRegionRequest]] = []
    for component in components:
        component_idx = page_counts.get(component.page_idx, 0)
        result.append((component_idx, component))
        page_counts[component.page_idx] = component_idx + 1
    return tuple(result)


def _normalized_bbox(value: object) -> tuple[float, float, float, float]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(
            isinstance(item, bool) or not isinstance(item, (int, float))
            for item in value
        )
    ):
        raise PdfVisualEvidenceError("visual region bbox is invalid")
    bbox = tuple(float(item) for item in value)
    if (
        not all(math.isfinite(item) for item in bbox)
        or min(bbox) < 0
        or max(bbox) > 1000
        or bbox[0] >= bbox[2]
        or bbox[1] >= bbox[3]
    ):
        raise PdfVisualEvidenceError("visual region bbox is invalid")
    return cast(tuple[float, float, float, float], bbox)


def _bbox_overlaps(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return (
        left[0] < right[2]
        and right[0] < left[2]
        and left[1] < right[3]
        and right[1] < left[3]
    )


def _bbox_union(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return (
        min(left[0], right[0]),
        min(left[1], right[1]),
        max(left[2], right[2]),
        max(left[3], right[3]),
    )


def _require_page_bounds(indices: tuple[int, ...], page_count: int) -> None:
    invalid = tuple(index for index in indices if index < 0 or index >= page_count)
    if invalid:
        raise PdfVisualEvidenceError(
            f"page_indices out of range for {page_count} pages: {invalid}"
        )


def _prepare_artifact_dir(
    artifact_dir: Path,
    targets: tuple[Path, ...],
) -> bool:
    created = False
    if artifact_dir.exists():
        if not artifact_dir.is_dir():
            raise PdfVisualEvidenceError(
                f"artifact directory is not a directory: {artifact_dir}"
            )
    else:
        try:
            artifact_dir.mkdir(parents=True)
        except OSError as exc:
            raise PdfVisualEvidenceError(
                f"cannot create artifact directory: {artifact_dir}"
            ) from exc
        created = True
    existing = tuple(path for path in targets if path.exists())
    if existing:
        raise PdfVisualEvidenceError(
            "visual evidence target already exists: "
            + ", ".join(str(path) for path in existing)
        )
    return created


def _write_page(
    image: Image.Image,
    page_idx: int,
    target: Path,
    created_paths: list[Path],
) -> VisualPageEvidence:
    png_bytes = _encode_png(image)
    pixel_width, pixel_height = image.size
    _write_exclusive(target, png_bytes, created_paths)
    role = _artifact_role(page_idx)
    return VisualPageEvidence(
        page_idx=page_idx,
        artifact_role=role,
        artifact_path=target,
        sha256="sha256:" + hashlib.sha256(png_bytes).hexdigest(),
        size_bytes=len(png_bytes),
        pixel_width=pixel_width,
        pixel_height=pixel_height,
        media_type=_MEDIA_TYPE,
        renderer=RENDERER_IDENTITY,
        render_options=RENDER_OPTIONS,
        png_options=PNG_OPTIONS,
    )


def _crop_normalized_bbox(
    image: Image.Image,
    bbox: tuple[float, float, float, float],
) -> Image.Image:
    width, height = image.size
    pixel_bbox = (
        max(0, math.floor(width * bbox[0] / 1000.0)),
        max(0, math.floor(height * bbox[1] / 1000.0)),
        min(width, math.ceil(width * bbox[2] / 1000.0)),
        min(height, math.ceil(height * bbox[3] / 1000.0)),
    )
    if pixel_bbox[0] >= pixel_bbox[2] or pixel_bbox[1] >= pixel_bbox[3]:
        raise PdfVisualEvidenceError("visual region maps to an empty pixel crop")
    return image.crop(pixel_bbox)


def _write_region(
    image: Image.Image,
    region: VisualRegionRequest,
    target: Path,
    created_paths: list[Path],
) -> VisualPageEvidence:
    png_bytes = _encode_png(image)
    _write_exclusive(target, png_bytes, created_paths)
    return VisualPageEvidence(
        page_idx=region.page_idx,
        artifact_role=target.stem,
        artifact_path=target,
        sha256="sha256:" + hashlib.sha256(png_bytes).hexdigest(),
        size_bytes=len(png_bytes),
        pixel_width=image.width,
        pixel_height=image.height,
        media_type=_MEDIA_TYPE,
        renderer=RENDERER_IDENTITY,
        render_options=RENDER_OPTIONS,
        png_options=PNG_OPTIONS,
        bbox=region.bbox,
    )


def _open_pdf(pdf_path: Path) -> tuple[pdfium.PdfDocument, int]:
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(pdf_path)
        try:
            document.init_forms()
            page_count = len(document)
        except Exception:
            document.close()
            raise
    return document, page_count


def rasterize_pdf_visual_page(
    document: pdfium.PdfDocument,
    page_idx: int,
) -> Image.Image:
    with PDFIUM_LOCK:
        page = document[page_idx]
        try:
            bitmap = page.render(
                scale=(
                    RENDER_OPTIONS.scale_numerator
                    / RENDER_OPTIONS.scale_denominator
                ),
                rotation=RENDER_OPTIONS.rotation,
                crop=RENDER_OPTIONS.crop,
                may_draw_forms=RENDER_OPTIONS.may_draw_forms,
                color_scheme=RENDER_OPTIONS.color_scheme,
                fill_to_stroke=RENDER_OPTIONS.fill_to_stroke,
                fill_color=RENDER_OPTIONS.fill_color,
                grayscale=RENDER_OPTIONS.grayscale,
                optimize_mode=RENDER_OPTIONS.optimize_mode,
                draw_annots=RENDER_OPTIONS.draw_annots,
                no_smoothtext=RENDER_OPTIONS.no_smoothtext,
                no_smoothimage=RENDER_OPTIONS.no_smoothimage,
                no_smoothpath=RENDER_OPTIONS.no_smoothpath,
                force_halftone=RENDER_OPTIONS.force_halftone,
                limit_image_cache=RENDER_OPTIONS.limit_image_cache,
                rev_byteorder=RENDER_OPTIONS.rev_byteorder,
                force_bitmap_format=RENDER_OPTIONS.force_bitmap_format,
                extra_flags=RENDER_OPTIONS.extra_flags,
            )
            try:
                rendered = cast(Image.Image, bitmap.to_pil())
                try:
                    if rendered.mode != PNG_OPTIONS.color_mode:
                        raise PdfVisualEvidenceError(
                            f"unexpected rendered pixel mode: {rendered.mode}"
                        )
                    return rendered.copy()
                finally:
                    rendered.close()
            finally:
                bitmap.close()
        finally:
            page.close()


def _encode_png(image: Image.Image) -> bytes:
    image.info.clear()
    output = BytesIO()
    image.save(
        output,
        format=PNG_OPTIONS.format,
        optimize=PNG_OPTIONS.optimize,
        compress_level=PNG_OPTIONS.compress_level,
        interlace=PNG_OPTIONS.interlace,
        dpi=PNG_OPTIONS.dpi,
        pnginfo=None,
        icc_profile=None,
        exif=b"",
    )
    return output.getvalue()


def _write_exclusive(
    target: Path,
    payload: bytes,
    created_paths: list[Path],
) -> None:
    try:
        with target.open("xb") as stream:
            created_paths.append(target)
            written = stream.write(payload)
            if written != len(payload):
                raise OSError(f"short write: expected {len(payload)}, got {written}")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise PdfVisualEvidenceError(f"cannot write visual evidence: {target}") from exc


def _artifact_role(page_idx: int) -> str:
    return f"source_page_visual_{page_idx + 1:06d}"


def _cleanup_created(
    created_paths: list[Path],
    artifact_dir: Path,
    created_dir: bool,
) -> tuple[str, ...]:
    errors: list[str] = []
    for path in reversed(created_paths):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    if created_dir:
        try:
            artifact_dir.rmdir()
        except OSError as exc:
            errors.append(f"{artifact_dir}: {exc}")
    return tuple(errors)


def _require_pdf_hash(path: Path, expected: str, *, phase: str) -> None:
    try:
        with path.open("rb") as stream:
            actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise PdfVisualEvidenceError(f"cannot hash source PDF: {path}") from exc
    if actual != expected:
        raise PdfVisualEvidenceError(
            f"source PDF hash mismatch {phase}: expected {expected}, got {actual}"
        )
