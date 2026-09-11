#!/usr/bin/env python3
"""Post-replace and post-commit fault families for the MinerU task registry.

Each family injects one failure at or after the registry pathname exchange and
checks the contract outcome class: committed after recovery, not committed with
the previous bytes durable, durability uncertain with explicit recovery to the
candidate or the previous state, committed with a cleanup error, or a reader
mutation that can only be resolved by a cold restart.  A cold reload of the
registry file is compared with the process view at the end of every family.

Synthetic temporary roots only.  This proves the software commit boundary and
its reconciliation logic; it is not a physical power-loss experiment and it
qualifies nothing about M6.

Usage::

    PYTHONPATH=src python tests/regressions/m6_p1_registry_persistence/run_postreplace_fault_regression.py [--report PATH]
    PYTHONPATH=src python -m unittest tests.regressions.m6_p1_registry_persistence.run_postreplace_fault_regression
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.windows.mineru_heap_trim_compat.agent_task_protocol_v2 import (  # noqa: E402
    TaskProtocolConflict,
    TaskRegistryPersistenceError,
)
from tests._m6_registry_lab import (  # noqa: E402
    FINALIZER_BUDGET,
    KEY,
    PERMANENT,
    TASK,
    CountedFault,
    RegistryLab,
    close_descriptor_then_fail,
    drive,
    fail_with,
    instance_hook,
    replace_then_fail,
    snapshot,
    without_live_readers,
    write_foreign_then_fail,
)

SCHEMA = "m6-p1-registry-postreplace-fault-families.v1"


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def persistence_error(call: Callable[[], object]) -> TaskRegistryPersistenceError:
    try:
        call()
    except TaskRegistryPersistenceError as exc:
        return exc
    raise AssertionError("call succeeded although a persistence outcome was expected")


def expect_cold_agreement(lab: RegistryLab, registry: Any) -> None:
    expect(snapshot(lab.open()) == without_live_readers(snapshot(registry)), "cold reload disagrees")


def family_replace_raises_after_rename(lab: RegistryLab) -> dict[str, Any]:
    registry = lab.open()
    drive(lab, registry, "bound")
    with instance_hook(registry, "_replace_registry_file", replace_then_fail):
        registry.transition(KEY, "processing")
    status = registry.persistence_status()
    expect(registry.get(KEY).state == "processing", "committed mutation was rolled back")
    expect(lab.disk_records()[KEY]["state"] == "processing", "candidate bytes are not durable")
    expect(status["state"] == "degraded", "health is not degraded after recovery")
    expect(status["last_event"]["outcome"] == "committed_after_recovery", status["last_event"])
    expect(status["last_event"]["committed"] is True, "recovery event not marked committed")
    expect(status["recovery_action"] == "do_not_retry_committed_operation", status["recovery_action"])
    expect(lab.stray_names() == [], "temp entries survived")
    error = persistence_error(registry.assert_persistence_healthy)
    expect(error.committed is True and error.outcome == "committed_after_recovery", "health error shape")
    expect_cold_agreement(lab, registry)
    registry.transition(KEY, "finalizing")
    expect(registry.persistence_status()["state"] == "healthy", "clean mutation did not clear health")
    return {"outcome": status["last_event"]["outcome"], "phase": status["last_event"]["phase"]}


def family_replace_raises_without_rename(lab: RegistryLab) -> dict[str, Any]:
    registry = lab.open()
    drive(lab, registry, "bound")
    disk = lab.disk_bytes()
    with instance_hook(registry, "_replace_registry_file", fail_with("replace failed before rename")):
        error = persistence_error(lambda: registry.transition(KEY, "processing"))
    expect(error.outcome == "not_committed" and error.committed is False, "outcome class")
    expect(error.phase == "replace_previous_durable", error.phase)
    expect(registry.get(KEY).state == "pending", "rollback failed")
    expect(lab.disk_bytes() == disk, "previous bytes changed")
    expect(lab.stray_names() == [], "temp entries survived")
    registry.transition(KEY, "processing")
    expect_cold_agreement(lab, registry)
    return {"outcome": error.outcome, "phase": error.phase}


def family_parent_fsync_transient(lab: RegistryLab) -> dict[str, Any]:
    registry = lab.open()
    drive(lab, registry, "bound")
    fault = CountedFault(1, os.fsync)
    with instance_hook(registry, "_fsync_parent_descriptor", fault):
        registry.transition(KEY, "processing")
    status = registry.persistence_status()
    expect(fault.calls == 2, f"parent fsync attempts {fault.calls}")
    expect(status["last_event"]["outcome"] == "committed_after_recovery", status["last_event"])
    expect(status["last_event"]["phase"] == "parent_fsync_retry", status["last_event"])
    expect(registry.get(KEY).state == "processing", "committed mutation was rolled back")
    expect_cold_agreement(lab, registry)
    return {"outcome": status["last_event"]["outcome"], "attempts": fault.calls}


def family_parent_fsync_permanent_then_candidate(lab: RegistryLab) -> dict[str, Any]:
    registry = lab.open()
    drive(lab, registry, "bound")
    with instance_hook(registry, "_fsync_parent_descriptor", CountedFault(PERMANENT)):
        error = persistence_error(lambda: registry.transition(KEY, "processing"))
    expect(error.outcome == "durability_uncertain" and error.committed is False, "outcome class")
    status = registry.persistence_status()
    expect(status["state"] == "durability_uncertain", status["state"])
    expect(status["recovery_action"] == "call recover_persistence_uncertainty", status["recovery_action"])
    expect(status["candidate_idempotency_keys"] == [KEY], status["candidate_idempotency_keys"])
    read_error = persistence_error(lambda: registry.get(KEY))
    expect(read_error.outcome == "durability_uncertain", "reads stayed open while uncertain")
    mutation_error = persistence_error(lambda: registry.fail(KEY, error="x"))
    expect(mutation_error.outcome == "durability_uncertain", "mutations stayed open while uncertain")
    recovered = registry.recover_persistence_uncertainty()
    expect(recovered["last_event"]["outcome"] == "committed_after_explicit_recovery", recovered)
    expect(recovered["last_event"]["committed"] is True, recovered)
    expect(registry.get(KEY).state == "processing", "candidate was not selected")
    expect_cold_agreement(lab, registry)
    registry.transition(KEY, "finalizing")
    expect(registry.persistence_status()["state"] == "healthy", "health not cleared")
    return {"uncertain_phase": error.phase, "recovery": recovered["last_event"]["outcome"]}


def family_replace_without_rename_then_previous(lab: RegistryLab) -> dict[str, Any]:
    registry = lab.open()
    drive(lab, registry, "bound")
    disk = lab.disk_bytes()
    with instance_hook(
        registry, "_replace_registry_file", fail_with("replace failed before rename")
    ), instance_hook(registry, "_fsync_parent_descriptor", CountedFault(PERMANENT)):
        error = persistence_error(lambda: registry.transition(KEY, "processing"))
    expect(error.outcome == "durability_uncertain", error.outcome)
    expect(error.phase == "replace_parent_fsync_retry", error.phase)
    expect(lab.disk_bytes() == disk, "previous bytes changed")
    recovered = registry.recover_persistence_uncertainty()
    expect(recovered["last_event"]["outcome"] == "not_committed_after_explicit_recovery", recovered)
    expect(recovered["recovery_action"] == "retry_idempotent_operation", recovered)
    expect(registry.get(KEY).state == "pending", "previous state was not restored")
    registry.transition(KEY, "processing")
    expect_cold_agreement(lab, registry)
    return {"uncertain_phase": error.phase, "recovery": recovered["last_event"]["outcome"]}


def family_foreign_bytes_stay_closed(lab: RegistryLab) -> dict[str, Any]:
    registry = lab.open()
    drive(lab, registry, "bound")
    with instance_hook(registry, "_replace_registry_file", write_foreign_then_fail):
        error = persistence_error(lambda: registry.transition(KEY, "processing"))
    expect(error.outcome == "durability_uncertain", error.outcome)
    expect(error.phase == "replace_ambiguous_bytes", error.phase)
    recovery = persistence_error(registry.recover_persistence_uncertainty)
    expect(recovery.outcome == "durability_uncertain", "recovery blessed foreign bytes")
    expect(isinstance(recovery.__cause__, TaskProtocolConflict), "conflict not chained")
    expect(registry.persistence_status()["state"] == "durability_uncertain", "state reopened")
    try:
        lab.open()
    except TaskProtocolConflict:
        cold = "rejected"
    else:
        raise AssertionError("cold load accepted foreign registry bytes")
    return {"uncertain_phase": error.phase, "cold_load": cold}


def family_bytes_changed_during_reconciliation(lab: RegistryLab) -> dict[str, Any]:
    registry = lab.open()
    drive(lab, registry, "bound")
    previous = lab.disk_bytes()
    assert previous is not None
    fired: list[bool] = []

    def restore_previous_then_fsync(descriptor: int) -> None:
        if not fired:
            fired.append(True)
            lab.registry_path.write_bytes(previous)
        os.fsync(descriptor)

    with instance_hook(registry, "_replace_registry_file", replace_then_fail), instance_hook(
        registry, "_fsync_parent_descriptor", restore_previous_then_fsync
    ):
        error = persistence_error(lambda: registry.transition(KEY, "processing"))
    expect(error.outcome == "durability_uncertain", error.outcome)
    expect(error.phase == "replace_changed_during_reconciliation", error.phase)
    recovered = registry.recover_persistence_uncertainty()
    expect(recovered["last_event"]["outcome"] == "not_committed_after_explicit_recovery", recovered)
    expect(registry.get(KEY).state == "pending", "previous state was not restored")
    registry.transition(KEY, "processing")
    expect_cold_agreement(lab, registry)
    return {"uncertain_phase": error.phase, "recovery": recovered["last_event"]["outcome"]}


def family_post_commit_temp_cleanup(lab: RegistryLab) -> dict[str, Any]:
    registry = lab.open()
    drive(lab, registry, "bound")
    with instance_hook(registry, "_cleanup_temp_path", fail_with("temp cleanup failed")):
        registry.transition(KEY, "processing")
    status = registry.persistence_status()
    expect(status["last_event"]["outcome"] == "committed_cleanup_failed", status)
    expect(status["last_event"]["phase"] == "post_commit_cleanup", status)
    expect(status["recovery_action"] == "do_not_retry_committed_operation", status)
    expect(registry.get(KEY).state == "processing", "committed mutation was rolled back")
    error = persistence_error(registry.assert_persistence_healthy)
    expect(error.committed is True, "health error not marked committed")
    expect_cold_agreement(lab, registry)
    return {"outcome": status["last_event"]["outcome"]}


def family_post_commit_parent_close(lab: RegistryLab) -> dict[str, Any]:
    registry = lab.open()
    drive(lab, registry, "bound")
    with instance_hook(registry, "_close_parent_descriptor", close_descriptor_then_fail):
        registry.transition(KEY, "processing")
    status = registry.persistence_status()
    expect(status["last_event"]["outcome"] == "committed_cleanup_failed", status)
    expect(registry.get(KEY).state == "processing", "committed mutation was rolled back")
    expect(lab.stray_names() == [], "temp entries survived")
    expect_cold_agreement(lab, registry)
    return {"outcome": status["last_event"]["outcome"]}


def _reader_family(lab: RegistryLab, operation: str) -> dict[str, Any]:
    registry = lab.open()
    drive(lab, registry, "leased" if operation == "acquire_result" else "acquired")
    mutate = getattr(registry, operation)
    with instance_hook(registry, "_fsync_parent_descriptor", CountedFault(PERMANENT)):
        error = persistence_error(lambda: mutate(KEY))
    expect(error.outcome == "durability_uncertain", error.outcome)
    status = registry.persistence_status()
    expect(status["recovery_action"] == "restart_registry_process", status["recovery_action"])
    refused = persistence_error(registry.recover_persistence_uncertainty)
    expect(refused.phase == "reader_mutation_requires_cold_restart", refused.phase)
    expect(registry.persistence_status()["state"] == "durability_uncertain", "state reopened")
    cold = lab.open()
    expect(cold.get(KEY).active_readers == 0, "cold start did not reset reader count")
    expect(cold.get(KEY).state == "completed", "cold start lost the completed result")
    return {"operation": operation, "recovery_action": status["recovery_action"], "refused_phase": refused.phase}


def family_reader_acquire_uncertainty(lab: RegistryLab) -> dict[str, Any]:
    return _reader_family(lab, "acquire_result")


def family_reader_release_uncertainty(lab: RegistryLab) -> dict[str, Any]:
    return _reader_family(lab, "release_result")


def family_uncertain_capacity_is_conservative(lab: RegistryLab) -> dict[str, Any]:
    registry = lab.open()
    drive(lab, registry, "reserved")
    path, digest, size, owner = lab.make_result(TASK)
    with instance_hook(registry, "_fsync_parent_descriptor", CountedFault(PERMANENT)):
        error = persistence_error(
            lambda: registry.complete(
                KEY, result_path=path, result_sha256=digest, result_bytes=size, result_owner=owner
            )
        )
    expect(error.outcome == "durability_uncertain", error.outcome)
    expect(registry.reserved_result_bytes == FINALIZER_BUDGET, "reservation released early")
    expect(registry.unacked_result_bytes == size, "candidate result charge not held")
    registry.recover_persistence_uncertainty()
    expect((registry.reserved_result_bytes, registry.unacked_result_bytes) == (0, size), "post-recovery charge")
    expect_cold_agreement(lab, registry)
    return {"reserved_while_uncertain": FINALIZER_BUDGET, "unacked_while_uncertain": size}


def family_recovery_when_healthy_is_a_no_op(lab: RegistryLab) -> dict[str, Any]:
    registry = lab.open()
    drive(lab, registry, "bound")
    status = registry.recover_persistence_uncertainty()
    expect(status["state"] == "healthy", status)
    expect(registry.get(KEY).state == "pending", "state changed")
    return {"state": status["state"]}


FAMILIES: tuple[tuple[str, Callable[[RegistryLab], dict[str, Any]]], ...] = (
    ("replace_raises_after_rename_is_committed", family_replace_raises_after_rename),
    ("replace_raises_without_rename_is_not_committed", family_replace_raises_without_rename),
    ("parent_fsync_transient_commits_after_retry", family_parent_fsync_transient),
    ("parent_fsync_permanent_then_explicit_candidate", family_parent_fsync_permanent_then_candidate),
    ("replace_without_rename_then_explicit_previous", family_replace_without_rename_then_previous),
    ("foreign_bytes_stay_closed", family_foreign_bytes_stay_closed),
    ("bytes_changed_during_reconciliation", family_bytes_changed_during_reconciliation),
    ("post_commit_temp_cleanup_failure", family_post_commit_temp_cleanup),
    ("post_commit_parent_close_failure", family_post_commit_parent_close),
    ("reader_acquire_uncertainty_requires_cold_restart", family_reader_acquire_uncertainty),
    ("reader_release_uncertainty_requires_cold_restart", family_reader_release_uncertainty),
    ("uncertain_capacity_is_conservative", family_uncertain_capacity_is_conservative),
    ("recovery_when_healthy_is_a_no_op", family_recovery_when_healthy_is_a_no_op),
)


def run_families() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for name, family in FAMILIES:
        entry: dict[str, Any] = {"family": name}
        try:
            with tempfile.TemporaryDirectory() as temporary:
                entry["facts"] = family(RegistryLab(Path(temporary)))
            entry["passed"] = True
        except BaseException as exc:  # report every failure family, never hide one
            entry["passed"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["traceback"] = traceback.format_exc()
        results.append(entry)
    failures = [item["family"] for item in results if not item["passed"]]
    return {
        "schema": SCHEMA,
        "family_count": len(results),
        "failures": failures,
        "passed": not failures,
        "families": results,
    }


class RegistryPostReplaceFaultFamilies(unittest.TestCase):
    def test_every_post_replace_family_matches_its_contract_outcome(self) -> None:
        report = run_families()
        self.assertEqual(
            report["failures"],
            [],
            json.dumps([item for item in report["families"] if not item["passed"]], indent=2),
        )
        self.assertEqual(report["family_count"], len(FAMILIES))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, default=None, help="write the JSON report to this path")
    arguments = parser.parse_args(argv)
    report = run_families()
    text = json.dumps(report, indent=2, sort_keys=True)
    if arguments.report is not None:
        arguments.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
