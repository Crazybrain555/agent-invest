from __future__ import annotations

import copy
import unittest
from datetime import timedelta

from disclosure_anchor.application.contracts.resident_session_evidence import (
    artifact_sha256, canonical_bytes, check_resident_closure, check_resident_ready,
    check_resident_observer_mapping,
)
from disclosure_anchor.application.contracts.synchronized_telemetry import (
    SynchronizedTelemetryFrameV2, SynchronizedTelemetryReceiptV3,
)
from tests.unit.test_synchronized_telemetry_contract import (
    START, HASH_C, _complete_frames, _receipt,
)


def _fixture(host: bool = False) -> dict:
    """Synthetic session protocol evidence, never an external runtime witness."""
    def h(value: str) -> str:
        return artifact_sha256(value.encode())
    names = (
        "load_mineru_resident_session.ps1", "load_mineru_telemetry_assembly.ps1",
        "mineru_resident_telemetry_exporter.ps1", "start_mineru_resident_telemetry.ps1",
        "linux_resident_host_sampler.py", "linux_resident_host_supervisor.py",
    )
    compiled = {
        "mineru_nvml_backend.cs": "nvml_source_sha256",
        "mineru_resident_wire.cs": "wire_source_sha256",
        "mineru_telemetry_job_supervisor.cs": "supervisor_source_sha256",
    }
    hashes = {name: h(name) for name in (*names, *compiled, "build_mineru_telemetry_assembly.ps1")}
    manifest = {
        "assembly_name": "mineru-telemetry.dll", "assembly_sha256": h("assembly"),
        "compiler_arguments": ["/nologo", "/noconfig", "/target:library", "/warnaserror+", r"/out:C:\fixture\prepared\mineru-telemetry.dll", r"/reference:C:\compiler\System.dll", r"/reference:C:\compiler\System.Net.Http.dll", *("C:\\fixture\\" + name for name in sorted(compiled))],
        "compiler_path": r"C:\compiler\csc.exe", "compiler_sha256": h("compiler"),
        "contract_version": "mineru.telemetry-prepared-assembly.v2",
        "http_assembly_sha256": h("http"), "powershell_version": "5.1.22621.6133",
        "preparation_recipe_sha256": hashes["build_mineru_telemetry_assembly.ps1"],
        "runtime_version": "4.0.30319.42000", "system_assembly_sha256": h("System"),
        "sources": [{"name": name, "sha256": hashes[name]} for name in sorted(compiled)],
    }
    session = "123456781234423482341234567890ab"
    owner = {name: h(name) for name in ("host_assignment_identity_sha256", "boot_identity_sha256", "runtime_bundle_identity_sha256", "process_profile_sha256")}
    config = {
        "backend": {"nvml_dll_sha256": h("nvml"), "gpu_uuid": "GPU-12345678-1234-4234-8234-1234567890ab"},
        "cadence_ms": 1000 if host else 250, "contract_version": "mineru.windows-resident-session.v1",
        "lane": "host_slow" if host else "gpu_fast", "lease_ms": 15000, "lifetime_ms": 60000,
        "owner_identity": owner, "port": 30317 if host else 30316,
        "powershell_executable_sha256": h("powershell"),
        "prepared": {"manifest_path": r"C:\fixture\prepared\manifest.json", "manifest_sha256": artifact_sha256(canonical_bytes(manifest)), **{field: hashes[name] for name, field in compiled.items()}},
        "response_timeout_ms": 500, "run_directory": "C:\\fixture\\run-" + session,
        "sampling_timeout_ms": 900 if host else 200, "session": session,
        "source_directory": r"C:\fixture", "sources": {name: hashes[name] for name in names},
    }
    backend_ready = {"nvml_dll_sha256": h("nvml"), "device_identity_sha256": h("nvml.device-uuid.v1|GPU-12345678-1234-4234-8234-1234567890ab")}
    if host:
        linux_config = {
            "boot_id": "12345678-1234-4234-8234-1234567890ab", "lease_ms": 15000, "lifetime_ms": 60000,
            "members": {name: {"cgroup": "/docker/" + str(index) * 64, "pid": index, "start_ticks": 100 + index} for index, name in enumerate(("api", "inference", "proxy"), 1)},
            "parent_device": 25, "parent_inode": 3067,
        }
        config["backend"] = {
            "api_namespace_pid": 1, "api_port": 30003, "docker_path": r"C:\docker\docker.exe",
            "docker_sha256": h("docker"), "image_id": h("image"), "linux_config": linux_config,
            "model_name": "fixture-model", "vllm_port": 30001,
        }
        namespaces = {"pid": "pid:[1234]", "cgroup": "cgroup:[5678]"}
        supervisor = {"boot_id": linux_config["boot_id"], "namespaces": namespaces, "pid": 100,
                      "start_ticks": 123, "source_sha256": hashes["linux_resident_host_supervisor.py"], "sampler_source_sha256": hashes["linux_resident_host_sampler.py"]}
        child = {key: value for key, value in linux_config.items() if key not in {"lease_ms", "lifetime_ms"}}
        child.update(namespaces=namespaces, pid=101, start_ticks=124, parent_path="/docker", source_sha256=hashes["linux_resident_host_sampler.py"])
        sampler = {"contract_version": "mineru.linux-resident-host.v1", "kind": "ready", "identity": child,
                   "epoch_sha256": artifact_sha256(canonical_bytes(child)), "cpu": {"user_ns_total": 1, "system_ns_total": 0}, "monotonic_ns": 1000}
        backend_ready = {"container_name": "m6-resident-" + session, "docker_pid": 300, "docker_creation_filetime_100ns": 134332345280408145,
                         "docker_sha256": h("docker"), "linux_ready": {"contract_version": "mineru.linux-resident-supervisor.v1", "kind": "ready", "identity": supervisor, "epoch_sha256": artifact_sha256(canonical_bytes(supervisor)), "sampler_ready": sampler}}
    config_hash = artifact_sha256(canonical_bytes(config))
    process = {"assembly_sha256": h("assembly"), "config_sha256": config_hash, "creation_filetime_100ns": 134332345277162393,
               "manifest_sha256": config["prepared"]["manifest_sha256"], "pid": 200, "session": session}
    clock = {"boot_identity_sha256": owner["boot_identity_sha256"], "clock_source": "QueryPerformanceCounter", "frequency_hz": 10000000}
    identity = {**owner, "clock_domain_identity_sha256": artifact_sha256(canonical_bytes(clock)), "exporter_process_epoch_sha256": artifact_sha256(canonical_bytes(process)), "exporter_source_sha256": hashes["mineru_resident_telemetry_exporter.ps1"]}
    ready = {"backend": backend_ready, "cadence_ms": config["cadence_ms"], "clock": clock, "config_sha256": config_hash,
             "contract_version": "mineru.windows-resident-ready.v1", "identity": identity, "lane": config["lane"], "port": config["port"], "process": process, "session": session}
    parent = {**process, "pid": 199, "creation_filetime_100ns": 134332345274349835}
    job = {
        "child_creation_filetime_100ns": process["creation_filetime_100ns"], "child_exit_code": 0, "child_pid": 200,
        "contract_version": "mineru.windows-job-accounting.v1", "forced_termination": False, "job_active_processes": 0,
        "job_instance": "23456789-1234-4234-8234-1234567890ab", "job_system_ns_total": 3, "job_total_processes": 3 if host else 1,
        "job_user_ns_total": 4, "supervisor_creation_filetime_100ns": parent["creation_filetime_100ns"], "supervisor_pid": 199,
        "supervisor_pre_attestation_system_ns_total": 5, "supervisor_pre_attestation_user_ns_total": 6,
        "supervisor_source_sha256": hashes["mineru_telemetry_job_supervisor.cs"],
    }
    linux = None
    if host:
        linux = {
            "backend_ready": backend_ready, "config_sha256": config_hash, "session": session,
            "contract_version": "mineru.windows-linux-closed-receipt.v1",
            "close": {"contract_version": "mineru.linux-resident-host.v1", "cpu": {"system_ns_total": 0, "user_ns_total": 2},
                      "epoch_sha256": sampler["epoch_sha256"], "finished_monotonic_ns": 1100, "kind": "close", "sequence": 1, "started_monotonic_ns": 1050, "values": None},
            "closed": {"contract_version": "mineru.linux-resident-supervisor.v1", "epoch_sha256": backend_ready["linux_ready"]["epoch_sha256"], "kind": "closed", "pre_attestation_monotonic_ns": 1200,
                       "sampler_epoch_sha256": sampler["epoch_sha256"], "sampler_exit_cpu": {"user_ns_total": 3, "system_ns_total": 1}, "sampler_pid": 101, "sampler_wait_status": 0, "sequence": 1, "supervisor_pre_attestation_cpu": {"user_ns_total": 5, "system_ns_total": 2}},
        }
    closed = {"config_sha256": config_hash, "contract_version": "mineru.windows-resident-closed.v2", "identity": identity,
              "linux_closed_sha256": artifact_sha256(canonical_bytes(linux)) if host else None, "session": session,
              "sampling": {"first_sampled_monotonic_ns": None, "first_observed_at_utc": None, "last_sampled_monotonic_ns": None, "last_observed_at_utc": None,
                           "last_sequence": 0, "sample_count": 0, "skipped_slots": 0, "closing_monotonic_ns": 5000, "closing_at_utc": "2026-01-01T00:00:01Z"}}
    return {"config": config, "ready": ready, "manifest": manifest, "closed": closed,
            "job": {"config_sha256": config_hash, "contract_version": "mineru.windows-resident-job-receipt.v1", "job": job, "session": session, "supervisor_process": parent},
            "linux": linux, "hashes": hashes}


