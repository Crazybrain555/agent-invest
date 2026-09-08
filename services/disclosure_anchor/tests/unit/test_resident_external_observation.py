"""Exact read-only observation shape and actual epoch/absence binding."""

from __future__ import annotations

import copy
import json
import unittest

from disclosure_anchor.application.contracts.resident_session_evidence import (
    artifact_sha256, canonical_bytes, check_external_windows_observation,
)
from tests.unit.test_resident_session_evidence import _fixture, _check_ready, _rebind_ready


NODE = "sha256:" + "0" * 64
BOOT = "2026-01-01T00:00:00.0000000Z"


def _external_fixture(host=False, fixture=None):
    f = _fixture(host) if fixture is None else fixture
    boot_hash = artifact_sha256(canonical_bytes({"windows_node_identity_sha256": NODE, "boot_utc": BOOT}))
    f["config"]["owner_identity"]["boot_identity_sha256"] = boot_hash
    f["ready"]["identity"]["boot_identity_sha256"] = boot_hash
    f["ready"]["clock"]["boot_identity_sha256"] = boot_hash
    f["ready"]["identity"]["clock_domain_identity_sha256"] = artifact_sha256(canonical_bytes(f["ready"]["clock"]))
    _rebind_ready(f)
    f["job"]["supervisor_process"]["config_sha256"] = f["ready"]["config_sha256"]
    f["job"]["config_sha256"] = f["ready"]["config_sha256"]
    f["closed"].update(config_sha256=f["ready"]["config_sha256"], identity=f["ready"]["identity"])
    if host:
        f["linux"]["config_sha256"] = f["ready"]["config_sha256"]
        f["closed"]["linux_closed_sha256"] = artifact_sha256(canonical_bytes(f["linux"]))
    started = {name: value for name, value in f["job"].items() if name != "job"}
    started["contract_version"] = "mineru.windows-resident-supervisor-started.v1"
    processes = []
    for role, process in (("supervisor", f["job"]["supervisor_process"]), ("exporter", f["ready"]["process"])):
        processes.append(dict(role=role, pid=process["pid"], expected_creation_filetime_100ns=process["creation_filetime_100ns"], actual_creation_filetime_100ns=process["creation_filetime_100ns"], state="same-process"))
    container = None
    if host:
        backend = f["ready"]["backend"]
        processes.append(dict(role="docker", pid=backend["docker_pid"], expected_creation_filetime_100ns=backend["docker_creation_filetime_100ns"], actual_creation_filetime_100ns=backend["docker_creation_filetime_100ns"], state="same-process"))
        container = dict(id="a" * 64, name="/m6-resident-" + f["config"]["session"], image=f["config"]["backend"]["image_id"], running=True, pid=backend["linux_ready"]["identity"]["pid"], started_at="2026-01-01T00:00:01Z", pid_mode="host", cgroup_mode="host", network="none", read_only=True, auto_remove=True, privileged=False, cap_add=None, cap_drop=["ALL"], security_opt=["no-new-privileges", "label=disable"], mount_count=0, entrypoint=["/usr/bin/python3.12"])
    observation = dict(contract_version="mineru.windows-resident-external-observation.v1", phase="ready", observed_at_utc="2026-01-01T00:00:02Z", windows_boot_utc=BOOT, config_sha256=f["ready"]["config_sha256"], ready_raw=canonical_bytes(f["ready"]).decode(), started_raw=canonical_bytes(started).decode(), processes=processes, container=container, container_absence=None, closed_raw=None, job_raw=None, linux_closed_raw=None)
    closed = copy.deepcopy(observation)
    closed.update(phase="closed", observed_at_utc="2026-01-01T00:00:03Z", container=None, closed_raw=canonical_bytes(f["closed"]).decode(), job_raw=canonical_bytes(f["job"]).decode())
    for process in closed["processes"]:
        process.update(state="absent", actual_creation_filetime_100ns=None)
    if host:
        closed.update(linux_closed_raw=canonical_bytes(f["linux"]).decode(), container_absence=dict(id=container["id"], exit_code=1, stdout="[]\n", stderr="error: no such object: " + container["id"] + "\n"))
    return _check_ready(f), observation, closed


