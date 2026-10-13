"""Network outages: exact terminal evidence, the shared cooldown and the staged wait.

The CLI texts are the pinned runtime's own: codex-cli 0.156.1
(``WorkspaceRoutingError::DiscoveryTimeout``) and Claude Code 2.1.280 (its
API-error mapper for requests that never got an HTTP response). Nothing here
runs a provider, a database or a worker.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from disclosure_anchor.adapters.semantics import codex_cli
from disclosure_anchor.adapters.semantics.claude_cli import ClaudeCliSemanticAdjudicator
from disclosure_anchor.adapters.semantics.codex_cli import CodexCliSemanticAdjudicator
from disclosure_anchor.adapters.semantics.failure_diagnostics import FAILURE_DIAGNOSTIC_DIRECTORY
from disclosure_anchor.adapters.semantics.runtime import (
    build_semantic_runtime,
    log_semantic_network_transition,
)
from disclosure_anchor.application.contracts.semantic_routes import SemanticRouteContractError
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticAdjudicationBatch,
    SemanticAdjudicationCacheEntry,
    SemanticExecutionGuard,
    SemanticNetworkUnavailable,
    SemanticProviderResult,
    SemanticRouteAdjudicatorError,
)
from disclosure_anchor.application.ports.staged_provider_parser import MaterializedProviderDocumentV4
from disclosure_anchor.application.services.semantic_adjudication import (
    ConfiguredSemanticProvider,
    OrderedSemanticAdjudicationExecutor,
    SemanticNetworkTransition,
    SharedSemanticNetworkCooldown,
    semantic_group_cache_key,
)
from disclosure_anchor.application.services.staged_coordinator_backend_v4 import (
    DurableStagedCoordinatorBackendV4,
    SemanticNetworkCommitWait,
    V4StageInputResolver,
)
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseLost
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorTerminal,
    StageCapacityBlocked,
    StageWaiting,
    StagedParseCoordinator,
)
from disclosure_anchor.settings import Settings
from tests.unit._codex_model_catalog_fixture import neutral_catalog, prepare_catalog_sha256
from tests.unit.test_remote_parse_evidence_v4 import _typed_happy_bundle
from tests.unit.test_semantic_adjudication import (
    _Cache, _batch, _decisions, _identity, _GROUP_HASH, _RESPONSE_HASH,
)
from tests.unit.test_semantic_claude_cli import CLAUDE_2_1_280_API_ERROR_ENVELOPE
from tests.unit.test_staged_coordinator_backend_v4 import (
    _Persistence,
    _authority,
    _guard,
    _work as _v4_work,
)
from tests.unit.test_staged_parse_coordinator import _Backend, _Clock, _limits, _work


_TIMEOUT = "workspace routing discovery timed out"
_REPLY = '{"decisions":{"0":{"verdicts":{"forecast_summary":false}}}}'
_OTHER_GROUP = "sha256:" + "7" * 64
# The event types and texts of the retained 2026-10-08T00:49:42Z private record.
_INCIDENT_EVENTS = (
    ("thread.started", None),
    ("turn.started", None),
    *(("error", f"Reconnecting... {index}/5 ({_TIMEOUT})") for index in range(2, 6)),
    ("item.completed", "Falling back from WebSockets to HTTPS transport. " + _TIMEOUT),
    *(("error", f"Reconnecting... {index}/5 ({_TIMEOUT})") for index in range(1, 6)),
    ("error", _TIMEOUT),
    ("turn.failed", _TIMEOUT),
)
_CLAUDE_OFFLINE = "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"


def _compact(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _incident_stdout(*, terminal: str = _TIMEOUT, siblings: tuple[str, ...] = ()) -> str:
    """The incident's codex exec JSONL, serialized as the CLI writes it."""

    lines = [
        _compact({"type": "thread.started", "thread_id": "019a1b2c-3d4e-7f60-8a9b-0c1d2e3f4a5b"}),
        _compact({"type": "turn.started"}),
    ]
    lines += [_compact({"type": "error", "message": f"Reconnecting... {index}/5 ({_TIMEOUT})"})
              for index in range(2, 6)]
    lines.append(_compact({"type": "item.completed", "item": {
        "id": "item_0", "type": "error",
        "message": "Falling back from WebSockets to HTTPS transport. " + _TIMEOUT,
    }}))
    lines += [_compact({"type": "error", "message": f"Reconnecting... {index}/5 ({_TIMEOUT})"})
              for index in range(1, 6)]
    lines += [_compact({"type": "error", "message": sibling}) for sibling in siblings]
    lines.append(_compact({"type": "error", "message": terminal}))
    lines.append(_compact({"type": "turn.failed", "error": {"message": terminal}}))
    return "\n".join(lines) + "\n"


