"""Concrete MinerU parser adapter."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from disclosure_anchor.adapters.parsers.mineru.artifact_reader import (
    MinerUArtifactReader,
)
from disclosure_anchor.adapters.parsers.mineru.existing_artifact_pipeline import (
    build_current_ir_from_mineru_artifacts,
)
from disclosure_anchor.adapters.parsers.mineru.mapper_to_ir import (
    MinerUToNormalizedIRMapper,
)
from disclosure_anchor.adapters.parsers.mineru.mineru_process import MinerUProcess
from disclosure_anchor.adapters.parsers.mineru.visual_semantic_enricher import (
    MinerUVisualSemanticEnricher,
)
from disclosure_anchor.application.ports.parser import (
    ParserIdentity,
    ParserOptions,
    ParserResult,
)
from disclosure_anchor.domain.errors import ParserOutputContractError


__all__ = ["MinerUDocumentParser"]


class MinerUDocumentParser:
    """Parse a PDF with MinerU and return parser-neutral NormalizedIR data."""

    def __init__(
        self,
        *,
        process: MinerUProcess,
        reader: MinerUArtifactReader | None = None,
        mapper: MinerUToNormalizedIRMapper | None = None,
        parser_version: str | None = None,
        server_url: str | None = None,
    ) -> None:
        self._process = process
        self._reader = reader or MinerUArtifactReader()
        self._mapper = mapper or MinerUToNormalizedIRMapper()
        self._version_cache: str | None = parser_version
        self._server_url = server_url

    def identity(self) -> ParserIdentity:
        if self._version_cache is None:
            self._version_cache = self._process.version()
        return ParserIdentity(
            name="MinerU",
            version=self._version_cache,
        )

    def readiness(self, options: ParserOptions | None = None) -> None:
        """Check parser runtime dependencies without changing its identity."""

        self.identity()
        if options is not None and not options.backend.endswith("-http-client"):
            return
        server_url = (
            options.server_url
            if options is not None and options.server_url
            else self._server_url
        )
        if server_url:
            self._process.probe_server(server_url)

    def parse(
        self,
        *,
        input_pdf: Path,
        output_dir: Path,
        options: ParserOptions,
        document_metadata: dict[str, Any],
    ) -> ParserResult:
        identity = self.identity()
        self._process.run(input_pdf=input_pdf, output_dir=output_dir, options=options)
        artifacts = self._reader.locate(output_dir)
        content_list_path = artifacts.paths["content_list"]
        assert content_list_path is not None
        model_path = artifacts.paths["model"]
        content_artifact = self._reader.read_content_artifact(
            content_list_path
        )
        artifact_stem = content_list_path.name.removesuffix("_content_list.json")
        content_list_v2_path = artifacts.paths["content_list_v2"]
        if content_list_v2_path is None:
            raise ParserOutputContractError(
                "MinerU content_list_v2 is required for canonical text closure"
            )
        content_list_v2 = self._reader.read_content_list_v2(
            content_list_v2_path
        )
        middle_path = artifacts.paths["middle"]
        if middle_path is None:
            raise ParserOutputContractError(
                "MinerU middle artifact is required for source role closure"
            )
        source_pdf_sha256 = document_metadata.get("raw_file_hash")
        if not isinstance(source_pdf_sha256, str) or not source_pdf_sha256:
            raise ParserOutputContractError(
                "registered raw PDF hash is required for structure extraction"
            )
        parser_info = options.target_identity(identity)
        server_url = options.server_url or self._server_url
        build = build_current_ir_from_mineru_artifacts(
            raw_pdf_path=input_pdf,
            source_pdf_sha256=source_pdf_sha256,
            content_artifact=content_artifact,
            content_list_v2=content_list_v2,
            middle_path=middle_path,
            model_path=model_path,
            parser_info=parser_info,
            document_metadata=document_metadata,
            visual_output_dir=content_list_path.with_name(
                artifact_stem + "_source_page_visuals"
            ),
            start_page=options.start_page,
            end_page=options.end_page,
            mapper=self._mapper,
            server_url=server_url,
            visual_semantic_extractor=MinerUVisualSemanticEnricher(
                process=self._process,
                options=options,
                server_url=server_url or "",
            ),
        )
        native_path = content_list_path.with_name(artifact_stem + "_pdf_structure.json")
        source_evidence_path = content_list_path.with_name(
            artifact_stem + "_source_evidence.json"
        )
        visual_semantics_path = content_list_path.with_name(
            artifact_stem + "_visual_semantics.json"
        )
        _write_json_artifact(
            native_path,
            build.native_structure,
            label="native PDF structure",
        )
        _write_json_artifact(
            source_evidence_path,
            build.source_evidence,
            label="source evidence",
        )
        _write_bytes_artifact(
            visual_semantics_path,
            build.visual_semantics_bytes,
            label="visual semantics",
        )
        return ParserResult(
            target_identity=parser_info,
            artifact_root=artifacts.root,
            artifact_paths={
                **artifacts.paths,
                **build.evidence_image_paths,
                **{
                    visual.artifact_role: visual.artifact_path
                    for visual in build.visual_evidence
                },
                "pdf_structure": native_path,
                "source_evidence": source_evidence_path,
                "visual_semantics": visual_semantics_path,
            },
            normalized_ir=build.normalized_ir,
        )


def _write_json_artifact(
    path: Path,
    payload: Mapping[str, Any],
    *,
    label: str,
) -> None:
    try:
        path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot write {label} artifact: {path}"
        ) from exc


def _write_bytes_artifact(
    path: Path,
    payload: bytes,
    *,
    label: str,
) -> None:
    try:
        path.write_bytes(payload)
    except OSError as exc:
        raise ParserOutputContractError(
            f"cannot write {label} artifact: {path}"
        ) from exc
