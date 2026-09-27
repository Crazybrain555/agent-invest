from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
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
    SemanticDecisionCoverageError,
    SemanticDocumentContext,
    SemanticProviderIdentity,
    SemanticRouteCandidate,
    SemanticRouteContractError,
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
from disclosure_anchor.adapters.semantics.runtime import build_semantic_runtime
from disclosure_anchor.settings import Settings
from tests.unit._codex_model_catalog_fixture import neutral_catalog, prepare_catalog_sha256
from tests.unit.test_semantic_codex_cli import (
    EXHAUSTED_STREAM,
    LIVE_USAGE_LIMIT_STDOUT,
    MODELS_REFRESH_STDERR,
    USAGE_LIMIT_MESSAGE,
    USAGE_LIMIT_STDOUT,
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
        cases = (
            (
                "",
                "API Error: 429 Too Many Requests\nfatal protocol parser crashed",
                "command_failed",
            ),
            # A usage limit reported next to the code-mode notice proves the
            # tool-free catalog was not honored; that is never availability.
            (LIVE_USAGE_LIMIT_STDOUT, MODELS_REFRESH_STDERR, "invalid_runtime_protocol"),
        )
        for stdout, stderr, reason_code in cases:
            with self.subTest(reason_code=reason_code), tempfile.TemporaryDirectory() as tmp:
                primary = CodexCliSemanticAdjudicator(
                    executable=Path("/opt/codex"),
                    runtime_tmp_root=Path(tmp),
                    model_catalog=neutral_catalog(),
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
                        return_value=subprocess.CompletedProcess(["codex"], 1, stdout, stderr),
                    ),
                    self.assertRaises(SemanticRouteAdjudicatorError) as caught,
                ):
                    executor.adjudicate(_batch(), group_hash=_GROUP_HASH)

            self.assertEqual(caught.exception.reason_code, reason_code)
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
        streams = (
            (json.dumps({"type": "error", "message": "API Error: 429 Too Many Requests"}), ""),
            (USAGE_LIMIT_STDOUT, MODELS_REFRESH_STDERR),
            # Codex exhausted its own bounded stream retries: a transport outage.
            (
                EXHAUSTED_STREAM,
                "2026-09-24T03:26:31.194978Z ERROR codex_api::endpoint::responses_websocket: "
                "failed to connect to websocket: WebSocket protocol error: HTTP version must "
                "be 1.1 or higher, url: ws://127.0.0.1:55957/v1/responses\n",
            ),
            ("\n".join(
                (
                    json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                    json.dumps({"type": "turn.started"}),
                    json.dumps({"type": "error", "message": USAGE_LIMIT_MESSAGE}),
                    json.dumps(
                        {
                            "type": "turn.failed",
                            "error": {"message": USAGE_LIMIT_MESSAGE},
                        }
                    ),
                )
            ), ""),
        )
        for stdout, stderr in streams:
            with self.subTest(stdout=stdout, stderr=stderr), tempfile.TemporaryDirectory() as tmp:
                primary = CodexCliSemanticAdjudicator(
                    executable=Path("/opt/codex"),
                    runtime_tmp_root=Path(tmp),
                    model_catalog=neutral_catalog(),
                )
                backup = _Adapter(_identity("backup", provider="anthropic"))
                executor = OrderedSemanticAdjudicationExecutor(
                    (
                        ConfiguredSemanticProvider(adapter=primary, cache=_Cache()),
                        _configured(backup),
                    )
                )
                with mock.patch(
                    "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                    return_value=subprocess.CompletedProcess(["codex"], 1, stdout, stderr),
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

    def test_rejected_answers_keep_truthful_lineage_and_are_never_cached(self) -> None:
        def reject(_decisions: tuple[SemanticAdjudicationDecision, ...]) -> None:
            raise SemanticRouteContractError("semantic model removed an exact title route")

        def run(executor, validate):  # type: ignore[no-untyped-def]
            stages: list[tuple[str, dict[str, object]]] = []
            with mock.patch(
                "disclosure_anchor.application.services.semantic_adjudication.note_stage",
                side_effect=lambda _guard, stage, **fields: stages.append((stage, fields)),
            ):
                try:
                    executor.adjudicate(_batch(), group_hash=_GROUP_HASH, validate=validate)
                except BaseException as exc:  # noqa: BLE001 - asserted by each case
                    return exc, stages
            self.fail("a rejected answer must not become an outcome")

        with self.subTest(case="backup answer rejected after primary outage"):
            primary = _Adapter(
                _identity("primary"), reason_code="capacity_unavailable", retryable=True
            )
            backup = _Adapter(_identity("backup", provider="anthropic"))
            primary_cache, backup_cache = _Cache(), _Cache()
            executor = OrderedSemanticAdjudicationExecutor(
                (_configured(primary, primary_cache), _configured(backup, backup_cache))
            )
            error, stages = run(executor, reject)

            assert isinstance(error, SemanticRouteAdjudicatorError)
            self.assertEqual((error.reason_code, error.retryable), ("invalid_decision", False))
            self.assertEqual(
                [
                    (item.ordinal, item.provider.provider_id, item.outcome, item.reason_code)
                    for item in error.attempts
                ],
                [
                    (1, "primary", "availability_failed", "capacity_unavailable"),
                    (2, "backup", "failed_closed", "invalid_decision"),
                ],
            )
            self.assertEqual(
                error.attempts[1].cache_key,
                semantic_group_cache_key(
                    identity=backup.provider_identity,
                    taxonomy_version=_batch().taxonomy.version,
                    group_hash=_GROUP_HASH,
                ),
            )
            self.assertEqual((primary.calls, backup.calls), (1, 1))
            self.assertEqual((primary_cache.entries, backup_cache.entries), ({}, {}))
            self.assertEqual(
                [(stage, fields.get("outcome")) for stage, fields in stages],
                [
                    ("group_started", None),
                    ("provider_call_started", None),
                    ("provider_call_ended", "availability_failed"),
                    ("provider_call_started", None),
                    ("provider_call_ended", "failed_closed"),
                    ("group_ended", "failed_closed"),
                ],
            )

        with self.subTest(case="stored entry rejected without a provider call"):
            adapter = _Adapter(_identity("primary"))
            cache = _Cache()
            cache_key = semantic_group_cache_key(
                identity=adapter.provider_identity,
                taxonomy_version=_batch().taxonomy.version,
                group_hash=_GROUP_HASH,
            )
            entry = SemanticAdjudicationCacheEntry(
                cache_key=cache_key,
                group_hash=_GROUP_HASH,
                provider=adapter.provider_identity,
                decisions=_decisions(),
                response_sha256=_RESPONSE_HASH,
            )
            cache.entries[cache_key] = entry
            executor = OrderedSemanticAdjudicationExecutor((_configured(adapter, cache),))
            error, stages = run(executor, reject)

            assert isinstance(error, SemanticRouteAdjudicatorError)
            self.assertEqual(error.reason_code, "invalid_decision")
            self.assertIn("cache entry failed routing validation", str(error))
            self.assertEqual(
                [(item.outcome, item.reason_code, item.cache_key) for item in error.attempts],
                [("failed_closed", "invalid_decision", cache_key)],
            )
            self.assertEqual(adapter.calls, 0)
            self.assertIs(cache.entries[cache_key], entry)
            self.assertEqual(
                [stage for stage, _fields in stages],
                ["group_started", "cache_hit_invalid", "group_ended"],
            )

        with self.subTest(case="wrong Unit coverage keeps the result-contract class"):
            adapter = _Adapter(_identity("primary"))
            cache = _Cache()
            executor = OrderedSemanticAdjudicationExecutor((_configured(adapter, cache),))

            def uncovered(_decisions: tuple[SemanticAdjudicationDecision, ...]) -> None:
                raise SemanticDecisionCoverageError("semantic decisions do not cover the exact requested Units")

            error, stages = run(executor, uncovered)

            assert isinstance(error, SemanticRouteAdjudicatorError)
            self.assertEqual((error.reason_code, error.retryable), ("invalid_contract", False))
            self.assertEqual(
                [(item.outcome, item.reason_code) for item in error.attempts],
                [("failed_closed", "invalid_contract")],
            )
            self.assertEqual(cache.entries, {})
            self.assertIn(
                ("provider_call_ended", "failed_closed", "invalid_contract"),
                [(stage, fields.get("outcome"), fields.get("reason_code")) for stage, fields in stages],
            )

        with self.subTest(case="an unexpected validator failure stays visible"):
            adapter = _Adapter(_identity("primary"))
            cache = _Cache()
            executor = OrderedSemanticAdjudicationExecutor((_configured(adapter, cache),))

            def broken(_decisions: tuple[SemanticAdjudicationDecision, ...]) -> None:
                raise LookupError("validator defect")

            error, stages = run(executor, broken)

            self.assertIs(type(error), LookupError)
            self.assertEqual(cache.entries, {})
            self.assertEqual(
                [(stage, fields.get("outcome")) for stage, fields in stages][-2:],
                [
                    ("provider_call_ended", "error:LookupError"),
                    ("group_ended", "error:LookupError"),
                ],
            )

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

    def test_missing_codex_executable_fails_over_under_its_catalog_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            primary = CodexCliSemanticAdjudicator(
                executable=Path(tmp) / "absent" / "codex",
                runtime_tmp_root=Path(tmp) / "semantic",
                model_catalog=neutral_catalog(),
            )
            backup = _Adapter(_identity("backup", provider="anthropic"))
            executor = OrderedSemanticAdjudicationExecutor(
                (
                    ConfiguredSemanticProvider(adapter=primary, cache=_Cache()),
                    _configured(backup),
                )
            )
            outcome = executor.adjudicate(_batch(), group_hash=_GROUP_HASH)

        first, second = outcome.attempts
        self.assertEqual(
            (first.outcome, first.reason_code, second.outcome),
            ("availability_failed", "executable_unavailable", "succeeded"),
        )
        self.assertEqual(first.provider, primary.provider_identity)
        self.assertIn("+catalog.", first.provider.adapter_version)
        self.assertEqual(
            first.cache_key,
            semantic_group_cache_key(
                identity=primary.provider_identity,
                taxonomy_version=_batch().taxonomy.version,
                group_hash=_GROUP_HASH,
            ),
        )
        self.assertEqual(backup.calls, 1)
        self.assertEqual(outcome.actual_result_identity, backup.provider_identity)

    def test_runtime_composition_needs_the_prepared_catalog_not_the_executable(self) -> None:
        ambient = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("DISCLOSURE_SEMANTIC_", "DATABASE_URL", "DISCLOSURE_RUNTIME_ROOT"))
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, ambient, clear=True),
        ):
            root = Path(tmp)
            runtime_root = root / "runtime"
            absent_codex = root / "absent" / "codex"

            def settings(**semantic: object) -> Settings:
                return Settings(
                    disclosure_data_root=root / "data",
                    disclosure_shared_root=root / "shared",
                    disclosure_runtime_root=runtime_root,
                    mineru_model_cache=root / "shared" / "mineru",
                    hf_home=root / "shared" / "hf",
                    modelscope_cache=root / "shared" / "modelscope",
                    disclosure_semantic_codex_bin=absent_codex,
                    **semantic,
                )

            def compose(value: Settings):  # type: ignore[no-untyped-def]
                return build_semantic_runtime(
                    settings=value, paths=mock.Mock(), artifacts=mock.Mock()
                )

            # No configured hash, or a hash whose prepared file is absent, stops
            # startup before any provider exists.
            with self.assertRaises(ValueError):
                compose(settings())
            with self.assertRaisesRegex(ValueError, "prepare it before startup"):
                compose(
                    settings(
                        disclosure_semantic_codex_model_catalog_sha256=neutral_catalog().sha256
                    )
                )
            sha256 = prepare_catalog_sha256(runtime_root)
            runtime = compose(settings(disclosure_semantic_codex_model_catalog_sha256=sha256))
            identities = runtime.router.executor.provider_identities
            # Composition never touched (or created) the configured executable.
            self.assertFalse(absent_codex.parent.exists())

        self.assertEqual(
            tuple(item.provider_id for item in identities), ("luna-primary", "sonnet-backup")
        )
        self.assertEqual(
            identities[0].adapter_version,
            "codex_cli.v7+catalog." + sha256.removeprefix("sha256:"),
        )
        self.assertNotIn("catalog", identities[1].adapter_version)


if __name__ == "__main__":
    unittest.main()
