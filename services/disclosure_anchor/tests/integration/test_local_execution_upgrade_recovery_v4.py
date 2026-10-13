"""Independent cross-version acceptance: the pre-F5 worker's duties recovered by the new code.

Pro §C1-§C6 and root's mandatory acceptance, on the managed scratch database. Cross-version, not
the same candidate booted again:

* the parent Q0 is a historical qualification whose runtime manifest pins a historical writer
  (the authored synthetic parent by default; the byte-exact production parent only when root sets
  ``F5_UPGRADE_ACTUAL_Q0_ROOT``, which stays outside Git);
* the legacy duties are written under that parent's exact R0/P0/WP0 through the real ingress and
  backend stages (``tests.integration._f5_upgrade_legacy_fixture``): 3 prepared, 6 submitted
  (3 complete while the worker is down), 2 remote_terminal, 1 local_materialized with 2 of its 3
  semantic groups cached (the F5 shape), 2 publish_committed and 1 ack_pending tail whose sources
  are gone. More submitted duties are added when the parent's P needs them, so the accepted duties
  always exceed both the old grant's 8 and P (12 for the synthetic P=6, 15 for the actual P=14).
  One neighbour is published and acked; one ordinary document waits;
* the new code runs with its recomputed W1. Only the MinerU HTTP API (``FleetProvider``), the
  model port and the live stream-pressure samples are fake; qualification, composition, DB,
  receipts, materialization, publication, semantic cache, resolver, POST guard, stream admission
  and the new-H0 hold are real.

Boots:

1. parent configuration without U01: refused by the real checker (writer drift) before any DB;
2. current R1/P1/A1 configuration without U01: refused before any DB;
   the U01 artifacts are then built with the product builders and each is checked independently;
   the read-only preflight is ready with the root-verified key lifetime, and unverified without it;
3. U01: the in-worker recheck and a create-only boot receipt, then an operator interruption after two
   legacy ACKs;
4. U01 again: recovery runs to closure; only then is the waiting document admitted, under R1;
5. U01 with the closed scope and an unresolved R1 head: the restart recovers it on the exact path.

A separate case ingests one extra old-profile duty after capture: the preflight blocks and the
resident worker is a public stop (``execution_upgrade_scope_failed``, exit 78) before any claim,
POST or model call.

Run only through the managed scratch runner (``tests/integration/_runner.py``), never with
``--real-mineru``. The runtime identity gate pins the production database and app login; the
managed runner uses its provisioning login in a disposable database. This fixture substitutes a
strict check of that runner's exact database name and managed marker at each identity gate. It
does not claim to test the production-role guard (covered independently), and cannot use production.
The positive boots follow ``run_resident_worker``/``_run_loop`` with the product's own
functions (checker, singleton, stop gate, in-worker recheck, ``_run_staged_v4_resident``, which
writes the boot receipt), without the reports/maintenance planes; the negative case runs the full
resident entry.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, redirect_stdout
import copy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import stat
import threading
import time
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

import httpx
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from disclosure_anchor.adapters.db.postgres import connection as db_connection
from disclosure_anchor.adapters.db.postgres import staged_upgrade_scope_v4 as scope_repository
from disclosure_anchor.adapters.db.postgres.unit_of_work import unit_of_work_factory
from disclosure_anchor.adapters.parsers.mineru_medium.http_remote_v4 import MinerUHttpRemoteV4
from disclosure_anchor.adapters.runtime import mineru_deployment_gate as gate
from disclosure_anchor.adapters.runtime import mineru_execution_upgrade as upgrade_runtime
from disclosure_anchor.adapters.runtime import mineru_stream_worker, staged_worker_v4
from disclosure_anchor.adapters.runtime.mineru_process_profile import load_mineru_process_profile
from disclosure_anchor.adapters.runtime.mineru_stream_activation import load_mineru_stream_activation
from disclosure_anchor.adapters.runtime.worker_stop_control import RuntimeWorkerStopControl, WorkerControlStore
from disclosure_anchor.adapters.storage.path_builder import FileStorePathBuilder
from disclosure_anchor.application.contracts.staged_resource_credit import ResourceCreditVector
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    HeavyWorkPermitForms,
    decode_execution_release_manifest,
    decode_execution_upgrade,
    decode_legacy_scope_inventory,
    decode_local_execution_upgrade,
    encode_local_execution_upgrade,
    encode_local_execution_upgrade_v3,
)
from disclosure_anchor.application.ports.mineru_stream_pressure import StreamPressureSample
from disclosure_anchor.application.services.mineru_stream_policy import (
    MineruStreamPolicy,
    StreamAdmissionControl,
    StreamPolicyConfig,
)
from disclosure_anchor.cli import execution_upgrade as upgrade_cli
from disclosure_anchor.cli import worker as worker_cli
from disclosure_anchor.domain import ids
from disclosure_anchor.settings import Settings
from tests._f5_upgrade_duty_fixture import member_payload
from tests._f5_upgrade_q0_fixture import (
    ParentQ0,
    actual_parent_q0,
    legacy_writer_digest,
    sha256_bytes,
    synthetic_parent_q0,
)
from tests._f5_upgrade_u01_fixture import (
    ARCHIVE_MEMBER_COUNT_LIMIT,
    GPU_METRICS_URL,
    REVIEW_CONTRACT,
    current_worker_profile,
    exact_json,
    frozen_wall_clock,
    interpreter_package_identity,
    release_scope_files,
    runtime_identity,
    write_private,
)
from tests.integration._f5_upgrade_legacy_fixture import (
    SEMANTIC_PROVIDER_ID,
    TARGETS,
    FleetProvider,
    LegacyDocument,
    PreF5Worker,
    create_document,
    semantic_runtime,
)
from tests.integration._support import engine_or_skip, is_managed_test_database_identity
# Module imports: importing a TestCase class by name would re-run its tests here.
from tests.integration import test_f5_operational_stop_three_boot_independent as three_boot
from tests.integration import test_staged_v4_end_to_end as end_to_end
from tests.unit.test_semantic_execution_guard import _Provider


FINAL_STATES = ("acked", "remote_failed", "local_failed", "pre_submission_failed", "preparation_failed",
                "superseded")
SOURCE_REVISION = "scratch-independent-acceptance"
DURABLE_TABLES = (
    "remote_parse_attempt", "remote_parse_v4_execution_spec", "remote_parse_v4_evidence",
    "remote_parse_v4_checkpoint", "remote_parse_v4_supersession_link", "atomic_publication_winner_v4",
    "remote_parse_v4_secret", "durable_publish_base",
)
_ZERO_CREDIT = ResourceCreditVector()
_EVIDENCE = "sha256:" + hashlib.sha256(b"independent healthy pressure").hexdigest()


class _BootBoundaryInterrupt(KeyboardInterrupt):
    """Fixture-only operator interruption, caught separately from a real user interrupt."""


class _HealthyPressure:
    """Fresh, healthy samples for the activation's own runtime and owner (never network)."""

    def __init__(self, config: StreamPolicyConfig) -> None:
        self.config = config
        self.sequence = itertools.count(1)

    def latest(self) -> StreamPressureSample:
        return StreamPressureSample(
            sequence=next(self.sequence), observed_monotonic=time.monotonic(),
            runtime_identity_sha256=self.config.runtime_identity_sha256,
            owner_identity_sha256=self.config.owner_identity_sha256, evidence_sha256=_EVIDENCE,
            gpu_free_bytes=self.config.gpu_recover_bytes * 8, host_available_bytes=self.config.host_recover_bytes * 8,
            http_active=0, http_pending=0,
        )


