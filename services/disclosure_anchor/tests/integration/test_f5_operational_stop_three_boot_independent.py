"""Independent F5 acceptance, group (2): one scratch-PG three-boot case.

Pro §7 (2): on the real resident staged path (production builder, real
coordinator, durable backend/persistence, atomic publisher, real semantic
router, executor and file cache) with only the remote parser and the model
adapter faked:

* boot 0 publishes a neighbor document normally;
* boot 1 materializes the target, adjudicates two semantic groups, and then a
  third group fails closed: the worker latches a public stop and persists it;
  the attempt stays ``local_materialized`` with its run, output and cache, no
  publication/cleanup/ACK happens for it, and the neighbor stays published;
* boot 2 (same DB and runtime) is refused at every start gate with zero model
  calls, zero provider HTTP and zero row changes;
* after the fix, an explicit hash-bound release under the worker singleton,
  boot 3 resumes the SAME attempt/run/materialization, reuses the two cached
  groups, calls the model only for the formerly failed group, and publishes,
  cleans up and ACKs exactly once.

Run only through the repository scratch runner (see the module docstring of
``tests/integration/_runner.py``); never with ``--real-mineru``.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, redirect_stderr
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch
import zipfile

import httpx
import pypdfium2 as pdfium
import sqlalchemy as sa

from disclosure_anchor.adapters.db.postgres.staged_recovery_scope_v4 import inspect_accepted_recovery_scope
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import MinerUHttpRemoteV4
from disclosure_anchor.adapters.runtime import staged_worker_v4
from disclosure_anchor.adapters.runtime.worker_stop_control import (
    RuntimeWorkerStopControl,
    WorkerControlStore,
    plan_release,
    release_worker_circuit,
)
from disclosure_anchor.adapters.semantics.runtime import SemanticRuntime
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.semantic_route_store import (
    SemanticRouteGroupFileCache,
    SemanticRouteReceiptStore,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    SemanticAdjudicatedRoute,
    SemanticAdjudicationDecision,
    SemanticDocumentContext,
)
from disclosure_anchor.application.ports.semantic_routes import SemanticRouteAdjudicatorError
from disclosure_anchor.application.ports.worker_stop_control import WorkerOperationalStopError
from disclosure_anchor.application.services.provider_unit_builder import build_provider_units
from disclosure_anchor.application.services.semantic_adjudication import (
    ConfiguredSemanticProvider,
    OrderedSemanticAdjudicationExecutor,
)
from disclosure_anchor.application.services.semantic_router import SemanticRouter
from disclosure_anchor.application.services.semantic_taxonomy import load_semantic_route_taxonomy
from disclosure_anchor.adapters.storage.provider_document_source import ProviderDocumentFileSource
from disclosure_anchor.cli import worker as worker_cli
from disclosure_anchor.domain import ids
from disclosure_anchor.settings import Settings, load_settings
from tests.integration._support import engine_or_skip
from tests.integration.test_staged_v4_end_to_end import _FakeMinerU, _official_result_zip
from tests.unit.test_mineru_medium_artifacts import _write_bundle
from tests.unit.test_mineru_process_profile import _profile
from tests.unit.test_provider_unit_builder import _admitted
from tests.unit.test_semantic_execution_guard import _Provider
from tests.unit.test_settings import _env, _mineru_topology


# Three performance-forecast sections whose units each need one bounded model
# adjudication under the packaged taxonomy (checked DB-free below).
_SECTIONS = (
    ("一、经营情况说明", "本期业绩变动原因以及预计业绩区间的说明"),
    ("二、其他说明", "业绩变动原因与业绩预告期间"),
    ("三、补充说明", "业绩变动原因以及预计业绩区间的补充说明"),
)
_FAILING_MARK = "补充说明"
_TITLE = "某公司业绩预告"
_FILING_TYPE = "performance_forecast"
_ROUTE = "performance_forecast_basis"
_SECRET = "sk-live-F5SCRATCH"


def _sectioned_result_zip(source_pdf_sha256: str) -> bytes:
    """The existing fake official bundle with the three sections as its text."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_sections(root, source_pdf_sha256)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        return output.getvalue()


