ifeq ($(shell test -x .venv/bin/python && echo yes),yes)
PYTHON ?= .venv/bin/python
else
PYTHON ?= /opt/miniconda3/bin/python3
endif
PYTHONPATH ?= src
API_HOST ?= 127.0.0.1
API_PORT ?= 8711
ARCHIVE_DIR ?= dist
ARCHIVE_NAME ?= disclosure-anchor-source.tar.gz

# Local PostgreSQL cluster (Homebrew postgresql@18, PGDATA on AgentSSD).
# Override on the command line or via the environment; never hard-code secrets.
PG_BIN ?= /opt/homebrew/opt/postgresql@18/bin
PGDATA ?= /Volumes/AgentSSD/agent_system/postgres/pg18-main
PGSOCKET_DIR ?= /Volumes/AgentSSD/agent_system/postgres/sockets
PGLOG ?= /Volumes/AgentSSD/agent_system/postgres/logs/disclosure-anchor-pg18.log
PGPORT ?= 55432

# Machine-local env (roots/DB/credentials) lives OUTSIDE the repo; DB-touching
# targets auto-load it (same files run_worker_once.sh sources) so a bare
# `make track` works in a fresh shell. Values are exported silently.
DISCLOSURE_ENV_DIR ?= $(HOME)/.config/agent-invest/disclosure_anchor
WITH_ENV = set -a; for f in worker.env cninfo.env; do [ -r "$(DISCLOSURE_ENV_DIR)/$$f" ] && . "$(DISCLOSURE_ENV_DIR)/$$f"; done; set +a;

.PHONY: doctor test test-unit test-contract test-data test-integration \
	test-mineru-smoke lint typecheck agent-check api archive pg-init pg-start pg-stop \
	pg-status db-create migrate register parse build-units publish process rebuild-units rebuild-search-projection retire-derived load-rules config-check track track-status track-export untrack purge-company sync worker-once worker-loop doctor-full \
	worker-status worker-restart gc-orphans audit-unmapped-codes audit-unit-corpus export-unit-corpus-manifest audit-weekly install-ops-launchd export-reparse-manifest verify-reparse-manifest reparse-status reparse-corpus reset-derived-rehearse wipe-derived \
	create-reset-backup prove-reset-backup purge-reset-trash purge-reset-trash-force

doctor:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.doctor

lint:
	$(PYTHON) -m ruff check src tests scripts

typecheck:
	$(PYTHON) -m mypy

# Aggregate deterministic gate for AI-agent edits: lint + types + no-DB tests +
# diff hygiene in one command. Live-DB tests and migration round trips stay in
# the milestone protocol (04R section 6).
agent-check:
	$(MAKE) lint
	$(MAKE) typecheck
	env -u DATABASE_URL -u DISCLOSURE_MIGRATION_DATABASE_URL $(MAKE) test
	$(PYTHON) scripts/audit_test_composition.py
	git diff --check

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -t . -p 'test_*.py'

test-unit:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests/unit -p 'test_*.py'

test-contract:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests/contract -t . -p 'test_*.py'

test-data:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests/sample_corpus -t . -p 'test_*.py'

test-integration:
	@$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tests.integration._runner

# Explicit opt-in real-MinerU end-to-end smoke (not collected by `make test`).
# Requires a local PostgreSQL cluster, DISCLOSURE_MINERU_BIN and the local
# short_announcement sample PDF. The runner always uses a disposable database.
test-mineru-smoke:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tests.integration._runner \
		--real-mineru -v tests.integration.smoke_real_mineru

api:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m uvicorn disclosure_anchor.main:create_app --factory --host $(API_HOST) --port $(API_PORT)

archive:
	mkdir -p "$(ARCHIVE_DIR)"
	git archive --format=tar.gz --output="$(ARCHIVE_DIR)/$(ARCHIVE_NAME)" HEAD
	@echo "[ok] wrote $(ARCHIVE_DIR)/$(ARCHIVE_NAME) from tracked files in HEAD"

# --- PostgreSQL cluster lifecycle (explicit pg_ctl; never brew services) ------

