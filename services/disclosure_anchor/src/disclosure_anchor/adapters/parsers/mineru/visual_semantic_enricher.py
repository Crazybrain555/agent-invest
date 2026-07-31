"""Thin application-side caller for MinerU's official content extractor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from disclosure_anchor.adapters.parsers.mineru.mineru_process import MinerUProcess
from disclosure_anchor.adapters.parsers.mineru.visual_semantic_closure import (
    VisualContentExtractRequest,
    VisualContentExtractResult,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.domain.errors import (
    ParserOutputContractError,
    ParserTaskError,
)


class MinerUVisualSemanticEnricher:
    """Call only the installed ``MinerUClient.content_extract`` bridge."""

    def __init__(
        self,
        *,
        process: MinerUProcess,
        options: ParserOptions,
        server_url: str,
    ) -> None:
        self._process = process
        self._options = options
        self._server_url = server_url

    def __call__(
        self,
        requests: tuple[VisualContentExtractRequest, ...],
    ) -> VisualContentExtractResult:
        if not requests:
            raise ParserOutputContractError(
                "visual enricher cannot be called with an empty request"
            )
        payload = {
            "server_url": self._server_url,
            "max_concurrency": (
                self._options.http_request_concurrency or 8
            ),
            "items": [
                {
                    "item_id": request.occurrence_id,
                    "path": str(request.artifact_path),
                    "sha256": request.artifact_sha256,
                    "visual_type": request.content_type,
                }
                for request in requests
            ],
        }
        result = self._process.run_runtime_helper(
            script=Path(__file__).with_name("content_extract_runtime.py"),
            input_payload=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            options=self._options,
        )
        try:
            decoded: Any = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ParserOutputContractError(
                "MinerU content-extract helper returned invalid JSON"
            ) from exc
        if result.returncode != 0:
            error_type = (
                decoded.get("error_type") if isinstance(decoded, dict) else None
            )
            message = decoded.get("message") if isinstance(decoded, dict) else None
            if error_type in {
                "FileNotFoundError",
                "IsADirectoryError",
                "UnidentifiedImageError",
                "ValueError",
            }:
                raise ParserOutputContractError(
                    "MinerU content-extract input/output contract failed: "
                    f"{error_type}: {message}"
                )
            raise ParserTaskError(
                "MinerU content-extract runtime failed: "
                f"{error_type or 'unknown'}: {message or result.stderr[:400]}"
            )
        if not isinstance(decoded, dict) or set(decoded) != {
            "mineru_vl_utils_version",
            "outputs",
        }:
            raise ParserOutputContractError(
                "MinerU content-extract response fields are not closed"
            )
        outputs = decoded["outputs"]
        if not isinstance(outputs, list) or len(outputs) != len(requests):
            raise ParserOutputContractError(
                "MinerU content-extract response count differs"
            )
        values: list[str | None] = []
        for request, raw_output in zip(requests, outputs, strict=True):
            if (
                not isinstance(raw_output, dict)
                or set(raw_output) != {"item_id", "text"}
                or raw_output.get("item_id") != request.occurrence_id
                or (
                    raw_output.get("text") is not None
                    and not isinstance(raw_output.get("text"), str)
                )
            ):
                raise ParserOutputContractError(
                    "MinerU content-extract response identity differs"
                )
            values.append(raw_output["text"])
        package_version = decoded["mineru_vl_utils_version"]
        if not isinstance(package_version, str) or not package_version:
            raise ParserOutputContractError(
                "MinerU content-extract runtime version is absent"
            )
        return VisualContentExtractResult(
            mineru_vl_utils_version=package_version,
            values=tuple(values),
        )


__all__ = ["MinerUVisualSemanticEnricher"]
