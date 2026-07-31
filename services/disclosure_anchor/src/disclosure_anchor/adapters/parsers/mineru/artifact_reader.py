"""Read MinerU parser artifacts from a completed output directory."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Literal

from disclosure_anchor.adapters.parsers.mineru.content_list_contract import (
    resolved_image_path,
    resolved_table_html,
)
from disclosure_anchor.adapters.parsers.mineru.table_html_structure import (
    ParsedHtmlTable,
    TableHtmlStructureError,
    parse_table_html_structure,
    table_media_artifact_role,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


@dataclass(frozen=True)
class MinerUArtifacts:
    root: Path
    paths: Mapping[str, Path | None]


@dataclass(frozen=True, slots=True)
class MinerUContentArtifact:
    """One validated interpretation of the exact content-list bytes."""

    path: Path
    raw: bytes
    items: list[dict[str, Any]]
    sha256: str
    evidence_image_paths: Mapping[str, Path]
    table_structures: Mapping[int, ParsedHtmlTable]


@dataclass(frozen=True, slots=True)
class MinerUContentListV2Artifact:
    """Exact bytes and validated page blocks for the typed representation."""

    path: Path
    raw: bytes
    pages: list[list[dict[str, Any]]]
    sha256: str


@dataclass(frozen=True, slots=True)
class MiddleTableRoleHint:
    page_idx: int
    parent_bbox: tuple[float, float, float, float]
    field: Literal["table_caption", "table_footnote"]
    field_index: int
    role_bbox: tuple[float, float, float, float]
    provider_deleted: bool


@dataclass(frozen=True, slots=True)
class MinerUMiddleArtifact:
    sha256: str
    version: str
    backend: str
    page_count: int
    table_roles: tuple[MiddleTableRoleHint, ...]


class MinerUArtifactReader:
    """Locate and read the stable MinerU artifacts Phase 04 depends on."""

    def locate(self, output_dir: Path) -> MinerUArtifacts:
        if not output_dir.is_dir():
            raise ParserOutputContractError(
                f"MinerU output directory is missing: {output_dir}"
            )

        content_lists = sorted(
            path
            for path in output_dir.rglob("*_content_list.json")
            if not path.name.endswith("_content_list_v2.json")
        )
        if not content_lists:
            raise ParserOutputContractError(
                f"MinerU content_list artifact not found under {output_dir}"
            )
        if len(content_lists) > 1:
            raise ParserOutputContractError(
                f"multiple MinerU content_list artifacts found under {output_dir}"
            )

        content_list_path = content_lists[0]
        stem = content_list_path.name.removesuffix("_content_list.json")
        candidates = {
            "content_list": content_list_path,
            "middle": content_list_path.with_name(f"{stem}_middle.json"),
            "content_list_v2": content_list_path.with_name(
                f"{stem}_content_list_v2.json"
            ),
            "model": content_list_path.with_name(f"{stem}_model.json"),
            "markdown": content_list_path.with_name(f"{stem}.md"),
        }
        return MinerUArtifacts(
            root=content_list_path.parent,
            paths={
                role: path if role == "content_list" or path.is_file() else None
                for role, path in candidates.items()
            },
        )

    def read_content_list(self, content_list_path: Path) -> list[dict[str, Any]]:
        return self.read_content_artifact(content_list_path).items

    def read_content_artifact(
        self,
        content_list_path: Path,
    ) -> MinerUContentArtifact:
        """Read bytes once and close their table/image filesystem evidence."""

        try:
            root = content_list_path.parent.resolve(strict=True)
            resolved_content_path = content_list_path.resolve(strict=True)
            resolved_content_path.relative_to(root)
            if not resolved_content_path.is_file():
                raise ParserOutputContractError(
                    f"MinerU content_list is not a file: {content_list_path}"
                )
            payload = content_list_path.read_bytes()
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ParserOutputContractError(
                f"cannot read MinerU content_list: {content_list_path}"
            ) from exc
        data = self.read_content_list_bytes(payload)
        evidence_image_paths: dict[str, Path] = {}
        table_structures: dict[int, ParsedHtmlTable] = {}
        for index, item in enumerate(data):
            image_path = resolved_image_path(item)
            if image_path is not None:
                _register_content_image(
                    evidence_image_paths,
                    role=f"evidence_image_{index:06d}",
                    raw_path=image_path,
                    root=root,
                    label=f"image artifact {index}",
                )
            if str(item.get("type") or "") != "table":
                continue
            html = resolved_table_html(item)
            if not isinstance(html, str) or not html.strip():
                continue
            try:
                structure = parse_table_html_structure(html)
            except TableHtmlStructureError as exc:
                raise ParserOutputContractError(
                    f"MinerU table {index} has invalid HTML: {exc}"
                ) from exc
            table_structures[index] = structure
            for media in structure.embedded_media:
                _register_content_image(
                    evidence_image_paths,
                    role=table_media_artifact_role(
                        index,
                        media.occurrence_index,
                    ),
                    raw_path=media.image_path,
                    root=root,
                    label=(
                        f"table {index} embedded image "
                        f"{media.occurrence_index}"
                    ),
                )
        return MinerUContentArtifact(
            path=content_list_path,
            raw=payload,
            items=data,
            sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
            evidence_image_paths=evidence_image_paths,
            table_structures=table_structures,
        )

    def read_content_list_v2(
        self,
        path: Path,
    ) -> MinerUContentListV2Artifact:
        """Read MinerU's page-grouped structural representation."""

        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ParserOutputContractError(
                f"cannot read MinerU content_list_v2: {path}"
            ) from exc
        data = self.read_content_list_v2_bytes(payload)
        return MinerUContentListV2Artifact(
            path=path,
            raw=payload,
            pages=data,
            sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
        )

    def read_content_list_bytes(
        self,
        payload: bytes,
    ) -> list[dict[str, Any]]:
        """Decode exact legacy content-list bytes without filesystem guesses."""

        try:
            data = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ParserOutputContractError(
                "invalid MinerU content_list JSON"
            ) from exc
        if not isinstance(data, list):
            raise ParserOutputContractError(
                "MinerU content_list must be a list"
            )
        if not all(isinstance(item, dict) for item in data):
            raise ParserOutputContractError(
                "MinerU content_list items must be objects"
            )
        return data

    def read_content_list_v2_bytes(
        self,
        payload: bytes,
    ) -> list[list[dict[str, Any]]]:
        """Decode exact typed content-list bytes without a path dependency."""

        try:
            data = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ParserOutputContractError(
                "invalid MinerU content_list_v2 JSON"
            ) from exc
        if not isinstance(data, list):
            raise ParserOutputContractError(
                "MinerU content_list_v2 must be page-grouped objects"
            )
        pages: list[list[dict[str, Any]]] = []
        for page in data:
            if not isinstance(page, list) or not all(
                isinstance(block, dict) for block in page
            ):
                raise ParserOutputContractError(
                    "MinerU content_list_v2 must be page-grouped objects"
                )
            pages.append(page)
        return pages

    def read_middle(
        self,
        path: Path,
        *,
        expected_version: str,
        expected_backend: str,
        expected_page_count: int,
    ) -> MinerUMiddleArtifact:
        """Read only source-format table roles and locators from middle.json."""

        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ParserOutputContractError(
                f"cannot read MinerU middle artifact: {path}"
            ) from exc
        return self.read_middle_bytes(
            payload,
            expected_version=expected_version,
            expected_backend=expected_backend,
            expected_page_count=expected_page_count,
        )

    def read_middle_bytes(
        self,
        payload: bytes,
        *,
        expected_version: str,
        expected_backend: str,
        expected_page_count: int,
    ) -> MinerUMiddleArtifact:
        """Validate exact middle.json bytes without trusting its text fields."""

        try:
            data = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ParserOutputContractError(
                "invalid MinerU middle JSON"
            ) from exc
        if not isinstance(data, dict) or set(data) != {
            "_backend",
            "_version_name",
            "pdf_info",
        }:
            raise ParserOutputContractError(
                "MinerU middle artifact has an unsupported root shape"
            )
        if (
            data.get("_version_name") != expected_version
            or data.get("_backend") != expected_backend
        ):
            raise ParserOutputContractError(
                "MinerU middle artifact identity differs from parser runtime"
            )
        pages = data.get("pdf_info")
        if (
            not isinstance(pages, list)
            or isinstance(expected_page_count, bool)
            or expected_page_count < 1
            or len(pages) != expected_page_count
        ):
            raise ParserOutputContractError(
                "MinerU middle artifact page count differs from the source PDF"
            )
        roles: list[MiddleTableRoleHint] = []
        for page_idx, raw_page in enumerate(pages):
            roles.extend(_middle_page_roles(raw_page, page_idx=page_idx))
        return MinerUMiddleArtifact(
            sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
            version=expected_version,
            backend=expected_backend,
            page_count=expected_page_count,
            table_roles=tuple(roles),
        )


