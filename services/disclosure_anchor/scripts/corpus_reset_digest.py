"""Exact, closed-scope PostgreSQL state digests for a corpus reset.

PostgreSQL serializes the data: rows never become Python values.  The digest
binds a PK-ordered binary COPY stream to its live catalog descriptor and
server encoding identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Any, Literal

from psycopg import Connection, sql
from psycopg.rows import tuple_row

from disclosure_anchor.adapters.db.postgres.migration_state import (
    migration_ancestry,
)

DESCRIPTOR_SCHEMA = "corpus-reset-postgres-state-descriptor.v1"
STATE_MATRIX_SCHEMA = "corpus-reset-postgres-state-matrix.v2"
_DIGEST_DOMAIN = b"disclosure-anchor/corpus-reset-postgres-state/v1\0"
_CORE = "disclosure_core"
_OPS = "disclosure_ops"
_PUBLIC = "disclosure_public"
DERIVED_OUTBOX_PREDICATE = (
    "(subject_kind IN ('processing_run', 'document_unit') "
    "OR processing_run_id IS NOT NULL OR asset_id IS NOT NULL)"
)


class ResetDigestError(RuntimeError):
    """The closed reset state cannot be proved."""


class ResetDigestScope(StrEnum):
    COMPANY = "company"
    COMPANY_IDENTIFIER = "company_identifier"
    SECURITY = "security"
    TRACKED_COMPANY = "tracked_company"
    SOURCE_ACCESS = "source_access"
    SOURCE_CHECKPOINT = "source_checkpoint"
    PROVIDER_CATEGORY = "provider_category"
    CLASSIFICATION_RULE = "classification_rule"
    DOCUMENT_FULL_PRE = "document_full_pre"
    DOCUMENT_PRESERVED = "document_preserved"
    PROCESSING_RUN = "processing_run"
    DOCUMENT_UNIT = "document_unit"
    UNIT_SEARCH_PROJECTION = "unit_search_projection"
    UNIT_BODY_SEARCH_WINDOW = "unit_body_search_window"
    UNIT_SEARCH_ATOM = "unit_search_atom"
    SOURCE_OUTBOX = "source_outbox"
    DERIVED_OUTBOX = "derived_outbox"
    ALEMBIC_VERSION = "alembic_version"
    OUTBOX_EVENT_SEQUENCE = "outbox_event_sequence"


class ResetScopeClass(StrEnum):
    """How one digest scope participates in the reset boundary."""

    PRESERVE = "preserve"
    MUTATE = "mutate"
    RESET = "reset"


class ResetAction(StrEnum):
    """The sole database action associated with a scope."""

    NONE = "none"
    UPDATE = "update"
    DELETE = "delete"
    TRUNCATE = "truncate"


@dataclass(frozen=True)
class ResetZeroProbe:
    """One post-reset row-count proof, optionally aggregated by key."""

    key: str
    predicate: str | None = None


@dataclass(frozen=True)
class ResetScopeSpec:
    """One typed source of truth for digest, catalog, lock and reset scope."""

    scope: ResetDigestScope
    schema: str
    relation: str
    reset_class: ResetScopeClass
    action: ResetAction = ResetAction.NONE
    kind: Literal["table", "sequence"] = "table"
    predicate: str = "TRUE"
    exclude: tuple[str, ...] = ()
    project: tuple[str, ...] | None = None
    introduction_revision: str | None = None
    lock_order: int | None = None
    mutation_order: int | None = None
    truncate_order: int | None = None
    update_assignments: str | None = None
    zero_probes: tuple[ResetZeroProbe, ...] = ()
    catalog_objects: tuple[tuple[str, str, str], ...] = ()
    routines: tuple[tuple[str, str, str], ...] = ()

    @property
    def relkind(self) -> str:
        return "S" if self.kind == "sequence" else "r"

    @property
    def qualified_relation(self) -> str:
        return f"{self.schema}.{self.relation}"


def _preserved_core_table(
    scope: ResetDigestScope,
    relation: str,
    lock_order: int,
) -> ResetScopeSpec:
    return ResetScopeSpec(
        scope,
        _CORE,
        relation,
        ResetScopeClass.PRESERVE,
        lock_order=lock_order,
    )


def _truncated_core_table(
    scope: ResetDigestScope,
    relation: str,
    *,
    lock_order: int,
    truncate_order: int,
    zero_key: str,
) -> ResetScopeSpec:
    return ResetScopeSpec(
        scope,
        _CORE,
        relation,
        ResetScopeClass.RESET,
        action=ResetAction.TRUNCATE,
        lock_order=lock_order,
        truncate_order=truncate_order,
        zero_probes=(ResetZeroProbe(zero_key),),
    )


RESET_SCOPE_MATRIX = (
    _preserved_core_table(ResetDigestScope.COMPANY, "company", 10),
    _preserved_core_table(
        ResetDigestScope.COMPANY_IDENTIFIER,
        "company_identifier",
        20,
    ),
    _preserved_core_table(ResetDigestScope.SECURITY, "security", 30),
    _preserved_core_table(
        ResetDigestScope.TRACKED_COMPANY,
        "tracked_company",
        40,
    ),
    _preserved_core_table(
        ResetDigestScope.SOURCE_ACCESS,
        "source_access",
        50,
    ),
    _preserved_core_table(
        ResetDigestScope.SOURCE_CHECKPOINT,
        "source_checkpoint",
        60,
    ),
    _preserved_core_table(
        ResetDigestScope.PROVIDER_CATEGORY,
        "provider_category",
        70,
    ),
    _preserved_core_table(
        ResetDigestScope.CLASSIFICATION_RULE,
        "classification_rule",
        80,
    ),
    ResetScopeSpec(
        ResetDigestScope.DOCUMENT_FULL_PRE,
        _CORE,
        "document",
        ResetScopeClass.MUTATE,
        action=ResetAction.UPDATE,
        lock_order=90,
        mutation_order=10,
        update_assignments=(
            "current_processing_run_id = NULL, status = 'registered'"
        ),
        zero_probes=(
            ResetZeroProbe(
                "current_pointers",
                "current_processing_run_id IS NOT NULL",
            ),
            ResetZeroProbe("non_registered", "status <> 'registered'"),
        ),
    ),
    ResetScopeSpec(
        ResetDigestScope.DOCUMENT_PRESERVED,
        _CORE,
        "document",
        ResetScopeClass.PRESERVE,
        exclude=("status", "current_processing_run_id"),
        lock_order=90,
    ),
    _truncated_core_table(
        ResetDigestScope.PROCESSING_RUN,
        "processing_run",
        lock_order=100,
        truncate_order=50,
        zero_key="runs",
    ),
    _truncated_core_table(
        ResetDigestScope.DOCUMENT_UNIT,
        "document_unit",
        lock_order=110,
        truncate_order=40,
        zero_key="units",
    ),
    _truncated_core_table(
        ResetDigestScope.UNIT_SEARCH_PROJECTION,
        "unit_search_projection",
        lock_order=120,
        truncate_order=30,
        zero_key="projections",
    ),
    ResetScopeSpec(
        introduction_revision="0028_safe_search_windows",
        scope=ResetDigestScope.UNIT_BODY_SEARCH_WINDOW,
        schema=_CORE,
        relation="unit_body_search_window",
        reset_class=ResetScopeClass.RESET,
        action=ResetAction.TRUNCATE,
        lock_order=130,
        truncate_order=20,
        zero_probes=(ResetZeroProbe("projections"),),
        catalog_objects=(
            (_CORE, "unit_body_search_window", "r"),
            (_CORE, "ix_unit_body_search_window_tsv", "i"),
            (_PUBLIC, "unit_body_search_windows_v1", "v"),
        ),
        routines=(
            (
                _CORE,
                "search_tsvector_is_safe",
                # PostgreSQL identity_arguments includes parameter names for
                # named-arg functions; a bare type list never matches on 18.x.
                "title_tokens text, path_tokens text, "
                "body_tokens text, key_tokens text",
            ),
        ),
    ),
    ResetScopeSpec(
        introduction_revision="0030_source_bound_search_atoms",
        scope=ResetDigestScope.UNIT_SEARCH_ATOM,
        schema=_CORE,
        relation="unit_search_atom",
        reset_class=ResetScopeClass.RESET,
        action=ResetAction.TRUNCATE,
        lock_order=140,
        truncate_order=10,
        zero_probes=(ResetZeroProbe("projections"),),
        catalog_objects=(
            (_CORE, "unit_search_atom", "r"),
            (_CORE, "ix_unit_search_atom_text_trgm", "i"),
            (_PUBLIC, "unit_search_atoms_v1", "v"),
        ),
    ),
    ResetScopeSpec(
        ResetDigestScope.SOURCE_OUTBOX,
        _OPS,
        "outbox_event",
        ResetScopeClass.PRESERVE,
        predicate=f"{DERIVED_OUTBOX_PREDICATE} IS NOT TRUE",
        lock_order=150,
    ),
    ResetScopeSpec(
        ResetDigestScope.DERIVED_OUTBOX,
        _OPS,
        "outbox_event",
        ResetScopeClass.RESET,
        action=ResetAction.DELETE,
        predicate=DERIVED_OUTBOX_PREDICATE,
        lock_order=150,
        mutation_order=20,
        zero_probes=(
            ResetZeroProbe("derived_events"),
        ),
    ),
    ResetScopeSpec(
        ResetDigestScope.ALEMBIC_VERSION,
        _OPS,
        "alembic_version",
        ResetScopeClass.PRESERVE,
        lock_order=160,
    ),
    ResetScopeSpec(
        ResetDigestScope.OUTBOX_EVENT_SEQUENCE,
        _OPS,
        "outbox_event_seq_seq",
        ResetScopeClass.PRESERVE,
        kind="sequence",
        project=("last_value", "is_called"),
    ),
)


def validate_reset_scope_contract(
    matrix: tuple[ResetScopeSpec, ...] = RESET_SCOPE_MATRIX,
) -> None:
    """Reject incomplete or internally inconsistent reset scope definitions."""

    scopes = tuple(spec.scope for spec in matrix)
    if len(scopes) != len(set(scopes)) or set(scopes) != set(ResetDigestScope):
        raise ResetDigestError(
            "reset scope matrix must classify every digest scope exactly once"
        )
    physical: dict[
        tuple[str, str, str],
        tuple[int | None, str | None],
    ] = {}
    lock_orders: dict[int, tuple[str, str, str]] = {}
    mutation_orders: set[int] = set()
    truncation_orders: set[int] = set()
    for spec in matrix:
        relation_key = (spec.schema, spec.relation, spec.relkind)
        relation_state = physical.setdefault(
            relation_key,
            (spec.lock_order, spec.introduction_revision),
        )
        if relation_state != (spec.lock_order, spec.introduction_revision):
            raise ResetDigestError(
                f"{spec.qualified_relation} has inconsistent lifecycle metadata"
            )
        if spec.kind == "table" and spec.lock_order is None:
            raise ResetDigestError(
                f"table scope lacks lock order: {spec.scope.value}"
            )
        if spec.lock_order is not None:
            prior_relation = lock_orders.setdefault(
                spec.lock_order,
                relation_key,
            )
            if prior_relation != relation_key:
                raise ResetDigestError("lock order must be unique per relation")
        if spec.reset_class is ResetScopeClass.PRESERVE:
            valid = (
                spec.action is ResetAction.NONE
                and spec.mutation_order is None
                and spec.truncate_order is None
                and spec.update_assignments is None
                and not spec.zero_probes
            )
        elif spec.reset_class is ResetScopeClass.MUTATE:
            valid = (
                spec.action is ResetAction.UPDATE
                and spec.mutation_order is not None
                and spec.truncate_order is None
                and bool(spec.update_assignments)
                and bool(spec.zero_probes)
            )
        else:
            valid = (
                spec.action in {ResetAction.DELETE, ResetAction.TRUNCATE}
                and bool(spec.zero_probes)
                and spec.update_assignments is None
                and (
                    (
                        spec.action is ResetAction.DELETE
                        and spec.mutation_order is not None
                        and spec.truncate_order is None
                    )
                    or (
                        spec.action is ResetAction.TRUNCATE
                        and spec.mutation_order is None
                        and spec.truncate_order is not None
                    )
                )
            )
        if not valid:
            raise ResetDigestError(
                f"incomplete reset policy for scope {spec.scope.value}"
            )
        if spec.mutation_order is not None:
            if spec.mutation_order in mutation_orders:
                raise ResetDigestError("mutation order must be unique")
            mutation_orders.add(spec.mutation_order)
        if spec.action is ResetAction.TRUNCATE:
            assert spec.truncate_order is not None
            if spec.truncate_order in truncation_orders:
                raise ResetDigestError("truncate order must be unique")
            truncation_orders.add(spec.truncate_order)
        if any(
            not probe.key
            or (
                probe.predicate is not None
                and not probe.predicate
            )
            for probe in spec.zero_probes
        ):
            raise ResetDigestError(
                f"invalid zero-state probe for scope {spec.scope.value}"
            )


validate_reset_scope_contract()
_SCOPES = {spec.scope: spec for spec in RESET_SCOPE_MATRIX}
# Production freezes the manifest at 0027, then resets while that schema is
# still live.  Only after the empty-state proof may migrations advance through
# the explicit 0031 empty-processing-run barrier.
PRE_RESET_SCOPES = tuple(
    spec.scope
    for spec in RESET_SCOPE_MATRIX
    if spec.introduction_revision is None
)
ALL_PRE_RESET_SCOPES = tuple(spec.scope for spec in RESET_SCOPE_MATRIX)
POST_RESET_PRESERVED_SCOPES = tuple(
    spec.scope
    for spec in RESET_SCOPE_MATRIX
    if spec.reset_class is ResetScopeClass.PRESERVE
)
RESET_MUTATED_SCOPES = tuple(
    spec.scope
    for spec in RESET_SCOPE_MATRIX
    if spec.reset_class is not ResetScopeClass.PRESERVE
)
RESET_ZERO_STATE_KEYS = frozenset(
    probe.key
    for spec in RESET_SCOPE_MATRIX
    for probe in spec.zero_probes
)


@dataclass(frozen=True)
class RelationStateDigest:
    descriptor: dict[str, object]
    descriptor_sha256: str
    state_sha256: str
    copy_byte_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "descriptor": self.descriptor,
            "descriptor_sha256": self.descriptor_sha256,
            "state_sha256": self.state_sha256,
            "copy_byte_count": self.copy_byte_count,
        }


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


_SERVER_QUERY = """
SELECT current_setting('server_version'),
       current_setting('server_version_num')::integer,
       current_setting('server_encoding'),
       current_setting('client_encoding')
