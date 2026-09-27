"""Closed contracts for historical security bindings and retained registration.

A historical binding is an operator-named, hash-bound decision that one old
exchange code of an already source-anchored company may carry that company's
acquisitions within an approved announcement range. It is stored as one
``local:historical_security_binding.v1`` SourceAccess; its ``result_hash`` is
the canonical hash of the decision below.

A retained registration registers an already archived raw PDF whose earlier
download attempt failed after archiving. The successful registration access
names the exact failed access it resolves (``recovery_of_source_access_id``).

Every document here is closed: unknown or duplicate keys, non-finite numbers,
strings PostgreSQL JSONB cannot store (NUL, lone surrogates) and values
outside the closed vocabularies are refused. Identity is the canonical
compact sorted UTF-8 encoding (``closed_document.canonical_bytes``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
import re
from typing import Any, Literal, Optional
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from disclosure_anchor.application.contracts.closed_document import (
    SHA256_RE,
    canonical_bytes,
    sha256_of,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads
from disclosure_anchor.domain import entities as e
from disclosure_anchor.domain.ids import is_internal_id
from disclosure_anchor.domain.value_objects import canonical_security_identity


PROVIDER = "cninfo"
HISTORICAL_SECURITY_STATUS = "historical"

HISTORICAL_SECURITY_BINDING_SCHEMA = "historical-security-binding.v1"
HISTORICAL_SECURITY_BINDING_INTERFACE = "local:historical_security_binding.v1"
HISTORICAL_SECURITY_BINDING_DATASET = "historical_security_binding.v1"
HISTORICAL_SECURITY_BINDING_PLAN_SCHEMA = "historical-security-binding-plan.v1"
SAME_ENTITY_CODE_CHANGE = "same_legal_entity_security_code_change"

RETAINED_REGISTRATION_INTERFACE = "local:register_retained_pdf.v1"
RETAINED_REGISTRATION_DATASET = "retained_archive_registration.v1"
RETAINED_REGISTRATION_SCHEMA = "retained-archive-registration.v1"
RETAINED_REGISTRATION_REQUEST_SCHEMA = "retained-registration-request.v1"
RETAINED_REGISTRATION_PLAN_SCHEMA = "retained-registration-plan.v1"
HISTORICAL_ACQUISITION_PROVENANCE_SCHEMA = "historical-acquisition-provenance.v1"

# Index interfaces whose candidates a binding may approve. Mirrors the
# provider index interfaces of sync_disclosure_index without importing a use
# case into the contract layer.
BINDABLE_INDEX_INTERFACES = frozenset({"cninfo:p_info3015", "cninfo:hisAnnouncement"})
DOWNLOAD_FAILURE_INTERFACE = "cninfo:download_pdf"
PROFILE_INTERFACE = "cninfo:p_stock2100"

# Failure classes a retained registration may resolve. Widening this set is a
# contract change: an operator-visible exclusion is not evidence that the
# stored raw may now be registered.
RECOVERABLE_FAILURE_ERROR_CODES = frozenset({"registration_metadata_error"})

# How the retained raw was associated with the failed attempt.
POST_FAILURE_ARCHIVE_INVENTORY = "post_failure_archive_inventory"
FAILURE_RECORD_ARCHIVE_BINDING = "failure_record_archive_binding"
ASSOCIATION_BASES = frozenset(
    {POST_FAILURE_ARCHIVE_INVENTORY, FAILURE_RECORD_ARCHIVE_BINDING}
)

# Index snapshot identity context written by sync (per-snapshot, additive).
INDEX_IDENTITY_CONTEXT_VERSION = 1

MAX_CONTRACT_BYTES = 8 * 1024 * 1024
MAX_PLAN_ITEMS = 10_000

_CNINFO_EXCHANGES = frozenset({"SSE", "SZSE", "BSE"})
_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_EVIDENCE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
_URL_RE = re.compile(r"^https?://[^\s\x00-\x1f\x7f]+$")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_YEAR_RE = re.compile(r"^[0-9]{4}$")


class ContractViolation(ValueError):
    """A closed document failed its contract; the message names the field."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _require_iso_date(value: str, *, label: str) -> date:
    if _ISO_DATE_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be an ISO date YYYY-MM-DD")
    return date.fromisoformat(value)


