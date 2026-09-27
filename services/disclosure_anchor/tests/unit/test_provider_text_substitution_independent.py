"""Independent acceptance: provider_text_nul_substitution.v1, locator v10 and exposure-based quality.

Accepted decision ``nul-content-preservation-design-r1/decision.md`` with the frozen Opus interface (§2-§7):

* an actual U+0000 in provider text is kept as a one-for-one U+FFFD marker in the derived (effective) view only,
  before Unit hashing; the original provider document, raw JSON and raw hashes stay unchanged; the literal six
  characters ``\\u0000`` and every lawful character (TAB/LF/CR/SOH/DEL, U+FFFD/FFFE/FFFF, astral text) stay as they
  are, with no record;
* one distinct, source-bound ``ProviderTextSubstitution`` per affected payload (never a native reconciliation, never a
  guessed glyph); a native finding may coexist on the same payload, a native reconciliation may not;
* the record attaches to every Unit that depends on the block (parts, evidence-only blocks, heading chain,
  continuation fragments); ``needs_review`` is set only where the repaired text is exposed (title, heading path of
  the Unit or of its descendants, part owner payloads); evidence-only records are kept without flagging clean
  output; the native finding scope is unchanged;
* locator v10 is emitted only for Units carrying records, every other Unit keeps exact v9 bytes; v1-v9 decoders
  never accept the v10 vocabulary.

Each crafted fixture's structure (which Unit exposes which block/payload) was verified with a visible marker on the
v23 builder before the NUL version was written. Exposure is proved by exact search bindings and substrings, never by
equality of whole-field hashes: a wrapped heading's repaired fragment is a substring of the title, not the title.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict, fields, replace
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import unittest
from unittest import mock

from disclosure_anchor.application.contracts.provider_document import (
    ProviderArtifact,
    ProviderBBox,
    ProviderDocument,
    ProviderPayload,
)
from disclosure_anchor.application.contracts.provider_document_envelope import (
    provider_document_envelope_from_bytes,
)
from disclosure_anchor.application.contracts.provider_quality import (
    SourceFindingOccurrence,
    TextSubstitutionOccurrence,
    quality_occurrence_from_payload,
    quality_occurrence_to_payload,
)
from disclosure_anchor.application.contracts.provider_source_semantics import (
    NUL_SUBSTITUTION_POLICY,
    ProviderSourceSemantics,
    ProviderTextSubstitution,
    SourceQualityFinding,
    SourceTextReconciliation,
    validate_source_semantics,
)
from disclosure_anchor.application.contracts.provider_unit import (
    PROVIDER_UNIT_BUILDER_VERSION,
    PROVIDER_UNIT_LOCATOR_VERSION,
    SUPPORTED_PROVIDER_UNIT_LOCATOR_VERSIONS,
    TEXT_SUBSTITUTION_PROVIDER_UNIT_LOCATOR_VERSION,
    ProviderUnitDraft,
    ProviderUnitLocator,
    ProviderUnitSourceQualityFinding,
    ProviderUnitSourceTextReconciliation,
    ProviderUnitTextSubstitution,
    provider_unit_locator_from_payload,
    provider_unit_locator_to_payload,
)
from disclosure_anchor.application.services import provider_unit_builder as builder_module
from disclosure_anchor.application.services.provider_quality import assess_source_build_quality
from disclosure_anchor.application.services.provider_source_semantics import derive_source_semantics
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units,
    build_source_provider_units,
)
from tests import _provider_source_semantics_fixture as h1
from tests.unit import test_provider_unit_builder as unit_fx


NUL = "\x00"
MARKER = "\U0000FFFD"
POLICY = "provider_text_nul_substitution.v1"
V9 = "provider_unit_locator.v9"
V10 = "provider_unit_locator.v10"
OLD_LOCATOR_VERSIONS = tuple(f"provider_unit_locator.v{index}" for index in range(1, 10))
RECORD_FIELDS = frozenset({
    "source_index", "payload_ordinal", "raw_block_sha256", "provider_text_sha256",
    "substituted_text_sha256", "occurrence_count", "policy",
})
P = ProviderPayload


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def repaired(text: str) -> str:
    return text.replace(NUL, MARKER)


def document(pages: tuple[tuple[Any, ...], ...], *, segments: tuple[Any, ...] = (),
             extra_artifacts: tuple[ProviderArtifact, ...] = ()) -> ProviderDocument:
    built = unit_fx._document(pages=pages, segments=segments, extra_artifacts=extra_artifacts)
    return replace(built, source_pdf_sha256=h1.sha(h1.SOURCE_BYTES))


def heading(index: int, page: int, text: str, level: int, *, bbox: ProviderBBox | None = None) -> Any:
    return unit_fx._block(index, page, "text", (P("text", None, text),), annotation="title", level=level, bbox=bbox)


def paragraph(index: int, page: int, text: str, *, bbox: ProviderBBox | None = None) -> Any:
    return unit_fx._block(index, page, "text", (P("text", None, text),), annotation="paragraph", bbox=bbox)


def page_number(index: int, page: int, text: str) -> Any:
    return unit_fx._block(index, page, "page_number", (P("text", None, text),), annotation="page_number")


def header(index: int, page: int, text: str) -> Any:
    return unit_fx._block(index, page, "header", (P("text", None, text),), annotation="page_header",
                          bbox=ProviderBBox(159, 52, 608, 84))


def admitted_build(doc: ProviderDocument, observations: tuple[Any, ...] = ()) -> tuple[Any, Any]:
    """The production admission (derives every source semantic) and the normal builder."""

    admitted = h1.admit(doc, observations)
    return admitted, build_provider_units(admitted)


def source_build(doc: ProviderDocument, observations: tuple[Any, ...] = ()) -> tuple[ProviderSourceSemantics, Any]:
    semantics = derive_source_semantics(document=doc, observations=observations)
    build = build_source_provider_units(
        semantics, semantic_record_sha256=sha("independent source semantic record"), target_identity=h1.target(),
    )
    return semantics, build


def strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings(item)


def exposed_strings(draft: ProviderUnitDraft) -> tuple[str, ...]:
    return (*(() if draft.title is None else (draft.title,)), *draft.heading_path, *strings(draft.payload))


def record_ids(draft: ProviderUnitDraft) -> tuple[tuple[int, int], ...]:
    return tuple((item.source_index, item.payload_ordinal) for item in draft.locator.text_substitutions)


def dependency_sources(draft: ProviderUnitDraft) -> set[int]:
    locator = draft.locator
    return {
        *(source for part in locator.parts for source in part.block_source_indices),
        *locator.evidence_only_block_source_indices,
        *(item.source_index for item in locator.heading_chain),
        *(fragment.source_index for item in locator.heading_chain for fragment in item.continuation_fragments),
    }


def exposed_ids(draft: ProviderUnitDraft, doc: ProviderDocument) -> set[tuple[int, int]]:
    """Opus §2 ``Exposed(unit)``, recomputed by this author from the locator and the document."""

    locator = draft.locator
    found = {(item.source_index, item.payload_ordinal) for item in locator.heading_chain}
    found |= {
        (fragment.source_index, fragment.payload_ordinal)
        for item in locator.heading_chain for fragment in item.continuation_fragments
    }
    for part in locator.parts:
        owner = part.block_source_indices[0]
        found |= {(owner, ordinal) for ordinal in range(len(doc.blocks[owner].payloads))}
    return found


def flagged_ids(semantics: ProviderSourceSemantics, build: Any) -> dict[int, set[tuple[int, int]]]:
    flagged: dict[int, set[tuple[int, int]]] = {unit.unit_index: set() for unit in build.units}
    for occurrence in assess_source_build_quality(semantics, build):
        if isinstance(occurrence, TextSubstitutionOccurrence):
            flagged[occurrence.unit_index].add(
                (occurrence.substitution.source_index, occurrence.substitution.payload_ordinal))
    return flagged


def destination_value(draft: ProviderUnitDraft, binding: Any) -> str:
    """This author's reading of one binding destination (title, title fragment or payload field)."""

    destination = binding.destination
    if destination.kind in {"unit_title", "unit_title_fragment"}:
        assert draft.title is not None
        return draft.title
    container: Any = draft.payload
    if destination.kind == "mixed_part":
        container = draft.payload["parts"][destination.part_index]  # type: ignore[index]
    value = container[destination.field]
    return value if destination.item_index is None else value[destination.item_index]


