"""Worker run_once integration on a live DB with fake source/parser (08 §5)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.bootstrap import (
    ensure_schemas_and_base_grants,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.application.dto.worker_report import WorkerLimits
from disclosure_anchor.application.ports.disclosure_source import AnnouncementRef
from disclosure_anchor.application.ports.parser import (
    ParserIdentity,
    ParserOptions,
    ParserResult,
)
from disclosure_anchor.application.worker.locks import DOC_NS, WORKER_NS
from disclosure_anchor.application.worker.worker import (
    WorkerConfig,
    WorkerDeps,
    run_once,
)
from disclosure_anchor.settings import SENTINEL_NAME, Settings
from tests.integration._support import _database_url

import sqlalchemy

FIXTURE_IR = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "phase00"
    / "short_announcement"
    / "normalized_ir.v2.json"
)
PDF_BYTES = b"%PDF-1.4 fake worker integration pdf\n%%EOF\n"


class FakeWorkerSource:
    """One announcement, deterministic bytes, no network."""

    def __init__(self, suffix: str) -> None:
        self.suffix = suffix
        self.closed = False

    def search_announcements(self, security, window, categories=None):  # type: ignore[no-untyped-def]
        return [
            AnnouncementRef(
                provider="cninfo",
                provider_document_id=f"wk{self.suffix}",
                title=f"工作器测试公告{self.suffix}",
                download_url=f"http://static.cninfo.com.cn/wk{self.suffix}.PDF",
                raw_category="",
                announcement_date=date(2026, 7, 1),
                security_code=f"W{self.suffix[:5]}",
                security_name="工作器测试",
                file_size=1,
                index_updated_at=None,
                filing_type="other",
                report_period=None,
            )
        ]

    def profile_for_security(self, security_code):  # type: ignore[no-untyped-def]
        return None

    def download_pdf(self, ref):  # type: ignore[no-untyped-def]
        return PDF_BYTES

    def close(self) -> None:
        self.closed = True


class FakeParser:
    """Returns the short_announcement fixture IR rebadged for the document."""

    def identity(self) -> ParserIdentity:
        return ParserIdentity(
            name="FakeParser", version="1.0", backend="fake", method="auto", language="ch"
        )

    def parse(
        self,
        *,
        input_pdf: Path,
        output_dir: Path,
        options: ParserOptions,
        document_metadata: dict,
    ) -> ParserResult:
        normalized = json.loads(FIXTURE_IR.read_text(encoding="utf-8"))
        normalized["document_id"] = document_metadata.get("document_id", "unknown")
        output_dir.mkdir(parents=True, exist_ok=True)
        content_list = output_dir / "fake_content_list.json"
        content_list.write_text("[]", encoding="utf-8")
        return ParserResult(
            parser_name="FakeParser",
            parser_version="1.0",
            parser_backend="fake",
            parser_method="auto",
            parser_language="ch",
            artifact_root=output_dir,
            content_list_path=content_list,
            markdown_path=None,
            normalized_ir=normalized,
        )


def _settings(root: Path) -> Settings:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    (data_root / "runtime").mkdir(parents=True, exist_ok=True)
    (shared_root / "model_cache").mkdir(parents=True, exist_ok=True)
    (root / SENTINEL_NAME).write_text("agent-system\n", encoding="utf-8")
    return Settings(
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
        mineru_model_cache=shared_root / "model_cache" / "mineru",
        hf_home=shared_root / "model_cache" / "huggingface",
        modelscope_cache=shared_root / "model_cache" / "modelscope",
    )


def _config() -> WorkerConfig:
    return WorkerConfig(
        max_parse_retries=3,
        max_build_retries=3,
        stale_run_threshold_seconds=3600,
        sync_interval_seconds=86400,
        cninfo_overlap_days=7,
        cninfo_max_retries=3,
        cninfo_oversized_kb=10240,
    )


class WorkerRunOnceIntegrationTests(unittest.TestCase):
    """Runs against a dedicated scratch database.

    The worker queues are global by design, so running run_once against the
    shared live DB would drain real pending documents through test fakes
    (observed: real docs got raw_missing failed runs from tmp-root deps).
    """

    temp_db: str = ""
    temp_url: str = ""
    base_url: str = ""
    class_engine: sqlalchemy.engine.Engine | None = None

    @classmethod
    def setUpClass(cls) -> None:
        base = _database_url()
        if base is None:
            raise unittest.SkipTest("no database configured")
        cls.base_url = base
        cls.temp_db = f"invest_engine_wktest_{os.getpid()}"
        admin = sqlalchemy.create_engine(base, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{cls.temp_db}" WITH (FORCE)'))
                conn.execute(text(f'CREATE DATABASE "{cls.temp_db}"'))
        except Exception as exc:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(f"cannot create scratch database: {exc}")
        finally:
            admin.dispose()
        cls.temp_url = sqlalchemy.engine.make_url(base).set(database=cls.temp_db).render_as_string(
            hide_password=False
        )
        schema_engine = sqlalchemy.create_engine(
            cls.temp_url, isolation_level="AUTOCOMMIT"
        )
        try:
            ensure_schemas_and_base_grants(schema_engine)
        finally:
            schema_engine.dispose()
        roots = tempfile.mkdtemp(prefix="wk_mig_roots_")
        env = {
            **os.environ,
            "DISCLOSURE_MIGRATION_DATABASE_URL": cls.temp_url,
            "DATABASE_URL": cls.temp_url,
            "DISCLOSURE_DATA_ROOT": f"{roots}/services/disclosure_anchor",
            "DISCLOSURE_SHARED_ROOT": f"{roots}/shared",
            "DISCLOSURE_RUNTIME_ROOT": f"{roots}/services/disclosure_anchor/runtime",
            "MINERU_MODEL_CACHE": f"{roots}/shared/model_cache/mineru",
            "HF_HOME": f"{roots}/shared/model_cache/huggingface",
            "MODELSCOPE_CACHE": f"{roots}/shared/model_cache/modelscope",
        }
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(Path(__file__).resolve().parents[2]),
            env={**env, "PYTHONPATH": "src"},
            capture_output=True,
            text=True,
        )
        if upgrade.returncode != 0:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(f"scratch migration failed: {upgrade.stderr[-500:]}")
        cls.class_engine = sqlalchemy.create_engine(cls.temp_url)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.class_engine is not None:
            cls.class_engine.dispose()
        if cls.temp_db and cls.base_url:
            admin = sqlalchemy.create_engine(cls.base_url, isolation_level="AUTOCOMMIT")
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{cls.temp_db}" WITH (FORCE)'))
            admin.dispose()

    def setUp(self) -> None:
        assert self.class_engine is not None
        self.engine = self.class_engine
        self.suffix = os.urandom(4).hex()
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name))
        self.source = FakeWorkerSource(self.suffix)

    def tearDown(self) -> None:
        pid = f"wk{self.suffix}"
        with self.engine.begin() as conn:
            doc_ids = [
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT document_id FROM disclosure_core.document "
                        "WHERE provider_document_id = :pid"
                    ),
                    {"pid": pid},
                ).all()
            ]
            if doc_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.document_unit "
                        "WHERE document_id = ANY(:ids)"
                    ),
                    {"ids": doc_ids},
                )
                conn.execute(
                    text(
                        "DELETE FROM disclosure_ops.outbox_event "
                        "WHERE document_id = ANY(:ids)"
                    ),
                    {"ids": doc_ids},
                )
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.processing_run "
                        "WHERE document_id = ANY(:ids)"
                    ),
                    {"ids": doc_ids},
                )
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.document "
                        "WHERE document_id = ANY(:ids)"
                    ),
                    {"ids": doc_ids},
                )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.source_access "
                    "WHERE provider='cninfo' AND ("
                    "  query_params->>'provider_document_id' = :pid"
                    "  OR query_params->>'scode' LIKE :sec"
                    "  OR result_snapshot->'candidates'->0->>'provider_document_id' = :pid)"
                ),
                {"pid": pid, "sec": f"W{self.suffix[:5]}%"},
            )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.source_checkpoint "
                    "WHERE scope_key LIKE '%' || :co || '%'"
                ),
                {"co": self.suffix},
            )
            for table, column in (
                ("tracked_company", "company_id"),
                ("company_identifier", "company_id"),
            ):
                conn.execute(
                    text(
                        f"DELETE FROM disclosure_core.{table} WHERE {column} IN ("
                        "SELECT company_id FROM disclosure_core.company "
                        "WHERE legal_name LIKE :name)"
                    ),
                    {"name": f"%{self.suffix}%"},
                )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.security WHERE company_id IN ("
                    "SELECT company_id FROM disclosure_core.company "
                    "WHERE legal_name LIKE :name)"
                ),
                {"name": f"%{self.suffix}%"},
            )
            conn.execute(
                text("DELETE FROM disclosure_core.company WHERE legal_name LIKE :name"),
                {"name": f"%{self.suffix}%"},
            )
        self.tmp.cleanup()

    def _deps(self) -> WorkerDeps:
        paths = FileStorePathBuilder(self.settings)
        return WorkerDeps(
            engine=self.engine,
            uow_factory=lambda: SqlAlchemyUnitOfWork(engine=self.engine),
            path_builder=paths,
            raw_store=RawDocumentStore(paths),
            artifact_store=ArtifactStore(paths),
            source_factory=lambda: self.source,
            profile_loader_factory=lambda source: source.profile_for_security,
            parser_factory=lambda: FakeParser(),
            parse_timeout_seconds=60,
            config=_config(),
            clock=lambda: datetime.now(timezone.utc),
        )

    def _seed_tracked_company(self) -> str:
        company_id = f"co_wk{self.suffix}"
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.company (company_id, legal_name) "
                    "VALUES (:id, :name)"
                ),
                {"id": company_id, "name": f"工作器集成测试公司{self.suffix}"},
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.security "
                    "(security_id, company_id, security_code, exchange, status) "
                    "VALUES (:sid, :cid, :code, 'LOCAL', 'active')"
                ),
                {
                    "sid": f"sec_wk{self.suffix}",
                    "cid": company_id,
                    "code": f"W{self.suffix[:5]}",
                },
            )
            conn.execute(
                text(
                    "INSERT INTO disclosure_core.tracked_company "
                    "(tracked_company_id, company_id, security_id, status) "
                    "VALUES (:tid, :cid, :sid, 'active')"
                ),
                {
                    "tid": f"tc_wk{self.suffix}",
                    "cid": company_id,
                    "sid": f"sec_wk{self.suffix}",
                },
            )
        return company_id

    def test_run_once_full_chain_with_reconciliation(self) -> None:
        self._seed_tracked_company()
        with self.engine.connect() as conn:
            before = self._counters(conn)

        report = run_once(
            WorkerLimits(sync=5, download=5, parse=5, build=5, publish=5),
            self._deps(),
        )

        self.assertEqual(
            [(f.stage, f.item_ref, f.error_code) for f in report.failures], []
        )
        self.assertEqual(report.synced_companies, 1)
        self.assertEqual(report.candidates_discovered, 1)
        self.assertEqual(report.downloaded, 1)
        self.assertEqual(report.parsed, 1)
        self.assertEqual(report.built, 1)
        self.assertEqual(report.published, 1)
        self.assertEqual(report.failed, 0)
        self.assertTrue(self.source.closed)

        with self.engine.connect() as conn:
            after = self._counters(conn)
            active = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.processing_run r "
                    "JOIN disclosure_core.document d ON d.document_id=r.document_id "
                    "WHERE d.provider_document_id = :pid AND r.is_active"
                ),
                {"pid": f"wk{self.suffix}"},
            ).scalar_one()

        # 对账断言组 (08 §4): report counters equal DB deltas.
        self.assertEqual(report.parsed, after["succeeded"] - before["succeeded"])
        self.assertEqual(report.failed, after["failed"] - before["failed"])
        self.assertEqual(report.published, after["active"] - before["active"])
        self.assertEqual(report.downloaded, after["documents"] - before["documents"])
        self.assertEqual(active, 1)

    def _counters(self, conn) -> dict[str, int]:  # type: ignore[no-untyped-def]
        return {
            "succeeded": conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.processing_run "
                    "WHERE status='succeeded'"
                )
            ).scalar_one(),
            "failed": conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.processing_run "
                    "WHERE status='failed'"
                )
            ).scalar_one(),
            "active": conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_core.processing_run WHERE is_active"
                )
            ).scalar_one(),
            "documents": conn.execute(
                text("SELECT count(*) FROM disclosure_core.document")
            ).scalar_one(),
        }

    def test_singleton_lock_classid_and_document_lock_namespace(self) -> None:
        with self.engine.connect() as holder:
            acquired = holder.execute(
                text("SELECT pg_try_advisory_lock(:ns, 0)"), {"ns": WORKER_NS}
            ).scalar_one()
            self.assertTrue(acquired)
            try:
                with self.engine.connect() as second:
                    reacquired = second.execute(
                        text("SELECT pg_try_advisory_lock(:ns, 0)"), {"ns": WORKER_NS}
                    ).scalar_one()
                    self.assertFalse(reacquired)
                    classids = [
                        row[0]
                        for row in second.execute(
                            text(
                                "SELECT classid FROM pg_locks "
                                "WHERE locktype='advisory' AND classid IN (:w, :d)"
                            ),
                            {"w": WORKER_NS, "d": DOC_NS},
                        ).all()
                    ]
                    self.assertIn(WORKER_NS, classids)
            finally:
                holder.execute(
                    text("SELECT pg_advisory_unlock(:ns, 0)"), {"ns": WORKER_NS}
                )

    def test_second_worker_skips_with_exit_zero(self) -> None:
        env = {
            **os.environ,
            "PYTHONPATH": "src",
            "DATABASE_URL": self.temp_url,
            "DISCLOSURE_MIGRATION_DATABASE_URL": self.temp_url,
            "DISCLOSURE_DATA_ROOT": str(self.settings.disclosure_data_root),
            "DISCLOSURE_SHARED_ROOT": str(self.settings.disclosure_shared_root),
            "DISCLOSURE_RUNTIME_ROOT": str(self.settings.disclosure_runtime_root),
            "MINERU_MODEL_CACHE": str(self.settings.mineru_model_cache),
            "HF_HOME": str(self.settings.hf_home),
            "MODELSCOPE_CACHE": str(self.settings.modelscope_cache),
            "CNINFO_ACCESS_TOKEN": "worker-skip-test-token",
            "WORKER_BATCH_SYNC": "0",
            "WORKER_BATCH_DOWNLOAD": "0",
            "WORKER_BATCH_PARSE": "0",
            "WORKER_BATCH_BUILD": "0",
            "WORKER_BATCH_PUBLISH": "0",
        }
        with self.engine.connect() as holder:
            self.assertTrue(
                holder.execute(
                    text("SELECT pg_try_advisory_lock(:ns, 0)"), {"ns": WORKER_NS}
                ).scalar_one()
            )
            try:
                second = subprocess.run(
                    [sys.executable, "-m", "disclosure_anchor.cli.worker", "once"],
                    cwd=str(Path(__file__).resolve().parents[2]),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            finally:
                holder.execute(
                    text("SELECT pg_advisory_unlock(:ns, 0)"), {"ns": WORKER_NS}
                )
        self.assertEqual(second.returncode, 0, second.stderr[-500:])
        self.assertIn("[skip] another worker holds the singleton lock", second.stdout)
        report_dir = self.settings.disclosure_runtime_root / "reports" / "worker"
        self.assertFalse(
            report_dir.exists() and any(report_dir.iterdir()),
            "skipped worker must not write a report section",
        )

    def test_bad_pdf_is_isolated_and_round_continues(self) -> None:
        self._seed_tracked_company()

        bad_suffix = self.suffix

        class TwoDocSource(FakeWorkerSource):
            def search_announcements(self, security, window, categories=None):  # type: ignore[no-untyped-def]
                good = super().search_announcements(security, window, categories)[0]
                from dataclasses import replace

                bad = replace(
                    good,
                    provider_document_id=f"bad{bad_suffix}",
                    title=f"坏PDF公告{bad_suffix}",
                    download_url=f"http://static.cninfo.com.cn/bad{bad_suffix}.PDF",
                )
                return [bad, good]

            def download_pdf(self, ref):  # type: ignore[no-untyped-def]
                if ref.provider_document_id.startswith("bad"):
                    return b"this is not a pdf at all"
                return PDF_BYTES

        self.source = TwoDocSource(self.suffix)
        report = run_once(
            WorkerLimits(sync=5, download=5, parse=5, build=5, publish=5),
            self._deps(),
        )

        self.assertEqual(report.downloaded, 1)
        self.assertEqual(report.published, 1)
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.failures[0].stage, "download")
        self.assertEqual(report.failures[0].error_code, "invalid_raw_document")
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.source_access "
                    "WHERE query_params->>'provider_document_id' = :pid"
                ),
                {"pid": f"bad{self.suffix}"},
            )

    def test_all_post_0008_migrations_roundtrip_on_scratch_database(self) -> None:
        env = {
            **os.environ,
            "PYTHONPATH": "src",
            "DATABASE_URL": self.temp_url,
            "DISCLOSURE_MIGRATION_DATABASE_URL": self.temp_url,
            "DISCLOSURE_DATA_ROOT": str(self.settings.disclosure_data_root),
            "DISCLOSURE_SHARED_ROOT": str(self.settings.disclosure_shared_root),
            "DISCLOSURE_RUNTIME_ROOT": str(self.settings.disclosure_runtime_root),
            "MINERU_MODEL_CACHE": str(self.settings.mineru_model_cache),
            "HF_HOME": str(self.settings.hf_home),
            "MODELSCOPE_CACHE": str(self.settings.modelscope_cache),
        }
        cwd = str(Path(__file__).resolve().parents[2])

        def view_names() -> set[str]:
            with self.engine.connect() as conn:
                return {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.views "
                            "WHERE table_schema='disclosure_ops'"
                        )
                    ).all()
                }

        self.assertLessEqual({"sync_due_v1", "pending_download_v1"}, view_names())
        with self.engine.connect() as conn:
            self.assertTrue(
                conn.execute(
                    text(
                        "SELECT 1 FROM pg_constraint "
                        "WHERE conname = 'ck_security_exchange_canonical'"
                    )
                ).scalar()
            )
        down = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "0008_unit_builder_provenance"],
            cwd=cwd, env=env, capture_output=True, text=True,
        )
        self.assertEqual(down.returncode, 0, down.stderr[-500:])
        self.assertFalse({"sync_due_v1", "pending_download_v1"} & view_names())
        with self.engine.connect() as conn:
            self.assertFalse(
                conn.execute(
                    text(
                        "SELECT 1 FROM pg_constraint "
                        "WHERE conname = 'ck_security_exchange_canonical'"
                    )
                ).scalar()
            )
        up = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=cwd, env=env, capture_output=True, text=True,
        )
        self.assertEqual(up.returncode, 0, up.stderr[-500:])
        self.assertLessEqual({"sync_due_v1", "pending_download_v1"}, view_names())
        with self.engine.connect() as conn:
            self.assertTrue(
                conn.execute(
                    text(
                        "SELECT 1 FROM pg_constraint "
                        "WHERE conname = 'ck_security_exchange_canonical'"
                    )
                ).scalar()
            )

    def test_kill_dash_nine_releases_advisory_locks(self) -> None:
        url = self.temp_url
        child_code = (
            "import time, sys\n"
            "import sqlalchemy\n"
            "from sqlalchemy import text\n"
            f"engine = sqlalchemy.create_engine({url!r}, poolclass=sqlalchemy.pool.NullPool)\n"
            "conn = engine.connect()\n"
            f"conn.execute(text('SELECT pg_advisory_lock({WORKER_NS}, 0)'))\n"
            f"conn.execute(text('SELECT pg_advisory_lock({DOC_NS}, 42)'))\n"
            "print('locked', flush=True)\n"
            "time.sleep(60)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONPATH": "src"},
        )
        try:
            assert child.stdout is not None
            line = child.stdout.readline().strip()
            self.assertEqual(line, "locked")
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
            deadline = time.monotonic() + 10
            remaining = None
            while time.monotonic() < deadline:
                with self.engine.connect() as conn:
                    remaining = conn.execute(
                        text(
                            # pg_locks is cluster-wide; scope to this scratch DB
                            # or a worker on another database trips the assert.
                            "SELECT count(*) FROM pg_locks "
                            "WHERE locktype='advisory' AND classid IN (:w, :d) "
                            "AND database = (SELECT oid FROM pg_database "
                            "                WHERE datname = current_database())"
                        ),
                        {"w": WORKER_NS, "d": DOC_NS},
                    ).scalar_one()
                if remaining == 0:
                    break
                time.sleep(0.5)
            self.assertEqual(remaining, 0, "advisory locks must die with the process")
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
            if child.stdout is not None:
                child.stdout.close()


if __name__ == "__main__":
    unittest.main()
