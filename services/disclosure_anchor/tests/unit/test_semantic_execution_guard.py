"""Independent semantic deadline propagation and owned-child regression tests."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from disclosure_anchor.adapters.semantics import codex_cli
from disclosure_anchor.adapters.semantics.claude_cli import ClaudeCliSemanticAdjudicator
from disclosure_anchor.application.contracts.provider_unit import ProviderUnitBuildResult
from disclosure_anchor.application.contracts.semantic_routes import SemanticDocumentContext, SemanticRouteContractError
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticProviderResult, SemanticRouteAdjudicatorError,
)
from disclosure_anchor.application.services.semantic_adjudication import (
    ConfiguredSemanticProvider, OrderedSemanticAdjudicationExecutor,
)
from disclosure_anchor.application.services.semantic_router import SemanticRouter
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseGuard, StageLeaseLost
from tests.unit.test_atomic_publication_request_builder_v4 import _Harness, _draft
from tests.unit.test_semantic_adjudication import _batch, _Cache, _decisions, _identity
from tests.unit.test_semantic_claude_cli import _stdout
from tests.unit.test_semantic_router import (
    _Adjudicator, _MemoryCache, _drafts, _two_model_drafts,
    _select_forecast_summary, _taxonomy,
)


_GROUP = 'sha256:' + '9' * 64
_RESPONSE = 'sha256:' + '8' * 64


def _guard():
    return StageLeaseGuard(time.monotonic() + 30, threading.Event(), time.monotonic)


class _ObservedGuard:
    def __init__(self):
        self.live = _guard()
        self.checked = threading.Event()

    def checkpoint(self):
        self.checked.set()
        self.live.checkpoint()

    def remaining_seconds(self):
        self.checked.set()
        return self.live.remaining_seconds()


class _Provider:
    def __init__(self, provider_id='guard-primary', decide=lambda _batch: _decisions()):
        self.provider_identity = _identity(provider_id)
        self.decide = decide
        self.guards = []
        self.calls = 0
        self.on_result = lambda: None

    def adjudicate_with_result(self, batch, *, stage_guard=None):
        self.calls += 1
        self.guards.append(stage_guard)
        decisions = self.decide(batch)
        self.on_result()
        return SemanticProviderResult(decisions=decisions, response_sha256=_RESPONSE)


def _configured(provider, cache=None):
    return ConfiguredSemanticProvider(adapter=provider, cache=cache if cache is not None else _Cache())


class SemanticChainGuardTests(unittest.TestCase):
    def test_actual_builder_passes_live_guard_to_router_and_preserves_guard_loss(self) -> None:
        harness = _Harness()
        guard = _guard()
        lost = StageLeaseLost('same bounded stage lost')
        seen = []

        def route(*, admitted, document, drafts, stage_guard=None):
            seen.append(stage_guard)
            raise lost

        harness.builder._semantic_router = mock.Mock(route=route)
        with mock.patch(
            'disclosure_anchor.application.services.atomic_publication_request_builder_v4.build_provider_units',
            return_value=ProviderUnitBuildResult(
                provider_document_sha256=harness.materialized.receipt.provider_envelope_sha256,
                units=(_draft(harness),), unassigned_table_parts=(),
            ),
        ), self.assertRaises(StageLeaseLost) as caught:
            harness.builder.build(checkpoint=harness.fixture.local_materialized,
                                  materialized=harness.materialized, stage_guard=guard)
        self.assertIs(caught.exception, lost)
        self.assertEqual(seen, [guard])

    def test_guarded_legacy_model_branch_fails_closed_but_none_and_pure_route_stay_compatible(self) -> None:
        admitted, drafts = _two_model_drafts()
        provider = _Adjudicator(_select_forecast_summary)
        router = SemanticRouter(taxonomy=_taxonomy(), batch_size=1,
                                adjudicator=provider, cache=_MemoryCache())
        context = SemanticDocumentContext(title='某公司业绩预告', filing_type='performance_forecast')
        with self.assertRaises(SemanticRouteContractError):
            router.route(admitted=admitted, drafts=drafts, document=context, stage_guard=_guard())
        self.assertEqual(provider.calls, 0)
        result = router.route(admitted=admitted, drafts=drafts, document=context)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(len(result.units), 2)
        pure_admitted, pure_drafts = _drafts('未指向具体业务的说明')
        pure = router.route(admitted=pure_admitted, drafts=pure_drafts,
                            document=context, stage_guard=_guard())
        self.assertEqual(provider.calls, 2)
        self.assertEqual(len(pure.units), 1)

    def test_real_two_group_router_and_executor_share_one_live_guard(self) -> None:
        admitted, drafts = _two_model_drafts()
        provider = _Provider(decide=_select_forecast_summary)
        cache = _Cache()
        guard = _guard()
        router = SemanticRouter(taxonomy=_taxonomy(), batch_size=1,
            executor=OrderedSemanticAdjudicationExecutor((_configured(provider, cache),)))
        result = router.route(admitted=admitted, drafts=drafts, stage_guard=guard,
            document=SemanticDocumentContext(title='某公司业绩预告', filing_type='performance_forecast'))
        self.assertEqual(provider.calls, 2)
        self.assertEqual(provider.guards, [guard, guard])
        self.assertEqual(len(cache.entries), 2)
        self.assertEqual(len(result.units), 2)
        self.assertEqual([unit.semantic_keys for unit in result.units],
                         [('performance_forecast_summary',), ('performance_forecast_summary',)])

    def test_loss_between_groups_prevents_next_group_without_erasing_prior_cache(self) -> None:
        admitted, drafts = _two_model_drafts()
        provider = _Provider(decide=_select_forecast_summary)
        cache = _Cache()
        guard = _guard()
        original_put = cache.put
        def put(entry):
            original_put(entry)  # Authorized first result remains reusable.
            guard.revoke()
        cache.put = put
        router = SemanticRouter(taxonomy=_taxonomy(), batch_size=1,
            executor=OrderedSemanticAdjudicationExecutor((_configured(provider, cache),)))
        with self.assertRaises(StageLeaseLost):
            router.route(admitted=admitted, drafts=drafts, stage_guard=guard,
                document=SemanticDocumentContext(title='某公司业绩预告', filing_type='performance_forecast'))
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(cache.entries), 1)

    def test_late_provider_success_cannot_cache_or_fallback_after_loss(self) -> None:
        guard = _guard()
        primary, backup = _Provider(), _Provider('guard-backup')
        cache = _Cache()
        primary.on_result = guard.revoke
        executor = OrderedSemanticAdjudicationExecutor((_configured(primary, cache), _configured(backup)))
        with self.assertRaises(StageLeaseLost):
            executor.adjudicate(_batch(), group_hash=_GROUP, stage_guard=guard)
        self.assertEqual(primary.guards, [guard])
        self.assertEqual(backup.calls, 0)
        self.assertEqual(cache.entries, {})

    def test_cache_hit_cannot_escape_revocation_during_cache_read(self) -> None:
        cache = _Cache()
        primary = _Provider('cache-read-guard-case')
        executor = OrderedSemanticAdjudicationExecutor((_configured(primary, cache),))
        executor.adjudicate(_batch(), group_hash=_GROUP)
        guard = _guard()
        original_get = cache.get
        def get(key):
            result = original_get(key)
            guard.revoke()
            return result
        cache.get = get
        with self.assertRaises(StageLeaseLost):
            executor.adjudicate(_batch(), group_hash=_GROUP, stage_guard=guard)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(len(cache.entries), 1)

    def test_same_stage_exception_is_not_converted_to_availability_failover(self) -> None:
        lost = StageLeaseLost('claim window lost')
        primary, backup = _Provider(), _Provider('guard-backup')
        def fail(_batch):
            raise lost
        primary.decide = fail
        executor = OrderedSemanticAdjudicationExecutor((_configured(primary), _configured(backup)))
        with self.assertRaises(StageLeaseLost) as caught:
            executor.adjudicate(_batch(), group_hash=_GROUP, stage_guard=_guard())
        self.assertIs(caught.exception, lost)
        self.assertEqual(backup.calls, 0)

    def test_provider_own_timeout_retains_original_availability_failover(self) -> None:
        primary, backup = _Provider(), _Provider('guard-backup')
        def timeout(_batch):
            raise SemanticRouteAdjudicatorError('provider timeout', reason_code='timeout', retryable=True)
        primary.decide = timeout
        guard = _guard()
        outcome = OrderedSemanticAdjudicationExecutor((_configured(primary), _configured(backup))).adjudicate(
            _batch(), group_hash=_GROUP, stage_guard=guard)
        self.assertEqual(tuple(a.outcome for a in outcome.attempts), ('availability_failed', 'succeeded'))
        self.assertEqual(backup.guards, [guard])
        self.assertFalse(outcome.degraded_unavailable)

    def test_guard_loss_interrupts_real_singleflight_wait_while_first_owner_stays_live(self) -> None:
        entered, release = threading.Event(), threading.Event()
        first_errors, second_errors = [], []
        guard = _ObservedGuard()
        primary = _Provider('singleflight-guard-case')
        def decide(_batch):
            entered.set()
            if not release.wait(3):
                raise AssertionError('test failed to release first owner')
            return _decisions()
        primary.decide = decide
        executor = OrderedSemanticAdjudicationExecutor((_configured(primary),))
        def first():
            try:
                executor.adjudicate(_batch(), group_hash=_GROUP)
            except BaseException as exc:
                first_errors.append(exc)
        def second():
            try:
                executor.adjudicate(_batch(), group_hash=_GROUP, stage_guard=guard)
            except BaseException as exc:
                second_errors.append(exc)
        one, two = threading.Thread(target=first), threading.Thread(target=second)
        one.start()
        try:
            self.assertTrue(entered.wait(2))
            two.start()
            self.assertTrue(guard.checked.wait(2))
            guard.live.revoke()
            two.join(2)
            self.assertFalse(two.is_alive(), 'guard must interrupt wait before first owner releases lock')
            self.assertTrue(one.is_alive())
            self.assertEqual(len(second_errors), 1)
            self.assertIsInstance(second_errors[0], StageLeaseLost)
            self.assertEqual(primary.calls, 1)
        finally:
            release.set()
            one.join(3)
            if two.ident is not None:
                two.join(3)
        self.assertEqual(first_errors, [])


class SemanticAdapterGuardTests(unittest.TestCase):
    def _adapters(self, directory):
        return (codex_cli.CodexCliSemanticAdjudicator(executable=Path('/not-run/codex'),
                    runtime_tmp_root=Path(directory)),
                ClaudeCliSemanticAdjudicator(executable=Path('/not-run/claude')))

    @staticmethod
    def _success(*, args, **_kwargs):
        if '--output-last-message' in args:
            Path(args[args.index('--output-last-message') + 1]).write_text(
                '{"decisions":{"0":{"verdicts":{"forecast_summary":true}}}}')
            return subprocess.CompletedProcess(args, 0, '', '')
        return subprocess.CompletedProcess(args, 0, _stdout(), '')

    def test_revoked_waiter_leaves_provider_slot_owned_and_never_spawns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for adapter in self._adapters(directory):
                with self.subTest(adapter=type(adapter).__name__):
                    guard = _ObservedGuard()
                    errors = []
                    self.assertTrue(adapter._slot.acquire(blocking=False))
                    def invoke():
                        try:
                            adapter.adjudicate_with_result(_batch(), stage_guard=guard)
                        except BaseException as exc:
                            errors.append(exc)
                    thread = threading.Thread(target=invoke)
                    with mock.patch.object(codex_cli, '_run_process', side_effect=self._success) as run:
                        thread.start()
                        try:
                            self.assertTrue(guard.checked.wait(2))
                            guard.live.revoke()
                            thread.join(2)
                            self.assertFalse(thread.is_alive())
                            self.assertEqual(len(errors), 1)
                            self.assertIsInstance(errors[0], StageLeaseLost)
                            run.assert_not_called()
                            self.assertFalse(adapter._slot.acquire(blocking=False))
                        finally:
                            adapter._slot.release()
                            thread.join(3)
                        # Adjacent positive: cancellation did not leak or overrelease the permit.
                        result = adapter.adjudicate_with_result(_batch(), stage_guard=_guard())
                        self.assertEqual(len(result.decisions), 1)
                        self.assertEqual(run.call_count, 1)

    def test_both_adapters_forward_guard_and_reject_success_after_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for adapter in self._adapters(directory):
                with self.subTest(adapter=type(adapter).__name__):
                    guard = _guard()
                    seen = []
                    def run(**kwargs):
                        seen.append(kwargs.get('stage_guard'))
                        result = self._success(**kwargs)
                        guard.revoke()
                        return result
                    with mock.patch.object(codex_cli, '_run_process', side_effect=run):
                        with self.assertRaises(StageLeaseLost):
                            adapter.adjudicate_with_result(_batch(), stage_guard=guard)
                    self.assertEqual(seen, [guard])

    def test_both_adapters_keep_provider_timeout_distinct_from_stage_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for adapter in self._adapters(directory):
                with self.subTest(adapter=type(adapter).__name__):
                    with mock.patch.object(codex_cli, '_run_process',
                            side_effect=subprocess.TimeoutExpired(['not-run'], 600)):
                        with self.assertRaises(SemanticRouteAdjudicatorError) as caught:
                            adapter.adjudicate_with_result(_batch(), stage_guard=_guard())
                    self.assertEqual(caught.exception.reason_code, 'timeout')
                    self.assertTrue(caught.exception.retryable)


class OwnedSemanticProcessTests(unittest.TestCase):
    def test_guarded_real_child_receives_stdin_once_and_success_drains_streams(self) -> None:
        processes, inputs, timeouts = [], [], []
        original_popen = subprocess.Popen
        def popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            communicate = process.communicate
            def observed(*args, **kwargs):
                inputs.append(kwargs.get('input'))
                timeouts.append(kwargs.get('timeout'))
                return communicate(*args, **kwargs)
            process.communicate = observed
            return process
        with mock.patch.object(codex_cli.subprocess, 'Popen', side_effect=popen):
            result = codex_cli._run_process(args=[sys.executable, '-c',
                'import sys,time; value=sys.stdin.read(); time.sleep(.15); '
                'sys.stdout.write(value); sys.stderr.write("owned-stderr")'],
                prompt='one immutable prompt', env={}, timeout_seconds=3, stage_guard=_guard())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'one immutable prompt')
        self.assertEqual(result.stderr, 'owned-stderr')
        self.assertGreaterEqual(len(inputs), 2)
        self.assertEqual(inputs[0], 'one immutable prompt')
        self.assertTrue(all(value is None for value in inputs[1:]))
        self.assertTrue(all(0 < value <= .1 for value in timeouts))
        process = processes[0]
        self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))
        self.assertNotIn(process, codex_cli._ACTIVE_PROCESSES)

    def test_guard_loss_stops_only_owned_child_and_reaps_before_propagation(self) -> None:
        original_popen = subprocess.Popen
        spawned = threading.Event()
        processes, errors = [], []
        guard = _guard()
        def popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            spawned.set()
            return process
        def execute():
            try:
                codex_cli._run_process(args=[sys.executable, '-c',
                    'import sys,time; sys.stdin.read(); time.sleep(60)'],
                    prompt='bounded input', env={}, timeout_seconds=20, stage_guard=guard)
            except BaseException as exc:
                errors.append(exc)
        with mock.patch.object(codex_cli.subprocess, 'Popen', side_effect=popen):
            thread = threading.Thread(target=execute)
            thread.start()
            try:
                self.assertTrue(spawned.wait(2))
                guard.revoke()
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], StageLeaseLost)
                process = processes[0]
                self.assertIsNotNone(process.poll())
                self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))
                self.assertNotIn(process, codex_cli._ACTIVE_PROCESSES)
                self.assertFalse(codex_cli._SEMANTIC_SHUTDOWN_REQUESTED.is_set())
            finally:
                if processes and processes[0].poll() is None:
                    processes[0].kill()
                    processes[0].communicate(timeout=2)
                thread.join(3)

    def test_provider_total_timeout_is_not_reset_by_guard_polling(self) -> None:
        guard = _guard()
        processes, errors = [], []
        original_popen = subprocess.Popen
        def popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process
        def execute():
            try:
                codex_cli._run_process(args=[sys.executable, '-c',
                    'import sys,time; sys.stdin.read(); time.sleep(60)'],
                    prompt='bounded input', env={}, timeout_seconds=1, stage_guard=guard)
            except BaseException as exc:
                errors.append(exc)
        with mock.patch.object(codex_cli.subprocess, 'Popen', side_effect=popen):
            thread = threading.Thread(target=execute)
            thread.start()
            try:
                thread.join(3)
                self.assertFalse(thread.is_alive(), 'provider total timeout must not reset every poll')
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], subprocess.TimeoutExpired)
                guard.checkpoint()
                process = processes[0]
                self.assertIsNotNone(process.poll())
                self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))
                self.assertNotIn(process, codex_cli._ACTIVE_PROCESSES)
            finally:
                guard.revoke()
                if processes and processes[0].poll() is None:
                    processes[0].kill()
                thread.join(3)

    def test_parent_exit_does_not_abandon_grandchild_holding_owned_pipes(self) -> None:
        # The leader exits normally; its child still owns stdout/stderr. Polling
        # only the leader must not skip group cancellation and EOF collection.
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'grandchild-ready'
            child_code = (
                'import pathlib,signal,sys,time; '
                'signal.signal(signal.SIGTERM, lambda *_: '
                '(sys.stderr.write("grandchild-terminated\\n"), sys.stderr.flush(), sys.exit(0))); '
                'pathlib.Path(sys.argv[1]).write_text("ready"); time.sleep(60)'
            )
            leader_code = (
                'import subprocess,sys; '
                'subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]])'
            )
            original_popen = subprocess.Popen
            processes, errors, drained_stderr = [], [], []
            spawned = threading.Event()
            guard = _guard()
            def popen(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                processes.append(process)
                communicate = process.communicate
                def observed(*args, **kwargs):
                    result = communicate(*args, **kwargs)
                    drained_stderr.append(result[1])
                    return result
                process.communicate = observed
                spawned.set()
                return process
            def execute():
                try:
                    codex_cli._run_process(args=[sys.executable, '-c', leader_code, child_code, str(ready)],
                        prompt='bounded input', env={}, timeout_seconds=10, stage_guard=guard)
                except BaseException as exc:
                    errors.append(exc)
            with mock.patch.object(codex_cli.subprocess, 'Popen', side_effect=popen):
                thread = threading.Thread(target=execute)
                thread.start()
                try:
                    self.assertTrue(spawned.wait(2))
                    self.assertEqual(processes[0].wait(timeout=2), 0)
                    until = time.monotonic() + 2
                    while not ready.exists() and time.monotonic() < until:
                        threading.Event().wait(.01)
                    self.assertTrue(ready.exists())
                    guard.revoke()
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], StageLeaseLost)
                    self.assertTrue(any('grandchild-terminated' in value for value in drained_stderr))
                    self.assertTrue(all(stream.closed for stream in (
                        processes[0].stdin, processes[0].stdout, processes[0].stderr)))
                finally:
                    guard.revoke()
                    if processes:
                        codex_cli._signal_process_group(processes[0], codex_cli.signal.SIGKILL)
                    thread.join(3)


if __name__ == '__main__':
    unittest.main()
