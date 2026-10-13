"""Coordinator stage grants: a verified need grows one attempt's reservation.

A synthetic backend raises ``StageResourceGrantRequired`` before its durable
commit exactly as the V4 backend does; its successors and renewals then carry
the durable reservation a real persistence projection would compute. Until
that commit the admitted grant is only an allowance: the persistence identity
checks, run here directly and on the in-memory V4 repository, still compare
the durable reservation exactly.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import unittest

from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import (
    DurableStagedCoordinatorPersistenceV4,
    StagedClaimLost,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorSnapshot,
    CoordinatorTerminal,
    CoordinatorWork,
    ResourceCreditVector,
    StageCapacityBlocked,
    StageLeaseGuard,
    StageResourceGrantRequired,
    StagedParseCoordinator,
)
from disclosure_anchor.application.services.worker_stop_latch import InProcessWorkerStopLatch
from tests.unit import test_staged_coordinator_persistence_v4 as _persistence
from tests.unit.test_staged_parse_coordinator import (
    _LIFECYCLE_RESERVATION,
    _Backend,
    _Clock,
    _limits,
    _work,
)

LARGE_RESULT = 5_000


def _grown(reservation: ResourceCreditVector, result: int) -> ResourceCreditVector:
    return replace(reservation, provider_result_bytes=result, compressed_bytes=result)


class _GrantBackend(_Backend):
    """Raises a grant requirement at the terminal poll until it is admitted.

    Renewals return the durable reservation (never an in-memory overlay), and
    the terminal successor carries the verified result bytes it committed.
    """

    def __init__(self, *, required: dict[str, ResourceCreditVector], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.required = required
        self.durable: dict[str, ResourceCreditVector] = {}
        self.requirements_raised: list[str] = []

    def _durable(self, work: CoordinatorWork) -> ResourceCreditVector:
        return self.durable.get(work.attempt_id, _LIFECYCLE_RESERVATION)

    def renew_claim(self, work: CoordinatorWork, *, lease_seconds: int) -> CoordinatorWork:
        renewed = super().renew_claim(work, lease_seconds=lease_seconds)
        return replace(renewed, credit_reservation=self._durable(work))

    def run_remote(
        self,
        work: CoordinatorWork,
        *,
        credit_allowance: ResourceCreditVector,
        stage_guard: StageLeaseGuard,
    ) -> CoordinatorWork:
        required = self.required.get(work.attempt_id)
        if work.state == "submitted" and required is not None and not required.fits(work.credit_reservation):
            self.requirements_raised.append(work.attempt_id)
            self.calls.append(f"grant_required:{work.attempt_id}")
            raise StageResourceGrantRequired("verified result exceeds estimate", required=required)
        updated = super().run_remote(work, credit_allowance=credit_allowance, stage_guard=stage_guard)
        if updated.state == "remote_terminal" and required is not None:
            # The durable terminal commit carries Z; the persistence
            # projection now computes the grown reservation.
            self.durable[work.attempt_id] = work.credit_reservation
            updated = replace(
                updated,
                credits=replace(updated.credits, provider_result_bytes=required.provider_result_bytes),
            )
            self._assert_credit_grant(work, updated, credit_allowance)
        return updated


class StageGrantCoordinatorTests(unittest.TestCase):
    def test_verified_result_beyond_its_estimate_is_granted_and_completes(self) -> None:
        backend = _GrantBackend(
            required={"attempt-1": _grown(_LIFECYCLE_RESERVATION, LARGE_RESULT)},
            new=(_work("attempt-1", "prepared"),),
        )
        result = StagedParseCoordinator(backend=backend, limits=_limits()).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(result.final_states, (("attempt-1", "acked"),))
        self.assertEqual(result.errors, ())
        self.assertEqual(result.credits_in_use, ResourceCreditVector())
        self.assertEqual(backend.requirements_raised, ["attempt-1"])
        # Submit, then the granted poll: the refused poll made no durable step.
        self.assertEqual(backend.remote_calls_by_attempt["attempt-1"], 2)
        self.assertNotIn("remote_failure", backend.outcome_by_attempt.values())

    def test_admission_yields_while_a_granted_result_waits_for_capacity(self) -> None:
        # A recovered terminal result already holds most of the result
        # dimension; the grant must wait for it, and new work must not take
        # the space it needs meanwhile.
        holder = replace(
            _work("attempt-0", "remote_terminal", 3),
            credit_reservation=_grown(_LIFECYCLE_RESERVATION, 6_000),
            credits=replace(_work("attempt-0", "remote_terminal", 3).credits, provider_result_bytes=6_000),
        )
        backend = _GrantBackend(
            required={"attempt-1": _grown(_LIFECYCLE_RESERVATION, LARGE_RESULT)},
            recoverable=(holder, _work("attempt-1", "submitted", 2)),
            new=(_work("attempt-2", "prepared"),),
        )
        backend.durable["attempt-0"] = holder.credit_reservation
        snapshots: list[CoordinatorSnapshot] = []
        result = StagedParseCoordinator(
            backend=backend,
            limits=_limits(credits=replace(_limits().credits, provider_result_bytes=10_000, compressed_bytes=10_000)),
            progress=snapshots.append,
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertEqual(
            dict(result.final_states),
            {"attempt-0": "acked", "attempt-1": "acked", "attempt-2": "acked"},
        )
        self.assertIn("stage_grant_waiting", {snapshot.blocked_reason for snapshot in snapshots})
        grant_request = backend.calls.index("grant_required:attempt-1")
        granted_poll = backend.calls.index("remote:attempt-1:submitted")
        self.assertLess(grant_request, granted_poll)
        # No admission ran between the requirement and the granted poll.
        self.assertFalse(any(
            call.startswith("admit:") for call in backend.calls[grant_request:granted_poll]
        ))

    def test_a_requirement_the_attempt_already_holds_opens_the_circuit(self) -> None:
        class Broken(_GrantBackend):
            def run_remote(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
                if work.state == "submitted":
                    raise StageResourceGrantRequired("bogus", required=work.credit_reservation)
                return super().run_remote(work, credit_allowance=credit_allowance, stage_guard=stage_guard)

        backend = Broken(required={}, recoverable=(_work("attempt-1", "submitted", 2),))
        snapshots: list[CoordinatorSnapshot] = []
        result = StagedParseCoordinator(
            backend=backend, limits=_limits(), progress=snapshots.append,
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertIn("stage_grant_contract_violation", {snapshot.blocked_reason for snapshot in snapshots})


class _NativeHoldBackend(_Backend):
    """A provider task under a native storage hold, as the V4 poll reports it."""

    def run_remote(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        if work.attempt_id == "held":
            self.calls.append(f"remote:{work.attempt_id}:{work.state}")
            raise StageCapacityBlocked(
                "native storage hold: hard_envelope_exceeded", dimensions=("hard_envelope_exceeded",),
            )
        return super().run_remote(work, credit_allowance=credit_allowance, stage_guard=stage_guard)


class SiteHoldStopTests(unittest.TestCase):
    def test_native_hold_stops_the_site_and_a_release_replays_the_same_cause_once(self) -> None:
        backend = _NativeHoldBackend(recoverable=(_work("held", "submitted", 2),))
        causes = []
        for _release in range(2):
            latch = InProcessWorkerStopLatch()
            before = len(backend.calls)
            result = StagedParseCoordinator(backend=backend, limits=_limits(), stop_control=latch).run(
                stop_requested=lambda: False,
            )
            cause = latch.first_cause()
            assert cause is not None, result
            causes.append(cause)
            self.assertEqual((result.terminal, result.termination_kind),
                             (CoordinatorTerminal.STUCK_OPEN_CIRCUIT, "public_stop"))
            self.assertEqual(result.final_states, ())
            self.assertEqual(result.credits_in_use, _work("held", "submitted", 2).credits)
            run_calls = backend.calls[before:]
            self.assertEqual(run_calls.count("remote:held:submitted"), 1)
            self.assertFalse(any(call.startswith(("cleanup:", "ack:")) for call in run_calls))
        self.assertEqual(
            {(item.kind, item.reason_code, item.attempt_id, item.lane, item.state_at_dispatch,
              item.exception_fingerprint) for item in causes},
            {("coordinator_circuit", "native_storage_hold", "held", "remote", "submitted",
              causes[0].exception_fingerprint)},
        )


_same_work = DurableStagedCoordinatorPersistenceV4._require_same_work
_continuity = DurableStagedCoordinatorPersistenceV4._require_reload_continuity


class _DurableIdentityBackend(_Backend):
    """A durable head model held to the V4 persistence identity rules.

    Every stage call, renewal and reload is compared with the durable head by
    the persistence's own checks, as ``load_owned_authority``, ``renew_claim``
    and ``reload_claim`` compare it; only a committed stage changes the head.
    A result beyond the H0 estimate asks for a grant at the terminal poll and
    again at local prepare, like the storage-era V4 backend.
    """

    def __init__(self, work: CoordinatorWork, *, needs: dict[str, ResourceCreditVector],
                 clock: _Clock) -> None:
        super().__init__(recoverable=(work,))
        self.clock = clock
        self.heads = {work.attempt_id: work}
        self.needs = needs
        self.requirements: list[str] = []
        self.identity_checks = 0

    def _owned(self, work: CoordinatorWork) -> None:
        _same_work(work, self.heads[work.attempt_id], ignore_lease=True)
        self.identity_checks += 1

    def _require(self, work: CoordinatorWork) -> None:
        need = self.needs.get(work.state)
        if need is not None and not need.fits(work.credit_reservation):
            self.requirements.append(work.state)
            self.calls.append(f"grant_required:{work.attempt_id}:{work.state}")
            # Time passes before the grant can run: its claim is renewed
            # while the grant is admitted but not yet durable.
            self.clock.advance(100)
            raise StageResourceGrantRequired("verified result beyond its estimate", required=need)

    def _commit(self, updated: CoordinatorWork) -> CoordinatorWork:
        self.heads[updated.attempt_id] = updated
        return updated

    def claim_recovery(self, candidate):  # type: ignore[no-untyped-def]
        return self._commit(super().claim_recovery(candidate))

    def renew_claim(self, work: CoordinatorWork, *, lease_seconds: int) -> CoordinatorWork:
        self._owned(work)
        renewed = replace(
            super().renew_claim(work, lease_seconds=lease_seconds),
            credit_reservation=work.durable_reservation, durable_credit_reservation=None,
        )
        return self._commit(renewed)

    def reload_claim(self, work: CoordinatorWork) -> CoordinatorWork:
        head = self.heads[work.attempt_id]
        _continuity(work, head)
        return head

    def run_remote(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        self._owned(work)
        self._require(work)
        return self._commit(super().run_remote(work, credit_allowance=credit_allowance, stage_guard=stage_guard))

    def prepare_local_io(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        self._owned(work)
        self._require(work)
        return self._commit(
            super().prepare_local_io(work, credit_allowance=credit_allowance, stage_guard=stage_guard),
        )

    def run_local(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        self._owned(work)
        return self._commit(super().run_local(work, credit_allowance=credit_allowance, stage_guard=stage_guard))

    def commit(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        self._owned(work)
        return self._commit(super().commit(work, credit_allowance=credit_allowance, stage_guard=stage_guard))

    def cleanup(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        self._owned(work)
        return self._commit(super().cleanup(work, credit_allowance=credit_allowance, stage_guard=stage_guard))

    def acknowledge(self, work, *, stage_guard):  # type: ignore[no-untyped-def]
        self._owned(work)
        return self._commit(super().acknowledge(work, stage_guard=stage_guard))


class DurableIdentityUnderGrantTests(unittest.TestCase):
    """The r3 failure shape: an admitted grant must never look like a lost claim."""

    def test_two_grants_with_renewals_keep_exact_durable_identity(self) -> None:
        terminal = _grown(_LIFECYCLE_RESERVATION, LARGE_RESULT)
        local = replace(terminal, decoded_bytes=3_000, temp_disk_bytes=9_000, output_bytes=2_000)
        clock = _Clock()
        backend = _DurableIdentityBackend(
            _work("attempt-1", "submitted", 2), needs={"submitted": terminal, "remote_terminal": local},
            clock=clock,
        )
        result = StagedParseCoordinator(backend=backend, limits=_limits(), monotonic=clock).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT, result.errors)
        self.assertEqual(result.final_states, (("attempt-1", "acked"),))
        self.assertEqual(result.errors, ())
        self.assertEqual(backend.requirements, ["submitted", "remote_terminal"])
        # Each grant was renewed under before its stage ran with it, and every
        # stage call passed the durable identity check.
        for state, stage in (("submitted", "remote:attempt-1:submitted"), ("remote_terminal", "local_prepare:attempt-1")):
            requested = backend.calls.index(f"grant_required:attempt-1:{state}")
            granted = backend.calls.index(stage, requested + 1)
            self.assertIn("renew:attempt-1", backend.calls[requested:granted])
        self.assertGreater(backend.identity_checks, 6)

    def test_reload_continuity_accepts_only_the_admitted_or_durable_reservation(self) -> None:
        durable = _work("attempt-1", "submitted", 2)
        admitted = _grown(durable.credit_reservation, LARGE_RESULT)
        overlaid = replace(durable, credit_reservation=admitted, durable_credit_reservation=durable.credit_reservation)
        _continuity(overlaid, durable)  # Unadvanced head: exactly the durable reservation.
        with self.assertRaises(StagedClaimLost):
            _continuity(overlaid, replace(durable, credit_reservation=admitted))
        successor = replace(_work("attempt-1", "remote_terminal", 3), credit_reservation=admitted)
        _continuity(overlaid, successor)  # The successor committed exactly the admitted grant.
        _continuity(overlaid, replace(successor, credit_reservation=durable.credit_reservation))
        with self.assertRaises(StagedClaimLost):
            _continuity(overlaid, replace(successor, credit_reservation=_grown(durable.credit_reservation, 7_000)))
        with self.assertRaises(StagedClaimLost):  # Without an admitted grant nothing may grow.
            _continuity(durable, successor)


class ActualPersistenceUnderGrantTests(unittest.TestCase):
    """The real persistence methods on the in-memory V4 repository (no database)."""

    def test_admitted_grant_is_owned_only_through_its_exact_durable_part(self) -> None:
        clock = _persistence._Clock()
        authority = _persistence._prepared_authority(
            "attempt-grant", snapshot_bytes=100, database_now=datetime(2026, 9, 4, tzinfo=UTC),
        )
        repository = _persistence._Repository((authority,))
        persistence = DurableStagedCoordinatorPersistenceV4(
            uow_factory=_persistence._Factory(repository),  # type: ignore[arg-type]
            limits=_persistence._limits(), owner_identity="worker-boot-one", monotonic=clock,
        )
        claimed = persistence.claim_recovery(repository._candidate(repository.load(authority.attempt_id)))
        durable = claimed.credit_reservation
        admitted = replace(durable, provider_result_bytes=durable.provider_result_bytes + 4_096,
                           compressed_bytes=durable.compressed_bytes + 4_096)
        overlaid = replace(claimed, credit_reservation=admitted, durable_credit_reservation=durable)

        persistence.load_owned_authority(overlaid)
        repository.database_now += timedelta(seconds=1)
        renewed = persistence.renew_claim(overlaid, lease_seconds=120)
        self.assertEqual((renewed.credit_reservation, renewed.durable_credit_reservation), (durable, None))
        self.assertEqual(persistence.reload_claim(overlaid).credit_reservation, durable)

        overlaid = replace(renewed, credit_reservation=admitted, durable_credit_reservation=durable)
        for label, forged in (
            ("admitted grant presented as identity", replace(renewed, credit_reservation=admitted)),
            ("wrong durable reservation beneath", replace(overlaid, durable_credit_reservation=replace(
                durable, snapshot_bytes=durable.snapshot_bytes - 1,
            ))),
        ):
            with self.subTest(label):
                with self.assertRaises(StagedClaimLost):
                    persistence.load_owned_authority(forged)
                with self.assertRaises(StagedClaimLost):
                    persistence.renew_claim(forged, lease_seconds=120)
        with self.assertRaises(StagedClaimLost):  # A durable projection is never an overlay.
            _same_work(renewed, overlaid, ignore_lease=True)
        with self.assertRaises(ValueError):  # A grant never shrinks the durable reservation.
            replace(renewed, durable_credit_reservation=admitted)


class LocalStageBoundTests(unittest.TestCase):
    def test_local_stage_uses_the_frozen_decode_bound_with_claim_renewal(self) -> None:
        clock = _Clock()
        seen: dict[str, tuple[float, float | None]] = {}

        class Recording(_Backend):
            def run_local(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
                seen["local"] = (stage_guard.deadline_monotonic - clock(), stage_guard.claim_deadline_monotonic)
                return super().run_local(work, credit_allowance=credit_allowance, stage_guard=stage_guard)

            def prepare_local_io(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
                seen["local_prepare"] = (
                    stage_guard.deadline_monotonic - clock(), stage_guard.claim_deadline_monotonic,
                )
                return super().prepare_local_io(work, credit_allowance=credit_allowance, stage_guard=stage_guard)

        backend = Recording(recoverable=(_work("attempt-1", "remote_terminal", 3),))
        backend.clock = clock
        limits = _limits(local_stage_seconds=900.0)
        result = StagedParseCoordinator(backend=backend, limits=limits, monotonic=clock).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        local_budget, local_claim = seen["local"]
        self.assertAlmostEqual(local_budget, 900.0, delta=1.0)
        self.assertIsNotNone(local_claim)
        prepare_budget, prepare_claim = seen["local_prepare"]
        self.assertAlmostEqual(prepare_budget, limits.max_stage_step_seconds, delta=1.0)
        self.assertIsNone(prepare_claim)
        with self.assertRaises(ValueError):
            _limits(local_stage_seconds=limits.max_stage_step_seconds - 1)


if __name__ == "__main__":
    unittest.main()
