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
TMP_PLIST="$(mktemp "${PLIST}.XXXXXX")"
trap 'rm -f "$TMP_PLIST"' EXIT
sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
  "$REPO/scripts/launchd/com.agentinvest.disclosure-worker.plist.template" > "$TMP_PLIST"
plutil -lint "$TMP_PLIST"
mv "$TMP_PLIST" "$PLIST"
launchctl enable "$DOMAIN/$LABEL"
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart "$DOMAIN/$LABEL"
JOB_STATE="$(launchctl print "$DOMAIN/$LABEL")"
print -r -- "$JOB_STATE" | grep -q "state = running"
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
echo "installed: $PLIST (KeepAlive adaptive loop; idle backoff 15-30m;" \
  "exit timeout ${EFFECTIVE_EXIT_TIMEOUT}s effective, 90s requested)"
echo "launchd log: $HOME/Library/Logs/agent-invest/disclosure-worker.{out,err}"
echo "worker log:  $DISCLOSURE_RUNTIME_ROOT/logs/worker-YYYYMMDD.log"
