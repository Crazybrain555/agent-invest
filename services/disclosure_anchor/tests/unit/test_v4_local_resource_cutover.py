from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from disclosure_anchor.adapters.runtime.staged_worker_v4 import StagedWorkerV4Runtime
from disclosure_anchor.application.ports.remote_parse_v4_repository import V4HistoricalLocalResources
from disclosure_anchor.application.services.verify_v4_local_resource_cutover import verify_v4_local_resource_cutover
from disclosure_anchor.application.contracts.remote_parse_lifecycle_v4 import deterministic_local_resource_paths_v4
from tests.unit.test_mineru_http_staged_v4 import _materialize_fixture, _official_zip


class _Uow:
    def __init__(self, repository):
        self.remote_parse_v4 = repository

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class V4LocalResourceCutoverTests(unittest.TestCase):
    def test_bounded_pages_include_every_history_and_do_not_write(self):
        intent = _materialize_fixture(_official_zip()).intent
        rows = []
        for key in ("a", "b", "c"):
            paths = deterministic_local_resource_paths_v4(
                attempt_id=key, fence_identity=intent.fence_identity,
                source_pdf_sha256=intent.source_pdf_sha256,
                artifact_owner_identity=intent.artifact_owner_identity,
                artifact_sha256=intent.artifact_sha256, output_dir_name=intent.output_dir_name,
            )
            rows.append(V4HistoricalLocalResources(replace(
                intent, attempt_id=key,
                **{f"{name}_relpath": paths[name] for name in (
                    "snapshot", "spool", "spool_part", "spool_part_owner", "spool_lock",
                    "staging", "staging_marker", "staging_lock", "output",
                )},
            ), None))
        repository = Mock()
        repository.list_historical_local_resources.side_effect = lambda after_attempt_id, limit: tuple(
            row for row in rows if after_attempt_id is None or row.intent.attempt_id > after_attempt_id
        )[:limit]
        inspector = Mock()
        guard = Mock()
        verify_v4_local_resource_cutover(
            uow_factory=lambda: _Uow(repository), inspector=inspector,
            ownership_guard=guard, page_size=1,
        )
        self.assertEqual(repository.list_historical_local_resources.call_count, 4)
        self.assertEqual([call.args[0] for call in inspector.verify_historical_local_resources.call_args_list], list(rows))
        self.assertGreaterEqual(guard.call_count, 8)

    def test_drifted_cursor_and_revoked_ownership_stop_before_more_io(self):
        intent = _materialize_fixture(_official_zip()).intent
        row = V4HistoricalLocalResources(intent, None)
        repository = Mock()
        repository.list_historical_local_resources.return_value = (row,)
        inspector = Mock()
        with self.assertRaisesRegex(ValueError, "cursor did not advance"):
            verify_v4_local_resource_cutover(
                uow_factory=lambda: _Uow(repository), inspector=inspector,
                ownership_guard=lambda: None, page_size=1,
            )
        inspector.verify_historical_local_resources.assert_called_once_with(row)
        repository.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "revoked"):
            verify_v4_local_resource_cutover(
                uow_factory=lambda: _Uow(repository), inspector=inspector,
                ownership_guard=Mock(side_effect=RuntimeError("revoked")),
            )
        repository.list_historical_local_resources.assert_not_called()

    def test_runtime_once_per_resident_not_per_quiescent_cycle_and_failed_gate_not_cached(self):
        gate = Mock(side_effect=[RuntimeError("unresolved old residual"), None])
        runtime = StagedWorkerV4Runtime(
            coordinator=SimpleNamespace(), remote=SimpleNamespace(),
            owner_identity="boot", worker_profile_sha256="sha256:"+"a"*64,
            startup_guard=gate,
        )
        with self.assertRaisesRegex(RuntimeError, "old residual"):
            runtime.verify_startup()
        self.assertFalse(runtime._startup_verified)
        runtime.verify_startup()
        runtime.verify_startup()
        self.assertEqual(gate.call_count, 2)