# -- fixtures ---------------------------------------------------------------------------------


def all_fields_document() -> ProviderDocument:
    """Actual U+0000 in every field the builder exposes, plus an evidence-only page number.

    The v23 builder exposed each of these fields (checked with a visible marker per field); the page number is
    always evidence-only.
    """

    artifact = ProviderArtifact(role="image_0001", relative_path="e_images/figure.jpg",
                                sha256="sha256:" + "e" * 64, size_bytes=12, media_type="image/jpeg")
    block = unit_fx._block
    blocks = (
        block(0, 0, "text", (P("text", None, "第一节 标\x00题"),), annotation="title", level=1),
        block(1, 0, "text", (P("text", None, "甲\x00乙\x00\x00丙"),), annotation="paragraph"),
        block(2, 0, "table", (P("table_body", None, "<table><tr><td>项\x00目</td><td>12</td></tr></table>"),
                              P("table_caption", 0, "表\x00题"), P("table_footnote", 0, "表\x00注")), annotation="table"),
        block(3, 0, "image", (P("content", None, "图\x00文"), P("image_caption", 0, "图\x00题"),
                              P("image_footnote", 0, "图\x00注")), annotation="image", artifact_roles=("image_0001",)),
        block(4, 0, "code", (P("code_body", None, "x = '\x00'"), P("code_caption", 0, "代\x00码")), annotation="code"),
        block(5, 0, "list", (P("list_items", 0, "甲\x00项"), P("list_items", 1, "\x00")), annotation="list"),
        block(6, 0, "equation", (P("text", None, "E=mc\x00"),), annotation="equation"),
        block(7, 0, "chart", (P("content", None, "图表\x00"), P("chart_caption", 0, "图表\x00题"),
                              P("chart_footnote", 0, "图表\x00注")), annotation="chart"),
        block(8, 0, "aside_text", (P("text", None, "旁\x00注"),), annotation="aside_text"),
        block(9, 0, "ref_text", (P("text", None, "参\x00考"),), annotation="ref_text"),
        block(10, 0, "page_footnote", (P("text", None, "脚\x00注"),), annotation="page_footnote"),
        block(11, 0, "phonetic", (P("text", None, "注\x00音"),), annotation="phonetic"),
        page_number(12, 0, "\x00页码"),
    )
    return document((blocks,), segments=(unit_fx._segment(0, 0, "retained"),), extra_artifacts=(artifact,))


def nested_headings_document() -> ProviderDocument:
    # The NUL follows the numeral, so the child still nests (a marker in the numeral position flattens it).
    return document(((heading(0, 0, "第一节 重要\x00事项", 1), heading(1, 0, "一、概述", 2),
                      paragraph(2, 0, "本节正文。"), heading(3, 0, "第二节 其他事项", 1),
                      paragraph(4, 0, "其他正文。")),))


WRAPPED_FRAGMENT = "券投资者\x00权益的影响"


def wrapped_heading_document() -> ProviderDocument:
    return document(((
        heading(0, 0, "第三节 管理层讨论与分析及对债", 1, bbox=ProviderBBox(100, 100, 900, 119)),
        paragraph(1, 0, WRAPPED_FRAGMENT, bbox=ProviderBBox(150, 127, 350, 145)),
        heading(2, 0, "一、概述", 2),
        paragraph(3, 0, "本节正文。"),
    ),))


# -- F1: the substitution policy --------------------------------------------------------------


