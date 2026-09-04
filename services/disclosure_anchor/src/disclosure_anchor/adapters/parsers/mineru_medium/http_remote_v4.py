"""Bounded MinerU protocol-v2 submit/reconcile/poll transport for V4.

Every method is one coordinator episode.  There is no blocking terminal wait,
no durable provider state in this adapter, and no inherited proxy or redirect
behavior.  POST ambiguity is reconciled only by the durable idempotency key.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import hashlib
from math import isfinite
import os
import time
from types import TracebackType
from typing import BinaryIO, Literal, Self, cast

import httpx

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import (
    ProviderAckTransportResponseV4,
)
from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    MAX_WIRE_JSON_BYTES,
    TASK_PROTOCOL_V2,
    MinerUProtocolV2WireError,
    MinerUResultLeaseExpiredV2,
    TaskProtocolV2Observation,
    api_origin_from_task_routes_v2,
    canonical_client_submit_key_v2,
    canonical_result_owner_v2,
    lookup_request_exact_bytes_v2,
    normalize_api_origin_v2,
    parse_result_lease_v2,
    parse_task_payload_v2,
    response_identity_v2,
    result_lease_url_v2,
    submission_form_v2,
    submission_request_exact_bytes_v2,
    task_ack_url_v2,
    task_lookup_url_v2,
    task_result_url_v2,
    validate_absence_payload_v2,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    AcceptedSubmissionReceiptV4,
    SubmissionAbsenceProofV4,
    TerminalReceiptV4,
)
from disclosure_anchor.application.ports.remote_provider_v4 import (
    AcceptedProviderSubmissionV4,
    RemotePollCommandV4,
    RemoteProviderCompletedV4,
    RemoteProviderFailedV4,
    RemoteProviderPollOutcomeV4,
    RemoteProviderProtocolErrorV4,
    RemoteProviderUnavailableV4,
    RemoteProviderWaitingV4,
    RemoteSubmissionAmbiguousV4,
    RemoteSubmissionCommandV4,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    PrivateProviderCapabilityV4,
    ProviderAckCommandV4,
    V4StageGuard,
)

_FILE_CHUNK_BYTES = 1024 * 1024
_RESPONSE_CHUNK_BYTES = 64 * 1024
_TOKEN_BYTES = 32
_TOKEN_KIND = "mineru-task-token.v1"
_TOKEN_VERSION = 1
_ACK_RESPONSE_BYTES = 64 * 1024


class _GuardedUpload:
    def __init__(self, source: BinaryIO, command: RemoteSubmissionCommandV4) -> None:
        self._source = source
        self._command = command
        self._digest = hashlib.sha256()
        self.byte_count = 0

    @property
    def sha256(self) -> str:
        return "sha256:" + self._digest.hexdigest()

    def read(self, size: int = -1) -> bytes:
        self._command.step_guard.checkpoint()
        bounded = _FILE_CHUNK_BYTES if size < 0 else min(size, _FILE_CHUNK_BYTES)
        chunk = self._source.read(bounded)
        if type(chunk) is not bytes:
            raise OSError("snapshot upload returned non-bytes")
        self._digest.update(chunk)
        self.byte_count += len(chunk)
        self._command.step_guard.checkpoint()
        return chunk

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._source.seek(offset, whence)

    def tell(self) -> int:
        return self._source.tell()


class MinerUHttpRemoteV4:
    """Exact one-episode HTTP implementation of the V4 remote provider port."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        token_factory: Callable[[int], bytes] = os.urandom,
        wall_clock: Callable[[], float] = time.time,
        request_timeout_seconds: float,
    ) -> None:
        if (
            not callable(token_factory)
            or not callable(wall_clock)
            or isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, (int, float))
            or not isfinite(float(request_timeout_seconds))
            or not 0 < float(request_timeout_seconds) <= 3600
        ):
            raise ValueError("MinerU V4 remote transport configuration is invalid")
        self._token_factory = token_factory
        self._wall_clock = wall_clock
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._client = httpx.Client(
            timeout=httpx.Timeout(self._request_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def reconcile_or_submit(
        self, command: RemoteSubmissionCommandV4
    ) -> AcceptedProviderSubmissionV4:
        self._validate_submission_command(command)
        parser_timeout_seconds = cast(int, command.parser_options.timeout_seconds)
        token = self._new_capability_token()
        intent = command.submission_intent
        api_origin = normalize_api_origin_v2(command.parser_options.api_url or "")
        lookup_url = task_lookup_url_v2(
            api_origin=api_origin,
            idempotency_key=intent.client_submit_key,
        )
        try:
            status_code, exact = self._request_bytes(
                method="GET",
                url=lookup_url,
                guard=command.step_guard,
                configured_timeout=min(
                    float(parser_timeout_seconds),
                    self._request_timeout_seconds,
                ),
            )
        except httpx.TransportError as exc:
            raise RemoteProviderUnavailableV4(
                "MinerU V4 lookup was unavailable before submission"
            ) from exc
        if status_code == 200:
            observation = self._parse_submission_observation(
                exact,
                command=command,
                api_origin=api_origin,
            )
            return self._accepted(
                command,
                observation,
                token=token,
                absence_proof=None,
            )
        if status_code != 404:
            if status_code in {408, 425, 429} or 500 <= status_code <= 599:
                raise RemoteProviderUnavailableV4(
                    f"MinerU V4 lookup returned HTTP {status_code}"
                )
            raise RemoteProviderProtocolErrorV4(
                f"MinerU V4 lookup returned HTTP {status_code}"
            )
        try:
            validate_absence_payload_v2(exact)
            lookup_request = lookup_request_exact_bytes_v2(
                api_origin=api_origin,
                idempotency_key=intent.client_submit_key,
            )
            response_sha256, response_bytes = response_identity_v2(exact)
            absence = SubmissionAbsenceProofV4(
                client_submit_key=intent.client_submit_key,
                lookup_request_sha256=(
                    "sha256:" + hashlib.sha256(lookup_request).hexdigest()
                ),
                provider_protocol_version=intent.provider_protocol_version,
                http_status=404,
                response_sha256=response_sha256,
                response_byte_count=response_bytes,
            )
        except (MinerUProtocolV2WireError, ValueError) as exc:
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 lookup 404 did not prove closed absence"
            ) from exc

        post_started = False
        try:
            with command.snapshot_source.open(step_guard=command.step_guard) as source:
                upload = _GuardedUpload(source, command)
                form = submission_form_v2(
                    command.parser_options,
                    server_url=command.parser_options.server_url or "",
                )
                data = {
                    **form,
                    "agent_idempotency_key": intent.client_submit_key,
                    "agent_attempt_identity": intent.attempt_id,
                    "agent_fence_identity": intent.fence_identity,
                }
                def mark_post_started() -> None:
                    nonlocal post_started
                    post_started = True

                try:
                    status_code, exact = self._request_bytes(
                        method="POST",
                        url=f"{api_origin}/tasks",
                        guard=command.step_guard,
                        configured_timeout=min(
                            float(parser_timeout_seconds),
                            self._request_timeout_seconds,
                        ),
                        before_send=mark_post_started,
                        data=data,
                        files={
                            "files": (
                                command.upload_filename,
                                cast(BinaryIO, upload),
                                "application/pdf",
                            )
                        },
                    )
                except httpx.TransportError:
                    accepted = self._reconcile_once_after_post(
                        command,
                        api_origin=api_origin,
                        token=token,
                        absence_proof=absence,
                    )
                else:
                    if (
                        upload.byte_count != command.source_byte_count
                        or upload.sha256 != intent.source_pdf_sha256
                    ):
                        raise RemoteSubmissionAmbiguousV4(
                            "MinerU V4 upload bytes drifted after submission began"
                        )
                    if status_code in {200, 202}:
                        try:
                            observation = self._parse_submission_observation(
                                exact,
                                command=command,
                                api_origin=api_origin,
                            )
                        except RemoteProviderProtocolErrorV4:
                            accepted = self._reconcile_once_after_post(
                                command,
                                api_origin=api_origin,
                                token=token,
                                absence_proof=absence,
                            )
                        else:
                            accepted = self._accepted(
                                command,
                                observation,
                                token=token,
                                absence_proof=absence,
                            )
                    else:
                        accepted = self._reconcile_once_after_post(
                            command,
                            api_origin=api_origin,
                            token=token,
                            absence_proof=absence,
                        )
            return accepted
        except RemoteSubmissionAmbiguousV4:
            raise
        except Exception as exc:
            if post_started:
                raise RemoteSubmissionAmbiguousV4(
                    "MinerU V4 upload began but its acceptance is ambiguous"
                ) from exc
            # No provider side effect has begun.  Preserve local claim, deadline,
            # filesystem, and configuration failures so the coordinator can
            # apply their real retry/fencing semantics instead of recording a
            # false permanent provider-contract violation.
            raise

    def poll_once(
        self, command: RemotePollCommandV4
    ) -> RemoteProviderPollOutcomeV4:
        if type(command) is not RemotePollCommandV4:
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 poll command is not exact"
            )
        accepted = command.accepted_submission
        intent = command.submission_intent
        try:
            api_origin = api_origin_from_task_routes_v2(
                status_url=accepted.status_url,
                result_url=accepted.result_url,
                task_id=accepted.remote_task_identity,
            )
        except MinerUProtocolV2WireError as exc:
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 persisted task routes violated protocol v2"
            ) from exc
        try:
            status_code, exact = self._request_bytes(
                method="GET",
                url=accepted.status_url,
                guard=command.step_guard,
                configured_timeout=self._request_timeout_seconds,
            )
        except httpx.TransportError as exc:
            raise RemoteProviderUnavailableV4(
                "MinerU V4 status poll was unavailable"
            ) from exc
        if status_code != 200:
            if status_code in {408, 425, 429} or 500 <= status_code <= 599:
                raise RemoteProviderUnavailableV4(
                    f"MinerU V4 status returned HTTP {status_code}"
                )
            raise RemoteProviderProtocolErrorV4(
                f"MinerU V4 status returned HTTP {status_code}"
            )
        try:
            observation = parse_task_payload_v2(
                exact,
                api_origin=api_origin,
                idempotency_key=intent.client_submit_key,
                attempt_identity=intent.attempt_id,
                fence_identity=intent.fence_identity,
                expected_task_id=accepted.remote_task_identity,
                expected_status_url=accepted.status_url,
                expected_result_url=accepted.result_url,
                artifact_byte_limit=command.artifact_byte_limit,
            )
            response_sha256, response_bytes = response_identity_v2(exact)
        except MinerUProtocolV2WireError as exc:
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 status payload violated protocol v2"
            ) from exc
        if observation.status in {"pending", "processing"}:
            return RemoteProviderWaitingV4(
                remote_task_identity=observation.task_id,
                status=cast(Literal["pending", "processing"], observation.status),
                response_sha256=response_sha256,
                response_byte_count=response_bytes,
            )
        if observation.status == "failed":
            return RemoteProviderFailedV4(
                remote_task_identity=observation.task_id,
                provider_error=(
                    observation.provider_error
                    or "MinerU remote task failed without provider detail"
                ),
                response_sha256=response_sha256,
                response_byte_count=response_bytes,
            )
        if observation.status != "completed":
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 status escaped the closed state set"
            )
        lease_url = result_lease_url_v2(
            api_origin=api_origin,
            task_id=accepted.remote_task_identity,
        )
        try:
            lease_status, lease_exact = self._request_bytes(
                method="POST",
                url=lease_url,
                guard=command.step_guard,
                configured_timeout=self._request_timeout_seconds,
                params={"seconds": str(command.result_lease_seconds)},
            )
        except httpx.TransportError as exc:
            raise RemoteProviderUnavailableV4(
                "MinerU V4 result lease was unavailable"
            ) from exc
        if lease_status != 200:
            if lease_status in {408, 425, 429} or 500 <= lease_status <= 599:
                raise RemoteProviderUnavailableV4(
                    f"MinerU V4 result lease returned HTTP {lease_status}"
                )
            raise RemoteProviderProtocolErrorV4(
                f"MinerU V4 result lease returned HTTP {lease_status}"
            )
        try:
            try:
                observed_at_unix = float(self._wall_clock())
            except Exception as exc:
                raise RemoteProviderUnavailableV4(
                    "MinerU V4 lease clock was unavailable"
                ) from exc
            if not isfinite(observed_at_unix):
                raise RemoteProviderProtocolErrorV4(
                    "MinerU V4 lease clock was not finite"
                )
            lease = parse_result_lease_v2(
                lease_exact,
                task_id=accepted.remote_task_identity,
                observed_at_unix=observed_at_unix,
            )
            lease_sha256, lease_bytes = response_identity_v2(lease_exact)
        except MinerUResultLeaseExpiredV2 as exc:
            raise RemoteProviderUnavailableV4(
                "MinerU V4 result lease expired before observation completed"
            ) from exc
        except MinerUProtocolV2WireError as exc:
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 result lease payload violated protocol v2"
            ) from exc
        if (
            observation.artifact_sha256 is None
            or observation.artifact_byte_count is None
            or observation.artifact_owner_identity is None
        ):
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 completed status lost its result identity"
            )
        terminal = TerminalReceiptV4(
            attempt_id=intent.attempt_id,
            fence_identity=intent.fence_identity,
            accepted_submission_receipt_sha256=accepted.sha256,
            remote_task_identity=accepted.remote_task_identity,
            result_owner_identity=observation.artifact_owner_identity,
            artifact_sha256="sha256:" + observation.artifact_sha256,
            artifact_byte_count=observation.artifact_byte_count,
            provider_protocol_version=accepted.provider_protocol_version,
        )
        return RemoteProviderCompletedV4(
            receipt=terminal,
            result_lease_until_unix=lease.lease_until_unix,
            lease_observed_at_unix=observed_at_unix,
            lease_response_sha256=lease_sha256,
            lease_response_byte_count=lease_bytes,
        )

    def stream_result(
        self,
        *,
        accepted_submission: AcceptedSubmissionReceiptV4,
        terminal_receipt: TerminalReceiptV4,
        provider_capability: PrivateProviderCapabilityV4,
        result_lease_seconds: int,
        step_guard: V4StageGuard,
        before_result_get: Callable[[], None],
    ) -> Iterable[bytes]:
        api_origin = self._validate_result_download(
            accepted=accepted_submission,
            terminal=terminal_receipt,
            capability=provider_capability,
            result_lease_seconds=result_lease_seconds,
            step_guard=step_guard,
            before_result_get=before_result_get,
        )
        lease_url = result_lease_url_v2(
            api_origin=api_origin,
            task_id=accepted_submission.remote_task_identity,
        )
        try:
            lease_status, lease_exact = self._request_bytes(
                method="POST",
                url=lease_url,
                guard=step_guard,
                configured_timeout=self._request_timeout_seconds,
                params={"seconds": str(result_lease_seconds)},
            )
        except httpx.TransportError as exc:
            raise RemoteProviderUnavailableV4(
                "MinerU V4 fresh result lease was unavailable"
            ) from exc
        if lease_status != 200:
            if lease_status in {408, 409, 425, 429} or 500 <= lease_status <= 599:
                raise RemoteProviderUnavailableV4(
                    f"MinerU V4 fresh result lease returned HTTP {lease_status}"
                )
            raise RemoteProviderProtocolErrorV4(
                f"MinerU V4 fresh result lease returned HTTP {lease_status}"
            )
        observed_at_unix = self._lease_clock()
        try:
            lease = parse_result_lease_v2(
                lease_exact,
                task_id=accepted_submission.remote_task_identity,
                observed_at_unix=observed_at_unix,
            )
        except MinerUResultLeaseExpiredV2 as exc:
            raise RemoteProviderUnavailableV4(
                "MinerU V4 fresh result lease expired before observation completed"
            ) from exc
        except MinerUProtocolV2WireError as exc:
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 fresh result lease payload violated protocol v2"
            ) from exc

        checkpoint = step_guard.checkpoint
        checkpoint()
        before_result_get()
        checkpoint()
        timeout = self._bounded_timeout(
            guard=step_guard,
            configured_timeout=self._request_timeout_seconds,
        )
        lease_remaining = lease.lease_until_unix - self._lease_clock()
        if lease_remaining <= 0.001:
            raise RemoteProviderUnavailableV4(
                "MinerU V4 fresh result lease expired before GET"
            )
        timeout = min(timeout, lease_remaining * 0.9)
        if timeout <= 0.001:
            raise RemoteProviderUnavailableV4(
                "MinerU V4 fresh result lease lacks time for GET"
            )
        request = self._client.build_request(
            "GET",
            accepted_submission.result_url,
            timeout=httpx.Timeout(timeout),
        )
        response: httpx.Response | None = None
        try:
            response = self._client.send(request, stream=True)
            checkpoint()
            if response.status_code != 200:
                if (
                    response.status_code in {408, 409, 425, 429}
                    or 500 <= response.status_code <= 599
                ):
                    raise RemoteProviderUnavailableV4(
                        f"MinerU V4 result GET returned HTTP {response.status_code}"
                    )
                raise RemoteProviderProtocolErrorV4(
                    f"MinerU V4 result GET returned HTTP {response.status_code}"
                )
            expected_sha = terminal_receipt.artifact_sha256.removeprefix("sha256:")
            expected_owner = terminal_receipt.result_owner_identity
            if (
                response.headers.get("X-MinerU-Result-SHA256") != expected_sha
                or response.headers.get("X-MinerU-Result-Owner") != expected_owner
                or response.headers.get("Content-Type", "").split(";", 1)[0].strip()
                != "application/zip"
            ):
                raise RemoteProviderProtocolErrorV4(
                    "MinerU V4 result headers drifted from terminal evidence"
                )
            content_length = response.headers.get("Content-Length")
            if content_length != str(terminal_receipt.artifact_byte_count):
                raise RemoteProviderProtocolErrorV4(
                    "MinerU V4 result length header drifted from terminal evidence"
                )
            byte_count = 0
            for chunk in response.iter_bytes(chunk_size=_FILE_CHUNK_BYTES):
                checkpoint()
                if type(chunk) is not bytes or not chunk:
                    raise RemoteProviderProtocolErrorV4(
                        "MinerU V4 result stream yielded an invalid chunk"
                    )
                byte_count += len(chunk)
                if byte_count > terminal_receipt.artifact_byte_count:
                    raise RemoteProviderProtocolErrorV4(
                        "MinerU V4 result exceeded its terminal byte count"
                    )
                yield chunk
                checkpoint()
            if byte_count != terminal_receipt.artifact_byte_count:
                raise RemoteProviderProtocolErrorV4(
                    "MinerU V4 result ended before its terminal byte count"
                )
        except httpx.TransportError as exc:
            raise RemoteProviderUnavailableV4(
                "MinerU V4 result stream was unavailable"
            ) from exc
        finally:
            if response is not None:
                response.close()

    def _validate_result_download(
        self,
        *,
        accepted: AcceptedSubmissionReceiptV4,
        terminal: TerminalReceiptV4,
        capability: PrivateProviderCapabilityV4,
        result_lease_seconds: int,
        step_guard: V4StageGuard,
        before_result_get: Callable[[], None],
    ) -> str:
        if (
            type(accepted) is not AcceptedSubmissionReceiptV4
            or type(terminal) is not TerminalReceiptV4
            or type(capability) is not PrivateProviderCapabilityV4
            or terminal.accepted_submission_receipt_sha256 != accepted.sha256
            or terminal.attempt_id != accepted.attempt_id
            or terminal.fence_identity != accepted.fence_identity
            or terminal.remote_task_identity != accepted.remote_task_identity
            or terminal.provider_protocol_version
            != accepted.provider_protocol_version
            or not capability.validates_accepted_submission(accepted)
            or capability.capability_purpose != "result_download"
            or isinstance(result_lease_seconds, bool)
            or not isinstance(result_lease_seconds, int)
            or not 1 <= result_lease_seconds <= 3600
            or not callable(getattr(step_guard, "checkpoint", None))
            or not callable(getattr(step_guard, "remaining_seconds", None))
            or not callable(before_result_get)
        ):
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 result download evidence is not exactly bound"
            )
        try:
            api_origin = api_origin_from_task_routes_v2(
                status_url=accepted.status_url,
                result_url=accepted.result_url,
                task_id=accepted.remote_task_identity,
            )
            expected_owner = canonical_result_owner_v2(
                task_id=accepted.remote_task_identity,
                artifact_sha256=terminal.artifact_sha256.removeprefix("sha256:"),
                artifact_byte_count=terminal.artifact_byte_count,
            )
        except MinerUProtocolV2WireError as exc:
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 result routes or owner violated protocol v2"
            ) from exc
        if (
            accepted.result_url
            != task_result_url_v2(
                api_origin=api_origin,
                task_id=accepted.remote_task_identity,
            )
            or terminal.result_owner_identity != expected_owner
        ):
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 result route or owner is not canonical"
            )
        return api_origin

    def acknowledge(
        self,
        *,
        command: ProviderAckCommandV4,
        provider_capability: PrivateProviderCapabilityV4,
        step_guard: V4StageGuard,
        before_ack_post: Callable[[], None],
    ) -> ProviderAckTransportResponseV4:
        if (
            type(command) is not ProviderAckCommandV4
            or type(provider_capability) is not PrivateProviderCapabilityV4
            or provider_capability.capability_purpose
            != "result_acknowledgement"
            or not provider_capability.validates_accepted_submission(
                command.accepted_submission
            )
            or command.provider_protocol_version != TASK_PROTOCOL_V2
            or not callable(getattr(step_guard, "checkpoint", None))
            or not callable(getattr(step_guard, "remaining_seconds", None))
            or not callable(before_ack_post)
        ):
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 ACK evidence is not exactly bound"
            )
        accepted = command.accepted_submission
        try:
            api_origin = api_origin_from_task_routes_v2(
                status_url=accepted.status_url,
                result_url=accepted.result_url,
                task_id=accepted.remote_task_identity,
            )
            ack_url = task_ack_url_v2(
                api_origin=api_origin,
                task_id=accepted.remote_task_identity,
            )
        except MinerUProtocolV2WireError as exc:
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 ACK route violated protocol v2"
            ) from exc
        step_guard.checkpoint()
        before_ack_post()
        step_guard.checkpoint()
        try:
            status_code, exact = self._request_bytes(
                method="POST",
                url=ack_url,
                guard=step_guard,
                configured_timeout=self._request_timeout_seconds,
                max_response_bytes=_ACK_RESPONSE_BYTES,
            )
        except httpx.TransportError as exc:
            raise RemoteProviderUnavailableV4(
                "MinerU V4 ACK response was unavailable"
            ) from exc
        if status_code in {200, 204, 404}:
            return ProviderAckTransportResponseV4(
                status_code=status_code,
                exact_bytes=exact,
            )
        if status_code in {408, 409, 425, 429} or 500 <= status_code <= 599:
            raise RemoteProviderUnavailableV4(
                f"MinerU V4 ACK returned HTTP {status_code}"
            )
        raise RemoteProviderProtocolErrorV4(
            f"MinerU V4 ACK returned HTTP {status_code}"
        )

    def _lease_clock(self) -> float:
        try:
            value = float(self._wall_clock())
        except Exception as exc:
            raise RemoteProviderUnavailableV4(
                "MinerU V4 lease clock was unavailable"
            ) from exc
        if not isfinite(value):
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 lease clock was not finite"
            )
        return value

    def _validate_submission_command(self, command: RemoteSubmissionCommandV4) -> None:
        if type(command) is not RemoteSubmissionCommandV4:
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 submission command is not exact"
            )
        options = command.parser_options
        if (
            command.submission_intent.provider_protocol_version != TASK_PROTOCOL_V2
            or command.parser_identity.name != "MinerU"
            or command.parser_identity.version != "3.4.4"
            or options.backend != "hybrid-http-client"
            or options.method != "auto"
            or options.language != "ch"
            or not options.formula
            or not options.table
            or options.effective_effort != "medium"
            or options.effective_image_analysis
            or options.start_page is not None
            or options.end_page is not None
            or not options.api_url
            or not options.server_url
            or not options.runtime_bundle_identity_sha256
            or isinstance(options.timeout_seconds, bool)
            or not isinstance(options.timeout_seconds, int)
            or options.timeout_seconds < 1
        ):
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 request is outside the pinned full-PDF Medium profile"
            )
        try:
            expected_key = canonical_client_submit_key_v2(
                source_pdf_sha256=command.submission_intent.source_pdf_sha256,
                attempt_identity=command.submission_intent.attempt_id,
                fence_identity=command.submission_intent.fence_identity,
                submission_epoch_unix=(
                    command.submission_intent.submission_epoch_unix
                ),
            )
            form = submission_form_v2(options, server_url=options.server_url)
            expected = submission_request_exact_bytes_v2(
                api_origin=options.api_url,
                form=form,
                upload_filename=command.upload_filename,
            )
        except MinerUProtocolV2WireError as exc:
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 exact request spec is invalid"
            ) from exc
        if (
            command.submission_intent.client_submit_key != expected_key
            or command.request_exact_bytes != expected
        ):
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 derived key or exact request drifted"
            )

    def _parse_submission_observation(
        self,
        exact: bytes,
        *,
        command: RemoteSubmissionCommandV4,
        api_origin: str,
    ) -> TaskProtocolV2Observation:
        intent = command.submission_intent
        try:
            return parse_task_payload_v2(
                exact,
                api_origin=api_origin,
                idempotency_key=intent.client_submit_key,
                attempt_identity=intent.attempt_id,
                fence_identity=intent.fence_identity,
            )
        except MinerUProtocolV2WireError as exc:
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 submit/reconcile payload violated protocol v2"
            ) from exc

    def _accepted(
        self,
        command: RemoteSubmissionCommandV4,
        observation: TaskProtocolV2Observation,
        *,
        token: bytes,
        absence_proof: SubmissionAbsenceProofV4 | None,
    ) -> AcceptedProviderSubmissionV4:
        token_sha256 = "sha256:" + hashlib.sha256(token).hexdigest()
        intent = command.submission_intent
        receipt = AcceptedSubmissionReceiptV4(
            attempt_id=intent.attempt_id,
            fence_identity=intent.fence_identity,
            submission_intent_sha256=intent.sha256,
            remote_task_identity=observation.task_id,
            status_url=observation.status_url,
            result_url=observation.result_url,
            secret_kind=_TOKEN_KIND,
            secret_version=_TOKEN_VERSION,
            token_sha256=token_sha256,
            token_byte_count=len(token),
            provider_protocol_version=intent.provider_protocol_version,
        )
        capability = PrivateProviderCapabilityV4(
            attempt_id=receipt.attempt_id,
            remote_task_identity=receipt.remote_task_identity,
            provider_protocol_version=receipt.provider_protocol_version,
            secret_kind=receipt.secret_kind,
            secret_version=receipt.secret_version,
            capability_purpose="submitted_task_resume",
            token_bytes=token,
            token_sha256=receipt.token_sha256,
            token_byte_count=receipt.token_byte_count,
        )
        return AcceptedProviderSubmissionV4(
            submission_intent=intent,
            receipt=receipt,
            provider_capability=capability,
            absence_proof=absence_proof,
        )

    def _new_capability_token(self) -> bytes:
        try:
            token = self._token_factory(_TOKEN_BYTES)
        except Exception as exc:
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 local capability entropy failed closed"
            ) from exc
        if type(token) is not bytes or len(token) != _TOKEN_BYTES:
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 local capability entropy failed closed"
            )
        return token

    def _reconcile_once_after_post(
        self,
        command: RemoteSubmissionCommandV4,
        *,
        api_origin: str,
        token: bytes,
        absence_proof: SubmissionAbsenceProofV4,
    ) -> AcceptedProviderSubmissionV4:
        intent = command.submission_intent
        status_code, exact = self._request_bytes(
            method="GET",
            url=task_lookup_url_v2(
                api_origin=api_origin,
                idempotency_key=intent.client_submit_key,
            ),
            guard=command.step_guard,
            configured_timeout=self._request_timeout_seconds,
        )
        if status_code == 200:
            observation = self._parse_submission_observation(
                exact,
                command=command,
                api_origin=api_origin,
            )
            return self._accepted(
                command,
                observation,
                token=token,
                absence_proof=absence_proof,
            )
        if status_code == 404:
            validate_absence_payload_v2(exact)
        raise RemoteSubmissionAmbiguousV4(
            "MinerU V4 submission acceptance remains ambiguous after one reconcile"
        )

    def _request_bytes(
        self,
        *,
        method: str,
        url: str,
        guard: V4StageGuard,
        configured_timeout: float,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, BinaryIO, str]] | None = None,
        params: dict[str, str] | None = None,
        max_response_bytes: int = MAX_WIRE_JSON_BYTES,
        before_send: Callable[[], None] | None = None,
    ) -> tuple[int, bytes]:
        checkpoint = cast(Callable[[], None], getattr(guard, "checkpoint"))
        timeout = self._bounded_timeout(
            guard=guard,
            configured_timeout=configured_timeout,
        )
        chunks: list[bytes] = []
        total = 0
        request = self._client.build_request(
            method,
            url,
            data=data,
            files=files,
            params=params,
            timeout=httpx.Timeout(timeout),
        )
        checkpoint()
        if before_send is not None:
            before_send()
        response = self._client.send(request, stream=True)
        try:
            checkpoint()
            for chunk in response.iter_bytes(chunk_size=_RESPONSE_CHUNK_BYTES):
                checkpoint()
                total += len(chunk)
                if total > max_response_bytes:
                    raise RemoteProviderProtocolErrorV4(
                        "MinerU V4 response exceeded the wire envelope"
                    )
                chunks.append(chunk)
                checkpoint()
            return response.status_code, b"".join(chunks)
        finally:
            response.close()

    @staticmethod
    def _bounded_timeout(*, guard: object, configured_timeout: float) -> float:
        checkpoint = getattr(guard, "checkpoint", None)
        remaining_seconds = getattr(guard, "remaining_seconds", None)
        if not callable(checkpoint) or not callable(remaining_seconds):
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 HTTP stage guard is invalid"
            )
        checkpoint()
        try:
            remaining = float(remaining_seconds())
            configured = float(configured_timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 HTTP deadline inputs are invalid"
            ) from exc
        if (
            not isfinite(remaining)
            or not isfinite(configured)
            or configured <= 0
        ):
            raise RemoteProviderProtocolErrorV4(
                "MinerU V4 HTTP deadline inputs are invalid"
            )
        timeout = min(configured, remaining * 0.9)
        if timeout <= 0.001:
            raise RemoteProviderUnavailableV4(
                "MinerU V4 stage lacks time for a bounded HTTP request"
            )
        return timeout


__all__ = ["MinerUHttpRemoteV4"]
