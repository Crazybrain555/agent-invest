"""Independent supervisor CLI composition: actual spool, isolated DB/SSH boundaries."""

from contextlib import ExitStack, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime.m6_e2e_assembly import M6LifecycleSpool
from disclosure_anchor.application.contracts.m6_run_events import M6DocumentQualified
from disclosure_anchor.cli import m6_verifier_supervisor as cli
from tests import m6_support as m6


class VerifierCliIndependentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = m6.make_fixture("e2e_publication", {"a": (7, "fresh")})
        self.spec = self.fixture.spec
        self.run = mock.Mock(pins={})
        self.run.require_spec.return_value = self.spec
        self.run.epoch.return_value = m6.RUNNER_EPOCH
        self.spool = M6LifecycleSpool(
            self.root / "runner", run_id=self.spec.run_id, spec_sha256=self.spec.canonical_sha256(),
            producer_epoch_sha256=m6.RUNNER_EPOCH, max_facts=8,
        )
        self.addCleanup(self.spool.close)
        self.path = Path(self.spool.status()["path"])
        journal = m6.Journal(self.fixture)
        self.admission = journal.admission(self.fixture.entries["a"], "attempt-a")
        self.committed = journal.commit(self.admission, journal.at(10), ledger_seq=1)
        self.public_event = journal.confirm(self.admission, journal.at(11), ledger_seq=1)
        evidence = m6.qualification_for(self.fixture.entries["a"], "e2e_publication", "attempt-a")
        self.quality_event = M6DocumentQualified(attempt_id="attempt-a",
                                               qualification_evidence_sha256=evidence.canonical_sha256())
        self.plan = self.root / "plan.json"
        self.plan.write_bytes(self.fixture.plan.canonical_bytes())
        self.receipt = self.root / "runner-receipt.json"
        self.public, self.quality = mock.Mock(), mock.Mock()
        for assembly in (self.public, self.quality):
            assembly.failed = False
            assembly.complete.return_value = {"status": "complete", "spool": {"pending": 0}}
            assembly.finish.return_value = {"status": "complete"}
            assembly.abort.return_value = {"status": "failed"}
        self.public.is_drain_role, self.quality.is_drain_role = True, False
        self.counter = 0

    def add_published_attempt(self):
        self.spool.record_event(self.admission, attempt_id="attempt-a")
        self.spool.record_event(self.committed, attempt_id="attempt-a")
        self.spool.mark_delivered(1, 10)
        self.spool.mark_delivered(2, 11)

    def receipt_value(self):
        return {
            "contract_version": "staged-v4-campaign.v1", "campaign_id": self.spec.campaign_id,
            "manifest_sha256": self.spec.manifest_sha256, "scope_sha256": self.spec.scope_sha256,
            "m6_assembly": {
                "status": "complete", "run_id": self.spec.run_id, "spec_sha256": self.spec.canonical_sha256(),
                "anchor_sha256": self.run.anchor.canonical_sha256.return_value,
                "producer_kind": "e2e_runner", "producer_epoch_sha256": m6.RUNNER_EPOCH,
                "run_directory_pins": {}, "spool": self.spool.status(),
                "worker_error": None, "closure": {"complete": True},
            },
        }

    def write_receipt(self, value=None):
        self.run.anchor.canonical_sha256.return_value = m6.digest("anchor")
        self.receipt.write_text(json.dumps(self.receipt_value() if value is None else value))

    def invoke(self, *, read_receipt=None, precreate_parents=False, fail_manifest=False):
        self.counter += 1
        out = self.root / f"supervisor-{self.counter}"
        observed_error = None
        code = None

        def start():
            if precreate_parents:
                for name in ("public", "quality"):
                    (out / name).mkdir(exist_ok=True)

        self.public.start.side_effect = start

        def confirm(**kwargs):
            # Match the shared sink's actual mkdir contract: the driver owns
            # phase parents, the verifier owns only its attempt directory.
            kwargs["attempt_dir"].mkdir(exist_ok=True)
            raw = b'{"synthetic":"public-consumer-audit"}'
            (kwargs["attempt_dir"] / "public-consumer-audit.json").write_bytes(raw)
            return SimpleNamespace(receipt=raw, receipt_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                                   confirmation=self.public_event)

        def qualify(**kwargs):
            kwargs["attempt_dir"].mkdir(exist_ok=True)
            held = kwargs["public_receipt_for"](self.admission)
            self.assertEqual(held.receipt, b'{"synthetic":"public-consumer-audit"}')
            return SimpleNamespace(qualified=self.quality_event)

        with ExitStack() as stack:
            values = {
                "load_m6_run_directory": self.run, "M6VerifierAssembly": None,
                "load_settings": SimpleNamespace(disclosure_semantic_batch_size=16),
                "FileStorePathBuilder": mock.Mock(), "ProviderDocumentFileSource": mock.Mock(),
                "load_semantic_route_taxonomy": mock.Mock(), "app_database_url": "fixture",
                "reader_database_url": "fixture", "create_db_engine": mock.Mock(),
            }
            for name, value in values.items():
                patch = mock.patch.object(cli, name, side_effect=[self.public, self.quality]) if name == "M6VerifierAssembly" \
                    else mock.patch.object(cli, name, return_value=value)
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(cli, "confirm_attempt", side_effect=confirm))
            stack.enter_context(mock.patch.object(cli, "qualify_attempt", side_effect=qualify))
            stack.enter_context(redirect_stderr(io.StringIO()))
            if read_receipt is not None:
                stack.enter_context(mock.patch.object(cli, "read_runner_receipt", side_effect=read_receipt))
            if fail_manifest:
                original_write = cli._write_new

                def write(path, payload):
                    if path.name == "public-inputs.json":
                        raise OSError("manifest disk failed")
                    return original_write(path, payload)

                stack.enter_context(mock.patch.object(cli, "_write_new", side_effect=write))
            try:
                code = cli.main([
                    "--m6-run-dir", str(self.root), "--runner-spool", str(self.path),
                    "--runner-receipt", str(self.receipt), "--output-dir", str(out),
                    "--verifier-identity", "independent", "--plan", str(self.plan),
                    "--deadline-seconds", "2", "--poll-seconds", "0.001",
                ])
            except Exception as exc:  # noqa: BLE001 - assert failure cleanup across either CLI error convention
                observed_error = exc
        summary_path = out / "run-summary.json"
        summary = json.loads(summary_path.read_bytes()) if summary_path.exists() else {}
        return code, observed_error, summary

    def test_complete_attempt_writes_both_phases_before_single_terminal_drain(self):
        self.add_published_attempt()
        self.write_receipt()
        code, error, summary = self.invoke()
        self.assertIsNone(error)
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["status"], "complete")
        self.public.record.assert_called_once()
        self.quality.record.assert_called_once()
        self.public.finish.assert_called_once()
        self.quality.finish.assert_not_called()

    def test_foreign_receipt_cannot_close_current_run(self):
        self.write_receipt()
        original = self.receipt.read_bytes()
        for scope, field, replacement in (
            ("m6_assembly", "run_id", "different-run"),
            ("m6_assembly", "spec_sha256", m6.digest("different-spec")),
            ("m6_assembly", "producer_epoch_sha256", m6.digest("different-epoch")),
            (None, "campaign_id", "different-campaign"),
            (None, "manifest_sha256", m6.digest("different-manifest")),
        ):
            value = json.loads(original)
            (value if scope is None else value[scope])[field] = replacement
            self.write_receipt(value)
            self.public.reset_mock()
            self.quality.reset_mock()
            with self.subTest(field=field):
                code, _error, summary = self.invoke()
                self.assertNotEqual(code, 0, summary)
                self.public.finish.assert_not_called()
                self.public.abort.assert_called()
                self.quality.abort.assert_called()
        # A valid receipt cannot stand in for missing lifecycle evidence.
        self.write_receipt(json.loads(original))
        self.path.unlink()
        self.public.reset_mock()
        self.quality.reset_mock()
        code, _error, summary = self.invoke()
        self.assertNotEqual(code, 0, summary)
        self.public.finish.assert_not_called()
        self.public.abort.assert_called()
        self.quality.abort.assert_called()

    def test_primary_read_error_closes_both_even_if_first_abort_fails(self):
        for secondary in (False, True):
            self.public.reset_mock()
            self.quality.reset_mock()
            self.public.abort.side_effect = RuntimeError("secondary cleanup") if secondary else None
            with self.subTest(secondary=secondary):
                code, error, summary = self.invoke(read_receipt=ValueError("primary receipt corrupt"))
                self.assertNotEqual(code, 0)
                self.public.abort.assert_called()
                self.quality.abort.assert_called()
                self.public.finish.assert_not_called()
                self.assertIn("primary receipt corrupt", str(error) + json.dumps(summary))

    def test_last_bytes_written_before_receipt_are_consumed_before_final_validation(self):
        self.add_published_attempt()
        self.spool.close()
        raw = self.path.read_bytes()
        self.path.write_bytes(raw[:-5])
        self.write_receipt()
        original = cli.read_runner_receipt

        def closed_receipt(*args, **kwargs):
            with self.path.open("ab") as stream:
                stream.write(raw[-5:])
            return original(*args, **kwargs)

        code, error, summary = self.invoke(read_receipt=closed_receipt, precreate_parents=True)
        self.assertIsNone(error)
        self.assertEqual(code, 0, summary)
        self.public.finish.assert_called_once()

    def test_required_receipt_write_failure_does_not_claim_clean_completion(self):
        self.add_published_attempt()
        self.write_receipt()
        code, error, summary = self.invoke(fail_manifest=True)
        self.assertNotEqual(code, 0, summary)
        self.assertIn("manifest disk failed", str(error) + json.dumps(summary))
        self.public.finish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
