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

exit 0
