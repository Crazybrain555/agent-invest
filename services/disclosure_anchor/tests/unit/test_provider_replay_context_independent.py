"""Independent call-scope regression tests; synthetic admission, no runtime IO.

The eager differential oracle uses the existing non-context public replay API.
It changes only view reuse, not source, ownership, or route validation.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

from disclosure_anchor.application.contracts import provider_document_admission as admission_contract
from disclosure_anchor.application.contracts.semantic_routes import (
    SemanticDocumentContext, SemanticRouteDefinition, SemanticRouteTaxonomy,
)
from disclosure_anchor.application.services import provider_unit_builder as builder
from disclosure_anchor.application.services import semantic_router as routing
from tests._provider_source_semantics_fixture import (
    admit, block, cases, document, observation,
)
from tests.unit.test_provider_unit_builder import (
    _admitted, _identical_text_parts_document, _representative_document,
    _table_with_equal_caption_and_footnote,
)
from tests.unit.test_semantic_router import _Adjudicator, _MemoryCache


DOCUMENT = SemanticDocumentContext(title=None, filing_type="annual_report")


def long_admission(repairs=3):
    pages = []
    observations = []
    for page in range(12):
        items = [block(page * 10, page, 0, f"验证章节{page}", heading=True)]
        for order in range(1, 10):
            item = block(page * 10 + order, page, order, "营业收入万元，同比增长%。")
            items.append(item)
            if page < repairs and order == 1:
                observations.append(observation(item, "营业收入12万元，同比增长3%。"))
        pages.append(tuple(items))
    return admit(document(tuple(pages)), tuple(observations))


def router():
    def unexpected_model(batch):
        raise AssertionError("fixture must use deterministic/fallback routes without a model")

    return routing.SemanticRouter(
        taxonomy=SemanticRouteTaxonomy(
            version="independent-scope.v1",
            definitions=(SemanticRouteDefinition(
                key="independent_test_topic", description="unrelated topic",
                labels=("ZZZ独立未出现主题ZZZ",), scopes=("annual_report",),
            ),),
        ),
        adjudicator=_Adjudicator(unexpected_model), cache=_MemoryCache(),
    )


@contextmanager
def view_calls():
    with patch.object(
        admission_contract, "effective_provider_document",
        wraps=admission_contract.effective_provider_document,
    ) as calls:
        yield calls


@contextmanager
def eager_semantic_replay():
    """Restore pre-optimization per-binding builds while keeping the same kernel."""
    def eager_values(self, draft, binding):
        return builder.replay_provider_unit_search_binding(self.admitted, draft, binding)

    def eager_scalar(self, draft, binding):
        return builder.replay_provider_unit_search_binding_source_text(self.admitted, draft, binding)

    with patch.object(builder.ProviderUnitReplayContext, "replay_search_binding", eager_values), patch.object(
        builder.ProviderUnitReplayContext, "replay_search_binding_source_text", eager_scalar,
    ):
        yield


class ProviderReplayContextIndependentTests(unittest.TestCase):
    def test_long_repaired_binding_oracle_proves_repeated_builds_and_conservation(self):
        admitted = long_admission()
        self.assertEqual(len(admitted.source_text_reconciliations), 3)
        drafts = builder.build_provider_units(admitted).units
        bindings = [(draft, binding) for draft in drafts for binding in draft.locator.search_targets]
        self.assertGreater(len(bindings), 100)
        with view_calls() as eager_calls:
            expected = [(builder.replay_provider_unit_search_binding(admitted, draft, binding),
                         builder.replay_provider_unit_search_binding_source_text(admitted, draft, binding))
                        for draft, binding in bindings]
        self.assertEqual(eager_calls.call_count, 2 * len(bindings))
        with view_calls() as scoped_calls:
            context = builder.ProviderUnitReplayContext(admitted)
            actual = [(context.replay_search_binding(draft, binding),
                context.replay_search_binding_source_text(draft, binding)) for draft, binding in bindings]
        self.assertEqual(scoped_calls.call_count, 1)
        self.assertEqual(actual, expected)
        self.assertIn("营业收入12万元，同比增长3%。", [scalar for _, scalar in actual])
        self.assertEqual(admitted.provider_document.blocks[1].payloads[0].text,
                         "营业收入万元，同比增长%。")

    def test_route_and_replay_each_build_once_and_preserve_eager_results(self):
        admitted = long_admission()
        drafts = builder.build_provider_units(admitted).units
        with eager_semantic_replay():
            expected = router().route(admitted=admitted, document=DOCUMENT, drafts=drafts)
        service = router()
        for operation in ("route", "replay", "route", "replay"):
            with self.subTest(operation=operation), view_calls() as calls:
                kwargs = dict(admitted=admitted, document=DOCUMENT, drafts=drafts)
                if operation == "replay":
                    kwargs["receipts"] = expected.receipts
                actual = getattr(service, operation)(**kwargs)
                self.assertEqual(calls.call_count, 1)
                self.assertEqual(actual, expected)

    def test_shape_matrix_preserves_sources_and_receipts(self):
        admitted_cases = {name: admit(doc, observations) for name, (doc, observations) in cases().items()}
        admitted_cases.update({
            "title_only": admit(document(((block(0, 0, 0, "验证章节", heading=True),),))),
            "caption_equal_footnote": _admitted(_table_with_equal_caption_and_footnote()),
            "mixed_title_table_body_footnote": _admitted(_representative_document()),
            "long_no_repair": long_admission(0),
        })
        for name, admitted in admitted_cases.items():
            with self.subTest(shape=name):
                drafts = builder.build_provider_units(admitted).units
                before = deepcopy(drafts)
                with eager_semantic_replay():
                    expected_sources = tuple(routing._unit_sources(
                        admitted=admitted, document=DOCUMENT, draft=draft) for draft in drafts)
                    expected = router().route(admitted=admitted, document=DOCUMENT, drafts=drafts)
                context = builder.ProviderUnitReplayContext(admitted)
                actual_sources = tuple(routing._unit_sources(
                    admitted=admitted, document=DOCUMENT, draft=draft,
                    replay_context=context) for draft in drafts)
                self.assertEqual(actual_sources, expected_sources)
                with view_calls() as calls:
                    actual = router().route(admitted=admitted, document=DOCUMENT, drafts=drafts)
                self.assertLessEqual(calls.call_count, 1)
                self.assertEqual(actual, expected)
                with view_calls() as calls:
                    replayed = router().replay(admitted=admitted, document=DOCUMENT,
                                               drafts=drafts, receipts=actual.receipts)
                self.assertLessEqual(calls.call_count, 1)
                self.assertEqual(replayed, expected)
                self.assertEqual(drafts, before)

    def test_table_structured_scalar_and_atoms_share_the_view(self):
        admitted = _admitted(_representative_document())
        draft = next(draft for draft in builder.build_provider_units(admitted).units
                     if any(binding.source.field == "table_body" for binding in draft.locator.search_targets))
        original = builder.ProviderUnitReplayContext.replay_search_binding_source_text
        with patch.object(builder.ProviderUnitReplayContext, "replay_search_binding_source_text",
                          autospec=True, side_effect=original) as scalar, view_calls() as calls:
            sources = routing._unit_sources(admitted=admitted, document=DOCUMENT, draft=draft)
        self.assertTrue(sources)
        self.assertGreater(scalar.call_count, 0)
        self.assertEqual(calls.call_count, 1)

    def test_numbered_footnote_filter_keeps_owned_and_foreign_evidence_distinct(self):
        footnote = "<sup>2</sup>现金储备包含货币资金、交易性金融资产。"
        for owned in (True, False):
            with self.subTest(owned=owned):
                items = (
                    block(0, 0, 0, "现金储备", heading=True),
                    block(1, 0, 1, "现金储备超过731.5亿元<sup>2</sup>。"),
                    block(2, 0, 2, "其他指标<sup>2</sup>" if owned else "其他指标", heading=True),
                    block(3, 0, 3, "其他指标保持稳定。"),
                    block(4, 0, 4, footnote, kind="page_footnote"),
                )
                admitted = admit(document((items,)))
                drafts = builder.build_provider_units(admitted).units
                draft = next(draft for draft in drafts if draft.title.startswith("其他指标"))
                with eager_semantic_replay():
                    expected = routing._unit_sources(admitted=admitted, document=DOCUMENT, draft=draft)
                with view_calls() as calls:
                    actual = routing._unit_sources(admitted=admitted, document=DOCUMENT, draft=draft)
                self.assertEqual(calls.call_count, 1)
                self.assertEqual(actual, expected)
                self.assertEqual(any("交易性金融资产" in item.text for item in actual), owned)

    def test_context_rejects_untyped_or_subclass_substitutes(self):
        admitted = long_admission(0)
        draft = builder.build_provider_units(admitted).units[0]
        class DerivedContext(builder.ProviderUnitReplayContext):
            pass
        for context in (object(), DerivedContext(admitted)):
            with self.subTest(context=type(context).__name__):
                with self.assertRaisesRegex(TypeError, "exact provider replay context"):
                    routing._unit_sources(admitted=admitted, document=DOCUMENT, draft=draft,
                                          replay_context=context)

    def test_cancelled_route_preflight_builds_nothing_and_next_call_is_fresh(self):
        class Cancelled(RuntimeError):
            pass
        class Guard:
            def checkpoint(self):
                raise Cancelled("independent cancellation")
        admitted = long_admission()
        drafts = builder.build_provider_units(admitted).units
        service = router()
        with view_calls() as calls:
            with self.assertRaisesRegex(Cancelled, "independent cancellation"):
                service.route(admitted=admitted, document=DOCUMENT, drafts=drafts, stage_guard=Guard())
            self.assertEqual(calls.call_count, 0)
            service.route(admitted=admitted, document=DOCUMENT, drafts=drafts)
            self.assertEqual(calls.call_count, 1)

    def test_equal_but_separately_admitted_object_cannot_use_context(self):
        admitted = long_admission()
        other = long_admission()
        self.assertEqual(other, admitted)
        self.assertIsNot(other, admitted)
        draft = builder.build_provider_units(admitted).units[0]
        binding = draft.locator.search_targets[-1]
        context = builder.ProviderUnitReplayContext(admitted)
        with self.assertRaisesRegex(ValueError, "another admitted object"):
            routing._unit_sources(admitted=other, document=DOCUMENT, draft=draft,
                                  replay_context=context)
        self.assertEqual(builder.replay_provider_unit_search_binding(other, draft, binding),
                         context.replay_search_binding(draft, binding))
        self.assertEqual(builder.replay_provider_unit_search_binding_source_text(other, draft, binding),
                         context.replay_search_binding_source_text(draft, binding))

    def test_context_identity_cannot_be_rebound_to_a_different_semantic_view(self):
        repaired = long_admission()
        unrepaired = replace(repaired, source_text_reconciliations=())
        draft = builder.build_provider_units(repaired).units[0]
        binding = next(binding for binding in draft.locator.search_targets
                       if binding.source.source_index == 1)
        with self.assertRaisesRegex(ValueError, "differs from its source"):
            builder.replay_provider_unit_search_binding(unrepaired, draft, binding)
        context = builder.ProviderUnitReplayContext(repaired)
        try:
            context._admitted = unrepaired
        except (AttributeError, TypeError):
            self.assertIs(context.admitted, repaired)
            return
        # If ordinary assignment is supported, the consuming boundary must detect
        # that the stored repaired view was built for another admitted identity.
        with self.assertRaises(ValueError):
            routing._unit_sources(admitted=unrepaired, document=DOCUMENT, draft=draft,
                                  replay_context=context)

    def assert_rejected_with_and_without_context(self, admitted, draft, binding):
        context = builder.ProviderUnitReplayContext(admitted)
        for replay in (builder.replay_provider_unit_search_binding,
                       builder.replay_provider_unit_search_binding_source_text):
            with self.subTest(replay=replay.__name__):
                with self.assertRaises(ValueError) as eager:
                    replay(admitted, draft, binding)
                with self.assertRaises(type(eager.exception)) as scoped:
                    getattr(context, "replay_search_binding_source_text" if replay is
                            builder.replay_provider_unit_search_binding_source_text else
                            "replay_search_binding")(draft, binding)
                self.assertEqual(str(scoped.exception), str(eager.exception))

    def test_locator_hash_and_source_hash_drift_remain_rejected(self):
        admitted = _admitted(_identical_text_parts_document())
        draft = builder.build_provider_units(admitted).units[0]
        binding = draft.locator.search_targets[0]
        wrong_document = replace(draft, locator=replace(
            draft.locator, provider_document_sha256="sha256:" + "b" * 64))
        self.assert_rejected_with_and_without_context(admitted, wrong_document, binding)
        forged_binding = replace(binding, source=replace(
            binding.source, raw_block_sha256="sha256:" + "b" * 64))
        forged = replace(draft, locator=replace(draft.locator, search_targets=(
            forged_binding, *draft.locator.search_targets[1:])))
        self.assert_rejected_with_and_without_context(admitted, forged, forged_binding)

    def test_equal_text_does_not_authorize_another_owner_or_field(self):
        for doc in (_identical_text_parts_document(), _table_with_equal_caption_and_footnote()):
            admitted = _admitted(doc)
            draft = builder.build_provider_units(admitted).units[0]
            first, second = draft.locator.search_targets
            forged_binding = replace(first, destination=second.destination)
            forged = replace(draft, locator=replace(
                draft.locator, search_targets=(forged_binding, second)))
            self.assert_rejected_with_and_without_context(admitted, forged, forged_binding)

    def test_valid_replay_does_not_memoize_later_destination_tampering(self):
        admitted = _admitted(_identical_text_parts_document())
        draft = builder.build_provider_units(admitted).units[0]
        binding = draft.locator.search_targets[0]
        context = builder.ProviderUnitReplayContext(admitted)
        expected = context.replay_search_binding(draft, binding)
        payload = deepcopy(draft.payload)
        payload["parts"][0]["text"] = "伪造正文"
        tampered = replace(draft, payload=payload)
        for replay in (builder.replay_provider_unit_search_binding,
                       builder.replay_provider_unit_search_binding_source_text):
            with self.assertRaisesRegex(ValueError, "differs from its source"):
                getattr(context, "replay_search_binding_source_text" if replay is
                        builder.replay_provider_unit_search_binding_source_text else
                        "replay_search_binding")(tampered, binding)
        self.assertEqual(context.replay_search_binding(draft, binding), expected)


if __name__ == "__main__":
    unittest.main()