class ResidentExternalObservationTests(unittest.TestCase):
    def test_cross_phase_parent_substitution_and_malformed_closure_are_rejected(self):
        for host in (False, True):
            ready, first, last = _external_fixture(host)
            checked = check_external_windows_observation(payload=canonical_bytes(first), ready=ready, windows_node_identity_sha256=NODE)
            changed = copy.deepcopy(last)
            started = json.loads(changed["started_raw"])
            started["supervisor_process"]["pid"] -= 1
            changed["started_raw"] = canonical_bytes(started).decode()
            changed["processes"][0]["pid"] -= 1
            with self.assertRaisesRegex(ValueError, "previous observation differs"):
                check_external_windows_observation(payload=canonical_bytes(changed), ready=ready, windows_node_identity_sha256=NODE, previous_ready=checked)
            changed = copy.deepcopy(last)
            job = json.loads(changed["job_raw"])
            job["supervisor_process"]["pid"] -= 1
            job["job"]["supervisor_pid"] -= 1
            changed["job_raw"] = canonical_bytes(job).decode()
            with self.assertRaises(ValueError):
                check_external_windows_observation(payload=canonical_bytes(changed), ready=ready, windows_node_identity_sha256=NODE, previous_ready=checked)
            for name in ("closed_raw", "job_raw", *(("linux_closed_raw",) if host else ())):
                changed = copy.deepcopy(last)
                changed[name] = "notJSON"
                with self.subTest(host=host, name=name), self.assertRaises(ValueError):
                    check_external_windows_observation(payload=canonical_bytes(changed), ready=ready, windows_node_identity_sha256=NODE, previous_ready=checked)

    def test_actual_role_births_then_exact_original_epoch_absence(self):
        for host in (False, True):
            ready, first, last = _external_fixture(host)
            checked = check_external_windows_observation(payload=canonical_bytes(first), ready=ready, windows_node_identity_sha256=NODE)
            # A reused numeric PID is not the original owned process; neither
            # this checker nor the observation script kills the replacement.
            last["processes"][0].update(state="different-birth", actual_creation_filetime_100ns=last["processes"][0]["expected_creation_filetime_100ns"] + 10)
            closed = check_external_windows_observation(payload=canonical_bytes(last), ready=ready, windows_node_identity_sha256=NODE, previous_ready=checked)
            self.assertEqual(closed.container_id, "a" * 64 if host else None)

    def test_relabelled_process_boot_and_helper_configuration_do_not_pass(self):
        ready, first, _ = _external_fixture(True)
        bad = []
        for field, value in (("read_only", False), ("network", "bridge"), ("cap_add", ["SYS_ADMIN"]), ("pid", 777), ("security_opt", ["no-new-privileges", "arbitrary"])):
            changed = copy.deepcopy(first)
            changed["container"][field] = value
            bad.append(changed)
        changed = copy.deepcopy(first)
        changed["processes"][0]["actual_creation_filetime_100ns"] += 1
        bad.append(changed)
        changed = copy.deepcopy(first)
        changed["windows_boot_utc"] = "2026-01-02T00:00:00Z"
        bad.append(changed)
        for payload in bad:
            with self.assertRaises(ValueError):
                check_external_windows_observation(payload=canonical_bytes(payload), ready=ready, windows_node_identity_sha256=NODE)

    def test_closed_observation_needs_same_ready_and_exact_absence_not_generic_error(self):
        ready, first, last = _external_fixture(True)
        checked = check_external_windows_observation(payload=canonical_bytes(first), ready=ready, windows_node_identity_sha256=NODE)
        for field, value in (("exit_code", 0), ("stderr", "connection refused"), ("id", "b" * 64)):
            changed = copy.deepcopy(last)
            changed["container_absence"][field] = value
            with self.assertRaises(ValueError):
                check_external_windows_observation(payload=canonical_bytes(changed), ready=ready, windows_node_identity_sha256=NODE, previous_ready=checked)
        changed = copy.deepcopy(last)
        changed["processes"][0].update(state="same-process", actual_creation_filetime_100ns=changed["processes"][0]["expected_creation_filetime_100ns"])
        with self.assertRaisesRegex(ValueError, "remains alive"):
            check_external_windows_observation(payload=canonical_bytes(changed), ready=ready, windows_node_identity_sha256=NODE, previous_ready=checked)
