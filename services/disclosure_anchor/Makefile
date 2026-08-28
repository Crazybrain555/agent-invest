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
SSH_PORT ?= 22

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
WITH_WORKER_ENV = set -a; [ -r "$(DISCLOSURE_ENV_DIR)/worker.env" ] && . "$(DISCLOSURE_ENV_DIR)/worker.env"; set +a;

.PHONY: doctor test test-unit test-contract test-data test-integration \
	lint typecheck agent-check api archive pg-init pg-start pg-stop \
	pg-status db-create migrate register parse build-units publish process rebuild-units rebuild-search-projection load-rules config-check track track-status track-export untrack sync worker-once worker-loop doctor-full \
	worker-status worker-restart mineru-smoke mineru-staged-load mineru-phase-trace-capture mineru-tunnel-status install-mineru-tunnel mineru-remote-attest mineru-admission gc-orphans audit-unmapped-codes audit-weekly install-ops-launchd \
	generate-current-source-replay audit-live-unit-replay capacity-observe capacity-verify capacity-summary capacity-commission capacity-catalog

doctor:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.doctor

lint:
	$(PYTHON) -m ruff check src tests scripts

typecheck:
	$(PYTHON) -m mypy

# Aggregate deterministic gate for AI-agent edits: lint + types + no-DB tests +
# diff hygiene in one command. Live-DB tests and migration round trips stay in
# the scratch-DB integration runner and current migration contract.
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

audit-live-unit-replay:
	@test -n "$(REPLAY)" || { echo "REPLAY is required" >&2; exit 64; }
	@test -n "$(OUTPUT)" || { echo "OUTPUT is required" >&2; exit 64; }
	@test -n "$(SOURCE_REVISION)" || { echo "SOURCE_REVISION is required" >&2; exit 64; }
	@set -a; \
	. "$(DISCLOSURE_ENV_DIR)/worker.env"; \
	set +a; \
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/audit_live_unit_replay.py \
		--replay "$(REPLAY)" \
		--output "$(OUTPUT)" \
		--source-revision "$(SOURCE_REVISION)"

generate-current-source-replay:
	@test -n "$(EVALUATION)" || { echo "EVALUATION is required" >&2; exit 64; }
	@test -n "$(RECEIPT)" || { echo "RECEIPT is required" >&2; exit 64; }
	@test -n "$(SOURCE_REVISION)" || { echo "SOURCE_REVISION is required" >&2; exit 64; }
	@set -a; \
	. "$(DISCLOSURE_ENV_DIR)/worker.env"; \
	set +a; \
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/generate_current_source_replay.py \
		--evaluation-output "$(EVALUATION)" \
		--receipt-output "$(RECEIPT)" \
		--source-revision "$(SOURCE_REVISION)"

test-integration:
	@$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m tests.integration._runner

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
	@case "$(ALLOW_EMPTY)" in ""|YES) ;; *) echo 'ALLOW_EMPTY must be empty or YES' >&2; exit 2;; esac
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline publish --processing-run-id "$(RUN)" $(if $(filter YES,$(ALLOW_EMPTY)),--allow-empty) $(if $(REASON),--reason "$(REASON)")

process:
	@case "$(ALLOW_EMPTY)" in ""|YES) ;; *) echo 'ALLOW_EMPTY must be empty or YES' >&2; exit 2;; esac
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline process --document-id "$(DOC)" $(if $(filter YES,$(ALLOW_EMPTY)),--allow-empty) $(if $(REASON),--reason "$(REASON)")

# Rules-only iteration: reuse the latest succeeded parse artifacts, no MinerU.
rebuild-units:
	@case "$(ALLOW_EMPTY)" in ""|YES) ;; *) echo 'ALLOW_EMPTY must be empty or YES' >&2; exit 2;; esac
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline rebuild-units --document-id "$(DOC)" $(if $(filter YES,$(ALLOW_EMPTY)),--allow-empty) $(if $(REASON),--reason "$(REASON)")

# Rebuild the 06R derived search projection (U7): delta by default, ALL=YES for a
# full recompute + orphan prune after a retrieval rules/dictionary version bump.
rebuild-search-projection:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline rebuild-search-projection $(if $(filter YES,$(ALL)),--all)

# Load class_map.json + facet_map.json + filing_type_map.json into
# disclosure_core.classification_rule.
# Vocabulary upgrade = edit JSON + bump version + run this (views derive live).
load-rules:
	$(WITH_ENV) PYTHONPATH=src .venv/bin/python scripts/load_classification_rules.py

