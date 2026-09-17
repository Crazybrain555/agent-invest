"""Independent WP3 oracles; raw journals and hand counts, never report-derived expectations."""

import json
import contextlib
import io
import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.application.contracts.m6_campaign_intent import (
    M6CampaignIntentV1, decode_campaign_intent,
)
from disclosure_anchor.application.contracts.m6_evaluation_plan import M6EvaluationPlan
from tests.m6_delivery_support import campaign_intent, evaluation_plan, publication_case
from tests import m6_support as m6


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class FrozenPlanIndependentTests(unittest.TestCase):
    def test_loader_pins_and_preserves_evaluation_bytes_separately_from_quality(self):
        from disclosure_anchor.adapters.runtime.m6_campaign_assembly import (
            CampaignIdentityError, load_campaign_inputs,
        )
        from disclosure_anchor.application.contracts.m6_campaign import M6CampaignScope

        plan = evaluation_plan()
        case = publication_case()
        intent = campaign_intent(case, plan)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "worker.env").write_bytes(b"TEST_ONLY=1\n")
            def sha(raw):
                return "sha256:" + hashlib.sha256(raw).hexdigest()
            release_raw = canonical({"source": {
                "head": intent.runtime.source_commit,
                "source_manifest_sha256": intent.runtime.source_manifest_sha256,
            }, "native_m6": {"launcher_sha256": m6.digest("launcher"),
                             "production_source_manifest_sha256": m6.digest("owner-source")}})
            binding_raw = canonical({
                "contract_version": "m6.release-binding.v1", "release_manifest_sha256": sha(release_raw),
                "source_head": intent.runtime.source_commit,
                "runtime_bundle_identity_sha256": intent.runtime.runtime_bundle_identity_sha256,
                "deployment_qualification_sha256": intent.runtime.deployment_qualification_sha256,
                "deployment_qualification_document_count": 2, "capacity_config_sha256": m6.digest("capacity"),
                "process_profile_sha256": intent.runtime.process_profile_sha256,
                "activation_sha256": m6.digest("activation"), "stream_ceiling": 7, "owner": {},
                "cgroup_identity_sha256": m6.digest("cgroup"), "cgroup_max_bytes": 1,
                "gpu_uuid": "synthetic-gpu", "worker_overlay_sha256": m6.digest("overlay"),
                "worker_env_sha256": sha((root / "worker.env").read_bytes()),
                "bound_at_utc": "2026-01-01T00:00:00Z", "paths": {},
            })
            intent = type(intent).model_validate({**intent.model_dump(),
                                                 "binding_sha256": sha(binding_raw),
                                                 "release_manifest_sha256": sha(release_raw)})
            payloads = {"intent": intent.canonical_bytes(), "binding": binding_raw,
                        "release_manifest": release_raw, "manifest": case.fixture.manifest.canonical_bytes(),
                        "scope": M6CampaignScope.from_manifest(case.fixture.manifest).canonical_bytes(),
                        "quality_plan": case.fixture.plan.canonical_bytes(), "evaluation_plan": plan.canonical_bytes()}
            args = {name + "_path": root / (name + ".json") for name in payloads}
            for name, raw in payloads.items():
                args[name + "_path"].write_bytes(raw)
            args.update(intent_sha256=sha(payloads["intent"]), private_binding_path=root / "private.json")
            declared_io = SimpleNamespace(env_dir=root, ssh=SimpleNamespace(), windows=SimpleNamespace(
                gpu_uuid="synthetic-gpu", launcher_sha256=m6.digest("launcher"),
                owner_source_sha256=m6.digest("owner-source")))
            prefix = "disclosure_anchor.adapters.runtime.m6_campaign_assembly."
            with (patch(prefix + "load_campaign_private_binding", return_value=declared_io),
                  patch(prefix + "assert_known_hosts_pins_address")):
                actual = load_campaign_inputs(**args)
                self.assertEqual(actual.evaluation_plan, plan)
                self.assertEqual(actual.evaluation_plan_raw, plan.canonical_bytes(),
                                 "quality-plan bytes must not overwrite the frozen evaluation-plan bytes")
                args["evaluation_plan_path"].write_bytes(case.fixture.plan.canonical_bytes())
                with self.assertRaises(CampaignIdentityError):
                    load_campaign_inputs(**args)

    def test_plan_is_closed_and_windows_are_fixed_before_measurement(self):
        plan = evaluation_plan()
        self.assertEqual(plan.sub_window_count, 3)
        self.assertEqual((plan.main_window.start_offset_seconds, plan.main_window.end_offset_seconds),
                         (600, 4200))
        self.assertEqual(M6EvaluationPlan.from_canonical_bytes(plan.canonical_bytes(), maximum_bytes=65536), plan)
        changes = (
            {"main_window": {"start_offset_seconds": 600, "end_offset_seconds": 600}},
            {"main_window": {"start_offset_seconds": True, "end_offset_seconds": 4200}},
            {"sub_window_seconds": 1199},
            {"choose_best_observed_window": True},
            {"credit": {**plan.credit.model_dump(), "late_backfill": True}},
            {"size_classes": {"short_max_pages": 150, "medium_max_pages": 149}},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                M6EvaluationPlan.model_validate({**plan.model_dump(), **change})

    def test_v2_pins_exact_plan_while_archived_v1_keeps_original_validation(self):
        plan = evaluation_plan()
        intent = campaign_intent(publication_case(), plan)
        self.assertEqual(decode_campaign_intent(intent.canonical_bytes()), intent)
        self.assertEqual(intent.evaluation_plan_sha256, plan.canonical_sha256())
        moved = M6EvaluationPlan.model_validate({**plan.model_dump(), "main_window": {
            "start_offset_seconds": 660, "end_offset_seconds": 4260,
        }})
        self.assertNotEqual(moved.canonical_sha256(), intent.evaluation_plan_sha256)
        original = intent.model_dump(mode="json")
        original.pop("evaluation_plan_sha256")
        original["contract_version"] = "m6.campaign-intent.v1"
        self.assertIsInstance(decode_campaign_intent(canonical(original)), M6CampaignIntentV1)
        # Read-only legacy decoding must not reinterpret records the old contract rejected.
        for change in (
            {"close_grace_seconds": 2401},
            {"run": {**original["run"], "planned_seconds": 600}, "runner_stop_reserve_seconds": 600},
            {"runtime": {**original["runtime"], "worker_profile_sha256": None}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                decode_campaign_intent(canonical({**original, **change}))


class DeliveryWindowIndependentTests(unittest.TestCase):
    def classify(self, case):
        from disclosure_anchor.application.services.m6_delivery_report import classify_main_window

        receipt = m6.reduce(case.fixture, case.journal, history=case.history, qualifications=case.qualifications)
        self.assertEqual(receipt.status, "complete", (receipt.invalid_reasons, receipt.incomplete_reasons))
        return classify_main_window(plan=evaluation_plan(), spec=case.fixture.spec, receipt=receipt).window

    def test_hand_counted_half_open_window_excludes_late_and_nonfresh_credit(self):
        case = publication_case()
        window = self.classify(case)
        self.assertEqual((window.start_ticks, window.end_ticks), (case.fixture.at(600), case.fixture.at(4200)))
        self.assertEqual((window.documents, window.pages), (4, 60))
        self.assertEqual(window.span_seconds, 3600)
        self.assertEqual((window.documents_per_hour, window.pages_per_minute), (4, 1))
        self.assertEqual([(w.documents, w.pages) for w in window.sub_windows], [(2, 24), (1, 17), (1, 19)])
        self.assertEqual([(w.start_ticks, w.end_ticks) for w in window.sub_windows], [
            (case.fixture.at(600), case.fixture.at(1800)),
            (case.fixture.at(1800), case.fixture.at(3000)),
            (case.fixture.at(3000), case.fixture.at(4200)),
        ])

    def test_exact_owner_retry_cannot_double_credit_source(self):
        case = publication_case()
        # A physically repeated, byte-identical delivery of an existing owner record.
        case.journal.records.insert(21, case.journal.records[20])
        window = self.classify(case)
        self.assertEqual((window.documents, window.pages), (4, 60))

    def test_rate_uses_frozen_hour_even_when_all_outputs_fit_a_shorter_interval(self):
        case = publication_case(threshold=True)
        window = self.classify(case)
        self.assertEqual((window.documents, window.pages), (55, 8525))
        self.assertEqual(window.documents_per_hour, 55)
        self.assertAlmostEqual(window.pages_per_minute, 8525 / 60)
        self.assertEqual(window.span_seconds, 3600)


class DeliveryProofIndependentTests(unittest.TestCase):
    def setUp(self):
        from disclosure_anchor.application.services.m6_delivery_report import ExternalExitFacts, RunClosureFacts

        self.case = publication_case(threshold=True)
        self.plan = evaluation_plan()
        self.intent = campaign_intent(self.case, self.plan)
        self.receipt = m6.reduce(self.case.fixture, self.case.journal, history=self.case.history,
                                 qualifications=self.case.qualifications)
        self.closure = RunClosureFacts(
            runner_receipt_sha256=m6.digest("runner-receipt"), runner_status="complete", closure_complete=True,
            ownership_closure_sha256=m6.digest("ownership-receipt"), residual_count=0, children_exited=True,
            admitted_attempt_count=55, final_attempt_count=55,
            admission_reconciliation_sha256=m6.digest("admission-reconciliation"),
            admission_closed_admitted_count=55, admission_closed_unresolved_count=0,
            admission_closed_last_producer_sequence=max(r.event.producer_sequence for r in self.case.journal.records
                                                       if r.event.producer_kind == "e2e_runner"),
            admission_closed_reconciliation_sha256=m6.digest("admission-reconciliation"),
            resources_closed_residual_count=0, resources_closed_children_exited=True,
            resources_closed_receipt_sha256=m6.digest("ownership-receipt"),
            verifier_summary_status="complete", drain_receipt_sha256=m6.digest("drain:drain-1"),
            run_closed_reason="deadline_drained",
        )
        self.external = ExternalExitFacts(verified=True, exit_code=0, forced_termination=False,
                                         local_children_reaped=True)

    def report(self, **changes):
        from disclosure_anchor.application.services.m6_delivery_report import build_delivery_report

        values = dict(plan=self.plan, intent=self.intent, spec=self.case.fixture.spec,
                      receipt=self.receipt, events=tuple(self.case.journal.records),
                      manifest=self.case.fixture.manifest, closure=self.closure,
                      external=self.external, telemetry=None, inputs=())
        values.update(changes)
        return build_delivery_report(**values)

    def test_positive_hand_threshold_is_conditional_on_all_declared_proofs(self):
        report = self.report()
        self.assertTrue(report.delivery_pass, report)
        self.assertEqual((report.main_window.documents, report.main_window.pages), (55, 8525))
        self.assertEqual((report.whole_run.documents, report.whole_run.pages), (55, 8525))
        self.assertEqual(report.unknowns, ())
        self.assertEqual(report.resource_safety.status, "unknown", "this vector did not measure GPU resources")

    def test_every_absent_or_failed_closure_proof_blocks_delivery(self):
        cases = (
            {"runner_receipt_sha256": None}, {"runner_status": "failed"}, {"closure_complete": False},
            {"verifier_summary_status": None}, {"verifier_summary_status": "failed"},
            {"drain_receipt_sha256": None}, {"drain_receipt_sha256": m6.digest("another-drain")},
            {"admission_closed_admitted_count": None}, {"admission_closed_unresolved_count": 1},
            {"admission_closed_last_producer_sequence": 1},
            {"resources_closed_receipt_sha256": None, "resources_closed_residual_count": None,
             "resources_closed_children_exited": None},
            {"ownership_closure_sha256": None, "residual_count": None, "children_exited": None},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertFalse(self.report(closure=replace(self.closure, **changes)).delivery_pass,
                                 "high counts must not replace an absent or failed closure proof")

    def test_missing_or_unbound_intent_cannot_be_a_formal_pass(self):
        self.assertFalse(self.report(intent=None).delivery_pass)
        # Required frozen evidence stays unproved when missing, unreadable or
        # oversized; changing the IO failure reason cannot turn the run green.
        for reason in ("evaluation_plan_not_in_run_output",
                       "evaluation_plan_not_in_run_output_unreadable:PermissionError",
                       "evaluation_plan_not_in_run_output_exceeds_byte_bound"):
            with self.subTest(reason=reason):
                self.assertFalse(self.report(loader_unknowns=(reason,)).delivery_pass)
        bad = type(self.intent).model_validate({**self.intent.model_dump(),
                                               "evaluation_plan_sha256": m6.digest("different-plan")})
        try:
            result = self.report(intent=bad)
        except ValueError:
            return
        self.assertFalse(result.delivery_pass)

    def test_external_zero_flags_cannot_override_bad_actual_exit_fields(self):
        for changes in ({"exit_code": 1}, {"forced_termination": True}, {"cancel": "cancelled"},
                        {"exit_code": None}, {"local_children_reaped": False}, {"verified": False}):
            with self.subTest(changes=changes):
                self.assertFalse(self.report(external=replace(self.external, **changes)).delivery_pass)

    def test_open_ack_and_damaged_owner_evidence_cannot_be_washed_by_closure_booleans(self):
        missing = publication_case(threshold=True, omit_final="t00")
        receipt = m6.reduce(missing.fixture, missing.journal, history=missing.history,
                            qualifications=missing.qualifications)
        report = self.report(receipt=receipt, events=tuple(missing.journal.records))
        self.assertFalse(report.delivery_pass)
        self.assertIn("att-t00", report.business_obligations_closed.attempts_without_final)
        for mutation in ("gap", "epoch", "truncated"):
            with self.subTest(mutation=mutation):
                raw = list(self.case.journal.lines())
                if mutation == "gap":
                    raw.pop(20)
                elif mutation == "epoch":
                    record = self.case.journal.records[20]
                    raw[20] = record.model_copy(update={"stamp": record.stamp.model_copy(
                        update={"owner_process_epoch_sha256": m6.OWNER_EPOCH_2})}).canonical_bytes() + b"\n"
                else:
                    raw[-1] = raw[-1][:-1]
                damaged = m6.reduce(self.case.fixture, raw, history=self.case.history,
                                    qualifications=self.case.qualifications)
                self.assertNotEqual(damaged.status, "complete")
                self.assertFalse(self.report(receipt=damaged).delivery_pass)

    def test_latency_gates_cannot_substitute_acceptance_or_publication_for_remote_terminal(self):
        from disclosure_anchor.application.services.m6_delivery_report import latency_by_size

        case = publication_case()
        actual = latency_by_size(evaluation_plan(required_safety=True), case.fixture.spec,
                                 tuple(case.journal.records))
        # These facts only give admission, acceptance and public observations. Neither
        # first POST send nor remote terminal exists, so the Pro remote gates are unknown.
        self.assertEqual(actual.gates.status, "unknown")
        self.assertEqual(actual.gates.failed, ())
        self.assertEqual(actual.stage_timing.status, "unknown")

    def test_required_resource_and_latency_evidence_cannot_be_inferred_from_good_counts(self):
        required = evaluation_plan(required_safety=True)
        report = self.report(plan=required, intent=campaign_intent(self.case, required))
        self.assertFalse(report.delivery_pass)
        self.assertEqual(report.resource_safety.status, "unknown")
        self.assertNotEqual(report.latency_by_size.gates.status, "pass")



class DeliveryEvidenceIndependentTests(unittest.TestCase):
    """Exercise the public CLI from physical evidence, without substituting the reader."""

    def setUp(self):
        from disclosure_anchor.application.contracts.m6_owner import M6OwnerAnchor
        from disclosure_anchor.application.contracts.m6_run_events import M6PublicConfirmation, M6VerifierDrained

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / "campaign"
        self.owner_pid, self.owner_birth = 123, 133700000012345678
        self.case = publication_case(threshold=True, physical_owner=(self.owner_pid, self.owner_birth))
        self.plan = evaluation_plan()
        self.intent = campaign_intent(self.case, self.plan)
        spec = self.case.fixture.spec
        self.write("intent.json", self.intent.canonical_bytes())
        self.write("campaign-intent.json", self.intent.canonical_bytes())
        self.write("evaluation-plan.json", self.plan.canonical_bytes())
        self.write("manifest.json", self.case.fixture.manifest.canonical_bytes())
        self.write("quality-plan.json", self.case.fixture.plan.canonical_bytes())
        self.write("run/run-spec.json", spec.canonical_bytes())
        anchor = M6OwnerAnchor.model_validate({
            **{name: getattr(spec, name) for name in (
                "run_id", "clock", "t0_ticks", "planned_seconds", "deadline_ticks", "max_close_ticks", "resources")},
            "owner_process_epoch_sha256": self.case.journal.owner_epoch,
            "owner_source_sha256": spec.runtime.owner_source_sha256,
            "gpu_device_identity_sha256": spec.runtime.gpu_device_identity_sha256,
        })
        self.write("run/anchor.json", anchor.canonical_bytes())
        self.write("campaign-inputs.json", {
            "contract_version": "m6.campaign-inputs.v2", "intent_sha256": self.intent.canonical_sha256(),
            "attempt_id": "independent-launch-1",
            "evaluation_plan_sha256": self.plan.canonical_sha256(),
            "manifest_path": str(self.run / "manifest.json"), "quality_plan_path": str(self.run / "quality-plan.json"),
        })
        (self.root / "input-hashes.json").write_bytes(canonical({
            str(self.run / "intent.json"): self.intent.canonical_sha256(),
        }))
        histories = {fact.source_pdf_sha256: fact for fact in self.case.history}
        # Bind each physical public observation to real canonical audit bytes.
        drain = canonical({"synthetic": "drain receipt bound to the raw owner observation"})
        self.write("verifier/drain-receipt.json", drain)
        for index, record in enumerate(self.case.journal.records):
            payload = record.event.payload
            changes = {}
            if isinstance(payload, M6PublicConfirmation):
                fact = histories[payload.source_pdf_sha256]
                audit = canonical({
                    "contract_version": "m6.private-source-history-audit.v1",
                    "snapshot": {"read_only": "on", "isolation": "repeatable read", "identity": {
                        "session_role": "disclosure_app", "current_role": "disclosure_app",
                        "session_superuser": False, "current_superuser": False}},
                    "sources": [payload.source_pdf_sha256], "base_rows": [], "publication_witnesses": [],
                    "unattributed_publication_count": 0, "query_sha256": m6.digest("readonly-query"),
                    "projections": [fact.model_dump(exclude={"audit_receipt_sha256"})],
                })
                self.write("verifier/public/" + payload.attempt_id + "/private-history-audit.json", audit)
                changes["history_audit_receipt_sha256"] = self.sha(audit)
            elif isinstance(payload, M6VerifierDrained):
                changes["drain_receipt_sha256"] = self.sha(drain)
            if changes:
                updated = record.event.model_copy(update={"payload": payload.model_copy(update=changes)})
                self.case.journal.records[index] = record.model_copy(update={
                    "event": updated, "stamp": record.stamp.model_copy(update={
                        "producer_event_sha256": updated.canonical_sha256()})})
        for name, proof in zip(self.case.fixture.entries, self.case.qualifications, strict=True):
            self.write("verifier/quality/att-" + name + "/qualification-evidence.json", proof.canonical_bytes())
        self.write("native/events.jsonl", b"".join(self.case.journal.lines()))
        last_sequence = max(r.event.producer_sequence for r in self.case.journal.records
                            if r.event.producer_kind == "e2e_runner")
        self.write("native/admission-closed.json", {
            "kind": "admission_closed", "runner_epoch_sha256": m6.RUNNER_EPOCH,
            "admitted_attempt_count": 55, "unresolved_claim_count": 0, "last_producer_sequence": last_sequence,
            "reconciliation_receipt_sha256": m6.digest("admission-reconciliation"),
        })
        self.write("native/resources-closed.json", {
            "residual_count": 0, "children_exited": True,
            "ownership_receipt_sha256": m6.digest("ownership-receipt"),
        })
        self.write("runner/campaign-receipt.json", {"m6_assembly": {"status": "complete", "closure": {
            "complete": True, "ownership_closure_sha256": m6.digest("ownership-receipt"),
            "residual_count": 0, "children_exited": True, "admitted_attempt_count": 55, "final_attempt_count": 55,
            "admission_reconciliation_sha256": m6.digest("admission-reconciliation"),
        }}})
        self.write("verifier/run-summary.json", {"status": "complete"})
        self.expected_start = {
            "run_id": spec.run_id, "attempt_id": "independent-launch-1", "hostname": "synthetic-windows",
            "binary_sha256": m6.digest("native-binary"), "launcher_sha256": m6.digest("launcher"),
            "configuration_sha256": m6.digest("native-configuration"),
            "planned_seconds": self.intent.run.planned_seconds,
            "close_grace_seconds": self.intent.close_grace_seconds, "memory_bytes": self.intent.memory_bytes,
            "bootstrap_bind_seconds": self.intent.bootstrap_bind_seconds,
            "ready_wait_seconds": self.intent.ready_wait_seconds,
        }
        self.start_record = {**self.expected_start, "contract_version": "m6.owner-external-start.v2",
                             "pid": self.owner_pid, "creation_filetime_100ns": self.owner_birth,
                             "resume": False, "original_anchor_sha256": "none"}
        self.exit_record = {
            **{key: self.expected_start[key] for key in (
                "run_id", "attempt_id", "hostname", "binary_sha256", "launcher_sha256", "configuration_sha256")},
            "contract_version": "m6.owner-external-exit.v2", "scope": "production_owner_run",
            "pid": self.owner_pid, "creation_filetime_100ns": self.owner_birth, "exit_code": 0,
            "forced_termination": False, "cancel": None, "parent_failure": None,
            "exact_process_handle_opened": True, "process_handle_signaled": True,
            "ready_received": True, "ready_timeout": False, "stdout_eof": True, "stderr_eof": True,
        }
        self.ready = {"status": "ready_unbound", "anchor": json.loads(anchor.canonical_bytes()),
                      "anchor_sha256": anchor.canonical_sha256(),
                      "owner_epoch_sha256": self.case.journal.owner_epoch, "spec_sha256": None,
                      "journal_prefix_bytes": 0, "journal_prefix_sha256": self.sha(b"")}
        self.write("launcher-command.json", {"intent_sha256": self.intent.canonical_sha256(),
                                              "expected_start": self.expected_start})
        self.write("launcher-records/process-start.json", self.start_record)
        self.write("launcher-records/ready.json", self.ready)
        self.write("ready.json", self.ready)
        self.write("launcher-records/process-exit.json", self.exit_record)
        self.write("native/spec.json", spec.canonical_bytes())
        self.write("native/anchor.json", anchor.canonical_bytes())
        self.write("native/exit-observation.json", {"synthetic": "native-exit-observation"})
        self.write("campaign-summary.json", {
            "spec_sha256": spec.canonical_sha256(), "evaluation_plan_sha256": self.plan.canonical_sha256(),
            "owner_external_exit_verified": True, "local_children_reaped": True, "cleanup_failures": {},
            "children": {"launcher": {"external_exit": self.exit_record}},
        })

    @staticmethod
    def sha(raw):
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def write(self, name, value):
        path = self.run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else canonical(value))

    def summary(self, label):
        from disclosure_anchor.cli.m6_campaign import main

        output = self.root / label
        before = {str(p): self.sha(p.read_bytes()) for p in self.run.rglob("*") if p.is_file()}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(["summary", "--run-dir", str(self.run), "--evaluation-plan",
                         str(self.run / "evaluation-plan.json"), "--output", str(output)])
        after = {str(p): self.sha(p.read_bytes()) for p in self.run.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "summary must not rewrite its evidence")
        report_path = output / "delivery-report.json"
        return code, json.loads(report_path.read_bytes()) if report_path.exists() else None

    def test_cli_reduces_physical_journal_and_hash_bound_audits(self):
        code, report = self.summary("positive")
        self.assertEqual(code, 0)
        self.assertEqual(report["run_validity"]["status"], "complete")
        self.assertEqual((report["main_window"]["documents"], report["main_window"]["pages"]), (55, 8525))
        self.assertTrue(report["delivery_pass"], report["unknowns"])

    def test_raw_exit_identity_matrix_cannot_borrow_positive_summary(self):
        # Each mutation retains the original positive summary and every other
        # raw record. A report-shaped "verified" flag cannot repair any one.
        baseline = {p.relative_to(self.run): p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        cases = [
            ("launcher-records/process-exit.json", key, value)
            for key, value in (
                ("run_id", "foreign-run"), ("attempt_id", "foreign-attempt"),
                ("process_handle_signaled", False), ("exact_process_handle_opened", False),
                ("ready_received", False), ("parent_failure", "parent failed"),
                ("binary_sha256", m6.digest("different-binary")),
                ("configuration_sha256", m6.digest("different-config")),
                ("launcher_sha256", m6.digest("different-launcher")),
                ("pid", self.owner_pid + 1), ("creation_filetime_100ns", self.owner_birth + 1),
                ("exit_code", False), ("stdout_eof", False), ("forced_termination", True),
            )
        ] + [
            ("launcher-records/process-start.json", "pid", self.owner_pid + 1),
            ("launcher-records/ready.json", "journal_prefix_bytes", 1),
            ("campaign-inputs.json", "attempt_id", "foreign-attempt"),
            ("launcher-command.json", "intent_sha256", m6.digest("foreign-intent")),
            ("launcher-records/process-exit.json", "parent_failure", "DELETE"),
            ("launcher-records/process-exit.json", "cancel", "DELETE"),
            ("launcher-records/process-start.json", None, None),
            ("launcher-records/ready.json", None, None),
        ]
        self.assertTrue(self.summary("matrix-positive")[1]["delivery_pass"])
        for index, (name, key, value) in enumerate(cases):
            with self.subTest(file=name, field=key, value=value):
                for path, raw in baseline.items():
                    self.write(str(path), raw)
                path = self.run / name
                if key is None:
                    path.unlink()
                else:
                    changed = json.loads(path.read_bytes())
                    if value == "DELETE":
                        changed.pop(key)
                    else:
                        changed[key] = value
                    self.write(name, changed)
                code, report = self.summary("exit-matrix-" + str(index))
                self.assertEqual(code, 0)
                self.assertFalse(report["delivery_pass"])
                self.assertFalse(report["business_obligations_closed"]["external_owner_exit_verified"])
                self.assertTrue(any("owner_external_exit" in item for item in report["unknowns"]))

    def test_cli_reads_actual_stage_bytes_and_keeps_clock_and_loss_uncertainty(self):
        payloads = [record.event.payload for record in self.case.journal.records]
        admitted = {p.attempt_id: p for p in payloads if p.kind == "attempt_admitted"}
        accepted = {p.attempt_id: p for p in payloads if p.kind == "remote_accepted"}
        public = {p.attempt_id: p for p in payloads if p.kind == "public_confirmation"}
        notes, attempts = [], []
        for index, (attempt_id, admission) in enumerate(admitted.items()):
            start = 1_000_000_000_000 + index * 1_000_000_000
            terminal = start + 100_000_000_000
            notes.extend((
                {"attempt_id": attempt_id, "lane": "remote", "kind": "remote_post_send",
                 "monotonic_ns": start, "scalars": {"fence_identity": admission.fence_identity,
                    "source_pdf_sha256": admission.source_pdf_sha256,
                    "submission_intent_sha256": m6.digest("submission:" + attempt_id)}},
                {"attempt_id": attempt_id, "lane": "remote", "kind": "remote_terminal_observed",
                 "monotonic_ns": terminal, "scalars": {"fence_identity": admission.fence_identity,
                    "remote_task_identity_sha256": accepted[attempt_id].remote_task_identity_sha256,
                    "accepted_submission_receipt_sha256": accepted[attempt_id].acceptance_receipt_sha256,
                    "terminal_receipt_sha256": m6.digest("terminal:" + attempt_id), "lease_observed_at_unix": "1.0"}},
            ))
            attempts.append({"attempt_id": attempt_id, "public": {"confirmed": True, "error": None, "files": []},
                             "public_ns": terminal + 2_000_000_000,
                             "public_confirmation_sha256": public[attempt_id].canonical_sha256(),
                             "started_ns": start, "finished_ns": terminal + 50_000_000_000})
        notes.append({"attempt_id": None, "lane": None, "kind": "observation_closed",
                      "monotonic_ns": 2_000_000_000_000, "scalars": {}})
        raw = b"".join(canonical(note) + b"\n" for note in notes)
        clock = {"source": "python.time.monotonic_ns", "implementation": "mach_absolute_time()",
                 "boot_session_uuid": "11111111-1111-4111-8111-111111111111"}
        observation = {"contract_version": "staged-observation-summary.v1", "clock": clock,
                       "writer_thread_alive": False, "events_file": "stage-events.jsonl",
                       "started_monotonic_ns": 999_000_000_000, "closed_monotonic_ns": 2_000_000_000_000,
                       "measurement_status": "complete", "events_written": len(notes), "bytes_written": len(raw),
                       **dict.fromkeys(("dropped", "late_notes", "note_errors", "guard_failures", "writer_errors",
                                        "truncated", "join_timeout", "summary_write_error"), 0)}
        verification = {"status": "complete", "clock": clock, "attempts": attempts,
                        "m6_run": {"run_id": self.case.fixture.spec.run_id,
                                   "spec_sha256": self.case.fixture.spec.canonical_sha256()}}
        self.write("runner/observation/stage-events.jsonl", raw)
        self.write("runner/observation/observation-summary.json", observation)
        self.write("verifier/run-summary.json", verification)
        code, report = self.summary("with-stage-evidence")
        self.assertEqual(code, 0)
        self.assertEqual(report["latency_by_size"]["stage_timing"]["status"], "measured")
        self.assertEqual(report["latency_by_size"]["classes"]["long"]["remote_post_to_terminal"]["max_s"], 100.0)
        self.assertEqual(report["latency_by_size"]["classes"]["long"]["terminal_to_public_confirmation"]["max_s"], 2.0)
        inputs = {item["path"]: item["sha256"] for item in report["evidence"]["inputs"]}
        self.assertEqual(inputs["runner/observation/stage-events.jsonl"], self.sha(raw))
        for name, bad_observation, bad_verification, bad_raw in (
            ("lost", {**observation, "dropped": 1}, verification, raw),
            ("clock", observation, {**verification, "clock": {**clock, "boot_session_uuid":
                "22222222-2222-4222-8222-222222222222"}}, raw),
            ("wrong-byte-count", {**observation, "bytes_written": len(raw) + 1}, verification, raw),
            ("missing-count", {key: value for key, value in observation.items() if key != "dropped"}, verification, raw),
            ("malformed", observation, verification, raw + b'{"kind":'),
            ("delete-valid-line", observation, verification, raw.split(b"\n", 1)[1]),
            ("append-unscored", observation, verification, raw + canonical({
                "attempt_id": None, "lane": None, "kind": "diagnostic_only",
                "monotonic_ns": 2_000_000_000_000, "scalars": {}}) + b"\n"),
            ("no-last-newline", {**observation, "bytes_written": len(raw) - 1}, verification, raw[:-1]),
            ("bool-count", {**observation, "events_written": True}, verification, raw),
            ("writer-live", {**observation, "writer_thread_alive": True}, verification, raw),
            ("wrong-close-time", {**observation, "closed_monotonic_ns": 1}, verification, raw),
            ("wrong-verifier-run", observation, {**verification, "m6_run": {
                **verification["m6_run"], "run_id": "foreign-run"}}, raw),
            ("duplicate-verifier", observation, {**verification, "attempts": [*attempts, attempts[0]]}, raw),
        ):
            with self.subTest(fault=name):
                self.write("runner/observation/observation-summary.json", bad_observation)
                self.write("verifier/run-summary.json", bad_verification)
                self.write("runner/observation/stage-events.jsonl", bad_raw)
                code, report = self.summary("stage-" + name)
                if code:
                    self.assertIn(code, (64, 65))
                else:
                    self.assertEqual(report["latency_by_size"]["stage_timing"]["status"], "unknown")

    def test_plain_campaign_needs_no_independent_driver_hash_index(self):
        # The official run must retain the exact intent; its own frozen hash is
        # enough to find that copy without the opt-in test driver's sibling file.
        (self.root / "input-hashes.json").unlink()
        code, report = self.summary("without-test-driver")
        self.assertEqual(code, 0)
        self.assertEqual(report["run_validity"]["status"], "complete")
        self.assertTrue(report["delivery_pass"], report["unknowns"])
        self.assertEqual(report["run_validity"]["intent_sha256"], self.intent.canonical_sha256())

    def test_changed_frozen_intent_is_not_hidden_by_an_older_driver_copy(self):
        changed = type(self.intent).model_validate({**self.intent.model_dump(),
                                                   "verifier_identity": "different-verifier"})
        self.write("campaign-intent.json", changed.canonical_bytes())
        code, report = self.summary("changed-frozen-intent")
        self.assertEqual(code, 65)
        self.assertIsNone(report)

    def test_frozen_intent_must_identify_the_whole_run_not_only_its_source_list(self):
        variations = (
            {"run": {**self.intent.run.model_dump(), "planned_seconds": 4700}},
            {"runtime": {**self.intent.runtime.model_dump(), "source_commit": "a" * 40}},
        )
        for number, change in enumerate(variations):
            with self.subTest(change=change):
                changed = type(self.intent).model_validate({**self.intent.model_dump(), **change})
                self.write("campaign-intent.json", changed.canonical_bytes())
                self.write("intent.json", changed.canonical_bytes())
                inputs = json.loads((self.run / "campaign-inputs.json").read_bytes())
                self.write("campaign-inputs.json", {**inputs, "intent_sha256": changed.canonical_sha256()})
                (self.root / "input-hashes.json").write_bytes(canonical({
                    str(self.run / "intent.json"): changed.canonical_sha256(),
                }))
                code, report = self.summary("mismatched-intent-" + str(number))
                self.assertEqual(code, 65)
                self.assertIsNone(report)

    def test_cli_missing_anchor_proof_cannot_disappear_in_separate_input_index(self):
        (self.run / "run/anchor.json").unlink()
        code, report = self.summary("missing-anchor")
        if code:
            self.assertIn(code, (64, 65))
        else:
            self.assertFalse(report["delivery_pass"])
            self.assertIn("owner_anchor_absent", report["unknowns"])

    def test_cli_rejects_changed_plan_and_keeps_evidence_read_only(self):
        changed = type(self.plan).model_validate({**self.plan.model_dump(), "main_window": {
            "start_offset_seconds": 0, "end_offset_seconds": 3600}})
        self.write("evaluation-plan.json", changed.canonical_bytes())
        code, report = self.summary("bad-plan")
        self.assertEqual(code, 65)
        self.assertIsNone(report)

    def test_cli_damaged_history_or_absent_journal_never_infers_good_output(self):
        path = self.run / "verifier/public/att-t00/private-history-audit.json"
        path.write_bytes(path.read_bytes() + b" ")
        code, report = self.summary("damaged-history")
        self.assertEqual(code, 0)
        self.assertFalse(report["delivery_pass"])
        self.assertEqual(report["main_window"]["documents"], 54)
        (self.run / "native/events.jsonl").unlink()
        code, report = self.summary("no-journal")
        self.assertEqual(code, 0)
        self.assertFalse(report["delivery_pass"])
        self.assertEqual(report["main_window"]["documents"], 0)

    def test_cli_raw_nonzero_owner_exit_overrides_old_positive_summary(self):
        self.write("launcher-records/process-exit.json", {**self.exit_record, "exit_code": 9})
        code, report = self.summary("bad-exit")
        self.assertEqual(code, 0)
        self.assertFalse(report["delivery_pass"])


if __name__ == "__main__":
    unittest.main()