def _check_ready(fixture: dict):
    return check_resident_ready(config_bytes=canonical_bytes(fixture["config"]), ready_bytes=canonical_bytes(fixture["ready"]), manifest_bytes=canonical_bytes(fixture["manifest"]), expected_source_hashes=fixture["hashes"])


def _check_closure(fixture: dict):
    return check_resident_closure(ready=_check_ready(fixture), closed_bytes=canonical_bytes(fixture["closed"]), job_bytes=canonical_bytes(fixture["job"]), linux_closed_bytes=canonical_bytes(fixture["linux"]) if fixture["linux"] is not None else None)


def _rebind_ready(fixture: dict) -> None:
    """Rehash deliberate mutations so tests reach semantics, not stale hashes."""
    ready = fixture["ready"]
    ready["config_sha256"] = artifact_sha256(canonical_bytes(fixture["config"]))
    ready["process"]["config_sha256"] = ready["config_sha256"]
    ready["identity"]["exporter_process_epoch_sha256"] = artifact_sha256(canonical_bytes(ready["process"]))
    if fixture["config"]["lane"] == "host_slow":
        linux = ready["backend"]["linux_ready"]
        for frame in (linux, linux["sampler_ready"]):
            frame["epoch_sha256"] = artifact_sha256(canonical_bytes(frame["identity"]))


