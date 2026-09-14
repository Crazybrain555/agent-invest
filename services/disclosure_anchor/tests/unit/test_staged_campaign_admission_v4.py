from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest import mock

from disclosure_anchor.application.contracts.staged_campaign_v4 import (
    V4CampaignAdmissionScope, V4CampaignScopeViolation, require_v4_campaign_scope,
)
from disclosure_anchor.application.ports.staged_new_work_v4 import validate_v4_admission_scope
from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import DurableStagedCoordinatorPersistenceV4
from disclosure_anchor.application.services.staged_new_work_admission_v4 import StagedV4NewWorkAdmitter
from tests import m6_support as m6
from tests._staged_campaign_sql import NOW, campaign
from tests.unit import test_staged_coordinator_persistence_v4 as persistence_fixture
from tests.unit import test_staged_new_work_admission_v4 as admission_fixture


class StagedCampaignContractTests(unittest.TestCase):
    def test_scope_manifest_pair_is_immutable_and_bad_binding_never_becomes_none(self):
        value = campaign()
        self.assertIs(require_v4_campaign_scope(value), value)
        self.assertEqual(len(value.document_ids), 12)
        self.assertEqual(dict(zip(value.document_ids, value.source_hashes)),
                         {e.document_id: e.source_pdf_sha256 for e in value.manifest.entries})
        with self.assertRaises(FrozenInstanceError):
            value.scope = None
        for bad in (None, value.scope, value.manifest, object()):
            with self.subTest(bad=type(bad)), self.assertRaises(ValueError):
                require_v4_campaign_scope(bad)
        other = campaign(count=11)
        with self.assertRaises(ValueError):
            V4CampaignAdmissionScope(scope=value.scope, manifest=other.manifest)
        forged = value.scope.model_copy(update={"document_ids": ()})
        with self.assertRaises(ValueError):
            V4CampaignAdmissionScope(scope=forged, manifest=value.manifest)

    def test_generic_none_and_legacy_eight_keep_their_separate_meanings(self):
        scope = campaign()
        validate_v4_admission_scope(admission_document_ids=None, campaign_scope=None)
        validate_v4_admission_scope(admission_document_ids=scope.document_ids[:8], campaign_scope=None)
        validate_v4_admission_scope(admission_document_ids=None, campaign_scope=scope)
        for ids, selected in (((), None), (scope.document_ids[:9], None), (scope.document_ids[:8], scope)):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                validate_v4_admission_scope(admission_document_ids=ids, campaign_scope=selected)


class StagedCampaignPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.head = persistence_fixture._prepared_authority("attempt-1", snapshot_bytes=100, database_now=NOW)

    def persistence(self, scope, repository):
        factory = persistence_fixture._Factory(repository)
        commits = []

        def uow_factory():
            uow = factory()
            original = uow.commit

            def commit():
                commits.append(True)
                return original()

            uow.commit = commit
            return uow

        value = DurableStagedCoordinatorPersistenceV4(
            uow_factory=uow_factory, limits=persistence_fixture._limits(),
            owner_identity="campaign-test-owner", monotonic=persistence_fixture._Clock(),
            campaign_scope=scope,
        )
        return value, commits

    def test_global_recovery_is_unfiltered_but_outside_or_changed_source_claim_writes_nothing(self):
        for pairs in ((('outside-document', self.head.source_pdf_sha256),),
                      ((self.head.document_id, m6.digest("changed")),)):
            with self.subTest(pairs=pairs):
                repository = persistence_fixture._Repository((self.head,))
                value, commits = self.persistence(campaign(pairs), repository)
                candidates = value.list_recoverable(after_attempt_id=None, limit=2)
                self.assertEqual([c.attempt_id for c in candidates], [self.head.attempt_id])
                with self.assertRaises(V4CampaignScopeViolation):
                    value.claim_recovery(candidates[0])
                self.assertEqual(repository.claim_calls, 0)
                self.assertEqual(commits, [])
                self.assertEqual(repository.heads[self.head.attempt_id], self.head)

    def test_carry_in_existing_h0_can_be_claimed_and_full_scope_reaches_prepared_source(self):
        scope = campaign(((self.head.document_id, self.head.source_pdf_sha256),), carry_in=True)

        class ScopedRepository(persistence_fixture._Repository):
            received_scope = None

            def list_unclaimed_prepared_heads(inner, *, after_attempt_id, limit, campaign_scope=None):
                inner.received_scope = campaign_scope
                return super().list_unclaimed_prepared_heads(after_attempt_id=after_attempt_id, limit=limit)

        repository = ScopedRepository((self.head,))
        value, commits = self.persistence(scope, repository)
        result = value.admit_new(limit=1, available_credits=persistence_fixture._limits().credits)
        self.assertEqual([w.attempt_id for w in result.work], [self.head.attempt_id])
        self.assertIs(repository.received_scope, scope)
        self.assertEqual(value.campaign_scope_sha256, scope.scope_sha256)
        self.assertEqual(repository.claim_calls, 1)
        self.assertEqual(len(commits), 1)
        with self.assertRaises(V4CampaignScopeViolation):
            scope.require_ordinary_document_source(self.head.document_id, self.head.source_pdf_sha256)