def _codex_success_stdout() -> str:
    return "\n".join((
        _compact({"type": "thread.started", "thread_id": "synthetic-thread"}),
        _compact({"type": "turn.started"}),
        _compact({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": _REPLY}}),
        _compact({"type": "turn.completed", "usage": {}}),
    )) + "\n"


def _claude_envelope(result: str, *, status: int | None = None) -> str:
    return json.dumps({
        **json.loads(CLAUDE_2_1_280_API_ERROR_ENVELOPE),
        "api_error_status": status,
        "result": result,
    })


class _Scripted:
    """A provider replaying one verdict per call (``None`` answers).

    ``hold[n]`` keeps call ``n`` inside the provider until that event is set.
    """

    def __init__(self, provider_id: str, verdicts: tuple[str | None, ...], *, provider: str = "openai") -> None:
        self.provider_identity = _identity(provider_id, provider=provider)
        self.hold: dict[int, threading.Event] = {}
        self.calls = 0
        self._verdicts = deque(verdicts)
        self._started: dict[int, threading.Event] = {}
        self._lock = threading.Lock()

    def started(self, number: int) -> threading.Event:
        with self._lock:
            return self._started.setdefault(number, threading.Event())

    def adjudicate_with_result(
        self, batch: SemanticAdjudicationBatch, *, stage_guard: SemanticExecutionGuard | None = None,
    ) -> SemanticProviderResult:
        with self._lock:
            self.calls += 1
            number = self.calls
        if stage_guard is not None:
            stage_guard.checkpoint()
        self.started(number).set()
        if number in self.hold:
            self.hold[number].wait(5)
        with self._lock:
            verdict = self._verdicts.popleft()
        if verdict is None:
            return SemanticProviderResult(_decisions(), _RESPONSE_HASH)
        raise SemanticRouteAdjudicatorError(f"scripted {verdict}", reason_code=verdict, retryable=True)


def _chain(
    primary: object, backup: object, *, cooldown: SharedSemanticNetworkCooldown | None,
) -> tuple[OrderedSemanticAdjudicationExecutor, _Cache, _Cache]:
    primary_cache, backup_cache = _Cache(), _Cache()
    executor = OrderedSemanticAdjudicationExecutor((
        ConfiguredSemanticProvider(primary, primary_cache),  # type: ignore[arg-type]
        ConfiguredSemanticProvider(backup, backup_cache),  # type: ignore[arg-type]
    ), network_cooldown=cooldown)
    return executor, primary_cache, backup_cache


def _cooldown(
    clock: Callable[[], float],
    transitions: list[SemanticNetworkTransition] | None = None,
    **changes: object,
) -> SharedSemanticNetworkCooldown:
    return SharedSemanticNetworkCooldown(
        monotonic=clock,
        on_transition=None if transitions is None else transitions.append,
        **changes,  # type: ignore[arg-type]
    )


def _answered(primary: _Scripted, group_hash: str) -> dict[str, SemanticAdjudicationCacheEntry]:
    key = semantic_group_cache_key(
        identity=primary.provider_identity, taxonomy_version=_batch().taxonomy.version, group_hash=group_hash,
    )
    return {key: SemanticAdjudicationCacheEntry(
        cache_key=key, group_hash=group_hash, provider=primary.provider_identity,
        decisions=_decisions(), response_sha256=_RESPONSE_HASH,
    )}


class CodexWorkspaceRoutingTimeoutTests(unittest.TestCase):
    def _run(self, stdout: str, stderr: str = "") -> tuple[object, _Scripted, _Cache, _Cache, Path]:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        primary = CodexCliSemanticAdjudicator(
            executable=Path("/synthetic/codex"), runtime_tmp_root=tmp, model_catalog=neutral_catalog(),
        )
        sonnet = _Scripted("sonnet-backup", (None,), provider="anthropic")
        executor, primary_cache, backup_cache = _chain(primary, sonnet, cooldown=None)

        def failed(*, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            # A plausible reply left by the failed process is never read.
            Path(args[args.index("--output-last-message") + 1]).write_text(_REPLY, encoding="utf-8")
            return subprocess.CompletedProcess(args, 1, stdout, stderr)

        with (
            mock.patch("disclosure_anchor.adapters.semantics.codex_cli._run_process", side_effect=failed),
            self.assertLogs("disclosure_anchor.adapters.semantics.codex_cli", level="WARNING"),
        ):
            try:
                result: object = executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
            except SemanticRouteAdjudicatorError as exc:
                result = exc
        return result, sonnet, primary_cache, backup_cache, tmp

    def test_retained_incident_replays_as_transport_and_reaches_sonnet_once(self) -> None:
        stdout = _incident_stdout()
        # The retained record's shape and size (its thread and item ids were
        # never kept, so only the hash itself cannot be reproduced).
        self.assertEqual((len(stdout.encode("utf-8")), stdout.count("\n")), (1215, 14))
        outcome, sonnet, primary_cache, backup_cache, tmp = self._run(stdout)

        self.assertNotIsInstance(outcome, SemanticRouteAdjudicatorError)
        self.assertEqual(
            [(item.ordinal, item.outcome, item.reason_code) for item in outcome.attempts],
            [(1, "availability_failed", "transport_unavailable"), (2, "succeeded", None)],
        )
        self.assertEqual(outcome.actual_result_identity.provider_id, "sonnet-backup")
        self.assertEqual(sonnet.calls, 1)
        self.assertFalse(primary_cache.entries)
        self.assertEqual(len(backup_cache.entries), 1)
        (record_path,) = (tmp / FAILURE_DIAGNOSTIC_DIRECTORY).glob("*.json")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [(event["type"], event.get("text")) for event in record["stdout_events"]],
            list(_INCIDENT_EVENTS),
        )
        self.assertEqual(
            (record["reason_code"], record["retryable"], record["returncode"],
             record["stdout_bytes"], record["stderr_bytes"]),
            ("transport_unavailable", True, 1, 1215, 0),
        )

    def test_routing_siblings_and_altered_timeouts_stay_failed_closed(self) -> None:
        # Every other WorkspaceRoutingError display of rust-v0.156.1, the
        # resolver's owner-missing error, and any variation of the timeout.
        terminals = (
            "workspace routing discovery failed",
            "workspace routing discovery unauthorized (401)",
            "workspace routing discovery cancelled",
            "workspace routing discovery cancelled during shutdown",
            "account changed during workspace routing discovery",
            "configuration changed during workspace routing discovery; retry account/read",
            "failed to load workspace requirements",
            "selected workspace missing from routing discovery",
            "duplicate workspace in routing discovery",
            "workspace routing requires a ChatGPT account id",
            "workspace routing discovery missing backend origin",
            "workspace routing discovery has invalid account routing override",
            "required ChatGPT backend conflicts with workspace routing",
            "workspace backend must use an HTTPS origin without credentials",
            "workspace routing owner is unavailable",
            "Workspace routing discovery timed out",
            "workspace routing discovery timed out after 15s",
            "error: workspace routing discovery timed out",
        )
        cases = [(terminal, _incident_stdout(terminal=terminal), "") for terminal in terminals]
        cases += [
            ("timeout beside a failed discovery",
             _incident_stdout(siblings=("workspace routing discovery failed",)), ""),
            ("timeout beside an unknown stderr line", _incident_stdout(), "routing cache corrupted\n"),
        ]
        for label, stdout, stderr in cases:
            with self.subTest(label):
                failure, sonnet, primary_cache, backup_cache, _tmp = self._run(stdout, stderr)
                self.assertIsInstance(failure, SemanticRouteAdjudicatorError)
                self.assertEqual(failure.reason_code, "command_failed")
                self.assertFalse(failure.retryable)
                self.assertEqual(sonnet.calls, 0)
                self.assertFalse(primary_cache.entries or backup_cache.entries)


_DROPPED = ("ECONNRESET", "EPIPE", "ECONNABORTED", "ConnectionClosed", "ERR_SOCKET_CLOSED", "UND_ERR_SOCKET")
_REFUSED = ("ECONNREFUSED", "ConnectionRefused")
_UNREACHABLE = ("ENOTFOUND", "EAI_AGAIN", "FailedToOpenSocket")
_NO_ROUTE = ("ENETUNREACH", "ENETDOWN", "EHOSTUNREACH", "EHOSTDOWN")
_CLAUDE_TRANSPORT_RESULTS = (
    "Request timed out",
    "API Error: Request timed out. Check your internet connection and proxy settings",
    "API Error: Unable to connect to API. Check your internet connection",
    *(f"API Error: Connection dropped ({code})" for code in _DROPPED),
    *(f"API Error: Connection refused — a firewall or proxy may be blocking it ({code})" for code in _REFUSED),
    *(f"API Error: Can't reach the API server — check your internet or DNS ({code})" for code in _UNREACHABLE),
    *(f"API Error: No internet route — check your connection or VPN ({code})" for code in _NO_ROUTE),
    "API Error: Connection lost while your computer was asleep",
    *(
        f"API Error: Connection to the API was lost ({code}). This is usually temporary — try again."
        for code in (*_DROPPED, "ETIMEDOUT", "StreamSuspended", *_REFUSED, *_UNREACHABLE, *_NO_ROUTE)
    ),
)


class ClaudeOfflineResultTests(unittest.TestCase):
    def _error(self, stdout: str, stderr: str = "") -> SemanticRouteAdjudicatorError:
        adapter = ClaudeCliSemanticAdjudicator(executable=Path("/synthetic/claude"))
        with (
            mock.patch(
                "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                return_value=subprocess.CompletedProcess(["claude"], 1, stdout, stderr),
            ),
            self.assertLogs("disclosure_anchor.adapters.semantics.claude_cli", level="WARNING"),
            self.assertRaises(SemanticRouteAdjudicatorError) as caught,
        ):
            adapter.adjudicate_with_result(_batch())
        return caught.exception

    def test_every_offline_result_sentence_is_transport(self) -> None:
        for result in _CLAUDE_TRANSPORT_RESULTS:
            with self.subTest(result=result):
                error = self._error(_claude_envelope(result))
                self.assertEqual((error.reason_code, error.retryable), ("transport_unavailable", True))
        # The same complete sentence alone on stderr, without an envelope.
        for result in (_CLAUDE_OFFLINE, "Request timed out"):
            with self.subTest(stderr=result):
                self.assertEqual(self._error("", result + "\n").reason_code, "transport_unavailable")

    def test_adjacent_connection_security_and_status_forms_stay_failed_closed(self) -> None:
        cases = (
            (_claude_envelope("API Error: Unable to connect to API: SSL certificate has expired"), ""),
            (_claude_envelope(
                "API Error: Unable to connect to API: Self-signed certificate detected (SELF_SIGNED_CERT_IN_CHAIN)."
            ), ""),
            (_claude_envelope(
                "API Error: Couldn't connect through your proxy (ERR_PROXY_TUNNEL) — the proxy refused the "
                "tunnel: check its credentials and that it allows this host"
            ), ""),
            (_claude_envelope("API Error: Unable to connect to API (EPROTO)"), ""),
            (_claude_envelope("API Error: Connection dropped (EPROTO)"), ""),
            (_claude_envelope(
                "API Error: Connection to the API was lost (ERR_PROXY_TUNNEL). This is usually temporary — try again."
            ), ""),
            (_claude_envelope(
                "API Error: No response from API (waited 1m). If a proxy or gateway on your network holds "
                "responses until they complete, raise API_TIMEOUT_MS or CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS "
                "to wait longer."
            ), ""),
            (_claude_envelope("request timed out"), ""),
            (_claude_envelope("Request timed out."), ""),
            (_claude_envelope("API Error: Connection refused - a firewall or proxy may be blocking it (ECONNREFUSED)"), ""),
            (_claude_envelope("API Error: Can’t reach the API server — check your internet or DNS (ENOTFOUND)"), ""),
            # A typed HTTP status keeps precedence over network wording.
            (_claude_envelope(_CLAUDE_OFFLINE, status=401), ""),
            (_claude_envelope(_CLAUDE_OFFLINE, status=503), ""),
            # Every inspected line must agree on one family.
            (_claude_envelope(_CLAUDE_OFFLINE), "unrecognized runtime fault\n"),
            (_claude_envelope(_CLAUDE_OFFLINE), "Not logged in · Please run /login\n"),
        )
        for stdout, stderr in cases:
            with self.subTest(stdout=stdout[-120:], stderr=stderr):
                error = self._error(stdout, stderr)
                self.assertEqual((error.reason_code, error.retryable), ("command_failed", False))


class SharedNetworkCooldownTests(unittest.TestCase):
    def test_real_cli_adapters_wait_through_a_shared_outage_then_resume_with_the_primary(self) -> None:
        clock = _Clock()
        transitions: list[SemanticNetworkTransition] = []
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        codex = CodexCliSemanticAdjudicator(
            executable=Path("/synthetic/codex"), runtime_tmp_root=tmp, model_catalog=neutral_catalog(),
        )
        claude = ClaudeCliSemanticAdjudicator(executable=Path("/synthetic/claude"))
        executor, codex_cache, claude_cache = _chain(codex, claude, cooldown=_cooldown(clock, transitions))
        calls = {"codex": 0, "claude": 0}
        network_up = threading.Event()

        def run(*, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[0].endswith("claude"):
                calls["claude"] += 1
                return subprocess.CompletedProcess(args, 1, _claude_envelope(_CLAUDE_OFFLINE), "")
            calls["codex"] += 1
            if not network_up.is_set():
                return subprocess.CompletedProcess(args, 1, _incident_stdout(), "")
            Path(args[args.index("--output-last-message") + 1]).write_text(_REPLY, encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, _codex_success_stdout(), "")

        with (
            mock.patch("disclosure_anchor.adapters.semantics.codex_cli._run_process", side_effect=run),
            self.assertLogs("disclosure_anchor.adapters.semantics", level="WARNING"),
        ):
            with self.assertRaises(SemanticNetworkUnavailable) as outage:
                executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
            self.assertEqual(
                [(item.provider.provider_id, item.reason_code) for item in outage.exception.attempts],
                [("luna-primary", "transport_unavailable"), ("sonnet-backup", "transport_unavailable")],
            )
            self.assertEqual(outage.exception.retry_after_seconds, 60)
            self.assertFalse(codex_cache.entries or claude_cache.entries)
            # While it cools down neither CLI runs, for any group.
            clock.advance(59)
            for group in (_GROUP_HASH, _OTHER_GROUP):
                with self.assertRaises(SemanticNetworkUnavailable) as held:
                    executor.adjudicate(_batch(), group_hash=group)
                self.assertEqual((held.exception.retry_after_seconds, held.exception.attempts), (1, ()))
            self.assertEqual(calls, {"codex": 1, "claude": 1})
            network_up.set()
            clock.advance(1)
            recovered = executor.adjudicate(_batch(), group_hash=_GROUP_HASH)

        self.assertEqual(recovered.actual_result_identity.provider_id, "luna-primary")
        self.assertEqual([item.outcome for item in recovered.attempts], ["succeeded"])
        self.assertEqual(calls, {"codex": 2, "claude": 1})
        self.assertEqual(len(codex_cache.entries), 1)
        self.assertFalse(claude_cache.entries)
        self.assertEqual([item.event for item in transitions], ["opened", "closed"])
        self.assertEqual(transitions[1].outage_seconds, 60)

    def test_one_probe_after_the_cooldown_while_concurrent_groups_wait_without_calls(self) -> None:
        clock = _Clock()
        primary = _Scripted("primary", ("transport_unavailable", None, None))
        backup = _Scripted("backup", ("transport_unavailable",), provider="anthropic")
        executor, _primary_cache, _backup_cache = _chain(primary, backup, cooldown=_cooldown(clock))
        with self.assertRaises(SemanticNetworkUnavailable):
            executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
        clock.advance(60)
        release = threading.Event()
        primary.hold[2] = release
        probe: list[object] = []
        thread = threading.Thread(
            target=lambda: probe.append(executor.adjudicate(_batch(), group_hash=_GROUP_HASH)), daemon=True,
        )
        thread.start()
        self.assertTrue(primary.started(2).wait(2))
        with self.assertRaises(SemanticNetworkUnavailable) as busy:
            executor.adjudicate(_batch(), group_hash=_OTHER_GROUP)
        self.assertEqual((busy.exception.retry_after_seconds, busy.exception.attempts), (30, ()))
        self.assertEqual((primary.calls, backup.calls), (2, 1))
        release.set()
        thread.join(2)
        self.assertFalse(probe[0].degraded_unavailable)
        self.assertFalse(executor.adjudicate(_batch(), group_hash=_OTHER_GROUP).degraded_unavailable)
        self.assertEqual((primary.calls, backup.calls), (3, 1))

    def test_a_group_admitted_before_the_outage_skips_its_backup_once_the_circuit_opens(self) -> None:
        clock = _Clock()
        primary = _Scripted("primary", ("transport_unavailable", "transport_unavailable"))
        backup = _Scripted("backup", ("transport_unavailable",), provider="anthropic")
        executor, primary_cache, backup_cache = _chain(primary, backup, cooldown=_cooldown(clock))
        release = threading.Event()
        primary.hold[1] = release
        early: list[BaseException] = []

        def run_early() -> None:
            try:
                executor.adjudicate(_batch(), group_hash=_OTHER_GROUP)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                early.append(exc)

        thread = threading.Thread(target=run_early, daemon=True)
        thread.start()
        self.assertTrue(primary.started(1).wait(2))
        with self.assertRaises(SemanticNetworkUnavailable):
            executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
        release.set()
        thread.join(2)
        # The early group's primary failed after the circuit opened: it waits
        # with its one observed attempt instead of spending a backup call.
        (failure,) = early
        self.assertIsInstance(failure, SemanticNetworkUnavailable)
        self.assertEqual(
            [(item.provider.provider_id, item.reason_code) for item in failure.attempts],
            [("primary", "transport_unavailable")],
        )
        self.assertEqual((primary.calls, backup.calls), (2, 1))
        self.assertFalse(primary_cache.entries or backup_cache.entries)

    def test_degraded_or_failed_closed_probes_never_claim_recovery(self) -> None:
        clock = _Clock()
        transitions: list[SemanticNetworkTransition] = []
        primary = _Scripted("primary", (
            "transport_unavailable", "not_authenticated", "invalid_output_schema", "transport_unavailable", None,
        ))
        backup = _Scripted(
            "backup", ("transport_unavailable", "capacity_unavailable", "transport_unavailable"), provider="anthropic",
        )
        executor, primary_cache, backup_cache = _chain(primary, backup, cooldown=_cooldown(clock, transitions))
        with self.assertRaises(SemanticNetworkUnavailable):
            executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
        clock.advance(60)
        # The deliberate non-network policy still degrades this group...
        self.assertTrue(executor.adjudicate(_batch(), group_hash=_GROUP_HASH).degraded_unavailable)
        # ...a failed-closed probe still stops without a backup call...
        with self.assertRaises(SemanticRouteAdjudicatorError) as stopped:
            executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
        self.assertNotIsInstance(stopped.exception, SemanticNetworkUnavailable)
        self.assertEqual(stopped.exception.reason_code, "invalid_output_schema")
        self.assertEqual(backup.calls, 2)
        # ...and neither closed the circuit: the next outage is a failed probe
        # with the doubled delay, not a first outage.
        with self.assertRaises(SemanticNetworkUnavailable) as again:
            executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
        self.assertEqual(again.exception.retry_after_seconds, 120)
        self.assertEqual([item.event for item in transitions], ["opened", "probe_failed"])
        self.assertEqual(transitions[1].consecutive_outages, 2)
        self.assertFalse(primary_cache.entries or backup_cache.entries)
        clock.advance(120)
        self.assertFalse(executor.adjudicate(_batch(), group_hash=_GROUP_HASH).degraded_unavailable)
        self.assertEqual([item.event for item in transitions], ["opened", "probe_failed", "closed"])

    def test_an_answer_that_fails_validation_closes_the_circuit_but_is_never_cached(self) -> None:
        clock = _Clock()
        transitions: list[SemanticNetworkTransition] = []
        primary = _Scripted("primary", ("transport_unavailable", None))
        backup = _Scripted("backup", ("transport_unavailable",), provider="anthropic")
        executor, primary_cache, backup_cache = _chain(primary, backup, cooldown=_cooldown(clock, transitions))
        with self.assertRaises(SemanticNetworkUnavailable):
            executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
        clock.advance(60)

        def reject(_decisions: object) -> None:
            raise SemanticRouteContractError("semantic model removed an exact title route")

        with self.assertRaises(SemanticRouteAdjudicatorError) as rejected:
            executor.adjudicate(_batch(), group_hash=_GROUP_HASH, validate=reject)
        self.assertEqual(rejected.exception.reason_code, "invalid_decision")
        self.assertFalse(primary_cache.entries or backup_cache.entries)
        self.assertEqual([item.event for item in transitions], ["opened", "closed"])

    def test_unscheduled_callers_keep_the_degraded_outcome(self) -> None:
        primary = _Scripted("primary", ("transport_unavailable",))
        backup = _Scripted("backup", ("transport_unavailable",), provider="anthropic")
        executor, primary_cache, backup_cache = _chain(primary, backup, cooldown=None)
        outcome = executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
        self.assertTrue(outcome.degraded_unavailable)
        self.assertFalse(primary_cache.entries or backup_cache.entries)

    def test_only_the_staged_composition_gets_the_shared_cooldown(self) -> None:
        ambient = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("DISCLOSURE_SEMANTIC_", "DATABASE_URL", "DISCLOSURE_RUNTIME_ROOT"))
        }
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, ambient, clear=True))
        runtime_root = tmp / "runtime"
        settings = Settings(
            disclosure_data_root=tmp / "data",
            disclosure_shared_root=tmp / "shared",
            disclosure_runtime_root=runtime_root,
            mineru_model_cache=tmp / "shared" / "mineru",
            hf_home=tmp / "shared" / "hf",
            modelscope_cache=tmp / "shared" / "modelscope",
            disclosure_semantic_codex_bin=tmp / "absent" / "codex",
            disclosure_semantic_codex_model_catalog_sha256=prepare_catalog_sha256(runtime_root),
        )
        plain = build_semantic_runtime(settings=settings, paths=mock.Mock(), artifacts=mock.Mock())
        staged = build_semantic_runtime(
            settings=settings, paths=mock.Mock(), artifacts=mock.Mock(), network_wait=True,
        )
        self.assertIsNone(plain.network_wait)
        self.assertIsNone(plain.router.executor._network_cooldown)
        self.assertIsInstance(staged.network_wait, SharedSemanticNetworkCooldown)
        self.assertIs(staged.router.executor._network_cooldown, staged.network_wait)
        # The worker's stop of semantic provider calls also ends network waits.
        with mock.patch.object(codex_cli._SEMANTIC_SHUTDOWN_REQUESTED, "is_set", return_value=True):
            self.assertTrue(staged.network_wait.shutdown_requested())

    def test_transition_log_is_bounded_and_content_free(self) -> None:
        transition = SemanticNetworkTransition(
            event="opened", consecutive_outages=1, outage_seconds=0.0, retry_after_seconds=60.0,
            group_hash="https://user:secret@example.invalid/x",
            reasons=(("luna-primary", "transport_unavailable"), ("Bearer secret-token", "x y")),
        )
        with self.assertLogs("disclosure_anchor.adapters.semantics.runtime", level="WARNING") as logs:
            log_semantic_network_transition(transition)
        (line,) = logs.output
        self.assertNotIn("secret", line)
        record = json.loads(line.split("semantic_network_wait ", 1)[1])
        self.assertEqual(set(record), {
            "event", "transition", "observed_at_unix_ns", "consecutive_outages", "outage_seconds",
            "retry_after_seconds", "group_hash", "attempts",
        })
        self.assertEqual((record["event"], record["transition"], record["group_hash"]),
                         ("semantic_network_wait.v1", "opened", None))
        self.assertEqual(record["attempts"], [["luna-primary", "transport_unavailable"],
                                              ["unrecognized", "unrecognized"]])


