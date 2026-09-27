"""Independent closed-contract tests for Mac stage resource grants.

All records are synthetic in-memory values. The existing V4 fixture provides
the immutable H0/evidence baseline; no persistence, provider, or parser runs.
"""

from __future__ import annotations

from dataclasses import replace
import json
import unittest

from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    TERMINAL_RECEIPT_V5_CONTRACT,
    TerminalResultStorageV1,
    decode_remote_parse_evidence_v4,
    encode_remote_parse_evidence_v4,
    validate_durable_remote_parse_evidence_bundle_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    MATERIALIZATION_INTENT_V5_CONTRACT,
    advance_remote_parse_checkpoint_v4,
    build_materialization_intent_v4,
    build_stage_resource_grant_v1,
    decode_materialization_intent_v4,
    decode_remote_parse_checkpoint_v4,
    decode_resource_reservation_v4,
    materialization_grant_limits_v4,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    ResourceCreditVector,
    STAGED_RESOURCE_CREDIT_POLICY_V2,
    STAGED_RESOURCE_CREDIT_POLICY_V3,
)
from tests.unit.test_remote_parse_evidence_v4 import _typed_happy_bundle
from tests.unit.test_remote_parse_lifecycle_v4 import SHA_E, SHA_F


def _canonical(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _v5_bundle():
    _, reservation, values, _, _, _, _, _, history = _typed_happy_bundle()
    old_intent = values[5]
    terminal = replace(
        values[4],
        contract_version=TERMINAL_RECEIPT_V5_CONTRACT,
        result_storage=TerminalResultStorageV1(
            policy_sha256=SHA_F,
            selected_bytes=30,
            member_count=3,
            inventory_sha256=SHA_E,
        ),
    )
    remote_terminal = advance_remote_parse_checkpoint_v4(
        history[2], state="remote_terminal",
        held_resource_credit=history[3].held_resource_credit,
        terminal_receipt_sha256=terminal.sha256,
    )
    grant = build_stage_resource_grant_v1(
        reservation=reservation,
        terminal_receipt_sha256=terminal.sha256,
        storage_policy_sha256=terminal.result_storage.policy_sha256,
        inventory_sha256=terminal.result_storage.inventory_sha256,
        artifact_byte_count=terminal.artifact_byte_count,
        selected_bytes=terminal.result_storage.selected_bytes,
        member_count=terminal.result_storage.member_count,
        decode_working_set_bytes=40,
        decode_input_limit_bytes=30,
    )
    intent = build_materialization_intent_v4(
        reservation=reservation,
        source_checkpoint=remote_terminal,
        terminal_receipt_sha256=terminal.sha256,
        remote_task_identity=terminal.remote_task_identity,
        artifact_owner_identity=terminal.result_owner_identity,
        artifact_sha256=terminal.artifact_sha256,
        artifact_byte_count=terminal.artifact_byte_count,
        provider_envelope_context=old_intent.provider_envelope_context,
        allowance_sha256=old_intent.allowance_sha256,
        provider_capability_kind=old_intent.provider_capability_kind,
        provider_capability_sha256=old_intent.provider_capability_sha256,
        provider_capability_byte_count=old_intent.provider_capability_byte_count,
        output_dir_name=old_intent.output_dir_name,
        provider_envelope_relpath=old_intent.provider_envelope_relpath,
        output_manifest_relpath=old_intent.output_manifest_relpath,
        member_count_limit=3,
        uncompressed_byte_limit=30,
        resource_grant=grant,
    )
    materializing = advance_remote_parse_checkpoint_v4(
        remote_terminal, state="materializing",
        held_resource_credit=intent.held_resource_credit,
        materialization_intent_sha256=intent.sha256,
    )
    return reservation, (*values[:4], terminal, intent), (
        *history[:3], remote_terminal, materializing,
    )


class StageResourceGrantIndependentTests(unittest.TestCase):
    def test_legacy_h0_and_v4_evidence_bytes_remain_pinned(self) -> None:
        _, reservation, values, _, _, _, _, _, history = _typed_happy_bundle()
        for label, value, expected_sha in (
            ("H0 reservation", reservation, "sha256:15c66e93e6cf83b4f4b3b243b5bc679e400cbb300f3d2215a29c4e6b71da381e"),
            ("H0 prepared", history[0], "sha256:90d1edf522648ed2a944c6cf2071599f7f3cc2de8a6bd1063ca813dd488bdd0d"),
            ("v4 terminal", values[4], "sha256:b5f3e3df57598bfdfd590ee0edcb04895a71c15a1d18aa3878644e9c296f1f6b"),
            ("v4 intent", values[5], "sha256:754d5e98d6625ef8de27b20e64ff4d6d05aa98fc75624a3ecfba05d343a142e6"),
        ):
            with self.subTest(label=label):
                self.assertEqual(value.sha256, expected_sha)
        self.assertEqual(decode_resource_reservation_v4(reservation.canonical_bytes), reservation)
        self.assertEqual(decode_remote_parse_checkpoint_v4(history[0].canonical_bytes), history[0])
        self.assertEqual(decode_materialization_intent_v4(values[5].canonical_bytes), values[5])
        self.assertEqual(
            decode_remote_parse_evidence_v4("terminal_receipt", values[4].canonical_bytes).value,
            values[4],
        )
        self.assertNotIn(b"resource_grant", values[5].canonical_bytes)
        self.assertNotIn(b"result_storage", values[4].canonical_bytes)
        self.assertNotEqual(
            STAGED_RESOURCE_CREDIT_POLICY_V2.sha256,
            STAGED_RESOURCE_CREDIT_POLICY_V3.sha256,
        )

    def test_synthetic_v5_grant_round_trip_and_z_s_w_accounting(self) -> None:
        reservation, values, _ = _v5_bundle()
        terminal, intent = values[4:6]
        grant = intent.resource_grant
        self.assertEqual(intent.contract_version, MATERIALIZATION_INTENT_V5_CONTRACT)
        self.assertEqual(grant.reservation_sha256, reservation.sha256)
        self.assertEqual(grant.terminal_receipt_sha256, terminal.sha256)
        self.assertEqual(grant.storage_policy_sha256, terminal.result_storage.policy_sha256)
        self.assertEqual(grant.inventory_sha256, terminal.result_storage.inventory_sha256)
        self.assertEqual(decode_materialization_intent_v4(intent.canonical_bytes), intent)
        self.assertEqual(
            decode_remote_parse_evidence_v4("terminal_receipt", terminal.canonical_bytes).value,
            terminal,
        )
        exact = materialization_grant_limits_v4(
            ResourceCreditVector(), artifact_byte_count=20,
            selected_bytes=30, decode_working_set_bytes=40,
        )
        self.assertEqual(exact.provider_result_bytes, 20)
        self.assertEqual(exact.compressed_bytes, 20)
        self.assertEqual(exact.decoded_bytes, 40)
        self.assertEqual(exact.temp_disk_bytes, 90)  # Z + S + W
        self.assertEqual(exact.output_bytes, 70)  # S + W
        self.assertTrue(exact.fits(grant.limits))

    def test_cross_attempt_fence_reservation_or_terminal_grant_is_rejected(self) -> None:
        _, values, _ = _v5_bundle()
        intent = values[5]
        grant = intent.resource_grant
        for changed in (
            replace(grant, attempt_id="other-attempt"),
            replace(grant, fence_identity="other-fence"),
            replace(grant, reservation_sha256="sha256:" + "a" * 64),
            replace(grant, terminal_receipt_sha256="sha256:" + "b" * 64),
            replace(grant, artifact_byte_count=19),
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                replace(intent, resource_grant=changed)

    def test_int64_aggregate_and_closed_version_decoder_boundaries(self) -> None:
        with self.assertRaises(ValueError):
            materialization_grant_limits_v4(
                ResourceCreditVector(), artifact_byte_count=(1 << 63) - 1,
                selected_bytes=1, decode_working_set_bytes=1,
            )
        _, values, _ = _v5_bundle()
        terminal, intent = values[4:6]
        for mutate in (
            lambda p: p.update(extra_field=1),
            lambda p: p["resource_grant"].update(extra_field=1),
            lambda p: p["resource_grant"]["limits"].update(extra_field=1),
            lambda p: p.update(contract_version="remote-parse-materialization-intent.v4"),
            lambda p: p.pop("resource_grant"),
        ):
            payload = json.loads(intent.canonical_bytes)
            mutate(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                decode_materialization_intent_v4(_canonical(payload))
        for mutate in (
            lambda p: p["result_storage"].update(extra_field=1),
            lambda p: p.update(contract_version="remote-terminal-receipt.v4"),
        ):
            payload = json.loads(terminal.canonical_bytes)
            mutate(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                decode_remote_parse_evidence_v4("terminal_receipt", _canonical(payload))

    def test_replay_rejects_grant_basis_different_from_terminal_storage(self) -> None:
        reservation, values, history = _v5_bundle()
        terminal, intent = values[4:6]
        evidence = tuple(encode_remote_parse_evidence_v4(value) for value in values)
        validate_durable_remote_parse_evidence_bundle_v4(
            checkpoint=history[-1], evidence=evidence, reservation=reservation,
            resourceful_checkpoint_history=history,
        )
        for label, changed in (
            ("policy", replace(intent.resource_grant, storage_policy_sha256="sha256:" + "a" * 64)),
            ("inventory", replace(intent.resource_grant, inventory_sha256="sha256:" + "b" * 64)),
            ("selected_bytes", replace(intent.resource_grant, selected_bytes=31)),
            ("member_count", replace(intent.resource_grant, member_count=4)),
        ):
            with self.subTest(field=label):
                # Reclose hashes/checkpoint around the forged grant. A mere
                # hash mismatch is not the tested contract boundary.
                forged_intent = replace(
                    intent, resource_grant=changed,
                    uncompressed_byte_limit=changed.selected_bytes,
                    decoded_byte_limit=changed.selected_bytes,
                    member_count_limit=changed.member_count,
                )
                forged_head = replace(
                    history[-1], materialization_intent_sha256=forged_intent.sha256,
                )
                forged_evidence = tuple(encode_remote_parse_evidence_v4(value)
                    for value in (*values[:5], forged_intent))
                with self.assertRaises(ValueError):
                    validate_durable_remote_parse_evidence_bundle_v4(
                        checkpoint=forged_head, evidence=forged_evidence,
                        reservation=reservation,
                        resourceful_checkpoint_history=(*history[:-1], forged_head),
                    )