"""
_RELATION_QUERY = """
SELECT c.relkind, c.relpersistence
  FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = %s AND c.relname = %s
"""
_COLUMN_QUERY = """
SELECT a.attnum, a.attname,
       pg_catalog.format_type(a.atttypid, a.atttypmod),
       tn.nspname, t.typname, a.attnotnull, a.attidentity, a.attgenerated,
       pg_catalog.pg_get_expr(ad.adbin, ad.adrelid),
       cn.nspname, co.collname
  FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
  JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
  JOIN pg_catalog.pg_namespace tn ON tn.oid = t.typnamespace
  LEFT JOIN pg_catalog.pg_attrdef ad
    ON ad.adrelid = c.oid AND ad.adnum = a.attnum
  LEFT JOIN pg_catalog.pg_collation co ON co.oid = a.attcollation
  LEFT JOIN pg_catalog.pg_namespace cn ON cn.oid = co.collnamespace
 WHERE n.nspname = %s AND c.relname = %s
   AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum
"""
_PRIMARY_KEY_QUERY = """
SELECT a.attname
  FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_catalog.pg_index i
    ON i.indrelid = c.oid AND i.indisprimary AND i.indisvalid
  JOIN LATERAL unnest(i.indkey)
    WITH ORDINALITY AS key_column(attnum, key_ordinal) ON TRUE
  JOIN pg_catalog.pg_attribute a
    ON a.attrelid = c.oid AND a.attnum = key_column.attnum
 WHERE n.nspname = %s AND c.relname = %s
 ORDER BY key_column.key_ordinal