pg-init:
	@if [ -f "$(PGDATA)/PG_VERSION" ]; then \
		echo "[skip] cluster already initialized at $(PGDATA)"; \
	else \
		$(PG_BIN)/initdb -D "$(PGDATA)" -U disclosure_anchor; \
		echo "[ok] initialized $(PGDATA); set port/listen_addresses/unix_socket_directories before starting"; \
	fi

pg-start:
	$(PG_BIN)/pg_ctl -D "$(PGDATA)" -l "$(PGLOG)" start

pg-stop:
	$(PG_BIN)/pg_ctl -D "$(PGDATA)" stop

pg-status:
	$(PG_BIN)/pg_ctl -D "$(PGDATA)" status

# --- Database bootstrap and migrations ---------------------------------------

db-create:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.db create

migrate:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m alembic upgrade head

# --- Single-document pipeline ------------------------------------------------

register:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline register --file "$(FILE)" --provider "$(PROVIDER)" --security-code "$(SECURITY_CODE)" --exchange "$(EXCHANGE)" --filing-type "$(FILING_TYPE)" --title "$(TITLE)" --announcement-date "$(ANNOUNCEMENT_DATE)" $(if $(REPORT_PERIOD),--report-period "$(REPORT_PERIOD)") $(if $(PROVIDER_DOCUMENT_ID),--provider-document-id "$(PROVIDER_DOCUMENT_ID)") $(if $(COMPANY_LEGAL_NAME),--company-legal-name "$(COMPANY_LEGAL_NAME)")

parse:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline parse --document-id "$(DOC)"

build-units:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline build-units --document-id "$(DOC)"

publish:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline publish --processing-run-id "$(RUN)" $(if $(ALLOW_EMPTY),--allow-empty) $(if $(REASON),--reason "$(REASON)")

process:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline process --document-id "$(DOC)" $(if $(ALLOW_EMPTY),--allow-empty) $(if $(REASON),--reason "$(REASON)")

# Rules-only iteration: reuse the latest succeeded parse artifacts, no MinerU.
rebuild-units:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline rebuild-units --document-id "$(DOC)" $(if $(ALLOW_EMPTY),--allow-empty) $(if $(REASON),--reason "$(REASON)")

# Rebuild the 06R derived search projection (U7): delta by default, ALL=YES for a
# full recompute + orphan prune after a retrieval rules/dictionary version bump.
rebuild-search-projection:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline rebuild-search-projection $(if $(filter YES,$(ALL)),--all)

