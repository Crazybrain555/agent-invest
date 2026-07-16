"""Real-MinerU end-to-end smoke test (triple-gated, off by default).

The rest of the suite exercises the parse pipeline with fake parsers only;
this test runs the actual MinerU CLI once over a small real filing so parser
upgrades and mapper regressions surface locally. It skips cleanly unless ALL
of the following hold:

- the migrated database is reachable (``engine_or_skip``),
- ``DISCLOSURE_MINERU_BIN`` points at an executable MinerU CLI,
- the phase00 short-announcement sample PDF referenced by
  ``tests/fixtures/phase00/short_announcement/parser_artifacts_ref.txt`` exists.

Run it explicitly, e.g.::

    DISCLOSURE_MINERU_BIN=/Volumes/AgentSSD/agent_system/services/\
disclosure_anchor/runtime/venvs/mineru-phase00/bin/mineru \
    DISCLOSURE_MIGRATION_DATABASE_URL=... \
    PYTHONPATH=src .venv/bin/python -m unittest \
    tests.integration.test_real_mineru_smoke -v
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests.integration._support import engine_or_skip, numeric_provider_document_id

FIXTURE_REF = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "phase00"
    / "short_announcement"
    / "parser_artifacts_ref.txt"
)


def _mineru_bin_or_skip() -> Path:
    raw = os.environ.get("DISCLOSURE_MINERU_BIN")
    if not raw:
        raise unittest.SkipTest("DISCLOSURE_MINERU_BIN not set; real-MinerU smoke off")
    path = Path(raw)
    if not (path.is_file() and os.access(path, os.X_OK)):
        raise unittest.SkipTest(f"MinerU binary not executable: {path}")
    return path


def _sample_pdf_or_skip() -> Path:
    if not FIXTURE_REF.is_file():
        raise unittest.SkipTest("short_announcement parser_artifacts_ref.txt absent")
    match = re.search(r"^Source PDF: (.+)$", FIXTURE_REF.read_text(), re.MULTILINE)
    if match is None:
        raise unittest.SkipTest("no Source PDF line in parser_artifacts_ref.txt")
    pdf = Path(match.group(1).strip())
    if not pdf.is_file():
        raise unittest.SkipTest(f"sample PDF absent: {pdf}")
    return pdf


class RealMinerUSmokeTest(unittest.TestCase):
    """Register + parse one real filing through the real MinerU CLI."""

    def setUp(self) -> None:
        from sqlalchemy import text

        self.engine = engine_or_skip()
        self.mineru = _mineru_bin_or_skip()
        self.pdf = _sample_pdf_or_skip()
        self.text = text
        self.pid = numeric_provider_document_id()
        self._cleanup()

    def tearDown(self) -> None:
        self._cleanup()
        self.engine.dispose()

    def _cleanup(self) -> None:
        with self.engine.begin() as conn:
            doc_ids = [
                row[0]
                for row in conn.execute(
                    self.text(
                        "SELECT document_id FROM disclosure_core.document "
                        "WHERE provider_document_id = :pid"
                    ),
                    {"pid": self.pid},
                )
            ]
            for doc_id in doc_ids:
                conn.execute(
                    self.text(
                        "DELETE FROM disclosure_core.processing_run "
                        "WHERE document_id = :id"
                    ),
                    {"id": doc_id},
                )
                conn.execute(
                    self.text(
                        "DELETE FROM disclosure_ops.outbox_event WHERE document_id = :id"
                    ),
                    {"id": doc_id},
                )
                conn.execute(
                    self.text(
                        "DELETE FROM disclosure_core.document WHERE document_id = :id"
                    ),
                    {"id": doc_id},
                )
            conn.execute(
                self.text(
                    "DELETE FROM disclosure_core.source_access "
                    "WHERE query_params ->> 'provider_document_id' = :pid"
                ),
                {"pid": self.pid},
            )

    def test_register_and_parse_real_pdf_with_real_mineru(self) -> None:
        from disclosure_anchor.adapters.db.postgres.unit_of_work import (
            SqlAlchemyUnitOfWork,
        )
        from disclosure_anchor.adapters.parsers.mineru.mineru_process import (
            MinerUProcess,
        )
        from disclosure_anchor.adapters.parsers.mineru.parser import (
            MinerUDocumentParser,
        )
        from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
        from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
        from disclosure_anchor.adapters.storage.raw_document_store import (
            RawDocumentStore,
        )
        from disclosure_anchor.application.use_cases.parse_document import (
            ParseDocument,
            ParseDocumentCommand,
        )
        from disclosure_anchor.application.use_cases.register_local_pdf import (
            RegisterLocalPdf,
            RegisterLocalPdfCommand,
        )
        from disclosure_anchor.settings import Settings

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_root = root / "services" / "disclosure_anchor"
            shared_root = root / "shared"
            settings = Settings(
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
                        "MODELSCOPE_CACHE",
                        str(shared_root / "model_cache" / "modelscope"),
                    )
                ),
            )
            paths = FileStorePathBuilder(settings=settings)
            raw_store = RawDocumentStore(paths)
            artifact_store = ArtifactStore(paths)
            uow_factory = lambda: SqlAlchemyUnitOfWork(engine=self.engine)  # noqa: E731

            register = RegisterLocalPdf(raw_store=raw_store, uow_factory=uow_factory)
            registered = register.execute(
                RegisterLocalPdfCommand(
                    file_path=self.pdf,
                    company_legal_name="南通江海电容器股份有限公司",
                    security_code="002484",
                    exchange="szse",
                    filing_type="other",
                    title="关于股票交易异常波动的公告（real-MinerU smoke）",
                    announcement_date=date(2026, 6, 18),
                    provider_document_id=self.pid,
                    provider="cninfo",
                )
            )
            self.assertIsNotNone(registered.document_id)

            parse = ParseDocument(
                parser=MinerUDocumentParser(process=MinerUProcess(executable=self.mineru)),
                path_builder=paths,
                raw_store=raw_store,
                artifact_store=artifact_store,
                uow_factory=uow_factory,
                default_timeout_seconds=900,
            )
            result = parse.execute(
                ParseDocumentCommand(document_id=registered.document_id)
            )
            self.assertEqual(result.status, "succeeded", result.error)

            ir_path = (
                settings.disclosure_data_root / "data" / result.normalized_ir_relpath
            )
            ir = json.loads(ir_path.read_text(encoding="utf-8"))
            self.assertEqual(ir["contract_version"], "normalized_ir.v3")
            self.assertGreater(len(ir["elements"]), 0)
            allowed = {
                "text",
                "heading",
                "table",
                "image",
                "equation",
                "page_furniture",
                "unknown",
            }
            self.assertTrue(
                all(element["kind"] in allowed for element in ir["elements"])
            )
            silent_empty = [
                element
                for element in ir["elements"]
                if element["kind"] == "table"
                and not element.get("table_parse_failed")
                and not element["table"].get("rows")
                and not element["table"].get("headers")
                and element.get("table_html", "").strip()
            ]
            self.assertEqual(silent_empty, [])

            with self.engine.connect() as conn:
                status = conn.execute(
                    self.text(
                        "SELECT status FROM disclosure_core.document "
                        "WHERE document_id = :id"
                    ),
                    {"id": registered.document_id},
                ).scalar_one()
                self.assertEqual(status, "parsed")
                run_row = (
                    conn.execute(
                        self.text(
                            "SELECT parser_name, parser_method, parser_language, "
                            "unit_build_status FROM disclosure_core.processing_run "
                            "WHERE processing_run_id = :id"
                        ),
                        {"id": result.processing_run_id},
                    )
                    .mappings()
                    .one()
                )
                self.assertEqual(run_row["parser_name"], "MinerU")
                self.assertEqual(run_row["parser_method"], "auto")
                self.assertEqual(run_row["parser_language"], "ch")
                self.assertEqual(run_row["unit_build_status"], "not_started")


if __name__ == "__main__":
    unittest.main()
