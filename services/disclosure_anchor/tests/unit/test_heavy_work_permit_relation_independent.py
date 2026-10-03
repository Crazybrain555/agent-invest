"""Independent acceptance of the U01 v3 relation: heavy-work permit forms as the one reviewed WP axis.

A synthetic Q0, an archived recovery origin and real H0/spec duties reach the real deployment gate,
resolver, POST boundary, scope recheck, builders and CLI. Every v3 bundle is hand-encoded from the
contract's field names, never by the product builder, except where the CLI composition is itself under
test. No database, provider, model, network or production file is involved.
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
from typing import Any, Iterator
import unittest
from unittest import mock

from disclosure_anchor.adapters.db.postgres.staged_upgrade_scope_v4 import verify_legacy_scope
from disclosure_anchor.adapters.runtime import mineru_deployment_gate as gate
from disclosure_anchor.adapters.runtime import mineru_execution_upgrade as upgrade_runtime
from disclosure_anchor.application.contracts.staged_worker_profile_v4 import (
    StagedWorkerProfileV4, heavy_work_permit_form, with_heavy_work_permit_form,
)
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    HeavyWorkPermitForms, LegacyExecutionRefused, LocalExecutionUpgradeV2, decode_execution_upgrade,
    decode_local_execution_upgrade_v2, decode_local_execution_upgrade_v3, encode_local_execution_upgrade_v2,
    encode_local_execution_upgrade_v3,
)
from disclosure_anchor.application.services.staged_v4_capacity import staged_v4_coordinator_limits
from disclosure_anchor.cli import execution_upgrade as upgrade_cli
from disclosure_anchor.settings import Settings
from tests._f5_upgrade_duty_fixture import DutyWorld, StageGuard, member_payload
from tests._f5_upgrade_q0_fixture import sha256_bytes, stale_clock, synthetic_parent_q0
from tests._f5_upgrade_u01_fixture import exact_json, frozen_wall_clock, write_private
from tests._nul_recovery_v2_fixture import RecoveryBundle, build_recovery_v2


V3 = "worker-local-execution-upgrade.v3"
PERMITS_ENV = "DISCLOSURE_V4_HEAVY_WORK_PERMITS"


def _with(settings: Settings, **overrides: object) -> Settings:
    return Settings(**dict(settings.model_dump(), **overrides))


def _v3(
    bundle: RecoveryBundle, *, anchor: int | None = None, origin: int | None = None, target: int | None = 2,
    same_release: bool = False,
) -> RecoveryBundle:
    """The reviewed v3 relation over ``bundle``: each role's form, the composed target, a re-pinned origin."""

    target_worker = with_heavy_work_permit_form(bundle.worker_profile, target)
    origin_worker = with_heavy_work_permit_form(bundle.origin_worker, origin)
    environment = {key: value for key, value in bundle.environment.items() if key != PERMITS_ENV}
    if target is not None:
        environment[PERMITS_ENV] = str(target)
    origin_update: dict[str, Any] = {"worker_profile_sha256": origin_worker.sha256}
    if same_release:
        # A configuration-only edge: the origin is the target's own release, writer and runtime.
        origin_update.update(
            release_manifest_file=str(bundle.release_path),
            release_manifest_sha256=sha256_bytes(bundle.release_path.read_bytes()),
            source_revision=bundle.release["source_revision"],
        )

    def relation(proposal: dict[str, Any]) -> None:
        proposal["contract_version"] = V3
        proposal["heavy_work_permits"] = {"anchor": anchor, "origin": origin, "target": target}
        proposal["current_execution"]["worker_profile_sha256"] = target_worker.sha256
        proposal["recovery_origin"].update(origin_update)

    updated = replace(bundle, environment=environment, worker_profile=target_worker, origin_worker=origin_worker)
    return updated.with_proposal(relation)


