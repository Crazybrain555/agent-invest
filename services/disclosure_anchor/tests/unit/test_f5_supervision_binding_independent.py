"""Independent acceptance for root finding R5: one configured supervision binding.

Root's native runs showed ``XPC_SERVICE_NAME="0"`` in every worker child the
wrapper spawned under launchd (a job process launched directly sees its label),
so launchd identity cannot select the native channel. The r3 authority
is configuration only: the supervised runtime root is bound to its label (the
production root to the production label, any other supervised root to its own
disposable label). The same binding drives the start gate and the public-stop
disable; ``XPC_SERVICE_NAME`` is never consulted; a temporary, synthetic or
mismatched binding can never read or disable the production label.

Every supervisor constructed here is replaced by a scripted runner that
refuses the production label, so no test can reach the real ``launchctl``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr
import hashlib
import io
import os
from pathlib import Path
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime import worker_stop_control
from disclosure_anchor.adapters.runtime.worker_stop_control import (
    DEFAULT_WORKER_LAUNCHD_LABEL,
    LaunchdSupervisor,
    RuntimeWorkerStopControl,
    StopRecord,
    WorkerControlBusy,
    observe_worker_control,
    plan_release,
    release_worker_circuit,
    worker_supervision,
)
from disclosure_anchor.application.ports.worker_stop_control import WorkerOperationalStopError
from disclosure_anchor.cli import worker as worker_cli
from disclosure_anchor.settings import PRODUCTION_WORKER_RUNTIME_ROOT, Settings, load_settings
from tests.unit._f5_stop_fixture import (
    FAKE_LAUNCHCTL,
    ScriptedLaunchctl,
    cause,
    control_dir,
    disposable_label,
    settings_for,
    trusted_root,
)
from tests.unit.test_settings import _env


@contextmanager
def _scripted_supervisors(scripts: dict[str, ScriptedLaunchctl]) -> Iterator[list[str]]:
    """Every ``LaunchdSupervisor(label)`` built by product code gets a scripted runner."""

    built: list[str] = []

    def factory(label: str, **kwargs: object) -> LaunchdSupervisor:
        built.append(label)
        if label not in scripts:
            raise AssertionError(f"product code built a supervisor for an unexpected label {label!r}")
        return LaunchdSupervisor(label, launchctl=FAKE_LAUNCHCTL, runner=scripts[label])

    with mock.patch.object(worker_stop_control, "LaunchdSupervisor", side_effect=factory):
        yield built


def _production_settings(**extra: str) -> Settings:
    """Settings naming the production runtime root; nothing here touches it."""

    with mock.patch.dict(os.environ, {**_env(Path("/nonexistent/f5")),
                                      "DISCLOSURE_RUNTIME_ROOT": str(PRODUCTION_WORKER_RUNTIME_ROOT),
                                      **extra}, clear=True):
        return load_settings()


class SupervisionBindingTests(unittest.TestCase):
    def test_the_binding_is_configuration_and_the_production_label_serves_only_its_root(self) -> None:
        label = disposable_label()
        root = trusted_root(self)
        cases = (
            ("production root, default label", _production_settings(), "supervised", DEFAULT_WORKER_LAUNCHD_LABEL),
            ("production root, other label",
             _production_settings(DISCLOSURE_WORKER_LAUNCHD_LABEL=label), "label_root_mismatch", None),
            ("temp root declared supervised with its own label",
             settings_for(root, supervised=True, label=label), "supervised", label),
            ("temp root declared supervised without a label", settings_for(root, supervised=True),
             "label_root_mismatch", None),
            ("temp root declared supervised with the production label",
             settings_for(root, supervised=True, label=DEFAULT_WORKER_LAUNCHD_LABEL), "label_root_mismatch", None),
            ("temp root not declared supervised, production label exported",
             settings_for(root, label=DEFAULT_WORKER_LAUNCHD_LABEL), "unsupervised_runtime_root", None),
        )
        for name, settings, scope, bound in cases:
            with self.subTest(case=name):
                supervision = worker_supervision(settings)
                self.assertEqual(supervision.scope, scope)
                if bound is None:
                    self.assertIsNone(supervision.supervisor)
                else:
                    assert supervision.supervisor is not None
                    self.assertEqual(supervision.supervisor.service_target, f"gui/{os.getuid()}/{bound}")
        with mock.patch("sys.platform", "linux"):
            self.assertEqual(worker_supervision(_production_settings()).scope, "not_macos")

    def test_launchd_identity_is_never_consulted_by_the_stop(self) -> None:
        label = disposable_label()
        outcomes = []
        for xpc in (None, "0", label, DEFAULT_WORKER_LAUNCHD_LABEL):
            with self.subTest(XPC_SERVICE_NAME=xpc):
                root = trusted_root(self)
                settings = settings_for(root, supervised=True, label=label)
                script = ScriptedLaunchctl(label)
                environment = {} if xpc is None else {"XPC_SERVICE_NAME": xpc}
                with (
                    mock.patch.dict(os.environ, environment),
                    _scripted_supervisors({label: script}) as built,
                ):
                    if xpc is None:
                        os.environ.pop("XPC_SERVICE_NAME", None)
                    control = RuntimeWorkerStopControl.for_settings(settings)
                    self.assertTrue(control.trip(cause()))
                    outcome = control.persistence_outcome(timeout=5)
                assert outcome is not None
                self.assertEqual(built, [label])
                self.assertEqual((outcome.native.status, outcome.native.detail), ("verified_disabled", "disable_ok"))
                self.assertEqual(outcome.native.service_target, f"gui/{os.getuid()}/{label}")
                self.assertEqual(outcome.marker.status, "written")
                self.assertEqual(script.verbs(), ["disable", "print-disabled"])
                record = StopRecord.decode((control_dir(root) / "worker-circuit-stop.json").read_bytes())
                self.assertEqual(record.native_disable, outcome.native)
                outcomes.append((outcome.native.status, outcome.native.detail))
        self.assertEqual(len(set(outcomes)), 1, "the result never depends on XPC_SERVICE_NAME")

    def test_mismatched_or_unsupervised_scopes_never_run_launchctl_and_stay_truthful(self) -> None:
        for name, supervised, label, detail in (
            ("mismatch", True, None, "label_root_mismatch"),
            ("mismatch with the production label", True, DEFAULT_WORKER_LAUNCHD_LABEL, "label_root_mismatch"),
            ("unsupervised temp root", False, DEFAULT_WORKER_LAUNCHD_LABEL, "unsupervised_runtime_root"),
        ):
            with self.subTest(case=name):
                root = trusted_root(self)
                settings = settings_for(root, supervised=supervised, label=label)
                with _scripted_supervisors({}) as built, mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": "0"}):
                    control = RuntimeWorkerStopControl.for_settings(settings)
                    control.trip(cause())
                    outcome = control.persistence_outcome(timeout=5)
                    snapshot = observe_worker_control(settings)
                assert outcome is not None
                self.assertEqual(built, [], "no supervisor, no launchctl")
                self.assertEqual((outcome.native.status, outcome.native.detail, outcome.native.service_target),
                                 ("failed", detail, None))
                self.assertEqual(outcome.marker.status, "written")
                self.assertEqual(snapshot.state, "PUBLIC_STOP")

    def test_a_mismatched_root_refuses_starts_releases_and_reconstruction(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root, supervised=True)
        with _scripted_supervisors({}) as built:
            snapshot = observe_worker_control(settings)
            self.assertEqual((snapshot.state, snapshot.supervision), ("CONTROL_UNAVAILABLE", "label_root_mismatch"))
            self.assertEqual(snapshot.native.disabled_detail, "label_root_mismatch")  # type: ignore[union-attr]
            with self.assertRaises(WorkerOperationalStopError) as refused:
                worker_stop_control.require_worker_start_permitted(settings)
            self.assertEqual(refused.exception.state, "CONTROL_UNAVAILABLE")
            control = RuntimeWorkerStopControl.for_settings(settings)
            control.trip(cause())
            record = control.persistence_outcome(timeout=5)
            assert record is not None and record.marker.sha256 is not None
            plan = plan_release(settings, expect_sha256=record.marker.sha256, process_lister=lambda: ())
            self.assertEqual((plan["native_closure"], plan["would_release"]), ("unknown", False))
            with self.assertRaises(WorkerControlBusy):
                release_worker_circuit(settings, expect_sha256=record.marker.sha256, decided_by="root",
                                       reason="r", fixed_by="f", process_lister=lambda: ())
            evidence = root / "evidence.txt"
            evidence.write_bytes(b"note\n")
            stderr = io.StringIO()
            with (
                mock.patch.object(worker_cli, "load_settings", return_value=settings),
                mock.patch.object(worker_cli, "_held_worker_singleton", side_effect=AssertionError("singleton")),
                redirect_stderr(stderr),
            ):
                code = worker_cli.main(["record-circuit-stop", "--from-disabled", "--evidence", str(evidence),
                                        "--evidence-sha256", "sha256:" + hashlib.sha256(b"note\n").hexdigest(),
                                        "--decided-by", "root", "--reason", "r"])
            self.assertEqual(code, 3)
            self.assertIn("label_root_mismatch", stderr.getvalue())
        self.assertEqual(built, [])


if __name__ == "__main__":
    unittest.main()
