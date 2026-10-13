"""Independent acceptance for the F5 local execution upgrade: a historical parent Q0 with new code.

Pro §C1.1–C1.3 and root's mandatory acceptance. A parent qualification whose runtime manifest pins
a historical writer, paired with the new code, must fail the real deployment checker when no
upgrade binding exists. The legacy 42-member writer digest stays exactly as it was. The parent
keeps its original date, freshness and explicit v11 capacity.

Only the external MinerU client-metadata port is replayed, and only from the parent's own closed
client section. ``writer_code_digest`` and every verifier run for real.

The default suite runs these checks on an authored synthetic parent (``synthetic_parent_q0``).
The byte-exact actual parent stays outside Git; ``ActualParentBaselineTests`` runs only with an
explicit ``F5_UPGRADE_ACTUAL_Q0_ROOT``.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime import mineru_deployment_gate as gate
from disclosure_anchor.adapters.runtime import mineru_identity
from disclosure_anchor.adapters.runtime.mineru_identity import (
    canonical_payload_sha256,
    verify_runtime_manifest_payload,
)
from disclosure_anchor.application.contracts.mineru_capacity_config import decode_mineru_capacity_config
from tests._f5_upgrade_q0_fixture import (
    ACTUAL_R0,
    ACTUAL_SHA256,
    ACTUAL_W0,
    LEGACY_WRITER_MEMBERS,
    ParentQ0,
    actual_parent_q0,
    legacy_writer_digest,
    sha256_bytes,
    stale_clock,
    synthetic_parent_q0,
)


class _ParentBaseline:
    """The same real-checker assertions for any installed parent."""

    parent_factory: Callable[[unittest.TestCase], ParentQ0]

    def _parent(self) -> ParentQ0:
        return type(self).parent_factory(self)  # type: ignore[arg-type]

    def _checker(self, parent: ParentQ0) -> gate.MinerUDeploymentChecker:
        return gate.MinerUDeploymentChecker(
            parent.settings, parse_enabled=True, process_profile=parent.process_profile,
            wall_clock=lambda: parent.clock,
        )

    def test_new_code_with_the_parent_is_refused_by_the_real_checker_without_a_binding(self) -> None:
        case: unittest.TestCase = self  # type: ignore[assignment]
        parent = self._parent()
        case.assertNotEqual(mineru_identity.writer_code_digest(), parent.historical_writer)
        with (
            mock.patch.object(gate, "client_bundle_identity", return_value=parent.client),
            case.assertRaisesRegex(gate.MinerUDeploymentGateError, "runtime manifest local writer code drifted"),
        ):
            self._checker(parent)

    def test_the_same_parent_passes_the_same_checker_only_under_its_historical_writer(self) -> None:
        # Control: every other byte of the parent is valid, so the refusal is the writer alone.
        case: unittest.TestCase = self  # type: ignore[assignment]
        parent = self._parent()
        with (
            mock.patch.object(gate, "client_bundle_identity", return_value=parent.client),
            mock.patch.object(gate, "writer_code_digest", return_value=parent.historical_writer),
        ):
            checker = self._checker(parent)
            evidence = gate.verify_mineru_deployment_gate(
                parent.settings, parse_enabled=True, process_profile=parent.process_profile, now=parent.clock,
            )
        assert evidence is not None
        case.assertEqual(checker.expected_capacity,
                         decode_mineru_capacity_config(parent.capacity_path.read_bytes()))
        case.assertEqual(
            (evidence.runtime_identity_sha256, evidence.canary_passed_at_utc, evidence.task_slots,
             evidence.canary_max_age_seconds),
            (parent.runtime_identity, parent.canary_passed_at, parent.task_slots, parent.canary_max_age_seconds),
        )

    def test_the_parent_keeps_its_original_date_and_freshness(self) -> None:
        case: unittest.TestCase = self  # type: ignore[assignment]
        parent = self._parent()
        with (
            mock.patch.object(gate, "client_bundle_identity", return_value=parent.client),
            mock.patch.object(gate, "writer_code_digest", return_value=parent.historical_writer),
            case.assertRaisesRegex(gate.MinerUDeploymentGateError, "stale"),
        ):
            gate.verify_mineru_deployment_gate(
                parent.settings, parse_enabled=True, process_profile=parent.process_profile,
                now=stale_clock(parent),
            )

    def test_the_parent_v11_manifest_still_requires_its_explicit_capacity(self) -> None:
        case: unittest.TestCase = self  # type: ignore[assignment]
        parent = self._parent()
        case.assertEqual(parent.manifest["contract_version"], "mineru-runtime-bundle.v11")
        envelope = {"identity_sha256": parent.runtime_identity, "manifest": parent.manifest}
        common = dict(configured_identity=parent.runtime_identity, local_client_identity=parent.client,
                      local_processing_window_size=16, local_writer_code_digest=parent.historical_writer)
        capacity = decode_mineru_capacity_config(parent.capacity_path.read_bytes())
        verified = verify_runtime_manifest_payload(envelope, expected_capacity=capacity, **common)
        case.assertEqual(verified.identity_sha256, parent.runtime_identity)
        with case.assertRaisesRegex(ValueError, "explicit capacity selection disagrees with version"):
            verify_runtime_manifest_payload(envelope, **common)


class SyntheticParentBaselineTests(_ParentBaseline, unittest.TestCase):
    """Default suite: the authored v11 parent (Package A composition), never labelled actual."""

    parent_factory = staticmethod(synthetic_parent_q0)


class ActualParentBaselineTests(_ParentBaseline, unittest.TestCase):
    """Opt-in: the byte-exact production parent from ``F5_UPGRADE_ACTUAL_Q0_ROOT``."""

    parent_factory = staticmethod(actual_parent_q0)

    def test_the_actual_parent_is_the_recorded_production_qualification(self) -> None:
        parent = self._parent()
        self.assertEqual((parent.manifest["client"]["writer_code_sha256"],
                          canonical_payload_sha256(parent.manifest)), (ACTUAL_W0, ACTUAL_R0))
        self.assertEqual(parent.process_profile.sha256, "sha256:" + ACTUAL_SHA256["process-profile-P0.json"])
        self.assertEqual(parent.process_profile.runtime_bundle_identity_sha256, ACTUAL_R0)
        self.assertEqual(parent.worker_profile.sha256, "sha256:" + ACTUAL_SHA256["worker-profile-WP0.json"])
        self.assertEqual(parent.worker_profile.process_profile_sha256, parent.process_profile.sha256)
        self.assertEqual(sha256_bytes(parent.activation_exact_bytes),
                         "sha256:" + ACTUAL_SHA256["activation-A0.json"])
        owners = [receipt["receipt"]["orchestrator"][when]["capacity_observation"]["owner"]
                  for receipt in json.loads(parent.heldout_path.read_bytes())["documents"]
                  for when in ("before", "after")]
        self.assertEqual(owners, [parent.activation["owner"]] * len(owners))


class LegacyWriterContractTests(unittest.TestCase):
    def test_the_legacy_writer_membership_and_digest_algorithm_are_unchanged(self) -> None:
        self.assertEqual(mineru_identity._WRITER_CODE_RELPATHS, LEGACY_WRITER_MEMBERS)
        self.assertEqual(mineru_identity.writer_code_digest(), legacy_writer_digest())


if __name__ == "__main__":
    unittest.main()