class _RoutingPublisher:
    """The V4 publisher seen by COMMIT: route one group, then commit transaction P.

    Every fixture attempt reopens the same durable checkpoint, so the test
    names the group of the document it commits next.
    """

    def __init__(self, executor: OrderedSemanticAdjudicationExecutor, persistence: _Persistence) -> None:
        self.executor = executor
        self.persistence = persistence
        self.group = _GROUP_HASH

    def __call__(self, **kwargs: object) -> object:
        self.executor.adjudicate(
            _batch(), group_hash=self.group, stage_guard=kwargs["stage_guard"],  # type: ignore[arg-type]
        )
        self.persistence.authority = _authority("publish_committed")
        return mock.Mock(previous_active_run_id=None)


def _commit_backend(cooldown: SharedSemanticNetworkCooldown, primary: _Scripted, backup: _Scripted):  # type: ignore[no-untyped-def]
    """A real V4 backend whose transaction-P publisher routes through ``cooldown``."""

    authority = _authority("local_materialized")
    (_, _, values, _, manifest, envelope, _, _, _) = _typed_happy_bundle()
    materialization = mock.Mock()
    materialization.reopen_materialized_v4.return_value = MaterializedProviderDocumentV4(
        receipt=values[6], intent=values[5], provider_envelope=envelope, manifest=manifest,
    )
    persistence = _Persistence(authority)
    inputs = mock.Mock(spec=V4StageInputResolver)
    inputs.bind_stage.return_value = inputs
    executor, primary_cache, _backup_cache = _chain(primary, backup, cooldown=cooldown)
    publisher = _RoutingPublisher(executor, persistence)
    published = mock.Mock()
    backend = DurableStagedCoordinatorBackendV4(
        persistence=persistence,  # type: ignore[arg-type]
        inputs=inputs,
        remote=mock.Mock(),
        materialization=materialization,
        secret_cipher=mock.Mock(),
        claim_guard=mock.Mock(),
        publisher=mock.Mock(execute=mock.Mock(side_effect=publisher)),
        poll_seconds=0.25,
        publication_committed=published,
        semantic_network_wait=cooldown,
    )
    return authority, backend, persistence, published, publisher, primary_cache


