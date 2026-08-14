import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from disclosure_anchor.settings import Settings, load_settings


def _env(root: Path) -> dict[str, str]:
    data_root = root / "services" / "disclosure_anchor"
    shared_root = root / "shared"
    return {
        "DISCLOSURE_DATA_ROOT": str(data_root),
        "DISCLOSURE_SHARED_ROOT": str(shared_root),
        "DISCLOSURE_RUNTIME_ROOT": str(data_root / "runtime"),
        "MINERU_MODEL_CACHE": str(shared_root / "model_cache" / "mineru"),
        "HF_HOME": str(shared_root / "model_cache" / "huggingface"),
        "MODELSCOPE_CACHE": str(shared_root / "model_cache" / "modelscope"),
    }


class SettingsTests(unittest.TestCase):
    def test_parallel_parse_requires_remote_http_backend_and_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = _env(Path(tmp))
            for extra in (
                {"WORKER_PARSE_CONCURRENCY": "8"},
                {
                    "WORKER_PARSE_CONCURRENCY": "8",
                    "DISCLOSURE_MINERU_BACKEND": "hybrid-http-client",
                },
                {
                    "WORKER_PARSE_CONCURRENCY": "8",
                    "WORKER_GPU_REQUEST_BUDGET": "4",
                    "DISCLOSURE_MINERU_BACKEND": "hybrid-http-client",
                    "DISCLOSURE_MINERU_SERVER_URL": "http://127.0.0.1:30000",
                },
                {
                    "WORKER_PARSE_CONCURRENCY": "8",
                    "WORKER_GPU_REQUEST_BUDGET": "129",
                    "WORKER_GPU_MAX_SEQUENCES": "128",
                    "DISCLOSURE_MINERU_BACKEND": "hybrid-http-client",
                    "DISCLOSURE_MINERU_SERVER_URL": "http://127.0.0.1:30000",
                },
                {
                    "WORKER_PARSE_HEAVY_PAGE_THRESHOLD": "80",
                    "WORKER_PARSE_HUGE_PAGE_THRESHOLD": "80",
                },
                {
                    "DISCLOSURE_PARSE_TIMEOUT_MAX_SECONDS": "14400",
                    "DISCLOSURE_PARSE_RUNAWAY_TIMEOUT_SECONDS": "14399",
                },
                {"WORKER_REPORT_INTERVAL_SECONDS": "0"},
            ):
                with self.subTest(extra=extra), patch.dict(
                    os.environ, {**base, **extra}, clear=True
                ), self.assertRaises(ValidationError):
                    load_settings()

            with patch.dict(
                os.environ,
                {
                    **base,
                    "WORKER_PARSE_CONCURRENCY": "8",
                    "DISCLOSURE_MINERU_BACKEND": "hybrid-http-client",
                    "DISCLOSURE_MINERU_SERVER_URL": "http://127.0.0.1:30000",
                },
                clear=True,
            ):
                settings = load_settings()

        self.assertEqual(settings.worker_parse_concurrency, 8)
        self.assertEqual(settings.worker_gpu_request_budget, 112)
        self.assertEqual(settings.worker_gpu_max_sequences, 128)
        self.assertEqual(settings.worker_parse_heavy_page_threshold, 80)
        self.assertEqual(settings.worker_parse_huge_page_threshold, 500)

    def test_loads_required_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _env(Path(tmp)), clear=True):
            settings = load_settings()
            self.assertIsInstance(settings, Settings)
            self.assertEqual(settings.agent_system_root, Path(tmp))
            self.assertIsNone(settings.database_url)
            self.assertIsNone(settings.cninfo_access_key)
            self.assertEqual(settings.disclosure_parse_timeout_seconds, 3600)
            self.assertEqual(
                settings.disclosure_parse_timeout_per_page_seconds, 12
            )
            self.assertEqual(settings.disclosure_parse_timeout_max_seconds, 14400)
            self.assertEqual(
                settings.disclosure_parse_runaway_timeout_seconds, 86400
            )
            self.assertEqual(settings.worker_report_interval_seconds, 300)
            self.assertIsNone(settings.disclosure_mineru_bin)
            self.assertEqual(settings.cninfo_max_qps, 1.0)
            self.assertEqual(settings.cninfo_max_retries, 3)
            self.assertEqual(settings.cninfo_overlap_days, 7)
            self.assertEqual(settings.cninfo_oversized_kb, 10240)
            self.assertEqual(settings.disclosure_semantic_model, "gpt-5.6-luna")
            self.assertEqual(settings.disclosure_semantic_reasoning_effort, "low")

    def test_secrets_are_optional_and_masked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(Path(tmp))
            env.update(
                {
                    "DATABASE_URL": "postgresql://user:<set-in-private-env>@127.0.0.1:55432/db",
                    "DISCLOSURE_READER_DATABASE_URL": (
                        "postgresql://reader:<set-in-private-env>@127.0.0.1:55432/db"
                    ),
                    "CNINFO_ACCESS_KEY": "key",
                    "CNINFO_ACCESS_SECRET": "<set-in-private-env>",
                    "CNINFO_MAX_QPS": "2.5",
                    "CNINFO_MAX_RETRIES": "5",
                    "CNINFO_OVERLAP_DAYS": "14",
                    "CNINFO_OVERSIZED_KB": "20480",
                    "DISCLOSURE_PARSE_TIMEOUT_SECONDS": "42",
                    "DISCLOSURE_PARSE_TIMEOUT_PER_PAGE_SECONDS": "3",
                    "DISCLOSURE_PARSE_TIMEOUT_MAX_SECONDS": "99",
                    "DISCLOSURE_PARSE_RUNAWAY_TIMEOUT_SECONDS": "360",
                    "WORKER_REPORT_INTERVAL_SECONDS": "77",
                    "DISCLOSURE_MINERU_BIN": "/opt/mineru/bin/mineru",
                    "DISCLOSURE_SEMANTIC_MODEL": "gpt-5.3-codex-spark",
                    "DISCLOSURE_SEMANTIC_REASONING_EFFORT": "high",
                }
            )
            with patch.dict(os.environ, env, clear=True):
                settings = load_settings()
                self.assertNotIn("<set-in-private-env>", repr(settings.database_url))
                self.assertNotIn(
                    "<set-in-private-env>",
                    repr(settings.disclosure_reader_database_url),
                )
                self.assertEqual(settings.cninfo_access_key.get_secret_value(), "key")
                self.assertEqual(
                    settings.disclosure_reader_database_url.get_secret_value(),
                    "postgresql://reader:<set-in-private-env>@127.0.0.1:55432/db",
                )
                self.assertEqual(settings.disclosure_parse_timeout_seconds, 42)
                self.assertEqual(
                    settings.disclosure_parse_timeout_per_page_seconds, 3
                )
                self.assertEqual(
                    settings.disclosure_parse_timeout_max_seconds, 99
                )
                self.assertEqual(
                    settings.disclosure_parse_runaway_timeout_seconds, 360
                )
                self.assertEqual(settings.worker_report_interval_seconds, 77)
                self.assertEqual(
                    settings.disclosure_mineru_bin, Path("/opt/mineru/bin/mineru")
                )
                self.assertEqual(settings.cninfo_max_qps, 2.5)
                self.assertEqual(settings.cninfo_max_retries, 5)
                self.assertEqual(settings.cninfo_overlap_days, 14)
                self.assertEqual(settings.cninfo_oversized_kb, 20480)
                self.assertEqual(
                    settings.disclosure_semantic_model,
                    "gpt-5.3-codex-spark",
                )
                self.assertEqual(
                    settings.disclosure_semantic_reasoning_effort,
                    "high",
                )


if __name__ == "__main__":
    unittest.main()