def _require_internal_id(value: str, *, label: str, prefix: str) -> None:
    if not is_internal_id(value) or not value.startswith(prefix + "_"):
        raise ValueError(f"{label} must be a canonical {prefix}_ id")


def _require_sha(value: str, *, label: str) -> None:
    if SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical sha256")


def _require_text(value: str, *, label: str, maximum: int = 4096) -> None:
    if not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be non-blank and at most {maximum} characters")


def _archive_provider_document_id_ok(value: str) -> bool:
    # Exactly the provider_document_id components the raw archive path
    # builder writes: never empty or dot-leading ("." and ".." included), no
    # separator of either kind, no control/format/unassigned character.
    if _SAFE_COMPONENT_RE.fullmatch(value):
        return True
    return (
        bool(value)
        and not value.startswith(".")
        and "/" not in value
        and "\\" not in value
        and not any(unicodedata.category(char).startswith("C") for char in value)
    )


def _require_retained_archive_relpath(relpath: str, *, raw_file_hash: str) -> None:
    """``raw_documents/<provider>/<code>/<year>/<pid>/sha256_<hex>.pdf`` of this hash."""

    parts = relpath.split("/")
    if (
        len(parts) != 6
        or parts[0] != "raw_documents"
        or _SAFE_COMPONENT_RE.fullmatch(parts[1]) is None
        or _SAFE_COMPONENT_RE.fullmatch(parts[2]) is None
        or _YEAR_RE.fullmatch(parts[3]) is None
        or not _archive_provider_document_id_ok(parts[4])
        or parts[5] != f"sha256_{raw_file_hash.removeprefix('sha256:')}.pdf"
    ):
        raise ValueError(
            "raw_file_relpath must be the content-addressed archive path of raw_file_hash"
        )


def retained_archive_relpath(
    *, security_code: str, year: int, provider_document_id: str, raw_file_hash: str
) -> str:
    """The archive path a CNINFO download writes for one document version."""

    return "/".join(
        (
            "raw_documents",
            PROVIDER,
            security_code,
            str(year),
            provider_document_id,
            f"sha256_{raw_file_hash.removeprefix('sha256:')}.pdf",
        )
    )


class ExpectedUsccAnchorV1(_Closed):
    identifier_id: str
    value: str
    profile_source_access_id: str

    @model_validator(mode="after")
    def _valid(self) -> "ExpectedUsccAnchorV1":
        _require_internal_id(self.identifier_id, label="expected_uscc.identifier_id", prefix="ci")
        _require_internal_id(
            self.profile_source_access_id,
            label="expected_uscc.profile_source_access_id",
            prefix="sa",
        )
        if self.value != self.value.strip().upper() or not 1 <= len(self.value) <= 64:
            raise ValueError("expected_uscc.value must be the normalized identifier")
        return self


class OfficialEvidenceV1(_Closed):
    url: str
    sha256: str
    byte_count: int = Field(ge=1)
    announcement_id: str
    pages: list[int] = Field(min_length=1)
    local_evidence_ref: str

    @model_validator(mode="after")
    def _valid(self) -> "OfficialEvidenceV1":
        if _URL_RE.fullmatch(self.url) is None or len(self.url) > 2048:
            raise ValueError("official_evidence.url must be an http(s) URL")
        _require_sha(self.sha256, label="official_evidence.sha256")
        _require_text(self.announcement_id, label="official_evidence.announcement_id", maximum=128)
        if any(page < 1 for page in self.pages) or self.pages != sorted(set(self.pages)):
            raise ValueError("official_evidence.pages must be sorted unique positive page numbers")
        # A reference label, never an absolute or parent-escaping path.
        if (
            len(self.local_evidence_ref) > 512
            or _EVIDENCE_REF_RE.fullmatch(self.local_evidence_ref) is None
            or ".." in self.local_evidence_ref.split("/")
        ):
            raise ValueError("official_evidence.local_evidence_ref must be a safe relative reference")
        return self


class ApprovedQueryScopeV1(_Closed):
    company_id: str
    security_id: str


class AnnouncementRangeV1(_Closed):
    from_inclusive: str
    to_exclusive: str

    @model_validator(mode="after")
    def _valid(self) -> "AnnouncementRangeV1":
        start = _require_iso_date(self.from_inclusive, label="approved_announcement_range.from_inclusive")
        end = _require_iso_date(self.to_exclusive, label="approved_announcement_range.to_exclusive")
        if not start < end:
            raise ValueError("approved_announcement_range must be non-empty")
        return self

    def contains(self, day: date) -> bool:
        return date.fromisoformat(self.from_inclusive) <= day < date.fromisoformat(self.to_exclusive)


