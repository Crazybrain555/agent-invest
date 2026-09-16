"""Independent run binding and durable-receipt boundaries; no live DB or SSH."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from disclosure_anchor.adapters.runtime.m6_e2e_assembly import M6LifecycleSpool
from disclosure_anchor.adapters.runtime.m6_e2e_run import (
    M6RunDirectory, M6RunnerClosureFailed, build_runner_closure_receipts, close_verifier_assembly,
    execute_runner_closure,
)
from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig
from disclosure_anchor.application.contracts.m6_campaign import M6CampaignScope
from disclosure_anchor.application.ports.staged_lifecycle_facts import AttemptAdmittedFact, AttemptFinalFact
from disclosure_anchor.application.services.staged_campaign_runner import CampaignInputError, CampaignRunRequest
from disclosure_anchor.application.services.staged_parse_coordinator import (
    CoordinatorResult, CoordinatorTerminal, ResourceCreditVector,
)
from disclosure_anchor.cli import m6_public_verify, m6_run_control, staged_campaign
from tests import m6_owner_support as owner
from tests import m6_support as m6


class _RuntimeReached(RuntimeError):
    pass


class RunWiringIndependentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = m6.make_fixture("e2e_publication", {"a": (7, "fresh")})

    def run_directory(self, spec=None):
        spec = spec or self.fixture.spec
        return M6RunDirectory(
            path=self.root, anchor=owner.anchor_for(spec), spec=spec, roles={},
            ssh=ResidentSSHConfig(address="127.0.0.1", port=22, username="fixture",
                                  private_key_path="/fixture/key", known_hosts_path="/fixture/known_hosts"),
            remote_port=4444, lease=owner.policy(), pins={},
        )

    def test_receipt_writers_preserve_exact_bytes_when_os_write_is_short(self):
        original = os.write
        payload = ('{"message":"完整回执", "count":123}' * 20).encode()

        def short_write(fd, data):
            return original(fd, data[:11])

        for module in (m6_public_verify, m6_run_control):
            with self.subTest(module=module.__name__):
                path = self.root / (module.__name__ + ".json")
                with mock.patch.object(os, "write", side_effect=short_write):
                    module._write_new(path, payload)
                self.assertEqual(path.read_bytes(), payload, "success requires the entire durable receipt")
                with self.subTest(failure="zero-progress"), mock.patch.object(os, "write", return_value=0) as write:
                    with self.assertRaises(OSError):
                        module._write_new(path.with_suffix(".zero"), payload)
                    self.assertLess(write.call_count, 32, "zero progress must not spin indefinitely")
                with self.subTest(failure="durability"), mock.patch.object(os, "fsync", side_effect=OSError("disk failed")):
                    with self.assertRaisesRegex(OSError, "disk failed"):
                        module._write_new(path.with_suffix(".unflushed"), payload)

    def test_campaign_receipt_appears_whole_and_never_replaces_prior_result(self):
        original_write, original_sync = os.write, os.fsync
        value = {"receipt": "完整结果" * 100, "status": "complete"}
        path = self.root / "campaign.json"
        checked = []

        def short_write(fd, data):
            self.assertFalse(path.exists(), "the completion name must not expose partial bytes")
            checked.append(len(data))
            return original_write(fd, data[:13])

        def durable_data(fd):
            if not checked:
                raise AssertionError("write did not reach exact-byte path")
            return original_sync(fd)

        with mock.patch.object(os, "write", side_effect=short_write), \
                mock.patch.object(os, "fsync", side_effect=durable_data):
            staged_campaign._write_new(path, value)
        self.assertGreater(len(checked), 1)
        import json
        self.assertEqual(json.loads(path.read_bytes()), value)
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            staged_campaign._write_new(path, {"replacement": "forbidden"})
        self.assertEqual(path.read_bytes(), before)
        failed = self.root / "failed-campaign.json"
        with mock.patch.object(os, "fsync", side_effect=OSError("receipt durability failed")):
            with self.assertRaisesRegex(OSError, "receipt durability failed"):
                staged_campaign._write_new(failed, value)
        self.assertFalse(failed.exists(), "a failed data fsync must leave no completion signal")

    def test_campaign_rejects_cross_run_inputs_before_runtime_or_db(self):
        request = CampaignRunRequest(
            manifest=self.fixture.manifest, scope=M6CampaignScope.from_manifest(self.fixture.manifest), max_seconds=60,
        )
        settings = SimpleNamespace(worker_parse_execution_mode="staged-v4")
        # The positive proves this test reaches the real pre-runtime boundary;
        # negative cases must be rejected before any profile, lock, DB or work.
        with mock.patch.object(staged_campaign, "_load_staged_process_profile", side_effect=_RuntimeReached):
            with self.assertRaises(_RuntimeReached):
                staged_campaign.run_campaign(settings, request=request, external_stop=lambda: False,
                                             m6_run=self.run_directory(), m6_spool_dir=self.root / "spool")
            for field, value in (("manifest_sha256", m6.digest("other-corpus")),
                                 ("scope_sha256", m6.digest("other-scope")),
                                 ("campaign_id", "other-campaign")):
                changed = self.fixture.spec.model_copy(update={field: value})
                with self.subTest(field=field), self.assertRaises(CampaignInputError):
                    staged_campaign.run_campaign(settings, request=request, external_stop=lambda: False,
                                                 m6_run=self.run_directory(changed), m6_spool_dir=self.root / "spool")

    def test_public_attempt_paths_and_duplicates_rejected_before_io(self):
        for index, ids in enumerate((("../escape",), ("/absolute",), ("a\\b",), (".",), ("same", "same"))):
            out = self.root / f"output-{index}"
            argv = ["--output-dir", str(out), "--verifier-identity", "independent"]
            for attempt_id in ids:
                argv += ["--attempt-id", attempt_id]
            with self.subTest(ids=ids), mock.patch.object(m6_public_verify, "load_settings", side_effect=_RuntimeReached):
                with self.assertRaises(ValueError):
                    m6_public_verify.main(argv)
                self.assertFalse(out.exists(), "invalid output identities must be rejected before creating files")

    def test_verifier_drain_hash_resolves_to_exact_persisted_receipt(self):
        out = self.root / "public"
        out.mkdir()
        assembly = mock.Mock()
        assembly.is_drain_role = True
        observed = []

        def finish(digest):
            # A success/drain observation cannot precede the durable bytes it
            # claims as its evidence, even when this verification found errors.
            observed.append((digest, [p.read_bytes() for p in out.rglob("*.json")]))
            return {"status": "complete", "drain_receipt_sha256": digest}

        assembly.finish.side_effect = finish
        result = close_verifier_assembly(
            assembly, output_dir=out,
            summary={"started_utc": "fixture-start", "finished_utc": "fixture-end", "attempts": []},
            producer_kind="public_verifier", verifier_identity="independent", exit_code=1, drained=True,
            write=m6_public_verify._write_new, terminal=True,
        )
        self.assertEqual(result, 1)
        self.assertEqual(len(observed), 1)
        digest, persisted_before_drain = observed[0]
        self.assertIn(digest, {"sha256:" + hashlib.sha256(raw).hexdigest() for raw in persisted_before_drain},
                      "verifier_drained must name a durable exact-byte receipt, not an unpersisted intermediate dict")

    def test_receipt_failure_closes_verifier_without_claiming_drain(self):
        assembly = mock.Mock()
        assembly.is_drain_role = True
        real_write = m6_public_verify._write_new

        def write(path, payload):
            if path.name == "drain-receipt.json":
                raise OSError("drain disk failed")
            return real_write(path, payload)

        with self.assertRaisesRegex(OSError, "drain disk failed"):
            close_verifier_assembly(
                assembly, output_dir=self.root,
                summary={"started_utc": "fixture-start", "finished_utc": "fixture-end", "attempts": []},
                producer_kind="public_verifier", verifier_identity="independent", exit_code=0, drained=True,
                write=write, terminal=True,
            )
        assembly.finish.assert_not_called()
        assembly.abort.assert_called_once()

    def test_public_cli_completes_evidence_without_premature_terminal_drain(self):
        assembly = mock.Mock()
        assembly.complete.return_value = {"status": "complete", "terminal_drain": False}
        out = self.root / "preparatory"
        with (
            mock.patch.object(m6_public_verify, "load_settings", return_value=mock.Mock()),
            mock.patch.object(m6_public_verify, "FileStorePathBuilder"),
            mock.patch.object(m6_public_verify, "load_m6_run_directory", return_value=self.run_directory()),
            mock.patch.object(m6_public_verify, "M6VerifierAssembly", return_value=assembly),
            mock.patch.object(m6_public_verify, "diagnostic_continuous_clock"),
            mock.patch.object(m6_public_verify, "app_database_url", return_value="fixture"),
            mock.patch.object(m6_public_verify, "reader_database_url", return_value="fixture"),
            mock.patch.object(m6_public_verify, "create_db_engine", return_value=mock.Mock()),
            mock.patch.object(m6_public_verify, "read_private_qualification_facts", side_effect=RuntimeError("retained failure")),
            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()),
        ):
            result = m6_public_verify.main([
                "--attempt-id", "attempt-a", "--output-dir", str(out),
                "--verifier-identity", "independent", "--m6-run-dir", str(self.root),
            ])
        self.assertEqual(result, 1, "the per-attempt failure must remain visible")
        assembly.complete.assert_called_once()
        assembly.finish.assert_not_called()
        self.assertFalse((out / "drain-receipt.json").exists())

    def closure(self, *, missing_final=False):
        spec = self.fixture.spec
        spool = M6LifecycleSpool(self.root / ("unfinished" if missing_final else "finished"),
                                 run_id=spec.run_id, spec_sha256=spec.canonical_sha256(),
                                 producer_epoch_sha256=m6.RUNNER_EPOCH, max_facts=6)
        self.addCleanup(spool.close)
        entry = self.fixture.entries["a"]
        fact = AttemptAdmittedFact("a", "fence-a", entry.document_id, "processing-a", entry.source_pdf_sha256,
                                   entry.source_byte_count, 7, spec.runtime.process_profile_sha256)
        spool.attempt_admitted(fact)
        spool.attempt_admitted(replace(fact, attempt_id="b", fence_identity="fence-b", processing_run_id="processing-b"))
        spool.attempt_final(AttemptFinalFact("a", "failed", "not_submitted", None, None, m6.digest("cleanup-a")))
        if not missing_final:
            spool.attempt_final(AttemptFinalFact("b", "failed", "not_submitted", None, None, m6.digest("cleanup-b")))
        for item in spool.pending():
            spool.mark_delivered(item.producer_sequence, item.producer_sequence + 10)
        result = CoordinatorResult(CoordinatorTerminal.QUIESCENT, True, 2, 1 if missing_final else 2,
                                   (("a", "pre_submission_failed"),), (), ResourceCreditVector())
        return build_runner_closure_receipts(
            run_id=spec.run_id, spec_sha256=spec.canonical_sha256(), runner_epoch_sha256=m6.RUNNER_EPOCH,
            owner_identity="fixture-worker", spool=spool, result=result, scratch_residual_count=0, children_exited=True,
        )

    def test_closure_uses_durable_history_not_bounded_recent_final_sample(self):
        clean = self.closure()
        self.assertEqual(clean.ownership_closure.admitted_attempt_count, 2)
        self.assertEqual(clean.ownership_closure.final_attempt_count, 2)
        self.assertEqual(clean.ownership_closure.residual_count, 0)
        self.assertIsNone(clean.unresolved_claims)
        unfinished = self.closure(missing_final=True)
        self.assertEqual(unfinished.ownership_closure.residual_count, 1)
        self.assertEqual(unfinished.unresolved_claims.attempts[0].attempt_id, "b")
        self.assertEqual(unfinished.unresolved_claims.attempts[0].state, "unknown")
        self.assertEqual(unfinished.admission_reconciliation.unresolved_receipt_sha256,
                         unfinished.unresolved_claims.canonical_sha256())

    def test_closure_transport_failures_never_send_later_success_steps(self):
        receipts = self.closure()
        steps = ["stop", "deposit_resource_audit", "deposit_admission_reconciliation", "admission_closed",
                 "deposit_ownership_closure"]
        kinds = ["stop", "deposit", "deposit", "admission_closed", "deposit"]
        for failure_index in (*range(len(steps)), None):
            with self.subTest(failure_index=failure_index):
                remote = owner.ScriptedOwner(self.fixture.spec, owner.anchor_for(self.fixture.spec), owner.ManualClock())
                for index in range(len(steps)):
                    if index == failure_index:
                        remote.raise_on_exchange(TimeoutError("reply lost; remote outcome unknown"))
                        break
                    remote.answer(remote.status(observed=self.fixture.at(10), state="stopping"))
                client = owner.client_for(remote)
                try:
                    if failure_index is None:
                        result = execute_runner_closure(client, receipts, request_stop=True)
                        self.assertTrue(result["complete"])
                        self.assertEqual([item["step"] for item in result["steps"]], steps)
                    else:
                        with self.assertRaises(M6RunnerClosureFailed) as caught:
                            execute_runner_closure(client, receipts, request_stop=True)
                        self.assertEqual(caught.exception.step, steps[failure_index])
                        self.assertIn("remote outcome unknown", caught.exception.reason)
                finally:
                    client.close()
                count = len(steps) if failure_index is None else failure_index + 1
                self.assertEqual([request.command.kind for request in remote.requests], kinds[:count])
                self.assertEqual(remote.close_calls, 1)

    def test_bind_lost_reply_can_reconcile_only_the_same_frozen_spec(self):
        spec = self.fixture.spec
        args = SimpleNamespace(run_dir=self.root, **{
            name: getattr(spec, name) for name in (
                "campaign_id", "mode", "phase", "start_condition", "manifest_sha256", "scope_sha256",
                "quality_plan_sha256", "carry_in_attempt_ids",
            )
        }, **{
            name: getattr(spec.runtime, name) for name in (
                "source_commit", "source_manifest_sha256", "runtime_bundle_identity_sha256", "process_profile_sha256",
                "worker_profile_sha256", "deployment_qualification_sha256",
            )
        })
        remote = owner.ScriptedOwner(spec, owner.anchor_for(spec), owner.ManualClock())
        remote.raise_on_exchange(TimeoutError("bind reply lost"))
        remote.answer(remote.status(observed=self.fixture.at(1), state="bound"))

        def load(_path, **_kwargs):
            return replace(self.run_directory(), spec=spec if (self.root / "run-spec.json").exists() else None)

        with (
            mock.patch.object(m6_run_control, "load_m6_run_directory", side_effect=load),
            mock.patch.object(m6_run_control, "_client", side_effect=lambda _path: owner.client_for(
                remote, role="controller", epoch=m6.digest("controller"))),
            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(TimeoutError, "bind reply lost"):
                m6_run_control.bind(args)
            frozen = (self.root / "run-spec.json").read_bytes()
            self.assertEqual(frozen, spec.canonical_bytes())
            self.assertEqual(m6_run_control.bind(args), 0)
            args.manifest_sha256 = m6.digest("different-manifest")
            self.assertEqual(m6_run_control.bind(args), 2)
        self.assertEqual((self.root / "run-spec.json").read_bytes(), frozen)
        self.assertEqual(len(remote.requests), 2, "changed binding must never reach transport")
        self.assertEqual(remote.requests[0].command.canonical_bytes(), remote.requests[1].command.canonical_bytes())
        self.assertEqual(remote.close_calls, 2)


if __name__ == "__main__":
    unittest.main()
