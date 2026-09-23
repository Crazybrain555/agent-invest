"""Independent durable retry-budget checks; only the managed scratch runner.

The upstream typed cause and terminal lifecycle are exercised separately in
the unit companion. These cases use real PostgreSQL queue SQL and committed
run history, then discard/reopen the client engine to rule out process-local
retry counters. They do not claim to restart a resident service.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from sqlalchemy import text

from disclosure_anchor.application.worker import queries
from tests.integration import test_ops_queue_views as queue_fixture
from tests.integration._support import engine_or_skip


class ProviderTerminalRetryBudgetIndependentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = queue_fixture.OpsQueueViewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def _restart_reader(self) -> None:
        self.fixture.engine.dispose()
        self.fixture.engine = engine_or_skip()

    def _eligible(self, document_id: str) -> bool:
        with self.fixture.engine.connect() as connection:
            return bool(queries.pending_parse(
                connection, max_retries=3, limit=1,
                document_ids=(document_id,),
            ))

    def _history(self, document_id: str) -> list[dict]:
        with self.fixture.engine.connect() as connection:
            return [dict(row) for row in connection.execute(text(
                "SELECT processing_run_id,status,error,started_at "
                "FROM disclosure_core.processing_run "
                "WHERE document_id=:document_id ORDER BY started_at,processing_run_id"
            ), {"document_id": document_id}).mappings()]

    def _add_failure(self, document_id: str, index: int, budget: str) -> None:
        with self.fixture.engine.begin() as connection:
            self.fixture._insert_run(
                connection, document_id, status="failed",
                started_at=datetime(2030, 1, 1, tzinfo=UTC) + timedelta(seconds=index),
                error={
                    "stage": "poll", "error_code": "provider_terminal_failure",
                    "error_class": "ExpectedV4AttemptFailure", "retryable": True,
                    "retry_budget_class": budget,
                    "message": f"original terminal cause {index}",
                },
            )

    def test_fourteenth_infra_failure_survives_restart_fifteenth_exhausts_without_erasure(self) -> None:
        with self.fixture.engine.begin() as connection:
            document = self.fixture._insert_document(connection, status="parse_failed")
            other = self.fixture._insert_document(connection, status="parse_failed")
        for index in range(14):
            self._add_failure(document, index, "infrastructure")
        before = self._history(document)
        self.assertEqual(len(before), 14)
        self._restart_reader()
        self.assertTrue(self._eligible(document))
        self.assertEqual(self._history(document), before)

        self._add_failure(document, 14, "infrastructure")
        exhausted = self._history(document)
        self._restart_reader()
        self.assertFalse(self._eligible(document))
        self.assertTrue(self._eligible(other), "an exhausted document does not close other work")
        self.assertEqual(self._history(document), exhausted)
        self.assertEqual(exhausted[:14], before)
        self.assertEqual(
            [row["error"]["message"] for row in exhausted],
            [f"original terminal cause {index}" for index in range(15)],
        )
        self.assertTrue(all(row["status"] == "failed" for row in exhausted))
        self.assertTrue(all(row["error"]["retryable"] for row in exhausted))

    def test_item_and_infra_history_share_total_ceiling_without_resetting_item_limit(self) -> None:
        with self.fixture.engine.begin() as connection:
            document = self.fixture._insert_document(connection, status="parse_failed")
        for index, budget in enumerate(("item", "item", "infrastructure")):
            self._add_failure(document, index, budget)
        self._restart_reader()
        self.assertTrue(self._eligible(document))
        self._add_failure(document, 3, "item")
        expected = self._history(document)
        self._restart_reader()
        self.assertFalse(self._eligible(document), "three item failures exhaust before fifteen total")
        self.assertEqual(self._history(document), expected)


if __name__ == "__main__":
    unittest.main()
