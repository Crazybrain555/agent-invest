from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast
import unittest

from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import (
    MinerUMediumArtifactReader,
)
from disclosure_anchor.adapters.parsers.pdf_page_probe import count_pdf_pages
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.contracts.provider_document import ProviderDocument
from disclosure_anchor.application.contracts.provider_document_admission import (
    SourcePdfObservation,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelope,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderUnitBuildResult,
)
from disclosure_anchor.application.ports.file_store import FileStorePathPort
from disclosure_anchor.application.ports.provider_document_source import (
    ProviderDocumentSourcePort,
)
from disclosure_anchor.application.services.provider_document_admission import (
    ProviderDocumentAdmission,
)
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units,
    replay_provider_unit_search_binding,
)
from disclosure_anchor.domain import entities as e


_ROOT_ENV = "DISCLOSURE_MEDIUM_FROZEN_ROOT"
_SAMPLES = ("zhongke", "caitong", "jianghai")


class FrozenProviderUnitBuilderTests(unittest.TestCase):
    def test_source_admitted_medium_bundles_conserve_units_and_known_tables(
        self,
    ) -> None:
        root_value = os.environ.get(_ROOT_ENV)
        if not root_value:
            self.skipTest(f"{_ROOT_ENV} is not configured")
        root = Path(root_value)
        if not root.is_dir():
            self.skipTest(f"frozen Medium root is absent: {root}")
        manifests = _manifest_rows(root)

        results = {
            slug: _admit_and_build(root=root, row=manifests[slug])
            for slug in _SAMPLES
        }

        zhongke = results["zhongke"]
        self.assertIn((2, 5), _table_block_spans(zhongke))
        self.assertIn((6,), _table_block_spans(zhongke))

        caitong = results["caitong"]
        self.assertIn((4, 5, 6, 7), _table_block_spans(caitong))

        jianghai = results["jianghai"]
        self.assertIn((464, 467), _table_block_spans(jianghai))
        self.assertIn((889,), _table_block_spans(jianghai))
        self.assertIn((892,), _table_block_spans(jianghai))


def _admit_and_build(
    *,
    root: Path,
    row: dict[str, object],
) -> ProviderUnitBuildResult:
    slug = _text(row, "document_slug")
    source_sha = "sha256:" + _text(row, "sha256")
    source_path = Path(_text(row, "source_path"))
    evidence_path = (
        root
        / "candidates"
        / "mineru-3.4.4-hybrid-medium"
        / slug
        / "run-evidence.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if (
        evidence.get("status") != "succeeded_raw_observation"
        or evidence.get("invariant_errors") != []
    ):
        raise AssertionError(f"frozen Medium evidence is not successful: {slug}")
    bundle_candidates = tuple(
        (
            root
            / "candidates"
            / "mineru-3.4.4-hybrid-medium"
            / slug
            / "parser-output"
        ).glob("sha256_*/hybrid_auto")
    )
    if len(bundle_candidates) != 1:
        raise AssertionError(f"frozen Medium bundle is not unique: {slug}")
    bundle_path = bundle_candidates[0]
    if _sha_file(source_path) != source_sha:
        raise AssertionError(f"frozen source hash drifted: {slug}")
    page_count = count_pdf_pages(source_path)
    if page_count != row.get("page_count"):
        raise AssertionError(f"frozen source page count drifted: {slug}")
    provider_document = MinerUMediumArtifactReader().read(
        bundle_path,
        source_pdf_sha256=source_sha,
    )
    owner = f"run_frozen_{slug}"
    document_id = f"doc_frozen_{slug}"
    provider = _text(row, "provider")
    provider_document_id = _text(row, "provider_document_id")
    security_code = _text(row, "security_code")
    source_relpath = (
        f"raw_documents/{provider}/{security_code}/2026/{provider_document_id}/"
        f"sha256_{source_sha.removeprefix('sha256:')}.pdf"
    )
    bundle_relpath = (
        f"parser_artifacts/{provider}/{security_code}/{provider_document_id}/"
        f"{owner}/sha256_{source_sha.removeprefix('sha256:')}/hybrid_auto"
    )
    target = _target()
    envelope = ProviderDocumentEnvelope.build(
        document_id=document_id,
        artifact_owner_processing_run_id=owner,
        provider=provider,
        provider_document_id=provider_document_id,
        source_pdf_relpath=source_relpath,
        source_pdf_page_count=page_count,
        parser_artifact_root_relpath=bundle_relpath,
        parser_target_identity=target,
        provider_document=provider_document,
    )
    record = provider_document_envelope_to_bytes(envelope)
    record_relpath = Path(
        f"derived/provider_documents/{provider}/{security_code}/"
        f"{provider_document_id}/{owner}/provider_document.v1.json"
    )
    document = e.Document(
        document_id=document_id,
        status="registered",
        provider=provider,
        provider_document_id=provider_document_id,
        raw_file_relpath=source_relpath,
        raw_file_hash=source_sha,
    )
    run = e.ProcessingRun(
        processing_run_id=owner,
        document_id=document_id,
        artifact_owner_processing_run_id=owner,
        run_kind="parse",
        status="succeeded",
        parser_name=target.name,
        parser_version=target.package_version,
        parser_backend=target.backend,
        parser_method=target.method,
        parser_language=target.language,
        parser_target_identity=target.to_payload(),
        input_raw_file_hash=source_sha,
        parser_artifact_relpath=bundle_relpath,
        artifact_hash=_sha_bytes(record),
        normalized_ir_relpath=None,
        provider_document_relpath=record_relpath.as_posix(),
    )
    source = _FrozenSource(
        record=record,
        record_relpath=record_relpath,
        source_path=source_path,
        source_relpath=Path(source_relpath),
        bundle_path=bundle_path,
        bundle_relpath=Path(bundle_relpath),
    )
    admission = ProviderDocumentAdmission(
        path_builder=cast(FileStorePathPort, _ExpectedPath(record_relpath)),
        source=cast(ProviderDocumentSourcePort, source),
    )
    admitted = admission.admit(
        document=document,
        run=run,
        artifact_owner=run,
        security_code=security_code,
    )
    first = build_provider_units(admitted)
    second = build_provider_units(admitted)
    if first != second:
        raise AssertionError(f"provider Unit build is nondeterministic: {slug}")
    if first.unassigned_table_parts:
        raise AssertionError(f"frozen Medium table parts are unbound: {slug}")
    for unit in first.units:
        for binding in unit.locator.search_targets:
            replay_provider_unit_search_binding(admitted, unit, binding)
    return first


