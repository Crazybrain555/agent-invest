"""Independent F5 acceptance: start gate, trusted storage, native tri-state, status.

Pro §6.1/§6.2 and root review R1/R3/R4: an active record, untrusted or
unreadable control storage, or (over the supervised root only) a known
disabled or unknown launchd label refuses every business start before any DB
engine, MinerU checker, profile/keyring, semantic runtime or publication; a
disabled label without a record is reported as OPERATOR_DISABLED and never as
an invented fault; temp/offline/non-macOS roots never run launchctl. Status
and doctor report the control state with the DB down.

Every supervised case injects a scripted launchctl for a disposable label.
"""

from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime import doctor, staged_worker_v4, worker_stop_control
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import MinerUDeploymentChecker
from disclosure_anchor.adapters.runtime.staged_worker_v4 import build_staged_worker_v4_runtime
from disclosure_anchor.adapters.runtime.worker_stop_control import (
    CONTROL_STATUS_CONTRACT,
    WorkerControlStore,
    control_status_payload,
    observe_worker_control,
    require_worker_start_permitted,
    worker_supervision,
)
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.api.errors import FilingApiError
from disclosure_anchor.api.routers import admin
from disclosure_anchor.application.ports.worker_stop_control import WorkerOperationalStopError
from disclosure_anchor.application.services.worker_stop_latch import InProcessWorkerStopLatch
from disclosure_anchor.cli import pipeline
from disclosure_anchor.cli import worker as worker_cli
from disclosure_anchor.settings import SENTINEL_NAME, Settings
from scripts import generate_current_source_replay as replay
from tests.unit._f5_stop_fixture import (
    ScriptedLaunchctl,
    cause,
    control_dir,
    disposable_label,
    listing,
    runtime_root,
    settings_for,
    trusted_root,
    write_stop,
)
from tests.unit.test_settings import _mineru_topology


_DOWN_DATABASE = {"DATABASE_URL": "postgresql+psycopg://app@127.0.0.1:9/f5_database_is_down"}


class _ReachedBusiness(Exception):
    """Raised by the first business dependency; proves the gate let a start through."""


def _write_active(root: Path, payload: bytes, *, mode: int = 0o600) -> Path:
    directory = control_dir(root)
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    path = directory / "worker-circuit-stop.json"
    path.write_bytes(payload)
    path.chmod(mode)
    return path


