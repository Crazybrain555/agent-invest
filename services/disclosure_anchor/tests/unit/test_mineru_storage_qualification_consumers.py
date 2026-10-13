"""Storage-runtime diagnostic results through the qualification consumers.

A capacity-config v2 runtime lets one retained diagnostic result reach its
policy's hard limit. The held-out receipt builder, the deployment gate and the
release qualification caller must bound that result only by the configured
capacity's own policy, after the recorded runtime and API health are proven to
belong to that capacity. The receipt topology is the existing Package A fixture;
only capacity, runtime and health identities are rebuilt from the product wire
contract. No PDF, provider, network or database is used.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from disclosure_anchor.adapters.parsers.mineru_medium.protocol_v2_wire import canonical_result_owner_v2
from disclosure_anchor.adapters.runtime import mineru_release_qualification as qualification
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import (
    MinerUDeploymentGateError,
    verify_mineru_heldout_validation,
)
from disclosure_anchor.adapters.runtime.mineru_diagnostic import validate_diagnostic_disposal
from disclosure_anchor.adapters.runtime.mineru_identity import canonical_payload_sha256 as digest
from disclosure_anchor.adapters.runtime.resident_owner_control import OwnerCommandResult
from disclosure_anchor.application.contracts.mineru_api_health import MINERU_API_RESULT_RESERVATION_BYTES
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    CAPACITY_CONFIG_CONTRACT_V2,
    AnyMineruCapacityConfig,
    MineruCapacityConfigV2,
)
from scripts import build_mineru_validation_receipt as builder
from tests._mineru_diagnostic_fixture import diagnostic_disposal_fixture
from tests._mineru_package_a_fixture import NOW, gate_fixture
from tests.unit.test_mineru_materialize_grant_v5 import synthetic_storage_policy


MIB = 1024**2
GIB = 1024**3
LEGACY_LIMIT = MINERU_API_RESULT_RESERVATION_BYTES
# A valid storage policy whose hard result limit lies above the legacy reservation.
LARGE_POLICY = synthetic_storage_policy(
    native_source_single_limit_bytes=256 * MIB, native_result_hard_limit_bytes=512 * MIB,
    native_completion_escrow_bytes=GIB, native_source_pool_bytes=GIB, native_work_disk_limit_bytes=3 * GIB,
    mac_work_disk_limit_bytes=GIB, minimum_progress_bytes=16 * MIB,
)


def _declare_extent(disposal: dict[str, Any], byte_count: int) -> None:
    disposal["terminal_artifact_bytes"] = byte_count
    disposal["terminal_artifact_owner"] = canonical_result_owner_v2(
        task_id=disposal["task_id"], artifact_sha256=disposal["terminal_artifact_sha256"],
        artifact_byte_count=byte_count,
    )


def _storage_health(sample: dict[str, Any], capacity: MineruCapacityConfigV2) -> dict[str, Any]:
    """One idle legacy capacity health sample as the storage runtime reports it."""
    value = deepcopy(sample)
    runtime = value["task_protocol_runtime"]
    for field in ("task_result_reservation_bytes", "max_unacked_result_bytes"):
        del runtime[field]
    runtime.update(
        schema="mineru-task-runtime.v4", registry_schema="mineru-task-registry.v4",
        capacity_config_sha256=capacity.sha256, result_storage_policy_sha256=capacity.result_storage.sha256,
    )
    value["task_admission"]["registry_schema"] = "mineru-task-registry.v4"
    observation = value["capacity_observation"]
    for field in ("result_reservation_bytes", "max_unacked_result_bytes"):
        del observation["resolved_limits"][field]
    observation["stage_counters"].update(source_growth_waiting=0, completion_waiting=0)
    observation.update(
        schema="mineru.capacity-observation.v2", capacity_config_sha256=capacity.sha256,
        result_storage={
            "policy_sha256": capacity.result_storage.sha256, "source_bytes": 0, "ingress_bytes": 0,
            "result_bytes": 0, "growing_producers": 0, "outstanding_promise_bytes": 0,
            "completion_queue_depth": 0, "blocked_tasks": 0,
            "waiting_tasks": {"source_growth_capacity": 0, "completion_capacity": 0, "free_floor": 0},
        },
    )
    return value


class _Receipts:
    """Package A smoke, held-out and epoch receipts, optionally rebound to a storage capacity."""

    def __init__(self, root: Path, *, storage: bool) -> None:
        self.root = root
        settings, _profile, legacy, _client = gate_fixture(root)
        self.observability_url = settings.disclosure_mineru_observability_url
        self.legacy_capacity = legacy
        self.smoke = json.loads(settings.disclosure_mineru_smoke_receipt.read_bytes())
        self.validation = json.loads(settings.disclosure_mineru_validation_receipt.read_bytes())
        self.capacity: AnyMineruCapacityConfig = legacy
        self.manifest = self.smoke["runtime_manifest"]
        if storage:
            payload = json.loads(legacy.exact_bytes)
            for field in ("result_reservation_bytes", "max_unacked_result_bytes"):
                del payload[field]
            capacity = MineruCapacityConfigV2(**{
                **payload, "contract_version": CAPACITY_CONFIG_CONTRACT_V2, "result_storage": LARGE_POLICY,
            })
            self.capacity = capacity
            self.manifest = deepcopy(self.manifest)
            self.manifest["orchestrator"].update(
                capacity_config=json.loads(capacity.exact_bytes), capacity_config_sha256=capacity.sha256,
                task_result_reservation_bytes=None, max_unacked_result_bytes=None,
            )
            identity = digest(self.manifest)
            for receipt in (self.smoke, *self.heldout):
                receipt["runtime_manifest"] = deepcopy(self.manifest)
                receipt["identity"].update(
                    runtime_manifest_identity_sha256=identity,
                    orchestrator_runtime_identity_sha256=digest(self.manifest["orchestrator"]),
                )
                for evidence in (receipt["canary"], receipt["provider"]["target_identity"],
                                 receipt["diagnostic_disposal"]):
                    evidence["runtime_bundle_identity_sha256"] = identity
                for side in ("before", "after"):
                    receipt["orchestrator"][side] = _storage_health(receipt["orchestrator"][side], capacity)
            for which in ("epoch_before", "epoch_after"):
                epoch = self.validation[which]["receipt"]["service_epoch"]
                epoch["runtime_manifest_identity_sha256"] = identity
                self.validation[which]["receipt"]["service_epoch_sha256"] = digest(epoch)
        self.identity = digest(self.manifest)

    @property
    def heldout(self) -> list[dict[str, Any]]:
        return [wrapper["receipt"] for wrapper in self.validation["documents"]]

    def epoch(self, which: str) -> dict[str, Any]:
        return self.validation[which]["receipt"]

    def files(self) -> list[Path]:
        """Rehash every wrapper and write the owner-only builder inputs."""
        paths = []
        for index, wrapper in enumerate(self.validation["documents"]):
            wrapper["receipt_sha256"] = digest(wrapper["receipt"])
            paths.append(_private(self.root / f"heldout-{index}.json", wrapper["receipt"]))
        for which in ("epoch_before", "epoch_after"):
            self.validation[which]["receipt_sha256"] = digest(self.epoch(which))
            _private(self.root / f"{which}.json", self.epoch(which))
        return paths

    def build(self, capacity: AnyMineruCapacityConfig | None) -> dict[str, Any]:
        return builder.build_receipt(
            self.files(), epoch_before_path=self.root / "epoch_before.json",
            epoch_after_path=self.root / "epoch_after.json", expected_capacity=capacity,
        )

    def gate(self) -> int:
        self.files()
        return verify_mineru_heldout_validation(
            self.validation, expected_identity=self.smoke["identity"],
            expected_topology=self.smoke["topology"], expected_runtime_manifest=self.manifest,
            runtime_identity=self.identity, task_slots=self.capacity.parse_active_limit,
            task_retention_seconds=600, cleanup_interval_seconds=30,
            observability_url=self.observability_url, max_age_seconds=3600, current=NOW,
            expected_capacity=self.capacity,
        ).document_count


def _private(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


class StorageQualificationConsumerTests(unittest.TestCase):
    def receipts(self, *, storage: bool) -> _Receipts:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        # The capacity file reader never follows a symlinked parent (macOS /var).
        return _Receipts(Path(directory.name).resolve(), storage=storage)

    def test_disposal_bound_comes_only_from_the_supplied_storage_policy(self) -> None:
        source, runtime, bundle = "sha256:" + "c" * 64, "sha256:" + "b" * 64, "sha256:" + "d" * 64
        proof = diagnostic_disposal_fixture(source=source, runtime=runtime, pages=2, bundle=bundle)
        _declare_extent(proof, LEGACY_LIMIT + 1)

        def validate(policy: object) -> None:
            validate_diagnostic_disposal(
                proof, source_pdf_sha256=source, runtime_identity=runtime, source_page_count=2,
                provider_bundle_sha256=bundle, result_storage_policy=policy,
            )

        validate(LARGE_POLICY)
        for label, policy in (
            ("legacy_reservation", None), ("smaller_policy", synthetic_storage_policy()),
            ("policy_bytes", json.loads(LARGE_POLICY.exact_bytes)), ("policy_digest", LARGE_POLICY.sha256),
        ):
            with self.subTest(label), self.assertRaises(ValueError):
                validate(policy)

    def test_consumers_accept_a_storage_result_above_the_legacy_reservation(self) -> None:
        receipts = self.receipts(storage=True)
        _declare_extent(receipts.heldout[0]["diagnostic_disposal"], LEGACY_LIMIT + 1)
        self.assertEqual(receipts.build(receipts.capacity)["document_count"], 2)
        self.assertEqual(receipts.gate(), 2)

    def test_consumers_refuse_a_storage_result_above_its_policy_hard_limit(self) -> None:
        receipts = self.receipts(storage=True)
        _declare_extent(receipts.heldout[0]["diagnostic_disposal"], LARGE_POLICY.native_result_hard_limit_bytes + 1)
        with self.assertRaises(ValueError):
            receipts.build(receipts.capacity)
        with self.assertRaises(MinerUDeploymentGateError):
            receipts.gate()

    def test_storage_receipts_are_sealed_only_under_their_own_capacity_and_policy(self) -> None:
        receipts = self.receipts(storage=True)
        for label, capacity in (("no_capacity", None), ("other_capacity", receipts.legacy_capacity)):
            with self.subTest(label), self.assertRaises(ValueError):
                receipts.build(capacity)
        health = receipts.heldout[0]["orchestrator"]["after"]["task_protocol_runtime"]
        health["result_storage_policy_sha256"] = "sha256:" + "0" * 64
        with self.subTest("recorded_policy"):
            with self.assertRaises(ValueError):
                receipts.build(receipts.capacity)
            with self.assertRaises(MinerUDeploymentGateError):
                receipts.gate()
        health["result_storage_policy_sha256"] = LARGE_POLICY.sha256
        receipts.heldout[1]["identity"]["runtime_manifest_identity_sha256"] = "sha256:" + "0" * 64
        with self.subTest("manifest_identity"), self.assertRaises(ValueError):
            receipts.build(receipts.capacity)
        paths = receipts.files()
        with self.subTest("unpaired_capacity"), self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
                builder.main([
                    *(item for path in paths for item in ("--smoke-receipt", str(path))),
                    "--epoch-before", str(receipts.root / "epoch_before.json"),
                    "--epoch-after", str(receipts.root / "epoch_after.json"),
                    "--receipt-out", str(receipts.root / "never.json"),
                    "--capacity-config", str(receipts.root / "capacity.json"),
                ])
        self.assertFalse((receipts.root / "never.json").exists())

    def test_legacy_receipts_keep_the_fixed_reservation(self) -> None:
        receipts = self.receipts(storage=False)
        self.assertEqual(receipts.build(receipts.capacity)["document_count"], 2)
        _declare_extent(receipts.heldout[0]["diagnostic_disposal"], LEGACY_LIMIT + 1)
        for label, capacity in (("unbound", None), ("legacy_capacity", receipts.capacity)):
            with self.subTest(label), self.assertRaises(ValueError):
                receipts.build(capacity)
        with self.assertRaises(MinerUDeploymentGateError):
            receipts.gate()

    def test_qualification_seals_heldout_receipts_under_the_release_capacity(self) -> None:
        receipts = self.receipts(storage=True)
        _declare_extent(receipts.heldout[0]["diagnostic_disposal"], LEGACY_LIMIT + 1)
        receipts.files()
        root, capacity = receipts.root, receipts.capacity
        package = root / "package"
        (package / "inputs").mkdir(parents=True)
        capacity_file = package / "inputs" / "capacity-config.json"
        capacity_file.write_bytes(capacity.exact_bytes)
        capacity_file.chmod(0o600)
        bundle = _private(root / "bundle.json", {"identity_sha256": receipts.identity, "manifest": receipts.manifest})
        documents = tuple(
            qualification.HeldoutDocument(label, root / f"{label}.pdf", receipt["input"]["sha256"],
                                          receipt["input"]["page_count"])
            for label, receipt in zip(("heldout-a", "heldout-b"), receipts.heldout, strict=True)
        )
        sources = {"heldout-a": receipts.heldout[0], "heldout-b": receipts.heldout[1]}
        builder_argv: list[str] = []

        class Child:
            """Replays the recorded receipts; the held-out builder step runs for real."""

            def __init__(child, argv: list[str], **_options: object) -> None:
                out = Path(argv[argv.index("--receipt-out") + 1])
                script = Path(argv[2]).name
                stdout = io.StringIO()
                if script == "build_mineru_validation_receipt.py":
                    builder_argv.extend(argv)
                    try:
                        with redirect_stdout(stdout):
                            code = builder.main(argv[3:])
                    except SystemExit as exc:  # the builder's own abort, as its process would exit
                        stdout.write(str(exc.code))
                        code = 1
                elif script == "freeze_mineru_campaign_epoch.py":
                    _private(out, receipts.epoch(out.stem.replace("-", "_")))
                    code = 0
                else:
                    label = Path(argv[argv.index("--input") + 1]).stem if "--input" in argv else None
                    _private(out, receipts.smoke if label is None else sources[label])
                    code = 0
                child.result = OwnerCommandResult(code, stdout.getvalue().encode(), b"")

            def finish(child) -> OwnerCommandResult:
                return child.result

        with patch.object(qualification, "BoundedOwnerCommand", Child):
            summary = qualification.qualify_release(
                report=SimpleNamespace(passed=True, inputs=SimpleNamespace(capacity=capacity),
                                       manifest=SimpleNamespace(sha256="sha256:" + "4" * 64)),
                runtime_bundle=bundle,
                canary=qualification.CanaryManifest(None, documents, "sha256:" + "3" * 64),
                binding=SimpleNamespace(
                    mineru_bin=root / "mineru", api_url="http://127.0.0.1:30002",
                    observability_url=receipts.observability_url, inference_upstream_url="http://engine:30000/v1",
                    ssh_host="192.0.2.1", ssh=SimpleNamespace(username="test", port=22,
                                                              private_key_path="/unused/key",
                                                              known_hosts_path="/unused/known_hosts"),
                ),
                package=package, output=root / "qualification",
            )
        # The storage result above the legacy reservation seals only because the
        # caller hands the builder the release's own capacity.
        self.assertEqual(summary["status"], "pass")
        self.assertEqual(json.loads((root / "qualification" / "validation-final.json").read_bytes())["document_count"], 2)
        self.assertEqual(builder_argv[builder_argv.index("--capacity-config") + 1],
                         str(package / "inputs" / "capacity-config.json"))
        self.assertEqual(builder_argv[builder_argv.index("--capacity-config-sha256") + 1], capacity.sha256)


if __name__ == "__main__":
    unittest.main()
