"""Independent actual E1 fixture tests for functional batch orchestration only."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from threading import get_ident
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import m6_service_batch as batch
from disclosure_anchor.adapters.runtime import mineru_diagnostic_lifecycle as lifecycle
from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournal, DiagnosticJournalError
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from tests._m6_service_batch_fixture import (
    JOURNAL_BYTES, BatchFixture, canonical, digest, expected_reservation, journal_bytes,
)
from tests._mineru_diagnostic_lifecycle_fixture import (
    LifecycleFixture, SimulatedCrash, crash_after_phase, journal_records,
)


class ServiceBatchTests(unittest.TestCase):
    def fixture(self, **kwargs):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return BatchFixture(Path(temporary.name), **kwargs)

    def test_two_complete_original_pdfs_have_real_v2_disposal_and_no_quality_claim(self):
        fixture = self.fixture()
        writes = []
        caller = get_ident()
        actual_append = DiagnosticJournal.append

        def append(owner, step, value):
            writes.append((owner.root, step, get_ident()))
            return actual_append(owner, step, value)

        with patch.object(DiagnosticJournal, "append", append):
            result = fixture.run()
        self.assertEqual(result.controller.terminal, "exhausted")
        self.assertEqual((result.not_dispatched, result.unreconciled), ((), ()))
        self.assertEqual(result.qualification_scope, "functional_lifecycle_only_quality_unverified")
        self.assertEqual(result.retained_or_unresolved_credits,
                         ResourceCreditVector(temp_disk_bytes=3 * JOURNAL_BYTES))
        self.assertEqual(len({item.source_pdf_sha256 for item in fixture.inputs}), 2)
        records = journal_records(fixture.journal)
        binding = records[0]["value"]
        self.assertEqual(records[0]["step"], "batch_binding")
        self.assertEqual(binding["inputs"], fixture.original_binding_inputs())
        self.assertEqual(binding["qualification_scope"], result.qualification_scope)
        for item, observed in zip(fixture.inputs, result.controller.completed, strict=True):
            simulation = fixture.simulations[item.attempt_id]
            root = fixture.attempt_root(item.attempt_id)
            proof = fixture.proofs[item.attempt_id]
            self.assertEqual(root.parent, fixture.journal.parent)
            self.assertNotEqual(root, fixture.journal)
            self.assertEqual(binding["attempt_journals"][item.attempt_id], str(root))
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            header = json.loads((root / "00-journal.json").read_bytes())
            self.assertEqual(header["contract_version"], "mineru-diagnostic-journal.v2")
            self.assertEqual(header["deadline_ns"], fixture.deadline_ns)
            self.assertEqual(header["clock_identity_sha256"], fixture.kwargs["clock_identity_sha256"])
            phases = journal_records(root)
            self.assertEqual(phases[0]["value"]["contract_version"], "mineru-diagnostic-binding.v2")
            self.assertEqual(phases[-1]["step"], "disposed")
            self.assertEqual(proof["quality"]["status"], "unverified")
            self.assertIsNone(proof["quality"]["verifier_sha256"])
            self.assertEqual(proof["source_pdf_sha256"], item.source_pdf_sha256)
            self.assertEqual(proof["source_page_count"], 2)
            self.assertEqual(proof["provider"]["page_count"], 2)
            self.assertEqual(proof["provider"]["block_count"], 3)
            self.assertEqual(proof["ack_proof"]["kind"], "actual_response")
            self.assertEqual(observed.disposal_receipt_sha256, digest(canonical(proof)))
            self.assertEqual(simulation.events.count(("POST", "/tasks")), 1)
            self.assertEqual(simulation.ack_effects, 1)
            self.assertFalse(simulation.task_exists)
            self.assertFalse((root / "resources").exists())
            self.assertFalse((root / "resources-reclaim").exists())
            self.assertEqual(item.input_pdf.read_bytes(), simulation.source_bytes)
            self.assertIn(simulation.source_bytes, simulation.uploaded_bodies[0])
            self.assertTrue(all(stream.closed for stream in simulation.streams))
        self.assertTrue(all(thread == caller for root, _, thread in writes if root == fixture.journal))
        self.assertTrue(all(thread != caller for root, _, thread in writes if root != fixture.journal))

    def test_literal_real_e1_resource_ceiling_and_batch_journal_are_all_charged(self):
        fixture = self.fixture(count=1)
        item = fixture.inputs[0]
        work = batch.service_work_reservation(item)
        self.assertEqual(work.reservation, expected_reservation(item))
        self.assertEqual(work.retained_after_disposal,
                         ResourceCreditVector(temp_disk_bytes=JOURNAL_BYTES))
        # The whole attempt fits, but leaving no separate16MiB for batch
        # evidence is invalid before creating any batch object.
        with self.assertRaises(ValueError):
            fixture.run(credits_limit=expected_reservation(item))
        self.assertFalse(fixture.journal.exists())
        self.assertEqual(fixture.calls, [])
        self.assertTrue(all(not events for events in fixture.events.values()))

    def test_source_hash_or_physical_page_mismatch_cannot_be_called_completed(self):
        for change in ({"source_pdf_sha256": "sha256:" + "f" * 64}, {"source_page_count": 3}):
            with self.subTest(change=change):
                fixture = self.fixture(count=1)
                item = replace(fixture.inputs[0], **change)
                limit = replace(fixture.kwargs["credits_limit"], output_pages=3)
                with self.assertRaises(batch.ServiceBatchFailure) as caught:
                    fixture.run(inputs=(item,), credits_limit=limit)
                result = caught.exception.result
                self.assertEqual(result.unreconciled, (item.attempt_id,))
                self.assertEqual(result.controller.completed, ())
                simulation = fixture.simulations[item.attempt_id]
                self.assertEqual(simulation.ack_effects, 0)
                self.assertNotIn(("POST", "/tasks"), simulation.events)
                self.assertNotIn("attempt_disposed", [r["step"] for r in journal_records(fixture.journal)])

    def test_frozen_binding_changes_reject_before_attempt_or_network(self):
        fixture = self.fixture()
        fixture.run(stop_requested=lambda: True)
        original = journal_bytes(fixture.journal)
        changes = (
            {"inputs": tuple(reversed(fixture.inputs))},
            {"inputs": (replace(fixture.inputs[0], fence_identity="different"), fixture.inputs[1])},
            {"inputs": (replace(fixture.inputs[0], submission_epoch_unix=1000), fixture.inputs[1])},
            {"inputs": (replace(fixture.inputs[0], source_page_count=1), fixture.inputs[1])},
            {"api_url": "http://other.invalid"}, {"server_url": "http://other.invalid/v1"},
            {"max_in_flight": 1}, {"deadline_ns": fixture.deadline_ns + 1},
            {"clock_identity_sha256": "sha256:" + "e" * 64},
            {"options": replace(fixture.kwargs["options"], timeout_seconds=59)},
            {"credits_limit": replace(fixture.kwargs["credits_limit"], documents=3)},
        )
        for change in changes:
            with self.subTest(fields=tuple(change)), self.assertRaises((ValueError, RuntimeError)):
                fixture.run(resume=True, **change)
            self.assertEqual(journal_bytes(fixture.journal), original)
            self.assertEqual(fixture.calls, [])
        self.assertTrue(all(not events for events in fixture.events.values()))

    def test_duplicate_sources_or_attempts_and_record_count_cap_fail_before_creation(self):
        fixture = self.fixture()
        first, second = fixture.inputs
        for inputs in ((first, replace(second, source_pdf_sha256=first.source_pdf_sha256)),
                       (first, replace(second, attempt_id=first.attempt_id)), (), list(fixture.inputs)):
            with self.subTest(inputs_type=type(inputs).__name__), self.assertRaises(ValueError):
                fixture.run(inputs=inputs)
            self.assertFalse(fixture.journal.exists())
        self.assertEqual(batch.MAX_SERVICE_BATCH_INPUTS, 47)  # floor((96-1)/2)
        too_many = tuple(replace(first, attempt_id=f"bounded-{i}",
                                 source_pdf_sha256=digest(str(i).encode())) for i in range(48))
        with self.assertRaises(ValueError):
            fixture.run(inputs=too_many)
        self.assertFalse(fixture.journal.exists())

    def test_batch_capacity_is_checked_before_first_dispatch_or_provider_effect(self):
        fixture = self.fixture()
        failure = DiagnosticJournalError("independent original capacity refusal")
        with patch.object(DiagnosticJournal, "require_capacity", side_effect=failure) as capacity:
            with self.assertRaises(DiagnosticJournalError) as caught:
                fixture.run()
        self.assertIs(caught.exception, failure)
        capacity.assert_called_once_with(additional_records=4, additional_bytes=8192)
        self.assertEqual([r["step"] for r in journal_records(fixture.journal)], ["batch_binding"])
        self.assertEqual(fixture.calls, [])
        self.assertTrue(all(not events for events in fixture.events.values()))

    def test_resume_binding_only_never_supplies_undispatched_inputs(self):
        fixture = self.fixture()
        initial = fixture.run(stop_requested=lambda: True)
        original = journal_bytes(fixture.journal)
        result = fixture.run(resume=True)
        identities = tuple(item.attempt_id for item in fixture.inputs)
        self.assertEqual(initial.not_dispatched, identities)
        self.assertIsNone(result.controller)
        self.assertEqual(result.not_dispatched, identities)
        self.assertEqual(result.unreconciled, ())
        self.assertEqual(result.retained_or_unresolved_credits,
                         ResourceCreditVector(temp_disk_bytes=JOURNAL_BYTES))
        self.assertEqual(journal_bytes(fixture.journal), original)
        self.assertEqual(fixture.calls, [])

    def test_recovery_all_preexisting_dispatches_count_even_when_invocation_stops(self):
        fixture = self.fixture(max_in_flight=1)
        fixture.run(stop_requested=lambda: True)
        for item in fixture.inputs:
            fixture.dispatch_intent(item.attempt_id)
        original = journal_bytes(fixture.journal)
        result = fixture.run(resume=True, stop_requested=lambda: True)
        identities = tuple(item.attempt_id for item in fixture.inputs)
        self.assertEqual(result.unreconciled, identities)
        self.assertEqual(result.not_dispatched, ())
        self.assertEqual(result.controller.not_started, identities)
        self.assertEqual(result.controller.retained_credits, ResourceCreditVector())
        # These durable bare intents are unknown obligations, not evidence that
        # resources were absent. Batch truth must charge both full reservations.
        expected = ResourceCreditVector(temp_disk_bytes=JOURNAL_BYTES)
        for item in fixture.inputs:
            expected += expected_reservation(item)
        self.assertEqual(result.retained_or_unresolved_credits, expected)
        self.assertEqual(journal_bytes(fixture.journal), original)
        self.assertEqual(fixture.calls, [])

    def test_recovery_rejects_all_unknown_reservations_over_original_envelope(self):
        fixture = self.fixture(max_in_flight=1)
        limit = expected_reservation(fixture.inputs[0]) + ResourceCreditVector(temp_disk_bytes=2 * JOURNAL_BYTES)
        fixture.kwargs["credits_limit"] = limit
        fixture.run(stop_requested=lambda: True)
        for item in fixture.inputs:
            fixture.dispatch_intent(item.attempt_id)
        before = journal_bytes(fixture.journal)
        with self.assertRaisesRegex(DiagnosticJournalError, "reservations.*envelope"):
            fixture.run(resume=True)
        self.assertEqual(fixture.calls, [])
        self.assertEqual(journal_bytes(fixture.journal), before)
        self.assertTrue(all(not events for events in fixture.events.values()))

    def test_completed_history_reopens_on_retained_budget_without_reexecuting_or_new_io(self):
        fixture = self.fixture(max_in_flight=1)
        # Fits one full attempt plus batch and prior journal. It deliberately
        # cannot fit two historical full reservations (documents limit1).
        fixture.kwargs["credits_limit"] = expected_reservation(fixture.inputs[0]) + ResourceCreditVector(
            temp_disk_bytes=2 * JOURNAL_BYTES)
        initial = fixture.run()
        before = journal_bytes(fixture.root)
        events = fixture.events
        fixture.calls.clear()
        with patch.object(lifecycle, "DiagnosticWireClient", side_effect=AssertionError("readonly wire created")):
            replay = fixture.run(resume=True)
        self.assertIsNone(replay.controller)
        self.assertEqual(replay.previously_disposed, initial.controller.completed)
        self.assertEqual((replay.unreconciled, replay.not_dispatched), ((), ()))
        self.assertEqual(replay.retained_or_unresolved_credits,
                         ResourceCreditVector(temp_disk_bytes=3 * JOURNAL_BYTES))
        self.assertEqual(len(fixture.calls), 2)
        self.assertTrue(all(call["resume"] and call["require_disposed"] for call in fixture.calls))
        self.assertEqual(fixture.events, events)
        self.assertEqual(journal_bytes(fixture.root), before)

    def test_batch_disposal_reference_does_not_create_or_adopt_missing_attempt_proof(self):
        fixture = self.fixture(count=1)
        fixture.run(stop_requested=lambda: True)
        identity = fixture.inputs[0].attempt_id
        fixture.dispatch_intent(identity)
        fixture.append_batch("attempt_disposed", {
            "attempt_id": identity, "outcome": "completed",
            "disposal_receipt_sha256": "sha256:" + "d" * 64,
        })
        original = journal_bytes(fixture.journal)
        with self.assertRaises((OSError, ValueError)):
            fixture.run(resume=True)
        self.assertFalse(fixture.attempt_root(identity).exists())
        self.assertEqual(journal_bytes(fixture.journal), original)
        self.assertTrue(all(not events for events in fixture.events.values()))

    def test_durable_dispatch_without_attempt_creation_remains_unreconciled_and_never_posts(self):
        fixture = self.fixture(max_in_flight=1)
        with crash_after_phase("dispatch_intent"), self.assertRaises(batch.ServiceBatchFailure) as first:
            fixture.run()
        identity = fixture.inputs[0].attempt_id
        self.assertEqual(first.exception.result.unreconciled, (identity,))
        self.assertFalse(fixture.attempt_root(identity).exists())
        before = journal_bytes(fixture.journal)
        with self.assertRaises(batch.ServiceBatchFailure) as resumed:
            fixture.run(resume=True)
        result = resumed.exception.result
        self.assertEqual(result.unreconciled, (identity,))
        self.assertEqual(result.not_dispatched, (fixture.inputs[1].attempt_id,))
        self.assertEqual(result.retained_or_unresolved_credits,
                         expected_reservation(fixture.inputs[0]) + ResourceCreditVector(temp_disk_bytes=JOURNAL_BYTES))
        self.assertFalse(fixture.attempt_root(identity).exists())
        self.assertEqual(journal_bytes(fixture.journal), before)
        self.assertTrue(all(not events for events in fixture.events.values()))

    def test_well_formed_batch_reference_must_equal_actual_original_disposal_proof(self):
        for field, value in (("disposal_receipt_sha256", "sha256:" + "d" * 64),
                             ("outcome", "failed")):
            with self.subTest(field=field):
                fixture = self.fixture(count=1)
                fixture.run()
                original_attempt = journal_bytes(fixture.attempt_root(fixture.inputs[0].attempt_id))
                last = sorted(fixture.journal.glob("[0-9][0-9][0-9][0-9]-*.json"))[-1]
                record = json.loads(last.read_bytes())
                self.assertEqual(record["step"], "attempt_disposed")
                record["value"][field] = value
                record["value_sha256"] = digest(canonical(record["value"]))
                # Correct canonical bytes/value hash/chain predecessor: this
                # models divergent semantics, not a JSON corruption shortcut.
                last.write_bytes(canonical(record))
                corrupted_batch = journal_bytes(fixture.journal)
                events = fixture.events
                with self.assertRaisesRegex(DiagnosticJournalError, "proof differs"):
                    fixture.run(resume=True)
                self.assertEqual(fixture.events, events)
                self.assertEqual(journal_bytes(fixture.journal), corrupted_batch)
                self.assertEqual(journal_bytes(fixture.attempt_root(fixture.inputs[0].attempt_id)),
                                 original_attempt)

    def test_lost_submit_response_recovers_only_same_original_key_without_new_post(self):
        fixture = self.fixture(max_in_flight=1)
        identity = fixture.inputs[0].attempt_id
        simulation = fixture.simulations[identity]
        simulation.lose_submit_response = True
        with self.assertRaises(batch.ServiceBatchFailure):
            fixture.run()
        key = simulation.fields["agent_idempotency_key"]
        original_header = (fixture.attempt_root(identity) / "00-journal.json").read_bytes()
        resumed = fixture.run(resume=True)
        self.assertEqual(resumed.unreconciled, ())
        self.assertEqual(resumed.not_dispatched, (fixture.inputs[1].attempt_id,))
        self.assertEqual(resumed.controller.completed[0].attempt_id, identity)
        self.assertEqual(simulation.events.count(("POST", "/tasks")), 1)
        self.assertEqual(simulation.ack_effects, 1)
        self.assertTrue(any(path == "/tasks/by-idempotency/" + key for _, path in simulation.events))
        self.assertEqual((fixture.attempt_root(identity) / "00-journal.json").read_bytes(), original_header)
        self.assertEqual(resumed.retained_or_unresolved_credits,
                         ResourceCreditVector(temp_disk_bytes=2 * JOURNAL_BYTES))

    def test_recovery_preserves_original_deadline_and_no_partial_renewal(self):
        fixture = self.fixture(count=1)
        simulation = next(iter(fixture.simulations.values()))
        simulation.lose_submit_response = True
        with self.assertRaises(batch.ServiceBatchFailure):
            fixture.run()
        events = fixture.events
        before = journal_bytes(fixture.root)
        fixture.now_ns = fixture.deadline_ns
        with self.assertRaises(TimeoutError):
            fixture.run(resume=True)
        self.assertEqual(fixture.events, events)
        self.assertEqual(journal_bytes(fixture.root), before)
        self.assertEqual(simulation.ack_effects, 0)


class ReadOnlyDisposedInspectionTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return LifecycleFixture(Path(temporary.name))

    def test_require_disposed_needs_resume_and_exact_boolean_before_any_creation(self):
        for overrides in ({"require_disposed": True}, {"require_disposed": 1, "resume": True}):
            fixture = self.fixture()
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                fixture.run(**overrides)
            self.assertFalse(fixture.journal.exists())
            self.assertEqual(fixture.events, [])

    def test_readonly_final_proof_matches_exact_original_without_wire_append_or_source_probe(self):
        fixture = self.fixture()
        original = fixture.run()
        before = journal_bytes(fixture.journal)
        events = list(fixture.events)
        with patch.object(lifecycle, "DiagnosticWireClient", side_effect=AssertionError("wire created")), \
             patch.object(lifecycle, "observe_diagnostic_source", side_effect=AssertionError("source probed")), \
             patch.object(DiagnosticJournal, "append", side_effect=AssertionError("journal appended")):
            actual = fixture.run(resume=True, require_disposed=True)
        self.assertEqual(actual, original)
        self.assertEqual(journal_bytes(fixture.journal), before)
        self.assertEqual(fixture.events, events)

    def test_unfinished_local_removed_cut_cannot_turn_readonly_inspection_into_ack(self):
        fixture = self.fixture()
        with crash_after_phase("local_removed"), self.assertRaises(SimulatedCrash):
            fixture.run()
        before = journal_bytes(fixture.journal)
        events = list(fixture.events)
        with patch.object(lifecycle, "DiagnosticWireClient", side_effect=AssertionError("wire created")):
            with self.assertRaisesRegex(ValueError, "not durably disposed"):
                fixture.run(resume=True, require_disposed=True)
        self.assertEqual(fixture.events, events)
        self.assertEqual(journal_bytes(fixture.journal), before)
        self.assertEqual(fixture.ack_effects, 0)
        # The unchanged default v2 recovery still has its original authority.
        proof = fixture.run(resume=True)
        self.assertEqual(proof["outcome"], "completed")
        self.assertEqual(fixture.ack_effects, 1)

    def test_final_inspection_keeps_original_deadline_and_rejects_clock_regression(self):
        for now in (70_000_000_000, 9_999_999_999):
            fixture = self.fixture()
            fixture.run()
            before = journal_bytes(fixture.journal)
            events = list(fixture.events)
            fixture.now_ns = now
            with self.subTest(now=now), self.assertRaises((TimeoutError, ValueError)):
                fixture.run(resume=True, require_disposed=True)
            self.assertEqual(fixture.events, events)
            self.assertEqual(journal_bytes(fixture.journal), before)

    def test_last_clock_callback_cannot_hide_reappeared_resources_or_quarantine(self):
        for name in ("resources", "resources-reclaim"):
            fixture = self.fixture()
            fixture.run()
            path = fixture.journal / name
            events = list(fixture.events)

            def clock():
                path.mkdir(mode=0o700)
                (path / "unremoved").write_bytes(b"preserve this exact unknown resource")
                return fixture.now_ns

            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "reappeared"):
                fixture.run(resume=True, require_disposed=True, continuous_ns=clock)
            self.assertEqual((path / "unremoved").read_bytes(), b"preserve this exact unknown resource")
            self.assertEqual(fixture.events, events)


if __name__ == "__main__":
    unittest.main()
