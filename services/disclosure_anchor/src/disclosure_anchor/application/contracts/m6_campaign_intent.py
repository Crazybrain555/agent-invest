"""Frozen campaign intent: the operator's declared inputs for one M6 run.

The intent carries what the owner cannot prove (campaign membership, phase,
runtime identity references, budgets); the owner's anchor proves T0, clock,
interval and resources. One pure factory joins them into the exact run spec.
Nothing here is a secret; private transport/workspace values live in the
campaign private binding, never in this tracked contract.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from disclosure_anchor.application.contracts.m6_campaign import M6Mode
from disclosure_anchor.application.contracts.m6_common import M6ClosedModel, M6Hash, M6Id, M6PositiveInt
from disclosure_anchor.application.contracts.m6_run import M6ResourceEnvelope

M6_CAMPAIGN_INTENT_CONTRACT = "m6.campaign-intent.v1"
M6_CAMPAIGN_INTENT_MAX_BYTES = 1_048_576


class M6CampaignRuntimeBinding(M6ClosedModel):
    """The runtime identity values the release binding provides; owner/device identity comes from the anchor."""

    source_commit: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{40}$")]
    source_manifest_sha256: M6Hash
    runtime_bundle_identity_sha256: M6Hash
    process_profile_sha256: M6Hash
    worker_profile_sha256: M6Hash | None
    deployment_qualification_sha256: M6Hash


class M6RunIntent(M6ClosedModel):
    """Spec-relevant declared inputs; anchor-proven fields must agree with the owner."""

    run_id: M6Id
    campaign_id: M6Id
    mode: M6Mode
    phase: Literal["short_batch", "hour_baseline", "stability_repeat", "recovery_experiment"]
    start_condition: Literal["cold", "resident_warm", "warm_service_disclosed"]
    manifest_sha256: M6Hash
    scope_sha256: M6Hash | None
    quality_plan_sha256: M6Hash
    carry_in_attempt_ids: Annotated[tuple[M6Id, ...], Field(max_length=100_000)]
    planned_seconds: Annotated[int, Field(strict=True, ge=1, le=7199)]
    resources: M6ResourceEnvelope

    @model_validator(mode="after")
    def closed_membership(self) -> Self:
        if (self.mode == "e2e_publication") != (self.scope_sha256 is not None):
            raise ValueError("publication runs carry exactly one campaign scope; diagnostics carry none")
        if self.carry_in_attempt_ids != tuple(sorted(set(self.carry_in_attempt_ids))):
            raise ValueError("carry-in attempt IDs must be sorted and unique")
        if len(self.carry_in_attempt_ids) > self.resources.max_attempts:
            raise ValueError("carry-in exceeds the run's attempt bound")
        return self


class M6CampaignIntent(M6ClosedModel):
    contract_version: Literal["m6.campaign-intent.v1"] = "m6.campaign-intent.v1"
    run: M6RunIntent
    runtime: M6CampaignRuntimeBinding
    release_manifest_sha256: M6Hash
    binding_sha256: M6Hash
    close_grace_seconds: Annotated[int, Field(strict=True, ge=1, le=7199)]
    memory_bytes: Annotated[int, Field(strict=True, ge=67_108_864, le=68_719_476_736)]
    bootstrap_bind_seconds: Annotated[int, Field(strict=True, ge=1, le=7200)] = 120
    ready_wait_seconds: Annotated[int, Field(strict=True, ge=5, le=300)] = 30
    runner_stop_reserve_seconds: Annotated[int, Field(strict=True, ge=1, le=3600)]
    verifier_deadline_seconds: Annotated[int, Field(strict=True, ge=1, le=7200)]
    verifier_identity: M6Id
    # The runner's lease guard: explicit, never guessed by a client (see M6LeasePolicy).
    stop_propagation_reserve_ns: Annotated[int, Field(strict=True, ge=1, le=30_000_000_000)]

    @model_validator(mode="after")
    def bounded_lifetime(self) -> Self:
        lifetime = self.run.planned_seconds + self.close_grace_seconds
        if lifetime > 7200:
            raise ValueError("planned plus grace seconds exceed the owner host ceiling")
        if self.bootstrap_bind_seconds > lifetime:
            raise ValueError("bootstrap bind bound exceeds the owner lifetime")
        if self.runner_stop_reserve_seconds >= self.run.planned_seconds:
            raise ValueError("runner stop reserve leaves no admission interval")
        if (self.run.mode == "e2e_publication") != (self.runtime.worker_profile_sha256 is not None):
            raise ValueError("publication runs require a worker profile; diagnostics carry none")
        return self

    @property
    def planned_seconds(self) -> M6PositiveInt:
        return self.run.planned_seconds


__all__ = [
    "M6_CAMPAIGN_INTENT_CONTRACT", "M6_CAMPAIGN_INTENT_MAX_BYTES",
    "M6CampaignIntent", "M6CampaignRuntimeBinding", "M6RunIntent",
]
