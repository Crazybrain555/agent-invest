"""Bounded private evidence of failed semantic CLI calls (offline)."""

from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from disclosure_anchor.adapters.semantics import codex_cli, failure_diagnostics
from disclosure_anchor.adapters.semantics.claude_cli import ClaudeCliSemanticAdjudicator
from disclosure_anchor.adapters.semantics.codex_cli import CodexCliSemanticAdjudicator
from disclosure_anchor.adapters.semantics.failure_diagnostics import (
    FAILURE_DIAGNOSTIC_DIRECTORY,
    FAILURE_DIAGNOSTIC_SCHEMA,
    MAX_RECORD_BYTES,
    PrivateFailureDiagnostics,
)
from disclosure_anchor.application.ports.semantic_routes import SemanticRouteAdjudicatorError
from disclosure_anchor.application.ports.staged_execution import semantic_group_scope
from disclosure_anchor.application.services.semantic_adjudication import (
    ConfiguredSemanticProvider,
    OrderedSemanticAdjudicationExecutor,
)
from tests.unit._codex_model_catalog_fixture import neutral_catalog
from tests.unit.test_semantic_adjudication import _Adapter, _Cache, _GROUP_HASH, _identity
from tests.unit.test_semantic_codex_cli import _batch

_CODEX_LOGGER = "disclosure_anchor.adapters.semantics.codex_cli"
_RECORD_NAME = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9]+-[0-9a-f]{16}\.json")
QUOTA = "Quota exceeded. Check your plan and billing details."
WEBSOCKET_STDERR = (
    "2026-10-07T04:49:05.000001Z ERROR codex_api::endpoint::responses_websocket: failed to connect"
    " to websocket: IO error: Connection refused (os error 61), url: wss://chatgpt.com/backend-api/codex/responses\n"
)


