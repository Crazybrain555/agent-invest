"""Runner-side formal closure: control receipts, deposits and admission close on the owner.

After the coordinator has drained and every lifecycle fact was delivered, the
runner deposits its resource audit, any unresolved claims and the admission
reconciliation, acknowledges ``admission_closed`` and finally deposits the
ownership closure the controller needs to close the run. Every receipt is
built from the spool and the coordinator's own result; nothing is invented and
a refused step stops the sequence with the exact step named.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from disclosure_anchor.application.contracts.m6_control_receipts import (
    M6AdmissionReconciliationReceipt, M6OwnershipClosureReceipt, M6ResourceAuditReceipt, M6UnresolvedClaim,
    M6UnresolvedClaimsReceipt, attempt_set_sha256,
)
from disclosure_anchor.application.contracts.m6_owner import M6AdmissionClosedAck, M6OwnerAnchor, M6OwnerControl
from disclosure_anchor.application.contracts.m6_run import M6RunSpec
from disclosure_anchor.application.contracts.m6_run_events import M6EventPayload, M6VerifierDrained
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.services.staged_parse_coordinator import CoordinatorResult
from disclosure_anchor.adapters.runtime.m6_e2e_assembly import M6E2EAssemblyWorker, M6LifecycleSpool
from disclosure_anchor.adapters.runtime.m6_owner_protocol import (
    M6CallerRole, M6LeasePolicy, M6OwnerClient, M6OwnerProtocolError, M6OwnerRejected,
)
from disclosure_anchor.adapters.runtime.m6_owner_ssh import m6_ssh_owner_transport
from disclosure_anchor.adapters.runtime.resident_ssh_http import ResidentSSHConfig, _read_private_config

_RUN_ROLES = ("controller", "e2e_runner", "public_verifier", "quality_verifier")
_MAX_RUN_FILE_BYTES = 65536


DRAIN_RECEIPT_CONTRACT = "m6.verifier-drain-receipt.v1"


@dataclass(frozen=True, slots=True)
class M6RunRole:
    epoch_sha256: str
    token_path: str


@dataclass(frozen=True, slots=True)
class M6RunDirectory:
    """Controller-prepared, hash-pinned inputs every principal needs to talk to the owner.

    The owner keys its private tokens by (role, epoch), so each role carries
    its own epoch and token file; no token bytes ever enter this record.
    """

    path: Path
    anchor: M6OwnerAnchor
    spec: M6RunSpec | None
    roles: dict[str, M6RunRole]
    ssh: ResidentSSHConfig
    remote_port: int
    lease: M6LeasePolicy
    pins: dict[str, str]

    def epoch(self, role: str) -> str:
        return self.roles[role].epoch_sha256

    def token_path(self, role: str) -> str:
        return self.roles[role].token_path

    def require_spec(self) -> M6RunSpec:
        if self.spec is None:
            raise ValueError("M6 run directory has no bound run spec yet")
        return self.spec


def _pinned(path: Path, *, secret: bool) -> tuple[bytes, str]:
    raw = _read_private_config(str(path), secret=secret).encode("utf-8")
    return raw, "sha256:" + hashlib.sha256(raw).hexdigest()


def load_m6_run_directory(path: Path, *, require_spec: bool = True) -> M6RunDirectory:
    """Load anchor, roles, transport and (once bound) the run spec with private-file checks."""
    path = Path(path)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("M6 run directory must be an absolute existing directory")
    pins: dict[str, str] = {}
    anchor_raw, pins["anchor.json"] = _pinned(path / "anchor.json", secret=False)
    roles_raw, pins["roles.json"] = _pinned(path / "roles.json", secret=True)
    transport_raw, pins["transport.json"] = _pinned(path / "transport.json", secret=True)
    anchor = M6OwnerAnchor.from_canonical_bytes(anchor_raw, maximum_bytes=_MAX_RUN_FILE_BYTES)
    spec: M6RunSpec | None = None
    if (path / "run-spec.json").exists() or require_spec:
        spec_raw, pins["run-spec.json"] = _pinned(path / "run-spec.json", secret=False)
        spec = M6RunSpec.from_canonical_bytes(spec_raw, maximum_bytes=_MAX_RUN_FILE_BYTES)
        anchor.assert_spec(spec)
    roles_value = strict_json_loads(roles_raw.decode("utf-8"))
    if type(roles_value) is not dict or set(roles_value) != set(_RUN_ROLES):
        raise ValueError("M6 roles file must name exactly controller, e2e_runner, public_verifier and quality_verifier")
    roles: dict[str, M6RunRole] = {}
    for role, value in roles_value.items():
        if (
            type(value) is not dict or set(value) != {"epoch_sha256", "token_path"}
            or type(value["epoch_sha256"]) is not str or len(value["epoch_sha256"]) != 71
            or not value["epoch_sha256"].startswith("sha256:")
            or type(value["token_path"]) is not str or not Path(value["token_path"]).is_absolute()
        ):
            raise ValueError(f"M6 role {role} requires an epoch hash and an absolute token path")
        roles[role] = M6RunRole(epoch_sha256=value["epoch_sha256"], token_path=value["token_path"])
    transport = strict_json_loads(transport_raw.decode("utf-8"))
    if type(transport) is not dict or set(transport) != {"ssh", "remote_port", "lease"}:
        raise ValueError("M6 transport file fields are not closed")
    ssh = transport["ssh"]
    if type(ssh) is not dict or set(ssh) != {"address", "port", "username", "private_key_path", "known_hosts_path"}:
        raise ValueError("M6 transport ssh fields are not closed")
    lease = transport["lease"]
    if type(lease) is not dict or not {"stop_propagation_reserve_ns"} <= set(lease) <= {
        "stop_propagation_reserve_ns", "maximum_lease_ns", "uncertainty_margin_ns", "maximum_clock_drift_ppm",
    } or any(isinstance(v, bool) or type(v) is not int for v in lease.values()):
        raise ValueError("M6 transport lease policy must be explicit integers")
    if isinstance(transport["remote_port"], bool) or type(transport["remote_port"]) is not int:
        raise ValueError("M6 transport port is invalid")
    return M6RunDirectory(
        path=path, anchor=anchor, spec=spec, roles=roles, ssh=ResidentSSHConfig(**ssh),
        remote_port=transport["remote_port"], lease=M6LeasePolicy(**lease), pins=pins,
    )


def m6_owner_client_factory(
    run: M6RunDirectory, *, role: M6CallerRole, continuous_ns: Callable[[], int],
) -> Callable[[], M6OwnerClient]:
    """A factory the caller invokes on the thread that will own the client; nothing is opened here."""
    if role not in _RUN_ROLES:
        raise ValueError("M6 role is not one of the run's principals")
    spec = run.require_spec()
    epoch, token_path = run.epoch(role), run.token_path(role)

    def factory() -> M6OwnerClient:
        transport = m6_ssh_owner_transport(
            config=run.ssh, token_path=token_path, remote_port=run.remote_port, continuous_ns=continuous_ns,
        )
        return M6OwnerClient(
            anchor=run.anchor, spec=spec, transport=transport, caller_role=role, producer_epoch_sha256=epoch,
            continuous_ns=continuous_ns, lease_policy=run.lease,
        )

    return factory

_FINAL_STATES = frozenset({
    "acked", "remote_failed", "local_failed", "pre_submission_failed", "preparation_failed", "superseded",
})


@dataclass(frozen=True, slots=True)
class RunnerClosureReceipts:
    resource_audit: M6ResourceAuditReceipt
    unresolved_claims: M6UnresolvedClaimsReceipt | None
    admission_reconciliation: M6AdmissionReconciliationReceipt
    ownership_closure: M6OwnershipClosureReceipt


class M6RunnerClosureFailed(RuntimeError):
    def __init__(self, step: str, reason: str) -> None:
        super().__init__(f"{step}: {reason}")
        self.step, self.reason = step, reason


def build_runner_closure_receipts(
    *, run_id: str, spec_sha256: str, runner_epoch_sha256: str, owner_identity: str, spool: M6LifecycleSpool,
    result: CoordinatorResult, scratch_residual_count: int, children_exited: bool,
) -> RunnerClosureReceipts:
    """Derive the four control receipts from durable facts and the coordinator result.

    Admitted and final sets come from the spool's delivered facts; unresolved
    claims are attempts admitted in this run whose final fact never arrived,
    named with the coordinator's last known state when it reported one.
    """
    admitted = spool.attempt_ids("attempt_admitted")
    final = spool.attempt_ids("attempt_final")
    known_states = dict(result.final_states)
    unresolved_ids = tuple(sorted(set(admitted) - set(final)))
    in_flight = sum(1 for _ in unresolved_ids)
    audit = M6ResourceAuditReceipt(
        run_id=run_id, spec_sha256=spec_sha256, runner_epoch_sha256=runner_epoch_sha256, owner_identity=owner_identity,
        coordinator_terminal=result.terminal.value, in_flight_count=in_flight,
        credits_in_use_zero=not result.credits_in_use.nonzero(), scratch_residual_count=scratch_residual_count,
        children_exited=children_exited,
    )
    unresolved: M6UnresolvedClaimsReceipt | None = None
    if unresolved_ids:
        unresolved = M6UnresolvedClaimsReceipt(
            run_id=run_id, spec_sha256=spec_sha256, runner_epoch_sha256=runner_epoch_sha256,
            unresolved_attempt_count=len(unresolved_ids), unresolved_attempt_set_sha256=attempt_set_sha256(unresolved_ids),
            attempts=tuple(
                M6UnresolvedClaim(attempt_id=item, state=known_states.get(item, "unknown")) for item in unresolved_ids
            ),
        )
    reconciliation = M6AdmissionReconciliationReceipt(
        run_id=run_id, spec_sha256=spec_sha256, runner_epoch_sha256=runner_epoch_sha256,
        last_producer_sequence=spool.last_delivered_sequence(), admitted_attempt_count=len(admitted),
        admitted_attempt_set_sha256=attempt_set_sha256(admitted), unresolved_claim_count=len(unresolved_ids),
        unresolved_receipt_sha256=None if unresolved is None else unresolved.canonical_sha256(),
    )
    ownership = M6OwnershipClosureReceipt(
        run_id=run_id, spec_sha256=spec_sha256, runner_epoch_sha256=runner_epoch_sha256,
        admitted_attempt_count=len(admitted), admitted_attempt_set_sha256=attempt_set_sha256(admitted),
        final_attempt_count=len(final), final_attempt_set_sha256=attempt_set_sha256(final),
        residual_count=scratch_residual_count + in_flight, children_exited=children_exited,
        resource_audit_sha256=audit.canonical_sha256(),
    )
    return RunnerClosureReceipts(audit, unresolved, reconciliation, ownership)


def execute_runner_closure(client: M6OwnerClient, receipts: RunnerClosureReceipts, *, request_stop: bool) -> dict[str, Any]:
    """Deposit, acknowledge and deposit again, in the order the owner enforces.

    Runs on the client's owning thread. Each owner reply is recorded under
    its step; the first refusal or transport failure raises
    ``M6RunnerClosureFailed`` naming that step, and nothing later is attempted.
    """
    record: dict[str, Any] = {"steps": []}

    def step(name: str, action: Any) -> Any:
        try:
            reply = action()
        except M6OwnerRejected as exc:
            record["steps"].append({"step": name, "outcome": exc.reply.outcome, "error_code": exc.reply.error_code})
            raise M6RunnerClosureFailed(name, f"owner {exc.reply.outcome}: {exc.reply.error_code}") from exc
        except (M6OwnerProtocolError, EOFError, OSError, RuntimeError, TimeoutError) as exc:
            record["steps"].append({"step": name, "outcome": "transport", "error": f"{type(exc).__name__}:{exc}"[:300]})
            raise M6RunnerClosureFailed(name, f"{type(exc).__name__}:{exc}"[:300]) from exc
        record["steps"].append({"step": name, "outcome": "ok"})
        return reply

    if request_stop:
        step("stop", lambda: client.request(M6OwnerControl(kind="stop")))
    audit_text = receipts.resource_audit.canonical_bytes().decode("utf-8")
    step("deposit_resource_audit", lambda: client.deposit("resource_audit", audit_text))
    record["resource_audit_sha256"] = receipts.resource_audit.canonical_sha256()
    if receipts.unresolved_claims is not None:
        unresolved_text = receipts.unresolved_claims.canonical_bytes().decode("utf-8")
        step("deposit_unresolved_claims", lambda: client.deposit("unresolved_claims", unresolved_text))
        record["unresolved_claims_sha256"] = receipts.unresolved_claims.canonical_sha256()
    reconciliation_text = receipts.admission_reconciliation.canonical_bytes().decode("utf-8")
    step("deposit_admission_reconciliation", lambda: client.deposit("admission_reconciliation", reconciliation_text))
    record["admission_reconciliation_sha256"] = receipts.admission_reconciliation.canonical_sha256()
    reconciliation = receipts.admission_reconciliation
    step("admission_closed", lambda: client.request(M6AdmissionClosedAck(
        runner_epoch_sha256=reconciliation.runner_epoch_sha256,
        last_producer_sequence=reconciliation.last_producer_sequence,
        admitted_attempt_count=reconciliation.admitted_attempt_count,
        unresolved_claim_count=reconciliation.unresolved_claim_count,
        reconciliation_receipt_sha256=reconciliation.canonical_sha256(),
    )))
    ownership_text = receipts.ownership_closure.canonical_bytes().decode("utf-8")
    step("deposit_ownership_closure", lambda: client.deposit("ownership_closure", ownership_text))
    record["ownership_closure_sha256"] = receipts.ownership_closure.canonical_sha256()
    record["residual_count"] = receipts.ownership_closure.residual_count
    record["children_exited"] = receipts.ownership_closure.children_exited
    record["admitted_attempt_count"] = receipts.ownership_closure.admitted_attempt_count
    record["final_attempt_count"] = receipts.ownership_closure.final_attempt_count
    record["complete"] = True
    return record


class M6VerifierAssembly:
    """One verifier role's spool and sender: record closed payloads, complete delivery, and drain only as the drain role.

    Evidence-sender completion and the terminal drain are distinct. Every
    verifier role completes: all recorded events are delivered and the sender
    closes. Only the run's drain role (`public_verifier` in e2e publication,
    `quality_verifier` in service diagnostics, as the owner and the reducer
    require) may additionally declare `verifier_drained`, and only after every
    downstream verifier has completed, because attempt evidence after the drain
    is invalid.
    """

    def __init__(
        self, run: M6RunDirectory, *, role: Literal["public_verifier", "quality_verifier"], spool_dir: Path,
        max_events: int, continuous_ns: Callable[[], int],
    ) -> None:
        spec = run.require_spec()
        self._role = role
        self._drain_role = drain_role_for_mode(spec.mode)
        self._spool = M6LifecycleSpool(
            spool_dir, run_id=spec.run_id, spec_sha256=spec.canonical_sha256(), producer_epoch_sha256=run.epoch(role),
            max_facts=max_events, producer_kind=role,
        )
        self._worker = M6E2EAssemblyWorker(
            self._spool, client_factory=m6_owner_client_factory(run, role=role, continuous_ns=continuous_ns),
        )
        self._run, self._started = run, False

    def start(self) -> None:
        self._worker.start()
        self._started = True

    def record(self, payload: M6EventPayload, *, attempt_id: str) -> None:
        self._spool.record_event(payload, attempt_id=attempt_id)

    @property
    def failed(self) -> bool:
        return self._spool.failed or self._worker.failed

    @property
    def role(self) -> str:
        return self._role

    @property
    def is_drain_role(self) -> bool:
        return self._role == self._drain_role

    def complete(self, *, deadline_seconds: float = 300.0) -> dict[str, Any]:
        """Deliver every recorded event and close the sender; no drain is claimed."""
        status = self._worker.close(deadline_seconds) if self._started else {"worker_error": "worker never started"}
        spool_status = self._spool.close()
        failed = bool(spool_status["failed"]) or status.get("worker_error") is not None
        return {**self._identity(), "status": "failed" if failed else "complete", "terminal_drain": False,
                "drain_receipt_sha256": None, "spool": spool_status, "worker_error": status.get("worker_error")}

    def abort(self, reason: str, *, deadline_seconds: float = 30.0) -> dict[str, Any]:
        """Close the sender without any drain claim; the reason stays visible in the spool and status."""
        self._spool.note_failure("verifier aborted before drain: " + reason[:300])
        status = self._worker.close(deadline_seconds) if self._started else {"worker_error": "worker never started"}
        spool_status = self._spool.close()
        return {**self._identity(), "status": "failed", "terminal_drain": False, "drain_receipt_sha256": None,
                "abort_reason": reason[:300], "spool": spool_status, "worker_error": status.get("worker_error")}

    def finish(self, drain_receipt_sha256: str, *, deadline_seconds: float = 300.0) -> dict[str, Any]:
        """Declare the terminal drain after every recorded event is delivered; failure stays visible.

        Only the run's drain role may call this, and only once every downstream
        verifier has completed; the caller binds that completion into the drain
        receipt. Another role asking to drain is a wiring error, refused before
        anything is sent.
        """
        if not self.is_drain_role:
            raise M6VerifierRoleError(
                f"{self._role} is not the drain role of a {self._run.require_spec().mode} run ({self._drain_role} is)"
            )
        if not self._spool.failed:
            self._spool.record_event(M6VerifierDrained(drain_receipt_sha256=drain_receipt_sha256), attempt_id=self._role)
        status = self._worker.close(deadline_seconds) if self._started else {"worker_error": "worker never started"}
        spool_status = self._spool.close()
        failed = bool(spool_status["failed"]) or status.get("worker_error") is not None
        return {**self._identity(), "status": "failed" if failed else "complete", "terminal_drain": True,
                "drain_receipt_sha256": drain_receipt_sha256, "spool": spool_status,
                "worker_error": status.get("worker_error")}

    def _identity(self) -> dict[str, Any]:
        spec = self._run.require_spec()
        return {
            "run_id": spec.run_id, "spec_sha256": spec.canonical_sha256(),
            "anchor_sha256": self._run.anchor.canonical_sha256(), "producer_kind": self._role,
            "producer_epoch_sha256": self._run.epoch(self._role), "run_directory_pins": self._run.pins,
        }


class M6VerifierRoleError(ValueError):
    """A verifier asked for a closing step its role or the run's mode does not allow."""


