"""Explicit processing requests: normalized intent and derived status (v1).

A request is one caller intent keyed by its idempotency key. Its status is a
pure function of durable facts (request, resolution attempts, bound target,
document/run/download facts read through the real queue predicates); nothing
here performs IO. Wire shape: report-root ``backend-api-contract.md`` and
``docs/implementation/design/agent-api-processing-requests.md``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import hashlib
import json
import re
from typing import Any
import unicodedata

from disclosure_anchor.domain.value_objects.security import infer_mainland_exchange


CONTRACT_VERSION = "disclosure-processing-request.v1"
# Discovery attempts run in the resident worker's maintenance cycle. Item
# failures (provider/subject errors, abandoned leases) are bounded by
# MAX_ITEM_FAILURES; provider refusals (throttle, quota, outage, stop) are
# deferrals that wait out a backoff without spending that budget, and every
# attempt of any kind is bounded by MAX_ATTEMPTS.
MAX_ITEM_FAILURES = 3
MAX_ATTEMPTS = 10
# A claimed attempt owns its request until this lease ends; the worker stops
# starting provider chunks after DISCOVERY_DEADLINE_SECONDS, well inside it.
ATTEMPT_LEASE_SECONDS = 900
DISCOVERY_DEADLINE_SECONDS = 300
# Same inclusive chunk size the CNINFO index adapter requests, so each chunk is
# one provider request and stop/deadline are checked between requests.
DISCOVERY_CHUNK_DAYS = 30
RETRY_BACKOFF_BASE_SECONDS = 300
RETRY_BACKOFF_MAX_SECONDS = 6 * 3600
MAX_WINDOW_DAYS = 366
MAX_CHOICES = 20
TITLE_QUERY_MIN_CHARS = 2
TITLE_QUERY_MAX_CHARS = 60
EXCHANGES = ("SSE", "SZSE", "BSE")
TARGET_KINDS = ("document", "provider_document", "title_match")
RESOLUTION_METHODS = (
    "document_id",
    "local_document",
    "local_candidate",
    "provider_discovery",
)
ATTEMPT_OUTCOMES = (
    "matched",
    "ambiguous",
    "not_found",
    "failed",
    "abandoned",
    "deferred",
)
_RETRYING_OUTCOMES = ("failed", "abandoned", "deferred")
_ITEM_FAILURE_OUTCOMES = ("failed", "abandoned")

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
REQUEST_ID_RE = re.compile(r"^dreq_[0-9A-HJKMNP-TV-Z]{26}$")
DOCUMENT_ID_RE = re.compile(r"^doc_[0-9A-HJKMNP-TV-Z]{26}$")
PROVIDER_DOCUMENT_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
SECURITY_CODE_RE = re.compile(r"^[0-9]{6}$")
_WHITESPACE_RE = re.compile(r"\s+")


class ProcessingRequestInvalid(ValueError):
    """One rejected request field; nothing is stored for it."""

    def __init__(self, field_name: str, message: str) -> None:
        self.field_name = field_name
        self.message = message
        super().__init__(f"{field_name}: {message}")


@dataclass(frozen=True)
class DocumentTarget:
    document_id: str

    @property
    def kind(self) -> str:
        return "document"

    def to_json(self) -> dict[str, object]:
        return {"kind": self.kind, "document_id": self.document_id}


@dataclass(frozen=True)
class ProviderDocumentTarget:
    security_code: str
    exchange: str
    date_from: date
    date_to: date
    provider_document_id: str

    @property
    def kind(self) -> str:
        return "provider_document"

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "security_code": self.security_code,
            "exchange": self.exchange,
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
            "provider_document_id": self.provider_document_id,
        }


@dataclass(frozen=True)
class TitleMatchTarget:
    security_code: str
    exchange: str
    date_from: date
    date_to: date
    title_contains: str

    @property
    def kind(self) -> str:
        return "title_match"

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "security_code": self.security_code,
            "exchange": self.exchange,
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
            "title_contains": self.title_contains,
        }


Target = DocumentTarget | ProviderDocumentTarget | TitleMatchTarget


@dataclass(frozen=True)
class ProcessingIntent:
    idempotency_key: str
    enqueue_parse: bool
    target: Target

    def canonical(self) -> dict[str, object]:
        """The intent the key is bound to; the key itself is not part of it."""

        return {
            "contract_version": CONTRACT_VERSION,
            "enqueue_parse": self.enqueue_parse,
            "target": self.target.to_json(),
        }

    @property
    def intent_sha256(self) -> str:
        return canonical_sha256(self.canonical())


def canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def normalize_title_query(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", value)).strip()


def title_match_key(value: str) -> str:
    return _WHITESPACE_RE.sub("", unicodedata.normalize("NFKC", value)).casefold()


def title_matches(title: str, query: str) -> bool:
    return title_match_key(query) in title_match_key(title)


def normalize_intent(
    payload: Mapping[str, object], *, today: date
) -> ProcessingIntent:
    """Validate and normalize one request body; ``today`` is Asia/Shanghai."""

    allowed = {"idempotency_key", "enqueue_parse", "target"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ProcessingRequestInvalid(unknown[0], "unknown field")
    key = payload.get("idempotency_key")
    if not isinstance(key, str) or not IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise ProcessingRequestInvalid(
            "idempotency_key", "must be 16-128 characters of [A-Za-z0-9._:-]"
        )
    enqueue_parse = payload.get("enqueue_parse", True)
    if not isinstance(enqueue_parse, bool):
        raise ProcessingRequestInvalid("enqueue_parse", "must be a boolean")
    raw_target = payload.get("target")
    if not isinstance(raw_target, Mapping):
        raise ProcessingRequestInvalid("target", "must be an object")
    return ProcessingIntent(
        idempotency_key=key,
        enqueue_parse=enqueue_parse,
        target=_normalize_target(raw_target, today=today),
    )


def _normalize_target(raw: Mapping[str, object], *, today: date) -> Target:
    kind = raw.get("kind")
    if kind == "document":
        _only_fields(raw, {"kind", "document_id"})
        document_id = raw.get("document_id")
        if not isinstance(document_id, str) or not DOCUMENT_ID_RE.fullmatch(document_id):
            raise ProcessingRequestInvalid("target.document_id", "must be a document id")
        return DocumentTarget(document_id=document_id)
    if kind == "provider_document":
        _only_fields(
            raw,
            {"kind", "security_code", "exchange", "date_from", "date_to",
             "provider_document_id"},
        )
        code, exchange = _security(raw)
        date_from, date_to = _window(raw, today=today)
        provider_document_id = raw.get("provider_document_id")
        if not isinstance(provider_document_id, str) or not PROVIDER_DOCUMENT_ID_RE.fullmatch(
            provider_document_id
        ):
            raise ProcessingRequestInvalid(
                "target.provider_document_id", "must be 1-64 characters of [0-9A-Za-z_-]"
            )
        return ProviderDocumentTarget(
            security_code=code,
            exchange=exchange,
            date_from=date_from,
            date_to=date_to,
            provider_document_id=provider_document_id,
        )
    if kind == "title_match":
        _only_fields(
            raw,
            {"kind", "security_code", "exchange", "date_from", "date_to",
             "title_contains"},
        )
        code, exchange = _security(raw)
        date_from, date_to = _window(raw, today=today)
        title = raw.get("title_contains")
        if not isinstance(title, str):
            raise ProcessingRequestInvalid("target.title_contains", "must be a string")
        normalized = normalize_title_query(title)
        if not TITLE_QUERY_MIN_CHARS <= len(normalized) <= TITLE_QUERY_MAX_CHARS:
            raise ProcessingRequestInvalid(
                "target.title_contains",
                f"must be {TITLE_QUERY_MIN_CHARS}-{TITLE_QUERY_MAX_CHARS} characters",
            )
        return TitleMatchTarget(
            security_code=code,
            exchange=exchange,
            date_from=date_from,
            date_to=date_to,
            title_contains=normalized,
        )
    raise ProcessingRequestInvalid("target.kind", f"must be one of {', '.join(TARGET_KINDS)}")


def _only_fields(raw: Mapping[str, object], allowed: set[str]) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ProcessingRequestInvalid(f"target.{unknown[0]}", "unknown field for this kind")


def _security(raw: Mapping[str, object]) -> tuple[str, str]:
    code = raw.get("security_code")
    if not isinstance(code, str) or not SECURITY_CODE_RE.fullmatch(code):
        raise ProcessingRequestInvalid("target.security_code", "must be 6 digits")
    exchange = raw.get("exchange")
    if not isinstance(exchange, str) or exchange not in EXCHANGES:
        raise ProcessingRequestInvalid(
            "target.exchange", f"must be one of {', '.join(EXCHANGES)}"
        )
    if infer_mainland_exchange(code) != exchange:
        raise ProcessingRequestInvalid(
            "target.exchange", "does not match the security_code's exchange"
        )
    return code, exchange


def _window(raw: Mapping[str, object], *, today: date) -> tuple[date, date]:
    date_from = _date(raw.get("date_from"), "target.date_from")
    date_to = _date(raw.get("date_to"), "target.date_to")
    if date_from > date_to:
        raise ProcessingRequestInvalid("target.date_from", "must not be after date_to")
    if date_to > today:
        raise ProcessingRequestInvalid("target.date_to", "must not be in the future")
    if (date_to - date_from).days + 1 > MAX_WINDOW_DAYS:
        raise ProcessingRequestInvalid(
            "target.date_to", f"window must span at most {MAX_WINDOW_DAYS} days"
        )
    return date_from, date_to


def _date(value: object, field_name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ProcessingRequestInvalid(field_name, "must be a YYYY-MM-DD date")


def target_from_json(payload: Mapping[str, object]) -> Target:
    """Rebuild a stored, already-normalized target without re-checking time."""

    kind = payload.get("kind")
    if kind == "document":
        return DocumentTarget(document_id=str(payload["document_id"]))
    window = (
        date.fromisoformat(str(payload["date_from"])),
        date.fromisoformat(str(payload["date_to"])),
    )
    if kind == "provider_document":
        return ProviderDocumentTarget(
            security_code=str(payload["security_code"]),
            exchange=str(payload["exchange"]),
            date_from=window[0],
            date_to=window[1],
            provider_document_id=str(payload["provider_document_id"]),
        )
    if kind == "title_match":
        return TitleMatchTarget(
            security_code=str(payload["security_code"]),
            exchange=str(payload["exchange"]),
            date_from=window[0],
            date_to=window[1],
            title_contains=str(payload["title_contains"]),
        )
    raise ValueError(f"stored processing request has unknown target kind {kind!r}")


# -- durable facts -----------------------------------------------------------


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    idempotency_key: str
    intent_sha256: str
    target: Mapping[str, object]
    enqueue_parse: bool
    created_at: datetime


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    attempt_no: int
    started_at: datetime
    lease_expires_at: datetime
    finished_at: datetime | None = None
    next_attempt_at: datetime | None = None
    outcome: str | None = None
    profile_source_access_id: str | None = None
    index_source_access_id: str | None = None
    provider_row_count: int | None = None
    choices: tuple[Mapping[str, object], ...] | None = None
    choices_truncated: bool = False
    error: Mapping[str, object] | None = None


@dataclass(frozen=True)
class TargetRecord:
    target_id: str
    resolution_method: str
    admit_download: bool
    admit_parse: bool
    attempt_id: str | None = None
    provider: str | None = None
    provider_document_id: str | None = None
    document_id: str | None = None
    raw_file_hash: str | None = None
    index_source_access_id: str | None = None


@dataclass(frozen=True)
class FilingFacts:
    provider: str | None
    provider_document_id: str | None
    security_code: str | None
    exchange: str | None
    title: str | None
    announcement_date: date | None


@dataclass(frozen=True)
class DocumentFacts:
    document_id: str
    status: str
    raw_file_hash: str | None
    filing: FilingFacts
    current_processing_run_id: str | None = None
    running_processing_run_id: str | None = None
    parse_selected: bool = False
    # parse_admission_diagnosis remaining_blockers, read with resident scope.
    retry: Mapping[str, object] = field(default_factory=dict)
    # For status 'parsed': whether the build or publish predicate selects it.
    post_parse_selected: bool = False


@dataclass(frozen=True)
class DownloadFacts:
    selected: bool
    failed_download_count: int | None = None
    nonretryable_unresolved: bool = False
    last_failure: Mapping[str, object] | None = None


@dataclass(frozen=True)
class ProcessingFacts:
    request: RequestRecord
    attempts: Sequence[AttemptRecord]
    target: TargetRecord | None
    observed_at: datetime
    max_download_retries: int
    candidate_filing: FilingFacts | None = None
    document: DocumentFacts | None = None
    download: DownloadFacts | None = None


def window_chunks(start: date, end: date, days: int) -> list[tuple[date, date]]:
    """Split [start, end] (inclusive) into consecutive chunks of ``days``."""

    if days < 1:
        raise ValueError("chunk days must be positive")
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=days - 1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def lease_expired(attempt: AttemptRecord, *, now: datetime) -> bool:
    return attempt.finished_at is None and attempt.lease_expires_at <= now


def item_failure_count(attempts: Sequence[AttemptRecord]) -> int:
    return sum(1 for attempt in attempts if attempt.outcome in _ITEM_FAILURE_OUTCOMES)


def retry_backoff(attempt_count: int) -> timedelta:
    """Delay before the next attempt once ``attempt_count`` attempts exist."""

    exponent = max(0, attempt_count - 1)
    seconds = min(RETRY_BACKOFF_MAX_SECONDS, RETRY_BACKOFF_BASE_SECONDS * (3**exponent))
    return timedelta(seconds=seconds)


def next_attempt_after(
    attempts: Sequence[AttemptRecord],
    *,
    outcome: str,
    retryable: bool,
    finished_at: datetime,
) -> datetime | None:
    """When the request becomes due again after this outcome; None = terminal.

    ``attempts`` already includes the attempt being finished, with its outcome.
    """

    if outcome not in _RETRYING_OUTCOMES or not retryable:
        return None
    if item_failure_count(attempts) >= MAX_ITEM_FAILURES or len(attempts) >= MAX_ATTEMPTS:
        return None
    return finished_at + retry_backoff(len(attempts))


def discovery_due(attempts: Sequence[AttemptRecord], *, now: datetime) -> bool:
    """Whether the worker may claim (or reclaim) a resolution attempt now."""

    if not attempts:
        return True
    latest = attempts[-1]
    if latest.finished_at is None:
        return lease_expired(latest, now=now)
    return latest.next_attempt_at is not None and latest.next_attempt_at <= now


# -- status derivation -------------------------------------------------------


def derive_status(facts: ProcessingFacts) -> dict[str, Any]:
    resolution = _resolution(facts)
    filing = _filing(facts)
    original = _original(facts)
    parse = _parse(facts)
    state, terminal, next_action = _summary(facts, resolution, original, parse)
    status: dict[str, Any] = {
        "state": state,
        "terminal": terminal,
        "next_action": next_action,
        "resolution": resolution,
        "filing": filing,
        "original": original,
        "parse": parse,
    }
    status["status_sha256"] = canonical_sha256(status)
    status["observed_at"] = _iso(facts.observed_at)
    return status


def _resolution(facts: ProcessingFacts) -> dict[str, Any]:
    attempts = list(facts.attempts)
    latest = attempts[-1] if attempts else None
    finished = [attempt for attempt in attempts if attempt.finished_at is not None]
    evidence = finished[-1] if finished else None
    next_attempt_at: datetime | None = None
    if facts.target is not None:
        matched = next(
            (a for a in attempts if a.attempt_id == facts.target.attempt_id), None
        )
        if matched is not None:
            evidence = matched
        state = "resolved"
    elif latest is None:
        # Accepted, waiting for the worker's maintenance cycle to claim it.
        state = "pending"
    elif latest.finished_at is None:
        # An expired lease is reclaimed by the next maintenance pass.
        state = "pending" if lease_expired(latest, now=facts.observed_at) else "discovering"
    elif latest.outcome == "ambiguous":
        state = "needs_selection"
    elif latest.outcome == "not_found":
        state = "not_found"
    elif latest.next_attempt_at is not None:
        state = "pending"
        next_attempt_at = latest.next_attempt_at
    else:
        state = "failed"
    return {
        "state": state,
        "method": facts.target.resolution_method if facts.target is not None else None,
        "attempt_count": len(attempts),
        "max_attempts": MAX_ATTEMPTS,
        "item_failure_count": item_failure_count(attempts),
        "max_item_failures": MAX_ITEM_FAILURES,
        "next_attempt_at": None if next_attempt_at is None else _iso(next_attempt_at),
        "last_attempt": None if latest is None else _attempt_summary(latest),
        "provider_row_count": None if evidence is None else evidence.provider_row_count,
        "choices": (
            [dict(choice) for choice in evidence.choices]
            if evidence is not None and evidence.choices is not None
            else []
        ),
        "choices_truncated": bool(evidence is not None and evidence.choices_truncated),
        "index_source_access_id": (
            facts.target.index_source_access_id
            if facts.target is not None
            else (None if evidence is None else evidence.index_source_access_id)
        ),
    }


def _attempt_summary(attempt: AttemptRecord) -> dict[str, Any]:
    error = attempt.error or {}
    retryable = error.get("retryable") if attempt.error is not None else None
    return {
        "outcome": attempt.outcome,
        "started_at": _iso(attempt.started_at),
        "finished_at": None if attempt.finished_at is None else _iso(attempt.finished_at),
        "error_code": error.get("error_code") if attempt.error is not None else None,
        "retryable": retryable if isinstance(retryable, bool) else None,
    }


def _filing(facts: ProcessingFacts) -> dict[str, Any] | None:
    if facts.target is None:
        return None
    if facts.document is not None:
        filing = facts.document.filing
        document_id: str | None = facts.document.document_id
    elif facts.candidate_filing is not None:
        filing = facts.candidate_filing
        document_id = None
    else:
        filing = FilingFacts(
            provider=facts.target.provider,
            provider_document_id=facts.target.provider_document_id,
            security_code=None,
            exchange=None,
            title=None,
            announcement_date=None,
        )
        document_id = None
    return {
        "provider": filing.provider,
        "provider_document_id": filing.provider_document_id,
        "security_code": filing.security_code,
        "exchange": filing.exchange,
        "title": filing.title,
        "announcement_date": (
            None if filing.announcement_date is None else filing.announcement_date.isoformat()
        ),
        "document_id": document_id,
    }


def _original(facts: ProcessingFacts) -> dict[str, Any]:
    body: dict[str, Any] = {
        "state": "pending_resolution",
        "document_id": None,
        "raw_file_hash": None,
        "text_path": None,
        "blocked_reason": None,
        "failed_download_count": None,
        "max_download_retries": facts.max_download_retries,
        "last_download_failure": None,
    }
    if facts.target is None:
        return body
    if facts.document is not None:
        body.update(
            state="archived",
            document_id=facts.document.document_id,
            raw_file_hash=facts.document.raw_file_hash,
            text_path=f"/v1/documents/{facts.document.document_id}/text",
        )
        return body
    download = facts.download or DownloadFacts(selected=False)
    body["failed_download_count"] = download.failed_download_count
    body["last_download_failure"] = (
        None if download.last_failure is None else dict(download.last_failure)
    )
    if download.selected:
        body["state"] = "download_queued"
        return body
    body["state"] = "download_blocked"
    if (
        download.failed_download_count is not None
        and download.failed_download_count >= facts.max_download_retries
    ):
        body["blocked_reason"] = "download_retry_exhausted"
    elif download.nonretryable_unresolved:
        body["blocked_reason"] = "download_nonretryable_failure"
    else:
        body["blocked_reason"] = "download_not_selected"
    return body


def _parse(facts: ProcessingFacts) -> dict[str, Any]:
    requested = facts.request.enqueue_parse
    body: dict[str, Any] = {
        "state": "pending_resolution",
        "requested": requested,
        "document_status": None,
        "current_processing_run_id": None,
        "running_processing_run_id": None,
        "units_path": None,
        "blocked_reasons": [],
        "retry": None,
    }
    if facts.target is None:
        return body
    document = facts.document
    if document is None:
        body["state"] = "waiting_for_original" if requested else "not_requested"
        return body
    body.update(
        document_status=document.status,
        current_processing_run_id=document.current_processing_run_id,
        running_processing_run_id=document.running_processing_run_id,
        retry=_retry_facts(document.retry) if document.retry else None,
    )
    if document.status == "published":
        body["state"] = "published"
        body["units_path"] = f"/v1/documents/{document.document_id}/units"
    elif document.running_processing_run_id is not None:
        body["state"] = "parse_running"
    elif document.status == "parsed":
        if document.post_parse_selected:
            body["state"] = "post_processing"
        else:
            body["state"] = "parse_blocked"
            body["blocked_reasons"] = ["post_processing_not_selected"]
    elif document.parse_selected:
        body["state"] = "parse_queued"
    elif not requested:
        body["state"] = "not_requested"
    else:
        body["state"] = "parse_blocked"
        body["blocked_reasons"] = _parse_blockers(document.retry)
    return body


_RETRY_FIELDS = (
    "item_failure_count",
    "charged_failure_count",
    "max_item_failures",
    "max_charged_failures",
    "latest_failed_run_id",
    "latest_failed_run_retryable",
    "latest_failed_run_released",
    "unreleased_contract_failures",
)


def _retry_facts(retry: Mapping[str, object]) -> dict[str, Any]:
    facts = {name: retry.get(name) for name in _RETRY_FIELDS}
    facts["latest_failed_run_released"] = bool(retry.get("latest_failed_run_released"))
    return facts


def _parse_blockers(retry: Mapping[str, object]) -> list[str]:
    reasons: list[str] = []
    if retry.get("latest_failed_run_retryable") is False and not retry.get(
        "latest_failed_run_released"
    ):
        reasons.append("latest_failure_nonretryable")
    contract = retry.get("unreleased_contract_failures")
    if isinstance(contract, int) and contract > 0:
        reasons.append("unreleased_contract_failure")
    items = retry.get("item_failure_count")
    max_items = retry.get("max_item_failures")
    if isinstance(items, int) and isinstance(max_items, int) and items >= max_items:
        reasons.append("item_retry_budget_exhausted")
    charged = retry.get("charged_failure_count")
    max_charged = retry.get("max_charged_failures")
    if isinstance(charged, int) and isinstance(max_charged, int) and charged >= max_charged:
        reasons.append("charged_retry_budget_exhausted")
    return reasons or ["not_selected_by_queue"]


def _summary(
    facts: ProcessingFacts,
    resolution: Mapping[str, Any],
    original: Mapping[str, Any],
    parse: Mapping[str, Any],
) -> tuple[str, bool, str]:
    resolution_state = resolution["state"]
    if resolution_state in ("pending", "discovering"):
        return "resolving", False, "wait"
    if resolution_state == "needs_selection":
        return "needs_selection", True, "choose_and_submit_new_request"
    if resolution_state == "not_found":
        return "not_found", True, "none"
    if resolution_state == "failed":
        return "resolution_failed", True, "none"
    if original["state"] == "download_queued":
        return "download_queued", False, "wait"
    if original["state"] == "download_blocked":
        return "download_blocked", True, "operator_action_required"
    parse_state = parse["state"]
    if parse_state == "published":
        return "published", True, "none"
    if not facts.request.enqueue_parse:
        return "archived", True, "none"
    if parse_state in ("parse_running", "post_processing", "parse_queued"):
        return parse_state, False, "wait"
    return "parse_blocked", True, "operator_action_required"


def _iso(value: datetime) -> str:
    return value.isoformat()


def choice_from_candidate(
    candidate: Mapping[str, object], *, document_id: str | None
) -> dict[str, object]:
    return {
        "provider_document_id": candidate.get("provider_document_id"),
        "title": candidate.get("title"),
        "announcement_date": candidate.get("announcement_date"),
        "security_code": candidate.get("security_code"),
        "exchange": candidate.get("exchange"),
        "document_id": document_id,
    }


def filing_from_candidate(candidate: Mapping[str, object]) -> FilingFacts:
    raw_date = candidate.get("announcement_date")
    announcement_date: date | None = None
    if isinstance(raw_date, str):
        try:
            announcement_date = date.fromisoformat(raw_date[:10])
        except ValueError:
            announcement_date = None
    return FilingFacts(
        provider="cninfo",
        provider_document_id=_text_or_none(candidate.get("provider_document_id")),
        security_code=_text_or_none(candidate.get("security_code")),
        exchange=_text_or_none(candidate.get("exchange")),
        title=_text_or_none(candidate.get("title")),
        announcement_date=announcement_date,
    )


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
