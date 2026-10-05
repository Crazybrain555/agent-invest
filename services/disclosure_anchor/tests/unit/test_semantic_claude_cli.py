from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.adapters.semantics import codex_cli
from disclosure_anchor.adapters.semantics.claude_cli import (
    ClaudeCliSemanticAdjudicator,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    SemanticDocumentContext,
    SemanticRouteCandidate,
    SemanticRouteDefinition,
    SemanticRouteSource,
    SemanticRouteTaxonomy,
    SemanticRouteUnitInput,
)
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticAdjudicationBatch,
    SemanticRouteAdjudicatorError,
)
from disclosure_anchor.application.ports.staged_execution import semantic_group_scope
from tests.unit.test_semantic_codex_cli import (
    ADVERSARIAL_ENVIRONMENT,
    LEAK_MARKERS,
    OPERATOR_NETWORK,
    OPERATOR_PROCESS_ENVIRONMENT,
    OPERATOR_RUNTIME,
    PROVIDER_ENVIRONMENT_CASES,
)


_LOGGER_NAME = "disclosure_anchor.adapters.semantics.claude_cli"
# Stands in for the Claude CLI: records what the adapter's subprocess received
# and answers with a fixed success envelope.
_FAKE_CLAUDE = """#!{python}
import hashlib, json, os, sys
prompt = sys.stdin.read()
with open({record!r}, "w", encoding="utf-8") as handle:
    json.dump(
        {{
            "argv": sys.argv[1:],
            "env": dict(os.environ),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }},
        handle,
    )
sys.stdout.write({stdout!r})
"""
CLAUDE_2_1_280_API_ERROR_ENVELOPE = (
    '{"duration_api_ms":0,"stop_reason":"stop_sequence","session_id":"8d6dd2dd-1098-421b-9408'
    '-5989bf75faaf","total_cost_usd":0,"usage":{"output_tokens_details":{"thinking_tokens":0}'
    ',"input_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_to'
    'kens":0,"server_tool_use":{"web_search_requests":0,"web_fetch_requests":0},"service_tier'
    '":"standard","cache_creation":{"ephemeral_1h_input_tokens":0,"ephemeral_5m_input_tokens"'
    ':0},"inference_geo":"","iterations":[],"speed":"standard"},"modelUsage":{},"permission_d'
    'enials":[],"terminal_reason":"api_error","fast_mode_state":"off","fast_mode_disabled_rea'
    'son":"sdk_opt_in_required","subagent_stats":{"spawned":0,"requested":{"background":0,"fo'
    'reground":0,"unset":0},"started_in_background":0,"max_depth":0,"spawned_by_subagents":0,'
    '"completed":0,"failed":0,"killed":{"parent":0,"user":0,"system":0},"refused":{"depth_lim'
    'it":0,"concurrency_limit":0,"budget":0},"by_type":{}},"is_error":true,"num_turns":1,"sub'
    'type":"success","api_error_status":400,"result":"API Error: 400 opus capture only","type'
    '":"result","duration_ms":17,"uuid":"79fb4cd1-3c45-44b4-89ba-411c21c7f7b5","queued_turn_c'
    'ount":0,"result_index":0}'
)


def _batch() -> SemanticAdjudicationBatch:
    return SemanticAdjudicationBatch(
        document=SemanticDocumentContext(
            title="某公司业绩预告",
            filing_type="performance_forecast",
        ),
        taxonomy=SemanticRouteTaxonomy(
            version="semantic-test.v1",
            definitions=(
                SemanticRouteDefinition(
                    key="forecast_summary",
                    description="业绩预告结论",
                    labels=("业绩预告",),
                ),
            ),
        ),
        units=(
            SemanticRouteUnitInput(
                unit_index=0,
                input_hash="sha256:" + "a" * 64,
                sources=(
                    SemanticRouteSource(
                        source_id="u0:title",
                        kind="unit_title",
                        text="业绩预告",
                    ),
                ),
                candidates=(
                    SemanticRouteCandidate(
                        key="forecast_summary",
                        source_ids=("u0:title",),
                        evidence_kinds=("source_heading_exact",),
                    ),
                ),
            ),
        ),
    )


