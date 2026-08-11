"""06R search projection integration on the suite scratch database."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.orm import Session

from disclosure_anchor.application.use_cases import (
    build_search_projection as projection_module,
)
from disclosure_anchor.adapters.db.postgres.models import (
    Company,
    Document,
    DocumentUnit,
    ProcessingRun,
    Security,
)
from disclosure_anchor.adapters.retrieval import tokenizer
from disclosure_anchor.application.use_cases.build_search_projection import (
    BuildSearchProjection,
    BuildSearchProjectionCommand,
    SearchProjectionSafetyError,
)
from disclosure_anchor.application.contracts.provider_unit import (
    ProviderSearchDestination,
    ProviderUnitLocator,
    ProviderUnitPartKind,
    ProviderUnitPartRef,
    ProviderUnitSearchBinding,
    provider_unit_locator_to_payload,
)
from disclosure_anchor.application.contracts.retrieval_primary import (
    RetrievalTarget,
    SearchTransform,
)
from tests.integration._support import engine_or_skip

_SERVICE_ROOT = Path(__file__).resolve().parents[2]


def _text_search_locator(order_index: int) -> dict[str, object]:
    return _search_locator(
        order_index,
        kind="text",
        targets=(("text", None, "identity.v1"),),
    )


def _table_search_locator(order_index: int) -> dict[str, object]:
    return _search_locator(
        order_index,
        kind="table",
        targets=(
            ("table_body", None, "html_visible_text_segments.v1"),
            ("table_caption", 0, "identity.v1"),
        ),
    )


def _search_locator(
    order_index: int,
    *,
    kind: ProviderUnitPartKind,
    targets: tuple[tuple[str, int | None, SearchTransform], ...],
) -> dict[str, object]:
    source_index = order_index
    raw_block_sha256 = f"sha256:{order_index:064x}"
    bindings = tuple(
        ProviderUnitSearchBinding(
            source=RetrievalTarget(
                target_id=f"block:{source_index}:payload:{payload_ordinal}",
                source_index=source_index,
                payload_ordinal=payload_ordinal,
                field=field,
                item_index=item_index,
                transform=transform,
                raw_block_sha256=raw_block_sha256,
            ),
            destination=ProviderSearchDestination(
                kind="unit_payload",
                field=field,
                item_index=item_index,
            ),
        )
        for payload_ordinal, (field, item_index, transform) in enumerate(targets)
    )
    return provider_unit_locator_to_payload(
        ProviderUnitLocator(
            provider_document_sha256=f"sha256:{'a' * 64}",
            unit_index=order_index - 1,
            heading_chain=(),
            parts=(
                ProviderUnitPartRef(
                    part_index=0,
                    kind=kind,
                    block_source_indices=(source_index,),
                ),
            ),
            evidence_only_block_source_indices=(),
            unbound_table_parts=(),
            evidence_artifacts=(),
            search_targets=bindings,
        )
    )


class SearchProjectionIntegrationTests(unittest.TestCase):
    temp_url: str = ""
    subprocess_env: dict[str, str] = {}
    class_engine: sqlalchemy.engine.Engine | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.class_engine = engine_or_skip()
        cls.addClassCleanup(cls.class_engine.dispose)
        cls.temp_url = cls.class_engine.url.render_as_string(hide_password=False)
        cls.subprocess_env = {
            **os.environ,
            "DISCLOSURE_MIGRATION_DATABASE_URL": cls.temp_url,
            "DATABASE_URL": cls.temp_url,
            "PYTHONPATH": "src",
        }

    @classmethod
    def _alembic(cls, *args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(_SERVICE_ROOT),
            env=cls.subprocess_env,
            capture_output=True,
            text=True,
        )

    def setUp(self) -> None:
        assert self.class_engine is not None
        self.engine = self.class_engine

    # -- migration round trip ----------------------------------------------
    def test_migration_0025_upgrade_downgrade_upgrade(self) -> None:
        self.addCleanup(self._restore_migration_head)
        self.assertTrue(self._table_exists("unit_search_projection"))
        self.assertTrue(self._table_exists("unit_body_search_window"))
        self.assertTrue(self._view_exists("unit_search_projection_v1"))
        self.assertTrue(self._view_exists("unit_body_search_windows_v1"))
        self.assertTrue(self._view_exists("unit_search_atoms_v1"))
        self.assertTrue(self._function_exists("search_tsvector_is_safe"))
        parent_columns = self._view_columns("unit_search_projection_v1")
        self.assertEqual(
            parent_columns,
            [
                "asset_id",
                "retrieval_rules_version",
                "title_text",
                "heading_path_text",
                "title_tokens",
                "path_tokens",
                "body_tokens",
                "key_tokens",
                "header_row_candidate",
                "built_at",
                "search_tsv",
            ],
        )

        down = self._alembic("downgrade", "0024_reader_vocabulary_grants")
        self.assertEqual(down.returncode, 0, down.stderr[-500:])
        self.assertFalse(self._table_exists("unit_search_projection"))
        self.assertFalse(self._table_exists("unit_body_search_window"))
        self.assertFalse(self._view_exists("unit_search_projection_v1"))
        self.assertFalse(self._view_exists("unit_body_search_windows_v1"))
        self.assertFalse(self._view_exists("unit_search_atoms_v1"))
        self.assertFalse(self._function_exists("search_tsvector_is_safe"))

        up = self._alembic("upgrade", "head")
        self.assertEqual(up.returncode, 0, up.stderr[-500:])
        self.assertTrue(self._table_exists("unit_search_projection"))
        self.assertTrue(self._table_exists("unit_body_search_window"))
        self.assertTrue(self._view_exists("unit_search_projection_v1"))
        self.assertTrue(self._view_exists("unit_body_search_windows_v1"))
        self.assertTrue(self._view_exists("unit_search_atoms_v1"))
        self.assertTrue(self._function_exists("search_tsvector_is_safe"))
        self.assertEqual(
            self._view_columns("unit_search_atoms_v1"),
            [
                "asset_id",
                "atom_index",
                "atom_text",
                "retrieval_rules_version",
                "built_at",
            ],
        )

    def _restore_migration_head(self) -> None:
        restored = self._alembic("upgrade", "head")
        self.assertEqual(restored.returncode, 0, restored.stderr[-500:])

    # -- full rebuild: count + ts_rank ordering + trgm substring -----------
    def test_full_rebuild_counts_ranks_title_over_body_and_trgm(self) -> None:
        suffix = os.urandom(4).hex()
        ids = self._seed_two_units(suffix)
        try:
            with Session(self.engine) as session:
                body_unit = session.get(DocumentUnit, ids["body_hit"])
                assert body_unit is not None
                body_unit.payload_kind = "table"
                body_unit.payload = {
                    "provider_type": "table",
                    "table_body": (
                        "<table><tr><td>应收账款账龄分析</td></tr>"
                        "<tr><td>股份变动及股东情况</td></tr>"
                        "<tr><td>ＡＢＣ％ＤＥＦ＿ＧＨ＼Ｉ</td></tr></table>"
                    ),
                    "table_caption": ["甲乙丙丁戊己庚辛"],
                    "table_footnote": [],
                }
                body_unit.artifact_locator = _table_search_locator(2)
                session.commit()
            result = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=True)
            )
            active = self._active_unit_count()
            self.assertEqual(active, 2)
            self.assertEqual(result.projected, active)
            self.assertEqual(self._projection_count(), active)

            # ts_rank: '应收账款' sits in unit A's title (weight A) and unit B's
            # body (weight C); the title hit must rank strictly higher.
            ranked = self._ranked_asset_ids("应收账款")
            self.assertIn(ids["title_hit"], ranked)
            self.assertIn(ids["body_hit"], ranked)
            self.assertLess(
                ranked.index(ids["title_hit"]), ranked.index(ids["body_hit"])
            )

            # pg_trgm substring channel over the raw heading_path_text.
            self.assertEqual(self._trgm_hits(f"账龄{suffix}"), [ids["title_hit"]])

            # The body substring lane stores each explicit target leaf as one
            # NFKC/casefolded atom. LIKE is only the GIN candidate; strpos is
            # the exact same-atom heap recheck.
            for query in (
                "甲乙丙",
                "应收账",
                "股份变",
                "ＡＢＣ％ＤＥＦ＿ＧＨ＼Ｉ",
            ):
                self.assertEqual(self._atom_hits(query), [ids["body_hit"]])
                self.assertEqual(self._candidate_hits(query), [ids["body_hit"]])
            self.assertEqual(self._atom_hits("庚辛 应收"), [])
            self.assertEqual(self._atom_hits("股份"), [])
            self.assertEqual(self._candidate_hits("股份"), [ids["body_hit"]])
            self.assertEqual(self._candidate_hits("股份 不存在"), [])

            with self.engine.begin() as conn:
                conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
                plan_query = self._CANDIDATE_QUERY_SQL.rsplit("ORDER BY asset_id", 1)[0]
                plan = "\n".join(
                    str(row[0])
                    for row in conn.execute(
                        text("EXPLAIN " + plan_query),
                        {
                            "normalized_query": (
                                tokenizer.normalize_search_text("应收账")
                            ),
                            "query_groups": list(
                                tokenizer.build_search_tsquery_groups("应收账")
                            ),
                        },
                    )
                )
            self.assertIn("ix_unit_search_atom_text_trgm", plan)
        finally:
            self._cleanup(ids)

    def test_full_rebuild_prunes_orphans(self) -> None:
        suffix = os.urandom(4).hex()
        ids = self._seed_two_units(suffix)
        try:
            BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=True)
            )
            self.assertEqual(self._projection_count(), 2)
            # Deactivate the run: its units are no longer active, so a full
            # rebuild must delete their projection rows.
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.processing_run "
                        "SET is_active = false WHERE processing_run_id = :rid"
                    ),
                    {"rid": ids["run"]},
                )
            result = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=True)
            )
            self.assertEqual(result.deleted, 2)
            self.assertEqual(self._projection_count(), 0)
        finally:
            self._cleanup(ids)

    def test_delta_drains_unbounded_and_prunes_orphans(self) -> None:
        # Delta is the worker's per-round path: it must drain every missing
        # unit without a cap (a borrowed document-scale constant once starved
        # it to 48% live coverage) and prune deactivated runs' rows without
        # waiting for a full rebuild, because the public search view reads
        # the projection bare (no is_active join).
        suffix = os.urandom(4).hex()
        ids = self._seed_two_units(suffix)
        try:
            # One-run keyset batches exercise select -> run-atomic replace ->
            # cursor advance -> terminal exact-empty proof.
            with mock.patch.object(BuildSearchProjection, "_RUN_BATCH_SIZE", 1):
                result = BuildSearchProjection(engine=self.engine).execute(
                    BuildSearchProjectionCommand(full=False)
                )
            self.assertEqual(result.projected, 2)
            self.assertEqual(result.skipped, 0)
            self.assertEqual(self._projection_count(), 2)

            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.processing_run "
                        "SET is_active = false WHERE processing_run_id = :rid"
                    ),
                    {"rid": ids["run"]},
                )
            result = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            self.assertEqual(result.projected, 0)
            self.assertEqual(result.deleted, 2)
            self.assertEqual(self._projection_count(), 0)
        finally:
            self._cleanup(ids)

    def test_delta_restamps_stale_rules_version(self) -> None:
        # Pass 2 of the delta: rows stamped under an older retrieval rules
        # version are re-tokenized and re-stamped current, without a full
        # rebuild. This is the "edit rules -> delta refreshes" contract.
        suffix = os.urandom(4).hex()
        ids = self._seed_two_units(suffix)
        try:
            BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.unit_search_projection "
                        "SET retrieval_rules_version = 'rp-0000.00-0' "
                        "WHERE asset_id IN (:a, :b)"
                    ),
                    {"a": ids["title_hit"], "b": ids["body_hit"]},
                )
            result = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            self.assertEqual(result.projected, 2)
            self.assertEqual(result.deleted, 0)
            with self.engine.connect() as conn:
                versions = list(
                    conn.execute(
                        text(
                            "SELECT DISTINCT retrieval_rules_version "
                            "FROM disclosure_core.unit_search_projection "
                            "WHERE asset_id IN (:a, :b)"
                        ),
                        {"a": ids["title_hit"], "b": ids["body_hit"]},
                    ).scalars()
                )
            self.assertEqual(versions, [tokenizer.RETRIEVAL_RULES_VERSION])
        finally:
            self._cleanup(ids)

    def test_inactive_stale_rows_are_pruned_not_restamped(self) -> None:
        # Review finding 2026-07-24: a rules bump plus a supersede in the
        # same round leaves rows that are BOTH stale and inactive. They must
        # leave via the orphan prune (which runs first), never be
        # re-tokenized and re-stamped current by the stale pass.
        suffix = os.urandom(4).hex()
        ids_map = self._seed_two_units(suffix)
        try:
            BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.processing_run "
                        "SET is_active = false WHERE processing_run_id = :rid"
                    ),
                    {"rid": ids_map["run"]},
                )
                conn.execute(
                    text(
                        "UPDATE disclosure_core.unit_search_projection "
                        "SET retrieval_rules_version = 'rp-0000.00-0' "
                        "WHERE asset_id IN (:a, :b)"
                    ),
                    {"a": ids_map["title_hit"], "b": ids_map["body_hit"]},
                )
            result = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            self.assertEqual(result.projected, 0)
            self.assertEqual(result.deleted, 2)
            self.assertEqual(self._projection_count(), 0)
        finally:
            self._cleanup(ids_map)

    def test_delta_prune_gate_skips_scan_when_no_deactivations(self) -> None:
        # prune=False (the worker's quiet-round signal) must skip the
        # corpus-sized orphan scan; the count gate still forces it when the
        # projection provably exceeds the active set.
        suffix = os.urandom(4).hex()
        ids_map = self._seed_two_units(suffix)
        try:
            BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.processing_run "
                        "SET is_active = false WHERE processing_run_id = :rid"
                    ),
                    {"rid": ids_map["run"]},
                )
            # prune=False, projection(2) > active(0): count gate overrides
            # and the orphans still go.
            result = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False, prune=False)
            )
            self.assertEqual(result.deleted, 2)
            self.assertEqual(self._projection_count(), 0)
        finally:
            self._cleanup(ids_map)

    def test_clean_delta_round_returns_without_error(self) -> None:
        # Regression (2026-07-24): the refactor left ``deleted`` unbound on
        # the quiet delta path — prune gate closed (prune=False, no count
        # divergence), nothing missing, nothing stale — so the final return
        # raised UnboundLocalError. Every no-op worker round (published a
        # fresh doc, no supersede, projection already caught up) hit it.
        suffix = os.urandom(4).hex()
        ids_map = self._seed_two_units(suffix)
        try:
            first = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            self.assertEqual(first.projected, 2)
            # Second delta with prune disabled: caught up, no orphans, no
            # stale rows — must return cleanly, doing nothing.
            second = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False, prune=False)
            )
            self.assertEqual(second.projected, 0)
            self.assertEqual(second.deleted, 0)
            self.assertEqual(second.skipped, 0)
            self.assertEqual(self._projection_count(), 2)

            # A non-windowed parent with a leftover child is not a closed
            # projection. Delta must select the owning run and replace it,
            # otherwise replay status remains permanently pending.
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO disclosure_core.unit_body_search_window "
                        "(asset_id, window_index, body_token_start, "
                        "body_token_end, body_tokens) "
                        "VALUES (:asset_id, 0, 0, 1, 'stalechild')"
                    ),
                    {"asset_id": ids_map["title_hit"]},
                )
            repaired = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False, prune=False)
            )
            self.assertEqual(repaired.projected, 2)
            with self.engine.connect() as conn:
                stale_children = int(
                    conn.execute(
                        text(
                            "SELECT count(*) FROM "
                            "disclosure_core.unit_body_search_window "
                            "WHERE asset_id = :asset_id"
                        ),
                        {"asset_id": ids_map["title_hit"]},
                    ).scalar_one()
                )
            self.assertEqual(stale_children, 0)
        finally:
            self._cleanup(ids_map)

    def test_postgres_safety_probe_detects_every_physical_loss_mode(self) -> None:
        def unique_tokens(count: int) -> str:
            return " ".join(f"lexeme{index}" for index in range(count))

        candidates = {
            "repeat_255": "repeat " * 255,
            "repeat_256": "repeat " * 256,
            "positions_16383": unique_tokens(16_383),
            "positions_16384": unique_tokens(16_384),
            "long_lexeme": "x" * 2_100,
            "over_one_megabyte": unique_tokens(120_000),
        }
        with self.engine.connect() as conn:
            results = {
                name: bool(
                    conn.execute(
                        text(
                            "SELECT disclosure_core."
                            "search_tsvector_is_safe('', '', :body, '')"
                        ),
                        {"body": body},
                    ).scalar_one()
                )
                for name, body in candidates.items()
            }
        self.assertTrue(results["repeat_255"])
        self.assertFalse(results["repeat_256"])
        self.assertTrue(results["positions_16383"])
        self.assertFalse(results["positions_16384"])
        self.assertFalse(results["long_lexeme"])
        self.assertFalse(results["over_one_megabyte"])

    def test_windowed_body_is_lossless_and_and_groups_do_not_cross_assets(
        self,
    ) -> None:
        suffix = os.urandom(4).hex()
        ids_map = self._seed_two_units(suffix)
        ids_map["left_only"] = f"ua_sp_left_{suffix}"
        ids_map["right_only"] = f"ua_sp_right_{suffix}"
        unsafe_body = (
            "leftsentinel " + " ".join("repeat" for _ in range(256)) + " rightsentinel"
        )
        try:
            with Session(self.engine) as session:
                unsafe = session.get(DocumentUnit, ids_map["body_hit"])
                assert unsafe is not None
                unsafe.payload = {
                    "provider_type": "text",
                    "text": unsafe_body,
                }
                for asset_id, body, order_index in (
                    (ids_map["left_only"], "leftsentinel", 3),
                    (ids_map["right_only"], "rightsentinel", 4),
                ):
                    session.add(
                        DocumentUnit(
                            asset_id=asset_id,
                            document_id=ids_map["document"],
                            processing_run_id=ids_map["run"],
                            payload_kind="text",
                            heading_path=["窗口负例"],
                            title="窗口负例",
                            order_index=order_index,
                            payload={"provider_type": "text", "text": body},
                            content_hash=f"h_{asset_id}",
                            semantic_keys=[],
                            artifact_locator=_text_search_locator(order_index),
                        )
                    )
                session.commit()

            result = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=True)
            )
            self.assertEqual(result.projected, 4)
            expected_body_tokens = tokenizer.index_word_tokens(unsafe_body)
            with self.engine.connect() as conn:
                parent = conn.execute(
                    text(
                        "SELECT body_tokens, body_search_windowed "
                        "FROM disclosure_core.unit_search_projection "
                        "WHERE asset_id = :asset_id"
                    ),
                    {"asset_id": ids_map["body_hit"]},
                ).one()
                windows = conn.execute(
                    text(
                        "SELECT window_index, body_token_start, body_token_end, "
                        "body_tokens, "
                        "disclosure_core.search_tsvector_is_safe("
                        "'', '', body_tokens, '') AS safe "
                        "FROM disclosure_core.unit_body_search_window "
                        "WHERE asset_id = :asset_id ORDER BY window_index"
                    ),
                    {"asset_id": ids_map["body_hit"]},
                ).all()
            self.assertEqual(parent.body_tokens, expected_body_tokens)
            self.assertTrue(parent.body_search_windowed)
            self.assertGreaterEqual(len(windows), 2)
            cursor = 0
            reconstructed: list[str] = []
            for window in windows:
                self.assertEqual(window.body_token_start, cursor)
                self.assertGreater(window.body_token_end, window.body_token_start)
                self.assertTrue(window.safe)
                reconstructed.extend(str(window.body_tokens).split())
                cursor = int(window.body_token_end)
            self.assertEqual(
                reconstructed,
                expected_body_tokens.split(),
            )
            self.assertEqual(cursor, len(expected_body_tokens.split()))

            groups = tokenizer.build_search_tsquery_groups("leftsentinel rightsentinel")
            self.assertEqual(len(groups), 2)
            with self.engine.connect() as conn:
                hits = list(
                    conn.execute(
                        text(
                            """
                            WITH query_groups(group_id, query_text) AS (
                                VALUES (0, :group_0), (1, :group_1)
                            ),
                            hits AS (
                                SELECT p.asset_id, q.group_id
                                  FROM disclosure_public.unit_search_projection_v1 p
                                  CROSS JOIN query_groups q
                                 WHERE p.search_tsv
                                       @@ to_tsquery('simple', q.query_text)
                                UNION
                                SELECT w.asset_id, q.group_id
                                  FROM disclosure_public.unit_body_search_windows_v1 w
                                  CROSS JOIN query_groups q
                                 WHERE w.search_tsv
                                       @@ to_tsquery('simple', q.query_text)
                            )
                            SELECT asset_id
                              FROM hits
                             GROUP BY asset_id
                            HAVING count(DISTINCT group_id) = 2
                             ORDER BY asset_id
                            """
                        ),
                        {"group_0": groups[0], "group_1": groups[1]},
                    ).scalars()
                )
            self.assertEqual(hits, [ids_map["body_hit"]])

            with self.engine.begin() as conn:
                conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
                plan = "\n".join(
                    str(row[0])
                    for row in conn.execute(
                        text(
                            "EXPLAIN SELECT asset_id FROM "
                            "disclosure_core.unit_body_search_window "
                            "WHERE search_tsv @@ to_tsquery('simple', :query)"
                        ),
                        {"query": groups[0]},
                    )
                )
            self.assertIn("ix_unit_body_search_window_tsv", plan)
            refused = self._alembic("downgrade", "0027_materialized_classification")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn(
                "cannot downgrade while windowed search projections exist",
                refused.stderr,
            )
            self.assertTrue(self._view_exists("unit_body_search_windows_v1"))
        finally:
            self._cleanup(ids_map)

    def test_child_insert_failure_rolls_back_the_complete_run(self) -> None:
        suffix = os.urandom(4).hex()
        ids_map = self._seed_two_units(suffix)
        try:
            BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=True)
            )
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.unit_search_projection "
                        "SET retrieval_rules_version = 'rp-before-failure' "
                        "WHERE asset_id IN (:a, :b)"
                    ),
                    {
                        "a": ids_map["title_hit"],
                        "b": ids_map["body_hit"],
                    },
                )
                before = conn.execute(
                    text(
                        "SELECT asset_id, retrieval_rules_version, built_at "
                        "FROM disclosure_core.unit_search_projection "
                        "WHERE asset_id IN (:a, :b) ORDER BY asset_id"
                    ),
                    {
                        "a": ids_map["title_hit"],
                        "b": ids_map["body_hit"],
                    },
                ).all()
                before_atoms = conn.execute(
                    text(
                        "SELECT asset_id, atom_index, atom_text "
                        "FROM disclosure_core.unit_search_atom "
                        "WHERE asset_id IN (:a, :b) "
                        "ORDER BY asset_id, atom_index"
                    ),
                    {
                        "a": ids_map["title_hit"],
                        "b": ids_map["body_hit"],
                    },
                ).all()

            with mock.patch.object(
                projection_module,
                "_prepare_search_rows",
                side_effect=SearchProjectionSafetyError(
                    "search_projection_unsplittable_body_token: "
                    f"asset_id={ids_map['body_hit']}"
                ),
            ):
                deterministic = BuildSearchProjection(engine=self.engine).execute(
                    BuildSearchProjectionCommand(full=False)
                )
            self.assertEqual(deterministic.projected, 0)
            self.assertEqual(len(deterministic.failures), 1)
            self.assertEqual(
                deterministic.failures[0].processing_run_id,
                ids_map["run"],
            )
            self.assertEqual(
                deterministic.failures[0].error_code,
                "search_projection_unsplittable_body_token",
            )
            with self.engine.connect() as conn:
                stored_error = conn.execute(
                    text(
                        "SELECT search_projection_error "
                        "FROM disclosure_core.processing_run "
                        "WHERE processing_run_id = :run"
                    ),
                    {"run": ids_map["run"]},
                ).scalar_one()
            self.assertEqual(stored_error["retryable"], False)
            self.assertEqual(
                stored_error["retrieval_rules_version"],
                tokenizer.RETRIEVAL_RULES_VERSION,
            )
            with self.engine.connect() as conn:
                transaction = conn.begin()
                try:
                    with (
                        self.assertRaises(sqlalchemy.exc.IntegrityError),
                        conn.begin_nested(),
                    ):
                        conn.execute(
                            text(
                                "UPDATE disclosure_core.processing_run "
                                "SET search_projection_error = "
                                '\'{"stage":"search_projection"}\'::jsonb '
                                "WHERE processing_run_id = :run"
                            ),
                            {"run": ids_map["run"]},
                        )
                    with (
                        self.assertRaises(sqlalchemy.exc.IntegrityError),
                        conn.begin_nested(),
                    ):
                        conn.execute(
                            text(
                                "UPDATE disclosure_core.processing_run "
                                "SET parser_target_identity = '[]'::jsonb "
                                "WHERE processing_run_id = :run"
                            ),
                            {"run": ids_map["run"]},
                        )
                finally:
                    transaction.rollback()
            with self.engine.connect() as conn:
                after_deterministic_failure = conn.execute(
                    text(
                        "SELECT asset_id, retrieval_rules_version, built_at "
                        "FROM disclosure_core.unit_search_projection "
                        "WHERE asset_id IN (:a, :b) ORDER BY asset_id"
                    ),
                    {
                        "a": ids_map["title_hit"],
                        "b": ids_map["body_hit"],
                    },
                ).all()
            self.assertEqual(after_deterministic_failure, before)
            with self.engine.connect() as conn:
                after_failure_atoms = conn.execute(
                    text(
                        "SELECT asset_id, atom_index, atom_text "
                        "FROM disclosure_core.unit_search_atom "
                        "WHERE asset_id IN (:a, :b) "
                        "ORDER BY asset_id, atom_index"
                    ),
                    {
                        "a": ids_map["title_hit"],
                        "b": ids_map["body_hit"],
                    },
                ).all()
            self.assertEqual(after_failure_atoms, before_atoms)

            # Delta does not spin on a current terminal fact. Full is the
            # explicit retry path, and success clears the fact atomically.
            skipped = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            self.assertEqual(skipped.projected, 0)
            self.assertEqual(skipped.failures, ())
            with Session(self.engine) as session:
                run = session.get(ProcessingRun, ids_map["run"])
                assert run is not None
                run.search_projection_error = {
                    **stored_error,
                    "retrieval_rules_version": "retrieval.old",
                }
                session.commit()
            rules_changed = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=False)
            )
            self.assertEqual(rules_changed.projected, 2)
            with Session(self.engine) as session:
                run = session.get(ProcessingRun, ids_map["run"])
                assert run is not None
                self.assertIsNone(run.search_projection_error)

            # Full mode retries even a current terminal fact without a gap.
            with Session(self.engine) as session:
                run = session.get(ProcessingRun, ids_map["run"])
                assert run is not None
                run.search_projection_error = stored_error
                session.commit()
            recovered = BuildSearchProjection(engine=self.engine).execute(
                BuildSearchProjectionCommand(full=True)
            )
            self.assertEqual(recovered.projected, 2)
            with self.engine.connect() as conn:
                cleared_error = conn.execute(
                    text(
                        "SELECT search_projection_error "
                        "FROM disclosure_core.processing_run "
                        "WHERE processing_run_id = :run"
                    ),
                    {"run": ids_map["run"]},
                ).scalar_one()
            self.assertIsNone(cleared_error)

            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE disclosure_core.unit_search_projection "
                        "SET retrieval_rules_version = 'rp-before-failure' "
                        "WHERE asset_id IN (:a, :b)"
                    ),
                    {
                        "a": ids_map["title_hit"],
                        "b": ids_map["body_hit"],
                    },
                )
                before = conn.execute(
                    text(
                        "SELECT asset_id, retrieval_rules_version, built_at "
                        "FROM disclosure_core.unit_search_projection "
                        "WHERE asset_id IN (:a, :b) ORDER BY asset_id"
                    ),
                    {
                        "a": ids_map["title_hit"],
                        "b": ids_map["body_hit"],
                    },
                ).all()
                before_atoms = conn.execute(
                    text(
                        "SELECT asset_id, atom_index, atom_text "
                        "FROM disclosure_core.unit_search_atom "
                        "WHERE asset_id IN (:a, :b) "
                        "ORDER BY asset_id, atom_index"
                    ),
                    {
                        "a": ids_map["title_hit"],
                        "b": ids_map["body_hit"],
                    },
                ).all()

            def invalid_child(
                session: Session,
                rows: list[dict[str, object]],
            ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
                del session
                prepared = [{**row, "body_search_windowed": True} for row in rows]
                return prepared, [
                    {
                        "asset_id": prepared[0]["asset_id"],
                        "window_index": 0,
                        "body_token_start": 0,
                        "body_token_end": 1,
                        "body_tokens": "",
                    }
                ]

            with (
                mock.patch.object(
                    projection_module,
                    "_prepare_search_rows",
                    side_effect=invalid_child,
                ),
                self.assertRaises(sqlalchemy.exc.IntegrityError),
            ):
                BuildSearchProjection(engine=self.engine).execute(
                    BuildSearchProjectionCommand(full=False)
                )

            with self.engine.connect() as conn:
                after = conn.execute(
                    text(
                        "SELECT asset_id, retrieval_rules_version, built_at "
                        "FROM disclosure_core.unit_search_projection "
                        "WHERE asset_id IN (:a, :b) ORDER BY asset_id"
                    ),
                    {
                        "a": ids_map["title_hit"],
                        "b": ids_map["body_hit"],
                    },
                ).all()
                child_count = int(
                    conn.execute(
                        text(
                            "SELECT count(*) FROM "
                            "disclosure_core.unit_body_search_window "
                            "WHERE asset_id IN (:a, :b)"
                        ),
                        {
                            "a": ids_map["title_hit"],
                            "b": ids_map["body_hit"],
                        },
                    ).scalar_one()
                )
                after_atoms = conn.execute(
                    text(
                        "SELECT asset_id, atom_index, atom_text "
                        "FROM disclosure_core.unit_search_atom "
                        "WHERE asset_id IN (:a, :b) "
                        "ORDER BY asset_id, atom_index"
                    ),
                    {
                        "a": ids_map["title_hit"],
                        "b": ids_map["body_hit"],
                    },
                ).all()
            self.assertEqual(after, before)
            self.assertEqual(child_count, 0)
            self.assertEqual(after_atoms, before_atoms)
        finally:
            self._cleanup(ids_map)

    # -- seeding / queries / cleanup ---------------------------------------
    def _seed_two_units(self, suffix: str) -> dict[str, str]:
        ids = {
            "company": f"co_sp_{suffix}",
            "security": f"sec_sp_{suffix}",
            "document": f"doc_sp_{suffix}",
            "run": f"run_sp_{suffix}",
            "title_hit": f"ua_sp_title_{suffix}",
            "body_hit": f"ua_sp_body_{suffix}",
        }
        with Session(self.engine) as session:
            # Flush each parent before its children so the FK targets exist
            # (these models carry no ORM relationships to auto-order inserts).
            session.add(
                Company(company_id=ids["company"], legal_name=f"投影测试公司{suffix}")
            )
            session.flush()
            session.add(
                Security(
                    security_id=ids["security"],
                    company_id=ids["company"],
                    security_code="600000",
                    exchange="SSE",
                )
            )
            session.flush()
            session.add(
                Document(
                    document_id=ids["document"],
                    company_id=ids["company"],
                    security_id=ids["security"],
                    provider="cninfo",
                    provider_document_id=f"T{suffix}",
                    status="published",
                    provider_metadata={},
                )
            )
            session.flush()
            session.add(
                ProcessingRun(
                    processing_run_id=ids["run"],
                    document_id=ids["document"],
                    artifact_owner_processing_run_id=ids["run"],
                    run_kind="parse",
                    status="succeeded",
                    is_active=True,
                    unit_build_status="succeeded",
                    provider_document_relpath=(
                        "derived/provider_documents/cninfo/600000/"
                        f"T{suffix}/{ids['run']}/provider_document.v1.json"
                    ),
                )
            )
            session.flush()
            session.add(
                DocumentUnit(
                    asset_id=ids["title_hit"],
                    document_id=ids["document"],
                    processing_run_id=ids["run"],
                    payload_kind="text",
                    heading_path=[f"财务附注{suffix}", f"账龄{suffix}分析"],
                    title="应收账款",
                    order_index=1,
                    payload={"provider_type": "text", "text": "期末余额说明"},
                    content_hash=f"h1_{suffix}",
                    semantic_keys=["receivable_aging"],
                    artifact_locator=_text_search_locator(1),
                )
            )
            session.add(
                DocumentUnit(
                    asset_id=ids["body_hit"],
                    document_id=ids["document"],
                    processing_run_id=ids["run"],
                    payload_kind="text",
                    heading_path=[f"减值损失{suffix}"],
                    title="坏账准备",
                    order_index=2,
                    payload={"provider_type": "text", "text": "应收账款"},
                    content_hash=f"h2_{suffix}",
                    semantic_keys=["credit_impairment_loss"],
                    artifact_locator=_text_search_locator(2),
                )
            )
            session.commit()
        return ids

    def _cleanup(self, ids: dict[str, str]) -> None:
        with self.engine.begin() as conn:
            # unit_search_projection cascades on document_unit delete (FK ON
            # DELETE CASCADE), but delete it explicitly first for clarity.
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.unit_search_projection "
                    "WHERE asset_id IN (:a, :b)"
                ),
                {"a": ids["title_hit"], "b": ids["body_hit"]},
            )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.document_unit WHERE document_id = :did"
                ),
                {"did": ids["document"]},
            )
            conn.execute(
                text(
                    "DELETE FROM disclosure_core.processing_run "
                    "WHERE document_id = :did"
                ),
                {"did": ids["document"]},
            )
            conn.execute(
                text("DELETE FROM disclosure_core.document WHERE document_id = :did"),
                {"did": ids["document"]},
            )
            conn.execute(
                text("DELETE FROM disclosure_core.security WHERE security_id = :sid"),
                {"sid": ids["security"]},
            )
            conn.execute(
                text("DELETE FROM disclosure_core.company WHERE company_id = :cid"),
                {"cid": ids["company"]},
            )

    def _ranked_asset_ids(self, term: str) -> list[str]:
        query = " ".join(tokenizer.query_word_tokens(term))
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT asset_id FROM disclosure_core.unit_search_projection "
                    "WHERE search_tsv @@ plainto_tsquery('simple', :q) "
                    "ORDER BY ts_rank(search_tsv, plainto_tsquery('simple', :q)) "
                    "DESC, asset_id"
                ),
                {"q": query},
            ).scalars()
            return list(rows)

    _ATOM_QUERY_SQL = r"""
        WITH q AS (
            SELECT CAST(:normalized_query AS text) AS normalized_query
        )
        SELECT DISTINCT a.asset_id
          FROM disclosure_public.unit_search_atoms_v1 a
          CROSS JOIN q
         WHERE char_length(q.normalized_query) >= 3
           AND a.atom_text LIKE (
                   '%' ||
                   replace(
                       replace(
                           replace(q.normalized_query, '\', '\\'),
                           '%',
                           '\%'
                       ),
                       '_',
                       '\_'
                   )
                   || '%'
               ) ESCAPE '\'
           AND strpos(a.atom_text, q.normalized_query) > 0
         ORDER BY a.asset_id
    """

    _CANDIDATE_QUERY_SQL = r"""
        WITH input AS (
            SELECT CAST(:normalized_query AS text) AS atom_query,
                   CAST(:query_groups AS text[]) AS word_groups
        ),
        groups AS (
            SELECT ordinality AS group_id, query_text
              FROM input,
                   unnest(word_groups) WITH ORDINALITY
                       AS g(query_text, ordinality)
        ),
        word_group_hits AS (
            SELECT p.asset_id, g.group_id
              FROM disclosure_public.unit_search_projection_v1 p
              CROSS JOIN groups g
             WHERE p.search_tsv
                   @@ to_tsquery('simple', g.query_text)
            UNION
            SELECT w.asset_id, g.group_id
              FROM disclosure_public.unit_body_search_windows_v1 w
              CROSS JOIN groups g
             WHERE w.search_tsv
                   @@ to_tsquery('simple', g.query_text)
        ),
        word_hits AS (
            SELECT asset_id
              FROM word_group_hits
             GROUP BY asset_id
            HAVING count(DISTINCT group_id) = (
                       SELECT count(*) FROM groups
                   )
               AND (SELECT count(*) FROM groups) > 0
        ),
        atom_hits AS (
            SELECT DISTINCT a.asset_id
              FROM disclosure_public.unit_search_atoms_v1 a
              CROSS JOIN input i
             WHERE char_length(i.atom_query) >= 3
               AND a.atom_text LIKE (
                       '%' ||
                       replace(
                           replace(
                               replace(i.atom_query, '\', '\\'),
                               '%',
                               '\%'
                           ),
                           '_',
                           '\_'
                       )
                       || '%'
                   ) ESCAPE '\'
               AND strpos(a.atom_text, i.atom_query) > 0
        )
        SELECT asset_id FROM word_hits
        UNION
        SELECT asset_id FROM atom_hits
        ORDER BY asset_id
    """

    def _atom_hits(self, query: str) -> list[str]:
        normalized_query = tokenizer.normalize_search_text(query)
        with self.engine.connect() as conn:
            return list(
                conn.execute(
                    text(self._ATOM_QUERY_SQL),
                    {"normalized_query": normalized_query},
                ).scalars()
            )

    def _candidate_hits(self, query: str) -> list[str]:
        with self.engine.connect() as conn:
            return list(
                conn.execute(
                    text(self._CANDIDATE_QUERY_SQL),
                    {
                        "normalized_query": (tokenizer.normalize_search_text(query)),
                        "query_groups": list(
                            tokenizer.build_search_tsquery_groups(query)
                        ),
                    },
                ).scalars()
            )

    def _trgm_hits(self, substring: str) -> list[str]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT asset_id FROM disclosure_core.unit_search_projection "
                    "WHERE heading_path_text LIKE :pat ORDER BY asset_id"
                ),
                {"pat": f"%{substring}%"},
            ).scalars()
            return list(rows)

    def _active_unit_count(self) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        "SELECT count(*) FROM disclosure_core.document_unit du "
                        "JOIN disclosure_core.processing_run pr "
                        "ON pr.processing_run_id = du.processing_run_id "
                        "WHERE pr.is_active"
                    )
                ).scalar_one()
            )

    def _projection_count(self) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT count(*) FROM disclosure_core.unit_search_projection")
                ).scalar_one()
            )

    def _table_exists(self, name: str) -> bool:
        with self.engine.connect() as conn:
            return bool(
                conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'disclosure_core' AND table_name = :n"
                    ),
                    {"n": name},
                ).scalar()
            )

    def _view_exists(self, name: str) -> bool:
        with self.engine.connect() as conn:
            return bool(
                conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.views "
                        "WHERE table_schema = 'disclosure_public' AND table_name = :n"
                    ),
                    {"n": name},
                ).scalar()
            )

    def _function_exists(self, name: str) -> bool:
        with self.engine.connect() as conn:
            return bool(
                conn.execute(
                    text(
                        "SELECT 1 FROM pg_proc AS routine "
                        "JOIN pg_namespace AS namespace "
                        "ON namespace.oid = routine.pronamespace "
                        "WHERE namespace.nspname = 'disclosure_core' "
                        "AND routine.proname = :name"
                    ),
                    {"name": name},
                ).scalar()
            )

    def _view_columns(self, name: str) -> list[str]:
        with self.engine.connect() as conn:
            return list(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'disclosure_public' "
                        "AND table_name = :name ORDER BY ordinal_position"
                    ),
                    {"name": name},
                ).scalars()
            )


if __name__ == "__main__":
    unittest.main()
