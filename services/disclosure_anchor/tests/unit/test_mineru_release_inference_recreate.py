"""Closed operation selection before any owner or remote action."""

from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest

from disclosure_anchor.adapters.runtime.mineru_release_install import (
    API_COMPATIBILITY,
    INFERENCE_RECREATE,
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

    def test_cli_requires_explicit_operation_selection(self):
        common = ["install", "--package", "/tmp/package", "--private-binding", "/tmp/binding", "--output", "/tmp/out"]
        parser = build_parser()
        self.assertEqual(parser.parse_args(common).operation_kind, API_COMPATIBILITY)
        self.assertEqual(parser.parse_args(common + ["--operation-kind", INFERENCE_RECREATE]).operation_kind, INFERENCE_RECREATE)
        with self.assertRaises(SystemExit):
            parser.parse_args(common + ["--operation-kind", "unknown"])


if __name__ == "__main__":
    unittest.main()