def _event(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _failed(message: str, *siblings: str) -> str:
    return "\n".join((
        _event({"type": "thread.started", "thread_id": "THREAD-ID-NOT-EVIDENCE"}),
        _event({"type": "turn.started"}),
        *siblings,
        _event({"type": "error", "message": message}),
        _event({"type": "turn.failed", "error": {"message": message}}),
    ))


def _records(root: Path) -> list[Path]:
    directory = root / FAILURE_DIAGNOSTIC_DIRECTORY
    return sorted(path for path in directory.iterdir() if _RECORD_NAME.fullmatch(path.name))


def _log_record(output: list[str]) -> dict[str, object]:
    (line,) = output
    return json.loads(line.split("semantic_provider_failure ", 1)[1])


class _Run:
    """One synthetic CLI completion; optionally leaves a result file behind."""

    def __init__(self, returncode: int, stdout: str, stderr: str = "", result: str | None = None) -> None:
        self.completed = (returncode, stdout, stderr)
        self.result = result

    def __call__(self, *, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if self.result is not None:
            Path(args[args.index("--output-last-message") + 1]).write_text(self.result, encoding="utf-8")
        returncode, stdout, stderr = self.completed
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def _codex(root: Path) -> CodexCliSemanticAdjudicator:
    return CodexCliSemanticAdjudicator(
        executable=Path("/synthetic/codex"), runtime_tmp_root=root, model_catalog=neutral_catalog(),
    )


class PrivateFailureDiagnosticsTests(unittest.TestCase):
    def _adjudicate(
        self, root: Path, run: _Run,
    ) -> tuple[SemanticRouteAdjudicatorError, dict[str, object]]:
        with (
            mock.patch.object(codex_cli, "_run_process", side_effect=run),
            semantic_group_scope(_GROUP_HASH),
            self.assertLogs(_CODEX_LOGGER, level="WARNING") as logs,
            self.assertRaises(SemanticRouteAdjudicatorError) as caught,
        ):
            _codex(root).adjudicate(_batch())
        return caught.exception, _log_record(logs.output)

    def test_failure_keeps_redacted_terminal_text_privately_and_logs_only_its_reference(self) -> None:
        # The incident's shape: exit 1, one error event plus the failed turn.
        notice = _event({"type": "error", "message": (
            "Reconnecting... 1/5 (unexpected status 503 Service Unavailable: busy, url:"
            " https://operator:proxy-secret@api.example.invalid/v1/responses?key=query-secret,"
            " cf-ray: ray-secret, request id: req-secret)"
        )})
        stdout = _failed(QUOTA, notice)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            error, logged = self._adjudicate(root, _Run(1, stdout, WEBSOCKET_STDERR))
            self.assertEqual((error.reason_code, error.retryable), ("capacity_unavailable", True))
            (path,) = _records(root)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            raw = path.read_text(encoding="ascii")
            record = json.loads(raw)

        private = logged["private_diagnostic"]
        self.assertEqual(private["status"], "retained")
        self.assertEqual(private["file"], path.name)
        self.assertEqual(private["bytes"], len(raw.encode("ascii")))
        logged_text = json.dumps(logged)
        for absent in ("Quota exceeded", "example.invalid", "chatgpt.com", "Reconnecting"):
            self.assertNotIn(absent, logged_text)

        self.assertEqual(record["schema"], FAILURE_DIAGNOSTIC_SCHEMA)
        self.assertEqual(
            {key: record[key] for key in (
                "provider", "provider_id", "group_hash", "returncode", "reason_code",
                "stdout_sha256", "stderr_sha256",
            )},
            {
                "provider": "codex_cli", "provider_id": "luna-primary", "group_hash": _GROUP_HASH,
                "returncode": 1, "reason_code": "capacity_unavailable",
                "stdout_sha256": logged["stdout_sha256"], "stderr_sha256": logged["stderr_sha256"],
            },
        )
        events = record["stdout_events"]
        self.assertEqual([item.get("type") for item in events],
                         ["thread.started", "turn.started", "error", "error", "turn.failed"])
        self.assertEqual([item.get("text") for item in events[3:]], [QUOTA, QUOTA])
        self.assertIn("unexpected status 503 Service Unavailable: busy", events[2]["text"])
        self.assertEqual(record["stderr_lines"], [{"line": 1, "text": WEBSOCKET_STDERR.strip()}])
        for secret in ("THREAD-ID", "proxy-secret", "query-secret", "ray-secret", "req-secret"):
            self.assertNotIn(secret, raw)

        # Unknown, credential-bearing stderr keeps the call closed and its values private.
        credential_lines = (
            "ERROR proxy: Proxy-Authorization: Basic basic-secret; X-API-Key: header-secret\n"
            'ERROR config: OPENAI_API_KEY=env-secret {"api_key": "json-secret", "refresh_token": "rt-secret"}\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            closed, _ = self._adjudicate(Path(tmp), _Run(1, stdout, WEBSOCKET_STDERR + credential_lines))
            (path,) = _records(Path(tmp))
            raw = path.read_text(encoding="ascii")
        self.assertEqual(closed.reason_code, "command_failed")
        for secret in ("basic-secret", "header-secret", "env-secret", "json-secret", "rt-secret"):
            self.assertNotIn(secret, raw)
        self.assertIn("X-API-Key: [redacted]", raw)
        self.assertIn("ERROR config: OPENAI_API_KEY=[redacted]", raw)

        # The backup CLI shares the ordinary record but keeps its exact shape.
        with (
            mock.patch.object(codex_cli, "_run_process", return_value=subprocess.CompletedProcess(
                ["claude"], 1, "", "API Error: 429 Too Many Requests")),
            self.assertLogs("disclosure_anchor.adapters.semantics.claude_cli", level="WARNING") as logs,
            self.assertRaises(SemanticRouteAdjudicatorError),
        ):
            ClaudeCliSemanticAdjudicator(executable=Path("/synthetic/claude")).adjudicate(_batch())
        self.assertEqual(set(_log_record(logs.output)), {
            "event", "provider", "observed_at_unix_ns", "group_hash", "returncode", "reason_code",
            "terminal_http_statuses", "terminal_message_count",
            "stdout_bytes", "stdout_sha256", "stderr_bytes", "stderr_sha256",
        })

    def test_model_output_tool_payloads_and_unknown_events_stay_out_of_evidence(self) -> None:
        decisions = '{"decisions":{"0":{"verdicts":{"forecast_summary":false}}}}'
        agent = _event({"type": "item.completed", "item": {
            "id": "item_1", "type": "agent_message", "text": "MODEL-TEXT " + decisions}})
        tool = _event({"type": "item.started", "item": {
            "id": "item_2", "type": "command_execution", "command": "cat TOOL-ARGUMENT"}})
        unknown = _event({"type": "runtime.notice", "detail": "UNKNOWN-PAYLOAD"})
        router = (
            "2026-10-07T04:49:06.000000Z ERROR codex_core::tools::router: error=unsupported call:"
            ' exec_command {"cmd":"ROUTER-ARGUMENT"}\n'
        )
        reroute = "model rerouted: gpt-5.6-luna -> gpt-5.2 (HighRiskCyberActivity)"
        completed_turn = _event({"type": "turn.completed", "usage": {}})
        cases = (
            # exit 1: the projection never changes the established verdicts
            ("agent message", _Run(1, _failed(QUOTA, agent)), "invalid_runtime_protocol"),
            ("tool item", _Run(1, _failed(QUOTA, tool)), "forbidden_tool_call"),
            ("unknown event", _Run(1, _failed(QUOTA, unknown)), "invalid_runtime_protocol"),
            ("not jsonl", _Run(1, "NOT-JSON-LINE\n" + _failed(QUOTA)), "invalid_runtime_protocol"),
            ("router line", _Run(1, _failed(QUOTA), router), "forbidden_tool_call"),
            # exit 0: a stray error item still fails closed, now with its text kept
            ("reroute on exit 0", _Run(0, "\n".join((
                _event({"type": "thread.started", "thread_id": "THREAD-ID-NOT-EVIDENCE"}),
                _event({"type": "turn.started"}),
                _event({"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": reroute}}),
                agent,
                completed_turn,
            )), result=decisions), "runtime_event_error"),
        )
        for label, run, reason_code in cases:
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                error, logged = self._adjudicate(Path(tmp), run)
                self.assertEqual(error.reason_code, reason_code)
                self.assertEqual(logged["private_diagnostic"]["status"], "retained")
                (path,) = _records(Path(tmp))
                raw = path.read_text(encoding="ascii")
                record = json.loads(raw)
                for secret in (
                    "MODEL-TEXT", "TOOL-ARGUMENT", "UNKNOWN-PAYLOAD", "NOT-JSON-LINE",
                    "ROUTER-ARGUMENT", "verdicts", "THREAD-ID",
                ):
                    self.assertNotIn(secret, raw)
                withheld = [
                    *(item for item in record["stdout_events"] if "withheld" in item),
                    *(item for item in record["stderr_lines"] if "withheld" in item),
                ]
                self.assertTrue(withheld)
                self.assertTrue(all(isinstance(item["bytes"], int) for item in withheld))
                if label == "reroute on exit 0":
                    self.assertEqual(record["returncode"], 0)
                    self.assertIn(reroute, [item.get("text") for item in record["stdout_events"]])

    def test_capture_failure_keeps_the_provider_verdict_and_is_reported(self) -> None:
        unknown_terminal = _failed("fatal protocol parser crashed")

        def unsafe_link(root: Path) -> None:
            (root / "elsewhere").mkdir()
            (root / FAILURE_DIAGNOSTIC_DIRECTORY).symlink_to(root / "elsewhere", target_is_directory=True)

        def plain_file(root: Path) -> None:
            (root / FAILURE_DIAGNOSTIC_DIRECTORY).write_text("not a directory", encoding="utf-8")

        disk_full = mock.patch.object(
            PrivateFailureDiagnostics, "_write", side_effect=OSError(errno.ENOSPC, "No space left on device"),
        )
        partial_write = mock.patch("os.fchmod", side_effect=OSError(errno.EIO, "I/O error"))
        cases = (
            ("symlinked directory", unsafe_link, None),
            ("file in place of the directory", plain_file, None),
            ("disk full", None, disk_full),
            ("failure after create", None, partial_write),
        )
        for label, prepare, fault in cases:
            for stdout, reason_code in ((unknown_terminal, "command_failed"), (_failed(QUOTA), "capacity_unavailable")):
                with self.subTest(label, reason_code=reason_code), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    if prepare is not None:
                        prepare(root)
                    primary = _codex(root)
                    backup = _Adapter(_identity("sonnet-backup", provider="anthropic"))
                    executor = OrderedSemanticAdjudicationExecutor((
                        ConfiguredSemanticProvider(adapter=primary, cache=_Cache()),
                        ConfiguredSemanticProvider(adapter=backup, cache=_Cache()),
                    ))
                    with (
                        mock.patch.object(codex_cli, "_run_process", side_effect=_Run(1, stdout)),
                        self.assertLogs(_CODEX_LOGGER, level="WARNING") as logs,
                    ):
                        if fault is not None:
                            fault.start()
                        try:
                            if reason_code == "command_failed":
                                with self.assertRaises(SemanticRouteAdjudicatorError) as caught:
                                    executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
                                # never converted into an availability verdict
                                self.assertEqual(caught.exception.reason_code, "command_failed")
                                self.assertEqual(backup.calls, 0)
                            else:
                                outcome = executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
                                self.assertEqual(
                                    [(item.outcome, item.reason_code) for item in outcome.attempts],
                                    [("availability_failed", "capacity_unavailable"), ("succeeded", None)],
                                )
                                self.assertEqual(backup.calls, 1)
                        finally:
                            if fault is not None:
                                fault.stop()
                    private = _log_record(logs.output)["private_diagnostic"]
                    self.assertEqual(private["status"], "failed")
                    self.assertRegex(private["error"], r"^[A-Za-z]+$")
                    if label == "symlinked directory":
                        self.assertEqual(list((root / "elsewhere").iterdir()), [])
                    elif label == "failure after create":
                        self.assertEqual(_records(root), [])

    def test_store_bounds_count_and_size_across_concurrent_writers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / FAILURE_DIAGNOSTIC_DIRECTORY
            directory.mkdir(mode=0o755)
            os.chmod(directory, 0o755)
            (directory / "operator-note.txt").write_text("kept", encoding="utf-8")
            outside = Path(tmp) / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            planted = directory / "20000101T000000000000Z-1-0000000000000000.json"
            planted.symlink_to(outside)
            store = PrivateFailureDiagnostics(directory, max_records=5)
            statuses: list[dict[str, object]] = []
            lock = threading.Lock()

            def write(worker: int) -> None:
                for index in range(4):
                    status = store.retain({"worker": worker, "index": index, "pad": "x" * 512})
                    with lock:
                        statuses.append(status)

            threads = [threading.Thread(target=write, args=(worker,)) for worker in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            oversize = store.retain({"pad": "x" * MAX_RECORD_BYTES})
            records = _records(Path(tmp))

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual({status["status"] for status in statuses}, {"retained"})
            self.assertEqual(len({status["file"] for status in statuses}), 32)
            self.assertEqual(oversize, {"status": "failed", "error": "ValueError"})
            # The newest five survive; foreign entries are neither counted, removed nor followed.
            self.assertEqual(len([path for path in records if not path.is_symlink()]), 5)
            self.assertEqual(
                [path.name for path in records if not path.is_symlink()],
                sorted(str(status["file"]) for status in statuses)[-5:],
            )
            self.assertTrue(planted.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "{}")
            self.assertEqual((directory / "operator-note.txt").read_text(encoding="utf-8"), "kept")
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for path in records:
                if not path.is_symlink():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.assertIn("worker", json.loads(path.read_text(encoding="ascii")))

            # A capture that fails at the limit never costs earlier evidence,
            # including on a host without POSIX locks.
            def snapshot() -> dict[str, bytes]:
                return {path.name: path.read_bytes() for path in directory.iterdir() if not path.is_symlink()}

            before = snapshot()
            for fault in (
                mock.patch("os.fchmod", side_effect=OSError(errno.EIO, "I/O error")),
                mock.patch.object(failure_diagnostics, "fcntl", None),
            ):
                with fault:
                    self.assertEqual(store.retain({"cause": "uncaptured"}), {"status": "failed", "error": "OSError"})
                self.assertEqual(snapshot(), before)
            # Another process holding the lock costs a bounded wait, not a hang.
            holder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(holder, fcntl.LOCK_EX)
                with mock.patch.object(failure_diagnostics, "_LOCK_WAIT_SECONDS", 0.05):
                    started = time.monotonic()
                    busy = store.retain({"cause": "contended"})
                    self.assertLess(time.monotonic() - started, 2.0)
            finally:
                os.close(holder)
            self.assertEqual(busy, {"status": "failed", "error": "TimeoutError"})
            self.assertEqual(snapshot(), before)

            # Undeletable records cost one extra slot at most, then captures fail visibly.
            def counted() -> int:
                return len([path for path in _records(Path(tmp)) if not path.is_symlink()])

            with mock.patch("os.unlink", side_effect=PermissionError(errno.EACCES, "denied")):
                over = store.retain({"cause": "over"})
                stuck = [store.retain({"cause": "stuck", "index": index}) for index in range(3)]
                self.assertEqual(counted(), 6)
            self.assertEqual((over["status"], over["prune_error"]), ("retained", "PermissionError"))
            self.assertEqual(stuck, [{"status": "failed", "error": "PermissionError"}] * 3)
            self.assertEqual(store.retain({"cause": "recovered"})["status"], "retained")
            self.assertEqual(counted(), 5)

    def test_pathological_cli_output_stays_within_the_record_bound(self) -> None:
        detail = "\x1b[31m月" + "‮" + "🙂" + "z" * 8000
        notices = [
            _event({"type": "error", "message": f"Reconnecting... {attempt % 5 + 1}/5 ({detail})"})
            for attempt in range(300)
        ]
        terminal = "stream disconnected before completion: idle timeout waiting for SSE"
        huge = "fatal: " + "\x00\x07" + "细节" * 300_000
        stdout = _failed(huge, *notices) + "\n" + _event({"type": "error", "message": terminal})
        stderr = "".join(f"ERROR line {index} " + "y" * 6000 + "\n" for index in range(100))
        with tempfile.TemporaryDirectory() as tmp:
            error, logged = self._adjudicate(Path(tmp), _Run(1, stdout, stderr))
            (path,) = _records(Path(tmp))
            size = path.stat().st_size
            record = json.loads(path.read_text(encoding="ascii"))

        self.assertEqual(error.reason_code, "command_failed")
        self.assertEqual(logged["private_diagnostic"]["status"], "retained")
        self.assertLessEqual(size, MAX_RECORD_BYTES)
        self.assertGreater(record["stdout_events_omitted"], 0)
        self.assertGreater(record["stderr_lines_omitted"], 0)
        last = record["stdout_events"][-1]
        self.assertEqual(last["text"], terminal)
        failed_turn = record["stdout_events"][-2]
        self.assertTrue(failed_turn["text"].startswith("fatal: \x00\x07细节"))
        self.assertTrue(failed_turn["text_truncated"])
        self.assertEqual(failed_turn["text_chars"], len(huge))


if __name__ == "__main__":
    unittest.main()
