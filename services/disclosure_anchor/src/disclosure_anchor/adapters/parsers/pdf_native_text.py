"""Hash-bound Poppler extraction of page text, words, and source geometry."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import math
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET


BBox = tuple[float, float, float, float]
_DEFAULT_TIMEOUT_SECONDS = 300
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
NATIVE_TEXT_RUN_ALGORITHM = "poppler-line-geometry-contiguous.v2"


class NativeTextExtractionError(RuntimeError):
    """The immutable PDF or Poppler output violated the extraction contract."""

    reason_code = "native_text_extraction_error"


@dataclass(frozen=True)
class NativeTextLayoutRef:
    """Poppler XML layout ancestry for one immutable native word occurrence."""

    flow_index: int
    block_index: int
    line_index: int
    word_index: int

    @property
    def line_ref(self) -> tuple[int, int, int]:
        return self.flow_index, self.block_index, self.line_index


@dataclass(frozen=True)
class NativeTextAtom:
    page_idx: int
    order: int
    bbox: BBox
    char_span: tuple[int, int]
    text: str
    layout: NativeTextLayoutRef


@dataclass(frozen=True)
class NativeTextWordGeometry:
    """One Poppler ``<word>`` node, independent of Unicode decoding."""

    page_idx: int
    order: int
    bbox: BBox
    layout: NativeTextLayoutRef


@dataclass(frozen=True, slots=True)
class NativeTextRun:
    """Maximal Poppler-line run whose adjacent word boxes physically touch."""

    page_idx: int
    layout_line: tuple[int, int, int]
    atom_orders: tuple[int, ...]
    bbox: BBox


@dataclass(frozen=True)
class NativeTextGeometryIssue:
    """One Poppler ``<word>`` node whose page geometry is unusable."""

    page_idx: int
    word_order: int
    text: str
    raw_bbox: BBox | None
    reason: str


@dataclass(frozen=True)
class NativeTextPage:
    page_idx: int
    width: float
    height: float
    text: str
    atoms: tuple[NativeTextAtom, ...]
    geometry_issues: tuple[NativeTextGeometryIssue, ...] = ()
    word_geometries: tuple[NativeTextWordGeometry, ...] = ()
    word_inventory_complete: bool = False


@dataclass(frozen=True)
class NativeTextExtraction:
    pages: tuple[NativeTextPage, ...]
    pdftotext_version: str
    pdfinfo_version: str


def visual_guard_page_indices(
    pages: Sequence[NativeTextPage],
) -> tuple[int, ...]:
    """Return exactly the pages that lack complete usable native geometry."""

    return tuple(
        page.page_idx for page in pages if not page.atoms or page.geometry_issues
    )


def native_text_runs(page: NativeTextPage) -> tuple[NativeTextRun, ...]:
    """Derive layout runs without treating each Poppler word as semantic."""

    runs: list[NativeTextRun] = []
    current: list[NativeTextAtom] = []

    def flush() -> None:
        if not current:
            return
        runs.append(
            NativeTextRun(
                page_idx=page.page_idx,
                layout_line=current[0].layout.line_ref,
                atom_orders=tuple(atom.order for atom in current),
                bbox=_union_bbox(current),
            )
        )
        current.clear()

    for atom in page.atoms:
        if atom.page_idx != page.page_idx:
            raise NativeTextExtractionError(
                "native atom page differs from its page container"
            )
        if current and not _same_geometry_run(current[-1], atom):
            flush()
        current.append(atom)
    flush()
    return tuple(runs)


def parse_pdftotext_bbox(xml_payload: str) -> tuple[NativeTextPage, ...]:
    """Parse ``pdftotext -bbox-layout`` into line-preserving page text."""

    try:
        root = ET.fromstring(xml_payload)
    except ET.ParseError as exc:
        raise NativeTextExtractionError(f"invalid pdftotext bbox XML: {exc}") from exc
    pages: list[NativeTextPage] = []
    page_nodes = [node for node in root.iter() if _tag(node) == "page"]
    if not page_nodes:
        raise NativeTextExtractionError("pdftotext bbox XML contains no pages")
    for page_idx, page_node in enumerate(page_nodes):
        error = f"page {page_idx} dimensions are invalid"
        width = _float_attr(page_node, "width", error)
        height = _float_attr(page_node, "height", error)
        if min(width, height) <= 0:
            raise NativeTextExtractionError(error)
        all_words = {id(node) for node in page_node.iter() if _tag(node) == "word"}
        seen_words: set[int] = set()
        chunks: list[str] = []
        atoms: list[NativeTextAtom] = []
        word_geometries: list[NativeTextWordGeometry] = []
        geometry_issues: list[NativeTextGeometryIssue] = []
        offset = 0
        word_order = 0
        seen_lines: set[int] = set()
        all_lines = {
            id(node) for node in page_node.iter() if _tag(node) == "line"
        }
        for flow_index, flow_node in enumerate(
            _children(page_node, "flow")
        ):
            for block_index, block_node in enumerate(
                _children(flow_node, "block")
            ):
                for line_index, line_node in enumerate(
                    _children(block_node, "line")
                ):
                    seen_lines.add(id(line_node))
                    words: list[
                        tuple[ET.Element, str, int, NativeTextLayoutRef]
                    ] = []
                    for word_index, word_node in enumerate(
                        _children(line_node, "word")
                    ):
                        seen_words.add(id(word_node))
                        # Payload text stays verbatim ToUnicode output;
                        # every matching layer folds width/compatibility in
                        # its own comparison space (NFKC there, not here).
                        text = "".join(
                            char
                            for char in "".join(word_node.itertext())
                            if not char.isspace()
                        )
                        words.append(
                            (
                                word_node,
                                text,
                                word_order,
                                NativeTextLayoutRef(
                                    flow_index=flow_index,
                                    block_index=block_index,
                                    line_index=line_index,
                                    word_index=word_index,
                                ),
                            )
                        )
                        word_order += 1
                    if not words:
                        continue
                    line_has_atom = False
                    for (
                        word_node,
                        text,
                        source_word_order,
                        layout,
                    ) in words:
                        bbox, issue_reason = _word_geometry(word_node)
                        if issue_reason is not None:
                            geometry_issues.append(
                                NativeTextGeometryIssue(
                                    page_idx=page_idx,
                                    word_order=source_word_order,
                                    text=text,
                                    raw_bbox=bbox,
                                    reason=issue_reason,
                                )
                            )
                            continue
                        assert bbox is not None
                        word_geometries.append(
                            NativeTextWordGeometry(
                                page_idx=page_idx,
                                order=source_word_order,
                                bbox=bbox,
                                layout=layout,
                            )
                        )
                        if not text:
                            continue
                        if chunks:
                            separator = "\n" if not line_has_atom else " "
                            chunks.append(separator)
                            offset += len(separator)
                        line_has_atom = True
                        start = offset
                        chunks.append(text)
                        offset += len(text)
                        atoms.append(
                            NativeTextAtom(
                                page_idx=page_idx,
                                order=source_word_order,
                                bbox=bbox,
                                char_span=(start, offset),
                                text=text,
                                layout=layout,
                            )
                        )
        if seen_lines != all_lines:
            raise NativeTextExtractionError(
                f"page {page_idx} contains lines outside Poppler flow/block structure"
            )
        if seen_words != all_words:
            raise NativeTextExtractionError(
                f"page {page_idx} contains words outside Poppler line structure"
            )
        pages.append(
            NativeTextPage(
                page_idx=page_idx,
                width=width,
                height=height,
                text="".join(chunks),
                atoms=tuple(atoms),
                geometry_issues=tuple(geometry_issues),
                word_geometries=tuple(word_geometries),
                word_inventory_complete=(
                    len(word_geometries) + len(geometry_issues) == len(all_words)
                ),
            )
        )
    return tuple(pages)


def extract_native_pages(
    pdf_path: Path,
    expected_pdf_sha256: str,
    pdftotext_binary: str = "pdftotext",
    pdfinfo_binary: str = "pdfinfo",
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> NativeTextExtraction:
    """Extract every page while proving source hash and page-count closure."""

    if _SHA256_RE.fullmatch(expected_pdf_sha256) is None:
        raise NativeTextExtractionError("expected PDF sha256 is invalid")
    _require_pdf_hash(pdf_path, expected_pdf_sha256, phase="before extraction")
    try:
        text_result = _run(
            (
                pdftotext_binary,
                "-bbox-layout",
                "-enc",
                "UTF-8",
                str(pdf_path),
                "-",
            ),
            timeout_seconds,
            "pdftotext failed",
        )
        info_result = _run(
            (pdfinfo_binary, str(pdf_path)),
            timeout_seconds,
            "pdfinfo failed",
        )
    finally:
        _require_pdf_hash(pdf_path, expected_pdf_sha256, phase="after extraction")
    page_match = re.search(r"^Pages:\s*(\d+)\s*$", info_result.stdout, re.MULTILINE)
    if page_match is None:
        raise NativeTextExtractionError("pdfinfo output has no page count")
    pages = parse_pdftotext_bbox(text_result.stdout)
    if len(pages) != int(page_match.group(1)):
        raise NativeTextExtractionError(
            "native page closure mismatch: "
            f"pdfinfo={page_match.group(1)}, bbox_pages={len(pages)}"
        )
    return NativeTextExtraction(
        pages=pages,
        pdftotext_version=poppler_tool_version(pdftotext_binary, timeout_seconds),
        pdfinfo_version=poppler_tool_version(pdfinfo_binary, timeout_seconds),
    )


@lru_cache(maxsize=16)
def poppler_tool_version(
    binary: str,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> str:
    result = _run(
        (binary, "-v"),
        timeout_seconds,
        f"cannot identify {binary}",
    )
    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )
    if not output:
        raise NativeTextExtractionError(f"cannot identify {binary}: empty output")
    return output.splitlines()[0]


def _run(
    args: tuple[str, ...],
    timeout_seconds: int,
    error: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeTextExtractionError(f"{error}: {exc}") from exc
    if result.returncode:
        raise NativeTextExtractionError(
            f"{error}: {result.stderr.strip() or result.returncode}"
        )
    return result


def _tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _children(node: ET.Element, tag: str) -> list[ET.Element]:
    return [child for child in node if _tag(child) == tag]


def _float_attr(node: ET.Element, name: str, error: str) -> float:
    try:
        value = float(node.attrib[name])
    except (KeyError, ValueError) as exc:
        raise NativeTextExtractionError(error) from exc
    if not math.isfinite(value):
        raise NativeTextExtractionError(error)
    return value


def _word_geometry(node: ET.Element) -> tuple[BBox | None, str | None]:
    values: list[float] = []
    for key in ("xMin", "yMin", "xMax", "yMax"):
        try:
            value = float(node.attrib[key])
        except (KeyError, ValueError):
            return None, "bbox_missing_or_non_finite"
        if not math.isfinite(value):
            return None, "bbox_missing_or_non_finite"
        values.append(value)
    bbox = (values[0], values[1], values[2], values[3])
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        return bbox, "bbox_non_positive_extent"
    return bbox, None


def _same_geometry_run(
    left: NativeTextAtom,
    right: NativeTextAtom,
) -> bool:
    if (
        left.page_idx != right.page_idx
        or left.layout.line_ref != right.layout.line_ref
        or right.order != left.order + 1
        or right.layout.word_index <= left.layout.word_index
    ):
        return False
    left_height = left.bbox[3] - left.bbox[1]
    right_height = right.bbox[3] - right.bbox[1]
    min_height = min(left_height, right_height)
    if min_height <= 0:
        return False
    vertical_overlap = min(left.bbox[3], right.bbox[3]) - max(
        left.bbox[1], right.bbox[1]
    )
    left_center_y = (left.bbox[1] + left.bbox[3]) / 2
    right_center_y = (right.bbox[1] + right.bbox[3]) / 2
    gap = right.bbox[0] - left.bbox[2]
    return bool(
        right.bbox[0] >= left.bbox[0]
        and right.bbox[2] >= left.bbox[2]
        and vertical_overlap / min_height >= 0.8
        and abs(right_center_y - left_center_y) <= 0.2 * min_height
        and -0.1 * min_height <= gap <= 0.1 * min_height
    )


def _union_bbox(atoms: Sequence[NativeTextAtom]) -> BBox:
    return (
        min(atom.bbox[0] for atom in atoms),
        min(atom.bbox[1] for atom in atoms),
        max(atom.bbox[2] for atom in atoms),
        max(atom.bbox[3] for atom in atoms),
    )


def _require_pdf_hash(path: Path, expected: str, *, phase: str) -> None:
    try:
        with path.open("rb") as stream:
            actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise NativeTextExtractionError(f"cannot hash source PDF: {path}") from exc
    if actual != expected:
        raise NativeTextExtractionError(
            f"source PDF hash mismatch {phase}: expected {expected}, got {actual}"
        )
