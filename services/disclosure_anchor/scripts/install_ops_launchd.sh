#!/bin/zsh
# Install the ops launchd jobs (batch 4, 2026-07-14). Idempotent.
#   com.agentinvest.postgres          — boot-time one-shot pg_ctl start
#   com.agentinvest.disclosure-doctor — daily 18:30 doctor + freshness alerts
# The resident worker job stays owned by scripts/install_launchd.sh.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$HOME/Library/Logs/agent-invest"

for label in com.agentinvest.postgres com.agentinvest.disclosure-doctor; do
  PLIST="$HOME/Library/LaunchAgents/$label.plist"
  sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
    "$REPO/scripts/launchd/$label.plist.template" > "$PLIST"
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "installed: $PLIST"
done
echo "verify: launchctl list | grep agentinvest"