class StagedCommitNetworkWaitTests(unittest.TestCase):
    def test_outage_waits_cheaply_and_the_same_attempt_commits_after_recovery(self) -> None:
        clock = _Clock()
        primary = _Scripted("primary", ("transport_unavailable", None))
        backup = _Scripted("backup", ("transport_unavailable",), provider="anthropic")
        authority, backend, persistence, published, _publisher, primary_cache = _commit_backend(
            _cooldown(clock), primary, backup,
        )
        work = _v4_work(authority)
        reopen = backend._materialization.reopen_materialized_v4

        with self.assertRaises(StageWaiting) as first:
            backend.commit(work, credit_allowance=ResourceCreditVector(), stage_guard=_guard())
        self.assertNotIsInstance(first.exception, StageCapacityBlocked)
        self.assertEqual(first.exception.retry_after_seconds, 60)
        self.assertIsInstance(first.exception.__cause__, SemanticNetworkUnavailable)
        self.assertEqual((reopen.call_count, primary.calls, backup.calls), (1, 1, 1))
        self.assertTrue(backend._semantic_network.waiting(work.attempt_id))

        clock.advance(30)
        with self.assertRaises(StageWaiting) as cheap:
            backend.commit(work, credit_allowance=ResourceCreditVector(), stage_guard=_guard())
        self.assertEqual(cheap.exception.retry_after_seconds, 30)
        # Waiting again reopens nothing and calls no provider.
        self.assertEqual((reopen.call_count, backend._publisher.execute.call_count), (1, 1))
        self.assertEqual((primary.calls, backup.calls), (1, 1))
        self.assertEqual(persistence.appends, [])
        published.assert_not_called()

        clock.advance(30)
        committed = backend.commit(work, credit_allowance=ResourceCreditVector(), stage_guard=_guard())
        self.assertEqual((committed.state, committed.attempt_id), ("publish_committed", work.attempt_id))
        self.assertEqual((reopen.call_count, primary.calls, backup.calls), (2, 2, 1))
        self.assertEqual(len(primary_cache.entries), 1)
        self.assertFalse(backend._semantic_network.waiting(work.attempt_id))
        self.assertEqual(persistence.appends, [])
        published.assert_called_once_with(False)

    def test_many_ready_attempts_share_one_outage_without_extra_calls_or_rebuilds(self) -> None:
        clock = _Clock()
        primary = _Scripted("primary", ("transport_unavailable",) + (None,) * 6)
        backup = _Scripted("backup", ("transport_unavailable",), provider="anthropic")
        authority, backend, persistence, published, publisher, primary_cache = _commit_backend(
            _cooldown(clock), primary, backup,
        )
        works = [replace(_v4_work(authority), attempt_id=f"attempt-{index}") for index in range(6)]
        reopen = backend._materialization.reopen_materialized_v4

        def commit_all() -> list[object]:
            outcomes: list[object] = []
            for index, work in enumerate(works):
                persistence.authority = _authority("local_materialized")
                publisher.group = "sha256:" + f"{index:x}" * 64
                try:
                    outcomes.append(
                        backend.commit(work, credit_allowance=ResourceCreditVector(), stage_guard=_guard())
                    )
                except StageWaiting as exc:
                    outcomes.append(exc)
            return outcomes

        self.assertTrue(all(isinstance(item, StageWaiting) for item in commit_all()))
        self.assertEqual((primary.calls, backup.calls, reopen.call_count), (1, 1, 6))
        clock.advance(30)
        self.assertTrue(all(isinstance(item, StageWaiting) for item in commit_all()))
        self.assertEqual((primary.calls, backup.calls, reopen.call_count), (1, 1, 6))
        clock.advance(30)
        recovered = commit_all()
        self.assertEqual([getattr(item, "state", None) for item in recovered], ["publish_committed"] * 6)
        self.assertEqual((primary.calls, backup.calls, reopen.call_count), (7, 1, 12))
        self.assertEqual(len(primary_cache.entries), 6)
        self.assertEqual(published.call_count, 6)

    def test_operator_stop_ends_a_network_wait_as_a_retry_neutral_cancellation(self) -> None:
        clock = _Clock()
        stopping = threading.Event()
        primary = _Scripted("primary", ("transport_unavailable",))
        backup = _Scripted("backup", ("transport_unavailable",), provider="anthropic")
        authority, backend, persistence, published, _publisher, _cache = _commit_backend(
            _cooldown(clock, shutdown_requested=stopping.is_set), primary, backup,
        )
        work = _v4_work(authority)
        with self.assertRaises(StageWaiting):
            backend.commit(work, credit_allowance=ResourceCreditVector(), stage_guard=_guard())
        stopping.set()
        # The same retry-neutral cancellation a stopped provider call raises
        # (the coordinator test below shows the drain it ends).
        with self.assertRaises(SemanticRouteAdjudicatorError) as cancelled:
            backend.commit(work, credit_allowance=ResourceCreditVector(), stage_guard=_guard())
        self.assertNotIsInstance(cancelled.exception, SemanticNetworkUnavailable)
        self.assertEqual((cancelled.exception.reason_code, cancelled.exception.retryable), ("cancelled", True))
        # A drain that revoked the stage first is a cancellation as well.
        revoked = _guard()
        revoked.revoke("operator_cancel")
        with self.assertRaises(StageLeaseLost) as lost:
            backend.commit(work, credit_allowance=ResourceCreditVector(), stage_guard=revoked)
        self.assertEqual(lost.exception.provenance, "operator_cancel")
        self.assertEqual(backend._materialization.reopen_materialized_v4.call_count, 1)
        self.assertEqual((primary.calls, backup.calls), (1, 1))
        self.assertEqual(persistence.appends, [])
        published.assert_not_called()

    def test_an_attempt_that_did_not_wait_is_not_held_by_the_open_cooldown(self) -> None:
        clock = _Clock()
        primary = _Scripted("primary", ("transport_unavailable",))
        backup = _Scripted("backup", ("transport_unavailable",), provider="anthropic")
        cooldown = _cooldown(clock)
        authority, backend, persistence, published, publisher, primary_cache = _commit_backend(
            cooldown, primary, backup,
        )
        waiting = replace(_v4_work(authority), attempt_id="waiting-attempt")
        cached = replace(_v4_work(authority), attempt_id="cached-attempt")
        # The cached document's only group was answered before the outage.
        primary_cache.entries.update(_answered(primary, _OTHER_GROUP))
        with self.assertRaises(StageWaiting):
            backend.commit(waiting, credit_allowance=ResourceCreditVector(), stage_guard=_guard())
        self.assertIsNotNone(cooldown.pending_seconds())
        publisher.group = _OTHER_GROUP
        committed = backend.commit(cached, credit_allowance=ResourceCreditVector(), stage_guard=_guard())
        self.assertEqual(committed.state, "publish_committed")
        self.assertEqual((primary.calls, backup.calls), (1, 1))
        published.assert_called_once_with(False)
        self.assertEqual(persistence.appends, [])


