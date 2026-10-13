"""Closed-vocabulary semantic adjudication through Claude Code CLI."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
from pathlib import Path
import re
import subprocess
import threading
from typing import Any

from disclosure_anchor.adapters.semantics import codex_cli
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_OUTPUT_SCHEMA_VERSION,
    SEMANTIC_PROMPT_VERSION,
    SemanticAdjudicationDecision,
    SemanticProviderIdentity,
)
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticAdjudicationBatch,
    SemanticAdjudicatorIdentity,
    SemanticExecutionGuard,
    SemanticProviderResult,
    SemanticRouteAdjudicatorError,
)
from disclosure_anchor.application.ports.staged_execution import current_semantic_group, note_stage


_CLAUDE_AUTH_DIAGNOSTICS = (
    *codex_cli._AUTH_DIAGNOSTICS,
    re.compile(r"anthropic profile login expired[.!]?", re.IGNORECASE),
    # Claude Code 2.1.274 `result` when the CLI's own stored login is gone.
    re.compile(
        r"failed to authenticate:\s*oauth session expired and could not be refreshed[.!]?",
        re.IGNORECASE,
    ),
)
_CLAUDE_CAPACITY_DIAGNOSTICS = (
    *codex_cli._CAPACITY_DIAGNOSTICS,
    re.compile(r"api error:\s*(?:429\s+)?overloaded[.!]?", re.IGNORECASE),
    re.compile(r"api error:\s*529(?:\s+overloaded)?[.!]?", re.IGNORECASE),
    re.compile(r"repeated 529 overloaded errors[.!]?", re.IGNORECASE),
    re.compile(
        r"server is temporarily limiting requests \(not your usage limit\)[.!]?",
        re.IGNORECASE,
    ),
    re.compile(r"overloaded_error[.!]?", re.IGNORECASE),
)
# Claude Code 2.1.280 `result` sentences for a request that never got an HTTP
# response, as its API-error mapper writes them after the CLI's own retries
# (embedded Anthropic SDK 0.112.1 APIConnectionError "Connection error.",
# APIConnectionTimeoutError, or a socket error code on the cause chain). Each
# sentence keeps the CLI's closed code list. Certificate/TLS forms, the proxy
# tunnel refusal, an unlisted code and the first-byte "No response" form are
# deliberately absent: they stay unrecognized and fail closed.
_CLAUDE_DROPPED_CODES = "ECONNRESET|EPIPE|ECONNABORTED|ConnectionClosed|ERR_SOCKET_CLOSED|UND_ERR_SOCKET"
_CLAUDE_REFUSED_CODES = "ECONNREFUSED|ConnectionRefused"
_CLAUDE_UNREACHABLE_CODES = "ENOTFOUND|EAI_AGAIN|FailedToOpenSocket"
_CLAUDE_NO_ROUTE_CODES = "ENETUNREACH|ENETDOWN|EHOSTUNREACH|EHOSTDOWN"
# The CLI's lost-connection sentence takes every socket code above plus these
# two; its proxy-tunnel code is left out with the proxy sentence.
_CLAUDE_LOST_CODES = "|".join((
    _CLAUDE_DROPPED_CODES, "ETIMEDOUT|StreamSuspended", _CLAUDE_REFUSED_CODES,
    _CLAUDE_UNREACHABLE_CODES, _CLAUDE_NO_ROUTE_CODES,
))
_CLAUDE_TRANSPORT_DIAGNOSTICS = tuple(
    re.compile(pattern)
    for pattern in (
        r"Request timed out",
        r"API Error: Request timed out\. Check your internet connection and proxy settings",
        r"API Error: Unable to connect to API\. Check your internet connection",
        rf"API Error: Connection dropped \((?:{_CLAUDE_DROPPED_CODES})\)",
        (
            r"API Error: Connection refused — a firewall or proxy may be blocking it"
            rf" \((?:{_CLAUDE_REFUSED_CODES})\)"
        ),
        (
            r"API Error: Can't reach the API server — check your internet or DNS"
            rf" \((?:{_CLAUDE_UNREACHABLE_CODES})\)"
        ),
        (
            r"API Error: No internet route — check your connection or VPN"
            rf" \((?:{_CLAUDE_NO_ROUTE_CODES})\)"
        ),
        r"API Error: Connection lost while your computer was asleep",
        (
            rf"API Error: Connection to the API was lost \((?:{_CLAUDE_LOST_CODES})\)\."
            r" This is usually temporary — try again\."
        ),
    )
)
_ERROR_ENVELOPE_CORE_FIELDS = frozenset(
    {"is_error", "permission_denials", "api_error_status", "result"}
)
_ERROR_ENVELOPE_INT_METADATA = frozenset(
    {
        "duration_api_ms",
        "duration_ms",
        "num_turns",
        "queued_turn_count",
        "result_index",
        "time_to_request_ms",
        "ttft_ms",
        "ttft_stream_ms",
    }
)
_ERROR_ENVELOPE_STRING_METADATA = frozenset(
    {"fast_mode_state", "session_id", "subtype", "type", "uuid"}
)
_ERROR_ENVELOPE_OPTIONAL_STRING_METADATA = frozenset(
    {"fast_mode_disabled_reason", "stop_reason", "terminal_reason"}
)
_ERROR_ENVELOPE_DICT_METADATA = frozenset({"modelUsage", "subagent_stats", "usage"})
# Claude Code 2.1.274 subagent accounting; the adjudicator runs with tools
# disabled, so every counter must stay zero (a nonzero one is a capability breach).
_SUBAGENT_STATS_COUNTERS = frozenset(
    {"completed", "failed", "max_depth", "spawned", "spawned_by_subagents", "started_in_background"}
)
_SUBAGENT_STATS_GROUPS: dict[str, frozenset[str]] = {
    "killed": frozenset({"parent", "system", "user"}),
    "refused": frozenset({"budget", "concurrency_limit", "depth_limit"}),
    "requested": frozenset({"background", "foreground", "unset"}),
}
_SUBAGENT_STATS_FIELDS = _SUBAGENT_STATS_COUNTERS | set(_SUBAGENT_STATS_GROUPS) | {"by_type"}
_ERROR_ENVELOPE_FIELDS = (
    _ERROR_ENVELOPE_CORE_FIELDS
    | _ERROR_ENVELOPE_INT_METADATA
    | _ERROR_ENVELOPE_STRING_METADATA
    | _ERROR_ENVELOPE_OPTIONAL_STRING_METADATA
    | _ERROR_ENVELOPE_DICT_METADATA
    | {"total_cost_usd"}
)
_USAGE_FIELDS = frozenset(
    {
        "cache_creation",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "inference_geo",
        "input_tokens",
        "iterations",
        "output_tokens",
        "output_tokens_details",
        "server_tool_use",
        "service_tier",
        "speed",
    }
)
_MODEL_USAGE_FIELDS = frozenset(
    {
        "cacheCreationInputTokens",
        "cacheReadInputTokens",
        "canonicalModel",
        "contextWindow",
        "costUSD",
        "inputTokens",
        "maxOutputTokens",
        "outputTokens",
        "provider",
        "webSearchRequests",
    }
)
_ERROR_ENVELOPE_CANONICAL_MODELS = frozenset(
    {"claude-haiku-4-5", "claude-sonnet-5"}
)
_LOGGER = logging.getLogger(__name__)


class ClaudeCliSemanticAdjudicator:
    """Use a canonical Claude model with tools and persistence disabled."""

    def __init__(
        self,
        *,
        executable: Path,
        model: str = "claude-sonnet-5",
        reasoning_effort: str = "low",
        timeout_seconds: int = 600,
        max_concurrency: int = 1,
        provider_id: str = "sonnet-backup",
    ) -> None:
        if model != "claude-sonnet-5":
            raise ValueError("Claude semantic model must use canonical claude-sonnet-5")
        if reasoning_effort not in {"low", "medium", "high"}:
            raise ValueError("Claude semantic reasoning effort is invalid")
        if timeout_seconds < 1 or max_concurrency < 1 or not provider_id:
            raise ValueError("Claude semantic adjudicator configuration is invalid")
        self._executable = executable
        self._timeout_seconds = timeout_seconds
        self._slot = threading.BoundedSemaphore(max_concurrency)
        self._identity = SemanticAdjudicatorIdentity(
            adapter=f"claude_cli.v1.{reasoning_effort}",
            model=model,
            prompt_version=SEMANTIC_PROMPT_VERSION,
        )
        self._provider_identity = SemanticProviderIdentity(
            provider_id=provider_id,
            provider="anthropic",
            adapter_kind="claude_cli",
            adapter_version="claude_cli.v2",
            canonical_model=model,
            inference_profile=reasoning_effort,
            prompt_version=SEMANTIC_PROMPT_VERSION,
            prompt_sha256=codex_cli._contract_hash("prompt", SEMANTIC_PROMPT_VERSION),
            output_schema_version=SEMANTIC_OUTPUT_SCHEMA_VERSION,
            output_schema_sha256=_output_schema_contract_hash(),
        )

    @property
    def identity(self) -> SemanticAdjudicatorIdentity:
        return self._identity

    @property
    def provider_identity(self) -> SemanticProviderIdentity:
        return self._provider_identity

    def adjudicate(
        self,
        batch: SemanticAdjudicationBatch,
    ) -> tuple[SemanticAdjudicationDecision, ...]:
        return self.adjudicate_with_result(batch).decisions

    def adjudicate_with_result(
        self,
        batch: SemanticAdjudicationBatch,
        *, stage_guard: SemanticExecutionGuard | None = None,
    ) -> SemanticProviderResult:
        if stage_guard is not None:
            stage_guard.checkpoint()
        group_hash = current_semantic_group()
        provider_id = self._provider_identity.provider_id
        note_stage(stage_guard, "slot_requested", group_hash=group_hash, provider_id=provider_id)
        while not self._slot.acquire(timeout=0.1):
            if stage_guard is not None:
                stage_guard.checkpoint()
            if codex_cli._SEMANTIC_SHUTDOWN_REQUESTED.is_set():
                raise _cancelled("before admission")
        note_stage(stage_guard, "slot_acquired", group_hash=group_hash, provider_id=provider_id)
        try:
            if stage_guard is not None:
                stage_guard.checkpoint()
            if codex_cli._SEMANTIC_SHUTDOWN_REQUESTED.is_set():
                raise _cancelled("before admission")
            result = self._adjudicate_serial(batch, stage_guard=stage_guard)
            if stage_guard is not None:
                stage_guard.checkpoint()
            return result
        finally:
            self._slot.release()
            note_stage(stage_guard, "slot_released", group_hash=group_hash, provider_id=provider_id)

    def _adjudicate_serial(
        self,
        batch: SemanticAdjudicationBatch,
        *, stage_guard: SemanticExecutionGuard | None = None,
    ) -> SemanticProviderResult:
        schema = _output_schema(batch)
        args = [
            str(self._executable),
            "-p",
            "--model",
            self._identity.model,
            "--effort",
            self._provider_identity.inference_profile,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, ensure_ascii=False, sort_keys=True),
            "--no-session-persistence",
            "--safe-mode",
            "--disable-slash-commands",
            "--no-chrome",
            "--permission-mode",
            "dontAsk",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--tools",
            "",
        ]
        try:
            completed = codex_cli._run_process(
                args=args,
                prompt=codex_cli._prompt(batch),
                env=codex_cli._safe_subprocess_environment(),
                timeout_seconds=self._timeout_seconds,
                **({} if stage_guard is None else {"stage_guard": stage_guard}),
            )
            if stage_guard is not None:
                stage_guard.checkpoint()
        except subprocess.TimeoutExpired as exc:
            raise SemanticRouteAdjudicatorError(
                "Claude semantic adjudication timed out",
                reason_code="timeout",
                retryable=True,
            ) from exc
        except codex_cli._SemanticProcessCancelled as exc:
            raise _cancelled("during execution") from exc
        except OSError as exc:
            unavailable = isinstance(exc, (FileNotFoundError, PermissionError))
            raise SemanticRouteAdjudicatorError(
                "Claude semantic adjudicator could not start",
                reason_code=(
                    "executable_unavailable" if unavailable else "runtime_io_failed"
                ),
                retryable=True,
            ) from exc
        if completed.returncode != 0:
            raise _command_error(completed)
        try:
            structured = _structured_output(completed.stdout)
        except SemanticRouteAdjudicatorError as exc:
            if exc.reason_code == "runtime_event_error":
                # An exit-0 error envelope fails closed like a nonzero exit. It
                # never passed the closed error-envelope validation, so the
                # record trusts none of its fields.
                _log_command_failure(completed, exc)
            raise
        canonical = json.dumps(
            structured,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return SemanticProviderResult(
            decisions=codex_cli._decode_result(canonical, batch),
            response_sha256="sha256:" + hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest(),
        )


def _structured_output(stdout: str) -> dict[str, object]:
    try:
        payload = codex_cli._strict_json_loads(stdout)
    except (json.JSONDecodeError, codex_cli._ClosedJsonError) as exc:
        raise SemanticRouteAdjudicatorError(
            "Claude semantic runtime output is not JSON",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        ) from exc
    if not isinstance(payload, dict):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic runtime output is invalid",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    is_error = payload.get("is_error")
    if type(is_error) is not bool:
        raise SemanticRouteAdjudicatorError(
            "Claude semantic runtime error status is invalid",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    if is_error:
        raise SemanticRouteAdjudicatorError(
            "Claude semantic runtime reported an error",
            reason_code="runtime_event_error",
            retryable=False,
        )
    denials = payload.get("permission_denials")
    if not isinstance(denials, list):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic runtime permission receipt is invalid",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    if denials:
        raise SemanticRouteAdjudicatorError(
            "Claude semantic adjudicator attempted a disabled capability",
            reason_code="forbidden_tool_call",
            retryable=False,
        )
    # The success path validates no envelope shape, so an unreadable subagent
    # block counts as a breach rather than as zero activity.
    if (
        _reports_disabled_capability(payload)
        or _reports_subagent_activity(payload)
        or (
            "subagent_stats" in payload
            and not _valid_subagent_stats(payload["subagent_stats"])
        )
    ):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic runtime reported a disabled capability",
            reason_code="forbidden_tool_call",
            retryable=False,
        )
    canonical_models: set[str] = set()
    model_usage = payload.get("modelUsage")
    if isinstance(model_usage, dict):
        for item in model_usage.values():
            if not isinstance(item, dict):
                continue
            canonical_model = item.get("canonicalModel")
            if isinstance(canonical_model, str):
                canonical_models.add(canonical_model)
    if (
        "claude-sonnet-5" not in canonical_models
        or not canonical_models <= _ERROR_ENVELOPE_CANONICAL_MODELS
    ):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic runtime model identity drifted",
            reason_code="model_identity_mismatch",
            retryable=False,
        )
    structured = payload.get("structured_output")
    if not isinstance(structured, dict):
        result = payload.get("result")
        if isinstance(result, str):
            try:
                structured = codex_cli._strict_json_loads(result)
            except (json.JSONDecodeError, codex_cli._ClosedJsonError):
                structured = None
    if not isinstance(structured, dict):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic adjudicator did not produce structured output",
            reason_code="result_missing",
            retryable=False,
        )
    return structured


def _output_schema(batch: SemanticAdjudicationBatch) -> dict[str, object]:
    schema = codex_cli._output_schema(batch)
    # Claude Code 2.1.237 validates the supported object vocabulary but rejects
    # the otherwise-correct draft URI metadata as an unknown schema ref.
    schema.pop("$schema", None)
    return schema


def _output_schema_contract_hash() -> str:
    raw = (
        SEMANTIC_OUTPUT_SCHEMA_VERSION
        + "\n"
        + inspect.getsource(codex_cli._output_schema)
        + inspect.getsource(_output_schema)
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _command_error(
    completed: subprocess.CompletedProcess[str],
) -> SemanticRouteAdjudicatorError:
    envelope: dict[str, Any] | None = None
    if completed.stdout.strip():
        try:
            envelope = _error_envelope(completed.stdout)
        except SemanticRouteAdjudicatorError as exc:
            _log_command_failure(completed, exc)
            return exc
        reason = _structured_command_reason(envelope, completed.stderr)
    else:
        diagnostics = (completed.stderr,) if completed.stderr.strip() else ()
        if any("not a valid json schema" in item.casefold() for item in diagnostics):
            reason = "invalid_output_schema"
        else:
            reason = _known_availability_reason(diagnostics) or "command_failed"
    error = SemanticRouteAdjudicatorError(
        f"Claude semantic adjudicator failed with exit {completed.returncode}",
        reason_code=reason,
        retryable=reason in {"not_authenticated", "capacity_unavailable", "transport_unavailable"},
    )
    _log_command_failure(completed, error, envelope)
    return error


def _log_command_failure(
    completed: subprocess.CompletedProcess[str],
    error: SemanticRouteAdjudicatorError,
    envelope: dict[str, Any] | None = None,
) -> None:
    # Only an envelope that passed the closed validation contributes its typed
    # status and whether it carried a terminal message; no text is copied.
    status = None if envelope is None else envelope.get("api_error_status")
    result = None if envelope is None else envelope.get("result")
    codex_cli._log_provider_failure(
        _LOGGER,
        provider="claude_cli",
        completed=completed,
        error=error,
        terminal_http_statuses=(
            [status] if type(status) is int and 100 <= status <= 599 else []
        ),
        terminal_message_count=1 if isinstance(result, str) and result.strip() else 0,
    )


def _error_envelope(stdout: str) -> dict[str, Any]:
    try:
        payload = codex_cli._strict_json_loads(stdout)
    except (json.JSONDecodeError, codex_cli._ClosedJsonError) as exc:
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error envelope is not JSON",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        ) from exc
    if not isinstance(payload, dict):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error envelope is invalid",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    _validate_error_envelope_metadata(payload)
    is_error = payload.get("is_error")
    denials = payload.get("permission_denials")
    if type(is_error) is not bool or not isinstance(denials, list) or not is_error:
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error envelope fields are invalid",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    if denials:
        raise SemanticRouteAdjudicatorError(
            "Claude semantic adjudicator attempted a disabled capability",
            reason_code="forbidden_tool_call",
            retryable=False,
        )
    status = payload.get("api_error_status")
    if status is not None and type(status) is not int:
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error status is invalid",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    result = payload.get("result")
    if result is not None and not isinstance(result, str):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error result is invalid",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    return payload


def _structured_command_reason(envelope: dict[str, Any], stderr: str = "") -> str:
    status = envelope.get("api_error_status")
    result = envelope.get("result")
    diagnostics = tuple(
        item
        for item in (result, stderr)
        if isinstance(item, str) and item.strip()
    )
    if any("not a valid json schema" in item.casefold() for item in diagnostics):
        return "invalid_output_schema"
    typed_reason = (
        None
        if status is None
        else {
            401: "not_authenticated",
            429: "capacity_unavailable",
            529: "capacity_unavailable",
        }.get(status)
    )
    if status is not None and typed_reason is None:
        return "command_failed"
    text_reason = _known_availability_reason(diagnostics)
    if typed_reason is not None:
        if diagnostics and text_reason != typed_reason:
            return "command_failed"
        return typed_reason
    return text_reason or "command_failed"


def _validate_error_envelope_metadata(payload: dict[str, object]) -> None:
    if set(payload) - _ERROR_ENVELOPE_FIELDS:
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error envelope contains unknown fields",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    if _reports_disabled_capability(payload) or _reports_subagent_activity(payload):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error envelope reported a disabled capability",
            reason_code="forbidden_tool_call",
            retryable=False,
        )
    if any(
        field in payload
        and not _is_nonnegative_int(payload[field])
        for field in _ERROR_ENVELOPE_INT_METADATA
    ):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error envelope integer metadata is invalid",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    if any(
        field in payload and not isinstance(payload[field], str)
        for field in _ERROR_ENVELOPE_STRING_METADATA
    ):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error envelope string metadata is invalid",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    if (
        ("type" in payload and payload["type"] != "result")
        # Claude Code 2.1.237, re-observed at 2.1.274: `success` is the subtype of a
        # completed CLI envelope even when `is_error=true`; `terminal_reason=api_error`
        # carries the failure.
        or ("subtype" in payload and payload["subtype"] != "success")
        or ("fast_mode_state" in payload and payload["fast_mode_state"] != "off")
        or (
            "session_id" in payload
            and not str(payload["session_id"]).strip()
        )
        or ("uuid" in payload and not str(payload["uuid"]).strip())
    ):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error envelope string metadata is unsupported",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    if any(
        field in payload
        and payload[field] is not None
        and not isinstance(payload[field], str)
        for field in _ERROR_ENVELOPE_OPTIONAL_STRING_METADATA
    ):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error envelope optional metadata is invalid",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    if (
        payload.get("fast_mode_disabled_reason")
        not in {None, "sdk_opt_in_required"}
        or payload.get("stop_reason") not in {None, "end_turn", "stop_sequence"}
        or payload.get("terminal_reason") not in {None, "api_error"}
    ):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error envelope outcome metadata is unsupported",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    if "total_cost_usd" in payload:
        cost = payload["total_cost_usd"]
        if not _is_nonnegative_finite_number(cost):
            raise SemanticRouteAdjudicatorError(
                "Claude semantic error envelope cost metadata is invalid",
                reason_code="invalid_runtime_protocol",
                retryable=False,
            )
    if "usage" in payload and not _valid_usage(payload["usage"]):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error envelope usage metadata is invalid",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    if "modelUsage" in payload and not _valid_model_usage(payload["modelUsage"]):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error envelope model metadata is invalid",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )
    if "subagent_stats" in payload and not _valid_subagent_stats(
        payload["subagent_stats"]
    ):
        raise SemanticRouteAdjudicatorError(
            "Claude semantic error envelope subagent metadata is invalid",
            reason_code="invalid_runtime_protocol",
            retryable=False,
        )


def _valid_subagent_stats(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _SUBAGENT_STATS_FIELDS:
        return False
    if any(not _is_nonnegative_int(value.get(key)) for key in _SUBAGENT_STATS_COUNTERS):
        return False
    for key, members in _SUBAGENT_STATS_GROUPS.items():
        group = value.get(key)
        if (
            not isinstance(group, dict)
            or set(group) != members
            or any(not _is_nonnegative_int(item) for item in group.values())
        ):
            return False
    return value.get("by_type") == {}


def _reports_subagent_activity(payload: dict[str, object]) -> bool:
    stats = payload.get("subagent_stats")
    if not isinstance(stats, dict):
        return False
    if any(_is_positive_int(stats.get(key)) for key in _SUBAGENT_STATS_COUNTERS):
        return True
    return any(
        isinstance(stats.get(key), dict)
        and any(_is_positive_int(item) for item in stats[key].values())
        for key in _SUBAGENT_STATS_GROUPS
    )


def _valid_usage(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _USAGE_FIELDS:
        return False
    for key in (
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "input_tokens",
        "output_tokens",
    ):
        if not _is_nonnegative_int(value.get(key)):
            return False
    if (
        value.get("inference_geo") != ""
        or value.get("service_tier") != "standard"
        or value.get("speed") != "standard"
    ):
        return False
    output_details = value.get("output_tokens_details")
    if (
        not isinstance(output_details, dict)
        or set(output_details) != {"thinking_tokens"}
        or not _is_nonnegative_int(output_details.get("thinking_tokens"))
    ):
        return False
    server_tools = value.get("server_tool_use")
    if (
        not isinstance(server_tools, dict)
        or set(server_tools) != {"web_fetch_requests", "web_search_requests"}
        or any(
            not _is_nonnegative_int(item) or item != 0
            for item in server_tools.values()
        )
    ):
        return False
    cache_creation = value.get("cache_creation")
    if not _valid_cache_creation(cache_creation):
        return False
    return value.get("iterations") == []


def _valid_cache_creation(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"}
        and all(_is_nonnegative_int(item) for item in value.values())
    )


def _valid_model_usage(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    canonical_models: set[str] = set()
    for model, item in value.items():
        if not isinstance(model, str) or not isinstance(item, dict):
            return False
        if set(item) != _MODEL_USAGE_FIELDS:
            return False
        for key in (
            "cacheCreationInputTokens",
            "cacheReadInputTokens",
            "contextWindow",
            "inputTokens",
            "maxOutputTokens",
            "outputTokens",
            "webSearchRequests",
        ):
            if not _is_nonnegative_int(item.get(key)):
                return False
        cost = item.get("costUSD")
        if not _is_nonnegative_finite_number(cost):
            return False
        canonical_model = item.get("canonicalModel")
        provider = item.get("provider")
        if not isinstance(canonical_model, str) or not isinstance(provider, str):
            return False
        canonical_models.add(canonical_model)
        if provider != "firstParty" or item.get("webSearchRequests") != 0:
            return False
    return not value or (
        "claude-sonnet-5" in canonical_models
        and canonical_models <= _ERROR_ENVELOPE_CANONICAL_MODELS
    )


def _reports_disabled_capability(payload: dict[str, object]) -> bool:
    usage = payload.get("usage")
    if isinstance(usage, dict):
        server_tools = usage.get("server_tool_use")
        if isinstance(server_tools, dict) and any(
            _is_positive_int(server_tools.get(key))
            for key in ("web_fetch_requests", "web_search_requests")
        ):
            return True
    model_usage = payload.get("modelUsage")
    return isinstance(model_usage, dict) and any(
        isinstance(item, dict)
        and _is_positive_int(item.get("webSearchRequests"))
        for item in model_usage.values()
    )


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_nonnegative_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
        and math.isfinite(value)
    )


def _known_availability_reason(diagnostics: tuple[str, ...]) -> str | None:
    return codex_cli._known_availability_reason(
        diagnostics,
        auth_diagnostics=_CLAUDE_AUTH_DIAGNOSTICS,
        capacity_diagnostics=_CLAUDE_CAPACITY_DIAGNOSTICS,
        transport_diagnostics=_CLAUDE_TRANSPORT_DIAGNOSTICS,
    )


def _cancelled(stage: str) -> SemanticRouteAdjudicatorError:
    return SemanticRouteAdjudicatorError(
        f"Claude semantic adjudication was cancelled {stage}",
        reason_code="cancelled",
        retryable=True,
    )


__all__ = ["ClaudeCliSemanticAdjudicator"]
