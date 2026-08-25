#!/bin/zsh
# Daily orphan-only collection for derived files that no processing run owns.
# Active and historical runs, units, outbox records, and raw PDFs are never
# retired by this job. Scheduled by com.agentinvest.disclosure-gc (launchd,
# 19:30 daily, after the 18:30 doctor sweep).
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV_DIR="${DISCLOSURE_ENV_DIR:-$HOME/.config/agent-invest/disclosure_anchor}"
set -a
for f in worker.env cninfo.env; do
  [ -r "$ENV_DIR/$f" ] && source "$ENV_DIR/$f"
done
set +a

NOTIFY="$REPO/scripts/notify.sh"

# Only files whose DB owner no longer exists can enter this collector. The
# 24-hour age guard and ownership recheck remain independent fail-closed gates.
ORPHAN_OUT=$(cd "$REPO" && .venv/bin/python scripts/gc_orphan_artifacts.py --apply 2>&1)
ORPHAN_EXIT=$?
echo "$ORPHAN_OUT"
if [ "$ORPHAN_EXIT" -ne 0 ]; then
  "$NOTIFY" "orphan-artifact GC FAIL" "exit=$ORPHAN_EXIT — see disclosure-gc.out"
fi

# Bounded retention for everything this stack appends to forever. All of it
# is diagnostic history, and the audit manifests share the data volume whose
# exhaustion takes PostgreSQL down with it.
if [ -n "${DISCLOSURE_DATA_ROOT:-}" ] && [ -d "$DISCLOSURE_DATA_ROOT/audit/gc" ]; then
  MANIFESTS=$(find "$DISCLOSURE_DATA_ROOT/audit/gc" -type f -mtime +30 | wc -l | tr -d ' ')
  echo "[retention] pruning $MANIFESTS gc manifests older than 30d"
  find "$DISCLOSURE_DATA_ROOT/audit/gc" -type f -mtime +30 -delete 2>/dev/null || true
fi
for sub in reports/worker reports/parse_quality; do
  dir="${DISCLOSURE_DATA_ROOT:-}/$sub"
  [ -d "$dir" ] && find "$dir" -type f -name '*.md' -mtime +30 -delete 2>/dev/null || true
done
LOGDIR="${DISCLOSURE_DATA_ROOT:-}/runtime/logs"
[ -d "$LOGDIR" ] && find "$LOGDIR" -type f -name 'worker-*.log' -mtime +14 -delete 2>/dev/null || true
# launchd writes these two without rotation; truncating beats unbounded growth
# on the internal disk, which no disk check watches.
for f in "$HOME/Library/Logs/agent-invest/disclosure-worker.out" \
         "$HOME/Library/Logs/agent-invest/disclosure-worker.err"; do
  if [ -f "$f" ] && [ "$(stat -f%z "$f" 2>/dev/null || echo 0)" -gt 104857600 ]; then
    echo "[retention] truncating oversized $f"
    : > "$f"
  fi
done

exit 0
