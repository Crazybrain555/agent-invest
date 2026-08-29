import os
import json
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
        "MINERU_PROCESSING_WINDOW_SIZE": "16",
        "HF_HOME": str(shared_root / "model_cache" / "huggingface"),
        "MODELSCOPE_CACHE": str(shared_root / "model_cache" / "modelscope"),
    }


def _mineru_topology() -> dict[str, str]:
    return {
        "DISCLOSURE_MINERU_API_URL": "http://127.0.0.1:30002",
        "DISCLOSURE_MINERU_OBSERVABILITY_URL": "http://127.0.0.1:30001/v1",
        "DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL": (
            "http://mineru-openai-server:30000/v1"
        ),
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
                    **_mineru_topology(),
                },
                {
                    "WORKER_PARSE_CONCURRENCY": "8",
                    "WORKER_GPU_REQUEST_BUDGET": "129",
                    "WORKER_GPU_MAX_SEQUENCES": "128",
                    "DISCLOSURE_MINERU_BACKEND": "hybrid-http-client",
                    **_mineru_topology(),
                },
                {
                    "WORKER_PARSE_CONCURRENCY": "8",
                    "DISCLOSURE_MINERU_API_URL": "http://127.0.0.1:30002",
                    "DISCLOSURE_MINERU_OBSERVABILITY_URL": (
                        "http://127.0.0.1:30001/v1"
                    ),
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
                {"MINERU_PROCESSING_WINDOW_SIZE": "32"},
                {"DISCLOSURE_MINERU_LIVE_PROBE_INTERVAL_SECONDS": "0"},
                {"DISCLOSURE_MINERU_API_TASK_SLOTS": "4"},
                {
                    "DISCLOSURE_MINERU_API_TASK_SLOTS": "2",
                    "WORKER_GPU_REQUEST_BUDGET": "7",
                },
            ):
                with (
                    self.subTest(extra=extra),
                    patch.dict(os.environ, {**base, **extra}, clear=True),
                    self.assertRaises(ValidationError),
                ):
                    load_settings()

            with patch.dict(
                os.environ,
                {
                    **base,
                    "WORKER_PARSE_CONCURRENCY": "8",
                    "DISCLOSURE_MINERU_BACKEND": "hybrid-http-client",
                    **_mineru_topology(),
                },
                clear=True,
            ):
                settings = load_settings()

        self.assertEqual(settings.worker_parse_concurrency, 8)
        self.assertEqual(settings.worker_gpu_request_budget, 7)
        self.assertEqual(settings.worker_gpu_max_sequences, 128)
        self.assertEqual(settings.mineru_http_request_concurrency, 7)
        self.assertEqual(settings.mineru_effective_inference_request_upper_bound, 7)
        self.assertEqual(settings.worker_parse_heavy_page_threshold, 80)
        self.assertEqual(settings.worker_parse_huge_page_threshold, 500)

        with patch.dict(
            os.environ,
            {
                **base,
                "WORKER_PARSE_CONCURRENCY": "16",
                "DISCLOSURE_MINERU_BACKEND": "hybrid-http-client",
                "DISCLOSURE_MINERU_API_TASK_SLOTS": "2",
                "WORKER_GPU_REQUEST_BUDGET": "14",
                **_mineru_topology(),
            },
            clear=True,
        ):
            with self.assertRaises(ValidationError):
                load_settings()

    def test_loads_required_environment(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {**_env(Path(tmp)), "MINERU_PROCESSING_WINDOW_SIZE": "16"},
                clear=True,
            ),
        ):
            settings = load_settings()
            self.assertIsInstance(settings, Settings)
            self.assertEqual(settings.agent_system_root, Path(tmp))
            self.assertIsNone(settings.database_url)
            self.assertIsNone(settings.cninfo_access_key)
            self.assertEqual(settings.disclosure_parse_timeout_seconds, 3600)
            self.assertEqual(settings.disclosure_parse_timeout_per_page_seconds, 12)
            self.assertEqual(settings.disclosure_parse_timeout_max_seconds, 14400)
            self.assertEqual(settings.disclosure_parse_runaway_timeout_seconds, 86400)
            self.assertEqual(settings.worker_report_interval_seconds, 300)
            self.assertEqual(settings.mineru_processing_window_size, 16)
            self.assertEqual(
                settings.disclosure_mineru_live_probe_interval_seconds,
                300,
            )
            self.assertIsNone(settings.disclosure_mineru_bin)
            self.assertIsNone(settings.disclosure_mineru_validation_receipt)
            self.assertIsNone(settings.disclosure_mineru_api_url)
            self.assertEqual(settings.cninfo_max_qps, 1.0)
            self.assertEqual(settings.cninfo_max_retries, 3)
            self.assertEqual(settings.cninfo_overlap_days, 7)
            self.assertEqual(settings.cninfo_oversized_kb, 10240)
            self.assertEqual(settings.disclosure_semantic_model, "gpt-5.6-luna")
            self.assertEqual(settings.disclosure_semantic_reasoning_effort, "low")
            self.assertEqual(
                tuple(item.id for item in settings.semantic_provider_configs),
                ("luna-primary", "sonnet-backup"),
            )
            self.assertEqual(
                settings.semantic_provider_configs[1].canonical_model,
                "claude-sonnet-5",
            )

    def test_mineru_topology_rejects_ambiguous_or_exposed_urls(self) -> None:
        invalid_overrides = (
            {
                **_mineru_topology(),
                "DISCLOSURE_MINERU_API_URL": "http://100.64.0.1:30002",
            },
            {
                **_mineru_topology(),
                "DISCLOSURE_MINERU_API_URL": "http://user@127.0.0.1:30002",
            },
            {
                **_mineru_topology(),
                "DISCLOSURE_MINERU_API_URL": "http://127.0.0.1:30002/v1",
            },
            {
                **_mineru_topology(),
                "DISCLOSURE_MINERU_OBSERVABILITY_URL": "http://100.64.0.1:30001/v1",
            },
            {
                **_mineru_topology(),
                "DISCLOSURE_MINERU_OBSERVABILITY_URL": "http://127.0.0.1:30001",
            },
            {
                **_mineru_topology(),
                "DISCLOSURE_MINERU_OBSERVABILITY_URL": "http://127.0.0.1:30001/v1?x=1",
            },
            {
                **_mineru_topology(),
                "DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL": "http://mineru-openai-server:30000/other",
            },
            {
                **_mineru_topology(),
                "DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL": "http://127.0.0.1:30000/v1",
            },
            {
                **_mineru_topology(),
                "DISCLOSURE_MINERU_INFERENCE_UPSTREAM_URL": "http://vllm.example.com:30000/v1",
            },
            {**_mineru_topology(), "DISCLOSURE_MINERU_API_TASK_SLOTS": "4"},
            {**_mineru_topology(), "DISCLOSURE_MINERU_API_INFERENCE_CONCURRENCY": "8"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = _env(Path(tmp))
            for override in invalid_overrides:
                with (
                    self.subTest(override=override),
                    patch.dict(os.environ, {**base, **override}, clear=True),
                    self.assertRaises(ValidationError),
                ):
                    load_settings()

    def test_gpu_metrics_url_is_the_exact_loopback_forward(self) -> None:
        invalid_overrides = (
            {"DISCLOSURE_GPU_METRICS_URL": "https://127.0.0.1:30004/metrics"},
            {"DISCLOSURE_GPU_METRICS_URL": "http://100.64.0.1:30004/metrics"},
            {"DISCLOSURE_GPU_METRICS_URL": "http://user@127.0.0.1:30004/metrics"},
            {"DISCLOSURE_GPU_METRICS_URL": "http://127.0.0.1:9835/metrics"},
            {"DISCLOSURE_GPU_METRICS_URL": "http://127.0.0.1:30004/health"},
            {"DISCLOSURE_GPU_METRICS_URL": "http://127.0.0.1:30004/metrics?x=1"},
            {
                "DISCLOSURE_GPU_METRICS_URL": "http://127.0.0.1:30004/metrics",
                "DISCLOSURE_DCGM_METRICS_URL": "http://dcgm.invalid/metrics",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = _env(Path(tmp))
            for override in invalid_overrides:
                with (
                    self.subTest(override=override),
                    patch.dict(os.environ, {**base, **override}, clear=True),
                    self.assertRaises(ValidationError),
                ):
                    load_settings()
            with patch.dict(
                os.environ,
                {
                    **base,
                    "DISCLOSURE_GPU_METRICS_URL": ("http://127.0.0.1:30004/metrics"),
                },
                clear=True,
            ):
                settings = load_settings()

        self.assertEqual(
            settings.disclosure_gpu_metrics_url,
            "http://127.0.0.1:30004/metrics",
        )

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
                self.assertEqual(settings.disclosure_parse_timeout_per_page_seconds, 3)
                self.assertEqual(settings.disclosure_parse_timeout_max_seconds, 99)
                self.assertEqual(settings.disclosure_parse_runaway_timeout_seconds, 360)
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

    def test_structured_semantic_provider_chain_is_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(Path(tmp))
            env["DISCLOSURE_SEMANTIC_PROVIDERS_JSON"] = json.dumps(
                [
                    {
                        "id": "sonnet-primary",
                        "kind": "claude_cli",
                        "provider": "anthropic",
                        "executable": "/opt/claude",
                        "canonical_model": "claude-sonnet-5",
                        "profile": "medium",
                        "timeout_seconds": 900,
                        "max_concurrency": 2,
                    },
                    {
                        "id": "luna-backup",
                        "kind": "codex_cli",
                        "provider": "openai",
                        "executable": "/opt/codex",
                        "canonical_model": "gpt-5.6-luna",
                        "profile": "low",
                        "timeout_seconds": 600,
                        "max_concurrency": 1,
                    },
                ]
            )

            with patch.dict(os.environ, env, clear=True):
                settings = load_settings()

            self.assertEqual(
                tuple(item.id for item in settings.semantic_provider_configs),
                ("sonnet-primary", "luna-backup"),
            )
            self.assertEqual(
                settings.semantic_provider_configs[0].executable,
                Path("/opt/claude"),
            )

    def test_semantic_provider_chain_rejects_aliases_duplicates_and_mismatch(
        self,
    ) -> None:
        invalid_chains = (
            [],
            [
                {
                    "id": "sonnet-primary",
                    "kind": "claude_cli",
                    "provider": "anthropic",
                    "executable": "claude",
                    "canonical_model": "sonnet",
                },
            ],
            [
                {
                    "id": "same-id",
                    "kind": "codex_cli",
                    "provider": "openai",
                    "executable": "codex",
                    "canonical_model": "gpt-5.6-luna",
                },
                {
                    "id": "same-id",
                    "kind": "claude_cli",
                    "provider": "anthropic",
                    "executable": "claude",
                    "canonical_model": "claude-sonnet-5",
                },
            ],
            [
                {
                    "id": "wrong-provider",
                    "kind": "codex_cli",
                    "provider": "anthropic",
                    "executable": "codex",
                    "canonical_model": "gpt-5.6-luna",
                },
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            for chain in invalid_chains:
                env = _env(Path(tmp))
                env["DISCLOSURE_SEMANTIC_PROVIDERS_JSON"] = json.dumps(chain)
                with (
                    self.subTest(chain=chain),
                    patch.dict(os.environ, env, clear=True),
                    self.assertRaises((ValidationError, ValueError)),
                ):
                    settings = load_settings()
                    settings.semantic_provider_configs


if __name__ == "__main__":
    unittest.main()
