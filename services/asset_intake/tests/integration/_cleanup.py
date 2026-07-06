"""Shared live-DB test hygiene: purge rows created by integration tests.

All integration fixtures register with adapter names under the ``tests.``
prefix, so cleanup is a deterministic sweep by that marker (review finding
2026-07-06: fake rows must not linger in the shared invest_engine database).
Called in setUpClass (clears residue from earlier aborted runs) and
tearDownClass (clears what this run created).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

TEST_ADAPTER_PREFIX = "tests."


def purge_test_rows(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM intake_ops.outbox_event WHERE processing_run_id IN "
            "(SELECT run_id FROM intake_core.processing_run WHERE adapter LIKE 'tests.%')"
        ))
        conn.execute(text(
            "UPDATE intake_core.data_asset SET superseded_by = NULL "
            "WHERE adapter LIKE 'tests.%'"
        ))
        conn.execute(text("DELETE FROM intake_core.data_asset WHERE adapter LIKE 'tests.%'"))
        conn.execute(text("DELETE FROM intake_core.source_access WHERE adapter LIKE 'tests.%'"))
        conn.execute(text("DELETE FROM intake_core.processing_run WHERE adapter LIKE 'tests.%'"))