def drain_role_for_mode(mode: str) -> Literal["public_verifier", "quality_verifier"]:
    """The single role that may declare `verifier_drained`; mirrors the owner's PayloadRole and the reducer."""
    if mode == "e2e_publication":
        return "public_verifier"
    if mode == "service_diagnostic":
        return "quality_verifier"
    raise ValueError("M6 run mode has no drain role")



def close_verifier_assembly(
    assembly: M6VerifierAssembly, *, output_dir: Path, summary: dict[str, Any], producer_kind: str,
    verifier_identity: str, exit_code: int, drained: bool, write: Callable[[Path, bytes], None],
    terminal: bool = False, downstream: dict[str, Any] | None = None,
) -> int:
    """Turn the attempt loop's outcome into exactly one closing observation.

    A loop that ran to its end completes its evidence sender. Only when
    ``terminal`` is requested, by the run's drain role and with every downstream
    verifier's completion bound in ``downstream``, is the immutable drain receipt
    written first and then ``verifier_drained`` claimed with that file's digest.
    An interrupted loop, or a receipt that could not be written whole, aborts the
    sender instead, so no drain is ever claimed for evidence that does not
    exist. The original failure stays the reported error; a failing abort is
    recorded beside it rather than replacing it.
    """
    if not drained:
        summary["m6_assembly"] = assembly.abort("verification did not run to its end")
        return max(exit_code, 1)
    if not terminal:
        summary["m6_assembly"] = assembly.complete()
        return exit_code if summary["m6_assembly"]["status"] == "complete" else max(exit_code, 1)
    if not assembly.is_drain_role:
        raise M6VerifierRoleError(f"{assembly.role} cannot declare the terminal drain of this run")
    receipt_path = output_dir / "drain-receipt.json"
    raw = json.dumps({
        "contract_version": DRAIN_RECEIPT_CONTRACT, "producer_kind": producer_kind,
        "verifier_identity": verifier_identity, "started_utc": summary["started_utc"],
        "finished_utc": summary["finished_utc"], "attempts": summary["attempts"],
        "exit_code_before_assembly": exit_code, "downstream": downstream,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        write(receipt_path, raw)
    except BaseException as exc:
        reason = "drain receipt not written: " + f"{type(exc).__name__}:{exc}"[:200]
        try:
            summary["m6_assembly"] = assembly.abort(reason)
        except Exception as abort_exc:  # noqa: BLE001 - the receipt failure must stay the reported error
            summary["m6_assembly"] = {"status": "failed", "abort_reason": reason,
                                      "abort_error": f"{type(abort_exc).__name__}:{abort_exc}"[:300]}
        raise
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    summary["drain_receipt"] = {"path": str(receipt_path), "sha256": digest}
    summary["m6_assembly"] = assembly.finish(digest)
    return exit_code if summary["m6_assembly"]["status"] == "complete" else max(exit_code, 1)


__all__ = [
    "DRAIN_RECEIPT_CONTRACT", "M6VerifierRoleError", "close_verifier_assembly", "drain_role_for_mode",
    "M6RunDirectory", "M6RunRole", "M6RunnerClosureFailed", "M6VerifierAssembly", "RunnerClosureReceipts", "build_runner_closure_receipts",
    "execute_runner_closure", "load_m6_run_directory", "m6_owner_client_factory",
]
