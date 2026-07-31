"""Closed-scope, streaming PostgreSQL reset digest contracts."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from copy import deepcopy
from contextlib import AbstractContextManager
from dataclasses import replace
from typing import Any, cast
import unittest

from psycopg import Connection

from scripts.corpus_reset_digest import (
    DERIVED_OUTBOX_PREDICATE,
    RESET_SCOPE_MATRIX,
    ResetAction,
    ResetDigestError,
    ResetDigestScope,
    assert_migration_feature_catalog,
    assert_service_catalog_classified,
    capture_state_matrix,
    digest_scope,
    reset_lock_plan_for_migration_revision,
    reset_truncate_relations_for_migration_revision,
    reset_zero_probes_for_migration_revision,
    scopes_for_migration_revision,
    validate_reset_scope_contract,
    validate_state_matrix,
)


_SERVER_ROW = ("18.0", 180000, "UTF8", "UTF8")
_DOCUMENT_COLUMNS = [
    (1, "document_id", "text", "pg_catalog", "text", True, "", "", None, None, None),
    (2, "status", "text", "pg_catalog", "text", True, "", "", None, None, None),
    (
        3,
        "current_processing_run_id",
        "text",
        "pg_catalog",
        "text",
        False,
        "",
        "",
        None,
        None,
        None,
    ),
    (
        4,
        "computed",
        "integer",
        "pg_catalog",
        "int4",
        False,
        "",
        "s",
        "(char_length(document_id))",
        None,
        None,
    ),
]
_OUTBOX_COLUMNS = [
    (1, "seq", "bigint", "pg_catalog", "int8", True, "d", "", None, None, None),
    (
        2,
        "subject_kind",
        "text",
        "pg_catalog",
        "text",
        True,
        "",
        "",
        None,
        None,
        None,
    ),
    (
        3,
        "processing_run_id",
        "text",
        "pg_catalog",
        "text",
        False,
        "",
        "",
        None,
        None,
        None,
    ),
    (4, "asset_id", "text", "pg_catalog", "text", False, "", "", None, None, None),
]
_SEQUENCE_COLUMNS = [
    (1, "last_value", "bigint", "pg_catalog", "int8", True, "", "", None, None, None),
    (2, "log_cnt", "bigint", "pg_catalog", "int8", True, "", "", None, None, None),
    (3, "is_called", "boolean", "pg_catalog", "bool", True, "", "", None, None, None),
]


class _FakeCopy(AbstractContextManager["_FakeCopy"]):
    def __init__(self, chunks: tuple[bytes | memoryview, ...]) -> None:
        self._chunks = chunks

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        return None

    def __iter__(self) -> Iterator[bytes | memoryview]:
        return iter(self._chunks)


class _FakeCursor(AbstractContextManager["_FakeCursor"]):
    def __init__(self, database: "_FakeDatabase") -> None:
        self._database = database
        self._rows: list[tuple[object, ...]] = []

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        return None

    def execute(
        self,
        query: object,
        params: object = None,
    ) -> "_FakeCursor":
        rendered = (
            query.as_string() if hasattr(query, "as_string") else str(query)
        )
        self._database.executed.append((rendered, params))
        if "current_setting('server_version')" in rendered:
            self._rows = [self._database.server_row]
        elif "FROM disclosure_ops.alembic_version" in rendered:
            self._rows = [(self._database.migration_revision,)]
        elif "FROM pg_catalog.pg_sequence" in rendered:
            self._rows = [self._database.sequence_row]
        elif "JOIN pg_catalog.pg_index" in rendered:
            self._rows = [(name,) for name in self._database.primary_key]
        elif "c.relkind IN" in rendered:
            self._rows = list(self._database.catalog_rows)
        elif "c.relname = ANY" in rendered:
            self._rows = list(self._database.feature_rows)
        elif "FROM pg_catalog.pg_proc" in rendered:
            self._rows = list(self._database.routine_rows)
        elif "SELECT c.relkind" in rendered:
            self._rows = [(self._database.relkind, "p")]
        elif "JOIN pg_catalog.pg_attribute" in rendered:
            self._rows = list(self._database.columns)
        else:
            raise AssertionError(f"unexpected SQL: {rendered}")
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)

    def copy(self, query: object) -> _FakeCopy:
        rendered = (
            query.as_string() if hasattr(query, "as_string") else str(query)
        )
        self._database.copy_queries.append(rendered)
        return _FakeCopy(self._database.copy_chunks)


class _FakeDatabase:
    def __init__(
        self,
        *,
        columns: Sequence[tuple[object, ...]] | None = None,
        primary_key: tuple[str, ...] = ("document_id",),
        copy_chunks: tuple[bytes | memoryview, ...] = (b"binary-copy",),
        relkind: str = "r",
        server_row: tuple[object, ...] = _SERVER_ROW,
        migration_revision: str = "0030_source_bound_search_atoms",
    ) -> None:
        self.columns = list(
            columns if columns is not None else _DOCUMENT_COLUMNS
        )
        self.primary_key = primary_key
        self.copy_chunks = copy_chunks
        self.relkind = relkind
        self.server_row = server_row
        self.migration_revision = migration_revision
        self.sequence_row: tuple[object, ...] = (
            "bigint",
            1,
            1,
            9_223_372_036_854_775_807,
            1,
            1,
            False,
        )
        self.catalog_rows: list[tuple[object, ...]] = []
        self.feature_rows: list[tuple[object, ...]] = []
        self.routine_rows: list[tuple[object, ...]] = []
        self.executed: list[tuple[str, object]] = []
        self.copy_queries: list[str] = []
        self.cursor_calls = 0

    def cursor(self, **_kwargs: object) -> _FakeCursor:
        self.cursor_calls += 1
        return _FakeCursor(self)

    def as_connection(self) -> Connection[Any]:
        return cast(Connection[Any], self)


class CorpusResetDigestTests(unittest.TestCase):
    def test_digest_is_stable_across_copy_chunk_boundaries(self) -> None:
        whole = _FakeDatabase(copy_chunks=(b"abcdefgh",))
        split = _FakeDatabase(
            copy_chunks=(b"ab", memoryview(b"cde"), b"fgh")
        )

        first = digest_scope(
            whole.as_connection(), ResetDigestScope.DOCUMENT_FULL_PRE
        )
        second = digest_scope(
            split.as_connection(), ResetDigestScope.DOCUMENT_FULL_PRE
        )

        self.assertEqual(first.state_sha256, second.state_sha256)
        self.assertEqual(first.descriptor_sha256, second.descriptor_sha256)
        self.assertEqual(first.copy_byte_count, 8)
        self.assertEqual(second.copy_byte_count, 8)
        self.assertEqual(
            whole.copy_queries,
            [
                'COPY (SELECT "document_id", "status", '
                '"current_processing_run_id", "computed" '
                'FROM "disclosure_core"."document" WHERE TRUE '
                'ORDER BY "document_id") TO STDOUT (FORMAT BINARY)'
            ],
        )

        changed_server = _FakeDatabase(
            copy_chunks=(b"abcdefgh",),
            server_row=("18.1", 180001, "UTF8", "UTF8"),
        )
        changed = digest_scope(
            changed_server.as_connection(),
            ResetDigestScope.DOCUMENT_FULL_PRE,
        )
        self.assertNotEqual(first.state_sha256, changed.state_sha256)

    def test_descriptor_keeps_generated_column_and_fixed_projection(self) -> None:
        database = _FakeDatabase()
        result = digest_scope(
            database.as_connection(),
            ResetDigestScope.DOCUMENT_PRESERVED,
        )

        descriptor = result.descriptor
        self.assertEqual(
            descriptor["projection_columns"],
            ["document_id", "computed"],
        )
        columns = cast(list[dict[str, object]], descriptor["columns"])
        self.assertEqual(columns[-1]["generated"], "s")
        self.assertEqual(
            columns[-1]["default_expression"],
            "(char_length(document_id))",
        )
        self.assertIn('ORDER BY "document_id"', database.copy_queries[0])
        self.assertNotIn('"status"', database.copy_queries[0])
        self.assertNotIn('"current_processing_run_id"', database.copy_queries[0])

        # pg_dump recreates surviving columns without historical dropped-attnum
        # gaps. Logical descriptors must therefore remain restore-stable.
        gapped_columns = [
            (ordinal * 3, *column[1:])
            for ordinal, column in enumerate(_DOCUMENT_COLUMNS, start=1)
        ]
        restored = digest_scope(
            _FakeDatabase(columns=_DOCUMENT_COLUMNS).as_connection(),
            ResetDigestScope.DOCUMENT_PRESERVED,
        )
        gapped = digest_scope(
            _FakeDatabase(columns=gapped_columns).as_connection(),
            ResetDigestScope.DOCUMENT_PRESERVED,
        )
        self.assertEqual(gapped.descriptor_sha256, restored.descriptor_sha256)
        self.assertEqual(gapped.state_sha256, restored.state_sha256)

    def test_outbox_source_is_exact_fixed_complement_of_delete_scope(self) -> None:
        source_database = _FakeDatabase(
            columns=_OUTBOX_COLUMNS,
            primary_key=("seq",),
        )
        derived_database = _FakeDatabase(
            columns=_OUTBOX_COLUMNS,
            primary_key=("seq",),
        )

        source = digest_scope(
            source_database.as_connection(),
            ResetDigestScope.SOURCE_OUTBOX,
        )
        derived = digest_scope(
            derived_database.as_connection(),
            ResetDigestScope.DERIVED_OUTBOX,
        )

        self.assertEqual(
            source.descriptor["predicate"],
            f"{derived.descriptor['predicate']} IS NOT TRUE",
        )
        self.assertEqual(
            derived.descriptor["predicate"], DERIVED_OUTBOX_PREDICATE
        )
        self.assertIn(
            "WHERE (subject_kind IN "
            "('processing_run', 'document_unit')",
            source_database.copy_queries[0],
        )
        self.assertIn("asset_id IS NOT NULL) IS NOT TRUE", source_database.copy_queries[0])
        self.assertIn(
            'FROM "disclosure_ops"."outbox_event"',
            source_database.copy_queries[0],
        )

    def test_sequence_uses_catalog_config_and_singleton_binary_state(self) -> None:
        database = _FakeDatabase(
            columns=_SEQUENCE_COLUMNS,
            primary_key=(),
            relkind="S",
            copy_chunks=(b"sequence-state",),
        )

        result = digest_scope(
            database.as_connection(),
            ResetDigestScope.OUTBOX_EVENT_SEQUENCE,
        )

        self.assertEqual(
            result.descriptor["projection_columns"],
            ["last_value", "is_called"],
        )
        self.assertEqual(
            result.descriptor["sequence_settings"],
            {
                "data_type": "bigint",
                "start_value": 1,
                "increment_by": 1,
                "max_value": 9_223_372_036_854_775_807,
                "min_value": 1,
                "cache_size": 1,
                "cycle": False,
            },
        )
        self.assertEqual(
            database.copy_queries,
            [
                'COPY (SELECT "last_value", "is_called" '
                'FROM "disclosure_ops"."outbox_event_seq_seq") '
                "TO STDOUT (FORMAT BINARY)"
            ],
        )
        matrix_database = _FakeDatabase(
            columns=_SEQUENCE_COLUMNS,
            primary_key=(),
            relkind="S",
            copy_chunks=(b"sequence-state",),
        )
        matrix = capture_state_matrix(
            matrix_database.as_connection(),
            scopes=(ResetDigestScope.OUTBOX_EVENT_SEQUENCE,),
            classify_catalog=False,
        )
        validate_state_matrix(
            matrix,
            expected_scopes=(ResetDigestScope.OUTBOX_EVENT_SEQUENCE,),
        )
        tampered = deepcopy(matrix)
        scopes = tampered["scopes"]
        assert isinstance(scopes, dict)
        record = scopes[ResetDigestScope.OUTBOX_EVENT_SEQUENCE.value]
        assert isinstance(record, dict)
        descriptor = record["descriptor"]
        assert isinstance(descriptor, dict)
        descriptor["copy_format"] = "text"
        with self.assertRaisesRegex(ResetDigestError, "identity mismatch"):
            validate_state_matrix(
                tampered,
                expected_scopes=(
                    ResetDigestScope.OUTBOX_EVENT_SEQUENCE,
                ),
            )

    def test_unknown_scope_missing_pk_and_wrong_kind_fail_closed(self) -> None:
        untouched = _FakeDatabase()
        with self.assertRaisesRegex(ResetDigestError, "unclassified"):
            digest_scope(untouched.as_connection(), "invented")
        self.assertEqual(untouched.cursor_calls, 0)

        without_pk = _FakeDatabase(primary_key=())
        with self.assertRaisesRegex(ResetDigestError, "primary key"):
            digest_scope(
                without_pk.as_connection(),
                ResetDigestScope.DOCUMENT_FULL_PRE,
            )

        view = _FakeDatabase(relkind="v")
        with self.assertRaisesRegex(ResetDigestError, "relkind"):
            digest_scope(
                view.as_connection(),
                ResetDigestScope.DOCUMENT_FULL_PRE,
            )

    def test_service_catalog_must_match_closed_state_relations(self) -> None:
        classified = _FakeDatabase(
            migration_revision="0027_materialized_classification"
        )
        classified.catalog_rows = [
            ("disclosure_core", "company", "r"),
            ("disclosure_core", "company_identifier", "r"),
            ("disclosure_core", "security", "r"),
            ("disclosure_core", "tracked_company", "r"),
            ("disclosure_core", "source_access", "r"),
            ("disclosure_core", "source_checkpoint", "r"),
            ("disclosure_core", "provider_category", "r"),
            ("disclosure_core", "classification_rule", "r"),
            ("disclosure_core", "document", "r"),
            ("disclosure_core", "processing_run", "r"),
            ("disclosure_core", "document_unit", "r"),
            ("disclosure_core", "unit_search_projection", "r"),
            ("disclosure_ops", "outbox_event", "r"),
            ("disclosure_ops", "alembic_version", "r"),
            ("disclosure_ops", "outbox_event_seq_seq", "S"),
        ]
        assert_service_catalog_classified(
            classified.as_connection(),
            revision="0027_materialized_classification",
        )

        classified.catalog_rows.append(
            ("disclosure_core", "unit_body_search_window", "r")
        )
        assert_service_catalog_classified(
            classified.as_connection(),
            revision="0028_safe_search_windows",
        )
        classified.catalog_rows.append(
            ("disclosure_core", "unit_search_atom", "r")
        )
        assert_service_catalog_classified(
            classified.as_connection(),
            revision="0030_source_bound_search_atoms",
        )
        classified.catalog_rows.append(
            ("disclosure_core", "unclassified_table", "r")
        )
        with self.assertRaisesRegex(ResetDigestError, "unexpected"):
            assert_service_catalog_classified(
                classified.as_connection(),
                revision="0030_source_bound_search_atoms",
            )

    def test_migration_graph_defines_only_reachable_search_scopes(self) -> None:
        base = scopes_for_migration_revision(
            "0027_materialized_classification"
        )
        window = scopes_for_migration_revision("0028_safe_search_windows")
        projection_state = scopes_for_migration_revision(
            "0029_run_projection_state"
        )
        atom = scopes_for_migration_revision(
            "0030_source_bound_search_atoms"
        )

        self.assertNotIn(ResetDigestScope.UNIT_BODY_SEARCH_WINDOW, base)
        self.assertNotIn(ResetDigestScope.UNIT_SEARCH_ATOM, base)
        self.assertIn(ResetDigestScope.UNIT_BODY_SEARCH_WINDOW, window)
        self.assertNotIn(ResetDigestScope.UNIT_SEARCH_ATOM, window)
        self.assertEqual(window, projection_state)
        self.assertIn(ResetDigestScope.UNIT_BODY_SEARCH_WINDOW, atom)
        self.assertIn(ResetDigestScope.UNIT_SEARCH_ATOM, atom)
        with self.assertRaisesRegex(ResetDigestError, "unsupported"):
            scopes_for_migration_revision("not_a_revision")

    def test_scope_matrix_drives_locks_truncation_and_zero_proofs(self) -> None:
        locks_0027 = dict(
            reset_lock_plan_for_migration_revision(
                "0027_materialized_classification"
            )
        )
        self.assertEqual(
            locks_0027["ACCESS EXCLUSIVE"],
            (
                "disclosure_core.document",
                "disclosure_core.processing_run",
                "disclosure_core.document_unit",
                "disclosure_core.unit_search_projection",
                "disclosure_ops.outbox_event",
            ),
        )
        self.assertEqual(
            reset_truncate_relations_for_migration_revision(
                "0027_materialized_classification"
            ),
            (
                "disclosure_core.unit_search_projection",
                "disclosure_core.document_unit",
                "disclosure_core.processing_run",
            ),
        )
        self.assertEqual(
            reset_truncate_relations_for_migration_revision(
                "0028_safe_search_windows"
            ),
            (
                "disclosure_core.unit_body_search_window",
                "disclosure_core.unit_search_projection",
                "disclosure_core.document_unit",
                "disclosure_core.processing_run",
            ),
        )
        target_relations = reset_truncate_relations_for_migration_revision(
            "0031_artifact_owner_run"
        )
        self.assertEqual(
            target_relations,
            (
                "disclosure_core.unit_search_atom",
                "disclosure_core.unit_body_search_window",
                "disclosure_core.unit_search_projection",
                "disclosure_core.document_unit",
                "disclosure_core.processing_run",
            ),
        )
        probes = reset_zero_probes_for_migration_revision(
            "0031_artifact_owner_run"
        )
        proved_scopes = {spec.scope for spec, _probe in probes}
        self.assertEqual(
            proved_scopes,
            {
                spec.scope
                for spec in RESET_SCOPE_MATRIX
                if spec.action is not ResetAction.NONE
            },
        )

    def test_incomplete_scope_or_missing_truncate_fails_closed(self) -> None:
        with self.assertRaisesRegex(ResetDigestError, "every digest scope"):
            validate_reset_scope_contract(RESET_SCOPE_MATRIX[:-1])

        broken = tuple(
            (
                replace(spec, truncate_order=None)
                if spec.scope is ResetDigestScope.UNIT_SEARCH_ATOM
                else spec
            )
            for spec in RESET_SCOPE_MATRIX
        )
        with self.assertRaisesRegex(ResetDigestError, "incomplete reset policy"):
            validate_reset_scope_contract(broken)

    def test_feature_catalog_is_bound_to_migration_ancestry(self) -> None:
        database = _FakeDatabase()

        assert_migration_feature_catalog(
            database.as_connection(),
            revision="0027_materialized_classification",
        )

        database.feature_rows = [
            ("disclosure_core", "ix_unit_body_search_window_tsv", "i"),
            ("disclosure_core", "unit_body_search_window", "r"),
            (
                "disclosure_public",
                "unit_body_search_windows_v1",
                "v",
            ),
        ]
        database.routine_rows = [
            (
                "disclosure_core",
                "search_tsvector_is_safe",
                "title_tokens text, path_tokens text, "
                "body_tokens text, key_tokens text",
            )
        ]
        assert_migration_feature_catalog(
            database.as_connection(),
            revision="0028_safe_search_windows",
        )

        database.feature_rows.extend(
            [
                ("disclosure_core", "ix_unit_search_atom_text_trgm", "i"),
                ("disclosure_core", "unit_search_atom", "r"),
                ("disclosure_public", "unit_search_atoms_v1", "v"),
            ]
        )
        assert_migration_feature_catalog(
            database.as_connection(),
            revision="0030_source_bound_search_atoms",
        )

        database.feature_rows.pop()
        with self.assertRaisesRegex(ResetDigestError, "missing_objects"):
            assert_migration_feature_catalog(
                database.as_connection(),
                revision="0030_source_bound_search_atoms",
            )

        atom_only = _FakeDatabase()
        atom_only.feature_rows = [
            ("disclosure_core", "ix_unit_search_atom_text_trgm", "i"),
            ("disclosure_core", "unit_search_atom", "r"),
            ("disclosure_public", "unit_search_atoms_v1", "v"),
        ]
        with self.assertRaisesRegex(ResetDigestError, "unexpected_objects"):
            assert_migration_feature_catalog(
                atom_only.as_connection(),
                revision="0027_materialized_classification",
            )


if __name__ == "__main__":
    unittest.main()