class _NetworkCommitBackend(_Backend):
    """The coordinator test backend with its COMMIT routed through the real wait path."""

    def __init__(self, *, recoverable, executor, waits: SemanticNetworkCommitWait) -> None:  # type: ignore[no-untyped-def]
        super().__init__(recoverable=recoverable)
        self.executor = executor
        self.waits = waits
        self.rebuilds: dict[str, int] = {}
        self.active_commits = 0
        self.peak_commits = 0
        self._commit_lock = threading.Lock()

    def commit(self, work, *, credit_allowance, stage_guard):  # type: ignore[no-untyped-def]
        self.waits.raise_if_pending(work.attempt_id, stage_guard)
        with self._commit_lock:
            # Where the real COMMIT reopens and rebuilds the whole request.
            self.rebuilds[work.attempt_id] = self.rebuilds.get(work.attempt_id, 0) + 1
            self.active_commits += 1
            self.peak_commits = max(self.peak_commits, self.active_commits)
        try:
            with self.waits.converting(work.attempt_id, stage_guard):
                self.executor.adjudicate(
                    _batch(), group_hash="sha256:" + work.attempt_id[-1] * 64, stage_guard=stage_guard,
                )
            return super().commit(work, credit_allowance=credit_allowance, stage_guard=stage_guard)
        finally:
            with self._commit_lock:
                self.active_commits -= 1


