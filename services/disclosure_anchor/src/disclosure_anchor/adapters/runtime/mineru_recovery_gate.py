"""Explicit, reviewed local-code compatibility for accepted-result recovery only.

This never qualifies a deployment or changes an H0 identity. The operator's
private grant and independent review bind both exact writer versions; normal
admission remains unavailable. Only already-accepted, pinned owners may run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import time
from typing import Any

from disclosure_anchor.adapters.runtime.mineru_deployment_gate import (
    _load_evidence,
    _verify_mineru_deployment_evidence,
    VerifiedMinerUDeployment,
)
from disclosure_anchor.adapters.runtime.mineru_host_capacity_observer import (
    MineruHostCapacitySampler,
    project_host_service_epoch,
)
from disclosure_anchor.adapters.runtime.mineru_identity import (
    MINERU_PROCESSING_WINDOW_SIZE,
    canonical_payload_sha256,
    client_bundle_identity,
    verify_runtime_manifest_payload,
    writer_code_digest,
)
from disclosure_anchor.application.contracts.mineru_process_profile import (
    MineruProcessProfile,
)
from disclosure_anchor.application.ports.staged_new_work_v4 import (
    validate_admission_document_ids,
)
from disclosure_anchor.settings import Settings

GRANT_CONTRACT = "mineru-accepted-result-recovery-grant.v1"
REVIEW_CONTRACT = "mineru-accepted-result-recovery-review.v1"
RECOVERY_RUNTIME_CONTRACT = "mineru-recovery-runtime-manifest.v1"
_ATTEMPT_KEYS = {
    "document_id",
    "attempt_id",
    "processing_run_id",
    "fence_identity",
    "source_pdf_sha256",
    "execution_spec_sha256",
    "h0_sha256",
    "accepted_submission_sha256",
}
_GRANT_KEYS = {
    "contract_version",
    "created_at",
    "expires_at",
    "reason",
    "old_runtime_sha256",
    "current_runtime_sha256",
    "old_writer_sha256",
    "current_writer_sha256",
    "process_profile_sha256",
    "worker_profile_sha256",
    "review_receipt_sha256",
    "attempts",
}


def _sha(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 71
        and value.startswith("sha256:")
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _utc(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("recovery grant time is invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("recovery grant requires explicit UTC")
    return parsed


def validate_recovery_grant(
    grant: dict[str, Any],
    review: dict[str, Any],
    *,
    now: datetime,
) -> tuple[dict[str, str], ...]:
    if (
        set(grant) != _GRANT_KEYS
        or grant.get("contract_version") != GRANT_CONTRACT
        or grant.get("reason") not in {
            "identity-content-encoding-recovery",
            "identity-content-encoding-and-publication-lineage-recovery",
        }
        or any(not _sha(grant[key]) for key in _GRANT_KEYS if key.endswith("sha256"))
    ):
        raise ValueError("recovery grant fields or identities are invalid")
    start, expiry = _utc(grant["created_at"]), _utc(grant["expires_at"])
    if not start <= now < expiry or not 0 < (expiry - start).total_seconds() <= 86400:
        raise ValueError("recovery grant is expired, future, or overlong")
    if (
        set(review)
        != {
            "contract_version",
            "verdict",
            "old_writer_sha256",
            "current_writer_sha256",
            "reviewed_delta_sha256",
        }
        or review.get("contract_version") != REVIEW_CONTRACT
        or review.get("verdict") != "GO"
        or not _sha(review.get("reviewed_delta_sha256"))
        or canonical_payload_sha256(review) != grant["review_receipt_sha256"]
        or any(
            review[key] != grant[key]
            for key in ("old_writer_sha256", "current_writer_sha256")
        )
        or grant["old_writer_sha256"] == grant["current_writer_sha256"]
    ):
        raise ValueError("recovery compatibility lacks an exact independent review")
    attempts = grant["attempts"]
    if (
        type(attempts) is not list
        or not 1 <= len(attempts) <= 8
        or any(
            type(item) is not dict or set(item) != _ATTEMPT_KEYS for item in attempts
        )
    ):
        raise ValueError("recovery grant attempt scope is invalid")
    for item in attempts:
        for key, value in item.items():
            if key.endswith("sha256"):
                if not _sha(value):
                    raise ValueError("recovery attempt evidence hash is invalid")
            elif (
                type(value) is not str
                or not 1 <= len(value) <= 128
                or value != value.strip()
                or any(ord(char) < 32 for char in value)
            ):
                raise ValueError("recovery attempt identity is invalid")
    validate_admission_document_ids(tuple(item["document_id"] for item in attempts))
    if len({item["attempt_id"] for item in attempts}) != len(attempts):
        raise ValueError("recovery grant repeats an attempt")
    return tuple(dict(item) for item in attempts)


def require_same_remote_manifest(old: dict[str, Any], current: dict[str, Any]) -> None:
    """Only local writer bytes may differ; model/image/topology/client stay exact."""
    old_client, new_client = old.get("client"), current.get("client")
    if (
        not isinstance(old_client, dict)
        or not isinstance(new_client, dict)
        or {key: value for key, value in old.items() if key != "client"}
        != {key: value for key, value in current.items() if key != "client"}
        or {
            key: value
            for key, value in old_client.items()
            if key != "writer_code_sha256"
        }
        != {
            key: value
            for key, value in new_client.items()
            if key != "writer_code_sha256"
        }
    ):
        raise ValueError(
            "recovery requires unchanged remote runtime and local client packages"
        )


def validate_recovery_runtime_wrapper(
    wrapper: dict[str, Any],
    *,
    grant: dict[str, Any],
    historical_epoch: dict[str, Any],
) -> None:
    if (
        set(wrapper)
        != {
            "schema",
            "identity_sha256",
            "manifest",
            "historical_runtime_identity_sha256",
            "historical_writer_sha256",
            "container_epoch_sha256",
        }
        or wrapper.get("schema") != RECOVERY_RUNTIME_CONTRACT
        or wrapper.get("identity_sha256") != grant["current_runtime_sha256"]
        or wrapper.get("historical_runtime_identity_sha256")
        != grant["old_runtime_sha256"]
        or wrapper.get("historical_writer_sha256") != grant["old_writer_sha256"]
        or wrapper.get("container_epoch_sha256")
        != historical_epoch["container_epoch_sha256"]
    ):
        raise ValueError(
            "recovery runtime must be an exact derived identity, not deployment attestation"
        )


@dataclass
class VerifiedMinerURecovery:
    grant: dict[str, Any]
    attempts: tuple[dict[str, str], ...]
    deployment: VerifiedMinerUDeployment
    expected_epoch: dict[str, Any]
    sampler: MineruHostCapacitySampler
    _last_probe: float | None = None

    @property
    def document_ids(self) -> tuple[str, ...]:
        return tuple(item["document_id"] for item in self.attempts)

    def assert_live(self, *, force: bool = False) -> None:
        now = datetime.now(UTC)
        if not _utc(self.grant["created_at"]) <= now < _utc(self.grant["expires_at"]):
            raise ValueError("recovery grant is no longer valid")
        # Controller-owned, bounded identity checks, not a telemetry sampling loop.
        observed = time.monotonic()
        if (
            not force
            and self._last_probe is not None
            and observed - self._last_probe < 30
        ):
            return
        if writer_code_digest() != self.grant["current_writer_sha256"]:
            raise ValueError("recovery writer changed after review")
        self.deployment.assert_fresh(now=now)
        self.deployment.probe_orchestrator(require_idle=False)
        self.deployment.probe_live_model()
        sample = project_host_service_epoch(
            self.sampler.sample_payload(),
            expected_collector_sha256=self.expected_epoch["collector_sha256"],
            expected_windows_node_identity_sha256=self.expected_epoch[
                "windows_node_identity_sha256"
            ],
        )
        if (
            sample.container_epoch_sha256
            != self.expected_epoch["container_epoch_sha256"]
            or sample.api_container_id != self.expected_epoch["api_container_id"]
            or any(
                (
                    sample.restart_count_total,
                    sample.oom_killed_count,
                    sample.unsafe_container_count,
                    sample.cgroup_oom_total,
                    sample.cgroup_oom_kill_total,
                )
            )
        ):
            raise ValueError("recovery remote epoch or safety state changed")
        self._last_probe = time.monotonic()


def load_recovery_gate(
    settings: Settings,
    *,
    profile: MineruProcessProfile,
    grant_path: Path,
    review_path: Path,
    current_manifest_path: Path,
    ssh_command: list[str],
) -> VerifiedMinerURecovery:
    grant, _ = _load_evidence(grant_path, label="recovery compatibility grant")
    review, _ = _load_evidence(review_path, label="recovery compatibility review")
    now = datetime.now(UTC)
    attempts = validate_recovery_grant(grant, review, now=now)
    if (
        settings.disclosure_mineru_runtime_bundle_identity_sha256
        != grant["old_runtime_sha256"]
        or profile.runtime_bundle_identity_sha256 != grant["old_runtime_sha256"]
        or profile.sha256 != grant["process_profile_sha256"]
        or writer_code_digest() != grant["current_writer_sha256"]
    ):
        raise ValueError(
            "recovery grant differs from current writer or bound process profile"
        )
    assert settings.disclosure_mineru_bin is not None
    current_wrapper, _ = _load_evidence(
        current_manifest_path, label="current recovery runtime"
    )
    current = verify_runtime_manifest_payload(
        current_wrapper,
        configured_identity=grant["current_runtime_sha256"],
        local_client_identity=client_bundle_identity(settings.disclosure_mineru_bin),
        local_processing_window_size=MINERU_PROCESSING_WINDOW_SIZE,
        local_writer_code_digest=grant["current_writer_sha256"],
    )
    historical = _verify_mineru_deployment_evidence(
        settings,
        parse_enabled=True,
        process_profile=profile,
        now=now,
        historical_writer_digest=grant["old_writer_sha256"],
    )
    if historical is None:
        raise ValueError("recovery historical qualification is absent")
    assert settings.disclosure_mineru_smoke_receipt is not None
    assert settings.disclosure_mineru_validation_receipt is not None
    smoke, _ = _load_evidence(
        settings.disclosure_mineru_smoke_receipt, label="historical smoke"
    )
    validation, _ = _load_evidence(
        settings.disclosure_mineru_validation_receipt,
        label="historical heldout",
        max_bytes=16 * 1024 * 1024,
    )
    require_same_remote_manifest(smoke["runtime_manifest"], current.manifest)
    epoch = validation["epoch_after"]["receipt"]["service_epoch"]
    validate_recovery_runtime_wrapper(
        current_wrapper, grant=grant, historical_epoch=epoch
    )
    sampler = MineruHostCapacitySampler(
        ssh_command=ssh_command,
        expected_collector_sha256=epoch["collector_sha256"],
        expected_windows_node_identity_sha256=epoch["windows_node_identity_sha256"],
    )
    return VerifiedMinerURecovery(grant, attempts, historical, dict(epoch), sampler)
