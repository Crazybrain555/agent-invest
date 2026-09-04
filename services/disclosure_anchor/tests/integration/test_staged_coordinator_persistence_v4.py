"""Scratch-PostgreSQL tests for the V4 coordinator persistence slice."""

from __future__ import annotations

from collections.abc import Callable
import time
from types import TracebackType
import unittest

import sqlalchemy as sa
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres.remote_parse_v4_repository import (
    RemoteParseV4Repository,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import (
    SqlAlchemyUnitOfWork,
    unit_of_work_factory,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    V4SuccessorAppend,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import (
    DurableStagedCoordinatorPersistenceV4,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorLimits,
    RecoveryDeferred,
)
from tests.integration._remote_parse_v4_factory import (
    build_v4_authority_fixture,
    build_v4_resource_free_supersession_fixture,
    install_prepared_cycle,
    install_submitted_cycle,
    install_v4_resource_free_supersession,
)
from tests.integration._support import engine_or_skip


def _limits() -> CoordinatorLimits:
    return CoordinatorLimits(
        credits=ResourceCreditVector(
            documents=8,
            snapshot_items=8,
            snapshot_bytes=10_000,
            remote_waits=8,
            provider_tasks=8,
            provider_result_bytes=10_000,
            materialization_items=8,
            compressed_bytes=10_000,
            decoded_bytes=2_000_000,
            temp_disk_bytes=4_000_000,
            output_items=8,
            output_bytes=2_000_000,
            output_pages=1_000,
            ack_items=8,
        ),
        recovery_page_size=2,
        poll_seconds=0.01,
    )


class _CommitResponseLostUnitOfWork:
    def __init__(
        self,
        delegate: SqlAlchemyUnitOfWork,
        lose_once: list[bool],
    ) -> None:
        self._delegate = delegate
        self._lose_once = lose_once

    @property
    def remote_parse_v4(self) -> object:
        return self._delegate.remote_parse_v4

    def __enter__(self) -> _CommitResponseLostUnitOfWork:
        self._delegate.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._delegate.__exit__(exc_type, exc, tb)

    def commit(self) -> None:
        self._delegate.commit()
        if self._lose_once[0]:
            self._lose_once[0] = False
            raise RuntimeError("simulated commit response loss")


class StagedCoordinatorPersistenceV4IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self._clean_scratch_rows()

    def tearDown(self) -> None:
        try:
            self._clean_scratch_rows()
        finally:
            self.engine.dispose()

    def _clean_scratch_rows(self) -> None:
        with self.engine.begin() as conn:
            roots = tuple(
                conn.execute(
                    sa.text(
                        "SELECT DISTINCT processing_run_id,document_id "
                        "FROM disclosure_core.processing_run "
                        "WHERE provider_document_relpath='scratch/provider.json'"
                    )
                ).mappings()
            )
            conn.exec_driver_sql(
                "TRUNCATE TABLE disclosure_ops.remote_parse_attempt CASCADE"
            )
            for root in roots:
                conn.execute(
                    sa.text(
                        "DELETE FROM disclosure_core.processing_run "
                        "WHERE processing_run_id=:processing_run_id"
                    ),
                    {"processing_run_id": root["processing_run_id"]},
                )
            for root in roots:
                conn.execute(
                    sa.text(
                        "DELETE FROM disclosure_core.document "
                        "WHERE document_id=:document_id"
                    ),
                    {"document_id": root["document_id"]},
                )

    def _backend(
        self,
        *,
        owner: str = "worker-integration-boot",
        factory: Callable[[], UnitOfWork] | None = None,
    ) -> DurableStagedCoordinatorPersistenceV4:
        return DurableStagedCoordinatorPersistenceV4(
            uow_factory=factory or unit_of_work_factory(self.engine),
            limits=_limits(),
            owner_identity=owner,
        )

    def test_claim_renew_and_one_step_reload_use_durable_authority(self) -> None:
        fixture = build_v4_authority_fixture(attempt_id="rpa_backend_claim")
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, fixture)
        backend = self._backend()

        candidate = backend.list_recoverable(
            after_attempt_id=None,
            limit=10,
        )[0]
        claimed = backend.claim_recovery(candidate)
        time.sleep(0.01)
        renewed = backend.renew_claim(claimed, lease_seconds=120)

        self.assertEqual(claimed.claim_generation, 1)
        self.assertEqual(claimed.claim_owner_identity, "worker-integration-boot")
        self.assertGreater(
            renewed.lease_expires_monotonic or 0,
            claimed.lease_expires_monotonic or 0,
        )
        with self.engine.begin() as conn:
            session = Session(bind=conn, expire_on_commit=False, future=True)
            try:
                repository = RemoteParseV4Repository(session)
                authority = repository.load(fixture.attempt_id)
                repository.append_successor(
                    V4SuccessorAppend(
                        claim=authority.claim_witness,
                        successor=fixture.reconciling,
                        new_evidence=(
                            encode_remote_parse_evidence_v4(fixture.submission),
                        ),
                    )
                )
            finally:
                session.close()

        reloaded = backend.reload_claim(renewed)
        self.assertEqual(reloaded.state, "reconciling")
        self.assertEqual(reloaded.lifecycle_version, 1)
        self.assertEqual(reloaded.claim_generation, 1)

    def test_live_foreign_claim_is_deferred_without_writing(self) -> None:
        fixture = build_v4_authority_fixture(attempt_id="rpa_backend_foreign")
        with self.engine.begin() as conn:
            install_submitted_cycle(conn, fixture, include_secret=True)
            before = conn.execute(
                sa.text(
                    "SELECT claim_generation,claim_owner_identity,claim_lease_until "
                    "FROM disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": fixture.attempt_id},
            ).one()
        backend = self._backend()
        candidate = backend.list_recoverable(
            after_attempt_id=None,
            limit=10,
        )[0]

        with self.assertRaises(RecoveryDeferred) as raised:
            backend.claim_recovery(candidate)

        self.assertEqual(
            raised.exception.durable_work.claim_owner_identity,
            "worker-test",
        )
        with self.engine.begin() as conn:
            after = conn.execute(
                sa.text(
                    "SELECT claim_generation,claim_owner_identity,claim_lease_until "
                    "FROM disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": fixture.attempt_id},
            ).one()
        self.assertEqual(after, before)

    def test_claim_response_loss_is_closed_by_fresh_database_reload(self) -> None:
        fixture = build_v4_authority_fixture(attempt_id="rpa_backend_loss")
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, fixture)
        lose_once = [True]

        def response_loss_factory() -> UnitOfWork:
            return _CommitResponseLostUnitOfWork(
                SqlAlchemyUnitOfWork(engine=self.engine),
                lose_once,
            )  # type: ignore[return-value]

        backend = self._backend(factory=response_loss_factory)
        candidate = backend.list_recoverable(
            after_attempt_id=None,
            limit=10,
        )[0]

        claimed = backend.claim_recovery(candidate)

        self.assertFalse(lose_once[0])
        self.assertEqual(claimed.claim_generation, 1)
        self.assertEqual(claimed.claim_owner_identity, "worker-integration-boot")
        with self.engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT claim_generation,claim_owner_identity "
                    "FROM disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": fixture.attempt_id},
            ).one()
        self.assertEqual(tuple(row), (1, "worker-integration-boot"))

    def test_renew_response_loss_is_closed_by_fresh_database_reload(self) -> None:
        fixture = build_v4_authority_fixture(attempt_id="rpa_backend_renew_loss")
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, fixture)
        backend = self._backend()
        candidate = backend.list_recoverable(
            after_attempt_id=None,
            limit=10,
        )[0]
        claimed = backend.claim_recovery(candidate)
        time.sleep(0.01)
        lose_once = [True]

        def response_loss_factory() -> UnitOfWork:
            return _CommitResponseLostUnitOfWork(
                SqlAlchemyUnitOfWork(engine=self.engine),
                lose_once,
            )  # type: ignore[return-value]

        renewed = self._backend(factory=response_loss_factory).renew_claim(
            claimed,
            lease_seconds=120,
        )

        self.assertFalse(lose_once[0])
        self.assertGreater(
            renewed.lease_expires_monotonic or 0,
            claimed.lease_expires_monotonic or 0,
        )

    def test_admission_claims_runtime_activated_generation_zero_superseder(self) -> None:
        source = build_v4_authority_fixture(attempt_id="rpa_backend_source")
        supersession = build_v4_resource_free_supersession_fixture(source)
        with self.engine.begin() as conn:
            install_v4_resource_free_supersession(conn, supersession)
        backend = self._backend()

        blocked = backend.admit_new(
            limit=2,
            available_credits=ResourceCreditVector(),
        )
        self.assertEqual(blocked.work, ())
        self.assertTrue(blocked.backlog_exists)
        self.assertIn("documents", blocked.blocked_dimensions)
        with self.engine.begin() as conn:
            untouched_generation = conn.execute(
                sa.text(
                    "SELECT claim_generation FROM "
                    "disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": supersession.target.attempt_id},
            ).scalar_one()
        self.assertEqual(untouched_generation, 0)

        admitted = backend.admit_new(
            limit=2,
            available_credits=_limits().credits,
        )

        self.assertEqual(
            tuple(item.attempt_id for item in admitted.work),
            (supersession.target.attempt_id,),
        )
        self.assertFalse(admitted.backlog_exists)
        self.assertEqual(admitted.work[0].claim_generation, 1)
        self.assertEqual(
            admitted.work[0].claim_owner_identity,
            "worker-integration-boot",
        )
        second = backend.admit_new(
            limit=2,
            available_credits=_limits().credits,
        )
        self.assertEqual(second.work, ())
        self.assertFalse(second.backlog_exists)


if __name__ == "__main__":
    unittest.main()
