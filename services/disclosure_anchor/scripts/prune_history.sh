#!/bin/zsh
# TEST-PHASE ONLY: keep exactly the active generation in the database.
# Deletes non-active processing runs, their document_units, and outbox events
# that reference the pruned runs/assets. U5 historical replay for pruned runs
# is intentionally given up during the test phase (user decision 2026-07-06);
# production retention policy is a separate post-launch decision.
set -euo pipefail
: "${DATABASE_URL:?DATABASE_URL required}"
PSQL_URL="${DATABASE_URL/postgresql+psycopg/postgresql}"

psql "$PSQL_URL" <<'SQL'
BEGIN;
CREATE TEMP TABLE pruned_runs AS
  SELECT processing_run_id FROM disclosure_core.processing_run
  WHERE NOT is_active AND status <> 'running';
CREATE TEMP TABLE pruned_assets AS
  SELECT asset_id FROM disclosure_core.document_unit
  WHERE processing_run_id IN (SELECT processing_run_id FROM pruned_runs);
DELETE FROM disclosure_ops.outbox_event
  WHERE (subject_kind = 'processing_run' AND subject_ref IN (SELECT processing_run_id FROM pruned_runs))
     OR (subject_kind = 'document_unit' AND subject_ref IN (SELECT asset_id FROM pruned_assets))
     OR (processing_run_id IS NOT NULL AND processing_run_id IN (SELECT processing_run_id FROM pruned_runs));
DELETE FROM disclosure_core.document_unit
  WHERE processing_run_id IN (SELECT processing_run_id FROM pruned_runs);
DELETE FROM disclosure_core.processing_run
  WHERE processing_run_id IN (SELECT processing_run_id FROM pruned_runs);
COMMIT;
SQL

psql "$PSQL_URL" -c "SELECT
  (SELECT count(*) FROM disclosure_core.processing_run) AS runs,
  (SELECT count(*) FROM disclosure_core.processing_run WHERE is_active) AS active_runs,
  (SELECT count(*) FROM disclosure_core.document_unit) AS units,
  (SELECT count(*) FROM disclosure_ops.outbox_event) AS outbox_events"
echo "prune complete: database now holds only the active generation"
