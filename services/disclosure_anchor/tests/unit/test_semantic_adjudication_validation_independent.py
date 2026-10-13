"""Independent business-validation checks at semantic cache boundaries."""

from __future__ import annotations

from dataclasses import replace
import unittest
from unittest import mock

from disclosure_anchor.application.contracts.semantic_routes import (
    SemanticAdjudicatedRoute,
    SemanticAdjudicationDecision,
    SemanticDocumentContext,
)
from disclosure_anchor.application.ports.semantic_routes import (
    SemanticProviderResult,
    SemanticRouteAdjudicatorError,
)
from disclosure_anchor.application.services.semantic_adjudication import (
    ConfiguredSemanticProvider,
    OrderedSemanticAdjudicationExecutor,
)
from disclosure_anchor.application.services.semantic_router import SemanticRouter
from tests.unit.test_semantic_adjudication import (
    _Adapter,
    _Cache,
    _GROUP_HASH,
    _RESPONSE_HASH,
    _batch,
    _configured,
    _decisions,
    _identity,
)
from tests.unit.test_semantic_router import _drafts_with_body, _taxonomy


class _ForecastAdapter(_Adapter):
    """Return a protocol-valid answer with selectable business witness quality."""

    def __init__(self, *, invalid_witness: bool) -> None:
        super().__init__(_identity("primary"))
        self.invalid_witness = invalid_witness

    def adjudicate_with_result(self, batch):  # type: ignore[no-untyped-def]
        self.calls += 1
        decisions = []
        for unit in batch.units:
            candidate = next(
                item for item in unit.candidates
                if item.key == "performance_forecast_summary"
            )
            decisions.append(SemanticAdjudicationDecision(
                unit_index=unit.unit_index,
                routes=(SemanticAdjudicatedRoute(
                    key=candidate.key,
                    support_ids=(
                        ("u999:title",)
                        if self.invalid_witness
                        else (f"u{unit.unit_index}:title",)
                    ),
                ),),
            ))
        return SemanticProviderResult(
            decisions=tuple(decisions), response_sha256=_RESPONSE_HASH
        )


def _forecast_input():  # type: ignore[no-untyped-def]
    admitted, drafts = _drafts_with_body(
        "业绩预告和预计业绩区间", "业绩变动原因"
    )
    context = SemanticDocumentContext(
        title="某公司业绩预告", filing_type="performance_forecast"
    )
    return admitted, drafts, context


