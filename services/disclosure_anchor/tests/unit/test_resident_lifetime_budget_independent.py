"""Independent finite-lifetime vectors; no long sleeps or remote workloads.

The ceilings are the R22 vector and are read from the product's own policy rather than
restated here, so a term the composition rule moves moves these cases with it. The default
command ceiling stays 7200 s and the business window is untouched; only the finite
measurement path may use the extended ceiling.
"""

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime import resident_telemetry_owner as owner
from disclosure_anchor.adapters.runtime.resident_owner_control import BoundedOwnerCommand
from disclosure_anchor.application.services.resident_measurement_policy import (
    FINITE_COMMAND_MAX_SECONDS, LANE_MAX_SECONDS, POST_SAMPLE_MAX_SECONDS, PRE_GO_MAX_SECONDS,
    SAMPLE_MAX_SECONDS, WIRE_MAX_SECONDS,
)
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
    def test_linux_primitives_accept_the_finite_ceiling_but_reject_overflow_before_any_alarm(self):
        from scripts.windows import linux_resident_host_sampler as sampler
        from scripts.windows import linux_resident_host_supervisor as supervisor

        class ValidationFinished(Exception):
            pass

        for module in (sampler, supervisor):
            ceiling_ms = FINITE_COMMAND_MAX_SECONDS * 1000
            for milliseconds in (WIRE_MAX_SECONDS * 1000, ceiling_ms, ceiling_ms + 1, True):
                with self.subTest(module=module.__name__, milliseconds=milliseconds):
                    kwargs = {"lease_ms": 30_000, "lifetime_ms": milliseconds}
                    with patch.object(module.signal, "signal", side_effect=ValidationFinished) as alarm:
                        expected = ValidationFinished if type(milliseconds) is int and milliseconds <= ceiling_ms else ValueError
                        with self.assertRaises(expected):
                            if module is sampler:
                                module.run(kwargs, "sha256:" + "a" * 64)
                            else:
                                module.run(kwargs, sampler, "sha256:" + "a" * 64, "sha256:" + "b" * 64)
                        if expected is ValueError:
                            alarm.assert_not_called()

    def test_wire_configuration_has_its_own_ceiling_below_the_primitive_limit(self):
        from disclosure_anchor.application.contracts.resident_session_evidence import check_resident_configuration

        with tempfile.TemporaryDirectory() as directory:
            request, _, _ = _fixture(Path(directory).resolve())
            wire_ms = WIRE_MAX_SECONDS * 1000
            self.assertLess(wire_ms, FINITE_COMMAND_MAX_SECONDS * 1000)
            for lifetime in (LANE_MAX_SECONDS * 1000, wire_ms, wire_ms + 1,
                             FINITE_COMMAND_MAX_SECONDS * 1000):
                case = duration_request(request, sampling=8200, lifetime=lifetime)
                for plan in (case.gpu, case.host):
                    with self.subTest(lifetime=lifetime, lane=json.loads(plan.config_bytes)["lane"]):
                        if lifetime <= wire_ms:
                            check_resident_configuration(config_bytes=plan.config_bytes, manifest_bytes=plan.manifest_bytes,
                                                         expected_source_hashes=request.source_hashes)
                        else:
                            with self.assertRaises(ValueError):
                                check_resident_configuration(config_bytes=plan.config_bytes, manifest_bytes=plan.manifest_bytes,
                                                             expected_source_hashes=request.source_hashes)

    def test_default_control_ceiling_stays_7200_and_extended_ceiling_is_explicit(self):
        argv = [sys.executable, "-c", "pass"]
        extended = FINITE_COMMAND_MAX_SECONDS
        self.assertEqual(extended, 8600)
        for ceiling, timeout in ((7200, 7200), (extended, extended), (extended, extended - 120)):
            with self.subTest(ceiling=ceiling, timeout=timeout):
                command = BoundedOwnerCommand(argv, timeout_seconds=timeout, lifetime_ceiling_seconds=ceiling)
                try:
                    self.assertEqual(command.finish().exit_code, 0)
                finally:
                    command.abort()
        options = (
            # The default ceiling is still 7200 s: an ordinary command may not reach past it.
            {"timeout_seconds": 7201},
            {"timeout_seconds": extended + 1, "lifetime_ceiling_seconds": extended},
            {"timeout_seconds": 1, "lifetime_ceiling_seconds": extended + 1},
            {"timeout_seconds": 1, "lifetime_ceiling_seconds": True},
            {"timeout_seconds": float("nan"), "lifetime_ceiling_seconds": extended},
            {"timeout_seconds": float("inf"), "lifetime_ceiling_seconds": extended},
            {"timeout_seconds": True, "lifetime_ceiling_seconds": extended},
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
            # A lane must outlive its window by the pre-GO reserve plus the post-sampling tail,
            # and may not outlive the lane ceiling.
            tail_ms = (PRE_GO_MAX_SECONDS + POST_SAMPLE_MAX_SECONDS) * 1000
            lane_ms = LANE_MAX_SECONDS * 1000
            for sampling in (8200, SAMPLE_MAX_SECONDS):
                lifetime = sampling * 1000 + tail_ms
                owner._validate_request(duration_request(request, sampling=sampling, lifetime=lifetime))
                self.assertFalse(request.evidence_directory.exists())
            self.assertEqual(SAMPLE_MAX_SECONDS * 1000 + tail_ms, lane_ms)
            invalid = (
                (SAMPLE_MAX_SECONDS + 0.001, lane_ms, 30000),
                (8200, 8200 * 1000 + tail_ms - 1, 30000),
                (8200, lane_ms + 1, 30000),
                (8200, 8200 * 1000 + tail_ms, 29999),
                (float("nan"), lane_ms, 30000),
                (float("inf"), lane_ms, 30000), (True, lane_ms, 30000),
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
        for duration in (8200, SAMPLE_MAX_SECONDS):
            self.assertEqual(replace(_request(), duration_seconds=duration).duration_seconds, duration)
        for duration in (SAMPLE_MAX_SECONDS + 0.001, 0, float("nan"), float("inf"), True):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                replace(_request(), duration_seconds=duration)


if __name__ == "__main__":
    unittest.main()
