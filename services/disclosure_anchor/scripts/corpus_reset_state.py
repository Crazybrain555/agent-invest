"""Shared database invariants for a manifest-bound corpus reset.

This module contains no filesystem or process-control behavior.  Backup,
restore proof, and reset all use the same read-only state classifier; only
``reset_transaction`` mutates, and it does so in one explicit transaction.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from psycopg import Connection as PsycopgConnection
from sqlalchemy import text
from sqlalchemy.engine import Connection

from scripts.corpus_reparse_manifest import (
    CorpusManifest,
    ManifestError,
    canonical_hash,
    document_input_identity,
    document_source_rows,
)
from scripts.corpus_reset_digest import (
    POST_RESET_PRESERVED_SCOPES,
    RESET_ZERO_STATE_KEYS,
    ResetAction,
    ResetDigestError,
    ResetDigestScope,
    capture_state_matrix,
    database_migration_revision,
    reset_lock_plan_for_migration_revision,
    reset_mutation_specs_for_migration_revision,
    reset_truncate_relations_for_migration_revision,
    reset_zero_probes_for_migration_revision,
    validate_state_matrix,
)


ResetState = Literal["pre_reset", "post_reset"]


def _psycopg_connection(connection: Connection) -> PsycopgConnection[Any]:
    driver = connection.connection.driver_connection
    if not isinstance(driver, PsycopgConnection):
        raise ManifestError(
            "exact reset state requires the configured psycopg driver"
        )
    return driver


def postgres_state(
    connection: Connection,
    *,
    scopes: tuple[ResetDigestScope, ...] | None = None,
    classify_catalog: bool = True,
) -> dict[str, object]:
    """Stream an exact state matrix through the active SQLAlchemy transaction."""

    try:
        return capture_state_matrix(
            _psycopg_connection(connection),
            scopes=scopes,
            classify_catalog=classify_catalog,
        )
    except ResetDigestError as exc:
        raise ManifestError(f"cannot prove exact PostgreSQL state: {exc}") from exc


def manifest_postgres_state(manifest: CorpusManifest) -> dict[str, object]:
    try:
        return validate_state_matrix(manifest.header.get("postgres_state"))
    except ResetDigestError as exc:
        raise ManifestError(f"invalid manifest PostgreSQL state: {exc}") from exc


def _scope_records(matrix: dict[str, object]) -> dict[str, object]:
    scopes = matrix.get("scopes")
    if not isinstance(scopes, dict):
        raise ManifestError("PostgreSQL state matrix lacks scopes")
    return scopes


def zero_state_counts(connection: Connection) -> dict[str, int]:
    """Count every changed scope from the revision-bound reset matrix."""

    revision = database_migration_revision(_psycopg_connection(connection))
    counts = {key: 0 for key in RESET_ZERO_STATE_KEYS}
    for spec, probe in reset_zero_probes_for_migration_revision(revision):
        predicate = (
            probe.predicate
            if probe.predicate is not None
            else spec.predicate
        )
        counts[probe.key] += int(
            connection.execute(
                text(
                    "SELECT count(*) FROM "
                    f"{spec.qualified_relation} "
                    f"WHERE {predicate}"
                )
            ).scalar_one()
        )
    return counts


def assert_zero_state_counts(counts: dict[str, int]) -> None:
    """Require complete zero-state evidence with no residual changed rows."""

    if set(counts) != RESET_ZERO_STATE_KEYS:
        raise ManifestError(
            "reset zero-state coverage mismatch: "
            f"unexpected={sorted(set(counts) - RESET_ZERO_STATE_KEYS)}, "
            f"missing={sorted(RESET_ZERO_STATE_KEYS - set(counts))}"
        )
    residual = {
        key: value
        for key, value in sorted(counts.items())
        if value != 0
    }
    if residual:
        raise ManifestError(
            f"reset postcondition has residual rows: {residual}"
        )


def processing_run_rows(connection: Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT processing_run_id, document_id, run_kind, status, is_active,
                   input_raw_file_hash, parser_artifact_relpath,
                   normalized_ir_relpath, document_units_relpath
              FROM disclosure_core.processing_run
             ORDER BY processing_run_id
            """
        )
    ).mappings()
    normalized: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["processing_run_id"] = str(item["processing_run_id"])
        item["document_id"] = str(item["document_id"])
        normalized.append(item)
    return normalized