class _ExpectedPath:
    def __init__(self, expected: Path) -> None:
        self.expected = expected

    def provider_document_relpath(self, **_: object) -> Path:
        return self.expected


class _FrozenSource:
    def __init__(
        self,
        *,
        record: bytes,
        record_relpath: Path,
        source_path: Path,
        source_relpath: Path,
        bundle_path: Path,
        bundle_relpath: Path,
    ) -> None:
        self.record = record
        self.record_relpath = record_relpath
        self.source_path = source_path
        self.source_relpath = source_relpath
        self.bundle_path = bundle_path
        self.bundle_relpath = bundle_relpath

    def read_provider_document_record(self, relpath: Path) -> bytes:
        if relpath != self.record_relpath:
            raise AssertionError("admission requested the wrong provider record")
        return self.record

    def observe_source_pdf(self, relpath: Path) -> SourcePdfObservation:
        if relpath != self.source_relpath:
            raise AssertionError("admission requested the wrong source PDF")
        return SourcePdfObservation(
            sha256=_sha_file(self.source_path),
            page_count=count_pdf_pages(self.source_path),
        )

    def rebuild_provider_document(
        self,
        bundle_relpath: Path,
        *,
        source_pdf_sha256: str,
    ) -> ProviderDocument:
        if bundle_relpath != self.bundle_relpath:
            raise AssertionError("admission requested the wrong provider bundle")
        return MinerUMediumArtifactReader().read(
            self.bundle_path,
            source_pdf_sha256=source_pdf_sha256,
        )


def _manifest_rows(root: Path) -> dict[str, dict[str, object]]:
    path = root / "manifests" / "cgn-input-manifest.jsonl"
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {
        _text(row, "document_slug"): row
        for row in rows
        if _text(row, "document_slug") in _SAMPLES
    }


def _table_block_spans(
    result: ProviderUnitBuildResult,
) -> set[tuple[int, ...]]:
    return {
        part.block_source_indices
        for unit in result.units
        for part in unit.locator.parts
        if part.kind == "table" and part.logical_table_index is not None
    }


def _target() -> ParserTargetIdentity:
    return ParserTargetIdentity(
        backend="hybrid-http-client",
        effort="medium",
        formula=True,
        full_pdf=True,
        image_analysis=False,
        language="ch",
        method="auto",
        name="MinerU",
        package_version="3.4.4",
        runtime_bundle_identity_sha256="sha256:" + "c" * 64,
        table=True,
    )


def _text(row: dict[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"manifest field is invalid: {field}")
    return value


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _sha_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


if __name__ == "__main__":
    unittest.main()