# Offline validation of watchlist + default research-universe sidecar + policy
# (HA check_config pattern): file/line errors, no DB access. FILE validates the
# exact custom import file; SCREEN_MANIFEST optionally binds its provenance.
config-check:
	@PYTHONPATH=src .venv/bin/python scripts/config_check.py \
		$(if $(FILE),--watchlist "$(FILE)") \
		$(if $(SCREEN_MANIFEST),--screen-manifest "$(SCREEN_MANIFEST)")

# Import companies into tracked_company (the DB is the pool's source of
# truth; the CSV is the import/seed + git-snapshot format). The DB write is
# idempotent; file/bulk profile enrichment is deferred to the audited worker.
track: config-check
	@case "$(PRUNE_DRIFT)" in ""|YES) ;; *) echo 'PRUNE_DRIFT must be empty or YES' >&2; exit 2;; esac
	@case "$(DRY_RUN)" in ""|1) ;; *) echo 'DRY_RUN must be empty or 1' >&2; exit 2;; esac
	@case "$(SKIP_PROFILE_RESOLUTION)" in ""|YES) ;; *) echo 'SKIP_PROFILE_RESOLUTION must be empty or YES' >&2; exit 2;; esac
	@{ [ -z "$(FILE)" ] || [ -z "$(CODES)" ]; } || { echo 'FILE and CODES are mutually exclusive' >&2; exit 2; }
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline track \
		$(if $(FILE),--file "$(FILE)") \
		$(if $(CODES),--codes "$(CODES)") \
		$(if $(SCREEN_MANIFEST),--screen-manifest "$(SCREEN_MANIFEST)") \
		$(if $(filter YES,$(PRUNE_DRIFT)),--prune-drift) \
		$(if $(filter 1,$(DRY_RUN)),--dry-run) \
		$(if $(CODES),$(if $(filter YES,$(SKIP_PROFILE_RESOLUTION)),--skip-profile-resolution),--skip-profile-resolution)

track-status:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline track-status

