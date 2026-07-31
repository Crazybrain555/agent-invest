"""Opt-in eyeball run: fresh-parse chosen real filings and dump their units.

Not discovered by default (no ``test_`` prefix). Drives the production
register→parse→build composition against the runner's disposable database
with the real MinerU CLI, then writes每份文档的 unit 序列到 JSON 供人工
切分验收。Inputs via env:

- ``EYEBALL_DOCS``: path to a JSON list of documents
  ``{pdf, company_legal_name, security_code, exchange, filing_type,
  title, announcement_date, provider_document_id}``
- ``EYEBALL_OUT``: output directory for ``<pid>.units.json``

Run::

    EYEBALL_DOCS=/path/docs.json EYEBALL_OUT=/path/out \
    PYTHONPATH=src .venv/bin/python -m tests.integration._runner \
        --real-mineru -v tests.integration.eyeball_real_corpus
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests.integration._support import engine_or_skip


class EyeballRealCorpusRun(unittest.TestCase):
    """Fresh-parse each configured filing and dump its published units."""

    def test_eyeball_batch(self) -> None:
        docs_path = os.environ.get("EYEBALL_DOCS")
        out_dir = os.environ.get("EYEBALL_OUT")
        if not docs_path or not out_dir:
            raise unittest.SkipTest("EYEBALL_DOCS/EYEBALL_OUT not set")
        mineru_raw = os.environ.get("DISCLOSURE_MINERU_BIN")
        if not mineru_raw:
            raise unittest.SkipTest("DISCLOSURE_MINERU_BIN not set")

        from sqlalchemy import text

        from disclosure_anchor.adapters.db.postgres.unit_of_work import (
            SqlAlchemyUnitOfWork,
        )
        from disclosure_anchor.adapters.parsers.mineru.mineru_process import (
            MinerUProcess,
        )
        from disclosure_anchor.adapters.parsers.mineru.parser import (
            MinerUDocumentParser,
        )
        from disclosure_anchor.adapters.parsers.mineru.source_evidence_validator import (
            MinerUSourceEvidenceValidator,
        )
        from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
        from disclosure_anchor.adapters.storage.path_builder import (
            FileStorePathBuilder,
        )
        from disclosure_anchor.adapters.storage.raw_document_store import (
            RawDocumentStore,
        )
        from disclosure_anchor.application.ports.parser import ParserOptions
        from disclosure_anchor.application.use_cases.build_units import (
            BuildUnits,
            BuildUnitsCommand,
        )
        from disclosure_anchor.application.use_cases.parse_document import (
            ParseDocument,
            ParseDocumentCommand,
        )
        from disclosure_anchor.application.use_cases.register_local_pdf import (
            RegisterLocalPdf,
            RegisterLocalPdfCommand,
        )
        from disclosure_anchor.domain.value_objects.common import ReportPeriod
        from disclosure_anchor.settings import Settings

        engine = engine_or_skip()
        self.addCleanup(engine.dispose)
        docs = json.loads(Path(docs_path).read_text(encoding="utf-8"))
        output_root = Path(out_dir)
        output_root.mkdir(parents=True, exist_ok=True)

        keep_root = os.environ.get("EYEBALL_WORKDIR")
        with contextlib.ExitStack() as stack:
            if keep_root:
                root = Path(keep_root)
                root.mkdir(parents=True, exist_ok=True)
            else:
                root = Path(
                    stack.enter_context(tempfile.TemporaryDirectory())
                )
            data_root = root / "services" / "disclosure_anchor"
            shared_root = root / "shared"
            settings = Settings(
                disclosure_data_root=data_root,
                disclosure_shared_root=shared_root,
                disclosure_runtime_root=data_root / "runtime",
                mineru_model_cache=Path(
                    os.environ.get(
                        "MINERU_MODEL_CACHE",
                        str(shared_root / "model_cache" / "mineru"),
                    )
                ),
                hf_home=Path(
                    os.environ.get(
                        "HF_HOME",
                        str(shared_root / "model_cache" / "huggingface"),
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
            uow_factory = lambda: SqlAlchemyUnitOfWork(engine=engine)  # noqa: E731
            register = RegisterLocalPdf(
                raw_store=raw_store, uow_factory=uow_factory
            )
            # Mirror the production worker composition exactly: the same
            # backend, server URL and request concurrency the resident
            # worker uses, so acceptance evidence comes from the engine
            # the recut will actually run.
            parse = ParseDocument(
                parser=MinerUDocumentParser(
                    process=MinerUProcess(executable=Path(mineru_raw)),
                    server_url=settings.disclosure_mineru_server_url,
                ),
                path_builder=paths,
                raw_store=raw_store,
                artifact_store=artifact_store,
                uow_factory=uow_factory,
                default_timeout_seconds=5400,
            )
            build = BuildUnits(
                path_builder=paths,
                artifact_store=artifact_store,
                uow_factory=uow_factory,
                source_evidence_validator=MinerUSourceEvidenceValidator(),
            )

            summary: list[dict[str, object]] = []
            failures: list[dict[str, object]] = []

            def run_one(doc: dict[str, str]) -> None:

                registered = register.execute(
                    RegisterLocalPdfCommand(
                        file_path=Path(doc["pdf"]),
                        company_legal_name=doc["company_legal_name"],
                        security_code=doc["security_code"],
                        exchange=doc["exchange"],
                        filing_type=doc["filing_type"],
                        title=doc["title"],
                        announcement_date=date.fromisoformat(
                            doc["announcement_date"]
                        ),
                        report_period=(
                            ReportPeriod.parse(doc["report_period"])
                            if doc.get("report_period")
                            else None
                        ),
                        provider_document_id=doc["provider_document_id"],
                        provider="cninfo",
                    )
                )
                parse_result = parse.execute(
                    ParseDocumentCommand(
                        document_id=registered.document_id,
                        options=ParserOptions(
                            backend=settings.disclosure_mineru_backend,
                            server_url=settings.disclosure_mineru_server_url,
                            http_request_concurrency=(
                                settings.mineru_http_request_concurrency
                            ),
                            runtime_bundle_identity_sha256=(
                                settings.disclosure_mineru_runtime_bundle_identity_sha256
                            ),
                        ),
                    )
                )
                self.assertEqual(
                    parse_result.status, "succeeded", parse_result.error
                )
                build_result = build.execute(
                    BuildUnitsCommand(
                        processing_run_id=parse_result.processing_run_id
                    )
                )
                self.assertEqual(
                    build_result.status, "succeeded", build_result.error
                )
                with engine.connect() as conn:
                    rows = [
                        dict(row)
                        for row in conn.execute(
                            text(
                                "SELECT order_index, payload_kind, title, "
                                "heading_path, semantic_key, semantic_keys, "
                                "quality_status, applicability, payload, artifact_locator "
                                "FROM disclosure_core.document_unit "
                                "WHERE processing_run_id = :run "
                                "ORDER BY order_index"
                            ),
                            {"run": parse_result.processing_run_id},
                        ).mappings()
                    ]
                out_path = (
                    output_root / f"{doc['provider_document_id']}.units.json"
                )
                out_path.write_text(
                    json.dumps(
                        {
                            "document": doc,
                            "document_id": registered.document_id,
                            "processing_run_id": parse_result.processing_run_id,
                            "unit_count": len(rows),
                            "units": rows,
                        },
                        ensure_ascii=False,
                        indent=1,
                        default=str,
                    ),
                    encoding="utf-8",
                )
                summary.append(
                    {
                        "pid": doc["provider_document_id"],
                        "title": doc["title"],
                        "units": len(rows),
                        "out": str(out_path),
                    }
                )

            for doc in docs:
                try:
                    run_one(doc)
                except Exception as exc:  # noqa: BLE001 - batch collector
                    failures.append(
                        {
                            "pid": doc["provider_document_id"],
                            "filing_type": doc["filing_type"],
                            "error": str(exc),
                        }
                    )
            print(json.dumps(summary, ensure_ascii=False))
            if failures:
                print(
                    "[failures] " + json.dumps(failures, ensure_ascii=False)
                )
                self.fail(
                    f"{len(failures)} documents failed; see [failures]"
                )


if __name__ == "__main__":
    unittest.main()
