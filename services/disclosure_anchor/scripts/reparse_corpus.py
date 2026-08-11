"""Manifest-bound corpus export, verification, status and resident replay.

This module owns no scheduler.  ``--run`` validates the immutable reset
bundle, constructs one exact replay guard, and hands it to the same
resident worker used in normal production.  PostgreSQL remains the durable
queue and the existing worker remains the only retry, recovery, projection,
watchdog and concurrency implementation.

The existing launchd worker label can supervise replay by setting these
machine-local ``worker.env`` values before restarting it:

* ``DISCLOSURE_REPLAY_MANIFEST``
* ``DISCLOSURE_REPLAY_RESET_RECEIPT``

Remove those values and restart the same job only after ``--status`` reports
``complete=true``.  No title, filing taxonomy, or post-reset status can
narrow the frozen document/raw target.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sys
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from scripts.corpus_reparse_manifest import (  # noqa: E402
    MANIFEST_SCHEMA,
    CorpusManifest,
    ManifestError,
    canonical_hash,
    capture_code_snapshot,
    document_input_identity,
    document_source_rows,
    hash_file,
    load_manifest,
    safe_data_path,
    validate_code_snapshot,
    validate_reset_bundle_paths,
    write_manifest,
)
from scripts.corpus_reset_backup import (  # noqa: E402
    assert_same_database_identity,
    database_identity,
    reset_receipt_path,
    verify_reset_receipt,
)
from scripts.corpus_reset_quiescence import worker_singleton_lock  # noqa: E402
from scripts.corpus_reset_state import (  # noqa: E402
    assert_manifest_document_state,
    postgres_state,
    processing_run_rows,
)
from disclosure_anchor.adapters.db.postgres.connection import (  # noqa: E402
    create_db_engine,
    migration_database_url,
)
from disclosure_anchor.adapters.db.postgres.migration_state import (  # noqa: E402
    single_migration_head,
)
from disclosure_anchor.adapters.db.postgres.schema import (  # noqa: E402
    ALEMBIC_VERSION_TABLE,
    ALEMBIC_VERSION_TABLE_SCHEMA,
)
from disclosure_anchor.adapters.retrieval.tokenizer import (  # noqa: E402
    RETRIEVAL_RULES_VERSION,
)
from disclosure_anchor.application.contracts.parser_target import (  # noqa: E402
    ParserTargetIdentityError,
)
from disclosure_anchor.application.contracts.provider_unit import (  # noqa: E402
    PROVIDER_UNIT_BUILDER_VERSION,
)
from disclosure_anchor.application.worker import queries  # noqa: E402
from disclosure_anchor.application.worker.locks import (  # noqa: E402
    exclusive_corpus_mutation,
)
from disclosure_anchor.application.worker.worker import (  # noqa: E402
    WorkerDeps,
)
from disclosure_anchor.cli.worker import (  # noqa: E402
    ExactReplayGuard,
    WorkerSingletonGuardError,
    assert_worker_singleton_or_cancel,
    build_worker_dependencies,
    run_resident_worker,
    worker_database_url,
)
from disclosure_anchor.settings import Settings, load_settings  # noqa: E402


CONTINUE_EXIT_CODE = 75
INVARIANT_EXIT_CODE = 70
TERMINAL_FAILURE_EXIT_CODE = 65


@dataclass(frozen=True)
class ReparseGeneration:
    started_at: datetime
    receipt_sha256: str
    database_identity: dict[str, Any]


def _parse_utc_timestamp(value: str, *, label: str) -> datetime:
    if not value or value != value.strip():
        raise ManifestError(f"{label} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(f"{label} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ManifestError(f"{label} must be an explicit UTC timestamp")
    try:
        if parsed.timestamp() <= 0:
            raise ManifestError(f"{label} must be after the Unix epoch")
    except (OverflowError, OSError) as exc:
        raise ManifestError(f"{label} is outside the supported range") from exc
    return parsed.astimezone(timezone.utc)


def _backup_path_from_reset_receipt(receipt_path: Path) -> Path:
    marker = ".reset-receipt.json"
    absolute = receipt_path.absolute()
    encoded = str(absolute)
    if not encoded.endswith(marker):
        raise ManifestError(
            "reset receipt path must end with .reset-receipt.json"
        )
    backup = Path(encoded[: -len(marker)])
    if reset_receipt_path(backup).absolute() != absolute:
        raise ManifestError("reset receipt path is not bound to a backup path")
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ManifestError(f"reset receipt is missing or unsafe: {receipt_path}")
    return backup


def _load_reparse_generation(
    engine: Engine,
    manifest: CorpusManifest,
    *,
    reset_receipt: Path,
) -> ReparseGeneration:
    backup = _backup_path_from_reset_receipt(reset_receipt)
    verification = verify_reset_receipt(
        backup,
        manifest=manifest,
    )
    boundary = _parse_utc_timestamp(
        str(verification.receipt["reset_boundary_at"]),
        label="reset receipt generation boundary",
    )
    expected_database_identity = verification.receipt["database_identity"]
    with engine.connect() as connection:
        live_database_identity = database_identity(connection)
    if live_database_identity != expected_database_identity:
        raise ManifestError(
            "live database identity differs from the immutable reset receipt"
        )
    return ReparseGeneration(
        started_at=boundary,
        receipt_sha256=verification.receipt_sha256,
        database_identity=dict(expected_database_identity),
    )


def _target_identity(
    deps: WorkerDeps,
) -> dict[str, Any]:
    parser_identity = deps.parser_factory().identity()
    options = deps.parser_options
    try:
        parser_target = options.target_identity(parser_identity)
    except ParserTargetIdentityError as exc:
        raise ManifestError(f"invalid parser target: {exc}") from exc
    if not parser_target.full_pdf:
        raise ManifestError("corpus reparse parser must cover the full PDF")
    return {
        "parser_target": parser_target.to_payload(),
        "max_parse_retries": deps.config.max_parse_retries,
        "max_build_retries": deps.config.max_build_retries,
        "builder_rules_version": PROVIDER_UNIT_BUILDER_VERSION,
        "retrieval_rules_version": RETRIEVAL_RULES_VERSION,
    }


def _export_fingerprint(connection: Connection) -> dict[str, Any]:
    """Read the complete DB reset/replay closure from one snapshot."""

    runs = processing_run_rows(connection)
    return {
        "documents": document_source_rows(connection),
        "runs": runs,
        "postgres_state": postgres_state(connection),
        "running": sum(row["status"] == "running" for row in runs),
    }


def _export_manifest(
    *,
    engine: Engine,
    settings: Settings,
    deps: WorkerDeps,
    output: Path,
) -> str:
    data_root = Path(settings.disclosure_data_root)
    validate_reset_bundle_paths(
        data_root,
        output,
        output.with_suffix(output.suffix + ".sha256"),
    )
    frozen_target_identity = _target_identity(deps)
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.exec_driver_sql(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )
        frozen = _export_fingerprint(connection)
        transaction.rollback()
    if frozen["running"]:
        raise ManifestError(
            "cannot freeze corpus while "
            f"{frozen['running']} processing runs are running"
        )

    documents: list[dict[str, Any]] = []
    for row in frozen["documents"]:
        if len(documents) % 100 == 0:
            deps.admission_guard()
        document_id = str(row["document_id"])
        raw_relpath = row["raw_file_relpath"]
        raw_hash = row["raw_file_hash"]
        if not isinstance(raw_relpath, str) or not raw_relpath:
            raise ManifestError(f"document {document_id} has no raw_file_relpath")
        if not isinstance(raw_hash, str) or not raw_hash:
            raise ManifestError(f"document {document_id} has no raw_file_hash")
        raw_path = safe_data_path(data_root, raw_relpath, family="raw")
        if not raw_path.is_file():
            raise ManifestError(
                f"document {document_id} raw PDF is missing: {raw_path}"
            )
        actual_hash = hash_file(raw_path)
        if actual_hash != raw_hash:
            raise ManifestError(
                f"document {document_id} raw hash mismatch: "
                f"expected {raw_hash}, got {actual_hash}"
            )
        input_identity = document_input_identity(row)
        documents.append(
            {
                "document_id": document_id,
                "raw_file_relpath": raw_relpath,
                "raw_file_hash": raw_hash,
                "old_status": row["status"],
                "old_current_processing_run_id": row[
                    "current_processing_run_id"
                ],
                "input_identity_sha256": canonical_hash(input_identity),
            }
        )
    deps.admission_guard()

    runs = [dict(row) for row in frozen["runs"]]
    for run in runs:
        for key in ("processing_run_id", "document_id"):
            run[key] = str(run[key])
        for key in (
            "parser_artifact_relpath",
            "normalized_ir_relpath",
            "document_units_relpath",
        ):
            value = run.get(key)
            if value is not None and not isinstance(value, str):
                raise ManifestError(
                    f"run {run['processing_run_id']} has invalid {key}"
                )

    header: dict[str, Any] = {
        "manifest_schema": MANIFEST_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "document_count": len(documents),
        "processing_run_count": len(runs),
        "postgres_state": frozen["postgres_state"],
        "target_identity": frozen_target_identity,
        "code_snapshot": capture_code_snapshot(),
    }
    # Raw hashing can take hours. The singleton prevents the resident worker
    # from changing scope; this second snapshot catches independent intake.
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.exec_driver_sql(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )
        current = _export_fingerprint(connection)
        transaction.rollback()
    if current != frozen:
        raise ManifestError(
            "database changed while raw files were being verified; "
            "stop all intake writers and export a fresh manifest"
        )
    if _target_identity(deps) != frozen_target_identity:
        raise ManifestError(
            "MinerU parser/deployment identity changed during manifest export"
        )
    digest = write_manifest(
        output,
        header=header,
        documents=documents,
        runs=runs,
    )
    load_manifest(output, data_root=data_root, verify_raw_files=False)
    print(
        f"[manifest] documents={len(documents)} runs={len(runs)} "
        f"sha256={digest} -> {output}"
    )
    return digest


def _validate_runtime_identity(
    manifest: CorpusManifest,
    deps: WorkerDeps,
    settings: Settings,
) -> dict[str, Any]:
    del settings
    actual = _target_identity(deps)
    expected = manifest.header["target_identity"]
    if actual != expected:
        raise ManifestError(
            "runtime parser/rules identity differs from frozen manifest: "
            f"expected={expected}, actual={actual}"
        )
    return actual


def _validate_document_truth(
    engine: Engine,
    manifest: CorpusManifest,
) -> None:
    with engine.connect() as connection:
        assert_manifest_document_state(connection, manifest)


def _assert_generation_run_identity(
    engine: Engine,
    manifest: CorpusManifest,
    *,
    generation_started_at: datetime,
) -> None:
    target = manifest.header["target_identity"]
    parser_target = target["parser_target"]
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT run.processing_run_id
                  FROM disclosure_core.processing_run AS run
                  LEFT JOIN disclosure_core.document AS document
                    ON document.document_id = run.document_id
                 WHERE run.started_at >= :generation_started_at
                   AND (
                        run.run_kind <> 'parse'
                        OR document.document_id IS NULL
                        OR run.input_raw_file_hash
                           IS DISTINCT FROM document.raw_file_hash
                        OR (
                            run.parser_target_identity IS NULL
                            AND NOT (
                                run.status = 'failed'
                                AND run.error->>'stage' = 'parser_identity'
                                AND run.error->>'error_code'
                                  = 'parser_version_probe_failed'
                            )
                        )
                        OR (
                            run.parser_target_identity IS NOT NULL
                            AND run.parser_target_identity
                              IS DISTINCT FROM CAST(:parser_target AS jsonb)
                        )
                        OR (
                            run.builder_rules_version IS NOT NULL
                            AND run.builder_rules_version
                              IS DISTINCT FROM :builder_rules_version
                        )
                   )
                 ORDER BY processing_run_id
                """
            ),
            {
                "generation_started_at": generation_started_at,
                "parser_target": json.dumps(
                    parser_target,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "builder_rules_version": target["builder_rules_version"],
            },
        ).scalars()
        invalid = [str(run_id) for run_id in rows]
    if invalid:
        raise ManifestError(
            "generation contains non-manifest parser/raw runs: "
            f"{invalid[:5]}"
        )


