from __future__ import annotations

import hashlib
from types import SimpleNamespace
import unittest

from disclosure_anchor.application.contracts.document_asset import (
    AssetExtractionStatus,
    BodyOccurrencePolicy,
    DocumentAsset,
    DocumentAssetContractError,
    DocumentAssetDomain,
    document_asset_id,
)
from disclosure_anchor.application.contracts.publication_safety import (
    GlyphDecodeStatus,
    GlyphMappingProof,
    GlyphMappingSource,
    GlyphToken,
    ProviderObservationTerminal,
    ProviderObservationTerminalKind,
    PublicationSafetyError,
    admit_pdf_tounicode_bfchar_mapping,
    display_text,
    evaluate_publication_gate_v1,
    glyph_token_id,
    semantic_search_segments,
    semantic_text,
    unresolved_glyph_display_fallback,
)


_PDF_SHA = "sha256:" + "1" * 64
_BLOB_SHA = "sha256:" + "2" * 64
_CMAP_BYTES = b"1 beginbfchar\n<8F> <80A1>\nendbfchar\n"
_CMAP_SHA = "sha256:" + hashlib.sha256(_CMAP_BYTES).hexdigest()


def _asset(
    *,
    domain: DocumentAssetDomain,
    source_object_ref: str,
    body_policy: BodyOccurrencePolicy,
    page_annotation_ref: str | None = None,
    page_index: int | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> DocumentAsset:
    return DocumentAsset(
        document_asset_id=document_asset_id(
            document_id="doc_1",
            processing_run_id="run_1",
            domain=domain,
            source_object_ref=source_object_ref,
        ),
        document_id="doc_1",
        processing_run_id="run_1",
        source_pdf_sha256=_PDF_SHA,
        domain=domain,
        source_object_ref=source_object_ref,
        blob_sha256=_BLOB_SHA,
        size_bytes=10,
        mime_type="application/octet-stream",
        filename="asset.bin",
        relationship=None,
        page_annotation_ref=page_annotation_ref,
        page_index=page_index,
        bbox=bbox,
        body_occurrence_policy=body_policy,
        extraction_status=AssetExtractionStatus.INVENTORIED,
    )


def _glyph(*, resolved: bool = False) -> GlyphToken:
    bbox = (10.0, 20.0, 15.0, 30.0)
    raw_code = "0x8f"
    font_ref = "pdf-object:12:0"
    mapping_proof = (
        admit_pdf_tounicode_bfchar_mapping(
            source_pdf_sha256=_PDF_SHA,
            font_object_ref=font_ref,
            raw_code=raw_code,
            semantic_unicode="股",
            mapping_evidence_bytes=_CMAP_BYTES,
            expected_mapping_evidence_sha256=_CMAP_SHA,
        )
        if resolved
        else None
    )
    return GlyphToken(
        glyph_token_id=glyph_token_id(
            source_pdf_sha256=_PDF_SHA,
            page_index=0,
            bbox=bbox,
            raster_ref="raster://glyph/1",
            raw_code=raw_code,
            cid=143,
            gid=20,
            font_object_ref=font_ref,
        ),
        source_pdf_sha256=_PDF_SHA,
        page_index=0,
        bbox=bbox,
        raster_ref="raster://glyph/1",
        raw_code=raw_code,
        cid=143,
        gid=20,
        font_object_ref=font_ref,
        semantic_unicode="股" if resolved else None,
        display_fallback=(
            "股"
            if resolved
            else unresolved_glyph_display_fallback(
                raw_code=raw_code,
                cid=143,
                gid=20,
                font_object_ref=font_ref,
            )
        ),
        decode_status=(
            GlyphDecodeStatus.RESOLVED_FONT_BOUND
            if resolved
            else GlyphDecodeStatus.UNRESOLVED
        ),
        mapping_proof=mapping_proof,
    )


class DocumentAssetContractTest(unittest.TestCase):
    def test_non_page_assets_do_not_create_body_units_or_new_asset_kind(self) -> None:
        asset = _asset(
            domain=DocumentAssetDomain.EMBEDDED_FILE,
            source_object_ref="pdf-object:20:0/EmbeddedFiles/0",
            body_policy=BodyOccurrencePolicy.NONE,
        )

        self.assertIsNone(asset.data_asset_kind)
        self.assertEqual(asset.body_occurrence_policy, BodyOccurrencePolicy.NONE)

    def test_equal_blob_does_not_collapse_distinct_source_occurrences(self) -> None:
        first = _asset(
            domain=DocumentAssetDomain.EMBEDDED_FILE,
            source_object_ref="pdf-object:20:0/EmbeddedFiles/0",
            body_policy=BodyOccurrencePolicy.NONE,
        )
        second = _asset(
            domain=DocumentAssetDomain.EMBEDDED_FILE,
            source_object_ref="pdf-object:21:0/EmbeddedFiles/1",
            body_policy=BodyOccurrencePolicy.NONE,
        )

        self.assertEqual(first.blob_sha256, second.blob_sha256)
        self.assertNotEqual(first.document_asset_id, second.document_asset_id)

    def test_file_attachment_requires_visible_annotation_geometry(self) -> None:
        with self.assertRaises(DocumentAssetContractError):
            _asset(
                domain=DocumentAssetDomain.FILE_ATTACHMENT,
                source_object_ref="pdf-object:30:0",
                body_policy=BodyOccurrencePolicy.VISIBLE_ASSET_REFERENCE,
            )

        visible = _asset(
            domain=DocumentAssetDomain.FILE_ATTACHMENT,
            source_object_ref="pdf-object:30:0",
            body_policy=BodyOccurrencePolicy.VISIBLE_ASSET_REFERENCE,
            page_annotation_ref="pdf-annotation:30:0",
            page_index=2,
            bbox=(10.0, 20.0, 30.0, 40.0),
        )
        self.assertEqual(visible.page_index, 2)

    def test_page_media_cannot_masquerade_as_pdf_attachment(self) -> None:
        with self.assertRaises(DocumentAssetContractError):
            _asset(
                domain=DocumentAssetDomain.PAGE_MEDIA,
                source_object_ref="mineru-image:1",
                body_policy=BodyOccurrencePolicy.VISIBLE_ASSET_REFERENCE,
                page_annotation_ref="invented-attachment",
                page_index=0,
                bbox=(1.0, 1.0, 2.0, 2.0),
            )

    def test_runtime_fake_domain_cannot_bypass_asset_policy(self) -> None:
        class FakeDomain:
            value = "not_a_domain"

        with self.assertRaises(DocumentAssetContractError):
            DocumentAsset(
                document_asset_id=document_asset_id(
                    document_id="doc_1",
                    processing_run_id="run_1",
                    domain=DocumentAssetDomain.EMBEDDED_FILE,
                    source_object_ref="pdf-object:20:0",
                ),
                document_id="doc_1",
                processing_run_id="run_1",
                source_pdf_sha256=_PDF_SHA,
                domain=FakeDomain(),  # type: ignore[arg-type]
                source_object_ref="pdf-object:20:0",
                blob_sha256=_BLOB_SHA,
                size_bytes=10,
                mime_type=None,
                filename=None,
                relationship=None,
                page_annotation_ref=None,
                page_index=None,
                bbox=None,
                body_occurrence_policy=(BodyOccurrencePolicy.VISIBLE_ASSET_REFERENCE),
                extraction_status=AssetExtractionStatus.INVENTORIED,
            )


class PublicationSafetyContractTest(unittest.TestCase):
    def test_publication_gate_consumes_only_closed_audit_metrics(self) -> None:
        clean_metrics = {
            "error_count": 0,
            "coverage": {"uncovered": 0},
            "primary_search": {"missing_carriers": 0},
        }
        clean = evaluate_publication_gate_v1(
            SimpleNamespace(ok=True, metrics=clean_metrics)
        )
        self.assertEqual(clean.decision, "publish")

        blocked = (
            SimpleNamespace(ok=False, metrics=clean_metrics),
            SimpleNamespace(
                ok=True,
                metrics={**clean_metrics, "error_count": 1},
            ),
            SimpleNamespace(
                ok=True,
                metrics={
                    **clean_metrics,
                    "coverage": {"uncovered": 1},
                },
            ),
            SimpleNamespace(
                ok=True,
                metrics={
                    **clean_metrics,
                    "primary_search": {"missing_carriers": 1},
                },
            ),
            SimpleNamespace(ok=True, metrics={"error_count": 0}),
        )
        for report in blocked:
            with self.subTest(metrics=report.metrics, ok=report.ok):
                self.assertEqual(
                    evaluate_publication_gate_v1(report).decision,
                    "block",
                )
    def test_provider_observation_terminal_cannot_create_payload_or_search(
        self,
    ) -> None:
        terminal = ProviderObservationTerminal(
            observation_id="sha256:" + "4" * 64,
            artifact_bindings={
                "content_list": "sha256:" + "5" * 64,
                "content_list_v2": "sha256:" + "8" * 64,
            },
            runtime_identity_sha256="sha256:" + "6" * 64,
            provenance_locator={"artifact_role": "content_list_v2", "index": 6},
            terminal_kind=ProviderObservationTerminalKind.REJECTED_OBSERVATION,
            bound_source_occurrence_id=None,
            terminal_reason="no reader-visible source occurrence",
        )

        self.assertFalse(terminal.creates_reader_visible_occurrence)
        self.assertFalse(terminal.creates_placement_owner)
        self.assertFalse(terminal.creates_payload_leaf)
        self.assertEqual(terminal.search_policy, "none")
        with self.assertRaises(PublicationSafetyError):
            ProviderObservationTerminal(
                **{
                    **terminal.as_dict(),
                    "terminal_kind": terminal.terminal_kind,
                    "creates_payload_leaf": True,
                }
            )
        with self.assertRaises(TypeError):
            terminal.provenance_locator["tampered"] = True  # type: ignore[index]

    def test_source_bound_provider_support_redirects_to_source_occurrence(self) -> None:
        terminal = ProviderObservationTerminal(
            observation_id="sha256:" + "4" * 64,
            artifact_bindings={
                "content_list": "sha256:" + "5" * 64,
                "content_list_v2": "sha256:" + "8" * 64,
            },
            runtime_identity_sha256="sha256:" + "6" * 64,
            provenance_locator={"artifact_role": "content_list", "index": 1},
            terminal_kind=ProviderObservationTerminalKind.SOURCE_BOUND_SUPPORT,
            bound_source_occurrence_id="sha256:" + "7" * 64,
            terminal_reason="exact source selector binding",
        )
        self.assertEqual(terminal.bound_source_occurrence_id, "sha256:" + "7" * 64)
        self.assertFalse(terminal.semantic_authority)

    def test_unresolved_glyph_splits_search_and_never_becomes_semantic_text(
        self,
    ) -> None:
        glyph = _glyph()
        parts = ("金额12", glyph, "34万元")

        self.assertEqual(semantic_search_segments(parts), ("金额12", "34万元"))
        self.assertIsNone(semantic_text(parts))
        self.assertEqual(
            display_text(parts),
            "金额12"
            + unresolved_glyph_display_fallback(
                raw_code="0x8f",
                cid=143,
                gid=20,
                font_object_ref="pdf-object:12:0",
            )
            + "34万元",
        )

    def test_single_pua_or_display_placeholder_is_rejected_even_below_ratio(
        self,
    ) -> None:
        for unsafe in (
            "正常内容很长但中间只有一个\ue000字符",
            "正常内容⟦未解码字形 cid=143⟧尾部",
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(PublicationSafetyError):
                    semantic_search_segments((unsafe,))

    def test_font_bound_resolution_is_searchable_and_emoji_is_not_blanket_blocked(
        self,
    ) -> None:
        resolved = _glyph(resolved=True)
        parts = ("回购", resolved, "票 👨‍👩‍👧‍👦")

        self.assertEqual(semantic_text(parts), "回购股票 👨‍👩‍👧‍👦")
        self.assertEqual(semantic_search_segments(parts), ("回购股票 👨‍👩‍👧‍👦",))

    def test_glyph_guess_or_self_reported_hash_cannot_become_searchable(self) -> None:
        with self.assertRaises(PublicationSafetyError):
            GlyphMappingProof(
                proof_id="sha256:" + "3" * 64,
                source_pdf_sha256=_PDF_SHA,
                font_object_ref="pdf-object:12:0",
                raw_code="0x8f",
                semantic_unicode="福建",
                mapping_source=GlyphMappingSource.PDF_TOUNICODE_BFCHAR,
                mapping_evidence_sha256=_CMAP_SHA,
                matched_pair_sha256="sha256:" + "4" * 64,
            )
        with self.assertRaises(PublicationSafetyError):
            admit_pdf_tounicode_bfchar_mapping(
                source_pdf_sha256=_PDF_SHA,
                font_object_ref="pdf-object:12:0",
                raw_code="0x8f",
                semantic_unicode="福建",
                mapping_evidence_bytes=_CMAP_BYTES,
                expected_mapping_evidence_sha256=_CMAP_SHA,
            )
        unresolved = _glyph()
        with self.assertRaises(PublicationSafetyError):
            GlyphToken(
                **{
                    **{
                        key: value
                        for key, value in unresolved.as_dict().items()
                        if key != "searchable"
                    },
                    "bbox": unresolved.bbox,
                    "decode_status": unresolved.decode_status,
                    "mapping_proof": None,
                    "display_fallback": "福建",
                }
            )


if __name__ == "__main__":
    unittest.main()
