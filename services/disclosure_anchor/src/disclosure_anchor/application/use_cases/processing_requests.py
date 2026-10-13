"""Explicit processing requests: accept/join on the API, resolve in the worker.

:class:`ProcessingRequestService` is the API side. ``submit`` is the only write
there: it persists the intent (or joins the existing one for the same key) and
binds a target immediately only when the filing is already known locally. It
never calls the provider. :class:`ProcessingRequestResolver` runs inside the
resident worker's maintenance cycle and resolves due requests from the same
tables with leases, bounded attempts and backoff. Resolution binds exactly one
filing or records not-found/ambiguous/failed facts; it never tracks a company,
changes tracked rows, or advances a sync checkpoint. Download and parse happen
in the existing worker stages through the target's admit flags.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
import json
import time
from typing import Any

from disclosure_anchor.application.contracts.historical_security_registration import (
    INDEX_IDENTITY_CONTEXT_VERSION,
)
from disclosure_anchor.application.contracts.processing_request import (
    ATTEMPT_LEASE_SECONDS,
    CONTRACT_VERSION,
    DISCOVERY_CHUNK_DAYS,
    DISCOVERY_DEADLINE_SECONDS,
    MAX_ATTEMPTS,
    MAX_CHOICES,
    AttemptRecord,
    DocumentTarget,
    ProcessingIntent,
    ProviderDocumentTarget,
    RequestRecord,
    TargetRecord,
    TitleMatchTarget,
    choice_from_candidate,
    derive_status,
    discovery_due,
    next_attempt_after,
    target_from_json,
    title_matches,
    window_chunks,
)
from disclosure_anchor.application.ports.disclosure_source import (
    AnnouncementRef,
    DisclosureSourcePort,
    DisclosureWindow,
    SourceCompanyProfile,
    SourceSecurity,
)
from disclosure_anchor.application.ports.processing_requests import (
    LocalDocument,
    ProcessingAttemptConflict,
    ProcessingRequestRepository,
    ProcessingRequestUnitOfWork,
    ProcessingStatusReader,
)
from disclosure_anchor.application.services.cninfo_profile_access import (
    add_cninfo_profile_access,
    add_failed_cninfo_profile_access,
)
from disclosure_anchor.application.services.subject_resolver import SubjectResolver
from disclosure_anchor.application.use_cases.sync_disclosure_index import (
    CNINFO_PROVIDER,
    candidate_snapshot,
    canonical_snapshot_sha256,
    index_failure_error,
    record_cninfo_org_identifier,
    resolve_index_subject,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain import ids
from disclosure_anchor.domain.errors import (
    DisclosureAnchorError,
    SourceRequestError,
    SubjectIdentityRaceError,
)


REQUEST_LIMIT_WINDOW = timedelta(hours=24)
WAIT_POLL_SECONDS = 1.0
MAX_WAIT_SECONDS = 20
# Provider refusal kinds a resolver classifier may return; anything else is an
# item failure that spends the request's bounded budget.
PROVIDER_REFUSALS = ("rate_limited", "quota_exhausted", "outage")


class ProcessingRequestNotFound(DisclosureAnchorError):
    pass


class ProcessingTargetNotFound(DisclosureAnchorError):
    pass


class ProcessingTargetMismatch(DisclosureAnchorError):
    def __init__(self, message: str, *, found: Mapping[str, object]) -> None:
        self.found = dict(found)
        super().__init__(message)


class IdempotencyConflict(DisclosureAnchorError):
    def __init__(self, *, request_id: str, intent_sha256: str) -> None:
        self.request_id = request_id
        self.intent_sha256 = intent_sha256
        super().__init__("idempotency key is bound to a different intent")


class ProcessingRequestLimitExceeded(DisclosureAnchorError):
    def __init__(self, *, limit: int, window_hours: int) -> None:
        self.limit = limit
        self.window_hours = window_hours
        super().__init__("processing request limit reached")


@dataclass(frozen=True)
class ProcessingRequestPolicy:
    daily_request_limit: int
    requested_by: str = "agent_api"

    def __post_init__(self) -> None:
        if self.daily_request_limit < 1:
            raise ValueError("daily_request_limit must be positive")


@dataclass(frozen=True)
class ProcessingRequestView:
    request: RequestRecord
    status: Mapping[str, Any]
    replayed: bool | None = None

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "request_id": self.request.request_id,
            "idempotency_key": self.request.idempotency_key,
            "intent_sha256": self.request.intent_sha256,
            "created_at": self.request.created_at.isoformat(),
            "enqueue_parse": self.request.enqueue_parse,
            "target": dict(self.request.target),
        }
        if self.replayed is not None:
            body["replayed"] = self.replayed
        body["status"] = dict(self.status)
        return body


@dataclass(frozen=True)
class WaitOutcome:
    view: ProcessingRequestView
    outcome: str
    waited_ms: int


class ProcessingRequestService:
    """API side: accept or join an intent, read status, bounded wait."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], ProcessingRequestUnitOfWork],
        status_reader: ProcessingStatusReader,
        policy: ProcessingRequestPolicy,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._uow_factory = uow_factory
        self._status_reader = status_reader
        self._policy = policy
        self._clock = clock
        self._sleep = sleep
        self._monotonic = monotonic

    def submit(self, intent: ProcessingIntent) -> ProcessingRequestView:
        request, replayed = self._create_or_join(intent)
        return self._view(request.request_id, replayed=replayed)

    def _create_or_join(self, intent: ProcessingIntent) -> tuple[RequestRecord, bool]:
        with self._uow_factory() as uow:
            repo = uow.processing_requests
            repo.lock_idempotency_key(intent.idempotency_key)
            existing = repo.get_by_key(intent.idempotency_key)
            if existing is not None:
                if existing.intent_sha256 != intent.intent_sha256:
                    raise IdempotencyConflict(
                        request_id=existing.request_id,
                        intent_sha256=existing.intent_sha256,
                    )
                return existing, True
            repo.lock_request_admission()
            since = self._clock() - REQUEST_LIMIT_WINDOW
            if repo.count_created_since(since) >= self._policy.daily_request_limit:
                raise ProcessingRequestLimitExceeded(
                    limit=self._policy.daily_request_limit,
                    window_hours=int(REQUEST_LIMIT_WINDOW.total_seconds() // 3600),
                )
            local_target = _local_target(uow, intent)
            request = repo.add_request(
                request_id=ids.new_id("dreq"),
                intent=intent,
                requested_by=self._policy.requested_by,
            )
            if local_target is not None:
                repo.add_target(request_id=request.request_id, target=local_target)
            uow.commit()
            return request, False

    def get(self, request_id: str) -> ProcessingRequestView:
        return self._view(request_id, replayed=None)

    def wait(
        self, request_id: str, *, timeout_s: int, since: str | None
    ) -> WaitOutcome:
        if not 1 <= timeout_s <= MAX_WAIT_SECONDS:
            raise ValueError(f"timeout_s must be 1-{MAX_WAIT_SECONDS}")
        started = self._monotonic()
        deadline = started + timeout_s
        view = self.get(request_id)
        baseline = since if since is not None else view.status["status_sha256"]
        while True:
            if view.status["terminal"]:
                outcome = "terminal"
                break
            if view.status["status_sha256"] != baseline:
                outcome = "changed"
                break
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                outcome = "timeout"
                break
            self._sleep(min(WAIT_POLL_SECONDS, remaining))
            view = self.get(request_id)
        waited_ms = max(0, int((self._monotonic() - started) * 1000))
        return WaitOutcome(view=view, outcome=outcome, waited_ms=waited_ms)

    def _view(self, request_id: str, *, replayed: bool | None) -> ProcessingRequestView:
        facts = self._status_reader.read(request_id, observed_at=self._clock())
        if facts is None:
            raise ProcessingRequestNotFound(f"processing request {request_id} not found")
        return ProcessingRequestView(
            request=facts.request, status=derive_status(facts), replayed=replayed
        )


def _local_target(
    uow: ProcessingRequestUnitOfWork, intent: ProcessingIntent
) -> TargetRecord | None:
    """Bind a filing already known locally; None leaves discovery to the worker."""

    repo = uow.processing_requests
    target = intent.target
    if isinstance(target, DocumentTarget):
        document = repo.document(target.document_id)
        if document is None:
            raise ProcessingTargetNotFound(f"document {target.document_id} not found")
        return _document_target(
            document, method="document_id", enqueue_parse=intent.enqueue_parse
        )
    if isinstance(target, ProviderDocumentTarget):
        document = repo.latest_provider_document(
            provider=CNINFO_PROVIDER, provider_document_id=target.provider_document_id
        )
        if document is not None:
            _require_same_filing(
                target,
                security_code=document.security_code,
                exchange=document.exchange,
                announcement_date=document.announcement_date,
            )
            return _document_target(
                document, method="local_document", enqueue_parse=intent.enqueue_parse
            )
        candidate = repo.persisted_candidate(target.provider_document_id)
        if candidate is not None:
            _require_same_filing(
                target,
                security_code=_text(candidate.candidate.get("security_code")),
                exchange=_text(candidate.candidate.get("exchange")),
                announcement_date=_candidate_date(candidate.candidate),
            )
            return TargetRecord(
                target_id=ids.new_id("dprt"),
                resolution_method="local_candidate",
                admit_download=True,
                admit_parse=intent.enqueue_parse,
                provider=CNINFO_PROVIDER,
                provider_document_id=target.provider_document_id,
                index_source_access_id=candidate.index_source_access_id,
            )
    return None


@dataclass
class ResolverRunResult:
    claimed: int = 0
    matched: int = 0
    ambiguous: int = 0
    not_found: int = 0
    failed: int = 0
    deferred: int = 0
    abandoned: int = 0
    # Set when a provider refusal ended the run; the worker maps it onto the
    # acquisition report's existing cooldown flags.
    provider_refusal: str | None = None

    @property
    def concluded(self) -> int:
        return self.matched + self.ambiguous + self.not_found


class ProcessingRequestResolver:
    """Worker side: resolve due discovery requests within one maintenance pass."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], ProcessingRequestUnitOfWork],
        source: DisclosureSourcePort,
        profile_loader: Callable[[str], SourceCompanyProfile | None],
        index_interface: str,
        classify_failure: Callable[[BaseException], str],
        clock: Callable[[], datetime],
        should_stop: Callable[[], bool] = lambda: False,
        heartbeat: Callable[[], None] = lambda: None,
        monotonic: Callable[[], float] = time.monotonic,
        subject_resolver: SubjectResolver | None = None,
        deadline_seconds: float = DISCOVERY_DEADLINE_SECONDS,
        lease_seconds: int = ATTEMPT_LEASE_SECONDS,
    ) -> None:
        if not 0 < deadline_seconds < lease_seconds:
            raise ValueError("discovery deadline must be positive and inside the lease")
        self._uow_factory = uow_factory
        self._source = source
        self._profile_loader = profile_loader
        self._index_interface = index_interface
        self._classify = classify_failure
        self._clock = clock
        self._should_stop = should_stop
        self._heartbeat = heartbeat
        self._monotonic = monotonic
        self._subject_resolver = subject_resolver or SubjectResolver()
        self._deadline_seconds = deadline_seconds
        self._lease = timedelta(seconds=lease_seconds)

    def run(self, *, limit: int) -> ResolverRunResult:
        result = ResolverRunResult()
        if limit < 1 or self._should_stop():
            return result
        with self._uow_factory() as uow:
            due = uow.processing_requests.due_discovery_requests(
                now=self._clock(), limit=limit
            )
        for request in due:
            if self._should_stop():
                break
            attempt = self._claim(request, result)
            if attempt is None:
                continue
            result.claimed += 1
            target = target_from_json(request.target)
            if not isinstance(target, (ProviderDocumentTarget, TitleMatchTarget)):
                raise ValueError(f"request {request.request_id} has no discovery target")
            outcome, refusal = self._discover(request, target, attempt)
            setattr(result, outcome, getattr(result, outcome) + 1)
            self._heartbeat()
            if refusal is not None:
                result.provider_refusal = refusal
                break
        return result

    # -- claim -------------------------------------------------------------------
    def _claim(
        self, request: RequestRecord, result: ResolverRunResult
    ) -> AttemptRecord | None:
        with self._uow_factory() as uow:
            repo = uow.processing_requests
            repo.lock_idempotency_key(request.idempotency_key)
            if repo.target(request.request_id) is not None:
                return None
            attempts = repo.attempts(request.request_id)
            now = self._clock()
            if not discovery_due(attempts, now=now):
                return None
            latest = attempts[-1] if attempts else None
            if latest is not None and latest.finished_at is None:
                # Its executor is gone (lease expired); the next attempt waits
                # out the normal backoff.
                if _abandon_expired(repo, attempts, now=now):
                    uow.commit()
                    result.abandoned += 1
                return None
            if len(attempts) >= MAX_ATTEMPTS:
                return None
            try:
                attempt = repo.start_attempt(
                    attempt_id=ids.new_id("dpra"),
                    request_id=request.request_id,
                    attempt_no=len(attempts) + 1,
                    started_at=now,
                    lease_expires_at=now + self._lease,
                )
            except ProcessingAttemptConflict:
                return None
            uow.commit()
            return attempt

    # -- discovery -----------------------------------------------------------------
    def _discover(
        self,
        request: RequestRecord,
        target: ProviderDocumentTarget | TitleMatchTarget,
        attempt: AttemptRecord,
    ) -> tuple[str, str | None]:
        accessed_at = self._clock()
        deadline = self._monotonic() + self._deadline_seconds
        try:
            profile = self._profile_loader(target.security_code)
        except DisclosureAnchorError as exc:
            kind = self._classify(exc)
            profile_error = exc

            def record_profile_failure(uow: ProcessingRequestUnitOfWork) -> str | None:
                access = add_failed_cninfo_profile_access(
                    uow=uow, security_code=target.security_code, error=profile_error,
                    accessed_at=accessed_at,
                )
                return self._finish_failure(
                    uow,
                    request,
                    attempt,
                    kind=kind,
                    error=_stage_error(profile_error, stage="profile"),
                    profile_source_access_id=access.source_access_id,
                )

            return self._conclude(request, attempt, record_profile_failure), _refusal(kind)
        chunks = window_chunks(target.date_from, target.date_to, DISCOVERY_CHUNK_DAYS)
        refs: list[AnnouncementRef] = []
        for completed, (chunk_start, chunk_end) in enumerate(chunks):
            coverage = {
                "chunks_total": len(chunks),
                "chunks_completed": completed,
                "pending_chunk": {
                    "date_from": chunk_start.isoformat(),
                    "date_to": chunk_end.isoformat(),
                },
            }
            interruption = self._interruption(deadline)
            if interruption is not None:
                # Nothing is concluded from a partly traversed window.
                stopped = interruption
                return self._conclude(
                    request,
                    attempt,
                    lambda uow: self._record_interruption(
                        uow, request, target, attempt, profile, accessed_at,
                        kind=stopped, coverage=coverage,
                    ),
                ), None
            try:
                refs.extend(
                    self._source.search_announcements(
                        SourceSecurity(
                            security_code=target.security_code,
                            exchange=target.exchange,
                            security_name=profile.security_name if profile else None,
                        ),
                        DisclosureWindow(chunk_start, chunk_end),
                    )
                )
            except DisclosureAnchorError as exc:
                kind = self._classify(exc)
                index_error = exc
                return self._conclude(
                    request,
                    attempt,
                    lambda uow: self._record_index_failure(
                        uow, request, target, attempt, profile, index_error, accessed_at,
                        kind=kind, coverage=coverage,
                    ),
                ), _refusal(kind)
        unique = _unique(refs)
        try:
            outcome = self._conclude(
                request,
                attempt,
                lambda uow: self._record_discovery(
                    uow, request, target, attempt, profile, unique, accessed_at,
                    chunks_total=len(chunks), deadline=deadline,
                ),
            )
        except DisclosureAnchorError as exc:
            # Subject resolution refused the queried security; nothing from the
            # observation is admitted and the refusal is kept on the attempt.
            subject_error = exc

            def record_subject_failure(uow: ProcessingRequestUnitOfWork) -> str | None:
                profile_access = add_cninfo_profile_access(
                    uow=uow, security_code=target.security_code, profile=profile,
                    accessed_at=accessed_at,
                )
                return self._finish_failure(
                    uow,
                    request,
                    attempt,
                    kind="item",
                    error=_stage_error(subject_error, stage="subject"),
                    profile_source_access_id=profile_access.source_access_id,
                    provider_row_count=len(unique),
                )

            outcome = self._conclude(request, attempt, record_subject_failure)
        return outcome, None

    def _interruption(self, deadline: float) -> str | None:
        if self._should_stop():
            return "worker_stop"
        if self._monotonic() >= deadline:
            return "deadline"
        return None

    def _conclude(
        self,
        request: RequestRecord,
        attempt: AttemptRecord,
        record: Callable[[ProcessingRequestUnitOfWork], str | None],
    ) -> str:
        """Commit one outcome of ``attempt`` only while it still owns the request.

        Under the request's key lock the attempt must still be the latest open
        attempt with an unexpired lease, and ``record``'s own finish repeats the
        lease check at its UPDATE. Without that authority nothing ``record``
        wrote is committed; the expired lease is closed through the same
        abandon path a reclaiming worker uses, keeping history and backoff.
        """

        with self._uow_factory() as uow:
            repo = uow.processing_requests
            repo.lock_idempotency_key(request.idempotency_key)
            outcome: str | None = None
            if self._holds_lease(uow, request, attempt):
                outcome = record(uow)
                if outcome is not None:
                    uow.commit()
        if outcome is not None:
            return outcome
        with self._uow_factory() as uow:
            repo = uow.processing_requests
            repo.lock_idempotency_key(request.idempotency_key)
            attempts = repo.attempts(request.request_id)
            if attempts and _abandon_expired(repo, attempts, now=self._clock()):
                uow.commit()
        return "abandoned"

    def _holds_lease(
        self,
        uow: ProcessingRequestUnitOfWork,
        request: RequestRecord,
        attempt: AttemptRecord,
    ) -> bool:
        repo = uow.processing_requests
        attempts = repo.attempts(request.request_id)
        latest = attempts[-1] if attempts else None
        return (
            latest is not None
            and latest.attempt_id == attempt.attempt_id
            and latest.finished_at is None
            and self._clock() < latest.lease_expires_at
            and repo.target(request.request_id) is None
        )

    def _finish_failure(
        self,
        uow: ProcessingRequestUnitOfWork,
        request: RequestRecord,
        attempt: AttemptRecord,
        *,
        kind: str,
        error: Mapping[str, object],
        profile_source_access_id: str | None = None,
        index_source_access_id: str | None = None,
        provider_row_count: int | None = None,
    ) -> str | None:
        """Close one attempt as failed (item) or deferred (refusal/stop/deadline).

        None when the lease no longer allowed it; the caller commits nothing.
        """

        repo = uow.processing_requests
        now = self._clock()
        outcome = "failed" if kind == "item" else "deferred"
        retryable = error.get("retryable") is True if outcome == "failed" else True
        history = [
            replace(item, finished_at=now, outcome=outcome)
            if item.attempt_id == attempt.attempt_id
            else item
            for item in repo.attempts(request.request_id)
        ]
        next_at = next_attempt_after(
            history, outcome=outcome, retryable=retryable, finished_at=now
        )
        if kind == "worker_stop" and next_at is not None:
            # A stop is not provider evidence: due again as soon as a worker runs.
            next_at = now
        payload = {**dict(error), "failure_kind": kind}
        closed = repo.finish_attempt(
            attempt_id=attempt.attempt_id,
            outcome=outcome,
            finished_at=now,
            next_attempt_at=next_at,
            profile_source_access_id=profile_source_access_id,
            index_source_access_id=index_source_access_id,
            provider_row_count=provider_row_count,
            error=payload,
        )
        return outcome if closed else None

    def _record_interruption(
        self,
        uow: ProcessingRequestUnitOfWork,
        request: RequestRecord,
        target: ProviderDocumentTarget | TitleMatchTarget,
        attempt: AttemptRecord,
        profile: SourceCompanyProfile | None,
        accessed_at: datetime,
        *,
        kind: str,
        coverage: Mapping[str, object],
    ) -> str | None:
        profile_access = add_cninfo_profile_access(
            uow=uow, security_code=target.security_code, profile=profile,
            accessed_at=accessed_at,
        )
        return self._finish_failure(
            uow,
            request,
            attempt,
            kind=kind,
            error={
                "stage": "index",
                "error_code": (
                    "worker_stop_requested" if kind == "worker_stop"
                    else "discovery_deadline_exceeded"
                ),
                "retryable": True,
                "coverage": dict(coverage),
            },
            profile_source_access_id=profile_access.source_access_id,
        )

    def _record_index_failure(
        self,
        uow: ProcessingRequestUnitOfWork,
        request: RequestRecord,
        target: ProviderDocumentTarget | TitleMatchTarget,
        attempt: AttemptRecord,
        profile: SourceCompanyProfile | None,
        error: DisclosureAnchorError,
        accessed_at: datetime,
        *,
        kind: str,
        coverage: Mapping[str, object],
    ) -> str | None:
        pending = coverage["pending_chunk"]
        assert isinstance(pending, Mapping)
        profile_access = add_cninfo_profile_access(
            uow=uow, security_code=target.security_code, profile=profile,
            accessed_at=accessed_at,
        )
        # A failed search proves nothing about the subject, so no ledger
        # row is created for it; an already-known security is referenced.
        security = uow.securities.get_by_code_exchange(
            target.security_code, target.exchange
        )
        failed = uow.source_accesses.add(
            e.SourceAccess(
                source_access_id=ids.new_source_access_id(),
                provider=CNINFO_PROVIDER,
                provider_interface=self._index_interface,
                dataset_key="p_info3015",
                query_params={
                    "scode": target.security_code,
                    "sdate": str(pending["date_from"]),
                    "edate": str(pending["date_to"]),
                },
                accessed_at=accessed_at,
                status="failed",
                error=_json(index_failure_error(error)),
                result_snapshot={"result": "failed"},
                company_id=None if security is None else security.company_id,
                security_id=None if security is None else security.security_id,
            )
        )
        return self._finish_failure(
            uow,
            request,
            attempt,
            kind=kind,
            error={**index_failure_error(error), "coverage": dict(coverage)},
            profile_source_access_id=profile_access.source_access_id,
            index_source_access_id=failed.source_access_id,
        )

    def _record_discovery(
        self,
        uow: ProcessingRequestUnitOfWork,
        request: RequestRecord,
        target: ProviderDocumentTarget | TitleMatchTarget,
        attempt: AttemptRecord,
        profile: SourceCompanyProfile | None,
        refs: Sequence[AnnouncementRef],
        accessed_at: datetime,
        *,
        chunks_total: int,
        deadline: float,
    ) -> str | None:
        interruption = self._interruption(deadline)
        if interruption is not None:
            # The last provider call can return after a stop or past the
            # deadline; its observation is then discarded, never bound.
            return self._record_interruption(
                uow, request, target, attempt, profile, accessed_at,
                kind=interruption,
                coverage={"chunks_total": chunks_total, "chunks_completed": chunks_total},
            )
        repo = uow.processing_requests
        matches = _matching_refs(target, refs)
        selected = matches if len(matches) == 1 else []
        local: LocalDocument | None = None
        conflict: dict[str, object] | None = None
        if selected:
            # A local row can appear while the provider is being searched; it
            # binds only if it is the requested filing, as on the POST path.
            local = repo.latest_provider_document(
                provider=CNINFO_PROVIDER, provider_document_id=selected[0].provider_document_id
            )
            if local is not None:
                conflict = _local_conflict(target, local)
        provider_org_id = profile.provider_org_id if profile else None
        profile_access = add_cninfo_profile_access(
            uow=uow, security_code=target.security_code, profile=profile,
            accessed_at=accessed_at,
        )
        subject = resolve_index_subject(
            uow,
            resolver=self._subject_resolver,
            security_code=target.security_code,
            exchange=target.exchange,
            profile=profile,
            profile_access=profile_access,
        )
        record_cninfo_org_identifier(
            uow,
            company_id=subject.company.company_id,
            provider_org_id=provider_org_id,
            source_access_id=profile_access.source_access_id,
            observed_at=accessed_at,
        )
        candidates = [
            candidate_snapshot(ref, exchange=target.exchange, provider_org_id=provider_org_id)
            for ref in matches
        ]
        choices: list[dict[str, object]] = []
        if len(matches) > 1:
            ranked = sorted(
                candidates,
                key=lambda item: (
                    str(item.get("announcement_date") or ""),
                    str(item.get("provider_document_id") or ""),
                ),
                reverse=True,
            )
            for candidate in ranked[:MAX_CHOICES]:
                known = repo.latest_provider_document(
                    provider=CNINFO_PROVIDER,
                    provider_document_id=str(candidate.get("provider_document_id")),
                )
                choices.append(
                    choice_from_candidate(
                        candidate,
                        document_id=(
                            known.document_id
                            if known is not None and _local_conflict(target, known) is None
                            else None
                        ),
                    )
                )
        error: dict[str, object] | None = None
        if conflict is not None:
            conflict["observed"] = choice_from_candidate(candidates[0], document_id=None)
            error = {"stage": "resolution", "retryable": False, **conflict}
            outcome = "failed"
        else:
            outcome = "matched" if selected else ("ambiguous" if matches else "not_found")
        bound = bool(selected) and conflict is None
        index_access = uow.source_accesses.add(
            _discovery_access(
                request=request,
                target=target,
                interface=self._index_interface,
                refs=refs,
                selected=selected if bound else [],
                selected_snapshots=candidates if bound else [],
                outcome=outcome,
                matched_count=len(matches),
                choices=choices,
                conflict=conflict,
                chunks_total=chunks_total,
                provider_org_id=provider_org_id,
                profile_source_access_id=profile_access.source_access_id,
                company_id=subject.company.company_id,
                security_id=subject.security.security_id,
                accessed_at=accessed_at,
            )
        )
        closed = repo.finish_attempt(
            attempt_id=attempt.attempt_id,
            outcome=outcome,
            finished_at=self._clock(),
            profile_source_access_id=profile_access.source_access_id,
            index_source_access_id=index_access.source_access_id,
            provider_row_count=len(refs),
            choices=choices if outcome == "ambiguous" else None,
            choices_truncated=len(matches) > MAX_CHOICES,
            error=error,
        )
        if not closed:
            return None
        if bound:
            repo.add_target(
                request_id=request.request_id,
                target=_discovered_target(
                    local=local,
                    provider_document_id=selected[0].provider_document_id,
                    attempt_id=attempt.attempt_id,
                    index_source_access_id=index_access.source_access_id,
                    enqueue_parse=request.enqueue_parse,
                ),
            )
        return outcome


def _abandon_expired(
    repo: ProcessingRequestRepository, attempts: Sequence[AttemptRecord], *, now: datetime
) -> bool:
    """Close the latest attempt as abandoned when its lease ended by ``now``.

    The abandoned attempt stays in history and spends one item failure; the
    repository refuses an attempt that is already closed or still leased.
    """

    latest = attempts[-1]
    closed = [*attempts[:-1], replace(latest, finished_at=now, outcome="abandoned")]
    return repo.abandon_attempt(
        attempt_id=latest.attempt_id,
        finished_at=now,
        next_attempt_at=next_attempt_after(
            closed, outcome="abandoned", retryable=True, finished_at=now
        ),
        error={
            "stage": "resolution",
            "error_code": "attempt_lease_expired",
            "retryable": True,
        },
    )


def _refusal(kind: str) -> str | None:
    return kind if kind in PROVIDER_REFUSALS else None


def _unique(refs: Sequence[AnnouncementRef]) -> list[AnnouncementRef]:
    seen: set[str] = set()
    unique: list[AnnouncementRef] = []
    for ref in refs:
        if ref.provider_document_id in seen:
            continue
        seen.add(ref.provider_document_id)
        unique.append(ref)
    return unique


def _document_target(
    document: LocalDocument, *, method: str, enqueue_parse: bool
) -> TargetRecord:
    if document.raw_file_hash is None:
        raise ProcessingTargetNotFound(
            f"document {document.document_id} has no archived original"
        )
    has_provider = document.provider == CNINFO_PROVIDER and bool(document.provider_document_id)
    return TargetRecord(
        target_id=ids.new_id("dprt"),
        resolution_method=method,
        admit_download=False,
        admit_parse=enqueue_parse and document.status != "published",
        provider=CNINFO_PROVIDER if has_provider else None,
        provider_document_id=document.provider_document_id if has_provider else None,
        document_id=document.document_id,
        raw_file_hash=document.raw_file_hash,
    )


def _discovered_target(
    *,
    local: LocalDocument | None,
    provider_document_id: str,
    attempt_id: str,
    index_source_access_id: str,
    enqueue_parse: bool,
) -> TargetRecord:
    """The bound target of a unique match; ``local`` already passed _local_conflict."""

    if local is not None:
        return TargetRecord(
            target_id=ids.new_id("dprt"),
            resolution_method="provider_discovery",
            admit_download=False,
            admit_parse=enqueue_parse and local.status != "published",
            attempt_id=attempt_id,
            provider=CNINFO_PROVIDER,
            provider_document_id=provider_document_id,
            document_id=local.document_id,
            raw_file_hash=local.raw_file_hash,
            index_source_access_id=index_source_access_id,
        )
    return TargetRecord(
        target_id=ids.new_id("dprt"),
        resolution_method="provider_discovery",
        admit_download=True,
        admit_parse=enqueue_parse,
        attempt_id=attempt_id,
        provider=CNINFO_PROVIDER,
        provider_document_id=provider_document_id,
        index_source_access_id=index_source_access_id,
    )


def _matching_refs(
    target: ProviderDocumentTarget | TitleMatchTarget, refs: Sequence[AnnouncementRef]
) -> list[AnnouncementRef]:
    matches: list[AnnouncementRef] = []
    for ref in refs:
        if ref.security_code != target.security_code:
            continue
        if not target.date_from <= ref.announcement_date <= target.date_to:
            continue
        if isinstance(target, ProviderDocumentTarget):
            hit = ref.provider_document_id == target.provider_document_id
        else:
            hit = title_matches(ref.title, target.title_contains)
        if hit:
            matches.append(ref)
    return matches


def _discovery_access(
    *,
    request: RequestRecord,
    target: ProviderDocumentTarget | TitleMatchTarget,
    interface: str,
    refs: Sequence[AnnouncementRef],
    selected: Sequence[AnnouncementRef],
    selected_snapshots: Sequence[Mapping[str, object]],
    outcome: str,
    matched_count: int,
    choices: Sequence[Mapping[str, object]],
    conflict: Mapping[str, object] | None,
    chunks_total: int,
    provider_org_id: str | None,
    profile_source_access_id: str,
    company_id: str,
    security_id: str,
    accessed_at: datetime,
) -> e.SourceAccess:
    # Only the selected candidate is acquirable from this snapshot; the other
    # provider rows are summarized, so a request never widens acquisition.
    snapshot: dict[str, object] = {
        "result": "ok" if selected_snapshots else "empty",
        "candidates": [dict(item) for item in selected_snapshots],
    }
    if selected_snapshots:
        snapshot["raw_records"] = [dict(ref.raw_record) for ref in selected]
        snapshot["identity_context"] = {
            "version": INDEX_IDENTITY_CONTEXT_VERSION,
            "query_profile_org_id": provider_org_id,
            "query_profile_source_access_id": profile_source_access_id,
            "candidate_org_source": "announcement_ref",
        }
    selection: dict[str, object] = {
        "contract_version": CONTRACT_VERSION,
        "request_id": request.request_id,
        "rule": (
            "provider_document_id"
            if isinstance(target, ProviderDocumentTarget)
            else "title_contains"
        ),
        "value": (
            target.provider_document_id
            if isinstance(target, ProviderDocumentTarget)
            else target.title_contains
        ),
        "window_chunks": chunks_total,
        "provider_row_count": len(refs),
        "matched_count": matched_count,
        "outcome": outcome,
        "choices": [dict(choice) for choice in choices],
    }
    if conflict is not None:
        # The unique provider match contradicts the local row holding its id:
        # both facts are kept and nothing is acquirable from this snapshot.
        selection["local_conflict"] = dict(conflict)
    snapshot["selection"] = selection
    return e.SourceAccess(
        source_access_id=ids.new_source_access_id(),
        provider=CNINFO_PROVIDER,
        provider_interface=interface,
        dataset_key="p_info3015",
        query_params={
            "scode": target.security_code,
            "sdate": target.date_from.isoformat(),
            "edate": target.date_to.isoformat(),
        },
        accessed_at=accessed_at,
        status="ok",
        result_hash=canonical_snapshot_sha256(snapshot),
        result_snapshot=snapshot,
        company_id=company_id,
        security_id=security_id,
    )


def _same_filing(
    target: ProviderDocumentTarget | TitleMatchTarget,
    *,
    security_code: str | None,
    exchange: str | None,
    announcement_date: date | None,
) -> bool:
    """Whether local facts fit the requested security, exchange and window."""

    return (
        security_code == target.security_code
        and exchange == target.exchange
        and announcement_date is not None
        and target.date_from <= announcement_date <= target.date_to
    )


def _filing_facts(
    *, security_code: str | None, exchange: str | None, announcement_date: date | None
) -> dict[str, object]:
    return {
        "security_code": security_code,
        "exchange": exchange,
        "announcement_date": None if announcement_date is None else announcement_date.isoformat(),
    }


def _require_same_filing(
    target: ProviderDocumentTarget,
    *,
    security_code: str | None,
    exchange: str | None,
    announcement_date: date | None,
) -> None:
    if not _same_filing(
        target,
        security_code=security_code,
        exchange=exchange,
        announcement_date=announcement_date,
    ):
        raise ProcessingTargetMismatch(
            "local filing contradicts the requested security or window",
            found=_filing_facts(
                security_code=security_code,
                exchange=exchange,
                announcement_date=announcement_date,
            ),
        )


def _local_conflict(
    target: ProviderDocumentTarget | TitleMatchTarget, local: LocalDocument
) -> dict[str, object] | None:
    """Why a local row holding the matched CNINFO id cannot be bound, if so."""

    found: dict[str, object] = {
        "document_id": local.document_id,
        **_filing_facts(
            security_code=local.security_code,
            exchange=local.exchange,
            announcement_date=local.announcement_date,
        ),
    }
    if not _same_filing(
        target,
        security_code=local.security_code,
        exchange=local.exchange,
        announcement_date=local.announcement_date,
    ):
        return {"error_code": "local_filing_mismatch", "found": found}
    if local.raw_file_hash is None:
        return {"error_code": "local_original_missing", "found": found}
    return None


def _stage_error(error: DisclosureAnchorError, *, stage: str) -> dict[str, object]:
    if isinstance(error, SourceRequestError):
        payload = dict(error.to_error(stage=stage))
        payload.setdefault("stage", stage)
        return payload
    return {
        "stage": stage,
        "error_code": type(error).__name__,
        "retryable": isinstance(error, SubjectIdentityRaceError),
    }


def _candidate_date(candidate: Mapping[str, object]) -> date | None:
    raw = candidate.get("announcement_date")
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
