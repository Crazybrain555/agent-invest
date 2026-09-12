"""Literal H2 mixed-build/hash preimages; never call a builder for expectations."""

from disclosure_anchor.application.contracts.provider_unit import ProviderUnitBuildResult, ProviderUnitDraft, provider_unit_locator_from_payload
from tests._source_semantic_record_fixture import canonical, literal_record, sha


def refresh_literal_hashes(unit):
    """The fixture has only scalar text parts, no mixed metadata filtering.

    Construct the three documented hash preimages directly with hashlib. This
    is deliberately limited to these literal fixtures, not a second Unit builder.
    """
    content = {"payload_kind": unit["payload_kind"], "payload": unit["payload"]}
    routes = unit["semantic_keys"]
    query = {"payload_kind": unit["payload_kind"], "title": unit["title"],
             "heading_path": unit["heading_path"], "semantic_key": routes[0] if routes else None,
             "quality_status": unit["quality_status"], "applicability": unit["applicability"]}
    if routes is not None and len(routes) > 1:
        query["semantic_keys"] = routes
    if unit["section_keys"] is not None:
        query["section_keys"] = unit["section_keys"]
    if unit["payload_kind"] == "mixed":
        assert all(set(part) == {"text"} for part in unit["payload"]["parts"])
        query["mixed_part_annotations"] = {"parts": [{} for _ in unit["payload"]["parts"]]}
    structure = {"payload_kind": unit["payload_kind"], "heading_path": unit["heading_path"],
                 "order_index": unit["unit_index"] + 1}
    unit.update(content_hash=sha(canonical(content)), query_projection_hash=sha(canonical(query)),
                structure_hash=sha(canonical(structure)))


def literal_build_record():
    source = literal_record()
    reference = sha(canonical(source))
    blocks = source["provider_document"]["pages"][0]["blocks"]
    repair = {k: v for k, v in source["source_text_reconciliations"][0].items() if k != "source_text"}
    finding = dict(source["source_quality_findings"][0])
    locator = {
        "contract_version": "provider_unit_locator.v9", "provider_document_sha256": reference,
        "unit_index": 0, "heading_chain": [],
        "parts": [{"part_index": i, "kind": "text", "block_source_indices": [i],
                   "physical_table_segment_indices": [], "logical_table_index": None} for i in (0, 1)],
        "evidence_only_block_source_indices": [], "unbound_table_parts": [], "evidence_artifacts": [],
        "source_text_reconciliations": [repair], "source_quality_findings": [finding],
        "search_targets": [{
            "source_index": i, "payload_ordinal": 0, "field": "text", "item_index": None,
            "raw_block_sha256": blocks[i]["raw_item_sha256"], "target_id": f"target:{i:08d}:0000",
            "transform": "identity.v1", "destination": {"kind": "mixed_part", "part_index": i,
                                                           "field": "text", "item_index": None},
        } for i in (0, 1)],
    }
    unit = {"unit_index": 0, "payload_kind": "mixed",
            "payload": {"parts": [{"text": "金额5元。"}, {"text": "公告2026〕7号"}]},
            "title": None, "heading_path": [], "section_keys": None, "semantic_keys": None,
            "applicability": None, "quality_status": "needs_review", "page_no": 1, "locator": locator}
    refresh_literal_hashes(unit)
    return {
        "contract_version": "m6.source-semantic-build.v1", "mode": "service_diagnostic",
        "semantic_record_sha256": reference, "builder_version": "provider_unit.v23",
        "level_hints": [], "negative_hints": [],
        "build": {"provider_document_sha256": reference, "units": [unit], "unassigned_table_parts": []},
        "quality_occurrences": [{"kind": "source_finding", "unit_index": 0, "finding": finding,
                                 "reason_id": "source_finding:source_pdf_native_text_quality.v2:cjk_bracket_omission"}],
    }


def unit_from_literal(value):
    return ProviderUnitDraft(**{
        **value,
        "heading_path": tuple(value["heading_path"]),
        "section_keys": None if value["section_keys"] is None else tuple(value["section_keys"]),
        "semantic_keys": None if value["semantic_keys"] is None else tuple(value["semantic_keys"]),
        "locator": provider_unit_locator_from_payload(value["locator"]),
    })


def build_from_literal(value):
    assert value["unassigned_table_parts"] == []
    return ProviderUnitBuildResult(value["provider_document_sha256"],
                                   tuple(unit_from_literal(unit) for unit in value["units"]), ())
