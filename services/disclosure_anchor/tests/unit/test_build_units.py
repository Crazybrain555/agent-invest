from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.adapters.storage.artifact_store import (
    ArtifactStore,
)
from disclosure_anchor.application.contracts.provider_document_admission import (
    ProviderDocumentAdmissionError,
)
from disclosure_anchor.application.contracts.provider_table_projection import (
    ProviderTablePartRef,
    UnboundProviderTablePart,
)
from disclosure_anchor.application.contracts.provider_unit import (
    PROVIDER_UNIT_BUILDER_VERSION,
    provider_unit_locator_from_payload,
)
from disclosure_anchor.application.ports.file_store import (
    ArtifactWriteResult,
)
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units,
)
from disclosure_anchor.application.use_cases import (
    build_units as build_module,
)
from disclosure_anchor.application.use_cases.build_units import (
    SNAPSHOT_KEYS,
    BuildUnits,
    BuildUnitsCommand,
)
from disclosure_anchor.domain import (
    entities as e,
)
from disclosure_anchor.domain.errors import (
    BuildUnitsError,
)
from tests.unit._fakes import FakeUnitOfWork
from tests.unit.test_provider_unit_builder import (
    _admitted,
    _representative_document,
)


class _Paths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def data_path(self, relpath: Path) -> Path:
        return self.root / relpath

    def document_units_snapshot_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        processing_run_id: str,
    ) -> Path:
        return (
            Path("derived/document_unit_snapshots")
            / provider
            / security_code
            / provider_document_id
            / processing_run_id
            / "document_units.v1.jsonl"
        )


class _Admission:
    def __init__(self, *, error: ProviderDocumentAdmissionError | None = None) -> None:
        self.error = error
        self.admitted = _admitted(_representative_document())
        self.calls = 0

    def admit(self, **_kwargs: object):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.admitted


class _BadHashArtifactStore(ArtifactStore):
    def write_jsonl_atomic(
        self,
        *,
        relpath: Path,
        rows: list[object],
    ) -> ArtifactWriteResult:
        result = super().write_jsonl_atomic(relpath=relpath, rows=rows)
        return replace(result, artifact_hash="sha256:" + "0" * 64)


def _uow(*, legacy: bool = False) -> FakeUnitOfWork:
    uow = FakeUnitOfWork()
    company = uow.companies.add(e.Company(company_id="co_1", legal_name="江海股份"))
    security = uow.securities.add(
        e.Security(
            security_id="sec_1",
            company_id=company.company_id,
            security_code="002484",
            exchange="SZSE",
        )
    )
    document = uow.documents.add(
        e.Document(
            document_id="doc_1",
            status="parsed",
            company_id=company.company_id,
            security_id=security.security_id,
            provider="cninfo",
            provider_document_id="pid_1",
            raw_file_relpath=(
                "raw_documents/cninfo/002484/2026/pid_1/sha256_"
                + "a" * 64
                + ".pdf"
            ),
            raw_file_hash="sha256:" + "a" * 64,
        )
    )
    uow.processing_runs.add(
        e.ProcessingRun(
            processing_run_id="run_1",
            document_id=document.document_id,
            artifact_owner_processing_run_id="run_1",
            run_kind="parse",
            status="succeeded",
            parser_name="MinerU",
            parser_version="3.4.4",
            parser_backend="hybrid-http-client",
            parser_method="auto",
            parser_language="ch",
            parser_target_identity={},
            input_raw_file_hash=document.raw_file_hash,
            parser_artifact_relpath="parser_artifacts/x/hybrid_auto",
            artifact_hash="sha256:" + "b" * 64,
            normalized_ir_relpath=("derived/normalized_ir/v4.json" if legacy else None),
            provider_document_relpath=(
                None
                if legacy
                else "derived/provider_documents/x/provider_document.v1.json"
            ),
        )
    )
    return uow


def _use_case(
    root: Path,
    uow: FakeUnitOfWork,
    *,
    admission: _Admission | None = None,
    artifact_store: ArtifactStore | None = None,
) -> tuple[BuildUnits, _Admission]:
    paths = _Paths(root)
    source_admission = admission or _Admission()
    return (
        BuildUnits(
            path_builder=paths,
            artifact_store=artifact_store or ArtifactStore(paths),
            uow_factory=lambda: uow,
            admission=source_admission,  # type: ignore[arg-type]
        ),
        source_admission,
    )


