"""Milestone 05 build/publish integration checks."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import text

from disclosure_anchor.adapters.db.postgres.unit_of_work import SqlAlchemyUnitOfWork
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.application.use_cases.build_units import (
    BuildUnits,
    BuildUnitsCommand,
)
from disclosure_anchor.application.use_cases.publish_run import (
    PublishRun,
    PublishRunCommand,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.services.unit_hashing import (
    compute_unit_hashes,
    content_hash_aggregate,
    structure_hash_aggregate,
)
from disclosure_anchor.settings import Settings
from tests.integration._support import engine_or_skip


def _settings(root: Path) -> Settings:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    return Settings(
        disclosure_data_root=data_root,
        disclosure_shared_root=shared_root,
        disclosure_runtime_root=data_root / "runtime",
        mineru_model_cache=shared_root / "model_cache" / "mineru",
        hf_home=shared_root / "model_cache" / "huggingface",
        modelscope_cache=shared_root / "model_cache" / "modelscope",
    )


def _normalized_ir(text_value: str = "公司存在退市风险，请投资者注意。") -> dict:
    return {
        "contract_version": "normalized_ir.v2",
        "created_at": "2026-07-05T00:00:00Z",
        "document_id": "doc_placeholder",
        "source_pdf": "raw.pdf",
        "title": "公告",
        "parser": {},
        "parser_artifacts": {
            "artifact_root_relpath": "parser/a",
            "content_list_relpath": "parser/a/content.json",
        },
        "parsed_pages": {"start_page_no": 1, "end_page_no": 1, "full_pdf": True},
        "elements": [
            {
                "ir_id": "ir_1",
                "kind": "heading",
                "raw_kind": "text",
                "order_index": 1,
                "source_item_index": 1,
                "heading_level": 1,
                "text": "重要提示",
            },
            {
                "ir_id": "ir_2",
                "kind": "text",
                "raw_kind": "text",
                "order_index": 2,
                "source_item_index": 2,
                "text": text_value,
            },
        ],
    }


def _unit(
    asset_id: str,
    document_id: str,
    run_id: str,
    *,
    content_hash: str | None = None,
    order_index: int = 1,
    title: str = "标题",
    query_projection_hash: str | None = None,
    semantic_key: str | None = "document_content",
    semantic_keys: list[str] | None = None,
    payload: dict[str, object] | None = None,
) -> e.DocumentUnit:
    resolved_payload = payload or {"text": asset_id}
    resolved_semantic_keys = (
        [semantic_key]
        if semantic_keys is None and semantic_key is not None
        else semantic_keys
    )
    hashes = compute_unit_hashes(
        payload_kind="text",
        payload=resolved_payload,
        title=title,
        heading_path=["第一节"],
        semantic_key=semantic_key,
        semantic_keys=resolved_semantic_keys,
        quality_status="ok",
        order_index=order_index,
    )
    return e.DocumentUnit(
        asset_id=asset_id,
        document_id=document_id,
        processing_run_id=run_id,
        payload_kind="text",
        order_index=order_index,
        payload=resolved_payload,
        content_hash=content_hash or hashes.content_hash,
        title=title,
        heading_path=["第一节"],
        semantic_key=semantic_key,
        semantic_keys=resolved_semantic_keys,
        quality_status="ok",
        query_projection_hash=(query_projection_hash or hashes.query_projection_hash),
        structure_hash=hashes.structure_hash,
    )


class BuildPublishIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.settings = _settings(self.root)
        self.paths = FileStorePathBuilder(self.settings)
        self.provider_document_ids: list[str] = []

    def tearDown(self) -> None:
        for provider_document_id in self.provider_document_ids:
            self._delete_by_provider_document_id(provider_document_id)
        self.engine.dispose()
        self.tmpdir.cleanup()

    def _uow(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(engine=self.engine)

    def _delete_by_provider_document_id(self, provider_document_id: str) -> None:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT document_id, source_access_id, company_id, security_id "
                    "FROM disclosure_core.document "
                    "WHERE provider='cninfo' AND provider_document_id=:pid"
                ),
                {"pid": provider_document_id},
            ).all()
            document_ids = [row[0] for row in rows]
            source_access_ids = [row[1] for row in rows if row[1]]
            company_ids = [row[2] for row in rows if row[2]]
            security_ids = [row[3] for row in rows if row[3]]
            for document_id in document_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_ops.outbox_event WHERE document_id=:id"
                    ),
                    {"id": document_id},
                )
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.document_unit WHERE document_id=:id"
                    ),
                    {"id": document_id},
                )
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.processing_run WHERE document_id=:id"
                    ),
                    {"id": document_id},
                )
                conn.execute(
                    text("DELETE FROM disclosure_core.document WHERE document_id=:id"),
                    {"id": document_id},
                )
            for source_access_id in source_access_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.source_access WHERE source_access_id=:id"
                    ),
                    {"id": source_access_id},
                )
            for security_id in security_ids:
                conn.execute(
                    text("DELETE FROM disclosure_core.security WHERE security_id=:id"),
                    {"id": security_id},
                )
            for company_id in company_ids:
                conn.execute(
                    text(
                        "DELETE FROM disclosure_core.company_identifier "
                        "WHERE company_id=:id"
                    ),
                    {"id": company_id},
                )
                conn.execute(
                    text("DELETE FROM disclosure_core.company WHERE company_id=:id"),
                    {"id": company_id},
                )

    def _seed_document(self, provider_document_id: str) -> tuple[str, str, str]:
        self.provider_document_ids.append(provider_document_id)
        company_id = ids.new_company_id()
        security_id = ids.new_security_id()
        document_id = ids.new_document_id()
        run_id = ids.new_processing_run_id()
        ir_relpath = (
            Path("derived/normalized_ir")
            / "cninfo"
            / "T05"
            / provider_document_id
            / run_id
            / "normalized_ir.v2.json"
        )
        payload = {**_normalized_ir(), "document_id": document_id}
        path = self.paths.data_path(ir_relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self._uow() as uow:
            uow.companies.add(
                e.Company(company_id=company_id, legal_name=provider_document_id)
            )
            uow.securities.add(
                e.Security(
                    security_id=security_id,
                    company_id=company_id,
                    security_code="T05" + provider_document_id[-6:],
                    exchange="LOCAL",
                )
            )
            uow.documents.add(
                e.Document(
                    document_id=document_id,
                    status="parsed",
                    company_id=company_id,
                    security_id=security_id,
                    provider="cninfo",
                    provider_document_id=provider_document_id,
                    title="公告",
                    raw_file_hash="sha256:raw" + provider_document_id[-8:],
                )
            )
            uow.processing_runs.add(
                e.ProcessingRun(
                    processing_run_id=run_id,
                    document_id=document_id,
                    run_kind="parse",
                    status="succeeded",
                    normalized_ir_relpath=str(ir_relpath),
                )
            )
            uow.commit()
        return document_id, run_id, provider_document_id

    def _seed_direct_publish_document(
        self,
        provider_document_id: str,
        *,
        old_units: list[e.DocumentUnit],
        new_units: list[e.DocumentUnit],
        old_aggregate: str | None = None,
        new_aggregate: str | None = None,
    ) -> tuple[str, str]:
        self.provider_document_ids.append(provider_document_id)
        company_id = ids.new_company_id()
        security_id = ids.new_security_id()
        document_id = (
            old_units[0].document_id if old_units else new_units[0].document_id
        )
        old_run_id = (
            old_units[0].processing_run_id if old_units else ids.new_processing_run_id()
        )
        new_run_id = new_units[0].processing_run_id
        resolved_old_aggregate = old_aggregate or content_hash_aggregate(
            unit.content_hash for unit in old_units
        )
        resolved_new_aggregate = new_aggregate or content_hash_aggregate(
            unit.content_hash for unit in new_units
        )
        old_structure_aggregate = structure_hash_aggregate(
            unit.structure_hash or ""
            for unit in sorted(old_units, key=lambda item: item.order_index)
        )
        new_structure_aggregate = structure_hash_aggregate(
            unit.structure_hash or ""
            for unit in sorted(new_units, key=lambda item: item.order_index)
        )
        with self._uow() as uow:
            uow.companies.add(
                e.Company(company_id=company_id, legal_name=provider_document_id)
            )
            uow.securities.add(
                e.Security(
                    security_id=security_id,
                    company_id=company_id,
                    security_code="T05" + provider_document_id[-6:],
                    exchange="LOCAL",
                )
            )
            uow.documents.add(
                e.Document(
                    document_id=document_id,
                    status="published",
                    company_id=company_id,
                    security_id=security_id,
                    provider="cninfo",
                    provider_document_id=provider_document_id,
                    raw_file_hash="sha256:raw" + provider_document_id[-8:],
                    current_processing_run_id=old_run_id if old_units else None,
                )
            )
            if old_units:
                uow.processing_runs.add(
                    e.ProcessingRun(
                        processing_run_id=old_run_id,
                        document_id=document_id,
                        run_kind="parse",
                        status="succeeded",
                        unit_build_status="succeeded",
                        is_active=True,
                        content_hash_aggregate=resolved_old_aggregate,
                        structure_hash=old_structure_aggregate,
                    )
                )
            uow.processing_runs.add(
                e.ProcessingRun(
                    processing_run_id=new_run_id,
                    document_id=document_id,
                    run_kind="parse",
                    status="succeeded",
                    unit_build_status="succeeded",
                    content_hash_aggregate=resolved_new_aggregate,
                    structure_hash=new_structure_aggregate,
                )
            )
            uow.document_units.add_many(old_units + new_units)
            uow.commit()
        return document_id, new_run_id

    def test_build_publish_chain_snapshot_payload_and_idempotence(self) -> None:
        document_id, run_id, _ = self._seed_document("p05-chain-" + ids.new_ulid())
        build = BuildUnits(
            path_builder=self.paths,
            artifact_store=ArtifactStore(self.paths),
            uow_factory=self._uow,
        )
        publish = PublishRun(uow_factory=self._uow)

        build_result = build.execute(BuildUnitsCommand(processing_run_id=run_id))
        publish_result = publish.execute(PublishRunCommand(processing_run_id=run_id))
        second_publish = publish.execute(PublishRunCommand(processing_run_id=run_id))

        self.assertEqual(build_result.status, "succeeded")
        self.assertEqual(publish_result.status, "published")
        self.assertTrue(second_publish.idempotent)
        with self.engine.connect() as conn:
            unit_row = (
                conn.execute(
                    text(
                        "SELECT asset_id, payload FROM disclosure_core.document_unit "
                        "WHERE processing_run_id=:run_id"
                    ),
                    {"run_id": run_id},
                )
                .mappings()
                .one()
            )
            run_row = (
                conn.execute(
                    text(
                        "SELECT document_units_relpath FROM disclosure_core.processing_run "
                        "WHERE processing_run_id=:run_id"
                    ),
                    {"run_id": run_id},
                )
                .mappings()
                .one()
            )
            view_count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_public.document_units_v1 "
                    "WHERE document_id=:document_id"
                ),
                {"document_id": document_id},
            ).scalar_one()
            event_rows = (
                conn.execute(
                    text(
                        "SELECT event_kind, change_kind, subject_kind, subject_ref "
                        "FROM disclosure_public.change_events_v1 "
                        "WHERE document_id=:document_id ORDER BY seq"
                    ),
                    {"document_id": document_id},
                )
                .mappings()
                .all()
            )

        snapshot_path = self.paths.data_path(Path(run_row["document_units_relpath"]))
        snapshot_rows = [
            json.loads(line)
            for line in snapshot_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(view_count, 1)
        self.assertEqual(snapshot_rows[0]["asset_id"], unit_row["asset_id"])
        self.assertEqual(snapshot_rows[0]["payload"], unit_row["payload"])
        self.assertNotIn("query_projection_hash", snapshot_rows[0])
        self.assertEqual(
            [row["event_kind"] for row in event_rows],
            ["document_unit_created", "processing_run_published"],
        )
        self.assertEqual(event_rows[-1]["change_kind"], "materialized")
        self.assertEqual(event_rows[-1]["subject_kind"], "processing_run")
        self.assertEqual(event_rows[-1]["subject_ref"], run_id)

    def test_publish_failure_rolls_back_old_active_run(self) -> None:
        document_id = ids.new_document_id()
        old_run_id = ids.new_processing_run_id()
        new_run_id = ids.new_processing_run_id()
        pid = "p05-rollback-" + ids.new_ulid()
        old_units = [
            _unit(
                ids.new_asset_id(),
                document_id,
                old_run_id,
                payload={"text": "old"},
            )
        ]
        new_units = [
            _unit(
                ids.new_asset_id(),
                document_id,
                new_run_id,
                payload={"text": "new"},
            )
        ]
        self._seed_direct_publish_document(
            pid, old_units=old_units, new_units=new_units
        )

        class FailingUoW(SqlAlchemyUnitOfWork):
            def _bind_repositories(self, session):  # noqa: ANN001
                super()._bind_repositories(session)
                original_add = self.outbox.add

                def add(event):  # noqa: ANN001
                    if event.event_kind == "processing_run_published":
                        raise RuntimeError("injected publish failure")
                    return original_add(event)

                self.outbox.add = add

        with self.assertRaises(RuntimeError):
            PublishRun(uow_factory=lambda: FailingUoW(engine=self.engine)).execute(
                PublishRunCommand(processing_run_id=new_run_id)
            )

        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT processing_run_id, is_active "
                        "FROM disclosure_core.processing_run "
                        "WHERE processing_run_id IN (:old, :new)"
                    ),
                    {"old": old_run_id, "new": new_run_id},
                )
                .mappings()
                .all()
            )
            document = (
                conn.execute(
                    text(
                        "SELECT current_processing_run_id, status "
                        "FROM disclosure_core.document WHERE document_id=:document_id"
                    ),
                    {"document_id": document_id},
                )
                .mappings()
                .one()
            )
            event_count = conn.execute(
                text(
                    "SELECT count(*) FROM disclosure_ops.outbox_event "
                    "WHERE document_id=:document_id"
                ),
                {"document_id": document_id},
            ).scalar_one()

        active_by_run = {row["processing_run_id"]: row["is_active"] for row in rows}
        self.assertTrue(active_by_run[old_run_id])
        self.assertFalse(active_by_run[new_run_id])
        self.assertEqual(document["current_processing_run_id"], old_run_id)
        self.assertEqual(document["status"], "published")
        self.assertEqual(event_count, 0)

    def test_diff_scenarios_emit_contract_events(self) -> None:
        scenarios = []
        for label in ("same", "changed", "duplicate", "projection"):
            document_id = ids.new_document_id()
            old_run_id = ids.new_processing_run_id()
            new_run_id = ids.new_processing_run_id()
            if label == "same":
                old_units = [
                    _unit(
                        ids.new_asset_id(),
                        document_id,
                        old_run_id,
                        payload={"text": "same"},
                    )
                ]
                new_units = [
                    _unit(
                        ids.new_asset_id(),
                        document_id,
                        new_run_id,
                        payload={"text": "same"},
                    )
                ]
            elif label == "changed":
                old_units = [
                    _unit(
                        ids.new_asset_id(),
                        document_id,
                        old_run_id,
                        payload={"text": "old"},
                    )
                ]
                new_units = [
                    _unit(
                        ids.new_asset_id(),
                        document_id,
                        new_run_id,
                        payload={"text": "new"},
                    )
                ]
            elif label == "duplicate":
                old_units = [
                    _unit(
                        ids.new_asset_id(),
                        document_id,
                        old_run_id,
                        order_index=1,
                        payload={"text": "same"},
                    ),
                    _unit(
                        ids.new_asset_id(),
                        document_id,
                        old_run_id,
                        order_index=2,
                        payload={"text": "same"},
                    ),
                ]
                new_units = [
                    _unit(
                        ids.new_asset_id(),
                        document_id,
                        new_run_id,
                        payload={"text": "same"},
                    )
                ]
            else:
                old_units = [
                    _unit(
                        ids.new_asset_id(),
                        document_id,
                        old_run_id,
                        title="原标题",
                        query_projection_hash="sha256:old_projection",
                        payload={"text": "same"},
                    )
                ]
                new_units = [
                    _unit(
                        ids.new_asset_id(),
                        document_id,
                        new_run_id,
                        title="新标题",
                        payload={"text": "same"},
                    )
                ]
            pid = "p05-diff-" + label + "-" + ids.new_ulid()
            document_id, new_run_id = self._seed_direct_publish_document(
                pid,
                old_units=old_units,
                new_units=new_units,
            )
            PublishRun(uow_factory=self._uow).execute(
                PublishRunCommand(processing_run_id=new_run_id)
            )
            scenarios.append((label, document_id))

        by_label = {}
        with self.engine.connect() as conn:
            for label, document_id in scenarios:
                rows = (
                    conn.execute(
                        text(
                            "SELECT event_kind, change_kind, payload "
                            "FROM disclosure_public.change_events_v1 "
                            "WHERE document_id=:document_id ORDER BY seq"
                        ),
                        {"document_id": document_id},
                    )
                    .mappings()
                    .all()
                )
                by_label[label] = rows

        self.assertEqual(
            [row["event_kind"] for row in by_label["same"]],
            ["processing_run_published"],
        )
        self.assertEqual(by_label["same"][0]["change_kind"], "observed")
        self.assertEqual(
            [row["event_kind"] for row in by_label["changed"]],
            [
                "document_unit_removed",
                "document_unit_created",
                "processing_run_published",
            ],
        )
        duplicate_removed = [
            row
            for row in by_label["duplicate"]
            if row["event_kind"] == "document_unit_removed"
        ]
        self.assertEqual(len(duplicate_removed), 1)
        self.assertEqual(
            [row["event_kind"] for row in by_label["projection"]],
            ["document_unit_projection_changed", "processing_run_published"],
        )
        self.assertEqual(
            by_label["projection"][0]["payload"]["changed_fields"],
            ["title"],
        )


if __name__ == "__main__":
    unittest.main()
