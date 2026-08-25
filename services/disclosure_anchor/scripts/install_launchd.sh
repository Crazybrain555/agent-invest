#!/bin/zsh
# Install the resident adaptive worker launchd job. A loaded job must be
# drained and booted out explicitly; installation never interrupts live work.
# launchd stdout/err live in ~/Library/Logs/agent-invest (internal disk —
# external volumes are TCC-denied for launchd-spawned processes); the real
# worker log still lands under $DISCLOSURE_RUNTIME_ROOT/logs.
set -euo pipefail
PLIST="$HOME/Library/LaunchAgents/com.agentinvest.disclosure-worker.plist"
LABEL="com.agentinvest.disclosure-worker"
DOMAIN="gui/$(id -u)"
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo "refusing to replace loaded $LABEL" >&2
  echo "drain it with the production runbook, bootout it, then rerun this installer" >&2
  exit 75
fi
if pgrep -f '/bin/mineru -p ' >/dev/null \
  || pgrep -f ' -m mineru\.cli\.fast_api ' >/dev/null; then
  echo "refusing to start beside an existing MinerU CLI/API process" >&2
  echo "verify the staged-cutover three-zero gate, then rerun this installer" >&2
  exit 75
fi
ENV_DIR="${DISCLOSURE_ENV_DIR:-$HOME/.config/agent-invest/disclosure_anchor}"
for f in worker.env cninfo.env; do
  [[ -r "$ENV_DIR/$f" ]] || { echo "missing $ENV_DIR/$f" >&2; exit 78; }
done
set -a
source "$ENV_DIR/worker.env"
source "$ENV_DIR/cninfo.env"
set +a
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
PYTHONPATH=src .venv/bin/python -c \
  'from disclosure_anchor.settings import load_settings; load_settings()'
mkdir -p "$HOME/Library/Logs/agent-invest"
disabled_snapshot="$(launchctl print-disabled "$DOMAIN")"
PRIOR_DISABLED=0
if grep -Fq '"'"$LABEL"'" => disabled' <<< "$disabled_snapshot" \
    || grep -Fq '"'"$LABEL"'" => true' <<< "$disabled_snapshot"; then
  PRIOR_DISABLED=1
elif grep -Fq '"'"$LABEL"'" => enabled' <<< "$disabled_snapshot" \
    || grep -Fq '"'"$LABEL"'" => false' <<< "$disabled_snapshot"; then
  PRIOR_DISABLED=0
elif grep -Fq '"'"$LABEL"'" =>' <<< "$disabled_snapshot"; then
  echo "unsupported launchctl disabled state for $LABEL" >&2
  exit 76
fi
TMP_PLIST="$(mktemp "${PLIST}.XXXXXX")"
BACKUP_PLIST="$(mktemp "${PLIST}.backup.XXXXXX")"
HAD_PLIST=0
PLIST_REPLACED=0
JOB_LOADED=0
COMMITTED=0
MUTATION_STARTED=0
if [[ -f "$PLIST" ]]; then
  cp -p "$PLIST" "$BACKUP_PLIST"
  HAD_PLIST=1
fi
rollback_install() {
  local exit_status="$?"
  trap - EXIT INT TERM HUP
  local rollback_failed=0
  if (( exit_status != 0 && COMMITTED == 0 && MUTATION_STARTED == 1 )); then
    if (( JOB_LOADED == 1 )) \
        || launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || rollback_failed=1
      for _ in $(seq 1 30); do
        launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
        sleep 1
      done
      if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
        rollback_failed=1
      fi
    fi
    if (( HAD_PLIST == 1 )); then
      cp -p "$BACKUP_PLIST" "$PLIST" || rollback_failed=1
    else
      rm -f "$PLIST" || rollback_failed=1
    fi
    if (( PRIOR_DISABLED == 1 )); then
      launchctl disable "$DOMAIN/$LABEL" >/dev/null 2>&1 || rollback_failed=1
    fi
    if (( rollback_failed == 0 )); then
      echo "worker launchd install failed; rollback verified" >&2
    else
      echo "worker launchd install failed; rollback incomplete" >&2
    fi
  fi
  rm -f "$TMP_PLIST" "$BACKUP_PLIST"
  exit "$exit_status"
}
trap rollback_install EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
  "$REPO/scripts/launchd/com.agentinvest.disclosure-worker.plist.template" > "$TMP_PLIST"