class _RecordingStreamControl(StreamAdmissionControl):
    """The real admission control; it refuses any POST whose runtime is not the policy's (R1)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.runtimes: list[str] = []

    def assert_submission_allowed(self, *, runtime_identity_sha256: str) -> None:
        self.runtimes.append(runtime_identity_sha256)
        super().assert_submission_allowed(runtime_identity_sha256=runtime_identity_sha256)


def _settings(base: Settings, **overrides: object) -> Settings:
    return Settings(**dict(base.model_dump(), **overrides))


class LocalExecutionUpgradeRecoveryTests(unittest.TestCase):
    parent_factory: Callable[[unittest.TestCase], ParentQ0] = staticmethod(synthetic_parent_q0)

    def setUp(self) -> None:
        self.engine = engine_or_skip()
        self.addCleanup(self.engine.dispose)
        for module in (db_connection, scope_repository, worker_cli):
            identity = patch.object(module, "require_runtime_app_connection", self._require_scratch_connection)
            identity.start()
            self.addCleanup(identity.stop)
        with self.engine.connect() as conn:
            existing = conn.execute(sa.text(
                "SELECT count(*) FROM disclosure_ops.remote_parse_attempt WHERE checkpoint_contract_version=4"
            )).scalar_one()
        self.assertEqual(existing, 0, "the legacy inventory is global: the scratch DB must start with no V4 head")
        self.parent = type(self).parent_factory(self)
        parent = self.parent
        self.root = parent.root
        self.database_url = self.engine.url.render_as_string(hide_password=False)
        self.runtime_root = parent.settings.disclosure_runtime_root
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.runtime_root.chmod(0o700)
        keyring = write_private(self.root / "keyring.json", json.dumps({
            "format": "disclosure-v4-secret-keyring.v1", "primary_kek_id": "fixture", "keks": {"fixture": "11" * 32},
        }).encode())
        self.old_settings = _settings(
            parent.settings, database_url=self.database_url, disclosure_v4_secret_keyring_file=keyring,
            disclosure_gpu_metrics_url=GPU_METRICS_URL,
            disclosure_mineru_stream_pressure_config=parent.activation_path,
            disclosure_mineru_stream_pressure_config_sha256=parent.file_sha256(parent.activation_path),
        )
        self.old_environment = {
            "DISCLOSURE_V4_PROCESS_PROFILE_FILE": str(parent.process_profile_path),
            "DISCLOSURE_V4_PROCESS_PROFILE_SHA256": parent.process_profile.sha256,
            "DISCLOSURE_V4_ARCHIVE_MEMBER_COUNT_LIMIT": str(ARCHIVE_MEMBER_COUNT_LIMIT),
        }
        self.paths = FileStorePathBuilder(self.old_settings)
        self.paths.data_path(Path()).mkdir(parents=True, exist_ok=True)
        self.company_id, self.security_id = "co_" + ids.new_ulid(), "sec_" + ids.new_ulid()
        with self.engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO disclosure_core.company (company_id,legal_name) "
                                 "VALUES (:company,'F5 Local Upgrade Fixture')"), {"company": self.company_id})
            conn.execute(sa.text("INSERT INTO disclosure_core.security (security_id,company_id,security_code,"
                                 "exchange) VALUES (:security,:company,'000001','SZSE')"),
                         {"security": self.security_id, "company": self.company_id})
        self.document_ids: list[str] = []
        self.fleet = FleetProvider()
        self.failing = [False]
        self.model = _Provider(SEMANTIC_PROVIDER_ID, decide=three_boot._decide(self.failing))
        self.worker = PreF5Worker(engine=self.engine, settings=self.old_settings, fleet=self.fleet,
                                  adapter=self.model, parent=parent)
        self.addCleanup(self.worker.close)
        self.remotes: list[MinerUHttpRemoteV4] = []
        self.remote_kwargs: list[dict[str, Any]] = []
        self.stream_controls: list[_RecordingStreamControl] = []
        self.addCleanup(self._cleanup_rows)

    def _require_scratch_connection(self, connection: Any) -> db_connection.RuntimeDatabaseIdentity:
        identity = db_connection.inspect_runtime_database_identity(connection)
        marker = connection.execute(sa.text(
            "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=current_database()"
        )).scalar_one()
        if (identity.database_name != self.engine.url.database
                or not is_managed_test_database_identity(identity.database_name, marker)):
            raise AssertionError("the recovery fixture refuses any connection outside its managed scratch DB")
        return identity

    def _cleanup_rows(self) -> None:
        for remote in self.remotes:
            remote.close()
        with self.engine.begin() as conn:
            conn.exec_driver_sql("TRUNCATE TABLE disclosure_ops.remote_parse_attempt CASCADE")
            for document_id in self.document_ids:
                for table in ("disclosure_ops.durable_publish_base", "disclosure_ops.outbox_event",
                              "disclosure_core.document_unit"):
                    conn.execute(sa.text(f"DELETE FROM {table} WHERE document_id=:doc"), {"doc": document_id})
                conn.execute(sa.text("UPDATE disclosure_core.document SET current_processing_run_id=NULL "
                                     "WHERE document_id=:doc"), {"doc": document_id})
                conn.execute(sa.text("DELETE FROM disclosure_core.processing_run WHERE document_id=:doc"),
                             {"doc": document_id})
                conn.execute(sa.text("DELETE FROM disclosure_core.document WHERE document_id=:doc"),
                             {"doc": document_id})
            conn.execute(sa.text("DELETE FROM disclosure_core.security WHERE security_id=:s"), {"s": self.security_id})
            conn.execute(sa.text("DELETE FROM disclosure_core.company WHERE company_id=:c"), {"c": self.company_id})

    # -- seeding: the old worker ----------------------------------------------------------------

    def _document(self, label: str, target: str, index: int, *, sectioned: bool = False) -> LegacyDocument:
        document = create_document(
            self.engine, self.paths, security_id=self.security_id, label=label, target=target,
            width=100 + index, height=100 + 2 * index,
            title=three_boot._TITLE if sectioned else None, filing_type=three_boot._FILING_TYPE if sectioned else None,
        )
        artifact = (three_boot._sectioned_result_zip(document.source_sha256) if sectioned
                    else end_to_end._official_result_zip(document.source_sha256))
        self.fleet.register(document.source_sha256, document.source, artifact,
                            complete_on_accept=target == "prepared" or label == "waiting-new")
        self.document_ids.append(document.document_id)
        return document

    def _plan(self) -> tuple[tuple[str, int], ...]:
        """TARGETS, with enough submitted duties that the accepted ones exceed 8 and P."""

        others = sum(count for state, count in TARGETS if state not in {"prepared", "submitted"})
        submitted = max(dict(TARGETS)["submitted"], self.parent.process_profile.api_max_pending_tasks + 1 - others)
        return tuple((state, submitted if state == "submitted" else count) for state, count in TARGETS)

    def _seed(self, plan: tuple[tuple[str, int], ...]) -> dict[str, list[tuple[LegacyDocument, str]]]:
        """Drive every planned duty with the old worker; returns target -> [(document, attempt)]."""

        seeded: dict[str, list[tuple[LegacyDocument, str]]] = {}
        index = itertools.count()
        for target, count in plan:
            for number in range(count):
                sectioned = target == "local_materialized"
                document = self._document(f"{target}-{number}", target, next(index), sectioned=sectioned)
                work = self.worker.admit(document, security_id=self.security_id)
                work = self.worker.drive(work, target, fleet=self.fleet)
                if sectioned:
                    # The production F5 shape: two groups adjudicated and cached, the third
                    # fails closed; the attempt stays local_materialized with no residue.
                    self.failing[0] = True
                    with self.assertRaises(Exception):
                        self.worker.backend.commit(work, credit_allowance=self.worker.limits.credits,
                                                   stage_guard=self.worker.guard())
                    self.failing[0] = False
                seeded.setdefault(target, []).append((document, work.attempt_id))
        return seeded

    def _neighbour(self) -> LegacyDocument:
        document = self._document("neighbour", "acked", 90)
        work = self.worker.drive(self.worker.admit(document, security_id=self.security_id), "ack_pending",
                                 fleet=self.fleet)
        final = self.worker.backend.acknowledge(work, stage_guard=self.worker.guard())
        self.assertEqual(final.state, "acked")
        return document

    def _expire_claims(self) -> None:
        """The previous process is gone: its claim leases are expired, nothing else changes."""

        with self.engine.begin() as conn:
            conn.execute(sa.text("UPDATE disclosure_ops.remote_parse_attempt SET claim_lease_until=:expired "
                                 "WHERE is_current AND claim_owner_identity IS NOT NULL"),
                         {"expired": datetime.now(UTC) - timedelta(seconds=1)})

    # -- observation ---------------------------------------------------------------------------

    def _durable_rows(self) -> dict[str, list[tuple[Any, ...]]]:
        with self.engine.connect() as conn:
            return {table: [tuple(row) for row in conn.execute(sa.text(
                f"SELECT * FROM disclosure_ops.{table} ORDER BY 1"))] for table in DURABLE_TABLES}

    def _tree(self) -> dict[str, str]:
        tree: dict[str, str] = {}
        for base in (self.runtime_root, self.paths.data_path(Path())):
            for path in sorted(base.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    tree[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return tree

    def _observation(self) -> tuple[Any, ...]:
        return (self._durable_rows(), self._tree(), self.fleet.http_call_count(), self.model.calls)

    def _authority(self, attempt_id: str) -> Any:
        with unit_of_work_factory(self.engine)() as uow:
            return uow.remote_parse_v4.load(attempt_id)

    def _states(self, attempt_ids: tuple[str, ...]) -> dict[str, tuple[str, bool]]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT attempt_id,state,is_current FROM disclosure_ops.remote_parse_attempt "
                "WHERE attempt_id=ANY(:ids)"), {"ids": list(attempt_ids)}).all()
        return {row[0]: (row[1], row[2]) for row in rows}

    def _attempts_of(self, document_id: str) -> list[tuple[str, str, bool]]:
        with self.engine.connect() as conn:
            return [tuple(row) for row in conn.execute(sa.text(
                "SELECT attempt_id,state,is_current FROM disclosure_ops.remote_parse_attempt "
                "WHERE document_id=:doc ORDER BY attempt_id"), {"doc": document_id})]

    def _receipts(self) -> list[Path]:
        directory = self.runtime_root / "reports" / "execution-boot"
        return sorted(directory.glob("*.json")) if directory.exists() else []

    def _cache_entries(self) -> list[Path]:
        cache = self.runtime_root / "cache" / "semantic_routes"
        return sorted(path for path in cache.rglob("*") if path.is_file()) if cache.exists() else []

    # -- the U01 artifacts, built by the product builders and checked independently -----------

    @contextmanager
    def _gate_ports(self) -> Iterator[None]:
        with (
            patch.object(gate, "client_bundle_identity", return_value=self.parent.client),
            patch.object(upgrade_runtime, "client_bundle_identity", return_value=self.parent.client),
            frozen_wall_clock(self.parent.clock),
        ):
            yield

    def _build_upgrade(self, directory: Path) -> SimpleNamespace:
        parent = self.parent
        directory.mkdir(mode=0o700)
        release_path, inventory_path = directory / "release-E1.json", directory / "inventory.json"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(upgrade_cli.main([
                "release-manifest", "--source-revision", SOURCE_REVISION, "--output", str(release_path)]), 0)
            with patch.dict(os.environ, {"DATABASE_URL": self.database_url}):
                self.assertEqual(upgrade_cli.main(["legacy-scope", "--output", str(inventory_path)]), 0)
        for path in (release_path, inventory_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with patch.dict(os.environ, self.old_environment), self._gate_ports():
            derived = upgrade_runtime.derive_local_upgrade(
                self.old_settings, parent_process_profile=parent.process_profile_path,
                parent_activation=parent.activation_path, output_dir=directory / "derived",
            )
        current_profile = load_mineru_process_profile(
            derived.process_profile_file, expected_sha256=derived.process_profile_sha256,
            expected_owner_uid=os.getuid(),
        ).profile
        current_settings = _settings(
            self.old_settings, disclosure_mineru_runtime_bundle_identity_sha256=derived.current_runtime_identity_sha256,
            disclosure_mineru_stream_pressure_config=derived.stream_activation_file,
            disclosure_mineru_stream_pressure_config_sha256=derived.stream_activation_sha256,
        )
        current_environment = dict(
            self.old_environment, DISCLOSURE_V4_PROCESS_PROFILE_FILE=str(derived.process_profile_file),
            DISCLOSURE_V4_PROCESS_PROFILE_SHA256=derived.process_profile_sha256,
        )
        with patch.dict(os.environ, current_environment), self._gate_ports():
            proposal = upgrade_runtime.build_upgrade_proposal(
                current_settings, release_manifest=release_path, runtime_bundle=derived.runtime_bundle_file,
                parent_process_profile=parent.process_profile_path, parent_activation=parent.activation_path,
                inventory=inventory_path, exact_change_manifest_sha256=sha256_bytes(b"scratch exact change"),
                independent_test_evidence_sha256=sha256_bytes(b"scratch test evidence"),
                independent_code_review_sha256=sha256_bytes(b"scratch code review"),
            )
        proposal_path = write_private(directory / "proposal.json", encode_local_execution_upgrade(proposal))
        review_path = write_private(directory / "review.json", exact_json({
            "contract_version": REVIEW_CONTRACT, "verdict": "GO",
            "proposal_sha256": sha256_bytes(proposal_path.read_bytes()),
            "reviewer_reference": "independent-scratch-review", "decision_reference": "independent-scratch-decision",
        }))
        upgrade_settings = _settings(
            current_settings,
            disclosure_worker_execution_upgrade_file=proposal_path,
            disclosure_worker_execution_upgrade_sha256=sha256_bytes(proposal_path.read_bytes()),
            disclosure_worker_execution_upgrade_review_file=review_path,
            disclosure_worker_execution_upgrade_review_sha256=sha256_bytes(review_path.read_bytes()),
        )
        return SimpleNamespace(
            release_path=release_path, inventory_path=inventory_path, derived=derived, proposal_path=proposal_path,
            current_profile=current_profile, current_settings=current_settings,
            current_environment=current_environment, settings=upgrade_settings,
            inventory=decode_legacy_scope_inventory(inventory_path.read_bytes()),
        )

    def _build_v3_permit_upgrade(self, directory: Path) -> SimpleNamespace:
        """Use the existing DB inventory, with old Q0/WPv2 as both anchor and origin."""

        base = self._build_upgrade(directory)
        parent = self.parent
        archived_bytes = b"scratch archived Q0 writer member"
        origin_release = json.loads(base.release_path.read_bytes())
        origin_release.update(source_revision="scratch-archived-q0",
                              writer_code_sha256=parent.historical_writer)
        origin_release["files"][0].update(sha256=sha256_bytes(archived_bytes), bytes=len(archived_bytes))
        origin_release_path = write_private(directory / "origin-release.json", exact_json(origin_release))
        origin_runtime_path = write_private(directory / "origin-runtime.json", exact_json({
            "identity_sha256": parent.runtime_identity, "manifest": parent.manifest,
        }))
        environment = dict(base.current_environment, DISCLOSURE_V4_HEAVY_WORK_PERMITS="2")
        with patch.dict(os.environ, environment), self._gate_ports():
            proposal = upgrade_runtime.build_upgrade_proposal_v2(
                base.current_settings,
                release_manifest=base.release_path, runtime_bundle=base.derived.runtime_bundle_file,
                anchor_process_profile=parent.process_profile_path, anchor_activation=parent.activation_path,
                origin_release_manifest=origin_release_path, origin_runtime_bundle=origin_runtime_path,
                origin_process_profile=parent.process_profile_path, origin_activation=parent.activation_path,
                inventory=base.inventory_path,
                exact_change_manifest_sha256=sha256_bytes(b"scratch v3 exact change"),
                independent_test_evidence_sha256=sha256_bytes(b"scratch v3 test evidence"),
                independent_code_review_sha256=sha256_bytes(b"scratch v3 code review"),
                heavy_work_permits=HeavyWorkPermitForms(anchor=None, origin=None, target=2),
            )
        proposal_path = write_private(directory / "proposal-v3.json", encode_local_execution_upgrade_v3(proposal))
        review_path = write_private(directory / "review-v3.json", exact_json({
            "contract_version": REVIEW_CONTRACT, "verdict": "GO",
            "proposal_sha256": sha256_bytes(proposal_path.read_bytes()),
            "reviewer_reference": "independent-scratch-v3", "decision_reference": "independent-scratch-v3",
        }))
        settings = _settings(
            base.current_settings,
            disclosure_worker_execution_upgrade_file=proposal_path,
            disclosure_worker_execution_upgrade_sha256=sha256_bytes(proposal_path.read_bytes()),
            disclosure_worker_execution_upgrade_review_file=review_path,
            disclosure_worker_execution_upgrade_review_sha256=sha256_bytes(review_path.read_bytes()),
        )
        return SimpleNamespace(**dict(vars(base), proposal_path=proposal_path, review_path=review_path,
                                      current_environment=environment, settings=settings))

    def _check_upgrade_independently(self, built: SimpleNamespace, heads: tuple[str, ...]) -> None:
        parent = self.parent
        release = decode_execution_release_manifest(built.release_path.read_bytes())
        self.assertEqual([{"path": item.path, "sha256": item.sha256, "bytes": item.bytes} for item in release.files],
                         release_scope_files(), "E1 is every file of the declared scope, by an independent scan")
        writer = legacy_writer_digest()
        self.assertEqual((release.writer_code_sha256, release.worker_python_version, release.worker_package_set_sha256),
                         (writer, *interpreter_package_identity()))
        self.assertNotEqual(writer, parent.historical_writer)
        manifest = copy.deepcopy(parent.manifest)
        manifest["client"]["writer_code_sha256"] = writer
        current_runtime = runtime_identity(manifest)
        derived = built.derived
        self.assertEqual(json.loads(derived.runtime_bundle_file.read_bytes()),
                         {"identity_sha256": current_runtime, "manifest": manifest}, "M1 = M0 except the writer")
        self.assertEqual(derived.process_profile_file.read_bytes(),
                         replace(parent.process_profile, runtime_bundle_identity_sha256=current_runtime).exact_bytes,
                         "P1 = P0 except the runtime reference")
        activation = copy.deepcopy(parent.activation)
        activation["runtime_identity_sha256"] = current_runtime
        activation["policy"]["runtime_identity_sha256"] = current_runtime
        self.assertEqual(json.loads(derived.stream_activation_file.read_bytes()), activation,
                         "A1 = A0 except its runtime references")
        members = {item.attempt_id: asdict(item) for item in built.inventory.members}
        self.assertEqual(sorted(members), sorted(heads), "the inventory is every unresolved head, no cap")
        for attempt_id in heads:
            self.assertEqual(members[attempt_id], member_payload(self._authority(attempt_id)))
            self.assertEqual(
                (members[attempt_id]["runtime_epoch_sha256"], members[attempt_id]["process_profile_sha256"],
                 members[attempt_id]["worker_profile_sha256"]),
                (parent.runtime_identity, parent.process_profile.sha256, parent.worker_profile.sha256),
            )
        upgrade = decode_local_execution_upgrade(built.proposal_path.read_bytes())
        canary = json.loads(parent.canary_path.read_bytes())
        heldout = json.loads(parent.heldout_path.read_bytes())
        worker = current_worker_profile(parent, built.current_profile)
        self.assertEqual(
            asdict(upgrade.parent),
            {
                "runtime_identity_sha256": parent.runtime_identity, "writer_code_sha256": parent.historical_writer,
                "smoke_receipt_sha256": parent.file_sha256(parent.smoke_path),
                "canary_cache_sha256": parent.file_sha256(parent.canary_path),
                "validation_receipt_sha256": parent.file_sha256(parent.heldout_path),
                "process_profile_file": str(parent.process_profile_path),
                "process_profile_sha256": parent.process_profile.sha256,
                "worker_profile_sha256": parent.worker_profile.sha256,
                "stream_activation_file": str(parent.activation_path),
                "stream_activation_sha256": parent.file_sha256(parent.activation_path),
                "qualified_at_utc": canary["passed_at_utc"],
                "service_epoch_sha256": heldout["epoch_after"]["receipt"]["service_epoch_sha256"],
            },
        )
        self.assertEqual(
            {name: getattr(upgrade.current, name) for name in (
                "release_manifest_sha256", "source_revision", "writer_code_sha256", "runtime_identity_sha256",
                "process_profile_sha256", "worker_profile_sha256", "capacity_config_sha256",
                "stream_activation_sha256")},
            {
                "release_manifest_sha256": sha256_bytes(built.release_path.read_bytes()),
                "source_revision": SOURCE_REVISION, "writer_code_sha256": writer,
                "runtime_identity_sha256": current_runtime, "process_profile_sha256": built.current_profile.sha256,
                "worker_profile_sha256": worker.sha256, "capacity_config_sha256": parent.capacity_sha256,
                "stream_activation_sha256": sha256_bytes(derived.stream_activation_file.read_bytes()),
            },
        )
        self.assertEqual((upgrade.legacy_scope.inventory_sha256, upgrade.legacy_scope.member_count),
                         (sha256_bytes(built.inventory_path.read_bytes()), len(heads)))

    # -- boots -----------------------------------------------------------------------------------

    def _refused_boot(self, settings: Settings, environment: dict[str, str], pattern: str) -> None:
        """The real resident entry refuses in its static gate: no DB, HTTP, model or file effect."""

        before = self._observation()
        blocked = AssertionError("a refused boot reached the database")
        with (
            patch.dict(os.environ, environment),
            self._gate_ports(),
            patch.object(worker_cli.sqlalchemy, "create_engine", side_effect=blocked),
            patch.object(worker_cli, "create_db_engine", side_effect=blocked),
            patch.object(worker_cli, "_create_worker_db_engine", side_effect=blocked),
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(gate.MinerUDeploymentGateError, pattern),
        ):
            worker_cli.run_resident_worker(settings, progress_output="off")
        self.assertEqual(self._observation(), before)
        self.assertEqual(self._receipts(), [])

    def _remote(self, **kwargs: Any) -> MinerUHttpRemoteV4:
        self.remote_kwargs.append({key: kwargs.get(key) for key in ("allow_task_submission", "legacy_execution")})
        remote = MinerUHttpRemoteV4(transport=httpx.MockTransport(self.fleet), **kwargs)
        self.remotes.append(remote)
        return remote

    @contextmanager
    def _stream_control(self, settings: Settings, *, expected_capacity: Any, wakeup: Callable[[], None]
                        ) -> Iterator[_RecordingStreamControl]:
        activation = load_mineru_stream_activation(
            settings.disclosure_mineru_stream_pressure_config,
            expected_sha256=settings.disclosure_mineru_stream_pressure_config_sha256,
            expected_owner_uid=os.getuid(), expected_capacity=expected_capacity,
            expected_runtime_identity_sha256=settings.disclosure_mineru_runtime_bundle_identity_sha256,
        )
        assert activation is not None
        control = _RecordingStreamControl(MineruStreamPolicy(activation.policy), _HealthyPressure(activation.policy))
        self.stream_controls.append(control)
        stopped = threading.Event()

        def publish_samples() -> None:
            # The real sampler wakes an idle resident when fresh pressure arrives.
            # Keep that event path in the fake; otherwise its 900-second idle wait
            # would outlive the test even though healthy samples are available.
            while not stopped.wait(0.1):
                wakeup()

        publisher = threading.Thread(target=publish_samples)
        publisher.start()
        try:
            yield control
        finally:
            stopped.set()
            publisher.join()

    def _u01_boot(self, built: SimpleNamespace, *, label: str, done: Callable[[], bool],
                  deadline_seconds: float = 300.0) -> dict[str, Any]:
        """``run_resident_worker``'s U01 path: checker, singleton, stop gate, recheck, resident."""

        settings = built.settings
        control = RuntimeWorkerStopControl.for_settings(settings)
        stopping = [False]
        deadline = time.monotonic() + deadline_seconds

        def should_stop() -> bool:
            if control.is_tripped():
                return True
            if done() or time.monotonic() >= deadline:
                stopping[0] = True
                return True
            return False

        def controller_guard() -> None:
            # A graceful admission stop deliberately keeps accepted remote work draining.
            # The provider leaves tasks pending for the next boot, so emulate the real
            # operator interrupt path to revoke local stages without a public fault.
            if done() or time.monotonic() >= deadline:
                stopping[0] = True
                raise _BootBoundaryInterrupt()

        stdout = io.StringIO()
        receipts_before = self._receipts()
        lock_engine = sa.create_engine(self.database_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, built.current_environment))
                with self._gate_ports():
                    checker = gate.MinerUDeploymentChecker(
                        settings, parse_enabled=True, process_profile=built.current_profile,
                        accept_execution_upgrade=True,
                    )
                self.assertEqual(checker.qualification_origin, "compatible_parent")
                execution = checker.verified_execution
                assert execution is not None
                stack.enter_context(patch.object(staged_worker_v4, "MinerUHttpRemoteV4", side_effect=self._remote))
                stack.enter_context(patch.object(
                    staged_worker_v4, "build_semantic_runtime",
                    side_effect=lambda *, settings, paths, artifacts: semantic_runtime(settings, paths, artifacts,
                                                                                       self.model)))
                stack.enter_context(patch.object(mineru_stream_worker, "owned_mineru_stream_control",
                                                 self._stream_control))
                stack.enter_context(redirect_stdout(stdout))
                lock_conn = stack.enter_context(lock_engine.connect())
                self.assertTrue(lock_conn.execute(sa.text("SELECT pg_try_advisory_lock(:ns, 0)"),
                                                  {"ns": worker_cli.WORKER_NS}).scalar_one())
                stack.callback(lock_conn.execute, sa.text("SELECT pg_advisory_unlock(:ns, 0)"),
                               {"ns": worker_cli.WORKER_NS})
                self.assertIsNone(worker_cli._refuse_stopped_start(settings))
                scope = worker_cli._recheck_execution_upgrade(self.engine, execution)
                try:
                    worker_cli._run_staged_v4_resident(
                        settings, engine=self.engine,
                        deps=SimpleNamespace(heartbeat=lambda: None, config=SimpleNamespace(process_scope_classes=None)),
                        should_stop=should_stop, ownership_guard=controller_guard, admission_guard=lambda: None,
                        work_available=threading.Event(), prune_tracker=worker_cli._ProjectionPruneTracker(),
                        progress_output="off", expected_capacity=checker.expected_capacity, stop_control=control,
                        operator_stop_requested=lambda: stopping[0], verified_execution=execution, execution_scope=scope,
                    )
                except _BootBoundaryInterrupt:
                    self.assertTrue(done(), f"{label} reached its deadline without its goal")
        finally:
            lock_engine.dispose()
        self.assertLess(time.monotonic(), deadline, f"{label} did not reach its goal within its bound")
        self.assertIsNone(control.first_cause(), f"{label} latched a public stop")
        receipts = [path for path in self._receipts() if path not in receipts_before]
        self.assertEqual(len(receipts), 1, f"{label} wrote exactly one boot receipt")
        return {"execution": execution, "scope": scope, "stdout": stdout.getvalue(), "receipt": receipts[0]}

    def _check_receipt(self, boot: dict[str, Any], built: SimpleNamespace, *, unresolved: int, closed: int,
                       current_heads: int) -> None:
        receipt = boot["receipt"]
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        payload = json.loads(receipt.read_bytes())
        execution = boot["execution"]
        self.assertEqual(
            {key: payload[key] for key in ("contract_version", "qualification_origin", "upgrade_sha256",
                                           "release_manifest_sha256", "runtime_identity_sha256",
                                           "parent_runtime_identity_sha256", "worker_profile_sha256",
                                           "parent_qualified_at_utc", "legacy_member_count")},
            {"contract_version": "worker-execution-boot-receipt.v1", "qualification_origin": "compatible_parent",
             "upgrade_sha256": sha256_bytes(built.proposal_path.read_bytes()),
             "release_manifest_sha256": sha256_bytes(built.release_path.read_bytes()),
             "runtime_identity_sha256": built.derived.current_runtime_identity_sha256,
             "parent_runtime_identity_sha256": self.parent.runtime_identity,
             "worker_profile_sha256": execution.upgrade.current.worker_profile_sha256,
             "parent_qualified_at_utc": json.loads(self.parent.canary_path.read_bytes())["passed_at_utc"],
             "legacy_member_count": len(built.inventory.members)},
        )
        self.assertEqual(
            (payload["scope"]["unresolved_member_count"], payload["scope"]["closed_member_count"],
             payload["scope"]["current_execution_head_count"]),
            (unresolved, closed, current_heads),
        )
        self.assertIn(str(receipt), boot["stdout"], "the boot audit line names its receipt")

    # -- the scenario -----------------------------------------------------------------------------

    def test_pre_f5_duties_recover_on_the_upgraded_worker_across_boots(self) -> None:
        parent = self.parent
        plan = self._plan()
        seeded = self._seed(plan)
        neighbour = self._neighbour()
        waiting = self._document("waiting-new", "new", 95)
        submitted = [attempt for _document, attempt in seeded["submitted"]]
        for attempt in submitted[:3]:
            self.fleet.complete(self.fleet.task_for_attempt(attempt).client_submit_key)
        tail_document, tail_attempt = seeded["ack_pending"][0]
        self.paths.data_path(tail_document.relpath).unlink()  # the published tail's source is legally gone
        self._expire_claims()
        members = tuple(sorted(attempt for pairs in seeded.values() for _document, attempt in pairs))
        prepared = tuple(attempt for _document, attempt in seeded["prepared"])
        accepted = tuple(attempt for attempt in members if attempt not in prepared)
        self.assertEqual((len(members), len(accepted)),
                         (sum(count for _state, count in plan), sum(count for state, count in plan if state != "prepared")))
        self.assertGreater(len(accepted), max(8, parent.process_profile.api_max_pending_tasks))
        total = len(members)
        self.assertEqual(self.model.calls, 3, "two groups adjudicated before the third failed closed")
        self.assertEqual(len(self._cache_entries()), 2)
        seed_posts = Counter(self.fleet.posts)
        self.assertEqual({attempt: self.fleet.posts[self._key(attempt)] for attempt in accepted},
                         dict.fromkeys(accepted, 1))
        self.assertTrue(all(self.fleet.posts[self._key(attempt)] == 0 for attempt in prepared))
        neighbour_before = self._rows_of(neighbour.document_id)

        # Boot 1: the parent configuration on the new code, no U01.
        self._refused_boot(self.old_settings, self.old_environment, "runtime manifest local writer code drifted")

        built = self._build_upgrade(self.root / "u01")
        self._check_upgrade_independently(built, members)
        inventory = {item.attempt_id: item for item in built.inventory.members}

        # Boot 2: the current configuration on the new code, no U01.
        self._refused_boot(built.current_settings, built.current_environment,
                           "MinerU exact runtime identity cannot be verified")

        # The read-only preflight: ready with the root-verified lifetime, never a write.
        rows_before = self._durable_rows()
        report = self._preflight(built, ttl=86400)
        self.assertEqual(self._durable_rows(), rows_before, "the preflight wrote nothing")
        self.assertTrue(report["ready_to_install"], report["blockers"])
        self.assertEqual((report["qualification_origin"], report["parent_qualified_at_utc"]),
                         ("compatible_parent", json.loads(parent.canary_path.read_bytes())["passed_at_utc"]))
        scope = report["legacy_scope"]
        self.assertEqual(
            (scope["unresolved_count"], scope["identity_refusals"], scope["non_member_unresolved_count"],
             scope["unresolved_member_count"], scope["closed_member_count"], scope["staged_prepared_count"]),
            (total, 0, 0, total, 0, 0),
        )
        self.assertEqual((report["prepared_key_status"], report["prepared_key_count"]), ("verified", 3))
        unverified = self._preflight(built, ttl=None)
        self.assertEqual(unverified["prepared_key_status"], "unverified")
        self.assertFalse(unverified["ready_to_install"])
        expired = self._preflight(built, ttl=86400, now=datetime.now(UTC) + timedelta(days=2))
        self.assertEqual((expired["prepared_key_status"], expired["ready_to_install"]), ("expired", False))
        self.assertEqual(self._durable_rows(), rows_before)

        # Boot 3: U01, interrupted by the operator after two legacy ACKs.
        def acked(count: int) -> Callable[[], bool]:
            return lambda: sum(state == "acked" for state, _ in self._states(members).values()) >= count

        boot3 = self._u01_boot(built, label="boot 3", done=acked(2))
        self._check_receipt(boot3, built, unresolved=total, closed=0, current_heads=0)
        self.assertEqual(self._attempts_of(waiting.document_id), [], "no new H0 while obligations are open")
        self._expire_claims()

        # Boot 4: U01 again; the remaining tasks complete; recovery closes, then the new H0.
        for attempt in submitted[3:]:
            self.fleet.complete(self.fleet.task_for_attempt(attempt).client_submit_key)
        violations: list[tuple[int, int]] = []
        watching = threading.Event()
        monitor = threading.Thread(target=self._watch_hold, args=(members, waiting.document_id, violations, watching))
        monitor.start()
        try:
            boot4 = self._u01_boot(built, label="boot 4", done=lambda: self._document_acked(waiting.document_id))
        finally:
            watching.set()
            monitor.join()
        self.assertEqual(violations, [], "a new H0 existed while a legacy obligation was open")
        receipt4 = json.loads(boot4["receipt"].read_bytes())["scope"]
        self.assertEqual(receipt4["unresolved_member_count"] + receipt4["closed_member_count"], total)
        self.assertGreater(receipt4["closed_member_count"], 0)
        self._expire_claims()

        # Boot 5: the scope is closed and an R1 head is unresolved across a restart.
        second = self._document("second-new", "new", 96)
        boot5 = self._u01_boot(built, label="boot 5", done=lambda: self._document_state(second.document_id) == "submitted")
        self._check_receipt(boot5, built, unresolved=0, closed=total, current_heads=0)
        self._expire_claims()
        (second_attempt, _state, _current), = self._attempts_of(second.document_id)
        self.fleet.complete(self._key(second_attempt))
        boot6 = self._u01_boot(built, label="boot 6", done=lambda: self._document_acked(second.document_id))
        self._check_receipt(boot6, built, unresolved=0, closed=total, current_heads=1)

        # Every obligation: same identity, prefix-monotonic history, final with zero credit.
        for attempt in members:
            authority = self._authority(attempt)
            member = asdict(inventory[attempt])
            observed = member_payload(authority)
            fixed = {key: value for key, value in member.items() if not key.startswith(("observed_", "accepted_"))}
            self.assertEqual({key: observed[key] for key in fixed}, fixed, attempt)
            self.assertEqual(authority.checkpoint_history[member["observed_lifecycle_version"]].sha256,
                             member["observed_checkpoint_sha256"])
            self.assertEqual((authority.state, authority.is_current), ("acked", False), attempt)
            self.assertEqual(authority.checkpoint.held_resource_credit, _ZERO_CREDIT, attempt)
        for _target, pairs in seeded.items():
            for document, attempt in pairs:
                self.assertEqual([row[0] for row in self._attempts_of(document.document_id)], [attempt],
                                 "no successor attempt for any legacy document")
                self.assertEqual(self._document_status(document.document_id), "published")

        # Provider calls: accepted 0 POST; prepared exactly 1 original POST; one result/ACK effect.
        for attempt in accepted:
            self.assertEqual(self.fleet.posts[self._key(attempt)], seed_posts[self._key(attempt)], attempt)
        for attempt in prepared:
            key = self._key(attempt)
            self.assertEqual(self.fleet.posts[key], 1, attempt)
            body = self.fleet.post_bodies[key]
            member = inventory[attempt]
            for value in (member.client_submit_key, member.attempt_id, member.fence_identity):
                self.assertIn(value.encode(), body)
            intent = next(item.value for item in self._authority(attempt).evidence if item.kind == "submission_intent")
            self.assertEqual((intent.request_sha256, intent.client_submit_key, intent.runtime_epoch_sha256),
                             (member.request_sha256, member.client_submit_key, parent.runtime_identity))
        self.assertEqual(self.fleet.duplicate_posts, [])
        for task in self.fleet.tasks.values():
            self.assertEqual((self.fleet.result_gets[task.task_id], self.fleet.ack_effects[task.task_id]), (1, 1),
                             task.attempt_id)
            # An operator interruption can land after the remote ACK and before its
            # local commit. Protocol v2 explicitly makes that replay idempotent.
            self.assertIn(self.fleet.acks[task.task_id], (1, 2), task.attempt_id)

        # Stream pressure judged every U01 POST under R1 (3 legacy + 2 new), never R0.
        runtimes = [runtime for control in self.stream_controls for runtime in control.runtimes]
        self.assertEqual(runtimes, [built.derived.current_runtime_identity_sha256] * 5)
        executions = [boot["execution"] for boot in (boot3, boot4, boot5, boot6)]
        self.assertTrue(self.remote_kwargs)
        for item in self.remote_kwargs:
            self.assertTrue(any(item["legacy_execution"] is execution for execution in executions))
            self.assertIs(item["allow_task_submission"], True)

        # The new documents: admitted only after closure, bound to R1/P1/WP1, published once.
        for document in (waiting, second):
            (attempt, state, current), = self._attempts_of(document.document_id)
            spec = self._authority(attempt).execution_spec
            self.assertEqual(
                (state, spec.worker_profile.sha256, spec.process_profile_sha256,
                 spec.prepared_submission.runtime_bundle_identity_sha256),
                ("acked", boot4["execution"].upgrade.current.worker_profile_sha256,
                 built.current_profile.sha256, built.derived.current_runtime_identity_sha256),
            )
            self.assertEqual(self.fleet.posts[self._key(attempt)], 1)

        # The semantic cache is reused: only the formerly failed group calls the model.
        self.assertEqual(self.model.calls, 4)
        self.assertEqual(len(self._cache_entries()), 3)
        self.assertEqual(self._rows_of(neighbour.document_id), neighbour_before, "the neighbour is untouched")
        with self.engine.connect() as conn:
            foreign = conn.execute(sa.text(
                "SELECT count(*) FROM disclosure_ops.remote_parse_attempt WHERE NOT (document_id=ANY(:docs))"),
                {"docs": self.document_ids}).scalar_one()
        self.assertEqual(foreign, 0)
        self.assertEqual(len(self._receipts()), 4)
        self.assertFalse(self.paths.data_path(tail_document.relpath).exists(), "the tail ACKed without its source")
        self.assertEqual(self._states((tail_attempt,))[tail_attempt], ("acked", False))

    def test_v3_implied_two_permits_recovers_old_v2_prepared_and_submitted(self) -> None:
        """Real scratch DB H0/spec and resident recovery across the WPv2 -> WPv3 edge."""

        prepared_doc = self._document("v3-prepared", "prepared", 120)
        submitted_doc = self._document("v3-submitted", "submitted", 121)
        waiting = self._document("waiting-new", "new", 122)
        prepared = self.worker.drive(self.worker.admit(prepared_doc, security_id=self.security_id),
                                     "prepared", fleet=self.fleet)
        submitted = self.worker.drive(self.worker.admit(submitted_doc, security_id=self.security_id),
                                      "submitted", fleet=self.fleet)
        members = (prepared.attempt_id, submitted.attempt_id)
        self.fleet.complete(self.fleet.task_for_attempt(submitted.attempt_id).client_submit_key)
        neighbour = self._neighbour()
        neighbour_before = self._rows_of(neighbour.document_id)
        before = {attempt: self._authority(attempt) for attempt in members}
        for attempt in members:
            authority = before[attempt]
            self.assertEqual(authority.execution_spec.worker_profile.sha256, self.parent.worker_profile.sha256)
            self.assertEqual(authority.execution_spec.worker_profile.contract_version,
                             "staged-worker-composition.v2")
        original_posts = Counter(self.fleet.posts)
        self._expire_claims()
        built = self._build_v3_permit_upgrade(self.root / "u01-v3-permits")
        proposal = decode_execution_upgrade(built.proposal_path.read_bytes())
        self.assertEqual((proposal.contract_version, proposal.heavy_work_permits),
                         ("worker-local-execution-upgrade.v3",
                          HeavyWorkPermitForms(anchor=None, origin=None, target=2)))
        self.assertEqual(proposal.qualification_anchor.worker_profile_sha256,
                         self.parent.worker_profile.sha256)
        self.assertEqual(proposal.recovery_origin.worker_profile_sha256,
                         self.parent.worker_profile.sha256)
        self.assertEqual(self._preflight(built, ttl=86400)["ready_to_install"], True)
        self.assertEqual(self._attempts_of(waiting.document_id), [], "new H0 must await legacy closure")

        observed_limits: list[int] = []
        original_limits = staged_worker_v4.staged_v4_coordinator_limits

        def capture_limits(*args: Any, **kwargs: Any) -> Any:
            limits = original_limits(*args, **kwargs)
            observed_limits.append(limits.heavy_work_permits)
            return limits

        violations: list[tuple[int, int]] = []
        watching = threading.Event()
        monitor = threading.Thread(target=self._watch_hold, args=(members, waiting.document_id,
                                                                  violations, watching))
        monitor.start()
        try:
            with patch.object(staged_worker_v4, "staged_v4_coordinator_limits", side_effect=capture_limits):
                boot = self._u01_boot(built, label="v3 implied to two", done=lambda: self._document_acked(waiting.document_id))
        finally:
            watching.set()
            monitor.join()
        self.assertEqual(violations, [], "new H0 was admitted while an old duty was open")
        self.assertTrue(observed_limits and all(value == 2 for value in observed_limits), observed_limits)
        boot_payload = json.loads(boot["receipt"].read_bytes())
        self.assertEqual((boot_payload["contract_version"], boot_payload["upgrade_contract_version"],
                          boot_payload["origin_heavy_work_permits"], boot_payload["heavy_work_permits"]),
                         (upgrade_runtime.BOOT_RECEIPT_V4_CONTRACT,
                          "worker-local-execution-upgrade.v3", None, 2))
        self.assertEqual(self._rows_of(neighbour.document_id), neighbour_before,
                         "the prior business winner changed")

        for document, attempt in ((prepared_doc, prepared.attempt_id),
                                  (submitted_doc, submitted.attempt_id)):
            after = self._authority(attempt)
            old = before[attempt]
            self.assertEqual((after.attempt_id, after.processing_run_id, after.fence_identity,
                              after.client_submit_key, after.request_sha256, after.runtime_epoch_sha256,
                              after.execution_spec, after.checkpoint_history[0].sha256),
                             (old.attempt_id, old.processing_run_id, old.fence_identity,
                              old.client_submit_key, old.request_sha256, old.runtime_epoch_sha256,
                              old.execution_spec, old.checkpoint_history[0].sha256))
            self.assertEqual([row[0] for row in self._attempts_of(document.document_id)], [attempt])
            self.assertEqual((after.state, after.is_current, self._document_status(document.document_id)),
                             ("acked", False, "published"))
            self.assertIsNotNone(after.publication_winner)
            self.assertEqual((after.publication_winner.attempt_id,
                              after.publication_winner.processing_run_id),
                             (attempt, old.processing_run_id))
            key = old.client_submit_key
            self.assertEqual(self.fleet.posts[key], original_posts[key] + (1 if attempt == prepared.attempt_id else 0))
            task = self.fleet.task_for_attempt(attempt)
            self.assertEqual((self.fleet.result_gets[task.task_id], self.fleet.ack_effects[task.task_id]), (1, 1))
        self.assertEqual(self.fleet.duplicate_posts, [])
        (new_attempt, new_state, _), = self._attempts_of(waiting.document_id)
        new_spec = self._authority(new_attempt).execution_spec
        self.assertEqual((new_state, new_spec.worker_profile.sha256,
                          new_spec.worker_profile.heavy_work_permits),
                         ("acked", proposal.current.worker_profile_sha256, 2))

    def test_an_extra_old_profile_head_after_capture_is_a_public_stop_before_any_effect(self) -> None:
        seeded = self._seed((("prepared", 1), ("submitted", 1)))
        self._expire_claims()
        members = tuple(sorted(attempt for pairs in seeded.values() for _document, attempt in pairs))
        built = self._build_upgrade(self.root / "u01")
        self._check_upgrade_independently(built, members)
        extra = self._document("extra-old-head", "prepared", 50)
        extra_attempt = self.worker.admit(extra, security_id=self.security_id).attempt_id
        self._expire_claims()

        report = self._preflight(built, ttl=86400)
        self.assertFalse(report["ready_to_install"])
        self.assertEqual(report["legacy_scope"]["non_member_unresolved_count"], 1)
        self.assertIn(extra_attempt, report["legacy_scope"]["non_member_unresolved"])
        self.assertTrue(any(extra_attempt in item for item in report["blockers"]), report["blockers"])

        before = self._observation()
        control = RuntimeWorkerStopControl.for_settings(built.settings)
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, built.current_environment),
            self._gate_ports(),
            redirect_stdout(io.StringIO()),
            patch("sys.stderr", stderr),
        ):
            status = worker_cli.run_resident_worker(built.settings, progress_output="off", stop_control=control)
        self.assertEqual(status, 78)
        cause = control.first_cause()
        assert cause is not None
        self.assertEqual((cause.kind, cause.reason_code), ("startup_fatal", "execution_upgrade_scope_failed"))
        outcome = control.persistence_outcome(timeout=5)
        assert outcome is not None
        self.assertEqual(outcome.marker.status, "written")
        active = WorkerControlStore.for_settings(built.settings).read_active()
        assert active.record is not None
        self.assertEqual(active.record.cause.reason_code, "execution_upgrade_scope_failed")
        rows, tree, http_calls, model_calls = before
        self.assertEqual(self._durable_rows(), rows, "no claim, recovery or write")
        self.assertEqual((self.fleet.http_call_count(), self.model.calls), (http_calls, model_calls))
        self.assertEqual(self._receipts(), [], "refused before any boot receipt")
        control_dir = str(self.runtime_root / "control")

        def outside_control(files: dict[str, str]) -> dict[str, str]:
            return {path: digest for path, digest in files.items() if not path.startswith(control_dir + os.sep)}

        self.assertEqual(outside_control(self._tree()), outside_control(tree), "only the stop control was written")
        self.assertTrue((self.runtime_root / "control" / "worker-circuit-stop.json").exists())

    # -- helpers -----------------------------------------------------------------------------

    def _key(self, attempt_id: str) -> str:
        with self.engine.connect() as conn:
            return str(conn.execute(sa.text(
                "SELECT client_submit_key FROM disclosure_ops.remote_parse_attempt WHERE attempt_id=:a"),
                {"a": attempt_id}).scalar_one())

    def _document_status(self, document_id: str) -> str:
        with self.engine.connect() as conn:
            return str(conn.execute(sa.text("SELECT status FROM disclosure_core.document WHERE document_id=:d"),
                                    {"d": document_id}).scalar_one())

    def _document_state(self, document_id: str) -> str | None:
        attempts = self._attempts_of(document_id)
        return attempts[-1][1] if attempts else None

    def _document_acked(self, document_id: str) -> bool:
        return self._document_state(document_id) == "acked"

    def _rows_of(self, document_id: str) -> dict[str, list[tuple[Any, ...]]]:
        queries = {
            "document": "SELECT * FROM disclosure_core.document WHERE document_id=:d",
            "processing_run": "SELECT * FROM disclosure_core.processing_run WHERE document_id=:d ORDER BY 1",
            "attempt": "SELECT * FROM disclosure_ops.remote_parse_attempt WHERE document_id=:d ORDER BY 1",
            "document_unit": "SELECT count(*) FROM disclosure_core.document_unit WHERE document_id=:d",
            "durable_publish_base": "SELECT * FROM disclosure_ops.durable_publish_base WHERE document_id=:d",
        }
        with self.engine.connect() as conn:
            return {name: [tuple(row) for row in conn.execute(sa.text(query), {"d": document_id})]
                    for name, query in queries.items()}

    def _preflight(self, built: SimpleNamespace, *, ttl: int | None,
                   now: datetime | None = None) -> dict[str, Any]:
        owner = json.loads(built.derived.stream_activation_file.read_bytes())["owner"]
        with patch.dict(os.environ, built.current_environment), self._gate_ports():
            return upgrade_runtime.run_deployment_preflight(
                built.settings, engine_factory=lambda: sa.create_engine(self.database_url, poolclass=NullPool),
                prepared_key_ttl_seconds=ttl, live_owner=lambda _url, _capacity: dict(owner),
                now=now or datetime.now(UTC),
            )

    def _watch_hold(self, members: tuple[str, ...], document_id: str, violations: list[tuple[int, int]],
                    stop: threading.Event) -> None:
        """Sample the invariant: no attempt for the new document while any obligation is open."""

        engine = sa.create_engine(self.database_url, poolclass=NullPool)
        try:
            while not stop.is_set():
                with engine.connect() as conn:
                    new = conn.execute(sa.text(
                        "SELECT count(*) FROM disclosure_ops.remote_parse_attempt WHERE document_id=:d"),
                        {"d": document_id}).scalar_one()
                    still_open = conn.execute(sa.text(
                        "SELECT count(*) FROM disclosure_ops.remote_parse_attempt WHERE attempt_id=ANY(:ids) "
                        "AND (is_current OR NOT (state=ANY(:final)))"),
                        {"ids": list(members), "final": list(FINAL_STATES)}).scalar_one()
                if new and still_open:
                    violations.append((new, still_open))
                stop.wait(0.05)
        finally:
            engine.dispose()


class ActualParentLocalExecutionUpgradeRecoveryTests(LocalExecutionUpgradeRecoveryTests):
    """Root's opt-in: the byte-exact production parent from ``F5_UPGRADE_ACTUAL_Q0_ROOT``."""

    parent_factory = staticmethod(actual_parent_q0)


class ScenarioPreconditionTests(unittest.TestCase):
    """DB-free: the plan really exceeds the old grant and the synthetic P, with every state."""

    def test_the_plan_covers_every_legacy_state_beyond_the_old_grant(self) -> None:
        plan = dict(TARGETS)
        accepted = sum(count for state, count in TARGETS if state != "prepared")
        self.assertEqual(set(plan), {"prepared", "submitted", "remote_terminal", "local_materialized",
                                     "publish_committed", "ack_pending"})
        self.assertEqual(accepted, 12)
        self.assertGreater(accepted, 8)


if __name__ == "__main__":
    unittest.main()
