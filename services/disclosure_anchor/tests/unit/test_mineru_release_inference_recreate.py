"""Closed operation selection before any owner or remote action."""

from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.mineru_release_install import (
    API_COMPATIBILITY,
    INFERENCE_RECREATE,
    FULL_INSTALL,
    installation_binding_document,
    install_release,
)
from disclosure_anchor.adapters.runtime.mineru_release_package import ReleaseIdentityError, ReleaseInputError
from disclosure_anchor.cli.mineru_release import build_parser


_HASH = "sha256:" + "a" * 64
_OTHER = "sha256:" + "b" * 64


def _binding(*, compose: str = _HASH, capacity: str = _HASH) -> SimpleNamespace:
    return SimpleNamespace(windows=SimpleNamespace(
        hostname="test-host", expected_active_compose_sha256=compose,
        expected_previous_capacity_sha256=capacity,
        job_lifetime_milliseconds=1_200_000, job_cleanup_milliseconds=5_000,
    ))


class InferenceRecreateSelectionTests(unittest.TestCase):
    def test_binding_keeps_v1_default_and_closes_v2_operation(self):
        legacy = installation_binding_document(_binding(), api_device_profile="cuda0", exclusivity_sha256=_HASH)
        self.assertEqual(legacy["contract_version"], "m6.installation-binding.v1")
        self.assertNotIn("operation_kind", legacy)
        inference = installation_binding_document(
            _binding(), api_device_profile="cuda0", exclusivity_sha256=_HASH,
            operation_kind=INFERENCE_RECREATE,
        )
        self.assertEqual(inference["contract_version"], "m6.installation-binding.v2")
        self.assertEqual(inference["operation_kind"], INFERENCE_RECREATE)
        self.assertEqual(set(inference), set(legacy) | {"operation_kind"})
        with self.assertRaises(ReleaseInputError):
            installation_binding_document(
                _binding(), api_device_profile="cuda0", exclusivity_sha256=_HASH,
                operation_kind="full-stack-recreate",
            )

    def test_inference_preflight_refuses_compose_or_capacity_drift_before_output(self):
        report = SimpleNamespace(
            passed=True, manifest=SimpleNamespace(projection={"compose_sha256": _HASH}),
            inputs=SimpleNamespace(capacity=SimpleNamespace(sha256=_HASH)),
        )
        with TemporaryDirectory() as temp:
            for binding in (_binding(compose=_OTHER), _binding(capacity=_OTHER)):
                output = Path(temp) / ("compose" if binding.windows.expected_active_compose_sha256 == _OTHER else "capacity")
                with self.assertRaises(ReleaseIdentityError):
                    install_release(
                        report=report, package=Path(temp), binding=binding,
                        output=output, operation_kind=INFERENCE_RECREATE,
                    )
                self.assertFalse(output.exists())
            unknown = Path(temp) / "unknown"
            with self.assertRaises(ReleaseInputError):
                install_release(
                    report=report, package=Path(temp), binding=_binding(),
                    output=unknown, operation_kind="unknown",
                )
            self.assertFalse(unknown.exists())

    def test_full_install_requires_exact_zero_duty_binding_without_weakening_old_modes(self):
        full = installation_binding_document(_binding(), api_device_profile="cuda0", exclusivity_sha256=_HASH,
                                             operation_kind=FULL_INSTALL, legacy_scope_inventory_sha256=_OTHER)
        self.assertEqual(full["contract_version"], "m6.installation-binding.v4")
        self.assertEqual(full["operation_kind"], "full-install")
        self.assertEqual(full["legacy_scope_inventory_sha256"], _OTHER)
        for operation, inventory in ((FULL_INSTALL, None), (API_COMPATIBILITY, _OTHER), (INFERENCE_RECREATE, _OTHER)):
            with self.subTest(operation=operation), self.assertRaises(ReleaseInputError):
                installation_binding_document(_binding(), api_device_profile="cuda0", exclusivity_sha256=_HASH,
                                             operation_kind=operation, legacy_scope_inventory_sha256=inventory)
        with TemporaryDirectory() as temp:
            out = Path(temp) / "out"
            with self.assertRaises(ReleaseInputError):
                install_release(report=SimpleNamespace(passed=True), package=Path(temp), binding=_binding(),
                                output=out, operation_kind=FULL_INSTALL)
            self.assertFalse(out.exists())

    def test_same_config_diagnostic_recreation_reaches_existing_owner_but_source_change_is_refused(self):
        report = SimpleNamespace(
            passed=True, manifest=SimpleNamespace(sha256=_HASH, projection={"compose_sha256": _HASH}),
            inputs=SimpleNamespace(capacity=SimpleNamespace(sha256=_HASH),
                                   deployment_profile=SimpleNamespace(inference_diagnostic=object())),
        )
        binding = _binding()
        binding.mac_exclusive_lock_path = Path("/unused-lock")
        with TemporaryDirectory() as temp:
            with patch("disclosure_anchor.adapters.runtime.mineru_release_install.acquire_exclusivity",
                       side_effect=RuntimeError("owner reached")) as owner:
                with self.assertRaisesRegex(RuntimeError, "owner reached"):
                    install_release(report=report, package=Path(temp), binding=binding,
                                    output=Path(temp) / "same", operation_kind=INFERENCE_RECREATE)
                owner.assert_called_once()
            for changed in (_binding(compose=_OTHER), _binding(capacity=_OTHER)):
                output = Path(temp) / ("compose" if changed.windows.expected_active_compose_sha256 == _OTHER else "capacity")
                with self.assertRaises(ReleaseIdentityError):
                    install_release(report=report, package=Path(temp), binding=changed,
                                    output=output, operation_kind=INFERENCE_RECREATE)
                self.assertFalse(output.exists())

    def test_cli_requires_explicit_operation_selection(self):
        common = ["install", "--package", "/tmp/package", "--private-binding", "/tmp/binding", "--output", "/tmp/out"]
        parser = build_parser()
        self.assertEqual(parser.parse_args(common).operation_kind, API_COMPATIBILITY)
        self.assertEqual(parser.parse_args(common + ["--operation-kind", INFERENCE_RECREATE]).operation_kind, INFERENCE_RECREATE)
        with self.assertRaises(SystemExit):
            parser.parse_args(common + ["--operation-kind", "unknown"])


if __name__ == "__main__":
    unittest.main()
