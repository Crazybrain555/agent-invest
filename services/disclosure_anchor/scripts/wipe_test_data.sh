#!/bin/zsh
# Wipe ALL business data (DB rows + on-disk archives) for a clean test round.
# TEST-PHASE TOOL ONLY: the production corpus must never be wiped casually.
# Usage: WIPE=YES ./scripts/wipe_test_data.sh
# Requires DATABASE_URL and DISCLOSURE_DATA_ROOT in the environment.
set -euo pipefail

if [[ "${WIPE:-}" != "YES" ]]; then
  echo "refusing: set WIPE=YES to confirm a full test-data wipe" >&2
  exit 2
fi
: "${DATABASE_URL:?DATABASE_URL required}"
: "${DISCLOSURE_DATA_ROOT:?DISCLOSURE_DATA_ROOT required}"

PSQL_URL="${DATABASE_URL/postgresql+psycopg/postgresql}"

psql "$PSQL_URL" <<'SQL'
BEGIN;
TRUNCATE
  disclosure_core.document_unit,
  disclosure_ops.outbox_event,
  disclosure_core.processing_run,
  disclosure_core.document,
  disclosure_core.source_access,
  disclosure_core.source_checkpoint,
  disclosure_core.tracked_company,
  disclosure_core.company_identifier,
  disclosure_core.security,
  disclosure_core.company
  CASCADE;
COMMIT;
SQL

# On-disk provider artifacts; keep _phase00 (fixture provenance) untouched.
for sub in raw_documents/cninfo derived parser_artifacts/cninfo; do
  target="$DISCLOSURE_DATA_ROOT/data/$sub"
  if [[ -d "$target" ]]; then
    rm -rf "$target"
    echo "wiped $target"
  fi
done
rm -rf "$DISCLOSURE_DATA_ROOT/runtime/quarantine" 2>/dev/null || true
rm -rf "$DISCLOSURE_DATA_ROOT/runtime/tmp" 2>/dev/null || true

psql "$PSQL_URL" -c "SELECT
  (SELECT count(*) FROM disclosure_core.company) AS companies,
  (SELECT count(*) FROM disclosure_core.document) AS documents,
  (SELECT count(*) FROM disclosure_core.document_unit) AS units,
  (SELECT count(*) FROM disclosure_core.source_access) AS accesses"
echo "wipe complete"
