#!/usr/bin/env python3
"""Render a DB-free, source-first review of one MinerU Medium bundle."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
from typing import Any, Sequence

from disclosure_anchor.adapters.parsers.mineru_medium import (
    MinerUMediumArtifactReader,
)
from disclosure_anchor.application.contracts.document_outline import (
    DocumentOutline,
    HeadingCandidate,
)
from disclosure_anchor.application.contracts.provider_document import (
    ProviderBlock,
    ProviderDocument,
)
from disclosure_anchor.application.services.document_outline import (
    build_document_outline,
)
from disclosure_anchor.application.services.provider_table_projection import (
    build_provider_table_projection,
)
from disclosure_anchor.application.services.retrieval_primary import (
    build_retrieval_primary_projection,
)


_SCHEMA = "mineru-medium-visual-review.v2"
_DPI = 144
_PAGES_RE = re.compile(r"^Pages:\s+([0-9]+)\s*$", re.MULTILINE)


def build_review_payload(
    document: ProviderDocument,
    outline: DocumentOutline,
    *,
    source_page_offset: int,
    selected_provider_pages: Sequence[int],
) -> dict[str, Any]:
    """Serialize exact DTOs plus the provider table and retrieval projections."""

    selected = tuple(selected_provider_pages)
    if source_page_offset < 0:
        raise ValueError("source page offset cannot be negative")
    if not selected or selected != tuple(sorted(set(selected))):
        raise ValueError("selected provider pages must be unique and ordered")
    if any(page < 0 or page >= len(document.pages) for page in selected):
        raise ValueError("selected provider page is out of range")
    if (
        outline.source_pdf_sha256 != document.source_pdf_sha256
        or outline.provider_bundle_sha256 != document.bundle_sha256
        or outline.block_count != len(document.blocks)
    ):
        raise ValueError("outline does not bind the exact provider document")

    units_by_source: dict[int, int] = {}
    for unit in outline.units:
        for source_index in unit.block_source_indices:
            if source_index in units_by_source:
                raise ValueError("provider block belongs to multiple coarse units")
            units_by_source[source_index] = unit.unit_index
    if set(units_by_source) != set(range(len(document.blocks))):
        raise ValueError("coarse units do not cover every provider block")

    blocks_by_source = {block.source_index: block for block in document.blocks}
    resolved_by_id = {heading.heading_id: heading for heading in outline.headings}
    for candidate in outline.candidates:
        block = blocks_by_source.get(candidate.source_index)
        if block is None or not _candidate_binds_block(candidate, block):
            raise ValueError("heading candidate does not bind its provider block")
        if (candidate.disposition == "accepted") != (
            candidate.heading_id in resolved_by_id
        ):
            raise ValueError("heading candidate disposition has no matching resolution")

    table_projection = build_provider_table_projection(document)
    retrieval_projection = build_retrieval_primary_projection(
        document,
        outline,
        table_projection,
    )
    retrieval_by_source = {
        block.source_index: block for block in retrieval_projection.blocks
    }
    table_relation_by_segment: dict[int, dict[str, object]] = {}
    for table_index, logical_table in enumerate(table_projection.logical_tables):
        owner_source = logical_table.owner.block_source_index
        assert owner_source is not None
        for role, part in (
            ("owner", logical_table.owner),
            *(("continuation", item) for item in logical_table.continuations),
        ):
            assert part.block_source_index is not None
            assert part.physical_segment_index is not None
            table_relation_by_segment[part.physical_segment_index] = {
                "relation": role,
                "flat_block_source_index": part.block_source_index,
                "logical_owner_source_index": owner_source,
                "logical_table_index": table_index,
                "unbound_reason": None,
            }
    for unbound in table_projection.unbound_parts:
        segment_index = unbound.part.physical_segment_index
        if segment_index is None:
            continue
        table_relation_by_segment[segment_index] = {
            "relation": "unbound",
            "flat_block_source_index": unbound.part.block_source_index,
            "logical_owner_source_index": None,
            "logical_table_index": None,
            "unbound_reason": unbound.reason,
        }

    provider_record = asdict(document)
    for artifact in provider_record["artifacts"]:
        artifact.pop("media_type")
    for page in provider_record["pages"]:
        for block in page["blocks"]:
            block.pop("raw_item_json")
    for segment in provider_record["physical_table_segments"]:
        html = segment.pop("page_local_html")
        segment.pop("raw_segment_json")
        encoded = html.encode("utf-8")
        segment["page_local_html_sha256"] = _sha256_bytes(encoded)
        segment["page_local_html_size_bytes"] = len(encoded)
        segment["page_local_html_preview"] = html[:300]

    physical_table_inventory: list[dict[str, object]] = []
    for segment_index, segment in enumerate(document.physical_table_segments):
        relation = table_relation_by_segment.get(segment_index)
        if relation is None:
            raise ValueError("table projection omitted a physical segment")
        relation_owner = relation.get("logical_owner_source_index")
        unit_index = (
            units_by_source[relation_owner]
            if isinstance(relation_owner, int)
            else None
        )
        physical_table_inventory.append(
            {
                "segment_index": segment_index,
                "stable_key": [
                    segment.page_index,
                    segment.order_in_page,
                    segment.provider_index,
                    segment.raw_segment_sha256,
                ],
                "source_page_index": source_page_offset + segment.page_index,
                "source_page_number": source_page_offset + segment.page_index + 1,
                **relation,
                "unit_index": unit_index,
            }
        )

    return {
        "schema": _SCHEMA,
        "source": {
            "pdf_sha256": document.source_pdf_sha256,
            "page_offset_zero_based": source_page_offset,
        },
        "provider": {
            "bundle_sha256": document.bundle_sha256,
            "parser_version": document.parser_version,
            "backend": document.backend,
            "effort": document.effort,
        },
        "selected_provider_pages": list(selected),
        "alias_evaluator": "not_implemented",
        "provider_document": provider_record,
        "outline": asdict(outline),
        "provider_table_projection": asdict(table_projection),
        "retrieval_primary": asdict(retrieval_projection),
        "review": {
            "block_assignments": [
                {
                    "source_index": block.source_index,
                    "unit_index": units_by_source[block.source_index],
                    "source_page_index": source_page_offset + block.page_index,
                    "source_page_number": source_page_offset + block.page_index + 1,
                    "alias_status": "not_evaluated",
                    "alias_group_id": None,
                    "retrieval_disposition": retrieval_by_source[
                        block.source_index
                    ].disposition,
                    "retrieval_reason": retrieval_by_source[block.source_index].reason,
                    "retrieval_target_ids": list(
                        retrieval_by_source[block.source_index].target_ids
                    ),
                }
                for block in document.blocks
            ],
            "heading_decisions": [
                {
                    "heading_id": candidate.heading_id,
                    "disposition": candidate.disposition,
                    "resolved_heading_id": candidate.heading_id
                    if candidate.heading_id in resolved_by_id
                    else None,
                }
                for candidate in outline.candidates
            ],
            "physical_table_segment_inventory": physical_table_inventory,
        },
    }


def _candidate_binds_block(candidate: HeadingCandidate, block: ProviderBlock) -> bool:
    return (
        candidate.source_index == block.source_index
        and candidate.page_index == block.page_index
        and candidate.bbox == block.bbox
        and candidate.raw_block_sha256 == block.raw_item_sha256
        and candidate.text == _heading_text(block)
    )


def _heading_text(block: ProviderBlock) -> str:
    for payload in block.payloads:
        if payload.field in {"text", "content"} and payload.text:
            return payload.text
    return ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-pdf", type=Path, required=True)
    parser.add_argument("--provider-bundle", type=Path, required=True)
    parser.add_argument("--source-page-offset", type=int, required=True)
    parser.add_argument("--provider-page", type=int, action="append")
    parser.add_argument("--run-evidence", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source_pdf = _plain_file(args.source_pdf, "source PDF")
    bundle = _plain_directory(args.provider_bundle, "provider bundle")
    _require_bundle_leaf(bundle)
    source_sha256 = _sha256_file(source_pdf)
    document = MinerUMediumArtifactReader().read(
        bundle,
        source_pdf_sha256=source_sha256,
    )
    outline = build_document_outline(document)
    selected = _selected_pages(args.provider_page, len(document.pages))
    payload = build_review_payload(
        document,
        outline,
        source_page_offset=args.source_page_offset,
        selected_provider_pages=selected,
    )

    pdfinfo = _program("pdfinfo")
    pdftoppm = _program("pdftoppm")
    source_page_count = _page_count(pdfinfo, source_pdf)
    if args.source_page_offset + len(document.pages) > source_page_count:
        raise ValueError("provider page window exceeds source PDF pages")
    layout_pdf, layout_sha256 = _layout_pdf(bundle, document)
    if _page_count(pdfinfo, layout_pdf) != len(document.pages):
        raise ValueError("layout PDF page count differs from provider pages")
    run_evidence = load_bound_run_evidence(
        _plain_file(args.run_evidence, "run evidence"),
        source_pdf_sha256=source_sha256,
        source_page_count=source_page_count,
        source_page_offset=args.source_page_offset,
        provider_page_count=len(document.pages),
        provider_bundle=bundle,
    )

    output = _create_output(args.out)
    pages = _render_pages(
        pdftoppm,
        source_pdf=source_pdf,
        layout_pdf=layout_pdf,
        output=output,
        provider_pages=selected,
        source_page_offset=args.source_page_offset,
    )
    if _sha256_file(source_pdf) != source_sha256:
        raise RuntimeError("source PDF changed during review")
    if _sha256_file(layout_pdf) != layout_sha256:
        raise RuntimeError("layout PDF changed during review")

    payload["source"].update(
        {"path": str(source_pdf), "page_count": source_page_count}
    )
    payload["provider"].update(
        {"bundle_path": str(bundle), "layout_pdf_sha256": layout_sha256}
    )
    payload["run_evidence"] = run_evidence
    payload["render"] = {"dpi": _DPI, "pages": pages}
    report_bytes = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    report_path = output / "report.json"
    report_path.write_bytes(report_bytes)
    report_sha256 = _sha256_bytes(report_bytes)
    markdown_path = output / "report.md"
    markdown_path.write_text(
        _markdown(document, outline, payload, report_sha256=report_sha256),
        encoding="utf-8",
    )
    print(f"report_json={report_path}")
    print(f"report_json_sha256={report_sha256}")
    print(f"report_markdown={markdown_path}")
    return 0


def _selected_pages(raw: list[int] | None, page_count: int) -> tuple[int, ...]:
    selected = tuple(range(page_count)) if raw is None else tuple(raw)
    if (
        not selected
        or selected != tuple(sorted(set(selected)))
        or any(page < 0 or page >= page_count for page in selected)
    ):
        raise ValueError("provider pages must be unique, ordered, and in range")
    return selected


def _plain_file(path: Path, label: str) -> Path:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"cannot inspect {label}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a regular non-symlink file")
    return path.resolve(strict=True)


def _plain_directory(path: Path, label: str) -> Path:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"cannot inspect {label}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ValueError(f"{label} must be a regular non-symlink directory")
    return path.resolve(strict=True)


def _require_bundle_leaf(bundle: Path) -> None:
    content_lists = [
        path
        for path in bundle.glob("*_content_list.json")
        if not path.name.endswith("_content_list_v2.json")
    ]
    if len(content_lists) != 1:
        raise ValueError("provider bundle must be the exact hybrid_auto leaf")


def _program(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"required review program is missing: {name}")
    return path


def _page_count(pdfinfo: str, path: Path) -> int:
    result = subprocess.run(
        [pdfinfo, str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    match = _PAGES_RE.search(result.stdout)
    if result.returncode != 0 or match is None or int(match.group(1)) < 1:
        raise RuntimeError(f"cannot read PDF page count: {path.name}")
    return int(match.group(1))


def _layout_pdf(bundle: Path, document: ProviderDocument) -> tuple[Path, str]:
    artifacts = [
        artifact for artifact in document.artifacts if artifact.role == "layout_pdf"
    ]
    if len(artifacts) != 1:
        raise ValueError("provider bundle must contain one layout PDF")
    artifact = artifacts[0]
    path = _plain_file(bundle / artifact.relative_path, "layout PDF")
    if _sha256_file(path) != artifact.sha256:
        raise ValueError("layout PDF differs from its provider descriptor")
    return path, artifact.sha256


def _create_output(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("review output path must be absolute")
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError("review output path must be create-only")
    parent = path.parent.resolve(strict=True)
    try:
        parent.relative_to(Path("/private/tmp").resolve(strict=True))
    except ValueError as exc:
        raise ValueError("review output must be under /private/tmp") from exc
    output = parent / path.name
    output.mkdir(mode=0o700)
    (output / "pages").mkdir(mode=0o700)
    return output


def _render_pages(
    pdftoppm: str,
    *,
    source_pdf: Path,
    layout_pdf: Path,
    output: Path,
    provider_pages: tuple[int, ...],
    source_page_offset: int,
) -> list[dict[str, object]]:
    records = []
    for provider_page in provider_pages:
        source_page = source_page_offset + provider_page
        source_relative = Path(
            f"pages/source-p{source_page + 1:06d}-provider-{provider_page:06d}.png"
        )
        layout_relative = Path(f"pages/layout-provider-{provider_page:06d}.png")
        _render_page(pdftoppm, source_pdf, source_page, output / source_relative)
        _render_page(pdftoppm, layout_pdf, provider_page, output / layout_relative)
        records.append(
            {
                "provider_page_index": provider_page,
                "source_page_index": source_page,
                "source_page_number": source_page + 1,
                "source_png": _file_record(output / source_relative, source_relative),
                "layout_png": _file_record(output / layout_relative, layout_relative),
            }
        )
    return records


def _render_page(pdftoppm: str, pdf: Path, page: int, output: Path) -> None:
    result = subprocess.run(
        [
            pdftoppm,
            "-f",
            str(page + 1),
            "-l",
            str(page + 1),
            "-r",
            str(_DPI),
            "-singlefile",
            "-png",
            str(pdf),
            str(output.with_suffix("")),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftoppm failed for {pdf.name}")
    _plain_file(output, "rendered PNG")


def _file_record(path: Path, relative: Path) -> dict[str, object]:
    return {
        "relative_path": relative.as_posix(),
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def load_bound_run_evidence(
    path: Path,
    *,
    source_pdf_sha256: str,
    source_page_count: int,
    source_page_offset: int,
    provider_page_count: int,
    provider_bundle: Path,
) -> dict[str, object]:
    """Bind the visual page domain to the exact provider execution evidence."""

    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("run evidence must be an object")
    for label in ("input_before", "input_after"):
        input_record = value.get(label)
        if not isinstance(input_record, dict):
            raise ValueError(f"run evidence {label} must be an object")
        observed_sha = input_record.get("sha256")
        if observed_sha not in {
            source_pdf_sha256,
            source_pdf_sha256.removeprefix("sha256:"),
        } or input_record.get("page_count") != source_page_count:
            raise ValueError(f"run evidence {label} does not bind the source PDF")
    command = value.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ValueError("run evidence command must be a string list")
    output_value = _single_flag_value(command, "-o")
    if output_value is None:
        raise ValueError("run evidence command is missing its output root")
    try:
        output_root = Path(output_value).resolve(strict=True)
    except OSError as exc:
        raise ValueError("run evidence output root is unavailable") from exc
    if output_root != provider_bundle.resolve(strict=True).parents[1]:
        raise ValueError("run evidence command does not bind the provider bundle")
    _validate_output_manifest(value, evidence_root=path.parent, bundle=provider_bundle)

    start_value = _single_flag_value(command, "-s")
    end_value = _single_flag_value(command, "-e")
    if (start_value is None) != (end_value is None):
        raise ValueError("run evidence page window is incomplete")
    if start_value is None:
        if source_page_offset != 0 or provider_page_count != source_page_count:
            raise ValueError("full-document evidence cannot bind a provider page window")
        page_window: dict[str, int | str] = {
            "mode": "full_document",
            "start_zero_based": 0,
            "end_zero_based_inclusive": source_page_count - 1,
        }
    else:
        assert start_value is not None and end_value is not None
        try:
            start = int(start_value)
            end = int(end_value)
        except ValueError as exc:
            raise ValueError("run evidence page window must use integer indices") from exc
        if (
            start < 0
            or end < start
            or end >= source_page_count
            or source_page_offset != start
            or provider_page_count != end - start + 1
        ):
            raise ValueError("run evidence page window does not bind the review offset")
        page_window = {
            "mode": "window",
            "start_zero_based": start,
            "end_zero_based_inclusive": end,
        }

    errors = value.get("invariant_errors")
    return {
        "sha256": _sha256_file(path),
        "status": value.get("status") if isinstance(value.get("status"), str) else None,
        "invariant_errors": errors
        if isinstance(errors, list) and all(isinstance(item, str) for item in errors)
        else [],
        "page_window": page_window,
    }


def _single_flag_value(command: list[str], flag: str) -> str | None:
    positions = [index for index, item in enumerate(command) if item == flag]
    if not positions:
        return None
    if len(positions) != 1 or positions[0] + 1 >= len(command):
        raise ValueError(f"run evidence command has an ambiguous {flag} flag")
    return command[positions[0] + 1]


def _validate_output_manifest(
    evidence: dict[str, Any],
    *,
    evidence_root: Path,
    bundle: Path,
) -> None:
    records = evidence.get("output_files")
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise ValueError("run evidence output manifest must be an object list")
    try:
        bundle_prefix = bundle.resolve(strict=True).relative_to(
            evidence_root.resolve(strict=True)
        )
    except ValueError as exc:
        raise ValueError("provider bundle is outside the run evidence root") from exc
    prefix = bundle_prefix.as_posix() + "/"
    expected: dict[str, tuple[str, int]] = {}
    for record in records:
        relative = record.get("path")
        sha256 = record.get("sha256")
        size_bytes = record.get("size_bytes")
        if not isinstance(relative, str) or not relative.startswith(prefix):
            continue
        if (
            relative in expected
            or not isinstance(sha256, str)
            or not isinstance(size_bytes, int)
        ):
            raise ValueError("run evidence output manifest has an invalid bundle record")
        expected[relative] = (sha256.removeprefix("sha256:"), size_bytes)

    actual: dict[str, tuple[str, int]] = {}
    for artifact in sorted(bundle.rglob("*")):
        if not artifact.is_file():
            continue
        artifact = _plain_file(artifact, "provider bundle artifact")
        relative = artifact.relative_to(evidence_root).as_posix()
        actual[relative] = (
            _sha256_file(artifact).removeprefix("sha256:"),
            artifact.stat().st_size,
        )
    if actual != expected:
        raise ValueError("run evidence output manifest does not bind the provider bundle")


def _markdown(
    document: ProviderDocument,
    outline: DocumentOutline,
    payload: dict[str, Any],
    *,
    report_sha256: str,
) -> str:
    unit_by_source = {
        source_index: unit.unit_index
        for unit in outline.units
        for source_index in unit.block_source_indices
    }
    candidate_by_source = {
        candidate.source_index: candidate for candidate in outline.candidates
    }
    resolved_by_id = {heading.heading_id: heading for heading in outline.headings}
    retrieval = payload["retrieval_primary"]
    table_projection = payload["provider_table_projection"]
    review = payload["review"]
    assert isinstance(retrieval, dict)
    assert isinstance(table_projection, dict)
    assert isinstance(review, dict)
    retrieval_by_source = {
        int(item["source_index"]): item
        for item in retrieval["blocks"]
        if isinstance(item, dict)
    }
    table_inventory = {
        int(item["segment_index"]): item
        for item in review["physical_table_segment_inventory"]
        if isinstance(item, dict)
    }
    lines = [
        "# MinerU Medium visual review",
        "",
        f"- Source SHA-256: `{document.source_pdf_sha256}`",
        f"- Bundle SHA-256: `{document.bundle_sha256}`",
        f"- Blocks / accepted headings / units / physical table segments: "
        f"`{len(document.blocks)} / {len(outline.headings)} / {len(outline.units)} / "
        f"{len(document.physical_table_segments)}`",
        f"- Retrieval targets / provider logical tables / unbound table parts: "
        f"`{len(retrieval['targets'])} / {len(table_projection['logical_tables'])} / "
        f"{len(table_projection['unbound_parts'])}`",
        "- Same-page alias evaluator: `not_implemented`; no content-list blocks are deduplicated.",
        "- Cross-page table relations replay only MinerU retained/deleted state and page boundaries; no HTML or cell repair is performed.",
        f"- `report.json` SHA-256: `{report_sha256}`",
        "",
    ]
    evidence = payload.get("run_evidence")
    if isinstance(evidence, dict):
        lines.extend(
            [
                f"- Run status: `{evidence.get('status')}`",
                f"- Run invariant errors: `{evidence.get('invariant_errors')}`",
                "",
            ]
        )
    render = payload["render"]
    assert isinstance(render, dict)
    rendered_pages = render["pages"]
    assert isinstance(rendered_pages, list)
    for rendered in rendered_pages:
        assert isinstance(rendered, dict)
        provider_page = int(rendered["provider_page_index"])
        source_number = int(rendered["source_page_number"])
        source_png = rendered["source_png"]
        layout_png = rendered["layout_png"]
        assert isinstance(source_png, dict) and isinstance(layout_png, dict)
        lines.extend(
            [
                f"## Provider page {provider_page} / source page {source_number}",
                "",
                "| Source | MinerU layout |",
                "|---|---|",
                f"| ![source]({source_png['relative_path']}) | ![layout]({layout_png['relative_path']}) |",
                "",
                "### Blocks",
                "",
            ]
        )
        for block in document.pages[provider_page].blocks:
            candidate = candidate_by_source.get(block.source_index)
            resolved = (
                None if candidate is None else resolved_by_id.get(candidate.heading_id)
            )
            lines.append(
                f"- `block {block.source_index}` type=`{block.provider_type}` "
                f"annotation=`{block.typed_annotation}` bbox=`{_bbox(block)}` "
                f"unit=`{unit_by_source[block.source_index]}` "
                f"heading=`{None if candidate is None else candidate.disposition}` "
                f"level=`{None if resolved is None else resolved.level}` "
                f"retrieval=`{retrieval_by_source[block.source_index]['disposition']}` "
                f"alias=`not_evaluated`"
            )
            if resolved is not None:
                lines.append(f"  - headpath: {' > '.join(resolved.headpath)}")
            preview = _payload_preview(block)
            if preview:
                lines.append(f"  - payload: {preview}")
        lines.extend(["", "### Physical table segments", ""])
        segments = [
            segment
            for segment in document.physical_table_segments
            if segment.page_index == provider_page
        ]
        if not segments:
            lines.append("- none")
        for segment in segments:
            segment_index = document.physical_table_segments.index(segment)
            relation = table_inventory[segment_index]
            lines.append(
                f"- order=`{segment.order_in_page}` provider_index=`{segment.provider_index}` "
                f"status=`{segment.logical_stream_status}` bbox=`{_bbox(segment)}` "
                f"relation=`{relation['relation']}` owner=`{relation['logical_owner_source_index']}`"
            )
            lines.append(
                f"  - HTML: `{_escape(_one_line(segment.page_local_html, 180))}`"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _bbox(value: ProviderBlock | Any) -> object:
    bbox = value.bbox
    return None if bbox is None else bbox.as_tuple()


def _payload_preview(block: ProviderBlock) -> str:
    text = " | ".join(payload.text for payload in block.payloads if payload.text)
    return _escape(_one_line(text, 240))


def _one_line(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _escape(value: str) -> str:
    return value.replace("`", "\\`")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
