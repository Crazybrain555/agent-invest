"""Independent integration of pure qualification core with local immutable files."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from disclosure_anchor.adapters.runtime import m6_qualification_verifier as subject
from disclosure_anchor.application.contracts.m6_document_qualification import (
    M6QualificationEvidence, M6QualityPlan, M6_SERVICE_CHECKS, qualify_document,
)
from disclosure_anchor.application.contracts.atomic_publication_artifact_readiness_v4 import AtomicPublicationArtifactConflict
from disclosure_anchor.application.services.provider_document_admission import ProviderDocumentAdmission
from disclosure_anchor.application.services.semantic_router import SemanticRouter
from disclosure_anchor.application.services.atomic_publication_request_builder_v4 import ProductionAtomicPublicationRequestBuilderV4
from disclosure_anchor.application.services.source_semantic_comparison import compare_build_conservation
from tests._provider_source_semantics_fixture import admit, cases
from disclosure_anchor.application.services.provider_unit_builder import build_provider_units
from tests._m6_qualification_fixture import Fixture, Source, canonical, sha


def plan():
    return M6QualityPlan(mode="e2e_publication", required_checks=tuple(sorted((*M6_SERVICE_CHECKS, "public_units_hash_match"))), reason_policies=())


class QualificationChainTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fixture = Fixture(Path(self.directory.name))
        self.emitted = []

    def verifier(self, *, payload=None, provided=True, expected_hash=None, **changes):
        f = self.fixture
        raw = canonical(f.public if payload is None else payload)
        arguments = dict(private_facts_for=lambda admission: f.facts(subject.M6PrivateQualificationFacts),
            paths=f.paths, source=f.source, taxonomy=f.taxonomy, batch_size=16,
            receipt_sink=lambda name, raw: self.emitted.append((name, raw)), verifier_identity="independent-qualification",
            public_receipt_for=lambda admission: subject.M6PublicReceiptInput(
                raw, expected_hash or sha(raw), "independent-public-reader") if provided else None)
        return subject.M6ReadonlyQualificationVerifier(**(arguments | changes))

    def qualify(self, **changes):
        # Fixture receipts are prepared before these tripwires. Verification
        # must run the real core but cannot generate new routing/admission.
        with ExitStack() as stack:
            for cls, method in ((SemanticRouter, "route"), (ProviderDocumentAdmission, "admit_materialized"),
                                (ProductionAtomicPublicationRequestBuilderV4, "build"),
                                (subject._RefusingExecutor, "adjudicate")):
                stack.enter_context(patch.object(cls, method, side_effect=AssertionError(f"forbidden {method}")))
            return self.verifier(**changes).qualify(self.fixture.admission)

    def report(self):
        return json.loads(next(raw for name, raw in reversed(self.emitted) if name == "qualification-report"))

    def assert_public_rejected(self, payload):
        try:
            evidence = self.qualify(payload=payload)
        except subject.M6QualificationUnavailable:
            self.assertNotIn("qualification-evidence", [name for name, _ in self.emitted])
        else:
            outcomes = {item.check_id: item.outcome for item in evidence.observation.checks}
            self.assertNotEqual(outcomes["public_units_hash_match"], "pass")
            self.assertEqual(qualify_document(evidence, plan()).verdict, "not_scorable")

    def test_complete_real_readiness_admit_v3_preid_core_emits_resolvable_evidence(self):
        evidence = self.qualify()
        self.assertEqual([name for name, _ in self.emitted], ["qualification-report", "qualification-evidence"])
        self.assertEqual(M6QualificationEvidence.from_canonical_bytes(self.emitted[-1][1], maximum_bytes=10**7), evidence)
        self.assertEqual(set(self.fixture.source.calls), {"record", "raw", "rebuild", "native"})
        self.assertEqual(len(evidence.observation.checks), 13)
        self.assertTrue(all(check.outcome == "pass" for check in evidence.observation.checks))
        self.assertEqual(evidence.observation.public_units_sha256, sha(canonical(self.fixture.public["units"])))
        self.assertEqual(qualify_document(evidence, plan()).verdict, "scorable")
        report = self.report()
        self.assertTrue(report["replay"]["succeeded"])
        self.assertEqual(report["projection"]["pre_id_projection_mismatches"], [])
        # Every check digest must name actual included evidence, not a made-up pin.
        for check in evidence.observation.checks:
            self.assertEqual(check.evidence_sha256, sha(canonical({"check_id": check.check_id,
                             **report["checks"][check.check_id]})))

    def test_source_rebuild_disagreement_cannot_fall_back_to_materialized(self):
        original = self.fixture.source.rebuilt
        self.fixture.source.rebuilt = replace(original, pages=tuple(replace(page, blocks=()) for page in original.pages))
        with self.assertRaises(subject.M6QualificationUnavailable) as caught:
            self.qualify()
        self.assertEqual(caught.exception.phase, "source_admission")
        self.assertIn("rebuild", self.fixture.source.calls)
        self.assertEqual([name for name, _ in self.emitted], ["qualification-report"])

    def test_actual_file_drift_is_rejected_by_readiness_before_source_admission(self):
        path = self.fixture.paths.data_path(Path(self.fixture.run.semantic_route_receipts_relpath))
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises((subject.M6QualificationUnavailable, AtomicPublicationArtifactConflict)):
            self.qualify()
        self.assertEqual(self.fixture.source.calls, [])
        self.assertNotIn("qualification-evidence", [name for name, _ in self.emitted])

    def test_nested_receipt_taxonomy_drift_fails_without_model_retry(self):
        evidence = self.qualify(taxonomy=replace(self.fixture.taxonomy, version="independent-other.v1"))
        outcomes = {item.check_id: item.outcome for item in evidence.observation.checks}
        self.assertEqual(outcomes["reading_order_contiguity"], "fail")
        self.assertEqual(qualify_document(evidence, plan()).verdict, "not_scorable")
        self.assertFalse(self.report()["replay"]["succeeded"])

    def test_v3_version_drift_and_document_context_drift_never_silently_replay(self):
        for changed in ("version", "context"):
            with self.subTest(changed=changed):
                old_version = self.fixture.run.semantic_route_receipts_contract_version
                old_title = self.fixture.document.title
                if changed == "version":
                    self.fixture.run.semantic_route_receipts_contract_version = "semantic_route_receipt.v2"
                else:
                    self.fixture.document.title = "不同的来源上下文"
                try:
                    evidence = self.qualify()
                    self.assertEqual(next(c.outcome for c in evidence.observation.checks
                                          if c.check_id == "reading_order_contiguity"), "fail")
                    self.assertEqual(qualify_document(evidence, plan()).verdict, "not_scorable")
                finally:
                    self.fixture.run.semantic_route_receipts_contract_version = old_version
                    self.fixture.document.title = old_title

    def test_missing_public_input_never_invents_an_e2e_digest(self):
        with self.assertRaises(subject.M6QualificationUnavailable) as caught:
            self.qualify(provided=False)
        self.assertEqual(caught.exception.phase, "public_receipt_missing")
        self.assertEqual([name for name, _ in self.emitted], ["qualification-report"])

    def test_public_expected_pin_and_canonical_bytes_are_required(self):
        with self.assertRaises(subject.M6QualificationUnavailable):
            self.qualify(expected_hash=sha(b"independently pinned different receipt"))
        raw = canonical(self.fixture.public) + b"\n"
        with self.assertRaises(subject.M6QualificationUnavailable):
            self.qualify(public_receipt_for=lambda admission: subject.M6PublicReceiptInput(raw, sha(raw)))

    def test_public_all_39_columns_participate_in_digest(self):
        for mutation in ("timestamp", "omit_column", "omit_unit", "duplicate_unit"):
            with self.subTest(mutation=mutation):
                payload = deepcopy(self.fixture.public)
                if mutation == "timestamp":
                    payload["units"][0]["observed_at"] = "2026-02-01T00:00:00+00:00"
                elif mutation == "omit_column":
                    del payload["units"][0]["created_at"]
                    payload["public_units_sha256"] = sha(canonical(payload["units"]))
                elif mutation == "omit_unit":
                    payload["units"] = []
                    payload["public_units_sha256"] = sha(canonical([]))
                else:
                    payload["units"] *= 2
                    payload["public_units_sha256"] = sha(canonical(payload["units"]))
                self.assert_public_rejected(payload)

    def test_public_cross_source_run_and_verifier_do_not_borrow_receipts(self):
        for target, key, value in (("admission", "source_byte_count", 101), ("run", "processing_run_id", "other-run"),
                                   ("document", "raw_file_hash", sha(b"other source")),
                                   ("publication", "winner_sha256", sha(b"other winner")),
                                   (None, "verifier_identity", "other-reader")):
            with self.subTest(target=target, key=key):
                payload = deepcopy(self.fixture.public)
                (payload if target is None else payload[target])[key] = value
                self.assert_public_rejected(payload)

    def test_public_wrong_database_and_publication_attempt_rejected(self):
        for target, key, value in (("identity", "database_name", "other_database"),
                                   ("publication", "attempt_id", "other-attempt")):
            with self.subTest(target=target):
                payload = deepcopy(self.fixture.public)
                (payload["snapshot"]["identity"] if target == "identity" else payload[target])[key] = value
                self.assert_public_rejected(payload)

    def test_public_closed_receipt_contract_rejects_extra_field_even_rehashed(self):
        payload = deepcopy(self.fixture.public)
        payload["trust_me_instead"] = True
        self.assert_public_rejected(payload)

    def test_actual_quality_finding_survives_and_unknown_policy_stays_pending(self):
        with tempfile.TemporaryDirectory() as root:
            self.fixture = Fixture(Path(root), quality=True)
            evidence = self.qualify()
            self.assertEqual(evidence.observation.needs_review_unit_count, 1)
            self.assertTrue(evidence.observation.review_reasons)
            self.assertTrue(all(reason.startswith("source_finding:") for reason in evidence.observation.review_reasons))
            self.assertEqual(evidence.reviews, ())
            self.assertEqual(qualify_document(evidence, plan()).verdict, "review_pending")

    def test_file_count_and_byte_limits_reject_readiness_before_source_io(self):
        for limit in ({"maximum_artifact_bytes": 1}, {"maximum_artifact_files": 1}):
            with self.subTest(limit=limit), self.assertRaises(subject.M6QualificationUnavailable):
                self.qualify(**limit)
            self.assertEqual(self.fixture.source.calls, [])

    def test_total_artifact_budget_must_not_reset_after_readiness(self):
        self.qualify()
        budget = self.report()["readiness"]["read_budget_after_readiness"]
        self.fixture.source.calls.clear()
        # Readiness alone consumes every configured byte/file. A subsequent
        # record/bundle/raw/native or V3 reread cannot fit the whole-call limit.
        with self.assertRaises((subject.M6QualificationUnavailable, ValueError)):
            self.qualify(maximum_artifact_bytes=budget["bytes_reserved"],
                         maximum_artifact_files=budget["files_reserved"])

    def test_source_file_size_cannot_exceed_admitted_size_before_unbounded_reads(self):
        path = self.fixture.paths.data_path(Path(self.fixture.document.raw_file_relpath))
        path.write_bytes(b"x" * 101)
        with self.assertRaises(subject.M6QualificationUnavailable) as caught:
            self.qualify()
        self.assertEqual(caught.exception.phase, "source_binding")
        self.assertEqual(self.fixture.source.calls, [])

    def test_v3_budget_exhaustion_remains_unavailable_not_semantic_failure(self):
        facts = self.fixture.facts(subject.M6PrivateQualificationFacts)
        store = subject._ReadOnlyStore(self.fixture.paths, subject._ReadBudget(1, 1))
        with self.assertRaises(subject.M6QualificationUnavailable) as caught:
            self.verifier()._v3_rows(facts, self.fixture.ready, store, {}, subject._Checks())
        self.assertEqual(caught.exception.phase, "read_budget")

    def test_source_failure_cancellation_and_sink_failure_are_not_success(self):
        for failure in (OSError("source unavailable"), KeyboardInterrupt("cancel source")):
            with self.subTest(failure=type(failure).__name__), patch.object(Source, "rebuild_provider_document", side_effect=failure):
                expected = subject.M6QualificationUnavailable if isinstance(failure, OSError) else type(failure)
                with self.assertRaises(expected) as caught:
                    self.qualify()
                if isinstance(failure, OSError):
                    self.assertIs(caught.exception.__cause__, failure)
                    self.assertEqual(caught.exception.phase, "source_admission")
                else:
                    self.assertIs(caught.exception, failure)
        with self.assertRaisesRegex(OSError, "sink unavailable"):
            self.qualify(receipt_sink=lambda name, raw: (_ for _ in ()).throw(OSError("sink unavailable")))

    def test_common_conservation_does_not_reject_equal_unassigned_diagnostic(self):
        provider, observations = cases()["multipage"]
        build = build_provider_units(admit(provider, observations))
        self.assertTrue(build.unassigned_table_parts)
        differences = compare_build_conservation(build.units, build.units,
            candidate_unassigned=build.unassigned_table_parts, reference_unassigned=build.unassigned_table_parts)
        self.assertTrue(all(not items for items in differences.values()))
        changed = compare_build_conservation(build.units, build.units,
            candidate_unassigned=(), reference_unassigned=build.unassigned_table_parts)
        self.assertTrue(changed["logical_table_conservation"])


class PrivateSnapshotTests(unittest.TestCase):
    def connection(self, *, role="disclosure_app", read_only="on", isolation="repeatable read"):
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        identity = dict(database_name="invest_engine", session_role=role, current_role=role,
                        session_superuser=False, current_superuser=False)
        info = dict(read_only=read_only, isolation=isolation, snapshot="10:10:", observed_at="2026-01-01T00:00:00+00:00")
        first, second = MagicMock(), MagicMock()
        first.mappings.return_value.one.return_value = identity
        second.mappings.return_value.one.return_value = info
        connection.execute.side_effect = [first, second]
        return engine, connection

    def test_private_snapshot_uses_app_readonly_and_rolls_back_success_and_error(self):
        for fail in (False, True):
            engine, connection = self.connection()
            with patch.object(subject, "Session"):
                try:
                    with subject._private_snapshot(engine) as (_, snapshot):
                        self.assertEqual(snapshot["identity"]["session_role"], "disclosure_app")
                        if fail:
                            raise RuntimeError("inside snapshot")
                except RuntimeError:
                    self.assertTrue(fail)
            self.assertIn("READ ONLY", connection.exec_driver_sql.call_args_list[0].args[0])
            connection.rollback.assert_called_once()
            engine.connect.return_value.__exit__.assert_called_once()

    def test_private_role_and_transaction_drift_fail_before_repository_access(self):
        for arguments in ({"role": "disclosure_reader"}, {"role": "postgres"},
                          {"read_only": "off"}, {"isolation": "read committed"}):
            with self.subTest(arguments=arguments):
                engine, _ = self.connection(**arguments)
                with patch.object(subject, "Session") as session:
                    with self.assertRaises(Exception):
                        with subject._private_snapshot(engine):
                            self.fail("invalid private snapshot yielded")
                    session.assert_not_called()
                engine.connect.return_value.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
