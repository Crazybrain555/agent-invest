#!/bin/zsh
# Daily unattended retirement of superseded derived generations: per
# document the newest superseded run stays as
# rollback insurance; anything older is retired through the guarded
# manifest-driven flow (retire_derived_generation.py --auto). Raw PDFs,
# lineage, and any relpath shared with a live run are untouchable by
# construction. Scheduled by com.agentinvest.disclosure-gc (launchd, 19:30
# daily, after the 18:30 doctor sweep).
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV_DIR="${DISCLOSURE_ENV_DIR:-$HOME/.config/agent-invest/disclosure_anchor}"
set -a
for f in worker.env cninfo.env; do
  [ -r "$ENV_DIR/$f" ] && source "$ENV_DIR/$f"
done
set +a

NOTIFY="$REPO/scripts/notify.sh"

GC_OUT=$(cd "$REPO" && .venv/bin/python scripts/retire_derived_generation.py --auto 2>&1)
GC_EXIT=$?
echo "$GC_OUT"
if [ "$GC_EXIT" -ne 0 ]; then
  "$NOTIFY" "derived-generation GC FAIL" \
    "exit=$GC_EXIT — see ~/Library/Logs/agent-invest/disclosure-gc.out and audit/gc manifests"
fi

# Retirement works run by run, so artifacts whose run is already gone (every
# full-corpus rebuild leaves some) were never anybody's job. doctor only
# WARNed about the growing pile; nothing collected it.
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