"""
_SEQUENCE_QUERY = """
SELECT pg_catalog.format_type(s.seqtypid, NULL), s.seqstart, s.seqincrement,
       s.seqmax, s.seqmin, s.seqcache, s.seqcycle
  FROM pg_catalog.pg_sequence s
  JOIN pg_catalog.pg_class c ON c.oid = s.seqrelid
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = %s AND c.relname = %s
"""
_CATALOG_QUERY = """
SELECT n.nspname, c.relname, c.relkind
  FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p', 'S', 'm', 'f')
 ORDER BY n.nspname, c.relname
"""
_ALEMBIC_REVISION_QUERY = f"""
SELECT version_num
  FROM {_OPS}.alembic_version
 ORDER BY version_num
"""
_FEATURE_OBJECT_QUERY = """
SELECT n.nspname, c.relname, c.relkind
  FROM pg_catalog.pg_class c
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = ANY(%s) AND c.relname = ANY(%s)
 ORDER BY n.nspname, c.relname, c.relkind
"""
_FEATURE_ROUTINE_QUERY = """
SELECT n.nspname, p.proname,
       pg_catalog.pg_get_function_identity_arguments(p.oid)
  FROM pg_catalog.pg_proc p
  JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = ANY(%s) AND p.proname = ANY(%s)
 ORDER BY n.nspname, p.proname,
          pg_catalog.pg_get_function_identity_arguments(p.oid)
