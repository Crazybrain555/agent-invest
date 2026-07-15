"""Read MinerU parser artifacts from a completed output directory."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from disclosure_anchor.domain.errors import ParserOutputContractError


@dataclass(frozen=True)
class MinerUArtifacts:
    root: Path
    content_list_path: Path
    markdown_path: Path | None
    model_path: Path | None


class MinerUArtifactReader:
    """Locate and read the stable MinerU artifacts Phase 04 depends on."""

    def locate(self, output_dir: Path) -> MinerUArtifacts:
        if not output_dir.is_dir():
            raise ParserOutputContractError(f"MinerU output directory is missing: {output_dir}")

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
        markdowns = sorted(content_list_path.parent.glob("*.md"))
        markdown_path = markdowns[0] if markdowns else None
        model_name = content_list_path.name.removesuffix(
            "_content_list.json"
        ) + "_model.json"
        model_candidate = content_list_path.with_name(model_name)
        return MinerUArtifacts(
            root=content_list_path.parent,
            content_list_path=content_list_path,
            markdown_path=markdown_path,
            model_path=model_candidate if model_candidate.is_file() else None,
        )

    def read_content_list(self, content_list_path: Path) -> list[dict[str, Any]]:
        try:
            data = json.loads(content_list_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ParserOutputContractError(
                f"cannot read MinerU content_list: {content_list_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ParserOutputContractError(
                f"invalid MinerU content_list JSON: {content_list_path}"
            ) from exc
        if not isinstance(data, list):
            raise ParserOutputContractError(
                f"MinerU content_list must be a list: {content_list_path}"
            )
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ParserOutputContractError(
                    f"MinerU content_list item {index} is not an object: {content_list_path}"
                )
        return data
