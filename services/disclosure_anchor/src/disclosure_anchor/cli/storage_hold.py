"""Operator decision for one held native storage task: preview, then execute.

    operator-verifier --operator-token-file FILE
    preview  --attempt-id ID --operator-token-file FILE [--out FILE]
    execute  --attempt-id ID --operator-token-file FILE --expect-preview-sha256 SHA
             --decided-by NAME --reason TEXT --fixed-by TEXT --out FILE
    recover  --attempt-id ID --out FILE

The worker never calls this and never holds the operator credential: it is an
owner-only 0600 file the operator names explicitly, never a setting or an
environment variable. ``operator-verifier`` prints only the credential's
sha256, which the operator enrolls inside the native API container; until then
the native route refuses every caller. preview and execute send the credential
as a bearer, read the attempt's durable V4 head (read-only) and require a
submitted attempt without a live claim; the native task, key, attempt and fence
must be exactly that attempt's accepted task. The native API refuses anything
but the reviewed held task and makes the decision durable before it answers.
``execute`` never resumes, reparses or deletes: the task fails with a closed
cause naming this decision, and its bytes stay until the worker's ordinary
failed-task ACK once the F5 stop is released. The native registry keeps the
canonical decision in the task's closed failure cause: a lost answer is
reconciled from the ordinary status route, and ``recover`` rebuilds the receipt
from that durable decision alone. A receipt is never overwritten; replaying the
same decision reproduces it exactly. Exit 0 = done, 1 = refused.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

import httpx

from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
    require_runtime_app_engine,
)
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    MinerUProtocolV2WireError,
    api_origin_from_task_routes_v2,
    decode_task_failure_cause_v2,
    response_identity_v2,
    task_status_url_v2,
)
from disclosure_anchor.adapters.runtime.exact_file_write import publish_new_exact
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import (
    MinerUDeploymentGateError,
    read_owner_only_evidence,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.application.ports.remote_provider_v4 import (
    NATIVE_STORAGE_HOLD_REASONS,
    STORAGE_HOLD_DECISION_SCHEMA,
    STORAGE_HOLD_TERMINATED_CAUSE_CODE,
    StorageHoldDecisionV1,
)
from disclosure_anchor.application.ports.unit_of_work import UnitOfWork
from disclosure_anchor.settings import load_settings

PREVIEW_SCHEMA = "mineru.storage-hold-preview.v1"
DECISION_SCHEMA = STORAGE_HOLD_DECISION_SCHEMA
NATIVE_RECEIPT_SCHEMA = "mineru.storage-hold-decision-receipt.v1"
OPERATOR_PREVIEW_SCHEMA = "disclosure.storage-hold-preview.v1"
OPERATOR_RECEIPT_SCHEMA = "disclosure.storage-hold-decision.v1"
_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_RESPONSE_BYTES = 64 * 1024
# A dedicated credential for this one destination (never the admin API token
# or the tunnel key), e.g. secrets.token_urlsafe(32): 43+ URL-safe characters.
_OPERATOR_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")
_MAX_OPERATOR_CREDENTIAL_FILE_BYTES = 256


class StorageHoldRefused(RuntimeError):
    """The decision cannot be previewed or applied as named."""


class _NativeUnavailable(StorageHoldRefused):
    """No answer arrived: a decision sent may or may not be durable natively."""


def read_operator_credential(path: Path) -> str:
    """The operator bearer credential from its owner-only 0600 file; its value never appears in errors."""

    try:
        raw, _identity = read_owner_only_evidence(
            path, label="storage-hold operator credential", max_bytes=_MAX_OPERATOR_CREDENTIAL_FILE_BYTES,
        )
    except (MinerUDeploymentGateError, OSError) as exc:
        raise StorageHoldRefused(f"operator credential file is unusable: {exc}") from None
    text = raw[:-1] if raw.endswith(b"\n") else raw
    try:
        credential = text.decode("ascii")
    except UnicodeDecodeError:
        credential = ""
    if _OPERATOR_CREDENTIAL.fullmatch(credential) is None:
        raise StorageHoldRefused(
            "operator credential must be one line of 43-128 URL-safe characters"
        )
    return credential


def operator_verifier(credential: str) -> str:
    """What the native container enrolls: the credential's sha256, never the credential."""

    return "sha256:" + hashlib.sha256(credential.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class BoundAttempt:
    attempt_id: str
    document_id: str
    fence_identity: str
    source_pdf_sha256: str
    client_submit_key: str
    remote_task_identity: str
    api_origin: str


def _canonical_sha256(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def decision_sha256(*, preview_sha256: str, decided_by: str, reason: str, fixed_by: str) -> str:
    """The identity the native registry records for exactly this decision."""

    return _decision(
        preview_sha256=preview_sha256, decided_by=decided_by, reason=reason, fixed_by=fixed_by,
    ).sha256


def _decision(*, preview_sha256: str, decided_by: str, reason: str, fixed_by: str) -> StorageHoldDecisionV1:
    try:
        return StorageHoldDecisionV1(
            preview_sha256=preview_sha256, decided_by=decided_by, reason=reason, fixed_by=fixed_by,
        )
    except ValueError as exc:
        raise StorageHoldRefused(str(exc)) from None


def bound_attempt(uow_factory: Callable[[], UnitOfWork], attempt_id: str) -> BoundAttempt:
    """The attempt's accepted native task, only while no live worker owns it."""

    with uow_factory() as uow:
        authority = uow.remote_parse_v4.load(attempt_id)
    if authority.state != "submitted":
        raise StorageHoldRefused(f"attempt {attempt_id} is {authority.state}, not a submitted provider task")
    lease = authority.database_lease
    if authority.claim_owner_identity is not None and (lease is None or lease.remaining_microseconds > 0):
        raise StorageHoldRefused(f"attempt {attempt_id} may be claimed by a live worker; stop the worker first")
    accepted = [item.value for item in authority.evidence if item.kind == "accepted_submission"]
    if len(accepted) != 1 or type(accepted[0]) is not AcceptedSubmissionReceiptV4:
        raise StorageHoldRefused(f"attempt {attempt_id} lacks one exact accepted submission")
    receipt = accepted[0]
    if (receipt.attempt_id, receipt.fence_identity) != (authority.attempt_id, authority.fence_identity):
        raise StorageHoldRefused(f"attempt {attempt_id} accepted submission is bound to another attempt")
    try:
        api_origin = api_origin_from_task_routes_v2(
            status_url=receipt.status_url, result_url=receipt.result_url,
            task_id=receipt.remote_task_identity,
        )
    except MinerUProtocolV2WireError as exc:
        raise StorageHoldRefused("accepted task routes are not canonical") from exc
    return BoundAttempt(
        attempt_id=authority.attempt_id,
        document_id=authority.document_id,
        fence_identity=authority.fence_identity,
        source_pdf_sha256=authority.source_pdf_sha256,
        client_submit_key=authority.client_submit_key,
        remote_task_identity=receipt.remote_task_identity,
        api_origin=api_origin,
    )


def _post(
    client: httpx.Client, bound: BoundAttempt, body: dict[str, Any], *, credential: str,
) -> dict[str, Any]:
    url = f"{bound.api_origin}/agent/storage-holds/{bound.remote_task_identity}"
    try:
        response = client.post(
            url, json=body, headers={"Authorization": f"Bearer {credential}"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.TransportError as exc:
        raise _NativeUnavailable(f"native API was unavailable: {type(exc).__name__}") from exc
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise StorageHoldRefused("native API answer exceeds its bound")
    if response.status_code == 401:
        raise StorageHoldRefused("native API rejected the operator credential")
    if response.status_code == 403:
        raise StorageHoldRefused(
            "native operator gate refused (disabled or misconfigured): enroll this credential's "
            f"verifier in the API container first; {response.text[:512]}"
        )
    if response.status_code != 200:
        raise StorageHoldRefused(
            f"native API refused with HTTP {response.status_code}: {response.text[:512]}"
        )
    try:
        payload = json.loads(response.content)
    except ValueError as exc:
        raise StorageHoldRefused("native API answer is not JSON") from exc
    if type(payload) is not dict:
        raise StorageHoldRefused("native API answer is not an object")
    return payload


def _require_task(payload: dict[str, Any], bound: BoundAttempt, schema: str) -> None:
    observed = (
        payload.get("schema"), payload.get("task_id"), payload.get("idempotency_key"),
        payload.get("attempt_identity"), payload.get("fence_identity"),
    )
    expected = (
        schema, bound.remote_task_identity, bound.client_submit_key, bound.attempt_id, bound.fence_identity,
    )
    if observed != expected:
        raise StorageHoldRefused("native answer is not this attempt's accepted task")


def preview(bound: BoundAttempt, client: httpx.Client, *, credential: str) -> dict[str, Any]:
    native = _post(client, bound, {"mode": "preview"}, credential=credential)
    _require_task(native, bound, PREVIEW_SCHEMA)
    facts = {key: value for key, value in native.items() if key != "preview_sha256"}
    if native.get("preview_sha256") != _canonical_sha256(facts):
        raise StorageHoldRefused("native preview digest does not match its facts")
    return {
        "schema": OPERATOR_PREVIEW_SCHEMA,
        "attempt_id": bound.attempt_id,
        "document_id": bound.document_id,
        "source_pdf_sha256": bound.source_pdf_sha256,
        "native": native,
    }


def _operator_receipt(
    bound: BoundAttempt, *, hold_reason: str, decision: StorageHoldDecisionV1,
) -> dict[str, Any]:
    """The operator receipt, built only from the durable native decision."""

    return {
        "schema": OPERATOR_RECEIPT_SCHEMA,
        "attempt_id": bound.attempt_id,
        "document_id": bound.document_id,
        "source_pdf_sha256": bound.source_pdf_sha256,
        "remote_task_identity": bound.remote_task_identity,
        "client_submit_key": bound.client_submit_key,
        "fence_identity": bound.fence_identity,
        "hold_reason": hold_reason,
        "preview_sha256": decision.preview_sha256,
        "decision_sha256": decision.sha256,
        "decided_by": decision.decided_by,
        "reason": decision.reason,
        "fixed_by": decision.fixed_by,
    }


def recover_decision(
    bound: BoundAttempt, client: httpx.Client,
) -> tuple[str, StorageHoldDecisionV1, str] | None:
    """Read back the durable decision from the task's ordinary status; None if none is recorded.

    The native registry keeps the canonical decision in the failed task's
    closed cause, so a lost response is recovered with its exact original
    attribution, not guessed from a digest.
    """

    url = task_status_url_v2(api_origin=bound.api_origin, task_id=bound.remote_task_identity)
    try:
        response = client.get(url, timeout=_REQUEST_TIMEOUT_SECONDS)
    except httpx.TransportError as exc:
        raise _NativeUnavailable(f"native status was unavailable: {type(exc).__name__}") from exc
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise StorageHoldRefused("native status answer exceeds its bound")
    if response.status_code != 200:
        raise StorageHoldRefused(f"native status refused with HTTP {response.status_code}")
    try:
        payload = strict_json_loads(response.content)
    except ValueError as exc:
        raise StorageHoldRefused("native status answer is not JSON") from exc
    if type(payload) is not dict or (
        payload.get("task_id"), payload.get("idempotency_key"),
        payload.get("attempt_identity"), payload.get("fence_identity"),
    ) != (bound.remote_task_identity, bound.client_submit_key, bound.attempt_id, bound.fence_identity):
        raise StorageHoldRefused("native status is not this attempt's accepted task")
    raw_cause = payload.get("failure_cause")
    if payload.get("status") != "failed" or raw_cause is None:
        return None
    response_sha256, response_bytes = response_identity_v2(response.content)
    try:
        cause = decode_task_failure_cause_v2(
            raw_cause, task_id=bound.remote_task_identity,
            response_sha256=response_sha256, response_byte_count=response_bytes,
        )
    except MinerUProtocolV2WireError as exc:
        raise StorageHoldRefused(f"native failure cause is outside its contract: {exc}") from exc
    if cause.code != STORAGE_HOLD_TERMINATED_CAUSE_CODE or cause.decision is None or cause.hold_reason is None:
        raise StorageHoldRefused("task failed for another cause, not a hold decision")
    state = payload.get("protocol_state")
    return cause.hold_reason, cause.decision, state if type(state) is str else "failed"


def execute(
    bound: BoundAttempt,
    client: httpx.Client,
    *,
    credential: str,
    expected_preview_sha256: str,
    decided_by: str,
    reason: str,
    fixed_by: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply the decision; return (the durable receipt, the native answer).

    A lost answer is reconciled through the task's ordinary status: only a
    durable decision equal to this one completes the command.
    """

    decision = _decision(
        preview_sha256=expected_preview_sha256, decided_by=decided_by, reason=reason, fixed_by=fixed_by,
    )
    try:
        native = _post(client, bound, {
            "mode": "execute", "expected_preview_sha256": expected_preview_sha256,
            "decided_by": decided_by, "reason": reason, "fixed_by": fixed_by,
        }, credential=credential)
    except _NativeUnavailable as lost:
        recovered = recover_decision(bound, client)
        if recovered is None or recovered[1] != decision:
            raise StorageHoldRefused(
                f"{lost}; no durable decision equal to this one is recorded, so none was applied "
                "under it: re-run the same command"
            ) from lost
        hold_reason, _, state = recovered
        return (
            _operator_receipt(bound, hold_reason=hold_reason, decision=decision),
            {"state": state, "replayed": True, "recovered": True},
        )
    _require_task(native, bound, NATIVE_RECEIPT_SCHEMA)
    native_hold_reason = native.get("hold_reason")
    if (
        native.get("preview_sha256") != expected_preview_sha256
        or native.get("decision_sha256") != decision.sha256
        or native.get("decision") != decision.payload()
        or type(native_hold_reason) is not str
        or native_hold_reason not in NATIVE_STORAGE_HOLD_REASONS
        or native.get("state") not in {"failed", "cleanup_pending"}
        or type(native.get("replayed")) is not bool
    ):
        raise StorageHoldRefused("native receipt does not record exactly this decision")
    return _operator_receipt(bound, hold_reason=native_hold_reason, decision=decision), native


def publish_receipt(path: Path, value: dict[str, Any]) -> None:
    """Publish once; an existing identical receipt is the same decision replayed."""

    payload = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        publish_new_exact(path, payload)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise StorageHoldRefused(f"{path} already holds a different receipt") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="disclosure-anchor storage-hold", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verifier_parser = commands.add_parser(
        "operator-verifier", help="print the sha256 to enroll in the API container (no DB, no network)",
    )
    verifier_parser.add_argument("--operator-token-file", type=Path, required=True)
    preview_parser = commands.add_parser("preview", help="show the exact reviewable hold")
    preview_parser.add_argument("--attempt-id", required=True)
    preview_parser.add_argument("--operator-token-file", type=Path, required=True)
    preview_parser.add_argument("--out", type=Path)
    execute_parser = commands.add_parser("execute", help="fail the reviewed held task terminally")
    execute_parser.add_argument("--attempt-id", required=True)
    execute_parser.add_argument("--operator-token-file", type=Path, required=True)
    execute_parser.add_argument("--expect-preview-sha256", required=True)
    execute_parser.add_argument("--decided-by", required=True, help="operator identity; never inferred")
    execute_parser.add_argument("--reason", required=True)
    execute_parser.add_argument("--fixed-by", required=True, help="what, if anything, fixed or will fix the cause")
    execute_parser.add_argument("--out", type=Path, required=True)
    recover_parser = commands.add_parser(
        "recover", help="read back a durable decision after a lost answer (ordinary status route)",
    )
    recover_parser.add_argument("--attempt-id", required=True)
    recover_parser.add_argument("--out", type=Path, required=True)
    return parser


def _refused(exc: StorageHoldRefused) -> int:
    print(json.dumps({"refused": str(exc)}, ensure_ascii=False), file=sys.stderr)
    return 1


def print_operator_verifier(path: Path) -> int:
    try:
        verifier = operator_verifier(read_operator_credential(path))
    except StorageHoldRefused as exc:
        return _refused(exc)
    print(json.dumps({"credential_sha256": verifier}, sort_keys=True))
    return 0


def run(
    argv: list[str] | None,
    *,
    uow_factory: Callable[[], UnitOfWork],
    client: httpx.Client,
) -> int:
    args = _parser().parse_args(argv)
    if args.command == "operator-verifier":
        return print_operator_verifier(args.operator_token_file)
    try:
        if args.command == "recover":
            bound = bound_attempt(uow_factory, args.attempt_id)
            recovered = recover_decision(bound, client)
            if recovered is None:
                raise StorageHoldRefused("the task records no hold decision; nothing to recover")
            hold_reason, decision, state = recovered
            receipt = _operator_receipt(bound, hold_reason=hold_reason, decision=decision)
            publish_receipt(args.out, receipt)
            print(json.dumps({**receipt, "native_state": state}, ensure_ascii=False, sort_keys=True))
            return 0
        credential = read_operator_credential(args.operator_token_file)
        bound = bound_attempt(uow_factory, args.attempt_id)
        if args.command == "preview":
            result = preview(bound, client, credential=credential)
            if args.out is not None:
                publish_receipt(args.out, result)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        for name in ("decided_by", "reason", "fixed_by"):
            if not str(getattr(args, name)).strip():
                raise StorageHoldRefused(f"--{name.replace('_', '-')} must not be blank")
        receipt, native = execute(
            bound, client, credential=credential, expected_preview_sha256=args.expect_preview_sha256,
            decided_by=args.decided_by, reason=args.reason, fixed_by=args.fixed_by,
        )
        publish_receipt(args.out, receipt)
        print(json.dumps(
            {**receipt, "native_state": native["state"], "replayed": native["replayed"]},
            ensure_ascii=False, sort_keys=True,
        ))
        return 0
    except StorageHoldRefused as exc:
        return _refused(exc)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "operator-verifier":
        return print_operator_verifier(args.operator_token_file)
    settings = load_settings()
    engine = create_db_engine(app_database_url(settings))
    try:
        require_runtime_app_engine(engine)
        with httpx.Client(trust_env=False) as client:
            return run(argv, uow_factory=unit_of_work_factory(engine), client=client)
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