def _register_content_image(
    paths: dict[str, Path],
    *,
    role: str,
    raw_path: str,
    root: Path,
    label: str,
) -> None:
    relative = _safe_content_image_relpath(raw_path, label=label)
    try:
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
    except FileNotFoundError as exc:
        raise ParserOutputContractError(
            f"MinerU {label} does not exist"
        ) from exc
    except ValueError as exc:
        raise ParserOutputContractError(
            f"MinerU {label} escapes artifact root"
        ) from exc
    if not path.is_file():
        raise ParserOutputContractError(f"MinerU {label} is not a file")
    try:
        if path.stat().st_size < 1:
            raise ParserOutputContractError(f"MinerU {label} is empty")
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot inspect MinerU {label}"
        ) from exc
    if role in paths:
        raise ParserOutputContractError(
            f"MinerU evidence image role is duplicated: {role}"
        )
    paths[role] = path


def _safe_content_image_relpath(value: str, *, label: str) -> Path:
    relative = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or relative.is_absolute()
        or ".." in relative.parts
        or "." in relative.parts
        or str(relative) != value
        or value.startswith("file:")
        or re.match(r"^[A-Za-z]:/", value)
    ):
        raise ParserOutputContractError(f"MinerU {label} path is unsafe")
    return Path(*relative.parts)


