"""Hand-listed table and repeated-heading source/build pairs, no expected builder calls."""

from disclosure_anchor.application.contracts._provider_content import provider_document_to_payload
from tests._provider_source_semantics_fixture import SOURCE_BYTES, block, document, segment, target
from tests._source_semantic_build_fixture import literal_build_record, refresh_literal_hashes
from tests._source_semantic_record_fixture import canonical, literal_record, sha


def source_record_for_document(doc):
    """H1 codec supplies an input document, never an expected H2 build."""
    record = literal_record()
    record.update(source_observation={"sha256": sha(SOURCE_BYTES), "byte_count": len(SOURCE_BYTES), "page_count": len(doc.pages)},
                  target_identity=target().to_payload(), provider_document=provider_document_to_payload(doc),
                  native_observations=[], source_text_reconciliations=[], source_quality_findings=[])
    return record


def empty_locator(reference, index):
    return {"contract_version": "provider_unit_locator.v9", "provider_document_sha256": reference,
            "unit_index": index, "heading_chain": [], "parts": [], "evidence_only_block_source_indices": [],
            "unbound_table_parts": [], "evidence_artifacts": [], "source_text_reconciliations": [],
            "source_quality_findings": [], "search_targets": []}


def target_payload(item, destination, transform="identity.v1"):
    return {"target_id": f"target:{item.source_index:08d}:0000", "source_index": item.source_index,
            "payload_ordinal": 0, "field": item.payloads[0].field, "item_index": None,
            "raw_block_sha256": item.raw_item_sha256, "transform": transform, "destination": destination}


def build_record_for_source(source, units, unassigned=(), occurrences=()):
    reference = sha(canonical(source))
    record = literal_build_record()
    record.update(semantic_record_sha256=reference,
                  build={"provider_document_sha256": reference, "units": units, "unassigned_table_parts": list(unassigned)},
                  quality_occurrences=list(occurrences))
    return record


def repeated_heading_pair():
    items = (block(0, 0, 0, "经营概况", heading=True), block(1, 0, 1, "正文甲"),
             block(2, 1, 0, "经营概况", heading=True), block(3, 1, 1, "正文乙"))
    source = source_record_for_document(document((items[:2], items[2:])))
    reference = sha(canonical(source))
    units = []
    for index, (heading, body) in enumerate(((items[0], items[1]), (items[2], items[3]))):
        locator = empty_locator(reference, index)
        locator["heading_chain"] = [{"heading_id": f"heading:{heading.source_index:08d}",
            "source_index": heading.source_index, "payload_ordinal": 0, "placement_source": "provider", "continuation_fragments": []}]
        locator["parts"] = [{"part_index": 0, "kind": "text", "block_source_indices": [body.source_index],
                             "physical_table_segment_indices": [], "logical_table_index": None}]
        locator["search_targets"] = [
            target_payload(heading, {"kind": "unit_title", "part_index": None, "field": None, "item_index": None}),
            target_payload(body, {"kind": "unit_payload", "part_index": None, "field": "text", "item_index": None}),
        ]
        unit = {"unit_index": index, "payload_kind": "text", "payload": {"text": body.payloads[0].text},
                "title": "经营概况", "heading_path": ["经营概况"], "section_keys": None, "semantic_keys": None,
                "applicability": None, "quality_status": "ok", "page_no": index + 1, "locator": locator}
        refresh_literal_hashes(unit)
        units.append(unit)
    return source, build_record_for_source(source, units)


def table_continuation_unassigned_pair():
    first = "<table><tr><td>17</td></tr></table>"
    second = "<table><tr><td>29</td></tr></table>"
    aggregate = "<table><tr><td>17</td></tr><tr><td>29</td></tr></table>"
    orphan = "<table><tr><td>33</td></tr></table>"
    owner, continuation = block(0, 0, 0, aggregate, kind="table"), block(1, 1, 0, "", kind="table")
    segments = (segment(0, 0, first, "retained"), segment(1, 0, second, "deleted"), segment(2, 0, orphan, "unbound"))
    source = source_record_for_document(document(((owner,), (continuation,), ()), segments))
    reference = sha(canonical(source))
    locator = empty_locator(reference, 0)
    locator["parts"] = [{"part_index": 0, "kind": "table", "block_source_indices": [0, 1],
                         "physical_table_segment_indices": [0, 1], "logical_table_index": 0}]
    locator["search_targets"] = [target_payload(owner, {"kind": "unit_payload", "part_index": None,
                                                         "field": "table_body", "item_index": None}, "html_visible_text_segments.v1")]
    unit = {"unit_index": 0, "payload_kind": "table", "payload": {"table_body": aggregate},
            "title": None, "heading_path": [], "section_keys": None, "semantic_keys": None,
            "applicability": None, "quality_status": "ok", "page_no": 1, "locator": locator}
    refresh_literal_hashes(unit)
    unassigned = {"part": {"block_source_index": None, "physical_segment_index": 2}, "reason": "page_table_count_mismatch"}
    occurrence = {"kind": "table_unbound", "reason_id": "table_unbound:page_table_count_mismatch", "unit_index": None,
                  "part": unassigned, "block_page_index": None, "raw_block_sha256": None,
                  "segment_page_index": 2, "raw_segment_sha256": segments[2].raw_segment_sha256}
    return source, build_record_for_source(source, [unit], [unassigned], [occurrence])


def rebind_source(record, source):
    """Rebind only explicit digest references after an intentional source claim change."""
    reference = sha(canonical(source))
    record["semantic_record_sha256"] = record["build"]["provider_document_sha256"] = reference
    for unit in record["build"]["units"]:
        unit["locator"]["provider_document_sha256"] = reference
