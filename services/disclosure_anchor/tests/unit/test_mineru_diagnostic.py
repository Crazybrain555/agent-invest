"""Real protocol/ZIP/reader composition with an offline synthetic provider."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import canonical_result_owner_v2
from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import PinnedArtifactTree
from disclosure_anchor.adapters.runtime.mineru_diagnostic import (
    run_diagnostic_pdf, validate_diagnostic_disposal,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.domain.errors import ParserOutputContractError
from tests._mineru_diagnostic_fixture import diagnostic_disposal_fixture
from tests.unit.test_mineru_medium_artifacts import _write_bundle


RUNTIME = "sha256:" + "b" * 64


class _Provider:
    def __init__(self, root: Path, *, failure: str = "") -> None:
        self.root = root
        self.failure = failure
        self.calls: list[tuple[str, str]] = []
        self.source = root / "input.pdf"
        self.source.write_bytes(b"synthetic source bytes; page count supplied by tested caller")
        self.source_sha = "sha256:" + hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.journal = root / "diagnostic"
        bundle = root / "fixture"
        bundle.mkdir()
        _write_bundle(bundle)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for item in sorted(bundle.rglob("*")):
                if item.is_file():
                    relative = item.relative_to(bundle).as_posix().replace(
                        "sha256_" + "a" * 64, self.source_sha.replace(":", "_"),
                    )
                    archive.writestr(relative, item.read_bytes())
            if failure == "unsafe_zip":
                archive.writestr("../escaped", b"must never extract")
        self.archive = output.getvalue()
        self.archive_sha = hashlib.sha256(self.archive).hexdigest()
        self.owner = canonical_result_owner_v2(
            task_id="task-1", artifact_sha256=self.archive_sha, artifact_byte_count=len(self.archive),
        )
        self.fields: dict[str, str] = {}
        self.acked = False

    def response(self, status: int, value: object) -> httpx.Response:
        return httpx.Response(status, stream=httpx.ByteStream(json.dumps(value).encode()))

    def payload(self, status: str) -> dict[str, object]:
        return {
            "task_id": "task-1", "status": status, "protocol_state": status,
            "status_url": "http://mineru.invalid/tasks/task-1",
            "result_url": "http://mineru.invalid/tasks/task-1/result",
            "task_protocol_schema": "mineru-task-protocol.v2",
            "idempotency_key": self.fields["agent_idempotency_key"],
            "attempt_identity": self.fields["agent_attempt_identity"],
            "fence_identity": ("wrong-fence" if self.failure == "identity" else self.fields["agent_fence_identity"]),
            "result_artifact_schema": "mineru-retained-result.v1" if status == "completed" else None,
            "result_artifact_sha256": self.archive_sha if status == "completed" else None,
            "result_artifact_bytes": len(self.archive) if status == "completed" else None,
            "result_artifact_owner": self.owner if status == "completed" else None,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/tasks":
            assert (self.journal / "01-intent.json").is_file()
            body = request.read()
            for name in ("agent_idempotency_key", "agent_attempt_identity", "agent_fence_identity"):
                match = re.search(rb'name="' + name.encode() + rb'"\r\n\r\n([^\r]+)', body)
                assert match is not None, name
                self.fields[name] = match.group(1).decode()
            expected_name = self.source_sha.replace(":", "_") + ".pdf"
            assert ('filename="' + expected_name + '"').encode() in body
            if self.failure == "submit_loss":
                raise httpx.ReadError("synthetic submit response loss")
            payload = self.payload("pending")
            payload["message"] = "Task submitted successfully"
            if self.failure == "submit_shape":
                payload["unknown"] = True
            return self.response(202, payload)
        if path == "/tasks/task-1":
            return self.response(200, self.payload("failed" if self.failure == "remote_failure" else "completed"))
        if path.endswith("/lease"):
            return self.response(200, {"schema": "mineru-task-protocol.v2", "task_id": "task-1",
                                       "lease_until_unix": 1 if self.failure == "expired_lease" else 1_000_000})
        if path.endswith("/result"):
            data = self.archive + (b"corrupt" if self.failure == "zip_hash" else b"")
            return httpx.Response(200, stream=httpx.ByteStream(data), headers={
                "x-mineru-result-sha256": self.archive_sha, "x-mineru-result-owner": self.owner,
            })
        if path.endswith("/ack"):
            assert (self.journal / "04-validated.json").is_file()
            assert (self.journal / "05-disposal-intent.json").is_file()
            assert not (self.journal / "resources").exists()
            self.acked = True
            if self.failure == "ack_loss":
                raise httpx.ReadError("synthetic ACK response loss")
            return self.response(200, {"schema": "mineru-task-protocol.v2", "task_id": "task-1",
                                       "status": "wrong" if self.failure == "ack_identity" else "consumed"})
        if path.startswith("/tasks/by-idempotency/"):
            if not self.acked:
                if self.failure == "reconcile_absence":
                    return self.response(404, {"detail": "Task not found"})
                return self.response(200, self.payload("completed"))
            return self.response(404, {"detail": "wrong" if self.failure == "absence" else "Task not found"})
        raise AssertionError(path)

    def run(self, *, pages: int = 2, reconcile: bool = False) -> tuple[dict[str, object], dict[str, object]]:
        return run_diagnostic_pdf(
            input_pdf=self.source, source_pdf_sha256=self.source_sha, source_page_count=pages,
            api_url="http://mineru.invalid", server_url="http://vlm.invalid/v1",
            options=ParserOptions(runtime_bundle_identity_sha256=RUNTIME, timeout_seconds=60),
            journal_root=self.journal, transport=httpx.MockTransport(self.handle),
            unix_time=lambda: 1000.0, pause=lambda _: None,
            reconcile_submitted=reconcile,
        )


class MinerUDiagnosticTests(unittest.TestCase):
    def test_real_wire_zip_reader_closure_before_owned_cleanup_and_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _Provider(Path(tmp))
            evidence, disposal = provider.run()
            self.assertEqual(evidence["page_count"], 2)
            self.assertEqual(evidence["block_count"], 3)
            self.assertTrue(provider.acked)
            self.assertTrue(provider.source.is_file())
            self.assertFalse((provider.journal / "resources").exists())
            self.assertTrue((provider.journal / "06-disposed.json").is_file())
            validate_diagnostic_disposal(
                disposal, source_pdf_sha256=provider.source_sha, runtime_identity=RUNTIME,
                source_page_count=2, provider_bundle_sha256=str(evidence["provider_bundle_sha256"]),
            )
            self.assertEqual(provider.calls.count(("POST", "/tasks")), 1)
            for record in provider.journal.iterdir():
                self.assertEqual(record.stat().st_mode & 0o777, 0o600)

    def test_pre_disposal_failures_preserve_resources_and_never_ack(self) -> None:
        for failure in ("identity", "remote_failure", "expired_lease", "zip_hash", "unsafe_zip"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                provider = _Provider(Path(tmp), failure=failure)
                with self.assertRaises((ValueError, RuntimeError, ParserOutputContractError)):
                    provider.run()
                self.assertFalse(provider.acked)
                self.assertTrue((provider.journal / "resources").is_dir())
                self.assertFalse((Path(tmp) / "escaped").exists())

    def test_page_mismatch_never_creates_disposal_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _Provider(Path(tmp))
            with self.assertRaisesRegex(ValueError, "page/profile"):
                provider.run(pages=3)
            self.assertFalse(provider.acked)
            self.assertFalse((provider.journal / "05-disposal-intent.json").exists())

    def test_submit_response_loss_keeps_original_key_without_second_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _Provider(Path(tmp), failure="submit_loss")
            with self.assertRaises(httpx.ReadError):
                provider.run()
            before = (provider.journal / "01-intent.json").read_bytes()
            with self.assertRaises(FileExistsError):
                provider.run()
            self.assertEqual((provider.journal / "01-intent.json").read_bytes(), before)
            self.assertEqual(provider.calls.count(("POST", "/tasks")), 1)
            self.assertFalse(provider.acked)

    def test_local_cleanup_failure_prevents_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _Provider(Path(tmp))
            with patch.object(PinnedArtifactTree, "remove_exact_admitted_contents",
                       side_effect=OSError("synthetic cleanup failure")):
                with self.assertRaises(OSError):
                    provider.run()
            self.assertFalse(provider.acked)
            self.assertTrue((provider.journal / "resources").is_dir())

    def test_explicit_same_intent_recovery_never_posts_again(self) -> None:
        for failure in ("submit_loss", "submit_shape"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                provider = _Provider(Path(tmp), failure=failure)
                with self.assertRaises((httpx.ReadError, ValueError)):
                    provider.run()
                before = (provider.journal / "01-intent.json").read_bytes()
                if failure == "submit_shape":
                    wire = json.loads((provider.journal / "02-submit-response.json").read_bytes())
                    self.assertTrue(json.loads(bytes.fromhex(wire["response_hex"]))["unknown"])
                provider.failure = ""
                evidence, _ = provider.run(reconcile=True)
                self.assertEqual(evidence["page_count"], 2)
                self.assertTrue(provider.acked)
                self.assertEqual(provider.calls.count(("POST", "/tasks")), 1)
                self.assertEqual((provider.journal / "01-intent.json").read_bytes(), before)
                with self.assertRaises((ValueError, FileNotFoundError)):
                    provider.run(reconcile=True)

    def test_recovery_drift_or_absence_keeps_owner_without_post_or_ack(self) -> None:
        for failure in ("identity", "reconcile_absence", "snapshot", "resource", "pages", "intent"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                provider = _Provider(Path(tmp), failure="submit_loss")
                with self.assertRaises(httpx.ReadError):
                    provider.run()
                provider.failure = failure
                if failure == "snapshot":
                    (provider.journal / "resources/source.pdf").write_bytes(b"changed")
                elif failure == "resource":
                    resources = provider.journal / "resources"
                    resources.rename(provider.journal / "retained-original")
                    resources.mkdir()
                    (resources / "source.pdf").write_bytes(provider.source.read_bytes())
                elif failure == "intent":
                    intent = provider.journal / "01-intent.json"
                    value = json.loads(intent.read_bytes())
                    value["prepared_identity"]["client_submit_key"] = "1." + "a" * 64
                    intent.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    provider.run(pages=3 if failure == "pages" else 2, reconcile=True)
                self.assertFalse(provider.acked)
                self.assertEqual(provider.calls.count(("POST", "/tasks")), 1)
                self.assertTrue((provider.journal / "resources").is_dir())

    def test_cleanup_root_replacement_keeps_both_trees_and_prevents_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _Provider(Path(tmp))
            original = PinnedArtifactTree.remove_exact_admitted_contents

            def replace_before_delete(tree: PinnedArtifactTree, **kwargs: object) -> None:
                resources = provider.journal / "resources"
                resources.rename(provider.journal / "retained-original")
                resources.mkdir()
                (resources / "unrelated").write_bytes(b"must survive")
                original(tree, **kwargs)

            with patch.object(PinnedArtifactTree, "remove_exact_admitted_contents", replace_before_delete):
                with self.assertRaises((ValueError, ParserOutputContractError)):
                    provider.run()
            self.assertFalse(provider.acked)
            self.assertTrue((provider.journal / "retained-original" / "source.pdf").is_file())
            self.assertEqual((provider.journal / "resources" / "unrelated").read_bytes(), b"must survive")
            self.assertFalse((provider.journal / "05-disposal-intent.json").exists())

    def test_ack_ambiguity_or_wrong_absence_never_yields_pass(self) -> None:
        for failure in ("ack_loss", "ack_identity", "absence"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                provider = _Provider(Path(tmp), failure=failure)
                with self.assertRaises((ValueError, httpx.ReadError)):
                    provider.run()
                self.assertTrue((provider.journal / "05-disposal-intent.json").is_file())
                self.assertFalse((provider.journal / "06-disposed.json").exists())
                self.assertTrue(provider.source.is_file())

    def test_disposal_proof_rejects_cross_source_and_forged_publication(self) -> None:
        source, bundle = "sha256:" + "c" * 64, "sha256:" + "d" * 64
        valid = diagnostic_disposal_fixture(source=source, runtime=RUNTIME, pages=2, bundle=bundle)
        for key, wrong in (
            ("authority", "finish_committed"), ("source_pdf_sha256", "sha256:" + "e" * 64),
            ("task_id", "other-task"), ("local_resources_removed", 1),
            ("provider_page_count", 3), ("terminal_artifact_bytes", True),
            ("idempotency_key", "1." + "0" * 64),
        ):
            with self.subTest(key=key):
                value = deepcopy(valid)
                value[key] = wrong
                with self.assertRaises(ValueError):
                    validate_diagnostic_disposal(value, source_pdf_sha256=source,
                        runtime_identity=RUNTIME, source_page_count=2, provider_bundle_sha256=bundle)
