"""Pure P/F/U classification tests for exact corpus replay."""

from __future__ import annotations

import unittest

from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)
from disclosure_anchor.application.worker.queries import (
    RETRY_CEILING_MULTIPLIER,
    _classify_document_processing_row,
)


TARGET = {
    "parser_target": ParserTargetIdentity(
        name="MinerU",
        package_version="3.4.0",
        backend="pipeline",
        method="auto",
        language="ch",
        formula=False,
        table=True,
        runtime_bundle_identity_sha256="sha256:" + "b" * 64,
    ).to_payload(),
    "builder_rules_version": "rules.v1",
    "retrieval_rules_version": "retrieval.v1",
}


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "document_id": "doc_1",
        "document_status": "registered",
        "raw_file_hash": "sha256:" + "a" * 64,
        "current_processing_run_id": None,
        "generation_run_count": 0,
        "non_parse_run_count": 0,
        "running_count": 0,
        "succeeded_count": 0,
        "failed_count": 0,
        "active_count": 0,
        "invalid_failure_count": 0,
        "item_failure_count": 0,
        "charged_failure_count": 0,
        "latest_failed_retryable": None,
        "processing_run_id": None,
        "is_active": None,
        "parser_target_identity": None,
        "search_projection_error": None,
        "input_raw_file_hash": None,
        "unit_build_status": None,
        "unit_build_attempt_count": 0,
        "unit_build_error": None,
        "builder_rules_version": None,
        "unit_count": 0,
        "projection_gap_count": 0,
        "failed_after_success_count": 0,
    }
    row.update(overrides)
    return row


def _succeeded_row(**overrides: object) -> dict[str, object]:
    row = _row(
        document_status="parsed",
        generation_run_count=1,
        succeeded_count=1,
        processing_run_id="run_1",
        parser_target_identity=TARGET["parser_target"],
        input_raw_file_hash="sha256:" + "a" * 64,
        unit_build_status="not_started",
    )
    row.update(overrides)
    return row


def _state(row: dict[str, object]):
    return _classify_document_processing_row(
        row,
        target_identity=TARGET,
        max_parse_retries=3,
        max_build_retries=3,
    )


def _projection_error(*, rules_version: str = "retrieval.v1") -> dict[str, object]:
    return {
        "stage": "search_projection",
        "error_code": "search_target_contract_invalid",
        "retryable": False,
        "retrieval_rules_version": rules_version,
    }


