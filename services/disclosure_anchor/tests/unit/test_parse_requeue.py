"""Explicit parse requeue: guardrails, receipt, queue release and CLI input."""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from disclosure_anchor.application.use_cases.parse_requeue import (
    ParseRequeue,
    ParseRequeueCommand,
)
from disclosure_anchor.application.worker import queries
from disclosure_anchor.cli.parse_requeue import main
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import ParseRequeueError

from tests.unit._fakes import FakeUnitOfWork

_FAILED_AT = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
_CONTRACT_ERROR = {
    "stage": "parse",
    "error_code": "semantic_route_locked_candidate_overflow",
    "retryable": False,
    "retry_budget_class": "semantic_route_contract",
}


def _run(
    processing_run_id: str = "run_failed",
    *,
    status: str = "failed",
    run_kind: str = "parse",
    error: dict | None = None,
    started_at: datetime | None = None,
    undated: bool = False,
    provider_relpath: str | None = "derived/provider_documents/x.json",
    normalized_relpath: str | None = None,
) -> e.ProcessingRun:
    return e.ProcessingRun(
        processing_run_id=processing_run_id,
        document_id="doc_1",
        artifact_owner_processing_run_id=processing_run_id,
        run_kind=run_kind,
        status=status,
        provider_document_relpath=provider_relpath,
        normalized_ir_relpath=normalized_relpath,
        started_at=None if undated else (started_at or _FAILED_AT),
        error=_CONTRACT_ERROR if error is None else error,
    )


def _command(**overrides: str) -> ParseRequeueCommand:
    fields = {
        "document_id": "doc_1",
        "processing_run_id": "run_failed",
        "fixed_by": "semantic_router.v102 4548ecaa",
        "reason": "locked-candidate overflow now demotes to model candidates",
        "decided_by": "operator",
    }
    fields.update(overrides)
    return ParseRequeueCommand(**fields)


def _uow_with_failed_run(run: e.ProcessingRun | None = None) -> FakeUnitOfWork:
    uow = FakeUnitOfWork()
    uow.processing_runs.add(run if run is not None else _run())
    return uow


