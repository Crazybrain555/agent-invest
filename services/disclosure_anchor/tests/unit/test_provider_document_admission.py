from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.parsers.pdf_text_observation import (
    observe_pdf_text_rectangles,
)
from disclosure_anchor.adapters.storage.provider_document_source import (
    ProviderDocumentFileSource,
)
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
    ProviderDocumentAdmissionError,
    SourcePdfObservation,
    SourcePdfTextObservation,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    ProviderDocumentEnvelope,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.ports.provider_document_source import (
    ProviderDocumentSourceError,
)
from disclosure_anchor.application.services.provider_document_admission import (
    ProviderDocumentAdmission,
)
from disclosure_anchor.domain import (
    entities as e,
)


_SOURCE_SHA = "sha256:" + "a" * 64
_OWNER = "run_01K0000000000000000000000"
_REBUILD = "run_01K0000000000000000000001"
_DOCUMENT = "doc_01K00000000000000000000000"
_PROVIDER_DOCUMENT_ID = "1225087169"
_RECORD_RELPATH = Path(
    "derived/provider_documents/cninfo/000001/1225087169/"
    f"{_OWNER}/provider_document.v1.json"
)


class ProviderDocumentAdmissionTests(unittest.TestCase):
    def test_admits_exact_parse_owner_after_full_bundle_rebuild(self) -> None:
        envelope = _envelope()
        record = provider_document_envelope_to_bytes(envelope)
        source = _FakeSource(record=record, rebuilt=envelope.provider_document)

        run = _run(artifact_hash=_sha_bytes(record))
        admitted = _admission(source).admit(
            document=_document(),
            run=run,
            artifact_owner=run,
            security_code="000001",
        )

        self.assertEqual(admitted.envelope, envelope)
        self.assertEqual(admitted.provider_document, envelope.provider_document)
        self.assertEqual(admitted.provider_document_sha256, _sha_bytes(record))
        self.assertEqual(
            source.calls,
            [
                ("record", _RECORD_RELPATH),
                ("pdf", Path(envelope.source_pdf_relpath)),
                (
                    "bundle",
                    Path(envelope.parser_artifact_root_relpath),
                    _SOURCE_SHA,
                ),
                (
                    "native_text",
                    Path(envelope.source_pdf_relpath),
                    envelope.provider_document,
                    _SOURCE_SHA,
                ),
            ],
        )

    def test_reconciles_only_exact_skeleton_numeric_source_text(self) -> None:
        original = _provider_document()
        page = original.pages[0]
        block = page.blocks[0]
        cases = (
            (
                "本集团2026年第一季度净利差 ，净利息收益率 ，较上季度分别"
                "下降 个基点和 个基点。",
                "本集团2026年第一季度净利差1.77%，净利息收益率1.83%，"
                "较上季度分别下降5个基点和8个基点。",
            ),
            ("股东信息", "3 股东信息"),
            ("年度区间月", "2026年度区间1-3月"),
        )
        for provider_text, native_text in cases:
            with self.subTest(provider_text=provider_text, native_text=native_text):
                provider_document = replace(
                    original,
                    pages=(
                        replace(
                            page,
                            blocks=(
                                replace(
                                    block,
                                    payloads=(
                                        ProviderPayload(
                                            field="text",
                                            item_index=None,
                                            text=provider_text,
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
                envelope = replace(_envelope(), provider_document=provider_document)
                record = provider_document_envelope_to_bytes(envelope)
                source = _FakeSource(
                    record=record,
                    rebuilt=provider_document,
                    text_observations=(
                        SourcePdfTextObservation(
                            source_index=0,
                            page_index=0,
                            payload_ordinal=0,
                            raw_block_sha256=block.raw_item_sha256,
                            text=native_text,
                        ),
                    ),
                )

                admitted = _admission(source).admit(
                    document=_document(),
                    run=_run(artifact_hash=_sha_bytes(record)),
                    artifact_owner=_run(artifact_hash=_sha_bytes(record)),
                    security_code="000001",
                )

                self.assertEqual(
                    admitted.provider_document.blocks[0].payloads[0].text,
                    provider_text,
                )
                self.assertEqual(
                    admitted.effective_provider_document.blocks[0].payloads[0].text,
                    native_text,
                )
                self.assertEqual(len(admitted.source_text_reconciliations), 1)

    def test_preserves_ascii_word_boundary_in_multiline_native_text(self) -> None:
        original = _provider_document()
        page = original.pages[0]
        block = page.blocks[0]
        provider_text = "Company Limited 净利率%。"
        provider_document = replace(
            original,
            pages=(
                replace(
                    page,
                    blocks=(
                        replace(
                            block,
                            payloads=(
                                ProviderPayload(
                                    field="text",
                                    item_index=None,
                                    text=provider_text,
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        envelope = replace(_envelope(), provider_document=provider_document)
        record = provider_document_envelope_to_bytes(envelope)
        native_text = "Company\r\nLimited 净利率1.77%。 "
        source = _FakeSource(
            record=record,
            rebuilt=provider_document,
            text_observations=(
                SourcePdfTextObservation(
                    source_index=0,
                    page_index=0,
                    payload_ordinal=0,
                    raw_block_sha256=block.raw_item_sha256,
                    text=native_text,
                ),
            ),
        )

        admitted = _admission(source).admit(
            document=_document(),
            run=_run(artifact_hash=_sha_bytes(record)),
            artifact_owner=_run(artifact_hash=_sha_bytes(record)),
            security_code="000001",
        )

        self.assertEqual(
            admitted.effective_provider_document.blocks[0].payloads[0].text,
            "Company Limited 净利率1.77%。",
        )

    def test_does_not_reconcile_when_nonnumeric_source_text_differs(self) -> None:
        envelope = _envelope()
        record = provider_document_envelope_to_bytes(envelope)
        block = envelope.provider_document.blocks[0]
        source = _FakeSource(
            record=record,
            rebuilt=envelope.provider_document,
            text_observations=(
                SourcePdfTextObservation(
                    source_index=0,
                    page_index=0,
                    payload_ordinal=0,
                    raw_block_sha256=block.raw_item_sha256,
                    text="2026年其他正文",
                ),
            ),
        )

        admitted = _admission(source).admit(
            document=_document(),
            run=_run(artifact_hash=_sha_bytes(record)),
            artifact_owner=_run(artifact_hash=_sha_bytes(record)),
            security_code="000001",
        )

        self.assertFalse(admitted.source_text_reconciliations)
        self.assertIs(admitted.effective_provider_document, admitted.provider_document)

    def test_does_not_reconcile_numeric_substitution_or_reordering(self) -> None:
        original = _provider_document()
        page = original.pages[0]
        block = page.blocks[0]

        for provider_text, native_text in (
            (
                "本期净利差1.77%，上期净利差1.83%，增加个百分点。",
                "本期净利差1.83%，上期净利差1.77%，增加0.06个百分点。",
            ),
            (
                "本期净利差1.77%，上期净利差1.83%，增加个百分点。",
                "本期净利差1.77%，上期净利差1.82%，增加0.06个百分点。",
            ),
            (
                "本期净利差0.06%。",
                "本期净利差0.06‰。",
            ),
            ("本期净利率1.7%。", "本期净利率1.77%。"),
            ("本期金额1,00元。", "本期金额1,000元。"),
            ("本期金额1,234元。", "本期金额1,2345元。"),
            ("本期金额5元。", "本期金额1,2345元。"),
            ("金额:元", "金额：123元"),
            ("Company A 金额元", "Ｃｏｍｐａｎｙ A 金额123元"),
            ("金额元", "金\x00额123元"),
            ("金额元", "金额12\x003元"),
            ("金额元", "金额123\n元"),
            ("金额元", "金额123\r元"),
            ("金额元", "金额123\r\n\r\n元"),
            ("金额元", "金额123\r\n\u00a0\r\n元"),
            ("金额", "金额123\r\n\u00a0"),
            ("金额元", "金额123\r\n 元"),
            ("金额元", "金额123 \r\n元"),
            ("金额元", "金额123\u00a0元"),
            ("金额元", "金额123\u2028元"),
            ("净利率%。", "净利率1.77\n%。"),
            ("净 利率为 %。", "净利率为 1.77%。"),
            (
                "Company Limited 净利率%。",
                "Company  Limited 净利率1.77%。",
            ),
            ("净利率%。", " 净利率1.77%。"),
            ("净利率%。", "净利率1.77%。 "),
            (
                "Company Limited 净利率%。",
                "Company\r\nLimited 净利率1.77%。  ",
            ),
            (
                "Company Limited 净利率%。",
                "Company\nLimited 净利率1.77%。",
            ),
            (
                "Company Limited 净利率%。",
                "Company\rLimited 净利率1.77%。",
            ),
            (
                "Company Limited 净利率%。",
                "Company\r\n\r\nLimited 净利率1.77%。",
            ),
            ("金额元", "金额123元  \r\n"),
            ("金额元", "金额123元\r\n  "),
            ("金额元", "金额123元 \r\n "),
        ):
            with self.subTest(provider_text=provider_text, native_text=native_text):
                provider_document = replace(
                    original,
                    pages=(
                        replace(
                            page,
                            blocks=(
                                replace(
                                    block,
                                    payloads=(
                                        ProviderPayload(
                                            field="text",
                                            item_index=None,
                                            text=provider_text,
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
                envelope = replace(_envelope(), provider_document=provider_document)
                record = provider_document_envelope_to_bytes(envelope)
                source = _FakeSource(
                    record=record,
                    rebuilt=provider_document,
                    text_observations=(
                        SourcePdfTextObservation(
                            source_index=0,
                            page_index=0,
                            payload_ordinal=0,
                            raw_block_sha256=block.raw_item_sha256,
                            text=native_text,
                        ),
                    ),
                )

                admitted = _admission(source).admit(
                    document=_document(),
                    run=_run(artifact_hash=_sha_bytes(record)),
                    artifact_owner=_run(artifact_hash=_sha_bytes(record)),
                    security_code="000001",
                )

                self.assertFalse(admitted.source_text_reconciliations)
                self.assertIs(
                    admitted.effective_provider_document,
                    admitted.provider_document,
                )

    def test_admits_rebuild_only_through_its_exact_parse_owner(self) -> None:
        envelope = _envelope()
        record = provider_document_envelope_to_bytes(envelope)
        source = _FakeSource(record=record, rebuilt=envelope.provider_document)
        owner = _run(artifact_hash=_sha_bytes(record))
        rebuild = replace(
            owner,
            processing_run_id=_REBUILD,
            run_kind="rebuild_units",
            artifact_owner_processing_run_id=_OWNER,
        )

        admitted = _admission(source).admit(
            document=_document(),
            run=rebuild,
            artifact_owner=owner,
            security_code="000001",
        )

        self.assertEqual(admitted.envelope.artifact_owner_processing_run_id, _OWNER)
        drifted = replace(rebuild, artifact_hash="sha256:" + "0" * 64)
        with self.assertRaises(ProviderDocumentAdmissionError) as caught:
            _admission(source).admit(
                document=_document(),
                run=drifted,
                artifact_owner=owner,
                security_code="000001",
            )
        self.assertEqual(caught.exception.reason_code, "parse_owner_identity_mismatch")

    def test_rejects_legacy_dual_or_ineligible_parse_owner(self) -> None:
        record = provider_document_envelope_to_bytes(_envelope())
        source = _FakeSource(record=record, rebuilt=_provider_document())
        invalid_runs = (
            replace(_run(), normalized_ir_relpath="derived/normalized_ir/v4.json"),
            replace(_run(), status="failed"),
            replace(_run(), run_kind="rebuild_units"),
            replace(_run(), artifact_owner_processing_run_id="run_other"),
        )

        for run in invalid_runs:
            with (
                self.subTest(run=run),
                self.assertRaises(ProviderDocumentAdmissionError),
            ):
                _admission(source).admit(
                    document=_document(),
                    run=run,
                    artifact_owner=run,
                    security_code="000001",
                )

    def test_rejects_path_record_and_source_identity_drift(self) -> None:
        envelope = _envelope()
        record = provider_document_envelope_to_bytes(envelope)
        cases = (
            (
                _FakeSource(record=record, rebuilt=envelope.provider_document),
                replace(
                    _run(artifact_hash=_sha_bytes(record)),
                    provider_document_relpath=(
                        "derived/provider_documents/wrong/provider_document.v1.json"
                    ),
                ),
                "provider_document_path_mismatch",
            ),
            (
                _FakeSource(record=record, rebuilt=envelope.provider_document),
                _run(artifact_hash="sha256:" + "0" * 64),
                "provider_document_hash_mismatch",
            ),
            (
                _FakeSource(
                    record=record,
                    rebuilt=envelope.provider_document,
                    observation=SourcePdfObservation(
                        sha256="sha256:" + "0" * 64,
                        page_count=1,
                    ),
                ),
                _run(artifact_hash=_sha_bytes(record)),
                "source_pdf_identity_mismatch",
            ),
        )

        for source, run, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                with self.assertRaises(ProviderDocumentAdmissionError) as caught:
                    _admission(source).admit(
                        document=_document(),
                        run=run,
                        artifact_owner=run,
                        security_code="000001",
                    )
                self.assertEqual(caught.exception.reason_code, reason_code)

    def test_full_reader_rejects_typed_payload_and_segment_drift(self) -> None:
        original = _envelope()
        page = original.provider_document.pages[0]
        block = page.blocks[0]
        segment = original.provider_document.physical_table_segments[0]
        mutated_documents = (
            replace(
                original.provider_document,
                pages=(
                    replace(
                        page,
                        blocks=(
                            replace(
                                block,
                                payloads=(
                                    replace(block.payloads[0], text="伪造正文"),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            replace(
                original.provider_document,
                physical_table_segments=(
                    replace(
                        segment,
                        provider_index=99,
                        page_local_html="<table><tr><td>伪造</td></tr></table>",
                        logical_stream_status="retained",
                    ),
                ),
            ),
        )

        for mutated_document in mutated_documents:
            mutated = replace(original, provider_document=mutated_document)
            record = provider_document_envelope_to_bytes(mutated)
            source = _FakeSource(
                record=record,
                rebuilt=original.provider_document,
            )
            run = _run(artifact_hash=_sha_bytes(record))
            with self.assertRaises(ProviderDocumentAdmissionError) as caught:
                _admission(source).admit(
                    document=_document(),
                    run=run,
                    artifact_owner=run,
                    security_code="000001",
                )
            self.assertEqual(
                caught.exception.reason_code,
                "provider_document_projection_mismatch",
            )

    def test_rejects_run_target_and_document_identity_drift(self) -> None:
        envelope = _envelope()
        record = provider_document_envelope_to_bytes(envelope)
        source = _FakeSource(record=record, rebuilt=envelope.provider_document)
        cases = (
            (_document(), replace(_run(), parser_version="3.4.3")),
            (replace(_document(), raw_file_hash="sha256:" + "0" * 64), _run()),
        )

        for document, run in cases:
            with self.assertRaises(ProviderDocumentAdmissionError):
                _admission(source).admit(
                    document=document,
                    run=run,
                    artifact_owner=run,
                    security_code="000001",
                )

    def test_wraps_nested_envelope_value_errors_as_contract_failures(self) -> None:
        payload = json.loads(
            provider_document_envelope_to_bytes(_envelope()).decode("utf-8")
        )
        payload["provider_document"]["artifacts"][0]["media_type"] = "INVALID"
        record = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        source = _FakeSource(record=record, rebuilt=_provider_document())

        run = _run(artifact_hash=_sha_bytes(record))
        with self.assertRaises(ProviderDocumentAdmissionError) as caught:
            _admission(source).admit(
                document=_document(),
                run=run,
                artifact_owner=run,
                security_code="000001",
            )

        self.assertEqual(
            caught.exception.reason_code,
            "provider_document_contract_invalid",
        )


class ProviderDocumentFileSourceTests(unittest.TestCase):
    def test_reads_regular_record_observes_stable_pdf_and_rebuilds_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record_relpath = Path("derived/provider_documents/x.json")
            pdf_relpath = Path("raw_documents/source.pdf")
            bundle_relpath = Path("parser_artifacts/run/hybrid_auto")
            _write(root / record_relpath, b"record")
            _write(root / pdf_relpath, b"%PDF-test")
            (root / bundle_relpath).mkdir(parents=True)
            reader = _FakeReader(_provider_document())
            source = ProviderDocumentFileSource(
                _DataPaths(root),  # type: ignore[arg-type]
                text_reader=lambda _path, *, document: (),
                reader=reader,  # type: ignore[arg-type]
                page_counter=lambda _path: 1,
            )

            self.assertEqual(
                source.read_provider_document_record(record_relpath),
                b"record",
            )
            observation = source.observe_source_pdf(pdf_relpath)
            self.assertEqual(observation.sha256, _sha_bytes(b"%PDF-test"))
            self.assertEqual(observation.page_count, 1)
            self.assertEqual(
                source.rebuild_provider_document(
                    bundle_relpath,
                    source_pdf_sha256=_SOURCE_SHA,
                ),
                _provider_document(),
            )
            self.assertEqual(reader.calls, [(root / bundle_relpath, _SOURCE_SHA)])

    def test_rejects_symlink_and_pdf_changed_during_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "record.json"
            target.write_bytes(b"record")
            link_relpath = Path("derived/provider_documents/link.json")
            link = root / link_relpath
            link.parent.mkdir(parents=True)
            link.symlink_to(target)
            source = ProviderDocumentFileSource(
                _DataPaths(root),  # type: ignore[arg-type]
                text_reader=lambda _path, *, document: (),
                page_counter=lambda _path: 1,
            )
            with self.assertRaises(ProviderDocumentSourceError) as symlink_error:
                source.read_provider_document_record(link_relpath)
            self.assertFalse(symlink_error.exception.retryable)

            with tempfile.TemporaryDirectory() as external_temporary:
                external = Path(external_temporary)
                _write(external / "source.pdf", b"%PDF-external")
                intermediate_relpath = Path("escape/source.pdf")
                (root / "escape").symlink_to(external, target_is_directory=True)
                with self.assertRaises(
                    ProviderDocumentSourceError
                ) as intermediate_error:
                    source.observe_source_pdf(intermediate_relpath)
                self.assertFalse(intermediate_error.exception.retryable)

            pdf_relpath = Path("raw_documents/source.pdf")
            pdf_path = root / pdf_relpath
            _write(pdf_path, b"%PDF-before")

            def mutate_pdf(path: Path) -> int:
                path.write_bytes(b"%PDF-after")
                return 1

            changing = ProviderDocumentFileSource(
                _DataPaths(root),  # type: ignore[arg-type]
                text_reader=lambda _path, *, document: (),
                page_counter=mutate_pdf,
            )
            with self.assertRaises(ProviderDocumentSourceError) as caught:
                changing.observe_source_pdf(pdf_relpath)
            self.assertEqual(caught.exception.reason_code, "source_pdf_changed")

    def test_rejects_invalid_page_counter_result_as_controlled_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf_relpath = Path("raw_documents/source.pdf")
            _write(root / pdf_relpath, b"%PDF-test")
            source = ProviderDocumentFileSource(
                _DataPaths(root),  # type: ignore[arg-type]
                text_reader=lambda _path, *, document: (),
                page_counter=lambda _path: 0,
            )

            with self.assertRaises(ProviderDocumentSourceError) as caught:
                source.observe_source_pdf(pdf_relpath)

            self.assertEqual(caught.exception.reason_code, "source_pdf_read_failed")
            self.assertFalse(caught.exception.retryable)


class PdfTextObservationTests(unittest.TestCase):
    def test_reads_only_provider_text_bbox_with_pdf_coordinates(self) -> None:
        text_page = _FakePdfTextPage("2026年1-3月，本集团净利差1.77%。")
        page = _FakePdfPage(
            text_page,
            bbox=(10.0, 20.0, 1_010.0, 2_020.0),
        )
        pdf = _FakePdfDocument(page)
        document = _provider_document()
        document = replace(
            document,
            pages=(replace(document.pages[0], page_size=(1_000.0, 2_000.0)),),
        )

        with patch(
            "disclosure_anchor.adapters.parsers.pdf_text_observation.pdfium.PdfDocument",
            return_value=pdf,
        ):
            observations = observe_pdf_text_rectangles(
                Path("source.pdf"),
                document=document,
            )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].source_index, 0)
        self.assertEqual(observations[0].text, "2026年1-3月，本集团净利差1.77%。")
        self.assertEqual(
            text_page.bounds,
            [
                {
                    "left": 20.0,
                    "bottom": 1_820.0,
                    "right": 910.0,
                    "top": 2_000.0,
                }
            ],
        )
        self.assertTrue(text_page.closed)
        self.assertTrue(page.closed)
        self.assertTrue(pdf.closed)

    def test_skips_rotated_or_page_shape_mismatched_native_text(self) -> None:
        document = _provider_document()
        document = replace(
            document,
            pages=(replace(document.pages[0], page_size=(1_000.0, 2_000.0)),),
        )
        for page in (
            _FakePdfPage(_FakePdfTextPage("不应读取"), rotation=90),
            _FakePdfPage(
                _FakePdfTextPage("不应读取"),
                bbox=(0.0, 0.0, 1_000.0, 1_000.0),
            ),
        ):
            with self.subTest(rotation=page.rotation, bbox=page.bbox):
                pdf = _FakePdfDocument(page)
                with patch(
                    "disclosure_anchor.adapters.parsers.pdf_text_observation.pdfium.PdfDocument",
                    return_value=pdf,
                ):
                    observations = observe_pdf_text_rectangles(
                        Path("source.pdf"),
                        document=document,
                    )
                self.assertEqual(observations, ())
                self.assertEqual(page.text_page.bounds, [])
                self.assertTrue(page.closed)
                self.assertTrue(pdf.closed)

    def test_skips_duplicate_bbox_but_keeps_adjacent_line_observations(self) -> None:
        base = _provider_document()
        first = base.blocks[0]
        page = base.pages[0]
        for second_bbox, expected_count in (
            (first.bbox, 0),
            (ProviderBBox(10, 99, 900, 190), 2),
        ):
            with self.subTest(second_bbox=second_bbox, expected_count=expected_count):
                assert second_bbox is not None
                second = replace(
                    first,
                    source_index=1,
                    order_in_page=1,
                    bbox=second_bbox,
                )
                document = replace(
                    base,
                    pages=(
                        replace(
                            page,
                            page_size=(1_000.0, 2_000.0),
                            blocks=(first, second),
                        ),
                    ),
                )
                text_page = _FakePdfTextPage("2026年第一季度")
                pdf_page = _FakePdfPage(text_page)
                pdf = _FakePdfDocument(pdf_page)
                with patch(
                    "disclosure_anchor.adapters.parsers.pdf_text_observation.pdfium.PdfDocument",
                    return_value=pdf,
                ):
                    observations = observe_pdf_text_rectangles(
                        Path("source.pdf"),
                        document=document,
                    )

                self.assertEqual(len(observations), expected_count)
                self.assertEqual(len(text_page.bounds), expected_count)


class _FakeSource:
    def __init__(
        self,
        *,
        record: bytes,
        rebuilt: ProviderDocument,
        observation: SourcePdfObservation | None = None,
        text_observations: tuple[SourcePdfTextObservation, ...] = (),
    ) -> None:
        self.record = record
        self.rebuilt = rebuilt
        self.observation = observation or SourcePdfObservation(
            sha256=_SOURCE_SHA,
            page_count=1,
        )
        self.text_observations = text_observations
        self.calls: list[tuple[object, ...]] = []

    def read_provider_document_record(self, relpath: Path) -> bytes:
        self.calls.append(("record", relpath))
        return self.record

    def observe_source_pdf(self, relpath: Path) -> SourcePdfObservation:
        self.calls.append(("pdf", relpath))
        return self.observation

    def rebuild_provider_document(
        self,
        bundle_relpath: Path,
        *,
        source_pdf_sha256: str,
    ) -> ProviderDocument:
        self.calls.append(("bundle", bundle_relpath, source_pdf_sha256))
        return self.rebuilt

    def observe_source_pdf_text(
        self,
        relpath: Path,
        *,
        document: ProviderDocument,
        expected_sha256: str,
    ) -> tuple[SourcePdfTextObservation, ...]:
        self.calls.append(("native_text", relpath, document, expected_sha256))
        return self.text_observations


class _FakeReader:
    def __init__(self, document: ProviderDocument) -> None:
        self.document = document
        self.calls: list[tuple[Path, str]] = []

    def read(self, output_dir: Path, *, source_pdf_sha256: str) -> ProviderDocument:
        self.calls.append((output_dir, source_pdf_sha256))
        return self.document


class _FakePdfTextPage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.bounds: list[dict[str, float]] = []
        self.closed = False

    def get_text_bounded(self, **bounds: float) -> str:
        self.bounds.append(bounds)
        return self.text

    def close(self) -> None:
        self.closed = True


class _FakePdfPage:
    def __init__(
        self,
        text_page: _FakePdfTextPage,
        *,
        bbox: tuple[float, float, float, float] = (0.0, 0.0, 1_000.0, 2_000.0),
        rotation: int = 0,
    ) -> None:
        self.text_page = text_page
        self.bbox = bbox
        self.rotation = rotation
        self.closed = False

    def get_bbox(self) -> tuple[float, float, float, float]:
        return self.bbox

    def get_rotation(self) -> int:
        return self.rotation

    def get_textpage(self) -> _FakePdfTextPage:
        return self.text_page

    def close(self) -> None:
        self.closed = True


class _FakePdfDocument:
    def __init__(self, page: _FakePdfPage) -> None:
        self.page = page
        self.closed = False

    def __len__(self) -> int:
        return 1

    def __getitem__(self, index: int) -> _FakePdfPage:
        if index != 0:
            raise IndexError(index)
        return self.page

    def close(self) -> None:
        self.closed = True


class _PathBuilder:
    def provider_document_relpath(
        self,
        *,
        provider: str,
        security_code: str,
        provider_document_id: str,
        artifact_owner_processing_run_id: str,
    ) -> Path:
        return Path(
            "derived/provider_documents"
        ) / provider / security_code / provider_document_id / (
            artifact_owner_processing_run_id
        ) / "provider_document.v1.json"


class _DataPaths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def data_path(self, relpath: Path) -> Path:
        return self.root / relpath


def _admission(source: _FakeSource) -> ProviderDocumentAdmission:
    return ProviderDocumentAdmission(
        path_builder=_PathBuilder(),  # type: ignore[arg-type]
        source=source,
    )


def _document() -> e.Document:
    return e.Document(
        document_id=_DOCUMENT,
        status="parsed",
        security_id="sec_01K00000000000000000000000",
        provider="cninfo",
        provider_document_id=_PROVIDER_DOCUMENT_ID,
        raw_file_relpath=(
            f"raw_documents/cninfo/000001/2026/1225087169/sha256_{'a' * 64}.pdf"
        ),
        raw_file_hash=_SOURCE_SHA,
    )


def _run(*, artifact_hash: str | None = None) -> e.ProcessingRun:
    return e.ProcessingRun(
        processing_run_id=_OWNER,
        document_id=_DOCUMENT,
        artifact_owner_processing_run_id=_OWNER,
        run_kind="parse",
        status="succeeded",
        parser_name="MinerU",
        parser_version="3.4.4",
        parser_backend="hybrid-http-client",
        parser_method="auto",
        parser_language="ch",
        parser_target_identity=_target().to_payload(),
        input_raw_file_hash=_SOURCE_SHA,
        parser_artifact_relpath=_envelope().parser_artifact_root_relpath,
        artifact_hash=artifact_hash or _sha_bytes(
            provider_document_envelope_to_bytes(_envelope())
        ),
        normalized_ir_relpath=None,
        provider_document_relpath=_RECORD_RELPATH.as_posix(),
    )


def _envelope() -> ProviderDocumentEnvelope:
    return ProviderDocumentEnvelope.build(
        document_id=_DOCUMENT,
        artifact_owner_processing_run_id=_OWNER,
        provider="cninfo",
        provider_document_id=_PROVIDER_DOCUMENT_ID,
        source_pdf_relpath=(
            f"raw_documents/cninfo/000001/2026/1225087169/sha256_{'a' * 64}.pdf"
        ),
        source_pdf_page_count=1,
        parser_artifact_root_relpath=(
            "parser_artifacts/cninfo/000001/1225087169/"
            f"{_OWNER}/sha256_{'a' * 64}/hybrid_auto"
        ),
        parser_target_identity=_target(),
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
        raw_item_sha256=_sha_text(raw_item),
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
        raw_segment_sha256=_sha_text(raw_segment),
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


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _sha_text(value: str) -> str:
    return _sha_bytes(value.encode("utf-8"))


def _sha_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


if __name__ == "__main__":
    unittest.main()
