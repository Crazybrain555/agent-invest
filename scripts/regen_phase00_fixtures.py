"""Regenerate Phase 00 NormalizedIR v2 fixtures from saved MinerU artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import MinerUArtifactReader
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUParserInfo,
    MinerUToNormalizedIRMapper,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PHASE00_ROOT = REPO_ROOT / "tests" / "fixtures" / "phase00"
SAMPLE_KEYS = (
    "annual_report_excerpt",
    "ir_activity",
    "short_announcement",
    "annual_report",
)
DATA_MARKER = "/data/"

SAMPLE_METADATA = {
    "annual_report_excerpt": {
        "source_pdf": "raw_documents/cninfo/002484/2026/1225087169/sha256_excerpt.pdf",
        "title": "江海股份 2025 年年度报告 excerpt",
        "sample_role": "clean_checkout_fixture",
    },
    "ir_activity": {
        "source_pdf": (
            "tmp/sample_filings/000333_美的集团/"
            "2025-04-11__investor_relations__000333__"
            "美的集团：2025年4月11日投资者关系活动记录表__1223071887.pdf"
        ),
        "title": "美的集团：2025年4月11日投资者关系活动记录表",
        "sample_role": "ir_activity.pdf",
    },
    "short_announcement": {
        "source_pdf": (
            "tmp/sample_filings/002484_江海股份/"
            "2026-06-18__risk_or_forecast__002484__"
            "江海股份：南通江海电容器股份有限公司关于股票交易异常波动的公告__1225376481.pdf"
        ),
        "title": "江海股份：股票交易异常波动公告",
        "sample_role": "short_announcement.pdf",
    },
    "annual_report": {
        "source_pdf": (
            "tmp/sample_filings/002484_江海股份/"
            "2026-04-10__periodic__002484__江海股份：2025年年度报告__1225087169.pdf"
        ),
        "title": "江海股份：2025年年度报告",
        "sample_role": "annual_report.pdf",
    },
}

PARSER_INFO = MinerUParserInfo(
    name="MinerU",
    package_version="3.4.0",
    backend="pipeline",
    method="auto",
    language="ch",
    formula=False,
    table=True,
)


def _read_ref(sample_key: str) -> dict[str, str]:
    ref_path = PHASE00_ROOT / sample_key / "parser_artifacts_ref.txt"
    values: dict[str, str] = {}
    for line in ref_path.read_text(encoding="utf-8").splitlines():
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        values[key] = value.strip()
    return values


def _content_list_path(sample_key: str) -> Path:
    ref_key = "annual_report" if sample_key == "annual_report_excerpt" else sample_key
    value = _read_ref(ref_key).get("Content list")
    if value is None:
        raise SystemExit(f"{ref_key}: parser_artifacts_ref.txt lacks Content list")
    path = Path(value)
    if not path.is_file():
        raise SystemExit(f"{ref_key}: content_list missing: {path}")
    return path


def _relpath(value: str | Path | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    if DATA_MARKER in text:
        return text.split(DATA_MARKER, 1)[1]
    return text


def _parser_artifacts(sample_key: str, content_list_path: Path) -> dict[str, str]:
    ref_key = "annual_report" if sample_key == "annual_report_excerpt" else sample_key
    values = _read_ref(ref_key)
    artifact_root = _relpath(values.get("Parser artifacts root") or content_list_path.parent)
    content_list = _relpath(content_list_path)
    markdown = _relpath(values.get("Markdown"))
    artifacts = {
        "artifact_root_relpath": artifact_root,
        "content_list_relpath": content_list,
    }
    if markdown is not None:
        artifacts["markdown_relpath"] = markdown
    return artifacts


def _content_list_for_sample(sample_key: str) -> tuple[list[dict[str, Any]], Path]:
    path = _content_list_path(sample_key)
    content_list = MinerUArtifactReader().read_content_list(path)
    if sample_key == "annual_report_excerpt":
        content_list = [
            item
            for item in content_list
            if isinstance(item.get("page_idx"), int) and item["page_idx"] <= 6
        ]
    return content_list, path


def _inject_fixture_fields(sample_key: str, normalized: dict[str, Any]) -> None:
    normalized["document_id"] = f"phase00_{sample_key}"
    normalized["sample_key"] = sample_key
    normalized["sample_role"] = SAMPLE_METADATA[sample_key]["sample_role"]
    for index, element in enumerate(normalized["elements"]):
        element["ir_id"] = f"{sample_key}_ir_{index:04d}"


def _write_excerpt_ref(content_list_path: Path) -> None:
    annual_values = _read_ref("annual_report")
    ref_path = PHASE00_ROOT / "annual_report_excerpt" / "parser_artifacts_ref.txt"
    ref_path.write_text(
        "\n".join(
            [
                f"Parser artifacts root: {annual_values.get('Parser artifacts root', content_list_path.parent)}",
                f"Content list: {content_list_path}",
                f"Markdown: {annual_values.get('Markdown', '')}",
                "Page range: page_idx <= 6 (pages 1-7) from annual_report content_list",
                "Note: excerpt fixture is regenerated from the full annual_report artifact.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _coverage_line(sample_key: str, normalized: dict[str, Any]) -> str:
    elements = normalized["elements"]
    heading_count = sum(1 for element in elements if element["kind"] == "heading")
    heading_level_count = sum(
        1 for element in elements if element.get("heading_level") is not None
    )
    return (
        f"{sample_key}: headings={heading_count} "
        f"heading_level={heading_level_count} total={len(elements)}"
    )


def regenerate_sample(sample_key: str) -> str:
    metadata = SAMPLE_METADATA[sample_key]
    content_list, content_list_path = _content_list_for_sample(sample_key)
    normalized = MinerUToNormalizedIRMapper().map_content_list(
        content_list=content_list,
        parser_info=PARSER_INFO,
        document_metadata={
            "document_id": f"phase00_{sample_key}",
            "source_pdf": metadata["source_pdf"],
            "title": metadata["title"],
        },
        parser_artifacts=_parser_artifacts(sample_key, content_list_path),
    )
    if sample_key == "annual_report_excerpt":
        normalized["parsed_pages"] = {
            "start_page_no": 1,
            "end_page_no": 7,
            "full_pdf": False,
        }
        _write_excerpt_ref(content_list_path)
    _inject_fixture_fields(sample_key, normalized)
    sample_dir = PHASE00_ROOT / sample_key
    output_path = sample_dir / "normalized_ir.v2.json"
    output_path.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    old_path = sample_dir / "normalized_ir.v1.json"
    if old_path.exists():
        old_path.unlink()
    return _coverage_line(sample_key, normalized)


def _selected_sample_keys(argv: list[str]) -> tuple[str, ...]:
    if not argv:
        return SAMPLE_KEYS
    unknown = sorted(set(argv) - set(SAMPLE_KEYS))
    if unknown:
        raise SystemExit(f"unknown sample_key(s): {', '.join(unknown)}")
    return tuple(argv)


def main(argv: list[str] | None = None) -> int:
    for sample_key in _selected_sample_keys(sys.argv[1:] if argv is None else argv):
        print(regenerate_sample(sample_key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