class SubstitutionPolicyTests(unittest.TestCase):
    def test_every_consumed_field_is_substituted_one_for_one_and_the_original_stays_untouched(self) -> None:
        doc = all_fields_document()
        semantics = derive_source_semantics(document=doc, observations=())
        expected = [(item.source_index, ordinal) for item in doc.blocks
                    for ordinal, payload in enumerate(item.payloads) if NUL in payload.text]
        self.assertEqual(len(expected), 21)
        records = semantics.text_substitutions
        self.assertEqual([(item.source_index, item.payload_ordinal) for item in records], expected)
        self.assertEqual((semantics.source_text_reconciliations, semantics.source_quality_findings), ((), ()))
        for record in records:
            with self.subTest(record=(record.source_index, record.payload_ordinal)):
                original_block = doc.blocks[record.source_index]
                original = original_block.payloads[record.payload_ordinal].text
                self.assertIs(type(record), ProviderTextSubstitution)
                self.assertEqual(record.raw_block_sha256, original_block.raw_item_sha256)
                self.assertEqual(record.provider_text_sha256, sha(original))
                self.assertEqual(record.substituted_text, repaired(original))
                self.assertEqual(record.substituted_text_sha256, sha(repaired(original)))
                self.assertEqual(record.occurrence_count, original.count(NUL))
                self.assertEqual(record.policy, POLICY)
                self.assertEqual(len(record.substituted_text), len(original))
                self.assertEqual(record.substituted_text.count(MARKER), original.count(NUL) + original.count(MARKER))
        self.assertEqual(NUL_SUBSTITUTION_POLICY, POLICY)
        self.assertEqual([record.occurrence_count for record in records if record.source_index == 1], [3])
        # Only the substituted payload texts change in the effective view; raw evidence is identical.
        effective = semantics.effective_provider_document
        self.assertEqual(semantics.provider_document, doc)
        for original_block, effective_block in zip(doc.blocks, effective.blocks, strict=True):
            self.assertEqual((effective_block.raw_item_json, effective_block.raw_item_sha256),
                             (original_block.raw_item_json, original_block.raw_item_sha256))
            self.assertEqual([payload.text for payload in effective_block.payloads],
                             [repaired(payload.text) for payload in original_block.payloads])
            self.assertEqual([(p.field, p.item_index) for p in effective_block.payloads],
                             [(p.field, p.item_index) for p in original_block.payloads])
        self.assertTrue(any(NUL in payload.text for item in doc.blocks for payload in item.payloads))
        # The production admission carries the same records and never rewrites its original document.
        admitted = h1.admit(doc)
        self.assertEqual(admitted.text_substitutions, records)
        self.assertEqual(admitted.provider_document, doc)
        self.assertEqual(admitted.effective_provider_document, effective)

    def test_lawful_unicode_controls_and_literal_escapes_produce_no_record(self) -> None:
        lawful = (
            "第一节\t重要事项", "第一节\n重要事项", "第一节\r\n重要事项", "第\x01节 重要事项", "第\x7f节 重要事项",
            "第\U0000FFFD节 重要事项", "第\U0000FFFE节 重要事项", "第\U0000FFFF节 重要事项",
            "第\U00020000节 重要事项", "第\U0001F600节 重要事项", "第\\u0000节 重要事项", '{"k":"\\u0000","n":null}',
        )
        doc = document((tuple(paragraph(index, 0, text) for index, text in enumerate(lawful)),))
        semantics = derive_source_semantics(document=doc, observations=())
        self.assertEqual(semantics.text_substitutions, ())
        self.assertEqual(semantics.effective_provider_document, doc)
        _admitted, build = admitted_build(doc)
        for draft in build.units:
            self.assertEqual((draft.locator.contract_version, draft.locator.text_substitutions), (V9, ()))
        # Every lawful value is exposed exactly as provided (a numbered one may be promoted to a heading).
        self.assertLessEqual(set(lawful), {text for draft in build.units for text in exposed_strings(draft)})

    def test_a_native_finding_and_a_substitution_coexist_on_one_payload(self) -> None:
        rows = "".join(f"<tr><td>{number}</td></tr>" for number in range(1, 9))
        html = f"<table><tr><td>项\x00目</td></tr>{rows}</table>"
        table = h1.block(0, 0, 0, html, kind="table")
        doc = h1.document(((table,),), (h1.segment(0, 0, f"<table>{rows}</table>", "retained"),))
        observations = (h1.observation(table, "项目 1 2 3 4 5 6 7 9"),)
        semantics = derive_source_semantics(document=doc, observations=observations)
        finding, = semantics.source_quality_findings
        record, = semantics.text_substitutions
        self.assertEqual((finding.source_index, finding.payload_ordinal, finding.reason),
                         (0, 0, "numeric_token_mismatch"))
        # The native finding is computed on the original provider text, not on the repaired view.
        self.assertEqual(finding.provider_text_sha256, sha(html))
        self.assertEqual((record.source_index, record.payload_ordinal, record.provider_text_sha256),
                         (0, 0, sha(html)))
        _admitted, build = admitted_build(doc, observations)
        draft, = build.units
        self.assertEqual([(f.source_index, f.payload_ordinal) for f in draft.locator.source_quality_findings], [(0, 0)])
        self.assertEqual(record_ids(draft), ((0, 0),))
        self.assertEqual(draft.locator.contract_version, V10)
        self.assertEqual(draft.quality_status, "needs_review")
        _semantics, source = source_build(doc, observations)
        kinds = {type(item) for item in assess_source_build_quality(semantics, source)}
        self.assertLessEqual({SourceFindingOccurrence, TextSubstitutionOccurrence}, kinds)

    def test_native_reconciliation_keeps_precedence_and_can_never_overlap_a_substitution(self) -> None:
        repaired_block = h1.block(0, 0, 0, "营业收入万元。")
        nul_text = "第一节 重要\x00事项"
        nul_block = h1.block(1, 0, 1, nul_text)
        doc = h1.document(((repaired_block, nul_block),))
        semantics = derive_source_semantics(document=doc, observations=(
            h1.observation(repaired_block, "营业收入12万元。"),))
        self.assertEqual([(r.source_index, r.payload_ordinal) for r in semantics.source_text_reconciliations], [(0, 0)])
        self.assertEqual([(s.source_index, s.payload_ordinal) for s in semantics.text_substitutions], [(1, 0)])
        self.assertEqual([item.payloads[0].text for item in semantics.effective_provider_document.blocks],
                         ["营业收入12万元。", repaired(nul_text)])
        forged = SourceTextReconciliation(1, 0, nul_block.raw_item_sha256, sha(nul_text), sha("第一节 重要事项"),
                                          "第一节 重要事项")
        with self.assertRaises(ValueError):
            ProviderSourceSemantics(doc, (semantics.source_text_reconciliations[0], forged), (),
                                    semantics.text_substitutions)
        with self.assertRaises(ValueError):
            validate_source_semantics(doc, (forged,), (), semantics.text_substitutions)
        # A finding may coexist with a substitution; a finding may still not overlap a reconciliation.
        coexisting = SourceQualityFinding(1, 0, nul_block.raw_item_sha256, sha(nul_text), sha("第一节 重要事项"),
                                          "native_text_omission", "source_pdf_native_text_quality.v1")
        ProviderSourceSemantics(doc, semantics.source_text_reconciliations, (coexisting,), semantics.text_substitutions)
        with self.assertRaises(ValueError):
            ProviderSourceSemantics(doc, (forged,), (coexisting,), ())
        # The same exclusion holds inside one locator.
        single = h1.block(0, 0, 0, nul_text)
        _admitted, build = admitted_build(h1.document(((single,),)))
        draft, = build.units
        self.assertEqual(record_ids(draft), ((0, 0),))
        overlap = ProviderUnitSourceTextReconciliation(0, 0, single.raw_item_sha256, sha(nul_text),
                                                       sha("第一节 重要事项"), "source_pdf_native_numeric.v1")
        with self.assertRaises(ValueError):
            replace(draft.locator, source_text_reconciliations=(overlap,))

    def test_forged_unbound_or_glyph_guessing_records_are_refused(self) -> None:
        text = "第\x00节 重要事项"
        doc = h1.document(((h1.block(0, 0, 0, text), h1.block(1, 0, 1, "完整正文。")),))
        record, = derive_source_semantics(document=doc, observations=()).text_substitutions

        def with_text(value: str) -> ProviderTextSubstitution:
            return replace(record, substituted_text=value, substituted_text_sha256=sha(value))

        forgeries = {
            "guessed_numeral": lambda: with_text("第1节 重要事项"),
            "deleted_nul": lambda: with_text("第节 重要事项"),
            "question_mark": lambda: with_text("第?节 重要事项"),
            "extra_non_nul_change": lambda: with_text("第\U0000FFFD章 重要事项"),
            "kept_nul": lambda: with_text(text),
            "text_hash_not_true": lambda: replace(record, substituted_text_sha256=sha("other")),
            "wrong_count": lambda: replace(record, occurrence_count=2),
            "zero_count": lambda: replace(record, occurrence_count=0),
            "wrong_provider_hash": lambda: replace(record, provider_text_sha256=sha("other text")),
            "wrong_raw_hash": lambda: replace(record, raw_block_sha256=sha("other raw")),
            "wrong_ordinal": lambda: replace(record, payload_ordinal=1),
            "out_of_range_block": lambda: replace(record, source_index=9),
            "nul_free_payload": lambda: replace(record, source_index=1, raw_block_sha256=doc.blocks[1].raw_item_sha256,
                                                provider_text_sha256=sha("完整正文。"),
                                                substituted_text="完整正文。", substituted_text_sha256=sha("完整正文。")),
            "wrong_policy": lambda: replace(record, policy="provider_text_nul_substitution.v2"),
            "negative_index": lambda: replace(record, source_index=-1),
        }
        for label, forge in forgeries.items():
            with self.subTest(forgery=label), self.assertRaises(ValueError):
                ProviderSourceSemantics(doc, (), (), (forge(),))
        second_doc = h1.document(((h1.block(0, 0, 0, text), h1.block(1, 0, 1, "乙\x00")),))
        first, second = derive_source_semantics(document=second_doc, observations=()).text_substitutions
        for label, records in (("duplicate", (first, first)), ("unordered", (second, first))):
            with self.subTest(order=label), self.assertRaises(ValueError):
                ProviderSourceSemantics(second_doc, (), (), records)

    def test_v1_source_record_keeps_exact_raw_facts_and_rederives_substitutions_on_decode(self) -> None:
        from disclosure_anchor.application.services.source_semantic_record import (
            decode_source_semantic_record, encode_source_semantic_record,
        )
        from tests._source_semantic_record_fixture import canonical, encoder_arguments, literal_record

        record = literal_record()
        record["native_observations"] = []
        record["source_text_reconciliations"] = []
        record["source_quality_findings"] = []
        block = record["provider_document"]["pages"][0]["blocks"][0]
        text = "金额\x00元。"
        raw = json.loads(block["raw_item_json"])
        raw["text"] = text
        block["raw_item_json"] = canonical(raw).decode()
        block["raw_item_sha256"] = sha(block["raw_item_json"])
        block["payloads"][0]["text"] = text
        exact = canonical(record)
        self.assertEqual(record["contract_version"], "m6.source-semantic-record.v1")
        self.assertNotIn("text_substitutions", record)
        self.assertEqual(encode_source_semantic_record(**encoder_arguments(record), maximum_bytes=len(exact)), exact)
        decoded = decode_source_semantic_record(exact, maximum_bytes=len(exact))
        substitution, = decoded.semantics.text_substitutions
        self.assertEqual(substitution.substituted_text, "金额\ufffd元。")
        self.assertEqual(substitution.provider_text_sha256, sha(text))
        self.assertEqual(decoded.provider_document.blocks[0].payloads[0].text, text)
        self.assertEqual(decoded.record_sha256, "sha256:" + hashlib.sha256(exact).hexdigest())

    def test_a_substitution_is_a_storage_marker_not_a_native_repair(self) -> None:
        self.assertFalse(issubclass(ProviderTextSubstitution, SourceTextReconciliation))
        self.assertEqual({item.name for item in fields(ProviderUnitTextSubstitution)}, RECORD_FIELDS)
        semantics = derive_source_semantics(document=h1.document(((h1.block(0, 0, 0, "第\x00节"),),)), observations=())
        record, = semantics.text_substitutions
        self.assertEqual(semantics.source_text_reconciliations, ())
        self.assertFalse(hasattr(record, "source_kind"))
        self.assertEqual(record.substituted_text, "第\U0000FFFD节")


