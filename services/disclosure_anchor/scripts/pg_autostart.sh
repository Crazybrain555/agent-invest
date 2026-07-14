#!/bin/zsh
# Boot-time PostgreSQL starter for the launchd one-shot job (batch 4,
# 2026-07-14). Idempotent: exits 0 when the cluster is already running so a
# manual `make pg-start` and this job never fight.
set -uo pipefail

PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@18/bin}"
PGDATA="${DISCLOSURE_PGDATA:-/Volumes/AgentSSD/agent_system/postgres/pg18-main}"
PGLOG="${DISCLOSURE_PGLOG:-/Volumes/AgentSSD/agent_system/postgres/logs/disclosure-anchor-pg18.log}"

# External volume may mount a beat after login; wait briefly.
for _ in {1..30}; do
  [ -d "$PGDATA" ] && break
  sleep 2
done
if [ ! -d "$PGDATA" ]; then
  echo "[FAIL] PGDATA not found: $PGDATA (volume not mounted?)" >&2
  exit 1
fi

if "$PG_BIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
  echo "[skip] postgres already running at $PGDATA"
  exit 0
fi

if ! "$PG_BIN/pg_ctl" -D "$PGDATA" -l "$PGLOG" start; then
  echo "[FAIL] pg_ctl start failed (PGDATA=$PGDATA, log=$PGLOG)" >&2
  exit 1
fi
echo "[ok] postgres started (PGDATA=$PGDATA)"