class DocumentProcessingStateTests(unittest.TestCase):
    def test_parse_pending_is_exactly_actionable(self) -> None:
        not_started = _state(_row())
        running = _state(
            _row(generation_run_count=1, running_count=1)
        )
        item_retry = _state(
            _row(
                generation_run_count=2,
                failed_count=2,
                item_failure_count=2,
                charged_failure_count=2,
                latest_failed_retryable=True,
            )
        )
        infrastructure_retry = _state(
            _row(
                generation_run_count=4,
                failed_count=4,
                charged_failure_count=4,
                latest_failed_retryable=True,
            )
        )
        for state in (
            not_started,
            running,
            item_retry,
            infrastructure_retry,
        ):
            self.assertEqual(state.state, "pending")
            self.assertEqual(state.actionable_stage, "parse")

    def test_parse_terminal_boundaries_and_contract_are_fail_closed(self) -> None:
        cases = {
            "parse_nonretryable": _row(
                generation_run_count=1,
                failed_count=1,
                item_failure_count=1,
                charged_failure_count=1,
                latest_failed_retryable=False,
            ),
            "parse_item_budget_exhausted": _row(
                generation_run_count=3,
                failed_count=3,
                item_failure_count=3,
                charged_failure_count=3,
                latest_failed_retryable=True,
            ),
            "parse_charge_ceiling_exhausted": _row(
                generation_run_count=3 * RETRY_CEILING_MULTIPLIER,
                failed_count=3 * RETRY_CEILING_MULTIPLIER,
                charged_failure_count=3 * RETRY_CEILING_MULTIPLIER,
                latest_failed_retryable=True,
            ),
            "invariant_failure_contract": _row(
                generation_run_count=1,
                failed_count=1,
                invalid_failure_count=1,
            ),
        }
        for reason, row in cases.items():
            with self.subTest(reason=reason):
                state = _state(row)
                self.assertEqual(state.state, "terminal_failed")
                self.assertEqual(state.reason_code, reason)
                self.assertIsNone(state.actionable_stage)

    def test_build_publish_and_projection_are_distinct_actions(self) -> None:
        build_new = _state(_succeeded_row())
        build_retry = _state(
            _succeeded_row(
                unit_build_status="failed",
                unit_build_attempt_count=2,
                unit_build_error={"stage": "build_units", "retryable": False},
            )
        )
        publish = _state(
            _succeeded_row(
                unit_build_status="succeeded",
                unit_build_attempt_count=1,
                builder_rules_version=TARGET["builder_rules_version"],
                unit_count=2,
            )
        )
        projection = _state(
            _succeeded_row(
                document_status="published",
                current_processing_run_id="run_1",
                active_count=1,
                is_active=True,
                unit_build_status="succeeded",
                unit_build_attempt_count=1,
                builder_rules_version=TARGET["builder_rules_version"],
                unit_count=2,
                projection_gap_count=1,
            )
        )
        projection_terminal = _state(
            _succeeded_row(
                document_status="published",
                current_processing_run_id="run_1",
                active_count=1,
                is_active=True,
                unit_build_status="succeeded",
                unit_build_attempt_count=1,
                builder_rules_version=TARGET["builder_rules_version"],
                unit_count=2,
                projection_gap_count=1,
                search_projection_error=_projection_error(),
            )
        )
        projection_after_rules_change = _state(
            _succeeded_row(
                document_status="published",
                current_processing_run_id="run_1",
                active_count=1,
                is_active=True,
                unit_build_status="succeeded",
                unit_build_attempt_count=1,
                builder_rules_version=TARGET["builder_rules_version"],
                unit_count=2,
                projection_gap_count=1,
                search_projection_error=_projection_error(
                    rules_version="retrieval.old"
                ),
            )
        )
        self.assertEqual(build_new.actionable_stage, "build")
        self.assertEqual(build_retry.actionable_stage, "build")
        self.assertEqual(publish.actionable_stage, "publish")
        self.assertEqual(projection.actionable_stage, "projection")
        self.assertEqual(projection_terminal.state, "terminal_failed")
        self.assertEqual(
            projection_terminal.reason_code,
            "search_projection_terminal",
        )
        self.assertIsNone(projection_terminal.actionable_stage)
        self.assertEqual(projection_after_rules_change.state, "pending")
        self.assertEqual(
            projection_after_rules_change.actionable_stage,
            "projection",
        )

    def test_empty_build_and_exhausted_build_are_terminal(self) -> None:
        empty = _state(
            _succeeded_row(
                unit_build_status="succeeded",
                builder_rules_version=TARGET["builder_rules_version"],
                unit_count=0,
            )
        )
        exhausted = _state(
            _succeeded_row(
                unit_build_status="failed",
                unit_build_attempt_count=3,
                unit_build_error={"stage": "build_units", "retryable": False},
            )
        )
        terminal_publish = _state(
            _succeeded_row(
                unit_build_status="failed",
                unit_build_attempt_count=1,
                unit_build_error={"stage": "publish", "retryable": False},
            )
        )
        self.assertEqual(empty.reason_code, "empty_unit_set")
        self.assertEqual(exhausted.reason_code, "build_budget_exhausted")
        self.assertEqual(terminal_publish.reason_code, "publish_terminal")
        self.assertTrue(
            all(
                state.state == "terminal_failed"
                for state in (empty, exhausted, terminal_publish)
            )
        )

    def test_usable_requires_current_active_nonempty_projection_closure(self) -> None:
        state = _state(
            _succeeded_row(
                document_status="published",
                current_processing_run_id="run_1",
                active_count=1,
                is_active=True,
                unit_build_status="succeeded",
                unit_build_attempt_count=1,
                builder_rules_version=TARGET["builder_rules_version"],
                unit_count=2,
                projection_gap_count=0,
                generation_run_count=3,
                failed_count=2,
                item_failure_count=2,
                charged_failure_count=2,
                latest_failed_retryable=True,
            )
        )
        self.assertEqual(state.state, "usable_published")
        self.assertIsNone(state.actionable_stage)

    def test_illegal_graphs_are_invariant_failures(self) -> None:
        cases = (
            _succeeded_row(succeeded_count=2, generation_run_count=2),
            _succeeded_row(running_count=1, generation_run_count=2),
            _succeeded_row(failed_after_success_count=1, generation_run_count=2),
            _succeeded_row(non_parse_run_count=1, generation_run_count=2),
            _succeeded_row(
                unit_build_status="running",
            ),
            _succeeded_row(
                document_status="published",
                current_processing_run_id="run_1",
                active_count=1,
                is_active=True,
                unit_build_status="succeeded",
                builder_rules_version=TARGET["builder_rules_version"],
                unit_count=1,
                projection_gap_count=1,
                search_projection_error={"stage": "search_projection"},
            ),
            _succeeded_row(
                document_status="published",
                current_processing_run_id="run_1",
                active_count=1,
                is_active=True,
                unit_build_status="succeeded",
                builder_rules_version=TARGET["builder_rules_version"],
                unit_count=1,
                projection_gap_count=0,
                search_projection_error=_projection_error(),
            ),
        )
        for row in cases:
            with self.subTest(row=row):
                state = _state(row)
                self.assertEqual(state.state, "terminal_failed")
                self.assertTrue(state.invariant_codes)


if __name__ == "__main__":
    unittest.main()
