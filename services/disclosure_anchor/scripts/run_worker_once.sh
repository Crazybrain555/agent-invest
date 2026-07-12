#!/bin/zsh
# Production worker entry for launchd/cron: loads the machine-local env
# (worker.env: roots/DB/MinerU; cninfo.env: credentials) then runs one round.
# Exit codes: 0 = round completed (per-item failures land in the report),
# 77 = TCC write-access failure (see below), else fatal.
#
# macOS TCC reality (observed 2026-07-08/09): launchd-spawned processes have
# no grant for external volumes. zsh writes fail EPERM instantly; python's
# open() HANGS forever waiting on a consent prompt that can never render —
# a wedged round then holds the worker singleton lock and silently blocks
# every future tick. So before launching the worker we probe the roots WITH
# THE SAME PYTHON BINARY under a watchdog, and fail loudly instead.
# One-time fix: System Settings > Privacy & Security > Full Disk Access ->
# add /bin/zsh AND the venv python; then kickstart the job.
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
FALLBACK_LOG_DIR="$HOME/Library/Logs/agent-invest"
mkdir -p "$FALLBACK_LOG_DIR"

tcc_help() {
  echo "FATAL: $1 is not writable from launchd (macOS TCC)." \
    "One-time fix: System Settings > Privacy & Security > Full Disk Access ->" \
    "add /bin/zsh and $(pwd)/.venv/bin/python, then run:" \
    "launchctl kickstart -k gui/$(id -u)/com.agentinvest.disclosure-worker" >&2
}

# Watchdog probe with the real worker interpreter (a plain zsh probe can
# EPERM while python still hangs — different TCC identities).
probe_roots() {
  .venv/bin/python - "$DISCLOSURE_DATA_ROOT" "$DISCLOSURE_RUNTIME_ROOT" <<'PY' &
import os, sys
for root in sys.argv[1:]:
    probe = os.path.join(root, ".launchd_write_probe")
    with open(probe, "a"):
        pass
    os.remove(probe)
PY
  local pid=$!
  local waited=0
  while kill -0 $pid 2>/dev/null; do
    if (( waited >= 50 )); then
      kill -9 $pid 2>/dev/null
      return 2
    fi
    sleep 0.1
    (( waited++ ))
  done
  wait $pid
}

if ! probe_roots; then
  tcc_help "$DISCLOSURE_DATA_ROOT or $DISCLOSURE_RUNTIME_ROOT"
  exit 77
fi

if ! mkdir -p "$LOG_DIR" 2>/dev/null \
    || ! : >>"$LOG_DIR/worker-$(date +%Y%m%d).log" 2>/dev/null; then
  echo "WARN: cannot write $LOG_DIR from this context; falling back to" \
    "$FALLBACK_LOG_DIR (grant /bin/zsh Full Disk Access to restore)." >&2
  LOG_DIR="$FALLBACK_LOG_DIR"
fi
exec >>"$LOG_DIR/worker-$(date +%Y%m%d).log" 2>&1
echo "=== worker-once $(date '+%F %T') ==="
exec make worker-once
