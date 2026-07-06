"""PublishRun use case and U5 diff tests."""

from __future__ import annotations

import unittest

from disclosure_anchor.application.use_cases.publish_run import (
    PublishRun,
    PublishRunCommand,
    diff_units,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.errors import PublishRunError
from tests.unit._fakes import FakeUnitOfWork


def _unit(
    asset_id: str,
    run_id: str,
    *,
    content_hash: str = "sha256:content",
    order_index: int = 1,
    title: str | None = "标题",
    heading_path: list[str] | None = None,
    semantic_key: str | None = None,
    quality_status: str = "ok",
    query_projection_hash: str = "sha256:projection",
    applicability: str | None = None,
) -> e.DocumentUnit:
    return e.DocumentUnit(
        asset_id=asset_id,
        document_id="doc_1",
        processing_run_id=run_id,
        payload_kind="text",
        order_index=order_index,
        payload={"text": asset_id},
        content_hash=content_hash,
        title=title,
        heading_path=heading_path or ["第一节"],
        semantic_key=semantic_key,
        quality_status=quality_status,
        query_projection_hash=query_projection_hash,
        applicability=applicability,
    )


def _run(
    run_id: str,
    *,
    active: bool = False,
    content_hash_aggregate: str = "sha256:agg",
    structure_hash: str = "sha256:structure",
) -> e.ProcessingRun:
    return e.ProcessingRun(
        processing_run_id=run_id,
        document_id="doc_1",
        run_kind="parse",
        status="succeeded",
        unit_build_status="succeeded",
        is_active=active,
        content_hash_aggregate=content_hash_aggregate,
        structure_hash=structure_hash,
    )


def _uow_with_document() -> FakeUnitOfWork:
    uow = FakeUnitOfWork()
    uow.documents.add(e.Document(document_id="doc_1", status="parsed"))
    return uow


class PublishRunTests(unittest.TestCase):
    def test_first_publish_creates_unit_events_then_published(self) -> None:
        uow = _uow_with_document()
        uow.processing_runs.add(_run("run_new"))
        uow.document_units.add(_unit("du_new_1", "run_new", order_index=1))
        uow.document_units.add(
            _unit("du_new_2", "run_new", content_hash="sha256:other", order_index=2)
        )

        result = PublishRun(uow_factory=lambda: uow).execute(
            PublishRunCommand(processing_run_id="run_new")
        )

        self.assertEqual(result.created_count, 2)
        self.assertEqual(result.published_change_kind, "materialized")
        self.assertEqual(uow.documents.get("doc_1").status, "published")
        self.assertEqual(uow.documents.get("doc_1").current_processing_run_id, "run_new")
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

        result = PublishRun(uow_factory=lambda: uow).execute(
            PublishRunCommand(processing_run_id="run_new")
        )

        self.assertTrue(result.idempotent)
        self.assertEqual(uow.outbox.all(), [])

    def test_multiset_duplicate_delete_removes_one_old_unit(self) -> None:
        old_units = [
            _unit("du_old_1", "run_old", order_index=1),
            _unit("du_old_2", "run_old", order_index=2),
        ]
        new_units = [_unit("du_new_1", "run_new", order_index=1)]

        diff = diff_units(old_units=old_units, new_units=new_units)

        self.assertEqual([unit.asset_id for unit in diff.removed], ["du_old_2"])
        self.assertEqual(diff.created, [])

    def test_projection_change_uses_fixed_changed_fields(self) -> None:
        old = _unit(
            "du_old",
            "run_old",
            title="原标题",
            semantic_key=None,
            query_projection_hash="sha256:old_projection",
        )
        new = _unit(
            "du_new",
            "run_new",
            title="新标题",
            semantic_key="receivable_aging",
            query_projection_hash="sha256:new_projection",
        )

        diff = diff_units(old_units=[old], new_units=[new])

        self.assertEqual(diff.created, [])
        self.assertEqual(diff.removed, [])
        self.assertEqual(diff.projection_changed[0][2], ["title", "semantic_key"])

    def test_applicability_only_change_reports_changed_fields(self) -> None:
        # query_projection_hash includes applicability; a hash change with
        # changed_fields=[] is an audit hole (round3 P1#8).
        old = _unit(
            "du_old",
            "run_old",
            applicability=None,
            query_projection_hash="sha256:old_projection",
        )
        new = _unit(
            "du_new",
            "run_new",
            applicability="not_applicable",
            query_projection_hash="sha256:new_projection",
        )

        diff = diff_units(old_units=[old], new_units=[new])

        self.assertEqual(diff.projection_changed[0][2], ["applicability"])

    def test_second_publish_event_order_and_observed_when_content_same(self) -> None:
        uow = _uow_with_document()
        document = uow.documents.get("doc_1")
        document.current_processing_run_id = "run_old"
        document.status = "published"
        uow.processing_runs.add(_run("run_old", active=True, content_hash_aggregate="sha256:same"))
        uow.processing_runs.add(_run("run_new", content_hash_aggregate="sha256:same"))
        uow.document_units.add(_unit("du_old", "run_old", order_index=1))
        uow.document_units.add(_unit("du_new", "run_new", order_index=2))

        result = PublishRun(uow_factory=lambda: uow).execute(
            PublishRunCommand(processing_run_id="run_new")
        )

        self.assertEqual(result.published_change_kind, "observed")
        events = sorted(uow.outbox.all(), key=lambda item: item.seq)
        self.assertEqual([event.event_kind for event in events], ["processing_run_published"])
        self.assertEqual(events[0].change_kind, "observed")
        self.assertFalse(uow.processing_runs.get("run_old").is_active)
        self.assertTrue(uow.processing_runs.get("run_new").is_active)

    def test_changed_content_emits_removed_created_then_published(self) -> None:
        uow = _uow_with_document()
        document = uow.documents.get("doc_1")
        document.current_processing_run_id = "run_old"
        document.status = "published"
        uow.processing_runs.add(_run("run_old", active=True, content_hash_aggregate="sha256:old"))
        uow.processing_runs.add(_run("run_new", content_hash_aggregate="sha256:new"))
        uow.document_units.add(_unit("du_old", "run_old", content_hash="sha256:old"))
        uow.document_units.add(_unit("du_new", "run_new", content_hash="sha256:new"))

        result = PublishRun(uow_factory=lambda: uow).execute(
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
            PublishRun(uow_factory=lambda: uow).execute(
                PublishRunCommand(processing_run_id="run_empty")
            )

        self.assertEqual(ctx.exception.error["error_code"], "EMPTY_RUN")

        result = PublishRun(uow_factory=lambda: uow).execute(
            PublishRunCommand(
                processing_run_id="run_empty",
                allow_empty=True,
                reason="fixture intentionally empty",
            )
        )
        self.assertEqual(result.status, "published")
        event = uow.outbox.all()[0]
        self.assertEqual(event.payload["allow_empty_reason"], "fixture intentionally empty")


if __name__ == "__main__":
    unittest.main()
