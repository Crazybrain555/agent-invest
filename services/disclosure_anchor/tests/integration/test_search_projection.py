"""06R search projection integration: migration round trip + full rebuild.

Runs against a dedicated scratch database. A full rebuild recomputes every
active-run unit and prunes orphan projection rows, so it must never touch the
shared DB's real units (test_worker_integration scratch-DB pattern).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres.bootstrap import (
    ensure_schemas_and_base_grants,
)
from disclosure_anchor.adapters.db.postgres.schema import OWNER_ROLE
from disclosure_anchor.adapters.db.postgres.models import (
    Company,
    Document,
    DocumentUnit,
    ProcessingRun,
    Security,
)
from disclosure_anchor.adapters.retrieval import tokenizer
from disclosure_anchor.application.use_cases.build_search_projection import (
    BuildSearchProjection,
    BuildSearchProjectionCommand,
)
from tests.integration._support import _database_url

_SERVICE_ROOT = Path(__file__).resolve().parents[2]


class SearchProjectionIntegrationTests(unittest.TestCase):
    temp_db: str = ""
    temp_url: str = ""
    base_url: str = ""
    subprocess_env: dict[str, str] = {}
    class_engine: sqlalchemy.engine.Engine | None = None

    @classmethod
    def setUpClass(cls) -> None:
        base = _database_url()
        if base is None:
            raise unittest.SkipTest("no database configured")
        cls.base_url = base
        cls.temp_db = f"invest_engine_sptest_{os.getpid()}"
        admin = sqlalchemy.create_engine(base, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                conn.execute(
                    text(f'DROP DATABASE IF EXISTS "{cls.temp_db}" WITH (FORCE)')
                )
                # Own the scratch DB with disclosure_owner, mirroring
                # bootstrap.ensure_database. Migrations SET ROLE disclosure_owner
                # (env.py), and 0025's CREATE EXTENSION pg_trgm needs the owner
                # role to hold CREATE on the database.
                conn.execute(
                    text(f'CREATE DATABASE "{cls.temp_db}" OWNER "{OWNER_ROLE}"')
                )
        except Exception as exc:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(f"cannot create scratch database: {exc}")
        finally:
            admin.dispose()
        cls.temp_url = (
            sqlalchemy.engine.make_url(base)
            .set(database=cls.temp_db)
            .render_as_string(hide_password=False)
        )
        schema_engine = sqlalchemy.create_engine(
            cls.temp_url, isolation_level="AUTOCOMMIT"
        )
        try:
            ensure_schemas_and_base_grants(schema_engine)
        finally:
            schema_engine.dispose()
        roots = tempfile.mkdtemp(prefix="sp_mig_roots_")
        cls.subprocess_env = {
            **os.environ,
            "DISCLOSURE_MIGRATION_DATABASE_URL": cls.temp_url,
            "DATABASE_URL": cls.temp_url,
            "DISCLOSURE_DATA_ROOT": f"{roots}/services/disclosure_anchor",
            "DISCLOSURE_SHARED_ROOT": f"{roots}/shared",
            "DISCLOSURE_RUNTIME_ROOT": f"{roots}/services/disclosure_anchor/runtime",
            "MINERU_MODEL_CACHE": f"{roots}/shared/model_cache/mineru",
            "HF_HOME": f"{roots}/shared/model_cache/huggingface",
            "MODELSCOPE_CACHE": f"{roots}/shared/model_cache/modelscope",
            "PYTHONPATH": "src",
        }
        upgrade = cls._alembic("upgrade", "head")
        if upgrade.returncode != 0:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(
                f"scratch migration to head failed: {upgrade.stderr[-500:]}"
            )
        cls.class_engine = sqlalchemy.create_engine(cls.temp_url)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.class_engine is not None:
            cls.class_engine.dispose()
        if cls.temp_db and cls.base_url:
            admin = sqlalchemy.create_engine(cls.base_url, isolation_level="AUTOCOMMIT")
            with admin.connect() as conn:
                conn.execute(
                    text(f'DROP DATABASE IF EXISTS "{cls.temp_db}" WITH (FORCE)')
                )
            admin.dispose()

    @classmethod
    def _alembic(cls, *args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(_SERVICE_ROOT),
            env=cls.subprocess_env,
            capture_output=True,
            text=True,
        )

    def setUp(self) -> None:
        assert self.class_engine is not None
        self.engine = self.class_engine

    # -- migration round trip ----------------------------------------------
    def test_migration_0025_upgrade_downgrade_upgrade(self) -> None:
        self.assertTrue(self._table_exists("unit_search_projection"))
        self.assertTrue(self._view_exists("unit_search_projection_v1"))

        down = self._alembic("downgrade", "0024_reader_vocabulary_grants")
        self.assertEqual(down.returncode, 0, down.stderr[-500:])
        self.assertFalse(self._table_exists("unit_search_projection"))
        self.assertFalse(self._view_exists("unit_search_projection_v1"))

        up = self._alembic("upgrade", "head")
        self.assertEqual(up.returncode, 0, up.stderr[-500:])
        self.assertTrue(self._table_exists("unit_search_projection"))
        self.assertTrue(self._view_exists("unit_search_projection_v1"))

    # -- full rebuild: count + ts_rank ordering + trgm substring -----------
    def test_full_rebuild_counts_ranks_title_over_body_and_trgm(self) -> None:
        suffix = os.urandom(4).hex()
        ids = self._seed_two_units(suffix)
        try:
            result = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=True)
            )
            active = self._active_unit_count()
            self.assertEqual(active, 2)
            self.assertEqual(result.projected, active)
            self.assertEqual(self._projection_count(), active)

            # ts_rank: '应收账款' sits in unit A's title (weight A) and unit B's
            # body (weight C); the title hit must rank strictly higher.
            ranked = self._ranked_asset_ids("应收账款")
            self.assertIn(ids["title_hit"], ranked)
            self.assertIn(ids["body_hit"], ranked)
            self.assertLess(
                ranked.index(ids["title_hit"]), ranked.index(ids["body_hit"])
            )

            # pg_trgm substring channel over the raw heading_path_text.
            self.assertEqual(
                self._trgm_hits(f"账龄{suffix}"), [ids["title_hit"]]
            )
        finally:
            self._cleanup(ids)

    def test_full_rebuild_prunes_orphans(self) -> None:
        suffix = os.urandom(4).hex()
        ids = self._seed_two_units(suffix)
        try:
            BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=True)
            )
            self.assertEqual(self._projection_count(), 2)
            # Deactivate the run: its units are no longer active, so a full
            # rebuild must delete their projection rows.
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.processing_run "
                        "SET is_active = false WHERE processing_run_id = :rid"
                    ),
                    {"rid": ids["run"]},
                )
            result = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=True)
            )
            self.assertEqual(result.deleted, 2)
            self.assertEqual(self._projection_count(), 0)
        finally:
            self._cleanup(ids)

    def test_delta_drains_unbounded_and_prunes_orphans(self) -> None:
        # Delta is the worker's per-round path: it must drain every missing
        # unit without a cap (a borrowed document-scale constant once starved
        # it to 48% live coverage) and prune deactivated runs' rows without
        # waiting for a full rebuild, because the public search view reads
        # the projection bare (no is_active join).
        suffix = os.urandom(4).hex()
        ids = self._seed_two_units(suffix)
        try:
            # _BATCH_SIZE=1 forces the keyset loop through multiple batches
            # (select -> upsert -> commit -> cursor advance -> terminal empty
            # select), so the unbounded drain path is exercised end to end
            # rather than fitting a single batch.
            with mock.patch.object(BuildSearchProjection, "_BATCH_SIZE", 1):
                result = BuildSearchProjection(engine=self.engine).execute(
                    BuildSearchProjectionCommand(full=False)
                )
            self.assertEqual(result.projected, 2)
            self.assertEqual(result.skipped, 0)
            self.assertEqual(self._projection_count(), 2)

            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.processing_run "
                        "SET is_active = false WHERE processing_run_id = :rid"
                    ),
                    {"rid": ids["run"]},
                )
            result = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            self.assertEqual(result.projected, 0)
            self.assertEqual(result.deleted, 2)
            self.assertEqual(self._projection_count(), 0)
        finally:
            self._cleanup(ids)

    def test_delta_restamps_stale_rules_version(self) -> None:
        # Pass 2 of the delta: rows stamped under an older retrieval rules
        # version are re-tokenized and re-stamped current, without a full
        # rebuild. This is the "edit rules -> delta refreshes" contract.
        suffix = os.urandom(4).hex()
        ids = self._seed_two_units(suffix)
        try:
            BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.unit_search_projection "
                        "SET retrieval_rules_version = 'rp-0000.00-0' "
                        "WHERE asset_id IN (:a, :b)"
                    ),
                    {"a": ids["title_hit"], "b": ids["body_hit"]},
                )
            result = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            self.assertEqual(result.projected, 2)
            self.assertEqual(result.deleted, 0)
            with self.engine.connect() as conn:
                versions = list(
                    conn.execute(
                        text(
                            "SELECT DISTINCT retrieval_rules_version "
                            "FROM disclosure_core.unit_search_projection "
                            "WHERE asset_id IN (:a, :b)"
                        ),
                        {"a": ids["title_hit"], "b": ids["body_hit"]},
                    ).scalars()
                )
            self.assertEqual(versions, [tokenizer.RETRIEVAL_RULES_VERSION])
        finally:
            self._cleanup(ids)

    def test_inactive_stale_rows_are_pruned_not_restamped(self) -> None:
        # Review finding 2026-07-24: a rules bump plus a supersede in the
        # same round leaves rows that are BOTH stale and inactive. They must
        # leave via the orphan prune (which runs first), never be
        # re-tokenized and re-stamped current by the stale pass.
        suffix = os.urandom(4).hex()
        ids_map = self._seed_two_units(suffix)
        try:
            BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.processing_run "
                        "SET is_active = false WHERE processing_run_id = :rid"
                    ),
                    {"rid": ids_map["run"]},
                )
                conn.execute(
                    text(
                        "UPDATE disclosure_core.unit_search_projection "
                        "SET retrieval_rules_version = 'rp-0000.00-0' "
                        "WHERE asset_id IN (:a, :b)"
                    ),
                    {"a": ids_map["title_hit"], "b": ids_map["body_hit"]},
                )
            result = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            self.assertEqual(result.projected, 0)
            self.assertEqual(result.deleted, 2)
            self.assertEqual(self._projection_count(), 0)
        finally:
            self._cleanup(ids_map)

    def test_delta_prune_gate_skips_scan_when_no_deactivations(self) -> None:
        # prune=False (the worker's quiet-round signal) must skip the
        # corpus-sized orphan scan; the count gate still forces it when the
        # projection provably exceeds the active set.
        suffix = os.urandom(4).hex()
        ids_map = self._seed_two_units(suffix)
        try:
            BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.processing_run "
                        "SET is_active = false WHERE processing_run_id = :rid"
                    ),
                    {"rid": ids_map["run"]},
                )
            # prune=False, projection(2) > active(0): count gate overrides
            # and the orphans still go.
            result = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False, prune=False)
            )
            self.assertEqual(result.deleted, 2)
            self.assertEqual(self._projection_count(), 0)
        finally:
            self._cleanup(ids_map)

    # -- seeding / queries / cleanup ---------------------------------------
    def _seed_two_units(self, suffix: str) -> dict[str, str]:
        ids = {
            "company": f"co_sp_{suffix}",
            "security": f"sec_sp_{suffix}",
            "document": f"doc_sp_{suffix}",
            "run": f"run_sp_{suffix}",
            "title_hit": f"ua_sp_title_{suffix}",
            "body_hit": f"ua_sp_body_{suffix}",
        }
        with Session(self.engine) as session:
            # Flush each parent before its children so the FK targets exist
            # (these models carry no ORM relationships to auto-order inserts).
            session.add(
                Company(company_id=ids["company"], legal_name=f"投影测试公司{suffix}")
            )
            session.flush()
            session.add(
                Security(
                    security_id=ids["security"],
                    company_id=ids["company"],
                    security_code="600000",
                    exchange="SSE",
                )
            )
            session.flush()
            session.add(
                Document(
                    document_id=ids["document"],
                    company_id=ids["company"],
                    security_id=ids["security"],
                    provider="cninfo",
                    provider_document_id=f"T{suffix}",
                    status="published",
                    provider_metadata={},
                )
            )
            session.flush()
            session.add(
                ProcessingRun(
                    processing_run_id=ids["run"],
                    document_id=ids["document"],
                    run_kind="rebuild_units",
                    status="succeeded",
                    is_active=True,
                    unit_build_status="succeeded",
                )
            )
            session.flush()
            session.add(
                DocumentUnit(
                    asset_id=ids["title_hit"],
                    document_id=ids["document"],
                    processing_run_id=ids["run"],
                    payload_kind="text",
                    heading_path=[f"财务附注{suffix}", f"账龄{suffix}分析"],
                    title="应收账款",
                    order_index=1,
                    payload={"text": "期末余额说明"},
                    content_hash=f"h1_{suffix}",
                    semantic_keys=["receivable_aging"],
                )
            )
            session.add(
                DocumentUnit(
                    asset_id=ids["body_hit"],
                    document_id=ids["document"],
                    processing_run_id=ids["run"],
                    payload_kind="text",
                    heading_path=[f"减值损失{suffix}"],
                    title="坏账准备",
                    order_index=2,
                    payload={"text": "应收账款"},
                    content_hash=f"h2_{suffix}",
                    semantic_keys=["credit_impairment_loss"],
                )
            )
            session.commit()
        return ids

    def _cleanup(self, ids: dict[str, str]) -> None:
        with self.engine.begin() as conn:
            # unit_search_projection cascades on document_unit delete (FK ON
            # DELETE CASCADE), but delete it explicitly first for clarity.
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.unit_search_projection "
                    "WHERE asset_id IN (:a, :b)"
                ),
                {"a": ids["title_hit"], "b": ids["body_hit"]},
            )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.document_unit "
                    "WHERE document_id = :did"
                ),
                {"did": ids["document"]},
            )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.processing_run "
                    "WHERE document_id = :did"
                ),
                {"did": ids["document"]},
            )
            conn.execute(
                text("DELETE FROM disclosure_core.document WHERE document_id = :did"),
                {"did": ids["document"]},
            )
            conn.execute(
                text("DELETE FROM disclosure_core.security WHERE security_id = :sid"),
                {"sid": ids["security"]},
            )
            conn.execute(
                text("DELETE FROM disclosure_core.company WHERE company_id = :cid"),
                {"cid": ids["company"]},
            )

    def _ranked_asset_ids(self, term: str) -> list[str]:
        query = tokenizer.tokenize(term)
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT asset_id FROM disclosure_core.unit_search_projection "
                    "WHERE search_tsv @@ plainto_tsquery('simple', :q) "
                    "ORDER BY ts_rank(search_tsv, plainto_tsquery('simple', :q)) "
                    "DESC, asset_id"
                ),
                {"q": query},
            ).scalars()
            return list(rows)

    def _trgm_hits(self, substring: str) -> list[str]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT asset_id FROM disclosure_core.unit_search_projection "
                    "WHERE heading_path_text LIKE :pat ORDER BY asset_id"
                ),
                {"pat": f"%{substring}%"},
            ).scalars()
            return list(rows)

    def _active_unit_count(self) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        "SELECT count(*) FROM disclosure_core.document_unit du "
                        "JOIN disclosure_core.processing_run pr "
                        "ON pr.processing_run_id = du.processing_run_id "
                        "WHERE pr.is_active"
                    )
                ).scalar_one()
            )

    def _projection_count(self) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT count(*) FROM disclosure_core.unit_search_projection")
                ).scalar_one()
            )

    def _table_exists(self, name: str) -> bool:
        with self.engine.connect() as conn:
            return bool(
                conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'disclosure_core' AND table_name = :n"
                    ),
                    {"n": name},
                ).scalar()
            )

    def _view_exists(self, name: str) -> bool:
        with self.engine.connect() as conn:
            return bool(
                conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.views "
                        "WHERE table_schema = 'disclosure_public' AND table_name = :n"
                    ),
                    {"n": name},
                ).scalar()
            )


if __name__ == "__main__":
    unittest.main()
