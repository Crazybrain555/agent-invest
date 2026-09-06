from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import unittest

from disclosure_anchor.application.contracts.staged_worker_profile_v4 import (
    StagedWorkerProfileV4, decode_staged_worker_profile_v4,
)


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


if __name__ == "__main__":
    unittest.main()