class SemanticAdjudicationValidationIndependentTests(unittest.TestCase):
    def test_missing_group_coverage_keeps_protocol_failure_class(self) -> None:
        admitted, drafts, context = _forecast_input()

        class BrokenCoverageAdapter(_ForecastAdapter):
            def __init__(self, mode: str) -> None:
                super().__init__(invalid_witness=False)
                self.mode = mode

            def adjudicate_with_result(self, batch):  # type: ignore[no-untyped-def]
                self.calls += 1
                requested_index = batch.units[0].unit_index
                decision = SemanticAdjudicationDecision(
                    unit_index=requested_index, routes=()
                )
                decisions = {
                    "missing": (),
                    "duplicate": (decision, decision),
                    "foreign": (
                        replace(decision, unit_index=requested_index + 999),
                    ),
                }[self.mode]
                return SemanticProviderResult(
                    decisions=decisions, response_sha256=_RESPONSE_HASH
                )

        for mode in ("missing", "duplicate", "foreign"):
            with self.subTest(mode=mode):
                primary = BrokenCoverageAdapter(mode)
                backup = _Adapter(_identity("backup", provider="anthropic"))
                cache = _Cache()
                executor = OrderedSemanticAdjudicationExecutor((
                    ConfiguredSemanticProvider(adapter=primary, cache=cache),
                    _configured(backup),
                ))
                with self.assertRaises(SemanticRouteAdjudicatorError) as caught:
                    SemanticRouter(taxonomy=_taxonomy(), executor=executor).route(
                        admitted=admitted, document=context, drafts=drafts
                    )

                self.assertEqual(caught.exception.reason_code, "invalid_contract")
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(caught.exception.attempts[0].outcome, "failed_closed")
                self.assertEqual(cache.entries, {})
                self.assertEqual(primary.calls, 1)
                self.assertEqual(backup.calls, 0)

    def test_router_invalid_fresh_answer_is_failed_closed_before_cache_write(self) -> None:
        admitted, drafts, context = _forecast_input()
        primary = _ForecastAdapter(invalid_witness=True)
        backup = _Adapter(_identity("backup", provider="anthropic"))
        cache = _Cache()
        stages = []
        executor = OrderedSemanticAdjudicationExecutor((
            ConfiguredSemanticProvider(adapter=primary, cache=cache),
            _configured(backup),
        ))
        with (
            mock.patch(
                "disclosure_anchor.application.services.semantic_adjudication.note_stage",
                side_effect=lambda _guard, stage, **fields: stages.append((stage, fields)),
            ),
            self.assertRaises(SemanticRouteAdjudicatorError) as caught,
        ):
            SemanticRouter(taxonomy=_taxonomy(), executor=executor).route(
                admitted=admitted, document=context, drafts=drafts
            )

        error = caught.exception
        self.assertEqual(error.reason_code, "invalid_decision")
        self.assertFalse(error.retryable)
        self.assertEqual(len(error.attempts), 1)
        self.assertEqual(error.attempts[0].outcome, "failed_closed")
        self.assertEqual(error.attempts[0].reason_code, "invalid_decision")
        self.assertIsNotNone(error.attempts[0].cache_key)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(backup.calls, 0)
        self.assertEqual(cache.entries, {})
        self.assertTrue(any(
            stage == "provider_call_ended"
            and fields.get("outcome") == "failed_closed"
            and fields.get("reason_code") == "invalid_decision"
            for stage, fields in stages
        ))

    def test_router_invalid_cached_answer_preserves_entry_without_provider_call(self) -> None:
        admitted, drafts, context = _forecast_input()
        primary = _ForecastAdapter(invalid_witness=False)
        backup = _Adapter(_identity("backup", provider="anthropic"))
        cache = _Cache()
        executor = OrderedSemanticAdjudicationExecutor((
            ConfiguredSemanticProvider(adapter=primary, cache=cache),
            _configured(backup),
        ))
        router = SemanticRouter(taxonomy=_taxonomy(), executor=executor)
        first = router.route(admitted=admitted, document=context, drafts=drafts)
        self.assertEqual(first.receipts[0].decision_source, "model")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(len(cache.entries), 1)
        cache_key, valid_entry = next(iter(cache.entries.items()))
        valid_route = valid_entry.decisions[0].routes[0]
        invalid_entry = replace(
            valid_entry,
            decisions=(SemanticAdjudicationDecision(
                unit_index=valid_entry.decisions[0].unit_index,
                routes=(replace(valid_route, support_ids=("u999:title",)),),
            ),),
        )
        cache.entries[cache_key] = invalid_entry
        stages = []
        with (
            mock.patch(
                "disclosure_anchor.application.services.semantic_adjudication.note_stage",
                side_effect=lambda _guard, stage, **fields: stages.append((stage, fields)),
            ),
            self.assertRaises(SemanticRouteAdjudicatorError) as caught,
        ):
            router.route(admitted=admitted, document=context, drafts=drafts)

        error = caught.exception
        self.assertEqual(error.reason_code, "invalid_decision")
        self.assertFalse(error.retryable)
        self.assertEqual(len(error.attempts), 1)
        self.assertEqual(error.attempts[0].outcome, "failed_closed")
        self.assertEqual(error.attempts[0].cache_key, cache_key)
        self.assertEqual(cache.entries, {cache_key: invalid_entry})
        self.assertEqual(primary.calls, 1)
        self.assertEqual(backup.calls, 0)
        self.assertFalse(any(
            stage in {"provider_call_started", "provider_call_ended"}
            for stage, _fields in stages
        ))
        self.assertTrue(any(
            stage == "group_ended" and fields.get("outcome") == "failed_closed"
            for stage, fields in stages
        ))

    def test_valid_business_decision_is_checked_before_put_and_again_on_hit(self) -> None:
        events = []

        class RecordingCache(_Cache):
            def put(self, entry):  # type: ignore[no-untyped-def]
                events.append("put")
                super().put(entry)

        def validate(decisions):  # type: ignore[no-untyped-def]
            self.assertEqual(decisions, _decisions())
            events.append("validate")

        primary = _Adapter(_identity("primary"))
        cache = RecordingCache()
        executor = OrderedSemanticAdjudicationExecutor((
            ConfiguredSemanticProvider(adapter=primary, cache=cache),
        ))
        first = executor.adjudicate(_batch(), group_hash=_GROUP_HASH, validate=validate)
        self.assertEqual(first.attempts[0].outcome, "succeeded")
        self.assertEqual(events, ["validate", "put"])
        second = executor.adjudicate(_batch(), group_hash=_GROUP_HASH, validate=validate)
        self.assertEqual(second.attempts[0].outcome, "cache_hit")
        self.assertEqual(events, ["validate", "put", "validate"])
        self.assertEqual(primary.calls, 1)
        self.assertEqual(len(cache.entries), 1)


if __name__ == "__main__":
    unittest.main()
