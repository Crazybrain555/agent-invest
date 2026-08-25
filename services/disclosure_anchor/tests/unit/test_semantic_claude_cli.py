from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

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


class ClaudeCliSemanticAdjudicatorTests(unittest.TestCase):
    def test_uses_closed_noninteractive_cli_and_attests_canonical_model(self) -> None:
        captured: dict[str, object] = {}

        def run(*, args, prompt, env, timeout_seconds):  # type: ignore[no-untyped-def]
            captured.update(
                args=args,
                prompt=prompt,
                env=env,
                timeout_seconds=timeout_seconds,
            )
            return subprocess.CompletedProcess(args, 0, _stdout(), "")

        adapter = ClaudeCliSemanticAdjudicator(
            executable=Path("/opt/claude"),
            model="claude-sonnet-5",
            reasoning_effort="low",
        )
        with mock.patch(
            "disclosure_anchor.adapters.semantics.codex_cli._run_process",
            side_effect=run,
        ):
            result = adapter.adjudicate_with_result(_batch())

        self.assertEqual(result.decisions[0].routes[0].key, "forecast_summary")
        self.assertTrue(result.response_sha256.startswith("sha256:"))
        self.assertEqual(adapter.provider_identity.canonical_model, "claude-sonnet-5")
        self.assertEqual(adapter.provider_identity.adapter_version, "claude_cli.v2")
        args = captured["args"]
        assert isinstance(args, list)
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
        self.assertEqual(
            args[args.index("--mcp-config") + 1],
            '{"mcpServers":{}}',
        )
        env = captured["env"]
        assert isinstance(env, dict)
        self.assertNotIn("DATABASE_URL", env)
        self.assertNotIn("CNINFO_ACCESS_SECRET", env)

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
                self.assertRaises(SemanticRouteAdjudicatorError) as caught,
            ):
                adapter.adjudicate_with_result(_batch())
            self.assertEqual(caught.exception.reason_code, reason_code)
            self.assertEqual(caught.exception.retryable, retryable)

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
        )
        for payload, stderr, reason_code, retryable in cases:
            with (
                self.subTest(reason_code=reason_code),
                mock.patch(
                    "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                    return_value=subprocess.CompletedProcess(
                        ["claude"], 1, json.dumps(payload), stderr
                    ),
                ),
                self.assertRaises(SemanticRouteAdjudicatorError) as caught,
            ):
                adapter.adjudicate_with_result(_batch())
            self.assertEqual(caught.exception.reason_code, reason_code)
            self.assertEqual(caught.exception.retryable, retryable)

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
        for payload in cases:
            with (
                self.subTest(payload=payload),
                mock.patch(
                    "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                    return_value=subprocess.CompletedProcess(
                        ["claude"], 1, json.dumps(payload), ""
                    ),
                ),
                self.assertRaises(SemanticRouteAdjudicatorError) as caught,
            ):
                adapter.adjudicate_with_result(_batch())
            expected_reason = (
                "forbidden_tool_call" if payload in cases[-3:] else "invalid_runtime_protocol"
            )
            self.assertEqual(caught.exception.reason_code, expected_reason)
            self.assertFalse(caught.exception.retryable)

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
                self.assertRaises(SemanticRouteAdjudicatorError) as caught,
            ):
                adapter.adjudicate_with_result(_batch())
            self.assertEqual(caught.exception.reason_code, "invalid_runtime_protocol")
            self.assertFalse(caught.exception.retryable)

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
