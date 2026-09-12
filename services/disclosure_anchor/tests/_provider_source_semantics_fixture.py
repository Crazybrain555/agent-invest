"""Independent synthetic H1 fixtures; no real PDF or production qualification.

Raw JSON is literal provider-shaped evidence with independently calculated hashes.
The fake source port establishes only deterministic admission control flow. It does
not certify that these records were read from a real PDF or MinerU artifact tree.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path

from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.contracts.provider_document import (
    ProviderArtifact,
    ProviderBBox,
    ProviderBlock,
    ProviderDocument,
    ProviderPage,
    ProviderPayload,
    ProviderPhysicalTableSegment,
    provider_artifact_bundle_sha256,
)
from disclosure_anchor.application.contracts.provider_document_admission import (
    SourcePdfObservation,
    SourcePdfTextObservation,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelope,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.services.provider_document_admission import (
    ProviderDocumentAdmission,
)
from disclosure_anchor.domain import entities as e


SOURCE_BYTES = b"H1 synthetic source identity; not a PDF"
DOCUMENT_ID = "h1-document"
OWNER_ID = "h1-parse-owner"
RECORD_PATH = Path(
    "derived/provider_documents/fixture/009999/h1-source/h1-parse-owner/"
    "provider_document.v1.json"
)


def sha(value: bytes | str) -> str:
    return "sha256:" + hashlib.sha256(
        value.encode("utf-8") if isinstance(value, str) else value
    ).hexdigest()


def raw_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def target() -> ParserTargetIdentity:
    return ParserTargetIdentity(
        name="MinerU", package_version="3.4.4", backend="hybrid-http-client",
        method="auto", language="ch", formula=True, table=True, effort="medium",
        image_analysis=False, full_pdf=True,
        runtime_bundle_identity_sha256=sha("synthetic runtime identity"),
    )


def block(
    index: int, page: int, order: int, text: str, *, kind: str = "text",
    heading: bool = False, visual: bool = False,
) -> ProviderBlock:
    field = {"table": "table_body", "image": "content"}.get(kind, "text")
    raw = raw_json({
        "bbox": [20, 40 + 90 * order, 920, 110 + 90 * order],
        "page_idx": page, "type": kind, field: text,
        **({"text_level": 1} if heading else {}),
        **({"img_path": "images/figure.png"} if visual else {}),
    })
    return ProviderBlock(
        source_index=index, page_index=page, order_in_page=order,
        provider_type=kind, typed_annotation="title" if heading else None,
        provider_level=1 if heading else None,
        bbox=ProviderBBox(20, 40 + 90 * order, 920, 110 + 90 * order),
        payloads=(ProviderPayload(field, None, text),) if text else (),
        referenced_artifact_roles=("figure",) if visual else (),
        raw_item_json=raw, raw_item_sha256=sha(raw),
    )


def segment(page: int, order: int, html: str, status: str) -> ProviderPhysicalTableSegment:
    raw = raw_json({"bbox": [20, 200, 920, 400], "html": html,
                    "index": order, "type": "table"})
    return ProviderPhysicalTableSegment(
        page_index=page, order_in_page=order, provider_index=order,
        bbox=ProviderBBox(20, 200, 920, 400), page_local_html=html,
        crop_artifact_role=None, logical_stream_status=status,
        raw_segment_json=raw, raw_segment_sha256=sha(raw),
    )


def document(
    pages: tuple[tuple[ProviderBlock, ...], ...],
    segments: tuple[ProviderPhysicalTableSegment, ...] = (),
) -> ProviderDocument:
    artifacts = tuple(
        ProviderArtifact(role, name, sha(raw_json({"fixture_role": role})),
                         len(raw_json({"fixture_role": role}).encode()), media)
        for role, name, media in (
            ("content_list", "a_content_list.json", "application/json"),
            ("content_list_v2", "b_content_list_v2.json", "application/json"),
            ("middle_json", "c_middle.json", "application/json"),
            ("model_json", "d_model.json", "application/json"),
            ("figure", "images/figure.png", "image/png"),
        )
    )
    return ProviderDocument(
        source_pdf_sha256=sha(SOURCE_BYTES), parser_version="3.4.4",
        backend="hybrid", effort="medium", ocr_enabled=False,
        pages=tuple(ProviderPage(i, (595.0, 842.0), items) for i, items in enumerate(pages)),
        physical_table_segments=segments, artifacts=artifacts,
        bundle_sha256=provider_artifact_bundle_sha256(artifacts),
    )


def observation(item: ProviderBlock, native: str, ordinal: int = 0) -> SourcePdfTextObservation:
    return SourcePdfTextObservation(
        item.source_index, item.page_index, ordinal, item.raw_item_sha256, native,
    )


def envelope(provider_document: ProviderDocument) -> ProviderDocumentEnvelope:
    digest = sha(SOURCE_BYTES).removeprefix("sha256:")
    return ProviderDocumentEnvelope.build(
        document_id=DOCUMENT_ID, artifact_owner_processing_run_id=OWNER_ID,
        provider="fixture", provider_document_id="h1-source",
        source_pdf_relpath=f"raw_documents/fixture/009999/2026/h1-source/sha256_{digest}.pdf",
        source_pdf_page_count=len(provider_document.pages),
        parser_artifact_root_relpath=(
            f"parser_artifacts/fixture/009999/h1-source/{OWNER_ID}/sha256_{digest}/hybrid_auto"
        ),
        parser_target_identity=target(), provider_document=provider_document,
    )


@dataclass
class SourcePort:
    envelope: ProviderDocumentEnvelope
    observations: tuple[SourcePdfTextObservation, ...] = ()
    source_identity: SourcePdfObservation | None = None
    rebuilt: ProviderDocument | None = None

    def read_provider_document_record(self, relpath):
        if relpath != RECORD_PATH:
            raise AssertionError("unexpected record path")
        return provider_document_envelope_to_bytes(self.envelope)

    def observe_source_pdf(self, relpath):
        if relpath != Path(self.envelope.source_pdf_relpath):
            raise AssertionError("unexpected source path")
        return self.source_identity or SourcePdfObservation(
            sha(SOURCE_BYTES), len(SOURCE_BYTES), self.envelope.source_pdf_page_count,
        )

    def rebuild_provider_document(self, bundle_relpath, *, source_pdf_sha256):
        if bundle_relpath != Path(self.envelope.parser_artifact_root_relpath):
            raise AssertionError("unexpected bundle path")
        if source_pdf_sha256 != sha(SOURCE_BYTES):
            raise AssertionError("unexpected source identity")
        return self.rebuilt or self.envelope.provider_document

    def observe_source_pdf_text(self, relpath, *, document, expected_sha256):
        if relpath != Path(self.envelope.source_pdf_relpath):
            raise AssertionError("unexpected source text path")
        if document != self.envelope.provider_document or expected_sha256 != sha(SOURCE_BYTES):
            raise AssertionError("unexpected native observation binding")
        return self.observations


class Paths:
    def provider_document_relpath(self, **kwargs):
        if kwargs != {"provider": "fixture", "security_code": "009999",
                      "provider_document_id": "h1-source",
                      "artifact_owner_processing_run_id": OWNER_ID}:
            raise AssertionError("unexpected owner path arguments")
        return RECORD_PATH


def admission_inputs(doc, observations=()):
    record = envelope(doc)
    source = SourcePort(record, observations)
    entity = e.Document(
        document_id=DOCUMENT_ID, status="parsed", security_id="h1-security",
        provider="fixture", provider_document_id="h1-source",
        raw_file_relpath=record.source_pdf_relpath, raw_file_hash=sha(SOURCE_BYTES),
    )
    run = e.ProcessingRun(
        processing_run_id=OWNER_ID, document_id=DOCUMENT_ID,
        artifact_owner_processing_run_id=OWNER_ID, run_kind="parse", status="succeeded",
        parser_name="MinerU", parser_version="3.4.4", parser_backend="hybrid-http-client",
        parser_method="auto", parser_language="ch", parser_target_identity=target().to_payload(),
        input_raw_file_hash=sha(SOURCE_BYTES), parser_artifact_relpath=record.parser_artifact_root_relpath,
        artifact_hash=sha(provider_document_envelope_to_bytes(record)),
        provider_document_relpath=RECORD_PATH.as_posix(), normalized_ir_relpath=None,
    )
    return ProviderDocumentAdmission(path_builder=Paths(), source=source), entity, run, source


def admit(doc, observations=()):
    service, entity, run, _ = admission_inputs(doc, observations)
    return service.admit(document=entity, run=run, artifact_owner=run, security_code="009999")


def cases():
    repaired = block(0, 0, 0, "营业收入万元，同比增长%。")
    flagged = block(1, 0, 1, "请参阅公告2026〕7号。")
    owner_html = "<table><tr><td>项目</td><td>金额</td></tr><tr><td>甲</td><td>17</td></tr></table>"
    return {
        "plain": (document(((block(0, 0, 0, "完整正文。"),),)), ()),
        "repair_and_finding": (
            document(((repaired, flagged),)),
            (observation(repaired, "营业收入12万元，同比增长3%。"),
             observation(flagged, "请参阅公告〔2026〕7号。")),
        ),
        "continued_table": (
            document(((block(0, 0, 0, owner_html, kind="table"),),
                      (block(1, 1, 0, "", kind="table"),)),
                     (segment(0, 0, "<table><tr><td>项目</td><td>金额</td></tr></table>", "retained"),
                      segment(1, 0, "<table><tr><td>甲</td><td>17</td></tr></table>", "deleted"))), (),
        ),
        "multipage": (
            document(((block(0, 0, 0, "业务情况", heading=True), block(1, 0, 1, "第一段。")),
                      (), (block(2, 2, 0, "业务情况", heading=True), block(3, 2, 1, "第二段。")),
                      (block(4, 3, 0, "其他事项", heading=True),)),
                     (segment(1, 0, "<table><tr><td>孤立证据</td></tr></table>", "unbound"),)), (),
        ),
        "visual_only": (document(((block(0, 0, 0, "", kind="image", visual=True),),)), ()),
        "empty": (document(((), ())), ()),
    }


def json_value(value):
    return json.loads(json.dumps(asdict(value), ensure_ascii=False, default=str))
