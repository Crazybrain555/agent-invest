"""Independent outage regressions: terminal evidence, task continuity and fencing."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from pathlib import Path
import subprocess
import tempfile
import threading
from dataclasses import replace
import unittest
from unittest import mock

from disclosure_anchor.adapters.semantics.codex_cli import CodexCliSemanticAdjudicator
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticAdjudicationBatch,
    SemanticExecutionGuard,
    SemanticNetworkUnavailable,
    SemanticProviderResult,
    SemanticRouteAdjudicatorError,
)
from disclosure_anchor.application.services.semantic_adjudication import (
    ConfiguredSemanticProvider,
    OrderedSemanticAdjudicationExecutor,
    SharedSemanticNetworkCooldown,
)
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseGuard, StageLeaseLost
from tests.unit._codex_model_catalog_fixture import neutral_catalog
from tests.unit.test_semantic_adjudication import (
    _Adapter, _Cache, _batch, _decisions, _identity, _GROUP_HASH, _RESPONSE_HASH,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorTerminal,
    ResourceCreditVector,
    StageHeavyWorkRequired,
    StageWaiting,
    StagedParseCoordinator,
)
from tests.unit.test_staged_parse_coordinator import _Backend, _Clock, _limits, _work


_ROUTING_TIMEOUT = "workspace routing discovery timed out"
_REPLY = '{"decisions":{"0":{"verdicts":{"forecast_summary":false}}}}'


def _incident_stream(*, terminal: str = _ROUTING_TIMEOUT, sibling: str | None = None) -> str:
    # Replay the incident's observed event sequence, with private identity removed.
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "independent-network-replay"},
        {"type": "turn.started"},
    ]
    events.extend({"type": "error", "message": f"Reconnecting... {index}/5 ({_ROUTING_TIMEOUT})"}
                  for index in range(2, 6))
    events.append({"type": "item.completed", "item": {
        "id": "item_0", "type": "error", "message":
        "Falling back from WebSockets to HTTPS transport. " + _ROUTING_TIMEOUT,
    }})
    events.extend({"type": "error", "message": f"Reconnecting... {index}/5 ({_ROUTING_TIMEOUT})"}
                  for index in range(1, 6))
    if sibling is not None:
        events.append({"type": "error", "message": sibling})
    events.extend((
        {"type": "error", "message": terminal},
        {"type": "turn.failed", "error": {"message": terminal}},
    ))
    return "\n".join(json.dumps(event) for event in events)


class NetworkTerminalEvidenceIndependentTests(unittest.TestCase):
    def _exercise(
        self, *, stdout: str, stderr: str = "", returncode: int = 1,
        process_timeout: bool = False,
    ) -> tuple[object, _Adapter, _Cache, _Cache]:
        with tempfile.TemporaryDirectory() as tmp:
            primary = CodexCliSemanticAdjudicator(
                executable=Path("/synthetic/codex"), runtime_tmp_root=Path(tmp),
                model_catalog=neutral_catalog(),
            )
            backup = _Adapter(_identity("sonnet-backup", provider="anthropic"))
            primary_cache, backup_cache = _Cache(), _Cache()
            executor = OrderedSemanticAdjudicationExecutor((
                ConfiguredSemanticProvider(primary, primary_cache),
                ConfiguredSemanticProvider(backup, backup_cache),
            ))

            def failed_process(*, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                # A valid-looking last reply belongs to a failed process and must
                # never win or be cached; only the backup's validated reply wins.
                Path(args[args.index("--output-last-message") + 1]).write_text(_REPLY, encoding="utf-8")
                if process_timeout:
                    raise subprocess.TimeoutExpired(args, timeout=600, output=stdout, stderr=stderr)
                return subprocess.CompletedProcess(args, returncode, stdout, stderr)

            with mock.patch("disclosure_anchor.adapters.semantics.codex_cli._run_process",
                            side_effect=failed_process) as run:
                try:
                    result: object = executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
                except SemanticRouteAdjudicatorError as exc:
                    result = exc
            self.assertEqual(run.call_count, 1)
            return result, backup, primary_cache, backup_cache

    def test_process_timeout_falls_back_without_claiming_a_known_network_diagnostic(self) -> None:
        outcome, backup, primary_cache, backup_cache = self._exercise(stdout="", process_timeout=True)
        self.assertNotIsInstance(outcome, SemanticRouteAdjudicatorError)
        self.assertEqual(outcome.attempts[0].reason_code, "timeout")
        self.assertEqual(outcome.actual_result_identity.provider_id, "sonnet-backup")
        self.assertEqual(backup.calls, 1)
        self.assertFalse(primary_cache.entries)
        self.assertEqual(len(backup_cache.entries), 1)

    def test_primary_recovery_after_workspace_routing_timeout_keeps_primary_result(self) -> None:
        events = _incident_stream().splitlines()[:-2]
        events.extend(json.dumps(event) for event in (
            {"type": "item.completed", "item": {
                "id": "item_1", "type": "agent_message", "text": _REPLY,
            }},
            {"type": "turn.completed", "usage": {}},
        ))
        outcome, backup, primary_cache, backup_cache = self._exercise(
            stdout="\n".join(events), returncode=0,
        )
        self.assertNotIsInstance(outcome, SemanticRouteAdjudicatorError)
        self.assertEqual(backup.calls, 0)
        self.assertEqual(outcome.actual_result_attempt, 1)
        self.assertEqual(outcome.actual_result_identity.provider_id, "luna-primary")
        self.assertFalse(outcome.degraded_unavailable)
        self.assertEqual(len(primary_cache.entries), 1)
        self.assertFalse(backup_cache.entries)

    def test_workspace_timeout_after_both_cli_transports_reaches_backup(self) -> None:
        outcome, backup, primary_cache, backup_cache = self._exercise(stdout=_incident_stream())
        self.assertNotIsInstance(outcome, SemanticRouteAdjudicatorError)
        self.assertEqual(backup.calls, 1)
        self.assertEqual(outcome.actual_result_identity.provider_id, "sonnet-backup")
        self.assertEqual(outcome.attempts[0].reason_code, "transport_unavailable")
        self.assertFalse(outcome.degraded_unavailable)
        self.assertFalse(primary_cache.entries)
        self.assertEqual(len(backup_cache.entries), 1)

    def test_network_words_do_not_hide_unknown_auth_or_schema_faults(self) -> None:
        cases = (
            (_incident_stream(terminal=_ROUTING_TIMEOUT + ": permission denied"), "", "command_failed"),
            (_incident_stream(sibling="API Error: 401 Unauthorized"), "", "command_failed"),
            (_incident_stream(), "independent runtime corruption\n", "command_failed"),
            (_incident_stream(), "invalid_json_schema\n", "invalid_output_schema"),
        )
        for stdout, stderr, reason in cases:
            with self.subTest(reason=reason, stderr=stderr, stdout_tail=stdout[-100:]):
                failure, backup, primary_cache, backup_cache = self._exercise(stdout=stdout, stderr=stderr)
                self.assertIsInstance(failure, SemanticRouteAdjudicatorError)
                self.assertEqual(failure.reason_code, reason)
                self.assertFalse(failure.retryable)
                self.assertEqual(backup.calls, 0)
                self.assertFalse(primary_cache.entries)
                self.assertFalse(backup_cache.entries)


class _ScriptedProvider:
    def __init__(self, name: str, reasons: tuple[str | None, ...]) -> None:
        self.provider_identity = _identity(name)
        self.reasons = deque(reasons)
        self.batches: list[SemanticAdjudicationBatch] = []
        self.before_reply: Callable[[], None] | None = None

    def adjudicate_with_result(
        self, batch: SemanticAdjudicationBatch, *, stage_guard: SemanticExecutionGuard | None = None,
    ) -> SemanticProviderResult:
        if stage_guard is not None:
            stage_guard.checkpoint()
        self.batches.append(batch)
        if self.before_reply is not None:
            self.before_reply()
        reason = self.reasons.popleft()
        if reason is not None:
            raise SemanticRouteAdjudicatorError("scripted provider verdict", reason_code=reason, retryable=True)
        return SemanticProviderResult(_decisions(), _RESPONSE_HASH)


def _scheduled_chain(
    primary_reasons: tuple[str | None, ...], backup_reasons: tuple[str | None, ...],
    clock: _Clock, *, transitions: list | None = None,
):  # type: ignore[no-untyped-def]
    primary = _ScriptedProvider("independent-primary", primary_reasons)
    backup = _ScriptedProvider("independent-backup", backup_reasons)
    primary_cache, backup_cache = _Cache(), _Cache()
    cooldown = SharedSemanticNetworkCooldown(
        initial_seconds=60, max_seconds=240, monotonic=clock,
        on_transition=None if transitions is None else transitions.append,
    )
    executor = OrderedSemanticAdjudicationExecutor((
        ConfiguredSemanticProvider(primary, primary_cache),  # type: ignore[arg-type]
        ConfiguredSemanticProvider(backup, backup_cache),  # type: ignore[arg-type]
    ), network_cooldown=cooldown)
    return executor, cooldown, primary, backup, primary_cache, backup_cache


class NetworkWaitContinuationIndependentTests(unittest.TestCase):
    def test_both_unreachable_wait_then_resume_original_group_without_failed_cache(self) -> None:
        clock = _Clock()
        transitions: list = []
        executor, cooldown, primary, backup, pcache, bcache = _scheduled_chain(
            ("transport_unavailable", None), ("timeout",), clock, transitions=transitions,
        )
        batch = _batch()
        with self.assertRaises(SemanticNetworkUnavailable) as first:
            executor.adjudicate(batch, group_hash=_GROUP_HASH)
        self.assertEqual(first.exception.retry_after_seconds, 60)
        self.assertEqual([attempt.reason_code for attempt in first.exception.attempts],
                         ["transport_unavailable", "timeout"])
        self.assertFalse(pcache.entries or bcache.entries)
        clock.advance(59)
        with self.assertRaises(SemanticNetworkUnavailable) as held:
            executor.adjudicate(batch, group_hash=_GROUP_HASH)
        self.assertEqual(held.exception.retry_after_seconds, 1)
        self.assertFalse(held.exception.attempts)
        self.assertEqual((len(primary.batches), len(backup.batches)), (1, 1))
        clock.advance(1)
        recovered = executor.adjudicate(batch, group_hash=_GROUP_HASH)
        self.assertFalse(recovered.degraded_unavailable)
        self.assertEqual(recovered.group_hash, _GROUP_HASH)
        self.assertEqual(recovered.actual_result_identity, primary.provider_identity)
        self.assertTrue(all(item is batch for item in (*primary.batches, *backup.batches)))
        self.assertEqual((len(primary.batches), len(backup.batches)), (2, 1))
        self.assertEqual(len(pcache.entries), 1)
        self.assertFalse(bcache.entries)
        self.assertIsNone(cooldown.pending_seconds())
        self.assertEqual([item.event for item in transitions], ["opened", "closed"])

    def test_long_outage_caps_backoff_without_consuming_a_finite_retry_budget(self) -> None:
        clock = _Clock()
        executor, _cooldown, primary, backup, pcache, bcache = _scheduled_chain(
            ("transport_unavailable",) * 12 + (None,), ("transport_unavailable",) * 12, clock,
        )
        waits = []
        for episode in range(12):
            with self.assertRaises(SemanticNetworkUnavailable) as outage:
                executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
            waits.append(outage.exception.retry_after_seconds)
            # Another group cannot multiply calls during the shared wait.
            with self.assertRaises(SemanticNetworkUnavailable):
                executor.adjudicate(_batch(), group_hash="sha256:" + "7" * 64)
            self.assertEqual((len(primary.batches), len(backup.batches)), (episode + 1, episode + 1))
            self.assertFalse(pcache.entries or bcache.entries)
            clock.advance(outage.exception.retry_after_seconds)
        self.assertEqual(waits, [60, 120] + [240] * 10)
        self.assertFalse(executor.adjudicate(_batch(), group_hash=_GROUP_HASH).degraded_unavailable)

    def test_accepted_group_cache_survives_other_group_outage_and_executor_restart(self) -> None:
        clock = _Clock()
        executor, cooldown, primary, backup, pcache, bcache = _scheduled_chain(
            (None, "transport_unavailable", None), ("transport_unavailable",), clock,
        )
        batch = _batch()
        accepted = executor.adjudicate(batch, group_hash=_GROUP_HASH)
        entry_before = dict(pcache.entries)
        retry_group = "sha256:" + "7" * 64
        with self.assertRaises(SemanticNetworkUnavailable):
            executor.adjudicate(batch, group_hash=retry_group)
        cached = executor.adjudicate(batch, group_hash=_GROUP_HASH)
        self.assertEqual(cached.attempts[0].outcome, "cache_hit")
        self.assertEqual(cached.decisions, accepted.decisions)
        self.assertEqual(pcache.entries, entry_before)
        self.assertEqual((len(primary.batches), len(backup.batches)), (2, 1))
        self.assertEqual(cooldown.pending_seconds(), 60)
        restarted = OrderedSemanticAdjudicationExecutor((
            ConfiguredSemanticProvider(primary, pcache),  # type: ignore[arg-type]
            ConfiguredSemanticProvider(backup, bcache),  # type: ignore[arg-type]
        ), network_cooldown=SharedSemanticNetworkCooldown(monotonic=clock))
        recovered = restarted.adjudicate(batch, group_hash=retry_group)
        self.assertEqual(recovered.group_hash, retry_group)
        self.assertFalse(recovered.degraded_unavailable)
        self.assertEqual(restarted.adjudicate(batch, group_hash=_GROUP_HASH).attempts[0].outcome, "cache_hit")
        self.assertEqual((len(primary.batches), len(backup.batches)), (3, 1))
        self.assertEqual(len(pcache.entries), 2)

    def test_cancelled_probe_releases_admission_without_backup_or_cache(self) -> None:
        clock = _Clock()
        executor, cooldown, primary, backup, pcache, bcache = _scheduled_chain(
            ("transport_unavailable", "cancelled", None), ("transport_unavailable",), clock,
        )
        with self.assertRaises(SemanticNetworkUnavailable):
            executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
        clock.advance(60)
        with self.assertRaises(SemanticRouteAdjudicatorError) as cancelled:
            executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
        self.assertEqual(cancelled.exception.reason_code, "cancelled")
        self.assertEqual(len(backup.batches), 1)
        self.assertFalse(pcache.entries or bcache.entries)
        self.assertIsNone(cooldown.pending_seconds())
        self.assertFalse(executor.adjudicate(_batch(), group_hash=_GROUP_HASH).degraded_unavailable)
        self.assertEqual(len(primary.batches), 3)

    def test_only_one_cross_group_probe_runs_when_shared_cooldown_expires(self) -> None:
        clock = _Clock()
        executor, cooldown, primary, backup, _pcache, _bcache = _scheduled_chain(
            ("transport_unavailable", None), ("transport_unavailable",), clock,
        )
        with self.assertRaises(SemanticNetworkUnavailable):
            executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
        clock.advance(60)
        entered, release = threading.Event(), threading.Event()
        outcomes, errors = [], []

        def hold_probe() -> None:
            entered.set()
            if not release.wait(2):
                raise AssertionError("independent probe was not released")

        def probe() -> None:
            try:
                outcomes.append(executor.adjudicate(_batch(), group_hash=_GROUP_HASH))
            except BaseException as exc:
                errors.append(exc)

        primary.before_reply = hold_probe
        thread = threading.Thread(target=probe)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(SemanticNetworkUnavailable) as other_group:
                executor.adjudicate(_batch(), group_hash="sha256:" + "7" * 64)
            self.assertFalse(other_group.exception.attempts)
            self.assertEqual((len(primary.batches), len(backup.batches)), (2, 1))
            self.assertEqual(cooldown.pending_seconds(), 30)
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len(outcomes), 1)
        self.assertFalse(outcomes[0].degraded_unavailable)
        self.assertIsNone(cooldown.pending_seconds())

    def test_revoked_fence_after_transport_failure_prevents_backup_and_cooldown(self) -> None:
        clock = _Clock()
        executor, cooldown, primary, backup, pcache, bcache = _scheduled_chain(
            ("transport_unavailable",), (None,), clock,
        )
        guard = StageLeaseGuard(clock() + 120, threading.Event(), clock)
        primary.before_reply = lambda: guard.revoke("ownership_lost")
        with self.assertRaises(StageLeaseLost) as revoked:
            executor.adjudicate(_batch(), group_hash=_GROUP_HASH, stage_guard=guard)
        self.assertEqual(revoked.exception.provenance, "ownership_lost")
        self.assertFalse(backup.batches)
        self.assertFalse(pcache.entries or bcache.entries)
        self.assertIsNone(cooldown.pending_seconds())

    def test_transport_primary_still_waits_when_backup_has_a_nonnetwork_availability_problem(self) -> None:
        for backup_reason in ("not_authenticated", "capacity_unavailable", "executable_unavailable"):
            with self.subTest(backup_reason=backup_reason):
                clock = _Clock()
                executor, _cooldown, primary, backup, pcache, bcache = _scheduled_chain(
                    ("transport_unavailable", None), (backup_reason,), clock,
                )
                with self.assertRaises(SemanticNetworkUnavailable):
                    executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
                self.assertFalse(pcache.entries or bcache.entries)
                clock.advance(60)
                self.assertFalse(executor.adjudicate(_batch(), group_hash=_GROUP_HASH).degraded_unavailable)
                self.assertEqual((len(primary.batches), len(backup.batches)), (2, 1))

    def test_pure_account_unavailability_keeps_explicit_existing_degraded_policy(self) -> None:
        executor, cooldown, primary, backup, pcache, bcache = _scheduled_chain(
            ("not_authenticated",), ("capacity_unavailable",), _Clock(),
        )
        outcome = executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
        self.assertTrue(outcome.degraded_unavailable)
        self.assertEqual((len(primary.batches), len(backup.batches)), (1, 1))
        self.assertFalse(pcache.entries or bcache.entries)
        self.assertIsNone(cooldown.pending_seconds())


class NetworkPublicationBoundaryIndependentTests(unittest.TestCase):
    def test_actual_publication_pipeline_releases_producer_context_before_any_readiness_or_p_write(self) -> None:
        from disclosure_anchor.application.contracts.provider_unit import ProviderUnitBuildResult
        from disclosure_anchor.application.services.atomic_publication_request_factory_v4 import (
            RecoverableAtomicPublicationRequestFactoryV4,
        )
        from disclosure_anchor.application.use_cases.prepare_and_publish_whole_document_v4 import (
            PrepareAndPublishWholeDocumentV4,
        )
        from tests.unit.test_atomic_publication_request_builder_v4 import _Guard, _Harness, _draft
        from tests.unit.test_prepare_and_publish_whole_document_v4 import _Uow

        harness = _Harness()
        outage = SemanticNetworkUnavailable("semantic providers unavailable", retry_after_seconds=60)
        harness.builder._semantic_router = mock.Mock()
        harness.builder._semantic_router.route.side_effect = outage
        events = []
        readiness = mock.Mock()
        readiness.reopen_prepared_request.return_value = None
        publisher = mock.Mock()
        factory = RecoverableAtomicPublicationRequestFactoryV4(readiness=readiness, new_request_builder=harness.builder)
        use_case = PrepareAndPublishWholeDocumentV4(
            uow_factory=lambda: _Uow(events), publication_requests=factory,
            readiness=readiness, publisher=publisher,
        )
        with (
            mock.patch("disclosure_anchor.application.services.atomic_publication_request_builder_v4.build_provider_units",
                       return_value=ProviderUnitBuildResult(
                           harness.materialized.receipt.provider_envelope_sha256, (_draft(harness),), (),
                       )),
            self.assertRaises(SemanticNetworkUnavailable) as caught,
        ):
            use_case.execute(checkpoint=harness.fixture.local_materialized, materialized=harness.materialized,
                             claim=mock.sentinel.claim, claim_guard=mock.sentinel.claim_guard, stage_guard=_Guard())
        self.assertIs(caught.exception, outage)
        self.assertEqual(events, ["lease-enter", "lease-exit"])
        readiness.reopen_prepared_request.assert_called_once()
        readiness.prepare_or_replay.assert_not_called()
        readiness.verify_ready.assert_not_called()
        self.assertFalse(publisher.method_calls)

    def test_actual_commit_keeps_durable_authority_and_skips_reopen_until_cooldown_ends(self) -> None:
        from disclosure_anchor.application.services.staged_coordinator_backend_v4 import (
            DurableStagedCoordinatorBackendV4,
        )
        from tests.unit.test_staged_coordinator_backend_v4 import (
            _authority, _backend_with_publisher_error, _guard, _work as durable_work,
        )

        clock = _Clock()
        executor, cooldown, _primary, _backup, _pcache, _bcache = _scheduled_chain(
            ("transport_unavailable",), ("timeout",), clock,
        )
        with self.assertRaises(SemanticNetworkUnavailable) as outage:
            executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
        def configured_backend(**kwargs):  # type: ignore[no-untyped-def]
            return DurableStagedCoordinatorBackendV4(**kwargs, semantic_network_wait=cooldown)

        with mock.patch("tests.unit.test_staged_coordinator_backend_v4.DurableStagedCoordinatorBackendV4",
                        side_effect=configured_backend):
            authority, backend, persistence, notification = _backend_with_publisher_error(outage.exception)
        original = durable_work(authority)
        with self.assertRaises(StageWaiting) as wait:
            backend.commit(original, credit_allowance=ResourceCreditVector(), stage_guard=_guard())
        self.assertEqual(wait.exception.retry_after_seconds, 60)
        self.assertIs(persistence.authority, authority)
        self.assertFalse(persistence.appends)
        notification.assert_not_called()
        backend._materialization.reopen_materialized_v4.assert_called_once()
        backend._publisher.execute.assert_called_once()

        # The second COMMIT must wait before any DB inventory/read or expensive
        # materialization, even though it has no heavy-work permission.
        with mock.patch.object(persistence, "load_owned_authority", wraps=persistence.load_owned_authority) as load:
            with self.assertRaises(StageWaiting):
                backend.commit(original, credit_allowance=ResourceCreditVector(),
                               stage_guard=replace(_guard(), heavy_work_permitted=False))
            load.assert_not_called()
        backend._materialization.reopen_materialized_v4.assert_called_once()
        backend._publisher.execute.assert_called_once()
        self.assertEqual(durable_work(persistence.authority), original)

        clock.advance(60)

        def publish(**kwargs):  # type: ignore[no-untyped-def]
            self.assertIs(kwargs["checkpoint"], authority.checkpoint)
            self.assertEqual(kwargs["claim"], authority.claim_witness)
            persistence.authority = _authority("publish_committed")
            return mock.Mock(previous_active_run_id=None)

        backend._publisher.execute.side_effect = publish
        recovered = backend.commit(original, credit_allowance=ResourceCreditVector(), stage_guard=_guard())
        self.assertEqual(recovered.state, "publish_committed")
        self.assertEqual(recovered.attempt_id, original.attempt_id)
        self.assertEqual(recovered.claim_generation, original.claim_generation)
        self.assertEqual(backend._materialization.reopen_materialized_v4.call_count, 2)
        notification.assert_called_once_with(False)

    def test_actual_request_builder_propagates_wait_before_unit_materialization_or_run_projection(self) -> None:
        from disclosure_anchor.application.contracts.provider_unit import ProviderUnitBuildResult
        from tests.unit.test_atomic_publication_request_builder_v4 import _Guard, _Harness, _draft

        harness = _Harness()
        outage = SemanticNetworkUnavailable("semantic providers unavailable", retry_after_seconds=60)
        harness.builder._semantic_router = mock.Mock()
        harness.builder._semantic_router.route.side_effect = outage
        before_document = replace(harness.document)
        with (
            mock.patch("disclosure_anchor.application.services.atomic_publication_request_builder_v4.build_provider_units",
                       return_value=ProviderUnitBuildResult(
                           harness.materialized.receipt.provider_envelope_sha256, (_draft(harness),), (),
                       )),
            mock.patch.object(harness.builder, "_unit", side_effect=AssertionError("no Unit materialization during wait")),
            self.assertRaises(SemanticNetworkUnavailable) as caught,
        ):
            harness.builder.build(checkpoint=harness.fixture.local_materialized,
                                  materialized=harness.materialized, stage_guard=_Guard())
        self.assertIs(caught.exception, outage)
        self.assertEqual(harness.document, before_document)

    def test_real_coordinator_renews_same_waiting_claim_and_releases_heavy_work_without_retry_stop(self) -> None:
        clock = _Clock()
        executor, cooldown, primary, backup, _pcache, _bcache = _scheduled_chain(
            ("transport_unavailable",) * 3 + (None,), ("transport_unavailable",) * 3, clock,
        )
        original = _work("original-network-task", "local_materialized", 5)
        observations = []
        events = []

        class SemanticBackend(_Backend):
            def commit(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
                if work.attempt_id != original.attempt_id:
                    return super().commit(work, credit_allowance=credit_allowance, stage_guard=stage_guard)
                stage_guard.checkpoint()
                observations.append((work.attempt_id, work.lifecycle_version, work.claim_generation,
                                     work.claim_owner_identity, stage_guard.heavy_work_permitted))
                pending = cooldown.pending_seconds()
                if pending is not None:
                    raise StageWaiting("semantic shared cooldown", retry_after_seconds=pending)
                events.append(("semantic_probe", clock()))
                try:
                    executor.adjudicate(_batch(), group_hash=_GROUP_HASH, stage_guard=stage_guard)
                except SemanticNetworkUnavailable as exc:
                    raise StageWaiting(str(exc), retry_after_seconds=exc.retry_after_seconds) from exc
                return super().commit(work, credit_allowance=credit_allowance, stage_guard=stage_guard)

            def run_local(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
                if not stage_guard.heavy_work_permitted:
                    raise StageHeavyWorkRequired("decode needs permit", retry_after_seconds=1)
                events.append(("other_heavy_local", clock()))
                return super().run_local(work, credit_allowance=credit_allowance, stage_guard=stage_guard)

        backend = SemanticBackend(recoverable=(original, _work("other-task", "materializing", 4)))
        backend.clock = clock
        backend.enforce_lease_expiry = True
        result = StagedParseCoordinator(
            backend=backend, monotonic=clock, progress=lambda _snapshot: clock.advance(1),
            limits=_limits(retry_max_attempts=1, retry_stuck_seconds=1),
        ).run()
        self.assertEqual(result.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertFalse(result.errors)
        self.assertEqual(result.completed, 2)
        self.assertEqual((len(primary.batches), len(backup.batches)), (4, 3))
        self.assertTrue(all(item[0:2] == (original.attempt_id, original.lifecycle_version)
                            and item[2:4] == observations[0][2:4] and item[4] is True
                            for item in observations))
        self.assertTrue(any(attempt == original.attempt_id for attempt, _ in backend.renew_times))
        probes = [at for event, at in events if event == "semantic_probe"]
        self.assertGreaterEqual(probes[1] - probes[0], 60)
        self.assertGreaterEqual(probes[2] - probes[1], 120)
        self.assertGreaterEqual(probes[3] - probes[2], 240)
        other_local = next(at for event, at in events if event == "other_heavy_local")
        self.assertLess(other_local, probes[1])
        self.assertEqual(sum(call == "commit:" + original.attempt_id for call in backend.calls), 1)
        self.assertFalse(any(call.startswith("remote:") for call in backend.calls))