def _middle_page_roles(
    value: object,
    *,
    page_idx: int,
) -> list[MiddleTableRoleHint]:
    if not isinstance(value, Mapping) or value.get("page_idx") != page_idx:
        raise ParserOutputContractError(
            "MinerU middle pages must have contiguous page indices"
        )
    raw_size = value.get("page_size")
    if (
        not isinstance(raw_size, list)
        or len(raw_size) != 2
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or float(item) <= 0
            for item in raw_size
        )
    ):
        raise ParserOutputContractError("MinerU middle page_size is invalid")
    width, height = (float(item) for item in raw_size)
    preproc = value.get("preproc_blocks")
    para = value.get("para_blocks")
    if not isinstance(preproc, list) or not isinstance(para, list):
        raise ParserOutputContractError(
            "MinerU middle page block collections must be arrays"
        )
    if any(
        isinstance(block, Mapping)
        and block.get("type") in {"table_caption", "table_footnote"}
        for block in (*preproc, *para)
    ):
        raise ParserOutputContractError(
            "MinerU middle table role is detached from its table parent"
        )
    pre_tables = _middle_tables(preproc, width=width, height=height)
    para_tables = _middle_tables(para, width=width, height=height)
    if set(pre_tables) != set(para_tables):
        raise ParserOutputContractError(
            "MinerU middle preproc/para table parents do not close one-to-one"
        )

    output: list[MiddleTableRoleHint] = []
    for raw_parent_bbox, pre_table in pre_tables.items():
        para_table = para_tables[raw_parent_bbox]
        pre_roles = _direct_table_roles(pre_table, width=width, height=height)
        para_roles = _direct_table_roles(para_table, width=width, height=height)
        if set(pre_roles) != set(para_roles):
            raise ParserOutputContractError(
                "MinerU middle preproc/para table roles do not close one-to-one"
            )
        field_counts = {"table_caption": 0, "table_footnote": 0}
        for key, pre_role in pre_roles.items():
            field, raw_role_bbox = key
            para_role = para_roles[key]
            deleted = para_role.get("lines_deleted", False)
            if not isinstance(deleted, bool):
                raise ParserOutputContractError(
                    "MinerU middle lines_deleted must be boolean"
                )
            field_index = field_counts[field]
            field_counts[field] += 1
            output.append(
                MiddleTableRoleHint(
                    page_idx=page_idx,
                    parent_bbox=_normalized_middle_bbox(
                        raw_parent_bbox,
                        width=width,
                        height=height,
                    ),
                    field=field,
                    field_index=field_index,
                    role_bbox=_normalized_middle_bbox(
                        raw_role_bbox,
                        width=width,
                        height=height,
                    ),
                    provider_deleted=deleted,
                )
            )
            if _contains_nested_table_role(pre_role):
                raise ParserOutputContractError(
                    "MinerU middle table role nesting is unsupported"
                )
    return output


