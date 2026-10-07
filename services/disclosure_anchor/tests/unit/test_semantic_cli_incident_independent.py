"""Independent incident boundaries; transport is recorded, no provider is invoked.

Capacity literals are the pinned rust-v0.156.1 protocol/error.rs formatters,
also checked against its installed binary. These are general regression cases,
not an assertion about the lost terminal text of the production incident.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import builtins
import importlib.util
import json
import hashlib
from dataclasses import replace
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from disclosure_anchor.adapters.semantics.codex_cli import CodexCliSemanticAdjudicator
from disclosure_anchor.adapters.semantics.failure_diagnostics import (
    FAILURE_DIAGNOSTIC_DIRECTORY,
    MAX_RECORD_BYTES,
    MAX_TEXT_BYTES,
    PrivateFailureDiagnostics,
    redact_diagnostic_text,
)
from disclosure_anchor.adapters.semantics import failure_diagnostics
from disclosure_anchor.application.ports.semantic_routes import SemanticRouteAdjudicatorError
from disclosure_anchor.application.services.semantic_adjudication import (
    ConfiguredSemanticProvider,
    OrderedSemanticAdjudicationExecutor,
)
from tests.unit._codex_model_catalog_fixture import neutral_catalog
from tests.unit.test_semantic_adjudication import _Adapter, _Cache, _GROUP_HASH, _batch, _identity


_QUOTA = "Quota exceeded. Check your plan and billing details."
_ADMIN = "You’ve hit your usage limit. To get more access now, send a request to your admin"
_MODEL = "You’ve hit your usage limit for GPT-6-Luna. Switch to another model now,"
_CAPACITY = (
    _QUOTA,
    "Your workspace is out of credits. Add credits to continue.",
    "Your workspace is out of credits. Ask your workspace owner to refill in order to continue.",
    "You hit your spend cap set in your workspace. Increase your spend cap to continue.",
    "You hit your spend cap set by the owner of your workspace. Ask an owner to increase your spend cap to continue.",
    _ADMIN + " or try again later.",
    _MODEL + " or try again later.",
)


def _wire(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


def _failed(message: str, *siblings: dict[str, object]) -> str:
    return _wire(
        {"type": "thread.started", "thread_id": "independent-thread"},
        {"type": "turn.started"},
        *siblings,
        {"type": "error", "message": message},
        {"type": "turn.failed", "error": {"message": message}},
    )


class SemanticCliIncidentIndependentTests(unittest.TestCase):
    def _chain(self, stdout: str, stderr: str = "") -> tuple[object, object, int, int, _Cache, _Cache]:
        with tempfile.TemporaryDirectory() as raw_tmp:
            primary = CodexCliSemanticAdjudicator(
                executable=Path("/recorded-not-executable/codex"),
                runtime_tmp_root=Path(raw_tmp),
                model="gpt-6-luna", model_catalog=neutral_catalog("gpt-6-luna"),
            )
            backup = _Adapter(replace(
                _identity("sonnet-backup", provider="anthropic"),
                canonical_model="claude-sonnet-5",
            ))
            primary_cache, backup_cache = _Cache(), _Cache()
            executor = OrderedSemanticAdjudicationExecutor((
                ConfiguredSemanticProvider(adapter=primary, cache=primary_cache),
                ConfiguredSemanticProvider(adapter=backup, cache=backup_cache),
            ))
            outcome = error = None
            with self.assertLogs("disclosure_anchor.adapters.semantics.codex_cli", level="WARNING"), mock.patch(
                "disclosure_anchor.adapters.semantics.codex_cli._run_process",
                return_value=subprocess.CompletedProcess([], 1, stdout, stderr),
            ) as transport:
                try:
                    outcome = executor.adjudicate(_batch(), group_hash=_GROUP_HASH)
                except SemanticRouteAdjudicatorError as caught:
                    error = caught
            return outcome, error, transport.call_count, backup.calls, primary_cache, backup_cache

    def _assert_backup(self, message: str) -> None:
        outcome, error, primary_calls, backup_calls, primary_cache, backup_cache = self._chain(_failed(message))
        self.assertIsNone(error)
        self.assertIsNotNone(outcome)
        self.assertEqual((primary_calls, backup_calls), (1, 1))
        self.assertEqual(outcome.actual_result_identity.provider_id, "sonnet-backup")
        self.assertEqual(outcome.actual_result_identity.canonical_model, "claude-sonnet-5")
        self.assertEqual(outcome.actual_result_attempt, 2)
        self.assertEqual(
            [(item.ordinal, item.outcome, item.reason_code) for item in outcome.attempts],
            [(1, "availability_failed", "capacity_unavailable"), (2, "succeeded", None)],
        )
        self.assertFalse(primary_cache.entries)
        self.assertEqual(len(backup_cache.entries), 1)

    def _assert_closed(self, stdout: str, reason: str = "command_failed", stderr: str = "") -> None:
        outcome, error, primary_calls, backup_calls, primary_cache, backup_cache = self._chain(stdout, stderr)
        self.assertIsNone(outcome)
        self.assertIsInstance(error, SemanticRouteAdjudicatorError)
        self.assertEqual(error.reason_code, reason)
        self.assertFalse(error.retryable)
        self.assertEqual((primary_calls, backup_calls), (1, 0))
        self.assertEqual([(item.outcome, item.reason_code) for item in error.attempts], [("failed_closed", reason)])
        self.assertFalse(primary_cache.entries)
        self.assertFalse(backup_cache.entries)

    def test_versioned_capacity_formatters_reach_backup_with_actual_lineage(self) -> None:
        for message in _CAPACITY:
            with self.subTest(message=message):
                self._assert_backup(message)

    def test_formatter_reset_suffixes_keep_capacity_identity(self) -> None:
        # Same-day and other-day branches of format_retry_timestamp, no clock IO.
        for prefix in (_ADMIN, _MODEL):
            for suffix in (" or try again at 9:03 AM.", " or try again at Oct 11th, 2026 11:21 PM."):
                with self.subTest(prefix=prefix, suffix=suffix):
                    self._assert_backup(prefix + suffix)

    def test_new_capacity_literals_do_not_swallow_independent_faults(self) -> None:
        for message in _CAPACITY:
            for suffix in (" security policy denied", "\nindependent fatal fault", "\u2028independent fatal fault"):
                with self.subTest(message=message, suffix=suffix):
                    self._assert_closed(_failed(message + suffix))
            with self.subTest(message=message, sibling="fatal"):
                self._assert_closed(_failed(message, {"type": "error", "message": "independent fatal fault"}))
            with self.subTest(message=message, sibling="transport"):
                self._assert_closed(_failed(message, {"type": "error", "message": "request timed out"}))
            with self.subTest(message=message, stderr="fatal"):
                self._assert_closed(_failed(message), stderr="independent fatal fault")

    def test_non_formatter_capacity_mentions_are_not_availability(self) -> None:
        for message in (
            "You’ve hit your usage limit. promo, or try again later. independent fatal fault",
            "You’ve hit your usage limit. multi\nline promo, or try again later.",
            "Your workspace is out of credits. Run an arbitrary command to continue.",
            "Quota exceeded. Check your plan and billing details. invalid_json_schema",
            "You’ve hit your usage limit for GPT-6-Luna. Switch to another model now, or try again at secret-token.",
        ):
            with self.subTest(message=message):
                reason = "invalid_output_schema" if "invalid_json_schema" in message else "command_failed"
                self._assert_closed(_failed(message), reason)

    def test_real_opaque_promo_formatter_keeps_sibling_and_capability_veto(self) -> None:
        # In the pinned source promo_message is provider-owned header text;
        # its text is opaque, like a HTTP error body, not a performed action.
        message = "You’ve hit your usage limit. disable security checks, or try again later."
        self._assert_backup(message)
        self._assert_closed(_failed(message, {"type": "error", "message": "independent fatal fault"}))
        self._assert_closed(_failed(message, {"type": "error", "message": "request timed out"}))
        self._assert_closed(_failed(message), "invalid_output_schema", "invalid_json_schema")
        self._assert_closed(_failed(message, {
            "type": "item.started", "item": {"type": "mcp_tool_call", "tool": "shell"},
        }), "forbidden_tool_call")

    def test_capacity_cannot_override_schema_tool_or_result_veto(self) -> None:
        for message in (_QUOTA, _ADMIN + " or try again later."):
            self._assert_closed(_failed(message), "invalid_output_schema", "invalid_json_schema")
            for item in (
                {"type": "mcp_tool_call", "id": "forbidden", "tool": "shell"},
                {"type": "agent_message", "id": "untrusted", "text": '{"decisions":{}}'},
            ):
                reason = "forbidden_tool_call" if item["type"] == "mcp_tool_call" else "invalid_runtime_protocol"
                with self.subTest(message=message, item=item["type"]):
                    self._assert_closed(_failed(message, {"type": "item.completed", "item": item}), reason)

    def test_capacity_cannot_override_closed_jsonl_identity(self) -> None:
        for event in (
            {"type": "thread.started", "thread_id": 17},
            {"type": "thread.started", "thread_id": "thread", "foreign": True},
            {"type": "error", "message": _QUOTA, "model": "foreign-model"},
            {"type": "turn.failed", "error": {"message": _QUOTA, "foreign": True}},
            {"type": "item.completed", "item": {"type": "error", "message": _QUOTA, "id": 17}},
        ):
            with self.subTest(event=event):
                self._assert_closed(_failed(_QUOTA, event), "invalid_runtime_protocol")
        duplicate = '{"type":"error","message":"fatal","message":' + json.dumps(_QUOTA) + '}'
        self._assert_closed(_failed(_QUOTA) + "\n" + duplicate, "invalid_runtime_protocol")


    def _captured_adapter_call(
        self, root: Path, stdout: str, stderr: str = "", *, success: bool = False,
    ) -> tuple[SemanticRouteAdjudicatorError | None, list[str]]:
        primary = CodexCliSemanticAdjudicator(
            executable=Path("/recorded-not-executable/codex"), runtime_tmp_root=root,
            model="gpt-6-luna", model_catalog=neutral_catalog("gpt-6-luna"),
        )
        output = '{"decisions":{"0":{"verdicts":{"forecast_summary":false}}}}'

        def recorded(*, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if success:
                Path(args[args.index("--output-last-message") + 1]).write_text(output, encoding="utf-8")
            return subprocess.CompletedProcess(args, 0 if success else 1, stdout, stderr)

        error = None
        with mock.patch("disclosure_anchor.adapters.semantics.codex_cli._run_process", side_effect=recorded), mock.patch(
            "disclosure_anchor.adapters.semantics.codex_cli._LOGGER.warning",
        ) as warning:
            try:
                batch = _batch()
                primary.adjudicate_with_result(replace(batch, document=replace(
                    batch.document, title="private_business_prompt_fixture_765",
                )))
            except SemanticRouteAdjudicatorError as caught:
                error = caught
        logs = [str(call.args[0]) % tuple(call.args[1:]) for call in warning.call_args_list]
        return error, logs

    def _read_capture(self, root: Path, logs: list[str]) -> tuple[dict[str, object], bytes]:
        self.assertEqual(len(logs), 1)
        ordinary = json.loads(logs[0].split("semantic_provider_failure ", 1)[1])
        reference = ordinary["private_diagnostic"]
        self.assertEqual(reference["status"], "retained")
        path = root / FAILURE_DIAGNOSTIC_DIRECTORY / reference["file"]
        data = path.read_bytes()
        self.assertEqual(len(data), reference["bytes"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), reference["sha256"])
        self.assertEqual((stat.S_IMODE(path.stat().st_mode), path.stat().st_nlink), (0o600, 1))
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertLessEqual(len(data), MAX_RECORD_BYTES)
        return json.loads(data), data

    def test_actual_adapter_capture_preserves_unknown_reason_and_redacts_credentials(self) -> None:
        secret = "fixture_credential_value_abc123"
        for text in (
            "unknown terminal failure: Authorization: Bearer " + secret,
            "unknown terminal failure: X-API-Key: " + secret,
            "unknown terminal failure: api_key=" + secret,
            "unknown terminal failure: https://fixture-user:" + secret + "@example.invalid/v1?token=" + secret,
        ):
            with self.subTest(text=text.split(secret)[0]), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp)
                error, logs = self._captured_adapter_call(root, _failed(text))
                self.assertEqual(error.reason_code, "command_failed")
                record, data = self._read_capture(root, logs)
                self.assertEqual(record["reason_code"], "command_failed")
                self.assertNotIn(secret, data.decode())
                self.assertIn("unknown terminal failure", data.decode())
                self.assertNotIn("private_business_prompt_fixture_765", data.decode())
                self.assertNotIn(secret, "\n".join(logs))
                self.assertNotIn("unknown terminal failure", "\n".join(logs))
                self.assertNotIn("example.invalid", "\n".join(logs))

    def test_capture_failure_logs_status_and_does_not_replace_original_provider_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            with mock.patch(
                "disclosure_anchor.adapters.semantics.failure_diagnostics._create",
                side_effect=OSError("fixture_private_error_not_for_log"),
            ):
                error, logs = self._captured_adapter_call(root, _failed("unknown terminal failure"))
            self.assertEqual(error.reason_code, "command_failed")
            ordinary = json.loads(logs[0].split("semantic_provider_failure ", 1)[1])
            self.assertEqual(ordinary["private_diagnostic"]["status"], "failed")
            self.assertEqual(ordinary["private_diagnostic"]["error"], "OSError")
            self.assertNotIn("fixture_private_error_not_for_log", "\n".join(logs))
            self.assertFalse(list((root / FAILURE_DIAGNOSTIC_DIRECTORY).glob("*.json")))

    def test_success_does_not_leave_failure_evidence_or_diagnostic_log(self) -> None:
        output = '{"decisions":{"0":{"verdicts":{"forecast_summary":false}}}}'
        wire = _wire(
            {"type": "thread.started", "thread_id": "independent-thread"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "id": "result", "text": output}},
            {"type": "turn.completed", "usage": {}},
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            error, logs = self._captured_adapter_call(root, wire, success=True)
            self.assertIsNone(error)
            self.assertFalse(logs)
            self.assertFalse((root / FAILURE_DIAGNOSTIC_DIRECTORY).exists())
            self.assertFalse(list(root.glob("semantic-route-*")))

    def test_malformed_model_and_tool_payloads_are_withheld_not_saved_as_diagnostics(self) -> None:
        secret = "private_model_or_tool_payload_fixture_871"
        for item in (
            {"type": "agent_message", "id": "result", "text": secret},
            {"type": "mcp_tool_call", "id": "tool", "arguments": secret},
        ):
            with self.subTest(type=item["type"]), tempfile.TemporaryDirectory() as raw_tmp:
                root = Path(raw_tmp)
                error, logs = self._captured_adapter_call(root, _failed(_QUOTA, {
                    "type": "item.completed", "item": item,
                }))
                reason = "invalid_runtime_protocol" if item["type"] == "agent_message" else "forbidden_tool_call"
                self.assertEqual(error.reason_code, reason)
                record, data = self._read_capture(root, logs)
                self.assertEqual(record["reason_code"], reason)
                self.assertNotIn(secret, data.decode())
                self.assertNotIn(secret, "\n".join(logs))

    def test_oversized_multibyte_diagnostics_keep_terminal_cause_with_finite_bounds(self) -> None:
        notices = tuple({"type": "error", "message": "Reconnecting... 1/5 (" + "界\\" * 5000 + ")"} for _ in range(80))
        terminal = "unknown terminal cause sentinel " + "界\\" * 5000
        stderr = "\n".join("stderr filler " + "界\\" * 5000 for _ in range(40))
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            error, logs = self._captured_adapter_call(root, _failed(terminal, *notices), stderr)
            self.assertEqual(error.reason_code, "command_failed")
            record, data = self._read_capture(root, logs)
            self.assertIn("unknown terminal cause sentinel", data.decode())
            self.assertLessEqual(len(record["stdout_events"]), 64)
            self.assertLessEqual(len(record["stderr_lines"]), 32)
            self.assertGreater(record["stdout_events_omitted"], 0)
            self.assertGreater(record["stderr_lines_omitted"], 0)
            entries = [*record["stdout_events"], *record["stderr_lines"]]
            self.assertTrue(any(entry.get("text_truncated") for entry in entries))
            for entry in entries:
                if "text" in entry:
                    self.assertLessEqual(len(json.dumps(entry["text"], ensure_ascii=True).encode()), MAX_TEXT_BYTES)

    def test_private_text_redacts_credential_fields_without_losing_error_category(self) -> None:
        secret = "fixture_credential_value_abc123"
        for fragment in (
            "Authorization: Bearer " + secret,
            "Authorization: Basic " + secret,
            "X-API-Key: " + secret,
            "api_key=" + secret,
            '"api_key": "' + secret + '"',
            "OPENAI_API_KEY=" + secret,
            "https://fixture-user:" + secret + "@example.invalid/v1?token=" + secret,
        ):
            with self.subTest(fragment=fragment.split(secret)[0]):
                text = redact_diagnostic_text("Connection failed: " + fragment)
                self.assertNotIn(secret, text)
                self.assertIn("Connection failed", text)

    def test_bounded_store_concurrent_capture_is_private_and_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            directory = Path(raw_tmp) / "evidence"

            def capture(index: int) -> dict[str, object]:
                return PrivateFailureDiagnostics(directory, max_records=4).retain({"ordinal": index})

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(capture, range(24)))
            self.assertTrue(all(result["status"] == "retained" for result in results), results)
            self.assertEqual(len({result["file"] for result in results}), 24)
            files = list(directory.glob("*.json"))
            self.assertEqual(len(files), 4)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for path in files:
                info = path.stat()
                self.assertEqual((stat.S_IMODE(info.st_mode), info.st_nlink, info.st_uid), (0o600, 1, os.geteuid()))
                self.assertIn(json.loads(path.read_bytes())["ordinal"], range(24))

    def test_bounded_store_real_processes_share_the_count_limit(self) -> None:
        code = (
            "import json,sys; from pathlib import Path; "
            "from disclosure_anchor.adapters.semantics.failure_diagnostics import PrivateFailureDiagnostics; "
            "s=PrivateFailureDiagnostics(Path(sys.argv[1]),max_records=4); "
            "print(json.dumps([s.retain({'writer':sys.argv[2],'ordinal':i}) for i in range(8)]))"
        )
        source_root = Path(failure_diagnostics.__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as raw_tmp:
            directory = Path(raw_tmp) / "evidence"

            def capture(writer: int) -> list[dict[str, object]]:
                completed = subprocess.run(
                    [sys.executable, "-B", "-c", code, str(directory), str(writer)],
                    env={"PYTHONPATH": str(source_root)}, capture_output=True, text=True,
                    check=True, timeout=10,
                )
                self.assertEqual(completed.stderr, "")
                return json.loads(completed.stdout)

            with ThreadPoolExecutor(max_workers=4) as pool:
                results = [record for group in pool.map(capture, range(4)) for record in group]
            self.assertEqual(len(results), 32)
            self.assertTrue(all(result["status"] == "retained" for result in results), results)
            self.assertEqual(len({result["file"] for result in results}), 32)
            self.assertEqual(len(list(directory.glob("*.json"))), 4)

    def test_refused_symlink_store_does_not_touch_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            target = root / "foreign"
            target.mkdir(mode=0o755)
            marker = target / "foreign.json"
            marker.write_bytes(b"exact foreign bytes")
            link = root / "evidence"
            link.symlink_to(target, target_is_directory=True)
            result = PrivateFailureDiagnostics(link).retain({"reason_code": "command_failed"})
            self.assertEqual(result["status"], "failed")
            self.assertEqual(marker.read_bytes(), b"exact foreign bytes")
            self.assertEqual(list(target.iterdir()), [marker])
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_failed_capture_at_retention_limit_preserves_existing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            directory = Path(raw_tmp) / "evidence"
            store = PrivateFailureDiagnostics(directory, max_records=2)
            self.assertEqual(store.retain({"reason_code": "command_failed", "cause": "first"})["status"], "retained")
            self.assertEqual(store.retain({"reason_code": "capacity_unavailable", "cause": "second"})["status"], "retained")
            before = {path.name: path.read_bytes() for path in directory.iterdir()}
            with mock.patch(
                "disclosure_anchor.adapters.semantics.failure_diagnostics._create",
                side_effect=OSError("fixture disk full"),
            ):
                result = store.retain({"reason_code": "command_failed", "cause": "uncaptured"})
            self.assertEqual(result["status"], "failed")
            self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, before)

    def test_persistent_prune_failure_cannot_grow_past_one_overflow_slot(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            directory = Path(raw_tmp) / "evidence"
            store = PrivateFailureDiagnostics(directory, max_records=2, max_record_bytes=1024)
            for ordinal in range(2):
                self.assertEqual(store.retain({"ordinal": ordinal, "text": "x" * 900})["status"], "retained")
            before = {path.name: path.read_bytes() for path in directory.glob("*.json")}
            counts, byte_counts, statuses = [], [], []
            with mock.patch(
                "disclosure_anchor.adapters.semantics.failure_diagnostics._prune",
                side_effect=OSError("fixture persistent unlink refusal"),
            ):
                for ordinal in range(2, 10):
                    statuses.append(store.retain({"ordinal": ordinal, "text": "x" * 900}))
                    files = list(directory.glob("*.json"))
                    counts.append(len(files))
                    byte_counts.append(sum(path.stat().st_size for path in files))
            for name, data in before.items():
                self.assertEqual((directory / name).read_bytes(), data)
            detail = f"counts={counts}, bytes={byte_counts}, statuses={statuses}"
            self.assertLessEqual(max(counts), 3, detail)
            self.assertLessEqual(max(byte_counts), 3 * 1024, detail)

    def test_optional_posix_lock_failure_does_not_break_semantic_adapter_import(self) -> None:
        # Exercise the actual module with the optional OS facility unavailable;
        # this is an import boundary, not a claim that Windows was tested.
        actual_import = builtins.__import__

        def without_fcntl(name: str, *args: object, **kwargs: object) -> object:
            if name == "fcntl":
                raise ImportError("fixture platform has no fcntl")
            return actual_import(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location("independent_lock_import", failure_diagnostics.__file__)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        loaded = importlib.util.module_from_spec(spec)
        with mock.patch("builtins.__import__", side_effect=without_fcntl):
            spec.loader.exec_module(loaded)
        with tempfile.TemporaryDirectory() as raw_tmp:
            status = loaded.PrivateFailureDiagnostics(Path(raw_tmp) / "evidence").retain({"cause": "original"})
            self.assertEqual(status["status"], "failed")


if __name__ == "__main__":
    unittest.main()
