"""Milestone 05 real-MinerU raw->parse->build->publish smoke.

This is explicit opt-in and intentionally not named ``test_*.py``. It runs the
three acceptance PDFs through the real MinerU binary, writes artifacts under a
temporary root, and cleans the rows it creates from the shared local database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.adapters.parsers.mineru.mineru_process import MinerUProcess
from disclosure_anchor.adapters.parsers.mineru.parser import MinerUDocumentParser
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.application.use_cases.build_units import BuildUnits, BuildUnitsCommand
from disclosure_anchor.application.use_cases.parse_document import ParseDocument, ParseDocumentCommand
from disclosure_anchor.application.use_cases.publish_run import PublishRun, PublishRunCommand
from disclosure_anchor.application.use_cases.register_local_pdf import (
    RegisterLocalPdf,
    RegisterLocalPdfCommand,
)
from disclosure_anchor.domain.value_objects import ReportPeriod
from disclosure_anchor.settings import Settings
from tests.integration._support import engine_or_skip, numeric_provider_document_id


SAMPLE_ROOT = Path("tmp/sample_filings")


@dataclass(frozen=True)
class Sample:
    label: str
    pdf: Path
    company_legal_name: str
    security_code: str
    exchange: str
    filing_type: str
    title: str
    announcement_date: date
    report_period: ReportPeriod | None = None


SAMPLES = (
    Sample(
        label="annual_report",
        pdf=SAMPLE_ROOT
        / "002484_江海股份"
        / "2026-04-10__periodic__002484__江海股份：2025年年度报告__1225087169.pdf",
        company_legal_name="南通江海电容器股份有限公司",
        security_code="002484",
        exchange="szse",
        filing_type="annual_report",
        title="江海股份：2025年年度报告",
        announcement_date=date(2026, 4, 10),
        report_period=ReportPeriod.parse("2025A"),
    ),
    Sample(
        label="ir_activity",
        pdf=SAMPLE_ROOT
        / "000333_美的集团"
        / "2025-04-11__investor_relations__000333__美的集团：2025年4月11日投资者关系活动记录表__1223071887.pdf",
        company_legal_name="美的集团股份有限公司",
        security_code="000333",
        exchange="szse",
        filing_type="investor_relations",
        title="美的集团：2025年4月11日投资者关系活动记录表",
        announcement_date=date(2025, 4, 11),
    ),
    Sample(
        label="short_announcement",
        pdf=SAMPLE_ROOT
        / "002484_江海股份"
        / "2026-06-18__risk_or_forecast__002484__江海股份：南通江海电容器股份有限公司关于股票交易异常波动的公告__1225376481.pdf",
        company_legal_name="南通江海电容器股份有限公司",
        security_code="002484",
        exchange="szse",
        filing_type="other",
        title="江海股份：关于股票交易异常波动的公告",
        announcement_date=date(2026, 6, 18),
    ),
)


def _mineru_bin_or_skip() -> Path:
    raw = os.environ.get("DISCLOSURE_MINERU_BIN")
    if not raw:
        raise unittest.SkipTest("DISCLOSURE_MINERU_BIN not set; real-MinerU smoke off")
    path = Path(raw)
    if not (path.is_file() and os.access(path, os.X_OK)):
        raise unittest.SkipTest(f"MinerU binary not executable: {path}")
    return path


class Milestone05RealMinerUBuildPublishSmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.mineru = _mineru_bin_or_skip()
        self.provider_document_ids: list[str] = []

    def tearDown(self) -> None:
        for provider_document_id in self.provider_document_ids:
            self._cleanup_provider_document_id(provider_document_id)
        self.engine.dispose()

    def test_three_samples_raw_parse_build_publish(self) -> None:
        missing = [sample.pdf for sample in SAMPLES if not sample.pdf.is_file()]
        if missing:
            raise unittest.SkipTest(f"sample PDF absent: {missing[0]}")
        with tempfile.TemporaryDirectory(prefix="m05-real-mineru-") as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            paths = FileStorePathBuilder(settings)
            raw_store = RawDocumentStore(paths)
            artifact_store = ArtifactStore(paths)

            results = {}
            for sample in SAMPLES:
                result = self._run_sample(
                    sample=sample,
                    settings=settings,
                    paths=paths,
                    raw_store=raw_store,
                    artifact_store=artifact_store,
                )
                results[sample.label] = result
                print(
                    "[m05-e2e] "
                    f"{sample.label} document_id={result['document_id']} "
                    f"run_id={result['run_id']} units={result['unit_count']} "
                    f"qa={result['qa_count']}"
                )

            self._assert_annual_report(results["annual_report"]["run_id"])
            self._assert_ir_activity(results["ir_activity"]["run_id"])
            self._assert_snapshot_payload_contract(
                paths=paths,
                run_id=results["short_announcement"]["run_id"],
            )

    def _settings(self, root: Path) -> Settings:
        data_root = root / "services" / "disclosure_anchor"
        shared_root = root / "shared"
        return Settings(
            disclosure_data_root=data_root,
            disclosure_shared_root=shared_root,
            disclosure_runtime_root=data_root / "runtime",
            mineru_model_cache=Path(
                os.environ.get(
                    "MINERU_MODEL_CACHE", str(shared_root / "model_cache" / "mineru")
                )
            ),
            hf_home=Path(
                os.environ.get(
                    "HF_HOME", str(shared_root / "model_cache" / "huggingface")
                )
            ),
            modelscope_cache=Path(
                os.environ.get(
                    "MODELSCOPE_CACHE", str(shared_root / "model_cache" / "modelscope")
                )
            ),
        )

    def _uow(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(engine=self.engine)

    def _run_sample(
        self,
        *,
        sample: Sample,
        settings: Settings,
        paths: FileStorePathBuilder,
        raw_store: RawDocumentStore,
        artifact_store: ArtifactStore,
    ) -> dict[str, object]:
        provider_document_id = numeric_provider_document_id()
        self.provider_document_ids.append(provider_document_id)
        register = RegisterLocalPdf(raw_store=raw_store, uow_factory=self._uow)
        registered = register.execute(
            RegisterLocalPdfCommand(
                file_path=sample.pdf,
                company_legal_name=sample.company_legal_name,
                security_code=sample.security_code,
                exchange=sample.exchange,
                filing_type=sample.filing_type,
                title=sample.title,
                announcement_date=sample.announcement_date,
                provider_document_id=provider_document_id,
                provider="cninfo",
                report_period=sample.report_period,
            )
        )
        self.assertIsNotNone(registered.document_id)

        parser = ParseDocument(
            parser=MinerUDocumentParser(process=MinerUProcess(executable=self.mineru)),
            path_builder=paths,
            raw_store=raw_store,
            artifact_store=artifact_store,
            uow_factory=self._uow,
            default_timeout_seconds=1800,
        )
        parsed = parser.execute(ParseDocumentCommand(document_id=registered.document_id))
        self.assertEqual(parsed.status, "succeeded", parsed.error)

        built = BuildUnits(
            path_builder=paths,
            artifact_store=artifact_store,
            uow_factory=self._uow,
        ).execute(BuildUnitsCommand(processing_run_id=parsed.processing_run_id))
        self.assertEqual(built.status, "succeeded", built.error)
        self.assertGreater(built.unit_count, 0)

        published = PublishRun(uow_factory=self._uow).execute(
            PublishRunCommand(processing_run_id=parsed.processing_run_id)
        )
        self.assertEqual(published.status, "published")

        with self.engine.connect() as conn:
            document = conn.execute(
                text(
                    "SELECT status, current_processing_run_id "
                    "FROM disclosure_core.document WHERE document_id=:document_id"
                ),
                {"document_id": registered.document_id},
            ).mappings().one()
            public_count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_public.document_units_v1 "
                    "WHERE document_id=:document_id"
                ),
                {"document_id": registered.document_id},
            ).scalar_one()
            qa_count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.document_unit "
                    "WHERE processing_run_id=:run_id AND payload_kind='qa'"
                ),
                {"run_id": parsed.processing_run_id},
            ).scalar_one()
        self.assertEqual(document["status"], "published")
        self.assertEqual(document["current_processing_run_id"], parsed.processing_run_id)
        self.assertEqual(public_count, built.unit_count)
        return {
            "document_id": registered.document_id,
            "run_id": parsed.processing_run_id,
            "unit_count": built.unit_count,
            "qa_count": qa_count,
        }

    def _assert_annual_report(self, run_id: str) -> None:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT payload_kind, title, heading_path, semantic_key, payload "
                    "FROM disclosure_core.document_unit WHERE processing_run_id=:run_id"
                ),
                {"run_id": run_id},
            ).mappings().all()
        self.assertTrue(
            any(
                row["payload_kind"] == "text"
                and "管理层讨论与分析" in " ".join(row["heading_path"] or [])
                and row["payload"].get("text")
                for row in rows
            )
        )
        receivable_tables = [
            row
            for row in rows
            if row["payload_kind"] == "table"
            and row["semantic_key"] == "receivable_aging"
        ]
        self.assertTrue(receivable_tables)
        self.assertTrue(any(row["payload"].get("headers") for row in receivable_tables))
        self.assertTrue(any(row["payload"].get("rows") for row in receivable_tables))
        self.assertTrue(any(row["payload"].get("unit") for row in receivable_tables))

    def _assert_ir_activity(self, run_id: str) -> None:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT payload, heading_path FROM disclosure_core.document_unit "
                    "WHERE processing_run_id=:run_id AND payload_kind='qa'"
                ),
                {"run_id": run_id},
            ).mappings().all()
        self.assertGreaterEqual(len(rows), 30)
        self.assertTrue(
            any(
                "美国加征关税" in row["payload"].get("question", "")
                and "美国收入占比很低" in row["payload"].get("answer", "")
                for row in rows
            )
        )
        polluted = [
            path
            for row in rows
            for path in (row["heading_path"] or [])
            if "?" in path or "？" in path
        ]
        self.assertEqual(polluted, [])

    def _assert_snapshot_payload_contract(
        self, *, paths: FileStorePathBuilder, run_id: str
    ) -> None:
        with self.engine.connect() as conn:
            unit = conn.execute(
                text(
                    "SELECT asset_id, payload FROM disclosure_core.document_unit "
                    "WHERE processing_run_id=:run_id ORDER BY order_index LIMIT 1"
                ),
                {"run_id": run_id},
            ).mappings().one()
            run = conn.execute(
                text(
                    "SELECT document_units_relpath FROM disclosure_core.processing_run "
                    "WHERE processing_run_id=:run_id"
                ),
                {"run_id": run_id},
            ).mappings().one()
        snapshot_path = paths.data_path(Path(run["document_units_relpath"]))
        snapshot_rows = [
            json.loads(line)
            for line in snapshot_path.read_text(encoding="utf-8").splitlines()
        ]
        snapshot = {
            row["asset_id"]: row
            for row in snapshot_rows
        }[unit["asset_id"]]
        self.assertEqual(snapshot["payload"], unit["payload"])
        self.assertNotIn("query_projection_hash", snapshot)
        self.assertFalse(_contains_snapshot_reference(snapshot["payload"]))

    def _cleanup_provider_document_id(self, provider_document_id: str) -> None:
        with self.engine.begin() as conn:
            document_ids = [
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT document_id FROM disclosure_core.document "
                        "WHERE provider='cninfo' AND provider_document_id=:pid"
                    ),
                    {"pid": provider_document_id},
                )
            ]
            for document_id in document_ids:
                conn.execute(
                    text("DELETE FROM disclosure_ops.outbox_event WHERE document_id=:id"),
                    {"id": document_id},
                )
                conn.execute(
                    text("DELETE FROM disclosure_core.document_unit WHERE document_id=:id"),
                    {"id": document_id},
                )
                conn.execute(
                    text("DELETE FROM disclosure_core.processing_run WHERE document_id=:id"),
                    {"id": document_id},
                )
                conn.execute(
                    text("DELETE FROM disclosure_core.document WHERE document_id=:id"),
                    {"id": document_id},
                )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.source_access "
                    "WHERE query_params ->> 'provider_document_id' = :pid"
                ),
                {"pid": provider_document_id},
            )


def _contains_snapshot_reference(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            key in {"document_units_relpath", "snapshot_relpath", "snapshot_path"}
            or _contains_snapshot_reference(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_snapshot_reference(item) for item in value)
    return False


if __name__ == "__main__":
    unittest.main()
