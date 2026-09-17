"""Independent physical-identity vectors and bounded raw-evidence reads; no host IO."""

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from disclosure_anchor.application.contracts.m6_owner import M6OwnerAnchor, bind_physical_owner_boot
from tests import m6_support as m6


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class PhysicalOwnerBootIndependentTests(unittest.TestCase):
    def setUp(self):
        # Input formulas follow the native protocol, without using the product
        # bridge/epoch implementation to generate the expected answer.
        self.node = m6.digest("independent-windows-node")
        self.utc = "2026-09-15T03:04:05.1234567Z"
        self.pid, self.birth = 4321, 134000000012345678
        self.boot = digest(canonical({"contract_version": "m6.windows-boot-counter.v1",
                                     "windows_node_identity_sha256": self.node, "boot_counter": 7}))
        spec = m6.make_fixture("e2e_publication").spec
        clock = spec.clock.model_copy(update={
            "boot_identity_sha256": self.boot,
            "clock_domain_identity_sha256": digest(canonical({
                "boot_identity_sha256": self.boot, "clock_source": "QueryPerformanceCounter",
                "frequency_hz": m6.QPC_HZ})),
        })
        epoch = digest(canonical({"run_id": spec.run_id,
                                  "owner_source_sha256": spec.runtime.owner_source_sha256,
                                  "boot_identity_sha256": self.boot,
                                  "pid": self.pid, "creation_filetime_100ns": self.birth}))
        self.anchor = M6OwnerAnchor.model_validate({
            **{name: getattr(spec, name) for name in (
                "run_id", "t0_ticks", "planned_seconds", "deadline_ticks", "max_close_ticks", "resources")},
            "clock": clock, "owner_process_epoch_sha256": epoch,
            "owner_source_sha256": spec.runtime.owner_source_sha256,
            "gpu_device_identity_sha256": spec.runtime.gpu_device_identity_sha256,
        })
        self.body = {"contract_version": "m6.physical-owner-identity.v2", "boot_counter": 7,
                     "boot_identity_version": "m6.windows-boot-counter.v1",
                     "clock": clock.model_dump(mode="json"),
                     "creation_filetime_100ns": self.birth, "pid": self.pid,
                     "gpu_device_identity_sha256": spec.runtime.gpu_device_identity_sha256,
                     "windows_boot_utc": self.utc, "windows_node_identity_sha256": self.node}

    def bind(self, body=None, *, raw=None, metadata=None, anchor=None, pid=None, birth=None):
        raw = canonical(self.body if body is None else body) if raw is None else raw
        metadata = canonical({"contract_version": "m6.transport-diagnostic.v1", "code": "owner_identity",
                              "body_sha256": digest(raw)}) if metadata is None else metadata
        return bind_physical_owner_boot(metadata_raw=metadata, body_raw=raw,
                                        anchor=self.anchor if anchor is None else anchor,
                                        pid=self.pid if pid is None else pid,
                                        creation_filetime_100ns=self.birth if birth is None else birth)

    def test_original_seven_digit_utc_is_preserved_across_two_distinct_boot_encodings(self):
        expected = digest(canonical({"windows_node_identity_sha256": self.node, "boot_utc": self.utc}))
        self.assertNotEqual(expected, self.boot)
        self.assertEqual(self.bind(), expected)
        truncated = digest(canonical({"windows_node_identity_sha256": self.node,
                                      "boot_utc": "2026-09-15T03:04:05.123456Z"}))
        self.assertNotEqual(self.bind(), truncated)

    def test_self_consistent_body_hash_does_not_repair_foreign_physical_identity(self):
        for key, value in (
            ("windows_node_identity_sha256", m6.digest("other-node")), ("boot_counter", 8),
            ("gpu_device_identity_sha256", m6.digest("other-gpu")), ("pid", self.pid + 1),
            ("creation_filetime_100ns", self.birth + 1), ("pid", True), ("boot_counter", True),
        ):
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.bind({**self.body, key: value})
        foreign_clock = copy.deepcopy(self.body)
        foreign_clock["clock"]["qpc_frequency_hz"] += 1
        with self.assertRaises(ValueError):
            self.bind(foreign_clock)
        with self.assertRaises(ValueError):
            self.bind(anchor=self.anchor.model_copy(update={"owner_process_epoch_sha256": m6.digest("other-epoch")}))

    def test_metadata_and_original_byte_integrity_are_required(self):
        raw = canonical(self.body)
        for damaged in (raw[:-1], raw + b"\n", b" " + raw):
            with self.subTest(raw=damaged[-12:]), self.assertRaises(ValueError):
                self.bind(raw=damaged)
        for metadata in (
            {"contract_version": "m6.transport-diagnostic.v1", "code": "owner_identity",
             "body_sha256": m6.digest("wrong-body")},
            {"contract_version": "m6.transport-diagnostic.v1", "code": "different", "body_sha256": digest(raw)},
            {"contract_version": "m6.transport-diagnostic.v1", "code": "owner_identity"},
        ):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                self.bind(metadata=canonical(metadata))

    def test_external_pid_birth_and_utc_syntax_are_not_optional(self):
        for kwargs in ({"pid": self.pid + 1}, {"birth": self.birth + 1}, {"pid": False}, {"birth": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.bind(**kwargs)
        for value in ("2026-09-15T03:04:05", "invalidZ", "2026-09-15T03:04:05+08:00"):
            with self.subTest(utc=value), self.assertRaises(ValueError):
                self.bind({**self.body, "windows_boot_utc": value})


class BoundedEvidenceReadIndependentTests(unittest.TestCase):
    def setUp(self):
        from disclosure_anchor.adapters.runtime.m6_campaign_evidence import _Reader
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reader = _Reader(self.root)

    def read(self, path, maximum=8):
        return self.reader.read(path, absent="sample_absent", maximum=maximum)

    def test_boundary_size_hash_and_rejection_are_based_on_actual_bounded_read(self):
        path = self.root / "raw"
        path.write_bytes(b"12345678")
        self.assertEqual(self.read(path), b"12345678")
        self.assertEqual(self.reader.digest(path), digest(b"12345678"))
        path.write_bytes(b"123456789")
        self.assertIsNone(self.read(path))
        self.assertIn("sample_exceeds_byte_bound", self.reader.unknowns)

    def test_symlink_directory_and_fifo_are_not_read_as_ordinary_evidence(self):
        target = self.root / "target"
        target.write_bytes(b"abc")
        link = self.root / "link"
        link.symlink_to(target)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        for path in (link, self.root, fifo):
            with self.subTest(path=path.name):
                self.assertIsNone(self.read(path))
        self.assertEqual(target.read_bytes(), b"abc")

    def test_mutation_during_read_is_visible_even_if_result_fits_the_bound(self):
        from disclosure_anchor.adapters.runtime import m6_campaign_evidence as module
        path = self.root / "raw"
        path.write_bytes(b"1234")
        original = os.fstat
        calls = []

        def changed(fd):
            value = original(fd)
            calls.append(fd)
            return SimpleNamespace(st_mode=value.st_mode, st_size=value.st_size,
                                   st_mtime_ns=value.st_mtime_ns + (1 if len(calls) > 1 else 0))

        with patch.object(module.os, "fstat", side_effect=changed):
            self.assertIsNone(self.read(path))
        self.assertIn("sample_unreadable:OSError", self.reader.unknowns)
        self.assertIsNone(self.reader.digest(path))

    def test_json_nonfinite_and_duplicate_keys_do_not_become_input_facts(self):
        for raw in (b'{"a":NaN}', b'{"a":1,"a":2}'):
            with self.subTest(raw=raw):
                path = self.root / "document"
                path.write_bytes(raw)
                self.assertIsNone(self.reader.document(path, absent="sample_absent", maximum=64))


if __name__ == "__main__":
    unittest.main()
