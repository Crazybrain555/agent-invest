import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime import staged_worker_v4 as builder
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.application.contracts.staged_campaign_v4 import V4CampaignScopeViolation
from disclosure_anchor.application.services.mineru_stream_policy import (
    MineruStreamPolicy, StreamAdmissionControl, StreamPolicyConfig,
)
from disclosure_anchor.settings import Settings
from tests import m6_support as m6
from tests._mineru_capacity_consumers_fixture import configuration
from tests._mineru_package_a_fixture import gate_fixture
from tests._staged_campaign_sql import CampaignSqlDatabase, campaign
from tests.unit import test_mineru_package_a_builder as builder_fixture
from tests.unit.test_mineru_stream_policy import Pressure


def stream_control(runtime=m6.digest("runtime"), maximum=2):
    return StreamAdmissionControl(MineruStreamPolicy(StreamPolicyConfig(
        qualified_max=maximum, runtime_identity_sha256=runtime,
        owner_identity_sha256=m6.digest("owner"))), Pressure(None))


class StagedCampaignBuilderTests(unittest.TestCase):
    def setUp(self):
        self.db = CampaignSqlDatabase()
        self.addCleanup(self.db.close)
        self.scope = campaign()

    def arguments(self):
        return dict(settings=SimpleNamespace(worker_parse_execution_mode="staged-v4"),
                    engine=self.db.engine, ownership_guard=mock.Mock(), admission_guard=mock.Mock(),
                    process_scope_classes=None, progress=mock.Mock(), campaign_scope=self.scope,
                    expected_capacity=configuration(), stream_control=stream_control())

    def test_explicit_entry_rejects_missing_or_wrong_authority_before_generic_builder(self):
        for key, value in (("campaign_scope", None), ("campaign_scope", self.scope.scope),
                           ("expected_capacity", None), ("expected_capacity", object()),
                           ("stream_control", None), ("stream_control", object())):
            args = {**self.arguments(), key: value}
            with (self.subTest(key=key, value=type(value)),
                  mock.patch.object(builder, "build_staged_worker_v4_runtime") as generic,
                  self.assertRaises(ValueError)):
                builder.build_staged_worker_v4_campaign_runtime(**args)
            generic.assert_not_called()
            args["ownership_guard"].assert_not_called()

    def test_generic_builder_rejects_mixed_legacy_and_campaign_before_owner_or_loader(self):
        args = self.arguments()
        with (mock.patch.object(builder, "load_staged_v4_settings") as loader,
              self.assertRaises(ValueError)):
            builder.build_staged_worker_v4_runtime(**args, admission_document_ids=self.scope.document_ids[:8])
        loader.assert_not_called()
        args["ownership_guard"].assert_not_called()

    def test_original_owner_guard_runs_before_campaign_database_read(self):
        args = self.arguments()
        failure = RuntimeError("original owner is stale")
        args["ownership_guard"].side_effect = failure
        self.db.statements.clear()
        with (mock.patch.object(builder, "load_staged_v4_settings") as loader,
              self.assertRaises(RuntimeError) as raised):
            builder.build_staged_worker_v4_campaign_runtime(**args)
        self.assertIs(raised.exception, failure)
        loader.assert_not_called()
        self.assertEqual(self.db.statements, [])

    def test_real_global_source_guard_refuses_before_profile_keyring_or_remote(self):
        # The document is a member; its unresolved source is not the frozen pair.
        self.db.add_head("outside-source-tail", self.scope.document_ids[0], self.scope.source_hashes[1],
                         state="ack_pending", claimed=True)
        before = self.db.total_changes()
        args = self.arguments()
        with (mock.patch.object(builder, "load_staged_v4_settings") as loader,
              mock.patch.object(builder, "MinerUHttpRemoteV4") as remote,
              self.assertRaises(V4CampaignScopeViolation)):
            builder.build_staged_worker_v4_campaign_runtime(**args)
        loader.assert_not_called()
        remote.assert_not_called()
        args["ownership_guard"].assert_called_once_with()
        self.assertEqual(self.db.total_changes(), before)

    def test_actual_builder_shares_full_scope_and_installs_both_live_effect_guards(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            root = Path(directory)
            settings, profile, capacity, _ = gate_fixture(root)
            environment = builder_fixture.ExplicitStagedBuilderTests().profile_environment(root, profile)
            settings = Settings(**dict(settings.model_dump(),
                disclosure_v4_secret_keyring_file=Path(environment["DISCLOSURE_V4_SECRET_KEYRING_FILE"])))
            FileStorePathBuilder(settings).data_path(Path()).mkdir(parents=True)
            control = stream_control(profile.runtime_bundle_identity_sha256, capacity.parse_active_limit)
            calls = []
            with (mock.patch.dict(os.environ, environment, clear=True),
                  mock.patch("httpx.Client.send", side_effect=AssertionError("network forbidden")),
                  mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden"))):
                runtime = builder.build_staged_worker_v4_campaign_runtime(
                    settings=settings, engine=self.db.engine,
                    ownership_guard=lambda: calls.append("owner"),
                    admission_guard=lambda: calls.append("admission"),
                    process_scope_classes=None, progress=lambda _: None,
                    campaign_scope=self.scope, expected_capacity=capacity,
                    stream_control=control, owner_identity="independent-campaign-owner",
                )
                try:
                    admitter = runtime.coordinator._admission_observer
                    self.assertIs(admitter._campaign_scope, self.scope)
                    self.assertIs(admitter._prepared_claims._campaign_scope, self.scope)
                    self.assertIs(admitter._ordinary_candidates._campaign_scope, self.scope)
                    self.assertIsNone(admitter._admission_document_ids)
                    self.assertEqual(admitter._ordinary_candidates._admission_document_ids, self.scope.document_ids)
                    self.assertIs(runtime.coordinator._stream_control, control)
                    self.db.add_head("foreign-claimed", "outside", m6.digest("outside"),
                                     state="submitted", claimed=True)
                    before = self.db.total_changes()
                    calls.clear()
                    with self.assertRaises(V4CampaignScopeViolation):
                        runtime.coordinator._process_guard()
                    self.assertEqual(calls, ["owner"])
                    calls.clear()
                    with self.assertRaises(V4CampaignScopeViolation):
                        admitter._admission_guard()
                    self.assertEqual(calls, ["admission", "owner"])
                    self.assertEqual(self.db.total_changes(), before)
                finally:
                    runtime.close()


if __name__ == "__main__":
    unittest.main()
