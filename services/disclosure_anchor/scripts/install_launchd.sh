#!/bin/zsh
# Install the worker launchd job (every 2h + at load). Idempotent.
set -euo pipefail
ENV_DIR="${DISCLOSURE_ENV_DIR:-$HOME/.config/agent-invest/disclosure_anchor}"
source "$ENV_DIR/worker.env"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.agentinvest.disclosure-worker.plist"
sed -e "s|__REPO__|$REPO|g" -e "s|__RUNTIME__|$DISCLOSURE_RUNTIME_ROOT|g" \
  "$REPO/scripts/launchd/com.agentinvest.disclosure-worker.plist.template" > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "installed: $PLIST (every 2h; logs in $DISCLOSURE_RUNTIME_ROOT/logs)"
