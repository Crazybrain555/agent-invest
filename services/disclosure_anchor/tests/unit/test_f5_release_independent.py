"""Independent F5 acceptance: SHA-bound release and native-only reconstruction.

Pro §6.3 and root review R3: a release names the exact active hash; it
refuses stale or new stops, live or unknown old-owner closure and an
unreadable process table, and then leaves the stop untouched; it writes an
immutable archive and a release decision before removing the active record,
so every crash point keeps the stop and a rerun is idempotent only for the
exact same decision; a release record alone never permits a start; a dry run
writes nothing; nothing here enables or starts a job. A native-only stop is
reconstructed as ``operator_reconstructed`` from hash-bound evidence, never as
an automatic record and never over an existing one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import threading
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime.worker_stop_control import (
    StopRecord,
    WorkerControlBusy,
    WorkerControlRefused,
    WorkerSupervision,
    plan_release,
    read_evidence,
    reconstruct_worker_circuit_stop,
    release_worker_circuit,
    require_worker_start_permitted,
)
from disclosure_anchor.application.ports.worker_stop_control import WorkerOperationalStopError
from disclosure_anchor.cli import worker as worker_cli
from disclosure_anchor.settings import Settings
from tests.unit._f5_stop_fixture import (
    ScriptedLaunchctl,
    cause,
    control_dir,
    disposable_label,
    durable_control,
    listing,
    settings_for,
    trusted_root,
    write_stop,
)


_UNSUPERVISED = WorkerSupervision("unsupervised_runtime_root", None)
_DECISION = {"decided_by": "root-f5-acceptance", "reason": "cause fixed and verified", "fixed_by": "fix-r3"}


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _no_owners() -> tuple[tuple[int, str], ...]:
    return ()


def _release(settings: Settings, sha: str, *, supervision: WorkerSupervision = _UNSUPERVISED,
             lister=_no_owners, **decision: str) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return release_worker_circuit(
        settings, expect_sha256=sha, supervision=supervision, process_lister=lister,
        **{**_DECISION, **decision},
    )


def _active(root: Path) -> Path:
    return control_dir(root) / "worker-circuit-stop.json"


@contextmanager
def _failing_once(operation: str, name_suffix: str) -> Iterator[list[str]]:
    """Fail the first ``os.link``/``os.unlink`` whose target name ends with the suffix."""

    real = getattr(os, operation)
    failed: list[str] = []

    def wrapper(*args: object, **kwargs: object) -> None:
        target = Path(os.fsdecode(args[-1 if operation == "link" else 0]))  # type: ignore[arg-type]
        if not failed and target.name.endswith(name_suffix):
            failed.append(target.name)
            raise OSError(errno.EIO, "injected crash point")
        real(*args, **kwargs)

    with mock.patch(f"os.{operation}", wrapper):
        yield failed


class ReleaseRefusalTests(unittest.TestCase):
    def test_only_the_exact_active_hash_releases(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root)
        sha = write_stop(settings)
        original = _active(root).read_bytes()
        before = listing(control_dir(root))
        for wrong in ("sha256:" + "0" * 64, sha.upper(), sha.removeprefix("sha256:"), "sha256:abc"):
            with self.subTest(expect=wrong), self.assertRaises(WorkerControlRefused):
                _release(settings, wrong)
        plan = plan_release(settings, expect_sha256="sha256:" + "0" * 64, supervision=_UNSUPERVISED,
                            process_lister=_no_owners)
        self.assertFalse(plan["would_release"])
        self.assertTrue(any("not the expected hash" in problem for problem in plan["problems"]))  # type: ignore[attr-defined]
        self.assertEqual(_active(root).read_bytes(), original)
        # An actual release takes the short control lock before it rereads the
        # hash; the (empty) lock file is protocol, not a stop mutation.
        after = {name: value for name, value in listing(control_dir(root)).items()
                 if name != "worker-circuit.lock"}
        self.assertEqual(after, before)

    def test_live_or_unknown_old_owner_closure_refuses_and_writes_nothing(self) -> None:
        owners = {
            "worker": "/usr/bin/python3 -m disclosure_anchor.cli.worker loop --progress off",
            "wrapper": "/bin/zsh /repo/scripts/run_worker_once.sh loop",
            "mineru": "/opt/mineru/bin/mineru -p /tmp/in.pdf",
            "semantic_claude": "claude -p --json-schema {} --no-session-persistence --safe-mode",
        }
        pid = 4242 if os.getpid() != 4242 else 4243
        cases: list[tuple[str, WorkerSupervision, object, str]] = []
        for kind, command in owners.items():
            cases.append((f"{kind} still alive", _UNSUPERVISED,
                          lambda command=command: ((pid, command),), f"{kind} process {pid}"))

        def unavailable() -> tuple[tuple[int, str], ...]:
            raise OSError(errno.EPERM, "ps denied")

        cases.append(("process table unavailable", _UNSUPERVISED, unavailable, "process table unavailable"))
        running = ScriptedLaunchctl(disposable_label(), loaded="running", pid=5151)
        cases.append(("supervised job still running", running.supervision(), _no_owners, "still running"))
        unknown = ScriptedLaunchctl(disposable_label(), failures={"print": 5})
        cases.append(("supervised job closure unknown", unknown.supervision(), _no_owners,
                      "closure cannot be proven"))
        for name, supervision, lister, blocker in cases:
            with self.subTest(case=name):
                root = trusted_root(self)
                settings = settings_for(root)
                sha = write_stop(settings)
                before = listing(control_dir(root))
                plan = plan_release(settings, expect_sha256=sha, supervision=supervision,
                                    process_lister=lister)  # type: ignore[arg-type]
                self.assertFalse(plan["would_release"])
                self.assertTrue(any(blocker in item for item in plan["closure_blockers"]))  # type: ignore[attr-defined]
                with self.assertRaises(WorkerControlBusy) as refused:
                    _release(settings, sha, supervision=supervision, lister=lister)
                self.assertIn(blocker, str(refused.exception))
                self.assertEqual(listing(control_dir(root)), before, "no lock, archive or record")
                self.assertEqual(_refused_state(settings), "PUBLIC_STOP")
        self.assertEqual({"print-disabled", "print"} | set(running.verbs()) | set(unknown.verbs()),
                         {"print-disabled", "print"}, "release never mutates launchd")

    def test_a_new_stop_is_never_released_by_an_old_hash(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root)
        old = write_stop(settings)
        _release(settings, old)
        new = write_stop(settings, "retry_stage_stuck")
        self.assertNotEqual(old, new)
        with self.assertRaises(WorkerControlRefused):
            _release(settings, old)
        self.assertEqual(_sha(_active(root).read_bytes()), new)


def _refused_state(settings: Settings) -> str | None:
    try:
        require_worker_start_permitted(settings)
    except WorkerOperationalStopError as exc:
        return exc.state
    return None


class ReleaseProtocolTests(unittest.TestCase):
    def test_dry_run_reports_closure_and_writes_nothing(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root)
        sha = write_stop(settings)
        before = listing(control_dir(root))
        plan = plan_release(settings, expect_sha256=sha, supervision=_UNSUPERVISED, process_lister=_no_owners)
        self.assertEqual(
            {key: plan[key] for key in ("dry_run", "active_status", "active_sha256", "would_release",
                                        "native_closure", "process_closure", "singleton_owner",
                                        "archive_exists", "release_record_exists")},
            {"dry_run": True, "active_status": "valid", "active_sha256": sha, "would_release": True,
             "native_closure": "not_applicable", "process_closure": "no_known_owner",
             "singleton_owner": "unknown", "archive_exists": False, "release_record_exists": False},
        )
        self.assertEqual(listing(control_dir(root)), before)
        stdout = io.StringIO()
        real_plan = worker_cli.plan_release
        with (
            mock.patch.object(worker_cli, "load_settings", return_value=settings),
            mock.patch.object(worker_cli, "plan_release",
                              side_effect=lambda s, **kw: real_plan(s, process_lister=_no_owners, **kw)),
            mock.patch.object(worker_cli, "_held_worker_singleton",
                              side_effect=AssertionError("a dry run never takes the singleton")),
            redirect_stdout(stdout),
        ):
            code = worker_cli.main(["release-circuit", "--expect-sha256", sha, "--decided-by", "root",
                                    "--reason", "fixed", "--fixed-by", "r3", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["contract_version"], "worker-circuit-release-plan.v1")
        self.assertEqual(listing(control_dir(root)), before)

    def test_release_archives_and_records_before_removing_and_never_starts_anything(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root)
        sha = write_stop(settings)
        original = _active(root).read_bytes()
        operations: list[tuple[str, str]] = []
        real_link, real_unlink = os.link, os.unlink

        def link(source: object, target: object, **kwargs: object) -> None:
            operations.append(("link", Path(os.fsdecode(target)).name))  # type: ignore[arg-type]
            real_link(source, target, **kwargs)  # type: ignore[arg-type]

        def unlink(path: object, **kwargs: object) -> None:
            name = Path(os.fsdecode(path)).name  # type: ignore[arg-type]
            if not name.endswith(".partial"):
                operations.append(("unlink", name))
            real_unlink(path, **kwargs)  # type: ignore[arg-type]

        with mock.patch("os.link", link), mock.patch("os.unlink", unlink):
            receipt = _release(settings, sha)
        digest = sha.removeprefix("sha256:")
        archive = f"worker-circuit-stop.{digest}.json"
        record = f"worker-circuit-release.{digest}.json"
        self.assertEqual(operations, [("link", archive), ("link", record), ("unlink", "worker-circuit-stop.json")])
        self.assertEqual((receipt["released"], receipt["idempotent_replay"]), (True, False))
        self.assertEqual((control_dir(root) / archive).read_bytes(), original)
        decision = json.loads((control_dir(root) / record).read_bytes())
        self.assertEqual({key: decision[key] for key in ("decided_by", "reason", "fixed_by",
                                                         "released_stop_sha256")},
                         {**_DECISION, "released_stop_sha256": sha})
        self.assertEqual(receipt["release_record_sha256"], _sha((control_dir(root) / record).read_bytes()))
        for name in (archive, record):
            self.assertEqual(stat.S_IMODE((control_dir(root) / name).stat().st_mode), 0o600)
        self.assertFalse(_active(root).exists())
        self.assertIsNone(_refused_state(settings))
        self.assertIn("does not enable or start", str(receipt["note"]))

    def test_every_crash_point_keeps_the_stop_and_only_the_same_decision_completes(self) -> None:
        for crash, operation, target in (
            ("before the archive", "link", "worker-circuit-stop.{digest}.json"),
            ("after the archive, before the decision", "link", "worker-circuit-release.{digest}.json"),
            ("after the decision, before removal", "unlink", "worker-circuit-stop.json"),
        ):
            with self.subTest(crash=crash):
                root = trusted_root(self)
                settings = settings_for(root)
                sha = write_stop(settings)
                name = target.format(digest=sha.removeprefix("sha256:"))
                with _failing_once(operation, name) as failed, self.assertRaises(OSError):
                    _release(settings, sha)
                self.assertEqual(failed, [name])
                self.assertTrue(_active(root).exists(), "a crash never removes the stop")
                self.assertEqual(_refused_state(settings), "PUBLIC_STOP", "a release record alone never permits")
                if operation == "unlink":
                    # The decision is already durable: only the same one may finish.
                    with self.assertRaises(WorkerControlRefused):
                        _release(settings, sha, reason="a different decision")
                    self.assertTrue(_active(root).exists())
                receipt = _release(settings, sha)
                self.assertEqual(receipt["released"], True)
                self.assertFalse(_active(root).exists())
                self.assertIsNone(_refused_state(settings))

    def test_replay_after_completion_is_idempotent_only_for_the_same_decision(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root)
        sha = write_stop(settings)
        _release(settings, sha)
        after = listing(control_dir(root))
        self.assertTrue(_release(settings, sha)["idempotent_replay"])
        with self.assertRaises(WorkerControlRefused):
            _release(settings, sha, decided_by="someone-else")
        self.assertEqual({name: value[:2] for name, value in listing(control_dir(root)).items()},
                         {name: value[:2] for name, value in after.items()})

    def test_concurrent_releases_remove_the_stop_exactly_once(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root)
        sha = write_stop(settings)
        barrier = threading.Barrier(2)
        receipts: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def release() -> None:
            barrier.wait(timeout=5)
            try:
                receipts.append(_release(settings, sha))
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=release) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(receipt["idempotent_replay"] for receipt in receipts), [False, True])
        self.assertEqual(sorted(p.name for p in control_dir(root).iterdir() if not p.name.endswith(".lock")),
                         sorted([f"worker-circuit-stop.{sha.removeprefix('sha256:')}.json",
                                 f"worker-circuit-release.{sha.removeprefix('sha256:')}.json"]))

    def test_an_invalid_record_is_released_only_by_its_exact_bytes(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root)
        control_dir(root).mkdir(mode=0o700)
        corrupt = b'{"schema":"worker-circuit-stop.v1"'
        _active(root).write_bytes(corrupt)
        _active(root).chmod(0o600)
        self.assertEqual(_refused_state(settings), "INVALID_STOP")
        receipt = _release(settings, _sha(corrupt))
        self.assertEqual(
            (control_dir(root) / f"worker-circuit-stop.{_sha(corrupt).removeprefix('sha256:')}.json").read_bytes(),
            corrupt,
        )
        self.assertFalse(receipt["idempotent_replay"])
        with self.subTest(case="symlinked record needs trusted-storage repair first"):
            other = trusted_root(self)
            other_settings = settings_for(other)
            control_dir(other).mkdir(mode=0o700)
            (other / "target.json").write_bytes(corrupt)
            _active(other).symlink_to(other / "target.json")
            with self.assertRaisesRegex(WorkerControlRefused, "not releasable"):
                _release(other_settings, _sha(corrupt))
            self.assertTrue(_active(other).is_symlink())


class BoundedControlReadTests(unittest.TestCase):
    def test_archive_reads_stay_bounded_when_a_file_grows_and_refuse_symlinks(self) -> None:
        from disclosure_anchor.adapters.runtime.worker_stop_control import MAX_HASHED_INVALID_BYTES, WorkerControlStore
        from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder

        root = trusted_root(self)
        settings = settings_for(root)
        store = WorkerControlStore(FileStorePathBuilder(settings),
                                   runtime_root=root / "services" / "disclosure_anchor" / "runtime")
        control_dir(root).mkdir(mode=0o700)
        grown = control_dir(root) / "grown.json"
        grown.write_bytes(b"{}")
        os.truncate(grown, MAX_HASHED_INVALID_BYTES + 4096)
        real_fstat = os.fstat

        def small_fstat(fd: int) -> os.stat_result:
            # The file was small when it was opened and grew while being read.
            info = real_fstat(fd)
            if stat.S_ISREG(info.st_mode) and info.st_size > MAX_HASHED_INVALID_BYTES:
                values = list(info)
                values[stat.ST_SIZE] = 2
                return os.stat_result(values)
            return info

        with mock.patch("os.fstat", small_fstat), self.assertRaisesRegex(WorkerControlRefused, "grew beyond"):
            store.read_exact(grown)
        with self.assertRaisesRegex(WorkerControlRefused, "not a bounded regular file"):
            store.read_exact(grown)
        planted = control_dir(root) / "planted.json"
        planted.symlink_to(root / "elsewhere.json")
        with self.assertRaises(WorkerControlRefused):
            store.read_exact(planted)
        self.assertIsNone(store.read_exact(control_dir(root) / "absent.json"))


class NativeOnlyReconstructionTests(unittest.TestCase):
    def _marker_failed_evidence(self, root: Path, launchctl: ScriptedLaunchctl) -> bytes:
        """A real trip whose record could not be installed (root missing)."""

        runtime = root / "services" / "disclosure_anchor" / "runtime"
        runtime.rmdir()
        lines: list[str] = []
        control = durable_control(settings_for(root), launchctl=launchctl, lines=lines)
        control.trip(cause("forbidden_tool_call"))
        runtime.mkdir(mode=0o700)
        evidence = [line for line in lines if " STOP_RECORD " in line]
        self.assertEqual(len(evidence), 1)
        return (evidence[0].split(" STOP_RECORD ", 1)[1] + "\n").encode("ascii")

    def test_native_only_stop_is_reconstructed_truthfully_and_then_released(self) -> None:
        root = trusted_root(self)
        launchctl = ScriptedLaunchctl(disposable_label())
        evidence = self._marker_failed_evidence(root, launchctl)
        self.assertEqual(launchctl.disabled, "disabled", "the native channel held the stop")
        settings = settings_for(root, supervised=True, label=launchctl.label)
        supervisor = launchctl.supervisor()
        self.assertEqual(_refused_state_with(settings, launchctl), "OPERATOR_DISABLED")
        dry = reconstruct_worker_circuit_stop(
            settings, evidence=evidence, evidence_sha256=_sha(evidence), decided_by="root-f5",
            reason="record failed; native disable held", supervisor=supervisor, dry_run=True)
        self.assertEqual((dry["would_record"], dry["evidence_parsed_as_stop_record"]), (True, True))
        self.assertFalse(control_dir(root).exists(), "a dry run writes nothing")
        receipt = reconstruct_worker_circuit_stop(
            settings, evidence=evidence, evidence_sha256=_sha(evidence), decided_by="root-f5",
            reason="record failed; native disable held", supervisor=supervisor)
        record = StopRecord.decode(_active(root).read_bytes())
        original = StopRecord.decode(evidence)
        self.assertEqual(record.record_origin, "operator_reconstructed")
        self.assertIsNone(record.worker)
        self.assertEqual(record.cause, original.cause)
        self.assertEqual((record.native_disable.status, record.native_disable.detail),
                         ("verified_disabled", "operator_readback"))
        reconstruction = dict(record.reconstruction or ())
        self.assertEqual(
            {key: reconstruction[key] for key in ("evidence_sha256", "evidence_record_origin",
                                                  "evidence_recorded_at", "decided_by")},
            {"evidence_sha256": _sha(evidence), "evidence_record_origin": "automatic_fault",
             "evidence_recorded_at": original.recorded_at, "decided_by": "root-f5"},
        )
        self.assertEqual(receipt["record_sha256"], _sha(_active(root).read_bytes()))
        with self.assertRaises(WorkerControlRefused):
            reconstruct_worker_circuit_stop(
                settings, evidence=evidence, evidence_sha256=_sha(evidence), decided_by="root-f5",
                reason="again", supervisor=supervisor)
        _release(settings, str(receipt["record_sha256"]), supervision=launchctl.supervision())
        self.assertEqual(_refused_state_with(settings, launchctl), "OPERATOR_DISABLED",
                         "release never enables the label")
        self.assertNotIn("enable", launchctl.verbs())

    def test_reconstruction_needs_a_disabled_readback_and_matching_evidence(self) -> None:
        root = trusted_root(self)
        enabled = ScriptedLaunchctl(disposable_label(), disabled="enabled")
        settings = settings_for(root, supervised=True, label=enabled.label)
        log_excerpt = b"[worker-control] STOP_PERSISTENCE_FAILED (native-only); operator note\n"
        with self.assertRaisesRegex(WorkerControlRefused, "not natively disabled"):
            reconstruct_worker_circuit_stop(settings, evidence=log_excerpt, evidence_sha256=_sha(log_excerpt),
                                            decided_by="root-f5", reason="r", supervisor=enabled.supervisor())
        disabled = ScriptedLaunchctl(disposable_label(), disabled="disabled")
        with self.assertRaisesRegex(WorkerControlRefused, "do not match"):
            reconstruct_worker_circuit_stop(settings, evidence=log_excerpt, evidence_sha256="sha256:" + "1" * 64,
                                            decided_by="root-f5", reason="r", supervisor=disabled.supervisor())
        self.assertFalse(control_dir(root).exists())
        reconstruct_worker_circuit_stop(settings, evidence=log_excerpt, evidence_sha256=_sha(log_excerpt),
                                        decided_by="root-f5", reason="r", supervisor=disabled.supervisor())
        record = StopRecord.decode(_active(root).read_bytes())
        self.assertIsNone(record.cause, "unparsed evidence never invents a cause")
        self.assertIsNone(dict(record.reconstruction or ())["evidence_record_origin"])
        for launchctl in (enabled, disabled):
            self.assertTrue(set(launchctl.verbs()) <= {"print-disabled", "print"})

    def test_evidence_files_are_bounded_regular_and_hash_bound(self) -> None:
        root = trusted_root(self)
        good = root / "evidence.txt"
        good.write_bytes(b"bounded evidence\n")
        self.assertEqual(read_evidence(good, _sha(good.read_bytes())), b"bounded evidence\n")
        big = root / "big.txt"
        big.write_bytes(b"x" * (64 * 1024 + 1))
        link = root / "link.txt"
        link.symlink_to(good)
        for path, expected in ((big, _sha(big.read_bytes())), (link, _sha(good.read_bytes())),
                               (good, "sha256:" + "2" * 64)):
            with self.subTest(path=path.name), self.assertRaises(WorkerControlRefused):
                read_evidence(path, expected)

    def test_cli_refuses_reconstruction_without_a_supervised_job(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root)
        evidence = root / "evidence.txt"
        evidence.write_bytes(b"note\n")
        stderr = io.StringIO()
        with (
            mock.patch.object(worker_cli, "load_settings", return_value=settings),
            mock.patch.object(worker_cli, "_held_worker_singleton", side_effect=AssertionError("singleton taken")),
            redirect_stderr(stderr),
        ):
            code = worker_cli.main(["record-circuit-stop", "--from-disabled", "--evidence", str(evidence),
                                    "--evidence-sha256", _sha(b"note\n"), "--decided-by", "root",
                                    "--reason", "r"])
        self.assertEqual(code, 3)
        self.assertIn("no supervised launchd job owns this runtime root", stderr.getvalue())
        self.assertFalse(control_dir(root).exists())


def _refused_state_with(settings: Settings, launchctl: ScriptedLaunchctl) -> str | None:
    try:
        require_worker_start_permitted(settings, supervision=launchctl.supervision())
    except WorkerOperationalStopError as exc:
        return exc.state
    return None


if __name__ == "__main__":
    unittest.main()
