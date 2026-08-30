from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged import (
    MinerUHttpStagedParser,
)
from disclosure_anchor.application.contracts.remote_parse_checkpoint import (
    TerminalReceipt,
    encode_terminal_receipt,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.ports.staged_provider_parser import (
    DurableCheckpointWitness,
    DurableFailureReceipt,
    PreparedLocalSubmission,
    RemoteArtifactReceipt,
    SubmissionAcceptanceAmbiguous,
)
from disclosure_anchor.domain.errors import ParserOutputContractError

PINNED_OPTIONS = ParserOptions(runtime_bundle_identity_sha256="sha256:" + "a" * 64)


def _expected_idempotency_key(
    source_sha256: str, attempt: str, fence: str, epoch: int
) -> str:
    digest = hashlib.sha256(
        f"{epoch:x}\0{source_sha256}\0{attempt}\0{fence}".encode()
    ).hexdigest()
    return f"{epoch:x}.{digest}"


def _witness(
    state: str,
    *,
    attempt: str = "attempt-1",
    fence: str = "fence-1",
    checkpoint_contract_version: int = 2,
    row_version: int = 1,
    claim_generation: int = 1,
    prepared_submission_sha256: str | None = None,
    accepted_submission_receipt_sha256: str | None = None,
    terminal_receipt_sha256: str | None = None,
    failure_receipt_sha256: str | None = None,
    remote_task_identity: str | None = None,
) -> DurableCheckpointWitness:
    projection = {
        "schema": "durable-checkpoint-witness.v1",
        "attempt_identity": attempt,
        "fence_identity": fence,
        "checkpoint_contract_version": checkpoint_contract_version,
        "row_version": row_version,
        "claim_generation": claim_generation,
        "state": state,
        "prepared_submission_sha256": prepared_submission_sha256,
        "accepted_submission_receipt_sha256": accepted_submission_receipt_sha256,
        "terminal_receipt_sha256": terminal_receipt_sha256,
        "failure_receipt_sha256": failure_receipt_sha256,
        "remote_task_identity": remote_task_identity,
    }
    exact = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    return DurableCheckpointWitness(
        **projection,
        exact_bytes=exact,
        sha256="sha256:" + hashlib.sha256(exact).hexdigest(),
    )


def _failure_receipt(
    failure_kind: str,
    *,
    attempt: str = "attempt-1",
    fence: str = "fence-1",
    remote_task_identity: str | None = None,
    terminal_receipt_sha256: str | None = None,
) -> DurableFailureReceipt:
    projection = {
        "schema": "durable-parse-failure.v1",
        "attempt_identity": attempt,
        "fence_identity": fence,
        "failure_kind": failure_kind,
        "remote_task_identity": remote_task_identity,
        "terminal_receipt_sha256": terminal_receipt_sha256,
    }
    exact = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    return DurableFailureReceipt(
        **projection,
        exact_bytes=exact,
        sha256="sha256:" + hashlib.sha256(exact).hexdigest(),
    )


def _terminal_exact_for_test(receipt: RemoteArtifactReceipt) -> bytes:
    return encode_terminal_receipt(
        TerminalReceipt(
            attempt_identity=receipt.attempt_identity,
            fence_identity=receipt.fence_identity,
            source_pdf_sha256=receipt.source_pdf_sha256,
            artifact_owner_identity=receipt.artifact_owner_identity,
            artifact_byte_count=receipt.artifact_byte_count,
            artifact_sha256="sha256:" + receipt.artifact_sha256,
            resume_token_sha256="sha256:"
            + hashlib.sha256(receipt.resume_token.encode("ascii")).hexdigest(),
        )
    ).exact_bytes


class _Reader:
    def read(self, output_dir: Path, *, source_pdf_sha256: str) -> object:
        return {"output": str(output_dir), "source": source_pdf_sha256}

    def locate_artifact_root(self, output_dir: Path) -> Path:
        return output_dir


class MinerUHttpStagedParserTests(unittest.TestCase):
    @staticmethod
    def _prepared_submission(
        parser: MinerUHttpStagedParser, source: Path, source_sha256: str
    ) -> PreparedLocalSubmission:
        identity = parser.prepare_submission_identity(
            options=PINNED_OPTIONS,
            source_pdf_sha256=source_sha256,
            attempt_identity="attempt-1",
            fence_identity="fence-1",
            submission_epoch_unix=1_000_000,
        )
        return parser.prepare_local_submission(
            input_pdf=source,
            options=PINNED_OPTIONS,
            identity=identity,
            witness=_witness(
                "prepared", prepared_submission_sha256=identity.sha256
            ),
        )

    def test_accept_disconnect_reconciles_and_submitted_checkpoint_resumes(self) -> None:
        posts = 0
        accepted = False
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            key = _expected_idempotency_key(
                source_sha256, "attempt-1", "fence-1", 1_000_000
            )

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal posts, accepted
                if request.url.path.startswith("/tasks/by-idempotency/"):
                    if not accepted:
                        return httpx.Response(404)
                    return httpx.Response(200, json={
                        "task_id": "task-1", "status_url": "/tasks/task-1",
                        "result_url": "/tasks/task-1/result",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "idempotency_key": key, "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                    })
                if request.method == "POST" and request.url.path == "/tasks":
                    posts += 1
                    accepted = True
                    raise httpx.ReadTimeout("accepted then disconnected", request=request)
                return httpx.Response(500)

            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            prepared = self._prepared_submission(parser, source, source_sha256)
            handle = parser.begin_remote_parse(
                options=PINNED_OPTIONS,
                prepared_submission=prepared,
            )
            public, secret = handle.submission_checkpoint()
            self.assertNotIn(secret.token_bytes, public.exact_bytes)
            resumed = parser.resume_submitted_parse(
                receipt=public, secret=secret, options=PINNED_OPTIONS
            )
            self.assertEqual(resumed.submission_checkpoint(), (public, secret))
            self.assertEqual(posts, 1)
            with self.assertRaisesRegex(
                ParserOutputContractError, "submitted snapshot discard receipt drifted"
            ):
                parser.discard_local_submission(
                    prepared_submission=prepared,
                    witness=_witness(
                        "submitted",
                        accepted_submission_receipt_sha256="sha256:" + "d" * 64,
                        remote_task_identity=public.remote_task_identity,
                    ),
                    submission_receipt=public,
                )
            self.assertTrue(prepared.snapshot_path.exists())
            parser.discard_local_submission(
                prepared_submission=prepared,
                witness=_witness(
                    "submitted",
                    accepted_submission_receipt_sha256=public.sha256,
                    remote_task_identity=public.remote_task_identity,
                ),
                submission_receipt=public,
            )
            parser.discard_local_submission(
                prepared_submission=prepared,
                witness=_witness(
                    "submitted",
                    accepted_submission_receipt_sha256=public.sha256,
                    remote_task_identity=public.remote_task_identity,
                ),
                submission_receipt=public,
            )
            self.assertFalse(prepared.snapshot_path.exists())

    def test_prepare_submission_identity_is_pure_and_begin_rejects_drift(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            identity = parser.prepare_submission_identity(
                options=PINNED_OPTIONS, source_pdf_sha256=source_sha256,
                attempt_identity="attempt-1", fence_identity="fence-1",
                submission_epoch_unix=1_000_000,
            )
            with self.assertRaisesRegex(
                ParserOutputContractError, "durable prepared checkpoint"
            ):
                parser.prepare_local_submission(
                    input_pdf=source,
                    options=PINNED_OPTIONS,
                    identity=identity,
                    witness=_witness(
                        "prepared",
                        prepared_submission_sha256="sha256:" + "d" * 64,
                    ),
                )
            with self.assertRaisesRegex(ValueError, "exactly 2"):
                _witness(
                    "prepared",
                    checkpoint_contract_version=1,
                    prepared_submission_sha256=identity.sha256,
                )
            prepared = parser.prepare_local_submission(
                input_pdf=source,
                options=PINNED_OPTIONS,
                identity=identity,
                witness=_witness(
                    "prepared", prepared_submission_sha256=identity.sha256
                ),
            )
            self.assertEqual(calls, 0)
            self.assertEqual(prepared.identity.client_submit_key, _expected_idempotency_key(
                source_sha256, "attempt-1", "fence-1", 1_000_000
            ))
            with self.assertRaisesRegex(
                ParserOutputContractError, "prepared submission identity drifted"
            ):
                parser.begin_remote_parse(
                    options=replace(
                        PINNED_OPTIONS,
                        runtime_bundle_identity_sha256="sha256:" + "b" * 64,
                    ),
                    prepared_submission=prepared,
                )
            self.assertEqual(calls, 0)

    def test_post_ambiguous_responses_reconcile_delayed_acceptance(self) -> None:
        for mode in ("500", "truncated", "timeout-delayed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "input.pdf"
                source.write_bytes(b"%PDF-stage")
                source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
                key = _expected_idempotency_key(
                    source_sha256, "attempt-1", "fence-1", 1_000_000
                )
                post_started = False
                reconcile_gets = 0

                def accepted_payload() -> dict[str, str]:
                    return {
                        "task_id": "task-1",
                        "status_url": "/tasks/task-1",
                        "result_url": "/tasks/task-1/result",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "idempotency_key": key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                    }

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal post_started, reconcile_gets
                    if request.url.path.startswith("/tasks/by-idempotency/"):
                        if not post_started:
                            return httpx.Response(404)
                        reconcile_gets += 1
                        if mode == "timeout-delayed" and reconcile_gets < 3:
                            return httpx.Response(404)
                        return httpx.Response(200, json=accepted_payload())
                    post_started = True
                    if mode == "500":
                        return httpx.Response(500)
                    if mode == "truncated":
                        return httpx.Response(
                            200, content=b'{"task_id":',
                            headers={"content-type": "application/json"},
                        )
                    raise httpx.ReadTimeout("accepted", request=request)

                parser = MinerUHttpStagedParser(
                    api_url="http://mineru.test:30000",
                    server_url="http://vlm.test:30000/v1",
                    spool_root=Path(directory) / "spool",
                    transport=httpx.MockTransport(handler),
                )
                handle = parser.begin_remote_parse(
                    options=PINNED_OPTIONS,
                    prepared_submission=self._prepared_submission(
                        parser, source, source_sha256
                    ),
                )
                self.assertEqual(
                    handle.submission_checkpoint()[0].remote_task_identity, "task-1"
                )

    def test_ambiguous_restart_prestage_lookup_never_becomes_definite(self) -> None:
        for restart_mode in ("transport", "500"):
            with self.subTest(restart_mode=restart_mode), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "input.pdf"
                source.write_bytes(b"%PDF-stage")
                source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
                phase = "first"
                post_started = False

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal post_started
                    if request.url.path.startswith("/tasks/by-idempotency/"):
                        if phase == "restart":
                            if restart_mode == "transport":
                                raise httpx.ConnectError("offline", request=request)
                            return httpx.Response(500)
                        if not post_started:
                            return httpx.Response(404)
                        return httpx.Response(404)
                    post_started = True
                    raise httpx.ReadTimeout("accepted unknown", request=request)

                parser = MinerUHttpStagedParser(
                    api_url="http://mineru.test:30000",
                    server_url="http://vlm.test:30000/v1",
                    spool_root=Path(directory) / "spool",
                    transport=httpx.MockTransport(handler),
                )
                prepared = self._prepared_submission(parser, source, source_sha256)
                with self.assertRaises(SubmissionAcceptanceAmbiguous):
                    parser.begin_remote_parse(
                        options=PINNED_OPTIONS, prepared_submission=prepared
                    )
                prepared_again = parser.prepare_local_submission(
                    input_pdf=source,
                    options=PINNED_OPTIONS,
                    identity=prepared.identity,
                    witness=_witness(
                        "prepared",
                        prepared_submission_sha256=prepared.identity.sha256,
                    ),
                )
                self.assertEqual(prepared_again.snapshot_path, prepared.snapshot_path)
                self.assertEqual(prepared_again.snapshot_inode, prepared.snapshot_inode)
                self.assertEqual(
                    len(list((Path(directory) / "spool").glob(".upload-*.pdf"))), 1
                )
                phase = "restart"
                with self.assertRaises(SubmissionAcceptanceAmbiguous):
                    parser.begin_remote_parse(
                        options=PINNED_OPTIONS, prepared_submission=prepared_again
                    )

    def test_snapshot_discard_policy_and_unsafe_reuse_are_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
            )
            identity = parser.prepare_submission_identity(
                options=PINNED_OPTIONS, source_pdf_sha256=source_sha256,
                attempt_identity="attempt-1", fence_identity="fence-1",
                submission_epoch_unix=1_000_000,
            )
            for reason in (
                "pre_submission_failed",
                "remote_failure_committed",
                "local_failure_committed",
            ):
                terminal_sha = (
                    "sha256:" + "d" * 64
                    if reason == "local_failure_committed"
                    else None
                )
                task_id = None if reason == "pre_submission_failed" else "task-1"
                failure = _failure_receipt(
                    reason,
                    remote_task_identity=task_id,
                    terminal_receipt_sha256=terminal_sha,
                )
                prepared = parser.prepare_local_submission(
                    input_pdf=source,
                    options=PINNED_OPTIONS,
                    identity=identity,
                    witness=_witness(
                        "prepared", prepared_submission_sha256=identity.sha256
                    ),
                )
                parser.discard_local_submission(
                    prepared_submission=prepared,
                    witness=_witness(
                        reason,
                        failure_receipt_sha256=failure.sha256,
                        terminal_receipt_sha256=terminal_sha,
                        remote_task_identity=task_id,
                    ),
                    failure_receipt=failure,
                )
                self.assertFalse(prepared.snapshot_path.exists())

            prepared = parser.prepare_local_submission(
                input_pdf=source,
                options=PINNED_OPTIONS,
                identity=identity,
                witness=_witness(
                    "prepared", prepared_submission_sha256=identity.sha256
                ),
            )
            with self.assertRaisesRegex(
                ParserOutputContractError, "exact durable checkpoint state"
            ):
                parser.discard_local_submission(
                    prepared_submission=prepared,
                    witness=_witness(
                        "finish_committed",
                        terminal_receipt_sha256="sha256:" + "d" * 64,
                        remote_task_identity="task-1",
                    ),
                )
            self.assertTrue(prepared.snapshot_path.exists())
            with self.assertRaisesRegex(
                ParserOutputContractError, "exact durable checkpoint state"
            ):
                failure = _failure_receipt("pre_submission_failed")
                parser.discard_local_submission(
                    prepared_submission=prepared,
                    witness=_witness(
                        "pre_submission_failed",
                        claim_generation=2,
                        failure_receipt_sha256=failure.sha256,
                    ),
                    failure_receipt=failure,
                )
            self.assertTrue(prepared.snapshot_path.exists())
            snapshot_path = prepared.snapshot_path
            failure = _failure_receipt("pre_submission_failed")
            parser.discard_local_submission(
                prepared_submission=prepared,
                witness=_witness(
                    "pre_submission_failed",
                    failure_receipt_sha256=failure.sha256,
                ),
                failure_receipt=failure,
            )
            snapshot_path.symlink_to(source)
            with self.assertRaises((OSError, ParserOutputContractError)):
                parser.prepare_local_submission(
                    input_pdf=source,
                    options=PINNED_OPTIONS,
                    identity=identity,
                    witness=_witness(
                        "prepared", prepared_submission_sha256=identity.sha256
                    ),
                )
            self.assertTrue(snapshot_path.is_symlink())
            snapshot_path.unlink()
            seed = Path(directory) / "seed"
            seed.write_bytes(b"%PDF-stage")
            seed.chmod(0o600)
            hardlink_peer = Path(directory) / "peer"
            os.link(seed, hardlink_peer)
            os.link(seed, snapshot_path)
            with self.assertRaisesRegex(ParserOutputContractError, "unsafe"):
                parser.prepare_local_submission(
                    input_pdf=source,
                    options=PINNED_OPTIONS,
                    identity=identity,
                    witness=_witness(
                        "prepared", prepared_submission_sha256=identity.sha256
                    ),
                )
            self.assertTrue(snapshot_path.exists())

    def test_copy_failure_never_publishes_a_partial_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
            )
            identity = parser.prepare_submission_identity(
                options=PINNED_OPTIONS,
                source_pdf_sha256=source_sha256,
                attempt_identity="attempt-1",
                fence_identity="fence-1",
                submission_epoch_unix=1_000_000,
            )
            with patch(
                "disclosure_anchor.adapters.parsers.mineru_medium.http_staged."
                "_write_snapshot_from_source",
                side_effect=OSError("crash before publish"),
            ), self.assertRaisesRegex(OSError, "crash before publish"):
                parser.prepare_local_submission(
                    input_pdf=source,
                    options=PINNED_OPTIONS,
                    identity=identity,
                    witness=_witness(
                        "prepared", prepared_submission_sha256=identity.sha256
                    ),
                )
            self.assertEqual(
                list((Path(directory) / "spool").glob(".upload-*.pdf")), []
            )

            recovered = parser.prepare_local_submission(
                input_pdf=source,
                options=PINNED_OPTIONS,
                identity=identity,
                    witness=_witness(
                        "prepared", prepared_submission_sha256=identity.sha256
                    ),
            )

            self.assertEqual(recovered.snapshot_path.read_bytes(), source.read_bytes())
            self.assertEqual(
                len(list((Path(directory) / "spool").glob(".upload-*.pdf"))), 1
            )

    def test_sigkill_stale_temp_is_reclaimed_under_attempt_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage" * 4096)
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            root = Path(directory) / "spool"
            script = textwrap.dedent(
                f"""
                import hashlib, json, os, signal
                from pathlib import Path
                from disclosure_anchor.adapters.parsers.mineru_medium import http_staged
                from disclosure_anchor.adapters.parsers.mineru_medium.http_staged import MinerUHttpStagedParser
                from disclosure_anchor.application.ports.parser import ParserOptions
                from disclosure_anchor.application.ports.staged_provider_parser import DurableCheckpointWitness
                parser = MinerUHttpStagedParser(api_url='http://mineru.test:30000', server_url='http://vlm.test:30000/v1', spool_root=Path({str(root)!r}))
                options = ParserOptions(runtime_bundle_identity_sha256='sha256:' + 'a' * 64)
                identity = parser.prepare_submission_identity(options=options, source_pdf_sha256={source_sha256!r}, attempt_identity='attempt-1', fence_identity='fence-1', submission_epoch_unix=1000000)
                projection = {{'schema':'durable-checkpoint-witness.v1','attempt_identity':'attempt-1','fence_identity':'fence-1','checkpoint_contract_version':2,'row_version':1,'claim_generation':1,'state':'prepared','prepared_submission_sha256':identity.sha256,'accepted_submission_receipt_sha256':None,'terminal_receipt_sha256':None,'failure_receipt_sha256':None,'remote_task_identity':None}}
                exact = json.dumps(projection, sort_keys=True, separators=(',', ':')).encode()
                witness = DurableCheckpointWitness(**projection, exact_bytes=exact, sha256='sha256:' + hashlib.sha256(exact).hexdigest())
                original = http_staged._write_snapshot_from_source
                def crash(**kwargs):
                    original(**kwargs)
                    os.kill(os.getpid(), signal.SIGKILL)
                http_staged._write_snapshot_from_source = crash
                parser.prepare_local_submission(input_pdf=Path({str(source)!r}), options=options, identity=identity, witness=witness)
                """
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                env={**os.environ, "PYTHONPATH": "src"},
                cwd=Path(__file__).parents[2],
                check=False,
            )
            self.assertLess(completed.returncode, 0)
            self.assertEqual(len(list(root.glob(".upload-*.tmp-*"))), 1)
            self.assertEqual(list(root.glob(".upload-*.pdf")), [])

            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=root,
            )
            identity = parser.prepare_submission_identity(
                options=PINNED_OPTIONS,
                source_pdf_sha256=source_sha256,
                attempt_identity="attempt-1",
                fence_identity="fence-1",
                submission_epoch_unix=1_000_000,
            )
            prepared = parser.prepare_local_submission(
                input_pdf=source,
                options=PINNED_OPTIONS,
                identity=identity,
                witness=_witness(
                    "prepared", prepared_submission_sha256=identity.sha256
                ),
            )
            self.assertEqual(list(root.glob(".upload-*.tmp-*")), [])
            self.assertEqual(prepared.snapshot_path.read_bytes(), source.read_bytes())

    def test_concurrent_snapshot_publish_returns_one_immutable_inode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage" * 1024)
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            root = Path(directory) / "spool"
            first_parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=root,
            )
            second_parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=root,
            )
            identity = first_parser.prepare_submission_identity(
                options=PINNED_OPTIONS,
                source_pdf_sha256=source_sha256,
                attempt_identity="attempt-1",
                fence_identity="fence-1",
                submission_epoch_unix=1_000_000,
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        parser.prepare_local_submission,
                        input_pdf=source,
                        options=PINNED_OPTIONS,
                        identity=identity,
                    witness=_witness(
                        "prepared", prepared_submission_sha256=identity.sha256
                    ),
                    )
                    for parser in (first_parser, second_parser)
                ]
                prepared = [future.result(timeout=10) for future in futures]

            self.assertEqual(prepared[0].snapshot_path, prepared[1].snapshot_path)
            self.assertEqual(prepared[0].snapshot_inode, prepared[1].snapshot_inode)
            self.assertEqual(prepared[0].snapshot_path.read_bytes(), source.read_bytes())
            self.assertEqual(len(list(root.glob(".upload-*.pdf"))), 1)

    def test_spool_root_rejects_symlink_and_permissive_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            private = base / "private"
            private.mkdir(mode=0o700)
            target = private / "target"
            target.mkdir(mode=0o700)
            link = private / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ParserOutputContractError, "symlink"):
                MinerUHttpStagedParser(
                    api_url="http://mineru.test:30000",
                    server_url="http://vlm.test:30000/v1",
                    spool_root=link,
                )
            permissive = base / "permissive"
            permissive.mkdir(mode=0o777)
            permissive.chmod(0o777)
            with self.assertRaisesRegex(ParserOutputContractError, "parent is not private"):
                MinerUHttpStagedParser(
                    api_url="http://mineru.test:30000",
                    server_url="http://vlm.test:30000/v1",
                    spool_root=permissive / "spool",
                )

    def test_every_post_started_http_rejection_remains_ambiguous(self) -> None:
        for post_status in (301, 400, 401, 403, 404, 409, 422):
            with self.subTest(post_status=post_status), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "input.pdf"
                source.write_bytes(b"%PDF-stage")
                source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
                posted = False

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal posted
                    if request.url.path.startswith("/tasks/by-idempotency/"):
                        return httpx.Response(404)
                    posted = True
                    return httpx.Response(post_status)

                parser = MinerUHttpStagedParser(
                    api_url="http://mineru.test:30000",
                    server_url="http://vlm.test:30000/v1",
                    spool_root=Path(directory) / "spool",
                    transport=httpx.MockTransport(handler),
                )
                prepared = self._prepared_submission(parser, source, source_sha256)
                with self.assertRaises(SubmissionAcceptanceAmbiguous):
                    parser.begin_remote_parse(
                        options=PINNED_OPTIONS, prepared_submission=prepared
                    )
                self.assertTrue(posted)

    def test_resume_rejects_retired_v1_and_v2_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
            )
            receipt = RemoteArtifactReceipt(
                attempt_identity="attempt-1",
                fence_identity="fence-1",
                artifact_owner_identity="owner-1",
                artifact_byte_count=1,
                artifact_sha256="a" * 64,
                source_pdf_sha256="sha256:" + "b" * 64,
            )
            for version in (1, 2):
                with self.subTest(version=version):
                    token = base64.urlsafe_b64encode(
                        json.dumps(
                            {"v": version}, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).decode()
                    with self.assertRaisesRegex(
                        ParserOutputContractError, "resume token shape"
                    ):
                        parser.resume_remote_parse(
                            receipt=replace(receipt, resume_token=token),
                            options=PINNED_OPTIONS,
                        )

    def test_failed_remote_ack_requires_exact_database_failure_checkpoint(self) -> None:
        ack_calls = 0
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            key = _expected_idempotency_key(
                source_sha256, "attempt-1", "fence-1", 1_000_000
            )

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal ack_calls
                if request.url.path.startswith("/tasks/by-idempotency/"):
                    return httpx.Response(404)
                if request.url.path.endswith("/ack"):
                    ack_calls += 1
                    return httpx.Response(
                        200,
                        json={
                            "schema": "mineru-task-protocol.v2",
                            "task_id": "task-1",
                            "status": "consumed",
                        },
                    )
                return httpx.Response(
                    202,
                    json={
                        "task_id": "task-1",
                        "status_url": "/tasks/task-1",
                        "result_url": "/tasks/task-1/result",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "idempotency_key": key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                    },
                )

            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            handle = parser.begin_remote_parse(
                options=PINNED_OPTIONS,
                prepared_submission=self._prepared_submission(
                    parser, source, source_sha256
                ),
            )
            remote_failure = _failure_receipt(
                "remote_failure_committed", remote_task_identity="task-1"
            )
            submitted = _witness(
                "submitted",
                accepted_submission_receipt_sha256="sha256:" + "d" * 64,
                remote_task_identity="task-1",
            )
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "remote_failure_committed or local_failure_committed",
            ):
                handle.acknowledge_after_failure_committed(
                    witness=submitted, failure_receipt=remote_failure
                )
            with self.assertRaisesRegex(ValueError, "state is unsupported"):
                _witness("failure_committed")
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "remote_failure_committed or local_failure_committed",
            ):
                handle.acknowledge_after_failure_committed(
                    witness=_witness(
                        "remote_failure_committed",
                        attempt="other-attempt",
                        failure_receipt_sha256=remote_failure.sha256,
                        remote_task_identity="task-1",
                    ),
                    failure_receipt=remote_failure,
                )
            wrong_task_failure = _failure_receipt(
                "remote_failure_committed", remote_task_identity="task-other"
            )
            with self.assertRaisesRegex(
                ParserOutputContractError,
                "remote_failure_committed or local_failure_committed",
            ):
                handle.acknowledge_after_failure_committed(
                    witness=_witness(
                        "remote_failure_committed",
                        failure_receipt_sha256=wrong_task_failure.sha256,
                        remote_task_identity="task-other",
                    ),
                    failure_receipt=wrong_task_failure,
                )
            self.assertEqual(ack_calls, 0)
            handle.acknowledge_after_failure_committed(
                witness=_witness(
                    "remote_failure_committed",
                    failure_receipt_sha256=remote_failure.sha256,
                    remote_task_identity="task-1",
                ),
                failure_receipt=remote_failure,
            )
            self.assertEqual(ack_calls, 1)
            local_terminal = "sha256:" + "e" * 64
            local_failure = _failure_receipt(
                "local_failure_committed",
                remote_task_identity="task-1",
                terminal_receipt_sha256=local_terminal,
            )
            with self.assertRaisesRegex(
                ParserOutputContractError, "exact terminal receipt"
            ):
                handle.acknowledge_after_failure_committed(
                    witness=_witness(
                        "local_failure_committed",
                        terminal_receipt_sha256=local_terminal,
                        failure_receipt_sha256=local_failure.sha256,
                        remote_task_identity="task-1",
                    ),
                    failure_receipt=local_failure,
                )
            self.assertEqual(ack_calls, 1)

    @staticmethod
    def _zip(entries: list[tuple[str, bytes]], *, symlink: bool = False) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in entries:
                if symlink:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, content)
                else:
                    archive.writestr(name, content)
        return buffer.getvalue()

    def _completed_parser(
        self,
        directory: str,
        result: bytes,
        *,
        result_url: str = "/tasks/task-1/result",
    ) -> tuple[MinerUHttpStagedParser, Path, str, list[bytes]]:
        submissions: list[bytes] = []
        artifact_sha256 = hashlib.sha256(result).hexdigest()
        artifact_bytes = len(result)
        owner = hashlib.sha256(
            f"task-1\0{artifact_sha256}\0{artifact_bytes}".encode()
        ).hexdigest()
        idempotency_key = ""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/tasks/by-idempotency/"):
                return httpx.Response(404)
            if request.method == "POST" and request.url.path == "/tasks":
                submissions.append(request.read())
                return httpx.Response(
                    202,
                    json={
                        "task_id": "task-1",
                        "status_url": "/tasks/task-1",
                        "result_url": result_url,
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "idempotency_key": idempotency_key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                    },
                )
            if request.method == "POST" and request.url.path.endswith("/lease"):
                return httpx.Response(
                    200,
                    json={
                        "schema": "mineru-task-protocol.v2",
                        "task_id": "task-1",
                        "lease_until_unix": 9_999_999_999.0,
                    },
                )
            if request.url.path == "/tasks/task-1":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "protocol_state": "completed",
                        "idempotency_key": idempotency_key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                        "result_artifact_schema": "mineru-retained-result.v1",
                        "result_artifact_sha256": artifact_sha256,
                        "result_artifact_bytes": artifact_bytes,
                        "result_artifact_owner": owner,
                    },
                )
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/zip",
                    "x-mineru-result-sha256": artifact_sha256,
                    "x-mineru-result-owner": owner,
                },
                content=result,
            )

        source = Path(directory) / "input.pdf"
        source.write_bytes(b"%PDF-stage")
        source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
        idempotency_key = _expected_idempotency_key(
            source_sha256, "attempt-1", "fence-1", 1_000_000
        )
        parser = MinerUHttpStagedParser(
            api_url="http://mineru.test:30000",
            server_url="http://vlm.test:30000/v1",
            spool_root=Path(directory) / "spool",
            reader=_Reader(),
            transport=httpx.MockTransport(handler),
        )  # type: ignore[arg-type]
        return parser, source, source_sha256, submissions

    def test_v2_submission_binds_identities_and_leases_before_credit_return(
        self,
    ) -> None:
        calls: list[str] = []
        artifact_sha256 = "a" * 64
        artifact_bytes = 17
        owner = hashlib.sha256(
            f"task-1\0{artifact_sha256}\0{artifact_bytes}".encode()
        ).hexdigest()

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            if request.url.path.startswith("/tasks/by-idempotency/"):
                return httpx.Response(404)
            if request.method == "POST" and request.url.path == "/tasks":
                body = request.read()
                self.assertIn(b"agent_idempotency_key", body)
                self.assertIn(b"attempt-1", body)
                self.assertIn(b"fence-1", body)
                return httpx.Response(
                    202,
                    json={
                        "task_id": "task-1",
                        "status_url": "/tasks/task-1",
                        "result_url": "/tasks/task-1/result",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "idempotency_key": idempotency_key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                    },
                )
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "protocol_state": "completed",
                        "idempotency_key": idempotency_key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                        "result_artifact_schema": "mineru-retained-result.v1",
                        "result_artifact_sha256": artifact_sha256,
                        "result_artifact_bytes": artifact_bytes,
                        "result_artifact_owner": owner,
                    },
                )
            if request.url.path.endswith("/ack"):
                return httpx.Response(
                    200,
                    json={
                        "schema": "mineru-task-protocol.v2",
                        "task_id": "task-1",
                        "status": "consumed",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "schema": "mineru-task-protocol.v2",
                    "task_id": "task-1",
                    "lease_until_unix": 9999999999.0,
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            idempotency_key = _expected_idempotency_key(
                source_sha256, "attempt-1", "fence-1", 1_000_000
            )
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            handle = parser.begin_remote_parse(
                options=PINNED_OPTIONS,
                prepared_submission=self._prepared_submission(
                    parser, source, source_sha256
                ),
            )
            receipt = handle.wait_terminal()
            with self.assertRaisesRegex(
                ParserOutputContractError, "finish_committed"
            ):
                handle.acknowledge_after_finish_committed(
                    receipt=receipt,
                    witness=_witness(
                        "submitted",
                        accepted_submission_receipt_sha256="sha256:" + "d" * 64,
                        remote_task_identity="task-1",
                    ),
                )
            with self.assertRaisesRegex(
                ParserOutputContractError, "finish_committed"
            ):
                handle.acknowledge_after_finish_committed(
                    receipt=receipt,
                    witness=_witness(
                        "finish_committed",
                        terminal_receipt_sha256="sha256:" + "d" * 64,
                        remote_task_identity="task-other",
                    ),
                )
            handle.acknowledge_after_finish_committed(
                receipt=receipt,
                witness=_witness(
                    "finish_committed",
                    terminal_receipt_sha256="sha256:"
                    + hashlib.sha256(_terminal_exact_for_test(receipt)).hexdigest(),
                    remote_task_identity="task-1",
                ),
            )
            handle.acknowledge_after_finish_committed(
                receipt=receipt,
                witness=_witness(
                    "finish_committed",
                    terminal_receipt_sha256="sha256:"
                    + hashlib.sha256(_terminal_exact_for_test(receipt)).hexdigest(),
                    remote_task_identity="task-1",
                ),
            )
        self.assertEqual(receipt.artifact_sha256, artifact_sha256)
        self.assertEqual(calls.count("POST /tasks/task-1/ack"), 2)

    def test_v2_rejects_duplicate_submit_wire_fields(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            if _request.url.path.startswith("/tasks/by-idempotency/"):
                return httpx.Response(404)
            return httpx.Response(
                202,
                content=(
                    b'{"task_id":"task-1","task_id":"task-2",'
                    b'"status_url":"/tasks/task-1",'
                    b'"result_url":"/tasks/task-1/result"}'
                ),
                headers={"content-type": "application/json"},
            )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            with self.assertRaisesRegex(
                SubmissionAcceptanceAmbiguous, "acceptance remains ambiguous"
            ):
                parser.begin_remote_parse(
                    options=PINNED_OPTIONS,
                    prepared_submission=self._prepared_submission(
                        parser,
                        source,
                        "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
                    ),
                )

    def test_v2_accepts_identity_bound_existing_post_200(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            key = _expected_idempotency_key(
                source_sha256, "attempt-1", "fence-1", 1_000_000
            )

            def handler(_request: httpx.Request) -> httpx.Response:
                if _request.url.path.startswith("/tasks/by-idempotency/"):
                    return httpx.Response(404)
                return httpx.Response(200, json={
                    "task_id": "task-1", "status_url": "/tasks/task-1",
                    "result_url": "/tasks/task-1/result",
                    "task_protocol_schema": "mineru-task-protocol.v2",
                    "idempotency_key": key, "attempt_identity": "attempt-1",
                    "fence_identity": "fence-1",
                })

            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            handle = parser.begin_remote_parse(
                options=PINNED_OPTIONS,
                prepared_submission=self._prepared_submission(
                    parser, source, source_sha256
                ),
            )
            self.assertIsNotNone(handle)

    def test_v2_rejects_wire_json_before_exceeding_bound(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            if _request.url.path.startswith("/tasks/by-idempotency/"):
                return httpx.Response(404)
            return httpx.Response(
                202, content=b"{" + b" " * (1024 * 1024) + b"}",
                headers={"content-type": "application/json"},
            )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            with self.assertRaisesRegex(
                SubmissionAcceptanceAmbiguous, "acceptance remains ambiguous"
            ):
                parser.begin_remote_parse(
                    options=PINNED_OPTIONS,
                    prepared_submission=self._prepared_submission(
                        parser,
                        source,
                        "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
                    ),
                )

    def test_submit_rejects_cross_origin_result_url(self) -> None:
        idempotency_key = ""

        def handler(_request: httpx.Request) -> httpx.Response:
            if _request.url.path.startswith("/tasks/by-idempotency/"):
                return httpx.Response(404)
            return httpx.Response(
                202,
                json={
                    "task_id": "task-1",
                    "status_url": "/tasks/task-1",
                    "result_url": "http://attacker.invalid/result",
                    "task_protocol_schema": "mineru-task-protocol.v2",
                    "idempotency_key": idempotency_key,
                    "attempt_identity": "attempt-1",
                    "fence_identity": "fence-1",
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            idempotency_key = _expected_idempotency_key(
                "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
                "attempt-1",
                "fence-1",
                1_000_000,
            )
            with self.assertRaisesRegex(
                SubmissionAcceptanceAmbiguous,
                "acceptance remains ambiguous",
            ):
                parser.begin_remote_parse(
                    options=PINNED_OPTIONS,
                    prepared_submission=self._prepared_submission(
                        parser,
                        source,
                        "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(),
                    ),
                )

    def test_effective_defaults_match_pinned_cli_form(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parser, source, source_sha256, submissions = self._completed_parser(
                directory,
                self._zip([("result.txt", b"ok")]),
            )
            parser.begin_remote_parse(
                options=PINNED_OPTIONS,
                prepared_submission=self._prepared_submission(
                    parser, source, source_sha256
                ),
            )
        body = submissions[0]
        self.assertIn(b'name="effort"\r\n\r\nmedium', body)
        self.assertIn(b'name="image_analysis"\r\n\r\nfalse', body)
        self.assertNotIn(b"none", body.lower())

    def test_materialize_atomically_promotes_verified_spool(self) -> None:
        result_zip = self._zip([("document/result.txt", b"verified")])
        with tempfile.TemporaryDirectory() as directory:
            parser, source, source_sha256, _ = self._completed_parser(
                directory, result_zip
            )
            handle = parser.begin_remote_parse(
                options=ParserOptions(
                    runtime_bundle_identity_sha256="sha256:" + "a" * 64
                ),
                prepared_submission=self._prepared_submission(
                    parser, source, source_sha256
                ),
            )
            receipt = handle.wait_terminal()
            output = Path(directory) / "out"
            prepared = handle.prepare_materialization(
                receipt=receipt, source_pdf_sha256=source_sha256
            )
            expected_terminal = encode_terminal_receipt(TerminalReceipt(
                attempt_identity=receipt.attempt_identity,
                fence_identity=receipt.fence_identity,
                source_pdf_sha256=receipt.source_pdf_sha256,
                artifact_owner_identity=receipt.artifact_owner_identity,
                artifact_byte_count=receipt.artifact_byte_count,
                artifact_sha256="sha256:" + receipt.artifact_sha256,
                resume_token_sha256="sha256:"
                + hashlib.sha256(receipt.resume_token.encode("ascii")).hexdigest(),
            ))
            self.assertEqual(prepared.terminal_receipt_sha256, expected_terminal.sha256)
            with self.assertRaisesRegex(ParserOutputContractError, "claim generation"):
                handle.materialize_prepared(
                    prepared=prepared, receipt=receipt, output_dir=output,
                    source_pdf_sha256=source_sha256,
                    parser_target_identity_sha256="sha256:" + "c" * 64,
                    producer_claim_generation=0,
                )
            parsed = handle.materialize_prepared(
                prepared=prepared,
                receipt=receipt,
                output_dir=output,
                source_pdf_sha256=source_sha256,
                parser_target_identity_sha256="sha256:" + "c" * 64,
                producer_claim_generation=1,
            )
            self.assertEqual(parsed.result.artifact_root, output)
            self.assertEqual(
                (output / "document" / "result.txt").read_bytes(), b"verified"
            )
            replay = handle.materialize_prepared(
                prepared=prepared, receipt=receipt, output_dir=output,
                source_pdf_sha256=source_sha256,
                parser_target_identity_sha256="sha256:" + "c" * 64,
                producer_claim_generation=2,
            )
            self.assertEqual(replay.result.artifact_root, output)
            self.assertEqual(replay.evidence.producer_claim_generation, 1)
            manifest_path = output / ".agent-materialization-manifest.v1.json"
            original_manifest = manifest_path.read_bytes()
            zero_manifest = json.loads(original_manifest)
            zero_manifest["produced_generation"] = 0
            manifest_path.write_bytes(json.dumps(
                zero_manifest, sort_keys=True, separators=(",", ":")
            ).encode())
            with self.assertRaisesRegex(
                ParserOutputContractError, "claim generation drifted"
            ):
                handle.materialize_prepared(
                    prepared=prepared, receipt=receipt, output_dir=output,
                    source_pdf_sha256=source_sha256,
                    parser_target_identity_sha256="sha256:" + "c" * 64,
                    producer_claim_generation=2,
                )
            manifest_path.write_bytes(original_manifest)
            (output / "document" / "result.txt").write_bytes(b"drift")
            with self.assertRaisesRegex(ParserOutputContractError, "output drifted"):
                handle.materialize_prepared(
                    prepared=prepared, receipt=receipt, output_dir=output,
                    source_pdf_sha256=source_sha256,
                    parser_target_identity_sha256="sha256:" + "c" * 64,
                    producer_claim_generation=2,
                )
            self.assertEqual(
                (output / "document" / "result.txt").read_bytes(), b"drift"
            )

    def test_prepare_enforces_member_and_decoded_projection_caps(self) -> None:
        result_zip = self._zip([("a.json", b"1234"), ("b.txt", b"56")])
        with tempfile.TemporaryDirectory() as directory:
            parser, source, source_sha256, _ = self._completed_parser(
                directory, result_zip
            )
            handle = parser.begin_remote_parse(
                options=PINNED_OPTIONS,
                prepared_submission=self._prepared_submission(
                    parser, source, source_sha256
                ),
            )
            receipt = handle.wait_terminal()
            with patch(
                "disclosure_anchor.adapters.parsers.mineru_medium.http_staged._MAX_ZIP_MEMBERS",
                1,
            ):
                with self.assertRaisesRegex(ParserOutputContractError, "member envelope"):
                    handle.prepare_materialization(
                        receipt=receipt, source_pdf_sha256=source_sha256
                    )
            with patch(
                "disclosure_anchor.adapters.parsers.mineru_medium.http_staged._MAX_DECODED_BYTES",
                8,
            ):
                with self.assertRaisesRegex(ParserOutputContractError, "decoded-byte"):
                    handle.prepare_materialization(
                        receipt=receipt, source_pdf_sha256=source_sha256
                    )

    def test_retained_capability_returns_credit_before_result_download(self) -> None:
        result_zip = self._zip([("document/result.txt", b"retained")])
        artifact_sha256 = hashlib.sha256(result_zip).hexdigest()
        owner = hashlib.sha256(
            f"task-1\0{artifact_sha256}\0{len(result_zip)}".encode()
        ).hexdigest()
        result_gets = 0
        idempotency_key = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal result_gets
            if request.url.path.startswith("/tasks/by-idempotency/"):
                return httpx.Response(404)
            if request.method == "POST" and request.url.path == "/tasks":
                return httpx.Response(
                    202,
                    json={
                        "task_id": "task-1",
                        "status_url": "/tasks/task-1",
                        "result_url": "/tasks/task-1/result",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "idempotency_key": idempotency_key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                    },
                )
            if request.method == "POST" and request.url.path.endswith("/lease"):
                return httpx.Response(200, json={
                    "schema": "mineru-task-protocol.v2", "task_id": "task-1",
                    "lease_until_unix": 9_999_999_999.0,
                })
            if request.url.path == "/tasks/task-1":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "protocol_state": "completed",
                        "idempotency_key": idempotency_key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                        "result_artifact_schema": "mineru-retained-result.v1",
                        "result_artifact_sha256": artifact_sha256,
                        "result_artifact_bytes": len(result_zip),
                        "result_artifact_owner": owner,
                    },
                )
            result_gets += 1
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/zip",
                    "x-mineru-result-sha256": artifact_sha256,
                    "x-mineru-result-owner": owner,
                },
                content=result_zip,
            )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            idempotency_key = _expected_idempotency_key(
                source_sha256, "attempt-1", "fence-1", 1_000_000
            )
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                reader=_Reader(),
                transport=httpx.MockTransport(handler),
            )  # type: ignore[arg-type]
            handle = parser.begin_remote_parse(
                options=ParserOptions(
                    runtime_bundle_identity_sha256="sha256:" + "a" * 64
                ),
                prepared_submission=self._prepared_submission(
                    parser, source, source_sha256
                ),
            )
            receipt = handle.wait_terminal()
            self.assertEqual(result_gets, 0)
            prepared = handle.prepare_materialization(
                receipt=receipt, source_pdf_sha256=source_sha256
            )
            handle.materialize_prepared(
                prepared=prepared, receipt=receipt,
                output_dir=Path(directory) / "out",
                source_pdf_sha256=source_sha256,
                parser_target_identity_sha256="sha256:" + "c" * 64,
                producer_claim_generation=1,
            )
            self.assertEqual(result_gets, 1)

    def test_wait_and_cancel_share_exactly_one_retained_receipt(self) -> None:
        result_zip = self._zip([("result.txt", b"once")])
        result_gets = 0
        idempotency_key = ""
        artifact_sha256 = hashlib.sha256(result_zip).hexdigest()
        owner = hashlib.sha256(
            f"task-1\0{artifact_sha256}\0{len(result_zip)}".encode()
        ).hexdigest()

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal result_gets
            if request.url.path.startswith("/tasks/by-idempotency/"):
                return httpx.Response(404)
            if request.method == "POST" and request.url.path == "/tasks":
                return httpx.Response(
                    202,
                    json={
                        "task_id": "task-1",
                        "status_url": "/tasks/task-1",
                        "result_url": "/tasks/task-1/result",
                        "task_protocol_schema": "mineru-task-protocol.v2",
                        "idempotency_key": idempotency_key,
                        "attempt_identity": "attempt-1",
                        "fence_identity": "fence-1",
                    },
                )
            if request.method == "POST" and request.url.path.endswith("/lease"):
                return httpx.Response(200, json={
                    "schema": "mineru-task-protocol.v2", "task_id": "task-1",
                    "lease_until_unix": 9_999_999_999.0,
                })
            if request.url.path == "/tasks/task-1":
                return httpx.Response(200, json={
                    "status": "completed",
                    "task_protocol_schema": "mineru-task-protocol.v2",
                    "protocol_state": "completed",
                    "idempotency_key": idempotency_key,
                    "attempt_identity": "attempt-1",
                    "fence_identity": "fence-1",
                    "result_artifact_schema": "mineru-retained-result.v1",
                    "result_artifact_sha256": artifact_sha256,
                    "result_artifact_bytes": len(result_zip),
                    "result_artifact_owner": owner,
                })
            result_gets += 1
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/zip",
                    "x-mineru-result-sha256": artifact_sha256,
                    "x-mineru-result-owner": owner,
                },
                content=result_zip,
            )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.write_bytes(b"%PDF-stage")
            source_sha256 = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            idempotency_key = _expected_idempotency_key(
                source_sha256, "attempt-1", "fence-1", 1_000_000
            )
            parser = MinerUHttpStagedParser(
                api_url="http://mineru.test:30000",
                server_url="http://vlm.test:30000/v1",
                spool_root=Path(directory) / "spool",
                transport=httpx.MockTransport(handler),
            )
            handle = parser.begin_remote_parse(
                options=PINNED_OPTIONS,
                prepared_submission=self._prepared_submission(
                    parser, source, source_sha256
                ),
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                wait_future = pool.submit(handle.wait_terminal)
                drain_future = pool.submit(handle.cancel_and_drain)
                receipt = wait_future.result()
                drain_future.result()
            self.assertGreater(receipt.artifact_byte_count, 0)
            self.assertEqual(result_gets, 0)

    def test_unsafe_zip_fails_without_partial_output(self) -> None:
        for result in (
            self._zip([("../escape", b"bad")]),
            self._zip([("link", b"target")], symlink=True),
        ):
            with (
                self.subTest(kind=result[:12]),
                tempfile.TemporaryDirectory() as directory,
            ):
                parser, source, source_sha256, _ = self._completed_parser(
                    directory, result
                )
                handle = parser.begin_remote_parse(
                    options=PINNED_OPTIONS,
                    prepared_submission=self._prepared_submission(
                        parser, source, source_sha256
                    ),
                )
                receipt = handle.wait_terminal()
                output = Path(directory) / "out"
                with self.assertRaisesRegex(ParserOutputContractError, "unsafe ZIP"):
                    prepared = handle.prepare_materialization(
                        receipt=receipt, source_pdf_sha256=source_sha256
                    )
                    handle.materialize_prepared(
                        prepared=prepared, receipt=receipt,
                        output_dir=output,
                        source_pdf_sha256=source_sha256,
                        parser_target_identity_sha256="sha256:" + "c" * 64,
                        producer_claim_generation=1,
                    )
                self.assertFalse(output.exists())
                self.assertEqual(
                    list((Path(directory) / "spool").glob(".retained-*.zip")), []
                )

    def test_receipt_rejects_non_hex_source_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical sha256"):
            RemoteArtifactReceipt(
                attempt_identity="a",
                fence_identity="f",
                artifact_owner_identity="o",
                artifact_byte_count=1,
                source_pdf_sha256="sha256:" + "Z" * 64,
            )


if __name__ == "__main__":
    unittest.main()
