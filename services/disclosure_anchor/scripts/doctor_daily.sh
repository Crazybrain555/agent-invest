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
fi

# 2. Freshness (采集服务第一告警): on a trading day (Mon-Fri) after 18:00,
#    zero document_registered events in the last 24h means the whole intake
#    path silently stalled (upstream ban, worker hang, DB fault all present
#    the same way).
DOW=$(date +%u)   # 1..7, Mon=1
HOUR=$(date +%H)
if [ "$DOW" -le 5 ] && [ "$HOUR" -ge 18 ]; then
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
