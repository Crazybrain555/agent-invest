#!/usr/bin/env python3
"""Pre-commit storage fault matrix for the MinerU task registry (M6 P1).

Every public mutator is driven to its durable precondition inside a disposable
temporary root, one storage phase that happens before the registry pathname is
exchanged is made to fail, and the harness checks the contract outcome:

* the call raises ``TaskRegistryPersistenceError`` with ``outcome="not_committed"``
  and ``committed=False`` (the whole-``_persist`` override keeps the injected
  exception itself);
* the in-memory state and the registry bytes are exactly what they were before;
* no temporary file survives in the private registry directory;
* the same idempotent call then succeeds, health is clean again, and a cold
  reload of the registry file agrees with the process state.

The harness prints one JSON report and exits non-zero when any scenario fails.
It is a synthetic regression matrix: it proves the software commit boundary,
not a physical power-loss experiment, and it qualifies nothing about M6.

Usage::

    PYTHONPATH=src python tests/regressions/m6_p1_registry_persistence/run_registry_fault_regression.py [--report PATH]
    PYTHONPATH=src python -m unittest tests.regressions.m6_p1_registry_persistence.run_registry_fault_regression
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.windows.mineru_heap_trim_compat.agent_task_protocol_v2 import (  # noqa: E402
    TaskRegistryPersistenceError,
)
from tests._m6_registry_lab import (  # noqa: E402
    MUTATOR_PRECONDITIONS,
    PRE_COMMIT_PHASES,
    RegistryLab,
    SyntheticStorageFault,
    persist_override,
    pre_commit_fault,
    prepare_mutation,
    snapshot,
    without_live_readers,
)

SCHEMA = "m6-p1-registry-precommit-fault-matrix.v1"
PHASES = PRE_COMMIT_PHASES + ("persist_override",)


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_scenario(phase: str, mutator: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temporary:
        lab = RegistryLab(Path(temporary))
        registry = lab.open()
        invoke = prepare_mutation(lab, registry, mutator)
        before = snapshot(registry)
        disk = lab.disk_bytes()
        fault = persist_override(registry) if phase == "persist_override" else pre_commit_fault(registry, phase)
        raised: BaseException | None = None
        with fault:
            try:
                invoke()
            except (SyntheticStorageFault, TaskRegistryPersistenceError) as exc:
                raised = exc
        expect(raised is not None, "mutation reported success under an injected pre-commit fault")
        assert raised is not None
        if phase == "persist_override":
            expect(isinstance(raised, SyntheticStorageFault), f"unexpected exception {type(raised).__name__}")
        else:
            expect(isinstance(raised, TaskRegistryPersistenceError), f"unexpected exception {type(raised).__name__}")
            assert isinstance(raised, TaskRegistryPersistenceError)
            expect(raised.outcome == "not_committed", f"outcome {raised.outcome!r} is not not_committed")
            expect(raised.committed is False, "not_committed outcome reported committed=True")
            expect(raised.operation == mutator, f"operation {raised.operation!r} != {mutator!r}")
            expect(isinstance(raised.__cause__, OSError), "storage cause was not chained")
            status = registry.persistence_status()
            expect(status["state"] == "degraded", f"status after fault is {status['state']!r}")
            expect(
                status["recovery_action"] == "retry_idempotent_operation",
                f"recovery action {status['recovery_action']!r}",
            )
            expect(status["last_event"]["operation"] == mutator, "event operation mismatch")
            expect(status["last_event"]["committed"] is False, "event marked committed")
        expect(snapshot(registry) == before, "in-memory state was not rolled back")
        expect(lab.disk_bytes() == disk, "registry bytes changed without a durable commit")
        expect(lab.stray_names() == [], f"temporary registry entries survived: {lab.stray_names()}")

        invoke()
        expect(registry.persistence_status()["state"] == "healthy", "retry did not clear health")
        after = snapshot(registry)
        expect(after != before, "retry did not change registry state")
        expect(lab.disk_bytes() != disk, "retry did not change registry bytes")
        expect(snapshot(lab.open()) == without_live_readers(after), "cold reload disagrees with process state")
        return {
            "phase": phase,
            "mutator": mutator,
            "raised": type(raised).__name__,
            "outcome": getattr(raised, "outcome", None),
            "registry_phase": getattr(raised, "phase", None),
        }


def run_matrix() -> dict[str, Any]:
    scenarios: list[dict[str, Any]] = []
    for phase in PHASES:
        for mutator in MUTATOR_PRECONDITIONS:
            entry: dict[str, Any] = {"phase": phase, "mutator": mutator}
            try:
                entry.update(run_scenario(phase, mutator))
                entry["passed"] = True
            except BaseException as exc:  # report every failure family, never hide one
                entry["passed"] = False
                entry["error"] = f"{type(exc).__name__}: {exc}"
                entry["traceback"] = traceback.format_exc()
            scenarios.append(entry)
    failures = [f"{item['phase']}/{item['mutator']}" for item in scenarios if not item["passed"]]
    return {
        "schema": SCHEMA,
        "phases": list(PHASES),
        "mutators": list(MUTATOR_PRECONDITIONS),
        "scenario_count": len(scenarios),
        "failures": failures,
        "passed": not failures,
        "scenarios": scenarios,
    }


class RegistryPreCommitFaultMatrix(unittest.TestCase):
    def test_every_pre_commit_fault_is_not_committed_and_retryable(self) -> None:
        report = run_matrix()
        self.assertEqual(
            report["failures"],
            [],
            json.dumps([item for item in report["scenarios"] if not item["passed"]], indent=2),
        )
        self.assertEqual(report["scenario_count"], len(PHASES) * len(MUTATOR_PRECONDITIONS))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, default=None, help="write the JSON report to this path")
    arguments = parser.parse_args(argv)
    report = run_matrix()
    text = json.dumps(report, indent=2, sort_keys=True)
    if arguments.report is not None:
        arguments.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
