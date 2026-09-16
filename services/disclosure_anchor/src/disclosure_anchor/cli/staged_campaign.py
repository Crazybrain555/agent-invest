"""One finite, frozen-scope V4 campaign run: frozen membership, bounded time.

Reuses the production singleton lock, deployment gate, explicit stream
activation, global recovery and all seven lanes through the existing campaign
runtime. It writes a run receipt; it does not emit formal M6 owner events,
publication credit or qualification.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import signal
import time
from types import FrameType
from typing import Any

import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from disclosure_anchor.adapters.db.postgres.connection import (
    require_runtime_app_connection, require_runtime_app_engine,
)
from disclosure_anchor.adapters.runtime.m6_continuous_clock import diagnostic_continuous_clock
from disclosure_anchor.adapters.runtime.m6_e2e_assembly import M6E2EAssemblyWorker, M6LifecycleSpool
from disclosure_anchor.adapters.runtime.m6_e2e_run import (
    M6RunDirectory, M6RunnerClosureFailed, build_runner_closure_receipts, execute_runner_closure,
    load_m6_run_directory, m6_owner_client_factory,
)
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import MinerUDeploymentChecker
from disclosure_anchor.adapters.runtime.mineru_stream_activation import load_mineru_stream_activation
from disclosure_anchor.adapters.runtime.mineru_stream_worker import owned_mineru_stream_control
from disclosure_anchor.adapters.runtime.stage_observation import JsonlStageObserver, ProgressRecorder
from disclosure_anchor.adapters.runtime.staged_worker_v4 import build_staged_worker_v4_campaign_runtime
from disclosure_anchor.application.services.staged_campaign_runner import (
    CampaignInputError, CampaignRunRequest, CampaignRuntimeIdentity, CampaignStopState,
    campaign_receipt, campaign_stop_predicate, load_campaign_inputs,
)
from disclosure_anchor.application.services.staged_parse_coordinator import CoordinatorResult, CoordinatorSnapshot
from disclosure_anchor.application.worker.locks import WORKER_NS
from disclosure_anchor.cli.staged_commission import _write_new
from disclosure_anchor.cli.worker import (
    _assert_staged_singleton, _assert_worker_admission, _create_worker_db_engine,
    _database_url, _load_staged_process_profile, _process_scope_classes,
)
from disclosure_anchor.settings import Settings, load_settings

CAMPAIGN_INPUT_FILE_MAX_BYTES = 8 * 1024 * 1024


def _read_pinned(path: Path, label: str) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, "rb") as stream:
            payload = stream.read(CAMPAIGN_INPUT_FILE_MAX_BYTES + 1)
    except IsADirectoryError as exc:
        raise CampaignInputError(f"{label} is not a regular file") from exc
    if not payload or len(payload) > CAMPAIGN_INPUT_FILE_MAX_BYTES:
        raise CampaignInputError(f"{label} is empty or over its byte bound")
    return payload


def _scratch_residuals(settings: Settings, started_utc: datetime) -> int:
    """Entries left under the shared V4 scratch root that were created during this run."""
    root = settings.disclosure_runtime_root / "staged_v4" / "scratch"
    if not root.is_dir():
        return 0
    threshold = started_utc.timestamp()
    return sum(1 for entry in root.iterdir() if entry.lstat().st_mtime >= threshold)


def run_campaign(
    settings: Settings, *, request: CampaignRunRequest, external_stop: Callable[[], bool],
    progress: Callable[[CoordinatorSnapshot], None] | None = None,
    publication_committed: Callable[[bool], None] | None = None,
    stage_observer: JsonlStageObserver | None = None,
    m6_run: M6RunDirectory | None = None, m6_spool_dir: Path | None = None,
) -> dict[str, Any]:
    """Run exactly one frozen campaign inside the existing production guards.

    With ``m6_run`` the run is owner-bound: lifecycle facts are spooled and
    delivered as ``e2e_runner`` events on a worker thread, the owner lease
    gates new admission, and after the drain the runner deposits its control
    receipts and acknowledges admission close on that same thread.
    """
    if settings.worker_parse_execution_mode != "staged-v4":
        raise CampaignInputError("campaign requires explicit staged-v4 mode")
    if (m6_run is None) != (m6_spool_dir is None):
        raise CampaignInputError("owner-bound campaign requires both the run directory and a spool directory")
    if m6_run is not None:
        # The frozen spec must name exactly this corpus, scope, campaign and
        # mode before any profile, lock, database or admission is touched.
        spec = m6_run.require_spec()
        if (
            spec.mode != "e2e_publication" or spec.campaign_id != request.scope.campaign_id
            or spec.manifest_sha256 != request.manifest.canonical_sha256()
            or spec.scope_sha256 != request.scope.canonical_sha256()
        ):
            raise CampaignInputError("owner run spec is bound to a different corpus, scope, campaign or mode")
    loaded = _load_staged_process_profile(settings)
    if m6_run is not None:
        runtime_identity = m6_run.require_spec().runtime
        if (
            runtime_identity.process_profile_sha256 != loaded.profile.sha256
            or runtime_identity.runtime_bundle_identity_sha256 != loaded.profile.runtime_bundle_identity_sha256
        ):
            raise CampaignInputError("owner run spec names a different process profile or runtime bundle")
    checker = MinerUDeploymentChecker(settings, parse_enabled=True, process_profile=loaded.profile)
    expected_capacity = checker.expected_capacity
    if expected_capacity is None:
        raise CampaignInputError("campaign requires an explicit MinerU capacity configuration")
    activation = load_mineru_stream_activation(
        settings.disclosure_mineru_stream_pressure_config,
        expected_sha256=settings.disclosure_mineru_stream_pressure_config_sha256,
        expected_owner_uid=os.getuid(), expected_capacity=expected_capacity,
        expected_runtime_identity_sha256=settings.disclosure_mineru_runtime_bundle_identity_sha256,
    )
    if activation is None:
        raise CampaignInputError("campaign requires an explicit stream activation")
    progress_hook = progress if progress is not None else (lambda _snapshot: None)
    committed_hook = publication_committed if publication_committed is not None else (lambda _replaced: None)
    lock_engine = sa.create_engine(_database_url(settings), poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        with lock_engine.connect() as lock_conn:
            require_runtime_app_connection(lock_conn)
            if not lock_conn.execute(
                sa.text("SELECT pg_try_advisory_lock(:ns, 0)"), {"ns": WORKER_NS},
            ).scalar_one():
                raise RuntimeError("another worker holds the singleton lock")
            engine = _create_worker_db_engine(settings)
            try:
                require_runtime_app_engine(engine)

                def ownership_guard() -> None:
                    _assert_staged_singleton(lock_conn)

                def admission_guard() -> None:
                    _assert_worker_admission(lock_conn, mineru_checker=checker, singleton_guard=ownership_guard)

                spool: M6LifecycleSpool | None = None
                worker: M6E2EAssemblyWorker | None = None
                if m6_run is not None and m6_spool_dir is not None:
                    spec = m6_run.require_spec()
                    spool = M6LifecycleSpool(
                        m6_spool_dir, run_id=spec.run_id, spec_sha256=spec.canonical_sha256(),
                        producer_epoch_sha256=m6_run.epoch("e2e_runner"),
                        max_facts=4 * len(request.admission_scope.ordinary_document_ids) + 8,
                    )
                    clock = diagnostic_continuous_clock()
                    refresh = min(30.0, max(0.1, m6_run.lease.maximum_lease_ns / 3e9))
                    worker = M6E2EAssemblyWorker(
                        spool, client_factory=m6_owner_client_factory(m6_run, role="e2e_runner", continuous_ns=clock.now_ns),
                        lease_refresh_seconds=refresh, continuous_ns=clock.now_ns,
                    )
                    worker.start()
                    inner_external_stop = external_stop
                    held_worker = worker

                    def external_stop() -> bool:  # noqa: F811 - a lapsed lease or failed sender only closes new admission
                        return inner_external_stop() or held_worker.failed or not held_worker.admission_allowed()
                try:
                    return _run_owned_campaign(
                        settings, request=request, external_stop=external_stop, progress_hook=progress_hook,
                        committed_hook=committed_hook, stage_observer=stage_observer, engine=engine,
                        ownership_guard=ownership_guard, admission_guard=admission_guard, loaded=loaded,
                        expected_capacity=expected_capacity, activation=activation, m6_run=m6_run, spool=spool,
                        worker=worker,
                    )
                except BaseException:
                    # Any failure before the closure sequence leaves the spool
                    # and worker visible in their files; close them, never hide.
                    if worker is not None:
                        worker.close(5.0)
                    if spool is not None:
                        spool.close()
                    raise
            finally:
                engine.dispose()
    finally:
        lock_engine.dispose()


def _run_owned_campaign(
    settings: Settings, *, request: CampaignRunRequest, external_stop: Callable[[], bool],
    progress_hook: Callable[[CoordinatorSnapshot], None], committed_hook: Callable[[bool], None],
    stage_observer: JsonlStageObserver | None, engine: Any, ownership_guard: Callable[[], None],
    admission_guard: Callable[[], None], loaded: Any, expected_capacity: Any, activation: Any,
    m6_run: M6RunDirectory | None, spool: M6LifecycleSpool | None, worker: M6E2EAssemblyWorker | None,
) -> dict[str, Any]:
    assembly: dict[str, Any] | None = None
    with owned_mineru_stream_control(
        settings, expected_capacity=expected_capacity, wakeup=lambda: None,
    ) as stream_control:
        if stream_control is None:
            raise CampaignInputError("campaign requires an active stream control")
        runtime = build_staged_worker_v4_campaign_runtime(
            settings=settings, engine=engine, ownership_guard=ownership_guard,
            admission_guard=admission_guard,
            process_scope_classes=_process_scope_classes(settings),
            progress=progress_hook, campaign_scope=request.admission_scope,
            expected_capacity=expected_capacity, stream_control=stream_control,
            publication_committed=committed_hook, stage_observer=stage_observer,
            lifecycle_facts=spool,
        )
        started_utc = datetime.now(UTC)
        try:
            pinned_worker_profile = None if m6_run is None else m6_run.require_spec().runtime.worker_profile_sha256
            if pinned_worker_profile is not None and pinned_worker_profile != runtime.worker_profile_sha256:
                raise CampaignInputError("owner run spec names a different worker profile")
            runtime.verify_startup()
            started_utc = datetime.now(UTC)
            started_monotonic = time.monotonic()
            state = CampaignStopState()
            stop_requested = campaign_stop_predicate(
                monotonic=time.monotonic, deadline_monotonic=started_monotonic + request.max_seconds,
                external_stop=external_stop, state=state,
            )
            result = runtime.coordinator.run(stop_requested=stop_requested)
            finished_monotonic = time.monotonic()
            owner_identity = runtime.owner_identity
            identity = CampaignRuntimeIdentity(
                owner_identity=owner_identity,
                worker_profile_sha256=runtime.worker_profile_sha256,
                process_profile_sha256=loaded.profile.sha256,
                runtime_bundle_identity_sha256=loaded.profile.runtime_bundle_identity_sha256,
                capacity_sha256=expected_capacity.sha256,
                stream_activation_sha256=activation.source_sha256,
            )
        finally:
            runtime.close()
        if m6_run is not None and spool is not None and worker is not None:
            assembly = _finish_assembly(
                m6_run, spool, worker, result=result, owner_identity=owner_identity,
                scratch_residual_count=_scratch_residuals(settings, started_utc),
            )
        return campaign_receipt(
            request=request, identity=identity, result=result, started_utc=started_utc,
            finished_utc=datetime.now(UTC), monotonic_elapsed_s=finished_monotonic - started_monotonic,
            stop_reason=state.reason if state.reason is not None else "quiescent", assembly=assembly,
        )


def _finish_assembly(
    m6_run: M6RunDirectory, spool: M6LifecycleSpool, worker: M6E2EAssemblyWorker, *,
    result: CoordinatorResult, owner_identity: str, scratch_residual_count: int,
) -> dict[str, Any]:
    """Drain the spool, then run the runner closure on the worker's own client thread."""
    spec = m6_run.require_spec()
    closure: dict[str, Any] = {}

    def after_drain(client: Any) -> dict[str, Any]:
        receipts = build_runner_closure_receipts(
            run_id=spec.run_id, spec_sha256=spec.canonical_sha256(), runner_epoch_sha256=m6_run.epoch("e2e_runner"),
            owner_identity=owner_identity, spool=spool, result=result,
            scratch_residual_count=scratch_residual_count, children_exited=True,
        )
        try:
            return execute_runner_closure(client, receipts, request_stop=True)
        except M6RunnerClosureFailed as exc:
            return {"complete": False, "failed_step": exc.step, "reason": exc.reason}

    status = worker.close(300.0, after_drain=after_drain)
    closure = status.get("after_drain") or {"complete": False, "reason": "closure sequence did not run"}
    spool_status = spool.close()
    failed = bool(spool_status["failed"]) or status.get("worker_error") is not None or not closure.get("complete")
    return {
        "status": "failed" if failed else "complete", "run_id": spec.run_id, "spec_sha256": spec.canonical_sha256(),
        "anchor_sha256": m6_run.anchor.canonical_sha256(), "producer_kind": "e2e_runner",
        "producer_epoch_sha256": m6_run.epoch("e2e_runner"), "run_directory_pins": m6_run.pins,
        "spool": spool_status, "worker_error": status.get("worker_error"), "closure": closure,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--scope-sha256", required=True)
    parser.add_argument("--max-seconds", type=int, required=True,
                        help="deadline for new admission; the frozen scope is the only admission bound")
    parser.add_argument("--activation-role", choices=("candidate", "production"), default="candidate",
                        help="label recorded in the receipt; it does not change any runtime behaviour")
    parser.add_argument("--receipt-out", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, default=None,
                        help="existence of this file closes new admission; accepted work still drains")
    parser.add_argument("--observation-out", type=Path, default=None,
                        help="new directory under runtime root for stage/progress observation (measurement only)")
    parser.add_argument("--observation-max-events", type=int, default=65536)
    parser.add_argument("--observation-max-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--m6-run-dir", type=Path, default=None,
                        help="controller-prepared M6 run directory (anchor, run-spec, roles, transport); "
                             "binds this run to the owner as e2e_runner and requires --observation-out")
    args = parser.parse_args(argv)
    settings = load_settings()
    try:
        manifest, scope = load_campaign_inputs(
            manifest_bytes=_read_pinned(args.manifest, "manifest"), manifest_sha256=args.manifest_sha256,
            scope_bytes=_read_pinned(args.scope, "scope"), scope_sha256=args.scope_sha256,
        )
        request = CampaignRunRequest(
            manifest=manifest, scope=scope, max_seconds=args.max_seconds, activation_role=args.activation_role,
        )
        m6_run = None if args.m6_run_dir is None else load_m6_run_directory(args.m6_run_dir.absolute())
    except (CampaignInputError, ValueError, OSError) as exc:
        print(json.dumps({"campaign_input_error": type(exc).__name__, "message": str(exc)}), flush=True)
        return 2
    if m6_run is not None and args.observation_out is None:
        parser.error("--m6-run-dir requires --observation-out for the assembly spool")
    receipt = args.receipt_out.absolute()
    if (receipt.parent.resolve(strict=True) != receipt.parent
            or not receipt.is_relative_to(settings.disclosure_runtime_root.resolve())):
        parser.error("--receipt-out requires an existing canonical directory under runtime root")
    if receipt.exists() or receipt.is_symlink():
        parser.error("receipt already exists; retain the previous run evidence")
    observation_dir: Path | None = None
    if args.observation_out is not None:
        observation_dir = args.observation_out.absolute()
        if (observation_dir.parent.resolve(strict=True) != observation_dir.parent
                or not observation_dir.is_relative_to(settings.disclosure_runtime_root.resolve())):
            parser.error("--observation-out requires an existing canonical parent under runtime root")
        if observation_dir.exists() or observation_dir.is_symlink():
            parser.error("observation directory already exists; retain the previous evidence")
        if not 1 <= args.observation_max_events <= 10_000_000 or not 4096 <= args.observation_max_bytes <= 2**31:
            parser.error("observation bounds are out of range")
    _write_new(receipt.with_name(receipt.name + ".intent.json"), {
        "contract_version": "staged-v4-campaign-intent.v1",
        "campaign_id": scope.campaign_id, "manifest_sha256": args.manifest_sha256,
        "scope_sha256": args.scope_sha256, "max_seconds": request.max_seconds,
        "activation_role": request.activation_role,
        "m6_run": None if m6_run is None else {"run_id": m6_run.require_spec().run_id,
                                               "spec_sha256": m6_run.require_spec().canonical_sha256(), "pins": m6_run.pins},
        "created_at": datetime.now(UTC).isoformat(),
    })
    stopped = False

    def stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stopped
        stopped = True

    def external_stop() -> bool:
        return stopped or (args.stop_file is not None and args.stop_file.exists())

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    observer: JsonlStageObserver | None = None
    progress_recorder: ProgressRecorder | None = None
    try:
        progress_hook = None
        committed_hook = None
        if observation_dir is not None:
            observation_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
            observer = JsonlStageObserver(observation_dir, max_events=args.observation_max_events,
                                          max_bytes=args.observation_max_bytes)
            try:
                progress_recorder = ProgressRecorder(observation_dir)
            except BaseException:
                observer.close()
                raise
            held_observer, held_progress = observer, progress_recorder
            progress_hook = held_progress.record
            committed_hook = held_progress.prune_signal
            inner_stop = external_stop

            def external_stop() -> bool:  # noqa: F811 - measurement failure also closes new admission
                return inner_stop() or held_observer.writer_failed
        try:
            result = run_campaign(settings, request=request, external_stop=external_stop,
                                  progress=progress_hook, publication_committed=committed_hook,
                                  stage_observer=observer, m6_run=m6_run,
                                  m6_spool_dir=None if observation_dir is None or m6_run is None else observation_dir / "m6-assembly")
        except CampaignInputError as exc:
            print(json.dumps({"campaign_input_error": type(exc).__name__, "message": str(exc)}), flush=True)
            return 2
        _write_new(receipt, result)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    finally:
        observation_summary: dict[str, Any] = {}
        closure_errors: list[str] = []
        if progress_recorder is not None:
            try:
                observation_summary.update(progress_recorder.close())
            except BaseException as exc:  # noqa: BLE001 - recorded, never masks the business outcome
                closure_errors.append("progress:" + type(exc).__name__)
        if observer is not None and not observer.closed:
            try:
                observation_summary.update(observer.close())
            except BaseException as exc:  # noqa: BLE001 - recorded, never masks the business outcome
                closure_errors.append("observer:" + type(exc).__name__)
        if observation_dir is not None:
            observation_summary["closure_errors"] = closure_errors
            observer_status = observation_summary.pop("measurement_status", None)
            if observer_status is not None:
                observation_summary["observer_status"] = observer_status
            components = [observer_status, observation_summary.get("progress_status")]
            if closure_errors or None in components or any(
                status not in ("complete", "partial") for status in components
            ):
                observation_summary["measurement_status"] = "invalid"
            elif "partial" in components:
                observation_summary["measurement_status"] = "partial"
            else:
                observation_summary["measurement_status"] = "complete"
            try:
                print(json.dumps({"observation": observation_summary}, sort_keys=True), flush=True)
            except Exception:  # noqa: BLE001 - stdout loss cannot block signal restoration
                pass
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