def _write_sections(root: Path, source_pdf_sha256: str) -> None:
    _write_bundle(root)
    (root / "images" / "owner.jpg").write_bytes(b"\xff\xd8\xffowner-crop")
    (root / "images" / "continuation.jpg").write_bytes(b"\xff\xd8\xffcontinuation-crop")
    content_list = next(root.glob("*_content_list.json"))
    typed_file = next(root.glob("*_content_list_v2.json"))
    content = json.loads(content_list.read_text())
    typed = json.loads(typed_file.read_text())
    (first_title, first_body), *rest = _SECTIONS
    content[0].update(text=first_title, text_level=1)
    typed[0][0].update(type="title", level=1)
    lines = [(first_body, None)] + [item for title, body in rest for item in ((title, 1), (body, None))]
    top = 500
    for text, level in lines:
        bbox = [100, top, 900, top + 40]
        entry: dict[str, object] = {"type": "text", "page_idx": 1, "bbox": bbox, "text": text}
        if level is None:
            typed[1].append({"type": "paragraph", "bbox": bbox})
        else:
            entry["text_level"] = level
            typed[1].append({"type": "title", "bbox": bbox, "level": level})
        content.append(entry)
        top += 50
    content_list.write_text(json.dumps(content, ensure_ascii=False))
    typed_file.write_text(json.dumps(typed, ensure_ascii=False))
    stem = content_list.name.removesuffix("_content_list.json")
    source_stem = source_pdf_sha256.replace("sha256:", "sha256_", 1)
    for path in tuple(root.iterdir()):
        if path.is_file() and path.name.startswith(stem):
            path.rename(path.with_name(source_stem + path.name[len(stem):]))


def _decide(failing: list[bool]) -> Callable[[Any], tuple[SemanticAdjudicationDecision, ...]]:
    """The fake model: route every Unit to one candidate; fail the marked group while broken."""

    def decide(batch: Any) -> tuple[SemanticAdjudicationDecision, ...]:
        for unit in batch.units:
            if failing[0] and any(_FAILING_MARK in source.text for source in unit.sources):
                raise SemanticRouteAdjudicatorError(
                    f"model output attempted a forbidden tool call ({_SECRET})",
                    reason_code="forbidden_tool_call",
                    retryable=False,
                )
        return tuple(
            SemanticAdjudicationDecision(
                unit_index=unit.unit_index,
                routes=(SemanticAdjudicatedRoute(
                    key=_ROUTE,
                    support_ids=next(item.source_ids for item in unit.candidates if item.key == _ROUTE),
                ),),
            )
            for unit in batch.units
        )

    return decide


def _no_model_expected(_batch: Any) -> tuple[SemanticAdjudicationDecision, ...]:
    raise AssertionError("the neighbor bundle must route without the model")


class ThreeBootFixturePreconditionTests(unittest.TestCase):
    """DB-free: the fixture really produces three model groups (no vacuous pass)."""

    def _route(self, write: Callable[[Path, str], None], context: SemanticDocumentContext,
               adapter: _Provider) -> Any:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, _env(root), clear=True):
                paths = FileStorePathBuilder(load_settings())
            # The shared unit-builder helper binds this exact artifact identity.
            source_sha = "sha256:" + "a" * 64
            relpath = Path("parser_artifacts/cninfo/000001/f5/run/sha256_" + "a" * 64 + "/hybrid_auto")
            bundle = paths.data_path(relpath)
            bundle.mkdir(parents=True)
            write(bundle, source_sha)
            admitted = _admitted(ProviderDocumentFileSource(
                paths, text_reader=lambda _path, *, document: (), page_counter=lambda _path: 2,
            ).rebuild_provider_document(relpath, source_pdf_sha256=source_sha))
            router = SemanticRouter(
                taxonomy=load_semantic_route_taxonomy(),
                executor=OrderedSemanticAdjudicationExecutor(
                    (ConfiguredSemanticProvider(adapter=adapter, cache=_MemoryGroupCache()),)),  # type: ignore[arg-type]
                batch_size=1,
            )
            return router.route(admitted=admitted, document=context, drafts=build_provider_units(admitted).units)

    def test_sections_yield_three_model_groups_under_the_packaged_taxonomy(self) -> None:
        context = SemanticDocumentContext(title=_TITLE, filing_type=_FILING_TYPE)
        adapter = _Provider("f5-scratch-model", decide=_decide([False]))
        result = self._route(_write_sections, context, adapter)
        self.assertEqual(adapter.calls, 3, "three model-adjudicated groups")
        self.assertEqual(len(result.adjudication_outcomes), 3)
        self.assertTrue(all(unit.semantic_keys == (_ROUTE,) for unit in result.units))
        failing = _Provider("f5-scratch-model", decide=_decide([True]))
        with self.assertRaises(SemanticRouteAdjudicatorError) as raised:
            self._route(_write_sections, context, failing)
        self.assertEqual(raised.exception.reason_code, "forbidden_tool_call")
        self.assertEqual(failing.calls, 3, "two groups succeed before the third fails")

    def test_neighbor_bundle_routes_without_the_model(self) -> None:
        def official(root: Path, source_sha: str) -> None:
            with zipfile.ZipFile(io.BytesIO(_official_result_zip(source_sha))) as archive:
                archive.extractall(root)

        silent = _Provider("f5-scratch-model", decide=_no_model_expected)
        self._route(official, SemanticDocumentContext(title=None, filing_type=None), silent)
        self.assertEqual(silent.calls, 0)


