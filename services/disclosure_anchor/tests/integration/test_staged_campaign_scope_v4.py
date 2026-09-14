"""C1 scope behavior on the original managed scratch PostgreSQL runner.

Synthetic documents and existing sealed V4 fixtures exercise real production
queries and persistence APIs. No SQLite shim, query mock, provider or PDF load
is used; these tests do not claim publication or formal M6 qualification.
"""

from __future__ import annotations

import unittest

import sqlalchemy as sa
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres.remote_parse_v4_repository import (
    RemoteParseV4Repository,
)
from disclosure_anchor.adapters.db.postgres.staged_new_work_v4 import (
    PostgresV4OrdinaryParseCandidateSource,
    require_campaign_recovery_scope,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.application.contracts.m6_campaign import (
    M6CampaignScope,
    M6CorpusEntry,
)
from disclosure_anchor.application.contracts.staged_campaign_v4 import (
    V4CampaignAdmissionScope,
    V4CampaignScopeViolation,
)
from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import (
    DurableStagedCoordinatorPersistenceV4,
)
from disclosure_anchor.domain import ids
from tests import m6_support as m6
from tests.integration import test_staged_coordinator_persistence_v4 as persistence_fixture
from tests.integration._remote_parse_v4_factory import (
    build_v4_authority_fixture,
    install_prepared_cycle,
    install_submitted_cycle,
)


def campaign(pairs, *, carry_in=False):
    manifest = m6.manifest(
        "e2e_publication",
        *(M6CorpusEntry(
            document_id=document,
            source_pdf_sha256=source,
            source_byte_count=100,
            source_page_count=2,
            stratum="native",
            origin="carry_in" if carry_in else "fresh",
        ) for document, source in pairs),
    )
    return V4CampaignAdmissionScope(
        scope=M6CampaignScope.from_manifest(manifest), manifest=manifest,
    )


class StagedCampaignScopeV4IntegrationTests(unittest.TestCase):
    def setUp(self):
        # Composition, not inheritance: no existing TestCase methods are
        # rediscovered. Its setUp calls engine_or_skip, which validates the
        # pinned scratch URL and managed DB marker before any fixture write.
        fixture = persistence_fixture.StagedCoordinatorPersistenceV4IntegrationTests()
        fixture.setUp()
        self.engine = fixture.engine
        self.addCleanup(fixture.tearDown)
        self.ordinary_ids = []
        self.addCleanup(self._delete_ordinary_rows)
        self.suffix = ids.new_ulid().lower()

    def _delete_ordinary_rows(self):
        if self.ordinary_ids:
            with self.engine.begin() as connection:
                connection.execute(sa.text(
                    "DELETE FROM disclosure_core.document WHERE document_id=ANY(:ids)"
                ), {"ids": self.ordinary_ids})

    def _head_snapshot(self):
        with self.engine.connect() as connection:
            return tuple(connection.execute(sa.text(
                "SELECT attempt_id,document_id,source_pdf_sha256,state,row_version,"
                "claim_generation,claim_owner_identity,claim_lease_until,"
                "current_checkpoint_sha256,updated_at "
                "FROM disclosure_ops.remote_parse_attempt ORDER BY attempt_id COLLATE \"C\""
            )))

    def test_ordinary_scope_filters_before_limit_beyond_legacy_eight(self):
        first = build_v4_authority_fixture()
        prefix = "doc_zzc1_" + self.suffix + "_"
        outside = [(prefix + "a" + str(i), m6.digest("outside-" + str(i))) for i in range(3)]
        selected = [(prefix + "z" + str(i), m6.digest("selected-" + str(i))) for i in range(9)]
        self.ordinary_ids = [first.document_id, *(document for document, _ in outside + selected)]
        with self.engine.begin() as connection:
            security = persistence_fixture.StagedCoordinatorPersistenceV4IntegrationTests._insert_ingress_document(
                connection, first,
            )
            for document, source in outside + selected:
                connection.execute(sa.text(
                    "INSERT INTO disclosure_core.document "
                    "(document_id,security_id,provider,provider_document_id,"
                    "raw_file_relpath,raw_file_hash,status) VALUES "
                    "(:document,:security,'cninfo',:provider_document,"
                    "'raw/c1-scratch.pdf',:source,'registered')"
                ), {"document": document, "security": security,
                    "provider_document": document, "source": source})

        unscoped = PostgresV4OrdinaryParseCandidateSource(
            engine=self.engine, max_retries=3, scope_classes=None,
        )
        self.assertEqual(
            [item.document_id for item in unscoped.list_candidates(
                after_document_id=prefix, limit=2,
            ).candidates],
            [document for document, _ in outside[:2]],
        )
        scope = campaign(selected)
        source = PostgresV4OrdinaryParseCandidateSource(
            engine=self.engine, max_retries=3, scope_classes=None, campaign_scope=scope,
        )
        observed = []
        cursor = prefix
        for length, more in ((2, True), (2, True), (2, True), (2, True), (1, False)):
            page = source.list_candidates(after_document_id=cursor, limit=2)
            self.assertEqual((len(page.candidates), page.has_more), (length, more))
            observed.extend((item.document_id, item.raw_file_hash) for item in page.candidates)
            cursor = page.candidates[-1].document_id
        self.assertEqual(observed, selected)
        self.assertEqual(source.list_candidates(after_document_id=cursor, limit=2).candidates, ())
        carry_in = PostgresV4OrdinaryParseCandidateSource(
            engine=self.engine, max_retries=3, scope_classes=None,
            campaign_scope=campaign(selected, carry_in=True),
        )
        empty = carry_in.list_candidates(after_document_id=None, limit=2)
        self.assertEqual((empty.candidates, empty.has_more), ((), False))

    def test_prepared_scope_filters_before_limit_and_does_not_claim(self):
        prefix = "rpa_c1_" + self.suffix + "_"
        outsiders = [build_v4_authority_fixture(attempt_id=prefix + "a" + str(i)) for i in range(3)]
        selected = [build_v4_authority_fixture(attempt_id=prefix + "z" + str(i)) for i in range(9)]
        with self.engine.begin() as connection:
            for fixture in outsiders + selected:
                install_prepared_cycle(connection, fixture)
        scope = campaign([(fixture.document_id, fixture.source_pdf_sha256) for fixture in selected])
        before = self._head_snapshot()
        with Session(self.engine, expire_on_commit=False) as session:
            repository = RemoteParseV4Repository(session)
            unscoped = repository.list_unclaimed_prepared_heads(after_attempt_id=None, limit=2)
            self.assertEqual([item.attempt_id for item in unscoped], [f.attempt_id for f in outsiders[:2]])
            cursor = None
            observed = []
            for length in (2, 2, 2, 2, 1):
                page = repository.list_unclaimed_prepared_heads(
                    after_attempt_id=cursor, limit=2, campaign_scope=scope,
                )
                self.assertEqual(len(page), length)
                self.assertTrue(all(item.state == "prepared" and item.claim_owner_identity is None for item in page))
                observed.extend(item.attempt_id for item in page)
                cursor = page[-1].attempt_id
            self.assertEqual(observed, [fixture.attempt_id for fixture in selected])
            self.assertEqual(repository.list_unclaimed_prepared_heads(
                after_attempt_id=cursor, limit=2, campaign_scope=scope,
            ), ())
        self.assertEqual(self._head_snapshot(), before)

    def test_global_pair_guard_rejects_foreign_owner_without_hiding_recovery(self):
        prefix = "rpa_c1_" + self.suffix + "_"
        outside = build_v4_authority_fixture(attempt_id=prefix + "a-outside")
        prepared = build_v4_authority_fixture(attempt_id=prefix + "b-prepared")
        accepted = build_v4_authority_fixture(attempt_id=prefix + "c-accepted")
        fixtures = [outside, prepared, accepted]
        with self.engine.begin() as connection:
            install_prepared_cycle(connection, outside)
            install_prepared_cycle(connection, prepared)
            install_submitted_cycle(connection, accepted, include_secret=True)
        before = self._head_snapshot()
        pairs = [(f.document_id, f.source_pdf_sha256) for f in fixtures]
        complete = campaign(pairs, carry_in=True)
        require_campaign_recovery_scope(self.engine, complete)
        restricted = campaign(pairs[1:], carry_in=True)
        swapped = campaign([
            (outside.document_id, prepared.source_pdf_sha256),
            (prepared.document_id, outside.source_pdf_sha256),
            (accepted.document_id, accepted.source_pdf_sha256),
        ], carry_in=True)
        # Swapping sources keeps both complete membership sets, but violates
        # the required document/source PAIRS in PostgreSQL's zipped unnest.
        for scope in (restricted, swapped):
            with self.subTest(scope=scope.scope_sha256), self.assertRaises(V4CampaignScopeViolation):
                require_campaign_recovery_scope(self.engine, scope)
        persistence = DurableStagedCoordinatorPersistenceV4(
            uow_factory=unit_of_work_factory(self.engine),
            limits=persistence_fixture._limits(),
            owner_identity="c1-independent-scratch-owner",
            campaign_scope=restricted,
        )
        # No process_guard is installed here: test the persistence API's
        # independent complete scan and pre-claim source check. The builder's
        # global effect-guard wiring is covered by its existing separate test.
        recovered = persistence.list_recoverable(after_attempt_id=None, limit=10)
        self.assertEqual([item.attempt_id for item in recovered], [f.attempt_id for f in fixtures])
        self.assertEqual(recovered[-1].claim_owner_identity, "worker-test")
        with self.assertRaises(V4CampaignScopeViolation):
            persistence.claim_recovery(recovered[0])
        self.assertEqual(self._head_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