class ParseRequeueUseCaseTests(unittest.TestCase):
    def test_decision_records_the_stored_failure_identity_and_commits(self) -> None:
        uow = _uow_with_failed_run()
        # An older succeeded generation is history, not a reason to refuse:
        # only a success that is not older than the failure blocks a requeue.
        uow.processing_runs.add(
            _run(
                "run_earlier_success",
                status="succeeded",
                started_at=_FAILED_AT - timedelta(days=2),
            )
        )

        result = ParseRequeue(
            uow_factory=lambda: uow,
            decision_id_factory=lambda: "prq_01M2VG1RW16QK0XMYMQQNA7G4B",
        ).execute(_command())

        self.assertEqual(result.decision_id, "prq_01M2VG1RW16QK0XMYMQQNA7G4B")
        self.assertEqual(result.processing_run_id, "run_failed")
        self.assertEqual(
            result.failure_error_code, "semantic_route_locked_candidate_overflow"
        )
        self.assertEqual(result.failure_retry_budget_class, "semantic_route_contract")
        self.assertEqual(result.decided_by, "operator")
        self.assertEqual(result.decided_at, uow.processing_runs.decision_clock)
        self.assertFalse(result.dry_run)
        self.assertEqual(uow.commit_count, 1)
        stored = uow.processing_runs.parse_requeue_decision_for_run("run_failed")
        assert stored is not None
        self.assertEqual(
            stored.reason,
            "locked-candidate overflow now demotes to model candidates",
        )

    def test_dry_run_evaluates_guardrails_and_writes_nothing(self) -> None:
        uow = _uow_with_failed_run()

        result = ParseRequeue(uow_factory=lambda: uow).execute(
            _command(), dry_run=True
        )

        self.assertTrue(result.dry_run)
        self.assertIsNone(result.decision_id)
        self.assertIsNone(result.decided_at)
        self.assertEqual(
            result.failure_error_code, "semantic_route_locked_candidate_overflow"
        )
        self.assertEqual(uow.commit_count, 0)
        self.assertEqual(uow.processing_runs.parse_requeue_decisions, {})

        with self.assertRaises(ParseRequeueError) as refused:
            ParseRequeue(uow_factory=lambda: uow).execute(
                _command(processing_run_id="run_missing"), dry_run=True
            )
        self.assertEqual(refused.exception.error["error_code"], "RUN_NOT_FOUND")

    def test_every_refusal_family_fails_closed_without_writing(self) -> None:
        # A rebuild_units generation counts as a later success too, and the
        # ordering is (started_at, processing_run_id): equal start times fall
        # back to the run id, and an unknown start time never proves "earlier".
        succeeded_later = _run(
            "run_succeeded",
            status="succeeded",
            run_kind="rebuild_units",
            started_at=_FAILED_AT,
        )
        decided = _uow_with_failed_run()
        ParseRequeue(uow_factory=lambda: decided).execute(_command())
        cases: list[tuple[str, FakeUnitOfWork, ParseRequeueCommand]] = [
            ("RUN_NOT_FOUND", _uow_with_failed_run(), _command(processing_run_id="run_x")),
            ("RUN_NOT_FOUND", _uow_with_failed_run(), _command(document_id="doc_other")),
            (
                "RUN_NOT_A_PROVIDER_PARSE_RUN",
                _uow_with_failed_run(_run(run_kind="rebuild_units")),
                _command(),
            ),
            (
                "RUN_NOT_A_PROVIDER_PARSE_RUN",
                _uow_with_failed_run(
                    _run(provider_relpath=None, normalized_relpath="derived/ir.json")
                ),
                _command(),
            ),
            (
                "RUN_NOT_FAILED",
                _uow_with_failed_run(_run(status="running")),
                _command(),
            ),
            (
                "RUN_ERROR_CONTRACT_INVALID",
                _uow_with_failed_run(_run(error={})),
                _command(),
            ),
            (
                "RUN_ERROR_CONTRACT_INVALID",
                _uow_with_failed_run(
                    _run(error={**_CONTRACT_ERROR, "retryable": "false"})
                ),
                _command(),
            ),
            (
                "RETRY_BUDGET_CLASS_IS_AUTOMATIC",
                _uow_with_failed_run(
                    _run(error={**_CONTRACT_ERROR, "retry_budget_class": "item"})
                ),
                _command(),
            ),
            (
                "RETRY_BUDGET_CLASS_UNKNOWN",
                _uow_with_failed_run(
                    _run(error={**_CONTRACT_ERROR, "retry_budget_class": "deterministic"})
                ),
                _command(),
            ),
            ("DECISION_EVIDENCE_REQUIRED", _uow_with_failed_run(), _command(reason="  ")),
            (
                "DECISION_EVIDENCE_REQUIRED",
                _uow_with_failed_run(),
                _command(decided_by=""),
            ),
            ("DECISION_ALREADY_EXISTS", decided, _command()),
        ]
        later_success = _uow_with_failed_run()
        later_success.processing_runs.add(succeeded_later)
        cases.append(("LATER_SUCCEEDED_PARSE_RUN", later_success, _command()))
        # An older dated success does not excuse an undated one: every
        # succeeded generation must be provably older than the failure.
        undated_success = _uow_with_failed_run()
        undated_success.processing_runs.add(
            _run(
                "run_earlier_success",
                status="succeeded",
                started_at=_FAILED_AT - timedelta(days=2),
            )
        )
        undated_success.processing_runs.add(
            _run("run_undated_success", status="succeeded", undated=True)
        )
        cases.append(("LATER_SUCCEEDED_PARSE_RUN", undated_success, _command()))

        for error_code, uow, command in cases:
            with self.subTest(error_code=error_code):
                written = dict(uow.processing_runs.parse_requeue_decisions)
                commits = uow.commit_count
                with self.assertRaises(ParseRequeueError) as refused:
                    ParseRequeue(uow_factory=lambda uow=uow: uow).execute(command)
                self.assertEqual(refused.exception.error["error_code"], error_code)
                self.assertFalse(refused.exception.error["retryable"])
                self.assertEqual(uow.processing_runs.parse_requeue_decisions, written)
                self.assertEqual(uow.commit_count, commits)


