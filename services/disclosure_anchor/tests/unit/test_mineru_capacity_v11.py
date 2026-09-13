"""Independent v11 qualification projections, not runtime/PDF acceptance."""

from contextlib import redirect_stderr
from copy import deepcopy
import io
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.runtime.mineru_identity import (
    verified_cpu_thread_policy,
    verify_runtime_manifest_payload,
)
from disclosure_anchor.application.contracts.mineru_capacity_config import decode_mineru_capacity_config
from disclosure_anchor.application.contracts.mineru_capacity_health import validate_mineru_capacity_wire_health
from scripts import attest_mineru_remote_runtime as attester
from tests._mineru_capacity_config_fixture import canonical_payload, capacity_payload
from tests._mineru_capacity_v11_fixture import (
    LABEL, VERSION, build, digest, envelope, expected_orchestrator,
    independent_manifest, legacy_identity, observation, source_pins,
)


def verify(manifest, config_payload, **changes):
    kwargs = dict(
        configured_identity=digest(manifest), local_client_identity=legacy_identity._client_identity(),
        local_writer_code_digest=legacy_identity.CODE_DIGEST,
        local_processing_window_size=16 if config_payload is None else config_payload["processing_window_size"],
        expected_capacity=None if config_payload is None else decode_mineru_capacity_config(canonical_payload(config_payload)),
    )
    kwargs.update(changes)
    return verify_runtime_manifest_payload(envelope(manifest), **kwargs)