class _MemoryGroupCache:
    def __init__(self) -> None:
        self.entries: dict[str, Any] = {}

    def get(self, cache_key: str) -> Any:
        return self.entries.get(cache_key)

    def put(self, entry: Any) -> None:
        self.entries[entry.cache_key] = entry


class F5OperationalStopThreeBootIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.tempdir = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.root = Path(self.tempdir.name)
        self.runtime_root = self.root / "services" / "disclosure_anchor" / "runtime"
        self.runtime_root.mkdir(parents=True, mode=0o700)
        self.runtime_root.chmod(0o700)
        self.database_url = self.engine.url.render_as_string(hide_password=False)
        profile = replace(_profile(), api_task_slots=1, api_max_pending_tasks=1,
            cpu_worker_threads=3, omp_thread_count=1,
            registry_nonterminal_cap=1, registry_terminal_cap=127, processing_window_size=16,
            raster_stage_slots=1, layout_stage_slots=1, postprocess_stage_slots=1,
            native_owner_slots=1, requested_hybrid_batch_ratio=1, effective_hybrid_batch_ratio=1,
            finalizer_slots=1, gpu_request_slots=7)
        profile_path, keyring_path = self.root / "profile.json", self.root / "keyring.json"
        profile_path.write_bytes(profile.exact_bytes)
        keyring_path.write_text(json.dumps({"format": "disclosure-v4-secret-keyring.v1",
            "primary_kek_id": "fixture", "keks": {"fixture": "11" * 32}}))
        profile_path.chmod(0o600)
        keyring_path.chmod(0o600)
        self.environment = {
            **_env(self.root), **_mineru_topology(),
            "DATABASE_URL": self.database_url,
            "WORKER_PARSE_EXECUTION_MODE": "staged-v4",
            "WORKER_PARSE_CONCURRENCY": "1", "WORKER_FINALIZE_CONCURRENCY": "1",
            "DISCLOSURE_MINERU_RUNTIME_BUNDLE_IDENTITY_SHA256": profile.runtime_bundle_identity_sha256,
            "DISCLOSURE_V4_PROCESS_PROFILE_FILE": str(profile_path),
            "DISCLOSURE_V4_PROCESS_PROFILE_SHA256": profile.sha256,
            "DISCLOSURE_V4_SECRET_KEYRING_FILE": str(keyring_path),
            "DISCLOSURE_V4_ARCHIVE_MEMBER_COUNT_LIMIT": "8192",
        }
        self.settings = self._settings()
        self.paths = FileStorePathBuilder(self.settings)
        self.company_id = "co_" + ids.new_ulid()
        self.security_id = "sec_" + ids.new_ulid()
        self.document_ids: list[str] = []
        with self.engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO disclosure_core.company (company_id,legal_name) "
                                 "VALUES (:company,'F5 Three Boot Fixture')"), {"company": self.company_id})
            conn.execute(sa.text("INSERT INTO disclosure_core.security (security_id,company_id,security_code,"
                                 "exchange) VALUES (:security,:company,'000001','SZSE')"),
                         {"security": self.security_id, "company": self.company_id})
        self.neighbor = self._document(page_height=120, title=None, filing_type=None)
        self.target = self._document(page_height=100, title=_TITLE, filing_type=_FILING_TYPE)
        self.remotes: list[MinerUHttpRemoteV4] = []

    def tearDown(self) -> None:
        for remote in self.remotes:
            remote.close()
        try:
            with self.engine.begin() as conn:
                conn.exec_driver_sql("TRUNCATE TABLE disclosure_ops.remote_parse_attempt CASCADE")
                for document_id in self.document_ids:
                    for table in ("disclosure_ops.durable_publish_base", "disclosure_ops.outbox_event",
                                  "disclosure_core.document_unit"):
                        conn.execute(sa.text(f"DELETE FROM {table} WHERE document_id=:doc"), {"doc": document_id})
                    conn.execute(sa.text("UPDATE disclosure_core.document SET current_processing_run_id=NULL "
                                         "WHERE document_id=:doc"), {"doc": document_id})
                    conn.execute(sa.text("DELETE FROM disclosure_core.processing_run WHERE document_id=:doc"),
                                 {"doc": document_id})
                    conn.execute(sa.text("DELETE FROM disclosure_core.document WHERE document_id=:doc"),
                                 {"doc": document_id})
                conn.execute(sa.text("DELETE FROM disclosure_core.security WHERE security_id=:s"),
                             {"s": self.security_id})
                conn.execute(sa.text("DELETE FROM disclosure_core.company WHERE company_id=:c"),
                             {"c": self.company_id})
        finally:
            self.engine.dispose()
            self.tempdir.cleanup()

    # -- fixture ---------------------------------------------------------------

    def _settings(self) -> Settings:
        with patch.dict(os.environ, self.environment, clear=True):
            return load_settings()

    def _document(self, *, page_height: int, title: str | None, filing_type: str | None) -> SimpleNamespace:
        with pdfium.PdfDocument.new() as pdf:
            for _ in range(2):
                pdf.new_page(100, page_height).close()
            output = io.BytesIO()
            pdf.save(output)
        source = output.getvalue()
        sha = "sha256:" + hashlib.sha256(source).hexdigest()
        document_id = str(ids.new_document_id())
        provider_document_id = "f5-" + ids.new_ulid()
        relpath = self.paths.raw_document_relpath(provider="cninfo", security_code="000001", year=2026,
                                                  provider_document_id=provider_document_id, raw_file_hash=sha)
        path = self.paths.data_path(relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source)
        path.chmod(0o600)
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO disclosure_core.document (document_id,security_id,provider,provider_document_id,"
                "raw_file_relpath,raw_file_hash,status,title,class_filing_type) VALUES (:doc,:sec,'cninfo',"
                ":provider_document,:relpath,:sha,'registered',:title,:filing_type)"),
                {"doc": document_id, "sec": self.security_id, "provider_document": provider_document_id,
                 "relpath": relpath.as_posix(), "sha": sha, "title": title, "filing_type": filing_type})
        self.document_ids.append(document_id)
        return SimpleNamespace(document_id=document_id, source=source, sha=sha)

    def _fake_provider(self, document: SimpleNamespace, artifact: bytes) -> _FakeMinerU:
        # One fake provider per document; the boots that use them never overlap.
        fake = _FakeMinerU(attempt_id="pending", fence_identity="pending", client_submit_key="pending",
                           source_pdf=document.source, artifact=artifact)
        scratch = self.settings.disclosure_runtime_root / "staged_v4" / "scratch"
        uow = unit_of_work_factory(self.engine)

        def cleanup_probe() -> bool:
            with uow() as unit:
                authority = unit.remote_parse_v4.load(fake.attempt_id)
            intent = next(e.value for e in authority.evidence if e.kind == "materialization_intent")
            return authority.state == "ack_pending" and all(not (scratch / relpath).exists() for relpath in (
                authority.reservation.snapshot_relpath, intent.spool_relpath, intent.output_relpath))

        fake.cleanup_probe = cleanup_probe
        return fake

    def _transport(self, fake: _FakeMinerU) -> Callable[..., MinerUHttpRemoteV4]:
        def http(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/tasks":
                body = request.read()
                for field, attribute in (("agent_attempt_identity", "attempt_id"),
                                         ("agent_fence_identity", "fence_identity"),
                                         ("agent_idempotency_key", "client_submit_key")):
                    match = re.search(b'name="' + field.encode() + b'"\r\n\r\n([^\r\n]+)', body)
                    assert match is not None
                    setattr(fake, attribute, match.group(1).decode())
            response = fake(request)
            if request.url.path.endswith("/lease"):
                response = httpx.Response(200, json={**response.json(), "lease_until_unix": time.time() + 600})
            return response

        def make_remote(**kwargs: Any) -> MinerUHttpRemoteV4:
            remote = MinerUHttpRemoteV4(transport=httpx.MockTransport(http), **kwargs)
            self.remotes.append(remote)
            return remote

        return make_remote

    def _semantic_runtime(self, adapter: _Provider) -> Callable[..., SemanticRuntime]:
        """``build_semantic_runtime`` with only the provider adapter replaced."""

        def build(*, settings: Settings, paths: Any, artifacts: Any) -> SemanticRuntime:
            taxonomy = load_semantic_route_taxonomy()
            cache = SemanticRouteGroupFileCache(
                settings.disclosure_runtime_root / "cache" / "semantic_routes" / "v2" / taxonomy.version
                / adapter.provider_identity.provider_id)
            executor = OrderedSemanticAdjudicationExecutor(
                (ConfiguredSemanticProvider(adapter=adapter, cache=cache),),  # type: ignore[arg-type]
                policy_version=settings.disclosure_semantic_failover_policy,
            )
            return SemanticRuntime(
                router=SemanticRouter(taxonomy=taxonomy, executor=executor, batch_size=1),
                receipts=SemanticRouteReceiptStore(paths=paths, artifacts=artifacts),
            )

        return build

    def _boot(self, document: SimpleNamespace, fake: _FakeMinerU, adapter: _Provider,
              *, done: Callable[[], bool]) -> tuple[RuntimeWorkerStopControl, BaseException | None]:
        """One resident boot: production composition, admission scoped to one document."""

        settings = self._settings()
        control = RuntimeWorkerStopControl.for_settings(settings)
        real_builder = staged_worker_v4.build_staged_worker_v4_runtime
        deadline = time.monotonic() + 60

        def scoped_builder(**kwargs: Any) -> Any:
            return real_builder(**kwargs, admission_document_ids=(document.document_id,))

        def should_stop() -> bool:
            return control.is_tripped() or done() or time.monotonic() >= deadline

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, self.environment, clear=True))
            stack.enter_context(patch.object(staged_worker_v4, "MinerUHttpRemoteV4",
                                             side_effect=self._transport(fake)))
            stack.enter_context(patch.object(staged_worker_v4, "build_semantic_runtime",
                                             side_effect=self._semantic_runtime(adapter)))
            stack.enter_context(patch.object(staged_worker_v4, "build_staged_worker_v4_runtime",
                                             side_effect=scoped_builder))
            try:
                worker_cli._run_staged_v4_resident(
                    settings, engine=self.engine,
                    deps=SimpleNamespace(heartbeat=lambda: None,
                                         config=SimpleNamespace(process_scope_classes=None)),
                    should_stop=should_stop, ownership_guard=lambda: None, admission_guard=lambda: None,
                    work_available=threading.Event(), prune_tracker=worker_cli._ProjectionPruneTracker(),
                    progress_output="off", stop_control=control, operator_stop_requested=lambda: False,
                )
            except (worker_cli.WorkerPublicStopError, WorkerOperationalStopError) as exc:
                return control, exc
        self.assertLess(time.monotonic(), deadline, "boot did not finish within its bound")
        return control, None

    # -- observations ------------------------------------------------------------

    def _rows(self) -> dict[str, object]:
        """Every business row of both documents that a boot could change."""

        queries = {
            "document": "SELECT document_id,status,current_processing_run_id FROM disclosure_core.document "
                        "WHERE document_id=ANY(:docs) ORDER BY document_id",
            "processing_run": "SELECT processing_run_id,document_id,status,is_active FROM "
                              "disclosure_core.processing_run WHERE document_id=ANY(:docs) ORDER BY 1",
            "attempt": "SELECT attempt_id,document_id,processing_run_id,state,row_version,is_current,"
                       "claim_generation FROM disclosure_ops.remote_parse_attempt WHERE document_id=ANY(:docs) "
                       "ORDER BY 1",
            "document_unit": "SELECT document_id,processing_run_id,count(*) FROM disclosure_core.document_unit "
                             "WHERE document_id=ANY(:docs) GROUP BY 1,2 ORDER BY 1,2",
            "outbox_event": "SELECT document_id,count(*) FROM disclosure_ops.outbox_event "
                            "WHERE document_id=ANY(:docs) GROUP BY 1 ORDER BY 1",
            "durable_publish_base": "SELECT document_id,count(*) FROM disclosure_ops.durable_publish_base "
                                    "WHERE document_id=ANY(:docs) GROUP BY 1 ORDER BY 1",
        }
        with self.engine.connect() as conn:
            return {name: [tuple(row) for row in conn.execute(sa.text(query),
                                                               {"docs": self.document_ids}).all()]
                    for name, query in queries.items()}

    def _tree(self) -> dict[str, str]:
        """Runtime and published files (path -> sha256), symlinks not followed."""

        tree: dict[str, str] = {}
        for base in (self.runtime_root, self.paths.data_path(Path())):
            for path in sorted(base.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    tree[str(path.relative_to(self.root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return tree

    def _authority(self, attempt_id: str) -> Any:
        with unit_of_work_factory(self.engine)() as uow:
            return uow.remote_parse_v4.load(attempt_id)

    def _cache_entries(self) -> list[Path]:
        cache = self.runtime_root / "cache" / "semantic_routes"
        return sorted(path for path in cache.rglob("*") if path.is_file()) if cache.exists() else []

    # -- the one three-boot case ----------------------------------------------------

    def test_public_stop_holds_across_boots_and_release_resumes_the_same_attempt(self) -> None:
        failing = [True]
        adapter = _Provider("f5-scratch-model", decide=_decide(failing))

        # Boot 0: the neighbor publishes normally (no model group).
        neighbor_fake = self._fake_provider(self.neighbor, _official_result_zip(self.neighbor.sha))
        neighbor_model = _Provider("f5-scratch-model", decide=_no_model_expected)
        control, stopped = self._boot(self.neighbor, neighbor_fake, neighbor_model,
                                      done=lambda: neighbor_fake.ack_posts >= 1)
        self.assertIsNone(stopped, stopped)
        self.assertEqual(neighbor_model.calls, 0)
        self.assertFalse(control.is_tripped())
        self.assertEqual((neighbor_fake.task_posts, neighbor_fake.ack_posts), (1, 1))
        neighbor_before = self._neighbor_state()
        self.assertEqual(neighbor_before["status"], "published")

        # Boot 1: two groups succeed and are cached, the third fails closed.
        fake = self._fake_provider(self.target, _sectioned_result_zip(self.target.sha))
        control, stopped = self._boot(self.target, fake, adapter, done=lambda: fake.ack_posts >= 1)
        self.assertIsInstance(stopped, worker_cli.WorkerPublicStopError)
        cause = control.first_cause()
        assert cause is not None
        self.assertEqual((cause.kind, cause.reason_code, cause.lane, cause.attempt_id),
                         ("semantic_failed_closed", "forbidden_tool_call", "commit", fake.attempt_id))
        self.assertEqual([item.outcome for item in cause.provider_attempts], ["failed_closed"])
        self.assertNotIn(_SECRET, json.dumps(cause.to_payload(), ensure_ascii=False))
        outcome = control.persistence_outcome(timeout=5)
        assert outcome is not None
        self.assertEqual(outcome.marker.status, "written")
        self.assertEqual(outcome.native.status, "failed", "a scratch root never touches launchd")
        active = WorkerControlStore.for_settings(self._settings()).read_active()
        assert active.record is not None and active.sha256 is not None
        self.assertEqual((active.record.record_origin, active.record.cause), ("automatic_fault", cause))
        self.assertTrue(all(value for _, value in active.record.fingerprints),
                        f"source/profile fingerprints are bound: {active.record.fingerprints}")
        self.assertEqual(stat.S_IMODE((self.runtime_root / "control" / "worker-circuit-stop.json").stat().st_mode),
                         0o600)
        stop_sha, stop_raw = active.sha256, active.raw
        self.assertEqual(adapter.calls, 3)
        self.assertEqual(len(self._cache_entries()), 2, "only the two successful groups are cached")
        self.assertEqual((fake.task_posts, fake.result_gets, fake.ack_posts), (1, 1, 0))
        scope = inspect_accepted_recovery_scope(self.engine, document_ids=(self.target.document_id,),
                                                accepted_attempt_ids=(fake.attempt_id,))
        self.assertEqual(len(scope), 1)
        # As in the existing recovery scenarios, the runtime epoch may be re-bound
        # by the next boot; the durable attempt identity may not change.
        pinned = {key: value for key, value in scope[0].items()
                  if key not in {"state", "is_current", "runtime_epoch_sha256"}}
        self.assertEqual(scope[0]["state"], "local_materialized")
        authority = self._authority(fake.attempt_id)
        kinds = {item.kind for item in authority.evidence}
        self.assertIn("local_materialization_receipt", kinds)
        self.assertNotIn("failure_receipt", kinds, "a public stop never settles the attempt as failed")
        self.assertIsNone(authority.publication_winner)
        receipt_sha = next(item.sha256 for item in authority.evidence
                           if item.kind == "local_materialization_receipt")
        intent = next(item.value for item in authority.evidence if item.kind == "materialization_intent")
        scratch = self.runtime_root / "staged_v4" / "scratch"
        self.assertTrue((scratch / intent.output_relpath).exists(), "materialized output is kept")
        self.assertEqual(self._neighbor_state(), neighbor_before, "the committed neighbor is untouched")
        rows = self._rows()
        target_row = [row for row in rows["document"] if row[0] == self.target.document_id][0]
        self.assertNotEqual(target_row[1], "published")
        for table in ("durable_publish_base", "document_unit"):
            self.assertEqual([row for row in rows[table] if row[0] == self.target.document_id], [],
                             f"no publication ({table}) for the stopped document")

        # Boot 2: every start gate refuses; nothing moves.
        rows_before, tree_before, http_before = self._rows(), self._tree(), list(fake.calls)
        blocked = AssertionError("composition started behind a recorded stop")
        stderr = io.StringIO()
        with (
            patch.object(worker_cli, "load_settings", return_value=self._settings()),
            patch.object(worker_cli.sqlalchemy, "create_engine", side_effect=blocked),
            patch.object(worker_cli, "create_db_engine", side_effect=blocked),
            patch.object(worker_cli, "MinerUDeploymentChecker", side_effect=blocked),
            redirect_stderr(stderr),
        ):
            self.assertEqual(worker_cli.main(["loop"]), 78)
        self.assertIn(stop_sha, stderr.getvalue())
        _, refused = self._boot(self.target, fake, adapter, done=lambda: False)
        self.assertIsInstance(refused, WorkerOperationalStopError)
        self.assertEqual((refused.state, refused.active_sha256), ("PUBLIC_STOP", stop_sha))  # type: ignore[union-attr]
        plan = plan_release(self._settings(), expect_sha256=stop_sha, process_lister=lambda: ())
        self.assertTrue(plan["would_release"])
        self.assertEqual(adapter.calls, 3)
        self.assertEqual(fake.calls, http_before)
        self.assertEqual(self._rows(), rows_before)
        self.assertEqual(self._tree(), tree_before)

        # Fix, then an explicit hash-bound release under the worker singleton.
        failing[0] = False
        receipt = self._release_under_singleton(stop_sha)
        self.assertFalse(receipt["idempotent_replay"])
        control_dir = self.runtime_root / "control"
        digest = stop_sha.removeprefix("sha256:")
        self.assertEqual((control_dir / f"worker-circuit-stop.{digest}.json").read_bytes(), stop_raw)
        decision = json.loads((control_dir / f"worker-circuit-release.{digest}.json").read_bytes())
        self.assertEqual(decision["released_stop_sha256"], stop_sha)
        self.assertFalse((control_dir / "worker-circuit-stop.json").exists())

        # The stopped process is gone: as in the existing recovery scenarios, its
        # claim lease is expired instead of waiting out the 300 s lease.
        with self.engine.begin() as conn:
            conn.execute(sa.text("UPDATE disclosure_ops.remote_parse_attempt SET claim_lease_until=:expired "
                                 "WHERE attempt_id=:attempt"),
                         {"attempt": fake.attempt_id, "expired": datetime.now(UTC) - timedelta(seconds=1)})

        # Boot 3: the same attempt resumes; only the formerly failed group is called.
        control, stopped = self._boot(self.target, fake, adapter, done=lambda: fake.ack_posts >= 1)
        self.assertIsNone(stopped, stopped)
        self.assertIsNone(control.first_cause())
        self.assertEqual(adapter.calls, 4, "groups 1-2 are cache hits; only group 3 calls the model")
        self.assertEqual(len(self._cache_entries()), 3)
        self.assertEqual((fake.task_posts, fake.result_gets, fake.ack_posts), (1, 1, 1))
        after = inspect_accepted_recovery_scope(self.engine, document_ids=(self.target.document_id,),
                                                accepted_attempt_ids=(fake.attempt_id,))
        self.assertEqual({key: after[0][key] for key in pinned}, pinned, "same attempt, run, fence, H0")
        self.assertEqual(after[0]["state"], "acked")
        final = self._authority(fake.attempt_id)
        self.assertEqual(next(item.sha256 for item in final.evidence
                              if item.kind == "local_materialization_receipt"), receipt_sha)
        rows = self._rows()
        target_document = [row for row in rows["document"] if row[0] == self.target.document_id][0]
        self.assertEqual(target_document[1:], ("published", pinned["processing_run_id"]))
        self.assertEqual([row for row in rows["durable_publish_base"] if row[0] == self.target.document_id],
                         [(self.target.document_id, 1)], "exactly one publication")
        units = [row for row in rows["document_unit"] if row[0] == self.target.document_id]
        self.assertEqual([row[1] for row in units], [pinned["processing_run_id"]])
        self.assertGreater(units[0][2], 0)
        self.assertEqual(self._neighbor_state(), neighbor_before)
        self.assertEqual(sorted(p.name for p in control_dir.iterdir() if p.name != "worker-circuit.lock"),
                         sorted([f"worker-circuit-release.{digest}.json", f"worker-circuit-stop.{digest}.json"]))

    def _release_under_singleton(self, stop_sha: str) -> dict[str, object]:
        """The CLI's ordering (worker singleton, then the control lock) on the scratch DB.

        The operator CLI additionally checks the production database identity,
        which a scratch database cannot satisfy, and reads the host process
        table, which is not evidence about this scratch run; both are replaced
        here by the explicit singleton and an empty owner list.
        """

        lock_engine = sa.create_engine(self.database_url, poolclass=sa.pool.NullPool,
                                       isolation_level="AUTOCOMMIT")
        try:
            with lock_engine.connect() as lock_conn:
                acquired = lock_conn.execute(sa.text("SELECT pg_try_advisory_lock(:ns, 0)"),
                                             {"ns": worker_cli.WORKER_NS}).scalar_one()
                self.assertTrue(acquired, "no other owner holds the worker singleton")
                try:
                    return release_worker_circuit(
                        self._settings(), expect_sha256=stop_sha, decided_by="root-f5-scratch",
                        reason="forbidden tool call fixed in the fake model", fixed_by="f5-scratch-fix",
                        process_lister=lambda: (),
                    )
                finally:
                    lock_conn.execute(sa.text("SELECT pg_advisory_unlock(:ns, 0)"), {"ns": worker_cli.WORKER_NS})
        finally:
            lock_engine.dispose()

    def _neighbor_state(self) -> dict[str, object]:
        rows = self._rows()
        document = [row for row in rows["document"] if row[0] == self.neighbor.document_id][0]
        return {
            "status": document[1],
            "run": document[2],
            "runs": [row for row in rows["processing_run"] if row[1] == self.neighbor.document_id],
            "units": [row for row in rows["document_unit"] if row[0] == self.neighbor.document_id],
            "attempts": [row for row in rows["attempt"] if row[1] == self.neighbor.document_id],
            "publish": [row for row in rows["durable_publish_base"] if row[0] == self.neighbor.document_id],
            "outbox": [row for row in rows["outbox_event"] if row[0] == self.neighbor.document_id],
        }


if __name__ == "__main__":
    unittest.main()
