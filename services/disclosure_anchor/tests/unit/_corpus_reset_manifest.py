"""Small valid v4 PostgreSQL-state fixtures shared by reset unit tests."""

from __future__ import annotations

import hashlib
import json

from scripts.corpus_reset_digest import (
    DESCRIPTOR_SCHEMA,
    DERIVED_OUTBOX_PREDICATE,
    PRE_RESET_SCOPES,
    STATE_MATRIX_SCHEMA,
    ResetDigestScope,
)


_OBJECTS = {
    ResetDigestScope.COMPANY: ("disclosure_core", "company"),
    ResetDigestScope.COMPANY_IDENTIFIER: (
        "disclosure_core",
        "company_identifier",
    ),
    ResetDigestScope.SECURITY: ("disclosure_core", "security"),
    ResetDigestScope.TRACKED_COMPANY: ("disclosure_core", "tracked_company"),
    ResetDigestScope.SOURCE_ACCESS: ("disclosure_core", "source_access"),
    ResetDigestScope.SOURCE_CHECKPOINT: (
        "disclosure_core",
        "source_checkpoint",
    ),
    ResetDigestScope.PROVIDER_CATEGORY: (
        "disclosure_core",
        "provider_category",
    ),
    ResetDigestScope.CLASSIFICATION_RULE: (
        "disclosure_core",
        "classification_rule",
    ),
    ResetDigestScope.DOCUMENT_FULL_PRE: ("disclosure_core", "document"),
    ResetDigestScope.DOCUMENT_PRESERVED: ("disclosure_core", "document"),
    ResetDigestScope.PROCESSING_RUN: ("disclosure_core", "processing_run"),
    ResetDigestScope.DOCUMENT_UNIT: ("disclosure_core", "document_unit"),
    ResetDigestScope.UNIT_SEARCH_PROJECTION: (
        "disclosure_core",
        "unit_search_projection",
    ),
    ResetDigestScope.SOURCE_OUTBOX: ("disclosure_ops", "outbox_event"),
    ResetDigestScope.DERIVED_OUTBOX: ("disclosure_ops", "outbox_event"),
    ResetDigestScope.ALEMBIC_VERSION: ("disclosure_ops", "alembic_version"),
    ResetDigestScope.OUTBOX_EVENT_SEQUENCE: (
        "disclosure_ops",
        "outbox_event_seq_seq",
    ),
}


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def postgres_state_matrix(*, state_seed: bytes = b"state") -> dict[str, object]:
    records: dict[str, object] = {}
    for scope in PRE_RESET_SCOPES:
        schema, relation = _OBJECTS[scope]
        is_sequence = scope is ResetDigestScope.OUTBOX_EVENT_SEQUENCE
        is_document = scope in {
            ResetDigestScope.DOCUMENT_FULL_PRE,
            ResetDigestScope.DOCUMENT_PRESERVED,
        }
        column_names = (
            ["last_value", "log_cnt", "is_called"]
            if is_sequence
            else (
                ["id", "status", "current_processing_run_id"]
                if is_document
                else ["id"]
            )
        )
        projection = (
            ["last_value", "is_called"]
            if is_sequence
            else (
                ["id"]
                if scope is ResetDigestScope.DOCUMENT_PRESERVED
                else column_names
            )
        )
        predicate = "TRUE"
        if scope is ResetDigestScope.SOURCE_OUTBOX:
            predicate = f"{DERIVED_OUTBOX_PREDICATE} IS NOT TRUE"
        elif scope is ResetDigestScope.DERIVED_OUTBOX:
            predicate = DERIVED_OUTBOX_PREDICATE
        descriptor: dict[str, object] = {
            "descriptor_schema": DESCRIPTOR_SCHEMA,
            "scope": scope.value,
            "object": {
                "schema_name": schema,
                "relation_name": relation,
                "relation_kind": "sequence" if is_sequence else "table",
                "postgres_relkind": "S" if is_sequence else "r",
                "persistence": "p",
            },
            "columns": [
                {
                    "ordinal": ordinal,
                    "name": name,
                    "formatted_type": (
                        "boolean" if name == "is_called" else "bigint"
                    ),
                    "type_schema": "pg_catalog",
                    "type_name": "bool" if name == "is_called" else "int8",
                    "not_null": True,
                    "identity": "",
                    "generated": "",
                    "default_expression": None,
                    "collation_schema": None,
                    "collation_name": None,
                }
                for ordinal, name in enumerate(column_names, start=1)
            ],
            "projection_columns": projection,
            "predicate": predicate,
            "ordering": (
                {"kind": "singleton_sequence"}
                if is_sequence
                else {"kind": "primary_key", "columns": ["id"]}
            ),
            "copy_format": "binary",
            "server": {
                "version": "18.4",
                "version_num": 180004,
                "server_encoding": "UTF8",
                "client_encoding": "UTF8",
            },
            "sequence_settings": (
                {
                    "data_type": "bigint",
                    "start_value": 1,
                    "increment_by": 1,
                    "max_value": 9_223_372_036_854_775_807,
                    "min_value": 1,
                    "cache_size": 1,
                    "cycle": False,
                }
                if is_sequence
                else None
            ),
        }
        descriptor_bytes = json.dumps(
            descriptor,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        records[scope.value] = {
            "descriptor": descriptor,
            "descriptor_sha256": _sha256(descriptor_bytes),
            "state_sha256": _sha256(state_seed + scope.value.encode()),
            "copy_byte_count": 21,
        }
    return {
        "schema": STATE_MATRIX_SCHEMA,
        "migration_revision": "0027_materialized_classification",
        "scopes": records,
    }
