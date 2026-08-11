"""Read official MinerU Medium artifacts without legacy repair or reconciliation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import codecs
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from disclosure_anchor.application.contracts.provider_document import (
    PhysicalTableLogicalStatus,
    ProviderArtifact,
    ProviderBBox,
    ProviderBlock,
    ProviderDocument,
    ProviderPage,
    ProviderPayload,
    ProviderPhysicalTableSegment,
    provider_artifact_bundle_sha256,
    provider_payload_field_contract,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


_EXPECTED_VERSION = "3.4.4"
_EXPECTED_BACKEND = "hybrid"
_EXPECTED_EFFORT = "medium"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PARSED_JSON_ROLES = frozenset(
    {"content_list", "content_list_v2", "middle_json", "model_json"}
)

_REQUIRED_SUFFIXES = {
    "content_list_v2": "_content_list_v2.json",
    "middle_json": "_middle.json",
    "model_json": "_model.json",
}
_OPTIONAL_SUFFIXES = {
    "markdown": ".md",
    "layout_pdf": "_layout.pdf",
    "origin_pdf": "_origin.pdf",
}
_KNOWN_PAYLOAD_FIELDS = frozenset(
    {
        "chart_caption",
        "chart_footnote",
        "code_body",
        "code_caption",
        "code_footnote",
        "content",
        "image_caption",
        "image_footnote",
        "list_items",
        "table_body",
        "table_caption",
        "table_footnote",
        "table_html",
        "text",
    }
)
_IMAGE_PATH_FIELDS = ("img_path", "image_path", "image")
_COMPATIBLE_TYPED_ANNOTATIONS = frozenset(
    {
        ("header", "page_header"),
        ("text", "paragraph"),
        ("text", "title"),
    }
)


class MinerUMediumArtifactReader:
    """Project the unique content-list directory subtree into diagnostic records."""

    def read(self, output_dir: Path, *, source_pdf_sha256: str) -> ProviderDocument:
        if not _SHA256_RE.fullmatch(source_pdf_sha256):
            raise ParserOutputContractError("source PDF sha256 must be canonical")
        root = _resolved_plain_directory(output_dir)
        content_path = _locate_content_list(root)
        artifact_root = _resolved_plain_directory(content_path.parent)
        stem = content_path.name.removesuffix("_content_list.json")
        expected_stem = source_pdf_sha256.replace("sha256:", "sha256_", 1)
        if stem != expected_stem:
            raise ParserOutputContractError(
                "MinerU content-list stem does not match the source PDF sha256"
            )

        role_paths: dict[str, Path] = {"content_list": content_path}
        for role, suffix in _REQUIRED_SUFFIXES.items():
            role_paths[role] = _resolved_plain_file(
                content_path.with_name(f"{stem}{suffix}"),
                root=artifact_root,
            )
        for role, suffix in _OPTIONAL_SUFFIXES.items():
            path = content_path.with_name(f"{stem}{suffix}")
            if not path.exists():
                continue
            role_paths[role] = _resolved_plain_file(path, root=artifact_root)

        tree_files = _plain_tree_files(artifact_root)
        relative_roles = _artifact_roles(
            root=artifact_root,
            files=tree_files,
            explicit_role_paths=role_paths,
        )
        artifacts_tuple = tuple(
            _artifact_record(
                role=relative_roles[path.relative_to(artifact_root).as_posix()],
                path=path,
                root=artifact_root,
            )
            for path in tree_files
        )
        artifacts_by_relative = {
            artifact.relative_path: artifact for artifact in artifacts_tuple
        }
        artifact_bytes = {
            role: _read_artifact_bytes(path=path, root=artifact_root, role=role)
            for role, path in role_paths.items()
            if role in _PARSED_JSON_ROLES
        }

        content_items = _object_list(
            artifact_bytes["content_list"],
            label="content_list",
        )
        typed_pages = _page_object_lists(
            artifact_bytes["content_list_v2"],
            label="content_list_v2",
        )
        page_sizes, parser_identity, ocr_enabled, middle_pages = _middle_document(
            artifact_bytes["middle_json"],
        )
        _json_value(artifact_bytes["model_json"], label="model_json")

        block_specs: list[tuple[int, dict[str, Any], int, int]] = []
        page_orders = [0 for _ in page_sizes]
        content_indices_by_page: list[list[int]] = [[] for _ in page_sizes]
        for source_index, item in enumerate(content_items):
            page_index = _page_index(item, page_count=len(page_sizes))
            order_in_page = page_orders[page_index]
            page_orders[page_index] += 1
            content_indices_by_page[page_index].append(source_index)
            block_specs.append((source_index, item, page_index, order_in_page))

        typed_annotations = (
            _bind_typed_annotations(
                typed_pages=typed_pages,
                content_indices_by_page=content_indices_by_page,
                content_items=content_items,
            )
            if len(typed_pages) == len(page_sizes)
            else {}
        )

        blocks_by_page: list[list[ProviderBlock]] = [[] for _ in page_sizes]
        for source_index, item, page_index, order_in_page in block_specs:
            provider_type = item.get("type")
            if not isinstance(provider_type, str) or not provider_type:
                raise ParserOutputContractError(
                    f"MinerU content-list item {source_index} has no type"
                )
            raw_item_json = _canonical_item_json(item, source_index=source_index)
            payloads = _provider_payloads(
                item,
                provider_type=provider_type,
                source_index=source_index,
            )
            image_roles = _referenced_image_roles(
                item=item,
                source_index=source_index,
                content_root=artifact_root,
                artifacts_by_relative=artifacts_by_relative,
            )
            blocks_by_page[page_index].append(
                ProviderBlock(
                    source_index=source_index,
                    page_index=page_index,
                    order_in_page=order_in_page,
                    provider_type=provider_type,
                    typed_annotation=typed_annotations.get(source_index),
                    provider_level=_provider_level(item, source_index=source_index),
                    bbox=_bbox_or_none(item.get("bbox"), strict=False),
                    payloads=payloads,
                    referenced_artifact_roles=image_roles,
                    raw_item_json=raw_item_json,
                    raw_item_sha256=_sha256(raw_item_json.encode("utf-8")),
                )
            )

        physical_table_segments = _physical_table_segments(
            middle_pages=middle_pages,
            page_sizes=page_sizes,
            artifacts_by_relative=artifacts_by_relative,
        )
        pages = tuple(
            ProviderPage(
                page_index=page_index,
                page_size=page_size,
                blocks=tuple(blocks_by_page[page_index]),
            )
            for page_index, page_size in enumerate(page_sizes)
        )
        return ProviderDocument(
            source_pdf_sha256=source_pdf_sha256,
            parser_version=parser_identity[0],
            backend=parser_identity[1],
            effort=parser_identity[2],
            ocr_enabled=ocr_enabled,
            pages=pages,
            physical_table_segments=physical_table_segments,
            artifacts=artifacts_tuple,
            bundle_sha256=provider_artifact_bundle_sha256(artifacts_tuple),
        )


def _resolved_plain_directory(path: Path) -> Path:
    try:
        if path.is_symlink():
            raise ParserOutputContractError(
                f"MinerU output root cannot be a symlink: {path}"
            )
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot resolve MinerU output root: {path}"
        ) from exc
    if not resolved.is_dir():
        raise ParserOutputContractError(f"MinerU output root is not a directory: {path}")
    return resolved


def _locate_content_list(root: Path) -> Path:
    candidates = sorted(
        path
        for path in root.rglob("*_content_list.json")
        if not path.name.endswith("_content_list_v2.json")
    )
    if len(candidates) != 1:
        raise ParserOutputContractError(
            "MinerU output must contain exactly one content_list artifact"
        )
    return _resolved_plain_file(candidates[0], root=root)


def _resolved_plain_file(path: Path, *, root: Path) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ParserOutputContractError("MinerU artifact escapes its output root") from exc
    cursor = root
    for component in relative.parts:
        cursor = cursor / component
        try:
            mode = cursor.lstat().st_mode
        except OSError as exc:
            raise ParserOutputContractError(
                f"cannot inspect MinerU artifact path: {relative.as_posix()}"
            ) from exc
        if stat.S_ISLNK(mode):
            raise ParserOutputContractError(
                f"MinerU artifact path contains a symlink: {relative.as_posix()}"
            )
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ParserOutputContractError(
            f"cannot resolve MinerU artifact: {relative.as_posix()}"
        ) from exc
    if not resolved.is_file():
        raise ParserOutputContractError(
            f"MinerU artifact is not a regular file: {relative.as_posix()}"
        )
    return resolved


def _plain_tree_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted(directory_names):
            candidate = current_path / name
            try:
                mode = candidate.lstat().st_mode
            except OSError as exc:
                raise ParserOutputContractError(
                    f"cannot inspect MinerU artifact directory: {candidate}"
                ) from exc
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ParserOutputContractError(
                    f"MinerU artifact tree contains an unsafe directory: {candidate}"
                )
        for name in sorted(file_names):
            candidate = current_path / name
            try:
                mode = candidate.lstat().st_mode
            except OSError as exc:
                raise ParserOutputContractError(
                    f"cannot inspect MinerU artifact file: {candidate}"
                ) from exc
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ParserOutputContractError(
                    f"MinerU artifact tree contains a non-regular file: {candidate}"
                )
            files.append(_resolved_plain_file(candidate, root=root))
    return tuple(sorted(files, key=lambda path: path.relative_to(root).as_posix()))


def _artifact_roles(
    *,
    root: Path,
    files: tuple[Path, ...],
    explicit_role_paths: Mapping[str, Path],
) -> dict[str, str]:
    explicit_by_relative = {
        path.relative_to(root).as_posix(): role
        for role, path in explicit_role_paths.items()
    }
    relative_paths = {path.relative_to(root).as_posix() for path in files}
    if not set(explicit_by_relative).issubset(relative_paths):
        raise ParserOutputContractError("MinerU artifact bundle is incomplete")
    roles: dict[str, str] = {}
    next_sidecar = 0
    for relative in sorted(relative_paths):
        explicit_role = explicit_by_relative.get(relative)
        if explicit_role is not None:
            roles[relative] = explicit_role
            continue
        roles[relative] = f"sidecar_{next_sidecar:06d}"
        next_sidecar += 1
    return roles


def _artifact_record(
    *,
    role: str,
    path: Path,
    root: Path,
) -> ProviderArtifact:
    resolved = _resolved_plain_file(path, root=root)
    try:
        digest = hashlib.sha256()
        size_bytes = 0
        leading_bytes = b""
        markdown_decoder = (
            codecs.getincrementaldecoder("utf-8")(errors="strict")
            if role == "markdown"
            else None
        )
        with resolved.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                if not leading_bytes:
                    leading_bytes = chunk[:16]
                if markdown_decoder is not None:
                    markdown_decoder.decode(chunk, final=False)
                digest.update(chunk)
                size_bytes += len(chunk)
        if markdown_decoder is not None:
            markdown_decoder.decode(b"", final=True)
    except (OSError, UnicodeDecodeError) as exc:
        raise ParserOutputContractError(f"cannot read MinerU artifact role={role}") from exc
    return ProviderArtifact(
        role=role,
        relative_path=resolved.relative_to(root).as_posix(),
        sha256="sha256:" + digest.hexdigest(),
        size_bytes=size_bytes,
        media_type=_artifact_media_type(
            role=role,
            leading_bytes=leading_bytes,
        ),
    )


def _artifact_media_type(*, role: str, leading_bytes: bytes) -> str:
    if role in _PARSED_JSON_ROLES:
        return "application/json"
    if role == "markdown":
        return "text/markdown"
    if leading_bytes.startswith(b"%PDF-"):
        return "application/pdf"
    if leading_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if leading_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if leading_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if (
        len(leading_bytes) >= 12
        and leading_bytes[:4] == b"RIFF"
        and leading_bytes[8:12] == b"WEBP"
    ):
        return "image/webp"
    return "application/octet-stream"


def _read_artifact_bytes(
    *,
    path: Path,
    root: Path,
    role: str,
) -> bytes:
    resolved = _resolved_plain_file(path, root=root)
    try:
        return resolved.read_bytes()
    except OSError as exc:
        raise ParserOutputContractError(f"cannot read MinerU artifact role={role}") from exc


def _json_value(payload: bytes, *, label: str) -> object:
    try:
        return json.loads(payload, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ParserOutputContractError(f"invalid MinerU {label} JSON") from exc


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _object_list(payload: bytes, *, label: str) -> list[dict[str, Any]]:
    value = _json_value(payload, label=label)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ParserOutputContractError(f"MinerU {label} must be an array of objects")
    return value


def _page_object_lists(
    payload: bytes,
    *,
    label: str,
) -> list[list[dict[str, Any]]]:
    value = _json_value(payload, label=label)
    if not isinstance(value, list):
        raise ParserOutputContractError(f"MinerU {label} must be page grouped")
    pages: list[list[dict[str, Any]]] = []
    for page in value:
        if not isinstance(page, list) or not all(isinstance(block, dict) for block in page):
            raise ParserOutputContractError(
                f"MinerU {label} pages must contain objects"
            )
        pages.append(page)
    return pages


def _object_value(payload: bytes, *, label: str) -> dict[str, Any]:
    value = _json_value(payload, label=label)
    if not isinstance(value, dict):
        raise ParserOutputContractError(f"MinerU {label} must be an object")
    return value


def _middle_document(
    payload: bytes,
) -> tuple[
    tuple[tuple[float, float], ...],
    tuple[str, str, str],
    bool,
    list[dict[str, Any]],
]:
    middle = _object_value(payload, label="middle_json")
    version = middle.get("_version_name")
    backend = middle.get("_backend")
    effort = middle.get("_effort")
    if not all(isinstance(value, str) for value in (version, backend, effort)):
        raise ParserOutputContractError("MinerU parser identity must contain text values")
    assert isinstance(version, str)
    assert isinstance(backend, str)
    assert isinstance(effort, str)
    if (version, backend, effort) != (
        _EXPECTED_VERSION,
        _EXPECTED_BACKEND,
        _EXPECTED_EFFORT,
    ):
        raise ParserOutputContractError(
            "MinerU artifact identity is not exact 3.4.4 Hybrid-medium"
        )
    ocr_enabled = middle.get("_ocr_enable")
    if not isinstance(ocr_enabled, bool):
        raise ParserOutputContractError("MinerU _ocr_enable must be boolean")
    pdf_info = middle.get("pdf_info")
    if (
        not isinstance(pdf_info, list)
        or not pdf_info
        or not all(isinstance(page, dict) for page in pdf_info)
    ):
        raise ParserOutputContractError("MinerU middle_json must contain PDF pages")
    page_sizes: list[tuple[float, float]] = []
    for expected_page, raw_page in enumerate(pdf_info):
        if not isinstance(raw_page, dict) or raw_page.get("page_idx") != expected_page:
            raise ParserOutputContractError(
                "MinerU middle_json pages must be contiguous and zero-based"
            )
        raw_size = raw_page.get("page_size")
        if not isinstance(raw_size, list) or len(raw_size) != 2:
            raise ParserOutputContractError("MinerU middle_json page_size is invalid")
        width = _positive_number(raw_size[0], field="page width")
        height = _positive_number(raw_size[1], field="page height")
        page_sizes.append((width, height))
    return tuple(page_sizes), (version, backend, effort), ocr_enabled, pdf_info


def _positive_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ParserOutputContractError(f"MinerU {field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ParserOutputContractError(f"MinerU {field} must be positive")
    return result


def _page_index(item: Mapping[str, object], *, page_count: int) -> int:
    value = item.get("page_idx")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParserOutputContractError("MinerU content-list page_idx must be an integer")
    if value < 0 or value >= page_count:
        raise ParserOutputContractError("MinerU content-list page_idx is out of range")
    return value


def _provider_level(
    item: Mapping[str, object],
    *,
    source_index: int,
) -> int | None:
    value = item.get("text_level")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ParserOutputContractError(
            f"MinerU content-list item {source_index} has an invalid text_level"
        )
    return value


def _bbox_or_none(value: object, *, strict: bool) -> ProviderBBox | None:
    if value is None:
        return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        if strict:
            raise ParserOutputContractError("MinerU bbox must contain four numbers")
        return None
    values = list(value)
    if len(values) != 4 or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) for item in values
    ):
        if strict:
            raise ParserOutputContractError("MinerU bbox must contain four numbers")
        return None
    try:
        return ProviderBBox(*(float(item) for item in values))
    except ValueError:
        if strict:
            raise ParserOutputContractError("MinerU bbox is outside its valid range")
        return None


def _bind_typed_annotations(
    *,
    typed_pages: list[list[dict[str, Any]]],
    content_indices_by_page: list[list[int]],
    content_items: list[dict[str, Any]],
) -> dict[int, str]:
    annotations: dict[int, str] = {}
    for page_index, source_indices in enumerate(content_indices_by_page):
        typed_blocks = typed_pages[page_index]
        if len(source_indices) != len(typed_blocks):
            continue
        for source_index, typed_block in zip(source_indices, typed_blocks, strict=True):
            typed_type = typed_block.get("type")
            if not isinstance(typed_type, str) or not typed_type:
                continue
            primary_item = content_items[source_index]
            primary_type = primary_item.get("type")
            if not isinstance(primary_type, str) or not _types_are_compatible(
                primary_type=primary_type,
                typed_type=typed_type,
            ):
                continue
            primary_bbox = _bbox_or_none(primary_item.get("bbox"), strict=False)
            typed_bbox = _bbox_or_none(typed_block.get("bbox"), strict=False)
            if (
                primary_bbox is None
                or typed_bbox is None
                or primary_bbox.as_tuple() != typed_bbox.as_tuple()
            ):
                continue
            annotations[source_index] = typed_type
    return annotations


def _types_are_compatible(*, primary_type: str, typed_type: str) -> bool:
    return primary_type == typed_type or (
        primary_type,
        typed_type,
    ) in _COMPATIBLE_TYPED_ANNOTATIONS


def _provider_payloads(
    item: Mapping[str, object],
    *,
    provider_type: str,
    source_index: int,
) -> tuple[ProviderPayload, ...]:
    try:
        scalar_fields, sequence_fields = provider_payload_field_contract(
            provider_type
        )
    except ValueError as exc:
        raise ParserOutputContractError(
            f"MinerU item {source_index} has unsupported type {provider_type}"
        ) from exc
    allowed_fields = frozenset((*scalar_fields, *sequence_fields))
    misplaced_fields = sorted(
        field
        for field in _KNOWN_PAYLOAD_FIELDS
        if field in item and field not in allowed_fields
    )
    if misplaced_fields:
        raise ParserOutputContractError(
            f"MinerU item {source_index} fields are invalid for type "
            f"{provider_type}: {', '.join(misplaced_fields)}"
        )
    payloads: list[ProviderPayload] = []
    for field in scalar_fields:
        if field not in item or item[field] is None:
            continue
        value = item[field]
        if not isinstance(value, str):
            raise ParserOutputContractError(
                f"MinerU item {source_index} field {field} must be text"
            )
        payloads.append(ProviderPayload(field=field, item_index=None, text=value))
    for field in sequence_fields:
        if field not in item or item[field] is None:
            continue
        value = item[field]
        if isinstance(value, list) and all(isinstance(entry, str) for entry in value):
            values = value
        else:
            raise ParserOutputContractError(
                f"MinerU item {source_index} field {field} must be a text array"
            )
        payloads.extend(
            ProviderPayload(field=field, item_index=index, text=text)
            for index, text in enumerate(values)
        )
    return tuple(payloads)


def _referenced_image_roles(
    *,
    item: Mapping[str, object],
    source_index: int,
    content_root: Path,
    artifacts_by_relative: Mapping[str, ProviderArtifact],
) -> tuple[str, ...]:
    roles: list[str] = []
    seen_paths: set[str] = set()
    for field in _IMAGE_PATH_FIELDS:
        value = item.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ParserOutputContractError(
                f"MinerU item {source_index} field {field} must be a path"
            )
        pure = PurePosixPath(value)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            raise ParserOutputContractError(
                f"MinerU item {source_index} contains an unsafe image path"
            )
        resolved = _resolved_plain_file(content_root.joinpath(*pure.parts), root=content_root)
        relative = resolved.relative_to(content_root).as_posix()
        if relative in seen_paths:
            continue
        seen_paths.add(relative)
        artifact = artifacts_by_relative.get(relative)
        if artifact is None:
            raise ParserOutputContractError(
                f"MinerU item {source_index} references an unbound image artifact"
            )
        roles.append(artifact.role)
    return tuple(roles)


def _physical_table_segments(
    *,
    middle_pages: list[dict[str, Any]],
    page_sizes: tuple[tuple[float, float], ...],
    artifacts_by_relative: Mapping[str, ProviderArtifact],
) -> tuple[ProviderPhysicalTableSegment, ...]:
    segments: list[ProviderPhysicalTableSegment] = []
    for page_index, middle_page in enumerate(middle_pages):
        preproc_blocks = _object_array_field(
            middle_page,
            field="preproc_blocks",
            page_index=page_index,
        )
        para_blocks = _object_array_field(
            middle_page,
            field="para_blocks",
            page_index=page_index,
        )
        table_order = 0
        for block in preproc_blocks:
            if block.get("type") != "table":
                continue
            provider_index = _nonnegative_integer(
                block.get("index"),
                field=f"middle page {page_index} table index",
            )
            table_spans = _table_spans(block)
            if len(table_spans) != 1:
                raise ParserOutputContractError(
                    "MinerU physical table segment must contain exactly one table span"
                )
            span = table_spans[0]
            html = span.get("html")
            if not isinstance(html, str) or not html:
                raise ParserOutputContractError(
                    "MinerU physical table segment HTML must be non-empty"
                )
            crop_role = _middle_crop_role(
                span=span,
                artifacts_by_relative=artifacts_by_relative,
            )
            raw_segment_json = _canonical_value_json(
                block,
                label=f"middle page {page_index} table {provider_index}",
            )
            segments.append(
                ProviderPhysicalTableSegment(
                    page_index=page_index,
                    order_in_page=table_order,
                    provider_index=provider_index,
                    bbox=_middle_bbox_or_none(
                        block.get("bbox"),
                        page_size=page_sizes[page_index],
                    ),
                    page_local_html=html,
                    crop_artifact_role=crop_role,
                    logical_stream_status=_table_logical_stream_status(
                        preproc_block=block,
                        para_blocks=para_blocks,
                    ),
                    raw_segment_json=raw_segment_json,
                    raw_segment_sha256=_sha256(raw_segment_json.encode("utf-8")),
                )
            )
            table_order += 1
    return tuple(segments)


def _object_array_field(
    value: Mapping[str, object],
    *,
    field: str,
    page_index: int,
) -> list[dict[str, Any]]:
    raw = value.get(field)
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ParserOutputContractError(
            f"MinerU middle page {page_index} field {field} must contain objects"
        )
    return raw


def _nonnegative_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ParserOutputContractError(f"MinerU {field} must be non-negative")
    return value


def _table_spans(block: Mapping[str, object]) -> list[dict[str, Any]]:
    raw_blocks = block.get("blocks")
    if not isinstance(raw_blocks, list) or not all(
        isinstance(item, dict) for item in raw_blocks
    ):
        return []
    spans: list[dict[str, Any]] = []
    for raw_block in raw_blocks:
        raw_lines = raw_block.get("lines")
        if not isinstance(raw_lines, list) or not all(
            isinstance(item, dict) for item in raw_lines
        ):
            continue
        for raw_line in raw_lines:
            raw_spans = raw_line.get("spans")
            if not isinstance(raw_spans, list) or not all(
                isinstance(item, dict) for item in raw_spans
            ):
                continue
            spans.extend(span for span in raw_spans if span.get("type") == "table")
    return spans


def _middle_crop_role(
    *,
    span: Mapping[str, object],
    artifacts_by_relative: Mapping[str, ProviderArtifact],
) -> str | None:
    value = span.get("image_path")
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ParserOutputContractError("MinerU middle table image_path must be text")
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ParserOutputContractError("MinerU middle table image_path is unsafe")
    relative = pure.as_posix() if len(pure.parts) > 1 else f"images/{pure.as_posix()}"
    artifact = artifacts_by_relative.get(relative)
    if artifact is None:
        raise ParserOutputContractError(
            "MinerU middle table image_path is not hash-bound in the artifact bundle"
        )
    return artifact.role


def _middle_bbox_or_none(
    value: object,
    *,
    page_size: tuple[float, float],
) -> ProviderBBox | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    raw = list(value)
    if len(raw) != 4 or any(
        isinstance(item, bool) or not isinstance(item, (int, float)) for item in raw
    ):
        return None
    x0, y0, x1, y1 = (float(item) for item in raw)
    width, height = page_size
    if not (
        all(math.isfinite(item) for item in (x0, y0, x1, y1))
        and 0 <= x0 < x1 <= width
        and 0 <= y0 < y1 <= height
    ):
        return None
    return ProviderBBox(
        x0=x0 / width * 1000.0,
        y0=y0 / height * 1000.0,
        x1=x1 / width * 1000.0,
        y1=y1 / height * 1000.0,
    )


def _table_logical_stream_status(
    *,
    preproc_block: Mapping[str, object],
    para_blocks: list[dict[str, Any]],
) -> PhysicalTableLogicalStatus:
    identity = _middle_block_identity(preproc_block)
    matches = [
        block
        for block in para_blocks
        if block.get("type") == "table" and _middle_block_identity(block) == identity
    ]
    if len(matches) != 1:
        return "unbound"
    match = matches[0]
    if any(
        isinstance(span.get("html"), str) and bool(span.get("html"))
        for span in _table_spans(match)
    ):
        return "retained"
    raw_blocks = match.get("blocks")
    if isinstance(raw_blocks, list) and raw_blocks:
        table_bodies = [
            block
            for block in raw_blocks
            if isinstance(block, dict) and block.get("type") == "table_body"
        ]
        if table_bodies and all(
            block.get("lines_deleted") is True and block.get("lines") == []
            for block in table_bodies
        ):
            return "deleted"
    return "unbound"


def _middle_block_identity(block: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        _canonical_value_json(block.get("type"), label="middle block type"),
        _canonical_value_json(block.get("index"), label="middle block index"),
        _canonical_value_json(block.get("bbox"), label="middle block bbox"),
    )


def _canonical_value_json(value: object, *, label: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ParserOutputContractError(f"MinerU {label} is not canonical JSON") from exc


def _canonical_item_json(item: Mapping[str, object], *, source_index: int) -> str:
    return _canonical_value_json(item, label=f"item {source_index}")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = ["MinerUMediumArtifactReader"]
