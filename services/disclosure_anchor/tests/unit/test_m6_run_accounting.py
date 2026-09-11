from __future__ import annotations

import unittest

from disclosure_anchor.application.contracts.m6_run_events import M6AdmissionControl, M6OwnerResumed, M6RunEvent
from tests.m6_support import RunExample, changed, golden, sha


class M6RunAccountingTests(unittest.TestCase):
    def test_golden_full_source_whole_run_and_deterministic_replay(self):
        example = golden()
        result = example.replay()
        self.assertEqual(result.status, "complete")
        self.assertEqual((result.metrics.window_pages, result.metrics.whole_run_pages), (7, 7))
        self.assertEqual(result.elapsed_ticks, 36300)
        self.assertEqual(result.qpc_frequency_hz, 10)
        self.assertEqual(example.replay().canonical_bytes(), result.canonical_bytes())

    def test_half_open_window_and_late_quality_cannot_backfill(self):
        example = RunExample()
        example.start()
        example.document(confirmed_tick=36099, qualified_tick=36100)
        example.close()
        result = example.replay()
        self.assertEqual(result.status, "complete")
        self.assertEqual((result.metrics.window_pages, result.metrics.whole_run_pages), (0, 7))
        self.assertEqual(result.sources[0].ready_received_ticks, 36100)

    def test_complete_zero_is_distinct_from_missing_cleanup(self):
        example = RunExample()
        example.start()
        example.close()
        result = example.replay()
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.metrics.whole_run_pages, 0)
        for kind, reason in (("attempt_final", "cleanup_or_ack_unresolved"),
                            ("verifier_drained", "verifier_not_drained"), ("run_closed", "run_not_closed")):
            case = golden()
            case.records = [r for r in case.records if r.event.payload.kind != kind]
            result = case.replay()
            self.assertIn(reason, result.incomplete_reasons)
            self.assertIsNone(result.metrics)

    def test_global_history_is_not_reset_by_new_run_or_profile(self):
        example = golden()
        example.history[0] = changed(example.history[0], first_processing_run_id="old-run", first_ledger_seq=1)
        result = example.replay()
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.sources[0].outcome, "not_first_publish")
        self.assertEqual(result.metrics.whole_run_pages, 0)
        example.history[0] = changed(example.history[0], scan_complete=False)
        result = example.replay()
        self.assertIn("history_scan_incomplete", result.incomplete_reasons)
        self.assertIsNone(result.metrics)

    def test_conflicting_page_counts_are_incomplete_not_partial_good_pages(self):
        for kind in ("publication_committed", "public_confirmation"):
            example = golden()
            example.rewrite(kind, payload_updates={"source_page_count": 8})
            result = example.replay()
            self.assertIn("page_count_conflict", result.incomplete_reasons)
            self.assertIsNone(result.metrics)
        example = golden()
        example.history[0] = changed(example.history[0], source_page_variants=2)
        self.assertIn("page_count_conflict", example.replay().incomplete_reasons)

    def test_source_winner_public_bytes_and_history_must_refer_to_same_publication(self):
        for update in ({"source_pdf_sha256": sha("other")}, {"processing_run_id": "other-run"},
                       {"winner_sha256": sha("winner-other")}, {"public_units_sha256": sha("other-units")},
                       {"history_audit_receipt_sha256": sha("other-history")}):
            example = golden()
            example.rewrite("public_confirmation", payload_updates=update)
            result = example.replay()
            self.assertEqual(result.status, "invalid", update)
            self.assertIsNone(result.metrics)

    def test_duplicate_records_and_producer_retries_credit_once(self):
        example = golden()
        example.records.insert(6, example.records[5])
        result = example.replay()
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.duplicate_events, 1)
        self.assertEqual(result.metrics.whole_run_pages, 7)
        other = golden()
        record = other.records[5]
        event = changed(record.event, payload=changed(record.event.payload, winner_sha256=sha("different")))
        conflict = M6RunEvent(event=event, stamp=changed(record.stamp, producer_event_sha256=event.canonical_sha256()))
        other.records.insert(6, conflict)
        self.assertIn("owner_sequence_conflict", other.replay().invalid_reasons)

    def test_owner_journal_reorder_gap_and_clock_regression_are_visible(self):
        example = golden()
        example.records[4], example.records[5] = example.records[5], example.records[4]
        self.assertIn("owner_record_reordered", example.replay().invalid_reasons)
        example = golden()
        del example.records[4]
        self.assertIn("event_gap", example.replay().incomplete_reasons)
        example = golden()
        example.rewrite("public_confirmation", stamp_updates={"received_qpc_ticks": 249})
        self.assertIn("clock_regression", example.replay().invalid_reasons)

    def test_producer_incarnation_is_part_of_dedup_identity(self):
        example = golden()
        example.rewrite("public_confirmation", event_updates={"producer_epoch_sha256": sha("new-verifier"), "producer_sequence": 1})
        # Drain belongs to old verifier and cannot silently lose its original seq=1.
        self.assertIn("producer_sequence_gap", example.replay().incomplete_reasons)
        example.rewrite("verifier_drained", event_updates={"producer_epoch_sha256": sha("new-verifier")})
        self.assertEqual(example.replay().status, "complete")

    def test_same_boot_resume_keeps_cost_and_original_deadline(self):
        example = RunExample()
        example.start()
        old_epoch = example.owner_epoch
        example.add(M6OwnerResumed(clock=example.clock, t0_ticks=100, deadline_ticks=36100,
                    previous_owner_epoch_sha256=old_epoch), 150, owner_epoch=sha("owner-2"))
        example.document()
        example.close()
        result = example.replay()
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.elapsed_ticks, 36300)
        example.rewrite("owner_resumed", payload_updates={"deadline_ticks": 36101})
        self.assertIn("original_clock_or_deadline_drift", example.replay().invalid_reasons)
        example.rewrite("owner_resumed", payload_updates={"deadline_ticks": 36100, "previous_owner_epoch_sha256": sha("wrong-predecessor")})
        self.assertIn("invalid_owner_resume", example.replay().invalid_reasons)

    def test_different_boot_cannot_produce_combined_qpc_elapsed_or_numerator(self):
        example = golden()
        example.rewrite("public_confirmation", stamp_updates={"boot_identity_sha256": sha("boot-2")})
        result = example.replay()
        self.assertIn("boot_identity_changed", result.incomplete_reasons)
        self.assertIsNone(result.elapsed_ticks)
        self.assertIsNone(result.metrics)

    def test_scope_profile_mode_and_ack_identity_fail_closed(self):
        for kind, updates in (("attempt_admitted", {"source_pdf_sha256": sha("outside")}),
                ("attempt_admitted", {"process_profile_sha256": sha("different-profile")}),
                ("attempt_final", {"remote_task_identity_sha256": sha("different-task")}),
                ("attempt_final", {"outcome": "diagnostic_disposed"})):
            example = golden()
            example.rewrite(kind, payload_updates=updates)
            self.assertEqual(example.replay().status, "invalid", updates)

    def test_truncated_noncanonical_and_oversized_wire_never_exposes_metrics(self):
        example = golden()
        lines = [record.canonical_bytes()+b"\n" for record in example.records]
        result = example.replay(raw=[*lines, b'{"partial":'])
        self.assertIn("event_log_truncated", result.incomplete_reasons)
        self.assertIsNone(result.metrics)
        result = example.replay(raw=[*lines, b'{}\n'])
        self.assertIn("malformed_event_record", result.invalid_reasons)
        result = example.replay(raw=[*lines, b"x"*20000])
        self.assertIn("event_log_bound_exceeded", result.incomplete_reasons)
        self.assertEqual(result.journal_bytes_consumed, sum(map(len, lines)))
        self.assertEqual(result.events_total, len(lines))

    def test_fresh_admission_after_effective_stop_is_invalid(self):
        example = RunExample()
        example.start()
        example.add(M6AdmissionControl(kind="stop_admission_effective"), 150)
        example.document()
        result = example.replay()
        self.assertIn("admission_outside_window", result.invalid_reasons)
        self.assertIsNone(result.metrics)

    def test_qualification_failures_have_matching_source_outcome(self):
        example = golden()
        example.evidence = []
        result = example.replay()
        self.assertIn("qualification_evidence_missing", result.incomplete_reasons)
        self.assertEqual(result.sources[0].outcome, "qualification_missing")
        example = golden()
        evidence = changed(example.evidence[0], observation=changed(
            example.evidence[0].observation, source_byte_count=1001))
        example.evidence = [evidence]
        example.rewrite("document_qualified", payload_updates={
            "qualification_evidence_sha256": evidence.canonical_sha256()})
        result = example.replay()
        self.assertIn("qualification_source_mismatch", result.invalid_reasons)
        self.assertEqual(result.sources[0].outcome, "quality_not_scorable")
        example = golden(service=True)
        example.rewrite("service_validated", payload_updates={"provider_bundle_sha256": sha("wrong")})
        result = example.replay()
        self.assertIn("service_provider_bundle_mismatch", result.invalid_reasons)
        self.assertEqual(result.sources[0].outcome, "quality_not_scorable")

    def test_io_failure_is_not_swallowed_as_successful_prefix(self):
        example = golden()
        def lines():
            yield example.records[0].canonical_bytes()+b"\n"
            raise OSError("simulated failed disk read")
        with self.assertRaisesRegex(OSError, "failed disk"):
            example.replay(raw=lines())

    def test_service_replay_is_labeled_and_never_claims_first_publication(self):
        example = golden(service=True, origin="replay")
        result = example.replay()
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.metrics.kind, "service_validated_source_pages")
        self.assertEqual(result.metrics.replay_pages, 7)
        self.assertEqual(result.metrics.whole_run_pages, 7)
        self.assertFalse(hasattr(result.metrics, "first_durable_publish"))

    def test_carry_in_is_separate_and_unresolved_carry_in_keeps_run_incomplete(self):
        example = golden(origin="carry_in")
        result = example.replay()
        self.assertEqual(result.status, "complete")
        self.assertEqual((result.metrics.whole_run_pages, result.metrics.carry_in_pages), (0, 7))
        example.records = [record for record in example.records if record.event.payload.kind != "attempt_final"]
        self.assertIn("cleanup_or_ack_unresolved", example.replay().incomplete_reasons)

    def test_late_evidence_after_claimed_drain_cannot_leave_complete_receipt(self):
        example = golden()
        record = example.records[5]
        event = changed(record.event, producer_sequence=3)
        late = M6RunEvent(event=event, stamp=changed(record.stamp,
            sequence=len(example.records)+1, received_qpc_ticks=36401, producer_event_sha256=event.canonical_sha256()))
        example.records.append(late)
        result = example.replay()
        self.assertIn("event_after_close", result.invalid_reasons)
        self.assertIsNone(result.metrics)

    def test_stop_propagation_admission_is_excluded_but_cost_and_cleanup_remain(self):
        example = golden()
        for i, record in enumerate(example.records):
            kind = record.event.payload.kind
            if kind in {"attempt_admitted", "remote_accepted", "publication_committed", "public_confirmation", "document_qualified", "attempt_final"}:
                example.records[i] = changed(record, stamp=changed(record.stamp, received_qpc_ticks=36100))
        result = example.replay()
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.sources[0].outcome, "admitted_after_deadline")
        self.assertEqual(result.sources[0].admission_received_ticks, 36100)
        self.assertEqual((result.metrics.window_pages, result.metrics.whole_run_pages), (0, 0))
        self.assertEqual(result.elapsed_ticks, 36300)
        self.assertEqual(result.stop_effective_ticks, 36100)
        for i, record in enumerate(example.records):
            if 3 <= record.stamp.sequence <= 9:
                example.records[i] = changed(record, stamp=changed(record.stamp, received_qpc_ticks=36111))
        self.assertIn("stop_admission_budget_exceeded", example.replay().incomplete_reasons)

    def test_stop_metadata_distinguishes_early_stop_from_full_supply(self):
        example = RunExample(phase="hour_baseline")
        example.start()
        example.document()
        example.add(M6AdmissionControl(kind="stop_admission_requested"), 1000)
        example.add(M6AdmissionControl(kind="stop_admission_effective"), 1001)
        example.close()
        # Remove the automatic second stop; renumber the physical append order.
        del example.records[-4]
        example.records = [changed(record, stamp=changed(record.stamp, sequence=i+1)) for i, record in enumerate(example.records)]
        # Keep owner producer sequences continuous after removing the extra stop.
        for kind in ("resources_closed", "run_closed"):
            record = next(r for r in example.records if r.event.payload.kind == kind)
            example.rewrite(kind, event_updates={"producer_sequence": record.event.producer_sequence-1})
        result = example.replay()
        self.assertEqual(result.status, "complete")
        self.assertEqual((result.stop_requested_ticks, result.stop_effective_ticks), (1000, 1001))
        self.assertEqual(result.close_reason, "deadline_drained")
        self.assertLess(result.stop_effective_ticks, result.deadline_ticks)

    def test_deep_json_and_zero_elapsed_produce_fail_closed_receipts(self):
        example = golden()
        example.spec = changed(example.spec, resources=changed(example.spec.resources,
            max_record_bytes=1048576, max_log_bytes=2097152))
        result = example.replay(raw=[b"["*100000+b"]"*100000+b"\n"])
        self.assertIn("malformed_event_record", result.invalid_reasons)
        example = RunExample()
        example.start()
        example.close()
        for i, record in enumerate(example.records):
            event = record.event
            if event.payload.kind == "run_closed":
                event = changed(event, payload=changed(event.payload, tclose_ticks=100))
            example.records[i] = M6RunEvent(event=event, stamp=changed(record.stamp,
                received_qpc_ticks=100, producer_event_sha256=event.canonical_sha256()))
        result = example.replay()
        self.assertIn("zero_length_or_negative_run", result.invalid_reasons)
        self.assertIsNone(result.metrics)

    def test_producer_conflict_and_postclose_retry_have_distinct_failure_reasons(self):
        for after_close in (False, True):
            example = golden()
            original = example.records[5]
            event = original.event if after_close else changed(original.event,
                payload=changed(original.event.payload, winner_sha256=sha("conflicting-winner")))
            index = len(example.records) if after_close else 6
            record = M6RunEvent(event=event, stamp=changed(original.stamp, sequence=index+1,
                received_qpc_ticks=36401 if after_close else 300, producer_event_sha256=event.canonical_sha256()))
            example.records.insert(index, record)
            example.records = [changed(record, stamp=changed(record.stamp, sequence=i+1)) for i, record in enumerate(example.records)]
            result = example.replay()
            self.assertIn("event_after_close" if after_close else "producer_event_conflict", result.invalid_reasons)
            self.assertIsNone(result.metrics)

    def test_service_two_attempts_same_source_credit_original_pages_once(self):
        example = RunExample(service=True)
        example.start()
        example.document()
        start = len(example.records)
        example.document()
        example.evidence.pop()  # Both attempts used the same immutable quality observation.
        for i in range(start, len(example.records)):
            record = example.records[i]
            event = changed(record.event, payload=changed(record.event.payload, attempt_id="attempt-retry"))
            example.records[i] = M6RunEvent(event=event, stamp=changed(record.stamp,
                received_qpc_ticks=record.stamp.received_qpc_ticks+1000, producer_event_sha256=event.canonical_sha256()))
        example.close()
        result = example.replay()
        self.assertEqual(result.status, "complete")
        self.assertEqual(len(result.sources), 2)
        self.assertEqual(result.metrics.whole_run_pages, 7)

    def test_missing_or_cross_source_qualification_and_record_bound(self):
        example = golden()
        example.evidence.clear()
        self.assertIn("qualification_evidence_missing", example.replay().incomplete_reasons)
        example = golden()
        example.evidence[0] = changed(example.evidence[0], observation=changed(example.evidence[0].observation, source_pdf_sha256=sha("wrong-source")))
        example.rewrite("document_qualified", payload_updates={"qualification_evidence_sha256": example.evidence[0].canonical_sha256()})
        self.assertIn("qualification_source_mismatch", example.replay().invalid_reasons)
        example = golden()
        example.spec = changed(example.spec, resources=changed(example.spec.resources, max_events=7, max_attempts=7))
        for i, record in enumerate(example.records):
            event = changed(record.event, spec_sha256=example.spec.canonical_sha256())
            example.records[i] = M6RunEvent(event=event, stamp=changed(record.stamp, producer_event_sha256=event.canonical_sha256()))
        result = example.replay()
        self.assertIn("event_log_bound_exceeded", result.incomplete_reasons)
        self.assertIsNone(result.metrics)

    def test_e2e_replay_and_unqualified_documents_do_not_supply_credit(self):
        example = golden(origin="replay")
        result = example.replay()
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.sources[0].outcome, "replay")
        self.assertEqual(result.metrics.whole_run_pages, 0)
        example.history[0] = changed(example.history[0], first_processing_run_id="historical-run")
        self.assertEqual(example.replay().sources[0].outcome, "not_first_publish")
        example = golden()
        example.records = [r for r in example.records if r.event.payload.kind != "document_qualified"]
        self.assertEqual(example.replay().sources[0].outcome, "qualification_missing")


if __name__ == "__main__":
    unittest.main()