class CapacityV11Tests(unittest.TestCase):
    def test_profiles_share_one_version_and_exact_independent_static_projection(self):
        profiles = [capacity_payload(), capacity_payload(
            parse_active_limit=1, total_nonterminal_limit=1, finalizer_active_limit=1,
            final_http_limit_per_loop=2, result_reservation_bytes=1, max_unacked_result_bytes=1,
            processing_window_size=3, hybrid_batch_ratio_requested=1, omp_num_threads=1),
            capacity_payload(parse_active_limit=3, total_nonterminal_limit=7, finalizer_active_limit=2,
                             final_http_limit_per_loop=11, result_reservation_bytes=101,
                             max_unacked_result_bytes=401, processing_window_size=32,
                             hybrid_batch_ratio_requested=4, mkl_num_threads=3)]
        identities = set()
        for config in profiles:
            with self.subTest(config=config):
                raw = observation(config)
                self.assertEqual(len(raw["api_health"]), 17)
                expected = expected_orchestrator(raw, config)
                self.assertEqual(len(expected), 28)
                result = build(raw, config)
                self.assertEqual(result["manifest"]["contract_version"], VERSION)
                self.assertEqual(result["manifest"]["orchestrator"], expected)
                self.assertEqual(result["identity_sha256"], digest(result["manifest"]))
                # Separate identity input is literal legacy envelope + independent 28-field oracle.
                closed = verify(independent_manifest(config), config)
                self.assertEqual(closed.max_concurrent_requests, config["parse_active_limit"])
                self.assertEqual(closed.max_pending_tasks, config["total_nonterminal_limit"])
                self.assertEqual(closed.orchestrator_identity_sha256, digest(expected))
                identities.add(result["identity_sha256"])
        self.assertEqual(len(identities), 3)

    def test_volatile_observations_are_validated_but_excluded_from_stable_identity(self):
        config = capacity_payload()
        raw = observation(config)
        first = build(raw, config)
        changed = deepcopy(raw)
        health = changed["api_health"]
        health.update(completed_tasks=71, failed_tasks=29)
        live = health["capacity_observation"]
        live["owner"].update(process_id=987, process_start_ticks=654,
                             boot_id="99999999-8888-4777-8666-555555555555",
                             loop_epoch="dddddddd-cccc-4bbb-8aaa-999999999999")
        live["observed_at"].update(implementation="monotonic alternative", started_ns=700, completed_ns=901)
        live["framework_limits"]["torch_intraop_threads"] = {
            "state": "unavailable", "value": None, "reason": "serving_getter_not_loaded"}
        live["framework_limits"]["pdf_render_pool_max_workers"]["value"] = 29
        live["http_limiter_state"] = "not_initialized"
        live["resolved_limits"]["final_http_limit_per_loop"] = None
        self.assertNotEqual(raw, changed)
        self.assertEqual(build(changed, config), first)
        live["owner"]["process_start_ticks"] = 0
        with self.assertRaises(ValueError):
            build(changed, config)

    def test_missing_busy_and_requested_or_applied_drain_never_qualify(self):
        config = capacity_payload()
        expected = decode_mineru_capacity_config(canonical_payload(config))
        for kind in ("missing", "ingress", "http", "requested", "applied"):
            with self.subTest(kind=kind):
                raw = observation(config)
                health = raw["api_health"]
                live = health["capacity_observation"]
                if kind == "missing":
                    del health["capacity_observation"]
                elif kind == "ingress":
                    health["queued_tasks"] = 1
                    health["task_admission"].update(ingress_tasks=1, durable_nonterminal_tasks=1)
                elif kind == "http":
                    live["http_counters"]["active_requests"] = 1
                else:
                    live["owner_control"].update(
                        foreign_loop_observed=True, soft_drain_requested=True,
                        soft_drain_applied=kind == "applied", trigger="foreign_event_loop")
                    if kind == "applied":
                        health["task_admission"].update(admission_open=False, blocked_reason="shutting_down")
                if kind != "missing":
                    validate_mineru_capacity_wire_health(health, expected_capacity=expected)
                with self.assertRaises(ValueError):
                    build(raw, config)

    def test_source_maps_labels_and_actual_file_evidence_are_closed_and_bound(self):
        config = capacity_payload()
        self.assertEqual(len(source_pins()), 4)
        for kind in ("missing_source", "extra_source", "different_actual", "wrong_label", "old_marker",
                     "file_extra", "file_path", "file_sha", "file_size"):
            with self.subTest(kind=kind):
                raw = observation(config)
                compat = raw["api_compatibility"]
                sources = compat["capacity_sources_actual_sha256"]
                if kind == "missing_source":
                    del sources[next(iter(sources))]
                elif kind == "extra_source":
                    sources["mineru/cli/unexpected.py"] = "sha256:" + "0" * 64
                elif kind == "different_actual":
                    sources[next(iter(sources))] = "sha256:" + "0" * 64
                elif kind == "wrong_label":
                    compat["image_labels"][LABEL + "capacity-sources-sha256"] = "sha256:" + "0" * 64
                elif kind == "old_marker":
                    compat["marker"]["capacity_policy"] = "single-owner-serial-mineru.v1"
                else:
                    field, value = {
                        "file_extra": ("mode", 292), "file_path": ("path", "/other/capacity.json"),
                        "file_sha": ("sha256", "sha256:" + "0" * 64), "file_size": ("byte_count", 1),
                    }[kind]
                    compat["capacity_config_file"][field] = value
                with self.assertRaises(ValueError):
                    build(raw, config)

    def test_actual_config_file_byte_count_rejects_equal_float_and_other_wrong_types(self):
        config = capacity_payload()
        raw = observation(config)
        actual = raw["api_compatibility"]["capacity_config_file"]["byte_count"]
        build(raw, config)  # The same integer and all other fields are valid.
        for value in (float(actual), str(actual), True, None):
            with self.subTest(value=value, type=type(value).__name__):
                changed = deepcopy(raw)
                changed["api_compatibility"]["capacity_config_file"]["byte_count"] = value
                with self.assertRaises(ValueError):
                    build(changed, config)

    def test_identity_rejects_resigned_alias_drift_unknown_fields_and_wrong_external_config(self):
        config = capacity_payload()
        manifest = independent_manifest(config)
        verify(manifest, config)
        for field, value in (
            ("max_concurrent_requests", 1), ("max_pending_tasks_effective", 2),
            ("task_result_reservation_bytes", 30), ("max_unacked_result_bytes", 74),
            ("inference_max_concurrency", 8), ("processing_window_size", 17),
            ("capacity_config_sha256", "sha256:" + "0" * 64),
            ("cpu_thread_policy", {}), ("capacity_source_sha256", {}),
            ("task_registry_max_records", 128.0),
        ):
            with self.subTest(field=field):
                changed = deepcopy(manifest)
                changed["orchestrator"][field] = value
                with self.assertRaises(ValueError):
                    verify(changed, config)
        changed = deepcopy(manifest)
        changed["orchestrator"]["capacity_config"]["finalizer_active_limit"] = 2
        changed["orchestrator"]["capacity_config_sha256"] = digest(changed["orchestrator"]["capacity_config"])
        with self.assertRaises(ValueError):
            verify(changed, config)
        # A correctly re-signed source change still cannot match the original external identity.
        changed = deepcopy(manifest)
        sources = changed["orchestrator"]["capacity_source_sha256"]
        sources[next(iter(sources))] = "sha256:" + "0" * 64
        with self.assertRaises(ValueError):
            verify(changed, config, configured_identity=digest(manifest))
        bad_envelope = envelope(manifest)
        bad_envelope["identity_sha256"] = "sha256:" + "0" * 64
        with self.assertRaises(ValueError):
            verify_runtime_manifest_payload(
                bad_envelope, configured_identity=digest(manifest),
                local_client_identity=legacy_identity._client_identity(),
                local_writer_code_digest=legacy_identity.CODE_DIGEST,
                local_processing_window_size=16,
                expected_capacity=decode_mineru_capacity_config(canonical_payload(config)))

    def test_legacy_versions_keep_exact_shapes_and_never_infer_explicit_capacity(self):
        legacy = [legacy_identity._manifest(), legacy_identity._staged_manifest()]
        cpu2 = deepcopy(legacy[1])
        cpu2["contract_version"] = "mineru-runtime-bundle.v10"
        cpu2["orchestrator"]["cpu_thread_policy"] = {
            "contract_version": "mineru.cpu-thread-policy.v1", "omp_num_threads": 2,
            "mkl_num_threads": 2, "openblas_num_threads": 1, "pdf_render_threads": 3,
        }
        for manifest in [*legacy, cpu2]:
            with self.subTest(version=manifest["contract_version"]):
                result = verify(manifest, None)
                self.assertEqual(result.manifest, manifest)
                self.assertEqual(result.identity_sha256, digest(manifest))
                self.assertEqual(verified_cpu_thread_policy(manifest), 2 if manifest is cpu2 else 1)
                with self.assertRaises(ValueError):
                    verify(manifest, capacity_payload())
                changed = deepcopy(manifest)
                changed["orchestrator"]["max_concurrent_requests"] = 2
                with self.assertRaises(ValueError):
                    verify(changed, None)
        explicit = independent_manifest(capacity_payload())
        with self.assertRaises(ValueError):
            verify(explicit, None)
        with self.assertRaises(ValueError):
            verified_cpu_thread_policy(explicit)

    def test_observation_version_and_external_selection_do_not_fallback(self):
        config = capacity_payload()
        raw = observation(config)
        for kwargs in (
            {"expected_capacity": None}, {"expected_capacity_source_sha256": None},
            {"expected_capacity_source_sha256": {}}, {"expected_api_cpu_threads": 1},
            {"expected_api_cpu_threads": 2},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                build(raw, config, **kwargs)
        for version in ("mineru-windows-runtime-observation.v5", "mineru-windows-runtime-observation.v7"):
            changed = deepcopy(raw)
            changed["schema"] = version
            with self.subTest(version=version), self.assertRaises(ValueError):
                build(changed, config)

    def test_cli_rejects_incomplete_or_mixed_selection_before_credentials_or_remote_io(self):
        argv = ["--mineru-bin", "/unused/bin", "--ssh-host", "host.invalid", "--ssh-user", "unused",
                "--identity-file", "/unused/key", "--known-hosts-file", "/unused/known-hosts",
                "--manifest-out", "/unused/out", "--observation-out", "/unused/raw"]
        choices = [
            ["--capacity-config", "/unused/config"],
            ["--expected-capacity-config-sha256", "sha256:" + "0" * 64],
            ["--capacity-config", "/unused/config", "--expected-capacity-config-sha256", "sha256:" + "0" * 64,
             "--expected-api-cpu-threads", "2"],
            *[["--capacity-" + name + "-source", "/unused/source"]
              for name in ("config", "file", "bootstrap", "observation")],
        ]
        for extra in choices:
            with (self.subTest(extra=extra), redirect_stderr(io.StringIO()),
                  patch.object(attester, "_private_regular_file", side_effect=AssertionError("credential IO")) as private,
                  patch.object(attester, "load_mineru_capacity_config", side_effect=AssertionError("config IO")) as load,
                  patch.object(attester, "_read_remote_file", side_effect=AssertionError("remote IO")) as remote,
                  patch.object(attester.subprocess, "run", side_effect=AssertionError("process")) as process):
                with self.assertRaises(SystemExit) as caught:
                    attester.main(argv + extra)
                self.assertEqual(caught.exception.code, 2)
                for seam in (private, load, remote, process):
                    seam.assert_not_called()


if __name__ == "__main__":
    unittest.main()