# -- F2: locator v10 ---------------------------------------------------------------------------


def _minimal_locator(version: str) -> ProviderUnitLocator:
    return ProviderUnitLocator(
        provider_document_sha256=sha("minimal locator"), unit_index=0, heading_chain=(), parts=(),
        evidence_only_block_source_indices=(), unbound_table_parts=(), evidence_artifacts=(), search_targets=(),
        contract_version=version,
    )


class LocatorV10Tests(unittest.TestCase):
    def setUp(self) -> None:
        # One headed Unit whose title and body both carry a NUL: two records in one v10 locator.
        doc = document(((heading(0, 0, "第一节 重要\x00事项", 1), paragraph(1, 0, "正文\x00内容"),
                         heading(2, 0, "第二节 其他事项", 1), paragraph(3, 0, "其他正文。")),))
        _admitted, build = admitted_build(doc)
        self.v10, self.v9 = build.units

    def test_v10_is_emitted_iff_records_exist_and_every_other_unit_keeps_v9(self) -> None:
        self.assertEqual((PROVIDER_UNIT_LOCATOR_VERSION, TEXT_SUBSTITUTION_PROVIDER_UNIT_LOCATOR_VERSION), (V9, V10))
        self.assertIn(V10, SUPPORTED_PROVIDER_UNIT_LOCATOR_VERSIONS)
        self.assertEqual(PROVIDER_UNIT_BUILDER_VERSION, "provider_unit.v24")
        self.assertEqual((self.v10.locator.contract_version, record_ids(self.v10)), (V10, ((0, 0), (1, 0))))
        self.assertEqual((self.v9.locator.contract_version, self.v9.locator.text_substitutions), (V9, ()))
        self.assertNotIn("text_substitutions", provider_unit_locator_to_payload(self.v9.locator))
        with self.assertRaises(ValueError):
            replace(self.v10.locator, contract_version=V9)
        with self.assertRaises(ValueError):
            replace(self.v10.locator, text_substitutions=())
        record = self.v10.locator.text_substitutions[0]
        for version in OLD_LOCATOR_VERSIONS:
            with self.subTest(version=version), self.assertRaises(ValueError):
                replace(_minimal_locator(version), text_substitutions=(record,))

    def test_v1_to_v9_decoders_refuse_the_v10_vocabulary(self) -> None:
        record = asdict(self.v10.locator.text_substitutions[0])
        payloads = {version: provider_unit_locator_to_payload(_minimal_locator(version))
                    for version in OLD_LOCATOR_VERSIONS}
        payloads[V9 + " built"] = provider_unit_locator_to_payload(self.v9.locator)
        for label, payload in payloads.items():
            provider_unit_locator_from_payload(payload)  # the untouched payload decodes
            for extra in ([], [record]):
                with self.subTest(version=label, records=len(extra)), self.assertRaises(ValueError):
                    provider_unit_locator_from_payload({**payload, "text_substitutions": extra})
        v10_payload = provider_unit_locator_to_payload(self.v10.locator)
        with self.assertRaises(ValueError):
            provider_unit_locator_from_payload({key: value for key, value in v10_payload.items()
                                                if key != "text_substitutions"})

    def test_v10_round_trips_and_refuses_tampered_records(self) -> None:
        payload = provider_unit_locator_to_payload(self.v10.locator)
        self.assertEqual(provider_unit_locator_from_payload(payload), self.v10.locator)
        records = payload["text_substitutions"]
        self.assertEqual([set(item) for item in records], [set(RECORD_FIELDS)] * 2)  # type: ignore[union-attr]
        self.assertNotIn(MARKER, json.dumps(payload, ensure_ascii=False), "a locator record never carries text")

        def tampered(change: Any) -> dict[str, Any]:
            copy = json.loads(json.dumps(payload))
            change(copy)
            return copy

        cases = {
            "extra_field": lambda p: p["text_substitutions"][0].update(substituted_text="x"),
            "missing_field": lambda p: p["text_substitutions"][0].pop("policy"),
            "hash_not_canonical": lambda p: p["text_substitutions"][0].update(provider_text_sha256="sha256:XYZ"),
            "count_bool": lambda p: p["text_substitutions"][0].update(occurrence_count=True),
            "count_text": lambda p: p["text_substitutions"][0].update(occurrence_count="1"),
            "count_zero": lambda p: p["text_substitutions"][0].update(occurrence_count=0),
            "wrong_policy": lambda p: p["text_substitutions"][0].update(policy="provider_text_nul_substitution.v2"),
            "negative_index": lambda p: p["text_substitutions"][0].update(source_index=-1),
            "unordered": lambda p: p["text_substitutions"].reverse(),
            "duplicate": lambda p: p["text_substitutions"].__setitem__(1, p["text_substitutions"][0]),
            "empty": lambda p: p.update(text_substitutions=[]),
            "reconciliation_overlap": lambda p: p["source_text_reconciliations"].append({
                "payload_ordinal": 0, "provider_text_sha256": sha("第一节 重要\x00事项"),
                "raw_block_sha256": p["text_substitutions"][0]["raw_block_sha256"],
                "source_index": 0, "source_kind": "source_pdf_native_numeric.v1",
                "source_text_sha256": sha("第一节 重要事项")}),
        }
        for label, change in cases.items():
            with self.subTest(tamper=label), self.assertRaises(ValueError):
                provider_unit_locator_from_payload(tampered(change))

    def test_v10_keeps_the_v9_vocabulary(self) -> None:
        finding = ProviderUnitSourceQualityFinding(
            5, 0, sha("raw"), sha("provider"), sha("native"), "numeric_token_truncation",
            "source_pdf_native_text_quality.v3")
        locator = replace(self.v10.locator, source_quality_findings=(finding,))
        self.assertEqual(provider_unit_locator_from_payload(provider_unit_locator_to_payload(locator)), locator)
        _admitted, build = admitted_build(wrapped_heading_document())
        wrapped = build.units[0].locator
        self.assertEqual(wrapped.contract_version, V10)
        self.assertEqual([b.destination.kind for b in wrapped.search_targets[:2]],
                         ["unit_title_fragment", "unit_title_fragment"])
        self.assertEqual(provider_unit_locator_from_payload(provider_unit_locator_to_payload(wrapped)), wrapped)


