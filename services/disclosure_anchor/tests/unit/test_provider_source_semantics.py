"""Hand-derived source repair/finding invariants, independent of the snapshots."""

from dataclasses import FrozenInstanceError, replace
import unittest

from disclosure_anchor.application.contracts import provider_document_admission as old
from disclosure_anchor.application.contracts import provider_source_semantics as pure
from disclosure_anchor.application.services.provider_source_semantics import derive_source_semantics
from tests._provider_source_semantics_fixture import block, document, observation, sha


class ProviderSourceSemanticsTests(unittest.TestCase):
    def derive(self, provider, native, *, kind="text"):
        item = block(0, 0, 0, provider, kind=kind)
        doc = document(((item,),))
        return derive_source_semantics(document=doc, observations=(observation(item, native),))

    def test_old_observation_and_provenance_exports_are_identical_classes(self):
        for name in ("SourcePdfObservation", "SourcePdfTextObservation",
                     "SourceTextReconciliation", "SourceQualityFinding"):
            with self.subTest(name=name):
                self.assertIs(getattr(old, name), getattr(pure, name))

    def test_literal_repairs_retain_original_raw_and_exact_replacement_provenance(self):
        for provider, native, expected_kind in (
            ("净利润万元。", "净利润25万元。", "source_pdf_native_numeric.v1"),
            ("简称债”", "简称“2026债”", "source_pdf_native_identifier.v1"),
            ("折算比例", "折算比例=12", "source_pdf_native_identifier.v2"),
        ):
            with self.subTest(kind=expected_kind):
                semantics = self.derive(provider, native)
                self.assertEqual(len(semantics.source_text_reconciliations), 1)
                repair, = semantics.source_text_reconciliations
                original, = semantics.provider_document.blocks
                effective, = semantics.effective_provider_document.blocks
                self.assertEqual(repair.source_kind, expected_kind)
                self.assertEqual((repair.source_index, repair.payload_ordinal), (0, 0))
                self.assertEqual(repair.source_text, native)
                self.assertEqual(repair.provider_text_sha256, sha(provider))
                self.assertEqual(repair.source_text_sha256, sha(native))
                self.assertEqual(repair.raw_block_sha256, sha(original.raw_item_json))
                self.assertEqual(original.payloads[0].text, provider)
                self.assertEqual(effective.payloads[0].text, native)
                self.assertEqual(effective.raw_item_json, original.raw_item_json)
                self.assertEqual(effective.raw_item_sha256, original.raw_item_sha256)
                self.assertEqual(semantics.source_quality_findings, ())

    def test_findings_preserve_payload_and_bind_exact_native_observation(self):
        for provider, native, expected_kind, reason in (
            ("公告2026〕7号", "公告〔2026〕7号", "source_pdf_native_text_quality.v2", "cjk_bracket_omission"),
            ("支付元。", "支付利息20元。", "source_pdf_native_text_quality.v1", "native_text_omission"),
            ("投资 A类1234元", "投资A类12345元", "source_pdf_native_text_quality.v3", "numeric_token_truncation"),
        ):
            with self.subTest(reason=reason, kind=expected_kind):
                semantics = self.derive(provider, native)
                self.assertEqual(semantics.source_text_reconciliations, ())
                finding, = semantics.source_quality_findings
                self.assertEqual((finding.source_kind, finding.reason), (expected_kind, reason))
                self.assertEqual(finding.provider_text_sha256, sha(provider))
                self.assertEqual(finding.source_text_sha256, sha(native))
                self.assertEqual(semantics.effective_provider_document, semantics.provider_document)

    def test_table_findings_and_identifier_precedence_do_not_repair_cells(self):
        rows = "<table>" + "".join(f"<tr><td>{i}</td></tr>" for i in range(1, 9)) + "</table>"
        for html, native, expected_kind, reason in (
            ("<table><tr><td>1</td></tr><tr><td></td></tr><tr><td></td></tr></table>",
             "1 2 3", "source_pdf_native_table_quality.v1", "empty_table_tail"),
            ("<table><tr><td>1.234,567.89</td></tr></table>",
             "1,234,567.89", "source_pdf_native_table_quality.v1", "malformed_numeric_grouping"),
            (rows, "1 2 3 4 5 6 7 9", "source_pdf_native_table_quality.v1", "numeric_token_mismatch"),
            ("<table><tr><td>统一社会信用代码</td><td>00000000000000000O</td></tr></table>",
             "统一社会信用代码 000000000000000000", "source_pdf_native_identifier_quality.v1", "identifier_confusable_mismatch"),
        ):
            with self.subTest(reason=reason):
                semantics = self.derive(html, native, kind="table")
                finding, = semantics.source_quality_findings
                self.assertEqual((finding.source_kind, finding.reason), (expected_kind, reason))
                self.assertEqual(finding.provider_text_sha256, sha(html))
                self.assertEqual(finding.source_text_sha256, sha(native))
                self.assertEqual(semantics.source_text_reconciliations, ())
                self.assertEqual(semantics.effective_provider_document.blocks[0].payloads[0].text, html)

    def test_adjacent_negative_text_and_observation_abstention_do_not_authorize_repair(self):
        for provider, native in (
            ("净利润25万元。", "净利润25万元。"),
            ("净利润24万元。", "净利润25万元。"),
            ("净利润万元。", "净利润1,2345万元。"),
            ("净利润万元。", "净利润25\n万元。"),
            ("净利润万元。", "\r\n净利润25万元。"),
            ("净利润万元。", "净利润25\r\n\r\n万元。"),
            ("净利润万元。", "净利润25万元。 "),
            ("折算比例", "折算比例=12=13"),
        ):
            with self.subTest(native=native):
                semantics = self.derive(provider, native)
                self.assertEqual(semantics.source_text_reconciliations, ())
                self.assertEqual(semantics.effective_provider_document, semantics.provider_document)
        doc = document(((block(0, 0, 0, "没有可用原生观察"),),))
        semantics = derive_source_semantics(document=doc, observations=())
        self.assertEqual(semantics.source_text_reconciliations, ())
        self.assertEqual(semantics.source_quality_findings, ())
        self.assertIs(semantics.effective_provider_document, doc)

    def test_adjacent_table_and_bracket_findings_abstain_without_their_proof(self):
        for provider, native, kind in (
            ("公告2026〕7号", "公告〔2026〕8号", "text"),
            ("<table><tr><td>登记号码</td><td>00000000000000000O</td></tr></table>",
             "登记号码 000000000000000000", "table"),
            ("<table><tr><td>1</td></tr><tr><td></td></tr></table>", "1 2", "table"),
        ):
            with self.subTest(provider=provider):
                self.assertEqual(self.derive(provider, native, kind=kind).source_quality_findings, ())

    def test_only_proven_isolated_crlf_normalization_enters_replacement(self):
        semantics = self.derive("净利润万元。", "净利润25\r\n万元。 ")
        repair, = semantics.source_text_reconciliations
        self.assertEqual(repair.source_text, "净利润25万元。")
        self.assertEqual(repair.source_text_sha256, sha("净利润25万元。"))

    def test_observation_identity_order_and_supported_payload_are_checked(self):
        first = block(0, 0, 0, "第一项元")
        second = block(1, 0, 1, "第二项元")
        doc = document(((first, second),))
        a, b = observation(first, "第一项1元"), observation(second, "第二项2元")
        for observations in (
            (b, a), (a, a), (replace(a, source_index=2),),
            (replace(a, page_index=1),), (replace(a, payload_ordinal=1),),
            (replace(a, raw_block_sha256=sha("other raw")),),
        ):
            with self.subTest(observations=observations), self.assertRaises(ValueError):
                derive_source_semantics(document=doc, observations=observations)
        image = block(0, 0, 0, "图示", kind="image", visual=True)
        with self.assertRaises(ValueError):
            derive_source_semantics(document=document(((image,),)), observations=(observation(image, "图示1"),))

    def test_semantic_record_rejects_unbound_duplicate_and_overlapping_provenance(self):
        semantics = self.derive("金额元", "金额5元")
        repair, = semantics.source_text_reconciliations
        doc = semantics.provider_document
        finding = pure.SourceQualityFinding(0, 0, repair.raw_block_sha256,
                                            repair.provider_text_sha256, sha("金额5元"),
                                            "native_text_omission", "source_pdf_native_text_quality.v1")
        for repairs, findings in (
            ((repair, repair), ()),
            ((replace(repair, source_index=1),), ()),
            ((replace(repair, payload_ordinal=1),), ()),
            ((replace(repair, raw_block_sha256=sha("different raw")),), ()),
            ((replace(repair, provider_text_sha256=sha("different text")),), ()),
            ((repair,), (finding,)),
            ((), (finding, finding)),
            ((), (replace(finding, provider_text_sha256=sha("different text")),)),
        ):
            with self.subTest(repairs=repairs, findings=findings), self.assertRaises(ValueError):
                pure.ProviderSourceSemantics(doc, repairs, findings)
        for change in ({"source_kind": "invented.v1"}, {"source_text_sha256": sha("other")}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(repair, **change)
        with self.assertRaises(FrozenInstanceError):
            semantics.provider_document = document(((),))

    def test_multiple_repairs_and_findings_keep_order_without_double_ownership(self):
        items = (block(0, 0, 0, "金额元"), block(1, 0, 1, "公告2026〕7号"),
                 block(2, 1, 0, "比例%"))
        observations = (observation(items[0], "金额5元"), observation(items[1], "公告〔2026〕7号"),
                        observation(items[2], "比例8%"))
        semantics = derive_source_semantics(document=document((items[:2], items[2:])), observations=observations)
        self.assertEqual([r.source_index for r in semantics.source_text_reconciliations], [0, 2])
        self.assertEqual([f.source_index for f in semantics.source_quality_findings], [1])
        self.assertEqual([b.payloads[0].text for b in semantics.effective_provider_document.blocks],
                         ["金额5元", "公告2026〕7号", "比例8%"])


if __name__ == "__main__":
    unittest.main()