def _run_coordinator(backend: _Backend, *, limits, stop_requested=lambda: False):  # type: ignore[no-untyped-def]
    result: list[object] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            result.append(StagedParseCoordinator(backend=backend, limits=limits).run(stop_requested=stop_requested))
        except BaseException as exc:  # noqa: BLE001 - asserted by the caller
            failures.append(exc)

    thread = threading.Thread(target=run, name="network-wait-coordinator", daemon=True)
    thread.start()
    return thread, result, failures


class CoordinatorNetworkWaitTests(unittest.TestCase):
    def test_outage_wait_frees_the_only_heavy_permit_and_every_original_attempt_commits(self) -> None:
        primary = _Scripted("primary", ("transport_unavailable",) * 2 + (None,) * 3)
        backup = _Scripted("backup", ("transport_unavailable",) * 2, provider="anthropic")
        cooldown = SharedSemanticNetworkCooldown(initial_seconds=0.05, max_seconds=0.1, probe_wait_seconds=0.02)
        executor, primary_cache, backup_cache = _chain(primary, backup, cooldown=cooldown)
        works = tuple(_work(f"network-{index}", "local_materialized", 5) for index in range(3))
        backend = _NetworkCommitBackend(
            recoverable=works, executor=executor, waits=SemanticNetworkCommitWait(cooldown),
        )
        thread, result, failures = _run_coordinator(
            backend, limits=_limits(commit_workers=2, cleanup_workers=1, heavy_work_permits=1),
        )
        thread.join(10)
        self.assertFalse(thread.is_alive(), "coordinator did not finish after the network returned")
        self.assertEqual(failures, [])
        (outcome,) = result
        self.assertEqual(outcome.terminal, CoordinatorTerminal.QUIESCENT)
        self.assertIsNone(outcome.stop_cause)
        self.assertEqual(dict(outcome.final_states), {work.attempt_id: "acked" for work in works})
        self.assertFalse([error for error in outcome.errors if "retry budget" in error])
        # One outage, one failed probe, then one answer per group.
        self.assertEqual((primary.calls, backup.calls), (5, 2))
        self.assertEqual((len(primary_cache.entries), len(backup_cache.entries)), (3, 0))
        self.assertEqual(backend.peak_commits, 1)
        self.assertTrue(all(count <= 3 for count in backend.rebuilds.values()), backend.rebuilds)

    def test_operator_drain_during_an_outage_ends_promptly_and_a_restart_resumes(self) -> None:
        stopping = threading.Event()
        primary = _Scripted("primary", ("transport_unavailable",) * 8)
        backup = _Scripted("backup", ("transport_unavailable",) * 8, provider="anthropic")
        cooldown = SharedSemanticNetworkCooldown(
            initial_seconds=0.3, max_seconds=0.3, probe_wait_seconds=0.05, shutdown_requested=stopping.is_set,
        )
        executor, _primary_cache, _backup_cache = _chain(primary, backup, cooldown=cooldown)
        works = tuple(_work(f"drain-{index}", "local_materialized", 5) for index in range(2))
        backend = _NetworkCommitBackend(
            recoverable=works, executor=executor, waits=SemanticNetworkCommitWait(cooldown),
        )
        thread, result, failures = _run_coordinator(
            backend, limits=_limits(commit_workers=2, cleanup_workers=1, heavy_work_permits=1),
            stop_requested=stopping.is_set,
        )
        deadline = time.monotonic() + 5
        while cooldown.pending_seconds() is None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNotNone(cooldown.pending_seconds(), "the outage never opened the cooldown")
        stopped_at = time.monotonic()
        stopping.set()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "an operator drain waited for the network")
        self.assertLess(time.monotonic() - stopped_at, 2.0)
        self.assertEqual(failures, [])
        (drained,) = result
        self.assertEqual(drained.termination_kind, "operator_drain")
        self.assertIsNone(drained.stop_cause)
        self.assertEqual(dict(drained.final_states), {})
        self.assertEqual((primary.calls, backup.calls), (1, 1))

        # A new process resumes the same durable attempts once the network is back.
        primary_up = _Scripted("primary", (None, None))
        backup_up = _Scripted("backup", (), provider="anthropic")
        restarted_cooldown = SharedSemanticNetworkCooldown()
        restarted_executor, _cache_a, _cache_b = _chain(primary_up, backup_up, cooldown=restarted_cooldown)
        restarted = _NetworkCommitBackend(
            recoverable=works, executor=restarted_executor, waits=SemanticNetworkCommitWait(restarted_cooldown),
        )
        thread, result, failures = _run_coordinator(restarted, limits=_limits(commit_workers=2, cleanup_workers=1))
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(dict(result[0].final_states), {work.attempt_id: "acked" for work in works})
        self.assertEqual(
            restarted.claimed_generations, {work.attempt_id: work.claim_generation + 1 for work in works},
        )
        self.assertEqual((primary_up.calls, backup_up.calls), (2, 0))


if __name__ == "__main__":
    unittest.main()
