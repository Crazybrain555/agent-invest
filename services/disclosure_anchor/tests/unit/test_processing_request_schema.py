from __future__ import annotations

import importlib
import json
import sqlite3
from typing import Any
import unittest
from unittest import mock

from sqlalchemy import CheckConstraint, Index
from sqlalchemy.dialects.postgresql import psycopg

from disclosure_anchor.adapters.db.postgres import models
from disclosure_anchor.adapters.db.postgres.migration_state import single_migration_head
from disclosure_anchor.adapters.db.postgres.schema import (
    APP_ROLE,
    FUTURE_L2_READER_ROLE,
    OPS_SCHEMA,
    READER_ROLE,
)
from disclosure_anchor.application.worker import queries

MIGRATION = importlib.import_module(
    "disclosure_anchor.adapters.db.postgres.migrations.versions.0066_processing_request"
)
TABLES = {
    "processing_request": models.ProcessingRequest,
    "processing_request_attempt": models.ProcessingRequestAttempt,
    "processing_request_target": models.ProcessingRequestTarget,
}


class _Recorder:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params: list[dict[str, Any]] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.statements.append(str(statement))
        self.params.append(dict(params or {}))
        result = mock.MagicMock()
        result.mappings.return_value = []
        return result


class MigrationParityTests(unittest.TestCase):
    def test_revision_appends_to_the_graph_head(self) -> None:
        self.assertEqual(MIGRATION.down_revision, "0065_requeue_key_lifetime")
        self.assertEqual(single_migration_head(), MIGRATION.revision)

    def test_tables_constraints_and_indexes_match_the_models(self) -> None:
        created: dict[str, Any] = {}
        indexes: dict[str, tuple[tuple[str, ...], str | None, bool]] = {}

        def create_table(name: str, *items: Any, **kwargs: Any) -> None:
            created[name] = items

        def create_index(name: str, table: str, columns: list[str], **kwargs: Any) -> None:
            where = kwargs.get("postgresql_where")
            indexes[name] = (tuple(columns), None if where is None else str(where),
                             bool(kwargs.get("unique", False)))

        with (
            mock.patch.object(MIGRATION.op, "create_table", side_effect=create_table),
            mock.patch.object(MIGRATION.op, "create_index", side_effect=create_index),
            mock.patch.object(MIGRATION.op, "execute") as execute,
        ):
            MIGRATION.upgrade()
        self.assertEqual(set(created), set(TABLES))
        for name, model in TABLES.items():
            table = model.__table__
            migration_columns = {item.name for item in created[name] if hasattr(item, "type")}
            self.assertEqual(migration_columns, {column.name for column in table.columns}, name)
            migration_checks = {
                item.name: str(item.sqltext) for item in created[name]
                if isinstance(item, CheckConstraint)
            }
            model_checks = {
                constraint.name: str(constraint.sqltext) for constraint in table.constraints
                if isinstance(constraint, CheckConstraint)
            }
            self.assertEqual(migration_checks, model_checks, name)
            for index in table.indexes:
                assert isinstance(index, Index)
                where = index.dialect_options["postgresql"].get("where")
                self.assertEqual(
                    indexes[str(index.name)],
                    (tuple(column.name for column in index.columns),
                     None if where is None else str(where), bool(index.unique)),
                    index.name,
                )
        grants = [str(call.args[0]) for call in execute.call_args_list]
        for name in TABLES:
            self.assertIn(
                f"REVOKE ALL ON {OPS_SCHEMA}.{name} FROM PUBLIC, {READER_ROLE}, {FUTURE_L2_READER_ROLE}",
                grants,
            )
        self.assertIn(f"GRANT SELECT, INSERT ON {OPS_SCHEMA}.processing_request TO {APP_ROLE}", grants)
        self.assertIn(
            f"GRANT SELECT, INSERT, UPDATE ON {OPS_SCHEMA}.processing_request_attempt TO {APP_ROLE}",
            grants,
        )
        self.assertIn(f"GRANT SELECT, INSERT ON {OPS_SCHEMA}.processing_request_target TO {APP_ROLE}", grants)
        attempt_columns = {item.name: item for item in created["processing_request_attempt"]
                           if hasattr(item, "type")}
        for name in ("choices", "error"):
            self.assertTrue(attempt_columns[name].type.none_as_null, name)

    def test_nullable_json_columns_bind_python_none_as_sql_null(self) -> None:
        dialect = psycopg.dialect()  # type: ignore[no-untyped-call]
        table = models.ProcessingRequestAttempt.__table__
        for name in ("choices", "error"):
            bind = table.c[name].type.dialect_impl(dialect).bind_processor(dialect)
            assert bind is not None
            # Not Jsonb(None): the JSON null literal would fail the nullable CHECKs.
            self.assertIsNone(bind(None), name)
            self.assertIsNotNone(bind([] if name == "choices" else {}), name)