def _stdout(
    *,
    canonical_model: str = "claude-sonnet-5",
    permission_denials: list[object] | None = None,
    helper_model: bool = False,
) -> str:
    model_usage = {
        "claude-sonnet-5": {"canonicalModel": canonical_model},
    }
    if helper_model:
        model_usage["claude-haiku-4-5"] = {
            "canonicalModel": "claude-haiku-4-5"
        }
    return json.dumps(
        {
            "is_error": False,
            "modelUsage": model_usage,
            "permission_denials": permission_denials or [],
            "structured_output": {
                "decisions": {
                    "0": {"verdicts": {"forecast_summary": True}},
                },
            },
        }
    )


def _error_usage() -> dict[str, object]:
    return {
        "output_tokens_details": {"thinking_tokens": 0},
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": "standard",
        "cache_creation": {
            "ephemeral_1h_input_tokens": 0,
            "ephemeral_5m_input_tokens": 0,
        },
        "inference_geo": "",
        "iterations": [],
        "speed": "standard",
    }


def _subagent_stats(**changes: object) -> dict[str, object]:
    stats: dict[str, object] = {
        "spawned": 0,
        "requested": {"background": 0, "foreground": 0, "unset": 0},
        "started_in_background": 0,
        "max_depth": 0,
        "spawned_by_subagents": 0,
        "completed": 0,
        "failed": 0,
        "killed": {"parent": 0, "user": 0, "system": 0},
        "refused": {"depth_limit": 0, "concurrency_limit": 0, "budget": 0},
        "by_type": {},
    }
    stats.update(changes)
    return stats


def _live_expired_login_envelope() -> dict[str, object]:
    # Claude Code 2.1.274 `-p --output-format json` envelope captured with the
    # adapter's exact argv while the CLI's stored login was gone (empty stderr).
    return {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "duration_ms": 16,
        "duration_api_ms": 0,
        "num_turns": 1,
        "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
        "stop_reason": "stop_sequence",
        "terminal_reason": "api_error",
        "session_id": "00000000-0000-4000-8000-000000000001",
        "total_cost_usd": 0,
        "usage": _error_usage(),
        "modelUsage": {},
        "permission_denials": [],
        "fast_mode_state": "off",
        "fast_mode_disabled_reason": "sdk_opt_in_required",
        "queued_turn_count": 0,
        "result_index": 0,
        "subagent_stats": _subagent_stats(),
        "api_error_status": None,
        "uuid": "00000000-0000-4000-8000-000000000002",
    }


def _model_usage(
    *,
    canonical_model: str,
    provider: str = "firstParty",
    helper_canonical_model: str | None = None,
    web_search_requests: int = 0,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
        "webSearchRequests": web_search_requests,
        "costUSD": 0.0,
        "contextWindow": 1_000_000,
        "maxOutputTokens": 64_000,
        "canonicalModel": canonical_model,
        "provider": provider,
    }
    usage: dict[str, object] = {"model": entry}
    if helper_canonical_model is not None:
        usage["helper"] = {
            **entry,
            "canonicalModel": helper_canonical_model,
        }
    return usage


def _request_not_allowed_envelope() -> dict[str, object]:
    # The 2.1.280 API-error envelope carrying the CLI's HTTP 403 result: a
    # typed status outside the availability set.
    return {
        **json.loads(CLAUDE_2_1_280_API_ERROR_ENVELOPE),
        "api_error_status": 403,
        "result": "Failed to authenticate. API Error: 403 Request not allowed",
    }


def _failure_records(output: list[str]) -> list[dict[str, object]]:
    return [json.loads(line.split("semantic_provider_failure ", 1)[1]) for line in output]


