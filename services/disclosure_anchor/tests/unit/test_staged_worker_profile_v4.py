from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from disclosure_anchor.application.contracts.staged_worker_profile_v4 import (
    MAX_HEAVY_WORK_PERMITS, StagedWorkerProfileV4, decode_staged_worker_profile_v4,
)
from disclosure_anchor.application.services.staged_v4_capacity import staged_v4_coordinator_limits
from tests.unit.test_mineru_process_profile import _profile


# The production-shaped v2 composition (16 preflight, 2 finalize, 3600 s
# commit budget), pinned as literal wire bytes so a later contract version
# can never re-encode an identity old heads and specs are bound to.
_V2_BYTES = (
    b'{"admission_probe_milliseconds":1000,"commit_stage_seconds":3600,'
    b'"contract_version":"staged-worker-composition.v2","mac_finalize_workers":2,'
    b'"mac_preflight_workers":16,"process_profile_sha256":'
    b'"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    b'"provider_poll_milliseconds":1000}'
)
_V2_HASH = "sha256:d2d3e4ba221db7a74b184eca56e6c336b9becbd701eadb6bdb951949975569fd"


class StagedWorkerProfileV4Tests(unittest.TestCase):
    def test_boot_archive_count_bound_matches_the_execution_contract(self) -> None:
        from disclosure_anchor.settings import StagedV4Settings

        values = dict(
            process_profile_file=Path("/not-read/process-profile.json"),
            process_profile_sha256="sha256:" + "a" * 64,
        )
        settings = StagedV4Settings(**values, archive_member_count_limit=100_000)
        self.assertEqual(settings.archive_member_count_limit, 100_000)
        with self.assertRaises(ValueError):
            StagedV4Settings(**values, archive_member_count_limit=100_001)

    def test_canonical_roundtrip_and_closed_fields(self) -> None:
        profile = StagedWorkerProfileV4("sha256:" + "a" * 64, 3, 5)
        self.assertEqual(decode_staged_worker_profile_v4(profile.exact_bytes), profile)
        for payload in (
            json.dumps(asdict(profile), indent=2).encode(),
            json.dumps(asdict(profile) | {"extra": 1}).encode(),
            profile.exact_bytes.replace(b'"mac_finalize_workers":5',
                                        b'"mac_finalize_workers":5,"mac_finalize_workers":5'),
            b'{}', b'[]', b'x' * 4097,
        ):
            with self.subTest(payload=payload[:80]), self.assertRaises(ValueError):
                decode_staged_worker_profile_v4(payload)

    def test_numeric_bounds_and_runtime_binding_are_strict(self) -> None:
        profile = StagedWorkerProfileV4("sha256:" + "a" * 64, 3, 5)
        for name in ("mac_preflight_workers", "mac_finalize_workers",
                     "provider_poll_milliseconds", "admission_probe_milliseconds"):
            for value in (0, -1, True, 1.5, "2"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    replace(profile, **{name: value})
        for change in ({"contract_version": "unknown"}, {"process_profile_sha256": "bad"},
                       {"provider_poll_milliseconds": 60001},
                       {"admission_probe_milliseconds": 60001}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(profile, **change)

    def test_v3_declares_heavy_permits_without_reencoding_older_identities(self) -> None:
        v2 = decode_staged_worker_profile_v4(_V2_BYTES)
        self.assertEqual((v2.exact_bytes, v2.sha256), (_V2_BYTES, _V2_HASH))
        self.assertIsNone(v2.heavy_work_permits)
        v3 = replace(v2, contract_version="staged-worker-composition.v3", heavy_work_permits=2)
        self.assertEqual(decode_staged_worker_profile_v4(v3.exact_bytes), v3)
        self.assertEqual(json.loads(v3.exact_bytes)["heavy_work_permits"], 2)
        hashes = {v2.sha256, v3.sha256, replace(v3, heavy_work_permits=1).sha256}
        self.assertEqual(len(hashes), 3, "every permit count is its own composition identity")
        self.assertEqual(MAX_HEAVY_WORK_PERMITS, 2)
        for value in (None, 0, 3, -1, True, 1.5, "2"):
            with self.subTest(v3_permits=value), self.assertRaises(ValueError):
                replace(v3, heavy_work_permits=value)
        for older in (v2, StagedWorkerProfileV4("sha256:" + "a" * 64, 3, 5)):
            with self.subTest(older=older.contract_version), self.assertRaises(ValueError):
                replace(older, heavy_work_permits=1)
        # Closed per version: the field is required in v3 and absent before it.
        without = json.loads(v3.exact_bytes)
        del without["heavy_work_permits"]
        smuggled = json.loads(_V2_BYTES) | {"heavy_work_permits": 2}
        for payload in (without, smuggled, json.loads(v3.exact_bytes) | {"extra": 1}):
            with self.subTest(fields=sorted(payload)), self.assertRaises(ValueError):
                decode_staged_worker_profile_v4(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                )

    def test_heavy_permit_setting_selects_v3_and_reaches_the_projection_only_when_set(self) -> None:
        from disclosure_anchor.settings import StagedV4Settings, load_staged_v4_settings

        remote = _profile()
        base = dict(process_profile_file=Path("/not-read/profile.json"),
                    process_profile_sha256=remote.sha256, archive_member_count_limit=8192)
        composed = dict(process_profile_sha256=remote.sha256,
                        mac_preflight_workers=16, mac_finalize_workers=2)
        with mock.patch.dict(os.environ, {}, clear=True):
            unset = StagedV4Settings(**base).worker_profile(**composed)
            # Unset is byte-for-byte the v2 composition this release replaces.
            self.assertEqual(unset, StagedWorkerProfileV4(
                remote.sha256, 16, 2, contract_version="staged-worker-composition.v2",
                commit_stage_seconds=3600,
            ))
            self.assertEqual(staged_v4_coordinator_limits(remote, worker_profile=unset).heavy_work_permits, 1)
            for permits in (1, 2):
                with self.subTest(permits=permits):
                    profile = StagedV4Settings(**base, heavy_work_permits=permits).worker_profile(**composed)
                    self.assertEqual(
                        (profile.contract_version, profile.heavy_work_permits, profile.commit_stage_seconds),
                        ("staged-worker-composition.v3", permits, 3600),
                    )
                    self.assertEqual(
                        staged_v4_coordinator_limits(remote, worker_profile=profile).heavy_work_permits,
                        permits,
                    )
            self.assertEqual(
                StagedV4Settings.model_fields["heavy_work_permits"].metadata[1].le,
                MAX_HEAVY_WORK_PERMITS,
            )
            for value in (0, 3, -1, 1.5, "two"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    StagedV4Settings(**base, heavy_work_permits=value)
        environment = {
            "DISCLOSURE_V4_PROCESS_PROFILE_FILE": "/not-read/profile.json",
            "DISCLOSURE_V4_PROCESS_PROFILE_SHA256": remote.sha256,
            "DISCLOSURE_V4_ARCHIVE_MEMBER_COUNT_LIMIT": "8192",
        }
        with mock.patch.dict(os.environ, environment | {"DISCLOSURE_V4_HEAVY_WORK_PERMITS": "2"}, clear=True):
            self.assertEqual(load_staged_v4_settings().heavy_work_permits, 2)
        with mock.patch.dict(os.environ, environment | {"DISCLOSURE_V4_HEAVY_WORK_PERMITS": "3"}, clear=True):
            with self.assertRaises(ValueError):
                load_staged_v4_settings()


if __name__ == "__main__":
    unittest.main()
