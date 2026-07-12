#!/bin/zsh
# Install the worker launchd job (every 2h + at load). Idempotent.
# launchd stdout/err live in ~/Library/Logs/agent-invest (internal disk —
# external volumes are TCC-denied for launchd-spawned processes); the real
# worker log still lands under $DISCLOSURE_RUNTIME_ROOT/logs.
set -euo pipefail
ENV_DIR="${DISCLOSURE_ENV_DIR:-$HOME/.config/agent-invest/disclosure_anchor}"
source "$ENV_DIR/worker.env"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.agentinvest.disclosure-worker.plist"
mkdir -p "$HOME/Library/Logs/agent-invest"
sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
  "$REPO/scripts/launchd/com.agentinvest.disclosure-worker.plist.template" > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "installed: $PLIST (every 2h + at load)"
echo "launchd log: $HOME/Library/Logs/agent-invest/disclosure-worker.{out,err}"
echo "worker log:  $DISCLOSURE_RUNTIME_ROOT/logs/worker-YYYYMMDD.log"