def _jsonb_typeof(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = json.loads(raw)
    for kind, matches in (("null", value is None), ("boolean", isinstance(value, bool)),
                          ("object", isinstance(value, dict)), ("array", isinstance(value, list)),
                          ("string", isinstance(value, str))):
        if matches:
            return kind
    return "number"


class CheckThreeValuedLogicTests(unittest.TestCase):
    """The model's exact CHECK text refuses a missing fact instead of passing UNKNOWN.

    SQLite shares SQL's CHECK rule (only FALSE refuses) and these operators,
    so the 0066 text is evaluated offline here; PostgreSQL itself is exercised
    by the scratch integration test.
    """

    def _table(self, model: Any, constraint: str, columns: str) -> sqlite3.Connection:
        check = next(item for item in model.__table__.constraints
                     if isinstance(item, CheckConstraint) and item.name == constraint)
        conn = sqlite3.connect(":memory:")
        conn.create_function("jsonb_typeof", 1, _jsonb_typeof, deterministic=True)
        conn.execute(f"CREATE TABLE t ({columns}, CONSTRAINT {constraint} CHECK ({check.sqltext}))")
        self.addCleanup(conn.close)
        return conn

    def test_attempt_outcome_needs_its_evidence_and_respects_the_lease(self) -> None:
        conn = self._table(
            models.ProcessingRequestAttempt, "ck_processing_request_attempt_outcome",
            "started_at TEXT, lease_expires_at TEXT, finished_at TEXT, outcome TEXT, "
            "next_attempt_at TEXT, index_source_access_id TEXT, provider_row_count INTEGER, "
            "choices TEXT, error TEXT",
        )
        start, inside, lease, after = ("2026-10-07 04:00", "2026-10-07 04:05",
                                       "2026-10-07 04:15", "2026-10-07 04:20")
        error, choices = '{"error_code": "x"}', "[]"

        def row(**values: Any) -> dict[str, Any]:
            base = {"started_at": start, "lease_expires_at": lease, "finished_at": None,
                    "outcome": None, "next_attempt_at": None, "index_source_access_id": None,
                    "provider_row_count": None, "choices": None, "error": None}
            return {**base, **values}

        accepted = {
            "open": row(),
            "matched": row(finished_at=inside, outcome="matched", index_source_access_id="sa"),
            "not_found": row(finished_at=inside, outcome="not_found"),
            "ambiguous": row(finished_at=inside, outcome="ambiguous", choices=choices),
            "failed": row(finished_at=inside, outcome="failed", error=error, next_attempt_at=after),
            "deferred": row(finished_at=inside, outcome="deferred", error=error, next_attempt_at=inside),
            "abandoned": row(finished_at=after, outcome="abandoned", error=error, next_attempt_at=after),
        }
        refused = {
            "failed without error": row(finished_at=inside, outcome="failed"),
            "failed with JSON null error": row(finished_at=inside, outcome="failed", error="null"),
            "deferred without error": row(finished_at=inside, outcome="deferred"),
            "abandoned without error": row(finished_at=after, outcome="abandoned"),
            "ambiguous without choices": row(finished_at=inside, outcome="ambiguous"),
            "matched with JSON null choices": row(finished_at=inside, outcome="matched",
                                                  index_source_access_id="sa", choices="null"),
            "matched without index evidence": row(finished_at=inside, outcome="matched"),
            "open attempt with a scheduled retry": row(next_attempt_at=after),
            "outcome recorded after the lease": row(finished_at=after, outcome="not_found"),
            "abandoned while still leased": row(finished_at=inside, outcome="abandoned", error=error),
        }
        insert = (f"INSERT INTO t ({', '.join(row())}) "
                  f"VALUES ({', '.join(':' + name for name in row())})")
        for name, values in accepted.items():
            with self.subTest(accepted=name):
                conn.execute(insert, values)
        for name, values in refused.items():
            with self.subTest(refused=name), self.assertRaises(sqlite3.IntegrityError):
                conn.execute(insert, values)

    def test_request_target_kind_must_be_present_and_agree(self) -> None:
        conn = self._table(models.ProcessingRequest, "ck_processing_request_target",
                           "target_kind TEXT, target TEXT")
        conn.execute("INSERT INTO t VALUES ('document', '{\"kind\": \"document\"}')")
        for target in ("{}", '{"kind": null}', '{"kind": "title_match"}', "[]", "null"):
            with self.subTest(target=target), self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO t VALUES ('document', ?)", (target,))


class QueuePredicateCompositionTests(unittest.TestCase):
    def test_parse_admission_is_an_alternative_to_the_scope_gates_only(self) -> None:
        recorder = _Recorder()
        queries.pending_parse(recorder, max_retries=3, limit=1,  # type: ignore[arg-type]
                              scope_classes=("annual_report",), require_active_company_scope=True,
                              document_ids=("doc_x",))
        sql = recorder.statements[0]
        retry = sql.index("COALESCE(q.last_failed_retryable, true)")
        alternative = sql.index("OR \n    EXISTS (")
        self.assertLess(retry, alternative)
        self.assertIn("processing_request_target AS pra", sql)
        self.assertIn("pra.raw_file_hash = d.raw_file_hash", sql)
        # The frozen document allowlist stays outside the alternative.
        self.assertGreater(sql.index("q.document_id = ANY(:document_ids)"), alternative)

    def test_frozen_replay_without_gates_reads_no_admission(self) -> None:
        recorder = _Recorder()
        queries.pending_parse(recorder, max_retries=3, limit=1,  # type: ignore[arg-type]
                              scope_classes=None, require_active_company_scope=False)
        self.assertNotIn("processing_request_target", recorder.statements[0])

    def test_download_admission_is_first_acquisition_only_and_exact(self) -> None:
        recorder = _Recorder()
        self.assertEqual(
            queries.pending_downloads(recorder, max_retries=3, limit=1,  # type: ignore[arg-type]
                                      provider_document_ids=()),
            [],
        )
        self.assertEqual(recorder.statements, [])
        queries.pending_downloads(recorder, max_retries=3, limit=1,  # type: ignore[arg-type]
                                  scope_classes=("annual_report",),
                                  provider_document_ids=("1220000001",))
        sql = recorder.statements[0]
        self.assertIn("NOT q.already_registered", sql)
        self.assertIn("pra.provider_document_id = q.provider_document_id", sql)
        self.assertLess(sql.index("q.failed_download_count < :max_retries"), sql.index("pra.admit_download"))
        self.assertEqual(recorder.params[0]["provider_document_ids"], ["1220000001"])


if __name__ == "__main__":
    unittest.main()
