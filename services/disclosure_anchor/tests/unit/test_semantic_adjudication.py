from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from disclosure_anchor.adapters.semantics.claude_cli import (
    ClaudeCliSemanticAdjudicator,
)
from disclosure_anchor.adapters.semantics.codex_cli import CodexCliSemanticAdjudicator
from disclosure_anchor.application.contracts.semantic_routes import (
    SemanticAdjudicationDecision,
    SemanticAdjudicatedRoute,
    SemanticDocumentContext,
    SemanticProviderIdentity,
    SemanticRouteCandidate,
    SemanticRouteDefinition,
    SemanticRouteSource,
    SemanticRouteTaxonomy,
    SemanticRouteUnitInput,
)
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticAdjudicationBatch,
    SemanticAdjudicationCacheEntry,
    SemanticProviderResult,
    SemanticRouteAdjudicatorError,
    SemanticRouteCacheError,
)
from disclosure_anchor.application.services.semantic_adjudication import (
    ConfiguredSemanticProvider,
    OrderedSemanticAdjudicationExecutor,
    semantic_group_cache_key,
)


_GROUP_HASH = "sha256:" + "9" * 64
_RESPONSE_HASH = "sha256:" + "8" * 64


def _identity(provider_id: str, *, provider: str = "openai") -> SemanticProviderIdentity:
    return SemanticProviderIdentity(
        provider_id=provider_id,
        provider=provider,
        adapter_kind="test_cli",
        adapter_version="test_cli.v1",
        canonical_model=f"{provider_id}-model",
        inference_profile="low",
        prompt_version="semantic_prompt.test",
        prompt_sha256="sha256:" + "1" * 64,
        output_schema_version="semantic_schema.test",
        output_schema_sha256="sha256:" + "2" * 64,
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
                input_hash="sha256:" + "3" * 64,
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


def _decisions() -> tuple[SemanticAdjudicationDecision, ...]:
    return (
        SemanticAdjudicationDecision(
            unit_index=0,
            routes=(
                SemanticAdjudicatedRoute(
                    key="forecast_summary",
                    support_ids=("u0:title",),
                ),
            ),
        ),
    )


class _Cache:
    def __init__(self, *, fail_write: bool = False) -> None:
        self.entries: dict[str, SemanticAdjudicationCacheEntry] = {}
        self.fail_write = fail_write

    def get(self, cache_key: str) -> SemanticAdjudicationCacheEntry | None:
        return self.entries.get(cache_key)

    def put(self, entry: SemanticAdjudicationCacheEntry) -> None:
        if self.fail_write:
            raise SemanticRouteCacheError("cache unavailable", retryable=True)
        self.entries[entry.cache_key] = entry


class _Adapter:
    def __init__(
        self,
        identity: SemanticProviderIdentity,
        *,
        reason_code: str | None = None,
        retryable: bool = False,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.provider_identity = identity
        self.identity = type(
            "LegacyIdentity",
            (),
            {
                "adapter": identity.adapter_version,
                "model": identity.canonical_model,
                "prompt_version": identity.prompt_version,
            },
        )()
        self.reason_code = reason_code
        self.retryable = retryable
        self.entered = entered
        self.release = release
        self.calls = 0

    def adjudicate(
        self, batch: SemanticAdjudicationBatch
    ) -> tuple[SemanticAdjudicationDecision, ...]:
        return self.adjudicate_with_result(batch).decisions

    def adjudicate_with_result(
        self, batch: SemanticAdjudicationBatch
    ) -> SemanticProviderResult:
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(timeout=5)
        if self.reason_code is not None:
            raise SemanticRouteAdjudicatorError(
                f"provider failed: {self.reason_code}",
                reason_code=self.reason_code,
                retryable=self.retryable,
            )
        return SemanticProviderResult(
            decisions=_decisions(),
            response_sha256=_RESPONSE_HASH,
        )


def _configured(adapter: _Adapter, cache: _Cache | None = None) -> ConfiguredSemanticProvider:
    return ConfiguredSemanticProvider(
        adapter=adapter,  # type: ignore[arg-type]
        cache=cache or _Cache(),
    )


class OrderedSemanticAdjudicationExecutorTests(unittest.TestCase):
    def test_primary_success_records_actual_identity_and_cache(self) -> None:
        adapter = _Adapter(_identity("primary"))
        cache = _Cache()
        executor = OrderedSemanticAdjudicationExecutor((_configured(adapter, cache),))

        outcome = executor.adjudicate(_batch(), group_hash=_GROUP_HASH)

        self.assertEqual(outcome.decisions, _decisions())
        self.assertEqual(outcome.actual_result_attempt, 1)
        self.assertEqual(outcome.actual_result_identity, adapter.provider_identity)
        self.assertEqual(outcome.attempts[0].outcome, "succeeded")
        self.assertFalse(outcome.degraded_unavailable)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(len(cache.entries), 1)

    def test_availability_failure_uses_backup_and_records_both_attempts(self) -> None:
        primary = _Adapter(
            _identity("primary"), reason_code="capacity_unavailable", retryable=True
        )
        backup = _Adapter(_identity("backup", provider="anthropic"))
        executor = OrderedSemanticAdjudicationExecutor(
            (_configured(primary), _configured(backup))
        )

        outcome = executor.adjudicate(_batch(), group_hash=_GROUP_HASH)

        self.assertEqual(
            tuple(item.outcome for item in outcome.attempts),
            ("availability_failed", "succeeded"),
        )
        self.assertEqual(outcome.actual_result_attempt, 2)
        self.assertEqual(outcome.actual_result_identity, backup.provider_identity)

    def test_all_availability_failures_produce_explicit_degraded_outcome(self) -> None:
        primary = _Adapter(
            _identity("primary"), reason_code="executable_unavailable", retryable=True
        )
        backup = _Adapter(
            _identity("backup", provider="anthropic"),
            reason_code="not_authenticated",
            retryable=False,
        )
        executor = OrderedSemanticAdjudicationExecutor(
            (_configured(primary), _configured(backup))
        )

        outcome = executor.adjudicate(_batch(), group_hash=_GROUP_HASH)

        self.assertTrue(outcome.degraded_unavailable)
        self.assertEqual(outcome.decisions, ())
        self.assertIsNone(outcome.actual_result_identity)
        self.assertTrue(
            all(item.availability_abstain_eligible for item in outcome.attempts)
        )

    def test_real_codex_collision_fails_closed_without_calling_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            primary = CodexCliSemanticAdjudicator(
                executable=Path("/opt/codex"),
                runtime_tmp_root=Path(tmp),
            )
            backup = _Adapter(_identity("backup", provider="anthropic"))
            executor = OrderedSemanticAdjudicationExecutor(
                (
                    ConfiguredSemanticProvider(adapter=primary, cache=_Cache()),
                    _configured(backup),
                )
            )
            with (
                mock.patch(
                    "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                    return_value=subprocess.CompletedProcess(
                        ["codex"],
                        1,
                        "",
                        (
                            "API Error: 429 Too Many Requests\n"
                            "fatal protocol parser crashed"
                        ),
                    ),
                ),
                self.assertRaises(SemanticRouteAdjudicatorError) as caught,
            ):
                executor.adjudicate(_batch(), group_hash=_GROUP_HASH)

        self.assertEqual(caught.exception.reason_code, "command_failed")
        self.assertEqual(backup.calls, 0)
        self.assertEqual(caught.exception.attempts[0].outcome, "failed_closed")
        self.assertFalse(caught.exception.attempts[0].availability_abstain_eligible)

    def test_real_claude_duplicate_error_field_never_uses_backup(self) -> None:
        primary = ClaudeCliSemanticAdjudicator(executable=Path("/opt/claude"))
        backup = _Adapter(_identity("backup"))
        executor = OrderedSemanticAdjudicationExecutor(
            (
                ConfiguredSemanticProvider(adapter=primary, cache=_Cache()),
                _configured(backup),
            )
        )
        model_usage_entry = {
            "inputTokens": 0,
            "outputTokens": 0,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
            "webSearchRequests": 0,
            "costUSD": 0.0,
            "contextWindow": 1_000_000,
            "maxOutputTokens": 64_000,
            "provider": "firstParty",
        }
        stdout_cases = (
            (
                '{"is_error":true,"permission_denials":[],'
                '"api_error_status":429,"result":"forbidden tool call",'
                '"result":"API Error: 429 Too Many Requests"}',
                "invalid_runtime_protocol",
            ),
            (
                json.dumps(
                    {
                        "is_error": True,
                        "permission_denials": [],
                        "api_error_status": 429,
                        "result": "API Error: 429 Too Many Requests",
                        "modelUsage": {
                            "sonnet": {
                                **model_usage_entry,
                                "canonicalModel": "claude-sonnet-5",
                            },
                            "unexpected": {
                                **model_usage_entry,
                                "canonicalModel": "claude-opus-4-1",
                            },
                        },
                    }
                ),
                "invalid_runtime_protocol",
            ),
            (
                json.dumps(
                    {
                        "is_error": True,
                        "permission_denials": [],
                        "api_error_status": 429,
                        "result": "API Error: 429 Too Many Requests",
                        "modelUsage": {
                            "sonnet": {
                                **model_usage_entry,
                                "canonicalModel": "claude-sonnet-5",
                                "webSearchRequests": 1,
                            },
                        },
                    }
                ),
                "forbidden_tool_call",
            ),
        )
        for stdout, reason_code in stdout_cases:
            with (
                self.subTest(stdout=stdout),
                mock.patch(
                    "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                    return_value=subprocess.CompletedProcess(
                        ["claude"],
                        1,
                        stdout,
                        "",
                    ),
                ),
                self.assertRaises(SemanticRouteAdjudicatorError) as caught,
            ):
                executor.adjudicate(_batch(), group_hash=_GROUP_HASH)

            self.assertEqual(caught.exception.reason_code, reason_code)
            self.assertEqual(backup.calls, 0)
            self.assertEqual(caught.exception.attempts[0].outcome, "failed_closed")

    def test_real_codex_structured_capacity_event_still_uses_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            primary = CodexCliSemanticAdjudicator(
                executable=Path("/opt/codex"),
                runtime_tmp_root=Path(tmp),
            )
            backup = _Adapter(_identity("backup", provider="anthropic"))
            executor = OrderedSemanticAdjudicationExecutor(
                (
                    ConfiguredSemanticProvider(adapter=primary, cache=_Cache()),
                    _configured(backup),
                )
            )
            event = {
                "type": "error",
                "message": "API Error: 429 Too Many Requests",
            }
            with mock.patch(
                "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                return_value=subprocess.CompletedProcess(
                    ["codex"], 1, json.dumps(event), ""
                ),
            ):
                outcome = executor.adjudicate(_batch(), group_hash=_GROUP_HASH)

        self.assertEqual(
            tuple(item.outcome for item in outcome.attempts),
            ("availability_failed", "succeeded"),
        )
        self.assertEqual(backup.calls, 1)
        self.assertFalse(outcome.degraded_unavailable)

    def test_cancelled_and_unknown_failures_never_try_backup(self) -> None:
        for reason_code, retryable in (
            ("cancelled", True),
            ("command_failed", False),
            ("invalid_runtime_protocol", False),
            ("invalid_output_schema", False),
            ("forbidden_tool_call", False),
            ("model_identity_mismatch", False),
            ("invalid_contract", False),
            ("runtime_event_error", True),
            ("result_missing", True),
        ):
            with self.subTest(reason_code=reason_code):
                primary = _Adapter(
                    _identity("primary"),
                    reason_code=reason_code,
                    retryable=retryable,
                )
                backup = _Adapter(_identity("backup", provider="anthropic"))
                executor = OrderedSemanticAdjudicationExecutor(
                    (_configured(primary), _configured(backup))
                )

                with self.assertRaises(SemanticRouteAdjudicatorError) as caught:
                    executor.adjudicate(_batch(), group_hash=_GROUP_HASH)

                self.assertEqual(caught.exception.reason_code, reason_code)
                self.assertEqual(backup.calls, 0)
                self.assertEqual(len(caught.exception.attempts), 1)
                self.assertEqual(
                    caught.exception.attempts[0].outcome,
                    "cancelled" if reason_code == "cancelled" else "failed_closed",
                )

    def test_backup_cache_hit_avoids_backup_process(self) -> None:
        batch = _batch()
        primary = _Adapter(
            _identity("primary"), reason_code="transport_unavailable", retryable=True
        )
        backup = _Adapter(_identity("backup", provider="anthropic"))
        backup_cache = _Cache()
        cache_key = semantic_group_cache_key(
            identity=backup.provider_identity,
            taxonomy_version=batch.taxonomy.version,
            group_hash=_GROUP_HASH,
        )
        backup_cache.entries[cache_key] = SemanticAdjudicationCacheEntry(
            cache_key=cache_key,
            group_hash=_GROUP_HASH,
            provider=backup.provider_identity,
            decisions=_decisions(),
            response_sha256=_RESPONSE_HASH,
        )
        executor = OrderedSemanticAdjudicationExecutor(
            (_configured(primary), _configured(backup, backup_cache))
        )

        outcome = executor.adjudicate(batch, group_hash=_GROUP_HASH)

        self.assertEqual(outcome.attempts[1].outcome, "cache_hit")
        self.assertEqual(outcome.actual_result_identity, backup.provider_identity)
        self.assertEqual(backup.calls, 0)

    def test_cache_write_failure_is_visible_but_does_not_erase_result(self) -> None:
        adapter = _Adapter(_identity("primary"))
        executor = OrderedSemanticAdjudicationExecutor(
            (_configured(adapter, _Cache(fail_write=True)),)
        )

        outcome = executor.adjudicate(_batch(), group_hash=_GROUP_HASH)

        self.assertEqual(outcome.decisions, _decisions())
        self.assertEqual(outcome.attempts[0].outcome, "succeeded_cache_write_failed")

    def test_single_flight_reuses_first_result(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        adapter = _Adapter(_identity("primary"), entered=entered, release=release)
        executor = OrderedSemanticAdjudicationExecutor((_configured(adapter),))

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(executor.adjudicate, _batch(), group_hash=_GROUP_HASH)
            self.assertTrue(entered.wait(timeout=2))
            second = pool.submit(executor.adjudicate, _batch(), group_hash=_GROUP_HASH)
            release.set()
            first_result = first.result(timeout=5)
            second_result = second.result(timeout=5)

        self.assertEqual(adapter.calls, 1)
        self.assertEqual(first_result.attempts[0].outcome, "succeeded")
        self.assertEqual(second_result.attempts[0].outcome, "cache_hit")


if __name__ == "__main__":
    unittest.main()