class HeavyWorkPermitContractTests(unittest.TestCase):
    """Prior wire contracts stay exact; v3 is closed and names only its one axis."""

    def setUp(self) -> None:
        self.parent = synthetic_parent_q0(self)
        self.v2 = build_recovery_v2(self.parent)

    def test_v1_v2_wires_and_decoders_are_unchanged_and_refuse_the_new_section(self) -> None:
        exact = self.v2.proposal_path.read_bytes()
        decoded = decode_local_execution_upgrade_v2(exact)
        self.assertIsNone(decoded.heavy_work_permits)
        self.assertEqual(decoded.contract_version, "worker-local-execution-upgrade.v2")
        self.assertEqual(encode_local_execution_upgrade_v2(decoded), exact)
        self.assertEqual(decode_execution_upgrade(exact), decoded)
        smuggled = dict(json.loads(exact), heavy_work_permits={"anchor": None, "origin": None, "target": 2})
        for payload in (smuggled, dict(json.loads(exact), contract_version=V3)):
            with self.subTest(contract=payload["contract_version"]), self.assertRaises(ValueError):
                decode_local_execution_upgrade_v2(exact_json(payload))
        with self.assertRaises(ValueError):
            decode_local_execution_upgrade_v3(exact)
        with self.assertRaises(ValueError):
            encode_local_execution_upgrade_v3(decoded)

    def test_v3_section_is_closed_and_bounded(self) -> None:
        bundle = _v3(self.v2)
        exact = bundle.proposal_path.read_bytes()
        decoded = decode_execution_upgrade(exact)
        assert isinstance(decoded, LocalExecutionUpgradeV2)
        self.assertEqual(decoded.heavy_work_permits, HeavyWorkPermitForms(anchor=None, origin=None, target=2))
        self.assertEqual((decoded.contract_version, encode_local_execution_upgrade_v3(decoded)), (V3, exact))
        with self.assertRaises(ValueError):
            encode_local_execution_upgrade_v2(decoded)
        payload = json.loads(exact)
        for forms in ({"anchor": None, "origin": None}, {"anchor": None, "origin": None, "target": 2, "lanes": 3},
                      {"anchor": None, "origin": None, "target": 3}, {"anchor": None, "origin": None, "target": 0},
                      {"anchor": None, "origin": None, "target": True}, {"anchor": None, "origin": None, "target": "2"},
                      [None, None, 2]):
            with self.subTest(forms=forms), self.assertRaises(ValueError):
                decode_local_execution_upgrade_v3(exact_json(dict(payload, heavy_work_permits=forms)))

    def test_a_v3_target_keeps_its_origin_release_only_when_its_form_moves(self) -> None:
        moved = decode_execution_upgrade(_v3(self.v2, same_release=True).proposal_path.read_bytes())
        assert isinstance(moved, LocalExecutionUpgradeV2)
        self.assertEqual(moved.recovery_origin.release_manifest_sha256, moved.current.release_manifest_sha256)
        with self.assertRaisesRegex(ValueError, "form moves"):
            replace(moved, heavy_work_permits=HeavyWorkPermitForms(anchor=None, origin=2, target=2))
        with self.assertRaisesRegex(ValueError, "new release, not its recovery origin"):
            replace(moved, heavy_work_permits=None)

    def test_the_worker_profile_mapping_moves_exactly_the_version_and_the_count(self) -> None:
        v2 = self.v2.worker_profile
        self.assertIsNone(heavy_work_permit_form(v2))
        v3 = with_heavy_work_permit_form(v2, 2)
        self.assertEqual((v3.contract_version, heavy_work_permit_form(v3)), ("staged-worker-composition.v3", 2))
        self.assertEqual(with_heavy_work_permit_form(v3, None), v2)
        self.assertEqual(
            {name for name in StagedWorkerProfileV4.__dataclass_fields__ if getattr(v2, name) != getattr(v3, name)},
            {"contract_version", "heavy_work_permits"},
        )
        v1 = StagedWorkerProfileV4(v2.process_profile_sha256, 3, 5)
        for call in (lambda: heavy_work_permit_form(v1), lambda: with_heavy_work_permit_form(v1, 2),
                     lambda: with_heavy_work_permit_form(v2, 3)):
            with self.assertRaises(ValueError):
                call()