def _expected_document_rows(
    manifest: CorpusManifest,
) -> dict[str, tuple[str, str, str, str | None]]:
    return {
        str(row["document_id"]): (
            str(row["raw_file_relpath"]),
            str(row["raw_file_hash"]),
            str(row["old_status"]),
            (
                str(row["old_current_processing_run_id"])
                if row.get("old_current_processing_run_id") is not None
                else None
            ),
        )
        for row in manifest.documents
    }


def assert_manifest_document_state(
    connection: Connection,
    manifest: CorpusManifest,
) -> dict[str, tuple[str, str, str, str | None]]:
    """Require the complete parse/build document and raw-input closure."""

    expected_inputs = {
        str(row["document_id"]): str(row["input_identity_sha256"])
        for row in manifest.documents
    }
    actual_source_rows = document_source_rows(connection)
    actual_inputs = {
        str(row["document_id"]): canonical_hash(document_input_identity(row))
        for row in actual_source_rows
    }
    if actual_inputs != expected_inputs:
        missing = sorted(expected_inputs.keys() - actual_inputs.keys())[:5]
        unexpected = sorted(actual_inputs.keys() - expected_inputs.keys())[:5]
        changed = sorted(
            document_id
            for document_id in expected_inputs.keys() & actual_inputs.keys()
            if expected_inputs[document_id] != actual_inputs[document_id]
        )[:5]
        raise ManifestError(
            "parse/build document input drifted from reset manifest: "
            f"missing={missing}, unexpected={unexpected}, changed={changed}"
        )

    expected_documents = _expected_document_rows(manifest)
    actual_documents = {
        str(row["document_id"]): (
            str(row["raw_file_relpath"]),
            str(row["raw_file_hash"]),
            str(row["status"]),
            (
                str(row["current_processing_run_id"])
                if row["current_processing_run_id"] is not None
                else None
            ),
        )
        for row in actual_source_rows
    }
    expected_raw = {
        document_id: values[:2]
        for document_id, values in expected_documents.items()
    }
    actual_raw = {
        document_id: values[:2]
        for document_id, values in actual_documents.items()
    }
    if actual_raw != expected_raw:
        raise ManifestError("document/raw identity drifted from reset manifest")
    return actual_documents


def assert_post_reset_postgres_state(
    manifest: CorpusManifest,
    actual: dict[str, object],
) -> None:
    """Prove preservation and catalog identity for a zero-state matrix."""

    expected = manifest_postgres_state(manifest)
    actual_records = _scope_records(actual)
    expected_records = _scope_records(expected)
    missing_scopes = sorted(set(expected_records) - set(actual_records))
    unexpected_scopes = sorted(set(actual_records) - set(expected_records))
    if missing_scopes or unexpected_scopes:
        raise ManifestError(
            "post-reset PostgreSQL scope coverage changed: "
            f"missing_scopes={missing_scopes}, "
            f"unexpected_scopes={unexpected_scopes}"
        )
    try:
        validate_state_matrix(actual)
    except ResetDigestError as exc:
        raise ManifestError(f"invalid post-reset PostgreSQL state: {exc}") from exc
    changed_preserved = [
        scope.value
        for scope in POST_RESET_PRESERVED_SCOPES
        if actual_records[scope.value] != expected_records[scope.value]
    ]
    if changed_preserved:
        raise ManifestError(
            "reset changed preserved PostgreSQL scopes: "
            f"{changed_preserved}"
        )
    preserved_names = {
        scope.value for scope in POST_RESET_PRESERVED_SCOPES
    }
    changed_catalog = [
        scope_name
        for scope_name in sorted(set(expected_records) - preserved_names)
        if (
            cast(dict[str, object], actual_records[scope_name]).get(
                "descriptor"
            )
            != cast(dict[str, object], expected_records[scope_name]).get(
                "descriptor"
            )
            or cast(dict[str, object], actual_records[scope_name]).get(
                "descriptor_sha256"
            )
            != cast(dict[str, object], expected_records[scope_name]).get(
                "descriptor_sha256"
            )
        )
    ]
    if changed_catalog:
        raise ManifestError(
            "reset changed mutated-scope catalog descriptors: "
            f"{changed_catalog}"
        )


