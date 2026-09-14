import json
import unittest

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres.remote_parse_v4_repository import RemoteParseV4Repository
from disclosure_anchor.adapters.db.postgres.staged_new_work_v4 import (
    PostgresV4OrdinaryParseCandidateSource, require_campaign_recovery_scope,
)
from disclosure_anchor.application.contracts.staged_campaign_v4 import V4CampaignScopeViolation
from tests import m6_support as m6
from tests._staged_campaign_sql import CampaignSqlDatabase, campaign


class StagedCampaignSqlTests(unittest.TestCase):
    def setUp(self):
        self.db = CampaignSqlDatabase()
        self.addCleanup(self.db.close)
        self.scope = campaign()

    def source(self, scope):
        return PostgresV4OrdinaryParseCandidateSource(engine=self.db.engine, max_retries=3,
                    scope_classes=None, campaign_scope=scope)

    def test_actual_ordinary_query_filters_twenty_earlier_outsiders_before_limit(self):
        for i in range(20):
            self.db.add_document(f"doc-before-{i:02}", m6.digest(f"outside-{i}"))
        for document, source in zip(self.scope.document_ids, self.scope.source_hashes):
            self.db.add_document(document, source)
        self.assertEqual(self.source(None).list_candidates(after_document_id=None, limit=2).candidates[0].document_id,
                         "doc-before-00")
        source = self.source(self.scope)
        found = []
        cursor = None
        for _ in range(6):
            page = source.list_candidates(after_document_id=cursor, limit=2)
            found.extend(item.document_id for item in page.candidates)
            cursor = page.candidates[-1].document_id
        self.assertEqual(tuple(found), self.scope.document_ids)
        self.assertFalse(page.has_more)
        self.assertEqual(source.campaign_scope_sha256, self.scope.scope_sha256)
        membership_queries = [(sql, params) for sql, params in self.db.statements if "pending_parse_v1 q" in sql and "ANY" in sql]
        self.assertEqual(len(membership_queries), 6)
        for _sql, params in membership_queries:
            self.assertIn(list(self.scope.document_ids), [json.loads(p) for p in params if isinstance(p,str) and p.startswith("[")])

    def test_ordinary_reloads_source_and_rejects_changed_sha_with_zero_writes(self):
        self.db.add_document(self.scope.document_ids[0], m6.digest("changed-source"))
        before = self.db.total_changes()
        with self.assertRaises(V4CampaignScopeViolation):
            self.source(self.scope).list_candidates(after_document_id=None, limit=2)
        self.assertEqual(self.db.total_changes(), before)

    def test_all_carry_in_has_no_ordinary_query_and_does_not_mean_unscoped(self):
        scope = campaign(carry_in=True)
        self.db.add_document(scope.document_ids[0], scope.source_hashes[0])
        self.db.statements.clear()
        page = self.source(scope).list_candidates(after_document_id=None, limit=2)
        self.assertEqual(page.candidates, ())
        self.assertFalse(page.has_more)
        self.assertEqual(self.db.statements, [])

    def test_actual_prepared_select_filters_scope_before_limit_and_preserves_clock(self):
        for i in range(20):
            self.db.add_head(f"attempt-before-{i:02}", f"doc-before-{i:02}", m6.digest(f"outside-{i}"))
        for i, (document, source) in enumerate(zip(self.scope.document_ids, self.scope.source_hashes)):
            self.db.add_head(f"attempt-selected-{i:02}", document, source)
        statements = []

        class RecordingSession(Session):
            def execute(inner, statement, *args, **kwargs):
                statements.append(statement)
                return super().execute(statement, *args, **kwargs)

        with RecordingSession(self.db.engine) as session:
            repository = RemoteParseV4Repository(session)
            found = []
            cursor = None
            for _ in range(6):
                page = repository.list_unclaimed_prepared_heads(after_attempt_id=cursor, limit=2, campaign_scope=self.scope)
                found.extend(item.attempt_id for item in page)
                cursor = page[-1].attempt_id
            self.assertEqual(found, [f"attempt-selected-{i:02}" for i in range(12)])
            self.assertEqual(repository.list_unclaimed_prepared_heads(after_attempt_id=cursor, limit=2, campaign_scope=self.scope), ())
        for statement in statements:
            compiled = statement.compile(dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True})
            self.assertEqual({v for v in compiled.params.values() if isinstance(v,str) and v.startswith("doc-")},
                             set(self.scope.document_ids))

    def test_global_guard_checks_paired_document_source_for_every_unresolved_state(self):
        states = ("prepared", "reconciling", "submitted", "remote_terminal", "materializing",
                  "local_materialized", "publish_committed", "cleanup_pending", "ack_pending")
        for state in states:
            for wrong_document in (False, True):
                with self.subTest(state=state, wrong_document=wrong_document):
                    with self.db.engine.begin() as c:
                        c.exec_driver_sql("DELETE FROM disclosure_ops.remote_parse_attempt")
                    document = "outside" if wrong_document else self.scope.document_ids[0]
                    source = self.scope.source_hashes[0] if wrong_document else self.scope.source_hashes[1]
                    self.db.add_head("outside-responsibility", document, source, state=state)
                    before = self.db.total_changes()
                    with self.assertRaises(V4CampaignScopeViolation):
                        require_campaign_recovery_scope(self.db.engine, self.scope)
                    self.assertEqual(self.db.total_changes(), before)

    def test_global_guard_allows_exact_carry_in_and_ignores_only_noncurrent_or_final(self):
        scope = campaign(carry_in=True)
        self.db.add_head("carry-in", scope.document_ids[0], scope.source_hashes[0], state="submitted", claimed=True)
        self.db.add_head("historical", "outside", m6.digest("old"), current=False)
        self.db.add_head("finished", "outside", m6.digest("done"), state="acked")
        before = self.db.total_changes()
        require_campaign_recovery_scope(self.db.engine, scope)
        self.assertEqual(self.db.total_changes(), before)

    def test_global_guard_database_error_is_visible(self):
        with self.db.engine.begin() as c:
            c.exec_driver_sql("DROP TABLE disclosure_ops.remote_parse_attempt")
        with self.assertRaises(sa.exc.DatabaseError):
            require_campaign_recovery_scope(self.db.engine, self.scope)


if __name__ == "__main__":
    unittest.main()
