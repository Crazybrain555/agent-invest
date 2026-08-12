"""PublishRun use case and U5 diff tests."""

from __future__ import annotations

from dataclasses import replace
import unittest

from disclosure_anchor.application.contracts.provider_document_admission import (
    AdmittedProviderDocument,
)
from disclosure_anchor.application.contracts.provider_unit import (
    PROVIDER_UNIT_BUILDER_VERSION,
    provider_unit_locator_to_payload,
)
from disclosure_anchor.application.services.provider_unit_builder import (
    build_provider_units,
)
from disclosure_anchor.application.use_cases.publish_run import (
    ProviderDocumentPublicationGuard,
    PublishRun,
    PublishRunCommand,
    _validate_candidate_unit_set,
    diff_units,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import PublishRunError
from disclosure_anchor.domain.services.unit_hashing import (
    compute_unit_hashes,
    content_hash_aggregate,
    structure_hash_aggregate,
)
from tests.unit._fakes import FakeUnitOfWork
from tests.unit.test_provider_unit_builder import (
    _admitted,
    _representative_document,
)


def _unit(
    asset_id: str,
    run_id: str,
    *,
    content_hash: str | None = None,
    order_index: int = 1,
    title: str | None = "标题",
    heading_path: list[str] | None = None,
    semantic_key: str | None = "document_content",
    quality_status: str = "ok",
    query_projection_hash: str | None = None,
    structure_hash: str | None = None,
    applicability: str | None = None,
    payload_kind: str = "text",
    payload: dict[str, object] | None = None,
) -> e.DocumentUnit:
    resolved_payload = payload or {"text": asset_id}
    resolved_heading_path = heading_path or ["第一节"]
    hashes = compute_unit_hashes(
        payload_kind=payload_kind,
        payload=resolved_payload,
        title=title,
        heading_path=resolved_heading_path,
        semantic_key=semantic_key,
        quality_status=quality_status,
        order_index=order_index,
        applicability=applicability,
    )
    return e.DocumentUnit(
        asset_id=asset_id,
        document_id="doc_1",
        processing_run_id=run_id,
        payload_kind=payload_kind,
        order_index=order_index,
        payload=resolved_payload,
        content_hash=content_hash or hashes.content_hash,
        title=title,
        heading_path=resolved_heading_path,
        semantic_key=semantic_key,
        quality_status=quality_status,
        query_projection_hash=(query_projection_hash or hashes.query_projection_hash),
        structure_hash=structure_hash or hashes.structure_hash,
        applicability=applicability,
    )


def _run(
    run_id: str,
    *,
    active: bool = False,
    content_hash_aggregate: str | None = None,
    structure_hash: str | None = None,
) -> e.ProcessingRun:
    return e.ProcessingRun(
        processing_run_id=run_id,
        document_id="doc_1",
        artifact_owner_processing_run_id=run_id,
        run_kind="parse",
        status="succeeded",
        unit_build_status="succeeded",
        parser_name="MinerU",
        parser_version="3.4.4",
        parser_backend="hybrid-http-client",
        parser_method="auto",
        parser_language="ch",
        provider_document_relpath=(
            f"derived/provider_documents/cninfo/002484/pid_1/{run_id}/"
            "provider_document.v1.json"
        ),
        is_active=active,
        content_hash_aggregate=(
            content_hash_aggregate or content_hash_aggregate_for([])
        ),
        structure_hash=structure_hash or structure_hash_aggregate([]),
    )


def _uow_with_document() -> FakeUnitOfWork:
    uow = FakeUnitOfWork()
    company = uow.companies.add(e.Company(company_id="co_1", legal_name="江海股份"))
    security = uow.securities.add(
        e.Security(
            security_id="sec_1",
            company_id=company.company_id,
            security_code="002484",
            exchange="SZSE",
        )
    )
    uow.documents.add(
        e.Document(
            document_id="doc_1",
            status="parsed",
            company_id=company.company_id,
            security_id=security.security_id,
            provider="cninfo",
            provider_document_id="pid_1",
        )
    )
    return uow


def content_hash_aggregate_for(units: list[e.DocumentUnit]) -> str:
    return content_hash_aggregate(unit.content_hash for unit in units)


def _sync_run_hashes(uow: FakeUnitOfWork, run_id: str) -> None:
    units = uow.document_units.list_by_processing_run(run_id)
    run = uow.processing_runs.get(run_id)
    assert run is not None
    run.content_hash_aggregate = content_hash_aggregate_for(units)
    run.structure_hash = structure_hash_aggregate(
        unit.structure_hash or ""
        for unit in sorted(units, key=lambda item: item.order_index)
    )


def _allow_provider_units(**_kwargs: object) -> None:
    """Unit tests below isolate publish semantics from artifact I/O."""


def _publisher(
    uow: FakeUnitOfWork,
    *,
    publication_guard=_allow_provider_units,  # noqa: ANN001
) -> PublishRun:
    return PublishRun(
        uow_factory=lambda: uow,
        publication_guard=publication_guard,
    )


class _FixedAdmission:
    def __init__(self, admitted: AdmittedProviderDocument) -> None:
        self.admitted = admitted

    def admit(self, **_kwargs: object) -> AdmittedProviderDocument:
        return self.admitted


def _provider_guard_fixture() -> tuple[
    ProviderDocumentPublicationGuard,
    e.ProcessingRun,
    e.Document,
    list[e.DocumentUnit],
]:
    admitted = _admitted(_representative_document())
    envelope = admitted.envelope
    document = e.Document(
        document_id=envelope.document_id,
        status="parsed",
        provider=envelope.provider,
        provider_document_id=envelope.provider_document_id,
        raw_file_relpath=envelope.source_pdf_relpath,
        raw_file_hash=envelope.input_raw_file_hash,
    )
    run = e.ProcessingRun(
        processing_run_id=envelope.artifact_owner_processing_run_id,
        document_id=document.document_id,
        artifact_owner_processing_run_id=envelope.artifact_owner_processing_run_id,
        run_kind="parse",
        status="succeeded",
        unit_build_status="succeeded",
        parser_name="MinerU",
        parser_version="3.4.4",
        parser_backend="hybrid-http-client",
        parser_method="auto",
        parser_language="ch",
        parser_target_identity=envelope.parser_target_identity.to_payload(),
        input_raw_file_hash=envelope.input_raw_file_hash,
        parser_artifact_relpath=envelope.parser_artifact_root_relpath,
        artifact_hash=admitted.provider_document_sha256,
        provider_document_relpath=admitted.provider_document_relpath.as_posix(),
        builder_rules_version=PROVIDER_UNIT_BUILDER_VERSION,
    )
    units = []
    for draft in build_provider_units(admitted).units:
        units.append(
            e.DocumentUnit(
                asset_id=f"asset_{draft.unit_index}",
                document_id=document.document_id,
                processing_run_id=run.processing_run_id,
                provider_document_id=document.provider_document_id,
                payload_kind=draft.payload_kind,
                payload=dict(draft.payload),
                content_hash=draft.content_hash,
                title=draft.title,
                heading_path=list(draft.heading_path),
                order_index=draft.unit_index + 1,
                semantic_key=draft.semantic_key,
                quality_status=draft.quality_status,
                query_projection_hash=draft.query_projection_hash,
                structure_hash=draft.structure_hash,
                page_no=draft.page_no,
                artifact_locator=provider_unit_locator_to_payload(draft.locator),
            )
        )
    return (
        ProviderDocumentPublicationGuard(_FixedAdmission(admitted)),  # type: ignore[arg-type]
        run,
        document,
        units,
    )


class PublishRunTests(unittest.TestCase):
    def test_provider_guard_replays_every_persisted_field(self) -> None:
        guard, run, document, units = _provider_guard_fixture()

        guard(
            run=run,
            document=document,
            artifact_owner=run,
            security_code="000001",
            units=units,
        )

        cases = {
            "provider_document_id": replace(
                units[0], provider_document_id="forged"
            ),
            "page_no": replace(units[0], page_no=999),
            "artifact_locator": replace(
                units[0], artifact_locator={"contract_version": "forged.v1"}
            ),
        }
        for reason, bad_unit in cases.items():
            with self.subTest(reason=reason), self.assertRaises(PublishRunError) as caught:
                guard(
                    run=run,
                    document=document,
                    artifact_owner=run,
                    security_code="000001",
                    units=[bad_unit, *units[1:]],
                )
            self.assertEqual(
                caught.exception.error["reason_code"],
                f"{reason}_mismatch",
            )

    def test_provider_guard_rejects_legacy_candidate(self) -> None:
        guard, run, document, units = _provider_guard_fixture()
        run.provider_document_relpath = None
        run.normalized_ir_relpath = "derived/normalized_ir/v4.json"

        with self.assertRaises(PublishRunError) as caught:
            guard(
                run=run,
                document=document,
                artifact_owner=run,
                security_code="000001",
                units=units,
            )

        self.assertEqual(
            caught.exception.error["error_code"],
            "RUN_OUTPUT_CONTRACT_UNSUPPORTED",
        )

    def test_first_publish_creates_unit_events_then_published(self) -> None:
        uow = _uow_with_document()
        uow.processing_runs.add(_run("run_new"))
        uow.document_units.add(_unit("du_new_1", "run_new", order_index=1))
        uow.document_units.add(_unit("du_new_2", "run_new", order_index=2))
        _sync_run_hashes(uow, "run_new")

        result = _publisher(uow).execute(
            PublishRunCommand(processing_run_id="run_new")
        )

        self.assertEqual(result.created_count, 2)
        self.assertEqual(result.published_change_kind, "materialized")
        self.assertEqual(uow.documents.get("doc_1").status, "published")
        self.assertEqual(
            uow.documents.get("doc_1").current_processing_run_id, "run_new"
        )
        events = sorted(uow.outbox.all(), key=lambda item: item.seq)
        self.assertEqual(
            [event.event_kind for event in events],
            [
                "document_unit_created",
                "document_unit_created",
                "processing_run_published",
            ],
        )
        self.assertEqual(events[0].subject_kind, "document_unit")
        self.assertEqual(events[-1].subject_kind, "processing_run")
        self.assertEqual(events[-1].payload["created_count"], 2)

    def test_idempotent_publish_writes_no_events(self) -> None:
        uow = _uow_with_document()
        uow.documents.get("doc_1").current_processing_run_id = "run_new"
        uow.documents.get("doc_1").status = "published"
        uow.processing_runs.add(_run("run_new", active=True))
        uow.document_units.add(_unit("du_new_1", "run_new"))

        result = _publisher(uow).execute(
            PublishRunCommand(processing_run_id="run_new")
        )

        self.assertTrue(result.idempotent)
        self.assertEqual(uow.outbox.all(), [])

    def test_multiset_duplicate_delete_removes_one_old_unit(self) -> None:
        old_units = [
            _unit("du_old_1", "run_old", order_index=1, payload={"text": "same"}),
            _unit("du_old_2", "run_old", order_index=2, payload={"text": "same"}),
        ]
        new_units = [
            _unit("du_new_1", "run_new", order_index=1, payload={"text": "same"})
        ]

        diff = diff_units(old_units=old_units, new_units=new_units)

        self.assertEqual([unit.asset_id for unit in diff.removed], ["du_old_2"])
        self.assertEqual(diff.created, [])

    def test_duplicate_content_pairs_equal_projection_before_position(self) -> None:
        old_units = [
            _unit(
                "du_old_a",
                "run_old",
                order_index=1,
                title="甲",
                query_projection_hash="sha256:projection_a",
                payload={"text": "same"},
            ),
            _unit(
                "du_old_b",
                "run_old",
                order_index=2,
                title="乙",
                query_projection_hash="sha256:projection_b",
                payload={"text": "same"},
            ),
        ]
        new_units = [
            _unit(
                "du_new_b",
                "run_new",
                order_index=1,
                title="乙",
                query_projection_hash="sha256:projection_b",
                payload={"text": "same"},
            ),
            _unit(
                "du_new_a",
                "run_new",
                order_index=2,
                title="甲",
                query_projection_hash="sha256:projection_a",
                payload={"text": "same"},
            ),
        ]

        diff = diff_units(old_units=old_units, new_units=new_units)

        self.assertEqual(diff.created, [])
        self.assertEqual(diff.removed, [])
        self.assertEqual(diff.projection_changed, [])

    def test_projection_change_uses_fixed_changed_fields(self) -> None:
        old = _unit(
            "du_old",
            "run_old",
            title="原标题",
            semantic_key=None,
            query_projection_hash="sha256:old_projection",
            payload={"text": "same"},
        )
        new = _unit(
            "du_new",
            "run_new",
            title="新标题",
            semantic_key="receivable_aging",
            query_projection_hash="sha256:new_projection",
            payload={"text": "same"},
        )

        diff = diff_units(old_units=[old], new_units=[new])

        self.assertEqual(diff.created, [])
        self.assertEqual(diff.removed, [])
        self.assertEqual(
            diff.projection_changed[0][2],
            ["title", "semantic_key"],
        )

    def test_applicability_only_change_reports_changed_fields(self) -> None:
        # query_projection_hash includes applicability; a hash change with
        # changed_fields=[] is an audit hole (round3 P1#8).
        old = _unit(
            "du_old",
            "run_old",
            applicability=None,
            query_projection_hash="sha256:old_projection",
            payload={"text": "same"},
        )
        new = _unit(
            "du_new",
            "run_new",
            applicability="not_applicable",
            query_projection_hash="sha256:new_projection",
            payload={"text": "same"},
        )

        diff = diff_units(old_units=[old], new_units=[new])

        self.assertEqual(diff.projection_changed[0][2], ["applicability"])

    def test_mixed_part_annotation_change_reports_changed_field(self) -> None:
        old = _unit(
            "du_old",
            "run_old",
            payload_kind="mixed",
            payload={
                "semantic_type": "section",
                "parts": [{"kind": "text", "order": 1, "text": "正文"}],
            },
            query_projection_hash="sha256:old_projection",
        )
        new = _unit(
            "du_new",
            "run_new",
            payload_kind="mixed",
            payload={
                "semantic_type": "section",
                "parts": [
                    {
                        "kind": "text",
                        "order": 2,
                        "text": "正文",
                        "applicability": "applicable",
                    }
                ],
            },
            query_projection_hash="sha256:new_projection",
        )

        diff = diff_units(old_units=[old], new_units=[new])

        self.assertEqual(diff.created, [])
        self.assertEqual(diff.removed, [])
        self.assertEqual(
            diff.projection_changed[0][2],
            ["mixed_part_annotations"],
        )

    def test_mixed_annotation_hash_and_diff_share_one_projection(self) -> None:
        old_payload = {
            "semantic_type": "section",
            "parts": [{"kind": "text", "order": 1, "text": "正文"}],
        }
        new_payload = {
            "semantic_type": "section",
            "parts": [
                {
                    "kind": "text",
                    "order": 99,
                    "text": "正文",
                    "local_heading": ["（一）收入"],
                }
            ],
        }
        common = {
            "payload_kind": "mixed",
            "title": "经营情况",
            "heading_path": ["一、经营情况"],
            "semantic_key": "document_content",
            "quality_status": "ok",
            "order_index": 1,
        }
        old_hash = compute_unit_hashes(payload=old_payload, **common)
        new_hash = compute_unit_hashes(payload=new_payload, **common)
        old = _unit(
            "du_old",
            "run_old",
            payload_kind="mixed",
            payload=old_payload,
            title="经营情况",
            heading_path=["一、经营情况"],
            semantic_key="document_content",
            content_hash=old_hash.content_hash,
            query_projection_hash=old_hash.query_projection_hash,
        )
        new = _unit(
            "du_new",
            "run_new",
            payload_kind="mixed",
            payload=new_payload,
            title="经营情况",
            heading_path=["一、经营情况"],
            semantic_key="document_content",
            content_hash=new_hash.content_hash,
            query_projection_hash=new_hash.query_projection_hash,
        )

        diff = diff_units(old_units=[old], new_units=[new])

        self.assertEqual(old.content_hash, new.content_hash)
        self.assertEqual(diff.projection_changed[0][2], ["mixed_part_annotations"])

    def test_stale_stored_projection_hashes_do_not_create_a_change(self) -> None:
        old = _unit(
            "du_old",
            "run_old",
            query_projection_hash="sha256:stale_old",
            payload={"text": "same"},
        )
        new = _unit(
            "du_new",
            "run_new",
            query_projection_hash="sha256:stale_new",
            payload={"text": "same"},
        )

        diff = diff_units(old_units=[old], new_units=[new])

        self.assertEqual(diff.created, [])
        self.assertEqual(diff.removed, [])
        self.assertEqual(diff.projection_changed, [])

    def test_diff_uses_canonical_payload_instead_of_stored_content_hash(self) -> None:
        same_stored = "sha256:" + "a" * 64
        old = _unit(
            "du_old",
            "run_old",
            content_hash=same_stored,
            payload={"text": "old"},
        )
        new = _unit(
            "du_new",
            "run_new",
            content_hash=same_stored,
            payload={"text": "new"},
        )

        diff = diff_units(old_units=[old], new_units=[new])

        self.assertEqual([unit.asset_id for unit in diff.removed], ["du_old"])
        self.assertEqual([unit.asset_id for unit in diff.created], ["du_new"])

    def test_diff_ignores_stale_stored_content_hash_when_payload_matches(self) -> None:
        old = _unit(
            "du_old",
            "run_old",
            content_hash="sha256:" + "a" * 64,
            payload={"text": "same"},
        )
        new = _unit(
            "du_new",
            "run_new",
            content_hash="sha256:" + "b" * 64,
            payload={"text": "same"},
        )

        diff = diff_units(old_units=[old], new_units=[new])

        self.assertEqual(diff.created, [])
        self.assertEqual(diff.removed, [])
        self.assertEqual(diff.projection_changed, [])

    def test_candidate_unit_hash_mismatch_fails_before_publish_mutation(self) -> None:
        for field in ("content_hash", "query_projection_hash", "structure_hash"):
            with self.subTest(field=field):
                uow = _uow_with_document()
                document = uow.documents.get("doc_1")
                document.current_processing_run_id = "run_old"
                document.status = "published"
                uow.processing_runs.add(_run("run_old", active=True))
                uow.processing_runs.add(_run("run_new"))
                new_unit = _unit("du_new", "run_new")
                uow.document_units.add(new_unit)
                _sync_run_hashes(uow, "run_new")
                setattr(new_unit, field, "sha256:" + "f" * 64)

                with self.assertRaises(PublishRunError) as ctx:
                    _publisher(uow).execute(
                        PublishRunCommand(processing_run_id="run_new")
                    )

                self.assertEqual(
                    ctx.exception.error["error_code"], "RUN_UNIT_HASH_INVALID"
                )
                self.assertEqual(
                    document.current_processing_run_id,
                    "run_old",
                )
                self.assertTrue(uow.processing_runs.get("run_old").is_active)
                failed_run = uow.processing_runs.get("run_new")
                self.assertFalse(failed_run.is_active)
                self.assertEqual(failed_run.unit_build_status, "failed")
                self.assertEqual(
                    failed_run.unit_build_error["error_code"],
                    "RUN_UNIT_HASH_INVALID",
                )
                self.assertEqual(uow.outbox.all(), [])

    def test_candidate_aggregate_mismatch_fails_before_publish_mutation(self) -> None:
        for field in ("content_hash_aggregate", "structure_hash"):
            with self.subTest(field=field):
                uow = _uow_with_document()
                document = uow.documents.get("doc_1")
                document.current_processing_run_id = "run_old"
                document.status = "published"
                uow.processing_runs.add(_run("run_old", active=True))
                uow.processing_runs.add(_run("run_new"))
                uow.document_units.add(_unit("du_new", "run_new"))
                _sync_run_hashes(uow, "run_new")
                setattr(
                    uow.processing_runs.get("run_new"),
                    field,
                    "sha256:" + "e" * 64,
                )

                with self.assertRaises(PublishRunError) as ctx:
                    _publisher(uow).execute(
                        PublishRunCommand(processing_run_id="run_new")
                    )

                self.assertEqual(
                    ctx.exception.error["error_code"],
                    "RUN_HASH_AGGREGATE_INVALID",
                )
                self.assertEqual(document.current_processing_run_id, "run_old")
                self.assertTrue(uow.processing_runs.get("run_old").is_active)
                self.assertFalse(uow.processing_runs.get("run_new").is_active)
                self.assertEqual(uow.outbox.all(), [])

    def test_candidate_semantic_invariant_is_rechecked_at_publish(self) -> None:
        uow = _uow_with_document()
        uow.processing_runs.add(_run("run_new"))
        unit = _unit("du_new", "run_new")
        unit.semantic_key = "INVALID"
        hashes = compute_unit_hashes(
            payload_kind=unit.payload_kind,
            payload=unit.payload,
            title=unit.title,
            heading_path=unit.heading_path,
            semantic_key=unit.semantic_key,
            quality_status=unit.quality_status,
            order_index=unit.order_index,
            applicability=unit.applicability,
        )
        unit.content_hash = hashes.content_hash
        unit.query_projection_hash = hashes.query_projection_hash
        unit.structure_hash = hashes.structure_hash
        uow.document_units.add(unit)
        _sync_run_hashes(uow, "run_new")

        with self.assertRaises(PublishRunError) as ctx:
            _publisher(uow).execute(
                PublishRunCommand(processing_run_id="run_new")
            )

        self.assertEqual(ctx.exception.error["error_code"], "RUN_UNIT_SEMANTIC_INVALID")
        self.assertIsNone(uow.documents.get("doc_1").current_processing_run_id)
        self.assertEqual(uow.outbox.all(), [])

    def test_candidate_unit_set_invariants_are_closed(self) -> None:
        cases = {
            "duplicate_asset_id": [
                _unit("du_same", "run_new", order_index=1),
                _unit("du_same", "run_new", order_index=2),
            ],
            "processing_run_mismatch": [
                _unit("du_new", "run_other", order_index=1)
            ],
            "document_mismatch": [_unit("du_new", "run_new", order_index=1)],
            "order_index_not_contiguous": [
                _unit("du_new", "run_new", order_index=2)
            ],
        }
        cases["document_mismatch"][0].document_id = "doc_other"
        for reason_code, units in cases.items():
            with self.subTest(reason_code=reason_code):
                with self.assertRaises(PublishRunError) as ctx:
                    _validate_candidate_unit_set(run=_run("run_new"), units=units)

                self.assertEqual(
                    ctx.exception.error["error_code"], "RUN_UNIT_SET_INVALID"
                )
                self.assertEqual(ctx.exception.error["reason_code"], reason_code)

    def test_candidate_order_invariant_fails_before_publish_mutation(self) -> None:
        uow = _uow_with_document()
        document = uow.documents.get("doc_1")
        document.current_processing_run_id = "run_old"
        document.status = "published"
        uow.processing_runs.add(_run("run_old", active=True))
        uow.processing_runs.add(_run("run_new"))
        uow.document_units.add(_unit("du_new", "run_new", order_index=2))
        _sync_run_hashes(uow, "run_new")

        with self.assertRaises(PublishRunError) as ctx:
            _publisher(uow).execute(
                PublishRunCommand(processing_run_id="run_new")
            )

        self.assertEqual(ctx.exception.error["error_code"], "RUN_UNIT_SET_INVALID")
        self.assertEqual(
            ctx.exception.error["reason_code"], "order_index_not_contiguous"
        )
        self.assertEqual(document.current_processing_run_id, "run_old")
        self.assertTrue(uow.processing_runs.get("run_old").is_active)
        self.assertFalse(uow.processing_runs.get("run_new").is_active)
        self.assertEqual(uow.outbox.all(), [])

    def test_second_publish_event_order_and_observed_when_content_same(self) -> None:
        uow = _uow_with_document()
        document = uow.documents.get("doc_1")
        document.current_processing_run_id = "run_old"
        document.status = "published"
        uow.processing_runs.add(
            _run("run_old", active=True, content_hash_aggregate="sha256:same")
        )
        uow.processing_runs.add(_run("run_new", content_hash_aggregate="sha256:same"))
        uow.document_units.add(
            _unit("du_old", "run_old", order_index=1, payload={"text": "same"})
        )
        uow.document_units.add(
            _unit("du_new", "run_new", order_index=1, payload={"text": "same"})
        )
        _sync_run_hashes(uow, "run_new")

        result = _publisher(uow).execute(
            PublishRunCommand(processing_run_id="run_new")
        )

        self.assertEqual(result.published_change_kind, "observed")
        events = sorted(uow.outbox.all(), key=lambda item: item.seq)
        self.assertEqual(
            [event.event_kind for event in events], ["processing_run_published"]
        )
        self.assertEqual(events[0].change_kind, "observed")
        self.assertFalse(uow.processing_runs.get("run_old").is_active)
        self.assertTrue(uow.processing_runs.get("run_new").is_active)

    def test_changed_content_emits_removed_created_then_published(self) -> None:
        uow = _uow_with_document()
        document = uow.documents.get("doc_1")
        document.current_processing_run_id = "run_old"
        document.status = "published"
        uow.processing_runs.add(
            _run("run_old", active=True, content_hash_aggregate="sha256:old")
        )
        uow.processing_runs.add(_run("run_new", content_hash_aggregate="sha256:new"))
        uow.document_units.add(_unit("du_old", "run_old", payload={"text": "old"}))
        uow.document_units.add(_unit("du_new", "run_new", payload={"text": "new"}))
        _sync_run_hashes(uow, "run_new")

        result = _publisher(uow).execute(
            PublishRunCommand(processing_run_id="run_new")
        )

        self.assertEqual(result.removed_count, 1)
        self.assertEqual(result.created_count, 1)
        events = sorted(uow.outbox.all(), key=lambda item: item.seq)
        self.assertEqual(
            [event.event_kind for event in events],
            [
                "document_unit_removed",
                "document_unit_created",
                "processing_run_published",
            ],
        )
        self.assertEqual(events[0].payload["old_asset_id"], "du_old")
        self.assertEqual(events[1].payload["new_asset_id"], "du_new")
        self.assertEqual(events[2].change_kind, "materialized")

    def test_empty_run_requires_allow_empty_reason(self) -> None:
        uow = _uow_with_document()
        uow.processing_runs.add(_run("run_empty"))

        with self.assertRaises(PublishRunError) as ctx:
            _publisher(uow).execute(
                PublishRunCommand(processing_run_id="run_empty")
            )

        self.assertEqual(ctx.exception.error["error_code"], "EMPTY_RUN")

        result = _publisher(uow).execute(
            PublishRunCommand(
                processing_run_id="run_empty",
                allow_empty=True,
                reason="fixture intentionally empty",
            )
        )
        self.assertEqual(result.status, "published")
        event = uow.outbox.all()[0]
        self.assertEqual(
            event.payload["allow_empty_reason"], "fixture intentionally empty"
        )


if __name__ == "__main__":
    unittest.main()
