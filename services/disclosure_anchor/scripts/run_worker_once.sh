#!/bin/zsh
# Production worker entry for launchd/cron: loads the machine-local env
# (worker.env: roots/DB/MinerU; cninfo.env: credentials) then runs one round.
# Exit codes: 0 = round completed (per-item failures land in the report), else fatal.
set -euo pipefail
ENV_DIR="${DISCLOSURE_ENV_DIR:-$HOME/.config/agent-invest/disclosure_anchor}"
for f in worker.env cninfo.env; do
  [[ -r "$ENV_DIR/$f" ]] || { echo "missing $ENV_DIR/$f" >&2; exit 78; }
done
set -a
source "$ENV_DIR/worker.env"
source "$ENV_DIR/cninfo.env"
set +a
cd "$(dirname "$0")/.."
LOG_DIR="$DISCLOSURE_RUNTIME_ROOT/logs"
mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/worker-$(date +%Y%m%d).log" 2>&1
echo "=== worker-once $(date '+%F %T') ==="
exec make worker-once
