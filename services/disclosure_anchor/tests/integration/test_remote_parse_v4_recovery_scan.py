"""Scratch-PostgreSQL tests for the side-effect-free V4 recovery scan."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
import unittest

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres.remote_parse_v4_repository import (
    RemoteParseV4Repository,
)
from disclosure_anchor.adapters.db.postgres.repositories import (
    RemoteParseAttemptRepository,
)
from disclosure_anchor.application.contracts.remote_parse_checkpoint import (
    RemoteParseCheckpointConflict,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RecoveryCandidate,
    V4HeadExpectation,
)
from tests.integration._remote_parse_v4_factory import (
    V4AuthorityFixture,
    append_remote_failed_tail,
    build_v4_authority_fixture,
    insert_core_rows,
    insert_legacy_head,
    install_prepared_cycle,
    install_remote_failed_without_secret,
    install_submitted_cycle,
)
from tests.integration._support import engine_or_skip


class RemoteParseV4RecoveryScanIntegrationTests(unittest.TestCase):
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

    @staticmethod
    def _repository(conn: Connection) -> tuple[Session, RemoteParseV4Repository]:
        session = Session(bind=conn, expire_on_commit=False, future=True)
        return session, RemoteParseV4Repository(session)

    def _scan(
        self,
        *,
        after_attempt_id: str | None = None,
        limit: int = 100,
    ) -> tuple[RecoveryCandidate, ...]:
        with self.engine.begin() as conn:
            session, repository = self._repository(conn)
            try:
                return repository.list_recoverable_heads(
                    after_attempt_id=after_attempt_id,
                    limit=limit,
                )
            finally:
                session.close()

    @staticmethod
    def _install_prepared(
        conn: Connection,
        attempt_id: str,
    ) -> V4AuthorityFixture:
        fixture = build_v4_authority_fixture(attempt_id=attempt_id)
        install_prepared_cycle(conn, fixture)
        return fixture

    def test_empty_short_and_byte_ordered_keyset_pages_are_exact(self) -> None:
        self.assertEqual(self._scan(limit=3), ())
        attempt_ids = ("rpa_A", "rpa_Z", "rpa__", "rpa_a", "rpa_0")
        with self.engine.begin() as conn:
            for attempt_id in reversed(attempt_ids):
                self._install_prepared(conn, attempt_id)

        observed: list[str] = []
        cursor: str | None = None
        page_lengths: list[int] = []
        while True:
            page = self._scan(after_attempt_id=cursor, limit=2)
            page_lengths.append(len(page))
            if not page:
                break
            identities = [item.attempt_id for item in page]
            self.assertEqual(identities, sorted(identities))
            if cursor is not None:
                self.assertGreater(identities[0], cursor)
            observed.extend(identities)
            cursor = identities[-1]
            if len(page) < 2:
                break

        self.assertEqual(observed, sorted(attempt_ids))
        self.assertEqual(page_lengths, [2, 2, 1])
        assert cursor is not None
        self.assertEqual(self._scan(after_attempt_id=cursor, limit=2), ())

    def test_unclaimed_live_and_expired_heads_share_one_database_clock(self) -> None:
        unclaimed = build_v4_authority_fixture(attempt_id="rpa_1-unclaimed")
        live = build_v4_authority_fixture(attempt_id="rpa_2-live")
        expired = build_v4_authority_fixture(attempt_id="rpa_3-expired")
        same_lease = build_v4_authority_fixture(attempt_id="rpa_4-same-lease")
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, unclaimed)
            install_submitted_cycle(conn, live, include_secret=True)
            install_submitted_cycle(conn, expired, include_secret=True)
            install_submitted_cycle(conn, same_lease, include_secret=True)
            conn.execute(
                sa.text(
                    "UPDATE disclosure_ops.remote_parse_attempt "
                    "SET claim_lease_until=clock_timestamp()-interval '1 second' "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": expired.attempt_id},
            )
            conn.execute(
                sa.text(
                    "UPDATE disclosure_ops.remote_parse_attempt target "
                    "SET claim_lease_until=source.claim_lease_until "
                    "FROM disclosure_ops.remote_parse_attempt source "
                    "WHERE target.attempt_id=:target_id "
                    "AND source.attempt_id=:source_id"
                ),
                {
                    "target_id": same_lease.attempt_id,
                    "source_id": live.attempt_id,
                },
            )

        candidates = {item.attempt_id: item for item in self._scan()}
        self.assertIsNone(candidates[unclaimed.attempt_id].claim_owner_identity)
        self.assertIsNone(candidates[unclaimed.attempt_id].lease_remaining_seconds)
        live_remaining = candidates[live.attempt_id].lease_remaining_seconds
        expired_remaining = candidates[expired.attempt_id].lease_remaining_seconds
        self.assertIsNotNone(live_remaining)
        self.assertIsNotNone(expired_remaining)
        assert live_remaining is not None
        assert expired_remaining is not None
        self.assertGreater(live_remaining, 0)
        self.assertLessEqual(live_remaining, 3600)
        self.assertLess(expired_remaining, 0)
        self.assertEqual(
            candidates[same_lease.attempt_id].lease_remaining_seconds,
            live_remaining,
        )

    def test_filtering_precedes_limit_and_legacy_heads_are_ignored(self) -> None:
        legacy_before = build_v4_authority_fixture(attempt_id="rpa_0-legacy")
        eligible_one = build_v4_authority_fixture(attempt_id="rpa_1-v4")
        legacy_middle = build_v4_authority_fixture(attempt_id="rpa_2-legacy")
        eligible_two = build_v4_authority_fixture(attempt_id="rpa_3-v4")
        final_v4 = build_v4_authority_fixture(attempt_id="rpa_4-final")
        with self.engine.begin() as conn:
            insert_core_rows(conn, legacy_before)
            insert_legacy_head(conn, legacy_before)
            install_prepared_cycle(conn, eligible_one)
            insert_core_rows(conn, legacy_middle)
            insert_legacy_head(conn, legacy_middle)
            install_prepared_cycle(conn, eligible_two)
            install_remote_failed_without_secret(conn, final_v4)

        self.assertEqual(
            tuple(item.attempt_id for item in self._scan(limit=1)),
            (eligible_one.attempt_id,),
        )
        self.assertEqual(
            tuple(item.attempt_id for item in self._scan(limit=2)),
            (eligible_one.attempt_id, eligible_two.attempt_id),
        )
        self.assertEqual(len(self._scan(limit=3)), 2)

        with self.engine.begin() as conn:
            session = Session(bind=conn, expire_on_commit=False, future=True)
            try:
                with self.assertRaises(RemoteParseCheckpointConflict):
                    RemoteParseAttemptRepository(session).list_recoverable(
                        after_attempt_id=None,
                        limit=10,
                    )
            finally:
                session.close()

    def test_uncommitted_claim_does_not_block_or_leak_into_scan(self) -> None:
        fixture = build_v4_authority_fixture(attempt_id="rpa_claim-race")
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, fixture)
            session, repository = self._repository(conn)
            try:
                expectation = V4HeadExpectation.from_authority(
                    repository.load(fixture.attempt_id)
                )
            finally:
                session.close()

        claim_written = Event()
        release_claim = Event()

        def hold_uncommitted_claim() -> None:
            with self.engine.begin() as conn:
                session, repository = self._repository(conn)
                try:
                    repository.claim(
                        expectation,
                        owner_identity="worker-uncommitted",
                        lease_seconds=30,
                    )
                    claim_written.set()
                    if not release_claim.wait(timeout=5):
                        raise AssertionError("claim test release timed out")
                finally:
                    session.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            claim_future = executor.submit(hold_uncommitted_claim)
            self.assertTrue(claim_written.wait(timeout=3))
            started = time.monotonic()
            scan_future = executor.submit(self._scan, limit=10)
            try:
                during = scan_future.result(timeout=2)
            finally:
                release_claim.set()
            claim_future.result(timeout=3)

        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(during), 1)
        self.assertEqual(during[0].claim_generation, 0)
        self.assertIsNone(during[0].claim_owner_identity)
        after = self._scan(limit=10)
        self.assertEqual(after[0].claim_generation, 1)
        self.assertEqual(after[0].claim_owner_identity, "worker-uncommitted")

    def test_finalization_between_pages_is_observed_without_stale_locking(self) -> None:
        first = build_v4_authority_fixture(attempt_id="rpa_1-first")
        finalizing = build_v4_authority_fixture(attempt_id="rpa_2-finalizing")
        last = build_v4_authority_fixture(attempt_id="rpa_3-last")
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, first)
            install_submitted_cycle(conn, finalizing, include_secret=True)
            install_prepared_cycle(conn, last)

        page_one = self._scan(limit=1)
        self.assertEqual(page_one[0].attempt_id, first.attempt_id)
        with self.engine.begin() as conn:
            append_remote_failed_tail(conn, finalizing)
            deleted = conn.execute(
                sa.text(
                    "SELECT disclosure_ops.purge_remote_parse_v4_secrets_final("
                    ":attempt_id,:fence,:version,:checkpoint_sha,:revision)"
                ),
                {
                    "attempt_id": finalizing.attempt_id,
                    "fence": finalizing.fence_identity,
                    "version": finalizing.remote_failed.lifecycle_version,
                    "checkpoint_sha": finalizing.remote_failed.sha256,
                    "revision": finalizing.sealed_secret.encryption_revision,
                },
            ).scalar_one()
            self.assertEqual(deleted, 1)

        page_two = self._scan(after_attempt_id=first.attempt_id, limit=2)
        self.assertEqual(
            tuple(item.attempt_id for item in page_two),
            (last.attempt_id,),
        )

    def test_scan_is_side_effect_free_and_does_not_hold_a_row_lock(self) -> None:
        fixture = build_v4_authority_fixture(attempt_id="rpa_lock-free")
        with self.engine.begin() as conn:
            install_prepared_cycle(conn, fixture)
            before = conn.execute(
                sa.text(
                    "SELECT state,row_version,claim_generation,"
                    "claim_owner_identity,claim_lease_until,updated_at "
                    "FROM disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": fixture.attempt_id},
            ).one()

        with self.engine.connect() as scan_conn:
            transaction = scan_conn.begin()
            session, repository = self._repository(scan_conn)
            try:
                self.assertEqual(len(repository.list_recoverable_heads(
                    after_attempt_id=None,
                    limit=10,
                )), 1)
                with self.engine.begin() as lock_conn:
                    locked = lock_conn.execute(
                        sa.text(
                            "SELECT attempt_id FROM "
                            "disclosure_ops.remote_parse_attempt "
                            "WHERE attempt_id=:attempt_id FOR UPDATE NOWAIT"
                        ),
                        {"attempt_id": fixture.attempt_id},
                    ).scalar_one()
                    self.assertEqual(locked, fixture.attempt_id)
            finally:
                session.close()
                transaction.rollback()

        with self.engine.begin() as conn:
            after = conn.execute(
                sa.text(
                    "SELECT state,row_version,claim_generation,"
                    "claim_owner_identity,claim_lease_until,updated_at "
                    "FROM disclosure_ops.remote_parse_attempt "
                    "WHERE attempt_id=:attempt_id"
                ),
                {"attempt_id": fixture.attempt_id},
            ).one()
        self.assertEqual(after, before)

    def test_invalid_arguments_are_rejected(self) -> None:
        with self.engine.begin() as conn:
            session, repository = self._repository(conn)
            try:
                for limit in (0, 1001, True, 1.5):
                    with self.subTest(limit=limit):
                        with self.assertRaises(ValueError):
                            repository.list_recoverable_heads(
                                after_attempt_id=None,
                                limit=limit,  # type: ignore[arg-type]
                            )
                for cursor in ("", "   ", 1):
                    with self.subTest(cursor=cursor):
                        with self.assertRaises(ValueError):
                            repository.list_recoverable_heads(
                                after_attempt_id=cursor,  # type: ignore[arg-type]
                                limit=1,
                            )
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
