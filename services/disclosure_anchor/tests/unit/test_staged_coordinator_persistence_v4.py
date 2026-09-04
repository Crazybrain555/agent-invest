"""No-database tests for the V4 coordinator persistence slice."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import unittest

from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    advance_remote_parse_checkpoint_v4,
    build_initial_remote_parse_checkpoint_v4,
    build_resource_reservation_v4,
)
from disclosure_anchor.application.contracts.staged_credit import (
    DatabaseLeaseSnapshot,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
    ResourceReservationInput,
    encode_resource_reservation_input,
)
from disclosure_anchor.application.ports.remote_parse_v4_repository import (
    RecoveryCandidate,
    RemoteParseV4Authority,
    V4ClaimHeldByOther,
    V4ClaimLost,
    V4HeadExpectation,
    V4HeadNotFound,
    V4HeadStale,
    V4SuccessorAppend,
    V4SuccessorNotCommitted,
    V4SuccessorReconciliation,
)
from disclosure_anchor.application.ports.staged_provider_parser import V4ClaimWitness
from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import (
    DurableStagedCoordinatorPersistenceV4,
    DurableV4ClaimGuard,
    StagedClaimLost,
    StagedLeaseNotRunnable,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    AdmissionInterrupted,
    CoordinatorLimits,
    RecoveryDeferred,
)


def _sha(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _prepared_authority(
    attempt_id: str,
    *,
    snapshot_bytes: int,
    owner: str | None = None,
    generation: int = 0,
    lease_seconds: int = 60,
    database_now: datetime,
) -> RemoteParseV4Authority:
    document_id = f"doc-{attempt_id}"
    processing_run_id = f"run-{attempt_id}"
    fence_identity = f"fence-{attempt_id}"
    source_sha = _sha(f"{attempt_id}:source")
    profile_sha = _sha(f"{attempt_id}:profile")
    policy_sha = _sha(f"{attempt_id}:policy")
    reservation_credit = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=snapshot_bytes,
        remote_waits=1,
        provider_tasks=1,
        provider_result_bytes=20,
        materialization_items=1,
        compressed_bytes=20,
        decoded_bytes=65_536,
        temp_disk_bytes=131_072,
        output_items=1,
        output_bytes=65_536,
        output_pages=2,
        ack_items=1,
    )
    reservation_input = encode_resource_reservation_input(
        ResourceReservationInput(
            source_pdf_sha256=source_sha,
            source_byte_count=snapshot_bytes,
            source_page_count=2,
            process_profile_sha256=profile_sha,
            credit_policy_sha256=policy_sha,
            bucket="regular",
            reservation=reservation_credit,
        )
    )
    reservation = build_resource_reservation_v4(
        attempt_id=attempt_id,
        attempt_generation=1,
        fence_identity=fence_identity,
        document_id=document_id,
        processing_run_id=processing_run_id,
        source_pdf_sha256=source_sha,
        source_byte_count=snapshot_bytes,
        source_page_count=2,
        prepared_submission_identity_sha256=_sha(f"{attempt_id}:prepared"),
        request_sha256=_sha(f"{attempt_id}:request"),
        runtime_epoch_sha256=_sha(f"{attempt_id}:epoch"),
        process_profile_sha256=profile_sha,
        credit_policy_sha256=policy_sha,
        reservation_bucket="regular",
        reservation_input_sha256=reservation_input.sha256,
        reserved_credit=reservation_credit,
    )
    checkpoint = build_initial_remote_parse_checkpoint_v4(
        reservation=reservation,
        preparation_intent_sha256=_sha(f"{attempt_id}:preparation-intent"),
        snapshot_receipt_sha256=_sha(f"{attempt_id}:snapshot-receipt"),
        held_resource_credit=ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=snapshot_bytes,
        ),
    )
    lease_until = (
        database_now + timedelta(seconds=lease_seconds)
        if owner is not None
        else None
    )
    database_lease = (
        DatabaseLeaseSnapshot(
            database_observed_at_utc=database_now,
            lease_until_utc=lease_until,
            remaining_microseconds=lease_seconds * 1_000_000,
        )
        if lease_until is not None
        else None
    )
    return RemoteParseV4Authority(
        attempt_id=attempt_id,
        processing_run_id=processing_run_id,
        document_id=document_id,
        attempt_generation=1,
        fence_identity=fence_identity,
        source_pdf_sha256=source_sha,
        parser_target_sha256=_sha(f"{attempt_id}:parser"),
        request_sha256=reservation.request_sha256,
        runtime_epoch_sha256=reservation.runtime_epoch_sha256,
        client_submit_key=f"submit-{attempt_id}",
        state="prepared",
        is_current=True,
        lifecycle_version=0,
        checkpoint_sha256=checkpoint.sha256,
        claim_generation=generation,
        claim_owner_identity=owner,
        claim_lease_until=lease_until,
        checkpoint_history=(checkpoint,),
        reservation=reservation,
        evidence=(),
        publication_winner=None,
        secret_history=(),
        source_supersession_link=None,
        staged_by_link=None,
        database_lease=database_lease,
    )


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        value = self.value
        self.value += 0.001
        return value


class _SequenceClock:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    def __call__(self) -> float:
        if not self.values:
            raise AssertionError("test monotonic sequence was exhausted")
        return self.values.pop(0)


class _Repository:
    def __init__(self, heads: tuple[RemoteParseV4Authority, ...]) -> None:
        self.heads = {head.attempt_id: head for head in heads}
        self.database_now = datetime(2026, 9, 4, tzinfo=UTC)
        self.recovery_list_calls = 0
        self.admission_list_calls = 0
        self.claim_calls = 0
        self.renew_calls = 0
        self.load_fail_attempt_id: str | None = None
        self.claim_generation_overrides: dict[str, int] = {}
        self.admission_list_fail_after: int | None = None

    def _observed(self, authority: RemoteParseV4Authority) -> RemoteParseV4Authority:
        if authority.claim_lease_until is None:
            return replace(authority, database_lease=None)
        delta = authority.claim_lease_until - self.database_now
        remaining = (
            delta.days * 86_400_000_000
            + delta.seconds * 1_000_000
            + delta.microseconds
        )
        return replace(
            authority,
            database_lease=DatabaseLeaseSnapshot(
                database_observed_at_utc=self.database_now,
                lease_until_utc=authority.claim_lease_until,
                remaining_microseconds=remaining,
            ),
        )

    def load(self, attempt_id: str) -> RemoteParseV4Authority:
        if attempt_id == self.load_fail_attempt_id:
            raise RuntimeError("fake durable load failure")
        try:
            return self._observed(self.heads[attempt_id])
        except KeyError as exc:
            raise V4HeadNotFound("missing fake V4 head") from exc

    def list_recoverable_heads(
        self,
        *,
        after_attempt_id: str | None,
        limit: int,
    ) -> tuple[RecoveryCandidate, ...]:
        self.recovery_list_calls += 1
        rows = [
            self._candidate(self._observed(head))
            for head in self.heads.values()
            if head.is_current
            and (after_attempt_id is None or head.attempt_id > after_attempt_id)
        ]
        return tuple(sorted(rows, key=lambda item: item.attempt_id)[:limit])

    def list_unclaimed_prepared_heads(
        self,
        *,
        after_attempt_id: str | None,
        limit: int,
    ) -> tuple[RecoveryCandidate, ...]:
        self.admission_list_calls += 1
        if (
            self.admission_list_fail_after is not None
            and self.admission_list_calls > self.admission_list_fail_after
        ):
            raise RuntimeError("fake admission page failure")
        rows = [
            self._candidate(self._observed(head))
            for head in self.heads.values()
            if head.is_current
            and head.state == "prepared"
            and head.lifecycle_version == 0
            and head.claim_generation == 0
            and head.claim_owner_identity is None
            and (after_attempt_id is None or head.attempt_id > after_attempt_id)
        ]
        return tuple(sorted(rows, key=lambda item: item.attempt_id)[:limit])

    @staticmethod
    def _candidate(authority: RemoteParseV4Authority) -> RecoveryCandidate:
        remaining = (
            authority.database_lease.remaining_microseconds / 1_000_000
            if authority.database_lease is not None
            else None
        )
        return RecoveryCandidate(
            attempt_id=authority.attempt_id,
            state=authority.state,
            lifecycle_version=authority.lifecycle_version,
            claim_generation=authority.claim_generation,
            claim_owner_identity=authority.claim_owner_identity,
            lease_remaining_seconds=remaining,
        )

    def claim(
        self,
        expectation: V4HeadExpectation,
        *,
        owner_identity: str,
        lease_seconds: int,
    ) -> RemoteParseV4Authority:
        self.claim_calls += 1
        if expectation.attempt_id in self.claim_generation_overrides:
            generation = self.claim_generation_overrides.pop(expectation.attempt_id)
            self.heads[expectation.attempt_id] = replace(
                self.heads[expectation.attempt_id],
                claim_generation=generation,
                claim_owner_identity="expired-worker",
                claim_lease_until=self.database_now - timedelta(seconds=1),
            )
        authority = self.load(expectation.attempt_id)
        if (
            authority.fence_identity != expectation.fence_identity
            or authority.state != expectation.state
            or authority.lifecycle_version != expectation.lifecycle_version
            or authority.checkpoint_sha256 != expectation.checkpoint_sha256
        ):
            raise V4HeadStale("fake claim head changed")
        live = (
            authority.database_lease is not None
            and authority.database_lease.remaining_microseconds > 0
        )
        if live and authority.claim_owner_identity != owner_identity:
            raise V4ClaimHeldByOther("fake live foreign claim")
        generation = (
            authority.claim_generation
            if live and authority.claim_owner_identity == owner_identity
            else authority.claim_generation + 1
        )
        lease_until = self.database_now + timedelta(seconds=lease_seconds)
        claimed = replace(
            authority,
            claim_generation=generation,
            claim_owner_identity=owner_identity,
            claim_lease_until=lease_until,
            database_lease=DatabaseLeaseSnapshot(
                database_observed_at_utc=self.database_now,
                lease_until_utc=lease_until,
                remaining_microseconds=lease_seconds * 1_000_000,
            ),
        )
        self.heads[authority.attempt_id] = claimed
        return claimed

    def renew(self, claim: object, *, lease_seconds: int) -> RemoteParseV4Authority:
        self.renew_calls += 1
        attempt_id = getattr(claim, "attempt_id")
        authority = self.load(attempt_id)
        if (
            authority.claim_owner_identity != getattr(claim, "claim_owner_identity")
            or authority.claim_generation != getattr(claim, "claim_generation")
            or authority.state != getattr(claim, "state")
            or authority.lifecycle_version != getattr(claim, "lifecycle_version")
        ):
            raise V4ClaimLost("fake renewal lost claim")
        lease_until = self.database_now + timedelta(seconds=lease_seconds)
        if authority.claim_lease_until is not None:
            lease_until = max(authority.claim_lease_until, lease_until)
        renewed = replace(
            authority,
            claim_lease_until=lease_until,
            database_lease=DatabaseLeaseSnapshot(
                database_observed_at_utc=self.database_now,
                lease_until_utc=lease_until,
                remaining_microseconds=lease_seconds * 1_000_000,
            ),
        )
        self.heads[attempt_id] = renewed
        return renewed

    def reload_claimed(
        self,
        claim: V4ClaimWitness,
        *,
        lock_for_transition: bool = False,
    ) -> RemoteParseV4Authority:
        del lock_for_transition
        authority = self.load(claim.attempt_id)
        if (
            authority.claim_witness != claim
            or authority.database_lease is None
            or authority.database_lease.remaining_microseconds <= 0
        ):
            raise V4ClaimLost("fake claimed reload lost authority")
        return authority

    def append_successor(
        self,
        append: V4SuccessorAppend,
    ) -> RemoteParseV4Authority:
        authority = self.reload_claimed(append.claim)
        successor = append.successor
        if successor.previous_checkpoint_sha256 != authority.checkpoint_sha256:
            raise V4HeadStale("fake successor predecessor drifted")
        updated = replace(
            authority,
            state=successor.state,
            lifecycle_version=successor.lifecycle_version,
            checkpoint_sha256=successor.sha256,
            checkpoint_history=(*authority.checkpoint_history, successor),
            evidence=(*authority.evidence, *append.new_evidence),
            publication_winner=append.publication_winner,
            claim_owner_identity=(
                None if successor.state.endswith("failed") else authority.claim_owner_identity
            ),
            claim_lease_until=(
                None if successor.state.endswith("failed") else authority.claim_lease_until
            ),
            database_lease=(
                None if successor.state.endswith("failed") else authority.database_lease
            ),
        )
        self.heads[authority.attempt_id] = updated
        return updated

    def reconcile_successor(
        self,
        append: V4SuccessorAppend,
    ) -> V4SuccessorReconciliation:
        authority = self.load(append.claim.attempt_id)
        if authority.checkpoint == append.successor:
            return V4SuccessorReconciliation(
                authority=authority,
                authorization_still_live=(
                    authority.database_lease is not None
                    and authority.database_lease.remaining_microseconds > 0
                ),
            )
        if authority.checkpoint_sha256 == append.claim.checkpoint_sha256:
            raise V4SuccessorNotCommitted("fake successor is absent")
        raise V4HeadStale("fake different successor committed")


class _UnitOfWork:
    def __init__(self, owner: _Factory) -> None:
        self.remote_parse_v4 = owner.repository
        self._owner = owner

    def __enter__(self) -> _UnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        if self._owner.commit_losses > 0:
            self._owner.commit_losses -= 1
            raise RuntimeError("fake commit response lost after apply")


class _Factory:
    def __init__(self, repository: _Repository) -> None:
        self.repository = repository
        self.commit_losses = 0

    def __call__(self) -> _UnitOfWork:
        return _UnitOfWork(self)


def _limits() -> CoordinatorLimits:
    return CoordinatorLimits(
        credits=ResourceCreditVector(
            documents=8,
            snapshot_items=8,
            snapshot_bytes=1_000,
            remote_waits=8,
            provider_tasks=8,
            provider_result_bytes=1_000,
            materialization_items=8,
            compressed_bytes=1_000,
            decoded_bytes=1_000_000,
            temp_disk_bytes=2_000_000,
            output_items=8,
            output_bytes=1_000_000,
            output_pages=100,
            ack_items=8,
        ),
        recovery_page_size=2,
        poll_seconds=0.1,
    )


class StagedCoordinatorPersistenceV4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.database_now = datetime(2026, 9, 4, tzinfo=UTC)
        self.clock = _Clock()

    def _backend(
        self,
        repository: _Repository,
        factory: _Factory,
    ) -> DurableStagedCoordinatorPersistenceV4:
        return DurableStagedCoordinatorPersistenceV4(
            uow_factory=factory,  # type: ignore[arg-type]
            limits=_limits(),
            owner_identity="worker-boot-one",
            monotonic=self.clock,
        )

    def test_claim_then_fresh_backend_renews_without_process_cache(self) -> None:
        authority = _prepared_authority(
            "attempt-one",
            snapshot_bytes=100,
            database_now=self.database_now,
        )
        repository = _Repository((authority,))
        factory = _Factory(repository)
        claimed = self._backend(repository, factory).claim_recovery(
            repository._candidate(repository.load(authority.attempt_id))
        )
        repository.database_now += timedelta(seconds=1)

        renewed = self._backend(repository, factory).renew_claim(
            claimed,
            lease_seconds=120,
        )

        self.assertEqual(renewed.claim_generation, 1)
        self.assertEqual(renewed.claim_owner_identity, "worker-boot-one")
        self.assertGreater(
            renewed.lease_expires_monotonic or 0,
            claimed.lease_expires_monotonic or 0,
        )
        self.assertEqual(repository.claim_calls, 1)
        self.assertEqual(repository.renew_calls, 1)

    def test_foreign_live_claim_is_accounting_only_and_not_rewritten(self) -> None:
        authority = _prepared_authority(
            "attempt-foreign",
            snapshot_bytes=100,
            owner="other-worker",
            generation=3,
            lease_seconds=30,
            database_now=self.database_now,
        )
        repository = _Repository((authority,))
        factory = _Factory(repository)

        with self.assertRaises(RecoveryDeferred) as raised:
            self._backend(repository, factory).claim_recovery(
                repository._candidate(repository.load(authority.attempt_id))
            )

        self.assertEqual(raised.exception.durable_work.claim_owner_identity, "other-worker")
        self.assertGreaterEqual(raised.exception.retry_after_seconds, 30)
        self.assertEqual(repository.claim_calls, 0)
        self.assertEqual(repository.heads[authority.attempt_id].claim_generation, 3)

    def test_claim_commit_response_loss_closes_from_durable_head(self) -> None:
        authority = _prepared_authority(
            "attempt-response-loss",
            snapshot_bytes=100,
            database_now=self.database_now,
        )
        repository = _Repository((authority,))
        factory = _Factory(repository)
        factory.commit_losses = 1

        claimed = self._backend(repository, factory).claim_recovery(
            repository._candidate(repository.load(authority.attempt_id))
        )

        self.assertEqual(claimed.claim_generation, 1)
        self.assertEqual(claimed.claim_owner_identity, "worker-boot-one")
        self.assertEqual(repository.claim_calls, 1)

    def test_renew_commit_response_loss_closes_without_witness_cache(self) -> None:
        authority = _prepared_authority(
            "attempt-renew-loss",
            snapshot_bytes=100,
            database_now=self.database_now,
        )
        repository = _Repository((authority,))
        factory = _Factory(repository)
        backend = self._backend(repository, factory)
        claimed = backend.claim_recovery(
            repository._candidate(repository.load(authority.attempt_id))
        )
        factory.commit_losses = 1
        repository.database_now += timedelta(seconds=1)

        renewed = self._backend(repository, factory).renew_claim(
            claimed,
            lease_seconds=120,
        )

        self.assertGreater(
            renewed.lease_expires_monotonic or 0,
            claimed.lease_expires_monotonic or 0,
        )
        self.assertEqual(repository.renew_calls, 1)

    def test_claim_fails_closed_when_clock_bracket_consumes_lease(self) -> None:
        authority = _prepared_authority(
            "attempt-short-bracket",
            snapshot_bytes=100,
            database_now=self.database_now,
        )
        repository = _Repository((authority,))
        factory = _Factory(repository)
        clock = _SequenceClock([0.0, 0.0, 0.0, 121.0])
        backend = DurableStagedCoordinatorPersistenceV4(
            uow_factory=factory,  # type: ignore[arg-type]
            limits=_limits(),
            owner_identity="worker-boot-one",
            monotonic=clock,
        )

        with self.assertRaises(StagedLeaseNotRunnable):
            backend.claim_recovery(
                repository._candidate(repository.load(authority.attempt_id))
            )

    def test_reload_accepts_exact_one_step_successor_from_database(self) -> None:
        authority = _prepared_authority(
            "attempt-successor",
            snapshot_bytes=100,
            database_now=self.database_now,
        )
        repository = _Repository((authority,))
        factory = _Factory(repository)
        backend = self._backend(repository, factory)
        claimed = backend.claim_recovery(
            repository._candidate(repository.load(authority.attempt_id))
        )
        durable = repository.load(authority.attempt_id)
        successor = advance_remote_parse_checkpoint_v4(
            durable.checkpoint,
            state="reconciling",
            held_resource_credit=replace(
                durable.checkpoint.held_resource_credit,
                remote_waits=1,
            ),
            submission_intent_sha256=_sha("successor-submission"),
        )
        repository.heads[authority.attempt_id] = replace(
            durable,
            state=successor.state,
            lifecycle_version=successor.lifecycle_version,
            checkpoint_sha256=successor.sha256,
            checkpoint_history=(*durable.checkpoint_history, successor),
        )

        reloaded = backend.reload_claim(claimed)

        self.assertEqual(reloaded.state, "reconciling")
        self.assertEqual(reloaded.lifecycle_version, 1)
        self.assertEqual(reloaded.claim_generation, claimed.claim_generation)

    def test_successor_commit_response_loss_reloads_exact_durable_head(self) -> None:
        authority = _prepared_authority(
            "attempt-successor-loss",
            snapshot_bytes=100,
            database_now=self.database_now,
        )
        repository = _Repository((authority,))
        factory = _Factory(repository)
        backend = self._backend(repository, factory)
        claimed = backend.claim_recovery(
            repository._candidate(repository.load(authority.attempt_id))
        )
        durable = repository.load(authority.attempt_id)
        successor = advance_remote_parse_checkpoint_v4(
            durable.checkpoint,
            state="reconciling",
            held_resource_credit=replace(
                durable.checkpoint.held_resource_credit,
                remote_waits=1,
            ),
            submission_intent_sha256=_sha("successor-loss-submission"),
        )
        factory.commit_losses = 1

        updated = backend.append_successor(
            claimed,
            V4SuccessorAppend(
                claim=durable.claim_witness,
                successor=successor,
            ),
        )

        self.assertEqual(updated.state, "reconciling")
        self.assertEqual(updated.lifecycle_version, 1)
        self.assertEqual(repository.heads[authority.attempt_id].checkpoint, successor)

    def test_claim_guard_reloads_live_exact_head_under_resource_lock(self) -> None:
        authority = _prepared_authority(
            "attempt-guard",
            snapshot_bytes=100,
            database_now=self.database_now,
        )
        repository = _Repository((authority,))
        factory = _Factory(repository)
        backend = self._backend(repository, factory)
        backend.claim_recovery(
            repository._candidate(repository.load(authority.attempt_id))
        )
        durable = repository.load(authority.attempt_id)
        guard = DurableV4ClaimGuard(uow_factory=factory)  # type: ignore[arg-type]

        guard.assert_current_under_resource_lock(
            checkpoint=durable.checkpoint,
            claim=durable.claim_witness,
        )
        repository.database_now += timedelta(seconds=121)
        with self.assertRaises(V4ClaimLost):
            guard.assert_current_under_resource_lock(
                checkpoint=durable.checkpoint,
                claim=durable.claim_witness,
            )

    def test_reload_rejects_a_foreign_reclaim(self) -> None:
        authority = _prepared_authority(
            "attempt-reclaimed",
            snapshot_bytes=100,
            database_now=self.database_now,
        )
        repository = _Repository((authority,))
        factory = _Factory(repository)
        backend = self._backend(repository, factory)
        claimed = backend.claim_recovery(
            repository._candidate(repository.load(authority.attempt_id))
        )
        durable = repository.load(authority.attempt_id)
        repository.heads[authority.attempt_id] = replace(
            durable,
            claim_generation=durable.claim_generation + 1,
            claim_owner_identity="other-worker",
        )

        with self.assertRaises(StagedClaimLost):
            backend.reload_claim(claimed)

    def test_admission_bypasses_large_head_without_using_recovery_scan(self) -> None:
        large = _prepared_authority(
            "attempt-a-large",
            snapshot_bytes=200,
            database_now=self.database_now,
        )
        small = _prepared_authority(
            "attempt-b-small",
            snapshot_bytes=50,
            database_now=self.database_now,
        )
        repository = _Repository((large, small))
        factory = _Factory(repository)

        outcome = self._backend(repository, factory).admit_new(
            limit=2,
            available_credits=replace(
                _limits().credits,
                snapshot_items=2,
                snapshot_bytes=100,
            ),
        )

        self.assertEqual(
            tuple(item.attempt_id for item in outcome.work),
            (small.attempt_id,),
        )
        self.assertTrue(outcome.backlog_exists)
        self.assertIn("snapshot_bytes", outcome.blocked_dimensions)
        self.assertEqual(repository.recovery_list_calls, 0)
        self.assertGreaterEqual(repository.admission_list_calls, 1)
        self.assertEqual(repository.heads[large.attempt_id].claim_generation, 0)
        self.assertEqual(repository.heads[small.attempt_id].claim_generation, 1)

    def test_admission_accepts_a_later_generation_won_after_scan(self) -> None:
        authority = _prepared_authority(
            "attempt-raced",
            snapshot_bytes=50,
            database_now=self.database_now,
        )
        repository = _Repository((authority,))
        repository.claim_generation_overrides[authority.attempt_id] = 3
        factory = _Factory(repository)

        outcome = self._backend(repository, factory).admit_new(
            limit=1,
            available_credits=_limits().credits,
        )

        self.assertEqual(len(outcome.work), 1)
        self.assertEqual(outcome.work[0].claim_generation, 4)
        self.assertEqual(outcome.work[0].claim_owner_identity, "worker-boot-one")

    def test_admission_exposes_prior_claim_when_a_later_load_fails(self) -> None:
        first = _prepared_authority(
            "attempt-a",
            snapshot_bytes=50,
            database_now=self.database_now,
        )
        second = _prepared_authority(
            "attempt-b",
            snapshot_bytes=50,
            database_now=self.database_now,
        )
        repository = _Repository((first, second))
        repository.load_fail_attempt_id = second.attempt_id
        factory = _Factory(repository)

        with self.assertRaises(AdmissionInterrupted) as raised:
            self._backend(repository, factory).admit_new(
                limit=2,
                available_credits=_limits().credits,
            )

        self.assertEqual(
            tuple(work.attempt_id for work in raised.exception.claimed_work),
            (first.attempt_id,),
        )
        self.assertEqual(repository.heads[first.attempt_id].claim_generation, 1)
        self.assertEqual(repository.heads[second.attempt_id].claim_generation, 0)

    def test_admission_exposes_all_claims_when_the_next_page_fails(self) -> None:
        heads = tuple(
            _prepared_authority(
                f"attempt-{index}",
                snapshot_bytes=50,
                database_now=self.database_now,
            )
            for index in range(3)
        )
        repository = _Repository(heads)
        repository.admission_list_fail_after = 1
        factory = _Factory(repository)

        with self.assertRaises(AdmissionInterrupted) as raised:
            self._backend(repository, factory).admit_new(
                limit=3,
                available_credits=_limits().credits,
            )

        self.assertEqual(
            tuple(work.attempt_id for work in raised.exception.claimed_work),
            ("attempt-0", "attempt-1"),
        )
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertEqual(
            tuple(repository.heads[f"attempt-{index}"].claim_generation for index in range(3)),
            (1, 1, 0),
        )

    def test_admission_reports_an_unscanned_page_after_count_limit(self) -> None:
        heads = tuple(
            _prepared_authority(
                f"attempt-{index}",
                snapshot_bytes=50,
                database_now=self.database_now,
            )
            for index in range(3)
        )
        repository = _Repository(heads)
        factory = _Factory(repository)
        backend = self._backend(repository, factory)

        first = backend.admit_new(
            limit=2,
            available_credits=_limits().credits,
        )
        second = backend.admit_new(
            limit=2,
            available_credits=_limits().credits,
        )

        self.assertEqual(len(first.work), 2)
        self.assertTrue(first.backlog_exists)
        self.assertEqual(tuple(item.attempt_id for item in second.work), ("attempt-2",))
        self.assertFalse(second.backlog_exists)


if __name__ == "__main__":
    unittest.main()
