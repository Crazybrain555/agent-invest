"""Independent activation/file/Settings boundaries, without runtime qualification.

The wire fixture is handwritten. Successful cases execute the real secure reader
against owned temporary files; no GPU, HTTP, worker or database is started.
"""

from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from disclosure_anchor.adapters.runtime import mineru_capacity_file as secure_file
from disclosure_anchor.adapters.runtime import mineru_stream_activation as activation
from disclosure_anchor.application.contracts.mineru_capacity_config import (
    MineruCapacityConfig,
)
from disclosure_anchor.application.services.mineru_stream_policy import MineruStreamPolicy
from disclosure_anchor.settings import load_settings
from tests._mineru_capacity_config_fixture import capacity_payload
from tests.unit.test_settings import _env, _mineru_topology


RUNTIME = "sha256:" + "1" * 64
OTHER = "sha256:" + "9" * 64
OWNER = {
    "process_id": 17,
    "process_start_ticks": 123456,
    "boot_id": "11111111-2222-4333-8444-555555555555",
    "loop_epoch": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _wire(capacity):
    # These values specify the tested public loader interface independently of
    # its private Pydantic classes and the policy implementation's defaults.
    return {
        "schema": "mineru.stream-activation.v1",
        "runtime_identity_sha256": RUNTIME,
        "capacity_config_sha256": capacity.sha256,
        "owner": dict(OWNER),
        "cgroup_identity_sha256": "sha256:" + "3" * 64,
        "cgroup_max_bytes": 32 * 1024**3,
        "gpu_uuid": "GPU-12345678-abcd-4321-9876-abcdef123456",
        "api_max_age_seconds": 2.0,
        "gpu_max_age_seconds": 5.0,
        "policy": {
            "qualified_max": 5,
            "runtime_identity_sha256": RUNTIME,
            "owner_identity_sha256": _sha(_json(OWNER)),
            "gpu_pause_bytes": 256 * 1024**2,
            "gpu_reduce_bytes": 768 * 1024**2,
            "gpu_recover_bytes": 2 * 1024**3,
            "host_pause_bytes": 3 * 1024**3,
            "host_recover_bytes": 7 * 1024**3,
            "sample_max_age_seconds": 5.0,
            "missing_pause_seconds": 11.0,
            "recovery_seconds": 13.0,
            "reduction_interval_seconds": 1.5,
        },
    }


class MineruStreamActivationLoadingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "activation.json"
        self.capacity = MineruCapacityConfig(**capacity_payload(
            parse_active_limit=5, total_nonterminal_limit=6,
            final_http_limit_per_loop=14,
        ))
        self.document = _wire(self.capacity)
        self.write()

    def write(self, document=None, *, raw=None):
        if raw is None:
            raw = _json(self.document if document is None else document)
        self.path.write_bytes(raw)
        self.path.chmod(0o600)
        self.digest = _sha(raw)
        return raw

    def load(self, **changes):
        arguments = {
            "expected_sha256": self.digest,
            "expected_owner_uid": os.getuid(),
            "expected_capacity": self.capacity,
            "expected_runtime_identity_sha256": RUNTIME,
        }
        arguments.update(changes)
        return activation.load_mineru_stream_activation(self.path, **arguments)

    def test_disabled_pair_reads_nothing_and_does_not_require_live_authority(self):
        with patch.object(activation, "read_mineru_capacity_file", side_effect=AssertionError("IO")) as read:
            result = activation.load_mineru_stream_activation(
                None, expected_sha256=None, expected_owner_uid=-1,
                expected_capacity=None, expected_runtime_identity_sha256=None,
            )
        self.assertIsNone(result)
        read.assert_not_called()

    def test_half_configured_pair_is_error_before_file_read(self):
        for path, digest in ((None, self.digest), (self.path, None)):
            with self.subTest(path=path), patch.object(activation, "read_mineru_capacity_file") as read:
                with self.assertRaisesRegex(ValueError, "paired"):
                    activation.load_mineru_stream_activation(
                        path, expected_sha256=digest, expected_owner_uid=os.getuid(),
                        expected_capacity=self.capacity, expected_runtime_identity_sha256=RUNTIME,
                    )
                read.assert_not_called()

    def test_enabled_requires_independently_selected_capacity_and_runtime(self):
        for delta in (
            {"expected_capacity": None},
            {"expected_capacity": self.document},
            {"expected_runtime_identity_sha256": None},
            {"expected_runtime_identity_sha256": "runtime"},
        ):
            with self.subTest(delta=delta), patch.object(activation, "read_mineru_capacity_file") as read:
                with self.assertRaises(ValueError):
                    self.load(**delta)
                read.assert_not_called()

    def test_real_read_keeps_exact_bytes_hash_and_all_explicit_policy_values(self):
        # Valid whitespace is part of file identity, not silently canonicalized.
        raw = json.dumps(self.document, indent=2).encode() + b"\n"
        self.write(raw=raw)
        with patch.object(activation, "read_mineru_capacity_file", wraps=secure_file.read_mineru_capacity_file) as read:
            loaded = self.load()
        read.assert_called_once()
        self.assertIs(type(loaded), activation.LoadedMineruStreamActivation)
        self.assertEqual((loaded.source_path, loaded.source_sha256), (self.path, _sha(raw)))
        self.assertIs(loaded.binding.capacity, self.capacity)
        self.assertEqual(loaded.binding.owner_json, _json(OWNER))
        self.assertEqual(loaded.binding.owner_sha256, self.document["policy"]["owner_identity_sha256"])
        self.assertEqual(loaded.binding.cgroup_max_bytes, 32 * 1024**3)
        self.assertEqual((loaded.binding.api_max_age_seconds, loaded.binding.gpu_max_age_seconds), (2.0, 5.0))
        self.assertEqual(asdict(loaded.policy), self.document["policy"])
        with self.assertRaises(FrozenInstanceError):
            loaded.source_sha256 = OTHER

    def test_declared_capacity_bound_is_not_a_live_qualification_or_pressure_sample(self):
        with patch("threading.Thread.start", side_effect=AssertionError("reader started")):
            loaded = self.load()
        decision = MineruStreamPolicy(loaded.policy).evaluate(None, now=0.0)
        self.assertEqual((decision.target, decision.reason), (0, "pressure_unknown"))
        self.assertEqual(loaded.policy.qualified_max, 5)

    def test_remote_wait_ceiling_spans_n_through_p_without_changing_parse_slots(self):
        # R20: C owns pre-POST/finalize waits too; N remains the API parse bound.
        for ceiling in (1, 4, 5, 6):
            with self.subTest(ceiling=ceiling):
                self.document["policy"]["qualified_max"] = ceiling
                self.write()
                loaded = self.load()
                self.assertEqual(loaded.policy.qualified_max, ceiling)
                self.assertEqual(loaded.binding.capacity.parse_active_limit, 5)
                self.assertEqual(loaded.binding.capacity.total_nonterminal_limit, 6)
                self.assertEqual(
                    MineruStreamPolicy(loaded.policy).evaluate(None, now=0).target, 0,
                )
        for ceiling in (0, -1, 7, True, 6.0, "6"):
            with self.subTest(invalid_ceiling=ceiling):
                self.document["policy"]["qualified_max"] = ceiling
                self.write()
                with self.assertRaises(ValueError):
                    self.load()

    def test_larger_wait_ceiling_preserves_independent_capacity_and_owner_checks(self):
        self.document["policy"]["qualified_max"] = 6
        self.write()
        with self.assertRaisesRegex(ValueError, "capacity differs"):
            self.load(expected_capacity=replace(self.capacity, total_nonterminal_limit=7))
        self.document["owner"]["process_start_ticks"] += 1
        self.write()
        with self.assertRaisesRegex(ValueError, "runtime/owner"):
            self.load()

    def test_capacity_and_runtime_must_match_independent_authority(self):
        for delta in (
            {"expected_runtime_identity_sha256": OTHER},
            {"expected_capacity": replace(self.capacity, final_http_limit_per_loop=20)},
        ):
            with self.subTest(delta=delta), self.assertRaisesRegex(ValueError, "differs"):
                self.load(**delta)

    def test_policy_runtime_and_owner_hash_cannot_choose_another_owner(self):
        for key in ("runtime_identity_sha256", "owner_identity_sha256"):
            value = deepcopy(self.document)
            value["policy"][key] = OTHER
            self.write(value)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "runtime/owner"):
                self.load()
        value = deepcopy(self.document)
        value["owner"]["process_start_ticks"] += 1
        self.write(value)
        with self.assertRaisesRegex(ValueError, "runtime/owner"):
            self.load()

    def test_changed_bytes_wrong_hash_and_wrong_file_owner_are_rejected(self):
        for delta, message in (
            ({"expected_sha256": OTHER}, "hash differs"),
            ({"expected_sha256": "sha256:" + "A" * 64}, "hash is invalid"),
            ({"expected_owner_uid": os.getuid() + 1}, "owner differs"),
            ({"expected_owner_uid": True}, "owner is invalid"),
        ):
            with self.subTest(delta=delta), self.assertRaisesRegex(ValueError, message):
                self.load(**delta)
        self.path.write_bytes(self.path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            self.load()

    def test_missing_file_os_error_is_not_silently_disabled(self):
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.load()

    def test_final_and_parent_symlinks_are_rejected_by_real_nofollow_reads(self):
        original = self.path
        for target, is_parent in ((original, False), (self.root, True)):
            link = self.root / ("parent-link" if is_parent else "file-link")
            link.symlink_to(target, target_is_directory=is_parent)
            self.path = link / "activation.json" if is_parent else link
            with self.subTest(parent=is_parent), self.assertRaises(OSError):
                self.load()
        self.path = original

    def test_relative_and_parent_traversal_paths_are_rejected(self):
        for path in (Path("activation.json"), self.root / ".." / self.root.name / "activation.json"):
            self.path = path
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "path"):
                self.load()

    def test_hardlinked_or_group_world_writable_file_is_rejected(self):
        alias = self.root / "alias.json"
        os.link(self.path, alias)
        with self.assertRaisesRegex(ValueError, "hard link"):
            self.load()
        alias.unlink()
        for mode in (0o620, 0o602):
            self.path.chmod(mode)
            with self.subTest(mode=oct(mode)), self.assertRaisesRegex(ValueError, "writable"):
                self.load()

    def test_empty_oversized_and_nonregular_inputs_fail_without_blocking(self):
        for raw in (b"", b" " * (65536 + 1)):
            self.write(raw=raw)
            with self.subTest(length=len(raw)), self.assertRaisesRegex(ValueError, "envelope"):
                self.load()
        self.path.unlink()
        os.mkfifo(self.path, mode=0o600)
        with self.assertRaisesRegex(ValueError, "regular"):
            self.load()

    def test_real_file_mutation_during_read_is_visible(self):
        original_read = os.read
        changed = False

        def read_and_change(fd, size):
            nonlocal changed
            raw = original_read(fd, size)
            if not changed:
                changed = True
                with self.path.open("ab") as writer:
                    writer.write(b" ")
            return raw

        with patch.object(secure_file.os, "read", side_effect=read_and_change):
            with self.assertRaisesRegex(ValueError, "changed while reading"):
                self.load()
        self.assertTrue(changed)

    def test_closed_wire_requires_explicit_top_level_and_policy_fields(self):
        for key in ("schema", "owner", "cgroup_max_bytes", "gpu_max_age_seconds", "policy"):
            value = deepcopy(self.document)
            del value[key]
            self.write(value)
            with self.subTest(missing=key), self.assertRaises(ValueError):
                self.load()
        for key in ("gpu_pause_bytes", "recovery_seconds", "sample_max_age_seconds"):
            value = deepcopy(self.document)
            del value["policy"][key]
            self.write(value)
            with self.subTest(missing_policy=key), self.assertRaises(ValueError):
                self.load()

    def test_closed_wire_rejects_extra_and_unsupported_version_fields(self):
        variants = []
        for section in (None, "owner", "policy"):
            value = deepcopy(self.document)
            (value if section is None else value[section])["extra"] = 1
            variants.append(value)
        value = deepcopy(self.document)
        value["schema"] = "mineru.stream-activation.v2"
        variants.append(value)
        for value in variants:
            self.write(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load()

    def test_duplicate_nonfinite_and_invalid_utf8_json_are_not_accepted(self):
        valid = _json(self.document)
        variants = (
            b'{"schema":"wrong",' + valid[1:],
            valid.replace(b'"api_max_age_seconds":2.0', b'"api_max_age_seconds":NaN'),
            valid.replace(b'"api_max_age_seconds":2.0', b'"api_max_age_seconds":1e999'),
            valid + b"\xff",
        )
        for raw in variants:
            self.write(raw=raw)
            with self.subTest(raw=raw[:70]), self.assertRaises(ValueError):
                self.load()

    def test_schema_rejects_numeric_coercion_boolean_and_bad_owner_device(self):
        variants = (
            ("api_max_age_seconds", True), ("gpu_max_age_seconds", "5.0"),
            ("cgroup_max_bytes", True), ("gpu_uuid", "GPU-unknown"),
            ("cgroup_identity_sha256", "not-a-hash"),
        )
        for key, invalid in variants:
            value = deepcopy(self.document)
            value[key] = invalid
            self.write(value)
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load()
        for key, invalid in (("process_id", True), ("process_start_ticks", 0), ("boot_id", "bad")):
            value = deepcopy(self.document)
            value["owner"][key] = invalid
            self.write(value)
            with self.subTest(owner=key), self.assertRaises(ValueError):
                self.load()
        value = deepcopy(self.document)
        value["policy"]["qualified_max"] = "5"
        self.write(value)
        with self.assertRaises(ValueError):
            self.load()

    def test_explicit_unlimited_self_cgroup_stays_none(self):
        self.document["cgroup_max_bytes"] = None
        self.write()
        self.assertIsNone(self.load().binding.cgroup_max_bytes)

    def test_source_age_must_fit_policy_and_all_durations_are_positive(self):
        for section, key, invalid in (
            (None, "api_max_age_seconds", 0.0),
            (None, "gpu_max_age_seconds", -1.0),
            (None, "api_max_age_seconds", 5.1),
            ("policy", "sample_max_age_seconds", 4.9),
            ("policy", "recovery_seconds", 0.0),
            ("policy", "missing_pause_seconds", -1.0),
            ("policy", "reduction_interval_seconds", False),
        ):
            value = deepcopy(self.document)
            (value if section is None else value[section])[key] = invalid
            self.write(value)
            with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                self.load()

    def test_memory_thresholds_require_strict_gpu_and_host_hysteresis(self):
        for key, invalid in (
            ("gpu_pause_bytes", 0),
            ("gpu_pause_bytes", 768 * 1024**2),
            ("gpu_reduce_bytes", 2 * 1024**3),
            ("gpu_recover_bytes", 256 * 1024**2),
            ("host_recover_bytes", 3 * 1024**3),
            ("host_pause_bytes", 8 * 1024**3),
            ("host_pause_bytes", 1.5),
        ):
            value = deepcopy(self.document)
            value["policy"][key] = invalid
            self.write(value)
            with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                self.load()


class MineruStreamActivationSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = _env(self.root)
        self.enabled = {
            **self.base, **_mineru_topology(),
            "WORKER_PARSE_EXECUTION_MODE": "staged-v4",
            "DISCLOSURE_MINERU_CAPACITY_CONFIG": str(self.root / "capacity.json"),
            "DISCLOSURE_MINERU_CAPACITY_CONFIG_SHA256": OTHER,
            "DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG": str(self.root / "pressure.json"),
            "DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG_SHA256": RUNTIME,
            "DISCLOSURE_GPU_METRICS_URL": "http://127.0.0.1:30004/metrics",
        }

    def settings(self, environment):
        with patch.dict(os.environ, environment, clear=True):
            return load_settings()

    def test_default_off_keeps_legacy_settings_without_pressure_io(self):
        with patch.object(activation, "read_mineru_capacity_file", side_effect=AssertionError("IO")) as read:
            settings = self.settings(self.base)
        self.assertIsNone(settings.disclosure_mineru_stream_pressure_config)
        self.assertIsNone(settings.disclosure_mineru_stream_pressure_config_sha256)
        self.assertEqual(settings.worker_parse_execution_mode, "legacy-sync")
        read.assert_not_called()

    def test_valid_pair_only_stores_configuration_and_does_not_open_missing_files(self):
        with patch.object(activation, "read_mineru_capacity_file", side_effect=AssertionError("IO")) as read:
            settings = self.settings(self.enabled)
        self.assertEqual(settings.disclosure_mineru_stream_pressure_config, self.root / "pressure.json")
        self.assertEqual(settings.disclosure_mineru_stream_pressure_config_sha256, RUNTIME)
        self.assertFalse((self.root / "pressure.json").exists())
        read.assert_not_called()

    def test_pressure_path_and_hash_are_paired(self):
        for key in ("DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG", "DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG_SHA256"):
            env = {name: value for name, value in self.enabled.items() if name != key}
            with self.subTest(missing=key), self.assertRaisesRegex(ValidationError, "paired"):
                self.settings(env)

    def test_pressure_requires_explicit_capacity_not_legacy_capacity_defaults(self):
        env = {name: value for name, value in self.enabled.items() if name not in {
            "DISCLOSURE_MINERU_CAPACITY_CONFIG", "DISCLOSURE_MINERU_CAPACITY_CONFIG_SHA256",
        }}
        with self.assertRaisesRegex(ValidationError, "explicit capacity"):
            self.settings(env)

    def test_pressure_cannot_activate_in_legacy_worker_mode(self):
        env = {**self.enabled, "WORKER_PARSE_EXECUTION_MODE": "legacy-sync"}
        with self.assertRaisesRegex(ValidationError, "staged-v4"):
            self.settings(env)

    def test_pressure_requires_both_api_topology_and_gpu_source(self):
        for absent in (
            ("DISCLOSURE_GPU_METRICS_URL",),
            tuple(_mineru_topology()),
        ):
            env = {name: value for name, value in self.enabled.items() if name not in absent}
            with self.subTest(absent=absent), self.assertRaisesRegex(ValidationError, "both API and GPU"):
                self.settings(env)

    def test_pressure_path_and_hash_validation_rejects_relative_traversal_or_bad_hash(self):
        for key, invalid in (
            ("DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG", "pressure.json"),
            ("DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG", str(self.root / ".." / "pressure.json")),
            ("DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG_SHA256", "sha256:" + "A" * 64),
            ("DISCLOSURE_MINERU_STREAM_PRESSURE_CONFIG_SHA256", ""),
        ):
            with self.subTest(key=key, invalid=invalid), self.assertRaises(ValidationError):
                self.settings({**self.enabled, key: invalid})

    def test_gpu_forward_dependency_cannot_be_replaced_by_arbitrary_url(self):
        for invalid in ("http://127.0.0.1:30004/other", "http://example.test/metrics"):
            with self.subTest(url=invalid), self.assertRaises(ValidationError):
                self.settings({**self.enabled, "DISCLOSURE_GPU_METRICS_URL": invalid})


if __name__ == "__main__":
    unittest.main()