class ClaudeCliSemanticAdjudicatorTests(unittest.TestCase):
    def test_uses_closed_noninteractive_cli_and_attests_canonical_model(self) -> None:
        # A real child stands in for the CLI, so argv, stdin and environment are
        # observed exactly as the adapter's subprocess receives them.
        observed: list[dict[str, object]] = []
        prompt_sha256 = hashlib.sha256(codex_cli._prompt(_batch()).encode("utf-8")).hexdigest()
        for label, network in PROVIDER_ENVIRONMENT_CASES:
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                record = Path(tmp) / "observed.json"
                executable = Path(tmp) / "claude"
                executable.write_text(
                    _FAKE_CLAUDE.format(
                        python=sys.executable, record=str(record), stdout=_stdout()
                    ),
                    encoding="utf-8",
                )
                executable.chmod(0o700)
                adapter = ClaudeCliSemanticAdjudicator(
                    executable=executable,
                    model="claude-sonnet-5",
                    reasoning_effort="low",
                )
                with mock.patch.dict(
                    os.environ,
                    {**OPERATOR_RUNTIME, **network, **ADVERSARIAL_ENVIRONMENT},
                    clear=True,
                ):
                    result = adapter.adjudicate_with_result(_batch())
                child = json.loads(record.read_text(encoding="utf-8"))
                self.assertEqual(result.decisions[0].routes[0].key, "forecast_summary")
                self.assertTrue(result.response_sha256.startswith("sha256:"))
                self.assertEqual(child["prompt_sha256"], prompt_sha256)
                # The operator's proxy route arrives verbatim, no worker secret does.
                self.assertEqual(child["env"], {**OPERATOR_PROCESS_ENVIRONMENT, **network})
                observed.append(child)

        self.assertEqual(len(observed), len(PROVIDER_ENVIRONMENT_CASES))
        self.assertEqual(adapter.provider_identity.canonical_model, "claude-sonnet-5")
        self.assertEqual(adapter.provider_identity.adapter_version, "claude_cli.v2")
        # The environment never changes the closed, tool-free argv.
        args = observed[0]["argv"]
        assert isinstance(args, list)
        self.assertTrue(all(child["argv"] == args for child in observed))
        self.assertFalse(
            [item for item in args if any(marker in item for marker in LEAK_MARKERS)]
        )
        for flag in (
            "-p",
            "--json-schema",
            "--no-session-persistence",
            "--safe-mode",
            "--disable-slash-commands",
            "--no-chrome",
            "--permission-mode",
            "--strict-mcp-config",
            "--tools",
        ):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--model") + 1], "claude-sonnet-5")
        schema = json.loads(args[args.index("--json-schema") + 1])
        self.assertNotIn("$schema", schema)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(
            args[args.index("--mcp-config") + 1],
            '{"mcpServers":{}}',
        )

    def test_constructor_rejects_alias_instead_of_silently_resolving_it(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical claude-sonnet-5"):
            ClaudeCliSemanticAdjudicator(executable=Path("claude"), model="sonnet")

    def test_allows_cli_internal_helper_only_when_requested_sonnet_is_attested(self) -> None:
        adapter = ClaudeCliSemanticAdjudicator(executable=Path("claude"))
        with mock.patch(
            "disclosure_anchor.adapters.semantics.codex_cli._run_process",
            return_value=subprocess.CompletedProcess(
                ["claude"], 0, _stdout(helper_model=True), ""
            ),
        ):
            result = adapter.adjudicate_with_result(_batch())

        self.assertEqual(result.decisions[0].routes[0].key, "forecast_summary")

    def test_permission_attempt_and_model_identity_drift_fail_closed(self) -> None:
        adapter = ClaudeCliSemanticAdjudicator(executable=Path("claude"))
        model_tool_receipt = json.loads(_stdout())
        model_tool_receipt["modelUsage"]["claude-sonnet-5"][
            "webSearchRequests"
        ] = 1
        server_tool_receipt = json.loads(_stdout())
        server_tool_receipt["usage"] = {
            "server_tool_use": {"web_search_requests": 1}
        }
        unexpected_helper = json.loads(_stdout(helper_model=True))
        unexpected_helper["modelUsage"]["claude-haiku-4-5"][
            "canonicalModel"
        ] = "claude-opus-4-1"
        cases = (
            (_stdout(permission_denials=[{"tool": "Read"}]), "forbidden_tool_call"),
            (_stdout(canonical_model="claude-sonnet-4"), "model_identity_mismatch"),
            (json.dumps(unexpected_helper), "model_identity_mismatch"),
            (json.dumps(model_tool_receipt), "forbidden_tool_call"),
            (json.dumps(server_tool_receipt), "forbidden_tool_call"),
            (
                json.dumps({**json.loads(_stdout()), "subagent_stats": _subagent_stats(spawned=1)}),
                "forbidden_tool_call",
            ),
            (
                json.dumps({**json.loads(_stdout()), "subagent_stats": "spawned 1 agent"}),
                "forbidden_tool_call",
            ),
        )
        with mock.patch(
            "disclosure_anchor.adapters.semantics.codex_cli._run_process",
            return_value=subprocess.CompletedProcess(
                ["claude"], 0,
                json.dumps({**json.loads(_stdout()), "subagent_stats": _subagent_stats()}), "",
            ),
        ):
            self.assertEqual(
                adapter.adjudicate_with_result(_batch()).decisions[0].routes[0].key,
                "forecast_summary",
            )
        for stdout, reason_code in cases:
            with (
                self.subTest(reason_code=reason_code),
                mock.patch(
                    "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                    return_value=subprocess.CompletedProcess(["claude"], 0, stdout, ""),
                ),
                self.assertRaises(SemanticRouteAdjudicatorError) as caught,
            ):
                adapter.adjudicate_with_result(_batch())
            self.assertEqual(caught.exception.reason_code, reason_code)
            self.assertFalse(caught.exception.retryable)

    def test_success_envelope_requires_boolean_error_and_list_denial_fields(self) -> None:
        adapter = ClaudeCliSemanticAdjudicator(executable=Path("claude"))
        for field, value in (
            ("is_error", "false"),
            ("permission_denials", {"tool": "Read"}),
        ):
            payload = json.loads(_stdout())
            payload[field] = value
            with (
                self.subTest(field=field),
                mock.patch(
                    "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                    return_value=subprocess.CompletedProcess(
                        ["claude"], 0, json.dumps(payload), ""
                    ),
                ),
                self.assertRaises(SemanticRouteAdjudicatorError) as caught,
            ):
                adapter.adjudicate_with_result(_batch())
            self.assertEqual(caught.exception.reason_code, "invalid_runtime_protocol")
            self.assertFalse(caught.exception.retryable)

        duplicate_success = _stdout().replace(
            '"is_error": false',
            '"is_error": true, "is_error": false',
            1,
        )
        with (
            mock.patch(
                "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                return_value=subprocess.CompletedProcess(
                    ["claude"], 0, duplicate_success, ""
                ),
            ),
            self.assertRaises(SemanticRouteAdjudicatorError) as caught,
        ):
            adapter.adjudicate_with_result(_batch())
        self.assertEqual(caught.exception.reason_code, "invalid_runtime_protocol")
        self.assertFalse(caught.exception.retryable)

        # An exit-0 error envelope fails closed like a nonzero exit and leaves
        # one record. It never passed the closed error validation, so even its
        # well-formed status is not reported.
        exit_zero_error = json.dumps(_request_not_allowed_envelope())
        with (
            mock.patch(
                "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                return_value=subprocess.CompletedProcess(
                    ["claude"], 0, exit_zero_error, ""
                ),
            ),
            self.assertLogs(_LOGGER_NAME, level="WARNING") as logs,
            self.assertRaises(SemanticRouteAdjudicatorError) as caught,
        ):
            adapter.adjudicate_with_result(_batch())
        self.assertEqual(caught.exception.reason_code, "runtime_event_error")
        self.assertFalse(caught.exception.retryable)
        (record,) = _failure_records(logs.output)
        self.assertEqual(
            {key: record[key] for key in (
                "provider", "returncode", "reason_code", "terminal_http_statuses",
                "terminal_message_count", "stdout_bytes", "stdout_sha256",
            )},
            {
                "provider": "claude_cli",
                "returncode": 0,
                "reason_code": "runtime_event_error",
                "terminal_http_statuses": [],
                "terminal_message_count": 0,
                "stdout_bytes": len(exit_zero_error.encode("utf-8")),
                "stdout_sha256": hashlib.sha256(exit_zero_error.encode("utf-8")).hexdigest(),
            },
        )

    def test_only_known_availability_exit_is_failover_eligible(self) -> None:
        adapter = ClaudeCliSemanticAdjudicator(executable=Path("claude"))
        cases = (
            ("Not logged in · Please run /login", "not_authenticated", True),
            (
                "API Error: 401 OAuth access token has expired. Re-authenticate",
                "not_authenticated",
                True,
            ),
            ("API Error: 429 Too Many Requests", "capacity_unavailable", True),
            ("quota exceeded", "capacity_unavailable", True),
            ("API Error: 429 Overloaded", "capacity_unavailable", True),
            ("API Error: 529 Overloaded", "capacity_unavailable", True),
            ("repeated 529 overloaded errors", "capacity_unavailable", True),
            (
                "server is temporarily limiting requests (not your usage limit)",
                "capacity_unavailable",
                True,
            ),
            ("overloaded_error", "capacity_unavailable", True),
            ("API Error: overloaded", "capacity_unavailable", True),
            ("Anthropic profile login expired.", "not_authenticated", True),
            # The four `authentication_failed` sentences Claude Code 2.1.274 emits.
            (
                "Failed to authenticate: OAuth session expired and could not be refreshed",
                "not_authenticated",
                True,
            ),
            ("Login expired · Please run /login", "not_authenticated", True),
            (
                "Authentication error · This may be a temporary network issue, please try again",
                "not_authenticated",
                True,
            ),
            ("Invalid API key · Fix external API key", "not_authenticated", True),
            ("Failed to authenticate: invalid API key", "command_failed", False),
            ("unexpected internal failure", "command_failed", False),
            (
                "fatal protocol error while formatting login diagnostics",
                "command_failed",
                False,
            ),
            (
                "authentication parser crashed on malformed response",
                "command_failed",
                False,
            ),
            (
                "Permission denied: login capability is forbidden",
                "command_failed",
                False,
            ),
            ("panic at line 429 of stream handler", "command_failed", False),
            ("invalid schema property quota", "command_failed", False),
            ("rate limit parser crashed on malformed response", "command_failed", False),
            (
                "quota exceeded while protocol parser crashed on malformed response",
                "command_failed",
                False,
            ),
            (
                "API Error: 429 Too Many Requests\nfatal protocol parser crashed",
                "command_failed",
                False,
            ),
            ("Please run /login to continue parsing", "command_failed", False),
            ("Invalid API key; forbidden tool call", "command_failed", False),
            ("overloaded_error: forbidden tool call", "command_failed", False),
            (
                "Anthropic profile login expired; forbidden tool call",
                "command_failed",
                False,
            ),
        )
        for stderr, reason_code, retryable in cases:
            with (
                self.subTest(reason_code=reason_code),
                mock.patch(
                    "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                    return_value=subprocess.CompletedProcess(
                        ["claude"], 1, "", stderr
                    ),
                ),
                self.assertLogs(_LOGGER_NAME, level="WARNING") as logs,
                self.assertRaises(SemanticRouteAdjudicatorError) as caught,
            ):
                adapter.adjudicate_with_result(_batch())
            self.assertEqual(caught.exception.reason_code, reason_code)
            self.assertEqual(caught.exception.retryable, retryable)
            # stderr is free text: it never supplies a status and is not copied.
            (record,) = _failure_records(logs.output)
            self.assertEqual(
                {key: record[key] for key in (
                    "provider", "returncode", "reason_code", "terminal_http_statuses",
                    "terminal_message_count", "stdout_bytes", "stderr_bytes",
                )},
                {
                    "provider": "claude_cli",
                    "returncode": 1,
                    "reason_code": reason_code,
                    "terminal_http_statuses": [],
                    "terminal_message_count": 0,
                    "stdout_bytes": 0,
                    "stderr_bytes": len(stderr.encode("utf-8")),
                },
            )
            self.assertNotIn(stderr, logs.output[0])

    def test_structured_nonzero_envelope_uses_typed_status_and_security_first(
        self,
    ) -> None:
        adapter = ClaudeCliSemanticAdjudicator(executable=Path("claude"))
        cases = (
            (
                {
                    "is_error": True,
                    "permission_denials": [{"tool": "login-429"}],
                    "api_error_status": 429,
                    "result": "API Error: 429 Too Many Requests",
                },
                "",
                "forbidden_tool_call",
                False,
            ),
            (
                {
                    "is_error": True,
                    "permission_denials": [],
                    "api_error_status": 401,
                    "result": (
                        "API Error: 401 OAuth access token has expired. "
                        "Re-authenticate"
                    ),
                },
                "",
                "not_authenticated",
                True,
            ),
            (
                {
                    "is_error": True,
                    "permission_denials": [],
                    "api_error_status": 429,
                    "result": "API Error: 429 Too Many Requests",
                },
                "",
                "capacity_unavailable",
                True,
            ),
            (
                {
                    "is_error": True,
                    "permission_denials": [],
                    "api_error_status": None,
                    "result": "authentication parser crashed on malformed response",
                },
                "",
                "command_failed",
                False,
            ),
            (
                {
                    "is_error": True,
                    "permission_denials": [],
                    "api_error_status": 429,
                    "result": (
                        "API Error: 429 Too Many Requests\n"
                        "fatal protocol parser crashed"
                    ),
                },
                "",
                "command_failed",
                False,
            ),
            (
                {
                    "is_error": True,
                    "permission_denials": [],
                    "api_error_status": 429,
                    "result": "unrecognized provider wording",
                },
                "",
                "command_failed",
                False,
            ),
            (
                {
                    "is_error": True,
                    "permission_denials": [],
                    "api_error_status": 500,
                    "result": "API Error: 429 Too Many Requests",
                },
                "",
                "command_failed",
                False,
            ),
            (
                {
                    "is_error": True,
                    "permission_denials": [],
                    "api_error_status": 429,
                    "result": "Not logged in · Please run /login",
                },
                "",
                "command_failed",
                False,
            ),
            (
                {
                    "is_error": True,
                    "permission_denials": [],
                    "api_error_status": 429,
                    "result": "not a valid JSON schema",
                },
                "",
                "invalid_output_schema",
                False,
            ),
            (
                {
                    "is_error": True,
                    "permission_denials": [],
                    "api_error_status": 429,
                    "result": "API Error: 429 Too Many Requests",
                },
                "not a valid JSON schema",
                "invalid_output_schema",
                False,
            ),
            (
                {
                    "is_error": True,
                    "permission_denials": [],
                    "api_error_status": 429,
                    "result": None,
                },
                "security policy rejected a forbidden tool call",
                "command_failed",
                False,
            ),
            (
                {
                    "is_error": True,
                    "permission_denials": [],
                    "api_error_status": 529,
                    "result": None,
                    "duration_api_ms": 10,
                    "modelUsage": {},
                    "session_id": "session-1",
                    "stop_reason": None,
                    "total_cost_usd": 0.0,
                    "usage": _error_usage(),
                },
                "",
                "capacity_unavailable",
                True,
            ),
            (
                {
                    "is_error": True,
                    "permission_denials": [],
                    "api_error_status": 429,
                    "result": "API Error: 429 Too Many Requests",
                },
                "Rate limit exceeded.",
                "capacity_unavailable",
                True,
            ),
            (_live_expired_login_envelope(), "", "not_authenticated", True),
            (
                {**_live_expired_login_envelope(), "api_error_status": 401},
                "",
                "not_authenticated",
                True,
            ),
            (
                {
                    **_live_expired_login_envelope(),
                    "subagent_stats": _subagent_stats(spawned=1),
                },
                "",
                "forbidden_tool_call",
                False,
            ),
            (
                {
                    **_live_expired_login_envelope(),
                    "subagent_stats": _subagent_stats(
                        requested={"background": 1, "foreground": 0, "unset": 0}
                    ),
                },
                "",
                "forbidden_tool_call",
                False,
            ),
            # Claude Code 2.1.280's complete API-error envelope (captured offline
            # against a local endpoint): a plain 400 stays a failed command...
            (json.loads(CLAUDE_2_1_280_API_ERROR_ENVELOPE), "", "command_failed", False),
            # ...and the same envelope shape still carries capacity to failover.
            (
                {
                    **json.loads(CLAUDE_2_1_280_API_ERROR_ENVELOPE),
                    "api_error_status": 429,
                    "result": "API Error: 429 Too Many Requests",
                },
                "",
                "capacity_unavailable",
                True,
            ),
            # A typed 403 is not availability: it never fails over.
            (_request_not_allowed_envelope(), "", "command_failed", False),
            # An integer outside the HTTP status range is no status to report.
            (
                {**_request_not_allowed_envelope(), "api_error_status": 10**30},
                "",
                "command_failed",
                False,
            ),
        )
        for payload, stderr, reason_code, retryable in cases:
            stdout = json.dumps(payload)
            with (
                self.subTest(reason_code=reason_code),
                mock.patch(
                    "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                    return_value=subprocess.CompletedProcess(
                        ["claude"], 1, stdout, stderr
                    ),
                ),
                self.assertLogs(_LOGGER_NAME, level="WARNING") as logs,
                self.assertRaises(SemanticRouteAdjudicatorError) as caught,
            ):
                adapter.adjudicate_with_result(_batch())
            self.assertEqual(caught.exception.reason_code, reason_code)
            self.assertEqual(caught.exception.retryable, retryable)
            # A status is reported only from an envelope that passed the closed
            # validation (the security-first cases stop inside it), and only
            # within the HTTP status range.
            validated = reason_code != "forbidden_tool_call"
            status = payload.get("api_error_status") if validated else None
            (record,) = _failure_records(logs.output)
            self.assertEqual(
                {key: record[key] for key in (
                    "provider", "returncode", "reason_code", "terminal_http_statuses",
                    "stdout_bytes", "stdout_sha256",
                )},
                {
                    "provider": "claude_cli",
                    "returncode": 1,
                    "reason_code": reason_code,
                    "terminal_http_statuses": (
                        [status] if status in range(100, 600) else []
                    ),
                    "stdout_bytes": len(stdout.encode("utf-8")),
                    "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
                },
            )

        # The record copies no provider text, proxy credential or worker secret.
        leaky = {
            **_request_not_allowed_envelope(),
            "result": (
                "API Error: 403 via " + OPERATOR_NETWORK["HTTPS_PROXY"]
                + " " + ADVERSARIAL_ENVIRONMENT["ANTHROPIC_API_KEY"]
            ),
        }
        group_hash = "sha256:" + "7" * 64
        with (
            mock.patch.dict(
                os.environ,
                {**OPERATOR_RUNTIME, **OPERATOR_NETWORK, **ADVERSARIAL_ENVIRONMENT},
                clear=True,
            ),
            mock.patch(
                "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                return_value=subprocess.CompletedProcess(
                    ["claude"], 1, json.dumps(leaky),
                    "proxy CONNECT failed: " + OPERATOR_NETWORK["HTTP_PROXY"] + "\n",
                ),
            ),
            semantic_group_scope(group_hash),
            self.assertLogs(_LOGGER_NAME, level="WARNING") as logs,
            self.assertRaises(SemanticRouteAdjudicatorError) as caught,
        ):
            adapter.adjudicate_with_result(_batch())
        self.assertEqual(caught.exception.reason_code, "command_failed")
        self.assertFalse(caught.exception.retryable)
        (record,) = _failure_records(logs.output)
        self.assertEqual(
            (record["group_hash"], record["terminal_http_statuses"],
             record["terminal_message_count"]),
            (group_hash, [403], 1),
        )
        self.assertLess(len(logs.output[0]), 1500)
        for marker in (*LEAK_MARKERS, "API Error", "CONNECT"):
            self.assertNotIn(marker, logs.output[0])
            self.assertNotIn(marker, str(caught.exception))

    def test_structured_nonzero_envelope_rejects_malformed_control_fields(self) -> None:
        adapter = ClaudeCliSemanticAdjudicator(executable=Path("claude"))
        cases = (
            {"permission_denials": [], "result": "Not logged in"},
            {
                "is_error": True,
                "permission_denials": {"tool": "Read"},
                "result": "Not logged in",
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": "429",
                "result": "API Error: 429 Too Many Requests",
            },
            # A bool is not an int status, and neither is a float.
            {**_request_not_allowed_envelope(), "api_error_status": True},
            {**_request_not_allowed_envelope(), "api_error_status": 403.0},
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": {"not": "a string"},
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "security_error": "forbidden tool call",
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "invalid_json_schema": True,
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "session_id": 123,
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "subtype": "invalid_json_schema",
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "terminal_reason": "forbidden_tool_call",
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "usage": {"security_error": "forbidden tool call"},
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "modelUsage": {"security_error": "forbidden tool call"},
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "duration_ms": -1,
            },
            {**_live_expired_login_envelope(), "queued_turn_count": -1},
            {**_live_expired_login_envelope(), "result_index": True},
            {**_live_expired_login_envelope(), "subagent_stats": {}},
            {
                **_live_expired_login_envelope(),
                "subagent_stats": _subagent_stats(security_error="forbidden tool call"),
            },
            {
                **_live_expired_login_envelope(),
                "subagent_stats": _subagent_stats(by_type={"Explore": 0}),
            },
            {**_live_expired_login_envelope(), "queued_turns": 0},
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "total_cost_usd": -1,
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "modelUsage": _model_usage(canonical_model="claude-haiku-4-5"),
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "modelUsage": _model_usage(
                    canonical_model="claude-sonnet-5",
                    provider="openai",
                ),
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "modelUsage": _model_usage(
                    canonical_model="claude-sonnet-5",
                    helper_canonical_model="claude-opus-4-1",
                ),
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "modelUsage": _model_usage(
                    canonical_model="claude-sonnet-5",
                    helper_canonical_model="gpt-5.6",
                ),
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "usage": {
                    **_error_usage(),
                    "iterations": [
                        {
                            "cache_creation": {
                                "ephemeral_1h_input_tokens": 0,
                                "ephemeral_5m_input_tokens": 0,
                            },
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "type": "tool_use",
                        }
                    ],
                },
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "usage": {**_error_usage(), "service_tier": "fatal_protocol"},
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "usage": {**_error_usage(), "inference_geo": "invalid_json_schema"},
            },
        )
        capability_cases = (
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "usage": {
                    **_error_usage(),
                    "server_tool_use": {
                        "web_search_requests": 1,
                        "web_fetch_requests": 0,
                    },
                },
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "usage": {
                    **_error_usage(),
                    "server_tool_use": {
                        "web_search_requests": 0,
                        "web_fetch_requests": 1,
                    },
                },
            },
            {
                "is_error": True,
                "permission_denials": [],
                "api_error_status": 429,
                "result": "API Error: 429 Too Many Requests",
                "modelUsage": _model_usage(
                    canonical_model="claude-sonnet-5",
                    web_search_requests=1,
                ),
            },
        )
        for payloads, expected_reason in (
            (cases, "invalid_runtime_protocol"),
            (capability_cases, "forbidden_tool_call"),
        ):
            for payload in payloads:
                with (
                    self.subTest(payload=payload),
                    mock.patch(
                        "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                        return_value=subprocess.CompletedProcess(
                            ["claude"], 1, json.dumps(payload), ""
                        ),
                    ),
                    self.assertLogs(_LOGGER_NAME, level="WARNING") as logs,
                    self.assertRaises(SemanticRouteAdjudicatorError) as caught,
                ):
                    adapter.adjudicate_with_result(_batch())
                self.assertEqual(caught.exception.reason_code, expected_reason)
                self.assertFalse(caught.exception.retryable)
                # Nothing from a rejected envelope is trusted, not even its status.
                (record,) = _failure_records(logs.output)
                self.assertEqual(
                    (record["reason_code"], record["terminal_http_statuses"],
                     record["terminal_message_count"]),
                    (expected_reason, [], 0),
                )

        duplicate_or_nonfinite = (
            (
                '{"is_error":true,"permission_denials":[{"tool":"Read"}],'
                '"permission_denials":[],"api_error_status":429,'
                '"result":"API Error: 429 Too Many Requests"}'
            ),
            (
                '{"is_error":true,"permission_denials":[],'
                '"api_error_status":500,"api_error_status":429,'
                '"result":"API Error: 429 Too Many Requests"}'
            ),
            (
                '{"is_error":true,"permission_denials":[],'
                '"api_error_status":429,"result":"forbidden tool call",'
                '"result":"API Error: 429 Too Many Requests"}'
            ),
            (
                '{"is_error":true,"permission_denials":[],'
                '"api_error_status":429,"result":null,"total_cost_usd":NaN}'
            ),
            (
                '{"is_error":true,"permission_denials":[],'
                '"api_error_status":429,"result":null,"total_cost_usd":1e999}'
            ),
        )
        for stdout in duplicate_or_nonfinite:
            with (
                self.subTest(stdout=stdout),
                mock.patch(
                    "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                    return_value=subprocess.CompletedProcess(
                        ["claude"], 1, stdout, ""
                    ),
                ),
                self.assertLogs(_LOGGER_NAME, level="WARNING") as logs,
                self.assertRaises(SemanticRouteAdjudicatorError) as caught,
            ):
                adapter.adjudicate_with_result(_batch())
            self.assertEqual(caught.exception.reason_code, "invalid_runtime_protocol")
            self.assertFalse(caught.exception.retryable)
            (record,) = _failure_records(logs.output)
            self.assertEqual(
                (record["terminal_http_statuses"], record["terminal_message_count"]), ([], 0)
            )

    def test_missing_executable_is_availability_failure(self) -> None:
        adapter = ClaudeCliSemanticAdjudicator(executable=Path("/missing/claude"))
        with (
            mock.patch(
                "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                side_effect=FileNotFoundError("missing"),
            ),
            self.assertRaises(SemanticRouteAdjudicatorError) as caught,
        ):
            adapter.adjudicate_with_result(_batch())

        self.assertEqual(caught.exception.reason_code, "executable_unavailable")
        self.assertTrue(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
