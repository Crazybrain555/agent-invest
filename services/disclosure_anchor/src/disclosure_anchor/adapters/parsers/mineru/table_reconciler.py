"""Prove page-local closure between MinerU content and model tables.

Production disables MinerU's cross-page table merge.  Each canonical
``content_list`` table must therefore describe exactly one physical page and
must have one uniquely equal table in ``*_model.json`` on that same page.
This module validates that closed relation; it never repairs or annotates the
provider payload.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re
from typing import Any

from disclosure_anchor.adapters.parsers.mineru.geometry import is_page_index
from disclosure_anchor.adapters.parsers.mineru.content_list_contract import (
    resolved_image_path,
    resolved_table_html,
)
from disclosure_anchor.adapters.parsers.mineru.table_html_structure import (
    HtmlTableMedia,
    ParsedHtmlTable,
    table_media_artifact_role,
)
from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    TABLE_COMPARISON_CONTRACT,
    TABLE_RECONCILIATION_ALGORITHM_VERSION,
    validate_table_reconciliation_diagnostics,
)
from disclosure_anchor.application.contracts.table_comparison import (
    ComparableTable,
    TableComparisonError,
    comparable_table,
    model_comparable_tables,
    prove_unique_bijection,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


MODEL_BBOX_MAX_DELTA = 3.0
_MINERU_INLINE_EQUATION_RE = re.compile(r"<eq>(.*?)</eq>", re.DOTALL)
_MINERU_EQ_LIKE_TAG_RE = re.compile(r"</?eq\b[^>]*(?:>|$)", re.IGNORECASE)
_SUPPORTED_MINERU_INLINE_DELIMITERS = ("$", "$")
_MODEL_IMAGE_DATA_URI_RE = re.compile(
    r"^data:image/[A-Za-z0-9.+-]+;base64,(?P<payload>[A-Za-z0-9+/]*={0,2})$"
)


@dataclass(frozen=True)
class TableReconciliationStats:
    model_hash: str
    content_tables: int
    model_tables: int
    matched_tables: int
    projection_root: str
    page_local_closed: bool = True
    comparison_contract: str = TABLE_COMPARISON_CONTRACT
    algorithm_version: str = TABLE_RECONCILIATION_ALGORITHM_VERSION

    def __post_init__(self) -> None:
        if self.algorithm_version != TABLE_RECONCILIATION_ALGORITHM_VERSION:
            raise ValueError("unexpected table reconciliation algorithm version")
        validate_table_reconciliation_diagnostics(self.as_dict())

    def as_dict(self) -> dict[str, str | int | bool]:
        return {
            "algorithm_version": self.algorithm_version,
            "comparison_contract": self.comparison_contract,
            "projection_root": self.projection_root,
            "model_hash": self.model_hash,
            "content_tables": self.content_tables,
            "model_tables": self.model_tables,
            "matched_tables": self.matched_tables,
            "page_local_closed": self.page_local_closed,
        }


@dataclass(frozen=True)
class TableReconciliationResult:
    content_list: list[dict[str, Any]]
    stats: TableReconciliationStats


def reconcile_content_list_tables(
    content_list: list[dict[str, Any]],
    *,
    model_path: Path | None,
    registered_evidence_image_paths: Mapping[str, Path],
    content_table_structures: Mapping[int, ParsedHtmlTable] | None = None,
) -> TableReconciliationResult:
    """Validate a complete one-to-one page-local table relation."""

    _validate_registered_paths(registered_evidence_image_paths)
    content_tables = _content_tables(
        content_list,
        registered_evidence_image_paths=registered_evidence_image_paths,
        content_table_structures=content_table_structures or {},
    )
    if model_path is None:
        raise ParserOutputContractError(
            "MinerU model artifact is required for page-local table closure"
        )
    try:
        raw = model_path.read_bytes()
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot read MinerU model artifact: {model_path}"
        ) from exc
    try:
        model_tables, model_hash = model_comparable_tables(raw)
        projection_root = prove_unique_bijection(content_tables, model_tables)
    except TableComparisonError as exc:
        raise ParserOutputContractError(str(exc)) from exc
    count = len(content_tables)
    return TableReconciliationResult(
        content_list=list(content_list),
        stats=TableReconciliationStats(
            model_hash=model_hash,
            content_tables=count,
            model_tables=len(model_tables),
            matched_tables=count,
            projection_root=projection_root,
        ),
    )


def _content_tables(
    content_list: list[dict[str, Any]],
    *,
    registered_evidence_image_paths: Mapping[str, Path],
    content_table_structures: Mapping[int, ParsedHtmlTable],
) -> list[ComparableTable]:
    tables: list[ComparableTable] = []
    for index, item in enumerate(content_list):
        if str(item.get("type") or "") != "table":
            continue
        page_idx = _page_index(item.get("page_idx"))
        bbox = _normalized_bbox(item.get("bbox"))
        html = resolved_table_html(item)
        image_path = resolved_image_path(item)
        if page_idx is None or bbox is None:
            raise ParserOutputContractError(
                f"MinerU content table {index} lacks a valid page-local bbox"
            )
        if not isinstance(html, str) or not html.strip():
            raise ParserOutputContractError(
                f"MinerU content table {index} has empty HTML"
            )
        if image_path is None or not _safe_relative_path(image_path):
            raise ParserOutputContractError(
                f"MinerU content table {index} lacks a valid image crop path"
            )
        structure = content_table_structures.get(index)
        if structure is None:
            raise ParserOutputContractError(
                "MinerU content table structure was not materialized from "
                f"the content artifact: {index}"
            )
        outer_role = f"evidence_image_{index:06d}"
        if outer_role not in registered_evidence_image_paths:
            raise ParserOutputContractError(
                f"MinerU content table {index} image crop is not registered"
            )
        # Media artifact integrity stays a loud, separate closure: every
        # embedded occurrence must resolve to registered non-empty bytes.
        # It never participates in reader-visible equality.
        for media in structure.embedded_media:
            _content_media_sha256(
                media,
                source_item_index=index,
                registered_evidence_image_paths=(
                    registered_evidence_image_paths
                ),
            )
        try:
            tables.append(
                comparable_table(
                    index=index,
                    page_idx=page_idx,
                    bbox=bbox,
                    html=html,
                    label=f"content table {index}",
                )
            )
        except TableComparisonError as exc:
            raise ParserOutputContractError(str(exc)) from exc
    return tables


def _content_media_sha256(
    media: HtmlTableMedia,
    *,
    source_item_index: int,
    registered_evidence_image_paths: Mapping[str, Path],
) -> str:
    if not _safe_relative_path(media.image_path):
        raise ParserOutputContractError(
            "MinerU content table "
            f"{source_item_index} has an unsafe embedded image path"
        )
    role = table_media_artifact_role(
        source_item_index,
        media.occurrence_index,
    )
    path = registered_evidence_image_paths.get(role)
    if path is None:
        raise ParserOutputContractError(
            "MinerU content table "
            f"{source_item_index} embedded image {media.occurrence_index} "
            "is not registered"
        )
    return _file_sha256(
        path,
        label=(
            f"content table {source_item_index} embedded image "
            f"{media.occurrence_index}"
        ),
    )


def _file_sha256(path: Path, *, label: str) -> str:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot read MinerU {label}: {path}"
        ) from exc
    if not payload:
        raise ParserOutputContractError(
            f"MinerU {label} has empty bytes"
        )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _safe_relative_path(value: str) -> bool:
    if not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and "." not in path.parts
        and str(path) == value
    )


def _validate_registered_paths(paths: Mapping[str, Path]) -> None:
    for role, path in paths.items():
        if (
            not isinstance(role, str)
            or re.fullmatch(r"[a-z][a-z0-9_]*", role) is None
            or not isinstance(path, Path)
            or not path.is_file()
        ):
            raise ValueError("registered evidence image paths are invalid")


def _normalized_bbox(
    value: Any,
) -> tuple[float, float, float, float] | None:
    from disclosure_anchor.application.contracts.table_comparison import (
        _normalized_bbox as shared_normalized_bbox,
    )

    return shared_normalized_bbox(value)


def _page_index(value: Any) -> int | None:
    return value if is_page_index(value) else None