class ParseQueueReleaseTests(unittest.TestCase):
    def test_pending_parse_consults_the_decision_for_both_admission_gates(
        self,
    ) -> None:
        connection = MagicMock()
        connection.execute.return_value.mappings.return_value = []

        queries.pending_parse(connection, max_retries=3, limit=10)

        statement = str(connection.execute.call_args.args[0])
        # A contract-class failure is excluded twice: by its own class and by
        # the view's retryable latch. One decision must release both.
        self.assertIn(queries.PARSE_UNRELEASED_CONTRACT_FAILURE_SQL, statement)
        self.assertIn(queries.PARSE_LAST_FAILURE_RELEASED_SQL, statement)
        self.assertIn(
            "COALESCE(q.last_failed_retryable, true)\n     OR (", statement
        )
        # The release is per run: the contract clause matches the failed run
        # itself, the latch clause only the latest failure.
        self.assertIn(
            "prd.processing_run_id = failed_run.processing_run_id",
            queries.PARSE_UNRELEASED_CONTRACT_FAILURE_SQL,
        )
        self.assertIn(
            "ORDER BY latest_failure.started_at DESC",
            queries.PARSE_LAST_FAILURE_RELEASED_SQL,
        )

    def test_admission_diagnosis_answers_eligibility_and_remaining_blockers(
        self,
    ) -> None:
        blockers = {
            "document_status": "parse_failed",
            "running_run_present": False,
            "latest_failed_run_id": "run_failed",
            "latest_failed_run_retryable": False,
            "latest_failed_run_released": True,
            "unreleased_contract_failures": 0,
            "item_failure_count": 1,
            "charged_failure_count": 1,
        }
        connection = MagicMock()
        eligible_result = MagicMock()
        eligible_result.mappings.return_value = [{"document_id": "doc_1"}]
        blockers_result = MagicMock()
        blockers_result.mappings.return_value.one_or_none.return_value = blockers
        connection.execute.side_effect = (eligible_result, blockers_result)

        diagnosis = queries.parse_admission_diagnosis(
            connection, document_id="doc_1", max_retries=3
        )

        self.assertTrue(diagnosis["currently_eligible"])
        self.assertEqual(
            diagnosis["remaining_blockers"],
            {
                **blockers,
                "max_item_failures": 3,
                "max_charged_failures": 3 * queries.RETRY_CEILING_MULTIPLIER,
            },
        )
        # Eligibility is the queue's own answer, not a second predicate.
        eligibility_sql = str(connection.execute.call_args_list[0].args[0])
        self.assertIn("pending_parse_v1", eligibility_sql)
        blockers_sql = str(connection.execute.call_args_list[1].args[0])
        self.assertIn("disclosure_ops.parse_requeue_decision", blockers_sql)

        missing = MagicMock()
        empty = MagicMock()
        empty.mappings.return_value = []
        absent = MagicMock()
        absent.mappings.return_value.one_or_none.return_value = None
        missing.execute.side_effect = (empty, absent)
        with self.assertRaises(ValueError):
            queries.parse_admission_diagnosis(
                missing, document_id="doc_gone", max_retries=3
            )


class ParseRequeueCliTests(unittest.TestCase):
    def test_cli_receipt_separates_record_eligibility_and_blockers(self) -> None:
        uow = _uow_with_failed_run()
        diagnosis = {
            "currently_eligible": False,
            "remaining_blockers": {
                "document_status": "parse_failed",
                "running_run_present": True,
                "latest_failed_run_id": "run_failed",
                "latest_failed_run_retryable": False,
                "latest_failed_run_released": True,
                "unreleased_contract_failures": 0,
                "item_failure_count": 0,
                "charged_failure_count": 0,
                "max_item_failures": 3,
                "max_charged_failures": 15,
            },
        }
        printed = io.StringIO()
        with (
            patch(
                "disclosure_anchor.cli.parse_requeue.load_settings",
                return_value=SimpleNamespace(disclosure_max_parse_retries=3),
            ),
            patch("disclosure_anchor.cli.parse_requeue.app_database_url"),
            patch("disclosure_anchor.cli.parse_requeue.create_db_engine"),
            patch("disclosure_anchor.cli.parse_requeue.require_runtime_app_engine"),
            patch(
                "disclosure_anchor.cli.parse_requeue.unit_of_work_factory",
                return_value=lambda: uow,
            ),
            patch(
                "disclosure_anchor.cli.parse_requeue.parse_admission_diagnosis",
                return_value=diagnosis,
            ),
            redirect_stdout(printed),
        ):
            exit_code = main(
                [
                    "--document-id", "doc_1",
                    "--processing-run-id", "run_failed",
                    "--fixed-by", "semantic_router.v102 4548ecaa",
                    "--reason", "locked-candidate overflow now demotes",
                    "--decided-by", "operator",
                ]
            )

        self.assertEqual(exit_code, 0)
        receipt = json.loads(printed.getvalue())
        self.assertTrue(receipt["decision_recorded"])
        self.assertTrue(receipt["decision_id"].startswith("prq_"))
        # Recording and admission are separate facts; the receipt never
        # collapses them into one "ok".
        self.assertFalse(receipt["currently_eligible"])
        self.assertEqual(
            receipt["remaining_blockers"], diagnosis["remaining_blockers"]
        )
        self.assertIn("admits nothing by itself", receipt["note"])

    def test_cli_refuses_missing_or_blank_evidence_before_settings(self) -> None:
        complete = [
            "--document-id", "doc_1",
            "--processing-run-id", "run_failed",
            "--fixed-by", "semantic_router.v102",
            "--reason", "fixed",
            "--decided-by", "operator",
        ]
        for arguments in (
            complete[:8],
            complete[:2] + complete[4:],
            complete[:7] + ["  "] + complete[8:],
            complete[:9] + [""],
        ):
            with (
                self.subTest(arguments=arguments),
                patch("disclosure_anchor.cli.parse_requeue.load_settings") as settings,
            ):
                with self.assertRaises(SystemExit):
                    main(arguments)
                settings.assert_not_called()
