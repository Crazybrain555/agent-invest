#!/bin/zsh
set -euo pipefail

# Match the resident worker's configuration order, including the explicit
# capacity authority required by the current health wire contract.
ENV_DIR="${DISCLOSURE_ENV_DIR:-$HOME/.config/agent-invest/disclosure_anchor}"
set -a
for f in worker.env cninfo.env; do
  [[ ! -r "$ENV_DIR/$f" ]] || source "$ENV_DIR/$f"
done
set +a

LABEL="com.agentinvest.mineru-tunnel"
DOMAIN="gui/$(id -u)"
launchctl print "$DOMAIN/$LABEL" | grep -E 'state =|pid =|last exit code ='

API_HEALTH="$(/usr/bin/curl --fail --silent --show-error --noproxy '*' \
  --max-redirs 0 --max-time 10 http://127.0.0.1:30002/health)"
PYTHONPATH="$(cd "$(dirname "$0")/.." && pwd)/src" \
  "$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/python" -c '
import json, sys
from disclosure_anchor.adapters.runtime.mineru_capacity_config import configured_mineru_capacity
from disclosure_anchor.adapters.runtime.worker_progress import mineru_api_health_snapshot
from disclosure_anchor.settings import load_settings
settings = load_settings()
capacity = configured_mineru_capacity(settings)
payload = sys.stdin.buffer.read()
print(json.dumps(mineru_api_health_snapshot(
    payload,
    expected_capacity=capacity,
    expected_task_slots=None if capacity is not None else settings.disclosure_mineru_api_task_slots,
), sort_keys=True))
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
