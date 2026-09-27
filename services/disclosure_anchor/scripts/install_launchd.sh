#!/bin/zsh
# Install the resident adaptive worker launchd job. A loaded job must be
# drained and booted out explicitly; installation never interrupts live work.
# launchd stdout/err live in ~/Library/Logs/agent-invest (internal disk —
# external volumes are TCC-denied for launchd-spawned processes); the real
# worker log still lands under $DISCLOSURE_RUNTIME_ROOT/logs.
#
# The installer never releases a worker public stop and never implicitly
# re-enables a disabled label: a recorded/invalid/unverifiable stop refuses
# with 78, and a disabled label is enabled only with the explicit
# --confirm-operator-disabled confirmation that it is an ordinary maintenance
# disable (not a native-only public stop whose record failed).
#
# Before any plist or launchd mutation the read-only `worker
# deployment-preflight` runs the resident worker's own deployment checker (and,
# in staged-v4, its resolver identity closure and legacy scope); not ready
# refuses with 78. Pass the provider's deployed idempotency-key lifetime (its
# key TTL, not task retention) with --prepared-key-ttl-seconds N when
# never-accepted heads exist; an unknown lifetime is never assumed.
set -euo pipefail
CONFIRM_OPERATOR_DISABLED=0
PREPARED_KEY_TTL_SECONDS=""
USAGE="usage: $0 [--confirm-operator-disabled] [--prepared-key-ttl-seconds N]"
while (( $# > 0 )); do
  case "$1" in
    --confirm-operator-disabled) CONFIRM_OPERATOR_DISABLED=1 ;;
    --prepared-key-ttl-seconds)
      if (( $# < 2 )) || [[ ! "$2" =~ '^[1-9][0-9]*$' ]]; then
        echo "$USAGE" >&2; exit 64
      fi
      PREPARED_KEY_TTL_SECONDS="$2"
      shift ;;
    *) echo "$USAGE" >&2; exit 64 ;;
  esac
  shift
done
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
PYTHONPATH=src .venv/bin/python - <<'PY'
from disclosure_anchor.settings import load_settings

settings = load_settings()
if settings.worker_parse_execution_mode == "staged-v4":
    from disclosure_anchor.adapters.security.provider_secret_keyring import (
        load_provider_secret_keyring_from_settings,
    )

    # Validate the actual private keyring before changing launchd state. Basic
    # Settings validation alone cannot detect a missing or unreadable key file.
    load_provider_secret_keyring_from_settings(settings)
PY
# The worker start gate would refuse anyway; refuse here, before any plist or
# launchd mutation, so an install can never look like a release.
PYTHONPATH=src .venv/bin/python - <<'PY'
import sys

from disclosure_anchor.adapters.runtime.worker_stop_control import (
    EXIT_PUBLIC_STOP,
    require_worker_start_permitted,
)
from disclosure_anchor.application.ports.worker_stop_control import (
    WorkerOperationalStopError,
)
from disclosure_anchor.settings import load_settings

try:
    require_worker_start_permitted(load_settings())
except WorkerOperationalStopError as exc:
    if exc.state == "OPERATOR_DISABLED":
        # No record exists; the disabled label itself is decided below, only
        # with the explicit --confirm-operator-disabled confirmation.
        raise SystemExit(0)
    print(f"refusing to install: {exc}", file=sys.stderr)
    print("inspect `make worker-control-status`; follow the production runbook "
          "(repair control storage, or release the exact stop) before installing",
          file=sys.stderr)
    raise SystemExit(EXIT_PUBLIC_STOP)
PY
# Technical install eligibility, never a start authorization: the resident
# worker's own static checker in every mode, plus (staged-v4) the resolver
# identity closure and legacy scope. The worker rechecks under its singleton
# before any business effect.
PREFLIGHT_ARGS=(--format terminal)
if [[ -n "$PREPARED_KEY_TTL_SECONDS" ]]; then
  PREFLIGHT_ARGS+=(--prepared-key-ttl-seconds "$PREPARED_KEY_TTL_SECONDS")
fi
if ! PYTHONPATH=src .venv/bin/python -m disclosure_anchor.cli.worker \
    deployment-preflight "${PREFLIGHT_ARGS[@]}"; then
  echo "refusing to install: the worker deployment preflight is not ready" >&2
  echo "resolve every BLOCKER above (see the production runbook), then rerun this installer" >&2
  exit 78
fi
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
if (( PRIOR_DISABLED == 1 && CONFIRM_OPERATOR_DISABLED == 0 )); then
  echo "refusing to re-enable disabled $LABEL implicitly" >&2
  echo "a public stop whose record failed also leaves only this native disable;" \
    "check the worker log for STOP_PERSISTENCE_FAILED and reconstruct/release it" \
    "(make worker-record-circuit-stop, make worker-release-circuit)." >&2
  echo "if this is an ordinary maintenance disable, rerun with" \
    "--confirm-operator-disabled" >&2
  exit 78
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
echo "installed: $PLIST (adaptive loop, restarted only after exit 0;" \
  "idle backoff 15-30m; fresh worker_progress.v2 observed; stable pid $STABLE_PID;" \
  "exit timeout ${EFFECTIVE_EXIT_TIMEOUT}s effective, 90s requested)"
echo "launchd log: $HOME/Library/Logs/agent-invest/disclosure-worker.{out,err}"
echo "worker log:  $DISCLOSURE_RUNTIME_ROOT/logs/worker-YYYYMMDD.log"
