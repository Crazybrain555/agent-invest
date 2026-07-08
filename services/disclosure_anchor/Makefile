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

.PHONY: doctor test test-unit test-contract test-data test-integration \
	test-mineru-smoke lint typecheck agent-check api archive pg-init pg-start pg-stop \
	pg-status db-create migrate register parse build-units publish process rebuild-units prune-history load-rules config-check track track-status sync worker-once worker-loop doctor-full wipe-test-data

doctor:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.doctor

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
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.db create

migrate:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m alembic upgrade head

# --- Single-document pipeline ------------------------------------------------

register:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline register --file "$(FILE)" --provider "$(PROVIDER)" --security-code "$(SECURITY_CODE)" --exchange "$(EXCHANGE)" --filing-type "$(FILING_TYPE)" --title "$(TITLE)" --announcement-date "$(ANNOUNCEMENT_DATE)" $(if $(REPORT_PERIOD),--report-period "$(REPORT_PERIOD)") $(if $(PROVIDER_DOCUMENT_ID),--provider-document-id "$(PROVIDER_DOCUMENT_ID)") $(if $(COMPANY_LEGAL_NAME),--company-legal-name "$(COMPANY_LEGAL_NAME)")

parse:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline parse --document-id "$(DOC)"

build-units:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline build-units --document-id "$(DOC)"

publish:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline publish --processing-run-id "$(RUN)" $(if $(ALLOW_EMPTY),--allow-empty) $(if $(REASON),--reason "$(REASON)")

process:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline process --document-id "$(DOC)" $(if $(ALLOW_EMPTY),--allow-empty) $(if $(REASON),--reason "$(REASON)")

# Rules-only iteration: reuse the latest succeeded parse artifacts, no MinerU.
rebuild-units:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline rebuild-units --document-id "$(DOC)" $(if $(ALLOW_EMPTY),--allow-empty) $(if $(REASON),--reason "$(REASON)")

# TEST-PHASE ONLY: delete superseded (non-active) runs + their units + events,
# so the DB shows exactly the current generation. Production retention is a
# separate post-launch decision.
prune-history:
	@test "$(PRUNE)" = "YES" || (echo 'refusing: set PRUNE=YES to confirm' && exit 2)
	./scripts/prune_history.sh

# Load class_map.json + facet_map.json into disclosure_core.classification_rule.
# Vocabulary upgrade = edit JSON + bump version + run this (views derive live).
load-rules:
	PYTHONPATH=src .venv/bin/python scripts/load_classification_rules.py

# Offline validation of config/watchlist.csv + config/processing_policy.json
# (HA check_config pattern): file/line errors, no DB access.
config-check:
	PYTHONPATH=src .venv/bin/python scripts/config_check.py

# Upsert the company watchlist (config/watchlist.csv) into tracked_company.
# Offline + idempotent; the worker backfills new companies on its next round.
track: config-check
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline track $(if $(FILE),--file "$(FILE)") $(if $(CODES),--codes "$(CODES)") $(if $(PRUNE_DRIFT),--prune-drift) $(if $(DRY_RUN),--dry-run)

track-status:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline track-status

sync:
	@test -n "$(COMPANY)" || (echo 'usage: make sync COMPANY=<scode> [WINDOW=N]' && exit 2); \
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline sync --company $(COMPANY) $(if $(WINDOW),--window $(WINDOW))

worker-once:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.worker once

worker-loop:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.worker loop

doctor-full:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.doctor --full

wipe-test-data:
	@test "$(WIPE)" = "YES" || (echo 'usage: make wipe-test-data WIPE=YES  (test-phase only!)' && exit 2)
	WIPE=YES ./scripts/wipe_test_data.sh
