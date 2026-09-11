"""Independently authored M6 contract tests (WP-B closed models and qualification).

Expectations come from docs/implementation/design/m6-run-contract.md, not from
the model code. Synthetic values only; none of these cases certifies a PDF,
a host, or an hour.
"""

from __future__ import annotations

import hashlib
import json
import unittest

from disclosure_anchor.application.contracts.m6_campaign import (
    M6_MAX_CORPUS_ENTRIES, M6CampaignScope, M6CorpusEntry, M6CorpusManifest,
)
from disclosure_anchor.application.contracts.m6_document_qualification import (
    M6CheckResult, M6DocumentQualification, M6QualificationEvidence, M6QualityPlan, qualify_document,
)
from disclosure_anchor.application.contracts.m6_owner import M6OwnerAnchor
from disclosure_anchor.application.contracts.m6_run import (
    M6ClockDomain, M6PublicationMetrics, M6RunReceipt, M6RunSpec, M6ServiceMetrics, M6SourceHistoryFact,
)
from disclosure_anchor.application.contracts.m6_run_events import M6OwnerStamp, M6RunEvent
from disclosure_anchor.application.ports.staged_new_work_v4 import validate_admission_document_ids

from tests import m6_support as m6


def _canonical(document: object) -> bytes:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class CampaignMembershipTests(unittest.TestCase):
    def test_campaign_above_eight_is_explicit_immutable_and_legacy_bound_remains(self) -> None:
        entries = [m6.entry("doc-%02d" % index, pages=index + 1, mode="e2e_publication") for index in range(9)]
        corpus = m6.manifest("e2e_publication", *entries)
        scope = M6CampaignScope.from_manifest(corpus)

        self.assertEqual(len(scope.document_ids), 9, "a nine-document campaign is an ordinary explicit scope")
        self.assertEqual(scope.document_ids, tuple(sorted(scope.document_ids)))
        self.assertEqual(scope.manifest_sha256, corpus.canonical_sha256())
        self.assertIsInstance(scope.document_ids, tuple)
        scope.verify_manifest(corpus)

        other = m6.manifest("e2e_publication", *entries[:8])
        with self.assertRaises(ValueError):
            scope.verify_manifest(other)
        with self.assertRaises(ValueError):
            M6CampaignScope.from_manifest(m6.manifest("service_diagnostic", m6.entry("s", pages=1, mode="service_diagnostic")))
        with self.assertRaises(ValueError):
            M6CampaignScope(campaign_id="c", manifest_sha256=corpus.canonical_sha256(),
                            document_ids=("doc-b", "doc-a"))
        with self.assertRaises(ValueError):
            M6CampaignScope(campaign_id="c", manifest_sha256=corpus.canonical_sha256(),
                            document_ids=("doc-a", "doc-a"))
        with self.assertRaises(ValueError):
            M6CampaignScope(campaign_id="c", manifest_sha256=corpus.canonical_sha256(), document_ids=())
        with self.assertRaises(ValueError):
            scope.document_ids = ("doc-z",)  # type: ignore[misc]

        # The engineering ceiling is a wire/memory bound, not a campaign size.
        self.assertEqual(M6_MAX_CORPUS_ENTRIES, 10_000)
        too_many = tuple("id-%05d" % index for index in range(M6_MAX_CORPUS_ENTRIES + 1))
        with self.assertRaises(ValueError):
            M6CampaignScope(campaign_id="c", manifest_sha256=corpus.canonical_sha256(), document_ids=too_many)

        # The separate legacy commissioning path retains its own exact bound.
        validate_admission_document_ids(scope.document_ids[:8])
        with self.assertRaises(ValueError):
            validate_admission_document_ids(scope.document_ids)

    def test_manifest_rejects_duplicate_source_document_and_noncanonical_order(self) -> None:
        first = m6.entry("a", pages=3, mode="e2e_publication")
        second = m6.entry("b", pages=4, mode="e2e_publication")
        ordered = tuple(sorted((first, second), key=lambda item: item.source_pdf_sha256))
        M6CorpusManifest(campaign_id="c", mode="e2e_publication", entries=ordered)

        with self.assertRaises(ValueError):
            M6CorpusManifest(campaign_id="c", mode="e2e_publication", entries=ordered[::-1])
        with self.assertRaises(ValueError):
            M6CorpusManifest(campaign_id="c", mode="e2e_publication", entries=(first, first))
        duplicate_document = second.model_copy(update={"document_id": first.document_id})
        with self.assertRaises(ValueError):
            M6CorpusManifest(campaign_id="c", mode="e2e_publication", entries=tuple(sorted(
                (first, duplicate_document), key=lambda item: item.source_pdf_sha256)))
        with self.assertRaises(ValueError):
            M6CorpusManifest(campaign_id="c", mode="e2e_publication",
                             entries=(first.model_copy(update={"document_id": None}),))
        with self.assertRaises(ValueError):
            M6CorpusManifest(campaign_id="c", mode="service_diagnostic", entries=(first,))
        with self.assertRaises(ValueError):
            M6CorpusManifest(campaign_id="c", mode="e2e_publication", entries=())
        for bad_id in (" doc", "doc ", "doc\tx", "x" * 65, ""):
            with self.assertRaises(ValueError, msg=repr(bad_id)):
                M6CorpusEntry(source_pdf_sha256=m6.digest("x"), source_byte_count=1, source_page_count=1,
                              document_id=bad_id, stratum="s", origin="fresh")
        service = m6.manifest("service_diagnostic", m6.entry("s1", pages=2, mode="service_diagnostic", byte_count=10),
                              m6.entry("s2", pages=3, mode="service_diagnostic", byte_count=20))
        self.assertEqual(service.source_bytes, 30)


class CanonicalWireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = m6.make_fixture("e2e_publication", {"a": (5, "fresh")})
        self.spec = self.fixture.spec
        self.payload = self.spec.canonical_bytes()

    def test_canonical_roundtrip_and_missing_unknown_duplicate_and_coerced_fields(self) -> None:
        restored = M6RunSpec.from_canonical_bytes(self.payload, maximum_bytes=len(self.payload))
        self.assertEqual(restored, self.spec)
        self.assertEqual(restored.canonical_bytes(), self.payload)
        self.assertEqual(self.spec.canonical_sha256(), "sha256:" + hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(self.payload, _canonical(json.loads(self.payload)), "exact sorted compact UTF-8")
        self.assertNotIn(b"\n", self.payload)

        document = json.loads(self.payload)
        variants: dict[str, bytes] = {
            "indented": json.dumps(document, indent=1, sort_keys=True).encode(),
            "unsorted": json.dumps(dict(reversed(list(document.items()))), ensure_ascii=False,
                                   separators=(",", ":")).encode("utf-8"),
            "trailing_newline": self.payload + b"\n",
            "unknown_field": _canonical({**document, "extra": 1}),
            "missing_field": _canonical({key: value for key, value in document.items() if key != "run_id"}),
            "duplicate_key": self.payload[:-1] + b',"run_id":"run-1"}',
            "coerced_int": _canonical({**document, "planned_seconds": str(document["planned_seconds"])}),
            "coerced_float": _canonical({**document, "t0_ticks": float(document["t0_ticks"])}),
            "coerced_bool": _canonical({**document, "planned_seconds": True}),
            "list_field": _canonical({**document, "carry_in_attempt_ids": None}),
            "escaped_equivalent": self.payload.replace(b'"run-1"', b'"run-\\u0031"', 1),
            "spaced_separators": json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            "empty": b"",
        }
        for label, wire in variants.items():
            with self.assertRaises(ValueError, msg=label):
                M6RunSpec.from_canonical_bytes(wire, maximum_bytes=65536)
        with self.assertRaises(ValueError):
            M6RunSpec.from_canonical_bytes(self.payload, maximum_bytes=len(self.payload) - 1)
        with self.assertRaises(ValueError):
            M6RunSpec.from_canonical_bytes(self.payload, maximum_bytes=0)

        # Every field participates in the hash; there is no recursive self-hash field.
        renamed = self.spec.model_copy(update={"run_id": "run-2"})
        self.assertNotEqual(renamed.canonical_sha256(), self.spec.canonical_sha256())
        self.assertNotIn("spec_sha256", document)
        self.assertNotIn("self_sha256", document)

    def test_short_batch_is_not_a_formal_hour_and_clock_cannot_drift(self) -> None:
        corpus, plan = self.fixture.manifest, self.fixture.plan
        short = m6.run_spec(manifest=corpus, plan=plan, phase="short_batch", planned_seconds=30)
        self.assertEqual(short.deadline_ticks, short.t0_ticks + 30 * m6.QPC_HZ)
        m6.run_spec(manifest=corpus, plan=plan, phase="hour_baseline", planned_seconds=3600)
        m6.run_spec(manifest=corpus, plan=plan, phase="recovery_experiment", planned_seconds=45)

        for phase in ("hour_baseline", "stability_repeat"):
            with self.assertRaises(ValueError, msg=phase):
                m6.run_spec(manifest=corpus, plan=plan, phase=phase, planned_seconds=3599)

        with self.assertRaises(ValueError):
            M6RunSpec.model_validate({**short.model_dump(), "deadline_ticks": short.deadline_ticks + 1})
        with self.assertRaises(ValueError):
            M6RunSpec.model_validate({**short.model_dump(), "deadline_ticks": short.deadline_ticks - 1})
        with self.assertRaises(ValueError):
            M6RunSpec.model_validate({**short.model_dump(), "max_close_ticks": short.deadline_ticks - 1})
        with self.assertRaises(ValueError):
            M6RunSpec.model_validate({**short.model_dump(), "carry_in_attempt_ids": ("b", "a")})
        with self.assertRaises(ValueError):
            M6RunSpec.model_validate({**short.model_dump(), "carry_in_attempt_ids": ("a", "a")})
        with self.assertRaises(ValueError):
            M6RunSpec.model_validate({**short.model_dump(), "carry_in_attempt_ids": tuple(
                "carry-%03d" % index for index in range(short.resources.max_attempts + 1))})
        with self.assertRaises(ValueError):
            M6RunSpec.model_validate({**short.model_dump(), "scope_sha256": None})
        with self.assertRaises(ValueError):
            M6RunSpec.model_validate({**short.model_dump(), "runtime": {
                **short.runtime.model_dump(), "worker_profile_sha256": None}})
        service = m6.make_fixture("service_diagnostic", {"s": (2, "fresh")}).spec
        with self.assertRaises(ValueError):
            M6RunSpec.model_validate({**service.model_dump(), "scope_sha256": m6.digest("scope")})

        # The clock domain identity binds boot and frequency; a changed frequency is a new domain.
        clock = short.clock
        with self.assertRaises(ValueError):
            M6ClockDomain.model_validate({**clock.model_dump(), "qpc_frequency_hz": clock.qpc_frequency_hz * 2})
        with self.assertRaises(ValueError):
            M6ClockDomain.model_validate({**clock.model_dump(), "boot_identity_sha256": m6.digest("boot:other")})
        with self.assertRaises(ValueError):
            M6OwnerAnchor(run_id="r", clock=clock, owner_process_epoch_sha256=m6.OWNER_EPOCH,
                          owner_source_sha256=m6.digest("o"), gpu_device_identity_sha256=m6.digest("g"),
                          t0_ticks=10, planned_seconds=1, deadline_ticks=10 + clock.qpc_frequency_hz + 1,
                          max_close_ticks=10 + clock.qpc_frequency_hz + 1, resources=short.resources)


class QualityContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = m6.entry("q", pages=9, mode="e2e_publication")
        self.service_source = m6.entry("q", pages=9, mode="service_diagnostic")
        self.plan = m6.quality_plan("e2e_publication")
        self.service_plan = m6.quality_plan("service_diagnostic")

    def test_quality_requires_every_structural_check_and_whole_document(self) -> None:
        for omitted in m6.SERVICE_CHECKS:
            with self.assertRaises(ValueError, msg=omitted):
                M6QualityPlan(mode="service_diagnostic", reason_policies=(),
                              required_checks=tuple(check for check in m6.SERVICE_CHECKS if check != omitted))
        with self.assertRaises(ValueError):
            M6QualityPlan(mode="e2e_publication", reason_policies=(), required_checks=m6.SERVICE_CHECKS)
        with self.assertRaises(ValueError):
            M6QualityPlan(mode="e2e_publication", reason_policies=(), required_checks=m6.E2E_CHECKS[::-1])
        with self.assertRaises(ValueError):
            M6QualityPlan(mode="service_diagnostic", reason_policies=(), required_checks=m6.SERVICE_CHECKS + ("page_closure",))
        with self.assertRaises(ValueError):
            m6.quality_plan("e2e_publication", ("r", "review_required"), ("r", "accepted_noncritical"))

        good = m6.qualification_for(self.source, "e2e_publication", "att")
        verdict = qualify_document(good, self.plan)
        self.assertEqual(verdict.verdict, "scorable")
        self.assertEqual(verdict.scorable_page_count, 9, "credit is the whole source page count")
        self.assertEqual(verdict.reasons, ())
        self.assertEqual(verdict.evidence_sha256, good.canonical_sha256())
        self.assertEqual(verdict.plan_sha256, self.plan.canonical_sha256())

        def observe(**kwargs: object) -> M6QualificationEvidence:
            return m6.qualification_for(self.source, "e2e_publication", "att", **kwargs)

        failing_cases = {
            "one_failed_check": observe(checks=m6.check_results(failing=("table_segment_conservation",))),
            "one_unverified_check": observe(checks=m6.check_results(unverified=("independent_rebuild_match",))),
            "one_missing_check": observe(checks=m6.check_results(omit=("public_units_hash_match",))),
            "provider_page_mismatch": observe(provider_pages=8),
            "provider_extra_page": observe(provider_pages=10),
            "unusable_units": observe(unit_count=100, unusable=1),
        }
        for label, proof in failing_cases.items():
            result = qualify_document(proof, self.plan)
            self.assertEqual(result.verdict, "not_scorable", label)
            self.assertIsNone(result.scorable_page_count, label)
            self.assertTrue(result.reasons, label)
        with self.assertRaises(ValueError):
            qualify_document(good, self.service_plan)
        for outcome in ("pass", "fail"):
            with self.assertRaises(ValueError, msg=outcome):
                M6CheckResult(check_id="page_closure", outcome=outcome, evidence_sha256=None)  # type: ignore[arg-type]
        M6CheckResult(check_id="page_closure", outcome="unverified", evidence_sha256=None)
        with self.assertRaises(ValueError):
            m6.observation(self.source, "e2e_publication", attempt_id="att", processing_run_id=None)
        with self.assertRaises(ValueError):
            m6.observation(self.source, "e2e_publication", attempt_id="att", public_units_sha256=None)
        with self.assertRaises(ValueError):
            m6.observation(self.service_source, "service_diagnostic", attempt_id="att",
                           public_units_sha256=m6.digest("u"))

    def test_blank_document_needs_source_conservation_not_artificial_nonempty_units(self) -> None:
        blank = m6.qualification_for(self.source, "e2e_publication", "blank", unit_count=0)
        self.assertEqual(qualify_document(blank, self.plan).verdict, "scorable")
        heading_only = m6.qualification_for(self.source, "e2e_publication", "heading", unit_count=1)
        self.assertEqual(qualify_document(heading_only, self.plan).verdict, "scorable")

        blank_without_conservation = m6.qualification_for(
            self.source, "e2e_publication", "blank", unit_count=0,
            checks=m6.check_results(unverified=("page_closure", "block_conservation")))
        self.assertEqual(qualify_document(blank_without_conservation, self.plan).verdict, "not_scorable")

        # Review units are pending until their reason is planned and, when required, accepted.
        unexplained = m6.qualification_for(self.source, "e2e_publication", "r", unit_count=4, needs_review=1)
        self.assertEqual(qualify_document(unexplained, self.plan).verdict, "review_pending")
        unknown_reason = m6.qualification_for(self.source, "e2e_publication", "r", unit_count=4, needs_review=1,
                                              review_reasons=("footnote_marker",))
        self.assertEqual(qualify_document(unknown_reason, self.plan).verdict, "review_pending")
        with self.assertRaises(ValueError):
            m6.observation(self.source, "e2e_publication", attempt_id="r", unit_count=1, needs_review=1, unusable=1)

        planned = m6.quality_plan("e2e_publication", ("footnote_marker", "accepted_noncritical"),
                                  ("scan_noise", "review_required"), ("split_table", "score_hard_fail"))
        self.assertEqual(qualify_document(unknown_reason, planned).verdict, "scorable")
        needs_review = m6.observation(self.source, "e2e_publication", attempt_id="r", unit_count=4, needs_review=2,
                                      review_reasons=("scan_noise",))
        self.assertEqual(qualify_document(m6.evidence(needs_review), planned).verdict, "review_pending")
        accepted = m6.evidence(needs_review, m6.review(needs_review, "scan_noise", "accept"))
        self.assertEqual(qualify_document(accepted, planned).verdict, "scorable")
        rejected = m6.evidence(needs_review, m6.review(needs_review, "scan_noise", "reject"))
        self.assertEqual(qualify_document(rejected, planned).verdict, "not_scorable")
        mixed = m6.evidence(needs_review, m6.review(needs_review, "scan_noise", "accept"),
                            m6.review(needs_review, "scan_noise", "reject", reviewer="reviewer-2"))
        self.assertEqual(qualify_document(mixed, planned).verdict, "not_scorable", "any rejection excludes")
        hard_fail = m6.observation(self.source, "e2e_publication", attempt_id="r", unit_count=4, needs_review=1,
                                   review_reasons=("split_table",))
        accepted_anyway = m6.evidence(hard_fail, m6.review(hard_fail, "split_table", "accept"))
        self.assertEqual(qualify_document(accepted_anyway, planned).verdict, "not_scorable",
                         "a frozen hard-fail disposition is not overridden by a later accept")

    def test_reason_plan_and_review_are_bound_to_exact_observation(self) -> None:
        obs = m6.observation(self.source, "e2e_publication", attempt_id="r", unit_count=4, needs_review=1,
                             review_reasons=("scan_noise",))
        other = m6.observation(self.source, "e2e_publication", attempt_id="r", unit_count=5, needs_review=1,
                               review_reasons=("scan_noise",))
        good_review = m6.review(obs, "scan_noise", "accept")
        M6QualificationEvidence(observation=obs, reviews=(good_review,))
        with self.assertRaises(ValueError):
            M6QualificationEvidence(observation=obs, reviews=(m6.review(other, "scan_noise", "accept"),))
        with self.assertRaises(ValueError):
            M6QualificationEvidence(observation=obs, reviews=(m6.review(obs, "unlisted", "accept"),))
        with self.assertRaises(ValueError):
            M6QualificationEvidence(observation=obs, reviews=(good_review, good_review))
        with self.assertRaises(ValueError):
            M6QualificationEvidence(observation=obs, reviews=(
                m6.review(obs, "scan_noise", "accept", reviewer="z"), m6.review(obs, "scan_noise", "accept", reviewer="a")))

        planned = m6.quality_plan("e2e_publication", ("scan_noise", "review_required"))
        proof = M6QualificationEvidence(observation=obs, reviews=(good_review,))
        first = qualify_document(proof, planned)
        second = qualify_document(proof, planned)
        self.assertEqual(first, second, "qualification is a pure projection of frozen evidence")
        self.assertEqual(first.plan_sha256, planned.canonical_sha256())
        self.assertNotEqual(first.plan_sha256, self.plan.canonical_sha256())
        self.assertEqual(qualify_document(proof, self.plan).verdict, "review_pending",
                         "the historical plan without the reason leaves the unit pending")

        with self.assertRaises(ValueError):
            M6DocumentQualification(evidence_sha256=m6.digest("e"), plan_sha256=m6.digest("p"),
                                    verdict="scorable", reasons=(), scorable_page_count=None)
        with self.assertRaises(ValueError):
            M6DocumentQualification(evidence_sha256=m6.digest("e"), plan_sha256=m6.digest("p"),
                                    verdict="not_scorable", reasons=(), scorable_page_count=None)
        with self.assertRaises(ValueError):
            M6DocumentQualification(evidence_sha256=m6.digest("e"), plan_sha256=m6.digest("p"),
                                    verdict="not_scorable", reasons=("x",), scorable_page_count=3)
        with self.assertRaises(ValueError):
            M6DocumentQualification(evidence_sha256=m6.digest("e"), plan_sha256=m6.digest("p"),
                                    verdict="review_pending", reasons=("b", "a"), scorable_page_count=None)


class ClosedModelShapeTests(unittest.TestCase):
    def test_schema_objects_do_not_admit_mutable_nested_members(self) -> None:
        fixture = m6.make_fixture("e2e_publication", {"a": (2, "fresh"), "b": (3, "replay")})
        spec, corpus = fixture.spec, fixture.manifest
        for value in (spec.carry_in_attempt_ids, corpus.entries, fixture.plan.required_checks,
                      fixture.plan.reason_policies):
            self.assertIsInstance(value, tuple)
        for model in (spec, spec.clock, spec.runtime, spec.resources, corpus, corpus.entries[0], fixture.plan):
            with self.assertRaises(ValueError, msg=type(model).__name__):
                setattr(model, next(iter(type(model).model_fields)), None)
        with self.assertRaises(ValueError):
            spec.resources.max_events = 1  # type: ignore[misc]
        with self.assertRaises(ValueError):
            M6CorpusManifest(campaign_id="c", mode="e2e_publication", entries=[corpus.entries[0]])  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            M6RunSpec.model_validate({**spec.model_dump(), "carry_in_attempt_ids": ["a"]})

        # Owner stamps bind the entire producer event; a changed digest or event is rejected.
        journal = m6.Journal(fixture)
        record = journal.start()
        with self.assertRaises(ValueError):
            M6RunEvent(event=record.event, stamp=record.stamp.model_copy(
                update={"producer_event_sha256": m6.digest("other")}))
        with self.assertRaises(ValueError):
            M6RunEvent(event=record.event.model_copy(update={"producer_sequence": 2}), stamp=record.stamp)
        with self.assertRaises(ValueError):
            M6OwnerStamp(sequence=0, received_qpc_ticks=1, boot_identity_sha256=m6.digest("b"),
                         owner_process_epoch_sha256=m6.OWNER_EPOCH, producer_event_sha256=m6.digest("e"))

        # Receipt and history projections cannot describe a trusted numerator without closure.
        with self.assertRaises(ValueError):
            M6SourceHistoryFact(source_pdf_sha256=m6.digest("s"), scan_complete=True, first_processing_run_id="r",
                                first_ledger_seq=None, first_source_page_count=None, source_page_variants=1,
                                audit_receipt_sha256=m6.digest("a"))
        with self.assertRaises(ValueError):
            M6SourceHistoryFact(source_pdf_sha256=m6.digest("s"), scan_complete=True, first_processing_run_id=None,
                                first_ledger_seq=None, first_source_page_count=None, source_page_variants=1,
                                audit_receipt_sha256=m6.digest("a"))
        with self.assertRaises(ValueError):
            M6PublicationMetrics(window_pages=5, whole_run_pages=4, carry_in_pages=0)
        with self.assertRaises(ValueError):
            M6ServiceMetrics(window_pages=0, whole_run_pages=4, replay_pages=5, carry_in_pages=0)
        base = {
            "run_id": "r", "spec_sha256": m6.digest("spec"), "journal_prefix_sha256": m6.digest("j"),
            "journal_bytes_consumed": 0, "mode": "service_diagnostic", "phase": "short_batch",
            "status": "complete", "incomplete_reasons": (), "invalid_reasons": (), "t0_ticks": 10,
            "deadline_ticks": 20, "tclose_ticks": 25, "elapsed_ticks": 15, "qpc_frequency_hz": 10,
            "stop_requested_ticks": None, "stop_effective_ticks": None, "close_reason": "deadline_drained",
            "metrics": M6ServiceMetrics(window_pages=0, whole_run_pages=0, replay_pages=0, carry_in_pages=0),
            "sources": (), "events_total": 0, "duplicate_events": 0,
        }
        M6RunReceipt(**base)  # type: ignore[arg-type]
        for label, update in {
            "complete_without_metrics": {"metrics": None},
            "incomplete_with_metrics": {"status": "incomplete", "incomplete_reasons": ("x",)},
            "status_disagrees_with_reasons": {"status": "complete", "invalid_reasons": ("x",)},
            "elapsed_not_interval": {"elapsed_ticks": 14},
            "complete_without_elapsed": {"elapsed_ticks": None, "tclose_ticks": None},
            "publication_metrics_in_service_mode": {"metrics": M6PublicationMetrics(
                window_pages=0, whole_run_pages=0, carry_in_pages=0)},
            "unsorted_reasons": {"status": "incomplete", "metrics": None, "incomplete_reasons": ("b", "a")},
        }.items():
            with self.assertRaises(ValueError, msg=label):
                M6RunReceipt(**{**base, **update})  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
