"""The one pure factory that freezes an M6 run spec from anchor, intent and runtime identity.

The owner anchor proves T0, clock, interval, resources and owner/device
identity; the intent declares campaign membership and phase; the runtime
binding supplies the release identity. Nothing is read or written here, and
every value the anchor proves overrides nothing: a disagreeing intent is
refused rather than reconciled.
"""

from __future__ import annotations

from disclosure_anchor.application.contracts.m6_campaign_intent import M6CampaignRuntimeBinding, M6RunIntent
from disclosure_anchor.application.contracts.m6_owner import M6OwnerAnchor
from disclosure_anchor.application.contracts.m6_run import M6RunSpec, M6RuntimeIdentity


def build_run_spec(*, anchor: M6OwnerAnchor, intent: M6RunIntent, runtime: M6CampaignRuntimeBinding) -> M6RunSpec:
    """Freeze the exact spec the controller binds by value; deterministic for identical inputs."""
    if intent.run_id != anchor.run_id:
        raise ValueError("run intent names a different run than the owner anchor")
    if intent.planned_seconds != anchor.planned_seconds:
        raise ValueError("run intent planned seconds differ from the owner's original interval")
    if intent.resources != anchor.resources:
        raise ValueError("run intent resource envelope differs from the owner's anchor")
    identity = M6RuntimeIdentity(
        source_commit=runtime.source_commit, source_manifest_sha256=runtime.source_manifest_sha256,
        runtime_bundle_identity_sha256=runtime.runtime_bundle_identity_sha256,
        process_profile_sha256=runtime.process_profile_sha256, worker_profile_sha256=runtime.worker_profile_sha256,
        owner_source_sha256=anchor.owner_source_sha256, gpu_device_identity_sha256=anchor.gpu_device_identity_sha256,
        deployment_qualification_sha256=runtime.deployment_qualification_sha256,
    )
    spec = M6RunSpec(
        run_id=anchor.run_id, campaign_id=intent.campaign_id, mode=intent.mode, phase=intent.phase,
        start_condition=intent.start_condition, clock=anchor.clock, runtime=identity,
        manifest_sha256=intent.manifest_sha256, scope_sha256=intent.scope_sha256,
        quality_plan_sha256=intent.quality_plan_sha256, t0_ticks=anchor.t0_ticks,
        planned_seconds=anchor.planned_seconds, deadline_ticks=anchor.deadline_ticks,
        max_close_ticks=anchor.max_close_ticks, carry_in_attempt_ids=intent.carry_in_attempt_ids,
        resources=anchor.resources,
    )
    anchor.assert_spec(spec)
    return spec


__all__ = ["build_run_spec"]
