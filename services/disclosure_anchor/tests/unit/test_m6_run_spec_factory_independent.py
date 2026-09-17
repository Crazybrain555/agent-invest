"""Independent spec-factory oracle: original owner interval and declared intent.

Existing synthetic specs are the hand-declared expected result, not generated
by the new factory. These checks neither launch a host nor open a database.
"""
from __future__ import annotations

import unittest

from disclosure_anchor.application.contracts.m6_campaign_intent import (
    M6CampaignIntent, M6CampaignRuntimeBinding, M6RunIntent,
)
from disclosure_anchor.application.services.m6_run_spec_factory import build_run_spec
from tests import m6_owner_support as owner
from tests import m6_support as m6


class RunSpecFactoryTests(unittest.TestCase):
    def parts(self, mode="e2e_publication"):
        spec = m6.make_fixture(mode, {"a": (7, "replay")}).spec
        value = spec.model_dump()
        intent = M6RunIntent.model_validate({key: value[key] for key in (
            "run_id", "campaign_id", "mode", "phase", "start_condition", "manifest_sha256", "scope_sha256",
            "quality_plan_sha256", "carry_in_attempt_ids", "planned_seconds", "resources",
        )})
        runtime = M6CampaignRuntimeBinding.model_validate({key: value["runtime"][key] for key in (
            "source_commit", "source_manifest_sha256", "runtime_bundle_identity_sha256",
            "process_profile_sha256", "worker_profile_sha256", "deployment_qualification_sha256",
        )})
        return spec, owner.anchor_for(spec), intent, runtime

    def campaign(self, **overrides):
        _, _, intent, runtime = self.parts()
        values = dict(run=intent, runtime=runtime, release_manifest_sha256=m6.digest("release"),
                      evaluation_plan_sha256=m6.digest("frozen-evaluation-plan"),
                      binding_sha256=m6.digest("binding"), close_grace_seconds=30, memory_bytes=268435456,
                      bootstrap_bind_seconds=60, ready_wait_seconds=30, runner_stop_reserve_seconds=5,
                      verifier_deadline_seconds=80, verifier_identity="verifier-independent",
                      stop_propagation_reserve_ns=1_000_000_000)
        values.update(overrides)
        return M6CampaignIntent(**values)

    def test_factory_preserves_exact_declared_spec_in_both_modes(self):
        for mode in ("e2e_publication", "service_diagnostic"):
            with self.subTest(mode=mode):
                spec, anchor, intent, runtime = self.parts(mode)
                first = build_run_spec(anchor=anchor, intent=intent, runtime=runtime)
                second = build_run_spec(anchor=anchor, intent=intent, runtime=runtime)
                self.assertEqual(first.canonical_bytes(), spec.canonical_bytes())
                self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
                self.assertEqual(first.deadline_ticks - first.t0_ticks, 60 * 10_000_000)
                self.assertEqual(first.max_close_ticks - first.deadline_ticks, 30 * 10_000_000)

    def test_disagreement_with_owner_is_refused_instead_of_reconciled(self):
        _, anchor, intent, runtime = self.parts()
        changes = ({"run_id": "different-run"}, {"planned_seconds": 61},
                   {"resources": m6.envelope(max_attempts=21)})
        for change in changes:
            values = intent.model_dump()
            values.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                build_run_spec(anchor=anchor, intent=M6RunIntent.model_validate(values), runtime=runtime)

    def test_runtime_update_cannot_replace_owner_clock_or_device_identity(self):
        spec, anchor, intent, runtime = self.parts()
        changed = runtime.model_copy(update={"source_commit": "a" * 40, "process_profile_sha256": m6.digest("new-profile")})
        actual = build_run_spec(anchor=anchor, intent=intent, runtime=changed)
        expected = spec.model_dump()
        expected["runtime"]["source_commit"] = "a" * 40
        expected["runtime"]["process_profile_sha256"] = m6.digest("new-profile")
        self.assertEqual(actual.model_dump(), expected)
        for key in ("owner_source_sha256", "gpu_device_identity_sha256", "clock", "t0_ticks"):
            value = runtime.model_dump()
            value[key] = m6.digest("override")
            with self.subTest(key=key), self.assertRaises(ValueError):
                M6CampaignRuntimeBinding.model_validate(value)

    def test_membership_mode_and_resource_bounds_stay_explicit(self):
        _, _, intent, runtime = self.parts()
        for change in ({"carry_in_attempt_ids": ("b", "a")}, {"carry_in_attempt_ids": ("a", "a")},
                       {"carry_in_attempt_ids": tuple(f"a{i:02d}" for i in range(21))}, {"scope_sha256": None},
                       {"extra": "unknown"}, {"planned_seconds": True}):
            value = intent.model_dump()
            value.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                M6RunIntent.model_validate(value)
        missing_worker = runtime.model_copy(update={"worker_profile_sha256": None})
        with self.assertRaises(ValueError):
            build_run_spec(anchor=owner.anchor_for(self.parts()[0]), intent=intent, runtime=missing_worker)

    def test_campaign_budgets_do_not_create_an_unbounded_bootstrap(self):
        good = self.campaign()
        self.assertEqual(good.run.planned_seconds + good.close_grace_seconds, 90)
        for override in ({"bootstrap_bind_seconds": 91}, {"runner_stop_reserve_seconds": 60},
                         {"close_grace_seconds": 0}, {"ready_wait_seconds": True},
                         {"runtime": good.runtime.model_copy(update={"worker_profile_sha256": None})}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.campaign(**override)

    def test_approved_sustained_window_fits_original_host_ceiling(self):
        # Pro R20 §5.5 retains the pre-frozen G5 4800 + 2400 second envelope.
        intent = self.parts()[2].model_copy(update={"planned_seconds": 4800})
        actual = self.campaign(run=intent, close_grace_seconds=2400, verifier_deadline_seconds=7200)
        self.assertEqual(actual.run.planned_seconds + actual.close_grace_seconds, 7200)
        with self.assertRaises(ValueError):
            self.campaign(run=intent, close_grace_seconds=2401)
