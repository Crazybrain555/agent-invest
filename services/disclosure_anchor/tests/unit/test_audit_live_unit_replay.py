from __future__ import annotations

from argparse import Namespace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

import scripts.audit_live_unit_replay as audit_script
from scripts.audit_live_unit_replay import (
    audit_rows,
    canonical_unit_row,
    replay_provider_scope,
)


_CONTENT_HASH = "sha256:" + "a" * 64
_QUERY_HASH = "sha256:" + "b" * 64


class AuditLiveUnitReplayTests(unittest.TestCase):
    def test_exact_rows_have_equal_falsifiable_aggregates(self) -> None:
        replay = [_row()]
        live = [_row()]

        result = audit_rows(replay_rows=replay, live_rows=live)

        self.assertTrue(result["passed"])
        self.assertEqual(
            result["replay_aggregate_sha256"], result["live_aggregate_sha256"]
        )
        self.assertEqual(result["mismatch_count"], 0)
        self.assertEqual(
            result["compared_fields"][-3:],
            ["query_projection_hash", "body_status", "applicability"],
        )

    def test_changed_route_is_named_and_changes_aggregate(self) -> None:
        replay = [_row()]
        live = [_row(semantic_keys=["revenue_and_cost"])]

        result = audit_rows(replay_rows=replay, live_rows=live)

        self.assertFalse(result["passed"])
        self.assertNotEqual(
            result["replay_aggregate_sha256"], result["live_aggregate_sha256"]
        )
        self.assertEqual(result["mismatch_count"], 1)
        self.assertEqual(
            result["mismatches"][0]["differing_fields"],
            ["semantic_keys"],
        )

    def test_changed_applicability_is_named_and_changes_aggregate(self) -> None:
        result = audit_rows(
            replay_rows=[_row(applicability="applicable")],
            live_rows=[_row(applicability=None)],
        )

        self.assertFalse(result["passed"])
        self.assertNotEqual(
            result["replay_aggregate_sha256"], result["live_aggregate_sha256"]
        )
        self.assertEqual(
            result["mismatches"][0]["differing_fields"],
            ["applicability"],
        )

    def test_invalid_applicability_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "applicability is unsupported"):
            canonical_unit_row(
                _row(applicability="unknown"),
                label="row",
                source="replay",
            )

    def test_nullable_public_route_arrays_equal_replay_empty_arrays(self) -> None:
        result = audit_rows(
            replay_rows=[_row(section_keys=[])],
            live_rows=[_row(semantic_keys=None, section_keys=None)],
        )

        self.assertTrue(result["passed"])
        self.assertEqual(
            set(result["normalizations"]), {"semantic_keys", "section_keys"}
        )

    def test_missing_and_unexpected_identities_fail_closed(self) -> None:
        result = audit_rows(
            replay_rows=[_row(unit_index=0)],
            live_rows=[_row(unit_index=1)],
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["missing"], [["pid", 0]])
        self.assertEqual(result["unexpected"], [["pid", 1]])

    def test_replay_provider_scope_is_sorted_hash_bound_and_complete(self) -> None:
        scope = replay_provider_scope(
            [
                _row(provider_document_id="pid-b", unit_index=0),
                _row(provider_document_id="pid-a", unit_index=1),
                _row(provider_document_id="pid-a", unit_index=0),
            ]
        )

        self.assertEqual(
            scope["provider_document_ids"],
            ["pid-a", "pid-b"],
        )
        self.assertEqual(scope["provider_document_count"], 2)
        self.assertEqual(
            scope["provider_document_ids_sha256"],
            "sha256:"
            + hashlib.sha256(b'["pid-a","pid-b"]').hexdigest(),
        )

    def test_replay_provider_scope_rejects_partial_document_indices(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "contiguous unit_index values from zero",
        ):
            replay_provider_scope([_row(unit_index=0), _row(unit_index=2)])

    def test_duplicate_identity_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "repeats Unit identity"):
            audit_rows(replay_rows=[_row(), _row()], live_rows=[_row()])

    def test_malformed_hash_is_rejected(self) -> None:
        for malformed in ("not-a-hash", "sha256:" + "z" * 64):
            with self.subTest(malformed=malformed):
                with self.assertRaisesRegex(ValueError, "canonical SHA-256"):
                    canonical_unit_row(
                        _row(content_hash=malformed),
                        label="row",
                        source="replay",
                    )

    def test_replay_missing_or_null_route_array_is_rejected(self) -> None:
        missing = _row()
        missing.pop("semantic_keys")
        for replay in (missing, _row(semantic_keys=None)):
            with self.subTest(replay=replay):
                with self.assertRaisesRegex(
                    ValueError,
                    "missing compared fields|must be an array",
                ):
                    audit_rows(
                        replay_rows=[replay],
                        live_rows=[_row(semantic_keys=None)],
                    )

    def test_non_string_body_status_is_rejected_as_contract_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "body_status is unsupported"):
            canonical_unit_row(
                _row(body_status=["content"]),
                label="row",
                source="replay",
            )

    def test_boolean_replay_row_count_is_rejected(self) -> None:
        replay_payload = {
            "contract_version": "semantic_route_model_eval.v1",
            "row_count": True,
            "rows": [_row()],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            replay_path = Path(temporary_directory) / "replay.json"
            replay_path.write_text(json.dumps(replay_payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "replay row_count drifted"):
                audit_script.load_replay(replay_path)

    def test_receipt_hash_binds_bytes_loaded_for_comparison(self) -> None:
        replay_payload = {
            "contract_version": "semantic_route_model_eval.v1",
            "evaluation_id": "test",
            "taxonomy_version": "taxonomy.v1",
            "router_version": "router.v1",
            "row_count": 1,
            "rows": [_row()],
        }
        replay_bytes = (
            json.dumps(replay_payload, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        replacement_bytes = b'{"different":"replay-b"}\n'
        live_metadata = {
            "public_view": "disclosure_public.document_units_v1",
            "active_only": True,
            "transaction_snapshot": "1:2:",
            "contract_versions": ["document_unit.v1"],
            "processing_run_ids": ["run"],
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            replay_path = Path(temporary_directory) / "replay.json"
            output_path = Path(temporary_directory) / "receipt.json"
            replay_path.write_bytes(replay_bytes)

            requested_provider_scopes: list[list[str]] = []

            def replace_replay_after_load(
                provider_document_ids: list[str],
            ) -> tuple[
                list[dict[str, object]], dict[str, object]
            ]:
                requested_provider_scopes.append(provider_document_ids)
                replay_path.write_bytes(replacement_bytes)
                return [_row()], live_metadata

            arguments = Namespace(
                replay=replay_path,
                output=output_path,
                source_revision="test-revision",
            )
            with (
                patch.object(audit_script, "_parser") as parser,
                patch.object(
                    audit_script,
                    "_live_rows",
                    side_effect=replace_replay_after_load,
                ),
            ):
                parser.return_value.parse_args.return_value = arguments
                self.assertEqual(audit_script.main(), 0)

            receipt = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertTrue(receipt["comparison"]["passed"])
        self.assertEqual(receipt["contract_version"], "live_unit_replay_audit.v2")
        self.assertEqual(requested_provider_scopes, [["pid"]])
        self.assertEqual(
            receipt["provider_scope"]["provider_document_ids"],
            ["pid"],
        )
        self.assertEqual(
            receipt["provider_scope"]["provider_document_ids_sha256"],
            "sha256:" + hashlib.sha256(b'["pid"]').hexdigest(),
        )
        self.assertEqual(
            receipt["source_replay"]["sha256"],
            "sha256:" + hashlib.sha256(replay_bytes).hexdigest(),
        )
        self.assertNotEqual(
            receipt["source_replay"]["sha256"],
            "sha256:" + hashlib.sha256(replacement_bytes).hexdigest(),
        )

    def test_live_query_is_provider_scoped_and_transaction_bound(self) -> None:
        provider_document_id = "pid') OR TRUE --"
        engine, connection = _fake_live_engine(
            rows=[_live_db_row(provider_document_id=provider_document_id)]
        )

        with (
            patch.object(audit_script, "load_settings", return_value=object()),
            patch.object(audit_script, "app_database_url", return_value="db"),
            patch.object(
                audit_script,
                "create_db_engine",
                return_value=engine,
            ),
        ):
            rows, metadata = audit_script._live_rows([provider_document_id])

        self.assertEqual(rows[0]["provider_document_id"], provider_document_id)
        self.assertEqual(
            connection.method_calls[0],
            call.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            ),
        )
        query_call = connection.execute.call_args_list[1]
        self.assertNotIn(provider_document_id, str(query_call.args[0]))
        self.assertEqual(
            query_call.args[1],
            {"provider_document_ids": [provider_document_id]},
        )
        self.assertEqual(metadata["transaction_isolation"], "repeatable read")
        self.assertIs(metadata["transaction_read_only"], True)
        self.assertEqual(metadata["transaction_snapshot"], "1:2:")
        self.assertEqual(
            metadata["processing_runs"],
            [
                {
                    "provider_document_id": provider_document_id,
                    "processing_run_id": "run",
                }
            ],
        )
        engine.dispose.assert_called_once_with()

    def test_live_query_rejects_wrong_transaction_characteristics(self) -> None:
        engine, _connection = _fake_live_engine(
            rows=[_live_db_row()],
            transaction_isolation="read committed",
        )

        with (
            patch.object(audit_script, "load_settings", return_value=object()),
            patch.object(audit_script, "app_database_url", return_value="db"),
            patch.object(
                audit_script,
                "create_db_engine",
                return_value=engine,
            ),
            self.assertRaisesRegex(
                ValueError,
                "not repeatable-read/read-only",
            ),
        ):
            audit_script._live_rows(["pid"])

    def test_live_query_rejects_multiple_active_runs_for_one_provider(self) -> None:
        engine, _connection = _fake_live_engine(
            rows=[
                _live_db_row(processing_run_id="run-a"),
                _live_db_row(processing_run_id="run-b"),
            ]
        )

        with (
            patch.object(audit_script, "load_settings", return_value=object()),
            patch.object(audit_script, "app_database_url", return_value="db"),
            patch.object(
                audit_script,
                "create_db_engine",
                return_value=engine,
            ),
            self.assertRaisesRegex(ValueError, "multiple active processing runs"),
        ):
            audit_script._live_rows(["pid"])


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "provider_document_id": "pid",
        "unit_index": 0,
        "title": "主要财务数据",
        "heading_path": ["经营情况", "主要财务数据"],
        "semantic_keys": [],
        "section_keys": ["business_review"],
        "content_hash": _CONTENT_HASH,
        "query_projection_hash": _QUERY_HASH,
        "body_status": "content",
        "applicability": None,
    }
    row.update(overrides)
    return row


def _live_db_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "provider_document_id": "pid",
        "processing_run_id": "run",
        "contract_version": "document_unit.v1",
    }
    row.update(overrides)
    return row


def _fake_live_engine(
    *,
    rows: list[dict[str, object]],
    transaction_isolation: str = "repeatable read",
    transaction_read_only: str = "on",
) -> tuple[MagicMock, MagicMock]:
    transaction_result = MagicMock()
    transaction_result.mappings.return_value.one.return_value = {
        "transaction_isolation": transaction_isolation,
        "transaction_read_only": transaction_read_only,
        "transaction_snapshot": "1:2:",
    }
    live_result = MagicMock()
    live_result.mappings.return_value.__iter__.return_value = iter(rows)
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.side_effect = [transaction_result, live_result]
    engine = MagicMock()
    engine.connect.return_value = connection
    return engine, connection