def _mapping_fixture(host: bool = False):
    f = _fixture(host)
    ready = _check_ready(f)
    owner = f["config"]["owner_identity"]
    parent = f["ready"]["backend"]
    api_epoch = artifact_sha256(b"unrelated-api")
    if host:
        child = parent["linux_ready"]["sampler_ready"]["identity"]
        api_epoch = artifact_sha256(canonical_bytes({"boot_id": child["boot_id"], **child["members"]["api"]}))
    source_origin, observer_origin = 9_000_000_000_000, 123_000_000_000_000
    frames = []
    for frame in _complete_frames():
        if frame.lane != ready.lane:
            continue
        value = frame.model_dump(mode="json")
        offset = frame.clock.started_monotonic_ns
        value.update(contract_version="mineru.synchronized-telemetry-frame.v2",
                     runtime_bundle_identity_sha256=owner["runtime_bundle_identity_sha256"],
                     process_profile_sha256=owner["process_profile_sha256"],
                     resident_exporter_provenance={
                         **{name: getattr(ready.identity, name) for name in ("exporter_source_sha256", "host_assignment_identity_sha256", "boot_identity_sha256", "exporter_process_epoch_sha256")},
                         "wire_sequence": len(frames) + 1,
                         "wire_observed_at_utc": frame.clock.observed_at_utc,
                         "wire_sampled_monotonic_ns": source_origin + offset,
                     })
        for key in ("started_monotonic_ns", "finished_monotonic_ns", "scheduled_monotonic_ns"):
            value["clock"][key] += observer_origin
        if host:
            value["api_process"]["values"]["process_epoch_sha256"] = api_epoch
            value["host_cgroup"]["values"]["parent_cgroup_epoch_sha256"] = artifact_sha256(canonical_bytes({name: child[name] for name in ("boot_id", "members", "parent_path", "parent_device", "parent_inode")}))
        else:
            value["gpu"]["values"]["device_identity_sha256"] = parent["device_identity_sha256"]
        frames.append(SynchronizedTelemetryFrameV2.model_validate(value))
    legacy = _receipt()
    receipt = SynchronizedTelemetryReceiptV3(
        run_id=legacy.run_id, runtime_bundle_identity_sha256=owner["runtime_bundle_identity_sha256"],
        process_profile={"process_epoch_sha256": api_epoch, "runtime_bundle_identity_sha256": owner["runtime_bundle_identity_sha256"], "process_profile_sha256": owner["process_profile_sha256"], "parameters": legacy.process_profile.parameters},
        observer_identity={"process_epoch_sha256": artifact_sha256(b"observer"), "clock_domain_identity_sha256": HASH_C},
        observer_source_sha256=HASH_C, clock_domain_identity_sha256=HASH_C,
        started_at_utc=START, finished_at_utc=START + timedelta(seconds=2),
        started_monotonic_ns=observer_origin, finished_monotonic_ns=observer_origin + 2_000_000_000,
        status="complete", lane_quality=legacy.lane_quality, termination_reason="duration_elapsed",
        observed_clock_divergence_ns=0, epoch_changed=False, safety_drift_reasons=(), unsupported_observation_count=0,
        artifacts={"frames_jsonl_sha256": HASH_C},
    )
    last = frames[-1].resident_exporter_provenance
    f["closed"]["sampling"].update(
        first_sampled_monotonic_ns=source_origin, first_observed_at_utc=START.isoformat(),
        last_sampled_monotonic_ns=last.wire_sampled_monotonic_ns, last_observed_at_utc=last.wire_observed_at_utc.isoformat(),
        last_sequence=len(frames), sample_count=len(frames),
        closing_monotonic_ns=source_origin + 2_000_000_000, closing_at_utc=(START + timedelta(seconds=2)).isoformat(),
    )
    return f, tuple(frames), receipt


