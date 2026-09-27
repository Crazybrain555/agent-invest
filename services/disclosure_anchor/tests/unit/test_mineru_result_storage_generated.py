"""The generated API and parser writer under capacity config v2 (storage mode).

Synthetic uploads and parser files only; see the fixture for the exact seams.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from disclosure_anchor.application.contracts.mineru_capacity_config import retained_zip_upper_bound
from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol
from tests._mineru_capacity_lifecycle_fixture import BoundaryHTTPException, Upload
from tests._mineru_result_storage_fixture import (
    MIB,
    HoldRouteRequest,
    ResultStorageApiFixture,
    generated_granted_writer,
)


class StorageApiCase(unittest.IsolatedAsyncioTestCase):
    def fixture(self, **options: object) -> ResultStorageApiFixture:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = ResultStorageApiFixture(Path(temporary.name), **options)  # type: ignore[arg-type]
        self.addCleanup(fixture.close)
        self.addAsyncCleanup(fixture.dispose_test_tasks)
        return fixture

    async def terminal_or_held(self, fixture: ResultStorageApiFixture, task_id: str) -> str:
        async def poll() -> str:
            while True:
                record = fixture.manager.task_protocol_v2.get_by_task_id(task_id)
                if record.state in {"completed", "failed"}:
                    return record.state
                if record.storage is not None and record.storage["wait_reason"] in protocol.STORAGE_BLOCKED_REASONS:
                    return "held"
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(poll(), 10)


class GeneratedStorageTaskTests(StorageApiCase):
    async def test_accepted_task_completes_with_its_storage_envelope_and_health(self) -> None:
        fixture = self.fixture()
        await fixture.manager.start()
        task = await fixture.create(fixture.options("one"))
        self.assertEqual(await self.terminal_or_held(fixture, task.task_id), "completed")
        route = fixture.manager.get(task.task_id)
        payload = fixture.manager.build_status_payload(route, fixture.request())
        self.assertEqual(payload["status"], "completed")
        storage = payload["storage"]
        self.assertEqual(storage["schema"], "mineru.task-storage-status.v1")
        self.assertEqual(storage["policy_sha256"], fixture.policy.sha256)
        self.assertEqual((storage["phase"], storage["blocked"]), ("zip_sealed", False))
        self.assertEqual(storage["zip_bytes"], payload["result_artifact_bytes"])
        result = Path(route.result_artifact_path)
        raw = result.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), payload["result_artifact_sha256"])
        self.assertEqual(
            payload["result_artifact_owner"],
            hashlib.sha256(f"{task.task_id}\0{hashlib.sha256(raw).hexdigest()}\0{len(raw)}".encode()).hexdigest(),
        )
        # Model output was requested but never written: only existing files pack.
        self.assertEqual(storage["member_count"], 3)
        health = await fixture.module.health_check()
        self.assertEqual(health["task_protocol_runtime"]["schema"], "mineru-task-runtime.v4")
        self.assertEqual(health["task_protocol_runtime"]["result_storage_policy_sha256"], fixture.policy.sha256)
        observation = health["capacity_observation"]
        self.assertEqual(observation["schema"], "mineru.capacity-observation.v2")
        self.assertNotIn("result_reservation_bytes", observation["resolved_limits"])
        self.assertEqual(observation["result_storage"]["result_bytes"], fixture.policy.physical_charge(len(raw)))
        self.assertEqual(observation["result_storage"]["blocked_tasks"], 0)
        self.assertTrue(protocol.storage_managed_output_required())

    async def test_ingress_without_space_is_refused_before_any_upload_byte(self) -> None:
        fixture = self.fixture()
        await fixture.manager.start()
        with patch.dict(fixture.module.__dict__, {"live_free_bytes": lambda _path: 0}):
            with self.assertRaises(BoundaryHTTPException) as refused:
                await fixture.create(fixture.options("tight"))
        self.assertEqual(refused.exception.status_code, 429)
        self.assertEqual(refused.exception.detail, {"code": "storage_capacity_wait", "reason": "free_floor"})
        options = fixture.options("tight")
        self.assertIsNone(fixture.manager.task_protocol_v2.get(options.agent_idempotency_key))
        self.assertEqual(fixture.manager.task_protocol_v2.storage_usage()["ingress"], 0)
        self.assertEqual([p for p in (fixture.root / "output").iterdir() if not p.name.startswith(".")], [])
        with self.assertRaises(BoundaryHTTPException) as extra:
            await fixture.create(fixture.options("pair", uploads=[Upload(), Upload()]))
        self.assertEqual(extra.exception.status_code, 400)

    async def test_oversized_upload_is_cut_at_the_policy_limit(self) -> None:
        fixture = self.fixture()
        await fixture.manager.start()

        class Oversized(Upload):
            async def read(self, size):
                self.reads += 1
                return b"x" * MIB if self.reads <= 2 else b""

        with self.assertRaises(BoundaryHTTPException) as refused:
            await fixture.create(fixture.options("big", uploads=[Oversized()]))
        self.assertEqual(refused.exception.status_code, 413)
        self.assertEqual(fixture.manager.task_protocol_v2.storage_usage()["ingress"], 0)

    async def test_growth_past_the_permit_is_held_until_an_exact_operator_decision(self) -> None:
        big = {"{name}.md": b"#\n", "images/huge.png": b"\0" * (2 * MIB)}
        fixture = self.fixture(outputs=big, single_limit=2 * MIB)
        await fixture.manager.start()
        task = await fixture.create(fixture.options("huge"))
        self.assertEqual(await self.terminal_or_held(fixture, task.task_id), "held")
        huge = Path(task.output_dir) / "paper" / "auto" / "images" / "huge.png"
        self.assertFalse(huge.exists())
        route = fixture.manager.get(task.task_id)
        payload = fixture.manager.build_status_payload(route, fixture.request())
        self.assertEqual(payload["status"], "processing")
        self.assertEqual(payload["storage"]["wait_reason"], "hard_envelope_exceeded")
        self.assertTrue(payload["storage"]["blocked"])
        # Outside the hard envelope the task is held with its bytes charged,
        # visibly, for an operator; nothing fails, reparses or evicts it.
        health = await fixture.module.health_check()
        self.assertEqual(health["capacity_observation"]["result_storage"]["blocked_tasks"], 1)
        held = fixture.manager.task_protocol_v2.get_by_task_id(task.task_id)
        self.assertEqual((held.state, held.error), ("processing", None))

        # The only exit is the operator route. Without the enrolled operator
        # credential no body byte is read and the manager is never reached.
        manager = fixture.manager
        route = fixture.hold_route()
        credential = "synthetic-operator-credential-for-tests-only"
        with patch.object(manager, "storage_hold_decision", side_effect=AssertionError("reached the manager")):
            disabled = HoldRouteRequest.json({"mode": "preview"}, credential=credential)
            with self.assertRaises(BoundaryHTTPException) as refused:
                await route(task.task_id, disabled)
            self.assertEqual((refused.exception.status_code, refused.exception.detail, disabled.read),
                             (403, {"code": "storage_hold_operator_disabled"}, 0))
            fixture.enroll_operator(credential)
            for label, request in (
                ("no credential", HoldRouteRequest.json({"mode": "preview"})),
                ("another scheme", HoldRouteRequest(b'{"mode":"preview"}',
                                                    headers={"Authorization": f"Basic {credential}"})),
                ("wrong credential", HoldRouteRequest.json({"mode": "preview"}, credential=credential + "-")),
            ):
                with self.subTest(label), self.assertRaises(BoundaryHTTPException) as refused:
                    await route(task.task_id, request)
                self.assertEqual(
                    (refused.exception.status_code, refused.exception.detail, refused.exception.headers,
                     request.read),
                    (401, {"code": "storage_hold_operator_unauthorized"}, {"WWW-Authenticate": "Bearer"}, 0),
                )
            # Authorized, the small body bound holds while reading, not after.
            authorized = {"Authorization": f"Bearer {credential}"}
            declared = HoldRouteRequest(b"{}", headers={**authorized, "Content-Length": "16385"})
            streamed = HoldRouteRequest(b" " * (64 * 1024), headers=authorized, chunk=4096)
            for request, most in ((declared, 0), (streamed, 16384 + 4096)):
                with self.assertRaises(BoundaryHTTPException) as too_large:
                    await route(task.task_id, request)
                self.assertEqual(too_large.exception.status_code, 413)
                self.assertLessEqual(request.read, most)
        self.assertEqual(manager.task_protocol_v2.get_by_task_id(task.task_id), held)
        for body in ({"mode": "resume"}, {"mode": "preview", "extra": 1}, ["preview"]):
            with self.subTest(body=body), self.assertRaises(BoundaryHTTPException) as invalid:
                await route(task.task_id, HoldRouteRequest.json(body, credential=credential))
            self.assertEqual(invalid.exception.status_code, 400)
        preview = await route(task.task_id, HoldRouteRequest.json({"mode": "preview"}, credential=credential))
        self.assertEqual(
            (preview["hold_reason"], preview["runtime_identity_sha256"], preview["storage"]["policy_sha256"]),
            ("hard_envelope_exceeded", manager.capacity_config.sha256, fixture.policy.sha256),
        )
        decision = {
            "mode": "execute", "expected_preview_sha256": preview["preview_sha256"],
            "decided_by": "root", "reason": "declared envelope stays", "fixed_by": "no fix; terminal",
        }
        manager._scheduled_task_ids.add(task.task_id)
        with self.assertRaises(BoundaryHTTPException) as in_flight:
            await route(task.task_id, HoldRouteRequest.json(decision, credential=credential))
        self.assertEqual(in_flight.exception.status_code, 409)
        manager._scheduled_task_ids.discard(task.task_id)
        receipt = await route(task.task_id, HoldRouteRequest.json(decision, credential=credential))
        self.assertEqual((receipt["state"], receipt["replayed"]), ("failed", False))
        self.assertTrue((await route(task.task_id, HoldRouteRequest.json(decision, credential=credential)))["replayed"])
        payload = fixture.manager.build_status_payload(manager.get(task.task_id), fixture.request())
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(
            (payload["failure_cause"]["code"], payload["failure_cause"]["decision_sha256"],
             payload["failure_cause"]["decision"]),
            ("storage_hold_terminated", receipt["decision_sha256"], receipt["decision"]),
        )
        self.assertEqual(
            (receipt["decision"]["decided_by"], receipt["decision"]["reason"]), ("root", "declared envelope stays"),
        )
        health = await fixture.module.health_check()
        self.assertEqual(health["capacity_observation"]["result_storage"]["blocked_tasks"], 0)
        self.assertTrue(Path(task.output_dir).is_dir())  # Kept until the ordinary ACK.
        await fixture.module.ack_async_task_result(task.task_id)
        self.assertEqual(manager.task_protocol_v2.get_by_task_id(task.task_id).state, "consumed")
        self.assertFalse(Path(task.output_dir).exists())


    async def test_no_writer_is_ever_rescheduled_for_a_held_task(self) -> None:
        """The decision's in-flight check needs no extra lock: nothing re-admits a held producer.

        The refill is the only scheduling site and admits pending tasks only;
        restart recovery hydrates a held record as processing and never replays it.
        """
        big = {"{name}.md": b"#\n", "images/huge.png": b"\0" * (2 * MIB)}
        fixture = self.fixture(outputs=big, single_limit=2 * MIB)
        await fixture.manager.start()
        task = await fixture.create(fixture.options("held-writer"))
        self.assertEqual(await self.terminal_or_held(fixture, task.task_id), "held")
        manager = fixture.manager

        async def producer_gone() -> None:
            while task.task_id in manager._scheduled_task_ids:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(producer_gone(), 5)
        manager._refill_pending_queue()
        self.assertNotIn(task.task_id, manager._scheduled_task_ids)
        recovered = {item["task_id"]: item["status"] for item in manager.task_protocol_v2.recoverable_payloads()}
        self.assertEqual(recovered[task.task_id], "processing")
        record = manager.task_protocol_v2.get_by_task_id(task.task_id)
        self.assertEqual((record.state, record.storage["wait_reason"]), ("processing", "hard_envelope_exceeded"))
        # One scheduling site in the whole generated API, and it admits pending only.
        sites = [index for index, line in enumerate(fixture.generated.splitlines())
                 if "_scheduled_task_ids.add(" in line]
        self.assertEqual(len(sites), 1)
        guard = "\n".join(fixture.generated.splitlines()[sites[0] - 6:sites[0]])
        self.assertIn("if (task.status == TASK_PENDING", guard)


class PreBodyIngressTests(StorageApiCase):
    """The actual generated request middleware in front of a synthetic route."""

    async def post(self, fixture: ResultStorageApiFixture, *, declared: int | None, body: bytes,
                   route=None):  # type: ignore[no-untyped-def]
        chunks = [body[index:index + 4096] for index in range(0, len(body), 4096)] or [b""]
        pending = [{"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
                   for index, chunk in enumerate(chunks)]
        reads: list[int] = []
        routed: list[bool] = []
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            reads.append(1)
            return pending.pop(0) if pending else {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        async def default_route(scope, receive, send):  # type: ignore[no-untyped-def]
            routed.append(True)
            while (await receive()).get("more_body"):
                pass
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        headers = [] if declared is None else [(b"content-length", str(declared).encode())]
        scope = {"type": "http", "method": "POST", "path": "/tasks", "headers": headers}
        await fixture.module._ServiceRequestMiddleware(route or default_route)(scope, receive, send)
        start = next(message for message in sent if message["type"] == "http.response.start")
        raw = b"".join(message.get("body", b"") for message in sent  # type: ignore[misc]
                       if message["type"] == "http.response.body")
        return start["status"], (json.loads(raw) if raw else None), len(reads), bool(routed)

    async def test_unbounded_or_oversized_body_is_refused_before_it_is_read(self) -> None:
        fixture = self.fixture()
        await fixture.manager.start()
        form_bytes = fixture.module._ServiceRequestMiddleware._STORAGE_INGRESS_FORM_BYTES
        for declared, status, code in (
            (None, 411, "content_length_required"),
            (fixture.policy.source_pdf_bytes_limit + form_bytes + 1, 413, "source_upload_too_large"),
        ):
            with self.subTest(declared=declared):
                outcome = await self.post(fixture, declared=declared, body=b"x")
                self.assertEqual(outcome, (status, {"detail": {"code": code}}, 0, False))
        self.assertEqual(fixture.manager.task_protocol_v2.storage_usage()["ingress"], 0)

    async def test_body_without_space_waits_before_any_byte_is_read(self) -> None:
        fixture = self.fixture()
        await fixture.manager.start()
        with patch.dict(fixture.module.__dict__, {"live_free_bytes": lambda _path: 0}):
            outcome = await self.post(fixture, declared=1000, body=b"x" * 1000)
        self.assertEqual(
            outcome, (429, {"detail": {"code": "storage_capacity_wait", "reason": "free_floor"}}, 0, False),
        )
        self.assertEqual(fixture.manager.task_protocol_v2.storage_usage()["ingress"], 0)

    async def test_admitted_body_moves_its_upload_share_to_the_key(self) -> None:
        fixture = self.fixture()
        await fixture.manager.start()
        registry = fixture.manager.task_protocol_v2
        charge = fixture.policy.physical_charge(1000)
        observed: list[int] = []

        async def route(scope, receive, send):  # type: ignore[no-untyped-def]
            observed.append(registry.storage_usage()["ingress"])  # Spool + upload, before any read.
            while (await receive()).get("more_body"):
                pass
            await fixture.manager._reserve_ingress_storage("key-body", [Upload()])
            observed.append(registry.storage_usage()["ingress"])
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        status, _, reads, _ = await self.post(fixture, declared=1000, body=b"x" * 1000, route=route)
        self.assertEqual((status, reads), (202, 1))
        self.assertEqual(observed, [2 * charge, 2 * charge])
        # The spool share ended with the request; the upload share is the key's.
        self.assertEqual(registry.storage_usage()["ingress"], charge)
        registry.release_ingress_storage("key-body")
        self.assertEqual(registry.storage_usage()["ingress"], 0)

    async def test_body_longer_than_declared_is_cut_and_its_charge_released(self) -> None:
        fixture = self.fixture()
        await fixture.manager.start()
        with self.assertRaises(BoundaryHTTPException) as cut:
            await self.post(fixture, declared=10, body=b"x" * 11)
        self.assertEqual(cut.exception.status_code, 413)
        self.assertEqual(fixture.manager.task_protocol_v2.storage_usage()["ingress"], 0)

    def test_framework_spool_lands_on_the_output_volume_or_binding_fails(self) -> None:
        import starlette.formparsers as multipart_forms

        fixture = self.fixture()
        spool_dir = str(fixture.root / "output" / ".agent-ingress-spool")
        spool = multipart_forms.SpooledTemporaryFile(max_size=1)
        self.addCleanup(spool.close)
        self.assertEqual(spool._TemporaryFileArgs["dir"], spool_dir)  # type: ignore[attr-defined]
        self.assertTrue(Path(spool_dir).is_dir())
        bind = fixture.module.AsyncTaskManager._bind_result_storage_process
        bind(fixture.policy)  # Rebinding the same volume is idempotent.
        with patch.object(multipart_forms, "SpooledTemporaryFile", object), self.assertRaises(RuntimeError):
            bind(fixture.policy)


class StorageOutputQuiescenceTests(StorageApiCase):
    """The installer's output witness across the actual storage-managed startup.

    Installer, rollback and collector read ``inspect_quiescent_output_root``
    before and after replacing the API; a storage-managed startup adds its
    ingress spool to the output root before any task arrives.
    """

    async def test_startup_spool_and_a_consumed_task_leave_a_quiescent_output_root(self) -> None:
        fixture = self.fixture()
        root = fixture.root / "output"
        # The actual startup created only its empty ingress spool.
        self.assertEqual([item.name for item in root.iterdir()], [".agent-ingress-spool"])
        fresh = protocol.inspect_quiescent_output_root(root, allow_empty=True)
        self.assertEqual((fresh["file_count"], fresh["total_bytes"]), (0, 0))
        self.assertIsNone(fresh["quiescence"]["registry_sha256"])
        with self.assertRaisesRegex(protocol.TaskProtocolConflict, "commissioned task registry is absent"):
            protocol.inspect_quiescent_output_root(root)
        await fixture.manager.start()
        task = await fixture.create(fixture.options("one"))
        self.assertEqual(await self.terminal_or_held(fixture, task.task_id), "completed")
        with self.assertRaises(protocol.TaskProtocolConflict):
            protocol.inspect_quiescent_output_root(root)  # The unacknowledged task root is retained.
        await fixture.module.ack_async_task_result(task.task_id)
        self.assertEqual(fixture.manager.task_protocol_v2.get_by_task_id(task.task_id).state, "consumed")
        raw = (root / ".agent-task-protocol-v2" / "registry.json").read_bytes()
        proof = protocol.inspect_quiescent_output_root(root)
        self.assertEqual(proof["quiescence"]["registry_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
        self.assertEqual((proof["file_count"], proof["total_bytes"], proof["quiescence"]["record_count"]),
                         (1, len(raw), 1))
        self.assertEqual(protocol.inspect_quiescent_output_root(root, allow_empty=True), proof)
        self.assertEqual(sorted(item.name for item in root.iterdir()),
                         [".agent-ingress-spool", ".agent-task-protocol-v2"])

    async def test_storage_managed_startup_keeps_the_deployed_v3_witness(self) -> None:
        import starlette.formparsers as multipart_forms

        fixture = self.fixture()
        root = fixture.root / "deployed"
        root.mkdir()
        path = root / ".agent-task-protocol-v2" / "registry.json"
        # A capacity-v1 (registry v3) process left one consumed tombstone here.
        key = f"{fixture.epoch:x}.{hashlib.sha256(b'v3 tombstone').hexdigest()}"
        legacy = protocol.DurableTaskRegistry(path, output_root=root, max_unacked_result_bytes=1024)
        legacy._records[key] = protocol.DurableTaskRecord(
            key, "task-v3", "attempt", "fence", state="consumed", consumed_at_unix=float(fixture.epoch),
            result_sha256="a" * 64, result_owner="b" * 64, result_bytes=12,
        )
        legacy._submission_watermark_bucket = fixture.epoch
        legacy._persist()
        raw = path.read_bytes()
        # Preflight form against the old API: the candidate source with allow_empty.
        before = protocol.inspect_quiescent_output_root(root, allow_empty=True)
        with patch.dict(os.environ, {"MINERU_API_OUTPUT_ROOT": str(root)}), patch.object(
            multipart_forms, "SpooledTemporaryFile", tempfile.SpooledTemporaryFile,
        ):
            manager = fixture.module.AsyncTaskManager(fixture.app)  # The actual replacement startup.
        try:
            self.assertEqual(sorted(item.name for item in root.iterdir()),
                             [".agent-ingress-spool", ".agent-task-protocol-v2"])
            self.assertEqual(manager.task_protocol_v2.get(key).state, "consumed")
            self.assertEqual(path.read_bytes(), raw)  # Startup never rewrites the v3 registry.
            # Post-deployment (installed module, collector) and rollback-witness forms
            # both equal the preflight proof, so the rollback comparison holds.
            self.assertEqual(protocol.inspect_quiescent_output_root(root), before)
            self.assertEqual(protocol.inspect_quiescent_output_root(root, allow_empty=True), before)
            self.assertEqual((before["file_count"], before["quiescence"]["record_count"]), (1, 1))
        finally:
            await asyncio.wait_for(manager.service_io.close(), 2)


class GeneratedWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        latch = patch.object(protocol, "_STORAGE_MANAGED_OUTPUT", False)
        latch.start()
        self.addCleanup(latch.stop)
        self.writer_class = generated_granted_writer()

    def test_writer_charges_before_open_and_fails_closed_without_a_permit(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            self.writer_class(str(root / "legacy")).write("a.txt", b"plain")  # Unmanaged process.
            self.assertEqual((root / "legacy/a.txt").read_bytes(), b"plain")
            protocol.require_storage_managed_output()
            with self.assertRaises(RuntimeError):
                self.writer_class(str(root / "managed")).write("a.txt", b"x")
            self.assertFalse((root / "managed").exists())
            (root / "task").mkdir()
            permit = protocol.SourceGrowthPermit(
                root=root / "task", limit_bytes=16 * (4096 + 4096), allocation_unit=4096, file_overhead=4096,
            )
            with protocol.bind_source_growth_permit(permit):
                writer = self.writer_class(str(root / "task/doc/auto"))
                writer.write_string("doc.md", "# heading\n")
                with self.assertRaises(protocol.SourceGrowthLimitExceeded):
                    writer.write("images/big.png", b"\0" * (8 * 4096))
            self.assertEqual((root / "task/doc/auto/doc.md").read_text(), "# heading\n")
            self.assertFalse((root / "task/doc/auto/images/big.png").exists())
            self.assertTrue(permit.tripped)


class RetainedZipIdentityTests(StorageApiCase):
    async def test_storage_zip_bytes_equal_the_legacy_retained_writer(self) -> None:
        fixture = self.fixture()
        module = fixture.module
        root = fixture.root / "output" / "task-zip"
        parse_dir = root / "paper" / "auto"
        files = {
            "paper.md": b"# paper\n" * 40,
            "paper_middle.json": json.dumps({"pdf_info": [1, 2, 3]}).encode(),
            "paper_model.json": b"[]",
            "paper_content_list.json": b"[{\"type\":\"text\"}]",
            "paper_content_list_v2.json": b"[]",
            "images/b.png": bytes(range(256)) * 8,
            "images/a.jpg": b"\xff\xd8jpeg" * 99,
            "images/skip.txt": b"not an image",
            "paper_origin.pdf": b"%PDF-origin",
        }
        for relative, data in files.items():
            target = parse_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (root / "uploads").mkdir()
        task = module.AsyncParseTask(
            task_id="task-zip", status="completed", backend="hybrid-http-client", file_names=["paper"],
            created_at="2026-09-27T00:00:00+00:00", output_dir=str(root), effort="medium",
            parse_method="auto", lang_list=["ch"], formula_enable=True, table_enable=True,
            image_analysis=False, server_url=None, return_md=True, return_middle_json=True,
            return_model_output=True, return_content_list=True, return_images=True,
            response_format_zip=True, return_original_file=True, client_side_output_generation=False,
            start_page_id=0, end_page_id=99999, upload_names=["paper.pdf"], uploads=[],
        )
        budget, observations = module._retained_result_sources(task, byte_budget=64 * MIB)
        legacy = fixture.root / "legacy.zip"
        module._write_retained_zip_from_fds(observations, str(legacy), budget)
        module._verify_and_close_result_sources(observations)
        selections = module.AsyncTaskManager._result_storage_selections(task)
        metadata = os.stat(root)
        identity = {"device": metadata.st_dev, "inode": metadata.st_ino,
                    "uid": metadata.st_uid, "mode": metadata.st_mode}
        inventory = protocol.build_result_inventory(
            task_id="task-zip", task_root=root, root_identity=identity, selections=selections,
            policy=fixture.policy, zip_upper_bound=retained_zip_upper_bound,
        )
        path, digest, size = protocol.write_retained_zip(
            task_root=root, root_identity=identity, inventory=inventory, selections=selections,
            grant_bytes=inventory.zip_upper_bound_bytes,
        )
        self.assertEqual(path.read_bytes(), legacy.read_bytes())
        self.assertEqual(size, legacy.stat().st_size)
        self.assertEqual(digest, hashlib.sha256(legacy.read_bytes()).hexdigest())
        self.assertLessEqual(size, inventory.zip_upper_bound_bytes)
        self.assertEqual(len(inventory.members), 8)


if __name__ == "__main__":
    unittest.main()
