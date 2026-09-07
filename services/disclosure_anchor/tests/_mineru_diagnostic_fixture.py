"""Synthetic closed diagnostic receipts for offline gate tests only."""
from __future__ import annotations

from typing import Any

from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import (
    canonical_client_submit_key_v2, canonical_result_owner_v2,
)


def diagnostic_disposal_fixture(
    *, source: str, runtime: str, pages: int, bundle: str,
) -> dict[str, Any]:
    return {
        "schema": "mineru-diagnostic-disposal.v1",
        "authority": "validated-diagnostic-no-publication.v1",
        "source_pdf_sha256": source, "runtime_bundle_identity_sha256": runtime,
        "attempt_identity": "test-attempt", "fence_identity": "test-fence",
        "submission_epoch_unix": 1,
        "idempotency_key": canonical_client_submit_key_v2(
            source_pdf_sha256=source, attempt_identity="test-attempt",
            fence_identity="test-fence", submission_epoch_unix=1,
        ),
        "task_id": "test-task", "terminal_artifact_sha256": "a" * 64,
        "terminal_artifact_bytes": 100,
        "terminal_artifact_owner": canonical_result_owner_v2(
            task_id="test-task", artifact_sha256="a" * 64, artifact_byte_count=100,
        ),
        "provider_bundle_sha256": bundle, "source_page_count": pages,
        "provider_page_count": pages, "local_resources_removed": True,
        "ack_response": {"schema": "mineru-task-protocol.v2", "task_id": "test-task", "status": "consumed"},
        "task_absence": {"detail": "Task not found"},
    }
