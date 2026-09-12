"""Independent complete inventories and correctly hashed divergent comparison records."""

from copy import deepcopy
import json
import unittest

from disclosure_anchor.application.services.source_semantic_build import decode_source_semantic_build
from disclosure_anchor.application.services.source_semantic_comparison import compare_source_semantic_candidate
from disclosure_anchor.application.services.source_semantic_record import parse_source_semantic_candidate
from tests._source_semantic_build_fixture import literal_build_record, refresh_literal_hashes
from tests._source_semantic_comparison_fixture import rebind_source, repeated_heading_pair, table_continuation_unassigned_pair
from tests._source_semantic_record_fixture import canonical, literal_record, sha


LIMIT = 500_000
CHECKS = tuple(sorted(("source_identity", "page_closure", "block_conservation", "table_segment_conservation",
                       "logical_table_conservation", "retrieval_target_binding", "repair_binding", "finding_binding",
                       "reading_order_contiguity", "heading_occurrence_closure", "artifact_closure", "independent_rebuild_match")))
UNVERIFIED = {"artifact_closure", "independent_rebuild_match"}


def compare(source, build, reference_source=None, reference_build=None, *, maximum_evidence_bytes=LIMIT):
    return compare_source_semantic_candidate(canonical(source), canonical(build),
        canonical(source if reference_source is None else reference_source),
        canonical(build if reference_build is None else reference_build),
        maximum_record_bytes=LIMIT, maximum_evidence_bytes=maximum_evidence_bytes)


def outcomes(result):
    return {check.check_id: check.outcome for check in result.checks}


def scalars(value):
    if isinstance(value, dict):
        return [scalar for child in value.values() for scalar in scalars(child)]
    if isinstance(value, list):
        return [scalar for child in value for scalar in scalars(child)]
    return [value]


