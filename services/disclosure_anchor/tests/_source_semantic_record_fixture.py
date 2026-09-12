"""Hand-assembled H2 record oracle; observations are synthetic data, not IO proof."""

import hashlib
import json

from disclosure_anchor.application.contracts._provider_content import provider_document_from_payload
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.contracts.provider_source_semantics import SourcePdfObservation, SourcePdfTextObservation


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(value):
    return "sha256:" + hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def literal_record():
    source_bytes = "synthetic H2 source identity，not a PDF".encode()
    source_hash = sha(source_bytes)
    artifacts = []
    for role, path in (("content_list", "a_content_list.json"),
                       ("content_list_v2", "b_content_list_v2.json"),
                       ("middle_json", "c_middle.json"), ("model_json", "d_model.json")):
        raw = canonical({"synthetic_role": role})
        artifacts.append({"role": role, "relative_path": path, "sha256": sha(raw),
                          "size_bytes": len(raw), "media_type": "application/json"})
    bundle_preimage = [{k: a[k] for k in ("role", "relative_path", "sha256", "size_bytes")} for a in artifacts]
    blocks = []
    for index, text in enumerate(("金额元。", "公告2026〕7号")):
        bbox = [20, 40 + index * 100, 920, 110 + index * 100]
        raw = canonical({"bbox": bbox, "page_idx": 0, "text": text, "type": "text"}).decode()
        blocks.append({"source_index": index, "page_index": 0, "order_in_page": index,
                       "provider_type": "text", "typed_annotation": None, "provider_level": None,
                       "bbox": bbox, "payloads": [{"field": "text", "item_index": None, "text": text}],
                       "referenced_artifact_roles": [], "raw_item_json": raw, "raw_item_sha256": sha(raw)})
    native = [
        {"source_index": 0, "page_index": 0, "payload_ordinal": 0,
         "raw_block_sha256": blocks[0]["raw_item_sha256"], "text": "金额5元。"},
        {"source_index": 1, "page_index": 0, "payload_ordinal": 0,
         "raw_block_sha256": blocks[1]["raw_item_sha256"], "text": "公告〔2026〕7号"},
    ]
    return {
        "contract_version": "m6.source-semantic-record.v1", "mode": "service_diagnostic",
        "source_semantics_version": "provider_source_semantics.v1",
        "source_observation": {"sha256": source_hash, "byte_count": len(source_bytes), "page_count": 2},
        "target_identity": {
            "name": "MinerU", "package_version": "3.4.4", "backend": "hybrid-http-client",
            "method": "auto", "language": "ch", "formula": True, "table": True, "effort": "medium",
            "image_analysis": False, "full_pdf": True, "start_page": None, "end_page": None,
            "runtime_bundle_identity_sha256": sha("synthetic H2 runtime"),
            "inline_equation_left": "$", "inline_equation_right": "$", "target_contract_version": "parser-target.v1",
        },
        "provider_document": {
            "source_pdf_sha256": source_hash, "parser_version": "3.4.4", "backend": "hybrid",
            "effort": "medium", "ocr_enabled": False,
            "pages": [{"page_index": 0, "page_size": [595.0, 842.0], "blocks": blocks},
                      {"page_index": 1, "page_size": [595.0, 842.0], "blocks": []}],
            "physical_table_segments": [], "artifacts": artifacts, "bundle_sha256": sha(canonical(bundle_preimage)),
        },
        "native_observations": native,
        "source_text_reconciliations": [{
            "source_index": 0, "payload_ordinal": 0, "raw_block_sha256": blocks[0]["raw_item_sha256"],
            "provider_text_sha256": sha("金额元。"), "source_text_sha256": sha("金额5元。"),
            "source_text": "金额5元。", "source_kind": "source_pdf_native_numeric.v1",
        }],
        "source_quality_findings": [{
            "source_index": 1, "payload_ordinal": 0, "raw_block_sha256": blocks[1]["raw_item_sha256"],
            "provider_text_sha256": sha("公告2026〕7号"), "source_text_sha256": sha("公告〔2026〕7号"),
            "reason": "cjk_bracket_omission", "source_kind": "source_pdf_native_text_quality.v2",
        }],
    }


def encoder_arguments(record):
    """Decode independent literal inputs via H1, never derive H2 expected output."""
    return {
        "source_observation": SourcePdfObservation(**record["source_observation"]),
        "target_identity": ParserTargetIdentity.from_payload(record["target_identity"]),
        "provider_document": provider_document_from_payload(record["provider_document"]),
        "native_observations": tuple(SourcePdfTextObservation(**item) for item in record["native_observations"]),
    }
