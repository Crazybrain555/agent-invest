from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from disclosure_anchor.application.contracts.m6_campaign import M6CampaignScope, M6CorpusManifest
from disclosure_anchor.application.contracts.m6_document_qualification import (
    M6QualificationEvidence, M6ReasonPolicy, M6ReviewRecord, qualify_document,
)
from disclosure_anchor.application.contracts.m6_run import M6RunSpec
from disclosure_anchor.application.ports.staged_new_work_v4 import validate_admission_document_ids
from tests.m6_support import RunExample, changed, golden, sha


class M6RunContractTests(unittest.TestCase):
    def test_campaign_above_eight_is_explicit_immutable_and_legacy_bound_remains(self):
        example = RunExample(sources=9)
        scope = M6CampaignScope.from_manifest(example.manifest)
        self.assertEqual(len(scope.document_ids), 9)
        scope.verify_manifest(example.manifest)
        with self.assertRaises(ValueError):
            validate_admission_document_ids(scope.document_ids)
        with self.assertRaises(ValidationError):
            scope.document_ids = ()
        with self.assertRaises(ValidationError):
            changed(scope, document_ids=())
        with self.assertRaises(ValueError):
            M6CampaignScope.from_manifest(RunExample(service=True).manifest)

    def test_manifest_rejects_duplicate_source_document_and_noncanonical_order(self):
        manifest = RunExample(sources=2).manifest
        for entries in ((), manifest.entries[::-1], (manifest.entries[0],)*2,
                        (manifest.entries[0], changed(manifest.entries[1], document_id=manifest.entries[0].document_id))):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                changed(manifest, entries=entries)
        with self.assertRaises(ValueError):
            changed(manifest.entries[0], document_id=" doc")

    def test_canonical_roundtrip_and_missing_unknown_duplicate_and_coerced_fields(self):
        spec = RunExample().spec
        raw = spec.canonical_bytes()
        self.assertEqual(M6RunSpec.from_canonical_bytes(raw, maximum_bytes=10000), spec)
        changes = ({"planned_seconds": True}, {"t0_ticks": "100"}, {"shell": "cmd"},
                   {"carry_in_attempt_ids": ["attempt"]})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                changed(spec, **change)
        missing = spec.model_dump(mode="json")
        del missing["contract_version"]
        for bad in (raw+b"\n", raw.replace(b'"t0_ticks":100', b'"t0_ticks":100,"t0_ticks":100'),
                    json.dumps(missing, sort_keys=True, separators=(",", ":")).encode()):
            with self.assertRaises(ValueError):
                M6RunSpec.from_canonical_bytes(bad, maximum_bytes=10000)

    def test_short_batch_is_not_a_formal_hour_and_clock_cannot_drift(self):
        spec = RunExample().spec
        short = changed(spec, planned_seconds=10, deadline_ticks=200)
        self.assertEqual(short.phase, "short_batch")
        for updates in ({"phase": "hour_baseline"}, {"phase": "stability_repeat"}, {"deadline_ticks": 201}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                changed(short, **updates)
        with self.assertRaises(ValueError):
            changed(spec.clock, qpc_frequency_hz=11)
        with self.assertRaises(ValueError):
            changed(spec, scope_sha256=None)
        with self.assertRaises(ValueError):
            changed(RunExample(service=True).spec, scope_sha256=sha("scope"))

    def test_quality_requires_every_structural_check_and_whole_document(self):
        example = golden()
        evidence = example.evidence[0]
        self.assertEqual(qualify_document(evidence, example.plan).scorable_page_count, 7)
        for update, expected in (({"provider_page_count": 6}, "page_count_mismatch"),
                ({"unusable_unit_count": 1}, "unusable_units_present"),
                ({"checks": evidence.observation.checks[1:]}, "required_check_missing:")):
            modified = changed(evidence, observation=changed(evidence.observation, **update))
            result = qualify_document(modified, example.plan)
            self.assertEqual(result.verdict, "not_scorable")
            self.assertIsNone(result.scorable_page_count)
            self.assertTrue(any(reason.startswith(expected) for reason in result.reasons))
        with self.assertRaises(ValueError):
            changed(example.plan, required_checks=("page_closure",))
        with self.assertRaises(ValueError):
            changed(evidence.observation.checks[0], evidence_sha256=None)

    def test_blank_document_needs_source_conservation_not_artificial_nonempty_units(self):
        example = golden(service=True)
        evidence = changed(example.evidence[0], observation=changed(example.evidence[0].observation, unit_count=0))
        self.assertEqual(qualify_document(evidence, example.plan).verdict, "scorable")
        failed = changed(evidence.observation.checks[0], outcome="fail")
        evidence = changed(evidence, observation=changed(evidence.observation, checks=(failed, *evidence.observation.checks[1:])))
        self.assertEqual(qualify_document(evidence, example.plan).verdict, "not_scorable")

    def test_reason_plan_and_review_are_bound_to_exact_observation(self):
        example = golden()
        observation = changed(example.evidence[0].observation, needs_review_unit_count=1,
                              review_reasons=("numeric_token_mismatch",))
        evidence = M6QualificationEvidence(observation=observation, reviews=())
        plan = changed(example.plan, reason_policies=(M6ReasonPolicy(reason="numeric_token_mismatch", disposition="review_required"),))
        self.assertEqual(qualify_document(evidence, plan).verdict, "review_pending")
        review = M6ReviewRecord(observation_sha256=observation.canonical_sha256(), reason="numeric_token_mismatch",
            reviewer_identity="independent-reviewer", decision="accept", evidence_sha256=sha("source-review"))
        reviewed = changed(evidence, reviews=(review,))
        self.assertEqual(qualify_document(reviewed, plan).verdict, "scorable")
        self.assertEqual(qualify_document(reviewed, example.plan).verdict, "review_pending")
        accepted_plan = changed(plan, reason_policies=(M6ReasonPolicy(reason="numeric_token_mismatch", disposition="accepted_noncritical"),))
        self.assertEqual(qualify_document(evidence, accepted_plan).verdict, "scorable")
        hard_plan = changed(plan, reason_policies=(M6ReasonPolicy(reason="numeric_token_mismatch", disposition="score_hard_fail"),))
        self.assertEqual(qualify_document(reviewed, hard_plan).verdict, "not_scorable")
        self.assertEqual(qualify_document(changed(evidence, reviews=(changed(review, decision="reject"),)), plan).verdict, "not_scorable")
        with self.assertRaises(ValueError):
            changed(reviewed, observation=changed(observation, source_pdf_sha256=sha("other-source")))

    def test_schema_objects_do_not_admit_mutable_nested_members(self):
        manifest = RunExample().manifest
        payload = manifest.model_dump(mode="json")
        with self.assertRaises(ValueError):
            M6CorpusManifest.model_validate(payload)
        parsed = M6CorpusManifest.from_canonical_bytes(manifest.canonical_bytes(), maximum_bytes=10000)
        with self.assertRaises(ValidationError):
            parsed.entries[0].source_page_count = 99


if __name__ == "__main__":
    unittest.main()
