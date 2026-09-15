"""Thin CLI real verifier tests, with only environment/external IO injected."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from disclosure_anchor.cli import m6_qualify_readonly as cli
from disclosure_anchor.adapters.runtime.m6_qualification_verifier import M6PrivateQualificationFacts
from disclosure_anchor.application.services.semantic_router import SemanticRouter
from tests._m6_qualification_fixture import Fixture, canonical, sha
from tests.unit.test_m6_qualification_verifier import plan


class QualificationCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = Fixture(self.root / "data", quality=True)
        self.output = self.root / "output"
        raw = canonical(self.fixture.public)
        (self.root / "public.json").write_bytes(raw)
        (self.root / "inputs.json").write_bytes(canonical(dict(
            contract_version=cli.PUBLIC_INPUTS_CONTRACT_VERSION,
            receipts={self.fixture.admission.attempt_id: dict(path="public.json", sha256=sha(raw),
                       verifier_identity="independent-public-reader")})))
        (self.root / "plan.json").write_bytes(plan().canonical_bytes())

    def run_cli(self, *, public=True, failure=None, attempt_id=None):
        f = self.fixture
        argv = ["--attempt-id", attempt_id or f.admission.attempt_id, "--output-dir", str(self.output),
                "--verifier-identity", "independent-qualification", "--plan", str(self.root / "plan.json")]
        if public:
            argv += ["--public-receipts", str(self.root / "inputs.json")]
        engine = MagicMock()
        with ExitStack() as stack:
            for name, result in (("load_settings", SimpleNamespace(disclosure_semantic_batch_size=16)),
                                 ("FileStorePathBuilder", f.paths), ("ProviderDocumentFileSource", f.source),
                                 ("load_semantic_route_taxonomy", f.taxonomy), ("app_database_url", "unused-synthetic-url"),
                                 ("create_db_engine", engine),
                                 ("read_private_qualification_facts", f.facts(M6PrivateQualificationFacts))):
                stack.enter_context(patch.object(cli, name, return_value=result))
            stack.enter_context(patch.object(SemanticRouter, "route", side_effect=AssertionError("CLI cannot route")))
            if failure is not None:
                stack.enter_context(patch.object(f.source, "rebuild_provider_document", side_effect=failure))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            code = cli.main(argv)
        engine.dispose.assert_called_once()
        return code, json.loads((self.output / "run-summary.json").read_bytes())

    def test_exit_zero_means_evidence_written_even_when_review_pending(self):
        code, summary = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(summary["attempts"][0]["status"], "evidence")
        self.assertEqual(summary["attempts"][0]["verdict"], "review_pending")
        self.assertFalse(summary["new_owner_events"])
        with self.assertRaises(FileExistsError):
            self.run_cli()

    def test_missing_public_input_is_exit_two_without_evidence(self):
        code, summary = self.run_cli(public=False)
        self.assertEqual(code, 2)
        self.assertEqual(summary["attempts"][0]["status"], "unavailable")
        self.assertFalse((self.output / self.fixture.admission.attempt_id / "qualification-evidence.json").exists())

    def test_unexpected_runtime_error_is_exit_one_with_visible_error_summary(self):
        code, summary = self.run_cli(failure=RuntimeError("source-read-failed"))
        self.assertEqual(code, 1)
        self.assertEqual(summary["attempts"][0]["error_type"], "RuntimeError")
        self.assertIn("source-read-failed", summary["attempts"][0]["error"])

    def test_new_sink_does_not_overwrite_and_loader_keeps_expected_pin(self):
        path = self.root / "immutable.json"
        cli._write_new(path, b"original")
        with self.assertRaises(FileExistsError):
            cli._write_new(path, b"changed")
        self.assertEqual(path.read_bytes(), b"original")
        inputs = cli.read_public_inputs(self.root / "inputs.json")
        expected = inputs[self.fixture.admission.attempt_id]["sha256"]
        (self.root / "public.json").write_bytes(b"different bytes")
        provided = cli.public_receipt_loader(inputs, self.root)(self.fixture.admission)
        self.assertEqual(provided.receipt_sha256, expected)
        self.assertNotEqual(sha(provided.receipt), expected)

    def test_cli_rejects_attempt_path_traversal_before_writing_outside_output(self):
        escaped = self.root / "escaped"
        try:
            code, _ = self.run_cli(attempt_id="../escaped")
        except (ValueError, SystemExit):
            pass
        else:
            self.assertFalse(escaped.exists(), "attempt parameter escaped the selected output directory")
            self.assertNotEqual(code, 0, "unsafe path component accepted as a successful CLI attempt")
        self.assertFalse(escaped.exists(), "attempt parameter escaped the selected output directory")


if __name__ == "__main__":
    unittest.main()
