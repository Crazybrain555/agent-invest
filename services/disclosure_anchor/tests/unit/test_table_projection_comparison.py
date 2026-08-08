"""Acceptance matrix for the reader-visible table comparison (v7).

Markup can never change equality; any reader-visible fact change must be
rejected; the audit replays the comparison from raw bytes and never
consumes a producer verdict; v6 stays readable and only v7 publishes.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from disclosure_anchor.application.contracts.normalized_ir_table_reconciliation import (
    ReconciliationCompatibility,
    TableReconciliationContractError,
    assess_normalized_ir_table_reconciliation,
)
from disclosure_anchor.application.contracts.table_comparison import (
    TableComparisonError,
    prove_unique_bijection,
    comparable_table,
    replay_page_local_table_comparison,
)
from disclosure_anchor.application.contracts.table_projection import (
    TableProjectionError,
    project_table_html,
)

_BASE = (
    '<table><tr><th>项目</th><th>金额</th></tr>'
    '<tr><td>营业收入</td><td>1,234.56</td></tr>'
    '<tr><td><img src="images/a.png"/>附图</td><td></td></tr></table>'
)


def _sha(html: str) -> str:
    return project_table_html(html).body().sha256()


class MarkupInvarianceTests(unittest.TestCase):
    """Markup, styling, and hidden attributes never affect equality."""

    def test_markup_only_changes_stay_equal(self) -> None:
        base = _sha(_BASE)
        variants = {
            "attr_order_and_noise": (
                '<table class="x" data-k="1"><tr><th style="color:red">项目'
                '</th><th id="h2">金额</th></tr>'
                '<tr><td>营业收入</td><td>1,234.56</td></tr>'
                '<tr><td><img alt="册" title="t" src="other/b.jpg"/>附图'
                "</td><td></td></tr></table>"
            ),
            "comments_and_scripts": (
                "<table><!-- c --><tr><th>项目</th><th>金额</th></tr>"
                "<tr><td>营业收入<script>x()</script></td>"
                "<td>1,234.56<style>.a{}</style></td></tr>"
                '<tr><td><img src="images/a.png"/>附图</td>'
                "<td><template>模板</template><noscript>无脚本</noscript>"
                "</td></tr></table>"
            ),
            "inline_wrappers": (
                "<table><tr><th><span><b>项目</b></span></th>"
                "<th><font>金额</font></th></tr>"
                "<tr><td><i>营业收入</i></td><td>1,234.56</td></tr>"
                '<tr><td><img src="images/a.png"/><u>附图</u></td>'
                "<td></td></tr></table>"
            ),
            "entities_and_whitespace": (
                "<table><tr><th>\n 项目 </th><th>金&#x989d;</th></tr>"
                "<tr><td>营业收入</td><td>1,234.56</td></tr>"
                '<tr><td><img src="images/a.png"/>附图</td>'
                "<td>  </td></tr></table>"
            ),
        }
        for label, html in variants.items():
            with self.subTest(label=label):
                self.assertEqual(_sha(html), base)

    def test_hidden_content_changes_stay_equal(self) -> None:
        with_hidden = _BASE.replace(
            "<td></td></tr></table>",
            '<td><span hidden>隐藏甲</span></td></tr></table>',
        )
        other_hidden = _BASE.replace(
            "<td></td></tr></table>",
            '<td><span hidden>隐藏乙不同</span></td></tr></table>',
        )
        self.assertEqual(_sha(with_hidden), _sha(other_hidden))
        self.assertEqual(_sha(with_hidden), _sha(_BASE))

    def test_br_is_a_stable_boundary_not_markup(self) -> None:
        joined = "<table><tr><td>甲乙</td></tr></table>"
        broken = "<table><tr><td>甲<br>乙</td></tr></table>"
        spaced = "<table><tr><td>甲 乙</td></tr></table>"
        self.assertNotEqual(_sha(joined), _sha(broken))
        self.assertEqual(_sha(spaced), _sha(broken))


class VisibleFactRejectionTests(unittest.TestCase):
    """Every reader-visible fact change must break equality."""

    def test_visible_fact_changes_break_equality(self) -> None:
        base = _sha(_BASE)
        mutations = {
            "single_code_point": _BASE.replace("1,234.56", "1,234.57"),
            "duplicate_cell_dropped": _BASE.replace(
                "<tr><td>营业收入</td><td>1,234.56</td></tr>", "", 1
            ),
            "cell_position_swap": _BASE.replace(
                "<td>营业收入</td><td>1,234.56</td>",
                "<td>1,234.56</td><td>营业收入</td>",
            ),
            "rowspan_change": _BASE.replace(
                "<td>营业收入</td>", '<td rowspan="2">营业收入</td>'
            ),
            "role_flip": _BASE.replace("<th>项目</th>", "<td>项目</td>"),
            "empty_cell_removed": _BASE.replace(
                '<td><img src="images/a.png"/>附图</td><td></td>',
                '<td><img src="images/a.png"/>附图</td>',
            ),
            "media_removed": _BASE.replace('<img src="images/a.png"/>', ""),
            "media_reordered": _BASE.replace(
                '<img src="images/a.png"/>附图', '附图<img src="images/a.png"/>'
            ),
            "media_moved_cell": _BASE.replace(
                '<td><img src="images/a.png"/>附图</td><td></td>',
                '<td>附图</td><td><img src="images/a.png"/></td>',
            ),
            "visible_becomes_hidden": _BASE.replace(
                "<td>营业收入</td>", "<td hidden>营业收入</td>"
            ),
        }
        for label, html in mutations.items():
            with self.subTest(label=label):
                self.assertNotEqual(_sha(html), base)

    def test_structural_damage_is_blocked_not_guessed(self) -> None:
        for label, html in {
            "nested_table": (
                "<table><tr><td><table></table></td></tr></table>"
            ),
            "overlapping_spans": (
                '<table><tr><td>p</td><td rowspan="2">q</td></tr>'
                '<tr><td colspan="2">r</td></tr></table>'
            ),
        }.items():
            with self.subTest(label=label):
                with self.assertRaises(TableProjectionError):
                    project_table_html(html)


class DomainPreservationTests(unittest.TestCase):
    """Caption/note/footnote domains keep order, multiplicity, identity."""

    def test_caption_and_footnote_domains_are_ordered_multisets(self) -> None:
        base = project_table_html(
            _BASE,
            extra_captions=("主表标题", "主表标题"),
            extra_footnotes=("注1", "注2"),
        )
        same = project_table_html(
            _BASE,
            extra_captions=("主表标题", "主表标题"),
            extra_footnotes=("注1", "注2"),
        )
        self.assertEqual(base.sha256(), same.sha256())
        for label, captions, footnotes in (
            ("caption_dropped", ("主表标题",), ("注1", "注2")),
            ("caption_reordered", ("主表标题", "主表标题2"), ("注1", "注2")),
            ("footnote_duplicated", ("主表标题", "主表标题"), ("注1", "注1")),
            ("domain_swapped", ("主表标题", "注1"), ("主表标题", "注2")),
        ):
            with self.subTest(label=label):
                variant = project_table_html(
                    _BASE,
                    extra_captions=captions,
                    extra_footnotes=footnotes,
                )
                self.assertNotEqual(variant.sha256(), base.sha256())

    def test_model_body_never_claims_content_domains(self) -> None:
        projected = project_table_html(
            _BASE, extra_captions=("标题",), extra_footnotes=("注",)
        )
        self.assertEqual(projected.body().caption, ())
        self.assertEqual(projected.body().footnotes, ())
        self.assertEqual(projected.body().cells, projected.cells)


class BijectionTests(unittest.TestCase):
    def _table(self, index: int, page: int, html: str, x: float = 100.0):
        return comparable_table(
            index=index,
            page_idx=page,
            bbox=(x, 100.0, x + 500.0, 400.0),
            html=html,
            label=f"content table {index}",
        )

    def test_identical_projections_with_competing_bboxes_block(self) -> None:
        content = [self._table(0, 0, _BASE)]
        models = [
            self._table(0, 0, _BASE, x=100.0),
            self._table(1, 0, _BASE, x=101.0),
        ]
        with self.assertRaisesRegex(TableComparisonError, "2 exact"):
            prove_unique_bijection(content, models)

    def test_cross_page_fragments_stay_two_page_local_tables(self) -> None:
        first = "<table><tr><td>上半</td></tr></table>"
        second = "<table><tr><td>下半</td></tr></table>"
        content = [self._table(0, 0, first), self._table(1, 1, second)]
        models = [self._table(0, 0, first), self._table(1, 1, second)]
        root = prove_unique_bijection(content, models)
        self.assertRegex(root, r"^sha256:[a-f0-9]{64}$")

    def test_missing_model_candidate_fails_closed(self) -> None:
        content = [self._table(0, 0, _BASE)]
        with self.assertRaisesRegex(TableComparisonError, "0 exact"):
            prove_unique_bijection(content, [])


class GenerationCompatibilityTests(unittest.TestCase):
    def _payload(self, diagnostics: dict) -> dict:
        return {
            "elements": [],
            "parser_diagnostics": {"table_reconciliation": diagnostics},
        }

    def _v7(self) -> dict:
        return {
            "algorithm_version": "mineru-page-local-table-closure.v7",
            "comparison_contract": "reader-visible-table-projection.v1",
            "projection_root": "sha256:" + "a" * 64,
            "model_hash": "sha256:" + "b" * 64,
            "content_tables": 0,
            "model_tables": 0,
            "matched_tables": 0,
            "page_local_closed": True,
        }

    def _v6(self) -> dict:
        diagnostics = self._v7()
        diagnostics["algorithm_version"] = "mineru-page-local-table-closure.v6"
        diagnostics.pop("comparison_contract")
        diagnostics.pop("projection_root")
        return diagnostics

    def test_generations_assess_to_their_own_tier(self) -> None:
        current = assess_normalized_ir_table_reconciliation(
            self._payload(self._v7())
        )
        self.assertIs(
            current.compatibility, ReconciliationCompatibility.CURRENT
        )
        legacy = assess_normalized_ir_table_reconciliation(
            self._payload(self._v6())
        )
        self.assertIs(legacy.compatibility, ReconciliationCompatibility.LEGACY)

    def test_mislabeled_shapes_are_invalid(self) -> None:
        v7_label_v6_shape = self._v6()
        v7_label_v6_shape["algorithm_version"] = (
            "mineru-page-local-table-closure.v7"
        )
        v6_label_v7_fields = self._v7()
        v6_label_v7_fields["algorithm_version"] = (
            "mineru-page-local-table-closure.v6"
        )
        missing_contract = self._v7()
        missing_contract.pop("comparison_contract")
        missing_root = self._v7()
        missing_root.pop("projection_root")
        for label, diagnostics in {
            "v7_label_v6_shape": v7_label_v6_shape,
            "v6_label_v7_fields": v6_label_v7_fields,
            "missing_comparison_contract": missing_contract,
            "missing_projection_root": missing_root,
        }.items():
            with self.subTest(label=label):
                with self.assertRaises(TableReconciliationContractError):
                    assess_normalized_ir_table_reconciliation(
                        self._payload(diagnostics)
                    )


class IndependentAuditReplayTests(unittest.TestCase):
    """The audit recomputes from raw bytes; it never copies a verdict."""

    def _environment(self):
        from tests.unit.test_unit_builder import (
            _audit_case_environment,
            _element,
            _heading,
        )

        elements = [
            _element(0, text="一、经营情况", text_level=1),
            _element(
                1,
                kind="table",
                raw_kind="table",
                table_caption=["主表"],
                table_footnote=["注:口径"],
                table_html=_BASE,
                image_path="images/table.png",
                table={
                    "headers": ["项目", "金额"],
                    "rows": [
                        ["营业收入", "1,234.56"],
                        ["附图", ""],
                    ],
                    "cells": [
                        {
                            "row": 0,
                            "col": 0,
                            "rowspan": 1,
                            "colspan": 1,
                            "text": "项目",
                            "is_header": True,
                        },
                        {
                            "row": 0,
                            "col": 1,
                            "rowspan": 1,
                            "colspan": 1,
                            "text": "金额",
                            "is_header": True,
                        },
                        {
                            "row": 1,
                            "col": 0,
                            "rowspan": 1,
                            "colspan": 1,
                            "text": "营业收入",
                            "is_header": False,
                        },
                        {
                            "row": 1,
                            "col": 1,
                            "rowspan": 1,
                            "colspan": 1,
                            "text": "1,234.56",
                            "is_header": False,
                        },
                        {
                            "row": 2,
                            "col": 0,
                            "rowspan": 1,
                            "colspan": 1,
                            "text": "附图",
                            "is_header": False,
                        },
                        {
                            "row": 2,
                            "col": 1,
                            "rowspan": 1,
                            "colspan": 1,
                            "text": "",
                            "is_header": False,
                        },
                    ],
                    "embedded_media": [
                        {
                            "occurrence_index": 0,
                            "cell_media_index": 0,
                            "row": 2,
                            "col": 0,
                            "rowspan": 1,
                            "colspan": 1,
                            "image_path": "images/a.png",
                            "artifact_role": (
                                "evidence_table_media_000001_000000"
                            ),
                        }
                    ],
                },
            ),
        ]
        headings = [_heading(1, 0, text="一、经营情况", section_end=1)]
        environment = _audit_case_environment(
            elements, headings=headings, page_count=1
        )
        normalized_ir = environment[0]
        # The embedded media occurrence must be manifest-bound like every
        # image occurrence; content stays identical across variants.
        normalized_ir["parser_artifacts"]["files"][
            "evidence_table_media_000001_000000"
        ] = {
            "availability": "present",
            "relpath": "parser/audit/images/a.png",
            "sha256": "sha256:" + "9" * 64,
            "size_bytes": 3,
        }
        return environment

    def _audit(self, normalized_ir, source_proof, table_comparison):
        from disclosure_anchor.application.services.document_unit_audit import (
            AuditDocumentMetadata,
            audit_document,
        )

        return audit_document(
            normalized_ir=normalized_ir,
            units=(),
            metadata=AuditDocumentMetadata(
                document_id=str(normalized_ir["document_id"]),
                title=None,
                filing_type="annual_report",
            ),
            source_proof=source_proof,
            image_hashes={},
            table_comparison=table_comparison,
        )

    def test_untampered_comparison_replays_clean(self) -> None:
        normalized_ir, source_proof, _r, _h, comparison = self._environment()
        report = self._audit(normalized_ir, source_proof, comparison)
        codes = {finding.code for finding in report.findings}
        self.assertNotIn("table_comparison_replay_mismatch", codes)
        self.assertNotIn("table_comparison_replay_unavailable", codes)
        self.assertNotIn("table_projection_preservation_mismatch", codes)

    def test_missing_raw_inputs_fail_closed(self) -> None:
        normalized_ir, source_proof, _r, _h, _c = self._environment()
        report = self._audit(normalized_ir, source_proof, None)
        self.assertIn(
            "table_comparison_replay_unavailable",
            {finding.code for finding in report.findings},
        )

    def test_forged_receipt_root_is_caught_by_replay(self) -> None:
        normalized_ir, source_proof, _r, _h, comparison = self._environment()
        forged = json.loads(json.dumps(normalized_ir))
        forged["parser_diagnostics"]["table_reconciliation"][
            "projection_root"
        ] = "sha256:" + "d" * 64
        report = self._audit(forged, source_proof, comparison)
        self.assertIn(
            "table_comparison_replay_mismatch",
            {finding.code for finding in report.findings},
        )

    def test_raw_bytes_tampered_without_manifest_update_fail(self) -> None:
        from disclosure_anchor.application.services.document_unit_audit import (
            TableComparisonInputs,
        )

        normalized_ir, source_proof, _r, _h, comparison = self._environment()
        tampered = TableComparisonInputs(
            model_bytes=comparison.model_bytes + b" ",
            content_list_bytes=comparison.content_list_bytes,
        )
        report = self._audit(normalized_ir, source_proof, tampered)
        self.assertIn(
            "table_comparison_input_hash_mismatch",
            {finding.code for finding in report.findings},
        )

    def test_nir_grid_tampered_with_stale_receipt_fails(self) -> None:
        normalized_ir, source_proof, _r, _h, comparison = self._environment()
        tampered = json.loads(json.dumps(normalized_ir))
        for element in tampered["elements"]:
            if element.get("raw_kind") == "table":
                # Internally consistent grid tamper: cells and rows agree
                # with each other but no longer with the HTML carrier, so
                # only the audit's re-derivation can catch it.
                element["table"]["cells"][2]["text"] = "篡改后的收入"
                element["table"]["rows"][0][0] = "篡改后的收入"
        report = self._audit(tampered, source_proof, comparison)
        self.assertIn(
            "table_projection_preservation_mismatch",
            {finding.code for finding in report.findings},
        )

    def test_caption_domain_tampered_in_nir_fails(self) -> None:
        normalized_ir, source_proof, _r, _h, comparison = self._environment()
        tampered = json.loads(json.dumps(normalized_ir))
        for element in tampered["elements"]:
            if element.get("raw_kind") == "table":
                element["table_caption"] = ["主表", "多出的标题"]
        report = self._audit(tampered, source_proof, comparison)
        self.assertIn(
            "table_projection_preservation_mismatch",
            {finding.code for finding in report.findings},
        )

    def test_divergent_normalization_injection_is_detected(self) -> None:
        # If either side's normalization drifts, the roots stop agreeing:
        # the audit compares recomputed facts, it never copies verdicts.
        normalized_ir, source_proof, _r, _h, comparison = self._environment()
        from disclosure_anchor.application.services import (
            document_unit_audit as audit_module,
        )

        real = replay_page_local_table_comparison

        def drifted(**kwargs):
            replayed = real(**kwargs)
            return type(replayed)(
                model_hash=replayed.model_hash,
                content_tables=replayed.content_tables,
                model_tables=replayed.model_tables,
                projection_root="sha256:" + "e" * 64,
            )

        with mock.patch.object(
            audit_module,
            "replay_page_local_table_comparison",
            side_effect=drifted,
        ):
            report = self._audit(normalized_ir, source_proof, comparison)
        self.assertIn(
            "table_comparison_replay_mismatch",
            {finding.code for finding in report.findings},
        )


if __name__ == "__main__":
    unittest.main()
