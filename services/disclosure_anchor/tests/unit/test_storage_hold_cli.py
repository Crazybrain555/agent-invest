"""The operator storage-hold decision, end to end against the real native registry.

The native route is served in memory by the actual operator gate and registry
methods, so the Mac command presents the credential the native side enrolled and
verifies the digests it computes. No DB, network or runtime; the credentials are
synthetic.
"""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import httpx

from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import encode_remote_parse_evidence_v4
from disclosure_anchor.application.contracts.staged_credit import DatabaseLeaseSnapshot
from disclosure_anchor.cli import storage_hold
from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol
from tests.unit import test_staged_coordinator_backend_v4 as durable
from tests.unit.test_mineru_result_storage import PLENTY, UPLOAD, StorageLab

RUNTIME = "sha256:" + "c" * 64
ORIGIN = "http://mineru.invalid"
CREDENTIAL = "synthetic-operator-credential-" + "A" * 20
OTHER_CREDENTIAL = "synthetic-operator-credential-" + "B" * 20


def _private(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text)
    os.chmod(path, mode)
    return path


def _lease(until: datetime, remaining_seconds: int) -> DatabaseLeaseSnapshot:
    """The database's own view of the claim's lease, observed ``remaining_seconds`` before its end."""

    return DatabaseLeaseSnapshot(
        database_observed_at_utc=until - timedelta(seconds=remaining_seconds),
        lease_until_utc=until,
        remaining_microseconds=remaining_seconds * 1_000_000,
    )


def _submitted_authority(*, live: bool = False):  # type: ignore[no-untyped-def]
    authority = durable._authority("submitted")
    accepted = next(item.value for item in authority.evidence if item.kind == "accepted_submission")
    canonical = replace(
        accepted, status_url=f"{ORIGIN}/tasks/{accepted.remote_task_identity}",
        result_url=f"{ORIGIN}/tasks/{accepted.remote_task_identity}/result",
    )
    evidence = tuple(
        encode_remote_parse_evidence_v4(canonical) if item.kind == "accepted_submission" else item
        for item in authority.evidence
    )
    assert authority.claim_lease_until is not None
    return replace(
        authority, evidence=evidence, database_lease=_lease(authority.claim_lease_until, 30 if live else -1),
    )


class StorageHoldCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = _submitted_authority()
        lab = StorageLab(self)
        self.registry = lab.open()
        key, task_id = self.authority.client_submit_key, "task-1"
        self.registry.reconcile_or_create(
            idempotency_key=key, task_id=task_id,
            attempt_identity=self.authority.attempt_id, fence_identity=self.authority.fence_identity,
        )
        self.registry.bind_task_payload(key, lab.lab.make_task_tree(task_id, upload=UPLOAD))
        self.registry.reserve_source_growth(
            key, live_free_bytes=PLENTY, outstanding_promise_bytes=0, completion_head_waiting=False,
        )
        self.registry.transition(key, "processing")
        self.registry.block_storage(key, reason="codec_bound_exceeded")
        self.key = key
        self.posts: list[dict[str, object]] = []
        self.refused: list[int] = []
        # Simulate an answer lost on the wire: "before" nothing reaches the
        # native side; "after" the native side applies it, then the answer is lost.
        self.lose: str | None = None
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.out = Path(directory.name)
        self.token_file = _private(self.out / "operator.token", CREDENTIAL + "\n")
        self.operator_path = self.out / "container-run" / "storage-hold-operator.json"
        protocol.enroll_storage_hold_operator(
            storage_hold.operator_verifier(CREDENTIAL), path=self.operator_path,
        )

    def status(self) -> httpx.Response:
        """The ordinary task status route: identity, state and the durable failure cause."""

        record = self.registry.get(self.key)
        assert record is not None
        payload: dict[str, object] = {
            "task_id": record.task_id, "status": "failed" if record.state == "failed" else "processing",
            "idempotency_key": record.idempotency_key, "attempt_identity": record.attempt_identity,
            "fence_identity": record.fence_identity, "protocol_state": record.state,
        }
        if record.failure_cause is not None:
            payload["failure_cause"] = record.failure_cause
        return httpx.Response(200, json=payload)

    def native(self, request: httpx.Request) -> httpx.Response:
        """The generated route's semantics: the operator gate, then the real registry methods."""

        if request.method == "GET":
            self.assertEqual(request.url.path, "/tasks/task-1")
            return self.status()
        self.assertEqual(request.url.path, "/agent/storage-holds/task-1")
        if self.lose == "before":
            raise httpx.ConnectError("synthetic connection loss before the request arrived")
        try:
            protocol.require_storage_hold_operator(
                request.headers.get("authorization"), path=self.operator_path,
            )
        except protocol.StorageHoldOperatorRefused as exc:
            self.refused.append(exc.status)
            return httpx.Response(exc.status, json={"detail": {"code": exc.code}})
        body = json.loads(request.content)
        self.posts.append(body)
        try:
            if body["mode"] == "preview":
                answer = self.registry.storage_hold_preview(self.key, runtime_identity_sha256=RUNTIME)
            else:
                answer = self.registry.decide_storage_hold(
                    self.key, runtime_identity_sha256=RUNTIME,
                    **{name: body[name] for name in (
                        "expected_preview_sha256", "decided_by", "reason", "fixed_by",
                    )},
                )
        except protocol.TaskProtocolConflict as exc:
            return httpx.Response(409, json={"detail": str(exc)})
        if self.lose == "after":
            raise httpx.ReadTimeout("synthetic answer lost after the native side applied it")
        return httpx.Response(200, json=answer)

    def run_command(self, *argv: str, authority=None, handler=None, token_file=None,  # type: ignore[no-untyped-def]
                    token: bool = True) -> int:
        @contextmanager
        def uow():  # type: ignore[no-untyped-def]
            yield SimpleNamespace(remote_parse_v4=SimpleNamespace(load=lambda _id: authority or self.authority))

        credential = ["--operator-token-file", str(token_file or self.token_file)] if token else []
        with httpx.Client(transport=httpx.MockTransport(handler or self.native)) as client:
            return storage_hold.run([*argv, *credential], uow_factory=uow, client=client)

    def execute(self, preview_sha: str, out: str, **changes: str) -> int:
        decision = {"--decided-by": "root", "--reason": "declared codec bound stays", "--fixed-by": "none"}
        decision.update(changes)
        args = ["execute", "--attempt-id", "attempt-1", "--expect-preview-sha256", preview_sha,
                "--out", str(self.out / out)]
        for flag, value in decision.items():
            args += [flag, value]
        return self.run_command(*args)

    def test_reviewed_decision_is_applied_once_and_replays_to_the_same_receipt(self) -> None:
        self.assertEqual(self.run_command("preview", "--attempt-id", "attempt-1", "--out",
                                          str(self.out / "preview.json")), 0)
        preview = json.loads((self.out / "preview.json").read_text())
        native = preview["native"]
        self.assertEqual(
            (preview["attempt_id"], preview["source_pdf_sha256"], native["hold_reason"], native["task_id"]),
            ("attempt-1", self.authority.source_pdf_sha256, "codec_bound_exceeded", "task-1"),
        )
        self.assertEqual(self.execute("sha256:" + "0" * 64, "stale.json"), 1)  # Not the reviewed hold.
        self.assertFalse((self.out / "stale.json").exists())
        self.assertEqual(self.execute(native["preview_sha256"], "decision.json"), 0)
        receipt = (self.out / "decision.json").read_bytes()
        record = self.registry.get(self.key)
        assert record is not None and record.failure_cause is not None
        self.assertEqual(
            (record.state, record.failure_cause["decision_sha256"]),
            ("failed", json.loads(receipt)["decision_sha256"]),
        )
        self.assertEqual(json.loads(receipt)["decision_sha256"], storage_hold.decision_sha256(
            preview_sha256=native["preview_sha256"], decided_by="root",
            reason="declared codec bound stays", fixed_by="none",
        ))
        # A lost response replays to exactly the same receipt file.
        self.assertEqual(self.execute(native["preview_sha256"], "decision.json"), 0)
        self.assertEqual((self.out / "decision.json").read_bytes(), receipt)
        self.assertEqual(self.execute(native["preview_sha256"], "other.json", **{"--reason": "changed"}), 1)
        self.assertEqual([post["mode"] for post in self.posts],
                         ["preview", "execute", "execute", "execute", "execute"])

    def test_a_live_claim_another_state_or_another_task_is_refused_before_any_decision(self) -> None:
        for label, authority in (
            ("live claim", _submitted_authority(live=True)),
            ("not submitted", durable._authority("remote_terminal")),
        ):
            with self.subTest(label):
                self.assertEqual(self.run_command("preview", "--attempt-id", "attempt-1", authority=authority), 1)
        self.assertEqual(self.posts, [])

        def foreign(request: httpx.Request) -> httpx.Response:
            preview = self.registry.storage_hold_preview(self.key, runtime_identity_sha256=RUNTIME)
            return httpx.Response(200, json={**preview, "attempt_identity": "attempt-other"})

        self.assertEqual(self.run_command("preview", "--attempt-id", "attempt-1", handler=foreign), 1)
        self.assertEqual(self.registry.get(self.key).state, "processing")

    def test_only_the_enrolled_owner_only_credential_reaches_the_native_registry(self) -> None:
        verifier_out = io.StringIO()
        offline = httpx.MockTransport(lambda _request: self.fail("the verifier needs no network"))
        with redirect_stdout(verifier_out), httpx.Client(transport=offline) as client:
            self.assertEqual(storage_hold.run(
                ["operator-verifier", "--operator-token-file", str(self.token_file)],
                uow_factory=lambda: self.fail("the verifier needs no database"), client=client,
            ), 0)
        printed = json.loads(verifier_out.getvalue())
        # Mac and native derive the same enrolled identity; the credential is never printed.
        self.assertEqual(printed, {"credential_sha256": protocol.storage_hold_operator_verifier(CREDENTIAL)})
        self.assertNotIn(CREDENTIAL, verifier_out.getvalue())
        preview = ("preview", "--attempt-id", "attempt-1")
        errors = io.StringIO()
        with redirect_stderr(errors):
            for label, token_file in (
                ("group-readable file", _private(self.out / "shared.token", CREDENTIAL, 0o640)),
                ("not a credential line", _private(self.out / "short.token", "short")),
                ("missing file", self.out / "absent.token"),
            ):
                with self.subTest(label):
                    self.assertEqual(self.run_command(*preview, token_file=token_file), 1)
            self.assertEqual(self.refused, [])  # Refused on the Mac: nothing was sent.
            other = _private(self.out / "other.token", OTHER_CREDENTIAL)
            self.assertEqual(self.run_command(*preview, token_file=other), 1)
            self.assertTrue(protocol.revoke_storage_hold_operator(path=self.operator_path))
            self.assertEqual(self.run_command(*preview), 1)
        self.assertEqual(self.refused, [401, 403])
        self.assertEqual(self.posts, [])
        self.assertNotIn(CREDENTIAL, errors.getvalue())
        self.assertNotIn(OTHER_CREDENTIAL, errors.getvalue())
        self.assertEqual(self.registry.get(self.key).state, "processing")

    def test_a_lost_answer_completes_only_from_this_exact_durable_decision(self) -> None:
        self.assertEqual(self.run_command("preview", "--attempt-id", "attempt-1", "--out",
                                          str(self.out / "preview.json")), 0)
        preview_sha = json.loads((self.out / "preview.json").read_text())["native"]["preview_sha256"]
        errors = io.StringIO()
        with redirect_stderr(errors):
            # Lost before anything arrived: nothing is durable, so it is refused and safe to re-run.
            self.lose = "before"
            self.assertEqual(self.execute(preview_sha, "lost-before.json"), 1)
            self.assertFalse((self.out / "lost-before.json").exists())
            self.assertEqual(self.registry.get(self.key).state, "processing")
            # Lost after the decision became durable: recovered from the ordinary status.
            self.lose = "after"
            self.assertEqual(self.execute(preview_sha, "lost-after.json"), 0)
            # Another decision is refused by the native side, never completed by the recovered one.
            self.assertEqual(self.execute(preview_sha, "other.json", **{"--reason": "changed"}), 1)
            self.lose = "before"
            self.assertEqual(self.execute(preview_sha, "other-lost.json", **{"--reason": "changed"}), 1)
            self.lose = None
            self.assertEqual(self.execute(preview_sha, "replayed.json"), 0)
        self.assertFalse((self.out / "other.json").exists() or (self.out / "other-lost.json").exists())
        # The recovered receipt is byte-identical to the ordinary replay.
        self.assertEqual((self.out / "lost-after.json").read_bytes(), (self.out / "replayed.json").read_bytes())

    def test_recover_rebuilds_the_exact_original_attribution_from_native_storage(self) -> None:
        preview = self.registry.storage_hold_preview(self.key, runtime_identity_sha256=RUNTIME)
        recover = ("recover", "--attempt-id", "attempt-1", "--out")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_command(*recover, str(self.out / "none.json"), token=False), 1)
        self.assertFalse((self.out / "none.json").exists())
        # Another session decided and its answer is gone: only native storage remains.
        self.registry.decide_storage_hold(
            self.key, runtime_identity_sha256=RUNTIME, expected_preview_sha256=preview["preview_sha256"],
            decided_by="another operator", reason="the declared codec bound stays", fixed_by="none",
        )
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_command(*recover, str(self.out / "recovered.json"), token=False), 0)
        receipt = json.loads((self.out / "recovered.json").read_text())
        self.assertEqual(
            (receipt["decided_by"], receipt["reason"], receipt["fixed_by"], receipt["preview_sha256"],
             receipt["hold_reason"], receipt["attempt_id"]),
            ("another operator", "the declared codec bound stays", "none", preview["preview_sha256"],
             "codec_bound_exceeded", "attempt-1"),
        )
        self.assertEqual(receipt["decision_sha256"], storage_hold.decision_sha256(
            preview_sha256=preview["preview_sha256"], decided_by="another operator",
            reason="the declared codec bound stays", fixed_by="none",
        ))
        self.assertEqual(self.posts, [])  # Recovery reads the ordinary status only.


if __name__ == "__main__":
    unittest.main()
