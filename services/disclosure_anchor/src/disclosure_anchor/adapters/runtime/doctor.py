"""Runtime doctor and startup preflight checks."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from disclosure_anchor.adapters.db.postgres.connection import create_db_engine
from disclosure_anchor.adapters.db.postgres.connection import uses_reader_database_url_fallback
from disclosure_anchor.adapters.db.postgres.catalog import view_names
from disclosure_anchor.adapters.db.postgres.migration_state import single_migration_head
from disclosure_anchor.adapters.db.postgres.schema import (
    ALEMBIC_VERSION_TABLE,
    ALEMBIC_VERSION_TABLE_SCHEMA,
    ALL_ROLES,
    ALL_SCHEMAS,
    APP_ROLE,
    CORE_SCHEMA,
    DATABASE_NAME,
    OPS_SCHEMA,
    PUBLIC_SCHEMA,
    PUBLIC_VIEWS,
    READ_ONLY_PUBLIC_ROLES,
)
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.raw_document_store import RawDocumentStore
from disclosure_anchor.domain.services.unit_hashing import content_hash_aggregate
from disclosure_anchor.settings import Settings


PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
_PYTHON_STRIP_CHARS_SQL = (
    r"U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F"
    r"\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005"
    r"\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000'"
)
_CANONICAL_CODE_SQL = f"btrim(security_code, {_PYTHON_STRIP_CHARS_SQL})"
_CANONICAL_EXCHANGE_SQL = f"upper(btrim(exchange, {_PYTHON_STRIP_CHARS_SQL}))"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    message: str

    @property
    def ok(self) -> bool:
        return self.status != FAIL


@dataclass(frozen=True)
class DoctorReport:
    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)


def _pass(name: str, message: str) -> CheckResult:
    return CheckResult(name=name, status=PASS, message=message)


def _warn(name: str, message: str) -> CheckResult:
    return CheckResult(name=name, status=WARN, message=message)


def _fail(name: str, message: str) -> CheckResult:
    return CheckResult(name=name, status=FAIL, message=message)


def _is_writable_dir(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _check_path_exists(name: str, path: Path) -> CheckResult:
    if path.exists():
        return _pass(name, str(path))
    return _fail(name, f"missing: {path}")


def _check_writable_dir(name: str, path: Path) -> CheckResult:
    if _is_writable_dir(path):
        return _pass(name, str(path))
    return _fail(name, f"not writable directory: {path}")


def _check_under_root(name: str, path: Path, root: Path) -> CheckResult:
    if _is_relative_to(path, root):
        return _pass(name, str(path))
    return _fail(name, f"{path} is not under {root}")


def _nearest_existing_parent(path: Path) -> Path | None:
    current = path
    while not current.exists():
        if current.parent == current:
            return None
        current = current.parent
    return current


def _check_same_filesystem(name: str, left: Path, right: Path) -> CheckResult:
    left_existing = _nearest_existing_parent(left)
    right_existing = _nearest_existing_parent(right)
    if left_existing is None or right_existing is None:
        return _fail(name, f"cannot stat existing parents: {left} / {right}")
    left_dev = left_existing.stat().st_dev
    right_dev = right_existing.stat().st_dev
    if left_dev == right_dev:
        return _pass(name, f"{left} and {right} share filesystem device {left_dev}")
    return _fail(
        name,
        f"{left} and {right} are on different filesystem devices: {left_dev} != {right_dev}",
    )


def _environment_checks(settings: Settings) -> list[CheckResult]:
    checks: list[CheckResult] = [
        _check_path_exists("agent_system_root", settings.agent_system_root),
        _check_path_exists("mount sentinel", settings.sentinel_path),
        _check_writable_dir("DISCLOSURE_DATA_ROOT", settings.disclosure_data_root),
        _check_writable_dir("DISCLOSURE_SHARED_ROOT", settings.disclosure_shared_root),
        _check_writable_dir("DISCLOSURE_RUNTIME_ROOT", settings.disclosure_runtime_root),
    ]
    cache_names = ("MINERU_MODEL_CACHE", "HF_HOME", "MODELSCOPE_CACHE")
    checks.extend(
        _check_under_root(name, path, settings.disclosure_shared_root)
        for name, path in zip(cache_names, settings.model_cache_paths)
    )
    checks.append(
        _check_same_filesystem(
            "raw archive filesystem",
            settings.disclosure_runtime_root / "tmp",
            settings.disclosure_data_root / "data" / "raw_documents",
        )
    )
    return checks


def _mineru_orphan_check() -> CheckResult:
    """WARN when MinerU processes linger (08 §2: killed runs must not leak).

    The observed leak is the fast_api backend MinerU 3.4 spawns; a resident
    one holds ~1.8GB and steals compute from the next parse.
    """

    try:
        result = subprocess.run(
            ["pgrep", "-fl", "mineru"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return _warn("mineru orphans", "pgrep unavailable; cannot check")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if lines:
        return _warn("mineru orphans", f"count={len(lines)} (residual mineru processes)")
    return _pass("mineru orphans", "none")


def _reader_database_url_checks(settings: Settings) -> list[CheckResult]:
    if uses_reader_database_url_fallback(settings):
        return [
            _warn(
                "DISCLOSURE_READER_DATABASE_URL",
                "missing; read API will use DATABASE_URL fallback",
            )
        ]
    if settings.disclosure_reader_database_url is not None:
        return [_pass("DISCLOSURE_READER_DATABASE_URL", "configured")]
    return []


def run_startup_preflight(
    settings: Settings, *, engine: Engine | None = None
) -> DoctorReport:
    """Run only fast checks suitable for API startup."""

    checks = _environment_checks(settings)
    checks.extend(_reader_database_url_checks(settings))
    if settings.database_url is None:
        checks.append(_fail("DATABASE_URL", "missing for API startup"))
        return DoctorReport(results=tuple(checks))

    owns_engine = engine is None
    if engine is None:
        engine = create_db_engine(settings.database_url.get_secret_value())
    try:
        checks.extend(_database_ping_and_migration_checks(engine))
    except Exception as exc:
        checks.append(_fail("pg preflight", str(exc)))
    finally:
        if owns_engine:
            engine.dispose()
    return DoctorReport(results=tuple(checks))


def run_doctor(
    settings: Settings, *, full: bool = False, sample_size: int = 20
) -> DoctorReport:
    """Run CLI doctor checks without creating or repairing external state."""

    checks = _environment_checks(settings)
    checks.extend(_reader_database_url_checks(settings))
    checks.append(_mineru_orphan_check())
    if settings.database_url is None:
        checks.append(_warn("DATABASE_URL", "missing; DB-backed doctor checks skipped"))
        return DoctorReport(results=tuple(checks))

    engine = create_db_engine(settings.database_url.get_secret_value())
    try:
        checks.extend(_database_ping_and_migration_checks(engine))
        checks.extend(_database_catalog_checks(engine))
        checks.extend(_classification_coverage_checks(engine))
        checks.extend(_database_consistency_checks(settings, engine))
        checks.extend(run_raw_archive_checks(settings, engine, full=full, sample_size=sample_size))
        checks.extend(_processing_run_checks(settings, engine))
        checks.extend(_document_unit_locator_checks(settings, engine, sample_size=sample_size))
        checks.extend(_orphan_file_checks(settings, engine))
    except Exception as exc:
        checks.append(_fail("database doctor checks", str(exc)))
    finally:
        engine.dispose()
    return DoctorReport(results=tuple(checks))


def _database_ping_and_migration_checks(engine: Engine) -> list[CheckResult]:
    checks: list[CheckResult] = []
    with engine.connect() as conn:
        current_database = conn.execute(text("SELECT current_database()")).scalar_one()
        if current_database == DATABASE_NAME:
            checks.append(_pass("pg connection", f"database={current_database}"))
        else:
            checks.append(
                _fail(
                    "pg connection",
                    f"expected database={DATABASE_NAME}, got {current_database}",
                )
            )
        current_revision = conn.execute(
            text(
                f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE_SCHEMA}."
                f"{ALEMBIC_VERSION_TABLE}"
            )
        ).scalar_one_or_none()
    expected_revision = single_migration_head()
    if current_revision == expected_revision:
        checks.append(_pass("migration head", str(current_revision)))
    else:
        checks.append(
            _fail(
                "migration head",
                f"expected {expected_revision}, got {current_revision}",
            )
        )
    checks.append(_classification_rules_check(engine))
    return checks


def _classification_rules_check(engine: Engine) -> CheckResult:
    """Loaded rules must match the repo vocabulary versions (0016 discipline)."""

    from disclosure_anchor.adapters.sources.cninfo.mapper import (
        load_class_map,
        load_facet_map,
        load_filing_type_rule_bundle,
    )

    bundle = load_filing_type_rule_bundle()
    expected = {
        "class": str(load_class_map()["version"]),
        "facet": str(load_facet_map()["version"]),
        "title": bundle.version,
    }
    if bundle.topic_rules:
        expected["title_topic"] = bundle.version
    if bundle.noise_rules:
        expected["title_noise"] = bundle.version
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT rule_set, string_agg(DISTINCT version, ',') AS versions "
                "FROM disclosure_core.classification_rule GROUP BY rule_set"
            )
        ).all()
    loaded = {str(row.rule_set): str(row.versions) for row in rows}
    if not loaded:
        return _fail("classification rules", "table empty — run `make load-rules`")
    mismatches = [
        f"{rule_set}: loaded={loaded.get(rule_set, '∅')} file={version}"
        for rule_set, version in expected.items()
        if loaded.get(rule_set) != version
    ]
    if mismatches:
        return _fail(
            "classification rules",
            "; ".join(mismatches) + " — run `make load-rules`",
        )
    return _pass(
        "classification rules",
        f"class={expected['class']} facet={expected['facet']} title={expected['title']}",
    )


def _classification_coverage_checks(engine: Engine) -> list[CheckResult]:
    """Audit candidate-boundary F006V gaps and persisted override integrity."""

    from disclosure_anchor.adapters.sources.cninfo.classification_coverage import (
        unmapped_code_counts,
    )
    from disclosure_anchor.adapters.sources.cninfo.mapper import (
        load_class_map,
        load_facet_map,
    )
    from disclosure_anchor.application.worker.queries import candidate_code_counts

    class_map = load_class_map()
    class_prefixes = [
        str(prefix)
        for spec in class_map["classes"].values()
        for prefix in spec["prefixes"]
    ]
    facet_prefixes = [
        str(prefix)
        for rule in load_facet_map()["rules"]
        for prefix in rule["prefixes"]
    ]
    known_classes = set(class_map["classes"])
    with engine.connect() as conn:
        counts = candidate_code_counts(conn)
        gaps = unmapped_code_counts(
            counts,
            class_prefixes=class_prefixes,
            facet_prefixes=facet_prefixes,
        )
        override_rows = conn.execute(
            text(
                f"SELECT tracked_company_id, process_classes "
                f"FROM {CORE_SCHEMA}.tracked_company "
                "WHERE process_classes IS NOT NULL"
            )
        ).all()

    if gaps:
        top = ", ".join(
            f"{code}={count}"
            for code, count in sorted(
                gaps.items(), key=lambda item: (-item[1], item[0])
            )[:10]
        )
        coverage = _warn(
            "candidate F006V coverage",
            f"unmapped={sum(gaps.values())} candidate-code occurrences; {top}; "
            "run `scripts/audit_unmapped_codes.py`",
        )
    else:
        coverage = _pass(
            "candidate F006V coverage",
            f"{sum(counts.values())} coded candidate segments covered",
        )

    invalid_overrides = _invalid_process_class_overrides(
        override_rows, known_classes=known_classes
    )
    overrides = (
        _warn("process_classes overrides", "; ".join(invalid_overrides[:10]))
        if invalid_overrides
        else _pass(
            "process_classes overrides",
            "all non-empty overrides use known class names; [] inherits",
        )
    )
    return [coverage, overrides]


def _invalid_process_class_overrides(
    override_rows: Iterable[object], *, known_classes: set[str]
) -> list[str]:
    invalid_overrides: list[str] = []
    for row in override_rows:
        tracked_company_id = getattr(row, "tracked_company_id")
        values = getattr(row, "process_classes")
        if not isinstance(values, list):
            invalid_overrides.append(f"{tracked_company_id}:non-array")
            continue
        invalid_shapes = sorted(
            type(value).__name__ for value in values if not isinstance(value, str)
        )
        unknown = sorted(
            value
            for value in values
            if isinstance(value, str) and value not in known_classes
        )
        if invalid_shapes:
            invalid_overrides.append(
                f"{tracked_company_id}:non-string={','.join(invalid_shapes)}"
            )
        if unknown:
            invalid_overrides.append(
                f"{tracked_company_id}:unknown={','.join(unknown)}"
            )
    return invalid_overrides


def _database_catalog_checks(engine: Engine) -> list[CheckResult]:
    checks: list[CheckResult] = []
    with engine.connect() as conn:
        schemas = {
            str(row[0])
            for row in conn.execute(
                text("SELECT schema_name FROM information_schema.schemata")
            )
        }
        roles = {str(row[0]) for row in conn.execute(text("SELECT rolname FROM pg_roles"))}

        missing_schemas = set(ALL_SCHEMAS) - schemas
        checks.append(
            _pass("schema presence", ", ".join(ALL_SCHEMAS))
            if not missing_schemas
            else _fail("schema presence", f"missing: {sorted(missing_schemas)}")
        )

        missing_roles = set(ALL_ROLES) - roles
        checks.append(
            _pass("role presence", ", ".join(ALL_ROLES))
            if not missing_roles
            else _fail("role presence", f"missing: {sorted(missing_roles)}")
        )

        actual_public_views = set(view_names(conn, schema=PUBLIC_SCHEMA))
        expected_public_views = set(PUBLIC_VIEWS)
        missing_views = expected_public_views - actual_public_views
        unexpected_views = actual_public_views - expected_public_views
        checks.append(
            _pass(
                "public view catalog",
                ", ".join(sorted(actual_public_views)),
            )
            if not missing_views and not unexpected_views
            else _fail(
                "public view catalog",
                f"missing={sorted(missing_views)} unexpected={sorted(unexpected_views)}",
            )
        )

        permission_failures: list[str] = []
        if APP_ROLE in roles:
            for schema in (CORE_SCHEMA, OPS_SCHEMA, PUBLIC_SCHEMA):
                if not conn.execute(
                    text("SELECT has_schema_privilege(:role, :schema, 'USAGE')"),
                    {"role": APP_ROLE, "schema": schema},
                ).scalar_one():
                    permission_failures.append(f"{APP_ROLE}:{schema}:USAGE")
        for role in (candidate for candidate in READ_ONLY_PUBLIC_ROLES if candidate in roles):
            if not conn.execute(
                text("SELECT has_schema_privilege(:role, :schema, 'USAGE')"),
                {"role": role, "schema": PUBLIC_SCHEMA},
            ).scalar_one():
                permission_failures.append(f"{role}:{PUBLIC_SCHEMA}:USAGE")
        for role in (
            candidate
            for candidate in (APP_ROLE, *READ_ONLY_PUBLIC_ROLES)
            if candidate in roles
        ):
            for view_name in actual_public_views:
                if not conn.execute(
                    text("SELECT has_table_privilege(:role, :rel, 'SELECT')"),
                    {
                        "role": role,
                        "rel": f"{PUBLIC_SCHEMA}.{view_name}",
                    },
                ).scalar_one():
                    permission_failures.append(
                        f"{role}:{PUBLIC_SCHEMA}.{view_name}:SELECT"
                    )
        checks.append(
            _pass("role permissions", "schema usage and public view select ok")
            if not permission_failures
            else _fail("role permissions", "; ".join(permission_failures))
        )
    return checks


def _database_consistency_checks(settings: Settings, engine: Engine) -> list[CheckResult]:
    checks: list[CheckResult] = []
    with engine.connect() as conn:
        duplicate_active = conn.execute(
            text(
                f"SELECT document_id, count(*) FROM {CORE_SCHEMA}.processing_run "
                "WHERE is_active GROUP BY document_id HAVING count(*) > 1"
            )
        ).all()
        checks.append(
            _pass("active run uniqueness", "each document has at most one active run")
            if not duplicate_active
            else _fail("active run uniqueness", f"duplicates={len(duplicate_active)}")
        )

        duplicate_security_keys = conn.execute(
            text(
                f"SELECT {_CANONICAL_CODE_SQL}, {_CANONICAL_EXCHANGE_SQL}, count(*) "
                f"FROM {CORE_SCHEMA}.security "
                f"GROUP BY {_CANONICAL_CODE_SQL}, {_CANONICAL_EXCHANGE_SQL} "
                "HAVING count(*) > 1"
            )
        ).all()
        identity_shape = conn.execute(
            text(
                f"SELECT count(*) FILTER (WHERE security_code <> {_CANONICAL_CODE_SQL} "
                f"OR exchange <> {_CANONICAL_EXCHANGE_SQL}) AS noncanonical, "
                "count(*) FILTER (WHERE exchange IN ('SSE','SZSE','BSE') AND NOT ("
                "security_code ~ '^[0-9]{6}$' AND CASE "
                "WHEN security_code LIKE '92%' OR security_code LIKE '4%' "
                "  OR security_code LIKE '8%' THEN exchange = 'BSE' "
                "WHEN security_code LIKE '6%' OR security_code LIKE '9%' "
                "  THEN exchange = 'SSE' "
                "WHEN security_code LIKE '0%' OR security_code LIKE '2%' "
                "  OR security_code LIKE '3%' THEN exchange = 'SZSE' "
                "ELSE false END)) AS mainland_mismatch "
                f"FROM {CORE_SCHEMA}.security"
            )
        ).mappings().one()
        identity_problem = bool(
            duplicate_security_keys
            or identity_shape["noncanonical"]
            or identity_shape["mainland_mismatch"]
        )
        checks.append(
            _pass(
                "canonical security identity",
                "no aliases, normalized duplicates, or mainland exchange mismatches",
            )
            if not identity_problem
            else _fail(
                "canonical security identity",
                f"normalized duplicates={len(duplicate_security_keys)}; "
                f"noncanonical={identity_shape['noncanonical']}; "
                f"mainland_mismatch={identity_shape['mainland_mismatch']}",
            )
        )

        contested_identifiers = int(
            conn.execute(
                text(
                    f"SELECT count(*) FROM {CORE_SCHEMA}.company_identifier "
                    "WHERE status = 'contested'"
                )
            ).scalar_one()
        )
        checks.append(
            _pass("contested company identifiers", "none")
            if contested_identifiers == 0
            else _warn(
                "contested company identifiers",
                f"count={contested_identifiers}; resolve before bulk intake",
            )
        )

        seq_stats = conn.execute(
            text(
                f"SELECT count(*) AS event_count, min(seq) AS min_seq, max(seq) AS max_seq "
                f"FROM {OPS_SCHEMA}.outbox_event"
            )
        ).mappings().one()
        if seq_stats["event_count"] == 0:
            checks.append(_pass("outbox seq", "no outbox events"))
        elif seq_stats["max_seq"] - seq_stats["min_seq"] + 1 == seq_stats["event_count"]:
            checks.append(_pass("outbox seq", "monotonic without gaps"))
        else:
            checks.append(
                _warn(
                    "outbox seq",
                    (
                        f"gap detected: min={seq_stats['min_seq']} "
                        f"max={seq_stats['max_seq']} count={seq_stats['event_count']}"
                    ),
                )
            )

        stale_rows = conn.execute(
            text(
                f"SELECT processing_run_id FROM {CORE_SCHEMA}.processing_run "
                "WHERE status = 'running' AND started_at IS NOT NULL "
                "AND started_at < now() - make_interval(secs => :seconds)"
            ),
            {"seconds": settings.disclosure_parse_timeout_seconds},
        ).all()
        checks.append(
            _pass("stale running runs", "none")
            if not stale_rows
            else _warn("stale running runs", f"count={len(stale_rows)}")
        )

        empty_publish = int(
            conn.execute(
                text(
                    f"SELECT count(DISTINCT q.document_id) "
                    f"FROM {OPS_SCHEMA}.pending_publish_v1 q "
                    f"WHERE NOT EXISTS (SELECT 1 FROM {CORE_SCHEMA}.document_unit u "
                    "WHERE u.processing_run_id = q.processing_run_id)"
                )
            ).scalar_one()
        )
        checks.append(
            _pass("empty publish dead letters", "none")
            if empty_publish == 0
            else _warn(
                "empty publish dead letters",
                f"count={empty_publish}; automatic publish excludes them; "
                "review before using --allow-empty",
            )
        )

        oversized = int(
            conn.execute(
                text(
                    f"SELECT count(*) FROM {CORE_SCHEMA}.document "
                    "WHERE COALESCE((provider_metadata->>'oversized')::boolean, false)"
                )
            ).scalar_one()
        )
        checks.append(
            _pass("oversized parse exclusions", "none")
            if oversized == 0
            else _warn(
                "oversized parse exclusions",
                f"count={oversized}; excluded before parse LIMIT",
            )
        )

        exhausted_parse = int(
            conn.execute(
                text(
                    f"SELECT count(*) FROM {OPS_SCHEMA}.pending_parse_v1 "
                    "WHERE failed_parse_count >= :max_retries "
                    "OR last_failed_retryable = false"
                ),
                {"max_retries": settings.disclosure_max_parse_retries},
            ).scalar_one()
        )
        checks.append(
            _pass("parse dead letters", "none")
            if exhausted_parse == 0
            else _warn(
                "parse dead letters",
                f"count={exhausted_parse}; exhausted or non-retryable",
            )
        )
    return checks


def _registered_raw_documents(
    engine: Engine, *, full: bool, sample_size: int
) -> list[tuple[str, str | None, str | None]]:
    query = (
        f"SELECT document_id, raw_file_relpath, raw_file_hash "
        f"FROM {CORE_SCHEMA}.document "
        "WHERE raw_file_relpath IS NOT NULL OR raw_file_hash IS NOT NULL"
    )
    with engine.connect() as conn:
        if full:
            rows = conn.execute(text(query + " ORDER BY raw_file_relpath")).all()
        else:
            first_rows = conn.execute(
                text(query + " ORDER BY raw_file_relpath LIMIT :limit"),
                {"limit": sample_size},
            ).all()
            latest_rows = conn.execute(
                text(query + " ORDER BY created_at DESC, document_id DESC LIMIT :limit"),
                {"limit": sample_size},
            ).all()
            by_document_id = {str(row[0]): row for row in first_rows}
            by_document_id.update({str(row[0]): row for row in latest_rows})
            rows = list(by_document_id.values())
    return [
        (
            str(row[0]),
            str(row[1]) if row[1] is not None else None,
            str(row[2]) if row[2] is not None else None,
        )
        for row in rows
    ]


def run_raw_archive_checks(
    settings: Settings,
    engine: Engine,
    *,
    full: bool = False,
    sample_size: int = 20,
) -> list[CheckResult]:
    """Verify registered raw files without mutating DB or files."""

    store = RawDocumentStore(FileStorePathBuilder(settings))
    results: list[CheckResult] = []
    sample_mode = "full" if full else f"sample={sample_size}"
    for document_id, relpath, expected_hash in _registered_raw_documents(
        engine, full=full, sample_size=sample_size
    ):
        if not relpath or not expected_hash:
            results.append(
                _fail("raw hash", f"document_id={document_id} missing relpath/hash")
            )
            continue

        verification = store.verify_raw_document(
            relpath=Path(relpath), expected_hash=expected_hash
        )
        results.append(
            CheckResult(
                name="raw hash",
                status=PASS if verification.ok else FAIL,
                message=(
                    f"{sample_mode} document_id={document_id} relpath={relpath} "
                    f"message={verification.message}"
                ),
            )
        )

    if not results:
        results.append(_pass("raw hash", f"{sample_mode} no registered raw documents"))
    return results


def _processing_run_checks(settings: Settings, engine: Engine) -> list[CheckResult]:
    checks: list[CheckResult] = []
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT processing_run_id, status, normalized_ir_relpath, "
                f"artifact_hash, error, unit_build_status, document_units_relpath, "
                f"content_hash_aggregate FROM {CORE_SCHEMA}.processing_run "
                "WHERE status IN ('succeeded', 'failed') "
                "OR unit_build_status = 'succeeded'"
            )
        ).mappings().all()

    if not rows:
        return [_pass("processing runs", "no completed or failed runs")]

    for row in rows:
        run_id = row["processing_run_id"]
        if row["status"] == "succeeded":
            checks.append(
                _check_artifact_hash(
                    settings=settings,
                    name="normalized IR artifact",
                    object_id=run_id,
                    relpath=row["normalized_ir_relpath"],
                    expected_hash=row["artifact_hash"],
                )
            )
        elif row["status"] == "failed":
            error = row["error"]
            if isinstance(error, dict) and {"stage", "error_code", "retryable"} <= error.keys():
                checks.append(_pass("failed run error", f"processing_run_id={run_id}"))
            else:
                checks.append(
                    _fail(
                        "failed run error",
                        f"processing_run_id={run_id} lacks structured error",
                    )
                )
        if row["unit_build_status"] == "succeeded":
            checks.append(
                _check_unit_snapshot_aggregate(
                    settings=settings,
                    object_id=run_id,
                    relpath=row["document_units_relpath"],
                    expected_aggregate=row["content_hash_aggregate"],
                )
            )
    return checks


def _check_artifact_hash(
    *,
    settings: Settings,
    name: str,
    object_id: str,
    relpath: str | None,
    expected_hash: str | None,
) -> CheckResult:
    if not relpath:
        return _fail(name, f"{object_id} missing relpath")
    if not expected_hash:
        return _fail(name, f"{object_id} missing expected hash")
    path = settings.disclosure_data_root / "data" / relpath
    if not path.is_file():
        return _fail(name, f"{object_id} missing file: {relpath}")
    actual_hash = _file_hash(path)
    if actual_hash == expected_hash:
        return _pass(name, f"{object_id} hash ok")
    return _fail(name, f"{object_id} hash mismatch: {actual_hash} != {expected_hash}")


def _check_unit_snapshot_aggregate(
    *,
    settings: Settings,
    object_id: str,
    relpath: str | None,
    expected_aggregate: str | None,
) -> CheckResult:
    """Recompute content_hash_aggregate from snapshot rows.

    The aggregate hashes the sorted unit content hashes, not the snapshot
    file's bytes, so a plain file hash can never match it.
    """

    name = "document unit snapshot"
    if not relpath:
        return _fail(name, f"{object_id} missing relpath")
    if not expected_aggregate:
        return _fail(name, f"{object_id} missing expected aggregate")
    path = settings.disclosure_data_root / "data" / relpath
    if not path.is_file():
        return _fail(name, f"{object_id} missing file: {relpath}")
    hashes: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                unit_row = json.loads(line)
                unit_content_hash = unit_row.get("content_hash")
                if not isinstance(unit_content_hash, str):
                    return _fail(
                        name,
                        f"{object_id} snapshot line {line_number} missing content_hash",
                    )
                hashes.append(unit_content_hash)
    except (OSError, json.JSONDecodeError) as exc:
        return _fail(name, f"{object_id} unreadable snapshot: {exc}")
    actual_aggregate = content_hash_aggregate(hashes)
    if actual_aggregate == expected_aggregate:
        return _pass(name, f"{object_id} aggregate ok ({len(hashes)} units)")
    return _fail(
        name,
        f"{object_id} aggregate mismatch: {actual_aggregate} != {expected_aggregate}",
    )


def _document_unit_locator_checks(
    settings: Settings, engine: Engine, *, sample_size: int
) -> list[CheckResult]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT asset_id, artifact_locator FROM {CORE_SCHEMA}.document_unit "
                "WHERE artifact_locator IS NOT NULL ORDER BY created_at DESC, asset_id DESC "
                "LIMIT :limit"
            ),
            {"limit": sample_size},
        ).all()
    if not rows:
        return [_pass("document unit artifact locator", "no locators")]

    checks: list[CheckResult] = []
    for asset_id, locator in rows:
        artifact_path = locator.get("artifact_path") if isinstance(locator, dict) else None
        if not artifact_path:
            checks.append(_pass("document unit artifact locator", f"asset_id={asset_id} no path"))
            continue
        path = settings.disclosure_data_root / "data" / str(artifact_path)
        checks.append(
            _pass("document unit artifact locator", f"asset_id={asset_id}")
            if path.exists()
            else _fail(
                "document unit artifact locator",
                f"asset_id={asset_id} missing {artifact_path}",
            )
        )
    return checks


def _orphan_file_checks(settings: Settings, engine: Engine) -> list[CheckResult]:
    data_root = settings.disclosure_data_root / "data"
    raw_root = data_root / "raw_documents"
    artifact_root = data_root / "parser_artifacts"
    with engine.connect() as conn:
        raw_relpaths = {
            str(row[0])
            for row in conn.execute(
                text(
                    f"SELECT raw_file_relpath FROM {CORE_SCHEMA}.document "
                    "WHERE raw_file_relpath IS NOT NULL"
                )
            )
        }
        artifact_relpaths = {
            str(row[0]).rstrip("/")
            for row in conn.execute(
                text(
                    f"SELECT parser_artifact_relpath FROM {CORE_SCHEMA}.processing_run "
                    "WHERE parser_artifact_relpath IS NOT NULL"
                )
            )
        }

    raw_orphans = _orphan_files(raw_root, data_root=data_root, expected_relpaths=raw_relpaths)
    artifact_orphans = _orphan_files(
        artifact_root,
        data_root=data_root,
        expected_relpaths=artifact_relpaths,
        prefix_match=True,
    )
    return [
        _warn("orphan raw files", f"count={len(raw_orphans)}")
        if raw_orphans
        else _pass("orphan raw files", "none"),
        _warn("orphan parser artifacts", f"count={len(artifact_orphans)}")
        if artifact_orphans
        else _pass("orphan parser artifacts", "none"),
    ]


def _orphan_files(
    root: Path,
    *,
    data_root: Path,
    expected_relpaths: set[str],
    prefix_match: bool = False,
) -> list[str]:
    if not root.exists():
        return []
    orphans: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relpath = str(path.relative_to(data_root))
        if prefix_match:
            if not any(
                relpath == expected or relpath.startswith(expected + "/")
                for expected in expected_relpaths
            ):
                orphans.append(relpath)
        elif relpath not in expected_relpaths:
            orphans.append(relpath)
    return orphans


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def render_report(results: Iterable[CheckResult]) -> str:
    return "\n".join(f"[{result.status}] {result.name}: {result.message}" for result in results)
