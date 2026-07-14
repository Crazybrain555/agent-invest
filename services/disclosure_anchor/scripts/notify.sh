#!/bin/zsh
# Minimal single-operator alert channel (batch 4, 2026-07-14): macOS
# notification with a per-title hourly rate limit so a flapping condition
# does not spam the desktop. Usage: notify.sh <title> <message>
set -euo pipefail

TITLE="${1:?usage: notify.sh <title> <message>}"
MESSAGE="${2:?usage: notify.sh <title> <message>}"

ENV_DIR="${DISCLOSURE_ENV_DIR:-$HOME/.config/agent-invest/disclosure_anchor}"
[ -r "$ENV_DIR/worker.env" ] && source "$ENV_DIR/worker.env"

MARKER_DIR="${DISCLOSURE_RUNTIME_ROOT:-/tmp}/notify-markers"
mkdir -p "$MARKER_DIR"
# Rate limit: one notification per title per hour.
MARKER="$MARKER_DIR/$(echo "$TITLE" | tr -c 'A-Za-z0-9' '_').last"
NOW=$(date +%s)
if [ -f "$MARKER" ]; then
  LAST=$(cat "$MARKER" 2>/dev/null || echo 0)
  if [ $((NOW - LAST)) -lt 3600 ]; then
    exit 0
  fi
fi

# argv-based AppleScript: no string interpolation, so quotes in the message
# cannot break the script; the rate-limit marker is written only after a
# successful notification so a failed attempt does not eat the hour
# (round23 review N1).
if /usr/bin/osascript \
    -e 'on run argv' \
    -e 'display notification (item 2 of argv) with title "disclosure_anchor" subtitle (item 1 of argv)' \
    -e 'end run' -- "$TITLE" "$MESSAGE"; then
  echo "$NOW" > "$MARKER"
fi
# Also append to a durable alert log so silenced/missed notifications remain
# discoverable after the fact.
echo "$(date '+%Y-%m-%d %H:%M:%S') [$TITLE] $MESSAGE" >> "$MARKER_DIR/alerts.log"