def _assert_migration_head(engine: Engine) -> None:
    """Require the exact repository schema before replay/status evaluation."""

    with engine.connect() as connection:
        current = connection.execute(
            text(
                f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE_SCHEMA}."
                f"{ALEMBIC_VERSION_TABLE}"
            )
        ).scalar_one_or_none()
    expected = single_migration_head()
    if current != expected:
        raise ManifestError(
            "corpus replay requires the repository migration head: "
            f"expected {expected}, got {current}"
        )


def _replay_status(
    engine: Engine,
    manifest: CorpusManifest,
    *,
    generation_started_at: datetime,
) -> dict[str, Any]:
    intended_ids = tuple(
        sorted(str(row["document_id"]) for row in manifest.documents)
    )
    with engine.connect() as connection:
        states = queries.document_processing_states(
            connection,
            document_ids=intended_ids,
            generation_started_at=generation_started_at,
            target_identity=dict(manifest.header["target_identity"]),
        )
        counts = connection.execute(
            text(
                """
                WITH active_unit AS (
                    SELECT unit.asset_id
                      FROM disclosure_core.document_unit AS unit
                     JOIN disclosure_core.processing_run AS run
                        ON run.processing_run_id = unit.processing_run_id
                     WHERE run.is_active
                       AND run.started_at >= :generation_started_at
                )
                SELECT
                  (SELECT count(*)
                     FROM disclosure_core.processing_run
                    WHERE status = 'running'
                      AND started_at >= :generation_started_at)
                    AS running_runs,
                  (SELECT count(*)
                     FROM disclosure_core.processing_run
                    WHERE status = 'failed'
                      AND started_at >= :generation_started_at)
                    AS failed_runs,
                  (SELECT count(*)
                     FROM disclosure_core.processing_run
                    WHERE is_active
                      AND started_at >= :generation_started_at)
                    AS active_runs,
                  (SELECT count(*)
                     FROM disclosure_core.processing_run
                    WHERE started_at IS NULL
                       OR started_at < :generation_started_at)
                    AS runs_before_generation,
                  (SELECT count(*) FROM active_unit) AS active_units,
                  (SELECT count(*)
                     FROM active_unit AS unit
                     LEFT JOIN disclosure_core.unit_search_projection AS p
                       ON p.asset_id = unit.asset_id
                    WHERE p.asset_id IS NULL) AS projection_missing,
                  (SELECT count(*)
                     FROM disclosure_core.unit_search_projection AS p
                     LEFT JOIN active_unit AS unit
                       ON unit.asset_id = p.asset_id
                    WHERE unit.asset_id IS NULL) AS projection_orphan,
                  (SELECT count(*)
                     FROM disclosure_core.unit_search_projection
                    WHERE retrieval_rules_version <> :rules_version)
                    AS projection_stale
                """
            ),
            {
                "generation_started_at": generation_started_at,
                "rules_version": RETRIEVAL_RULES_VERSION,
            },
        ).mappings().one()
    normalized = {key: int(value) for key, value in counts.items()}
    state_counts = {
        state_name: sum(state.state == state_name for state in states)
        for state_name in (
            "pending",
            "terminal_failed",
            "usable_published",
        )
    }
    reason_counts: dict[str, int] = {}
    for state in states:
        reason_counts[state.reason_code] = (
            reason_counts.get(state.reason_code, 0) + 1
        )
    invariant_states = [
        state for state in states if state.invariant_codes
    ]
    global_invariant_fields = (
        "runs_before_generation",
        "projection_orphan",
        "projection_stale",
    )
    global_invariants = {
        field: normalized[field]
        for field in global_invariant_fields
        if normalized[field] != 0
    }
    settled = state_counts["pending"] == 0
    successful = (
        settled
        and state_counts["terminal_failed"] == 0
        and not invariant_states
        and not global_invariants
        and normalized["active_runs"] == len(intended_ids)
    )
    return {
        "manifest_sha256": manifest.sha256,
        "generation_started_at": generation_started_at.isoformat(),
        "intended_documents": len(intended_ids),
        "state_counts": state_counts,
        "completed_documents": state_counts["usable_published"],
        "pending_documents": state_counts["pending"],
        "terminal_failed_documents": state_counts["terminal_failed"],
        "reason_counts": dict(sorted(reason_counts.items())),
        "pending_document_sample": [
            state.document_id
            for state in states
            if state.state == "pending"
        ][:10],
        "terminal_document_sample": [
            {
                "document_id": state.document_id,
                "reason_code": state.reason_code,
                "invariant_codes": list(state.invariant_codes),
            }
            for state in states
            if state.state == "terminal_failed"
        ][:10],
        "global_invariants": global_invariants,
        "invariant_documents": len(invariant_states),
        **normalized,
        "settled": settled,
        "successful": successful,
        "complete": successful,
    }


