"""Materializing a storage-managed result under its v5 stage grant.

The official synthetic MinerU bundle and in-memory V4 evidence only: one real
decode after a metadata-only root locate, and the decode envelope enforced
before any mutation, with the verified result kept when it is exceeded.
"""

from __future__ import annotations

from dataclasses import replace
import io
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

from disclosure_anchor.adapters.parsers.mineru_medium.http_staged_v4 import MinerUHttpStagedV4
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    RESULT_STORAGE_POLICY_CONTRACT,
    MineruResultStoragePolicy,
)
from disclosure_anchor.application.contracts.remote_parse_evidence_v4 import (
    TERMINAL_RECEIPT_V5_CONTRACT,
    TerminalResultStorageV1,
    encode_remote_parse_evidence_v4,
)
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import (
    MATERIALIZATION_INTENT_V5_CONTRACT,
    advance_remote_parse_checkpoint_v4,
    build_materialization_intent_v4,
    build_stage_resource_grant_v1,
)
from disclosure_anchor.application.contracts.staged_resource_credit import (
    PerAttemptResourceAllowance,
)
from disclosure_anchor.application.ports.staged_provider_parser import (
    MaterializationCapacityBlockedV4,
    MaterializationHeavyWorkRequiredV4,
    V4EvidenceReplayContext,
)
from tests.unit.test_mineru_http_staged_v4 import (
    _Guard,
    _MaterializeFixture,
    _StepGuard,
    _Transport,
    _claim,
    _materialize_fixture,
    _official_zip,
    _published_test_root,
)

MIB = 1024 * 1024


def _temporary_volume_bytes() -> int:
    """Total bytes of the volume test scratch roots live on (the Mac work volume)."""
    usage = os.statvfs(Path(tempfile.gettempdir()).resolve())
    return usage.f_blocks * usage.f_frsize


def synthetic_storage_policy(**changes: object) -> MineruResultStoragePolicy:
    """A valid storage policy bound to the test volume; SHA, Mac and transfer fields matter here."""
    values: dict[str, object] = dict(
        contract_version=RESULT_STORAGE_POLICY_CONTRACT,
        native_volume_identity="synthetic-native-volume", native_volume_total_bytes=1 << 40,
        native_work_disk_limit_bytes=64 * MIB, native_free_floor_bytes=MIB,
        native_source_pool_bytes=32 * MIB, native_completion_escrow_bytes=16 * MIB,
        native_metadata_reserve_bytes=MIB, native_source_single_limit_bytes=8 * MIB,
        native_growing_producer_limit=1, native_result_hard_limit_bytes=12 * MIB,
        native_normal_unacked_target_bytes=8 * MIB, initial_result_estimate_bytes=2 * MIB,
        native_allocation_unit_bytes=4096, native_file_overhead_bytes=4096, source_pdf_bytes_limit=MIB,
        mac_volume_identity="synthetic-mac-volume", mac_volume_total_bytes=_temporary_volume_bytes(),
        mac_work_disk_limit_bytes=64 * MIB, mac_free_floor_bytes=1024 * MIB,
        mac_normal_output_target_bytes=8 * MIB, mac_decode_input_limit_bytes=MIB,
        mac_decode_working_set_budget_bytes=8 * MIB, mac_decode_expansion_factor=4,
        mac_decode_stage_seconds=300, max_members=64, max_name_bytes=128, max_inventory_bytes=64 * 1024,
        transfer_logical_deadline_seconds=600, progress_window_seconds=10, minimum_progress_bytes=MIB,
    )
    values.update(changes)
    return MineruResultStoragePolicy(**values)  # type: ignore[arg-type]


STORAGE_POLICY = synthetic_storage_policy()
POLICY = STORAGE_POLICY.sha256
INVENTORY = "sha256:" + "c" * 64
_DECODED_SUFFIXES = ("_content_list.json", "_content_list_v2.json", "_middle.json", "_model.json")


def _archive_facts(archive: bytes) -> tuple[int, int, int]:
    """Selected bytes S, member count M and decoded JSON input J of one ZIP."""
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        infos = [info for info in zipped.infolist() if not info.is_dir()]
    selected = sum(info.file_size for info in infos)
    decoded = sum(info.file_size for info in infos if info.filename.endswith(_DECODED_SUFFIXES))
    return selected, len(infos), decoded


