"""DB-gated test for the outbox commit-order advisory lock (round23).

BIGSERIAL assigns seq at INSERT, not commit; without the writer-side lock two
concurrent publishers can commit out of seq order and a `seq > cursor`
consumer of /v1/changes skips the late-committing row forever. The repository
must therefore hold pg_advisory_xact_lock(OUTBOX_NS, 0) from the first outbox
insert until commit/rollback.
"""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.application.worker.locks import OUTBOX_NS
from disclosure_anchor.domain.entities import outbox_events
from tests.integration._support import engine_or_skip


def _advisory_lock_count(session, ns: int) -> int:
    return session.execute(
        text(
            "SELECT count(*) FROM pg_locks "
            "WHERE locktype = 'advisory' AND classid = :ns "
            "AND pid = pg_backend_pid()"
        ),
        {"ns": ns},
    ).scalar_one()


class OutboxCommitOrderLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_outbox_add_holds_xact_lock_until_transaction_end(self) -> None:
        event = outbox_events.document_observed(
            document_id="doc_outbox_lock_test",
            provider="cninfo",
            provider_document_id="0",
            raw_file_hash="sha256:test",
            source_access_id="sa_outbox_lock_test",
            occurred_at=datetime.now(timezone.utc),
        )
        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            self.assertEqual(_advisory_lock_count(uow.session, OUTBOX_NS), 0)
            stored = uow.outbox.add(event)
            self.assertIsNotNone(stored.seq)
            self.assertEqual(_advisory_lock_count(uow.session, OUTBOX_NS), 1)
            # no commit: default rollback discards the row and releases the lock

        with SqlAlchemyUnitOfWork(engine=self.engine) as uow:
            self.assertEqual(_advisory_lock_count(uow.session, OUTBOX_NS), 0)
            leftover = uow.outbox.get(event.event_id)
            self.assertIsNone(leftover)


if __name__ == "__main__":
    unittest.main()
