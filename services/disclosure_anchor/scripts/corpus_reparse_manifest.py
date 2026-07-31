"""Immutable manifest contract shared by corpus reset and reparse tools.

The v6 manifest records a closed catalog-bound PostgreSQL binary-COPY state
matrix, raw-file hashes, and run-owned artifact paths. Every preserved raw
document is a replay target; filing taxonomy and title text cannot narrow that
closure. Older count/sample manifests cannot authorize any operation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Literal, cast

from sqlalchemy import text
from sqlalchemy.engine import Connection

from scripts.corpus_reset_digest import (
    ResetDigestError,
    validate_state_matrix,
)
from disclosure_anchor.application.contracts.normalized_ir import (
    CURRENT_NORMALIZED_IR_VERSION,
)
from disclosure_anchor.application.contracts.parser_target import (
    ParserTargetIdentity,
    ParserTargetIdentityError,
)

MANIFEST_SCHEMA = "corpus-reparse-reset-manifest.v6"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PathFamily = Literal[
    "raw",
    "parser_artifact",
    "normalized_ir",
    "document_units",
]
_FAMILY_PREFIXES: dict[PathFamily, tuple[str, ...]] = {
    "raw": ("raw_documents",),
    "parser_artifact": ("parser_artifacts",),
    "normalized_ir": ("derived", "normalized_ir"),
    "document_units": ("derived", "document_unit_snapshots"),
}
_RESET_BUNDLE_COMPONENTS = ("audit", "reset-bundles")
_HEADER_FIELDS = {
    "manifest_schema",
    "generated_at",
    "document_count",
    "processing_run_count",
    "postgres_state",
    "target_identity",
    "code_snapshot",
}
_DOCUMENT_FIELDS = {
    "document_id",
    "raw_file_relpath",
    "raw_file_hash",
    "old_status",
    "old_current_processing_run_id",
    "input_identity_sha256",
}
_RUN_FIELDS = {
    "processing_run_id",
    "document_id",
    "run_kind",
    "status",
    "is_active",
    "input_raw_file_hash",
    "parser_artifact_relpath",
    "normalized_ir_relpath",
    "document_units_relpath",
}


class ManifestError(RuntimeError):
    """The reset/reparse manifest is incomplete, unsafe, or has drifted."""


@dataclass(frozen=True)
class CorpusManifest:
    header: dict[str, Any]
    documents: tuple[dict[str, Any], ...]
    runs: tuple[dict[str, Any], ...]
    sha256: str


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def document_source_rows(connection: Connection) -> list[dict[str, Any]]:
    """Read the complete preserved document/input closure canonically."""

    return [
        dict(row)
        for row in connection.execute(
            text(
                """
                SELECT d.document_id,
                       d.company_id,
                       d.security_id,
                       d.source_access_id,
                       d.provider,
                       d.provider_document_id,
                       d.title,
                       d.announcement_date::text AS announcement_date,
                       d.report_period,
                       d.raw_file_relpath,
                       d.raw_file_hash,
                       d.status,
                       d.provider_metadata,
                       d.current_processing_run_id,
                       d.supersedes_document_id,
                       d.correction_of_document_id,
                       d.class_filing_type,
                       d.class_market,
                       d.class_rules_version,
                       d.class_disclosure_topics,
                       d.class_publisher_categories,
                       d.class_content_categories,
                       security.security_code,
                       security.exchange
                  FROM disclosure_core.document AS d
                  LEFT JOIN disclosure_core.security AS security
                    ON security.security_id = d.security_id
                 ORDER BY d.document_id
                """
            )
        ).mappings()
    ]


def document_input_identity(row: dict[str, Any]) -> dict[str, Any]:
    """Freeze the DB fields that can change parse/build/public evidence."""

    identity = {
        field: row.get(field)
        for field in (
            "document_id",
            "company_id",
            "security_id",
            "source_access_id",
            "provider",
            "provider_document_id",
            "title",
            "announcement_date",
            "report_period",
            "raw_file_relpath",
            "raw_file_hash",
            "provider_metadata",
            "supersedes_document_id",
            "correction_of_document_id",
            "class_filing_type",
            "class_market",
            "class_rules_version",
            "class_disclosure_topics",
            "class_publisher_categories",
            "class_content_categories",
            "security_code",
            "exchange",
        )
    }
    for field in (
        "document_id",
        "company_id",
        "security_id",
        "source_access_id",
        "provider_document_id",
        "supersedes_document_id",
        "correction_of_document_id",
    ):
        value = identity[field]
        if value is not None:
            identity[field] = str(value)
    return identity


def hash_file(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as exc:
        raise ManifestError(f"cannot hash file {path}: {exc}") from exc


def _hash_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def capture_code_snapshot() -> dict[str, Any]:
    """Bind an operation to every tracked/untracked file in this service.

    ``git diff HEAD`` covers staged and unstaged tracked changes.  Git does not
    include untracked file contents in a diff, so those files are inventoried
    and hashed separately; this matters while a safety script itself is new.
    Runtime reset bundles live outside the checkout and therefore do not
    perturb the snapshot after export.
    """

    service_root = Path(__file__).resolve().parents[1]

    def run(
        *args: str,
        cwd: Path = service_root,
        text_mode: bool = False,
    ) -> bytes | str:
        try:
            completed = subprocess.run(
                ("git", *args),
                cwd=cwd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=text_mode,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ManifestError(
                f"cannot bind manifest to the current code: {exc}"
            ) from exc
        return cast(bytes | str, completed.stdout)

    top_level_raw = run("rev-parse", "--show-toplevel", text_mode=True)
    if not isinstance(top_level_raw, str):
        raise AssertionError("text-mode git output must be str")
    top_level = Path(top_level_raw.strip()).resolve()
    try:
        service_scope = service_root.resolve().relative_to(top_level).as_posix()
    except ValueError as exc:
        raise ManifestError("service root is outside its git checkout") from exc
    # Only parse-behavior surfaces bind the frozen generation: docs/tests/agent
    # notes must not invalidate a bundle during the weeks-long replay window.
    scope = tuple(
        f"{service_scope}/{part}"
        for part in ("config", "contracts", "scripts", "src")
    )

    head_raw = run("rev-parse", "HEAD", cwd=top_level, text_mode=True)
    if not isinstance(head_raw, str):
        raise AssertionError("text-mode git output must be str")
    diff = run(
        "diff",
        "--binary",
        "--no-ext-diff",
        "HEAD",
        "--",
        *scope,
        cwd=top_level,
    )
    if not isinstance(diff, bytes):
        raise AssertionError("binary git output must be bytes")
    untracked_raw = run(
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        *scope,
        cwd=top_level,
    )
    if not isinstance(untracked_raw, bytes):
        raise AssertionError("binary git output must be bytes")

    untracked: list[dict[str, Any]] = []
    for raw_name in sorted(part for part in untracked_raw.split(b"\0") if part):
        try:
            relative_name = raw_name.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManifestError("untracked path is not valid UTF-8") from exc
        path = top_level / relative_name
        if path.is_symlink():
            raise ManifestError(
                f"refusing untracked symlink in code snapshot: {relative_name}"
            )
        if not path.is_file():
            raise ManifestError(
                f"untracked code snapshot entry is not a file: {relative_name}"
            )
        untracked.append(
            {
                "path": relative_name,
                "sha256": hash_file(path),
                "byte_count": path.stat().st_size,
            }
        )

    snapshot = {
        "git_head": head_raw.strip(),
        "scope": list(scope),
        "tracked_diff_sha256": _hash_bytes(diff),
        "untracked_inventory_sha256": canonical_hash(untracked),
        "untracked_file_count": len(untracked),
    }
    return snapshot


def validate_code_snapshot(manifest: CorpusManifest) -> dict[str, Any]:
    expected = manifest.header.get("code_snapshot")
    if not isinstance(expected, dict):
        raise ManifestError("manifest header lacks code_snapshot")
    actual = capture_code_snapshot()
    if actual != expected:
        raise ManifestError(
            "service code drifted from the frozen manifest: "
            f"expected={expected}, actual={actual}"
        )
    return actual


def safe_data_path(
    data_root: Path,
    relpath: str,
    *,
    family: PathFamily,
) -> Path:
    """Resolve one provider-recorded path inside its bounded storage family."""

    relative = Path(relpath)
    expected_prefix = _FAMILY_PREFIXES[family]
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or relative.parts[: len(expected_prefix)] != expected_prefix
    ):
        raise ManifestError(
            f"unsafe {family} relpath outside {expected_prefix}: {relpath!r}"
        )
    lexical_service_root = data_root.absolute()
    lexical_data_root = lexical_service_root / "data"
    if lexical_service_root.is_symlink():
        raise ManifestError(f"service data root is a symlink: {data_root}")
    if lexical_data_root.is_symlink():
        raise ManifestError(f"service data directory is a symlink: {lexical_data_root}")
    service_root = lexical_service_root.resolve()
    root = lexical_data_root.resolve()
    if root.parent != service_root:
        raise ManifestError(
            f"service data directory escaped its configured root: {lexical_data_root}"
        )
    family_root = root.joinpath(*expected_prefix).resolve()
    lexical_candidate = lexical_data_root / relative
    current = lexical_data_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ManifestError(
                f"{family} relpath traverses a symlink: {relpath!r}"
            )
    candidate = lexical_candidate.resolve()
    try:
        candidate.relative_to(family_root)
    except ValueError as exc:
        raise ManifestError(
            f"{family} relpath escapes its storage family: {relpath!r}"
        ) from exc
    return lexical_candidate


def validate_reset_bundle_paths(
    data_root: Path,
    manifest_path: Path,
    *member_paths: Path,
) -> Path:
    """Require reset control files to live in one non-derived audit bundle.

    Manifest and backup bundle are control-plane evidence.
    If any of them lives below a parse-derived family, reset can move it out
    from under itself and make recovery impossible.  This
    boundary is structural: one direct child of ``audit/reset-bundles`` and no
    symlink traversal.  It never classifies document content.
    """

    lexical_service_root = data_root.absolute()
    if lexical_service_root.is_symlink():
        raise ManifestError(f"service data root is a symlink: {data_root}")
    service_root = lexical_service_root.resolve()
    lexical_bundle_root = lexical_service_root.joinpath(*_RESET_BUNDLE_COMPONENTS)

    current = lexical_service_root
    for part in _RESET_BUNDLE_COMPONENTS:
        if current.is_symlink():
            raise ManifestError(
                f"reset-bundle root traverses a symlink: {lexical_bundle_root}"
            )
        current = current / part
    if current.is_symlink():
        raise ManifestError(f"reset-bundle root is a symlink: {current}")
    bundle_root = lexical_bundle_root.resolve()
    if bundle_root.parent.parent != service_root:
        raise ManifestError(
            f"reset-bundle root escaped configured service root: {bundle_root}"
        )

    all_paths = (manifest_path, *member_paths)
    bundle_dir: Path | None = None
    seen_names: set[str] = set()
    for supplied in all_paths:
        lexical = supplied.absolute()
        try:
            relative = lexical.relative_to(lexical_bundle_root)
        except ValueError as exc:
            raise ManifestError(
                "reset control files must live under "
                f"{lexical_bundle_root}: {supplied}"
            ) from exc
        if (
            len(relative.parts) != 2
            or relative.parts[0] in {"", ".", ".."}
            or relative.parts[1] in {"", ".", ".."}
        ):
            raise ManifestError(
                "reset control files must be direct files in one bundle under "
                f"{lexical_bundle_root}: {supplied}"
            )
        if relative.parts[1] in seen_names:
            raise ManifestError(
                f"reset bundle member paths must be distinct: {supplied}"
            )
        seen_names.add(relative.parts[1])

        current = lexical_service_root
        for part in (*_RESET_BUNDLE_COMPONENTS, *relative.parts):
            if current.is_symlink():
                raise ManifestError(
                    f"reset control path traverses a symlink: {supplied}"
                )
            current = current / part
        if current.is_symlink():
            raise ManifestError(f"reset control path is a symlink: {supplied}")

        candidate_bundle = (
            bundle_root / relative.parts[0]
        ).resolve()
        if candidate_bundle.parent != bundle_root:
            raise ManifestError(
                f"reset control path escaped its audit bundle: {supplied}"
            )
        if bundle_dir is None:
            bundle_dir = candidate_bundle
        elif candidate_bundle != bundle_dir:
            raise ManifestError(
                "manifest, plan and backup must share one reset bundle"
            )
    if bundle_dir is None:
        raise AssertionError("manifest path always defines a reset bundle")
    return bundle_dir


def write_once_durable(path: Path, payload: bytes) -> None:
    """Durably create one immutable file; residue after a crash fails closed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError as exc:
        raise ManifestError(f"refusing to overwrite immutable file {path}") from exc