class SourceSemanticComparisonTests(unittest.TestCase):
    def assert_semantic_failure(self, check, source, build, reference_source, reference_build):
        # Both mutated records must survive structural/correct-hash parsing first.
        parse_source_semantic_candidate(canonical(source), maximum_bytes=LIMIT)
        decode_source_semantic_build(canonical(build), maximum_bytes=LIMIT)
        result = compare(source, build, reference_source, reference_build)
        self.assertEqual(outcomes(result)[check], "fail")
        self.assertEqual([c.check_id for c in result.checks], list(CHECKS))
        self.assertEqual({key: outcomes(result)[key] for key in UNVERIFIED}, {key: "unverified" for key in UNVERIFIED})
        return result

    def test_three_literal_complete_inventories_pass_ten_data_checks_but_never_qualify(self):
        for source, build in ((literal_record(), literal_build_record()), repeated_heading_pair(), table_continuation_unassigned_pair()):
            with self.subTest(kind=build["build"]["units"][0]["payload_kind"]):
                result = compare(source, build)
                self.assertEqual([c.check_id for c in result.checks], list(CHECKS))
                self.assertEqual(outcomes(result), {key: "unverified" if key in UNVERIFIED else "pass" for key in CHECKS})
                self.assertFalse(hasattr(result, "qualify"))
                self.assertFalse(hasattr(result, "scorable_pages"))
                self.assertEqual(result.evidence_sha256, sha(result.evidence))
                evidence = json.loads(result.evidence)
                self.assertEqual(canonical(evidence), result.evidence)
                leaves = scalars(evidence)
                for raw in (canonical(source), canonical(build)):
                    self.assertIn(sha(raw), leaves)
                    self.assertIn(len(raw), leaves)
        source, build = table_continuation_unassigned_pair()
        self.assertEqual(len(source["provider_document"]["pages"]), 3)
        self.assertEqual(len(build["build"]["units"]), 1)
        self.assertEqual(build["build"]["units"][0]["payload"]["table_body"],
                         "<table><tr><td>17</td></tr><tr><td>29</td></tr></table>")
        self.assertEqual(build["quality_occurrences"][0]["unit_index"], None)

    def test_changed_source_identity_and_blank_page_claim_reach_named_failures(self):
        for key, value, check in (("sha256", sha("different observed source"), "source_identity"),
                                  ("page_count", 3, "page_closure")):
            source, build = literal_record(), literal_build_record()
            reference_source, reference_build = deepcopy(source), deepcopy(build)
            source["source_observation"][key] = value
            rebind_source(build, source)
            with self.subTest(key=key):
                result = self.assert_semantic_failure(check, source, build, reference_source, reference_build)
                leaves = scalars(json.loads(result.evidence))
                for raw in (canonical(source), canonical(build), canonical(reference_source), canonical(reference_build)):
                    self.assertIn(sha(raw), leaves)
                    self.assertIn(len(raw), leaves)

    def test_equal_count_wrong_block_ownership_is_not_count_only_conservation(self):
        source, build = literal_record(), literal_build_record()
        reference = deepcopy(build)
        build["build"]["units"][0]["locator"]["parts"][1]["block_source_indices"] = [0]
        self.assert_semantic_failure("block_conservation", source, build, source, reference)

    def test_omitted_or_duplicate_physical_segment_evidence_fails_conservation(self):
        source, original = table_continuation_unassigned_pair()
        for variant in ("omit_unassigned", "duplicate_unassigned"):
            build = deepcopy(original)
            if variant == "omit_unassigned":
                build["build"]["unassigned_table_parts"] = []
                build["quality_occurrences"] = []
            else:
                # Repeat the orphan as a bound occurrence while omitting segment 1;
                # the two block/segment arrays retain equal local cardinality.
                unit = build["build"]["units"][0]
                unit["locator"]["parts"][0]["physical_table_segment_indices"] = [0, 2]
            with self.subTest(variant=variant):
                self.assert_semantic_failure("table_segment_conservation", source, build, source, original)

    def test_logical_owner_partition_and_exact_unbound_reason_are_compared(self):
        source, original = table_continuation_unassigned_pair()
        for variant in ("owner", "reason"):
            build = deepcopy(original)
            if variant == "owner":
                build["build"]["units"][0]["locator"]["parts"][0]["logical_table_index"] = 1
            else:
                build["build"]["unassigned_table_parts"][0]["reason"] = "continuation_without_owner"
                build["quality_occurrences"][0]["part"]["reason"] = "continuation_without_owner"
                build["quality_occurrences"][0]["reason_id"] = "table_unbound:continuation_without_owner"
            with self.subTest(variant=variant):
                self.assert_semantic_failure("logical_table_conservation", source, build, source, original)

    def test_retrieval_source_and_transformed_destination_replays_detect_wrong_values(self):
        for variant in ("scalar", "destination", "source_hash", "transform"):
            source, build = literal_record(), literal_build_record()
            original = deepcopy(build)
            unit = build["build"]["units"][0]
            if variant == "scalar":
                unit["payload"]["parts"][0]["text"] = "金额6元。"
                refresh_literal_hashes(unit)
            elif variant == "destination":
                unit["locator"]["search_targets"][0]["destination"]["part_index"] = 1
            elif variant == "source_hash":
                unit["locator"]["search_targets"][0]["raw_block_sha256"] = sha("other source block")
            else:
                unit["locator"]["search_targets"][0]["transform"] = "html_visible_text_segments.v1"
            with self.subTest(variant=variant):
                self.assert_semantic_failure("retrieval_target_binding", source, build, source, original)

    def test_repair_claims_and_dependent_locator_occurrences_are_both_required(self):
        for variant in ("claim", "locator"):
            source, build = literal_record(), literal_build_record()
            original_source, original_build = deepcopy(source), deepcopy(build)
            if variant == "claim":
                source["source_text_reconciliations"] = []
                rebind_source(build, source)
            else:
                build["build"]["units"][0]["locator"]["source_text_reconciliations"] = []
            with self.subTest(variant=variant):
                self.assert_semantic_failure("repair_binding", source, build, original_source, original_build)

    def test_finding_claim_membership_quality_status_and_occurrence_evidence_cannot_disappear(self):
        for variant in ("claim", "locator", "quality_occurrence", "status"):
            source, build = literal_record(), literal_build_record()
            original_source, original_build = deepcopy(source), deepcopy(build)
            if variant == "claim":
                source["source_quality_findings"] = []
                rebind_source(build, source)
            elif variant == "locator":
                build["build"]["units"][0]["locator"]["source_quality_findings"] = []
            elif variant == "quality_occurrence":
                build["quality_occurrences"] = []
            else:
                build["build"]["units"][0]["quality_status"] = "future_unknown"
                refresh_literal_hashes(build["build"]["units"][0])
            with self.subTest(variant=variant):
                self.assert_semantic_failure("finding_binding", source, build, original_source, original_build)

    def test_contiguous_renumbered_units_still_fail_when_reading_order_reversed(self):
        source, build = repeated_heading_pair()
        original = deepcopy(build)
        build["build"]["units"].reverse()
        for index, unit in enumerate(build["build"]["units"]):
            unit["unit_index"] = unit["locator"]["unit_index"] = index
            refresh_literal_hashes(unit)
        self.assert_semantic_failure("reading_order_contiguity", source, build, source, original)

    def test_repeated_equal_title_text_does_not_substitute_for_source_occurrence_identity(self):
        source, build = repeated_heading_pair()
        original = deepcopy(build)
        first, second = build["build"]["units"]
        self.assertEqual(first["title"], second["title"])
        second["locator"]["heading_chain"] = deepcopy(first["locator"]["heading_chain"])
        self.assert_semantic_failure("heading_occurrence_closure", source, build, source, original)

    def test_equal_but_wrong_reference_is_rejected_before_apparent_passing_comparison(self):
        source, build = literal_record(), literal_build_record()
        build["build"]["units"][0]["payload"]["parts"][0]["text"] = "金额6元。"
        refresh_literal_hashes(build["build"]["units"][0])
        decode_source_semantic_build(canonical(build), maximum_bytes=LIMIT)
        with self.assertRaises(ValueError):
            compare(source, build)
        source, build = literal_record(), literal_build_record()
        build["quality_occurrences"] = []
        with self.assertRaises(ValueError):
            compare(source, build)

    def test_input_and_evidence_budgets_are_explicit_and_exact_without_truncated_success(self):
        source, build = literal_record(), literal_build_record()
        result = compare(source, build)
        self.assertEqual(compare(source, build, maximum_evidence_bytes=len(result.evidence)), result)
        with self.assertRaises(ValueError):
            compare(source, build, maximum_evidence_bytes=len(result.evidence) - 1)
        for limit in (0, True, 1, len(canonical(source)) - 1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                compare_source_semantic_candidate(canonical(source), canonical(build), canonical(source), canonical(build),
                                                   maximum_record_bytes=limit, maximum_evidence_bytes=LIMIT)
        with self.assertRaises(ValueError):
            compare_source_semantic_candidate(source, canonical(build), canonical(source), canonical(build),
                                               maximum_record_bytes=LIMIT, maximum_evidence_bytes=LIMIT)


if __name__ == "__main__":
    unittest.main()