def _middle_tables(
    blocks: list[Any],
    *,
    width: float,
    height: float,
) -> dict[tuple[float, float, float, float], Mapping[str, Any]]:
    tables: dict[tuple[float, float, float, float], Mapping[str, Any]] = {}
    for block in blocks:
        if not isinstance(block, Mapping):
            raise ParserOutputContractError(
                "MinerU middle page block must be an object"
            )
        if block.get("type") != "table":
            continue
        bbox = _raw_middle_bbox(block.get("bbox"), width=width, height=height)
        if bbox in tables:
            raise ParserOutputContractError(
                "MinerU middle table parent locator is ambiguous"
            )
        tables[bbox] = block
    return tables


def _direct_table_roles(
    table: Mapping[str, Any],
    *,
    width: float,
    height: float,
) -> dict[
    tuple[
        Literal["table_caption", "table_footnote"],
        tuple[float, float, float, float],
    ],
    Mapping[str, Any],
]:
    children = table.get("blocks")
    if not isinstance(children, list):
        raise ParserOutputContractError(
            "MinerU middle table blocks must be an array"
        )
    roles: dict[
        tuple[
            Literal["table_caption", "table_footnote"],
            tuple[float, float, float, float],
        ],
        Mapping[str, Any],
    ] = {}
    for child in children:
        if not isinstance(child, Mapping):
            raise ParserOutputContractError(
                "MinerU middle table child must be an object"
            )
        field = child.get("type")
        if field not in {"table_caption", "table_footnote"}:
            if _contains_nested_table_role(child):
                raise ParserOutputContractError(
                    "MinerU middle table role must be a direct table child"
                )
            continue
        bbox = _raw_middle_bbox(child.get("bbox"), width=width, height=height)
        key = (field, bbox)
        if key in roles:
            raise ParserOutputContractError(
                "MinerU middle table role locator is ambiguous"
            )
        roles[key] = child
    return roles


def _contains_nested_table_role(value: Mapping[str, Any]) -> bool:
    children = value.get("blocks")
    if not isinstance(children, list):
        return False
    return any(
        isinstance(child, Mapping)
        and (
            child.get("type") in {"table_caption", "table_footnote"}
            or _contains_nested_table_role(child)
        )
        for child in children
    )


def _raw_middle_bbox(
    value: object,
    *,
    width: float,
    height: float,
) -> tuple[float, float, float, float]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            for item in value
        )
    ):
        raise ParserOutputContractError("MinerU middle bbox is invalid")
    left, top, right, bottom = (float(item) for item in value)
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ParserOutputContractError(
            "MinerU middle bbox lies outside its physical page"
        )
    return left, top, right, bottom


def _normalized_middle_bbox(
    value: tuple[float, float, float, float],
    *,
    width: float,
    height: float,
) -> tuple[float, float, float, float]:
    left, top, right, bottom = value
    return (
        1000.0 * left / width,
        1000.0 * top / height,
        1000.0 * right / width,
        1000.0 * bottom / height,
    )
