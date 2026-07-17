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
	pg-status db-create migrate register parse build-units publish process rebuild-units rebuild-search-projection prune-history load-rules config-check track track-status track-export untrack purge-company sync worker-once worker-loop doctor-full wipe-test-data \
	worker-status worker-restart gc-orphans audit-unmapped-codes audit-boilerplate audit-headings audit-unit-corpus export-unit-corpus-manifest audit-weekly install-ops-launchd

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
	@PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests/integration -t . -p 'test_*.py' || [ $$? -eq 5 ]

# Explicit opt-in real-MinerU end-to-end smoke (not collected by `make test`).
# Requires DISCLOSURE_MIGRATION_DATABASE_URL, DISCLOSURE_MINERU_BIN and the
# local short_announcement sample PDF; skips cleanly when any is absent.
test-mineru-smoke:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest tests.integration.smoke_real_mineru -v

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

# TEST-PHASE ONLY: delete superseded (non-active) runs + their units + events,
# so the DB shows exactly the current generation. Production retention is a
# separate post-launch decision.
prune-history:
	@test "$(PRUNE)" = "YES" || (echo 'refusing: set PRUNE=YES to confirm' && exit 2)
	./scripts/prune_history.sh

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

# TEST-PHASE ONLY: cascade-delete ONE company (rows + files) — the
# single-company version of wipe-test-data, for undoing mistakes/residue.
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

# Resident worker control (batch 4, 2026-07-14). Config/code/env changes only
# take effect after an explicit restart — see config/README.md 生效矩阵.
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
	@echo "[note] restarted; run 'make doctor-full' and watch one round before walking away"

# Orphan parser-artifact inventory (dry-run; APPLY=YES deletes with an audit
# manifest). Raw documents are never touched.
gc-orphans:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/gc_orphan_artifacts.py $(if $(filter YES,$(APPLY)),--apply)

# --- Data-quality audits (batch 4: previously script-only, no make entry) ----

audit-unmapped-codes:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/audit_unmapped_codes.py

audit-boilerplate:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/audit_boilerplate_candidates.py

audit-headings:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/audit_heading_coverage.py

# Deterministic file-only replay gate. Export is a separate read-only DB step
# so every audit is bound to one manifest and exact IR file hashes.
export-unit-corpus-manifest:
	@test -n "$(OUT)" || (echo 'usage: make export-unit-corpus-manifest OUT=/private/tmp/corpus-manifest.jsonl' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/export_unit_corpus_manifest.py --output "$(OUT)"

audit-unit-corpus:
	@test -n "$(MANIFEST)" -a -n "$(OUT)" || (echo 'usage: make audit-unit-corpus MANIFEST=/path/manifest.jsonl OUT=/private/tmp/audit-dir [WORKERS=4]' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/audit_unit_corpus.py --manifest "$(MANIFEST)" --out "$(OUT)" $(if $(WORKERS),--workers "$(WORKERS)")

# Weekly sweep: run all three audits; each exits nonzero on a real finding.
audit-weekly: audit-unmapped-codes audit-boilerplate audit-headings

# Install the ops launchd jobs (postgres boot autostart + daily doctor).
install-ops-launchd:
	./scripts/install_ops_launchd.sh

doctor-full:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.doctor --full

wipe-test-data:
	@test "$(WIPE)" = "YES" || (echo 'usage: make wipe-test-data WIPE=YES  (test-phase only!)' && exit 2)
	$(WITH_ENV) WIPE=YES ./scripts/wipe_test_data.sh
