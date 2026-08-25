#!/bin/zsh
# Transactionally install the three non-worker launchd jobs. Existing loaded
# jobs must be quiesced explicitly; installation never interrupts live work.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DOMAIN="gui/$(id -u)"
LABELS=(
  com.agentinvest.postgres
  com.agentinvest.disclosure-doctor
  com.agentinvest.disclosure-gc
)
mkdir -p "$HOME/Library/Logs/agent-invest"

for label in $LABELS; do
  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    echo "refusing to replace loaded $label" >&2
    echo "verify it is idle, bootout it explicitly, then rerun" >&2
    exit 75
  fi
done

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/disclosure-ops-launchd.XXXXXX")"
typeset -A HAD_PLIST PRIOR_DISABLED INSTALLED
COMMITTED=0
SNAPSHOT_COMPLETE=0
MUTATION_STARTED=0

cleanup_install() {
  local exit_status="$?"
  trap - EXIT INT TERM HUP
  local rollback_failed=0
  if (( exit_status != 0 && COMMITTED == 0 && MUTATION_STARTED == 1 )); then
    for label in $LABELS; do
      if [[ "${INSTALLED[$label]:-0}" == 1 ]] \
          || launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
        launchctl bootout "$DOMAIN/$label" >/dev/null 2>&1 || rollback_failed=1
        for _ in $(seq 1 30); do
          launchctl print "$DOMAIN/$label" >/dev/null 2>&1 || break
          sleep 1
        done
        if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
          rollback_failed=1
        fi
      fi
    done
    for label in $LABELS; do
      local plist="$HOME/Library/LaunchAgents/$label.plist"
      local backup="$WORK_DIR/$label.backup.plist"
      if [[ "${HAD_PLIST[$label]:-0}" == 1 ]]; then
        cp -p "$backup" "$plist" || rollback_failed=1
      else
        rm -f "$plist" || rollback_failed=1
      fi
      if [[ "${PRIOR_DISABLED[$label]:-0}" == 1 ]]; then
        launchctl disable "$DOMAIN/$label" >/dev/null 2>&1 || rollback_failed=1
      fi
    done
    if (( rollback_failed == 0 )); then
      echo "ops launchd install failed; rollback verified" >&2
    else
      echo "ops launchd install failed; rollback incomplete" >&2
    fi
  elif (( exit_status != 0 && COMMITTED == 0 )); then
    if (( SNAPSHOT_COMPLETE == 1 )); then
      echo "ops launchd install failed before formal mutation; snapshot discarded" >&2
    else
      echo "ops launchd install failed while snapshotting; formal state untouched" >&2
    fi
  fi
  for artifact in "$WORK_DIR"/*.plist(N); do
    rm -f "$artifact" || true
  done
  rm -f "$WORK_DIR/launchctl-disabled.snapshot" || true
  rmdir "$WORK_DIR" 2>/dev/null || true
  exit "$exit_status"
}
trap cleanup_install EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

# Snapshot every formal target before any fallible render.  Cleanup must never
# infer missing snapshot entries as "plist absent / label enabled".
disabled_snapshot="$WORK_DIR/launchctl-disabled.snapshot"
launchctl print-disabled "$DOMAIN" > "$disabled_snapshot"
for label in $LABELS; do
  plist="$HOME/Library/LaunchAgents/$label.plist"
  if [[ -f "$plist" ]]; then
    cp -p "$plist" "$WORK_DIR/$label.backup.plist"
    HAD_PLIST[$label]=1
  else
    HAD_PLIST[$label]=0
  fi
  if grep -Fq '"'"$label"'" => disabled' "$disabled_snapshot" \
      || grep -Fq '"'"$label"'" => true' "$disabled_snapshot"; then
    PRIOR_DISABLED[$label]=1
  elif grep -Fq '"'"$label"'" => enabled' "$disabled_snapshot" \
      || grep -Fq '"'"$label"'" => false' "$disabled_snapshot"; then
    PRIOR_DISABLED[$label]=0
  elif grep -Fq '"'"$label"'" =>' "$disabled_snapshot"; then
    echo "unsupported launchctl disabled state for $label" >&2
    exit 76
  else
    PRIOR_DISABLED[$label]=0
  fi
done
SNAPSHOT_COMPLETE=1

# Render and lint all definitions before the first formal mutation.
for label in $LABELS; do
  candidate="$WORK_DIR/$label.candidate.plist"
  sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
    "$REPO/scripts/launchd/$label.plist.template" > "$candidate"
  plutil -lint "$candidate"
done

MUTATION_STARTED=1
for label in $LABELS; do
  plist="$HOME/Library/LaunchAgents/$label.plist"
  mv "$WORK_DIR/$label.candidate.plist" "$plist"
  if [[ "${PRIOR_DISABLED[$label]}" == 1 ]]; then
    launchctl enable "$DOMAIN/$label"
  fi
  INSTALLED[$label]=1
  launchctl bootstrap "$DOMAIN" "$plist"
done

# PostgreSQL is an idempotent one-shot. Calendar doctor/GC jobs are not fired
# merely because their definitions were installed.
launchctl kickstart "$DOMAIN/com.agentinvest.postgres"
for label in $LABELS; do
  launchctl print "$DOMAIN/$label" >/dev/null
done
COMMITTED=1

for label in $LABELS; do
  echo "installed: $HOME/Library/LaunchAgents/$label.plist"
done
echo "verify: launchctl print $DOMAIN/com.agentinvest.postgres"
echo "verify: launchctl print $DOMAIN/com.agentinvest.disclosure-doctor"
echo "verify: launchctl print $DOMAIN/com.agentinvest.disclosure-gc"
