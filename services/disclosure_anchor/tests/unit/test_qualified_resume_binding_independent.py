"""Independent negative witnesses for P7 resume grant and approved-key TTL binding.

All objects are synthetic. The actual resolver/preflight methods execute, while
unrelated DB, source-file and evidence-loading seams are replaced in memory.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from types import SimpleNamespace
import unittest
from unittest import mock

from disclosure_anchor.adapters.parsers.mineru_medium.v4_stage_input_resolver import (
    ProductionV4StageInputResolver,
)
from disclosure_anchor.adapters.runtime import mineru_execution_upgrade as upgrade_runtime
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import build_materialization_intent_v4
from tests.unit.test_qualified_runtime_upgrade import qualified_upgrade, verified, sha
from tests.unit.test_stage_resource_grant_independent import _v5_bundle
from disclosure_anchor.application.contracts.worker_execution_upgrade import KeyLookupEvidenceReference


class QualifiedResumeBindingIndependentTests(unittest.TestCase):
    def _resume_inputs(self):
        reservation, values, history = _v5_bundle()
        original_intent = values[5]
        approved = verified()
        original_spec_sha = approved.member(reservation.attempt_id).execution_spec_sha256
        grant = replace(
            original_intent.resource_grant,
            execution_upgrade_sha256=approved.upgrade_sha256,
            execution_spec_sha256=original_spec_sha,
        )
        allowance = ProductionV4StageInputResolver._allowance(reservation, grant)
        intent = build_materialization_intent_v4(
            reservation=reservation,
            source_checkpoint=history[-2],
            terminal_receipt_sha256=values[4].sha256,
            remote_task_identity=original_intent.remote_task_identity,
            artifact_owner_identity=original_intent.artifact_owner_identity,
            artifact_sha256=original_intent.artifact_sha256,
            artifact_byte_count=original_intent.artifact_byte_count,
            provider_envelope_context=original_intent.provider_envelope_context,
            allowance_sha256=allowance.sha256,
            provider_capability_kind=original_intent.provider_capability_kind,
            provider_capability_sha256=original_intent.provider_capability_sha256,
            provider_capability_byte_count=original_intent.provider_capability_byte_count,
            output_dir_name=original_intent.processing_run_id,
            provider_envelope_relpath=original_intent.provider_envelope_relpath,
            output_manifest_relpath=original_intent.output_manifest_relpath,
            member_count_limit=grant.member_count,
            uncompressed_byte_limit=grant.selected_bytes,
            resource_grant=grant,
        )
        context = intent.provider_envelope_context
        bound = SimpleNamespace(
            reservation=reservation,
            spec=SimpleNamespace(
                sha256=original_spec_sha,
                parser_identity=object(),
                parser_options=SimpleNamespace(target_identity=lambda _identity: context.parser_target_identity),
            ),
            facts=SimpleNamespace(
                document=SimpleNamespace(provider=context.provider,
                                         provider_document_id=context.provider_document_id),
                source_pdf_relpath=PurePosixPath(context.source_pdf_relpath),
                parser_artifact_root_relpath=PurePosixPath(context.parser_artifact_root_relpath),
            ),
        )
        authority = SimpleNamespace(
            state="materializing", document_id=intent.document_id,
            processing_run_id=intent.processing_run_id,
        )
        return approved, bound, authority, intent, allowance

    def _actual_allowance(self, execution, bound, authority, intent):
        resolver = object.__new__(ProductionV4StageInputResolver)
        resolver._legacy_execution = execution
        resolver._storage_policy = None  # Policy facts are unchanged in this one-axis witness.
        with (
            mock.patch.object(ProductionV4StageInputResolver, "_bound", return_value=bound),
            mock.patch.object(ProductionV4StageInputResolver, "_require_evidence"),
        ):
            return resolver.materialization_allowance(authority, intent)

    def test_resumed_grant_requires_its_original_approval_and_spec_under_current_verified_object(self) -> None:
        approved, bound, authority, intent, expected = self._resume_inputs()
        self.assertEqual(self._actual_allowance(approved, bound, authority, intent), expected)
        # Same E7 member, H0/spec, result, policy, and persisted grant. Only the
        # live approved proposal identity changes between worker boots.
        base_upgrade = qualified_upgrade()
        newer = verified(replace(
            base_upgrade, basis=replace(base_upgrade.basis, independent_code_review_sha256=sha("b")),
        ))
        self.assertNotEqual(newer.upgrade_sha256, intent.resource_grant.execution_upgrade_sha256)
        self.assertEqual(newer.member(intent.attempt_id).execution_spec_sha256,
                         bound.spec.sha256)
        self.assertEqual(intent.attempt_id, bound.reservation.attempt_id)
        with self.assertRaises(ValueError):
            self._actual_allowance(newer, bound, authority, intent)

    def test_preflight_cannot_use_a_ttl_longer_than_the_approved_original_lookup(self) -> None:
        approved = verified(replace(
            qualified_upgrade(), key_lookups=KeyLookupEvidenceReference(
                evidence_file="/o/lookups.json", evidence_sha256=sha("9"), key_ttl_seconds=120,
            ),
        ))
        now = datetime(2026, 9, 28, 0, 10, tzinfo=UTC)
        submission_epoch = int((now - timedelta(seconds=130)).timestamp())
        head = SimpleNamespace(
            attempt_id=approved.member_attempt_ids[0], state="prepared",
            execution_spec=SimpleNamespace(
                prepared_submission=SimpleNamespace(submission_epoch_unix=submission_epoch),
            ),
        )
        repository = SimpleNamespace(count_staged_prepared_heads=lambda: 1)
        engine = SimpleNamespace(dispose=lambda: None)

        @contextmanager
        def read_only(_engine):
            yield repository, now, {}

        def preflight(supplied_ttl: int):
            report, blockers = {}, []
            with (
                mock.patch.object(upgrade_runtime, "read_only_repository", read_only),
                mock.patch.object(upgrade_runtime, "observe_unresolved_heads", return_value=(head,)),
                mock.patch.object(upgrade_runtime, "_preflight_resolver",
                                  return_value=SimpleNamespace(inspect_frozen_identity=lambda _head: None)),
                mock.patch.object(upgrade_runtime, "verify_legacy_scope",
                                  return_value=SimpleNamespace(to_payload=lambda: {})),
            ):
                upgrade_runtime._preflight_scope(
                    report, blockers, SimpleNamespace(), lambda: engine,
                    SimpleNamespace(), approved, supplied_ttl, now,
                )
            return report, blockers

        exact_report, exact_blockers = preflight(120)
        self.assertEqual(exact_report["prepared_key_status"], "expired")
        self.assertTrue(exact_blockers)
        oversized_report, oversized_blockers = preflight(600)
        self.assertNotEqual(oversized_report.get("prepared_key_status"), "verified")
        self.assertTrue(oversized_blockers)


if __name__ == "__main__":
    unittest.main()