# -- F3: dependency attachment, exposure and quality ------------------------------------------


class ExposureAndQualityTests(unittest.TestCase):
    def _assert_rule(self, doc: ProviderDocument, observations: tuple[Any, ...] = ()) -> tuple[Any, Any, Any]:
        """Attachment equals dependency; flags equal the exposed subset; no NUL leaks; every record bound."""

        admitted, build = admitted_build(doc, observations)
        semantics, source = source_build(doc, observations)
        records = {(item.source_index, item.payload_ordinal): item for item in semantics.text_substitutions}
        self.assertEqual(tuple(admitted.text_substitutions), tuple(semantics.text_substitutions))
        flagged = flagged_ids(semantics, source)
        bound: set[tuple[int, int]] = set()
        for draft, source_draft in zip(build.units, source.units, strict=True):
            with self.subTest(unit=draft.unit_index):
                expected = tuple(key for key in records if key[0] in dependency_sources(draft))
                self.assertEqual(record_ids(draft), expected)
                self.assertEqual(record_ids(source_draft), expected)
                self.assertEqual(draft.locator.contract_version, V10 if expected else V9)
                for attached in draft.locator.text_substitutions:
                    source_record = records[(attached.source_index, attached.payload_ordinal)]
                    self.assertEqual({name: getattr(attached, name) for name in RECORD_FIELDS},
                                     {name: getattr(source_record, name) for name in RECORD_FIELDS})
                exposed = set(expected) & exposed_ids(draft, doc)
                self.assertEqual(flagged[draft.unit_index], exposed)
                if exposed:
                    self.assertEqual(draft.quality_status, "needs_review")
                for text in exposed_strings(draft):
                    self.assertNotIn(NUL, text)
                for key in set(expected) - exposed:
                    unexposed = records[key].substituted_text
                    self.assertFalse(any(unexposed in text for text in exposed_strings(draft)))
                for binding in draft.locator.search_targets:
                    key = (binding.source.source_index, binding.source.payload_ordinal)
                    if key in records:
                        value = destination_value(draft, binding)
                        if binding.destination.kind == "unit_title_fragment":
                            self.assertIn(records[key].substituted_text, value)
                        else:
                            self.assertEqual(value, records[key].substituted_text)
                bound.update(expected)
        self.assertEqual(bound, set(records), "every substitution must be bound by a dependent Unit")
        return admitted, build, flagged

    def test_a_leaf_heading_substitution_is_exposed_in_title_and_path_with_one_record(self) -> None:
        doc = document(((heading(0, 0, "第一节 重要\x00事项", 1), paragraph(1, 0, "本节正文。"),
                         heading(2, 0, "第二节 其他事项", 1), paragraph(3, 0, "其他正文。")),))
        _admitted, build, flagged = self._assert_rule(doc)
        first, second = build.units
        self.assertEqual((first.title, first.heading_path), ("第一节 重要\U0000FFFD事项", ("第一节 重要\U0000FFFD事项",)))
        self.assertEqual(flagged, {0: {(0, 0)}, 1: set()})
        self.assertEqual((second.quality_status, second.locator.contract_version), ("ok", V9))

    def test_inherited_headings_and_continuation_fragments_flag_every_affected_descendant(self) -> None:
        _admitted, build, flagged = self._assert_rule(nested_headings_document())
        ancestor, child, sibling = build.units
        self.assertEqual(child.heading_path, ("第一节 重要\U0000FFFD事项", "一、概述"))
        self.assertEqual(child.title, "一、概述")
        self.assertEqual(flagged, {0: {(0, 0)}, 1: {(0, 0)}, 2: set()})
        self.assertEqual((sibling.quality_status, record_ids(sibling)), ("ok", ()))
        _admitted, build, flagged = self._assert_rule(wrapped_heading_document())
        wrapped, descendant = build.units
        fragment = repaired(WRAPPED_FRAGMENT)
        self.assertEqual(wrapped.title, "第三节 管理层讨论与分析及对债" + fragment)
        self.assertTrue(wrapped.title.endswith(fragment))
        # The record binds the fragment, not the whole title: a field-hash rule would miss this exposure.
        record, = wrapped.locator.text_substitutions
        self.assertEqual(record.substituted_text_sha256, sha(fragment))
        self.assertNotEqual(sha(wrapped.title), record.substituted_text_sha256)
        self.assertEqual([(f.source_index, f.payload_ordinal)
                          for f in wrapped.locator.heading_chain[-1].continuation_fragments], [(1, 0)])
        self.assertEqual(descendant.heading_path[0], wrapped.title)
        self.assertEqual(flagged, {0: {(1, 0)}, 1: {(1, 0)}})

    def test_evidence_only_substitutions_are_recorded_without_flagging_clean_output(self) -> None:
        _admitted, build, flagged = self._assert_rule(document((
            (heading(0, 0, "一、经营情况", 1), paragraph(1, 0, "本期经营稳定。"), page_number(2, 0, "- 1\x00 -")),)))
        draft, = build.units
        self.assertEqual(draft.locator.evidence_only_block_source_indices, (2,))
        self.assertEqual((record_ids(draft), flagged, draft.quality_status), (((2, 0),), {0: set()}, "ok"))
        self.assertEqual(draft.locator.contract_version, V10)
        furniture = "某某公司\x00年度报告"
        _admitted, build, flagged = self._assert_rule(document((
            (header(0, 0, furniture), heading(1, 0, "一、经营情况", 1)),
            (header(2, 1, furniture), paragraph(3, 1, "本期经营稳定。")),
            (header(4, 2, furniture), paragraph(5, 2, "后续说明。")),
        )))
        self.assertEqual({source for unit in build.units for source in unit.locator.evidence_only_block_source_indices},
                         {0, 2, 4})
        self.assertEqual({key for unit in build.units for key in record_ids(unit)}, {(0, 0), (2, 0), (4, 0)})
        self.assertTrue(all(not keys for keys in flagged.values()))
        self.assertTrue(all(unit.quality_status == "ok" for unit in build.units))

    def test_native_finding_scope_is_unchanged_while_substitution_follows_exposure(self) -> None:
        bracket = heading(0, 0, "第一节 公告2026〕7号", 1)
        doc = document(((bracket, heading(1, 0, "一、概述", 2), paragraph(2, 0, "本节正文。"),
                         heading(3, 0, "第二节 重要\x00事项", 1), heading(4, 0, "一、说明", 2),
                         paragraph(5, 0, "说明正文。")),))
        observations = (h1.observation(bracket, "第一节 公告〔2026〕7号"),)
        _admitted, build, flagged = self._assert_rule(doc, observations)
        finding_heading, finding_child, nul_heading, nul_child = build.units
        self.assertEqual([f.source_index for f in finding_heading.locator.source_quality_findings], [0])
        self.assertEqual(finding_child.locator.source_quality_findings, ())
        self.assertEqual((finding_heading.quality_status, finding_child.quality_status), ("needs_review", "ok"))
        self.assertEqual(finding_child.heading_path[0], "第一节 公告2026〕7号")
        self.assertEqual(flagged, {0: set(), 1: set(), 2: {(3, 0)}, 3: {(3, 0)}})
        self.assertEqual((nul_heading.quality_status, nul_child.quality_status), ("needs_review", "needs_review"))

    def test_every_consumed_field_leaks_no_nul_and_every_record_is_bound(self) -> None:
        doc = all_fields_document()
        _admitted, build, flagged = self._assert_rule(doc)
        # 19 payload fields plus the heading's title and heading_path entry carry the marker; the page number
        # (block 12) is recorded but never exposed.
        self.assertEqual(sum(1 for unit in build.units for text in exposed_strings(unit) if MARKER in text), 21)
        self.assertEqual(set().union(*flagged.values()), {
            key for key in ((b.source_index, o) for b in doc.blocks for o in range(len(b.payloads)))
            if key[0] != 12})

    def test_quality_occurrence_codec_is_closed_and_round_trips(self) -> None:
        semantics, build = source_build(nested_headings_document())
        occurrences = [item for item in assess_source_build_quality(semantics, build)
                       if isinstance(item, TextSubstitutionOccurrence)]
        self.assertEqual([item.unit_index for item in occurrences], [0, 1])
        for occurrence in occurrences:
            payload = quality_occurrence_to_payload(occurrence)
            self.assertEqual((payload["kind"], payload["reason_id"]),
                             ("text_substitution", "text_substitution:" + POLICY))
            self.assertEqual(quality_occurrence_from_payload(json.loads(json.dumps(payload))), occurrence)

    def test_the_build_closure_refuses_a_missing_or_extra_attachment(self) -> None:
        doc = nested_headings_document()
        admitted = h1.admit(doc)
        real = builder_module.ProviderUnitLocator
        for label in ("missing", "extra"):
            calls = {"changed": 0}

            def corrupt(*args: Any, _label: str = label, **kwargs: Any) -> ProviderUnitLocator:
                records = tuple(kwargs.get("text_substitutions", ()))
                if _label == "missing" and records:
                    calls["changed"] += 1
                    kwargs["text_substitutions"] = records[:-1]
                elif _label == "extra" and not records and admitted.text_substitutions:
                    calls["changed"] += 1
                    source = admitted.text_substitutions[0]
                    kwargs["text_substitutions"] = (ProviderUnitTextSubstitution(**{
                        name: getattr(source, name) for name in RECORD_FIELDS}),)
                if kwargs.get("text_substitutions"):
                    kwargs["contract_version"] = V10
                else:
                    kwargs.pop("contract_version", None)
                return real(*args, **kwargs)

            with self.subTest(corruption=label), mock.patch.object(builder_module, "ProviderUnitLocator", corrupt):
                with self.assertRaises(ValueError):
                    build_provider_units(admitted)
                self.assertGreater(calls["changed"], 0, "the corruption must actually reach a locator")

    def test_nul_free_documents_keep_exact_v9_locators_without_the_v10_vocabulary(self) -> None:
        for name, (doc, observations) in h1.cases().items():
            with self.subTest(case=name):
                admitted = h1.admit(doc, observations)
                self.assertEqual(admitted.text_substitutions, ())
                for draft in build_provider_units(admitted).units:
                    self.assertEqual((draft.locator.contract_version, draft.locator.text_substitutions), (V9, ()))
                    self.assertNotIn("text_substitutions", provider_unit_locator_to_payload(draft.locator))