def _v5_fixture(archive: bytes, *, working_set: int, input_limit: int,
                policy_sha256: str = POLICY) -> _MaterializeFixture:
    base = _materialize_fixture(archive, output_bytes=4 * 1024 * 1024, temp_disk_bytes=8 * 1024 * 1024,
                                uncompressed_byte_limit=4 * 1024 * 1024)
    selected, members, _ = _archive_facts(archive)
    terminal = replace(
        base.terminal, contract_version=TERMINAL_RECEIPT_V5_CONTRACT,
        result_storage=TerminalResultStorageV1(
            policy_sha256=policy_sha256, selected_bytes=selected, member_count=members,
            inventory_sha256=INVENTORY,
        ),
    )
    prepared, reconciling, submitted, old_terminal, _old_materializing = base.history
    remote_terminal = advance_remote_parse_checkpoint_v4(
        submitted, state="remote_terminal", held_resource_credit=old_terminal.held_resource_credit,
        terminal_receipt_sha256=terminal.sha256,
    )
    grant = build_stage_resource_grant_v1(
        reservation=base.reservation, terminal_receipt_sha256=terminal.sha256,
        storage_policy_sha256=policy_sha256, inventory_sha256=INVENTORY,
        artifact_byte_count=terminal.artifact_byte_count, selected_bytes=selected, member_count=members,
        decode_working_set_bytes=working_set, decode_input_limit_bytes=input_limit,
    )
    allowance = PerAttemptResourceAllowance(
        reservation_input_sha256=base.allowance.reservation_input_sha256,
        reservation_input=base.allowance.reservation_input,
        limits=grant.limits, stage_grant_sha256=grant.sha256,
    )
    old = base.intent
    intent = build_materialization_intent_v4(
        reservation=base.reservation, source_checkpoint=remote_terminal,
        terminal_receipt_sha256=terminal.sha256, remote_task_identity=terminal.remote_task_identity,
        artifact_owner_identity=terminal.result_owner_identity, artifact_sha256=terminal.artifact_sha256,
        artifact_byte_count=terminal.artifact_byte_count,
        provider_envelope_context=old.provider_envelope_context, allowance_sha256=allowance.sha256,
        provider_capability_kind=old.provider_capability_kind,
        provider_capability_sha256=old.provider_capability_sha256,
        provider_capability_byte_count=old.provider_capability_byte_count,
        output_dir_name=old.output_dir_name, provider_envelope_relpath=old.provider_envelope_relpath,
        output_manifest_relpath=old.output_manifest_relpath,
        member_count_limit=old.member_count_limit, uncompressed_byte_limit=old.uncompressed_byte_limit,
        resource_grant=grant,
    )
    materializing = advance_remote_parse_checkpoint_v4(
        remote_terminal, state="materializing", held_resource_credit=intent.held_resource_credit,
        materialization_intent_sha256=intent.sha256,
    )
    values = (*base.evidence_values[:4], terminal, intent)
    history = (prepared, reconciling, submitted, remote_terminal, materializing)
    return replace(
        base, terminal=terminal, intent=intent, allowance=allowance, checkpoint=materializing,
        claim=_claim(materializing), evidence_values=values, history=history,
        replay=V4EvidenceReplayContext(
            evidence=tuple(encode_remote_parse_evidence_v4(value) for value in values),
            reservation=base.reservation, resourceful_checkpoint_history=history,
        ),
    )


