from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest

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
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelope,
    ProviderDocumentEnvelopeError,
    provider_document_envelope_from_bytes,
    provider_document_envelope_from_payload,
    provider_document_envelope_to_bytes,
    provider_document_envelope_to_payload,
)


_SOURCE_SHA = "sha256:" + "a" * 64
_OWNER = "run_01K0000000000000000000000"
_DOCUMENT = "doc_01K00000000000000000000000"
_PROVIDER_DOCUMENT_ID = "1225087169"


class ProviderDocumentEnvelopeTests(unittest.TestCase):
    def test_round_trip_is_closed_and_stable(self) -> None:
        envelope = _envelope()
        payload = provider_document_envelope_to_payload(envelope)

        loaded = provider_document_envelope_from_payload(
            json.loads(json.dumps(payload, ensure_ascii=False))
        )

        self.assertEqual(loaded, envelope)
        self.assertEqual(provider_document_envelope_to_payload(loaded), payload)
        self.assertEqual(loaded.provider_document.blocks[0].payloads[0].text, "正文")
        self.assertEqual(
            loaded.provider_document.physical_table_segments[0].page_local_html,
            "<table><tr><td>正文</td></tr></table>",
        )
        self.assertEqual(
            loaded.provider_document.artifacts[0].media_type,
            "application/json",
        )
        encoded = provider_document_envelope_to_bytes(envelope)
        self.assertEqual(provider_document_envelope_from_bytes(encoded), envelope)

    def test_rejects_noncanonical_or_duplicate_key_bytes(self) -> None:
        canonical = provider_document_envelope_to_bytes(_envelope())
        compact = json.dumps(
            json.loads(canonical),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        duplicate = canonical.replace(
            b'{\n  "artifact_owner_processing_run_id"',
            b'{\n  "contract_version": "provider_document.v1",\n'
            b'  "artifact_owner_processing_run_id"',
            1,
        )

        for value in (compact, duplicate):
            with (
                self.subTest(value=value[:80]),
                self.assertRaises(ProviderDocumentEnvelopeError),
            ):
                provider_document_envelope_from_bytes(value)

    def test_write_side_rejects_runtime_type_drift(self) -> None:
        envelope = _envelope()
        page = envelope.provider_document.pages[0]
        block = page.blocks[0]
        invalid_documents = (
            replace(
                envelope.provider_document,
                ocr_enabled="false",  # type: ignore[arg-type]
            ),
            replace(
                envelope.provider_document,
                pages=(
                    replace(
                        page,
                        blocks=(replace(block, source_index=False),),
                    ),
                ),
            ),
            replace(
                envelope.provider_document,
                pages=(
                    replace(
                        page,
                        blocks=(
                            replace(
                                block,
                                source_index=0.0,  # type: ignore[arg-type]
                            ),
                        ),
                    ),
                ),
            ),
            replace(
                envelope.provider_document,
                pages=(
                    replace(
                        page,
                        page_size=(True, 842.0),  # type: ignore[arg-type]
                    ),
                ),
            ),
        )

        for document in invalid_documents:
            with (
                self.subTest(document=document),
                self.assertRaises(ProviderDocumentEnvelopeError),
            ):
                provider_document_envelope_to_bytes(
                    replace(envelope, provider_document=document)
                )

    def test_rejects_unknown_or_missing_envelope_field(self) -> None:
        unknown = provider_document_envelope_to_payload(_envelope())
        unknown["unexpected"] = True
        missing = provider_document_envelope_to_payload(_envelope())
        del missing["document_id"]

        for payload in (unknown, missing):
            with (
                self.subTest(fields=sorted(payload)),
                self.assertRaises(ProviderDocumentEnvelopeError),
            ):
                provider_document_envelope_from_payload(payload)

    def test_rejects_non_medium_target(self) -> None:
        targets = (
            replace(_target(), package_version="3.4.3"),
            replace(_target(), effort="high", image_analysis=True),
            replace(_target(), backend="vlm-http-client", effort=None),
            replace(_target(), full_pdf=False, start_page=0, end_page=0),
        )

        for target in targets:
            with (
                self.subTest(target=target),
                self.assertRaises(ProviderDocumentEnvelopeError),
            ):
                _envelope(parser_target=target)

    def test_rejects_owner_and_source_path_drift(self) -> None:
        envelope = _envelope()
        with self.assertRaises(ProviderDocumentEnvelopeError):
            _envelope(source_pdf_page_count=2)

        for field, value in (
            ("source_pdf_page_count", 2),
            (
                "parser_artifact_root_relpath",
                "parser_artifacts/cninfo/000001/1225087169/run_other/x/hybrid_auto",
            ),
            (
                "source_pdf_relpath",
                "raw_documents/cninfo/000001/2026/1225087169/sha256_"
                + "b" * 64
                + ".pdf",
            ),
        ):
            with (
                self.subTest(field=field),
                self.assertRaises(ProviderDocumentEnvelopeError),
            ):
                replace(envelope, **{field: value})

    def test_rejects_raw_record_hash_drift(self) -> None:
        envelope = _envelope()
        page = envelope.provider_document.pages[0]
        bad_block = replace(page.blocks[0], raw_item_sha256="sha256:" + "0" * 64)
        bad_document = replace(
            envelope.provider_document,
            pages=(replace(page, blocks=(bad_block,)),),
        )

        with self.assertRaises(ProviderDocumentEnvelopeError):
            replace(envelope, provider_document=bad_document)

        noncanonical_raw = '{"type": "text"}'
        with self.assertRaises(ProviderDocumentEnvelopeError):
            replace(
                envelope,
                provider_document=replace(
                    envelope.provider_document,
                    pages=(
                        replace(
                            page,
                            blocks=(
                                replace(
                                    page.blocks[0],
                                    raw_item_json=noncanonical_raw,
                                    raw_item_sha256=_sha(noncanonical_raw),
                                ),
                            ),
                        ),
                    ),
                ),
            )

        segment = envelope.provider_document.physical_table_segments[0]
        with self.assertRaises(ProviderDocumentEnvelopeError):
            replace(
                envelope,
                provider_document=replace(
                    envelope.provider_document,
                    physical_table_segments=(
                        replace(segment, raw_segment_sha256="sha256:" + "0" * 64),
                    ),
                ),
            )

    def test_rejects_noncanonical_paths_and_missing_required_artifact(self) -> None:
        envelope = _envelope()
        for field, value in (
            (
                "source_pdf_relpath",
                envelope.source_pdf_relpath.replace("/2026/", "/2026//"),
            ),
            (
                "parser_artifact_root_relpath",
                envelope.parser_artifact_root_relpath.replace(
                    "/hybrid_auto", "/./hybrid_auto"
                ),
            ),
        ):
            with (
                self.subTest(field=field),
                self.assertRaises(ProviderDocumentEnvelopeError),
            ):
                replace(envelope, **{field: value})

        document = envelope.provider_document
        artifacts = document.artifacts[:-1]
        with self.assertRaises(ProviderDocumentEnvelopeError):
            replace(
                envelope,
                provider_document=replace(
                    document,
                    artifacts=artifacts,
                    bundle_sha256=provider_artifact_bundle_sha256(artifacts),
                ),
            )

        wrong_media = (
            replace(
                document.artifacts[0],
                media_type="application/octet-stream",
            ),
            *document.artifacts[1:],
        )
        with self.assertRaises(ProviderDocumentEnvelopeError):
            replace(
                envelope,
                provider_document=replace(
                    document,
                    artifacts=wrong_media,
                    bundle_sha256=provider_artifact_bundle_sha256(wrong_media),
                ),
            )

    def test_provider_document_rejects_noncanonical_artifact_inventory(self) -> None:
        document = _provider_document()
        with self.assertRaises(ValueError):
            replace(document, bundle_sha256="sha256:" + "0" * 64)

        first = document.artifacts[0]
        second = replace(
            first,
            role="sidecar_000000",
            relative_path="a.bin",
            media_type="application/octet-stream",
        )
        artifacts = (first, second)

        with self.assertRaises(ValueError):
            replace(
                document,
                artifacts=artifacts,
                bundle_sha256=provider_artifact_bundle_sha256(artifacts),
            )

    def test_path_builder_contract_is_independent_of_bundle_root(self) -> None:
        envelope = _envelope()
        self.assertEqual(
            Path(envelope.parser_artifact_root_relpath).parts[:5],
            ("parser_artifacts", "cninfo", "000001", _PROVIDER_DOCUMENT_ID, _OWNER),
        )


def _envelope(
    *,
    parser_target: ParserTargetIdentity | None = None,
    source_pdf_page_count: int = 1,
) -> ProviderDocumentEnvelope:
    return ProviderDocumentEnvelope.build(
        document_id=_DOCUMENT,
        artifact_owner_processing_run_id=_OWNER,
        provider="cninfo",
        provider_document_id=_PROVIDER_DOCUMENT_ID,
        source_pdf_relpath=(
            f"raw_documents/cninfo/000001/2026/1225087169/sha256_{'a' * 64}.pdf"
        ),
        source_pdf_page_count=source_pdf_page_count,
        parser_artifact_root_relpath=(
            "parser_artifacts/cninfo/000001/1225087169/"
            f"{_OWNER}/sha256_{'a' * 64}/hybrid_auto"
        ),
        parser_target_identity=parser_target or _target(),
        provider_document=_provider_document(),
    )


def _provider_document() -> ProviderDocument:
    raw_item = json.dumps(
        {
            "bbox": [10, 10, 900, 100],
            "page_idx": 0,
            "text": "正文",
            "type": "text",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    artifacts = tuple(
        ProviderArtifact(
            role=role,
            relative_path=relative_path,
            sha256="sha256:" + digest * 64,
            size_bytes=128,
            media_type="application/json",
        )
        for role, relative_path, digest in (
            ("content_list", "a_content_list.json", "b"),
            ("content_list_v2", "b_content_list_v2.json", "c"),
            ("middle_json", "c_middle.json", "d"),
            ("model_json", "d_model.json", "e"),
        )
    )
    block = ProviderBlock(
        source_index=0,
        page_index=0,
        order_in_page=0,
        provider_type="text",
        typed_annotation="paragraph",
        provider_level=None,
        bbox=ProviderBBox(10, 10, 900, 100),
        payloads=(ProviderPayload(field="text", item_index=None, text="正文"),),
        referenced_artifact_roles=(),
        raw_item_json=raw_item,
        raw_item_sha256=_sha(raw_item),
    )
    raw_segment = json.dumps(
        {
            "bbox": [10, 120, 900, 300],
            "html": "<table><tr><td>正文</td></tr></table>",
            "index": 0,
            "type": "table",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    segment = ProviderPhysicalTableSegment(
        page_index=0,
        order_in_page=0,
        provider_index=0,
        bbox=ProviderBBox(10, 120, 900, 300),
        page_local_html="<table><tr><td>正文</td></tr></table>",
        crop_artifact_role=None,
        logical_stream_status="unbound",
        raw_segment_json=raw_segment,
        raw_segment_sha256=_sha(raw_segment),
    )
    return ProviderDocument(
        source_pdf_sha256=_SOURCE_SHA,
        parser_version="3.4.4",
        backend="hybrid",
        effort="medium",
        ocr_enabled=False,
        pages=(ProviderPage(page_index=0, page_size=(595.0, 842.0), blocks=(block,)),),
        physical_table_segments=(segment,),
        artifacts=artifacts,
        bundle_sha256=provider_artifact_bundle_sha256(artifacts),
    )


def _target() -> ParserTargetIdentity:
    return ParserTargetIdentity(
        backend="hybrid-http-client",
        effort="medium",
        formula=True,
        full_pdf=True,
        image_analysis=False,
        language="ch",
        method="auto",
        name="MinerU",
        package_version="3.4.4",
        runtime_bundle_identity_sha256="sha256:" + "c" * 64,
        table=True,
    )


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
