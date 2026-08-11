#!/bin/zsh
# Install the ops launchd jobs (batch 4, 2026-07-14). Idempotent.
#   com.agentinvest.postgres          — boot-time one-shot pg_ctl start
#   com.agentinvest.disclosure-doctor — daily 18:30 doctor + freshness alerts
#   com.agentinvest.disclosure-gc     — daily 19:30 orphan-only artifact GC
# The resident worker job stays owned by scripts/install_launchd.sh.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DOMAIN="gui/$(id -u)"
mkdir -p "$HOME/Library/Logs/agent-invest"

for label in com.agentinvest.postgres com.agentinvest.disclosure-doctor com.agentinvest.disclosure-gc; do
  PLIST="$HOME/Library/LaunchAgents/$label.plist"
  sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
    "$REPO/scripts/launchd/$label.plist.template" > "$PLIST"
  # `enable` is the only way back from the persistent disable that the
  # destructive-reset quiescence gate writes for the GC label; legacy
  # unload/load leaves the label disabled and silently never runs it again.
  launchctl enable "$DOMAIN/$label"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  # bootout returns before launchd finishes unloading a job that is mid-run
  # (e.g. a 19:30 GC pass); bootstrap would then fail with "already in
  # progress". Drain with a bounded wait instead of failing loud on a rerun.
  for _ in $(seq 1 30); do
    launchctl print "$DOMAIN/$label" >/dev/null 2>&1 || break
    sleep 1
  done
  launchctl bootstrap "$DOMAIN" "$PLIST"
  if [[ "$label" == com.agentinvest.postgres ]]; then
    # RunAtLoad one-shot whose wrapper exits 0 on an already-running cluster,
    # so an explicit kickstart is idempotent and makes recovery deterministic.
    # The 18:30/19:30 calendar jobs are never kickstarted here: that would fire
    # a doctor sweep or orphan-GC pass at install time.
    launchctl kickstart "$DOMAIN/$label"
  fi
  echo "installed: $PLIST"
done
echo "verify: launchctl list | grep agentinvest"
