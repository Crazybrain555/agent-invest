"""Independent regression cases for cold COMMIT work spanning claim leases.

Clock movement is explicit; production coordinator and stage threads still run.
No database, provider process, GPU or real-minute waits are involved.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from disclosure_anchor.application.contracts.staged_worker_profile_v4 import (
    StagedWorkerProfileV4, decode_staged_worker_profile_v4,
)
from disclosure_anchor.application.services.staged_execution_guard import (
    StageLeaseGuard, StageLeaseLost,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorResult, CoordinatorTerminal, StagedParseCoordinator,
)
from disclosure_anchor.application.services.staged_v4_capacity import (
    PRODUCTION_MAX_STAGE_STEP_SECONDS,
    staged_v4_coordinator_limits,
)
from disclosure_anchor.settings import StagedV4Settings, load_settings, load_staged_v4_settings
from tests.unit.test_mineru_process_profile import _profile
from tests.unit.test_settings import _env
from tests.unit.test_staged_parse_coordinator import _Backend, _Clock, _limits, _work


_V1_BYTES = (
    b'{"admission_probe_milliseconds":1000,"contract_version":"staged-worker-composition.v1",'
    b'"mac_finalize_workers":5,"mac_preflight_workers":3,"process_profile_sha256":'
    b'"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    b'"provider_poll_milliseconds":1000}'
)
_V1_HASH = 'sha256:fc60cf55ec7f726235b99694548028f9edfbe458c609cc8001675ced369c6862'


class _SlowCommitBackend(_Backend):
    def __init__(self, clock: _Clock) -> None:
        super().__init__(recoverable=(_work('attempt-cold', 'local_materialized', 6),))
        self.clock = clock
        self.claim_lease_seconds_override = 120.0
        self.commit_entered = threading.Event()
        self.commit_release = threading.Event()
        self.commit_drained = threading.Event()
        self.renew_entered = threading.Event()
        self.renew_release = threading.Event()
        self.block_renew = False
        self.lose_renew_response = False
        self.durable = None
        self.commit_guard = None

    def renew_claim(self, work, *, lease_seconds):
        self.renew_entered.set()
        if self.block_renew and not self.renew_release.wait(2):
            raise AssertionError('test did not release renewal')
        renewed = super().renew_claim(work, lease_seconds=lease_seconds)
        self.durable = renewed
        if self.lose_renew_response:
            raise ConnectionError('renewal committed but response was lost')
        return renewed

    def reload_claim(self, work):
        self.calls.append('reload:' + work.attempt_id)
        return self.durable or work

    def commit(self, work, *, credit_allowance, stage_guard):
        self.commit_guard = stage_guard
        self.commit_entered.set()
        try:
            while not self.commit_release.wait(0.002):
                stage_guard.checkpoint()
            stage_guard.checkpoint()
            updated = super().commit(work, credit_allowance=credit_allowance,
                                     stage_guard=stage_guard)
            if self.durable is not None:
                updated = replace(updated, lease_expires_monotonic=self.durable.lease_expires_monotonic)
            return updated
        finally:
            self.commit_drained.set()


class StageClaimWindowTests(unittest.TestCase):
    def test_refresh_extends_only_claim_window_and_never_total_budget(self) -> None:
        clock = _Clock(1000)
        guard = StageLeaseGuard(1300, threading.Event(), clock, claim_deadline_monotonic=1090)
        self.assertEqual(guard.remaining_seconds(), 90)
        clock.advance(40)
        guard.refresh_claim_deadline(1130)
        self.assertEqual(guard.remaining_seconds(), 90)
        self.assertEqual(guard.deadline_monotonic, 1300)
        guard.refresh_claim_deadline(1400)
        self.assertEqual(guard.remaining_seconds(), 260)
        clock.advance(260)
        with self.assertRaises(StageLeaseLost):
            guard.checkpoint()
        with self.assertRaises(StageLeaseLost):
            guard.refresh_claim_deadline(1500)

    def test_expired_or_revoked_claim_cannot_be_revived_by_verified_late_reply(self) -> None:
        for cause in ('claim', 'total', 'revoked'):
            with self.subTest(cause=cause):
                clock = _Clock(1000)
                guard = StageLeaseGuard(1100, threading.Event(), clock, claim_deadline_monotonic=1050)
                if cause == 'claim':
                    clock.advance(50)
                elif cause == 'total':
                    guard.refresh_claim_deadline(1200)
                    clock.advance(100)
                else:
                    guard.revoke()
                with self.assertRaises(StageLeaseLost):
                    guard.refresh_claim_deadline(1300)
                with self.assertRaises(StageLeaseLost):
                    guard.checkpoint()

    def test_refresh_rejects_nonincreasing_or_nonfinite_authority(self) -> None:
        for value in (1090, 1089, 0, -1, True, '1200', float('nan'), float('inf')):
            with self.subTest(value=value):
                clock = _Clock(1000)
                guard = StageLeaseGuard(1500, threading.Event(), clock, claim_deadline_monotonic=1090)
                with self.assertRaises(ValueError):
                    guard.refresh_claim_deadline(value)
                self.assertEqual(guard.remaining_seconds(), 90)

    def test_old_guard_retains_total_deadline_without_claim_refresh(self) -> None:
        clock = _Clock(1000)
        guard = StageLeaseGuard(1060, threading.Event(), clock)
        clock.advance(59)
        self.assertEqual(guard.remaining_seconds(), 1)
        clock.advance(1)
        with self.assertRaises(StageLeaseLost):
            guard.checkpoint()


class LongCommitCoordinatorTests(unittest.TestCase):
    def _start(self, backend, clock, **limit_changes):
        condition = threading.Condition()
        results: list[CoordinatorResult] = []
        failures: list[BaseException] = []

        def progress(_snapshot):
            with condition:
                condition.notify_all()

        coordinator = StagedParseCoordinator(
            backend=backend, limits=_limits(**({"commit_stage_seconds": 500} | limit_changes)),
            monotonic=clock, progress=progress,
        )

        def run():
            try:
                results.append(coordinator.run(stop_requested=lambda: True))
            except BaseException as exc:
                failures.append(exc)
            finally:
                with condition:
                    condition.notify_all()

        thread = threading.Thread(target=run)
        thread.start()

        def cleanup():
            clock.advance(100_000)
            backend.commit_release.set()
            backend.renew_release.set()
            backend.remote_release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'coordinator must drain actual stage thread')

        self.addCleanup(cleanup)
        return thread, condition, results, failures

    def _renew_then_observe(self, backend, clock, condition):
        prior = backend.renew_calls
        clock.advance(40)

        def refreshed():
            guard = backend.commit_guard
            return (backend.renew_calls > prior and guard is not None
                    and guard.claim_deadline_monotonic == clock() + 90)

        with condition:
            self.assertTrue(condition.wait_for(refreshed, timeout=2), 'verified renewal must refresh live guard')

    def test_commit_spans_multiple_real_claim_windows_and_drains_same_attempt(self) -> None:
        for lost_response in (False, True):
            with self.subTest(lost_response=lost_response):
                clock = _Clock()
                backend = _SlowCommitBackend(clock)
                backend.lose_renew_response = lost_response
                thread, condition, results, failures = self._start(backend, clock)
                self.assertTrue(backend.commit_entered.wait(2))
                guard = backend.commit_guard
                self.assertEqual(guard.deadline_monotonic, 1500)
                for _ in range(4):
                    self._renew_then_observe(backend, clock, condition)
                    self.assertEqual(guard.deadline_monotonic, 1500)
                    guard.checkpoint()
                self.assertEqual(clock(), 1160)
                backend.commit_release.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(results[0].terminal, CoordinatorTerminal.QUIESCENT)
                self.assertEqual(results[0].admitted, 0)
                self.assertEqual(results[0].completed, 1)
                self.assertEqual(results[0].credits_in_use.nonzero(), {})
                self.assertEqual(results[0].errors, ())
                self.assertEqual(backend.calls.count('commit:attempt-cold'), 1)
                self.assertGreaterEqual(backend.renew_calls, 4)
                if lost_response:
                    self.assertGreaterEqual(backend.calls.count('reload:attempt-cold'), 4)

    def test_total_budget_expiry_keeps_responsibility_despite_successful_renewal(self) -> None:
        clock = _Clock()
        backend = _SlowCommitBackend(clock)
        # Override only the total budget; leases still last 120s.
        thread, condition, results, failures = self._start(
            backend, clock, commit_stage_seconds=70,
        )
        self.assertTrue(backend.commit_entered.wait(2))
        # Here remaining_seconds is already capped by total70; observe claim deadline itself.
        self._renew_then_observe(backend, clock, condition)
        clock.advance(30)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(backend.commit_drained.is_set())
        self.assertEqual(failures, [])
        result = results[0]
        self.assertEqual(result.terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertEqual(result.completed, 0)
        self.assertEqual(result.credits_in_use.documents, 1)
        self.assertEqual(result.credits_in_use.ack_items, 1)
        self.assertFalse(any(call.startswith(('commit:', 'cleanup:', 'ack:')) for call in backend.calls))
        with self.assertRaises(StageLeaseLost):
            backend.commit_guard.refresh_claim_deadline(2000)

    def test_stalled_renewal_cannot_revive_a_stage_that_crossed_claim_window(self) -> None:
        clock = _Clock()
        backend = _SlowCommitBackend(clock)
        backend.block_renew = True
        thread, _condition, results, failures = self._start(backend, clock)
        self.assertTrue(backend.commit_entered.wait(2))
        clock.advance(40)
        self.assertTrue(backend.renew_entered.wait(2))
        clock.advance(50)  # Exactly the original conservative claim deadline.
        self.assertTrue(backend.commit_drained.wait(2))
        backend.renew_release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results[0].terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertEqual(results[0].credits_in_use.documents, 1)
        self.assertFalse(any(call.startswith(('commit:', 'cleanup:', 'ack:')) for call in backend.calls))
        with self.assertRaises(StageLeaseLost):
            backend.commit_guard.refresh_claim_deadline(2000)

    def test_configured_long_commit_does_not_extend_remote_lane(self) -> None:
        clock = _Clock()
        backend = _SlowCommitBackend(clock)
        backend.recoverable = [_work('attempt-remote', 'submitted', 3)]
        backend.block_remote = True
        thread, _condition, results, failures = self._start(backend, clock)
        self.assertTrue(backend.remote_entered.wait(2))
        clock.advance(60)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results[0].terminal, CoordinatorTerminal.STUCK_OPEN_CIRCUIT)
        self.assertEqual(results[0].credits_in_use.provider_tasks, 1)
        self.assertEqual(backend.remote_calls, 1)
        self.assertFalse(backend.commit_entered.is_set())

    def test_long_commit_budget_does_not_relax_short_stage_or_claim_contract(self) -> None:
        for change in ({'commit_stage_seconds': 0}, {'commit_stage_seconds': True},
                       {'commit_stage_seconds': float('inf')}, {'claim_lease_seconds': 301},
                       {'max_stage_step_seconds': 90}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                _limits(**({'commit_stage_seconds': 3600} | change))


class CommitBudgetIdentityTests(unittest.TestCase):
    def test_legacy_literal_wire_and_hash_survive_v2_addition(self) -> None:
        old = decode_staged_worker_profile_v4(_V1_BYTES)
        self.assertEqual(old, StagedWorkerProfileV4('sha256:' + 'a' * 64, 3, 5))
        self.assertEqual(old.exact_bytes, _V1_BYTES)
        self.assertEqual(old.sha256, _V1_HASH)
        self.assertIsNone(old.commit_stage_seconds)
        # An explicit new-field null is not the historical v1 wire contract.
        wire = json.loads(_V1_BYTES)
        wire['commit_stage_seconds'] = None
        with self.assertRaises(ValueError):
            decode_staged_worker_profile_v4(json.dumps(wire, sort_keys=True, separators=(',', ':')).encode())

    def test_v2_commit_budget_is_closed_and_changes_identity(self) -> None:
        profile = StagedWorkerProfileV4('sha256:' + 'a' * 64, 3, 5,
            contract_version='staged-worker-composition.v2', commit_stage_seconds=3600)
        self.assertEqual(decode_staged_worker_profile_v4(profile.exact_bytes), profile)
        self.assertNotEqual(profile.sha256, _V1_HASH)
        self.assertNotEqual(replace(profile, commit_stage_seconds=7200).sha256, profile.sha256)
        for change in ({'contract_version': 'staged-worker-composition.v1'},
                       {'commit_stage_seconds': None}, {'commit_stage_seconds': True},
                       {'commit_stage_seconds': 0}, {'commit_stage_seconds': float('inf')}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(profile, **change)
        payload = json.loads(profile.exact_bytes)
        del payload['commit_stage_seconds']
        with self.assertRaises(ValueError):
            decode_staged_worker_profile_v4(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode())

    def test_staged_setting_default_and_range_reach_real_capacity_projection(self) -> None:
        remote = _profile()
        base = dict(process_profile_file=Path('/not-read/profile.json'),
                    process_profile_sha256=remote.sha256, archive_member_count_limit=8192)
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = StagedV4Settings(**base)
            self.assertEqual(settings.commit_stage_seconds, 3600)
            for seconds in (240, 3600, 86400):
                with self.subTest(seconds=seconds):
                    current = StagedV4Settings(**base, commit_stage_seconds=seconds)
                    profile = current.worker_profile(process_profile_sha256=remote.sha256,
                        mac_preflight_workers=3, mac_finalize_workers=5)
                    self.assertEqual(profile.contract_version, 'staged-worker-composition.v2')
                    self.assertEqual(profile.commit_stage_seconds, seconds)
                    limits = staged_v4_coordinator_limits(remote, worker_profile=profile)
                    self.assertEqual(limits.commit_stage_seconds, seconds)
                    # The bounded stage step covers the largest retained provider result over the
                    # Mac<->Windows tunnel (~34 MB took ~62 s at ~0.55 MB/s); the lease keeps the
                    # 30 s renewal/deferral headroom (lease - step - margin) the scheduler had at 60/120.
                    self.assertEqual(limits.max_stage_step_seconds, 240)
                    self.assertEqual(limits.claim_lease_seconds, 300)
                    self.assertEqual(
                        limits.claim_lease_seconds - limits.max_stage_step_seconds
                        - limits.claim_renew_margin_seconds,
                        30,
                    )
            # The settings floor must not admit a commit budget the production projection rejects.
            self.assertEqual(
                StagedV4Settings.model_fields['commit_stage_seconds'].metadata[0].ge,
                PRODUCTION_MAX_STAGE_STEP_SECONDS,
            )
            for value in (239, 60, 86401, -1, True, float('nan'), float('inf')):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    StagedV4Settings(**base, commit_stage_seconds=value)

    def test_environment_budget_is_staged_only_and_invalid_value_stays_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = _env(Path(directory)) | {
                'DISCLOSURE_V4_PROCESS_PROFILE_FILE': str(Path(directory) / 'profile.json'),
                'DISCLOSURE_V4_PROCESS_PROFILE_SHA256': 'sha256:' + 'a' * 64,
                'DISCLOSURE_V4_ARCHIVE_MEMBER_COUNT_LIMIT': '8192',
                'DISCLOSURE_V4_COMMIT_STAGE_SECONDS': '7200',
            }
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(load_staged_v4_settings().commit_stage_seconds, 7200)
                self.assertEqual(load_settings().worker_parse_execution_mode, 'legacy-sync')
            env['DISCLOSURE_V4_COMMIT_STAGE_SECONDS'] = 'not-a-number'
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(load_settings().worker_parse_execution_mode, 'legacy-sync')
                with self.assertRaises(ValueError):
                    load_staged_v4_settings()


if __name__ == '__main__':
    unittest.main()