def _regular_file_on_another_volume(device: int) -> Path | None:
    """An existing regular file (lstat only) on a volume other than ``device``."""

    volumes = Path("/System/Volumes")
    try:
        mounts = sorted(volumes.iterdir()) if volumes.is_dir() else []
    except OSError:
        return None
    for mount in mounts:
        try:
            entries = sorted(mount.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                info = os.lstat(entry)
            except OSError:
                continue
            if info.st_dev != device and entry.is_file() and not entry.is_symlink():
                return entry
    return None


def _refusal(settings: Settings, **kwargs: object) -> WorkerOperationalStopError:
    try:
        require_worker_start_permitted(settings, **kwargs)  # type: ignore[arg-type]
    except WorkerOperationalStopError as exc:
        return exc
    raise AssertionError("the start gate let a non-runnable state through")


class TrustedControlStorageTests(unittest.TestCase):
    def test_absent_record_under_a_trusted_root_is_runnable_and_read_only(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root)
        before = listing(runtime_root(root))
        snapshot = observe_worker_control(settings)
        require_worker_start_permitted(settings)
        self.assertEqual(snapshot.state, "RUNNABLE")
        self.assertEqual(snapshot.supervision, "unsupervised_runtime_root")
        self.assertIsNone(snapshot.native)
        self.assertEqual(listing(runtime_root(root)), before, "a read never creates control state")
        # Group read on the root is not a trust problem (adjacent negative).
        runtime_root(root).chmod(0o750)
        require_worker_start_permitted(settings)

    def test_every_untrusted_or_unreadable_state_refuses_and_never_reads_as_absent(self) -> None:
        corrupt = b'{"schema":"worker-circuit-stop.v1","truncated":'
        cases: list[tuple[str, object, str, str]] = [
            ("runtime root missing", lambda root: runtime_root(root).rmdir(),
             "CONTROL_UNAVAILABLE", "runtime_root_missing"),
            ("runtime root is a symlink", "symlink_root", "CONTROL_UNAVAILABLE", "runtime_root_symlink"),
            ("runtime root is a file", "file_root", "CONTROL_UNAVAILABLE", "runtime_root_not_directory"),
            ("runtime root world writable", lambda root: runtime_root(root).chmod(0o777),
             "CONTROL_UNAVAILABLE", "runtime_root_group_or_world_writable"),
            ("control dir is a symlink", "symlink_control", "CONTROL_UNAVAILABLE", "control_dir_not_directory"),
            ("control dir 0755", lambda root: control_dir(root).mkdir(mode=0o755) or control_dir(root).chmod(0o755),
             "CONTROL_UNAVAILABLE", "control_dir_mode_not_0700"),
            ("active record is a symlink", "symlink_active", "INVALID_STOP", "active_symlink"),
            ("active record is a FIFO", "fifo_active", "INVALID_STOP", "active_not_regular"),
            ("active record corrupt", lambda root: _write_active(root, corrupt), "INVALID_STOP", "active_corrupt"),
            ("active record group readable", "group_readable_active", "INVALID_STOP", "active_mode_not_0600"),
            ("active record over the record bound", lambda root: _write_active(root, b"{" + b" " * 70_000 + b"}"),
             "INVALID_STOP", "active_oversized"),
            ("active record beyond the hashing bound", "huge_active", "INVALID_STOP", "active_oversized_unhashed"),
        ]
        if os.geteuid() != 0:
            cases.append(("active record unreadable", "unreadable_active", "INVALID_STOP", "active_open_eacces"))
        for name, arrange, state, problem in cases:
            with self.subTest(case=name):
                root = trusted_root(self)
                settings = settings_for(root)
                if callable(arrange):
                    arrange(root)
                else:
                    self._arrange(root, settings, str(arrange))
                snapshot = observe_worker_control(settings)
                self.assertEqual((snapshot.state, snapshot.active.problem), (state, problem))
                refused = _refusal(settings)
                self.assertEqual(refused.state, state)
                self.assertNotIn(str(root), str(refused), "refusals never name absolute paths")
                if problem in ("active_corrupt", "active_mode_not_0600", "active_oversized"):
                    raw = (control_dir(root) / "worker-circuit-stop.json").read_bytes()
                    self.assertEqual(refused.active_sha256, "sha256:" + hashlib.sha256(raw).hexdigest())

    def _arrange(self, root: Path, settings: Settings, kind: str) -> None:
        runtime = runtime_root(root)
        if kind == "symlink_root":
            real = root / "stand-in-runtime"
            real.mkdir(mode=0o700)
            runtime.rmdir()
            runtime.symlink_to(real, target_is_directory=True)
        elif kind == "file_root":
            runtime.rmdir()
            runtime.write_text("", encoding="utf-8")
        elif kind == "symlink_control":
            real = root / "stand-in-control"
            real.mkdir(mode=0o700)
            control_dir(root).symlink_to(real, target_is_directory=True)
        elif kind == "symlink_active":
            target = root / "elsewhere.json"
            target.write_bytes(b"{}")
            control_dir(root).mkdir(mode=0o700)
            (control_dir(root) / "worker-circuit-stop.json").symlink_to(target)
        elif kind == "fifo_active":
            control_dir(root).mkdir(mode=0o700)
            os.mkfifo(control_dir(root) / "worker-circuit-stop.json", 0o600)
        elif kind == "group_readable_active":
            write_stop(settings)
            (control_dir(root) / "worker-circuit-stop.json").chmod(0o640)
        elif kind == "huge_active":
            path = _write_active(root, b"")
            os.truncate(path, 16 * 1024 * 1024 + 1)
        elif kind == "unreadable_active":
            write_stop(settings)
            path = control_dir(root) / "worker-circuit-stop.json"
            path.chmod(0o000)
            self.addCleanup(path.chmod, 0o600)
        else:  # pragma: no cover - the case table is closed
            raise AssertionError(kind)

    def test_a_valid_record_refuses_by_its_exact_hash(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root)
        sha = write_stop(settings)
        refused = _refusal(settings)
        self.assertEqual((refused.state, refused.active_sha256), ("PUBLIC_STOP", sha))
        self.assertIn("forbidden_tool_call", refused.detail)
        self.assertIn("release-circuit", refused.detail)

    def test_foreign_owned_runtime_root_is_not_trusted(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root)
        store = WorkerControlStore(
            FileStorePathBuilder(settings), runtime_root=runtime_root(root), uid=os.getuid() + 1,
        )
        self.assertEqual(
            (store.read_active().status, store.read_active().problem),
            ("unavailable", "runtime_root_foreign_owner"),
        )

    def test_supervised_root_requires_the_mount_sentinel_on_the_same_volume(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root, supervised=True)
        launchctl = ScriptedLaunchctl(disposable_label())
        supervision = launchctl.supervision()
        self.assertEqual(observe_worker_control(settings, supervision=supervision).state, "RUNNABLE")
        sentinel = root / SENTINEL_NAME
        sentinel.unlink()
        self.assertEqual(
            observe_worker_control(settings, supervision=supervision).active.problem,
            "mount_sentinel_missing",
        )
        sentinel.mkdir()
        self.assertEqual(
            observe_worker_control(settings, supervision=supervision).active.problem,
            "mount_sentinel_not_regular",
        )
        self.assertEqual(_refusal(settings, supervision=supervision).state, "CONTROL_UNAVAILABLE")
        # The same temp root without the supervised binding needs no sentinel.
        self.assertEqual(observe_worker_control(settings_for(root)).state, "RUNNABLE")

    def test_runtime_root_off_the_sentinel_volume_is_refused(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root)
        other = _regular_file_on_another_volume(os.lstat(runtime_root(root)).st_dev)
        if other is None:
            self.skipTest("no regular file on a second volume is available on this host")
        store = WorkerControlStore(
            FileStorePathBuilder(settings), runtime_root=runtime_root(root), mount_sentinel=other,
        )
        self.assertEqual(store.read_active().problem, "runtime_root_off_sentinel_volume")


class NativeSupervisionTests(unittest.TestCase):
    def _state(self, launchctl: ScriptedLaunchctl, *, record: bool = False) -> tuple[str, object]:
        root = trusted_root(self)
        settings = settings_for(root, supervised=True, label=launchctl.label)
        if record:
            write_stop(settings_for(root))
        snapshot = observe_worker_control(settings, supervision=launchctl.supervision())
        return snapshot.state, snapshot

    def test_readback_is_tri_state_and_only_known_enabled_runs(self) -> None:
        timeout = subprocess.TimeoutExpired(["launchctl"], 10)
        cases = (
            ("disabled, not loaded", dict(disabled="disabled"), "OPERATOR_DISABLED"),
            ("disabled literal true", dict(disabled="true"), "OPERATOR_DISABLED"),
            ("explicitly enabled, exact not-found pair", dict(disabled="enabled"), "RUNNABLE"),
            ("unlisted override, exact not-found pair", dict(), "RUNNABLE"),
            ("loaded and running", dict(loaded="running", pid=4242), "RUNNABLE"),
            ("loaded, last exit 0", dict(loaded="not running", last_exit="0"), "RUNNABLE"),
            ("loaded, last exit 78: EX_CONFIG", dict(loaded="not running", last_exit="78: EX_CONFIG"),
             "SUPERVISOR_ONLY_STOP"),
            ("print-disabled exits nonzero", dict(failures={"print-disabled": 1}), "CONTROL_UNAVAILABLE"),
            ("print-disabled times out", dict(failures={"print-disabled": timeout}), "CONTROL_UNAVAILABLE"),
            ("print-disabled without its table", dict(disabled="disabled", table=False), "CONTROL_UNAVAILABLE"),
            ("unknown override value", dict(disabled="perhaps"), "CONTROL_UNAVAILABLE"),
            ("print exits 5", dict(failures={"print": 5}), "CONTROL_UNAVAILABLE"),
            ("print times out", dict(failures={"print": timeout}), "CONTROL_UNAVAILABLE"),
            ("launchctl cannot run", dict(failures={
                "print-disabled": PermissionError(13, "denied"), "print": PermissionError(13, "denied")}),
             "CONTROL_UNAVAILABLE"),
        )
        for name, script, expected in cases:
            with self.subTest(case=name):
                launchctl = ScriptedLaunchctl(disposable_label(), **script)  # type: ignore[arg-type]
                state, snapshot = self._state(launchctl)
                self.assertEqual(state, expected)
                self.assertTrue(set(launchctl.verbs()) <= {"print", "print-disabled"})
                payload = control_status_payload(snapshot)  # type: ignore[arg-type]
                self.assertEqual(payload["contract_version"], CONTROL_STATUS_CONTRACT)
                self.assertEqual(payload["supervision"], "supervised")
                self.assertIsNone(payload["control_record"]["cause"], "native state never invents a cause")  # type: ignore[index]
                if expected != "RUNNABLE":
                    root = trusted_root(self)
                    refused = _refusal(settings_for(root, supervised=True, label=launchctl.label),
                                       supervision=launchctl.supervision())
                    self.assertEqual(refused.state, expected)

    def test_only_the_exact_not_found_pair_means_not_loaded(self) -> None:
        launchctl = ScriptedLaunchctl(disposable_label())
        original = launchctl._print

        def other_service(argv: list[str]) -> subprocess.CompletedProcess[str]:
            completed = original(argv)
            return subprocess.CompletedProcess(
                argv, completed.returncode, "",
                'Could not find service "com.agentinvest.f5-acceptance.other" in domain for user gui\n',
            )

        launchctl._print = other_service  # type: ignore[method-assign]
        state, snapshot = self._state(launchctl)
        self.assertEqual(state, "CONTROL_UNAVAILABLE")
        self.assertIsNone(snapshot.native.loaded)  # type: ignore[attr-defined]
        self.assertEqual(snapshot.native.closure, "unknown")  # type: ignore[attr-defined]

    def test_a_record_decides_before_any_native_read(self) -> None:
        launchctl = ScriptedLaunchctl(disposable_label(), disabled="disabled")
        root = trusted_root(self)
        settings = settings_for(root, supervised=True, label=launchctl.label)
        sha = write_stop(settings_for(root))
        refused = _refusal(settings, supervision=launchctl.supervision())
        self.assertEqual((refused.state, refused.active_sha256), ("PUBLIC_STOP", sha))
        self.assertEqual(launchctl.calls, [], "the gate reads native state only without a record")

    def test_unsupervised_and_non_macos_roots_never_build_a_supervisor(self) -> None:
        root = trusted_root(self)
        unsupervised = settings_for(root, label=disposable_label())
        self.assertEqual(worker_supervision(unsupervised).scope, "unsupervised_runtime_root")
        self.assertIsNone(worker_supervision(unsupervised).supervisor)
        supervised = settings_for(root, supervised=True, label=disposable_label())
        with mock.patch("sys.platform", "linux"):
            scope = worker_supervision(supervised)
            snapshot = observe_worker_control(supervised)
            require_worker_start_permitted(supervised)
        self.assertEqual((scope.scope, scope.supervisor), ("not_macos", None))
        self.assertEqual((snapshot.state, snapshot.native), ("RUNNABLE", None))


class _Supervised:
    """Bind the supervised root to a scripted disposable label for entry tests."""

    def __init__(self, test: unittest.TestCase, **script: object) -> None:
        self.launchctl = ScriptedLaunchctl(disposable_label(), **script)  # type: ignore[arg-type]
        self.root = trusted_root(test)
        self.settings = settings_for(self.root, supervised=True, label=self.launchctl.label)
        patcher = mock.patch.object(
            worker_stop_control, "worker_supervision", return_value=self.launchctl.supervision(),
        )
        patcher.start()
        test.addCleanup(patcher.stop)


def _refusing_business() -> list[mock._patch]:  # type: ignore[type-arg]
    blocked = AssertionError("business composition started behind a stop")
    return [
        mock.patch.object(worker_cli, "run_resident_worker", side_effect=blocked),
        mock.patch.object(worker_cli, "MinerUDeploymentChecker", side_effect=blocked),
        mock.patch.object(worker_cli.sqlalchemy, "create_engine", side_effect=blocked),
        mock.patch.object(worker_cli, "create_db_engine", side_effect=blocked),
        mock.patch.object(worker_cli, "_load_staged_process_profile", side_effect=blocked),
    ]


class EntryGateTests(unittest.TestCase):
    def _main(self, argv: list[str], settings: Settings) -> tuple[int, str]:
        stderr = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(worker_cli, "load_settings", return_value=settings))
            for patcher in _refusing_business():
                stack.enter_context(patcher)
            stack.enter_context(redirect_stderr(stderr))
            code = worker_cli.main(argv)
        return code, stderr.getvalue()

    def test_known_disabled_label_without_record_refuses_loop_and_once_before_business(self) -> None:
        for command in ("loop", "once"):
            with self.subTest(command=command):
                supervised = _Supervised(self, disabled="disabled")
                code, stderr = self._main([command], supervised.settings)
                self.assertEqual(code, 78)
                self.assertIn("OPERATOR_DISABLED", stderr)
                self.assertEqual(set(supervised.launchctl.verbs()), {"print-disabled", "print"})
                self.assertFalse(control_dir(supervised.root).exists(), "refusal writes nothing")

    def test_every_non_runnable_state_refuses_worker_starts_with_78(self) -> None:
        cases = {
            "PUBLIC_STOP": lambda s: write_stop(settings_for(s.root)),
            "INVALID_STOP": lambda s: _write_active(s.root, b"not json"),
            "CONTROL_UNAVAILABLE": lambda s: runtime_root(s.root).chmod(0o777),
            "SUPERVISOR_ONLY_STOP": lambda s: setattr(s.launchctl, "loaded", "not running")
            or setattr(s.launchctl, "last_exit", "78: EX_CONFIG"),
        }
        for state, arrange in cases.items():
            with self.subTest(state=state):
                supervised = _Supervised(self)
                arrange(supervised)
                code, stderr = self._main(["loop"], supervised.settings)
                self.assertEqual(code, 78)
                self.assertIn(state, stderr)
        with self.subTest(state="CONTROL_UNAVAILABLE (unknown launchd)"):
            supervised = _Supervised(self, failures={"print": 5})
            code, stderr = self._main(["loop"], supervised.settings)
            self.assertEqual(code, 78)
            self.assertIn("CONTROL_UNAVAILABLE", stderr)

    def test_runnable_roots_reach_business_without_launchctl(self) -> None:
        with self.subTest(root="temp, unsupervised"):
            root = trusted_root(self)
            settings = settings_for(root)
            with (
                mock.patch.object(worker_cli, "load_settings", return_value=settings),
                mock.patch.object(worker_cli, "run_resident_worker", return_value=0) as resident,
            ):
                self.assertEqual(worker_cli.main(["loop"]), 0)
            resident.assert_called_once()
        with self.subTest(root="supervised, known enabled"):
            supervised = _Supervised(self, disabled="enabled")
            with (
                mock.patch.object(worker_cli, "load_settings", return_value=supervised.settings),
                mock.patch.object(worker_cli, "MinerUDeploymentChecker", side_effect=_ReachedBusiness),
                self.assertRaises(_ReachedBusiness),
            ):
                worker_cli.main(["once"])

    def test_resident_rechecks_under_the_singleton_and_busy_is_75_without_record(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root, extra=_DOWN_DATABASE)
        lock_conn = mock.MagicMock()
        lock_engine = mock.MagicMock()
        lock_engine.connect.return_value = lock_conn
        acquired = {"value": True}

        def try_lock(*_args: object, **_kwargs: object) -> mock.MagicMock:
            # A stop recorded by the previous owner while we waited for the lock.
            if acquired["value"]:
                write_stop(settings)
            result = mock.MagicMock()
            result.scalar_one.return_value = acquired["value"]
            return result

        lock_conn.execute.side_effect = try_lock

        def resident(stack: ExitStack) -> mock.MagicMock:
            stack.enter_context(mock.patch.object(worker_cli, "MinerUDeploymentChecker"))
            stack.enter_context(mock.patch.object(worker_cli, "_print_version_banner"))
            stack.enter_context(
                mock.patch.object(worker_cli.sqlalchemy, "create_engine", return_value=lock_engine))
            stack.enter_context(mock.patch.object(worker_cli, "require_runtime_app_connection"))
            return stack.enter_context(
                mock.patch.object(worker_cli, "_run_loop", side_effect=AssertionError("business started")))

        with self.subTest(case="stop recorded before the singleton was taken"):
            stderr = io.StringIO()
            with ExitStack() as stack:
                run_loop = resident(stack)
                stack.enter_context(redirect_stderr(stderr))
                self.assertEqual(worker_cli.run_resident_worker(settings), 78)
            run_loop.assert_not_called()
            self.assertIn("PUBLIC_STOP", stderr.getvalue())
            lock_conn.close.assert_called_once_with()
        with self.subTest(case="singleton held by another owner"):
            busy_root = trusted_root(self)
            busy_settings = settings_for(busy_root, extra=_DOWN_DATABASE)
            acquired["value"] = False
            before = listing(runtime_root(busy_root))
            with ExitStack() as stack:
                run_loop = resident(stack)
                stack.enter_context(redirect_stdout(io.StringIO()))
                self.assertEqual(worker_cli.run_resident_worker(busy_settings), 75)
            run_loop.assert_not_called()
            self.assertEqual(listing(runtime_root(busy_root)), before, "busy is not a public stop")

    def test_known_disabled_label_refuses_under_the_singleton_too(self) -> None:
        supervised = _Supervised(self, disabled="disabled")
        settings = settings_for(supervised.root, supervised=True, label=supervised.launchctl.label,
                                extra=_DOWN_DATABASE)
        lock_conn = mock.MagicMock()
        lock_conn.execute.return_value.scalar_one.return_value = True
        lock_engine = mock.MagicMock()
        lock_engine.connect.return_value = lock_conn
        stderr = io.StringIO()
        with (
            mock.patch.object(worker_cli, "MinerUDeploymentChecker"),
            mock.patch.object(worker_cli, "_print_version_banner"),
            mock.patch.object(worker_cli.sqlalchemy, "create_engine", return_value=lock_engine),
            mock.patch.object(worker_cli, "require_runtime_app_connection"),
            mock.patch.object(worker_cli, "_run_loop", side_effect=AssertionError("business started")) as run_loop,
            redirect_stderr(stderr),
        ):
            self.assertEqual(worker_cli.run_resident_worker(settings), 78)
        run_loop.assert_not_called()
        self.assertIn("OPERATOR_DISABLED", stderr.getvalue())
        self.assertFalse(control_dir(supervised.root).exists())

    def test_builder_refuses_before_profile_keyring_or_remote_composition(self) -> None:
        engine = mock.MagicMock(spec=[])

        def guard() -> None:
            raise AssertionError("scope guard called for an unscoped build")

        def build(settings: Settings, **kwargs: object) -> None:
            build_staged_worker_v4_runtime(
                settings=settings, engine=engine, ownership_guard=guard, admission_guard=guard,
                process_scope_classes=None, progress=lambda _snapshot: None, **kwargs,  # type: ignore[arg-type]
            )

        staged = {"WORKER_PARSE_EXECUTION_MODE": "staged-v4", **_mineru_topology()}
        blocked = AssertionError("profile/keyring/remote composition started behind a stop")
        with (
            mock.patch.object(staged_worker_v4, "load_staged_v4_settings", side_effect=blocked),
            mock.patch.object(staged_worker_v4, "MinerUHttpRemoteV4", side_effect=blocked),
            mock.patch.object(staged_worker_v4, "build_semantic_runtime", side_effect=blocked),
        ):
            with self.subTest(case="active record"):
                root = trusted_root(self)
                settings = settings_for(root, extra=staged)
                sha = write_stop(settings_for(root))
                with self.assertRaises(WorkerOperationalStopError) as refused:
                    build(settings)
                self.assertEqual((refused.exception.state, refused.exception.active_sha256), ("PUBLIC_STOP", sha))
            with self.subTest(case="known disabled label without record"):
                supervised = _Supervised(self, disabled="disabled")
                settings = settings_for(supervised.root, supervised=True, label=supervised.launchctl.label,
                                        extra=staged)
                with self.assertRaises(WorkerOperationalStopError) as refused:
                    build(settings)
                self.assertEqual(refused.exception.state, "OPERATOR_DISABLED")
            with self.subTest(case="latched in this process, nothing persisted"):
                latch = InProcessWorkerStopLatch()
                latch.trip(cause())
                root = trusted_root(self)
                with self.assertRaises(WorkerOperationalStopError) as refused:
                    build(settings_for(root, extra=staged), stop_control=latch)
                self.assertEqual(refused.exception.state, "PUBLIC_STOP")
                self.assertIsNone(refused.exception.active_sha256)
                self.assertFalse(control_dir(root).exists())

    def test_manual_pipeline_admin_and_replay_entries_refuse_before_semantic_or_parse_effects(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root, extra=_DOWN_DATABASE)
        sha = write_stop(settings)
        blocked = AssertionError("semantic/parse/publication composition started behind a stop")
        commands = (
            ["build-units", "--document-id", "doc_f5"],
            ["publish", "--processing-run-id", "run_f5"],
            ["process", "--document-id", "doc_f5"],
            ["rebuild-units", "--document-id", "doc_f5"],
        )
        for argv in commands:
            with self.subTest(pipeline=argv[0]):
                stderr = io.StringIO()
                with (
                    mock.patch.object(pipeline, "load_settings", return_value=settings),
                    mock.patch.object(pipeline, "MinerUDeploymentChecker",
                                      return_value=mock.MagicMock(spec=MinerUDeploymentChecker)),
                    mock.patch.object(pipeline, "create_db_engine", return_value=mock.MagicMock()),
                    mock.patch.object(pipeline, "require_runtime_app_engine"),
                    mock.patch.object(pipeline, "build_semantic_runtime", side_effect=blocked),
                    mock.patch.object(pipeline, "exclusive_worker_admission", side_effect=blocked),
                    mock.patch.object(pipeline._Deps, "parse", side_effect=blocked),
                    mock.patch.object(pipeline._Deps, "rebuild_units", side_effect=blocked),
                    redirect_stderr(stderr),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(pipeline.main(argv), 78)
                self.assertIn(f"PUBLIC_STOP ({sha})", stderr.getvalue())
        with self.subTest(pipeline="track-status stays available while stopped"):
            with (
                mock.patch.object(pipeline, "load_settings", return_value=settings),
                mock.patch.object(pipeline, "create_db_engine", return_value=mock.MagicMock()),
                mock.patch.object(pipeline, "require_runtime_app_engine"),
                mock.patch.object(pipeline._Deps, "track_status", return_value=[]),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(pipeline.main(["track-status"]), 0)
        with self.subTest(entry="admin build/publish"):
            deps = admin.AdminDeps(settings=settings, engine=mock.MagicMock())
            request = mock.MagicMock()
            request.app.state.admin_deps = deps
            with mock.patch.object(admin, "build_semantic_runtime", side_effect=blocked):
                for call in (
                    lambda: admin.build_document_units("doc_f5", request),
                    lambda: admin.publish_run(
                        "run_f5", request, admin.PublishRunRequest(allow_empty=False, reason=None)),
                ):
                    with self.assertRaises(FilingApiError) as refused:
                        call()
                    self.assertEqual(refused.exception.status_code, 503)
                    self.assertEqual(refused.exception.error_code, "SERVICE_UNAVAILABLE")
                    self.assertIn(sha, refused.exception.message)
                    self.assertNotIn(str(root), json.dumps(refused.exception.body(), default=str))
        with self.subTest(entry="current-source replay script"):
            with tempfile.TemporaryDirectory() as out:
                evaluation, receipt = Path(out) / "evaluation.json", Path(out) / "receipt.json"
                with (
                    mock.patch.object(replay, "load_settings", return_value=settings),
                    mock.patch.object(replay, "build_semantic_runtime", side_effect=blocked),
                    redirect_stderr(io.StringIO()),
                ):
                    code = replay.main(["--evaluation-output", str(evaluation),
                                        "--receipt-output", str(receipt), "--source-revision", "f5"])
                self.assertEqual(code, 78)
                self.assertEqual(sorted(p.name for p in Path(out).iterdir()), [])


class StatusAndDoctorTests(unittest.TestCase):
    def _status(self, settings: Settings, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(worker_cli, "load_settings", return_value=settings),
            mock.patch.object(worker_cli, "create_db_engine",
                              side_effect=ConnectionRefusedError("database is down")),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            try:
                code = worker_cli.main(argv)
            except ConnectionRefusedError:
                code = -1
        return code, stdout.getvalue(), stderr.getvalue()

    def test_control_only_status_needs_no_database_and_reports_the_record(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root, extra=_DOWN_DATABASE)
        code, stdout, _ = self._status(settings, ["status", "--control-only", "--format", "json"])
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual((payload["contract_version"], payload["state"], payload["runnable"]),
                         (CONTROL_STATUS_CONTRACT, "RUNNABLE", True))
        self.assertEqual(payload["supervision"], "unsupervised_runtime_root")
        self.assertIsNone(payload["native_supervisor"])
        sha = write_stop(settings)
        code, stdout, _ = self._status(settings, ["status", "--control-only", "--format", "json"])
        self.assertEqual(code, 3)
        payload = json.loads(stdout)
        self.assertEqual((payload["state"], payload["control_record"]["sha256"]), ("PUBLIC_STOP", sha))
        self.assertEqual(payload["control_record"]["cause"]["reason_code"], "forbidden_tool_call")
        self.assertNotIn(str(root), stdout)
        code, stdout, _ = self._status(settings, ["status", "--control-only"])
        self.assertEqual(code, 3)
        self.assertIn("state=PUBLIC_STOP", stdout)

    def test_plain_status_reports_the_stop_before_the_business_query(self) -> None:
        root = trusted_root(self)
        settings = settings_for(root, extra=_DOWN_DATABASE)
        write_stop(settings)
        code, stdout, stderr = self._status(settings, ["status"])
        self.assertEqual(code, -1, "the business query still ran (and found the DB down)")
        self.assertIn("[worker-control] STOPPED", stderr)
        self.assertIn("PUBLIC_STOP", stderr)
        self.assertEqual(stdout, "")

    def test_doctor_reports_control_state_with_the_database_down_and_never_invents_a_fault(self) -> None:
        with self.subTest(state="RUNNABLE"):
            result = doctor.worker_operational_control_check(settings_for(trusted_root(self)))
            self.assertEqual(result.status, "PASS")
            self.assertIn("no native supervisor applies", result.message)
        with self.subTest(state="PUBLIC_STOP"):
            root = trusted_root(self)
            sha = write_stop(settings_for(root))
            result = doctor.worker_operational_control_check(settings_for(root))
            self.assertEqual(result.status, "FAIL")
            self.assertIn(sha, result.message)
        with self.subTest(state="CONTROL_UNAVAILABLE"):
            root = trusted_root(self)
            runtime_root(root).rmdir()
            self.assertEqual(doctor.worker_operational_control_check(settings_for(root)).status, "FAIL")
        for script, state in ((dict(disabled="disabled"), "OPERATOR_DISABLED"),
                              (dict(loaded="not running", last_exit="78: EX_CONFIG"), "SUPERVISOR_ONLY_STOP")):
            with self.subTest(state=state):
                supervised = _Supervised(self, **script)
                result = doctor.worker_operational_control_check(supervised.settings)
                self.assertEqual(result.status, "WARN")
                self.assertIn(state, result.message)
                self.assertIn("no stop record", result.message)
        with self.subTest(state="PUBLIC_STOP with DATABASE_URL absent"):
            root = trusted_root(self)
            settings = settings_for(root)
            write_stop(settings)
            quiet = [
                mock.patch.object(doctor, name, return_value=[] if name.endswith("checks") else None)
                for name in ("_environment_checks", "_reader_database_url_checks", "_disk_headroom_checks")
            ]
            single = [
                mock.patch.object(doctor, name, return_value=doctor._pass(name, "isolated"))
                for name in ("_mineru_orphan_check", "mineru_orchestrator_check",
                             "mineru_remote_inference_check", "_ops_launchd_check")
            ]
            for patcher in (*quiet, *single):
                patcher.start()
                self.addCleanup(patcher.stop)
            report = doctor.run_doctor(settings)
            control = [item for item in report.results if item.name == "worker operational control"]
            self.assertEqual([item.status for item in control], ["FAIL"])


if __name__ == "__main__":
    unittest.main()
