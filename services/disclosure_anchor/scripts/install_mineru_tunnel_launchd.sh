#!/bin/zsh
# Install/start the dedicated, pinned-host-key MinerU SSH forwarding agent.
set -euo pipefail

LABEL="com.agentinvest.mineru-tunnel"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_DIR="$HOME/.local/lib/agent-invest/mineru-tunnel"
RUNTIME_WRAPPER="$RUNTIME_DIR/mineru_ssh_tunnel.py"
"$REPO/.venv/bin/python" "$REPO/scripts/mineru_ssh_tunnel.py" --check

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo "refusing to replace loaded $LABEL; bootout it after proving no parse work" >&2
  exit 75
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/agent-invest" "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"
disabled_snapshot="$(launchctl print-disabled "$DOMAIN")"
if grep -Fq '"'"$LABEL"'" => disabled' <<< "$disabled_snapshot" \
    || grep -Fq '"'"$LABEL"'" => true' <<< "$disabled_snapshot"; then
  PRIOR_DISABLED=1
elif grep -Fq '"'"$LABEL"'" => enabled' <<< "$disabled_snapshot" \
    || grep -Fq '"'"$LABEL"'" => false' <<< "$disabled_snapshot"; then
  PRIOR_DISABLED=0
elif grep -Fq '"'"$LABEL"'" =>' <<< "$disabled_snapshot"; then
  echo "unsupported launchctl disabled state for $LABEL" >&2
  exit 76
else
  PRIOR_DISABLED=0
fi
TMP_PLIST="$(mktemp "${PLIST}.XXXXXX")"
PLIST_BACKUP=""
WRAPPER_BACKUP=""
if [[ -f "$PLIST" ]]; then
  PLIST_BACKUP="${PLIST}.pre-mineru-tunnel.$(date -u +%Y%m%dT%H%M%SZ).bak"
  cp -p "$PLIST" "$PLIST_BACKUP"
fi
if [[ -f "$RUNTIME_WRAPPER" ]]; then
  WRAPPER_BACKUP="${RUNTIME_WRAPPER}.pre-mineru-tunnel.$(date -u +%Y%m%dT%H%M%SZ).bak"
  cp -p "$RUNTIME_WRAPPER" "$WRAPPER_BACKUP"
fi
INSTALL_COMMITTED=0
MUTATION_STARTED=0

finish_install() {
  local exit_code=$?
  trap - EXIT INT TERM HUP
  rm -f "$TMP_PLIST"
  if (( INSTALL_COMMITTED == 0 && MUTATION_STARTED == 1 )); then
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    fi
    if [[ -n "$PLIST_BACKUP" ]]; then
      cp -p "$PLIST_BACKUP" "$PLIST"
    else
      rm -f "$PLIST"
    fi
    if [[ -n "$WRAPPER_BACKUP" ]]; then
      cp -p "$WRAPPER_BACKUP" "$RUNTIME_WRAPPER"
    else
      rm -f "$RUNTIME_WRAPPER"
    fi
    if (( PRIOR_DISABLED == 1 )); then
      launchctl disable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    fi
  fi
  exit "$exit_code"
}
trap finish_install EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

MUTATION_STARTED=1
install -m 600 "$REPO/scripts/mineru_ssh_tunnel.py" "$RUNTIME_WRAPPER"
/usr/bin/python3 "$RUNTIME_WRAPPER" --check

sed -e "s|__RUNTIME_WRAPPER__|$RUNTIME_WRAPPER|g" -e "s|__HOME__|$HOME|g" \
  "$REPO/scripts/launchd/$LABEL.plist.template" > "$TMP_PLIST"
plutil -lint "$TMP_PLIST"
mv "$TMP_PLIST" "$PLIST"
chmod 600 "$PLIST"
if (( PRIOR_DISABLED == 1 )); then
  launchctl enable "$DOMAIN/$LABEL"
fi
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart "$DOMAIN/$LABEL"

for _ in $(seq 1 30); do
  if /usr/bin/curl --fail --silent --show-error --noproxy '*' --max-redirs 0 \
      --max-time 5 \
      http://127.0.0.1:30002/health >/dev/null \
    && /usr/bin/curl --fail --silent --show-error --noproxy '*' --max-redirs 0 \
      --max-time 5 \
      http://127.0.0.1:30001/health >/dev/null \
    && /usr/bin/curl --fail --silent --show-error --noproxy '*' --max-redirs 0 \
      --max-time 5 \
      http://127.0.0.1:30004/metrics >/dev/null; then
    launchctl print "$DOMAIN/$LABEL" | grep -q 'state = running'
    INSTALL_COMMITTED=1
    echo "installed: $PLIST"
    exit 0
  fi
  sleep 1
done

echo "tunnel job loaded but one or more remote endpoints were not ready" >&2
exit 1