def _status_exit_code(status: dict[str, Any]) -> int:
    if status["complete"]:
        return 0
    if status["invariant_documents"] or status["global_invariants"]:
        return INVARIANT_EXIT_CODE
    if status["state_counts"]["pending"]:
        return CONTINUE_EXIT_CODE
    return TERMINAL_FAILURE_EXIT_CODE


def _exact_replay_guard(
    manifest: CorpusManifest,
    generation: ReparseGeneration,
    settings: Settings,
) -> ExactReplayGuard:
    expected_raw_hashes = {
        str(row["document_id"]): str(row["raw_file_hash"])
        for row in manifest.documents
    }

    def runtime_check(deps: WorkerDeps) -> None:
        _validate_runtime_identity(manifest, deps, settings)

    next_database_check_at = 0.0

    def database_check(engine: Engine) -> None:
        nonlocal next_database_check_at
        # The full source closure is ~47k rows; raw identity is still checked
        # for every document, while this broader invariant is rescanned once
        # per minute across the weeks-long replay.
        now = time.monotonic()
        if now < next_database_check_at:
            return
        _validate_document_truth(engine, manifest)
        next_database_check_at = now + 60.0

    def raw_identity_check(
        document_id: str,
        actual_raw_hash: str | None,
    ) -> None:
        expected_raw_hash = expected_raw_hashes.get(document_id)
        if expected_raw_hash is None:
            raise RuntimeError(
                "replay queue returned a document outside the frozen "
                f"manifest: {document_id}"
            )
        if actual_raw_hash != expected_raw_hash:
            raise RuntimeError(
                "replay raw identity drifted before parse: "
                f"document_id={document_id}, expected={expected_raw_hash}, "
                f"actual={actual_raw_hash}"
            )

    return ExactReplayGuard(
        manifest_sha256=manifest.sha256,
        reset_boundary_at=generation.started_at,
        document_count=len(expected_raw_hashes),
        runtime_check=runtime_check,
        database_check=database_check,
        raw_identity_check=raw_identity_check,
    )


