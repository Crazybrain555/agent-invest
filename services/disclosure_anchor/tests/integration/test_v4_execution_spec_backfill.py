"""Historical cutover and same-UoW spec invariants on managed scratch only."""

from contextlib import contextmanager
from dataclasses import replace
import importlib
from pathlib import Path
import tempfile
import unittest

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres.remote_parse_v4_repository import RemoteParseV4Repository
from disclosure_anchor.adapters.storage.immutable_artifact_store import ImmutableArtifactStore
from disclosure_anchor.adapters.storage.v4_execution_spec_catalog import ImmutableV4ExecutionSpecCatalog
from disclosure_anchor.adapters.storage.v4_legacy_spec_retirement import LegacyV4ExecutionSpecRetirer
from disclosure_anchor.application.ports.remote_parse_v4_repository import RemoteParseV4AuthorityViolation
from disclosure_anchor.application.services.backfill_v4_execution_specs import backfill_v4_execution_specs_once
from disclosure_anchor.application.services.retire_v4_execution_specs import retire_v4_execution_specs_once
from disclosure_anchor.application.ports.v4_execution_spec_catalog import V4ExecutionSpecCatalogReference
from tests.integration._remote_parse_v4_factory import (
    build_v4_authority_fixture, build_v4_supersession_stage_fixture,
    install_acked_cycle, install_v4_supersession_stage,
)
from tests.integration._support import engine_or_skip
from tests.unit.test_v4_execution_spec_catalog import _CatalogPaths, _DataPaths
from tests.unit.test_v4_legacy_spec_retirement import _RetirementPaths
from tests.unit.test_v4_prepared_execution_spec import _spec


class _BackfillUow:
    def __init__(self, connection):
        self.session = Session(bind=connection, join_transaction_mode="create_savepoint")
        self.remote_parse_v4 = RemoteParseV4Repository(self.session)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.session.close()

    def commit(self):
        self.session.commit()


class V4ExecutionSpecBackfillIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = engine_or_skip()
        self.addCleanup(self.engine.dispose)
        prefix = "disclosure_anchor.adapters.db.postgres.migrations.versions."
        self.artifact = importlib.import_module(prefix+"0060_v4_execution_spec_artifact")
        self.validation = importlib.import_module(prefix+"0061_validate_v4_execution_spec_artifact")

    @contextmanager
    def _legacy(self):
        with self.engine.connect() as conn, tempfile.TemporaryDirectory() as root:
            outer = conn.begin()
            try:
                # Disposable test namespace; outer rollback restores both schema and rows.
                conn.exec_driver_sql("TRUNCATE disclosure_ops.remote_parse_attempt CASCADE")
                with Operations.context(MigrationContext.configure(conn)):
                    self.artifact.downgrade()
                final = build_v4_authority_fixture(attempt_id="backfill-a-final")
                source = build_v4_authority_fixture(attempt_id="backfill-b-source")
                stage = build_v4_supersession_stage_fixture(source)
                install_acked_cycle(conn, final)
                install_v4_supersession_stage(conn, stage)
                conn.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")
                conn.exec_driver_sql("SET CONSTRAINTS ALL DEFERRED")
                paths = _CatalogPaths()
                store = ImmutableArtifactStore(_DataPaths(Path(root)))
                for fixture in (final, source, stage):
                    store.create_or_verify(
                        relpath=paths.v4_execution_spec_relpath(spec_sha256=fixture.execution_spec.sha256),
                        payload=fixture.execution_spec.exact_bytes,
                    )
                legacy = ImmutableV4ExecutionSpecCatalog(paths=paths, immutable_store=store)
                with Operations.context(MigrationContext.configure(conn)):
                    self.artifact.upgrade()
                yield conn, legacy, (final, source, stage)
            finally:
                outer.rollback()

    def test_all_history_bounded_backfill_validation_replay_and_immutable_rows(self):
        with self._legacy() as (conn, legacy, fixtures):
            retirement_paths = _RetirementPaths(legacy._store._paths.root)
            files = LegacyV4ExecutionSpecRetirer(retirement_paths)
            references = tuple(V4ExecutionSpecCatalogReference(f.execution_spec.sha256, f.execution_spec.byte_count) for f in fixtures)
            with self.assertRaisesRegex(RemoteParseV4AuthorityViolation, "validated execution-spec"):
                retire_v4_execution_specs_once(
                    uow_factory=lambda: _BackfillUow(conn), files=files,
                    references=references, ownership_guard=lambda: None,
                )
            with conn.begin_nested() as rejected:
                with self.assertRaises(SQLAlchemyError), Operations.context(MigrationContext.configure(conn)):
                    self.validation.upgrade()
                rejected.rollback()
            with _BackfillUow(conn) as uow:
                with self.assertRaisesRegex(RemoteParseV4AuthorityViolation, "cutover incomplete"):
                    uow.remote_parse_v4.load(fixtures[0].attempt_id)
            cursor = None
            visited = []
            while True:
                result = backfill_v4_execution_specs_once(
                    uow_factory=lambda: _BackfillUow(conn), legacy=legacy,
                    after_attempt_id=cursor, limit=1, write_guard=lambda: None,
                )
                if result.verified:
                    self.assertEqual(result.inserted, 1)
                    visited.append(result.after_attempt_id)
                cursor = result.after_attempt_id
                if result.exhausted:
                    break
            self.assertEqual(set(visited), {item.attempt_id for item in fixtures})
            # The final attempt and staged noncurrent target must both survive.
            with _BackfillUow(conn) as uow:
                self.assertEqual(uow.remote_parse_v4.load(fixtures[0].attempt_id).state, "acked")
                self.assertFalse(uow.remote_parse_v4.load(fixtures[2].attempt_id).is_current)
            with Operations.context(MigrationContext.configure(conn)):
                self.validation.upgrade()
            replay = backfill_v4_execution_specs_once(
                uow_factory=lambda: _BackfillUow(conn), legacy=object(),
                after_attempt_id=None, limit=100, write_guard=lambda: None,
            )
            self.assertEqual((replay.verified, replay.inserted, replay.exhausted), (3, 0, True))
            for sql in (
                "UPDATE disclosure_ops.remote_parse_v4_execution_spec SET created_at=created_at",
                "DELETE FROM disclosure_ops.remote_parse_v4_execution_spec",
            ):
                with conn.begin_nested() as rejected:
                    with self.assertRaisesRegex(SQLAlchemyError, "immutable"):
                        conn.exec_driver_sql(sql)
                    rejected.rollback()
            with self.assertRaisesRegex(RuntimeError, "discard immutable"), Operations.context(MigrationContext.configure(conn)):
                self.artifact.downgrade()
            with _BackfillUow(conn) as uow:
                with self.assertRaisesRegex(RemoteParseV4AuthorityViolation, "exact durable copy"):
                    uow.remote_parse_v4.require_legacy_execution_spec_retirable(replace(
                        fixtures[0].execution_spec,
                        result_lease_seconds=fixtures[0].execution_spec.result_lease_seconds+1,
                    ))
            orphan = _spec()
            legacy._store.create_or_verify(
                relpath=retirement_paths.v4_execution_spec_relpath(spec_sha256=orphan.sha256), payload=orphan.exact_bytes,
            )
            references += (V4ExecutionSpecCatalogReference(orphan.sha256, orphan.byte_count),)
            self.assertEqual(retire_v4_execution_specs_once(
                uow_factory=lambda: _BackfillUow(conn), files=files,
                references=references, ownership_guard=lambda: None,
            ), 4)
            self.assertEqual(retire_v4_execution_specs_once(
                uow_factory=lambda: _BackfillUow(conn), files=files,
                references=references, ownership_guard=lambda: None,
            ), 0)
            with _BackfillUow(conn) as uow:
                for fixture in fixtures:
                    self.assertEqual(uow.remote_parse_v4.load(fixture.attempt_id).execution_spec, fixture.execution_spec)

    def test_guard_rollback_wrong_spec_and_commit_response_loss_replay(self):
        with self._legacy() as (conn, legacy, fixtures):
            calls = 0

            def revoke_after_insert():
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise RuntimeError("revoked before commit")

            with self.assertRaisesRegex(RuntimeError, "revoked"):
                backfill_v4_execution_specs_once(
                    uow_factory=lambda: _BackfillUow(conn), legacy=legacy,
                    after_attempt_id=None, limit=1, write_guard=revoke_after_insert,
                )
            self.assertEqual(conn.execute(text("SELECT count(*) FROM disclosure_ops.remote_parse_v4_execution_spec")).scalar_one(), 0)
            with _BackfillUow(conn) as uow:
                with self.assertRaisesRegex(ValueError, "execution spec drifted"):
                    uow.remote_parse_v4.backfill_execution_spec(
                        attempt_id=fixtures[0].attempt_id, spec=fixtures[1].execution_spec,
                    )

            class LostResponseUow(_BackfillUow):
                def commit(self):
                    super().commit()
                    raise ConnectionError("committed response lost")

            with self.assertRaisesRegex(ConnectionError, "response lost"):
                backfill_v4_execution_specs_once(
                    uow_factory=lambda: LostResponseUow(conn), legacy=legacy,
                    after_attempt_id=None, limit=1, write_guard=lambda: None,
                )
            replay = backfill_v4_execution_specs_once(
                uow_factory=lambda: _BackfillUow(conn), legacy=object(),
                after_attempt_id=None, limit=1, write_guard=lambda: None,
            )
            self.assertEqual((replay.verified, replay.inserted), (1, 0))