"""


def _resolve(
    scope: ResetDigestScope | str,
) -> tuple[ResetDigestScope, ResetScopeSpec]:
    try:
        key = ResetDigestScope(scope)
    except ValueError as exc:
        raise ResetDigestError(f"unclassified reset digest scope: {scope!r}") from exc
    return key, _SCOPES[key]


def _column_descriptor(
    row: tuple[Any, ...],
    *,
    logical_ordinal: int,
) -> dict[str, object]:
    return {
        # pg_dump intentionally does not recreate dropped-column attnum gaps.
        # Bind the surviving logical order, which is what COPY projects.
        "ordinal": logical_ordinal,
        "name": str(row[1]),
        "formatted_type": str(row[2]),
        "type_schema": str(row[3]),
        "type_name": str(row[4]),
        "not_null": bool(row[5]),
        "identity": str(row[6]),
        "generated": str(row[7]),
        "default_expression": str(row[8]) if row[8] is not None else None,
        "collation_schema": str(row[9]) if row[9] is not None else None,
        "collation_name": str(row[10]) if row[10] is not None else None,
    }


def _projection(
    key: ResetDigestScope,
    scope: ResetScopeSpec,
    columns: list[dict[str, object]],
) -> tuple[str, ...]:
    actual = tuple(str(column["name"]) for column in columns)
    if not actual or len(actual) != len(set(actual)):
        raise ResetDigestError(f"invalid live columns for {key.value}")
    if scope.project is not None:
        missing = sorted(set(scope.project) - set(actual))
        projected = scope.project
    else:
        missing = sorted(set(scope.exclude) - set(actual))
        projected = tuple(name for name in actual if name not in scope.exclude)
    if missing:
        raise ResetDigestError(f"{key.value} catalog columns are missing: {missing}")
    if not projected:
        raise ResetDigestError(f"{key.value} has an empty projection")
    return projected


def _describe(
    connection: Connection[Any],
    key: ResetDigestScope,
    scope: ResetScopeSpec,
) -> tuple[dict[str, object], tuple[str, ...], tuple[str, ...]]:
    parameters = (scope.schema, scope.relation)
    with connection.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(_SERVER_QUERY)
        server = cursor.fetchone()
        if server is None:
            raise ResetDigestError("PostgreSQL server identity is unavailable")
        cursor.execute(_RELATION_QUERY, parameters)
        relation = cursor.fetchone()
        if relation is None:
            raise ResetDigestError(
                f"classified relation is missing: {scope.schema}.{scope.relation}"
            )
        if str(relation[0]) != scope.relkind:
            raise ResetDigestError(
                f"{scope.schema}.{scope.relation} has relkind {relation[0]!r}, "
                f"expected {scope.relkind!r}"
            )
        cursor.execute(_COLUMN_QUERY, parameters)
        columns = [
            _column_descriptor(row, logical_ordinal=ordinal)
            for ordinal, row in enumerate(cursor.fetchall(), start=1)
        ]
        projected = _projection(key, scope, columns)

        primary_key: tuple[str, ...] = ()
        sequence_settings: dict[str, object] | None = None
        if scope.kind == "table":
            cursor.execute(_PRIMARY_KEY_QUERY, parameters)
            primary_key = tuple(str(row[0]) for row in cursor.fetchall())
            if not primary_key:
                raise ResetDigestError(
                    f"{scope.schema}.{scope.relation} lacks a valid primary key"
                )
            if not set(primary_key).issubset(projected):
                raise ResetDigestError(f"{key.value} excludes a primary-key column")
        else:
            cursor.execute(_SEQUENCE_QUERY, parameters)
            sequence = cursor.fetchone()
            if sequence is None:
                raise ResetDigestError(
                    f"sequence settings missing: {scope.schema}.{scope.relation}"
                )
            sequence_settings = dict(
                zip(
                    (
                        "data_type",
                        "start_value",
                        "increment_by",
                        "max_value",
                        "min_value",
                        "cache_size",
                        "cycle",
                    ),
                    (
                        str(sequence[0]),
                        *(int(value) for value in sequence[1:6]),
                        bool(sequence[6]),
                    ),
                    strict=True,
                )
            )

    ordering: dict[str, object] = {"kind": "singleton_sequence"}
    if scope.kind == "table":
        ordering = {"kind": "primary_key", "columns": list(primary_key)}
    descriptor: dict[str, object] = {
        "descriptor_schema": DESCRIPTOR_SCHEMA,
        "scope": key.value,
        "object": {
            "schema_name": scope.schema,
            "relation_name": scope.relation,
            "relation_kind": scope.kind,
            "postgres_relkind": str(relation[0]),
            "persistence": str(relation[1]),
        },
        "columns": columns,
        "projection_columns": list(projected),
        "predicate": scope.predicate,
        "ordering": ordering,
        "copy_format": "binary",
        "server": {
            "version": str(server[0]),
            "version_num": int(server[1]),
            "server_encoding": str(server[2]),
            "client_encoding": str(server[3]),
        },
        "sequence_settings": sequence_settings,
    }
    return descriptor, projected, primary_key


def _copy_query(
    scope: ResetScopeSpec,
    projected: tuple[str, ...],
    primary_key: tuple[str, ...],
) -> sql.Composed:
    query = sql.SQL("COPY (SELECT {fields} FROM {relation}").format(
        fields=sql.SQL(", ").join(map(sql.Identifier, projected)),
        relation=sql.Identifier(scope.schema, scope.relation),
    )
    if scope.kind == "table":
        query += sql.SQL(" WHERE {predicate} ORDER BY {key}").format(
            predicate=sql.SQL(scope.predicate),
            key=sql.SQL(", ").join(map(sql.Identifier, primary_key)),
        )
    return query + sql.SQL(") TO STDOUT (FORMAT BINARY)")


def digest_scope(
    connection: Connection[Any],
    scope: ResetDigestScope | str,
) -> RelationStateDigest:
    """Hash one raw binary COPY stream without materializing its rows."""

    key, definition = _resolve(scope)
    descriptor, projected, primary_key = _describe(connection, key, definition)
    descriptor_bytes = json.dumps(
        descriptor,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    state = hashlib.sha256()
    state.update(_DIGEST_DOMAIN)
    state.update(len(descriptor_bytes).to_bytes(8, "big"))
    state.update(descriptor_bytes)
    byte_count = 0
    with connection.cursor(row_factory=tuple_row) as cursor:
        with cursor.copy(
            _copy_query(definition, projected, primary_key)
        ) as copy:
            for chunk in copy:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise ResetDigestError("binary COPY yielded non-binary data")
                state.update(chunk)
                byte_count += len(chunk)
    return RelationStateDigest(
        descriptor=descriptor,
        descriptor_sha256=(
            f"sha256:{hashlib.sha256(descriptor_bytes).hexdigest()}"
        ),
        state_sha256=f"sha256:{state.hexdigest()}",
        copy_byte_count=byte_count,
    )


def database_migration_revision(connection: Connection[Any]) -> str:
    with connection.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(_ALEMBIC_REVISION_QUERY)
        rows = cursor.fetchall()
    if (
        len(rows) != 1
        or len(rows[0]) != 1
        or not isinstance(rows[0][0], str)
        or not rows[0][0]
    ):
        raise ResetDigestError(
            "database must have exactly one nonblank Alembic revision"
        )
    return str(rows[0][0])


def scopes_for_migration_revision(
    revision: str,
) -> tuple[ResetDigestScope, ...]:
    return tuple(
        spec.scope
        for spec in scope_specs_for_migration_revision(revision)
    )


def scope_specs_for_migration_revision(
    revision: str,
) -> tuple[ResetScopeSpec, ...]:
    """Resolve the one closed scope matrix against Alembic ancestry."""

    try:
        ancestry = migration_ancestry(revision)
    except RuntimeError as exc:
        raise ResetDigestError(
            f"database Alembic revision is unsupported: {revision!r}"
        ) from exc
    return tuple(
        spec
        for spec in RESET_SCOPE_MATRIX
        if (
            spec.introduction_revision is None
            or spec.introduction_revision in ancestry
        )
    )


def reset_lock_plan_for_migration_revision(
    revision: str,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return deterministic SHARE then ACCESS EXCLUSIVE relation groups."""

    relations: dict[tuple[str, str], tuple[int, bool]] = {}
    for spec in scope_specs_for_migration_revision(revision):
        if spec.lock_order is None:
            continue
        relation = (spec.schema, spec.relation)
        current = relations.get(relation)
        mutated = spec.action is not ResetAction.NONE
        if current is None:
            relations[relation] = (spec.lock_order, mutated)
        else:
            relations[relation] = (
                current[0],
                current[1] or mutated,
            )
    groups: list[tuple[str, tuple[str, ...]]] = []
    for mode, mutated in (
        ("SHARE", False),
        ("ACCESS EXCLUSIVE", True),
    ):
        names = tuple(
            f"{schema}.{relation}"
            for (schema, relation), (order, is_mutated) in sorted(
                relations.items(),
                key=lambda item: item[1][0],
            )
            if is_mutated is mutated
        )
        if names:
            groups.append((mode, names))
    return tuple(groups)