def write_manifest(
    path: Path,
    *,
    header: dict[str, Any],
    documents: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> str:
    """Atomically write JSONL plus a sidecar hash and return that hash."""

    ordered_documents = sorted(documents, key=lambda row: str(row["document_id"]))
    ordered_runs = sorted(runs, key=lambda row: str(row["processing_run_id"]))
    _validate_header(
        header,
        document_count=len(ordered_documents),
        run_count=len(ordered_runs),
    )
    if any(set(record) != _DOCUMENT_FIELDS for record in ordered_documents):
        raise ManifestError("manifest document field coverage mismatch")
    if any(set(record) != _RUN_FIELDS for record in ordered_runs):
        raise ManifestError("manifest processing_run field coverage mismatch")
    records = [
        {"record_type": "header", **header},
        *({"record_type": "document", **row} for row in ordered_documents),
        *({"record_type": "processing_run", **row} for row in ordered_runs),
    ]
    payload = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in records
    ).encode("utf-8")
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if path.exists() or sidecar.exists():
        raise ManifestError(
            f"refusing to overwrite manifest or hash sidecar: {path}"
        )
    write_once_durable(path, payload)
    write_once_durable(
        sidecar,
        (digest + "\n").encode("ascii"),
    )
    return digest


def _required_string(record: dict[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"manifest record lacks non-empty {field}")
    return value


def _required_nonnegative_int(record: dict[str, Any], field: str) -> int:
    value = record.get(field)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise ManifestError(f"manifest record lacks non-negative integer {field}")
    return value


def _validate_header(
    header: dict[str, Any],
    *,
    document_count: int,
    run_count: int,
) -> None:
    if header.get("manifest_schema") != MANIFEST_SCHEMA:
        raise ManifestError(
            "manifest header schema mismatch; only "
            f"{MANIFEST_SCHEMA} may authorize reset/reparse"
        )
    if set(header) != _HEADER_FIELDS:
        raise ManifestError(
            "manifest header field coverage mismatch: "
            f"unexpected={sorted(set(header) - _HEADER_FIELDS)}, "
            f"missing={sorted(_HEADER_FIELDS - set(header))}"
        )
    _required_string(header, "generated_at")
    if _required_nonnegative_int(header, "document_count") != document_count:
        raise ManifestError("manifest document_count does not match records")
    if (
        _required_nonnegative_int(header, "processing_run_count")
        != run_count
    ):
        raise ManifestError(
            "manifest processing_run_count does not match records"
        )
    try:
        validate_state_matrix(header.get("postgres_state"))
    except ResetDigestError as exc:
        raise ManifestError(
            f"manifest PostgreSQL state is incomplete: {exc}"
        ) from exc
    target = header.get("target_identity")
    if not isinstance(target, dict):
        raise ManifestError("manifest header lacks target_identity")
    for field in (
        "builder_rules_version",
        "retrieval_rules_version",
        "normalized_ir_contract_version",
    ):
        _required_string(target, field)
    expected_target_fields = {
        "parser_target",
        "max_parse_retries",
        "max_build_retries",
        "builder_rules_version",
        "retrieval_rules_version",
        "normalized_ir_contract_version",
    }
    if set(target) != expected_target_fields:
        raise ManifestError("manifest target_identity field coverage mismatch")
    try:
        parser_target = ParserTargetIdentity.from_payload(
            target.get("parser_target")
        )
    except ParserTargetIdentityError as exc:
        raise ManifestError(
            f"manifest parser target is invalid: {exc}"
        ) from exc
    if not parser_target.full_pdf:
        raise ManifestError("corpus reparse target must cover the full PDF")
    if (
        target["normalized_ir_contract_version"]
        != CURRENT_NORMALIZED_IR_VERSION
    ):
        raise ManifestError(
            "manifest target_identity normalized IR contract is unsupported"
        )
    for field in ("max_parse_retries", "max_build_retries"):
        if _required_nonnegative_int(target, field) < 1:
            raise ManifestError(
                f"manifest target_identity {field} must be positive"
            )
    code_snapshot = header.get("code_snapshot")
    if not isinstance(code_snapshot, dict):
        raise ManifestError("manifest header lacks code_snapshot")


def load_manifest(
    path: Path,
    *,
    data_root: Path | None = None,
    verify_raw_files: bool = False,
) -> CorpusManifest:
    """Load and fully validate an immutable reset/reparse manifest."""

    if verify_raw_files and data_root is None:
        raise ManifestError("raw-file verification requires data_root")

    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    sidecar = path.with_suffix(path.suffix + ".sha256")
    try:
        expected_digest = sidecar.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest hash sidecar {sidecar}: {exc}") from exc
    if expected_digest != digest:
        raise ManifestError(
            f"manifest hash mismatch: expected {expected_digest}, got {digest}"
        )

    header: dict[str, Any] | None = None
    documents: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ManifestError(
                f"invalid manifest JSON at line {line_number}: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise ManifestError(f"manifest line {line_number} is not an object")
        record_type = record.pop("record_type", None)
        if record_type == "header":
            if header is not None or documents or runs:
                raise ManifestError("manifest header must be the first record")
            header = record
        elif record_type == "document":
            if header is None or runs:
                raise ManifestError("document records must follow the header")
            documents.append(record)
        elif record_type == "processing_run":
            if header is None:
                raise ManifestError("processing_run record precedes header")
            runs.append(record)
        else:
            raise ManifestError(
                f"unknown manifest record_type at line {line_number}: {record_type!r}"
            )
    if header is None:
        raise ManifestError("missing manifest header")
    _validate_header(
        header,
        document_count=len(documents),
        run_count=len(runs),
    )
    if documents != sorted(documents, key=lambda row: str(row.get("document_id"))):
        raise ManifestError("manifest document records are not canonically ordered")
    if runs != sorted(runs, key=lambda row: str(row.get("processing_run_id"))):
        raise ManifestError("manifest run records are not canonically ordered")
    document_ids: set[str] = set()
    current_run_by_document: dict[str, str] = {}
    for record in documents:
        if set(record) != _DOCUMENT_FIELDS:
            raise ManifestError("manifest document field coverage mismatch")
        document_id = _required_string(record, "document_id")
        if document_id in document_ids:
            raise ManifestError(f"duplicate document_id in manifest: {document_id}")
        document_ids.add(document_id)
        raw_relpath = _required_string(record, "raw_file_relpath")
        raw_hash = _required_string(record, "raw_file_hash")
        if _HASH_RE.fullmatch(raw_hash) is None:
            raise ManifestError(
                f"invalid raw_file_hash for {document_id}: {raw_hash!r}"
            )
        if data_root is not None:
            raw_path = safe_data_path(data_root, raw_relpath, family="raw")
            if not raw_path.is_file():
                raise ManifestError(
                    f"raw PDF is missing or not a file for {document_id}: {raw_path}"
                )
            if verify_raw_files:
                actual = hash_file(raw_path)
                if actual != raw_hash:
                    raise ManifestError(
                        f"raw hash mismatch for {document_id}: "
                        f"expected {raw_hash}, got {actual}"
                    )
        _required_string(record, "old_status")
        input_identity_sha256 = _required_string(
            record, "input_identity_sha256"
        )
        if _HASH_RE.fullmatch(input_identity_sha256) is None:
            raise ManifestError(
                f"document {document_id} has invalid input_identity_sha256"
            )
        old_current_run = record.get("old_current_processing_run_id")
        if old_current_run is not None:
            if not isinstance(old_current_run, str) or not old_current_run:
                raise ManifestError(
                    f"invalid old_current_processing_run_id for {document_id}"
                )
            current_run_by_document[document_id] = old_current_run
    run_ids: set[str] = set()
    run_document_ids: dict[str, str] = {}
    path_fields: tuple[tuple[str, PathFamily], ...] = (
        ("parser_artifact_relpath", "parser_artifact"),
        ("normalized_ir_relpath", "normalized_ir"),
        ("document_units_relpath", "document_units"),
    )
    for record in runs:
        if set(record) != _RUN_FIELDS:
            raise ManifestError("manifest processing_run field coverage mismatch")
        run_id = _required_string(record, "processing_run_id")
        if run_id in run_ids:
            raise ManifestError(f"duplicate processing_run_id in manifest: {run_id}")
        run_ids.add(run_id)
        run_document_id = _required_string(record, "document_id")
        run_document_ids[run_id] = run_document_id
        if run_document_id not in document_ids:
            raise ManifestError(f"run {run_id} references a document outside manifest")
        _required_string(record, "run_kind")
        _required_string(record, "status")
        if not isinstance(record.get("is_active"), bool):
            raise ManifestError(f"run {run_id} is_active must be boolean")
        if data_root is not None:
            for field, family in path_fields:
                relpath = record.get(field)
                if relpath is not None:
                    if not isinstance(relpath, str) or not relpath:
                        raise ManifestError(f"run {run_id} has invalid {field}")
                    safe_data_path(data_root, relpath, family=family)

    for document_id, current_run_id in current_run_by_document.items():
        if current_run_id not in run_ids:
            raise ManifestError(
                f"document {document_id} current run is absent from manifest"
            )
        if run_document_ids[current_run_id] != document_id:
            raise ManifestError(
                f"document {document_id} current run belongs to another document"
            )

    return CorpusManifest(
        header=header,
        documents=tuple(documents),
        runs=tuple(runs),
        sha256=digest,
    )
