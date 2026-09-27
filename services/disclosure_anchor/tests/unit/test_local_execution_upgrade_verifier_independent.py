"""Independent acceptance: the one U01 verifier, positive edge and one-fact counterexamples.

Pro §C1/§C3 and root's mandatory acceptance. The reviewed edge relates the immutable parent
qualification Q0, the actual current execution E1 and the legacy inventory. It is accepted only
through ``verify_mineru_deployment_gate(..., accept_execution_upgrade=True)``, and only as an
inherited ``compatible_parent`` proof with the parent's original date and freshness.

Every bundle is derived independently (``tests._f5_upgrade_u01_fixture``). Each counterexample
changes exactly one fact, re-pins it and (unless the review itself is the subject) re-reviews it,
so the refusal belongs to that fact. Only the MinerU client-metadata port is replayed, from the
parent's own closed client section; the writer, E1, the parent receipts, the profiles and the
activations are all read and hashed for real.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import itertools
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime import mineru_deployment_gate as gate
from disclosure_anchor.adapters.runtime import mineru_execution_upgrade as upgrade_runtime
from disclosure_anchor.adapters.runtime.mineru_identity import canonical_payload_sha256, writer_code_digest
from disclosure_anchor.application.contracts.worker_execution_upgrade import VerifiedQualifiedExecution
from tests._f5_upgrade_q0_fixture import ParentQ0, actual_parent_q0, sha256_bytes, stale_clock, synthetic_parent_q0
from tests._f5_upgrade_u01_fixture import (
    U01Bundle,
    build_u01,
    exact_json,
    interpreter_package_identity,
    inventory_payload,
    release_scope_files,
    synthetic_member,
    write_private,
)


_UPGRADE_SETTINGS = (
    "disclosure_worker_execution_upgrade_file",
    "disclosure_worker_execution_upgrade_sha256",
    "disclosure_worker_execution_upgrade_review_file",
    "disclosure_worker_execution_upgrade_review_sha256",
)
_REFUSED = gate.MinerUDeploymentGateError


def _states(parent: ParentQ0) -> list[dict[str, Any]]:
    """One member per unresolved lifecycle state, bound to the parent pair."""

    states = ("prepared", "reconciling", "submitted", "remote_terminal", "materializing",
              "local_materialized", "publish_committed", "cleanup_pending", "ack_pending")
    return [
        synthetic_member(parent, index, state=state, lifecycle_version=index)
        for index, state in enumerate(states)
    ]


class _VerifierMatrix:
    parent_factory: Callable[[unittest.TestCase], ParentQ0]

    def _bundle(self, members: list[dict[str, Any]] | None = None) -> U01Bundle:
        parent = type(self).parent_factory(self)  # type: ignore[arg-type]
        return build_u01(parent, members=_states(parent) if members is None else members)

    def _refused(self, bundle: U01Bundle, pattern: str, **kwargs: Any) -> None:
        case: unittest.TestCase = self  # type: ignore[assignment]
        with case.assertRaisesRegex(_REFUSED, pattern):
            bundle.verify(**kwargs)

    # -- the positive edge -----------------------------------------------------------------

    def test_the_complete_reviewed_edge_verifies_as_an_inherited_parent_qualification(self) -> None:
        case: unittest.TestCase = self  # type: ignore[assignment]
        bundle = self._bundle()
        parent = bundle.parent
        evidence = bundle.verify()
        execution = evidence.execution
        case.assertIsInstance(execution, VerifiedQualifiedExecution)
        assert execution is not None
        case.assertEqual(evidence.qualification_origin, "compatible_parent")
        case.assertEqual(execution.qualification_origin, "compatible_parent")
        # Inherited, never refreshed: the parent's own canary date, age limit and slots.
        case.assertEqual(
            (evidence.runtime_identity_sha256, evidence.canary_passed_at_utc,
             evidence.canary_max_age_seconds, evidence.task_slots),
            (bundle.current_runtime, parent.canary_passed_at, parent.canary_max_age_seconds, parent.task_slots),
        )
        case.assertEqual(execution.parent_qualified_at, parent.canary_passed_at)
        summary = execution.summary()
        case.assertEqual(
            {key: summary[key] for key in (
                "parent_runtime_identity_sha256", "parent_writer_code_sha256", "parent_process_profile_sha256",
                "parent_worker_profile_sha256", "writer_code_sha256", "current_runtime_identity_sha256",
                "process_profile_sha256", "worker_profile_sha256", "stream_activation_sha256",
                "upgrade_sha256", "review_sha256", "legacy_inventory_sha256", "legacy_member_count",
            )},
            {
                "parent_runtime_identity_sha256": parent.runtime_identity,
                "parent_writer_code_sha256": parent.historical_writer,
                "parent_process_profile_sha256": parent.process_profile.sha256,
                "parent_worker_profile_sha256": parent.worker_profile.sha256,
                "writer_code_sha256": bundle.current_writer,
                "current_runtime_identity_sha256": bundle.current_runtime,
                "process_profile_sha256": bundle.process_profile.sha256,
                "worker_profile_sha256": bundle.worker_profile.sha256,
                "stream_activation_sha256": bundle.settings.disclosure_mineru_stream_pressure_config_sha256,
                "upgrade_sha256": bundle.proposal_sha256,
                "review_sha256": bundle.review_sha256,
                "legacy_inventory_sha256": bundle.inventory_sha256,
                "legacy_member_count": len(bundle.inventory["members"]),
            },
        )
        case.assertEqual(summary["parent_qualified_at_utc"], json.loads(parent.canary_path.read_bytes())["passed_at_utc"])
        case.assertEqual(execution.member_attempt_ids, tuple(item["attempt_id"] for item in bundle.inventory["members"]))
        # The independently derived identities agree with the product's own recomputation.
        case.assertEqual(bundle.current_writer, writer_code_digest())
        case.assertNotEqual(bundle.current_writer, parent.historical_writer)
        case.assertEqual(bundle.current_runtime, canonical_payload_sha256(bundle.current_manifest))
        checker = bundle.checker()
        case.assertEqual(checker.qualification_origin, "compatible_parent")
        verified = checker.verified_execution
        assert verified is not None
        case.assertEqual((verified.upgrade_sha256, verified.inventory), (execution.upgrade_sha256, execution.inventory))

    def test_verification_reads_every_input_without_writing_any(self) -> None:
        case: unittest.TestCase = self  # type: ignore[assignment]
        bundle = self._bundle()

        def snapshot() -> dict[str, tuple[bytes, int, int]]:
            found = {}
            for path in sorted(bundle.root.rglob("*")):
                if path.is_file():
                    info = path.stat()
                    found[str(path)] = (path.read_bytes(), info.st_mode, info.st_mtime_ns)
            return found

        before = snapshot()
        bundle.verify()
        case.assertEqual(snapshot(), before)

    # -- entries and configuration -----------------------------------------------------------

    def test_without_the_upgrade_the_current_configuration_is_refused_by_the_exact_path(self) -> None:
        case: unittest.TestCase = self  # type: ignore[assignment]
        bundle = self._bundle()
        current_only = bundle.with_settings(**{name: None for name in _UPGRADE_SETTINGS})
        case.assertFalse(current_only.settings.execution_upgrade_configured)
        self._refused(current_only, "MinerU exact runtime identity cannot be verified")

    def test_an_entry_that_cannot_carry_the_context_refuses_a_configured_upgrade(self) -> None:
        case: unittest.TestCase = self  # type: ignore[assignment]
        bundle = self._bundle()
        with bundle.active():
            with case.assertRaisesRegex(_REFUSED, "this entry cannot carry its legacy authorization"):
                gate.verify_mineru_deployment_gate(
                    bundle.settings, parse_enabled=True, process_profile=bundle.process_profile,
                    now=bundle.parent.clock,
                )
        with case.assertRaisesRegex(_REFUSED, "this entry cannot carry its legacy authorization"):
            bundle.checker(accept_execution_upgrade=False)

    def test_every_partial_set_of_upgrade_settings_is_refused(self) -> None:
        bundle = self._bundle()
        for size in (1, 2, 3):
            for kept in itertools.combinations(_UPGRADE_SETTINGS, size):
                with self.subTest(kept=kept):  # type: ignore[attr-defined]
                    partial = bundle.with_settings(**{name: None for name in _UPGRADE_SETTINGS if name not in kept})
                    self._refused(partial, "needs its proposal and review files with pinned SHA-256 together")

    def test_the_parent_configuration_cannot_run_under_the_upgrade(self) -> None:
        bundle = self._bundle()
        parent = bundle.parent
        old = bundle.with_settings(
            disclosure_mineru_runtime_bundle_identity_sha256=parent.runtime_identity,
            disclosure_mineru_stream_pressure_config=parent.activation_path,
            disclosure_mineru_stream_pressure_config_sha256=parent.file_sha256(parent.activation_path),
        )
        pattern = "is not the upgrade's current execution"
        self._refused(old, pattern, process_profile=parent.process_profile)
        self._refused(bundle, pattern, process_profile=parent.process_profile)
        self._refused(bundle.with_settings(
            disclosure_mineru_stream_pressure_config=parent.activation_path,
            disclosure_mineru_stream_pressure_config_sha256=parent.file_sha256(parent.activation_path),
        ), pattern)

    # -- proposal and GO review -----------------------------------------------------------

    def test_only_a_go_review_of_these_exact_proposal_bytes_is_accepted(self) -> None:
        bundle = self._bundle()
        other = sha256_bytes(b"another proposal")
        self._refused(bundle.with_review(lambda review: review.update(proposal_sha256=other)),
                      "does not approve this exact upgrade proposal")
        for field, value in (("verdict", "NO-GO"), ("contract_version", "worker-local-execution-upgrade-review.v0")):
            with self.subTest(field=field):  # type: ignore[attr-defined]
                self._refused(bundle.with_review(lambda review: review.update({field: value})),
                              "not a GO review of this contract")
        self._refused(bundle.with_review(lambda review: review.update(note="extra")), "fields are not closed")
        self._refused(bundle.with_settings(disclosure_worker_execution_upgrade_review_sha256=other),
                      "review differs from its pinned sha256")
        self._refused(bundle.with_proposal(lambda proposal: proposal.update(note="extra")), "fields are not closed")
        self._refused(bundle.with_proposal(lambda proposal: proposal.update(transition_kind="refresh")),
                      "transition kind is unsupported")
        self._refused(bundle.with_settings(disclosure_worker_execution_upgrade_sha256=other),
                      "upgrade differs from its pinned sha256")
        # A proposal changed after review keeps the old GO review: refused.
        self._refused(bundle.with_proposal(
            lambda proposal: proposal["compatibility_basis"].update(
                independent_code_review_sha256=sha256_bytes(b"another review")),
            reviewed=False,
        ), "does not approve this exact upgrade proposal")

    def test_upgrade_artifacts_must_be_owner_only_files(self) -> None:
        bundle = self._bundle()
        for path in (bundle.proposal_path, bundle.review_path, bundle.inventory_path, bundle.release_path):
            with self.subTest(path=path.name):  # type: ignore[attr-defined]
                path.chmod(0o644)
                try:
                    self._refused(bundle, "must be an owner-only 0600 regular file with one link")
                finally:
                    path.chmod(0o600)

    # -- the immutable parent qualification --------------------------------------------------

    def test_the_parent_receipts_are_pinned_and_keep_their_original_freshness(self) -> None:
        bundle = self._bundle()
        parent = bundle.parent
        wrong = sha256_bytes(b"not the parent")
        for field, pattern in (
            ("smoke_receipt_sha256", "parent smoke receipt differs from its pinned sha256"),
            ("canary_cache_sha256", "parent canary cache differs from its pinned sha256"),
            ("validation_receipt_sha256", "parent held-out receipt differs from its pinned sha256"),
            ("runtime_identity_sha256", "process profiles do not bind the upgrade's parent/current runtime"),
            ("writer_code_sha256", "runtime manifest local writer code drifted"),
            ("service_epoch_sha256", "parent service epoch differs from the held-out receipt"),
        ):
            with self.subTest(field=field):  # type: ignore[attr-defined]
                self._refused(bundle.with_proposal(lambda proposal: proposal["parent_qualification"].update(
                    {field: wrong})), pattern)
        self._refused(bundle.with_proposal(lambda proposal: proposal["parent_qualification"].update(
            qualified_at_utc="2026-09-14T11:59:02+00:00")), "not the canary's original pass time")
        # Freshness is the parent's own: one second past its original window is stale.
        self._refused(bundle, "stale", now=stale_clock(parent))

    def test_a_rewritten_parent_receipt_is_refused_even_when_re_pinned(self) -> None:
        bundle = self._bundle()
        parent = bundle.parent
        heldout = json.loads(parent.heldout_path.read_bytes())
        heldout["policy"] = "operator-held-out-complete-pdf.v0"
        rewritten = write_private(parent.root / "heldout-rewritten.json", exact_json(heldout))
        repinned = bundle.with_settings(disclosure_mineru_validation_receipt=rewritten).with_proposal(
            lambda proposal: proposal["parent_qualification"].update(
                validation_receipt_sha256=sha256_bytes(rewritten.read_bytes())))
        self._refused(repinned, "held-out validation is not PASS")
        canary = json.loads(parent.canary_path.read_bytes())
        canary["passed_at_utc"] = parent.clock.isoformat()
        refreshed = write_private(parent.root / "canary-refreshed.json", exact_json(canary))
        repinned = bundle.with_settings(disclosure_mineru_canary_cache=refreshed).with_proposal(
            lambda proposal: proposal["parent_qualification"].update(
                canary_cache_sha256=sha256_bytes(refreshed.read_bytes()),
                qualified_at_utc=canary["passed_at_utc"]))
        self._refused(repinned, "canary evidence drifted")

    def test_the_parent_profiles_and_activation_are_exact(self) -> None:
        bundle = self._bundle()
        wrong = sha256_bytes(b"not the parent")
        for field, pattern in (
            ("process_profile_sha256", "parent process profile: MinerU process profile hash differs"),
            ("worker_profile_sha256", "differs from the parent beyond the process-profile reference"),
            ("stream_activation_sha256", "stream activation: MinerU capacity file hash differs"),
        ):
            with self.subTest(field=field):  # type: ignore[attr-defined]
                self._refused(bundle.with_proposal(lambda proposal: proposal["parent_qualification"].update(
                    {field: wrong})), pattern)

    # -- E1: every loadable byte ---------------------------------------------------------

    def test_every_current_source_byte_is_pinned_including_bytes_outside_the_legacy_writer_set(self) -> None:
        bundle = self._bundle()
        outside_writer = (
            "src/disclosure_anchor/cli/worker.py",
            "src/disclosure_anchor/application/contracts/worker_execution_upgrade.py",
            "src/disclosure_anchor/adapters/db/postgres/staged_upgrade_scope_v4.py",
            "scripts/install_launchd.sh",
        )
        paths = {item["path"] for item in bundle.release["files"]}
        launchd = sorted(path for path in paths if path.startswith("scripts/launchd/"))
        case: unittest.TestCase = self  # type: ignore[assignment]
        case.assertTrue(launchd, "scripts/launchd is inside E1")
        for relpath in (*outside_writer, launchd[0]):
            case.assertIn(relpath, paths)

            def change(release: dict[str, Any], relpath: str = relpath) -> None:
                for item in release["files"]:
                    if item["path"] == relpath:
                        item["sha256"] = sha256_bytes(b"other bytes")

            with self.subTest(changed=relpath):  # type: ignore[attr-defined]
                self._refused(bundle.with_release(change), r"execution release differs from E1: missing=0 extra=0 changed=1")

        def omit(release: dict[str, Any]) -> None:
            release["files"] = [item for item in release["files"] if item["path"] != outside_writer[2]]

        def add(release: dict[str, Any]) -> None:
            release["files"].append({"path": "src/disclosure_anchor/zz_absent.py", "sha256": sha256_bytes(b""), "bytes": 0})

        self._refused(bundle.with_release(omit), r"missing=0 extra=1 changed=0")
        self._refused(bundle.with_release(add), r"missing=1 extra=0 changed=0")

    def test_the_writer_packages_and_revision_are_recomputed_not_declared(self) -> None:
        bundle = self._bundle()
        other = sha256_bytes(b"declared writer")
        both = bundle.with_release(lambda release: release.update(writer_code_sha256=other)).with_proposal(
            lambda proposal: proposal["current_execution"].update(writer_code_sha256=other))
        self._refused(both, "execution release writer differs from E1")
        self._refused(bundle.with_release(lambda release: release.update(writer_code_sha256=other)),
                      "execution release manifest is not the proposal's E1")
        self._refused(bundle.with_release(lambda release: release.update(source_revision="another-revision")),
                      "execution release manifest is not the proposal's E1")
        self._refused(bundle.with_release(lambda release: release.update(worker_package_set_sha256=other)),
                      "worker interpreter packages differ from E1")
        self._refused(bundle.with_release(lambda release: release.update(worker_python_version="3.0.0")),
                      "worker interpreter packages differ from E1")

    def test_one_edge_only_no_transitive_chain(self) -> None:
        bundle = self._bundle()
        parent = bundle.parent
        for section, field, value in (
            ("current_execution", "writer_code_sha256", parent.historical_writer),
            ("current_execution", "runtime_identity_sha256", parent.runtime_identity),
            ("current_execution", "process_profile_sha256", parent.process_profile.sha256),
            ("current_execution", "worker_profile_sha256", parent.worker_profile.sha256),
            ("current_execution", "stream_activation_sha256", parent.file_sha256(parent.activation_path)),
        ):
            with self.subTest(field=field):  # type: ignore[attr-defined]
                self._refused(bundle.with_proposal(lambda proposal: proposal[section].update({field: value})),
                              "must change exactly the local execution identities")

    # -- M1: the same computation ----------------------------------------------------------

    def test_the_current_runtime_may_differ_from_the_parent_only_by_the_local_writer(self) -> None:
        bundle = self._bundle()

        def set_path(path: tuple[str, ...], value: object) -> Callable[[dict[str, Any]], None]:
            def mutate(manifest: dict[str, Any]) -> None:
                node = manifest
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = value
            return mutate

        manifest = bundle.current_manifest
        beyond_writer = "current runtime differs from the parent beyond the local writer"
        profile_identity = "staged V4 process profile identity differs from the attested runtime manifest"
        cases = {
            # Facts only the parent/current relation can see.
            "model": (("inference_server", "served_model_id"), manifest["inference_server"]["served_model_id"] + "-x",
                      beyond_writer),
            "collector": (("topology", "windows_collector_sha256"), sha256_bytes(b"collector"), beyond_writer),
            # Facts the current runtime's own checks already bind.
            "orchestrator image": (("orchestrator", "container_image_digest"), sha256_bytes(b"image"),
                                   profile_identity + ": orchestrator_image_identity_sha256"),
            "inference image": (("inference_server", "container_image_digest"), sha256_bytes(b"image"),
                                profile_identity + ": inference_image_identity_sha256"),
            "engine command": (("inference_server", "command"),
                               [*manifest["inference_server"]["command"], "--enforce-eager"],
                               profile_identity + ": vllm_engine_args_sha256"),
            "client packages": (("client", "package_set_sha256"), sha256_bytes(b"packages"),
                                "runtime manifest local client digest is stale"),
        }
        for label, (path, value, pattern) in cases.items():
            with self.subTest(changed=label):  # type: ignore[attr-defined]
                self._refused(bundle.with_runtime_manifest(set_path(path, value)), pattern)

    def test_capacity_is_the_explicit_selection_of_the_proposal(self) -> None:
        bundle = self._bundle()
        self._refused(bundle.with_proposal(lambda proposal: proposal["current_execution"].update(
            capacity_config_sha256=sha256_bytes(b"another capacity"))), "is not the upgrade's current execution")

    # -- P1 / WP1 / A1 -----------------------------------------------------------------------

    def test_p1_moves_only_its_runtime_reference(self) -> None:
        bundle = self._bundle()
        profile = bundle.process_profile
        for field, value in (
            ("temporary_disk_bytes_limit", profile.temporary_disk_bytes_limit + 1),
            ("vllm_gpu_memory_utilization_millionths", profile.vllm_gpu_memory_utilization_millionths - 1),
            ("resident_pages_limit", profile.resident_pages_limit + 1),
        ):
            with self.subTest(field=field):  # type: ignore[attr-defined]
                self._refused(bundle.with_process_profile(replace(profile, **{field: value})),
                              "changes a physical ceiling; only the runtime reference may move")

    def test_wp1_moves_only_its_process_profile_reference(self) -> None:
        bundle = self._bundle()
        changed = replace(bundle, environment=dict(bundle.environment, DISCLOSURE_V4_COMMIT_STAGE_SECONDS="3599"))
        self._refused(changed, "composed worker profile is not the upgrade's current WP1")
        worker = replace(bundle.worker_profile, commit_stage_seconds=3599)
        repinned = changed.with_proposal(lambda proposal: proposal["current_execution"].update(
            worker_profile_sha256=worker.sha256))
        self._refused(repinned, "differs from the parent beyond the process-profile reference")

    def test_a1_moves_only_its_runtime_references_and_keeps_the_qualified_owner(self) -> None:
        bundle = self._bundle()

        def changed(mutate: Callable[[dict[str, Any]], None]) -> U01Bundle:
            activation = json.loads(json.dumps(bundle.activation))
            mutate(activation)
            return bundle.with_activation(activation)

        def other_owner(item: dict[str, Any]) -> None:
            item["owner"]["process_id"] += 1
            item["policy"]["owner_identity_sha256"] = sha256_bytes(exact_json(item["owner"]))

        pattern = "only the runtime may move"
        self._refused(changed(lambda item: item.update(gpu_uuid="GPU-00000000-abcd-4321-9876-abcdef123456")), pattern)
        self._refused(changed(lambda item: item.update(cgroup_max_bytes=item["cgroup_max_bytes"] + 1)), pattern)
        self._refused(changed(lambda item: item["policy"].update(gpu_pause_bytes=item["policy"]["gpu_pause_bytes"] + 1)),
                      pattern)
        self._refused(changed(other_owner), pattern)
        self._refused(changed(lambda item: item["owner"].update(process_id=item["owner"]["process_id"] + 1)),
                      "stream policy differs from runtime/owner binding")
        self._refused(changed(lambda item: item.update(runtime_identity_sha256=bundle.parent.runtime_identity)),
                      "stream activation runtime differs from selected identity")
        # A0 and A1 agree with each other but name another owner than Q0's held-out receipts.
        parent_activation = json.loads(json.dumps(bundle.parent.activation))
        other_owner(parent_activation)
        parent_path = write_private(bundle.root / "activation-A0-other-owner.json", exact_json(parent_activation))
        current_activation = json.loads(json.dumps(bundle.activation))
        other_owner(current_activation)
        self._refused(bundle.with_proposal(lambda proposal: proposal["parent_qualification"].update(
            stream_activation_file=str(parent_path), stream_activation_sha256=sha256_bytes(parent_path.read_bytes()),
        )).with_activation(current_activation), "not the native owner the parent qualification recorded")

    # -- the legacy inventory --------------------------------------------------------------

    def test_the_inventory_is_pinned_counted_and_closed(self) -> None:
        bundle = self._bundle()
        members = list(bundle.inventory["members"])
        self._refused(bundle.with_proposal(lambda proposal: proposal["legacy_scope"].update(
            inventory_sha256=sha256_bytes(b"another inventory"))), "legacy scope inventory differs from its pinned sha256")
        self._refused(bundle.with_proposal(lambda proposal: proposal["legacy_scope"].update(
            member_count=len(members) + 1)), "inventory disagrees with the proposal")
        payload = inventory_payload(members, captured_at_utc=bundle.inventory["captured_at_utc"])
        payload["member_count"] = len(members) - 1
        self._refused(bundle.with_inventory_payload(payload, count=len(members)), "member count disagrees with its members")
        duplicate = [*members, dict(members[0])]
        self._refused(bundle.with_inventory_payload(inventory_payload(
            duplicate, captured_at_utc=bundle.inventory["captured_at_utc"])), "unique and sorted by attempt")
        unsorted = inventory_payload(members, captured_at_utc=bundle.inventory["captured_at_utc"])
        unsorted["members"] = list(reversed(unsorted["members"]))
        self._refused(bundle.with_inventory_payload(unsorted), "unique and sorted by attempt")
        for field in ("fence_identity", "client_submit_key", "h0_checkpoint_sha256", "execution_spec_sha256"):
            with self.subTest(repeated=field):  # type: ignore[attr-defined]
                twin = [dict(members[0]), dict(members[1], **{field: members[0][field]}), *members[2:]]
                self._refused(bundle.with_members(twin), f"repeats a member {field}")
        extra = [dict(members[0], note="x"), *members[1:]]
        self._refused(bundle.with_members(extra), "fields are not closed")
        final = [dict(members[0], observed_state="acked"), *members[1:]]
        self._refused(bundle.with_members(final), "not an unresolved responsibility")

    def test_every_member_binds_the_single_parent_profile_pair(self) -> None:
        bundle = self._bundle()
        members = list(bundle.inventory["members"])
        for field, value in (
            ("runtime_epoch_sha256", bundle.current_runtime),
            ("process_profile_sha256", bundle.process_profile.sha256),
            ("worker_profile_sha256", bundle.worker_profile.sha256),
        ):
            with self.subTest(field=field):  # type: ignore[attr-defined]
                stray = [dict(members[0], **{field: value}), *members[1:]]
                self._refused(bundle.with_members(stray), "not bound to the single verified parent profile pair")

    def test_the_scope_has_no_fixed_member_cap(self) -> None:
        case: unittest.TestCase = self  # type: ignore[assignment]
        parent = type(self).parent_factory(self)  # type: ignore[arg-type]
        members = [synthetic_member(parent, index, state=("prepared", "submitted")[index % 2])
                   for index in range(1000)]
        execution = build_u01(parent, members=members).verify().execution
        assert execution is not None
        case.assertEqual(len(execution.member_attempt_ids), 1000)
        case.assertEqual(len(set(execution.member_attempt_ids)), 1000)


class SyntheticParentVerifierTests(_VerifierMatrix, unittest.TestCase):
    """Default suite: the authored synthetic parent."""

    parent_factory = staticmethod(synthetic_parent_q0)


class ActualParentVerifierTests(_VerifierMatrix, unittest.TestCase):
    """Opt-in: the byte-exact production parent from ``F5_UPGRADE_ACTUAL_Q0_ROOT``."""

    parent_factory = staticmethod(actual_parent_q0)


class ExecutionReleaseScopeTests(unittest.TestCase):
    """E1's file scanner over the real tree and over disposable trees (no product file touched)."""

    def test_the_release_scope_equals_an_independent_scan_of_this_tree(self) -> None:
        product = [
            {"path": item.path, "sha256": item.sha256, "bytes": item.bytes}
            for item in upgrade_runtime.release_files()
        ]
        self.assertEqual(product, release_scope_files())
        paths = {item["path"] for item in product}
        self.assertTrue({"scripts/install_launchd.sh", "src/disclosure_anchor/cli/worker.py"} <= paths)
        self.assertFalse(any("__pycache__" in path for path in paths))
        self.assertEqual(upgrade_runtime.worker_python_identity(), interpreter_package_identity())

    def _tree(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="f5-e1-", dir=Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(shutil.rmtree, root, True)
        package = root / "src" / "disclosure_anchor"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "module.py").write_text("VALUE = 1\n")
        (root / "scripts" / "launchd").mkdir(parents=True)
        (root / "scripts" / "install.sh").write_text("#!/bin/zsh\n")
        (root / "scripts" / "launchd" / "job.plist").write_text("<plist/>\n")
        return root

    def _manifest(self, root: Path) -> Any:
        return upgrade_runtime.build_execution_release_manifest(source_revision="tree", service_root=root)

    def test_changed_extra_and_missing_files_are_refused(self) -> None:
        cases = {
            "changed module byte": lambda root: (root / "src/disclosure_anchor/module.py").write_text("VALUE = 2\n"),
            "changed script": lambda root: (root / "scripts/install.sh").write_text("#!/bin/sh\n"),
            "changed launchd file": lambda root: (root / "scripts/launchd/job.plist").write_text("<plist></plist>\n"),
            "extra module": lambda root: (root / "src/disclosure_anchor/extra.py").write_text("X = 1\n"),
            "extra shared library": lambda root: (root / "src/disclosure_anchor/ext.so").write_bytes(b"\x7fELF"),
            "extra script": lambda root: (root / "scripts/extra.py").write_text("X = 1\n"),
            "missing module": lambda root: (root / "src/disclosure_anchor/module.py").unlink(),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                root = self._tree()
                manifest = self._manifest(root)
                upgrade_runtime.verify_execution_release(manifest, service_root=root)
                mutate(root)
                with self.assertRaisesRegex(_REFUSED, "execution release differs from E1"):
                    upgrade_runtime.verify_execution_release(manifest, service_root=root)

    def test_an_unknown_script_directory_is_refused_and_the_windows_node_tree_is_outside_e1(self) -> None:
        root = self._tree()
        (root / "scripts" / "tools").mkdir()
        (root / "scripts" / "tools" / "helper.py").write_text("X = 1\n")
        with self.assertRaisesRegex(_REFUSED, "refuses an unknown directory: scripts/tools"):
            upgrade_runtime.release_files(root)
        root = self._tree()
        (root / "scripts" / "windows").mkdir()
        (root / "scripts" / "windows" / "collector.py").write_text("X = 1\n")
        self.assertFalse(any(item.path.startswith("scripts/windows") for item in upgrade_runtime.release_files(root)))

    def test_symlinks_are_refused_never_followed_or_skipped(self) -> None:
        for label, link in (
            ("file", lambda root: (root / "src/disclosure_anchor/alias.py").symlink_to(root / "src/disclosure_anchor/module.py")),
            ("directory", lambda root: (root / "src/disclosure_anchor/sub").symlink_to(root / "scripts/launchd")),
            ("flat script", lambda root: (root / "scripts/alias.sh").symlink_to(root / "scripts/install.sh")),
        ):
            with self.subTest(link=label):
                root = self._tree()
                link(root)
                with self.assertRaisesRegex(_REFUSED, "refuses a (non-regular entry|symlinked directory|symlink)"):
                    upgrade_runtime.release_files(root)

    def test_a_sourceless_bytecode_module_is_not_ignored(self) -> None:
        # CPython imports a legacy-location ``name.pyc`` without source (SourcelessFileLoader), so
        # it is an executable member of the package and must be pinned or refused, never skipped.
        root = self._tree()
        manifest = self._manifest(root)
        package = root / "src" / "disclosure_anchor"
        source = package / "stray.py"
        source.write_text("VALUE = 'executed-without-source'\n")
        py_compile.compile(str(source), cfile=str(package / "stray.pyc"), doraise=True)
        source.unlink()
        probe = subprocess.run(
            [sys.executable, "-I", "-B", "-c",
             f"import sys; sys.path.insert(0, {str(root / 'src')!r}); "
             "import disclosure_anchor.stray as m; print(m.VALUE)"],
            capture_output=True, text=True, check=False, timeout=60,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        self.assertEqual(probe.stdout.strip(), "executed-without-source", probe.stderr)
        listed = False
        try:
            listed = any(item.path.endswith("stray.pyc") for item in upgrade_runtime.release_files(root))
            upgrade_runtime.verify_execution_release(manifest, service_root=root)
        except _REFUSED:
            return
        self.fail(f"an importable sourceless module was ignored by E1 (listed={listed})")


class BootReceiptTests(unittest.TestCase):
    """Root's per-boot proof: one create-only, owner-only receipt binding U01, E1, profiles and owner."""

    def test_the_boot_receipt_is_one_create_only_owner_only_file(self) -> None:
        from disclosure_anchor.adapters.db.postgres.staged_upgrade_scope_v4 import LegacyScopeObservation

        parent = synthetic_parent_q0(self)
        bundle = build_u01(parent, members=_states(parent))
        execution = bundle.verify().execution
        assert execution is not None
        runtime_root = bundle.settings.disclosure_runtime_root
        runtime_root.chmod(0o700)
        members = execution.member_attempt_ids
        scope = LegacyScopeObservation(
            observed_at=parent.clock, unresolved_members=members[:4], closed_members=members[4:],
            state_counts=(("prepared", 4),), current_execution_heads=(),
        )
        owner = "staged-v4-independent-receipt-owner"
        path, digest, line = upgrade_runtime.write_boot_receipt(
            bundle.settings, execution, owner_identity=owner, scope=scope, booted_at=parent.clock,
        )
        self.assertEqual(path, runtime_root / "reports" / "execution-boot" / f"{owner}.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(sha256_bytes(path.read_bytes()), digest)
        payload = json.loads(path.read_bytes())
        self.assertEqual(
            {key: payload[key] for key in (
                "contract_version", "owner_identity", "qualification_origin", "upgrade_sha256", "review_sha256",
                "release_manifest_sha256", "writer_code_sha256", "parent_writer_code_sha256",
                "runtime_identity_sha256", "parent_runtime_identity_sha256", "process_profile_sha256",
                "worker_profile_sha256", "parent_qualified_at_utc", "legacy_member_count")},
            {
                "contract_version": "worker-execution-boot-receipt.v1", "owner_identity": owner,
                "qualification_origin": "compatible_parent", "upgrade_sha256": bundle.proposal_sha256,
                "review_sha256": bundle.review_sha256,
                "release_manifest_sha256": sha256_bytes(bundle.release_path.read_bytes()),
                "writer_code_sha256": bundle.current_writer, "parent_writer_code_sha256": parent.historical_writer,
                "runtime_identity_sha256": bundle.current_runtime,
                "parent_runtime_identity_sha256": parent.runtime_identity,
                "process_profile_sha256": bundle.process_profile.sha256,
                "worker_profile_sha256": bundle.worker_profile.sha256,
                "parent_qualified_at_utc": json.loads(parent.canary_path.read_bytes())["passed_at_utc"],
                "legacy_member_count": len(members),
            },
        )
        self.assertEqual((payload["scope"]["unresolved_member_count"], payload["scope"]["closed_member_count"]),
                         (4, len(members) - 4))
        self.assertIn(str(path), line)
        self.assertIn(digest, line)
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            upgrade_runtime.write_boot_receipt(bundle.settings, execution, owner_identity=owner, scope=scope)
        self.assertEqual(path.read_bytes(), before, "a receipt is never overwritten")
        self.assertEqual([item.name for item in path.parent.iterdir()], [path.name], "no partial sibling remains")


class ConfiguredEntryTests(unittest.TestCase):
    """Doctor's entry verifies exactly the resident loop's gate call and reports inheritance."""

    def test_doctor_reports_the_inherited_parent_never_a_new_pass(self) -> None:
        from disclosure_anchor.adapters.runtime.doctor import worker_execution_upgrade_check

        parent = synthetic_parent_q0(self)
        bundle = build_u01(parent, members=_states(parent))
        with bundle.active():
            execution = upgrade_runtime.verify_configured_execution_upgrade(bundle.settings)
            result, reported = worker_execution_upgrade_check(bundle.settings)
        self.assertEqual(execution.upgrade_sha256, bundle.proposal_sha256)
        assert result is not None and reported is not None
        self.assertEqual(result.status, "PASS")
        self.assertIn("compatible_parent (inherited)", result.message)
        self.assertIn(f"parent_qualified_at={json.loads(parent.canary_path.read_bytes())['passed_at_utc']}", result.message)
        exact = bundle.with_settings(**{name: None for name in _UPGRADE_SETTINGS})
        self.assertEqual(worker_execution_upgrade_check(exact.settings), (None, None))
        with bundle.active(), mock.patch.dict(os.environ, {"DISCLOSURE_V4_COMMIT_STAGE_SECONDS": "3599"}):
            failed, none = worker_execution_upgrade_check(bundle.settings)
        assert failed is not None
        self.assertEqual((failed.status, none), ("FAIL", None))


if __name__ == "__main__":
    unittest.main()
