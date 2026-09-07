from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest import mock

import sqlalchemy as sa

from disclosure_anchor.adapters.db.postgres.staged_new_work_v4 import (
    PostgresV4OrdinaryParseCandidateSource, require_commissioning_recovery_scope,
)
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.ports.staged_new_work_v4 import validate_admission_document_ids
from disclosure_anchor.application.services.staged_parse_coordinator import CoordinatorResult, CoordinatorTerminal
from disclosure_anchor.application.worker.queries import pending_parse
from disclosure_anchor.cli.staged_commission import _outcomes, run_commissioning
from tests.unit.test_mineru_process_profile import _profile


class StagedCommissionTests(unittest.TestCase):
    def test_scope_is_closed_and_never_empty_means_all(self) -> None:
        validate_admission_document_ids(None)
        validate_admission_document_ids(("opaque:id", "doc-2"))
        for invalid in ((), [], ("",), ("x", "x"), (" x",), ("x\n",), ("a"*65,), tuple(str(i) for i in range(9))):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                validate_admission_document_ids(invalid)

    def test_selected_sql_and_keyset_are_both_before_limit(self) -> None:
        connection = mock.MagicMock()
        connection.execute.return_value.mappings.return_value = []
        self.assertEqual(pending_parse(connection, max_retries=3, limit=2,
                                      document_ids=("chosen",), after_document_id="before"), [])
        statement, params = connection.execute.call_args.args
        sql = str(statement)
        self.assertLess(sql.index("ANY(:document_ids)"), sql.index("LIMIT :limit"))
        self.assertLess(sql.index("q.document_id > :after_document_id"), sql.index("LIMIT :limit"))
        self.assertIn("tc_scope.status = 'active'", sql)
        self.assertEqual(params["document_ids"], ["chosen"])
        self.assertEqual(params["after_document_id"], "before")

    def test_source_passes_allowlist_into_authoritative_query(self) -> None:
        engine = sa.create_engine("sqlite://")
        try:
            source = PostgresV4OrdinaryParseCandidateSource(engine=engine, max_retries=3,
                        scope_classes=("annual_report",), admission_document_ids=("chosen",))
            with mock.patch("disclosure_anchor.adapters.db.postgres.staged_new_work_v4.pending_parse", return_value=[]) as query:
                self.assertEqual(source.list_candidates(after_document_id="before", limit=1).candidates, ())
            self.assertEqual(query.call_args.kwargs["document_ids"], ("chosen",))
            self.assertEqual(query.call_args.kwargs["after_document_id"], "before")
            self.assertEqual(query.call_args.kwargs["limit"], 2)
        finally:
            engine.dispose()

    def test_outside_recovery_blocks_instead_of_being_filtered(self) -> None:
        engine = mock.MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        connection.execute.return_value.scalar_one.return_value = True
        with self.assertRaisesRegex(RuntimeError, "outside commissioning scope"):
            require_commissioning_recovery_scope(engine, ("chosen",))
        sql, params = connection.execute.call_args.args
        self.assertIn("NOT (document_id=ANY", str(sql))
        self.assertIn("ack_pending", params["resource_states"])
        self.assertIn("prepared", params["resource_states"])
        self.assertNotIn("acked", params["resource_states"])

    def test_runtime_rejects_outside_owner_before_profile_or_scratch(self) -> None:
        from disclosure_anchor.adapters.runtime.staged_worker_v4 import build_staged_worker_v4_runtime
        with (
            mock.patch("disclosure_anchor.adapters.runtime.staged_worker_v4.require_commissioning_recovery_scope", side_effect=RuntimeError("outside commissioning scope")),
            mock.patch("disclosure_anchor.adapters.runtime.staged_worker_v4.load_staged_v4_settings") as loader,
            self.assertRaisesRegex(RuntimeError, "outside commissioning scope"),
        ):
            build_staged_worker_v4_runtime(settings=SimpleNamespace(worker_parse_execution_mode="staged-v4"),
                engine=mock.Mock(), ownership_guard=lambda: None, admission_guard=lambda: None,
                process_scope_classes=None, progress=lambda _: None, admission_document_ids=("chosen",))
        loader.assert_not_called()

    def test_cli_uses_single_production_run_and_quiescence_does_not_forge_success(self) -> None:
        before = {"chosen": {"status": "registered", "current_processing_run_id": None}}
        published = {"chosen": {"status": "published", "current_processing_run_id": "run",
                                "attempt_run_id": "run", "attempt_state": "acked",
                                "run_is_active": True, "run_status": "succeeded"}}
        for after, expected in ((published, "PASS"), (before, "NOT_PASS")):
            after = {key: {"attempt_state": None, **value} for key, value in after.items()}
            with self.subTest(expected=expected), ExitStack() as stack:
                prefix = "disclosure_anchor.cli.staged_commission."
                loaded = SimpleNamespace(profile=_profile())
                stack.enter_context(mock.patch(prefix+"_load_staged_process_profile", return_value=loaded))
                stack.enter_context(mock.patch(prefix+"MinerUDeploymentChecker"))
                lock_engine = stack.enter_context(mock.patch(prefix+"sa.create_engine")).return_value
                stack.enter_context(mock.patch(prefix+"require_runtime_app_connection"))
                stack.enter_context(mock.patch(prefix+"require_runtime_app_engine"))
                stack.enter_context(mock.patch(prefix+"_database_url", return_value="unused"))
                stack.enter_context(mock.patch(prefix+"_create_worker_db_engine"))
                stack.enter_context(mock.patch(prefix+"_process_scope_classes", return_value=("annual_report",)))
                stack.enter_context(mock.patch(prefix+"_documents", side_effect=[before, after]))
                build = stack.enter_context(mock.patch(prefix+"build_staged_worker_v4_runtime"))
                runtime = build.return_value
                runtime.owner_identity = "test-owner"
                runtime.worker_profile_sha256 = "sha256:"+"f"*64
                runtime.coordinator.run.return_value = CoordinatorResult(
                    CoordinatorTerminal.QUIESCENT, True, 1, 1, (), (), ResourceCreditVector())
                result = run_commissioning(SimpleNamespace(worker_parse_execution_mode="staged-v4"),
                                          document_ids=("chosen",), max_seconds=5)
                self.assertEqual(result["result"], expected)
                self.assertEqual(build.call_args.kwargs["admission_document_ids"], ("chosen",))
                runtime.verify_startup.assert_called_once_with()
                runtime.coordinator.run.assert_called_once()
                runtime.close.assert_called_once_with()
                lock_engine.dispose.assert_called_once_with()

    def test_ack_pending_or_preexisting_publication_is_not_new_success(self) -> None:
        existing = {"status": "published", "current_processing_run_id": "old", "attempt_state": "acked", "attempt_run_id": "old", "run_is_active": True, "run_status": "succeeded"}
        self.assertEqual(_outcomes(("d",), {"d": existing}, {"d": existing})[0]["outcome"], "already_ineligible")
        for state in ("ack_pending", "published_cleanup_pending"):
            pending = {**existing, "attempt_state": state, "current_processing_run_id": "new", "attempt_run_id": "new"}
            self.assertEqual(_outcomes(("d",), {"d": existing}, {"d": pending})[0]["outcome"], "blocked")
        prior = {**existing, "status": "parsed"}
        self.assertEqual(_outcomes(("d",), {"d": prior}, {"d": existing})[0]["outcome"], "recovered_published")
        for active, status in ((False, "succeeded"), (True, "running")):
            changed = {**existing, "current_processing_run_id": "new", "attempt_run_id": "new",
                       "run_is_active": active, "run_status": status}
            self.assertEqual(_outcomes(("d",), {"d": prior}, {"d": changed})[0]["outcome"], "blocked")
