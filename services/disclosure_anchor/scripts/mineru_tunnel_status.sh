#!/bin/zsh
set -euo pipefail

LABEL="com.agentinvest.mineru-tunnel"
DOMAIN="gui/$(id -u)"
launchctl print "$DOMAIN/$LABEL" | grep -E 'state =|pid =|last exit code ='

API_HEALTH="$(/usr/bin/curl --fail --silent --show-error --noproxy '*' \
  --max-redirs 0 --max-time 10 http://127.0.0.1:30002/health)"
PYTHONPATH="$(cd "$(dirname "$0")/.." && pwd)/src" \
  "$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/python" -c '
import json, sys
from disclosure_anchor.adapters.runtime.worker_progress import mineru_api_health_snapshot
payload = sys.stdin.buffer.read()
print(json.dumps(mineru_api_health_snapshot(payload), sort_keys=True))
' <<< "$API_HEALTH"
/usr/bin/curl --fail --silent --show-error --noproxy '*' --max-redirs 0 \
  --max-time 10 http://127.0.0.1:30001/health >/dev/null
echo 'vLLM health: available'
GPU_METRICS="$(/usr/bin/curl --fail --silent --show-error --noproxy '*' \
  --max-redirs 0 --max-time 10 http://127.0.0.1:30004/metrics)"
PYTHONPATH="$(cd "$(dirname "$0")/.." && pwd)/src" \
  "$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/python" -c '
import json, sys
from disclosure_anchor.adapters.runtime.worker_progress import gpu_metrics_snapshot
payload = sys.stdin.buffer.read()
print(json.dumps(gpu_metrics_snapshot(payload), sort_keys=True))
' <<< "$GPU_METRICS"