def reset_mutation_specs_for_migration_revision(
    revision: str,
) -> tuple[ResetScopeSpec, ...]:
    """Return non-TRUNCATE mutation scopes in their required order."""

    return tuple(
        sorted(
            (
                spec
                for spec in scope_specs_for_migration_revision(revision)
                if spec.action in {ResetAction.UPDATE, ResetAction.DELETE}
            ),
            key=lambda spec: (
                spec.mutation_order
                if spec.mutation_order is not None
                else -1
            ),
        )
    )


def reset_truncate_relations_for_migration_revision(
    revision: str,
) -> tuple[str, ...]:
    """Return every revision-present reset table in dependency-safe order."""

    return tuple(
        spec.qualified_relation
        for spec in sorted(
            (
                spec
                for spec in scope_specs_for_migration_revision(revision)
                if spec.action is ResetAction.TRUNCATE
            ),
            key=lambda spec: (
                spec.truncate_order
                if spec.truncate_order is not None
                else -1
            ),
        )
    )


def reset_zero_probes_for_migration_revision(
    revision: str,
) -> tuple[tuple[ResetScopeSpec, ResetZeroProbe], ...]:
    """Return the exhaustive post-reset probes for all changed scopes."""

    return tuple(
        (spec, probe)
        for spec in scope_specs_for_migration_revision(revision)
        for probe in spec.zero_probes
    )


