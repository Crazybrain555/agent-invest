#!/bin/zsh
# Reset every derived layer while KEEPING raw PDFs and their provenance:
# wipes units/runs/events/search projections (DB rows) and parser/derived
# files (disk), then returns all documents to 'registered' so the resident
# worker re-parses and republishes everything from the retained raw files.
# Companies, securities, the tracking pool, source accesses, and sync
# checkpoints stay untouched — nothing is re-downloaded.
# Usage: WIPE=YES ./scripts/wipe_derived_data.sh
set -euo pipefail

if [[ "${WIPE:-}" != "YES" ]]; then
  echo "refusing: set WIPE=YES to confirm a derived-data wipe" >&2
  exit 2
fi
: "${DATABASE_URL:?DATABASE_URL required}"
: "${DISCLOSURE_DATA_ROOT:?DISCLOSURE_DATA_ROOT required}"

PSQL_URL="${DATABASE_URL/postgresql+psycopg/postgresql}"

# current_processing_run_id must drop before processing_run empties (FK);
# no CASCADE anywhere so an unexpected referencing table aborts loudly.
psql "$PSQL_URL" <<'SQL'
BEGIN;
UPDATE disclosure_core.document
   SET current_processing_run_id = NULL,
       status = 'registered';
TRUNCATE
  disclosure_core.unit_search_projection,
  disclosure_core.document_unit,
  disclosure_ops.outbox_event,
  disclosure_core.processing_run;
COMMIT;
SQL

# Derived files only; raw_documents and the _phase00 fixture area stay.
for sub in derived parser_artifacts/cninfo; do
  target="$DISCLOSURE_DATA_ROOT/data/$sub"
  if [[ -d "$target" ]]; then
    rm -rf "$target"
    echo "wiped $target"
  fi
done
rm -rf "$DISCLOSURE_DATA_ROOT/runtime/quarantine" 2>/dev/null || true
rm -rf "$DISCLOSURE_DATA_ROOT/runtime/tmp" 2>/dev/null || true

psql "$PSQL_URL" -c "SELECT
  (SELECT count(*) FROM disclosure_core.document) AS documents_kept,
  (SELECT count(*) FROM disclosure_core.document WHERE raw_file_hash IS NOT NULL) AS with_raw,
  (SELECT count(*) FROM disclosure_core.document_unit) AS units,
  (SELECT count(*) FROM disclosure_core.processing_run) AS runs,
  (SELECT count(*) FROM disclosure_core.tracked_company) AS tracked"
echo "derived wipe complete — raw PDFs kept; the worker re-parses everything from disk"
