"""Independent F5 acceptance: a typed semantic fault is never masked or cached.

Pro §5.2 and §7 group (1): a recognized non-availability, non-cancel
adjudicator error is raised as ``failed_closed`` (with its cache key) before
any guard checkpoint can turn it into lease loss, so a concurrent revocation
never hides it (counterexample CE2); cancellation and the six availability
reasons keep the original guard/fallback rules; a failed group is never
cached, so only the persistent stop bounds re-drives (CE1, by design); a model
child killed by the worker shutdown with truncated output stays a retry-neutral
cancellation, while a complete protocol-invalid answer is a public stop.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from disclosure_anchor.adapters.semantics import codex_cli
from disclosure_anchor.adapters.semantics.claude_cli import ClaudeCliSemanticAdjudicator
from disclosure_anchor.application.ports.semantic_routes import SemanticRouteAdjudicatorError
from disclosure_anchor.application.services.semantic_adjudication import (
    ConfiguredSemanticProvider,
    OrderedSemanticAdjudicationExecutor,
    semantic_group_cache_key,
)
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseGuard, StageLeaseLost
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorWork,
    ResourceCreditVector,
    StagedParseCoordinator,
)
from disclosure_anchor.application.services.worker_stop_latch import InProcessWorkerStopLatch
from tests.unit.test_f5_coordinator_stop_independent import _StageBackend, _run
from tests.unit.test_semantic_adjudication import _batch, _Cache, _decisions
from tests.unit.test_semantic_execution_guard import _Provider
from tests.unit.test_staged_parse_coordinator import _limits, _work


_GROUP = "sha256:" + "9" * 64


def _guard() -> StageLeaseGuard:
    return StageLeaseGuard(time.monotonic() + 30, threading.Event(), time.monotonic)


class _Revoking(_Provider):
    """Revokes the live stage guard (an operator stop arriving) and then fails."""

    def __init__(self, provider_id: str, *, reason_code: str, provenance: str = "operator_cancel") -> None:
        super().__init__(provider_id)
        self.reason_code = reason_code
        self.provenance = provenance

    def adjudicate_with_result(self, batch, *, stage_guard=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.guards.append(stage_guard)
        if stage_guard is not None:
            stage_guard.revoke(self.provenance)
        raise SemanticRouteAdjudicatorError(
            f"provider failed: {self.reason_code} sk-live-F5SECRET",
            reason_code=self.reason_code,
            retryable=self.reason_code != "forbidden_tool_call",
        )


def _raising(reason_code: str):  # type: ignore[no-untyped-def]
    def decide(_batch):  # type: ignore[no-untyped-def]
        raise SemanticRouteAdjudicatorError(
            f"provider failed: {reason_code}", reason_code=reason_code, retryable=True)

    return decide


def _chain(primary: _Provider, backup: _Provider) -> tuple[OrderedSemanticAdjudicationExecutor, _Cache, _Cache]:
    primary_cache, backup_cache = _Cache(), _Cache()
    return OrderedSemanticAdjudicationExecutor((
        ConfiguredSemanticProvider(adapter=primary, cache=primary_cache),  # type: ignore[arg-type]
        ConfiguredSemanticProvider(adapter=backup, cache=backup_cache),  # type: ignore[arg-type]
    )), primary_cache, backup_cache


class FailedClosedPrecedenceTests(unittest.TestCase):
    def test_typed_fault_raised_with_a_concurrent_revocation_stays_the_visible_error(self) -> None:
        primary, backup = _Revoking("f5-primary", reason_code="forbidden_tool_call"), _Provider("f5-backup")
        executor, primary_cache, backup_cache = _chain(primary, backup)
        guard = _guard()
        with self.assertRaises(SemanticRouteAdjudicatorError) as raised:
            executor.adjudicate(_batch(), group_hash=_GROUP, stage_guard=guard)
        error = raised.exception
        self.assertEqual((error.reason_code, error.retryable), ("forbidden_tool_call", False))
        self.assertIsInstance(error.__cause__, SemanticRouteAdjudicatorError)
        self.assertEqual(len(error.attempts), 1)
        attempt = error.attempts[0]
        expected_key = semantic_group_cache_key(
            identity=primary.provider_identity, taxonomy_version=_batch().taxonomy.version, group_hash=_GROUP)
        self.assertEqual((attempt.outcome, attempt.reason_code, attempt.cache_key),
                         ("failed_closed", "forbidden_tool_call", expected_key))
        self.assertEqual((primary.calls, backup.calls), (1, 0))
        self.assertEqual((primary_cache.entries, backup_cache.entries), ({}, {}))
        self.assertEqual(guard.revocation_provenance, "operator_cancel")

    def test_cancellation_and_availability_still_check_the_guard_first(self) -> None:
        with self.subTest(case="cancelled after revocation"):
            primary, backup = _Revoking("f5-primary", reason_code="cancelled"), _Provider("f5-backup")
            executor, primary_cache, _ = _chain(primary, backup)
            with self.assertRaises(StageLeaseLost) as raised:
                executor.adjudicate(_batch(), group_hash=_GROUP, stage_guard=_guard())
            self.assertEqual(raised.exception.provenance, "operator_cancel")
            self.assertEqual((primary.calls, backup.calls), (1, 0))
            self.assertEqual(primary_cache.entries, {})
        with self.subTest(case="availability after revocation never falls back"):
            primary, backup = _Revoking("f5-primary", reason_code="capacity_unavailable"), _Provider("f5-backup")
            executor, _, backup_cache = _chain(primary, backup)
            with self.assertRaises(StageLeaseLost):
                executor.adjudicate(_batch(), group_hash=_GROUP, stage_guard=_guard())
            self.assertEqual((primary.calls, backup.calls), (1, 0))
            self.assertEqual(backup_cache.entries, {})
        with self.subTest(case="availability with a live guard keeps the bounded fallback"):
            primary = _Provider("f5-primary", decide=_raising("capacity_unavailable"))
            backup = _Provider("f5-backup")
            executor, primary_cache, backup_cache = _chain(primary, backup)
            outcome = executor.adjudicate(_batch(), group_hash=_GROUP, stage_guard=_guard())
            self.assertEqual([item.outcome for item in outcome.attempts], ["availability_failed", "succeeded"])
            self.assertEqual((primary.calls, backup.calls), (1, 1))
            self.assertEqual((len(primary_cache.entries), len(backup_cache.entries)), (0, 1))

    def test_a_failed_group_is_never_cached_but_a_successful_group_is_reused(self) -> None:
        # CE1 by design: the cache cannot bound a failed group's re-drives;
        # the persistent stop does. Successful groups are reused without a call.
        failing = _Provider("f5-primary", decide=_raising("invalid_json"))
        cache = _Cache()
        executor = OrderedSemanticAdjudicationExecutor(
            (ConfiguredSemanticProvider(adapter=failing, cache=cache),))  # type: ignore[arg-type]
        for _ in range(3):
            with self.assertRaises(SemanticRouteAdjudicatorError):
                executor.adjudicate(_batch(), group_hash=_GROUP)
        self.assertEqual((failing.calls, cache.entries), (3, {}))
        succeeding = _Provider("f5-primary")
        self.assertEqual(succeeding.provider_identity, failing.provider_identity)
        reuse = OrderedSemanticAdjudicationExecutor(
            (ConfiguredSemanticProvider(adapter=succeeding, cache=cache),))  # type: ignore[arg-type]
        first = reuse.adjudicate(_batch(), group_hash=_GROUP)
        second = reuse.adjudicate(_batch(), group_hash=_GROUP)
        self.assertEqual([item.outcome for item in first.attempts], ["succeeded"])
        self.assertEqual([item.outcome for item in second.attempts], ["cache_hit"])
        self.assertEqual(succeeding.calls, 1)
        self.assertEqual(second.decisions, _decisions())


class _SemanticCommitBackend(_StageBackend):
    """Commit adjudicates one group through the real executor and adapter."""

    def __init__(self, executor: OrderedSemanticAdjudicationExecutor, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.executor = executor

    def commit(self, work: CoordinatorWork, *, credit_allowance: ResourceCreditVector,
               stage_guard: StageLeaseGuard) -> CoordinatorWork:
        self.guards[work.attempt_id] = stage_guard
        self.executor.adjudicate(_batch(), group_hash=_GROUP, stage_guard=stage_guard)
        return super().commit(work, credit_allowance=credit_allowance, stage_guard=stage_guard)


class RealModelChildShutdownTests(unittest.TestCase):
    """Real child processes through the real Claude adapter and coordinator."""

    def setUp(self) -> None:
        codex_cli._SEMANTIC_SHUTDOWN_REQUESTED.clear()
        self.addCleanup(codex_cli._SEMANTIC_SHUTDOWN_REQUESTED.clear)
        self.spawned = threading.Event()
        self.processes: list[subprocess.Popen[str]] = []
        original_popen = subprocess.Popen

        def popen(*args, **kwargs):  # type: ignore[no-untyped-def]
            process = original_popen(*args, **kwargs)
            self.processes.append(process)
            self.spawned.set()
            return process

        patcher = mock.patch.object(codex_cli.subprocess, "Popen", side_effect=popen)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._reap)

    def _reap(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def _executor(self, child: str) -> OrderedSemanticAdjudicationExecutor:
        original_run = codex_cli._run_process

        def run_child(*, args, prompt, env, timeout_seconds, stage_guard=None):  # type: ignore[no-untyped-def]
            del args
            return original_run(args=[sys.executable, "-c", child], prompt=prompt, env={},
                                timeout_seconds=timeout_seconds,
                                **({} if stage_guard is None else {"stage_guard": stage_guard}))

        patcher = mock.patch.object(codex_cli, "_run_process", side_effect=run_child)
        patcher.start()
        self.addCleanup(patcher.stop)
        adapter = ClaudeCliSemanticAdjudicator(executable=Path("/not-run/claude"), timeout_seconds=30)
        return OrderedSemanticAdjudicationExecutor((ConfiguredSemanticProvider(adapter=adapter, cache=_Cache()),))  # type: ignore[arg-type]

    def test_shutdown_with_truncated_model_output_stays_a_retry_neutral_cancellation(self) -> None:
        # The worker's TERM handler flags the stop and signals every owned model
        # child; the child has already written a truncated answer. The stage's
        # own cleanup then signals the child's group again, and that can fail:
        # on Darwin killpg returns EPERM for a group whose members have all
        # exited (seen in real runs of this scenario, see the acceptance
        # report). Made deterministic: the child outlives the handler's SIGTERM
        # until its SIGKILL, so the stage always takes its cancellation cleanup
        # path, and only killpg calls outside the handler's thread fail.
        truncated = ("import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                     "sys.stdin.read(); "
                     "sys.stdout.write('{\"type\":\"result\",\"structured_output\":{\"decisions\":'); "
                     "sys.stdout.flush(); time.sleep(60)")
        backend = _SemanticCommitBackend(self._executor(truncated),
                                         recoverable=(_work("attempt-s", "local_materialized", 6),))
        latch = InProcessWorkerStopLatch()
        operator = threading.Event()
        handler_threads: set[int] = set()
        real_killpg = os.killpg

        def killpg(pgid: int, signum: int) -> None:
            if threading.get_ident() not in handler_threads and codex_cli._SEMANTIC_SHUTDOWN_REQUESTED.is_set():
                raise PermissionError(errno.EPERM, "Operation not permitted")
            real_killpg(pgid, signum)

        def sigterm() -> None:
            handler_threads.add(threading.get_ident())
            if self.spawned.wait(timeout=10):
                time.sleep(0.2)
                operator.set()
                codex_cli.terminate_active_semantic_processes(grace_seconds=1)

        signaller = threading.Thread(target=sigterm)
        with mock.patch("os.killpg", killpg):
            signaller.start()
            result = _run(StagedParseCoordinator(backend=backend, limits=_limits(),
                                                 stop_control=latch), stop_requested=operator.is_set)
            signaller.join(timeout=10)

        self.assertFalse(latch.is_tripped(), "a shutdown-truncated answer is not a public fault")
        self.assertEqual((result.stop_cause, result.termination_kind), (None, "operator_drain"),
                         "a cancelled model call must never become a provider-availability fallback "
                         "or a degraded publication")
        self.assertFalse(any(call.startswith(("commit:", "cleanup:", "ack:")) for call in backend.calls))
        self.assertTrue(all(process.poll() is not None for process in self.processes))

    def test_complete_protocol_invalid_answer_is_a_public_stop(self) -> None:
        invalid = "import sys; sys.stdin.read(); sys.stdout.write('not a runtime protocol answer')"
        backend = _SemanticCommitBackend(self._executor(invalid),
                                         recoverable=(_work("attempt-p", "local_materialized", 6),))
        latch = InProcessWorkerStopLatch()
        result = _run(StagedParseCoordinator(backend=backend, limits=_limits(),
                                             stop_control=latch))
        self.assertTrue(latch.is_tripped())
        cause = result.stop_cause
        assert cause is not None
        self.assertEqual((cause.kind, cause.reason_code, cause.attempt_id, cause.lane),
                         ("semantic_failed_closed", "invalid_runtime_protocol", "attempt-p", "commit"))
        self.assertEqual([item.outcome for item in cause.provider_attempts], ["failed_closed"])
        self.assertEqual(result.termination_kind, "public_stop")


if __name__ == "__main__":
    unittest.main()