class QueryOrgObservationV1(_Closed):
    value: str
    provenance: Literal["profile_context"]
    source_access_id: str

    @model_validator(mode="after")
    def _valid(self) -> "QueryOrgObservationV1":
        _require_text(self.value, label="query_org_observation.value", maximum=128)
        _require_internal_id(
            self.source_access_id, label="query_org_observation.source_access_id", prefix="sa"
        )
        return self


class DecisionBasisV1(_Closed):
    ref: str
    sha256: str

    @model_validator(mode="after")
    def _valid(self) -> "DecisionBasisV1":
        _require_text(self.ref, label="decision_basis.ref", maximum=512)
        _require_sha(self.sha256, label="decision_basis.sha256")
        return self


class HistoricalSecurityBindingV1(_Closed):
    """One named same-entity code-change binding; the stored decision."""

    schema_id: Literal["historical-security-binding.v1"] = Field(alias="schema")
    provider: Literal["cninfo"]
    event_kind: Literal["same_legal_entity_security_code_change"]
    target_company_id: str
    exchange: str
    current_security_id: str
    current_code: str
    old_code: str
    security_code_effective_date: str
    previous_security_short_name: str
    current_security_short_name: str
    expected_uscc: ExpectedUsccAnchorV1
    official_evidence: OfficialEvidenceV1
    approved_index_interfaces: list[str] = Field(min_length=1)
    approved_query: ApprovedQueryScopeV1
    approved_announcement_range: AnnouncementRangeV1
    query_org_observation: Optional[QueryOrgObservationV1]
    decided_by: str
    decided_at: str
    reason: str
    decision_basis: list[DecisionBasisV1]


    @model_validator(mode="after")
    def _valid(self) -> "HistoricalSecurityBindingV1":
        _require_internal_id(self.target_company_id, label="target_company_id", prefix="co")
        _require_internal_id(self.current_security_id, label="current_security_id", prefix="sec")
        for label, code in (("current_code", self.current_code), ("old_code", self.old_code)):
            try:
                canonical = canonical_security_identity(code, self.exchange)
            except ValueError as exc:
                raise ValueError(f"{label} is not a valid {self.exchange} code: {exc}") from exc
            if canonical != (code, self.exchange):
                raise ValueError(f"{label}/exchange must already be canonical")
        if self.old_code == self.current_code:
            # Same code with a new short name needs no second security.
            raise ValueError("a code-change binding requires old_code != current_code")
        effective = _require_iso_date(
            self.security_code_effective_date, label="security_code_effective_date"
        )
        if date.fromisoformat(self.approved_announcement_range.to_exclusive) > effective:
            raise ValueError(
                "approved_announcement_range must end on or before the code effective date"
            )
        _require_text(self.previous_security_short_name, label="previous_security_short_name", maximum=128)
        _require_text(self.current_security_short_name, label="current_security_short_name", maximum=128)
        interfaces = self.approved_index_interfaces
        if interfaces != sorted(set(interfaces)) or not set(interfaces) <= BINDABLE_INDEX_INTERFACES:
            raise ValueError(
                "approved_index_interfaces must be a sorted unique subset of "
                f"{sorted(BINDABLE_INDEX_INTERFACES)}"
            )
        if (
            self.approved_query.company_id != self.target_company_id
            or self.approved_query.security_id != self.current_security_id
        ):
            # Same legal entity: the approved query owner is the company's
            # current security, never another company's scope.
            raise ValueError("approved_query must be the target company's current security")
        _require_text(self.decided_by, label="decided_by", maximum=128)
        _require_text(self.reason, label="reason", maximum=4096)
        try:
            decided_at = datetime.fromisoformat(self.decided_at)
        except ValueError as exc:
            raise ValueError("decided_at must be an ISO datetime") from exc
        if decided_at.tzinfo is None:
            raise ValueError("decided_at must carry a UTC offset")
        refs = [item.ref for item in self.decision_basis]
        if len(refs) != len(set(refs)):
            raise ValueError("decision_basis refs must be unique")
        return self

    def to_document(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_document())

    def sha256(self) -> str:
        return sha256_of(self.canonical_bytes())

    def facts(self) -> tuple[object, ...]:
        """The identity facts two coexisting bindings must agree on.

        Approved scopes and the org observation are per-decision: an org id
        is only ever a consistency check of the binding that carries it.
        """

        return (
            self.provider,
            self.event_kind,
            self.target_company_id,
            self.exchange,
            self.current_security_id,
            self.current_code,
            self.old_code,
            self.security_code_effective_date,
            self.expected_uscc.identifier_id,
            self.expected_uscc.value,
            self.expected_uscc.profile_source_access_id,
        )

    def approves(
        self,
        *,
        index_provider_interface: str,
        query_company_id: str | None,
        query_security_id: str | None,
        announcement_date: date,
    ) -> bool:
        return (
            index_provider_interface in self.approved_index_interfaces
            and query_company_id == self.approved_query.company_id
            and query_security_id == self.approved_query.security_id
            and self.approved_announcement_range.contains(announcement_date)
        )