# -- F4/F5: opt-in replay of retained real sources (root runs; nothing enters Git) -------------


REPLAY_DIR_ENV = "NUL_RECOVERY_REPLAY_DIR"
REPLAY_DATA_ROOT_ENV = "NUL_RECOVERY_REPLAY_DATA_ROOT"


class RetainedSourceReplayTests(unittest.TestCase):
    """Source-only v24 builds against root's captured v23 baselines (same boundary, no routing).

    ``NUL_RECOVERY_REPLAY_DIR`` names root's design directory holding ``root-replay-inputs.json``,
    ``root-baseline-v23.json``, ``root-replay-bad-input.json`` and ``root-bad-baseline-v23.json``;
    ``NUL_RECOVERY_REPLAY_DATA_ROOT`` names the data root of the archived PDFs. Everything is opened read-only;
    source-only drafts precede semantic routing, so they are compared only with source-only baselines.
    """

    def setUp(self) -> None:
        directory, data_root = os.environ.get(REPLAY_DIR_ENV), os.environ.get(REPLAY_DATA_ROOT_ENV)
        if not directory or not data_root:
            self.skipTest(f"{REPLAY_DIR_ENV} and {REPLAY_DATA_ROOT_ENV} are not set: retained real sources are "
                          "an explicit root opt-in and stay outside Git")
        self.directory, self.data_root = Path(directory), Path(data_root)

    def _replay(self, inputs_name: str) -> list[tuple[dict[str, Any], list[Any], Any, str]]:
        from disclosure_anchor.adapters.parsers.pdf_text_observation import observe_pdf_text_rectangles

        replayed = []
        for row in json.loads((self.directory / inputs_name).read_bytes())["inputs"]:
            envelope_path = Path(row["envelope_path"])
            envelope_bytes = envelope_path.read_bytes()
            self.assertEqual(hashlib.sha256(envelope_bytes).hexdigest(), row["envelope_sha256"])
            envelope = provider_document_envelope_from_bytes(envelope_bytes)
            pdf = (self.data_root / envelope.source_pdf_relpath).resolve()
            self.assertTrue(pdf.is_relative_to(self.data_root.resolve()))
            pdf_bytes = pdf.read_bytes()
            self.assertEqual("sha256:" + hashlib.sha256(pdf_bytes).hexdigest(), envelope.input_raw_file_hash)
            observations = observe_pdf_text_rectangles(pdf, document=envelope.provider_document)
            record = {"envelope_sha256": "sha256:" + hashlib.sha256(envelope_bytes).hexdigest(),
                      "source_sha256": "sha256:" + hashlib.sha256(pdf_bytes).hexdigest(),
                      "observations": [asdict(item) for item in observations]}
            record_sha = "sha256:" + hashlib.sha256(json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")).hexdigest()
            semantics = derive_source_semantics(document=envelope.provider_document, observations=observations)
            build = build_source_provider_units(semantics, semantic_record_sha256=record_sha,
                                                target_identity=envelope.parser_target_identity)
            units = []
            for draft in build.units:
                value = asdict(draft)
                value["locator"] = provider_unit_locator_to_payload(draft.locator)
                units.append(json.loads(json.dumps(value, ensure_ascii=False)))
            self.assertEqual(envelope_path.read_bytes(), envelope_bytes)
            self.assertEqual(pdf.read_bytes(), pdf_bytes)
            replayed.append((row, units, semantics, record_sha))
        return replayed

    def test_nul_free_retained_sources_build_byte_identical_units_to_the_v23_baseline(self) -> None:
        baseline = json.loads((self.directory / "root-baseline-v23.json").read_bytes())["documents"]
        replayed = self._replay("root-replay-inputs.json")
        self.assertEqual(len(replayed), len(baseline))
        for (row, units, semantics, record_sha), expected in zip(replayed, baseline, strict=True):
            with self.subTest(pages=expected["pages"]):
                self.assertEqual(expected["original_nul_count"], 0)
                self.assertEqual(semantics.text_substitutions, ())
                self.assertEqual(record_sha, expected["semantic_record_sha256"])
                self.assertEqual(units, expected["units"])

    def test_the_actual_nul_source_changes_only_unit_22_and_only_by_the_recorded_marker(self) -> None:
        baseline, = json.loads((self.directory / "root-bad-baseline-v23.json").read_bytes())["documents"]
        (row, units, semantics, record_sha), = self._replay("root-replay-bad-input.json")
        self.assertEqual((len(units), len(baseline["units"])), (22, 22))
        self.assertEqual(record_sha, baseline["semantic_record_sha256"])
        self.assertEqual(units[:21], baseline["units"][:21])
        record, = semantics.text_substitutions
        self.assertEqual(record.provider_text_sha256, sha("第\x00节 重要事项"))
        self.assertEqual((record.substituted_text, record.occurrence_count), ("第\U0000FFFD节 重要事项", 1))
        actual, before = units[21], baseline["units"][21]
        self.assertEqual(actual["title"], "第\U0000FFFD节 重要事项")
        self.assertEqual(actual["heading_path"][2], actual["title"])
        self.assertEqual(actual["heading_path"][:2], before["heading_path"][:2])
        self.assertEqual((actual["quality_status"], before["quality_status"]), ("needs_review", "ok"))
        self.assertEqual((actual["payload"], actual["content_hash"]), (before["payload"], before["content_hash"]))
        self.assertNotEqual(actual["structure_hash"], before["structure_hash"])
        locator = actual["locator"]
        self.assertEqual(locator["contract_version"], V10)
        self.assertEqual([item["provider_text_sha256"] for item in locator["text_substitutions"]],
                         [sha("第\x00节 重要事项")])
        comparable = {key: value for key, value in locator.items() if key not in {"contract_version",
                                                                                  "text_substitutions"}}
        self.assertEqual(comparable, {key: value for key, value in before["locator"].items() if key != "contract_version"})
        self.assertFalse(any(NUL in text for unit in units for text in strings(
            {"title": unit["title"], "heading_path": unit["heading_path"], "payload": unit["payload"]})))


if __name__ == "__main__":
    unittest.main()
