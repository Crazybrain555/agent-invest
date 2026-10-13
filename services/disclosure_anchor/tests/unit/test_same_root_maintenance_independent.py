"""Independent same-root tests using real registry bytes and disposable roots.

The fixture intentionally contains expired tombstones, non-key-sorted records,
and optional-member variation. Maintenance must preserve those facts exactly.
These tests do not attest Windows NTFS/bind durability or operate any service.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.windows.mineru_heap_trim_compat import agent_task_protocol_v2 as protocol
from scripts.windows import mineru_same_root_registry_helper as helper


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _root_identity(root: Path) -> dict[str, Any]:
    metadata = root.stat()
    return {
        "path": str(root), "device": metadata.st_dev, "inode": metadata.st_ino,
        "uid": metadata.st_uid, "mode": metadata.st_mode,
    }


def _decode(raw: bytes, identity: dict[str, Any], *, quiescent: bool) -> dict[str, Any]:
    # Exercise the actual reader without its normal constructor's recovery or
    # expired-tombstone purge; that constructor is forbidden for maintenance.
    reader = object.__new__(protocol.DurableTaskRegistry)
    reader._output_root = Path(identity["path"])
    reader._output_root_identity = tuple(identity[name] for name in ("device", "inode", "uid", "mode"))
    reader._storage_policy = object()
    records = reader._decode_registry(raw, require_quiescent=quiescent)
    return {key: asdict(record) for key, record in records.items()}


class _SameRootCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name).resolve()
        self.root = self.scratch / "output"
        self.root.mkdir(mode=0o700)
        self.control = self.root / ".agent-task-protocol-v2"
        self.control.mkdir(mode=0o700)
        self.registry = self.control / "registry.json"
        self.identity = _root_identity(self.root)
        records = []
        for key, task, generation, consumed in (
            ("1700000000." + "f" * 64, "task-zulu", 4, 1),
            ("1700000001." + "a" * 64, "task-alpha", 2, 1720000000.25),
            ("1700000002." + "c" * 64, "task-middle", 8, 1720000001),
        ):
            records.append(asdict(protocol.DurableTaskRecord(
                idempotency_key=key, task_id=task, attempt_identity="attempt-" + task,
                fence_identity="fence-" + task, state="consumed", consumed_at_unix=consumed,
                recovery_generation=generation,
            )))
        records[0].pop("storage")
        records[0].pop("failure_cause")
        records[1].update(result_sha256="1" * 64, result_owner="2" * 64, result_bytes=37)
        records[2].pop("failure_cause")
        self.payload = {
            "schema": "mineru-task-registry.v4", "output_root": self.identity,
            "submission_watermark_bucket": 1900000123, "records": records,
        }
        self.raw = _canonical(self.payload)
        self.registry.write_bytes(self.raw)
        self.registry.chmod(0o600)

    def plan(self) -> tuple[dict[str, Any], bytes, bytes]:
        old = copy.deepcopy(self.identity)
        old["device"] += 17
        old["inode"] += 31
        payload = copy.deepcopy(self.payload)
        payload["output_root"] = old
        r0 = _canonical(payload)
        self.registry.write_bytes(r0)
        return old, r0, self.raw

    def observe(self, old: dict[str, Any], r0: bytes, **overrides: Any) -> dict[str, Any]:
        options = {
            "approved_root": old, "expected_r0_sha256": _digest(r0),
            "expected_r0_bytes": len(r0),
            "expected_body_sha256": protocol.same_root_registry_body_sha256(r0),
        }
        options.update(overrides)
        return protocol.observe_same_root_registry(self.root, **options)

    def apply(self, old: dict[str, Any], r0: bytes, r1: bytes, **overrides: Any) -> dict[str, Any]:
        options = {
            "operation_id": "sr-" + "7" * 32, "approved_root": old,
            "current_root": self.identity, "expected_r0_sha256": _digest(r0),
            "expected_r0_bytes": len(r0),
            "expected_body_sha256": protocol.same_root_registry_body_sha256(r0),
            "expected_r1_sha256": _digest(r1), "expected_r1_bytes": len(r1),
        }
        options.update(overrides)
        return protocol.apply_same_root_registry(self.root, **options)


class StrictReaderConservationTests(_SameRootCase):
    def test_fixture_is_valid_and_readers_preserve_expired_record_and_result_identity(self) -> None:
        for quiescent in (False, True):
            with self.subTest(quiescent=quiescent):
                records = _decode(self.raw, self.identity, quiescent=quiescent)
                self.assertEqual(list(records), [record["idempotency_key"] for record in self.payload["records"]])
                self.assertEqual(records[self.payload["records"][0]["idempotency_key"]]["consumed_at_unix"], 1)
                self.assertEqual(records[self.payload["records"][1]["idempotency_key"]]["result_bytes"], 37)
                self.assertEqual(self.registry.read_bytes(), self.raw)
        proof = protocol.inspect_quiescent_output_root(self.root)["quiescence"]
        self.assertEqual(proof["record_count"], 3)
        self.assertEqual(proof["submission_watermark_bucket"], 1900000123)
        self.assertEqual(proof["registry_sha256"], _digest(self.raw))

    def test_ordinary_reader_rejects_device_only_and_inode_only_drift(self) -> None:
        for fields in (("device",), ("inode",), ("device", "inode")):
            current = copy.deepcopy(self.identity)
            for field in fields:
                current[field] += 1
            for quiescent in (False, True):
                with self.subTest(fields=fields, quiescent=quiescent):
                    with self.assertRaises(protocol.TaskProtocolConflict):
                        _decode(self.raw, current, quiescent=quiescent)
                    self.assertEqual(self.registry.read_bytes(), self.raw)


class SameRootCandidateInvariantTests(_SameRootCase):
    def test_only_permitted_root_members_change_and_every_business_member_survives(self) -> None:
        for fields in (("device",), ("inode",), ("device", "inode"), ()):
            old = copy.deepcopy(self.identity)
            for field in fields:
                old[field] += 1
            payload = copy.deepcopy(self.payload)
            payload["output_root"] = old
            r0 = _canonical(payload)
            with self.subTest(fields=fields), patch.object(
                protocol.DurableTaskRegistry, "__init__", side_effect=AssertionError("normal recovery entered"),
            ):
                candidate = protocol.same_root_reattested_registry(
                    r0, approved_root=old, current_root=self.identity,
                )
                self.assertEqual(candidate, self.raw)
                self.assertEqual(json.loads(candidate)["records"], self.payload["records"])
                self.assertEqual(json.loads(candidate)["submission_watermark_bucket"], 1900000123)
                self.assertEqual(protocol.same_root_registry_body_sha256(r0),
                                 protocol.same_root_registry_body_sha256(candidate))
                if not fields:
                    self.assertEqual(candidate, r0)
                _decode(r0, old, quiescent=True)
                _decode(candidate, self.identity, quiescent=True)
        self.assertEqual(self.registry.read_bytes(), self.raw)

    def test_large_native_identity_integers_are_preserved_without_float_rounding(self) -> None:
        old = copy.deepcopy(self.identity)
        old.update(device=9007199254740993, inode=9223372036854775806)
        current = copy.deepcopy(old)
        current.update(device=9007199254740992, inode=9223372036854775805)
        payload = copy.deepcopy(self.payload)
        payload["output_root"] = old
        candidate = protocol.same_root_reattested_registry(_canonical(payload), approved_root=old, current_root=current)
        self.assertEqual(json.loads(candidate)["output_root"], current)
        self.assertEqual(json.loads(candidate)["records"], payload["records"])

    def test_path_uid_mode_changes_and_ambiguous_identity_values_are_rejected(self) -> None:
        for key, value in (
            ("path", str(self.scratch / "different-root")), ("path", str(self.root) + "/../output"),
            ("device", True), ("inode", -1), ("uid", self.identity["uid"] + 1),
            ("mode", self.identity["mode"] ^ 0o100), ("mode", stat.S_IFREG | 0o600),
        ):
            changed = copy.deepcopy(self.identity)
            changed[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(protocol.SameRootRefused):
                protocol.same_root_reattested_registry(
                    self.raw, approved_root=self.identity, current_root=changed,
                )

    def test_r0_must_validate_under_approved_old_root_before_candidate_is_generated(self) -> None:
        old, r0, _ = self.plan()
        wrong = copy.deepcopy(old)
        wrong["inode"] += 1
        with self.assertRaises(protocol.SameRootRefused):
            protocol.same_root_reattested_registry(r0, approved_root=wrong, current_root=self.identity)
        self.assertEqual(self.registry.read_bytes(), r0)

    def test_registry_schema_canonical_identity_uniqueness_and_resource_families_refuse(self) -> None:
        mutations = (
            ("old-v3", lambda p: p.update(schema="mineru-task-registry.v3")),
            ("old-v2", lambda p: p.update(schema="mineru-task-registry.v2")),
            ("watermark-bool", lambda p: p.update(submission_watermark_bucket=True)),
            ("watermark-underflow", lambda p: p.update(submission_watermark_bucket=-2)),
            ("duplicate-key", lambda p: p["records"][1].update(idempotency_key=p["records"][0]["idempotency_key"])),
            ("duplicate-task", lambda p: p["records"][1].update(task_id=p["records"][0]["task_id"])),
            ("live-state", lambda p: p["records"][0].update(state="processing", consumed_at_unix=None)),
            ("reader", lambda p: p["records"][0].update(active_readers=1)),
            ("reservation", lambda p: p["records"][0].update(reserved_result_bytes=1)),
            ("retained-path", lambda p: p["records"][0].update(result_path="/do-not-read")),
            ("lease", lambda p: p["records"][0].update(lease_until_unix=1720000100)),
            ("payload", lambda p: p["records"][0].update(task_payload={})),
            ("storage", lambda p: p["records"][0].update(storage={})),
            ("foreign-field", lambda p: p["records"][0].update(unknown=0)),
        )
        for label, mutate in mutations:
            payload = copy.deepcopy(self.payload)
            mutate(payload)
            with self.subTest(label=label), self.assertRaises(protocol.SameRootRefused):
                protocol.same_root_reattested_registry(
                    _canonical(payload), approved_root=self.identity, current_root=self.identity,
                )
        for raw in (
            self.raw + b"\n", self.raw[:-1], b" " * (16 * 1024 * 1024 + 1),
            self.raw.replace(b'"records":', b'"records":[],"records":', 1),
            self.raw.replace(b'1900000123', b'NaN', 1),
        ):
            with self.subTest(raw_size=len(raw)), self.assertRaises(protocol.SameRootRefused):
                protocol.same_root_reattested_registry(raw, approved_root=self.identity, current_root=self.identity)


class SameRootObservationAndPublicationTests(_SameRootCase):
    def test_observe_is_read_only_then_apply_has_strict_reader_and_exact_business_body(self) -> None:
        old, r0, r1 = self.plan()
        (self.root / ".agent-ingress-spool").mkdir(mode=0o700)
        before_file = self.registry.stat()
        with patch.object(protocol.DurableTaskRegistry, "__init__", side_effect=AssertionError("normal recovery entered")):
            observation = self.observe(old, r0)
            self.assertEqual(observation["registry_state"], "r0")
            self.assertEqual(observation["outcome"], "reattest")
            self.assertEqual(observation["approved_root"], old)
            self.assertEqual(observation["current_root"], self.identity)
            self.assertEqual(observation["record_count"], 3)
            self.assertEqual(observation["submission_watermark_bucket"], 1900000123)
            self.assertEqual(observation["r1_sha256"], _digest(r1))
            self.assertEqual(self.registry.read_bytes(), r0)
            result = self.apply(old, r0, r1)
        self.assertEqual(result["outcome"], "applied")
        self.assertEqual(self.registry.read_bytes(), r1)
        self.assertNotEqual(self.registry.stat().st_ino, before_file.st_ino)
        self.assertEqual(stat.S_IMODE(self.registry.stat().st_mode), 0o600)
        self.assertEqual(self.registry.stat().st_nlink, 1)
        self.assertEqual(json.loads(r1)["records"], json.loads(r0)["records"])
        _decode(self.registry.read_bytes(), self.identity, quiescent=False)
        self.assertEqual(self.observe(old, r0)["registry_state"], "r1")
        repeat = self.apply(old, r0, r1)
        self.assertEqual(repeat["outcome"], "already_applied")
        self.assertEqual(self.registry.read_bytes(), r1)

    def test_unchanged_tuple_is_observed_as_noop_and_redundant_apply_never_replaces(self) -> None:
        observation = self.observe(self.identity, self.raw)
        self.assertEqual(observation["outcome"], "noop")
        before = self.registry.stat()
        with patch.object(protocol.os, "replace", side_effect=AssertionError("no-op replaced registry")):
            with self.assertRaises(protocol.SameRootRefused):
                self.apply(self.identity, self.raw, self.raw)
        self.assertEqual(self.registry.stat().st_ino, before.st_ino)
        self.assertEqual(self.registry.read_bytes(), self.raw)

    def test_stale_hash_length_body_candidate_and_current_tuple_are_refused_without_replacement(self) -> None:
        old, r0, r1 = self.plan()
        bad_current = copy.deepcopy(self.identity)
        bad_current["inode"] += 9
        for overrides in (
            {"expected_r0_sha256": "sha256:" + "0" * 64}, {"expected_r0_bytes": len(r0) + 1},
            {"expected_body_sha256": "sha256:" + "0" * 64},
            {"expected_r1_sha256": "sha256:" + "0" * 64}, {"expected_r1_bytes": len(r1) + 1},
            {"current_root": bad_current}, {"operation_id": "sr-../escape"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(protocol.SameRootRefused):
                self.apply(old, r0, r1, **overrides)
            self.assertEqual(self.registry.read_bytes(), r0)

    def test_unknown_inventory_and_nonempty_spool_block_the_same_root_writer(self) -> None:
        old, r0, r1 = self.plan()
        for relative in ("foreign-task", ".unknown", ".agent-task-protocol-v2/.foreign.tmp", ".agent-ingress-spool/body"):
            path = self.root / relative
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"keep-foreign-data")
            with self.subTest(relative=relative):
                with self.assertRaises(protocol.TaskProtocolConflict):
                    self.observe(old, r0)
                with self.assertRaises(protocol.TaskProtocolConflict):
                    self.apply(old, r0, r1)
                self.assertEqual(path.read_bytes(), b"keep-foreign-data")
                self.assertEqual(self.registry.read_bytes(), r0)
            path.unlink()

    def test_symlink_hardlink_and_file_mode_do_not_become_maintenance_permission(self) -> None:
        old, r0, r1 = self.plan()
        for kind in ("symlink", "hardlink", "mode"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    target = self.scratch / "real-registry"
                    self.registry.rename(target)
                    self.registry.symlink_to(target)
                elif kind == "hardlink":
                    target = self.scratch / "other-link"
                    os.link(self.registry, target)
                else:
                    self.registry.chmod(0o644)
                with self.assertRaises((protocol.TaskProtocolConflict, OSError)):
                    self.apply(old, r0, r1)
                self.assertEqual(self.registry.read_bytes(), r0)
                if kind == "symlink":
                    self.registry.unlink()
                    target.rename(self.registry)
                elif kind == "hardlink":
                    target.unlink()
                else:
                    self.registry.chmod(0o600)

    def test_old_plan_cannot_replay_after_new_business_bytes_or_another_remount(self) -> None:
        old, r0, r1 = self.plan()
        self.apply(old, r0, r1)
        for change in ("watermark", "remount"):
            payload = json.loads(r1)
            if change == "watermark":
                payload["submission_watermark_bucket"] += 1
            else:
                payload["output_root"]["inode"] += 1
            latest = _canonical(payload)
            self.registry.write_bytes(latest)
            with self.subTest(change=change), self.assertRaises(protocol.SameRootRefused):
                self.apply(old, r0, r1)
            self.assertEqual(self.registry.read_bytes(), latest)

    def test_r1_resume_still_binds_the_original_pair_digest_and_length(self) -> None:
        old, r0, r1 = self.plan()
        self.apply(old, r0, r1)
        for overrides in ({"expected_r0_sha256": "sha256:" + "0" * 64}, {"expected_r0_bytes": len(r0) + 1}):
            for operation in ("observe", "apply"):
                with self.subTest(operation=operation, overrides=overrides):
                    with self.assertRaises(protocol.SameRootRefused):
                        if operation == "observe":
                            self.observe(old, r0, **overrides)
                        else:
                            self.apply(old, r0, r1, **overrides)
                    self.assertEqual(self.registry.read_bytes(), r1)

    def test_missing_or_foreign_registry_stays_unknown_to_the_old_plan(self) -> None:
        old, r0, r1 = self.plan()
        self.registry.unlink()
        with self.assertRaises((protocol.TaskProtocolConflict, OSError)):
            self.apply(old, r0, r1)
        self.assertFalse(self.registry.exists())
        self.registry.write_bytes(b"unowned-byte-state")
        self.registry.chmod(0o600)
        with self.assertRaises(protocol.TaskProtocolConflict):
            self.apply(old, r0, r1)
        self.assertEqual(self.registry.read_bytes(), b"unowned-byte-state")


class SameRootPublicationFaultTests(_SameRootCase):
    def pre_replace_failure(self, phase: str) -> None:
        old, r0, r1 = self.plan()
        actual_open, actual_close = os.open, os.close
        actual_write, actual_fsync, actual_replace = os.write, os.fsync, os.replace
        temp_fds: set[int] = set()
        injected = False

        def fault() -> None:
            nonlocal injected
            injected = True
            raise OSError("independent-maintenance-" + phase)

        def open_file(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            is_temp = str(path) == protocol.same_root_temp_name("sr-" + "7" * 32)
            if is_temp and phase == "create":
                fault()
            fd = actual_open(path, flags, *args, **kwargs)
            if is_temp:
                temp_fds.add(fd)
            return fd

        def write(fd: int, raw: bytes) -> int:
            if fd in temp_fds and phase == "write":
                fault()
            return actual_write(fd, raw)

        def fsync(fd: int) -> None:
            if fd in temp_fds and phase == "file_fsync":
                fault()
            actual_fsync(fd)

        def close(fd: int) -> None:
            was_temp = fd in temp_fds
            temp_fds.discard(fd)
            actual_close(fd)
            if was_temp and phase == "close" and not injected:
                fault()

        def replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
            if phase == "replace":
                fault()
            actual_replace(src, dst, *args, **kwargs)

        with ExitStack() as patches:
            for name, replacement in (("open", open_file), ("write", write), ("fsync", fsync), ("close", close), ("replace", replace)):
                patches.enter_context(patch.object(protocol.os, name, side_effect=replacement))
            with self.assertRaises((protocol.TaskProtocolConflict, OSError)):
                self.apply(old, r0, r1)
        self.assertTrue(injected)
        self.assertEqual(self.registry.read_bytes(), r0)
        # Recovery may clean only this operation's declared temporary file.
        result = self.apply(old, r0, r1)
        self.assertIn(result["outcome"], ("applied", "already_applied"))
        self.assertEqual(self.registry.read_bytes(), r1)

    def test_temp_create_failure_leaves_r0_and_same_operation_can_resume(self) -> None:
        self.pre_replace_failure("create")

    def test_write_failure_leaves_r0_and_same_operation_can_resume(self) -> None:
        self.pre_replace_failure("write")

    def test_file_fsync_failure_leaves_r0_and_same_operation_can_resume(self) -> None:
        self.pre_replace_failure("file_fsync")

    def test_close_failure_leaves_r0_and_same_operation_can_resume(self) -> None:
        self.pre_replace_failure("close")

    def test_replace_failure_leaves_r0_and_same_operation_can_resume(self) -> None:
        self.pre_replace_failure("replace")

    def test_visible_r1_after_parent_fsync_failure_is_unknown_until_reconfirmed(self) -> None:
        old, r0, r1 = self.plan()
        actual_fsync = os.fsync
        failures = 0

        def fsync(fd: int) -> None:
            nonlocal failures
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                failures += 1
                raise OSError("independent-parent-flush-failure")
            actual_fsync(fd)

        with patch.object(protocol.os, "fsync", side_effect=fsync):
            with self.assertRaises(protocol.SameRootOutcomeUnknown) as raised:
                self.apply(old, r0, r1)
        self.assertGreater(failures, 0)
        self.assertEqual(raised.exception.visible, "r1")
        self.assertEqual(self.registry.read_bytes(), r1)
        directory_flushes: list[bool] = []

        def confirm_flush(fd: int) -> None:
            directory_flushes.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            actual_fsync(fd)

        with patch.object(protocol.os, "fsync", side_effect=confirm_flush):
            resumed = self.apply(old, r0, r1)
        self.assertEqual(resumed["outcome"], "already_applied")
        self.assertIn(True, directory_flushes)
        self.assertEqual(self.registry.read_bytes(), r1)

    def test_replace_lost_response_classifies_visible_r1_without_rolling_back(self) -> None:
        old, r0, r1 = self.plan()
        actual_replace = os.replace

        def replace_then_fail(*args: Any, **kwargs: Any) -> None:
            actual_replace(*args, **kwargs)
            raise OSError("independent-lost-replace-response")

        with patch.object(protocol.os, "replace", side_effect=replace_then_fail):
            with self.assertRaises(protocol.SameRootOutcomeUnknown) as raised:
                self.apply(old, r0, r1)
        self.assertEqual(raised.exception.visible, "r1")
        self.assertEqual(self.registry.read_bytes(), r1)
        self.assertEqual(self.apply(old, r0, r1)["outcome"], "already_applied")

    def post_replace_foreign_state(self, visible: str) -> None:
        old, r0, r1 = self.plan()
        actual_fsync = os.fsync
        foreign = json.loads(r1)
        foreign["submission_watermark_bucket"] += 1
        foreign_bytes = _canonical(foreign)
        injected = False

        def intervene(fd: int) -> None:
            nonlocal injected
            actual_fsync(fd)
            if stat.S_ISDIR(os.fstat(fd).st_mode) and not injected:
                if visible == "missing":
                    self.registry.unlink()
                else:
                    self.registry.write_bytes(r0 if visible == "r0" else foreign_bytes)
                injected = True

        with patch.object(protocol.os, "fsync", side_effect=intervene):
            with self.assertRaises(protocol.SameRootOutcomeUnknown) as raised:
                self.apply(old, r0, r1)
        self.assertTrue(injected)
        self.assertEqual(raised.exception.visible, "unreadable" if visible == "missing" else visible)
        if visible == "missing":
            self.assertFalse(self.registry.exists())
        else:
            self.assertEqual(self.registry.read_bytes(), r0 if visible == "r0" else foreign_bytes)

    def test_r0_readback_after_replace_remains_unknown_and_is_not_overwritten(self) -> None:
        self.post_replace_foreign_state("r0")

    def test_other_readback_after_replace_remains_unknown_and_is_not_overwritten(self) -> None:
        self.post_replace_foreign_state("other")

    def test_missing_readback_after_replace_remains_unknown_and_is_not_recreated(self) -> None:
        self.post_replace_foreign_state("missing")

    def test_business_write_between_temp_creation_and_cas_is_never_overwritten(self) -> None:
        old, r0, r1 = self.plan()
        actual_open = os.open
        newer = json.loads(r0)
        newer["submission_watermark_bucket"] += 1
        newer_raw = _canonical(newer)
        raced = False

        def race(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            nonlocal raced
            fd = actual_open(path, flags, *args, **kwargs)
            if str(path) == protocol.same_root_temp_name("sr-" + "7" * 32):
                self.registry.write_bytes(newer_raw)
                raced = True
            return fd

        with patch.object(protocol.os, "open", side_effect=race):
            with self.assertRaises((protocol.SameRootRefused, protocol.SameRootOutcomeUnknown)):
                self.apply(old, r0, r1)
        self.assertTrue(raced)
        self.assertEqual(self.registry.read_bytes(), newer_raw)

    def test_same_bytes_on_a_replaced_registry_inode_during_publish_are_rejected(self) -> None:
        old, r0, r1 = self.plan()
        actual_open = os.open
        raced = False

        def race(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            nonlocal raced
            fd = actual_open(path, flags, *args, **kwargs)
            if str(path) == protocol.same_root_temp_name("sr-" + "7" * 32):
                replacement = self.scratch / "replacement"
                replacement.write_bytes(r0)
                replacement.chmod(0o600)
                os.replace(replacement, self.registry)
                raced = True
            return fd

        with patch.object(protocol.os, "open", side_effect=race):
            with self.assertRaises((protocol.SameRootRefused, protocol.SameRootOutcomeUnknown)):
                self.apply(old, r0, r1)
        self.assertTrue(raced)
        self.assertEqual(self.registry.read_bytes(), r0)

    def test_foreign_operation_temp_is_not_deleted_or_adopted(self) -> None:
        old, r0, r1 = self.plan()
        foreign = self.control / protocol.same_root_temp_name("sr-" + "8" * 32)
        foreign.write_bytes(r1)
        foreign.chmod(0o600)
        with self.assertRaises(protocol.TaskProtocolConflict):
            self.apply(old, r0, r1)
        self.assertEqual(foreign.read_bytes(), r1)
        self.assertEqual(self.registry.read_bytes(), r0)

    def assert_detachment_does_not_publish(self, component: str) -> None:
        old, r0, r1 = self.plan()
        actual_open = os.open
        detached = self.scratch / ("detached-" + component)
        raced = False

        def race(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            nonlocal raced
            fd = actual_open(path, flags, *args, **kwargs)
            if str(path) == protocol.same_root_temp_name("sr-" + "7" * 32):
                target = self.root if component == "root" else self.control
                target.rename(detached)
                target.mkdir(mode=0o700)
                if component == "root":
                    self.control.mkdir(mode=0o700)
                self.registry.write_bytes(r0)
                self.registry.chmod(0o600)
                raced = True
            return fd

        with patch.object(protocol.os, "open", side_effect=race):
            with self.assertRaises((protocol.TaskProtocolConflict, OSError)):
                self.apply(old, r0, r1)
        self.assertTrue(raced)
        detached_registry = detached / ".agent-task-protocol-v2" / "registry.json" if component == "root" else detached / "registry.json"
        self.assertEqual(self.registry.read_bytes(), r0)
        self.assertEqual(detached_registry.read_bytes(), r0, "published R1 into a detached physical tree")

    def test_root_path_replacement_during_temp_write_refuses_before_publish(self) -> None:
        self.assert_detachment_does_not_publish("root")

    def test_control_path_replacement_during_temp_write_refuses_before_publish(self) -> None:
        self.assert_detachment_does_not_publish("control")

    def test_replaced_temp_inode_is_preserved_when_this_call_refuses(self) -> None:
        old, r0, r1 = self.plan()
        actual_open, actual_fsync = os.open, os.fsync
        temp_fd: int | None = None
        raced = False
        temp = self.control / protocol.same_root_temp_name("sr-" + "7" * 32)
        foreign_bytes = b"foreign-writer-data-must-survive"

        def opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            nonlocal temp_fd
            fd = actual_open(path, flags, *args, **kwargs)
            if str(path) == temp.name:
                temp_fd = fd
            return fd

        def race(fd: int) -> None:
            nonlocal raced
            actual_fsync(fd)
            if fd == temp_fd and not raced:
                foreign = self.scratch / "foreign-temp"
                foreign.write_bytes(foreign_bytes)
                foreign.chmod(0o600)
                os.replace(foreign, temp)
                raced = True

        with patch.object(protocol.os, "open", side_effect=opened), patch.object(protocol.os, "fsync", side_effect=race):
            with self.assertRaises(protocol.TaskProtocolConflict):
                self.apply(old, r0, r1)
        self.assertTrue(raced)
        self.assertEqual(self.registry.read_bytes(), r0)
        self.assertTrue(temp.exists(), "refusal deleted a foreign replacement inode")
        self.assertEqual(temp.read_bytes(), foreign_bytes)


class SameRootHelperBoundaryTests(_SameRootCase):
    def request(self, action: str) -> tuple[dict[str, Any], bytes, bytes]:
        old, r0, r1 = self.plan()
        return {
            "schema": "mineru-same-root-helper-request.v1", "action": action,
            "operation_id": "sr-" + "7" * 32, "root": str(self.root), "approved_root": old,
            "expected_r0_sha256": _digest(r0), "expected_r0_bytes": len(r0),
            "expected_body_sha256": protocol.same_root_registry_body_sha256(r0),
            "current_root": self.identity if action == "apply" else None,
            "expected_r1_sha256": _digest(r1) if action == "apply" else None,
            "expected_r1_bytes": len(r1) if action == "apply" else None, "deadline_seconds": 60,
        }, r0, r1

    def invoke(self, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        path = self.scratch / "helper-request.json"
        path.write_bytes(_canonical(request))
        output = io.StringIO()
        with redirect_stdout(output):
            code = helper.main([str(path)], protocol=protocol)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1, "helper output must be one complete machine-readable line")
        return code, json.loads(lines[0])

    def test_actual_helper_observes_then_applies_without_normal_registry_constructor(self) -> None:
        request, r0, r1 = self.request("observe")
        with patch.object(protocol.DurableTaskRegistry, "__init__", side_effect=AssertionError("normal recovery entered")):
            code, observation = self.invoke(request)
        self.assertEqual(code, 0)
        self.assertEqual(observation["registry_state"], "r0")
        self.assertEqual(self.registry.read_bytes(), r0)
        request.update(action="apply", current_root=self.identity,
                       expected_r1_sha256=_digest(r1), expected_r1_bytes=len(r1))
        code, result = self.invoke(request)
        self.assertEqual(code, 0)
        self.assertEqual(result["outcome"], "applied")
        self.assertEqual(self.registry.read_bytes(), r1)

    def test_packaged_helper_and_protocol_run_with_isolated_python_and_no_api_environment(self) -> None:
        request, r0, _ = self.request("observe")
        package = self.scratch / "packaged-helper"
        package.mkdir()
        entry = package / "mineru_same_root_registry_helper.py"
        shutil.copyfile(helper.__file__, entry)
        shutil.copyfile(protocol.__file__, package / "agent_task_protocol_v2.py")
        request_path = self.scratch / "isolated-request.json"
        request_path.write_bytes(_canonical(request))
        completed = subprocess.run(
            [sys.executable, "-I", str(entry), str(request_path)],
            capture_output=True, check=False, timeout=10, env={"PATH": os.defpath},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(len(completed.stdout.splitlines()), 1)
        result = json.loads(completed.stdout)
        self.assertEqual(result["registry_state"], "r0")
        self.assertEqual(result["record_count"], 3)
        self.assertEqual(self.registry.read_bytes(), r0)
        self.assertFalse((package / "__pycache__").exists())

    def test_apply_precondition_walker_conflict_maps_to_refused_not_uncertain_write(self) -> None:
        request, r0, _ = self.request("apply")
        (self.root / "foreign-task").write_bytes(b"do-not-delete")
        code, failure = self.invoke(request)
        self.assertEqual(code, 65)
        self.assertEqual(failure["kind"], "refused")
        self.assertEqual(failure["operation_id"], request["operation_id"])
        self.assertEqual(self.registry.read_bytes(), r0)
        self.assertEqual((self.root / "foreign-task").read_bytes(), b"do-not-delete")

    def test_invalid_observe_operation_identity_cannot_obtain_a_success_receipt(self) -> None:
        request, r0, _ = self.request("observe")
        request["operation_id"] = "../../unowned-operation"
        code, failure = self.invoke(request)
        self.assertIn(code, (64, 65))
        self.assertIn(failure["kind"], ("invalid", "refused"))
        self.assertEqual(self.registry.read_bytes(), r0)

    def test_invalid_request_deadline_and_extra_fields_never_reach_a_write(self) -> None:
        request, r0, _ = self.request("apply")
        for changes in ({"deadline_seconds": 0}, {"deadline_seconds": True}, {"deadline_seconds": 601}, {"unknown": True}):
            changed = dict(request, **changes)
            with self.subTest(changes=changes):
                code, failure = self.invoke(changed)
                self.assertEqual(code, 64)
                self.assertEqual(failure["kind"], "invalid")
                self.assertEqual(self.registry.read_bytes(), r0)

    def test_helper_reports_visible_r1_as_unknown_on_failed_parent_flush(self) -> None:
        request, _, r1 = self.request("apply")
        actual_fsync = os.fsync

        def fail_directory_flush(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("independent-helper-parent-flush")
            actual_fsync(fd)

        with patch.object(protocol.os, "fsync", side_effect=fail_directory_flush):
            code, failure = self.invoke(request)
        self.assertEqual(code, 70)
        self.assertEqual(failure["kind"], "unknown")
        self.assertEqual(failure["visible"], "r1")
        self.assertEqual(failure["operation_id"], request["operation_id"])
        self.assertEqual(self.registry.read_bytes(), r1)
