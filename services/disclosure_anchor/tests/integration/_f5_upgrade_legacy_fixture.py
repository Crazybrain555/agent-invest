"""The pre-F5 worker's legacy duties for the cross-version upgrade scenario (scratch DB only).

The seeding plays the old worker's durable behaviour exactly. It uses the installed parent's exact
P0/WP0/R0 (the authored synthetic parent by default, the actual parent on root's explicit opt-in),
the real V4 ingress factory and durable ingress, and the backend's own stage calls. Persistence,
resolver, materialization, the atomic publisher and the semantic router, executor and file cache
are all real too. Only the MinerU HTTP API (``FleetProvider``) and the model port are fake.

The durable V4 contracts (spec, checkpoint, evidence, lifecycle, ingress, repository, both profile
contracts) are byte-identical between the pre-F5 baseline and the phase-start candidate. So these
duties are the old worker's duties. No checkpoint is written by SQL. Claims stay with the old
owner, and their leases are expired afterwards, as a stopped worker leaves them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import re
import threading
import time
from typing import Any

import httpx
import pypdfium2 as pdfium
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from disclosure_anchor.adapters.db.postgres.atomic_document_publisher_v4 import (
    PostgresAtomicWholeDocumentPublisherV4,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import MinerUHttpRemoteV4
from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import MinerUHttpStagedV4
from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import canonical_result_owner_v2
from disclosure_anchor.adapters.parsers.mineru_medium.v4_initial_ingress import MinerUV4InitialIngressFactory
from disclosure_anchor.adapters.parsers.mineru_medium.v4_stage_input_resolver import (
    ProductionV4StageInputResolver,
)
from disclosure_anchor.adapters.parsers.pdf_text_observation import observe_pdf_text_rectangles
from disclosure_anchor.adapters.security.provider_secret_cipher import AesGcmProviderSecretCipher
from disclosure_anchor.adapters.security.provider_secret_keyring import load_provider_secret_keyring_from_settings
from disclosure_anchor.adapters.semantics.runtime import SemanticRuntime
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.atomic_publication_artifact_readiness_v4 import (
    FilesystemAtomicPublicationArtifactReadinessV4,
)
from disclosure_anchor.adapters.storage.immutable_artifact_store import ImmutableArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.provider_document_source import ProviderDocumentFileSource
from disclosure_anchor.adapters.storage.semantic_route_store import (
    SemanticRouteGroupFileCache,
    SemanticRouteReceiptStore,
)
from disclosure_anchor.adapters.storage.v4_source_observation import BoundedV4SourcePdfObserver
from disclosure_anchor.application.ports.parser import ParserIdentity, ParserOptions
from disclosure_anchor.application.ports.remote_parse_v4_repository import RecoveryCandidate
from disclosure_anchor.application.ports.staged_new_work_v4 import V4OrdinaryParseCandidate
from disclosure_anchor.application.services.atomic_publication_request_builder_v4 import (
    ProductionAtomicPublicationRequestBuilderV4,
)
from disclosure_anchor.application.services.atomic_publication_request_factory_v4 import (
    RecoverableAtomicPublicationRequestFactoryV4,
)
from disclosure_anchor.application.services.provider_document_admission import ProviderDocumentAdmission
from disclosure_anchor.application.services.semantic_adjudication import (
    ConfiguredSemanticProvider,
    OrderedSemanticAdjudicationExecutor,
)
from disclosure_anchor.application.services.semantic_router import SemanticRouter
from disclosure_anchor.application.services.semantic_taxonomy import load_semantic_route_taxonomy
from disclosure_anchor.application.services.staged_coordinator_backend_v4 import DurableStagedCoordinatorBackendV4
from disclosure_anchor.application.services.staged_coordinator_persistence_v4 import (
    DurableStagedCoordinatorPersistenceV4,
    DurableV4ClaimGuard,
)
from disclosure_anchor.application.services.staged_execution_guard import StageLeaseGuard, StageLeaseLost
from disclosure_anchor.application.services.staged_ingress_v4 import DurableStagedIngressV4
from disclosure_anchor.application.services.staged_parse_coordinator import CoordinatorWork, RetryStage
from disclosure_anchor.application.services.staged_v4_capacity import staged_v4_coordinator_limits
from disclosure_anchor.application.use_cases.prepare_and_publish_whole_document_v4 import (
    PrepareAndPublishWholeDocumentV4,
)
from disclosure_anchor.domain import ids
from disclosure_anchor.settings import Settings
from tests._f5_upgrade_q0_fixture import API_URL, INFERENCE_URL, ParentQ0


OLD_OWNER = "staged-v4-pre-f5-worker-fixture-" + "0" * 8
# Production spec values (every legacy spec in root's inventory carries exactly these).
LEGACY_RUNAWAY_SECONDS = 86400
LEGACY_DRAIN_SECONDS = 86400
LEGACY_ARCHIVE_MEMBER_LIMIT = 100000
LEGACY_RESULT_LEASE_SECONDS = 600
SEMANTIC_PROVIDER_ID = "f5-upgrade-model"
# Target states and how many duties each gets. Accepted duties (all but prepared) number 12,
# beyond the old content-encoding grant's 1..8. ``ack_pending`` is the published tail whose
# scratch source was legally cleaned before its ACK.
TARGETS: tuple[tuple[str, int], ...] = (
    ("prepared", 3),
    ("submitted", 6),
    ("remote_terminal", 2),
    ("local_materialized", 1),
    ("publish_committed", 2),
    ("ack_pending", 1),
)
LEGACY_STATES = frozenset(state for state, _count in TARGETS)


# -- fake MinerU task protocol v2 for many tasks -------------------------------------------------


@dataclass
class FleetTask:
    task_id: str
    client_submit_key: str
    attempt_id: str
    fence_identity: str
    source_sha256: str
    artifact: bytes
    status: str

    @property
    def artifact_sha256(self) -> str:
        return hashlib.sha256(self.artifact).hexdigest()

    @property
    def owner(self) -> str:
        return canonical_result_owner_v2(
            task_id=self.task_id, artifact_sha256=self.artifact_sha256, artifact_byte_count=len(self.artifact),
        )

    def payload(self) -> dict[str, object]:
        value: dict[str, object] = {
            "task_id": self.task_id, "status": self.status,
            "status_url": f"/tasks/{self.task_id}", "result_url": f"/tasks/{self.task_id}/result",
            "task_protocol_schema": "mineru-task-protocol.v2", "idempotency_key": self.client_submit_key,
            "attempt_identity": self.attempt_id, "fence_identity": self.fence_identity,
            "protocol_state": self.status, "error": None,
        }
        if self.status == "completed":
            value.update(result_artifact_schema="mineru-retained-result.v1",
                         result_artifact_sha256=self.artifact_sha256,
                         result_artifact_bytes=len(self.artifact), result_artifact_owner=self.owner)
        return value


@dataclass
class FleetProvider:
    """The MinerU task protocol v2 for many tasks, counted per idempotency key.

    A POST registers the task and then loses its HTTP response, so every accepted submission
    goes through the client's GET reconciliation, as in the existing end-to-end fake. A second
    POST for a known key answers 409 and is recorded: the acceptance requires exactly one.
    """

    sources: dict[str, tuple[bytes, bytes]] = field(default_factory=dict)
    complete_on_accept: dict[str, bool] = field(default_factory=dict)
    tasks: dict[str, FleetTask] = field(default_factory=dict)
    by_task: dict[str, FleetTask] = field(default_factory=dict)
    posts: Counter[str] = field(default_factory=Counter)
    post_bodies: dict[str, bytes] = field(default_factory=dict)
    duplicate_posts: list[str] = field(default_factory=list)
    lookups: Counter[str] = field(default_factory=Counter)
    status_gets: Counter[str] = field(default_factory=Counter)
    result_gets: Counter[str] = field(default_factory=Counter)
    lease_posts: Counter[str] = field(default_factory=Counter)
    acks: Counter[str] = field(default_factory=Counter)
    ack_effects: Counter[str] = field(default_factory=Counter)
    calls: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def register(self, source_sha256: str, source: bytes, artifact: bytes, *, complete_on_accept: bool) -> None:
        self.sources[source_sha256] = (source, artifact)
        self.complete_on_accept[source_sha256] = complete_on_accept

    def complete(self, client_submit_key: str) -> None:
        with self.lock:
            self.tasks[client_submit_key].status = "completed"

    def task_for_attempt(self, attempt_id: str) -> FleetTask:
        (task,) = [item for item in self.tasks.values() if item.attempt_id == attempt_id]
        return task

    def http_call_count(self) -> int:
        with self.lock:
            return len(self.calls)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = f"{request.method} {request.url.path}"
        with self.lock:
            self.calls.append(route)
            if request.method == "GET" and request.url.path.startswith("/tasks/by-idempotency/"):
                key = request.url.path.rsplit("/", 1)[1]
                self.lookups[key] += 1
                task = self.tasks.get(key)
                if task is None:
                    return httpx.Response(404, json={"detail": "Task not found"})
                return httpx.Response(200, json=task.payload())
            if request.method == "POST" and request.url.path == "/tasks":
                body = request.read()
                fields = {}
                for name in ("agent_idempotency_key", "agent_attempt_identity", "agent_fence_identity"):
                    match = re.search(b'name="' + name.encode() + b'"\r\n\r\n([^\r\n]+)', body)
                    if match is None:
                        raise AssertionError(f"fake provider POST lacks {name}")
                    fields[name] = match.group(1).decode()
                key = fields["agent_idempotency_key"]
                self.posts[key] += 1
                if key in self.tasks:
                    self.duplicate_posts.append(key)
                    return httpx.Response(409, json={"detail": "duplicate submission"})
                (source_sha256,) = [sha for sha, (source, _) in self.sources.items() if source in body]
                task = FleetTask(
                    task_id="task-" + hashlib.sha256(key.encode()).hexdigest()[:24], client_submit_key=key,
                    attempt_id=fields["agent_attempt_identity"], fence_identity=fields["agent_fence_identity"],
                    source_sha256=source_sha256, artifact=self.sources[source_sha256][1],
                    status="completed" if self.complete_on_accept[source_sha256] else "pending",
                )
                self.tasks[key], self.by_task[task.task_id] = task, task
                self.post_bodies[key] = body
                raise httpx.ReadTimeout("simulated POST response loss", request=request)
            parts = request.url.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "tasks" and parts[1] in self.by_task:
                task = self.by_task[parts[1]]
                if request.method == "GET" and len(parts) == 2:
                    self.status_gets[task.task_id] += 1
                    return httpx.Response(200, json=task.payload())
                if request.method == "POST" and parts[2:] == ["lease"]:
                    self.lease_posts[task.task_id] += 1
                    return httpx.Response(200, json={"schema": "mineru-task-protocol.v2",
                                                     "task_id": task.task_id,
                                                     "lease_until_unix": time.time() + 600})
                if request.method == "GET" and parts[2:] == ["result"] and task.status == "completed":
                    self.result_gets[task.task_id] += 1
                    return httpx.Response(200, content=task.artifact, headers={
                        "Content-Type": "application/zip", "Content-Length": str(len(task.artifact)),
                        "X-MinerU-Result-SHA256": task.artifact_sha256, "X-MinerU-Result-Owner": task.owner,
                    })
                if request.method == "POST" and parts[2:] == ["ack"]:
                    self.acks[task.task_id] += 1
                    if task.status != "consumed":
                        self.ack_effects[task.task_id] += 1
                        task.status = "consumed"
                    return httpx.Response(200, content=json.dumps(
                        {"schema": "mineru-task-protocol.v2", "task_id": task.task_id, "status": "consumed"},
                        separators=(",", ":")).encode())
        raise AssertionError(f"unexpected fake MinerU request: {route}")


# -- documents ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyDocument:
    label: str
    target: str
    document_id: str
    provider_document_id: str
    source: bytes
    source_sha256: str
    relpath: Path
    sectioned: bool


def two_page_pdf(width: int, height: int) -> bytes:
    with pdfium.PdfDocument.new() as pdf:
        for _ in range(2):
            pdf.new_page(width, height).close()
        output = io.BytesIO()
        pdf.save(output)
    return output.getvalue()


def create_document(engine: Engine, paths: FileStorePathBuilder, *, security_id: str, label: str, target: str,
                    width: int, height: int, title: str | None = None,
                    filing_type: str | None = None) -> LegacyDocument:
    source = two_page_pdf(width, height)
    sha = "sha256:" + hashlib.sha256(source).hexdigest()
    document_id = str(ids.new_document_id())
    provider_document_id = "f5u-" + ids.new_ulid()
    relpath = paths.raw_document_relpath(provider="cninfo", security_code="000001", year=2026,
                                         provider_document_id=provider_document_id, raw_file_hash=sha)
    path = paths.data_path(relpath)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(source)
    path.chmod(0o600)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO disclosure_core.document (document_id,security_id,provider,provider_document_id,"
            "raw_file_relpath,raw_file_hash,status,title,class_filing_type) VALUES (:doc,:sec,'cninfo',"
            ":provider_document,:relpath,:sha,'registered',:title,:filing_type)"),
            {"doc": document_id, "sec": security_id, "provider_document": provider_document_id,
             "relpath": relpath.as_posix(), "sha": sha, "title": title, "filing_type": filing_type})
    return LegacyDocument(label=label, target=target, document_id=document_id,
                          provider_document_id=provider_document_id, source=source, source_sha256=sha,
                          relpath=relpath, sectioned=filing_type is not None)


# -- semantic runtime shared by the old worker and the recovery boots ----------------------------


def semantic_runtime(settings: Settings, paths: Any, artifacts: Any, adapter: Any) -> SemanticRuntime:
    """``build_semantic_runtime`` with only the provider adapter replaced (same file cache path)."""

    taxonomy = load_semantic_route_taxonomy()
    cache = SemanticRouteGroupFileCache(
        settings.disclosure_runtime_root / "cache" / "semantic_routes" / "v2" / taxonomy.version
        / adapter.provider_identity.provider_id)
    executor = OrderedSemanticAdjudicationExecutor(
        (ConfiguredSemanticProvider(adapter=adapter, cache=cache),),
        policy_version=settings.disclosure_semantic_failover_policy,
    )
    return SemanticRuntime(
        router=SemanticRouter(taxonomy=taxonomy, executor=executor, batch_size=1),
        receipts=SemanticRouteReceiptStore(paths=paths, artifacts=artifacts),
    )


# -- the old worker ------------------------------------------------------------------------------


class PreF5Worker:
    """The pre-F5 worker's durable behaviour under the parent's exact P0/WP0/R0."""

    def __init__(self, *, engine: Engine, settings: Settings, fleet: FleetProvider, adapter: Any,
                 parent: ParentQ0) -> None:
        self.p0_bytes = parent.process_profile_exact_bytes
        self.p0 = parent.process_profile
        self.wp0 = parent.worker_profile
        self.r0 = parent.runtime_identity
        if (self.p0.exact_bytes != self.p0_bytes or self.wp0.process_profile_sha256 != self.p0.sha256
                or self.p0.runtime_bundle_identity_sha256 != self.r0):
            raise AssertionError("the parent profile pair does not bind the parent P0/WP0/R0")
        self.limits = staged_v4_coordinator_limits(self.p0, worker_profile=self.wp0)
        self.paths = FileStorePathBuilder(settings)
        uow = unit_of_work_factory(engine)
        source = ProviderDocumentFileSource(self.paths, text_reader=observe_pdf_text_rectangles)
        self.ingress_factory = MinerUV4InitialIngressFactory(
            source=BoundedV4SourcePdfObserver(paths=self.paths),
            paths=self.paths,
            parser_identity=ParserIdentity(name="MinerU", version="3.4.4"),
            parser_options=ParserOptions(
                backend="hybrid-http-client", effort="medium", image_analysis=False,
                timeout_seconds=LEGACY_RUNAWAY_SECONDS, api_url=API_URL,
                api_drain_timeout_seconds=LEGACY_DRAIN_SECONDS, server_url=INFERENCE_URL,
                http_request_concurrency=None, runtime_bundle_identity_sha256=self.r0,
            ),
            process_profile=self.p0,
            process_profile_exact_bytes=self.p0_bytes,
            worker_profile=self.wp0,
            result_lease_seconds=min(self.p0.task_retention_seconds, 3600),
            remote_runaway_seconds=LEGACY_RUNAWAY_SECONDS,
            archive_member_count_limit=LEGACY_ARCHIVE_MEMBER_LIMIT,
            archive_uncompressed_byte_limit=self.p0.temporary_disk_bytes_limit,
            max_retries=settings.disclosure_max_parse_retries,
            scope_classes=None,
        )
        self.ingress = DurableStagedIngressV4(uow_factory=uow)
        self.persistence = DurableStagedCoordinatorPersistenceV4(
            uow_factory=uow, limits=self.limits, owner_identity=OLD_OWNER,
        )
        self.remote = MinerUHttpRemoteV4(transport=httpx.MockTransport(fleet),
                                         request_timeout_seconds=self.limits.max_stage_step_seconds)
        materialization = MinerUHttpStagedV4(
            scratch_root=settings.disclosure_runtime_root / "staged_v4" / "scratch",
            published_root=self.paths.data_path(Path()), transport=self.remote, clock=time.time,
        )
        semantic = semantic_runtime(settings, self.paths, ArtifactStore(self.paths), adapter)
        readiness = FilesystemAtomicPublicationArtifactReadinessV4(
            paths=self.paths, immutable_store=ImmutableArtifactStore(self.paths), output_promotion=materialization,
        )
        publisher = PrepareAndPublishWholeDocumentV4(
            uow_factory=uow,
            publication_requests=RecoverableAtomicPublicationRequestFactoryV4(
                readiness=readiness,
                new_request_builder=ProductionAtomicPublicationRequestBuilderV4(
                    path_builder=self.paths, uow_factory=uow,
                    admission=ProviderDocumentAdmission(path_builder=self.paths, source=source),
                    semantic_router=semantic.router,
                ),
            ),
            readiness=readiness,
            publisher=PostgresAtomicWholeDocumentPublisherV4(engine=engine),
        )
        self.backend = DurableStagedCoordinatorBackendV4(
            persistence=self.persistence,
            inputs=ProductionV4StageInputResolver(uow_factory=uow, paths=self.paths, provider_source=source,
                                                  worker_profile=self.wp0),
            remote=self.remote,
            materialization=materialization,
            secret_cipher=AesGcmProviderSecretCipher(keyring=load_provider_secret_keyring_from_settings(settings)),
            claim_guard=DurableV4ClaimGuard(uow_factory=uow),
            publisher=publisher,
            poll_seconds=self.wp0.provider_poll_milliseconds / 1000,
        )

    def close(self) -> None:
        self.remote.close()

    @staticmethod
    def guard(seconds: float = 600.0) -> StageLeaseGuard:
        return StageLeaseGuard(time.monotonic() + seconds, threading.Event(), time.monotonic)

    def admit(self, document: LegacyDocument, *, security_id: str) -> CoordinatorWork:
        """The admitter's exact sequence: observe, build, durable ingress, claim the new H0."""

        candidate = V4OrdinaryParseCandidate(
            document_id=document.document_id, provider="cninfo",
            provider_document_id=document.provider_document_id, security_id=security_id,
            security_code="000001", raw_file_relpath=document.relpath.as_posix(),
            raw_file_hash=document.source_sha256, archived_raw_byte_count=len(document.source),
        )
        request = self.ingress_factory.observation_request(candidate, available_credits=self.limits.credits)
        observed = self.ingress_factory.observe(request, stage_guard=self.guard())
        command = self.ingress_factory.build(candidate, source_observation=observed.source,
                                             available_credits=self.limits.credits)
        authority = self.ingress.execute(command, write_guard=lambda: None)
        if authority.state != "prepared" or authority.lifecycle_version != 0:
            raise AssertionError(f"ingress did not create one H0 for {document.label}")
        return self.backend.claim_recovery(RecoveryCandidate(
            attempt_id=authority.attempt_id, state="prepared", lifecycle_version=0,
            claim_generation=authority.claim_generation, claim_owner_identity=None,
            lease_remaining_seconds=None,
        ))

    def drive(self, work: CoordinatorWork, target: str, *, fleet: FleetProvider,
              commit_guard: Callable[[], StageLeaseGuard] | None = None) -> CoordinatorWork:
        """Advance one claimed duty through the backend's own stages until ``target``."""

        credits = self.limits.credits

        def step(name: str, current: CoordinatorWork, **kwargs: Any) -> CoordinatorWork:
            # As the coordinator does: a RetryStage (for example the lost POST response, which
            # leaves the duty reconciling until a GET finds the task) re-runs the same stage.
            for _attempt in range(3):
                try:
                    return getattr(self.backend, name)(current, credit_allowance=credits,
                                                       stage_guard=kwargs.get("guard") or self.guard())
                except RetryStage:
                    continue
            raise AssertionError(f"{current.attempt_id} {name} kept asking for a retry")

        if target == "prepared":
            return work
        work = step("prepare_remote_io", work)
        work = step("run_remote", work)
        if work.state != "submitted":
            raise AssertionError(f"{work.attempt_id} reached {work.state}, not submitted")
        if target == "submitted":
            return work
        fleet.complete(fleet.task_for_attempt(work.attempt_id).client_submit_key)
        work = step("run_remote", work)
        if target == "remote_terminal":
            return work
        work = step("prepare_local_io", work)
        work = step("run_local", work)
        if target == "local_materialized":
            if commit_guard is not None:
                # The worker stops while the commit adjudicates: cached groups stay, no residue.
                try:
                    step("commit", work, guard=commit_guard())
                except StageLeaseLost:
                    pass
                else:
                    raise AssertionError("the interrupted commit unexpectedly published")
            return work
        work = step("commit", work)
        if target == "publish_committed":
            return work
        work = step("cleanup", work)
        work = step("cleanup", work)
        if work.state != "ack_pending":
            raise AssertionError(f"{work.attempt_id} reached {work.state}, not ack_pending")
        return work


def expire_old_claims(engine: Engine) -> int:
    """Leave the old owner's claims in place with expired leases (a stopped worker)."""

    with engine.begin() as conn:
        return conn.execute(sa.text(
            "UPDATE disclosure_ops.remote_parse_attempt SET claim_lease_until=:expired "
            "WHERE is_current AND claim_owner_identity=:owner"),
            {"expired": datetime.now(UTC) - timedelta(seconds=1), "owner": OLD_OWNER}).rowcount


def legacy_rows(engine: Engine) -> dict[str, dict[str, object]]:
    """Every V4 row's durable identity and history, for exact before/after comparison."""

    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT attempt_id,document_id,processing_run_id,state,is_current,row_version,claim_generation,"
            "claim_owner_identity,fence_identity,client_submit_key,request_sha256,runtime_epoch_sha256 "
            "FROM disclosure_ops.remote_parse_attempt WHERE checkpoint_contract_version=4 ORDER BY attempt_id"
        )).mappings().all()
    return {row["attempt_id"]: dict(row) for row in rows}