def inspect_reset_state(
    connection: Connection,
    manifest: CorpusManifest,
) -> tuple[ResetState, dict[str, object], dict[str, int]]:
    """Return one exact classified matrix and its operational zero counters."""

    expected = manifest_postgres_state(manifest)
    actual = postgres_state(connection)
    zero_counts = zero_state_counts(connection)
    if all(value == 0 for value in zero_counts.values()):
        assert_post_reset_postgres_state(manifest, actual)
        return "post_reset", actual, zero_counts
    if actual != expected:
        actual_records = _scope_records(actual)
        expected_records = _scope_records(expected)
        changed = [
            scope_name
            for scope_name in sorted(expected_records)
            if actual_records.get(scope_name) != expected_records[scope_name]
        ]
        raise ManifestError(
            "database is neither the frozen exact pre-reset state nor a "
            f"valid zero state; changed_scopes={changed}"
        )
    return "pre_reset", actual, zero_counts


def detect_reset_state(
    connection: Connection,
    manifest: CorpusManifest,
) -> ResetState:
    """Classify only an exact manifest pre-state or its exact zero-state."""

    return inspect_reset_state(connection, manifest)[0]


def reset_transaction(
    connection: Connection,
    manifest: CorpusManifest,
    *,
    commit: bool,
) -> dict[str, int]:
    """Reset all regenerable database state atomically, or rehearse by rollback."""

    transaction = connection.begin()
    try:
        connection.exec_driver_sql(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        )
        connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '30min'")
        revision = database_migration_revision(
            _psycopg_connection(connection)
        )
        for mode, relations in reset_lock_plan_for_migration_revision(
            revision
        ):
            connection.exec_driver_sql(
                "LOCK TABLE "
                + ", ".join(relations)
                + f" IN {mode} MODE"
            )
        if detect_reset_state(connection, manifest) != "pre_reset":
            raise ManifestError("reset transaction no longer sees pre-reset state")
        for spec in reset_mutation_specs_for_migration_revision(revision):
            if spec.action is ResetAction.UPDATE:
                if spec.update_assignments is None:
                    raise ManifestError(
                        f"reset update scope lacks assignments: "
                        f"{spec.scope.value}"
                    )
                statement = (
                    f"UPDATE {spec.qualified_relation} "
                    f"SET {spec.update_assignments}"
                )
            elif spec.action is ResetAction.DELETE:
                statement = (
                    f"DELETE FROM {spec.qualified_relation} "
                    f"WHERE {spec.predicate}"
                )
            else:
                raise ManifestError(
                    f"unsupported reset mutation action: {spec.action}"
                )
            connection.execute(text(statement))
        reset_relations = reset_truncate_relations_for_migration_revision(
            revision
        )
        if not reset_relations:
            raise ManifestError("reset scope matrix has no truncate relations")
        connection.exec_driver_sql(
            "TRUNCATE " + ", ".join(reset_relations)
        )
        zero_counts = zero_state_counts(connection)
        assert_zero_state_counts(zero_counts)
        assert_post_reset_postgres_state(
            manifest,
            postgres_state(connection),
        )
        if commit:
            transaction.commit()
        else:
            transaction.rollback()
        return zero_counts
    except BaseException:
        if transaction.is_active:
            transaction.rollback()
        raise