class BuildUnitsTests(unittest.TestCase):
    def test_materializes_provider_units_snapshot_and_hash_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            uow = _uow()
            use_case, admission = _use_case(Path(tmp), uow)
            result = use_case.execute(BuildUnitsCommand(processing_run_id="run_1"))

            self.assertEqual(result.status, "succeeded")
            self.assertGreater(result.unit_count, 0)
            self.assertEqual(admission.calls, 1)
            run = uow.processing_runs.get("run_1")
            assert run is not None and run.document_units_relpath is not None
            self.assertEqual(run.builder_rules_version, PROVIDER_UNIT_BUILDER_VERSION)
            units = uow.document_units.list_by_processing_run("run_1")
            self.assertEqual(len(units), result.unit_count)
            self.assertEqual(
                [unit.order_index for unit in units],
                list(range(1, len(units) + 1)),
            )
            for unit in units:
                provider_unit_locator_from_payload(unit.artifact_locator)
                self.assertIsNone(unit.semantic_key)
                self.assertIsNone(unit.applicability)
            rows = (Path(tmp) / run.document_units_relpath).read_text().splitlines()
            self.assertEqual(len(rows), len(units))
            self.assertEqual(set(result.build_stats or {}), {
                "contract_version",
                "builder_rules_version",
                "provider_document_sha256",
                "unit_count",
                "unassigned_table_part_count",
            })

    def test_rejects_legacy_output_before_source_admission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            admission = _Admission()
            use_case, _ = _use_case(Path(tmp), _uow(legacy=True), admission=admission)
            with self.assertRaises(BuildUnitsError) as caught:
                use_case.execute(BuildUnitsCommand(processing_run_id="run_1"))
        self.assertEqual(caught.exception.error["error_code"], "RUN_OUTPUT_CONTRACT_UNSUPPORTED")
        self.assertEqual(admission.calls, 0)

    def test_admission_and_unassigned_table_failures_are_persisted(self) -> None:
        cases: list[tuple[_Admission, str, object | None]] = [
            (
                _Admission(
                    error=ProviderDocumentAdmissionError(
                        "provider_document_hash_mismatch",
                        "record drifted",
                    )
                ),
                "PROVIDER_DOCUMENT_ADMISSION_FAILED",
                None,
            )
        ]
        base_admission = _Admission()
        build = build_provider_units(base_admission.admitted)
        unbound = UnboundProviderTablePart(
            part=ProviderTablePartRef(
                block_source_index=None,
                physical_segment_index=0,
            ),
            reason="provider_status_unbound",
        )
        cases.append(
            (
                base_admission,
                "UNASSIGNED_TABLE_EVIDENCE",
                replace(build, unassigned_table_parts=(unbound,)),
            )
        )
        for admission, code, replacement in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                uow = _uow()
                use_case, _ = _use_case(Path(tmp), uow, admission=admission)
                patcher = (
                    mock.patch.object(build_module, "build_provider_units", return_value=replacement)
                    if replacement is not None
                    else mock.patch.object(build_module, "build_provider_units", wraps=build_provider_units)
                )
                with patcher:
                    result = use_case.execute(BuildUnitsCommand(processing_run_id="run_1"))
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error["error_code"], code)

    def test_recomputes_draft_hashes_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            uow = _uow()
            use_case, admission = _use_case(Path(tmp), uow)
            build = build_provider_units(admission.admitted)
            bad = replace(
                build,
                units=(
                    replace(build.units[0], content_hash="sha256:" + "0" * 64),
                    *build.units[1:],
                ),
            )
            with mock.patch.object(build_module, "build_provider_units", return_value=bad):
                result = use_case.execute(BuildUnitsCommand(processing_run_id="run_1"))
        self.assertEqual(result.error["error_code"], "PROVIDER_UNIT_HASH_MISMATCH")
        self.assertEqual(uow.document_units.list_by_processing_run("run_1"), [])

    def test_snapshot_hash_mismatch_never_persists_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uow = _uow()
            paths = _Paths(root)
            use_case, _ = _use_case(
                root,
                uow,
                artifact_store=_BadHashArtifactStore(paths),
            )
            result = use_case.execute(BuildUnitsCommand(processing_run_id="run_1"))
        self.assertEqual(result.error["error_code"], "ARTIFACT_WRITE_FAILED")
        self.assertEqual(uow.document_units.list_by_processing_run("run_1"), [])

    def test_snapshot_row_contract_remains_stable(self) -> None:
        self.assertEqual(
            SNAPSHOT_KEYS,
            {
                "applicability", "page_no", "artifact_locator", "asset_id",
                "content_hash", "document_id", "heading_path", "order_index",
                "payload", "payload_kind", "quality_status", "semantic_key",
                "title",
            },
        )


__all__ = ["BuildUnitsTests"]