# Snapshot the DB pool to an explicit path. Never default to the reviewed
# config/watchlist.csv: pre-import evidence must not be overwritten by export.
track-export:
	@test -n "$(OUT)" || (echo 'usage: make track-export OUT=/timestamped/path/watchlist.csv' >&2; exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline track-export --out "$(OUT)"

# Remove companies from the pool (deletes tracked rows; company/documents
# stay). Reversible stop is status=paused via track/PUT instead.
untrack:
	@test -n "$(CODES)" || (echo 'usage: make untrack CODES=600887[,601318]' && exit 2)
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline untrack --codes "$(CODES)"

sync:
	@test -n "$(COMPANY)" || (echo 'usage: make sync COMPANY=<scode> [WINDOW=N]' && exit 2); \
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.pipeline sync --company $(COMPANY) $(if $(WINDOW),--window $(WINDOW))

worker-once:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.worker once

worker-loop:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.worker loop --progress terminal

# DB-free deployment gate: repeated multimodal canary + one frozen full-PDF
# official writer parse. The command removes its temporary parse tree and
# persists only explicit receipt/cache paths.
mineru-smoke:
	@test -n "$(RUNTIME_MANIFEST)" || (echo 'usage: make mineru-smoke RUNTIME_MANIFEST=/path/runtime.json RECEIPT=/path/receipt.json CANARY_CACHE=/path/canary.json [INPUT=/path/file.pdf EXPECTED_SHA256=...]' && exit 2)
	@test -n "$(RECEIPT)" || (echo 'mineru-smoke: RECEIPT is required' && exit 2)
	@test -n "$(CANARY_CACHE)" || (echo 'mineru-smoke: CANARY_CACHE is required' && exit 2)
	$(WITH_WORKER_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/mineru_smoke.py \
		--runtime-manifest "$(RUNTIME_MANIFEST)" \
		--receipt-out "$(RECEIPT)" \
		--canary-cache-out "$(CANARY_CACHE)" \
		$(if $(INPUT),--input "$(INPUT)") \
		$(if $(EXPECTED_SHA256),--expected-input-sha256 "$(EXPECTED_SHA256)")

# DB/queue-free fixed-API load gate. Stages are intentionally not configurable:
# 4/8/16 documents use a single API-facing outstanding submission; huge inputs
# remain exclusive. A pinned process-external observer proves epoch/OOM/RSS/memory.
mineru-staged-load:
	@test -n "$(RUNTIME_MANIFEST)" || (echo 'usage: make mineru-staged-load RUNTIME_MANIFEST=/path/runtime.json CORPUS_MANIFEST=/path/frozen-corpus.json EXPECTED_CORPUS_SHA256=<sha256> RECEIPT=/path/new-receipt.json SSH_HOST=<host> SSH_USER=<user> SSH_IDENTITY=/private/key SSH_KNOWN_HOSTS=/private/known_hosts DOCKER_MEMORY_RESERVE_BYTES=<bytes> [WORK_ROOT=/private/scratch DOCUMENT_RUNAWAY_TIMEOUT_SECONDS=86400 API_DRAIN_TIMEOUT_SECONDS=86400]' && exit 2)
	@test -z "$(TIMEOUT_SECONDS)" || (echo 'mineru-staged-load: TIMEOUT_SECONDS is obsolete; use the two explicit safety-limit variables' && exit 2)
	@test -n "$(CORPUS_MANIFEST)" || (echo 'mineru-staged-load: CORPUS_MANIFEST is required' && exit 2)
	@test -n "$(EXPECTED_CORPUS_SHA256)" || (echo 'mineru-staged-load: EXPECTED_CORPUS_SHA256 is required' && exit 2)
	@test -n "$(RECEIPT)" || (echo 'mineru-staged-load: RECEIPT is required' && exit 2)
	@test -n "$(SSH_HOST)" || (echo 'mineru-staged-load: SSH_HOST is required' && exit 2)
	@test -n "$(SSH_USER)" || (echo 'mineru-staged-load: SSH_USER is required' && exit 2)
	@test -n "$(SSH_IDENTITY)" || (echo 'mineru-staged-load: SSH_IDENTITY is required' && exit 2)
	@test -n "$(SSH_KNOWN_HOSTS)" || (echo 'mineru-staged-load: SSH_KNOWN_HOSTS is required' && exit 2)
	@test -n "$(DOCKER_MEMORY_RESERVE_BYTES)" || (echo 'mineru-staged-load: DOCKER_MEMORY_RESERVE_BYTES is required' && exit 2)
	$(WITH_WORKER_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/mineru_staged_load.py \
		--runtime-manifest "$(RUNTIME_MANIFEST)" \
		--corpus-manifest "$(CORPUS_MANIFEST)" \
		--expected-corpus-sha256 "$(EXPECTED_CORPUS_SHA256)" \
		--receipt-out "$(RECEIPT)" \
		--host-observer-ssh-host "$(SSH_HOST)" \
		--host-observer-ssh-user "$(SSH_USER)" \
		--host-observer-ssh-port "$(SSH_PORT)" \
		--host-observer-identity-file "$(SSH_IDENTITY)" \
		--host-observer-known-hosts-file "$(SSH_KNOWN_HOSTS)" \
		--docker-memory-reserve-bytes "$(DOCKER_MEMORY_RESERVE_BYTES)" \
		$(if $(WORK_ROOT),--work-root "$(WORK_ROOT)") \
		--document-runaway-timeout-seconds "$(or $(DOCUMENT_RUNAWAY_TIMEOUT_SECONDS),86400)" \
		--api-drain-timeout-seconds "$(or $(API_DRAIN_TIMEOUT_SECONDS),86400)"

# Capture only strict content-free phase lines for one already-finished staged
# arm, bound them to its external host/container epoch, and prove page closure.
mineru-phase-trace-capture:
	@test -n "$(STAGED_RECEIPT)" || (echo 'mineru-phase-trace-capture: STAGED_RECEIPT is required' && exit 2)
	@test -n "$(CAPACITY_MODE)" || (echo 'mineru-phase-trace-capture: CAPACITY_MODE is required' && exit 2)
	@test -n "$(PROFILE_SHA256)" || (echo 'mineru-phase-trace-capture: PROFILE_SHA256 is required' && exit 2)
	@test -n "$(CAPTURE)" || (echo 'mineru-phase-trace-capture: CAPTURE is required' && exit 2)
	@test -n "$(SSH_HOST)" || (echo 'mineru-phase-trace-capture: SSH_HOST is required' && exit 2)
	@test -n "$(SSH_USER)" || (echo 'mineru-phase-trace-capture: SSH_USER is required' && exit 2)
	@test -n "$(SSH_IDENTITY)" || (echo 'mineru-phase-trace-capture: SSH_IDENTITY is required' && exit 2)
	@test -n "$(SSH_KNOWN_HOSTS)" || (echo 'mineru-phase-trace-capture: SSH_KNOWN_HOSTS is required' && exit 2)
	$(WITH_WORKER_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m scripts.collect_mineru_phase_trace \
		--staged-load-receipt "$(STAGED_RECEIPT)" \
		--capacity-mode "$(CAPACITY_MODE)" \
		--profile-sha256 "$(PROFILE_SHA256)" \
		--ssh-host "$(SSH_HOST)" --ssh-user "$(SSH_USER)" --ssh-port "$(SSH_PORT)" \
		--ssh-identity "$(SSH_IDENTITY)" --ssh-known-hosts "$(SSH_KNOWN_HOSTS)" \
		--capture-out "$(CAPTURE)"

# Offline constrained search verdict. The immutable profile is commissionable
# only when its repeated gain clears the predeclared materiality/noise policy.
capacity-commission:
	@for name in A1_RECEIPT A1_CAPTURE B1_RECEIPT B1_CAPTURE B2_RECEIPT B2_CAPTURE A2_RECEIPT A2_CAPTURE LEGACY_PROFILE_SHA256 CANDIDATE_PROFILE_SHA256 WINDOWS_NODE_IDENTITY_SHA256 DOCKER_MEMORY_RESERVE_BYTES MINIMUM_IMPROVEMENT_BASIS_POINTS MAXIMUM_REPEAT_SPREAD_BASIS_POINTS RECEIPT; do \
		eval "value=\$$$${name}"; [ -n "$$value" ] || { echo "capacity-commission: $$name is required" >&2; exit 2; }; \
	done
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/evaluate_mineru_capacity_commissioning.py \
		--a1-receipt "$(A1_RECEIPT)" --a1-capture "$(A1_CAPTURE)" \
		--b1-receipt "$(B1_RECEIPT)" --b1-capture "$(B1_CAPTURE)" \
		--b2-receipt "$(B2_RECEIPT)" --b2-capture "$(B2_CAPTURE)" \
		--a2-receipt "$(A2_RECEIPT)" --a2-capture "$(A2_CAPTURE)" \
		--legacy-profile-sha256 "$(LEGACY_PROFILE_SHA256)" \
		--candidate-profile-sha256 "$(CANDIDATE_PROFILE_SHA256)" \
		--windows-node-identity-sha256 "$(WINDOWS_NODE_IDENTITY_SHA256)" \
		--docker-memory-reserve-bytes "$(DOCKER_MEMORY_RESERVE_BYTES)" \
		--minimum-improvement-basis-points "$(MINIMUM_IMPROVEMENT_BASIS_POINTS)" \
		--maximum-repeat-spread-basis-points "$(MAXIMUM_REPEAT_SPREAD_BASIS_POINTS)" \
		--receipt-out "$(RECEIPT)"

# Generate the only Auto-admission sidecar from an exact COMMISSION receipt.
capacity-catalog:
	@for name in COMMISSIONING_RECEIPT CAPACITY_PROFILE RUNTIME_COMPATIBILITY_SHA256 CATALOG; do \
		eval "value=\$$$${name}"; [ -n "$$value" ] || { echo "capacity-catalog: $$name is required" >&2; exit 2; }; \
	done
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/build_mineru_capacity_catalog.py \
		--commissioning-receipt "$(COMMISSIONING_RECEIPT)" \
		--profile "$(CAPACITY_PROFILE)" \
		--runtime-compatibility-sha256 "$(RUNTIME_COMPATIBILITY_SHA256)" \
		--catalog-out "$(CATALOG)"

# DB-free and data-plane-independent capacity observation. It only reads the
# exact attested API/vLLM/GPU/host surfaces and writes private replay evidence
# under DISCLOSURE_RUNTIME_ROOT/reports/capacity/<run-id>/.
capacity-observe:
	@test -n "$(RUNTIME_MANIFEST)" || (echo 'capacity-observe: RUNTIME_MANIFEST is required' && exit 2)
	@test -n "$(DURATION_SECONDS)" || (echo 'capacity-observe: DURATION_SECONDS is required' && exit 2)
	@test -n "$(SSH_HOST)" || (echo 'capacity-observe: SSH_HOST is required' && exit 2)
	@test -n "$(SSH_USER)" || (echo 'capacity-observe: SSH_USER is required' && exit 2)
	@test -n "$(SSH_IDENTITY)" || (echo 'capacity-observe: SSH_IDENTITY is required' && exit 2)
	@test -n "$(SSH_KNOWN_HOSTS)" || (echo 'capacity-observe: SSH_KNOWN_HOSTS is required' && exit 2)
	$(WITH_WORKER_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.capacity observe \
		--runtime-manifest "$(RUNTIME_MANIFEST)" \
		--duration-seconds "$(DURATION_SECONDS)" \
		$(if $(INTERVAL_SECONDS),--interval-seconds "$(INTERVAL_SECONDS)") \
		--host-observer-ssh-host "$(SSH_HOST)" \
		--host-observer-ssh-user "$(SSH_USER)" \
		--host-observer-ssh-port "$(SSH_PORT)" \
		--host-observer-identity-file "$(SSH_IDENTITY)" \
		--host-observer-known-hosts-file "$(SSH_KNOWN_HOSTS)" \
		$(if $(RUN_ID),--run-id "$(RUN_ID)")

capacity-verify:
	@test -n "$(RUN_ID)" || (echo 'capacity-verify: RUN_ID is required' && exit 2)
	$(WITH_WORKER_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.capacity verify \
		--run-id "$(RUN_ID)" $(if $(filter YES,$(REQUIRE_COMPLETE)),--require-complete)

capacity-summary:
	@test -n "$(RUN_ID)" || (echo 'capacity-summary: RUN_ID is required' && exit 2)
	$(WITH_WORKER_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.capacity summarize --run-id "$(RUN_ID)"

# Fixed API transport is a separate LaunchAgent from the worker. It may stay
# resident while the worker is unloaded, but never exposes a non-loopback port.
mineru-tunnel-status:
	@./scripts/mineru_tunnel_status.sh

install-mineru-tunnel:
	@./scripts/install_mineru_tunnel_launchd.sh

# Read-only v6 attestation uses the operator identity because it executes the
# fixed PowerShell collector. It must never use the forwarding-only tunnel key.
mineru-remote-attest:
	@test -n "$(MINERU_BIN)" || { echo 'MINERU_BIN is required' >&2; exit 64; }
	@test -n "$(SSH_HOST)" || { echo 'SSH_HOST is required' >&2; exit 64; }
	@test -n "$(SSH_USER)" || { echo 'SSH_USER is required' >&2; exit 64; }
	@test -n "$(SSH_IDENTITY)" || { echo 'SSH_IDENTITY is required' >&2; exit 64; }
	@test -n "$(SSH_KNOWN_HOSTS)" || { echo 'SSH_KNOWN_HOSTS is required' >&2; exit 64; }
	@test -n "$(MANIFEST)" || { echo 'MANIFEST is required' >&2; exit 64; }
	@test -n "$(OBSERVATION)" || { echo 'OBSERVATION is required' >&2; exit 64; }
	@PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/attest_mineru_remote_runtime.py \
		--mineru-bin "$(MINERU_BIN)" \
		--ssh-host "$(SSH_HOST)" \
		--ssh-user "$(SSH_USER)" \
		--ssh-port "$(SSH_PORT)" \
		--identity-file "$(SSH_IDENTITY)" \
		--known-hosts-file "$(SSH_KNOWN_HOSTS)" \
		--expected-compose "$(CURDIR)/config/mineru-windows.compose.yaml" \
		--collector-source "$(CURDIR)/scripts/windows/collect_mineru_runtime.ps1" \
		--compat-patcher-source "$(CURDIR)/scripts/windows/mineru_heap_trim_compat/patch_mineru_344.py" \
		--compat-dockerfile-source "$(CURDIR)/scripts/windows/mineru_heap_trim_compat/Dockerfile" \
		$(if $(REMOTE_COLLECTOR_PATH),--remote-collector-path "$(REMOTE_COLLECTOR_PATH)") \
		--manifest-out "$(MANIFEST)" \
		--observation-out "$(OBSERVATION)"

# DB-free resident admission check against the current private worker.env.
mineru-admission:
	@$(WITH_WORKER_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -c \
		'from disclosure_anchor.adapters.runtime.mineru_deployment_gate import require_mineru_deployment_gate; from disclosure_anchor.settings import load_settings; require_mineru_deployment_gate(load_settings(), parse_enabled=True); print("MinerU admission: PASS")'

# Resident worker control. kickstart reloads code/env only for a job whose
# installed plist is already current; plist changes require the staged
# drain/bootout/install procedure in the production runbook.
worker-status:
	@launchctl print gui/$$(id -u)/com.agentinvest.disclosure-worker 2>/dev/null \
		| grep -E "state|pid|last exit" || echo "[warn] worker job not loaded"
	@$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.worker status

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

# Legacy NIR corpus replay and reset commands were retired with the provider writer.

# Weekly sweep keeps only audits that do not propose lexical parsing rules.
audit-weekly: audit-unmapped-codes

# Install the ops launchd jobs (postgres boot autostart + daily doctor).
install-ops-launchd:
	./scripts/install_ops_launchd.sh

doctor-full:
	$(WITH_ENV) PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m disclosure_anchor.cli.doctor --full
