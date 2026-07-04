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
	test-mineru-smoke api archive pg-init pg-start pg-stop pg-status db-create migrate

doctor:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.doctor

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