class StagedCampaignAdmitterTests(unittest.TestCase):
    def fixture(self, scope, *, bind=True):
        base, source, claims, _claimed, credit = admission_fixture.StagedV4NewWorkAdmitterTests()._fixture()
        if bind:
            source.campaign_scope_sha256 = scope.scope_sha256
            claims.campaign_scope_sha256 = scope.scope_sha256
        value = StagedV4NewWorkAdmitter(
            prepared_claims=claims, ordinary_candidates=source,
            ingress_factory=base._ingress_factory, ingress=base._ingress,
            campaign_scope=scope, candidate_page_size=2,
        )
        return value, source, claims, credit

    def test_unscoped_dependencies_are_rejected_before_any_admission(self):
        scope = campaign((("doc-1", admission_fixture._SOURCE_SHA),))
        with self.assertRaises(V4CampaignScopeViolation):
            self.fixture(scope, bind=False)

    def test_candidate_escape_source_drift_and_carry_in_are_rejected_before_observation(self):
        scopes = (campaign((("outside", admission_fixture._SOURCE_SHA),)),
                  campaign((("doc-1", m6.digest("changed-source")),)),
                  campaign((("doc-1", admission_fixture._SOURCE_SHA),), carry_in=True))
        for scope in scopes:
            with self.subTest(scope=scope.scope_sha256):
                value, _source, claims, credit = self.fixture(scope)
                with (mock.patch.object(value._ingress_factory, "observation_request") as observe,
                      mock.patch.object(value._ingress, "execute") as execute,
                      self.assertRaises(V4CampaignScopeViolation)):
                    value.admit_new(limit=1, available_credits=credit.reservation)
                observe.assert_not_called()
                execute.assert_not_called()
                self.assertEqual(claims.claims, [])

    def test_factory_source_escape_is_rejected_before_ingress_mutation(self):
        scope = campaign((("doc-1", admission_fixture._SOURCE_SHA),))
        value, _source, claims, credit = self.fixture(scope)
        # Fault injection at the port: a producer returned another document.
        escaped = SimpleNamespace(proposal=SimpleNamespace(document_id="outside"),
                                  expected_raw_file_hash=admission_fixture._SOURCE_SHA)
        with (mock.patch.object(value._ingress_factory, "build", return_value=escaped),
              mock.patch.object(value._ingress, "execute") as execute,
              self.assertRaises(V4CampaignScopeViolation)):
            admission_fixture.StagedV4NewWorkAdmitterTests._admit(value, limit=1, available_credits=credit.reservation)
        execute.assert_not_called()
        self.assertEqual(claims.claims, [])


if __name__ == "__main__":
    unittest.main()