# Review-first, DB-first retirement. The first invocation writes a manifest;
# APPLY=YES requires that exact reviewed manifest and atomically removes only
# its still-retirable DB ownership. Files are collected later by gc-orphans.
retire-derived:
	@test -n "$(BEFORE)" || (echo 'usage: make retire-derived BEFORE=<ISO8601> [MANIFEST=/path/retire.json] [APPLY=YES]' && exit 2)
	@test "$(APPLY)" != "YES" -o -n "$(MANIFEST)" || (echo 'APPLY=YES requires MANIFEST=/path/to/reviewed-manifest.json' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/retire_derived_generation.py \
		--before "$(BEFORE)" $(if $(MANIFEST),--manifest "$(MANIFEST)") \
		$(if $(filter YES,$(APPLY)),--apply)

# Load class_map.json + facet_map.json + filing_type_map.json into
# disclosure_core.classification_rule.
# Vocabulary upgrade = edit JSON + bump version + run this (views derive live).
load-rules:
	$(WITH_ENV) PYTHONPATH=src .venv/bin/python scripts/load_classification_rules.py

# Offline validation of config/watchlist.csv + config/processing_policy.json
# (HA check_config pattern): file/line errors, no DB access.
config-check:
	PYTHONPATH=src .venv/bin/python scripts/config_check.py

# Import companies into tracked_company (the DB is the pool's source of
# truth; the CSV is the import/seed + git-snapshot format). Offline +
# idempotent; the worker backfills new companies on its next round.
track: config-check
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline track $(if $(FILE),--file "$(FILE)") $(if $(CODES),--codes "$(CODES)") $(if $(PRUNE_DRIFT),--prune-drift) $(if $(DRY_RUN),--dry-run)

track-status:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline track-status

# Snapshot the DB pool back into config/watchlist.csv (git point-in-time
# backup; round-trips with `make track`). OUT=path overrides the target.
track-export:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline track-export --out "$(or $(OUT),config/watchlist.csv)"

# Remove companies from the pool (deletes tracked rows; company/documents
# stay). Reversible stop is status=paused via track/PUT instead.
untrack:
	@test -n "$(CODES)" || (echo 'usage: make untrack CODES=600887[,601318]' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline untrack --codes "$(CODES)"

# TEST-PHASE ONLY: corpus-gated cascade delete of one company (rows + files)
# for undoing a specifically identified mistake or test residue.
purge-company:
	@test "$(PURGE)" = "YES" || (echo 'usage: make purge-company CODE=600887 PURGE=YES  (test-phase only!)' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline purge-company --code "$(CODE)" $(if $(EXCHANGE),--exchange "$(EXCHANGE)") --yes

sync:
	@test -n "$(COMPANY)" || (echo 'usage: make sync COMPANY=<scode> [WINDOW=N]' && exit 2); \
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline sync --company $(COMPANY) $(if $(WINDOW),--window $(WINDOW))

worker-once:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.worker once

worker-loop:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.worker loop

# Resident worker control. kickstart reloads code/env only for a job whose
# installed plist is already current; plist changes require the staged
# drain/bootout/install procedure in the production runbook.
worker-status:
	@launchctl print gui/$$(id -u)/com.agentinvest.disclosure-worker 2>/dev/null \
		| grep -E "state|pid|last exit" || echo "[warn] worker job not loaded"
	@echo "--- latest report ---"
	@$(WITH_ENV) tail -n 30 "$$DISCLOSURE_RUNTIME_ROOT/reports/worker/$$(date +%F).md" 2>/dev/null \
		|| echo "[note] no report for today yet"

worker-restart:
	launchctl kickstart -k gui/$$(id -u)/com.agentinvest.disclosure-worker
	@sleep 5
	@launchctl print gui/$$(id -u)/com.agentinvest.disclosure-worker | grep -E "state|pid" || true
	@echo "[note] code/env restarted; plist was not reloaded"
	@echo "[note] run 'make doctor-full' and watch one round before walking away"

# Orphan derived-artifact inventory for parser output, normalized IR, and unit
# snapshots (dry-run; APPLY=YES deletes with an audit manifest). Raw documents
# are never touched.
gc-orphans:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/gc_orphan_artifacts.py $(if $(filter YES,$(APPLY)),--apply)

# --- Data-quality audits (batch 4: previously script-only, no make entry) ----

audit-unmapped-codes:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/audit_unmapped_codes.py

# Deterministic file-only replay gate. Export is a separate read-only DB step
# so every audit is bound to one manifest and exact IR file hashes.
export-unit-corpus-manifest:
	@test -n "$(OUT)" || (echo 'usage: make export-unit-corpus-manifest OUT=/private/tmp/corpus-manifest.jsonl' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/export_unit_corpus_manifest.py --output "$(OUT)"

audit-unit-corpus:
	@test -n "$(MANIFEST)" -a -n "$(OUT)" || (echo 'usage: make audit-unit-corpus MANIFEST=/path/manifest.jsonl OUT=/private/tmp/audit-dir [WORKERS=4] [LIMIT=N] [SOURCE_REPLAY=YES]' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/audit_unit_corpus.py --manifest "$(MANIFEST)" --out "$(OUT)" $(if $(WORKERS),--workers "$(WORKERS)") $(if $(LIMIT),--limit "$(LIMIT)") $(if $(filter YES,$(SOURCE_REPLAY)),--source-replay)

# Weekly sweep keeps only audits that do not propose lexical parsing rules.
audit-weekly: audit-unmapped-codes

# Install the ops launchd jobs (postgres boot autostart + daily doctor).
install-ops-launchd:
	./scripts/install_ops_launchd.sh

doctor-full:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.doctor --full

export-reparse-manifest:
	@test -n "$(OUT)" || (echo 'usage: make export-reparse-manifest OUT=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/reset.jsonl' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/reparse_corpus.py \
		--export-manifest "$(OUT)"

verify-reparse-manifest:
	@test -n "$(MANIFEST)" || (echo 'usage: make verify-reparse-manifest MANIFEST=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/reset.jsonl' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/reparse_corpus.py \
		--verify --manifest "$(MANIFEST)"

create-reset-backup:
	@test -n "$(MANIFEST)" -a -n "$(BACKUP)" || (echo 'usage: make create-reset-backup MANIFEST=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/reset.jsonl BACKUP=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/pre-reset.dump (one bundle dir holds manifest+plan+backup)' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/create_corpus_reset_backup.py \
		--manifest "$(MANIFEST)" --backup "$(BACKUP)" \
		--pg-dump "$(PG_BIN)/pg_dump"

prove-reset-backup:
	@test -n "$(MANIFEST)" -a -n "$(BACKUP)" || (echo 'usage: make prove-reset-backup MANIFEST=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/reset.jsonl BACKUP=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/pre-reset.dump (one bundle dir holds manifest+plan+backup)' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/prove_corpus_reset_backup.py \
		--manifest "$(MANIFEST)" --backup "$(BACKUP)" \
		--pg-restore "$(PG_BIN)/pg_restore"

reparse-status:
	@test -n "$(MANIFEST)" -a -n "$(RESET_RECEIPT)" || (echo 'usage: make reparse-status MANIFEST=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/reset.jsonl RESET_RECEIPT=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/pre-reset.dump.reset-receipt.json' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/reparse_corpus.py \
		--status --manifest "$(MANIFEST)" \
		--reset-receipt "$(RESET_RECEIPT)"

purge-reset-trash:
	@test -n "$(MANIFEST)" -a -n "$(RESET_RECEIPT)" || (echo 'usage: make purge-reset-trash MANIFEST=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/reset.jsonl RESET_RECEIPT=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/pre-reset.dump.reset-receipt.json [PURGE=YES]  (gated: refuses unless reparse --status exits 0; without PURGE=YES only reports size)' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/reparse_corpus.py \
		--purge-trash --manifest "$(MANIFEST)" \
		--reset-receipt "$(RESET_RECEIPT)" $(if $(filter YES,$(PURGE)),--yes,)

purge-reset-trash-force:
	@test -n "$(MANIFEST)" || (echo 'usage: make purge-reset-trash-force MANIFEST=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/reset.jsonl [PURGE=YES]  (skips the verified-generation gate and destroys the only rollback material; without PURGE=YES only reports size)' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/reparse_corpus.py \
		--purge-trash --force --manifest "$(MANIFEST)" $(if $(filter YES,$(PURGE)),--yes,)

reparse-corpus:
	@test -n "$(MANIFEST)" -a -n "$(RESET_RECEIPT)" || (echo 'usage: make reparse-corpus MANIFEST=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/reset.jsonl RESET_RECEIPT=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/pre-reset.dump.reset-receipt.json' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/reparse_corpus.py \
		--run --manifest "$(MANIFEST)" \
		--reset-receipt "$(RESET_RECEIPT)"

reset-derived-rehearse:
	@test -n "$(MANIFEST)" -a -n "$(BACKUP)" || (echo 'usage: make reset-derived-rehearse MANIFEST=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/reset.jsonl BACKUP=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/pre-reset.dump (both in the same bundle dir)' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/reset_derived_corpus.py \
		--manifest "$(MANIFEST)" --backup "$(BACKUP)" --rehearse

wipe-derived:
	@test "$(WIPE)" = "YES" -a -n "$(MANIFEST)" -a -n "$(BACKUP)" || (echo 'usage: make wipe-derived WIPE=YES MANIFEST=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/reset.jsonl BACKUP=$$DISCLOSURE_DATA_ROOT/audit/reset-bundles/<name>/pre-reset.dump (both in the same bundle dir)' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/reset_derived_corpus.py \
		--manifest "$(MANIFEST)" --backup "$(BACKUP)" --apply