class HeavyWorkPermitGateTests(unittest.TestCase):
    """The real deployment gate, resolver and scope recheck over v3 relations."""

    def setUp(self) -> None:
        self.parent = synthetic_parent_q0(self)
        self.v2 = build_recovery_v2(self.parent)

    def _members(self, bundle: RecoveryBundle, world: DutyWorld) -> tuple[RecoveryBundle, Any, Any]:
        """One never-submitted and one accepted duty, both frozen under the origin's v2/v3 profile."""

        prepared = world.duty(tag="prepared-member", process_profile=bundle.origin_profile,
                              worker_profile=bundle.origin_worker)
        accepted = world.duty(tag="accepted-member", process_profile=bundle.origin_profile,
                              worker_profile=bundle.origin_worker)
        return bundle.with_members([member_payload(prepared.prepared()), member_payload(accepted.submitted())]), \
            prepared, accepted

    def test_a_v1_or_v2_relation_never_carries_a_v3_composition(self) -> None:
        self.assertIsNotNone(self.v2.verify().execution, "the prior relation verifies unchanged")
        composed = with_heavy_work_permit_form(self.v2.worker_profile, 2)
        carried = replace(self.v2, environment=dict(self.v2.environment, **{PERMITS_ENV: "2"})).with_proposal(
            lambda proposal: proposal["current_execution"].update(worker_profile_sha256=composed.sha256),
        )
        with self.assertRaisesRegex(gate.MinerUDeploymentGateError, "only by the v3 relation"):
            carried.verify()

    def test_forward_relation_verifies_and_every_original_duty_continues_under_two_permits(self) -> None:
        for same_release in (False, True):
            with self.subTest(same_release=same_release):
                world = DutyWorld(self.v2.root / f"forward-{int(same_release)}")
                bundle = _v3(self.v2, same_release=same_release)
                bundle, prepared, accepted = self._members(bundle, world)
                execution = bundle.verify().execution
                assert execution is not None
                summary = execution.summary()
                self.assertEqual(
                    (summary["upgrade_contract_version"], summary["anchor_heavy_work_permits"],
                     summary["origin_heavy_work_permits"], summary["heavy_work_permits"]),
                    (V3, None, None, 2),
                )
                # Q0 is untouched: its date, runtime and the worker profile it already carried.
                anchor = bundle.proposal["qualification_anchor"]
                self.assertEqual(
                    (summary["anchor_qualified_at_utc"], summary["anchor_runtime_identity_sha256"],
                     anchor["worker_profile_sha256"]),
                    (self.v2.proposal["qualification_anchor"]["qualified_at_utc"], self.parent.runtime_identity,
                     self.parent.worker_profile.sha256),
                )
                self.assertEqual(execution.member_worker_profile_sha256, self.v2.origin_worker.sha256)
                self.assertEqual(staged_v4_coordinator_limits(
                    bundle.process_profile, worker_profile=bundle.worker_profile).heavy_work_permits, 2)
                resolver = world.resolver(bundle.worker_profile, legacy_execution=execution)
                resolver.assert_execution_profile(prepared.prepared())
                authority, intent, _ = prepared.reconciling()
                snapshot, _, source = prepared.command_parts()
                command = resolver.submission_command(authority, snapshot=snapshot, intent=intent,
                                                      snapshot_source=source, stage_guard=StageGuard())
                assert command.legacy_authorization is not None
                self.assertEqual(command.request_exact_bytes, prepared.spec.request_exact_bytes)
                self.assertEqual(execution.require_submission(command, command.legacy_authorization),
                                 bundle.current_runtime)
                resolver.inspect_frozen_identity(accepted.submitted())
                self.assertEqual(execution.require_member_progress(accepted.submitted()).attempt_id,
                                 accepted.attempt_id)
                # The same duty under the old composition without the relation is refused.
                with self.assertRaises(ValueError):
                    world.resolver(bundle.worker_profile).assert_execution_profile(prepared.prepared())

    def test_new_work_waits_for_every_member_and_then_binds_only_the_v3_target(self) -> None:
        world = DutyWorld(self.v2.root / "scope")
        bundle = _v3(self.v2)
        open_bundle, prepared, _accepted = self._members(bundle, world)
        captured = datetime.fromisoformat(open_bundle.inventory["captured_at_utc"])
        new_v3 = world.duty(tag="new-v3", process_profile=bundle.process_profile, worker_profile=bundle.worker_profile)
        new_v2 = world.duty(tag="new-v2", process_profile=bundle.process_profile,
                            worker_profile=with_heavy_work_permit_form(bundle.worker_profile, None))
        repository = mock.Mock()
        repository.count_staged_prepared_heads.return_value = 0
        later = {item: captured + timedelta(seconds=1) for item in (new_v3.attempt_id, new_v2.attempt_id)}

        def scope(candidate: RecoveryBundle, *heads: Any) -> Any:
            execution = candidate.verify().execution
            assert execution is not None
            return verify_legacy_scope(repository, execution, observed_at=captured + timedelta(seconds=10),
                                       unresolved=heads, created_at_by_attempt=later)

        with self.assertRaisesRegex(LegacyExecutionRefused, "premature"):
            scope(open_bundle, prepared.prepared(), new_v3.prepared())
        empty = bundle.with_members([])
        self.assertEqual(scope(empty, new_v3.prepared()).current_execution_heads, (new_v3.attempt_id,))
        with self.assertRaises(LegacyExecutionRefused):
            scope(empty, new_v2.prepared())

    def test_substituted_profiles_hashes_forms_and_unnamed_axes_are_refused(self) -> None:
        bundle = _v3(self.v2)
        self.assertIsNotNone(bundle.verify().execution)
        forged_target = with_heavy_work_permit_form(bundle.worker_profile, 1)
        other_axis = replace(bundle.worker_profile, mac_finalize_workers=bundle.worker_profile.mac_finalize_workers + 1)
        anchor_beyond = "differs from the qualification anchor beyond the process-profile reference and its declared"
        origin_beyond = "differs from the recovery origin beyond the process-profile reference and its declared"
        cases = {
            "target hash": (bundle.with_proposal(lambda proposal: proposal["current_execution"].update(
                worker_profile_sha256=forged_target.sha256)), "composed worker profile is not the upgrade's target"),
            "declared target form": (bundle.with_proposal(lambda proposal: proposal["heavy_work_permits"].update(
                target=1)), "does not have the relation's declared heavy-work permit form"),
            "declared anchor form": (bundle.with_proposal(lambda proposal: proposal["heavy_work_permits"].update(
                anchor=2)), anchor_beyond),
            "declared origin form": (bundle.with_proposal(lambda proposal: proposal["heavy_work_permits"].update(
                origin=2)), origin_beyond),
            "origin hash": (bundle.with_proposal(lambda proposal: proposal["recovery_origin"].update(
                worker_profile_sha256=with_heavy_work_permit_form(bundle.origin_worker, 2).sha256)), origin_beyond),
            "anchor hash": (bundle.with_proposal(lambda proposal: proposal["qualification_anchor"].update(
                worker_profile_sha256=with_heavy_work_permit_form(self.parent.worker_profile, 2).sha256)),
                anchor_beyond),
            # Another worker-profile axis, consistently composed, pinned and reviewed: not the named delta.
            "finalize lanes": (bundle.with_settings(
                worker_finalize_concurrency=other_axis.mac_finalize_workers,
            ).with_proposal(lambda proposal: proposal["current_execution"].update(
                worker_profile_sha256=other_axis.sha256)), anchor_beyond),
            "same release, unmoved form": (_v3(self.v2, origin=None, target=None, same_release=True),
                                           "keeps its origin's release only when its heavy-work permit form moves"),
            "stale Q0": (bundle, "stale or drifted"),
        }
        for label, (candidate, reason) in cases.items():
            with self.subTest(case=label), self.assertRaisesRegex(gate.MinerUDeploymentGateError, reason):
                candidate.verify(now=stale_clock(self.parent) if label == "stale Q0" else None)
        archived = Path(bundle.proposal["recovery_origin"]["process_profile_file"])
        exact = archived.read_bytes()
        try:
            archived.write_bytes(exact + b"\n")
            with self.assertRaises(gate.MinerUDeploymentGateError):
                bundle.verify()
        finally:
            archived.write_bytes(exact)

    def test_steady_state_and_rollback_relations_verify(self) -> None:
        steady_origin = with_heavy_work_permit_form(self.v2.origin_worker, 2)
        for label, bundle in (
            ("later release keeps two", _v3(self.v2, origin=2, target=2)),
            ("configuration-only rollback", _v3(self.v2, origin=2, target=None, same_release=True)),
        ):
            with self.subTest(case=label):
                world = DutyWorld(self.v2.root / label.replace(" ", "-"))
                duty = world.duty(tag="member", process_profile=bundle.origin_profile, worker_profile=steady_origin)
                bundle = bundle.with_members([member_payload(duty.prepared())])
                execution = bundle.verify().execution
                assert execution is not None
                self.assertEqual(execution.member_worker_profile_sha256, steady_origin.sha256)
                world.resolver(bundle.worker_profile, legacy_execution=execution).assert_execution_profile(
                    duty.prepared())
                self.assertEqual(staged_v4_coordinator_limits(
                    bundle.process_profile, worker_profile=bundle.worker_profile,
                ).heavy_work_permits, 2 if heavy_work_permit_form(bundle.worker_profile) == 2 else 1)


