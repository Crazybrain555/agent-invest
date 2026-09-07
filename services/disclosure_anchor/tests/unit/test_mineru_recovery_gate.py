from __future__ import annotations

import copy
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from disclosure_anchor.adapters.db.postgres.staged_recovery_scope_v4 import (
    require_accepted_recovery_scope,
    validate_recovery_scope_rows,
)
from disclosure_anchor.adapters.runtime.mineru_identity import canonical_payload_sha256
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import (
    MinerUDeploymentGateError,
    VerifiedMinerUDeployment,
    verify_mineru_deployment_gate,
)
from disclosure_anchor.adapters.runtime.mineru_recovery_gate import (
    GRANT_CONTRACT,
    RECOVERY_RUNTIME_CONTRACT,
    REVIEW_CONTRACT,
    VerifiedMinerURecovery,
    load_recovery_gate,
    require_same_remote_manifest,
    validate_recovery_grant,
)
from disclosure_anchor.adapters.runtime.staged_worker_v4 import _RecoveryOnlyAdmission
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
)
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorResult,
    CoordinatorTerminal,
)
from disclosure_anchor.cli.staged_recover import run_recovery
from tests.unit import test_mineru_deployment_gate as deployment_fixture


def _fixture() -> tuple[dict, dict, datetime]:
    now = datetime.now(UTC)
    review = {
        "contract_version": REVIEW_CONTRACT,
        "verdict": "GO",
        "old_writer_sha256": "sha256:" + "1" * 64,
        "current_writer_sha256": "sha256:" + "2" * 64,
        "reviewed_delta_sha256": "sha256:" + "3" * 64,
    }
    attempt = {
        "document_id": "doc-1",
        "attempt_id": "attempt-1",
        "processing_run_id": "run-1",
        "fence_identity": "fence-1",
        "source_pdf_sha256": "sha256:" + "4" * 64,
        "execution_spec_sha256": "sha256:" + "5" * 64,
        "h0_sha256": "sha256:" + "6" * 64,
        "accepted_submission_sha256": "sha256:" + "7" * 64,
    }
    grant = {
        "contract_version": GRANT_CONTRACT,
        "created_at": (now - timedelta(seconds=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "reason": "identity-content-encoding-recovery",
        "old_runtime_sha256": "sha256:" + "8" * 64,
        "current_runtime_sha256": "sha256:" + "9" * 64,
        "old_writer_sha256": review["old_writer_sha256"],
        "current_writer_sha256": review["current_writer_sha256"],
        "process_profile_sha256": "sha256:" + "a" * 64,
        "worker_profile_sha256": "sha256:" + "b" * 64,
        "review_receipt_sha256": canonical_payload_sha256(review),
        "attempts": [attempt],
    }
    return grant, review, now


class MinerURecoveryGateTests(unittest.TestCase):
    def test_grant_requires_exact_bounded_scope_review_and_valid_time(self) -> None:
        grant, review, now = _fixture()
        self.assertEqual(
            validate_recovery_grant(grant, review, now=now), tuple(grant["attempts"])
        )
        self.assertEqual(validate_recovery_grant(
            {**grant, "reason": "identity-content-encoding-and-publication-lineage-recovery"},
            review, now=now), tuple(grant["attempts"]))
        for key, value in (
            ("reason", "any-upgrade"),
            ("extra", True),
            ("review_receipt_sha256", "sha256:" + "f" * 64),
            ("expires_at", now.isoformat()),
            ("created_at", (now + timedelta(seconds=1)).isoformat()),
            ("expires_at", (now + timedelta(days=2)).isoformat()),
            ("expires_at", now.astimezone().replace(tzinfo=None).isoformat()),
            ("attempts", []),
            ("attempts", grant["attempts"] * 2),
            ("current_writer_sha256", grant["old_writer_sha256"]),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_recovery_grant({**grant, key: value}, review, now=now)
        for key, value in (
            ("verdict", "STOP"),
            ("extra", True),
            ("current_writer_sha256", "sha256:" + "f" * 64),
        ):
            changed = {**review, key: value}
            with self.subTest(review=key), self.assertRaises(ValueError):
                validate_recovery_grant(
                    {
                        **grant,
                        "review_receipt_sha256": canonical_payload_sha256(changed),
                    },
                    changed,
                    now=now,
                )

    def test_only_writer_may_change_in_remote_manifest(self) -> None:
        old = {
            "client": {"writer_code_sha256": "old", "packages": "same"},
            "orchestrator": {"image": "same"},
            "inference_server": {"model": "same"},
            "topology": {"host": "same"},
        }
        current = copy.deepcopy(old)
        current["client"]["writer_code_sha256"] = "new"
        require_same_remote_manifest(old, current)
        for section, field in (
            ("client", "packages"),
            ("orchestrator", "image"),
            ("inference_server", "model"),
            ("topology", "host"),
        ):
            changed = copy.deepcopy(current)
            changed[section][field] = "different"
            with self.subTest(section=section), self.assertRaises(ValueError):
                require_same_remote_manifest(old, changed)

    def test_scope_rejects_hidden_outside_replaced_prepared_and_evidence_drift(
        self,
    ) -> None:
        grant, _, _ = _fixture()
        attempt = grant["attempts"][0]
        good = {
            **attempt,
            "is_current": True,
            "runtime_epoch_sha256": grant["old_runtime_sha256"],
            "state": "materializing",
        }
        for state in (
            "submitted",
            "remote_terminal",
            "local_materialized",
            "publish_committed",
            "cleanup_pending",
            "ack_pending",
            "acked",
        ):
            validate_recovery_scope_rows(
                [{**good, "state": state}],
                attempts=(attempt,),
                runtime_sha256=grant["old_runtime_sha256"],
            )
        invalid = [[], [good, good], [{**good, "attempt_id": "outside"}]]
        invalid += [[{**good, key: None}] for key in attempt]
        invalid += [
            [{**good, "state": value}]
            for value in ("prepared", "reconciling", "superseded")
        ]
        invalid += [[{**good, "runtime_epoch_sha256": "changed"}]]
        invalid += [[{**good, "is_current": False}]]
        validate_recovery_scope_rows(
            [{**good, "is_current": False, "state": "acked"}],
            attempts=(attempt,),
            runtime_sha256=grant["old_runtime_sha256"],
        )
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                validate_recovery_scope_rows(
                    rows,
                    attempts=(attempt,),
                    runtime_sha256=grant["old_runtime_sha256"],
                )

    def test_sql_keeps_all_outside_resource_owners_visible(self) -> None:
        grant, _, _ = _fixture()
        engine = mock.MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        connection.execute.return_value.mappings.return_value = []
        with self.assertRaises(RuntimeError):
            require_accepted_recovery_scope(
                engine,
                attempts=tuple(grant["attempts"]),
                runtime_sha256=grant["old_runtime_sha256"],
            )
        statement, params = connection.execute.call_args.args
        self.assertIn(
            "a.state=ANY(:resource_states) OR a.document_id=ANY(:document_ids)",
            str(statement),
        )
        self.assertIn("prepared", params["resource_states"])
        self.assertIn("h.lifecycle_version=0", str(statement))

    def test_closed_admission_cannot_poll_or_admit_prepared_or_ordinary_work(
        self,
    ) -> None:
        result = _RecoveryOnlyAdmission().admit_new(
            limit=10, available_credits=ResourceCreditVector(documents=10)
        )
        self.assertEqual(result.work, ())
        self.assertFalse(result.backlog_exists or result.scan_incomplete)
        self.assertIsNone(result.observation_request)

    def test_live_gate_rechecks_current_writer_and_original_epoch(self) -> None:
        grant, _, _ = _fixture()
        epoch = {
            "collector_sha256": "collector",
            "windows_node_identity_sha256": "node",
            "container_epoch_sha256": "epoch",
            "api_container_id": "api",
        }
        facts = {
            "container_epoch_sha256": "epoch",
            "api_container_id": "api",
            "restart_count_total": 0,
            "oom_killed_count": 0,
            "unsafe_container_count": 0,
            "cgroup_oom_total": 0,
            "cgroup_oom_kill_total": 0,
        }
        for changed in (
            {},
            {"container_epoch_sha256": "new"},
            {"api_container_id": "new"},
            {"cgroup_oom_total": 1},
            {"restart_count_total": 1},
        ):
            gate = VerifiedMinerURecovery(
                grant,
                tuple(grant["attempts"]),
                mock.Mock(spec=VerifiedMinerUDeployment),
                epoch,
                mock.Mock(),
            )
            with (
                mock.patch(
                    "disclosure_anchor.adapters.runtime.mineru_recovery_gate.writer_code_digest",
                    return_value=grant["current_writer_sha256"],
                ),
                mock.patch(
                    "disclosure_anchor.adapters.runtime.mineru_recovery_gate.project_host_service_epoch",
                    return_value=SimpleNamespace(**{**facts, **changed}),
                ),
            ):
                if changed:
                    with self.assertRaises(ValueError):
                        gate.assert_live()
                else:
                    gate.assert_live()
                    gate.assert_live()
                    gate.sampler.sample_payload.assert_called_once()
                    gate.deployment.probe_orchestrator.assert_called_once_with(
                        require_idle=False
                    )
        gate = VerifiedMinerURecovery(
            grant, tuple(grant["attempts"]), mock.Mock(), epoch, mock.Mock()
        )
        with (
            mock.patch(
                "disclosure_anchor.adapters.runtime.mineru_recovery_gate.writer_code_digest",
                return_value="changed",
            ),
            self.assertRaises(ValueError),
        ):
            gate.assert_live()
        gate.sampler.sample_payload.assert_not_called()

    def test_loader_checks_both_versions_without_qualifying_old_writer_as_current(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            factory = deployment_fixture.MinerUDeploymentGateTests()
            settings, client, validation = factory._fixture(root, now=datetime.now(UTC))
            profile = factory._staged_profile(settings)
            smoke = json.loads(settings.disclosure_mineru_smoke_receipt.read_text())
            manifest = copy.deepcopy(smoke["runtime_manifest"])
            current_writer = "sha256:" + "d" * 64
            manifest["client"]["writer_code_sha256"] = current_writer
            current_identity = canonical_payload_sha256(manifest)
            grant, review, _ = _fixture()
            review.update(
                old_writer_sha256=deployment_fixture.CODE_DIGEST,
                current_writer_sha256=current_writer,
            )
            grant.update(
                old_runtime_sha256=settings.disclosure_mineru_runtime_bundle_identity_sha256,
                current_runtime_sha256=current_identity,
                old_writer_sha256=deployment_fixture.CODE_DIGEST,
                current_writer_sha256=current_writer,
                process_profile_sha256=profile.sha256,
                review_receipt_sha256=canonical_payload_sha256(review),
            )
            for name, payload in (
                ("grant", grant),
                ("review", review),
                (
                    "current",
                    {
                        "schema": RECOVERY_RUNTIME_CONTRACT,
                        "identity_sha256": current_identity,
                        "manifest": manifest,
                        "historical_runtime_identity_sha256": grant[
                            "old_runtime_sha256"
                        ],
                        "historical_writer_sha256": grant["old_writer_sha256"],
                        "container_epoch_sha256": validation["epoch_after"]["receipt"][
                            "service_epoch"
                        ]["container_epoch_sha256"],
                    },
                ),
            ):
                path = root / name
                path.write_text(json.dumps(payload))
                path.chmod(0o600)
            with (
                mock.patch(
                    "disclosure_anchor.adapters.runtime.mineru_recovery_gate.client_bundle_identity",
                    return_value=client,
                ),
                mock.patch(
                    "disclosure_anchor.adapters.runtime.mineru_recovery_gate.writer_code_digest",
                    return_value=current_writer,
                ),
                mock.patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate.client_bundle_identity",
                    return_value=client,
                ),
                mock.patch(
                    "disclosure_anchor.adapters.runtime.mineru_deployment_gate.writer_code_digest",
                    return_value=current_writer,
                ),
            ):
                gate = load_recovery_gate(
                    settings,
                    profile=profile,
                    grant_path=root / "grant",
                    review_path=root / "review",
                    current_manifest_path=root / "current",
                    ssh_command=["unused"],
                )
                self.assertEqual(
                    gate.deployment.runtime_identity_sha256, grant["old_runtime_sha256"]
                )
                with self.assertRaisesRegex(
                    MinerUDeploymentGateError, "writer code drifted"
                ):
                    verify_mineru_deployment_gate(
                        settings, parse_enabled=True, process_profile=profile
                    )
                with (
                    mock.patch(
                        "disclosure_anchor.adapters.runtime.mineru_recovery_gate.writer_code_digest",
                        return_value="sha256:" + "e" * 64,
                    ),
                    self.assertRaises(ValueError),
                ):
                    load_recovery_gate(
                        settings,
                        profile=profile,
                        grant_path=root / "grant",
                        review_path=root / "review",
                        current_manifest_path=root / "current",
                        ssh_command=["unused"],
                    )
                valid_wrapper = json.loads((root / "current").read_text())
                for key, value in (
                    ("schema", "mineru-runtime-manifest.v1"),
                    ("historical_runtime_identity_sha256", "sha256:" + "e" * 64),
                    ("historical_writer_sha256", "sha256:" + "e" * 64),
                    ("container_epoch_sha256", "sha256:" + "e" * 64),
                    ("deployment_qualification", True),
                ):
                    with self.subTest(wrapper_field=key):
                        (root / "current").write_text(
                            json.dumps({**valid_wrapper, key: value})
                        )
                        with self.assertRaisesRegex(ValueError, "exact derived identity"):
                            load_recovery_gate(
                                settings,
                                profile=profile,
                                grant_path=root / "grant",
                                review_path=root / "review",
                                current_manifest_path=root / "current",
                                ssh_command=["unused"],
                            )
                wrapper = {
                    "identity_sha256": "sha256:" + "e" * 64,
                    "manifest": manifest,
                }
                (root / "current").write_text(json.dumps(wrapper))
                with self.assertRaises(ValueError):
                    load_recovery_gate(
                        settings,
                        profile=profile,
                        grant_path=root / "grant",
                        review_path=root / "review",
                        current_manifest_path=root / "current",
                        ssh_command=["unused"],
                    )

    def test_cli_recovery_receipt_requires_same_original_run_zero_admission_and_scope(
        self,
    ) -> None:
        for scenario in ("pass", "admitted", "wrong_run", "scope", "profile"):
            with self.subTest(scenario=scenario), ExitStack() as stack:
                grant, _, _ = _fixture()
                gate = VerifiedMinerURecovery(
                    grant, tuple(grant["attempts"]), mock.Mock(), {}, mock.Mock()
                )
                stack.enter_context(mock.patch.object(gate, "assert_live"))
                prefix = "disclosure_anchor.cli.staged_recover."
                for name in (
                    "require_runtime_app_connection",
                    "require_runtime_app_engine",
                    "_assert_staged_singleton",
                ):
                    stack.enter_context(mock.patch(prefix + name))
                scope = stack.enter_context(
                    mock.patch(prefix + "require_accepted_recovery_scope")
                )
                if scenario == "scope":
                    scope.side_effect = RuntimeError("outside owner")
                stack.enter_context(
                    mock.patch(prefix + "_database_url", return_value="unused")
                )
                lock = stack.enter_context(
                    mock.patch(prefix + "sa.create_engine")
                ).return_value
                stack.enter_context(mock.patch(prefix + "_create_worker_db_engine"))
                stack.enter_context(
                    mock.patch(prefix + "_process_scope_classes", return_value=None)
                )
                row = {
                    "attempt_id": "attempt-1",
                    "attempt_run_id": "run-1",
                    "current_processing_run_id": "run-1",
                    "status": "published",
                    "attempt_state": "acked",
                    "run_is_active": True,
                    "run_status": "succeeded",
                }
                if scenario == "wrong_run":
                    row["current_processing_run_id"] = "other"
                stack.enter_context(
                    mock.patch(prefix + "_documents", return_value={"doc-1": row})
                )
                build = stack.enter_context(
                    mock.patch(prefix + "build_staged_worker_v4_runtime")
                )
                runtime = build.return_value
                runtime.worker_profile_sha256 = (
                    "changed"
                    if scenario == "profile"
                    else grant["worker_profile_sha256"]
                )
                runtime.owner_identity = "recovery-owner"
                runtime.coordinator.run.return_value = CoordinatorResult(
                    CoordinatorTerminal.QUIESCENT,
                    True,
                    1 if scenario == "admitted" else 0,
                    1,
                    (),
                    (),
                    ResourceCreditVector(),
                )
                if scenario in {"scope", "profile"}:
                    with self.assertRaises((RuntimeError, ValueError)):
                        run_recovery(
                            SimpleNamespace(worker_parse_execution_mode="staged-v4"),
                            gate=gate,
                            max_seconds=5,
                        )
                    runtime.coordinator.run.assert_not_called()
                    if scenario == "scope":
                        build.assert_not_called()
                    else:
                        runtime.close.assert_called_once()
                else:
                    result = run_recovery(
                        SimpleNamespace(worker_parse_execution_mode="staged-v4"),
                        gate=gate,
                        max_seconds=5,
                    )
                    self.assertEqual(
                        result["result"],
                        "RECOVERY_PASS" if scenario == "pass" else "NOT_PASS",
                    )
                    self.assertIs(result["deployment_qualification"], False)
                    self.assertEqual(
                        result["historical_runtime_sha256"], grant["old_runtime_sha256"]
                    )
                    self.assertEqual(
                        result["execution_writer_sha256"],
                        grant["current_writer_sha256"],
                    )
                    self.assertIs(build.call_args.kwargs["recovery_only"], True)
                    with self.assertRaises(RuntimeError):
                        build.call_args.kwargs["admission_guard"]()
                    runtime.coordinator.run.assert_called_once()
                    runtime.close.assert_called_once()
                lock.dispose.assert_called_once()
