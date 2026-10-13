"""Offline Codex CLI outage classification through the ordered backup chain."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.adapters.semantics.codex_cli import CodexCliSemanticAdjudicator
from disclosure_anchor.application.ports.semantic_routes import SemanticRouteAdjudicatorError
from disclosure_anchor.application.services.semantic_adjudication import (
    ConfiguredSemanticProvider,
    OrderedSemanticAdjudicationExecutor,
)
from tests.unit._codex_model_catalog_fixture import neutral_catalog
from tests.unit.test_semantic_adjudication import (
    _Adapter, _Cache, _decisions, _identity, _batch, _GROUP_HASH, _RESPONSE_HASH,
)


QUOTA = "Quota exceeded. Check your plan and billing details."


def _event(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _failed(message: str, *siblings: str) -> str:
    return "\n".join((
        _event({"type": "thread.started", "thread_id": "synthetic-thread"}),
        _event({"type": "turn.started"}),
        *siblings,
        _event({"type": "error", "message": message}),
        _event({"type": "turn.failed", "error": {"message": message}}),
    ))


def _completed() -> str:
    return "\n".join((
        _event({"type": "thread.started", "thread_id": "synthetic-thread"}),
        _event({"type": "turn.started"}),
        _event({"type": "item.completed", "item": {
            "id": "item_1", "type": "agent_message",
            "text": '{"decisions":{"0":{"verdicts":{"forecast_summary":false}}}}',
        }}),
        _event({"type": "turn.completed", "usage": {}}),
    ))


class CodexOutageFailoverTests(unittest.TestCase):
    def _run_chain(
        self, *, stdout: str, stderr: str = "", returncode: int = 1, write_result: bool = False,
    ) -> tuple[object | None, SemanticRouteAdjudicatorError | None, int, int, _Cache, _Cache]:
        with tempfile.TemporaryDirectory() as tmp:
            primary = CodexCliSemanticAdjudicator(
                executable=Path("/synthetic/codex"), runtime_tmp_root=Path(tmp),
                model_catalog=neutral_catalog(),
            )
            sonnet = _Adapter(_identity("sonnet-backup", provider="anthropic"))
            primary_cache, backup_cache = _Cache(), _Cache()
            executor = OrderedSemanticAdjudicationExecutor((
                ConfiguredSemanticProvider(adapter=primary, cache=primary_cache),
                ConfiguredSemanticProvider(adapter=sonnet, cache=backup_cache),
            ))
            calls = 0

            def run(*, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal calls
                calls += 1
                if returncode == 0 or write_result:
                    Path(args[args.index("--output-last-message") + 1]).write_text(
                        '{"decisions":{"0":{"verdicts":{"forecast_summary":false}}}}',
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess(args, returncode, stdout, stderr)

            with mock.patch(
                "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                side_effect=run,
            ):
                caught: SemanticRouteAdjudicatorError | None = None
                try:
                    outcome = executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
                except SemanticRouteAdjudicatorError as error:
                    caught = error
                    outcome = None
            return outcome, caught, calls, sonnet.calls, primary_cache, backup_cache

    def test_verified_availability_terminals_use_sonnet_once_with_lineage(self) -> None:
        transport = (
            "exceeded retry limit, last status: 500 Internal Server Error, request id: req_1",
            "exceeded retry limit, last status: 502 Bad Gateway",
            "exceeded retry limit, last status: 503 Service Unavailable",
            "exceeded retry limit, last status: 504 Gateway Timeout, request id: req_4",
            (
                "unexpected status 503 Service Unavailable: <html>\r\n"
                "<h1>temporary outage</h1>\r\n</html>, "
                "url: https://example.invalid/v1/responses, request id: req_5"
            ),
            "stream disconnected before completion: error sending request",
            "stream disconnected before completion: Transport error: network error: error decoding response body",
            "stream disconnected before completion: idle timeout waiting for SSE",
        )
        # codex-cli 0.156.1 capacity displays, as the incident's stream shape
        # carries them: one error event and the failed turn, empty stderr.
        capacity = (
            "Quota exceeded. Check your plan and billing details.",
            "You’ve hit your usage limit. To get more access now, send a request to your admin or try again later.",
            "You’ve hit your usage limit for GPT-5.6-Luna. Switch to another model now, or try again at 4:49 AM.",
            "You hit your spend cap set by the owner of your workspace. Ask an owner to increase your spend cap to continue.",
        )
        cases = (
            *((message, "transport_unavailable") for message in transport),
            *((message, "capacity_unavailable") for message in capacity),
        )
        for message, reason_code in cases:
            with self.subTest(message=message[:65]):
                # A result file left by the failed primary is never read.
                outcome, error, codex_calls, sonnet_calls, primary_cache, backup_cache = (
                    self._run_chain(stdout=_failed(message), write_result=True)
                )
                self.assertIsNone(error)
                self.assertIsNotNone(outcome)
                self.assertEqual((codex_calls, sonnet_calls), (1, 1))
                self.assertEqual(outcome.actual_result_attempt, 2)
                self.assertEqual(outcome.actual_result_identity.provider_id, "sonnet-backup")
                self.assertEqual(outcome.decisions, _decisions())
                self.assertEqual(outcome.group_response_sha256, _RESPONSE_HASH)
                self.assertEqual(
                    [(item.ordinal, item.outcome, item.reason_code) for item in outcome.attempts],
                    [(1, "availability_failed", reason_code), (2, "succeeded", None)],
                )
                self.assertEqual(len(primary_cache.entries), 0)
                self.assertEqual(len(backup_cache.entries), 1)

    def test_recovery_notice_is_not_a_terminal_verdict(self) -> None:
        notice = _event({"type": "error", "message": (
            "Reconnecting... 1/5 (unexpected status 503 Service Unavailable: temporary)"
        )})
        outcome, error, calls, sonnet_calls, _, _ = self._run_chain(
            stdout=_failed(
                "exceeded retry limit, last status: 500 Internal Server Error",
                notice,
            )
        )
        self.assertIsNone(error)
        self.assertIsNotNone(outcome)
        self.assertEqual((calls, sonnet_calls), (1, 1))
        self.assertEqual(outcome.attempts[0].reason_code, "transport_unavailable")

    def test_unknown_or_conflicting_diagnostics_remain_failed_closed(self) -> None:
        cases = (
            ("400", _failed("exceeded retry limit, last status: 400 Bad Request"), "", "command_failed"),
            ("unrecognized 401", _failed("exceeded retry limit, last status: 401 Unauthorized"), "", "command_failed"),
            ("403", _failed("exceeded retry limit, last status: 403 Forbidden"), "", "command_failed"),
            ("malformed status", _failed("exceeded retry limit, last status: 503 Service Unavailable, request id: req_5, unrelated tail"), "", "command_failed"),
            ("unknown stderr", _failed("exceeded retry limit, last status: 503 Service Unavailable"), "unrecognized diagnostic\n", "command_failed"),
            ("multiline 503 with independent stderr fault", _failed(
                "unexpected status 503 Service Unavailable: <html>\r\n</html>, url: https://example.invalid/v1/responses"
            ), "unexpected status 503 Service Unavailable: server\nindependent stderr fault\n", "command_failed"),
            ("mixed availability families", _failed(
                "exceeded retry limit, last status: 503 Service Unavailable",
                _event({"type": "error", "message": "API Error: 429 Too Many Requests"}),
            ), "", "command_failed"),
            ("invalid schema", _failed("exceeded retry limit, last status: 503 Service Unavailable"), "invalid_json_schema\n", "invalid_output_schema"),
            ("tool call", "\n".join((
                _event({"type": "thread.started", "thread_id": "synthetic-thread"}),
                _event({"type": "turn.started"}),
                _event({"type": "item.started", "item": {"type": "mcp_tool_call", "tool": "forbidden"}}),
                _event({"type": "turn.failed", "error": {"message": "exceeded retry limit, last status: 503 Service Unavailable"}}),
            )), "", "forbidden_tool_call"),
            ("quota with unknown stderr", _failed(QUOTA), "unrecognized diagnostic\n", "command_failed"),
            ("quota with transport sibling", _failed(
                QUOTA, _event({"type": "error", "message": "stream disconnected before completion: error sending request"}),
            ), "", "command_failed"),
            ("quota with schema error", _failed(QUOTA), "invalid_json_schema\n", "invalid_output_schema"),
            ("quota with tool router line", _failed(QUOTA), (
                "2026-10-07T04:49:06.000000Z ERROR codex_core::tools::router: error=unsupported call: exec_command\n"
            ), "forbidden_tool_call"),
            ("usage limit beside a tool item", "\n".join((
                _event({"type": "thread.started", "thread_id": "synthetic-thread"}),
                _event({"type": "turn.started"}),
                _event({"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "ls"}}),
                _event({"type": "turn.failed", "error": {"message": "You’ve hit your usage limit. Try again later."}}),
            )), "", "forbidden_tool_call"),
            ("plan entitlement", _failed(
                "To use Codex with your ChatGPT plan, upgrade to Plus: https://chatgpt.com/explore/plus."
            ), "", "command_failed"),
            ("free-text rate limit", _failed(
                "rate limit exceeded: Rate limit reached for gpt-5.6-luna. Please try again in 1.2s."
            ), "", "command_failed"),
            ("non-transport stream detail", _failed(
                "stream disconnected before completion: Incomplete response returned, reason: max_output_tokens"
            ), "", "command_failed"),
        )
        for label, stdout, stderr, reason in cases:
            with self.subTest(label):
                outcome, error, codex_calls, sonnet_calls, primary_cache, backup_cache = (
                    self._run_chain(stdout=stdout, stderr=stderr)
                )
                self.assertIsNone(outcome)
                self.assertIsNotNone(error)
                self.assertEqual(error.reason_code, reason)
                self.assertFalse(error.retryable)
                self.assertEqual((codex_calls, sonnet_calls), (1, 0))
                self.assertEqual([(item.outcome, item.reason_code) for item in error.attempts],
                                 [("failed_closed", reason)])
                self.assertFalse(primary_cache.entries)
                self.assertFalse(backup_cache.entries)

    def test_successful_codex_turn_keeps_primary_identity(self) -> None:
        outcome, error, codex_calls, sonnet_calls, primary_cache, backup_cache = (
            self._run_chain(stdout=_completed(), returncode=0)
        )
        self.assertIsNone(error)
        self.assertIsNotNone(outcome)
        self.assertEqual((codex_calls, sonnet_calls), (1, 0))
        self.assertEqual(outcome.actual_result_attempt, 1)
        self.assertEqual(outcome.actual_result_identity.provider_id, "luna-primary")
        self.assertEqual([item.outcome for item in outcome.attempts], ["succeeded"])
        self.assertEqual(len(primary_cache.entries), 1)
        self.assertFalse(backup_cache.entries)

    def test_failure_warning_is_bounded_redacted_and_protocol_error_still_closed(self) -> None:
        server_body = "private-server-body-secret"
        url = "https://private.example.invalid/token"
        transient = _failed(
            "unexpected status 503 Service Unavailable: <html>" + server_body
            + "</html>, url: " + url
        )
        with self.assertLogs("disclosure_anchor.adapters.semantics.codex_cli", level="WARNING") as logs:
            outcome, error, codex_calls, sonnet_calls, _, _ = self._run_chain(stdout=transient)
        self.assertIsNone(error)
        self.assertIsNotNone(outcome)
        self.assertEqual((codex_calls, sonnet_calls), (1, 1))
        self.assertEqual(len(logs.output), 1)
        self.assertLess(len(logs.output[0]), 1500)
        self.assertNotIn(server_body, logs.output[0])
        self.assertNotIn(url, logs.output[0])
        self.assertNotIn("prompt", logs.output[0].lower())
        record = json.loads(logs.output[0].split("semantic_provider_failure ", 1)[1])
        self.assertEqual(record["event"], "semantic_provider_failure.v1")
        self.assertEqual(record["provider"], "codex_cli")
        self.assertEqual(record["returncode"], 1)
        self.assertEqual(record["reason_code"], "transport_unavailable")
        self.assertEqual(record["terminal_http_statuses"], [503])
        self.assertEqual(record["terminal_message_count"], 2)
        self.assertEqual(record["group_hash"], _GROUP_HASH)
        self.assertIsInstance(record["observed_at_unix_ns"], int)
        self.assertGreater(record["observed_at_unix_ns"], 0)
        self.assertEqual(record["stdout_bytes"], len(transient.encode("utf-8")))
        self.assertEqual(record["stdout_sha256"], hashlib.sha256(transient.encode("utf-8")).hexdigest())
        self.assertEqual(record["stderr_bytes"], 0)
        self.assertEqual(record["stderr_sha256"], hashlib.sha256(b"").hexdigest())

        malformed = "\n".join((
            _event({"type": "thread.started", "thread_id": "synthetic-thread"}),
            _event({"type": "turn.started"}),
            _event({"type": "item.started", "item": {"type": "mcp_tool_call", "tool": "forbidden"}}),
        ))
        with self.assertLogs("disclosure_anchor.adapters.semantics.codex_cli", level="WARNING") as logs:
            outcome, error, codex_calls, sonnet_calls, _, _ = self._run_chain(stdout=malformed)
        self.assertIsNone(outcome)
        self.assertIsNotNone(error)
        self.assertEqual(error.reason_code, "forbidden_tool_call")
        self.assertEqual((codex_calls, sonnet_calls), (1, 0))
        self.assertEqual(len(logs.output), 1)
        record = json.loads(logs.output[0].split("semantic_provider_failure ", 1)[1])
        self.assertEqual(record["reason_code"], "forbidden_tool_call")
        self.assertEqual(record["terminal_http_statuses"], [])
        self.assertEqual(record["group_hash"], _GROUP_HASH)
        self.assertEqual(record["stdout_bytes"], len(malformed.encode("utf-8")))
        self.assertEqual(record["stdout_sha256"], hashlib.sha256(malformed.encode("utf-8")).hexdigest())