def assert_service_catalog_classified(
    connection: Connection[Any],
    *,
    revision: str | None = None,
) -> None:
    """Reject any state relation inconsistent with the migration lineage."""

    expected_scopes = scopes_for_migration_revision(
        (
            revision
            if revision is not None
            else database_migration_revision(connection)
        )
    )
    expected = {
        (
            _SCOPES[key].schema,
            _SCOPES[key].relation,
            _SCOPES[key].relkind,
        )
        for key in expected_scopes
    }
    with connection.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(_CATALOG_QUERY, ([_CORE, _OPS],))
        actual = {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in cursor.fetchall()
        }
    if actual != expected:
        raise ResetDigestError(
            "service state catalog is not exactly classified: "
            f"unexpected={sorted(actual - expected)}, "
            f"missing={sorted(expected - actual)}"
        )


def assert_migration_feature_catalog(
    connection: Connection[Any],
    *,
    revision: str,
) -> None:
    """Bind feature tables, indexes, views, and routines to Alembic ancestry."""

    try:
        ancestry = migration_ancestry(revision)
    except RuntimeError as exc:
        raise ResetDigestError(
            f"database Alembic revision is unsupported: {revision!r}"
        ) from exc
    all_objects = {
        item
        for spec in RESET_SCOPE_MATRIX
        for item in spec.catalog_objects
    }
    expected_objects = {
        item
        for spec in RESET_SCOPE_MATRIX
        if (
            spec.introduction_revision is None
            or spec.introduction_revision in ancestry
        )
        for item in spec.catalog_objects
    }
    all_routines = {
        item
        for spec in RESET_SCOPE_MATRIX
        for item in spec.routines
    }
    expected_routines = {
        item
        for spec in RESET_SCOPE_MATRIX
        if (
            spec.introduction_revision is None
            or spec.introduction_revision in ancestry
        )
        for item in spec.routines
    }
    with connection.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(
            _FEATURE_OBJECT_QUERY,
            (
                sorted({schema for schema, _name, _kind in all_objects}),
                sorted({name for _schema, name, _kind in all_objects}),
            ),
        )
        actual_objects = {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in cursor.fetchall()
            if (str(row[0]), str(row[1]), str(row[2])) in all_objects
        }
        if all_routines:
            cursor.execute(
                _FEATURE_ROUTINE_QUERY,
                (
                    sorted(
                        {schema for schema, _name, _args in all_routines}
                    ),
                    sorted({_name for _schema, _name, _args in all_routines}),
                ),
            )
            actual_routines = {
                (str(row[0]), str(row[1]), str(row[2]))
                for row in cursor.fetchall()
                if (str(row[0]), str(row[1]), str(row[2]))
                in all_routines
            }
        else:
            actual_routines = set()
    if actual_objects != expected_objects or actual_routines != expected_routines:
        raise ResetDigestError(
            "migration feature catalog disagrees with Alembic ancestry: "
            f"missing_objects={sorted(expected_objects - actual_objects)}, "
            f"unexpected_objects={sorted(actual_objects - expected_objects)}, "
            f"missing_routines={sorted(expected_routines - actual_routines)}, "
            f"unexpected_routines={sorted(actual_routines - expected_routines)}"
        )