class ResidentSessionEvidenceTests(unittest.TestCase):
    def test_cross_host_mapping_keeps_distinct_clocks_and_binds_hardware(self):
        for host in (False, True):
            f, frames, receipt = _mapping_fixture(host)
            check_resident_observer_mapping(ready=_check_ready(f), closed_bytes=canonical_bytes(f["closed"]), frames=frames, receipt=receipt)
            payload = frames[0].model_dump(mode="json")
            section, field = ("api_process", "process_epoch_sha256") if host else ("gpu", "device_identity_sha256")
            payload[section]["values"][field] = HASH_C
            changed = (SynchronizedTelemetryFrameV2.model_validate(payload), *frames[1:])
            with self.assertRaisesRegex(ValueError, "binding differs"):
                check_resident_observer_mapping(ready=_check_ready(f), closed_bytes=canonical_bytes(f["closed"]), frames=changed, receipt=receipt)
            if host:
                payload = frames[0].model_dump(mode="json")
                # The sampler process epoch is valid but is not a cgroup epoch.
                payload["host_cgroup"]["values"]["parent_cgroup_epoch_sha256"] = f["ready"]["backend"]["linux_ready"]["sampler_ready"]["epoch_sha256"]
                with self.assertRaisesRegex(ValueError, "parent cgroup"):
                    check_resident_observer_mapping(ready=_check_ready(f), closed_bytes=canonical_bytes(f["closed"]), frames=(SynchronizedTelemetryFrameV2.model_validate(payload), *frames[1:]), receipt=receipt)
                changed_receipt = receipt.model_copy(update={"process_profile": receipt.process_profile.model_copy(update={"process_epoch_sha256": HASH_C})})
                with self.assertRaisesRegex(ValueError, "receipt API epoch"):
                    check_resident_observer_mapping(ready=_check_ready(f), closed_bytes=canonical_bytes(f["closed"]), frames=frames, receipt=changed_receipt)

    def test_mapping_rejects_source_gap_clock_drift_and_old_receipt(self):
        f, frames, receipt = _mapping_fixture()
        changes = (
            {"wire_sequence": 2},
            {"wire_sampled_monotonic_ns": frames[0].resident_exporter_provenance.wire_sampled_monotonic_ns + 200_000_000},
            {"wire_observed_at_utc": START + timedelta(seconds=1)},
            {"exporter_process_epoch_sha256": HASH_C},
        )
        for change in changes:
            payload = frames[0].model_dump(mode="json")
            payload["resident_exporter_provenance"].update(change)
            with self.assertRaises(ValueError):
                check_resident_observer_mapping(ready=_check_ready(f), closed_bytes=canonical_bytes(f["closed"]), frames=(SynchronizedTelemetryFrameV2.model_validate(payload), *frames[1:]), receipt=receipt)
        f["closed"]["sampling"]["closing_at_utc"] = (START + timedelta(seconds=3)).isoformat()
        with self.assertRaisesRegex(ValueError, "wall/monotonic"):
            check_resident_observer_mapping(ready=_check_ready(f), closed_bytes=canonical_bytes(f["closed"]), frames=frames, receipt=receipt)
        with self.assertRaisesRegex(ValueError, "explicit observer v3"):
            check_resident_observer_mapping(ready=_check_ready(f), closed_bytes=canonical_bytes(f["closed"]), frames=frames, receipt=_receipt())

    def test_rehashed_invalid_backend_config_is_not_runtime_evidence(self):
        cases = (
            (False, lambda f: f["config"]["backend"].update(gpu_uuid="GPU-fixture"), "GPU UUID"),
            (True, lambda f: f["config"]["backend"].update(docker_path="docker.exe"), "Docker executable"),
            (True, lambda f: f["config"]["backend"]["linux_config"]["members"]["api"].update(pid=2), "duplicate service"),
            (True, lambda f: f["config"]["backend"]["linux_config"]["members"]["api"].update(cgroup="/docker/" + "2" * 64), "duplicate service"),
            (True, lambda f: f["ready"]["backend"].update(docker_creation_filetime_100ns=134332345277162392), "bounded integer"),
            (True, lambda f: f["ready"]["backend"].update(docker_pid=200), "PID collide"),
        )
        for host, mutate, error in cases:
            fixture = _fixture(host)
            mutate(fixture)
            _rebind_ready(fixture)
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                _check_ready(fixture)

    def test_parent_creation_order_cannot_be_relabelled(self):
        f = _fixture()
        f["job"]["supervisor_process"]["creation_filetime_100ns"] = 134332345277162394
        f["job"]["job"]["supervisor_creation_filetime_100ns"] = 134332345277162394
        with self.assertRaisesRegex(ValueError, "parent created after"):
            _check_closure(f)

    def test_gpu_and_host_replay_exact_integer_identity_and_cpu_roles(self):
        for host in (False, True):
            with self.subTest(host=host):
                fixture = _fixture(host)
                ready = _check_ready(fixture)
                self.assertEqual(ready.creation_filetime_100ns, 134332345277162393)
                closure = _check_closure(fixture)
                self.assertEqual(closure.windows_job_cpu_ns, 7)
                self.assertEqual(closure.windows_supervisor_pre_attestation_cpu_ns, 11)
                self.assertEqual(closure.linux_sampler_exit_cpu_ns, 4 if host else None)
                self.assertEqual(closure.linux_supervisor_pre_attestation_cpu_ns, 7 if host else None)
                self.assertEqual(closure.source_sample_count, 0)

    def test_ready_rehashes_clock_process_manifest_and_all_sources(self):
        mutations = (
            lambda f: f["ready"]["clock"].update(frequency_hz=10000001),
            lambda f: f["ready"]["process"].update(creation_filetime_100ns=134332345277162392),
            lambda f: f["manifest"].update(assembly_sha256="sha256:" + "a" * 64),
            lambda f: f["hashes"].update({"start_mineru_resident_telemetry.ps1": "sha256:" + "b" * 64}),
            lambda f: f["ready"]["backend"].update(device_identity_sha256="sha256:" + "b" * 64),
        )
        for mutate in mutations:
            fixture = _fixture()
            mutate(fixture)
            with self.assertRaises(ValueError):
                _check_ready(fixture)

    def test_noncanonical_and_duplicate_bytes_are_not_repaired(self):
        f = _fixture()
        for invalid in (canonical_bytes(f["ready"]) + b"\n", canonical_bytes(f["ready"]).replace(b'"cadence_ms":250', b'"cadence_ms":250,"cadence_ms":250')):
            with self.assertRaises(ValueError):
                check_resident_ready(config_bytes=canonical_bytes(f["config"]), ready_bytes=invalid, manifest_bytes=canonical_bytes(f["manifest"]), expected_source_hashes=f["hashes"])

    def test_job_abnormal_noninteger_and_mismatched_child_never_qualify(self):
        for key, value in (("child_exit_code", 1), ("forced_termination", True), ("job_active_processes", 1), ("child_pid", 201), ("job_user_ns_total", True), ("job_system_ns_total", -1), ("supervisor_pid", 200)):
            f = _fixture()
            f["job"]["job"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                _check_closure(f)

    def test_old_close_version_and_invented_zero_sample_times_are_rejected(self):
        for mutate in (lambda f: f["closed"].update(contract_version="mineru.windows-resident-closed.v1"), lambda f: f["closed"]["sampling"].update(first_sampled_monotonic_ns=0), lambda f: f["closed"]["sampling"].update(last_sequence=1)):
            f = _fixture()
            mutate(f)
            with self.assertRaises(ValueError):
                _check_closure(f)

    def test_linux_exit_lower_bound_and_exact_identity_are_replayed(self):
        baseline = _fixture(True)
        for mutate in (
            lambda f: f["linux"]["closed"].update(sampler_wait_status=9),
            lambda f: f["linux"]["closed"].update(sampler_pid=102),
            lambda f: f["linux"]["closed"]["sampler_exit_cpu"].update(user_ns_total=1),
            lambda f: f["linux"]["closed"].update(sequence=2),
            lambda f: f["linux"].update(backend_ready={**f["linux"]["backend_ready"], "docker_pid": 999}),
        ):
            f = copy.deepcopy(baseline)
            mutate(f)
            f["closed"]["linux_closed_sha256"] = artifact_sha256(canonical_bytes(f["linux"]))
            with self.assertRaises(ValueError):
                _check_closure(f)
        f = _fixture(True)
        f["linux"] = None
        with self.assertRaisesRegex(ValueError, "Linux exit evidence"):
            _check_closure(f)
