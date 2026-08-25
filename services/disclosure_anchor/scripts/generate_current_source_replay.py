#!/usr/bin/env python3
"""Generate a source-replayed semantic evaluation for every active Unit.

This is deliberately stronger than a snapshot comparison.  In one PostgreSQL
``REPEATABLE READ READ ONLY`` transaction it loads every active generation,
re-admits the immutable source PDF and exact ProviderDocument bundle, rebuilds
the provider Units, replays the hash-bound semantic receipts without a model
call, and invokes the same publication guard used by the write path.  Only
after the guard has compared every persisted private Unit field with that fresh
source replay are evaluation rows emitted.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.orm import Session

from disclosure_anchor.adapters.db.postgres.connection import (
    app_database_url,
    create_db_engine,
)
from disclosure_anchor.adapters.db.postgres.repositories import (
    DocumentRepository,
    DocumentUnitRepository,
    ProcessingRunRepository,
    SecurityRepository,
)
from disclosure_anchor.adapters.parsers.pdf_text_observation import (
    observe_pdf_text_rectangles,
)
from disclosure_anchor.adapters.semantics.runtime import build_semantic_runtime
from disclosure_anchor.adapters.storage.artifact_store import ArtifactStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.adapters.storage.provider_document_source import (
    ProviderDocumentFileSource,
)
from disclosure_anchor.application.contracts.provider_unit import (
    PROVIDER_UNIT_BUILDER_VERSION,
)
from disclosure_anchor.application.contracts.document_unit_body_status import (
    derive_document_unit_body_status,
)
from disclosure_anchor.application.contracts.semantic_routes import (
    SEMANTIC_ROUTE_RECEIPT_V1,
    SEMANTIC_ROUTE_RECEIPT_VERSION,
    SEMANTIC_ROUTE_RECEIPTS_V1_FILENAME,
    SEMANTIC_ROUTER_VERSION,
)
from disclosure_anchor.application.services.provider_document_admission import (
    ProviderDocumentAdmission,
)
from disclosure_anchor.application.use_cases.publish_run import (
    ProviderDocumentPublicationGuard,
)
from disclosure_anchor.domain import entities as e
from disclosure_anchor.settings import load_settings


_SHA256_REPLAY_FIELDS = ("content_hash", "query_projection_hash")


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write_new(path: Path, payload: bytes) -> None:
    """Create immutable evidence without silently replacing an older receipt."""

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _source_tree_manifest(repository_root: Path) -> dict[str, object]:
    """Bind the exact local implementation bytes used for the replay."""

    source_root = repository_root / "src" / "disclosure_anchor"
    paths = sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix in {".json", ".py"}
    )
    script_path = Path(__file__).resolve()
    paths.append(script_path)
    files = [
        {
            "path": str(path.relative_to(repository_root)),
            "sha256": _sha256_bytes(path.read_bytes()),
        }
        for path in paths
    ]
    return {
        "file_count": len(files),
        "manifest_sha256": _sha256_bytes(
            json.dumps(
                files,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        "files": files,
    }


def _receipt_relpath(run: e.ProcessingRun) -> tuple[Path, str]:
    if run.semantic_route_receipts_relpath is not None:
        if run.semantic_route_receipts_contract_version != SEMANTIC_ROUTE_RECEIPT_VERSION:
            raise ValueError("active run has an unsupported semantic receipt version")
        return Path(run.semantic_route_receipts_relpath), SEMANTIC_ROUTE_RECEIPT_VERSION
    if run.semantic_route_receipts_contract_version is not None:
        raise ValueError("active run has a semantic receipt version without a path")
    if run.document_units_relpath is None:
        raise ValueError("active run has no Unit snapshot path")
    return (
        Path(run.document_units_relpath).parent / SEMANTIC_ROUTE_RECEIPTS_V1_FILENAME,
        SEMANTIC_ROUTE_RECEIPT_V1,
    )


def _evaluation_row(
    *,
    document: e.Document,
    run: e.ProcessingRun,
    unit: e.DocumentUnit,
    decision_source: str,
    body_status: str,
) -> dict[str, object]:
    if document.provider_document_id is None or not document.provider_document_id:
        raise ValueError("active document has no provider document identity")
    if document.class_filing_type is None or not document.class_filing_type:
        raise ValueError("active document has no effective filing type")
    if unit.provider_document_id != document.provider_document_id:
        raise ValueError("active Unit provider document identity drifted")
    if unit.processing_run_id != run.processing_run_id:
        raise ValueError("active Unit processing run identity drifted")
    if unit.order_index < 1:
        raise ValueError("active Unit order must start at one")
    for field in _SHA256_REPLAY_FIELDS:
        value = getattr(unit, field)
        if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
            raise ValueError(f"active Unit {field} is not a canonical SHA-256")
    if body_status not in {"content", "heading_only", "empty"}:
        raise ValueError("active Unit body status is unsupported")
    return {
        "applicability": unit.applicability,
        "asset_id": unit.asset_id,
        "body_status": body_status,
        "content_hash": unit.content_hash,
        "decision_source": decision_source,
        "document_id": document.document_id,
        "effective_filing_type": document.class_filing_type,
        "heading_path": list(unit.heading_path),
        "processing_run_id": run.processing_run_id,
        "provider_document_id": document.provider_document_id,
        "query_projection_hash": unit.query_projection_hash,
        "section_keys": list(unit.section_keys or []),
        "semantic_keys": list(unit.semantic_keys or []),
        "title": unit.title,
        "unit_index": unit.order_index - 1,
    }


def _active_generation_ids(session: Session) -> list[tuple[str, str]]:
    rows = session.execute(
        text(
            """
            SELECT d.document_id::text, r.processing_run_id::text
              FROM disclosure_core.document AS d
              JOIN disclosure_core.processing_run AS r
                ON r.processing_run_id = d.current_processing_run_id
             WHERE r.is_active
             ORDER BY d.provider_document_id, d.document_id
            """
        )
    ).all()
    if not rows:
        raise ValueError("there are no active document generations to replay")
    return [(str(document_id), str(run_id)) for document_id, run_id in rows]


def _replay_active_generation(
    *,
    session: Session,
    guard: ProviderDocumentPublicationGuard,
    semantic_receipts: Any,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    documents = DocumentRepository(session)
    runs = ProcessingRunRepository(session)
    securities = SecurityRepository(session)
    units = DocumentUnitRepository(session)
    evaluation_rows: list[dict[str, object]] = []
    source_documents: list[dict[str, object]] = []
    observed_assets: set[str] = set()

    for document_id, run_id in _active_generation_ids(session):
        document = documents.get(document_id)
        run = runs.get(run_id)
        if document is None or run is None:
            raise ValueError("active generation disappeared inside repeatable-read snapshot")
        if document.current_processing_run_id != run.processing_run_id or not run.is_active:
            raise ValueError("active generation pointer is inconsistent")
        artifact_owner = runs.get(run.artifact_owner_processing_run_id)
        security = (
            securities.get(document.security_id)
            if document.security_id is not None
            else None
        )
        if artifact_owner is None or security is None:
            raise ValueError("active generation has no artifact owner or security")
        persisted_units = units.list_by_processing_run(run.processing_run_id)
        if not persisted_units:
            raise ValueError("active generation has no persisted Units")

        # The production publication guard re-admits PDF/bundle bytes, rebuilds
        # Units, replays the frozen receipts, and compares every private field.
        guard(
            run=run,
            document=document,
            artifact_owner=artifact_owner,
            security_code=security.security_code,
            units=persisted_units,
        )

        if run.semantic_route_receipts_hash is None:
            raise ValueError("active generation has no semantic receipt hash")
        receipt_relpath, expected_receipt_version = _receipt_relpath(run)
        receipt_rows = semantic_receipts.read(
            relpath=receipt_relpath,
            expected_hash=run.semantic_route_receipts_hash,
        )
        ordered_units = sorted(persisted_units, key=lambda item: item.order_index)
        if len(receipt_rows) != len(ordered_units):
            raise ValueError("active receipt count differs from persisted Units")
        for receipt_row, unit in zip(receipt_rows, ordered_units, strict=True):
            if receipt_row.receipt.contract_version != expected_receipt_version:
                raise ValueError("active semantic receipt contract drifted")
            if (
                receipt_row.order_index != unit.order_index
                or receipt_row.asset_id != unit.asset_id
            ):
                raise ValueError("active semantic receipt identity drifted")
            if unit.asset_id in observed_assets:
                raise ValueError("active source replay repeats an asset identity")
            # The guard above has already proved these persisted payload fields
            # byte-for-value equal to the freshly rebuilt/routed source draft.
            # Derive the public status here without reading the target view, so
            # the subsequent live-view audit cannot circularly self-validate.
            body_status = derive_document_unit_body_status(
                payload_kind=unit.payload_kind,
                payload=unit.payload,
                title=unit.title,
            )
            evaluation_rows.append(
                _evaluation_row(
                    document=document,
                    run=run,
                    unit=unit,
                    decision_source=receipt_row.receipt.decision_source,
                    body_status=body_status,
                )
            )
            observed_assets.add(unit.asset_id)

        source_documents.append(
            {
                "artifact_owner_processing_run_id": artifact_owner.processing_run_id,
                "builder_rules_version": run.builder_rules_version,
                "document_id": document.document_id,
                "provider_document_artifact_sha256": artifact_owner.artifact_hash,
                "provider_document_id": document.provider_document_id,
                "raw_file_sha256": document.raw_file_hash,
                "semantic_receipts_sha256": run.semantic_route_receipts_hash,
                "processing_run_id": run.processing_run_id,
                "unit_count": len(ordered_units),
            }
        )

    evaluation_rows.sort(
        key=lambda row: (
            str(row["provider_document_id"]),
            cast(int, row["unit_index"]),
        )
    )
    return evaluation_rows, source_documents


def generate(*, source_revision: str) -> tuple[dict[str, object], dict[str, object]]:
    settings = load_settings()
    repository_root = Path(__file__).resolve().parents[1]
    paths = FileStorePathBuilder(settings)
    artifacts = ArtifactStore(paths)
    provider_source = ProviderDocumentFileSource(
        paths,
        text_reader=observe_pdf_text_rectangles,
    )
    semantic = build_semantic_runtime(
        settings=settings,
        paths=paths,
        artifacts=artifacts,
    )
    guard = ProviderDocumentPublicationGuard(
        ProviderDocumentAdmission(path_builder=paths, source=provider_source),
        semantic_router=semantic.router,
        semantic_receipts=semantic.receipts,
    )
    engine = create_db_engine(app_database_url(settings))
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            transaction = dict(
                connection.execute(
                    text(
                        """
                        SELECT current_setting('transaction_isolation')
                                   AS transaction_isolation,
                               current_setting('transaction_read_only')
                                   AS transaction_read_only,
                               txid_current_snapshot()::text
                                   AS transaction_snapshot
                        """
                    )
                )
                .mappings()
                .one()
            )
            if (
                transaction["transaction_isolation"] != "repeatable read"
                or transaction["transaction_read_only"] != "on"
            ):
                raise ValueError("source replay transaction is not read-only repeatable-read")
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                rows, source_documents = _replay_active_generation(
                    session=session,
                    guard=guard,
                    semantic_receipts=semantic.receipts,
                )
    finally:
        engine.dispose()

    generated_at = datetime.now(timezone.utc).isoformat()
    row_aggregate = _sha256_bytes(
        json.dumps(
            rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    evaluation_id = (
        f"current-source-{semantic.router.taxonomy.version}-"
        f"{SEMANTIC_ROUTER_VERSION}-{PROVIDER_UNIT_BUILDER_VERSION}-"
        f"{row_aggregate.removeprefix('sha256:')[:16]}"
    )
    evaluation: dict[str, object] = {
        "builder_rules_version": PROVIDER_UNIT_BUILDER_VERSION,
        "contract_version": "semantic_route_model_eval.v1",
        "evaluation_id": evaluation_id,
        "generated_at_utc": generated_at,
        "row_aggregate_sha256": row_aggregate,
        "row_count": len(rows),
        "router_version": SEMANTIC_ROUTER_VERSION,
        "rows": rows,
        "taxonomy_version": semantic.router.taxonomy.version,
    }
    receipt: dict[str, object] = {
        "builder_rules_version": PROVIDER_UNIT_BUILDER_VERSION,
        "contract_version": "current_source_unit_replay_receipt.v1",
        "evaluation_id": evaluation_id,
        "generated_at_utc": generated_at,
        "passed": True,
        "read_scope": {
            "transaction_isolation": transaction["transaction_isolation"],
            "transaction_read_only": True,
            "transaction_snapshot": transaction["transaction_snapshot"],
        },
        "replay_guarantee": _replay_guarantee(),
        "router_version": SEMANTIC_ROUTER_VERSION,
        "row_aggregate_sha256": row_aggregate,
        "row_count": len(rows),
        "source_documents": source_documents,
        "source_revision": source_revision,
        "source_tree": _source_tree_manifest(repository_root),
        "taxonomy_version": semantic.router.taxonomy.version,
    }
    return evaluation, receipt


def _replay_guarantee() -> str:
    return (
        "production publication guard re-admitted each immutable source PDF "
        f"and ProviderDocument, rebuilt {PROVIDER_UNIT_BUILDER_VERSION}, replayed "
        "the exact hash-bound semantic receipts without a model call, and compared "
        "every persisted private Unit field before this evaluation was emitted; "
        "body_status was then independently derived from the guarded source-equal "
        "payload fields without reading the public Unit view"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.evaluation_output.resolve() == args.receipt_output.resolve():
        raise ValueError("evaluation and receipt outputs must be distinct")
    evaluation, receipt = generate(source_revision=args.source_revision)
    evaluation_bytes = _canonical_json(evaluation)
    receipt["evaluation"] = {
        "path": str(args.evaluation_output.resolve()),
        "sha256": _sha256_bytes(evaluation_bytes),
    }
    _atomic_write_new(args.evaluation_output, evaluation_bytes)
    _atomic_write_new(args.receipt_output, _canonical_json(receipt))
    print(
        json.dumps(
            {
                "evaluation_id": evaluation["evaluation_id"],
                "passed": True,
                "row_count": evaluation["row_count"],
                "receipt": str(args.receipt_output.resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
