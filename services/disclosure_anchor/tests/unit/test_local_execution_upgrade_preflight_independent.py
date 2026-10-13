"""Independent acceptance: the read-only deployment preflight (no database in this suite).

Pro §C5 and root's review: the preflight runs the worker's own gate over the U01 edge and reports
technical install eligibility only. A PUBLIC_STOP, an INVALID_STOP or an unreadable control plane
refuses before any database access (root follow-up 19:31Z), OPERATOR_DISABLED stays a technical
eligibility state, and the live API owner must be the activation's qualified owner. Database-backed
scope, prepared-key lifetimes and retained remote tasks are exercised by the scratch scenario
(``tests.integration.test_local_execution_upgrade_recovery_v4``).
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime import mineru_execution_upgrade as upgrade_runtime
from disclosure_anchor.adapters.runtime import mineru_release_binding, worker_stop_control
from disclosure_anchor.cli import worker as worker_cli
from disclosure_anchor.adapters.runtime import mineru_deployment_gate as gate
from disclosure_anchor.application.contracts.mineru_capacity_config import decode_mineru_capacity_config
from tests._mineru_capacity_config_fixture import canonical_payload
from tests._mineru_capacity_v11_fixture import idle_health
from tests._mineru_package_a_fixture import explicit_payload
from tests._f5_upgrade_q0_fixture import SYNTHETIC_NOW, synthetic_parent_q0
from tests._f5_upgrade_u01_fixture import U01Bundle, build_u01, frozen_wall_clock, synthetic_member
from tests.unit._f5_stop_fixture import write_stop
# Module imports: importing a TestCase class by name would re-run its tests here.
from tests.unit import test_mineru_deployment_gate as gate_tests
from tests.unit import test_mineru_process_pressure_consumer as pressure_tests


PREFLIGHT_CONTRACT = "worker-deployment-preflight.v1"


class DeploymentPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        parent = synthetic_parent_q0(self)
        self.bundle = build_u01(parent, members=[synthetic_member(parent, index) for index in range(3)])
        runtime_root = self.bundle.settings.disclosure_runtime_root
        runtime_root.chmod(0o700)
        self.engine_calls: list[str] = []
        self.owner_probes: list[tuple[str, str]] = []

    def _engine_factory(self) -> Any:
        self.engine_calls.append("engine_factory")
        raise RuntimeError("no database in the unit suite")

    def _live_owner(self, api_url: str, capacity_sha256: str) -> dict[str, Any]:
        self.owner_probes.append((api_url, capacity_sha256))
        return dict(self.bundle.activation["owner"])

    def _preflight(self, bundle: U01Bundle | None = None, **kwargs: Any) -> dict[str, Any]:
        bundle = bundle or self.bundle
        kwargs.setdefault("live_owner", self._live_owner)
        with bundle.active():
            return upgrade_runtime.run_deployment_preflight(
                bundle.settings, engine_factory=self._engine_factory, now=bundle.parent.clock, **kwargs,
            )

    def test_a_public_or_invalid_stop_or_unreadable_control_blocks_before_any_database_access(self) -> None:
        settings = self.bundle.settings
        runtime_root = settings.disclosure_runtime_root
        for state in ("PUBLIC_STOP", "INVALID_STOP", "CONTROL_UNAVAILABLE"):
            with self.subTest(state=state):
                if runtime_root.exists():
                    shutil.rmtree(runtime_root)
                runtime_root.mkdir(mode=0o700)
                runtime_root.chmod(0o700)
                if state == "PUBLIC_STOP":
                    write_stop(settings)
                elif state == "INVALID_STOP":
                    control = runtime_root / "control"
                    control.mkdir(mode=0o700)
                    (control / "worker-circuit-stop.json").write_bytes(b"{truncated")
                    (control / "worker-circuit-stop.json").chmod(0o600)
                else:
                    runtime_root.rmdir()
                self.engine_calls.clear()
                report = self._preflight()
                self.assertFalse(report["ready_to_install"])
                self.assertTrue(any("operational control" in item for item in report["blockers"]), report["blockers"])
                self.assertEqual(self.engine_calls, [], f"{state}: the database factory ran after a {state} blocker")
        runtime_root.mkdir(mode=0o700, exist_ok=True)

    def test_operator_disabled_is_technical_eligibility_not_a_blocker(self) -> None:
        disabled = mock.MagicMock(state="OPERATOR_DISABLED")
        with mock.patch.object(worker_stop_control, "observe_worker_control", return_value=disabled):
            report = self._preflight()
        self.assertEqual(report["operational_control_state"], "OPERATOR_DISABLED")
        self.assertFalse(any("operational control" in item for item in report["blockers"]), report["blockers"])
        self.assertEqual(self.engine_calls, ["engine_factory"])

    def test_the_report_names_the_inherited_parent_and_the_current_execution(self) -> None:
        bundle, parent = self.bundle, self.bundle.parent
        report = self._preflight()
        self.assertEqual(report["contract_version"], PREFLIGHT_CONTRACT)
        self.assertEqual(report["operational_control_state"], "RUNNABLE")
        self.assertEqual(
            {key: report[key] for key in (
                "qualification_origin", "parent_runtime_identity_sha256", "parent_writer_code_sha256",
                "parent_qualified_at_utc", "writer_code_sha256", "current_runtime_identity_sha256",
                "process_profile_sha256", "worker_profile_sha256", "stream_activation_sha256",
                "upgrade_sha256", "legacy_member_count", "native_identity_match",
            )},
            {
                "qualification_origin": "compatible_parent",
                "parent_runtime_identity_sha256": parent.runtime_identity,
                "parent_writer_code_sha256": parent.historical_writer,
                "parent_qualified_at_utc": json.loads(parent.canary_path.read_bytes())["passed_at_utc"],
                "writer_code_sha256": bundle.current_writer,
                "current_runtime_identity_sha256": bundle.current_runtime,
                "process_profile_sha256": bundle.process_profile.sha256,
                "worker_profile_sha256": bundle.worker_profile.sha256,
                "stream_activation_sha256": bundle.settings.disclosure_mineru_stream_pressure_config_sha256,
                "upgrade_sha256": bundle.proposal_sha256,
                "legacy_member_count": 3,
                "native_identity_match": True,
            },
        )
        # The scope needs the database; its absence is a blocker, never an empty PASS.
        self.assertEqual(self.engine_calls, ["engine_factory"])
        self.assertIn("legacy scope database unavailable (RuntimeError)", report["blockers"])
        self.assertFalse(report["ready_to_install"])
        self.assertEqual(self.owner_probes, [(bundle.settings.disclosure_mineru_api_url, parent.capacity_sha256)])
        json.dumps(report)

    def test_a_live_owner_other_than_the_qualified_activation_owner_is_a_blocker(self) -> None:
        other = dict(self.bundle.activation["owner"], process_id=self.bundle.activation["owner"]["process_id"] + 1)
        report = self._preflight(live_owner=lambda _url, _capacity: other)
        self.assertIs(report["native_identity_match"], False)
        self.assertIn("live API owner differs from the configured activation owner (new native epoch)",
                      report["blockers"])

        def unavailable(_url: str, _capacity: str) -> dict[str, Any]:
            raise OSError("connection refused")

        report = self._preflight(live_owner=unavailable)
        self.assertIs(report["native_identity_match"], False)
        self.assertTrue(any(item.startswith("native identity unavailable") for item in report["blockers"]))

    def test_without_proof_the_preflight_blocks_before_the_database(self) -> None:
        exact = self.bundle.with_settings(
            disclosure_worker_execution_upgrade_file=None, disclosure_worker_execution_upgrade_sha256=None,
            disclosure_worker_execution_upgrade_review_file=None, disclosure_worker_execution_upgrade_review_sha256=None,
        )
        report = self._preflight(exact)
        self.assertIsNone(report["qualification_origin"])
        self.assertTrue(any(item.startswith("deployment qualification: MinerU exact runtime identity cannot be verified")
                            for item in report["blockers"]), report["blockers"])
        self.assertEqual(self.engine_calls, [])
        self.assertFalse(report["ready_to_install"])

    def test_the_command_prints_one_json_report_and_exits_78_when_not_ready(self) -> None:
        bundle = self.bundle
        live = mock.MagicMock(owner=dict(bundle.activation["owner"]))
        for output_format in ("json", "terminal"):
            with self.subTest(output_format=output_format):
                stdout = io.StringIO()
                with (
                    bundle.active(),
                    mock.patch.object(mineru_release_binding, "sample_live_identity", return_value=live) as probe,
                    contextlib.redirect_stdout(stdout),
                ):
                    code = worker_cli._deployment_preflight_command(
                        bundle.settings, output_format=output_format, prepared_key_ttl_seconds=86400,
                    )
                self.assertEqual(code, 78)
                probe.assert_called_once()
                if output_format == "json":
                    report = json.loads(stdout.getvalue())
                    self.assertEqual((report["contract_version"], report["ready_to_install"]),
                                     (PREFLIGHT_CONTRACT, False))
                    self.assertEqual(report["qualification_origin"], "compatible_parent")
                    self.assertTrue(any(item.startswith("legacy scope database unavailable") for item in report["blockers"]))
                else:
                    self.assertIn("ready_to_install=false origin=compatible_parent", stdout.getvalue())
                    self.assertIn("BLOCKER legacy scope database unavailable", stdout.getvalue())

    def test_legacy_sync_keeps_an_equivalent_exact_preflight_without_the_database(self) -> None:
        # Root review: the installer's preflight must not turn into a staged-v4-only door. The
        # legacy-sync resident loop already demands the exact MinerU proof at start (see
        # ResidentLoopBoundaryTests); its preflight is that same exact gate, with no V4 scope,
        # no database and no native probe. A qualified legacy-sync deployment is ready.
        root = Path(tempfile.mkdtemp(prefix="f5-legacy-sync-", dir=Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(shutil.rmtree, root, True)
        settings, client, _validation = gate_tests.MinerUDeploymentGateTests()._fixture(root, now=SYNTHETIC_NOW)
        settings.disclosure_runtime_root.chmod(0o700)
        self.assertEqual(settings.worker_parse_execution_mode, "legacy-sync")
        self.assertFalse(settings.execution_upgrade_configured)

        def no_probe(_url: str, _capacity: str) -> dict[str, Any]:
            raise AssertionError("legacy-sync has no stream activation to compare")

        def preflight() -> dict[str, Any]:
            return upgrade_runtime.run_deployment_preflight(
                settings, engine_factory=self._engine_factory, live_owner=no_probe, now=SYNTHETIC_NOW,
            )

        with mock.patch.object(gate, "client_bundle_identity", return_value=client), frozen_wall_clock(SYNTHETIC_NOW):
            with mock.patch.object(gate, "writer_code_digest", return_value=gate_tests.CODE_DIGEST):
                ready = preflight()
            drifted = preflight()
        self.assertEqual((ready["ready_to_install"], ready["qualification_origin"]), (True, "exact"), ready["blockers"])
        self.assertEqual((ready["legacy_scope"], ready["native_identity_match"]), (None, None))
        self.assertEqual(self.engine_calls, [])
        self.assertFalse(drifted["ready_to_install"])
        self.assertTrue(any("local writer code drifted" in item for item in drifted["blockers"]), drifted["blockers"])

    def test_retained_completed_tasks_are_not_active_parsing_but_live_work_is(self) -> None:
        # Root item 3: the 17 original accepted tasks stay retained and completed on the API; the
        # native probe must accept them (queued=0, processing=0) and refuse only live work.
        payload = explicit_payload(14)
        capacity = decode_mineru_capacity_config(canonical_payload(payload))
        pressure = pressure_tests.pressure_payload()
        pressure["capacity_config_sha256"] = capacity.sha256

        def probe(**changes: int) -> Any:
            health = idle_health(payload)
            health["capacity_observation"]["owner"] = dict(pressure_tests.OWNER)
            health.update(completed_tasks=17, **changes)
            answers = {"/health": health, "/agent/telemetry/pressure/v1": pressure}

            def fetch(url: str, **_kwargs: Any) -> tuple[dict[str, Any], bytes]:
                value = answers[url.removeprefix("http://127.0.0.1:30002")]
                return value, json.dumps(value).encode()

            with mock.patch.object(mineru_release_binding, "fetch_json", side_effect=fetch):
                return mineru_release_binding.sample_live_identity(
                    "http://127.0.0.1:30002", capacity_sha256=capacity.sha256,
                )

        self.assertEqual(probe().owner, dict(pressure_tests.OWNER))
        for active in ({"queued_tasks": 1}, {"processing_tasks": 1}):
            with self.subTest(active=active), self.assertRaisesRegex(
                mineru_release_binding.ReleaseIdentityError, "API is not healthy and idle",
            ):
                probe(**active)

    def test_the_key_lifetime_argument_must_be_positive(self) -> None:
        for value in ("0", "-86400", "x"):
            with self.subTest(value=value):
                with (
                    mock.patch.object(worker_cli, "load_settings") as load_settings,
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    worker_cli.main(["deployment-preflight", "--prepared-key-ttl-seconds", value])
                self.assertEqual(raised.exception.code, 2)
                load_settings.assert_not_called()


if __name__ == "__main__":
    unittest.main()