def _validate_bundle_paths(
    *,
    data_root: Path,
    manifest_path: Path,
    reset_receipt: Path | None,
) -> None:
    members = [manifest_path.with_suffix(manifest_path.suffix + ".sha256")]
    if reset_receipt is not None:
        members.extend(
            (
                reset_receipt,
                reset_receipt.with_suffix(reset_receipt.suffix + ".sha256"),
            )
        )
    validate_reset_bundle_paths(data_root, manifest_path, *members)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reparse_corpus", description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--export-manifest", type=Path)
    action.add_argument("--verify", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--run", action="store_true")
    action.add_argument("--purge-trash", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--reset-receipt", type=Path)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser


def purge_reset_trash(
    manifest_sha256: str,
    *,
    data_root: Path,
    status_exit_code: int,
    confirmed: bool,
    forced: bool = False,
) -> int:
    """Delete the reset rollback tree once the new generation is proven.

    The trash tree is the only file-side rollback material for a derived
    reset, so deletion is gated on the same machine truth as ``--status``:
    anything short of a complete, invariant-clean replay refuses. ``forced``
    skips that gate for the operator who is deliberately abandoning the
    rollback path (e.g. giving up on the frozen generation or reclaiming
    disk under pressure); it still requires explicit confirmation.
    """

    if status_exit_code != 0 and not forced:
        print(
            "[refuse] replay is not a verified complete generation "
            f"(status exit {status_exit_code}); audit/reset-trash is the "
            "only rollback material and stays until --status exits 0 "
            "(--force overrides deliberately)",
            file=sys.stderr,
        )
        return status_exit_code
    trash_root = (
        data_root
        / "audit"
        / "reset-trash"
        / manifest_sha256.removeprefix("sha256:")
    )
    lexical_root = data_root.absolute()
    relative = trash_root.absolute().relative_to(lexical_root)
    if len(relative.parts) != 3 or relative.parts[:2] != (
        "audit",
        "reset-trash",
    ):
        raise ManifestError(f"unexpected reset-trash layout: {trash_root}")
    current = lexical_root
    for part in relative.parts:
        if current.is_symlink():
            raise ManifestError(
                f"reset-trash path traverses a symlink: {trash_root}"
            )
        current = current / part
    if current.is_symlink():
        raise ManifestError(f"reset-trash root is a symlink: {trash_root}")
    if not trash_root.exists():
        print(f"[purge] nothing to delete: {trash_root} is already absent")
        return 0
    total_bytes = 0
    file_count = 0
    for path in trash_root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            total_bytes += path.stat().st_size
            file_count += 1
    size_mib = total_bytes / 1_048_576
    mode = "force-" if forced else ""
    if not confirmed:
        print(
            f"[{mode}dry-run] would delete {trash_root}: {file_count} files, "
            f"{size_mib:.1f} MiB; rerun with --yes to delete"
        )
        return 0
    if forced:
        print(
            "[warn] deleting rollback material without generation "
            "verification; the pre-reset derived state becomes unrecoverable",
            file=sys.stderr,
        )
    shutil.rmtree(trash_root)
    print(
        f"[{mode}purged] {trash_root}: {file_count} files, "
        f"{size_mib:.1f} MiB freed"
    )
    return 0


def _run_export(
    *,
    output: Path,
    settings: Settings,
) -> int:
    lock_database_url = worker_database_url(settings)
    engine = create_db_engine(migration_database_url(settings))
    try:
        # The frozen manifest must not race the nightly GC deleters, which
        # hold this same corpus-mutation lock; acquire it before the worker
        # singleton in the same order as reset_derived_corpus.
        with ExitStack() as stack:
            stack.enter_context(exclusive_corpus_mutation(engine))
            lock_connection = stack.enter_context(
                worker_singleton_lock(lock_database_url)
            )
            with engine.connect() as operation_connection:
                assert_same_database_identity(
                    lock_connection,
                    operation_connection,
                )
            deps = build_worker_dependencies(
                settings,
                engine,
                admission_guard=lambda: assert_worker_singleton_or_cancel(
                    lock_connection
                ),
            )
            try:
                _export_manifest(
                    engine=engine,
                    settings=settings,
                    deps=deps,
                    output=output,
                )
                return 0
            finally:
                deps.close_source()
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.export_manifest is not None:
        if any(
            value
            for value in (
                args.manifest,
                args.verify,
                args.status,
                args.run,
                args.reset_receipt,
            )
        ):
            parser.error("--export-manifest accepts no other action arguments")
        return _run_export(
            output=args.export_manifest,
            settings=load_settings(),
        )

    if args.manifest is None:
        parser.error("--manifest is required")
    if args.verify and args.reset_receipt is not None:
        parser.error("--verify does not accept reset receipt arguments")
    if (args.status or args.run) and args.reset_receipt is None:
        parser.error("--status/--run require --reset-receipt")
    if args.purge_trash and not args.force and args.reset_receipt is None:
        parser.error("--purge-trash requires --reset-receipt (or --force)")
    if args.force and not args.purge_trash:
        parser.error("--force only applies to --purge-trash")
    if args.yes and not args.purge_trash:
        parser.error("--yes only applies to --purge-trash")

    settings = load_settings()
    data_root = Path(settings.disclosure_data_root)
    _validate_bundle_paths(
        data_root=data_root,
        manifest_path=args.manifest,
        reset_receipt=args.reset_receipt,
    )
    manifest = load_manifest(
        args.manifest,
        data_root=data_root,
        verify_raw_files=args.verify,
    )
    if args.verify:
        # --status/--run must survive hotfixes during the weeks-long replay
        # window; their per-round admission is bound by target_identity, so
        # the exact frozen code snapshot is only asserted on explicit verify
        # (and by the reset/backup/prove stages inside the freeze window).
        validate_code_snapshot(manifest)
    if args.purge_trash and args.force:
        return purge_reset_trash(
            manifest.sha256,
            data_root=data_root,
            status_exit_code=0,
            confirmed=args.yes,
            forced=True,
        )

    engine = create_db_engine(migration_database_url(settings))
    deps: WorkerDeps | None = None
    generation: ReparseGeneration | None = None
    replay_guard: ExactReplayGuard | None = None
    try:
        if args.run:
            with worker_singleton_lock(
                worker_database_url(settings)
            ) as lock_connection:
                with engine.connect() as operation_connection:
                    assert_same_database_identity(
                        lock_connection,
                        operation_connection,
                    )
                assert_worker_singleton_or_cancel(lock_connection)
        _validate_document_truth(engine, manifest)
        if args.status or args.run or args.purge_trash:
            generation = _load_reparse_generation(
                engine,
                manifest,
                reset_receipt=args.reset_receipt,
            )
            _assert_migration_head(engine)
        if args.verify:
            deps = build_worker_dependencies(settings, engine)
            _validate_runtime_identity(manifest, deps, settings)
            print(
                f"[verified] manifest={manifest.sha256} "
                f"documents={len(manifest.documents)}"
            )
            return 0
        if args.status or args.purge_trash:
            assert generation is not None
            deps = build_worker_dependencies(settings, engine)
            _validate_runtime_identity(manifest, deps, settings)
            _assert_generation_run_identity(
                engine,
                manifest,
                generation_started_at=generation.started_at,
            )
            status = _replay_status(
                engine,
                manifest,
                generation_started_at=generation.started_at,
            )
            status["reset_receipt_sha256"] = generation.receipt_sha256
            print(json.dumps(status, ensure_ascii=False, sort_keys=True))
            status_code = _status_exit_code(status)
            if args.status:
                return status_code
            return purge_reset_trash(
                manifest.sha256,
                data_root=data_root,
                status_exit_code=status_code,
                confirmed=args.yes,
            )
        if not args.run:
            raise AssertionError("argparse requires one action")
        assert generation is not None
        _assert_generation_run_identity(
            engine,
            manifest,
            generation_started_at=generation.started_at,
        )
        replay_guard = _exact_replay_guard(
            manifest,
            generation,
            settings,
        )
        print(
            f"[replay-boundary] receipt={generation.receipt_sha256} "
            f"database={generation.database_identity['database_name']} "
            f"reset_boundary_at={generation.started_at.isoformat()}"
        )

    finally:
        if deps is not None:
            deps.close_source()
        engine.dispose()
    assert replay_guard is not None
    try:
        return run_resident_worker(
            settings,
            exact_replay_guard=replay_guard,
        )
    except WorkerSingletonGuardError as exc:
        raise ManifestError(str(exc)) from exc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ManifestError as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