class GrantedMaterializationTests(unittest.TestCase):
    def run_materialize(self, fixture: _MaterializeFixture, archive: bytes):  # type: ignore[no-untyped-def]
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "scratch"
        root.mkdir(mode=0o700)
        backend = MinerUHttpStagedV4(
            scratch_root=root, published_root=_published_test_root(root),
            transport=_Transport(archive), clock=lambda: 1.0, storage_policy=STORAGE_POLICY,
        )
        return root, lambda: backend.materialize_v4(**fixture.arguments(), claim_guard=_Guard())

    def test_v5_grant_materializes_with_one_decode_and_grant_limits(self) -> None:
        archive = _official_zip()
        fixture = _v5_fixture(archive, working_set=4 * 1024 * 1024, input_limit=1024 * 1024)
        self.assertEqual(fixture.intent.contract_version, MATERIALIZATION_INTENT_V5_CONTRACT)
        root, materialize = self.run_materialize(fixture, archive)
        from disclosure_anchor.adapters.parsers.mineru_medium import artifacts

        reads = []
        original = artifacts.MinerUMediumArtifactReader.read_pinned

        def counted(reader, tree, *, source_pdf_sha256):  # type: ignore[no-untyped-def]
            reads.append(tree)
            return original(reader, tree, source_pdf_sha256=source_pdf_sha256)

        artifacts.MinerUMediumArtifactReader.read_pinned = counted  # type: ignore[method-assign]
        try:
            materialized = materialize()
        finally:
            artifacts.MinerUMediumArtifactReader.read_pinned = original  # type: ignore[method-assign]
        self.assertEqual(len(reads), 1, "the root is located from metadata; one real decode")
        receipt = materialized.receipt
        self.assertLessEqual(receipt.temporary_disk_peak_byte_count, fixture.intent.temporary_disk_byte_limit)
        self.assertLessEqual(receipt.output_byte_count, fixture.intent.output_byte_limit)
        self.assertTrue((root / fixture.intent.output_relpath).is_dir())

    def test_decode_input_beyond_the_envelope_is_held_with_the_result_kept(self) -> None:
        archive = _official_zip()
        _, _, decoded = _archive_facts(archive)
        fixture = _v5_fixture(archive, working_set=decoded, input_limit=decoded - 1)
        root, materialize = self.run_materialize(fixture, archive)
        with self.assertRaises(MaterializationCapacityBlockedV4) as blocked:
            materialize()
        self.assertEqual(blocked.exception.dimension, "decode_input_bytes")
        self.assertTrue((root / fixture.intent.spool_relpath).is_file())
        self.assertTrue((root / fixture.intent.staging_relpath).is_dir())
        self.assertFalse((root / fixture.intent.output_relpath).exists())

    def test_decode_outputs_beyond_the_working_set_are_held_before_any_mutation(self) -> None:
        archive = _official_zip()
        _, _, decoded = _archive_facts(archive)
        # The input fits; the serialized envelope + manifest + marker do not.
        fixture = _v5_fixture(archive, working_set=decoded, input_limit=decoded)
        root, materialize = self.run_materialize(fixture, archive)
        with self.assertRaises(MaterializationCapacityBlockedV4) as blocked:
            materialize()
        self.assertEqual(blocked.exception.dimension, "decode_output_bytes")
        self.assertTrue((root / fixture.intent.staging_relpath).is_dir())
        self.assertFalse((root / fixture.intent.output_relpath).exists())
        staging = root / fixture.intent.staging_relpath
        self.assertFalse((staging / fixture.intent.provider_envelope_relpath).exists())
        self.assertFalse((staging / fixture.intent.output_manifest_relpath).exists())

    def test_the_whole_decode_waits_for_the_heavy_work_permit_after_a_durable_unpack(self) -> None:
        """A LOCAL stage dispatched without the shared permit stops before any whole-object decode.

        Transfer and unpack are durable; the same stage continues later with
        the permit and decodes once. Replaying a promoted output decodes it
        whole too, so it needs the permit as well.
        """
        archive = _official_zip()
        fixture = _v5_fixture(archive, working_set=4 * 1024 * 1024, input_limit=1024 * 1024)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "scratch"
        root.mkdir(mode=0o700)
        transport = _Transport(archive)
        backend = MinerUHttpStagedV4(
            scratch_root=root, published_root=_published_test_root(root),
            transport=transport, clock=lambda: 1.0, storage_policy=STORAGE_POLICY,
        )
        from disclosure_anchor.adapters.parsers.mineru_medium import artifacts

        reads: list[object] = []
        original = artifacts.MinerUMediumArtifactReader.read_pinned

        def counted(reader, tree, *, source_pdf_sha256):  # type: ignore[no-untyped-def]
            reads.append(tree)
            return original(reader, tree, source_pdf_sha256=source_pdf_sha256)

        def run(permitted: bool | None):  # type: ignore[no-untyped-def]
            guard = _StepGuard()
            guard.heavy_work_permitted = permitted  # type: ignore[attr-defined]
            return backend.materialize_v4(**(fixture.arguments() | {"stage_guard": guard}), claim_guard=_Guard())

        artifacts.MinerUMediumArtifactReader.read_pinned = counted  # type: ignore[method-assign]
        try:
            with self.assertRaises(MaterializationHeavyWorkRequiredV4):
                run(False)
            staging = root / fixture.intent.staging_relpath
            self.assertTrue(staging.is_dir())
            self.assertFalse((staging / fixture.intent.provider_envelope_relpath).exists())
            self.assertFalse((root / fixture.intent.output_relpath).exists())
            self.assertEqual(reads, [])
            materialized = run(True)
            self.assertEqual((len(reads), transport.downloads), (1, 1))
            # A promoted output is replayed by a whole decode: only with the permit.
            with self.assertRaises(MaterializationHeavyWorkRequiredV4):
                run(False)
            self.assertEqual(run(None).receipt, materialized.receipt)
            self.assertEqual((len(reads), transport.downloads), (1, 1))
        finally:
            artifacts.MinerUMediumArtifactReader.read_pinned = original  # type: ignore[method-assign]


if __name__ == "__main__":
    unittest.main()
