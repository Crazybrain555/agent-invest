"""Independent R21 finite-lifetime vectors; no long sleeps or remote workloads."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import resident_telemetry_owner as owner
from disclosure_anchor.adapters.runtime.resident_owner_control import BoundedOwnerCommand
from tests.unit.test_dedicated_mac_observer import _request
from tests.unit.test_resident_telemetry_owner import _fixture


def duration_request(request, *, sampling, lifetime, lease=30000):
    plans = []
    for plan in (request.gpu, request.host):
        value = json.loads(plan.config_bytes)
        value.update(lifetime_ms=lifetime, lease_ms=lease)
        if value["lane"] == "host_slow":
            value["backend"]["linux_config"].update(lifetime_ms=lifetime, lease_ms=lease)
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        plans.append(replace(plan, config_bytes=raw))
    return replace(request, duration_seconds=sampling, gpu=plans[0], host=plans[1])


class FiniteLifetimeIndependentTests(unittest.TestCase):
    def test_linux_primitives_accept_8400_but_reject_overflow_before_installing_any_alarm(self):
        from scripts.windows import linux_resident_host_sampler as sampler
        from scripts.windows import linux_resident_host_supervisor as supervisor

        class ValidationFinished(Exception):
            pass

        for module in (sampler, supervisor):
            for milliseconds in (8_390_000, 8_400_000, 8_400_001, True):
                with self.subTest(module=module.__name__, milliseconds=milliseconds):
                    kwargs = {"lease_ms": 30_000, "lifetime_ms": milliseconds}
                    with patch.object(module.signal, "signal", side_effect=ValidationFinished) as alarm:
                        expected = ValidationFinished if type(milliseconds) is int and milliseconds <= 8_400_000 else ValueError
                        with self.assertRaises(expected):
                            if module is sampler:
                                module.run(kwargs, "sha256:" + "a" * 64)
                            else:
                                module.run(kwargs, sampler, "sha256:" + "a" * 64, "sha256:" + "b" * 64)
                        if expected is ValueError:
                            alarm.assert_not_called()

    def test_wire_configuration_has_its_own_8390_ceiling_not_the_8400_primitive_limit(self):
        from disclosure_anchor.application.contracts.resident_session_evidence import check_resident_configuration

        with tempfile.TemporaryDirectory() as directory:
            request, _, _ = _fixture(Path(directory).resolve())
            for lifetime in (8_380_000, 8_390_000, 8_390_001, 8_400_000):
                case = duration_request(request, sampling=8200, lifetime=lifetime)
                for plan in (case.gpu, case.host):
                    with self.subTest(lifetime=lifetime, lane=json.loads(plan.config_bytes)["lane"]):
                        if lifetime <= 8_390_000:
                            check_resident_configuration(config_bytes=plan.config_bytes, manifest_bytes=plan.manifest_bytes,
                                                         expected_source_hashes=request.source_hashes)
                        else:
                            with self.assertRaises(ValueError):
                                check_resident_configuration(config_bytes=plan.config_bytes, manifest_bytes=plan.manifest_bytes,
                                                             expected_source_hashes=request.source_hashes)

    def test_default_control_ceiling_stays_7200_and_extended_ceiling_is_explicit(self):
        argv = [sys.executable, "-c", "pass"]
        for ceiling, timeout in ((7200, 7200), (8400, 8400), (8400, 8280)):
            with self.subTest(ceiling=ceiling, timeout=timeout):
                command = BoundedOwnerCommand(argv, timeout_seconds=timeout, lifetime_ceiling_seconds=ceiling)
                try:
                    self.assertEqual(command.finish().exit_code, 0)
                finally:
                    command.abort()
        options = (
            {"timeout_seconds": 7201}, {"timeout_seconds": 8401, "lifetime_ceiling_seconds": 8400},
            {"timeout_seconds": 1, "lifetime_ceiling_seconds": 8401},
            {"timeout_seconds": 1, "lifetime_ceiling_seconds": True},
            {"timeout_seconds": float("nan"), "lifetime_ceiling_seconds": 8400},
            {"timeout_seconds": float("inf"), "lifetime_ceiling_seconds": 8400},
            {"timeout_seconds": True, "lifetime_ceiling_seconds": 8400},
        )
        for kwargs in options:
            with self.subTest(kwargs=kwargs), patch(
                "disclosure_anchor.adapters.runtime.resident_owner_control.subprocess.Popen",
                side_effect=AssertionError("invalid timeout reached process creation"),
            ) as popen:
                with self.assertRaises(ValueError):
                    BoundedOwnerCommand(argv, **kwargs)
                popen.assert_not_called()

    def test_sampling_lifetime_lease_budget_rejected_before_any_launch_or_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            request, _, _ = _fixture(Path(directory).resolve())
            for sampling, lifetime in ((8200, 8260000), (8300, 8360000), (8300, 8380000)):
                owner._validate_request(duration_request(request, sampling=sampling, lifetime=lifetime))
                self.assertFalse(request.evidence_directory.exists())
            invalid = (
                (8300.001, 8380000, 30000), (8300, 8359999, 30000), (8200, 8380001, 30000),
                (8200, 8260000, 29999), (float("nan"), 8260000, 30000),
                (float("inf"), 8260000, 30000), (True, 8260000, 30000),
            )
            for sampling, lifetime, lease in invalid:
                with self.subTest(sampling=sampling, lifetime=lifetime, lease=lease):
                    bad = duration_request(request, sampling=sampling, lifetime=lifetime, lease=lease)
                    with patch.object(owner, "_launch_command", side_effect=AssertionError("invalid request launched")) as launch:
                        with self.assertRaises(ValueError):
                            owner._validate_request(bad)
                        launch.assert_not_called()
                    self.assertFalse(request.evidence_directory.exists())

    def test_dedicated_observer_allows_full_frozen_window_but_no_infinite_or_bool_duration(self):
        for duration in (8200, 8300):
            self.assertEqual(replace(_request(), duration_seconds=duration).duration_seconds, duration)
        for duration in (8300.001, 0, float("nan"), float("inf"), True):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                replace(_request(), duration_seconds=duration)


if __name__ == "__main__":
    unittest.main()
