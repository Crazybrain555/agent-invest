"""Full literal build/hash wire and intentionally divergent structural candidates."""

from copy import deepcopy
from dataclasses import replace
import unittest

from disclosure_anchor.application.contracts.provider_quality import quality_occurrence_from_payload
from disclosure_anchor.application.contracts.provider_table_projection import ProviderTablePartRef, UnboundProviderTablePart
from disclosure_anchor.application.contracts.provider_unit import ProviderUnitBuildResult, ProviderUnitDraft
from disclosure_anchor.application.services.source_semantic_build import (
    decode_source_semantic_build, encode_source_semantic_build, source_unit_to_payload,
)
from tests._source_semantic_build_fixture import build_from_literal, literal_build_record, refresh_literal_hashes
from tests._source_semantic_record_fixture import canonical, sha


LIMIT = 200_000


def encode_literal(record, maximum_bytes=LIMIT):
    return encode_source_semantic_build(
        semantic_record_sha256=record["semantic_record_sha256"], build=build_from_literal(record["build"]),
        quality_occurrences=tuple(quality_occurrence_from_payload(o) for o in record["quality_occurrences"]),
        maximum_bytes=maximum_bytes,
    )


class SourceSemanticBuildTests(unittest.TestCase):
    def test_complete_literal_wire_and_three_independent_hash_preimages_round_trip(self):
        record = literal_build_record()
        raw = canonical(record)
        actual = encode_literal(record, len(raw))
        self.assertEqual(actual, raw)
        parsed = decode_source_semantic_build(raw, maximum_bytes=len(raw))
        self.assertEqual(parsed.record_sha256, sha(raw))
        self.assertEqual(parsed.semantic_record_sha256, record["semantic_record_sha256"])
        self.assertEqual(source_unit_to_payload(parsed.build.units[0]), record["build"]["units"][0])
        self.assertEqual(parsed.build, build_from_literal(record["build"]))
        self.assertEqual(len(source_unit_to_payload(parsed.build.units[0])), 14)
        self.assertEqual(parsed.build.units[0].payload["parts"], [{"text": "金额5元。"}, {"text": "公告2026〕7号"}])

    def test_unit_hashes_reject_stale_content_query_and_structure_preimages(self):
        for field, value in (("payload", {"parts": [{"text": "changed"}, {"text": "公告2026〕7号"}]}),
                             ("title", "unrecorded query change"), ("heading_path", ["unrecorded structure change"])):
            record = literal_build_record()
            record["build"]["units"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                decode_source_semantic_build(canonical(record), maximum_bytes=LIMIT)
        for field in ("content_hash", "query_projection_hash", "structure_hash"):
            record = literal_build_record()
            record["build"]["units"][0][field] = sha("forged serialized hash")
            with self.subTest(hash=field), self.assertRaises(ValueError):
                decode_source_semantic_build(canonical(record), maximum_bytes=LIMIT)

    def test_correct_hash_divergent_candidates_retain_all_fields_without_semantic_approval(self):
        record = literal_build_record()
        unit = record["build"]["units"][0]
        unit.update(title="Changed title", heading_path=["Changed title"], section_keys=["section_a"],
                    semantic_keys=["route_a", "route_b"], applicability="applicable",
                    quality_status="future_unknown", page_no=2)
        unit["locator"]["heading_chain"] = [{"heading_id": "heading:00000000", "source_index": 0,
            "payload_ordinal": 0, "placement_source": "provider", "continuation_fragments": []}]
        unit["payload"]["parts"][0]["text"] = "金额6元。"
        refresh_literal_hashes(unit)
        raw = canonical(record)
        parsed = decode_source_semantic_build(raw, maximum_bytes=LIMIT)
        self.assertEqual(source_unit_to_payload(parsed.build.units[0]), unit)
        self.assertEqual(encode_literal(record), raw)
        self.assertEqual(parsed.build.units[0].quality_status, "future_unknown")
        self.assertFalse(hasattr(parsed, "admitted"))

    def test_record_build_and_locator_source_mismatches_remain_visible_candidates(self):
        record = literal_build_record()
        record["semantic_record_sha256"] = sha("different record")
        record["build"]["provider_document_sha256"] = sha("different build source")
        record["build"]["units"][0]["locator"]["provider_document_sha256"] = sha("different locator source")
        parsed = decode_source_semantic_build(canonical(record), maximum_bytes=LIMIT)
        self.assertEqual(parsed.semantic_record_sha256, sha("different record"))
        self.assertEqual(parsed.build.provider_document_sha256, sha("different build source"))
        self.assertEqual(parsed.build.units[0].locator.provider_document_sha256, sha("different locator source"))

    def test_empty_and_unassigned_builds_round_trip_without_invented_units(self):
        for parts in ((), (UnboundProviderTablePart(ProviderTablePartRef(None, 4), "page_table_count_mismatch"),)):
            build = ProviderUnitBuildResult(sha("empty source"), (), parts)
            raw = encode_source_semantic_build(semantic_record_sha256=sha("empty source"), build=build,
                                               quality_occurrences=(), maximum_bytes=LIMIT)
            parsed = decode_source_semantic_build(raw, maximum_bytes=len(raw))
            with self.subTest(parts=parts):
                self.assertEqual(parsed.build, build)
                self.assertEqual(parsed.build.units, ())
                self.assertEqual(parsed.quality_occurrences, ())

    def test_closed_versions_hints_shapes_and_local_indices_reject(self):
        base = literal_build_record()
        changes = (
            lambda r: r.update(extra=True), lambda r: r.update(contract_version="m6.source-semantic-build.v2"),
            lambda r: r.update(mode="production"), lambda r: r.update(builder_version="provider_unit.v22"),
            lambda r: r.update(level_hints=[{}]), lambda r: r.update(negative_hints=[{}]),
            lambda r: r["build"].update(extra=True), lambda r: r["build"]["units"][0].update(extra=True),
            lambda r: r["build"]["units"][0].update(unit_index=True),
            lambda r: r["build"]["units"][0].update(page_no=True),
            lambda r: r["build"]["units"][0].update(heading_path="ancestor"),
            lambda r: r["build"]["units"][0]["locator"].update(contract_version="provider_unit_locator.v8"),
            lambda r: r["build"]["units"][0]["locator"].update(unit_index=1),
            lambda r: r["build"]["units"][0].pop("title"),
        )
        for index, change in enumerate(changes):
            record = deepcopy(base)
            change(record)
            with self.subTest(case=index), self.assertRaises(ValueError):
                decode_source_semantic_build(canonical(record), maximum_bytes=LIMIT)

    def test_occurrences_cannot_be_duplicated_or_reordered_silently(self):
        record = literal_build_record()
        occurrence = record["quality_occurrences"][0]
        for occurrences in ([occurrence, occurrence], [dict(occurrence, unit_index=10), dict(occurrence, unit_index=2)]):
            record["quality_occurrences"] = occurrences
            with self.subTest(occurrences=occurrences), self.assertRaises(ValueError):
                decode_source_semantic_build(canonical(record), maximum_bytes=LIMIT)
        record["quality_occurrences"] = [dict(occurrence, unit_index=2), dict(occurrence, unit_index=10)]
        parsed = decode_source_semantic_build(canonical(record), maximum_bytes=LIMIT)
        self.assertEqual([o.unit_index for o in parsed.quality_occurrences], [2, 10])

    def test_canonical_and_exact_byte_boundaries_are_required_in_both_directions(self):
        record = literal_build_record()
        raw = canonical(record)
        for invalid in (raw + b"\n", raw.replace(b'"mode":', b'"mode" :'), raw.replace(b'"mode":', b'"mode":"service_diagnostic","mode":')):
            with self.subTest(raw=invalid[:80]), self.assertRaises(ValueError):
                decode_source_semantic_build(invalid, maximum_bytes=LIMIT)
        with self.assertRaises(ValueError):
            decode_source_semantic_build(raw, maximum_bytes=len(raw) - 1)
        with self.assertRaises(ValueError):
            encode_literal(record, len(raw) - 1)
        self.assertEqual(encode_literal(record, len(raw)), raw)

    def test_encoder_rejects_nonexact_build_and_invalid_unit_types(self):
        class BuildSubclass(ProviderUnitBuildResult):
            pass
        class UnitSubclass(ProviderUnitDraft):
            pass
        record = literal_build_record()
        build = build_from_literal(record["build"])
        draft = build.units[0]
        subclass_unit = UnitSubclass(**{name: getattr(draft, name) for name in ProviderUnitDraft.__dataclass_fields__})
        for changed in (BuildSubclass(build.provider_document_sha256, build.units, ()), replace(build, units=(subclass_unit,))):
            with self.subTest(build=type(changed)), self.assertRaises(ValueError):
                encode_source_semantic_build(semantic_record_sha256=record["semantic_record_sha256"], build=changed,
                                               quality_occurrences=(), maximum_bytes=LIMIT)


if __name__ == "__main__":
    unittest.main()