plutil -lint "$TMP_PLIST"
MUTATION_STARTED=1
mv "$TMP_PLIST" "$PLIST"
PLIST_REPLACED=1

PROGRESS_PATH="$DISCLOSURE_RUNTIME_ROOT/reports/progress/$(TZ=Asia/Shanghai date +%F).jsonl"
PROGRESS_SIZE_BEFORE=0
if [[ -f "$PROGRESS_PATH" ]]; then
  PROGRESS_SIZE_BEFORE="$(stat -f %z "$PROGRESS_PATH")"
fi
INSTALL_STARTED_EPOCH="$(date -u +%s)"
if (( PRIOR_DISABLED == 1 )); then
  launchctl enable "$DOMAIN/$LABEL"
fi
launchctl bootstrap "$DOMAIN" "$PLIST"
JOB_LOADED=1
launchctl kickstart "$DOMAIN/$LABEL"

HEALTH_PID=""
for _ in $(seq 1 90); do
  JOB_STATE="$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null || true)"
  if print -r -- "$JOB_STATE" | grep -q "state = running" \
      && [[ -f "$PROGRESS_PATH" ]] \
      && (( $(stat -f %z "$PROGRESS_PATH") > PROGRESS_SIZE_BEFORE )) \
      && .venv/bin/python - "$PROGRESS_PATH" "$INSTALL_STARTED_EPOCH" <<'PY'
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
started = datetime.fromtimestamp(int(sys.argv[2]), tz=timezone.utc)
last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
observed = datetime.fromisoformat(str(last.get("observed_at")))
if (
    last.get("contract_version") != "worker_progress.v2"
    or observed.astimezone(timezone.utc) < started
    or not isinstance(last.get("producer_instance_id"), str)
    or not last["producer_instance_id"]
    or not isinstance(last.get("event_id"), str)
    or not last["event_id"]
):
    raise SystemExit(1)
PY
  then
    HEALTH_PID="$(print -r -- "$JOB_STATE" | awk '/pid =/{print $3; exit}')"
    [[ "$HEALTH_PID" == <-> ]] && break
    HEALTH_PID=""
  fi
  sleep 1
done
if [[ -z "$HEALTH_PID" ]]; then
  echo "worker did not emit a fresh progress event within 90s" >&2
  exit 1
fi

# A crash-loop can briefly look running and append one event.  Require the same
# process to remain loaded for a bounded stability window.
sleep 5
JOB_STATE="$(launchctl print "$DOMAIN/$LABEL")"
print -r -- "$JOB_STATE" | grep -q "state = running"
STABLE_PID="$(print -r -- "$JOB_STATE" | awk '/pid =/{print $3; exit}')"
[[ "$STABLE_PID" == "$HEALTH_PID" ]]
EFFECTIVE_EXIT_TIMEOUT="$(
  print -r -- "$JOB_STATE" | awk '/exit timeout =/{print $4; exit}'
)"
case "$EFFECTIVE_EXIT_TIMEOUT" in
  ''|*[!0-9]*)
    echo "cannot verify loaded launchd exit timeout" >&2
    exit 1
    ;;
esac
if (( EFFECTIVE_EXIT_TIMEOUT < 60 )); then
  echo "loaded launchd exit timeout is only ${EFFECTIVE_EXIT_TIMEOUT}s" >&2
  exit 1
fi
COMMITTED=1
echo "installed: $PLIST (KeepAlive adaptive loop; idle backoff 15-30m;" \
  "fresh worker_progress.v2 observed; stable pid $STABLE_PID;" \
  "exit timeout ${EFFECTIVE_EXIT_TIMEOUT}s effective, 90s requested)"
echo "launchd log: $HOME/Library/Logs/agent-invest/disclosure-worker.{out,err}"
echo "worker log:  $DISCLOSURE_RUNTIME_ROOT/logs/worker-YYYYMMDD.log"