class HeavyWorkPermitBuilderCliTests(unittest.TestCase):
    """Root's composition path: release-manifest, derive, propose (product builders), review, gate, preflight."""

    def setUp(self) -> None:
        self.parent = synthetic_parent_q0(self)
        self.v2 = build_recovery_v2(self.parent)
        self.root = self.v2.root / "cli"
        self.root.mkdir(mode=0o700)
        self.v2.settings.disclosure_runtime_root.chmod(0o700)
        world = DutyWorld(self.v2.root / "cli-duties")
        duty = world.duty(tag="member", process_profile=self.v2.origin_profile, worker_profile=self.v2.origin_worker)
        self.inventory = write_private(self.root / "inventory.json", exact_json({
            "contract_version": "worker-legacy-scope-inventory.v1",
            "captured_at_utc": self.parent.clock.isoformat(),
            "member_count": 1,
            "members": [member_payload(duty.prepared())],
        }))

    def _cli(self, argv: list[str], settings: Any) -> tuple[int, dict[str, Any]]:
        stdout = io.StringIO()
        with mock.patch.object(upgrade_cli, "load_settings", return_value=settings), redirect_stdout(stdout):
            code = upgrade_cli.main(argv)
        lines = stdout.getvalue().strip().splitlines()
        return code, json.loads(lines[-1]) if lines else {}

    @contextmanager
    def _ports(self, environment: dict[str, str]) -> Iterator[None]:
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(gate, "client_bundle_identity", return_value=self.parent.client),
            mock.patch.object(upgrade_runtime, "client_bundle_identity", return_value=self.parent.client),
            frozen_wall_clock(self.parent.clock),
        ):
            yield

    def _propose_argv(self, release: Path, derived: dict[str, Any], *, target: str = "2",
                      output: str = "proposal.json") -> list[str]:
        origin = self.v2.proposal["recovery_origin"]
        return [
            "propose", "--contract-version", "v3", "--release-manifest", str(release),
            "--runtime-bundle", derived["runtime_bundle_file"],
            "--anchor-process-profile", str(self.parent.process_profile_path),
            "--anchor-activation", str(self.parent.activation_path),
            "--origin-release-manifest", origin["release_manifest_file"],
            "--origin-runtime-bundle", origin["runtime_bundle_file"],
            "--origin-process-profile", origin["process_profile_file"],
            "--origin-activation", origin["stream_activation_file"],
            "--anchor-heavy-work-permits", "implied", "--origin-heavy-work-permits", "implied",
            "--target-heavy-work-permits", target,
            "--inventory", str(self.inventory),
            "--exact-change-manifest-sha256", sha256_bytes(b"exact change"),
            "--test-evidence-sha256", sha256_bytes(b"test evidence"),
            "--code-review-sha256", sha256_bytes(b"code review"),
            "--output", str(self.root / output),
        ]

    def test_root_builds_reviews_verifies_and_preflights_a_v3_relation(self) -> None:
        unconfigured = self.v2._settings(
            disclosure_worker_execution_upgrade_file=None, disclosure_worker_execution_upgrade_sha256=None,
            disclosure_worker_execution_upgrade_review_file=None,
            disclosure_worker_execution_upgrade_review_sha256=None,
        )
        release = self.root / "release.json"
        with self._ports(self.v2.environment):
            code, built = self._cli(["release-manifest", "--source-revision", "heavy-work-candidate",
                                     "--output", str(release)], unconfigured)
            self.assertEqual(code, 0, built)
            code, derived = self._cli(["derive", "--contract-version", "v3",
                                       "--anchor-process-profile", str(self.parent.process_profile_path),
                                       "--anchor-activation", str(self.parent.activation_path),
                                       "--output-dir", str(self.root / "derived")], unconfigured)
        self.assertEqual(code, 0, derived)
        # The writer did not move, so the derived target is the bundle's own R/P/A.
        self.assertEqual((derived["target_runtime_identity_sha256"], derived["process_profile_sha256"]),
                         (self.v2.current_runtime, self.v2.process_profile.sha256))
        environment = dict(self.v2.environment, DISCLOSURE_V4_PROCESS_PROFILE_FILE=derived["process_profile_file"],
                           DISCLOSURE_V4_PROCESS_PROFILE_SHA256=derived["process_profile_sha256"],
                           **{PERMITS_ENV: "2"})
        target = _with(
            unconfigured,
            disclosure_mineru_runtime_bundle_identity_sha256=derived["target_runtime_identity_sha256"],
            disclosure_mineru_stream_pressure_config=Path(derived["stream_activation_file"]),
            disclosure_mineru_stream_pressure_config_sha256=derived["stream_activation_sha256"],
        )
        with self._ports(environment):
            code, refused = self._cli(self._propose_argv(release, derived, target="1", output="wrong.json"), target)
            self.assertEqual((code, refused.get("kind")), (65, "identity"), refused)
            code, proposed = self._cli(self._propose_argv(release, derived), target)
        self.assertEqual(code, 0, proposed)
        self.assertEqual(
            (proposed["contract_version"], proposed["heavy_work_permits"], proposed["anchor_worker_profile_sha256"],
             proposed["origin_worker_profile_sha256"]),
            (V3, {"anchor": None, "origin": None, "target": 2}, self.parent.worker_profile.sha256,
             self.v2.origin_worker.sha256),
        )
        proposal_path = Path(proposed["output"])
        review = write_private(self.root / "review.json", exact_json({
            "contract_version": "worker-local-execution-upgrade-review.v1", "verdict": "GO",
            "proposal_sha256": sha256_bytes(proposal_path.read_bytes()),
            "reviewer_reference": "independent-heavy-work-reviewer", "decision_reference": "independent-decision",
        }))
        configured = _with(
            target,
            disclosure_worker_execution_upgrade_file=proposal_path,
            disclosure_worker_execution_upgrade_sha256=sha256_bytes(proposal_path.read_bytes()),
            disclosure_worker_execution_upgrade_review_file=review,
            disclosure_worker_execution_upgrade_review_sha256=sha256_bytes(review.read_bytes()),
        )

        def no_database() -> Any:
            raise RuntimeError("no database in the unit suite")

        with self._ports(environment):
            report = upgrade_runtime.run_deployment_preflight(
                configured, engine_factory=no_database, now=self.parent.clock,
                live_owner=lambda _url, _capacity: dict(self.parent.activation["owner"]),
            )
        self.assertEqual(
            (report["qualification_origin"], report["upgrade_contract_version"], report["heavy_work_permits"],
             report["anchor_qualified_at_utc"]),
            ("compatible_parent", V3, 2, json.loads(self.parent.canary_path.read_bytes())["passed_at_utc"]),
        )
        self.assertEqual(report["blockers"], ["legacy scope database unavailable (RuntimeError)"], report)
        self.assertIn("heavy-work permits: anchor:implied,origin:implied,target:2",
                      upgrade_runtime.render_preflight_terminal(report))

    def test_each_contract_version_takes_exactly_its_own_flags(self) -> None:
        base = ["--release-manifest", "/r", "--runtime-bundle", "/b", "--inventory", "/i",
                "--exact-change-manifest-sha256", "x", "--test-evidence-sha256", "x", "--code-review-sha256", "x",
                "--output", "/o"]
        v2_roles = ["--anchor-process-profile", "/p", "--anchor-activation", "/a", "--origin-release-manifest", "/r",
                    "--origin-runtime-bundle", "/b", "--origin-process-profile", "/p", "--origin-activation", "/a"]
        forms = ["--anchor-heavy-work-permits", "implied", "--origin-heavy-work-permits", "implied",
                 "--target-heavy-work-permits", "2"]
        for label, argv in (
            ("v3 without forms", ["propose", "--contract-version", "v3", *base, *v2_roles]),
            ("v2 with forms", ["propose", "--contract-version", "v2", *base, *v2_roles, *forms]),
            ("v1 with forms", ["propose", *base, "--parent-process-profile", "/p", "--parent-activation", "/a",
                               *forms]),
            ("count outside the contract", ["propose", "--contract-version", "v3", *base, *v2_roles,
                                            *forms[:-1], "3"]),
        ):
            with self.subTest(case=label), redirect_stdout(io.StringIO()), \
                    mock.patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(upgrade_cli.main(argv), 64)


if __name__ == "__main__":
    unittest.main()
