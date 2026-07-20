#!/bin/zsh
# Daily unattended health sweep (batch 4, 2026-07-14). Runs the sampled
# doctor, then a freshness rule, and raises a macOS notification through
# notify.sh on anything an operator should look at. Scheduled by
# com.agentinvest.disclosure-doctor (launchd, 18:30 daily).
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV_DIR="${DISCLOSURE_ENV_DIR:-$HOME/.config/agent-invest/disclosure_anchor}"
set -a
for f in worker.env cninfo.env; do
  [ -r "$ENV_DIR/$f" ] && source "$ENV_DIR/$f"
done
set +a

NOTIFY="$REPO/scripts/notify.sh"

# 1. Sampled doctor: FAIL (nonzero exit) or any [FAIL] line alerts. WARN
#    lines stay advisory by design (doctor exit semantics are pinned), but a
#    daily summary of WARN count is included in the alert log for trends.
DOCTOR_OUT=$(cd "$REPO" && PYTHONPATH=src .venv/bin/python -m disclosure_anchor.cli.doctor 2>&1)
DOCTOR_EXIT=$?
FAIL_COUNT=$(echo "$DOCTOR_OUT" | grep -c '^\[FAIL\]' || true)
WARN_COUNT=$(echo "$DOCTOR_OUT" | grep -c '^\[WARN\]' || true)
if [ "$DOCTOR_EXIT" -ne 0 ] || [ "$FAIL_COUNT" -gt 0 ]; then
  "$NOTIFY" "doctor FAIL" "exit=$DOCTOR_EXIT fail=$FAIL_COUNT warn=$WARN_COUNT — run make doctor-full"
else
  # A few WARN families are the early edge of an outage rather than advice:
  # a volume that fills stops PostgreSQL outright, and artifacts/dead
  # letters/stale runs that keep growing mean a stage quietly stopped
  # draining. They keep doctor's exit code (pinned) but must still reach
  # an operator during an unattended multi-week backfill.
  CRITICAL_WARNS=$(echo "$DOCTOR_OUT" | grep -E '^\[WARN\].*(disk|free space|orphan parser artifacts|parse dead letters|stale runs)' || true)
  if [ -n "$CRITICAL_WARNS" ]; then
    "$NOTIFY" "doctor WARN (actionable)" "$(echo "$CRITICAL_WARNS" | head -3 | tr '\n' ' ')"
  fi
fi

# 2. Freshness (采集服务第一告警): after 18:00, zero document_registered
#    events in the last 24h means the whole intake path silently stalled
#    (upstream ban, worker hang, DB fault all present the same way). The
#    weekday gate is gone: a backfill runs through weekends, and a Friday
#    evening stall used to stay invisible until Monday.
HOUR=$(date +%H)
if [ "$HOUR" -ge 18 ]; then
  PSQL_URL=$(echo "${DATABASE_URL:-}" | sed 's|+psycopg||')
  if [ -n "$PSQL_URL" ]; then
    REGISTERED=$(psql "$PSQL_URL" -X -A -t -c \
      "SELECT count(*) FROM disclosure_ops.outbox_event
        WHERE event_kind='document_registered'
          AND created_at > now() - interval '24 hours'" 2>/dev/null || echo "query-failed")
    if [ "$REGISTERED" = "query-failed" ]; then
      "$NOTIFY" "freshness check failed" "cannot query outbox_event — is PostgreSQL up?"
    elif [ "$REGISTERED" = "0" ]; then
      "$NOTIFY" "no new filings in 24h" "0 document_registered events — check worker/report and CNINFO reachability"
    fi
  fi
fi

exit 0
