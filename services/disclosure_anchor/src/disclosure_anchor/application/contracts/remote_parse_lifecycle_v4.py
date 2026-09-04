"""Crash-safe, default-off remote parse lifecycle v4 contracts.

This module deliberately does not reinterpret any v1-v3 row.  It replaces the
abandoned stage/seal/promote draft with one write-ahead materialization intent,
one idempotent filesystem operation, one exact receipt, and a linear
cleanup/ACK chain.  Claim ownership and lease timestamps are operational head
fields and never enter these immutable canonical bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields, replace
from pathlib import PurePosixPath
from typing import Any, Literal, cast

from disclosure_anchor.application.contracts.local_materialization_manifest_v4 import (
    LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME,
    LocalMaterializationManifestV4,
)
from disclosure_anchor.application.contracts.parser_target import ParserTargetIdentity
from disclosure_anchor.application.contracts.provider_document_envelope import (
    PROVIDER_DOCUMENT_FILENAME,
    ProviderDocumentEnvelope,
    provider_document_envelope_to_bytes,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    STAGED_RESOURCE_STATE_TRANSITIONS,
    CleanupOutcome,
    ResourceCreditBucket,
    ResourceCreditVector,
    ResourceReservationInput,
    encode_resource_reservation_input,
)
from disclosure_anchor.application.contracts.staged_resource_paths import (
    staged_materialization_relpaths,
    staged_retained_relpaths,
    staged_snapshot_relpaths,
    validate_relative_resource_path_v4,
)
from disclosure_anchor.application.contracts.strict_json import strict_json_loads

CHECKPOINT_V4_CONTRACT = "remote-parse-checkpoint.v4"
RESOURCE_RESERVATION_V4_CONTRACT = "remote-parse-resource-reservation.v4"
MATERIALIZATION_INTENT_V4_CONTRACT = "remote-parse-materialization-intent.v4"
PROVIDER_ENVELOPE_CONTEXT_V4_CONTRACT = "provider-envelope-context.v4"
LOCAL_MATERIALIZATION_V4_CONTRACT = "local-materialization-receipt.v4"
CLEANUP_PLAN_V4_CONTRACT = "local-cleanup-plan.v4"
CLEANUP_RECEIPT_V4_CONTRACT = "local-cleanup-receipt.v4"
ACK_RECEIPT_V4_CONTRACT = "provider-ack-receipt.v4"

_MAX_INT = (1 << 63) - 1
_MAX_BYTES = 1024 * 1024
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")

CheckpointStateV4 = Literal[
    "prepared",
    "reconciling",
    "submitted",
    "remote_terminal",
    "materializing",
    "local_materialized",
    "publish_committed",
    "cleanup_pending",
    "ack_pending",
    "acked",
    "remote_failed",
    "local_failed",
    "pre_submission_failed",
    "preparation_failed",
    "superseded",
]
LocalResourceKind = Literal[
    "snapshot",
    "snapshot_part",
    "snapshot_part_owner",
    "spool",
    "spool_part",
    "spool_part_owner",
    "staging",
    "staging_marker",
    "output",
]
CleanupAction = Literal["delete", "transfer"]
CleanupDisposition = Literal["absent", "transferred"]
ProviderAckKind = Literal["consumed", "absent"]

_RESOURCE_KINDS = (
    "snapshot",
    "snapshot_part",
    "snapshot_part_owner",
    "spool",
    "spool_part",
    "spool_part_owner",
    "staging",
    "staging_marker",
    "output",
)
_RESOURCE_ORDER = {name: index for index, name in enumerate(_RESOURCE_KINDS)}
_FINAL_STATES = frozenset(
    {
        "acked",
        "remote_failed",
        "local_failed",
        "pre_submission_failed",
        "preparation_failed",
        "superseded",
    }
)
_ALL_STATES = frozenset(STAGED_RESOURCE_STATE_TRANSITIONS) | _FINAL_STATES
_EVIDENCE_FIELDS = (
    "preparation_intent_sha256",
    "snapshot_receipt_sha256",
    "submission_intent_sha256",
    "accepted_submission_sha256",
    "terminal_receipt_sha256",
    "materialization_intent_sha256",
    "local_materialization_receipt_sha256",
    "publication_winner_sha256",
    "failure_receipt_sha256",
    "supersession_receipt_sha256",
    "cleanup_plan_sha256",
    "cleanup_receipt_sha256",
    "ack_receipt_sha256",
)
_OUTCOME_EVIDENCE_FIELDS = (
    "publication_winner_sha256",
    "failure_receipt_sha256",
    "supersession_receipt_sha256",
)
_ALLOWED_NEW_EVIDENCE_BY_TRANSITION = {
    # A generation-zero head may be inserted before copying the source PDF.
    # The claimed PRE_FLIGHT transition then durably closes both the first
    # snapshot and the exact submission command in one successor append.
    ("prepared", "reconciling"): frozenset(
        {"snapshot_receipt_sha256", "submission_intent_sha256"}
    ),
    ("reconciling", "submitted"): frozenset(
        {"accepted_submission_sha256"}
    ),
    ("submitted", "remote_terminal"): frozenset(
        {"terminal_receipt_sha256"}
    ),
    ("remote_terminal", "materializing"): frozenset(
        {"materialization_intent_sha256"}
    ),
    ("materializing", "local_materialized"): frozenset(
        {"local_materialization_receipt_sha256"}
    ),
    ("local_materialized", "publish_committed"): frozenset(
        {"publication_winner_sha256"}
    ),
    **{
        (state, "cleanup_pending"): frozenset(
            {
                "failure_receipt_sha256",
                "supersession_receipt_sha256",
                "cleanup_plan_sha256",
            }
        )
        for state in (
            "prepared",
            "reconciling",
            "submitted",
            "remote_terminal",
            "materializing",
            "local_materialized",
            "publish_committed",
        )
    },
    ("cleanup_pending", "ack_pending"): frozenset(
        {"cleanup_receipt_sha256"}
    ),
    ("cleanup_pending", "pre_submission_failed"): frozenset(
        {"cleanup_receipt_sha256"}
    ),
    ("cleanup_pending", "superseded"): frozenset(
        {"cleanup_receipt_sha256"}
    ),
    ("ack_pending", "acked"): frozenset({"ack_receipt_sha256"}),
    ("ack_pending", "remote_failed"): frozenset({"ack_receipt_sha256"}),
    ("ack_pending", "local_failed"): frozenset({"ack_receipt_sha256"}),
    ("ack_pending", "superseded"): frozenset({"ack_receipt_sha256"}),
}


@dataclass(frozen=True, slots=True)
class ResourceReservationV4:
    attempt_id: str
    attempt_generation: int
    fence_identity: str
    document_id: str
    processing_run_id: str
    source_pdf_sha256: str
    source_byte_count: int
    source_page_count: int
    prepared_submission_identity_sha256: str
    request_sha256: str
    runtime_epoch_sha256: str
    process_profile_sha256: str
    credit_policy_sha256: str
    reservation_bucket: ResourceCreditBucket
    reservation_input_sha256: str
    snapshot_relpath: str
    snapshot_part_relpath: str
    snapshot_part_owner_relpath: str
    snapshot_lock_relpath: str
    reserved_credit: ResourceCreditVector
    contract_version: str = RESOURCE_RESERVATION_V4_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != RESOURCE_RESERVATION_V4_CONTRACT:
            raise ValueError("resource reservation contract is unsupported")
        _identity_tuple(
            self.attempt_id,
            self.fence_identity,
            self.document_id,
            self.processing_run_id,
        )
        for value, label in (
            (self.source_pdf_sha256, "source PDF"),
            (self.prepared_submission_identity_sha256, "prepared submission"),
            (self.request_sha256, "request"),
            (self.runtime_epoch_sha256, "runtime epoch"),
            (self.process_profile_sha256, "process profile"),
            (self.credit_policy_sha256, "credit policy"),
            (self.reservation_input_sha256, "reservation input"),
        ):
            _require_sha(value, label)
        _positive(self.source_byte_count, "source byte count")
        _positive(self.source_page_count, "source page count")
        _positive(self.attempt_generation, "attempt generation")
        _relative_path(self.snapshot_relpath, "snapshot")
        _relative_path(self.snapshot_part_relpath, "snapshot part")
        _relative_path(self.snapshot_part_owner_relpath, "snapshot part owner")
        _relative_path(self.snapshot_lock_relpath, "snapshot lock")
        if type(self.reserved_credit) is not ResourceCreditVector:
            raise ValueError("resource reservation requires an exact credit vector")
        reservation_input = encode_resource_reservation_input(
            ResourceReservationInput(
                source_pdf_sha256=self.source_pdf_sha256,
                source_byte_count=self.source_byte_count,
                source_page_count=self.source_page_count,
                process_profile_sha256=self.process_profile_sha256,
                credit_policy_sha256=self.credit_policy_sha256,
                bucket=self.reservation_bucket,
                reservation=self.reserved_credit,
            )
        )
        if self.reservation_input_sha256 != reservation_input.sha256:
            raise ValueError("resource reservation input hash does not close")
        expected = deterministic_local_resource_paths_v4(
            attempt_id=self.attempt_id,
            fence_identity=self.fence_identity,
            source_pdf_sha256=self.source_pdf_sha256,
            artifact_owner_identity=None,
            artifact_sha256=None,
            output_dir_name=None,
        )
        if (
            self.snapshot_relpath != expected["snapshot"]
            or self.snapshot_part_relpath != expected["snapshot_part"]
            or self.snapshot_part_owner_relpath != expected["snapshot_part_owner"]
            or self.snapshot_lock_relpath != expected["snapshot_lock"]
        ):
            raise ValueError("snapshot paths are not deterministic")
        if (
            self.reserved_credit.documents != 1
            or self.reserved_credit.snapshot_items != 1
            or self.reserved_credit.snapshot_bytes != self.source_byte_count
            or self.reserved_credit.remote_waits != 1
            or self.reserved_credit.provider_tasks != 1
            or self.reserved_credit.provider_result_bytes < 1
            or self.reserved_credit.materialization_items != 1
            or self.reserved_credit.compressed_bytes
            != self.reserved_credit.provider_result_bytes
            or self.reserved_credit.decoded_bytes < 1
            or self.reserved_credit.temp_disk_bytes < 1
            or self.reserved_credit.output_items != 1
            or self.reserved_credit.output_bytes < 1
            or self.reserved_credit.output_pages != self.source_page_count
            or self.reserved_credit.ack_items != 1
        ):
            raise ValueError("resource reservation does not bind whole-PDF facts")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(asdict(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


@dataclass(frozen=True, slots=True)
class ProviderEnvelopeContextV4:
    document_id: str
    processing_run_id: str
    provider: str
    provider_document_id: str
    source_pdf_relpath: str
    source_pdf_sha256: str
    source_page_count: int
    parser_artifact_root_relpath: str
    parser_target_identity: ParserTargetIdentity
    contract_version: str = PROVIDER_ENVELOPE_CONTEXT_V4_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != PROVIDER_ENVELOPE_CONTEXT_V4_CONTRACT:
            raise ValueError("provider envelope context contract is unsupported")
        _identity_tuple(
            self.document_id,
            self.processing_run_id,
            self.provider,
            self.provider_document_id,
        )
        _require_sha(self.source_pdf_sha256, "provider envelope source PDF")
        _positive(self.source_page_count, "provider envelope source page count")
        _relative_path(self.source_pdf_relpath, "provider envelope source PDF")
        _relative_path(
            self.parser_artifact_root_relpath,
            "provider envelope parser artifact root",
        )
        if type(self.parser_target_identity) is not ParserTargetIdentity:
            raise ValueError("provider envelope context lacks an exact parser target")
        source_parts = PurePosixPath(self.source_pdf_relpath).parts
        source_digest_name = "sha256_" + self.source_pdf_sha256.removeprefix(
            "sha256:"
        )
        if (
            len(source_parts) != 6
            or source_parts[0] != "raw_documents"
            or source_parts[1] != self.provider
            or source_parts[4] != self.provider_document_id
            or source_parts[5] != source_digest_name + ".pdf"
        ):
            raise ValueError("provider envelope source path identity drifted")
        parser_parts = PurePosixPath(self.parser_artifact_root_relpath).parts
        target_profile = (
            self.parser_target_identity.backend.split("-", 1)[0]
            + "_"
            + self.parser_target_identity.method
        )
        if (
            len(parser_parts) != 7
            or parser_parts[0] != "parser_artifacts"
            or parser_parts[1] != self.provider
            or parser_parts[2] != source_parts[2]
            or parser_parts[3] != self.provider_document_id
            or parser_parts[4] != self.processing_run_id
            or parser_parts[5] != source_digest_name
            or parser_parts[6] != target_profile
        ):
            raise ValueError("provider envelope parser artifact path identity drifted")

    @property
    def parser_target_sha256(self) -> str:
        return _digest(_canonical_json(self.parser_target_identity.to_payload()))

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(asdict(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


@dataclass(frozen=True, slots=True)
class MaterializationIntentV4:
    attempt_id: str
    fence_identity: str
    document_id: str
    processing_run_id: str
    source_checkpoint_sha256: str
    source_lifecycle_version: int
    source_pdf_sha256: str
    reservation_sha256: str
    terminal_receipt_sha256: str
    remote_task_identity: str
    artifact_owner_identity: str
    artifact_sha256: str
    artifact_byte_count: int
    source_page_count: int
    parser_target_sha256: str
    provider_envelope_context: ProviderEnvelopeContextV4
    allowance_sha256: str
    provider_capability_kind: str
    provider_capability_sha256: str
    provider_capability_byte_count: int
    held_resource_credit: ResourceCreditVector
    snapshot_relpath: str
    spool_relpath: str
    spool_part_relpath: str
    spool_part_owner_relpath: str
    spool_lock_relpath: str
    staging_relpath: str
    staging_marker_relpath: str
    staging_lock_relpath: str
    output_relpath: str
    output_dir_name: str
    provider_envelope_relpath: str
    output_manifest_relpath: str
    result_byte_limit: int
    member_count_limit: int
    uncompressed_byte_limit: int
    decoded_byte_limit: int
    temporary_disk_byte_limit: int
    output_byte_limit: int
    output_page_limit: int
    contract_version: str = MATERIALIZATION_INTENT_V4_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != MATERIALIZATION_INTENT_V4_CONTRACT:
            raise ValueError("materialization intent contract is unsupported")
        _identity_tuple(
            self.attempt_id,
            self.fence_identity,
            self.document_id,
            self.processing_run_id,
            self.remote_task_identity,
            self.artifact_owner_identity,
            self.output_dir_name,
            self.provider_capability_kind,
        )
        for value, label in (
            (self.source_checkpoint_sha256, "source checkpoint"),
            (self.source_pdf_sha256, "source PDF"),
            (self.reservation_sha256, "resource reservation"),
            (self.terminal_receipt_sha256, "terminal receipt"),
            (self.artifact_sha256, "retained artifact"),
            (self.parser_target_sha256, "parser target"),
            (self.allowance_sha256, "attempt allowance"),
            (self.provider_capability_sha256, "provider capability"),
        ):
            _require_sha(value, label)
        _nonnegative(self.source_lifecycle_version, "source lifecycle version")
        _positive(self.artifact_byte_count, "artifact byte count")
        _positive(self.source_page_count, "source page count")
        _positive(self.provider_capability_byte_count, "provider capability byte count")
        if type(self.held_resource_credit) is not ResourceCreditVector:
            raise ValueError("materialization intent lacks exact held credit")
        if type(self.provider_envelope_context) is not ProviderEnvelopeContextV4:
            raise ValueError("materialization intent lacks provider envelope context")
        context = self.provider_envelope_context
        if (
            context.document_id != self.document_id
            or context.processing_run_id != self.processing_run_id
            or context.source_pdf_sha256 != self.source_pdf_sha256
            or context.source_page_count != self.source_page_count
            or context.parser_target_sha256 != self.parser_target_sha256
        ):
            raise ValueError("provider envelope context drifted from intent")
        for path_value, label in (
            (self.snapshot_relpath, "snapshot"),
            (self.spool_relpath, "spool"),
            (self.spool_part_relpath, "spool part"),
            (self.spool_part_owner_relpath, "spool owner"),
            (self.spool_lock_relpath, "spool lock"),
            (self.staging_relpath, "staging"),
            (self.staging_marker_relpath, "staging marker"),
            (self.staging_lock_relpath, "staging lock"),
            (self.output_relpath, "output"),
            (self.provider_envelope_relpath, "provider envelope"),
            (self.output_manifest_relpath, "output manifest"),
        ):
            _relative_path(path_value, label)
        if self.staging_relpath == self.output_relpath:
            raise ValueError("staging and output paths must differ")
        if len(
            {
                self.provider_envelope_relpath,
                self.output_manifest_relpath,
                self.staging_marker_relpath,
            }
        ) != 3:
            raise ValueError("materialization metadata paths collide")
        if PurePosixPath(self.provider_envelope_relpath).name != PROVIDER_DOCUMENT_FILENAME:
            raise ValueError("provider envelope filename is not fixed")
        if (
            PurePosixPath(self.output_manifest_relpath).name
            != LOCAL_MATERIALIZATION_MANIFEST_V4_FILENAME
        ):
            raise ValueError("output manifest filename is not fixed")
        for limit_value, label in (
            (self.result_byte_limit, "result byte limit"),
            (self.member_count_limit, "member count limit"),
            (self.uncompressed_byte_limit, "uncompressed byte limit"),
            (self.decoded_byte_limit, "decoded byte limit"),
            (self.temporary_disk_byte_limit, "temporary disk byte limit"),
            (self.output_byte_limit, "output byte limit"),
            (self.output_page_limit, "output page limit"),
        ):
            _positive(limit_value, label)
        if self.artifact_byte_count > self.result_byte_limit:
            raise ValueError("retained artifact exceeds materialization byte limit")
        if self.source_page_count > self.output_page_limit:
            raise ValueError("source pages exceed materialization page limit")
        expected_paths = deterministic_local_resource_paths_v4(
            attempt_id=self.attempt_id,
            fence_identity=self.fence_identity,
            source_pdf_sha256=self.source_pdf_sha256,
            artifact_owner_identity=self.artifact_owner_identity,
            artifact_sha256=self.artifact_sha256,
            output_dir_name=self.output_dir_name,
        )
        if self.snapshot_relpath != expected_paths["snapshot"]:
            raise ValueError("materialization snapshot path is not deterministic")
        for name in (
            "spool",
            "spool_part",
            "spool_part_owner",
            "spool_lock",
            "staging",
            "staging_marker",
            "staging_lock",
            "output",
        ):
            if getattr(self, f"{name}_relpath") != expected_paths[name]:
                raise ValueError("materialization paths are not deterministic")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(asdict(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


@dataclass(frozen=True, slots=True)
class LocalOutputFileV4:
    relpath: str
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        _relative_path(self.relpath, "local output file")
        _require_sha(self.sha256, "local output file")
        _nonnegative(self.byte_count, "local output file byte count")


@dataclass(frozen=True, slots=True)
class LocalMaterializationReceiptV4:
    attempt_id: str
    fence_identity: str
    document_id: str
    processing_run_id: str
    materialization_intent_sha256: str
    terminal_receipt_sha256: str
    source_pdf_sha256: str
    source_page_count: int
    parser_target_sha256: str
    spool_relpath: str
    spool_sha256: str
    spool_byte_count: int
    member_count: int
    uncompressed_byte_count: int
    decoded_byte_count: int
    temporary_disk_peak_byte_count: int
    output_relpath: str
    output_files: tuple[LocalOutputFileV4, ...]
    output_file_count: int
    output_byte_count: int
    output_files_sha256: str
    provider_envelope_relpath: str
    provider_envelope_sha256: str
    provider_envelope_byte_count: int
    output_manifest_relpath: str
    output_manifest_sha256: str
    output_manifest_byte_count: int
    file_fsync_completed: bool
    output_parent_fsync_completed: bool
    marker_removed: bool
    spool_part_absent: bool
    spool_part_owner_absent: bool
    staging_absent: bool
    contract_version: str = LOCAL_MATERIALIZATION_V4_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != LOCAL_MATERIALIZATION_V4_CONTRACT:
            raise ValueError("local materialization contract is unsupported")
        _identity_tuple(
            self.attempt_id,
            self.fence_identity,
            self.document_id,
            self.processing_run_id,
        )
        for value, label in (
            (self.materialization_intent_sha256, "materialization intent"),
            (self.terminal_receipt_sha256, "terminal receipt"),
            (self.source_pdf_sha256, "source PDF"),
            (self.parser_target_sha256, "parser target"),
            (self.spool_sha256, "retained spool"),
            (self.output_files_sha256, "output files"),
            (self.provider_envelope_sha256, "provider envelope"),
            (self.output_manifest_sha256, "output manifest"),
        ):
            _require_sha(value, label)
        _positive(self.source_page_count, "source page count")
        _relative_path(self.spool_relpath, "retained spool")
        _relative_path(self.output_relpath, "output")
        _relative_path(self.provider_envelope_relpath, "provider envelope")
        _relative_path(self.output_manifest_relpath, "output manifest")
        _positive(self.spool_byte_count, "spool byte count")
        _positive(self.member_count, "member count")
        _positive(self.uncompressed_byte_count, "uncompressed byte count")
        _positive(self.decoded_byte_count, "decoded byte count")
        _positive(
            self.temporary_disk_peak_byte_count,
            "temporary disk peak byte count",
        )
        _positive(self.output_file_count, "output file count")
        _positive(self.output_byte_count, "output byte count")
        _positive(self.provider_envelope_byte_count, "provider envelope byte count")
        _positive(self.output_manifest_byte_count, "output manifest byte count")
        _exact_tuple(self.output_files, "output files")
        ordered = tuple(sorted(self.output_files, key=lambda item: item.relpath))
        if ordered != self.output_files:
            raise ValueError("output files are not canonically ordered")
        if len({item.relpath for item in self.output_files}) != len(self.output_files):
            raise ValueError("output files contain duplicate paths")
        expected_hash = local_output_files_sha256_v4(self.output_files)
        expected_bytes = _checked_sum(item.byte_count for item in self.output_files)
        if (
            self.output_file_count != len(self.output_files)
            or self.output_byte_count != expected_bytes
            or self.output_files_sha256 != expected_hash
        ):
            raise ValueError("output-file manifest does not close")
        by_path = {item.relpath: item for item in self.output_files}
        manifest = by_path.get(self.output_manifest_relpath)
        envelope = by_path.get(self.provider_envelope_relpath)
        if (
            manifest is None
            or manifest.sha256 != self.output_manifest_sha256
            or manifest.byte_count != self.output_manifest_byte_count
            or envelope is None
            or envelope.sha256 != self.provider_envelope_sha256
            or envelope.byte_count != self.provider_envelope_byte_count
        ):
            raise ValueError("output manifest or provider envelope is not closed")
        if not (
            self.file_fsync_completed is True
            and self.output_parent_fsync_completed is True
            and self.marker_removed is True
            and self.spool_part_absent is True
            and self.spool_part_owner_absent is True
            and self.staging_absent is True
        ):
            raise ValueError("local materialization lacks durable promotion evidence")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(_materialization_payload(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


@dataclass(frozen=True, slots=True)
class CleanupResourceEntryV4:
    kind: LocalResourceKind
    relpath: str
    ownership_basis_sha256: str
    expected_sha256: str | None
    expected_byte_count: int | None
    action: CleanupAction
    target_owner_identity: str | None = None
    target_relpath: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _RESOURCE_ORDER:
            raise ValueError("cleanup resource kind is unsupported")
        _relative_path(self.relpath, "cleanup resource")
        _require_sha(self.ownership_basis_sha256, "cleanup ownership basis")
        _optional_sha(self.expected_sha256, "cleanup resource")
        if (self.expected_sha256 is None) != (self.expected_byte_count is None):
            raise ValueError("cleanup exact identity is incomplete")
        if self.expected_byte_count is not None:
            _nonnegative(self.expected_byte_count, "cleanup resource byte count")
        if self.expected_sha256 is None and self.kind not in {
            "snapshot_part",
            "snapshot_part_owner",
            "spool_part",
            "spool_part_owner",
            "staging",
            "staging_marker",
        }:
            raise ValueError("cleanup exact identity is required for durable resources")
        if self.action not in {"delete", "transfer"}:
            raise ValueError("cleanup action is unsupported")
        if self.action == "delete":
            if self.target_owner_identity is not None or self.target_relpath is not None:
                raise ValueError("delete action cannot transfer ownership")
        else:
            if self.kind != "output":
                raise ValueError("only exact output may transfer ownership")
            if self.target_owner_identity is None or self.target_relpath is None:
                raise ValueError("transfer action lacks its target")
            _identity(self.target_owner_identity, "cleanup target owner")
            _relative_path(self.target_relpath, "cleanup target")


@dataclass(frozen=True, slots=True)
class LocalCleanupPlanV4:
    attempt_id: str
    fence_identity: str
    document_id: str
    processing_run_id: str
    outcome: CleanupOutcome
    source_state: str
    source_lifecycle_version: int
    source_checkpoint_sha256: str
    remote_task_identity: str | None
    terminal_receipt_sha256: str | None
    materialization_intent_sha256: str | None
    local_materialization_receipt_sha256: str | None
    publication_winner_sha256: str | None
    failure_receipt_sha256: str | None
    supersession_receipt_sha256: str | None
    resources: tuple[CleanupResourceEntryV4, ...]
    resource_count: int
    resources_sha256: str
    contract_version: str = CLEANUP_PLAN_V4_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != CLEANUP_PLAN_V4_CONTRACT:
            raise ValueError("cleanup plan contract is unsupported")
        _identity_tuple(
            self.attempt_id,
            self.fence_identity,
            self.document_id,
            self.processing_run_id,
        )
        if self.outcome not in {
            "success",
            "remote_failure",
            "local_failure",
            "pre_submission_failure",
            "superseded",
        }:
            raise ValueError("cleanup outcome is unsupported")
        if (
            self.source_state not in STAGED_RESOURCE_STATE_TRANSITIONS
            or self.source_state in {"cleanup_pending", "ack_pending"}
        ):
            raise ValueError("cleanup source state is not resourceful")
        _nonnegative(self.source_lifecycle_version, "cleanup source version")
        _require_sha(self.source_checkpoint_sha256, "cleanup source checkpoint")
        for value, label in (
            (self.terminal_receipt_sha256, "terminal receipt"),
            (self.materialization_intent_sha256, "materialization intent"),
            (self.local_materialization_receipt_sha256, "local receipt"),
            (self.publication_winner_sha256, "publication winner"),
            (self.failure_receipt_sha256, "failure receipt"),
            (self.supersession_receipt_sha256, "supersession receipt"),
        ):
            _optional_sha(value, label)
        if self.remote_task_identity is not None:
            _identity(self.remote_task_identity, "remote task")
        if self.outcome == "success" and (
            self.remote_task_identity is None
            or self.terminal_receipt_sha256 is None
            or self.materialization_intent_sha256 is None
            or self.local_materialization_receipt_sha256 is None
            or self.publication_winner_sha256 is None
        ):
            raise ValueError("successful cleanup lacks publication evidence")
        if self.outcome == "pre_submission_failure" and (
            self.remote_task_identity is not None
            or self.terminal_receipt_sha256 is not None
        ):
            raise ValueError("pre-submission cleanup cannot own a provider task")
        if self.outcome != "success" and self.publication_winner_sha256 is not None:
            raise ValueError("non-success cleanup cannot drain a committed publication")
        if self.outcome in {
            "pre_submission_failure",
            "remote_failure",
            "local_failure",
        } and self.failure_receipt_sha256 is None:
            raise ValueError("failure cleanup lacks exact failure evidence")
        if self.outcome == "remote_failure" and (
            self.remote_task_identity is None
            or self.terminal_receipt_sha256 is not None
            or self.materialization_intent_sha256 is not None
            or self.local_materialization_receipt_sha256 is not None
            or self.publication_winner_sha256 is not None
        ):
            raise ValueError("remote-failure cleanup evidence shape is invalid")
        if self.outcome == "local_failure" and (
            self.remote_task_identity is None
            or self.terminal_receipt_sha256 is None
            or self.publication_winner_sha256 is not None
        ):
            raise ValueError("local-failure cleanup evidence shape is invalid")
        if self.outcome == "superseded":
            if (
                self.supersession_receipt_sha256 is None
                or self.failure_receipt_sha256 is not None
                or self.publication_winner_sha256 is not None
            ):
                raise ValueError("superseded cleanup evidence shape is invalid")
        elif self.supersession_receipt_sha256 is not None:
            raise ValueError("non-superseded cleanup has supersession evidence")
        if self.outcome not in {
            "pre_submission_failure",
            "remote_failure",
            "local_failure",
        } and self.failure_receipt_sha256 is not None:
            raise ValueError("non-failure cleanup has failure evidence")
        _exact_tuple(self.resources, "cleanup resources")
        if not self.resources:
            raise ValueError("resourceful cleanup plan cannot be empty")
        ordered = tuple(sorted(self.resources, key=_cleanup_resource_sort_key))
        if ordered != self.resources:
            raise ValueError("cleanup resources are not canonically ordered")
        if len({item.relpath for item in self.resources}) != len(self.resources):
            raise ValueError("cleanup resources contain duplicate paths")
        if self.resource_count != len(self.resources) or self.resources_sha256 != _digest(
            _canonical_json(_resource_payloads(self.resources))
        ):
            raise ValueError("cleanup resource manifest does not close")

    @property
    def provider_ack_required(self) -> bool:
        return self.remote_task_identity is not None

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(_cleanup_plan_payload(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


@dataclass(frozen=True, slots=True)
class LocalCleanupResourceResultV4:
    kind: LocalResourceKind
    relpath: str
    disposition: CleanupDisposition
    target_owner_identity: str | None = None
    target_relpath: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _RESOURCE_ORDER:
            raise ValueError("cleanup result kind is unsupported")
        _relative_path(self.relpath, "cleanup result")
        if self.disposition not in {"absent", "transferred"}:
            raise ValueError("cleanup disposition is unsupported")
        if self.disposition == "absent":
            if self.target_owner_identity is not None or self.target_relpath is not None:
                raise ValueError("absent cleanup result cannot have a transfer target")
        else:
            if self.target_owner_identity is None or self.target_relpath is None:
                raise ValueError("transferred cleanup result lacks its target")
            _identity(self.target_owner_identity, "cleanup target owner")
            _relative_path(self.target_relpath, "cleanup target")


@dataclass(frozen=True, slots=True)
class LocalCleanupReceiptV4:
    attempt_id: str
    fence_identity: str
    document_id: str
    processing_run_id: str
    outcome: CleanupOutcome
    cleanup_plan_sha256: str
    cleanup_pending_checkpoint_sha256: str
    cleanup_pending_lifecycle_version: int
    results: tuple[LocalCleanupResourceResultV4, ...]
    result_count: int
    results_sha256: str
    parent_fsync_completed: bool
    contract_version: str = CLEANUP_RECEIPT_V4_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != CLEANUP_RECEIPT_V4_CONTRACT:
            raise ValueError("cleanup receipt contract is unsupported")
        _identity_tuple(
            self.attempt_id,
            self.fence_identity,
            self.document_id,
            self.processing_run_id,
        )
        if self.outcome not in {
            "success",
            "remote_failure",
            "local_failure",
            "pre_submission_failure",
            "superseded",
        }:
            raise ValueError("cleanup receipt outcome is unsupported")
        _require_sha(self.cleanup_plan_sha256, "cleanup plan")
        _require_sha(
            self.cleanup_pending_checkpoint_sha256, "cleanup-pending checkpoint"
        )
        _nonnegative(
            self.cleanup_pending_lifecycle_version,
            "cleanup-pending lifecycle version",
        )
        _exact_tuple(self.results, "cleanup results")
        ordered = tuple(sorted(self.results, key=_cleanup_result_sort_key))
        if ordered != self.results:
            raise ValueError("cleanup results are not canonically ordered")
        if len({item.relpath for item in self.results}) != len(self.results):
            raise ValueError("cleanup results contain duplicate paths")
        if self.result_count != len(self.results) or self.results_sha256 != _digest(
            _canonical_json(_cleanup_result_payloads(self.results))
        ):
            raise ValueError("cleanup result manifest does not close")
        if self.parent_fsync_completed is not True:
            raise ValueError("cleanup receipt lacks parent-directory fsync")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(_cleanup_receipt_payload(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


@dataclass(frozen=True, slots=True)
class ProviderAckReceiptV4:
    attempt_id: str
    fence_identity: str
    document_id: str
    processing_run_id: str
    outcome: CleanupOutcome
    ack_pending_checkpoint_sha256: str
    ack_pending_lifecycle_version: int
    accepted_submission_sha256: str
    remote_task_identity: str
    result_owner_identity: str | None
    terminal_receipt_sha256: str | None
    failure_receipt_sha256: str | None
    supersession_receipt_sha256: str | None
    local_materialization_receipt_sha256: str | None
    publication_winner_sha256: str | None
    cleanup_plan_sha256: str
    cleanup_receipt_sha256: str
    provider_protocol_version: str
    request_identity: str
    ack_request_sha256: str
    ack_kind: ProviderAckKind
    http_status: int
    provider_response_sha256: str
    provider_response_byte_count: int
    provider_receipt_identity: str | None
    contract_version: str = ACK_RECEIPT_V4_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != ACK_RECEIPT_V4_CONTRACT:
            raise ValueError("provider ACK receipt contract is unsupported")
        _identity_tuple(
            self.attempt_id,
            self.fence_identity,
            self.document_id,
            self.processing_run_id,
            self.remote_task_identity,
            self.provider_protocol_version,
            self.request_identity,
        )
        if self.outcome not in {
            "success",
            "remote_failure",
            "local_failure",
            "superseded",
        }:
            raise ValueError("provider ACK outcome is unsupported")
        for value, label in (
            (self.ack_pending_checkpoint_sha256, "ack-pending checkpoint"),
            (self.accepted_submission_sha256, "accepted submission"),
            (self.terminal_receipt_sha256, "terminal receipt"),
            (self.failure_receipt_sha256, "failure receipt"),
            (self.supersession_receipt_sha256, "supersession receipt"),
            (self.local_materialization_receipt_sha256, "local receipt"),
            (self.publication_winner_sha256, "publication winner"),
            (self.cleanup_plan_sha256, "cleanup plan"),
            (self.cleanup_receipt_sha256, "cleanup receipt"),
            (self.ack_request_sha256, "ACK request"),
            (self.provider_response_sha256, "provider response"),
        ):
            if value is None:
                if label in {"cleanup plan", "cleanup receipt", "ack-pending checkpoint"}:
                    raise ValueError(f"{label} hash is required")
            else:
                _require_sha(value, label)
        _nonnegative(self.ack_pending_lifecycle_version, "ack-pending version")
        _nonnegative(
            self.provider_response_byte_count,
            "provider response byte count",
        )
        if self.result_owner_identity is not None:
            _identity(self.result_owner_identity, "result owner")
        if self.outcome == "success" and (
            self.result_owner_identity is None
            or self.terminal_receipt_sha256 is None
            or self.local_materialization_receipt_sha256 is None
            or self.publication_winner_sha256 is None
            or self.failure_receipt_sha256 is not None
            or self.supersession_receipt_sha256 is not None
        ):
            raise ValueError("successful ACK lacks exact publication evidence")
        if self.outcome == "remote_failure" and (
            self.failure_receipt_sha256 is None
            or self.result_owner_identity is not None
            or self.terminal_receipt_sha256 is not None
            or self.local_materialization_receipt_sha256 is not None
            or self.publication_winner_sha256 is not None
            or self.supersession_receipt_sha256 is not None
        ):
            raise ValueError("remote-failure ACK evidence shape is invalid")
        if self.outcome == "local_failure" and (
            self.failure_receipt_sha256 is None
            or self.terminal_receipt_sha256 is None
            or self.result_owner_identity is None
            or self.publication_winner_sha256 is not None
            or self.supersession_receipt_sha256 is not None
        ):
            raise ValueError("local-failure ACK evidence shape is invalid")
        if self.outcome == "superseded" and (
            self.supersession_receipt_sha256 is None
            or self.failure_receipt_sha256 is not None
            or self.publication_winner_sha256 is not None
            or (
                (self.result_owner_identity is None)
                != (self.terminal_receipt_sha256 is None)
            )
        ):
            raise ValueError("superseded ACK evidence shape is invalid")
        if self.ack_kind == "consumed":
            if self.http_status not in {200, 204}:
                raise ValueError("consumed ACK requires HTTP 200 or 204")
            if self.http_status == 200 and self.provider_receipt_identity is None:
                raise ValueError("HTTP 200 ACK lacks provider receipt identity")
            if self.provider_receipt_identity is not None:
                _identity(self.provider_receipt_identity, "provider ACK receipt")
        elif self.ack_kind == "absent":
            if self.http_status != 404 or self.provider_receipt_identity is not None:
                raise ValueError("absence ACK requires exact HTTP 404")
        else:
            raise ValueError("provider ACK kind is unsupported")
        expected_request_bytes = provider_ack_request_v4_bytes(
            accepted_submission_sha256=self.accepted_submission_sha256,
            ack_pending_checkpoint_sha256=self.ack_pending_checkpoint_sha256,
            attempt_id=self.attempt_id,
            cleanup_plan_sha256=self.cleanup_plan_sha256,
            cleanup_receipt_sha256=self.cleanup_receipt_sha256,
            document_id=self.document_id,
            fence_identity=self.fence_identity,
            outcome=self.outcome,
            processing_run_id=self.processing_run_id,
            provider_protocol_version=self.provider_protocol_version,
            remote_task_identity=self.remote_task_identity,
            result_owner_identity=self.result_owner_identity,
            terminal_receipt_sha256=self.terminal_receipt_sha256,
        )
        expected_request_sha256 = _digest(expected_request_bytes)
        if (
            self.ack_request_sha256 != expected_request_sha256
            or self.request_identity
            != provider_ack_request_v4_identity(expected_request_sha256)
        ):
            raise ValueError("provider ACK request identity does not close")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(asdict(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


def provider_ack_request_v4_bytes(
    *,
    accepted_submission_sha256: str,
    ack_pending_checkpoint_sha256: str,
    attempt_id: str,
    cleanup_plan_sha256: str,
    cleanup_receipt_sha256: str,
    document_id: str,
    fence_identity: str,
    outcome: CleanupOutcome,
    processing_run_id: str,
    provider_protocol_version: str,
    remote_task_identity: str,
    result_owner_identity: str | None,
    terminal_receipt_sha256: str | None,
) -> bytes:
    """Return the sole pure provider-ACK request projection used by all layers."""

    for value, label in (
        (accepted_submission_sha256, "accepted submission"),
        (ack_pending_checkpoint_sha256, "ack-pending checkpoint"),
        (cleanup_plan_sha256, "cleanup plan"),
        (cleanup_receipt_sha256, "cleanup receipt"),
    ):
        _require_sha(value, label)
    _optional_sha(terminal_receipt_sha256, "terminal receipt")
    _identity_tuple(
        attempt_id,
        fence_identity,
        document_id,
        processing_run_id,
        provider_protocol_version,
        remote_task_identity,
    )
    if result_owner_identity is not None:
        _identity(result_owner_identity, "result owner")
    if outcome not in {
        "success",
        "remote_failure",
        "local_failure",
        "superseded",
    }:
        raise ValueError("provider ACK request outcome is unsupported")
    return _canonical_json(
        {
            "accepted_submission_sha256": accepted_submission_sha256,
            "ack_pending_checkpoint_sha256": ack_pending_checkpoint_sha256,
            "attempt_id": attempt_id,
            "cleanup_plan_sha256": cleanup_plan_sha256,
            "cleanup_receipt_sha256": cleanup_receipt_sha256,
            "document_id": document_id,
            "fence_identity": fence_identity,
            "outcome": outcome,
            "processing_run_id": processing_run_id,
            "provider_protocol_version": provider_protocol_version,
            "remote_task_identity": remote_task_identity,
            "result_owner_identity": result_owner_identity,
            "schema": "provider-ack-request.v4",
            "terminal_receipt_sha256": terminal_receipt_sha256,
        }
    )


def provider_ack_request_v4_identity(request_sha256: str) -> str:
    _require_sha(request_sha256, "provider ACK request")
    return "provider-ack-v4." + request_sha256.removeprefix("sha256:")


@dataclass(frozen=True, slots=True)
class RemoteParseCheckpointV4:
    attempt_id: str
    attempt_generation: int
    fence_identity: str
    document_id: str
    processing_run_id: str
    state: CheckpointStateV4
    lifecycle_version: int
    previous_checkpoint_sha256: str | None
    source_pdf_sha256: str
    source_byte_count: int
    source_page_count: int
    request_sha256: str
    runtime_epoch_sha256: str
    process_profile_sha256: str
    credit_policy_sha256: str
    reservation_input_sha256: str
    held_resource_credit: ResourceCreditVector
    preparation_intent_sha256: str | None = None
    snapshot_receipt_sha256: str | None = None
    submission_intent_sha256: str | None = None
    accepted_submission_sha256: str | None = None
    terminal_receipt_sha256: str | None = None
    materialization_intent_sha256: str | None = None
    local_materialization_receipt_sha256: str | None = None
    publication_winner_sha256: str | None = None
    failure_receipt_sha256: str | None = None
    supersession_receipt_sha256: str | None = None
    cleanup_plan_sha256: str | None = None
    cleanup_receipt_sha256: str | None = None
    ack_receipt_sha256: str | None = None
    contract_version: str = CHECKPOINT_V4_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != CHECKPOINT_V4_CONTRACT:
            raise ValueError("checkpoint-v4 contract is unsupported")
        _identity_tuple(
            self.attempt_id,
            self.fence_identity,
            self.document_id,
            self.processing_run_id,
        )
        if self.state not in _ALL_STATES:
            raise ValueError("checkpoint-v4 state is unsupported")
        _nonnegative(self.lifecycle_version, "lifecycle version")
        _positive(self.attempt_generation, "attempt generation")
        _optional_sha(self.previous_checkpoint_sha256, "previous checkpoint")
        for value, label in (
            (self.source_pdf_sha256, "source PDF"),
            (self.request_sha256, "request"),
            (self.runtime_epoch_sha256, "runtime epoch"),
            (self.process_profile_sha256, "process profile"),
            (self.credit_policy_sha256, "credit policy"),
            (self.reservation_input_sha256, "reservation input"),
        ):
            _require_sha(value, label)
        _positive(self.source_byte_count, "source byte count")
        _positive(self.source_page_count, "source page count")
        if type(self.held_resource_credit) is not ResourceCreditVector:
            raise ValueError("checkpoint requires an exact held-resource credit")
        for name in _EVIDENCE_FIELDS:
            _optional_sha(getattr(self, name), name)
        self._validate_state_evidence()
        _validate_checkpoint_evidence_frontier_v4(self)
        _validate_checkpoint_credit_shape_v4(self)

    def _validate_state_evidence(self) -> None:
        if (
            self.failure_receipt_sha256 is not None
            and self.supersession_receipt_sha256 is not None
        ):
            raise ValueError("checkpoint cannot mix failure and supersession evidence")
        if (
            self.publication_winner_sha256 is not None
            and (
                self.failure_receipt_sha256 is not None
                or self.supersession_receipt_sha256 is not None
            )
        ):
            raise ValueError("failed or superseded checkpoint cannot retain a winner")
        if self.state in {"preparation_failed", "superseded"} and self.lifecycle_version == 0:
            if self.previous_checkpoint_sha256 is not None:
                raise ValueError("resource-free final cannot have a predecessor")
            if self.held_resource_credit != ResourceCreditVector():
                raise ValueError("resource-free final retains resource credit")
            if self.state == "preparation_failed" and self.failure_receipt_sha256 is None:
                raise ValueError("resource-free preparation failure lacks exact evidence")
            if self.state == "superseded" and self.supersession_receipt_sha256 is None:
                raise ValueError("resource-free supersession lacks exact evidence")
            return
        if self.lifecycle_version > 0 and self.state in {
            "prepared",
            "preparation_failed",
        }:
            raise ValueError(
                "prepared and preparation_failed are lifecycle-zero states"
            )
        if self.lifecycle_version == 0:
            if self.state != "prepared" or self.previous_checkpoint_sha256 is not None:
                raise ValueError("only prepared may start a resourceful lifecycle")
        elif self.previous_checkpoint_sha256 is None:
            raise ValueError("evolved checkpoint lacks its predecessor")
        if self.preparation_intent_sha256 is None:
            raise ValueError("resourceful checkpoint lacks preparation evidence")
        required_by_state: dict[str, tuple[str, ...]] = {
            "reconciling": (
                "snapshot_receipt_sha256",
                "submission_intent_sha256",
            ),
            "submitted": (
                "snapshot_receipt_sha256",
                "submission_intent_sha256",
                "accepted_submission_sha256",
            ),
            "remote_terminal": (
                "snapshot_receipt_sha256",
                "submission_intent_sha256",
                "accepted_submission_sha256",
                "terminal_receipt_sha256",
            ),
            "materializing": (
                "snapshot_receipt_sha256",
                "accepted_submission_sha256",
                "terminal_receipt_sha256",
                "materialization_intent_sha256",
            ),
            "local_materialized": (
                "snapshot_receipt_sha256",
                "materialization_intent_sha256",
                "local_materialization_receipt_sha256",
            ),
            "publish_committed": (
                "snapshot_receipt_sha256",
                "local_materialization_receipt_sha256",
                "publication_winner_sha256",
            ),
            "cleanup_pending": ("cleanup_plan_sha256",),
            "ack_pending": ("cleanup_plan_sha256", "cleanup_receipt_sha256"),
            "acked": (
                "publication_winner_sha256",
                "cleanup_receipt_sha256",
                "ack_receipt_sha256",
            ),
            "remote_failed": (
                "failure_receipt_sha256",
                "cleanup_receipt_sha256",
                "ack_receipt_sha256",
            ),
            "local_failed": (
                "failure_receipt_sha256",
                "cleanup_receipt_sha256",
                "ack_receipt_sha256",
            ),
            "pre_submission_failed": (
                "failure_receipt_sha256",
                "cleanup_receipt_sha256",
            ),
            "superseded": (
                "supersession_receipt_sha256",
                "cleanup_receipt_sha256",
            ),
        }
        for field_name in required_by_state.get(self.state, ()):
            if getattr(self, field_name) is None:
                raise ValueError(
                    f"{self.state} checkpoint lacks {field_name.removesuffix('_sha256')}"
                )
        if self.state == "ack_pending" and self.accepted_submission_sha256 is None:
            raise ValueError("ack_pending requires accepted provider-task evidence")
        if self.state in {"cleanup_pending", "ack_pending"} and sum(
            getattr(self, field_name) is not None
            for field_name in _OUTCOME_EVIDENCE_FIELDS
        ) != 1:
            raise ValueError(
                f"{self.state} checkpoint requires exactly one outcome evidence"
            )
        if self.state in _FINAL_STATES and self.held_resource_credit != ResourceCreditVector():
            raise ValueError("final checkpoint retains resource credit")
        if self.state == "pre_submission_failed" and (
            self.accepted_submission_sha256 is not None
            or self.ack_receipt_sha256 is not None
        ):
            raise ValueError(
                "pre-submission failure cannot have accepted-task or ACK evidence"
            )
        if (
            self.state == "superseded"
            and (self.accepted_submission_sha256 is None)
            != (self.ack_receipt_sha256 is None)
        ):
            raise ValueError(
                "resourceful supersession accepted-task and ACK evidence disagree"
            )

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(_checkpoint_payload(self))

    @property
    def sha256(self) -> str:
        return _digest(self.canonical_bytes)


def build_resource_reservation_v4(
    *,
    attempt_id: str,
    attempt_generation: int,
    fence_identity: str,
    document_id: str,
    processing_run_id: str,
    source_pdf_sha256: str,
    source_byte_count: int,
    source_page_count: int,
    prepared_submission_identity_sha256: str,
    request_sha256: str,
    runtime_epoch_sha256: str,
    process_profile_sha256: str,
    credit_policy_sha256: str,
    reservation_bucket: ResourceCreditBucket,
    reservation_input_sha256: str,
    reserved_credit: ResourceCreditVector,
) -> ResourceReservationV4:
    paths = deterministic_local_resource_paths_v4(
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        source_pdf_sha256=source_pdf_sha256,
        artifact_owner_identity=None,
        artifact_sha256=None,
        output_dir_name=None,
    )
    return ResourceReservationV4(
        attempt_id=attempt_id,
        attempt_generation=attempt_generation,
        fence_identity=fence_identity,
        document_id=document_id,
        processing_run_id=processing_run_id,
        source_pdf_sha256=source_pdf_sha256,
        source_byte_count=source_byte_count,
        source_page_count=source_page_count,
        prepared_submission_identity_sha256=prepared_submission_identity_sha256,
        request_sha256=request_sha256,
        runtime_epoch_sha256=runtime_epoch_sha256,
        process_profile_sha256=process_profile_sha256,
        credit_policy_sha256=credit_policy_sha256,
        reservation_bucket=reservation_bucket,
        reservation_input_sha256=reservation_input_sha256,
        snapshot_relpath=paths["snapshot"],
        snapshot_part_relpath=paths["snapshot_part"],
        snapshot_part_owner_relpath=paths["snapshot_part_owner"],
        snapshot_lock_relpath=paths["snapshot_lock"],
        reserved_credit=reserved_credit,
    )


def build_materialization_intent_v4(
    *,
    reservation: ResourceReservationV4,
    source_checkpoint: RemoteParseCheckpointV4,
    terminal_receipt_sha256: str,
    remote_task_identity: str,
    artifact_owner_identity: str,
    artifact_sha256: str,
    artifact_byte_count: int,
    provider_envelope_context: ProviderEnvelopeContextV4,
    allowance_sha256: str,
    provider_capability_kind: str,
    provider_capability_sha256: str,
    provider_capability_byte_count: int,
    output_dir_name: str,
    provider_envelope_relpath: str,
    output_manifest_relpath: str,
    member_count_limit: int,
    uncompressed_byte_limit: int,
) -> MaterializationIntentV4:
    if (
        source_checkpoint.state != "remote_terminal"
        or source_checkpoint.terminal_receipt_sha256 != terminal_receipt_sha256
    ):
        raise ValueError("materialization intent lacks remote-terminal authority")
    validate_resource_reservation_checkpoint_binding_v4(
        reservation=reservation,
        checkpoint=source_checkpoint,
    )
    paths = deterministic_local_resource_paths_v4(
        attempt_id=reservation.attempt_id,
        fence_identity=reservation.fence_identity,
        source_pdf_sha256=reservation.source_pdf_sha256,
        artifact_owner_identity=artifact_owner_identity,
        artifact_sha256=artifact_sha256,
        output_dir_name=output_dir_name,
    )
    return MaterializationIntentV4(
        attempt_id=reservation.attempt_id,
        fence_identity=reservation.fence_identity,
        document_id=reservation.document_id,
        processing_run_id=reservation.processing_run_id,
        source_checkpoint_sha256=source_checkpoint.sha256,
        source_lifecycle_version=source_checkpoint.lifecycle_version,
        source_pdf_sha256=reservation.source_pdf_sha256,
        reservation_sha256=reservation.sha256,
        terminal_receipt_sha256=terminal_receipt_sha256,
        remote_task_identity=remote_task_identity,
        artifact_owner_identity=artifact_owner_identity,
        artifact_sha256=artifact_sha256,
        artifact_byte_count=artifact_byte_count,
        source_page_count=reservation.source_page_count,
        parser_target_sha256=provider_envelope_context.parser_target_sha256,
        provider_envelope_context=provider_envelope_context,
        allowance_sha256=allowance_sha256,
        provider_capability_kind=provider_capability_kind,
        provider_capability_sha256=provider_capability_sha256,
        provider_capability_byte_count=provider_capability_byte_count,
        held_resource_credit=ResourceCreditVector(
            documents=1,
            snapshot_items=1,
            snapshot_bytes=reservation.source_byte_count,
            provider_tasks=1,
            provider_result_bytes=artifact_byte_count,
            materialization_items=1,
            compressed_bytes=artifact_byte_count,
            decoded_bytes=reservation.reserved_credit.decoded_bytes,
            temp_disk_bytes=reservation.reserved_credit.temp_disk_bytes,
            ack_items=1,
        ),
        snapshot_relpath=paths["snapshot"],
        spool_relpath=paths["spool"],
        spool_part_relpath=paths["spool_part"],
        spool_part_owner_relpath=paths["spool_part_owner"],
        spool_lock_relpath=paths["spool_lock"],
        staging_relpath=paths["staging"],
        staging_marker_relpath=paths["staging_marker"],
        staging_lock_relpath=paths["staging_lock"],
        output_relpath=paths["output"],
        output_dir_name=output_dir_name,
        provider_envelope_relpath=provider_envelope_relpath,
        output_manifest_relpath=output_manifest_relpath,
        result_byte_limit=reservation.reserved_credit.provider_result_bytes,
        member_count_limit=member_count_limit,
        uncompressed_byte_limit=uncompressed_byte_limit,
        decoded_byte_limit=reservation.reserved_credit.decoded_bytes,
        temporary_disk_byte_limit=reservation.reserved_credit.temp_disk_bytes,
        output_byte_limit=reservation.reserved_credit.output_bytes,
        output_page_limit=reservation.source_page_count,
    )


def build_local_materialization_receipt_v4(
    *,
    intent: MaterializationIntentV4,
    manifest: LocalMaterializationManifestV4,
    source_page_count: int,
    output_files: tuple[LocalOutputFileV4, ...],
    provider_envelope_relpath: str,
    output_manifest_relpath: str,
    member_count: int,
    uncompressed_byte_count: int,
    decoded_byte_count: int,
    temporary_disk_peak_byte_count: int,
    file_fsync_completed: bool,
    output_parent_fsync_completed: bool,
    marker_removed: bool,
    spool_part_absent: bool,
    spool_part_owner_absent: bool,
    staging_absent: bool,
) -> LocalMaterializationReceiptV4:
    if type(manifest) is not LocalMaterializationManifestV4:
        raise ValueError("materialization receipt lacks exact local manifest")
    ordered = tuple(sorted(output_files, key=lambda item: item.relpath))
    by_path = {item.relpath: item for item in ordered}
    envelope = by_path.get(provider_envelope_relpath)
    manifest_file = by_path.get(output_manifest_relpath)
    if envelope is None or manifest_file is None:
        raise ValueError("materialization output lacks manifest or envelope")
    if (
        provider_envelope_relpath != intent.provider_envelope_relpath
        or output_manifest_relpath != intent.output_manifest_relpath
    ):
        raise ValueError("materialization output identity drifted from intent")
    context = intent.provider_envelope_context
    if (
        manifest.attempt_id != intent.attempt_id
        or manifest.fence_identity != intent.fence_identity
        or manifest.document_id != intent.document_id
        or manifest.processing_run_id != intent.processing_run_id
        or manifest.materialization_intent_sha256 != intent.sha256
        or manifest.terminal_receipt_sha256 != intent.terminal_receipt_sha256
        or manifest.remote_task_identity != intent.remote_task_identity
        or manifest.artifact_owner_identity != intent.artifact_owner_identity
        or manifest.artifact_sha256 != intent.artifact_sha256
        or manifest.artifact_byte_count != intent.artifact_byte_count
        or manifest.source_pdf_sha256 != intent.source_pdf_sha256
        or manifest.source_page_count != intent.source_page_count
        or manifest.parser_target_sha256 != intent.parser_target_sha256
        or manifest.spool_relpath != intent.spool_relpath
        or manifest.output_relpath != intent.output_relpath
        or manifest.provider_envelope_relpath != intent.provider_envelope_relpath
        or context.document_id != manifest.document_id
        or context.processing_run_id != manifest.processing_run_id
        or context.source_pdf_sha256 != manifest.source_pdf_sha256
        or context.source_page_count != manifest.source_page_count
        or context.parser_target_sha256 != manifest.parser_target_sha256
    ):
        raise ValueError("local materialization manifest drifted from intent")
    if (
        manifest_file.sha256 != manifest.sha256
        or manifest_file.byte_count != len(manifest.canonical_bytes)
    ):
        raise ValueError("local materialization manifest bytes do not close")
    payload_files = {
        (item.relpath, item.sha256, item.byte_count)
        for item in ordered
        if item.relpath != output_manifest_relpath
    }
    manifest_payload_files = {
        (item.relpath, item.sha256, item.byte_count) for item in manifest.payload_files
    }
    if payload_files != manifest_payload_files:
        raise ValueError("local materialization payload files drifted from manifest")
    observations = manifest.observations
    if (
        manifest.provider_envelope_relpath != provider_envelope_relpath
        or manifest.provider_envelope_sha256 != envelope.sha256
        or manifest.provider_envelope_byte_count != envelope.byte_count
        or observations.member_count != member_count
        or observations.uncompressed_byte_count != uncompressed_byte_count
        or observations.decoded_byte_count != decoded_byte_count
        or observations.temporary_disk_peak_byte_count
        != temporary_disk_peak_byte_count
    ):
        raise ValueError("local materialization observations drifted from manifest")
    output_byte_count = _checked_sum(item.byte_count for item in ordered)
    if (
        source_page_count != intent.source_page_count
        or member_count > intent.member_count_limit
        or uncompressed_byte_count > intent.uncompressed_byte_limit
        or decoded_byte_count > intent.decoded_byte_limit
        or temporary_disk_peak_byte_count > intent.temporary_disk_byte_limit
        or output_byte_count > intent.output_byte_limit
    ):
        raise ValueError("materialization observation exceeds or drifts from intent")
    return LocalMaterializationReceiptV4(
        attempt_id=intent.attempt_id,
        fence_identity=intent.fence_identity,
        document_id=intent.document_id,
        processing_run_id=intent.processing_run_id,
        materialization_intent_sha256=intent.sha256,
        terminal_receipt_sha256=intent.terminal_receipt_sha256,
        source_pdf_sha256=intent.source_pdf_sha256,
        source_page_count=source_page_count,
        parser_target_sha256=intent.parser_target_sha256,
        spool_relpath=intent.spool_relpath,
        spool_sha256=intent.artifact_sha256,
        spool_byte_count=intent.artifact_byte_count,
        member_count=member_count,
        uncompressed_byte_count=uncompressed_byte_count,
        decoded_byte_count=decoded_byte_count,
        temporary_disk_peak_byte_count=temporary_disk_peak_byte_count,
        output_relpath=intent.output_relpath,
        output_files=ordered,
        output_file_count=len(ordered),
        output_byte_count=output_byte_count,
        output_files_sha256=local_output_files_sha256_v4(ordered),
        provider_envelope_relpath=provider_envelope_relpath,
        provider_envelope_sha256=envelope.sha256,
        provider_envelope_byte_count=envelope.byte_count,
        output_manifest_relpath=output_manifest_relpath,
        output_manifest_sha256=manifest_file.sha256,
        output_manifest_byte_count=manifest_file.byte_count,
        file_fsync_completed=file_fsync_completed,
        output_parent_fsync_completed=output_parent_fsync_completed,
        marker_removed=marker_removed,
        spool_part_absent=spool_part_absent,
        spool_part_owner_absent=spool_part_owner_absent,
        staging_absent=staging_absent,
    )


def validate_materialized_provider_evidence_v4(
    *,
    intent: MaterializationIntentV4,
    receipt: LocalMaterializationReceiptV4,
    manifest: LocalMaterializationManifestV4,
    provider_envelope: ProviderDocumentEnvelope,
) -> None:
    """Close one materialized provider document over its exact canonical bytes."""

    if (
        type(intent) is not MaterializationIntentV4
        or type(receipt) is not LocalMaterializationReceiptV4
        or type(manifest) is not LocalMaterializationManifestV4
        or type(provider_envelope) is not ProviderDocumentEnvelope
    ):
        raise ValueError("materialized provider evidence type is invalid")
    context = intent.provider_envelope_context
    if (
        provider_envelope.document_id != context.document_id
        or provider_envelope.artifact_owner_processing_run_id
        != context.processing_run_id
        or provider_envelope.provider != context.provider
        or provider_envelope.provider_document_id
        != context.provider_document_id
        or provider_envelope.source_pdf_relpath != context.source_pdf_relpath
        or provider_envelope.input_raw_file_hash != context.source_pdf_sha256
        or provider_envelope.source_pdf_page_count != context.source_page_count
        or provider_envelope.parser_artifact_root_relpath
        != context.parser_artifact_root_relpath
        or provider_envelope.parser_target_identity
        != context.parser_target_identity
    ):
        raise ValueError("provider envelope drifted from materialization context")
    provider_envelope_bytes = provider_document_envelope_to_bytes(provider_envelope)
    provider_envelope_sha256 = _digest(provider_envelope_bytes)
    parser_artifacts = {
        (artifact.relative_path, artifact.sha256, artifact.size_bytes)
        for artifact in provider_envelope.provider_document.artifacts
    }
    manifest_parser_artifacts = {
        (item.relpath, item.sha256, item.byte_count)
        for item in manifest.payload_files
        if item.role == "parser_artifact"
    }
    if (
        manifest.provider_envelope_sha256 != provider_envelope_sha256
        or manifest.provider_envelope_byte_count != len(provider_envelope_bytes)
        or parser_artifacts != manifest_parser_artifacts
    ):
        raise ValueError("provider envelope bytes or artifact inventory drifted")
    expected = build_local_materialization_receipt_v4(
        intent=intent,
        manifest=manifest,
        source_page_count=receipt.source_page_count,
        output_files=receipt.output_files,
        provider_envelope_relpath=receipt.provider_envelope_relpath,
        output_manifest_relpath=receipt.output_manifest_relpath,
        member_count=receipt.member_count,
        uncompressed_byte_count=receipt.uncompressed_byte_count,
        decoded_byte_count=receipt.decoded_byte_count,
        temporary_disk_peak_byte_count=receipt.temporary_disk_peak_byte_count,
        file_fsync_completed=receipt.file_fsync_completed,
        output_parent_fsync_completed=receipt.output_parent_fsync_completed,
        marker_removed=receipt.marker_removed,
        spool_part_absent=receipt.spool_part_absent,
        spool_part_owner_absent=receipt.spool_part_owner_absent,
        staging_absent=receipt.staging_absent,
    )
    if receipt != expected:
        raise ValueError("materialized provider receipt drifted from exact evidence")


def build_local_cleanup_plan_v4(
    *,
    reservation: ResourceReservationV4,
    source_checkpoint: RemoteParseCheckpointV4,
    outcome: CleanupOutcome,
    resources: tuple[CleanupResourceEntryV4, ...],
    materialization_intent: MaterializationIntentV4 | None = None,
    local_materialization_receipt: LocalMaterializationReceiptV4 | None = None,
    remote_task_identity: str | None = None,
    failure_receipt_sha256: str | None = None,
    supersession_receipt_sha256: str | None = None,
) -> LocalCleanupPlanV4:
    if (
        source_checkpoint.state not in STAGED_RESOURCE_STATE_TRANSITIONS
        or source_checkpoint.state in {"cleanup_pending", "ack_pending"}
    ):
        raise ValueError("cleanup plan requires a resourceful source checkpoint")
    if not resources:
        raise ValueError("resourceful cleanup plan cannot be empty")
    validate_resource_reservation_checkpoint_binding_v4(
        reservation=reservation,
        checkpoint=source_checkpoint,
    )
    published_output_relpath = (
        materialization_intent.provider_envelope_context.parser_artifact_root_relpath
        if outcome == "success" and materialization_intent is not None
        else None
    )
    if outcome == "success" and published_output_relpath is None:
        raise ValueError("successful cleanup lacks materialization target evidence")
    if outcome != "success" and source_checkpoint.publication_winner_sha256 is not None:
        raise ValueError("non-success cleanup cannot drain a committed publication")
    if (
        source_checkpoint.materialization_intent_sha256
        != (
            None if materialization_intent is None else materialization_intent.sha256
        )
        or source_checkpoint.local_materialization_receipt_sha256
        != (
            None
            if local_materialization_receipt is None
            else local_materialization_receipt.sha256
        )
    ):
        raise ValueError("cleanup inputs drifted from source checkpoint")
    allowed: dict[
        tuple[str, str], tuple[str, str | None, int | None]
    ] = {
        ("snapshot", reservation.snapshot_relpath): (
            reservation.sha256,
            reservation.source_pdf_sha256,
            reservation.source_byte_count,
        ),
        ("snapshot_part", reservation.snapshot_part_relpath): (
            reservation.sha256,
            None,
            None,
        ),
        ("snapshot_part_owner", reservation.snapshot_part_owner_relpath): (
            reservation.sha256,
            None,
            None,
        ),
    }
    mandatory = {("snapshot", reservation.snapshot_relpath)}
    if source_checkpoint.snapshot_receipt_sha256 is None:
        mandatory.update(
            {
                ("snapshot_part", reservation.snapshot_part_relpath),
                ("snapshot_part_owner", reservation.snapshot_part_owner_relpath),
            }
        )
    if materialization_intent is not None:
        _same_identity(materialization_intent, source_checkpoint)
        partials = {
            ("spool_part", materialization_intent.spool_part_relpath): (
                materialization_intent.sha256,
                None,
                None,
            ),
            ("spool_part_owner", materialization_intent.spool_part_owner_relpath): (
                materialization_intent.sha256,
                None,
                None,
            ),
            ("staging", materialization_intent.staging_relpath): (
                materialization_intent.sha256,
                None,
                None,
            ),
            ("staging_marker", materialization_intent.staging_marker_relpath): (
                materialization_intent.sha256,
                None,
                None,
            ),
        }
        allowed.update(partials)
        spool = {
            ("spool", materialization_intent.spool_relpath): (
                materialization_intent.sha256,
                materialization_intent.artifact_sha256,
                materialization_intent.artifact_byte_count,
            )
        }
        allowed.update(spool)
        mandatory.update(spool)
        if local_materialization_receipt is None:
            mandatory.update(partials)
        else:
            _same_identity(local_materialization_receipt, source_checkpoint)
            allowed.update(
                {
                    ("output", materialization_intent.output_relpath): (
                        local_materialization_receipt.sha256,
                        local_materialization_receipt.output_files_sha256,
                        local_materialization_receipt.output_byte_count,
                    ),
                }
            )
            mandatory.update(
                {
                    ("output", materialization_intent.output_relpath),
                }
            )
    ordered = tuple(sorted(resources, key=_cleanup_resource_sort_key))
    observed_keys = {(item.kind, item.relpath) for item in ordered}
    if not mandatory.issubset(observed_keys) or not observed_keys.issubset(allowed):
        raise ValueError("cleanup resources do not close the intent-owned namespace")
    for item in ordered:
        basis, expected_sha, expected_bytes = allowed[(item.kind, item.relpath)]
        if (
            item.ownership_basis_sha256 != basis
            or item.expected_sha256 != expected_sha
            or item.expected_byte_count != expected_bytes
        ):
            raise ValueError("cleanup resource identity drifted from durable evidence")
        if item.action == "transfer" and (
            outcome != "success"
            or item.target_owner_identity != source_checkpoint.processing_run_id
            or item.target_relpath != published_output_relpath
        ):
            raise ValueError("cleanup transfer target is not the published run")
        if item.kind == "output" and outcome == "success" and item.action != "transfer":
            raise ValueError("successful cleanup must transfer exact output")
    return LocalCleanupPlanV4(
        attempt_id=source_checkpoint.attempt_id,
        fence_identity=source_checkpoint.fence_identity,
        document_id=source_checkpoint.document_id,
        processing_run_id=source_checkpoint.processing_run_id,
        outcome=outcome,
        source_state=source_checkpoint.state,
        source_lifecycle_version=source_checkpoint.lifecycle_version,
        source_checkpoint_sha256=source_checkpoint.sha256,
        remote_task_identity=remote_task_identity,
        terminal_receipt_sha256=source_checkpoint.terminal_receipt_sha256,
        materialization_intent_sha256=source_checkpoint.materialization_intent_sha256,
        local_materialization_receipt_sha256=(
            source_checkpoint.local_materialization_receipt_sha256
        ),
        publication_winner_sha256=source_checkpoint.publication_winner_sha256,
        failure_receipt_sha256=(
            failure_receipt_sha256 or source_checkpoint.failure_receipt_sha256
        ),
        supersession_receipt_sha256=supersession_receipt_sha256,
        resources=ordered,
        resource_count=len(ordered),
        resources_sha256=_digest(_canonical_json(_resource_payloads(ordered))),
    )


def validate_local_cleanup_plan_v4(
    *,
    plan: LocalCleanupPlanV4,
    reservation: ResourceReservationV4,
    source_checkpoint: RemoteParseCheckpointV4,
    materialization_intent: MaterializationIntentV4 | None,
    local_receipt: LocalMaterializationReceiptV4 | None,
) -> None:
    """Mechanically replay one cleanup plan from its exact durable inputs."""

    if (
        type(plan) is not LocalCleanupPlanV4
        or type(reservation) is not ResourceReservationV4
        or type(source_checkpoint) is not RemoteParseCheckpointV4
        or (
            materialization_intent is not None
            and type(materialization_intent) is not MaterializationIntentV4
        )
        or (
            local_receipt is not None
            and type(local_receipt) is not LocalMaterializationReceiptV4
        )
    ):
        raise ValueError("cleanup plan validation input type is invalid")
    expected = build_local_cleanup_plan_v4(
        reservation=reservation,
        source_checkpoint=source_checkpoint,
        outcome=plan.outcome,
        resources=plan.resources,
        materialization_intent=materialization_intent,
        local_materialization_receipt=local_receipt,
        remote_task_identity=plan.remote_task_identity,
        failure_receipt_sha256=plan.failure_receipt_sha256,
        supersession_receipt_sha256=plan.supersession_receipt_sha256,
    )
    if plan != expected:
        raise ValueError("cleanup plan drifted from exact durable inputs")


def validate_resource_reservation_checkpoint_binding_v4(
    *,
    reservation: ResourceReservationV4,
    checkpoint: RemoteParseCheckpointV4,
) -> None:
    """Require one reservation to bind every immutable checkpoint fact it owns."""

    if (
        type(reservation) is not ResourceReservationV4
        or type(checkpoint) is not RemoteParseCheckpointV4
    ):
        raise ValueError("reservation/checkpoint binding type is invalid")
    reservation_facts = (
        reservation.attempt_id,
        reservation.attempt_generation,
        reservation.fence_identity,
        reservation.document_id,
        reservation.processing_run_id,
        reservation.source_pdf_sha256,
        reservation.source_byte_count,
        reservation.source_page_count,
        reservation.request_sha256,
        reservation.runtime_epoch_sha256,
        reservation.process_profile_sha256,
        reservation.credit_policy_sha256,
        reservation.reservation_input_sha256,
    )
    checkpoint_facts = (
        checkpoint.attempt_id,
        checkpoint.attempt_generation,
        checkpoint.fence_identity,
        checkpoint.document_id,
        checkpoint.processing_run_id,
        checkpoint.source_pdf_sha256,
        checkpoint.source_byte_count,
        checkpoint.source_page_count,
        checkpoint.request_sha256,
        checkpoint.runtime_epoch_sha256,
        checkpoint.process_profile_sha256,
        checkpoint.credit_policy_sha256,
        checkpoint.reservation_input_sha256,
    )
    if reservation_facts != checkpoint_facts:
        raise ValueError(
            "resource reservation drifted from checkpoint immutable facts"
        )


def build_local_cleanup_receipt_v4(
    *,
    plan: LocalCleanupPlanV4,
    cleanup_pending_checkpoint: RemoteParseCheckpointV4,
    results: tuple[LocalCleanupResourceResultV4, ...],
) -> LocalCleanupReceiptV4:
    if (
        cleanup_pending_checkpoint.state != "cleanup_pending"
        or cleanup_pending_checkpoint.cleanup_plan_sha256 != plan.sha256
        or cleanup_pending_checkpoint.previous_checkpoint_sha256
        != plan.source_checkpoint_sha256
        or cleanup_pending_checkpoint.lifecycle_version
        != plan.source_lifecycle_version + 1
    ):
        raise ValueError("cleanup receipt lacks its exact pending checkpoint")
    ordered = tuple(sorted(results, key=_cleanup_result_sort_key))
    planned = {
        (
            item.kind,
            item.relpath,
            item.action,
            item.target_owner_identity,
            item.target_relpath,
        )
        for item in plan.resources
    }
    observed = {
        (
            item.kind,
            item.relpath,
            "delete" if item.disposition == "absent" else "transfer",
            item.target_owner_identity,
            item.target_relpath,
        )
        for item in ordered
    }
    if planned != observed:
        raise ValueError("cleanup results do not close the exact plan")
    return LocalCleanupReceiptV4(
        attempt_id=plan.attempt_id,
        fence_identity=plan.fence_identity,
        document_id=plan.document_id,
        processing_run_id=plan.processing_run_id,
        outcome=plan.outcome,
        cleanup_plan_sha256=plan.sha256,
        cleanup_pending_checkpoint_sha256=cleanup_pending_checkpoint.sha256,
        cleanup_pending_lifecycle_version=(
            cleanup_pending_checkpoint.lifecycle_version
        ),
        results=ordered,
        result_count=len(ordered),
        results_sha256=_digest(_canonical_json(_cleanup_result_payloads(ordered))),
        parent_fsync_completed=True,
    )


def build_initial_remote_parse_checkpoint_v4(
    *,
    reservation: ResourceReservationV4,
    preparation_intent_sha256: str,
    held_resource_credit: ResourceCreditVector,
    snapshot_receipt_sha256: str | None = None,
) -> RemoteParseCheckpointV4:
    return RemoteParseCheckpointV4(
        attempt_id=reservation.attempt_id,
        attempt_generation=reservation.attempt_generation,
        fence_identity=reservation.fence_identity,
        document_id=reservation.document_id,
        processing_run_id=reservation.processing_run_id,
        state="prepared",
        lifecycle_version=0,
        previous_checkpoint_sha256=None,
        source_pdf_sha256=reservation.source_pdf_sha256,
        source_byte_count=reservation.source_byte_count,
        source_page_count=reservation.source_page_count,
        request_sha256=reservation.request_sha256,
        runtime_epoch_sha256=reservation.runtime_epoch_sha256,
        process_profile_sha256=reservation.process_profile_sha256,
        credit_policy_sha256=reservation.credit_policy_sha256,
        reservation_input_sha256=reservation.reservation_input_sha256,
        held_resource_credit=held_resource_credit,
        preparation_intent_sha256=preparation_intent_sha256,
        snapshot_receipt_sha256=snapshot_receipt_sha256,
    )


def advance_remote_parse_checkpoint_v4(
    previous: RemoteParseCheckpointV4,
    *,
    state: CheckpointStateV4,
    held_resource_credit: ResourceCreditVector,
    **evidence_updates: str | None,
) -> RemoteParseCheckpointV4:
    if state not in STAGED_RESOURCE_STATE_TRANSITIONS.get(
        previous.state, frozenset()
    ):
        raise ValueError("checkpoint-v4 state transition is not allowed")
    unknown = set(evidence_updates) - set(_EVIDENCE_FIELDS)
    if unknown:
        raise ValueError("checkpoint-v4 evidence update is unsupported")
    values = {name: getattr(previous, name) for name in _EVIDENCE_FIELDS}
    for name, value in evidence_updates.items():
        old = values[name]
        if old is not None and value != old:
            raise ValueError("immutable checkpoint evidence cannot be replaced")
        values[name] = value
    current = RemoteParseCheckpointV4(
        attempt_id=previous.attempt_id,
        attempt_generation=previous.attempt_generation,
        fence_identity=previous.fence_identity,
        document_id=previous.document_id,
        processing_run_id=previous.processing_run_id,
        state=state,
        lifecycle_version=previous.lifecycle_version + 1,
        previous_checkpoint_sha256=previous.sha256,
        source_pdf_sha256=previous.source_pdf_sha256,
        source_byte_count=previous.source_byte_count,
        source_page_count=previous.source_page_count,
        request_sha256=previous.request_sha256,
        runtime_epoch_sha256=previous.runtime_epoch_sha256,
        process_profile_sha256=previous.process_profile_sha256,
        credit_policy_sha256=previous.credit_policy_sha256,
        reservation_input_sha256=previous.reservation_input_sha256,
        held_resource_credit=held_resource_credit,
        **values,
    )
    validate_remote_parse_checkpoint_successor_v4(previous, current)
    return current


def build_resource_free_remote_parse_checkpoint_v4(
    *,
    state: Literal["preparation_failed", "superseded"],
    attempt_id: str,
    attempt_generation: int,
    fence_identity: str,
    document_id: str,
    processing_run_id: str,
    source_pdf_sha256: str,
    source_byte_count: int,
    source_page_count: int,
    request_sha256: str,
    runtime_epoch_sha256: str,
    process_profile_sha256: str,
    credit_policy_sha256: str,
    reservation_input_sha256: str,
    failure_receipt_sha256: str | None = None,
    supersession_receipt_sha256: str | None = None,
) -> RemoteParseCheckpointV4:
    if state == "preparation_failed" and failure_receipt_sha256 is None:
        raise ValueError("preparation failure lacks exact failure evidence")
    return RemoteParseCheckpointV4(
        attempt_id=attempt_id,
        attempt_generation=attempt_generation,
        fence_identity=fence_identity,
        document_id=document_id,
        processing_run_id=processing_run_id,
        state=state,
        lifecycle_version=0,
        previous_checkpoint_sha256=None,
        source_pdf_sha256=source_pdf_sha256,
        source_byte_count=source_byte_count,
        source_page_count=source_page_count,
        request_sha256=request_sha256,
        runtime_epoch_sha256=runtime_epoch_sha256,
        process_profile_sha256=process_profile_sha256,
        credit_policy_sha256=credit_policy_sha256,
        reservation_input_sha256=reservation_input_sha256,
        held_resource_credit=ResourceCreditVector(),
        failure_receipt_sha256=failure_receipt_sha256,
        supersession_receipt_sha256=supersession_receipt_sha256,
    )


def validate_remote_parse_checkpoint_successor_v4(
    previous: RemoteParseCheckpointV4,
    current: RemoteParseCheckpointV4,
) -> None:
    if current.state not in STAGED_RESOURCE_STATE_TRANSITIONS.get(
        previous.state, frozenset()
    ):
        raise ValueError("checkpoint-v4 state transition is not allowed")
    if (
        current.lifecycle_version != previous.lifecycle_version + 1
        or current.previous_checkpoint_sha256 != previous.sha256
    ):
        raise ValueError("checkpoint-v4 predecessor/version is not exact")
    _same_identity(previous, current)
    immutable = (
        "source_pdf_sha256",
        "source_byte_count",
        "source_page_count",
        "request_sha256",
        "runtime_epoch_sha256",
        "process_profile_sha256",
        "credit_policy_sha256",
        "reservation_input_sha256",
    )
    if any(getattr(previous, name) != getattr(current, name) for name in immutable):
        raise ValueError("checkpoint-v4 immutable source identity drifted")
    for name in _EVIDENCE_FIELDS:
        old = getattr(previous, name)
        new = getattr(current, name)
        if old is not None and old != new:
            raise ValueError("checkpoint-v4 immutable evidence drifted")
    allowed_new = _ALLOWED_NEW_EVIDENCE_BY_TRANSITION[
        (previous.state, current.state)
    ]
    introduced = {
        name
        for name in _EVIDENCE_FIELDS
        if getattr(previous, name) is None and getattr(current, name) is not None
    }
    if not introduced <= allowed_new:
        raise ValueError("checkpoint-v4 transition introduced unexpected evidence")
    if (
        current.state == "cleanup_pending"
        and current.held_resource_credit != previous.held_resource_credit
    ):
        raise ValueError("cleanup-pending credit must equal its source checkpoint")


def _validate_checkpoint_credit_shape_v4(
    checkpoint: RemoteParseCheckpointV4,
) -> None:
    credit = checkpoint.held_resource_credit
    if checkpoint.state in _FINAL_STATES:
        if credit != ResourceCreditVector():
            raise ValueError("final checkpoint retains resource credit")
        return
    if checkpoint.state == "ack_pending":
        if (
            credit.documents != 1
            or credit.provider_tasks != 1
            or credit.ack_items != 1
        ):
            raise ValueError("ack-pending resource credit shape drifted")
        if checkpoint.terminal_receipt_sha256 is None:
            if credit.provider_result_bytes != 0:
                raise ValueError("ack-pending invented provider result credit")
        elif credit.provider_result_bytes < 1:
            raise ValueError("ack-pending lost terminal provider result credit")
        local_names = (
            "snapshot_items",
            "snapshot_bytes",
            "remote_waits",
            "materialization_items",
            "compressed_bytes",
            "decoded_bytes",
            "temp_disk_bytes",
            "output_items",
            "output_bytes",
            "output_pages",
        )
        if any(getattr(credit, name) for name in local_names):
            raise ValueError("ack-pending retains cleaned local credit")
        return
    if (
        credit.documents != 1
        or credit.snapshot_items != 1
        or credit.snapshot_bytes != checkpoint.source_byte_count
    ):
        raise ValueError("resourceful checkpoint lacks exact snapshot credit")
    if checkpoint.state == "cleanup_pending":
        return
    base = ResourceCreditVector(
        documents=1,
        snapshot_items=1,
        snapshot_bytes=checkpoint.source_byte_count,
    )
    if checkpoint.state == "prepared":
        if credit != base:
            raise ValueError("prepared resource credit shape drifted")
        return
    if checkpoint.state == "reconciling":
        if credit != replace(base, remote_waits=1):
            raise ValueError("reconciling resource credit shape drifted")
        return
    if credit.provider_tasks != 1 or credit.ack_items != 1:
        raise ValueError("provider-owned checkpoint credit shape drifted")
    if checkpoint.state == "submitted":
        if (
            credit.remote_waits != 1
            or credit.provider_result_bytes != 0
            or credit.materialization_items
            or credit.compressed_bytes
            or credit.decoded_bytes
            or credit.temp_disk_bytes
            or credit.output_items
            or credit.output_bytes
            or credit.output_pages
        ):
            raise ValueError("submitted resource credit shape drifted")
        return
    if credit.remote_waits or credit.provider_result_bytes < 1:
        raise ValueError("terminal provider-result credit shape drifted")
    if checkpoint.state == "remote_terminal":
        if (
            credit.materialization_items
            or credit.compressed_bytes
            or credit.decoded_bytes
            or credit.temp_disk_bytes
            or credit.output_items
            or credit.output_bytes
            or credit.output_pages
        ):
            raise ValueError("remote-terminal resource credit shape drifted")
        return
    if checkpoint.state == "materializing":
        if (
            credit.materialization_items != 1
            or credit.compressed_bytes < credit.provider_result_bytes
            or credit.decoded_bytes < 1
            or credit.temp_disk_bytes < 1
            or credit.output_items
            or credit.output_bytes
            or credit.output_pages
        ):
            raise ValueError("materializing reservation credit shape drifted")
        return
    if checkpoint.state in {"local_materialized", "publish_committed"}:
        if (
            credit.materialization_items
            or credit.compressed_bytes != credit.provider_result_bytes
            or credit.decoded_bytes
            or credit.temp_disk_bytes
            or credit.output_items != 1
            or credit.output_bytes < 1
            or credit.output_pages != checkpoint.source_page_count
        ):
            raise ValueError("closed-output resource credit shape drifted")
        return
    raise ValueError("resourceful checkpoint credit state is unsupported")


def _validate_checkpoint_evidence_frontier_v4(
    checkpoint: RemoteParseCheckpointV4,
) -> None:
    if checkpoint.lifecycle_version == 0 and checkpoint.state in {
        "preparation_failed",
        "superseded",
    }:
        allowed = {
            "failure_receipt_sha256"
            if checkpoint.state == "preparation_failed"
            else "supersession_receipt_sha256"
        }
    else:
        order = (
            "prepared",
            "reconciling",
            "submitted",
            "remote_terminal",
            "materializing",
            "local_materialized",
            "publish_committed",
        )
        ordinary_fields = (
            {"preparation_intent_sha256", "snapshot_receipt_sha256"},
            {"submission_intent_sha256"},
            {"accepted_submission_sha256"},
            {"terminal_receipt_sha256"},
            {"materialization_intent_sha256"},
            {"local_materialization_receipt_sha256"},
            {"publication_winner_sha256"},
        )
        if checkpoint.state in order:
            frontier = order.index(checkpoint.state)
            allowed = set().union(*ordinary_fields[: frontier + 1])
        else:
            allowed = set().union(*ordinary_fields)
            allowed.update(
                {
                    "failure_receipt_sha256",
                    "supersession_receipt_sha256",
                    "cleanup_plan_sha256",
                }
            )
            if checkpoint.state in {"ack_pending", *_FINAL_STATES}:
                allowed.add("cleanup_receipt_sha256")
            if checkpoint.state in _FINAL_STATES:
                allowed.add("ack_receipt_sha256")
    unexpected = {
        name
        for name in _EVIDENCE_FIELDS
        if getattr(checkpoint, name) is not None and name not in allowed
    }
    if unexpected:
        raise ValueError("checkpoint-v4 contains future or conflicting evidence")


def local_output_files_sha256_v4(
    output_files: tuple[LocalOutputFileV4, ...],
) -> str:
    return _digest(_canonical_json([asdict(item) for item in output_files]))


def deterministic_local_resource_paths_v4(
    *,
    attempt_id: str,
    fence_identity: str,
    source_pdf_sha256: str,
    artifact_owner_identity: str | None,
    artifact_sha256: str | None,
    output_dir_name: str | None,
) -> dict[str, str]:
    _identity_tuple(attempt_id, fence_identity)
    _require_sha(source_pdf_sha256, "source PDF")
    paths = staged_snapshot_relpaths(
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        source_pdf_sha256=source_pdf_sha256,
    )
    if artifact_owner_identity is None:
        if artifact_sha256 is not None or output_dir_name is not None:
            raise ValueError("post-terminal path inputs are incomplete")
        return paths
    _identity(artifact_owner_identity, "artifact owner")
    if artifact_sha256 is None or output_dir_name is None:
        raise ValueError("post-terminal path inputs are incomplete")
    _require_sha(artifact_sha256, "retained artifact")
    _identity(output_dir_name, "output directory name")
    paths.update(
        staged_retained_relpaths(
            attempt_id=attempt_id,
            fence_identity=fence_identity,
            artifact_owner_identity=artifact_owner_identity,
            artifact_sha256=artifact_sha256,
        )
    )
    paths.update(
        staged_materialization_relpaths(
            output_dir_name=output_dir_name,
            attempt_id=attempt_id,
            fence_identity=fence_identity,
            artifact_sha256=artifact_sha256,
        )
    )
    return paths


def deterministic_local_resource_relpath_v4(
    *,
    resource_kind: LocalResourceKind,
    attempt_id: str,
    fence_identity: str,
    source_pdf_sha256: str,
    artifact_owner_identity: str | None = None,
    artifact_sha256: str | None = None,
    output_dir_name: str | None = None,
) -> str:
    paths = deterministic_local_resource_paths_v4(
        attempt_id=attempt_id,
        fence_identity=fence_identity,
        source_pdf_sha256=source_pdf_sha256,
        artifact_owner_identity=artifact_owner_identity,
        artifact_sha256=artifact_sha256,
        output_dir_name=output_dir_name,
    )
    if resource_kind not in _RESOURCE_ORDER or resource_kind not in paths:
        raise ValueError("resource path is unavailable before its evidence exists")
    return paths[resource_kind]


def decode_resource_reservation_v4(exact_bytes: bytes) -> ResourceReservationV4:
    payload = _decode_canonical_object(exact_bytes)
    _closed(payload, ResourceReservationV4)
    nested = _mapping(payload["reserved_credit"], "reserved credit")
    _closed(nested, ResourceCreditVector)
    value = ResourceReservationV4(
        **{
            **payload,
            "reserved_credit": ResourceCreditVector(**nested),
        }
    )
    _canonical_match(value.canonical_bytes, exact_bytes)
    return value


def decode_materialization_intent_v4(exact_bytes: bytes) -> MaterializationIntentV4:
    payload = _decode_canonical_object(exact_bytes)
    _closed(payload, MaterializationIntentV4)
    credit_payload = _mapping(payload["held_resource_credit"], "held resource credit")
    _closed(credit_payload, ResourceCreditVector)
    context_payload = _mapping(
        payload["provider_envelope_context"], "provider envelope context"
    )
    _closed(context_payload, ProviderEnvelopeContextV4)
    target_payload = _mapping(
        context_payload["parser_target_identity"], "provider envelope parser target"
    )
    target = ParserTargetIdentity.from_payload(target_payload)
    context = ProviderEnvelopeContextV4(
        **{
            **context_payload,
            "parser_target_identity": target,
        }
    )
    value = MaterializationIntentV4(
        **{
            **payload,
            "held_resource_credit": ResourceCreditVector(**credit_payload),
            "provider_envelope_context": context,
        }
    )
    _canonical_match(value.canonical_bytes, exact_bytes)
    return value


def decode_local_materialization_receipt_v4(
    exact_bytes: bytes,
) -> LocalMaterializationReceiptV4:
    payload = _decode_canonical_object(exact_bytes)
    _closed(payload, LocalMaterializationReceiptV4)
    output_files = _decode_tuple(
        payload["output_files"], LocalOutputFileV4, "output files"
    )
    value = LocalMaterializationReceiptV4(
        **{**payload, "output_files": output_files}
    )
    _canonical_match(value.canonical_bytes, exact_bytes)
    return value


def decode_local_cleanup_plan_v4(exact_bytes: bytes) -> LocalCleanupPlanV4:
    payload = _decode_canonical_object(exact_bytes)
    _closed(payload, LocalCleanupPlanV4)
    resources = _decode_tuple(
        payload["resources"], CleanupResourceEntryV4, "cleanup resources"
    )
    value = LocalCleanupPlanV4(**{**payload, "resources": resources})
    _canonical_match(value.canonical_bytes, exact_bytes)
    return value


def decode_local_cleanup_receipt_v4(exact_bytes: bytes) -> LocalCleanupReceiptV4:
    payload = _decode_canonical_object(exact_bytes)
    _closed(payload, LocalCleanupReceiptV4)
    results = _decode_tuple(
        payload["results"], LocalCleanupResourceResultV4, "cleanup results"
    )
    value = LocalCleanupReceiptV4(**{**payload, "results": results})
    _canonical_match(value.canonical_bytes, exact_bytes)
    return value


def decode_provider_ack_receipt_v4(exact_bytes: bytes) -> ProviderAckReceiptV4:
    payload = _decode_canonical_object(exact_bytes)
    _closed(payload, ProviderAckReceiptV4)
    value = ProviderAckReceiptV4(**payload)
    _canonical_match(value.canonical_bytes, exact_bytes)
    return value


def decode_remote_parse_checkpoint_v4(exact_bytes: bytes) -> RemoteParseCheckpointV4:
    payload = _decode_canonical_object(exact_bytes)
    _closed(payload, RemoteParseCheckpointV4)
    nested = _mapping(payload["held_resource_credit"], "held resource credit")
    _closed(nested, ResourceCreditVector)
    value = RemoteParseCheckpointV4(
        **{
            **payload,
            "held_resource_credit": ResourceCreditVector(**nested),
        }
    )
    _canonical_match(value.canonical_bytes, exact_bytes)
    return value


def _materialization_payload(
    receipt: LocalMaterializationReceiptV4,
) -> dict[str, Any]:
    payload = asdict(receipt)
    payload["output_files"] = [asdict(item) for item in receipt.output_files]
    return payload


def _cleanup_plan_payload(plan: LocalCleanupPlanV4) -> dict[str, Any]:
    payload = asdict(plan)
    payload["resources"] = _resource_payloads(plan.resources)
    return payload


def _cleanup_receipt_payload(receipt: LocalCleanupReceiptV4) -> dict[str, Any]:
    payload = asdict(receipt)
    payload["results"] = _cleanup_result_payloads(receipt.results)
    return payload


def _checkpoint_payload(checkpoint: RemoteParseCheckpointV4) -> dict[str, Any]:
    payload = asdict(checkpoint)
    payload["held_resource_credit"] = asdict(checkpoint.held_resource_credit)
    return payload


def _resource_payloads(
    resources: tuple[CleanupResourceEntryV4, ...],
) -> list[dict[str, Any]]:
    return [asdict(item) for item in resources]


def _cleanup_result_payloads(
    results: tuple[LocalCleanupResourceResultV4, ...],
) -> list[dict[str, Any]]:
    return [asdict(item) for item in results]


def _decode_canonical_object(exact_bytes: bytes) -> dict[str, Any]:
    if type(exact_bytes) is not bytes or not 1 <= len(exact_bytes) <= _MAX_BYTES:
        raise ValueError("canonical record bytes are outside the envelope")
    decoded = strict_json_loads(exact_bytes)
    if not isinstance(decoded, dict):
        raise ValueError("canonical record must be an object")  # noqa: TRY004
    return cast(dict[str, Any], decoded)


def _decode_tuple(value: object, item_type: type[Any], label: str) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")  # noqa: TRY004
    items: list[Any] = []
    for nested in value:
        mapping = _mapping(nested, label)
        _closed(mapping, item_type)
        items.append(item_type(**mapping))
    return tuple(items)


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")  # noqa: TRY004
    return cast(dict[str, Any], value)


def _closed(value: dict[str, Any], item_type: type[Any]) -> None:
    if set(value) != {item.name for item in fields(item_type)}:
        raise ValueError(f"{item_type.__name__} fields are not closed")


def _canonical_match(observed: bytes, expected: bytes) -> None:
    if observed != expected:
        raise ValueError("canonical record JSON is not canonical")


def _same_identity(left: object, right: object) -> None:
    names = ("attempt_id", "fence_identity", "document_id", "processing_run_id")
    if any(getattr(left, name) != getattr(right, name) for name in names):
        raise ValueError("attempt identity drifted")
    if (
        hasattr(left, "attempt_generation")
        and hasattr(right, "attempt_generation")
        and cast(Any, left).attempt_generation != cast(Any, right).attempt_generation
    ):
        raise ValueError("attempt generation drifted")


def _cleanup_resource_sort_key(
    item: CleanupResourceEntryV4,
) -> tuple[int, str]:
    return (_RESOURCE_ORDER[item.kind], item.relpath)


def _cleanup_result_sort_key(
    item: LocalCleanupResourceResultV4,
) -> tuple[int, str]:
    return (_RESOURCE_ORDER[item.kind], item.relpath)


def _canonical_json(value: object) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if not 1 <= len(encoded) <= _MAX_BYTES:
        raise ValueError("canonical record bytes are outside the envelope")
    return encoded


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _require_sha(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"{label} hash is not canonical")


def _optional_sha(value: str | None, label: str) -> None:
    if value is not None:
        _require_sha(value, label)


def _identity_tuple(*values: str) -> None:
    for value in values:
        _identity(value, "identity")


def _identity(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 512
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"{label} is invalid")


def _relative_path(value: str, label: str) -> None:
    validate_relative_resource_path_v4(value, label)


def _positive(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_INT:
        raise ValueError(f"{label} must be a positive bounded integer")


def _nonnegative(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_INT:
        raise ValueError(f"{label} must be a non-negative bounded integer")


def _checked_sum(values: Any) -> int:
    total = 0
    for value in values:
        _nonnegative(value, "byte count")
        total += value
        if total > _MAX_INT:
            raise ValueError("canonical byte count overflowed")
    return total


def _exact_tuple(value: object, label: str) -> None:
    if not isinstance(value, tuple):
        raise ValueError(f"{label} must be an exact tuple")  # noqa: TRY004


__all__ = [
    "ACK_RECEIPT_V4_CONTRACT",
    "CHECKPOINT_V4_CONTRACT",
    "CLEANUP_PLAN_V4_CONTRACT",
    "CLEANUP_RECEIPT_V4_CONTRACT",
    "MATERIALIZATION_INTENT_V4_CONTRACT",
    "PROVIDER_ENVELOPE_CONTEXT_V4_CONTRACT",
    "RESOURCE_RESERVATION_V4_CONTRACT",
    "CleanupResourceEntryV4",
    "LocalCleanupPlanV4",
    "LocalCleanupReceiptV4",
    "LocalCleanupResourceResultV4",
    "LocalMaterializationReceiptV4",
    "LocalOutputFileV4",
    "MaterializationIntentV4",
    "ProviderAckReceiptV4",
    "ProviderEnvelopeContextV4",
    "RemoteParseCheckpointV4",
    "ResourceReservationV4",
    "advance_remote_parse_checkpoint_v4",
    "build_initial_remote_parse_checkpoint_v4",
    "build_local_cleanup_plan_v4",
    "build_local_cleanup_receipt_v4",
    "build_local_materialization_receipt_v4",
    "build_materialization_intent_v4",
    "build_resource_free_remote_parse_checkpoint_v4",
    "build_resource_reservation_v4",
    "decode_local_cleanup_plan_v4",
    "decode_local_cleanup_receipt_v4",
    "decode_local_materialization_receipt_v4",
    "decode_materialization_intent_v4",
    "decode_provider_ack_receipt_v4",
    "decode_remote_parse_checkpoint_v4",
    "decode_resource_reservation_v4",
    "deterministic_local_resource_paths_v4",
    "deterministic_local_resource_relpath_v4",
    "local_output_files_sha256_v4",
    "provider_ack_request_v4_bytes",
    "provider_ack_request_v4_identity",
    "validate_local_cleanup_plan_v4",
    "validate_materialized_provider_evidence_v4",
    "validate_remote_parse_checkpoint_successor_v4",
    "validate_resource_reservation_checkpoint_binding_v4",
]