def capture_state_matrix(
    connection: Connection[Any],
    *,
    scopes: tuple[ResetDigestScope, ...] | None = None,
    classify_catalog: bool = True,
) -> dict[str, object]:
    """Capture a canonical exact-state matrix in one caller-owned snapshot."""

    migration_revision = database_migration_revision(connection)
    migration_scopes = scopes_for_migration_revision(migration_revision)
    resolved_scopes = scopes or migration_scopes
    if not resolved_scopes or len(resolved_scopes) != len(set(resolved_scopes)):
        raise ResetDigestError("reset digest scopes must be non-empty and unique")
    if classify_catalog:
        if resolved_scopes != migration_scopes:
            raise ResetDigestError(
                "classified reset scopes disagree with Alembic ancestry"
            )
        assert_service_catalog_classified(
            connection,
            revision=migration_revision,
        )
        assert_migration_feature_catalog(
            connection,
            revision=migration_revision,
        )
    return {
        "schema": STATE_MATRIX_SCHEMA,
        "migration_revision": migration_revision,
        "scopes": {
            scope.value: digest_scope(connection, scope).as_dict()
            for scope in resolved_scopes
        },
    }


def validate_state_matrix(
    value: object,
    *,
    expected_scopes: tuple[ResetDigestScope, ...] | None = None,
) -> dict[str, object]:
    """Validate a closed matrix without trusting caller-provided descriptors."""

    if not isinstance(value, dict) or set(value) != {
        "schema",
        "migration_revision",
        "scopes",
    }:
        raise ResetDigestError("PostgreSQL state matrix has an invalid shape")
    if value.get("schema") != STATE_MATRIX_SCHEMA:
        raise ResetDigestError("PostgreSQL state matrix schema mismatch")
    migration_revision = value.get("migration_revision")
    if not isinstance(migration_revision, str) or not migration_revision:
        raise ResetDigestError(
            "PostgreSQL state matrix lacks migration revision"
        )
    migration_scopes = scopes_for_migration_revision(migration_revision)
    scopes = value.get("scopes")
    if not isinstance(scopes, dict):
        raise ResetDigestError("PostgreSQL state matrix lacks scopes")
    if expected_scopes is None:
        expected_scopes = migration_scopes
    expected_names = {scope.value for scope in expected_scopes}
    if set(scopes) != expected_names:
        raise ResetDigestError(
            "PostgreSQL state matrix scope coverage mismatch: "
            f"unexpected={sorted(set(scopes) - expected_names)}, "
            f"missing={sorted(expected_names - set(scopes))}"
        )

    server_identity: object = None
    descriptor_fields = {
        "descriptor_schema", "scope", "object", "columns",
        "projection_columns", "predicate", "ordering", "copy_format",
        "server", "sequence_settings",
    }
    object_fields = {
        "schema_name", "relation_name", "relation_kind",
        "postgres_relkind", "persistence",
    }
    server_fields = {
        "version", "version_num", "server_encoding", "client_encoding",
    }
    for scope in expected_scopes:
        record = scopes.get(scope.value)
        if not isinstance(record, dict) or set(record) != {
            "descriptor", "descriptor_sha256", "state_sha256",
            "copy_byte_count",
        }:
            raise ResetDigestError(
                f"PostgreSQL state scope {scope.value} has an invalid shape"
            )
        descriptor = record.get("descriptor")
        definition = _SCOPES[scope]
        if not isinstance(descriptor, dict) or set(descriptor) != descriptor_fields:
            raise ResetDigestError(
                f"PostgreSQL state scope {scope.value} has an invalid descriptor"
            )
        object_identity = descriptor.get("object")
        server = descriptor.get("server")
        columns = descriptor.get("columns")
        projection = descriptor.get("projection_columns")
        ordering = descriptor.get("ordering")
        if (
            descriptor.get("descriptor_schema") != DESCRIPTOR_SCHEMA
            or descriptor.get("scope") != scope.value
            or descriptor.get("copy_format") != "binary"
            or descriptor.get("predicate") != definition.predicate
            or not isinstance(object_identity, dict)
            or set(object_identity) != object_fields
            or (
                object_identity.get("schema_name"),
                object_identity.get("relation_name"),
                object_identity.get("relation_kind"),
                object_identity.get("postgres_relkind"),
            )
            != (
                definition.schema,
                definition.relation,
                definition.kind,
                definition.relkind,
            )
            or not isinstance(server, dict)
            or set(server) != server_fields
            or not isinstance(columns, list)
            or not columns
            or any(
                not isinstance(column, dict)
                or not isinstance(column.get("name"), str)
                or not isinstance(column.get("ordinal"), int)
                for column in columns
            )
            or not isinstance(projection, list)
            or not projection
            or not isinstance(ordering, dict)
        ):
            raise ResetDigestError(
                f"PostgreSQL state scope {scope.value} descriptor identity mismatch"
            )
        names = [str(column["name"]) for column in columns]
        expected_projection = (
            list(definition.project)
            if definition.project is not None
            else [name for name in names if name not in definition.exclude]
        )
        expected_ordering = (
            {"kind": "singleton_sequence"}
            if definition.kind == "sequence"
            else {"kind": "primary_key", "columns": ordering.get("columns")}
        )
        if (
            projection != expected_projection
            or not set(definition.exclude).issubset(names)
            or ordering != expected_ordering
            or (
                definition.kind == "sequence"
                and not isinstance(descriptor.get("sequence_settings"), dict)
            )
            or (
                definition.kind == "table"
                and (
                    descriptor.get("sequence_settings") is not None
                    or not isinstance(ordering.get("columns"), list)
                    or not ordering["columns"]
                    or not set(ordering["columns"]).issubset(projection)
                )
            )
        ):
            raise ResetDigestError(
                f"PostgreSQL state scope {scope.value} projection mismatch"
            )
        if server_identity is None:
            server_identity = server
        elif server != server_identity:
            raise ResetDigestError(
                "PostgreSQL state scopes have inconsistent server identities"
            )
        descriptor_bytes = json.dumps(
            descriptor, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if (
            record.get("descriptor_sha256")
            != f"sha256:{hashlib.sha256(descriptor_bytes).hexdigest()}"
            or not _is_sha256(record.get("state_sha256"))
            or not isinstance(record.get("copy_byte_count"), int)
            or isinstance(record.get("copy_byte_count"), bool)
            or int(record["copy_byte_count"]) < 0
        ):
            raise ResetDigestError(
                f"PostgreSQL state scope {scope.value} digest fields are invalid"
            )
    return value