class BindingEvidenceFileV1(_Closed):
    sha256: str
    byte_count: int = Field(ge=1)


class BindingPreflightV1(_Closed):
    action: Literal["create_historical_security", "append_binding", "already_recorded"]
    historical_security_id: Optional[str]
    existing_binding_source_access_ids: list[str]
    company_legal_name: str
    tracked_security_id: str


class HistoricalSecurityBindingPlanV1(_Closed):
    schema_id: Literal["historical-security-binding-plan.v1"] = Field(alias="schema")
    binding: HistoricalSecurityBindingV1
    binding_sha256: str
    evidence_file: BindingEvidenceFileV1
    preflight: BindingPreflightV1
    code_identity: str


    @model_validator(mode="after")
    def _valid(self) -> "HistoricalSecurityBindingPlanV1":
        if self.binding.sha256() != self.binding_sha256:
            raise ValueError("binding_sha256 does not match the binding document")
        if (
            self.evidence_file.sha256 != self.binding.official_evidence.sha256
            or self.evidence_file.byte_count != self.binding.official_evidence.byte_count
        ):
            raise ValueError("evidence_file does not match official_evidence")
        _require_sha(self.code_identity, label="code_identity")
        return self

    def to_document(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class RetainedRegistrationRequestItemV1(_Closed):
    failed_source_access_id: str
    index_source_access_id: str
    expected_raw_file_hash: str
    expected_byte_count: int = Field(ge=1)

    @model_validator(mode="after")
    def _valid(self) -> "RetainedRegistrationRequestItemV1":
        _require_internal_id(self.failed_source_access_id, label="failed_source_access_id", prefix="sa")
        _require_internal_id(self.index_source_access_id, label="index_source_access_id", prefix="sa")
        _require_sha(self.expected_raw_file_hash, label="expected_raw_file_hash")
        return self


class RetainedRegistrationRequestV1(_Closed):
    """Operator input: the exact failed accesses, never a query or glob."""

    schema_id: Literal["retained-registration-request.v1"] = Field(alias="schema")
    items: list[RetainedRegistrationRequestItemV1] = Field(min_length=1, max_length=MAX_PLAN_ITEMS)


    @model_validator(mode="after")
    def _valid(self) -> "RetainedRegistrationRequestV1":
        failed = [item.failed_source_access_id for item in self.items]
        if len(failed) != len(set(failed)):
            raise ValueError("failed_source_access_id values must be unique")
        return self

    def to_document(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class RetainedRegistrationPlanItemV1(_Closed):
    sequence: int = Field(ge=1)
    failed_source_access_id: str
    failed_access_projection_sha256: str
    failure_error_code: str
    failure_reason: Optional[str]
    provider_document_id: str
    index_source_access_id: str
    index_result_hash: str
    index_provider_interface: str
    candidate_sha256: str
    announcement_date: str
    original_candidate_code: str
    exchange: str
    acquisition_scope_company_id: str
    acquisition_scope_security_id: str
    target_company_id: str
    target_security_id: str
    raw_file_relpath: str
    raw_file_hash: str
    byte_count: int = Field(ge=1)
    association_basis: Literal["post_failure_archive_inventory", "failure_record_archive_binding"]
    document_state: Literal["absent", "existing_same_subject"]
    existing_document_id: Optional[str]
    preview_state: Literal["ready", "already_resolved"]
    existing_receipt_source_access_id: Optional[str]

    @model_validator(mode="after")
    def _valid(self) -> "RetainedRegistrationPlanItemV1":
        for label in ("failed_access_projection_sha256", "candidate_sha256", "raw_file_hash"):
            _require_sha(getattr(self, label), label=label)
        if not self.index_result_hash:
            raise ValueError("index_result_hash must be present")
        _require_iso_date(self.announcement_date, label="announcement_date")
        _require_retained_archive_relpath(self.raw_file_relpath, raw_file_hash=self.raw_file_hash)
        if (self.document_state == "absent") != (self.existing_document_id is None):
            raise ValueError("existing_document_id must be present exactly for an existing document")
        if (self.preview_state == "already_resolved") != (
            self.existing_receipt_source_access_id is not None
        ):
            raise ValueError("existing_receipt_source_access_id must match preview_state")
        return self


class RetainedRegistrationPlanV1(_Closed):
    schema_id: Literal["retained-registration-plan.v1"] = Field(alias="schema")
    provider: Literal["cninfo"]
    binding_source_access_id: str
    binding_sha256: str
    request_sha256: str
    code_identity: str
    max_items: int = Field(ge=1, le=MAX_PLAN_ITEMS)
    item_count: int = Field(ge=1, le=MAX_PLAN_ITEMS)
    total_byte_count: int = Field(ge=1)
    items: list[RetainedRegistrationPlanItemV1] = Field(min_length=1, max_length=MAX_PLAN_ITEMS)


    @model_validator(mode="after")
    def _valid(self) -> "RetainedRegistrationPlanV1":
        for label in ("binding_sha256", "request_sha256", "code_identity"):
            _require_sha(getattr(self, label), label=label)
        if self.item_count != len(self.items) or self.item_count > self.max_items:
            raise ValueError("item_count must equal the item list and not exceed max_items")
        if [item.sequence for item in self.items] != list(range(1, len(self.items) + 1)):
            raise ValueError("item sequence must be 1..n in order")
        failed = [item.failed_source_access_id for item in self.items]
        if len(failed) != len(set(failed)):
            raise ValueError("failed_source_access_id values must be unique")
        if self.total_byte_count != sum(item.byte_count for item in self.items):
            raise ValueError("total_byte_count must equal the item byte counts")
        return self

    def to_document(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_document())

    def sha256(self) -> str:
        return sha256_of(self.canonical_bytes())


def require_representable(value: object, *, label: str = "document") -> None:
    """Refuse strings PostgreSQL JSONB or UTF-8 cannot carry losslessly."""

    if isinstance(value, str):
        if "\x00" in value:
            raise ContractViolation(f"{label} contains a NUL character")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ContractViolation(f"{label} contains an unpaired surrogate") from exc
    elif isinstance(value, Mapping):
        for key, item in value.items():
            require_representable(key, label=label)
            require_representable(item, label=f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            require_representable(item, label=f"{label}[{index}]")


def _load_closed(payload: bytes, *, label: str) -> object:
    if type(payload) is not bytes or not payload or len(payload) > MAX_CONTRACT_BYTES:
        raise ContractViolation(f"{label} bytes are outside the closed envelope")
    try:
        decoded = strict_json_loads(payload)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ContractViolation(f"{label} is not strict UTF-8 JSON: {exc}") from exc
    if type(decoded) is not dict:
        raise ContractViolation(f"{label} must be a JSON object")
    require_representable(decoded, label=label)
    return decoded


def _validated(model: type[BaseModel], document: object, *, label: str) -> Any:
    try:
        return model.model_validate(document, strict=True, by_alias=True)
    except ValidationError as exc:
        raise ContractViolation(f"{label} violates its contract: {exc}") from exc


def load_binding(payload: bytes) -> HistoricalSecurityBindingV1:
    document = _load_closed(payload, label=HISTORICAL_SECURITY_BINDING_SCHEMA)
    binding: HistoricalSecurityBindingV1 = _validated(
        HistoricalSecurityBindingV1, document, label=HISTORICAL_SECURITY_BINDING_SCHEMA
    )
    return binding


def binding_from_snapshot(snapshot: object) -> HistoricalSecurityBindingV1:
    """Re-validate a stored binding decision read back from JSONB."""

    if not isinstance(snapshot, Mapping):
        raise ContractViolation("stored binding snapshot is not an object")
    require_representable(snapshot, label="stored binding")
    binding: HistoricalSecurityBindingV1 = _validated(
        HistoricalSecurityBindingV1, dict(snapshot), label="stored binding"
    )
    return binding


def load_request(payload: bytes) -> tuple[RetainedRegistrationRequestV1, str]:
    document = _load_closed(payload, label=RETAINED_REGISTRATION_REQUEST_SCHEMA)
    request: RetainedRegistrationRequestV1 = _validated(
        RetainedRegistrationRequestV1, document, label=RETAINED_REGISTRATION_REQUEST_SCHEMA
    )
    return request, sha256_of(canonical_bytes(request.to_document()))


def load_canonical_plan(
    payload: bytes, *, expected_sha256: str, model: type[BaseModel], label: str
) -> Any:
    """A plan executes only from its exact canonical bytes."""

    _require_sha(expected_sha256, label="expected plan sha256")
    if sha256_of(payload) != expected_sha256:
        raise ContractViolation(f"{label} sha256 does not match the expected value")
    document = _load_closed(payload, label=label)
    if canonical_bytes(document) != payload:
        raise ContractViolation(f"{label} is not in canonical encoding")
    return _validated(model, document, label=label)


def plan_bytes(document: Mapping[str, object]) -> bytes:
    require_representable(document, label="plan")
    return canonical_bytes(dict(document))


def candidate_sha256(candidate: Mapping[str, object]) -> str:
    """Identity of one stored index candidate (canonical JSON of the object)."""

    require_representable(candidate, label="candidate")
    return sha256_of(canonical_bytes(dict(candidate)))


def failed_access_projection(access: e.SourceAccess) -> dict[str, object]:
    """The original failed-attempt fields; later nullable columns are excluded."""

    accessed_at = access.accessed_at
    if accessed_at.tzinfo is None:
        raise ContractViolation("failed access accessed_at must be timezone-aware")
    return {
        "source_access_id": access.source_access_id,
        "provider": access.provider,
        "provider_interface": access.provider_interface,
        "dataset_key": access.dataset_key,
        "query_params": access.query_params,
        "accessed_at": accessed_at.astimezone(timezone.utc).isoformat(),
        "status": access.status,
        "result_hash": access.result_hash,
        "error": access.error,
        "result_snapshot": access.result_snapshot,
        "company_id": access.company_id,
        "security_id": access.security_id,
    }


def failed_access_projection_sha256(access: e.SourceAccess) -> str:
    projection = failed_access_projection(access)
    require_representable(projection, label="failed access")
    return sha256_of(canonical_bytes(projection))


__all__ = [
    "ASSOCIATION_BASES",
    "BINDABLE_INDEX_INTERFACES",
    "ContractViolation",
    "DOWNLOAD_FAILURE_INTERFACE",
    "FAILURE_RECORD_ARCHIVE_BINDING",
    "HISTORICAL_ACQUISITION_PROVENANCE_SCHEMA",
    "HISTORICAL_SECURITY_BINDING_DATASET",
    "HISTORICAL_SECURITY_BINDING_INTERFACE",
    "HISTORICAL_SECURITY_BINDING_PLAN_SCHEMA",
    "HISTORICAL_SECURITY_BINDING_SCHEMA",
    "HISTORICAL_SECURITY_STATUS",
    "HistoricalSecurityBindingPlanV1",
    "HistoricalSecurityBindingV1",
    "INDEX_IDENTITY_CONTEXT_VERSION",
    "POST_FAILURE_ARCHIVE_INVENTORY",
    "PROFILE_INTERFACE",
    "PROVIDER",
    "RECOVERABLE_FAILURE_ERROR_CODES",
    "RETAINED_REGISTRATION_DATASET",
    "RETAINED_REGISTRATION_INTERFACE",
    "RETAINED_REGISTRATION_PLAN_SCHEMA",
    "RETAINED_REGISTRATION_REQUEST_SCHEMA",
    "RETAINED_REGISTRATION_SCHEMA",
    "RetainedRegistrationPlanItemV1",
    "RetainedRegistrationPlanV1",
    "RetainedRegistrationRequestItemV1",
    "RetainedRegistrationRequestV1",
    "SAME_ENTITY_CODE_CHANGE",
    "binding_from_snapshot",
    "candidate_sha256",
    "failed_access_projection",
    "failed_access_projection_sha256",
    "load_binding",
    "load_canonical_plan",
    "load_request",
    "plan_bytes",
    "require_representable",
    "retained_archive_relpath",
]
