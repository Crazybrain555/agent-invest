"""Direct runs of scripts/mineru_tunnel_status.sh against isolated fakes.

The unmodified script, virtualenv and health parsers run. A temporary ZDOTDIR
shadows launchctl and the absolute /usr/bin/curl with zsh functions, so no
launchd job, loopback forward, private env directory or runtime root is read.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

from tests._mineru_admission_health_fixture import canonical, wire_health
from tests._mineru_capacity_config_fixture import (
    CAPACITY_BYTES,
    canonical_payload,
    capacity_payload,
)
from tests._mineru_capacity_health_fixture import capacity_health_payload
from tests.unit.test_capacity_sources import _gpu_payload
from tests.unit.test_settings import _env


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mineru_tunnel_status.sh"
CURL = "curl --fail --silent --show-error --noproxy * --max-redirs 0 --max-time 10 "
LAUNCHCTL = f"launchctl print gui/{os.getuid()}/com.agentinvest.mineru-tunnel"
API_HEALTH = CURL + "http://127.0.0.1:30002/health"
LATER_PROBES = [
    CURL + "http://127.0.0.1:30001/health",
    CURL + "--include --output <private-temp-file> --write-out %{time_total} http://127.0.0.1:30004/metrics",
]
# A real pinned-exporter Date on its own host clock (1790697811).
EXPORTER_DATE = "Tue, 29 Sep 2026 16:03:31 GMT"
CAPACITY_SHA256 = "sha256:" + hashlib.sha256(CAPACITY_BYTES).hexdigest()
# Differs only in a limit the health wire never echoes, so the bound hash is
# the sole evidence separating it from CAPACITY_BYTES.
OTHER_CAPACITY_BYTES = canonical_payload(capacity_payload(omp_num_threads=8))
OTHER_CAPACITY_SHA256 = "sha256:" + hashlib.sha256(OTHER_CAPACITY_BYTES).hexdigest()
SECRET = "tunnel-status-sentinel-secret"
CNINFO = {"CNINFO_ACCESS_KEY": "placeholder", "CNINFO_ACCESS_SECRET": SECRET}
LAUNCHD_LINES = ["state = running", "pid = 4242", "last exit code = 0"]

FAKES = r"""
launchctl() {
  print -r -- "launchctl $*" >> "$TUNNEL_STATUS_CALLS"
  [[ "$*" == "print gui/$(/usr/bin/id -u)/com.agentinvest.mineru-tunnel" ]] || return 64
  print -r -- 'com.agentinvest.mineru-tunnel = {'
  print -r -- 'state = running'
  print -r -- 'pid = 4242'
  print -r -- 'last exit code = 0'
}
/usr/bin/curl() {
  print -r -- "curl $*" >> "$TUNNEL_STATUS_CALLS"
  case "${argv[-1]}" in
    http://127.0.0.1:30002/health) /bin/cat -- "$TUNNEL_STATUS_API_HEALTH" ;;
    http://127.0.0.1:30001/health) print -r -- ok ;;
    http://127.0.0.1:30004/metrics)
      local output=/dev/stdout index
      for (( index = 1; index < $#; index++ )); do
        [[ "${argv[index]}" != --output ]] || output="${argv[index + 1]}"
      done
      {
        if (( ${argv[(Ie)--include]} )); then
          printf 'HTTP/1.1 200 OK\r\nContent-Type: text/plain; version=0.0.4\r\n'
          [[ -z "${TUNNEL_STATUS_GPU_DATE-}" ]] || printf 'Date: %s\r\n' "$TUNNEL_STATUS_GPU_DATE"
          printf '\r\n'
        fi
        if [[ -n "${TUNNEL_STATUS_GPU_BODY-}" ]]; then
          /bin/cat -- "$TUNNEL_STATUS_GPU_BODY"
        else
          print -r -- 'DCGM_FI_DEV_GPU_UTIL{gpu="0"} 80'
        fi
      } > "$output"
      if (( ${argv[(Ie)--write-out]} )); then
        print -rn -- "${TUNNEL_STATUS_GPU_ELAPSED:-0.050000}"
      fi ;;
    *) return 7 ;;
  esac
}
"""


class MineruTunnelStatusScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        # The capacity reader refuses symlinked components such as macOS /var.
        temporary = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        home = self.root / "home"
        self.default_env_dir = home / ".config" / "agent-invest" / "disclosure_anchor"
        self.default_env_dir.mkdir(parents=True)
        zdotdir = self.root / "zdotdir"
        zdotdir.mkdir()
        (zdotdir / ".zshenv").write_text(FAKES, encoding="utf-8")
        self.calls = self.root / "calls.log"
        self.health = self.root / "api-health.json"
        self.capacity = self.root / "capacity.json"
        self.environment = {
            "HOME": str(home),
            "ZDOTDIR": str(zdotdir),
            "PATH": "/usr/bin:/bin",
            "TUNNEL_STATUS_CALLS": str(self.calls),
            "TUNNEL_STATUS_API_HEALTH": str(self.health),
            "TUNNEL_STATUS_GPU_DATE": EXPORTER_DATE,
        }
        shadowed = subprocess.run(
            ["/bin/zsh", "-c", "whence -w launchctl /usr/bin/curl"],
            capture_output=True,
            check=True,
            env=self.environment,
            text=True,
        )
        # Never start the script unless both external commands are fakes.
        self.assertEqual(
            shadowed.stdout.split(),
            ["launchctl:", "function", "/usr/bin/curl:", "function"],
        )

    def write_capacity(self, payload: bytes) -> None:
        self.capacity.write_bytes(payload)
        self.capacity.chmod(0o600)

    def write_env(self, path: Path, values: dict[str, str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def explicit_env(self, sha256: str) -> dict[str, str]:
        return {
            **_env(self.root),
            "WORKER_PARSE_EXECUTION_MODE": "staged-v4",
            "DISCLOSURE_MINERU_CAPACITY_CONFIG": str(self.capacity),
            "DISCLOSURE_MINERU_CAPACITY_CONFIG_SHA256": sha256,
        }

    def run_script(
        self, health: bytes, **environment: str
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        self.health.write_bytes(health)
        self.calls.write_text("", encoding="utf-8")
        completed = subprocess.run(
            [str(SCRIPT)],
            capture_output=True,
            check=False,
            cwd=self.root,
            env={**self.environment, **environment},
            text=True,
            timeout=120,
        )
        self.assertNotIn(SECRET, completed.stdout + completed.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        outputs = [match.group(1) for call in calls if (match := re.search(r"--output (\S+)", call))]
        # The script's private response file never outlives it.
        self.assertTrue(all(not Path(output).exists() for output in outputs))
        return completed, [re.sub(r"--output \S+", "--output <private-temp-file>", call) for call in calls]

    def assert_completed(
        self, completed: subprocess.CompletedProcess[str], calls: list[str]
    ) -> dict[str, object]:
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(calls, [LAUNCHCTL, API_HEALTH, *LATER_PROBES])
        lines = completed.stdout.splitlines()
        self.assertEqual(len(lines), 6, completed.stdout)
        self.assertEqual(lines[:3], LAUNCHD_LINES)
        self.assertEqual(lines[4], "vLLM health: available")
        self.assertEqual(json.loads(lines[5])["source"], "nvidia_dcgm_exporter")
        snapshot = json.loads(lines[3])
        self.assertEqual(
            (snapshot["status"], snapshot["source"], snapshot["health_status"]),
            ("available", "mineru_api_health", "healthy"),
        )
        return snapshot

    def test_explicit_capacity_from_default_env_dir_accepts_bound_runtime_v3(self) -> None:
        self.write_capacity(CAPACITY_BYTES)
        self.write_env(self.default_env_dir / "worker.env", self.explicit_env(CAPACITY_SHA256))
        self.write_env(self.default_env_dir / "cninfo.env", CNINFO)
        served = capacity_health_payload()

        snapshot = self.assert_completed(*self.run_script(canonical(served)))

        self.assertEqual(snapshot["capacity_observation"], served["capacity_observation"])
        self.assertEqual(snapshot["task_admission"], served["task_admission"])
        self.assertEqual(
            (snapshot["max_concurrent_requests"], snapshot["max_pending_tasks_effective"]),
            (2, 3),
        )

    def test_legacy_env_dir_override_keeps_single_task_slot_contract(self) -> None:
        # Reading the default directory instead would require a missing file.
        self.write_env(self.default_env_dir / "worker.env", self.explicit_env(CAPACITY_SHA256))
        override = self.root / "override-env"
        self.write_env(override / "worker.env", _env(self.root))
        widened = wire_health()
        widened.update(
            max_concurrent_requests=2,
            max_pending_tasks_requested=2,
            max_pending_tasks_effective=2,
        )
        widened["task_admission"]["nonterminal_limit"] = 2

        snapshot = self.assert_completed(
            *self.run_script(canonical(wire_health()), DISCLOSURE_ENV_DIR=str(override))
        )
        self.assertNotIn("capacity_observation", snapshot)
        self.assertEqual(snapshot["max_concurrent_requests"], 1)

        completed, calls = self.run_script(canonical(widened), DISCLOSURE_ENV_DIR=str(override))
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(calls, [LAUNCHCTL, API_HEALTH])
        self.assertIn("MinerU API task-slot/pending limit drifted", completed.stderr)

    def test_explicit_capacity_mismatch_or_invalid_health_stops_before_later_probes(
        self,
    ) -> None:
        self.write_env(self.default_env_dir / "cninfo.env", CNINFO)
        bound = canonical(capacity_health_payload())
        cases = (
            (
                "served capacity hash differs from the configured authority",
                OTHER_CAPACITY_BYTES,
                OTHER_CAPACITY_SHA256,
                bound,
                "MinerU serving task runtime differs from expected capacity",
            ),
            (
                "local capacity bytes differ from the pinned hash",
                CAPACITY_BYTES,
                OTHER_CAPACITY_SHA256,
                bound,
                "MinerU capacity file hash differs from expected identity",
            ),
            (
                "legacy wire is not accepted under explicit capacity",
                CAPACITY_BYTES,
                CAPACITY_SHA256,
                canonical(wire_health()),
                "MinerU capacity wire health fields are not closed",
            ),
            (
                "truncated response",
                CAPACITY_BYTES,
                CAPACITY_SHA256,
                bound[:-1],
                "JSONDecodeError",
            ),
        )
        for label, capacity, pinned, health, reason in cases:
            with self.subTest(label):
                self.write_capacity(capacity)
                self.write_env(self.default_env_dir / "worker.env", self.explicit_env(pinned))

                completed, calls = self.run_script(health)

                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(calls, [LAUNCHCTL, API_HEALTH])
                self.assertEqual(completed.stdout.splitlines(), LAUNCHD_LINES)
                self.assertIn(reason, completed.stderr)

    def test_cninfo_env_is_sourced_after_worker_env(self) -> None:
        # A later file overrides an earlier one, as in run_worker_once.sh.
        self.write_capacity(CAPACITY_BYTES)
        self.write_env(
            self.default_env_dir / "worker.env", self.explicit_env(OTHER_CAPACITY_SHA256)
        )
        self.write_env(
            self.default_env_dir / "cninfo.env",
            {**CNINFO, "DISCLOSURE_MINERU_CAPACITY_CONFIG_SHA256": CAPACITY_SHA256},
        )

        snapshot = self.assert_completed(
            *self.run_script(canonical(capacity_health_payload()))
        )

        self.assertEqual(
            snapshot["capacity_observation"]["capacity_config_sha256"], CAPACITY_SHA256
        )

    def test_nvidia_snapshot_ages_by_the_exporter_response_date(self) -> None:
        self.write_capacity(CAPACITY_BYTES)
        self.write_env(self.default_env_dir / "worker.env", self.explicit_env(CAPACITY_SHA256))
        body = self.root / "gpu-metrics.prom"
        body.write_bytes(
            _gpu_payload().replace(b"timestamp_seconds 1000", b"timestamp_seconds 1.790697811e+09")
        )
        health = canonical(capacity_health_payload())

        completed, calls = self.run_script(
            health, TUNNEL_STATUS_GPU_BODY=str(body), TUNNEL_STATUS_GPU_ELAPSED="0.250000",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(calls, [LAUNCHCTL, API_HEALTH, *LATER_PROBES])
        gpu = json.loads(completed.stdout.splitlines()[5])
        # Same-host bound (Date + 1 - success) plus curl's request-to-receipt time.
        self.assertEqual((gpu["source"], gpu["sample_age_seconds"]), ("nvidia_smi_exporter", 1.25))
        # Without the exporter's own Date there is no freshness evidence, and
        # this host's wall clock is never substituted.
        completed, _ = self.run_script(
            health, TUNNEL_STATUS_GPU_BODY=str(body), TUNNEL_STATUS_GPU_DATE="",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("GpuClockEvidenceError", completed.stderr)


if __name__ == "__main__":
    unittest.main()
