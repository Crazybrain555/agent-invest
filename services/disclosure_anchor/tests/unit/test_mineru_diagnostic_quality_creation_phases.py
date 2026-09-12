"""Independent v3 creation schema/replay tests; no store or child authority.

Literal expected receipts extend actual temporary E1 journal records. Retained
identities below are declared structural data, deliberately not IO receipts.
Store creation, payload integrity and closure belong to independent store tests.
"""

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest

from disclosure_anchor.adapters.runtime.mineru_diagnostic_journal import DiagnosticJournal
from disclosure_anchor.adapters.runtime.mineru_diagnostic_phases import DiagnosticPhases
from tests._mineru_held_inputs_fixture import CLOCK, HeldInputFixture, canonical, digest, identity
from tests._mineru_quality_store_fixture import QualityStoreFixture


STEPS = ("quality_intent", "quality_root_created", "quality_files_created", "quality_input_sealed")
SLOTS = (
    "input.json", "producer.request.json", "producer.source.json", "producer.build.json",
    "producer.reads.json", "producer.control.raw", "producer.stderr.raw",
    "verifier.request.json", "verifier.source.json", "verifier.build.json",
    "verifier.reads.json", "verifier.control.raw", "verifier.stderr.raw",
    "comparison.json", "qualification.json", "result.json",
)
BLOCKED = (
    "producer_intent", "producer_launched", "producer_closed", "producer_sealed",
    "verifier_intent", "verifier_launched", "verifier_closed", "verifier_sealed",
    "quality_producer_intent", "quality_verifier_intent", "quality_comparison_sealed",
    "quality_qualification_sealed", "quality_result_sealed", "quality_final",
    "validated", "cleanup_intent", "local_removed", "ack_intent",
    "ack_exchange_intent", "ack_reply", "ack_lookup", "remote_absent", "disposed",
)


def creation_payload(fixture, step):
    """Hand-derived contract fields; never call the production payload builder."""
    index = STEPS.index(step)
    basis = "output_sealed" if index == 0 else STEPS[index - 1]
    value = {
        "contract_version": "mineru-owned-quality.phase.v1",
        "configuration_sha256": digest(canonical(fixture.binding)),
        "basis_record_sha256": fixture.phases.latest[basis].sha256,
    }
    if step == "quality_intent":
        value.update(retained_parent_identity=identity(fixture.root), retained_name="journal.quality",
                     snapshot_record_sha256=fixture.phases.latest["snapshot_sealed"].sha256,
                     output_record_sha256=fixture.phases.latest["output_sealed"].sha256)
    elif step == "quality_root_created":
        value["root_identity"] = [101, 900, 0o40700, os.getuid()]
    elif step == "quality_files_created":
        value["files"] = [{"slot": slot, "identity": [101, 1000 + i, 0o100600, os.getuid()]}
                          for i, slot in enumerate(SLOTS)]
    else:
        value["input_file"] = {
            "slot": "input.json", "identity": [101, 1000, 0o100600, os.getuid()],
            "byte_count": 1, "sha256": digest(b"x"), "evidence_kind": "complete",
        }
    return value


class QualityCreationPhasesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="m6-quality-phases-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.number = 0

    def fixture(self, *, legacy=False, **kwargs):
        self.number += 1
        cls = HeldInputFixture if legacy else QualityStoreFixture
        fixture = cls(self.base / str(self.number), **kwargs)
        self.addCleanup(fixture.close)
        return fixture

    def advance(self, fixture, count=4):
        for step in STEPS[:count]:
            fixture.phases.append(step, creation_payload(fixture, step))

    def rejected(self, fixture, step, value):
        before = fixture.persisted()
        history = tuple(fixture.phases.history)
        with self.assertRaises(ValueError):
            fixture.phases.append(step, value)
        self.assertEqual(fixture.persisted(), before, "invalid phase changed durable bytes")
        self.assertEqual(tuple(fixture.phases.history), history, "invalid phase changed accepted cache")

    def independent_journal(self, binding, *, header_sha=None):
        self.number += 1
        parent = self.base / ("owner-" + str(self.number))
        parent.mkdir(mode=0o700)
        owner = DiagnosticJournal(
            parent / "journal", create=True, attempt_id="phase-test",
            configuration_sha256=digest(canonical(binding)) if header_sha is None else header_sha,
            clock_identity_sha256=CLOCK, deadline_ns=9_000_000_000, continuous_ns=lambda: 100,
        )
        self.addCleanup(owner.close)
        return owner

    def test_actual_e1_v3_binding_precedes_resources_and_keeps_original_header(self):
        fixture = self.fixture()
        self.assertEqual(fixture.phases.history[0].step, "binding")
        self.assertEqual(fixture.phases.history[0].value, fixture.binding)
        self.assertEqual(fixture.phases.history[-1].step, "output_sealed")
        self.assertNotIn("quality_verifier_sha256", fixture.binding)
        self.assertEqual(fixture.phases.owned_quality.to_payload(), fixture.binding["owned_quality"])
        original = fixture.journal.original_identity
        self.assertEqual(original.configuration_sha256, digest(canonical(fixture.binding)))
        self.assertNotEqual(original.configuration_sha256, digest(canonical(fixture.binding["owned_quality"])))
        self.assertEqual((original.started_ns, original.deadline_ns), (100, fixture.deadline))
        self.assertEqual(fixture.phases.inventory()[-len(fixture.output_inventory):], fixture.output_inventory)

    def test_four_literal_receipts_replay_at_each_cut_without_new_records(self):
        fixture = self.fixture()
        original = fixture.journal.original_identity
        expected = {}
        for step in STEPS:
            expected[step] = creation_payload(fixture, step)
            receipt = fixture.phases.append(step, expected[step])
            self.assertEqual(receipt.value, expected[step])
            before = fixture.persisted()
            if fixture.resources is not None:
                fixture.resources.close()
                fixture.resources = None
            fixture.journal.close()
            fixture.journal = DiagnosticJournal(
                fixture.root / "journal", create=False, attempt_id=original.attempt_id,
                configuration_sha256=original.configuration_sha256, clock_identity_sha256=CLOCK,
                deadline_ns=original.deadline_ns, continuous_ns=lambda: fixture.now,
            )
            fixture.refresh()
            self.assertEqual(fixture.persisted(), before)
            self.assertEqual(fixture.phases.history[-1].step, step)
            self.assertEqual(fixture.journal.original_identity, original)
            for accepted, payload in expected.items():
                self.assertEqual(fixture.phases.value(accepted), payload)
        self.assertEqual([x["slot"] for x in expected["quality_files_created"]["files"]], list(SLOTS))

    def test_v3_binding_fields_config_and_original_header_are_not_interchangeable(self):
        fixture = self.fixture()
        original = deepcopy(fixture.binding)
        mutations = []
        for key in original:
            if key != "contract_version":
                changed = deepcopy(original)
                del changed[key]
                mutations.append(("missing-" + key, changed))
        for extra in ("quality_verifier_sha256", "publication_authority"):
            mutations.append((extra, {**original, extra: None}))
        for version in (None, "mineru-diagnostic-binding.v1", "mineru-diagnostic-binding.v4", "", True):
            mutations.append(("unsupported-version", {**original, "contract_version": version}))
        without_version = deepcopy(original)
        del without_version["contract_version"]
        mutations.append(("missing-version", without_version))
        bad_config = deepcopy(original)
        bad_config["owned_quality"]["unexpected"] = True
        mutations.append(("config-unknown", bad_config))
        for label, binding in mutations:
            with self.subTest(label=label):
                owner = self.independent_journal(binding)
                before = owner.records
                with self.assertRaises(ValueError):
                    DiagnosticPhases(owner, binding)
                self.assertEqual(owner.records, before)
        for sha in (digest(canonical(original["owned_quality"])), "sha256:" + "0" * 64):
            with self.subTest(wrong_header_domain=sha):
                with self.assertRaises(ValueError):
                    DiagnosticPhases(fixture.journal, {**original, "source_page_count": 3})
                owner = self.independent_journal(original, header_sha=sha)
                with self.assertRaises(ValueError):
                    DiagnosticPhases(owner, original)

    def test_retained_name_is_derived_from_actual_journal_basename(self):
        for name in ("other.quality", "journal", "journal.quality.extra"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.fixture(retained_name=name)
        fixture = self.fixture()
        self.assertEqual(fixture.config.retained_name, fixture.journal.root.name + ".quality")

    def test_common_and_stage_specific_fields_are_closed_before_append(self):
        fixture = self.fixture()
        for step in STEPS:
            valid = creation_payload(fixture, step)
            for field in valid:
                changed = deepcopy(valid)
                del changed[field]
                with self.subTest(step=step, missing=field):
                    self.rejected(fixture, step, changed)
            for extra in ("identity", "basis_sha256", "quality_verifier_sha256", "authority"):
                with self.subTest(step=step, extra=extra):
                    self.rejected(fixture, step, {**valid, extra: None})
            fixture.phases.append(step, valid)

    def test_every_creation_phase_binds_version_header_hash_and_exact_previous_record(self):
        fixture = self.fixture()
        for step in STEPS:
            valid = creation_payload(fixture, step)
            for field, wrong in (
                ("contract_version", "mineru-owned-quality.phase.v2"),
                ("configuration_sha256", digest(canonical(fixture.binding["owned_quality"]))),
                ("configuration_sha256", fixture.journal.original_identity.header_sha256),
                ("basis_record_sha256", fixture.phases.latest["snapshot_sealed"].sha256),
                ("basis_record_sha256", "sha256:" + "0" * 64),
            ):
                with self.subTest(step=step, field=field, wrong=wrong):
                    self.rejected(fixture, step, {**valid, field: wrong})
            fixture.phases.append(step, valid)

    def test_intent_binds_both_original_seals_and_retained_name(self):
        fixture = self.fixture()
        valid = creation_payload(fixture, STEPS[0])
        for field, wrong in (
            ("snapshot_record_sha256", fixture.phases.latest["output_sealed"].sha256),
            ("snapshot_record_sha256", fixture.snapshot["sha256"]),
            ("output_record_sha256", fixture.phases.value("output_sealed")["inventory_sha256"]),
            ("output_record_sha256", fixture.phases.latest["snapshot_sealed"].sha256),
            ("retained_name", "unrelated.quality"),
        ):
            with self.subTest(field=field):
                self.rejected(fixture, STEPS[0], {**valid, field: wrong})
        fixture.phases.append(STEPS[0], valid)

    def test_creation_cannot_skip_output_or_any_prior_creation_phase(self):
        unfinished = self.fixture(finish=False)
        value = {"contract_version": "mineru-owned-quality.phase.v1",
                 "configuration_sha256": digest(canonical(unfinished.binding)),
                 "basis_record_sha256": unfinished.phases.latest["snapshot_sealed"].sha256,
                 "retained_parent_identity": identity(unfinished.root), "retained_name": "journal.quality",
                 "snapshot_record_sha256": unfinished.phases.latest["snapshot_sealed"].sha256,
                 "output_record_sha256": "sha256:" + "0" * 64}
        self.rejected(unfinished, STEPS[0], value)
        complete = self.fixture()
        self.advance(complete)
        for index, step in enumerate(STEPS[1:]):
            fixture = self.fixture()
            self.advance(fixture, index)
            with self.subTest(skipped_before=step):
                self.rejected(fixture, step, complete.phases.value(step))

    def test_parent_identity_is_directory_data_not_an_invented_private_parent_rule(self):
        fixture = self.fixture()
        valid = creation_payload(fixture, STEPS[0])
        for bad in (None, (1, 2, 0o40755, 3), [1, 2, 0o40755], [True, 2, 0o40755, 3],
                    [1, 0, 0o40755, 3], [1, 2, 0o100600, 3], [1, 2, 0o40755, 2**63]):
            with self.subTest(identity=bad):
                self.rejected(fixture, STEPS[0], {**valid, "retained_parent_identity": bad})
        # Structural acceptance does not assert that this declared parent is open.
        valid["retained_parent_identity"] = [0, 1, 0o40755, os.getuid() + 1]
        fixture.phases.append(STEPS[0], valid)

    def test_root_identity_requires_private_directory_current_uid_and_strict_scalars(self):
        fixture = self.fixture()
        self.advance(fixture, 1)
        valid = creation_payload(fixture, STEPS[1])
        wrongs = [None, tuple(valid["root_identity"]), [1, 2, 0o40700],
                  [1, 0, 0o40700, os.getuid()], [1, 2, 0o100600, os.getuid()],
                  [1, 2, 0o40755, os.getuid()], [1, 2, 0o40700, os.getuid() + 1]]
        for index in range(4):
            for bad in (True, -1, 2**63, 1.0):
                changed = list(valid["root_identity"])
                changed[index] = bad
                wrongs.append(changed)
        for wrong in wrongs:
            with self.subTest(identity=wrong):
                self.rejected(fixture, STEPS[1], {**valid, "root_identity": wrong})
        fixture.phases.append(STEPS[1], valid)

    def test_files_have_all_sixteen_ordered_unique_slots_and_distinct_original_inodes(self):
        fixture = self.fixture()
        self.advance(fixture, 2)
        valid = creation_payload(fixture, STEPS[2])
        files = valid["files"]
        wrongs = [None, tuple(files), files[:-1], files + [deepcopy(files[0])], list(reversed(files))]
        for field, replacement in (("slot", "unknown.json"), ("slot", SLOTS[0]),
                                   ("identity", deepcopy(files[0]["identity"]))):
            changed = deepcopy(files)
            changed[1][field] = replacement
            wrongs.append(changed)
        changed = deepcopy(files)
        changed[1]["bytes"] = 0
        wrongs.append(changed)
        changed = deepcopy(files)
        del changed[1]["identity"]
        wrongs.append(changed)
        for wrong in wrongs:
            with self.subTest(files=wrong):
                self.rejected(fixture, STEPS[2], {**valid, "files": wrong})
        # Same inode number on different devices is not a duplicate identity.
        valid["files"][1]["identity"][:2] = [102, 1000]
        fixture.phases.append(STEPS[2], valid)

    def test_every_file_slot_identity_is_exact_private_regular_file_data(self):
        fixture = self.fixture()
        self.advance(fixture, 2)
        valid = creation_payload(fixture, STEPS[2])
        for slot_index in (0, 6, 7, 15):
            for component, wrong in ((0, True), (1, 0), (2, 0o40700), (2, 0o100644),
                                     (3, os.getuid() + 1), (3, -1), (0, 2**63)):
                changed = deepcopy(valid)
                changed["files"][slot_index]["identity"][component] = wrong
                with self.subTest(slot=SLOTS[slot_index], component=component, wrong=wrong):
                    self.rejected(fixture, STEPS[2], changed)
        self.rejected(fixture, STEPS[2], {"identity": [101, 900, 0o100600, os.getuid()]})
        fixture.phases.append(STEPS[2], valid)

    def test_input_seal_requires_complete_nonempty_original_input_slot(self):
        fixture = self.fixture()
        self.advance(fixture, 3)
        valid = creation_payload(fixture, STEPS[3])
        for field, wrong in (("slot", SLOTS[1]), ("evidence_kind", "failure_prefix"),
                             ("byte_count", 0), ("byte_count", True), ("byte_count", -1),
                             ("sha256", "0" * 64), ("sha256", "sha256:" + "A" * 64),
                             ("identity", [101, 1001, 0o100600, os.getuid()])):
            changed = deepcopy(valid)
            changed["input_file"][field] = wrong
            with self.subTest(field=field, wrong=wrong):
                self.rejected(fixture, STEPS[3], changed)
        for field in valid["input_file"]:
            changed = deepcopy(valid)
            del changed["input_file"][field]
            with self.subTest(missing=field):
                self.rejected(fixture, STEPS[3], changed)
        changed = deepcopy(valid)
        changed["input_file"]["authority"] = "quality-pass"
        self.rejected(fixture, STEPS[3], changed)
        fixture.phases.append(STEPS[3], valid)

    def test_input_seal_exact_minimum_of_per_slot_and_aggregate_byte_bounds(self):
        for slot_limit, aggregate in ((7, 11), (11, 7), (7, 7)):
            with self.subTest(slot_limit=slot_limit, aggregate=aggregate):
                budget = {"semantic_record_bytes": 31, "build_record_bytes": 31,
                          "comparison_evidence_bytes": slot_limit, "child_control_bytes": 31,
                          "child_stderr_bytes": 31, "retained_total_bytes": aggregate}
                fixture = self.fixture(budget=budget)
                self.advance(fixture, 3)
                valid = creation_payload(fixture, STEPS[3])
                bound = min(slot_limit, aggregate)
                valid["input_file"]["byte_count"] = bound + 1
                self.rejected(fixture, STEPS[3], valid)
                valid["input_file"]["byte_count"] = bound
                fixture.phases.append(STEPS[3], valid)

    def test_duplicates_and_regressions_never_replace_original_creation_receipts(self):
        fixture = self.fixture()
        for step in STEPS:
            valid = creation_payload(fixture, step)
            fixture.phases.append(step, valid)
            self.rejected(fixture, step, valid)
            for prior in STEPS[:STEPS.index(step)]:
                with self.subTest(at=step, prior=prior):
                    self.rejected(fixture, prior, fixture.phases.value(prior))

    def test_invalid_generic_hash_chain_record_is_rejected_by_phase_replay(self):
        for index, step in enumerate(STEPS):
            with self.subTest(step=step):
                fixture = self.fixture()
                self.advance(fixture, index)
                value = creation_payload(fixture, step)
                value["configuration_sha256"] = digest(canonical(fixture.binding["owned_quality"]))
                record = fixture.journal.append(step, value)
                self.assertEqual(fixture.journal.records[-1], record)
                before = fixture.persisted()
                with self.assertRaises(ValueError):
                    fixture.refresh()
                self.assertEqual(fixture.persisted(), before, "replay discarded original invalid evidence")

    def test_v3_cannot_grant_later_quality_cleanup_ack_or_disposal_at_any_creation_cut(self):
        fixture = self.fixture()
        for cut in range(5):
            for step in BLOCKED:
                with self.subTest(cut=cut, blocked=step):
                    self.rejected(fixture, step, {})
            if cut < 4:
                fixture.phases.append(STEPS[cut], creation_payload(fixture, STEPS[cut]))
        self.assertEqual(fixture.phases.history[-1].step, "quality_input_sealed")

    def test_v3_valid_legacy_validation_cannot_bypass_ownership_by_direct_or_replayed_append(self):
        legacy = self.fixture(legacy=True)
        legacy.begin_cleanup()
        old_validated = legacy.phases.value("validated")
        fixture = self.fixture()
        self.advance(fixture)
        self.rejected(fixture, "validated", old_validated)
        fixture.journal.append("validated", old_validated)
        before = fixture.persisted()
        with self.assertRaises(ValueError):
            fixture.refresh()
        self.assertEqual(fixture.persisted(), before)

    def test_v2_rejects_all_four_new_phases_but_retains_legacy_validation_cleanup(self):
        legacy = self.fixture(legacy=True)
        self.assertIsNone(legacy.phases.owned_quality)
        modern = self.fixture()
        self.advance(modern)
        for step in STEPS:
            with self.subTest(step=step):
                self.rejected(legacy, step, modern.phases.value(step))
        legacy.begin_cleanup()
        expected = legacy.phases.value("validated")
        before = legacy.persisted()
        legacy.refresh()
        self.assertEqual(legacy.persisted(), before)
        self.assertIsNone(legacy.phases.owned_quality)
        self.assertEqual(legacy.phases.value("validated"), expected)
        self.assertEqual(legacy.phases.history[-1].step, "cleanup_intent")


if __name__ == "__main__":
    unittest.main()
