"""Private DB-free protocol-v2 commissioning owner, not a publication ACK.

The only executable caller is the opt-in smoke command. Every remote side effect
has a new-only fsynced local intent. A failure preserves this exact attempt and
its resources; there is no automatic new-key retry, fake PG witness, or recovery
by deleting an unknown remote task. The journal is operator recovery evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import time
from typing import Any
from uuid import uuid4

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.artifacts import (
    MinerUMediumArtifactReader,
)
from disclosure_anchor.adapters.parsers.mineru_medium.http_staged import (
    prepare_submission_identity_v2,
    read_diagnostic_retained_archive,
)
from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    MAX_WIRE_JSON_BYTES,
    TaskProtocolV2Observation,
    canonical_client_submit_key_v2,
    canonical_result_owner_v2,
    decode_closed_json_v2,
    parse_result_lease_v2,
    parse_task_payload_v2,
    result_lease_url_v2,
    submission_form_v2,
    task_ack_url_v2,
    task_lookup_url_v2,
    validate_absence_payload_v2,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_store import (
    _canonical, _digest, _fsync_directory, _identity, _record,
    _remove_diagnostic_resources,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic_wire import DiagnosticWireClient
from disclosure_anchor.application.contracts.mineru_api_health import (
    MINERU_API_RESULT_RESERVATION_BYTES,
)
from disclosure_anchor.application.ports.parser import ParserOptions
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity


DIAGNOSTIC_DISPOSAL_SCHEMA = "mineru-diagnostic-disposal.v1"
DIAGNOSTIC_AUTHORITY = "validated-diagnostic-no-publication.v1"
_MAX_INPUT_BYTES = 512 * 1024 * 1024


def validate_diagnostic_disposal(
    value: object, *, source_pdf_sha256: str, runtime_identity: str,
    source_page_count: int, provider_bundle_sha256: str,
) -> None:
    """Validate a closed diagnostic proof; grants no production ACK authority."""
    fields = {
        "schema", "authority", "source_pdf_sha256", "runtime_bundle_identity_sha256",
        "attempt_identity", "fence_identity", "submission_epoch_unix", "idempotency_key",
        "task_id", "terminal_artifact_sha256", "terminal_artifact_bytes", "terminal_artifact_owner",
        "provider_bundle_sha256", "source_page_count", "provider_page_count", "local_resources_removed",
        "ack_response", "task_absence",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("diagnostic disposal proof fields drifted")
    expected = {
        "schema": DIAGNOSTIC_DISPOSAL_SCHEMA, "authority": DIAGNOSTIC_AUTHORITY,
        "source_pdf_sha256": source_pdf_sha256,
        "runtime_bundle_identity_sha256": runtime_identity,
        "source_page_count": source_page_count, "provider_page_count": source_page_count,
        "provider_bundle_sha256": provider_bundle_sha256,
        "local_resources_removed": True, "task_absence": {"detail": "Task not found"},
    }
    if (any(value.get(key) != item for key, item in expected.items())
            or value["local_resources_removed"] is not True
            or type(value["source_page_count"]) is not int
            or type(value["provider_page_count"]) is not int or source_page_count < 1):
        raise ValueError("diagnostic disposal source/runtime/cleanup evidence drifted")
    key = canonical_client_submit_key_v2(
        source_pdf_sha256=source_pdf_sha256, attempt_identity=value["attempt_identity"],
        fence_identity=value["fence_identity"], submission_epoch_unix=value["submission_epoch_unix"],
    )
    owner = canonical_result_owner_v2(
        task_id=value["task_id"], artifact_sha256=value["terminal_artifact_sha256"],
        artifact_byte_count=value["terminal_artifact_bytes"],
    )
    if (value["idempotency_key"] != key or value["terminal_artifact_owner"] != owner
            or not 0 < value["terminal_artifact_bytes"] <= MINERU_API_RESULT_RESERVATION_BYTES
            or value["ack_response"] != {"schema": "mineru-task-protocol.v2",
                                         "task_id": value["task_id"], "status": "consumed"}):
        raise ValueError("diagnostic disposal task/result/ACK identity drifted")


def run_diagnostic_pdf(
    *, input_pdf: Path, source_pdf_sha256: str, source_page_count: int,
    api_url: str, server_url: str, options: ParserOptions, journal_root: Path,
    transport: httpx.BaseTransport | None = None,
    reader: MinerUMediumArtifactReader | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    unix_time: Callable[[], float] = time.time,
    pause: Callable[[float], None] = time.sleep,
    reconcile_submitted: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate one whole PDF, dispose only its diagnostics, retain audit proof.

    Existing journals are never overwritten or resumed by guessing. Explicit
    reconciliation supports only a pre-accepted intent and its intact snapshot,
    validates every supplied identity, and issues GET by the original key only.
    Absence or drift is a failure, never permission to POST. In
    particular a POST response loss is a FAIL with a durable lookup identity,
    not permission to submit again. Network inactivity is bounded to 30 seconds;
    every request/chunk also checks the whole operation deadline.
    """
    if type(source_page_count) is not int or source_page_count < 1:
        raise ValueError("diagnostic source page count is invalid")
    if options.timeout_seconds is None or options.timeout_seconds <= 0:
        raise ValueError("diagnostic deadline is required")
    if not journal_root.is_absolute() or journal_root.is_symlink():
        raise ValueError("diagnostic journal must be a new absolute private directory")
    if not reconcile_submitted:
        journal_root.mkdir(mode=0o700)
    root_identity = _identity(journal_root)
    _fsync_directory(journal_root.parent)
    resources = journal_root / "resources"
    if not reconcile_submitted:
        resources.mkdir(mode=0o700)
    resource_identity = _identity(resources)
    deadline = monotonic() + options.timeout_seconds

    def checkpoint() -> float:
        if _identity(journal_root) != root_identity:
            raise ValueError("diagnostic journal path changed")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("diagnostic deadline expired; preserve exact attempt")
        return min(30.0, remaining)

    prior: dict[str, Any] | None = None
    if reconcile_submitted:
        if (set(item.name for item in journal_root.iterdir()) - {
                "01-intent.json", "02-submit-response.json", "resources"}
                or set(item.name for item in resources.iterdir()) != {"source.pdf"}):
            raise ValueError("diagnostic reconciliation requires an untouched pre-accepted journal")
        fd = os.open(journal_root / "01-intent.json", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError("diagnostic intent ownership drifted")
            prior = decode_closed_json_v2(source.read(MAX_WIRE_JSON_BYTES + 1),
                                          required=frozenset(), allowed=None)
        identity = prior["prepared_identity"]
        attempt, fence, epoch = (identity["attempt_identity"], identity["fence_identity"],
                                 identity["submission_epoch_unix"])
    else:
        attempt = str(uuid4())
        fence = str(uuid4())
        epoch = int(unix_time())
    prepared = prepare_submission_identity_v2(
        api_url=api_url, server_url=server_url, options=options,
        source_pdf_sha256=source_pdf_sha256, attempt_identity=attempt,
        fence_identity=fence, submission_epoch_unix=epoch,
    )
    snapshot = resources / "source.pdf"
    descriptor = os.open(snapshot if reconcile_submitted else input_pdf, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source, (
        nullcontext(None) if reconcile_submitted else snapshot.open("xb")
    ) as target:
        if target is not None:
            os.chmod(snapshot, 0o600)
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.getuid() or not 0 < before.st_size <= _MAX_INPUT_BYTES):
            raise ValueError("diagnostic source is not a bounded owned regular file")
        digest = hashlib.sha256()
        copied = 0
        while chunk := source.read(1024 * 1024):
            checkpoint()
            copied += len(chunk)
            if copied > _MAX_INPUT_BYTES:
                raise ValueError("diagnostic source exceeds byte envelope")
            digest.update(chunk)
            if target is not None:
                target.write(chunk)
        after = os.fstat(source.fileno())
        before_identity = (before.st_dev, before.st_ino, before.st_mode, before.st_uid,
                           before.st_nlink, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_mode, after.st_uid,
                          after.st_nlink, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if (before_identity != after_identity or copied != before.st_size
                or "sha256:" + digest.hexdigest() != source_pdf_sha256):
            raise ValueError("diagnostic source changed or hash drifted")
        if target is not None:
            target.flush()
            os.fsync(target.fileno())
    _fsync_directory(resources)
    intent = {
        "schema": "mineru-diagnostic-intent.v1", "authority": DIAGNOSTIC_AUTHORITY,
        "prepared_identity": json.loads(prepared.exact_bytes),
        "request_sha256": prepared.request_sha256, "api_url": api_url,
        "source_page_count": source_page_count,
        "source_byte_count": copied, "resource_identity": list(resource_identity),
    }
    if reconcile_submitted:
        if prior != intent:
            raise ValueError("diagnostic reconciliation intent/source/runtime/resource identity drifted")
    else:
        _record(journal_root, "01-intent.json", intent)

    with DiagnosticWireClient(checkpoint=checkpoint, transport=transport) as wire:
        client, read_response, request = wire.client, wire.read_response, wire.request

        def observation(exact: bytes, task_id: str | None = None) -> TaskProtocolV2Observation:
            return parse_task_payload_v2(
                exact, api_origin=api_url, idempotency_key=prepared.client_submit_key,
                attempt_identity=attempt, fence_identity=fence, expected_task_id=task_id,
                artifact_byte_limit=MINERU_API_RESULT_RESERVATION_BYTES,
            )

        # No blind automatic retry: the durable intent is sufficient to perform
        # a later read-only same-key reconciliation after any ambiguous outcome.
        data = submission_form_v2(options, server_url=server_url)
        data.update(agent_idempotency_key=prepared.client_submit_key,
                    agent_attempt_identity=attempt, agent_fence_identity=fence)
        class GuardedUpload(io.BufferedReader):
            def read(self, size: int | None = -1) -> bytes:
                checkpoint()
                result = super().read(size)
                checkpoint()
                return result

        if reconcile_submitted:
            status, exact = request("GET", task_lookup_url_v2(
                api_origin=api_url, idempotency_key=prepared.client_submit_key,
            ))
            if status != 200:
                raise ValueError(f"diagnostic reconciliation returned HTTP {status}; never resubmit")
        else:
            with GuardedUpload(io.FileIO(snapshot, "r")) as upload, client.stream(
                "POST", api_url.rstrip("/") + "/tasks", data=data,
                files={"files": ("sha256_" + source_pdf_sha256[7:] + ".pdf", upload, "application/pdf")},
                timeout=checkpoint(), headers={"Accept-Encoding": "identity"},
            ) as response:
                status, exact = response.status_code, read_response(response)
            # Preserve bounded wire evidence even when strict decoding fails.
            _record(journal_root, "02-submit-response.json", {
                "http_status": status, "response_sha256": _digest(exact), "response_hex": exact.hex(),
            })
        if status not in {200, 202}:
            _record(journal_root, "02-submit-rejected.json", {
                "http_status": status, "response_sha256": _digest(exact),
            })
            raise ValueError(f"diagnostic submit returned HTTP {status}; evidence retained")
        accepted = observation(exact)
        _record(journal_root, "02-accepted.json", json.loads(exact))
        current = accepted
        while current.status in {"pending", "processing"}:
            pause(min(1.0, checkpoint()))
            try:
                status, exact = request("GET", accepted.status_url)
            except httpx.TransportError:
                checkpoint()
                continue
            if status != 200:
                raise ValueError(f"diagnostic poll returned HTTP {status}; evidence retained")
            current = observation(exact, accepted.task_id)
        _record(journal_root, "03-terminal.json", json.loads(exact))
        if current.status != "completed":
            raise ValueError("diagnostic provider failed; exact terminal evidence retained")
        status, lease_exact = request("POST", result_lease_url_v2(
            api_origin=api_url, task_id=current.task_id,
        ))
        if status != 200:
            raise ValueError(f"diagnostic result lease returned HTTP {status}")
        lease = parse_result_lease_v2(lease_exact, task_id=current.task_id,
                                      observed_at_unix=unix_time())
        archive = resources / "result.zip"
        digest = hashlib.sha256()
        count = 0
        with archive.open("xb") as sink, client.stream(
            "GET", current.result_url, timeout=checkpoint(),
            headers={"Accept-Encoding": "identity"},
        ) as response:
            os.chmod(archive, 0o600)
            if (response.status_code != 200
                    or response.headers.get("x-mineru-result-sha256") != current.artifact_sha256
                    or response.headers.get("x-mineru-result-owner") != current.artifact_owner_identity
                    or response.headers.get("content-encoding", "identity") != "identity"):
                raise ValueError("diagnostic result response identity drifted")
            for chunk in response.iter_raw():
                checkpoint()
                if unix_time() >= lease.lease_until_unix:
                    raise ValueError("diagnostic result lease expired during download")
                count += len(chunk)
                if count > MINERU_API_RESULT_RESERVATION_BYTES:
                    raise ValueError("diagnostic ZIP exceeds retained byte envelope")
                digest.update(chunk)
                sink.write(chunk)
            sink.flush()
            os.fsync(sink.fileno())
        if count != current.artifact_byte_count or digest.hexdigest() != current.artifact_sha256:
            raise ValueError("diagnostic retained ZIP hash/byte count drifted")
        admitted = read_diagnostic_retained_archive(
            zip_path=archive, output_dir=resources / "output",
            source_pdf_sha256=source_pdf_sha256, reader=reader,
        )
        document = admitted.document
        if (document.parser_version != "3.4.4" or document.backend != "hybrid"
                or document.effort != "medium" or len(document.pages) != source_page_count):
            raise ValueError("diagnostic full-source provider page/profile closure failed")
        parser_target = ParserTargetIdentity(
            name="MinerU", package_version="3.4.4", backend=options.backend,
            method=options.method, language=options.language, formula=options.formula,
            table=options.table, effort=options.effective_effort,
            image_analysis=options.effective_image_analysis, full_pdf=True,
            start_page=None, end_page=None,
            runtime_bundle_identity_sha256=options.runtime_bundle_identity_sha256 or "",
        )
        provider = {
            "target_identity": parser_target.to_payload(),
            "provider_bundle_sha256": document.bundle_sha256,
            "page_count": len(document.pages), "block_count": len(document.blocks),
            "artifact_count": len(document.artifacts),
        }
        _record(journal_root, "04-validated.json", {
            "provider": provider, "terminal": asdict(current), "intent_sha256": _digest(_canonical(intent)),
        })
        checkpoint()
        if _identity(resources) != resource_identity:
            raise ValueError("diagnostic resource root drifted before cleanup")
        # This new private tree belongs exclusively to this non-publishing
        # diagnostic. The original source is outside it and is never deleted.
        _remove_diagnostic_resources(resources, expected_identity=resource_identity,
                                     checkpoint=checkpoint)
        _fsync_directory(journal_root)
        if resources.exists() or resources.is_symlink():
            raise ValueError("diagnostic local resource absence was not proved")
        disposal: dict[str, Any] = {
            "schema": DIAGNOSTIC_DISPOSAL_SCHEMA, "authority": DIAGNOSTIC_AUTHORITY,
            "source_pdf_sha256": source_pdf_sha256,
            "runtime_bundle_identity_sha256": options.runtime_bundle_identity_sha256,
            "attempt_identity": attempt, "fence_identity": fence,
            "submission_epoch_unix": epoch, "idempotency_key": prepared.client_submit_key,
            "task_id": current.task_id, "terminal_artifact_sha256": current.artifact_sha256,
            "terminal_artifact_bytes": current.artifact_byte_count,
            "terminal_artifact_owner": current.artifact_owner_identity,
            "provider_bundle_sha256": document.bundle_sha256,
            "source_page_count": source_page_count, "provider_page_count": len(document.pages),
            "local_resources_removed": True,
        }
        _record(journal_root, "05-disposal-intent.json", disposal)
        # The authority is validated diagnostic disposal, NOT finish_committed.
        # Never invoke a production ACK method or construct its PG witness here.
        status, ack_exact = request("POST", task_ack_url_v2(
            api_origin=api_url, task_id=current.task_id,
        ))
        expected_ack = {"schema": "mineru-task-protocol.v2", "task_id": current.task_id,
                        "status": "consumed"}
        if status != 200 or decode_closed_json_v2(
            ack_exact, required=frozenset(expected_ack), allowed=frozenset(expected_ack),
        ) != expected_ack:
            raise ValueError("diagnostic ACK not proved; preserve disposal intent")
        status, absent_exact = request("GET", task_lookup_url_v2(
            api_origin=api_url, idempotency_key=prepared.client_submit_key,
        ))
        if status != 404:
            raise ValueError("diagnostic consumed task route was not removed")
        validate_absence_payload_v2(absent_exact)
        disposal.update(ack_response=expected_ack, task_absence={"detail": "Task not found"})
        validate_diagnostic_disposal(
            disposal, source_pdf_sha256=source_pdf_sha256,
            runtime_identity=options.runtime_bundle_identity_sha256 or "",
            source_page_count=source_page_count, provider_bundle_sha256=document.bundle_sha256,
        )
        _record(journal_root, "06-disposed.json", disposal)
        return provider, disposal
